package run

import (
	"context"
	"errors"
	"fmt"

	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/store"
)

// Annotations the adapter stamps on every AgenticRun it creates. They are
// the restart-recovery record (design §2.3): the store is reconstructible
// from the CR list plus these annotations. None of them ever carries a
// token value.
const (
	AnnotationTaskID     = "a2a.lightspeed.io/task-id"
	AnnotationContextID  = "a2a.lightspeed.io/context-id"
	AnnotationPrincipal  = "a2a.lightspeed.io/principal" // auth.PrincipalID(sub, iss); never the token
	AnnotationMessageID  = "a2a.lightspeed.io/message-id"
	AnnotationTarget     = "a2a.lightspeed.io/target-cluster"
	AnnotationWrittenRun = "a2a.lightspeed.io/created-by" // adapter identity, provenance
	// AnnotationCredentialPauses persists the §6.4 pause counter on the
	// run CR so a restart (or crash-looping adapter) cannot reset the
	// max-credential-pauses cap. Written by the watch controller whenever
	// the count changes; read back on rehydration (server.recordFromRun).
	// Part of the README's restart-recovery contract.
	AnnotationCredentialPauses = "a2a.lightspeed.io/credential-pauses"
)

// ErrRunCollision means a deterministic run name already exists with a
// DIFFERENT spec — a taskId collision. Fails closed (design §2.2): never a
// silent second run.
var ErrRunCollision = errors.New("agenticrun name collision: existing run with different spec")

// Creator maps an A2A submission onto an AgenticRun (design §4.1).
//
// Mapping decisions (design §4.1, restated):
//   - analysis-only: execution/verification steps are omitted — the A2A
//     caller asked a question; a follow-up that needs execution is a new
//     task/run. The CRD makes step fields immutable anyway.
//   - request = the A2A message's text parts, joined verbatim.
//   - revisionFeedback (the CRD's built-in same-run refinement path) is the
//     adapter's "revise" follow-up DataPart.
//   - LLM credentials, agents (beyond the analysis agent name), approval
//     policies: NOT the adapter's spec to fill; they come from the
//     cluster-scoped Agent/LLMProvider/ApprovalPolicy CRs.
type Creator struct {
	// Namespace is where AgenticRuns are created (repo C operator
	// namespace; design §8.3 / Q4).
	Namespace string
	// AgentName is spec.analysis.agent. The CRD requires analysis
	// (MinProperties=1 on the step), so the design doc's literal
	// "analysis: {}" would be rejected by the API server — the agent name
	// is set explicitly, defaulting to the CRD's documented default agent
	// "default" (agenticrun_types.go: AgenticRunStep.Agent).
	AgentName string
	// WrittenBy identifies this adapter in the created-by annotation.
	WrittenBy string
}

// NewRunInput is everything a new run needs.
type NewRunInput struct {
	ContextID string
	TaskID    string
	Principal string // auth.PrincipalID(sub, iss)
	Target    string // resolved target; "" = hub-local run
	Request   string // joined text parts, verbatim
	MessageID string // first messageId, for recovery
}

// NewRun builds the AgenticRun CR (not submitted). The name is the
// deterministic idempotency key.
func (c *Creator) NewRun(in NewRunInput) *agenticv1alpha1.AgenticRun {
	return &agenticv1alpha1.AgenticRun{
		ObjectMeta: metav1.ObjectMeta{
			Name:      store.RunName(in.ContextID, in.TaskID),
			Namespace: c.Namespace,
			Annotations: map[string]string{
				AnnotationTaskID:     in.TaskID,
				AnnotationContextID:  in.ContextID,
				AnnotationPrincipal:  in.Principal,
				AnnotationTarget:     in.Target,
				AnnotationMessageID:  in.MessageID,
				AnnotationWrittenRun: c.WrittenBy,
			},
		},
		Spec: agenticv1alpha1.AgenticRunSpec{
			Request:       in.Request,
			TargetCluster: in.Target,
			Analysis:      agenticv1alpha1.AgenticRunStep{Agent: c.AgentName},
		},
	}
}

// Submit performs create-or-get (design §2.2's REJECT_DUPLICATE
// substitute):
//   - fresh create succeeds → the run is returned with its server-assigned
//     UID;
//   - AlreadyExists with an identical spec → an "uncertain start" retry;
//     the existing run is returned (no second run);
//   - AlreadyExists with a different spec → ErrRunCollision (taskId
//     collision; the API server's CEL immutability is the backstop).
func (c *Creator) Submit(ctx context.Context, rc RunClient, run *agenticv1alpha1.AgenticRun) (*agenticv1alpha1.AgenticRun, error) {
	if err := rc.CreateRun(ctx, run); err == nil {
		return run, nil
	} else if !apierrors.IsAlreadyExists(err) {
		return nil, fmt.Errorf("create agenticrun %s/%s: %w", run.Namespace, run.Name, err)
	}

	existing, err := rc.GetRun(ctx, c.Namespace, run.Name)
	if err != nil {
		return nil, fmt.Errorf("get existing agenticrun %s: %w", run.Name, err)
	}
	if !sameSpec(existing, run) {
		return nil, fmt.Errorf("%w: %s (task context mismatch)", ErrRunCollision, run.Name)
	}
	return existing, nil
}

func sameSpec(a, b *agenticv1alpha1.AgenticRun) bool {
	return a.Spec.Request == b.Spec.Request &&
		a.Spec.TargetCluster == b.Spec.TargetCluster &&
		a.Spec.Analysis.Agent == b.Spec.Analysis.Agent
}

// SubmitRevision patches spec.revisionFeedback on an existing run — the
// CRD's built-in "same run, refined question" path (design §4.1): patching
// bumps generation and triggers re-analysis with the feedback appended.
func (c *Creator) SubmitRevision(ctx context.Context, rc RunClient, runName, feedback string) error {
	if feedback == "" {
		return errors.New("revision feedback is empty")
	}
	run, err := rc.GetRun(ctx, c.Namespace, runName)
	if err != nil {
		return fmt.Errorf("get agenticrun %s for revision: %w", runName, err)
	}
	run.Spec.RevisionFeedback = feedback
	if err := rc.UpdateRun(ctx, run); err != nil {
		return fmt.Errorf("update agenticrun %s revisionFeedback: %w", runName, err)
	}
	return nil
}
