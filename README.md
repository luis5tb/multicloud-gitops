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


## Deployment

Deploy from whatever branch carries this pattern's commits in your fork/remote
(the steps below call it `<your-branch>`).

### Phase 0 — Before touching the cluster

1. Build and push the two images the ACME/OLS investigation flow needs that
   are not published upstream. Praxis itself isn't built from this repo --
   it pulls a pinned
   upstream image (see its own version caveats below). ACME is built from
   `agents/acme_agent/` here. The A2A-enabled OpenShift Lightspeed
   app-server image is built from the external fork
   [`luis5tb/lightspeed-service`](https://github.com/luis5tb/lightspeed-service)
   on branch [`a2a-split`](https://github.com/luis5tb/lightspeed-service/tree/a2a-split)
   (tip at time of writing: `4d85f430`, which separates reusable OIDC
   authentication, A2A transport, and optional token exchange)
   -- this pattern no longer vendors that tree.

    ```bash
    export QUAY_ORG=<your-quay-org>
    podman login quay.io

    # --- ACME (from this repo) ---
    export ACME_TAG=$(git rev-parse --short HEAD)
    podman build -f agents/acme_agent/Containerfile \
      -t quay.io/${QUAY_ORG}/acme-agent:${ACME_TAG} agents/acme_agent
    podman push quay.io/${QUAY_ORG}/acme-agent:${ACME_TAG}

    # --- OLS + A2A (from luis5tb/lightspeed-service@a2a-split) ---
    # Clone beside this repo (or wherever you prefer); pin a SHA for
    # reproducibility if you want an immutable image tag.
    git clone --branch a2a-split --single-branch \
      https://github.com/luis5tb/lightspeed-service.git /tmp/opencode/lightspeed-service-a2a-split
    cd /tmp/opencode/lightspeed-service-a2a-split
    export OLS_TAG=$(git rev-parse --short HEAD)

    # Prefer UBI base images so the build does not need a RHEL
    # entitlement (registry.redhat.io/rhel9/python-312 requires one for
    # `dnf install`). Same Python 3.12 line the Containerfile defaults to.
    podman build -f Containerfile \
      --build-arg BUILDER_BASE_IMAGE=registry.access.redhat.com/ubi9/python-312:9.6 \
      --build-arg RUNTIME_BASE_IMAGE=registry.access.redhat.com/ubi9/python-312-minimal:9.6 \
      -t quay.io/${QUAY_ORG}/openshift-lightspeed:${OLS_TAG} .
    podman push quay.io/${QUAY_ORG}/openshift-lightspeed:${OLS_TAG}
    cd -
    ```

    The OpenShift Lightspeed Operator picks the app-server image via its own
    `--service-image` startup flag, not an `OLSConfig` field, and has no CRD
    field for the `A2A_*` environment variables the A2A-enabled image needs
    either. `charts/all/openshift-lightspeed-config`'s `appServerPatch`
    (disabled by default) is a **temporary** bridge for both: it patches the
    operator-managed app-server Deployment directly, after the fact, with
    this image and the required `A2A_*` settings (cluster ID, public RPC URL,
    and the Keycloak/SPIFFE exchange settings) --
    see that chart's README.md ("Temporary A2A bridge") for how to set it,
    and why it also scales the operator itself to 0 replicas first (a
    periodic re-assert against the operator's own reconcile loop turned out
    to be a losing fight -- confirmed live). Delete it once the operator/
    OLSConfig CRD natively supports a custom service image and A2A
    configuration.

    **Important for `a2a-split`:** upstream A2A token exchange is optional
    and defaults to disabled. This pattern must enable it explicitly alongside
    OIDC inbound validation; otherwise OLS sends the caller's original token
    to MCP instead of exchanging it for an `openshift-mcp`-audience token.
    OLS now uses its configured OIDC authentication module for both A2A and
    the stock REST API; clients of `/v1/*` that previously used OpenShift
    ServiceAccount tokens must be retested with OIDC credentials. Praxis
    continues to enforce the `azp` caller allow-list; OLS no longer has a
    separate A2A `azp` allow-list. See
    [`agents/AUTHENTICATION.md`](agents/AUTHENTICATION.md). The OIDC module
    validates HTTPS issuer discovery/JWKS with **HTTPX/certifi roots** by
    default; the exchange-specific
    `A2A_KEYCLOAK_CA_BUNDLE` does not add trust for inbound OIDC validation.
    A private Keycloak Route CA requires `SSL_CERT_FILE` to point to a bundle
    containing **both** public roots and the private CA. Adding it only to
    the OS trust store is not sufficient for HTTPX's default certifi context.
    The standalone override enables `appServerPatch.oidcTrust.enabled`: the
    bridge mounts the synced managed-ingress CA Secret under `/etc/certs`, and
    the `a2a-split` runner rebuilds its operator-provided `SSL_CERT_FILE`
    bundle from public roots plus sibling certificates. The patch Job fails
    before pausing the operator if that writable path is absent on the
    installed release; verify it before cutover. The CA-sync CronJob restarts
    the app-server when the synced CA changes so the startup bundle refreshes
    (see the OLS chart README).

    **Multi-OIDC Support:** To accept tokens from multiple OIDC issuers (e.g.,
    Red Hat SSO production, Red Hat SSO stage, and the local Keycloak), see
    [`MULTI_OIDC_CONFIGURATION.md`](MULTI_OIDC_CONFIGURATION.md) for detailed
    configuration instructions. This configures Praxis, OLS, and Keycloak to
    validate tokens from multiple trusted issuers while still performing token
    exchange through the local Keycloak instance. Example configuration is in
    [`MULTI_OIDC_EXAMPLE.yaml`](MULTI_OIDC_EXAMPLE.yaml).

2. Replace the `acme-agent` application's `image.repository`/`image.tag`
   **and** `openshift-lightspeed-config`'s `appServerPatch.image.repository`/
   `appServerPatch.image.tag` overrides in
   `variants/standalone/values-standalone.yaml` with the images you just
   pushed. **These are not empty placeholders** -- they point to a
   specific prior deployment's personal `quay.io` account, and the OLS tag
   is the new source SHA, not proof that its image has been published;
   do not assume a filled-in-looking value is already correct for your
   deployment. `praxis-proxy` needs no image edit for
   now -- it pins `ghcr.io/praxis-proxy/ai:0.4.1` by digest in
   `charts/all/praxis-proxy/values.yaml`. That image is alpha/prerelease
   upstream software and this integration isn't a Red Hat-supported Praxis
   distribution; a newer Praxis Core release (v0.7.2) adds the header-based
   routing and per-upstream TLS this pattern needs, but as of this writing no
   published `praxis-proxy/ai` image embeds it yet -- see
   `LIGHTSPEED_DESIGN.md` section 7 before bumping `image.tag`
   yourself. `openshift-lightspeed-config`'s own `OLSConfig` doesn't carry an
   image reference at all; see step 1 above for how the custom OLS image is
   actually wired in (`appServerPatch`, a temporary post-reconcile patch).

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

    # The lightspeed-operator Subscription's package/channel/source in
    # variants/standalone/values-standalone.yaml are best guesses
    # ("stable"/redhat-operators), not yet confirmed against a live
    # catalog -- a wrong value here leaves the Subscription stuck
    # (CatalogSourcesUnhealthy/ResolutionFailed) and nothing downstream
    # of it (openshift-lightspeed-config, its OLSConfig, the
    # operator-managed MCP server) ever comes up. Check before installing:
    oc get packagemanifest lightspeed-operator -o jsonpath='{.status.channels[*].name}{"\n"}{.status.defaultChannel}{"\n"}' -n openshift-marketplace
    # If the package/channel/source differ from values-standalone.yaml's
    # lightspeed-operator subscription entry, fix it there now, before
    # the first sync.

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

    - `global.olsClusters` (top of `values-standalone.yaml`, shared by `acme-agent`,
      `praxis-proxy`, and `openshift-lightspeed-config`) → one entry per cluster,
      needing only its **KEY** (the `X-OLS-Cluster` id: lowercase host+port with
      `.`/`:` → `-`; see `agents/acme_agent/src/acme_agent/cluster_registry.py`)
      and its `apiURL` (`oc whoami --show-server`). The key MUST equal
      `derive_cluster_id(apiURL)` -- `openshift-lightspeed-config`'s render
      validates this and fails loud on a drift. The in-cluster
      `upstreamHost`/`upstreamPort`/`upstreamSNI` now default to the
      operator-managed app-server Service (override per entry only for a
      cross-cluster backend).
    - `openshift-lightspeed-config` → `appServerPatch.a2a.clusterId` = the registry
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
    - `keycloak-oidc` → `lightspeedRbac.namespaces` = the real namespace(s) on your cluster you want OLS's MCP investigation scoped to (e.g. `lightspeedRbac.namespaces[0]`). Left unset, this is an intentional no-op -- the chart's own default is an empty list, which renders zero Role/RoleBinding resources (acme-agent gets no MCP access anywhere, not a broken deployment) until you set this. There is no "all namespaces" default: if you explicitly want cluster-wide investigation scope (e.g. a sandbox/demo cluster, not a real environment), set `lightspeedRbac.allNamespaces: "true"` instead -- this renders a single ClusterRole/ClusterRoleBinding granting every caller in `lightspeedRbac.groupName` read access to pods/events in *every* namespace, a real widening of blast radius, not just a convenience toggle; see `charts/all/keycloak-oidc/values.yaml`'s comment on it before enabling.
    - `keycloak-oidc` → the default grant above (`pods`/`events` only) is the safe least-privilege starting point, but MCP's generic investigation tools (`resources_list`, `list_metrics`) aren't limited to pods -- they can ask about any resource kind, and each new kind hits a fresh RBAC gap one at a time. Two further opt-in, off-by-default toggles exist for a sandbox/demo cluster that wants broad investigation coverage without chasing individual resource kinds: `lightspeedRbac.useBuiltinViewRole: "true"` (binds OpenShift's built-in `view` ClusterRole -- read access to nearly everything except Secrets and RBAC objects) and `lightspeedRbac.grantMonitoringView: "true"` (additionally binds `cluster-monitoring-view`, needed separately for cluster/pod metrics since those sit behind OpenShift monitoring's own auth proxy, not the core API server). Both are real widenings of blast radius, documented in detail in `charts/all/keycloak-oidc/values.yaml` and `charts/all/keycloak-oidc/README.md`; for anything resembling a real environment, prefer the narrow default plus explicit `lightspeedRbac.extraRules` for only the resource kinds actually needed.

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
    registration either -- it authorizes callers by validating their bearer
    JWT against Keycloak's JWKS and checking the `azp` claim against
    `policy.allowedCallers`, never acting as a Keycloak client itself.

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

At this point `openshift-lightspeed-config`, `praxis-proxy`, and `acme-agent`
are functional up to, but not including, the final OpenShift MCP tool call:
acme-agent's Keycloak token (plus the `X-OLS-Cluster` header naming the
target cluster) reaches the public Praxis Route, Praxis validates the token
and its `azp` allow-list, selects the OLS backend the header names, and
forwards the request unchanged. OpenShift Lightspeed's A2A endpoint then
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

By default, ACME routes through Praxis → OLS. To route through MAS A2A instead
(ACME → MAS A2A → OLS), follow these steps:

**Step 1: Obtain ACME Agent's Token Subject**

ACME authenticates with Keycloak using its service account. Get the `sub` claim:

```bash
# Method 1: From Keycloak Admin Console
# Navigate to: Realm 'rca' → Groups → 'acme-agent-rca' → Members
# Open 'service-account-acme-agent' and copy its ID (UUID).
# The chart adds this service-account user to the acme-agent-rca group.
# An empty roles list is expected; no roles need to be assigned.

# Method 2: From ACME logs (if ACME has made at least one request)
oc logs -n acme-agent deployment/acme-agent | grep acme_audit | jq -r .token_a_subject | head -1
```

Use the member's user ID as the `sub` in `a2a.allowedPrincipals`—not the
username or the group's ID. If `keycloak.acmeGroupName` is overridden, use that
configured group instead of `acme-agent-rca`.

**Step 2: Create MAS Blueprint**

Use the `setup-mas-blueprint.sh` script to register inventory resources and create
a multi-cluster blueprint. The script handles:

1. Registering LLM resource for orchestration
2. Registering Orchestrator Node (references LLM, contains routing logic)
3. Registering A2A Agent Node (OLS through Praxis)
4. Creating blueprint with orchestrator feedback loop
5. Saving blueprint and outputting the ID

**For multi-cluster with orchestration:**

```bash
# Set LiteLLM credentials (or OPENAI_API_KEY for direct OpenAI)
export LITELLM_API_KEY="your-litellm-api-key"
export LITELLM_API_BASE="https://maas-rhdp.apps.maas.redhatworkshops.io/v1"
export LITELLM_MODEL="gpt-oss-20b"

# Run the script
./setup-mas-blueprint.sh multi-cluster
```

**For simple single-cluster (no orchestrator):**

```bash
./setup-mas-blueprint.sh simple
```

The script will output the `BLUEPRINT_ID` at the end. Save this for Step 3.

**Important:** When `agentCard.enabled: true` (enabled by default in
`values-standalone.yaml` for multi-cluster deployments), Praxis serves a static
agent card with Praxis's own endpoint as `rpc_url`. This ensures MAS A2A routes
through Praxis for actual calls (allowing `X-OLS-Cluster` header routing) instead
of bypassing Praxis by calling OLS directly.

**Blueprint Architecture:**

The multi-cluster blueprint creates an orchestrator-based workflow:
- **User Question** → **Orchestrator** (analyzes question, decides routing)
- **Orchestrator** → **OLS Agent** (investigates cluster) → back to **Orchestrator**
- **Orchestrator** decides: loop for more info OR finalize answer
- **Orchestrator** → **Final Answer**

The orchestrator uses the registered LLM to make routing decisions and can loop
back after investigation to gather more information before finalizing the answer.

**Step 3: Enable MAS A2A**

In `variants/standalone/values-standalone.yaml`, under the `mao-a2a` application,
uncomment the PHASE 2 overrides and fill in the values.

**For production (with multi-issuer OIDC authentication):**

```yaml
mao-a2a:
  overrides:
    # ... existing config ...
    
    # PHASE 2: Enable MAS A2A
    - name: a2a.enabled
      value: "true"
    - name: a2a.blueprintId
      value: "<blueprint-id-from-step-2>"  # Replace with $BLUEPRINT_ID from script
    - name: a2a.publicBaseUrl
      value: "https://mas-a2a-mao.apps.<your-cluster>"  # Replace with your cluster domain
    - name: a2a.allowedPrincipals
      value: '["<acme-principal-sub>"]'  # Replace with ACME agent's 'sub' claim from Red Hat SSO
    - name: route.enabled
      value: "true"
    # route.host is optional - auto-derives from global.clusterBaseDomain
    
    # Multi-issuer OIDC (already configured in values-standalone.yaml)
    - name: identity.oidcIssuers[0].issuer_url
      value: https://sso.redhat.com/auth/realms/redhat-external
    # ... (rest of oidcIssuers configuration)
```

**For dev/testing (simplified mode, no real authentication):**

```yaml
mao-a2a:
  overrides:
    # ... existing config ...
    
    # PHASE 2: Enable MAS A2A (DEV MODE)
    - name: a2a.enabled
      value: "true"
    - name: a2a.blueprintId
      value: "<blueprint-id-from-step-2>"
    - name: a2a.publicBaseUrl
      value: "https://mas-a2a-mao.apps.<your-cluster>"
    - name: a2a.allowedPrincipals
      value: '["dev"]'  # Accept any token with username "dev"
    - name: identity.providerMode
      value: dev  # Bypass OIDC validation for testing
    - name: route.enabled
      value: "true"
```

**Notes:**
- `route.host` is **optional** and derives from `global.clusterBaseDomain` if not set
- `identity.oidcIssuers` (production) and `identity.providerMode: dev` (testing) are **mutually exclusive**
- The default `values-standalone.yaml` uses multi-issuer OIDC for production readiness
- For initial testing, dev mode is simpler but provides no real authentication

Commit and push the changes. Wait for ArgoCD to sync the `mao-a2a` application.

**Step 4: Verify MAS A2A**

```bash
# Check MAS A2A pod is running
oc get pods -n mao -l app.kubernetes.io/name=mas-a2a

# Test agent card endpoint
MAS_A2A_URL="https://$(oc get route -n mao -l app.kubernetes.io/name=mas-a2a -o jsonpath='{.items[0].spec.host}')"
curl "${MAS_A2A_URL}/.well-known/agent-card.json" | jq .
```

**Step 5: Route ACME to MAS A2A**

In `variants/standalone/values-standalone.yaml`, under the `acme-agent` application,
uncomment the PHASE 3 override:

```yaml
acme-agent:
  overrides:
    # ... existing config ...
    
    # PHASE 3: Route to MAS A2A (uncomment)
    # name must be a valid Python identifier (underscores, not hyphens)
    - name: a2a.remoteAgents[0].name
      value: mas_orchestrator
    - name: a2a.remoteAgents[0].description
      value: "MAS A2A Orchestrator for OpenShift Lightspeed"
    - name: a2a.remoteAgents[0].endpoint
      # Must equal the MAS A2A base URL from Step 4 (MAS_A2A_URL), which is the
      # same value as mao-a2a's a2a.publicBaseUrl. With route.host unset, the
      # generated host is mao-a2a-mas-a2a-<namespace>.apps.<cluster>, NOT
      # mas-a2a.apps.<cluster>.
      value: "https://mao-a2a-mas-a2a-mao.apps.<your-cluster>"
```

Commit and push. Wait for ArgoCD to sync the `acme-agent` application.

**Step 6: Verify the Full Path**

Send a test request:

ACME's A2A JSON-RPC endpoint is the root path `/` (served by Google ADK's
`to_a2a`), not `/a2a/jsonrpc`. Use the A2A `message/send` method. ACME refuses
to delegate unless the message contains exactly one explicit cluster API URL,
so include it in the text:

