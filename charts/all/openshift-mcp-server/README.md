# OpenShift MCP server

This chart deploys the upstream `openshift/openshift-mcp-server` image in HTTP
mode. It enables only `resources_create_or_update` and `resources_get`, and
configures OAuth passthrough so the inbound `Authorization` bearer is used for
the Kubernetes API request. The server's ServiceAccount token is disabled; it
does not receive an independent AgenticRun identity.

The RCA agent validates the bearer against Keycloak first. OpenShift Native
OIDC then validates the same token at the Kubernetes API and maps its `groups`
claim to the `keycloak:rca-agenticrun`/`keycloak:ericsson-agent-rca` RoleBindings
created by the companion `keycloak-oidc` chart, which also enforces which
`spec.targetNamespaces` each group may request via a `ValidatingAdmissionPolicy`
(see that chart's README).

## This is intentionally a token-passthrough gateway, not a resource server

`server.requireOAuth: true` + `server.skipJWTVerification: true` +
`server.clusterAuthMode: passthrough` together mean **this server never makes
an authorization decision of its own** -- it forwards the inbound bearer
straight through to the Kubernetes API server, which is the only party that
validates the token's issuer/audience/signature and applies RBAC
(`agents/AUTHENTICATION.md`, "Per-hop identity and validation", step 7). RCA
performs the RFC 8693 token exchange itself
(`agents/rca_agent/rca_agent/identity.py`'s `KeycloakTokenExchanger`) before
ever calling this server; this server never authenticates to Keycloak or
establishes an identity of its own. This is a deliberate choice, not a gap to
close later: moving the exchange into MCP would require it to hold and
authenticate with a SPIFFE identity independently, and upstream's own OIDC/
token-exchange support is explicitly documented as preview
(https://github.com/containers/kubernetes-mcp-server/blob/main/docs/KEYCLOAK_OIDC_SETUP.md).
Keep this design unless MCP specifically needs to establish caller identity
on its own.

`skipJWTVerification: true` is only safe because of that trust chain --
`networkPolicy.enabled` (default `true`) is a compensating network-layer
control restricting ingress to `networkPolicy.callerNamespace` (rca-agent's
namespace), on top of this Service already being `ClusterIP`-only.

## Why `resources_create_or_update`, not a create-only tool

RCA only ever needs to create `AgenticRun`s (see
`agents/rca_agent/rca_agent/mcp_agentic_run.py`'s `build_analysis_only_run`,
which always sets a fresh, unique `metadata.name`), and `agentic-rbac.yaml`
grants no `list`/`watch`/`update`, so a real update-in-place attempt is still
rejected by the API server's own RBAC regardless of what this tool is named.
It does need `patch`, though, even for names it has never seen before:
`resources_create_or_update` implements its upsert via Kubernetes Server-Side
Apply, which the API server always processes as an HTTP `PATCH` -- including
when the object doesn't exist yet -- so RBAC checks the `patch` verb, not
`create`, for every call this tool makes (confirmed in production: granting
only `create`/`get` was rejected with "cannot patch resource agenticruns" on
a brand-new name; `agentic-rbac.yaml` grants `patch` for exactly this
reason). `resources_create_or_update` is upstream's only generic write tool
(checked against `containers/kubernetes-mcp-server`'s `pkg/kubernetes/resources.go`
-- there is no separate create-only tool to switch to), so the tool's name
implying more capability than RBAC actually grants is an upstream constraint,
not something this chart can change.

The default image tag is `latest` only to follow the upstream chart defaults.
Pin an approved immutable tag or digest before production deployment.
