// Package server implements the A2A protocol surface (design §2) on top of
// the a2a-go SDK's JSON-RPC/SSE transport, mapping A2A semantics onto the
// AgenticRun machinery via the run and store packages.
package server

import (
	"fmt"

	"github.com/a2aproject/a2a-go/a2a"
	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// TaskStateFor maps an AgenticRun's derived phase onto an A2A task state
// (design §7.2 mapping table, full M3 version).
//
// CredentialsExpired does NOT participate in phase derivation (repo C
// contract) — the auth-required overlay lives in the task record
// (store.TaskRecord.AuthPaused) and is applied by projectTask, not here.
func TaskStateFor(run *agenticv1alpha1.AgenticRun) a2a.TaskState {
	if run == nil {
		return a2a.TaskStateUnknown
	}
	// Stopping records accepted stop intent (repo C: does not participate
	// in DerivePhase). Design §7.2: "tasks/cancel during Stopping →
	// canceled" — surfacing the interim state as canceled is the honest
	// projection of a run that is going down.
	if cond := findCondition(run, agenticv1alpha1.AgenticRunConditionStopping); cond != nil && cond.Status == metav1.ConditionTrue {
		return a2a.TaskStateCanceled
	}
	switch agenticv1alpha1.DerivePhase(run.Status.Conditions) {
	case agenticv1alpha1.AgenticRunPhasePending:
		return a2a.TaskStateSubmitted
	case agenticv1alpha1.AgenticRunPhaseAnalyzing,
		agenticv1alpha1.AgenticRunPhaseExecuting,
		agenticv1alpha1.AgenticRunPhaseVerifying,
		agenticv1alpha1.AgenticRunPhaseEscalating:
		return a2a.TaskStateWorking
	case agenticv1alpha1.AgenticRunPhaseProposed:
		// Analysis done, awaiting approval → input-required (§7.2). The
		// approval-decision DataPart path (approve/reject on the same
		// task, new messageId) answers it — see approvalDecisionOf.
		return a2a.TaskStateInputRequired
	case agenticv1alpha1.AgenticRunPhaseCompleted:
		return a2a.TaskStateCompleted
	case agenticv1alpha1.AgenticRunPhaseFailed, agenticv1alpha1.AgenticRunPhaseDenied:
		return a2a.TaskStateFailed
	case agenticv1alpha1.AgenticRunPhaseEscalated:
		// Escalation completed: terminal. A2A has no "escalated" state;
		// failed carries the escalation reason in the status message.
		return a2a.TaskStateFailed
	case agenticv1alpha1.AgenticRunPhaseEmergencyStopped:
		return a2a.TaskStateCanceled
	default:
		return a2a.TaskStateUnknown
	}
}

// RunTerminalPhase reports whether the AgenticRun's derived phase is
// terminal (design §5.5: Completed/Failed/Denied/Escalated/EmergencyStopped
// — the phases after which no step can retry).
func RunTerminalPhase(run *agenticv1alpha1.AgenticRun) bool {
	switch agenticv1alpha1.DerivePhase(run.Status.Conditions) {
	case agenticv1alpha1.AgenticRunPhaseCompleted,
		agenticv1alpha1.AgenticRunPhaseFailed,
		agenticv1alpha1.AgenticRunPhaseDenied,
		agenticv1alpha1.AgenticRunPhaseEscalated,
		agenticv1alpha1.AgenticRunPhaseEmergencyStopped:
		return true
	default:
		return false
	}
}

// CredentialsExpired reports whether the run carries repo C's pause
// condition (CredentialsExpired=True; the reason is always
// ReasonCredentialsExpired when the controller wrote it). Design §6.1
// step 1: this is the hub-side renewal-bridge signal.
func CredentialsExpired(run *agenticv1alpha1.AgenticRun) bool {
	cond := findCondition(run, agenticv1alpha1.AgenticRunConditionCredentialsExpired)
	return cond != nil && cond.Status == metav1.ConditionTrue
}

func findCondition(run *agenticv1alpha1.AgenticRun, condType string) *metav1.Condition {
	for i := range run.Status.Conditions {
		if run.Status.Conditions[i].Type == condType {
			return &run.Status.Conditions[i]
		}
	}
	return nil
}

// StatusMessageFor renders the human-readable status message attached to a
// projected task state: terminal and failure states carry the run's own
// condition reason/message (design §7.2: "with step failure
// reason/message", "with denial reason", "with their reasons"); an
// auth-required pause describes the renewal follow-up (§6.1 step 2).
func StatusMessageFor(run *agenticv1alpha1.AgenticRun, state a2a.TaskState) string {
	switch state {
	case a2a.TaskStateFailed, a2a.TaskStateCanceled, a2a.TaskStateCompleted:
		if run != nil {
			for _, condType := range []string{
				agenticv1alpha1.AgenticRunConditionAnalyzed,
				agenticv1alpha1.AgenticRunConditionExecuted,
				agenticv1alpha1.AgenticRunConditionVerified,
				agenticv1alpha1.AgenticRunConditionDenied,
				agenticv1alpha1.AgenticRunConditionEscalated,
				agenticv1alpha1.AgenticRunConditionEmergencyStopped,
			} {
				if cond := findCondition(run, condType); cond != nil && cond.Message != "" {
					return fmt.Sprintf("%s: %s", cond.Reason, cond.Message)
				}
			}
		}
		return string(state)
	case a2a.TaskStateAuthRequired:
		return "run credential expired; renew with a follow-up message on this task (same context, new messageId, fresh token)"
	case a2a.TaskStateInputRequired:
		// Supplemented by the approval detail in projectTask.
		return "approval requested; reply on this task with a decision DataPart"
	default:
		return ""
	}
}

// TerminalState reports whether an A2A task state admits no further
// transitions. Used for cancel semantics (§2.3: a terminal run cannot be
// cancelled; return the terminal state instead) and stream deadlines.
func TerminalState(state a2a.TaskState) bool {
	switch state {
	case a2a.TaskStateCompleted, a2a.TaskStateFailed, a2a.TaskStateCanceled,
		a2a.TaskStateRejected, a2a.TaskStateUnknown:
		return true
	default:
		return false
	}
}
