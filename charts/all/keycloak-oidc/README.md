# Keycloak/OIDC integration for ACME and OpenShift Lightspeed

This companion chart is installed after the validated pattern's `rhbk` chart.
It imports the configured Keycloak realm, human OIDC clients, the SPIFFE identity
provider, the ACME and Lightspeed workload clients/groups, OpenShift native OIDC
settings, and optional AgenticRun/MCP RBAC. The realm defaults to `rca` for
compatibility with the existing issuer configuration; that realm name does not
imply that the retired RCA service is deployed.

The clients have separate purposes:

- `keycloak.clientId` (`openshift-cli`) and `keycloak.consoleClientId`
  (`openshift-console`) are for human `oc login` and console access. They are
  unrelated to workload A2A identity.
- `keycloak.acmeClientId` (`acme-agent`) is ACME's confidential,
  SPIFFE-federated client. Its service-account user is reconciled into
  `keycloak.acmeGroupName` (`acme-agent-lightspeed`), whose OpenShift-mapped
  group is `keycloak:acme-agent-lightspeed`.
- `keycloak.mcpAudienceClientId` (`openshift-mcp`) is the real Keycloak client
  audience requested during RFC 8693 exchange and trusted by the OpenShift API
  server. It does not authenticate.
- `keycloak.lightspeedMcp.clientId` (`lightspeed-mcp`) is the optional
  confidential, SPIFFE-federated Lightspeed exchange client. It is emitted
  only when `keycloak.lightspeedMcp.enabled: true`; its SPIFFE subject must
  match the actual operator-created app-server ServiceAccount.
- `keycloak.groupName` (`agenticrun-users`) is for the separate human
  AgenticRun UI/CLI workflow. It is not granted to ACME and does not authorize
  MCP requests.

The ACME and Lightspeed clients use Keycloak federated client authentication
(`clientAuthenticatorType: federated-jwt`) against the configured `spiffe`
identity provider; no static client secret is needed. The expected SPIFFE
subjects are derived from the configured trust domain and workload
namespace/ServiceAccount. The Lightspeed subject remains unset until the
operator-created ServiceAccount is verified.

For token exchange, Keycloak requires the inbound Token A to carry the real
`lightspeed-mcp` client audience as well as the custom `lightspeed-a2a`
inbound audience. The client assertion is a fresh Lightspeed SPIFFE JWT-SVID
whose audience is the Keycloak realm issuer URL. The exchange omits the
`client_id` form field, uses Token A as `subject_token`, and requests the real
`openshift-mcp` audience. Verify the live Token B claims before enabling RBAC;
this chart's mappers are not proof of runtime Keycloak behavior.

## Optional Lightspeed A2A identity and MCP RBAC

`keycloak.lightspeedMcp.enabled` is **false by default**. Enable it only after
the locally modified Lightspeed service and operator-managed MCP are ready.
The `lightspeed-mcp` client is a confidential, SPIFFE-federated client for the
Lightspeed app-server workload. Its `workload.namespace` and
`workload.serviceAccount` must match the actual ServiceAccount in the pinned
operator deployment; the chart deliberately leaves both unset and fails Helm
rendering if the client is enabled without them. Do not guess the OLM operand's
ServiceAccount name.

When enabled, the imported realm configures:

- `lightspeed-mcp` with federated client authentication, standard token
  exchange, an `openshift-mcp` client-audience mapper, and a `groups` mapper.
- `acme-agent` with `lightspeed-a2a` as a custom inbound audience and
  `lightspeed-mcp` as a real client audience required by Keycloak's exchange.
  The fresh realm contains no RCA client or audience mappers.
- OpenShift `Authentication/cluster` to trust `openshift-mcp` (already in
  `openshiftOIDC.extraAudiences` by default), and to map `sub`/`groups` into
  `keycloak:`-prefixed OpenShift username/group identities.

The Lightspeed A2A handler must validate Token A itself, authenticate as the
`lightspeed-mcp` SPIFFE workload, exchange Token A for Token B, and send only
Token B to MCP. Inspect the real exchanged claims (`iss`, `sub`, `azp`, `aud`,
`groups`) and a real API-server TokenReview before granting permissions. A
group mapper on the exchanging client is not proof that the resulting token
contains the expected original caller's groups.

`mcpAccess.enabled` is also **false by default**. If tool/RBAC review approves
read access, enable only explicit namespace-scoped Roles. Example Helm values
for restricted pod-object reads in `payments` (not a general default; review
actual Pod specs because literal environment values can appear in responses):

```yaml
keycloak:
  lightspeedMcp:
    enabled: true
    workload:
      namespace: openshift-lightspeed
      serviceAccount: <verified-operator-app-server-serviceaccount>

mcpAccess:
  enabled: true
  groupName: keycloak:acme-agent-lightspeed
  namespaces:
    payments:
      - apiGroups: [""]
        resources: ["pods"]
        verbs: ["get", "list"]
```

