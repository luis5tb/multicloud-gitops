# AGENTS.md

Concise orientation for any agent (AI or human) about to change code here.
Read this first; it says *where* things live and lists the commands. For the
design narrative and the architecture decisions behind this pattern, see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Every live finding, render
failure and known image quirk is in
[docs/LESSONS_LEARNED.md](docs/LESSONS_LEARNED.md) -- read the relevant
section there before touching adjacent code. Chart-specific caveats live in
each `charts/all/<name>/README.md`; the human deployment walkthrough is in
[README.md](README.md).

## What this repo is

`multicloud-gitops` is a Red Hat Validated Patterns GitOps repo: ArgoCD's
"clusterGroup" pattern deploys the Helm charts in `charts/all/*` as
Applications, parameterized per-cluster by
`variants/<variant>/values-<variant>.yaml` (only `standalone` is live).

This branch's payload is an **agentic pattern**: a chat agent (ACME)
answers Red Hat questions by delegating to the MAS orchestrator (Ask Red Hat
specialists), with the caller's identity propagated end-to-end through
Keycloak + SPIFFE, behind a Praxis policy gateway. The OpenShift Lightspeed
(OLS) investigation backend and RHOAI were **removed** from this branch
(see `PLAN-lightspeed-hub-migration.md`, Task A; its replacement,
lightspeed-hub, is Task B and not built yet). The full picture, and why each
piece is shaped the way it is, is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Commands

Work in an ephemeral env (container or venv); don't install tooling onto the
host. Chart scripts under `charts/all/*/files/*.py` are stdlib-only.

| Task | Command |
| --- | --- |
| Render one chart locally | `helm template <name> charts/all/<name> [--set k=v ...]` |
| Render the whole pattern | `make show` |
| Validate values files against schema | `make validate-schema` |
| Syntax-check a chart's Python helper | `python3 -I -m py_compile charts/all/<name>/files/<script>.py` |
| Run the test suite | `make run-ci-tests` (pytest lives under `tests/`) |
| Check all ArgoCD apps are synced | `make argo-healthcheck` |
| Watch the identity/exchange chain live | `bash scripts/monitor.sh` (add `--help`; `--endpoint` prints selectors without a cluster) |
| Replay-test the monitor parser | `python3 -m unittest tests.scripts.test_monitor -v` |
| Install the pattern onto a cluster | `make install` |
| List all Make targets | `make help` |

Linting runs in CI via super-linter (`.github/workflows/superlinter.yml`).

## Repository layout (brief)

- `charts/all/<name>/` -- one Helm chart per ArgoCD Application
  (`keycloak-oidc`, `praxis-proxy`, `acme-agent`, optional `mao/mas-a2a`,
  plus other `mao/*`). Each has its own `README.md`; MAS A2A is registered
  but dormant by default. (The `openshift-lightspeed-config` and
  `rhoai-config` charts were removed -- see
  `PLAN-lightspeed-hub-migration.md` Task A.)
- `variants/standalone/values-standalone.yaml` -- the **only** place
  cluster-specific values belong (URLs, trust domain, image tags).
- `values-secret.yaml.template` -- Vault-backed secrets via External Secrets;
  never commit a real secret anywhere else.
- `agents/acme_agent/` -- the ACME agent's Python source;
  `agents/AUTHENTICATION.md` is the authoritative identity-chain + auth
  troubleshooting write-up.
- `README.md` (deploy walkthrough), `docs/ARCHITECTURE.md` (design +
  decisions), `docs/LESSONS_LEARNED.md` (all live findings and workarounds),
  `LIGHTSPEED_DESIGN.md` / `LIGHTSPEED_IMPLEMENTATION_PLAN.md` (historical
  design docs for the removed OLS backend -- cross-check before trusting).
- `docs/agentic-architecture-overview.html` / `docs/agentic-e2e-token-flow.html`
  -- component diagram and step-by-step token walkthrough (ACME -> MAS ->
  lightspeed-hub). Update them when a client, audience or hop changes.

## Where do I change X?

| Task | File(s) |
| --- | --- |
| Agent's prompt / routing | `agents/acme_agent/src/acme_agent/agent.py` |
| ACME's outbound auth (Keycloak, SPIFFE, TLS trust) | `agents/acme_agent/src/acme_agent/auth.py` |
| Keycloak realm/clients/mappers/groups | `charts/all/keycloak-oidc/templates/keycloak-realm-import.yaml` + `values.yaml` |
| OpenShift Native OIDC resource | `charts/all/keycloak-oidc/templates/openshift-authentication.yaml` |
| Praxis routing / allow-list policy | `charts/all/praxis-proxy/files/{praxis.yaml,policy.yaml}` |
| **Multi-OIDC configuration (accept tokens from multiple issuers)** | **See [`MULTI_OIDC_CONFIGURATION.md`](MULTI_OIDC_CONFIGURATION.md) for complete guide; example in [`MULTI_OIDC_EXAMPLE.yaml`](MULTI_OIDC_EXAMPLE.yaml)** |
| MAS inbound A2A blueprint, caller allow-list, Keycloak auth, HITL | `charts/all/mao/mas-a2a/` and its README; activate only after hardening `mao/mas-api` and pinning the API/worker/A2A image together |
| Hub Execution approval and read/write token split | [`docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md`](docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md); chart rollout gates in the `lightspeed-hub`, `praxis-proxy`, and `keycloak-oidc` READMEs |
| Any cluster-specific value (URLs, trust domain, image tags, namespace scope) | `variants/standalone/values-standalone.yaml` only |
| New secret | `values-secret.yaml.template`, consumed via ExternalSecret -- never a chart default |

## Working conventions

- Every chart's own `README.md` documents its specific "UNCONFIRMED GUESS" /
  "SPECULATIVE" items (pod labels, CRD field names, ServiceAccount names,
  Subscription channel/source). When you confirm or fix one against a live
  cluster, update that comment/README immediately -- otherwise the next person
  re-does the same investigation from scratch.
- When you find a bug by actually running something (not just reading code),
  add an entry to `docs/LESSONS_LEARNED.md` saying *how it was actually hit*
  and *why nothing caught it sooner* -- that context is what makes the next
  debugging session fast instead of a repeat investigation. Keep the
  surrounding README to the rule/configuration it implies, with at most a
  one-line pointer to the entry; do not paste the story into the README, the
  chart comment and the lessons doc. Keep code comments to the invariant.
- Temporary/bridge mechanisms must
  say so explicitly in both the chart's `values.yaml` comment and its README,
  including the exact condition under which they should be deleted.
- Never hardcode a real cluster's hostname, trust domain, or image ref into a
  chart template or default `values.yaml`; keep those generic/empty so the
  chart works on any cluster, and put the real value in
  `values-standalone.yaml`.
