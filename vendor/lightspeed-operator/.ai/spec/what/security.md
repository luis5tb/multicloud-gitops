# Security

The operator enforces security boundaries through RBAC, network policies, pod security contexts, and credential management.

## Behavioral Rules

### RBAC
1. The operator creates a ClusterRole (`lightspeed-app-server-sar-role`) and ClusterRoleBinding for the backend service account with permissions for: SubjectAccessReview (create), TokenReview (create), ClusterVersion (get, list), and pull-secret Secret (get by resourceName).
2. These permissions enable the backend service to authenticate users via Kubernetes TokenReview and authorize API access via SubjectAccessReview.
3. The operator controller itself requires RBAC including: managing deployments, services, configmaps, secrets, PVCs, network policies, RBAC resources (clusterroles, clusterrolebindings, roles, rolebindings), console plugins, image streams, and monitoring resources (servicemonitors, prometheusrules). It also has NonResourceURL permissions for `/ls-access` and `/ols-metrics-access`.
4. The backend service account also receives a NonResourceURL permission for `/ls-access` to control Lightspeed API access (declared via kubebuilder RBAC markers on the controller).

### Network Policies
5. Each component has its own NetworkPolicy restricting ingress:
   - Operator (`lightspeed-operator`): allows Prometheus scraping from `openshift-monitoring` namespace on port 8443.
   - Backend/AppServer (`lightspeed-app-server`): allows Prometheus from `openshift-monitoring`, OpenShift Console pods from `openshift-console`, and ingress controllers (namespaces with `network.openshift.io/policy-group: ingress`), all on port 8443.
   - PostgreSQL (`lightspeed-postgres-server`): allows only backend pods (matched by `app.kubernetes.io/name: lightspeed-service-api` label) and OTel Collector pods (matched by the OTel Collector labels).
   - Console UI (`lightspeed-console-plugin`): allows only OpenShift Console pods from `openshift-console` namespace.
   - OTEL Collector (`lightspeed-otel-collector`): allows all pods in the operator namespace (empty `PodSelector`) on OTLP gRPC `:4317` and `postgres_admin` HTTPS `:8080`; allows Prometheus from `openshift-monitoring` on HTTPS metrics `:8888` only.
6. Network policies use combined pod label selectors and namespace selectors for source filtering.
7. Egress is unrestricted for all components. PolicyTypes includes only `Ingress`; egress rules are empty (`[]`), meaning no egress restrictions.

### Pod Security
8. All containers (main containers and sidecars) run with restricted security context: `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `runAsNonRoot: true`, `seccompProfile: RuntimeDefault`, `capabilities: {drop: [ALL]}`. This is enforced via `utils.RestrictedContainerSecurityContext()`.
9. Writable paths (`/tmp`, llama-cache, user-data) use `emptyDir` volumes to provide write access on an otherwise read-only root filesystem.

### Credential Management
10. LLM provider credentials are validated during the annotation phase via `ValidateLLMCredentials()`. The operator verifies that each referenced secret exists and contains the expected key before proceeding with reconciliation.
11. Standard providers must have a secret with the `apitoken` key (or the key specified by `credentialKey`). Azure OpenAI providers must have either `apitoken` or all three of `client_id`, `tenant_id`, `client_secret`.
12. Custom TLS secrets are validated via `ValidateTLSSecret()` to ensure they contain `tls.crt` and `tls.key`.
13. Provider credentials are mounted as read-only volume files at `/etc/apikeys/<secretName>/`, never exposed as environment variables.
14. PostgreSQL passwords are generated randomly on first creation (via the postgres reconciler) and never updated on subsequent reconciliations.
15. MCP server header secrets must contain a specific key `header` (constant `MCPSECRETDATAPATH`) and are mounted read-only at `/etc/mcp/headers/<secretName>/`.

### OpenShift MCP Server Security
16. The downstream OpenShift MCP config always emits `require_oauth = true`, local OIDC validation (`authorization_url`, `oauth_audience = "openshift-mcp"`, and a mounted issuer CA), and `cluster_auth_mode = "passthrough"`. It never enables `skip_jwt_verification` or MCP-side token exchange. A configured HTTPS issuer and CA Secret are required when introspection is enabled; missing/invalid configuration fails closed.
17. The downstream TOML always emits `read_only = true`, permits only configured allow-listed toolsets (default `core`), and denies `core/v1/secrets` plus all `rbac.authorization.k8s.io/v1` resources. Kubernetes RBAC remains authoritative for cluster access.
18. MCP ingress is restricted to pods with the exact operator app-server labels, plus the exact cluster Prometheus selectors in `openshift-monitoring` when metrics scraping is enabled. NetworkPolicy enforcement still depends on the cluster CNI.
19. User-defined MCP servers (via `spec.mcpServers`) are the user's responsibility to secure.

### Optional A2A Workload Security
20. `spec.ols.a2a.enabled` defaults false. Enabling it requires OpenShift introspection and `spec.ols.mcpServerSecurity`; the app-server receives only the fixed caller/audience/client contract and the target ID/public Praxis origin from the CR.
21. The app-server uses its existing `lightspeed-app-server` ServiceAccount for the SPIFFE CSI Workload API mount. The CSI volume is read-only; the Keycloak CA Secret is separately mounted read-only and is the same Secret/key used by MCP local OIDC validation. The service-ca bundle used for OLS-to-MCP HTTPS remains distinct.
22. A2A does not change the stock REST `k8s` auth path or add RBAC. SPIFFE CSI driver installation and workload registration are cluster-level prerequisites outside this operator patch.

## Configuration Surface

The operator exposes the local OIDC issuer/CA, a bounded toolset allow-list, and a Prometheus scrape toggle for the built-in MCP operand. Read-only mode, `openshift-mcp` audience, passthrough mode, denied resources, and app-server ingress are not weakenable through this API. Optional A2A settings are constrained to one target ID, HTTPS Praxis origin, fixed inbound audience, and fixed Keycloak client IDs. A custom service image is accepted only as an immutable `@sha256:` reference; otherwise the operator startup image argument remains in use.

## Constraints

1. The operator must not store credentials in ConfigMaps or environment variables directly. Secrets are always file-mounted as read-only volumes.
2. Network policies require a CNI plugin that supports NetworkPolicy enforcement.
3. All containers must run as non-root with read-only root filesystems.

## Known Limitations

1. **Agentic v2 dynamic cluster RBAC is not yet ownership-safe.** The agentic
   controller currently creates and deletes per-run `ClusterRole` and
   `ClusterRoleBinding` resources named `ls-exec-cluster-<AgenticRun UID>` and
   updates reader bindings discovered by ServiceAccount subject. Its controller
   identity therefore requires unrestricted mutation of those cluster-scoped
   RBAC resource types. Kubernetes RBAC cannot restrict this access to a
   dynamic name prefix, and a static `resourceNames` list would break creation
   and cleanup for new runs.

## Planned Changes

None.