```bash
ACME_URL="https://$(oc get route acme-agent -n acme-agent -o jsonpath='{.spec.host}')"
curl -X POST "${ACME_URL}/" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "message/send",
    "params": {
      "message": {
        "messageId": "test-1",
        "role": "user",
        "parts": [{"kind": "text", "text": "For cluster https://api.example.com:6443, list pods in namespace default"}]
      }
    }
  }'
```

The agent card itself is at `${ACME_URL}/.well-known/agent-card.json`.

Check logs at each hop:

```bash
# ACME → MAS A2A
oc logs -n acme-agent deployment/acme-agent | grep acme_audit | tail -5

# MAS A2A → OLS
oc logs -n mao deployment/mas-a2a | grep -E "validate|principal" | tail -10

# OLS → MCP
oc logs -n a2a-lightspeed -l app.kubernetes.io/component=application-server | grep a2a_audit | tail -5
```

**To Revert to Praxis:**

Comment out the ACME `a2a.remoteAgents` override. ACME will fall back to
deriving the endpoint from `global.clusterBaseDomain`, which points to Praxis.

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

## OpenShift Lightspeed

ACME delegates OpenShift questions and incident investigations to OpenShift
Lightspeed, deployed via the OpenShift Lightspeed Operator and configured by
this pattern's own `charts/all/openshift-lightspeed-config` chart (an
`OLSConfig` using Gemini on Vertex AI, with its operator-managed,
introspection-enabled OpenShift MCP server). See that chart's
[README](charts/all/openshift-lightspeed-config/README.md) for what it
configures and what remains an open, unresolved MCP-hardening gap, and
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

