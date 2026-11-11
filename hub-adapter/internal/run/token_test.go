package run

import (
	"context"
	"errors"
	"testing"

	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

func newTestRun(uid, target string) *agenticv1alpha1.AgenticRun {
	return &agenticv1alpha1.AgenticRun{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "a2a-test",
			Namespace: "test-ns",
			UID:       types.UID(uid),
		},
		Spec: agenticv1alpha1.AgenticRunSpec{TargetCluster: target},
	}
}

// TestWriteTokenSecretContract asserts the exact repo C contract:
// ls-run-token-{runUID}, key token-a (non-empty), labels
// hub.openshift.io/agentic-run={uid} and hub.openshift.io/spoke-cluster={target}.
func TestWriteTokenSecretContract(t *testing.T) {
	secrets := newFakeSecrets()
	w := &TokenWriter{Secrets: secrets, Namespace: "test-ns", Identity: "adapter-sa"}
	run := newTestRun("uid-1", "spoke-a")

	if err := w.Write(context.Background(), run, "token-a-value"); err != nil {
		t.Fatal(err)
	}

	s := secrets.get("test-ns", RunTokenSecretName("uid-1"))
	if s == nil {
		t.Fatal("secret not created")
	}
	if got := string(s.Data[RunTokenKey]); got != "token-a-value" || got == "" {
		t.Errorf("token-a = %q", got)
	}
	if got := s.Labels[LabelAgenticRun]; got != "uid-1" {
		t.Errorf("label agentic-run = %q", got)
	}
	if got := s.Labels[LabelSpokeCluster]; got != "spoke-a" {
		t.Errorf("label spoke-cluster = %q", got)
	}
	if got := s.Annotations[AnnotationWrittenBy]; got != "adapter-sa" {
		t.Errorf("annotation written-by = %q", got)
	}
}

func TestWriteTokenSecretValidation(t *testing.T) {
	w := &TokenWriter{Secrets: newFakeSecrets(), Namespace: "test-ns", Identity: "adapter-sa"}
	if err := w.Write(context.Background(), newTestRun("", "spoke-a"), "t"); !errors.Is(err, ErrNoRunUID) {
		t.Errorf("want ErrNoRunUID, got %v", err)
	}
	if err := w.Write(context.Background(), newTestRun("uid-1", "spoke-a"), ""); !errors.Is(err, ErrEmptyToken) {
		t.Errorf("want ErrEmptyToken, got %v", err)
	}
}

// TestWriteTokenSecretRenewalPreservesLabels asserts design §5.3: renewing
// OUR OWN Secret rewrites only token-a; labels and the resourceVersion
// change naturally (repo C's resume signal).
func TestWriteTokenSecretRenewalPreservesLabels(t *testing.T) {
	secrets := newFakeSecrets()
	w := &TokenWriter{Secrets: secrets, Namespace: "test-ns", Identity: "adapter-sa"}
	run := newTestRun("uid-2", "spoke-b")

	if err := w.Write(context.Background(), run, "old-token"); err != nil {
		t.Fatal(err)
	}
	before := secrets.get("test-ns", RunTokenSecretName("uid-2"))
	beforeRV := before.ResourceVersion

	if err := w.Write(context.Background(), run, "new-token"); err != nil {
		t.Fatal(err)
	}
	after := secrets.get("test-ns", RunTokenSecretName("uid-2"))

	if got := string(after.Data[RunTokenKey]); got != "new-token" {
		t.Errorf("token-a = %q, want renewed value", got)
	}
	if after.Labels[LabelAgenticRun] != before.Labels[LabelAgenticRun] ||
		after.Labels[LabelSpokeCluster] != before.Labels[LabelSpokeCluster] {
		t.Errorf("renewal must not touch labels: before=%v after=%v", before.Labels, after.Labels)
	}
	if after.ResourceVersion == beforeRV {
		t.Error("resourceVersion must change on renewal (repo C resume signal)")
	}
}

// TestWriteTokenSecretSameValueNoOp: a renewal whose token value is
// byte-identical to the current Secret's is a no-op — repo C's resume
// signal is the resourceVersion, which only an actual VALUE change bumps.
// A caller retrying an unchanged token must not depend on an RV bump that
// never happens.
func TestWriteTokenSecretSameValueNoOp(t *testing.T) {
	secrets := newFakeSecrets()
	w := &TokenWriter{Secrets: secrets, Namespace: "test-ns", Identity: "adapter-sa"}
	run := newTestRun("uid-4", "spoke-d")

	if err := w.Write(context.Background(), run, "token-value"); err != nil {
		t.Fatal(err)
	}
	before := secrets.get("test-ns", RunTokenSecretName("uid-4"))
	beforeRV := before.ResourceVersion

	// Same value again: success, but NO update (RV unchanged).
	if err := w.Write(context.Background(), run, "token-value"); err != nil {
		t.Fatalf("same-value renewal must succeed, got %v", err)
	}
	after := secrets.get("test-ns", RunTokenSecretName("uid-4"))
	if after.ResourceVersion != beforeRV {
		t.Errorf("resourceVersion changed on a same-value renewal (%s -> %s): repo C would resume on a token that did not change",
			beforeRV, after.ResourceVersion)
	}

	// A genuinely different value still rewrites (and bumps the RV).
	if err := w.Write(context.Background(), run, "token-value-2"); err != nil {
		t.Fatal(err)
	}
	after = secrets.get("test-ns", RunTokenSecretName("uid-4"))
	if string(after.Data[RunTokenKey]) != "token-value-2" {
		t.Errorf("token-a = %q, want the new value", after.Data[RunTokenKey])
	}
	if after.ResourceVersion == beforeRV {
		t.Error("resourceVersion must change on a real renewal (repo C resume signal)")
	}
}

// TestWriteTokenSecretForeignTakeover asserts design §5.6: a Secret the
// adapter did not write is overwritten wholesale, never consumed.
func TestWriteTokenSecretForeignTakeover(t *testing.T) {
	secrets := newFakeSecrets()
	w := &TokenWriter{Secrets: secrets, Namespace: "test-ns", Identity: "adapter-sa"}
	run := newTestRun("uid-3", "spoke-c")

	// A foreign secret with wrong labels and no provenance annotation.
	foreign := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      RunTokenSecretName("uid-3"),
			Namespace: "test-ns",
			Labels:    map[string]string{LabelSpokeCluster: "wrong-cluster"},
		},
		Data: map[string][]byte{RunTokenKey: []byte("foreign-token")},
	}
	if err := secrets.CreateSecret(context.Background(), foreign); err != nil {
		t.Fatal(err)
	}

	if err := w.Write(context.Background(), run, "our-token"); err != nil {
		t.Fatal(err)
	}
	s := secrets.get("test-ns", RunTokenSecretName("uid-3"))
	if string(s.Data[RunTokenKey]) != "our-token" {
		t.Errorf("token-a = %q", s.Data[RunTokenKey])
	}
	if s.Labels[LabelAgenticRun] != "uid-3" || s.Labels[LabelSpokeCluster] != "spoke-c" {
		t.Errorf("labels not taken over: %v", s.Labels)
	}
	if s.Annotations[AnnotationWrittenBy] != "adapter-sa" {
		t.Errorf("provenance annotation not stamped: %v", s.Annotations)
	}
}
