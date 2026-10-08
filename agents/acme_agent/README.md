# ACME A2A agent

This is the first agent under `agents/`. It is a small Google Agent Development
Kit (ADK) agent that exposes its own A2A endpoint and routes each request to one
of several configurable downstream A2A sub-agents. It also serves a browser UI
from the same HTTP service.

The exposed `acme_agent` is a local ADK coordinator agent. Its configured
`RemoteA2aAgent` children are downstream peers, not the public entrypoint. The
coordinator selects a peer using its description, passes the request to that
peer, and returns the peer's result without doing domain-specific work itself.

Each peer is wrapped in `google.adk.tools.agent_tool.AgentTool` and attached
via `tools=`, not `sub_agents=`. A bare `RemoteA2aAgent` with no `mode` set
(the only option our downstream A2A server supports -- see
`agents/AUTHENTICATION.md`) is, per its own docstring, "a plain
`transfer_to_agent` target": calling `transfer_to_agent` ends the current
task immediately once the hand-off is recorded, with no mechanism to wait
for and return the peer's actual answer in the same request. `AgentTool`
instead runs the wrapped agent to completion via its own `Runner` and
returns its result as a normal function-call return value, which is what a
single HTTP request/response round trip through this coordinator actually
needs. The trade-off: no streaming and no multi-turn `input_required`
hand-back -- both would need the peer to implement `RemoteA2aAgent`'s
`mode="task"` handshake, which requires real task-lifecycle support
(`finish_task`, intermediate status updates) on the OLS side that does not
exist today.

## Project layout

```text
agents/acme_agent/
├── src/acme_agent/
│   ├── agent.py       # Local ADK router, remote sub-agents, and A2A/HTTP app
│   ├── auth.py        # Keycloak token exchange and ZTO identity handling
│   ├── config.py      # Environment configuration helpers
│   └── ui.py          # Small browser client
├── Containerfile
├── pyproject.toml
└── tests/
```

The Helm chart is at [`charts/all/acme-agent`](../../charts/all/acme-agent),
alongside every other component in this pattern.

## Local setup

Python 3.11 or newer is recommended.

```bash
cd agents/acme_agent
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Set the downstream A2A agent(s) via `REMOTE_A2A_AGENTS_JSON`, a JSON list of
`{name, description, endpoint}` objects -- there is no single-agent shorthand,
a one-item list is the single-agent case. Each `endpoint` can be either the
agent's base URL or its agent-card URL; when a base URL is supplied, the
agent appends ADK's well-known agent-card path automatically.

The local coordinator's model always goes through LiteLLM, never Google's
Gemini API directly:

```bash
export REMOTE_A2A_AGENTS_JSON='[{"name":"downstream_agent","description":"The configured downstream A2A agent.","endpoint":"http://localhost:8001"}]'
export A2A_AUTH_MODE=none
export ADK_MODEL='openai/acme-agent'
export LITELLM_API_BASE='http://localhost:4000'
export LITELLM_API_KEY='replace-me'
uvicorn acme_agent.agent:a2a_app --host 0.0.0.0 --port 8080
```

For multiple downstream agents, add more entries to the same list:

```bash
export REMOTE_A2A_AGENTS_JSON='[
  {"name":"inventory_agent","description":"Answers inventory questions","endpoint":"http://localhost:8001"},
  {"name":"orders_agent","description":"Answers order questions","endpoint":"http://localhost:8002"}
]'
```

Open <http://localhost:8080/ui> for the UI. The local service also publishes:

| URL | Purpose |
| --- | --- |
| `/ui` | Browser chat UI |
| `/healthz` | Kubernetes/OpenShift health endpoint |
| `/` | A2A JSON-RPC endpoint (POST) and UI redirect (GET) |
| `/.well-known/agent.json` or `/.well-known/agent-card.json` | A2A agent card, depending on the installed A2A SDK version |

The A2A endpoint can be called directly, for example:

```bash
curl -sS http://localhost:8080/ \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": "local-test-1",
    "method": "message/send",
    "params": {
      "message": {
        "messageId": "local-test-1",
        "role": "user",
        "parts": [{"kind": "text", "text": "Hello from ACME"}]
      }
    }
  }'
