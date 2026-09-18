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

`llmProvider.enabled` is `false` by default in this chart, but the
`standalone` variant (`variants/standalone/values-standalone.yaml`) turns it
on with `llmProvider.type: vertexAnthropic` out of the box. Only one provider
is active at a time, picked by `llmProvider.type`: `vertexAnthropic` or
`openai`. Credentials always come from Vault via an `ExternalSecret` -- never
commit them to Git. `projectID`/`region` are plain `overrides` in git, not
routed through Vault: they aren't credentials (comparable to an AWS account
ID) and the `LLMProvider` CRD has no `secretRef` indirection for them anyway
(only `credentialsSecret` supports referencing a Secret).

### Vertex AI (Anthropic / Claude) -- enabled by default

The `standalone` variant already sets:

```yaml
overrides:
  - name: llmProvider.enabled
    value: "true"
  - name: llmProvider.type
    value: vertexAnthropic
  - name: llmProvider.vertexAnthropic.projectID
    value: my-project-id   # replace with your real GCP project ID
  - name: llmProvider.vertexAnthropic.region
    value: global          # replace with your real Vertex AI region
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

### OpenAI

To switch to OpenAI instead, change the `standalone` overrides to:

```yaml
overrides:
  - name: llmProvider.enabled
    value: "true"
  - name: llmProvider.type
    value: openai
  - name: llmProvider.openai.model
    value: "gpt-5.4"   # optional, this is the default
```

Matches upstream's
[`examples/openai.yaml`](https://github.com/openshift/lightspeed-agentic-operator/blob/main/hack/quickstart/examples/openai.yaml)
and its `OPENAI_API_KEY` secret key. Its credentials come from Vault too:

1. In `values-secret.yaml.template`, uncomment the `llm-creds-openai` entry
   (`onMissingValue: prompt` asks for the key interactively during
   `load-secrets` -- it can't be auto-generated).
2. Run `./pattern.sh make load-secrets`.

## Verifying and running an agent

```bash
oc get llmprovider -n lightspeed-agentic-operator -o yaml
oc get agent -n lightspeed-agentic-operator -o yaml

oc-agentic run create --request="Fix crashloop in my-app namespace" \
  --target-namespaces=my-app -n lightspeed-agentic-operator
oc-agentic run list -n lightspeed-agentic-operator
```

`oc-agentic` is a separate CLI plugin -- see the
[install instructions](https://github.com/openshift/lightspeed-agentic-operator#install)
in the upstream README.

## Out of scope

The OTEL collector, Alertmanager alerts adapter, and OpenShift console plugin
from `hack/quickstart/` are not deployed here -- only the core operator plus
the optional single-provider `LLMProvider`/`Agent` pair described above.
