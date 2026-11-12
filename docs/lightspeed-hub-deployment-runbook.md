# Lightspeed-hub deployment runbook (greenfield, everything on)

This is the **fresh-cluster** end-to-end runbook for the Task B
investigation backend: the hub A2A adapter
(`charts/all/lightspeed-hub`, source in the
[lightspeed-hub repo](https://github.com/luis5tb/lightspeed-hub), branch
`a2a`, nested module `a2a-adapter/`) driving the
lightspeed-agentic-operator (repo C, branch `a2a-support`), fronted by
the Praxis policy gateway, with the ACME agent delegating through MAS.

It assumes a **greenfield hub + at least one spoke** OpenShift cluster and
enables **everything from the start** — Keycloak hub/spoke exchange
clients, Praxis spoke gateways + A2A upstream, the lightspeed-hub chart —
in ONE `values-standalone.yaml` edit. There is no phased enablement here
because nothing has imported a realm yet: the one-shot
`KeycloakRealmImport` picks up every client and mapper on the first pass,
which is exactly the advantage a fresh cluster has over the existing
bm-cluster deployment (where each of these blocks must be hand-created in
the Admin Console instead — see the "One-shot realm import" notes in
`charts/all/keycloak-oidc/README.md`).

Settled architecture, reflected throughout:

- All modifications to the agentic-operator live in **its repo**
  (`lightspeed-agentic-operator`, branch `a2a-support`); its image is
  built there and pushed to quay (`quay.io/ltomasbo/lightspeed-agentic-operator`).
- The gitops `lightspeed-hub` chart deploys the **full operator stack**
  (vendored CRDs + RBAC + Deployment + `lightspeed-agentic-configuration`
  ConfigMap) plus the adapter, with both images pinned in
  `variants/standalone/values-standalone.yaml`.
- The adapter image is built from the lightspeed-hub repo
  (`make image-a2a-adapter`).

Placeholders used below: hub base domain `hub.example.test`, spokes
`east` and `west` (spoke API servers `https://api.east.example.test:6443`,
`https://api.west.example.test:6443`). Replace with your real values.

Honest scope note: this runbook has **not** been executed end-to-end on a
live greenfield pair yet. Everything marked UNCONFIRMED inline is a live
validation item; section B2 doubles as the live-validation checklist.

---

## Phase A — one-shot initial deployment

### A0. Prerequisites

- A fresh **hub** OpenShift cluster and at least one fresh **spoke**
  OpenShift cluster, with `cluster-admin` credentials for both.
- Tooling on your workstation (in a container or venv, not on the host):
  `oc`, `podman`, `helm`, `make`, `jq`, `python3`, and Vault access for
  the pattern's `load-secrets` flow.
- Checkouts, on the pinned branches:

```bash
git clone -b rca-mao-praxis-hub <your-fork> multicloud-gitops   # this repo
git clone -b a2a        <lightspeed-hub-fork> lightspeed-hub
git clone -b a2a-support <agentic-operator-fork> lightspeed-agentic-operator
```

- `oc login` to **both** clusters (keep two kubeconfigs, e.g.
  `hub.kubeconfig` / `east.kubeconfig`; the pattern's default expects
  `KUBECONFIG` or `$HOME/gitops.kubeconfig` pointing at the hub).
- The sibling-replace requirement: `lightspeed-hub/a2a-adapter/go.mod`
  `replace`s the `lightspeed-agentic-operator/api` module with the
  **sibling checkout** `../../lightspeed-agentic-operator/api` — the two
  repos must sit side by side in the same parent directory for
  `make image-a2a-adapter` to build.

### A1. Build and push images (record the digests)

All three images are ours; nothing is published upstream.

**agentic-operator (repo C)** — built from
`/path/to/lightspeed-agentic-operator`:

```bash
cd lightspeed-agentic-operator
podman login quay.io
make docker-build IMG=quay.io/ltomasbo/lightspeed-agentic-operator:<tag>
make docker-push    IMG=quay.io/ltomasbo/lightspeed-agentic-operator:<tag>
# record: podman images --digests | grep lightspeed-agentic-operator
```

**hub A2A adapter** — built from `/path/to/lightspeed-hub` (the sibling
`../../lightspeed-agentic-operator` checkout must exist — see A0):

```bash
cd lightspeed-hub
make image-a2a-adapter A2A_ADAPTER_IMG=quay.io/ltomasbo/lightspeed-hub-a2a-adapter:<tag>
podman push quay.io/ltomasbo/lightspeed-hub-a2a-adapter:<tag>
```

`make image-a2a-adapter` vendors the nested module's dependencies into a
staging context (`.a2a-image-context/`, removed afterwards) precisely so
the sibling `replace` resolves at build time without leaving a vendor
tree in the repo.

**ACME** — rebuilt per the main README's Phase 0
(`agents/acme_agent/Containerfile` in this repo):

```bash
cd multicloud-gitops
export ACME_TAG=$(git rev-parse --short HEAD)
podman build -f agents/acme_agent/Containerfile \
  -t quay.io/${QUAY_ORG}/acme-agent:${ACME_TAG} agents/acme_agent
podman push quay.io/${QUAY_ORG}/acme-agent:${ACME_TAG}
```

> **Bridge note:** the currently pinned ACME image
> (`variants/standalone/values-standalone.yaml`, `acme-agent` →
> `image.tag` "69cd1c1f" BRIDGE comment) predates the Task A OLS removal
> and still contains the removed OLS/cluster-routing code. On a
> greenfield deploy you pin the image you just built, which closes that
> bridge; delete the BRIDGE comment when you do.

Pin **digests or exact quoted tags** in `values-standalone.yaml` — never
an all-digits unquoted tag (YAML number-parsing has produced
`3.425697e+07` image names on this pattern before; see
`docs/ARCHITECTURE.md`).

### A2. Vault secrets

Copy and edit the secrets file:

```bash
cp values-secret.yaml.template \
   ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
$EDITOR ~/.config/hybrid-cloud-patterns/values-secret-multicloud-gitops.yaml
```

Every Vault entry the full deployment consumes (new Task B entries in
**bold**). `load-secrets` materializes them into Vault under
`secret/data/global/<name>`; fields marked `onMissingValue: generate` are
generated **once**, on the first load, and reused verbatim afterwards —
the fresh cluster's one-shot realm import substitutes the same values
into the new Keycloak clients (`praxis-spoke-gateway`,
`lightspeed-hub-adapter`) via `$(...)` placeholders, which is why both
new entries must exist in Vault **before** the first `make install`
(Phase A5), not after.

| Secret (Vault) | Field | Consumer | Env / field | Vault path |
| --- | --- | --- | --- | --- |
| `rhbk` | `admin-password` | Keycloak admin user | realm-secrets-reconciler | `secret/data/global/rhbk` |
| `rhbk` | `db-password` | Keycloak PostgreSQL | `Keycloak` CR | `secret/data/global/rhbk` |
| `rhbk` | `console-client-secret` | `openshift-console` OIDC client | `openshiftOIDC.consoleClientSecretName` | `secret/data/global/rhbk` |
| `rhbk` | `console-admin-password` | in-realm admin user (`consoleAdminUser`) | `console-admin-user` Secret | `secret/data/global/rhbk` |
| **`praxis-spoke-gateway`** | `praxis-spoke-gateway-client-secret` | per-spoke Praxis spoke-gateway pods (PPE `delegator/oauth` plugin) — the A→U exchange client | `PRAXIS_SPOKE_EXCHANGE_CLIENT_SECRET` env on spoke-gateway pods; `client_secret_source` in `delegator/oauth`; realm import (`keycloak.spokeGatewayExchanger`) | `secret/data/global/praxis-spoke-gateway` |
| **`lightspeed-hub-adapter`** | `lightspeed-hub-adapter-client-secret` | hub A2A adapter — the A→H exchange client | `KEYCLOAK_CLIENT_SECRET` env on the adapter (via Secret `lightspeed-hub-adapter-client`); realm import (`keycloak.hubExchange`) | `secret/data/global/lightspeed-hub-adapter` |
| `llm-creds-vertex` | `credentials` | mao-worker Vertex ADC (`vertexCreds`) | ExternalSecret `mas-worker-vertex-creds` | `secret/data/global/llm-creds-vertex` |
| `acme-agent-litellm` | `api-key` | acme-agent LiteLLM routing | `litellm.vaultKey` | `secret/data/global/acme-agent-litellm` |
| `mao-run-credentials` | `keys` | mao-api / mao-worker / mao-a2a Fernet keys | `runCredentials.secretName` | `secret/data/global/mao-run-credentials` |
| `acme-agent-redhat-sso-production` | `client-id`, `client-secret` | acme-agent `auth.redhatSso` + Keycloak `externalOidcProviders[0]` | ExternalSecrets both sides | `secret/data/global/acme-agent-redhat-sso-production` |
| `acme-agent-redhat-sso-stage` | `client-id`, `client-secret` | acme-agent `auth.redhatSso` + Keycloak `externalOidcProviders[1]` | ExternalSecrets both sides | `secret/data/global/acme-agent-redhat-sso-stage` |

Uncomment/enable the two commented entries
(`acme-agent-redhat-sso-*`). The `praxis-spoke-gateway` and
`lightspeed-hub-adapter` blocks above are now recorded (commented) in
`values-secret.yaml.template` itself — uncomment them there too rather
than re-typing:

```yaml
  - name: praxis-spoke-gateway
    vaultPrefixes:
    - global
    fields:
    - name: praxis-spoke-gateway-client-secret
      onMissingValue: generate
      vaultPolicy: validatedPatternDefaultPolicy

  - name: lightspeed-hub-adapter
    vaultPrefixes:
    - global
    fields:
    - name: lightspeed-hub-adapter-client-secret
      onMissingValue: generate
      vaultPolicy: validatedPatternDefaultPolicy
```

### A3. `variants/standalone/values-standalone.yaml` — one combined edit

Make **one** commit-sized edit covering base values and every activation
block. The commented blocks already in the file are the exact key-name
source; this section is the assembled version with `east`/`west`
placeholders.

**Base values:**

- `global.clusterBaseDomain: hub.example.test` (hub base domain, no
  `apps.` prefix) — everything else derives from it.
- `acme-agent` → `image.tag`: the ACME tag from A1 (quoted; closes the
  bridge note).
- `lightspeed-hub` → `adapter.image.repository`:
  `quay.io/ltomasbo/lightspeed-hub-a2a-adapter`,
  `adapter.image.tag`: quoted tag/digest from A1.
- `lightspeed-hub` → `agenticOperator.image.repository`:
  `quay.io/ltomasbo/lightspeed-agentic-operator`,
  `agenticOperator.image.tag`: quoted tag/digest from A1.

**keycloak-oidc activation** (hub side; uncomment the commented blocks):

```yaml
        - name: keycloak.hubExchange.enabled
          value: "true"
        - name: keycloak.hubExchange.vaultKey
          value: secret/data/global/lightspeed-hub-adapter
        - name: keycloak.hubExchange.hubAudience
          value: lightspeed-hub-a2a
        - name: keycloak.spokeGatewayExchanger.enabled
          value: "true"
        - name: keycloak.spokeGatewayExchanger.vaultKey
          value: secret/data/global/praxis-spoke-gateway
        - name: keycloak.spokeGatewayExchanger.spokeAudiences[0]
          value: praxis-spoke-east
        - name: keycloak.spokeGatewayExchanger.spokeAudiences[1]
          value: praxis-spoke-west
        - name: openshiftOIDC.extraAudiences[0]
          value: lightspeed-hub-a2a
        - name: openshiftOIDC.groupsClaim
          value: groups
```

- `openshiftOIDC.extraAudiences` must include `lightspeed-hub-a2a` so the
  hub's own API server accepts Token H (whose audience is the hub
  audience) as a trusted audience of the hub Keycloak issuer.
