# Architecture & operational notes

The deep background for this repo: what the pattern actually does, the
architecture decisions behind it, and the hard-won lessons from deploying it
to real clusters. [AGENTS.md](../AGENTS.md) is the concise orientation and the
"where do I change X" map; this file is the "why". Chart-specific caveats live
in each `charts/all/<name>/README.md`; the human deployment walkthrough is in
[README.md](../README.md).

## What this repo is

`multicloud-gitops` is a Red Hat Validated Patterns GitOps repo: ArgoCD's
"clusterGroup" pattern deploys a fixed set of Helm charts (`charts/all/*`)
as Applications, parameterized per-cluster by `variants/<variant>/values-
<variant>.yaml` (currently only `standalone` is live; `hub` exists but is
not the focus of this work).

This branch's actual payload is an **agentic Red Hat assistance pattern**: a
chat agent (ACME, built on Google's ADK) delegates Red Hat-related questions
to the MAO workflow. MAO can use Ask Red Hat for product, project, and
documentation guidance, OpenShift Lightspeed (OLS) for cluster investigation,
or both. OLS uses MCP tools to query the live cluster -- with the caller's
identity propagated end-to-end through Keycloak + SPIFFE + an RFC 8693 token
exchange, so the resulting RBAC check is attributed to the real caller, not to
OLS's own service account. A Praxis Policy Engine gateway sits in front of OLS
enforcing the caller allow-list.

This replaces an earlier "RCA agent" + OPA-based pattern that lived in this
same repo; you may still see comments/leftovers referencing "RCA" or a
former `charts/all/opa` chart, since removed (it was never wired into any
Application).

## Repository layout

- `README.md` -- the Phase 0-3 deployment walkthrough for a human operator.
  Keep it in sync with reality; it's been wrong before (see "Hard-won
  lessons" below) and a stale walkthrough actively misleads the next
  deployer.
- `LIGHTSPEED_DESIGN.md` -- the design doc this pattern was built from.
  Written entirely from research/documentation, **before any live cluster
  existed to test against** -- full of explicit "UNCONFIRMED GUESS"
  markers. Treat it as a hypothesis record, not ground truth; cross-check
  anything it asserts against the actual installed operator/CRD/cluster
  before relying on it, and update it (or the relevant chart) once you've
  actually confirmed something live.
- `variants/standalone/values-standalone.yaml` -- **the only place
  cluster-specific values belong.** Trust domain, issuer URLs, image refs,
  the `global.olsClusters` registry, OLM Subscription channel/source, RBAC
  namespace scope -- all of it. Never hardcode a real cluster's hostname or
  trust domain into a chart template or default `values.yaml`; those stay
  generic/empty/placeholder so the chart works on any cluster.
- `values-secret.yaml.template` -- Vault-backed secrets (via External
  Secrets Operator). Never commit a real secret value anywhere else.
- `charts/all/<name>/` -- one Helm chart per ArgoCD Application (mostly).
  Each has its own `README.md` with chart-specific caveats -- read it before
  touching that chart.
  - `keycloak-oidc` -- the `rca` realm (5 clients, SPIFFE federation,
    `groups` mappers), least-privilege MCP RBAC
    (`templates/lightspeed-mcp-rbac.yaml`), and the OpenShift Native OIDC
    `Authentication/cluster` resource.
  - `praxis-proxy` -- the policy-enforcing gateway in front of OLS. Routes
    by `X-OLS-Cluster` header; validates the caller's JWT and `azp`
    allow-list via the pinned `ghcr.io/praxis-proxy/ai` image (alpha
    upstream software with documented quirks -- see its README).
  - `openshift-lightspeed-config` -- the `OLSConfig` CR plus the
    `appServerPatch` bridge (see "Core architecture decisions" below).
  - `acme-agent` -- deploys the agent built from `agents/acme_agent/`.
  - `mao/mas-a2a` -- optional, separately authenticated inbound MAS A2A
    endpoint for a persisted orchestrator blueprint; its Application is
    registered but the chart renders no resources until explicitly enabled.
    Other `mao/*` and `rhoai-config` charts serve the demo/platform stack.
- `agents/acme_agent/` -- the ACME agent's Python source (ADK-based A2A
  client). `agents/AUTHENTICATION.md` is the authoritative, detailed
  write-up of the full per-request identity chain with real captured claim
  shapes and a substantial Troubleshooting section -- read it before
  debugging any auth failure in this flow. `agents/README.md` is a short
  index.
