# One Principal, Four Tokens: ACME to the Spoke Cluster

How an ACME question becomes a lightspeed-hub investigation, running live on the standalone cluster — and how tokens A, H, R and U carry one caller identity across every hop.

**Multicloud GitOps · branch rca-mao-praxis-hub** · as implemented, verified live 2026-11-20
_Multicloud GitOps · Red Hat_

Companion of `lightspeed-hub-architecture-deck.html`. References: `docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md` (design and ownership split), `agents/AUTHENTICATION.md` (per-hop reference), `docs/agentic-architecture-overview.html` and `docs/agentic-e2e-token-flow.html` (diagrams). The original plan (A → H plus A → U, parked Token A) is kept in `PLAN-lightspeed-hub-migration.md` as history.

---

## Slide 1 — Title

Tags: A2A · RFC 8693 Token Exchange · Token A/H/R/U · Praxis hub + spoke gateways · Keycloak + SPIFFE

---

## Slide 2 — We started from a chain that leaned on a *forked OLS bridge*

ACME → MAS → Praxis → OLS (a2a-split fork, now retired) → spokes.

- **Retired** — Task A removed RHOAI and OLS from multicloud-gitops.
- **Token A is still an agent identity** — client-credentials minted by ACME; human SSO callers remain open.

---

## Slide 3 — The hub had *no A2A and no identity* — both are built and live

- **A2A adapter** — Go service in lightspeed-hub (`a2a-adapter/`): A2A task → AgenticRun, status → A2A events, own agent card (protocolVersion 0.3).
- **Spokes see the caller** — every kube read uses Token U for the caller's `sub`, through a per-spoke Praxis gateway.
- **lightspeed-agentic-operator, gateway credential mode** — sandbox kubeconfig points at the spoke gateway with the run token; no spoke SA, no spoke-management kubeconfig.
- **GitOps for all of it** — charts `lightspeed-hub`, `praxis-proxy`, `keycloak-oidc`.

---

## Slide 4 — A chain: *A → H* at the hub gateway, *H → R* in the adapter, *R → U* at the spoke gateway

Request leg: ACME (A, aud mas-a2a) → MAS A2A (validates, seals A) → Praxis hub gateway (A → H as client `mas-a2a`) → hub A2A adapter (creates the run with H; H → R) → hub kube API (H ⇒ agenticrun-creator group; RBAC + admission policy).

Run leg: agentic operator (sandbox pod + kubeconfig → spoke gateway) → sandbox (tokenFile = R) → `praxis-spoke-<id>` (R → U per call as `praxis-spoke-gateway`) → spoke kube API (U ⇒ reader group).

Why a chain and not the planned fan-out: parking A failed live (5-minute A, investigations died); exchanging at the hub gateway means the adapter never sees A; R, unlike A, is useless anywhere but the spoke gateway.

---

## Slide 5 — Four tokens, one *sub* — each audience names the next exchanger

| Token | Minted by (client) | azp | aud | groups | Life |
| --- | --- | --- | --- | --- | --- |
| A — caller | Keycloak for `acme-agent` (SPIFFE) | acme-agent | mas-a2a | acme-agent-rca | 300 s |
| H — run creation | hub gateway as `mas-a2a` | mas-a2a | lightspeed-hub-a2a | lightspeed-hub-a2a:agenticrun-creator | 300 s |
| R — run credential | adapter as `lightspeed-hub-a2a` | lightspeed-hub-a2a | praxis-spoke-gateway | none | 1800 s |
| U — spoke reads | spoke gateway as `praxis-spoke-gateway`, per call | praxis-spoke-gateway | praxis-spoke-<id> | praxis-spoke-<id>:reader | 300 s |

- sub is preserved, azp becomes the exchanger — any azp allow-list after an exchange names the exchanger.
- Only one hop can use each token — A and R get 401 at every kube API.

---

## Slide 6 — Leg 1 (ACME → MAS): A2A IDs are the *durable spine*

| Layer / ID | Meaning |
| --- | --- |
| A2A contextId | Conversation (principal + blueprint) → MAS session reused across turns |
| A2A taskId | One submitted turn |
| A2A messageId | One submission/follow-up; duplicates return the existing record |
| Temporal `a2a-<taskId>` | Parent SessionWorkflow (REJECT_DUPLICATE); a graph node calls the Hub Cluster Investigator via A2A |

- Token A is sealed (Fernet, Vault `mao-run-credentials`).
- The hub task maps to one AgenticRun; MAS streams it for up to 1200 s.

---

## Slide 7 — Leg 2 (MAS → hub): the hub gateway mints *H*; the adapter creates the run as the caller and mints *R*

