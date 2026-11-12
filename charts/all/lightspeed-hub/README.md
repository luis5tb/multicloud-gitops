# lightspeed-hub (Task B / M4 skeleton)

Deploys the **hub A2A adapter** (the net-new Go service designed in
`docs/LIGHTSPEED_HUB_ADAPTER_DESIGN.md`) plus the
**lightspeed-agentic-operator** (repo C, branch `a2a-support`) stack it
drives, on the hub cluster. This is the replacement investigation backend
for the removed OLS (`PLAN-lightspeed-hub-migration.md` Task A; this chart
is Task B).

**Status: M4 SKELETON, DISABLED BY DEFAULT.** The Go adapter is being built
in parallel (milestones M1-M3). With `enabled: false` (the default) this
chart renders **no Kubernetes resources** — the pattern can carry it dormant
exactly like `charts/all/mao/mas-a2a`'s `a2a.enabled` gating. Every
PLACEHOLDER / UNCONFIRMED GUESS is listed at the bottom of this README;
each one is a slot the M1-M3 outputs must fill before activation.

## Contents

| Template | What it is |
| --- | --- |
| `deployment.yaml` | Adapter Deployment (placeholder image; REAL adapter flags — the adapter's `internal/config/config.go` in the lightspeed-hub repo, branch `a2a`, nested module `a2a-adapter/`, is the source of truth) |
| `service.yaml` | Internal-only ClusterIP Service — **no Route, ever** |
| `serviceaccount.yaml` | Dedicated adapter ServiceAccount |
| `networkpolicy.yaml` | Ingress admitting only the praxis-proxy namespace |
| `run-token-writer-rbac.yaml` | Role/RoleBinding for the `ls-run-token-*` Secret writer + watch grants (design §5.2; verbs reconciled against the M3 adapter) |
| `run-creators-rbac.yaml` | "Only the configured group can create AgenticRuns": Role (create on `agenticruns`) + Group RoleBinding when `adapter.hubRunCreatorsGroup` is set |
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
adapter now serves TLS (a2a-adapter `--tls-cert-file`/`--tls-key-file`,
which its `config.Validate` requires to be set together or both empty).
This chart's default (`adapter.tls.enabled=true`, port 8443) wires it to
an OpenShift service-serving cert: the Service carries
`service.beta.openshift.io/serving-cert-secret-name`, and the Deployment
mounts the minted Secret (`tls.crt`/`tls.key`) for the two flags — the
shape praxis-proxy's `a2aUpstream` expects (TLS on 8443, verified
against the service-serving CA). Remaining UNCONFIRMED: live-cluster
confirmation that the service-ca operator actually issues the cert for
this Service (the annotation is standard OpenShift, but issuance has not
been observed on a live install). `adapter.tls.enabled=false` (plain
HTTP, both flags omitted) is a **local dev only** override. The
NetworkPolicy (praxis-proxy namespace only) remains the in-cluster
compensating control.

**Duplicate target headers:** the pinned Praxis router OR-matches
repeated header values (source-verified against Core v0.7.0 — see
praxis-proxy's `files/praxis.yaml` comment), so the gateway cannot reject
a multi-valued target header. The ADAPTER must reject
duplicate/multi-valued target headers itself (plan adapter-scope item 5),
and the admission policy gates what a smuggled request achieves anyway.

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

## Adapter Deployment contract (reconciled M4)

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

Resolved in the M4 reconciliation (kept for the history):

- ~~CRD + operator RBAC vendoring~~ — both vendored
  (`agentic-operator-crds.yaml`, `agentic-operator-rbac.yaml`) from repo C
  `a2a-support` with provenance headers; values-gated templates + sync-wave
  −1, not Helm's `crds/` dir (ArgoCD never applies it — see "CRD source").
- ~~Adapter env var names~~ — the Deployment now carries the adapter's
  real flags (`--listen`, `--namespace`, `--keycloak-*`, `--hub-audience`,
  card flags, M3 bound flags); the secret rides `KEYCLOAK_CLIENT_SECRET`
  via `secretKeyRef`.
- ~~Probe paths~~ — `/healthz` is the adapter's real endpoint
  (`server.HealthHandler`), unauthenticated by design.
- ~~Adapter watch/patch RBAC verbs~~ — re-derived from the M3
  implementation (see the RBAC table above): `delete` on secrets added,
  `agenticruns/status` dropped, `agenticrunapprovals` reduced to `get`,
  `update` on `agenticruns` for the pause-count annotation.
- ~~Card name default drift~~ — chart aligned to the adapter flag default
  `lightspeed-hub`.