- OLS A2A app-server image -- built from the external fork
  [`luis5tb/lightspeed-service@a2a-split`](https://github.com/luis5tb/lightspeed-service/tree/a2a-split)
  (Keycloak validate → SPIFFE → RFC 8693 exchange → OLS pipeline), since
  upstream OpenShift Lightspeed has **no real A2A support** yet (the one
  attempt, PR #2866, was closed unmerged with zero reviews and had no
  auth/identity story -- don't assume a future upstream version adds this
  for free). Build/push steps live in the top-level `README.md` Phase 0;
  this pattern no longer vendors that tree.
- `vendor/praxis-ai/` -- untracked, reference-only checkout of Praxis's
  source, used only while writing APL policy syntax. The deployed
  `praxis-proxy` chart pins a public upstream image; this directory is not
  built from and does not need to exist for a deployment to work.

## Core architecture decisions (don't relitigate these without reason)

- **The identity chain is the point.** ACME authenticates to Keycloak as
  itself (SPIFFE JWT-SVID, `client_credentials`), gets "Token A", sends it
  through Praxis to OLS, and OLS (with its OIDC authentication module and
  optional A2A token exchange explicitly enabled for this pattern) performs
  an RFC 8693 token exchange (`subject_token=Token A`) to get "Token B"
  scoped to `openshift-mcp`. Token B's `sub` is unchanged from Token A, so
  the eventual API server RBAC check is attributed to the original caller.
  Token A never reaches MCP or the API server; only Token B does. See
  `agents/AUTHENTICATION.md`.
- **ACME provider selection is request-scoped, not a new trust decision.**
  The UI may choose local Keycloak or a Red Hat SSO production/stage
  confidential-client agent token. That choice only determines which
  configured credential mints Token A; MAS and OLS independently validate the
  resulting JWT's issuer, audience, signature, and principal. Red Hat SSO
  user-token login is a documented TODO, not a partially trusted shortcut.
- **On `a2a-split`, Praxis is the A2A caller allow-list.** OLS reuses its
  configured OIDC module to validate the JWT's signature, issuer and audience;
  it no longer checks a separate `A2A_INBOUND_AZP` allow-list. The same OIDC
  module also applies to the stock OLS REST API. Keep the Praxis policy and
  the OLS ingress NetworkPolicy intact; an OIDC-valid token alone is not
  proof that the caller was admitted by Praxis.
- **Inbound MAS A2A is a separate, dormant trust boundary.** It never routes
  through ACME/Praxis/OLS or uses OLS's token exchange. Its Keycloak mode
  validates a dedicated audience and exact subject allow-list, with private
  Keycloak Route CA trust added to (not substituted for) public roots; its
  separate public API must be hardened before activation. Do not invent a
  persisted blueprint ID, caller subjects, or a published image. See the MAO
  section in `README.md` and `charts/all/mao/mas-a2a/README.md`.
- **OpenShift Native OIDC (`openshiftOIDC.enabled`) is required, not
  optional**, for the final MCP tool call to work -- `claimMappings.groups`
  (what makes the Keycloak group RBAC binds to exist as an OpenShift group
  at all) only exists once `Authentication/cluster` is switched to
  `type: OIDC`. It's gated behind a break-glass-first sequence (README
  Phase 3) because flipping it cluster-wide, done wrong, locks out
  console/CLI login entirely -- but it is not skippable if you want the
  agent to actually reach the cluster.
- **`global.olsClusters` is the single shared cluster-routing registry.**
  ACME, Praxis, and `appServerPatch` must all render from this one map
  (`values-standalone.yaml`, top of file) -- never fork a second copy. The
  map key is a derived id, not a free-form name: `lower(host):port` with
  every `.`/`:` replaced by `-` (see "Hard-won lessons").
