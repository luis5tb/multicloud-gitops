# Gateway credential mode with audience-scoped token exchange

**Status:** the read-only gateway chain is partly implemented and validated
live on the standalone cluster (2026-11). The execution split below is
**[implemented]**, but has **not** been verified live; its rollout remains
disabled in the standalone variant. Each section is marked **[live]**
(implemented and verified), **[implemented]** (in code, not yet exercised
live) or **[proposed]**.

**Audience:** the lightspeed-agentic-operator and lightspeed-hub maintainers.
The doc is self-contained so it can be proposed upstream; references to this
repo (`multicloud-gitops`) show one working deployment of it.

**Builds on:** [`LIGHTSPEED_HUB_IDENTITY_DESIGN.md`](LIGHTSPEED_HUB_IDENTITY_DESIGN.md)
Option B ("token exchange instead of SA tokens") and its B-full variant. This
document is Option B made concrete. It adds what live testing showed is needed:
audience clients, role-scoped `groups`, and dropping spoke management in
gateway mode.

**Diagrams:** [`agentic-architecture-overview.html`](agentic-architecture-overview.html)
(components and token flows) and
[`agentic-e2e-token-flow.html`](agentic-e2e-token-flow.html) (the chain in s2,
step by step). Per-hop reference for this deployment:
[`agents/AUTHENTICATION.md`](../agents/AUTHENTICATION.md). Presentation:
[`lightspeed-hub-architecture-deck.html`](lightspeed-hub-architecture-deck.html).

---

## 0. Who owns what

Three components, three owners. Each section below is tagged with its owner.

| Owner | Component | Responsibility in this design |
|---|---|---|
| **lightspeed-agentic-operator** | the operator that runs AgenticRuns in sandbox pods | gateway credential mode: render each sandbox kubeconfig for the appropriate spoke gateway and project only its step's credential (`token-a` for Analysis/Verification; `token-execution` for Execution when configured); validate the selected key; pause on expired credentials; **do no spoke management in gateway mode** (s3). |
| **lightspeed-hub** | `a2a-adapter` (A2A front for AgenticRuns) | receive **Token H** (`--inbound-hub-token`) and create the AgenticRun *as the authenticated caller principal* with it; exchange H -> read token R (`--run-token-audience`) and, after Execution approval, H -> R_exec (`--execution-token-audience`); write/renew run credentials and patch the approval as the caller; serve the agent card at `/a2a/.well-known/agent-card.json` too |
| **lightspeed-hub** | hub operator (`SpokeCluster` registration) | spoke registry and probes. In gateway mode it needs no standing admin kubeconfig and no spoke-side provisioning (`openshift-lightspeed-managed`, reader bindings); registration becomes "URL + trust" (s6, B-full) |
| **deployment** (here: `multicloud-gitops`) | Keycloak realm, Praxis gateways, cluster OIDC + RBAC | exchanger clients and audience-scoped roles (s4); the **praxis hub gateway** (main `praxis-proxy`, MAS -> adapter) doing A -> H; per-spoke read and optional execution **Praxis gateways** doing R -> U or R_exec -> U_exec per call; OIDC claim mappings and RBAC bindings to the role groups (s5) |
| **MAS** | orchestrator | currently forwards the inbound token; the hub node uses `hitl_mode: ask` and its outbound HITL gate relays decisions on the paused remote A2A task (s7) |

The operator never talks to Keycloak and never sees H or U: it only mounts
the stored run credential and points the sandbox at the spoke gateway. The
adapter never sees Token A and never touches the spoke. Naming: the **front
door** is the MAS A2A endpoint; the Praxis instances are the **hub gateway**
(between MAS and the adapter) and the **spoke gateways** (between the sandbox
and each spoke API). Keycloak and RBAC configuration is deployment-owned, but its *shape*
(s4.3, s5.2) is the contract the two upstream components rely on.

## 1. Goal

An agent investigating a cluster acts as the **authenticated caller principal**,
with that principal's RBAC on the target cluster, and nothing more. In ACME's
current `client_credentials` flow this is the ACME service account, not the
person at the UI; a per-human boundary requires a per-user token flow (§3.3).

- no standing admin credential per spoke on the hub;
- no operator-minted ServiceAccount whose permissions are decided by the
  operator rather than the cluster's own RBAC;
- each token in the chain authorizes **only its own hop**, even when two hops
  land on the same Kubernetes API server (the hub acting as its own spoke).

## 2. Actors and tokens

```
ACME --A--> MAS (front door) --A--> praxis hub gateway --H--> hub A2A adapter --H--> hub kube API
                                      (exchanges A->H)          | exchanges H->R, stores R (run-token Secret)
                                                                v
   Analysis/Verification sandbox --R--> praxis read gateway --U--> spoke kube API
       Execution sandbox --R_exec--> praxis exec gateway --U_exec--> spoke kube API
                                   (R -> U and R_exec -> U_exec per call)
   ACME <-- approval checkpoint via MAS HITL <-- adapter (input-required; H patches decision)
```