- `openshiftOIDC.groupsClaim: groups` + the default
  `groupsPrefix: "keycloak:"` is what maps H's Keycloak groups to
  Kubernetes groups like `keycloak:acme-agent-rca` for the run-creators
  RoleBinding and the admission policy.
- On a **fresh** cluster the one-shot realm import creates the
  `lightspeed-hub-adapter` and `praxis-spoke-gateway` clients, their
  secrets (from the Vault entries of A2), the audience mappers and the
  group mappers — no Admin Console work. This is the only deployment
  shape where the commented blocks work by just uncommenting.

**praxis-proxy activation** (spoke gateways + A2A upstream):

```yaml
        - name: spokeGateways.east.remoteAPIServer
          value: https://api.east.example.test:6443
        - name: spokeGateways.east.exchangeAudience
          value: praxis-spoke-east
        - name: spokeGateways.east.remoteCACert.secretName
          value: praxis-spoke-east-api-ca
        - name: spokeGateways.west.remoteAPIServer
          value: https://api.west.example.test:6443
        - name: spokeGateways.west.exchangeAudience
          value: praxis-spoke-west
        - name: spokeGateways.west.remoteCACert.secretName
          value: praxis-spoke-west-api-ca
        - name: spokeGatewayConfig.networkPolicy.ingressFromNamespaces[0]
          value: lightspeed-hub
        - name: spokeGatewayConfig.servingCA.targetNamespace
          value: lightspeed-hub
        - name: a2aUpstream.enabled
          value: "true"
        - name: a2aUpstream.targetHeader
          value: X-Hub-Cluster
```

