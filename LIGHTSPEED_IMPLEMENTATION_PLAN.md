# Implementation plan: OpenShift Lightspeed pattern

**Status:** implemented; kept as a record of the phased task breakdown carried
out. Companion to **`LIGHTSPEED_DESIGN.md`** (the "why" / design). This
document is the **"how"**: ordered, self-contained tasks an implementing agent
executed one at a time. Where this doc says "see MP §N" ("MP" = the design
doc, `LIGHTSPEED_DESIGN.md`, by its original working title), read that section
for rationale and verified upstream facts.

---

## 0. How to use this document (read first)

**Audience:** autonomous implementing agents. Each task below is written to be
picked up in isolation. Do the tasks **in dependency order** — each lists
`Depends on`. Do **not** start a task whose dependencies are unmet.

**Task format:**
- **ID / Title**
- **Depends on** — task IDs that must be `DONE` first
- **Suggested agent** — the kind of subagent best suited
- **Files** — concrete paths to create/change (repo-relative)
- **Steps** — the actions to perform
- **Acceptance criteria** — objective, checkable conditions for `DONE`
- **Verify** — commands / observations proving it
- **Rollback** — how to undo if it regresses

**Global constraints (apply to every task — violating any is a hard stop):**
1. **Scope = `standalone` variant only.** Edits to cluster config live in
   `variants/standalone/values-standalone.yaml`. Do **not** touch `hub` /
   `group-one` variants.
2. **Plan precedes irreversible change.** Phase 0 spikes are **blocking**; do not
   start Phase 1+ build work until the interface-freeze decisions (T0.9) are
   recorded.
3. **Preserve the identity guarantees in MP §10 verbatim.** Token A validated at
   both Praxis and OLS; only exchanged Token B reaches MCP; no pod-SA fallback;
   no public route to OLS/MCP; `X-OLS-Cluster` is a routing hint, not a claim;
   fail closed.
4. **Greenfield only — no migration/cutover to design for.** There is no live,
   already-deployed instance of this pattern. `rca-agent` + `openshift-mcp-server`
   are replaced **directly**; there is no parallel-run requirement, no staged
   cutover, and no rollback path to maintain. The Keycloak realm is created fresh
   by `KeycloakRealmImport` — new clients/mappers are added declaratively like
   every existing one, with no live-realm migration step.
5. **No host mutation.** Never install packages/deps on the host. Build, lint,
   and test only inside containers or ephemeral Python venvs. (User standing
   rule.)
6. **No secrets in git.** All credentials flow Vault → ESO. Pin every container
   image by **digest**. Keep break-glass (`make admin-break-glass-kubeconfig`)
   working at all times.
7. **GitOps discipline.** Changes are declarative (Helm charts + variant values
   reconciled by Argo CD). Do not hand-edit live cluster objects except for
   read-only spikes.
8. **Record findings.** Spikes write their results into this file's
   **"Frozen interface decisions"** table (T0.9) so later tasks consume concrete
   values, not guesses.

**Status legend for tracking:** `TODO` / `IN-PROGRESS` / `BLOCKED` / `DONE`.
Keep a one-line status next to each task ID as work proceeds.

---

## Phase 0 — Spikes & interface freeze (BLOCKING)

Read-only investigation on the live cluster + upstream repos. **No chart/code
changes in this phase** except writing decisions into T0.9. Use the `Explore` /
`general-purpose` agents; use `oc` read-only.

### T0.1 — Cluster + SPIRE facts
- **Depends on:** —
- **Suggested agent:** general-purpose
- **Steps:** capture `oc whoami --show-server` (the canonical API URL that seeds
  the cluster id), `oc get zerotrustworkloadidentitymanager cluster -o jsonpath='{.spec.trustDomain}'`
  (expect `apps.bm-cluster.e2e.bos.redhat.com`), and the realm issuer already in
  values (`https://keycloak.apps.bm-cluster.e2e.bos.redhat.com/realms/rca`).
- **Acceptance:** API URL, trust domain, issuer recorded in T0.9.
- **Verify:** values match `variants/standalone/values-standalone.yaml`.
- **Rollback:** n/a (read-only).

### T0.2 — Lightspeed Operator availability
- **Depends on:** —
- **Suggested agent:** general-purpose
- **Steps:** find the operator in the cluster catalog
  (`oc get packagemanifests | grep -i lightspeed`); record package name,
  channel, catalog source, and the installed `OLSConfig` **group/version**
  (`oc get crd | grep -i olsconfig`, then `oc explain olsconfig.spec.llm`).
  Confirm the operator flags `--service-image` and `--openshift-mcp-server-image`
  exist on the installed version (MP §4/§5).
- **Acceptance:** subscription coordinates + CRD apiVersion + flag availability
  recorded in T0.9.
- **Verify:** `oc explain` returns the `google_vertex`/`googleVertexConfig`
  fields named in MP §4.
- **Rollback:** n/a.