**Principle: each token's audience names the client that exchanges or
consumes it next.** So every exchange requests a single audience (the
pinned Praxis `delegate()` takes exactly one), and Keycloak's rule that the
exchanging client must be in the subject token's `aud` (s4.2) holds by
construction.

| Token | Minted by (client) | `aud` | Lifespan | Exchanged / consumed by | Purpose |
|---|---|---|---|---|---|
| **A** | Keycloak for the caller (`acme-agent`) | `mas-a2a` | 300 s | MAS; the hub gateway exchanges it **as client `mas-a2a`** | caller identity, only as far as the hub gateway |
| **H** | hub gateway (`mas-a2a`) | `lightspeed-hub-a2a` | 300 s | **hub** kube API (create the AgenticRun); the adapter exchanges it **as client `lightspeed-hub-a2a`** | run creation as the authenticated caller principal |
| **R** | adapter (`lightspeed-hub-a2a`) | `praxis-spoke-gateway` | **1800 s** | only the spoke gateway, which exchanges it **as client `praxis-spoke-gateway`** | the run's stored credential, covering a whole step |
| **U** | spoke gateway (`praxis-spoke-gateway`), per call | `praxis-spoke-<id>` | 300 s (per call) | **spoke** kube API | the investigation's reads as the same caller principal |
| **R_exec** [implemented; not live-verified] | adapter (`lightspeed-hub-a2a`), after Execution approval | `praxis-spoke-gateway-exec` | configured on `lightspeed-hub-a2a` | only the execution gateway, which exchanges it **as client `praxis-spoke-gateway-exec`** | Execution-only run credential |
| **U_exec** [implemented; not live-verified] | execution gateway (`praxis-spoke-gateway-exec`), per call | `praxis-spoke-<id>` | 300 s (per call) | **spoke** kube API | approved writes, plus reads when the principal also has the ordinary reader role |

Token exchange is configured to preserve the same `sub` across all tokens;
the live replay confirms this for A/H/R/U. The R_exec/U_exec subject and claim
set remain part of the live-verification checklist (§9). Kubernetes sees the
same username (`keycloak:<sub>`) for H and U (and is intended to see it for
U_exec), which is why section 5 is needed. R is rejected by every kube API
(verified live: 401), and so is Token A.

Verified live (2026-11-20), one fresh Token A replayed through all three
exchanges exactly as each hop performs them (Praxis hops with HTTP Basic,
the adapter with form auth):

```
H by mas-a2a              aud lightspeed-hub-a2a    groups [lightspeed-hub-a2a:agenticrun-creator]  300s
R by lightspeed-hub-a2a   aud praxis-spoke-gateway  groups none                                     1800s
U by praxis-spoke-gateway aud praxis-spoke-hub      groups [praxis-spoke-hub:reader]                300s
kube API: H -> keycloak:lightspeed-hub-a2a:agenticrun-creator   U -> keycloak:praxis-spoke-hub:reader   R -> 401
```

**Why this shape** (it replaced two earlier ones, both run live):

1. *Adapter stores Token A.* A (5 min) expired mid-investigation; the
   interim fix was a 15-minute A, replayable that long everywhere.
2. *Adapter exchanges A -> H and A -> R.* Works, but the adapter handles the
   caller's original token and A must name the adapter as an audience.
3. *Current: the hub gateway exchanges A -> H; the adapter exchanges H -> R.*
   The adapter never sees A and creates the run with what it receives; ACME
   requests only `mas-a2a`; R is long-lived but useless anywhere except the
   spoke gateway; U is per call and short. The former "protected-A handoff"
   blocker for exchanging at Praxis disappeared because the run credential
   now derives from H, not A.

**Lifetimes (verified live):** an exchanged token gets the **exchanging
client's** `access.token.lifespan`, even past the subject token's expiry (A
900 s exchanged by a 3600 s client gave a token expiring 2702 s after A).
That is what lets R (1800 s) outlive A (300 s). Beyond R's lifetime the
renewal path applies (fresh A -> fresh H -> fresh R, s8).

## 3. Gateway credential mode in the agentic operator

*Owner: lightspeed-agentic-operator.*

### 3.1 Before (`lightspeed-agentic-operator`, branch `a2a-support`, commit `8225d31`)