- `a2aUpstream.service/namespace/port` default to the lightspeed-hub
  chart's Service (`lightspeed-hub.lightspeed-hub.svc.cluster.local:8443`)
  — set them only to diverge.
- `spokeGateways` keys (`east`, `west`) ARE the caller×target allow-list
  the Praxis router matches on; keep them, `HUB_ADAPTER_CLUSTERS` (B1)
  and `adapter.allowedTargets` (below) on the **same list**.
- `networkPolicy.ingressFromNamespaces` is a mandatory override (the
  chart default is a placeholder tied to design open question Q4); on a
  greenfield deploy with the default run namespace it is
  `lightspeed-hub` — confirm against where sandbox pods actually land
  (see `charts/all/lightspeed-hub/README.md` "Run namespace").
- `spokeGatewayConfig.servingCA.targetNamespace: lightspeed-hub` routes
  the CA-sync bridge's Secret into the operator's namespace (repo C
  reads `gatewayCASecret` from its **operator namespace**).

**lightspeed-hub activation:**

```yaml
        - name: enabled
          value: "true"
        - name: adapter.hubAudience
          value: lightspeed-hub-a2a
        - name: adapter.keycloak.clientId
          value: lightspeed-hub-adapter
        - name: adapter.keycloak.clientSecretSecret.name
          value: lightspeed-hub-adapter-client
        - name: adapter.targetHeader
          value: X-Hub-Cluster
        - name: adapter.allowedTargets[0]
          value: east
        - name: adapter.allowedTargets[1]
          value: west
        - name: adapter.hubRunCreatorsGroup
          value: keycloak:acme-agent-rca
        - name: admissionPolicy.allowTargets.keycloak\:acme-agent-rca[0]
          value: east
        - name: admissionPolicy.allowTargets.keycloak\:acme-agent-rca[1]
          value: west
```

