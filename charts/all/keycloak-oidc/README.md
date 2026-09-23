# Keycloak/OIDC integration for the RCA and Ericsson A2A agents

This companion chart is installed after the validated pattern's `rhbk` chart.
It imports the RCA realm, four clients, and a group into the deployed
Keycloak, and creates the least-privilege AgenticRun RoleBinding for the
OpenShift group emitted by the Keycloak `groups` claim.

The four clients serve distinct purposes and must not be conflated:

- `keycloak.clientId` (default `rca-agent`): public client for browser/`oc
  login` OIDC flows (Native OIDC). Cannot authenticate itself. Also
  registered as the `cli` OIDC platform client when `openshiftOIDC.enabled`
  is true.
- `keycloak.mcpClientId` (default `rca-agent-mcp`): confidential client
  `charts/all/rca-agent` uses to exchange a caller's token for one scoped to
  OpenShift MCP.
- `keycloak.ericssonClientId` (default `ericsson-agent`): confidential client
  `charts/all/ericsson-agent` uses for its client-credentials
  call to Keycloak.
- `keycloak.consoleClientId` (default `openshift-console`): confidential
  client registered as the `console` OIDC platform client when
  `openshiftOIDC.enabled` is true. See "OpenShift Native OIDC" below.

The two confidential clients are intended to authenticate with a SPIFFE
JWT-SVID (no static secret) via Keycloak's federated client authentication
feature, against the `spiffe` identity provider configured by the `keycloak`
application's `spiffeIdentityProvider` override
(`variants/standalone/values-standalone.yaml`). That feature's exact
client-side fields are Keycloak-version-specific and not yet declarative in
this chart's `KeycloakRealmImport`; configure it once in the Keycloak Admin
Console under each confidential client's Credentials tab, then grant
`rca-agent-mcp` token-exchange permission for the `openshift-mcp` audience.

## OpenShift Native OIDC

`Authentication/cluster` is a cluster-wide singleton: setting
`openshiftOIDC.enabled=true` switches `spec.type` to `OIDC` for the *entire*
cluster, replacing the internal OAuth server everywhere at once (console,
`oc login`, everything). Enabling it before it is fully and correctly
configured does not fail safely -- it locks out the console and CLI cluster-
wide, with `ExternalOIDCControllerDegraded` reporting an issuer/token error.
Do not enable it speculatively; walk the checklist below in order.