`sandbox-credential-mode: gateway` (ConfigMap `lightspeed-agentic-configuration`)
rewrites only the sandbox's execution kubeconfig. It points at
`gateway-server-url-template` (`{targetCluster}` substituted), trusts
`gateway-ca-secret`, and authenticates with `tokenFile` = Token A from the
run-scoped Secret `ls-run-token-<run UID>` that the A2A adapter writes. See
the operator's `docs/gateway-credential-mode.md`.

**Spoke management is still done in gateway mode.** For every spoke-run
step the operator:

1. reads the standing `spoke-kubeconfig-<targetCluster>` Secret (an
   admin-capable credential for the spoke);
2. creates a per-step ServiceAccount in `openshift-lightspeed-managed` on the
   spoke;
3. creates per-run ClusterRoleBindings giving that ServiceAccount the roles
   of `lightspeed-hub:cluster-reader` / `lightspeed-hub:cluster-monitoring-view`;
4. skips only the TokenRequest (`ensureSA(..., mintToken=false)`).

So in gateway mode steps 1–3 produce a ServiceAccount and bindings that
**nothing ever uses**. They exist only because the spoke-sa code path was kept.
They still require a standing admin credential, a spoke-side namespace and
reader bindings provisioned by the lightspeed-hub hub operator. And every run
leaves behind a dormant ServiceAccount with read privileges.

