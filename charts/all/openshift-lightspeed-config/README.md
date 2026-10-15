# openshift-lightspeed-config

Configures the OpenShift Lightspeed Operator (`ols.openshift.io`), installed by
the `lightspeed-operator` Subscription added in
`variants/standalone/values-standalone.yaml`. This chart does **not** deploy
the Lightspeed app-server or MCP server Deployments/Services themselves --
those are reconciled by the operator from the `OLSConfig` custom resource
this chart renders. See `LIGHTSPEED_DESIGN.md` section 4 and
`LIGHTSPEED_IMPLEMENTATION_PLAN.md` Phase 2 (T2.1-T2.3) for the full design
rationale; this file only covers what this chart configures and what it does
not.

## A note on sourcing

This chart's field names, defaults and CRD-shape assumptions (apiVersion
`ols.openshift.io/v1alpha1`, the `google_vertex` provider shape, the
cluster-singleton CR name, the operator-managed pod label guesses used by
`templates/networkpolicy.yaml`/`templates/cluster-spiffe-id.yaml`, etc.) are
based on `LIGHTSPEED_IMPLEMENTATION_PLAN.md`'s frozen interface table (T0.9)
and reasonable conventions, **not** on an independently verified copy of the
operator's actual CRD. An earlier draft of this chart additionally cited a
local, untracked `vendor/lightspeed-operator` checkout found on the
filesystem outside this worktree as supporting evidence for several of these
fields (notably `mcpServerSecurity`/`A2AConfig`). That checkout turned out to
be work-in-progress from an unrelated, external agent process running in
parallel on the same machine -- not trustworthy upstream reference material
-- and has been removed as a cited source throughout this chart. (A later CRD
investigation confirmed `mcpServerSecurity` is **not** a real OLSConfig
field; the stanza it would have rendered has since been removed -- see "MCP
hardening" below.) Nothing in
this chart or its templates should be read as "confirmed via upstream
source"; everything not explicitly stated as a frozen interface decision in
`LIGHTSPEED_IMPLEMENTATION_PLAN.md` is this chart's best-effort assumption
and must be verified against the actually-installed operator (`oc explain
olsconfig.spec`, the installed CRD's OpenAPI schema, and
`oc get pods -n <namespace> --show-labels`) before being trusted in
a real deployment.

## What this chart creates

- `templates/olsconfig.yaml` -- the `OLSConfig` singleton CR (`metadata.name:
  cluster` by convention), with a `google_vertex` LLM provider pointed at the
  same Vertex AI project/model as `charts/all/lightspeed-agentic-operator`'s
  `agents.gemini`, and `spec.ols.introspectionEnabled: true` so the operator
  auto-deploys its built-in OpenShift MCP server.
- `templates/externalsecret-vertex.yaml` -- an ESO `ExternalSecret` reading
  the same Vault path as `lightspeed-agentic-operator`'s `llm-creds-vertex`
  (`secret/data/global/llm-creds-vertex`, `credentials` property) into a
  **separate** Kubernetes Secret (`ols-llm-creds-vertex` by default) and key
  (`apitoken` by default, per T0.9's frozen interface decision) so the two
  operators never reconcile the same object.
- `templates/keycloak-ca-sync-*.yaml` + `files/sync_keycloak_ca.py` -- syncs
  the cluster's managed ingress CA bundle (which also signs Keycloak's Route
  certificate) into a Secret in this namespace, following this pattern's
  established CA-sync approach. Unlike the ConfigMap target other charts use,
  this copy targets a Secret. Disabled by default; the optional A2A OIDC trust
  bridge can mount it for private Keycloak Routes. MCP Mode A (passthrough)
  does not need this CA.
- `templates/networkpolicy.yaml` -- best-effort ingress restriction for the
  (unconfirmed-label) operator-managed app-server pods to same-namespace
  traffic and the Praxis gateway.
- `templates/cluster-spiffe-id.yaml` -- SPIRE `ClusterSPIFFEID` registration
  for the operator-created `lightspeed-app-server` ServiceAccount, disabled
  by default (trust domain is installation-specific).

## MCP hardening

**The gap.** `LIGHTSPEED_DESIGN.md` section 5 is explicit that the
operator-generated OpenShift MCP server does **not** set `require_oauth`, and
that the MCP binary's own default for that flag is `false`. With
`require_oauth=false` the HTTP auth middleware is a no-op, and a tokenless
call to MCP **falls back to the MCP pod's ServiceAccount** -- a confused-deputy
bypass of this pattern's entire Keycloak/SPIFFE/RBAC identity chain (the
eventual API-server RBAC check would be attributed to MCP's own SA, not the
real caller).

