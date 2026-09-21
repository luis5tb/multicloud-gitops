# RCA Agent

`rca_agent` is a Google Agent Development Kit (ADK) agent exposed through
Agent2Agent (A2A). It validates the caller's Keycloak bearer token and its own
ZTWIM/SPIRE workload identity, creates an analysis-only `AgenticRun` through
the upstream `openshift/openshift-mcp-server`, polls it, and returns the
diagnosis plus remediation proposals from the `AnalysisResult`.

It never creates an approval, execution, or verification step. The caller
token is kept in memory for the request only and is forwarded in the MCP HTTP
`Authorization` header. It is not placed in the AgenticRun CR and is not given
to the asynchronous analysis sandbox.

## Prerequisites

- Python 3.12+ for local development.
- OpenShift with the Lightspeed Agentic operator and its CRDs installed.
- An analysis `Agent` configured in the operator (the default is `default`).
- The upstream OpenShift MCP server configured with
  `resources_create_or_update` and `resources_get`.
- Keycloak configured to emit `groups` and an `openshift` audience in access
  tokens, plus OpenShift Native OIDC and a RoleBinding for the RCA group.
- ZTWIM/SPIRE configured with a `ClusterSPIFFEID` for this Deployment's
  ServiceAccount and the `rca-agent` JWT audience.
- A LiteLLM-compatible endpoint and an API key delivered through a Secret.

## Local setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e 'agents/rca_agent[dev]'

export ADK_MODEL='openai/rca-agent'
export LITELLM_API_BASE='http://localhost:4000'
export LITELLM_API_KEY='replace-me'
export OPENSHIFT_MCP_URL='http://localhost:8080/mcp'
export OPENSHIFT_MCP_CREATE_TOOL='resources_create_or_update'
export OPENSHIFT_MCP_GET_TOOL='resources_get'
export AGENTIC_RUN_NAMESPACE='openshift-lightspeed'
export AGENTIC_RUN_ANALYSIS_AGENT='default'
export KEYCLOAK_ISSUER_URL='https://keycloak.example/realms/rca'
export KEYCLOAK_AUDIENCES='rca-agent,openshift'
export SPIFFE_ENDPOINT_SOCKET='unix:///tmp/spire-agent/public/api.sock'
export SPIFFE_JWT_AUDIENCE='rca-agent'

uvicorn rca_agent.main:a2a_app --app-dir agents/rca_agent --host 0.0.0.0 --port 8000
```

ADK uses LiteLLM for the model call. Configure the provider, upstream model,
and provider credentials in the LiteLLM proxy; do not put provider keys in this
repository or in Helm values.

The public A2A agent card is available at
`http://localhost:8000/.well-known/agent-card.json`.

## Example A2A request

```bash
curl -sS http://localhost:8000/ \
  -H "authorization: Bearer ${KEYCLOAK_ACCESS_TOKEN}" \
  -H 'content-type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
      "message": {
        "messageId": "rca-1",
        "role": "user",
        "parts": [{"kind": "text", "text": "Investigate why pod api-0 is crash looping in namespace payments."}]
      }
    }
  }'
```

The response contains the run reference, diagnosis, and proposal options. A
timeout returns the latest run status so it can be inspected with MCP or `oc`.

## Helm charts

The RCA chart is at [`charts/all/rca-agent`](../../charts/all/rca-agent). The
companion [`charts/all/openshift-mcp-server`](../../charts/all/openshift-mcp-server)
deploys the upstream OpenShift MCP image in passthrough mode and allowlists
only the two generic resource tools required by this agent. The
[`charts/all/keycloak-oidc`](../../charts/all/keycloak-oidc) chart imports the
Keycloak realm/client/group and creates the OpenShift RoleBinding; Native OIDC
is explicitly opt-in because `Authentication/cluster` is cluster-wide.

Install the MCP server first:

```bash
helm upgrade --install openshift-mcp-server charts/all/openshift-mcp-server \
  --namespace openshift-mcp-server --create-namespace \
  --set image.repository=quay.io/containers/kubernetes_mcp_server \
  --set image.tag=<approved-immutable-tag>
```

Import the Keycloak configuration and AgenticRun RBAC:

```bash
helm upgrade --install keycloak-oidc charts/all/keycloak-oidc \
  --namespace keycloak-system \
  --set keycloak.name=keycloak \
  --set keycloak.realm=rca \
  --set agenticRun.namespace=lightspeed-agentic-operator
```

If enabling Native OIDC, set a stable Keycloak route reachable by the
OpenShift control plane. It must exactly match the JWT `iss` claim:

```bash
helm upgrade --install keycloak-oidc charts/all/keycloak-oidc \
  --namespace keycloak-system \
  --set openshiftOIDC.enabled=true \
  --set openshiftOIDC.issuerURL='https://keycloak.apps.example.com/realms/rca'
```

Deploy the agent using a Vault-backed LiteLLM key:

```bash
helm upgrade --install rca-agent charts/all/rca-agent \
  --namespace lightspeed-agentic-operator --create-namespace \
  --set image.repository=quay.io/<organization>/rca-agent \
  --set image.tag=<immutable-tag> \
  --set identity.keycloak.issuerUrl='https://keycloak.apps.example.com/realms/rca' \
  --set identity.keycloak.audiences[0]=rca-agent \
  --set identity.keycloak.audiences[1]=openshift \
  --set litellm.vaultKey='secret/data/global/rca-agent-litellm'
```

The chart creates an `ExternalSecret` with the `LITELLM_API_KEY` key. To use a
pre-existing Secret instead, set `litellm.existingSecret.name` and leave
`litellm.vaultKey` empty. No raw API key value is accepted by the chart.

The RCA ServiceAccount has no AgenticRun Kubernetes Role. OpenShift MCP uses
the caller token, and the `keycloak:rca-users` group receives only `create/get`
on AgenticRuns and `get` on AnalysisResults in the AgenticRun namespace.

## Build and push the RCA image to Quay

Use an immutable tag so the Helm release identifies exactly what was tested:

```bash
export QUAY_ORG=<organization>
export IMAGE_TAG=$(git rev-parse --short HEAD)
export IMAGE="quay.io/${QUAY_ORG}/rca-agent:${IMAGE_TAG}"

podman login quay.io
podman build --file agents/rca_agent/Containerfile --tag "${IMAGE}" agents/rca_agent
podman push "${IMAGE}"
skopeo inspect "docker://${IMAGE}"
```

For a private repository, create a Quay robot credential and reference it as
the image pull Secret in `image.pullSecrets`. Avoid `latest` in production.

## Tests

```bash
pytest -q agents/rca_agent/tests
helm lint charts/all/rca-agent
helm lint charts/all/openshift-mcp-server
helm lint charts/all/keycloak-oidc
```

The unit test verifies that the generated AgenticRun contains no execution,
verification, or MCP-token fields.
