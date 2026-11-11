// Package store holds the adapter's in-memory A2A task/context/message
// records (design §2.2) and the deterministic AgenticRun naming that
// substitutes for MAS's REJECT_DUPLICATE idempotency.
//
// The store is a CACHE, not the source of truth: every CR-backed task is
// reconstructible from AgenticRun annotations (restart recovery, design
// §2.3), so losing this map to a restart costs nothing durable. Records for
// tasks that never got a CR (auth-required before first submit) are
// legitimately lost on restart — the caller re-sends.
//
// M3 adds per-task SUBSCRIPTIONS (design §7.1): the watch controller
// projects AgenticRun/Result/Approval changes into records via PutTask, and
// open message/stream + tasks/resubscribe SSE holds wait on the signal
// channel instead of polling. PutTask signals only when the caller-visible
// projection changed, so an informer relist after a watch drop/reconnect
// (which re-delivers every object) produces no duplicate updates and loses
// none.
package store

import (
	"crypto/sha256"
	"encoding/hex"
	"sync"
	"time"
)

// RunNamePrefix is the AgenticRun name prefix for adapter-created runs
// (design §4.1: "a2a-<taskId>", deterministic, DNS-safe).
const RunNamePrefix = "a2a-"

// TaskRecord is the adapter-maintained taskId → run mapping (design §2.2).
// It never contains token values.
type TaskRecord struct {
	TaskID        string
	ContextID     string
	RunName       string // deterministic AgenticRun name; empty until first submit
	RunUID        string // from the created CR; empty until first submit
	Principal     string // auth.PrincipalID(sub, iss) of the verified caller
	Target        string // resolved spec.targetCluster; immutable per task
	LastMessageID string
	// HasRun is false only for tasks parked in auth-required BEFORE their
	// first submit (design §6.2) — no CR exists for them.
	HasRun bool
	// PendingRequest holds the joined text of the message that parked the
	// task in auth-required. When a renewal follow-up finally creates the
	// run, THIS text (the original question) becomes spec.request — the
	// credential-only renewal message must not overwrite it.
	PendingRequest string
	// State is the last projected A2A task state. For HasRun tasks it is
	// refreshed from the CR on every projection; it is authoritative only
	// for no-run (auth-required) tasks.
	State string
	// AuthPaused is the renewal-bridge overlay (design §6.1 step 2): the
	// bound run observed CredentialsExpired=True (or the pre-start expiry
	// check parked a not-yet-submitted task). While set, the task projects
	// auth-required REGARDLESS of the derived phase (the condition does
	// not participate in DerivePhase, repo C contract).
	AuthPaused bool
	// AuthPausedAt is when the CURRENT pause started — the anchor for the
	// per-pause renewal deadline (design §6.4).
	AuthPausedAt time.Time
	// PauseCount is how many times the bound run has paused for
	// credentials. Capped by MaxPauses (design §6.4) so a flapping
	// credential cannot pause-loop forever.
	PauseCount int
	// AdapterTerminal marks a task the ADAPTER itself failed (pause
	// deadline exhausted, or the pause cap exceeded). The AgenticRun may
	// still exist (repo C has no bound on a credential pause — it requeues
	// every 30 s indefinitely), but the A2A task is terminal and renewal
	// must not revive it (design §6.3).
	AdapterTerminal bool
	// FailureReason is the adapter-side terminal reason (credential
	// timeout / pause cap), surfaced as the failed task's status message.
	FailureReason string
	// ArtifactIDs is the snapshot of the projected artifact IDs (derived
	// from Result CR UIDs, design §7.2), for change detection only.
	ArtifactIDs []string
	CreatedAt   time.Time
}

// Clone returns a deep copy so callers cannot mutate store internals.
func (r *TaskRecord) Clone() *TaskRecord {
	copied := *r
	copied.ArtifactIDs = append([]string(nil), r.ArtifactIDs...)
	return &copied
}

// ContextRecord maps a contextId to its owning principal and task list
// (design §2.2: one investigation conversation, owned by the principal).
type ContextRecord struct {
	ContextID string
	Principal string
	TaskIDs   []string
}

// Store is the concurrency-safe in-memory record set.
type Store struct {
	mu       sync.RWMutex
	tasks    map[string]*TaskRecord
	contexts map[string]*ContextRecord
	messages map[string]string // messageId -> taskId

	subMu       sync.Mutex
	subSeq      uint64
	subscribers map[string]map[uint64]chan struct{}
}

// New returns an empty store.
func New() *Store {
	return &Store{
		tasks:       map[string]*TaskRecord{},
		contexts:    map[string]*ContextRecord{},
		messages:    map[string]string{},
		subscribers: map[string]map[uint64]chan struct{}{},
	}
}

// RunName derives the deterministic AgenticRun name from contextId+taskId.
// This name IS the idempotency key (design §2.2): a duplicate create with
// the same name is a get, a name collision with a different spec fails
// closed, and an "uncertain start" retry re-creates with the identical name
// and identical spec, which the CRD's CEL immutability makes a no-op.
//
// The name is sha256-based rather than a sanitized taskId because caller
// taskIds are arbitrary strings (not DNS-safe). Hashing contextId in as
// well means the same taskId under two contexts (two conversations) maps to
// two distinct runs.
func RunName(contextID, taskID string) string {
	sum := sha256.Sum256([]byte(contextID + "\x00" + taskID))
	return RunNamePrefix + hex.EncodeToString(sum[:])[:40]
}

