# Ericsson A2A agent

This is the first agent under `agents/`. It is a small Google Agent Development
Kit (ADK) agent that exposes its own A2A endpoint and routes each request to one
of several configurable downstream A2A sub-agents. It also serves a browser UI
from the same HTTP service.

The exposed `ericsson_agent` is a local ADK coordinator agent. Its configured
`RemoteA2aAgent` children are downstream peers, not the public entrypoint. The
coordinator selects a peer using its description, passes the request to that
peer, and returns the peer's result without doing domain-specific work itself.

## Project layout

```text
agents/ericsson_agent/
├── src/ericsson_agent/
│   ├── agent.py       # Local ADK router, remote sub-agents, and A2A/HTTP app
│   ├── auth.py        # Keycloak token exchange and ZTO identity handling
│   ├── config.py      # Environment configuration helpers
│   └── ui.py          # Small browser client
├── chart/ericsson-agent/  # Self-contained Helm chart
├── Containerfile
├── pyproject.toml
└── tests/
```

## Local setup

Python 3.11 or newer is recommended.

```bash
cd agents/ericsson_agent
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Set the endpoint of the downstream A2A service. The value can be either its
base URL or its agent-card URL. When a base URL is supplied, the agent appends
ADK's well-known agent-card path automatically.

```bash
export DOWNSTREAM_A2A_ENDPOINT=http://localhost:8001
export A2A_AUTH_MODE=none
export GOOGLE_API_KEY=your-google-api-key
uvicorn ericsson_agent.agent:a2a_app --host 0.0.0.0 --port 8080
```

For multiple downstream agents, use `REMOTE_A2A_AGENTS_JSON` instead of the
single-agent shorthand:

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
        "parts": [{"kind": "text", "text": "Hello from Ericsson"}]
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

For the OpenShift deployment, configure `keycloak` and let ZTO provide the
workload identity token at `ZTO_IDENTITY_TOKEN_FILE`. The chart supports both a
Secret-backed file and a projected service-account token volume. The identity
token can be submitted to Keycloak as a JWT client assertion using
`KEYCLOAK_CLIENT_AUTH_METHOD=client_assertion_post`. For a SPIFFE/ZTWIM
JWT-SVID, the default assertion type is
`urn:ietf:params:oauth:client-assertion-type:jwt-spiffe`; set
`KEYCLOAK_CLIENT_ASSERTION_TYPE` to the JWT-bearer URI only for a realm that
explicitly expects that profile. A normal Keycloak client-secret method can
also be selected when appropriate for the target realm.

The following settings are supported:

| Environment variable | Description |
| --- | --- |
| `DOWNSTREAM_A2A_ENDPOINT` | Single downstream A2A base URL or agent-card URL shorthand |
| `REMOTE_A2A_AGENTS_JSON` | Preferred JSON list of downstream `{name, description, endpoint}` objects |
| `ADK_MODEL` | Model used by the local coordinator to select a sub-agent |
| `GOOGLE_API_KEY` | Google model credential for local development |
| `A2A_AUTH_MODE` | `none`, `static`, or `keycloak` |
| `KEYCLOAK_TOKEN_URL` | Keycloak token endpoint |
| `KEYCLOAK_CLIENT_ID` | Keycloak client ID |
| `KEYCLOAK_CLIENT_SECRET` | Optional secret for `client_secret_*` methods |
| `KEYCLOAK_SCOPE` | Optional space-separated OAuth scopes |
| `KEYCLOAK_CLIENT_AUTH_METHOD` | `client_assertion_post`, `client_secret_basic`, or `client_secret_post` |
| `KEYCLOAK_CLIENT_ASSERTION_TYPE` | OAuth client assertion type; defaults to the SPIFFE JWT-SVID type |
| `ZTO_IDENTITY_TOKEN_FILE` | File containing the ZTO-issued identity token |
| `ZTO_IDENTITY_TOKEN` | Optional environment fallback for the identity token |
| `ZTO_FORWARD_IDENTITY` | Also forward the identity in `ZTO_IDENTITY_HEADER` |
| `A2A_TLS_VERIFY` | Set to `false` only for local development with test certificates |

The exact Keycloak client policy and ZTO audience are deployment-specific, so
the chart exposes the token URL, client-auth method, token-file path, audience,
and service-account annotations as values rather than assuming a fixed ZTO
installation.

## Build the image

The Helm chart deploys a pre-built image; it does not build an image in the
cluster. Choose a registry reachable by OpenShift, log in to it, then use the
same repository and tag for both `podman push` and Helm:

```bash
export IMAGE_REPOSITORY=quay.io/your-org/ericsson-agent
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

The chart is kept with the project so it can be added to the validated pattern
as an Argo CD application later. Deploy it directly with:

```bash
helm upgrade --install ericsson-agent ./chart/ericsson-agent \
  --namespace ericsson-agent --create-namespace \
  --set image.repository="${IMAGE_REPOSITORY}" \
  --set image.tag="${IMAGE_TAG}" \
  --set a2a.downstreamEndpoint=https://downstream.example.com/a2a \
  --set a2a.publicHost=ericsson-agent-ericsson-agent.apps.example.com
```

The important check is that the rendered Deployment contains the same image
reference that was pushed:

```bash
helm get manifest ericsson-agent -n ericsson-agent \
  | grep -A1 'image:'
oc get deployment ericsson-agent -n ericsson-agent \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

For more than one downstream agent, put the routing configuration in a values
file:

```yaml
a2a:
  model: gemini-flash-latest
  remoteAgents:
    - name: inventory_agent
      description: Answers inventory questions
      endpoint: https://inventory.example.com/a2a
    - name: orders_agent
      description: Answers order questions
      endpoint: https://orders.example.com/a2a
```

For a cluster deployment, put the Keycloak client secret in a Kubernetes
Secret and configure `auth.keycloak.clientSecretSecret`. Do not put the secret
value in Git or in Helm values committed to the repository. A typical pattern
application entry will be:

```yaml
ericsson-agent:
  name: ericsson-agent
  namespace: ericsson-agent
  argoProject: hub
  path: agents/ericsson_agent/chart/ericsson-agent
```

See `chart/ericsson-agent/values.yaml` for all deployment options, including
route settings, resource limits, ZTO token volumes, and Keycloak settings.
