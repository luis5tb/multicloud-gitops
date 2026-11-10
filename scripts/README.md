# Interactive MAS query

`mas-query.py` calls MAS directly over A2A 1.0 without starting ACME or a local
LLM. It needs Python 3.10+ and uses only the standard library.

```bash
python3 scripts/mas-query.py --mas-url https://<mas-a2a-route>
```

To discover the deployed Route using your current `oc` login:

```bash
python3 scripts/mas-query.py \
  --mas-url "https://$(oc get route -n mao -l app.kubernetes.io/name=mas-a2a -o jsonpath='{.items[0].spec.host}')"
```

The prompts are:

1. **Paste a token** or **Get a token**. Pasting accepts a raw access token or
   `Bearer <token>` and skips the provider questions.
2. When getting a token, choose **Keycloak**, **Red Hat SSO**, or **Red Hat SSO
   stage**. Supply the client ID and client secret if they are not already
   configured in the environment. The Red Hat SSO token endpoints are built in.
   When `--mas-url` uses a standard OpenShift `apps` Route, Keycloak defaults
   to `https://keycloak.apps.<domain>/realms/rca/protocol/openid-connect/token`.
   Keycloak uses `KEYCLOAK_TOKEN_URL` or `KEYCLOAK_ISSUER_URL` first;
   `KEYCLOAK_HOST` and `KEYCLOAK_REALM` adjust the derived default. It prompts
   only when no default can be derived. Keycloak also offers a
   JWT-SVID/assertion file instead of a client secret.
3. **Question to ask**. Include any context the workflow needs (for example
   the cluster API URL for a cluster investigation); MAS owns target-cluster
   selection and validation.

`--mas-url` also accepts the full `/a2a/jsonrpc` or agent-card URL. `MAS_URL`
can supply it; otherwise the script prompts for it after the question. Use the
**MAS A2A Route**, not the Flask MAS API Route.

The output prints the **full bearer token actually used**, live **Thinking**
parts when MAS marks them as such, **Progress** messages and task states, and
the final **Result**. The script displays only what MAS exposes; some workflows
provide progress summaries without reasoning text. Token replacements are
printed too. Token and client-secret prompts do not echo keyboard input, but
the requested token output remains visible in terminal history or captured logs.

## Getting tokens without ACME

The local token path uses `client_credentials`, matching ACME's agent-token
mode. It reads these same environment variables, prompting for missing values:

| Provider | Environment variables |
| --- | --- |
| Keycloak | `KEYCLOAK_TOKEN_URL`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET` |
| Red Hat SSO | `REDHAT_SSO_PRODUCTION_TOKEN_URL`, `REDHAT_SSO_PRODUCTION_CLIENT_ID`, `REDHAT_SSO_PRODUCTION_CLIENT_SECRET` |
| Red Hat SSO stage | `REDHAT_SSO_STAGE_TOKEN_URL`, `REDHAT_SSO_STAGE_CLIENT_ID`, `REDHAT_SSO_STAGE_CLIENT_SECRET` |

The Red Hat SSO production and stage token endpoint URLs default to the
public Red Hat SSO endpoints used by this pattern. Keycloak's token endpoint is
derived from the MAS Route's `apps` domain, using the chart's standard
`keycloak` host and `rca` realm. For a nonstandard installation, set
`KEYCLOAK_ISSUER_URL` (preferred), `KEYCLOAK_TOKEN_URL`, `KEYCLOAK_HOST`, or
`KEYCLOAK_REALM`; an issuer's token URL is
`<issuer>/protocol/openid-connect/token`.
Keycloak defaults to `client_assertion_post`, matching ACME's SPIFFE workload
identity; assertion mode uses no client ID or client secret. It reads
`ZTO_IDENTITY_TOKEN_FILE` (or a prompted path) at each mint, using
`KEYCLOAK_CLIENT_ASSERTION_TYPE` or ACME's
`urn:ietf:params:oauth:client-assertion-type:jwt-spiffe` default. The assertion
must be issued for the configured Keycloak client authentication flow; this
script does not itself fetch JWT-SVIDs from a SPIFFE Workload API socket. To
reuse ACME's in-pod SPIFFE identity directly, use the token command below
rather than exporting or copying its short-lived JWT-SVID.

`KEYCLOAK_SCOPE` is optional. Red Hat SSO defaults to
`openid api.ask_red_hat`, required for the downstream Keycloak exchange;
`REDHAT_SSO_SCOPE` overrides it. Use confidential clients with service accounts
enabled. This path obtains agent tokens; paste an existing token for user-token
flows. A locally configured client must already be trusted by MAS and downstream
services: MAS checks the token's audience and exact subject allow-list. Creating
a new client ID alone does not grant access. See the
[MAS authentication prerequisites](../charts/all/mao/mas-a2a/README.md).

## Reusing the deployed SPIFFE identity

For the existing Keycloak client authenticated by ACME's workload identity,
use its mint helper as an optional token command. This uses the deployed pod's
credentials and SPIFFE socket, while the query and A2A transport run locally:

```bash
python3 scripts/mas-query.py --mas-url https://<mas-a2a-route> \
  --token-command 'oc exec -n acme-agent deploy/acme-agent -- python -m acme_agent.mint_token --provider {provider} --environment "{environment}"'