**How this was found:** live, 2026-11-20. Once runs got past admission, the
operator's failure cleanup logged `get spoke kubeconfig Secret
spoke-kubeconfig-hub: not found`, although the sandbox would never use
anything it creates.

### 3.2 Change [implemented] (`a2a-support`, follow-up to `8225d31`)

In gateway mode the operator **skips spoke management entirely**: no
`spoke-kubeconfig` lookup, no per-step ServiceAccount (spoke or hub), no
per-run reader ClusterRoleBindings, no execution-RBAC materialization, and no
matching cleanup. Spoke-sa mode is unchanged. Implemented as
`spokeManagementAccess` / `isSpokeRun` in `controller/agenticrun/gateway.go`,
used by `Create`, the create-failure cleanup, `ReleaseSandbox(es)` and
`handleFailed`. Covered by `TestCreate_GatewaySpokeRun` (no spoke client, no
kubeconfig Secret, no SA) and `TestRelease_GatewaySpokeRun_NoSpokeKubeconfig`.
The operator's `docs/gateway-credential-mode.md` documents the contract.

Implementation note: gateway mode used to be *detected* as
`spoke != nil && gatewayCredentialMode(cfg)` (`sandbox_manager.go`), where
`spoke` is the result of `spokeAccessForRun` (which loads the kubeconfig). Two
notions need separating:

- **is this a spoke run** (`spec.targetCluster != ""`), which drives gateway
  kubeconfig rendering and run-token validation;
- **do I need management access to the spoke** (spoke-sa mode only), which
  drives `spokeAccessForRun`, `ensureSA`, `addReaderSubjectOnSpoke` and the
  failure/terminal RBAC cleanup.

Consequences:

- `openshift-lightspeed-managed`, the `lightspeed-agent` ServiceAccount and
  the `lightspeed-hub:*` reader bindings are no longer needed on spokes in
  gateway mode. The hub operator's spoke provisioning can be skipped for
  gateway-mode spokes, leaving registration as "URL + trust" (B-full).
- **Execution stage:** the operator can use a distinct gateway URL and
  `token-execution` key for the Execution sandbox. The read-token flow remains
  the default for Analysis and Verification. The approval/elevation question
  is resolved by the scoped split in §3.3: only an approved Execution can get
  R_exec, and the target's writer group remains a separate RBAC boundary.

### 3.3 Approved Execution credential split [implemented; not live-verified]

This is implemented across the operator, hub adapter, Keycloak/Praxis charts,
and the lightspeed-hub deployment chart. It is **not live-verified**; the standalone
variant leaves every execution switch commented out. The operator contract is
documented in `lightspeed-agentic-operator/docs/gateway-credential-mode.md`.

| Step / token | Gateway and credential | Target API identity / permission |
|---|---|---|
| Analysis, Verification | `gateway-server-url-template` + `token-a` = R | read gateway exchanges R -> U; `reader` only |
| Execution after approval | `gateway-execution-server-url-template` + `token-execution` = R_exec | execution gateway exchanges R_exec -> U_exec; target `writer` role (and `reader` only if the principal also belongs to the ordinary exchange group) |

The Praxis execution gateway uses its own `app.kubernetes.io/name` label and
Deployment/Service selector; the read selector is unchanged. Its NetworkPolicy
ingress peer also requires the execution step pod label
`agentic.openshift.io/step=execution` (the read peer keeps its configured
read selector). See the [praxis-proxy chart README](../charts/all/praxis-proxy/README.md).

Each sandbox projects only its selected Secret key using an `items` projection
(never `subPath`). A missing `token-execution` pauses the run with
`CredentialsExpired`, just as a missing read token does. Execution uses a
separate gateway endpoint and audience; R is not intended to authenticate at
the execution gateway, and R_exec is not intended to authenticate at the read
gateway. Both checks still require live verification (§9).

**Keycloak shape.** The ordinary `praxis-spoke-gateway` exchanger has a
per-spoke role scope limited to `reader`. The distinct confidential,
exchange-only `praxis-spoke-gateway-exec` exchanger has the same per-spoke
audiences and scopes `reader` plus `writer`; both have `fullScopeAllowed:
false`. The adapter exchanger client `lightspeed-hub-a2a` has separate read
and execution audience mappers and exchanges H for the requested audience.
Keycloak assigns ordinary `exchangeRoles.groups` the hub `agenticrun-creator`
and spoke `reader` roles; `exchangeRoles.remediation.groups` receives the hub
`agenticrun-approver` and spoke `writer` roles. A principal that needs both
read and write roles must belong to both configured groups. The exact values
keys and one-time import caveats are in the
[`keycloak-oidc README`](../charts/all/keycloak-oidc/README.md).

**Kubernetes authorization.** The hub's namespaced `run-approvers-rbac.yaml`
Role binds `keycloak:lightspeed-hub-a2a:agenticrun-approver` to `get` and
`update` on AgenticRunApprovals; `adapter.hubRunApproversGroup` configures the
binding. The adapter submits an approval patch using H as the caller, not its
own ServiceAccount. For same-run revisions, the adapter SA updates
`revisionFeedback` only after task ownership validation and records
`a2a.lightspeed.io/revised-by`; callers have no `update` on AgenticRuns. On
each spoke, `spokeExecutorRBAC` binds the audience-scoped
`keycloak:praxis-spoke-<id>:writer` group through namespaced RoleBindings only
in the required `spokeExecutorRBAC.namespaces` allow-list; there is no
ClusterRoleBinding. The chart rejects system/pattern namespaces. This is not
full workload isolation: in an allowed namespace, a writer can run workloads
as any ServiceAccount there and mount that namespace's Secrets. Defaults
exclude Secrets and `pods/exec`, `pods/attach`, and `pods/portforward`. See the
[lightspeed-hub chart README](../charts/all/lightspeed-hub/README.md) for the
denylist, residual risk and rollout contract; remote spokes need equivalent
reviewed configuration.

**Approval and minting sequence.** The MAS hub node defaults to
`hitl_mode: ask` in `setup-mas-blueprint.sh`; Analysis completion produces
`input-required` with an `approvalRequest` DataPart and ends that A2A stream
with `Final=true`. The HITL gate relays the decision on the paused MAS task,
while ACME only forwards a reply on the same inbound ACME task that paused; a
new ACME task routes normally. Decisions, revisions and credential changes are
serialized per run; decision replay is idempotent. A revision is rejected
once Execution has either decision recorded: start a new task to request a
different remediation. On approval, the adapter records the approved
AnalysisResult UID/generation in `a2a.lightspeed.io/approved-analysis`,
exchanges the approver's current H into fresh R and R_exec, writes both Secret
keys, and only then patches AgenticRunApproval as that caller with H. Renewal
rechecks the approval and analysis identity; startup and periodic
reconciliation remove invalid `token-execution` credentials, allowing a
two-minute preparation grace unless Execution has finished or the run is
terminal. The run-token Secret has an AgenticRun ownerReference as a deletion
backstop; terminal cleanup still uses its configured grace. The adapter must
remain single-replica with `Recreate` until its per-run lock is distributed
(temporary; see the [lightspeed-hub chart README](../charts/all/lightspeed-hub/README.md)).
If the approval patch fails, it removes `token-execution`. A rejection does
not mint an execution token and, after successful analysis, the A2A task
completes with the analysis result attached.

**Why the split limits writes.** The Analysis and Verification sandboxes only
receive `token-a`/R and point at the read gateway; the read exchanger can
project only `reader`, so those steps cannot acquire the writer group. A write
therefore requires (1) membership in the configured remediation group,
(2) an explicit human approval relayed through the adapter, and (3) the
approver's H carrying the hub `agenticrun-approver` role needed to patch that
decision. Kubernetes then enforces the separate target `writer` binding on
U_exec. The execution sandbox acts as the identity in the approving H (same
`sub`); it does not execute as the adapter's ServiceAccount.

**Identity caveat.** A per-user authorization boundary requires a per-user
Token A to reach the hub gateway. With ACME's current Keycloak
`client_credentials` grant, the subject is ACME's service account, so granting
that subject remediation-group membership means **ACME can remediate**; it
does not distinguish the person at the chat UI. Do not enable that mapping as
if it were per-human approval without a per-user identity/token flow.

MAS's outbound `a2a_agent_node` maps its ApprovalGate decisions onto the hub
DataPart (`APPROVE`, `REJECT`, `MODIFY` to `revise` or approval, `REDIRECT` to
reject) and continues the same remote task. Structured option values may arrive
as protobuf floats; integral values are accepted as zero-based indexes, while
plain-text `approve option N` uses the human-facing one-based number. Its
default 300-second HITL timeout rejects the request. `setup-mas-blueprint.sh`
sets `HUB_ADAPTER_HITL_MODE=ask` by default. This upstream change is present
in the current MAS working tree but is not live-verified; see
[`agents/AUTHENTICATION.md`](../agents/AUTHENTICATION.md) for the caller path.

## 4. Keycloak

*Owner: deployment (realm config); the contract is consumed by the lightspeed-hub adapter (A->H) and the Praxis gateways (A->U).*

### 4.1 Exchange target audiences must be clients [live]

In Keycloak 26 standard token exchange, the `audience` request parameter must
be the **client ID of an existing client**. Otherwise the exchange fails with
`400 invalid_client "Audience not found"`. That reads like a bad client
secret, but isn't one.

The target audiences (`lightspeed-hub-a2a`, `praxis-spoke-<id>`) name
**resource servers**, the kube API servers, so they are modeled as
**audience-only clients**: confidential, with every flow disabled and no
service account. Nothing can log in or mint tokens as them; they exist so
the name resolves.

How this was found: live, 2026-11-20. The adapter logged `token exchange
failed kind=invalid_client`. Replaying its exact request against Keycloak
returned `Audience not found`; the same exchange without `audience`
succeeded. Multiple audiences on Token A were **not** the cause.

### 4.2 The subject token must carry the exchanger [live]

Keycloak 26 also requires the **exchanging client** to be in the subject
token's `aud`. In the current chain that is satisfied by naming each token's
audience after its next exchanger (s2): A carries `mas-a2a` (the hub
gateway's client), H carries `lightspeed-hub-a2a` (the adapter's client), R
carries `praxis-spoke-gateway`.

So Token A now carries **only `mas-a2a`**, and `mas-a2a` is a confidential
exchanger client whose secret the hub gateway holds: the hop acting for MAS
on its outbound delegation swaps the audience. When MAS can exchange per
delegation itself, it takes over that same client and nothing changes for
the caller. Trade-off: whoever holds the `mas-a2a` secret can exchange any
token meant for MAS. (Previously Token A carried every downstream exchanger
-- `lightspeed-hub-adapter`, `praxis-spoke-gateway` -- via audience mappers
on the caller's client, because the adapter and spoke gateway exchanged A
directly.)

Also required on the caller's client: **`fullScopeAllowed: false`.** With
audience-scoped roles (s4.3) the caller's group holds client roles on the
hub/spoke audience clients, and with full scope Keycloak's default `roles`
scope ("audience resolve") added those clients to Token A's `aud` -- making
the raw Token A acceptable to the kube APIs (found live 2026-11-20; fixed,
Token A now gets 401 at the kube API).

### 4.3 Audience-scoped roles projected into `groups` [live]

**Problem.** A Keycloak group mapper writes all of a user's groups into
**every** token. H and U therefore carry the same `groups`, and Kubernetes
RBAC binds groups and users, **not audiences**. When the hub is its own spoke,
H and U hit the **same** API server: anything granted for U (cluster read) is
also usable with H, and anything granted for H (create AgenticRuns) is also
usable with U, which an LLM drives inside the sandbox.

**Design.** Each target gets **client roles** on its audience client. Groups
get those roles. Each exchanger projects **only its audience's roles** into
`groups`, with the audience as prefix:

```
group  acme-agent-rca                       members: service-account-acme-agent
  role mappings:
    lightspeed-hub-a2a / agenticrun-creator
    praxis-spoke-hub   / reader

