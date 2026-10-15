# MAO inbound A2A deployment

This chart runs one configured `uie-mas-hosted` orchestrator blueprint as an
A2A 1.0 service, separate from the Flask API and Temporal worker. It shares
MongoDB, Redis, and Temporal with those services. One release exposes one
blueprint; use a separate release for each additional public agent.

## Safe activation

The chart is intentionally dormant by default: `a2a.enabled: false` renders no
Kubernetes resources. This lets the Pattern register the chart without
inventing an orchestrator blueprint ID or granting access to an unknown
caller. Do not set `a2a.enabled: true` until the MAS operator has confirmed
all of:

1. An existing persisted orchestrator blueprint ID in `a2a.blueprintId`.
2. At least one exact Keycloak token subject in `a2a.allowedPrincipals`, or an
   existing Secret name with key `A2A_ALLOWED_PRINCIPALS` in
   `a2a.allowedPrincipalsSecretName`.
3. A caller token whose `aud` contains the dedicated MAS audience configured
    in `identity.keycloakAudience` (default realm-import audience:
    `keycloak.masA2aAudience`, `mas-a2a`). It must not be a reserved
    OpenShift/OLS/MCP audience; operator-specific reserved equivalents can be
    listed in `identity.forbiddenAudiences`.

### Prepare the published blueprint

`a2a.blueprintId` must identify an actual persisted orchestrator blueprint,
not a fixture or an unsaved draft. Create the orchestrator blueprint through
the authenticated, admin-only MAS API endpoint `POST
/api/blueprints/blueprint.save`, passing its YAML/JSON string in the
`blueprintRaw` field. The API returns `blueprint_id`; verify that the saved
blueprint resolves with `GET
/api/blueprints/available.blueprints.resolved.get?blueprintId=<blueprint_id>`
before using that exact ID in the A2A values. A2A loads the configured
blueprint from MongoDB at service startup and fails readiness/startup if it
cannot resolve it.

Do not use `uie-mas-hosted/run/fixtures/blueprint_a2a_agent.yml` as the
published inbound blueprint: it is an **outbound A2A client-node example**
(`a2a_agent_node` with a remote `base_url` and `agent_card`), not a ready
inbound orchestrator. No fixture ID is configured by this chart.

The allow-list matches the validated JWT `user_id` or `sub` claim exactly. For
the Keycloak validator, the subject is normally the opaque Keycloak user `sub`,
not a username, group, or email address. Obtain it from an authorized test
token's decoded claims; never log or copy the token itself. The chart fails
rendering when enabled without the blueprint, principal allow-list, production
issuer/audience, or pinned image configuration.

Example activation values (replace every angle-bracketed value only after it
has been confirmed):

```yaml
a2a:
  enabled: true
  blueprintId: "<persisted-orchestrator-blueprint-id>"
  publicBaseUrl: "https://<public-host>"
  allowedPrincipals:
    - "<validated-keycloak-token-subject>"
identity:
  providerMode: keycloak
  # If omitted, issuer is derived from global.clusterBaseDomain and
  # global.keycloakRealm, the same anchors used by keycloak-oidc.
  keycloakIssuerUri: ""
  # Must match the distinct MAS audience actually present in caller tokens.
  keycloakAudience: "<confirmed-mas-a2a-audience>"
image:
  repository: "<registry>/<repository>/mas-api"
  tag: "<release-tag-or-source-commit>"
```

Keep `route.enabled: false` when exposing this service through a gateway and
set `a2a.publicBaseUrl` to that gateway's HTTPS base URL. For a direct
OpenShift Route, set `route.enabled: true`, optionally set `route.host`, and
make `a2a.publicBaseUrl` the resulting HTTPS URL. Agent Card discovery at
`/.well-known/agent-card.json` and health probes are public; JSON-RPC calls
require a valid Bearer token and an allow-listed subject. An enabled direct
Route must use `route.tls.termination: edge` and
`route.tls.insecureEdgeTerminationPolicy: Redirect`. The MAS A2A process binds
plain HTTP on port 8003 (`ROLE=a2a-serve` starts Uvicorn without TLS
certificate/key options), so passthrough and reencrypt termination would send
TLS to an HTTP backend and are rejected. Rendering also fails if Route TLS is
missing, insecure, or does not redirect HTTP.

### Keycloak realm caveat

MAS uses a distinct audience, `mas-a2a` by default in the Keycloak realm
import, not `lightspeed-a2a`, `openshift-mcp`, or `openshift`. The mapper is
added to the `openshift-cli` client in
`charts/all/keycloak-oidc/templates/keycloak-realm-import.yaml`; set
`identity.keycloakAudience` to the exact audience that the token issuer/client
actually emits. The chart leaves this value empty and refuses enabled
Keycloak-mode rendering until the operator configures it.

