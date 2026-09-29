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

Deploy from branch `rca-praxis`.

### Phase 0 — Before touching the cluster

1. Build and push both agent images (Praxis itself isn't built from this
   repo -- it pulls a pinned upstream image):

    ```bash
    export QUAY_ORG=<your-quay-org>
    podman login quay.io

    export RCA_TAG=$(git rev-parse --short HEAD)
    podman build -f agents/rca_agent/Containerfile -t quay.io/${QUAY_ORG}/rca-agent:${RCA_TAG} agents/rca_agent
    podman push quay.io/${QUAY_ORG}/rca-agent:${RCA_TAG}

    export ACME_TAG=$(git rev-parse --short HEAD)
    podman build -f agents/acme_agent/Containerfile -t quay.io/${QUAY_ORG}/acme-agent:${ACME_TAG} agents/acme_agent
    podman push quay.io/${QUAY_ORG}/acme-agent:${ACME_TAG}
    ```

2. Fill in the image placeholders in `variants/standalone/values-standalone.yaml`
   for the `rca-agent` and `acme-agent` applications only. `praxis-proxy`
   needs no image edit -- it pins `ghcr.io/praxis-proxy/ai:0.4.1` by digest in
   `charts/all/praxis-proxy/values.yaml`. That image is alpha/prerelease
   upstream software and this integration isn't a Red Hat-supported Praxis
   distribution -- don't bump `image.tag` without reviewing upstream release
   notes and re-pinning the digest.

3. Confirm `rca-praxis` is what's pushed and what the new cluster's
   ArgoCD Application will track:

    ```bash
    git checkout rca-praxis
    git push origin rca-praxis
    ```

### Phase 1 — Install

4. Prepare secrets:

    ```bash
    cp values-secret.yaml.template ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
    $EDITOR ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
    ```

    - Uncomment `llm-creds-vertex` and point `path:` at a real GCP
      Application Default Credentials JSON file --
      `lightspeed-agentic-operator` needs this
      (the Vertex provider is enabled in `values-standalone.yaml`).
    - `rca-agent-litellm`, `acme-agent-litellm`, and `llm-creds-openai` prompt
      interactively during `load-secrets` (`onMissingValue: prompt`). The
      lightspeed operator uses the independent `llm-creds-openai` key by
      default; its `default` Agent calls LiteLLM model `gpt-oss-20b`, while the
      separate `vertex` Agent uses Vertex Anthropic.
    - Optionally set `breakGlass.enabled: "true"` and
      `keycloak.adminGroupName` on the `keycloak-oidc` application now,
      before install (see Phase 3). Leave `openshiftOIDC.enabled: "false"`
      -- it can't be safely enabled until the cluster (and its real trust
      domain) exists; see Phase 3.

5. Log into the fresh cluster as cluster-admin, then install:

    ```bash
    oc login <new-cluster-api>
    make validate-prereq
    make validate-origin
    ./pattern.sh make install
    ```

    This brings up vault, ESO, ZTWIM, Keycloak, RHOAI, MAO,
    `lightspeed-agentic-operator`, `openshift-mcp-server`, `keycloak-oidc`,
    `rca-agent`, `praxis-proxy`, and `acme-agent` in one shot.
    `rca-agent`'s own Route stays disabled (`route.enabled: "false"`) and its
    Service is NetworkPolicy-restricted to same-namespace `praxis-proxy`
    pods; `praxis-proxy` owns the public Route instead. Everything syncs
    with placeholder Keycloak URLs and trust domain at first, so
    `keycloak-oidc`/`rca-agent`/`praxis-proxy`/`acme-agent` will be
    Synced but not functional yet. Expected -- continue to Phase 2.

    If secrets are added/modified after installation, re-run:

    ```bash
    cp values-secret.yaml.template ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
    ./pattern.sh make load-secrets
    ```

### Phase 2 — Wire up the real cluster-specific values

