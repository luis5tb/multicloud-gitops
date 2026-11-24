# Architecture & operational notes

The deep background for this repo: what the pattern actually does, the
architecture decisions behind it. [AGENTS.md](../AGENTS.md) is the concise
orientation and the "where do I change X" map; this file is the "why". Live
findings and operational gotchas are in
[LESSONS_LEARNED.md](LESSONS_LEARNED.md). Chart-specific caveats live in each
`charts/all/<name>/README.md`; the human deployment walkthrough is in
[README.md](../README.md).

Diagrams: [agentic-architecture-overview.html](agentic-architecture-overview.html)
(all components, Keycloak clients and token flows) and
[agentic-e2e-token-flow.html](agentic-e2e-token-flow.html) (one ACME ->
lightspeed-hub investigation, hop by hop: token received, client used, exchange,
token forwarded), plus the slide deck
[lightspeed-hub-architecture-deck.html](lightspeed-hub-architecture-deck.html)
for presenting the design. The per-hop authentication reference is
[agents/AUTHENTICATION.md](../agents/AUTHENTICATION.md); the original options
analysis is [LIGHTSPEED_HUB_IDENTITY_DESIGN.md](LIGHTSPEED_HUB_IDENTITY_DESIGN.md).

## What this repo is

`multicloud-gitops` is a Red Hat Validated Patterns GitOps repo: ArgoCD's
"clusterGroup" pattern deploys a fixed set of Helm charts (`charts/all/*`)
as Applications, parameterized per-cluster by `variants/<variant>/values-
<variant>.yaml` (currently only `standalone` is live; `hub` exists but is
not the focus of this work).

This branch's actual payload is an **agentic Red Hat assistance pattern**: a
chat agent (ACME, built on Google's ADK) delegates Red Hat-related questions
to the MAO workflow, which uses Ask Red Hat for product, project, and
documentation guidance. The caller's identity is propagated end-to-end
through Keycloak + SPIFFE, behind a Praxis Policy Engine gateway enforcing
the caller allow-list.

> **Status change (Task A, `PLAN-lightspeed-hub-migration.md`):** the
> OpenShift Lightspeed (OLS) investigation backend and RHOAI were removed
> from this branch. Everything below that describes OLS, the
> `X-OLS-Cluster` routing, the RFC 8693 `openshift-mcp` exchange, the
> `openshift-lightspeed-config` chart, `appServerPatch`, or `lightspeedRbac`
> is kept as the historical record (and teardown/rollback context); its
> replacement (lightspeed-hub) is Task B of that plan.

This replaces an earlier "RCA agent" + OPA-based pattern that lived in this
same repo; you may still see comments/leftovers referencing "RCA" or a
former `charts/all/opa` chart, since removed (it was never wired into any
Application).

## Repository layout

- `README.md` -- the deployment walkthrough for a human operator. Keep it in
  sync with reality; a stale walkthrough actively misleads the next deployer.
- `docs/LESSONS_LEARNED.md` -- every live finding, render failure and
  pinned-image quirk. Add new ones there, not inline in the READMEs.
- `LIGHTSPEED_DESIGN.md` -- HISTORICAL: the design doc for the removed OLS
  backend (see the status note above). Written entirely from
  research/documentation, **before any live cluster
  existed to test against** -- full of explicit "UNCONFIRMED GUESS"
  markers. Treat it as a hypothesis record, not ground truth; cross-check
  anything it asserts against the actual installed operator/CRD/cluster
  before relying on it, and update it (or the relevant chart) once you've
  actually confirmed something live.
- `variants/standalone/values-standalone.yaml` -- **the only place
  cluster-specific values belong.** Trust domain, issuer URLs, image refs,
  OLM Subscription channel/source -- all of it. Never hardcode a real
  cluster's hostname or
  trust domain into a chart template or default `values.yaml`; those stay
  generic/empty/placeholder so the chart works on any cluster.
- `values-secret.yaml.template` -- Vault-backed secrets (via External
  Secrets Operator). Never commit a real secret value anywhere else.
- `charts/all/<name>/` -- one Helm chart per ArgoCD Application (mostly).
  Each has its own `README.md` with chart-specific caveats -- read it before
  touching that chart.
  - `keycloak-oidc` -- the `rca` realm (3 clients, SPIFFE federation,
    `groups` mappers) and the OpenShift Native OIDC
    `Authentication/cluster` resource.
  - `praxis-proxy` -- the policy-enforcing A2A gateway. Validates the
    caller's JWT and `azp`
    allow-list via the pinned `ghcr.io/praxis-proxy/ai` image (alpha
    upstream software with documented quirks -- see its README). It
    currently fronts no upstream (see the status note above).
  - `acme-agent` -- deploys the agent built from `agents/acme_agent/`.
  - `mao/mas-a2a` -- optional, separately authenticated inbound MAS A2A
    endpoint for a persisted orchestrator blueprint; its Application is
    registered but the chart renders no resources until explicitly enabled.
    Other `mao/*` charts serve the demo/platform stack.
- `agents/acme_agent/` -- the ACME agent's Python source (ADK-based A2A
  client). `agents/AUTHENTICATION.md` is the authoritative, detailed
  write-up of the full per-request identity chain with real captured claim
  shapes and a substantial Troubleshooting section -- read it before
  debugging any auth failure in this flow. `agents/README.md` is a short
  index.