```

Run the tests with:

```bash
python -m pytest
```

## Authentication for OpenShift

The application supports three downstream authentication modes:

* `none`: send no authentication header; useful only for local development.
* `static`: read `A2A_BEARER_TOKEN` from the environment.
* `keycloak`: obtain and cache a JWT from `KEYCLOAK_TOKEN_URL` using the
  client-credentials grant.

For the OpenShift deployment, configure `keycloak` and set
`identity.spiffe.enabled=true` (chart value) so the agent fetches its own
short-lived JWT-SVID directly from ZTWIM/SPIRE through the Workload API
(`csi.spiffe.io`) instead of reading a static token file. This is the "ZTO" identity used as the JWT
client assertion (`KEYCLOAK_CLIENT_ASSERTION_TYPE`, default
`urn:ietf:params:oauth:client-assertion-type:jwt-spiffe`) when calling
Keycloak with `KEYCLOAK_CLIENT_AUTH_METHOD=client_assertion_post`. This
requires a matching `ClusterSPIFFEID` (`identity.clusterSpiffeID.enabled=true`
with the cluster's trust domain) and a Keycloak client configured for
federated client authentication against the SPIFFE identity provider (see
`charts/all/keycloak-oidc`).

A static Secret or plain projected ServiceAccount token
(`ztoIdentity.enabled=true`) remains available as a fallback for a "ZTO" that
is a separate system from ZTWIM/SPIRE; it is mutually exclusive with
`identity.spiffe.enabled`. A normal Keycloak client-secret method can also be
selected when appropriate for the target realm.

The following settings are supported:

| Environment variable | Description |
| --- | --- |
| `REMOTE_A2A_AGENTS_JSON` | Required JSON list of downstream `{name, description, endpoint}` objects (a single downstream agent is still a one-item list) |
| `ADK_MODEL` | LiteLLM model string used by the local coordinator to select a sub-agent, e.g. `openai/acme-agent` |
| `LITELLM_API_BASE` | LiteLLM proxy base URL |
| `LITELLM_API_KEY` | LiteLLM proxy API key |
| `A2A_AUTH_MODE` | `none`, `static`, or `keycloak` |
| `KEYCLOAK_TOKEN_URL` | Keycloak token endpoint (e.g. `https://keycloak.example/realms/rca/protocol/openid-connect/token`) |
| `KEYCLOAK_CLIENT_ID` | Keycloak client ID |
| `KEYCLOAK_CLIENT_SECRET` | Optional secret for `client_secret_*` methods |
| `KEYCLOAK_SCOPE` | Optional space-separated OAuth scopes |
| `KEYCLOAK_CLIENT_AUTH_METHOD` | `client_assertion_post`, `client_secret_basic`, or `client_secret_post` |
| `KEYCLOAK_CLIENT_ASSERTION_TYPE` | OAuth client assertion type; defaults to the SPIFFE JWT-SVID type |
| `SPIFFE_ENABLED` | When `true`, fetch the client assertion fresh from ZTWIM/SPIRE instead of `ZTO_IDENTITY_TOKEN_FILE` |
| `SPIFFE_ENDPOINT_SOCKET` | SPIRE Workload API socket, e.g. `unix:///spiffe-workload-api/spire-agent.sock` (the SPIFFE CSI driver always names the file `spire-agent.sock`) |
| `SPIFFE_JWT_AUDIENCE` | JWT-SVID audience requested from SPIRE. Must be the Keycloak realm issuer URL (e.g. `https://keycloak.example/realms/rca`), not this workload's own name -- Keycloak's federated-jwt client validator checks the client_assertion's `aud` against the realm issuer by default |
| `ZTO_IDENTITY_TOKEN_FILE` | File containing the ZTO-issued identity token (ignored when `SPIFFE_ENABLED=true`) |
| `ZTO_IDENTITY_TOKEN` | Optional environment fallback for the identity token |
| `ZTO_FORWARD_IDENTITY` | Also forward the identity in `ZTO_IDENTITY_HEADER` |
| `A2A_TLS_VERIFY` | Set to `false` only for local development with test certificates |
| `A2A_CA_BUNDLE` | Optional path to a PEM CA bundle used to verify downstream A2A and Keycloak HTTPS connections |

For OpenShift routes signed by the cluster's managed ingress CA, the Helm
chart can sync that CA into the agent namespace (`auth.caBundleSync.enabled`),
mount it, and pass its path through `A2A_CA_BUNDLE`. A periodic sync refreshes
the bundle and rolls the Deployment when it changes, so the long-lived HTTPX
client reloads its TLS context. Keep `auth.tlsVerify=true`; disabling TLS
verification also affects the Keycloak token client.

