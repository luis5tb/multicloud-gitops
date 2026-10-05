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

1. Build and push the two images this pattern builds itself. The vendored,
   A2A-enabled OpenShift Lightspeed service (`vendor/lightspeed-service/`)
   is built from its own upstream `Containerfile`; see
   `vendor/lightspeed-service/VENDOR.md` for the exact build command and its
   current caveats (the hash-locked `requirements.txt` doesn't build cleanly
   yet -- build from `pyproject.toml`'s looser constraints in the meantime,
   or regenerate the lock first). Praxis itself isn't built from this repo --
   it pulls a pinned upstream image (see its own version caveats below):

    ```bash
    export QUAY_ORG=<your-quay-org>
    podman login quay.io

    export ACME_TAG=$(git rev-parse --short HEAD)
    podman build -f agents/acme_agent/Containerfile -t quay.io/${QUAY_ORG}/acme-agent:${ACME_TAG} agents/acme_agent
    podman push quay.io/${QUAY_ORG}/acme-agent:${ACME_TAG}

    export OLS_TAG=$(git rev-parse --short HEAD)
    podman build -f vendor/lightspeed-service/Containerfile -t quay.io/${QUAY_ORG}/openshift-lightspeed:${OLS_TAG} vendor/lightspeed-service
    podman push quay.io/${QUAY_ORG}/openshift-lightspeed:${OLS_TAG}
    ```

    The OpenShift Lightspeed Operator picks the app-server image via its own
    `--service-image` startup flag, not an `OLSConfig` field, and has no CRD
    field for the `A2A_*` environment variables the vendored service needs
    either. `charts/all/openshift-lightspeed-config`'s `appServerPatch`
    (disabled by default) is a **temporary** bridge for both: it patches the
    operator-managed app-server Deployment directly, after the fact, with
    this image and the required `A2A_*` settings (`keycloakIssuerURL`,
    `clusterId`, `rpcUrl`, plus the optional audience/azp overrides) --
    see that chart's README.md ("Temporary A2A bridge") for how to set it,
    and why it also scales the operator itself to 0 replicas first (a
    periodic re-assert against the operator's own reconcile loop turned out
    to be a losing fight -- confirmed live). Delete it once the operator/OLSConfig CRD natively
    supports a custom service image and A2A configuration.

