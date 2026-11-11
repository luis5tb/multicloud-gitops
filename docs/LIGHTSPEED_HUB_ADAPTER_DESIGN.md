# Design: the hub A2A adapter (Task B, Phase 1)

Status: DRAFT for review. Authoritative plan: `PLAN-lightspeed-hub-migration.md`
(v3.8). This document designs the **net-new hub A2A adapter** — the Go service
that gives lightspeed-hub (repo B) an A2A front door and bridges A2A task
semantics onto the `AgenticRun` machinery of lightspeed-agentic-operator
(repo C, branch `a2a-support`). It is the counterpart of repo C's
already-implemented gateway credential mode and `CredentialsExpired` pause
contract.

Conventions used here:

- **Token A** = the caller's original token (no groups, by contract:
  `PLAN-lightspeed-hub-migration.md:14-18`). **Token H** = hub-audience
  exchange of A (plan `:493-497`). **Token U** = spoke-audience exchange of A,
  minted per call by the hub-hosted spoke-facing PPE (plan B1, `:710-833`).
- Every claim about current code cites `file:line`. Citations into
  `charts/all/praxis-proxy` and `charts/all/keycloak-oidc` reflect the
  pre-B1 state of branch `rca-mao-praxis-hub`; a concurrent implementer is
  adding spoke-gateway wiring to those charts, so line numbers there may
  drift — treat the cited *facts* as authoritative and re-locate lines before
  editing.
- Repo C paths are relative to
  `/home/ltomasbo/git_repos/lightspeed-agentic-operator` (branch
  `a2a-support`).

---

## 1. Role and placement

**What it is.** A net-new Go service (kubebuilder-style controller-runtime
binary with an HTTP/A2A server), deployed on the hub cluster alongside the
repo B and repo C operators. It is the A2A protocol front door for
lightspeed-hub: it accepts A2A `message/send` (and streaming), maps the
message onto an `AgenticRun` CR, watches that CR and its Result CRs, and
projects status and artifacts back onto the A2A task. It also owns the
run-token Secret lifecycle (`ls-run-token-{runUID}`) that repo C's gateway
credential mode consumes.

**Placement.** An **internal-only Service**, reachable only through Praxis —
exactly the boundary OLS used to occupy (plan `:459-464`). Reasons:

- Praxis is the caller allow-list. The hub API's OIDC re-authentication
  (Token H) enforces issuer/audience/groups but **does not enforce Praxis's
  caller restrictions** — "an OIDC-valid token alone is not proof that the
  caller was admitted by Praxis" (`docs/ARCHITECTURE.md:110-115`, historical
  but the reasoning stands). The adapter's Service gets no public Route and a
  NetworkPolicy admitting only the Praxis pods' namespace/pod selector.
- Trade-off: internal-only means every debugging session needs a port-forward
  or must go through Praxis. We accept that; the alternative (public adapter
  route "temporarily") is how the MAS management API exposure issue
  (plan `:958-962`) happened.

**What it is NOT** (scope fence, from plan `:610-639`):

- **Not a proxy.** It terminates A2A and creates CRs; it does not forward
  arbitrary HTTP. The spoke-leg proxying is the hub-hosted spoke-facing PPE
  (B1), a separate deployment with separate trust.
- **Not the LLM.** All model inference happens in repo C sandbox pods
  (repo C `ARCHITECTURE.md:76-84` — analysis/execution/verification steps each
  run in an ephemeral sandbox pod).
- **Not MAS.** It does not orchestrate multi-agent graphs; it is itself a
  *downstream* agent from MAS's perspective, one hop further out than OLS was.
  MAS's Temporal machinery, sessions, and mailbox are upstream of it
  (plan mapping table `:371-379`).
- **Not a credential store beyond the run-token Secret.** It never holds
  standing spoke kubeconfigs, never holds LLM credentials, and (Phase 1) never
  mints Token U — the PPE does, per call.

**Language/runtime choice.** Go, reusing controller-runtime (watch/informer
machinery, leader election for the single-active-writer guarantee on task
state). The A2A protocol server uses the A2A SDK (plan `:611`). Trade-off: the
only proven A2A-server implementations we own are Python (OLS fork's
`a2a_executor.py`, MAS); a Go server is new code but keeps one toolchain with
repo B/repo C, shares their CRD Go types (`api/` module is published
separately for exactly this — repo C `ARCHITECTURE.md:88-110`), and avoids a
second Python image in the hub security context. If the Go A2A SDK proves
immature during M2, the fallback is an A2A-to-CR translation shim behind the
same contract — but that is a rewrite of the server layer only, not of the
design.

---

## 2. A2A surface

### 2.1 Server and agent card

- JSON-RPC A2A endpoints (`message/send`, `message/stream`, `tasks/get`,
  `tasks/cancel`, `resubscribe`) plus an agent card at
  `{base}/.well-known/agent-card.json` (served by Praxis's static card today
  for discovery — `charts/all/praxis-proxy/files/praxis.yaml:21-30`; the card
  will be re-authored toward this adapter per plan `:641-642`).
- The card advertises: one skill ("Red Hat cluster investigation via
  lightspeed-hub"), input modes (text), output modes (text + structured
  artifacts from Result CRs), and streaming support.

### 2.2 ID mapping — reuse the proven MAS semantics

The adapter's ID discipline mirrors what MAS already proves works upstream
(plan `:371-379`), because ACME's client behavior (renewal follow-ups on the
same task/context, `agents/AUTHENTICATION.md:469-478` describes the pattern
from MAS) is built against exactly these semantics:

| A2A concept | Adapter meaning | Persistence |
| --- | --- | --- |
| `contextId` | one investigation conversation, owned by the authenticated principal | adapter-maintained record mapping contextId → principal (sub), allowed target set, and a *list* of taskIds |
| `taskId` | one submitted work item = one `AgenticRun` (name/UID) | CR itself is the durable record; adapter stores taskId → {run name, run UID, principal, target, latest messageId} |
| `messageId` | one submission/follow-up | duplicate `messageId` on an existing task returns the current task record **without** re-submitting (MAS: "duplicate delivery returns the existing record rather than submitting a second turn", plan `:375`) |