Do not add pod logs, events, cluster-wide `view`, writes, Secret or RBAC access
without a separate review of the enabled MCP tools and data exposure. Even
`get/list` on Pods can reveal literal environment values, annotations, or
command arguments; read-only tools do not redact those values. The chart
renders one `Role`/`RoleBinding` per configured namespace; an empty
namespace map or empty rule list fails closed. This example assumes
`openshiftOIDC.enabled: true` and the issuer/console fields are configured;
the chart rejects MCP RBAC when the API server is not configured to trust the
Keycloak group mapping. The separate AgenticRun Role and admission policy are
for the human workflow and are not used by ACME or MCP.

`KeycloakRealmImport` is a one-shot import, not a continuous realm reconciler.
For a fresh greenfield realm this is its initial client configuration. The
chart also adds an optional PostSync
`lightspeed-client-reconciler-job.yaml`, activated with
`keycloak.lightspeedMcp.enabled`, which idempotently creates/updates the
Lightspeed exchange client and its mappers, adds the ACME audiences/groups
mapper, and ensures ACME's service-account user is in the configured group.
It uses the existing Keycloak admin Secret and verified issuer CA settings;
never expose the Job logs or environment as credentials evidence. Verify the
Job succeeds, run another sync to confirm idempotency, and then decode a real
exchanged token's claims. The job does not prove Keycloak's live exchange
behavior, OpenShift TokenReview or authorization. Existing realms require a
separate reviewed cleanup because this job adds/updates Lightspeed objects but
does not delete old clients, groups, or mappers.

Validate the default chart, then render the optional Lightspeed client/RBAC
path with the real SPIFFE trust domain, Keycloak issuer and verified operator
ServiceAccount (replace the shell values first):

```bash
export SPIFFE_TRUST_DOMAIN='apps.example.test'
export SPIFFE_JWKS_URL='https://spire.example.test/keys'
export KEYCLOAK_ISSUER='https://keycloak.example.test/realms/rca'
export OPENSHIFT_CONSOLE_ROUTE='https://console-openshift-console.apps.example.test'
export OLS_NAMESPACE='openshift-lightspeed'
export OLS_SERVICE_ACCOUNT='<verified-app-server-serviceaccount>'

helm lint charts/all/keycloak-oidc \
  --set keycloak.spiffeIdentityProvider.trustDomain="$SPIFFE_TRUST_DOMAIN" \
  --set keycloak.spiffeIdentityProvider.bundleEndpoint="$SPIFFE_JWKS_URL"
helm template keycloak-oidc charts/all/keycloak-oidc \
  --set keycloak.spiffeIdentityProvider.trustDomain="$SPIFFE_TRUST_DOMAIN" \
  --set keycloak.spiffeIdentityProvider.bundleEndpoint="$SPIFFE_JWKS_URL" \
  --set openshiftOIDC.enabled=true \
  --set openshiftOIDC.issuerURL="$KEYCLOAK_ISSUER" \
  --set openshiftOIDC.consoleRoute="$OPENSHIFT_CONSOLE_ROUTE" \
  --set keycloak.lightspeedMcp.enabled=true \
  --set keycloak.lightspeedMcp.workload.namespace="$OLS_NAMESPACE" \
  --set keycloak.lightspeedMcp.workload.serviceAccount="$OLS_SERVICE_ACCOUNT" \
  --set mcpAccess.enabled=true \
  --set-json 'mcpAccess.namespaces={"payments":[{"apiGroups":[""],"resources":["pods"],"verbs":["get","list"]}]}'
python3 -m unittest discover -s tests/keycloak -p 'test_*.py' -v
```

## AgenticRun authorization: RBAC + namespace-scoping admission policy

`KeycloakRealmImport` creates the `agenticrun-users` human group and its
`groups` mapper on a fresh realm. It is a one-shot import, not continuous
reconciliation. Human users who need the separate AgenticRun UI/CLI permissions
must be added to this group explicitly; ACME is intentionally not granted
AgenticRun permissions. Check the import status with:

```bash
oc get keycloakrealmimport <keycloak.realm, default "rca"> \
  -n <keycloak.namespace, default "keycloak-system"> -o jsonpath='{.status.conditions}'
```

On a fresh deployment, assign approved human users to `agenticrun-users`
through the Keycloak Admin Console. Enabling the Lightspeed client later uses
the idempotent PostSync reconciler to add the ACME group, audiences and service
account membership; it does not reconcile unrelated human membership.

Two independent RBAC/admission layers, checking two different things:

- `templates/agentic-rbac.yaml` grants every group in `agenticRun.groupNames`
  `create`/`patch`/`get` on `agenticruns` and `get` on `analysisresults`, in
  `agenticRun.namespace`. `patch` is required even though callers only ever
  create AgenticRuns with a fresh, unique name -- the MCP tool backing this
  (`resources_create_or_update`) upserts via Kubernetes Server-Side Apply,
  which the API server always processes as a `PATCH`, even for objects that
  don't exist yet. This is
  coarse: it decides whether a caller may act on the CRD at all, the same way
  any other RBAC grant would.