```

The same command supports all three provider choices using ACME's configured
credentials. `--token-command` (or `MAS_TOKEN_COMMAND`) must write only a raw
token to stdout and exit successfully. `{provider}` becomes `keycloak` or
`redhat`; `{environment}` becomes empty, `production`, or `stage`. Commands
are executed without a shell; use a wrapper executable for pipelines or other
shell logic. A pasted token bypasses the command.

## TLS, long tasks, and approvals

Use `--ca-bundle /path/to/ca.pem` or `A2A_CA_BUNDLE` for private Route CAs. The
bundle extends the system trust store for both MAS and token endpoints. HTTPS
verification stays enabled; loopback HTTP is supported for local development.
With a current `oc` login, export the managed ingress CA used by OpenShift
Routes:

```bash
oc get configmap default-ingress-cert -n openshift-config-managed \
  -o jsonpath='{.data.ca-bundle\.crt}' > /tmp/ingress-ca.pem
```

The script reads MAS's public agent card, streams when advertised, and polls
otherwise. A closed stream triggers task retrieval and reattachment rather than
resubmission of the question. The default overall timeout is 1,200 seconds
(`--timeout`), with a 330-second socket timeout (`--request-timeout`) and a
2-second poll/reconnect delay (`--poll-interval`). Interruption or timeout does
not cancel the remote task; its task/context IDs are printed for recovery.

HTTP 401 renews the token and retries that call once, preserving its message ID.
`AUTH_REQUIRED` renews credentials and resumes the same task, at most twice.
For pasted tokens, renewal prompts for another token for the **same principal**;
minted credentials reuse the selected provider. `INPUT_REQUIRED` prompts for a
reply, such as `approve` or `reject`, and resumes the same task/context with a
new message ID. Approval is always interactive. Task failures return a nonzero
exit code.

Offline tests, inside a temporary environment:

```bash
python3 -m venv /tmp/mas-query-tests
/tmp/mas-query-tests/bin/python -m unittest discover -s tests/scripts -v
```

# Flow monitor

Real-time view of the agentic identity chain on an OpenShift
cluster, following the caller's token at every stage:

```
acme-agent ──Token A──▶ MAO A2A endpoint ──Token A──▶ MAS orchestrator
  ──Token A──▶ praxis (routing to downstream agents)
