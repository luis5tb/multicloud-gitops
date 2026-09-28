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
`rca-agent` audience. Its `claim_mapper` (`keycloak`/`standard` presets)
normalizes the token's client identity -- `azp`, `client_id`, or the
pre-2023 Keycloak `clientId` -- into a single mapped field always named
`client_id`; `azp` itself is never the exposed field, so APL matches
`claim.client_id` (not `claim.azp`, and not `sub`). `policy.allowedCallers`
defaults to the explicit allow-list `[acme-agent]`; an absent identity, a
different client, or a policy/JWKS failure does not reach RCA. Also note
`pre_invocation` entries must be bare boolean expressions (see the two
unauthenticated routes' `"allow"` entries) -- wrapping a comparison in
`require(...)` is accepted by the config schema but never evaluates true.

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