- **`appServerPatch` (in `openshift-lightspeed-config`) is a deliberate,
  temporary bridge**, not a real feature. Neither a custom app-server image
  nor the A2A configuration has a native home in the `OLSConfig` CRD today.
  The operator picks the image via its own `--service-image` flag and
  generates the app-server Deployment and ConfigMap. The bridge waits for
  provisioning and a successful CSV, verifies the operator is stopped at
  zero replicas, then patches the app-server config, image, environment,
  and CSI/CA mounts directly. A periodic re-assert cannot outrun the
  operator's reconciliation. Delete the bridge once the Operator/OLSConfig
  CRD natively supports the custom image and A2A configuration; don't build
  more features on top of it as if it were permanent.
- **Least-privilege RBAC is explicit-namespace by default.**
  `lightspeedRbac.namespaces` (per-namespace Role/RoleBinding) is the
  normal path; `lightspeedRbac.allNamespaces` (cluster-wide
  ClusterRole/ClusterRoleBinding) is an opt-in, off-by-default escape hatch
  for sandbox/demo clusters, documented as a real widening of blast radius,
  not a convenience default.

## Hard-won lessons (live findings and one local render failure)

The deployment findings below were found on a real OpenShift cluster -- the
design docs didn't anticipate them, and nothing short of a live cluster
surfaced them. The local render lesson is labeled separately. If you're
touching adjacent code, re-read the relevant one.

Local render lesson (not a live-cluster finding): always render the actual
`variants/standalone/values-standalone.yaml` after editing overrides; an
extra leading space on an OLS list item broke YAML parsing, while chart-only
tests using synthetic values could not catch the standalone-file error. Also
apply the per-application overrides when rendering a chart: merely loading the
clusterGroup file as Helm values does not enable its Application settings.

Local render lesson: Praxis multi-OIDC output is YAML nested inside a ConfigMap
string. Helm whitespace trimming once joined the last external JWKS URL to the
local issuer item; parse `data.policy.yaml` itself, not only the outer Helm YAML.

Live ACME logging finding (2026-10-29): uvicorn's default log config only
configures the `uvicorn.*` loggers, so every `acme_audit` INFO record was
dropped by Python's last-resort handler — a full pasted-token → 401 → renewal
flow left zero audit lines in the pod log, and the renewal could only be
proven from the MAS side (same X-Request-Id on its 401 and the retried 200).
The app now installs its own handler at import (`agent.py`); audit lines also
carry token fingerprints (`fp:…`) and `source=pasted_override|minted` so a
paste and its renewal are visible without the token values. Related: MAS's
`Failed to resolve signing key` ERROR is benign multi-issuer probe noise —
it appears on fully successful requests too; the real rejection signal is
`Token validation failed for all … issuers` + 401.

Live Praxis startup finding: enabling the static agent card originally rendered
`static_files` as a `load_balancer.clusters[]` field. The pinned
`ghcr.io/praxis-proxy/ai:0.4.1` image embeds Core 0.7.0, whose strict cluster
schema rejects that field and crash-loops the pod. The chart now serves the card
with Core's `static_response` filter after the policy filter; keep discovery
public through the explicit policy route and do not model files as LB clusters.

Live Praxis TLS finding: a same-cluster `global.olsClusters` entry was pointed
at the OLS public Route while Praxis trusted only the cluster service-ca; the
Route's ingress-CA certificate failed verification (`UnknownIssuer`) on the
readiness request. Use the internal OLS Service for same-cluster routing; remote
Routes require their own trusted ingress CA bundle (never turn verification off).