- `adapter.allowedTargets` = the same list as the `spokeGateways` keys;
  it is the adapter's fail-fast `--allowed-targets` behavior (the
  unknown-target message is the model-facing spelling guard).
- `adapter.hubRunCreatorsGroup` names the Kubernetes group the hub API
  maps from H's `groups` claim (`keycloak:` prefix + the Keycloak group
  the `hubExchange` mappers put on H — for brokered Red Hat SSO callers
  that is `acme-agent-rca` via `hardcodedGroups`).
- `admissionPolicy.allowTargets` keys are Token H usernames or group
  names; values are allowed `targetCluster` names (comma-separated when
  more than one).
- **Caution (colon-containing keys via ArgoCD `--helm-set`):** keys like
  `keycloak:acme-agent-rca` must have the colon escaped as shown above
  (`admissionPolicy.allowTargets.keycloak\:acme-agent-rca[0]`). If the
  escape misbehaves in your ArgoCD/Helm combination, prefer a values-file
  snippet (`admissionPolicy.allowTargets` as a proper YAML map) over
  fighting `--helm-set` quoting.
- The gateway credential keys
  (`agenticOperator.config.gatewayServerURLTemplate`,
  `gatewayCASecret`) default to match the per-spoke gateway Service
  naming and the CA bridge — verify against
  `charts/all/lightspeed-hub/README.md` "Agentic-operator stack wiring"
  before relying on the defaults.

