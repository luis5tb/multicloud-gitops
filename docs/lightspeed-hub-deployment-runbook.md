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
clients, Praxis spoke gateways + A2A upstream, the lightspeed-hub chart,
and the approval-gated **remediation execution path** (Execution with a
separate write token R_exec, then automatic Verification) — in ONE
`values-standalone.yaml` edit. There is no phased enablement here
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

Names used below: hub base domain placeholder `hub.example.test`; spoke
keys as in the standalone file, `hub` (the hub as its own spoke) and
`mario-ipi` (spoke API server placeholder `https://api.mario-ipi.example.test:6443`).
Replace them consistently (A3 lists where each appears).

The standalone variant already carries the remediation lab scope: ACME's
Keycloak service account is a **shared principal** that both starts and
approves runs, and it may write only on spoke `mario-ipi`, namespace
`payments`. Anyone who can reach ACME's UI can approve such a change, so
restrict that access. For an environment that was deployed before this
path existed, see [the brownfield upgrade](A2A_REMEDIATION_BROWNFIELD.md).

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
  `hub.kubeconfig` / `mario-ipi.kubeconfig`; the pattern's default expects
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

**MAS** — one image for `mao-api`, `mao-worker` and `mao-a2a` (its
Dockerfile copies the whole context; the repo's `.dockerignore` keeps local
credentials, virtualenvs and caches out). The build downloads Red Hat's IT
root CA from `certs.corp.redhat.com`:

```bash
cd uie-mas-hosted
podman build -f Dockerfile -t quay.io/ltomasbo/mas-api:<tag> .
podman push quay.io/ltomasbo/mas-api:<tag>
```

The standalone file pins all four images to the tag `a2a-rem-20261126`;
build with that tag or change it in the file. Also pin a **tested sandbox
image by digest** in `agenticOperator.sandbox.image`: the operator runs
Execution and Verification in it, and none of the builds above produce it.

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

`values-secret.yaml.template` is the source of truth: every exchanger entry
it needs is already active there. `load-secrets` writes them to Vault under
`secret/data/global/<name>`; `onMissingValue: generate` fields are generated
**once** and reused, and the one-shot realm import substitutes the same
values into the new Keycloak clients, so they must exist in Vault **before**
the first `make install` (A5). The Praxis credentials use
`alphaNumericPolicy` (the pinned Praxis delegator does not URL-encode Basic
auth); keep that. Uncomment the `acme-agent-redhat-sso-*` entries only if
you use Red Hat SSO.

| Secret (Vault) | Field | Consumer |
| --- | --- | --- |
| `rhbk` | `admin-password`, `db-password`, `console-client-secret`, `console-admin-password` | Keycloak admin/DB, console OIDC client, in-realm admin user |
| `praxis-spoke-gateway` | `praxis-spoke-gateway-client-secret` | read spoke gateways (R → U), client `praxis-spoke-gateway` |
| `praxis-spoke-gateway-exec` | `praxis-spoke-gateway-exec-client-secret` | execution spoke gateways (R_exec → U_exec), client `praxis-spoke-gateway-exec`; never reuse the read secret |
| `praxis-hub-exchange` | `praxis-hub-exchange-client-secret` | Praxis hub gateway (A → H), client `mas-a2a` |
| `lightspeed-hub-adapter` | `lightspeed-hub-adapter-client-secret` | hub adapter (H → R / R_exec), client `lightspeed-hub-a2a` |
| `llm-creds-vertex` | `credentials` | mao-worker and agentic operator Vertex ADC |
| `acme-agent-litellm` | `api-key` | acme-agent LiteLLM routing |
| `mao-run-credentials` | `keys` | mao-api / mao-worker / mao-a2a Fernet keys |
| `acme-agent-redhat-sso-{production,stage}` | `client-id`, `client-secret` | optional: ACME `auth.redhatSso` + Keycloak external IdPs |

### A3. `variants/standalone/values-standalone.yaml` — review and adapt

The committed file is the complete configuration of the reference lab:
hub base domain `bm-cluster.e2e.bos.redhat.com`, and two registered spokes,
`hub` (the hub as its own spoke) and `mario-ipi`. For another environment,
change only the cluster-specific keys below, keeping each row consistent
everywhere it appears. The examples in this runbook use the reference
names.