Difference from MAS, stated deliberately: MAS maps contextId → a MAS
*session* and Temporal workflow `a2a-<taskId>` (plan `:376-377`). **The
adapter has no Temporal and no session engine.** Its equivalent of "same
conversation" is purely the contextId record above, and its equivalent of
workflow-ID-based `REJECT_DUPLICATE` idempotency is the CR name: the adapter
derives a deterministic `AgenticRun` name from the taskId (see §4) and uses
create-or-get — a `AlreadyExists` on a different run UID means a taskId
collision and fails closed with an A2A error, never a silent second run.
"Uncertain start" (create request timed out / connection dropped before the
response): retry the same create with the same derived name; the CRD's CEL
immutability (`api/v1alpha1/agenticrun_types.go:303-310`) means a retry with
identical spec is a no-op get, and a retry with *different* spec is rejected
by the API server itself — an acceptable substitute for Temporal's
`REJECT_DUPLICATE` given AgenticRuns are run-to-completion.

### 2.3 Task lifecycle semantics

- **Cancellation** (`tasks/cancel`): patch the AgenticRun's stop path. Repo C
  has an `EmergencyStopped` phase and `Stopping` condition
  (`agenticrun_types.go:41,62,263-265`); whether "cancel" maps to the
  emergency-stop or a gentler stop is an open question (§10, Q6) — the
  emergency stop is the only stop semantic visible in the inspected types.
  Ownership is validated first (§5.6). A terminal run cannot be cancelled;
  return the terminal state instead.
- **Result retention**: A2A artifacts are retained **exactly as long as the
  CRs are** — repo C owns TTL via `spec.terminalTTL` / `status.deleteAfter`
  (`agenticrun_types.go:386-394,440-445`). The adapter does not copy artifacts
  into its own store. After the CR is GC'd, `tasks/get` returns
  `TaskNotFound` (a documented, non-surprising terminal answer). Trade-off:
  no adapter-side retention means no adapter-side secret-leak surface and no
  drift between "what the CR says" and "what the task says"; the cost is that
  callers lose history when TTL expires. Acceptable for Phase 1.
- **Protocol version / restart recovery**: the adapter keeps **no required
  in-memory state** — every task is recoverable from (a) the AgenticRun CR,
  (b) the run-token Secret's labels, and (c) the taskId↔run-name derivation.
  A contextId whose record lives only in adapter memory is reconstructed
  lazily: `tasks/get` for an unknown-but-CR-backed taskId rebuilds the mapping
  from the CR's annotations (adapter-written: taskId, principal hash, original
  messageId). This is the same "durable dispatch intent" property the plan
  requires of MAS's adapter (plan `:382-391`) — applied to ourselves.
- **Bounded waits**: streaming `message/stream` holds open at most until the
  run reaches a terminal phase or a wall-clock deadline (configurable,
  default 30 min, less than repo C's agent execution budget — repo C
  `result_types.go:35-36` shows the operator enforces its own
  `AgentTimeout`). After the deadline the stream returns a
  `TaskStateFailed`-equivalent with "deadline exceeded, poll tasks/get".
  Renewal pauses (§6) do **not** count against the execution deadline by
  themselves, but a renewal wait has its own bound (§6.4).

---

## 3. Authentication contract

Per the plan's Phase 1 table (plan `:466-473`) and the current identity chain
(`agents/AUTHENTICATION.md:40-95`): today's inbound Token A is minted by ACME
via `client_credentials` + SPIFFE assertion, `aud=["mas-a2a"]`,
`azp=acme-agent` (`AUTHENTICATION.md:311-321`); MAS validates it and passes
it through unchanged (`AUTHENTICATION.md:64-75`), so **Praxis sees `azp=acme-agent`,
never a MAS identity** (`AUTHENTICATION.md:88-94`). Human Keycloak tokens and
external Red Hat SSO tokens are future inputs with the same shape of handling.

### 3.1 Inbound validation (Praxis, not the adapter)

Praxis continues to be the caller gate: JWT signature/issuer/audience/JWKS
plus the `claim.azp` allow-list
(`charts/all/praxis-proxy/files/policy.yaml:153-165`, currently rendered as a
`role: user` + explicit `deny(...)` workaround for the pinned praxis-policy
0.3.1's `role: client` defect — `policy.yaml:18-36`). For the hub route the
allow-list gains the adapter's expected callers (still `acme-agent` today;
the external SSO client's `azp` is already handled the same way,
`AUTHENTICATION.md:118-126`). Multi-issuer support already exists
(`MULTI_OIDC_CONFIGURATION.md`, referenced at plan `:586-588`).

### 3.2 First exchange A→H: preferred at Praxis, fallback in the adapter

The plan is explicit (plan `:475-497`): the first exchange is **required**
(it is the only way to obtain a hub-trusted issuer, a hub-accepted audience,
and Keycloak-mapped groups — an audience mapper alone cannot do this, and by
contract even Keycloak-issued A has no groups). Preferred placement: Praxis,
after inbound policy passes. Fallback: the adapter receives and validates A
itself and exchanges it for H, exactly as OLS's `a2a_auth.py` did
(`docs/ARCHITECTURE.md:95-103`, historical). Which one ships first is gated
by the PPE version gate (§10, Q2/Q3).

If the adapter performs the exchange, it reuses the documented Keycloak
prerequisites verbatim (plan `:894-905`): `standard.token.exchange.enabled`
on the exchanging client; **A must already carry the exchanging client as an
audience** (audience mappers on the caller's client, as `lightspeed-mcp`'s
did — `docs/ARCHITECTURE.md:345-352`); SVID `client_assertion` requested with
the realm issuer as audience; omit `client_id` from the token request; after
exchange `sub` is preserved but **`azp` becomes the exchanger** — key
hub-side policy on `sub`/groups, record `azp` as provenance. The
implementation reference for the exchange itself is the external
a2a-split fork's `KeycloakTokenExchanger` at a pinned commit (plan `:889-892`)
— a port to Go, not a re-derivation.

