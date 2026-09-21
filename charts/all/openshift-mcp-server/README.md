# OpenShift MCP server

This chart deploys the upstream `openshift/openshift-mcp-server` image in HTTP
mode. It enables only `resources_create_or_update` and `resources_get`, and
configures OAuth passthrough so the inbound `Authorization` bearer is used for
the Kubernetes API request. The server's ServiceAccount token is disabled; it
does not receive an independent AgenticRun identity.

The RCA agent validates the bearer against Keycloak first. OpenShift Native
OIDC then validates the same token at the Kubernetes API and maps its `groups`
claim to the `keycloak:rca-users` RoleBinding created by the companion
`keycloak-oidc` chart.

The default image tag is `latest` only to follow the upstream chart defaults.
Pin an approved immutable tag or digest before production deployment.