Also fill the remaining live values the file already documents: the
`a2a.blueprintId`/`allowedPrincipals` pins (B1), ACME's remote agent
endpoint, and `mao-a2a.publicBaseUrl`.

### A4. Spoke-side prep (NOT chart-automated — known gap)

Nothing in the pattern's charts touches the **spoke** clusters. Per
spoke, three manual steps. Automating this (dedicated spoke chart /
config management) is a recorded known gap.

**(a) Spoke API CA Secret on the hub.** The spoke gateway verifies the
spoke API server's TLS; its CA must exist in the hub's `praxis-proxy`
namespace as the Secret each `spokeGateways.<name>.remoteCACert.secretName`
names (key `ca.crt` by default — see the chart's `remoteCACert` docs).
From the spoke:

```bash
# fetch the spoke's API CA bundle (adjust to your cluster's signer)
oc --kubeconfig east.kubeconfig get configmap \
  -n openshift-config-managed default-ingress-cert -o jsonpath='{.data.ca-bundle\.crt}' \
  > /tmp/east-api-ca.crt   # NOTE: for the API server itself, use the CA that
                           # signed api.east.example.test's serving cert, e.g.
                           # the cluster's root CA from the kubeconfig or
                           # /etc/kubernetes/ca.crt on a control-plane node.
oc --kubeconfig hub.kubeconfig create secret generic praxis-spoke-east-api-ca \
  -n praxis-proxy --from-file=ca.crt=/tmp/east-api-ca.crt
```

Never commit a real CA to this repo. A dangling reference leaves the
spoke-gateway pod unschedulable (fail closed), so do this before A5.

**(b) Spoke `Authentication` (OIDC) trusting the hub Keycloak.** ON THE
SPOKE, switch the cluster to OIDC against the **hub** Keycloak issuer,
with the **per-spoke audience** and the groups claim mapping. This is
spoke-adapted from
`charts/all/keycloak-oidc/templates/openshift-authentication.yaml`
(the hub's own version registers platform cli/console clients; a spoke
only needs the issuer/audience/claim mappings — apply manually, no
oidcClients entries):

```yaml
# east-spoke-authentication.yaml (SPOKE-ADAPTED — apply on the spoke, not via gitops)
apiVersion: config.openshift.io/v1
kind: Authentication
metadata:
  name: cluster
spec:
  type: OIDC
  webhookTokenAuthenticator: null
  oidcProviders:
    - name: hub-keycloak
      issuer:
        issuerURL: https://keycloak.apps.hub.example.test/realms/rca
        audiences:
          - praxis-spoke-east          # this spoke's exchangeAudience ONLY
        issuerCertificateAuthority:
          name: hub-keycloak-ca         # ConfigMap in openshift-config
      claimMappings:
        username:
          claim: preferred_username
          prefixPolicy: Prefix
          prefix:
            prefixString: "keycloak:"
        groups:
          claim: groups                 # UNCONFIRMED: groups on EXCHANGED
          prefix: "keycloak:"           # Token U -- validate in B2
```

Before applying, create the CA ConfigMap on the spoke from the **hub
cluster's managed ingress CA** (the CA that signs
`keycloak.apps.hub.example.test`'s route certificate):

```bash
oc --kubeconfig hub.kubeconfig get configmap \
  -n openshift-config-managed default-ingress-cert \
  -o jsonpath='{.data.ca-bundle\.crt}' > /tmp/hub-ingress-ca.crt
oc --kubeconfig east.kubeconfig create configmap hub-keycloak-ca \
  -n openshift-config --from-file=ca-bundle.crt=/tmp/hub-ingress-ca.crt
oc --kubeconfig east.kubeconfig apply -f east-spoke-authentication.yaml
```

> Same caution as the hub-side chart README: `Authentication/cluster` is
> cluster-wide and one-shot-risky — confirm you hold a break-glass
> kubeconfig on the spoke before applying. On a spoke this is lower
> stakes (the spoke is expendable), but lockout is still lockout.

**(c) Spoke RBAC for the investigating principal.** Token U arrives at
the spoke carrying the original principal's groups (mapped above as
`keycloak:<group>`). Grant that group the permissions investigations
should have — read-mostly, per the pattern's intent:

```bash
oc --kubeconfig east.kubeconfig adm policy add-cluster-role-to-group \
  view keycloak:acme-agent-rca
# or, narrower: a ClusterRole/Role with exactly the verbs the sandbox
# tools need, bound to the group in the target namespaces.
```

The intended permission set is a per-deployment decision; what matters
for the flow is that some binding covers `keycloak:acme-agent-rca`
(otherwise every sandbox API call returns 403).

### A5. Install

```bash
./pattern.sh make install      # or: make install
make argo-healthcheck
```

This brings up vault → ESO → ZTWIM/SPIRE → Keycloak + realm import (with
all the A2/A3 clients and mappers) → keycloak-oidc (incl. hub
`Authentication` OIDC) → praxis-proxy (main gateway + spoke gateways +
CA-sync bridge + A2A upstream) → lightspeed-hub (adapter + operator
stack) → MAS → acme-agent in one shot.

**Ordering inside lightspeed-hub:** the chart renders the vendored CRDs
ahead of the operator Deployment via sync-wave, so ArgoCD applies the
`AgenticRun`/`AgenticRunApproval`/`*Result`/`Agent`/`LLMProvider`/
`ApprovalPolicy`/`AgenticOLSConfig` CRDs before the operator pod starts
(otherwise the manager crashes on unknown kinds until the next sync).
The exact wave numbers and the vendoring convention are the
lightspeed-hub chart's — **verify in `charts/all/lightspeed-hub/README.md`
before relying on this**; that vendoring was being completed concurrently
with this runbook.

Also expect, and act on, the already-documented first-sync sequence:
approve any pending Manual InstallPlans, walk the keycloak-oidc
pre-flight checklist (`openshiftOIDC.enabled` is on from the start here,
so verify the realm import reached `Done: True` **before** the
`Authentication` CR's sync-wave applies — the realm-import-healer CronJob
covers the fresh-install race; see `charts/all/keycloak-oidc/README.md`).

### A6. Verification checklist (post-sync, pre-blueprint)

```bash
make argo-healthcheck          # every Application Synced + Healthy

# Adapter is alive (internal-only Service: port-forward)
oc -n lightspeed-hub port-forward svc/lightspeed-hub 8443:8443 &
curl -k https://localhost:8443/healthz

# Spoke gateways running (one Deployment per spoke)
oc -n praxis-proxy get deploy,pod | grep praxis-spoke-

# CA-sync bootstrap Job delivered the gateway CA Secret into the run namespace
oc -n lightspeed-hub get secret praxis-spoke-gateway-ca -o jsonpath='{.data.ca\.crt}' | head -c 40; echo

# Operator validated the gateway credential mode (fails closed otherwise)
# (Deployment name is <chart fullname>-agentic-operator per
# templates/agentic-operator.yaml)
oc -n lightspeed-hub logs deploy/lightspeed-hub-agentic-operator | grep -i "gateway credential mode"

# Run-creators RBAC: the group can create AgenticRuns, others cannot
oc -n lightspeed-hub auth can-i create agenticruns --as=group:keycloak:acme-agent-rca   # yes
oc -n lightspeed-hub auth can-i create agenticruns --as=group:system:authenticated     # no
oc -n lightspeed-hub get rolebinding | grep -i run-creator

# Keycloak: hubExchange + exchanger clients exist with the right mappers
# (fresh realm: they came from the import; confirm anyway)
oc -n keycloak-system get keycloakrealmimport rca -o jsonpath='{.status.conditions}'
# then in the Admin Console (Clients): lightspeed-hub-adapter and
# praxis-spoke-gateway present, standard.token.exchange.enabled,
# audience + group mappers as configured.
```

