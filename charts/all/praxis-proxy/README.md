# Praxis authorization gateway

This chart deploys the [Praxis AI proxy](https://github.com/praxis-proxy/ai)
with its embedded [Praxis Policy Engine (PPE)](https://github.com/praxis-proxy/policy)
as a public, policy-enforcing A2A gateway. The public OpenShift Route targets
this proxy; the proxy validates the inbound Keycloak/external-SSO access
token, authorizes the caller with native APL, serves its own A2A discovery
card, and answers readiness itself.

> Historical note: this chart previously routed per-request to OpenShift
> Lightspeed (OLS) app-servers selected by an `X-OLS-Cluster` header, with
> one router route and one `load_balancer` cluster per entry in a shared
> `global.olsClusters` registry. The OLS backend was removed
> (`PLAN-lightspeed-hub-migration.md` Task A), so the chart currently
> configures **no upstream clusters**: everything else (policy filter,
> allow-list enforcement check, readiness) is unchanged, and
> a future backend (Task B, lightspeed-hub) will re-introduce routed
> upstreams. The "Validation evidence" section below records the live
> findings from the routing era and is kept as historical reference.

## Agent card (pass-through, no static card)

Praxis has **no agent card of its own**. With `a2aUpstream.enabled`, the
router forwards `GET /.well-known/agent-card.json` and
`GET /a2a/.well-known/agent-card.json` (MAS resolves the card at
`{base_url}/.well-known/agent-card.json`, and its base URL ends in `/a2a`)
to the `hub-adapter` cluster **unchanged**, so callers see the
lightspeed-hub adapter's own card. With `a2aUpstream` disabled there is no
agent behind the gateway, and discovery answers 404. The policy filter
exempts only `GET` on those two exact paths from authentication.

Why: callers pick their JSON-RPC transport *and method names* from this
card. **VERIFIED-BROKEN LIVE (2026-11):** the static card this chart used
to serve advertised A2A `protocolVersion: "1.0"`, so MAS (a2a-sdk 1.1.5)
used its v1.0 transport and called `SendStreamingMessage`. The adapter
(a2a-go v0.3.15) only knows the 0.3 names `message/stream` /
`message/send`, so every `hub_investigate` delegation failed with JSON-RPC
`method not found`. A first attempted fix, `streaming: false` on the static
card, just switched MAS to `SendMessage`, which fails the same way (probed
directly against the adapter). Nothing caught it sooner because the chart
tests only checked the card's *shape*, the adapter's own tests never saw
the Praxis card, and MAS logged the failure only as a work-item note, not
an error. The fix is to stop duplicating the backend's card at all.

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
cluster: the pinned `ghcr.io/praxis-proxy/ai:0.4.1` image (Core 0.7.0)
rejects `static_files` as an unknown cluster field and crash-loops.

### Readiness

`GET /readiness` (public, like the discovery paths) is answered by a
`static_response` filter returning 200 -- with no upstream clusters
configured there is no backend health to check. The startup/liveness probes
cover process health via the loopback admin listener. When a routed backend
is (re-)introduced, consider routing this path to it again so the proxy is
not marked ready while the backend is down (see the note in
`files/praxis.yaml`).

### No-backends router caveat (VERIFIED-BROKEN, fixed)

This chart previously kept a `router` filter with an empty `routes` list so
allow-listed callers hitting any non-public path would fall through to a
"clean 404". That was an UNCONFIRMED GUESS, and it is now **verified
broken**: on the pinned image (`ghcr.io/praxis-proxy/ai:0.4.1`,
praxis-policy 0.3.1) a `router` filter with `routes: []` is a **fatal
config-load error** — the binary refuses to start, printing:

```
fatal: router: 'routes' is empty; every request would fail with 404
```

(Reproduced 2026-11 with a local `podman run` of the pinned image against
a minimal no-upstream config; the same binary accepts the config with the
router filter removed.) The fix, as the old guess's fallback prescribed:
the rendered `praxis.yaml` (with `a2aUpstream` disabled) has **no router
filter at all** — the readiness `static_response` filter is the only
route answered locally, and anything else that survives the policy filter falls off the
end of the filter chain to Core's own 404. Re-introduce a router only
together with real (non-empty) routes, e.g. when the Task B
lightspeed-hub backend lands.

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

Running `policy.enabled=true` with an external issuer such as Red Hat SSO means
Praxis itself must fetch that issuer's JWKS over the public internet. On an
IPv4-only cluster two things break that fetch, and both are handled by this
chart (see `agents/AUTHENTICATION.md` for the live investigation):

1. **TLS trust.** The policy plugin points OpenSSL's `SSL_CERT_FILE` at the
   synced `policy.caBundleConfigMap` bundle, which *replaces* the default trust
   store. The ingress-CA-only bundle (for Keycloak's Route) does not trust the
   public chain of `sso.redhat.com`, so the JWKS fetch fails certificate
   verification. Set `policy.caBundleSync.includeSystemRoots=true` so the sync
   appends the sync container's public CA roots to the ingress CA — one
   self-contained bundle that trusts both Keycloak and the external issuer.

2. **IPv6 with no fallback.** The pinned Praxis image's Rust HTTP client
   resolves external hosts to IPv6 and does **not** fall back to IPv4, so on an
   IPv4-only pod the JWKS fetch fails with `ConnectNoRoute`. The image is
   Alpine/musl, so the glibc `options no-aaaa` resolver switch does not apply.
   Set `dnsIPv4Only.enabled=true` (with `dnsIPv4Only.image` and
   `dnsIPv4Only.clusterDnsIP`) to run a CoreDNS sidecar that strips AAAA records
   and forwards everything else to the cluster DNS; the pod resolves through it
   (`dnsPolicy: None`), so the client only ever receives A records. CoreDNS
   binds loopback:53 as non-root via the "safe" sysctl
   `net.ipv4.ip_unprivileged_port_start=53` (accepted by OpenShift's
   restricted-v2 SCC). In-cluster names still resolve (non-AAAA queries are
   forwarded unchanged). Validated live from the Praxis pod: the default
   resolver returns A+AAAA, the sidecar returns A only, and in-cluster
   Service names still resolve.

Also add every non-`acme-agent` caller's `azp` to `policy.allowedCallers` — e.g.
the UI's Red Hat SSO client — or the `azp` allow-list will `deny` it even though
its token validates.

**Header routing and the `role: user` workaround (read this before touching
the allow-list predicate).** This chart's identity/jwt plugin is configured
`role: user` and compares `claim.azp`, even though it is authenticating an
OAuth *client* (acme-agent's service account), and the PPE docs' own worked
examples use `role: client` + `client.client_id` for exactly this case. This
was changed deliberately after empirically finding that the semantically
"correct" form does not work on the exact pinned image:

- Pinned image: `ghcr.io/praxis-proxy/ai:0.4.1`, whose `Cargo.lock` resolves
  `praxis-policy` (PPE) to **0.3.1**.
- With `role: client` (the originally-shipped config), `RUST_LOG=debug`
  confirmed the `keycloak` claim_mapper's "client" `claim_map` section runs
  for every request, but the resulting `client.*` bag namespace --
  `client.client_id`, `client.claim.azp`, per
  [`cmf-extensions.md` at `v0.3.1`](https://github.com/praxis-proxy/policy/blob/v0.3.1/docs/content/cmf-extensions.md)
  -- never becomes visible to this route's `pre_invocation` predicates on
  this pinned build: `exists(client.client_id)` and
  `exists(client.claim.azp)` both evaluated **false** for a token that
  demonstrably carried `azp`, with every plausible `read_*` capability
  granted to the plugin.
- With `role: user` instead, `exists(subject.id)` and `exists(claim.azp)`
  both evaluated **true** for the identical token. `role: user` is therefore
  the form verified to actually work on this pinned image; revisit
  `role: client` when the image is next upgraded, since that is the
  semantically correct role for a service-account/OAuth-client caller.
- Separately, **the chart's original allow-list predicate was a silent
  no-op on this pinned image regardless of the role/attribute-path issue
  above**: it compared `claim.client_id`, bare (no `require(...)`, no
  explicit `: deny(...)` action), a form this engine's effects model
  resolves to "allow/continue" in both the true and false case on this
  build. Live-tested with two tokens differing only in `azp`: both an
  allow-listed and a non-allow-listed caller reached the backend. The fix
  verified to work is a `claim.azp != '<id>': deny('not an allow-listed
  caller')` form with an **explicit** `deny(...)` action (ANDing one such
  conjunct per `policy.allowedCallers` entry, so the rule denies unless
  `azp` matches at least one of them). `require(...)` is *also* confirmed
  broken on this pinned build -- it denied unconditionally regardless of the
  claim compared -- so it is not a usable alternative either. **Do not
  revert to a bare comparison or to `require(...)` without re-verifying
  against the pinned image first** (see "Validation evidence" for the exact
  reproduction).

### Allow-list enforcement check (fail-open regression guard)

Every fail-open described above was silent: a validly-signed token reached the
backend when it should not have, with nothing in the sync or the logs flagging
it. Because those behaviors are properties of the *pinned image* (and
`require(...)`/bare-predicate/`client.*`-visibility quirks could regress
differently on a future `ghcr.io/praxis-proxy/ai` bump), the chart ships an
**allow-list enforcement check** as a `PostSync` hook Job
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
  (Historical note: when this chart fronted OLS, this path proxied the
  backend's `readiness_probe_get_method` route from the external fork
  `luis5tb/lightspeed-service@a2a-split` -- no `/v1` prefix, confirmed
  against that source. The path was originally `/health/ready`, copied
  from the prior RCA-era chart without verification against the actual
  service -- confirmed broken (404) against a live backend and fixed then.)

All other paths, including the A2A RPC endpoint, hit the authenticated
catch-all route; with no upstream clusters configured and no router filter
(see "No-backends router caveat" above), a request that survives the
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

### Validation evidence (T6.1-T6.3, 2026-10-01 -- historical, from the routing era)

Rendered this chart's `praxis.yaml`/`policy.yaml` for one and two
`global.olsClusters` entries, validated the result against the real pinned
`ghcr.io/praxis-proxy/ai@sha256:83c86...` image with
`praxis-ai --config ... --validate` (passes cleanly), then ran it live
(`podman run ... --network host`) against two local HTTPS backends
(self-signed certs, distinct SNI per backend) and a local JWKS server backing
hand-signed RS256 test tokens. Observed, against the pinned binary:

| Case | Result |
|---|---|
| Public `/.well-known/agent-card.json`, `/readiness`, no auth | Forwarded to the registry's primary cluster, 200 from backend |
| Valid token + `X-OLS-Cluster: <registered-id>` | Routed to that id's own backend (verified both of two registered clusters reach their *own* distinct backend) |
| Valid token, no `X-OLS-Cluster` | Router rejects, HTTP 404, no forwarding |
| Valid token, unregistered `X-OLS-Cluster` value | Router rejects, HTTP 404, no forwarding |
| No `Authorization` header at all | `policy` filter rejects, HTTP 401, before router ever runs |
| Non-allow-listed caller (valid signature, different `azp`), valid header | `policy` filter rejects, HTTP 403 ("not an allow-listed caller") |
| Two `X-OLS-Cluster` values, one bogus + one valid | **Routed successfully** -- confirmed known upstream gap, see "Known gap -- duplicate headers" above |

This also caught and fixed a real defect in the inherited policy document
(before this pass, a non-allow-listed caller with a validly-signed token
reached the backend exactly like an allow-listed one): see "Header routing
and the `role: user` workaround" above for the two separate root causes and
their fixes.

Rendering `identity.oidcIssuers` with two external entries exposed a Helm
whitespace-trimming bug that joined the final external JWKS URL and the local
Keycloak list item onto one line, making the embedded policy invalid YAML.
No prior test parsed the rendered `ConfigMap.data.policy.yaml`; the new chart
test does so to keep this from recurring.

## Spoke gateways (B1)

`spokeGateways` renders one internal, TLS-serving Praxis gateway per
registered spoke cluster (Phase 2 B1 of
`PLAN-lightspeed-hub-migration.md`). The map is **empty by default**
(nothing renders). The Praxis-side wiring below was **verified at runtime
against the pinned image** (`ghcr.io/praxis-proxy/ai:0.4.1`, praxis-policy
0.3.1) — see the register below for exactly what was and was not
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
  into the Secret repo C's `gatewayCASecret` names — see UNCONFIRMED
  GUESS #6 below;
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

### Verified/open register

The pinned image is `ghcr.io/praxis-proxy/ai:0.4.1`, whose `Cargo.lock`
resolves `praxis-policy` (PPE) to **0.3.1**. **Spike method (recorded
honestly):** everything marked VERIFIED below was exercised on the *real
pinned binary* run locally with podman, fronted by a hand-rolled **mock
IdP** (mock JWKS + RFC 8693 token endpoint) and local echo/websocket
upstreams — **no live cluster and no real Keycloak were involved**. That is
runtime evidence on the exact shipped binary, but not on a deployment.

1. **Oauth delegator — VERIFIED-WORKS on 0.3.1.** The pinned image ships
   `delegator/oauth` as a **policy.yaml `plugins:` entry** (not a Core
   filter, and none of the originally-proposed filter field names
   — `token_url`/`client_secret.env`/`audience`/
   `replace_authorization`/`strip_headers` — exist). Verified form, now
   rendered by the chart:
   `kind: delegator/oauth`, `hooks: [token.delegate]`, `capabilities:
   [read_inbound_credentials, write_delegated_tokens, perform_http]`,
   `config: {token_endpoint, client_id, client_secret_source: {kind:
   env_var, name: PRAXIS_SPOKE_EXCHANGE_CLIENT_SECRET}}` (an optional
   `cache` block exists; left commented off in the chart), driven from the
   route by `authorization.pre_invocation:
   ["delegate(<plugin>, target: spoke-api, audience: <exchangeAudience>)"]`
   after the `authentication: {steps: [jwt-client]}` resolver. Verified
   end-to-end against the mock IdP (A→U exchange, Authorization replaced
   with U upstream). **Still open: real-Keycloak behavior**, including the
   STE v2 impersonation-only caveat and the mapper questions in item 5.
   Empirical gotchas (all reproduced on the pinned image, recorded as
   comments in `files/policy.yaml`): `delegate()` without identity
   resolution raises a `delegation_without_identity_resolution` alarm and
   fails; a `DelegationPayload` with an empty bearer fails the delegation;
   **both** the jwt resolver plugin and the delegator need `perform_http`;
   client authentication at the token endpoint is HTTP Basic
   `client_id:client_secret`. The `token_endpoint` must be https in prod
   (`insecure_http: true` is dev-only and deliberately not set).
2. **Kube-API pass-through — VERIFIED-WORKS.** The pinned Praxis proxies
   the Kubernetes API shape: arbitrary methods/paths/query strings,
   incremental SSE watch streaming, websocket 101 upgrade with
   bidirectional frames, 5MB request bodies, and `Expect: 100-continue`
   all passed through to a test upstream. **Residual (needs a live
   cluster):** real `kubectl exec` uses the SPDY `v1.remotecommand`
   subprotocol (vs the websocket `v4.channel.k8s.io` subprotocol the
   spike exercised) — validate exec against a real kube API before
   relying on it.
3. **TLS-serving listener — VERIFIED, with corrected schema.** The
   originally-proposed `listeners[].tls.{cert_path,key_path}` is **wrong**;
   the pinned image requires certs under a `certificates` list:
   `tls: {certificates: [{cert_path: ..., key_path: ...}]}` (now rendered).
   (Bonus, unverified but present in the binary: `client_cert_mode` +
   `trusted_spiffe_ids` under the same `tls` block for future
   mTLS-SPIFFE client verification.)
4. **Header stripping — VERIFIED via the Core `headers` filter.** There is
   no `strip_headers` field; the verified form is a Core filter
   `filter: headers` with `request_remove: [X-OLS-Cluster,
   X-Forwarded-For, Forwarded, ...]` (now rendered, including the
   X-Forwarded-Host/Proto/Server variants). A `forwarded_headers` filter
   also exists upstream if explicit forwarding is ever wanted.
5. **Keycloak mapper set — OPEN.** Whether mappers on the *exchanging*
   client apply to RFC 8693-exchanged tokens (vs only client-credentials
   tokens), and whether the subject-token audience mapper on the
   acme-agent client is accepted by every audience-sensitive validator of
   Token A. See `charts/all/keycloak-oidc`'s
   `keycloak.spokeGatewayExchanger` comments for the plan-recorded
   caveats: `standard.token.exchange.enabled` on the exchanging client;
   the subject token must already carry the exchanger as audience (the
   `audience` request parameter only narrows, never adds); omit
   `client_id` for SVID client-assertion flows; after the exchange `azp`
   becomes the exchanger, so policy must key on `sub`/groups and record
   `azp` as provenance only.
6. **Service-CA bundle delivery — OPEN end-to-end** (bridge shipped;
   mechanism validated by contract reading, not live). The
   `inject-cabundle` ConfigMap (`praxis-spoke-service-ca`, key
   `service-ca.crt`) is the standard in-namespace source for the
   service-serving CA bundle — but repo C (lightspeed-agentic-operator,
   `a2a-support`) requires a **Secret with key `ca.crt`** for its
   `gatewayCASecret` (`controller/agenticrun/gateway.go` mounts it as the
   sandbox kubeconfig's certificate-authority; `pkg/configuration/config.go`
   fails closed without the name). The chart therefore ships a
   ConfigMap→Secret sync bridge (`spoke-ca-sync-*.yaml`, mirroring the
   repo's historical keycloak-ca-sync pattern and this chart's own
   `policy.caBundleSync`): a one-shot bootstrap Job copies the bundle
   into the Secret named by `spokeGatewayConfig.servingCA.secretName`
   (default `praxis-spoke-gateway-ca`, key `ca.crt`), and a CronJob
   (default hourly) keeps it current — the OpenShift service CA
   **rotates** (roughly every two years, with an overlap window) and the
   Secret copy would otherwise go stale, making sandbox kubeconfigs
   reject the gateway's serving cert after rotation. The Secret is
   created by the job, deliberately NOT rendered as a chart manifest
   (Argo would otherwise fight the job's writes). Repo C consumes the
   Secret from its **operator namespace**, so in the standalone variant
   `spokeGatewayConfig.servingCA.targetNamespace` MUST be set to that
   namespace (default is this chart's namespace; the sync job's RBAC is
   rendered there accordingly). Still unvalidated end-to-end: the exact
   `service-ca.crt` key contents and repo C's mount against a live
   sandbox kubeconfig. Fallback: the kubeconfig can instead point at the
   pod's projected serving-cert Secret CA if repo C prefers.
7. **Combined filter chain.** The per-spoke chain actually rendered
   (policy + headers + static_response + router + load_balancer, plus the
   delegator plugin and delegate() step in policy.yaml) has now been
   loaded and `--validate`d by the pinned binary against the chart's
   rendered output; the main gateway's own empty-routes router problem is
   closed separately (see "No-backends router caveat" above). What
   remains is running that exact chain against a live cluster and a real
   Keycloak.

### NetworkPolicy limits

The per-spoke NetworkPolicy restricts ingress to the configured sandbox
namespaces (`spokeGatewayConfig.networkPolicy.ingressFromNamespaces`; the
`mao` default is a PLACEHOLDER tied to design-doc open question Q4 — where
repo C's sandboxes run is undecided, so it is a mandatory-override value,
not a correct default) on 8443, and egress to cluster DNS plus TCP 443
(Keycloak/JWKS) plus the spoke's own **remote API port, parsed at render
time from `remoteAPIServer`** (typically 6443 — remote kube API servers do
not listen on 443, so an egress rule allowing only 443 silently
blackholes the remote API leg). **Plain Kubernetes NetworkPolicies cannot
match on FQDNs**, so that egress is port-scoped
only: FQDN-aware enforcement of "its registered remote API, IdP/JWKS and DNS
needs" needs CNI egress facilities (EgressFirewall/EgressFirewallPolicy, an
egress router) beyond ordinary NetworkPolicy hostname rules — this is
exactly what the plan records for B1. The remote is additionally pinned by
the gateway's fixed operator-configured endpoint and verified CA/SNI.

## A2A upstream (Task B request leg)

`a2aUpstream` (disabled by default; **empty by default renders nothing and
the default render is byte-identical to the pre-block one** — pinned by
`tests/chart/test_praxis_a2a_upstream.py`) is the front-door hop of the
lightspeed-hub request leg (leg 2; `docs/lightspeed-hub-plan.html` s3
"Praxis stays the front door"): the main gateway forwards A2A traffic to
the hub A2A adapter (charts/all/lightspeed-hub) instead of answering 404.

**Design decision (settled for this leg):** Praxis VALIDATES Token A
(the existing JWT + `claim.azp` allow-list policy, unchanged) and ROUTES;
the **ADAPTER performs the A→H exchange** with its M1 exchanger. The
Praxis-side exchange is documented as `a2aUpstream.exchangeAtPraxis` but
**fails the render loudly** when set to true: front-door delegation
replaces the outbound Authorization header with H, hiding Token A from
the adapter — which needs A for the run leg (the A→U spoke exchange), and
the pinned image (ai:0.4.1 / praxis-policy 0.3.1) has no protected-A
handoff mechanism (PPE's assertions can only project validated identity
slots, never the raw bearer, onto a header). See
`PLAN-lightspeed-hub-migration.md` "Protected A handoff is required, not
deferred". Fail-loudly-honest was chosen over rendering the spoke-gateway
delegator pattern half-wired: a delegator that silently destroys the run
leg's subject token is worse than no delegator.

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

### VERIFIED-UNFIXABLE on the pinned image: multi-valued target headers

The plan (adapter-scope item 5) requires rejecting
duplicate/multi-valued target headers because the router OR-matches
repeated values. **No built-in filter on the pinned image can do this**,
source-confirmed 2026-11 against Core v0.7.0 (the exact Core embedded in
ai:0.4.1):

- router `headers_match` (crates/filter/.../router/matching.rs):
  `get_all(key).any(|v| v == val)` — OR across a repeated header's
  values;
- guardrails `scan_header_values`:
  `get_all(...).find_map(...)` — same OR shape;
- the `headers` filter has add/set/remove only, no reject form.

So a request smuggling two target headers, one registered and one not,
still matches the registered route. Compensating controls (both REQUIRED
before enabling): the **adapter** must reject duplicate/multi-valued
target headers (it sees the raw header lines and can count them), and the
server-side admission policy + run-creators RBAC
(charts/all/lightspeed-hub) bound what a smuggled request can achieve.
Re-check this gap when the image is next bumped.

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
