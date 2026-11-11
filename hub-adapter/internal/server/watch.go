package server

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"sync"
	"time"

	"github.com/a2aproject/a2a-go/a2a"
	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"

	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/run"
	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/store"
)

// Watcher is the M3 watch controller (design §6 renewal bridge, §7 status
// flow-back, §5.5 terminal-run Secret cleanup). It is fed by the
// controller-runtime informer glue (internal/watch) in production and
// directly by tests; every entry point re-projects the affected task into
// the store, and the store signals open SSE streams.
//
// It NEVER writes conditions on runs (design §6.1: the adapter's only
// resume-relevant write is the run-token Secret) and never touches token
// values.
type Watcher struct {
	cfg  Config
	deps Deps
	log  *slog.Logger

	mu sync.Mutex
	// cleanups maps runUID → the time its run-token Secret becomes
	// deletable (terminal observed + grace). Timer-free: the sweeper
	// checks due times, so a terminal→non-terminal revision re-open just
	// cancels the entry (design §5.5 edge).
	cleanups map[string]time.Time
}

// NewWatcher builds the watch controller sharing this handler's config,
// collaborators, and (critically) task store — watch projections and A2A
// reads must land in the same records.
func (h *Handler) NewWatcher() (*Watcher, error) {
	if err := fillDefaults(&h.cfg, &h.deps); err != nil {
		return nil, err
	}
	return &Watcher{cfg: h.cfg, deps: h.deps, log: h.deps.Logger, cleanups: map[string]time.Time{}}, nil
}

// Start runs the sweeper loop (pause deadlines, §6.4; cleanup due-times,
// §5.5) until ctx is cancelled.
func (w *Watcher) Start(ctx context.Context) {
	ticker := time.NewTicker(w.cfg.SweepInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			w.Sweep(ctx, w.cfg.now())
		}
	}
}

// RunChanged re-projects the task bound to runCR. It is the AgenticRun
// informer's (and the test harness's) entry point; it is also safe to call
// repeatedly with unchanged state — the store dedupes, so an informer
// relist after a watch drop/reconnect neither duplicates nor loses task
// updates.
func (w *Watcher) RunChanged(ctx context.Context, runCR *agenticv1alpha1.AgenticRun) {
	if runCR == nil {
		return
	}
	taskID := runCR.Annotations[run.AnnotationTaskID]
	if taskID == "" {
		return // not ours
	}

	rec, ok := w.deps.Store.Task(taskID)
	if !ok {
		// Lazy rehydration (design §2.3) — same shape as the handler's
		// findTask, for events that arrive before any A2A read.
		rec = recordFromRun(runCR)
	} else if rec.RunUID != "" && runCR.UID != "" && rec.RunUID != string(runCR.UID) {
		// Deterministic name reused by a different run: a taskId
		// collision. Do not project someone else's run onto this task.
		w.log.Warn("run UID mismatch for known task; ignoring event",
			"task_id", taskID, "record_run_uid", rec.RunUID, "event_run_uid", string(runCR.UID))
		return
	}

	terminal := RunTerminalPhase(runCR)

	// §5.5: terminal → schedule Secret cleanup after the grace period;
	// non-terminal (including the revision re-open edge) → cancel it.
	if terminal {
		if rec.RunUID != "" {
			w.mu.Lock()
			w.cleanups[rec.RunUID] = w.cfg.now().Add(w.cfg.TerminalCleanupGrace)
			w.mu.Unlock()
		}
	} else {
		w.cancelCleanup(rec.RunUID)
	}

	// §6.1 steps 1-2: CredentialsExpired=True on a non-terminal run pauses
	// the EXISTING task (auth-required overlay), never a new task.
	if !terminal && CredentialsExpired(runCR) {
		if !rec.AuthPaused {
			rec.PauseCount++
			// Persist the counter on the CR (§6.4 durability): a restart
			// or crash-looping adapter must not reset the pause cap. The
			// annotation is restored by recordFromRun on rehydration.
			w.persistPauseCount(ctx, runCR, rec.PauseCount)
			if rec.PauseCount > w.cfg.MaxPauses {
				// §6.4: total-pause cap — a flapping credential must not
				// pause-loop forever.
				rec.AdapterTerminal = true
				rec.FailureReason = fmt.Sprintf(
					"credential renewal pause cap exceeded (%d pauses, max %d); task failed",
					rec.PauseCount-1, w.cfg.MaxPauses)
				rec.AuthPaused = false
				w.log.Warn("task failed: credential pause cap exceeded",
					"task_id", taskID, "run", rec.RunName, "pauses", rec.PauseCount-1)
			} else {
				rec.AuthPaused = true
				rec.AuthPausedAt = w.cfg.now()
				w.log.Info("task paused for credential renewal",
					"task_id", taskID, "run", rec.RunName, "pause_count", rec.PauseCount)
			}
		}
	} else if terminal && rec.AuthPaused {
		// A terminal phase wins over a lingering pause overlay — the
		// sweeper must not later fail an already-terminal task.
		rec.AuthPaused = false
	}

	// §6.4, restart-durable: a rehydrated pause whose ORIGINAL anchor
	// (the condition's lastTransitionTime, restored by recordFromRun) is
	// already older than the renewal timeout fails immediately — not on
	// the next sweeper tick. Without this, a restart mid-pause would
	// grant the task a fresh window.
	w.expirePauseIfDue(rec)

	rec, _, err := w.projectTask(ctx, rec)
	if err != nil {
		if errors.Is(err, a2a.ErrTaskNotFound) {
			return // run CR gone (TTL GC); reads observe TaskNotFound
		}
		w.log.Error("project task from run event", "task_id", taskID, "error", err)
		return
	}
	w.deps.Store.PutTask(rec)
}

