# Praxis authorization gateway

This chart deploys the [Praxis AI proxy](https://github.com/praxis-proxy/ai)
with its embedded [Praxis Policy Engine (PPE)](https://github.com/praxis-proxy/policy)
in front of one or more per-cluster OpenShift Lightspeed (OLS) app-servers. The
public OpenShift Route targets this proxy; the proxy validates the inbound
Keycloak access token, authorizes the caller with native APL, and then routes
to the OLS backend selected by an exact `X-OLS-Cluster` header match -- see
"Header routing" below. See `LIGHTSPEED_DESIGN.md` SS7 and
`LIGHTSPEED_IMPLEMENTATION_PLAN.md` Phase 6 for the full design and rollout
plan this chart implements.

The OLS app-server (and its operator-managed MCP backend) independently
validates the Keycloak token again and performs its own RFC 8693 token
exchange. This is intentional defense in depth; this chart does not make that
decision for it. Restrict OLS ingress to Praxis via the OLS chart's own
NetworkPolicy so callers cannot bypass this gateway's APL policy through the
internal Service.

## Cluster registry and header routing

`files/praxis.yaml` renders one Core `router` route and one `load_balancer`
cluster per entry in `.Values.global.olsClusters` (the shared registry
populated in `variants/<variant>/values-<variant>.yaml`; see the frozen
interface in the implementation plan). Each route matches `X-OLS-Cluster`
**exactly** against that entry's map key and forwards only to that entry's
`upstreamHost:upstreamPort` over HTTPS with `upstreamSNI` and a verified
server certificate -- never to a caller-supplied URL, and never `apiURL`
(that field exists only for ACME's own validation and must never reach
Praxis). Core's `routes[].headers` exact-match syntax and
`clusters[].tls.{sni,verify,ca.ca_path}` schema are identical between Core
v0.7.0 (embedded in the pinned `ai:0.4.1` image) and the v0.7.2 docs cited
below, and were validated structurally with `--validate` and live-tested with
two fake HTTPS backends against the real pinned binary (see "Validation
evidence").

On an absent, unregistered, or otherwise non-matching `X-OLS-Cluster` value,
Core's router rejects with a plain **HTTP 404** and forwards nothing -- there
is no default/catch-all cluster (confirmed by reading
`crates/filter/src/builtins/http/traffic_management/router/mod.rs` at the
`v0.7.0` tag and reproduced live against the pinned image).

**Known gap -- duplicate headers are not rejected.** Core's header matcher
(`router/matching.rs::headers_match`) checks *all* values of a repeated
header with OR semantics: a request carrying two `X-OLS-Cluster` values, one
legitimate and one not, still matches if *any* value equals the route's id.
There is no router config knob to instead reject a multi-valued header
outright; this chart's configuration cannot close this gap on its own.
Reproduced live: a request with `X-OLS-Cluster: bogus-id` and
`X-OLS-Cluster: cluster-a` both present routed successfully to `cluster-a`.
If this matters before ACME's own header-setting code is trusted to never
duplicate the header, it needs an upstream fix or a pre-router guard this
pinned image does not otherwise provide.

**Known gap -- public-path backend selection with 2+ clusters.** The
unauthenticated `/.well-known/agent-card.json` and `/health/ready` routes
(see "Policy behavior") need exactly one upstream, but the registry has no
"primary" concept. This chart picks the lexically first registry key. This is
fine for the single currently-registered cluster but needs a real decision
before a second `olsClusters` entry is registered -- see
`LIGHTSPEED_DESIGN.md` SS7 ("Multi-cluster session safety").

## Upstream TLS trust

The operator-managed `lightspeed-app-server` Service is HTTPS-only. This
chart's policy-plugin `SSL_CERT_FILE` (see "Keycloak CA bundle" below) is a
**separate** trust store used only for the Keycloak JWKS fetch -- it is not
automatically valid for the OLS app-server's serving certificate. When
`upstreamTLS.enabled` (default `true`), this chart instead creates its own
ConfigMap annotated `service.beta.openshift.io/inject-cabundle: "true"`; the
in-cluster OpenShift service-ca-operator injects the current serving CA
bundle into it with no sync Job/CronJob needed, and every registered
cluster's `load_balancer` entry trusts it via `tls.ca.ca_path`. This only
establishes trust for Services in *this same* OpenShift cluster: a future
cross-cluster `olsClusters` entry needs a distributed/synced CA source (for
example, something closer to the `policy.caBundleSync` mechanism below), not
this annotation. This chart also does not force a Deployment rollout on CA
rotation (unlike `policy.caBundleSync`'s CronJob+checksum pattern) -- a known,
accepted gap for this pass given OpenShift's service-ca rotates far less
often than the ingress CA that mechanism targets.

## Policy behavior

`files/policy.yaml` is rendered into the chart ConfigMap and loaded by the
Praxis `policy` filter at process startup. The `identity/jwt` plugin validates
`Authorization` against the configured Keycloak issuer, JWKS endpoint, and
configured audience. `policy.allowedCallers` defaults to the explicit
allow-list `[acme-agent]`; an absent identity, a different client, or a
policy/JWKS failure does not reach any OLS backend.

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

Two unauthenticated HTTP paths are intentionally forwarded (see "Known gap --
public-path backend selection" above for which backend answers them):

- `GET /.well-known/agent-card.json` serves A2A discovery. The card must
  advertise the public Praxis Route, not an internal OLS Service.
- `GET /health/ready` lets the proxy readiness probe verify both the gateway
  and the selected OLS backend's readiness. These paths were left unchanged
  from the prior RCA-era chart; if the real OLS app-server's A2A/health paths
  differ, update them here (no visibility into the vendored A2A/OLS service
  from this workstream at the time of writing).

All other paths, including the A2A RPC endpoint, hit the authenticated
catch-all route and then the per-cluster header-routing rules in
`files/praxis.yaml`. The `policy` filter runs strictly before `router`/
`load_balancer` in the filter chain, so a denied request is never forwarded
to any upstream.

## Configuration

Set `identity.keycloak.issuerUrl` to the exact token issuer and `route.host` to
the public A2A hostname. Populate `.Values.global.olsClusters` (normally via
the shared `variants/<variant>/values-<variant>.yaml` registry, not a
chart-local override) with one entry per registered OLS cluster; Praxis reads
only the map key plus `upstreamHost`/`upstreamPort`/`upstreamSNI` from each
entry and never `apiURL`. The OLS application's own chart should restrict
ingress to this proxy via NetworkPolicy. Keep the proxy and each OLS instance
reachable on the documented Service DNS name; the upstream hostname/port come
only from the registry, never from caller input.

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
these static in-cluster upstreams. Those are broad private-address opt-ins, so
this chart keeps every destination fixed (sourced only from
`global.olsClusters`) and relies on each OLS instance's own NetworkPolicy for
ingress isolation. Do not use these flags to proxy user-supplied destinations.

## Checks and diagnostics

```bash
helm lint charts/all/praxis-proxy -f values-global.yaml -f variants/standalone/values-standalone.yaml
helm template praxis-proxy charts/all/praxis-proxy \
  -f values-global.yaml -f variants/standalone/values-standalone.yaml
```

`global.olsClusters` must contain at least one entry (the chart `fail`s
loudly otherwise); a bare `helm lint charts/all/praxis-proxy` with no
registry values file is expected to fail for this reason.

At runtime, inspect the rendered policy and proxy logs with:

```bash
oc get configmap praxis-proxy -n praxis-proxy -o yaml
oc logs -n praxis-proxy -l app.kubernetes.io/name=praxis-proxy
oc get networkpolicy -n openshift-lightspeed -o yaml
```

### Validation evidence (T6.1-T6.3, 2026-10-01)

Rendered this chart's `praxis.yaml`/`policy.yaml` for one and two
`global.olsClusters` entries, validated the result against the real pinned
`ghcr.io/praxis-proxy/ai@sha256:83c86...` image with
`praxis-ai --config ... --validate` (passes cleanly), then ran it live
(`podman run ... --network host`) against two local HTTPS backends
(self-signed certs, distinct SNI per backend) and a local JWKS server backing
hand-signed RS256 test tokens. Observed, against the pinned binary:

| Case | Result |
|---|---|
| Public `/.well-known/agent-card.json`, `/health/ready`, no auth | Forwarded to the registry's primary cluster, 200 from backend |
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
