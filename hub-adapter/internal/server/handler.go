package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"iter"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/a2aproject/a2a-go/a2a"
	"github.com/a2aproject/a2a-go/a2asrv"
	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/auth"
	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/run"
	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/store"
)

// Config holds the server's tunables.
type Config struct {
	// Namespace is where AgenticRuns (and their run-token Secrets) live —
	// the repo C operator namespace (design §8.3 / Q4).
	Namespace string
	// TargetHeader is the fixed hub-leg transport header carrying the
	// target cluster (design §4.2; "X-Hub-Cluster" proposed, renameable
	// at the chart level).
	TargetHeader string
	// ClockSkew is the grace after exp in the pre-start expiry check
	// (design §6.2; see auth.Expired).
	ClockSkew time.Duration
	// StreamDeadline bounds a message/stream hold (design §2.3; default
	// 30 min, less than repo C's agent execution budget). A renewal
	// pause does NOT count against it (§2.3/§6.4): while a task is
	// auth-required the deadline is pushed out, and the pause's own
	// bound (RenewalTimeout) ends the stream instead.
	StreamDeadline time.Duration
	// RenewalTimeout bounds how long a task stays auth-required before
	// the adapter fails it with a credential-timeout reason (design
	// §6.4; default 900 s, aligned with MAS's worker-side bound — ACME
	// gives up after 2 renewals, so a pause older than one worker bound
	// can no longer be answered).
	RenewalTimeout time.Duration
	// MaxPauses caps the total credential pauses per run (design §6.4,
	// default 3) so a flapping credential cannot pause-loop forever.
	MaxPauses int
	// TerminalCleanupGrace is how long a terminal run's run-token Secret
	// survives before deletion (design §5.5; the grace absorbs the
	// terminal→non-terminal revision re-open edge).
	TerminalCleanupGrace time.Duration
	// SweepInterval is how often the watcher's sweeper checks pause
	// deadlines and cleanup due-times.
	SweepInterval time.Duration
	// Now overrides the clock (tests).
	Now func() time.Time
}

func (c Config) now() time.Time {
	if c.Now != nil {
		return c.Now()
	}
	return time.Now()
}

// Deps are the handler's collaborators.
type Deps struct {
	Store *store.Store
	// Credentials extracts Token A (+ binding) from the inbound request
	// (design §3.3). Adapter-fallback mode: auth.HeaderSource.
	Credentials auth.CredentialSource
	// Exchanger performs the A→H exchange when the credential source did
	// not (adapter-fallback placement, design §3.2). Nil means the source
	// must supply TokenH itself (future Praxis-side placement).
	Exchanger auth.Exchanger
	// Readers reads AgenticRuns as the ADAPTER SA (RBAC §8.1:
	// get/list/watch in the run namespace). Used for tasks/get,
	// projections, and restart recovery.
	Readers run.RunClient
	// Results reads Result CRs and AgenticRunApprovals as the ADAPTER SA
	// (design §7.1/§7.2: artifacts, token usage, approval state).
	Results run.ResultClient
	// Submitters builds per-request RunClients authenticated AS THE
	// CALLER via Token H (design §3.3: never create runs — or make
	// approval decisions — as the adapter SA). Used for
	// CreateRun/UpdateRun/UpdateApproval.
	Submitters run.RunClientFactory
	// Runs maps messages onto AgenticRun CRs (design §4.1).
	Runs *run.Creator
	// Tokens writes and deletes run-token Secrets (design §5), as the
	// adapter SA.
	Tokens *run.TokenWriter
	// Logger receives audit-style lines. Never token values.
	Logger *slog.Logger
}

// Handler implements a2asrv.RequestHandler: the A2A surface of design §2
// on top of the store + AgenticRun machinery.
type Handler struct {
	cfg  Config
	deps Deps
}

var _ a2asrv.RequestHandler = (*Handler)(nil)

// New validates and wires a Handler.
func New(cfg Config, deps Deps) (*Handler, error) {
	if err := fillDefaults(&cfg, &deps); err != nil {
		return nil, err
	}
	return &Handler{cfg: cfg, deps: deps}, nil
}

func fillDefaults(cfg *Config, deps *Deps) error {
	if cfg.Namespace == "" {
		return errors.New("server: namespace is required")
	}
	if cfg.TargetHeader == "" {
		cfg.TargetHeader = "X-Hub-Cluster"
	}
	if cfg.StreamDeadline == 0 {
		cfg.StreamDeadline = 30 * time.Minute
	}
	if cfg.RenewalTimeout == 0 {
		cfg.RenewalTimeout = 900 * time.Second
	}
	if cfg.MaxPauses == 0 {
		cfg.MaxPauses = 3
	}
	if cfg.TerminalCleanupGrace == 0 {
		cfg.TerminalCleanupGrace = time.Minute
	}
	if cfg.SweepInterval == 0 {
		cfg.SweepInterval = 5 * time.Second
	}
	if deps.Store == nil || deps.Credentials == nil || deps.Readers == nil ||
		deps.Results == nil || deps.Submitters == nil || deps.Runs == nil || deps.Tokens == nil {
		return errors.New("server: all deps are required")
	}
	if deps.Logger == nil {
		deps.Logger = slog.Default()
	}
	return nil
}

// ---- request capture -------------------------------------------------------

// The SDK's JSON-RPC layer does not hand the *http.Request to the handler;
// this middleware stashes it in the context so CredentialSource can read
// the inbound Authorization and target headers.
type ctxKeyRequest struct{}

// WithRequestCapture must wrap the JSON-RPC handler on the HTTP mux.
func WithRequestCapture(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ctxKeyRequest{}, r)))
	})
}

func requestFromContext(ctx context.Context) (*http.Request, error) {
	req, ok := ctx.Value(ctxKeyRequest{}).(*http.Request)
	if !ok {
		return nil, errors.New("no http request in context (WithRequestCapture missing?)")
	}
	return req, nil
}

// ---- credential helpers ----------------------------------------------------

func (h *Handler) credential(ctx context.Context) (*auth.Credential, error) {
	req, err := requestFromContext(ctx)
	if err != nil {
		return nil, err
	}
	cred, err := h.deps.Credentials.Credential(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", a2a.ErrUnauthenticated, err)
	}
	return cred, nil
}