- `vendor/praxis-ai/` -- untracked, reference-only checkout of Praxis's
  source, used only while writing APL policy syntax. The deployed
  `praxis-proxy` chart pins a public upstream image; this directory is not
  built from and does not need to exist for a deployment to work.

## Core architecture decisions (don't relitigate these without reason)

> Note: the OLS-specific decisions below (identity-chain Token A/B exchange,
> Praxis-as-OLS-allow-list, `global.olsClusters`, `appServerPatch`,
> `lightspeedRbac`) are historical since the Task A removal -- kept because
> the reasoning and live findings behind them still apply to the
> replacement backend design (Task B).

- **Current (Task B) request path: one exchange per hop, each token's
  audience names its next exchanger.** Chain verified live 2026-11-20 (see
  `docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md` s2 for the token table):

  ```
  ACME --A--> MAS A2A (front door; public Route)   validates aud mas-a2a (local KC) / api.ask_red_hat (SSO)
                 | MAS forwards A verbatim
                 v
              praxis HUB gateway (praxis-proxy)    validates A (aud mas-a2a, azp allow-list), exchanges A->H as client mas-a2a
                 | H (aud lightspeed-hub-a2a, 300s)
                 v
              hub adapter --H--> hub kube API      creates the AgenticRun with H (no exchange)
                 | exchanges H->R as client lightspeed-hub-a2a; stores R (aud praxis-spoke-gateway, 1800s)
                 v
              AgenticRun sandbox --R--> praxis SPOKE gateway (praxis-spoke-<id>) --U--> spoke kube API
                                        validates R (aud praxis-spoke-gateway), exchanges R->U per call (aud praxis-spoke-<id>)
  ```

  Token A carries only `mas-a2a` and lives 5 minutes; the adapter never sees
  it. The run's stored credential R outlives A (an exchanged token gets the
  exchanging client's lifespan), but only the spoke gateway accepts it. The
  front door is the MAS A2A endpoint; the two Praxis instances are the hub
  gateway and the spoke gateways. MAS validates its own `mas-a2a` audience
  and principal allow-list. Target state: MAS performs the A->H exchange per
  delegation itself, taking over the `mas-a2a` exchanger client.

- **The identity chain is the point.** ACME authenticates to Keycloak as
  itself (SPIFFE JWT-SVID, `client_credentials`), gets "Token A", sends it
  through Praxis to OLS, and OLS (with its OIDC authentication module and
  optional A2A token exchange explicitly enabled for this pattern) performs
  an RFC 8693 token exchange (`subject_token=Token A`) to get "Token B"
  scoped to `openshift-mcp`. Token B's `sub` is unchanged from Token A, so
  the eventual API server RBAC check is attributed to the original caller.
  Token A never reaches MCP or the API server; only Token B does. See
  `agents/AUTHENTICATION.md`.
