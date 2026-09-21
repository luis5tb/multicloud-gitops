# RCA Agent

`rca_agent` is a Google Agent Development Kit (ADK) agent exposed through the
Agent2Agent (A2A) protocol. It is a deliberately narrow wrapper around the
OpenShift Lightspeed Agentic operator:

1. It receives a natural-language troubleshooting request.
2. It creates an analysis-only `AgenticRun` custom resource.
3. It waits for the operator's `Analyzed` condition.
4. It reads the referenced `AnalysisResult` and returns its diagnosis and all
   remediation proposals in the A2A response.

It never creates approvals, and it never requests or performs execution or
verification. The implementation uses the Kubernetes API directly; an
OpenShift MCP server can be introduced later without changing the A2A contract.

Every A2A call is authenticated in two ways. The pod obtains a short-lived
JWT-SVID from the ZTWIM/SPIRE Workload API, and the caller's bearer token is
validated against the configured Keycloak issuer and JWKS. The public agent
card endpoint is left unauthenticated so A2A discovery can work.

## Prerequisites

- Python 3.12+
- An OpenShift cluster with the `lightspeed-agentic-operator` and its
  `agentic.openshift.io/v1alpha1` CRDs installed
- An analysis `Agent` configured in the operator (the default is `default`)
- ZTWIM/SPIRE configured with a `ClusterSPIFFEID` that registers this
  Deployment's ServiceAccount and allows the `rca-agent` JWT audience
- A Keycloak realm configured to trust the SPIRE OIDC discovery provider and a
  client/audience for this agent
- Permission for the runtime identity to create/read `AgenticRun` and read
  `AnalysisResult` resources in the configured run namespace
- Either a Google AI Studio key (`GOOGLE_API_KEY`) or Vertex AI credentials

## Local setup

From the repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e 'agents/rca_agent[dev]'

export KUBECONFIG="$PWD/.kubeconfig"
export GOOGLE_API_KEY='replace-me'
export AGENTIC_RUN_NAMESPACE='openshift-lightspeed'
export AGENTIC_RUN_ANALYSIS_AGENT='default'
export KEYCLOAK_ISSUER_URL='https://keycloak.example/realms/example'
export KEYCLOAK_AUDIENCE='rca-agent'
export SPIFFE_ENDPOINT_SOCKET='unix:///tmp/spire-agent/public/api.sock'
export SPIFFE_JWT_AUDIENCE='rca-agent'

uvicorn rca_agent.main:a2a_app --app-dir agents/rca_agent --host 0.0.0.0 --port 8000
```

For Vertex AI, set `GOOGLE_GENAI_USE_VERTEXAI=true`,
`GOOGLE_CLOUD_PROJECT`, and `GOOGLE_CLOUD_LOCATION` instead of
`GOOGLE_API_KEY`.

The public A2A agent card is available at:

```text
http://localhost:8000/.well-known/agent-card.json
```

The model used by ADK can be changed with `ADK_MODEL`; it defaults to
`gemini-2.5-flash`. Polling is controlled by `AGENTIC_RUN_TIMEOUT_SECONDS`
(default `900`) and `AGENTIC_RUN_POLL_INTERVAL_SECONDS` (default `5`).

## Example A2A request

The exact JSON-RPC envelope is defined by the installed A2A SDK. A typical
`message/send` request is:

```bash
curl -sS http://localhost:8000/ \
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

Requests without a valid Keycloak bearer token, or when ZTWIM cannot issue the
pod a JWT-SVID, receive HTTP 401 before ADK handles the message.

The deployment readiness endpoint (`/health/ready`) checks both Keycloak OIDC
discovery and the SPIFFE Workload API. The liveness endpoint remains the public
agent card so a temporary identity-provider outage does not restart the pod.

The response contains the agent's text plus the run name, current status,
analysis result, and the list of proposal options returned by the operator.
For an analysis that has not completed before the timeout, the response
contains the run reference and current status so it can be inspected with
`oc -n <namespace> get agenticrun <name> -o yaml`.

## Tests

```bash
pytest -q agents/rca_agent/tests
```

The tests do not require a cluster; they verify the most important safety
property, namely that the generated CR never includes execution or verification
steps.

## Container image

```bash
podman build -f agents/rca_agent/Containerfile \
  -t quay.io/<organization>/rca-agent:latest agents/rca_agent
podman push quay.io/<organization>/rca-agent:latest
```

## Helm deployment

The chart is at [`charts/all/rca-agent`](../../charts/all/rca-agent). Install it
after pushing the image:

```bash
helm upgrade --install rca-agent charts/all/rca-agent \
  --namespace openshift-lightspeed --create-namespace \
  --set image.repository=quay.io/<organization>/rca-agent \
  --set image.tag=latest \
  --set google.apiKey.value='replace-me'
```

For production, use an existing Secret instead of putting the key on the Helm
command line:

```bash
helm upgrade --install rca-agent charts/all/rca-agent \
  --namespace openshift-lightspeed \
  --set image.repository=quay.io/<organization>/rca-agent \
  --set google.apiKey.existingSecret=my-google-api-key \
  --set google.apiKey.existingSecretKey=GOOGLE_API_KEY
```

The chart creates a namespaced ServiceAccount, Role, RoleBinding, Deployment,
and Service. Its Role grants only `create/get` on `agenticruns` and `get` on
`analysisresults`, plus read access needed for polling. The chart does not
install the operator or its CRDs.

The chart mounts the ZTWIM/SPIRE Workload API through the `csi.spiffe.io` CSI
driver. Configure the Keycloak and SPIFFE settings when installing:

```bash
helm upgrade --install rca-agent charts/all/rca-agent \
  --namespace openshift-lightspeed --create-namespace \
  --set image.repository=quay.io/<organization>/rca-agent \
  --set image.tag=<tag> \
  --set identity.keycloak.issuerUrl='https://keycloak.keycloak-system.svc.cluster.local/realms/<realm>' \
  --set identity.keycloak.audience=rca-agent \
  --set identity.workloadApiAudience=rca-agent
```

For an internal Keycloak CA, use a Secret or ConfigMap rather than disabling
TLS verification:

```bash
helm upgrade --install rca-agent charts/all/rca-agent \
  --namespace openshift-lightspeed --create-namespace \
  --set identity.keycloak.issuerUrl='https://keycloak.keycloak-system.svc.cluster.local/realms/<realm>' \
  --set identity.keycloak.caBundleSecret.name=keycloak-ca \
  --set identity.keycloak.caBundleSecret.key=ca.crt
```

The chart can create the matching `ClusterSPIFFEID` when the trust domain is
known. This is the registration policy that allows ZTWIM/SPIRE to issue the
identity automatically; it does not create a static credential:

```bash
helm upgrade --install rca-agent charts/all/rca-agent \
  --namespace openshift-lightspeed --create-namespace \
  --set identity.clusterSpiffeID.enabled=true \
  --set identity.clusterSpiffeID.trustDomain=<spire-trust-domain> \
  --set identity.keycloak.issuerUrl='https://keycloak.keycloak-system.svc.cluster.local/realms/<realm>'
```

If the validated pattern already manages a matching `ClusterSPIFFEID`, leave
`identity.clusterSpiffeID.enabled=false` and reuse that policy. The chart still
mounts the CSI Workload API socket and the agent will fail readiness until
ZTWIM can issue the configured audience.