// tokenH returns the hub-audience token for kube API calls, exchanging A
// if the credential source did not (design §3.2 adapter-fallback).
func (h *Handler) tokenH(ctx context.Context, cred *auth.Credential) (string, error) {
	if cred.TokenH != "" {
		return cred.TokenH, nil
	}
	if h.deps.Exchanger == nil {
		return "", fmt.Errorf("%w: credential source supplied no TokenH and no exchanger is configured", a2a.ErrInternalError)
	}
	tokenH, err := h.deps.Exchanger.Exchange(ctx, cred.TokenA)
	if err != nil {
		var xe *auth.ExchangeError
		if errors.As(err, &xe) {
			switch xe.Kind {
			case auth.ErrorKindInvalidGrant:
				// Typically: Token A does not carry the exchanging client
				// as audience (documented prerequisite).
				return "", fmt.Errorf("%w: subject token rejected by exchange", a2a.ErrUnauthenticated)
			case auth.ErrorKindInvalidClient, auth.ErrorKindDenied:
				return "", fmt.Errorf("%w: %v", a2a.ErrUnauthorized, err)
			}
		}
		return "", fmt.Errorf("%w: %v", a2a.ErrInternalError, err)
	}
	return tokenH, nil
}

// ---- message parsing helpers -----------------------------------------------

// textOf joins the message's text parts verbatim (design §4.1:
// spec.request = text parts, joined).
func textOf(msg *a2a.Message) string {
	var texts []string
	for _, part := range msg.Parts {
		if tp, ok := part.(a2a.TextPart); ok {
			texts = append(texts, tp.Text)
		}
	}
	return strings.Join(texts, "\n")
}

// dataPartsOf returns the data payloads of all DataParts.
func dataPartsOf(msg *a2a.Message) []map[string]any {
	var out []map[string]any
	for _, part := range msg.Parts {
		if dp, ok := part.(a2a.DataPart); ok {
			out = append(out, dp.Data)
		}
	}
	return out
}

// reviseOf extracts the "revise" structured follow-up DataPart (design
// §4.1: revisionFeedback path) — a DataPart carrying {"revise": "<text>"}.
func reviseOf(msg *a2a.Message) string {
	for _, dp := range dataPartsOf(msg) {
		if raw, ok := dp["revise"]; ok {
			if s, ok := raw.(string); ok {
				return s
			}
		}
	}
	return ""
}

// approvalDecision is the parsed approve/reject DataPart (design §7.1: the
// structured decision DataPart, replied on the same task with a new
// messageId). MINIMAL M3 REPRESENTATION, documented: the design's full
// vocabulary is approve/reject/modify/redirect, but repo C's
// AgenticRunApproval only accepts Approved/Denied per stage (plus an
// execution option index) — "modify"/"redirect" have no CR-side mapping
// and are rejected as invalid params rather than silently degraded.
// The stage is REQUIRED (Analysis|Execution|Verification|Escalation):
// guessing it would put words in the approver's mouth.
type approvalDecision struct {
	Stage   agenticv1alpha1.ApprovalStageType
	Denied  bool
	Option  *int32
	Comment string // optional free-text reason (log only; no CR field)
}

func approvalDecisionOf(msg *a2a.Message) (*approvalDecision, error) {
	for _, dp := range dataPartsOf(msg) {
		raw, ok := dp["approve"]
		if !ok {
			raw, ok = dp["reject"]
		}
		if !ok {
			continue
		}
		fields, _ := raw.(map[string]any)
		if fields == nil {
			return nil, fmt.Errorf("approval decision DataPart must be an object with a stage field")
		}
		stageRaw, _ := fields["stage"].(string)
		stage := agenticv1alpha1.ApprovalStageType(stageRaw)
		switch stage {
		case agenticv1alpha1.ApprovalStageAnalysis, agenticv1alpha1.ApprovalStageExecution,
			agenticv1alpha1.ApprovalStageVerification, agenticv1alpha1.ApprovalStageEscalation:
		default:
			return nil, fmt.Errorf("approval decision requires a valid stage (Analysis|Execution|Verification|Escalation), got %q", stageRaw)
		}
		dec := &approvalDecision{Stage: stage, Denied: dp["reject"] != nil}
		if c, ok := fields["comment"].(string); ok {
			dec.Comment = c
		}
		if opt, ok := fields["option"].(float64); ok {
			if stage != agenticv1alpha1.ApprovalStageExecution {
				return nil, fmt.Errorf("option index only applies to an Execution approval")
			}
			if opt < 0 {
				return nil, fmt.Errorf("option index must be >= 0")
			}
			v := int32(opt)
			dec.Option = &v
		}
		return dec, nil
	}
	return nil, nil
}

// ---- projection (design §7) -------------------------------------------------

// taskFromRecord refreshes and projects a task record into an A2A Task.
// It is the single read path for tasks/get, stream events, and send
// responses: state from the CR's derived phase (+ the auth-required pause
// overlay from the record), status message from the run's own conditions,
// artifacts and token usage from the Result CRs referenced in
// status.steps.
func (h *Handler) taskFromRecord(ctx context.Context, rec *store.TaskRecord) (*a2a.Task, error) {
	_, task, err := h.project(ctx, rec)
	return task, err
}