// Task returns a copy of the record for taskID.
func (s *Store) Task(taskID string) (*TaskRecord, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	rec, ok := s.tasks[taskID]
	if !ok {
		return nil, false
	}
	return rec.Clone(), true
}

// All returns copies of every task record (the sweeper's iteration source).
func (s *Store) All() []*TaskRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*TaskRecord, 0, len(s.tasks))
	for _, rec := range s.tasks {
		out = append(out, rec.Clone())
	}
	return out
}

// TaskForMessage returns the task a messageId was already delivered to
// (duplicate-delivery detection, design §2.2: a duplicate messageId returns
// the current task record WITHOUT re-submitting).
func (s *Store) TaskForMessage(messageID string) (*TaskRecord, bool) {
	s.mu.RLock()
	taskID, ok := s.messages[messageID]
	s.mu.RUnlock()
	if !ok {
		return nil, false
	}
	return s.Task(taskID)
}

// PutTask upserts a task record, its context index entry, and its message
// binding. It signals the task's subscribers iff the caller-visible
// projection changed (state, pause overlay, adapter-terminal failure, or
// artifact set) — an identical re-put (informer relist after a watch
// reconnect) is silent. Returns whether a change was signaled.
func (s *Store) PutTask(rec *TaskRecord) bool {
	s.mu.Lock()
	old, existed := s.tasks[rec.TaskID]
	stored := rec.Clone()
	s.tasks[rec.TaskID] = stored

	ctx, ok := s.contexts[rec.ContextID]
	if !ok {
		ctx = &ContextRecord{ContextID: rec.ContextID, Principal: rec.Principal}
		s.contexts[rec.ContextID] = ctx
	}
	found := false
	for _, id := range ctx.TaskIDs {
		if id == rec.TaskID {
			found = true
			break
		}
	}
	if !found {
		ctx.TaskIDs = append(ctx.TaskIDs, rec.TaskID)
	}
	if rec.LastMessageID != "" {
		s.messages[rec.LastMessageID] = rec.TaskID
	}
	s.mu.Unlock()

	changed := !existed || projectionChanged(old, stored)
	if changed {
		s.Signal(rec.TaskID)
	}
	return changed
}

// projectionChanged compares the fields subscribers can observe. Everything
// else (bookkeeping like LastMessageID, PauseCount, timestamps) is silent —
// a renewal signals via AuthPaused, a re-pause via AuthPausedAt+AuthPaused.
func projectionChanged(old, new *TaskRecord) bool {
	return old.State != new.State ||
		old.AuthPaused != new.AuthPaused ||
		old.AdapterTerminal != new.AdapterTerminal ||
		old.FailureReason != new.FailureReason ||
		old.HasRun != new.HasRun ||
		!sameStrings(old.ArtifactIDs, new.ArtifactIDs)
}

func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// PutMessage records a messageId → task binding. Returns false (and does
// nothing) if the messageId was already seen — the caller must then treat
// the delivery as a duplicate and NOT submit a second run.
func (s *Store) PutMessage(messageID, taskID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, exists := s.messages[messageID]; exists {
		return false
	}
	s.messages[messageID] = taskID
	return true
}

// Context returns a copy of the context record.
func (s *Store) Context(contextID string) (*ContextRecord, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	ctx, ok := s.contexts[contextID]
	if !ok {
		return nil, false
	}
	copied := *ctx
	copied.TaskIDs = append([]string(nil), ctx.TaskIDs...)
	return &copied, true
}

// Subscribe returns a signal channel for taskID plus a cancel func. The
// channel is buffered(1) and coalescing: at least one signal arrives after
// every PutTask that changed the projection; multiple changes between
// reads collapse into one signal. Cancel must be called exactly once (the
// stream tail does it via defer).
func (s *Store) Subscribe(taskID string) (<-chan struct{}, func()) {
	ch := make(chan struct{}, 1)
	s.subMu.Lock()
	if s.subscribers[taskID] == nil {
		s.subscribers[taskID] = map[uint64]chan struct{}{}
	}
	s.subSeq++
	id := s.subSeq
	s.subscribers[taskID][id] = ch
	s.subMu.Unlock()

	cancel := func() {
		s.subMu.Lock()
		defer s.subMu.Unlock()
		if subs := s.subscribers[taskID]; subs != nil {
			delete(subs, id)
			if len(subs) == 0 {
				delete(s.subscribers, taskID)
			}
		}
	}
	return ch, cancel
}

// Signal wakes every subscriber of taskID without changing any record —
// used when the backing CR disappears (tasks/get must then observe
// TaskNotFound rather than waiting out the stream deadline).
func (s *Store) Signal(taskID string) {
	s.subMu.Lock()
	defer s.subMu.Unlock()
	for _, ch := range s.subscribers[taskID] {
		select {
		case ch <- struct{}{}:
		default: // already signaled; coalesced
		}
	}
}