**Path A (OLSConfig field) does not exist.** A CRD investigation confirmed
there is **no** OLSConfig field to set `require_oauth` or any MCP auth
hardening -- the operator hardcodes the MCP config TOML in its own
`internal/controller/ocpmcp/assets.go`. An earlier draft of this chart
rendered a speculative `spec.ols.mcpServerSecurity` stanza on the chance such
a field existed; it does not (a structural CRD schema would silently prune
it), so that stanza and its `ols.mcpServerSecurity.*` values have been
**removed**. Do not re-add an OLSConfig-level hardening field without first
confirming it against the installed CRD (`oc explain olsconfig.spec.ols`).

**What this chart does: "Mode A" via the appServerPatch bridge.** Because the
fix cannot go through OLSConfig, it is applied out-of-band by the same bridge
that stops the operator (see "Temporary A2A bridge" below). When
`appServerPatch.mcpHardening.enabled` is true, after the operator is stopped
the bootstrap Job:

1. GETs the operator-generated MCP ConfigMap
   (`appServerPatch.mcpHardening.configMapName`, default
   `openshift-mcp-server-config` = `utils.OpenShiftMCPServerConfigCmName`),
   reading the TOML from `mcpHardening.configMapKey` (default `config.toml`
   = `OpenShiftMCPServerConfigFilename`). Both CONFIRMED from operator source
   (openshift/lightspeed-operator).
2. Idempotently sets three top-level TOML keys (replace-if-present,
   else-prepend): `require_oauth=true`, `skip_jwt_verification=true`,
   `cluster_auth_mode="passthrough"` (all configurable via
   `mcpHardening.requireOauth`/`skipJwtVerification`/`clusterAuthMode`). All
   three are absent from the operator's default `config.toml` and are
   top-level keys (CONFIRMED), so appending is safe; the script replaces in
   place if already present.
   When `mcpHardening.alertmanagerUrl` is set, it also corrects the
   operator-generated `alertmanager_url` (under
   `[toolset_configs."observability/metrics"]`) **in place** -- the operator
   ships a port that does not exist on OpenShift's `alertmanager-main` Service
   (observed live: `:9095`, but the Service only has `web=9094`/`tenancy=9092`/
   `metrics=9097`), so the MCP alerts tool hangs 30s on a blackholed connection
   and fails with `context deadline exceeded`, stalling the whole agent request.
   Unlike the three keys above this is replace-only (never prepended), so a
   section-scoped key is never relocated to the top level; if the key is absent
   the override is skipped.
3. **Only if the ConfigMap actually changed**, forces a rollout of the MCP
   Deployment (`mcpHardening.deploymentName`, default `openshift-mcp-server` =
   `utils.OpenShiftMCPServerDeploymentName`, CONFIRMED) via a
   `kubectl.kubernetes.io/restartedAt` pod-template annotation -- the same
   mechanism `kubectl rollout restart` uses (the operator is stopped, so the
   built-in Deployment controller rolls the pods on any pod-template change).
   If the ConfigMap is already hardened, no rollout is forced, so re-running on
   every ArgoCD sync does not pointlessly bounce the MCP pods. (The operator's
   own `ols.openshift.io/force-reload` annotation is only meaningful while the
   operator reconciles, which it is not here.)
4. Polls for the ConfigMap/Deployment to exist first (the operator must create
   them on a cold install), bounded by `appServerPatch.poll.*`.