6. Look up the values the placeholders need:

    ```bash
    oc get zerotrustworkloadidentitymanager cluster -o jsonpath='{.spec.trustDomain}{"\n"}'
    ```

    That single value (e.g. `apps.ocp.<hash>.sandboxNNNN.opentlc.com`) is the
    trust domain, and everything else derives from it:

    - `keycloak-oidc` → `openshiftOIDC.issuerURL` = `https://keycloak.<trustDomain>/realms/rca`
    - `keycloak-oidc` → `openshiftOIDC.consoleRoute` = `https://console-openshift-console.<trustDomain>` (confirm with `oc whoami --show-console`) -- leave `openshiftOIDC.enabled` `"false"` regardless, until Phase 3
    - `keycloak-oidc` → `keycloak.spiffeIdentityProvider.trustDomain` = the trust domain itself
    - `rca-agent` → `identity.keycloak.issuerUrl` = same as `openshiftOIDC.issuerURL`
    - `rca-agent` → `identity.clusterSpiffeID.trustDomain` = the trust domain itself
    - `rca-agent` → `a2a.publicHost` = the public A2A hostname you choose (e.g. `rca-agent.<trustDomain>`) -- `rca-agent`'s own Route stays off, so this is used only for agent-card metadata, but it must match `praxis-proxy`'s `route.host` below exactly
    - `praxis-proxy` → `route.host` = the same hostname as `rca-agent`'s `a2a.publicHost` -- this is the Route actually exposed to callers
    - `praxis-proxy` → `identity.keycloak.issuerUrl` = same as `openshiftOIDC.issuerURL`
    - `acme-agent` → `auth.keycloak.tokenUrl` = `<issuerURL>/protocol/openid-connect/token`
    - `acme-agent` → `identity.clusterSpiffeID.trustDomain` = the trust domain itself
    - `acme-agent` → `a2a.downstreamEndpoint` = `https://<same hostname as praxis-proxy's route.host>`

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

    Federated-jwt client authentication for `rca-agent-mcp` and
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
    git commit -m "Set cluster-specific Keycloak URL and trust domain"
    git push origin rca-praxis
    make argo-healthcheck
    ```

10. Verify Praxis is actually enforcing the policy:

    ```bash
    oc get configmap praxis-proxy -n lightspeed-agentic-operator -o yaml   # rendered policy.yaml/praxis.yaml
    oc logs -n lightspeed-agentic-operator -l app.kubernetes.io/name=praxis-proxy
    oc get networkpolicy rca-agent -n lightspeed-agentic-operator -o yaml  # ingress restricted to praxis-proxy pods

    # agent-card discovery is intentionally public, no token needed:
    curl -sf https://<praxis route host>/.well-known/agent-card.json

    # everything else must reject an unauthenticated/non-allow-listed caller:
    curl -s -o /dev/null -w '%{http_code}\n' https://<praxis route host>/  # expect 401/403
    ```

At this point `rca-agent`, `praxis-proxy`, and `acme-agent` should be
fully functional: `acme-agent`'s Keycloak token reaches the public
Praxis Route, Praxis validates the token and its `azp` allow-list and
forwards it unchanged to `rca-agent` (which independently re-validates it
and does the RFC 8693 token exchange for OpenShift MCP), and RCA's own
Service is unreachable except from Praxis. See
[`agents/AUTHENTICATION.md`](agents/AUTHENTICATION.md) for the full request
flow.

### Phase 3 — Optional: enable OpenShift Native OIDC

Skip entirely unless you specifically want direct Keycloak login for the
console and `oc`. Nothing above depends on it, and getting it wrong locks
out console and CLI login cluster-wide. Full detail is in
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
    git push origin rca-praxis
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

## Lightspeed Agentic Operator

See the [lightspeed-agentic-operator chart README](charts/all/lightspeed-agentic-operator/README.md)
for instructions on configuring, using, and testing the operator.

The RCA A2A service is fronted by the Praxis AI gateway and its embedded
Praxis Policy Engine. See [`agents/AUTHENTICATION.md`](agents/AUTHENTICATION.md)
for the request flow and [`charts/all/praxis-proxy/README.md`](charts/all/praxis-proxy/README.md)
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