H is used by the adapter for **hub API requests** (creating the AgenticRun,
watching, status patches it is allowed to make). Kubernetes authenticates H
and authorizes by RBAC; the hub's `Authentication` mapping and RoleBindings
are keycloak-oidc chart work (concurrent implementer), not adapter work.

### 3.3 Protected Token A handoff (required, not deferred)

Invariant from the plan (plan `:499-509`): **A must reach the run credential
path** — the sandbox's kubeconfig reads Token A per request from
`ls-run-token-{runUID}` (repo C `docs/gateway-credential-mode.md:19-25`), and
the PPE's per-call A→U exchange needs A as subject. If Praxis performs the
A→H exchange and replaces the outbound `Authorization` header, it must retain
A and deliver it (or an access-controlled handle) to the adapter's
run-credential path through a confidential channel. Concretely:

- A is **never** placed in CR fields, logs, Result CRs, or requests to either
  kube API. The only at-rest location is `ls-run-token-{runUID}`, key
  `token-a` (§5).
- A is bound, at handoff and at storage, to the **verified principal**
  (validated `sub`/issuer), the **immutable run** (UID), and the **immutable
  target** (`spec.targetCluster` is CEL-immutable,
  `agenticrun_types.go:304`). Spoofed handoff metadata — e.g. a handoff
  header claiming a different principal than the validated one — is rejected
  or overwritten, never merged.
- If Praxis cannot do exchange + secure handoff, the adapter receives A
  directly (validation at Praxis still applies) and does the exchange itself
  (plan `:506-509`).
- **Never** create runs as the adapter's own service account identity
  (plan `:509`) — that would make every run attributable to the adapter and
  destroy the requester-authority property (plan `:922`).

### 3.4 Token U (B1, out of adapter scope but adjacent)

The hub-hosted spoke-facing PPE mints U per call from A (plan `:710-833`;
repo C `gateway.go:22-30` describes the consumer side). The adapter's only
responsibilities toward U are: (a) write A into the run-token Secret so the
sandbox can present it, and (b) renew that Secret (§6). The adapter does not
mint, cache, or validate U in Phase 1. For why the recorded option-A
fallback is moot against current repo C (which has taken the B1 shape
itself), see the re-assessment note in Q1 (§10) — the full argument lives
there.

---

## 4. AgenticRun creation

### 4.1 Message → CR mapping

An A2A `message/send` on a new task maps onto a minimal, analysis-shaped run:

```yaml
apiVersion: agentic.openshift.io/v1alpha1
kind: AgenticRun
metadata:
  name: a2a-<taskId>            # deterministic, DNS-safe; the idempotency key
  namespace: <adapter-managed ns or caller-mapped ns — see Q4>
  annotations:
    a2a.lightspeed.io/task-id: <taskId>
    a2a.lightspeed.io/context-id: <contextId>
    a2a.lightspeed.io/principal: <verified sub + issuer hash>   # never the token
spec:
  request: <text parts of the A2A message, joined; verbatim>
  targetCluster: <from the deterministic header, §4.2>
  analysis: {}                  # default agent
```

Decisions and trade-offs:

- **Analysis-only by default** (omit `execution`/`verification`). The A2A
  caller asked a question; repo C's own doc positions analysis-only as the
  advisory pattern (`agenticrun_types.go:460-475`). Adding execution on a
  follow-up is not possible on the same run (step fields are immutable,
  `agenticrun_types.go:309-310`) — a follow-up that requests execution creates
  a *new* task/run, referencing the prior context. This is honest about the
  CRD's run-to-completion shape rather than fighting it.
- `request` is immutable (`agenticrun_types.go:322`), which matches A2A
  semantics where a task's original message is history, not mutable state.
  `spec.revisionFeedback` (`agenticrun_types.go:374-384`) is the CRD's
  built-in "same run, refined question" path — the adapter exposes it as a
  structured follow-up DataPart (`revise`), reusing the existing generation-
  bump re-analysis rather than inventing anything.
- LLM credentials, agents, approval policies: **not the adapter's spec to
  fill**. They come from cluster-scoped `LLMProvider`, `Agent`, and
  `ApprovalPolicy` CRs that the repo C stack's deployment (§8) provisions.

### 4.2 Deterministic target-header handling

The header lesson is the oldest live finding in this repo: the MAS planning
LLM emitted `X_OLS_Cluster` (underscores) in a fraction of delegations and
each miss became a silent 404 (`docs/ARCHITECTURE.md:353-381`). The durable
rules, restated as adapter requirements:

1. **The model never constructs header names.** The re-authored blueprint
   carries the target as a *tool argument value* (or the MAS node sets it),
   and the mapping from that value to the transport header is deterministic
   code, not prompt text. The header name for the hub leg is fixed
   (`X-Hub-Cluster` proposed — rename is chart-level, decided with the
   concurrent praxis work).
2. **Reject duplicate/conflicting target headers.** The pinned router
   OR-matches repeated header values (plan `:627-632`, citing
   `files/praxis.yaml:91-99` in its pre-rework form — the router behavior is
   an upstream property that survives the chart rewrite), so a request
   carrying two cluster headers matches if *any* value is valid. The adapter
   therefore treats a multi-valued target header as a protocol error
   (HTTP-level rejection at the gateway is also being added — plan `:629-632`;
   the adapter check is defense-in-depth, not the only line).
3. **Caller×target rules live in the router** (`praxis.yaml`), not APL —
   APL route selectors accept path/method only (plan `:631-632`).

### 4.3 Server-side requester×target authorization

