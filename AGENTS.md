# AGENTS.md

Concise orientation for any agent (AI or human) about to change code here.
Read this first; it says *where* things live and lists the commands. For the
full design narrative, the architecture decisions behind this pattern, and the
hard-won lessons from deploying it live, see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) -- read the relevant lesson there
before touching adjacent code. Chart-specific caveats live in each
`charts/all/<name>/README.md`; the human deployment walkthrough is in
[README.md](README.md).

## What this repo is

`multicloud-gitops` is a Red Hat Validated Patterns GitOps repo: ArgoCD's
"clusterGroup" pattern deploys the Helm charts in `charts/all/*` as
Applications, parameterized per-cluster by
`variants/<variant>/values-<variant>.yaml` (only `standalone` is live).

This branch's payload is an **agentic OpenShift investigation pattern**: a
chat agent (ACME) answers "what's wrong with my cluster" by delegating to
OpenShift Lightspeed (OLS), which uses MCP tools to query the live cluster --
with the caller's identity propagated end-to-end through Keycloak + SPIFFE +
an RFC 8693 token exchange, behind a Praxis policy gateway. The full picture,
and why each piece is shaped the way it is, is in
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
| Install the pattern onto a cluster | `make install` |
| List all Make targets | `make help` |

Linting runs in CI via super-linter (`.github/workflows/superlinter.yml`).

## Repository layout (brief)

- `charts/all/<name>/` -- one Helm chart per ArgoCD Application
  (`keycloak-oidc`, `praxis-proxy`, `openshift-lightspeed-config`,
  `acme-agent`, plus unrelated `mao/*`/`rhoai-config`). Each has its own
  `README.md`.
- `variants/standalone/values-standalone.yaml` -- the **only** place
  cluster-specific values belong (URLs, trust domain, image tags, the
  `global.olsClusters` registry, RBAC scope).
- `values-secret.yaml.template` -- Vault-backed secrets via External Secrets;
  never commit a real secret anywhere else.
- `agents/acme_agent/` -- the ACME agent's Python source;
  `agents/AUTHENTICATION.md` is the authoritative identity-chain + auth
  troubleshooting write-up.
- OLS A2A image source -- external fork
  [`luis5tb/lightspeed-service@a2a`](https://github.com/luis5tb/lightspeed-service/tree/a2a);
  build/push steps are in the top-level `README.md` Phase 0 (this pattern no
  longer vendors that tree).
- `README.md` (deploy walkthrough), `LIGHTSPEED_DESIGN.md` (pre-cluster
  hypothesis doc -- cross-check before trusting), `docs/ARCHITECTURE.md`
  (design + decisions + hard-won lessons).

## Where do I change X?

| Task | File(s) |
| --- | --- |
| Agent's prompt / routing / cluster-URL requirement | `agents/acme_agent/src/acme_agent/agent.py` |
| Cluster-id derivation or validation | `agents/acme_agent/src/acme_agent/cluster_registry.py` -- mirror any change everywhere else this id is computed |
| ACME's outbound auth (Keycloak, SPIFFE, TLS trust) | `agents/acme_agent/src/acme_agent/auth.py` |
| Keycloak realm/clients/mappers/groups | `charts/all/keycloak-oidc/templates/keycloak-realm-import.yaml` + `values.yaml` |
| MCP investigation RBAC scope | `charts/all/keycloak-oidc/templates/lightspeed-mcp-rbac.yaml`, set via `lightspeedRbac.*` overrides in `values-standalone.yaml` |
| OpenShift Native OIDC resource | `charts/all/keycloak-oidc/templates/openshift-authentication.yaml` |
| Praxis routing / allow-list policy | `charts/all/praxis-proxy/files/{praxis.yaml,policy.yaml}` |
| OLS LLM provider / MCP introspection | `charts/all/openshift-lightspeed-config/templates/olsconfig.yaml` + `values.yaml` |
| Custom OLS image or A2A env vars (temporary bridge) | `charts/all/openshift-lightspeed-config`'s `appServerPatch.*` values + `files/patch_appserver.py` |
| OLS's own A2A endpoint / auth | [`luis5tb/lightspeed-service@a2a`](https://github.com/luis5tb/lightspeed-service/tree/a2a) (`ols/app/endpoints/{a2a.py,a2a_auth.py,a2a_executor.py}`); rebuild/push the image and bump `appServerPatch.image.*` in `values-standalone.yaml` |
| Any cluster-specific value (URLs, trust domain, image tags, namespace scope) | `variants/standalone/values-standalone.yaml` only |
| New secret | `values-secret.yaml.template`, consumed via ExternalSecret -- never a chart default |

## Working conventions

- Every chart's own `README.md` documents its specific "UNCONFIRMED GUESS" /
  "SPECULATIVE" items (pod labels, CRD field names, ServiceAccount names,
  Subscription channel/source). When you confirm or fix one against a live
  cluster, update that comment/README immediately -- otherwise the next person
  re-does the same investigation from scratch.
- When you find a bug by actually running something (not just reading code),
  write down *how it was actually hit* and *why nothing caught it sooner* in
  the relevant README/Troubleshooting section (see `agents/AUTHENTICATION.md`
  and each chart's README for the style), and add a one-liner to
  `docs/ARCHITECTURE.md`'s "Hard-won lessons" -- that context is what makes the
  next debugging session fast instead of a repeat investigation.
- Temporary/bridge mechanisms (`appServerPatch` is the current example) must
  say so explicitly in both the chart's `values.yaml` comment and its README,
  including the exact condition under which they should be deleted.
- Never hardcode a real cluster's hostname, trust domain, or image ref into a
  chart template or default `values.yaml`; keep those generic/empty so the
  chart works on any cluster, and put the real value in
  `values-standalone.yaml`.