`mao-a2a` points to [`charts/all/mao/mas-a2a`](charts/all/mao/mas-a2a), but
the chart's `a2a.enabled: false` default renders **no resources**. No
orchestrator blueprint ID, authorized Keycloak subject, caller token, or
published image has been supplied for activation. Do not turn it on simply
because its Argo CD Application appears Synced.

**IMPORTANT:** The A2A functionality requires a custom-built MAS image from
the `a2a-inbound-support` branch. The existing `mas-api` and `mas-worker`
`latest` defaults do NOT contain A2A support. Follow the build and
configuration steps below before enabling.

#### Step 0 — Build the A2A-enabled MAS image

The A2A runtime (`ROLE=a2a-serve`) is built into the same container image as
the MAS API and worker. Build from the
[`ltomasbo/uie-mas-hosted`](https://gitlab.cee.redhat.com/ltomasbo/uie-mas-hosted)
repository on branch `a2a-inbound-support`:

```bash
export QUAY_ORG=<your-quay-org>
podman login quay.io

# Clone the A2A-enabled source
git clone --branch a2a-inbound-support --single-branch \
  git@gitlab.cee.redhat.com:ltomasbo/uie-mas-hosted.git /tmp/uie-mas-hosted
cd /tmp/uie-mas-hosted

# Tag with the source commit for reproducibility
export MAS_TAG=$(git rev-parse --short=12 HEAD)

# Build the unified image (contains API, worker, and A2A roles)
podman build -f Dockerfile -t quay.io/${QUAY_ORG}/mas-api:${MAS_TAG} .
podman push quay.io/${QUAY_ORG}/mas-api:${MAS_TAG}
```

This single image supports all three roles (`ROLE=serve` for API,
`ROLE=temporal-worker` for worker, `ROLE=a2a-serve` for A2A) via the
entrypoint. You **must** use the same image tag across all three deployments
(`mas-api`, `mas-worker`, `mas-a2a`) to ensure compatible code and
dependencies.

After building, update `variants/standalone/values-standalone.yaml` with the
new image coordinates in the `mao-api`, `mao-worker`, and `mao-a2a`
application overrides:

```yaml
  - name: mao-api
    overrides:
      - name: image.repository
        value: quay.io/<your-org>/mas-api
      - name: image.tag
        value: <MAS_TAG>
      # ... other API overrides

  - name: mao-worker
    overrides:
      - name: image.repository
        value: quay.io/<your-org>/mas-api
      - name: image.tag
        value: <MAS_TAG>
      # ... other worker overrides

  - name: mao-a2a
    overrides:
      - name: a2a.enabled
        value: "true"
      - name: image.repository
        value: quay.io/<your-org>/mas-api
      - name: image.tag
        value: <MAS_TAG>
      # ... A2A configuration (see below)
```

#### Step 1 — Harden the MAS API before enabling A2A

**Do not enable A2A while the existing `mas-api` Route accepts development-mode
authentication.** The API is independently reachable with a public Route.

**The `mao-api` application is now pre-configured with Keycloak authentication**
in `variants/standalone/values-standalone.yaml`. You only need to add authorized
admin users.

1. Get Keycloak user subject IDs for authorized administrators:

   ```bash
   # Get user IDs from Keycloak
   oc exec -n keycloak-system deploy/keycloak -- \
     /opt/keycloak/bin/kcadm.sh config credentials \
     --server http://localhost:8080 \
     --realm master \
     --user admin \
     --password $(oc get secret keycloak-initial-admin -n keycloak-system -o jsonpath='{.data.password}' | base64 -d)
   
   oc exec -n keycloak-system deploy/keycloak -- \
     /opt/keycloak/bin/kcadm.sh get users -r rca --fields id,username
   
   # Or from Keycloak Admin Console:
   # Navigate to: Keycloak → Realm 'rca' → Users → select user → copy ID (UUID)
   ```

2. Update `variants/standalone/values-standalone.yaml` with authorized user IDs:

   ```yaml
   - name: mao-api
     overrides:
       # ... existing overrides ...
       - name: identity.adminAllowedUsers
         value: '["a1b2c3d4-e5f6-...", "g7h8i9j0-k1l2-..."]'  # Replace with actual user IDs
   ```

   **Important:** Use Keycloak user subject IDs (UUIDs from the `sub` claim),
   not usernames. The validator checks the token's `sub` claim against this list.

2. If switching the worker to Keycloak mode as well, configure its audience:

   ```yaml
   - name: mao-worker
     overrides:
       - name: identity.providerMode
         value: keycloak
       - name: identity.keycloakAudience
         value: "<your-mas-worker-audience>"
   ```

3. Verify a real token from your Keycloak issuer contains the configured
   audience before deploying. The issuer URL is derived from
   `global.clusterBaseDomain` and `global.keycloakRealm` unless explicitly
   overridden with `identity.keycloakIssuerUri`.

#### Step 2 — Create and persist an orchestrator blueprint

The A2A service publishes one MAS orchestrator blueprint. You must create this
blueprint through the authenticated MAS API **before** enabling A2A.

**For multi-cluster routing:** See [Multi-cluster routing with dynamic headers](#multi-cluster-routing-with-dynamic-headers)
below for an orchestrator blueprint that routes to different OLS backends based
on the user's prompt (requires Praxis static agent card and OLS external Routes).

1. Use the MAS API's `POST /api/blueprints/blueprint.save` endpoint to save
   an orchestrator blueprint:

   ```bash
   export MAO_API="https://$(oc get route mas-api -n mao -o jsonpath='{.spec.host}')"
   export KEYCLOAK_TOKEN="<your-keycloak-token>"

   BLUEPRINT_ID=$(curl -ksS -X POST "$MAO_API/api/blueprints/blueprint.save" \
     -H "Authorization: Bearer $KEYCLOAK_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "blueprintRaw": "<your-orchestrator-blueprint-yaml-or-json>"
     }' | jq -r '.blueprint_id')

   echo "Blueprint ID: $BLUEPRINT_ID"
   ```

2. Verify the blueprint resolves:

   ```bash
   curl -ksS -H "Authorization: Bearer $KEYCLOAK_TOKEN" \
     "$MAO_API/api/blueprints/available.blueprints.resolved.get?blueprintId=$BLUEPRINT_ID"
   ```

3. **Do not use** `uie-mas-hosted/run/fixtures/blueprint_a2a_agent.yml` as the
   inbound blueprint — it is an outbound A2A client-node fixture, not a
   publishable orchestrator.

#### Step 3 — Configure A2A authentication and authorization

MAS A2A uses **Keycloak token validation with passthrough** to downstream
agents. The authenticated token is retained and passed through to a2a_agent_node
instances in the orchestrator blueprint, enabling the full authentication chain:

```
ACME (SPIFFE) → MAS (passthrough) → Praxis → OLS (SPIFFE + RFC 8693) → MCP → API
```

**MAS does not need its own SPIFFE workload identity or Keycloak client
registration.** It acts as a transparent orchestrator, validating the inbound
token and passing it through to downstream services.

##### How authentication works

1. **Token validation**: The A2A service validates bearer tokens against the
   Keycloak issuer's JWKS endpoint. It checks:
   - Token signature (via JWKS)
   - Expiration (`exp` claim)
   - Audience (`aud` claim must contain the configured `identity.keycloakAudience`)
   - Issuer (`iss` claim must match `identity.keycloakIssuerUri`)

2. **Principal authorization**: After validation, the token's `sub` (subject)
   claim is checked against `a2a.allowedPrincipals`. Only exact matches are
   authorized. The `sub` claim is typically an opaque Keycloak user ID, **not**
   a username or email.

3. **Token passthrough**: After validation, MAS **retains the bearer token**
   and passes it through to the MAS session execution context as
   `RunContext.credentials`. When an `a2a_agent_node` in the orchestrator
   blueprint has an empty `bearer_token` field, it automatically uses the
   context token for outbound calls (this is already implemented in the
   `a2a_agent_node_factory`).

4. **Multi-audience support**: ACME (or any caller) can request a **single
   token with multiple audiences** from Keycloak. Each downstream service
   validates against its own expected audience from the array. This allows one
   token to work for MAS, OLS, Ask Red Hat, and other agents without separate
   token requests.

##### Configuration steps

1. **Choose an audience strategy:**

   **Option A: Share the OLS audience** (recommended for simplicity)
   - MAS validates `lightspeed-a2a` (same as OLS)
   - ACME requests one token: `scope=openid lightspeed-a2a`
   - Token works for both MAS inbound and OLS outbound (via passthrough)
   - No changes to `acme-agent` Keycloak client needed

   **Option B: Separate MAS audience** (for stronger isolation)
   - MAS validates `mas-a2a`, OLS validates `lightspeed-a2a`
   - ACME requests **multi-audience token**: `scope=openid mas-a2a lightspeed-a2a`
   - Requires adding `mas-a2a-audience` mapper to `acme-agent` in Keycloak

2. Obtain a test token from a caller that will use the A2A service. Decode the
   token (e.g., at jwt.io) and note its `iss`, `aud`, and `sub` claims.
   **Never log or commit the actual token.**

3. Configure the A2A application with the validated values:

   ```yaml
   - name: mao-a2a
     overrides:
       - name: a2a.enabled
         value: "true"
       - name: a2a.blueprintId
         value: "<BLUEPRINT_ID>"
       - name: a2a.publicBaseUrl
         value: "https://mas-a2a-mao.apps.<cluster-domain>"
       - name: a2a.allowedPrincipals
         value: '["<validated-keycloak-sub-1>", "<validated-keycloak-sub-2>"]'
       - name: identity.providerMode
         value: keycloak
       - name: identity.keycloakAudience
         value: "lightspeed-a2a"  # Option A: shared audience
         # OR value: "mas-a2a"     # Option B: separate audience
       - name: image.repository
         value: quay.io/<your-org>/mas-api
       - name: image.tag
         value: <MAS_TAG>
   ```

4. **Keycloak audience mapper** (only for Option B - separate audience):

   If using a separate `mas-a2a` audience, add the mapper to `acme-agent` in
   `charts/all/keycloak-oidc/templates/keycloak-realm-import.yaml`:

   ```yaml
   - clientId: acme-agent
     protocolMappers:
       # Existing mappers...
       - name: mas-a2a-audience
         protocol: openid-connect
         protocolMapper: oidc-audience-mapper
         config:
           included.custom.audience: "mas-a2a"
           access.token.claim: "true"
   ```

   For an existing realm, add the mapper in the Keycloak Admin Console:
   - Navigate to the `acme-agent` client (or the client your callers use)
   - Add an **Audience** protocol mapper:
     - Mapper Type: **Audience**
     - Included Custom Audience: `mas-a2a`
     - Add to Access Token: **ON**

   ACME will then request multiple audiences: `scope=openid mas-a2a lightspeed-a2a`

   **Option A (shared audience) requires no Keycloak changes** — the existing
   `lightspeed-a2a-audience` mapper on `acme-agent` is sufficient.

5. **Private Keycloak CA (optional)**: If the Keycloak Route uses a private
   ingress CA, configure CA trust:

   ```yaml
   - name: mao-a2a
     overrides:
       - name: identity.keycloakCaTrust.enabled
         value: "true"
   ```

   The `keycloak-oidc` application must have published the CA ConfigMap in the
   `mao` namespace via `openshiftOIDC.caBundleSync.enabled: true` and
   `openshiftOIDC.caBundleSync.additionalTargetNamespaces: [mao]`. Apply the
   same setting to `mao-api` and `mao-worker` when switching them to Keycloak
   mode.

6. **Orchestrator blueprint configuration**: Create a blueprint with
   `a2a_agent_node` instances that use **empty `bearer_token`** for passthrough:

   ```yaml
   nodes:
     - rid: ols_agent_rid
       name: OpenShift Lightspeed Agent
       type: a2a_agent_node
       config:
         type: a2a_agent_node
         base_url: https://praxis-proxy-praxis-proxy.apps.<cluster-domain>/
         bearer_token: ""  # Empty = use inbound context token
         verify_ssl: true
   
   plan:
     - uid: user_input
       node: user_question_node_rid
     - uid: call_ols
       after: user_input
       node: ols_agent_rid
     - uid: finalize
       after: call_ols
       node: final_answer_node_rid
   ```

   Save this blueprint via the MAS API and use its returned `blueprint_id` in
   Step 2.

#### Step 4 — Deploy and verify

1. Ensure MongoDB, Redis, Temporal, and the Temporal worker are ready:

   ```bash
   oc get pods -n mao | grep -E 'mongo|redis|temporal|mas-worker'
   ```

2. Commit and sync the changes:

   ```bash
   git add variants/standalone/values-standalone.yaml
   git commit -m "Enable MAS A2A with hardened authentication"
   git push origin <your-branch>
   make argo-healthcheck
   ```

3. Verify the A2A deployment:

   ```bash
   oc get pods -n mao -l app.kubernetes.io/name=mas-a2a
   oc logs -n mao -l app.kubernetes.io/name=mas-a2a
   ```

4. Test the Agent Card (public, no auth required):

   ```bash
   export A2A_HOST=$(oc get route -n mao -l app.kubernetes.io/name=mas-a2a -o jsonpath='{.items[0].spec.host}')
   curl -ksS "https://$A2A_HOST/.well-known/agent-card.json" | jq
   ```

5. Test authorization boundaries:

   ```bash
   # Unauthenticated request (expect 401)
   curl -ksS -o /dev/null -w '%{http_code}\n' "https://$A2A_HOST/a2a/jsonrpc"

   # Valid token, non-allow-listed subject (expect 403)
   curl -ksS -o /dev/null -w '%{http_code}\n' \
     -H "Authorization: Bearer <token-with-wrong-subject>" \
     "https://$A2A_HOST/a2a/jsonrpc"

   # Valid token, allow-listed subject (expect 200 or valid JSON-RPC response)
   curl -ksS -H "Authorization: Bearer <valid-token>" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","method":"tasks.list","params":{},"id":1}' \
     "https://$A2A_HOST/a2a/jsonrpc"
   ```

#### What's NOT needed (passthrough design)

Because MAS uses **token passthrough** rather than acting as an independent
workload identity:

- ❌ **No SPIFFE for MAS** — MAS doesn't fetch its own JWT-SVID
- ❌ **No Keycloak client for MAS** — MAS is not registered in
  `keycloak-realm-import.yaml`
- ❌ **No Praxis policy changes** — Praxis still validates `azp=acme-agent`, not
  `mas-orchestrator`
- ❌ **No RBAC changes** — OpenShift RBAC still binds to
  `Group:keycloak:acme-agent-rca`
- ✅ **MAS validates and passes through** — The same token ACME fetched reaches
  OLS unchanged

From Praxis/OLS/MCP's perspective, MAS is invisible — they only see ACME's
token.

#### Human-in-the-loop (HITL) approvals

When an orchestrator node requires human approval for a tool call, the task
state becomes `TASK_STATE_INPUT_REQUIRED` (non-terminal). To resume:

1. Send a follow-up A2A message with the **same `task_id`** and a **new
   `message_id`**.
2. Include a decision as either:
   - **Structured**: `{"decision": "approve"|"reject"|"modify"|"redirect",
     "feedback": "...", "modifiedArgs": {...}}`
   - **Plain text**: Leading keyword `approve`/`yes` to approve,
     `reject`/`no`/`deny` to reject, with the rest as feedback.
3. Reattach/subscribe to the task's stream to observe the next event and clear
   `INPUT_REQUIRED`. Polling alone may continue showing `INPUT_REQUIRED` until
   a stream attachment observes a progress event.

#### Verification and testing

After deployment, test the passthrough flow:

```bash
# 1. Verify MAS A2A is running
oc get pods -n mao -l app.kubernetes.io/name=mas-a2a
export A2A_HOST=$(oc get route -n mao -l app.kubernetes.io/name=mas-a2a -o jsonpath='{.items[0].spec.host}')

# 2. Test agent card (public, no auth)
curl -ksS "https://$A2A_HOST/.well-known/agent-card.json" | jq

# 3. Get a multi-audience token from ACME's perspective
# (This would normally happen inside ACME's code)
# For manual testing, use acme-agent's service account:
oc exec -n acme-agent deploy/acme-agent -- \
  python3 -c "
import os
from acme_agent.auth import get_keycloak_token
token = get_keycloak_token(
    scope='openid lightspeed-a2a mas-a2a',  # Multi-audience!
)
print(token)
" > /tmp/acme-token.txt

export ACME_TOKEN=$(cat /tmp/acme-token.txt)

# 4. Decode and verify token has both audiences
echo $ACME_TOKEN | cut -d. -f2 | base64 -d 2>/dev/null | jq '.aud'
# Should show: ["lightspeed-a2a", "mas-a2a"]

# 5. Test MAS A2A with the token
curl -ksS -H "Authorization: Bearer $ACME_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "messages.send",
    "params": {
      "message": {
        "role": "user",
        "messageId": "'$(uuidgen)'",
        "parts": [{"text": "What pods are running in namespace openshift-operators?"}]
      }
    },
    "id": 1
  }' \
  "https://$A2A_HOST/a2a/jsonrpc" | jq

# 6. Verify token passthrough: check Praxis/OLS logs
oc logs -n praxis-proxy -l app.kubernetes.io/name=praxis-proxy --tail=50 | grep azp
# Should show: azp=acme-agent (not mas-orchestrator!)

oc logs -n a2a-lightspeed -l app=lightspeed-app-server --tail=50 | grep a2a_audit
# Should show: on_behalf_of=acme-agent
```

See [`uie-mas-hosted/docs/a2a-inbound.md`](https://gitlab.cee.redhat.com/ltomasbo/uie-mas-hosted/-/blob/a2a-inbound-support/docs/a2a-inbound.md)
for client examples and the [separate MAS auth boundary](agents/AUTHENTICATION.md#separate-trust-boundary-inbound-mas-a2a)
for the full authentication flow. The [`mas-a2a` chart guide](charts/all/mao/mas-a2a/README.md)
has deployment details and the full verification checklist.

### Multi-cluster routing with dynamic headers

**When to use:** After completing Phase 4 Step 2 (creating a blueprint), optionally
use this multi-cluster orchestrator blueprint instead of a simple pass-through
blueprint if you have multiple OLS clusters registered in `global.olsClusters`.

**Prerequisites:**
- Praxis static agent card enabled (`agentCard.enabled: true` - already configured in values-standalone.yaml)
- OLS external Routes configured for each cluster (completed in Phase 1)
- Multiple clusters registered in `global.olsClusters` with different IDs
- MAS image with dynamic headers support (uie-mas-hosted commit 3de31db or later)

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

**Requirements:**
- Praxis static agent card enabled (`agentCard.enabled: true` in values-standalone.yaml)
- OLS external Routes configured for each cluster (see Phase 1)
- MAS image with dynamic headers support (commit 3de31db or later in uie-mas-hosted)

**uie-mas-hosted changes:** The `a2a_agent_node` now reads `task.data["headers"]`
and applies them via `a2a_provider.update_headers()` before sending requests,
enabling per-task header injection without static blueprint configuration.
