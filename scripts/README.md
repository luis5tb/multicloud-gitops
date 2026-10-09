# Flow monitor

Real-time view of the agentic investigation identity chain on an OpenShift
cluster: ACME → Praxis → OLS A2A → Keycloak (RFC 8693) → MCP → API server.

Adapted from the Docker/Postgres monitor in
[agentic-partners-integration](https://github.com/rh-ai-quickstart/agentic-partners-integration/blob/main/scripts/monitor.sh).
This pattern has no local audit database; correlation uses greppable log lines
joined on `X-Request-Id` / `request_id`:

- ACME emits `acme_audit request_id=…` (Token A dispatch)
- OLS emits `a2a_audit request_id=…` (Token A validation + Token B exchange + query)

## Prerequisites

- `oc` on `PATH`, logged into the target cluster (`oc whoami`)
- GNU `grep` (`grep -oP`; Fedora/RHEL default)
- Pattern apps deployed (namespaces below from `variants/standalone/values-standalone.yaml`)

Optional for richer Praxis cards: raise `logLevel` on the praxis-proxy chart
(wired to `RUST_LOG`), for example:

```text
info,praxis_filter=debug,praxis_policy_plugin_identity_jwt=debug
```

## Quick start

From the repo root:

```bash
bash scripts/monitor.sh
```

Send a chat message through the ACME UI that includes a target cluster API URL
from `global.olsClusters`. You should see an ACME DISPATCH card, then an OLS A2A
card sharing the same `request_id`.

## Modes

| Command | What it tails |
| --- | --- |
| `bash scripts/monitor.sh` | All hops (ACME, Praxis, OLS, MCP) |
| `bash scripts/monitor.sh --join` | Same as default |
| `bash scripts/monitor.sh --acme` | ACME only |
| `bash scripts/monitor.sh --ols` | OLS app-server only |
| `bash scripts/monitor.sh --praxis` | Praxis only |
| `bash scripts/monitor.sh --mcp` | MCP only |
| `bash scripts/monitor.sh --since 10m` | Start from logs of the last 10m (`oc --since`) |

Filter one request after you copy an id from an ACME card:

```bash
REQUEST_ID=<request_id> bash scripts/monitor.sh
```

## Namespaces and selectors

| Hop | Namespace | Label selector | Default override env |
| --- | --- | --- | --- |
| ACME | `acme-agent` | `app.kubernetes.io/name=acme-agent` | `NS_ACME`, `SEL_ACME` |
| Praxis | `praxis-proxy` | `app.kubernetes.io/name=praxis-proxy` | `NS_PRAXIS`, `SEL_PRAXIS` |
| OLS | `a2a-lightspeed` | `app.kubernetes.io/component=application-server` | `NS_OLS`, `SEL_OLS` |
| MCP | `a2a-lightspeed` | `app=openshift-mcp-server` | `NS_OLS`, `SEL_MCP` |

Also: `MONITOR_SINCE` (same as `--since`), `REQUEST_ID` (filter).

Verify labels on a live cluster before debugging empty streams:

```bash
oc get pods -n acme-agent,praxis-proxy,a2a-lightspeed --show-labels
```

If a selector matches no pods, the script prints a warning and skips that hop.

## What you will see

| Card | Meaning |
| --- | --- |
| **ACME DISPATCH** | ACME stamped `X-Request-Id` and sent Token A toward Praxis/OLS (`acme_audit` … `outcome=sent`) |
| **OLS A2A** | OLS validated Token A, performed the RFC 8693 exchange for Token B, and recorded the query (`a2a_audit`; `on_behalf_of` is the Token A `azp`) |
| **AUTH/EXCHANGE FAILURE** | Inbound JWT reject, cluster header reject, exchange failure, SPIFFE error, etc. |
| **PRAXIS** | JWT/policy deny lines (only useful when `logLevel` / `RUST_LOG` is raised) |
| **MCP/API DENY** | 401/403 from MCP logs — usually API-server OIDC/RBAC on Token B (MCP is passthrough) |

Tokens themselves are never printed (by design of `acme_audit` / `a2a_audit`).

## Lab note: no human login in these logs

The ACME UI Route is unauthenticated by design in this pattern. The monitor shows
**workload identity** (SPIFFE → Keycloak client_credentials → Token A → exchange
→ Token B), not an end-user OIDC login. Anyone who can reach ACME acts as the
`acme-agent` client for cluster investigation.

## Related docs

- [agents/AUTHENTICATION.md](../agents/AUTHENTICATION.md) — full identity chain, claim shapes, Troubleshooting (including Keycloak Admin Events for grants/exchanges)
- [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) — why the chain is shaped this way
- [charts/all/praxis-proxy/README.md](../charts/all/praxis-proxy/README.md) — Praxis `logLevel` / JWT enforcement quirks