`A2A_CA_BUNDLE` is added to the system's default trust store, not used in
place of it (`auth.py`'s `_tls_context_trusting`) -- confirmed necessary on a
live cluster whose ingress certificate chains to a public CA (ZeroSSL):
the synced managed-ingress-CA bundle only contains the leaf's intermediate
certificates, not the actual trusted root, so verifying against that bundle
alone (what `httpx`'s `verify=<path>` does by default) fails with
`CERTIFICATE_VERIFY_FAILED: unable to get issuer certificate` even though the
certificate itself is perfectly valid.

The exact Keycloak client policy is deployment-specific, so the chart exposes
the token URL, client-auth method, and SPIFFE/ZTO settings as values rather
than assuming a fixed installation.

## Build the image

The Helm chart deploys a pre-built image; it does not build an image in the
cluster. Choose a registry reachable by OpenShift, log in to it, then use the
same repository and tag for both `podman push` and Helm:

```bash
export IMAGE_REPOSITORY=quay.io/your-org/acme-agent
export IMAGE_TAG=0.1.0

podman login quay.io
podman build \
  --file Containerfile \
  --tag "${IMAGE_REPOSITORY}:${IMAGE_TAG}" \
  .
podman push "${IMAGE_REPOSITORY}:${IMAGE_TAG}"
```

For a local-only test, omit `podman push` and use a registry available to the
cluster, or load the image into the cluster’s development registry. The build
uses the UBI Python base image and installs the dependencies from
`pyproject.toml`.

## Deploy with Helm

The chart lives at `charts/all/acme-agent` and is wired into
`variants/standalone/values-standalone.yaml` as an Argo CD application. To
deploy it standalone (outside the pattern) instead:

```bash
helm upgrade --install acme-agent ../../charts/all/acme-agent \
  --namespace acme-agent --create-namespace \
  --set image.repository="${IMAGE_REPOSITORY}" \
  --set image.tag="${IMAGE_TAG}" \
  --set a2a.remoteAgents[0].name=downstream_agent \
  --set a2a.remoteAgents[0].description="The configured downstream A2A agent." \
  --set a2a.remoteAgents[0].endpoint=https://downstream.example.com/a2a \
  --set a2a.publicHost=acme-agent-acme-agent.apps.example.com
```

The important check is that the rendered Deployment contains the same image
reference that was pushed:

```bash
helm get manifest acme-agent -n acme-agent \
  | grep -A1 'image:'
oc get deployment acme-agent -n acme-agent \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

For more than one downstream agent, put the routing configuration in a values
file:

```yaml
a2a:
  remoteAgents:
    - name: inventory_agent
      description: Answers inventory questions
      endpoint: https://inventory.example.com/a2a
    - name: orders_agent
      description: Answers order questions
      endpoint: https://orders.example.com/a2a
```

For a cluster deployment authenticating with a static client secret, put it
in a Kubernetes Secret and configure `auth.keycloak.clientSecretSecret`. Do
not put the secret value in Git or in Helm values committed to the
repository. Prefer `identity.spiffe.enabled=true` (see above) so no secret is
needed at all. This repo wires the application into
`variants/standalone/values-standalone.yaml` as:

```yaml
acme-agent:
  name: acme-agent
  namespace: acme-agent
  argoProject: hub
  path: charts/all/acme-agent
  overrides:
    - name: image.repository
      value: quay.io/<your-quay-org>/acme-agent
    - name: image.tag
      value: <immutable-tag>
    # The Praxis gateway's edge-TLS Route in front of OpenShift Lightspeed,
    # not its in-cluster Service DNS name -- google-adk's RemoteA2aAgent
    # requires https (or loopback) for any agent card it fetches. See
    # agents/AUTHENTICATION.md. A single downstream agent is still a
    # one-item a2a.remoteAgents list.
    - name: a2a.remoteAgents[0].name
      value: openshift_lightspeed
    - name: a2a.remoteAgents[0].description
      value: Answers OpenShift questions and investigates cluster incidents using OpenShift Lightspeed grounded against the live cluster via its OpenShift MCP tools.
    - name: a2a.remoteAgents[0].endpoint
      value: https://<praxis-route>
    - name: auth.mode
      value: keycloak
    - name: auth.keycloak.issuerUrl
      value: https://<keycloak-route>/realms/rca
    - name: auth.keycloak.tokenUrl
      value: https://<keycloak-route>/realms/rca/protocol/openid-connect/token
    - name: auth.keycloak.clientId
      value: acme-agent
    - name: identity.spiffe.enabled
      value: "true"
    - name: identity.clusterSpiffeID.enabled
      value: "true"
    - name: identity.clusterSpiffeID.trustDomain
      value: <cluster trust domain>
```

The chart creates an `ExternalSecret` for `litellm.vaultKey` (default
`secret/data/global/acme-agent-litellm`).
To use a pre-existing Secret instead, set `litellm.existingSecret.name` and
leave `litellm.vaultKey` empty. No raw API key value is accepted by the
chart.

See `charts/all/acme-agent/values.yaml` for all deployment options, including
route settings, resource limits, ZTO/SPIFFE identity, and Keycloak settings.
