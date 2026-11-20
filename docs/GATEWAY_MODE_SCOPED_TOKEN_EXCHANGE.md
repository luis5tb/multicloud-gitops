# Gateway credential mode with audience-scoped token exchange

**Status:** proposal, partly implemented and validated live on the standalone
cluster (2026-11). Each section is marked **[live]** (implemented and
verified), **[implemented]** (in code, not yet exercised live) or
**[proposed]**.

**Audience:** the lightspeed-agentic-operator and lightspeed-hub maintainers.
The doc is self-contained so it can be proposed upstream; references to this
repo (`multicloud-gitops`) show one working deployment of it.

**Builds on:** [`LIGHTSPEED_HUB_IDENTITY_DESIGN.md`](LIGHTSPEED_HUB_IDENTITY_DESIGN.md)
Option B ("token exchange instead of SA tokens") and its B-full variant. This
document is Option B made concrete. It adds what live testing showed is needed:
audience clients, role-scoped `groups`, and dropping spoke management in
gateway mode.

---

## 0. Who owns what

Three components, three owners. Each section below is tagged with its owner.

| Owner | Component | Responsibility in this design |
|---|---|---|
| **lightspeed-agentic-operator** | the operator that runs AgenticRuns in sandbox pods | gateway credential mode: render the sandbox kubeconfig pointing at the gateway with the requester's Token A; validate the run-token Secret; pause on expired credentials; **do no spoke management in gateway mode** (s3) |
| **lightspeed-hub** | `a2a-adapter` (A2A front for AgenticRuns) | A->H exchange; create the AgenticRun *as the user* with H; write and renew the run-token Secret (Token A) the operator mounts; serve the agent card at `/a2a/.well-known/agent-card.json` too |
| **lightspeed-hub** | hub operator (`SpokeCluster` registration) | spoke registry and probes. In gateway mode it needs no standing admin kubeconfig and no spoke-side provisioning (`openshift-lightspeed-managed`, reader bindings); registration becomes "URL + trust" (s6, B-full) |
| **deployment** (here: `multicloud-gitops`) | Keycloak realm, Praxis gateways, cluster OIDC + RBAC | audience clients, audience-scoped roles in `groups` (s4); per-spoke Praxis gateway doing A->U; OIDC claim mappings and RBAC bindings to the role groups (s5) |
| **MAS** | orchestrator | today forwards Token A verbatim; future per-delegation exchange (s4.2) |

The operator never talks to Keycloak and never sees H or U: it only mounts
Token A and points the sandbox at the gateway. The adapter never touches the
spoke. Keycloak and RBAC configuration is deployment-owned, but its *shape*
(s4.3, s5.2) is the contract the two upstream components rely on.

## 1. Goal

An agent investigating a cluster must act **as the requesting user**, with
that user's RBAC on the target cluster, and nothing more:

- no standing admin credential per spoke on the hub;
- no operator-minted ServiceAccount whose permissions are decided by the
  operator rather than the cluster's own RBAC;
- each token in the chain authorizes **only its own hop**, even when two hops
  land on the same Kubernetes API server (the hub acting as its own spoke).

## 2. Actors and tokens

```
ACME/caller --A--> MAS --A--> praxis-proxy --A--> hub A2A adapter --H--> hub kube API
                                                        | stores A (run-token Secret)
                                                        v
            AgenticRun sandbox --A--> praxis-spoke-<id> --U--> spoke kube API
```

| Token | Minted by | Audience (`aud`) | Used against | Purpose |
|---|---|---|---|---|
| **A** | Keycloak, for the caller (client credentials / user login) | the exchangers + the first hop (`mas-a2a`, `lightspeed-hub-adapter`, `praxis-spoke-gateway`) | MAS, praxis-proxy; never a kube API | caller identity, carried through the chain |
| **H** | Keycloak, exchange A→H by the hub adapter | `lightspeed-hub-a2a` | **hub** kube API | create the AgenticRun *as the user* |
| **U** | Keycloak, exchange A→U by the per-spoke Praxis gateway | `praxis-spoke-<id>` | **spoke** kube API | the investigation's reads, *as the user* |

