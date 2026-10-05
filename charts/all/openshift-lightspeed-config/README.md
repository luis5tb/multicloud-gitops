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
-- and has been removed as a cited source throughout this chart. Nothing in
this chart or its templates should be read as "confirmed via upstream
source"; everything not explicitly stated as a frozen interface decision in
`LIGHTSPEED_IMPLEMENTATION_PLAN.md` is this chart's best-effort assumption
and must be verified against the actually-installed operator (`oc explain
olsconfig.spec`, the installed CRD's OpenAPI schema, and
`oc get pods -n openshift-lightspeed --show-labels`) before being trusted in
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
  this copy targets a Secret, in case the speculative
  `mcpServerSecurity.caSecretRef` option below turns out to be real and
  need one.
- `templates/networkpolicy.yaml` -- best-effort ingress restriction for the
  (unconfirmed-label) operator-managed app-server pods to same-namespace
  traffic and the Praxis gateway.
- `templates/cluster-spiffe-id.yaml` -- SPIRE `ClusterSPIFFEID` registration
  for the operator-created `lightspeed-app-server` ServiceAccount, disabled
  by default (trust domain is installation-specific).

## MCP hardening (T2.3): open, unresolved gap

`LIGHTSPEED_DESIGN.md` section 5 is explicit that the inspected
upstream `openshift/lightspeed-operator` (commit
`fd157f53b5cd3fa353cdeb44e9dbcd1fcfd6a1a6`) generates an MCP server
configuration that does **not** set `require_oauth`, and that the MCP
binary's own default for that flag is `false` -- i.e. no bearer is required
unless the operator is patched or reconfigured. The plan explicitly frames
closing this gap as requiring "an explicit operator-extension/fork or proven
supported configuration capability, not an Argo CD override of an
operator-reconciled ConfigMap," and separately notes the operator's
generated MCP `NetworkPolicy` permits any pod in its own namespace -- which a
second, additive NetworkPolicy from this chart cannot narrow, since
NetworkPolicies targeting the same pods are OR'd together, not intersected.

**This chart does not have a confirmed solution to that gap.** It does not
attempt to fight the operator's reconciled MCP ConfigMap or NetworkPolicy
with a competing resource, per the plan's explicit guidance.

What it does instead: `templates/olsconfig.yaml` can optionally render a
`spec.ols.mcpServerSecurity` stanza (`authorizationURL`, `oauthAudience`,
`caSecretRef`, `toolsets`, `allowPrometheusMetrics`) gated behind
`ols.mcpServerSecurity.enabled` in `values.yaml`, **defaulted to `false`**.
This is included purely as a convenience in case a future or different build
of the Lightspeed Operator turns out to support a supported hardening field
shaped like this -- **it is not based on any confirmed evidence that such a
field exists in the operator this pattern will actually install.** Do not
enable it against a real cluster without first checking the installed
operator's actual `OLSConfig` CRD schema (`oc explain olsconfig.spec.ols`, or
inspect the CRD's OpenAPI schema directly for a `mcpServerSecurity`
property). If the field does not exist, the Kubernetes API will either
reject the CR (if the CRD uses strict/non-structural validation) or silently
prune the unknown field (the common case for structural schemas), and either
way this chart provides no MCP hardening in that scenario.

**Until that is confirmed, treat MCP hardening as an open operational gap
requiring an operator-level fix, exactly as `LIGHTSPEED_DESIGN.md`
section 5 describes**, not something this chart has solved. Required bearer
auth (`require_oauth`), independent OIDC verification, and/or a genuinely
restrictive MCP NetworkPolicy need either a supported operator configuration
surface (not yet confirmed to exist) or a reviewed operator fork/patch,
which is explicitly called out in the plan as a separate, blocking piece of
work -- it is out of scope for this chart alone to deliver.

## Temporary A2A bridge

