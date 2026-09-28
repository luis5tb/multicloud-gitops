# Keycloak/OIDC integration for the RCA and ACME A2A agents

This companion chart is installed after the validated pattern's `rhbk` chart.
It imports the RCA realm, its five clients, a SPIFFE identity provider, and
its groups into the deployed Keycloak.

The five clients serve distinct purposes and must not be conflated:

- `keycloak.clientId` (default `openshift-cli`): public client for browser/`oc
  login` OIDC flows (Native OIDC). Cannot authenticate itself. Intended as
  the future `cli` OIDC platform client once OpenShift Native OIDC is wired
  up (a separate, later addition) -- named for that role, not for the RCA
  agent, since it's purely human/CLI cluster access and has nothing to do
  with the agent request flow. (It used to be
  named `rca-agent`, which also doubled as the string in
  `keycloak.rcaAgentAudience` below by coincidence of sharing a name -- the
  two are unrelated and have been split apart.)
- `keycloak.mcpClientId` (default `rca-agent-mcp`): confidential client
  `charts/all/rca-agent` uses to exchange a caller's token for one scoped to
  OpenShift MCP.
- `keycloak.mcpAudienceClientId` (default `openshift-mcp`): confidential
  client that exists only so `rca-agent-mcp`'s token exchange has a real
  `client_id` to name as its `audience` parameter -- Keycloak's standard (V2)
  token exchange requires that parameter to be an actual client in the
  realm, not an arbitrary string. Never authenticates itself.
- `keycloak.acmeClientId` (default `acme-agent`): confidential client
  `charts/all/acme-agent` uses for its client-credentials
  call to Keycloak. Its service account belongs to `keycloak.acmeGroupName`
  (default `acme-agent-rca`, named `<caller>-<callee>` rather than just
  `acme-agent` so it reads as "acme-agent's rights when calling
  rca-agent") -- without this group, and the `groups` protocol mapper this
  chart attaches to the client, the token this client mints (and, unchanged,
  the token `rca-agent-mcp` exchanges it for) carries no groups claim at all,
  so any group-based authorization check added on top of this chart would
  have nothing to key on for calls attributed to acme-agent.
- `keycloak.consoleClientId` (default `openshift-console`): confidential
  client for the OpenShift web console. This commit only creates the
  Keycloak-side client; wiring it up as the `console` OIDC platform
  client is a separate, later addition.

`rca-agent-mcp` and `acme-agent` authenticate with a SPIFFE JWT-SVID (no
static secret) via Keycloak's federated client authentication feature
(`clientAuthenticatorType: federated-jwt`), against the `spiffe` identity
provider this chart also creates (`keycloak.spiffeIdentityProvider.*`). This
is fully declarative -- no manual Admin Console step is required for it, and
the `jwt.credential.sub` value each client expects is computed from
`keycloak.acmeWorkload`/`keycloak.mcpWorkload` (the namespace/ServiceAccount
pair ZTWIM/SPIRE issues that workload's JWT-SVID for) and
`keycloak.spiffeIdentityProvider.trustDomain`. Get any of those three values
wrong and Keycloak rejects the assertion with a generic "Invalid client or
Invalid client credentials" -- there is no more specific error surfaced to
the caller.

Two related gotchas, both baked into this chart's templates already but
worth knowing when debugging directly against Keycloak:

- Keycloak's JWT client validators reject the token request outright
  ("client_id parameter does not match sub claim") if a `client_id` form
  parameter is present and differs from the assertion's `sub` -- which it
  always will for a SPIFFE assertion, since `sub` is a SPIFFE ID, never the
  Keycloak client_id. Both agents' code omits `client_id` from these
  requests entirely; the client is resolved from the assertion's `sub`.
- The SPIFFE JWT-SVID used as `client_assertion` must itself be requested
  with the Keycloak realm issuer URL as its audience (`SPIFFE_JWT_AUDIENCE`
  in both agent charts) -- that is what Keycloak's federated-jwt validator
  checks the assertion's `aud` claim against by default, not the workload's
  own name.
- `rca-agent-mcp`'s actual RFC 8693 exchange additionally needs
  `standard.token.exchange.enabled: "true"` (a client attribute this chart
  sets) plus a protocol mapper adding `keycloak.mcpAudienceClientId` as an
  audience on `rca-agent-mcp` itself, and a protocol mapper on
  `acme-agent` adding `keycloak.mcpClientId` as an audience (a real
  client, `included.client.audience`) to the tokens it mints -- Keycloak's
  standard token exchange requires the *subject_token* to already carry the
  exchanging client as an audience, and the `audience` request parameter
  only ever narrows audiences a client scope already resolves, it never adds
  a new one. `acme-agent` also carries a *separate* mapper adding
  `keycloak.rcaAgentAudience` as an audience (`included.custom.audience`, no
  client involved) -- that one exists only to satisfy rca-agent's own inbound
  check and has nothing to do with the exchange itself. All of this is
  templated already; it's listed here because it is not obvious from
  Keycloak's own error messages if you ever need to debug it directly.
