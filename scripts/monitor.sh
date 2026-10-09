#!/usr/bin/env bash
# Real-time identity/delegation flow monitor for multicloud-gitops
# (ACME → Praxis → OLS A2A → Keycloak RFC 8693 → MCP → API)
#
# Usage:
#   bash scripts/monitor.sh              # live stream (all hops)
#   bash scripts/monitor.sh --acme       # ACME only
#   bash scripts/monitor.sh --ols        # OLS A2A only
#   bash scripts/monitor.sh --praxis     # Praxis only (raise logLevel first)
#   bash scripts/monitor.sh --mcp        # MCP only
#   bash scripts/monitor.sh --join       # all hops (same as default)
#   bash scripts/monitor.sh --since 10m  # oc --since (default: 0s = now)
#   REQUEST_ID=abc123 bash scripts/monitor.sh --join  # filter one request
#
# Unlike agentic-partners-integration/scripts/monitor.sh this uses `oc logs`
# (no Docker/Postgres audit_events). Join ACME↔OLS on request_id.
#
# See scripts/README.md for selectors, overrides, and troubleshooting tips.

set -euo pipefail

SINCE="${MONITOR_SINCE:-0s}"
FILTER_REQ="${REQUEST_ID:-}"
MODE="all"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --acme)   MODE=acme; shift ;;
    --ols)    MODE=ols; shift ;;
    --praxis) MODE=praxis; shift ;;
    --mcp)    MODE=mcp; shift ;;
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
      sed -n '2,20p' "$0"
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

SEL_ACME="${SEL_ACME:-app.kubernetes.io/name=acme-agent}"
SEL_PRAXIS="${SEL_PRAXIS:-app.kubernetes.io/name=praxis-proxy}"
# Verify live: oc get pods -n a2a-lightspeed --show-labels
SEL_OLS="${SEL_OLS:-app.kubernetes.io/component=application-server}"
# Chart-confirmed MCP labels (AUTHENTICATION.md's component=mcp-server may be stale)
SEL_MCP="${SEL_MCP:-app=openshift-mcp-server}"

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
  IDENTITY FLOW MONITOR — ACME · Praxis · OLS · Keycloak · MCP · API
═══════════════════════════════════════════════════════════════════════════════

  Full request (one chat message that targets a cluster URL):

  [Browser / ACME UI]  ← unauthenticated Route (lab assumption)
       │  A2A message/send  (must include apiURL from global.olsClusters)
       ▼
  ┌─ ACME-AGENT ─────────────────────────────────────────────────────────────┐
  │  1. ClusterRoutingMiddleware — allow-list / derive X-OLS-Cluster id      │
  │  2. LiteLLM (static LITELLM_API_KEY) — route to openshift_lightspeed     │
  │  3. SPIFFE JWT-SVID (ZTWIM) → Keycloak client_credentials → Token A      │
  │     Token A: azp=acme-agent, aud includes openshift-lightspeed           │
  │  4. POST via Praxis  Authorization: Bearer Token A                       │
  │     Headers: X-OLS-Cluster, X-Request-Id  → logs acme_audit … outcome=sent│
  └──────────────────────────────────────────────────────────────────────────┘
       ▼
  ┌─ PRAXIS-PROXY ───────────────────────────────────────────────────────────┐
  │  5. JWT validate (JWKS) + APL allow-list on claim.azp                    │
  │  6. Route by X-OLS-Cluster → OLS upstream                                │
  │     (raise logLevel / RUST_LOG for JWT deny detail)                      │
  └──────────────────────────────────────────────────────────────────────────┘
       ▼
  ┌─ OLS A2A (lightspeed-app-server) ───────────────────────────────────────┐
  │  7. Validate Token A (sig, aud, azp ∈ inbound allow-list, cluster hdr)   │
  │  8. SPIFFE → Keycloak RFC 8693 exchange (subject_token=Token A)          │
  │     → Token B: azp=lightspeed-mcp, aud=openshift-mcp, sub unchanged      │
  │  9. MCP tools with Token B; API server OIDC+RBAC as caller groups        │
  │     → logs a2a_audit … on_behalf_of=acme-agent outcome=ok|error          │
  └──────────────────────────────────────────────────────────────────────────┘

  Join one request:
    grep request_id=<id> across acme-agent + a2a-lightspeed logs
    or: REQUEST_ID=<id> bash scripts/monitor.sh

  Legend:  dispatch   exchange   praxis/jwt   ols   fail
