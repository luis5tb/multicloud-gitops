package server

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/a2aproject/a2a-go/a2a"
	"github.com/a2aproject/a2a-go/a2asrv"
	agenticv1alpha1 "github.com/openshift/lightspeed-agentic-operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrlclient "sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/auth"
	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/run"
	"github.com/luis5tb/multicloud-gitops/hub-adapter/internal/store"
)

// ---- test doubles ----------------------------------------------------------

// fakeKube is a combined in-memory RunClient + SecretClient + ResultClient
// with API-server semantics (AlreadyExists, NotFound, server-assigned UIDs,
// RV bumps). Its `notify` hook simulates the informer: every mutation that
// the production watch would observe calls back into the watcher, so tests
// exercise the real watch path (projection → store → subscriber signal).
type fakeKube struct {
	mu       sync.Mutex
	runs     map[string]*agenticv1alpha1.AgenticRun
	secrets  map[string]*corev1.Secret
	objects  map[string]ctrlclient.Object // Result CRs + approvals, keyed ns/name
	uid, rv  int
	created  int // AgenticRun create calls
	secWritt int // Secret create+update calls
	secDel   int // Secret delete calls (§5.5 cleanup)
	// notify, when set, is invoked AFTER each observable mutation
	// (outside the mutex) — the fake watch delivery.
	notify func()
}

func newFakeKube() *fakeKube {
	return &fakeKube{
		runs:    map[string]*agenticv1alpha1.AgenticRun{},
		secrets: map[string]*corev1.Secret{},
		objects: map[string]ctrlclient.Object{},
	}
}

// changed delivers the mutation to the fake watch. Callers must NOT hold
// f.mu (RunChanged reads back through the same mutex).
func (f *fakeKube) changed() {
	if f.notify != nil {
		f.notify()
	}
}

func (f *fakeKube) CreateRun(_ context.Context, r *agenticv1alpha1.AgenticRun) error {
	f.mu.Lock()
	k := r.Namespace + "/" + r.Name
	if _, ok := f.runs[k]; ok {
		f.mu.Unlock()
		return apierrors.NewAlreadyExists(schema.GroupResource{Group: "agentic.openshift.io", Resource: "agenticruns"}, r.Name)
	}
	f.created++
	f.uid++
	stored := r.DeepCopy()
	stored.UID = types.UID(fmt.Sprintf("run-uid-%d", f.uid))
	f.runs[k] = stored
	*r = *stored
	f.mu.Unlock()
	f.changed()
	return nil
}

func (f *fakeKube) GetRun(_ context.Context, ns, name string) (*agenticv1alpha1.AgenticRun, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	r, ok := f.runs[ns+"/"+name]
	if !ok {
		return nil, apierrors.NewNotFound(schema.GroupResource{Group: "agentic.openshift.io", Resource: "agenticruns"}, name)
	}
	return r.DeepCopy(), nil
}

func (f *fakeKube) UpdateRun(_ context.Context, r *agenticv1alpha1.AgenticRun) error {
	f.mu.Lock()
	f.runs[r.Namespace+"/"+r.Name] = r.DeepCopy()
	f.mu.Unlock()
	f.changed()
	return nil
}

func (f *fakeKube) ListRuns(_ context.Context, ns string) (*agenticv1alpha1.AgenticRunList, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	list := &agenticv1alpha1.AgenticRunList{}
	for _, r := range f.runs {
		if r.Namespace == ns {
			list.Items = append(list.Items, *r.DeepCopy())
		}
	}
	return list, nil
}

func (f *fakeKube) UpdateApproval(_ context.Context, a *agenticv1alpha1.AgenticRunApproval) error {
	f.mu.Lock()
	f.objects[a.Namespace+"/"+a.Name] = a.DeepCopy()
	f.mu.Unlock()
	f.changed()
	return nil
}

func (f *fakeKube) setConditions(name string, conds ...metav1.Condition) {
	f.mu.Lock()
	r := f.runs["test-ns/"+name]
	for i := range conds {
		meta.SetStatusCondition(&r.Status.Conditions, conds[i])
	}
	f.mu.Unlock()
	f.changed()
}

