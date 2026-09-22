# Keycloak/OIDC integration for the RCA and Ericsson A2A agents

This companion chart is installed after the validated pattern's `rhbk` chart.
It imports the RCA realm, three clients, and a group into the deployed
Keycloak, and creates the least-privilege AgenticRun RoleBinding for the
OpenShift group emitted by the Keycloak `groups` claim.

The three clients serve distinct purposes and must not be conflated:

- `keycloak.clientId` (default `rca-agent`): public client for browser/`oc
  login` OIDC flows (Native OIDC). Cannot authenticate itself.
- `keycloak.mcpClientId` (default `rca-agent-mcp`): confidential client
  `charts/all/rca-agent` uses to exchange a caller's token for one scoped to
  OpenShift MCP.
- `keycloak.ericssonClientId` (default `ericsson-agent`): confidential client
  `charts/all/ericsson-agent` uses for its client-credentials
  call to Keycloak.

The two confidential clients are intended to authenticate with a SPIFFE
JWT-SVID (no static secret) via Keycloak's federated client authentication
feature, against the `spiffe` identity provider configured by the `keycloak`
application's `spiffeIdentityProvider` override
(`variants/standalone/values-standalone.yaml`). That feature's exact
client-side fields are Keycloak-version-specific and not yet declarative in
this chart's `KeycloakRealmImport`; configure it once in the Keycloak Admin
Console under each confidential client's Credentials tab, then grant
`rca-agent-mcp` token-exchange permission for the `openshift-mcp` audience.

OpenShift Native OIDC is disabled by default because `Authentication/cluster`
is a singleton and changes cluster-wide login behavior. Enable it only when the
cluster is intended to use direct Keycloak OIDC authentication. The issuer URL
must be the stable Keycloak route and must exactly match the JWT `iss` claim;
the OpenShift control plane must be able to reach it.

The realm import is applied only when the realm does not already exist. The
Keycloak operator does not continuously reconcile edits made after import.