// project refreshes rec in place (state, artifact snapshot, terminal
// overlay) and returns the refreshed record plus its A2A projection. It
// never mutates the store — callers decide whether to PutTask.
func (h *Handler) project(ctx context.Context, rec *store.TaskRecord) (*store.TaskRecord, *a2a.Task, error) {
	if !rec.HasRun {
		// Parked pre-submit (design §6.2): the record is authoritative.
		state := a2a.TaskStateAuthRequired
		message := StatusMessageFor(nil, state)
		if rec.AdapterTerminal {
			state = a2a.TaskStateFailed
			message = rec.FailureReason
		}
		rec.State = string(state)
		now := h.cfg.now()
		return rec, &a2a.Task{
			ID:        a2a.TaskID(rec.TaskID),
			ContextID: rec.ContextID,
			Status:    a2a.TaskStatus{State: state, Timestamp: &now, Message: h.statusMessage(message)},
		}, nil
	}

	runCR, err := h.deps.Readers.GetRun(ctx, h.cfg.Namespace, rec.RunName)
	if err != nil {
		if apierrors.IsNotFound(err) {
			// CR GC'd by repo C's TTL: a documented, non-surprising
			// terminal answer (design §2.3).
			return nil, nil, a2a.ErrTaskNotFound
		}
		return nil, nil, fmt.Errorf("get agenticrun %s: %w", rec.RunName, err)
	}
	if runCR.Annotations[run.AnnotationTaskID] != rec.TaskID {
		// The record and the CR disagree about who owns this name — treat
		// as missing rather than projecting someone else's run.
		return nil, nil, a2a.ErrTaskNotFound
	}

	state := TaskStateFor(runCR)
	message := StatusMessageFor(runCR, state)
	rec.State = string(state)

	artifacts, usage := h.artifactsFor(ctx, runCR)
	ids := make([]string, len(artifacts))
	for i, art := range artifacts {
		ids[i] = string(art.ID)
	}
	rec.ArtifactIDs = ids

	// Overlays: an adapter-side terminal failure (pause deadline/cap)
	// wins; then the auth-required pause (§6.1 step 2) — on the EXISTING
	// task, never a new task.
	if rec.AdapterTerminal {
		state = a2a.TaskStateFailed
		message = rec.FailureReason
	} else if rec.AuthPaused {
		state = a2a.TaskStateAuthRequired
		message = StatusMessageFor(nil, a2a.TaskStateAuthRequired)
	}
	if state == a2a.TaskStateInputRequired {
		if detail := h.approvalDetail(ctx, runCR); detail != "" {
			message = detail
		}
	}

	now := h.cfg.now()
	task := &a2a.Task{
		ID:        a2a.TaskID(rec.TaskID),
		ContextID: rec.ContextID,
		Status:    a2a.TaskStatus{State: state, Timestamp: &now, Message: h.statusMessage(message)},
		Artifacts: artifacts,
	}
	if usage != nil {
		task.Metadata = map[string]any{"tokenUsage": usage}
	}
	return rec, task, nil
}

// HealthHandler serves GET /healthz: a plain 200 with no auth. The
// adapter is an internal-only Service behind the NetworkPolicy (Praxis
// namespace ingress only), so an unauthenticated liveness/readiness probe
// is safe by construction — see the README's HTTP surface section.
func HealthHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
}

func (h *Handler) statusMessage(text string) *a2a.Message {
	if text == "" {
		return nil
	}
	return &a2a.Message{
		Role:  a2a.MessageRoleAgent,
		ID:    string(a2a.NewTaskID()),
		Parts: []a2a.Part{a2a.TextPart{Text: text}},
	}
}

// approvalDetail names the stage a Proposed run is waiting on, derived
// from the run's AgenticRunApproval: the first workflow stage not yet
// present in spec.stages (stages are append-only, decisions immutable).
// Documented minimal representation — repo C does not expose a "pending
// stage" field, and its controller-side gating semantics (ApprovalPolicy
// vs per-run approval) are not fully inspectable from the CR alone.
func (h *Handler) approvalDetail(ctx context.Context, runCR *agenticv1alpha1.AgenticRun) string {
	order := []agenticv1alpha1.ApprovalStageType{
		agenticv1alpha1.ApprovalStageAnalysis,
		agenticv1alpha1.ApprovalStageExecution,
		agenticv1alpha1.ApprovalStageVerification,
		agenticv1alpha1.ApprovalStageEscalation,
	}
	pending := order[0]
	approval, err := h.deps.Results.GetApproval(ctx, h.cfg.Namespace, runCR.Name)
	if err == nil && approval != nil {
		seen := map[agenticv1alpha1.ApprovalStageType]bool{}
		for _, stage := range approval.Spec.Stages {
			seen[stage.Type] = true
		}
		for _, stageType := range order {
			if !seen[stageType] {
				pending = stageType
				break
			}
		}
	}
	return fmt.Sprintf(
		"approval requested for stage %s: reply on this task with a DataPart "+
			`{"approve":{"stage":"%s"}} (add "option":<index> for Execution) `+
			`or {"reject":{"stage":"%s"}}`,
		pending, pending, pending)
}

// artifactsFor builds the A2A artifacts (design §7.2) from the Result CRs
// referenced in the run's status.steps: one artifact per step kind, from
// the NEWEST Succeeded attempt. Artifact IDs are derived from the Result
// CR's UID so they are stable across adapter restarts. Also returns the
// aggregated token usage, if any Result CR exposes it. Missing Result CRs
// (not yet created, or already GC'd) are simply absent — never an error.
func (h *Handler) artifactsFor(ctx context.Context, runCR *agenticv1alpha1.AgenticRun) ([]*a2a.Artifact, map[string]any) {
	var artifacts []*a2a.Artifact
	var in, out int64
	addUsage := func(u agenticv1alpha1.TokenUsage) {
		if u.InputTokens != nil {
			in += *u.InputTokens
		}
		if u.OutputTokens != nil {
			out += *u.OutputTokens
		}
	}

	refs := runCR.Status.Steps.Analysis.Results
	if name, ok := newestSucceeded(refs); ok {
		if res, err := h.deps.Results.GetAnalysisResult(ctx, h.cfg.Namespace, name); err == nil {
			artifacts = append(artifacts, resultArtifact("analysis", string(res.UID), analysisText(res), res.Status))
			addUsage(res.Status.TokenUsage)
		}
	}
	refs = runCR.Status.Steps.Execution.Results
	if name, ok := newestSucceeded(refs); ok {
		if res, err := h.deps.Results.GetExecutionResult(ctx, h.cfg.Namespace, name); err == nil {
			artifacts = append(artifacts, resultArtifact("execution", string(res.UID), executionText(res), res.Status))
			addUsage(res.Status.TokenUsage)
		}
	}
	refs = runCR.Status.Steps.Verification.Results
	if name, ok := newestSucceeded(refs); ok {
		if res, err := h.deps.Results.GetVerificationResult(ctx, h.cfg.Namespace, name); err == nil {
			artifacts = append(artifacts, resultArtifact("verification", string(res.UID), verificationText(res), res.Status))
			addUsage(res.Status.TokenUsage)
		}
	}
	refs = runCR.Status.Steps.Escalation.Results
	if name, ok := newestSucceeded(refs); ok {
		if res, err := h.deps.Results.GetEscalationResult(ctx, h.cfg.Namespace, name); err == nil {
			artifacts = append(artifacts, resultArtifact("escalation", string(res.UID), "escalation result", res.Status))
			addUsage(res.Status.TokenUsage)
		}
	}

	var usage map[string]any
	if in > 0 || out > 0 {
		usage = map[string]any{"inputTokens": in, "outputTokens": out}
	}
	return artifacts, usage
}