client lightspeed-hub-adapter   (hub exchanger)
  fullScopeAllowed: false
  role scope mappings:  lightspeed-hub-a2a / *
  mapper audience:      aud += lightspeed-hub-a2a
  mapper client-role:   claim "groups", roles of client lightspeed-hub-a2a only,
                        prefix "lightspeed-hub-a2a:"

client praxis-spoke-gateway     (spoke exchanger)
  fullScopeAllowed: false
  role scope mappings:  praxis-spoke-<id> / *       (one per spoke)
  mapper audience:      aud += praxis-spoke-<id>    (one per spoke)
  mapper client-role:   claim "groups", roles of client praxis-spoke-<id> only,
                        prefix "praxis-spoke-<id>:" (one per spoke)
```

Roles say *what* the principal may do on *which* target; group membership
stays the single *who* switch. The role scope (`fullScopeAllowed: false` plus
role scope mappings) is the Keycloak-native "scope" filter. It ensures a
token can only ever carry its own audience's roles, even if more mappers are
added later.

**Why the prefix.** One spoke exchanger serves all spokes, so a U for `east`
also carries the user's `west` roles. The prefix keeps them distinct:
`east` binds only `keycloak:praxis-spoke-east:reader`, so being a west reader
grants nothing on east.

### 4.4 Example tokens

Token A (decoded live, after the hub-gateway cutover):

```json
{ "sub": "bb366787-080c-4d98-b69e-2774e15855cb", "azp": "acme-agent",
  "aud": "mas-a2a",
  "preferred_username": "service-account-acme-agent", "groups": ["acme-agent-rca"] }
