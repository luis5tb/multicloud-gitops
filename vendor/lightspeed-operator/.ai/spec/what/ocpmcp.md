# OpenShift MCP Server (ocp-mcp)

Standalone HTTPS OpenShift MCP server operand managed by the `ocpmcp` package ([OLS-3526](https://redhat.atlassian.net/browse/OLS-3526)). Replaces the former app-server sidecar. Related: [OLS-3684](https://redhat.atlassian.net/browse/OLS-3684) (agentic handoff MCP keys/CA), [OLS-3594](https://redhat.atlassian.net/browse/OLS-3594) (deferred agentic auto-injection).

## Architecture

```text
lightspeed-service (app-server)
  └─ HTTPS MCP client
       url: https://openshift-mcp-server.<ns>.svc:8443/mcp
       trust: Secret lightspeed-agentic-mcp-ca → /etc/certs/openshift-mcp-server-ca/service-ca.crt
            │  (PEM from openshift-service-ca.crt; same cluster CA as OTEL)
            ▼
openshift-mcp-server Deployment + ClusterIP Service (:8443)
  ├─ service-ca serving cert Secret  openshift-mcp-server-tls
  └─ TOML ConfigMap                   openshift-mcp-server-config
```

Gated by `spec.ols.introspectionEnabled` (default `true` when absent). When false, the operator removes managed MCP resources and sets `MCPServerReady=True` with `Reason=NotConfigured`.

## Behavioral Rules

### Activation
1. When `spec.ols.introspectionEnabled` is true (or absent), Phase 1 and Phase 2 reconcile the standalone MCP operand.
2. When false, Phase 1 calls `ocpmcp.Remove()`; Phase 2 skips deployment reconciliation and records `MCPServerReady` as `NotConfigured`.

### Phase 1 Resources
3. ConfigMap `openshift-mcp-server-config` — TOML runtime config (required local OIDC, read-only mode, configured allow-listed toolsets, denied Secret/RBAC resources).
4. ServiceAccount `openshift-mcp-server` — no RBAC bindings; callers pass their own token (app-server uses `Authorization: ols`).
5. NetworkPolicy `openshift-mcp-server` — ingress on TCP `:8443` only from the exact app-server pod labels, plus the optional Prometheus pods in `openshift-monitoring` for scraping.

### Phase 2 Resources
6. Service `openshift-mcp-server` — ClusterIP, port `https` `:8443`, serving-cert annotation → Secret `openshift-mcp-server-tls`.
7. Wait for TLS Secret keys `tls.crt` / `tls.key` and the configured OIDC CA Secret key before creating/updating the Deployment.
8. Deployment `openshift-mcp-server` — HTTPS (`--tls-cert` / `--tls-key`), probes on `/healthz` (HTTPS), image from `--openshift-mcp-server-image`, `PullIfNotPresent`. The Keycloak/OIDC CA is mounted read-only at `/etc/mcp-server/oidc-ca/ca-bundle.crt`; it is separate from the service-ca serving certificate mounted at `/etc/tls/`. Replicas/resources/tolerations/nodeSelector from `spec.ols.deployment.mcpServer` (`Config`).

### App-server Integration
9. olsconfig `mcp_servers` includes an `openshift` entry pointing at `https://openshift-mcp-server.<namespace>.svc:8443/mcp` with `Authorization: ols` when introspection is enabled. See `app-server.md` and `config-generation.md`.
10. App-server mounts client CA Secret `lightspeed-agentic-mcp-ca` (sourced from `openshift-service-ca.crt`) at `/etc/certs/openshift-mcp-server-ca/` and adds `service-ca.crt` to `extra_ca`. See `tls.md` / `agentic-sandbox-profile.md`. There is no dedicated MCP inject-cabundle ConfigMap; Phase 1 / `Remove` deletes leftover `openshift-mcp-server-ca` on upgrade.
11. App-server Deployment tracks MCP client CA content hash (`ols.openshift.io/mcp-server-ca-configmap-hash`) only while introspection is enabled.

### Watching and Restarts
12. Secret `openshift-mcp-server-tls` is listed statically in `WatcherConfig.Secrets.SystemResources`. Watching is gated by `OpenShiftMCPServerTLSWatchEnabled` (`syncOpenShiftMCPServerTLSWatcher`), set from `introspectionEnabled`, so enable/disable does not rewrite the SystemResources slice under the informer.
13. On TLS Secret data change, the watcher restarts `openshift-mcp-server`, `lightspeed-app-server`, and touches `lightspeed-agentic-configuration`. OIDC CA Secrets referenced by `spec.ols.mcpServerSecurity.caSecretRef` are annotated and restart the MCP deployment on rotation.
14. ConfigMap `openshift-service-ca.crt` changes also restart `lightspeed-app-server`, refreshing all client CA Secrets (OTEL, MCP, RHOKP).
15. MCP Deployment also tracks ConfigMap, service-ca TLS Secret, and OIDC CA Secret ResourceVersions and rolls when they change.

### Security
16. TOML requires OAuth, validates tokens against the configured HTTPS issuer/audience with the mounted OIDC CA, and explicitly sets `cluster_auth_mode = "passthrough"`. No skip-verification or MCP-side token exchange is emitted; Lightspeed supplies the already-exchanged request token.
17. `read_only = true` is fixed by the operator. Toolsets are configurable from an allow-list and default to `core`; Secret and all RBAC resources remain denied regardless of the selected toolsets. Metrics, when selected, uses in-cluster Thanos Querier and Alertmanager URLs. Metrics `guardrails = "!tsdb"` (PromQL query safety, not RBAC) follows upstream OpenShift guidance when Thanos lacks the TSDB status API.
18. User-defined MCP servers (`spec.mcpServers`) are out of scope for this operand.

### Monitoring
19. ServiceMonitor `openshift-mcp-server-monitor` (OLS-3728) — scrapes MCP server metrics via HTTPS on port 8443, path `/metrics` (Go promhttp). Server TLS only (service-ca CA bundle + `serverName`; no client certs / Bearer token), 30s interval. Reconciled in Phase 2 via `utils.ReconcileServiceMonitor()`. Skipped if Prometheus Operator CRDs are not installed.

### Finalizer
20. On CR deletion, `ocpmcp.Remove()` deletes Deployment, Service, NetworkPolicy, ConfigMap, ServiceAccount, TLS Secret (`openshift-mcp-server-tls`), and ServiceMonitor (`openshift-mcp-server-monitor`) before owned-resource sweep.

## Configuration Surface

| Field path | Description |
|---|---|
| `spec.ols.introspectionEnabled` | Enable/disable standalone MCP (`*bool`, default true) |
| `spec.ols.mcpKubeServerConfig.timeout` | Timeout seconds for the built-in openshift MCP entry in olsconfig |
| `spec.ols.mcpServerSecurity.authorizationURL` | Required HTTPS OIDC issuer for local bearer verification |
| `spec.ols.mcpServerSecurity.oauthAudience` | Required bearer audience; only `openshift-mcp` is accepted |
| `spec.ols.mcpServerSecurity.caSecretRef` | Required Secret key in the OLS namespace containing the issuer CA bundle |
| `spec.ols.mcpServerSecurity.toolsets` | Optional allow-listed MCP toolsets; omitted defaults to `core`; read-only remains enforced |
| `spec.ols.mcpServerSecurity.allowPrometheusMetrics` | Optional Prometheus ingress peer toggle; defaults to true |
| `spec.ols.deployment.mcpServer` | Standalone MCP `Config` (replicas, resources, tolerations, nodeSelector) |
| `--openshift-mcp-server-image` | MCP container image override |

## Constraints

1. Multi-replica is allowed; Streamable HTTP is configured for stateless operation upstream.
2. The MCP ServiceAccount has no cluster RBAC; authorization uses the calling user's token.
3. The `openshift-mcp-server` image is shipped by the OCP MCP team from `registry.redhat.io/openshift-mcp/openshift-mcp-server-rhel9`. OLS does not build or release this image. Digest/tag updates track the OCP MCP team's releases; bump `related_images.json` and regenerate the bundle when a new release is available.
4. Agentic/sandbox reuse of the MCP Service URL is published in the handoff ConfigMap; the MCP client CA Secret is owned by appserver when introspection is enabled — see `agentic-sandbox-profile.md`. Optional auto-injection into agent runs remains deferred (OLS-3594).

## Downstream security extension

The OIDC/tool/network extension is maintained in this local source snapshot. It is not present in published upstream OLM bundles until a downstream operator image and bundle are built and installed.