### T0.3 — Operator-managed MCP config audit (deployment blocker)
- **Depends on:** T0.2
- **Suggested agent:** general-purpose
- **Steps:** on a scratch `OLSConfig` with `introspectionEnabled: true`, inspect
  the operator-generated `openshift-mcp-server-config` ConfigMap. Determine
  whether it sets `require_oauth=true`, `cluster_auth_mode=passthrough`, and
  whether any token-less pod-SA fallback is possible (MP §5). Decide **Mode A
  (passthrough)** vs **Mode B (independent OIDC)** and whether a hardened MCP
  image via `--openshift-mcp-server-image` is required.
- **Acceptance:** the exact MCP hardening approach (settings + whether a custom
  image is needed) recorded in T0.9. **If the operator config cannot be made
  safe through supported settings, raise it as a blocker before Phase 2.**
- **Verify:** generated config inspected; decision justified against MP §5.
- **Rollback:** delete the scratch `OLSConfig`/namespace.

### T0.4 — Vertex credential shape for OLS
- **Depends on:** T0.2
- **Suggested agent:** general-purpose
- **Steps:** determine the secret **key** the operator mounts for a
  `google_vertex` provider (`credentialKey`, default `apitoken`) and the expected
  JSON shape (service-account vs authorized-user). Compare against the existing
  Vault entry `secret/data/global/llm-creds-vertex` property `credentials` used
  by the agentic operator (MP §1/§4).
- **Acceptance:** the ESO key-mapping (`credentials` → `apitoken` or override)
  recorded in T0.9.
- **Verify:** operator docs/CRD confirm the key; JSON shape matches.
- **Rollback:** n/a.

### T0.5 — Gemini model-id validity
- **Depends on:** T0.2, T0.4
- **Suggested agent:** general-purpose
- **Steps:** confirm model id `gemini-3.8-flash` is accepted by OLS on Vertex in
  project `redhat-marketplace-dev` / location `global`, **with tool-calling**
  (MCP needs function-calling). If the OLS/Vertex id differs from the agentic
  alias, record the correct id.
- **Acceptance:** confirmed model id recorded in T0.9.
- **Verify:** a minimal OLS query returns a completion using that model in the
  scratch deploy (can fold into T2.1).
- **Rollback:** n/a.

### T0.6 — Operator SA names for SPIFFE federation
- **Depends on:** T0.2
- **Suggested agent:** general-purpose
- **Steps:** confirm the app-server SA is `lightspeed-app-server` and the MCP SA
  is `openshift-mcp-server`, and the namespace OLS deploys into
  (`openshift-lightspeed` proposed) (MP §4/§5/§8).
- **Acceptance:** exact `ns/sa` pairs recorded in T0.9 (feeds the Keycloak
  `jwt.credential.sub` SPIFFE id in T3.x).
- **Verify:** `oc get sa -n <ns>` on the scratch deploy.
- **Rollback:** n/a.

### T0.7 — lightspeed-service A2A surface confirmation
- **Depends on:** —
- **Suggested agent:** Explore
- **Steps:** re-confirm against the pinned commit
  (`openshift/lightspeed-service@c11e8167…`): no existing A2A / well-known route;
  router registration point (`ols/app/routers.py`); auth module layout
  (`ols/src/auth/`); MCP header resolution + the `"kubernetes"` placeholder and
  `MCP-Headers` override (`ols/utils/mcp_utils.py`); Vertex provider raw-JSON
  credential handling; root `Containerfile`. Identify the exact ADK JSON-RPC
  methods ACME's `RemoteA2aAgent` calls so the A2A endpoint implements the right
  set (MP §3).
- **Acceptance:** the A2A endpoint's method list + integration points recorded
  in T0.9.
- **Verify:** file paths exist at the pinned commit.
- **Rollback:** n/a.