func newestSucceeded(refs []agenticv1alpha1.StepResultRef) (string, bool) {
	for i := len(refs) - 1; i >= 0; i-- {
		if refs[i].Outcome == agenticv1alpha1.ActionOutcomeSucceeded {
			return refs[i].Name, true
		}
	}
	return "", false
}

func analysisText(res *agenticv1alpha1.AnalysisResult) string {
	if res.Status.Diagnosis.Summary != "" {
		return res.Status.Diagnosis.Summary
	}
	var titles []string
	for _, opt := range res.Status.Options {
		titles = append(titles, opt.Title)
	}
	if len(titles) == 0 {
		return "analysis result"
	}
	return strings.Join(titles, "; ")
}

func executionText(res *agenticv1alpha1.ExecutionResult) string {
	var acts []string
	for _, a := range res.Status.ActionsTaken {
		acts = append(acts, fmt.Sprintf("%s: %s (%s)", a.Type, a.Description, a.Outcome))
	}
	if len(acts) == 0 {
		return "execution result"
	}
	return strings.Join(acts, "\n")
}

func verificationText(res *agenticv1alpha1.VerificationResult) string {
	if res.Status.Summary != "" {
		return res.Status.Summary
	}
	return "verification result"
}

// resultArtifact builds the artifact: a text part (human summary) plus a
// structured DataPart carrying the result CR's status payload (the
// analysis options schema etc., design §7.2).
func resultArtifact(step string, uid, text string, status any) *a2a.Artifact {
	data := map[string]any{}
	if raw, err := json.Marshal(status); err == nil {
		_ = json.Unmarshal(raw, &data)
	}
	return &a2a.Artifact{
		// Stable across adapter restarts (design §7.2): derived from the
		// Result CR's UID, never freshly generated.
		ID:   a2a.ArtifactID("result-" + string(uid)),
		Name: step,
		Parts: []a2a.Part{
			a2a.TextPart{Text: text},
			a2a.DataPart{Data: data},
		},
	}
}

// findTask locates a task record, lazily rehydrating from AgenticRun
// annotations after an adapter restart (design §2.3), then enforces
// ownership (design §5.6): a principal mismatch is TaskNotFound, never
// AccessDenied — do not confirm task existence to a non-owner.
func (h *Handler) findTask(ctx context.Context, taskID string, principal string) (*store.TaskRecord, error) {
	if rec, ok := h.deps.Store.Task(taskID); ok {
		return rec, nil
	}
	list, err := h.deps.Readers.ListRuns(ctx, h.cfg.Namespace)
	if err != nil {
		return nil, fmt.Errorf("list agenticruns for recovery: %w", err)
	}
	for i := range list.Items {
		runCR := &list.Items[i]
		if runCR.Annotations[run.AnnotationTaskID] != taskID {
			continue
		}
		rec := recordFromRun(runCR)
		h.deps.Store.PutTask(rec)
		h.deps.Logger.Info("rehydrated task from agenticrun",
			"task_id", rec.TaskID, "run", rec.RunName)
		return rec, nil
	}
	return nil, a2a.ErrTaskNotFound
}

// recordFromRun rebuilds a task record from an AgenticRun's annotations
// (lazy rehydration, design §2.3). When the CR carries a live
// CredentialsExpired condition, the §6.4 pause state is restored with it:
// AuthPausedAt is anchored to the condition's lastTransitionTime (the
// ORIGINAL wall-clock pause start — not "now" — so an adapter restart
// cannot extend the renewal deadline), and PauseCount is restored from the
// a2a.lightspeed.io/credential-pauses annotation so the pause cap cannot
// be reset by a crash loop.
func recordFromRun(runCR *agenticv1alpha1.AgenticRun) *store.TaskRecord {
	rec := &store.TaskRecord{
		TaskID:        runCR.Annotations[run.AnnotationTaskID],
		ContextID:     runCR.Annotations[run.AnnotationContextID],
		RunName:       runCR.Name,
		RunUID:        string(runCR.UID),
		Principal:     runCR.Annotations[run.AnnotationPrincipal],
		Target:        runCR.Spec.TargetCluster,
		LastMessageID: runCR.Annotations[run.AnnotationMessageID],
		HasRun:        true,
		State:         string(TaskStateFor(runCR)),
		CreatedAt:     runCR.CreationTimestamp.Time,
	}
	// The pause COUNTER is cumulative and independent of the condition's
	// current state (it survives renewals and condition clears).
	if n, err := strconv.Atoi(runCR.Annotations[run.AnnotationCredentialPauses]); err == nil && n > 0 {
		rec.PauseCount = n
	}
	if cond := findCondition(runCR, agenticv1alpha1.AgenticRunConditionCredentialsExpired); cond != nil &&
		cond.Status == metav1.ConditionTrue && !RunTerminalPhase(runCR) {
		rec.AuthPaused = true
		rec.AuthPausedAt = cond.LastTransitionTime.Time
		if rec.AuthPausedAt.IsZero() {
			// Defensive: a condition without a transition time (never
			// produced by a real API server) falls back to the CR's
			// creation, not to "now" — the deadline must not be extended
			// by missing data.
			rec.AuthPausedAt = runCR.CreationTimestamp.Time
		}
	}
	return rec
}

