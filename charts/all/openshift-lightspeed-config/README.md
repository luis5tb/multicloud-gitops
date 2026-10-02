# OpenShift Lightspeed configuration

This chart can render the `OLSConfig` custom resource and optional ESO
`ExternalSecret`s. It is inert by default:
`olsConfig.enabled: false` and
`vertexCredentials.externalSecret.enabled: false`. It does not install the
OpenShift Lightspeed Operator or create/patch operator-owned Deployments,
Services, MCP config, NetworkPolicies, or ConfigMaps. The `OLSConfig` name is
`cluster`, as required by the operator release inspected for this workstream.

The Google Vertex provider references a Secret named `llm-creds-vertex` with
the raw Google credential JSON under key `apitoken`. When enabled, the Vertex
ExternalSecret reads property `credentials` from Vault key
`secret/data/global/llm-creds-vertex`; that matches this repository's
Vault/ESO convention and the operator's `ProviderSpec` default credential key.
No credential value is stored in this chart. The root
`values-secret.yaml.template` must contain the corresponding `credentials`
field and a user must load it into Vault before inference can work. To render
the chart without ESO, leave its ExternalSecret disabled and provide the
referenced Secret separately.

The default model/provider is `vertex-google` / `gemini-3.8-flash`, project
`redhat-marketplace-dev`, location `global`. These values reflect the
Lightspeed Operator API at commit
[`fd157f53b5cd3fa353cdeb44e9dbcd1fcfd6a1a6`](https://github.com/openshift/lightspeed-operator/tree/fd157f53b5cd3fa353cdeb44e9dbcd1fcfd6a1a6)
and the service snapshot at commit
[`690939861bf7888209b1c9187614af3efd3a5a26`](https://github.com/openshift/lightspeed-service/tree/690939861bf7888209b1c9187614af3efd3a5a26).
The service source pin, build and refresh instructions are in
[`vendor/lightspeed-service/DOWNSTREAM.md`](../../../vendor/lightspeed-service/DOWNSTREAM.md).

## Security gate: deployment remains disabled by default

The downstream operator source now emits required bearer auth, Keycloak OIDC
validation, bearer passthrough, read-only MCP configuration and restricted
resources/tools. Those changes are in the vendored source and generated bundle,
but no new versioned operator image or catalog has been built/published. Stock
upstream OLM installations do not contain those changes. Therefore this chart
defaults to rendering no OLSConfig or ExternalSecrets; enabling its features
before installing the reviewed downstream operator release is unsafe.

The chart now exposes the downstream operator fields, but they remain opt-in.
After installing the **patched downstream operator**, the CA Secret and
Keycloak client are ready, and the custom service image is published by digest,
the target OLSConfig shape is:

```yaml
olsConfig:
  enabled: true
  introspectionEnabled: true
  serviceImage: quay.io/example/lightspeed-service@sha256:<64-lowercase-hex-digest>
  mcpServerSecurity:
    enabled: true
    authorizationURL: https://keycloak.example.com/realms/rca
    oauthAudience: openshift-mcp
    caSecretName: keycloak-issuer-ca
    caSecretKey: ca-bundle.crt
    toolsets: [core]
    allowPrometheusMetrics: false
  a2a:
    enabled: true
    targetClusterID: <registered-cluster-id>
    publicURL: https://praxis.example.com
    inboundAudience: lightspeed-a2a
    allowedCallerClientID: acme-agent
    exchangeClientID: lightspeed-mcp

praxisIngress:
  enabled: true
  namespace: lightspeed-agentic-operator
  podSelector:
    app.kubernetes.io/name: praxis-proxy
    app.kubernetes.io/instance: praxis-proxy

keycloakCA:
  externalSecret:
    enabled: true
    name: keycloak-issuer-ca
    targetSecretName: keycloak-issuer-ca
    targetKey: ca-bundle.crt
    vaultKey: secret/data/global/keycloak-issuer-ca
    vaultProperty: ca-bundle

vertexCredentials:
  externalSecret:
    enabled: true

crdWait:
  cliImage: registry.redhat.io/openshift4/ose-cli@sha256:<64-lowercase-hex-digest>
```

The example is a target shape, **not** the current default or an instruction to
enable OLS/MCP before the downstream bundle and runtime inputs are verified.
`serviceImage` must be a real immutable digest, the issuer must match Keycloak
token `iss`, the CA must
verify that issuer, and the target ID must equal the cluster registry key. The
MCP CA Secret is separate from the service-ca certificate used for OLS-to-MCP
HTTPS. To source the CA through Vault, uncomment/provision the optional
`keycloak-issuer-ca` file entry in `values-secret.yaml.template`; alternatively
create the referenced Secret through an approved CA-sync mechanism. Do not put
private keys or bearer tokens in the values file.

The chart's conditional `olsConfig.a2a` fields serialize the target ID, public
Praxis URL, fixed Keycloak audience/client IDs and immutable custom service
image. With A2A enabled the patched operator mounts the SPIFFE Workload API and
Keycloak CA and supplies the service's identity environment contract. These
fields do not install SPIFFE/ZTWIM or create a `ClusterSPIFFEID`; cluster
operators must install the CSI driver and register the exact workload subject.
When `a2a.enabled` is true, `praxisIngress` must allow only the labeled Praxis
pods in their namespace to reach the operator-labeled OLS app-server pods. This
NetworkPolicy is additive to the operator's NetworkPolicy; audit all selected
policies and do not create a direct OLS Route. Praxis remains the sole intended
public path.
The inspected operator takes its service image from the
[`--service-image` startup flag at `cmd/main.go:206`](https://github.com/openshift/lightspeed-operator/blob/fd157f53b5cd3fa353cdeb44e9dbcd1fcfd6a1a6/cmd/main.go#L206),
not an `OLSConfig` field; the downstream fork adds an immutable-digest
`spec.ols.serviceImage` override. This only works after the patched operator
image and a **new versioned OLM bundle/catalog** are built and installed; a
stock operator ignores these fields.
Until those separately owned integration settings exist, the vendored A2A
identity provider returns 503 by default. Stock REST authentication remains
the operator/service's `k8s` mode; this chart does not set a `noop` mode or
disable auth.

When `olsConfig.enabled=true`, the chart also requires `crdWait.cliImage` to be
an immutable, deployment-approved OpenShift CLI image digest. An Argo Sync hook
with narrowly scoped `get/watch` access waits for
`olsconfigs.ols.openshift.io` to reach `Established` before applying
ExternalSecrets or the OLSConfig. This prevents a child-Application wave from
being mistaken for CRD readiness; it does not prove the operator is available,
the operand is reconciled, or credentials/RBAC are valid.

## Validation

```bash
helm lint charts/all/openshift-lightspeed-config
helm template openshift-lightspeed-config charts/all/openshift-lightspeed-config \
  --namespace openshift-lightspeed
```

The chart does not create an OLM Subscription or invent a package, channel,
CSV, or operator version. The CRD wait hook fails closed if the downstream
operator does not install the expected CRD before the configured timeout.
