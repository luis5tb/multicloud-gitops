# Multicloud Gitops

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

[Live build status](https://validatedpatterns.io/ci/?pattern=mcgitops)

## Start Here

If you've followed a link to this repository, but are not really sure what it contains
or how to use it, head over to [Multicloud GitOps](https://validatedpatterns.io/patterns/multicloud-gitops/)
for additional context and installation instructions

For this branch's agentic pattern (ACME -> MAS -> lightspeed-hub, behind
Praxis and Keycloak), start with the diagrams:
[`docs/agentic-architecture-overview.html`](docs/agentic-architecture-overview.html)
(components and how they connect) and
[`docs/agentic-e2e-token-flow.html`](docs/agentic-e2e-token-flow.html)
(an investigation step by step, with the token at every hop). The written
references are [`agents/AUTHENTICATION.md`](agents/AUTHENTICATION.md),
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), and -- for every bug and
workaround found on a live cluster --
[`docs/LESSONS_LEARNED.md`](docs/LESSONS_LEARNED.md).

## Rationale

The goal for this pattern is to:

* Use a GitOps approach to manage hybrid and multi-cloud deployments across both public and private clouds.
* Enable cross-cluster governance and application lifecycle management.
* Securely manage secrets across the deployment.


## Deployment

Deploy from whatever branch carries this pattern's commits in your fork/remote
(the steps below call it `<your-branch>`).

> **Bugs, workarounds and "why does it behave like that".** Symptoms that
> took a live cluster to find -- Keycloak realm-import races, Praxis image
> quirks, token-exchange errors, TLS trust -- are collected in
> [`docs/LESSONS_LEARNED.md`](docs/LESSONS_LEARNED.md). Check it first when
> something fails in a way this walkthrough does not explain.

### Phase 0 — Before touching the cluster

> **Status change (Task A, `PLAN-lightspeed-hub-migration.md`):** the
> OpenShift Lightspeed (OLS) investigation backend and RHOAI have been
> **removed** from this repo (`charts/all/openshift-lightspeed-config/`,
> `charts/all/rhoai-config/`, the `a2a-lightspeed` namespace, the
> `lightspeed-operator` subscription, and all `appServerPatch` overrides).
> Everything in this README that mentions OLS, the `a2a-split` Lightspeed
> image, `X-OLS-Cluster` routing, or the `openshift-lightspeed-config`
> chart is **historical** -- kept as the record of how the pre-removal
> deployment was built, and for the teardown/rollback context in the
> migration plan. The replacement investigation backend (lightspeed-hub) is
> Task B in that plan and is not built yet. ACME now routes directly to MAS
> A2A; Praxis remains deployed as a policy gateway with no upstream.

1. Build and push the image the ACME flow needs that is
   not published upstream. Praxis itself isn't built from this repo --
   it pulls a pinned
   upstream image (see its own version caveats below). ACME is built from
   `agents/acme_agent/` here. (Historical: this step also used to build the
   A2A-enabled OpenShift Lightspeed app-server image from the external fork
   `luis5tb/lightspeed-service@a2a-split`; it is no longer deployed by this
   repo -- see the banner above.)

    ```bash
    export QUAY_ORG=<your-quay-org>
    podman login quay.io

    # --- ACME (from this repo) ---
    export ACME_TAG=$(git rev-parse --short HEAD)
    podman build -f agents/acme_agent/Containerfile \
      -t quay.io/${QUAY_ORG}/acme-agent:${ACME_TAG} agents/acme_agent
    podman push quay.io/${QUAY_ORG}/acme-agent:${ACME_TAG}
    ```
    **Multi-OIDC Support:** To accept tokens from multiple OIDC issuers (e.g.,
    Red Hat SSO production, Red Hat SSO stage, and the local Keycloak), see
    [`MULTI_OIDC_CONFIGURATION.md`](MULTI_OIDC_CONFIGURATION.md) for detailed
    configuration instructions. This configures Praxis and Keycloak to
    validate tokens from multiple trusted issuers.
    Example configuration is in
    [`MULTI_OIDC_EXAMPLE.yaml`](MULTI_OIDC_EXAMPLE.yaml).