// RunDeleted handles the AgenticRun informer's delete events: the task's
// subscribers must wake (their next projection observes TaskNotFound per
// design §2.3 result retention), and any run-token Secret still standing
// is removed — the adapter-written Secret carries no owner ref, so it
// would otherwise outlive the run.
func (w *Watcher) RunDeleted(ctx context.Context, runCR *agenticv1alpha1.AgenticRun) {
	if runCR == nil {
		return
	}
	w.cancelCleanup(string(runCR.UID))
	if runCR.UID != "" {
		if err := w.deps.Tokens.Delete(ctx, string(runCR.UID)); err != nil {
			w.log.Error("delete run-token Secret for deleted run", "run_uid", string(runCR.UID), "error", err)
		}
	}
	if taskID := runCR.Annotations[run.AnnotationTaskID]; taskID != "" {
		w.deps.Store.Signal(taskID)
	}
}

// ObjectChanged fans a Result CR or AgenticRunApproval event onto the
// parent run's task (design §7.1: results and approvals are watched to
// re-project artifacts and approval state). AgenticRun events are
// dispatched straight to RunChanged.
func (w *Watcher) ObjectChanged(ctx context.Context, obj interface{}) {
	switch o := obj.(type) {
	case *agenticv1alpha1.AgenticRun:
		w.RunChanged(ctx, o)
		return
	case *agenticv1alpha1.AnalysisResult:
		w.runChangedByName(ctx, o.Spec.AgenticRunName)
	case *agenticv1alpha1.ExecutionResult:
		w.runChangedByName(ctx, o.Spec.AgenticRunName)
	case *agenticv1alpha1.VerificationResult:
		w.runChangedByName(ctx, o.Spec.AgenticRunName)
	case *agenticv1alpha1.EscalationResult:
		w.runChangedByName(ctx, o.Spec.AgenticRunName)
	case *agenticv1alpha1.AgenticRunApproval:
		// 1:1 with the run — same name, same namespace (repo C
		// agenticrunapproval_types.go).
		w.runChangedByName(ctx, o.Name)
	}
}

func (w *Watcher) runChangedByName(ctx context.Context, runName string) {
	if runName == "" {
		return
	}
	runCR, err := w.deps.Readers.GetRun(ctx, w.cfg.Namespace, runName)
	if err != nil {
		return // run gone or not yet visible; the run's own informer covers it
	}
	w.RunChanged(ctx, runCR)
}