`KeycloakRealmImport` is a one-shot import. If the existing realm was imported
before the `mas-a2a-audience` mapper was added, changing the YAML does not
reconcile that live client. Before activating MAS A2A, inspect a token minted
for the selected caller/client and confirm its `iss`, `aud` (including the
dedicated MAS audience), and `sub`. If the mapper is absent, add an Audience
protocol mapper to the actual token-issuing Keycloak client in the Admin
Console: mapper type **Audience**, Included Custom Audience equal to
`keycloak.masA2aAudience` (default `mas-a2a`), and Access Token Claim enabled.
Do not delete/re-import a live realm merely to add the mapper. Then configure
`identity.keycloakAudience` to that value and populate the principal allow-list
with exact validated subjects. The chart verifies issuer, signature,
expiration, and configured audience; the allow-list checks the exact subject.
It does not authorize by Keycloak groups.

The Keycloak issuer helper derives
`https://keycloak.apps.<clusterBaseDomain>/realms/<keycloakRealm>` from the
shared cluster values when `identity.keycloakIssuerUri` is empty. The optional
`identity.keycloakJwksUri` overrides the validator's default
`<issuer>/protocol/openid-connect/certs` endpoint. TLS certificate validation
is on by default; only set `keycloakVerifySsl: false` for a controlled
non-production test.

The chart rejects known OpenShift/OLS/MCP audiences (`openshift`,
`openshift-mcp`, `lightspeed-a2a`, `lightspeed-mcp`, `openshift-cli`, and
`openshift-console`) as the MAS A2A audience, including when one appears among
comma-delimited values. Add organization-specific reserved equivalents with
`identity.forbiddenAudiences`; do not add a caller's real audience until it has
been confirmed from an authorized token's decoded claims.

### Private Keycloak Route CA (optional)

If the Keycloak Route's issuer/JWKS endpoint uses a private CA not already
trusted by the MAS image, configure `identity.keycloakCaTrust.enabled: true`
in each Keycloak-mode MAS chart (API, worker, and A2A as applicable). The
`keycloak-oidc` chart must have `openshiftOIDC.caBundleSync.enabled: true` and
`openshiftOIDC.caBundleSync.additionalTargetNamespaces: [mao]`; its bootstrap
Job and rotation CronJob publish the same `keycloak-oidc-ca` ConfigMap in
`mao`. Override `identity.keycloakCaTrust.configMapName`/`configMapKey` only
when the Keycloak chart uses a different name/key.

Each MAS pod mounts that ConfigMap read-only. An init container using the exact
runtime image validates the image's existing system/public CA bundle and the
mounted private CA, then appends the private CA to the existing roots in a
pod-local bundle. Missing, empty, or invalid roots/CA fail the init container;
the app uses the combined file through `SSL_CERT_FILE` (and related CA env
vars) while `identity.keycloakVerifySsl` remains true. This never replaces
public roots with the private CA and never disables certificate or hostname
verification. The bundle output is on an `emptyDir`; on clusters that require
an explicit writable-volume group, set `podSecurityContext.fsGroup` to a
namespace-permitted value.

The Keycloak chart refreshes the ConfigMap on its configured schedule after CA
rotation. Projected ConfigMap updates alone may not refresh a cached PyJWKClient
or SSL context, so roll the MAS API, worker, and A2A pods after rotation. This
is a deployment prerequisite only: the charts and CA rotation behavior have
not been validated against a live cluster.

`charts/all/mao/mas-api` and `mas-worker` expose the same Keycloak validator
settings. If changing their `identity.providerMode` to `keycloak`, configure
the audience and ensure the issuer resolves there too: the worker initializes
the shared application container and otherwise fails startup when Keycloak
mode has no issuer. Keep API, worker, and A2A settings aligned.

### Prerequisite: harden the existing public MAS API

Do not enable inbound A2A while the existing `mas-api` Route still accepts the
development identity mode. The API is independently reachable and its chart
defaults `route.enabled` to true. Before A2A activation, configure the API with
`identity.providerMode: keycloak`, a non-empty, API-specific
`identity.keycloakAudience` that the selected issuer/client actually puts in
access tokens, and `identity.adminAllowedUsers` containing the real authorized
Keycloak usernames (not the development `dev` principal or placeholder
values). Do not reuse the A2A audience for the API; arrange for the API token
client to emit its dedicated audience and verify it in a real token first.
The API chart now rejects a missing or delimiter/whitespace-only Keycloak
audience, but cannot validate the live issuer's token contents or the admin
allow-list against your organization.