```

(The MAS orchestrator can also delegate to **Ask Red Hat** directly —
`access.stage.redhat.com` — carrying the same Token A; that branch has no
Praxis hop.)

Adapted from the Docker/Postgres monitor in
[agentic-partners-integration](https://github.com/rh-ai-quickstart/agentic-partners-integration/blob/main/scripts/monitor.sh).
This pattern has no local audit database; correlation uses greppable log lines:

- ACME emits `acme_audit request_id=…` (Token A mint + dispatch)
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
| 1. `acme-agent` | **Token A minted** — Keycloak `client_credentials` + SPIFFE JWT-SVID (`azp=acme-agent`, `aud=mas-a2a`, `groups=acme-agent-rca`) **or** a Red Hat SSO prod/stage agent token (`aud=api.ask_red_hat`) | ACME (mint), sent as `Authorization: Bearer` to the MAO A2A Route | `TOKEN A MINT` cards, then `ACME DISPATCH` |
| 2. MAO A2A endpoint (`mao/mas-a2a`) | **Token A validated, never exchanged** — multi-issuer OIDC (Keycloak `mas-a2a` / Red Hat SSO `api.ask_red_hat`), then `sub` checked against `a2a.allowedPrincipals`; kept in `RunContext.credentials` | MAS validates only | `MAS A2A INBOUND` / `MAS SESSION START` / `MAS A2A AUTH DENY` |
| 3. MAS orchestrator (`mao/mas-worker`, via Temporal) | **Token A passed through** — `a2a_agent_node` with empty `bearer_token` reads `RunContext.auth_token`; the orchestrator LLM decides which specialist agents to call | MAS forwards verbatim | `TOKEN A PASSTHROUGH` / `ORCHESTRATOR PLAN` / `MAS OUTBOUND A2A` / `MAS DELEGATED` |
| 4. `praxis-proxy` | **Token A validated + routed** — standalone runs policy enforcement (`policy.enabled=true`): the policy filter validates the caller JWT and enforces the `azp` allow-list (`acme-agent` + Red Hat SSO client), then routes to the configured downstream backend, fail-closed | Praxis validates + routes | `PRAXIS ROUTE` / `PRAXIS NO ROUTE` / `PRAXIS REJECT` |

Tokens themselves are never printed (by design of `acme_audit` and of
the MAS matchers — the monitor only ever names the token stage; ACME audit
lines carry short SHA-256 fingerprints `fp:…` that identify a token without
being usable as one). One further
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

Send a chat message through the ACME UI. With the Phase-3 (MAS) routing you
should see, in order:

1. `TOKEN A MINT` (Keycloak or Red Hat SSO) and `ACME DISPATCH` from acme-agent
2. `MAS SESSION START` (auth=True) and eventually `MAS A2A INBOUND` (200) as
   the endpoint validates Token A and hands it to the orchestrator
3. `ORCHESTRATOR PLAN`, `TOKEN A PASSTHROUGH`, `MAS OUTBOUND A2A`,
   `MAS DELEGATED` as the worker plans and dispatches
4. `PRAXIS ROUTE` (or `PRAXIS NO ROUTE` / `PRAXIS REJECT`) when the
   delegated call goes through Praxis
5. An `ACME TOKEN RENEWAL` if a downstream refused Token A and ACME
   re-minted and resent it

Note the MAS requests are long-running: one `POST /a2a/jsonrpc` was
observed taking 122 seconds, and the whole chain can run for minutes — the
`MAS A2A INBOUND` card for a request only appears when it *completes*.

## Modes

| Command | What it tails |
| --- | --- |
| `bash scripts/monitor.sh` | All tailable hops (ACME, MAS A2A, worker, Praxis) |
| `bash scripts/monitor.sh --join` | Same as default |
| `bash scripts/monitor.sh --acme` | ACME only |
| `bash scripts/monitor.sh --mas` | MAO A2A endpoint only |
| `bash scripts/monitor.sh --worker` | MAS orchestrator (Temporal worker) only |
| `bash scripts/monitor.sh --praxis` | Praxis only |
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
| OCP API | `openshift-kube-apiserver` | `app=openshift-kube-apiserver` | `NS_API`, `SEL_API` |

All selectors were verified live on bm-cluster (2026-10-23), including the
kube-apiserver static-pod label. Also: `MONITOR_SINCE` (same as `--since`),
`REQUEST_ID` (filter).

Verify labels on a live cluster before debugging empty streams:

```bash
oc get pods -n acme-agent,praxis-proxy,mao --show-labels
```

If a selector matches no pods, the script prints a warning and skips that hop
(`mas-a2a` renders no pods at all until `a2a.enabled: true`).

## What you will see

| Card | Meaning |
| --- | --- |
| **TOKEN A MINT** | ACME obtained (or failed to obtain) Token A via Keycloak `client_credentials` (`acme_audit` … `action="mint Keycloak Token A"`), with the new token's fingerprint (`token=fp:…`) |
| **TOKEN A MINT (Red Hat SSO)** | Same, but minted by Red Hat SSO prod/stage as a confidential-client agent token (`action="mint Red Hat SSO … agent Token A"`) |
| **ACME DISPATCH** | ACME stamped `X-Request-Id` and sent Token A toward the remote agent (`acme_audit` … `outcome=sent token=fp:… source=… exp=…`) — `source=pasted_override` (yellow) marks a UI-pasted token, `source=minted` a real Token A; `expires` shows the token's `exp` (decoded unverified, display-only) and flags it red when already in the past |
| **ACME TOKEN RENEWAL** | A downstream refused the current token (HTTP 401 / `TASK_STATE_AUTH_REQUIRED`); ACME minted a fresh Token A and resent (`action="renew Token A"`, `renewed_from=fp:…` → `token=fp:…`, `override=retired` when a pasted token was the one refused) |
| **MAS SESSION START** | The endpoint submitted an orchestrator session with `auth=True` — Token A attached to the run context |
| **MAS TOKEN REJECTED** | No configured issuer validated the bearer (`Token validation failed for all … issuers`) — a token problem (sig/iss/exp/aud), explicitly not the principal allow-list (that is 403); expect a 401 and, from ACME, an **ACME TOKEN RENEWAL**. When the preceding dispatch carried the pasted override, the card says so and points at the dispatch's `expires` line |
| mas issuer probe miss (dim) | `Failed to resolve signing key` — **benign**: the multi-issuer validator probing the next issuer's JWKS; the same ERROR appears on fully successful requests (confirmed live 2026-10-29) |
| **MAS A2A INBOUND** | The endpoint completed a JSON-RPC call carrying Token A (method/path/status/duration; 401=bad token, 403=principal not allow-listed) |
| **MAS A2A AUTH DENY** | Validation failure logged outside the middleware line (best-effort fallback) |
| **ORCHESTRATOR PLAN** | `workplan.plan_and_delegate` — the LLM routed the request to its specialist agents; carded once per planning call (the `tool_args` line — MAS also logs the bare tool name and phase-timing tables, which are skipped) |
| **TOKEN A PASSTHROUGH** | `a2a_agent_node` with an empty `bearer_token` using `RunContext.auth_token` — the literal Token A passthrough (DEBUG-level) |
| **MAS OUTBOUND A2A** | `A2A stream_message to agent=… auth=True` — the outbound call carrying Token A |
| **MAS DELEGATED** | The work item was dispatched to the remote agent |
| **MAS DELEGATION LOST (404)** | An outbound A2A call came back 404 — usually a Praxis routing/policy failure |
| **MAS OUTBOUND TLS FAILURE** | Agent-card fetch / A2A call hit an untrusted Route CA — check `identity.keycloakCaTrust.enabled` |
| **ORCHESTRATOR ERROR** | ERROR-level or Traceback lines from the worker — failures are matched by log *level*, not keyword, because INFO lines legitimately contain `'error': None` in context dicts (e.g. `final_answer` on a successful flow) |
| **PLANNING LLM GRANT (static)** | The orchestrator's own Vertex ADC token fetch — a separate static credential, deliberately *not* Token A |
| **AUTH FAILURE** | Inbound JWT reject, exchange failure, SPIFFE error, etc. |
| **PRAXIS ROUTE** | Router matched the request to a downstream backend; Token A forwarded unchanged |
| **PRAXIS NO ROUTE** | No backend matched the request — Praxis fail-closes with no default backend |
| **PRAXIS REJECT** | A filter rejected the request with 4xx/5xx (the benign `static_response` 200 that serves the agent card is not shown) |
| **API TOKEN REJECT** (`--api`) | The API server's own OIDC authenticator rejected a bearer (audience/issuer/signature) |
| **API AUTHZ DENY** (`--api`) | A `Forbidden` line from the API server |

### The final hop (OCP API / RBAC) is indirect by design

RBAC decisions are not in any pod log this script can normally read. Confirm
a deny yourself:

```bash
oc auth can-i get pods --as-group=keycloak:acme-agent-rca -n <namespace>
oc get authentication.config.openshift.io cluster \
  -o jsonpath='{.spec.oidcProviders[0].issuer.audiences}'