This is **Mode A (passthrough)**: MCP now *requires* a bearer (tokenless ->
`401 missing_token`, killing the pod-SA fallback), and the **OpenShift API
server** validates the forwarded token -- so no CA mount or `authorization_url`
is needed. CONFIRMED from the MCP server source: the passthrough branch fires
only when `skip_jwt_verification=true` AND `authorization_url==""`, in which
case MCP does no JWT/JWKS parsing (`certificate_authority` is irrelevant) and
forwards the bearer unmodified to the kube API server, which validates Token B
(trusted audience `openshift-mcp`) + RBAC. It relies on `openshift-mcp` already
being in the API server's trusted audiences (it is) and on network isolation.
Mode A only holds because the operator is paused first (the operator owns both
the ConfigMap and the Deployment via controllerRef and would otherwise revert
them); this re-asserts on every ArgoCD sync and is idempotent.

**Mode B (future, not implemented).** A stricter "Mode B" would additionally
have MCP validate the Keycloak JWT **itself** (`authorization_url` +
`oauth_audience` + a mounted `certificate_authority`) before passthrough --
defence in depth that does not trust the network alone. That would need the
Keycloak issuer CA mounted into the MCP pod from a Secret. The existing
`identity.keycloak.caBundleSync` Secret is now optionally consumed by the
app-server's inbound OIDC trust bridge; MCP does not yet mount it.