All three carry the same `sub`. Kubernetes sees the same username
(`keycloak:<sub>`) for H and U, which is why section 5 is needed.

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
- **Execution stage:** mutations would also run as the user, so they need the
  user's own write RBAC on the spoke instead of operator-materialized
  execution RBAC. Keep Execution `Manual` in the ApprovalPolicy. Whether
  approval should be able to *elevate* beyond the user is an open product
  question (Option B notes approval gates *whether*, not *how much*).

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
token's `aud`. Because MAS forwards Token A verbatim (it cannot exchange per
delegation yet), Token A carries every downstream exchanger
(`lightspeed-hub-adapter`, `praxis-spoke-gateway`) via audience mappers on the
caller's client. **This is a stopgap.** The target design has the caller mint
`aud: mas-a2a` only and MAS exchange per delegation (needs a MAS feature).

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

Token A (real, decoded live):

```json
{ "sub": "bb366787-080c-4d98-b69e-2774e15855cb", "azp": "acme-agent",
  "aud": ["lightspeed-hub-adapter", "mas-a2a", "praxis-spoke-gateway"],
  "preferred_username": "service-account-acme-agent", "groups": ["acme-agent-rca"] }
```

Exchange A→H (by the adapter):

```
POST /realms/rca/protocol/openid-connect/token
  grant_type=urn:ietf:params:oauth:grant-type:token-exchange
  subject_token=<A>  subject_token_type=urn:ietf:params:oauth:token-type:access_token
  requested_token_type=urn:ietf:params:oauth:token-type:access_token
  audience=lightspeed-hub-a2a  client_id=lightspeed-hub-adapter  client_secret=...
```

| | H before | H now [live] | U before | U now [live] |
|---|---|---|---|---|
| `sub` | `bb366787-…` | same | same | same |
| `azp` | `lightspeed-hub-adapter` | same | `praxis-spoke-gateway` | same |
| `aud` | `lightspeed-hub-a2a` | same | `praxis-spoke-hub` | same |
| `groups` | `["acme-agent-rca"]` | `["lightspeed-hub-a2a:agenticrun-creator"]` | `["acme-agent-rca"]` | `["praxis-spoke-hub:reader"]` |

Verified live (2026-11-20), same fresh Token A, both exchanges replayed:

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
```

| Action | H | U |
|---|---|---|
| create AgenticRun in `lightspeed-hub` | allowed | **denied** |
| `get pods -n payments` | **denied** | allowed |

**Rules:**
- **Never bind anything to `keycloak:<sub>` directly.** The user is identical
  across tokens; only the role groups differ.
- `cluster-reader` excludes Secrets.
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
  spoke exchanger;
- that spoke's Praxis gateway (`praxis-spoke-<id>`).

Per spoke, the spoke needs:
- OIDC trust for audience `praxis-spoke-<id>`;
- the reader ClusterRoleBindings for `keycloak:praxis-spoke-<id>:reader`.

With ACM, the spoke-side pieces are ordinary per-cluster configuration that
policies can distribute, and the hub operator's planned MCE mode (OLS-3954)
handles registration and trust. Because gateway mode no longer needs spoke
management (3.2), **no standing admin kubeconfig per spoke is required**. The
hub needs only a minimal probe identity for health checks (B-full).

## 7. Upstream split

| Change | Owner / repo | State |
|---|---|---|
| Gateway credential mode (sandbox -> gateway with Token A, run-token validation, credential pause) | lightspeed-agentic-operator | implemented on fork (`8225d31`) |
| No spoke management in gateway mode (s3.2) | lightspeed-agentic-operator | implemented on fork |
| A2A adapter: A->H exchange, AgenticRun as the user, run-token Secret, renewal | lightspeed-hub (`a2a-adapter`) | implemented on fork |
| Adapter serves its agent card under `/a2a` too (gateway card pass-through) | lightspeed-hub (`a2a-adapter`) | implemented on fork |
| Gateway-mode spokes need no admin kubeconfig / spoke provisioning; B-full probe identity | lightspeed-hub (hub operator) | proposed |
| Audience-only clients (s4.1) | deployment (Keycloak) | live |
| Audience-scoped roles in `groups` (s4.3) | deployment (Keycloak); shape documented upstream | live |
| RBAC bound to role groups (s5.2) | deployment (cluster RBAC) | implemented (charts) |
| Per-delegation token exchange (drop multi-audience Token A) | MAS | proposed |

## 8. Open questions

- Execution with the user's own RBAC vs approval-based elevation (3.2).
- Token A lifetime vs long runs: the adapter's renewal bridge and
  `CredentialsExpired` pause exist, but haven't been exercised end to end.
- SSO-brokered callers on the hub leg: the adapter's exchanger does not send
  `subject_issuer`, which external-token exchange needs.