// mutateRun alters a stored run in place (test driver for verify-don't-
// trust checks, e.g. changing spec.targetCluster directly on the CR).
func (f *fakeKube) mutateRun(name string, fn func(*agenticv1alpha1.AgenticRun)) {
	f.mu.Lock()
	fn(f.runs["test-ns/"+name])
	f.mu.Unlock()
	f.changed()
}

// deleteRun removes a run (TTL GC / informer delete path).
func (f *fakeKube) deleteRun(name string) {
	f.mu.Lock()
	delete(f.runs, "test-ns/"+name)
	f.mu.Unlock()
	f.changed()
}

// putObject stores a Result CR or AgenticRunApproval (server-assigned UID
// if empty) and delivers the watch event.
func (f *fakeKube) putObject(obj ctrlclient.Object) {
	f.mu.Lock()
	if obj.GetUID() == "" {
		f.uid++
		obj.SetUID(types.UID(fmt.Sprintf("obj-uid-%d", f.uid)))
	}
	f.objects["test-ns/"+obj.GetName()] = obj.DeepCopyObject().(ctrlclient.Object)
	f.mu.Unlock()
	f.changed()
}

func (f *fakeKube) getObject(name string) (ctrlclient.Object, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	o, ok := f.objects["test-ns/"+name]
	if !ok {
		return nil, apierrors.NewNotFound(schema.GroupResource{Group: "agentic.openshift.io"}, name)
	}
	return o.DeepCopyObject().(ctrlclient.Object), nil
}
func (f *fakeKube) GetAnalysisResult(_ context.Context, _, name string) (*agenticv1alpha1.AnalysisResult, error) {
	o, err := f.getObject(name)
	if err != nil {
		return nil, err
	}
	return o.(*agenticv1alpha1.AnalysisResult), nil
}

func (f *fakeKube) GetExecutionResult(_ context.Context, _, name string) (*agenticv1alpha1.ExecutionResult, error) {
	o, err := f.getObject(name)
	if err != nil {
		return nil, err
	}
	return o.(*agenticv1alpha1.ExecutionResult), nil
}

func (f *fakeKube) GetVerificationResult(_ context.Context, _, name string) (*agenticv1alpha1.VerificationResult, error) {
	o, err := f.getObject(name)
	if err != nil {
		return nil, err
	}
	return o.(*agenticv1alpha1.VerificationResult), nil
}

func (f *fakeKube) GetEscalationResult(_ context.Context, _, name string) (*agenticv1alpha1.EscalationResult, error) {
	o, err := f.getObject(name)
	if err != nil {
		return nil, err
	}
	return o.(*agenticv1alpha1.EscalationResult), nil
}

func (f *fakeKube) GetApproval(_ context.Context, _, name string) (*agenticv1alpha1.AgenticRunApproval, error) {
	o, err := f.getObject(name)
	if err != nil {
		return nil, err
	}
	return o.(*agenticv1alpha1.AgenticRunApproval), nil
}

func (f *fakeKube) CreateSecret(_ context.Context, s *corev1.Secret) error {
	f.mu.Lock()
	f.secWritt++
	k := s.Namespace + "/" + s.Name
	if _, ok := f.secrets[k]; ok {
		f.mu.Unlock()
		return apierrors.NewAlreadyExists(schema.GroupResource{Resource: "secrets"}, s.Name)
	}
	f.rv++
	stored := s.DeepCopy()
	stored.ResourceVersion = fmt.Sprintf("%d", f.rv)
	f.secrets[k] = stored
	f.mu.Unlock()
	return nil
}

func (f *fakeKube) GetSecret(_ context.Context, ns, name string) (*corev1.Secret, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	s, ok := f.secrets[ns+"/"+name]
	if !ok {
		return nil, apierrors.NewNotFound(schema.GroupResource{Resource: "secrets"}, name)
	}
	return s.DeepCopy(), nil
}

func (f *fakeKube) UpdateSecret(_ context.Context, s *corev1.Secret) error {
	f.mu.Lock()
	f.secWritt++
	f.rv++
	stored := s.DeepCopy()
	stored.ResourceVersion = fmt.Sprintf("%d", f.rv)
	f.secrets[s.Namespace+"/"+s.Name] = stored
	f.mu.Unlock()
	return nil
}

func (f *fakeKube) DeleteSecret(_ context.Context, ns, name string) error {
	f.mu.Lock()
	k := ns + "/" + name
	if _, ok := f.secrets[k]; !ok {
		f.mu.Unlock()
		return apierrors.NewNotFound(schema.GroupResource{Resource: "secrets"}, name)
	}
	f.secDel++
	delete(f.secrets, k)
	f.mu.Unlock()
	return nil
}