### T0.8 — Praxis `ai`/Core-v0.7.2 image resolution spike (corrected scope)
- **Depends on:** —
- **Suggested agent:** general-purpose
- **Steps:** `praxis-proxy/praxis@v0.7.2` is a **Core** pre-release
  (commit `1de023c`), not a `praxis-proxy/ai` release — the chart runs `ai`,
  which is what carries the `identity/jwt` policy plugin used in
  `charts/all/praxis-proxy/files/policy.yaml`. The currently pinned `ai:0.4.1`
  tag/lockfile resolves its Core dependency to 0.7.0; `ai`'s `main` resolves to
  0.7.1; no published `ai` tag goes past v0.4.1. **Do not retag the existing
  image to `0.7.2`.** First check whether a newer `ai` release has shipped that
  embeds Core v0.7.2. If not, this task is a **build decision**, not a config
  change: get sign-off to pin `ai`'s source + `Cargo.lock` to resolve Core to
  v0.7.2, build it from that pinned source (ephemeral container, no host
  installs), run `ai`'s own test suite, generate an SBOM, scan it, and publish an
  immutable digest. Once an image is in hand (published or built), confirm on it:
  (a) the `identity/jwt` plugin is present; (b) `routes[]` matches on an HTTP
  header (`X-OLS-Cluster`) per Core v0.7.2's
  `docs/filters/http/traffic_management/router.md`; (c) HTTPS upstream with
  trusted CA/SNI per `docs/filters/http/traffic_management/load_balancer.md`;
  (d) `insecure_options.allow_private_endpoints/upstreams` and policy
  `allow_private_idp`/`require_protocol_metadata` still behave as in 0.4.1 after
  the IP-classification change (PR #1282); (e) header-name **casing** behavior.
  Check `ai@v0.4.1`'s `examples/configs/{a2a-agent-card-routing,a2a-task-routing}.yaml`
  as a starting point for routing config shape — note the task-routing example's
  owner store is in-memory, unsuitable for a multi-replica gateway. (MP §7, §12 Q4.)
- **Acceptance:** Core version, `ai` version/source commit, and final image
  digest recorded **separately** in T0.9 (never label an image "0.7.2" because
  only Core is); confirmed header-match + HTTPS-upstream config syntax; any
  schema deltas recorded. **If no compatible image can be obtained/built, or
  header matching/the JWT plugin is unavailable on it, raise as a blocker before
  Phase 6 — this may mean Phase 6 ships without the version bump.**
- **Verify:** a local container smoke test (ephemeral, not host-installed) with a
  minimal config matching a header to a fake backend.
- **Rollback:** n/a.

### T0.9 — Freeze the interface (gate to Phase 1+)
- **Depends on:** T0.1–T0.8
- **Suggested agent:** fork (owner keeps the decisions)
- **Steps:** fill the table below with concrete values. Phase 1+ tasks consume
  **only** these frozen values.

| Decision | Value (fill in) |
| --- | --- |
| Cluster API URL (seed) | `https://api.…:6443` |
| Derived cluster id | `api-…-6443` |
| `X-OLS-Cluster` header name | `X-OLS-Cluster` |
| URL canonicalization + id algorithm | (scheme/host/port rules + SHA suffix rule) |
| OLS namespace | `openshift-lightspeed` |
| OLSConfig apiVersion | `ols.openshift.io/v1alpha1` (confirm) |
| OLS LLM model id | `gemini-3.8-flash` (confirm) |
| Vertex ESO secret name / key | `llm-creds-vertex` / `apitoken` (confirm) |
| MCP hardening mode | A (passthrough) / B (OIDC) + custom image? |
| App-server SA / MCP SA | `lightspeed-app-server` / `openshift-mcp-server` |
| OLS inbound A2A audience | `lightspeed-a2a` (proposed) |
| Exchange client / audience | `lightspeed-mcp` / `openshift-mcp` |
| Praxis image + digest | `ghcr.io/praxis-proxy/…@sha256:…` |
| Praxis header-match syntax | (confirmed config snippet) |
| Praxis HTTPS-upstream syntax | (confirmed config snippet) |

- **Acceptance:** every row filled; blockers (T0.3, T0.8) resolved or escalated.
- **Verify:** no `TODO`/guess left in the table.
- **Rollback:** n/a.

---

## Phase 1 — Vendor lightspeed-service + add A2A

### T1.1 — Vendor the repo
- **Depends on:** T0.7, T0.9
- **Suggested agent:** general-purpose
- **Files:** `vendor/lightspeed-service/**`
- **Steps:** add a tracked snapshot of `openshift/lightspeed-service` at the
  pinned commit (git subtree or pinned snapshot + a `VENDOR.md` recording the
  upstream commit, license/attribution, and a refresh procedure). Strip any
  nested `.git`, venv, caches, secrets.
- **Acceptance:** repo present, builds from its root `Containerfile`, no nested
  VCS/secrets, `VENDOR.md` records provenance.
- **Verify:** container image builds in an ephemeral build (no host deps).
- **Rollback:** `git rm -r vendor/lightspeed-service`.

### T1.2 — Port Keycloak validation + SPIFFE + exchange helpers
- **Depends on:** T1.1
- **Suggested agent:** general-purpose
- **Files:** `vendor/lightspeed-service/ols/app/endpoints/a2a.py` (new, helpers
  module optional)
- **Steps:** port the behavior of `agents/rca_agent/rca_agent/identity.py`
  (`KeycloakTokenValidator`, `WorkloadIdentityProvider`/SPIFFE JWT-SVID fetch,
  `KeycloakTokenExchanger.exchange()` — RFC 8693, **no `client_id` form param**)
  into the vendored service. Validate inbound Token A against the realm JWKS
  (issuer, **`lightspeed-a2a` audience**, RS256, expiry, `azp=acme-agent`).
  Exchange as client `lightspeed-mcp` using this pod's JWT-SVID →
  `audience=openshift-mcp` (Token B).
- **Acceptance:** unit tests cover valid/expired/wrong-audience/wrong-azp tokens
  and a successful exchange (mock Keycloak).
- **Verify:** `pytest` in an ephemeral venv/container, all green.
- **Rollback:** delete the added module.

### T1.3 — Implement the A2A endpoint + well-known card
- **Depends on:** T1.2
- **Suggested agent:** general-purpose
- **Files:** `vendor/lightspeed-service/ols/app/endpoints/a2a.py`,
  `vendor/lightspeed-service/ols/app/routers.py` (register router)
- **Steps:** serve `/.well-known/agent-card.json` (registered **without** the
  `/v1` prefix, like health/metrics) advertising the **public Praxis HTTPS
  origin**, cluster-neutral. Implement the JSON-RPC methods ACME actually uses
  (from T0.7): `message/send`, task lookup, terminal/error responses (+
  streaming/cancel only if ACME uses them). Enforce: validate Token A (T1.2),
  require + check `X-OLS-Cluster` against this instance's configured id (reject
  missing/unknown), run exchange → inject **Token B** as the `"kubernetes"` MCP
  token for that single invocation, scope task/conversation state to caller +
  cluster id, and **block the `MCP-Headers` override**.
