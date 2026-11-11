// Package run maps A2A messages onto AgenticRun CRs (design §4) and owns
// the run-token Secret lifecycle on the write side (design §5).
package run

import (
	"context"

	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

// RunClient is the adapter's narrow interface over the AgenticRun and
// AgenticRunApproval APIs. Read operations (Get/List) are exercised with
// the ADAPTER'S service account; Create/Update (including approval
// decisions, which repo C's mutating admission webhook attributes to the
// request user) are exercised AS THE CALLER (Token H) so runs and approval
// decisions are attributable to the requester, never to the adapter
// (design §3.3, §7.1). RBAC notes for the chart (M4) are in the
// hub-adapter README.
type RunClient interface {
	CreateRun(ctx context.Context, run *agenticv1alpha1.AgenticRun) error
	GetRun(ctx context.Context, namespace, name string) (*agenticv1alpha1.AgenticRun, error)
	UpdateRun(ctx context.Context, run *agenticv1alpha1.AgenticRun) error
	ListRuns(ctx context.Context, namespace string) (*agenticv1alpha1.AgenticRunList, error)
	// UpdateApproval appends a decision stage to an AgenticRunApproval —
	// ALWAYS as the caller (Token H): repo C's mutating admission webhook
	// stamps spec.approver from the AdmissionReview userInfo, so patching
	// as the adapter SA would falsify attribution.
	UpdateApproval(ctx context.Context, approval *agenticv1alpha1.AgenticRunApproval) error
}

// ResultClient reads the Result CRs referenced from AgenticRun status and
// the per-run AgenticRunApproval (design §7.1 watching; §7.2 artifacts),
// always as the adapter SA.
type ResultClient interface {
	GetAnalysisResult(ctx context.Context, namespace, name string) (*agenticv1alpha1.AnalysisResult, error)
	GetExecutionResult(ctx context.Context, namespace, name string) (*agenticv1alpha1.ExecutionResult, error)
	GetVerificationResult(ctx context.Context, namespace, name string) (*agenticv1alpha1.VerificationResult, error)
	GetEscalationResult(ctx context.Context, namespace, name string) (*agenticv1alpha1.EscalationResult, error)
	// GetApproval fetches the AgenticRunApproval for a run (1:1, same name
	// and namespace per repo C's agenticrunapproval_types.go).
	GetApproval(ctx context.Context, namespace, name string) (*agenticv1alpha1.AgenticRunApproval, error)
}

// SecretClient is the adapter's narrow interface over the Secret API,
// always exercised with the adapter's own service account (design §5.2:
// the SA holds the Role on run-token Secrets in the run namespace). Delete
// is the §5.5 terminal-run cleanup grant (see the README RBAC table).
type SecretClient interface {
	CreateSecret(ctx context.Context, secret *corev1.Secret) error
	GetSecret(ctx context.Context, namespace, name string) (*corev1.Secret, error)
	UpdateSecret(ctx context.Context, secret *corev1.Secret) error
	DeleteSecret(ctx context.Context, namespace, name string) error
}

// RunClientFactory builds a per-request RunClient authenticated as the
// caller via Token H (never the adapter SA). The returned client is used
// for CreateRun/UpdateRun/UpdateApproval on that request only.
type RunClientFactory func(ctx context.Context, tokenH string) (RunClient, error)
