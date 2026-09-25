# Keycloak/OIDC integration for the RCA and Ericsson A2A agents

This companion chart is installed after the validated pattern's `rhbk` chart.
It imports the RCA realm, its five clients, a SPIFFE identity provider, and
its groups into the deployed Keycloak; creates the least-privilege AgenticRun
RoleBinding for the OpenShift groups emitted by the Keycloak `groups` claim
(`agenticRun.groupNames`); and enforces which `spec.targetNamespaces` each of
those groups may request on an `AgenticRun` via a `ValidatingAdmissionPolicy`
(`agenticRun.namespaceAllowlist`, `templates/agentic-vap-namespace-scope.yaml`)
-- RBAC alone can grant or deny the whole resource, but has no way to inspect
a field inside it, which is exactly the gap a compromised or misdirected
caller could otherwise use to reach namespaces it has no business touching.

The five clients serve distinct purposes and must not be conflated:

- `keycloak.clientId` (default `openshift-cli`): public client for browser/`oc
  login` OIDC flows (Native OIDC). Cannot authenticate itself. Registered as
  the `cli` OIDC platform client when `openshiftOIDC.enabled` is true -- named
  for that role, not for the RCA agent, since it's purely human/CLI cluster
  access and has nothing to do with the agent request flow. (It used to be
  named `rca-agent`, which also doubled as the string in
  `keycloak.rcaAgentAudience` below by coincidence of sharing a name -- the
  two are unrelated and have been split apart.)
- `keycloak.mcpClientId` (default `rca-agent-mcp`): confidential client
  `charts/all/rca-agent` uses to exchange a caller's token for one scoped to
  OpenShift MCP.
- `keycloak.mcpAudienceClientId` (default `openshift-mcp`): confidential
  client that exists only so `rca-agent-mcp`'s token exchange has a real
  `client_id` to name as its `audience` parameter -- Keycloak's standard (V2)
  token exchange requires that parameter to be an actual client in the
  realm, not an arbitrary string. Never authenticates itself.
