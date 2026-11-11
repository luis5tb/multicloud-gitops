package store

import (
	"regexp"
	"strings"
	"testing"
	"time"
)

func TestRunNameDeterministic(t *testing.T) {
	a := RunName("ctx-1", "task-1")
	b := RunName("ctx-1", "task-1")
	if a != b {
		t.Errorf("same contextId+taskId must derive the same name: %q vs %q", a, b)
	}
	for _, other := range [][2]string{
		{"ctx-2", "task-1"}, // different context
		{"ctx-1", "task-2"}, // different task
	} {
		if c := RunName(other[0], other[1]); c == a {
			t.Errorf("RunName(%q,%q) collides with RunName(ctx-1,task-1)", other[0], other[1])
		}
	}
}

func TestRunNameDNSSafe(t *testing.T) {
	dnsSubdomain := regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`)
	// Hostile caller-supplied IDs must still produce a safe name.
	for _, id := range []string{"task/with/slashes", "UPPER", "task with spaces", strings.Repeat("x", 500), "", "emoji-🚀"} {
		name := RunName("ctx", id)
		if !strings.HasPrefix(name, RunNamePrefix) {
			t.Errorf("name %q lacks prefix", name)
		}
		if !dnsSubdomain.MatchString(name) {
			t.Errorf("name %q is not a DNS subdomain", name)
		}
		if len(name) > 253 {
			t.Errorf("name %q too long", name)
		}
	}
}

func TestTaskForMessageDuplicate(t *testing.T) {
	s := New()
	rec := &TaskRecord{
		TaskID: "t1", ContextID: "c1", Principal: "p", LastMessageID: "m1",
		CreatedAt: time.Now(),
	}
	s.PutTask(rec)

	got, ok := s.TaskForMessage("m1")
	if !ok || got.TaskID != "t1" {
		t.Fatalf("TaskForMessage(m1) = %v, %v", got, ok)
	}
	if _, ok := s.TaskForMessage("m2"); ok {
		t.Error("unknown messageId must not resolve")
	}

	// A second message on the same task registers separately.
	if !s.PutMessage("m2", "t1") {
		t.Error("PutMessage(m2) must succeed")
	}
	if s.PutMessage("m2", "t1") {
		t.Error("duplicate PutMessage(m2) must fail (duplicate delivery)")
	}
	if got, _ := s.TaskForMessage("m2"); got.TaskID != "t1" {
		t.Errorf("TaskForMessage(m2) = %v", got)
	}
}

func TestPutTaskIndexesContext(t *testing.T) {
	s := New()
	s.PutTask(&TaskRecord{TaskID: "t1", ContextID: "c1", Principal: "p"})
	s.PutTask(&TaskRecord{TaskID: "t2", ContextID: "c1", Principal: "p"})
	s.PutTask(&TaskRecord{TaskID: "t3", ContextID: "c2", Principal: "p"})

	ctx, ok := s.Context("c1")
	if !ok || len(ctx.TaskIDs) != 2 {
		t.Fatalf("context c1 = %+v", ctx)
	}
	ctx2, _ := s.Context("c2")
	if len(ctx2.TaskIDs) != 1 || ctx2.TaskIDs[0] != "t3" {
		t.Errorf("context c2 = %+v", ctx2)
	}
}

func TestTaskCloneIsolation(t *testing.T) {
	s := New()
	s.PutTask(&TaskRecord{TaskID: "t1", ContextID: "c1", LastMessageID: "m1"})
	got, _ := s.Task("t1")
	got.LastMessageID = "mutated"
	again, _ := s.Task("t1")
	if again.LastMessageID != "m1" {
		t.Error("returned records must be copies")
	}
}