```

Exchange A→H (by the hub gateway; Praxis sends the client credentials as
HTTP Basic):

```
POST /realms/rca/protocol/openid-connect/token
  Authorization: Basic base64(mas-a2a:<secret>)
  grant_type=urn:ietf:params:oauth:grant-type:token-exchange
  subject_token=<A>  subject_token_type=urn:ietf:params:oauth:token-type:access_token
  audience=lightspeed-hub-a2a
```

H→R (adapter, as `lightspeed-hub-a2a`, `audience=praxis-spoke-gateway`) and
R→U (spoke gateway, as `praxis-spoke-gateway`, `audience=praxis-spoke-<id>`)
have the same shape.

| | A | H [live] | R [live] | U [live] |
|---|---|---|---|---|
| `sub` | `bb366787-…` | same | same | same |
| `azp` | `acme-agent` | `mas-a2a` | `lightspeed-hub-a2a` | `praxis-spoke-gateway` |
| `aud` | `mas-a2a` | `lightspeed-hub-a2a` | `praxis-spoke-gateway` | `praxis-spoke-hub` |
| `groups` | `["acme-agent-rca"]` | `["lightspeed-hub-a2a:agenticrun-creator"]` | none | `["praxis-spoke-hub:reader"]` |

The `azp` column matters: an exchanged token's `azp` is the client that
exchanged it, so any `azp` allow-list after an exchange must name that
client (the spoke gateways allow-list `lightspeed-hub-a2a`; verified broken
live when they inherited `acme-agent`).

Before the hub gateway took over A→H, the adapter exchanged A itself as
client `lightspeed-hub-adapter` (since retired), and A had to carry
`lightspeed-hub-adapter` and `praxis-spoke-gateway` in `aud` as well.
Verified live (2026-11-20) with that earlier shape, same fresh Token A, both
exchanges replayed:

```
H: aud lightspeed-hub-a2a  azp lightspeed-hub-adapter  groups [lightspeed-hub-a2a:agenticrun-creator]
U: aud praxis-spoke-hub    azp praxis-spoke-gateway    groups [praxis-spoke-hub:reader]
```

and the kube API's `SelfSubjectReview` for each:

```
H -> user keycloak:bb366787-…  groups [keycloak:lightspeed-hub-a2a:agenticrun-creator, system:authenticated]
U -> user keycloak:bb366787-…  groups [keycloak:praxis-spoke-hub:reader, system:authenticated]
```

So client-role mappers on the exchanger apply to exchanged tokens, and the
role scope (`fullScopeAllowed: false` + scope mappings) does not interfere.

## 5. Kubernetes side

*Owner: deployment (each target cluster's OIDC config and RBAC).*

### 5.1 Authentication [live]

Each target cluster trusts the Keycloak issuer (OpenShift Native OIDC /
structured authentication) with its own audience, and maps claims:

```yaml
oidcProviders:
- issuer: {issuerURL: https://keycloak.apps.<base>/realms/rca,
           audiences: [openshift-cli, openshift-console, lightspeed-hub-a2a, praxis-spoke-hub]}
  claimMappings:
    username: {claim: sub, prefixPolicy: Prefix, prefix: {prefixString: "keycloak:"}}
    groups:   {claim: groups, prefix: "keycloak:"}