- **Acceptance:** endpoint returns a valid agent card; `message/send` drives the
  OLS pipeline; negative cases (no/invalid token, missing/unknown cluster,
  `MCP-Headers` injection attempt) are rejected.
- **Verify:** integration tests against a mocked MCP + mocked Keycloak;
  `include_router` present.
- **Rollback:** unregister the router; delete the module.

### T1.4 — Build & publish the vendored OLS image
- **Depends on:** T1.3
- **Suggested agent:** general-purpose
- **Files:** `vendor/lightspeed-service/VENDOR.md` (record image coordinates)
- **Steps:** build from the root `Containerfile` in an ephemeral builder, publish
  to an immutable **digest**, record SBOM/scan. This image is supplied to the
  operator via `--service-image` (T2.2).
- **Acceptance:** image pushed, digest recorded, scan clean.
- **Verify:** `skopeo inspect`/`oc image info` shows the digest.
- **Rollback:** untag; no cluster impact yet.

---

## Phase 2 — Operator + OLSConfig (Gemini) in a test namespace

### T2.1 — Lightspeed Operator subscription + namespace
- **Depends on:** T0.9
- **Suggested agent:** general-purpose
- **Files:** `variants/standalone/values-standalone.yaml`
  (`clusterGroup.namespaces` + `subscriptions`), new namespace
  `openshift-lightspeed`
- **Steps:** add the operator Subscription (coordinates from T0.2) and the
  `openshift-lightspeed` namespace, following the existing subscription style in
  the values file (e.g. `ztwim`, `rhbk`). Do **not** remove existing apps.
- **Acceptance:** Argo installs the operator; CSV `Succeeded`.
- **Verify:** `oc get csv -n openshift-lightspeed`.
- **Rollback:** remove the subscription block; Argo prunes.

### T2.2 — `openshift-lightspeed-config` chart (OLSConfig + Vertex secret)
- **Depends on:** T2.1, T0.3, T0.4, T0.5, T1.4
- **Suggested agent:** general-purpose
- **Files:** `charts/all/openshift-lightspeed-config/**` (new chart:
  `Chart.yaml`, `values.yaml`, `templates/olsconfig.yaml`,
  `templates/externalsecret-vertex.yaml`, `templates/networkpolicy.yaml`,
  plus Keycloak-CA + SPIFFE wiring); add the application to
  `variants/standalone/values-standalone.yaml`
- **Steps:** render the `OLSConfig` per MP §4 (`google_vertex`,
  `googleVertexConfig.{projectID: redhat-marketplace-dev, location: global}`,
  `models[].name: <frozen model id>`, `credentialsSecretRef`,
  `ols.defaultProvider/defaultModel`, `introspectionEnabled: true`). Create the
  ESO `ExternalSecret` mapping Vault `secret/data/global/llm-creds-vertex`
  property `credentials` → the operator-expected key (T0.4). Set the vendored OLS
  image via the operator `--service-image` mechanism (T0.2). Add a NetworkPolicy
  restricting OLS ingress to Praxis (same-namespace / gateway only), mirroring
  the `rca-agent` NetworkPolicy approach.
- **Acceptance:** `OLSConfig` reconciles; app-server + introspection MCP pods
  Ready; a direct OLS query returns a Gemini completion.
- **Verify:** `oc get olsconfig`, `oc get pods -n openshift-lightspeed`; a test
  query via the stock `/v1` path (k8s auth) succeeds.
- **Rollback:** remove the application + chart; Argo prunes.

### T2.3 — Harden the operator-managed MCP
- **Depends on:** T2.2, T0.3
- **Suggested agent:** general-purpose
- **Files:** depends on T0.3 outcome — either operator config values in
  `charts/all/openshift-lightspeed-config/` or a hardened MCP image supplied via
  `--openshift-mcp-server-image`