| What | Keys (application) | Must match |
| --- | --- | --- |
| Hub base domain | `global.clusterBaseDomain` | everything derived from it (issuer, routes) |
| Spoke set | praxis-proxy `spokeGateways.<key>.{remoteAPIServer,exchangeAudience,remoteCACert.secretName}`; keycloak-oidc `spokeGatewayExchanger.spokeAudiences[]` **and** `spokeGatewayExecExchanger.spokeAudiences[]`; lightspeed-hub `adapter.allowedTargets[]` and `admissionPolicy.allowTargets.keycloak\:lightspeed-hub-a2a\:agenticrun-creator[]` | audience = `praxis-spoke-<key>`; blueprint `HUB_ADAPTER_CLUSTERS` (B1); spoke `Authentication` audience (A4 b) |
| Remediation target | keycloak-oidc `exchangeRoles.remediation.spokeAudiences[]` | a subset of the spoke audiences; spoke writer RBAC (A4 d) on exactly those spokes |
| Images | four `image.tag`/`adapter.image.tag`/`agenticOperator.image.tag` (A1); `agenticOperator.sandbox.image` | tested builds; sandbox by digest |
| Live pins | mao-a2a `a2a.blueprintId`, `a2a.allowedPrincipals`, `a2a.publicBaseUrl`; acme-agent `a2a.remoteAgents[0].endpoint` | B1; ACME's service-account `sub` |

Identity chain (keep unless you redesign it; see
`docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md`): ACME's Token A (aud
`mas-a2a`) → **H** exchanged by `mas-a2a` at the Praxis hub gateway (aud
`lightspeed-hub-a2a`) → **R** / **R_exec** exchanged by the adapter as
client `lightspeed-hub-a2a` (`adapter.keycloak.clientId`; auds
`praxis-spoke-gateway` / `praxis-spoke-gateway-exec`) → **U** / **U_exec**
exchanged per call by the read / execution spoke gateway (aud
`praxis-spoke-<key>`). Exchanged tokens carry only **audience-scoped role
groups**, and RBAC binds those, never the raw `acme-agent-rca` group:

- hub: `keycloak:lightspeed-hub-a2a:agenticrun-creator` (run creators and
  admission policy), `keycloak:lightspeed-hub-a2a:agenticrun-approver`
  (approvals), and `keycloak:praxis-spoke-hub:reader` (the hub as a spoke,
  `spokeReaderRBAC`);
- each spoke: `keycloak:praxis-spoke-<key>:reader`, plus
  `keycloak:praxis-spoke-<key>:writer` only on the remediation target.

On a fresh cluster the one-shot realm import creates every client, role,
group, scope and mapper from these values; no Admin Console work.

**Remediation execution path** (already enabled in the standalone file;
keep the three charts consistent — `tests/chart/test_remediation_execution.py`
checks it):

- keycloak-oidc: `hubExchange.executionTokenAudience` and
  `spokeGatewayExecExchanger.*` (same spoke audiences as the read
  exchanger); `exchangeRoles.remediation.groups[0]`
  (`acme-remediation-mario-ipi`, gets `agenticrun-approver` + `writer`);
  `exchangeRoles.remediation.spokeAudiences` (`praxis-spoke-mario-ipi`
  only: the only spoke where `writer` exists); and
  `exchangeRoles.remediation.includeAcmeServiceAccount: "true"` (the
  shared-principal opt-in; leave it out to add members by hand).
- praxis-proxy: `spokeGatewayConfig.execution.*` renders
  `praxis-spoke-<key>-exec` gateways accepting only R_exec.
- lightspeed-hub: `adapter.executionTokenAudience`,
  `agenticOperator.config.gatewayExecutionServerURLTemplate`, and
  `adapter.hubRunApproversGroup`. `spokeExecutorRBAC` stays off: the writer
  binding belongs on the target spoke (A4 d), never the hub.
- acme-agent: `auth.tokenDebug`/`auth.tokenOverride` stay **off**, because
  ACME's token can approve and write.
- mao-api: `route.enabled: "false"` (it runs in development identity mode).

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
oc --kubeconfig mario-ipi.kubeconfig get configmap \
  -n openshift-config-managed default-ingress-cert -o jsonpath='{.data.ca-bundle\.crt}' \
  > /tmp/mario-ipi-api-ca.crt   # NOTE: for the API server itself, use the CA that
                           # signed api.mario-ipi.example.test's serving cert, e.g.
                           # the cluster's root CA from the kubeconfig or
                           # /etc/kubernetes/ca.crt on a control-plane node.
