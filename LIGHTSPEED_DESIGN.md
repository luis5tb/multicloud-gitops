# Design: OpenShift Lightspeed replaces `rca-agent` + `openshift-mcp-server`

**Status:** implemented. This document records the design rationale, security
analysis, and verified upstream research behind the OpenShift Lightspeed
pattern -- see [`LIGHTSPEED_IMPLEMENTATION_PLAN.md`](LIGHTSPEED_IMPLEMENTATION_PLAN.md)
for the phased task breakdown that was carried out against this design.

**Scope:** the **`standalone`** variant only (`variants/standalone/values-standalone.yaml`).
The `hub`/`group-one` variants do not deploy the RCA/ACME chain and must not change.
**This targets greenfield deployments only** — there is no live, already-deployed
instance of this pattern to migrate in place. `rca-agent` and
`openshift-mcp-server` are replaced directly by the new components; there is no
parallel-run, staged cutover, rollback path, or live-realm migration to design
for. Any step below framed as "migration" (e.g. the Keycloak realm) just means
"what the fresh `KeycloakRealmImport` renders," not an update to an existing
realm.

**Goal (verbatim from the request):** replace the custom `rca_agent` + the
standalone `openshift-mcp-server` with [OpenShift Lightspeed](https://github.com/openshift/lightspeed-service),
with the OpenShift MCP server enabled *inside* Lightspeed. Lightspeed must use
the **same Gemini-on-Vertex** model the `AgenticRun` `gemini` agent uses today
as its LLM provider and model. The **same Keycloak authentication/authorization
guarantees** must be preserved, so the request path becomes:

```
acme-agent ──► praxis-proxy ──► OpenShift Lightspeed ──► OpenShift MCP ──► OpenShift API
```

Because Lightspeed does **not** speak A2A today, one deliverable is a locally
vendored, tracked copy of `lightspeed-service` with an A2A server added. Two
further asks shape routing:

1. **ACME must require the user to state the target cluster (API URL)** in their
   request; ACME turns that into an extra HTTP header on the A2A call to
   Lightspeed.
2. **Praxis must route on that header** to the correct Lightspeed backend
   (load-balancing). There is one OLS now; the design must scale to many, each
   identified by a stable name **derived from its cluster URL**.

---

## 1. What exists today (verified against this branch)

| Component | Today | Source of truth |
| --- | --- | --- |
| `agents/acme_agent` + `charts/all/acme-agent` | Google ADK app. One `RemoteA2aAgent` child, one **shared** `httpx.AsyncClient` whose request event-hook (`auth.py:add_auth`) attaches a Keycloak bearer (Token A) to **every** outbound request. Root-agent instruction just says "route to one sub-agent". No cluster concept, no per-request header. UI (`ui.py`) posts a one-shot `message/send` JSON-RPC with no conversation/context persistence. | `agents/acme_agent/src/acme_agent/{agent,auth,config,ui}.py`, `variants/standalone/values-standalone.yaml` |
| `charts/all/praxis-proxy` | Owns the single public Route (`rca-agent.apps.<domain>`). `files/policy.yaml`: `identity/jwt` plugin validates the Keycloak JWT (issuer/audience/RS256 via JWKS) and allow-lists `claim.client_id == acme-agent`; `/.well-known/agent-card.json` and `/health/ready` are public. `files/praxis.yaml`: **one** catch-all route `path_prefix: /` → **one** static `load_balancer` cluster `rca-agent`. Runs `ghcr.io/praxis-proxy/ai:0.4.1` (pinned by digest, Core 0.7.0) — **to be replaced with an `ai` image whose Core dependency is v0.7.2** as part of this migration; no such image is published yet, see §7. | `charts/all/praxis-proxy/files/{policy,praxis}.yaml`, `values.yaml` |
| `agents/rca_agent` + `charts/all/rca-agent` | A2A server behind Praxis. `identity.py`: independently re-validates Token A (`KeycloakTokenValidator`), fetches its own SPIFFE JWT-SVID, and `KeycloakTokenExchanger` does the RFC 8693 exchange → Token B (`aud=openshift-mcp`). It does **not** run Gemini itself — it creates an `AgenticRun` CR via MCP and polls for the `AnalysisResult`. | `agents/rca_agent/rca_agent/{identity,mcp_agentic_run,agent}.py` |
| `charts/all/openshift-mcp-server` | A **separately packaged** MCP server. `configmap.yaml`: `require_oauth=true`, `skip_jwt_verification=true`, `cluster_auth_mode=passthrough`, `read_only=false`, `toolsets=["core"]`, `server_instructions` restricting it to AgenticRun/AnalysisResult. API server validates the forwarded Token B. | `charts/all/openshift-mcp-server/{templates/configmap.yaml,values.yaml}` |
| `charts/all/lightspeed-agentic-operator` | Deploys the **agentic** operator (a different product from Lightspeed-service). Defines the `gemini` Agent → `vertex-google` `LLMProvider` and runs `AgenticRun`s. **This is where the "Gemini option" lives today.** | `charts/all/lightspeed-agentic-operator/templates/llmprovider.yaml`, `variants/standalone/values-standalone.yaml` |
| `charts/all/keycloak-oidc` | Realm `rca`. SPIFFE-federated clients `acme-agent` (Token A; SA in group `acme-agent-rca`) and `rca-agent-mcp` (token-exchange; `openshift-mcp` audience mapper + `groups` mapper); audience-only client `openshift-mcp`; `openshift-cli`/`openshift-console` for human login. Native OIDC (`Authentication/cluster`) trusts the realm issuer; `openshift-mcp` is in `extraAudiences`; `sub → keycloak:<id>` username, `groups → keycloak:<group>`. RBAC + a `ValidatingAdmissionPolicy` bind the `keycloak:acme-agent-rca` group. | `charts/all/keycloak-oidc/templates/{keycloak-realm-import,openshift-authentication}.yaml`, `values.yaml`, `agents/AUTHENTICATION.md` |

### The "Gemini option" that must be reused

From `variants/standalone/values-standalone.yaml` (the `lightspeed-agentic-operator`
application), the `gemini` agent resolves to the `vertex-google` `LLMProvider`:

- provider `type: GoogleCloudVertex`, `modelProvider: Google`
- `projectID: redhat-marketplace-dev`, `region: global`
- model alias **`gemini-3.8-flash`**
- credentials: ESO-managed Secret `llm-creds-vertex`, from Vault key
  `secret/data/global/llm-creds-vertex`, property `credentials` (Google JSON).

> ⚠️ This is the **agentic-operator's** `LLMProvider` CRD shape, **not**
> Lightspeed's. Lightspeed's `OLSConfig` uses a completely different schema (see
> §4). "Same Gemini" means the same **Vertex project / region / model /
> credentials**, re-expressed in `OLSConfig` form — not a copy of the
> `LLMProvider` CR.

### Current end-to-end identity (preserve this exactly)

Two tokens, two independent checks (full detail in `agents/AUTHENTICATION.md`):

- **Token A** — ACME authenticates to Keycloak as client `acme-agent` using its
  SPIFFE JWT-SVID (`client_assertion_type=…:jwt-spiffe`, `grant_type=client_credentials`,
  **no `client_id` form field**). Claims: `sub=<acme SA>`, `azp=acme-agent`,
  `aud=[rca-agent, rca-agent-mcp]`, `groups=[acme-agent-rca]`. Praxis validates
  it and allow-lists `azp`; `rca-agent` validates it **again**.
- **Token B** — `rca-agent` performs RFC 8693 exchange authenticated as
  `rca-agent-mcp` (its own JWT-SVID as `client_assertion`), `subject_token=Token A`,
  `audience=openshift-mcp`. Result: `sub` unchanged (still ACME's SA), `azp=rca-agent-mcp`,
  `aud=openshift-mcp`, `groups=[acme-agent-rca]`. **Only Token B** reaches MCP.
- **OpenShift** validates Token B against the cached Keycloak JWKS (native OIDC),
  maps `sub → keycloak:<id>`, `groups → keycloak:acme-agent-rca`, and applies
  RBAC. MCP runs `cluster_auth_mode=passthrough` so the API server is the
  authority.

---

## 2. Target architecture

```
                 Token A (Keycloak, azp=acme-agent)          Token A (unchanged)
Browser ─► acme-agent ──────────────────────────► praxis-proxy ─────────────────► OpenShift Lightspeed
  │           │  + X-OLS-Cluster: <cluster-id>       │  validate JWT (as today)      (vendored, A2A added)
  │           │                                      │  route by X-OLS-Cluster         │
  │           └─ asks user for target cluster URL    │  → static OLS upstream          │ re-validate Token A
  │              validates it → derives <cluster-id> │                                 │ SPIFFE JWT-SVID
  │                                                   └── one OLS now, N later         │ RFC 8693 exchange
  │                                                                                    ▼
  │                                                                         Token B (aud=openshift-mcp)
  │                                                                                    │
  │                                                           operator-managed OpenShift MCP (introspection)
  │                                                                                    │ passthrough
  │                                                                                    ▼
  │                                                                       OpenShift API (native OIDC + RBAC)
  └─ response ◄───────────────────────────────────────────────────────────────────────┘
```

Key properties that **must not regress**:

- Token A is validated **independently** at Praxis *and* at Lightspeed's A2A
  boundary (defence in depth — a Praxis bypass must still fail).
- Only the **exchanged Token B** (never Token A, never a pod ServiceAccount
  token, never a static/shared MCP token, never `disable_auth`) reaches MCP.
- No public Route to Lightspeed or MCP; the only ingress is via Praxis. MCP is
  private to its cluster.
- The `X-OLS-Cluster` header is a **routing hint, not an authorization claim** —
  Praxis authorizes on the Keycloak token first, then routes.

---

## 3. Vendoring `lightspeed-service` and adding A2A

**Why:** verified against `openshift/lightspeed-service@c11e81671b…` (main,
2026-09-30) — the repo has **no A2A server and no `/.well-known/agent-card.json`**.
Routes are centralized in `ols/app/routers.py`; auth is `ols/src/auth/` (k8s =
`TokenReview` + `SubjectAccessReview` on the `/ols-access` non-resource path);
MCP header resolution is `ols/utils/mcp_utils.py` (the literal `"kubernetes"`
placeholder forwards the inbound user token; a caller-supplied `MCP-Headers`
header can override opt-in headers); the Vertex-Google provider
(`ols/src/llms/providers/google_vertex.py`) consumes **raw JSON** (service-account
or authorized-user) via provider config, *not* `GOOGLE_APPLICATION_CREDENTIALS`.
Image builds from the root `Containerfile`.

**Add (new):**

- `vendor/lightspeed-service/` — a **tracked** snapshot (git subtree or pinned
  snapshot + patch log) at a recorded upstream commit, keeping `LICENSE`/
  attribution and a documented refresh procedure. No nested `.git`, venv,
  caches, or secrets.
- `vendor/lightspeed-service/ols/app/endpoints/a2a.py` — a new A2A router that:
  - serves `/.well-known/agent-card.json` (registered **without** the `/v1`
    prefix, like `health`/`metrics`) advertising the **public Praxis HTTPS
    origin** as the RPC URL, cluster-neutral so ADK can discover it before any
    target is chosen;
  - implements the JSON-RPC methods ACME's ADK `RemoteA2aAgent` actually uses
    (`message/send`, task lookup, terminal/error responses; confirm
    streaming/cancel before claiming support);
  - **independently validates Token A** against Keycloak (JWKS/RS256, issuer, a
    **Lightspeed-specific inbound audience**, expiry, `azp=acme-agent`) — port
    the logic from `agents/rca_agent/rca_agent/identity.py`
    (`KeycloakTokenValidator`, `A2AAuthenticationMiddleware`);
  - reads and enforces `X-OLS-Cluster` (reject missing/unknown; compare against
    *this* instance's configured cluster id);
  - fetches this pod's SPIFFE JWT-SVID and performs the RFC 8693 exchange
    (`audience=openshift-mcp`) — port `KeycloakTokenExchanger` — then drives the
    normal OLS query/tool pipeline with **Token B** supplied as the `"kubernetes"`
    MCP token for that single invocation;
  - scopes conversation/task state to the authenticated caller + cluster id, and
    **blocks the `MCP-Headers` override** so a caller cannot inject its own MCP
    credential.
- `vendor/lightspeed-service/` build/provenance notes: build from the upstream
  `Containerfile`, publish to an immutable digest, record scan/SBOM.
- `ols/app/routers.py` patch: `include_router(a2a.router)`.

**Why not REST:** keep stock `/v1` REST on its `k8s` auth (`TokenReview` + the
`/ols-access` SAR). The A2A path is a *separate* boundary with the Keycloak +
SPIFFE + exchange flow; never grant ACME `/ols-access`, never use `noop`/
`disable_auth`.

---

## 4. Deploy Lightspeed via its operator; reuse Gemini-on-Vertex

Verified against `openshift/lightspeed-operator@02527d95…` (main, 2026-10-01).

**Add (new):**

- Operator **Subscription** + `openshift-lightspeed` namespace in
  `variants/standalone/values-standalone.yaml` (confirm package/channel/source
  from the live cluster catalog).
- `charts/all/openshift-lightspeed-config/` — a new local chart rendering the
  `OLSConfig` CR and its integration resources (Vertex credential Secret via
  ESO, SPIFFE registration, Keycloak CA, NetworkPolicy, etc.).

**`OLSConfig` Gemini provider** (exact field names verified in
`api/v1alpha1/olsconfig_types.go`):

```yaml
apiVersion: ols.openshift.io/v1alpha1   # confirm group/version on the installed operator
kind: OLSConfig
metadata:
  name: cluster
spec:
  llm:
    providers:
      - name: vertex-google
        type: google_vertex                 # lowercase enum value
        googleVertexConfig:
          projectID: redhat-marketplace-dev # same as the agentic gemini
          location: global                  # OLSConfig uses "location", not "region"
        models:
          - name: gemini-3.8-flash          # verify this exact id is Vertex-valid for OLS
        credentialsSecretRef:
          name: llm-creds-vertex
        # credentialKey defaults to "apitoken" — see credential note below
  ols:
    defaultProvider: vertex-google
    defaultModel: gemini-3.8-flash
    introspectionEnabled: true              # see §5 (default is already true)
```

**Credential mapping (investigate before implementing):** the agentic chart's
ESO Secret stores the Google JSON under key `credentials` and exposes it as
`GOOGLE_APPLICATION_CREDENTIALS`. Lightspeed's `google_vertex` provider instead
reads **raw JSON** from the provider credentials file, and the operator mounts
`credentialsSecretRef` using `credentialKey` (**default `apitoken`**). So the
new ESO `ExternalSecret` must publish the same Vault JSON
(`secret/data/global/llm-creds-vertex`, property `credentials`) under the key
the operator expects (`apitoken` unless overridden). Confirm the operator's
mounted path/key and that the JSON is `service_account` or `authorized_user`
shaped. **Do not** reuse ACME's LiteLLM/OpenAI key for OLS.

**Model-id caveat:** `gemini-3.8-flash` is the alias the agentic operator uses;
confirm OLS/Vertex accepts the same model id in this project/region with
tool-calling before rollout.

---

## 5. OpenShift MCP: what it must be configured to accept (the Keycloak→RBAC insights)

**The operator deploys MCP for us** (introspection). Verified: `introspectionEnabled`
defaults to **true**; when enabled the operator reconciles a standalone
`openshift-mcp-server` (Deployment/Service/SA/ConfigMap `openshift-mcp-server-config`/
NetworkPolicy/TLS `openshift-mcp-server-tls`, HTTPS **:8443**) in the operator
namespace, and injects an app-server `mcp_servers` entry:
`name: openshift`, `url: https://openshift-mcp-server.<ns>.svc:8443/mcp`,
`headers: {Authorization: "kubernetes"}`, `timeout: 60`.

**Therefore: do NOT keep `charts/all/openshift-mcp-server`.** Its replacement is
operator-managed. Do not package a second MCP server, and do not try to override
the operator-reconciled ConfigMap from Argo.

### The two viable ways MCP can accept a Keycloak token → OCP RBAC

Verified against `kubernetes-mcp-server@f4a39b17…` (the upstream of
`openshift/openshift-mcp-server`). **Critical default risk:** `require_oauth`
defaults to **false**, which makes the HTTP auth middleware a **no-op**; with no
bearer the Kubernetes client **falls back to the pod ServiceAccount**. The MCP
server the operator generates must therefore be hardened. Two supported shapes:

| Mode | MCP config | Who validates the token | Trade-off |
| --- | --- | --- | --- |
| **A. Passthrough (parity with today)** | `require_oauth=true`, `skip_jwt_verification=true`, `cluster_auth_mode=passthrough`, no `authorization_url` | The **OpenShift API server** (native OIDC) validates the forwarded Token B; MCP only requires *a* bearer | Simplest; relies on `openshift-mcp` being in `Authentication.extraAudiences` (already true) and on network isolation |
| **B. Independent OIDC (defence in depth)** | `require_oauth=true`, `authorization_url=https://<keycloak>/realms/rca`, `oauth_audience=openshift-mcp`, `certificate_authority=<mounted keycloak CA>`, `cluster_auth_mode=passthrough` | MCP validates the Keycloak JWT **itself** (JWKS), then forwards it; API server validates + RBAC again | Doesn't trust the network alone; needs the Keycloak CA mounted into MCP |

Either way: **no second MCP-side token exchange** (Lightspeed already supplies
the exchanged Token B — its `[token_exchange]` block from
`KEYCLOAK_OIDC_SETUP.md` must **not** be used), `read_only=true`, a restricted
toolset, Secrets/RBAC resources denied, and **no token-less pod-SA fallback**.

**Blocker — now addressed via Mode A (pending live verification).** The
operator *owns* `openshift-mcp-server-config`, and a later CRD investigation
confirmed the operator does **not** emit `require_oauth=true` and the `OLSConfig`
CRD exposes **no** field to set it (the operator hardcodes the MCP `config.toml`).
Rather than an "Argo override war" against a continuously-reconciled ConfigMap,
this is resolved by **Mode A**: `charts/all/openshift-lightspeed-config`'s
`appServerPatch.mcpHardening` bridge stops the operator reconciling (the
supported `operator.openshift.io/managementState: Unmanaged` annotation on the
`OLSConfig` CR, scale-to-0 as fallback) and then patches the generated ConfigMap
to `require_oauth=true` + `skip_jwt_verification=true` +
`cluster_auth_mode=passthrough`, re-asserting idempotently on every ArgoCD sync.
This is a **temporary** bridge: delete it once the operator/`OLSConfig` CRD
exposes `require_oauth` natively. It is implemented but **not yet verified
live**. See `charts/all/openshift-lightspeed-config/README.md`'s "MCP hardening"
section and `agents/AUTHENTICATION.md` step 7.

**NetworkPolicy is additive — don't assume a chart-side policy can narrow it.**
The operator's generated MCP NetworkPolicy allows any pod in its own namespace
to reach it, not just the app-server. Because Kubernetes NetworkPolicies are
**additive** (a second, stricter policy cannot override a permissive one already
in effect), our chart cannot fix this by adding its own restrictive
NetworkPolicy alongside the operator's — the permissive one still applies. If
this fails the threat model, the fix has to go through the operator's own
config/patch mechanism, same as the `require_oauth` gap above.

**Auditing the actor, not just the subject.** Keycloak's standard RFC 8693
exchange does not emit an `act` (actor) claim here, so Token B alone does not
record *which* exchanging client performed the exchange on the caller's behalf.
Actor attribution therefore lives in the **logs**: OLS emits one structured
`a2a_audit` record per A2A request (`actor=lightspeed-mcp`,
`on_behalf_of`/`subject` = the caller's `azp`/`sub`, plus cluster/task/outcome,
never the token), correlated with ACME's own `acme_audit` line via a shared
`X-Request-Id`. Attribution is scoped to *workload* identity (OLS on behalf of
`acme-agent`), not the human UI caller (unauthenticated by design). See
`agents/AUTHENTICATION.md`.

### What OpenShift must trust and map (already in place — keep it)

- `Authentication/cluster` trusts the realm issuer; **`openshift-mcp` stays in
  `openshiftOIDC.extraAudiences`** (the exchange audience and the API-server
  trusted audience must match).
- `sub → keycloak:<id>` username, `groups → keycloak:<group>` (keep the
  `keycloak:` prefix — it prevents collisions with reserved `system:` groups).
- RBAC binds the **prefixed** group `keycloak:acme-agent-rca`. API-server RBAC is
  the final authority on every Kubernetes call; MCP's denied-resources/read-only
  are supplemental hints.

### RBAC to grant (and not grant)

Add only **namespace-scoped**, least-privilege `Role`/`RoleBinding`s for
`keycloak:acme-agent-rca` in exactly the namespace(s) investigation needs (start
from the existing `payments` allow-list entry). Read verbs for the specific
resources the chosen MCP toolset exposes (e.g. `get/list` pods/events; `get` pod
logs only if deliberately in scope — logs can leak secrets). **No** automatic
`view`/cluster-admin, **no** Secret/RBAC access, **no** writes by default. Note
the current `agenticRun.namespaceAllowlist` admission policy governs **AgenticRun
creates only** — it does **not** constrain general MCP reads, so it is not a
substitute for RBAC here.

> **Identity caveat (unchanged, worth restating):** the browser user is *not*
> authenticated — Keycloak identifies the **ACME workload**, not the human. So
> the RBAC grant and the cluster registry bound what *any* ACME user can reach.
> Per-human authorization would be a separate design (capture + propagate a human
> subject); a cluster header is not a user-authorization mechanism.

---

## 6. ACME: require the target cluster URL, emit the routing header

**Prompt (necessary, not sufficient).** Update the root-agent instruction in
`agents/acme_agent/src/acme_agent/agent.py` to require the user to state the
**target OpenShift cluster API URL** (e.g. `https://api.bm-cluster.e2e.bos.redhat.com:6443`)
and to ask a clarifying question when it is missing/ambiguous — never infer a
cluster from a resource name or pick a default.

**Deterministic enforcement (the real control).** A prompt can't be trusted to
set a header, so add a **server-side validator** before remote dispatch:

- canonicalize the URL (https only; lowercased/IDNA host; explicit port; reject
  credentials/path/query/fragment);
- look it up in a GitOps-managed **cluster registry** (allow-list); reject
  unknown URLs;
- derive a stable, DNS-safe **cluster id from the URL** (the user's "name based
  on its url"): lowercase host+port, separators → `-`, with a short SHA-256
  suffix only if needed for the 63-char cap / collisions. Example:
  `https://api.bm-cluster.e2e.bos.redhat.com:6443`
  → **`api-bm-cluster-e2e-bos-redhat-com-6443`**.
- Because `ui.py` is **one-shot** (no conversation state carried), require the
  URL on **every** message until stateful clarification exists; optionally add a
  required cluster field to the UI, but still enforce server-side for other A2A
  clients.

**Transport (the tricky part).** `agent.py` uses **one shared** `httpx.AsyncClient`
and `auth.py`'s event hook adds the bearer to every request — including the
public agent-card fetch. Emit `X-OLS-Cluster: <cluster-id>` as an
**invocation/task-scoped** header on the **authenticated RPCs only**, without a
process-global default (concurrent conversations must not swap clusters) and
without touching the unauthenticated card fetch. If ADK's `RemoteA2aAgent` can't
carry a request-scoped header through send/poll/cancel, add a narrow transport
adapter (e.g. a `ContextVar`-driven hook). Keep `a2a.remoteAgents[0].endpoint`
pointed at the **single Praxis Route**, not at per-cluster OLS services.

---

## 7. Praxis: authorize, then route on `X-OLS-Cluster`

**Keep** the Keycloak JWT validation and `claim.client_id == acme-agent`
allow-list in `files/policy.yaml` — authorization runs **before** routing.

**Change `files/praxis.yaml`** from the single catch-all
(`path_prefix: /` → one `rca-agent` cluster) to **header-matched routing**: one
route per registered cluster matching `X-OLS-Cluster` exactly → a **static**
`load_balancer` cluster for that OLS backend. On absent/unknown/duplicate header,
**reject** (fail closed) — no default backend, no routing to a user-supplied
URL, no HTTP redirect (would leak the bearer). For one OLS now, register exactly
one id → backend; a cluster's OLS replicas may be balanced among *equivalent*
endpoints, but never round-robin across *different* clusters.

**Cluster registry (shared GitOps values)** — the single source both ACME and
Praxis render from (ACME gets URL + id; Praxis gets id + static upstream/TLS):

```yaml
global:
  olsClusters:
    api-bm-cluster-e2e-bos-redhat-com-6443:
      apiURL: https://api.bm-cluster.e2e.bos.redhat.com:6443
      upstreamHost: lightspeed-app-server.openshift-lightspeed.svc.cluster.local
      upstreamPort: 8443            # confirm OLS app-server service port/scheme
      upstreamSNI: lightspeed-app-server.openshift-lightspeed.svc
```

**Praxis version bump + capability spike (dependency) — corrected.** An earlier
pass at this plan treated "bump to v0.7.2" as a near-drop-in digest pin. A
deeper check (cross-validated against a parallel plan on branch `rca-praxis-ols`,
which inspected the actual source tags/lockfiles) shows **that is wrong**, and
the correction matters because it changes this from a config change to a build
project:

- **`praxis-proxy/praxis` (Core) and `praxis-proxy/ai` are separate
  repos/release lines.** The chart runs **`ai`**, not `praxis` — `ai` is what
  carries the `identity/jwt` policy plugin used in `files/policy.yaml`. The
  v0.7.2 **Core** release (`praxis-proxy/praxis@v0.7.2`, pre-release,
  2026-09-30, commit `1de023c`) is not a `praxis-proxy/ai` tag at all.
- **No published `ai` image embeds Core v0.7.2 yet.** The `ai` v0.4.1 source
  tag's own lockfile resolves its Praxis Core dependency to **0.7.0**; `ai`'s
  current `main` branch resolves to Core **0.7.1**. Published `ai` tags stop at
  v0.4.1. **Do not retag the existing `ghcr.io/praxis-proxy/ai:0.4.1` image to
  `0.7.2`** — that image does not exist upstream.
- **Therefore the real task is:** either find/wait for a published `ai` release
  that embeds Core v0.7.2, or pin `ai`'s source + `Cargo.lock` to resolve Core to
  exactly v0.7.2, build it from that pinned source, run its upstream test suite,
  generate an SBOM, scan it, and publish an immutable digest ourselves. Record
  the **Core version**, the **`ai` version/source commit**, and the final image
  digest as three separate facts — never label an image "0.7.2" because only its
  Core dependency is 0.7.2.
- **What v0.7.2 Core is confirmed (not just hoped) to add**, per its own docs at
  that tag: `routes[].headers` exact-match routing
  (`docs/filters/http/traffic_management/router.md`) and per-upstream TLS
  configuration (`docs/filters/http/traffic_management/load_balancer.md`). These
  are the two capabilities this plan needs — but they only become usable once an
  `ai` image actually embeds this Core version; until then the currently
  deployed `ai:0.4.1` (Core 0.7.0) has **neither**, confirming the single
  catch-all route in `files/praxis.yaml` is a real, not hypothetical, gap.
- **Still re-validate on the chosen image regardless:** `insecure_options`
  (`allow_private_endpoints`/`allow_private_upstreams`), `allow_private_idp`/
  `require_protocol_metadata`, response status on a rejected/unmatched route, and
  **header-name casing** behavior for `X-OLS-Cluster`. Core's release notes also
  mention a subrequest stream-deadline fix (#1179) and file-descriptor handling
  (#1303) — regression-test deadlines/cancellation/resource limits for
  long-running A2A requests, not just happy-path routing.
- **Existing building blocks to reuse rather than design from scratch:** `ai`
  v0.4.1 ships reference configs for exactly this shape —
  `examples/configs/a2a-agent-card-routing.yaml` and
  `examples/configs/a2a-task-routing.yaml` (A2A task-owner routing). The
  task-routing example uses an **in-memory** owner store, unsuitable for a
  multi-replica gateway — decide between a persistent task-owner store and
  relying on ACME's per-task target propagation (MP §6) with fail-closed
  mismatch handling, rather than adopting the example verbatim.

**Multi-cluster later:** pin each A2A task/context to the cluster chosen on its
first `message/send` and deny a later conflicting header. Each future OLS on
another cluster needs its own trusted TLS path, its own SPIFFE-bound exchanging
client, its own API-server OIDC/RBAC, and ideally a **per-cluster MCP audience**
so a Token B minted for cluster A can't be replayed against cluster B.

---

## 8. Keycloak client changes

In `charts/all/keycloak-oidc` — added declaratively to `keycloak-realm-import.yaml`
like every other client already there; this is a greenfield realm, so these just
render in on first sync, no coexistence-with-a-live-realm concerns:

- **New** confidential, SPIFFE-federated client `lightspeed-mcp`
  (`clientAuthenticatorType: federated-jwt`, `jwt.credential.sub =
  spiffe://<trust-domain>/ns/openshift-lightspeed/sa/lightspeed-app-server` —
  the operator's **verified** app-server SA name), with
  `standard.token.exchange.enabled: "true"`, an `openshift-mcp` **client**
  audience mapper, and a `groups` mapper. This plays the role `rca-agent-mcp`
  plays today, but bound to the OLS workload.
- On the **`acme-agent`** client, add a mapper for the new Lightspeed inbound
  audience (e.g. `lightspeed-a2a`, a custom-string audience the OLS A2A validator
  checks) and a `lightspeed-mcp` **client** audience mapper (so the subject token
  can be exchanged by `lightspeed-mcp`).
- Keep `openshift-mcp` (audience-only client) and `extraAudiences`.

---

## 9. What is added / changed / deleted (summary)

**Add**
- `vendor/lightspeed-service/` (tracked snapshot + A2A endpoint + image build notes)
- `charts/all/openshift-lightspeed-config/` (`OLSConfig`, Vertex ESO Secret, SPIFFE, Keycloak CA, NetworkPolicy)
- Lightspeed Operator Subscription + `openshift-lightspeed` namespace (standalone values)
- `global.olsClusters` registry (shared by ACME + Praxis)
- Keycloak `lightspeed-mcp` client + ACME mappers; new least-privilege RBAC for `keycloak:acme-agent-rca`

**Change**
- `agents/acme_agent` — cluster-URL prompt + server-side validator + id derivation + invocation-scoped `X-OLS-Cluster` header; tests
- `charts/all/acme-agent` — registry-derived config; `a2a.remoteAgents[0]` name/description/endpoint (still the Praxis Route)
- `charts/all/praxis-proxy` — header-matched routing + static HTTPS upstream per cluster; **a rebuilt `praxis-proxy/ai` image whose Cargo.lock resolves Praxis Core to v0.7.2** (not a retag — see §7), pinned by digest; keep JWT policy; readiness path
- `charts/all/keycloak-oidc` — new client/mappers/RBAC, added declaratively (greenfield realm)
- `variants/standalone/values-standalone.yaml` — add operator + OLS config + registry; retarget Praxis/ACME; remove old apps directly (no staged cutover — greenfield)
- `agents/AUTHENTICATION.md`, `README.md`s — new diagram, tokens, routing, runbook

**Delete (directly, as part of this work — greenfield, no live instance to preserve)**
- `agents/rca_agent/`, `charts/all/rca-agent/`
- `charts/all/openshift-mcp-server/` (replaced by operator-managed MCP)
- `rca-agent-litellm` from `values-secret.yaml.template`; stale RCA/AgenticRun docs
- The `lightspeed-agentic-operator` app / `AgenticRun` RBAC / VAP / `llm-creds-openai`
  **only if** a dependency audit shows nothing else uses them (still a separate,
  verified check — the "greenfield" simplification removes the need for a staged
  cutover, not the need to confirm nothing else depends on these before removing
  them)

---

## 10. Security requirements (must all hold)

- Token A independently validated at Praxis **and** at the OLS A2A boundary.
- Only exchanged **Token B** reaches MCP; never Token A, a pod SA token, a shared
  static token, or `disable_auth`.
- MCP requires a bearer (`require_oauth=true`) with **no pod-SA fallback**
  (satisfied by Mode A via the `appServerPatch.mcpHardening` bridge — §5;
  implemented, pending live verification); `read_only=true`, restricted
  toolset, Secrets/RBAC denied (the operator default leaves `read_only=false`
  and a broad toolset — tightening these the same way is in-scope-but-not-done,
  so today this rests on the least-privilege MCP RBAC the API server enforces).
- OCP RBAC (via `keycloak:` group mapping) is the final authority; grants are
  least-privilege and namespace-scoped.
- `X-OLS-Cluster` is a routing hint, not a claim; unknown/absent → fail closed,
  never a default backend or a user-supplied upstream URL.
- No public Route to OLS or MCP; ingress only via Praxis; MCP private.
- No credentials/tokens/kubeconfigs committed; use Vault → ESO; keep break-glass.

---

## 11. Phased implementation (greenfield — no staged cutover needed)

0. **Spikes / interface freeze (blocking).** Confirm on the live cluster:
   `oc whoami --show-server`, SPIRE trust domain, the operator package/channel,
   the operator app-server SA (`lightspeed-app-server`) and MCP SA
   (`openshift-mcp-server`), the operator's generated MCP config
   (`require_oauth`?), the `--service-image`/`--openshift-mcp-server-image`
   override path, the `OLSConfig` group/version + Vertex credential key, the
   `gemini-3.8-flash` id validity, and the **Praxis `ai`-image-embedding-Core-v0.7.2**
   go/no-go (does a compatible published image exist yet, or must one be built
   from a pinned source + `Cargo.lock` — see §7) plus header-routing +
   HTTPS-upstream capability on whichever image is chosen. Freeze: header name,
   canonicalization/id algorithm, registry schema, audiences, upstream TLS,
   chosen Praxis Core version + `ai` version/source commit + final image digest
   (recorded as three separate facts).
1. Vendor `lightspeed-service`; reproducible image.
2. Operator + `OLSConfig` (Gemini) deployed to a test namespace; MCP hardened
   (§5); verify Gemini inference + operator-managed MCP.
3. Keycloak `lightspeed-mcp` client + mappers + scoped RBAC, added declaratively
   to the realm import; verify real Token A/B claims + API-server authorization.
4. A2A endpoint in the vendored service (Keycloak validate → SPIFFE → exchange →
   Token B into the `"kubernetes"` MCP slot); tests incl. negative cases.
5. ACME prompt + validator + id + invocation-scoped header; tests incl.
   concurrent different-cluster callers.
6. Resolve the Praxis `ai`/Core-v0.7.2 image (find a published one or build+publish
   from a pinned source/lockfile — §7); pin by digest; header routing + HTTPS
   upstream (fake backends first); tests.
7. Pattern integration; full ACME→…→API dry run.
8. Remove `rca-agent`/`openshift-mcp-server` wiring and source directly (no
   staged cutover/rollback needed — greenfield); update docs.

**Cross-phase negative tests:** missing/unknown cluster URL → no downstream call;
absent/unknown/duplicate `X-OLS-Cluster` → Praxis fail-closed; wrong-audience
Token A → denied at both Praxis and OLS; MCP call without bearer → rejected, no
SA fallback; Token B without the expected group / out-of-scope namespace → RBAC
denies; two interleaved callers → no header/token/task cross-talk.

---

## 12. Open questions / risks to resolve before coding

1. ~~**Operator MCP config** — does the installed operator emit `require_oauth=true`,
   or must we use `--openshift-mcp-server-image` / a patch?~~ **Resolved:** the
   operator does **not** emit it and the `OLSConfig` CRD has no field for it, so
   it is patched out-of-band by Mode A (`appServerPatch.mcpHardening`, §5) while
   the operator is paused. Implemented; **still to verify live** that a tokenless
   MCP call is now rejected and the pod-SA fallback is gone.
2. **Vertex credential key** — exact secret key/path the operator mounts for
   `google_vertex` (`credentialKey` default `apitoken`) vs. our Vault JSON shape.
3. **Model id** — is `gemini-3.8-flash` valid for OLS on Vertex in
   `redhat-marketplace-dev`/`global` with tool-calling?
4. **Praxis `ai` image embedding Core v0.7.2** — none is published as of this
   writing (`ai` v0.4.1 resolves Core to 0.7.0; `ai` `main` resolves to 0.7.1;
   `ai` tags stop at v0.4.1). Has a later `ai` release shipped by
   implementation time? If not, is pinning `ai`'s source + `Cargo.lock` to Core
   v0.7.2 and building/publishing it in-house approved, and by whom? Once an
   image is chosen: does it match `X-OLS-Cluster` and do HTTPS upstream with
   trusted CA/SNI (Core v0.7.2's own docs say yes — confirm on the actual `ai`
   build, not just Core's CLI)? Confirm the IP-classification change (PR #1282)
   doesn't alter `insecure_options`/`allow_private_idp` behavior.
5. **ADK header propagation** — can `RemoteA2aAgent` carry an invocation-scoped
   header across send/poll/cancel, or is a transport adapter required?
6. **AnalysisResult parity** — RCA returns a structured diagnosis + remediation
   proposals via `AgenticRun`/`AnalysisResult`. If that output contract is a
   product requirement, agree an OLS equivalent **before** deleting RCA; a plain
   OLS answer is not automatically equivalent.
7. ~~Keycloak realm migration~~ — not applicable; greenfield only, no live realm
   to migrate. Break-glass should still be confirmed working on the fresh realm.

---

## 13. Upstream references (verified for this plan)

> **Note on pin discrepancies:** an independently written plan pair on branch
> `rca-praxis-ols` (commit `15e26a55`) researched the same two repos and pinned
> different commits for both (`lightspeed-service@690939861b…` vs. this plan's
> `c11e81671b…`; `lightspeed-operator@fd157f53b5…` vs. this plan's
> `02527d9512…`). Both plans independently reached the **same conclusions** on
> every load-bearing fact (no A2A/well-known route, `OLSConfig` field names,
> `introspectionEnabled` default-true auto-MCP, the generated `require_oauth`
> gap) — this is reassuring cross-validation, not a contradiction, but it means
> "main" moved between research passes. **Re-verify whichever commit is current
> at Phase 0 (T0.2/T0.7) rather than trusting either pin as still-current.**

- `openshift/lightspeed-service@c11e81671b3e84d76e8ad824f2d1d5485590e654` —
  `ols/app/routers.py` (no A2A/well-known), `ols/src/auth/{auth,k8s}.py`
  (TokenReview + `/ols-access` SAR), `ols/utils/mcp_utils.py` (`"kubernetes"`
  placeholder + `MCP-Headers`), `ols/src/llms/providers/{google_vertex,utils}.py`
  (raw JSON creds), root `Containerfile`.
- `openshift/lightspeed-operator@02527d951231d889749d76e44d06ef452076a629` —
  `api/v1alpha1/olsconfig_types.go` (`google_vertex` + `googleVertexConfig.{projectID,location}`,
  `models[].name`, `credentialsSecretRef`/`credentialKey`, `ols.defaultProvider/defaultModel`),
  `introspectionEnabled` default true auto-deploys MCP (`internal/controller/ocpmcp/*`,
  `appserver/assets.go` → `openshift` entry at `https://openshift-mcp-server.<ns>.svc:8443/mcp`,
  `Authorization: "kubernetes"`), `cmd/main.go` (`--service-image`,
  `--openshift-mcp-server-image`), SAs `lightspeed-app-server` / `openshift-mcp-server`.
- `containers/kubernetes-mcp-server@f4a39b17ca4cb60e1bfa83bd2eabd725a352d22d`
  (general upstream) and `openshift/openshift-mcp-server@c0cc705caf3aa031b198a0bfd47c92cf8ec737da`
  (the actual downstream fork this pattern consumes — prefer this pin when
  re-verifying) — `pkg/config/config.go` + `pkg/http/authorization.go`
  (`require_oauth` default **false** → middleware no-op; `skip_jwt_verification`;
  `cluster_auth_mode` default `passthrough`;
  `authorization_url`/`oauth_audience`/`certificate_authority`),
  `pkg/kubernetes/manager.go` (no bearer → **pod-SA fallback**; passthrough
  forwards the token), `docs/KEYCLOAK_OIDC_SETUP.md` (independent OIDC
  validation — its example MCP-side exchange + `cluster-admin` demo account is an
  **alternative** to this plan's OLS-side exchange, not additional guidance to
  copy). Both pins independently confirmed the same `require_oauth` default-false
  gap.
- `praxis-proxy/praxis@v0.7.2` (pre-release, 2026-09-30, commit `1de023c`) —
  release notes cover CI/FIPS, centralized IP-address classification (PR #1282),
  subrequest stream-deadline fix (#1179), file-descriptor handling (#1303); its
  docs at this tag confirm `routes[].headers` exact matching
  (`docs/filters/http/traffic_management/router.md`) and per-upstream TLS
  (`docs/filters/http/traffic_management/load_balancer.md`). **This is a
  `praxis-proxy/praxis` (Core) tag, not a `praxis-proxy/ai` release** — the chart
  runs `ai`, whose v0.4.1 tag/lockfile resolves Core to 0.7.0 and whose `main`
  resolves to 0.7.1; no published `ai` image embeds Core v0.7.2 yet. See §7 for
  the corrected (build-or-wait) plan; do not retag `ghcr.io/praxis-proxy/ai:0.4.1`
  to `0.7.2`. Reference configs: `ai@v0.4.1`
  `examples/configs/{a2a-agent-card-routing,a2a-task-routing}.yaml`.
- This repo: `agents/AUTHENTICATION.md`, `agents/rca_agent/rca_agent/identity.py`
  (port validation + exchange), `charts/all/keycloak-oidc/templates/{keycloak-realm-import,openshift-authentication}.yaml`,
  `charts/all/openshift-mcp-server/templates/configmap.yaml`,
  `charts/all/praxis-proxy/files/{policy,praxis}.yaml`,
  `variants/standalone/values-standalone.yaml`.