- **Steps:** apply the chosen hardening (MP §5): `require_oauth=true`, **no
  pod-SA fallback**, `cluster_auth_mode=passthrough`, `read_only=true`,
  restricted toolset, Secrets/RBAC denied. For Mode B also set
  `authorization_url`/`oauth_audience=openshift-mcp`/`certificate_authority`
  (mount Keycloak CA). Do **not** enable any MCP-side token exchange (OLS
  supplies Token B). **Do not attempt to fix the operator's permissive
  same-namespace MCP NetworkPolicy by adding a second, stricter NetworkPolicy
  in this chart** — Kubernetes NetworkPolicies are additive, so the operator's
  permissive policy keeps applying regardless of ours; if that policy is in
  scope to fix, it must go through the operator's own config/patch mechanism,
  same as the `require_oauth` setting itself.
- **Acceptance:** an MCP call with **no** bearer is rejected (no SA fallback);
  with Token B it is accepted and RBAC-limited.
- **Verify:** negative + positive MCP calls against the test deploy.
- **Rollback:** revert to the operator defaults (and re-flag the blocker).

---

## Phase 3 — Keycloak clients, mappers, RBAC

### T3.1 — Define the `lightspeed-mcp` client + ACME mappers
- **Depends on:** T0.6, T0.9
- **Suggested agent:** general-purpose
- **Files:** `charts/all/keycloak-oidc/templates/keycloak-realm-import.yaml`,
  `charts/all/keycloak-oidc/values.yaml`,
  `variants/standalone/values-standalone.yaml` (overrides)
- **Steps:** add a confidential, SPIFFE-federated client `lightspeed-mcp`
  (`clientAuthenticatorType: federated-jwt`, `jwt.credential.sub =
  spiffe://<trust-domain>/ns/openshift-lightspeed/sa/lightspeed-app-server`),
  `standard.token.exchange.enabled: "true"`, an `openshift-mcp` client audience
  mapper, and a `groups` mapper (mirror `rca-agent-mcp`). On the `acme-agent`
  client add a `lightspeed-a2a` audience mapper + a `lightspeed-mcp` client
  audience mapper. Keep existing RCA clients/audiences during overlap. (MP §8.)
- **Acceptance:** the realm import template renders the new client + mappers.
- **Verify:** `helm template` shows the client; schema valid.
- **Rollback:** revert the template/values.

### T3.2 — Least-privilege OCP RBAC for `keycloak:acme-agent-rca`
- **Depends on:** T0.9
- **Suggested agent:** general-purpose
- **Files:** `charts/all/openshift-lightspeed-config/templates/rbac.yaml` (or the
  keycloak-oidc chart, matching existing convention)
- **Steps:** create namespace-scoped `Role`/`RoleBinding`s for group
  `keycloak:acme-agent-rca` in exactly the target namespace(s) (start from the
  existing `payments` allow-list), granting only the read verbs the chosen MCP
  toolset needs (MP §5). No `view`/cluster-admin, no Secret/RBAC access, no
  writes. Keep `openshift-mcp` in `openshiftOIDC.extraAudiences`.
- **Acceptance:** `oc auth can-i` as the mapped group confirms exactly the
  intended verbs and denies out-of-scope resources/namespaces.
- **Verify:** `oc auth can-i --as=... --as-group=keycloak:acme-agent-rca ...`.
- **Rollback:** remove the RBAC objects.

### T3.3 — ~~Idempotent realm migration~~ (dropped — greenfield only)
Not applicable. There is no live realm to migrate; `KeycloakRealmImport` renders
`lightspeed-mcp` and the new `acme-agent` mappers on first sync, same as every
other client, as part of T3.1. No migration script, no realm backup/replacement
procedure, no run-twice idempotency check needed.

### T3.4 — Verify real Token A / Token B claims end to end
- **Depends on:** T3.1, T3.2, T2.3
- **Suggested agent:** general-purpose
- **Steps:** mint a real Token A (as `acme-agent` via SPIFFE) and perform the
  exchange as `lightspeed-mcp` → Token B. Confirm claims: Token A
  `azp=acme-agent`, `aud` includes `lightspeed-a2a`; Token B `sub` preserved,
  `azp=lightspeed-mcp`, `aud=openshift-mcp`, `groups=[acme-agent-rca]`. Confirm
  the API server authorizes Token B per T3.2 RBAC.
- **Acceptance:** claims match MP §1/§8; RBAC behaves as designed.
- **Verify:** decode both tokens; `oc auth can-i` with Token B.
- **Rollback:** n/a (verification).

---

## Phase 4 — Wire A2A end to end (OLS side)

### T4.1 — Deploy the vendored OLS image with A2A enabled
- **Depends on:** T1.4, T2.2, T3.4
- **Suggested agent:** general-purpose
- **Files:** `charts/all/openshift-lightspeed-config/` (image pin + any A2A
  config/env), SPIFFE registration for `lightspeed-app-server`, Keycloak CA mount