// Recover rebuilds the in-memory store from AgenticRun annotations at
// startup (design §2.3: no REQUIRED in-memory state; every CR-backed task
// is recoverable). Tasks parked in auth-required without a CR are
// legitimately unrecoverable — the caller re-sends.
func (h *Handler) Recover(ctx context.Context) error {
	list, err := h.deps.Readers.ListRuns(ctx, h.cfg.Namespace)
	if err != nil {
		return fmt.Errorf("list agenticruns for recovery: %w", err)
	}
	for i := range list.Items {
		runCR := &list.Items[i]
		if runCR.Annotations[run.AnnotationTaskID] == "" {
			continue // not ours
		}
		h.deps.Store.PutTask(recordFromRun(runCR))
	}
	h.deps.Logger.Info("startup recovery complete", "runs", len(list.Items))
	return nil
}

// ---- message/send core -----------------------------------------------------

// handleSend implements message/send (and the submission half of
// message/stream): design §2.2 ID mapping, §4.1 CR mapping, §4.2 target
// rules, §6.2 pre-start expiry check, §5 run-token write, §6.1 step 3-5
// renewal acceptance (ownership + immutability validated, Secret
// rewritten, task returned to its in-flight state).
func (h *Handler) handleSend(ctx context.Context, params *a2a.MessageSendParams) (*a2a.Task, error) {
	cred, err := h.credential(ctx)
	if err != nil {
		return nil, err
	}
	msg := params.Message
	if msg == nil || msg.ID == "" {
		return nil, fmt.Errorf("%w: message and messageId are required", a2a.ErrInvalidParams)
	}
	taskID := string(msg.TaskID)
	if taskID == "" {
		taskID = string(a2a.NewTaskID())
	}
	contextID := msg.ContextID
	if contextID == "" {
		contextID = a2a.NewContextID()
	}

	req, _ := requestFromContext(ctx)
	principal := auth.PrincipalID(cred.Subject, cred.Issuer)

	// Duplicate messageId: return the current task record WITHOUT
	// re-submitting (design §2.2). This makes a re-delivered renewal
	// message idempotent — no second Secret rewrite, no second approval
	// patch. Ownership still applies (§5.6): a non-owner replaying a
	// known messageId gets TaskNotFound, not the task.
	if rec, ok := h.deps.Store.TaskForMessage(msg.ID); ok {
		if rec.Principal != principal {
			return nil, a2a.ErrTaskNotFound
		}
		return h.taskFromRecord(ctx, rec)
	}

	// Deterministic target rules (design §4.2): structured data only,
	// duplicates/conflicts rejected.
	target, err := run.ResolveTarget(
		req.Header.Values(h.cfg.TargetHeader),
		msg.Metadata[run.MetadataTargetKey],
		dataPartsOf(msg),
	)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", a2a.ErrInvalidParams, err)
	}

	rec, exists := h.deps.Store.Task(taskID)
	if !exists {
		// Rehydrate-and-mask (design §5.6, same as findTask): after a
		// restart the in-memory store is empty, so a caller-supplied
		// taskID must be resolved against the AgenticRun annotations
		// BEFORE any create path runs — otherwise a foreign principal
		// sending the victim's taskID with a fresh messageId would
		// shadow the victim's record with a new (attacker-owned) one.
		// A rehydrated record owned by someone else hits the ownership
		// check below and is masked as TaskNotFound.
		r, err := h.findTask(ctx, taskID, principal)
		if err != nil {
			if !errors.Is(err, a2a.ErrTaskNotFound) {
				return nil, err
			}
		} else {
			rec, exists = r, true
		}
	}
	if exists {
		// Ownership (design §5.6): mismatch masks existence.
		if rec.Principal != principal {
			return nil, a2a.ErrTaskNotFound
		}
		if rec.ContextID != contextID {
			return nil, fmt.Errorf("%w: contextId mismatch for existing task", a2a.ErrInvalidParams)
		}
		// Target immutability on resume: a mismatched target is rejected,
		// never OR-matched (design §5.6).
		if target != "" && target != rec.Target {
			return nil, fmt.Errorf("%w: target is immutable for an existing task", a2a.ErrInvalidParams)
		}
	}

	// Pre-start expiry check (design §6.2): a dead Token A is never
	// written into a run-token Secret.
	expired, expErr := auth.Expired(cred.TokenA, h.cfg.now(), h.cfg.ClockSkew)
	if expErr != nil && !errors.Is(expErr, auth.ErrNoExpiry) {
		return nil, fmt.Errorf("%w: credential exp check: %v", a2a.ErrInvalidParams, expErr)
	}
	if expired {
		if rec == nil || !rec.HasRun {
			// No CR exists (fresh task, or parked pre-submit): park in
			// auth-required with the pause clock running (§6.4 bound).
			if rec == nil {
				rec = &store.TaskRecord{
					TaskID:    taskID,
					ContextID: contextID,
					Principal: principal,
					Target:    target,
					CreatedAt: h.cfg.now(),
				}
			}
			rec.HasRun = false
			rec.AuthPaused = true
			rec.AuthPausedAt = h.cfg.now()
			rec.State = string(a2a.TaskStateAuthRequired)
			rec.LastMessageID = msg.ID
			rec.PendingRequest = textOf(msg)
			h.deps.Store.PutTask(rec)
			h.deps.Logger.Warn("parked task in auth-required: token expired before submit",
				"task_id", taskID, "principal", principal, "credential_fp", cred.Fingerprint)
			return h.taskFromRecord(ctx, rec)
		}
		// Run-backed task, dead renewal token (§6.1 step 3 with a stale
		// A'): do NOT write a dead token and do NOT destroy the run
		// binding — the task stays wherever it is (auth-required if the
		// run paused; the previous state otherwise) and the caller may
		// retry with a live token.
		rec.LastMessageID = msg.ID
		h.deps.Store.PutTask(rec)
		h.deps.Logger.Warn("renewal follow-up carried an expired token; not written",
			"task_id", taskID, "run", rec.RunName, "credential_fp", cred.Fingerprint)
		return h.taskFromRecord(ctx, rec)
	}

	// Follow-up on a live run: revision feedback, an approval decision,
	// and/or credential renewal (design §4.1 revise; §6.1 steps 3-7).
	if exists && rec.HasRun {
		return h.handleFollowUp(ctx, rec, msg, cred, target)
	}

	// Fresh submit (new task, or a parked auth-required task now holding
	// fresh A — the parked task's original question wins over the
	// credential-only renewal text).
	request := textOf(msg)
	if rec != nil && rec.PendingRequest != "" {
		request = rec.PendingRequest
	}
	if request == "" {
		return nil, fmt.Errorf("%w: message carries no text parts", a2a.ErrInvalidParams)
	}
	if target == "" && rec != nil {
		target = rec.Target
	}

	tokenH, err := h.tokenH(ctx, cred)
	if err != nil {
		return nil, err
	}
	rc, err := h.deps.Submitters(ctx, tokenH)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", a2a.ErrInternalError, err)
	}

	newRun := h.deps.Runs.NewRun(run.NewRunInput{
		ContextID: contextID,
		TaskID:    taskID,
		Principal: principal,
		Target:    target,
		Request:   request,
		MessageID: msg.ID,
	})
	submitted, err := h.deps.Runs.Submit(ctx, rc, newRun)
	if err != nil {
		// ErrRunCollision (taskId collision) fails closed (design §2.2).
		return nil, fmt.Errorf("%w: %v", a2a.ErrInternalError, err)
	}

	// Write the run-token Secret immediately after CR creation (design
	// §5.4): the name needs the server-assigned UID; the common path
	// never pauses.
	if err := h.deps.Tokens.Write(ctx, submitted, cred.TokenA); err != nil {
		return nil, fmt.Errorf("%w: %v", a2a.ErrInternalError, err)
	}

	rec = &store.TaskRecord{
		TaskID:        taskID,
		ContextID:     contextID,
		RunName:       submitted.Name,
		RunUID:        string(submitted.UID),
		Principal:     principal,
		Target:        submitted.Spec.TargetCluster,
		LastMessageID: msg.ID,
		HasRun:        true,
		State:         string(TaskStateFor(submitted)),
		CreatedAt:     h.cfg.now(),
	}
	h.deps.Store.PutTask(rec)
	h.deps.Logger.Info("submitted agenticrun",
		"task_id", taskID, "run", submitted.Name, "run_uid", string(submitted.UID),
		"principal", principal, "target", target, "credential_fp", cred.Fingerprint)
	return h.taskFromRecord(ctx, rec)
}

