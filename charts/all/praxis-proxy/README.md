# Praxis authorization gateway

This chart is the public Keycloak-validating A2A gateway for the greenfield
ACME → OpenShift Lightspeed path. It is **disabled by default** and has no RCA
or other legacy fallback. When enabled, it requires a static cluster registry,
a tested custom Praxis AI image containing `ols_cluster_guard`, and a verified
Keycloak issuer. The request header selects only among registered static
backends; it never supplies a URL or destination.

## Enablement gates

Do not set `enabled: true` or `clusterRouting.enabled: true` until all of these
are available:

- A published, scanned, digest-pinned Praxis AI candidate built from the
  tracked Praxis AI source with Core v0.7.2, including the duplicate-header
  guard. Core v0.7.2 is a pre-release; record AI and Core versions separately.
- A verified HTTPS Keycloak realm issuer and CA for the JWT/JWKS policy call.
- A real `global.olsClusters` registry entry with verified API URL/ID, static
  OLS Service host/port/SNI, and trust bundle. Example cluster values below are
  illustrative only; the candidate API URL is not confirmed.
- A verified public HTTPS Praxis Route and matching ACME downstream endpoint.

No downstream Praxis AI image has been published. The local-only image
`praxis-ai-local:0.4.1-core0.7.2` was built and validated against the guard
example, but its local digest is not a registry reference and must not be used
for cluster deployment.

## Static routing contract

The Keycloak identity plugin is configured with `role: user`, and the PPE
caller allow-list uses an explicit `deny(...)` action. `role: client` does not
bridge the normalized client identity into the APL predicate context, and a
bare boolean predicate is not itself a denial. The rendered rule denies when
the JWT's standard Keycloak `azp` claim differs from every configured caller;
the realm does not need a custom `client_id` protocol mapper. The chart test checks
both the identity role and the explicit deny expression. An implementation-side
live check was reported using two otherwise equivalent tokens with different
`azp` values; no live-cluster test was run from this workspace.

ACME uses the registry's `apiURL` to validate the user's explicit cluster
selection and sends the map key as `X-OLS-Cluster`. Praxis uses the map key and
only the static `upstreamHost`, `upstreamPort`, and `upstreamSNI` for proxying;
`apiURL` is ignored. `X-OLS-Cluster` is routing input, not authorization:
Keycloak validation and the ACME caller allow-list run first, then the guard
rejects missing, duplicated, malformed, or unknown selector headers before the
router. There is no default cluster, catch-all backend, URL-derived upstream,
or RCA fallback. Only the explicitly configured cluster-neutral public agent
card GET is headerless.

Illustrative shared values (do not activate the sample hostname):

```yaml
global:
  olsClusters:
    api-bm-cluster-e2e-bos-redhat-com-6443:
      apiURL: https://api.bm-cluster.e2e.bos.redhat.com:6443
      upstreamHost: lightspeed-app-server.openshift-lightspeed.svc.cluster.local
      upstreamPort: 8443
      upstreamSNI: lightspeed-app-server.openshift-lightspeed.svc
    # Future example only; all values are fake and commented out.
    # example-second-cluster:
    #   apiURL: https://api.example-second.invalid:6443
    #   upstreamHost: lightspeed-app-server.example-second.invalid
    #   upstreamPort: 443
    #   upstreamSNI: lightspeed.example-second.invalid
    #   upstreamCA:
    #     configMapName: example-second-ols-ca
    #     key: ca.crt

enabled: true
clusterRouting:
  enabled: true
  guardFilterEnabled: true # assertion only; the selected image must contain the guard
  discoveryCluster: api-bm-cluster-e2e-bos-redhat-com-6443

image:
  repository: quay.io/example/praxis-ai
  tag: "<verified-ai-version>"
  digest: sha256:<64-lowercase-hex-from-published-image>

identity:
  keycloak:
    issuerUrl: https://keycloak.<verified-domain>/realms/<realm>
    audience: lightspeed-a2a

route:
  enabled: true
  host: <verified-praxis-route>
```

The chart renders one exact `x-ols-cluster` router match and one load-balancer
cluster per registry key. Every upstream uses TLS certificate verification, a
static authority and configured SNI. It mounts a read-only CA bundle per
backend. By default, the chart creates `praxis-ols-service-ca` with OpenShift
service-CA injection and uses `service-ca.crt`; remote backends must specify an
explicitly provisioned trusted CA through `upstreamCA.configMapName`/`key`.
Rotating a custom CA requires rolling Praxis so its loaded TLS context refreshes.

The `/.well-known/agent-card.json` route is sent to the explicit
`clusterRouting.discoveryCluster`, never the first item or a fallback for A2A
RPC. The public Route's readiness probe uses the loopback-only Praxis admin
listener. `policy.allowPrivateIdp` and `policy.caBundleConfigMap` affect only
static Keycloak/JWKS callouts; they do not configure upstream TLS.

## Source and image validation status

The tracked Praxis AI v0.4.1 source snapshot now locks Praxis Core crates to
v0.7.2. The following source checks passed: 13 focused guard tests, all 1,436
filter unit tests, two guard integration tests, the proxy build, docs/example
checks, and a local container build. The local binary successfully validated
the guard example. `rustfmt` and Clippy were unavailable.

Still required before enabling routing: pin builder/runtime base images by
digest, complete the applicable schema/runtime checks, generate and scan an
SBOM/image, test real duplicate-wire-header rejection and HTTPS/SNI/CA routing
against the candidate container, publish the image, and record its registry
digest/provenance. No live Keycloak, SPIFFE, OLS, MCP, RBAC, CNI, or cluster
validation has been performed.

## Render checks

Defaults intentionally render no resources:

```bash
helm lint charts/all/praxis-proxy
helm template praxis-proxy charts/all/praxis-proxy --namespace lightspeed-agentic-operator
```

Routing-mode rendering is covered with one and two illustrative registry
entries by the chart validation work. Do not use a rendered sample as a
verified cluster destination.