oc --kubeconfig hub.kubeconfig create secret generic praxis-spoke-mario-ipi-api-ca \
  -n praxis-proxy --from-file=ca.crt=/tmp/mario-ipi-api-ca.crt
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
# mario-ipi-spoke-authentication.yaml (SPOKE-ADAPTED — apply on the spoke, not via gitops)
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
          - praxis-spoke-mario-ipi     # this spoke's exchangeAudience ONLY
        issuerCertificateAuthority:
          name: hub-keycloak-ca         # ConfigMap in openshift-config
      claimMappings:
        username:
          claim: preferred_username
          prefixPolicy: Prefix
          prefix:
            prefixString: "keycloak:"
        groups:
          claim: groups                 # audience-scoped role groups on U
          prefix: "keycloak:"
```

Before applying, create the CA ConfigMap on the spoke from the **hub
cluster's managed ingress CA** (the CA that signs
`keycloak.apps.hub.example.test`'s route certificate):

```bash
oc --kubeconfig hub.kubeconfig get configmap \
  -n openshift-config-managed default-ingress-cert \
  -o jsonpath='{.data.ca-bundle\.crt}' > /tmp/hub-ingress-ca.crt
oc --kubeconfig mario-ipi.kubeconfig create configmap hub-keycloak-ca \
  -n openshift-config --from-file=ca-bundle.crt=/tmp/hub-ingress-ca.crt
oc --kubeconfig mario-ipi.kubeconfig apply -f mario-ipi-spoke-authentication.yaml
```

> Same caution as the hub-side chart README: `Authentication/cluster` is
> cluster-wide and one-shot-risky — confirm you hold a break-glass
> kubeconfig on the spoke before applying. On a spoke this is lower
> stakes (the spoke is expendable), but lockout is still lockout.

**(c) Spoke reader RBAC.** Token U carries only the audience-scoped role
group `keycloak:praxis-spoke-<key>:reader` (not the raw `acme-agent-rca`
group). Bind it to read-only roles, mirroring the hub's `spokeReaderRBAC`
(`cluster-reader`, `cluster-monitoring-view`):

```bash
oc --kubeconfig mario-ipi.kubeconfig adm policy add-cluster-role-to-group \
  cluster-reader keycloak:praxis-spoke-mario-ipi:reader
oc --kubeconfig mario-ipi.kubeconfig adm policy add-cluster-role-to-group \
  cluster-monitoring-view keycloak:praxis-spoke-mario-ipi:reader
```

Without it every sandbox API call on that spoke returns 403. The hub as its
own spoke is covered by the chart (`spokeReaderRBAC`).

**(d) Spoke writer RBAC for remediation — prepare now, apply in B3.** Only
the spoke listed in `exchangeRoles.remediation.spokeAudiences` gets it, and
only in reviewed workload namespaces. The script prints a ClusterRole with
the chart's reviewed writer rules plus namespaced RoleBindings for
`keycloak:praxis-spoke-<spoke>:writer` (no ClusterRoleBinding):

```bash
scripts/remediation/spoke-writer-rbac.sh mario-ipi payments > mario-ipi-writer-rbac.yaml
```

Do **not** apply it yet: B3 first proves token separation while writes are
still impossible. Writers can run Pods (any ServiceAccount, mounting
Secrets) in those namespaces; keep privileged ServiceAccounts out.

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
# (NOTE: groups are impersonated with --as-group, NOT --as=group:... --
# the latter impersonates a USER literally named "group:...", which has
# no bindings and always returns "no"):
oc -n lightspeed-hub auth can-i create agenticruns --as=test-user --as-group=keycloak:lightspeed-hub-a2a:agenticrun-creator   # yes
oc -n lightspeed-hub auth can-i create agenticruns --as=test-user --as-group=system:authenticated     # no
oc -n lightspeed-hub get rolebinding | grep -i run-creator

# Remediation path: execution gateways, approver RBAC, Execution Manual
oc -n praxis-proxy get deploy | grep -- '-exec'
oc -n lightspeed-hub get role,rolebinding | grep approver
oc get approvalpolicy cluster -o yaml   # Analysis Automatic, Execution Manual, Verification Automatic

# Keycloak claim scoping for ACME's service account (example claims only,
# never a usable token; works on fresh and upgraded realms):
python3 scripts/remediation/keycloak_execution_brownfield.py --verify \
  --keycloak-url https://keycloak.apps.hub.example.test

# Keycloak: hubExchange + exchanger clients exist with the right mappers
# (fresh realm: they came from the import; confirm anyway)
oc -n keycloak-system get keycloakrealmimport rca -o jsonpath='{.status.conditions}'
# then in the Admin Console (Clients): mas-a2a, lightspeed-hub-a2a,
# praxis-spoke-gateway and praxis-spoke-gateway-exec present, each with
# standard.token.exchange.enabled and its audience + role mappers.
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
oc -n mao port-forward svc/mas-api 8002:8002 &
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
export HUB_ADAPTER_CLUSTERS="hub,mario-ipi"        # = spokeGateways keys
export HUB_ADAPTER_HITL_MODE=ask                   # default; relays the Execution approval
./setup-mas-blueprint.sh --dry-run multi-cluster
```

