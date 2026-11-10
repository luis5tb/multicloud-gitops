#!/usr/bin/env bash
# Real-time identity/delegation flow monitor for multicloud-gitops.
# Tracks the caller's token through every stage of the agentic chain:
#
#   acme-agent ─Token A─▶ MAO A2A endpoint ─Token A─▶ MAS orchestrator
#     ─Token A─▶ praxis (routing) / Ask Red Hat
#
# Usage:
#   bash scripts/monitor.sh                # live stream (all tailable hops)
#   bash scripts/monitor.sh --acme         # ACME only
#   bash scripts/monitor.sh --mas          # MAO A2A endpoint only (mao ns)
#   bash scripts/monitor.sh --worker       # MAS orchestrator (Temporal worker)
#   bash scripts/monitor.sh --praxis       # Praxis only
#   bash scripts/monitor.sh --api          # OCP API server token rejects
#   bash scripts/monitor.sh --join         # all hops except --api (default)
#   bash scripts/monitor.sh --since 10m    # oc --since (default: 0s = now)
#   REQUEST_ID=abc123 bash scripts/monitor.sh --join  # filter one request
#
# Unlike agentic-partners-integration/scripts/monitor.sh this uses `oc logs`
# (no Docker/Postgres audit_events). ACME emits structured audit lines
# (acme_audit); the MAS (uie-mas-hosted) and Praxis patterns were confirmed
# against live logs on bm-cluster (2026-10-23 and 2026-10-26, mas image
# a80e09316564) -- see the comments in
# parse_line and scripts/README.md for the routine-noise exclusions and the
# request_id caveats. Tokens are never printed by design — audit lines and
# cards carry only short SHA-256 fingerprints (fp:…) that identify a token
# without being usable as one. Cards are stamped with the POD'S OWN
# timestamp (converted to local time); a leading ~ means the line carried
# no timestamp and the monitor's receipt time was used instead — cross-hop
# order between a ~ card and stamped cards is only approximate, because
# each hop is a separate log pipeline with its own delivery latency.
#
# See scripts/README.md for selectors, overrides, and troubleshooting tips.

set -euo pipefail

SINCE="${MONITOR_SINCE:-0s}"
FILTER_REQ="${REQUEST_ID:-}"
MODE="all"
# Cross-hop scratch state (what ACME last dispatched). Followers run as
# separate pipelines, so globals cannot cross hops; a tiny file can.
# MONITOR_STATE_FILE lets you pin it (or inspect it) manually.
STATE_FILE="${MONITOR_STATE_FILE:-$(mktemp "${TMPDIR:-/tmp}/acme-monitor.XXXXXX")}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --acme)   MODE=acme; shift ;;
    --mas)    MODE=mas; shift ;;
    --worker) MODE=worker; shift ;;
    --praxis) MODE=praxis; shift ;;
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
NS_MAS="${NS_MAS:-mao}"
NS_WORKER="${NS_WORKER:-mao}"
# Needs cluster-admin to read; not part of the default join for that reason.
NS_API="${NS_API:-openshift-kube-apiserver}"

SEL_ACME="${SEL_ACME:-app.kubernetes.io/name=acme-agent}"
SEL_PRAXIS="${SEL_PRAXIS:-app.kubernetes.io/name=praxis-proxy}"
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
═══════════════════════════════════════════════════════════════════════════════
  IDENTITY FLOW MONITOR — ACME · MAS · Praxis · Keycloak
