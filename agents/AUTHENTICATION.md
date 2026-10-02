# Authentication: ACME → Praxis → OpenShift Lightspeed → MCP

This document describes the intended greenfield request path. Source-level
checks exist, but live Keycloak, SPIFFE, OpenShift OIDC/RBAC, operator, and
network-policy validation remain deployment gates. The former RCA A2A service
and its standalone MCP server are no longer part of this pattern.

## Keep model credentials separate from caller identity

- ACME is the coordinator. Its LiteLLM key is a standing model credential; its
  configured coordinator model is `openai/gpt-6-luna`.
- OpenShift Lightspeed performs the investigation with the configured
  Gemini-on-Vertex provider (`vertex-google`, `gemini-3.8-flash`). It uses its
  own ADC credentials from Vault/ESO.
- Keycloak/SPIFFE tokens identify the ACME workload and delegate the caller
  identity to OpenShift for MCP requests. They are never model credentials.

Do not copy either model key into an identity token, or use a workload
ServiceAccount token as a substitute for the delegated MCP token.

## Request sequence

```mermaid
sequenceDiagram
    participant User as ACME UI/client
    participant ACME as acme-agent
    participant KC as Keycloak
    participant PX as Praxis gateway
    participant OLS as Lightspeed A2A service
    participant MCP as Operator-managed OpenShift MCP
    participant API as OpenShift API server

    User->>ACME: message/send with an explicit registered API URL
    ACME->>KC: client_credentials using ACME SPIFFE JWT-SVID
    KC-->>ACME: Token A (ACME workload; required audiences/groups)
    ACME->>PX: A2A POST + Token A + X-OLS-Cluster: <registered ID>
    PX->>KC: validate issuer/signature/audience via JWKS
    PX->>PX: authorize acme-agent, reject invalid/unknown selector
    PX->>OLS: forward original request and Token A to fixed HTTPS backend
    OLS->>OLS: independently validate Token A and configured cluster ID
    OLS->>KC: RFC 8693 exchange using OLS SPIFFE JWT-SVID
    KC-->>OLS: Token B (same sub, azp=lightspeed-mcp, aud=openshift-mcp)
    OLS->>MCP: invoke only built-in openshift MCP with Token B
    MCP->>API: forward Token B; API server validates issuer/audience/groups
    API-->>MCP: response subject to namespace-scoped OpenShift RBAC
    MCP-->>OLS: tool result
    OLS-->>PX: A2A response
    PX-->>ACME: A2A response
    ACME-->>User: coordinator response
```

### Token A: ACME → Praxis and Lightspeed

ACME obtains a short-lived Keycloak access token using its own SPIFFE
JWT-SVID as the federated client assertion. Praxis independently validates the
signature, issuer, required audiences, expiry, and allowed caller client before
routing. The PPE policy uses `role: user` and an explicit `deny(...)` action:
the former `role: client` plus a bare predicate was a silent no-op. An
implementation-side live check compared tokens differing only in `azp` and
reported the expected allow/deny; the chart regression here checks rendered
policy, not live traffic. Token A must have audiences `lightspeed-a2a` and
`lightspeed-mcp`, the standard Keycloak `azp=acme-agent` claim, and the
configured ACME group. No custom `client_id` token mapper is assumed. Lightspeed
validates the token again at the A2A boundary, including its inbound audience
and `azp=acme-agent`, and checks that the single
`X-OLS-Cluster` value matches that Lightspeed instance's configured target ID.

The header is a registry ID, not a URL and not authorization by itself. ACME
must resolve the user's explicit HTTPS API URL against the GitOps registry;
Praxis may route only to statically configured HTTPS upstreams. The public
agent-card GET is cluster-neutral. Requests without one valid registered ID
must fail closed before reaching an OLS backend.

### Token B: Lightspeed → MCP → API server

For each A2A invocation, Lightspeed obtains a fresh SPIFFE JWT-SVID and performs
a Keycloak RFC 8693 token exchange with Token A as `subject_token` and
`openshift-mcp` as the requested audience. The exchange omits `client_id` and
uses the federated `lightspeed-mcp` client. Before use, the service validates
Token B's issuer, audience, authorized client, subject, expiry, and required
caller group. It places Token B only in the per-request OLS MCP user-token
context and sends it only to the operator-managed server named `openshift`;
client-provided MCP headers cannot replace it. Other MCP servers must not
receive this token.

The downstream operator source configures its MCP server to require bearer
authentication and forward that bearer unchanged; these changes are not
available until a new versioned operator bundle/catalog is published and
installed. OpenShift's API server validates its configured Keycloak
issuer/audience and maps the subject/groups; namespace-scoped RoleBindings are
the final authorization boundary. A missing bearer must not fall back to the
MCP pod's ServiceAccount. `read_only` and denied-tool settings are defense in
depth, not a replacement for API-server RBAC.

The expected contract is Token B with the same `sub` as Token A,
`azp=lightspeed-mcp`, `aud=openshift-mcp`, and group
`acme-agent-lightspeed`. With the chart's default `groupsPrefix: "keycloak:"`,
OpenShift RBAC binds to `keycloak:acme-agent-lightspeed`. Verify those claims
and a real TokenReview/harmless API request on the target cluster before
granting MCP access. Do not log or persist bearer values.

## A2A and task limitations

The current Lightspeed adapter implements blocking `message/send`. It does not
claim task polling, cancellation, or streaming support. ACME rejects inbound
task methods while it has no durable authenticated task-to-cluster ownership.
Do not enable task continuation unless caller/context ownership and target
cluster affinity are persisted and tested.

## Deployment gates

Before enabling Praxis routing or Lightspeed introspection/A2A, provide and
verify all of the following:

1. A published, digest-pinned downstream Lightspeed operator bundle/catalog
   with new release metadata; the checked-in `1.1.4` CSV is stale and must not
   be published with the downstream changes under that version.
2. Published, scanned, digest-pinned Lightspeed service and Praxis AI images.
   The local Praxis candidate is not a registry image. The attempted local
   Lightspeed service build currently fails when the RHEL base image's DNF
   access returns HTTP 403. Confirm each image's source/Core version and
   runtime duplicate-header/TLS behavior before publication.
3. The real cluster API URL, stable registry ID, public Praxis route, OLS
   Service/SNI, and verified CA bundles. Example hostnames in chart docs are
   illustrative only.
4. The actual operator-created ServiceAccount/SPIFFE subject, Keycloak issuer
   CA, live Token A/Token B claim behavior, OpenShift OIDC audience mapping,
   reviewed namespace-scoped MCP RBAC, and NetworkPolicy enforcement.
5. A real Vertex ADC credential and successful Gemini/tool invocation.

Until those gates pass, leave the chart feature flags disabled and do not
represent Helm rendering or unit tests as live validation.