There is no supported way to keep the internal OAuth server active
alongside OIDC -- `spec.type` is a single mutually exclusive field, and
switching to `OIDC` replaces the console/CLI login flow entirely rather than
adding to it. Two things do soften this in practice, and are worth relying
on rather than treating the switch as one-way: existing `IdentityProvider`
entries on `oauth.config.openshift.io/cluster` are not deleted when you
enable OIDC, so switching `spec.type` back to `""` (see "Recovering if it
breaks anyway" below) restores them immediately with no reconfiguration; and
client-certificate/kubeconfig-based admin access (for example a
`system:admin` context from the installer) never goes through the OAuth/OIDC
login flow at all, so it keeps working even if the console and `oc login`
are both broken. **Before ever setting `openshiftOIDC.enabled=true`,
confirm you have such a break-glass kubeconfig available** -- it is what
makes recovery possible if something in the checklist below was missed.

Two things must both be correct, not just the issuer:

1. `oidcProviders` -- the Keycloak realm itself (`openshiftOIDC.issuerURL`,
   which must exactly match the JWT `iss` claim and be reachable from the
   OpenShift control plane).
2. `oidcClients` -- registers the console and `oc` CLI as OIDC clients of
   that realm. Without this, OpenShift still switches to `type: OIDC`, but
   the console and CLI operators report "no OIDC client found" and nobody
   can log in at all, even though the identity provider itself is fine. This
   chart now templates both `oidcClients` entries automatically
   (`keycloak.clientId` as the public `cli` client, `keycloak.consoleClientId`
   as the confidential `console` client), but the console client's secret
   still needs the one manual step below.

### Keeping admin access after enabling OIDC

CLI and console need two separate answers here -- OIDC replaces the login
*flow*, not the API server's ability to accept other credentials, but the
console has no other credential path while the CLI does.

**CLI break-glass (do this first, before touching anything else below):** set
`breakGlass.enabled=true` on the `keycloak-oidc` application and sync -- this
templates a cluster-admin ServiceAccount, a `ClusterRoleBinding`, and a
non-expiring, legacy-style token Secret, all in `openshift-config`. This is
part of `make install`'s normal GitOps flow like everything else in this
chart, so it's in place before you ever touch `openshiftOIDC.enabled`.
ServiceAccount tokens are validated by the API server directly and never go
through `Authentication/cluster`'s OAuth/OIDC flow at all, so this keeps
working no matter what `spec.type` is set to -- it is what makes "Recovering
if it breaks anyway" below actually possible.

What can't be automated as part of the GitOps sync is turning that token
into a local kubeconfig file -- Helm/ArgoCD only create in-cluster objects,
they can't write to your workstation's disk. Do that once, separately, with:

```bash
make admin-break-glass-kubeconfig
```

This reads the Secret created above and writes `admin-break-glass.kubeconfig`
in the repo root (already `.gitignore`d). Move it somewhere safe outside the
repo -- a password manager or offline vault -- and verify it works
(`oc --kubeconfig=admin-break-glass.kubeconfig whoami`) before you rely on
it.

**Console:** the web console only ever authenticates through the active
OAuth/OIDC flow -- there is no client-certificate or ServiceAccount-token
login path for a browser. So once `openshiftOIDC.enabled=true`, the only way
to reach the console as an administrator is through a Keycloak identity that
Kubernetes RBAC recognizes as `cluster-admin`. Set `keycloak.adminGroupName`
(for example `rca-admins`) to have this chart create that group in the realm
and bind it to `cluster-admin` via a `ClusterRoleBinding`, then add your own
Keycloak user to that group (Admin Console → Users → your user → Groups →
Join Group) *before* enabling OIDC. This binding is created unconditionally
whenever `keycloak.adminGroupName` is set, independent of
`openshiftOIDC.enabled`, so it's ready by the time you flip the switch.

Note the same one-shot limitation as the rest of the realm import: setting
`keycloak.adminGroupName` after the realm has already reached `Done: True`
will not retroactively create the group (see the checklist below and the
note at the end of this file). If that's already happened, create the group
manually once in the Admin Console with the same name instead -- the
`ClusterRoleBinding` itself is a normal, always-reconciled resource and
doesn't have this limitation.

### Pre-flight checklist (do this before setting `openshiftOIDC.enabled=true`)

0. Complete "Keeping admin access after enabling OIDC" above: have a
   verified CLI break-glass kubeconfig, and a Keycloak user already in
   `keycloak.adminGroupName` if you set one.

1. Confirm the realm import actually succeeded -- it is applied only once,
   and if it raced the `Keycloak` CR's own creation on a fresh install it can
   fail permanently without retrying:

   ```bash
   oc get keycloakrealmimport <keycloak.realm, default "rca"> \
     -n <keycloak.namespace, default "keycloak-system"> -o jsonpath='{.status.conditions}'
   ```

   Look for `"type":"Done","status":"True"`. If instead you see a
   `HasErrors` condition mentioning `keycloaks.k8s.keycloak.org "<name>" not
   found`, the `Keycloak` CR did not exist yet when the import first
   reconciled. Delete the `KeycloakRealmImport` object once the `Keycloak` CR
   and its pod are `Running`, and let ArgoCD/the operator recreate it -- it
   will succeed once the dependency it needs actually exists.

2. Confirm the realm is actually reachable at the issuer URL you're about to
   set:

   ```bash
   curl -sk https://<keycloak-route>/realms/<realm>/.well-known/openid-configuration
   ```

   A `{"error":"Realm does not exist"}` 404 here means step 1 hasn't
   succeeded yet -- do not proceed.

3. Get the `openshift-console` client's Keycloak-generated secret (Admin
   Console → Clients → `openshift-console` → Credentials tab → Client
   secret), and create the Secret `oidcClients` expects, in the
   `openshift-config` namespace (not this chart's namespace):

   ```bash
   oc create secret generic openshift-console-oidc \
     -n openshift-config \
     --from-literal=clientSecret='<value from the Credentials tab>'
   ```

   The secret name must match `openshiftOIDC.consoleClientSecretName`, and
   the key must be literally `clientSecret` (required by the `Authentication`
   CRD's `oidcClients[].clientSecret` field).

4. Only then set `openshiftOIDC.enabled=true`, `openshiftOIDC.issuerURL`, and
   `openshiftOIDC.consoleRoute` (the console's public route, from `oc whoami
   --show-console`), and sync.

### Recovering if it breaks anyway

If login breaks cluster-wide before all of the above is verified, restore the
internal OAuth server immediately:

```bash
oc patch authentication.config.openshift.io cluster --type=merge \
  -p '{"spec":{"type":"","oidcProviders":null}}'
```

Then fix `openshiftOIDC.enabled=false` (or the underlying issue) in
`variants/standalone/values-standalone.yaml` too, so the next ArgoCD sync
doesn't re-apply the broken configuration on top of your patch.

The realm import is applied only when the realm does not already exist. The
Keycloak operator does not continuously reconcile edits made after import.
