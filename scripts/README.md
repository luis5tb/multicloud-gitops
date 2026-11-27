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

Real-time view of the agentic identity chain on an OpenShift cluster,
following the caller's identity at every stage and showing **who acts, what
token it wears, and on whose behalf**. The chain (standalone, live
2026-11-24, two spokes registered) is one passthrough followed by three
exchanges, one per hop:

```
acme-agent ──A──▶ MAO A2A ──A──▶ MAS orchestrator ──A──▶ praxis HUB gateway
    ──H──▶ lightspeed-hub adapter ──▶ hub kube API (AgenticRun created as the caller)
    ──R──▶ praxis SPOKE gateway ──U──▶ spoke kube API
```

| Token | Minted/exchanged by (client) | `aud` | Used at |
| --- | --- | --- | --- |
| **A** | Keycloak for `acme-agent` (SPIFFE JWT-SVID) | `mas-a2a` | MAS, hub gateway |
| **H** | hub gateway, as client `mas-a2a` | `lightspeed-hub-a2a` | adapter, hub kube API |
| **R** | adapter, as client `lightspeed-hub-a2a` | `praxis-spoke-gateway` | spoke gateways only |
| **U** | spoke gateway, as client `praxis-spoke-gateway` | `praxis-spoke-<id>` | spoke kube API |

Two spokes are registered on the live standalone cluster (2026-11-24):
`hub` (the hub cluster as its own spoke) and `mario-ipi`. Each has its own
spoke gateway, its own U audience (`praxis-spoke-hub`, `praxis-spoke-mario-ipi`)
and its own reader role group; the monitor names the spoke on every spoke
card and lists both in `--endpoint`-style diagnostics via the Pod name.

All four carry the same `sub`; each token's `aud` names the client that
exchanges or consumes it next. The design is
[docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md](../docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md);
the per-hop reference is [agents/AUTHENTICATION.md](../agents/AUTHENTICATION.md).

## Where each answer comes from

The monitor shows three kinds of fact, and it is deliberate that they do not
all come from the same place:

- **Identities and exchanges** come from **Keycloak's realm events**
  (`--kc`, on by default). Keycloak signed every grant: the event names the
  client that performed it (`clientId`), the principal it was for (`userId`),
  the audience it asked for, and any error. That is the authoritative answer
  to "who did what on behalf of whom", independent of any pod's log level. If
  realm events are disabled the monitor prints that up front instead of
  silently showing nothing.
- **What each hop did with its token** comes from the pod logs (below).
  Tokens are matched **by fingerprint** (`fp:`…) across hops, so a card can
  say "this is still A" or "this is H, which Keycloak minted for you".
- **Rejections and anomalies** come from whichever hop logs them; the monitor
  additionally flags a token presented at a hop that should already have
  exchanged it (`SUSPECT` / `IMPOSSIBLE` cards).

Log sources:

- ACME emits `acme_audit request_id=… actor=… action=… outcome=… token=fp:…`
  for Token A mint, dispatch and renewal.
- MAO A2A logs structured `context={…}` dicts (`inbound.a2a.middleware`,
  `mas.session.service`), including the `AUTH_REQUIRED` renewal pause.
- the MAS worker logs the delegation lifecycle (`tool.execute`,
  `a2a_agent_node_factory`, `a2a_client.provider`, `a2a.delegated`).
- Praxis (Rust `tracing`) logs router/policy decisions for the hub gateway and
  every `praxis-spoke-<id>` gateway; the monitor tells them apart by the pod
  name in `oc logs --prefix`, so one `--praxis` stream covers both.
- the lightspeed-hub adapter (run creation, `ls-run-token-*`, renewals,
  duplicate/unknown-target) and the agentic operator (gateway credential
  mode, sandbox lifecycle, run-credential problems).
- with `--api`, the API server's own bearer rejects and `Forbidden` lines;
  the `audit` username `keycloak:<sub>` is shown there, which is the only
  place the *Kubernetes* identity of H and U becomes visible.