The OLSConfig CRD has no field for a custom app-server image -- the operator
picks that via its own `--service-image` startup flag, not `OLSConfig` (see
the top-level `README.md` Phase 0) -- and no field for the `A2A_*`
environment variables `vendor/lightspeed-service/ols/app/endpoints/a2a_auth.py`
requires to serve A2A requests at all (`A2A_KEYCLOAK_ISSUER_URL`,
`A2A_CLUSTER_ID`, `A2A_RPC_URL` are required; it raises
`A2AConfigurationError` and never starts the A2A endpoint without them).
Both are properties of the operator-managed app-server Deployment, which
this chart does not own and which the operator continuously reconciles.

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
  (`appServerPatch.deploymentName` -- an unconfirmed-guess name like
  `appServer.podSelectorLabels` above, verify against `oc get deployment -n
  <namespace>` -- and `appServerPatch.operatorDeploymentName`), plus `get` on
  the named Subscription and `get`/`patch` on `clusterserviceversions`
  (unscoped by name: the installed CSV's name carries a version suffix only
  known at runtime, so RBAC `resourceNames` can't pin it).
- `templates/appserver-patch-job.yaml` -- an ArgoCD `Sync` hook Job that:
  1. Resolves the installed CSV via `appServerPatch.operatorSubscriptionName`'s
     `status.installedCSV` and best-effort annotates it with
     `appServerPatch.operatorManagementStateAnnotation: Unmanaged`. This
     operator's own binary was checked directly (`strings` on the extracted
     controller-manager binary) and never references that annotation key --
     it is **not confirmed to do anything** here, kept only as
     defense-in-depth in case OLM itself honors it for CSV-owned deployments.
  2. Scales `appServerPatch.operatorDeploymentName` to 0 replicas. This part
     **is** confirmed live: observed stable at `spec.replicas=0` with zero
     drift-correction, from either the operator itself (not running) or OLM,
     over several minutes of direct observation.
  3. Only then polls for the app-server Deployment to exist (the operator
     may not have reconciled `OLSConfig` into it yet on a fresh install) and
     applies a strategic merge patch setting the container's `image` (if
     `appServerPatch.image.repository`/`tag` are set) and merging the
     `A2A_*` env vars built from `appServerPatch.a2a`/`extraEnv` -- by name,
     so any other env var or container the operator set is left alone.

  Steps 1-2 are skipped entirely if `appServerPatch.operatorSubscriptionName`/
  `operatorDeploymentName` are left empty, falling back to the old
  patch-only behavior (which will not persist).

Scaling the operator to 0 also stops it from reconciling *everything else*
it manages for this OLSConfig (the console plugin, OTEL collector, Solr/RHOKP
sidecar, the operator-managed MCP server, and OLSConfig status updates) --
acceptable for this pattern's purposes, but worth knowing if any of those
need to change later: scale `appServerPatch.operatorDeploymentName` back to
1 first, make the change, then re-run this Job (or let ArgoCD's next sync
re-run it) to scale back down.

**This is explicitly temporary.** Delete `appServerPatch` from `values.yaml`,
`templates/appserver-patch-*.yaml`, and `files/patch_appserver.py` entirely
once the OpenShift Lightspeed Operator/OLSConfig CRD natively supports a
custom service image and A2A configuration (i.e. once the A2A changes in
`vendor/lightspeed-service` are upstreamed and the operator exposes them as
real CRD fields) -- at that point this whole mechanism becomes dead weight
fighting a problem the operator itself already solves.

## Known gaps / deliberately out of scope

- `values-secret.yaml.template`'s `llm-creds-vertex` entry is commented out
  and is owned by a different workstream; it must be uncommented/populated
  for this chart's `ExternalSecret` (and `lightspeed-agentic-operator`'s) to
  resolve a real credential. Not edited here per this chart's scope.
- The operator-created `lightspeed-app-server` / `openshift-mcp-server`
  ServiceAccounts are not created by this chart (operator-managed); the
  `ClusterSPIFFEID` and `NetworkPolicy` templates reference them/their pods
  by an unconfirmed guess at name/labels only (see `values.yaml`'s
  `appServer.podSelectorLabels`), and are disabled or best-effort until that
  identity work (a separate workstream, `vendor/lightspeed-service` --
  the *service* source import named in the migration plan, distinct from
  the untrusted operator checkout discussed above) lands.
- The Keycloak `lightspeed-mcp` client, its SPIFFE federation, and the
  `openshift-mcp` audience/`groups` mappers are `charts/all/keycloak-oidc`'s
  responsibility (Phase 3), not this chart's.
- The `lightspeed-operator` Subscription's package name/channel/source in
  `variants/standalone/values-standalone.yaml` are a best-effort guess
  (`stable` channel, Red Hat Operators catalog) per
  `LIGHTSPEED_IMPLEMENTATION_PLAN.md` T2.1's instructions and need
  confirmation against the live cluster's actual catalog before cutover.
- This chart's `NetworkPolicy` is additive to, and cannot narrow, whatever
  NetworkPolicy (if any) the operator creates for the app-server pods on its
  own; this chart did not inspect that policy's contents.
