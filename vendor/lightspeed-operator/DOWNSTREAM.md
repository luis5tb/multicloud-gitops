# Downstream OpenShift Lightspeed Operator snapshot

## Provenance and refresh

- Upstream: <https://github.com/openshift/lightspeed-operator>
- Pinned commit: `fd157f53b5cd3fa353cdeb44e9dbcd1fcfd6a1a6`
- The tracked source is an archive snapshot, not a Git submodule or nested clone. `LICENSE` and upstream attribution are retained.
- To refresh, fetch a reviewed immutable commit archive, compare it against this tree, and reapply the downstream hardening diff. Do not replace the tree from a floating branch or overwrite the downstream API/reconciliation changes without rerunning generation and tests.

## Downstream MCP security behavior

When `spec.ols.introspectionEnabled` is true or omitted, the OLSConfig must include:

```yaml
ols:
  introspectionEnabled: true
  mcpServerSecurity:
    authorizationURL: https://keycloak.example.com/realms/openshift
    oauthAudience: openshift-mcp
    caSecretRef:
      name: keycloak-issuer-ca
      key: ca.crt
    # Optional; omission enables only `core`.
    toolsets: [core, observability/metrics]
    # Optional; defaults to true for the narrowly selected OpenShift Prometheus pods.
    allowPrometheusMetrics: true
```

`keycloak-issuer-ca` must be a Secret in the operator/OLS namespace and the selected key must contain a valid PEM CA bundle. The CRD rejects empty/non-DNS Secret names, empty/invalid key names (including `.`/`..` prefixes), and an explicitly empty toolset array; omitting toolsets still defaults to `core`. The API also rejects a missing security block while introspection is enabled, non-HTTPS issuer URLs, and audiences other than `openshift-mcp`; reconciliation fails closed for invalid/missing Secret data. Existing OLSConfig objects that enable introspection must be updated with this block before the downstream CRD/operator is rolled out. Setting introspection false remains the explicit way to disable the MCP operand.

The generated MCP TOML always sets `require_oauth = true`, `cluster_auth_mode = "passthrough"`, `read_only = true`, the issuer URL, `oauth_audience = "openshift-mcp"`, and the mounted CA path. It does not set `skip_jwt_verification` and contains no `[token_exchange]`: the Lightspeed service/A2A extension must pass its already-exchanged Token B as a request-scoped token. Secret and all RBAC API resources remain denied, and the MCP ServiceAccount receives no RBAC grants.

The Keycloak issuer CA is mounted at `/etc/mcp-server/oidc-ca/ca-bundle.crt`. It is **not** the service-ca serving certificate: the MCP TLS keypair remains the operator-generated `openshift-mcp-server-tls` at `/etc/tls/`, and OLS continues to trust that service certificate using its separate `lightspeed-agentic-mcp-ca` material. OIDC CA Secret updates are watched and roll the MCP Deployment.

The operator-owned MCP NetworkPolicy is reconciled in place to allow HTTPS only from app-server pods and, by default, the cluster Prometheus pods (`app.kubernetes.io/name=prometheus`, `prometheus=k8s`) in `openshift-monitoring`. `allowPrometheusMetrics: false` removes the scrape peer. NetworkPolicies are additive: inspect any separately managed policy selecting the MCP pods because another permissive policy can still widen access.

## Optional A2A workload and service image API

The new `spec.ols.a2a` block defaults to disabled. An enabled example is:

```yaml
ols:
  introspectionEnabled: true
  mcpServerSecurity:
    authorizationURL: https://keycloak.example.com/realms/openshift
    oauthAudience: openshift-mcp
    caSecretRef: {name: keycloak-issuer-ca, key: ca.crt}
  a2a:
    enabled: true
    targetClusterID: api-bm-cluster-e2e-bos-redhat-com-6443
    publicURL: https://praxis.example.com
    # These have fixed defaults and enum validation:
    # inboundAudience: lightspeed-a2a
    # allowedCallerClientID: acme-agent
    # exchangeClientID: lightspeed-mcp
  serviceImage: quay.io/example/lightspeed-service@sha256:<64-lowercase-hex-digest>
```

`a2a.enabled` requires introspection and `mcpServerSecurity`. Cluster IDs are DNS-safe labels; `publicURL` must be an HTTPS origin without a path. Audience/client values are fixed to `lightspeed-a2a`, `acme-agent`, and `lightspeed-mcp`. `serviceImage` is optional; when present it must use `@sha256:<64 lowercase hex>` and overrides the operator's `--service-image` only for the app-server container. When absent, the existing startup-argument image selection is unchanged.

`OLSConfig` is cluster-scoped and selecting a custom image executes that image with the app-server ServiceAccount. Restrict write access to the OLSConfig to cluster administrators/trusted release automation; digest pinning provides immutability, not publisher trust.

