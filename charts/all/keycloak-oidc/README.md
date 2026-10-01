# Keycloak/OIDC integration for the ACME agent and OpenShift Lightspeed

This companion chart is installed after the validated pattern's `rhbk` chart.
It imports the `rca` realm, its five clients, a SPIFFE identity provider, and
its groups into the deployed Keycloak, and creates least-privilege,
namespace-scoped RBAC (`templates/lightspeed-mcp-rbac.yaml`) for the group
whose tokens carry OpenShift Lightspeed's exchanged, MCP-scoped access.

The five clients serve distinct purposes and must not be conflated:

- `keycloak.clientId` (default `openshift-cli`): public client for browser/`oc
  login` OIDC flows (Native OIDC). Cannot authenticate itself. Registered as
  the `cli` OIDC platform client when `openshiftOIDC.enabled` is true. Purely
  human/CLI cluster access, unrelated to the agent request flow.
- `keycloak.lightspeedMcpClientId` (default `lightspeed-mcp`): confidential
  client the OpenShift Lightspeed A2A integration (`vendor/lightspeed-service`)
  uses to exchange a caller's token for one scoped to OpenShift MCP.
- `keycloak.mcpAudienceClientId` (default `openshift-mcp`): confidential
  client that exists only so `lightspeed-mcp`'s token exchange has a real
  `client_id` to name as its `audience` parameter -- Keycloak's standard (V2)
  token exchange requires that parameter to be an actual client in the
  realm, not an arbitrary string. Never authenticates itself.