func (f *fakeKube) secret(name string) *corev1.Secret {
	f.mu.Lock()
	defer f.mu.Unlock()
	s, ok := f.secrets["test-ns/"+name]
	if !ok {
		return nil
	}
	return s.DeepCopy()
}

func (f *fakeKube) runByName(name string) *agenticv1alpha1.AgenticRun {
	f.mu.Lock()
	defer f.mu.Unlock()
	r, ok := f.runs["test-ns/"+name]
	if !ok {
		return nil
	}
	return r.DeepCopy()
}

// stubExchanger returns a fixed Token H.
type stubExchanger struct{ token string }

func (s stubExchanger) Exchange(context.Context, string) (string, error) { return s.token, nil }

// ---- harness ---------------------------------------------------------------

// testClock is a controllable clock: frozen by default (deterministic
// projections), advanceable for deadline/grace tests.
type testClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *testClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *testClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

var testNow = time.Unix(1_750_000_000, 0)

func makeToken(sub string, exp int64) string {
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"none"}`))
	claims := map[string]any{"sub": sub, "iss": "https://kc/realms/hub"}
	if exp != 0 {
		claims["exp"] = exp
	}
	payload, _ := json.Marshal(claims)
	return header + "." + base64.RawURLEncoding.EncodeToString(payload) + ".sig"
}

type harness struct {
	kube    *fakeKube
	server  *httptest.Server
	watcher *Watcher
	clock   *testClock
}

// newHarness builds the full M3 stack against the fake kube: HTTP handler
// + watch controller sharing one store, with the fake informer delivery
// wired through fakeKube.notify.
func newHarness(t *testing.T) *harness {
	t.Helper()
	return newHarnessWithConfig(t, nil)
}

func newHarnessWithConfig(t *testing.T, tune func(*Config)) *harness {
	t.Helper()
	return newHarnessSharing(t, newFakeKube(), &testClock{t: testNow}, tune)
}

// newHarnessSharing builds a full harness on top of an EXISTING fake kube
// and clock — the adapter-restart shape: fresh store, fresh handler and
// watcher, same API server state and same clock domain.
func newHarnessSharing(t *testing.T, kube *fakeKube, clock *testClock, tune func(*Config)) *harness {
	t.Helper()
	cfg := Config{
		Namespace:            "test-ns",
		TargetHeader:         "X-Hub-Cluster",
		ClockSkew:            30 * time.Second,
		StreamDeadline:       5 * time.Second,
		RenewalTimeout:       time.Hour, // frozen clock: never fires unless advanced
		MaxPauses:            3,
		TerminalCleanupGrace: 30 * time.Second,
		SweepInterval:        10 * time.Millisecond,
		Now:                  clock.Now,
	}
	if tune != nil {
		tune(&cfg)
	}
	handler, err := New(cfg, Deps{
		Store:       store.New(),
		Credentials: auth.HeaderSource{},
		Exchanger:   stubExchanger{token: "token-h-value"},
		Readers:     kube,
		Results:     kube,
		Submitters:  func(context.Context, string) (run.RunClient, error) { return kube, nil },
		Runs:        &run.Creator{Namespace: "test-ns", AgentName: "default", WrittenBy: "adapter-sa"},
		Tokens:      &run.TokenWriter{Secrets: kube, Namespace: "test-ns", Identity: "adapter-sa"},
	})
	if err != nil {
		t.Fatal(err)
	}
	watcher, err := handler.NewWatcher()
	if err != nil {
		t.Fatal(err)
	}
	// Fake informer delivery: every observable kube mutation re-projects
	// through the real watch path.
	kube.notify = func() {
		ctx := context.Background()
		list, err := kube.ListRuns(ctx, "test-ns")
		if err != nil {
			return
		}
		for i := range list.Items {
			watcher.RunChanged(ctx, &list.Items[i])
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go watcher.Start(ctx)

	mux := http.NewServeMux()
	mux.Handle("/healthz", HealthHandler())
	mux.Handle("/.well-known/agent-card.json", ServeAgentCard(AgentCard(CardConfig{
		Name: "lightspeed-hub", URL: "https://praxis/hub-adapter", Version: "0.1.0",
		SkillID: "cluster-investigation", SkillName: "investigate",
	})))
	mux.Handle("/", WithRequestCapture(a2asrv.NewJSONRPCHandler(handler)))

	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return &harness{kube: kube, server: srv, watcher: watcher, clock: clock}
}

// rpc posts a JSON-RPC request. extraHeaders carries X-Hub-Cluster etc.
func (h *harness) rpc(t *testing.T, method string, params map[string]any, token string, extraHeaders map[string]string) (map[string]any, *http.Response) {
	t.Helper()
	body := map[string]any{"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
	raw, _ := json.Marshal(body)
	req, err := http.NewRequest(http.MethodPost, h.server.URL+"/", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for k, v := range extraHeaders {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var decoded map[string]any
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("response not JSON: %s", data)
	}
	return decoded, resp
}

func sendMessage(t *testing.T, h *harness, msg map[string]any, token string, headers map[string]string) map[string]any {
	t.Helper()
	decoded, _ := h.rpc(t, "message/send", map[string]any{"message": msg}, token, headers)
	return decoded
}

func resultTask(t *testing.T, decoded map[string]any) map[string]any {
	t.Helper()
	if e, ok := decoded["error"].(map[string]any); ok {
		t.Fatalf("unexpected JSON-RPC error: %v", e)
	}
	task, ok := decoded["result"].(map[string]any)
	if !ok || task["kind"] != "task" {
		t.Fatalf("result is not a task: %v", decoded)
	}
	return task
}

func taskState(task map[string]any) string {
	status, _ := task["status"].(map[string]any)
	state, _ := status["state"].(string)
	return state
}

func userMessage(id, taskID, contextID, text string, metadata map[string]any) map[string]any {
	msg := map[string]any{
		"role":      "user",
		"messageId": id,
		"taskId":    taskID,
		"contextId": contextID,
		"parts":     []map[string]any{{"kind": "text", "text": text}},
	}
	if metadata != nil {
		msg["metadata"] = metadata
	}
	return msg
}

var freshToken = makeToken("acme-agent", testNow.Add(time.Hour).Unix())
var expiredToken = makeToken("acme-agent", testNow.Add(-time.Hour).Unix())

// ---- tests -----------------------------------------------------------------

// Happy path: message/send creates EXACTLY ONE AgenticRun and ONE run-token
// Secret with the exact repo C contract, and returns a submitted task.
func TestMessageSendHappyPath(t *testing.T) {
	h := newHarness(t)

	decoded := sendMessage(t, h, userMessage("m1", "t1", "c1", "why is pod foo crashlooping?",
		map[string]any{"targetCluster": "spoke-a"}), freshToken, nil)
	task := resultTask(t, decoded)
	if taskState(task) != "submitted" {
		t.Errorf("state = %q, want submitted", taskState(task))
	}
	if task["id"] != "t1" || task["contextId"] != "c1" {
		t.Errorf("task id mapping = %v", task)
	}

	if h.kube.created != 1 {
		t.Errorf("AgenticRun creates = %d, want 1", h.kube.created)
	}
	runCR := h.kube.runByName(store.RunName("c1", "t1"))
	if runCR.Spec.Request != "why is pod foo crashlooping?" {
		t.Errorf("spec.request = %q", runCR.Spec.Request)
	}
	if runCR.Spec.TargetCluster != "spoke-a" {
		t.Errorf("spec.targetCluster = %q", runCR.Spec.TargetCluster)
	}
	if runCR.Annotations[run.AnnotationTaskID] != "t1" {
		t.Errorf("task-id annotation = %v", runCR.Annotations)
	}

	// The run-token Secret contract, exactly (gateway.go:35,38,117-130).
	secretName := run.RunTokenSecretName(string(runCR.UID))
	s := h.kube.secret(secretName)
	if s == nil {
		t.Fatalf("secret %s not written", secretName)
	}
	if got := string(s.Data[run.RunTokenKey]); got != freshToken {
		t.Errorf("token-a value mismatch (len %d vs %d)", len(got), len(freshToken))
	}
	if s.Labels[run.LabelAgenticRun] != string(runCR.UID) {
		t.Errorf("label agentic-run = %q, want %q", s.Labels[run.LabelAgenticRun], runCR.UID)
	}
	if s.Labels[run.LabelSpokeCluster] != "spoke-a" {
		t.Errorf("label spoke-cluster = %q", s.Labels[run.LabelSpokeCluster])
	}
	if h.kube.secWritt != 1 {
		t.Errorf("secret writes = %d, want 1", h.kube.secWritt)
	}
}

// Duplicate messageId: same record returned, NO second run submitted.
func TestDuplicateMessageID(t *testing.T) {
	h := newHarness(t)
	msg := userMessage("m-dup", "t1", "c1", "question", map[string]any{"targetCluster": "spoke-a"})

	first := resultTask(t, sendMessage(t, h, msg, freshToken, nil))
	second := resultTask(t, sendMessage(t, h, msg, freshToken, nil))

	if first["id"] != second["id"] {
		t.Errorf("duplicate delivery returned different tasks: %v vs %v", first["id"], second["id"])
	}
	if h.kube.created != 1 {
		t.Errorf("AgenticRun creates = %d, want 1 (no re-submission)", h.kube.created)
	}
	if h.kube.secWritt != 1 {
		t.Errorf("secret writes = %d, want 1", h.kube.secWritt)
	}
}

// Expired Token A: task parks in auth-required, NO CR, NO secret.
func TestExpiredTokenParksAuthRequired(t *testing.T) {
	h := newHarness(t)
	decoded := sendMessage(t, h, userMessage("m1", "t1", "c1", "question",
		map[string]any{"targetCluster": "spoke-a"}), expiredToken, nil)
	task := resultTask(t, decoded)
	if taskState(task) != "auth-required" {
		t.Fatalf("state = %q, want auth-required", taskState(task))
	}
	if h.kube.created != 0 {
		t.Errorf("AgenticRun creates = %d, want 0", h.kube.created)
	}
	if h.kube.secWritt != 0 {
		t.Errorf("secret writes = %d, want 0 (never write a dead token)", h.kube.secWritt)
	}
}

// Renewal follow-up on a parked task: fresh A creates the run with the
// ORIGINAL question (not the credential-only follow-up text).
func TestRenewalFollowUpCreatesRunWithOriginalRequest(t *testing.T) {
	h := newHarness(t)
	sendMessage(t, h, userMessage("m1", "t1", "c1", "the original question",
		map[string]any{"targetCluster": "spoke-a"}), expiredToken, nil)

	decoded := sendMessage(t, h, userMessage("m2", "t1", "c1", "",
		map[string]any{"targetCluster": "spoke-a"}), freshToken, nil)
	task := resultTask(t, decoded)
	if taskState(task) != "submitted" {
		t.Fatalf("state = %q, want submitted after renewal", taskState(task))
	}
	if h.kube.created != 1 {
		t.Fatalf("creates = %d, want 1", h.kube.created)
	}
	runCR := h.kube.runByName(store.RunName("c1", "t1"))
	if runCR.Spec.Request != "the original question" {
		t.Errorf("spec.request = %q, want the ORIGINAL question", runCR.Spec.Request)
	}
	// The fresh token must be in the Secret.
	s := h.kube.secret(run.RunTokenSecretName(string(runCR.UID)))
	if string(s.Data[run.RunTokenKey]) != freshToken {
		t.Error("renewed token not written")
	}
}

// Target rules (design §4.2): conflicts are rejected, no CR.
func TestTargetRejections(t *testing.T) {
	h := newHarness(t)
	// Conflict: header says spoke-a, metadata says spoke-b.
	decoded := sendMessage(t, h, userMessage("m1", "t1", "c1", "q",
		map[string]any{"targetCluster": "spoke-b"}), freshToken,
		map[string]string{"X-Hub-Cluster": "spoke-a"})
	errObj, ok := decoded["error"].(map[string]any)
	if !ok {
		t.Fatalf("want error, got %v", decoded)
	}
	if code, _ := errObj["code"].(float64); code != -32602 {
		t.Errorf("code = %v, want -32602 (invalid params)", errObj["code"])
	}
	if h.kube.created != 0 {
		t.Errorf("creates = %d, want 0 on target conflict", h.kube.created)
	}
}

// The HTTP layer joins repeated headers; Go's http.Header.Values exposes
// them separately, and the handler must see ALL values (OR-match hazard).
func TestTargetDuplicateHeaderValues(t *testing.T) {
	h := newHarness(t)

	body := map[string]any{"jsonrpc": "2.0", "id": 1, "method": "message/send",
		"params": map[string]any{"message": userMessage("m1", "t1", "c1", "q", nil)}}
	raw, _ := json.Marshal(body)
	req, _ := http.NewRequest(http.MethodPost, h.server.URL+"/", bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+freshToken)
	req.Header.Add("X-Hub-Cluster", "spoke-a")
	req.Header.Add("X-Hub-Cluster", "spoke-a")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var decoded map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&decoded)
	if _, ok := decoded["error"]; !ok {
		t.Fatalf("want error for duplicate target header, got %v", decoded)
	}
	if h.kube.created != 0 {
		t.Errorf("creates = %d, want 0", h.kube.created)
	}
}

// Agent card served at the well-known path.
func TestAgentCardServed(t *testing.T) {
	h := newHarness(t)
	resp, err := http.Get(h.server.URL + "/.well-known/agent-card.json")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var card map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&card); err != nil {
		t.Fatal(err)
	}
	if card["name"] != "lightspeed-hub" {
		t.Errorf("card name = %v", card["name"])
	}
	caps, _ := card["capabilities"].(map[string]any)
	if caps["streaming"] != true {
		t.Errorf("capabilities = %v, want streaming=true", caps)
	}
}

// tasks/get reflects the CR's status via the (minimal M2) mapping.
func TestTasksGetReflectsCRStatus(t *testing.T) {
	h := newHarness(t)
	resultTask(t, sendMessage(t, h, userMessage("m1", "t1", "c1", "q",
		map[string]any{"targetCluster": "spoke-a"}), freshToken, nil))

	runName := store.RunName("c1", "t1")

	// No conditions → Pending → submitted.
	decoded, _ := h.rpc(t, "tasks/get", map[string]any{"id": "t1"}, freshToken, nil)
	if taskState(resultTask(t, decoded)) != "submitted" {
		t.Errorf("fresh run should map to submitted")
	}

	// Analyzed=Unknown → Analyzing → working.
	h.kube.setConditions(runName, metav1.Condition{
		Type: agenticv1alpha1.AgenticRunConditionAnalyzed, Status: metav1.ConditionUnknown,
		Reason: "InProgress", Message: "analysis running",
	})
	decoded, _ = h.rpc(t, "tasks/get", map[string]any{"id": "t1"}, freshToken, nil)
	if taskState(resultTask(t, decoded)) != "working" {
		t.Errorf("analyzing run should map to working, got %q", taskState(resultTask(t, decoded)))
	}

	// Proposed (Analyzed=True) → input-required: the approval-decision
	// DataPart path answers it (design §7.2, M3).
	h.kube.setConditions(runName, metav1.Condition{
		Type: agenticv1alpha1.AgenticRunConditionAnalyzed, Status: metav1.ConditionTrue,
		Reason: "AnalysisComplete", Message: "options ready",
	})
	decoded, _ = h.rpc(t, "tasks/get", map[string]any{"id": "t1"}, freshToken, nil)
	if got := taskState(resultTask(t, decoded)); got != "input-required" {
		t.Errorf("proposed run maps to %q, want input-required (M3 §7.2 mapping)", got)
	}
}

// tasks/cancel: not-supported for live tasks (design Q6), terminal state
// returned for finished ones (design §2.3).
func TestTasksCancel(t *testing.T) {
	h := newHarness(t)
	resultTask(t, sendMessage(t, h, userMessage("m1", "t1", "c1", "q",
		map[string]any{"targetCluster": "spoke-a"}), freshToken, nil))

	decoded, _ := h.rpc(t, "tasks/cancel", map[string]any{"id": "t1"}, freshToken, nil)
	errObj, ok := decoded["error"].(map[string]any)
	if !ok {
		t.Fatalf("want not-cancelable error, got %v", decoded)
	}
	if code, _ := errObj["code"].(float64); code != -32002 {
		t.Errorf("code = %v, want -32002 (task not cancelable)", errObj["code"])
	}

	// Terminal run: cancel returns the terminal state, not an error.
	runName := store.RunName("c1", "t1")
	h.kube.setConditions(runName, metav1.Condition{
		Type: agenticv1alpha1.AgenticRunConditionVerified, Status: metav1.ConditionTrue,
		Reason: "Verified", Message: "done",
	})
	decoded, _ = h.rpc(t, "tasks/cancel", map[string]any{"id": "t1"}, freshToken, nil)
	if got := taskState(resultTask(t, decoded)); got != "completed" {
		t.Errorf("terminal cancel = %q, want completed", got)
	}
}

// Ownership (design §5.6): a different principal gets TaskNotFound, never
// the task and never AccessDenied.
func TestOwnershipMasking(t *testing.T) {
	h := newHarness(t)
	resultTask(t, sendMessage(t, h, userMessage("m1", "t1", "c1", "q",
		map[string]any{"targetCluster": "spoke-a"}), freshToken, nil))

	otherToken := makeToken("someone-else", testNow.Add(time.Hour).Unix())
	decoded, _ := h.rpc(t, "tasks/get", map[string]any{"id": "t1"}, otherToken, nil)
	errObj, ok := decoded["error"].(map[string]any)
	if !ok {
		t.Fatalf("want TaskNotFound for non-owner, got %v", decoded)
	}
	if code, _ := errObj["code"].(float64); code != -32001 {
		t.Errorf("code = %v, want -32001 (task not found)", errObj["code"])
	}
}

// Unknown task: TaskNotFound.
func TestTasksGetUnknown(t *testing.T) {
	h := newHarness(t)
	decoded, _ := h.rpc(t, "tasks/get", map[string]any{"id": "nope"}, freshToken, nil)
	if _, ok := decoded["error"]; !ok {
		t.Fatalf("want error, got %v", decoded)
	}
}

// No credential: fail closed.
func TestNoCredentialRejected(t *testing.T) {
	h := newHarness(t)
	decoded := sendMessage(t, h, userMessage("m1", "t1", "c1", "q", nil), "", nil)
	if _, ok := decoded["error"]; !ok {
		t.Fatalf("want error without Authorization, got %v", decoded)
	}
	if h.kube.created != 0 {
		t.Errorf("creates = %d, want 0", h.kube.created)
	}
}

// message/stream: initial task event, then progress to terminal with
// Final=true.
func TestMessageStream(t *testing.T) {
	h := newHarness(t)

	body := map[string]any{"jsonrpc": "2.0", "id": 7, "method": "message/stream",
		"params": map[string]any{"message": userMessage("m1", "t1", "c1", "q",
			map[string]any{"targetCluster": "spoke-a"})}}
	raw, _ := json.Marshal(body)
	req, _ := http.NewRequest(http.MethodPost, h.server.URL+"/", bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+freshToken)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); !strings.HasPrefix(ct, "text/event-stream") {
		t.Fatalf("content-type = %q", ct)
	}

	// Advance the run to terminal while the stream is open.
	go func() {
		time.Sleep(50 * time.Millisecond)
		h.kube.setConditions(store.RunName("c1", "t1"), metav1.Condition{
			Type: agenticv1alpha1.AgenticRunConditionVerified, Status: metav1.ConditionTrue,
			Reason: "Verified", Message: "done",
		})
	}()

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 1<<20), 1<<20)
	var events []map[string]any
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		var ev map[string]any
		if err := json.Unmarshal([]byte(line[len("data: "):]), &ev); err != nil {
			t.Fatalf("bad SSE data: %q", line)
		}
		events = append(events, ev)
		result, ok := ev["result"].(map[string]any)
		if !ok {
			continue
		}
		if final, _ := result["final"].(bool); final {
			break
		}
	}
	if len(events) < 2 {
		t.Fatalf("want at least initial + final events, got %d", len(events))
	}
	first := events[0]["result"].(map[string]any)
	if first["kind"] != "task" {
		t.Errorf("first event kind = %v", first["kind"])
	}
	last := events[len(events)-1]["result"].(map[string]any)
	if last["kind"] != "status-update" || last["final"] != true {
		t.Fatalf("last event = %v", last)
	}
	status, _ := last["status"].(map[string]any)
	if status["state"] != "completed" {
		t.Errorf("final state = %v", status["state"])
	}
}

// Revise follow-up: a DataPart {"revise": "..."} patches
// spec.revisionFeedback on the existing run, no new run.
func TestReviseFollowUp(t *testing.T) {
	h := newHarness(t)
	resultTask(t, sendMessage(t, h, userMessage("m1", "t1", "c1", "first question",
		map[string]any{"targetCluster": "spoke-a"}), freshToken, nil))

	msg := userMessage("m2", "t1", "c1", "", map[string]any{"targetCluster": "spoke-a"})
	msg["parts"] = []map[string]any{
		{"kind": "data", "data": map[string]any{"revise": "focus on networking"}},
	}
	decoded := sendMessage(t, h, msg, freshToken, nil)
	if _, ok := decoded["error"]; ok {
		t.Fatalf("revise follow-up failed: %v", decoded["error"])
	}
	if h.kube.created != 1 {
		t.Errorf("creates = %d, want 1 (revision is not a new run)", h.kube.created)
	}
	runCR := h.kube.runByName(store.RunName("c1", "t1"))
	if runCR.Spec.RevisionFeedback != "focus on networking" {
		t.Errorf("revisionFeedback = %q", runCR.Spec.RevisionFeedback)
	}
	if runCR.Spec.Request != "first question" {
		t.Errorf("request must stay immutable, got %q", runCR.Spec.Request)
	}
}

// Target immutability on follow-up: a mismatched target is rejected, not
// OR-matched (design §5.6).
func TestFollowUpTargetImmutable(t *testing.T) {
	h := newHarness(t)
	resultTask(t, sendMessage(t, h, userMessage("m1", "t1", "c1", "q",
		map[string]any{"targetCluster": "spoke-a"}), freshToken, nil))

	decoded := sendMessage(t, h, userMessage("m2", "t1", "c1", "more",
		map[string]any{"targetCluster": "spoke-B"}), freshToken, nil)
	if _, ok := decoded["error"]; !ok {
		t.Fatalf("want error for changed target, got %v", decoded)
	}
}

// Restart recovery: a fresh handler (empty store) rebuilds the task record
// from AgenticRun annotations on tasks/get.
func TestRestartRecovery(t *testing.T) {
	h := newHarness(t)
	resultTask(t, sendMessage(t, h, userMessage("m1", "t1", "c1", "q",
		map[string]any{"targetCluster": "spoke-a"}), freshToken, nil))

	// Second harness sharing the same fake kube = adapter restart.
	handler, err := New(Config{
		Namespace: "test-ns", ClockSkew: 30 * time.Second,
		StreamDeadline: 5 * time.Second, RenewalTimeout: time.Hour,
		TerminalCleanupGrace: 30 * time.Second, SweepInterval: 10 * time.Millisecond,
		Now: func() time.Time { return testNow },
	}, Deps{
		Store:       store.New(), // empty — restart
		Credentials: auth.HeaderSource{},
		Exchanger:   stubExchanger{token: "token-h"},
		Readers:     h.kube,
		Results:     h.kube,
		Submitters:  func(context.Context, string) (run.RunClient, error) { return h.kube, nil },
		Runs:        &run.Creator{Namespace: "test-ns", AgentName: "default"},
		Tokens:      &run.TokenWriter{Secrets: h.kube, Namespace: "test-ns", Identity: "adapter-sa"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := handler.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}

	decoded, resp := h.rpc(t, "tasks/get", map[string]any{"id": "t1"}, freshToken, nil)
	_ = resp
	task := resultTask(t, decoded)
	if task["id"] != "t1" {
		t.Errorf("recovered task = %v", task)
	}
}

// TaskStateFor spot-checks for phases not reachable through the fakes.
func TestTaskStateForPhases(t *testing.T) {
	runCR := &agenticv1alpha1.AgenticRun{}
	if got := TaskStateFor(runCR); got != a2a.TaskStateSubmitted {
		t.Errorf("empty run = %v", got)
	}
	runCR.Status.Conditions = []metav1.Condition{{
		Type: agenticv1alpha1.AgenticRunConditionDenied, Status: metav1.ConditionTrue,
	}}
	if got := TaskStateFor(runCR); got != a2a.TaskStateFailed {
		t.Errorf("denied = %v", got)
	}
	runCR.Status.Conditions = []metav1.Condition{{
		Type: agenticv1alpha1.AgenticRunConditionEmergencyStopped, Status: metav1.ConditionTrue,
	}}
	if got := TaskStateFor(runCR); got != a2a.TaskStateCanceled {
		t.Errorf("emergency-stopped = %v", got)
	}
}