2. Replace the `acme-agent` application's `image.repository`/`image.tag`
   overrides in `variants/standalone/values-standalone.yaml` with the image
   you just pushed. **These are not an empty placeholder** -- they currently
   point to a specific prior deployment's personal `quay.io` account and a
   stale tag; do not assume a filled-in-looking value is already correct for
   your deployment. `praxis-proxy` needs no image edit for
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
    its Service is NetworkPolicy-restricted to same-namespace `praxis-proxy`
    pods; `praxis-proxy` owns the public Route instead. Everything syncs
    with placeholder Keycloak URLs and trust domain at first, so
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

    The trust domain (e.g. `apps.ocp.<hash>.sandboxNNNN.opentlc.com`) and the
    API server URL both feed into the values below:

    - `keycloak-oidc` → `openshiftOIDC.issuerURL` = `https://keycloak.<trustDomain>/realms/rca`
    - `keycloak-oidc` → `openshiftOIDC.consoleRoute` = `https://console-openshift-console.<trustDomain>` (confirm with `oc whoami --show-console`) -- leave `openshiftOIDC.enabled` `"false"` regardless, until Phase 3
    - `keycloak-oidc` → `keycloak.spiffeIdentityProvider.trustDomain` = the trust domain itself
    - `keycloak-oidc` → `keycloak.lightspeedWorkload.namespace`/`serviceAccount` = confirm these match the *actual* Service Account the installed Lightspeed Operator creates for its app-server (default guess: `openshift-lightspeed`/`lightspeed-app-server`)
    - `praxis-proxy` → `route.host` = the public A2A hostname you choose (e.g. `openshift-lightspeed.<trustDomain>`) -- this is the Route actually exposed to callers
    - `praxis-proxy` → `identity.keycloak.issuerUrl` = same as `openshiftOIDC.issuerURL`
    - `acme-agent` → `a2a.remoteAgents[0].endpoint` = `https://<same hostname as praxis-proxy's route.host>`
    - `acme-agent` → `auth.keycloak.tokenUrl` = `<issuerURL>/protocol/openid-connect/token`
    - `acme-agent` → `identity.clusterSpiffeID.trustDomain` = the trust domain itself
    - `global.olsClusters` (top of `values-standalone.yaml`, shared by `acme-agent` and `praxis-proxy`) → replace the placeholder entry with the real `oc whoami --show-server` URL, its derived `X-OLS-Cluster` id (lowercase host+port, `.`/`:` → `-`; see `agents/acme_agent/src/acme_agent/cluster_registry.py`), and the real OLS app-server Service `upstreamHost`/`upstreamPort`/`upstreamSNI`
    - `openshift-lightspeed-config` → `appServerPatch.a2a.keycloakIssuerURL` = same as `openshiftOIDC.issuerURL`, `appServerPatch.a2a.clusterId` = the same id you just set in `global.olsClusters`, `appServerPatch.a2a.rpcUrl` = same hostname as `praxis-proxy`'s `route.host` -- and `appServerPatch.image.repository`/`tag` once Phase 0 step 1's image is actually pushed
    - `keycloak-oidc` → `lightspeedRbac.namespaces` = the real namespace(s) on your cluster you want OLS's MCP investigation scoped to (e.g. `lightspeedRbac.namespaces[0]`). Left unset, this is an intentional no-op -- the chart's own default is an empty list, which renders zero Role/RoleBinding resources (acme-agent gets no MCP access anywhere, not a broken deployment) until you set this. There is no "all namespaces" default: if you explicitly want cluster-wide investigation scope (e.g. a sandbox/demo cluster, not a real environment), set `lightspeedRbac.allNamespaces: "true"` instead -- this renders a single ClusterRole/ClusterRoleBinding granting every caller in `lightspeedRbac.groupName` read access to pods/events in *every* namespace, a real widening of blast radius, not just a convenience toggle; see `charts/all/keycloak-oidc/values.yaml`'s comment on it before enabling.

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
    oc get configmap praxis-proxy -n praxis-proxy -o yaml   # rendered policy.yaml/praxis.yaml
    oc logs -n praxis-proxy -l app.kubernetes.io/name=praxis-proxy
    oc get networkpolicy -n openshift-lightspeed -o yaml  # OLS ingress restricted to praxis-proxy pods

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
forwards the request unchanged -- OpenShift Lightspeed's A2A endpoint
independently re-validates it and does the RFC 8693 token exchange for
OpenShift MCP, and OLS's own Service is unreachable except from Praxis. The
exchanged token ("Token B") itself mints successfully, but the API server
does not yet trust it as an identity: it will reject the MCP tool call with
401 until Phase 3 below is done, since `claimMappings.groups` (what maps
Token B's `groups` claim to the OpenShift group `lightspeed-mcp-rbac.yaml`
binds RBAC to) and the `openshift-mcp` audience trust only exist once
`Authentication/cluster` is actually switched to `type: OIDC`. See
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
the full design and task breakdown. The A2A integration itself lives in a
vendored, locally patched copy of the upstream service
([`vendor/lightspeed-service/`](vendor/lightspeed-service/VENDOR.md)), since
upstream OpenShift Lightspeed doesn't speak A2A.

OpenShift Lightspeed's A2A endpoint is fronted by the Praxis AI gateway and
its embedded Praxis Policy Engine, which also routes each request to the
right OLS backend based on the `X-OLS-Cluster` header ACME sends (see
`global.olsClusters` in `variants/standalone/values-standalone.yaml`). See
[`agents/AUTHENTICATION.md`](agents/AUTHENTICATION.md) for the full request
flow and [`charts/all/praxis-proxy/README.md`](charts/all/praxis-proxy/README.md)
for deployment, policy, and upstream maturity details.

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