- **ACME provider selection is request-scoped, not a new trust decision.**
  The UI may choose local Keycloak or a Red Hat SSO production/stage
  confidential-client agent token. That choice only determines which
  configured credential mints Token A; MAS and OLS independently validate the
  resulting JWT's issuer, audience, signature, and principal. Red Hat SSO
  user-token login is a documented TODO, not a partially trusted shortcut.
- **On `a2a-split`, Praxis is the A2A caller allow-list.** OLS reuses its
  configured OIDC module to validate the JWT's signature, issuer and audience;
  it no longer checks a separate `A2A_INBOUND_AZP` allow-list. The same OIDC
  module also applies to the stock OLS REST API. Keep the Praxis policy and
  the OLS ingress NetworkPolicy intact; an OIDC-valid token alone is not
  proof that the caller was admitted by Praxis.
- **Inbound MAS A2A is a separate, dormant trust boundary.** It never routes
  through ACME/Praxis/OLS or uses OLS's token exchange. Its Keycloak mode
  validates a dedicated audience and exact subject allow-list, with private
  Keycloak Route CA trust added to (not substituted for) public roots; its
  separate public API must be hardened before activation. Do not invent a
  persisted blueprint ID, caller subjects, or a published image. See the MAO
  section in `README.md` and `charts/all/mao/mas-a2a/README.md`.
- **OpenShift Native OIDC (`openshiftOIDC.enabled`) is required, not
  optional**, for the final MCP tool call to work -- `claimMappings.groups`
  (what makes the Keycloak group RBAC binds to exist as an OpenShift group
  at all) only exists once `Authentication/cluster` is switched to
  `type: OIDC`. It's gated behind a break-glass-first sequence (README
  Phase 3) because flipping it cluster-wide, done wrong, locks out
  console/CLI login entirely -- but it is not skippable if you want the
  agent to actually reach the cluster.
- **`global.olsClusters` is the single shared cluster-routing registry.**
  ACME, Praxis, and `appServerPatch` must all render from this one map
  (`values-standalone.yaml`, top of file) -- never fork a second copy. The
  map key is a derived id, not a free-form name: `lower(host):port` with
  every `.`/`:` replaced by `-` (see `LESSONS_LEARNED.md` for why an
  inexact key breaks both services).
- **`appServerPatch` (in `openshift-lightspeed-config`) is a deliberate,
  temporary bridge**, not a real feature. Neither a custom app-server image
  nor the A2A configuration has a native home in the `OLSConfig` CRD today.
  The operator picks the image via its own `--service-image` flag and
  generates the app-server Deployment and ConfigMap. The bridge waits for
  provisioning and a successful CSV, verifies the operator is stopped at
  zero replicas, then patches the app-server config, image, environment,
  and CSI/CA mounts directly. A periodic re-assert cannot outrun the
  operator's reconciliation. Delete the bridge once the Operator/OLSConfig
  CRD natively supports the custom image and A2A configuration; don't build
  more features on top of it as if it were permanent.
- **Least-privilege RBAC is explicit-namespace by default.**
  `lightspeedRbac.namespaces` (per-namespace Role/RoleBinding) is the
  normal path; `lightspeedRbac.allNamespaces` (cluster-wide
  ClusterRole/ClusterRoleBinding) is an opt-in, off-by-default escape hatch
  for sandbox/demo clusters, documented as a real widening of blast radius,
  not a convenience default.

## Lessons learned

Every live finding, local render failure and pinned-image quirk -- with how it
was hit and why it was missed -- lives in
[LESSONS_LEARNED.md](LESSONS_LEARNED.md). Read the relevant section there
before touching adjacent code; the chart READMEs reference it instead of
repeating the history.