Upgrade `mas-api`, `mas-worker`, and `mas-a2a` to the same newly built,
immutable image tag or digest containing inbound A2A support. The worker has
no public Route, but it must run that same application revision; if it is put
in Keycloak mode, configure its issuer and non-empty audience too because its
application container initializes the shared token validator at startup.
`adminAllowedUsers` applies to the public API's admin operations, not the
A2A principal allow-list. Keep the API admin usernames and A2A's exact JWT
subject allow-list as separate authorization controls.

## Image build and pinning

The A2A runtime is a role in the existing MAS container image
(`ROLE=a2a-serve`), not a separate source tree. Build from the exact
`uie-mas-hosted` source revision that contains inbound A2A support and publish
the result to the deployment's approved registry. For example, from that
source checkout:

```bash
REV=$(git rev-parse --short=12 HEAD)
podman build -f Dockerfile -t <registry>/<repository>/mas-api:${REV} .
podman push <registry>/<repository>/mas-api:${REV}
```

Use the release pipeline's immutable digest where available. Set
`image.repository` plus a non-`latest` `image.tag`, or set `image.digest` to a
`sha256:...` digest. Use the same image revision for API, worker, and A2A so
the Temporal activities and A2A adapter have compatible code and dependencies.
The chart deliberately contains no environment-specific registry or image
reference and rejects `latest` when enabled. Keep real registry values and
the tag/digest in deployment-specific values, not chart defaults.

## Runtime and dependencies

Install/enable A2A only after the shared MongoDB, Redis, Temporal service, and
Temporal worker are ready and the configured blueprint exists in MongoDB.
MongoDB is required for durable A2A task/context ownership and recovery after
pod restart. The Mongo connection Secret must provide `db.host` and `db.port`;
`db.uri`, username, and password keys are optional. The chart's Mongo TLS
default (`false`) matches the in-pattern Mongo chart; change both consistently
if TLS is enabled.

Redis is required for the A2A process to communicate with the Temporal worker
through the shared channel used by HITL approvals. It also backs streaming
event readers and, by default, cluster-wide request/stream limits. The Redis
connection Secret must provide `db.endpoint` and `db.port`; `db.auth_token` is
optional. Do not remove Redis references just to disable streaming: without a
shared Redis channel, an A2A caller cannot reliably resume a worker's
cross-process approval. `a2a.streamingEnabled: false` disables the advertised
streaming capability but does not disable the shared Redis channel.

When a MAS tool call pauses for human approval, the task state is
`INPUT_REQUIRED` (non-terminal). Reply using the same `task_id` and a new
`message_id`, with either a structured decision data part or a plain-text
`approve`/`yes` or `reject`/`no`/`deny` prefix. `modify` and `redirect` require
structured decision data. A2A task records are retained by Mongo TTL for 30
days by default; this bounds idempotency replay, not the separate MAS session
history. Reattach/subscribe after a decision so the stream sees the next
worker event and clears `INPUT_REQUIRED`; polling alone can continue to show
the pending state until reattachment or the approval gate's timeout.

Streaming defaults to enabled with a 300-second maximum. When a direct Route
is enabled its timeout (360 seconds by default) must exceed the maximum stream
duration; the application shutdown timeout and pod termination grace period
must also remain ordered as chart validation requires. Configure any external
gateway timeout to exceed the Route timeout. Redis cluster limits default to
100 requests/minute and 10 concurrent streams per principal and are shared
across replicas when Redis is available.

The A2A ServiceAccount has no Kubernetes API permissions and does not
automount a token. Mongo, Redis, and Temporal host/port defaults target the
existing in-pattern services in namespace `mao`; override them when using a
different deployment topology.

## Render and verify

The default chart renders no resources. Check that safe default and verify the
enabled chart with operator-provided test values before activation:

```bash
helm template mas-a2a charts/all/mao/mas-a2a
helm lint charts/all/mao/mas-a2a --values <a2a-values.yaml>
helm template mas-a2a charts/all/mao/mas-a2a \
  --namespace mao --values <a2a-values.yaml>
```

After deployment, verify readiness and the public card URL, then test
unauthenticated (401), invalid-token (401), non-allow-listed subject (403),
task-owner isolation, idempotent replay, streaming, and approval/resume in a
non-production namespace. The upstream implementation's full rollout checklist
is `uie-mas-hosted/docs/a2a-inbound-verification.md`; local Helm rendering does
not replace a cluster-level Redis, Mongo, Route, restart, or multi-replica test.

## Rollback and removal

```bash
helm rollback mas-a2a -n <namespace>
helm uninstall mas-a2a -n <namespace>
```