- `keycloak.ericssonClientId` (default `ericsson-agent`): confidential client
  `charts/all/ericsson-agent` uses for its client-credentials
  call to Keycloak. Its service account belongs to `keycloak.ericssonGroupName`
  (default `ericsson-agent-rca`, named `<caller>-<callee>` rather than just
  `ericsson-agent` so it reads as "ericsson-agent's rights when calling
  rca-agent") -- without this group, and the `groups` protocol mapper this
  chart attaches to the client, the token this client mints (and, unchanged,
  the token `rca-agent-mcp` exchanges it for) carries no groups claim at all,
  so neither `agentic-rbac.yaml` nor `agentic-vap-namespace-scope.yaml` have
  anything to key on for calls attributed to ericsson-agent.
- `keycloak.consoleClientId` (default `openshift-console`): confidential
  client registered as the `console` OIDC platform client when
  `openshiftOIDC.enabled` is true. See "OpenShift Native OIDC" below.

`rca-agent-mcp` and `ericsson-agent` authenticate with a SPIFFE JWT-SVID (no
static secret) via Keycloak's federated client authentication feature
(`clientAuthenticatorType: federated-jwt`), against the `spiffe` identity
provider this chart also creates (`keycloak.spiffeIdentityProvider.*`). This
is fully declarative -- no manual Admin Console step is required for it, and
the `jwt.credential.sub` value each client expects is computed from
`keycloak.ericssonWorkload`/`keycloak.mcpWorkload` (the namespace/ServiceAccount
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
- `rca-agent-mcp`'s actual RFC 8693 exchange additionally needs
  `standard.token.exchange.enabled: "true"` (a client attribute this chart
  sets) plus a protocol mapper adding `keycloak.mcpAudienceClientId` as an
  audience on `rca-agent-mcp` itself, and a protocol mapper on
  `ericsson-agent` adding `keycloak.mcpClientId` as an audience (a real
  client, `included.client.audience`) to the tokens it mints -- Keycloak's
  standard token exchange requires the *subject_token* to already carry the
  exchanging client as an audience, and the `audience` request parameter
  only ever narrows audiences a client scope already resolves, it never adds
  a new one. `ericsson-agent` also carries a *separate* mapper adding
  `keycloak.rcaAgentAudience` as an audience (`included.custom.audience`, no
  client involved) -- that one exists only to satisfy rca-agent's own inbound
  check and has nothing to do with the exchange itself. All of this is
  templated already; it's listed here because it is not obvious from
  Keycloak's own error messages if you ever need to debug it directly.

## AgenticRun authorization: RBAC + namespace-scoping admission policy

**If the `rca` realm has already reached `Done: True`, none of this section's
Keycloak-side pieces (the `ericsson-agent-rca` group, its membership, or the
`groups` protocol mappers on `ericsson-agent`/`rca-agent-mcp`) take effect
just from the next sync** -- same one-shot limitation as `keycloak.adminGroupName`
(see "Keeping admin access after enabling OIDC" below): `KeycloakRealmImport`
is only applied when the realm doesn't already exist, and isn't continuously
reconciled after that. Check with:

```bash
oc get keycloakrealmimport <keycloak.realm, default "rca"> \
  -n <keycloak.namespace, default "keycloak-system"> -o jsonpath='{.status.conditions}'
```

If it's already `Done: True`, create the `ericsson-agent-rca` group manually
in the Admin Console, add the `groups` mapper to both `ericsson-agent` and
`rca-agent-mcp` by hand, and add `ericsson-agent`'s service account
(`service-account-ericsson-agent`) to the group -- matching what
`keycloak-realm-import.yaml` declares, so a future full realm re-import
doesn't diverge from what's actually configured.

Two independent RBAC/admission layers, checking two different things:

- `templates/agentic-rbac.yaml` grants every group in `agenticRun.groupNames`
  `create`/`get` on `agenticruns` and `get` on `analysisresults`, in
  `agenticRun.namespace`. This is coarse: it decides whether a caller may act
  on the CRD at all, the same way any other RBAC grant would.
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
oc get role,rolebinding -n lightspeed-agentic-operator rca-agent-user
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
# Positive: the caller group this repo actually grants may create in an
# allowed namespace (adjust the namespace to one actually in
# agenticRun.namespaceAllowlist.ericsson-agent-rca for your environment).
oc auth can-i create agenticruns \
  --as=nobody --as-group=keycloak:ericsson-agent-rca \
  -n lightspeed-agentic-operator

cat <<'EOF' | oc create --dry-run=server -f - \
  --as=nobody --as-group=keycloak:ericsson-agent-rca
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

# Negative: authorized group, but a namespace outside its allow-list --
# RBAC alone would allow this (it can't see spec fields); the VAP must deny
# it. A rejection here confirms the policy is actually being evaluated, not
# just present.
cat <<'EOF' | oc create --dry-run=server -f - \
  --as=nobody --as-group=keycloak:ericsson-agent-rca
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
  --as=nobody --as-group=keycloak:ericsson-agent-rca
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

**2. That the real exchanged token actually carries the `groups` claim
(needs a live pod -- this is the part that can't be verified without a
running deployment).** `groups` mappers exist on both `ericsson-agent` and
`rca-agent-mcp` (see the comment in `keycloak-realm-import.yaml`) precisely
because it isn't verified which client's mappers Keycloak's standard V2
exchange actually applies to the newly-minted, differently-audienced token.
Confirm by decoding a real exchanged token's payload -- **never log or print
the full token, only its decoded claims**:

```bash
# From inside a running rca-agent pod (has httpx + the spiffe SDK already):
oc exec -n lightspeed-agentic-operator deploy/rca-agent -- python3 -c '
import base64, json, os
import httpx
from spiffe import WorkloadApiClient

# 1. Fetch a caller-shaped token the same way ericsson-agent does, so this
#    reproduces a real exchange rather than asserting expected shape.
#    (Run the equivalent from an ericsson-agent pod using its own
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
matches the caller's (not rca-agent's own) subject, `azp` is `rca-agent-mcp`,
and -- the thing this whole check exists for -- `groups` contains
`ericsson-agent-rca`. If it doesn't, the belt-and-suspenders mapper placement
didn't work and RBAC/the VAP have nothing to key on; see
`keycloak-realm-import.yaml`'s comment on the `rca-agent-mcp` client's
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
     `keycloak-realm-import.yaml`'s `$(CONSOLE_CLIENT_SECRET)` placeholder
     set the same value on the client at import time. Confirm it exists:

     ```bash
     oc get secret <openshiftOIDC.consoleClientSecretName, default "openshift-console-oidc"> \
       -n openshift-config
     ```

   - **Otherwise** (or if the realm already existed before you set
     `consoleClientSecretVaultKey` -- see the one-shot caveat on that value
     in `values.yaml`), get the client's Keycloak-generated secret by hand
     (Admin Console → Clients → `openshift-console` → Credentials tab →
     Client secret) and create the Secret yourself:

     ```bash
     oc create secret generic openshift-console-oidc \
       -n openshift-config \
       --from-literal=clientSecret='<value from the Credentials tab>'
     ```

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
