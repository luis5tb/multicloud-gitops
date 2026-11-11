package run

import (
	"context"
	"errors"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/store"
)

func testCreator() *Creator {
	return &Creator{Namespace: "test-ns", AgentName: "default", WrittenBy: "adapter-sa"}
}

func TestNewRunMapping(t *testing.T) {
	c := testCreator()
	run := c.NewRun(NewRunInput{
		ContextID: "ctx-1", TaskID: "task-1", Principal: "p@hash",
		Target: "spoke-a", Request: "investigate the crashlooping pod", MessageID: "m1",
	})

	if run.Name != store.RunName("ctx-1", "task-1") {
		t.Errorf("name = %q", run.Name)
	}
	if run.Namespace != "test-ns" {
		t.Errorf("namespace = %q", run.Namespace)
	}
	if run.Spec.Request != "investigate the crashlooping pod" {
		t.Errorf("request = %q", run.Spec.Request)
	}
	if run.Spec.TargetCluster != "spoke-a" {
		t.Errorf("target = %q", run.Spec.TargetCluster)
	}
	// Analysis-only (design §4.1) and the agent is set explicitly: the
	// CRD's MinProperties=1 rejects a literal "analysis: {}".
	if run.Spec.Analysis.Agent != "default" {
		t.Errorf("analysis agent = %q", run.Spec.Analysis.Agent)
	}
	if !run.Spec.Execution.IsZero() || !run.Spec.Verification.IsZero() {
		t.Error("run must be analysis-only")
	}
	wantAnnotations := map[string]string{
		AnnotationTaskID:     "task-1",
		AnnotationContextID:  "ctx-1",
		AnnotationPrincipal:  "p@hash",
		AnnotationTarget:     "spoke-a",
		AnnotationMessageID:  "m1",
		AnnotationWrittenRun: "adapter-sa",
	}
	for k, v := range wantAnnotations {
		if run.Annotations[k] != v {
			t.Errorf("annotation %s = %q, want %q", k, run.Annotations[k], v)
		}
	}
}

func TestSubmitCreateOrGet(t *testing.T) {
	c := testCreator()
	runs := newFakeRuns()

	first := c.NewRun(NewRunInput{ContextID: "c", TaskID: "t", Principal: "p", Request: "q1"})
	submitted, err := c.Submit(context.Background(), runs, first)
	if err != nil {
		t.Fatal(err)
	}
	if submitted.UID == "" {
		t.Error("submit must return the server-assigned UID")
	}

	// Uncertain-start retry: identical spec, AlreadyExists → same run.
	retry := c.NewRun(NewRunInput{ContextID: "c", TaskID: "t", Principal: "p", Request: "q1"})
	again, err := c.Submit(context.Background(), runs, retry)
	if err != nil {
		t.Fatal(err)
	}
	if again.UID != submitted.UID {
		t.Errorf("retry returned different run: %s vs %s", again.UID, submitted.UID)
	}
	if runs.createCalls != 2 {
		t.Errorf("createCalls = %d (retry must attempt the create)", runs.createCalls)
	}

	// Collision: same deterministic name, different spec → fail closed.
	conflicting := c.NewRun(NewRunInput{ContextID: "c", TaskID: "t", Principal: "p", Request: "DIFFERENT"})
	if _, err := c.Submit(context.Background(), runs, conflicting); !errors.Is(err, ErrRunCollision) {
		t.Errorf("want ErrRunCollision, got %v", err)
	}
}

func TestSubmitRevision(t *testing.T) {
	c := testCreator()
	runs := newFakeRuns()
	submitted, err := c.Submit(context.Background(), runs,
		c.NewRun(NewRunInput{ContextID: "c", TaskID: "t", Principal: "p", Request: "q"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.SubmitRevision(context.Background(), runs, submitted.Name, "focus on the networking angle"); err != nil {
		t.Fatal(err)
	}
	got, _ := runs.GetRun(context.Background(), "test-ns", submitted.Name)
	if got.Spec.RevisionFeedback != "focus on the networking angle" {
		t.Errorf("revisionFeedback = %q", got.Spec.RevisionFeedback)
	}
	if got.Spec.Request != "q" {
		t.Errorf("request must stay immutable: %q", got.Spec.Request)
	}
}

func TestRecordFromRunRoundTrip(t *testing.T) {
	// creator + handler recovery both rely on annotations round-tripping.
	c := testCreator()
	runs := newFakeRuns()
	submitted, err := c.Submit(context.Background(), runs,
		c.NewRun(NewRunInput{ContextID: "ctx-9", TaskID: "task-9", Principal: "p@h", Target: "spoke-z", Request: "q", MessageID: "m9"}))
	if err != nil {
		t.Fatal(err)
	}
	got, _ := runs.GetRun(context.Background(), "test-ns", submitted.Name)
	if got.Annotations[AnnotationTaskID] != "task-9" ||
		got.Annotations[AnnotationContextID] != "ctx-9" ||
		got.Annotations[AnnotationMessageID] != "m9" {
		t.Errorf("annotations = %v", got.Annotations)
	}
	_ = metav1.ObjectMeta{}
}