═══════════════════════════════════════════════════════════════════════════════

  Full request (one chat message; standalone Phase-3 routing):

  [Browser / ACME UI]  ← unauthenticated Route (lab assumption)
       │  A2A message/send
       ▼
  ┌─ ACME-AGENT ────────────────────────────────────────────────────────────┐
  │  1. LiteLLM (static LITELLM_API_KEY) — route to mas_orchestrator        │
  │  2. MINT TOKEN A —                                                      │
  │     · Keycloak: SPIFFE JWT-SVID → client_credentials                    │
  │       azp=acme-agent  aud=mas-a2a  groups=acme-…-rca                    │
  │     · or Red Hat SSO prod/stage agent token (aud=api.ask_red_hat)       │
  │  3. POST to MAO A2A endpoint  Authorization: Bearer TOKEN A             │
  │     → logs acme_audit … outcome=sent token=fp:… source=…               │
  │  3b. On 401 / TASK_STATE_AUTH_REQUIRED: mint fresh Token A, resend     │
  │     → logs acme_audit … "renew Token A" renewed_from=fp:… token=fp:…   │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  TOKEN A (unchanged)
  ┌─ MAO A2A ENDPOINT (mao/mas-a2a) ────────────────────────────────────────┐
  │  4. Validate TOKEN A — multi-issuer OIDC: Keycloak aud=mas-a2a,         │
  │     Red Hat SSO prod/stage aud=api.ask_red_hat (sig/iss/exp/aud)        │
  │  5. Principal allow-list: sub ∈ a2a.allowedPrincipals (else 401/403)    │
  │  6. NO exchange — Token A kept in RunContext.credentials (passthrough)  │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  TOKEN A (unchanged)
  ┌─ MAS ORCHESTRATOR (mao/mas-worker via Temporal) ────────────────────────┐
  │  7. Blueprint run — LLM plans which specialist agent(s) to call         │
  │  8. Outbound A2A with RunContext.auth_token = TOKEN A:                  │
  │     · downstream agents via Praxis (routing)                            │
  │     · Ask Red Hat direct (access.stage.redhat.com)                      │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  TOKEN A (unchanged)
  ┌─ PRAXIS-PROXY ──────────────────────────────────────────────────────────┐
  │  9. Policy filter: validate TOKEN A + azp allow-list (else deny)        │
  │     Route to the configured downstream backend (fail-closed)            │
  │     (raise logLevel / RUST_LOG for routing + JWT detail)                │
  └─────────────────────────────────────────────────────────────────────────┘

  Join one request:
    REQUEST_ID=<id> bash scripts/monitor.sh
    (per-hop ids only: MAS mints its own request ids -- see
     scripts/README.md "Correlating hops" for time-window correlation)

  Legend:  dispatch   mint   mas   praxis/jwt   fail
───────────────────────────────────────────────────────────────────────────────
DIAGRAM
}

