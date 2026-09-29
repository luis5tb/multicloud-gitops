# lightspeed-agentic-operator

Deploys [openshift/lightspeed-agentic-operator](https://github.com/openshift/lightspeed-agentic-operator).
This is a dev-preview, non-OLM operator: no CSV/channel/catalog source, just
CRDs + RBAC + a Deployment with a floating Konflux `:main` image. This chart
mirrors `hack/quickstart/deploy-operator.sh` and `deploy-configmap.sh` from
upstream commit `116d2048f96722463d619cf05481d713749139d6`, with CRDs and the
AgenticRun suspension `ValidatingAdmissionPolicy` vendored as-is under `crds/`
and `templates/agenticrun-suspension-*.yaml`.

The namespace is `lightspeed-agentic-operator`, not the upstream default
`openshift-lightspeed` -- that name is already used by the separate classic
OpenShift Lightspeed (OLS) operator.

## Testing the operator is up

```bash
oc get pods -n lightspeed-agentic-operator
oc get crd | grep agentic.openshift.io
oc get validatingadmissionpolicy,validatingadmissionpolicybinding | grep agentic
oc get mutatingwebhookconfiguration agentic-operator-mutating-webhook
oc get approvalpolicy cluster -o yaml
oc get configmap lightspeed-agentic-configuration -n lightspeed-agentic-operator
```

## Enabling an LLMProvider + Agent

Providers and Agents are configured independently under `llmProviders` and
`agents`, so multiple providers can be installed together. Each enabled
provider creates an `LLMProvider`; OpenAI gets its own ESO-managed credentials
Secret, while Vertex Anthropic and Vertex Google share the GCP credentials
Secret. Each Agent selects one provider and its model name.
Credentials always come from Vault via an `ExternalSecret` -- never commit
them to Git. Vertex `projectID`/`region` are ordinary values, not credentials.

### Vertex AI: Anthropic and Gemini

Vertex Anthropic and Vertex Google (Gemini) can be enabled independently:

```yaml
overrides:
  - name: llmProviders.vertexAnthropic.enabled
    value: "true"
  - name: llmProviders.vertexAnthropic.projectID
    value: my-project-id   # replace with your real GCP project ID
  - name: llmProviders.vertexAnthropic.region
    value: global          # replace with your real Vertex AI region
  - name: agents.vertex.llmProvider
    value: vertex-anthropic
  - name: agents.vertex.model
    value: claude-opus-4-6
  - name: llmProviders.vertexGoogle.enabled
    value: "true"
  - name: llmProviders.vertexGoogle.name
    value: vertex-google
  - name: llmProviders.vertexGoogle.projectID
    value: my-project-id
  - name: llmProviders.vertexGoogle.region
    value: global
  - name: agents.gemini.llmProvider
    value: vertex-google
  - name: agents.gemini.model
    value: gemini-3.8-flash
```

Replace `projectID`/`region` with your real values before syncing. Matches
upstream's
[`examples/vertex-anthropic.yaml`](https://github.com/openshift/lightspeed-agentic-operator/blob/main/hack/quickstart/examples/vertex-anthropic.yaml)
and the `GOOGLE_APPLICATION_CREDENTIALS` secret key documented in
`hack/quickstart/install.sh`:

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/your/service-account-key.json
oc create secret generic llm-creds-vertex -n lightspeed-agentic-operator \
  --from-file=GOOGLE_APPLICATION_CREDENTIALS="$GOOGLE_APPLICATION_CREDENTIALS"
```

This chart creates that same secret declaratively instead, via Vault + ESO:

1. In `values-secret.yaml.template`, uncomment the `llm-creds-vertex` entry
   and point `path` at your service-account key / Application Default
   Credentials JSON file.
2. Copy the template to wherever `load-secrets` reads it from and run
   `./pattern.sh make load-secrets`.

Both Vertex providers reference that same GCP credentials Secret. Select a
Gemini model available to the configured project and region; the standalone
variant uses `gemini-3.8-flash` as an example.

### OpenAI-compatible services and LiteLLM

An OpenAI-compatible provider can point at OpenAI itself or a proxy such as
LiteLLM. Set `url` to the proxy's OpenAI-compatible API base and set `vaultKey`
to a Vault secret whose `api-key` property is accepted by that proxy:

```yaml
overrides:
  - name: llmProviders.openai.enabled
    value: "true"
  - name: llmProviders.openai.name
    value: litellm-gpt-oss
  - name: llmProviders.openai.url
    value: http://litellm.litellm.svc.cluster.local:4000/v1
  - name: llmProviders.openai.vaultKey
    value: secret/data/global/llm-creds-openai
  - name: agents.default.llmProvider
    value: litellm-gpt-oss
  - name: agents.default.model
    value: gpt-oss-20b
```

The chart maps the Vault `api-key` property into an operator-namespace Secret
with the required `OPENAI_API_KEY` key. For LiteLLM, the Agent's `model` must
be the model name exposed by LiteLLM. The standalone pattern uses
`gpt-oss-20b`, matching RCA's `litellm.model`, for the `default` Agent so
existing RCA-created runs continue to work. It also creates a `vertex` Agent
for Anthropic-on-Vertex and a `gemini` Agent for Google-on-Vertex.

To select a provider for an individual run, set the Agent name on that
AgenticRun stage: `analysis.agent: default` selects LiteLLM,
`analysis.agent: vertex` selects Anthropic on Vertex, and
`analysis.agent: gemini` selects Gemini on Vertex. RCA-created runs take this
name from `agenticRun.analysisAgent` (default `default`). To send all new
RCA-created runs to Gemini, set this override on the `rca-agent` application:

```yaml
- name: agenticRun.analysisAgent
  value: gemini
```

Sync the application after changing the override. Set it back to `default` to
route new RCA-created runs through LiteLLM again, or use `vertex` for
Anthropic-on-Vertex.

For direct OpenAI API usage, leave `url` empty, set `vaultKey` to the Vault
entry containing the OpenAI API key, and set the Agent's model to one available
to that key.

The OpenAI ExternalSecret defaults to reading
`secret/data/global/llm-creds-openai`. This gives the Lightspeed operator an
independent key from RCA. To deliberately share RCA's LiteLLM key instead,
override `llmProviders.openai.vaultKey` with
`secret/data/global/rca-agent-litellm`; ESO still materializes a separate
operator-namespace Secret with the key `OPENAI_API_KEY`.
For Vertex credentials used by either provider, uncomment `llm-creds-vertex` in
`values-secret.yaml.template`, point `path` at a GCP Application Default
Credentials JSON file, then run `./pattern.sh make load-secrets`.

## Verifying and running an agent

```bash
oc get llmprovider -n lightspeed-agentic-operator -o yaml
oc get agent -n lightspeed-agentic-operator -o yaml

oc-agentic run create --request="Fix crashloop in my-app namespace" \
  --target-namespaces=my-app -n lightspeed-agentic-operator
oc-agentic run list -n lightspeed-agentic-operator
```

`oc-agentic` is a separate CLI plugin that runs on your machine, not the
cluster -- this chart doesn't install it. Grab it from the
[upstream releases](https://github.com/openshift/lightspeed-agentic-operator#install):

```bash
# Linux amd64
curl -L https://github.com/openshift/lightspeed-agentic-operator/releases/latest/download/oc-agentic_linux_amd64.tar.gz | tar xz
sudo mv oc-agentic /usr/local/bin/

# macOS Apple Silicon
curl -L https://github.com/openshift/lightspeed-agentic-operator/releases/latest/download/oc-agentic_darwin_arm64.tar.gz | tar xz
sudo mv oc-agentic /usr/local/bin/
```

Once it's on `$PATH`, `oc` auto-discovers it as `oc agentic <subcommand>` (it
also works standalone as `oc-agentic`). Default namespace is
`openshift-lightspeed` unless overridden with `-n` -- since this chart uses
`lightspeed-agentic-operator` instead, pass `-n lightspeed-agentic-operator`
on every command (as in the examples above and below).

## How to run a test AgenticRun

### Break the cluster

Deploy a deliberately broken workload from
[rhobs/troubleshooting-scenarios](https://github.com/rhobs/troubleshooting-scenarios)
so there's something real for the agent to investigate:

```bash
git clone https://github.com/rhobs/troubleshooting-scenarios.git
cd troubleshooting-scenarios/generic/01-payments-api-failure
make deploy SINGLE_NAMESPACE=1  # =1 until OLS-3463 is fixed
make break
```

### Submit a proposal

```bash
oc delete agenticrun test-run -n lightspeed-agentic-operator --ignore-not-found
oc apply -f - << EOF
apiVersion: agentic.openshift.io/v1alpha1
kind: AgenticRun
metadata:
  name: test-run
  namespace: lightspeed-agentic-operator
spec:
  request: |
    Alert: PaymentErrorRateHigh (critical)
    Namespace: payments
    Description: Payment error rate is 100.00%, which exceeds the 15% threshold.
    Labels:
      alertname: PaymentErrorRateHigh
      namespace: payments
      severity: critical

    Investigate using the skill at /app/skills/cluster-troubleshoot/investigate-alert
  targetNamespaces:
  - payments
  tools:
     skills:
       - image: quay.io/openshiftanalytics/agentic-skills:latest
         paths:
           - /skills/cluster-troubleshoot/investigate-alert
  analysis:
    agent: default
  execution:
    agent: default
  verification:
    agent: default
EOF
```

`agent: default` selects the OpenAI-compatible LiteLLM provider in the
standalone pattern. Use
`agent: vertex` for a run stage to select Vertex instead. Then watch/approve it:

```bash
oc-agentic run watch test-run -n lightspeed-agentic-operator
oc-agentic run approve test-run --stage=execution --option=0 -n lightspeed-agentic-operator
```

## Out of scope

The OTEL collector, Alertmanager alerts adapter, and OpenShift console plugin
from `hack/quickstart/` are not deployed here -- only the core operator plus
the configured `LLMProvider` and `Agent` resources described above.
