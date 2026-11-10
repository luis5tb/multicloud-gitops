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
> allow-list enforcement check, discovery card, readiness) is unchanged, and
> a future backend (Task B, lightspeed-hub) will re-introduce routed
> upstreams. The "Validation evidence" section below records the live
> findings from the routing era and is kept as historical reference.

## Static agent card

When `agentCard.enabled` is `true` (the default), Praxis serves its own
static agent card through the `static_response` filter at both
`/.well-known/agent-card.json` and `/a2a/.well-known/agent-card.json` (MAS
A2A validation looks in both places depending on the configured base URL).
The card's critical field is `rpc_url`: it points to Praxis's own public
endpoint (auto-derived from `global.clusterBaseDomain` as
`https://praxis-proxy.apps.<base>/a2a` when not explicitly set), ensuring
A2A clients (like the MAS orchestrator) call Praxis rather than any
internal Service. The filter runs after the policy filter, whose explicit
public-path rule allows agent-card discovery without a bearer token.

**Why this matters:** The A2A SDK (`client.py`) uses the agent card's
`rpc_url` for all actual calls, not the `base_url` initially provided.

**Configuration:**
- `agentCard.enabled: true` activates the feature.
- `agentCard.name`, `description`, and `version` default to a generic
  gateway card (no skills -- there is no agent backend behind it); override
  in the variant when a real backend is registered.
- `agentCard.rpcUrl` auto-derives from `global.clusterBaseDomain`; override
  only for a non-standard Praxis Route hostname.
- The chart renders the card JSON into a conditional `static_response` filter
  in `praxis.yaml`; it does not need a filesystem mount or a load-balancer
  cluster for the card. This is compatible with the pinned
  `ghcr.io/praxis-proxy/ai:0.4.1` image, whose embedded Praxis Core 0.7.0
  rejects `static_files` as an unknown load-balancer cluster field.

### Static card configuration failure

If the Praxis pod fails configuration loading with an error that `static_files`
is an unknown field in an inline load-balancer cluster, the config is using an
unsupported cluster type. The pinned AI 0.4.1 image embeds Core 0.7.0, whose
strict cluster schema does not support that field. Keep static card serving on
the built-in `static_response` filter as rendered by this chart; do not model
files as load-balancer clusters.

### Readiness

`GET /readiness` (public, like the discovery paths) is answered by a
`static_response` filter returning 200 -- with no upstream clusters
configured there is no backend health to check. The startup/liveness probes
cover process health via the loopback admin listener. When a routed backend
is (re-)introduced, consider routing this path to it again so the proxy is
not marked ready while the backend is down (see the note in
`files/praxis.yaml`).

### No-backends router caveat (UNCONFIRMED GUESS)

The rendered `praxis.yaml` keeps a `router` filter with an empty `routes`
list so allow-listed callers hitting any non-public path fall through to a
clean 404 rather than a config-load error. Whether the pinned Praxis image
accepts an empty routes list has **not** been validated against a live
deployment of this no-backend configuration -- verify on the next cluster
rollout; if the image rejects it, remove the router filter entirely (the
static_response filters can serve every route this gateway currently
answers).

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
(see "Static agent card" and "Readiness" above):

- `GET /.well-known/agent-card.json` (and its `/a2a/`-prefixed twin) serves
  A2A discovery. The card advertises the public Praxis Route, never an
  internal Service.
- `GET /readiness` lets the proxy readiness probe verify the gateway is
  serving; with no backend configured it is answered by the proxy itself.
  (Historical note: when this chart fronted OLS, this path proxied the
  backend's `readiness_probe_get_method` route from the external fork
  `luis5tb/lightspeed-service@a2a-split` -- no `/v1` prefix, confirmed
  against that source. The path was originally `/health/ready`, copied
  from the prior RCA-era chart without verification against the actual
  service -- confirmed broken (404) against a live backend and fixed then.)

All other paths, including the A2A RPC endpoint, hit the authenticated
catch-all route; with no upstream clusters configured, a request that
survives the policy filter falls through the empty router to a 404. The
`policy` filter runs strictly before `router` in the filter chain, so a
denied request is never forwarded to any upstream.

## Configuration

Set `identity.keycloak.issuerUrl` to the exact token issuer and `route.host` to
the public A2A hostname (both derive from `global.clusterBaseDomain` by
default). In single-issuer mode (`identity.oidcIssuers` empty),
`identity.keycloak.audience` must name the audience this gateway's callers'
Keycloak tokens carry -- the chart `fail`s loudly when it is empty. There is
currently no per-cluster registry and no routed upstream: when a backend is
re-introduced (Task B of the migration plan), its Service coordinates and
TLS trust must come from operator-controlled values, never caller input.

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