# Parse one log line into a coloured card when it matches known patterns.
parse_line() {
  local src="$1" line="$2" ts
  # Prefer the POD'S OWN timestamp from the line over the monitor's
  # receipt time: each hop is a separate `oc logs -f` pipeline whose
  # delivery latency differs, so receipt-time ordering interleaves
  # same-second events from different hops arbitrarily (observed live:
  # a MAS probe-miss card landing before the ACME dispatch that caused
  # it). Stamps taken from the line keep cross-hop order truthful.
  # Pod stamps are UTC and are converted to local time; "~" marks the
  # fallback: no timestamp in the line, receipt time only.
  ts="~$(date +%H:%M:%S)"
  local stamp
  stamp=""
  if [[ "$line" =~ ([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[.0-9]*Z?) ]]; then
    stamp="${BASH_REMATCH[1]}"
  elif [[ "$line" =~ ([0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]][0-9]{2}:[0-9]{2}:[0-9]{2}) ]]; then
    # Python asctime-style, no timezone.
    stamp="${BASH_REMATCH[1]}"
  fi
  if [[ -n "$stamp" ]]; then
    # Pods log in UTC (with or without an explicit Z); convert to the
    # monitor's local time so all stamps — and the ~ receipt fallback —
    # share one clock.
    [[ "$stamp" != *Z ]] && stamp="${stamp}Z"
    ts=$(date -d "$stamp" +%H:%M:%S 2>/dev/null || echo "${stamp:11:8}")
  fi

  # Optional REQUEST_ID filter
  if [[ -n "$FILTER_REQ" ]] && ! grep -qF "$FILTER_REQ" <<<"$line"; then
    return 0
  fi

  # Strip ANSI colour escapes (Praxis's tracing output embeds them around
  # field names) so the patterns below see clean names/values.
  line=$(sed 's/\x1b\[[0-9;]*m//g' <<<"$line")

  # ── ACME structured audit ───────────────────────────────────────────────
  if [[ "$line" == *"acme_audit "* ]]; then
    local rid action outcome token source renewed_from reason token_exp
    rid=$(grep -oP 'request_id=\K[^ ]+' <<<"$line" || true)
    action=$(grep -oP 'action=\K("[^"]*"|[^ ]+)' <<<"$line" || true)
    outcome=$(grep -oP 'outcome=\K[^ ]+' <<<"$line" || true)
    token=$(grep -oP ' token=\K\S+' <<<"$line" || true)
    source=$(grep -oP 'source=\K\S+' <<<"$line" || true)
    renewed_from=$(grep -oP 'renewed_from=\K\S+' <<<"$line" || true)
    reason=$(grep -oP 'reason=\K.*?(?= renewed_from=|$)' <<<"$line" || true)
    token_exp=$(grep -oP ' exp=\K\S+' <<<"$line" || true)

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

    printf "\n${C}┌─ ACME DISPATCH [${ts}]${N}\n"
    printf "${C}│${N} ${D}CREATOR : Keycloak or Red Hat SSO (Token A)${N}\n"
    printf "${C}│${N} ${D}NEXT    : MAO A2A endpoint (MAS orchestrator)${N}\n"
    printf "${C}│${N} request_id : ${W}${rid}${N}\n"
    if [[ -n "$token" ]]; then
      if [[ "$source" == "pasted_override" ]]; then
        printf "${C}│${N} token      : ${Y}${token}${N} ${Y}(UI-pasted override — not a minted Token A)${N}\n"
      else
        printf "${C}│${N} token      : ${W}${token}${N} ${D}(minted Token A)${N}\n"
      fi
      if [[ -n "$token_exp" && "$token_exp" != "-" ]]; then
        # exp is decoded unverified from the JWT payload by ACME purely for
        # this display; an exp in the past explains a following 401 without
        # guessing between expiry / signature / audience.
        local exp_epoch exp_color="$G" exp_note="still valid at dispatch time"
        exp_epoch=$(date -u -d "$token_exp" +%s 2>/dev/null || echo 0)
        if [[ "$exp_epoch" -gt 0 && "$exp_epoch" -lt "$(date -u +%s)" ]]; then
          exp_color="$R"; exp_note="EXPIRED — a downstream 401 on this token is expected"
        fi
        printf "${C}│${N} expires    : ${exp_color}${token_exp}${N} ${D}(${exp_note})${N}\n"
      fi
    fi
    printf "${C}│${N} action     : ${action}\n"
    printf "${C}│${N} outcome    : ${G}${outcome}${N}\n"
    printf "${C}└─ → follow Token A: mas-a2a validation → orchestrator${N}\n"
    # Remember what went out (state file — followers run in separate
    # pipelines, so shell globals do not cross hops): the MAS TOKEN
    # REJECTED card uses this to say whether the refused bearer was a
    # pasted override.
    printf 'dispatch_source=%s\ndispatch_fp=%s\n' "$source" "$token" > "$STATE_FILE" 2>/dev/null || true
    return 0
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
      local rej_src
      rej_src=$(grep '^dispatch_source=' "$STATE_FILE" 2>/dev/null | cut -d= -f2 || true)
      printf "\n${R}┌─ MAS TOKEN REJECTED [${ts}]${N}\n"
      printf "${R}│${N} ${D}no configured issuer validated the bearer — this is a TOKEN${N}\n"
      printf "${R}│${N} ${D}problem (signature / issuer / expiry / audience), NOT the${N}\n"
      printf "${R}│${N} ${D}principal allow-list (that rejects with 403, after validation)${N}\n"
      if [[ "$rej_src" == "pasted_override" ]]; then
        printf "${R}│${N} ${D}the preceding ACME DISPATCH carried the UI-pasted override —${N}\n"
        printf "${R}│${N} ${D}check its expires line: an exp in the past is the usual cause${N}\n"
      fi
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
      elif [[ "$m_status" == "401" ]]; then
        printf "${M}└─ 401 = the TOKEN failed issuer validation (sig/iss/exp/aud) — not${N}\n"
        printf "${M}   the principal allow-list, which rejects with 403 after validation${N}\n"
      elif [[ "$m_status" == "403" ]]; then
        printf "${M}└─ 403 = token validated but sub is not in a2a.allowedPrincipals${N}\n"
      else
        printf "${M}└─ ${m_color}${m_status}${N}\n"
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
      printf "${Y}└─ routed agents go via Praxis · Ask Red Hat is direct${N}\n"
      return 0
    fi
    if [[ "$line" == *"a2a.delegated"* ]]; then
      local d_node
      d_node=$(grep -oP "'node_uid': '\K[^']+" <<<"$line" || true)
      printf "\n${Y}┌─ MAS DELEGATED [${ts}]${N}\n"
      printf "${Y}│${N} ${D}node_uid=${d_node} — work item dispatched to the remote agent${N}\n"
      printf "${Y}└─ logs at stream completion — line up with PRAXIS ROUTE by timestamp${N}\n"
      return 0
    fi
    # Card only the tool.execute line that carries tool_args (the routing
    # decision the LLM made); MAS also logs the bare tool name and
    # phase-timing tables, which would duplicate this card several times per
    # delegation.
    if [[ "$line" == *"workplan.plan_and_delegate"* ]] && [[ "$line" == *"tool_args"* ]]; then
      printf "\n${Y}┌─ ORCHESTRATOR PLAN [${ts}]${N}\n"
      printf "${Y}│${N} ${D}workplan.plan_and_delegate — LLM routes to its specialist agents${N}\n"
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
      printf "${R}│${N} ${D}outbound A2A rejected — usually Praxis fail-closed on${N}\n"
      printf "${R}│${N} ${D}an unknown route or unallow-listed caller${N}\n"
      printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${R}└─ Token A was fine; the routing decision was not${N}\n"
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

  # ── OCP API server: bearer-token OIDC rejects (needs --api + cluster-admin) ─
  if [[ "$src" == "api" ]]; then
    if grep -qiE 'unable to authenticate|invalid bearer token|oidc' <<<"$line"; then
      printf "\n${R}┌─ API TOKEN REJECT [${ts}]${N}\n"
      printf "${R}│${N} ${D}the API server's OIDC authenticator rejected a bearer token${N}\n"
      printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${R}└─ check Authentication audiences + issuer/JWKS${N}\n"
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

  # ── Auth failures (any hop) ─────────────────────────────────────────────
  if grep -qE 'A2A inbound token rejected|A2A request rejected|A2A query failed|SpiffeIdentityError|CERTIFICATE_VERIFY_FAILED' <<<"$line"; then
    printf "\n${R}┌─ AUTH FAILURE [${ts}] (${src})${N}\n"
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
      printf "${Y}│${N} ${D}no backend matched this request — Praxis fail-closes (no default${N}\n"
      printf "${Y}│${N} ${D}backend)${N}\n"
      printf "${Y}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${Y}└─ Token A was forwarded but never reached a downstream backend${N}\n"
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
          printf "${R}│${N} ${D}filter=${p_filter} — request denied before any downstream backend${N}\n"
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
  rm -f "$STATE_FILE" 2>/dev/null || true
  jobs -p 2>/dev/null | xargs -r kill 2>/dev/null || true
}
trap cleanup EXIT INT TERM