───────────────────────────────────────────────────────────────────────────────
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

  # ── ACME structured audit ───────────────────────────────────────────────
  if [[ "$line" == *"acme_audit "* ]]; then
    local rid cluster action outcome
    rid=$(grep -oP 'request_id=\K[^ ]+' <<<"$line" || true)
    cluster=$(grep -oP 'cluster=\K[^ ]+' <<<"$line" || true)
    action=$(grep -oP 'action=\K("[^"]*"|[^ ]+)' <<<"$line" || true)
    outcome=$(grep -oP 'outcome=\K[^ ]+' <<<"$line" || true)
    printf "\n${C}┌─ ACME DISPATCH [${ts}]${N}\n"
    printf "${C}│${N} ${D}CREATOR : Keycloak (Token A, client_credentials + SPIFFE)${N}\n"
    printf "${C}│${N} ${D}NEXT    : Praxis → OLS A2A${N}\n"
    printf "${C}│${N} request_id : ${W}${rid}${N}\n"
    printf "${C}│${N} cluster    : ${W}${cluster}${N}\n"
    printf "${C}│${N} action     : ${action}\n"
    printf "${C}│${N} outcome    : ${G}${outcome}${N}\n"
    printf "${C}└─ → look for a2a_audit request_id=${rid}${N}\n"
    return 0
  fi

  # ── OLS structured audit ────────────────────────────────────────────────
  if [[ "$line" == *"a2a_audit "* ]]; then
    local rid actor obo sub cluster action outcome task
    local oc_color icon
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
    printf "\n${M}┌─ OLS A2A [${ts}] ${oc_color}${icon}${N}\n"
    printf "${M}│${N} ${D}ACTOR   : ${actor} (RFC 8693 exchange client)${N}\n"
    printf "${M}│${N} ${D}OBO     : ${obo} (Token A azp)${N}\n"
    printf "${M}│${N} ${D}SUBJECT : ${sub}${N}\n"
    printf "${M}│${N} request_id : ${W}${rid}${N}\n"
    printf "${M}│${N} cluster    : ${cluster}\n"
    printf "${M}│${N} task       : ${D}${task}${N}\n"
    printf "${M}│${N} action     : ${action}\n"
    printf "${M}│${N} outcome    : ${oc_color}${outcome}${N}\n"
    printf "${M}└─ Token B used for MCP (never logged)${N}\n"
    return 0
  fi

  # ── Auth / exchange failures ────────────────────────────────────────────
  if grep -qE 'A2A inbound token rejected|A2A token exchange failed|A2A request rejected|A2A query failed|SpiffeIdentityError|A2AWorkloadIdentityError|ClusterURLError|CERTIFICATE_VERIFY_FAILED' <<<"$line"; then
    printf "\n${R}┌─ AUTH/EXCHANGE FAILURE [${ts}] (${src})${N}\n"
    printf "${R}│${N} $(trunc "$line" 160)\n"
    printf "${R}└─ see agents/AUTHENTICATION.md Troubleshooting${N}\n"
    return 0
  fi

  # ── Praxis (best-effort; needs elevated RUST_LOG) ───────────────────────
  if [[ "$src" == "praxis" ]]; then
    if grep -qiE 'deny|reject|unauthorized|jwt|forbidden|403|401' <<<"$line"; then
      printf "\n${B}┌─ PRAXIS [${ts}]${N}\n"
      printf "${B}│${N} $(trunc "$line" 160)\n"
      printf "${B}└─${N}\n"
      return 0
    fi
    # Quiet by default when logLevel is empty — skip noise
    return 0
  fi

  # ── MCP / API denials (passthrough — real authz is API server) ──────────
  if [[ "$src" == "mcp" ]] && grep -qiE '401|403|Unauthorized|Forbidden|forbidden' <<<"$line"; then
    printf "\n${Y}┌─ MCP/API DENY [${ts}]${N}\n"
    printf "${Y}│${N} ${D}MCP is passthrough; this is usually API-server RBAC/OIDC on Token B${N}\n"
    printf "${Y}│${N} $(trunc "$line" 160)\n"
    printf "${Y}└─ check groups on Token B + lightspeed-mcp-rbac${N}\n"
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
echo -e "${D}Tip: for Praxis JWT detail set praxis-proxy logLevel, e.g.${N}"
echo -e "${D}  info,praxis_filter=debug,praxis_policy_plugin_identity_jwt=debug${N}"
echo -e "${D}Tip: Keycloak Admin → Realm Events filtered by clients acme-agent, lightspeed-mcp${N}"
echo -e "${D}Docs: scripts/README.md  ·  agents/AUTHENTICATION.md${N}"
echo

case "$MODE" in
  acme)   follow acme   "$NS_ACME"   "$SEL_ACME" ;;
  ols)    follow ols    "$NS_OLS"    "$SEL_OLS" ;;
  praxis) follow praxis "$NS_PRAXIS" "$SEL_PRAXIS" ;;
  mcp)    follow mcp    "$NS_OLS"    "$SEL_MCP" ;;
  join|all)
    follow acme   "$NS_ACME"   "$SEL_ACME"   &
    follow ols    "$NS_OLS"    "$SEL_OLS"    &
    follow praxis "$NS_PRAXIS" "$SEL_PRAXIS" &
    follow mcp    "$NS_OLS"    "$SEL_MCP"    &
    wait
    ;;
esac
