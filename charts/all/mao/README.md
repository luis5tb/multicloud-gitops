# MAO runtime charts

These charts package the runtime components from `uie-mas-hosted` for use by
the Multicloud GitOps Pattern:

- `mongo` — persistent MongoDB storage
- `redis` — internal Redis Streams storage
- `temporal` — internal Temporal server
- `mas-worker` — Temporal workflow worker
- `mas-api` — externally reachable MAS API
- `mas-a2a` — optional inbound A2A endpoint for one persisted orchestrator
  blueprint (dormant unless explicitly enabled)

The Pattern deploys these charts into the `mao` namespace. The charts are
kept as separate applications so each component can be upgraded or scaled
independently. The default values are suitable for a development installation;
the API Route is already enabled, so restrict it until you replace the `dev`
identity mode with configured Keycloak or production SSO authentication and
real API administrators. See the `mas-a2a` activation prerequisites below.

The inbound A2A chart is deliberately safe to register before its operator has
selected a blueprint or authorized caller subjects: `a2a.enabled` defaults to
false and renders no resources. Enable it only after a persisted
`a2a.blueprintId`, a token client emitting the distinct MAS A2A audience, and
an exact Keycloak subject allow-list are confirmed. See
[`mas-a2a/README.md`](mas-a2a/README.md) for issuer/audience validation,
Keycloak's one-shot realm-import caveat and documented existing-realm mapper
step, HITL approval/resume behavior, the Mongo/Redis requirements, and image
build/pin guidance. Use the same pinned MAS image revision for API, worker,
and A2A; the worker and A2A service must share Redis-backed channels for
A2A streaming and cross-process HITL.

The A2A build (`uie-mas-hosted` branch `a2a-inbound-deploy`: the A2A PR
stack plus multi-issuer OIDC and Vertex ADC) seals each A2A caller's
credentials with Fernet keys shared by the API, worker, and A2A service.
`runCredentials` in each chart reads them from the Vault entry
`global/mao-run-credentials` (see `values-secret.yaml.template`); `mas-a2a`
refuses to render without them. The worker's `credentialRenewal` settings
bound how long a task waits in `TASK_STATE_AUTH_REQUIRED` for the caller to
send a fresh token; keep the timeout below OpenShift Lightspeed's
`appServerPatch.a2a.authRequiredTimeoutSeconds`. `hitl.approvalCollection` and
`credentialRenewal.collection` must match across the charts that set them.

The source repository also contains a Vault Secrets Operator chart. It is not
deployed here because the Pattern already installs Vault and the Validated
Patterns `vault-backend` ClusterSecretStore through ESO. The runtime defaults
create local connection Secrets for MongoDB and Redis, so the MAO stack does
not require an additional secret operator.

The source implementation is maintained in the private
[`uie-mas-hosted`](https://gitlab.cee.redhat.com/ai_tools/uie-mas-hosted)
repository. Keep the copied chart templates aligned when the source chart
changes.
