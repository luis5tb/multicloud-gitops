# Praxis authorization gateway

This chart deploys the [Praxis AI proxy](https://github.com/praxis-proxy/ai)
with its embedded [Praxis Policy Engine (PPE)](https://github.com/praxis-proxy/policy)
in front of `rca-agent`. The public OpenShift Route targets this proxy;
the proxy validates the inbound Keycloak access token and authorizes the
caller with native APL before forwarding the unchanged request and bearer token
to RCA.

The RCA process continues to validate Keycloak tokens and its own SPIFFE
workload identity. This is intentional defense in depth and preserves its
per-request token-exchange behavior. It does not make an authorization
decision. The RCA chart's NetworkPolicy is enabled by default and permits
ingress only from Praxis pods so callers cannot bypass the APL policy through
the internal Service.

## Policy behavior

`files/policy.yaml` is rendered into the chart ConfigMap and loaded by the
Praxis `policy` filter at process startup. The `identity/jwt` plugin validates
`Authorization` against the configured Keycloak issuer, JWKS endpoint, and
`rca-agent` audience. It uses `role: user` so the verified token's explicitly
included `azp` claim is available to APL as `claim.azp`; `role: client` places
identity in `client.*` and does not bridge that claim into the predicates used
here. The catch-all has an explicit `deny(...)` effect when `azp` does not
match the configured allow-list. `policy.allowedCallers` defaults to
`[acme-agent]`; an absent or different caller, an invalid token, or a
policy/JWKS failure does not reach RCA. A bare boolean comparison in
`pre_invocation` is not an authorization action and does not deny a request,
so the explicit `deny(...)` is essential.

Two unauthenticated HTTP paths are intentionally forwarded:

- `GET /.well-known/agent-card.json` serves A2A discovery. The card must
  advertise the public Praxis Route, not the internal RCA Service.
- `GET /health/ready` lets the proxy readiness probe verify both the gateway
  and the RCA/Keycloak/SPIFFE readiness checks.

All other paths, including the A2A RPC endpoint, hit the authenticated
catch-all route. The Praxis filter is before routing in the chain, so a denied
request is not sent upstream.

## Configuration

Set `identity.keycloak.issuerUrl` to the exact token issuer and `route.host` to
the existing A2A hostname. The RCA application should set `route.enabled:
false`, keep `a2a.publicHost`/`publicPort`/`publicProtocol` aligned with the
public Route, and enable `networkPolicy.enabled`. Keep the proxy and RCA in the
same namespace unless you also update the RCA NetworkPolicy's pod and namespace
selectors. The upstream hostname and port are chart values, not caller input.

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
(config-load-time validation of the load_balancer cluster endpoint) and
`insecure_options.allow_private_upstreams` (the runtime connection check) for
this static in-cluster upstream. Those are broad private-address opt-ins, so
this chart keeps the destination fixed and relies on the RCA NetworkPolicy for
ingress isolation. Do not use these flags to proxy user-supplied destinations.

## Checks and diagnostics

```bash
helm lint charts/all/praxis-proxy
helm template praxis-proxy charts/all/praxis-proxy \
  --set identity.keycloak.issuerUrl=https://keycloak.example.com/realms/rca
pytest -q agents/rca_agent/tests
```

At runtime, inspect the rendered policy and proxy logs with:

```bash
oc get configmap praxis-proxy -n lightspeed-agentic-operator -o yaml
oc logs -n lightspeed-agentic-operator -l app.kubernetes.io/name=praxis-proxy
oc get networkpolicy rca-agent -n lightspeed-agentic-operator -o yaml
```

## Upstream maturity and pinning

This Pattern currently pins the `0.4.1` upstream release by its image digest
in `values.yaml`; its upstream release is prerelease software. Praxis AI labels
itself alpha, and PPE's pre-1.0 public API may change between minor releases.
This integration is not currently a Red Hat-supported Praxis distribution.
Review upstream release notes and revalidate the chart before changing
`image.tag`; for production rollouts, pin the verified image digest as well as
its version.