- `templates/agentic-vap-namespace-scope.yaml` (a `ValidatingAdmissionPolicy`)
  decides which `spec.targetNamespaces` each of those groups may request
  *inside* an `AgenticRun` it's allowed to create, via `agenticRun.namespaceAllowlist`
  (bare group name -> `"*"` or a comma-separated namespace list). RBAC has no
  way to inspect a resource's own spec fields, so it can't express this by
  itself -- without this policy, any caller with RBAC access to create
  `AgenticRuns` could target any namespace in the cluster, regardless of
  which upstream agent it actually represents.

A caller with no `namespaceAllowlist` entry, or one that doesn't cover a
requested namespace, is denied -- deny by default, so a newly onboarded
caller group needs an explicit entry before it can target anything. This
also applies if the caller omits `spec.targetNamespaces` entirely: the CRD
treats that as "not namespace-scoped, the analysis agent decides from
context" (`crds/agentic.openshift.io_agenticruns.yaml`), so the policy
denies a restricted caller that omits the field rather than treating
omission as unscoped -- only a `"*"` allow-list entry may omit it.

Verify both are active:

```bash
oc get role,rolebinding -n lightspeed-agentic-operator agenticrun-user
oc get validatingadmissionpolicy,validatingadmissionpolicybinding | grep agentic
oc get configmap -n lightspeed-agentic-operator keycloak-oidc-agentic-run-namespace-allowlist -o yaml
```

### End-to-end verification

This is genuinely two separate things to verify, only one of which can be
scripted without a live token.

**1. RBAC + the namespace-scoping policy, via impersonation (fully
scriptable, no live Keycloak token needed).** `oc`'s `--as`/`--as-group`
populate `request.userInfo` for the *entire* admission chain, including
`ValidatingAdmissionPolicy` -- so this exercises the same checks a real
exchanged token would, without needing one:

```bash
# Positive: a human member of the AgenticRun group may create a request.
oc auth can-i create agenticruns \
  --as=nobody --as-group=keycloak:agenticrun-users \
  -n lightspeed-agentic-operator

cat <<'EOF' | oc create --dry-run=server -f - \
  --as=nobody --as-group=keycloak:agenticrun-users
apiVersion: agentic.openshift.io/v1alpha1
kind: AgenticRun
metadata:
  name: verify-allowed-namespace
  namespace: lightspeed-agentic-operator
spec:
  request: verification
  targetNamespaces: ["<a-namespace-actually-in-the-allow-list>"]
  analysis:
    agent: default
EOF

# Negative: unauthorized group entirely (no RBAC grant at all).
oc auth can-i create agenticruns \
  --as=nobody --as-group=keycloak:company-b-agent-rca \
  -n lightspeed-agentic-operator
# expect: no

# Negative: this chart's human allow-list is `*`; use a test override with a
# narrow allow-list to verify an out-of-scope namespace is rejected --
# RBAC alone would allow this (it can't see spec fields); the VAP must deny
# it. A rejection here confirms the policy is actually being evaluated, not
# just present.
cat <<'EOF' | oc create --dry-run=server -f - \
  --as=nobody --as-group=keycloak:agenticrun-users
apiVersion: agentic.openshift.io/v1alpha1
kind: AgenticRun
metadata:
  name: verify-denied-namespace
  namespace: lightspeed-agentic-operator
spec:
  request: verification
  targetNamespaces: ["some-namespace-not-in-the-allow-list"]
  analysis:
    agent: default
EOF
# expect: admission webhook "agentic.openshift.io-agenticrun-caller-namespace-scope" denied the request

# Negative: omitted spec.targetNamespaces for a restricted caller --
# confirm this is denied, not treated as unscoped (see above).
cat <<'EOF' | oc create --dry-run=server -f - \
  --as=nobody --as-group=keycloak:agenticrun-users
apiVersion: agentic.openshift.io/v1alpha1
kind: AgenticRun
metadata:
  name: verify-omitted-namespaces
  namespace: lightspeed-agentic-operator
spec:
  request: verification
  analysis:
    agent: default
EOF
# expect: denied
```

**2. Live Token B validation.** Once the patched Lightspeed service and operator
are deployed, verify the actual RFC 8693 result with an approved test harness.
Never log or persist raw tokens; inspect only decoded claims. Confirm `iss` is
the configured realm, `aud` includes `openshift-mcp`, `sub` is unchanged from
Token A, `azp` is `lightspeed-mcp`, and `groups` includes
`acme-agent-lightspeed`. Then verify an API-server TokenReview and a harmless
read in an explicitly authorized namespace, plus denied out-of-scope reads and
writes. Helm output and the Keycloak PostSync Job do not prove these live
behaviors.

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
    not affect the Lightspeed workload CA or OpenShift's OIDC verification.

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
(for example `cluster-admins`) to have this chart
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