- `keycloak.acmeClientId` (default `acme-agent`): confidential client
  `charts/all/acme-agent` uses for its client-credentials
  call to Keycloak. Its service account belongs to `keycloak.acmeGroupName`
  (default `acme-agent-rca`, named `<caller>-<callee>` rather than just
  `acme-agent` so it reads as "acme-agent's rights when calling
  Lightspeed/MCP") -- without this group, and the `groups` protocol mapper
  this chart attaches to the client, the token this client mints (and,
  unchanged, the token `lightspeed-mcp` exchanges it for) carries no groups
  claim at all, so `templates/lightspeed-mcp-rbac.yaml` has nothing to key on
  for calls attributed to acme-agent.
- `keycloak.consoleClientId` (default `openshift-console`): confidential
  client registered as the `console` OIDC platform client when
  `openshiftOIDC.enabled` is true. See "OpenShift Native OIDC" below.

`lightspeed-mcp` and `acme-agent` authenticate with a SPIFFE JWT-SVID (no
static secret) via Keycloak's federated client authentication feature
(`clientAuthenticatorType: federated-jwt`), against the `spiffe` identity
provider this chart also creates (`keycloak.spiffeIdentityProvider.*`). This
is fully declarative -- no manual Admin Console step is required for it, and
the `jwt.credential.sub` value each client expects is computed from
`keycloak.acmeWorkload`/`keycloak.lightspeedWorkload` (the namespace/ServiceAccount
pair ZTWIM/SPIRE issues that workload's JWT-SVID for) and
`keycloak.spiffeIdentityProvider.trustDomain`. Get any of those three values
wrong and Keycloak rejects the assertion with a generic "Invalid client or
Invalid client credentials" -- there is no more specific error surfaced to
the caller.

Two related gotchas, both baked into this chart's templates already but
worth knowing when debugging directly against Keycloak:

- Keycloak's JWT client validators reject the token request outright
  ("client_id parameter does not match sub claim") if a `client_id` form
  parameter is present and differs from the assertion's `sub` -- which it
  always will for a SPIFFE assertion, since `sub` is a SPIFFE ID, never the
  Keycloak client_id. Both agents' code omits `client_id` from these
  requests entirely; the client is resolved from the assertion's `sub`.
- The SPIFFE JWT-SVID used as `client_assertion` must itself be requested
  with the Keycloak realm issuer URL as its audience (`SPIFFE_JWT_AUDIENCE`
  in both agent charts) -- that is what Keycloak's federated-jwt validator
  checks the assertion's `aud` claim against by default, not the workload's
  own name.
- `lightspeed-mcp`'s actual RFC 8693 exchange additionally needs
  `standard.token.exchange.enabled: "true"` (a client attribute this chart
  sets) plus a protocol mapper adding `keycloak.mcpAudienceClientId` as an
  audience on `lightspeed-mcp` itself, and a protocol mapper on
  `acme-agent` adding `keycloak.lightspeedMcpClientId` as an audience (a real
  client, `included.client.audience`) to the tokens it mints -- Keycloak's
  standard token exchange requires the *subject_token* to already carry the
  exchanging client as an audience, and the `audience` request parameter
  only ever narrows audiences a client scope already resolves, it never adds
  a new one. `acme-agent` also carries a *separate* mapper adding
  `keycloak.lightspeedA2aAudience` as an audience (`included.custom.audience`,
  no client involved) -- that one exists only to satisfy Lightspeed's own A2A
  inbound check and has nothing to do with the exchange itself. All of this
  is templated already; it's listed here because it is not obvious from
  Keycloak's own error messages if you ever need to debug it directly.

## Least-privilege MCP RBAC for the mapped acme-agent-rca group

**If the `rca` realm has already reached `Done: True`, none of this section's
Keycloak-side pieces (the `acme-agent-rca` group, its membership, or the
`groups` protocol mappers on `acme-agent`/`lightspeed-mcp`) take effect
just from the next sync** -- same one-shot limitation as `keycloak.adminGroupName`
(see "Keeping admin access after enabling OIDC" below): `KeycloakRealmImport`
is only applied when the realm doesn't already exist, and isn't continuously
reconciled after that. Check with:

```bash
oc get keycloakrealmimport <keycloak.realm, default "rca"> \
  -n <keycloak.namespace, default "keycloak-system"> -o jsonpath='{.status.conditions}'
```

If it's already `Done: True`, create the `acme-agent-rca` group manually
in the Admin Console, add the `groups` mapper to both `acme-agent` and
`lightspeed-mcp` by hand, and add `acme-agent`'s service account
(`service-account-acme-agent`) to the group -- matching what
`keycloak-realm-import.yaml` declares, so a future full realm re-import
doesn't diverge from what's actually configured.

`templates/lightspeed-mcp-rbac.yaml` grants the mapped `keycloak:acme-agent-rca`
group `get`/`list` on `pods` and `events`, in exactly the namespaces listed
under `lightspeedRbac.namespaces`, and nothing else by default (no pod logs,
no Secrets, no RBAC objects, no write verbs -- see
`LIGHTSPEED_DESIGN.md`'s "Rights to grant (and not grant)" section).
This is OpenShift RBAC on the exchanged Token B's own mapped identity; it has
no relationship to any AgenticRun-style admission policy -- OpenShift
Lightspeed's MCP tools are authorized purely by this RBAC plus whatever
toolset/read-only restrictions the MCP server itself applies.

Verify it's active:

```bash
oc get role,rolebinding -n <a namespace in lightspeedRbac.namespaces> <lightspeedRbac.roleName, default "lightspeed-mcp-investigate">
```

### End-to-end verification

This is genuinely two separate things to verify, only one of which can be
scripted without a live token.

**1. RBAC, via impersonation (fully scriptable, no live Keycloak token
needed).** `oc`'s `--as`/`--as-group` populate `request.userInfo` the same
way a real exchanged token's mapped identity would:

```bash
# Positive: the mapped group may read pods in an allow-listed namespace.
oc auth can-i get pods \
  --as=nobody --as-group=keycloak:acme-agent-rca \
  -n <a namespace in lightspeedRbac.namespaces>

# Negative: the same group has no rights in a namespace outside the
# allow-list -- RBAC alone enforces this, no admission policy needed.
oc auth can-i get pods \
  --as=nobody --as-group=keycloak:acme-agent-rca \
  -n <a namespace NOT in lightspeedRbac.namespaces>
# expect: no

# Negative: no write access even in an allow-listed namespace.
oc auth can-i delete pods \
  --as=nobody --as-group=keycloak:acme-agent-rca \
  -n <a namespace in lightspeedRbac.namespaces>
# expect: no
```

**2. That the real exchanged token actually carries the `groups` claim
(needs a live pod -- this is the part that can't be verified without a
running deployment).** `groups` mappers exist on both `acme-agent` and
`lightspeed-mcp` (see the comment in `keycloak-realm-import.yaml`) precisely
because it isn't verified which client's mappers Keycloak's standard V2
exchange actually applies to the newly-minted, differently-audienced token.
Confirm by decoding a real exchanged token's payload -- **never log or print
the full token, only its decoded claims**:

```bash
# From inside a running OpenShift Lightspeed app-server pod (has httpx and
# the spiffe SDK, per vendor/lightspeed-service's own dependencies):
oc exec -n openshift-lightspeed deploy/lightspeed-app-server -- python3 -c '
import base64, json, os
import httpx
from spiffe import WorkloadApiClient

# 1. Fetch a caller-shaped token the same way acme-agent does, so this
#    reproduces a real exchange rather than asserting expected shape.
#    (Run the equivalent from an acme-agent pod using its own
#    SPIFFE_JWT_AUDIENCE/client id if you want a fully independent check;
#    this abbreviated version assumes you already have a caller token.)
caller_token = os.environ["CALLER_TOKEN_FOR_VERIFICATION"]  # paste one, do not commit it anywhere

with WorkloadApiClient(socket_path=os.environ["SPIFFE_ENDPOINT_SOCKET"]) as c:
    svid = c.fetch_jwt_svid(audience={os.environ["SPIFFE_JWT_AUDIENCE"]})

resp = httpx.post(
    os.environ["KEYCLOAK_TOKEN_URL"] or f"{os.environ[\"KEYCLOAK_ISSUER_URL\"]}/protocol/openid-connect/token",
    data={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": caller_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "client_assertion_type": os.environ["KEYCLOAK_CLIENT_ASSERTION_TYPE"],
        "client_assertion": svid.token,
        "audience": os.environ["KEYCLOAK_TOKEN_EXCHANGE_AUDIENCE"],
    },
)
resp.raise_for_status()
token = resp.json()["access_token"]
payload = token.split(".")[1]
payload += "=" * (-len(payload) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload))
print(json.dumps({k: claims.get(k) for k in ("iss", "aud", "sub", "azp", "groups", "act")}, indent=2))
'
```

Confirm: `iss` is the expected realm, `aud` contains `openshift-mcp`, `sub`
matches the caller's (not the Lightspeed app-server's own) subject, `azp` is
`lightspeed-mcp`, and -- the thing this whole check exists for -- `groups`
contains `acme-agent-rca`. If it doesn't, the belt-and-suspenders mapper
placement didn't work and `lightspeed-mcp-rbac.yaml` has nothing to key on;
see `keycloak-realm-import.yaml`'s comment on the `lightspeed-mcp` client's
`groups` mapper for what to try next.

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