- **TLS trust: extend the default store, don't replace it.**
  `ssl.create_default_context(cafile=X)` and httpx's `verify=<path>` both
  *replace* the system's default trust store with only `X`. That's correct
  when the only valid certificate is signed by a known-fixed internal CA
  (e.g. anything hitting `https://kubernetes.default.svc` -- its cert
  always chains to the pod's own projected service-account CA, so an
  exclusive `cafile=` there is right, see `sync_keycloak_ca.py`,
  `sync_ingress_ca.py`, `patch_appserver.py`). It silently breaks
  verification for anything that might hit a **publicly-CA-signed** OpenShift
  Route (e.g. a sandbox cluster using ZeroSSL/Let's Encrypt for
  `*.apps.<cluster>`), because the synced managed-ingress-CA bundle may
  contain only the leaf's intermediate chain, not the actual trusted root.
  The fix is always: `ctx = ssl.create_default_context(); if bundle:
  ctx.load_verify_locations(cafile=bundle)`. Already fixed in
  `agents/acme_agent/src/acme_agent/auth.py`,
  `charts/all/keycloak-oidc/templates/realm-secrets-reconciler-job.yaml`,
  and the OLS A2A fork's `ols/app/endpoints/a2a_auth.py`
  (`A2ASettings.tls_verify` on
  [`luis5tb/lightspeed-service@a2a-split`](https://github.com/luis5tb/lightspeed-service/tree/a2a-split)).
   Apply the same pattern to any *new* code that authenticates to a public
   Route, not to code that only ever talks to the in-cluster API server.
- **MAS outbound A2A discovery also needs ingress-CA trust.** A workflow failed
  while MAS fetched the OLS/Praxis Agent Card from the cluster Route with
  `CERTIFICATE_VERIFY_FAILED` (`self-signed certificate in certificate chain`).
  The managed ingress CA was already synced to namespace `mao`, but API/worker
  pods in `providerMode=dev` did not mount it because CA trust was coupled to
  Keycloak identity mode. Enable the combined public-plus-ingress CA bundle on
  each MAS caller (API/worker, and A2A if it makes outbound calls); do not
  disable TLS verification. The local setup script's CA setting does not
  configure trust inside the pods. Praxis's service-ca bundle for its internal
  OLS upstream is a separate TLS hop and does not fix MAS-to-Route trust.
- **Red Hat SSO agent tokens need `openid` for Keycloak 26.4 exchange.**
  Keycloak's standard V2 exchange calls both introspection and userinfo after
  receiving an external subject token. With only `api.ask_red_hat`, the Red Hat
  client-credentials token passes introspection but Red Hat's userinfo endpoint
  returns 403, surfacing in OLS only as `Keycloak token exchange failed`.
  Request `openid api.ask_red_hat`, and configure OLS to send the external
  issuer URL as `subject_issuer`; this avoids depending on an imported IdP
  alias that can drift from chart values. The OLS image must also preserve
  `subject_token_type=access_token`; the `650f5486` image rewrites external
  tokens to `jwt`, which Keycloak 26.4 rejects.
- **External-token exchange must also map a Keycloak group.** A successful
  Red Hat SSO exchange that emits no `groups` claim makes OpenShift authorize
  the brokered subject as `keycloak:<uuid>`, while the pattern's RBAC targets
  `Group:keycloak:acme-agent-rca`. Configure each external provider's
  `hardcodedGroups` with `acme-agent-rca`; the PostSync reconciler matches the
  live IdP by issuer URL and adds the Hardcoded Group mapper even when the
  live alias has drifted.
- **Cluster-id derivation must match exactly, everywhere.** The algorithm
  (`agents/acme_agent/src/acme_agent/cluster_registry.py`'s
  `derive_cluster_id`) is `f"{host}:{port}".replace(".", "-").replace(":",
  "-")` -- lowercase host, every single `.` and `:` replaced, not just the
  first one. A hand-edited `global.olsClusters` key that misses an internal
  dot (e.g. `api-ocp.pgt8r.sandbox-6443` instead of
  `api-ocp-pgt8r-sandbox-6443`) makes acme-agent crash-loop with
  `ClusterURLError` *and* makes Praxis crash-loop separately at startup
  (Envoy rejects a cluster name containing a literal dot). Both symptoms,
  one root cause -- check this value first if either service won't start
  after an id change.
- **Check an operator's actual supported `installModes` before setting
  `targetNamespaces`.** `targetNamespaces: []` renders an AllNamespaces
  OperatorGroup; not every operator supports that (`oc get csv <name> -o
  jsonpath='{.spec.installModes}'` to check). `lightspeed-operator` only
  supports `OwnNamespace` -- an AllNamespaces OperatorGroup makes OLM
  permanently fail its CSV with `UnsupportedOperatorGroup`, and the
  operator never runs at all. Don't copy another operator's
  `clusterGroup.namespaces` entry in `values-standalone.yaml` without
  checking this.
- **`KeycloakRealmImport` only ever gets one real shot.** On a fresh
  install it can race the `Keycloak` CR's own creation (`keycloaks
  .k8s.keycloak.org "keycloak" not found`) and then sits in `HasErrors`
  forever -- it does not retry on its own even after the `Keycloak` CR
  exists and is `Ready`. Recovery is to delete it and let ArgoCD recreate
  it (`oc delete keycloakrealmimport <realm> -n keycloak-system`), not to
  wait or to patch its status. This recovery is now automated by the
  `realm-import-healer` CronJob in `charts/all/keycloak-oidc` (on by default,
  `realmImport.selfHeal.enabled`): it deletes the import only while it is in
  `HasErrors` and the `Keycloak` CR is `Ready`. Safe because deleting the
  import CR never deletes the realm (the import is a one-shot record; realm
  data lives in Keycloak's DB) and the `HasErrors` race fails before
  importing anything, so no user/client/session exists to lose.
- **Don't trust a hardcoded path against an external backend without
  checking the real source.** `praxis-proxy`'s `/health/ready` route was
  copied from a predecessor chart's backend and silently 404'd against
  the real OLS service (whose actual route is `GET /readiness`, no
  prefix) -- undetected until a real backend was finally reachable to
  test against. If a chart assumes a path/port/label on an external or
  operator-managed component, grep the actual source (or `oc get`/`oc
  explain` the live cluster) before trusting it, even if a README or
  comment asserts it confidently.
- **Quote image tags (and any other value) that could be all-digits.** An
  unquoted YAML scalar like `value: 34256970` (a git short SHA that happens
  to contain no letters) parses as a number, not a string, and a large
  all-digit value can come back out the other side of Helm/Argo's rendering
  pipeline re-serialized in scientific notation -- confirmed live: it became
  `3.425697e+07`, which then fails image-reference parsing
  (`InvalidImageName`) and silently blocks the ArgoCD sync (hung waiting on
  the hook Job using that image) with no obviously-related error at the
  Application level. Always quote `image.tag`-style overrides in
  `values-standalone.yaml`.
- **A PR existing upstream does not mean the feature exists upstream.**
  Before assuming some upstream project already solves a problem this
  pattern works around, check whether the PR actually merged
  (`gh pr view <n> --json mergedAt,reviews`) and whether its scope actually
  matches what's needed -- `lightspeed-service` PR #2866 added A2A protocol
  *transport* with zero authentication, which doesn't address this
  pattern's actual requirement (per-caller delegated identity) even
  hypothetically.
- **An operator's CSV `Succeeded` is not "done reconciling" -- don't pause it
  on that signal.** `appServerPatch` stops the lightspeed-operator (OLSConfig
  `Unmanaged` + scale-to-0) so its app-server patch isn't reverted, and
  originally gated that stop only on the CSV reaching `Succeeded`. `Succeeded`
  means *installed*, not that the operator has created every `OLSConfig` child.
  Observed live: the operator was frozen before it created the
  `lightspeed-postgres-bootstrap` Secret, so `lightspeed-postgres-server` stuck
  in `ContainerCreating` ("secret not found"), the app-server's
  `wait-for-postgres` init crash-looped, `Service/lightspeed-app-server` had no
  endpoints, and `praxis-proxy` went `Degraded` (connection-refused) -- yet
  `openshift-lightspeed-config` stayed `Synced`/`Healthy`, because ArgoCD
  tracks only the resources an Application owns, not operator-created ones, and
  `OLSConfig` has no health check. A stopped operator never self-heals. Fix:
  `patch_appserver.py` now waits for the postgres Deployment to be Available
  (proves the Secret exists and postgres started) before stopping, and
  un-pauses a previously-frozen operator to recover -- see
  `appServerPatch.postgresDeploymentName` and that chart's README
  "Provisioning gate". General rule: before pausing any operator, wait on a
  concrete child resource it must produce, not just the operator's own install
  status.
- **OLS/MCP pod labels match the chart selectors (confirmed live).**
  `appServer.podSelectorLabels` and `mcpHardening.podSelectorLabels` select
  real pods; `ClusterSPIFFEID/ols-app-server` and
  `NetworkPolicy/openshift-lightspeed-config-app-server` are not silent
  no-ops. The operator's own app-server NetworkPolicy still OR-allows
  monitoring, console, and cluster ingress -- this chart's NP cannot narrow
  that (see `charts/all/openshift-lightspeed-config` README).
- **Token B `groups` survive Keycloak's standard V2 exchange.** Verified live:
  mint Token A in `acme-agent` (SPIFFE `client_credentials`), exchange in
  `lightspeed-app-server` (RFC 8693, `lightspeed-mcp` SPIFFE assertion) --
  Token B carries `groups: ["acme-agent-rca"]`, `aud: "openshift-mcp"`,
  `azp: "lightspeed-mcp"`, `sub` unchanged, no `act` claim. Keep the
  belt-and-suspenders `groups` mappers on both clients; re-verify after
  realm/mapper changes (see `charts/all/keycloak-oidc/README.md`
  "End-to-end verification" and `agents/AUTHENTICATION.md` Grant 2).
- **Never let an LLM spell a transport header.** Observed live on the MAS
  orchestrator: the planning LLM (Gemini, temperature 0.7) emitted the
  routing header as `X_OLS_Cluster` -- underscores -- in 3 of 5 OLS
  delegations, while the blueprint prompt showed the correct hyphenated
  form. MAS forwards work-item `headers` verbatim on the outbound A2A
  POST, and Praxis matches the `X-OLS-Cluster` name exactly (fail-closed,
  no default route), so each misspelling became
  `no route matched path=/a2a` -> HTTP 404 and the delegation was silently
  lost -- intermittent ("sometimes works"), because the model spelling is
  sampled per request. Nothing caught it sooner because the cluster id
  *value* was always correct and praxis logs the 404 as a routine router
  decision. Models habitually normalize hyphenated dict/JSON keys to
  underscores, so any header whose name comes from model tool args is a
  latent intermittent failure. Mitigated in the blueprint prompt (explicit
  spelling rule + temperature 0, see `setup-mas-blueprint.sh`'s LIVE
  FINDING); the durable fix is deterministic header injection in the MAS
  image (canonicalize `X_OLS_*` -> `X-OLS-*`, or set it on the
  `a2a_agent_node` instead of via model tool args).
  **Recurrence (2026-10-23, live via scripts/monitor.sh):** the prompt
  mitigation reduced but did not eliminate it -- Praxis still 404'd 12 of
  41 `/a2a` POSTs in 72h *after* the temperature-0 blueprint re-save, and
  the worker's `tool.execute`/`applying_runtime_headers` lines still show
  `X_OLS_Cluster` in the args. The monitor's MISSPELLED ROUTING HEADER and
  PRAXIS NO ROUTE cards exist to catch exactly this; the durable in-image
  fix remains outstanding. Related trap found
  while fixing it: MAS `resource.save` rejects duplicates with 400
  "exists for user", so re-running `setup-mas-blueprint.sh` used to
  silently keep the stale resource config (a temperature change refused
  to apply); the script now upserts via `resource.update`.
- **Each MAS stage mints its own request id -- `request_id` does not join
  across the orchestrated flow.** Observed live (2026-10-23): the MAO A2A
  endpoint's middleware logged its own dashed-UUID `request_id` for an
  inbound POST, while OLS's `a2a_audit` for the same request (correlated
  by time) logged a different hex id; ACME's `X-Request-Id` never
  reappears downstream of MAS. The ACME↔OLS `request_id` join documented
  in `agents/AUTHENTICATION.md` only works for the Phase-1 direct flow;
  with MAS in the path, trace by time window (every card in
  `scripts/monitor.sh` carries a wall-clock timestamp) or by Keycloak
  Realm Events, not by id.
- **Red Hat SSO client-credentials requires service accounts enabled.**
  Observed live as an ADK agent-card 401 that actually came from the SSO token
  endpoint: `Client not enabled to retrieve service account`. External-secret
  syncing and local mocked-token tests cannot detect that IdP policy, so ACME
  now surfaces the OAuth error description.
- **Red Hat SSO's principal is `sub`, not the client UUID.** MAS A2A authorizes
  the validated token's `user_id`/`sub`; a Red Hat `client_credentials` token's
  `sub` is its service-account principal and can differ from `azp`/`client_id`.
  Observed live as a 403 after signature and audience validation succeeded:
  the standalone allow-list had been populated with the OAuth client UUID.
  Decode a real non-secret test token and copy only its `sub` into
  `a2a.allowedPrincipals`.
