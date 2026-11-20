# Keycloak/OIDC integration for ACME and optional MAS A2A

This companion chart is installed after the validated pattern's `rhbk` chart.
It imports the `rca` realm, its three clients, a SPIFFE identity provider, and
its groups into the deployed Keycloak.

> Historical note: this chart previously also created the `lightspeed-mcp` /
> `openshift-mcp` clients and `templates/lightspeed-mcp-rbac.yaml` for the
> OpenShift Lightspeed investigation backend. That backend was removed (see
> `PLAN-lightspeed-hub-migration.md` Task A); the realm import is one-shot, so
> cleaning those out of a live realm is a manual Admin API step covered by
> that plan's teardown runbook.

The three clients serve distinct purposes and must not be conflated:

- `keycloak.clientId` (default `openshift-cli`): public client for browser/`oc
  login` OIDC flows (Native OIDC). Cannot authenticate itself. Registered as
  the `cli` OIDC platform client when `openshiftOIDC.enabled` is true. Purely
  human/CLI cluster access; its tokens carry the `openshift` audience for the
  OpenShift API and, through a separate mapper, the distinct `mas-a2a`
  audience for the optional MAS inbound A2A endpoint. MAS still requires its
  own exact principal allow-list, so the audience mapper alone does not grant
  A2A access.
- `keycloak.acmeClientId` (default `acme-agent`): confidential client
  `charts/all/acme-agent` uses for its client-credentials
  call to Keycloak. Its service account belongs to `keycloak.acmeGroupName`
  (default `acme-agent-rca`) -- without this group, and the `groups` protocol
  mapper this chart attaches to the client, the token this client mints
  carries no groups claim at all, so group-based authorization downstream has
  nothing to key on for calls attributed to acme-agent.
- `keycloak.consoleClientId` (default `openshift-console`): confidential
  client registered as the `console` OIDC platform client when
  `openshiftOIDC.enabled` is true. See "OpenShift Native OIDC" below.

