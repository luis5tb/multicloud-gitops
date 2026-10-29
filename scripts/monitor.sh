#!/usr/bin/env bash
# Real-time identity/delegation flow monitor for multicloud-gitops.
# Tracks the caller's token through every stage of the agentic chain:
#
#   acme-agent ─Token A─▶ MAO A2A endpoint ─Token A─▶ MAS orchestrator
#     ─Token A─▶ praxis ─Token A─▶ OLS A2A ─exchange: Token B─▶
#     openshift mcp ─Token B─▶ OCP API server (RBAC)
#
# Usage:
#   bash scripts/monitor.sh                # live stream (all tailable hops)
#   bash scripts/monitor.sh --acme         # ACME only
#   bash scripts/monitor.sh --mas          # MAO A2A endpoint only (mao ns)
#   bash scripts/monitor.sh --worker       # MAS orchestrator (Temporal worker)
#   bash scripts/monitor.sh --praxis       # Praxis only
#   bash scripts/monitor.sh --ols          # OLS A2A app-server only
#   bash scripts/monitor.sh --mcp          # OpenShift MCP only
#   bash scripts/monitor.sh --api          # OCP API server token rejects
#   bash scripts/monitor.sh --join         # all hops except --api (default)
#   bash scripts/monitor.sh --since 10m    # oc --since (default: 0s = now)
#   REQUEST_ID=abc123 bash scripts/monitor.sh --join  # filter one request
#
# Unlike agentic-partners-integration/scripts/monitor.sh this uses `oc logs`
# (no Docker/Postgres audit_events). ACME and OLS emit structured audit lines
# (acme_audit / a2a_audit); the MAS (uie-mas-hosted), Praxis, OLS-internal
# and MCP-server patterns were confirmed against live logs on bm-cluster
# (2026-10-23 and 2026-10-26, mas image a80e09316564) -- see the comments in
# parse_line and scripts/README.md for the routine-noise exclusions and the
# request_id caveats. Tokens are never printed by design — audit lines and
# cards carry only short SHA-256 fingerprints (fp:…) that identify a token
# without being usable as one.
#
# See scripts/README.md for selectors, overrides, and troubleshooting tips.

set -euo pipefail