2. Replace the `acme-agent` application's `image.repository`/`image.tag`
   overrides in
   `variants/standalone/values-standalone.yaml` with the image you just
   pushed. **These are not empty placeholders** -- they point to a
   specific prior deployment's personal `quay.io` account;
   do not assume a filled-in-looking value is already correct for your
   deployment. `praxis-proxy` needs no image edit for
   now -- it pins `ghcr.io/praxis-proxy/ai:0.4.1` by digest in
   `charts/all/praxis-proxy/values.yaml`. That image is alpha/prerelease
   upstream software and this integration isn't a Red Hat-supported Praxis
   distribution; see `LIGHTSPEED_DESIGN.md` section 7 (historical) before
   bumping `image.tag` yourself.

3. Confirm the branch you're deploying from is what's pushed and what the
   new cluster's ArgoCD Application will track:

    ```bash
    git push origin <your-branch>
    ```

### Phase 1 — Install

4. Prepare secrets:

    ```bash
    cp values-secret.yaml.template ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
    $EDITOR ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
    ```

    - Uncomment `llm-creds-vertex` and point `path:` at a real GCP
      Application Default Credentials JSON file -- OpenShift Lightspeed's
      `OLSConfig` (via `charts/all/openshift-lightspeed-config`) needs this
      for its Gemini-on-Vertex provider, reading the same Vault path into its
      own dedicated `ols-llm-creds-vertex` Secret.
    - `acme-agent-litellm` prompts interactively during `load-secrets`
      (`onMissingValue: prompt`) -- this is acme-agent's own LiteLLM key for
      its local routing decision, unrelated to OLS's Vertex credential above.
    - Optionally set `breakGlass.enabled: "true"` and
      `keycloak.adminGroupName` on the `keycloak-oidc` application now,
      before install (see Phase 3). Leave `openshiftOIDC.enabled: "false"`
      -- it can't be safely enabled until the cluster (and its real trust
      domain) exists; see Phase 3.

5. Log into the fresh cluster as cluster-admin, confirm the OpenShift
   Lightspeed Operator's real catalog coordinates, then install:

    ```bash
    oc login <new-cluster-api>

    # The lightspeed-operator Subscription in
    # variants/standalone/values-standalone.yaml is pinned to
    # lightspeed-operator.v1.1.5 (channel "stable", redhat-operators)
    # with installPlanApproval: Manual. v2.0.0 MUST be avoided: an
    # auto-upgrade to it reset the app-server to the stock image
    # (dropping the a2a-split patch) and assumes the openshift-lightspeed
    # namespace, breaking everything downstream
    # (openshift-lightspeed-config, its OLSConfig, the operator-managed
    # MCP server). Because approval is Manual, the first InstallPlan must
    # be approved by hand during install. Confirm the pinned CSV is
    # available in the live catalog before installing:
    oc get packagemanifest lightspeed-operator -o jsonpath='{.status.channels[*].name}{"\n"}{.status.defaultChannel}{"\n"}' -n openshift-marketplace
    # After the first sync, approve the pending InstallPlan so the
    # operator installs:
    # (historical: OLS backend removed -- see PLAN-lightspeed-hub-migration.md Task A)
    oc -n a2a-lightspeed patch installplan "$(oc -n a2a-lightspeed get installplan -o jsonpath='{.items[0].metadata.name}')" --type merge -p '{"spec":{"approved":true}}'

    make validate-prereq
    make validate-origin
    ./pattern.sh make install
    ```

    This brings up vault, ESO, ZTWIM, Keycloak, RHOAI, MAO, the OpenShift
    Lightspeed Operator Subscription, `openshift-lightspeed-config`,
    `keycloak-oidc`, `praxis-proxy`, and `acme-agent` in one shot. The OLS
    app-server's own Route stays disabled (OLS has no Route of its own) and
    `praxis-proxy` owns the public Route instead. The chart adds a Praxis
    ingress allow to OLS, but the operator's own additive NetworkPolicy
    allows other in-cluster sources too (see the OLS chart README). Everything
    syncs with placeholder Keycloak URLs and trust domain at first, so
    `keycloak-oidc`/`openshift-lightspeed-config`/`praxis-proxy`/`acme-agent`
    will be Synced but not functional yet. Expected -- continue to Phase 2.

    If secrets are added/modified after installation, re-run:

    ```bash
    cp values-secret.yaml.template ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
    ./pattern.sh make load-secrets
    ```

### Phase 2 — Wire up the real cluster-specific values

