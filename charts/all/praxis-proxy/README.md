# Praxis authorization gateway

This chart deploys the [Praxis AI proxy](https://github.com/praxis-proxy/ai)
with its embedded [Praxis Policy Engine (PPE)](https://github.com/praxis-proxy/policy)
as a public, policy-enforcing A2A gateway. The public OpenShift Route targets
this proxy; the proxy validates the inbound Keycloak/external-SSO access
token, authorizes the caller with native APL, passes A2A discovery through
to its backend, and answers readiness itself.

> Historical note: this chart previously routed per-request to OpenShift
> Lightspeed (OLS) app-servers selected by an `X-OLS-Cluster` header, with
> one router route and one `load_balancer` cluster per entry in a shared
> `global.olsClusters` registry. The OLS backend was removed
> (`PLAN-lightspeed-hub-migration.md` Task A), so the chart currently
> configures **no upstream clusters**; a future backend (Task B,
> lightspeed-hub) re-introduces routed upstreams via `a2aUpstream` below.

> **Pinned-image quirks:** the pinned image is alpha software. Constraints on
> `files/praxis.yaml` / `files/policy.yaml` (no static agent cards, fatal
> empty-router config, `delegator/oauth` shape, multi-valued header limits,
> the historical fail-open allow-list) and how each was found are in
> [`docs/LESSONS_LEARNED.md` §5](../../../docs/LESSONS_LEARNED.md#5-praxis-gateway-pinned-image-quirks).

## Agent card (pass-through, no static card)

Praxis has **no agent card of its own**. With `a2aUpstream.enabled`, the
router forwards `GET /.well-known/agent-card.json` and
`GET /a2a/.well-known/agent-card.json` (MAS resolves the card at
`{base_url}/.well-known/agent-card.json`, and its base URL ends in `/a2a`)
to the `hub-adapter` cluster **unchanged**, so callers see the
lightspeed-hub adapter's own card. With `a2aUpstream` disabled there is no
agent behind the gateway, and discovery answers 404. The policy filter
exempts only `GET` on those two exact paths from authentication.

Callers pick their JSON-RPC transport *and method names* from this card, so
serving a hand-maintained copy at the gateway broke every delegation once
(card advertised A2A 1.0, adapter speaks 0.3). Do not add a static card --
see [`docs/LESSONS_LEARNED.md` §5](../../../docs/LESSONS_LEARNED.md#5-praxis-gateway-pinned-image-quirks).

Contract with the backend (lightspeed-hub chart / a2a-adapter):

- The adapter serves its card at **both** paths
  (`a2a-adapter/internal/server/card.go` `AgentCardPaths`): the pinned
  image has no path-rewrite filter, so Praxis cannot map `/a2a/...` to
  `/...` itself.
- The card's `url` must be the public Praxis endpoint
  (`https://praxis-proxy.apps.<base>/a2a`); the lightspeed-hub chart
  derives it (`adapter.agentCard.url` overrides). a2a-sdk only maps a 0.3
  card's `protocolVersion` onto an interface when `url` is non-empty;
  with an empty url MAS falls back to the v1.0 transport again.
- The card routes are the only header-less routes, listed before the
  per-spoke routes. Because they are prefix matches, an authenticated
  request to e.g. `/a2a/.well-known/agent-card.json/x` would reach the
  adapter's catch-all JSON-RPC handler without the router's target-header
  check. The adapter's `--allowed-targets` allow-list and the admission
  policy still reject a missing/unregistered target.

Card serving never uses a filesystem mount or a load-balancer `static_files`
cluster: the pinned image rejects that field and crash-loops.

### Readiness

`GET /readiness` (public, like the discovery paths) is answered by a
`static_response` filter returning 200 -- with no upstream clusters
configured there is no backend health to check. The startup/liveness probes
cover process health via the loopback admin listener. When a routed backend
is (re-)introduced, consider routing this path to it again so the proxy is
not marked ready while the backend is down (see the note in
`files/praxis.yaml`).

### No router without routes

The rendered `praxis.yaml` has **no router filter at all** while no upstream is
configured: a router with `routes: []` is a fatal config-load error on the
pinned image, and falling through to Core's own 404 on every other path is
intentional. Re-introduce a router only together with real routes (e.g. when
`a2aUpstream` is enabled); the exact fatal error and reproduction are in
[`docs/LESSONS_LEARNED.md` §5](../../../docs/LESSONS_LEARNED.md#5-praxis-gateway-pinned-image-quirks).

## Policy behavior

`files/policy.yaml` is rendered into the chart ConfigMap and loaded by the
Praxis `policy` filter at process startup. The `identity/jwt` plugin validates
`Authorization` against the configured Keycloak issuer, JWKS endpoint, and
configured audience. `policy.allowedCallers` defaults to the explicit
allow-list `[acme-agent]`; an absent identity, a different client, or a
policy/JWKS failure never reaches the router.

For multi-issuer validation, set `identity.oidcIssuers` with each external
issuer URL, JWKS URL, and expected token audience. The chart also appends the
local Keycloak issuer using `identity.keycloak.audience`. This changes token
validation only: the `claim.azp` check against `policy.allowedCallers` still
applies to every issuer and must be updated separately if additional clients
should be authorized. Because `policy.yaml` is embedded as a string inside a
ConfigMap, validate the rendered inner YAML too; the chart regression test
`tests/chart/test_praxis_multi_oidc.py` parses that value and checks all trusted
issuers.

### Policy enforcement on IPv4-only clusters (external SSO JWKS)

With `policy.enabled=true` and an external issuer such as Red Hat SSO, Praxis
fetches that issuer's JWKS over the public internet. On an IPv4-only cluster:

- set `policy.caBundleSync.includeSystemRoots=true` so the CA bundle extends
  (not replaces) the system trust store -- the ingress-only bundle does not
  trust `sso.redhat.com`;
- `policy.allowPrivateIdp` is only for a static issuer that genuinely resolves
  to a private address. `UnknownIssuer` means TLS trust; `no public address` /
  private-address errors mean the egress guard. They are separate checks.

See [`agents/AUTHENTICATION.md`](../../../agents/AUTHENTICATION.md) for the live
investigation and [`docs/LESSONS_LEARNED.md` §6](../../../docs/LESSONS_LEARNED.md#6-tls-trust)
for the trust-store rule.

### Allow-list enforcement check (fail-open regression guard)

The pinned image once forwarded a validly-signed, non-allow-listed token
silently, and that behaviour could regress differently on a future
`ghcr.io/praxis-proxy/ai` bump (details in
[`docs/LESSONS_LEARNED.md` §5](../../../docs/LESSONS_LEARNED.md#5-praxis-gateway-pinned-image-quirks)).
The chart therefore ships an **allow-list enforcement check** as a `PostSync`
hook Job
(`templates/enforcement-check-job.yaml`, `files/enforcement_check.py`), enabled
by default via `policy.enforcementTest.enabled`. Its purpose: guard against a
silent fail-open regression by asserting the proxy **denies** a caller whose
`azp` is not in the allow-list. A detected fail-open (a forwarded/200 request)
exits non-zero and fails the hook, so the regression shows up on the sync
instead of going unnoticed.

It runs in two tiers, by what credentials are available:

- **Always-on (no credentials needed).** Sends an unauthenticated request and
  a bad-token request to a protected path
  (`policy.enforcementTest.protectedPath`, default `/`) and asserts each is
  denied (401/403) and **not** forwarded (any 2xx is treated as a fail-open).
  This proves the authentication step is wired. **Limitation:** it does *not*
  exercise the historically dangerous fail-open -- a *validly-signed* token
  whose `azp` is not allow-listed being forwarded -- because the jwt-client
  step rejects an unsigned/garbage token before the allow-list predicate ever
  runs.
- **Strong (opt-in).** Set `policy.enforcementTest.minter.clientId` and
  `policy.enforcementTest.minter.clientSecret.{name,key}` to a Keycloak client
  whose `azp` is **not** in `policy.allowedCallers`. The Job then mints a real,
  validly-signed token via `client_credentials` and asserts Praxis denies it on
  `azp` -- the check that actually catches the allow-list fail-open. It is
  opt-in because it needs a non-allow-listed confidential client's credentials,
  which this chart has no self-contained source for; when
  `policy.caBundleConfigMap.name` is set, that bundle is mounted so TLS to the
  Keycloak Route verifies.

Two unauthenticated HTTP paths are intentionally served without a token
(see "Agent card" and "Readiness" above):

- `GET /.well-known/agent-card.json` (and its `/a2a/`-prefixed twin) serves
  A2A discovery: the a2aUpstream backend's own card, routed through
  unchanged (404 without a backend). The card advertises the public Praxis
  Route, never an internal Service.
- `GET /readiness` lets the proxy readiness probe verify the gateway is
  serving; with no backend configured it is answered by the proxy itself.
  (Historical: when the chart fronted OLS, this path proxied the fork's
  `GET /readiness`; the inherited `/health/ready` 404'd against the real
  service -- see [`docs/LESSONS_LEARNED.md` §5](../../../docs/LESSONS_LEARNED.md#5-praxis-gateway-pinned-image-quirks).)

All other paths, including the A2A RPC endpoint, hit the authenticated
catch-all route; with no upstream clusters configured and no router filter
(see "No router without routes" above), a request that survives the
policy filter falls off the end of the filter chain to Core's own 404. The
`policy` filter runs first in the filter chain, so a denied request is
never forwarded to any upstream.

## Configuration

Set `identity.keycloak.issuerUrl` to the exact token issuer and `route.host` to
the public A2A hostname (both derive from `global.clusterBaseDomain` by
default). In single-issuer mode (`identity.oidcIssuers` empty),
`identity.keycloak.audience` must name the audience this gateway's callers'
Keycloak tokens carry -- the chart `fail`s loudly when it is empty. With
`a2aUpstream` fronting the lightspeed-hub adapter, that is the audience of
the hop this gateway protects: the standalone variant sets
`lightspeed-hub-adapter` (Token A carries it via keycloak-oidc's
`hubExchange.subjectTokenAudienceMapper`). It used to be `mas-a2a`, left over
from when ACME called Praxis directly; MAS validates `mas-a2a` itself and no
Praxis fronts MAS (see `docs/ARCHITECTURE.md` "Current (Task B) request
path"). The Red Hat SSO issuers (`api.ask_red_hat`) stay so SSO callers that
MAS forwards are admitted. The backend's Service coordinates and TLS trust
come from operator-controlled values (`a2aUpstream.*`), never caller input.

The pinned Praxis AI 0.4.1 build only provides the filter-wide
`allow_private_idp` option, exposed as `policy.allowPrivateIdp` here. It is
needed when the configured Keycloak JWKS host resolves to a private ingress
address. It permits non-public destinations—including loopback and
link-local—for all policy-plugin callouts, so keep those endpoints
operator-controlled and static; this chart's policy only calls its configured
Keycloak JWKS URL. This setting is distinct from
`insecure_options.allow_private_endpoints` / `allow_private_upstreams`, which
apply to the proxy's load-balancer upstream. Newer Praxis builds expose a
narrower per-host `trusted_private_endpoints` option; use that instead when the
image is upgraded to a release that supports it.

When the configured Keycloak route uses the cluster's managed ingress CA,
enable `policy.caBundleSync`. A bootstrap Job copies the source bundle into
this namespace before Praxis starts; a CronJob refreshes it and rolls the
Deployment when the CA changes. The chart mounts that bundle and sets
`SSL_CERT_FILE` for the policy transport's OpenSSL client. The bundle replaces
the image's default trust file, so this is appropriate while the policy's only
HTTPS callout is the configured Keycloak JWKS URL. If other HTTPS callouts are
added, provide a combined bundle containing the image's public roots and every
required private issuer. For a publicly trusted Keycloak certificate, leave CA
sync disabled and use the image's normal trust store.

Praxis 0.4.1 currently requires both `insecure_options.allow_private_endpoints`
(config-load-time validation of the load_balancer cluster endpoints) and
`insecure_options.allow_private_upstreams` (the runtime connection check) for
static in-cluster upstreams. Those are broad private-address opt-ins; no
upstream clusters are currently configured, and when one is re-introduced
its destination must stay fixed and operator-controlled (never a
caller-supplied URL). Do not use these flags to proxy user-supplied
destinations.

## Checks and diagnostics

```bash
helm lint charts/all/praxis-proxy -f values-global.yaml -f variants/standalone/values-standalone.yaml
helm template praxis-proxy charts/all/praxis-proxy \
  -f values-global.yaml -f variants/standalone/values-standalone.yaml
```

In single-issuer mode (`identity.oidcIssuers` empty) with
`policy.enabled=true`, `identity.keycloak.audience` must be set (the chart
`fail`s loudly otherwise); a bare `helm lint charts/all/praxis-proxy` with
no values file is expected to fail for this reason.

At runtime, inspect the rendered policy and proxy logs with:

```bash
oc get configmap praxis-proxy -n praxis-proxy -o yaml
oc logs -n praxis-proxy -l app.kubernetes.io/name=praxis-proxy
```

**Routing/request counts, without changing any log level at
all**: confirmed live that the admin port (`127.0.0.1:9901`, loopback-only,
not exposed outside the pod) serves a Prometheus-format `/metrics` endpoint
with per-cluster counters -- the quickest way to confirm a request actually
reached the expected upstream cluster/endpoint, rather than inferring it
from ordinary logs (which don't mention routing at all by default):

```bash
oc exec -n praxis-proxy <praxis-pod> -- wget -q -O - http://127.0.0.1:9901/metrics
# praxis_upstream_requests_total{cluster="ols-<id>",endpoint="<host>:<port>",status_class="2xx"} <n>
# praxis_http_requests_total{method="POST",status_class="2xx",route="/*",cluster="ols-<id>"} <n>
```

**Log verbosity (`logLevel` in `values.yaml`, wired to `RUST_LOG`)**: unset
by default (the binary's own sparse, roughly info-level default applies --
occasional events like a JWKS refresh, not per-request detail). The image is
built on Rust's `tracing` ecosystem; crate names are literally the
module-path prefix already shown in each log line (e.g.
`praxis_policy_plugin_identity_jwt::resolver: ...`), so `logLevel` can scope
verbosity to just the component being investigated instead of flooding logs
with everything, e.g. `info,praxis_filter=debug,praxis_policy_plugin_identity_jwt=debug`
(`praxis_filter` covers routing/load-balancing). A bare level (`debug`)
applies to the whole binary. Not meant to stay at `debug` indefinitely on a
real environment -- see its comment in `values.yaml`.

### Validation evidence

The routing-era evidence table (header routing, 401/403 behaviour, the
duplicate-header gap) is in
[`docs/LESSONS_LEARNED.md` §5](../../../docs/LESSONS_LEARNED.md#5-praxis-gateway-pinned-image-quirks).

## Spoke gateways (B1)

`spokeGateways` renders one internal, TLS-serving Praxis gateway per
registered spoke cluster (Phase 2 B1 of
`PLAN-lightspeed-hub-migration.md`). The map is **empty by default**
(nothing renders). The Praxis-side wiring below was **verified at runtime
against the pinned image** (`ghcr.io/praxis-proxy/ai:0.4.1`, praxis-policy
0.3.1) — see "Runtime evidence" below for exactly what was and was not
validated — but it stays disabled by default until the remaining
real-Keycloak and live-cluster items close.

What each entry creates (all named `praxis-spoke-<cluster>`, same pinned
image as the main gateway, in this chart's namespace):

- a **Deployment** running the pinned Praxis image with a per-spoke config:
  listener serving TLS on 8443, a route matching all paths forwarding to the
  FIXED `remoteAPIServer` over new HTTPS (remote CA + SNI verified), and an
  oauth delegator exchanging the inbound Token A for a `exchangeAudience`
  Token U using PPE's own confidential client identity before forwarding
  (internal credential/routing headers stripped on forward);
- an **internal-only Service** on 8443 with an OpenShift service-serving
  certificate (`service.beta.openshift.io/serving-cert-secret-name`). The URL
  matches repo C's generated sandbox-kubeconfig convention:
  `https://praxis-spoke-<cluster>.praxis-proxy.svc.cluster.local:8443`.
  **No Route is ever rendered** for a spoke gateway (the plan forbids public
  gateway Routes; the chart's public Route targets only the main Service);
- a `praxis-spoke-service-ca` ConfigMap (`service.beta.openshift.io/
  inject-cabundle: "true"`) carrying the service-serving CA bundle the
  sandbox must trust for the gateway's serving cert, **plus a
  ConfigMap→Secret sync bridge** (one-shot bootstrap Job + CronJob + RBAC +
  stdlib-only Python script, `spoke-ca-sync-*.yaml`) copying that bundle
  into the Secret repo C's `gatewayCASecret` names (delivery is
  contract-validated only, not yet live);
- a per-spoke **NetworkPolicy** (best-effort; see below).

The remote spoke API CA is mounted from the referenced
`remoteCACert.secretName`/`configMapName` + `key`; a dangling reference
leaves the pod unschedulable (fail closed). PPE's exchanger client identity
(`spokeGatewayConfig.exchangeClient`) and its Keycloak prerequisites live in
`charts/all/keycloak-oidc` (`keycloak.spokeGatewayExchanger`) — the
Vault-backed secret is materialized into this namespace by that chart's
`spoke-gateway-exchanger-secret.yaml`.

The `spokeGateways` map is **purely per-spoke** (literally `{}` by default):
shared cross-spoke settings (`exchangeClient`, `networkPolicy`, `servingCA`)
live in the separate top-level `spokeGatewayConfig` block, so a spoke
literally named `exchangeClient` or `networkPolicy` renders — or fails
validation, e.g. the DNS-label check rejects the uppercase `P` — as a spoke;
nothing is silently dropped as a "reserved" sibling key.

Per the plan, B1 changes only the sandbox execution path; it never rewrites
`SpokeCluster.spec.apiServer` or any standing management kubeconfig.

### Runtime evidence for the spoke gateways

The per-spoke wiring was exercised against the pinned binary with a mock IdP
and local echo/websocket upstreams (no live cluster / real Keycloak), plus
`--validate` against the chart's rendered config. Still open: real-Keycloak
exchange behaviour, `kubectl exec` (SPDY vs websocket), service-CA Secret
delivery into repo C, and a live end-to-end run. What was verified, what was
wrong in the first attempt, and the target-header gap are in
[`docs/LESSONS_LEARNED.md` §5](../../../docs/LESSONS_LEARNED.md#5-praxis-gateway-pinned-image-quirks).

### NetworkPolicy limits

Each spoke gateway's NetworkPolicy restricts ingress to
`spokeGatewayConfig.networkPolicy.ingressFromNamespaces` on 8443 (the `mao`
default is a placeholder tied to design open question Q4 -- where repo C's
sandboxes run is undecided, so override it), and egress to cluster DNS, TCP
443 (Keycloak/JWKS) and the spoke's own remote API port, parsed at render time
from `remoteAPIServer` (typically 6443).

Plain NetworkPolicies cannot match FQDNs, so egress is port-scoped only;
FQDN-aware enforcement of "only its registered remote API, IdP/JWKS and DNS"
needs CNI egress features (EgressFirewall/Policy, an egress router). The
remote is additionally pinned by the gateway's fixed endpoint and verified
CA/SNI.

## A2A upstream (Task B request leg)

Where the hub and spoke gateways sit in the token chain:
`docs/agentic-architecture-overview.html`, `docs/agentic-e2e-token-flow.html`
(steps 5, 10 and 11) and `agents/AUTHENTICATION.md`.

`a2aUpstream` (disabled by default; **empty by default renders nothing and
the default render is byte-identical to the pre-block one** — pinned by
`tests/chart/test_praxis_a2a_upstream.py`) is the hub-gateway hop of the
lightspeed-hub request leg (leg 2; `docs/agentic-e2e-token-flow.html`
step 5): the main gateway forwards A2A traffic to
the hub A2A adapter (charts/all/lightspeed-hub) instead of answering 404.

**Design (current): this main gateway is the praxis HUB gateway and
exchanges A→H** (`a2aUpstream.exchangeAtPraxis`). It validates the caller's
Token A (`aud: mas-a2a`, plus the `claim.azp` allow-list), then
`delegate(hub-delegator, target: hub-adapter, audience: lightspeed-hub-a2a)`
exchanges it as client `mas-a2a` (Token A's audience, which is Keycloak's
condition for exchanging it) and forwards Token H. The adapter creates the
AgenticRun with H and derives the run token R from H, so it never sees Token
A. The delegator block, client secret env var
(`PRAXIS_HUB_EXCHANGE_CLIENT_SECRET`) and token endpoint are the same
parameterized `files/policy.yaml` block the spoke gateways use (`__delegation`).
The pinned `delegate()` takes ONE audience, which is why every token's
audience names the client that exchanges it next.

History: this was previously fail-guarded. The adapter did A→H itself and
stored Token A for the run leg, and front-door delegation would have hidden A
from it (no protected-A handoff on ai:0.4.1). Deriving the run credential
from H removed that blocker. Two caveats remain:
- **Alphanumeric client secret only.** The delegator sends Basic auth without
  URL-encoding (`values-secret.yaml.template` `alphaNumericPolicy`).
- **No SSO callers yet.** The delegator sends no `subject_issuer`, so an
  SSO-issued Token A cannot be exchanged here.

The spoke gateways validate their own exchanger audience
(`spokeGatewayConfig.inboundAudience`, default the exchanger client ID
`praxis-spoke-gateway`), which is what the run token R carries.

They also enforce their own azp allow-list
(`spokeGatewayConfig.allowedCallers`; empty inherits `policy.allowedCallers`).
Keycloak stamps an exchanged token's `azp` with the client that performed the
exchange, so R's `azp` is the adapter's exchanger (`lightspeed-hub-a2a`), not
`acme-agent` -- leaving this empty (inherit the hub list) denies every sandbox
kube call with 403 "not an allow-listed caller" (hit live 2026-11-20).

Known limit: a large `kubectl logs` response fails at the spoke gateway with
500 "streamed response body exceeds global body limit" (Praxis default 10 MiB;
the demo payments-api log was 12.4 MB). Raise it with
`spokeGatewayConfig.maxResponseBytes` (renders `body_limits.max_response_bytes`).

When enabled, `files/praxis.yaml` gains (inside one `{{- if }}` — the
disabled render keeps the historical no-router bytes):

- **Router caller×target rules, not APL** — APL `http:` route selectors
  accept path/path_prefix/method only (documented in `files/policy.yaml`),
  so the target header is validated by the router: one route per
  **registered spoke** (`spokeGateways` map keys — the allow-list IS the
  spoke map; a caller may only target spokes that have gateways). A
  missing or unregistered target header matches no route → Core answers
  404 (the behavior verified live during the pre-Task-A routing era; see
  "Header routing" history above). Enabling `a2aUpstream` with an empty
  `spokeGateways` map **fails the render loudly** — a front door with an
  empty allow-list would route nothing while looking configured.
  `a2aUpstream.enabled` with `policy.enabled=false` also fails the
  render: the leg's design is "Praxis stays the front door".
- **One fixed upstream cluster** `hub-adapter` — the adapter's
  internal-only Service (`a2aUpstream.service/namespace/port`, default
  the lightspeed-hub chart's Service), reached over HTTPS with
  service-CA verification (the chart renders an inject-cabundle
  ConfigMap, `praxis-hub-adapter-ca`, and mounts it — same mechanism as
  the spoke gateways). The target header is deliberately NOT stripped:
  the adapter resolves `spec.targetCluster` from it after the router has
  validated it.

### Multi-valued target headers cannot be rejected by the router

The router OR-matches repeated header values, and the pinned image has no
filter that counts or rejects them, so a request carrying one registered and
one unregistered target header still matches. Two compensating controls are
required before enabling (source-confirmed; see
[`docs/LESSONS_LEARNED.md` §5](../../../docs/LESSONS_LEARNED.md#5-praxis-gateway-pinned-image-quirks)):

1. the **adapter** rejects duplicate/multi-valued target headers (it sees the
   raw header lines and can count them);
2. the server-side admission policy plus run-creators RBAC
   (`charts/all/lightspeed-hub`) bound what a smuggled request can achieve.

Re-check when the image is next bumped.

### Open items before enabling

1. **Adapter TLS serving (UNCONFIRMED GUESS):** the upstream cluster
   verifies TLS with the service CA on port 8443, but the adapter
   currently binds plain HTTP (`--listen`, design §1) and no TLS-serving
   flags are confirmed in the hub-adapter build. Wire/confirm the adapter
   side first (see the lightspeed-hub README's cross-chart contract).
2. The multi-valued-header compensating controls above (adapter-side
   rejection + admission policy configured).
3. The lightspeed-hub chart itself is still a disabled M4 skeleton
   (placeholder images) — enabling this leg front-doors a backend that
   does not run yet.

## Upstream maturity and pinning

This Pattern currently pins the `0.4.1` upstream release by its image digest
in `values.yaml`; its upstream release is prerelease software. Praxis AI labels
itself alpha, and PPE's pre-1.0 public API may change between minor releases.
This integration is not currently a Red Hat-supported Praxis distribution.
Review upstream release notes and revalidate the chart before changing
`image.tag`; for production rollouts, pin the verified image digest as well as
its version.

**Praxis Core v0.7.2 is not available in a published `praxis-proxy/ai`
image (checked 2026-10-01; re-check before the next version decision).**
`praxis-proxy/praxis` (Core) and `praxis-proxy/ai` (the image this chart
actually runs) are separate release lines -- see
`LIGHTSPEED_DESIGN.md` SS7. Findings from this pass, via the GitHub
API against the real `praxis-proxy/ai` and `praxis-proxy/praxis` repos and
`skopeo list-tags` against the real `ghcr.io/praxis-proxy/ai` registry (no
local vendor/fork path was used for any of this):

- `praxis-proxy/ai` releases stop at `v0.4.1` (2026-09-26, pre-release). The
  registry has no numeric tag beyond `0.4.1` (only `main`/`nightly` floating
  tags, which are not safe to pin).
- The `v0.4.1` tag's own `Cargo.lock` resolves `praxis-proxy-core` to
  **0.7.0** (and `praxis-policy` to **0.3.1** -- the PPE version actually
  exercised by every finding in this README).
  `praxis-proxy/ai`'s `main` branch (commit `4110021`, 2026-10-01) has since
  moved its `Cargo.lock` to resolve `praxis-proxy-core` to **0.7.2** -- so
  upstream is actively integrating it -- but `main` is an unpinned floating
  branch with no release, tag, or digest, and is not something to deploy
  from.
- Building and publishing our own `ai` image from that commit is explicitly
  out of scope for this pass (no Rust toolchain or registry-push credentials
  available here); re-check `https://github.com/praxis-proxy/ai/releases`
  before the next attempt at this bump.
- The header-routing (`routes[].headers`) and per-upstream TLS
  (`clusters[].tls.{sni,verify,ca.ca_path}`) syntax this chart now targets
  was confirmed byte-for-byte identical between the
  [Core v0.7.0 docs](https://github.com/praxis-proxy/praxis/blob/v0.7.0/docs/filters/http/traffic_management/router.md)
  (what `ai:0.4.1` actually embeds) and the
  [Core v0.7.2 docs](https://github.com/praxis-proxy/praxis/blob/v0.7.2/docs/filters/http/traffic_management/router.md)
  cited in the migration plan, and was then validated structurally and live
  against the real pinned binary (see "Validation evidence" above) -- so
  this chart's routing/TLS config needs no changes if/when the image is
  later bumped to an `ai` release built against Core v0.7.2.

## External-IdP egress gateways

`egressGateways.<key>` renders one internal Praxis per external destination.
It accepts the caller's local-Keycloak Token A, mints a token at an external
IdP with its own client (`delegate(..., subject: this_workload)`, which is
`client_credentials`), and forwards to one fixed upstream. The standalone
variant uses it for Ask Red Hat (stage), so MAS forwards the local Token A
instead of a Red Hat SSO token. Design, rollout and the live evidence are in
[`docs/EXTERNAL_IDP_EGRESS.md`](../../../docs/EXTERNAL_IDP_EGRESS.md); adding
another agent: [`docs/ADDING_AN_EXTERNAL_AGENT.md`](../../../docs/ADDING_AN_EXTERNAL_AGENT.md).
The external API sees one shared account (the gateway's client), not
individual users.

| Rendered per entry | Purpose |
|---|---|
| `ConfigMap praxis-egress-<key>` | TLS listener (8443), `policy` (local Keycloak only + `azp` allow-list + delegator), `headers` (`Host` = upstream host), the agent card from `agentCard`, router for the upstream's own path prefix, upstream cluster verified with `policy.caBundleConfigMap` |
| `Deployment praxis-egress-<key>` | same pinned image, DNS sidecar and trust store as the other gateways; app label `praxis-egress-gateway`, so the main Service never selects it |
| `Service praxis-egress-<key>` | ClusterIP 8443 with an OpenShift service-serving cert; no Route |
| `NetworkPolicy praxis-egress-<key>` | ingress only from `egressGatewayConfig.networkPolicy` namespaces and pods (default: `mas-worker`, `mas-api` in `mao`); egress DNS + TCP 443 |
| `ExternalSecret praxis-egress-<key>-client` | only with `clientSecret.vaultKey`; reads the same Vault record the caller-side client uses |

Rules:

- **Requires** `policy.enabled` and `policy.caBundleConfigMap.name` with
  public roots (`policy.caBundleSync.includeSystemRoots`); the chart fails
  the render otherwise.
- **Credentials come from one Secret (Vault record).** With `clientId` empty
  (recommended), both `client-id` and `client-secret` come from the client's
  Secret. The secret is passed as an env var. The client ID is written into
  `policy.yaml` by a `render-config` init container (same pinned image, which
  is Alpine with `sh`/`sed`) into an in-memory `emptyDir`, because the
  delegator's `client_id` is a plain string and Praxis does no env
  interpolation. Both are read at pod start, so restart the gateway after a
  Vault change. The secret must be alphanumeric (HTTP Basic is sent without
  URL-encoding), and the client ID must match `[A-Za-z0-9._-]`.
- **`scopes` are enforced.** The pinned delegator denies with
  `delegation.scope_too_broad` unless every requested scope is in the granted
  `scope`.
- **Path and `Host`.** The upstream's own path is served unchanged (the
  pinned image has no path rewrite), and `Host` is always rewritten to the
  upstream host: Red Hat's edge rejects the Service name with `400 Invalid
  URL` (`docs/LESSONS_LEARNED.md` section 5).
- **A2A upstreams need `agentCard`.** The a2a-sdk sends to the card's URL,
  and the upstream's card advertises the upstream's own host. The gateway
  therefore serves `agentCard` itself at `GET <path>/.well-known/agent-card.json`
  (the one unauthenticated route; it mints nothing), with `url` and any
  `supportedInterfaces[].url` forced to the gateway and `additionalInterfaces`
  dropped. Without it, the card GET is passed through and A2A callers would
  bypass the gateway. This is the deliberate exception to "never serve a
  hand-maintained card": pass-through with a rewritten URL is impossible on
  the pinned image. Keep the copy current
  (`docs/EXTERNAL_IDP_EGRESS.md`, "Keeping the card current").
- **Callers must trust the OpenShift service CA.** Set
  `identity.serviceCaTrust.enabled` on mas-worker and mas-api.
- **Stage to production is a values change only** (`upstreamURL`,
  `tokenUrl`, the Vault record, plus `agentCard` if the cards differ). Callers keep `base_url` = the gateway. The standalone variant puts
  each environment in its own `extraValueFiles` file.
- **Monitoring:** `bash scripts/monitor.sh --egress`. The gateway logs no mint
  line and no deny reason; the monitor explains what each card proves.
- **The PostSync `enforcement-check` Job does not cover egress gateways.** It
  tests only the main gateway. The egress allow-list is rendered by the same
  template, but the `azp` deny on an egress gateway has not been exercised
  live.