The ACME, MAS and Praxis patterns were **confirmed against live logs on
bm-cluster (2026-10-23/26, mas image `a80e09316564`)**. The Praxis log level
that was confirmed is `praxis_filter=debug`; the delegator crate's level for
the exchange line is *not* among the confirmed ones, so a delegator card
labelled UNCONFIRMED means the variant's `logLevel` needs that crate's module
added — the Keycloak card is unaffected and remains authoritative. The lightspeed-hub
adapter/operator patterns and the Praxis delegator line are marked
**UNCONFIRMED WATCHER** in `scripts/monitor-lib.sh` until a live sample is
replayed; see [Log patterns](#log-patterns) for how to tighten them.

## Files

| File | What it is |
| --- | --- |
| `scripts/monitor.sh` | entry point: follows the hops, polls Keycloak events |
| `scripts/monitor-lib.sh` | all parsing and card rendering; sourced by both the monitor and the tests |
| `tests/scripts/test_monitor.py` | replays captured lines for every hop through the real parser (`python3 -m unittest tests.scripts.test_monitor -v`) |

## Prerequisites

- `oc` on `PATH`, logged into the target cluster (`oc whoami`)
- GNU `grep` (`grep -oP`; Fedora/RHEL default) and `python3` (stdlib only)
- For the Keycloak feed: read access to the `keycloak-admin-user` Secret in
  `keycloak-system` (the keycloak-oidc chart creates it), and realm events
  enabled (`Realm settings → Events`). `KC_URL` / `KC_CA` for a private Route.
- Pattern apps deployed (namespaces below from
  `variants/standalone/values-standalone.yaml`)
- `mas-a2a` activated (`a2a.enabled: true`) and `mao-worker` running; a
  dormant `mas-a2a` is skipped with a warning
- For the `TOKEN A PASSTHROUGH` / `MAS OUTBOUND A2A` cards: mas-worker at
  `logLevel: DEBUG` (the standalone variant sets this)
- Praxis `logLevel` from the variant
  (`info,praxis_filter=debug,praxis_policy_plugin_identity_jwt=debug`) — the
  router/policy cards need it
- For `--api`: cluster-admin (reading `openshift-kube-apiserver` logs)

## Quick start

```bash
bash scripts/monitor.sh              # everything, including Keycloak events
bash scripts/monitor.sh --no-kc      # pod logs only
```

Send a chat message through the ACME UI. Expect, in order:

1. `TOKEN A MINT` / `ACME DISPATCH` (ACME; A, aud `mas-a2a`), then Keycloak's
   `MINT A` event.
2. `MAS A2A INBOUND` → `MAS SESSION START` → `TOKEN A PASSTHROUGH` →
   `MAS OUTBOUND A2A` (A unchanged all the way to the hub gateway).
3. `PRAXIS HUB ROUTE`, then `TOKEN EXCHANGE A ──▶ H` (client `mas-a2a`) —
   the Keycloak event is the authoritative copy; a delegator log copy is
   suppressed once Keycloak confirms it.
4. `ADAPTER AGENTICRUN` (created as the caller with H), `TOKEN EXCHANGE H ──▶ R`,
   `ADAPTER RUN-TOKEN SECRET (stored)` (R in `ls-run-token-<uid>`). The adapter
   logs the Secret's *deletion* but never its creation or the H→R exchange, so
   these two cards come from the runcred watcher (`oc get secrets -w` in the
   hub namespace) and/or the Keycloak event — never from an adapter log line.
5. `OPERATOR: GATEWAY CREDENTIAL MODE` / `OPERATOR SANDBOX` — explicitly no
   exchange. Reconcile-loop churn (`reconciling`, `already in progress,
   waiting`, repeated `handling <phase>`) is silent: it carried no signal and
   buried the real events.
6. `PRAXIS SPOKE ROUTE` and `TOKEN EXCHANGE R ──▶ U` per kube call; with
   `--api`, `API IDENTITY` shows `keycloak:<sub>` and RBAC denials.
7. An `ACME TOKEN RENEWAL` if a token was refused (`HTTP 401`) or MAS paused
   the ongoing task (`task auth-required`, near-expiry A — a resume of the
   same task, not a new request); the adapter's
   `ADAPTER PAUSED: auth-required` is the hub half of the same renewal.

The Keycloak feed needs **realm events enabled** (Realm settings → Events →
Save events; the realm import sets `keycloak.realmEvents.enabled`). With
events off, `--kc` is structurally empty — the monitor's probe line says so
at startup.

MAS requests are long-running: one `POST /a2a/jsonrpc` was observed taking
122 s, and an investigation can run for minutes — `MAS A2A INBOUND` appears
only when the request completes.

## Modes

| Command | What it tails |
| --- | --- |
| `bash scripts/monitor.sh` / `--join` | ACME, MAS A2A, worker, Praxis (hub+spokes), adapter, operator, Keycloak events |
| `--acme` / `--mas` / `--worker` | that hop only |
| `--praxis` | hub gateway + every spoke gateway |
| `--hub` | lightspeed-hub adapter + agentic operator + the `ls-run-token-*` Secret watch |
| `--kc` | Keycloak realm events only (authoritative exchanges) |
| `--api` | OCP API server rejects (cluster-admin; not in the default join) |
| `--no-kc` | skip the Keycloak feed |
| `--since 10m` | start from logs of the last 10m (`oc --since`; default `1s` = attach from now — `0s` would replay ALL retained logs) |
| `--endpoint` | print namespaces/selectors and exit (works without a cluster) |

Filter one request after copying an id from a card:

```bash
REQUEST_ID=<request_id> bash scripts/monitor.sh
```

`REQUEST_ID` only matches hops that log that id. ACME's `X-Request-Id` stops
at MAS — later hops mint their own ids — so end-to-end correlation is by
fingerprint, `sub`, and time; `--kc` records the exchanges independently.

## Namespaces and selectors

| Hop | Namespace | Label selector | Override env |
| --- | --- | --- | --- |
| ACME | `acme-agent` | `app.kubernetes.io/name=acme-agent` | `NS_ACME`, `SEL_ACME` |
| MAO A2A endpoint | `mao` | `app.kubernetes.io/name=mas-a2a` | `NS_MAS`, `SEL_MAS` |
| MAS orchestrator | `mao` | `app.kubernetes.io/name=mas-worker` | `NS_WORKER`, `SEL_WORKER` |
| Praxis hub + spokes | `praxis-proxy` | `app.kubernetes.io/name=praxis-proxy` | `NS_PRAXIS`, `SEL_PRAXIS` |
| lightspeed-hub adapter | `lightspeed-hub` | `app.kubernetes.io/name=lightspeed-hub` | `NS_HUB`, `SEL_HUB` |
| agentic operator | `lightspeed-hub` | `app.kubernetes.io/name=lightspeed-hub-agentic-operator` | `NS_OPERATOR`, `SEL_OPERATOR` |
| Keycloak events | `keycloak-system` | Secret `keycloak-admin-user` → Admin REST `admin-events` (realm `rca`) | `KC_REALM`, `KC_ADMIN_SECRET`, `KC_URL`, `KC_CA` |
| OCP API | `openshift-kube-apiserver` | `app=openshift-kube-apiserver` | `NS_API`, `SEL_API` |

Other knobs: `MONITOR_SINCE` (same as `--since`), `MONITOR_KC_POLL` (seconds
between event polls, default 3), `MONITOR_CHAIN_MODE=1` (one ongoing
"request" label instead of a new one per request), `MONITOR_STATE_FILE` (pin
the cross-hop state file, e.g. to inspect it).

Verify labels before debugging empty streams:

```bash
bash scripts/monitor.sh --endpoint
oc get pods -n acme-agent,praxis-proxy,mao,lightspeed-hub --show-labels
```

## What you will see

| Card | Identity | Exchange | Meaning |
| --- | --- | --- | --- |
| **TOKEN A MINT** / **(Red Hat SSO)** | ACME as `acme-agent` | none (A is the root) | A minted; `azp`, `aud`, fingerprint shown |
| **ACME DISPATCH** | on behalf of the caller | none | A sent to MAS |
| **ACME TOKEN RENEWAL** | ACME as itself | refused A → fresh A | a downstream refused A; same task resent |
| **MAS A2A INBOUND / SESSION START** | caller wearing A | none | validated + sealed into the run context |
| **MAS TOKEN REJECTED / A2A AUTH DENY** | — | none | token invalid (401) or principal not allow-listed (403) |
| **MAS AWAITING FRESH CREDENTIAL** | — | none | MAS paused in `AUTH_REQUIRED`; the caller must renew |
| **TOKEN A PASSTHROUGH / MAS OUTBOUND A2A** | MAS for the caller | none | A forwarded verbatim |
| **ORCHESTRATOR PLAN** | the planner's own static grant | none | LLM route choice — *not* the caller's token |
| **PRAXIS HUB ROUTE / PRAXIS NO ROUTE / PRAXIS REJECT** | MAS wearing A | A → H next | policy + target routing at the hub gateway |
| **TOKEN EXCHANGE A ──▶ H** | client `mas-a2a`, subject unchanged | yes | authoritative copy from Keycloak (`--kc`) |
| **ADAPTER AGENTICRUN** | created as the caller, wearing H | none | hub API sees `keycloak:<sub>` + role group |
| **TOKEN EXCHANGE H ──▶ R** | client `lightspeed-hub-a2a` | yes | run credential for the spoke gateway |
| **ADAPTER RUN-TOKEN SECRET** | adapter SA writes R | none | `ls-run-token-<uid>`, removed at terminal state |
| **ADAPTER PAUSED / RENEWED RUN CREDENTIAL** | adapter | H → R (renewal) | credential-pause path for long runs |
| **ADAPTER UNKNOWN TARGET / DUPLICATE SUPPRESSED / EXCHANGE FAILED** | — | — | fail-fast, idempotency, Keycloak refusal |
| **OPERATOR: GATEWAY CREDENTIAL MODE / OPERATOR SANDBOX** | operator's own SA | **no exchange** | mounts R; no spoke SA or kubeconfig |
| **OPERATOR RUN CREDENTIAL PROBLEM** | — | none | mounted R missing/expired |
| **PRAXIS SPOKE ROUTE / REJECT** | sandbox wearing R | R → U next | per-call exchange at the spoke gateway |
| **TOKEN EXCHANGE R ──▶ U** | client `praxis-spoke-gateway` | yes | authoritative copy from Keycloak |
| **KEYCLOAK MINT / TOKEN_EXCHANGE / …_ERROR** | the acting client + subject | yes | the authoritative grant record |
| **API IDENTITY** (`--api`) | `keycloak:<sub>` | — | the Kubernetes identity of H/U |
| **API TOKEN REJECT / API AUTHZ DENY** (`--api`) | — | — | expected for A/R; a finding for H/U |
| **IDENTITY / VERIFY** | the token the hop may see | — | on hops that log the bearer (ACME, Praxis hub/spoke, adapter, API): names the token and checks its fingerprint against the hop's expected token |
| **PRAXIS EXCHANGE REJECTED** | client `mas-a2a` | A → H failed | Keycloak refused the hub gateway's exchange (`delegation.idp_rejected`, `unauthorized_client`) — see the 2026-11-24 note below |
| **OPERATOR: RUN HAS NO CREDENTIAL** | — | H → R incomplete | `CredentialsExpired=True` / `ls-run-token-… not found`: the run exists but its Secret was never written |
| **SUSPECT / IMPOSSIBLE** | — | — | a token shown at the wrong hop, an unlabelled bearer, or a fingerprint that does not match the hop (see below) |

### SUSPECT and IMPOSSIBLE cards

These are fingerprint checks, not heuristics. On any hop that logs the bearer
(ACME, the hub/spoke gateways, the adapter, the API server), the card prints
`IDENTITY` (the token that hop may see) and `VERIFY`:

- `matches the hop — this is H` — the fingerprint is the token this hop is
  supposed to hold;
- `MISMATCH — this is A, the hop expects H` — a token crossed the boundary
  without being exchanged (e.g. Token A reaching the adapter);
- `UNIDENTIFIED` — a bearer no hop labelled: it was never minted, exchanged
  or recorded by Keycloak. This is deliberately *not* treated as "fine";
- `RETIRED` — the token was superseded by a renewal.

`IMPOSSIBLE` still flags a later token (H/R/U) on an earlier leg.



These are the "who did what on behalf of whom" checks, and they only fire
when the monitor can *identify* the token (fingerprint, or a hop whose bearer
is fixed):

- **IMPOSSIBLE** — a later token (H/R/U) appears on an earlier leg. It cannot
  be produced by this chain, so treat it as a forged/replayed token.
- **SUSPECT A STILL WORN AT THE ADAPTER** — the hub gateway exchanges A→H;
  a valid A reaching the adapter means the exchange did not happen and the
  caller's original token travelled past its boundary.
- **SUSPECT … AT THE SPOKE GATEWAY** — only R is accepted there.
- **SUSPECT A/R AT A KUBE API** — both are rejected there by design.

## Log patterns

Patterns for ACME come from this repo's source. MAS and Praxis were confirmed
against captured live logs on bm-cluster (2026-10-23/26, mas image
`a80e09316564`). The lightspeed-hub and delegator matchers are **UNCONFIRMED
WATCHERS** — broad, labelled on every card they emit, and backed by the
Keycloak feed.

If a hop's vocabulary changes (new image, renamed logger, changed `context=`
keys), capture and replay:

```bash
oc logs -n mao -l app.kubernetes.io/name=mas-a2a  --since 15m > /tmp/mas-a2a.log
oc logs -n mao -l app.kubernetes.io/name=mas-worker --since 15m > /tmp/mas-worker.log
oc logs -n praxis-proxy -l app.kubernetes.io/name=praxis-proxy --since 15m > /tmp/praxis.log
oc logs -n lightspeed-hub -l app.kubernetes.io/name=lightspeed-hub --since 15m > /tmp/adapter.log
oc logs -n lightspeed-hub -l app.kubernetes.io/name=lightspeed-hub-agentic-operator --since 15m > /tmp/operator.log
```

Then tighten the matching block in `scripts/monitor-lib.sh` (`parse_mas`,
`parse_worker`, `parse_praxis`, `parse_adapter`, `parse_operator`,
`parse_kc_event`), add the line to `tests/scripts/test_monitor.py`, and —
per the repo convention — replace the guess immediately.

Routine noise that is deliberately **not** carded:

- `Failed to resolve signing key` (ERROR, ~once per request) — the MAS
  multi-issuer validator probes each issuer's JWKS in turn and logs a miss
  even when a later issuer validates the token. The real rejection signal is
  `Token validation failed for all … issuers` + 401.
- `Task … already exists. Ignoring task replacement.` — A2A task polling.
- `/.well-known/agent-card.json`, `/readyz`, `/healthz`, `/readiness` —
  public, unauthenticated requests. They must never open a chain.
