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
| `deployment.yaml` | Adapter Deployment (placeholder image; REAL adapter flags — `hub-adapter/internal/config/config.go` is the source of truth) |
| `service.yaml` | Internal-only ClusterIP Service — **no Route, ever** |
| `serviceaccount.yaml` | Dedicated adapter ServiceAccount |
| `networkpolicy.yaml` | Ingress admitting only the praxis-proxy namespace |
| `run-token-writer-rbac.yaml` | Role/RoleBinding for the `ls-run-token-*` Secret writer + watch grants (design §5.2; verbs reconciled against the M3 adapter) |
| `admission-policy.yaml` | ValidatingAdmissionPolicy + binding + param ConfigMap for requester×target authorization (design §4.3) |
| `agentic-operator.yaml` | Repo C operator Deployment + `lightspeed-agentic-configuration` ConfigMap + operator SA |

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
  `hub-adapter/internal/server/watch.go` `Sweep`/`RunDeleted`). No
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
built adapter actually does (hub-adapter README "RBAC notes"):

| Resource | Verbs | Why |
| --- | --- | --- |
| `secrets` | create/get/update/**delete** | run-token write/renew (§5) + terminal/deletion cleanup (§5.5) |
| `agenticruns` | get/list/watch/**update** | reads + informers; `update` is ONLY the §6.4 pause-count annotation `a2a.lightspeed.io/credential-pauses` (restart-durable pause cap) |
| `agenticruns/status` | — **none** | the adapter never writes status or conditions (design §6.1); the earlier skeleton grant was dropped |
| `agenticrunapprovals` | **get** only | decisions are patched **as the caller** (Token H) via hub `Authentication`/RoleBinding wiring (keycloak-oidc chart) |
| `*results` (4 kinds) | get/list/watch | artifact/usage projection + informers |

Escalation checks for CI (`oc auth can-i` probes, design §8.1 item 2) are an
M4 hardening item — not yet wired.

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
- **Operator Deployment**: placeholder image + standard controller-runtime
  shape; repo C's actual manager flags are UNCONFIRMED (see below).

### CRD source (open decision)

The `AgenticRun` / `AgenticRunApproval` / `*Result` / `Agent` /
`LLMProvider` / `ApprovalPolicy` / `AgenticOLSConfig` CRDs are **not
rendered by this chart**. Intended source: the lightspeed-agentic-operator
repo's `config/crd/bases` (ten files). Options deferred to the M4 hardening
pass:

1. **Vendor** the CRD YAMLs into this chart (drift risk with the pinned
   operator image; needs a re-sync convention like the vendored rhbk chart).
2. Render repo C's kustomize `make deploy` output into the chart at pin
   time (re-render on every image bump).

For the skeleton, `agenticOperator.crds.install: false` exists as a values
anchor documenting the intent and renders nothing. **The chart alone cannot
bring up a working stack until this decision lands.**

### Operator RBAC (not rendered)

Repo C's own `config/rbac` (role, leader election, run approver) governs its
operator SA. Vendoring it here has the same open decision as the CRDs; the
skeleton renders only a dedicated operator ServiceAccount. The operator
Deployment will not be functional until its RBAC lands with the CRD
decision.

## Adapter Deployment contract (reconciled M4)

The adapter container runs with the **real flags** from
`hub-adapter/internal/config/config.go` (the source of truth — do not
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
   (M1-M3 build; pin digest/exact quoted tag in the variant).
2. **Run namespace** — Q4: confirm on a live repo C `a2a-support` install
   that sandboxes/runs share the operator namespace.
3. **Target header name** — `X-Hub-Cluster` is the design doc's proposal,
   to be decided with the concurrent praxis work.
4. **SVID socket wiring** — `adapter.svid.enabled` passes `SPIFFE_SOCKET`
   through, but the chart does not deploy/mount a SPIRE Workload API
   socket; the variant must wire the volume (hostPath/CSI) for SVID mode
   to actually work on-cluster.
5. **Operator image** — placeholder pending the pinned repo C `a2a-support`
   build; manager args are the standard controller-runtime shape, unverified
   against repo C's `config/manager`.
6. **CRD + operator RBAC vendoring** — open decision (above); nothing
   rendered.
7. **NetworkPolicy** — namespaceSelector on `praxis-proxy` only; pod-
   selector narrowing and egress rules (kube API / Keycloak / DNS) are
   follow-ups; CNI behavior unverified.
8. **Admission-policy mapping** — deny-all default; the real
   principal→spokes mapping is the open design item (design §4.3).
9. **`oc auth can-i` escalation probes** in CI — design §8.1 item 2, M4
   hardening.

Resolved in the M4 reconciliation (kept for the history):

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