- **Steps:** point the operator `--service-image` at the vendored digest (T1.4);
  ensure the app-server SA gets a SPIFFE JWT-SVID (ZTWIM/SPIRE registration for
  `ns/openshift-lightspeed/sa/lightspeed-app-server`) and the Keycloak CA is
  mounted for JWKS/exchange TLS (mirror `rca-agent`'s CA-sync pattern).
- **Acceptance:** A2A `/.well-known/agent-card.json` served; a direct
  (in-cluster, authorized) `message/send` with Token A + valid `X-OLS-Cluster`
  reaches MCP with Token B and returns an answer.
- **Verify:** curl the card; an authorized A2A call succeeds; MCP logs show
  Token B.
- **Rollback:** revert the image pin to a no-A2A build.

### T4.2 — Negative-path tests at the OLS boundary
- **Depends on:** T4.1
- **Suggested agent:** general-purpose
- **Steps:** exercise: no/expired/wrong-audience/wrong-azp Token A → rejected;
  missing/unknown `X-OLS-Cluster` → rejected; `MCP-Headers` injection → ignored;
  confirm **only Token B** ever reaches MCP and never a pod SA token.
- **Acceptance:** all negative cases fail closed; no token leakage.
- **Verify:** test matrix green; MCP never called with Token A or SA token.
- **Rollback:** n/a.

---

## Phase 5 — ACME: cluster-URL requirement + routing header

### T5.1 — System prompt requires the target cluster URL
- **Depends on:** T0.9
- **Suggested agent:** general-purpose
- **Files:** `agents/acme_agent/src/acme_agent/agent.py`
- **Steps:** update the root-agent instruction to require the user to state the
  target OpenShift cluster **API URL** and to ask a clarifying question when it's
  missing/ambiguous — never infer from a resource name, never default (MP §6).
- **Acceptance:** prompt text updated; agent asks when URL absent.
- **Verify:** unit/behavioral test with a URL-less prompt → clarification.
- **Rollback:** revert the instruction.

### T5.2 — Server-side validator + cluster-id derivation
- **Depends on:** T0.9, T5.1
- **Suggested agent:** general-purpose
- **Files:** `agents/acme_agent/src/acme_agent/` (new validator module),
  `config.py`, tests
- **Steps:** implement canonicalization + allow-list lookup against the GitOps
  cluster registry (T6.1) and the frozen id algorithm (T0.9): reject non-https,
  credentials/path/query, and unknown URLs; derive the DNS-safe id (MP §6).
  Because `ui.py` is one-shot, require the URL on every message.
- **Acceptance:** valid URL → correct id; unknown/malformed → rejected with no
  downstream call.
- **Verify:** unit tests incl. the example
  `https://api.bm-cluster.e2e.bos.redhat.com:6443` →
  `api-bm-cluster-e2e-bos-redhat-com-6443`.
- **Rollback:** remove the validator.

### T5.3 — Invocation-scoped `X-OLS-Cluster` transport
- **Depends on:** T5.2
- **Suggested agent:** general-purpose
- **Files:** `agents/acme_agent/src/acme_agent/{agent.py,auth.py}` (+ transport
  adapter if needed)
- **Steps:** emit `X-OLS-Cluster: <id>` on the **authenticated** A2A RPCs only,
  invocation/task-scoped (no process-global default; concurrent conversations
  must not swap clusters), and **not** on the unauthenticated card fetch. The
  single shared `httpx.AsyncClient` + `auth.add_auth` event hook complicates
  this — use a `ContextVar`-driven hook or a narrow transport adapter if ADK's
  `RemoteA2aAgent` can't carry a request-scoped header (MP §6).
- **Acceptance:** header present on RPCs, absent on card fetch; two concurrent
  callers to different clusters never cross-talk.
- **Verify:** concurrency test asserting per-call header isolation.
- **Rollback:** revert transport changes.

### T5.4 — ACME chart config from the registry
- **Depends on:** T6.1, T5.3
- **Suggested agent:** general-purpose
- **Files:** `charts/all/acme-agent/**`,
  `variants/standalone/values-standalone.yaml`
- **Steps:** render ACME's allowed cluster URLs→ids from `global.olsClusters`
  (T6.1). Keep `a2a.remoteAgents[0].endpoint` pointed at the **single Praxis
  Route** (do not point at per-cluster OLS). Rebuild + pin the ACME image by
  digest. Update `a2a.remoteAgents[0].name/description` for Lightspeed.
- **Acceptance:** chart renders; ACME deploys and loads the registry.
- **Verify:** `helm template`; pod logs show the registry loaded.
- **Rollback:** revert overrides/image pin.

---

## Phase 6 — Praxis: image resolution (Core v0.7.2) + header routing

### T6.1 — Shared cluster registry values
- **Depends on:** T0.9
- **Suggested agent:** general-purpose
- **Files:** `variants/standalone/values-standalone.yaml` (`global.olsClusters`),
  consumed by both ACME (T5.4) and Praxis (T6.3)
- **Steps:** add the registry per MP §7 — for each cluster: `apiURL`,
  `upstreamHost`, `upstreamPort`, `upstreamSNI`. One entry now
  (`api-bm-cluster-e2e-bos-redhat-com-6443` → the in-cluster OLS app-server).
- **Acceptance:** registry present; both charts can template from it.
- **Verify:** `helm template` for both charts resolves the values.
- **Rollback:** remove the block.

### T6.2 — Switch Praxis to the resolved Core-v0.7.2-embedding `ai` image
- **Depends on:** T0.8
- **Suggested agent:** general-purpose
- **Files:** `charts/all/praxis-proxy/values.yaml`,
  `variants/standalone/values-standalone.yaml`
- **Steps:** switch the Praxis image to whatever T0.8 resolved — a published
  `ai` release embedding Core v0.7.2, or the in-house build/publish of a pinned
  `ai` source + `Cargo.lock` resolving Core to v0.7.2 (`ghcr.io/praxis-proxy/…@sha256:…`,
  pinned by digest; it must carry the `identity/jwt` plugin). **If T0.8
  concluded no compatible image is available, stop here, leave the current
  `ai:0.4.1` pin in place, and flag it in the final report — do not block the
  rest of Phase 6 indefinitely on an unresolved build.** Re-validate
  `files/policy.yaml` loads unchanged and `insecure_options`/`allow_private_idp`
  still behave (PR #1282). Keep the JWT validation + `claim.client_id ==
  acme-agent` allow-list.
- **Acceptance:** Praxis starts on the resolved image with the existing
  single-route config; JWT policy still enforces as before (no behavior change
  yet); Core version + `ai` version/source commit + digest recorded separately.
- **Verify:** pod Ready; an authorized request still proxies; an unauthorized one
  is still rejected.
- **Rollback:** revert the image pin to the pinned `ai:0.4.1` digest.

### T6.3 — Header-matched routing + HTTPS upstream
- **Depends on:** T6.1, T6.2, T4.1
- **Suggested agent:** general-purpose
- **Files:** `charts/all/praxis-proxy/files/praxis.yaml`,
  `charts/all/praxis-proxy/files/policy.yaml` (if needed),
  `variants/standalone/values-standalone.yaml`
- **Steps:** replace the single catch-all route with **per-cluster
  header-matched** routes (match `X-OLS-Cluster` exactly → the static upstream
  from the registry), using the v0.7.2 header-match + HTTPS-upstream syntax
  confirmed in T0.8. Configure upstream TLS (CA/SNI) for the OLS app-server's
  serving cert — the existing `SSL_CERT_FILE` (Keycloak JWKS trust) is **not** an
  upstream CA. Fail closed on absent/unknown/duplicate header (no default
  backend, no user-URL routing, no HTTP redirect). Authorization (JWT) still runs
  **before** routing. (MP §7.)
- **Acceptance:** request with the valid header → correct OLS; absent/unknown
  header → rejected; JWT still enforced first.
- **Verify:** route tests against a fake backend, then against the real OLS.
- **Rollback:** restore the single catch-all `praxis.yaml`.

---

## Phase 7 — Integration and direct replacement (greenfield — no cutover gate)

### T7.1 — Full-path dry run
- **Depends on:** T4.2, T5.4, T6.3
- **Suggested agent:** general-purpose
- **Steps:** drive a full `acme → praxis → OLS → MCP → API` request end to end in
  a test namespace. Run the cross-phase negative-test matrix (MP §11):
  missing/unknown cluster URL → no downstream call; absent/unknown/duplicate
  `X-OLS-Cluster` → Praxis fail-closed; wrong-audience Token A → denied at both
  Praxis and OLS; MCP without bearer → rejected (no SA fallback); Token B
  without the group / out-of-scope namespace → RBAC denies; two interleaved
  callers → no cross-talk.
- **Acceptance:** happy path returns an answer; every negative case fails closed.
- **Verify:** full matrix green; logs confirm token/header isolation.
- **Rollback:** n/a (nothing live depends on this yet).

### T7.2 — AnalysisResult parity decision
- **Depends on:** T7.1
- **Suggested agent:** general-purpose + user decision (MP §12 Q6)
- **Steps:** compare RCA's structured diagnosis/remediation output contract
  against OLS's answer. If structured parity is a product requirement, agree an
  OLS equivalent **before** T8.1 removes RCA.
- **Acceptance:** explicit go/no-go recorded; if parity required, implemented.
- **Verify:** sample outputs compared.
- **Rollback:** n/a.

---

## Phase 8 — Direct replacement & cleanup (greenfield — no staged cutover)

### T8.1 — Wire the new path as the only path
- **Depends on:** T7.1 (and T7.2 if parity required)
- **Suggested agent:** general-purpose
- **Files:** `variants/standalone/values-standalone.yaml`
- **Steps:** since this is a greenfield deployment, there's no live traffic to
  "flip" — Praxis's upstream and ACME's remote-agent target are simply
  configured to point at the OLS path from the start (this should already be the
  end state produced by Phases 2/5/6; this task is the final confirmation pass,
  not a separate switchover).
- **Acceptance:** `variants/standalone/values-standalone.yaml` has no remaining
  reference pointing ACME/Praxis at the old `rca-agent` path.
- **Verify:** `helm template` the full standalone variant; confirm only the OLS
  path is wired.
- **Rollback:** n/a.

### T8.2 — Remove obsolete components
- **Depends on:** T8.1
- **Suggested agent:** general-purpose
- **Files:** delete `agents/rca_agent/`, `charts/all/rca-agent/`,
  `charts/all/openshift-mcp-server/`; remove their applications + `rca-agent`
  litellm entry from `values-secret.yaml.template`; prune stale RCA/AgenticRun
  docs
- **Steps:** remove the old apps directly — no live instance depends on them.
  The `lightspeed-agentic-operator` app / `AgenticRun` RBAC / VAP /
  `llm-creds-openai` are removed **only** if a dependency audit shows nothing
  else uses them (still a separate, verified check — greenfield removes the need
  for a staged cutover, not the need to confirm nothing else depends on these).
  (MP §9.)
- **Acceptance:** Argo prunes cleanly; no dangling references.
- **Verify:** `grep -r rca-agent`/`openshift-mcp-server` finds no live refs;
  `helm template` the standalone variant cleanly.
- **Rollback:** restore from git history if something else turns out to depend
  on a removed piece.

### T8.3 — Docs
- **Depends on:** T8.1
- **Suggested agent:** general-purpose
- **Files:** `agents/AUTHENTICATION.md`, relevant `README.md`s
- **Steps:** update the sequence/token-exchange diagrams, the two-token flow, the
  header-routing/registry model, and a runbook for the OLS path. (MP §9.)
- **Acceptance:** docs describe the new flow accurately.
- **Verify:** peer read against the implemented behavior.
- **Rollback:** n/a.

---

## Dependency graph (quick reference)

```
T0.1..T0.8 ─► T0.9 (freeze) ─► everything below

Phase 1: T1.1 ► T1.2 ► T1.3 ► T1.4
Phase 2: T2.1 ► T2.2 ► T2.3            (T2.2 also needs T1.4)
Phase 3: T3.1 ► T3.4 ;  T3.2 ► T3.4   (T3.4 also needs T2.3; T3.3 dropped — greenfield)
Phase 4: T4.1 (needs T1.4,T2.2,T3.4) ► T4.2
Phase 5: T5.1 ► T5.2 ► T5.3 ► T5.4 (T5.4 needs T6.1)
Phase 6: T6.1 ; T6.2 (needs T0.8) ; T6.3 (needs T6.1,T6.2,T4.1)
Phase 7: T7.1 (needs T4.2,T5.4,T6.3) ► T7.2
Phase 8: T8.1 (needs T7.1[/T7.2]) ► T8.2 ; T8.1 ► T8.3
```

## Definition of done (whole migration)
- `acme → praxis → OLS → MCP → API` serves the standalone variant via the OLS
  path, using Gemini-on-Vertex, with Praxis on an `ai` image whose Core
  dependency is v0.7.2 (pinned digest) — **or**, if T0.8 found no such image
  obtainable/buildable in scope, on the previous pinned image with the gap
  explicitly flagged rather than silently dropped.
- Every MP §10 security property holds (verified by the T7.1 negative matrix).
- Old RCA + standalone MCP removed (T8.2); docs updated (T8.3).
- No host deps installed; no secrets in git; all images pinned by digest;
  break-glass intact.
- Greenfield only: no migration script, no staged cutover, no rollback path was
  built or is needed — the new components are the pattern's only wiring from the
  start.

## References
- Design + rationale + verified upstream facts: **`LIGHTSPEED_DESIGN.md`**.
- Praxis: Core `praxis-proxy/praxis@v0.7.2` (commit `1de023c`) vs. the deployed
  `praxis-proxy/ai` image line (`ai:0.4.1` resolves Core to 0.7.0; `ai` `main`
  resolves to 0.7.1; no published `ai` embeds Core 0.7.2 yet) — see MP §7 and
  MP §13 for the corrected build-or-wait plan.
- Cross-validated against the independently written plan pair on branch
  `rca-praxis-ols` (commit `15e26a55`, `LIGHTSPEED_DESIGN.md` /
  `LIGHTSPEED_IMPLEMENTATION_PLAN.md`), which is where the Praxis Core-vs-`ai`
  lockfile finding and the MCP NetworkPolicy additive-policy gap (MP §5) came
  from.
