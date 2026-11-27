# Keycloak/OIDC integration for ACME and optional MAS A2A

This companion chart is installed after the validated pattern's `rhbk` chart.
It imports the `rca` realm, its three clients, a SPIFFE identity provider, and
its groups into the deployed Keycloak.

> OLS-era note: this chart also used to create `lightspeed-mcp`/`openshift-mcp`
> clients; OLS was removed (`PLAN-lightspeed-hub-migration.md` Task A), and
> cleaning a live realm is a manual Admin API step in that plan's teardown
> runbook.

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

Two client-auth rules are already baked into the templates (both hit live;
details in [`docs/LESSONS_LEARNED.md` §2](../../../docs/LESSONS_LEARNED.md#2-keycloak-realm-import-and-clients)):

- omit the `client_id` form parameter on federated-JWT requests -- Keycloak
  rejects it when it differs from the assertion's `sub`, and a SPIFFE `sub`
  never equals a Keycloak client id;
- request the JWT-SVID with the **realm issuer URL** as its audience
  (`SPIFFE_JWT_AUDIENCE`), which is what the federated-jwt validator checks.

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

**Introspection needs the external client secret too.** Stage exchange reached
Keycloak with valid SPIFFE client authentication but Red Hat SSO introspection
returned 401 (`Introspection endpoint call failure`) because the live IdP had
the client ID but no secret. The chart now renders the full endpoint set and
reuses ACME's Vault record for the IdP credentials, so discovery/authorization/
token/introspection/userinfo and the secret all come from one source.

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
alongside OIDC (`spec.type` is mutually exclusive). Recovery is possible:
existing `IdentityProvider` entries on `oauth.config.openshift.io/cluster`
are not deleted, so `spec.type: ""` (see "Recovering if it breaks anyway"
below) restores them, and certificate/kubeconfig admin access never goes
through OAuth/OIDC. **Before setting `openshiftOIDC.enabled=true`, confirm
you have that break-glass kubeconfig** -- it is what makes recovery possible.

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

   `KeycloakRealmImport`'s `${CONSOLE_CLIENT_SECRET}` placeholder substitution
   only ever gets one shot, at import time. Older chart versions incorrectly
   used `$(...)`, storing literal placeholders despite `Done: True` (initially
   attributed to a Secret-readiness race; see the verified 2026-11-24 finding
   below). Even with correct syntax, later Vault changes do not update an
   existing realm through the import CR.
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
    `[mao, lightspeed-hub]` for the standalone MAS callers and hub adapter;
    the bootstrap Job and
    rotation CronJob will publish the same `keycloak-oidc-ca` ConfigMap there,
    and this chart creates namespace-local Roles (plus the existing source
    read Role). `get`/`patch` are restricted to the named ConfigMap, but
    Kubernetes RBAC cannot limit `create` by resource name: the sync account
    can create **any** ConfigMap in each listed target namespace. Only add
    namespaces where that bootstrap permission is acceptable. The list
    defaults empty; no extra namespace receives the bundle or permissions
    unless explicitly configured.
    The lightspeed-hub adapter also builds a combined bundle from this source;
    replace its pods after CA rotation to rerun init and reload Go's cached
    trust roots (see that chart's README). Source ConfigMap refresh alone is
    not a workload restart or TLS-context reload.
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
`${CONSOLE_ADMIN_PASSWORD}` substitution was previously stored literally
because an older chart used `$(...)`, or the Vault value changes later, this
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
      `keycloak-realm-import.yaml`'s `${CONSOLE_CLIENT_SECRET}` placeholder sets
     the same value on the client at import time, with
     `realm-secrets-reconciler-job.yaml` re-asserting it on every sync as a
      backstop for older imports and subsequent Vault changes. Confirm the
      Secret exists and that the registered client credential matches it:

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

`invalid_redirect_uri` with `clientId="openshift-console"` means the live
client's `redirectUris` is empty -- typical when the realm was imported while
`openshiftOIDC.enabled` was false. Never delete the realm or re-import it;
`realm-secrets-reconciler-job.yaml` repairs the client on every sync, or add
`https://<console-route>/auth/callback` in the Admin Console to fix it right
now. See [`docs/LESSONS_LEARNED.md` §2](../../../docs/LESSONS_LEARNED.md#2-keycloak-realm-import-and-clients).

## Spoke-gateway token exchanger (B1, proposed)

`keycloak.spokeGatewayExchanger` (disabled by default) renders the Keycloak
prerequisites for the per-spoke Praxis gateways' RFC 8693 exchange
(`charts/all/praxis-proxy`'s `spokeGateways`). When enabled it adds to the
realm import:

- a confidential client (`praxis-spoke-gateway` by default) with
  `standard.token.exchange.enabled: "true"` and a Vault-backed secret
  (`vaultKey`), materialized into `keycloak.namespace` (realm-import
  placeholder) and `targetNamespace` (`praxis-proxy`) via
  `spoke-gateway-exchanger-secret.yaml`;
- one audience mapper per `spokeAudiences` entry on that client (the
  per-spoke audiences for exchanged Token U);
- optionally (`subjectTokenAudienceMapper`, default true) an audience mapper
  on the **acme-agent** client putting the exchanger's client ID on Token A
  -- the subject token must already carry the exchanger as an audience; the
  `audience` request parameter only narrows, never adds.

Caveats recorded from the plan: omit `client_id` for SVID
client-assertion flows (a SPIFFE `sub` never equals a Keycloak `client_id`);
after an exchange `sub` is preserved but **`azp` becomes the exchanger**, so
hub/spoke policy must key on `sub`/groups and record `azp` as provenance
only. Exchanger-client mappers **do** apply to exchanged tokens on this
Keycloak build (confirmed for `hubExchange`; see §2 of the lessons doc).

**One-shot realm import.** Clients and mappers are created only on the first
import; on an existing realm create them once through the Admin Console/API
(never delete/re-import). The PostSync reconciler then aligns and verifies the
existing client's secret on every sync -- see "Setting and repairing
exchanger credentials" below.

## Hub A2A adapter token exchanger (Task B / Phase 1, disabled)

> **Standalone runs the hub-gateway mode instead** (`hubGatewayExchanger`,
> section "Hub gateway exchange" below): Praxis exchanges A→H as `mas-a2a`,
> and this `hubExchange` client (`lightspeed-hub-a2a` there) becomes H's
> audience and the adapter's **H→R** exchanger. The description below is the
> earlier mode, where the adapter exchanged A→H itself as
> `lightspeed-hub-adapter`; it still renders when `hubGatewayExchanger` is
> off. Flow diagrams: `docs/agentic-e2e-token-flow.html`.

`keycloak.hubExchange` (disabled by default) renders the H-side Keycloak
prerequisites for the hub A2A adapter's A→H token exchange
(`charts/all/lightspeed-hub`'s `adapter.keycloak.clientId`; see
`PLAN-lightspeed-hub-migration.md` Phase 1 "First exchange"). In this mode
the **adapter** performs the exchange and Praxis validates Token A and
routes only. When enabled it adds to the realm import:

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

**Confirmed:** mappers on the exchanger client DO apply to RFC 8693-exchanged
tokens on this Keycloak build, so the audience and group mappers above land on
H. Re-check after a Keycloak upgrade (see
[`docs/LESSONS_LEARNED.md` §2](../../../docs/LESSONS_LEARNED.md#2-keycloak-realm-import-and-clients)).

**One-shot realm import.** Clients and mappers are created only on the first
import. On an existing realm, create them once through the Admin Console/API;
the PostSync reconciler repairs and verifies the existing client's secret on
every sync. It does not create clients or change their mappers or permissions.

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

Why these clients exist, and the `invalid_client "Audience not found"` you see
without them, are explained in
[`docs/LESSONS_LEARNED.md` §2](../../../docs/LESSONS_LEARNED.md#2-keycloak-realm-import-and-clients).

**Live realm (one-shot import): create them once by hand.** On a realm that
already imported, add each audience client (defaults `lightspeed-hub-a2a` and
`praxis-spoke-hub`; use whatever your `spokeGateways` keys/exchanger audiences
are) in the Admin Console: realm `rca` -> Clients -> Create client, type OpenID
Connect, client authentication **On**, then uncheck every authentication flow
(Standard, Direct access grants, Implicit, Service accounts, Device, CIBA) and
save. If your current admin login works, the same thing from the pod:

```bash
for aud in lightspeed-hub-a2a praxis-spoke-hub; do
  kcadm.sh create clients -r rca -s clientId=$aud -s enabled=true \
    -s publicClient=false -s standardFlowEnabled=false \
    -s implicitFlowEnabled=false -s directAccessGrantsEnabled=false \
    -s serviceAccountsEnabled=false
done
```

## Hub gateway exchange (`keycloak.hubGatewayExchanger`)

Diagrams of every client and exchange: `docs/agentic-architecture-overview.html`
and `docs/agentic-e2e-token-flow.html`; per-client reference in
`agents/AUTHENTICATION.md` ("The Keycloak clients").

Chain (see `docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md` s2): each token's
audience names the client that exchanges it next, so every exchange needs a
single audience.

| Client | Exchanges | Adds to the issued token | Notes |
|---|---|---|---|
| `acme-agent` (caller) | (mints A) | `aud mas-a2a` only | `fullScopeAllowed: false` (otherwise "audience resolve" leaks the kube audiences into A) |
| `mas-a2a` (`hubGatewayExchanger`) | A -> H (Praxis hub gateway) | `aud lightspeed-hub-a2a`, hub roles in `groups` | confidential, exchange-only; Vault `praxis-hub-exchange`, **alphanumeric** |
| `lightspeed-hub-a2a` (`hubExchange`, clientId = hubAudience) | H -> R (adapter) | `aud praxis-spoke-gateway`, no roles | `runTokenLifespanSeconds` (1800); Vault `lightspeed-hub-adapter` (same secret as before) |
| `praxis-spoke-gateway` (`spokeGatewayExchanger`) | R -> U (spoke gateway, per call) | `aud praxis-spoke-<id>`, spoke roles in `groups` | alphanumeric secret |

Guards enforce the contract: `hubGatewayExchanger.clientId` equals
`masA2aAudience`, `hubExchange.clientId` equals `hubAudience`, both
`subjectTokenAudienceMapper` flags are off (Token A = `[mas-a2a]`), and
`hubExchange.runTokenAudience` is set. Boolean flags are normalized, so
`--set-string ...=false` is handled correctly.

**Verified live (2026-11-20):** exchanged tokens get the exchanging client's
lifespan, so R (1800 s) outlives A (300 s); replaying A->H->R->U with fresh
tokens yields the expected audiences and `keycloak:*` groups, and R and A both
get 401 at the kube APIs. Failure modes to expect if this regresses (wrong
audience parameter, missing `openid` on SSO tokens, URL-encoded secrets) are in
[`docs/LESSONS_LEARNED.md` §2](../../../docs/LESSONS_LEARNED.md#2-keycloak-realm-import-and-clients).

**Live realm (one-shot import):** configured on 2026-11-20 via the admin API:
- the `mas-a2a` client (secret from Vault `praxis-hub-exchange`), with its
  audience and role mappers and the hub-role scope mapping;
- `lightspeed-hub-a2a` turned into a confidential exchanger (secret = the
  adapter's existing Vault value, `standard.token.exchange.enabled`, lifespan
  1800, mapper `aud praxis-spoke-gateway`, no service account).

After cutover, remove the old Token A audience mappers from `acme-agent`
(`lightspeed-hub-adapter`, `praxis-spoke-gateway`) and its lifespan override
(back to 300 s). The `lightspeed-hub-adapter` client is then unused.

### Setting and repairing exchanger credentials

Vault is the source of truth; do not paste real credentials into chart values,
Git, shell arguments or troubleshooting logs. The standalone variant uses:

| Client | Vault path (under `secret/data/global/`) | Vault property | Kubernetes Secret |
|---|---|---|---|
| `mas-a2a` | `praxis-hub-exchange` | `praxis-hub-exchange-client-secret` | `praxis-hub-exchange-client` |
| `lightspeed-hub-a2a` | `lightspeed-hub-adapter` | `lightspeed-hub-adapter-client-secret` | `lightspeed-hub-adapter-client` |
| `praxis-spoke-gateway` | `praxis-spoke-gateway` | `praxis-spoke-gateway-client-secret` | `praxis-spoke-gateway-client` |

1. Keep these entries from `values-secret.yaml.template` in your local
   `~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml`, then
   run `./pattern.sh make load-secrets`. The template generates **missing**
   values; preserve existing Vault values when repairing an imported realm.
   Both Praxis credentials require `alphaNumericPolicy` because the pinned
   delegator does not form-URL-encode Basic-auth credentials.
2. Configure each enabled exchanger's `vaultKey`, `vaultProperty`,
   `secretName` and `targetNamespace` in the variant's Application overrides.
   ExternalSecrets create a `client-secret` key in both `keycloak.namespace`
   and the consumer namespace (`praxis-proxy` or `lightspeed-hub`). Check that
   both ExternalSecrets are Ready and their values match; do not print them.
3. Sync the `keycloak-oidc` Application **with these chart changes**. Fresh
   realms use `${NAME}` references resolved by Keycloak from `spec.placeholders`;
   the PostSync `<realm>-realm-secrets-reconciler` also handles existing realms.
   It runs when any exchanger is enabled, even without console or external IdP
   configuration. Its `reconcile_exchange_secrets.py` helper resolves exact
   client IDs, checks the registered secrets through the Admin API, updates
   only mismatches, and verifies read-back without logging credentials. No
   client-secret regeneration endpoint is used. All clients must already
   exist; a missing client fails the hook instead of creating an incomplete
   exchanger. Check ArgoCD's sync/hook result; successful hook Jobs are deleted
   by `HookSucceeded`, so an absent Job alone is not a failure.
4. After a Vault change, wait for ExternalSecrets to refresh, then explicitly
   sync `keycloak-oidc` again: this is a **PostSync hook**, not a periodic
   credential watcher. Consumers reading secrets through environment variables
   need a separately coordinated rollout **if the value changed**. Repairing a
   literal placeholder to the value consumers already use needs no restart.

The reconciler uses the master-realm credentials in `keycloak.adminUser` and
the TLS settings in `realmSecretsReconciler`. Preserve certificate verification;
configure `caBundleConfigMapName` for a private issuer. To repair before a chart
sync, perform the same narrow Admin API operation: resolve the exact client
with `GET /admin/realms/<realm>/clients?clientId=<client-id>`, then
`PUT /admin/realms/<realm>/clients/<uuid>` with only a `secret` field loaded
in memory from the existing Kubernetes Secret, and verify with
`GET .../<uuid>/client-secret`. Never rotate credentials or delete/re-import
the realm just to correct a stored placeholder.

### Troubleshooting: Praxis 403 `delegation.idp_rejected` / `unauthorized_client`

Praxis rejecting `POST /a2a` while Keycloak logs `TOKEN_EXCHANGE_ERROR` with
`invalid_client_credentials` means the registered client secret in Keycloak
does not match the value Praxis sends -- for example a realm that imported
with older `$(...)` placeholders stored literally. Compare in memory, never
print secrets; then align the client through the steps in "Setting and
repairing exchanger credentials" above (no rotation, no realm re-import).
Restarting Praxis or Keycloak cannot fix a literal value stored in the realm.
Background: [`docs/LESSONS_LEARNED.md` §2](../../../docs/LESSONS_LEARNED.md#2-keycloak-realm-import-and-clients).

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

**Confirmed live (2026-11-20):** from one fresh Token A, H carries
`groups: ["lightspeed-hub-a2a:agenticrun-creator"]` and U carries
`["praxis-spoke-hub:reader"]`; the hub API's `SelfSubjectReview` maps them to
`keycloak:lightspeed-hub-a2a:agenticrun-creator` and
`keycloak:praxis-spoke-hub:reader`. Bind RBAC to those groups
(`adapter.hubRunCreatorsGroup`, `admissionPolicy.allowTargets`,
`spokeReaderRBAC` in `charts/all/lightspeed-hub`), never to the user. Why this
replaced plain group mappers: [`docs/LESSONS_LEARNED.md` §2](../../../docs/LESSONS_LEARNED.md#2-keycloak-realm-import-and-clients).
Full design: `docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md`.

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

## Remediation execution token and role separation (disabled by default)

The read-only and write-capable paths use distinct audiences and exchanger
clients; do not reuse the read client secret or audience:

| Token/hop | Audience | Exchanger / groups |
| --- | --- | --- |
| H (hub API) | `lightspeed-hub-a2a` | `mas-a2a`; includes `agenticrun-creator`, and `agenticrun-approver` only for `exchangeRoles.remediation.groups` |
| R (ordinary run token) | `praxis-spoke-gateway` | `lightspeed-hub-a2a`; accepted by read gateways only |
| U (ordinary spoke token) | `praxis-spoke-<spoke>` audience | `praxis-spoke-gateway`; `reader` only |
| R_exec (Execution run token) | `praxis-spoke-gateway-exec` | `lightspeed-hub-a2a`; accepted by execution gateways only |
| U_exec (Execution spoke token) | same target spoke audience as U | `praxis-spoke-gateway-exec`; `reader` plus `writer` only for a principal with the ordinary and remediation group roles |

Set `hubExchange.executionTokenAudience` equal to
`spokeGatewayExecExchanger.clientId`; it must differ from
`hubExchange.runTokenAudience`. Execution exchanger audiences must exactly
match the read exchanger's registered spoke audience set. The Keycloak chart
fails rendering for mismatched audiences, missing dependencies, or an
execution exchanger without `exchangeRoles`.

With `exchangeRoles.enabled`, ordinary `exchangeRoles.groups` receive only
`hubRoles`/`spokeRoles`; `exchangeRoles.remediation.groups` alone receive the
additional hub `agenticrun-approver` and spoke `writer` roles. The read
exchanger role scope explicitly excludes `writer`; the execution exchanger
scope contains `reader` and `writer`. A designated human who needs to execute
must have both the ordinary group (for creator/reader) and an explicitly
approved remediation group. A remediation group that is not the ACME/admin
group is created **empty** by a fresh import; membership is always a separate
admin action. Do not add the automation service account to the remediation
group as a shortcut, except in an explicitly scoped shared-principal lab:
`exchangeRoles.remediation.includeAcmeServiceAccount: true` is that explicit
opt-in (ACME's own token can then approve and write). By default `writer`
exists on every registered spoke audience;
`exchangeRoles.remediation.spokeAudiences` limits the role, its group grant
and the execution exchanger's writer scope to the listed spokes.

**Existing live realm: one-shot import means the chart will not add clients,
audience mappers, roles, group-role mappings, or exchange scopes.** The
PostSync reconciler only aligns client secrets.
`scripts/remediation/keycloak_execution_brownfield.py` applies the changes
below idempotently (see `docs/A2A_REMEDIATION_BROWNFIELD.md`). Manually,
apply these narrow changes through the Keycloak Admin API
(realm `rca`; use the existing Vault secret value in memory, never in shell
history/logs):

1. Create confidential client `praxis-spoke-gateway-exec`, with standard,
   implicit, direct-access and browser flows disabled, service accounts
   disabled, `standard.token.exchange.enabled=true`, and its secret matching
   the Vault value `secret/data/global/praxis-spoke-gateway-exec` property
   `praxis-spoke-gateway-exec-client-secret`. The ExternalSecrets rendered by
   this chart then materialize the same value into Keycloak and `praxis-proxy`.
2. On client `lightspeed-hub-a2a`, retain the ordinary run-token audience
   mapper (`praxis-spoke-gateway`) and add an Access Token Audience mapper for
   `praxis-spoke-gateway-exec`. Keycloak 26 standard exchange is expected to
   downscope each response to the single requested audience; this R/R_exec
   separation has **not** yet been verified against the live realm.
3. For every registered spoke audience client, ensure it has client roles
   `reader` and `writer`. Assign `reader` to the ordinary
   `exchangeRoles.groups`; assign `writer` only to groups in
   `exchangeRoles.remediation.groups`. On audience client `lightspeed-hub-a2a`,
   similarly assign `agenticrun-creator` to ordinary groups and
   `agenticrun-approver` only to remediation groups. Ensure those groups exist
   in the already-imported realm before mapping.
4. Configure the spoke read exchanger's scope mapping to `reader` only and
   the execution exchanger's scope mapping to `reader` plus `writer` for each
   target audience. Add the client-role-to-`groups` mapper to the execution
   exchanger with `usermodel.clientRoleMapping.clientId=<spoke audience>`,
   `rolePrefix=<spoke audience>:` and `claim.name=groups`; keep the matching
   mapper on the read exchanger limited by its `reader` scope. Configure the
   existing `mas-a2a` hub exchange scope/role mapper for the hub
   `agenticrun-approver` role as well as `agenticrun-creator`.
5. Use the same API sequence as "Audience-scoped exchange roles" above:
   resolve exact client IDs with
   `GET /admin/realms/rca/clients?clientId=<id>`, create missing client roles
   with `POST .../clients/<audience-uuid>/roles`, map group roles with
   `POST .../groups/<group-id>/role-mappings/clients/<audience-uuid>`, and
   scope exchanger roles with
   `POST .../clients/<exchanger-uuid>/scope-mappings/clients/<audience-uuid>`.
   Add protocol mappers via the client's
   `POST .../clients/<exchanger-uuid>/protocol-mappers/models` endpoint. GET
   the resulting roles, scopes and mappers back before changing values.

After those Admin API changes, enable the keycloak-oidc values and wait for
both ExternalSecrets; sync the chart so the PostSync credential reconciler
checks the execution client secret. Then verify fresh exchanges: ordinary R
has only `praxis-spoke-gateway` and cannot reach `praxis-spoke-<key>-exec`; R_exec
has only `praxis-spoke-gateway-exec` and cannot reach the read gateway; U has
the `reader` group only; U_exec has `reader` plus `writer` only for the
designated remediation principal. Chart rendering proves configuration
intent, not these live Keycloak claims.