6. Look up the values the placeholders need:

    ```bash
    oc get zerotrustworkloadidentitymanager cluster -o jsonpath='{.spec.trustDomain}{"\n"}'
    oc whoami --show-server
    ```

    **Almost every cluster-specific value derives from one anchor.** Set
    `global.clusterBaseDomain` (top of `values-standalone.yaml`) to your cluster's
    base domain *without* the `apps.` prefix -- i.e. what `oc whoami --show-server`
    shows between `api.` and `:6443` (e.g. `ocp.<hash>.sandboxNNNN.opentlc.com`).
    From it, each chart's templates derive the Keycloak issuer
    (`https://keycloak.apps.<base>/realms/<realm>`), the console route, the SPIFFE
    trust domain (`apps.<base>`), the public A2A host
    (`openshift-lightspeed.apps.<base>`) and the `olsClusters` apiURL -- so you no
    longer hand-edit those ~9 values. The clustergroup framework also injects
    `global.localClusterDomain` (this cluster's apps domain); `clusterBaseDomain`
    falls back to it with `apps.` stripped, so on many clusters you can leave it
    empty. The realm defaults to `rca` via `global.keycloakRealm`. Each derived
    value is still explicitly overridable in the per-app `overrides` block.

    What you still set by hand:

    - `global.olsClusters` (historical; OLS backend removed -- see PLAN-lightspeed-hub-migration.md Task A) (top of `values-standalone.yaml`, shared by `acme-agent`,
      `praxis-proxy`, and `openshift-lightspeed-config`) → one entry per cluster,
      needing only its **KEY** (the `X-OLS-Cluster` id: lowercase host+port with
      `.`/`:` → `-`; see `agents/acme_agent/src/acme_agent/cluster_registry.py`)
      and its `apiURL` (`oc whoami --show-server`). The key MUST equal
      `derive_cluster_id(apiURL)` -- `openshift-lightspeed-config`'s render
      validates this and fails loud on a drift. The in-cluster
      `upstreamHost`/`upstreamPort`/`upstreamSNI` now default to the
      operator-managed app-server Service (override per entry only for a
      cross-cluster backend).
    - `openshift-lightspeed-config` (historical; chart removed) → `appServerPatch.a2a.clusterId` = the registry
      KEY above that this app-server serves (kept explicit so it stays
      unambiguous with multiple clusters; validated against the registry), plus
      `appServerPatch.image.repository`/`tag` once Phase 0 step 1's image is pushed.
      Confirm `appServerPatch.a2a.configMapName` names the operator-managed
      app-server configuration ConfigMap (`olsconfig`, key `olsconfig.yaml`
      on the inspected operator source): the bridge writes
      `a2a.token_exchange_enabled: true` there. Verify the mounted object on
      the installed operator release before enabling the patch.
    - `keycloak-oidc` → `keycloak.spiffeIdentityProvider.bundleEndpoint` (an
      in-cluster Service URL, not domain-derived) and
      `keycloak.lightspeedWorkload.namespace`/`serviceAccount` = confirm these
      match the *actual* Service Account the installed Lightspeed Operator creates
      for its app-server (default guess: `a2a-lightspeed`/`lightspeed-app-server`).
    - Leave `openshiftOIDC.enabled` `"false"` until Phase 3 regardless.
    - `keycloak-oidc` → `lightspeedRbac.namespaces` (historical; OLS backend removed -- see PLAN-lightspeed-hub-migration.md Task A) = the real namespace(s) on your cluster you want OLS's MCP investigation scoped to (e.g. `lightspeedRbac.namespaces[0]`). Left unset, this is an intentional no-op -- the chart's own default is an empty list, which renders zero Role/RoleBinding resources (acme-agent gets no MCP access anywhere, not a broken deployment) until you set this. There is no "all namespaces" default: if you explicitly want cluster-wide investigation scope (e.g. a sandbox/demo cluster, not a real environment), set `lightspeedRbac.allNamespaces: "true"` instead -- this renders a single ClusterRole/ClusterRoleBinding granting every caller in `lightspeedRbac.groupName` read access to pods/events in *every* namespace, a real widening of blast radius, not just a convenience toggle; see `charts/all/keycloak-oidc/values.yaml`'s comment on it before enabling.
    - `keycloak-oidc` → (historical; OLS backend removed) the default grant above (`pods`/`events` only) is the safe least-privilege starting point, but MCP's generic investigation tools (`resources_list`, `list_metrics`) aren't limited to pods -- they can ask about any resource kind, and each new kind hits a fresh RBAC gap one at a time. Two further opt-in, off-by-default toggles exist for a sandbox/demo cluster that wants broad investigation coverage without chasing individual resource kinds: `lightspeedRbac.useBuiltinViewRole: "true"` (binds OpenShift's built-in `view` ClusterRole -- read access to nearly everything except Secrets and RBAC objects) and `lightspeedRbac.grantMonitoringView: "true"` (additionally binds `cluster-monitoring-view`, needed separately for cluster/pod metrics since those sit behind OpenShift monitoring's own auth proxy, not the core API server). Both are real widenings of blast radius, documented in detail in `charts/all/keycloak-oidc/values.yaml` and `charts/all/keycloak-oidc/README.md`; for anything resembling a real environment, prefer the narrow default plus explicit `lightspeedRbac.extraRules` for only the resource kinds actually needed.

7. Edit `variants/standalone/values-standalone.yaml`, replacing the
   placeholders above with the real values.

8. Verify the realm import actually succeeded -- a real gotcha: on a fresh
   install, `KeycloakRealmImport` can race the `Keycloak` CR's own creation
   and fail permanently without retrying.

    ```bash
    oc get keycloakrealmimport rca -n keycloak-system -o jsonpath='{.status.conditions}'
    ```

    If you don't see `"type":"Done","status":"True"`, and instead see
    `HasErrors` mentioning `keycloaks.k8s.keycloak.org "keycloak" not found`:
    wait for the `Keycloak` CR and its pod to be `Running`, then delete the
    `KeycloakRealmImport` object and let ArgoCD recreate it -- it'll succeed
    once the dependency actually exists. Do this before moving on, since the
    realm import only ever gets one real shot (if you set
    `breakGlass.enabled`/`adminGroupName` in step 4, this is also the moment
    those get created correctly).

    Federated-jwt client authentication for `lightspeed-mcp` and
    `acme-agent` against the SPIFFE identity provider, and the RFC 8693
    token-exchange wiring for `openshift-mcp`, are fully declarative in
    `charts/all/keycloak-oidc/templates/keycloak-realm-import.yaml` -- no
    manual Admin Console step is required. `praxis-proxy` needs no client
    registration either -- when `policy.enabled: true` it authorizes callers by
    validating their bearer JWT against Keycloak's JWKS and checking the `azp`
    claim against `policy.allowedCallers`, never acting as a Keycloak client
    itself. (The `standalone` variant sets `policy.enabled: false` and runs
    Praxis router-only, delegating caller validation to OLS's multi-issuer
    inbound OIDC -- see [`agents/AUTHENTICATION.md`](agents/AUTHENTICATION.md).)

9. Commit, push, and re-sync:

    ```bash
    git add variants/standalone/values-standalone.yaml
    git commit -m "Set cluster-specific Keycloak URL, trust domain, and cluster registry"
    git push origin <your-branch>
    make argo-healthcheck
    ```

10. Verify Praxis is actually enforcing the policy:

    ```bash
    # The operator-managed app-server config should now select OIDC and
    # enable token exchange; an env var alone cannot enable exchange here.
    OLS_CONFIG=$(oc get configmap olsconfig -n a2a-lightspeed \
      -o go-template='{{ index .data "olsconfig.yaml" }}')
    printf '%s\n' "$OLS_CONFIG" | grep -Eq '^[[:space:]]*module:[[:space:]]*"?oidc"?[[:space:]]*$' &&
    printf '%s\n' "$OLS_CONFIG" | grep -Eq '^[[:space:]]*oidc_issuer_url:' &&
    printf '%s\n' "$OLS_CONFIG" | grep -Eq '^[[:space:]]*oidc_audience:[[:space:]]*"?lightspeed-a2a"?[[:space:]]*$' &&
    printf '%s\n' "$OLS_CONFIG" | grep -Eq '^[[:space:]]*token_exchange_enabled:[[:space:]]*true[[:space:]]*$' ||
      { echo 'ERROR: OLS OIDC/token exchange config is incomplete'; false; }
    oc get configmap praxis-proxy -n praxis-proxy -o yaml   # rendered policy.yaml/praxis.yaml
    oc logs -n praxis-proxy -l app.kubernetes.io/name=praxis-proxy
    oc get networkpolicy -n a2a-lightspeed -o yaml  # include the operator's additive allows

    # agent-card discovery is intentionally public, no token needed:
    curl -sf https://<praxis route host>/.well-known/agent-card.json

    # everything else must reject an unauthenticated/non-allow-listed caller:
    curl -s -o /dev/null -w '%{http_code}\n' https://<praxis route host>/  # expect 401/403
    ```

    > **Router-only mode:** the `standalone` variant sets `policy.enabled: false`
    > for `praxis-proxy`, so Praxis does **no** token verification and **no**
    > `azp` allow-list — it is a pure router keyed on `X-OLS-Cluster`. In that
    > mode the last check above returns the routed backend's response (often a
    > `200`/app error), **not** a `401/403`, because OLS — not Praxis — is the
    > validating boundary. Leave `policy.enabled: true` if you want Praxis to
    > enforce the caller allow-list. See
    > [`agents/AUTHENTICATION.md`](agents/AUTHENTICATION.md) ("Praxis policy mode").