- INFO-level worker lines whose context dicts merely contain `'error': None`.
- `temporalio.worker._activity` payload dumps and other multi-line
  continuations.
- Praxis's `static_response` "request rejected … status=200" (the agent card).
- Keycloak events for clients outside the chain (e.g. `security-admin-console`).

## Correlating hops

ACME stamps `X-Request-Id` on its outbound POST and logs it, but MAS mints
its own request id for the same request and the hub/spoke hops have theirs,
so `REQUEST_ID` isolates one hop's lines rather than joining them end to end.
The joins that do work across the whole chain:

```bash
# every acme_audit line for one UI request
oc logs -n acme-agent -l app.kubernetes.io/name=acme-agent --since=30m | grep acme_audit

# the worker's delegation lifecycle (DEBUG level in standalone)
oc logs -n mao -l app.kubernetes.io/name=mas-worker --since=30m \
  | grep -aE "using user auth token|stream_message|delegated"

# the authoritative exchange record for the window
# live exchanges (Keycloak realm events); --since seeds the event window
bash scripts/monitor.sh --kc --since 30m
```

- `sub` is identical in A/H/R/U, so it ties the whole chain together; it is
  printed on every exchange card (Keycloak's `userId` is that same `sub`).
- The `fp:…` fingerprint is the same token at every hop that logs it.
- Keycloak's Admin Console (Realm Events, filtered by client) records every
  grant — the monitor's `--kc` feed reads the same data.

## Keycloak event feed details

`--kc` (and the default join) polls
`/admin/realms/<KC_REALM>/admin-events` with the `keycloak-admin-user` Secret
and prints one card per grant:

| Event | Card | What it proves |
| --- | --- | --- |
| `LOGIN` for `acme-agent`, `client_credentials` | **KEYCLOAK MINT A** | the minting client and the principal A carries |
| `TOKEN_EXCHANGE` by `mas-a2a` | **TOKEN EXCHANGE A ──▶ H** | A→H, for `lightspeed-hub-a2a` |
| `TOKEN_EXCHANGE` by `lightspeed-hub-a2a` | **TOKEN EXCHANGE H ──▶ R** | H→R, for `praxis-spoke-gateway` |
| `TOKEN_EXCHANGE` by `praxis-spoke-gateway` | **TOKEN EXCHANGE R ──▶ U** | R→U, for `praxis-spoke-<id>` |
| `TOKEN_EXCHANGE_ERROR` | red card | which hop failed and Keycloak's error (`invalid_client`, …) |

If no cards appear: the monitor prints the realm's event configuration at
start-up. `eventsEnabled=false` (or an `enabledEventTypes` list that omits
token exchange) is the usual cause — enable it under
**Realm settings → Events → Save events**. An unreachable Keycloak prints a
warning and the pod-log hops continue. Do **not** rely on a pod missing the
delegator line: the exchange card comes from Keycloak, and the delegator line
is only a corroborating extra when it exists.

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

## Upstream failure modes the monitor explains (2026-11-24)

The `rca-mao-praxis-hub` branch was advanced with fixes for four live
failures. Each is observable through the monitor, and the card text names the
likely cause so the next debugging session starts from the finding:

| Symptom in the monitor | Cause (upstream lesson) | Where the monitor says it |
| --- | --- | --- |
| `MAS A2A INBOUND … 403`, `sub` is the new principal | `a2a.allowedPrincipals` is injected from a Secret and read **at process startup**; a synced Secret is not a refreshed environment. Restart MAS A2A only. | the 403 footer of `MAS A2A INBOUND` (prints the exact `oc rollout restart`) |
| `PRAXIS EXCHANGE REJECTED` / `KEYCLOAK TOKEN_EXCHANGE_ERROR` for `mas-a2a` (`invalid_client_credentials`) | the registered exchanger secret is a literal `${NAME}`/`$(NAME)` realm-import placeholder. Align the client's secret to its Vault-backed Secret; do not rotate or re-import. | `PRAXIS EXCHANGE REJECTED` + the Keycloak card |
| `ADAPTER AGENTICRUN` created, then `OPERATOR: RUN HAS NO CREDENTIAL` (`ls-run-token-… not found`) | the adapter could not complete H→R or write the Secret (often an untrusted Keycloak CA, surfaced as a generic `request_failed`). A run existing proves nothing about its credential. | the adapter `SEQUENCE` line, `ADAPTER PAUSED`, `OPERATOR: RUN HAS NO CREDENTIAL` |
| a kube read denied on `mario-ipi` only | per-spoke U audiences and role groups; RBAC binds `keycloak:praxis-spoke-<id>:reader`, never `keycloak:<sub>` | `PRAXIS SPOKE ROUTE (spoke:mario-ipi)` + `API IDENTITY` |

The monitor shows these identities and failures; it does not fix them. The
chart-side explanations are in `charts/all/mao/mas-a2a/README.md`,
`charts/all/keycloak-oidc/README.md` and `charts/all/lightspeed-hub/README.md`.

## Troubleshooting the monitor

| Symptom | Cause / fix |
| --- | --- |
| `warn: no pods ... skip <hop>` | selector/namespace wrong — or the chart is dormant on purpose (`mas-a2a` with `a2a.enabled=false`, all of `lightspeed-hub` with `enabled=false`). Check `--endpoint`, then `oc get pods -n <ns> --show-labels`. |
| No Keycloak cards | the monitor prints the realm event config at start; `eventsEnabled=false` is the usual cause (Realm settings → Events). A failed admin login prints a warning about the `keycloak-admin-user` Secret. |
| Keycloak cards but no pod cards | `oc logs` permission, or the hop does not log at that level (worker `logLevel=DEBUG`, Praxis `praxis_filter=debug`). |
| A hop goes quiet after an ArgoCD sync | handled: `oc logs -f` ends when a pod is replaced, so the monitor re-attaches and prints `log stream closed … reattaching`. |
| Two requests' cards interleave | expected with concurrent chat messages; the `A/H/R/U` tag and `sub` separate them. `MONITOR_CHAIN_MODE=1` keeps one running label. |
| `SUSPECT` / `IMPOSSIBLE` cards | a token shown at a hop that should already have exchanged it — a finding, not noise (see "SUSPECT and IMPOSSIBLE cards"). |
| Praxis hub vs spoke cards look wrong | the split is the pod name (`praxis-spoke-<id>`); it needs `oc logs --prefix` intact. `--praxis` alone is the quickest check. |

## Related docs

- [agents/AUTHENTICATION.md](../agents/AUTHENTICATION.md) — full identity chain (orchestrated/passthrough flow), claim shapes, Troubleshooting (including Keycloak Admin Events for grants)
- [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) — why the chain is shaped this way
- [docs/LESSONS_LEARNED.md](../docs/LESSONS_LEARNED.md) — every live finding, symptom and workaround
- [charts/all/mao/mas-a2a/README.md](../charts/all/mao/mas-a2a/README.md) — MAS A2A trust boundary, principal allow-list, activation prerequisites
- [charts/all/praxis-proxy/README.md](../charts/all/praxis-proxy/README.md) — Praxis `logLevel` / JWT enforcement quirks (hub gateway + spoke gateways)
- [charts/all/lightspeed-hub/README.md](../charts/all/lightspeed-hub/README.md) — adapter/operator wiring the monitor `--hub` hop reads
- [docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md](../docs/GATEWAY_MODE_SCOPED_TOKEN_EXCHANGE.md) — the per-hop exchange chain and why each `aud` is what it is

# Remediation helpers

`scripts/remediation/` holds the operator-side helpers for the
approval-gated execution path (see the
[deployment runbook](../docs/lightspeed-hub-deployment-runbook.md) A4 d and
B3). All use only the standard library and `oc`.

| Script | Purpose |
| --- | --- |
| `spoke-writer-rbac.sh <spoke> <ns>...` | Prints the writer ClusterRole (the lightspeed-hub chart's `spokeExecutorRBAC.rules`) and namespaced RoleBindings for `keycloak:praxis-spoke-<spoke>:writer`, to apply **on that spoke**. |
| `capture_exec_claims.py <secret> <run> <timeout>` | Token-separation gate: decodes `aud`/`azp`/`sub`/`exp`/`groups` of a run's R and R_exec from one Secret read; never prints a token; fails closed. |
| `keycloak_execution_brownfield.py` | **Brownfield only**: adds the execution client, roles, group, scopes and mappers to an already-imported realm (`--dry-run`, `--grant-acme-membership`); `--verify` checks the groups issued to ACME's service account on any realm. See [brownfield upgrade](../docs/A2A_REMEDIATION_BROWNFIELD.md). |