- A → H at Praxis, `delegate()` as `mas-a2a`; no protected-A handoff needed.
- Run created with H: user `keycloak:<sub>`, group `…:agenticrun-creator`; the adapter's SA cannot create runs.
- H → R in the adapter as `lightspeed-hub-a2a` (1800 s); R stored in `ls-run-token-<uid>`, key `token-a`.
- Requester × target: ValidatingAdmissionPolicy on `spec.targetCluster`.
- Agent card passthrough: Praxis serves the adapter's own card (protocolVersion 0.3).

---

## Slide 8 — Leg 3 (hub → spoke): the spoke gateway mints *U per call* — the sandbox holds only R

agentic-operator (gateway mode) → sandbox (tokenFile = R) → spoke gateway (TLS leg 1, service CA; validates R, azp `lightspeed-hub-a2a`; R → U) → spoke API (TLS leg 2, spoke CA; U ⇒ reader group).

- Control plane is not the command path.
- Stable per-spoke endpoints `praxis-spoke-<id>.praxis-proxy.svc:8443`; NetworkPolicy admits only the sandbox namespace.
- R goes only to the gateway (401 at every kube API).
- Kube API pass-through works on the pinned Praxis (watch, streaming, websocket; exec over SPDY unverified); responses capped at 32 MiB.

---

## Slide 9 — One kubeconfig is the whole *run-leg contract*

```yaml
clusters:
  - name: spoke-via-gateway
    cluster:
      server: https://praxis-spoke-hub.praxis-proxy.svc.cluster.local:8443
      certificate-authority: /var/run/secrets/praxis-gateway/ca.crt
users:
  - name: requester
    user:
      tokenFile: /var/run/secrets/run-identity/token-a   # the run token R
contexts:
  - name: spoke
    context: { cluster: spoke-via-gateway, user: requester }
current-context: spoke
```

- The gateway is the server; no subPath, so a renewed R appears in place.
- The B1 spike passed; the fallback (adapter brokers U) was never needed.

---

## Slide 10 — *B1 won* — and the run token R replaced the parked A

| Option | Status |
| --- | --- |
| A · adapter brokers U | Not needed (spike passed) |
| **B1 · gateway at the spoke API** | **Implemented — U per call; sandbox carries R, not A** |
| B2 · sandbox self-serve | Rejected — blast radius |
| C · operator SA tokens | Upstream default outside gateway mode |

Invariant kept: the spoke-accepted token speaks for the caller (same `sub`); what crosses the request→run gap is R, derived from H.

---

## Slide 11 — R outlives A because *an exchanged token takes the exchanger's lifespan*

| Token | Lifetime | Renewed by |
| --- | --- | --- |
| SVID | Short, rotated in place | Workload API |
| A | 300 s | ACME re-mint: 401 → mint + resend; AUTH_REQUIRED → same-task follow-up |
| H | 300 s | Hub gateway exchanges the fresh A |
| R | 1800 s | Adapter exchanges the fresh H, rewrites the run Secret |
| U | 300 s | Nothing — minted per call |

- MAS mailbox: the adapter's auth-required lands on the same task/context; no new run.
- Open: renewal for steps longer than one R.
- Safety ≠ renewal.

---

## Slide 12 — Done means *fails closed* — proven with real requests

- End to end: an ACME request ran to `Analyzed=True Succeeded`.
- H ⇒ agenticrun-creator, U ⇒ reader; A and R get 401 at every kube API.
- Keycloak rules: exchange audience must be a client; exchanger must be in the subject's aud; azp becomes the exchanger; `fullScopeAllowed: false` or audiences leak into A.
- Praxis quirks: Basic auth without URL-encoding (alphanumeric secrets); `role: user`; IPv4-only DNS sidecar; DNS pod port 5353 in NetworkPolicy.
- Fail-closed paths seen live: wrong azp ⇒ 403; missing finalizer UPDATE clause ⇒ admission denied; expired A ⇒ investigation stopped.

---

## Slide 13 — Phases 0–2 are *live*; Phase 3 and hardening remain

| Phase | Work | Status |
| --- | --- | --- |
| 0 · entry gate | Operator AgenticRun / sandbox contract | Done |
| Task A | Remove RHOAI + OLS | Done |
| 1 · request leg | Adapter, hub chart, admission policy, operator, hub gateway A → H | Live |
| 2 · run leg | Gateway credential mode, spoke gateways R → U, run token R | Live |
| 3 · registration | Remove the standing spoke kubeconfig (CredentialSource) | Not started |
| Hardening | Renewal past 30 min · per-spoke R · SSO callers | Open |

---

## Slide 14 — The design is *documented and running*

- Diagrams: `docs/agentic-architecture-overview.html`, `docs/agentic-e2e-token-flow.html`.
- Design for upstream: `docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md`.
- Per-hop reference: `agents/AUTHENTICATION.md`.

---

## Slide 15 — Thank You

Multicloud GitOps · Agentic OpenShift Investigation Pattern · branch rca-mao-praxis-hub · Red Hat