`acme-agent` authenticates with a SPIFFE JWT-SVID (no
static secret) via Keycloak's federated client authentication feature
(`clientAuthenticatorType: federated-jwt`), against the `spiffe` identity
provider this chart also creates (`keycloak.spiffeIdentityProvider.*`). This
is fully declarative -- no manual Admin Console step is required for it, and
the `jwt.credential.sub` value each client expects is computed from
`keycloak.acmeWorkload` (the namespace/ServiceAccount
pair ZTWIM/SPIRE issues that workload's JWT-SVID for) and the SPIFFE trust
domain. The trust domain, like the issuer URL and console route, is derived
from `global.clusterBaseDomain` (`apps.<base>`) when
`keycloak.spiffeIdentityProvider.trustDomain` is left empty -- see the
domain-derivation block in `templates/_helpers.tpl`; set it explicitly only to
override. Get any of those values wrong and Keycloak rejects the assertion with
a generic "Invalid client or Invalid client credentials" -- there is no more
specific error surfaced to the caller.

Two related gotchas, both baked into this chart's templates already but
worth knowing when debugging directly against Keycloak:

- Keycloak's JWT client validators reject the token request outright
  ("client_id parameter does not match sub claim") if a `client_id` form
  parameter is present and differs from the assertion's `sub` -- which it
  always will for a SPIFFE assertion, since `sub` is a SPIFFE ID, never the
  Keycloak client_id. The agent's code omits `client_id` from these
  requests entirely; the client is resolved from the assertion's `sub`.
- The SPIFFE JWT-SVID used as `client_assertion` must itself be requested
  with the Keycloak realm issuer URL as its audience (`SPIFFE_JWT_AUDIENCE`
  in the agent chart) -- that is what Keycloak's federated-jwt validator
  checks the assertion's `aud` claim against by default, not the workload's
  own name.

## Dedicated audience for MAS inbound A2A

The `openshift-cli` and `acme-agent` clients have an `mas-a2a-audience` mapper
whose custom audience defaults to `keycloak.masA2aAudience` (`mas-a2a`). The
ACME mapper lets the same client-credentials token pass MAS A2A validation.
This is separate from the `openshift` API audience. Configure the MAS A2A
chart's `identity.keycloakAudience` to the audience present in its caller's
access token. MAS additionally checks the exact validated `sub` against
`a2a.allowedPrincipals`; the mapper does not grant access by itself.

## Dedicated audience for the public MAS API

The `openshift-cli` client also has a `mas-api-audience` mapper, defaulting to
`keycloak.masApiAudience` (`mas-api`). The separately exposed `mao-api` chart
should validate this audience in Keycloak mode and restrict administrative
operations with its exact `identity.adminAllowedUsers` allow-list. Do not reuse
`mas-a2a` for the API. On an already-imported realm, add an Audience protocol
mapper to the actual human administrator token-issuing client, with Included
Custom Audience `mas-api` and Access Token Claim enabled; the realm import does
not reconcile existing clients after the initial import.

**Existing-realm caveat:** `KeycloakRealmImport` only applies when the realm
does not already exist; a later sync does not add this mapper to existing
`openshift-cli` or `acme-agent` clients. Before enabling MAS A2A on an already
imported realm, inspect a caller token's `iss`, `aud`, and `sub`. If `aud`
does not contain the configured MAS audience, manually add an Audience
protocol mapper to the actual token-issuing client in Keycloak's Admin
Console, with Included Custom Audience set to the same value as
`keycloak.masA2aAudience` and Access Token Claim enabled. Do not delete/re-import
a live realm merely to update the mapper. If a different client issues the
caller token, add the mapper to that client (or an appropriate client scope)
instead of assuming either default mapper applies.

### External Red Hat SSO token exchange

Keycloak 26.4's standard V2 token exchange validates an external subject token
with introspection and userinfo, not JWKS alone. When configuring
`keycloak.externalOidcProviders` for Red Hat SSO, include the issuer's
`authorizationUrl`, `tokenUrl`, `jwksUrl`, `tokenIntrospectionUrl`, and
`userInfoUrl`. The standalone variant configures all five endpoints for both
SSO environments. ACME must request `openid api.ask_red_hat`; with only
`api.ask_red_hat`, Red Hat's userinfo endpoint returns 403 and the caller
sees only the generic `Keycloak token exchange failed` error.

The exchanging caller should send the external issuer URL as
`subject_issuer`. Keycloak accepts
the issuer URL directly, which avoids depending on an imported IdP alias that
can drift from chart values on a one-shot realm import.

**Live finding (2026-10-29, historical -- recorded while the removed
lightspeed-mcp exchange path still existed; the introspection requirement it
established still applies to any caller of these IdPs): stage introspection
also needs the external
client secret in Keycloak's IdP configuration.** Requests at 15:31, 15:33,
and 15:34 UTC reached Keycloak as `lightspeed-mcp` (the since-removed
exchanging client) using `federated-jwt`
authentication and the correct stage `subject_issuer`, but failed with
`Introspection endpoint call failure. Introspection response status: 401`.
Red Hat SSO returned `Authentication failed.` The live `red-hat-sso-stage`
IdP had the same client ID as ACME but no `config.clientSecret`. A direct
probe with a freshly minted stage token reproduced HTTP 401 using the IdP's
credentials; the same token returned HTTP 200 with `active: true` using
ACME's stage client ID and secret. Userinfo also returned HTTP 200, ruling
out the separate missing-`openid` failure above.

The chart previously rendered only the external client ID and some endpoints;
the PostSync hook managed only group mappers. The old chart tests checked
those rendered fields without exercising authenticated introspection, so they
passed despite the missing credential. Authorization and token URLs were also
absent. Native Keycloak subject tokens avoid this external hop, explaining why
that path worked.

The pattern now supplies the IdP client ID and secret from the **same Vault
record ACME uses**, with no new credentials to provision:

```yaml
credentials:
  vaultKey: secret/data/global/acme-agent-redhat-sso-stage
```

The chart creates a per-provider ExternalSecret in `keycloak.namespace` with
keys `client-id` and `client-secret`. Vault property names default to those
same names; override `credentials.clientIdProperty` and
`credentials.clientSecretProperty` when needed. Alternatively, set
`credentials.existingSecretName` to a Secret in the Keycloak namespace with
those keys; this is mutually exclusive with `vaultKey`. Never put a real
client secret into Helm values. The standalone variant uses the existing
production and stage Vault records already documented in
`values-secret.yaml.template`.

Fresh imports resolve both credentials through `KeycloakRealmImport`
placeholders. On every ArgoCD sync, `rca-realm-secrets-reconciler` also creates
missing IdPs or updates existing ones through the Admin API: endpoints,
credentials, signature validation, token-exchange setting, client auth method,
scope, and group mappers. Issuer matching preserves existing aliases and
unmanaged settings. The default client auth method is `client_secret_post`;
`defaultScope` defaults to `openid` (standalone uses `openid api.ask_red_hat`).
Configured providers are enabled; removing one from the list does not delete
it or its existing group mappers. Secret rotation reaches the Kubernetes Secret
on the ExternalSecret refresh interval, then reaches Keycloak on the **next
ArgoCD sync**; refresh alone does not trigger the PostSync hook. No realm
deletion or re-import is required.

For brokered callers that must receive a specific Keycloak group identity,
add the target
Keycloak group to each provider's `hardcodedGroups` list (the standalone
variant uses `acme-agent-rca` for both Red Hat SSO environments). Both the
realm import and PostSync hook manage a `Hardcoded Group` IdP mapper with
sync mode `FORCE`. Issuer matching intentionally handles alias drift such as the live
`rh-sso-stage` alias versus the chart's `red-hat-sso-stage` value.