At this point `openshift-lightspeed-config`, `praxis-proxy`, and `acme-agent`
are functional up to, but not including, the final OpenShift MCP tool call:
acme-agent's Keycloak token (plus the `X-OLS-Cluster` header naming the
target cluster) reaches the public Praxis Route, Praxis validates the token
and its `azp` allow-list (when `policy.enabled: true`; the standalone variant
runs router-only and lets OLS validate instead), selects the OLS backend the
header names, and forwards the request unchanged. OpenShift Lightspeed's A2A endpoint then
validates the OIDC token and exchanges it for an MCP-scoped token. OLS has no
separate public Route, but its operator-managed NetworkPolicy permits some
non-Praxis in-cluster sources. The exchanged token ("Token B") can mint, but
the API server rejects the final MCP call until Phase 3 switches
`Authentication/cluster` to `type: OIDC`, adding the `openshift-mcp` audience
and mapping Token B's `groups` claim to the group RBAC binds to. See
[`agents/AUTHENTICATION.md`](agents/AUTHENTICATION.md) for the full request
flow.

### Phase 3 — Enable OpenShift Native OIDC (required for the MCP tool call)

This is **not optional** if you want acme-agent's investigation to actually
reach the cluster: the exchanged MCP-scoped token is only honored by the API
server once `Authentication/cluster` switches to `type: OIDC`, which is
exactly what `openshiftOIDC.enabled=true` does. It is only "skippable" in the
narrow sense that you can stop at Phase 2 to verify Praxis enforcement and
the token exchange in isolation, with the final MCP call expected to 401.

