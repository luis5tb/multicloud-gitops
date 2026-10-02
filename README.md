# Multicloud Gitops

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

[Live build status](https://validatedpatterns.io/ci/?pattern=mcgitops)

## Start Here

If you've followed a link to this repository, but are not really sure what it contains
or how to use it, head over to [Multicloud GitOps](https://validatedpatterns.io/patterns/multicloud-gitops/)
for additional context and installation instructions

## Rationale

The goal for this pattern is to:

* Use a GitOps approach to manage hybrid and multi-cloud deployments across both public and private clouds.
* Enable cross-cluster governance and application lifecycle management.
* Securely manage secrets across the deployment.


## Deploy the validated pattern

### Current state and safety gates

The greenfield target is **ACME → Praxis → OpenShift Lightspeed → the
operator-managed OpenShift MCP server**. The former RCA service and its
standalone MCP chart/application have been removed; do not use this branch to
upgrade a cluster that still runs them.

The standalone Pattern contains the application scaffolding, but the new OLS
operator Subscription, OLSConfig, and Praxis gateway are intentionally
**disabled**, and the ACME chart is inert until enabled with a downstream
endpoint. Supply the Praxis route and shared cluster registry before enabling
it. A successful Argo sync or Helm render does not mean the chain is usable.

Before enabling the chain, the deployment owner must provide:

1. A newly versioned, published downstream OLS operator image and bundle/catalog
   with the actual package, channel, starting CSV, and immutable catalog digest.
   The checked-in bundle still identifies as `1.1.4`; do not publish the
   downstream changes under that version.
2. Scanned, published, immutable Lightspeed service and Praxis AI images. The
   local Praxis image is not a registry artifact; the Lightspeed service image
   build currently fails in this environment because the pinned RHEL builder
   cannot access its package repository (HTTP 403).
3. The actual cluster API URL and stable routing ID, Keycloak realm issuer and
   CA, cluster trust domain, public Praxis HTTPS route, OLS service host/SNI,
   and the operator-created Lightspeed ServiceAccount/SPIFFE subject. Values
   containing `bm-cluster.e2e.bos.redhat.com` are examples from a previous
   target and must be replaced and verified before syncing a greenfield cluster.
4. Reviewed MCP namespace/resource permissions, working Vertex ADC credentials,
   a tested OIDC/API-server audience mapping, and a verified break-glass
   administrator kubeconfig before enabling Native OIDC.

Keep the cluster-neutral agent card public, but require Praxis JWT validation
and an explicit registered cluster ID for RPC. ACME initially supports blocking
`message/send` only; it does not support inbound task polling/cancel/streaming.

The shared registry belongs in deployment values visible to both ACME and
Praxis (for example `values-global.yaml` in this single-cluster Pattern):

```yaml
global:
  olsClusters:
    <stable-id-derived-from-the-verified-api-url>:
      apiURL: "https://api.<verified-cluster-domain>:6443"
      upstreamHost: lightspeed-app-server.openshift-lightspeed.svc.cluster.local
      upstreamPort: 8443
      upstreamSNI: lightspeed-app-server.openshift-lightspeed.svc
      # Add upstreamCA for a non-local or non-service-ca certificate.
```

This is a schema example only. Do not copy an example URL or enable a second
backend until its own OLS/MCP, token audience, trust and network path are ready.
See the [Praxis chart README](charts/all/praxis-proxy/README.md) for a commented
fake second-backend example and field semantics.

### Components and model roles

| Component | Role | Image/configuration |
| --- | --- | --- |
| `acme-agent` | Browser/A2A coordinator; chooses a downstream peer | Build from `agents/acme_agent/Containerfile`. Its LiteLLM coordinator uses `openai/gpt-6-luna`; its image reference is set on the `acme-agent` application. |
| `praxis-proxy` | Public Keycloak-validating gateway; static `X-OLS-Cluster` routing | Build the tracked Praxis AI source with the `ols_cluster_guard` filter. Use a published digest; do not enable with the stock image. |
| OpenShift Lightspeed service/operator | A2A investigation service plus operator-managed MCP | Build/publish the patched service and a newly versioned operator bundle/catalog. Lightspeed analysis uses Gemini-on-Vertex (`vertex-google`, `gemini-3.8-flash`), not ACME's coordinator model. |
| `lightspeed-agentic-operator` | Separate, retained AgenticRun/Agent functionality | Not the classic OLS operator and not part of ACME's A2A chain. Its `default` Agent uses `gpt-6-luna`; `vertex` and `gemini` Agents use Vertex. The current chart is a dev-preview using an upstream `:main` image; pin/approve it separately for production. |

### Build and publish images

Use a registry reachable by the target cluster. Choose a unique, immutable tag
for ACME (the chart accepts a tag) and record the resulting registry digest for
the digest-based Praxis, Lightspeed service, and OLM catalog values. Do not put
registry credentials in Git. Start from the repository root; keep
`IMAGE_REGISTRY` and `REPO_ROOT` exported in the same shell while running these
commands, and replace every quoted placeholder before use.

**ACME coordinator** — from the repository root:

```bash
export REPO_ROOT="$PWD"
export IMAGE_REGISTRY="quay.io/<your-org>"
export BUILD_TAG="$(git rev-parse --short=12 HEAD)"
podman login quay.io
podman build \
  --file agents/acme_agent/Containerfile \
  --tag "${IMAGE_REGISTRY}/acme-agent:${BUILD_TAG}" \
  agents/acme_agent
podman push "${IMAGE_REGISTRY}/acme-agent:${BUILD_TAG}"
```

Set the `acme-agent` application's `image.repository` and `image.tag` in
`variants/standalone/values-standalone.yaml` to that pushed image. Rebuild and
republish after changing ACME source; do not leave the old `da3c4e81` example
tag in place for a fresh install.

**Praxis AI gateway** — the AI image has its own version, separate from Praxis
Core v0.7.2. The tracked AI snapshot currently resolves Core to v0.7.2 and has
passed its Rust guard/unit/integration checks. From the repository root:

```bash
export PRAXIS_IMAGE="${IMAGE_REGISTRY}/praxis-ai"
export PRAXIS_TAG="<ai-version-or-unique-build-tag>"
make -C vendor/praxis-ai test
make -C vendor/praxis-ai container IMAGE="${PRAXIS_IMAGE}" VERSION="${PRAXIS_TAG}"
podman push "${PRAXIS_IMAGE}:${PRAXIS_TAG}"
```

Before using it in the chart, scan the image, produce an SBOM, test duplicate
wire headers and TLS/SNI/CA against the actual container, and record the
published digest. The local-only `praxis-ai-local:0.4.1-core0.7.2` image and its
local digest are not deployment inputs. The AI image tag must not be called
`0.7.2` just because its Core crates use that version.

**Lightspeed service** — from `vendor/lightspeed-service`:

```bash
cd "$REPO_ROOT/vendor/lightspeed-service"
make install-deps
make test-unit
podman build -f Containerfile \
  -t "${IMAGE_REGISTRY}/lightspeed-service:<unique-build-tag>" .
podman push "${IMAGE_REGISTRY}/lightspeed-service:<unique-build-tag>"
```

The current pinned Containerfile uses a RHEL builder whose DNF install received
HTTP 403 from `cdn.redhat.com` in this environment. No image was produced. Use
an authorized build environment or an approved, reviewed base-image change;
do not add subscription credentials to the repository. Scan, create an SBOM,
and record the pushed digest before setting `olsConfig.serviceImage`.

**Downstream OLS operator and OLM catalog** — from
`vendor/lightspeed-operator`:

1. Run `make test`. Choose a new downstream version; update the matching labels
   in `bundle.Dockerfile` and the CSV name/version as required by
   [`AGENTS.md`](vendor/lightspeed-operator/AGENTS.md). Do not reuse `1.1.4`.
2. Build/push the operator image. Capture its immutable digest and update the
   `lightspeed-operator` entry in `related_images.json`; update the
   `lightspeed-service-api` entry there as well to the intended default service
   image. OLSConfig may supply a separate service-image digest, but preserve a
   safe default for the operator flag.

   ```bash
   cd "$REPO_ROOT/vendor/lightspeed-operator"
   export OLS_VERSION="<approved-new-downstream-version>"
   export OLS_IMAGE="${IMAGE_REGISTRY}/lightspeed-operator:${OLS_VERSION}"
   make docker-build IMG="${OLS_IMAGE}"
   make docker-push IMG="${OLS_IMAGE}"
   ```

3. Generate/build/push the versioned v1 bundle:

   ```bash
   cd "$REPO_ROOT/vendor/lightspeed-operator"
   export BUNDLE_IMAGE="${IMAGE_REGISTRY}/lightspeed-operator-bundle:v${OLS_VERSION}"
   make bundle BUNDLE_VARIANT=v1 BUNDLE_TAG="${OLS_VERSION}"
   make bundle-build BUNDLE_IMG="${BUNDLE_IMAGE}" VERSION="${OLS_VERSION}"
   make bundle-push BUNDLE_IMG="${BUNDLE_IMAGE}"
   cd "$REPO_ROOT"
   ```

4. Build/publish a catalog using the approved downstream release pipeline, then
   configure `catalogSource.image` with its immutable digest and set the actual
   `subscription.packageName`, `channel`, and `startingCSV`. The checked-in
   operator Makefile does not provide catalog build/push targets; its
   `hack/bundle_to_catalog.sh` workflow is Konflux-snapshot-specific. Follow the
   release pipeline rather than guessing catalog contents or OLM identifiers.

The chart READMEs contain the exact security-sensitive values for each image
and chart: [OLS operator](charts/all/openshift-lightspeed-operator/README.md),
[OLSConfig](charts/all/openshift-lightspeed-config/README.md), and
[Praxis](charts/all/praxis-proxy/README.md).

### Configure secrets and deployment values

Copy the template to the Validated Patterns secret location and edit it locally
only; do not commit the resulting file or credential values:

```bash
cp values-secret.yaml.template \
  ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
$EDITOR ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
```

The template covers:

- `rhbk`: generated Keycloak admin/database passwords and console credentials.
- `llm-creds-vertex`: a Google ADC JSON file used by Lightspeed and the separate
  agentic operator.
- `llm-creds-openai`: the agentic operator's LiteLLM/OpenAI-compatible key.
- `acme-agent-litellm`: ACME's separate LiteLLM key.
- `keycloak-issuer-ca` (commented): uncomment and point to the verified PEM CA
  before enabling Keycloak OIDC validation for MCP/A2A.

Replace every target-specific `bm-cluster.e2e.bos.redhat.com` value in the
standalone file before a new-cluster install. In particular configure the
Keycloak issuer, SPIFFE trust domain, ACME token URL, and console route. Keep
`openshiftOIDC.enabled: "false"` for initial install. The standalone values
already enable break-glass; after it syncs, create and move
`admin-break-glass.kubeconfig` outside the repository. Follow the full Native
OIDC preflight in the [Keycloak chart README](charts/all/keycloak-oidc/README.md)
before deliberately enabling cluster-wide OIDC.

Update the existing application entries in
`variants/standalone/values-standalone.yaml`; do not enable features by copying
the illustrative values wholesale:

- `keycloak-oidc`: set the verified realm issuer, console route, cluster SPIFFE
  trust domain and discovery-provider endpoint. Keep
  `keycloak.lightspeedMcp.enabled` off until the OLS app-server namespace and
  ServiceAccount are confirmed. Enable it together with reviewed
  `mcpAccess.namespaces` only after Token B and API-server RBAC validation.
- `openshift-lightspeed-operator`: enable its CatalogSource/Subscription using
  the published catalog digest and the package/channel/starting CSV from that
  catalog. The chart deliberately has no guessed defaults.
- `openshift-lightspeed-config`: use the values shape in its README. Set
  `olsConfig.enabled`, `introspectionEnabled`, `serviceImage`, hardened
  `mcpServerSecurity`, A2A settings, CA/Vertex ExternalSecrets, and the
  digest-pinned `crdWait.cliImage`. Keep MCP RBAC limited to approved
  namespaces/resources.
- `praxis-proxy`: set the published image digest, verified Keycloak issuer/CA,
  public Route host, and `global.olsClusters` registry. Enable static routing
  and the guard only after that image has passed runtime tests.
- `acme-agent`: set its image tag, Keycloak issuer/token URL, SPIFFE trust
  domain, set `enabled: true`, and add a single
  `a2a.remoteAgents[0].endpoint` pointing at the public Praxis HTTPS Route. Set
  `clusterSelection.enabled` only with the same reviewed registry used by
  Praxis. The chart renders no ACME resources until explicitly enabled.

The separate `lightspeed-agentic-operator` remains deployed for direct
AgenticRun/Agent use; its `default`, `vertex`, and `gemini` Agents are not the
OLS A2A service. Its current standalone LLM settings use `gpt-6-luna` for the
default Agent and Vertex Anthropic/Gemini for the other Agents.

Before activating the new chain, deployment-owned values must configure all of
the following:

- A shared `global.olsClusters` entry: verified HTTPS API URL, stable map-key
  ID, static OLS service host/port/SNI, and trusted upstream CA. Keep ACME and
  Praxis on this same registry. Examples in the Praxis chart README are not
  active destinations.
- `openshift-lightspeed-operator`: enable CatalogSource/Subscription with the
  published catalog digest and real package/channel/CSV.
- `openshift-lightspeed-config`: set `olsConfig.enabled`, enable the operator's
  hardened MCP and A2A fields, set the published service image digest, enable
  the Vertex/Keycloak CA ExternalSecrets, provide the approved OpenShift CLI
  image digest for `crdWait.cliImage`, and configure a namespace-scoped MCP
  role only after tool/data review.
- `keycloak-oidc`: enable `keycloak.lightspeedMcp` with the actual OLS
  namespace/ServiceAccount, the new realm issuer/CA, and reviewed
  `mcpAccess.namespaces`. The service account group is
  `keycloak:acme-agent-lightspeed`.
- `praxis-proxy`: set `enabled`, static cluster routing, guard-enabled image
  digest, Keycloak issuer/CA, public Route host, and explicit card-discovery
  cluster.
- `acme-agent`: set its pushed image, verified HTTPS Praxis endpoint,
  `clusterSelection.enabled`, and the same shared registry. It requires an
  explicit registered API URL in every message; it does not silently select a
  default target.

When `mcpAccess.enabled` is set, the Keycloak chart also requires Native OIDC
claim mapping to be enabled so the API server can validate/map Token B. Enable
that cluster-wide change only after the Keycloak realm import, console secret,
and break-glass preflight have been verified. A chart sync wave does not prove
operator/CRD readiness; the OLSConfig chart's Sync hook waits for the CRD using
the supplied CLI image digest.

### Local chart checks

The following commands validate the inert/default chart state before any
cluster-specific values are enabled:

```bash
helm lint charts/all/acme-agent
helm lint charts/all/keycloak-oidc
helm lint charts/all/lightspeed-agentic-operator
helm lint charts/all/openshift-lightspeed-operator
helm lint charts/all/openshift-lightspeed-config
helm lint charts/all/praxis-proxy

helm template acme-agent charts/all/acme-agent --namespace acme-agent
helm template openshift-lightspeed-operator charts/all/openshift-lightspeed-operator \
  --namespace openshift-lightspeed
helm template openshift-lightspeed-config charts/all/openshift-lightspeed-config \
  --namespace openshift-lightspeed
helm template praxis-proxy charts/all/praxis-proxy \
  --namespace lightspeed-agentic-operator
```

The rendered defaults intentionally omit the ACME, OLSConfig, operator
Subscription/CatalogSource, and Praxis resources. To test enabled paths, use
the target-shape examples and validation commands in each chart README; do not
substitute sample hosts or tags for real deployment inputs.

### Install and verify the Pattern

Push the configured branch/ref to the Git remote that the Pattern CR will
track; GitOps cannot deploy unpushed local edits. Log in to the **fresh target
cluster** as cluster-admin, then run the Validated Patterns checks and install
through the utility container:

```bash
oc login "https://api.<verified-cluster-domain>:6443"
./pattern.sh make validate-prereq
./pattern.sh make validate-origin
./pattern.sh make validate-cluster
./pattern.sh make validate-schema
./pattern.sh make install
./pattern.sh make argo-healthcheck
```

`make install` loads the configured secrets when the secret framework is
enabled. If you update the secret file later, run
`./pattern.sh make load-secrets` and then wait for Argo reconciliation.

The standalone configuration leaves Native OIDC disabled for the first sync.
After Keycloak imports the fresh realm and `keycloak-oidc` creates the
break-glass token, verify and move the generated kubeconfig outside the repo:

```bash
make admin-break-glass-kubeconfig
oc --kubeconfig=admin-break-glass.kubeconfig whoami
```

Do not enable Native OIDC until the Keycloak issuer, console client/secret,
claim mappings, and break-glass access pass the checklist in the
[Keycloak chart README](charts/all/keycloak-oidc/README.md).

Verify the installed components in order; keep tokens and Secret contents out
of logs and terminal transcripts:

```bash
oc get applications -n vp-gitops
oc get catalogsource -n openshift-marketplace
oc get subscription,installplan,csv -n openshift-lightspeed
oc get crd olsconfigs.ols.openshift.io
oc get olsconfig cluster
oc get pods,svc -n openshift-lightspeed
oc get pods,svc,route -n lightspeed-agentic-operator
oc get route praxis-proxy -n lightspeed-agentic-operator
oc get networkpolicy -n openshift-lightspeed
```

Only enable the final OLSConfig/Praxis/ACME flags after the downstream OLM
release is installed, the custom images are scanned and digest-pinned, and live
Keycloak Token A/Token B, OpenShift RBAC, TLS, NetworkPolicy, and end-to-end
A2A checks pass. No live-cluster validation has been performed from this
workspace.

## Accessing Argo CD

With `global.singleArgoCD: true` (this pattern's default), the hub Argo CD
instance is always named `vp-gitops`, in the `vp-gitops` namespace --
regardless of the pattern name -- so this is stable across clusters:

```bash
export ARGOCD_URL="https://$(oc get route vp-gitops-server -n vp-gitops -o jsonpath='{.spec.host}')"
export ARGOCD_PASSWORD=$(oc get secret vp-gitops-cluster -n vp-gitops -o jsonpath='{.data.admin\.password}' | base64 -d)
echo "$ARGOCD_URL"
```

Username is `admin`. Treat the password as a secret: avoid printing it into a
terminal recording or pasting it into Git/chat.

## Accessing Keycloak

Once the `keycloak` application has synced, get the realm's Admin
Console URL and the operator-generated admin credentials (populated from
Vault via `keycloak.adminUser.passwordVaultKey`):

```bash
export KEYCLOAK_URL="https://keycloak.<trustDomain>"   # use the verified deployment trust domain
oc get secret keycloak-admin-user -n keycloak-system
```

Open `$KEYCLOAK_URL` and select the configured realm (default `rca`) to inspect
the human OIDC clients, ACME/Lightspeed workload clients, groups, and SPIFFE
identity provider declared by `keycloak-realm-import.yaml`. Retrieve credentials
only through your approved secret-handling workflow; do not print or commit
password values.

## Lightspeed Agentic Operator

See the [lightspeed-agentic-operator chart README](charts/all/lightspeed-agentic-operator/README.md)
for instructions on configuring, using, and testing the operator.

The target ACME → Praxis → OpenShift Lightspeed chain and token contract are
documented in [`agents/AUTHENTICATION.md`](agents/AUTHENTICATION.md). Praxis
image, routing, and deployment gates are in
[`charts/all/praxis-proxy/README.md`](charts/all/praxis-proxy/README.md).

## MAO Configuration

The standalone variant deploys the MAO runtime from
[`uie-mas-hosted`](https://gitlab.cee.redhat.com/ai_tools/uie-mas-hosted) into
the `mao` namespace. It is split into five Argo CD applications so the
components can be upgraded independently:

- MongoDB, with persistent storage
- Redis, for event streaming and HITL overrides
- Temporal, for workflow execution
- `mas-worker`, the Temporal worker
- `mas-api`, exposed through an OpenShift Route

The chart values are copied under
[`charts/all/mao`](charts/all/mao).
The default Pattern configuration is intended for development:

- MongoDB and Redis are internal, single-instance services.
- MongoDB client TLS is disabled because the bundled MongoDB chart does not
  configure MongoDB certificates or TLS server mode. This matches the source
  repository's Docker Compose stack. The source repository's Helm/OpenShift
  values set `MONGODB_TLS=true`, but its bundled MongoDB deployment does not
  enable server-side TLS; copying that setting unchanged would make the client
  connect to a plaintext server with TLS and fail.
- MAO uses `providerMode: dev`, which accepts bearer tokens permissively. Do
  not use this mode for an internet-facing deployment.
- The standalone development override allows the synthetic `dev` identity to
  use protected MAO API endpoints. This is configured on the `mao-api`
  application as `identity.adminAllowedUsers: '["dev"]'`; replace it with
  real users when switching to production authentication.
- Langfuse is disabled unless its Secret is configured.

### Using the MAO API

Only `mas-api` is exposed through an OpenShift Route. The Route root (`/`) is
not an API endpoint and returns 404. Get the usable URL with:

```bash
export MAO_API="https://$(oc get route mas-api -n mao -o jsonpath='{.spec.host}')"
curl -ksS "$MAO_API/api/health/"
```

The health request should return `{"status":"ok",...}`. In the development
configuration, use any non-empty bearer token; the example below uses
`dev-token`:

```bash
curl -ksS -H 'Authorization: Bearer dev-token' \
  "$MAO_API/api/catalog/elements.list.get"
```

To run a workflow, first save a YAML blueprint, then create and submit a
session. The API returns the blueprint and session IDs needed by the next
requests:

```bash
BLUEPRINT_ID=$(curl -ksS -X POST "$MAO_API/api/blueprints/blueprint.save" \
  -H 'Authorization: Bearer dev-token' \
  -H 'Content-Type: application/x-yaml' \
  --data-binary @uie-mas-hosted/run/fixtures/blueprint_llm_agent.yml \
  | jq -r '.blueprint_id')

SESSION_ID=$(curl -ksS -X POST "$MAO_API/api/sessions/user.session.create" \
  -H 'Authorization: Bearer dev-token' \
  -H 'Content-Type: application/json' \
  -d "{\"blueprintId\":\"$BLUEPRINT_ID\"}" \
  | jq -r '.')

curl -ksS -X POST "$MAO_API/api/sessions/user.session.submit" \
  -H 'Authorization: Bearer dev-token' \
  -H 'Content-Type: application/json' \
  -d "{\"sessionId\":\"$SESSION_ID\",\"inputs\":{\"user_prompt\":\"Hello\"}}"

curl -ksS -H 'Authorization: Bearer dev-token' \
  "$MAO_API/api/sessions/session.chat.get?sessionId=$SESSION_ID"
```

The example LLM blueprint requires a working model configuration. MongoDB,
Redis, and Temporal are internal services. The Temporal UI is not routed by
default; use `oc -n mao port-forward svc/temporal 8233:8233` and open
`http://localhost:8233` when needed.

For a production deployment, override the MAS API and worker values in
`variants/standalone/values-standalone.yaml` or with an application-specific
values file:

```yaml
overrides:
  - name: identity.providerMode
    value: prod
  - name: identity.ssoIssuerUri
    value: https://sso.example.com/realms/example
  - name: identity.ssoAudience
    value: api.mas
  - name: identity.adminAllowedUsers
    value: '["user@example.com"]'
```

Enable MongoDB TLS only when MongoDB is deployed with matching certificates
and server-side TLS configuration. Adjust the MongoDB storage class, image
references, API/worker replicas, and Route settings through the component
values as needed.