**NetworkPolicy caveat (unchanged).** The operator's generated MCP
NetworkPolicy permits any pod in its own namespace; because NetworkPolicies
are additive (OR'd), a second policy from this chart cannot narrow it. That
remains an operator-level concern, separate from the Mode A ConfigMap fix.

**Further hardening available (not done here).** The same operator-generated
`config.toml` also hardcodes `read_only=false` and a broad `toolsets` list
(`core`, `config`, `helm`, `observability/metrics`, `kubevirt`) -- with
`denied_resources` already covering Secrets + RBAC. Tightening to
`read_only=true` and/or a narrower toolset would be the same ConfigMap and the
same patch mechanism as Mode A; it is deliberately left out of scope here.
Defence-in-depth against over-broad reads today rests on the least-privilege
Keycloak MCP RBAC (`charts/all/keycloak-oidc`), which the API server enforces
on the passed-through Token B regardless of MCP's own toolset.

**Deletion condition.** Delete `appServerPatch.mcpHardening` (and the rest of
the bridge) once the Operator/OLSConfig CRD exposes `require_oauth` (and the
other MCP auth settings) natively as supported fields.

## Temporary A2A bridge

The OLSConfig CRD has no field for a custom app-server image -- the operator
picks that via its own `--service-image` startup flag, not `OLSConfig` (see
the top-level `README.md` Phase 0) -- and no field for the A2A configuration
used by the operator-managed app-server. The current
[`luis5tb/lightspeed-service@a2a-split`](https://github.com/luis5tb/lightspeed-service/tree/a2a-split)
branch requires `A2A_ENABLED` to mount `POST /a2a`, plus
`A2A_CLUSTER_ID`/`A2A_RPC_URL` to build the A2A server and agent card. Inbound
requests use OLS's configured `k8s` or `oidc` authentication module. The old
`A2A_INBOUND_AUDIENCE` and `A2A_INBOUND_AZP` variables are not consumed by this
branch and are intentionally not rendered.

**Token exchange is optional and is not an environment toggle in a2a-split.**
The service reads `a2a.token_exchange_enabled` from its YAML config; it does
not read `A2A_TOKEN_EXCHANGE_ENABLED`. It also reuses the configured OLS
authentication module for inbound A2A, so the bridge explicitly selects OIDC
and configures its issuer/audience rather than leaving the operator's default
`k8s` module in place. When
`appServerPatch.a2a.tokenExchangeEnabled` is true (the chart default, needed
for this pattern's caller-identity-to-`openshift-mcp` flow), the patch Job
sets `ols_config.authentication_config.module: oidc`, the Keycloak issuer,
`inboundOidcAudience` (default `lightspeed-a2a`), and the exchange setting in
the explicitly named `appServerPatch.a2a.configMapName` / `configMapKey`. Set
the ConfigMap name to the object mounted by the live app-server's
`OLS_CONFIG_FILE` and verify the key/operator release before enabling the
bridge; the default name is deliberately empty because the operator-managed
ConfigMap name is not chart-owned. The patcher sets those three OIDC fields,
removes obsolete K8s-only auth fields, and sets `a2a.token_exchange_enabled`,
preserving all other OLS config (including username/metrics settings), and
grants the Job `get`/`patch` only on that one ConfigMap. It validates the
target/config before pausing the operator and restarts the app-server only
when the config content changes. Keep `configMapName` configured when toggling
exchange off so the bridge can set `token_exchange_enabled: false` while
retaining inbound OIDC.

When exchange is turned off, the bridge explicitly deletes its exchange-only
`A2A_*` environment entries and the SPIFFE CSI volume/mount; strategic-merge
omission alone would leave these stale on the Deployment.

With token exchange enabled, `A2A_KEYCLOAK_ISSUER_URL` must be HTTPS and the
SPIFFE workload API must be mounted. The chart renders only current
`a2a-split` inputs: `A2A_KEYCLOAK_CA_BUNDLE`, `A2A_EXCHANGE_AUDIENCE`,
`A2A_EXCHANGE_CLIENT_ASSERTION_TYPE`, `A2A_SPIFFE_JWT_AUDIENCE`,
`A2A_SPIFFE_ENDPOINT_SOCKET`, and its timeout/cache variables. Inbound OIDC
validation remains independent of RFC 8693 token exchange.

### Optional private-Keycloak-CA trust (`appServerPatch.oidcTrust.enabled`)

Inbound OIDC uses `httpx.AsyncClient(verify=True)`. For a private Keycloak
Route, opt in with `appServerPatch.oidcTrust.enabled: true` and
`identity.keycloak.caBundleSync.enabled: true`. The patch mounts the synced
managed-ingress CA Secret as a sibling under `/etc/certs` and points the app
back at its operator-provided writable `SSL_CERT_FILE` bundle path. At startup
the a2a-split runner combines the image's system CA roots with `.crt`/`.pem`
files in sibling `/etc/certs` directories. This retains public/system trust
while adding the private CA; certificate material stays in the synced
Kubernetes Secret, never in Git. The option requires an explicit
`appServerPatch.image.repository` and `tag` so the runner bundle-building
behavior is present. Disabling it restores the operator's original
`SSL_CERT_FILE` and removes the bridge's named Secret mount.

Because the runner builds `SSL_CERT_FILE` only once at process startup, merely
updating the mounted Secret on ingress-CA rotation is insufficient. When
`oidcTrust.enabled` is on, the CA sync script compares the Secret contents and,
only after a create or content change, stamps the app-server pod template with
a stable SHA-256 of the bundle. The Deployment controller rolls the app-server
so the runner rebuilds its trust bundle. Re-running with the same CA is a
no-op. The sync ServiceAccount receives only `get`/`patch` on the named
`appServerPatch.deploymentName` Deployment for this operation. On a cold install
the CA sync may run before that Deployment exists; it logs and skips the
rollout because the Secret is already populated and subsequently created pods
will read it at startup. A CA change after the app-server exists triggers the
rollout.

`appServerPatch` (disabled by default) works around this by patching that
Deployment directly, after the fact. An earlier version of this mechanism
re-applied the patch on a 5-minute `CronJob` schedule, on the assumption the
operator would only *occasionally* revert it -- confirmed live to be wrong:
the operator reconciles the app-server Deployment's entire container spec
from scratch on every pass (45+ Deployment generations observed within
~90 minutes, with at most two patch attempts from this chart in that
window -- the operator itself is the aggressor), far faster than any
reasonable re-assert interval could outrun. A periodic patch can never
durably win that fight.

Instead, the bootstrap Job now stops the operator from reconciling at all,
once, before patching:

- `templates/appserver-patch-rbac.yaml` -- a ServiceAccount/Role/RoleBinding
  scoped to `patch` on exactly the named Deployments
  (`appServerPatch.deploymentName` -- confirmed live as
  `lightspeed-app-server` -- `appServerPatch.operatorDeploymentName`, and, when
  `mcpHardening.enabled`, `mcpHardening.deploymentName`), plus `get` on the
  named Subscription and `get` on `clusterserviceversions` (unscoped by name:
  the installed CSV's name carries a version suffix only known at runtime, so
  RBAC `resourceNames` can't pin it). When `mcpHardening.enabled` it also
  grants `get`/`patch` on the MCP `ConfigMap` (`mcpHardening.configMapName`).
  When `appServerPatch.a2a.configMapName` is set, it separately grants
  `get`/`patch` on that exact OLS config `ConfigMap`.
  A separate `ClusterRole`/`ClusterRoleBinding` grants `get`/`patch` on the
   cluster-scoped `OLSConfig` CR (`olsConfigName`), for the optional
   post-stop annotation below.
- `templates/appserver-patch-job.yaml` -- an ArgoCD `Sync` hook Job that:
   1. **Required / empirically confirmed:** resolves the installed CSV via
      `appServerPatch.operatorSubscriptionName`'s `status.installedCSV`, waits
      for it to reach `status.phase: Succeeded`, then scales
     `appServerPatch.operatorDeploymentName` to 0 replicas. Confirmed live:
     OLM itself (not the operator) actively re-enforces its CSV-declared
     replica count while a CSV is still mid-install, logged as
     `InstallWaiting ... Deployment does not have minimum availability` --
     scaling to 0 during that window gets immediately scaled back to 1 by OLM,
     not reconciled. Once `Succeeded`, a manual scale-to-0 was separately
     confirmed stable with zero drift-correction from either the operator (not
      running) or OLM, over several minutes of direct observation -- that's the
      behavior this waits to reach before scaling down. It then GETs the
      operator Deployment until its generation is observed and both `replicas`
      and `availableReplicas` are zero in two consecutive observations. A
      missing CSV, failed scale, or unverified stop fails the hook before the
      OLSConfig annotation or any operator-managed resource patch.
   2. Only after the verified stop, requests an additional pause by annotating
      the `OLSConfig` CR (`olsConfigName`, a cluster-scoped singleton) with
      `appServerPatch.operatorManagementStateAnnotation: Unmanaged`. The target
      is the OLSConfig CR, not the CSV, but this annotation has not been verified
      against the target operator release and is not accepted as proof that
      reconciliation stopped.
  3. **Mode A MCP hardening** (only when `mcpHardening.enabled`, and only after
     the operator is stopped): patches the operator-generated MCP ConfigMap
     TOML (`require_oauth=true`/`skip_jwt_verification=true`/
     `cluster_auth_mode=passthrough`) and rolls the MCP Deployment. See the
     "MCP hardening" section above for the full rationale.
  4. Updates the named OLS config ConfigMap's `a2a.token_exchange_enabled`
      value when `appServerPatch.a2a.configMapName` is set. The bridge also
      selects the OIDC inbound auth module and configures its issuer/audience.
      This is the service-supported configuration mechanism on `a2a-split`; no
      `A2A_TOKEN_EXCHANGE_ENABLED` variable is added to the app-server. The
      target/config is checked before pausing the operator, and a stable hash
      annotation causes an app-server rollout only when config changes.
  5. Only then polls for the app-server Deployment to exist (the operator
     may not have reconciled `OLSConfig` into it yet on a fresh install) and
     applies a strategic merge patch setting the container's `image` (if
     `appServerPatch.image.repository`/`tag` are set), merging only
      `a2a-split`-supported A2A env vars built from
      `appServerPatch.a2a`/`extraEnv`, adding/removing the SPIRE CSI volume as
      exchange is enabled/disabled, and -- when
      `appServerPatch.oidcTrust.enabled` -- mounting the synced Keycloak CA
      Secret beside the operator-provided bundle `emptyDir` and setting the
      saved operator `SSL_CERT_FILE` path.
     SPIRE's workload API is a CSI volume every workload declares for
     itself, not something a mutating webhook injects (confirmed by
     checking `charts/all/acme-agent/templates/deployment.yaml`, the only
     other consumer of it in this pattern), and OLSConfig/the operator have
     no field or flag for it either -- without it,
     `a2a_auth.py`'s `SpiffeWorkloadIdentity` fails every RFC 8693 exchange
      with "SPIFFE did not issue a JWT-SVID". All of
      this merges `env` and pod `volumes` by `name`, and `volumeMounts` by
      `mountPath`, so any other operator-provided values stay intact. When a
      bridge mount is disabled, the Job reads its current mount paths and
      includes those strategic-merge keys in the deletion tombstones; omitting
      `mountPath` would not identify a `volumeMount` to delete.

   The best-effort annotation step is skipped if `olsConfigName` is empty, but
   the verified CSV-Succeeded and scale-to-zero steps are mandatory and happen
   first. Render fails if
   `operatorSubscriptionName` or `operatorDeploymentName` is empty; the Job
   fails closed if the CSV or stopped state cannot be confirmed.

  **Provisioning gate (runs before any stop; self-healing).** `Succeeded` means
  the operator is *installed*, not that it has finished reconciling the
  `OLSConfig` children. Stopping it on `Succeeded` alone races that reconcile --
  **confirmed live:** the operator was frozen (`Unmanaged` + scaled to 0) before
  it created the `lightspeed-postgres-bootstrap` Secret, so postgres stuck in
  `ContainerCreating` ("secret not found"), the app-server's `wait-for-postgres`
  init container crash-looped, `Service/lightspeed-app-server` had no endpoints,
  and `praxis-proxy` went `Degraded` with connection-refused -- while *this*
  Application still showed `Synced`/`Healthy`, because ArgoCD does not track
  operator-owned resources and `OLSConfig` has no health check. Nothing
  reconciles a stopped operator, so it never self-corrected. So before stopping,
  the Job waits for `appServerPatch.postgresDeploymentName`'s Deployment to
  report an available replica (which proves the bootstrap Secret exists and
  postgres actually started -- exactly the state that makes a stop safe). If it
  is not available, the Job first **un-pauses** the operator (`OLSConfig` ->
  `Managed`, scale to `appServerPatch.operatorReplicas`) so it can finish or
  recover a previously-frozen stack, then waits up to
  `appServerPatch.poll.provisionDeadlineSeconds`. If postgres still never comes
  up, the Job leaves the operator **running** and fails, so ArgoCD retries the
  sync instead of re-freezing a half-built stack (that is why
  `jobActiveDeadlineSeconds` must exceed `provisionDeadlineSeconds`). Set
  `postgresDeploymentName` empty to disable the gate (old, race-prone behavior).

Scaling the operator to 0 also stops it from reconciling *everything else*
it manages for this OLSConfig (the console plugin, OTEL collector, Solr/RHOKP
sidecar, the operator-managed MCP server, and OLSConfig status updates) --
acceptable for this pattern's purposes, but worth knowing if any of those
need to change later: scale `appServerPatch.operatorDeploymentName` back to
1 first, make the change, then re-run this Job (or let ArgoCD's next sync
re-run it) to scale back down.

### Two operational facts to expect

Two consequences of the above are normal, not failures -- know them before
debugging a "stuck" sync or an OLSConfig edit that seems to do nothing:

1. **A cold install may need one or more ArgoCD resyncs.** The bootstrap Job
   is a wave-4 `Sync` hook, but on a fresh cluster the operator-created
   app-server Deployment it patches (step 3) may not exist yet when the Job
   first runs -- the operator has to finish installing its CSV and reconcile
   `OLSConfig` into that Deployment first. The Job polls (bounded by
   `appServerPatch.poll.deadlineSeconds`) and will fail that attempt if the
   Deployment never appears in time; the fix is simply to let ArgoCD resync
   (it re-runs the hook), possibly more than once, until the Deployment
   exists and the patch lands. This is expected first-install behavior, not a
   misconfiguration.
2. **After bootstrap the operator is OFF, so later OLSConfig edits do not
   reconcile.** Because the Job scales
   `appServerPatch.operatorDeploymentName` to 0 (and OLM does not drift-correct
   a `Succeeded` CSV's replica count -- confirmed live), nothing is watching
   `OLSConfig` once bootstrap completes. Editing `templates/olsconfig.yaml`
   values (LLM provider, introspection, etc.) will render and sync the CR but
   produce no effect on the running app-server until the operator is running
   again. To apply such a change: scale the operator back to 1, let it
   reconcile, then re-run this Job to re-patch and scale back to 0 -- or,
   preferably, remove the whole `appServerPatch` bridge once the operator
   natively supports a custom service image and A2A config (see below).

### Inbound A2A authentication

`a2a-split` authenticates inbound `POST /a2a` requests with the OLS service's
selected `k8s` or `oidc` authentication module. This bridge selects `oidc` and
configures its issuer and audience; caller allow-listing remains the Praxis
policy's responsibility. This chart does not add an
A2A-specific inbound allow-list, and the old
`A2A_INBOUND_AUDIENCE` / `A2A_INBOUND_AZP` environment variables are not part
of this branch's interface. Enabling token exchange changes only how an
already-validated caller token is exchanged for MCP; it does not weaken inbound
OIDC validation.

### Guarded: `appServerPatch.a2a.clusterId` must be a registry key

`templates/appserver-patch-job.yaml` fails the render unless
`appServerPatch.a2a.clusterId` is a **key** of the shared
`global.olsClusters` routing registry (not merely non-empty). That id is the
`X-OLS-Cluster` value this app-server advertises; a value absent from the
registry would be silently unroutable (Praxis rejects anything not in that
one map). Keep it derived by the same algorithm the rest of the pattern uses
(`f"{host}:{port}"` with lowercase host and every `.`/`:` replaced by `-` --
see AGENTS.md's hard-won lesson on cluster-id derivation).

## Troubleshooting

### Standalone values YAML does not parse

This was caught by running `helm template` with the real
`variants/standalone/values-standalone.yaml`: an extra-indented OLS override
list item made the YAML invalid. Earlier chart tests rendered the chart with
defaults or synthetic chart-only values, so they never parsed the standalone
clusterGroup file. `tests/chart/test_ols_a2a.py::test_standalone_values_file_parses`
renders the chart with that actual file as input, and
`test_standalone_ols_application_overrides_render_enabled_bridge` applies its
OLS application overrides to catch deployment-only render errors too.

### Ingress CA rotation leaves OIDC trust stale

Static review found that the CA-sync CronJob updated its mounted Secret, but
`a2a-split` builds the `SSL_CERT_FILE` trust bundle only at process startup.
The sync now stamps a hash of the current CA on the app-server pod template
when OIDC trust is enabled, restarting it only when the CA changes. If the
Secret update succeeds but the rollout fails, the next sync retries the hash
check even though the Secret already matches. Earlier tests checked the mount
but not rotation or this partial-failure path.

**This is explicitly temporary.** Delete `appServerPatch` from `values.yaml`,
`templates/appserver-patch-*.yaml`, and `files/patch_appserver.py` entirely
once the OpenShift Lightspeed Operator/OLSConfig CRD natively supports a
custom service image and A2A configuration (i.e. once the A2A changes in the
external lightspeed-service branch are upstreamed and the operator exposes them as
real CRD fields) -- at that point this whole mechanism becomes dead weight
fighting a problem the operator itself already solves.

## Known gaps / deliberately out of scope

- `values-secret.yaml.template`'s `llm-creds-vertex` entry is commented out
  and is owned by a different workstream; it must be uncommented/populated
  for this chart's `ExternalSecret` (and `lightspeed-agentic-operator`'s) to
  resolve a real credential. Not edited here per this chart's scope.
- The operator-created `lightspeed-app-server` / `openshift-mcp-server`
  ServiceAccounts are not created by this chart (operator-managed).
  `appServer.podSelectorLabels`, `appServerPatch.deploymentName` /
  `containerName`, and `mcpHardening.podSelectorLabels` are **confirmed
  live** against lightspeed-operator pods; `ClusterSPIFFEID` and this
  chart's `NetworkPolicy` do select the real app-server pods.
- The Keycloak `lightspeed-mcp` client, its SPIFFE federation, and the
  `openshift-mcp` audience/`groups` mappers are `charts/all/keycloak-oidc`'s
  responsibility (Phase 3), not this chart's.
- The `lightspeed-operator` Subscription's package name/channel/source in
  `variants/standalone/values-standalone.yaml` are a best-effort guess
  (`stable` channel, Red Hat Operators catalog) per
  `LIGHTSPEED_IMPLEMENTATION_PLAN.md` T2.1's instructions and need
  confirmation against the live cluster's actual catalog before cutover.
- This chart's `NetworkPolicy` is additive to, and cannot narrow, the
  operator's `NetworkPolicy/lightspeed-app-server` for the same pods.
  Confirmed live: that operator policy allows ingress from
  openshift-monitoring, openshift-console, and namespaces labeled
  `network.openshift.io/policy-group=ingress` (cluster ingress). MCP's
  operator policy (`openshift-mcp-server`) allows any same-namespace pod on
  8443. Our policy adds an explicit Praxis allow; it is not sole edge
  lockdown.