Flipping `type: OIDC` also replaces the internal OAuth server for console/`oc
login` cluster-wide, in one step, for everyone -- getting it wrong locks out
console and CLI login cluster-wide. Full detail is in
[`charts/all/keycloak-oidc/README.md`](charts/all/keycloak-oidc/README.md);
short version:

11. Set up admin access first, unconditionally, before touching anything
    OIDC-related:

    ```bash
    # if you didn't set these in step 4 already:
    # breakGlass.enabled: "true" and keycloak.adminGroupName: rca-admins
    # on the keycloak-oidc application, then: git commit, push, make argo-healthcheck
    make admin-break-glass-kubeconfig
    oc --kubeconfig=admin-break-glass.kubeconfig whoami   # verify it works
    ```

    Move `admin-break-glass.kubeconfig` somewhere safe outside the repo. Add
    your own user to `keycloak.adminGroupName` in the Admin Console
    (Users → your user → Groups → Join Group), or set
    `keycloak.consoleAdminUser.enabled: "true"` to have the chart create a
    bootstrap admin user for you instead.

12. Get the `openshift-console` client's secret into `openshift-config`. If
    `keycloak.consoleClientSecretVaultKey` is set, this is already
    automated -- confirm the Secret exists:

    ```bash
    oc get secret openshift-console-oidc -n openshift-config
    ```

    Otherwise, create it by hand from the Admin Console's Credentials tab:

    ```bash
    oc create secret generic openshift-console-oidc -n openshift-config \
      --from-literal=clientSecret='<value from the Credentials tab>'
    ```

