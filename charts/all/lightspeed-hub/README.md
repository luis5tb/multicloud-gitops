# lightspeed-hub (Task B / M4 skeleton)

Deploys the **hub A2A adapter** (the net-new Go service designed in
`docs/LIGHTSPEED_HUB_ADAPTER_DESIGN.md`) plus the
**lightspeed-agentic-operator** (repo C, branch `a2a-support`) stack it
drives, on the hub cluster. This is the replacement investigation backend
for the removed OLS (`PLAN-lightspeed-hub-migration.md` Task A; this chart
is Task B).

**Status: M4 SKELETON, DISABLED BY DEFAULT.** With `enabled: false` (the
default) this chart renders **no Kubernetes resources**, like
`charts/all/mao/mas-a2a`'s `a2a.enabled` gating. Every PLACEHOLDER /
UNCONFIRMED item is listed in the register at the bottom; each is a slot to
fill before activation. Live findings from bringing this up are in
[`docs/LESSONS_LEARNED.md` §8](../../../docs/LESSONS_LEARNED.md#8-lightspeed-hub--agentic-operator).

## Contents

| Template | What it is |
| --- | --- |
| `deployment.yaml` | Adapter Deployment (placeholder image; REAL adapter flags — the adapter's `internal/config/config.go` in the lightspeed-hub repo, branch `a2a`, nested module `a2a-adapter/`, is the source of truth) |
| `service.yaml` | Internal-only ClusterIP Service — **no Route, ever** |
| `serviceaccount.yaml` | Dedicated adapter ServiceAccount |
| `networkpolicy.yaml` | Ingress admitting only the praxis-proxy namespace |
| `run-token-writer-rbac.yaml` | Role/RoleBinding for the `ls-run-token-*` Secret writer + watch grants (design §5.2; verbs reconciled against the M3 adapter) |
| `run-creators-rbac.yaml` | "Only the configured group can create AgenticRuns": Role (create on `agenticruns`) + Group RoleBinding when `adapter.hubRunCreatorsGroup` is set |
| `run-approvers-rbac.yaml` | Group RoleBinding granting `get/update` on namespaced `AgenticRunApprovals` when `adapter.hubRunApproversGroup` is set; decisions are made as Token H, never as the adapter SA |
| `spoke-executor-rbac.yaml` | Optional enumerated writer ClusterRole with namespaced RoleBindings only in `spokeExecutorRBAC.namespaces`; no ClusterRoleBinding |
| `admission-policy.yaml` | ValidatingAdmissionPolicy + binding + param ConfigMap for requester×target authorization (design §4.3) |
| `agentic-operator.yaml` | Repo C operator Deployment + `lightspeed-agentic-configuration` ConfigMap + operator SA |
| `agentic-operator-crds.yaml` | Vendored repo C CRDs (all ten `agentic.openshift.io` kinds), values-gated + sync-wave −1 |
| `agentic-operator-rbac.yaml` | Vendored repo C operator RBAC (ClusterRole + ClusterRoleBinding), chart-named, sync-wave −1 |

## Why no Route (design §1)

The adapter is an **internal-only Service, reachable only through Praxis**.
Praxis is the caller allow-list; an OIDC-valid token alone is not proof the
caller was admitted by Praxis. This chart deliberately contains no Route
template — adding one is how the MAS management-API exposure issue happened
(see the plan's history). Debugging goes through a port-forward or Praxis.

## Run namespace (design §8.3, open question Q4)

Adapter, repo C operator, sandbox pods, AgenticRuns and the run-token
Secrets all share **one namespace** (the `lightspeed-hub` Application
namespace by default; override `adapter.runNamespace` only against live
Phase 0 findings). Single namespace keeps the §5.2 writer/reader RBAC
isolation exact: no other Secrets live there. Callers never get namespace
access — their only path in is A2A via Praxis. **UNCONFIRMED GUESS:**
whether repo C's `a2a-support` branch runs sandboxes in the operator
namespace on a live install (Q4) — confirm before activation.

## Run-token Secret writer/reader RBAC (design §5.2)

- **Writer (this chart):** the adapter SA gets a Role in the run namespace
  granting `create/get/update/delete` on Secrets — `delete` is the §5.5
  terminal-run Secret cleanup grant, now implemented (M3:
  `a2a-adapter/internal/server/watch.go` `Sweep`/`RunDeleted`). No
  wildcard `resourceNames` prefix exists for `ls-run-token-*`, so
  namespace isolation is the real boundary.
- **Reader (cross-repo):** repo C's operator SA reads the Secrets by name
  (`controller/agenticrun/gateway.go` in the lightspeed-agentic-operator
  repo). That grant belongs to repo C's own RBAC (its `config/rbac`), not
  this chart. The sandbox pod mounts the Secret via kubelet projection — a
  mount, not an API read, needs no RBAC.
- The adapter SA gets **no** secrets access in any other namespace.

### Adapter RBAC, reconciled against the M3 implementation

The verb list in `run-token-writer-rbac.yaml` is derived from what the
built adapter actually does (a2a-adapter README "RBAC notes" in the lightspeed-hub repo, branch `a2a`):

| Resource | Verbs | Why |
| --- | --- | --- |
| `secrets` | create/get/update/**delete** | run-token write/renew (§5) + terminal/deletion cleanup (§5.5) |
| `agenticruns` | get/list/watch/**update** | reads + informers; `update` is ONLY the §6.4 pause-count annotation `a2a.lightspeed.io/credential-pauses` (restart-durable pause cap) |
| `agenticruns/status` | — **none** | the adapter never writes status or conditions (design §6.1); the earlier skeleton grant was dropped |
| `agenticrunapprovals` | **get** only | decisions are patched **as the caller** (Token H) via hub `Authentication`/RoleBinding wiring (keycloak-oidc chart) |
| `*results` (4 kinds) | get/list/watch | artifact/usage projection + informers |

Escalation checks for CI (`oc auth can-i` probes, design §8.1 item 2) are an
M4 hardening item — not yet wired.

### Troubleshooting: AgenticRun exists, but its run-token Secret does not

The adapter creates the AgenticRun as H, exchanges H for R, then writes
`ls-run-token-<run UID>` as its own ServiceAccount. A failure between steps
leaves a real run with `CredentialsExpired=True` / `Secret not found`; a brief
initial pause can be a creation race, persistent absence is a real failure.
Check, in order: `oc auth can-i create secrets` for the adapter SA and the
expected Secret names; then the adapter's outbound Keycloak trust (below) --
without it the H->R exchange fails with a generic `request_failed` and no
Secret is written. Do not widen Secret RBAC, disable TLS verification, or
hand-inject tokens; decide explicitly whether to renew stranded tasks or start
a fresh request. Full write-up: [`docs/LESSONS_LEARNED.md` §8](../../../docs/LESSONS_LEARNED.md#8-lightspeed-hub--agentic-operator).

## Adapter outbound Keycloak CA trust

`adapter.keycloak.caTrust.enabled` is **off by default**, independently of
inbound `adapter.tls.enabled` and secret-versus-SVID client authentication.
For a private-CA-signed Keycloak Route, enable it and configure:

| Value under `adapter.keycloak.caTrust` | Meaning |
| --- | --- |
| `caBundleConfigMapName` | Required namespace-local ConfigMap containing the issuer CA; this chart does not create it |
| `caBundleKey` | Source key (default `ca-bundle.crt`), projected to a fixed filename |
| `publicBundlePath` | Optional system/public PEM bundle path **inside the adapter image**; empty searches common RHEL/Debian/Alpine locations |
| `resources` | CPU/memory requests and limits for the bundle-building init container |

The `build-keycloak-ca-bundle` init container reuses the **same image and
tag/digest** as the adapter. It runs `files/build_keycloak_ca_bundle.sh` to
copy that image's system/public bundle plus the mounted issuer CA into an
`emptyDir`. The adapter mounts only the combined output, read-only, and gets
`SSL_CERT_FILE=/var/run/adapter-keycloak-trust/ca-bundle.crt`. The Go default
HTTPS client uses this file; no adapter binary change or new image is needed.
This extends default outbound trust; Kubernetes clients with their explicitly
configured API CA continue using that CA. TLS/hostname verification stays on.

The image needs `/bin/sh`, `cat`, `grep` and public PEM roots (the UBI-minimal
adapter has them). Fail-closed by design: a wrong `publicBundlePath`, missing
or empty inputs, or a bundle without PEM markers stops init; the source
ConfigMap and key are required projections; there is never a private-CA-only
fallback. Go still performs the real chain/signature/hostname checks. The init
container is non-root, read-only rootfs, all capabilities dropped, writing only
the shared `emptyDir`; no adapter RBAC changes are needed.

### Initial deployment (GitOps, no manual CA patch)

The standalone variant configures both ends:

1. `keycloak-oidc`'s CA bootstrap Sync hook and refresh CronJob copy the
   managed ingress signer to `lightspeed-hub/keycloak-oidc-ca` (the
   `caBundleSync.additionalTargetNamespaces` entry); the adapter only mounts
   it.
2. `lightspeed-hub` enables `adapter.keycloak.caTrust` with that ConfigMap and
   the current adapter's `/etc/pki/tls/certs/ca-bundle.crt` public bundle.

After committing and pushing, sync the parent Application and ensure **both
`keycloak-oidc` and `lightspeed-hub` child Applications sync the new revision**.
The changed Deployment pod template triggers the adapter rollout; no manual
CA creation, Deployment patch or initial `rollout restart` is needed. Child
Applications may sync concurrently: the new pod waits for its required
ConfigMap until the Keycloak bootstrap hook creates it. A different issuer
signer requires changing the CA-sync source, not disabling verification.

Check the adapter rollout and the `build-keycloak-ca-bundle` init container
logs, then submit a **fresh ACME request**. Success means more than Argo
Healthy: its AgenticRun must receive `ls-run-token-<UID>` and progress past
credential waiting. Stranded tasks are not repaired automatically. When
diagnosing with curl inside the adapter, pass `--cacert "$SSL_CERT_FILE"`
explicitly -- Go's env var does not configure curl's TLS backend.

### CA rotation

The Keycloak CronJob refreshes the source ConfigMap, but that does **not**
rerun init or reload Go's cached roots. After confirming the rotated CA in the
namespace-local ConfigMap, replace the adapter pods (during the old/new CA
overlap window):

```bash
oc -n lightspeed-hub rollout restart deployment/lightspeed-hub
oc -n lightspeed-hub rollout status deployment/lightspeed-hub --timeout=180s
```

Each new pod reruns init with the refreshed bundle (a pod-template change also
works). Restarting only the container does not rerun a completed init
container; a ConfigMap refresh or no-op sync is not a trust reload.

## Run-creators RBAC ("only the configured group can create AgenticRuns")

`run-creators-rbac.yaml` (rendered only when `adapter.hubRunCreatorsGroup`
is set) binds that Kubernetes group to a Role granting **`create`** — and
only `create` — on `agenticruns` in the run namespace. This is the
server-side, API-enforced half of the Phase 1 RBAC goal: the adapter
presents the **exchanged Token H** when POSTing runs, Kubernetes
authenticates H's mapped groups, and this binding decides whether run
creation is possible at all. With the value empty (default) nothing
renders, which — together with the deny-all admission policy — keeps run
creation fully closed.

Two boundaries to keep straight:

- **This gates creation itself; the admission policy gates *which*
  target.** `admissionPolicy` (below) restricts `spec.targetCluster` per
  principal; kube create-permission alone cannot do that, and direct kube
  callers bypass Praxis entirely — both controls are required.
- The subject is the group name **as the hub API maps it** from H's
  `groups` claim: with the default OpenShift OIDC mapping
  (`openshiftOIDC.groupsPrefix: "keycloak:"` in charts/all/keycloak-oidc)
  that is `keycloak:<Keycloak group>`, e.g. `keycloak:acme-agent-rca` for
  the group the keycloak-oidc `hubExchange` mappers put on H.

## Cross-chart contract (request leg / leg 2, all disabled by default)

The request leg — MAS → Praxis → this chart's adapter — spans three
charts. These values must line up across them (placeholder defaults shown;
the variant is where real values land):

| Contract item | praxis-proxy | lightspeed-hub | keycloak-oidc |
| --- | --- | --- | --- |
| Target header | `a2aUpstream.targetHeader: X-Hub-Cluster` | `adapter.targetHeader` (adapter `--target-header`) | — |
| Adapter Service | `a2aUpstream.service/namespace/port` (default `lightspeed-hub`/`lightspeed-hub`/`8443`) | this chart's Service (`service.port` 8443, TLS via service-serving cert — see `adapter.tls`) | — |
| Hub audience | — | `adapter.hubAudience` (adapter `--hub-audience`) | `keycloak.hubExchange.hubAudience` |
| Exchange client | — | `adapter.keycloak.clientId` + `clientSecretSecret.name` | `keycloak.hubExchange.clientId` + `secretName` |
| Allowed targets | `spokeGateways` keys (the caller×target allow-list IS the spoke map) | `adapter.allowedTargets` (adapter `--allowed-targets`, fail-fast) + `admissionPolicy.allowTargets` values (server-side hard gate) | — |
| Run-creators group | — | `adapter.hubRunCreatorsGroup` | the group carried on H via `hubExchange.groupMappers` |

**`adapter.allowedTargets` invariant:** this list MUST stay identical to
praxis-proxy's `spokeGateways` keys. It is the adapter's fail-fast
`--allowed-targets` behavior (an unknown target fails the task BEFORE any
AgenticRun or run-token Secret is created, with a self-correcting
"unknown target ... available clusters" message); the admission policy
(`admissionPolicy.allowTargets`) is the hard gate either way. Empty (default)
renders `--allowed-targets=` (empty value), which the adapter treats as no
enforcement.

Design decision settled for this leg (see praxis-proxy's
`a2aUpstream` values comment): **Praxis validates Token A + routes +
enforces caller×target; the ADAPTER performs the A→H exchange** with its
M1 exchanger. Praxis-side exchange (`a2aUpstream.exchangeAtPraxis`) is
documented but unsupported — it fails the render loudly — because
front-door delegation would replace the outbound Authorization with H and
hide Token A from the adapter, which needs A for the run leg (no
protected-A handoff exists on the pinned Praxis image).

**Praxis→adapter TLS (implemented; live issuance UNCONFIRMED):** the
adapter serves TLS (`--tls-cert-file`/`--tls-key-file`, which
`config.Validate` requires together or both empty). The default
(`adapter.tls.enabled=true`, port 8443) wires an OpenShift service-serving
cert: the Service carries `serving-cert-secret-name` and the Deployment
mounts the minted `tls.crt`/`tls.key`. Not yet observed: the service-ca
operator issuing the cert on a live install. `adapter.tls.enabled=false`
(plain HTTP) is **local dev only**; the NetworkPolicy remains the
in-cluster compensating control.

**Duplicate target headers:** the pinned Praxis router OR-matches repeated
header values, so the gateway cannot reject a multi-valued target header. The
adapter must reject duplicates itself (plan adapter-scope item 5), and the
admission policy gates what a smuggled request achieves.

## Admission policy (design §4.3) — SKELETON, deny-all default


Gateway enforcement is bypassable (direct kube callers skip Praxis), so a
ValidatingAdmissionPolicy gates CREATE/UPDATE of `agenticruns`, validating
`spec.targetCluster` against the **authenticated requester's** permitted
targets. It mirrors repo C's `config/admission/agenticrun-suspension-policy.yaml`
CEL + paramKind pattern, but with a ConfigMap parameter.

**The exact principal→spoke mapping is an open design item.** The safe
default implemented here: **deny-all unless an explicit allow entry
matches** — and a *missing* param ConfigMap also denies
(`parameterNotFoundAction: Deny`), so the policy never fails open. Hub runs
(no `spec.targetCluster`) are allowed through. Param ConfigMap shape:

```yaml
data:
  # key = authenticated username OR group name (Token H's mapped identity),
  # value = comma-separated allowed targetCluster names
  "keycloak:acme-agent-rca": "example-spoke"
```

The policy is namespace-scoped to the run namespace. Fallback design (the
adapter stamps a principal annotation and the webhook cross-checks it) is
recorded in design §4.3 if group granularity proves too coarse.

## Agentic-operator stack wiring

With `agenticOperator.enabled` (default) the chart deploys the **FULL
operator stack** — CRDs + RBAC + Deployment + configuration ConfigMap — so
the chart alone brings up a working repo C installation:

- **CRDs** (`agentic-operator-crds.yaml`): all ten
  `agentic.openshift.io` CRDs vendored from repo C's `config/crd/bases`
  (see below).
- **Operator RBAC** (`agentic-operator-rbac.yaml`): repo C's
  `config/rbac/role.yaml` rules under chart names (see below).
- **Configuration ConfigMap** (`lightspeed-agentic-configuration`, repo C's
  well-known name from `pkg/configuration/constants.go`) carrying
  `sandbox-credential-mode: gateway`, `gateway-server-url-template`,
  `gateway-ca-secret` — repo C fails loudly without the latter two in
  gateway mode, and this chart mirrors that at Helm render time. The two
  gateway keys are **owned by the praxis-proxy spoke-gateway implementer**:
  `gateway-server-url-template` must match the per-spoke gateway Service
  naming (`praxis-spoke-{targetCluster}...`), and `gateway-ca-secret` must
  name the Secret the praxis-proxy `spokeGatewayConfig.servingCA` bridge
  maintains, with its `targetNamespace` set to this chart's namespace.
- **Operator Deployment**: the image is pinned in
  `variants/standalone/values-standalone.yaml` to the quay ref built from
  repo C's `a2a-support` branch (the chart default is a placeholder, per
  repo convention); repo C's actual manager flags are UNCONFIRMED (see
  the register).
- **Webhook serving cert** (`<fullname>-agentic-operator-webhook` Service):
  repo C's manager registers its admission webhooks unconditionally and exits
  at startup without `tls.crt` at its CertDir. The chart provisions an
  OpenShift service-ca serving cert and mounts it at
  `/tmp/k8s-webhook-server/serving-certs` (port 9443).
  **KNOWN LIMITATION**: no Mutating/ValidatingWebhookConfiguration is
  rendered yet, so the endpoints are served but never invoked — the
  AgenticRunApproval approver-identity mutator and the Agent validator are
  effectively OFF. Follow-up: render the webhook configurations with this
  Service's CA bundle (config-import from repo C's `config/webhook`,
  adapted to service-ca like the CRD vendoring).

### CRD source (vendored, values-gated templates)

The `AgenticRun` / `AgenticRunApproval` / `*Result` / `Agent` /
`LLMProvider` / `ApprovalPolicy` / `AgenticOLSConfig` CRDs are **vendored
into this chart** (`templates/agentic-operator-crds.yaml`) from repo C's
`config/crd/bases`, branch `a2a-support` — each vendored file's provenance
header records the source repo, branch, commit and the re-vendor
instruction (re-vendor on every operator change; repo C's upstream
schema-drift test is the drift guard).

**Mechanism decision — `templates/` with a values gate
(`agenticOperator.crds.install`, default true) + `argocd.argoproj.io/
sync-wave: "-1"`, NOT Helm's native `crds/` directory.** Trade-offs:

- Helm's `crds/` is applied on `helm install` only — never upgraded,
  never deleted — and, decisively for this pattern, **ArgoCD does not
  apply a Helm chart's `crds/` directory at all** when rendering Helm
  charts; since every chart here deploys as an ArgoCD Application, the
  CRDs would silently never land.
- `crds/` also cannot be gated: it would install even with
  `enabled: false`, breaking this chart's dormant-by-default guarantee
  (and the `test_default_render_is_empty` pin).
- A template, by contrast, is ArgoCD-synced, upgradable (schema changes
  land with chart upgrades), deletable on uninstall, and gated. The cost:
  ArgoCD could theoretically apply the operator Deployment before the
  CRDs — mitigated by the sync-wave annotation (CRDs at −1, Deployment at
  the default wave 0), pinned by test.
- Remaining trade-off vs `crds/`: CRD deletion on release uninstall is
  usually *desirable* here (the pattern owns the whole stack), but note
  Helm prune of CRDs takes every CR instance with it.

### Operator RBAC (vendored, chart-named)

`templates/agentic-operator-rbac.yaml` carries repo C's
`config/rbac/role.yaml` rules **verbatim**, adapted only in naming:
`ClusterRole`/`ClusterRoleBinding` are `<fullname>-agentic-operator-manager`
so the release owns them, and the binding subjects **this chart's**
`<fullname>-agentic-operator` SA in the run namespace (upstream binds a
`controller-manager` SA in a placeholder namespace). Both sit at
sync-wave −1 (before the operator Deployment), so the manager does not
crash-loop waiting for permissions on first install.

**Upstream semantics preserved deliberately:** the grant is cluster-wide
(upstream `role_binding.yaml` is a ClusterRoleBinding), because the rules
include cluster-scoped resources (`customresourcedefinitions` get,
`clusterroles` bind, `clusterrolebindings` create/delete — repo C creates
spoke-side sandbox RBAC). That means `secrets` create/delete/update are
granted **cluster-wide**, exactly as in repo C's own `make deploy`.
Narrowing to the run namespace (design §8.3 single-namespace hardening)
is a follow-up item — kept in the register below.

## Agent configuration (`agenticOperator.agent`)

The operator cannot run anything without three CLUSTER-scoped CRs, which
`templates/agentic-agent.yaml` renders when `agenticOperator.agent.enabled`
(adapted from the mao-poc-rca branch's lightspeed-agentic-operator chart):

- an `LLMProvider` (`GoogleCloudVertex`; project/region/modelProvider from
  values, no defaults -- set in the variant) whose credentials Secret is an
  ExternalSecret re-keying the shared Vault ADC record
  (`secret/data/global/llm-creds-vertex`, the one mao/mas-worker uses) to
  `GOOGLE_APPLICATION_CREDENTIALS`;
- an `Agent` named **`adapter.runAgent`** (so the agent the adapter requests
  always exists) with the model from values;
- the `ApprovalPolicy` named `cluster` with **Analysis Automatic** (adapter
  runs are analysis-only) and Execution Manual.

The standalone variant uses the same model as the MAS orchestrator
(`gemini-3.8-flash`, `modelProvider: Google`, project
`redhat-marketplace-dev`, region `global` -- `setup-mas-blueprint.sh`
defaults); keep them in sync.

The chart installs what the operator needs to run: CRDs, operator, and an
Agent/LLMProvider -- without them every run fails terminally with
`Agent "default" not found`, and without an ApprovalPolicy Analysis waits for
a human (only Escalation auto-approves). Both were missed until a run first
got past admission; see [`docs/LESSONS_LEARNED.md` §8](../../../docs/LESSONS_LEARNED.md#8-lightspeed-hub--agentic-operator).

UNCONFIRMED: the Vault record is a gcloud `authorized_user` ADC, not the
service-account key the operator's API comment describes (google-auth accepts
both); and the sandbox's Gemini support is untested live.

## Tokens the adapter handles (hub-gateway chain)

Diagrams: `docs/agentic-architecture-overview.html` and the step-by-step
`docs/agentic-e2e-token-flow.html`; per-hop reference in
`agents/AUTHENTICATION.md`.

With `adapter.inboundHubToken` the adapter's inbound bearer is **Token H**,
already exchanged from the caller's Token A by the praxis hub gateway. The
adapter creates the AgenticRun with it directly (no exchange) and, with
`adapter.runTokenAudience`, exchanges H for the **run token R**. It
authenticates as `adapter.keycloak.clientId` = H's audience
(`lightspeed-hub-a2a`), the only client Keycloak lets exchange H. It stores R,
not its inbound token, in `ls-run-token-<run UID>` (key `token-a`: the
operator's contract name, now holding R).
- **R's properties:** `aud: praxis-spoke-gateway`, lifespan from keycloak-oidc
  `hubExchange.runTokenLifespanSeconds` (1800 s), usable only at the spoke
  gateway, which exchanges it per call for U.
- **R_exec's properties:** when remediation is enabled, the adapter separately
  exchanges H for `aud: praxis-spoke-gateway-exec` for the approved Execution
  stage. Praxis's execution gateway accepts that audience, not R's. Keycloak
  26 is expected to downscope to the requested audience even though both
  audience mappers are configured on `lightspeed-hub-a2a`; verify R/R_exec
  claims on the deployed realm before activation (not yet live-verified).
- **Renewal:** a fresh H from the caller is exchanged into a fresh R the same
  way.
- **Why:** storing the caller's 5-minute Token A made investigations die
  about 5 minutes in. See `docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md` s2 and
  [`docs/LESSONS_LEARNED.md` §2](../../../docs/LESSONS_LEARNED.md#2-keycloak-realm-import-and-clients).

## Identity-scoped RBAC (run creation vs spoke reads)

With keycloak-oidc `exchangeRoles`, Token H and Token U carry different,
audience-scoped role groups, and this chart binds each to its own job:

| Group (as the kube API sees it) | Token | Bound by | Grants |
|---|---|---|---|
| `keycloak:lightspeed-hub-a2a:agenticrun-creator` | H | `adapter.hubRunCreatorsGroup`, `admissionPolicy.allowTargets` | create AgenticRuns in the run namespace, for allowed targets |
| `keycloak:lightspeed-hub-a2a:agenticrun-approver` | H | `adapter.hubRunApproversGroup` | get/update AgenticRunApprovals in the run namespace |
| `keycloak:praxis-spoke-hub:reader` | U | `spokeReaderRBAC` (hub as its own spoke) | `cluster-reader`, `cluster-monitoring-view` |
| `keycloak:praxis-spoke-hub:writer` | U_exec | `spokeExecutorRBAC` (hub as its own spoke) | the explicitly enumerated `spokeExecutorRBAC.rules` only |

In gateway credential mode the operator creates no spoke ServiceAccounts or
bindings: these group bindings are the only spoke authorization (and before
them, nothing granted spoke reads at all). For a separate spoke, the
equivalent binding belongs on that cluster, keyed on its own audience's role
group.
See `docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md`.

## Approved Execution (disabled by default)

The remediation path is separate from the read-only investigation path:

```text
H --exchange(aud=praxis-spoke-gateway)----> R ----> praxis-spoke-<target>
H --exchange(aud=praxis-spoke-gateway-exec)-> R_exec -> praxis-spoke-<target>-exec
                                                   -> U / U_exec -> spoke API
```

Analysis and Verification continue to use `gateway-server-url-template` and
the read token `R`. Only the Execution stage uses
`gateway-execution-server-url-template` and `R_exec`. The separate client/aud
is important: possessing `R` must not authenticate to an execution gateway.
The adapter's `--execution-token-audience` is omitted by default, the operator
ConfigMap execution key is absent by default, and Praxis renders no `-exec`
gateway unless `spokeGatewayConfig.execution.enabled=true`. Rendering an
execution audience without the matching read audience, gateway template, or
gateway-credential mode fails closed.

Keycloak must scope the ordinary spoke exchanger to `reader` only and the
execution exchanger to `reader` plus `writer`. The
`agenticrun-approver` hub role and `writer` spoke role are granted only to
`keycloak.exchangeRoles.remediation.groups`; the ordinary `exchangeRoles.groups`
remain reader/creator-only. A user who should execute must therefore belong to
both the ordinary read group and the designated remediation group. Approval is
an explicit Manual Execution decision in the `cluster` ApprovalPolicy; H's
approver group authorizes the decision, while U_exec's writer group authorizes
the subsequent Kubernetes writes. Neither token is a cluster-admin role.

`spokeExecutorRBAC.rules` is deliberately enumerated. Defaults include
`pods/eviction` for the eviction API, but do not include Secrets,
`pods/exec`, `pods/attach`, or `pods/portforward`, RBAC mutation, or
wildcard resources/verbs. The ClusterRole is bound **only** by namespaced
RoleBindings in the required, explicit `spokeExecutorRBAC.namespaces`
allow-list; no ClusterRoleBinding is rendered. `default`, `openshift*`,
`kube-*`, this chart's own release/run namespace, and the default
`forbiddenNamespaces` (Praxis, mao, Keycloak, OpenShift GitOps, Vault, and
External Secrets namespaces) are rejected at render time. Add more
control-plane or credential-bearing namespaces to that denylist; do not
remove its protected defaults.

Namespace scoping is not full workload isolation: **within each allowed
namespace, this role can still create/patch workloads to run as any
ServiceAccount and mount that namespace's Secrets** (residual risk similar to
Kubernetes `edit`). Keep privileged ServiceAccounts out of remediation
namespaces or add admission policy that blocks their use. The standalone demo
workload namespace `payments` (see `agenticrun_test.yaml`) is an example of an
allow-list entry, not an enabled default. This chart creates bindings only on
the cluster where it is installed (the hub's own spoke in standalone); for a
real separate spoke, deliver equivalent RoleBindings on that cluster via its
GitOps configuration.

### Rollout order

1. Provision the distinct Vault entry `praxis-spoke-gateway-exec` using
   `alphaNumericPolicy`; never reuse the read gateway credential.
2. On an already-imported realm, perform the one-time Keycloak Admin API
   setup in the keycloak-oidc README: execution exchanger, its target-audience
   role scopes, the R_exec audience mapper on `lightspeed-hub-a2a`, and the
   designated remediation-group assignments. Refresh ExternalSecrets and
   sync `keycloak-oidc`; verify fresh `R` and `R_exec` tokens have distinct
   audiences/groups before proceeding. Realm import is one-shot and the
   PostSync reconciler repairs secrets only; it does not repair roles/mappers.
3. Enable the Praxis execution exchanger and sync `praxis-proxy`. Confirm the
   per-target `praxis-spoke-<target>-exec` Services/Deployments use the
   execution secret and a distinct inbound audience, while read gateway
   resources remain unchanged.
4. Enable the adapter execution audience, execution URL template, approver
   group and reviewed executor bindings in the variant. Sync the adapter and
   operator only after their pinned images support `--execution-token-audience`
   and the `gateway-execution-server-url-template` key.
5. Verify Analysis/Verification still use the read gateway, an unapproved
   Execution remains Manual, only the approver group can approve, and an
   approved Execution reaches the writer gateway with U_exec. Probe both
   audience/group combinations with fresh tokens; do not infer separation
   from chart values alone.

## Adapter Deployment contract (reconciled M4)

The adapter is intentionally constrained to exactly one replica, and its
Deployment uses the `Recreate` strategy so an old and new adapter cannot
overlap during rollout. The current per-run lock is process-local and does not
coordinate across replicas. **Temporary constraint:** remove the replica
guard and `Recreate` strategy only when the adapter implements distributed
per-run locking.

The adapter container runs with the **real flags** from
`a2a-adapter/internal/config/config.go` (the source of truth — do not
rename one side without the other):

- `--listen=:<adapter.port>`, `--namespace=<run namespace>`,
  `--keycloak-issuer` / `--keycloak-token-endpoint` (both **must be
  https** — the adapter rejects anything else at startup; the chart
  derives them from the cluster domain by default),
  `--hub-audience`, `--target-header`, `--run-agent`;
- card flags `--card-name/-description/-url/-version/-protocol-version/
  -skill-id/-skill-name/-skill-description`;
- `--run-token-audience` (read R) and, only when enabled,
  `--execution-token-audience` (write-capable R_exec);
- the M3 bounds `--clock-skew`, `--stream-deadline`, `--renewal-timeout`,
  `--max-credential-pauses`, `--terminal-cleanup-grace`,
  `--sweep-interval` (values defaults mirror the adapter's flag defaults).

The Keycloak client **secret** rides the `KEYCLOAK_CLIENT_SECRET` env via
`secretKeyRef` (the flag's documented env fallback). **SVID mode**
(`adapter.svid.enabled`, default off) swaps the confidential client for a
SPIFFE JWT-SVID: no `--keycloak-client-id`, no secret env, and the
Workload API socket is passed via the `SPIFFE_SOCKET` env — mirroring the
adapter's `config.Validate` client-auth exclusivity (setting
`adapter.keycloak.clientId` together with `svid.enabled` fails at render
time, exactly as the adapter would fail at startup). The chart passes the
socket path through; providing the socket itself (SPIRE agent volume) is
the variant's/deployer's job — see the open-items register.

**Card name alignment:** `adapter.agentCard.name` defaults to
`lightspeed-hub`, the ADAPTER FLAG default (`--card-name`) — the earlier
chart default `lightspeed-hub-adapter` was drift; the chart was aligned to
the adapter side.

**Probes** hit `GET /healthz`: the adapter's real, unauthenticated
health endpoint (safe by construction — internal-only Service behind the
NetworkPolicy, which admits only the praxis-proxy namespace).

**§6.4 pause durability note:** the adapter persists the credential-pause
counter on each AgenticRun as the `a2a.lightspeed.io/credential-pauses`
annotation and anchors the renewal deadline to the `CredentialsExpired`
condition's `lastTransitionTime`, so restarts cannot reset the
deadline/cap — that is why `agenticruns` carries the `update` verb above.

## Values

Everything cluster-specific (image pins, hub audience, Keycloak client,
gateway URL template, spoke names) belongs in
`variants/standalone/values-standalone.yaml` — never in this chart's
defaults. See the fully commented example block there
(`example.test` placeholders). Required-when-enabled values fail loudly at
render time: `adapter.hubAudience`, `adapter.keycloak.clientId` +
`clientSecretSecret.name`, a derivable Keycloak issuer, and the two gateway
keys when `sandboxCredentialMode=gateway`.

## PLACEHOLDER / UNCONFIRMED GUESS register

Remaining slots to fill before enabling (resolved items were struck from
this register in the M4 reconciliation pass):

1. **Adapter image** — `quay.io/example/lightspeed-hub-adapter:placeholder`
   (the adapter source now lives in the lightspeed-hub repo, branch `a2a`,
   nested module `a2a-adapter/`, built by its `make image-a2a-adapter`;
   pin the published digest/exact quoted tag in the variant when that
   build lands in a registry).
2. **Run namespace** — Q4: confirm on a live repo C `a2a-support` install
   that sandboxes/runs share the operator namespace.
3. **Target header name** — `X-Hub-Cluster` is the design doc's proposal,
   to be decided with the concurrent praxis work.
4. **SVID socket wiring** — `adapter.svid.enabled` passes `SPIFFE_SOCKET`
   through, but the chart does not deploy/mount a SPIRE Workload API
   socket; the variant must wire the volume (hostPath/CSI) for SVID mode
   to actually work on-cluster.
5. **Operator image** — the chart default remains a placeholder per repo
   convention; the real pin (the quay ref built from repo C's
   `a2a-support` branch) lives in `variants/standalone/values-standalone.yaml`.
   Manager args are the standard controller-runtime shape, unverified
   against repo C's `config/manager`.
6. **Operator RBAC breadth** — the vendored ClusterRole/Binding grants
   `secrets` create/delete/update cluster-wide (upstream semantics, see
   "Operator RBAC" above); narrowing to the run namespace is a design §8.3
   hardening follow-up.
7. **NetworkPolicy** — namespaceSelector on `praxis-proxy` only; pod-
   selector narrowing and egress rules (kube API / Keycloak / DNS) are
   follow-ups; CNI behavior unverified.
8. **Admission-policy mapping** — deny-all default; the real
   principal→spokes mapping is the open design item (design §4.3).
9. **`oc auth can-i` escalation probes** in CI — design §8.1 item 2, M4
   hardening.