```

`--api` additionally tails `openshift-kube-apiserver` for token-reject and
`Forbidden` lines (needs cluster-admin, so it is not part of the default
join).

## Log patterns

Patterns for ACME (`acme_audit`) come from this repo's own source. The MAS
(private `uie-mas-hosted`) and Praxis patterns were confirmed against captured
live logs on bm-cluster (2026-10-23 and 2026-10-26, mas image `a80e09316564`,
worker at `logLevel: DEBUG`):

- MAO A2A endpoint: `A2A HTTP request completed context={'request_id': …,
  'method': 'POST', 'path': '/a2a/jsonrpc', 'status_code': 200,
  'duration_ms': …}` and `Submitting session=… auth=True …`
- worker: `tool.execute … workplan.plan_and_delegate`, `A2A agent node
  […]: using user auth token`, `A2A stream_message to agent=… auth=True`,
  `a2a.delegated`
- Praxis (ANSI-stripped): `route matched path=/a2a cluster=…`,
  `no route matched path=/a2a`, `request rejected by filter filter="router"
  status=404`

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
- Praxis's `static_response` "request rejected … status=200" — that is the
  public agent card being served, not a rejection.

If a MAS image bump changes the vocabulary (new/renamed loggers, changed
`context=` keys), capture and replay to refine:

```bash
oc logs -n mao -l app.kubernetes.io/name=mas-a2a  --since 15m > /tmp/mas-a2a.log
oc logs -n mao -l app.kubernetes.io/name=mas-worker --since 15m > /tmp/mas-worker.log
oc logs -n praxis-proxy -l app.kubernetes.io/name=praxis-proxy --since 15m > /tmp/praxis.log
```

Then tighten the `src == "mas"` / `src == "worker"` /
`src == "praxis"` blocks in `scripts/monitor.sh`'s
`parse_line` against the real lines, and
update this section — per the repo convention, a confirmed pattern replaces
the guess immediately.

## Correlating hops

ACME stamps `X-Request-Id` on its outbound POST and logs it on the
`acme_audit` line. With MAS in the middle the join is broken — **observed
live** on the same request (2026-10-23, 14:11): the MAO endpoint's inbound
middleware logged its own dashed-UUID `request_id` (`db37d282-…`). Each MAS
stage mints its own id and does not propagate ACME's. Practical
consequences:

- `REQUEST_ID=<id>` isolates one hop's lines (mostly useful for `--acme`).
- End-to-end tracing across the MAS stages is by **time window**: every card
  carries a wall-clock timestamp, and the chain is slow enough (tens of
  seconds to minutes per hop) that the ordering is unambiguous. Card stamps
  are taken from the **pod's own log line** (converted to local time) so
  cross-hop order is truthful; a leading `~` marks cards where the line had
  no timestamp and only the monitor's receipt time is known — those can
  interleave with same-second cards from other hops (each hop is a separate
  `oc logs -f` pipeline with independent delivery latency).

```bash
oc logs -n acme-agent -l app.kubernetes.io/name=acme-agent --since=30m | grep acme_audit
oc logs -n mao -l app.kubernetes.io/name=mas-worker --since=30m | grep -aE "using user auth token|stream_message|delegated"
```

- Keycloak's Admin Console (Realm Events, filtered by client `acme-agent`)
  records every grant regardless of header propagation — see
  `agents/AUTHENTICATION.md`.

## Lab note: no human login in these logs

The ACME UI Route is unauthenticated by design in this pattern. The monitor
shows **workload identity** (SPIFFE → Keycloak Token A → MAS passthrough),
not an end-user OIDC login. Anyone who can reach ACME acts as the
`acme-agent` client (or the Red Hat SSO confidential client).

## Approval and token renewal scenarios

`a2a-renewal-scenarios.py` plays ACME's role against MAS's A2A endpoint and
checks each behavior end to end. It mints Token A inside the ACME pod
(`python -m acme_agent.mint_token`), so tasks belong to ACME's real principal.

| Scenario | What happens | Needs |
| --- | --- | --- |
| `approval` | MAS pauses in `TASK_STATE_INPUT_REQUIRED` at the approval gate; the script replies (`--decision approve` or `reject`) and waits for completion | HITL blueprint |
| `expired-at-door` | An expired token gets HTTP 401 before any task exists | `--expired-token`, or waits one token lifetime |
| `expiry-in-mas` | Token A runs out while the approval waits; after approving, MAS pauses in `TASK_STATE_AUTH_REQUIRED` before the next step and completes after a fresh token | HITL blueprint, short token lifespan |

Prerequisites:

1. Build the blueprint with the approval gate and point `mao-a2a`'s
   `a2a.blueprintId` at it: `./setup-mas-blueprint.sh multi-cluster-hitl`
   (with `HITL_OC_TOKEN`, or `HITL_CREATE_SA=true`).
2. For `expiry-in-mas`, shorten acme-agent's token lifespan, e.g. 120 seconds
   (`keycloak.acmeAccessTokenLifespanSeconds` on a fresh realm, or Clients >
   acme-agent > Advanced > Access Token Lifespan on an existing one).
3. Use a prompt that makes MAS run a long-running specialist workflow.

```bash
./scripts/a2a-renewal-scenarios.py \
  --mas-url "https://$(oc get route -n mao -l app.kubernetes.io/name=mas-a2a -o jsonpath='{.items[0].spec.host}')" \
  --prompt "On https://api.${CLUSTER_DOMAIN}:6443 list failing pods in openshift-monitoring" \
  --scenario all
```

## Related docs

- [agents/AUTHENTICATION.md](../agents/AUTHENTICATION.md) — full identity chain (orchestrated/passthrough flow), claim shapes, Troubleshooting (including Keycloak Admin Events for grants)
- [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) — why the chain is shaped this way; hard-won lessons
- [charts/all/mao/mas-a2a/README.md](../charts/all/mao/mas-a2a/README.md) — MAS A2A trust boundary, principal allow-list, activation prerequisites
- [charts/all/praxis-proxy/README.md](../charts/all/praxis-proxy/README.md) — Praxis `logLevel` / JWT enforcement quirks