// expirePauseIfDue fails a task whose renewal-pause deadline (§6.4) has
// passed: the pause's ORIGINAL anchor (AuthPausedAt — condition
// lastTransitionTime when rehydrated) plus RenewalTimeout. Shared by
// RunChanged (immediate check on rehydration) and Sweep.
func (w *Watcher) expirePauseIfDue(rec *store.TaskRecord) bool {
	if !rec.AuthPaused || rec.AdapterTerminal {
		return false
	}
	now := w.cfg.now()
	if !now.After(rec.AuthPausedAt.Add(w.cfg.RenewalTimeout)) {
		return false
	}
	rec.AdapterTerminal = true
	rec.AuthPaused = false
	rec.FailureReason = fmt.Sprintf(
		"credential renewal deadline exceeded (%s pause started %s ago); task failed",
		w.cfg.RenewalTimeout, now.Sub(rec.AuthPausedAt).Round(time.Second))
	w.log.Warn("task failed: credential renewal deadline exceeded",
		"task_id", rec.TaskID, "run", rec.RunName)
	return true
}

// persistPauseCount stamps the §6.4 pause counter onto the run CR as the
// a2a.lightspeed.io/credential-pauses annotation (see server.recordFromRun
// for the read-back). Runs as the adapter SA (RBAC: patch on agenticruns).
// Best-effort on failure: the in-memory count still applies until the
// next event re-delivers the CR, and the sweeper bound does not depend on
// it — only the cap's restart durability does.
func (w *Watcher) persistPauseCount(ctx context.Context, runCR *agenticv1alpha1.AgenticRun, count int) {
	if runCR == nil || runCR.Name == "" {
		return // parked pre-submit task: no CR to persist on (documented unrecoverable)
	}
	if s := strconv.Itoa(count); runCR.Annotations[run.AnnotationCredentialPauses] != s {
		updated := runCR.DeepCopy()
		if updated.Annotations == nil {
			updated.Annotations = map[string]string{}
		}
		updated.Annotations[run.AnnotationCredentialPauses] = s
		if err := w.deps.Readers.UpdateRun(ctx, updated); err != nil {
			w.log.Error("persist credential pause count on run CR",
				"run", runCR.Name, "error", err)
		}
	}
}

// Sweep enforces the §6.4 pause deadlines and the §5.5 cleanup due-times.
// It is exported so tests can drive it deterministically with a controlled
// clock; Start drives it on a ticker in production.
func (w *Watcher) Sweep(ctx context.Context, now time.Time) {
	for _, rec := range w.deps.Store.All() {
		if w.expirePauseIfDue(rec) {
			w.deps.Store.PutTask(rec)
		}
	}

	w.mu.Lock()
	var due []string
	for uid, at := range w.cleanups {
		if !now.Before(at) {
			due = append(due, uid)
		}
	}
	for _, uid := range due {
		delete(w.cleanups, uid)
	}
	w.mu.Unlock()
	for _, uid := range due {
		if err := w.deps.Tokens.Delete(ctx, uid); err != nil {
			// Re-arm for the next sweep rather than dropping the cleanup.
			w.mu.Lock()
			w.cleanups[uid] = now.Add(w.cfg.TerminalCleanupGrace)
			w.mu.Unlock()
			w.log.Error("delete run-token Secret at terminal", "run_uid", uid, "error", err)
			continue
		}
		w.log.Info("deleted run-token Secret after terminal grace", "run_uid", uid)
	}
}

func (w *Watcher) cancelCleanup(runUID string) {
	if runUID == "" {
		return
	}
	w.mu.Lock()
	delete(w.cleanups, runUID)
	w.mu.Unlock()
}

// projectTask runs the shared projection (Handler.project) so watch events
// and A2A reads can never disagree about a task's state.
func (w *Watcher) projectTask(ctx context.Context, rec *store.TaskRecord) (*store.TaskRecord, *a2a.Task, error) {
	h := Handler{cfg: w.cfg, deps: w.deps}
	return h.project(ctx, rec)
}