With A2A enabled, the app-server keeps ServiceAccount `lightspeed-app-server`, mounts the read-only `csi.spiffe.io` volume at `/spiffe-workload-api`, and receives the A2A env contract described in `.ai/spec/how/config-generation.md`. It mounts the *same Secret key* used by MCP local OIDC at `/etc/certs/a2a-keycloak-ca/ca-bundle.crt`; this does not replace the independent service-ca trust for OLS-to-MCP TLS. Missing, optional, or invalid CA data makes app-server Deployment generation fail rather than generating a workload with absent trust.

OIDC CA Secret update events restart MCP and, when A2A is enabled, the app-server. The existing generic Secret delete handler is intentionally a no-op; if the CA Secret is deleted, the next reconciliation fails app-server Deployment generation, but deletion does not itself enqueue reconciliation or immediately scale down an already-running Deployment. Recreate the Secret and trigger OLSConfig reconciliation to restore the desired pod. A2A requests fail TLS verification if the mounted issuer CA is unavailable, but immediate delete-event teardown remains a watcher limitation.

The cluster must separately install/configure the SPIFFE CSI driver and register `system:serviceaccount:<ols-namespace>:lightspeed-app-server` for the expected trust domain/audience. This source snapshot does not create a `ClusterSPIFFEID` or SPIFFE controller/CSI installation.

### Actual app-server identity and selector

At this pinned revision, the app-server Deployment explicitly uses ServiceAccount `lightspeed-app-server`; in the default namespace its Kubernetes identity is:

```text
system:serviceaccount:openshift-lightspeed:lightspeed-app-server
```

The MCP ingress NetworkPolicy uses these app-server pod labels from `GenerateAppServerSelectorLabels()` (namespace-local selector):

```yaml
app.kubernetes.io/component: application-server
app.kubernetes.io/managed-by: lightspeed-operator
app.kubernetes.io/name: lightspeed-service-api
app.kubernetes.io/part-of: openshift-lightspeed
```

For a SPIFFE federation subject in the default namespace, the expected workload subject is `spiffe://<trust-domain>/ns/openshift-lightspeed/sa/lightspeed-app-server`. The operator may be installed into a non-default namespace; use its actual namespace in the subject.

## Build and verification

From this directory:

```bash
make test
```

This runs code generation, formatting, vet, CRD test setup, and the API/controller unit suite. In this snapshot it passes. The Makefile retains `ignore_autogenerated` in `controller-gen`'s build tags; without it the generated deepcopy file is incorrectly regenerated without the API runtime-object methods.

For bundle regeneration, use the repository's pinned Go `yq` binary (the host Python `yq` wrapper is not compatible) and kustomize:

```bash
KUSTOMIZE="$PWD/bin/kustomize" make bundle
```

The upstream bundle script writes `bundle-v1/`; the tracked release bundle under `bundle/` must be synchronized from its generated output. The source CSV still reports upstream version `1.1.4`; **do not publish changed contents as version 1.1.4**. A downstream release must bump the operator/bundle version, build and publish an immutable operator image, regenerate the versioned bundle/catalog, and test OLM install and upgrade.

An operator container can be built from this source with `make docker-build IMG=<registry>/<name>:<immutable-tag>` (and pushed with `make docker-push`). This only builds the operator source. No patched operator image, catalog, OLM Subscription, or live-cluster deployment is produced by this snapshot. Although the source now supports `spec.ols.serviceImage`, those fields are unavailable to clusters still running the published upstream operator; build/publish/install the downstream operator image and a new versioned bundle/catalog first.

## Not-yet-supported deployment wiring / validation limits

- The checked-in Pattern does not yet install a downstream operator bundle/catalog. A stock OLM Subscription continues to run the published upstream operator and will not receive these reconciliation changes.
- The existing `--service-image` process argument remains the fallback. `spec.ols.serviceImage` is now an immutable-digest override in this fork, but no downstream operator image/catalog or upgrade-safe OLM packaging has been built/published/installed. Do not apply an Argo override to an operator-owned Deployment.
- The service-side A2A implementation must still supply exchanged Token B through the existing request-scoped MCP bearer resolution. This operator does not exchange tokens and does not turn Token A or a pod ServiceAccount token into Token B.
- Unit tests and generated manifests are not live-cluster validation. OLM upgrade behavior, CNI policy enforcement, real Keycloak JWKS/CA validation, Token B claims, API-server OIDC/RBAC, and effective MCP `401` behavior remain unverified here.
- The policy updates the operator's formerly permissive MCP NetworkPolicy, but NetworkPolicy policy union is additive; audit all other policies and routes before claiming that ingress is isolated.