// handleFollowUp implements the follow-up paths on an existing, run-backed
// task: renewal (§6.1 steps 3-7), revision feedback (§4.1), and approval
// decisions (§7.1). Ownership, context, and target-header checks have
// already passed in handleSend.
func (h *Handler) handleFollowUp(ctx context.Context, rec *store.TaskRecord, msg *a2a.Message, cred *auth.Credential, target string) (*a2a.Task, error) {
	runCR, err := h.deps.Readers.GetRun(ctx, h.cfg.Namespace, rec.RunName)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return nil, a2a.ErrTaskNotFound
		}
		return nil, fmt.Errorf("%w: %v", a2a.ErrInternalError, err)
	}

	// Verify target immutability against the bound run's ACTUAL spec
	// (design §6.1 step 4): spec.targetCluster is CEL-immutable in the
	// CRD, but the adapter verifies rather than trusting its own record.
	if target != "" && target != runCR.Spec.TargetCluster {
		return nil, fmt.Errorf("%w: target is immutable for an existing task", a2a.ErrInvalidParams)
	}
	if rec.Target != runCR.Spec.TargetCluster {
		return nil, fmt.Errorf("%w: bound run's targetCluster changed under the task record", a2a.ErrInternalError)
	}

	terminal := RunTerminalPhase(runCR) || rec.AdapterTerminal

	// Approval decision (§7.1): forwarded to the AgenticRunApproval CR AS
	// THE CALLER, so repo C's mutating admission webhook records the true
	// approver.
	decision, err := approvalDecisionOf(msg)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", a2a.ErrInvalidParams, err)
	}
	if decision != nil {
		if err := h.submitApprovalDecision(ctx, cred, rec, decision); err != nil {
			return nil, err
		}
	}

	// Revision feedback (§4.1): the CRD's same-run refinement path.
	if feedback := reviseOf(msg); feedback != "" {
		tokenH, err := h.tokenH(ctx, cred)
		if err != nil {
			return nil, err
		}
		rc, err := h.deps.Submitters(ctx, tokenH)
		if err != nil {
			return nil, fmt.Errorf("%w: %v", a2a.ErrInternalError, err)
		}
		if err := h.deps.Runs.SubmitRevision(ctx, rc, rec.RunName, feedback); err != nil {
			return nil, fmt.Errorf("%w: %v", a2a.ErrInternalError, err)
		}
	}

	if terminal {
		// §6.3: a fresh A does not revive a terminal/cancelled task, and
		// §5.5's revision re-open edge says park new A on the NEXT
		// message — so no Secret rewrite here. The revision above may
		// re-open the run; the watch will observe terminal→non-terminal
		// and the next valid follow-up renews through this same path.
		rec.LastMessageID = msg.ID
		h.deps.Store.PutTask(rec)
		h.deps.Logger.Info("follow-up on terminal task: credential not rewritten",
			"task_id", rec.TaskID, "run", rec.RunName)
		return h.taskFromRecord(ctx, rec)
	}

	// Renewal (§6.1 step 5): rewrite token-a with fresh A. Labels are
	// untouched (run/token.go); the resourceVersion change is repo C's
	// resume signal (it re-attempts the paused step within its 30 s
	// requeue — gateway-credential-mode.md "Resume latency").
	if err := h.deps.Tokens.Write(ctx, runCR, cred.TokenA); err != nil {
		return nil, fmt.Errorf("%w: %v", a2a.ErrInternalError, err)
	}
	rec.AuthPaused = false
	rec.LastMessageID = msg.ID
	h.deps.Store.PutTask(rec) // signals streams: back to in-flight
	h.deps.Logger.Info("renewed run credential",
		"task_id", rec.TaskID, "run", rec.RunName, "credential_fp", cred.Fingerprint)
	return h.taskFromRecord(ctx, rec)
}