Gateway enforcement is bypassable (plan `:620-626`): kube permission to create
agenticruns does not restrict `spec.targetCluster`, and direct kube callers
skip Praxis entirely. The fix is an **admission webhook shipped in the new
`charts/all/lightspeed-hub` chart** (plan `:621-626`, owner named in Round-3
finding 4, plan `:973-974`): a ValidatingAdmissionPolicy (or a small webhook
deployment if CEL can't express it) on CREATE/UPDATE of agenticruns that
validates `spec.targetCluster` against the **authenticated requester's**
permitted targets.

- **Phase 0 found no existing hook in repo C for this** — repo C ships
  **two** admission policies, neither restricting `targetCluster` or
  requester on AgenticRuns: `config/admission/agenticrun-suspension-policy.yaml`
  (blocks creation while `AgenticOLSConfig.spec.suspended=true`) and
  `config/admission/agenticolsconfig-safety-policy.yaml` (guards UPDATE/DELETE
  of AgenticOLSConfigs, e.g. against removing a completed suspension). So
  the webhook is ours to own.
- The requester→targets mapping source: the webhook cannot see Token H (the
  API server consumed it), but it *can* see the authenticated user/groups of
  the create request. Design: a CR or ConfigMap in the hub chart
  (`A2ATargetPolicy`) mapping hub-accepted groups (e.g.
  `keycloak:acme-agent-rca`) or principals to allowed `targetCluster` names;
  the policy denies any create whose target is not allowed for the caller's
  groups. Fallback if group granularity proves too coarse: the adapter stamps
  an annotation carrying the verified principal, and the webhook cross-checks
  annotation vs. request user — noting the annotation is advisory, the RBAC
  user is authoritative.
- Trade-off: a ValidatingAdmissionPolicy is declarative and cheap but CEL
  cannot call out to anything; a dedicated webhook server can consult richer
  state but is another running component. Start with CEL +
  paramKind (mirroring repo C's suspension policy's `paramKind` pattern,
  `agenticrun-suspension-policy.yaml:10-12`); escalate to a webhook server
  only if the mapping needs per-request lookups.

---

## 5. Run-token Secret lifecycle

This is the plan's "Run-token Secret" requirement block (plan `:634-639`),
item by item, against repo C's implemented contract
(`docs/gateway-credential-mode.md`, `controller/agenticrun/gateway.go`).

### 5.1 Naming, key, labels (repo C's contract, verbatim)

- Name: `ls-run-token-{run UID}` (`gateway.go:35,67-69`).
- Key: `token-a` (`gateway.go:38`).
- Labels: `hub.openshift.io/agentic-run: {run UID}` and
  `hub.openshift.io/spoke-cluster: {target cluster}` (`gateway.go:117-130`,
  label constants at `spoke.go:53-54`). Repo C validates all three fail-closed
  before creating the sandbox pod (`gateway.go:100-132`) — the adapter must
  write exactly this shape or the run pauses.
- Namespace: the run's namespace (repo C fetches it in `r.Namespace`, the
  operator namespace — `credential_pause.go:80`; sandbox pods run in the
  operator namespace per repo C `ARCHITECTURE.md:114`). Confirm against live
  Phase 0 findings whether run CRs and Secrets share the namespace in this
  branch; the adapter writes wherever the run CR lives.

### 5.2 Writer/reader RBAC with namespace isolation

No wildcard `resourceNames` prefix exists in RBAC (plan `:634-635`): you
cannot grant "secrets named `ls-run-token-*`". Therefore:

- The adapter's SA gets a Role (not ClusterRole) in the run namespace(s):
  `create`, `get`, `update`, `delete` on `secrets` **scoped by
  `resourceNames`** listing exactly the `ls-run-token-{runUID}` names for
  *live* runs it owns. Since new UIDs appear continuously, a static list is
  impossible — so the practical shape is:
  - a Role granting secret create/get/update/delete **in the dedicated
    adapter/run namespace only** (namespace isolation is the real boundary:
    no other Secrets live there), and
  - the admission policy (§4.3) additionally restricting which principals
    may create runs in that namespace.
- Readers: **only** repo C's operator SA needs to read these Secrets (it
  fetches by name, `gateway.go:101-108`). Nobody else gets any access. The
  sandbox pod mounts the Secret read-only via the kubelet projection
  (`gateway.go:172-200`) — that is a mount, not an API read, and needs no
  RBAC.
- The adapter SA gets **no** secrets access anywhere else, and no
  `create` on arbitrary namespaces' secrets.

### 5.3 Immutable creator/target binding

Enforced twice: (a) the adapter writes the labels from the *run CR's* UID and
`spec.targetCluster` (never from the inbound message, which could lie);
(b) repo C independently rejects a Secret whose labels don't match the run
(`gateway.go:117-130`). A renewal rewrite (§6) updates `token-a` **without**
touching labels or any other key — if the target or run changed, that is a
different run and a different Secret.

### 5.4 Missing credential → CredentialsExpired pause

Repo C implements the pause: missing/mislabeled/rejected Secret →
`CredentialsExpired=True` condition (reason `CredentialsExpired`), no pod,
step condition stays Unknown (`credential_pause.go:30-46`;
`agenticrun_types.go:57-62,266-278`). The adapter's duty is only to *cause*
the Secret to exist in time and to react to the condition (§6). Deliberate
sequencing note: the adapter should write the Secret **before or immediately
after** creating the run CR (it has A in hand at message time), so the common
path never pauses at all — the pause path exists for expiry, not for
first-write lag.

### 5.5 Terminal-run cleanup