require_oc
print_diagram

echo -e "${D}since=${SINCE}  mode=${MODE}  request_id filter=${FILTER_REQ:-none}${N}"
echo -e "${D}Tokens are never printed; each card names the token stage it observed.${N}"
echo -e "${D}Tip: for Praxis JWT/routing detail set praxis-proxy logLevel (RUST_LOG), e.g.${N}"
echo -e "${D}  info,praxis_filter=debug,praxis_policy_plugin_identity_jwt=debug${N}"
echo -e "${D}Tip: Keycloak Admin → Realm Events filtered by client acme-agent${N}"
echo -e "${D}Tip: final RBAC hop — confirm the mapped group (needs cluster-admin):${N}"
echo -e "${D}  oc auth can-i get pods --as-group=keycloak:acme-agent-rca -n <namespace>${N}"
echo -e "${D}Tip: bash scripts/monitor.sh --api also tails the API server's token rejects${N}"
echo -e "${D}Docs: scripts/README.md  ·  agents/AUTHENTICATION.md${N}"
echo

case "$MODE" in
  acme)   follow acme   "$NS_ACME"   "$SEL_ACME" ;;
  mas)    follow mas    "$NS_MAS"    "$SEL_MAS" ;;
  worker) follow worker "$NS_WORKER" "$SEL_WORKER" ;;
  praxis) follow praxis "$NS_PRAXIS" "$SEL_PRAXIS" ;;
  api)    follow api    "$NS_API"    "$SEL_API" ;;
  join|all)
    # --api is deliberately not in the default join: it needs cluster-admin
    # and the API hop is normally observed via downstream deny cards instead.
    follow acme   "$NS_ACME"   "$SEL_ACME"   &
    follow mas    "$NS_MAS"    "$SEL_MAS"    &
    follow worker "$NS_WORKER" "$SEL_WORKER" &
    follow praxis "$NS_PRAXIS" "$SEL_PRAXIS" &
    wait
    ;;
esac