13. Walk the rest of the pre-flight checklist in the README (confirm the
    realm's `.well-known/openid-configuration` is reachable, etc.), then
    enable it:

    ```bash
    # openshiftOIDC.enabled: "true" on the keycloak-oidc application
    git add variants/standalone/values-standalone.yaml
    git commit -m "Enable OpenShift Native OIDC"
    git push origin <your-branch>
    make argo-healthcheck
    ```

    Verify console and `oc login` both work via Keycloak before
    disconnecting your current session. If anything's wrong:

    ```bash
    oc --kubeconfig=admin-break-glass.kubeconfig patch authentication.config.openshift.io cluster \
      --type=merge -p '{"spec":{"type":"","oidcProviders":null}}'
    ```

    restores the internal OAuth server immediately.

### Phase 4 (Optional) — Enable MAS A2A Routing

By default ACME delegates straight to Praxis (the hub gateway) and the MAS A2A
front door stays dormant. To route through MAS A2A instead, follow the chart
guide [`charts/all/mao/mas-a2a/README.md`](charts/all/mao/mas-a2a/README.md):
safe-activation prerequisites, blueprint preparation, Keycloak audience and
principal allow-list, private-Route CA trust, and the deploy/verify steps.

In short, activation needs four things, all in
`variants/standalone/values-standalone.yaml`:

1. a persisted blueprint (`a2a.blueprintId`) created with
   `./setup-mas-blueprint.sh multi-cluster` (or `simple` for
   Ask-Red-Hat-only); the script prints the ID,
2. ACME's Keycloak service-account `sub` (an opaque UUID, not its SPIFFE ID)
   in `a2a.allowedPrincipals`, read from the Keycloak Admin API,
3. `a2a.enabled: "true"` with `a2a.publicBaseUrl` set to the MAS A2A Route,
4. the image pinned to the A2A-enabled build (`uie-mas-hosted` branch
   `a2a-inbound-review-pr8`) for `mao-api`, `mao-worker` and `mao-a2a` alike,
   built with the same tag.

Then point ACME at it by setting `a2a.remoteAgents[0].endpoint` to the MAS A2A
base URL (default host `mao-a2a-mas-a2a-<namespace>.apps.<cluster>`, not
`mas-a2a.apps.<cluster>`); comment that override out to fall back to Praxis.

LLM choice for the orchestrator: `LLM_PROVIDER=vertex` (Gemini on Vertex, using
the same ADC as the rest of the pattern) is the recommended path;
`setup-mas-blueprint.sh` documents the LiteLLM/OpenAI alternative and its
gpt-oss tool-call caveat.

## Accessing Argo CD

With `global.singleArgoCD: true` (this pattern's default), the hub Argo CD
instance is always named `vp-gitops`, in the `vp-gitops` namespace --
regardless of the pattern name -- so this is stable across clusters:

```bash
export ARGOCD_URL="https://$(oc get route vp-gitops-server -n vp-gitops -o jsonpath='{.spec.host}')"
export ARGOCD_PASSWORD=$(oc get secret vp-gitops-cluster -n vp-gitops -o jsonpath='{.data.admin\.password}' | base64 -d)
echo "$ARGOCD_URL"
echo "$ARGOCD_PASSWORD"
```

Username is `admin`.

## Accessing Keycloak

Once the `keycloak` application has synced (Phase 1), get the realm's Admin
Console URL and the operator-generated admin credentials (populated from
Vault via `keycloak.adminUser.passwordVaultKey`):

```bash
export KEYCLOAK_URL="https://keycloak.<trustDomain>"   # trustDomain from Phase 2, step 6
oc get secret keycloak-admin-user -n keycloak-system -o jsonpath='{.data.username}' | base64 -d; echo
oc get secret keycloak-admin-user -n keycloak-system -o jsonpath='{.data.password}' | base64 -d; echo
```

Open `$KEYCLOAK_URL` and select the `rca` realm to inspect the clients,
groups, and identity provider `keycloak-realm-import.yaml` declares.

## OpenShift Lightspeed (HISTORICAL -- removed)

> The OLS backend described in this section was removed from this repo (see
> the Phase 0 banner and `PLAN-lightspeed-hub-migration.md` Task A). The text
> below is kept as the historical record of the pre-removal deployment; none
> of it applies to a fresh install of this branch.

ACME delegates OpenShift questions and incident investigations to OpenShift
Lightspeed, deployed via the OpenShift Lightspeed Operator and configured by
this pattern's own `charts/all/openshift-lightspeed-config` chart (an
`OLSConfig` using Gemini on Vertex AI, with its operator-managed,
introspection-enabled OpenShift MCP server). The chart itself is gone; its
open MCP-hardening questions are carried in the lessons doc, and
[`LIGHTSPEED_DESIGN.md`](LIGHTSPEED_DESIGN.md)/
[`LIGHTSPEED_IMPLEMENTATION_PLAN.md`](LIGHTSPEED_IMPLEMENTATION_PLAN.md) for
the full design and task breakdown. The A2A integration itself lives in the
external fork
[`luis5tb/lightspeed-service` branch `a2a-split`](https://github.com/luis5tb/lightspeed-service/tree/a2a-split)
(see Phase 0 for how to build and push that image), since upstream OpenShift
Lightspeed doesn't speak A2A yet.

OpenShift Lightspeed's A2A endpoint is fronted by the Praxis AI gateway and
its embedded Praxis Policy Engine, which also routes each request to the
right OLS backend based on the `X-OLS-Cluster` header ACME sends (see
`global.olsClusters` in `variants/standalone/values-standalone.yaml`). See
[`agents/AUTHENTICATION.md`](agents/AUTHENTICATION.md) for the full request
flow and [`charts/all/praxis-proxy/README.md`](charts/all/praxis-proxy/README.md)
for deployment, policy, and upstream maturity details.

## MAO Configuration

The standalone variant registers the MAO runtime from
[`uie-mas-hosted`](https://gitlab.cee.redhat.com/ai_tools/uie-mas-hosted) into
the `mao` namespace. It is split into six Argo CD applications (one dormant)
so the components can be upgraded independently:

- MongoDB, with persistent storage
- Redis, for event streaming and HITL overrides
- Temporal, for workflow execution
- `mas-worker`, the Temporal worker
- `mas-api`, exposed through an OpenShift Route
- `mas-a2a`, registered without any resources until explicitly enabled

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

### Optional inbound MAS A2A (registered, not active)

`mao-a2a` points to [`charts/all/mao/mas-a2a`](charts/all/mao/mas-a2a), but the
chart's `a2a.enabled: false` default renders **no resources**, and it stays
off until every prerequisite in that chart's README (blueprint, principal
allow-list, audience, image pin) is satisfied. Do not turn it on because the
Argo CD Application looks Synced. Activation is Phase 4 above; the chart guide
is the detailed procedure.

### Multi-cluster routing with dynamic headers

**When to use:** After completing Phase 4 Step 2 (creating a blueprint), optionally
use this multi-cluster orchestrator blueprint instead of a simple pass-through
blueprint if you have multiple OLS clusters registered in `global.olsClusters`.

**Prerequisites:**
- Multiple clusters registered in `global.olsClusters` with different IDs
- MAS image with per-task header forwarding (uie-mas-hosted `a2a-inbound-review-pr1` or later)

**MAS orchestrator + Praxis routing** enables investigating multiple OpenShift
clusters through a single interface. The orchestrator analyzes the user's question
to determine which cluster they're asking about, then dynamically routes to the
correct OLS backend via Praxis's X-OLS-Cluster header.

**How it works:**

1. **User**: "What pods are failing in cluster-a?"
2. **Orchestrator**: Parses prompt → extracts cluster ID → delegates with headers
3. **Praxis**: Receives X-OLS-Cluster header → routes to cluster-a OLS
4. **OLS cluster-a**: Investigates → responds
5. **Orchestrator**: Synthesizes final answer

**Blueprint configuration:**

The orchestrator uses the `delegate_task` tool's `data` parameter to pass routing
headers:

```yaml
delegate_task(
  dst_uid="praxis_investigator",
  content="What pods are failing?",
  work_item_id="investigate",
  data={"headers": {"X-OLS-Cluster": "api-ocp-cluster-a-com-6443"}}
)
```

**Example blueprint:** See [`docs/blueprint-multi-cluster-routing.yaml`](docs/blueprint-multi-cluster-routing.yaml)
for a complete multi-cluster orchestrator that:
- Identifies cluster from user prompt
- Delegates to Praxis with X-OLS-Cluster header
- Falls back to asking user which cluster if ambiguous

**uie-mas-hosted changes:** The `a2a_agent_node` reads `task.data["headers"]`
and passes them to `a2a_provider.set_call_headers()`, so they are sent with
that task's outbound A2A calls only and replaced on the next task. Set the
header on every delegation (as the blueprints above do); it does not carry over.
Because the values can come from model tool arguments, `Authorization`,
`Proxy-Authorization`, `Cookie`, `Host` and `Content-Length` are dropped, and
the node's configured or delegated credentials always take precedence.
`X-OLS-Cluster` is forwarded unchanged.