Owner-ref GC is **not** cleanup for retained runs (plan `:636-638`): a
run-token Secret with an owner ref to the AgenticRun is deleted when the run
CR is deleted, but a *retained* terminal run (TTL not yet elapsed,
`status.deleteAfter`, `agenticrun_types.go:440-445`) keeps its Secret — and
that Secret still contains a Token A that expired long ago. Requirement: the
adapter's controller, on observing a run transition to a terminal phase
(Completed/Failed/Denied/Escalated/EmergencyStopped, derivable via
`DerivePhase`, `agenticrun_types.go:68-137`), **deletes** (not blanks —
deletes, so the name can't be re-resolved) `ls-run-token-{runUID}` once no
step can retry. Repo C's mode-flip behavior (`gateway-credential-mode.md:80-83`)
shows a deleted Secret in gateway mode simply re-pauses, never corrupts — so
deleting early-at-terminal is safe even if a human later triggers a revision
re-analysis: the adapter re-writes the Secret from the *next* valid
follow-up's A, through the same renewal path (§6). Edge: revision feedback on
a terminal analysis re-opens the run (`agenticrun_types.go:429-438`) — the
adapter must treat a terminal→non-terminal transition as "park new A on the
next message", not auto-resurrect an old one.

### 5.6 Denial of untrusted credential references; ownership validation

- The adapter is the **only** writer it trusts: it never *reads* a
  run-token Secret to make decisions (the token never round-trips through the
  adapter's logs or responses). If a Secret exists that the adapter did not
  write (unexpected resourceVersion ancestry, labels present but creator
  annotation missing — the adapter adds a
  `a2a.lightspeed.io/written-by: <adapter SA name>` annotation for
  provenance), the adapter overwrites it rather than consuming it.
- **Ownership validation on every A2A get/stream/resume/cancel** (plan
  `:638-639`): each operation first checks that the task's bound principal
  (context/task record, and the CR's principal annotation) matches the
  request's verified principal (the validated sub of the inbound token at
  Praxis — for renewed calls, the *new* token's sub, §6). Mismatch → A2A
  `TaskNotFound` (not `AccessDenied` — do not confirm task existence to a
  non-owner; matches MAS's scoping behavior, `AUTHENTICATION.md:340-342`).
  Target is immutable at the CR (CEL), so re-validating target on resume
  means: the resume's inbound target header, if present, must equal the
  bound target — a mismatched header on a resume is rejected, not
  OR-matched (§4.2).

---

## 6. Renewal bridge (sandbox ↔ MAS)

This implements the plan's "B1 still needs a sandbox-to-task renewal bridge"
requirement (plan `:864-877`) using the repo C condition contract as the
hub-side signal and the **existing** MAS↔ACME renewal flow as the
caller-side mechanism (plan `:393-407`: worker opens a Mongo renewal mailbox
request, projects `AUTH_REQUIRED` on the MAS task; ACME supplies fresh A on
the same MAS task/context with a new message ID; MAS's downstream provider
sends the credential-only follow-up on the same *downstream* task — i.e., on
**this adapter's** task).

### 6.1 The bridge, step by step

1. **Watch**: the adapter watches AgenticRuns for
   `CredentialsExpired=True` (reason `CredentialsExpired`) — repo C sets it
   when the Secret is missing/mislabeled or the token was rejected/expired
   (`gateway.go:57-62`, `credential_pause.go:30-46`).
2. **Surface**: for the run's bound A2A task, transition the A2A task to
   `AUTH_REQUIRED` **on the existing task** — new task state, not a new task,
   not a new MAS turn (plan `:421-435`). The adapter's streaming subscribers
   get a task-status update artifact describing the credential pause.
3. **Accept**: ACME's follow-up arrives on the same task/context with a new
   `messageId` and fresh Authorization (Token A′). Praxis re-validates A′ and
   performs A′→H′ for the resumed inbound call (plan `:426-428`).
4. **Validate ownership + immutability**: same principal (sub), same context,
   same immutable target (§5.6). Renewal does **not** grant permission to
   revive terminal/cancelled tasks or bypass approvals (plan `:446-448`).
5. **Rewrite**: update `ls-run-token-{runUID}` key `token-a` with A′ —
   labels untouched (§5.3).
6. **Resume**: repo C detects the Secret's changed `resourceVersion`
   (embedded in the pause condition message, `gateway.go:51-54,216-236`) and
   re-attempts the paused step on its next poll — worst case one
   `credentialPauseRequeueAfter` = 30 s (`credential_pause.go:21`) plus
   informer resync after an operator restart (`gateway-credential-mode.md:67-71`).
   The adapter does nothing to trigger resume beyond the Secret write.
7. **Return**: the task's status update continues projecting the run's
   conditions as normal (§7).

Note the trust subtlety repo C built in: only condition messages the
controller itself wrote (parseable observed resourceVersion) get the RV
comparison; a foreign condition message resumes as soon as the Secret
validates (`gateway-credential-mode.md:73-78`, `credential_pause.go:50-54`).
The adapter never writes conditions — it only writes the Secret — so it never
interacts with that rule. Keep it that way.

### 6.2 Pre-start expiry check (parked A is often already dead)

Token A is minted at request time; the sandbox may start hours later behind
an approval, so the parked A is frequently **already expired at sandbox
start** (plan `:868-871`, Round-5 finding 5 at plan `:1008-1012`). Therefore
the adapter, in addition to reacting to the condition, **proactively checks
A's `exp` before writing it** (and on a timer while a run is
pre-execution/approval-pending): if stale, do not write a dead token —
immediately surface `AUTH_REQUIRED` (§6.1 steps 2-7) and wait for A′. This
converts the failure from "pod starts, first PPE call 401s, pause, round
trip" into "pause before pod creation" — fewer moving parts, and repo C's
pause machinery already supports not-yet-started runs (the pause happens
before pod creation by construction, `credential_pause.go` pauses instead of
failing the step).

### 6.3 What this bridge is NOT

- **403 ≠ renewal** (plan `:442-443`). An authorization denial (RBAC 403 from
  the spoke, surfaced through the PPE as a 403 on the sandbox's call) is a
  terminal-ish failure surfaced as task failure detail, never an
  `AUTH_REQUIRED`. Only credential-specific rejections (401 / token expired /
  CredentialsExpired condition) enter the renewal path.
- **Not a Temporal signal / workflow restart** — the plan's mailbox
  coordination model (plan `:393-407`): the run stays paused at the same
  step; completed steps are not repeated; the same AgenticRun, approvals, and
  (where the pod is still alive) the valid pod are reused (plan `:429-435`).
  If the pod has exited, resume requires repo C's own controlled
  re-attempt machinery — the adapter does not replay execution steps
  (plan `:433-435`).
- **Not a credentials resurrection for terminal runs** — freshness of A′
  does not override terminal/cancelled state (plan `:446-448`).

### 6.4 Bounded waits and deadlines

- Renewal wait bound: once `AUTH_REQUIRED` is surfaced, the adapter keeps the
  task in that state for at most a configurable window (default: aligned with
  MAS's worker-side 900 s bound and ACME's 2-renewal default — plan `:861-863`).
  After the window, the task fails with a credential-timeout reason. The
  window is per-pause, but the *total* pauses per run are capped (default 3,
  configurable) so a flapping credential can't pause-loop forever.
- Stream deadlines (§2.3) apply independently: a client that disconnects
  during a pause can reattach; the run itself is unaffected (the pause lives
  in the CR, not the connection).

---

## 7. Status/result flow-back

### 7.1 Watching

The adapter watches (informer-based) in the run namespace:

- **AgenticRun** status: conditions (Analyzed/Executed/Verified/Denied/
  Escalated/EmergencyStopped/Stopping/CredentialsExpired) and
  `status.steps[]` (per-step timing, sandbox info, Result references —
  `agenticrun_types.go:404-446`).
- **Result CRs** (`AnalysisResult`, `ExecutionResult`, `VerificationResult`,
  `EscalationResult` — `api/v1alpha1/`, each with Started/Completed
  conditions, `result_types.go:26-36`) referenced from `status.steps`.
- **AgenticRunApproval** (approval gates, repo C `ARCHITECTURE.md:76-84`):
  when a step waits on a manual approval, the adapter surfaces the A2A task
  as `INPUT_REQUIRED` — the exact pattern MAS already implements for its own
  HITL (structured decision DataPart `approve`/`reject`/`modify`/`redirect`,
  reply on the same task with a new messageId —
  `AUTHENTICATION.md:467-478`). The adapter forwards the decision to the
  AgenticRunApproval CR (the patch shape is repo C's; the fields are in
  `agenticrunapproval_types.go`).

### 7.2 Mapping table

| AgenticRun phase (derived, `agenticrun_types.go:68-137`) | A2A task state |
| --- | --- |
| Pending | `submitted` → `working` |
| Analyzing / Executing / Verifying | `working` (with per-step progress artifacts) |
| Proposed (analysis done, awaiting approval) | `input-required` (approval decision requested) |
| CredentialsExpired=True (any phase) | `working` + `AUTH_REQUIRED` status message (§6) |
| Completed | `completed`, artifacts from Result CRs |
| Failed | `failed`, with step failure reason/message |
| Denied | `failed` with denial reason (approval rejected) |
| Escalated / EmergencyStopped | terminal states with their reasons; `tasks/cancel` during Stopping → `canceled` |

Artifacts: each completed step's Result CR payload becomes an A2A artifact
(text summary + structured DataPart for the analysis options schema —
`AnalysisOutput`, `agenticrun_types.go:192-217`). Artifact IDs are derived
from the Result CR's UID so they are stable across adapter restarts.

### 7.3 Lifecycle contract items (plan `:617-619`), restated as acceptance

- Protocol version: A2A spec version negotiation is the SDK's job; the
  adapter pins one SDK version per image and the agent card reflects it.
- Task/context mapping: §2.2.
- Retries/idempotency: duplicate messageId and uncertain-start retry must not
  create a second run (acceptance item, plan `:450-455`).
- Restart recovery: §2.3 (all state reconstructible from CRs + annotations).
- Approval waiting: §7.1.
- Cancellation: §2.3.
- Result retention: §2.3.
- Renewal must not create a new MAS session/turn, downstream task, or
  AgenticRun; completed steps not repeated (plan `:450-455`).

---

## 8. Deployment story

### 8.1 New chart: `charts/all/lightspeed-hub`

Contents (all net-new; registered in the clusterGroup and parameterized in
`variants/standalone/values-standalone.yaml` — the only place
cluster-specific values go, per `docs/ARCHITECTURE.md:52-57`):

1. **Adapter Deployment** (the Go service from this doc) + internal Service
   + NetworkPolicy admitting only Praxis.
2. **Adapter RBAC**: dedicated SA with the exact grants from §5.2 (secrets in
   the run namespace; agenticruns get/list/watch/patch-status in the run
   namespace; agenticrunapprovals get/list/patch; result CRs get/list/watch;
   no cluster-admin anywhere — plan `:654-656`). Creating RBAC objects is
   insufficient by itself; the chart's README must document the escalation
   checks (`oc auth can-i` probes) run in CI.
3. **The target-authorization admission policy/webhook** (§4.3).
4. **HubConfig + SpokeCluster wiring** for repo B's operator (its CRDs, its
   Deployment/RBAC via its own kustomize `make deploy` rendered into the
   chart, or its image pinned and deployed directly — repo B has no Helm
   chart, plan `:195-196`). Note repo B's `adapter_deployment.go` is a
   factory for an *external* alerts-adapter image (plan `:191-193`) — do not
   confuse that with this adapter; ours is a separate deployment entirely.
5. **The repo C stack** (CRDs, operator Deployment, configuration ConfigMap
   with `sandbox-credential-mode: gateway`, `gateway-server-url-template`,
   `gateway-ca-secret` — repo C `pkg/configuration/config.go:225-246` fails
   loudly without all three). **Phase 0 finding: repo C is self-contained for
   this purpose — it carries its own ValidatingAdmissionPolicy and has no
   `lightspeed-operator` dependency in its own module/config** (verified:
   the only OLSConfig references in repo C's config/ are its own CRD bases
   and admission policy). The plan's worry that repo B's README lists
   lightspeed-operator as a prerequisite (plan `:194-196,311-315`) applies to
   repo B's full feature set, not to the AgenticRun execution path this
   design uses — record this in the Phase 0 notes and re-verify at install
   time.
6. **Spoke-facing PPE wiring**: consumed from the concurrent praxis-proxy
   work (per-spoke gateway Services, serving CA Secret named by
   `gateway-ca-secret`, URL template matching `gateway-server-url-template`)
   — owned by the praxis implementer, referenced by this chart's values.
   Coordination note: the praxis-proxy chart now ships a ConfigMap→Secret
   sync bridge for that serving CA (its `spokeGatewayConfig.servingCA`
   values + `spoke-ca-sync-*.yaml` templates copy the inject-cabundle
   bundle into the Secret/key repo C's `gatewayCASecret` expects, kept
   current across service-CA rotation) — this chart's `gateway-ca-secret`
   value must name that Secret, in the operator namespace.

### 8.2 Image story

- The adapter gets its **own image**, own build pipeline, own immutable pin
  (digest or exact tag, quoted — the all-digit-tag/scientific-notation trap,
  `docs/ARCHITECTURE.md:300-309`). Separate build artifacts/entrypoints from
  repo B and repo C images: the adapter changes on A2A/auth cadence; repo C
  changes on sandbox/execution cadence; pinning them together would force
  lockstep upgrades (plan `:649-652`).
- Repo C image: pin the `a2a-support` build; rollback image = the upstream
  `spoke-sa`-mode image (byte-identical behavior when the new ConfigMap keys
  are absent — `gateway-credential-mode.md:43-45` — so rollback is: flip the
  ConfigMap back and swap the image).
- Repo B image: pinned per its Konflux build; unchanged by this work.

### 8.3 Namespace plan

Adapter and (per repo C `ARCHITECTURE.md:114`) sandbox pods in the repo C
operator namespace; AgenticRuns created there too (repo C fetches the
run-token Secret in `r.Namespace`, `credential_pause.go:80`), so the Secret
writer/reader share one namespace and the RBAC isolation of §5.2 is exact.
Callers never get any namespace access there — their only path in is A2A via
Praxis.

---

## 9. Traceability

- **Hub-side: per-authenticated-principal.** Every A2A task binds the
  verified principal (validated `sub` + issuer) at creation; the CR carries
  it as an annotation (§4.1); ownership checks on every operation (§5.6).
  Until human delegation lands (Keycloak 26.8 `act`/`may_act`, plan `:558`,
  `:1045-1047`), "principal" means the authenticated caller — for now the
  ACME service-account principal, not the human behind the chat
  (`AUTHENTICATION.md:323-330`). Audit lines carry token fingerprints
  (`fp:…`), never token values — the pattern ACME already uses
  (`docs/ARCHITECTURE.md:177-187`).
- **Spoke-side: none in Phase 1** (plan `:658-659`). The spoke sees Token U
  minted by the PPE from A; U carries the mapped principal's groups, so
  spoke RBAC decisions *are* attributable, but spoke-side audit cannot join
  U back to the hub principal chain until B1's U carries the principal
  explicitly (the PPE's exchange preserves `sub` per the plan's exchange
  semantics, plan `:903-905`, so this is a query/joinability limitation, not
  an attribution fabrication).
- Request-id joins do **not** cross MAS (live finding,
  `docs/ARCHITECTURE.md:382-391`): trace the orchestrated flow by time
  window or Keycloak Realm Events. The adapter mints its own request id per
  inbound A2A call and logs it with the task/run IDs — expected, and
  documented here so nobody burns an afternoon looking for ACME's
  `X-Request-Id` inside the hub.

---

## 10. Open questions / gates

**Q1 — Praxis kube-API-proxy pass-through spike (the B1 gating unknown).**
Can the pinned Praxis act as a pass-through kube API proxy — watch/streaming
(`GET /api/v1/...?watch=true`), exec/websocket upgrades, arbitrary paths?
The router is built for tool/A2A calls (plan `:700-703`). Acceptance list for
the spike is the plan's (plan `:818-822`): two concurrent runs without editing
Praxis, full discovery/API paths, list/watch/log streaming, websocket/exec
upgrades (or explicitly disabling those tools), CA/SNI failures, wrong
audience, no-group A → group-bearing H/U, 200 vs 403, direct-API bypass and
cross-target denials, token replacement mid-run, cleanup. **Re-assessment
note:** the recorded fallback, option A ("adapter-brokered Token U + minimal
repo C patch"), was written against repo C *before* `a2a-support`; repo C has
since implemented the B1 shape itself (gateway credential mode). Option A as
specified is **moot** — the minimal patch it describes no longer corresponds
to repo C's credential path. If the spike fails, the real fallback design
must be re-derived against the current repo C (likely: adapter or PPE-side
variant keeping the `ls-run-token` contract but changing who mints U), not
resurrected from plan v3.5. **This is the top gating item; M1-M2 can proceed
without it, M3's PPE-dependent tests cannot.**

**Q2 — PPE version gate.** Pinned image = praxis-policy **0.3.1**; recipes
doc is 0.4.x. `role: client` is empirically broken on ours (documented in
`charts/all/praxis-proxy/files/policy.yaml:18-36`); the `apl:` wrapper is a
load error in theirs; whether the chosen bumped image ships `delegator/oauth`
must be confirmed (plan `:537-548`). Sequence: bump/verify image → re-test
`role: client` → only then rely on delegation recipes. Blocks: Praxis-side
A→H exchange (§3.2's preferred path) and any PPE-side policy beyond the
current `role: user` workaround.

**Q3 — Protected-A handoff mechanism on the pinned Praxis.** Can the pinned
Praxis perform exchange *and* retain/forward A on a confidential side channel
to the adapter (plan `:499-509`)? If not — expected outcome given Q2 — the
adapter performs the first exchange itself (fallback, §3.2). Decision gate
before M1 implementation chooses which side owns the Keycloak exchange
client. Trade-off already recorded (plan `:879-887`): adapter-side exchange
concentrates credential-broker responsibility (Keycloak client creds, secret
writes) in code we own, and its availability gates sandbox starts — accepted.

**Q4 — Run namespace and multi-tenancy.** Repo C runs sandboxes in the
operator namespace (repo C `ARCHITECTURE.md:114`) but AgenticRuns are
namespaced and the examples show workload namespaces
(`agenticrun_types.go:479-499`). Confirm on a live Phase 0 install where the
adapter should create runs (and therefore where §5.2's Role lands). If
callers get distinct namespaces later, the admission policy (§4.3) and the
Role scoping both need the mapping.

**Q5 — Whether Praxis response-validation handles external→local subject
mapping.** The plan's acceptance gates require verifying proxy
response-validation compatibility with external→local subject mapping
(plan `:31-32`). Unverified. Affects the external-SSO input row of the auth
table only (§3, future input); does not gate ACME-token Phase 1.

**Q6 — Cancel semantics.** Does `tasks/cancel` map to repo C's
EmergencyStopped path (with its RBAC/audit weight) or is there a gentler
stop? The inspected types expose `Stopping` + `EmergencyStopped`
(`agenticrun_types.go:262-265`); the reconciler has `suspension_cleanup.go`
suggesting a suspension flow. Phase 0/implementation must read
`suspension_cleanup.go` + `pod_handler.go` before choosing. Default
assumption: emergency-stop, surfaced honestly in the A2A response.

**Q7 — AgenticRunApproval patch shape for A2A-driven approvals.** The
structured `approve/reject/modify/redirect` DataPart must map onto
AgenticRunApproval's fields (`agenticrunapproval_types.go`) — exact field
names TBD at implementation; the dual approval model (ApprovalPolicy OR
per-run approval, repo C `ARCHITECTURE.md:152`) decides which.

**Q8 — A2A SDK maturity in Go.** If the SDK's server lacks
`resubscribe`/streaming semantics we need, decide early: shim vs port.
Discovery spike in M2's first week.

---

## 11. Effort / sequencing estimate

Coarse, dependency-ordered. "Can build before spikes resolve" marks work
that does not depend on Q1-Q3.

| Milestone | Contents | Depends on | Buildable before spikes resolve? |
| --- | --- | --- | --- |
| **M1 — auth contract + exchange** | Keycloak client/mappers for A→H (with keycloak-oidc implementer); adapter-side exchange port (Go `KeycloakTokenExchanger`); protected-A handoff interface (both placements behind one interface); hub `Authentication` audience/group wiring | Q3 decides placement; Q2 gates the Praxis-side path — **build the adapter-fallback side first** | Adapter-fallback exchange: yes. Praxis-side: no |
| **M2 — A2A server + CR mapping** | A2A server (SDK), agent card, context/task/message records, deterministic run naming, message→CR mapping, target-header rules (§4.2), pre-create A-expiry check (§6.2 write-side) | M1 (needs a validated principal); Q8 spike | Yes (against a fake issuer/test realm) |
| **M3 — status + renewal bridge** | AgenticRun/Result/Approval watches, phase mapping (§7), CredentialsExpired → `AUTH_REQUIRED` → rewrite → resume (§6), pause bounds, terminal-run Secret cleanup (§5.5) | M2; repo C `a2a-support` deployed; **PPE-dependent end-to-end tests need Q1** | Bridge logic + unit/integration vs a stubbed condition-writer: yes. Live PPE path: no |
| **M4 — webhook + chart** | `charts/all/lightspeed-hub` (adapter Deployment/RBAC/SA, admission policy, repo B + repo C stack, ConfigMap with gateway keys), values-standalone entries, image pins | M1-M3 shapes; Q4 | Yes (chart skeleton + admission policy), with placeholders for spike outputs |
| **M5 — integration tests** | The plan's acceptance list (plan `:450-455`, `:818-822`): duplicate message/uncertain start; renewal not creating new runs/turns; expiry before sandbox creation and mid-execution; wrong-owner/target renewal; deadline exhaustion; restart recovery; concurrent runs; bypass/cross-target denials; live Temporal-less hub tests kept separate from existing scripted MAS tests (plan `:455`) | All of the above; Q1 for the full spoke-leg matrix | Partially (CR-level tests yes; spoke-leg no) |

Dependency spine: **M1 → M2 → M3 → (M4, M5)**. The Praxis kube-API-proxy
spike (Q1) and the PPE version gate (Q2) should start immediately and *in
parallel* with M1 — they gate the integration tail, not the design's core,
and everything in M2 plus most of M3 is testable against a stubbed repo C
(the `CredentialsExpired` condition contract is fully specified in repo C's
code and docs, so a fake controller-writing test harness is cheap).

**Blueprint re-authoring (MAS-blueprint side — explicit owner, milestone,
trigger; not left implicit in Phase-1 scope, plan `:641-642`).** §4.2
assumes a re-authored MAS orchestrator blueprint (target carried as a tool
argument value, deterministic header mapping — never model-constructed
header names). Owner: the pattern's MAS integration (the
`charts/all/mao/mas-a2a` blueprint authors), delivered as a re-authored
persisted blueprint plus a pinned `a2a.blueprintId` cutover in
`variants/standalone/values-standalone.yaml` (the same mechanism
`charts/all/mao/mas-a2a/README.md` already requires: `a2a.blueprintId` must
name an actual persisted orchestrator blueprint). Milestone: it sits on the
**M2/M4 boundary** — it is *not* part of M2's adapter code, but it MUST
land before M4's chart/values cutover points live MAS callers at the
adapter (M4's values-standalone entries include the new pinned
blueprintId). Trigger: M2's agent-card + target-header rules (§4.2) being
merged — the blueprint encodes exactly those rules, so it is authored
against the merged contract, then pinned at M4. It is not descoped:
without it the MAS planning LLM keeps constructing header names itself,
which is §4.2's original failure mode.

Concrete next steps, in order:

1. Stand up the Q1 spike (Praxis pass-through of watch/exec/websocket against
   a throw-away kube API) — the longest-lead unknown.
2. Confirm Q4 (run namespace) on a scratch install of repo C `a2a-support`;
   record in the Phase 0 notes.
3. M1 adapter-fallback exchange (Go port, test realm) — no spike dependency.
4. M2 skeleton with the deterministic naming and header rules; A2A SDK spike
   (Q8) in week one.
5. Re-assess the plan's option-A fallback text against current repo C (Q1
   note above) and update `PLAN-lightspeed-hub-migration.md` so the next
   reader isn't designing against a moot fallback.