```

### 5.2 Authorization [implemented]

| Token | Kubernetes user | Kubernetes groups |
|---|---|---|
| H | `keycloak:<sub>` | `keycloak:lightspeed-hub-a2a:agenticrun-creator` |
| U | `keycloak:<sub>` | `keycloak:praxis-spoke-hub:reader` |
| U_exec [implemented; not live-verified] | `keycloak:<sub>` | `keycloak:praxis-spoke-hub:reader` + `keycloak:praxis-spoke-hub:writer` when the principal belongs to both configured groups |

```yaml
# hub: only H may create runs (namespace lightspeed-hub)
kind: RoleBinding
roleRef: {kind: Role, name: lightspeed-hub-run-creators}
subjects: [{kind: Group, name: "keycloak:lightspeed-hub-a2a:agenticrun-creator"}]
---
# each spoke: only that spoke's U may read
kind: ClusterRoleBinding
roleRef: {kind: ClusterRole, name: cluster-reader}        # + cluster-monitoring-view
subjects: [{kind: Group, name: "keycloak:praxis-spoke-hub:reader"}]
---
# each approved workload namespace: U_exec's writer group is bound to reviewed rules
kind: RoleBinding
metadata: {namespace: payments}
roleRef: {kind: ClusterRole, name: <reviewed-spoke-executor-role>}
subjects: [{kind: Group, name: "keycloak:praxis-spoke-hub:writer"}]
```

| Action | H | U |
|---|---|---|
| create AgenticRun in `lightspeed-hub` | allowed | **denied** |
| `get pods -n payments` | **denied** | allowed |

**Rules:**
- **Never bind anything to `keycloak:<sub>` directly.** The user is identical
  across tokens; only the role groups differ.
- `cluster-reader` excludes Secrets.
- `spokeExecutorRBAC` is separately enumerated and disabled by default; see
  its chart README for the default rules and rollout contract.
- For human users brokered from an external IdP, give them group membership
  in Keycloak, and the same roles flow through.

### 5.3 Requester × target authorization [live, then proposed]

A ValidatingAdmissionPolicy on `agenticruns` checks that the requester's
username or a group is allow-listed for `spec.targetCluster`. It re-checks on
UPDATE only when the target is set or changed, so the operator's own
finalizer and metadata updates pass. Live finding, 2026-11-20: checking every
UPDATE denied the operator's finalizer, and runs never started.

**[proposed]** Key the allow-list on per-target roles, e.g.
`lightspeed-hub-a2a:target-<id>` in H's `groups`, instead of a broad group,
so "may target spoke X" is a Keycloak role.

## 6. Multiple spokes and ACM [proposed]

*Owners: deployment (per-spoke Keycloak/OIDC/RBAC); lightspeed-hub hub operator (registration, MCE mode).*

Per spoke `<id>`, the hub needs:
- an audience client `praxis-spoke-<id>` with role `reader`;
- a role scope mapping, audience mapper and prefixed client-role mapper on the
  read exchanger and the execution exchanger (the latter includes `writer`);
- that spoke's Praxis gateway (`praxis-spoke-<id>`).

Per spoke, the spoke needs:
- OIDC trust for audience `praxis-spoke-<id>`;
- the reader ClusterRoleBindings for `keycloak:praxis-spoke-<id>:reader`.
- when approved execution is enabled, reviewed namespaced RoleBindings for
  `keycloak:praxis-spoke-<id>:writer`, only in the required
  `spokeExecutorRBAC.namespaces` allow-list (no ClusterRoleBinding).

With ACM, the spoke-side pieces are ordinary per-cluster configuration that
policies can distribute, and the hub operator's planned MCE mode (OLS-3954)
handles registration and trust. Because gateway mode no longer needs spoke
management (3.2), **no standing admin kubeconfig per spoke is required**. The
hub needs only a minimal probe identity for health checks (B-full).

## 7. Upstream split

| Change | Owner / repo | State |
|---|---|---|
| Gateway credential mode (sandbox -> gateway with the stored run credential, run-token validation, credential pause) | lightspeed-agentic-operator | implemented on fork (`8225d31`) |
| Per-step gateway/token split (`gateway-execution-server-url-template`, `token-execution` key-only projection, missing-key credential pause) | lightspeed-agentic-operator | implemented on fork; not live-verified |
| No spoke management in gateway mode (s3.2) | lightspeed-agentic-operator | implemented on fork |
| Adapter: create AgenticRun as the authenticated caller principal; run-token Secret + renewal | lightspeed-hub (`a2a-adapter`) | implemented on fork |
| Adapter: `--inbound-hub-token` (inbound bearer is H, no A->H) and `--run-token-audience` (store R = exchange(H)) | lightspeed-hub (`a2a-adapter`) | implemented on fork, unit-tested; chain verified live by replay |
| Adapter serves its agent card under `/a2a` too | lightspeed-hub (`a2a-adapter`) | implemented on fork |
| Adapter approval checkpoint, approval DataPart, H-authorized decision patch, R/R_exec minting and execution-token cleanup | lightspeed-hub (`a2a-adapter`) | implemented on fork; tests added, not live-verified |
| ApprovalPolicy stages, hub approver Role, optional spoke writer ClusterRole plus allow-listed namespaced RoleBindings, read/exec Praxis gateways and Keycloak roles | deployment (this repo) | implemented in charts; standalone rollout disabled |
| MAS outbound A2A HITL decision relay | MAS | implemented in current working tree; tests added, not live-verified |
| ACME approval UI / same-task follow-up | ACME | present in current working tree; not live-verified |
| Gateway-mode spokes need no admin kubeconfig / spoke provisioning; B-full probe identity | lightspeed-hub (hub operator) | proposed |
| Hub gateway A->H exchange (`a2aUpstream.exchangeAtPraxis`) | deployment (praxis-proxy chart) | implemented (chart) |
| Exchanger-per-audience clients, audience-scoped roles (s2, s4) | deployment (Keycloak) | live |
| RBAC bound to role groups (s5.2) | deployment (cluster RBAC) | implemented (charts) |
| Praxis delegator: form-URL-encode `client_secret_basic` | Praxis | proposed (workaround: alphanumeric secrets) |
| Per-delegation token exchange in MAS (take over the `mas-a2a` exchanger) | MAS | proposed |

## 8. Open questions

- **Runs longer than R (30 min):** the renewal path (fresh A from the caller
  -> fresh H -> fresh R) exists in the adapter but needs the caller (MAS/ACME)
  to answer `auth-required`; unverified end to end.
- **Per-target R:** R is spoke-agnostic (`aud: praxis-spoke-gateway`); a
  compromised sandbox could present it at another spoke's gateway and get a
  U there (still within the user's own rights). Hardening: per-spoke
  exchanger clients and a per-target R audience.
- **SSO callers:** the hub gateway's Praxis delegator sends no
  `subject_issuer`, so an SSO-issued Token A cannot be exchanged A->H.
  Empirically ruled out 2026-11-25 (LESSONS_LEARNED §2/§5): the jwt-typed
  alternative is rejected by this RHBK 26.4 and `claim.iss` is not in the
  Praxis attribute bag anyway, so the working exchange needs `subject_issuer`
  from a component that can send it -- adapter-side is the current candidate;
  the reverted twin-delegator attempt (`c7c6f205`) holds the routing machinery.

- **Execution identity is resolved, but live validation remains open:** the
  approved Execution gets R_exec from the approving caller's H and U_exec is
  authorized by the spoke's separate writer group (3.3). This is caller-RBAC
  elevation gated by human approval, not operator-SA elevation; with the
  current ACME client-credentials token it is ACME's service-account identity,
  not the human at the UI. Verify the complete matrix in §3.3/§9 before
  enabling.
- Token A lifetime vs long runs: the adapter's renewal bridge and
  `CredentialsExpired` pause exist, but haven't been exercised end to end.
- SSO-brokered callers on the hub leg: the adapter's exchanger does not send
  `subject_issuer`, which external-token exchange needs.

## 9. Execution rollout: live-verification checklist

The execution split is implemented but **not live-verified**. Keep the
standalone switches disabled until the coordinated rollout passes this matrix
on the deployed Keycloak 26.x, Praxis, hub adapter, and operator versions:

1. **Keycloak 26.x audience and claims:** inspect freshly exchanged R and
   R_exec. R must have `aud=praxis-spoke-gateway`; R_exec must have
   `aud=praxis-spoke-gateway-exec`; both retain the caller's `sub`. Exchange
   each at its matching gateway and confirm U/U_exec have only the intended
   per-spoke audience. Check that no unintended audience survives downscoping.
2. **Gateway isolation:** confirm R is rejected at the execution gateway and
    R_exec is rejected at the read gateway. Confirm ordinary R→U has no
   `writer` group, and R_exec→U_exec has `writer` only for a caller in the
   configured remediation group (plus `reader` only if also in the ordinary
   exchange group).
3. **Approval and identity:** before approval, no `token-execution` key is
   projected and no write is possible. An authorized caller can approve on
   the same A2A task; a caller lacking the hub approver role cannot patch the
   approval. Rejection mints no R_exec and returns the successful analysis
   artifact. Record the actual `sub`: with ACME's current client-credentials
   grant it is ACME's service-account identity, not the UI's human.
4. **Operator credential selection and cleanup:** Analysis/Verification
   project only `token-a` and use the read URL; approved Execution projects
   only `token-execution` and uses the execution URL. Verify a missing selected
   key pauses with `CredentialsExpired`, the adapter removes the execution
   key on failed approval patch and after Execution finishes, and Verification
   can still use R afterward.
5. **Target RBAC:** use a disposable target and verify the configured writer
    RoleBindings exist only in the required namespace allow-list and the
    writer role permits only reviewed remediation verbs/resources. Secrets,
   `pods/exec`, `pods/attach`, and `pods/portforward` must remain denied by
   default; verify both an allowed write and a denied out-of-scope write at
   the API server.

Capture token claims, API authorization results, approval/task events, and
cleanup observations without recording bearer-token values. Live execution
verification must not be inferred from chart rendering or unit tests.
