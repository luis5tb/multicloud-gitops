# Flow monitor

Real-time view of the agentic investigation identity chain on an OpenShift
cluster, following the caller's token at every stage:

```
acme-agent ──Token A──▶ MAO A2A endpoint ──Token A──▶ MAS orchestrator
  ──Token A──▶ praxis ──Token A──▶ OLS A2A ──RFC 8693 exchange: Token B──▶
  openshift mcp ──Token B──▶ OCP API server (RBAC)
```

(The MAS orchestrator can also delegate to **Ask Red Hat** directly —
`access.stage.redhat.com` — carrying the same Token A; that branch has no
Praxis/OLS/MCP hops.)

Adapted from the Docker/Postgres monitor in
[agentic-partners-integration](https://github.com/rh-ai-quickstart/agentic-partners-integration/blob/main/scripts/monitor.sh).
This pattern has no local audit database; correlation uses greppable log lines:

- ACME emits `acme_audit request_id=…` (Token A mint + dispatch)
- OLS emits `a2a_audit request_id=…` (Token A validation + Token B exchange)
- the MAO A2A endpoint logs structured `context={…}` dicts
  (`inbound.a2a.middleware`, `mas.session.service`)
- the MAS worker logs the delegation lifecycle (`tool.execute`,
  `a2a_agent_node_factory`, `a2a_client.provider`, `a2a.delegated`)
- Praxis (Rust `tracing`) logs router decisions (`route matched` /
  `no route matched` / `request rejected by filter`) with embedded ANSI
  colour codes, which the script strips before matching

The MAS and Praxis patterns were **confirmed against live logs on
bm-cluster (2026-10-23, mas image `a80e09316564`)** by replaying captured
lines through `parse_line`; see [Log patterns](#log-patterns) for the
routine-noise exclusions and how to refine after a MAS image bump.

## Token map (what the monitor is actually tracking)

| Stage | Token | Who touches it | What to watch |
| --- | --- | --- | --- |
| 1. `acme-agent` | **Token A minted** — Keycloak `client_credentials` + SPIFFE JWT-SVID (`azp=acme-agent`, `aud=[mas-a2a, lightspeed-a2a]`, `groups=acme-agent-rca`) **or** a Red Hat SSO prod/stage agent token (`aud=api.ask_red_hat`) | ACME (mint), sent as `Authorization: Bearer` to the MAO A2A Route | `TOKEN A MINT` cards, then `ACME DISPATCH` |
| 2. MAO A2A endpoint (`mao/mas-a2a`) | **Token A validated, never exchanged** — multi-issuer OIDC (Keycloak `mas-a2a` / Red Hat SSO `api.ask_red_hat`), then `sub` checked against `a2a.allowedPrincipals`; kept in `RunContext.credentials` | MAS validates only | `MAS A2A INBOUND` / `MAS SESSION START` / `MAS A2A AUTH DENY` |
| 3. MAS orchestrator (`mao/mas-worker`, via Temporal) | **Token A passed through** — `a2a_agent_node` with empty `bearer_token` reads `RunContext.auth_token`; the orchestrator LLM decides Ask Red Hat vs OLS vs both | MAS forwards verbatim (plus work-item `headers` like `X-OLS-Cluster`) | `TOKEN A PASSTHROUGH` / `ORCHESTRATOR PLAN` / `MAS OUTBOUND A2A` / `MAS DELEGATED` / `MISSPELLED ROUTING HEADER` |
| 4. `praxis-proxy` | **Token A routed, unchanged** — standalone runs router-only (`policy.enabled=false`), matching `X-OLS-Cluster` to an OLS backend, fail-closed | Praxis routes only (OLS is the validating boundary) | `PRAXIS ROUTE` / `PRAXIS NO ROUTE` / `PRAXIS REJECT` |
| 5. OLS A2A (`a2a-lightspeed` app-server) | **Token B minted here** — OLS validates Token A (multi-issuer OIDC + cluster-header binding), then RFC 8693 exchange as `lightspeed-mcp`: `azp=lightspeed-mcp`, `aud=openshift-mcp`, **`sub` unchanged** | OLS validates + exchanges | `OLS CALLER VALIDATED` → `OLS TOKEN EXCHANGE` (the Keycloak grant itself) → `OLS → MCP SESSION` / `OLS TOOL LOOP`; the `a2a_audit` card summarizes with the explicit `EXCHANGE` line |
| 6. OpenShift MCP (`a2a-lightspeed`) | **Token B forwarded** — `require_oauth=true` + passthrough; MCP makes no authz decision of its own | MCP requires presence only | `MCP SESSION` (the Go server accepting a Token B session) / `MCP/API DENY` (a 401/403 here is really the next hop) |
| 7. OCP API server | **Token B consumed** — OIDC validation (cached JWKS), `claimMappings` maps `groups=acme-agent-rca` → `Group keycloak:acme-agent-rca`, then RBAC decides | API server is the final authority | indirect: `MCP/API DENY` cards, `--api` mode, `oc auth can-i` |

Token A never reaches MCP or the API server; only Token B does. Tokens
themselves are never printed (by design of `acme_audit` / `a2a_audit` and of
the MAS matchers — the monitor only ever names the token stage). One further
credential appears on the worker cards and is deliberately labelled as
**not** Token A: the orchestrator's planning LLM uses its own static Vertex
ADC grant (`PLANNING LLM GRANT`), the separate credential system described
in `agents/AUTHENTICATION.md`.

## Prerequisites

- `oc` on `PATH`, logged into the target cluster (`oc whoami`)
- GNU `grep` (`grep -oP`; Fedora/RHEL default)
- Pattern apps deployed (namespaces below from `variants/standalone/values-standalone.yaml`)
- For the MAS hops: `mao-a2a` activated (`a2a.enabled: true`) and `mao-worker`
  running. If `mas-a2a` is dormant the script warns and skips that hop.
- For the `TOKEN A PASSTHROUGH` / `MAS OUTBOUND A2A` cards: mas-worker at
  `logLevel: DEBUG` (the standalone variant sets this; those lines are
  DEBUG-level and disappear at INFO).
- For `--api`: cluster-admin (reading `openshift-kube-apiserver` logs)

Optional for richer Praxis cards: raise `logLevel` on the praxis-proxy chart
(wired to `RUST_LOG`). The standalone variant already sets a scoped demo
value in `variants/standalone/values-standalone.yaml`:

```text
info,praxis_filter=debug,praxis_policy_plugin_identity_jwt=debug
```

Override there (or clear it) for quieter environments. Router decisions
(`PRAXIS ROUTE` / `PRAXIS NO ROUTE`) need `praxis_filter=debug`, which that
value already includes.

## Quick start

From the repo root:

```bash
bash scripts/monitor.sh
```

Send a chat message through the ACME UI that includes a target cluster API URL
from `global.olsClusters`. With the Phase-3 (MAS) routing you should see, in
order:

1. `TOKEN A MINT` (Keycloak or Red Hat SSO) and `ACME DISPATCH` from acme-agent
2. `MAS SESSION START` (auth=True) and eventually `MAS A2A INBOUND` (200) as
   the endpoint validates Token A and hands it to the orchestrator
3. `ORCHESTRATOR PLAN`, `TOKEN A PASSTHROUGH`, `MAS OUTBOUND A2A`,
   `MAS DELEGATED` as the worker plans and dispatches
4. `PRAXIS ROUTE` (or `PRAXIS NO ROUTE` + a red `MISSPELLED ROUTING HEADER`
   from the worker if the LLM emitted `X_OLS_Cluster`)
5. `OLS CALLER VALIDATED` (Token A validated, `User ID:` resolved), then the
   explicit **`OLS TOKEN EXCHANGE`** card — the Keycloak token-endpoint POST
   that mints Token B — followed by `OLS → MCP SESSION` and `OLS TOOL LOOP`
   cards as OLS's tools run against the cluster with Token B
6. `MCP SESSION` as the MCP server accepts the Token B session, and the
   closing `OLS A2A` audit card (`on_behalf_of` = Token A's `azp` —
   `acme-agent` or the Red Hat SSO client — with the `EXCHANGE` line)
7. `MCP/API DENY` only if the final RBAC check refused Token B

Note the MAS/OLS requests are long-running: one `POST /a2a/jsonrpc` was
observed taking 122 seconds, and the whole chain can run for minutes — the
`MAS A2A INBOUND` card for a request only appears when it *completes*.

## Modes

| Command | What it tails |
| --- | --- |
| `bash scripts/monitor.sh` | All tailable hops (ACME, MAS A2A, worker, OLS, Praxis, MCP) |
| `bash scripts/monitor.sh --join` | Same as default |
| `bash scripts/monitor.sh --acme` | ACME only |
| `bash scripts/monitor.sh --mas` | MAO A2A endpoint only |
| `bash scripts/monitor.sh --worker` | MAS orchestrator (Temporal worker) only |
| `bash scripts/monitor.sh --ols` | OLS app-server only |
| `bash scripts/monitor.sh --praxis` | Praxis only |
| `bash scripts/monitor.sh --mcp` | OpenShift MCP only |
| `bash scripts/monitor.sh --api` | OCP API server token rejects (cluster-admin; not in the default join) |
| `bash scripts/monitor.sh --since 10m` | Start from logs of the last 10m (`oc --since`) |

Filter one request after you copy an id from an ACME card:

```bash
REQUEST_ID=<request_id> bash scripts/monitor.sh
```

## Namespaces and selectors

| Hop | Namespace | Label selector | Default override env |
| --- | --- | --- | --- |
| ACME | `acme-agent` | `app.kubernetes.io/name=acme-agent` | `NS_ACME`, `SEL_ACME` |
| MAO A2A endpoint | `mao` | `app.kubernetes.io/name=mas-a2a` | `NS_MAS`, `SEL_MAS` |
| MAS orchestrator | `mao` | `app.kubernetes.io/name=mas-worker` | `NS_WORKER`, `SEL_WORKER` |
| Praxis | `praxis-proxy` | `app.kubernetes.io/name=praxis-proxy` | `NS_PRAXIS`, `SEL_PRAXIS` |
| OLS | `a2a-lightspeed` | `app.kubernetes.io/component=application-server` | `NS_OLS`, `SEL_OLS` |
| MCP | `a2a-lightspeed` | `app=openshift-mcp-server` | `NS_OLS`, `SEL_MCP` |
| OCP API | `openshift-kube-apiserver` | `app=openshift-kube-apiserver` | `NS_API`, `SEL_API` |

All selectors were verified live on bm-cluster (2026-10-23), including the
kube-apiserver static-pod label. Also: `MONITOR_SINCE` (same as `--since`),
`REQUEST_ID` (filter).

Verify labels on a live cluster before debugging empty streams:

```bash
oc get pods -n acme-agent,praxis-proxy,a2a-lightspeed,mao --show-labels
```

If a selector matches no pods, the script prints a warning and skips that hop
(`mas-a2a` renders no pods at all until `a2a.enabled: true`).

## What you will see

| Card | Meaning |
| --- | --- |
| **TOKEN A MINT** | ACME obtained (or failed to obtain) Token A via Keycloak `client_credentials` (`acme_audit` … `action="mint Keycloak Token A"`) |
| **TOKEN A MINT (Red Hat SSO)** | Same, but minted by Red Hat SSO prod/stage as a confidential-client agent token (`action="mint Red Hat SSO … agent Token A"`) |
| **CLUSTER ALLOW-LIST REJECT** | `ClusterRoutingMiddleware` denied a missing/unknown cluster URL before ADK or Keycloak ran (`outcome=denied`) |
| **ACME DISPATCH** | ACME stamped `X-Request-Id` and sent Token A toward the remote agent (`acme_audit` … `outcome=sent`) |
| **MAS SESSION START** | The endpoint submitted an orchestrator session with `auth=True` — Token A attached to the run context |
| **MAS A2A INBOUND** | The endpoint completed a JSON-RPC call carrying Token A (method/path/status/duration; 401=bad token, 403=principal not allow-listed) |
| **MAS A2A AUTH DENY** | Validation failure logged outside the middleware line (best-effort fallback) |
| **ORCHESTRATOR PLAN** | `workplan.plan_and_delegate` — the LLM routed the request to Ask Red Hat / OLS, with the `X-OLS-Cluster` it chose (when spelled correctly); carded once per planning call (the `tool_args` line — MAS also logs the bare tool name and phase-timing tables, which are skipped) |
| **TOKEN A PASSTHROUGH** | `a2a_agent_node` with an empty `bearer_token` using `RunContext.auth_token` — the literal Token A passthrough (DEBUG-level) |
| **MAS OUTBOUND A2A** | `A2A stream_message to agent=… auth=True` — the outbound call carrying Token A |
| **MAS DELEGATED** | The work item was dispatched to the remote agent |
| **MISSPELLED ROUTING HEADER** | The planning LLM emitted `X_OLS_Cluster` (underscores) — Praxis will fail-closed 404 it ([live lesson](../docs/ARCHITECTURE.md); still recurring, see below) |
| **MAS DELEGATION LOST (404)** | An outbound A2A call came back 404 — usually that misspelled/missing `X-OLS-Cluster` |
| **MAS OUTBOUND TLS FAILURE** | Agent-card fetch / A2A call hit an untrusted Route CA — check `identity.keycloakCaTrust.enabled` |
| **ORCHESTRATOR ERROR** | ERROR-level or Traceback lines from the worker — failures are matched by log *level*, not keyword, because INFO lines legitimately contain `'error': None` in context dicts (e.g. `final_answer` on a successful flow) |
| **PLANNING LLM GRANT (static)** | The orchestrator's own Vertex ADC token fetch — a separate static credential, deliberately *not* Token A |
| **OLS CALLER VALIDATED** | OLS's OIDC module validated Token A and resolved the caller (`ols.py` … `User ID: …`) — this subject becomes the exchange subject |
| **OLS TOKEN EXCHANGE** | The RFC 8693 exchange itself, from OLS's httpx log: the Keycloak token-endpoint POST (`HTTP Request: POST …/protocol/openid-connect/token "HTTP/1.1 200 OK"` — 200 = Token B minted, 4xx = exchange failed). Also fires on the rarer `a2a_audit` … `action="exchange MCP credentials"` line; red cards point at the known exchange failure causes |
| **OLS A2A** | OLS validated Token A, performed the RFC 8693 exchange for Token B, and recorded the query (`a2a_audit`; `on_behalf_of` is the Token A `azp`; the `EXCHANGE` line in the card spells out Token A → Token B) |
| **AUTH/EXCHANGE FAILURE** | Inbound JWT reject, cluster header reject, exchange failure, SPIFFE error, etc. |
| **PRAXIS ROUTE** | Router matched `X-OLS-Cluster` → OLS backend; Token A forwarded unchanged |
| **PRAXIS NO ROUTE** | `X-OLS-Cluster` missing/unknown (or misspelled by the orchestrator LLM) — Praxis fail-closes with no default backend |
| **PRAXIS REJECT** | A filter rejected the request with 4xx/5xx (the benign `static_response` 200 that serves the agent card is not shown) |
| **MCP/API DENY** | 401/403 from MCP logs — really API-server OIDC/RBAC rejecting Token B (MCP is passthrough) |
| **OLS → MCP SESSION** | OLS's MCP client opened a session carrying Token B (`Received session ID: …`) — the tool calls follow on it |
| **OLS TOOL LOOP** | OLS's tool-calling rounds (`Tool loop iteration N/5 outcome=…`) — MCP tools executing with Token B |
| **MCP SESSION** | The operator-managed MCP server accepting the Token B session (`server session connected session_id="…"`; the Go server logs no tool names at default verbosity — the tool detail lives on the OLS side) |
| **API TOKEN REJECT** (`--api`) | The API server's own OIDC authenticator rejected a bearer (audience/issuer/signature) |
| **API AUTHZ DENY** (`--api`) | A `Forbidden` line from the API server |

### The final hop (OCP API / RBAC) is indirect by design

RBAC decisions are not in any pod log this script can normally read. Confirm
a deny yourself, as the MCP/API DENY card suggests:

```bash
oc auth can-i get pods --as-group=keycloak:acme-agent-rca -n <namespace>
oc get authentication.config.openshift.io cluster \
  -o jsonpath='{.spec.oidcProviders[0].issuer.audiences}'   # must include openshift-mcp
```

`--api` additionally tails `openshift-kube-apiserver` for token-reject and
`Forbidden` lines (needs cluster-admin, so it is not part of the default
join).

## Log patterns

Patterns for ACME (`acme_audit`) and OLS (`a2a_audit`) come from this repo's
own source. The MAS (private `uie-mas-hosted`), Praxis, OLS-internal and
MCP-server patterns were confirmed against captured live logs on bm-cluster
(2026-10-23 and 2026-10-26, mas image `a80e09316564`, worker at
`logLevel: DEBUG`):

- MAO A2A endpoint: `A2A HTTP request completed context={'request_id': …,
  'method': 'POST', 'path': '/a2a/jsonrpc', 'status_code': 200,
  'duration_ms': …}` and `Submitting session=… auth=True …`
- worker: `tool.execute … workplan.plan_and_delegate`, `A2A agent node
  […]: using user auth token`, `A2A stream_message to agent=… auth=True`,
  `a2a.delegated`
- OLS internals: `User ID: …` (caller validated), `HTTP Request: POST
  …/protocol/openid-connect/token "HTTP/1.1 200 OK"` (the RFC 8693 Token B
  mint — OLS's httpx client logs every outbound request),
  `Received session ID: …` (MCP session with Token B), `Tool loop
  iteration N/5 outcome=…`; and `a2a_audit … action="exchange MCP
  credentials"` (the exchange as its own audited event — a *successful*
  exchange is normally only visible as the Keycloak token POST above,
  followed by `action="dispatched MCP-grounded query"`)
- MCP server (Go, klog-style): `server session connected
  session_id="…"` — it logs session lifecycle only, **no tool names at
  default verbosity**; the tool-call detail lives on the OLS side
- Praxis (ANSI-stripped): `route matched path=/a2a cluster=ols-…`,
  `no route matched path=/a2a`, `request rejected by filter filter="router"
  status=404`

One deliberate limitation: the `MISSPELLED ROUTING HEADER` trap fires only
on the worker's `tool.execute` line (the LLM's own output). The
`a2a.applying_runtime_headers` line can show the stored underscore name even
on attempts that route successfully (observed live 2026-10-26: a retried
delegation showed `X_OLS_Cluster` there yet Praxis routed it) — the actual
failure is always caught by `PRAXIS NO ROUTE` regardless.

Routine noise that is deliberately **not** carded:

- `Failed to resolve signing key` (ERROR, ~once per request) — the MAS
  multi-issuer validator tries each issuer's JWKS in turn and logs every
  miss, even when a later issuer validates the token successfully.
- `Task … already exists. Ignoring task replacement.` — A2A task polling.
- `/.well-known/agent-card.json` requests on every hop — the public,
  unauthenticated card fetches carry no token (the middleware line for them
  is skipped along with the card-resolver body dumps).
- INFO-level lines whose context dicts merely contain `'error': None` —
  worker failures are matched by log level, not keyword.
- `/readyz` + `/healthz` probes on every hop, giant agent-card dumps,
  `temporalio.worker._activity` payload dumps and other multi-line log
  continuations (whitespace-led lines after the `--prefix` tag).
- OLS-side HTTP noise: the JWKS/discovery GETs that precede validation, the
  raw per-request MCP `POST/GET/DELETE …/mcp` lines (the session card covers
  them), `GET stream disconnected, reconnecting…`, and the MCP server's
  connect/initialize/disconnect lifecycle lines other than
  `server session connected`.
- Praxis's `static_response` "request rejected … status=200" — that is the
  public agent card being served, not a rejection.

If a MAS image bump changes the vocabulary (new/renamed loggers, changed
`context=` keys), capture and replay to refine:

```bash
oc logs -n mao -l app.kubernetes.io/name=mas-a2a  --since 15m > /tmp/mas-a2a.log
oc logs -n mao -l app.kubernetes.io/name=mas-worker --since 15m > /tmp/mas-worker.log
oc logs -n praxis-proxy -l app.kubernetes.io/name=praxis-proxy --since 15m > /tmp/praxis.log
oc logs -n a2a-lightspeed -l app.kubernetes.io/component=application-server -c lightspeed-service-api --since 15m > /tmp/ols.log
oc logs -n a2a-lightspeed -l app=openshift-mcp-server --since 15m > /tmp/mcp.log
```

Then tighten the `src == "mas"` / `src == "worker"` / `src == "ols"` /
`src == "mcp"` / `src == "praxis"` blocks in `scripts/monitor.sh`'s
`parse_line` against the real lines, and
update this section — per the repo convention, a confirmed pattern replaces
the guess immediately.

## Correlating hops

ACME stamps `X-Request-Id` on its outbound POST and logs it on the
`acme_audit` line; OLS echoes the header it receives on its `a2a_audit`
line. In the Phase-1 direct flow (ACME → Praxis → OLS) the id joins the two
hops cleanly.

With MAS in the middle the join is broken — **observed live** on the same
request (2026-10-23, 14:11): the MAO endpoint's inbound middleware logged
its own dashed-UUID `request_id` (`db37d282-…`), while OLS's `a2a_audit`
for that request logged a different, hex id (`47006b0d…`). Each MAS stage
mints its own id and does not propagate ACME's. Practical consequences:

- `REQUEST_ID=<id>` isolates one hop's lines (mostly useful for `--acme`).
- End-to-end tracing across the MAS stages is by **time window**: every card
  carries a wall-clock timestamp, and the chain is slow enough (tens of
  seconds to minutes per hop) that the ordering is unambiguous:

```bash
oc logs -n acme-agent -l app.kubernetes.io/name=acme-agent --since=30m | grep acme_audit
oc logs -n mao -l app.kubernetes.io/name=mas-worker --since=30m | grep -aE "using user auth token|stream_message|delegated|X_OLS_Cluster"
oc logs -n a2a-lightspeed -l app.kubernetes.io/component=application-server -c lightspeed-service-api --since=30m | grep a2a_audit
```

- Keycloak's Admin Console (Realm Events, filtered by client `acme-agent`,
  `lightspeed-mcp`) records every grant/exchange regardless of header
  propagation — see `agents/AUTHENTICATION.md`.

Note the OLS app-server pod runs two containers; the monitor uses
`--all-containers`, and the `a2a_audit` lines come from
`lightspeed-service-api`.

## Lab note: no human login in these logs

The ACME UI Route is unauthenticated by design in this pattern. The monitor shows
**workload identity** (SPIFFE → Keycloak Token A → MAS passthrough → RFC 8693
Token B), not an end-user OIDC login. Anyone who can reach ACME acts as the
`acme-agent` client (or the Red Hat SSO confidential client) for cluster
investigation.

## Related docs

- [agents/AUTHENTICATION.md](../agents/AUTHENTICATION.md) — full identity chain (direct + orchestrated/passthrough flows), claim shapes, Troubleshooting (including Keycloak Admin Events for grants/exchanges)
- [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) — why the chain is shaped this way; hard-won lessons incl. the `X_OLS_Cluster` misspelling
- [charts/all/mao/mas-a2a/README.md](../charts/all/mao/mas-a2a/README.md) — MAS A2A trust boundary, principal allow-list, activation prerequisites
- [charts/all/praxis-proxy/README.md](../charts/all/praxis-proxy/README.md) — Praxis `logLevel` / JWT enforcement quirks
