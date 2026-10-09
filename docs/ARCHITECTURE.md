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

This branch's actual payload is an **agentic OpenShift investigation
pattern**: a chat agent (ACME, built on Google's ADK) that answers "what's
wrong with my cluster" questions by delegating to OpenShift Lightspeed
(OLS), which uses MCP tools to actually query the live cluster -- with the
caller's identity propagated end-to-end through Keycloak + SPIFFE + an RFC
8693 token exchange, so the resulting RBAC check is attributed to the real
caller, not to OLS's own service account. A Praxis Policy Engine gateway
sits in front of OLS enforcing the caller allow-list.

This replaces an earlier "RCA agent" + OPA-based pattern that lived in this
same repo; you may still see comments/leftovers referencing "RCA" or
`charts/all/opa` (present but **not wired into any Application** --
leftover, not live).

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
  - `mao/*`, `rhoai-config` -- unrelated demo/platform additions bundled
    into this same branch; not part of the OLS/A2A investigation flow.
- `agents/acme_agent/` -- the ACME agent's Python source (ADK-based A2A
  client). `agents/AUTHENTICATION.md` is the authoritative, detailed
  write-up of the full per-request identity chain with real captured claim
  shapes and a substantial Troubleshooting section -- read it before
  debugging any auth failure in this flow. `agents/README.md` is a short
  index.
- `vendor/lightspeed-service/` -- a tracked snapshot of upstream
  `openshift/lightspeed-service`, extended with a hand-rolled A2A endpoint
  (`ols/app/endpoints/{a2a.py,a2a_auth.py}`) since upstream has **no real
  A2A support** (the one attempt, PR #2866, was closed unmerged with zero
  reviews and had no auth/identity story anyway -- don't assume a future
  upstream version adds this for free). `VENDOR.md` documents the pin,
  every local deviation from upstream, and every local-build issue found
  and fixed -- update it when you touch this directory, including when you
  discover a new build issue (several were found only by actually attempting
  a local build; Konflux's hermetic CI path never exercises them).
- `vendor/praxis-ai/` -- untracked, reference-only checkout of Praxis's
  source, used only while writing APL policy syntax. The deployed
  `praxis-proxy` chart pins a public upstream image; this directory is not
  built from and does not need to exist for a deployment to work.

## Core architecture decisions (don't relitigate these without reason)

- **The identity chain is the point.** ACME authenticates to Keycloak as
  itself (SPIFFE JWT-SVID, `client_credentials`), gets "Token A", sends it
  through Praxis to OLS, and OLS performs an RFC 8693 token exchange
  (`subject_token=Token A`) to get "Token B" scoped to `openshift-mcp` --
  Token B's `sub` is unchanged from Token A, so the eventual API server
  RBAC check is attributed to the original caller. Token A never reaches
  MCP or the API server; only Token B does. See `agents/AUTHENTICATION.md`.
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
  nor the `A2A_*` env vars the vendored service needs have a home in the
  `OLSConfig` CRD today -- the operator picks the image via its own
  `--service-image` flag and generates the app-server Deployment's env
  entirely from its own code. `appServerPatch` polls for that
  operator-managed Deployment and patches it directly, then re-asserts
  periodically because the operator's own reconcile loop can revert it.
  Delete the whole mechanism once the Operator/OLSConfig CRD natively
  supports this -- don't build more features on top of it as if it were
  permanent.
- **Least-privilege RBAC is explicit-namespace by default.**
  `lightspeedRbac.namespaces` (per-namespace Role/RoleBinding) is the
  normal path; `lightspeedRbac.allNamespaces` (cluster-wide
  ClusterRole/ClusterRoleBinding) is an opt-in, off-by-default escape hatch
  for sandbox/demo clusters, documented as a real widening of blast radius,
  not a convenience default.

## Hard-won lessons (verified live, not theoretical)

These were each found by actually deploying this pattern to a real
OpenShift cluster for the first time -- the design docs didn't anticipate
them, and nothing short of a live cluster surfaced them. If you're
touching adjacent code, re-read the relevant one.

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
  `agents/acme_agent/src/acme_agent/auth.py` and
  `charts/all/keycloak-oidc/templates/realm-secrets-reconciler-job.yaml`;
  apply the same pattern to any *new* code that authenticates to a public
  Route, not to code that only ever talks to the in-cluster API server.
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
- **Don't trust a hardcoded path against a vendored backend without
  checking the vendored source.** `praxis-proxy`'s `/health/ready` route
  was copied from a predecessor chart's backend and silently 404'd against
  the real `vendor/lightspeed-service` (whose actual route is `GET
  /readiness`, no prefix) -- undetected until a real backend was finally
  reachable to test against. If a chart assumes a path/port/label on a
  vendored or operator-managed component, grep the actual vendored source
  (or `oc get`/`oc explain` the live cluster) before trusting it, even if a
  README or comment asserts it confidently.
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
- **Token B `groups` survive Keycloak's standard V2 exchange.** Verified live:
  mint Token A in `acme-agent` (SPIFFE `client_credentials`), exchange in
  `lightspeed-app-server` (RFC 8693, `lightspeed-mcp` SPIFFE assertion) --
  Token B carries `groups: ["acme-agent-rca"]`, `aud: "openshift-mcp"`,
  `azp: "lightspeed-mcp"`, `sub` unchanged, no `act` claim. Keep the
  belt-and-suspenders `groups` mappers on both clients; re-verify after
  realm/mapper changes (see `charts/all/keycloak-oidc/README.md`
  "End-to-end verification" and `agents/AUTHENTICATION.md` Grant 2).