---

## Phase B — blueprint + end-to-end

### B1. Register the MAS blueprint

How the script reaches the MAS management API (read from
`setup-mas-blueprint.sh`): `MAS_API_URL` defaults to the `mas-api` Route
in the `mao` namespace, resolved live via `oc get route mas-api -n mao`;
`MAS_TOKEN` defaults to `dev-token` (the mas-api chart currently runs
`identity.providerMode: dev`, which admits any token as user `dev` —
that is why the Route is meant to stay private). If you keep the Route
disabled, port-forward instead and point the script at it:

```bash
oc -n mao port-forward svc/mao-api 8002:8002 &
export MAS_API_URL=http://localhost:8002
```

For a private-CA Route, set `MAS_API_CA_BUNDLE=/path/to/ca-bundle.crt`
(never `MAS_API_INSECURE_TLS=true` outside a throwaway test).

Then, dry-run first (no MAS API calls; placeholder RIDs):

```bash
export HUB_ADAPTER_A2A_URL="https://praxis-proxy.apps.hub.example.test/a2a"
#   ^ the Praxis PUBLIC route (front door) — NEVER the adapter's internal
#     Service: Praxis validates Token A and routes on X-Hub-Cluster.
export HUB_ADAPTER_AGENT_NAME="Hub Cluster Investigator"
export HUB_ADAPTER_TARGET_HEADER="X-Hub-Cluster"   # = a2aUpstream.targetHeader
export HUB_ADAPTER_CLUSTERS="east,west"            # = spokeGateways keys
./setup-mas-blueprint.sh --dry-run multi-cluster
```