// submitApprovalDecision appends the decision stage to the run's
// AgenticRunApproval as the caller (Token H). Idempotent: a stage of the
// same type with the same decision is left alone (repo C's CRD makes
// stages append-only and decisions immutable — a conflicting re-decision
// is the API server's to reject).
func (h *Handler) submitApprovalDecision(ctx context.Context, cred *auth.Credential, rec *store.TaskRecord, decision *approvalDecision) error {
	approval, err := h.deps.Results.GetApproval(ctx, h.cfg.Namespace, rec.RunName)
	if err != nil {
		if apierrors.IsNotFound(err) {
			return fmt.Errorf("%w: no AgenticRunApproval exists for run %s", a2a.ErrInvalidParams, rec.RunName)
		}
		return fmt.Errorf("%w: %v", a2a.ErrInternalError, err)
	}
	for _, stage := range approval.Spec.Stages {
		if stage.Type == decision.Stage {
			if (stage.Decision == agenticv1alpha1.ApprovalDecisionDenied) == decision.Denied {
				return nil // duplicate delivery of the same decision
			}
			return fmt.Errorf("%w: stage %s already has decision %s (immutable)", a2a.ErrInvalidParams, decision.Stage, stage.Decision)
		}
	}
	dec := agenticv1alpha1.ApprovalDecisionApproved
	if decision.Denied {
		dec = agenticv1alpha1.ApprovalDecisionDenied
	}
	approval.Spec.Stages = append(approval.Spec.Stages,
		agenticv1alpha1.NewApprovalStage(decision.Stage, dec, "", decision.Option))

	tokenH, err := h.tokenH(ctx, cred)
	if err != nil {
		return err
	}
	rc, err := h.deps.Submitters(ctx, tokenH)
	if err != nil {
		return fmt.Errorf("%w: %v", a2a.ErrInternalError, err)
	}
	if err := rc.UpdateApproval(ctx, approval); err != nil {
		return fmt.Errorf("%w: %v", a2a.ErrInternalError, err)
	}
	h.deps.Logger.Info("forwarded approval decision",
		"task_id", rec.TaskID, "run", rec.RunName, "stage", decision.Stage,
		"decision", dec, "principal", auth.PrincipalID(cred.Subject, cred.Issuer))
	return nil
}

// ---- a2asrv.RequestHandler -------------------------------------------------

// OnSendMessage handles message/send.
func (h *Handler) OnSendMessage(ctx context.Context, params *a2a.MessageSendParams) (a2a.SendMessageResult, error) {
	return h.handleSend(ctx, params)
}

// OnSendMessageStream handles message/stream: submit, then hold the SSE
// stream open until the run reaches a terminal phase or the wall-clock
// deadline (design §2.3). Updates are PUSHED: the watch controller
// projects CR changes into the task store, and the store's per-task signal
// channel wakes this loop (M3 replaced M2's polling). A renewal pause does
// not count against the deadline — while auth-required the deadline is
// pushed out and the pause's own bound (§6.4) ends the stream instead.
func (h *Handler) OnSendMessageStream(ctx context.Context, params *a2a.MessageSendParams) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		task, err := h.handleSend(ctx, params)
		if err != nil {
			yield(nil, err)
			return
		}
		if !yield(task, nil) {
			return
		}
		if task.Status.State == a2a.TaskStateAuthRequired && !taskHasRun(h.deps.Store, string(task.ID)) {
			// Parked before submit: the renewal follow-up is a NEW
			// message/send on the same task — end this stream.
			yield(h.statusEvent(task, a2a.TaskStateAuthRequired, true), nil)
			return
		}
		if TerminalState(task.Status.State) {
			yield(h.statusEvent(task, task.Status.State, true), nil)
			return
		}
		h.streamUpdates(ctx, string(task.ID), yield)
	}
}

func taskHasRun(s *store.Store, taskID string) bool {
	rec, ok := s.Task(taskID)
	return ok && rec.HasRun
}

