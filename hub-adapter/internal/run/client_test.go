package run

import (
	"context"
	"fmt"
	"sync"

	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
)

// fakeSecrets is an in-memory SecretClient mimicking the API server's
// create/get/update semantics (incl. AlreadyExists and resourceVersion
// bumps).
type fakeSecrets struct {
	mu      sync.Mutex
	secrets map[string]*corev1.Secret
	rv      int
}

func newFakeSecrets() *fakeSecrets {
	return &fakeSecrets{secrets: map[string]*corev1.Secret{}}
}

// DeleteSecret satisfies the SecretClient interface (M3 §5.5 cleanup);
// the token-writer tests exercise deletion through their own assertions.
func (f *fakeSecrets) DeleteSecret(_ context.Context, ns, name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	k := key(ns, name)
	if _, exists := f.secrets[k]; !exists {
		return apierrors.NewNotFound(schema.GroupResource{Group: "", Resource: "secrets"}, name)
	}
	delete(f.secrets, k)
	return nil
}

func key(ns, name string) string { return ns + "/" + name }

func (f *fakeSecrets) CreateSecret(_ context.Context, s *corev1.Secret) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	k := key(s.Namespace, s.Name)
	if _, exists := f.secrets[k]; exists {
		return apierrors.NewAlreadyExists(schema.GroupResource{Group: "", Resource: "secrets"}, s.Name)
	}
	f.rv++
	stored := s.DeepCopy()
	stored.ResourceVersion = fmt.Sprintf("%d", f.rv)
	f.secrets[k] = stored
	*s = *stored
	return nil
}

func (f *fakeSecrets) GetSecret(_ context.Context, ns, name string) (*corev1.Secret, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	s, ok := f.secrets[key(ns, name)]
	if !ok {
		return nil, apierrors.NewNotFound(schema.GroupResource{Group: "", Resource: "secrets"}, name)
	}
	return s.DeepCopy(), nil
}

func (f *fakeSecrets) UpdateSecret(_ context.Context, s *corev1.Secret) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	k := key(s.Namespace, s.Name)
	if _, ok := f.secrets[k]; !ok {
		return apierrors.NewNotFound(schema.GroupResource{Group: "", Resource: "secrets"}, s.Name)
	}
	f.rv++
	stored := s.DeepCopy()
	stored.ResourceVersion = fmt.Sprintf("%d", f.rv)
	f.secrets[k] = stored
	*s = *stored
	return nil
}

func (f *fakeSecrets) get(ns, name string) *corev1.Secret {
	s, _ := f.GetSecret(context.Background(), ns, name)
	return s
}

// fakeRuns is an in-memory RunClient mimicking create/get/update/list and
// assigning server-side UIDs.
type fakeRuns struct {
	mu   sync.Mutex
	runs map[string]*agenticv1alpha1.AgenticRun
	uid  int
	// createCalls counts CreateRun invocations (duplicate-detection tests).
	createCalls int
}

func newFakeRuns() *fakeRuns {
	return &fakeRuns{runs: map[string]*agenticv1alpha1.AgenticRun{}}
}

func (f *fakeRuns) CreateRun(_ context.Context, run *agenticv1alpha1.AgenticRun) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.createCalls++
	k := key(run.Namespace, run.Name)
	if _, exists := f.runs[k]; exists {
		return apierrors.NewAlreadyExists(schema.GroupResource{Group: "agentic.openshift.io", Resource: "agenticruns"}, run.Name)
	}
	f.uid++
	stored := run.DeepCopy()
	stored.UID = types.UID(fmt.Sprintf("run-uid-%d", f.uid))
	stored.ResourceVersion = "1"
	f.runs[k] = stored
	*run = *stored
	return nil
}

func (f *fakeRuns) GetRun(_ context.Context, ns, name string) (*agenticv1alpha1.AgenticRun, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	run, ok := f.runs[key(ns, name)]
	if !ok {
		return nil, apierrors.NewNotFound(schema.GroupResource{Group: "agentic.openshift.io", Resource: "agenticruns"}, name)
	}
	return run.DeepCopy(), nil
}

func (f *fakeRuns) UpdateRun(_ context.Context, run *agenticv1alpha1.AgenticRun) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	k := key(run.Namespace, run.Name)
	if _, ok := f.runs[k]; !ok {
		return apierrors.NewNotFound(schema.GroupResource{Group: "agentic.openshift.io", Resource: "agenticruns"}, run.Name)
	}
	f.runs[k] = run.DeepCopy()
	return nil
}

func (f *fakeRuns) ListRuns(_ context.Context, ns string) (*agenticv1alpha1.AgenticRunList, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	list := &agenticv1alpha1.AgenticRunList{}
	for _, run := range f.runs {
		if run.Namespace == ns {
			list.Items = append(list.Items, *run.DeepCopy())
		}
	}
	return list, nil
}

// setConditions mutates a stored run's status (test driver).
func (f *fakeRuns) setConditions(ns, name string, conditions ...metav1.Condition) {
	f.mu.Lock()
	defer f.mu.Unlock()
	run := f.runs[key(ns, name)]
	for i := range conditions {
		meta.SetStatusCondition(&run.Status.Conditions, conditions[i])
	}
}

// UpdateApproval satisfies the RunClient interface (M3); the run-package
// tests never exercise approval decisions.
func (f *fakeRuns) UpdateApproval(_ context.Context, _ *agenticv1alpha1.AgenticRunApproval) error {
	return nil
}