SINCE="${MONITOR_SINCE:-0s}"
FILTER_REQ="${REQUEST_ID:-}"
MODE="all"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --acme)   MODE=acme; shift ;;
    --mas)    MODE=mas; shift ;;
    --worker) MODE=worker; shift ;;
    --ols)    MODE=ols; shift ;;
    --praxis) MODE=praxis; shift ;;
    --mcp)    MODE=mcp; shift ;;
    --api)    MODE=api; shift ;;
    --join)   MODE=join; shift ;;
    --since)
      if [[ $# -lt 2 ]]; then
        echo "error: --since requires a duration (e.g. 10m, 1h)" >&2
        exit 1
      fi
      SINCE="$2"
      shift 2
      ;;
    -h|--help)
      sed -n '2,29p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $1 (try --help)" >&2
      exit 1
      ;;
  esac
done

# ── colours ──────────────────────────────────────────────────────────────────
R=$'\033[0;31m'
G=$'\033[0;32m'
Y=$'\033[1;33m'
B=$'\033[0;34m'
C=$'\033[0;36m'
M=$'\033[0;35m'
W=$'\033[1;37m'
D=$'\033[2m'
N=$'\033[0m'

# ── namespaces / selectors (from values-standalone + chart labels) ────────────
NS_ACME="${NS_ACME:-acme-agent}"
NS_PRAXIS="${NS_PRAXIS:-praxis-proxy}"
NS_OLS="${NS_OLS:-a2a-lightspeed}"
NS_MAS="${NS_MAS:-mao}"
NS_WORKER="${NS_WORKER:-mao}"
# Needs cluster-admin to read; not part of the default join for that reason.
NS_API="${NS_API:-openshift-kube-apiserver}"

SEL_ACME="${SEL_ACME:-app.kubernetes.io/name=acme-agent}"
SEL_PRAXIS="${SEL_PRAXIS:-app.kubernetes.io/name=praxis-proxy}"
# Verified live: oc get pods -n a2a-lightspeed --show-labels
SEL_OLS="${SEL_OLS:-app.kubernetes.io/component=application-server}"
# Chart-confirmed MCP labels (AUTHENTICATION.md's component=mcp-server may be stale)
SEL_MCP="${SEL_MCP:-app=openshift-mcp-server}"
# Verified live (bm-cluster): charts/all/mao render app.kubernetes.io/name=<chart>.
# mas-a2a renders no pods at all until a2a.enabled=true -- a missing hop is
# skipped with a warning.
SEL_MAS="${SEL_MAS:-app.kubernetes.io/name=mas-a2a}"
SEL_WORKER="${SEL_WORKER:-app.kubernetes.io/name=mas-worker}"
# Verified live (bm-cluster): the static pods carry app=openshift-kube-apiserver
SEL_API="${SEL_API:-app=openshift-kube-apiserver}"

trunc() {
  local s="$1" n="${2:-100}"
  if [[ ${#s} -gt $n ]]; then
    echo "${s:0:$n}…"
  else
    echo "$s"
  fi
}

require_oc() {
  if ! command -v oc >/dev/null 2>&1; then
    echo "error: oc not found on PATH" >&2
    exit 1
  fi
  if ! oc whoami >/dev/null 2>&1; then
    echo "error: oc is not logged in (oc whoami failed)" >&2
    exit 1
  fi
}

print_diagram() {
  clear 2>/dev/null || true
  cat << 'DIAGRAM'
══════════════════════════════════════════════════════════════════════════════
  IDENTITY FLOW MONITOR — ACME · MAS · Praxis · OLS · Keycloak · MCP · API
══════════════════════════════════════════════════════════════════════════════

  Full request (one chat message; standalone Phase-3 routing):

  [Browser / ACME UI]  ← unauthenticated Route (lab assumption)
       │  A2A message/send  (must include apiURL from global.olsClusters)
       ▼
  ┌─ ACME-AGENT ────────────────────────────────────────────────────────────┐
  │  1. ClusterRoutingMiddleware — allow-list / derive X-OLS-Cluster id     │
  │  2. LiteLLM (static LITELLM_API_KEY) — route to mas_orchestrator        │
  │  3. MINT TOKEN A —                                                      │
  │     · Keycloak: SPIFFE JWT-SVID → client_credentials                    │
  │       azp=acme-agent  aud=[mas-a2a, lightspeed-a2a]  groups=acme-…-rca  │
  │     · or Red Hat SSO prod/stage agent token (aud=api.ask_red_hat)       │
  │  4. POST to MAO A2A endpoint  Authorization: Bearer TOKEN A             │
  │     → logs acme_audit … outcome=sent token=fp:… source=…               │
  │  4b. On 401 / TASK_STATE_AUTH_REQUIRED: mint fresh Token A, resend     │
  │     → logs acme_audit … "renew Token A" renewed_from=fp:… token=fp:…   │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  TOKEN A (unchanged)
  ┌─ MAO A2A ENDPOINT (mao/mas-a2a) ────────────────────────────────────────┐
  │  5. Validate TOKEN A — multi-issuer OIDC: Keycloak aud=mas-a2a,         │
  │     Red Hat SSO prod/stage aud=api.ask_red_hat (sig/iss/exp/aud)        │
  │  6. Principal allow-list: sub ∈ a2a.allowedPrincipals (else 401/403)    │
  │  7. NO exchange — Token A kept in RunContext.credentials (passthrough)  │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  TOKEN A (unchanged)
  ┌─ MAS ORCHESTRATOR (mao/mas-worker via Temporal) ────────────────────────┐
  │  8. Blueprint run — LLM plans: Ask Red Hat vs OLS Investigator vs both  │
  │  9. Outbound A2A with RunContext.auth_token = TOKEN A:                  │
  │     · OLS via Praxis — X-OLS-Cluster must be spelled EXACTLY            │
  │       (X_OLS_Cluster underscores → Praxis 404, delegation lost)         │
  │     · Ask Red Hat direct (access.stage.redhat.com), no routing header   │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  TOKEN A (unchanged)
  ┌─ PRAXIS-PROXY ──────────────────────────────────────────────────────────┐
  │ 10. Policy filter: validate TOKEN A + azp allow-list (else deny)        │
  │     Route by X-OLS-Cluster → OLS upstream (fail-closed, no default)     │
  │     (raise logLevel / RUST_LOG for routing + JWT detail)                │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  TOKEN A (unchanged)
  ┌─ OLS A2A (lightspeed-app-server) ───────────────────────────────────────┐
  │ 11. Validate TOKEN A (multi-issuer OIDC + X-OLS-Cluster binding)        │
  │ 12. EXCHANGE — Keycloak RFC 8693 (subject_token=TOKEN A, OLS SPIFFE)    │
  │     → TOKEN B: azp=lightspeed-mcp  aud=openshift-mcp  sub unchanged     │
  │ 13. MCP tool calls carry TOKEN B                                        │
  │     → logs a2a_audit … on_behalf_of=<Token A azp> outcome=ok|error      │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  TOKEN B (Token A never reaches MCP or the API server)
  ┌─ OPENSHIFT MCP (openshift-mcp-server) ──────────────────────────────────┐
  │ 14. require_oauth=true + passthrough — MCP does no authz of its own     │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  TOKEN B (forwarded unmodified)
  ┌─ OCP API SERVER (RBAC — final authority) ───────────────────────────────┐
  │ 15. OIDC-validate TOKEN B (cached JWKS), claimMappings                  │
  │     groups=acme-agent-rca → Group keycloak:acme-agent-rca               │
  │ 16. RBAC allow|deny — not tailable by default; watch MCP/API DENY cards │
  │     or run --api (cluster-admin), then confirm the group's rights:      │
  │     oc auth can-i get pods --as-group=keycloak:acme-agent-rca -n <ns>   │
  └─────────────────────────────────────────────────────────────────────────┘

  Join one request:
    REQUEST_ID=<id> bash scripts/monitor.sh
    (per-hop ids only: MAS and OLS mint their own request ids -- see
     scripts/README.md "Correlating hops" for time-window correlation)

  Legend:  dispatch   mint/exchange   mas/ols   praxis/jwt   mcp/api   fail
──────────────────────────────────────────────────────────────────────────────
DIAGRAM
}

# Parse one log line into a coloured card when it matches known patterns.
parse_line() {
  local src="$1" line="$2" ts
  ts=$(date +%H:%M:%S)

  # Optional REQUEST_ID filter
  if [[ -n "$FILTER_REQ" ]] && ! grep -qF "$FILTER_REQ" <<<"$line"; then
    return 0
  fi

  # Strip ANSI colour escapes (Praxis's tracing output embeds them around
  # field names) so the patterns below see clean names/values.
  line=$(sed 's/\x1b\[[0-9;]*m//g' <<<"$line")

  # ── ACME structured audit ───────────────────────────────────────────────
  if [[ "$line" == *"acme_audit "* ]]; then
    local rid cluster action outcome token source renewed_from reason
    rid=$(grep -oP 'request_id=\K[^ ]+' <<<"$line" || true)
    cluster=$(grep -oP 'cluster=\K[^ ]+' <<<"$line" || true)
    action=$(grep -oP 'action=\K("[^"]*"|[^ ]+)' <<<"$line" || true)
    outcome=$(grep -oP 'outcome=\K[^ ]+' <<<"$line" || true)
    token=$(grep -oP ' token=\K\S+' <<<"$line" || true)
    source=$(grep -oP 'source=\K\S+' <<<"$line" || true)
    renewed_from=$(grep -oP 'renewed_from=\K\S+' <<<"$line" || true)
    reason=$(grep -oP 'reason=\K[^ ]+' <<<"$line" || true)

    if [[ "$action" == *'renew Token A'* ]]; then
      local rn_color="$G"
      [[ "$outcome" != "ok" ]] && rn_color="$R"
      printf "\n${Y}┌─ ACME TOKEN RENEWAL [${ts}] ${rn_color}${outcome}${N}\n"
      printf "${Y}│${N} ${D}reason : ${reason:-?} — downstream refused the current token${N}\n"
      printf "${Y}│${N} ${D}token  : ${renewed_from:-?} → ${W}${token:-?}${N} ${D}(refused → freshly minted)${N}\n"
      if grep -q 'override=retired' <<<"$line"; then
        printf "${Y}│${N} ${D}a UI-pasted override token was refused and is now retired${N}\n"
      fi
      printf "${Y}│${N} request_id : ${W}${rid}${N}\n"
      printf "${Y}└─ CredentialRenewingTransport resent the request with the fresh token${N}\n"
      return 0
    fi

    if [[ "$action" == *'mint'*'Token A'* ]]; then
      local st_color="$G" label="TOKEN A MINT"
      local creator="Keycloak client_credentials + SPIFFE assertion"
      if [[ "$action" == *'Red Hat SSO'* ]]; then
        label="TOKEN A MINT (Red Hat SSO)"
        creator="Red Hat SSO client_credentials (confidential client)"
      fi
      [[ "$outcome" != "ok" ]] && st_color="$R" && label="${label} FAILED"
      printf "\n${M}┌─ ${label} [${ts}]${N}\n"
      printf "${M}│${N} ${D}CREATOR : ${creator}${N}\n"
      printf "${M}│${N} request_id : ${W}${rid}${N}\n"
      printf "${M}│${N} cluster    : ${cluster}\n"
      printf "${M}│${N} token      : ${W}${token}${N}\n"
      printf "${M}│${N} action     : ${action}\n"
      printf "${M}│${N} outcome    : ${st_color}${outcome}${N}\n"
      if [[ "$outcome" == "ok" ]]; then
        printf "${M}└─ Token A cached for the outbound A2A call (never logged in full)${N}\n"
      else
        printf "${M}└─ no Token A — dispatch will fail${N}\n"
      fi
      return 0
    fi

    if [[ "$outcome" == "denied" ]] || [[ "$action" == *'reject unregistered'* ]]; then
      local detail
      detail=$(grep -oP 'detail="\K[^"]+' <<<"$line" || true)
      printf "\n${R}┌─ CLUSTER ALLOW-LIST REJECT [${ts}]${N}\n"
      printf "${R}│${N} ${D}GATE : ClusterRoutingMiddleware (before ADK / Keycloak)${N}\n"
      printf "${R}│${N} request_id : ${W}${rid}${N}\n"
      printf "${R}│${N} detail     : %s\n" "$detail"
      printf "${R}└─ no Token A mint, no MAS/Praxis/OLS call${N}\n"
      return 0
    fi

    printf "\n${C}┌─ ACME DISPATCH [${ts}]${N}\n"
    printf "${C}│${N} ${D}CREATOR : Keycloak or Red Hat SSO (Token A)${N}\n"
    printf "${C}│${N} ${D}NEXT    : MAO A2A endpoint (Phase 3) · Praxis→OLS (Phase 1)${N}\n"
    printf "${C}│${N} request_id : ${W}${rid}${N}\n"
    printf "${C}│${N} cluster    : ${W}${cluster}${N}\n"
    if [[ -n "$token" ]]; then
      if [[ "$source" == "pasted_override" ]]; then
        printf "${C}│${N} token      : ${Y}${token}${N} ${Y}(UI-pasted override — not a minted Token A)${N}\n"
      else
        printf "${C}│${N} token      : ${W}${token}${N} ${D}(minted Token A)${N}\n"
      fi
    fi
    printf "${C}│${N} action     : ${action}\n"
    printf "${C}│${N} outcome    : ${G}${outcome}${N}\n"
    printf "${C}└─ → follow Token A: mas-a2a validation → orchestrator → a2a_audit${N}\n"
    return 0
  fi

  # ── OLS structured audit (inbound validate + RFC 8693 exchange) ──────────
  # CONFIRMED LIVE (bm-cluster, 2026-10-23): a2a_audit carries one line per
  # request whose action names the audited step -- "exchange MCP credentials"
  # when the exchange itself is the event (observed on its failure; the
  # known SSO openid-scope / groups-mapper failures surface here), and
  # "dispatched MCP-grounded query" once the query went out with Token B.
  # A successful exchange therefore shows as the dispatch line -- the card
  # spells the Token A → Token B step out either way.
  if [[ "$line" == *"a2a_audit "* ]]; then
    local rid actor obo sub cluster action outcome task
    local oc_color icon title
    rid=$(grep -oP 'request_id=\K[^ ]+' <<<"$line" || true)
    actor=$(grep -oP 'actor=\K[^ ]+' <<<"$line" || true)
    obo=$(grep -oP 'on_behalf_of=\K[^ ]+' <<<"$line" || true)
    sub=$(grep -oP 'subject=\K[^ ]+' <<<"$line" || true)
    cluster=$(grep -oP 'cluster=\K[^ ]+' <<<"$line" || true)
    action=$(grep -oP 'action=\K("[^"]*"|[^ ]+)' <<<"$line" || true)
    outcome=$(grep -oP 'outcome=\K[^ ]+' <<<"$line" || true)
    task=$(grep -oP 'task=\K[^ ]+' <<<"$line" || true)
    oc_color="$G"
    icon="ok"
    if [[ "$outcome" != "ok" && "$outcome" != "sent" ]]; then
      oc_color="$R"
      icon="fail"
    fi
    if [[ "$action" == *'exchange'* ]]; then
      printf "\n${M}┌─ OLS TOKEN EXCHANGE [${ts}] ${oc_color}${icon}${N}\n"
      printf "${M}│${N} ${D}ACTOR   : ${actor} (authenticates the exchange as lightspeed-mcp)${N}\n"
      printf "${M}│${N} ${D}OBO     : ${obo} (Token A azp)${N}\n"
      printf "${M}│${N} ${D}SUBJECT : ${sub}${N}\n"
      printf "${M}│${N} request_id : ${W}${rid}${N}\n"
      printf "${M}│${N} cluster    : ${cluster}\n"
      printf "${M}│${N} task       : ${D}${task}${N}\n"
      printf "${M}│${N} outcome    : ${oc_color}${outcome}${N}\n"
      printf "${M}└─ RFC 8693: Token A → Token B (azp=lightspeed-mcp, aud=openshift-mcp, sub unchanged)${N}\n"
      if [[ "$outcome" != "ok" ]]; then
        printf "${M}   see agents/AUTHENTICATION.md — known exchange failures: SSO scope,${N}\n"
        printf "${M}   subject_issuer, groups mapper, subject_token_type${N}\n"
      fi
      return 0
    fi
    printf "\n${M}┌─ OLS A2A [${ts}] ${oc_color}${icon}${N}\n"
    printf "${M}│${N} ${D}ACTOR   : ${actor} (RFC 8693 exchange client)${N}\n"
    printf "${M}│${N} ${D}OBO     : ${obo} (Token A azp)${N}\n"
    printf "${M}│${N} ${D}SUBJECT : ${sub}${N}\n"
    printf "${M}│${N} request_id : ${W}${rid}${N}\n"
    printf "${M}│${N} cluster    : ${cluster}\n"
    printf "${M}│${N} task       : ${D}${task}${N}\n"
    printf "${M}│${N} action     : ${action}\n"
    printf "${M}│${N} outcome    : ${oc_color}${outcome}${N}\n"
    printf "${M}│${N} EXCHANGE   : Token A → Token B (RFC 8693) preceded this dispatch${N}\n"
    printf "${M}└─ Token B (azp=lightspeed-mcp, aud=openshift-mcp) carried the MCP query${N}\n"
    return 0
  fi

  # ── OLS internals: validation, the exchange grant, the MCP tool loop ─────
  # CONFIRMED LIVE (bm-cluster, 2026-10-26): OLS's httpx client logs every
  # outbound request -- the Keycloak token-endpoint POST below IS the RFC
  # 8693 Token B mint, and the MCP session/tool-loop lines are Token B being
  # spent. Unmatched lines fall through to the generic failure matcher.
  if [[ "$src" == "ols" ]]; then
    if grep -q 'User ID: ' <<<"$line"; then
      local o_uid
      o_uid=$(grep -oP 'User ID: \K\S+' <<<"$line" || true)
      printf "\n${G}┌─ OLS CALLER VALIDATED [${ts}]${N}\n"
      printf "${G}│${N} ${D}User ID: ${o_uid} — Token A validated (multi-issuer OIDC)${N}\n"
      printf "${G}└─ this subject becomes the RFC 8693 exchange subject${N}\n"
      return 0
    fi
    if grep -q 'HTTP Request: POST' <<<"$line" && grep -q 'protocol/openid-connect/token' <<<"$line"; then
      local x_status x_color
      x_status=$(grep -oP '"HTTP/1\.1 \K[0-9]+' <<<"$line" || true)
      x_color="$G"
      [[ "$x_status" != "200" ]] && x_color="$R"
      printf "\n${M}┌─ OLS TOKEN EXCHANGE [${ts}] ${x_color}${x_status}${N}\n"
      printf "${M}│${N} ${D}Keycloak token endpoint — RFC 8693 grant (subject_token=Token A)${N}\n"
      printf "${M}└─ 200 = Token B minted (azp=lightspeed-mcp, aud=openshift-mcp, sub unchanged)${N}\n"
      return 0
    fi
    if grep -q 'Received session ID:' <<<"$line"; then
      local mcpsid
      mcpsid=$(grep -oP 'Received session ID: \K\S+' <<<"$line" || true)
      printf "\n${M}┌─ OLS → MCP SESSION [${ts}]${N}\n"
      printf "${M}│${N} ${D}session_id=${mcpsid} — MCP connection opened with Token B${N}\n"
      printf "${M}└─ tool calls follow on this session; API-server RBAC decides on Token B${N}\n"
      return 0
    fi
    if grep -q 'Tool loop iteration' <<<"$line"; then
      local loop
      loop=$(grep -oP 'Tool loop iteration \K\S+ outcome=\S+' <<<"$line" || true)
      printf "\n${D}┌─ OLS TOOL LOOP [${ts}]${N}\n"
      printf "${D}│${N} ${D}${loop} — MCP tools executing with Token B${N}\n"
      printf "${D}└─${N}\n"
      return 0
    fi
  fi

  # ── MAO A2A endpoint: Token A validation + principal allow-list ──────────
  # Patterns CONFIRMED LIVE (bm-cluster, 2026-10-23, mas image a80e09316564):
  # the endpoint logs structured `context={...}` dicts (inbound.a2a.middleware,
  # mas.session.service). Routine noise is skipped explicitly:
  #  - "Task … already exists. Ignoring task replacement." (task polling)
  #  - /readyz + /healthz probes, giant agent-card dumps, startup banners.
  # "Failed to resolve signing key" (ERROR, ~once per request) is NOT noise to
  # hide: the multi-issuer validator probes each issuer's JWKS in turn and
  # logs a miss even when a later issuer validates the token successfully —
  # CONFIRMED LIVE 2026-10-29: the same ERROR appears on fully successful
  # requests. It is shown as a dim benign card (not hidden, not red) so it is
  # never mistaken for the actual rejection signal, which is the
  # "Token validation failed for all … issuers" WARNING + 401 below.
  if [[ "$src" == "mas" ]]; then
    if grep -q 'Failed to resolve signing key' <<<"$line"; then
      local miss_kid
      miss_kid=$(grep -oP 'matches: "\K[^"]+' <<<"$line" || true)
      printf "${D}┄─ mas issuer probe miss [${ts}] kid=${miss_kid} — benign: next issuer's JWKS is tried (multi-issuer OIDC)${N}\n"
      return 0
    fi
    if grep -q 'Token validation failed for all' <<<"$line"; then
      printf "\n${R}┌─ MAS TOKEN REJECTED [${ts}]${N}\n"
      printf "${R}│${N} ${D}no configured issuer validated the bearer (bad sig/iss/exp/aud)${N}\n"
      printf "${R}│${N} ${D}→ expect HTTP 401 next; ACME's renewal transport will mint a${N}\n"
      printf "${R}│${N} ${D}  fresh Token A and resend (watch for ACME TOKEN RENEWAL)${N}\n"
      printf "${R}└─ a UI-pasted token dying here is exactly how the override is tested${N}\n"
      return 0
    fi
    if grep -qE 'already exists\. Ignoring task replacement|/readyz|/healthz|/\.well-known/agent-card|Successfully fetched agent card data|token validator initialized|Starting A2A ASGI server|bootstrap\.container' <<<"$line"; then
      return 0
    fi
    if [[ "$line" == *"A2A HTTP request completed"* ]]; then
      local m_rid m_method m_path m_status m_dur m_color
      m_rid=$(grep -oP "'request_id': '\K[^']+" <<<"$line" || true)
      m_method=$(grep -oP "'method': '\K[^']+" <<<"$line" || true)
      m_path=$(grep -oP "'path': '\K[^']+" <<<"$line" || true)
      m_status=$(grep -oP "'status_code': \K[0-9]+" <<<"$line" || true)
      m_dur=$(grep -oP "'duration_ms': \K[0-9.]+" <<<"$line" || true)
      m_color="$G"
      [[ "$m_status" =~ ^[45] ]] && m_color="$R"
      printf "\n${M}┌─ MAS A2A INBOUND [${ts}] ${m_color}${m_status}${N}\n"
      printf "${M}│${N} ${D}${m_method} ${m_path} (${m_dur}ms) — endpoint hop carrying Token A${N}\n"
      printf "${M}│${N} request_id : ${W}${m_rid}${N} ${D}(MAS's own, not ACME's X-Request-Id)${N}\n"
      if [[ "$m_status" =~ ^2 ]]; then
        printf "${M}└─ Token A validated + principal allow-listed; orchestrator takes over${N}\n"
      else
        printf "${M}└─ 401=bad token (sig/iss/exp/aud) · 403=sub not in a2a.allowedPrincipals${N}\n"
      fi
      return 0
    fi
    if [[ "$line" == *"Submitting session="* ]] && [[ "$line" == *"auth=True"* ]]; then
      local sess
      sess=$(grep -oP 'Submitting session=\K\S+' <<<"$line" || true)
      printf "\n${G}┌─ MAS SESSION START [${ts}]${N}\n"
      printf "${G}│${N} ${D}session=${sess} auth=True — Token A attached to the run context${N}\n"
      printf "${G}└─ orchestrator will pass Token A through unchanged (no exchange)${N}\n"
      return 0
    fi
    # Fallback for validation failures that log outside the middleware line.
    if grep -qiE '(unauthorized|forbidden|not allowed|denied|rejected|invalid|expired)' <<<"$line" \
       && grep -qiE '(token|principal|audience|issuer|credential|jwks|signature|auth)' <<<"$line"; then
      printf "\n${R}┌─ MAS A2A AUTH DENY [${ts}]${N}\n"
      printf "${R}│${N} ${D}GATE : multi-issuer OIDC + principal allow-list (Token A)${N}\n"
      printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${R}└─ 401=bad token (sig/iss/exp/aud) · 403=sub not in a2a.allowedPrincipals${N}\n"
      return 0
    fi
    return 0
  fi

  # ── MAS orchestrator (Temporal worker): planning + Token A passthrough ───
  # Patterns CONFIRMED LIVE (bm-cluster, 2026-10-23, mas-worker at
  # logLevel=DEBUG): the token-passthrough and stream lines are DEBUG-level
  # and disappear at INFO — the standalone DEBUG override is what makes the
  # Token A passthrough visible here.
  if [[ "$src" == "worker" ]]; then
    # temporalio activity payloads echo the whole prompt/workplan dump at
    # DEBUG; they repeat the workplan headers many times per delegation and
    # their arbitrary prompt text trips the keyword matchers below. The
    # meaningful lines (tool.execute, applying_runtime_headers,
    # a2a_agent_node_factory) carry the same information once.
    if grep -q 'temporalio.worker._activity' <<<"$line"; then
      return 0
    fi
    # Multi-line log records arrive as separate lines; only the first line
    # carries the logger name, continuations start with whitespace (after
    # oc's "[ns/pod:container] " --prefix, which adds exactly one space).
    # Skip them.
    local w_content="$line"
    [[ "$w_content" =~ ^\[[^]]*\][[:space:]](.*)$ ]] && w_content="${BASH_REMATCH[1]}"
    if [[ "$w_content" =~ ^[[:space:]] ]]; then
      return 0
    fi
    # Live trap: the planning LLM emits X_OLS_Cluster (underscores) in
    # workplan tool args; Praxis matches the header name exactly and
    # fail-closes into HTTP 404. Trapped ONLY on the tool.execute line (the
    # LLM's own output): the a2a.applying_runtime_headers line can show the
    # stored underscore name even on attempts that route successfully
    # (observed live 2026-10-26 — a retried delegation trapped on
    # applying_runtime_headers yet Praxis routed it), and the actual failure
    # is always caught by PRAXIS NO ROUTE regardless.
    if grep -q 'X_OLS_Cluster' <<<"$line" && grep -q 'tool.execute' <<<"$line"; then
      printf "\n${R}┌─ MISSPELLED ROUTING HEADER [${ts}]${N}\n"
      printf "${R}│${N} ${D}X_OLS_Cluster (underscores) in a delegation — Praxis will 404 it${N}\n"
      printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${R}└─ docs/ARCHITECTURE.md: never let an LLM spell a transport header${N}\n"
      return 0
    fi
    # The Token A passthrough itself: a2a_agent_node with an empty
    # bearer_token reads the caller's token from the run context.
    if [[ "$line" == *"using user auth token"* ]]; then
      local node_url
      node_url=$(grep -oP 'A2A agent node \[\K[^\]]+' <<<"$line" || true)
      printf "\n${G}┌─ TOKEN A PASSTHROUGH [${ts}]${N}\n"
      printf "${G}│${N} ${D}a2a_agent_node → ${node_url}${N}\n"
      printf "${G}│${N} ${D}empty bearer_token: the node uses RunContext.auth_token (Token A)${N}\n"
      printf "${G}└─ Token A forwarded verbatim — MAS never exchanges or rewrites it${N}\n"
      return 0
    fi
    if [[ "$line" == *"A2A stream_message to agent="* ]]; then
      local t_agent t_url t_auth
      t_agent=$(grep -oP 'to agent=\K\S+' <<<"$line" || true)
      t_url=$(grep -oP 'url=\K\S+' <<<"$line" || true)
      t_auth=$(grep -oP 'auth=\K\S+' <<<"$line" || true)
      printf "\n${Y}┌─ MAS OUTBOUND A2A [${ts}]${N}\n"
      printf "${Y}│${N} ${D}agent=${t_agent}  auth=${t_auth}${N}\n"
      printf "${Y}│${N} ${D}url=${t_url}${N}\n"
      printf "${Y}└─ OLS goes via Praxis (needs X-OLS-Cluster) · Ask Red Hat is direct${N}\n"
      return 0
    fi
    if [[ "$line" == *"a2a.delegated"* ]]; then
      local d_node
      d_node=$(grep -oP "'node_uid': '\K[^']+" <<<"$line" || true)
      printf "\n${Y}┌─ MAS DELEGATED [${ts}]${N}\n"
      printf "${Y}│${N} ${D}node_uid=${d_node} — work item dispatched to the remote agent${N}\n"
      printf "${Y}└─ logs at stream completion — line up with PRAXIS ROUTE / OLS A2A by timestamp${N}\n"
      return 0
    fi
    # Card only the tool.execute line that carries tool_args (the header the
    # LLM chose); MAS also logs the bare tool name and phase-timing tables,
    # which would duplicate this card several times per delegation.
    if [[ "$line" == *"workplan.plan_and_delegate"* ]] && [[ "$line" == *"tool_args"* ]]; then
      local w_cluster
      w_cluster=$(grep -oP "'X-OLS-Cluster': '\K[^']+" <<<"$line" || true)
      printf "\n${Y}┌─ ORCHESTRATOR PLAN [${ts}]${N}\n"
      printf "${Y}│${N} ${D}workplan.plan_and_delegate — LLM routes Ask Red Hat vs OLS${N}\n"
      if [[ -n "$w_cluster" ]]; then
        printf "${Y}│${N} ${D}X-OLS-Cluster=${w_cluster} (spelled correctly)${N}\n"
      fi
      printf "${Y}└─ Token A stays in the run context for every delegated work item${N}\n"
      return 0
    fi
    if grep -qiE 'certificate verify failed|CERTIFICATE_VERIFY_FAILED|SSLError' <<<"$line"; then
      printf "\n${R}┌─ MAS OUTBOUND TLS FAILURE [${ts}]${N}\n"
      printf "${R}│${N} ${D}agent-card fetch / A2A call hit an untrusted Route CA${N}\n"
      printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${R}└─ identity.keycloakCaTrust.enabled must be true on the MAS caller${N}\n"
      return 0
    fi
    if grep -qiE '(no route matched|http[^0-9]*404|status[^0-9]*404|404 not found)' <<<"$line"; then
      printf "\n${R}┌─ MAS DELEGATION LOST (404) [${ts}]${N}\n"
      printf "${R}│${N} ${D}outbound A2A rejected — usually Praxis fail-closed on the${N}\n"
      printf "${R}│${N} ${D}X-OLS-Cluster header (missing/unknown/misspelled)${N}\n"
      printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${R}└─ Token A was fine; the routing header was not${N}\n"
      return 0
    fi
    # The orchestrator's planning LLM has its own static credential (Vertex
    # ADC) — documented as deliberately separate from Token A.
    if grep -q 'urllib3.connectionpool' <<<"$line" && grep -q 'POST /token' <<<"$line"; then
      printf "\n${D}┌─ PLANNING LLM GRANT (static) [${ts}]${N}\n"
      printf "${D}│${N} %s\n" "$(trunc "$line" 120)"
      printf "${D}└─ Vertex ADC token for the orchestrator's own LLM — not Token A${N}\n"
      return 0
    fi
    # Card failures by LOG LEVEL, not by keyword: INFO lines legitimately
    # contain the word "error" in context dicts (e.g. final_answer's
    # 'error': None on a successful flow), which falsely painted successes
    # red. Only ERROR-level lines and Python tracebacks card as failures.
    if grep -qE ' ERROR |Traceback' <<<"$line"; then
      printf "\n${R}┌─ ORCHESTRATOR ERROR [${ts}]${N}\n"
      printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${R}└─ see charts/all/mao/mas-worker (logLevel) + uie-mas-hosted docs${N}\n"
      return 0
    fi
    return 0
  fi

  # ── OCP API server: Token B OIDC rejects (needs --api + cluster-admin) ───
  if [[ "$src" == "api" ]]; then
    if grep -qiE 'unable to authenticate|invalid bearer token|oidc' <<<"$line"; then
      printf "\n${R}┌─ API TOKEN REJECT [${ts}]${N}\n"
      printf "${R}│${N} ${D}the API server's OIDC authenticator rejected a bearer (Token B)${N}\n"
      printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${R}└─ check Authentication audiences incl. openshift-mcp + issuer/JWKS${N}\n"
      return 0
    fi
    if grep -qiE 'forbidden' <<<"$line"; then
      printf "\n${Y}┌─ API AUTHZ DENY [${ts}]${N}\n"
      printf "${Y}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${Y}└─ RBAC on the mapped group; confirm with oc auth can-i (see README)${N}\n"
      return 0
    fi
    return 0
  fi

  # ── Auth / exchange failures (any hop) ───────────────────────────────────
  if grep -qE 'A2A inbound token rejected|A2A token exchange failed|A2A request rejected|A2A query failed|SpiffeIdentityError|A2AWorkloadIdentityError|ClusterURLError|CERTIFICATE_VERIFY_FAILED' <<<"$line"; then
    printf "\n${R}┌─ AUTH/EXCHANGE FAILURE [${ts}] (${src})${N}\n"
    printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${R}└─ see agents/AUTHENTICATION.md Troubleshooting${N}\n"
    return 0
  fi

  # ── Praxis (policy enforcement in standalone; patterns CONFIRMED LIVE) ───
  # Tracing output, colours stripped above. The policy filter validates the
  # caller JWT and enforces the azp allow-list before routing; success/fail
  # both appear as filter/router decisions and readiness probes are skipped.
  if [[ "$src" == "praxis" ]]; then
    # The dnsIPv4Only CoreDNS sidecar shares this pod. Surface only its errors
    # (a failing AAAA-strip/forward resolver breaks the external SSO JWKS
    # fetch); skip its routine output.
    if grep -q '/dns-ipv4only' <<<"$line"; then
      if grep -qE '\[ERROR\]|SERVFAIL|plugin/errors' <<<"$line"; then
        printf "\n${R}┌─ PRAXIS DNS SIDECAR ERROR [${ts}]${N}\n"
        printf "${R}│${N} ${D}AAAA-strip/forward resolver failing — SSO JWKS lookups at risk${N}\n"
        printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
        printf "${R}└─${N}\n"
      fi
      return 0
    fi
    if grep -qE 'path="?/readiness|path="?/health' <<<"$line"; then
      return 0
    fi
    if grep -q 'no route matched' <<<"$line"; then
      printf "\n${Y}┌─ PRAXIS NO ROUTE [${ts}]${N}\n"
      printf "${Y}│${N} ${D}X-OLS-Cluster missing/unknown — or misspelled by the orchestrator${N}\n"
      printf "${Y}│${N} ${D}LLM (X_OLS_Cluster) — and Praxis fail-closes (no default backend)${N}\n"
      printf "${Y}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${Y}└─ Token A was forwarded but never reached an OLS backend${N}\n"
      return 0
    fi
    if grep -q 'route matched' <<<"$line"; then
      local p_path p_cluster
      p_path=$(grep -oP 'route matched path=\K\S+' <<<"$line" || true)
      p_cluster=$(grep -oP 'cluster=\K\S+' <<<"$line" | head -1 || true)
      printf "\n${B}┌─ PRAXIS ROUTE [${ts}]${N}\n"
      printf "${B}│${N} ${D}path=${p_path} → ${p_cluster}${N}\n"
      printf "${B}└─ policy filter validated caller (JWT + azp allow-list); Token A forwarded${N}\n"
      return 0
    fi
    # static_response "request rejected … status=200" is the agent card being
    # served (benign); only 4xx/5xx filter rejects are failures.
    if grep -q 'request rejected by filter' <<<"$line"; then
      local p_filter p_status
      p_filter=$(grep -oP 'request rejected by filter filter="\K[^"]+' <<<"$line" || true)
      p_status=$(grep -oP 'status=\K[0-9]+' <<<"$line" || true)
      if [[ "$p_status" =~ ^[45] ]]; then
        printf "\n${R}┌─ PRAXIS REJECT [${ts}] ${R}${p_status}${N}\n"
        if [[ "$p_filter" == "policy" ]]; then
          printf "${R}│${N} ${D}policy filter denied — caller JWT invalid, or its azp is not${N}\n"
          printf "${R}│${N} ${D}in policy.allowedCallers (acme-agent + Red Hat SSO client)${N}\n"
        else
          printf "${R}│${N} ${D}filter=${p_filter} — request denied before any OLS backend${N}\n"
        fi
        printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
        printf "${R}└─${N}\n"
      fi
      return 0
    fi
    if grep -qiE 'deny|reject|unauthorized|jwt|forbidden|403|401' <<<"$line"; then
      printf "\n${B}┌─ PRAXIS [${ts}]${N}\n"
      printf "${B}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${B}└─${N}\n"
      return 0
    fi
    # Quiet by default when logLevel is empty — skip noise
    return 0
  fi

  # ── MCP hop (passthrough — real authz is the API server on Token B) ──────
  # CONFIRMED LIVE (bm-cluster, 2026-10-26): the operator-managed MCP server
  # is a Go binary logging klog-style lifecycle lines only — no tool names at
  # default verbosity. The tool-call detail lives on the OLS side
  # (OLS → MCP SESSION / OLS TOOL LOOP cards); here the session lines show
  # Token B being accepted.
  if [[ "$src" == "mcp" ]]; then
    if grep -qiE '401|403|unauthorized|forbidden' <<<"$line" || grep -qE '(^|\] )E[0-9]{4} ' <<<"$line"; then
      printf "\n${Y}┌─ MCP/API DENY [${ts}]${N}\n"
      printf "${Y}│${N} ${D}MCP is passthrough; this is API-server RBAC/OIDC on Token B${N}\n"
      printf "${Y}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${Y}└─ confirm the mapped group's rights:${N}\n"
      printf "${Y}   oc auth can-i get pods --as-group=keycloak:acme-agent-rca -n <namespace>${N}\n"
      return 0
    fi
    if grep -q 'server session connected' <<<"$line"; then
      local s_id
      s_id=$(grep -oP 'session_id="\K[^"]+' <<<"$line" || true)
      printf "\n${G}┌─ MCP SESSION [${ts}]${N}\n"
      printf "${G}│${N} ${D}session_id=${s_id} — MCP accepted the Token B session${N}\n"
      printf "${G}└─ require_oauth passed; the API server's RBAC is the authority${N}\n"
      return 0
    fi
    return 0
  fi
}

follow() {
  local name="$1" ns="$2" sel="$3"
  # --prefix labels lines with pod name so multi-pod namespaces stay readable.
  # Failures (no pods matching selector) print once then exit that follower.
  if ! oc get pods -n "$ns" -l "$sel" --no-headers 2>/dev/null | grep -q .; then
    printf "${Y}warn: no pods in %s matching %s — skip %s${N}\n" "$ns" "$sel" "$name" >&2
    return 0
  fi
  oc logs -n "$ns" -l "$sel" -f --since="$SINCE" --prefix=true --all-containers=true 2>/dev/null \
    | while IFS= read -r line; do
        parse_line "$name" "$line"
      done
}

cleanup() {
  jobs -p 2>/dev/null | xargs -r kill 2>/dev/null || true
}
trap cleanup EXIT INT TERM

require_oc
print_diagram

echo -e "${D}since=${SINCE}  mode=${MODE}  request_id filter=${FILTER_REQ:-none}${N}"
echo -e "${D}Tokens are never printed; each card names the token stage it observed.${N}"
echo -e "${D}Tip: for Praxis JWT/routing detail set praxis-proxy logLevel (RUST_LOG), e.g.${N}"
echo -e "${D}  info,praxis_filter=debug,praxis_policy_plugin_identity_jwt=debug${N}"
echo -e "${D}Tip: Keycloak Admin → Realm Events filtered by clients acme-agent, lightspeed-mcp${N}"
echo -e "${D}Tip: final RBAC hop — confirm the mapped group (needs cluster-admin):${N}"
echo -e "${D}  oc auth can-i get pods --as-group=keycloak:acme-agent-rca -n <namespace>${N}"
echo -e "${D}Tip: bash scripts/monitor.sh --api also tails the API server's token rejects${N}"
echo -e "${D}Docs: scripts/README.md  ·  agents/AUTHENTICATION.md${N}"
echo

case "$MODE" in
  acme)   follow acme   "$NS_ACME"   "$SEL_ACME" ;;
  mas)    follow mas    "$NS_MAS"    "$SEL_MAS" ;;
  worker) follow worker "$NS_WORKER" "$SEL_WORKER" ;;
  ols)    follow ols    "$NS_OLS"    "$SEL_OLS" ;;
  praxis) follow praxis "$NS_PRAXIS" "$SEL_PRAXIS" ;;
  mcp)    follow mcp    "$NS_OLS"    "$SEL_MCP" ;;
  api)    follow api    "$NS_API"    "$SEL_API" ;;
  join|all)
    # --api is deliberately not in the default join: it needs cluster-admin
    # and the API hop is normally observed via MCP/API DENY cards instead.
    follow acme   "$NS_ACME"   "$SEL_ACME"   &
    follow mas    "$NS_MAS"    "$SEL_MAS"    &
    follow worker "$NS_WORKER" "$SEL_WORKER" &
    follow ols    "$NS_OLS"    "$SEL_OLS"    &
    follow praxis "$NS_PRAXIS" "$SEL_PRAXIS" &
    follow mcp    "$NS_OLS"    "$SEL_MCP"    &
    wait
    ;;
esac