Inspect the printed `blueprintRaw` JSON: the orchestrator system message
must contain the exact-spelling header rule **and** the closed cluster
list ("complete list, there are no others: east, west"), the
`hub_investigate` node's `base_url` must be the Praxis route, and
`bearer_token` must be empty (execution-context credential). Then run it
for real (same env, drop `--dry-run`); the recommended LLM provider is
`LLM_PROVIDER=vertex` (gpt-oss over MaaS returns harmony tool calls that
break plan building — see the script's comments).

Pin the returned blueprint ID in `variants/standalone/values-standalone.yaml`:

```yaml
        - name: a2a.blueprintId
          value: "<returned-blueprint-id>"
        - name: a2a.allowedPrincipals[0]
          value: "<ACME principal sub>"   # see the file's comment for how to get it
```

Commit, push, let Argo roll mao-a2a.

### B2. End-to-end checklist (doubles as the live-validation list)

Work through these in order; each is also an UNCONFIRMED item being
validated by the first live run:

1. **Knowledge regression:** ask ACME a plain Red Hat documentation
   question → routed to Ask Red Hat, no AgenticRun anywhere.
2. **Investigation happy path:** ask a question naming a spoke ("what
   pods are failing on east?") → an `AgenticRun` appears in the
   `lightspeed-hub` namespace with the principal annotation set → a
   sandbox pod starts in the run namespace **with the gateway kubeconfig
   and run-token Secret mounted** (verify the mounts:
   `oc -n lightspeed-hub get pod <sandbox> -o yaml | grep -A5 volumes`).
3. **Identity propagation:** on the spoke, audit logs (`oc get events`,
   API server audit log if configured) show Token U's username carrying
   the principal's groups (`keycloak:acme-agent-rca`) — not the
   exchanger's identity.
4. **Authorization:** allowed operations return 200 on the spoke;
   operations outside the spoke RBAC return 403 (not silently allowed).
5. **Duplicate suppression:** send the same message twice → no second
   AgenticRun (adapter duplicate rejection).
6. **Token A expiry:** expire/override Token A (ACME's test-only
   `auth.tokenOverride`) mid-run → the task returns auth-required →
   ACME renews → the **same** run resumes (credential-pause annotation
   increments, not a new run).
7. **Unknown target:** ask for a misspelled/unregistered cluster → the
   adapter's fail-fast unknown-target message lists the valid clusters
   (the `--allowed-targets` behavior, i.e. `adapter.allowedTargets`) —
   and anything that got past it anyway is denied by the admission
   policy.
8. **Streaming tools:** run an exec- or watch-based tool through the
   gateway (validates SPDY/websocket pass-through — the pinned Praxis
   image was only spike-tested with the websocket subprotocol) and
   stream logs back.
9. **Token decode (H/U vs A):** using ACME's debug aids
   (`auth.tokenDebug`), decode the three tokens: **H/U carry `groups`**
   (`acme-agent-rca`), **A is groupless**, `azp` = the exchanger
   (`lightspeed-hub-adapter` / `praxis-spoke-gateway`), `sub` = the
   principal. This is the live answer to the Keycloak-mapper
   UNCONFIRMED item.
10. **Terminal cleanup:** let a run reach a terminal state → the
    `ls-run-token-*` Secret is deleted after the terminal-cleanup grace
    period.

### B3. Rollback

Flip the activation flags off in `values-standalone.yaml` and push —
Argo prunes what the charts rendered. Notes:

- `lightspeed-hub.enabled=false` prunes the adapter + operator stack
  (CRDs are typically kept on uninstall; delete them by hand only if you
  want a truly clean slate — check the chart's README for its prune
  behavior).
- `a2aUpstream.enabled=false` and emptying `spokeGateways` returns
  praxis-proxy to the no-upstream front door.
- `keycloak.hubExchange`/`spokeGatewayExchanger` off: on a fresh cluster
  the realm was imported WITH them, so the clients stay in the live
  realm after the import CR stops carrying them — they are harmless
  (secrets remain Vault-backed) and can be ignored or deleted in the
  Admin Console.
- **Retain the old `a2a.blueprintId`** (or the pre-investigation
  blueprint ID) so ACME immediately falls back to the Ask-Red-Hat-only
  blueprint; the script's default output (no `HUB_ADAPTER_A2A_URL`) is
  exactly that shape.
- Spoke-side manual artifacts (A4) are untouched by gitops; revert the
  spoke `Authentication` with the README's recovery patch if the spoke
  must go back to its internal OAuth server.

---

## Known limitations / open items

- **`X-Hub-Cluster` header NAME is model-spelled** (design §4.2
  deviation; MAS has no node-config header mechanism). Mitigations:
  temperature-0 LLM, the prompt's exact-spelling rule, the closed
  cluster list (`HUB_ADAPTER_CLUSTERS`), the adapter's fail-fast
  `--allowed-targets` message, and the admission policy as the
  server-side backstop. Future improvement: per-spoke skills on the
  agent card so the target is chosen from a skill list instead of typed.
- **Keycloak mappers on EXCHANGED tokens UNCONFIRMED** (plan spike item
  #5): whether the exchanger clients' audience/group mappers apply to
  RFC 8693-exchanged H/U on this Keycloak build is validated live only
  by B2 step 9. If they do not apply, groups must ride the subject
  token's client scope — which conflicts with groupless A; resolve
  before production.
- **Multi-valued target headers cannot be rejected by the pinned Praxis
  router** (source-verified: Core v0.7.0 OR-matches repeated header
  values; no filter on the pinned image rejects them). Compensating
  controls, both required: adapter-side duplicate/multi-header rejection
  and the admission policy + run-creators RBAC bounding what a smuggled
  request achieves. Re-check on the next Praxis image bump.
- **Spoke-side `Authentication` is manual** (A4) — no chart automates
  the spoke OIDC trust, API CA Secret, or spoke RBAC. Known gap;
  automate later (a spoke-side chart or the operator's own bootstrap).
- **ACME image bridge:** the pinned ACME image predates Task A (contains
  removed OLS code) until rebuilt per A1 — greenfield deploys pin the
  rebuild, closing it.
- **Live-cluster validations are embedded in B2**, not pre-verified:
  service-serving cert issuance for the adapter Service, the CA-sync
  Secret end-to-end, SPDY exec through the gateway, run-token cleanup
  timing, and the mapper behavior above all get their first live
  exercise on this runbook's first execution. Record outcomes in the
  relevant chart READMEs per the repo's working conventions.
