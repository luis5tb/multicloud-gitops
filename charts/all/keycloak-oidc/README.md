# Keycloak/OIDC integration for the RCA agent

This companion chart is installed after the validated pattern's `rhbk` chart.
It imports the RCA realm/client/group into the deployed Keycloak and creates
the least-privilege AgenticRun RoleBinding for the OpenShift group emitted by
the Keycloak `groups` claim.

OpenShift Native OIDC is disabled by default because `Authentication/cluster`
is a singleton and changes cluster-wide login behavior. Enable it only when the
cluster is intended to use direct Keycloak OIDC authentication. The issuer URL
must be the stable Keycloak route and must exactly match the JWT `iss` claim;
the OpenShift control plane must be able to reach it.

The realm import is applied only when the realm does not already exist. The
Keycloak operator does not continuously reconcile edits made after import.