Inspect the printed `blueprintRaw` JSON: the orchestrator system message
must contain the exact-spelling header rule **and** the closed cluster
list ("complete list, there are no others: hub, mario-ipi"), the
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
   pods are failing on mario-ipi?") → an `AgenticRun` appears in the
   `lightspeed-hub` namespace with the principal annotation set → a
   sandbox pod starts in the run namespace **with the gateway kubeconfig
   and run-token Secret mounted** (verify the mounts:
   `oc -n lightspeed-hub get pod <sandbox> -o yaml | grep -A5 volumes`).
3. **Identity propagation:** on the spoke, audit logs (`oc get events`,
   API server audit log if configured) show Token U's username carrying
   the audience-scoped group (`keycloak:praxis-spoke-mario-ipi:reader`) —
   not the exchanger's identity.
4. **Authorization:** allowed operations return 200 on the spoke;
   operations outside the spoke RBAC return 403 (not silently allowed).
5. **Duplicate suppression:** send the same message twice → no second
   AgenticRun (adapter duplicate rejection).
6. **Token A expiry:** needs ACME's test-only `auth.tokenOverride`, which
   must stay **off** while ACME's account holds remediation rights (this
   variant). Run it only in an environment without
   `includeAcmeServiceAccount`/remediation membership, or skip it here.
7. **Unknown target:** ask for a misspelled/unregistered cluster → the
   adapter's fail-fast unknown-target message lists the valid clusters
   (the `--allowed-targets` behavior, i.e. `adapter.allowedTargets`) —
   and anything that got past it anyway is denied by the admission
   policy.
8. **Streaming tools:** run an exec- or watch-based tool through the
   gateway (validates SPDY/websocket pass-through — the pinned Praxis
   image was only spike-tested with the websocket subprotocol) and
   stream logs back.
9. **Claims, without exporting tokens:** never re-enable ACME's
   `auth.tokenDebug` here. Check what Keycloak issues with
   `python3 scripts/remediation/keycloak_execution_brownfield.py --verify
   --keycloak-url https://keycloak.apps.hub.example.test` (example claims:
   groups and audiences per exchanger), and the stored R / R_exec with B3's
   observer, which prints selected claims only.
10. **Terminal cleanup:** let a run reach a terminal state → the
    `ls-run-token-*` Secret is deleted after the terminal-cleanup grace
    period.

### B3. Remediation: approval → Execution → automatic Verification

The lab target is `mario-ipi`, namespace `payments`, with the payments demo
in its failing state (the `PaymentErrorRateHigh` scenario).

```bash
export MOC="oc --kubeconfig mario-ipi.kubeconfig"
mkdir -p "$HOME/remediation-evidence"
export EVIDENCE="$(mktemp -d "$HOME/remediation-evidence/run.XXXXXX")"
$MOC -n payments get deploy,sts,svc,cm,pods -o yaml > "$EVIDENCE/payments-before.yaml"
```

**1. Token-separation gate, still without any writer binding.** Ask ACME,
for example: *"On cluster mario-ipi, investigate the PaymentErrorRateHigh
alert in namespace payments. Propose remediation options limited to
payments, do not change anything until I approve, and verify the result
after execution."* When ACME shows the options, start the observer before
approving. It decodes selected claims of `token-a` (R) and
`token-execution` (R_exec) from one read of the run-token Secret, never
prints a token, and fails if it misses the window:

```bash
$MOC get clusterrolebinding,rolebinding -A -o wide | grep 'praxis-spoke-mario-ipi:writer' \
  && echo 'STOP: a writer binding already exists'
RUN=$(oc -n lightspeed-hub get agenticruns --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2)
UID_=$(oc -n lightspeed-hub get agenticrun "$RUN" -o jsonpath='{.metadata.uid}')
python3 scripts/remediation/capture_exec_claims.py "ls-run-token-$UID_" "$RUN" 900 \
  > "$EVIDENCE/token-claims.json" 2> "$EVIDENCE/token-claims.err" &
OBSERVER=$!
# ... approve one option in ACME, then:
wait "$OBSERVER" && cat "$EVIDENCE/token-claims.json" || cat "$EVIDENCE/token-claims.err"
oc -n praxis-proxy logs deploy/praxis-spoke-mario-ipi-exec --since=15m | tail -50
```

Go only if R's `aud` is exactly `praxis-spoke-gateway`, R_exec's is exactly
`praxis-spoke-gateway-exec`, both carry ACME's `sub`, Execution went through
`praxis-spoke-mario-ipi-exec`, and its writes were **denied** (403) because
no writer binding exists yet; `payments` is unchanged. If the observer
fails, repeat with a fresh question.

**2. Apply the spoke writer RBAC (A4 d) and check it:**

```bash
$MOC apply -f mario-ipi-writer-rbac.yaml
$MOC auth can-i patch deployments -n payments --as=lab --as-group=keycloak:praxis-spoke-mario-ipi:writer   # yes
$MOC auth can-i patch deployments -n default  --as=lab --as-group=keycloak:praxis-spoke-mario-ipi:writer   # no
$MOC auth can-i get secrets -n payments       --as=lab --as-group=keycloak:praxis-spoke-mario-ipi:writer   # no
$MOC auth can-i patch deployments -n payments --as=lab --as-group=keycloak:praxis-spoke-mario-ipi:reader   # no
oc auth can-i patch deployments -n payments   --as=lab --as-group=keycloak:praxis-spoke-hub:writer         # no (hub)
```

**3. Reject.** Ask a fresh question and reject the proposal: the
AgenticRunApproval records Execution `Denied`, no Execution sandbox runs,
the run's Secret never gains `token-execution`, `payments` is unchanged,
and ACME reports the analysis with the rejection (not a remediation).

**4. Approve.** Ask a fresh question, record the new run before choosing,
and pick an option in the UI (or `approve option N`, one-based):

```bash
RUN=$(oc -n lightspeed-hub get agenticruns --sort-by=.metadata.creationTimestamp -o name | tail -1 | cut -d/ -f2)
UID_=$(oc -n lightspeed-hub get agenticrun "$RUN" -o jsonpath='{.metadata.uid}')
oc -n lightspeed-hub get secret "ls-run-token-$UID_" -w \
  -o go-template='{{range $k, $v := .data}}{{$k}} {{end}}{{"\n"}}'   # key names only
```

Expected: `token-execution` appears only after approval and is removed after
Execution; changes happen only in `payments`; Verification runs
automatically through the read gateway; ACME shows the verified result in
the same conversation. Success needs a healthy workload **and** a
successful VerificationResult:

```bash
oc -n lightspeed-hub get analysisresults,executionresults,verificationresults
$MOC -n payments get deploy,pods
$MOC -n payments get deploy,sts,svc,cm,pods -o yaml > "$EVIDENCE/payments-after.yaml"
```

**5. Invalid choice.** Reply `approve option 9`: ACME stays on the same
approval with an error, and a corrected choice then works.

### B4. Rollback

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
- Remediation only: delete the spoke writer RBAC
  (`oc --kubeconfig mario-ipi.kubeconfig delete -f mario-ipi-writer-rbac.yaml`)
  and remove ACME's service account from `acme-remediation-mario-ipi` in
  the realm; then set the execution keys back to disabled in all three
  charts together.
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
- **Exchanged-token claims:** audience-scoped role groups on exchanged H
  and U were confirmed live on 2026-11-20 (keycloak-oidc README
  "Audience-scoped exchange roles"). R vs R_exec audience separation and
  writer-only-on-the-target are not yet live-verified: B3's token gate
  checks them on the first run.
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