// streamUpdates is the shared SSE tail for message/stream and
// tasks/resubscribe: subscribe to the task's store signal, project on
// every wake-up, emit artifact events for new artifacts and status events
// for state changes, end on terminal or deadline.
func (h *Handler) streamUpdates(ctx context.Context, taskID string, yield func(a2a.Event, error) bool) {
	signals, cancel := h.deps.Store.Subscribe(taskID)
	defer cancel()

	state := a2a.TaskStateUnspecified
	emitted := map[a2a.ArtifactID]bool{}
	// Anchored once; not armed at all while the task sits in a renewal
	// pause (§2.3 — the pause has its own §6.4 bound).
	deadline := h.cfg.now().Add(h.cfg.StreamDeadline)
	for {
		rec, ok := h.deps.Store.Task(taskID)
		if !ok {
			yield(nil, a2a.ErrTaskNotFound)
			return
		}
		rec, task, err := h.project(ctx, rec)
		if err != nil {
			yield(nil, err)
			return
		}
		for _, art := range task.Artifacts {
			if !emitted[art.ID] {
				emitted[art.ID] = true
				ev := &a2a.TaskArtifactUpdateEvent{
					TaskID:    task.ID,
					ContextID: task.ContextID,
					Artifact:  art,
					LastChunk: true,
				}
				if !yield(ev, nil) {
					return
				}
			}
		}
		if task.Status.State != state {
			state = task.Status.State
			final := TerminalState(state)
			if !yield(h.statusEvent(task, state, final), nil) {
				return
			}
			if final {
				return
			}
		}
		// A renewal pause does not consume the stream deadline (§2.3):
		// while paused, the stream-deadline timer is not armed at all —
		// the pause's OWN bound (§6.4, enforced by the watcher's
		// sweeper) fails the task and ends this stream through the
		// normal terminal path, and a renewal signal re-arms the
		// deadline. This is the design's "renewal waits have their own
		// bound" made literal.
		var timeout <-chan time.Time
		if !rec.AuthPaused {
			if h.cfg.Now != nil {
				// Frozen test clock: time.Until cannot see it — wait
				// the full window in real time; signals still wake us.
				timeout = time.After(h.cfg.StreamDeadline)
			} else {
				timeout = time.After(time.Until(deadline))
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-signals:
		case <-timeout:
			// The deadline event must say so explicitly — the last
			// projected task's status message may describe an older
			// state.
			now := h.cfg.now()
			yield(&a2a.TaskStatusUpdateEvent{
				TaskID:    task.ID,
				ContextID: task.ContextID,
				Final:     true,
				Status: a2a.TaskStatus{
					State:     a2a.TaskStateFailed,
					Timestamp: &now,
					Message: &a2a.Message{
						Role:  a2a.MessageRoleAgent,
						ID:    string(a2a.NewTaskID()),
						Parts: []a2a.Part{a2a.TextPart{Text: "stream deadline exceeded, poll tasks/get"}},
					},
				},
			}, nil)
			return
		}
	}
}

func (h *Handler) statusEvent(task *a2a.Task, state a2a.TaskState, final bool) a2a.Event {
	now := h.cfg.now()
	return &a2a.TaskStatusUpdateEvent{
		TaskID:    task.ID,
		ContextID: task.ContextID,
		Final:     final,
		Status: a2a.TaskStatus{
			State:     state,
			Timestamp: &now,
			Message: &a2a.Message{
				Role:  a2a.MessageRoleAgent,
				ID:    string(a2a.NewTaskID()),
				Parts: []a2a.Part{a2a.TextPart{Text: streamStatusText(state, task)}},
			},
		},
	}
}

func streamStatusText(state a2a.TaskState, task *a2a.Task) string {
	if task.Status.Message != nil && len(task.Status.Message.Parts) > 0 {
		if tp, ok := task.Status.Message.Parts[0].(a2a.TextPart); ok && tp.Text != "" {
			return tp.Text
		}
	}
	switch state {
	case a2a.TaskStateFailed:
		return "stream deadline exceeded, poll tasks/get"
	case a2a.TaskStateAuthRequired:
		return "credential expired; renew with a follow-up message on this task"
	default:
		return "run progress: " + string(state)
	}
}

// OnGetTask handles tasks/get. Ownership is validated first (design §5.6).
func (h *Handler) OnGetTask(ctx context.Context, params *a2a.TaskQueryParams) (*a2a.Task, error) {
	cred, err := h.credential(ctx)
	if err != nil {
		return nil, err
	}
	principal := auth.PrincipalID(cred.Subject, cred.Issuer)
	rec, err := h.findTask(ctx, string(params.ID), principal)
	if err != nil {
		return nil, err
	}
	if rec.Principal != principal {
		return nil, a2a.ErrTaskNotFound
	}
	return h.taskFromRecord(ctx, rec)
}

// OnCancelTask handles tasks/cancel.
//
// M3 CHOICE (design §10 Q6, still unresolved): the only stop semantic
// visible in the inspected repo C types is EmergencyStopped (with its
// RBAC/audit weight), and the adapter must not write conditions on runs
// (design §6.1). Until Q6 is decided against suspension_cleanup.go /
// pod_handler.go, non-terminal tasks return ErrTaskNotCancelable. Terminal
// tasks (including a run already Stopping, which projects canceled per
// §7.2) return their terminal state (design §2.3), which IS implemented.
func (h *Handler) OnCancelTask(ctx context.Context, params *a2a.TaskIDParams) (*a2a.Task, error) {
	cred, err := h.credential(ctx)
	if err != nil {
		return nil, err
	}
	principal := auth.PrincipalID(cred.Subject, cred.Issuer)
	rec, err := h.findTask(ctx, string(params.ID), principal)
	if err != nil {
		return nil, err
	}
	if rec.Principal != principal {
		return nil, a2a.ErrTaskNotFound
	}
	task, err := h.taskFromRecord(ctx, rec)
	if err != nil {
		return nil, err
	}
	if TerminalState(task.Status.State) {
		return task, nil
	}
	return nil, fmt.Errorf("%w: tasks/cancel is not implemented (AgenticRun stop semantics, design §10 Q6, unresolved)", a2a.ErrTaskNotCancelable)
}

// OnResubscribeToTask handles tasks/resubscribe (M3, with the watch): a
// client that disconnected mid-run reattaches to the task's update stream.
// Ownership is validated first (design §5.6) — a non-owner receives
// TaskNotFound, never the stream.
func (h *Handler) OnResubscribeToTask(ctx context.Context, params *a2a.TaskIDParams) iter.Seq2[a2a.Event, error] {
	return func(yield func(a2a.Event, error) bool) {
		cred, err := h.credential(ctx)
		if err != nil {
			yield(nil, err)
			return
		}
		principal := auth.PrincipalID(cred.Subject, cred.Issuer)
		rec, err := h.findTask(ctx, string(params.ID), principal)
		if err != nil {
			yield(nil, err)
			return
		}
		if rec.Principal != principal {
			yield(nil, a2a.ErrTaskNotFound)
			return
		}
		task, err := h.taskFromRecord(ctx, rec)
		if err != nil {
			yield(nil, err)
			return
		}
		if !yield(task, nil) {
			return
		}
		if TerminalState(task.Status.State) {
			yield(h.statusEvent(task, task.Status.State, true), nil)
			return
		}
		h.streamUpdates(ctx, string(params.ID), yield)
	}
}

// OnListTasks: not supported (not in the design's surface, §2.1).
func (h *Handler) OnListTasks(context.Context, *a2a.ListTasksRequest) (*a2a.ListTasksResponse, error) {
	return nil, a2a.ErrUnsupportedOperation
}

// Push-notification configs are not part of the Phase 1 surface.
func (h *Handler) OnGetTaskPushConfig(context.Context, *a2a.GetTaskPushConfigParams) (*a2a.TaskPushConfig, error) {
	return nil, a2a.ErrPushNotificationNotSupported
}

func (h *Handler) OnListTaskPushConfig(context.Context, *a2a.ListTaskPushConfigParams) ([]*a2a.TaskPushConfig, error) {
	return nil, a2a.ErrPushNotificationNotSupported
}

func (h *Handler) OnSetTaskPushConfig(context.Context, *a2a.TaskPushConfig) (*a2a.TaskPushConfig, error) {
	return nil, a2a.ErrPushNotificationNotSupported
}

func (h *Handler) OnDeleteTaskPushConfig(context.Context, *a2a.DeleteTaskPushConfigParams) error {
	return a2a.ErrPushNotificationNotSupported
}

// OnGetExtendedAgentCard: only the unauthenticated card is served.
func (h *Handler) OnGetExtendedAgentCard(context.Context) (*a2a.AgentCard, error) {
	return nil, a2a.ErrAuthenticatedExtendedCardNotConfigured
}