The mapper settings are:

| Setting | Value |
| --- | --- |
| Name | `acme-agent-rca` |
| Sync Mode Override | `Force` |
| Mapper Type | `Hardcoded Group` |
| Group | `/acme-agent-rca` |

After the mapper runs and a **fresh** token exchange occurs (existing access
tokens are not retroactively changed), the exchanged token must contain
`groups: ["acme-agent-rca"]`; otherwise downstream group-based authorization
sees an individual `keycloak:<subject>` user and the group binding
does not apply.

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
   as the confidential `console` client). The console client's secret is
   also fully automated when `keycloak.consoleClientSecretVaultKey` is set
   (see below); leave it empty to fall back to the manual step.

   `KeycloakRealmImport`'s `$(CONSOLE_CLIENT_SECRET)` placeholder substitution
   (used to set this automatically) only ever gets one shot, at import time --
   if it loses a startup race against the Secret it reads from (observed in
   practice on a fresh cluster: `ClusterSecretStore` wasn't `Ready` yet), it
   silently bakes in the literal placeholder text as the client's real secret
   instead, with `KeycloakRealmImport` still reporting `Done: True`.
   `realm-secrets-reconciler-job.yaml`, an ArgoCD `PostSync` hook, re-asserts
   this value (and `consoleAdminUser`'s password, below) directly against the
   running realm on every sync using `keycloak.adminUser`'s master-realm
   credentials, so this self-heals on the next sync rather than requiring a
   manual Admin REST API/Console fix. Check `oc get job -n
   <keycloak.namespace> <keycloak.realm>-realm-secrets-reconciler` if you
   suspect it hasn't run.

   The same hook also re-asserts the `openshift-console` client's
   `redirectUris` whenever `openshiftOIDC.enabled` is true (to
   `<effective console route>/auth/callback`, matching what the realm import
   would have rendered). This covers the common case where the realm was
   imported while `openshiftOIDC.enabled` was still false (the break-glass
   sequence enables it only after the first import): the one-shot import is
   applied only when the realm does not already exist, so it never adds the
   redirectUris retroactively, and console login fails with Keycloak's
   `Invalid parameter: redirect_uri` until the reconciler runs. Because this
   path only needs the console client (not its Vault secret), the hook now
   also runs when `openshiftOIDC.enabled` is true even if
   `keycloak.consoleClientSecretVaultKey` is empty.

    When `openshiftOIDC.caBundleSync.enabled` is true, this chart copies the
    configured source CA bundle (by default
    `openshift-config-managed/default-ingress-cert`, key `ca-bundle.crt`) into
    `openshift-config` and `keycloak.namespace`. The first copy is an Argo CD Sync
    hook that completes before `Authentication/cluster` is applied; a CronJob
    refreshes both ConfigMaps on the configured schedule so signer CA rotations
    propagate. Set `openshiftOIDC.caConfigMapName` to the generated ConfigMap name.
   Set `realmSecretsReconciler.caBundleConfigMapName` to the same name to mount
   the local copy into the reconciler and keep its TLS verification enabled.
   This assumes the source bundle is the CA that issued the Keycloak route
   certificate; configure the source values if the route uses a different CA.
    Avoid `realmSecretsReconciler.skipTlsVerify: true` except as a last resort:
    it disables certificate and hostname verification for that Job, and does
    not affect the RCA agent or OpenShift's OIDC verification.

    `caBundleSync.additionalTargetNamespaces` is an opt-in list for other
    workloads that must trust this same ingress signer. For example, set it to
    `[mao]` when MAS Keycloak CA trust is enabled; the bootstrap Job and
    rotation CronJob will publish the same `keycloak-oidc-ca` ConfigMap there,
    and this chart creates namespace-local Roles (plus the existing source
    read Role). `get`/`patch` are restricted to the named ConfigMap, but
    Kubernetes RBAC cannot limit `create` by resource name: the sync account
    can create **any** ConfigMap in each listed target namespace. Only add
    namespaces where that bootstrap permission is acceptable. The list
    defaults empty; no extra namespace receives the bundle or permissions
    unless explicitly configured.
    MAS consumes that ConfigMap through a read-only volume and builds a bundle
    that extends its image's existing trust roots, with TLS verification still
    enabled. The CronJob refreshes the ConfigMap on its schedule after signer
    rotation; Kubernetes updates the projected volume, but a running Python
    process may cache its JWKS client/SSL context, so roll MAS pods after
    rotation to ensure all requests use the new bundle. This CA path is a
    deployment prerequisite only and has not been live-validated.

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
(for example `cluster-admins` -- named plainly, since cluster-admin access
granted this way has nothing to do with the RCA agent) to have this chart
create that group in the realm
and bind it to `cluster-admin` via a `ClusterRoleBinding`, then add your own
Keycloak user to that group (Admin Console → Users → your user → Groups →
Join Group) *before* enabling OIDC. This binding is created unconditionally
whenever `keycloak.adminGroupName` is set, independent of
`openshiftOIDC.enabled`, so it's ready by the time you flip the switch.

Setting `keycloak.consoleAdminUser.enabled=true` (and
`keycloak.consoleAdminUser.passwordVaultKey`) automates the "add your own
Keycloak user" step above instead: this chart creates a human user
(`keycloak.consoleAdminUser.username`, default `cluster-admin`) directly in
`keycloak.adminGroupName`, with an initial password read from Vault via
External Secrets Operator (`console-admin-user-secret.yaml`). Keycloak forces
a password change on first login (`temporary: true`), so this is a bootstrap
credential, not a long-term one. Get the initial password with:

```bash
oc get secret <keycloak.consoleAdminUser.passwordSecretName, default "console-admin-user"> \
  -n <keycloak.namespace, default "keycloak-system"> -o jsonpath='{.data.password}' | base64 -d
```

This is opt-in and on top of, not a replacement for, CLI break-glass above --
a standing cluster-admin credential is a permanent addition to the cluster's
attack surface.

Note the same one-shot limitation as the rest of the realm import: setting
`keycloak.adminGroupName` after the realm has already reached `Done: True`
will not retroactively create the group (see the checklist below and the
note at the end of this file). If that's already happened, create the group
manually once in the Admin Console with the same name instead -- the
`ClusterRoleBinding` itself is a normal, always-reconciled resource and
doesn't have this limitation.

Unlike the group itself, `consoleAdminUser`'s *password* is covered by
`realm-secrets-reconciler-job.yaml` (see above): if the initial
`$(CONSOLE_ADMIN_PASSWORD)` substitution ever loses its startup race and gets
baked in literally, or the underlying Vault value changes later, this
PostSync hook resets it back to the current Vault value on the next sync.
The user itself still isn't retroactively created if `consoleAdminUser` is
enabled after the realm already imported -- only its password self-heals
once the user exists.

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

   This manual recovery is automated by the `realm-import-healer` CronJob
   (`realm-import-healer-{cronjob,script,rbac}.yaml`, `files/heal_realm_import.py`),
   on by default via `realmImport.selfHeal.enabled`. Every
   `realmImport.selfHeal.schedule` (default every 5 min) it checks: if the
   `Keycloak` CR is `Ready` and the `KeycloakRealmImport` is in `HasErrors`,
   it deletes the import so ArgoCD's self-heal recreates it against the
   now-Ready CR -- exactly the `oc delete` above, unattended. It only ever
   deletes an import already in `HasErrors` (pinning its uid), never one that
   is healthy or mid-import, and has `get`/`delete` on just those two named
   objects. It also recovers any *later* recurrence (e.g. an operator
   reconcile that re-breaks the import), not just the install-time race. Set
   `realmImport.selfHeal.enabled: false` to disable it and recover by hand.

2. Confirm the realm is actually reachable at the issuer URL you're about to
   set:

   ```bash
   curl -sk https://<keycloak-route>/realms/<realm>/.well-known/openid-configuration
   ```

   A `{"error":"Realm does not exist"}` 404 here means step 1 hasn't
   succeeded yet -- do not proceed.

3. Get the `openshift-console` client's secret into the Secret `oidcClients`
   expects, in the `openshift-config` namespace (not this chart's namespace):

   - **If `keycloak.consoleClientSecretVaultKey` is set** (recommended for a
     fresh install): nothing to do here -- `console-client-secret.yaml`
     already created it via External Secrets Operator, and
     `keycloak-realm-import.yaml`'s `$(CONSOLE_CLIENT_SECRET)` placeholder set
     the same value on the client at import time, with
     `realm-secrets-reconciler-job.yaml` re-asserting it on every sync as a
     backstop (see above) if that one-shot substitution ever loses its
     startup race. Confirm it exists, and that the two actually match:

     ```bash
     oc get secret <openshiftOIDC.consoleClientSecretName, default "openshift-console-oidc"> \
       -n openshift-config
     oc get job -n <keycloak.namespace, default "keycloak-system"> \
       <keycloak.realm, default "rca">-realm-secrets-reconciler
     ```

     A login failing with "Authentication error" / the console logging
     `unable to verify auth code with issuer: ... "Invalid client or Invalid
     client credentials"` despite this Secret existing means the two are out
     of sync -- check whether the reconciler Job actually completed
     successfully.

   - **Otherwise**: get the client's Keycloak-generated secret by hand
     (Admin Console → Clients → `openshift-console` → Credentials tab →
     Client secret) and create the Secret yourself:

     ```bash
     oc create secret generic openshift-console-oidc \
       -n openshift-config \
       --from-literal=clientSecret='<value from the Credentials tab>'
     ```

     If the realm already existed before you set `consoleClientSecretVaultKey`
     (the one-shot caveat on that value in `values.yaml`), you no longer need
     this manual step either: set it and `keycloak.consoleAdminUser.enabled`
     as needed, sync, and `realm-secrets-reconciler-job.yaml` will set the
     existing `openshift-console` client's secret to the Vault-backed value
     directly, the same way it self-heals a botched one-shot substitution
     above.

   Either way, the secret name must match `openshiftOIDC.consoleClientSecretName`,
   and the key must be literally `clientSecret` (required by the
   `Authentication` CRD's `oidcClients[].clientSecret` field).

4. The **live** `openshift-console` client must have
    `https://<console-route>/auth/callback` in its Valid redirect URIs, or
    browser login fails with `Invalid parameter: redirect_uri`. Checking the
    `KeycloakRealmImport` manifest is not enough: when the realm was imported
    with `openshiftOIDC.enabled=false`, this chart omitted `redirectUris`, and
    enabling OIDC later updates only the one-shot import CR, not the existing
    client. You do **not** need to add this by hand: the secrets reconciler
    (`realm-secrets-reconciler-job.yaml`) re-asserts the console client's
    `redirectUris` on every sync once `openshiftOIDC.enabled` is true, so the
    next sync after enabling OIDC fixes it automatically. To confirm it took
    effect, check the live client in the Admin Console (Clients →
    `openshift-console` → Settings → Valid redirect URIs).

5. Only then set `openshiftOIDC.enabled=true` and sync. `openshiftOIDC.issuerURL`
   and `openshiftOIDC.consoleRoute` are derived from `global.clusterBaseDomain`
   (`https://keycloak.apps.<base>/realms/<realm>` and
   `https://console-openshift-console.apps.<base>`); set them explicitly only to
   override the derivation.

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

### Console shows an invalid redirect URI after enabling OIDC


On a live cluster, the console route and Keycloak issuer both returned HTTP
200, the operators reported Available, and `KeycloakRealmImport/rca` reported
Done. Yet browser login failed: Keycloak logged `LOGIN_ERROR` with
`clientId="openshift-console"`, `error="invalid_redirect_uri"`, and the
console's `/auth/callback` URL. The live client's `redirectUris` was `[]`,
while the current import CR already contained the correct URL. The realm had
been imported before OIDC was enabled, when the template's conditional
`redirectUris` block did not render; updating that one-shot CR later did not
update the client. At the time, the pre-flight checks only covered the issuer
and client secret, and the post-sync reconciler only reasserted secrets, so
neither caught this mismatch.

The fix is to repair the **existing client**, never the realm import (do not
delete the realm or re-import it to repair a client's redirect list). This is
now automated: `realm-secrets-reconciler-job.yaml` re-asserts the
`openshift-console` client's `redirectUris` to
`<effective console route>/auth/callback` on every sync whenever
`openshiftOIDC.enabled` is true, so a plain ArgoCD sync repairs it -- no manual
step. If you need it fixed before the next sync (e.g. to log in right now), do
the same thing by hand in the Keycloak Admin Console: Clients →
`openshift-console` → Settings → Valid redirect URIs, add the exact
`https://<console-route>/auth/callback` value and save. Then retry console
login; if it still fails, check Keycloak events for a different error.

## Spoke-gateway token exchanger (B1, proposed)

`keycloak.spokeGatewayExchanger` (disabled by default) renders the Keycloak
prerequisites for the per-spoke Praxis spoke-gateways' RFC 8693 token
exchange (`charts/all/praxis-proxy`'s `spokeGateways`; see
`PLAN-lightspeed-hub-migration.md` "B1 wiring" and the praxis-proxy chart
README's UNCONFIRMED GUESS list). When enabled it adds to the realm import:

- a confidential client (`praxis-spoke-gateway` by default) with
  `standard.token.exchange.enabled: "true"` and a Vault-backed secret
  (`vaultKey`), materialized into both `keycloak.namespace` (realm-import
  placeholder) and `targetNamespace` (`praxis-proxy`, where the gateway
  pods read it) via `spoke-gateway-exchanger-secret.yaml`;
- one audience mapper per `spokeAudiences` entry on that client (the
  per-spoke audiences for exchanged Token U);
- optionally (`subjectTokenAudienceMapper`, default true) an audience mapper
  on the **acme-agent** client putting the exchanger's client ID on Token A
  -- per the plan's documented exchange prerequisite, the subject token must
  already carry the exchanging client as an audience; the `audience`
  request parameter only ever narrows, never adds.

Caveats recorded from the plan: omit `client_id` for SVID
client-assertion flows (a SPIFFE `sub` never equals a Keycloak `client_id`);
after an exchange `sub` is preserved but **`azp` becomes the exchanger**, so
hub/spoke policy must key on `sub`/groups and record `azp` as provenance
only. Whether mappers on the exchanging client apply to *exchanged* tokens
is an UNCONFIRMED GUESS -- validate against a live realm.

**One-shot realm import.** Like everything in `keycloak-realm-import.yaml`,
these changes only apply when the realm is first created. On the standalone
cluster, whose realm already imported, the client, secret, and mappers must
be created manually in the Admin Console (or the realm re-imported) when B1
is enabled; the realm-secrets-reconciler does not re-assert them.

## Hub A2A adapter token exchanger (Task B / Phase 1, disabled)

`keycloak.hubExchange` (disabled by default) renders the H-side Keycloak
prerequisites for the hub A2A adapter's A→H token exchange
(`charts/all/lightspeed-hub`'s `adapter.keycloak.clientId`; see
`PLAN-lightspeed-hub-migration.md` Phase 1 "First exchange" and
`docs/lightspeed-hub-plan.html` s3). On this leg the **adapter** performs
the exchange — Praxis validates Token A and routes only (see
charts/all/praxis-proxy's `a2aUpstream.exchangeAtPraxis` for why the
Praxis-side exchange is unsupported). When enabled it adds to the realm
import:

- a confidential client (`lightspeed-hub-adapter` by default, must equal
  the lightspeed-hub chart's `adapter.keycloak.clientId`) with
  `standard.token.exchange.enabled: "true"` and a Vault-backed secret
  (`vaultKey`), materialized into both `keycloak.namespace` (realm-import
  placeholder) and `targetNamespace` (`lightspeed-hub`, where the adapter
  reads it as `adapter.keycloak.clientSecretSecret.name`) via
  `hub-exchange-secret.yaml`;
- an audience mapper putting `hubAudience` (must equal the lightspeed-hub
  chart's `adapter.hubAudience`) on the exchanged Token H;
- group-membership protocol mappers (`groupMappers`, default true) so
  **exchanged** tokens (H) carry the principal's Keycloak groups while
  minted Token A stays groupless — the plan's Phase 1 contract ("separate
  mint/exchange client scopes and group mappers so initial A omits groups
  while H/U include them"). Note the existing `groups` mapper on the
  acme-agent client predates that contract; today's live Token A still
  carries `acme-agent-rca`. Reaching the plan's groupless-A target means
  moving that mapper off the mint path too — deliberately NOT done here
  (it would change every live consumer of Token A) and marked as part of
  the same live-realm spike;
- optionally (`subjectTokenAudienceMapper`, default true) an audience
  mapper on the **acme-agent** client putting the exchanger's client ID on
  Token A (RFC 8693 prerequisite (b): the subject token must already carry
  the exchanging client as an audience).

**Audience distinctness is fail-guarded:** `hubAudience` must be distinct
from `openshift`, `masA2aAudience`, `masApiAudience`, the exchanger's own
client ID, and every `spokeGatewayExchanger.spokeAudiences` entry — a
collision would make a token minted for one consumer silently valid for
another.

**CONFIRMED LIVE (2026-11-20, was plan spike item #5):** mappers on the
exchanger client DO apply to RFC 8693-*exchanged* tokens on this Keycloak
build. Exchanging a freshly minted acme-agent Token A as
`lightspeed-hub-adapter` (no `audience` parameter) returned H with
`aud: "lightspeed-hub-a2a"`, `azp: lightspeed-hub-adapter`, the caller's
`sub`/`preferred_username` (`service-account-acme-agent`) and
`groups: ["acme-agent-rca"]`. **Claim-map note:** if a Praxis policy ever
inspects H's groups, PPE's `keycloak` claim_mapper preset maps *realm
roles*, not groups — an explicit claim_map entry would be needed; nothing
on this leg reads H's groups at Praxis (the hub API's OIDC group mapping
does).

**One-shot realm import.** Like everything in
`keycloak-realm-import.yaml`, these changes only apply when the realm is
first created. On the standalone cluster, whose realm already imported,
the client, secret, and mappers must be created manually in the Admin
Console when this is enabled; the realm-secrets-reconciler does not
re-assert them.

## Audience-only clients (exchange target audiences)

Keycloak 26 standard token exchange requires the `audience` request
parameter to be the **client ID of an existing client**. The hub and spoke
audiences (`hubExchange.hubAudience`, each
`spokeGatewayExchanger.spokeAudiences` entry) name *resource servers* -- the
hub/spoke kube API servers that accept them via
`openshiftOIDC.extraAudiences` -- not applications, so they used to exist
only as custom strings in audience mappers. The realm import now renders one
**audience-only client** per requested audience: enabled, confidential, with
every flow off (no standard/implicit/direct grants, no service account), so
nothing can log in or mint tokens as it. A name that collides with a real
client ID fails the render. The exchanged token's `aud` still comes from the
exchanging client's audience mapper; the client only makes the name
resolvable.

**VERIFIED-BROKEN LIVE (2026-11-20), how it was hit:** after the A2A card
fix let MAS reach the adapter, every delegation logged `token exchange
failed ... kind=invalid_client http=400`. The client's own credentials were
fine (client_credentials succeeded) and Token A carried
`lightspeed-hub-adapter`; replaying the adapter's exact request against
Keycloak returned `{"error":"invalid_client","error_description":"Audience
not found"}` for `audience=lightspeed-hub-a2a`, while the same exchange
without `audience` succeeded. Multiple audiences on Token A are NOT the
problem. **Why nothing caught it sooner:** the chart tests only checked the
rendered mapper strings, the adapter's exchange tests use a fake token
endpoint, and Keycloak reports this as `invalid_client` -- which reads like
a bad client secret. `praxis-spoke-hub` (the spoke gateway's requested
audience) had the same gap and would have failed next.

**Live realm (one-shot import): create them once by hand.** On a realm
that has already imported, add each client in the Admin Console (realm
`rca` -> Clients -> Create client):

1. Client type `OpenID Connect`, Client ID `lightspeed-hub-a2a` (then again
   for `praxis-spoke-hub`), Next.
2. Client authentication **On**; uncheck **every** authentication flow
   (Standard flow, Direct access grants, Implicit, Service accounts, OAuth
   2.0 Device, OIDC CIBA), Next, Save.

Or with `kcadm.sh` from the Keycloak pod, after `kcadm.sh config
credentials` with a working admin login:

```bash
for aud in lightspeed-hub-a2a praxis-spoke-hub; do
  kcadm.sh create clients -r rca -s clientId=$aud -s enabled=true \
    -s publicClient=false -s standardFlowEnabled=false \
    -s implicitFlowEnabled=false -s directAccessGrantsEnabled=false \
    -s serviceAccountsEnabled=false
done
```

Note: the `keycloak-system/keycloak-admin-user` secret's password was found
stale on the standalone cluster (2026-11-20, `invalid_grant` for `admin`),
so use whatever admin login you currently have.

## Audience-scoped exchange roles (`keycloak.exchangeRoles`)

Kubernetes RBAC binds groups, not audiences. With a plain group mapper, every
exchanged token (H for the hub API, U for each spoke API) carries the same
`groups`. On a hub that is its own spoke, U could then create AgenticRuns and
H could read the whole cluster. With `exchangeRoles.enabled`:

- client roles on each exchange target audience client (`hubRoles` on
  `hubExchange.hubAudience`, `spokeRoles` on every `spokeAudiences` entry);
- the realm groups in `exchangeRoles.groups` get all of them;
- each exchanger gets `fullScopeAllowed: false` plus role scope mappings for
  only its audiences, and a client-role mapper projecting **only its
  audience's roles** into `groups`, prefixed `"<audience>:"`. This replaces
  the hub exchanger's raw group mapper.

**CONFIRMED LIVE (2026-11-20):** from one fresh Token A, H carried
`groups: ["lightspeed-hub-a2a:agenticrun-creator"]` and U carried
`["praxis-spoke-hub:reader"]`. The hub API's `SelfSubjectReview` mapped them
to `keycloak:lightspeed-hub-a2a:agenticrun-creator` and
`keycloak:praxis-spoke-hub:reader` (same user). Bind RBAC to those groups
(charts/all/lightspeed-hub `adapter.hubRunCreatorsGroup`,
`admissionPolicy.allowTargets`, `spokeReaderRBAC`), never to the user. Full
design: `docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md`.

**Live realm (one-shot import):** applied via the admin API on 2026-11-20.
For another already-imported realm, for each `(audience, role, exchanger)`:

1. `POST /admin/realms/<realm>/clients/<audience-uuid>/roles` `{"name": "<role>"}`;
2. `POST .../groups/<group-id>/role-mappings/clients/<audience-uuid>` `[<role rep>]`;
3. on the exchanger: `fullScopeAllowed: false` (PUT the client), then
   `POST .../clients/<exchanger-uuid>/scope-mappings/clients/<audience-uuid>` `[<role rep>]`;
4. add a mapper on the exchanger: `oidc-usermodel-client-role-mapper` with
   `usermodel.clientRoleMapping.clientId=<audience>`,
   `usermodel.clientRoleMapping.rolePrefix=<audience>:`, `claim.name=groups`,
   `multivalued=true`, access token only; and delete any
   `oidc-group-membership-mapper` on the exchanger.

**Known gap -- SSO callers on the hub leg:** Praxis still accepts Red Hat SSO
tokens (`api.ask_red_hat`) on this leg, and Keycloak trusts the SSO IdPs
(`externalOidcProviders`), but the adapter's exchanger never sends
`subject_issuer`, which external-token exchange needs (see "External Red Hat
SSO token exchange" above). An SSO-authenticated request reaching the
adapter is therefore expected to fail its A->H exchange until the adapter
sets `subject_issuer` from the token's `iss` -- UNCONFIRMED, not yet tested
live.
