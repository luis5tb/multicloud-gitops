package run

import (
	"context"
	"fmt"

	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/rest"
	ctrlclient "sigs.k8s.io/controller-runtime/pkg/client"
)

// ctrlClient adapts controller-runtime's client.Client to the adapter's
// narrow RunClient/SecretClient interfaces. Kept as a separate type from
// the interfaces so tests can substitute hand-rolled fakes and so the M3
// watch/bridge can slot a caching/informer-backed client in behind the
// same seam.
type ctrlClient struct {
	client ctrlclient.Client
}

var (
	_ RunClient    = (*ctrlClient)(nil)
	_ SecretClient = (*ctrlClient)(nil)
)

// NewCtrlClient returns a RunClient+SecretClient backed by
// controller-runtime, authenticated with the given rest config (normally
// the adapter's in-cluster SA config).
func NewCtrlClient(cfg *rest.Config, scheme *runtime.Scheme) (*ctrlClient, error) {
	c, err := ctrlclient.New(cfg, ctrlclient.Options{Scheme: scheme})
	if err != nil {
		return nil, fmt.Errorf("build controller-runtime client: %w", err)
	}
	return &ctrlClient{client: c}, nil
}

func (c *ctrlClient) CreateRun(ctx context.Context, run *agenticv1alpha1.AgenticRun) error {
	return c.client.Create(ctx, run)
}

func (c *ctrlClient) GetRun(ctx context.Context, namespace, name string) (*agenticv1alpha1.AgenticRun, error) {
	run := &agenticv1alpha1.AgenticRun{}
	if err := c.client.Get(ctx, ctrlclient.ObjectKey{Namespace: namespace, Name: name}, run); err != nil {
		return nil, err
	}
	return run, nil
}

func (c *ctrlClient) UpdateRun(ctx context.Context, run *agenticv1alpha1.AgenticRun) error {
	return c.client.Update(ctx, run)
}

func (c *ctrlClient) ListRuns(ctx context.Context, namespace string) (*agenticv1alpha1.AgenticRunList, error) {
	list := &agenticv1alpha1.AgenticRunList{}
	if err := c.client.List(ctx, list, ctrlclient.InNamespace(namespace)); err != nil {
		return nil, err
	}
	return list, nil
}

func (c *ctrlClient) CreateSecret(ctx context.Context, secret *corev1.Secret) error {
	return c.client.Create(ctx, secret)
}

func (c *ctrlClient) GetSecret(ctx context.Context, namespace, name string) (*corev1.Secret, error) {
	secret := &corev1.Secret{}
	if err := c.client.Get(ctx, ctrlclient.ObjectKey{Namespace: namespace, Name: name}, secret); err != nil {
		return nil, err
	}
	return secret, nil
}

func (c *ctrlClient) UpdateSecret(ctx context.Context, secret *corev1.Secret) error {
	return c.client.Update(ctx, secret)
}

func (c *ctrlClient) DeleteSecret(ctx context.Context, namespace, name string) error {
	return c.client.Delete(ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name}})
}

func (c *ctrlClient) UpdateApproval(ctx context.Context, approval *agenticv1alpha1.AgenticRunApproval) error {
	return c.client.Update(ctx, approval)
}

func (c *ctrlClient) GetAnalysisResult(ctx context.Context, namespace, name string) (*agenticv1alpha1.AnalysisResult, error) {
	obj := &agenticv1alpha1.AnalysisResult{}
	if err := c.client.Get(ctx, ctrlclient.ObjectKey{Namespace: namespace, Name: name}, obj); err != nil {
		return nil, err
	}
	return obj, nil
}

func (c *ctrlClient) GetExecutionResult(ctx context.Context, namespace, name string) (*agenticv1alpha1.ExecutionResult, error) {
	obj := &agenticv1alpha1.ExecutionResult{}
	if err := c.client.Get(ctx, ctrlclient.ObjectKey{Namespace: namespace, Name: name}, obj); err != nil {
		return nil, err
	}
	return obj, nil
}

func (c *ctrlClient) GetVerificationResult(ctx context.Context, namespace, name string) (*agenticv1alpha1.VerificationResult, error) {
	obj := &agenticv1alpha1.VerificationResult{}
	if err := c.client.Get(ctx, ctrlclient.ObjectKey{Namespace: namespace, Name: name}, obj); err != nil {
		return nil, err
	}
	return obj, nil
}

func (c *ctrlClient) GetEscalationResult(ctx context.Context, namespace, name string) (*agenticv1alpha1.EscalationResult, error) {
	obj := &agenticv1alpha1.EscalationResult{}
	if err := c.client.Get(ctx, ctrlclient.ObjectKey{Namespace: namespace, Name: name}, obj); err != nil {
		return nil, err
	}
	return obj, nil
}

func (c *ctrlClient) GetApproval(ctx context.Context, namespace, name string) (*agenticv1alpha1.AgenticRunApproval, error) {
	obj := &agenticv1alpha1.AgenticRunApproval{}
	if err := c.client.Get(ctx, ctrlclient.ObjectKey{Namespace: namespace, Name: name}, obj); err != nil {
		return nil, err
	}
	return obj, nil
}

// NewCtrlRunClientFactory returns a RunClientFactory that builds a
// per-request controller-runtime client AUTHENTICATED AS THE CALLER via
// Token H (design §3.2/§3.3: runs are created under the requester's
// identity, never the adapter SA; Kubernetes authenticates H and RBAC
// authorizes — the hub Authentication/RoleBinding wiring is keycloak-oidc
// chart work, not adapter work).
//
// The factory clears the base config's token file and bearer token so H is
// what is actually presented (an in-cluster config carries the SA token
// file, which would otherwise win).
func NewCtrlRunClientFactory(base *rest.Config, scheme *runtime.Scheme) RunClientFactory {
	return func(ctx context.Context, tokenH string) (RunClient, error) {
		cfg := rest.CopyConfig(base)
		cfg.BearerToken = tokenH
		cfg.BearerTokenFile = ""
		c, err := ctrlclient.New(cfg, ctrlclient.Options{Scheme: scheme})
		if err != nil {
			return nil, fmt.Errorf("build caller-authenticated client: %w", err)
		}
		return &ctrlClient{client: c}, nil
	}
}
