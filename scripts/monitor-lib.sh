#!/usr/bin/env bash
# Real-time identity/delegation flow monitor for multicloud-gitops.
#
# Follows the caller's token through every stage of the agentic chain, and
# answers three questions per hop: WHO is acting, WHAT identity it wears, and
# WHAT it did on whose behalf.
#
# acme-agent ─A─▶ MAO A2A endpoint ─A─▶ MAS orchestrator ─A─▶ praxis HUB
# gateway
# ─H─▶ lightspeed-hub A2A adapter ──▶ hub kube API (AgenticRun as the caller)
#     ─R─▶ praxis SPOKE gateway ─U─▶ spoke kube API
#
#   Token chain (survives across hops; see agents/AUTHENTICATION.md):
#     A  acme-agent         → aud=mas-a2a               MAS + hub gateway
#     H  mas-a2a            → aud=lightspeed-hub-a2a    adapter + hub kube API
#     R  lightspeed-hub-a2a → aud=praxis-spoke-gateway  spoke gateways only
#     U  praxis-spoke-gateway → aud=praxis-spoke-<id>   spoke kube API
#   Every token keeps the SAME sub: one principal, re-scoped per hop.
#
# Usage:
# bash scripts/monitor.sh # live stream (all tailable hops + Keycloak)
#   bash scripts/monitor.sh --acme         # ACME only
#   bash scripts/monitor.sh --mas          # MAO A2A endpoint only (mao ns)
#   bash scripts/monitor.sh --worker       # MAS orchestrator (Temporal worker)
# bash scripts/monitor.sh --praxis # Praxis: hub gateway + spoke gateways
# bash scripts/monitor.sh --egress # Praxis egress gateways (external agents)
# bash scripts/monitor.sh --hub # lightspeed-hub adapter + agentic operator
# bash scripts/monitor.sh --kc # Keycloak realm events only (AUTHORITATIVE)
#   bash scripts/monitor.sh --api          # OCP API server token rejects
#   bash scripts/monitor.sh --join         # all hops except --api (default)
#   bash scripts/monitor.sh --since 10m    # oc --since (default: 0s = now)
#   bash scripts/monitor.sh --no-kc        # skip the Keycloak event feed
#   bash scripts/monitor.sh --endpoint     # print the hop map and exit
#   REQUEST_ID=abc123 bash scripts/monitor.sh --join  # filter one request
#
# Unlike agentic-partners-integration/scripts/monitor.sh this uses `oc logs`
# (no Docker/Postgres audit_events). ACME emits structured audit lines
# (acme_audit, with actor= and fp:… token fingerprints); the MAS
# (uie-mas-hosted) and Praxis patterns were confirmed against live logs on
# bm-cluster (2026-10-23 and 2026-10-26, mas image a80e09316564). The
# lightspeed-hub adapter/operator patterns are marked "UNCONFIRMED WATCHER"
# until replayed against a live lightspeed-hub build -- see parse_line and
# scripts/README.md. The Keycloak feed (`--kc`) is different: it reads realm
# admin events, which are the signed record of every exchange, so WHO mints
# WHAT for WHOM is authoritative even when a pod's own matcher misses.
#
# Tokens are never printed by design — audit lines and cards carry only short
# SHA-256 fingerprints (fp:…) that identify a token without being usable as
# one. Cards are stamped with the POD'S OWN timestamp (converted to local
# time); a leading ~ means the line carried no timestamp and the monitor's
# receipt time was used instead — cross-hop order between a ~ card and stamped
# cards is only approximate, because each hop is a separate log pipeline with
# its own delivery latency.
#
# See scripts/README.md for selectors, overrides, and troubleshooting tips.

# shellcheck shell=bash
# UTF-8 so the box-drawing cards survive a C/POSIX locale.
export LC_ALL=C.UTF-8
set -euo pipefail
# Library mode: source scripts/monitor-lib.sh (MONITOR_LIB=1) to unit-test the
# parser without touching a cluster; tests/scripts/test_monitor.py does this.
MONITOR_LIB="${MONITOR_LIB:-0}"

# `--since=0s` does NOT mean "now" — kubectl/oc treat it as "no limit" and
# replay every retained log line, burying the live flow under history. 1s
# means "newer than one second": attach from now.
SINCE="${MONITOR_SINCE:-1s}"
FILTER_REQ="${REQUEST_ID:-}"
MODE="all"
WANT_KC=1
# Cross-hop scratch state (what ACME dispatched, token labels seen so far,
# which fp was exchanged into what). Followers run as separate pipelines, so
# globals cannot cross hops; a tiny file can.
# MONITOR_STATE_FILE lets you pin it (or inspect it) manually.
STATE_FILE="${MONITOR_STATE_FILE:-$(mktemp "${TMPDIR:-/tmp}/acme-monitor.XXXXXX")}"
# Keycloak admin events are polled in short windows so the cards stay live.
KC_POLL_SECONDS="${MONITOR_KC_POLL:-3}"

# ── Keycloak realm events (the authoritative exchange record) ───────────────
# KC_URL empty derives https://keycloak.<cluster domain> from the current
# kubeconfig; KC_CA verifies a private-CA Route (recommended), otherwise
# -k is used so a fresh cluster still shows its grants. The admin credential
# is the Secret the keycloak-oidc chart already maintains
# (keycloak-admin-user).
KC_REALM="${KC_REALM:-rca}"
KC_ADMIN_SECRET="${KC_ADMIN_SECRET:-keycloak-admin-user}"
KC_ADMIN_USER_KEY="${KC_ADMIN_USER_KEY:-username}"
KC_ADMIN_PASS_KEY="${KC_ADMIN_PASS_KEY:-password}"
KC_URL="${KC_URL:-}"
KC_CA="${KC_CA:-}"


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

# ── namespaces / selectors (from values-standalone + chart labels)
# ────────────
NS_ACME="${NS_ACME:-acme-agent}"
NS_PRAXIS="${NS_PRAXIS:-praxis-proxy}"
NS_MAS="${NS_MAS:-mao}"
NS_WORKER="${NS_WORKER:-mao}"
NS_HUB="${NS_HUB:-lightspeed-hub}"
NS_OPERATOR="${NS_OPERATOR:-lightspeed-hub}"
NS_KC="${NS_KC:-keycloak-system}"
# Needs cluster-admin to read; not part of the default join for that reason.
NS_API="${NS_API:-openshift-kube-apiserver}"

SEL_ACME="${SEL_ACME:-app.kubernetes.io/name=acme-agent}"
SEL_PRAXIS="${SEL_PRAXIS:-app.kubernetes.io/name=praxis-proxy}"
# External-IdP egress gateways (praxis-proxy egressGateways, one per external
# agent). They carry their own app label, so the praxis follower never sees
# them; the gateway key is taken from the pod name (praxis-egress-<key>-…).
NS_EGRESS="${NS_EGRESS:-praxis-proxy}"
SEL_EGRESS="${SEL_EGRESS:-app.kubernetes.io/name=praxis-egress-gateway}"
# Verified live (bm-cluster): charts/all/mao render
# app.kubernetes.io/name=<chart>.
# mas-a2a renders no pods at all until a2a.enabled=true -- a missing hop is
# skipped with a warning.
SEL_MAS="${SEL_MAS:-app.kubernetes.io/name=mas-a2a}"
SEL_WORKER="${SEL_WORKER:-app.kubernetes.io/name=mas-worker}"
# lightspeed-hub chart: the adapter Deployment is named <fullname> and the
# agentic-operator Deployment <fullname>-agentic-operator (see
# templates/agentic-operator.yaml + tests/chart/test_lightspeed_hub_chart.py).
# The adapter selector is an EXACT name so the operator's pod never lands in
# the adapter stream (unlike the mao charts, they do not share a name label).
SEL_HUB="${SEL_HUB:-app.kubernetes.io/name=lightspeed-hub}"
SEL_OPERATOR="${SEL_OPERATOR:-app.kubernetes.io/name=lightspeed-hub-agentic-operator}"
# Verified live (bm-cluster): the static pods carry
# app=openshift-kube-apiserver
SEL_API="${SEL_API:-app=openshift-kube-apiserver}"
# Spoke gateways are identified per line from the pod name (praxis-spoke-<id>),
# not by a separate selector: one deployment per spoke, same chart labels.

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

# ── cross-hop state: the running picture of the chain ───────────────────────
# Keys are grouped by prefix so an observer can dump the whole world:
# fp_<hex> what token a fingerprint is (A/H/R/U, retired, unclassified)
#   tok_<LABEL>      latest fingerprint seen for A/H/R/U
#   subject          the caller's sub, as soon as a hop reports it
#   chain_seq        how many requests/observations have started
#   last_mas_rid     MAS's own id for the newest request (dedupe for chain_seq)
#   ex_<n> / ex_count  the n-th exchange (from→to, client, audience, hop)
#   ex_seen_<X>_to_<Y>  that exchange was already carded (pod vs Keycloak)
#   fwd_since_<X>    token X has moved on (kept for diagnostics)
#   run_name/run_created  the AgenticRun a card named
#   dispatch_source/dispatch_fp  what ACME last sent
# The file is touched by parallel followers; writes are single-line appends
# (atomic enough for lines < PIPE_BUF), readers take the last value seen.
S="$STATE_FILE"
kv_get() { grep -m1 "^$1=" "$S" 2>/dev/null | cut -d= -f2- || true; }
kv_set() {
  local k="$1"; shift
  printf '%s=%s\n' "$k" "$*" >>"$S" 2>/dev/null || true
}
# The token a card is about ("A", "H", …), so a long stream stays readable at
# a glance; the card body says what identity that token carries.
token_tag() { printf '%s' "${1:-}"; }

# Was this exchange already carded (by a pod log), so a later Keycloak event
# is a corroboration rather than a second card for the same grant?
exchange_seen() { [[ "$(kv_get "ex_seen_${1//>/_to_}")" == "1" ]]; }
mark_exchange_seen() { kv_set "ex_seen_${1//>/_to_}" "1"; }

# An exchange (A→H, H→R, R→U) updates the chain state and emits the card from
# wherever it was observed. `via` is the hop that performed it; that is the
# "who did this" of the whole update.
record_exchange() {
  local from="$1" to="$2" client="$3" audience="$4" via="$5" ts="$6" sub="${7:-}" fp_from="${8:-}" fp_to="${9:-}"
  local n
  n=$(( $(kv_get ex_count) + 1 ))
  kv_set "ex_count" "$n"
  kv_set "ex_${n}" "${from}->${to} by ${client} aud ${audience} @ ${via} ${ts}"
  [[ -n "$fp_from" ]] && kv_set "tok_${from}" "${fp_from#fp:}"
  [[ -n "$fp_to" ]] && kv_set "tok_${to}" "${fp_to#fp:}"
  [[ -n "$sub" ]] && kv_set "subject" "$sub"
  # Remember that X has been swapped out: from here on, any hop that still
  # presents X is a chain break worth flagging (see stale_token_check).
  [[ "$from" != "refused" ]] && kv_set "fwd_since_${from}" "1"
  mark_exchange_seen "${from}>${to}"
  # Who performed the exchange is also who the *next* token authenticates as.
  printf "\n${M}┌─ TOKEN EXCHANGE ${from} ──▶ ${to}  [${ts}]${N} ${D}(${via})${N}\n"
  sub="${sub:-$(kv_get subject)}"
  if [[ -n "$sub" ]]; then
    printf "${M}│${N} ${D}SUBJECT :${N} ${W}${sub}${N} ${D}(unchanged by the exchange — same principal)${N}\n"
  else
    printf "${M}│${N} ${D}SUBJECT :${N} ${D}the caller's sub — unchanged; Keycloak (--kc) records it${N}\n"
  fi
  printf "${M}│${N} ${D}CLIENT  :${N} ${W}${client}${N} ${D}(authenticates as this; ${to}'s azp)${N}\n"
  printf "${M}│${N} ${D}AUDIENCE:${N} ${W}${audience}${N} ${D}(${to}'s aud — names the next hop that may consume it)${N}\n"
  if [[ -n "$fp_from" || -n "$fp_to" ]]; then
    printf "${M}│${N} ${D}TOKENS  :${N} ${from} ${D}${fp_from:-fp:?}${N} ${D}──▶${N} ${to} ${D}${fp_to:-fp:?}${N}\n"
  fi
  # For a Keycloak-sourced card, show the wire shape of the grant so the three
  # exchanges can be compared with the events (same subject, audience, client).
  if [[ "$via" == Keycloak* ]]; then
    printf "${M}│${N} ${D}GRANT   :${N} ${D}POST /protocol/openid-connect/token · grant_type=token-exchange · subject_token=${from} · audience=${audience}${N}\n"
  fi
  printf "${M}└─ On behalf of the subject throughout; the actor is now ${client}${N}\n"
}

# The same exchange can be observed twice — once in a pod's log and once in
# Keycloak's authoritative event. Either order is handled in the callers:
# a pod card after Keycloak's is reduced to a one-line reference, and a
# Keycloak card after a pod card becomes a corroboration line (see
# parse_praxis / parse_adapter / parse_kc_event).
#
# Did an identity arrive at a hop still wearing a token that should already
# have been exchanged? Two directions, both meaningful:
#   STALE  — the exchange into this token was seen, but an earlier token is
#            still being presented past its boundary (e.g. A at the adapter)
#   AHEAD  — a DOWNSTREAM token is presented where an earlier one belongs
#            (e.g. H on the ACME → MAS leg) — cannot be produced by this chain
# Both mean "the identity shown here is not who this hop should be acting as".
# ── hop identity checks ────────────────────────────────────────────────────
# Every hop below has one credential it may legitimately see. The monitor
# states which one it is showing, and — when the token can be identified by
# fingerprint — confirms it against that expectation instead of assuming it.
# Outcomes:
#   OK       the fingerprint is the hop's expected token
#   ALERT    it is a token this hop must never see (A at the adapter, H at a
#            spoke gateway, …) — check_ip also prints SUSPECT/IMPOSSIBLE
#   UNKNOWN  a bearer is present but no hop has labelled that fingerprint
#            (the token was never minted, exchanged or recorded by Keycloak:
#            treat as suspicious, exactly like an unlabelled "valid-looking"
#            token at a gateway)
hop_expects() {
  case "$1" in
    acme|mas|worker) echo A ;;
    hubgw)           echo A ;;
    adapter)         echo H ;;
    spoke)           echo R ;;
    kubeapi-hub)     echo H ;;
    kubeapi-spoke)   echo U ;;
    *)               echo "" ;;
  esac
}

# A readable name for the token a hop is verified against (H, U, …).
hp_name() {
  case "$1" in
    hubgw) echo "A (caller token)";;
    adapter) echo "H (run creation)";;
    spoke) echo "R (run credential)";;
    kubeapi-hub) echo "H (run creation)";;
    kubeapi-spoke) echo "U (spoke reads)";;
  esac
}

# check_ip SRC TS [fp] — prints the hop's IDENTITY and VERIFY lines and
# returns a status in IP_STATUS (ok|alert|unknown|none). Deliberately makes no
# claim about `sub`: values like "the caller's sub" are descriptions, and
# treating them as evidence is how a wrong template substitution hid (the pod
# log carried an object dump instead of the subject, and nothing flagged it).
check_ip() {
  local src="$1" ts="$2" fp="${3:-}" expect label tname
  expect=$(hop_expects "$src"); tname=$(hp_name "$src")
  IP_STATUS="none"
  if [[ -z "$fp" ]]; then
    [[ -n "$expect" ]] && printf "${W}│${N} ${D}IDENTITY:${N} ${W}${expect}${N} ${D}— ${tname} is what this hop may see${N}\n"
    return 0
  fi
  label=$(kv_get "fp_${fp}")
  printf "${W}│${N} ${D}IDENTITY:${N} ${W}${expect}${N} ${D}fp:${fp} — ${tname} is what this hop may see${N}\n"
  if [[ -z "$label" ]]; then
    IP_STATUS="unknown"
    printf "${R}│${N} ${D}VERIFY  :${N} ${R}UNIDENTIFIED — see SUSPECT card below${N}\n"
    return 0
  fi
  if [[ "$label" == "retired" ]]; then
    IP_STATUS="alert"
    printf "${R}│${N} ${D}VERIFY  :${N} ${R}RETIRED token — it was superseded by a renewal${N}\n"
    return 0
  fi
  if [[ "$label" == "$expect" ]]; then
    IP_STATUS="ok"
    printf "${G}│${N} ${D}VERIFY  :${N} ${G}matches the hop — this is ${expect}${N}\n"
  else
    IP_STATUS="alert"
    printf "${R}│${N} ${D}VERIFY  :${N} ${R}MISMATCH — this is ${label}, the hop expects ${expect}${N}\n"
  fi
  return 0
}

# The SUSPECT/IMPOSSIBLE card for a verified-later check. `status` is the
# check_ip result; only alert/unknown print.
flag_identity() {
  local src="$1" ts="$2" fp="${3:-}" status="${4:-}" expect label
  expect=$(hop_expects "$src")
  [[ -z "$status" || "$status" == "ok" || "$status" == "none" ]] && return 0
  label=$(kv_get "fp_${fp}")
  local hop="hub"
  [[ "$src" == spoke ]] && hop="spoke"
  case "$src:$label" in
    adapter:A|spoke:A|spoke:H)
      stale_token_check "$label" "$hop" "$ts"
      return 0 ;;
  esac
  if [[ "$status" == "unknown" ]]; then
    printf "\n${R}┌─ SUSPECT: UNIDENTIFIED BEARER AT ${src^^} [${ts}]${N}\n"
    printf "${R}│${N} ${D}a token reached this hop that no hop labelled: not a caller${N}\n"
    printf "${R}│${N} ${D}mint the monitor saw, not an exchange Keycloak recorded.${N}\n"
    printf "${R}└─ compare the fingerprint above with ACME's acme_audit / --kc${N}\n"
    return 0
  fi
  printf "\n${R}┌─ SUSPECT: TOKEN MISMATCH AT ${src^^} [${ts}]${N}\n"
  printf "${R}│${N} ${D}seen ${W}${label}${N} ${D}where ${W}${expect}${N} ${D}is expected (fp:… above).${N}\n"
  printf "${R}└─ a token crossed this hop without being exchanged for it${N}\n"
}

stale_token_check() {
  local tok="$1" hop="$2" ts="$3"
  # A is already dead by the time H/R/U exist; H expected but seeing A is the
  # interesting case, and it is only a finding once the chain has gone past A.
  case "$hop:$tok" in
    acme:H|acme:R|acme:U|mas:R|mas:U|worker:R|worker:U)
      printf "\n${R}┌─ IMPOSSIBLE: TOKEN ${tok} ON THE ${hop^^} LEG [${ts}]${N}\n"
      printf "${R}│${N} ${D}${tok} cannot exist here: it is minted later in the chain,${N}\n"
      printf "${R}│${N} ${D}and only by a hop this leg does not involve.${N}\n"
      printf "${R}└─ treat as a forged/replayed token, not a chain event${N}\n"
      return 0 ;;
    hub:A)
      # The contract is absolute, not conditional: whatever ExchangeAtPraxis
      # is set to, the ADAPTER only ever receives H. A (or a valid A's
      # fingerprint) at this hop means the A→H exchange did not happen, so the
      # caller's original token travelled past its boundary.
      printf "\n${R}┌─ SUSPECT: A STILL WORN AT THE ADAPTER [${ts}]${N}\n"
      printf "${R}│${N} ${D}the hub gateway exchanges A→H; the adapter must never see A.${N}\n"
      printf "${R}│${N} ${D}a valid A reaching the adapter means the caller's original${N}\n"
      printf "${R}│${N} ${D}identity travelled past its boundary (no exchange happened).${N}\n"
      printf "${R}└─ check a2aUpstream.exchangeAtPraxis / the delegator's A→H log${N}\n" ;;
    spoke:H|spoke:A)
      printf "\n${R}┌─ SUSPECT: ${tok} AT THE SPOKE GATEWAY [${ts}]${N}\n"
      printf "${R}│${N} ${D}the spoke gateway accepts only R (aud praxis-spoke-gateway).${N}\n"
      printf "${R}│${N} ${D}${tok} here is either a misconfiguration or a replay.${N}\n"
      printf "${R}└─ expect PRAXIS REJECT (policy/azp) right after this${N}\n" ;;
    kubeapi:*)
      case "$tok" in
        A|R)
          printf "\n${R}┌─ SUSPECT: TOKEN ${tok} AT A KUBE API [${ts}]${N}\n"
          printf "${R}│${N} ${D}A and R are rejected by design at every kube API; seeing${N}\n"
          printf "${R}│${N} ${D}this means a hop forwarded the wrong credential.${N}\n"
          printf "${R}└─ a 401 is EXPECTED here; treat it as a finding only if unexpected${N}\n" ;;
      esac ;;
  esac
  return 0
}


print_diagram() {
  clear 2>/dev/null || true
  cat << 'DIAGRAM'
═══════════════════════════════════════════════════════════════════════════════
  IDENTITY FLOW MONITOR — ACME · MAS · Praxis · lightspeed-hub · Keycloak
═══════════════════════════════════════════════════════════════════════════════

  One chat message. Each token's aud names the client that exchanges or
  consumes it NEXT; sub is unchanged all the way to the kube API.

  [Browser / ACME UI]  ← unauthenticated Route (lab assumption)
       │  A2A message/send
       ▼
  ┌─ ACME-AGENT ────────────────────────────────────────────────────────────┐
  │  1. MINT TOKEN A — Keycloak client_credentials + SPIFFE JWT-SVID        │
  │     azp=acme-agent  aud=mas-a2a  groups=acme-agent-rca                  │
  │     (one A for every destination; no Red Hat SSO token needed)          │
  │  2. POST to MAO A2A endpoint  Bearer A  (+ X-Request-Id)                │
  │  2b. On 401 / AUTH_REQUIRED: mint a fresh A and resend THE SAME task    │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  A
  ┌─ MAO A2A ENDPOINT (mao/mas-a2a) ────────────────────────────────────────┐
  │  3. VALIDATE A — multi-issuer OIDC (sig/iss/exp/aud)                    │
  │  4. Principal allow-list: sub ∈ a2a.allowedPrincipals (403 otherwise)   │
  │  5. NO EXCHANGE — A sealed into RunContext for the workflow             │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  A
  ┌─ MAS ORCHESTRATOR (mao/mas-worker via Temporal) ────────────────────────┐
  │  6. LLM plans; the delegated A2A call forwards A verbatim               │
  │     (external agents: the same A goes to praxis-egress-<agent>)         │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  A
  ┌─ PRAXIS HUB GATEWAY (praxis-proxy) ─────────────────────────────────────┐
  │  7. VALIDATE A (sig/iss/aud + azp allow-list) and route by target       │
  │  8. EXCHANGE A→H as client mas-a2a, audience lightspeed-hub-a2a         │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  H
  ┌─ HUB A2A ADAPTER (lightspeed-hub) ──────────────────────────────────────┐
  │  9.  CREATE AgenticRun with H — hub API sees keycloak:<sub>             │
  │  10. EXCHANGE H→R as client lightspeed-hub-a2a (aud praxis-spoke-       │
  │      gateway, 1800 s) → Secret ls-run-token-<uid>                       │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  R  (mounted by the agentic operator; it itself never exchanges)
  ┌─ AGENTIC OPERATOR + SANDBOX (lightspeed-hub) ───────────────────────────┐
  │      gateway credential mode: sandbox kubeconfig tokenFile = R          │
  │      no spoke ServiceAccount, no spoke kubeconfig, no exchange          │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  R
  ┌─ PRAXIS SPOKE GATEWAY (praxis-spoke-<id>) ──────────────────────────────┐
  │  11. VALIDATE R (aud praxis-spoke-gateway, azp lightspeed-hub-a2a)      │
  │  12. EXCHANGE R→U per call as client praxis-spoke-gateway, for the      │
  │      target spoke only: aud praxis-spoke-hub / praxis-spoke-mario-ipi   │
  └─────────────────────────────────────────────────────────────────────────┘
       ▼  U   (one per registered spoke; the target comes from X-Hub-Cluster)
  ┌─ SPOKE KUBE API ────────────────────────────────────────────────────────┐
  │  13. user keycloak:<sub>, group keycloak:praxis-spoke-<id>:reader       │
  │      → cluster-reader / cluster-monitoring-view  (or 403)               │
  └─────────────────────────────────────────────────────────────────────────┘

        ▲  A   (external agents — Ask Red Hat, partners — reached from MAS)
  ┌─ PRAXIS EGRESS GATEWAY (praxis-egress-<agent>, from MAS) ───────────────┐
  │  E1. VALIDATE A (local Keycloak only) + azp allow-list                  │
  │  E2. SERVE the agent card from config (url → the gateway itself)        │
  │  E3. MINT the agent's IdP token as the gateway's own client             │
  │      (client_credentials: it speaks for that client, not the caller)    │
  │  E4. FORWARD to the fixed agent endpoint (Host rewritten)               │
  └─────────────────────────────────────────────────────────────────────────┘

  Cards carry the token they observed (A/H/R/U) and, for exchanges, the
  client that performed it and the audience it asked for. Where a hop logs
  the bearer, IDENTITY/VERIFY names the token and checks its fingerprint
  against the only token that hop may see; a mismatch, an unlabelled bearer
  or a token past its exchange is flagged SUSPECT — that is a broken chain.

  Join one request:
    REQUEST_ID=<id> bash scripts/monitor.sh
    (ACME's X-Request-Id stops at MAS; later hops mint their own ids --
     see scripts/README.md "Correlating hops" and use --kc for exchanges)

  Legend:  dispatch   mint   exchange   mas   praxis/jwt   kc event   fail
───────────────────────────────────────────────────────────────────────────────
DIAGRAM
}

# ── per-line metadata (runs BEFORE the card is printed) ─────────────────────
# Labels added to a card must describe the state at the moment the card is
# emitted. These two bookkeeping steps used to run mid-card (after the header
# was printed), so a new request's first card showed the PREVIOUS request's
# number and an exchange card could show the fingerprint of the token it had
# not produced yet. Doing it first is what makes the labels trustworthy.
metadata_prepass() {
  local src="$1" line="$2" rid
  case "$src" in
    mas)
      if [[ "$line" == *"A2A HTTP request completed"* ]]; then
        rid=$(grep -m1 -oP "'request_id': '\K[^']+" <<<"$line" || true)
        [[ "$(grep -m1 -oP "'status_code': \K[0-9]+" <<<"$line" || true)" =~ ^2 ]] || return 0
        [[ -z "$rid" || "$rid" == "$(kv_get last_mas_rid)" ]] && return 0
        kv_set last_mas_rid "$rid"
        # One new observation. With MONITOR_CHAIN_MODE=1 every request after
        # the first keeps the same number (the old single-chain behaviour).
        if [[ "${MONITOR_CHAIN_MODE:-0}" == "1" && "$(kv_get chain_seq)" != "" ]]; then
          :
        else
          kv_set chain_seq $(( $(kv_get chain_seq) + 1 ))
        fi
      fi
      ;;
    acme)
      # A renewal replaces A for the same principal: retire the old fingerprint
      # label so a later sighting of the refused token is not reported as
      # "still A". The new fingerprint is labelled by the renewal card itself.
      local from newfp oldfp
      from=$(grep -m1 -oP 'renewed_from=fp:\K[0-9a-f]+' <<<"$line" || true)
      newfp=$(grep -m1 -oP 'token=fp:\K[0-9a-f]+' <<<"$line" || true)
      if [[ -n "$from" && -n "$newfp" && "$from" != "$newfp" ]]; then
        oldfp=$(kv_get "fp_$from")
        kv_set "fp_$from" "retired"
        [[ -n "$oldfp" ]] && kv_set "fp_token_$from" "retired"
      fi
      ;;
  esac
  # A bearer seen in a line is identified by its fingerprint once the monitor
  # has labelled it somewhere. The label is NOT inferred here: a fingerprint
  # first seen at a hop is only a hypothesis until ACME's mint, a Keycloak
  # event or an exchange card names the token. Tagging it "unclassified" and
  # then reading any non-empty tag as "consistent with this hop" is how an
  # A-at-the-adapter regression hid inside a card that claimed to check it.
  return 0
}

# ── classification of one log line ──────────────────────────────────────────
# `src` is the follower's name; for the Praxis follower the pod name decides
# whether the line came from the hub gateway or a spoke gateway, and
# `line` is what `oc logs --prefix` produced ("[ns/pod:container] text") or
# the raw line when the prefix is absent.
parse_line() {
  local src="$1" line="$2" ts
  # The --prefix only annotates lines; it is not part of the program's output,
  # and it contains the pod name (the ONLY way to tell the hub gateway from a
  # spoke gateway when both share one selector). Strip it here, once, so no
  # matcher below has to know about it.
  local pod="" hop="" container=""
  if [[ "$line" =~ ^\[([^][]*)\][[:space:]]?(.*)$ ]]; then
    local tag="${BASH_REMATCH[1]}"
    line="${BASH_REMATCH[2]}"
    if [[ "$tag" == pod/*/* ]]; then
      # `oc logs -l … --prefix` on oc 4.19 prints "pod/<pod>/<container>"
      # (verified live 2026-11-27). The old split below then took the
      # CONTAINER for the pod, so every spoke read as spoke "gateway".
      local rest="${tag#pod/}"
      pod="${rest%%/*}"
      container="${rest#*/}"
    else
      pod="${tag##*/}"          # "ns/pod:container" -> "pod:container"
      [[ "$pod" == *:* ]] && container="${pod#*:}"
      pod="${pod%%:*}"          # -> "pod"
    fi
  fi
  # The container a prefixed line came from (the prefix itself is stripped
  # here, so matchers that care -- the DNS sidecar -- read this instead).
  PFX_CONTAINER="$container"
  if [[ "$src" == "egress" && -n "$pod" ]]; then
    hop=$(egress_key_from_pod "$pod")
  fi
  if [[ "$src" == "praxis" ]]; then
    if [[ "$pod" == praxis-spoke-* ]]; then
      hop="spoke/${pod#praxis-spoke-}"
    elif [[ -n "$pod" ]]; then
      hop="hub"
    fi
  fi

  # Colour codes must be gone before ANY matcher or the stamp runs — Praxis
  # wraps even its leading timestamp in escapes ("^<esc>[2m2026-…"), so an
  # anchored timestamp regex run before this strip never matched and every
  # praxis card fell back to "~" receipt time.
  line=$(sed 's/\x1b\[[0-9;]*m//g' <<<"$line")

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
  # Pod timestamps lead the line. Anchoring matters: ACME's dispatch line
  # carries the token's exp= (an ISO date) later on, and an unanchored match
  # stamped the card with the EXPIRY rather than the event time.
  if [[ "$line" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[.0-9]*Z?) ]]; then
    stamp="${BASH_REMATCH[1]}"
  elif [[ "$line" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]][0-9]{2}:[0-9]{2}:[0-9]{2}([.,][0-9]+)?) ]]; then
    # Python logging style ("2026-11-20 10:00:01,120"), no timezone. Keep the
    # comma form parseable too: date(1) wants a dot.
    stamp="${BASH_REMATCH[1]}"
  fi
  if [[ -n "$stamp" ]]; then
    # Pods log in UTC (with or without an explicit Z); convert to the
    # monitor's local time so all stamps — and the ~ receipt fallback —
    # share one clock.
    [[ "$stamp" == *T* && "$stamp" != *Z ]] && stamp="${stamp}Z"
    ts=$(date -d "$stamp" +%H:%M:%S 2>/dev/null || echo "${stamp:11:8}")
  fi

  # Optional REQUEST_ID filter (before any parsing, so a filter never
  # half-cards a multi-line record).
  if [[ -n "$FILTER_REQ" ]] && ! grep -qF "$FILTER_REQ" <<<"$line"; then
    return 0
  fi
  metadata_prepass "$src" "$line"
  # Which log stream is this, from the identity model's point of view? The
  # caller names ACME/MAS/worker; Praxis is split by pod into hubgw/spoke;
  # the API follower is split by the pod/container it saw the line in.
  local hsrc="$src"
  case "$src" in
    praxis) [[ "$hop" == spoke/* ]] && hsrc="spoke" || hsrc="hubgw" ;;
    egress) hsrc="egress" ;;
    hub)    hsrc="adapter" ;;
    api)    case "$pod" in *kube-apiserver*) hsrc="kubeapi-hub" ;; esac ;;
  esac

  # ── ACME structured audit ───────────────────────────────────────────────
  if [[ "$line" == *"acme_audit "* ]]; then
    parse_acme "$line" "$ts"
    return 0
  fi

  # ── Keycloak realm events: the signed record of every exchange ──────────
  if [[ "$src" == "kc" ]]; then
    parse_kc_event "$line" "$ts"
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
    parse_mas "$line" "$ts"
    return 0
  fi

  # ── MAS orchestrator (Temporal worker): planning + Token A passthrough ───
  if [[ "$src" == "worker" ]]; then
    parse_worker "$line" "$ts"
    return 0
  fi

  # ── Praxis: the hub gateway exchanges A→H, spoke gateways exchange R→U ───
  if [[ "$src" == "praxis" ]]; then
    parse_praxis "$line" "$ts" "$hop"
    return 0
  fi

  # ── Praxis egress gateways: Token A in, the agent's own IdP token out ─────
  if [[ "$src" == "egress" ]]; then
    parse_egress "$line" "$ts" "$hop"
    return 0
  fi

  # ── lightspeed-hub: A2A adapter (H→R) and agentic operator (no exchange) ─
  if [[ "$src" == "hub" ]]; then
    parse_adapter "$line" "$ts"
    # Token-verification cards are the primary output for this hop: any
    # bearer the adapter logs is reported (H expected) even when the rest of
    # the line matches nothing else.
    [[ -n "${ADAPTER_FP:-}" ]] && report_adapter_identity "$ts" "$ADAPTER_FP"
    return 0
  fi
  if [[ "$src" == "operator" ]]; then
    parse_operator "$line" "$ts"
    return 0
  fi

  # ── run-token Secret watch (see parse_runcred) ──────────────────────────
  if [[ "$src" == "runcred" ]]; then
    parse_runcred "$line" "$ts"
    return 0
  fi

  # ── OCP API server: bearer-token OIDC rejects (needs --api + cluster-admin)
  # ─
  if [[ "$src" == "api" ]]; then
    parse_api "$line" "$ts"
    return 0
  fi
}

# A JWT in the line, per ACME's rule: the token is `Bearer <jwt>`; the
# fingerprint is the first 12 hex of sha256, which is what acme_audit prints.
# This is display-only; the token itself is never echoed.
jwt_fingerprint_in() {
  local line="$1" tok
  tok=$(grep -oP 'Bearer\s+\K[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+' <<<"$line" | head -1 || true)
  [[ -z "$tok" ]] && return 0
  printf '%s' "$tok" | sha256sum | cut -c1-12
}

# ── ACME (acme_audit) ────────────────────────────────────────────────────────
# Fields per line: request_id, actor, action, outcome; mint lines add
# expires_in + token=fp:…; dispatch lines add token, source, exp; renewal
# lines add reason, renewed_from, token, override.
parse_acme() {
  local line="$1" ts="$2"
  local rid action actor outcome token source renewed_from reason token_exp
  rid=$(grep -oP 'request_id=\K[^ ]+' <<<"$line" || true)
  actor=$(grep -oP ' actor=\K[^ ]+' <<<"$line" || true)
  action=$(grep -oP 'action=\K("[^"]*"|[^ ]+)' <<<"$line" || true)
  outcome=$(grep -oP 'outcome=\K[^ ]+' <<<"$line" || true)
  token=$(grep -oP ' token=\K\S+' <<<"$line" || true)
  source=$(grep -oP ' source=\K\S+' <<<"$line" || true)
  renewed_from=$(grep -oP 'renewed_from=\K\S+' <<<"$line" || true)
  # renewal.py reasons carry spaces ("task auth-required"): capture up to the
  # next key=value pair, not just the first word.
  reason=$(grep -oP ' reason=\K.*?(?= \w+=|$)' <<<"$line" || true)
  token_exp=$(grep -oP ' exp=\K\S+' <<<"$line" || true)

  # A renewal is not a mint-to-dispatch cycle: the SAME request is retried
  # with a fresh token after a downstream refusal (401 / AUTH_REQUIRED).
  if [[ "$action" == *'renew Token A'* ]]; then
    if [[ "$outcome" == "ok" && -n "$token" ]]; then
      # A renewal is NOT an exchange: same client, same audience, same
      # principal — a brand-new A. Update the A fingerprint and retire the
      # refused one so a later sighting is not mislabelled as "still A".
      kv_set "tok_A" "${token#fp:}"
      kv_set "fp_${token#fp:}" "A"
      if [[ -n "$renewed_from" ]]; then
        kv_set "fp_${renewed_from#fp:}" "retired"
        kv_set "fp_token_${renewed_from#fp:}" "retired"
      fi
    fi
    printf "\n${Y}┌─ ACME TOKEN RENEWAL [${ts}]${N} ${D}$(token_tag A)${N}\n"
    if [[ "$outcome" == "ok" ]]; then
      # renewal.py has exactly two causes: "HTTP 401" (a real downstream
      # refusal) and "task auth-required" (MAS paused the ONGOING task because
      # the credential is nearly invalid — not a refusal; the follow-up POST
      # resumes the same A2A task). One text for both made a routine
      # mid-task renewal look like a rejected request.
      local cause="downstream refused the current token"
      local tail="resends the SAME request on the fresh A; sub is unchanged"
      if [[ "$reason" == *auth-required* ]]; then
        cause="MAS paused the ongoing task (credential nearly invalid)"
        tail="resumes the SAME task on the fresh A; sub is unchanged — not a new request"
      fi
      printf "${Y}│${N} ${D}WHO     :${N} ACME ${D}mints a fresh A as itself (same workload identity)${N}\n"
      printf "${Y}│${N} ${D}REASON  :${N} ${reason:-?} ${D}— ${cause}${N}\n"
      printf "${Y}│${N} ${D}TOKEN   :${N} ${renewed_from:-?} ${D}──▶${N} ${W}${token:-?}${N} ${D}(old → fresh)${N}\n"
      printf "${Y}└─ ${tail}${N}\n"
      check_ip acme "$ts" "${token#fp:}"
      flag_identity acme "$ts" "${token#fp:}" "$IP_STATUS"
    else
      printf "${Y}│${N} ${D}WHO     :${N} ACME ${D}could not mint a replacement (${outcome})${N}\n"
      printf "${Y}│${N} ${D}REASON  :${N} ${reason:-?}${N}\n"
      printf "${Y}└─ the original refusal will propagate to the user${N}\n"
    fi
    return 0
  fi

  if [[ "$action" == *'mint'*'Token A'* ]]; then
    local label="TOKEN A MINT" creator="Keycloak client_credentials + SPIFFE JWT-SVID (client_assertion)"
    if [[ "$action" == *'Red Hat SSO'* ]]; then
      label="TOKEN A MINT (Red Hat SSO)"
      creator="Red Hat SSO client_credentials (confidential client)"
    fi
    local color="$M"
    if [[ "$outcome" == "ok" && -n "$token" ]]; then
      kv_set "fp_${token#fp:}" "A"
      kv_set "tok_A" "${token#fp:}"
    fi
    [[ "$outcome" != "ok" ]] && color="$R"
    printf "\n${M}┌─ ${label} [${ts}]${N} ${D}$(token_tag A)${N}\n"
    local ip="none"
    printf "${M}│${N} ${D}WHO     :${N} ACME / ${W}${actor:-acme-agent}${N} ${D}(the caller's workload identity)${N}\n"
    if [[ "$outcome" == "ok" ]]; then
      check_ip acme "$ts" "${token#fp:}"; ip="$IP_STATUS"
      printf "${M}│${N} ${D}CLAIMS  :${N} ${D}azp=${W}acme-agent${N} ${D}·${N} aud=${W}mas-a2a${N}\n"
      printf "${M}│${N} ${D}EXCHANGE:${N} ${D}n/a — A is the first token; nothing is traded to mint it${N}\n"
    fi
    printf "${M}│${N} ${D}CREATOR : ${creator}${N}\n"
    printf "${M}│${N} request_id : ${W}${rid}${N}\n"
    printf "${M}│${N} token      : ${W}${token:-?}${N} ${D}(fingerprint only)${N}\n"
    printf "${M}│${N} outcome    : ${color}${outcome}${N}\n"
    if [[ "$outcome" == "ok" ]]; then
      printf "${M}└─ A is good for MAS and the hub gateway, nowhere else${N}\n"
      flag_identity acme "$ts" "${token#fp:}" "$ip"
    else
      printf "${M}└─ no Token A — dispatch will fail${N}\n"
    fi
    return 0
  fi

  if [[ "$action" == *'dispatch'* ]]; then
    local color="$C" fp="${token#fp:}"
    if [[ -n "$token" && "$source" != "pasted_override" ]]; then
      kv_set "fp_$fp" "A"; kv_set "tok_A" "$fp"
    fi
    printf "\n${C}┌─ ACME DISPATCH [${ts}]${N} ${D}$(token_tag A)${N}\n"
    printf "${C}│${N} ${D}WHO     :${N} ACME / ${W}${actor:-acme-agent}${N} ${D}on behalf of${N} ${W}the caller${N} ${D}(A's sub)${N}\n"
    printf "${C}│${N} ${D}NEXT    :${N} ${D}MAO A2A endpoint (Token A, aud=mas-a2a)${N}\n"
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
    printf "${C}│${N} ${D}EXCHANGE: none at this hop — MAS forwards A verbatim${N}\n"
    check_ip acme "$ts" "$fp"; local ip="$IP_STATUS"
    printf "${C}│${N} action     : ${action}\n"
    printf "${C}│${N} outcome    : ${color}${outcome}${N}\n"
    printf "${C}└─ expect next: MAS validates A; hub gateway exchanges A→H${N}\n"
    # Remember what went out (state file — followers run in separate
    # pipelines, so shell globals do not cross hops): the MAS TOKEN
    # REJECTED card uses this to say whether the refused bearer was a
    # pasted override.
    kv_set dispatch_source "$source"
    kv_set dispatch_fp "$token"
    flag_identity acme "$ts" "$fp" "$ip"
    return 0
  fi

  # Unknown acme_audit action: show the raw line so a new audit kind is never
  # silently invisible.
  printf "\n${C}┌─ ACME AUDIT [${ts}]${N}\n"
  printf "${C}│${N} %s\n" "$(trunc "$line" 180)"
  printf "${C}└─${N}\n"
}

# ── MAO A2A endpoint (mas-a2a) ───────────────────────────────────────────────
parse_mas() {
  local line="$1" ts="$2"
  if grep -q 'Failed to resolve signing key' <<<"$line"; then
    local miss_kid
    miss_kid=$(grep -oP 'matches: "\K[^"]+' <<<"$line" || true)
    printf "${D}┄─ mas issuer probe miss [${ts}] kid=${miss_kid} — benign: next issuer's JWKS is tried (multi-issuer OIDC)${N}\n"
    return 0
  fi
  # Agent-card / probe requests are public and not part of the chain.
  if grep -qE '\.well-known/agent-card|/readyz|/healthz' <<<"$line"; then
    return 0
  fi
  if grep -q 'Token validation failed for all' <<<"$line"; then
    local rej_src
    rej_src=$(kv_get dispatch_source)
    printf "\n${R}┌─ MAS TOKEN REJECTED [${ts}]${N} ${D}$(token_tag A)${N}\n"
    printf "${R}│${N} ${D}WHO     :${N} bearer presented to the front door, refused by every issuer${N}\n"
    printf "${R}│${N} ${D}GATE    :${N} multi-issuer OIDC — signature / issuer / expiry / audience${N}\n"
    printf "${R}│${N} ${D}NOT the principal allow-list (that rejects with 403 after validation)${N}\n"
    if [[ "$rej_src" == "pasted_override" ]]; then
      printf "${R}│${N} ${D}the preceding ACME DISPATCH carried the UI-pasted override —${N}\n"
      printf "${R}│${N} ${D}check its expires line: an exp in the past is the usual cause${N}\n"
    fi
    printf "${R}│${N} ${D}→ expect HTTP 401; ACME mints a fresh A and resends (TOKEN RENEWAL)${N}\n"
    printf "${R}└─ a UI-pasted token dying here is exactly how the override is tested${N}\n"
    return 0
  fi
  # The renewal handshake MAS opens when a credential is (nearly) invalid: the
  # task pauses in AUTH_REQUIRED and MAS waits for the CALLER to send a fresh
  # token on the same task. This is the MAS-side half of ACME's TOKEN RENEWAL.
  if grep -qiE 'AUTH_REQUIRED|auth.required' <<<"$line"; then
    printf "\n${Y}┌─ MAS AWAITING FRESH CREDENTIAL [${ts}]${N} ${D}$(token_tag A)${N}\n"
    printf "${Y}│${N} ${D}WHO     :${N} the workflow pauses; MAS does NOT mint its own token${N}\n"
    printf "${Y}│${N} ${D}EXCHANGE:${N} ${D}none — the CALLER must present a fresh A on the same task${N}\n"
    printf "${Y}│${N} ${D}ACME then mints + resends (ACME TOKEN RENEWAL → new fp for A)${N}\n"
    printf "${Y}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${Y}└─ same task id: renewals must not start a second run on the hub${N}\n"
    return 0
  fi
  if grep -qE 'already exists\. Ignoring task replacement|Successfully fetched agent card data|token validator initialized|Starting A2A ASGI server|bootstrap\.container' <<<"$line"; then
    return 0
  fi
  if [[ "$line" == *"A2A HTTP request completed"* ]]; then
    local m_rid m_method m_path m_status m_dur m_color fp
    m_rid=$(grep -oP "'request_id': '\K[^']+" <<<"$line" || true)
    m_method=$(grep -oP "'method': '\K[^']+" <<<"$line" || true)
    m_path=$(grep -oP "'path': '\K[^']+" <<<"$line" || true)
    m_status=$(grep -oP "'status_code': \K[0-9]+" <<<"$line" || true)
    m_dur=$(grep -oP "'duration_ms': \K[0-9.]+" <<<"$line" || true)
    m_color="$G"
    [[ "$m_status" =~ ^[45] ]] && m_color="$R"
    local m_stream
    m_stream=$(grep -m1 -oP "stream=\K[^ ']+" <<<"$line" || true)
    printf "\n${M}┌─ MAS A2A INBOUND [${ts}] ${m_color}${m_status}${N} ${D}$(token_tag A)${N}\n"
    printf "${M}│${N} ${D}${m_method} ${m_path} (${m_dur}ms) — front door carrying Token A${N}\n"
    [[ -n "$m_stream" ]] && printf "${M}│${N} ${D}STREAM  :${N} ${D}served by ${W}${m_stream}${N} ${D}(a2a.allowedPrincipals is read at process startup)${N}\n"
    printf "${M}│${N} ${D}EXCHANGE:${N} ${D}none — validated, then sealed into RunContext${N}\n"
    local m_sub
    m_sub=$(grep -m1 -oP "'(sub|user_id)': '\K[^']+" <<<"$line" || true)
    [[ -n "$m_sub" ]] && kv_set subject "$m_sub"
    printf "${M}│${N} request_id : ${W}${m_rid}${N} ${D}(MAS's own, not ACME's X-Request-Id)${N}\n"
    if [[ -n "$m_sub" ]]; then
      printf "${M}│${N} ${D}SUBJECT :${N} ${W}${m_sub}${N} ${D}(the principal tested against a2a.allowedPrincipals)${N}\n"
    fi
    check_ip mas "$ts"
    if [[ "$m_status" =~ ^2 ]]; then
      printf "${M}└─ A validated + principal allow-listed; orchestrator takes over${N}\n"
    elif [[ "$m_status" == "401" ]]; then
      printf "${M}└─ 401 = TOKEN failed issuer validation (sig/iss/exp/aud) — not${N}\n"
      printf "${M}   the principal allow-list, which rejects with 403 after validation${N}\n"
    elif [[ "$m_status" == "403" ]]; then
      printf "${M}└─ 403 = A validated but principal not allowed${N}\n"
      printf "${M}   ${D}a2a.allowedPrincipals is injected from a Secret and read at process${N}\n"
      printf "${M}   ${D}startup. A synced Secret is NOT a refreshed environment: if the${N}\n"
      printf "${M}   ${D}list changed for this sub, the old list may still be in effect.${N}\n"
      printf "${M}   ${D}Verify the pod's env, then restart MAS A2A only:${N}\n"
      printf "${M}   ${D}  oc -n mao rollout restart deployment/mao-a2a-mas-a2a${N}\n"
    else
      printf "${M}└─ ${m_color}${m_status}${N}\n"
    fi
    return 0
  fi
  if [[ "$line" == *"Submitting session="* ]] && [[ "$line" == *"auth=True"* ]]; then
    local sess
    sess=$(grep -oP 'Submitting session=\K\S+' <<<"$line" || true)
    printf "\n${G}┌─ MAS SESSION START [${ts}]${N} ${D}$(token_tag A)${N}\n"
    printf "${G}│${N} ${D}WHO     :${N} MAS acting for the caller; A attached to the run context${N}\n"
    printf "${G}│${N} ${D}session=${sess} auth=True${N}\n"
    printf "${G}└─ no token is minted or exchanged here; the caller's A travels on${N}\n"
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
}

# ── MAS orchestrator (Temporal worker) ──────────────────────────────────────
parse_worker() {
  local line="$1" ts="$2"
  # temporalio activity payloads echo the whole prompt/workplan dump at
  # DEBUG; they repeat the workplan headers many times per delegation and
  # their arbitrary prompt text trips the keyword matchers below. The
  # meaningful lines (tool.execute, applying_runtime_headers,
  # a2a_agent_node_factory) carry the same information once.
  if grep -q 'temporalio.worker._activity' <<<"$line"; then
    return 0
  fi
  # Multi-line records: only the first line carries the logger name, and any
  # following line begins with whitespace (the --prefix was already removed).
  if [[ "$line" =~ ^[[:space:]] ]]; then
    return 0
  fi
  if [[ "$line" == *"using user auth token"* ]]; then
    local node_url
    node_url=$(grep -oP 'A2A agent node \[\K[^\]]+' <<<"$line" || true)
    printf "\n${G}┌─ TOKEN A PASSTHROUGH [${ts}]${N} ${D}$(token_tag A)${N}\n"
    printf "${G}│${N} ${D}WHO     :${N} mas-worker acting on behalf of the caller (A's sub)${N}\n"
    check_ip worker "$ts"
    printf "${G}│${N} ${D}${node_url}${N}\n"
    printf "${G}│${N} ${D}empty bearer_token: the node uses RunContext.auth_token (A)${N}\n"
    printf "${G}└─ A forwarded verbatim; MAS never exchanges or rewrites it${N}\n"
    return 0
  fi
  if [[ "$line" == *"A2A stream_message to agent="* ]]; then
    local t_agent t_url t_auth
    t_agent=$(grep -oP 'to agent=\K\S+' <<<"$line" || true)
    t_url=$(grep -oP 'url=\K\S+' <<<"$line" || true)
    t_auth=$(grep -oP 'auth=\K\S+' <<<"$line" || true)
    printf "\n${Y}┌─ MAS OUTBOUND A2A [${ts}]${N} ${D}$(token_tag A)${N}\n"
    printf "${Y}│${N} ${D}WHO     :${N} mas-worker acting for the caller, wearing ${W}A${N}\n"
    printf "${Y}│${N} ${D}agent=${t_agent}  auth=${t_auth}${N}\n"
    printf "${Y}│${N} ${D}url=${t_url}${N}\n"
    if grep -q 'praxis-egress-' <<<"$t_url"; then
      local e_key
      e_key=$(grep -oP 'praxis-egress-\K[a-z0-9-]+?(?=\.)' <<<"$t_url" || true)
      printf "${Y}└─ next hop: egress gateway ${W}${e_key:-?}${Y} mints the agent's own IdP token (A stops there)${N}\n"
    elif grep -qi 'praxis' <<<"$t_url"; then
      printf "${Y}└─ next hop exchanges A→H (hub gateway)${N}\n"
    else
      printf "${Y}└─ DIRECT to the agent: it receives A itself (no egress gateway in between)${N}\n"
    fi
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
    printf "${Y}│${N} ${D}WHO     :${N} the orchestrator's own LLM (Vertex ADC) picks the route${N}\n"
    printf "${Y}│${N} ${D}that is a STATIC planning credential, not the caller's identity${N}\n"
    printf "${Y}└─ the chosen delegation still leaves MAS wearing the caller's A${N}\n"
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
    printf "${R}│${N} ${D}an unknown route / unregistered target, or a non-allow-listed azp${N}\n"
    printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${R}└─ A was fine; the routing or the caller allow-list was not${N}\n"
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
  # A context dict can carry an "error" KEY on a successful record (a status
  # snapshot); only the actual level token / traceback means failure.
  if grep -qE '(^|[[:space:]])(ERROR|CRITICAL)([[:space:]]|:)|Traceback' <<<"$line"; then
    printf "\n${R}┌─ ORCHESTRATOR ERROR [${ts}]${N}\n"
    printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${R}└─ see charts/all/mao/mas-worker (logLevel) + uie-mas-hosted docs${N}\n"
    return 0
  fi
  return 0
}

# ── Praxis: hub gateway (A→H) and spoke gateways (R→U) ──────────────────────
# `hop` is "hub" or "spoke/<id>", derived from the pod name by parse_line.
parse_praxis() {
  local line="$1" ts="$2" hop="$3"
  local who="hub"
  SPOKE_FP=""
  [[ "$line" == *"Bearer "* ]] && SPOKE_FP=$(jwt_fingerprint_in "$line")
  [[ "$hop" == spoke/* ]] && who="spoke:${hop#spoke/}"
  # The dnsIPv4Only CoreDNS sidecar shares this pod. Surface only its errors
  # (a failing AAAA-strip/forward resolver breaks the Keycloak/JWKS fetch that
  # the exchange needs); skip its routine output.
  # The sidecar is recognised by the prefix's container (parse_line strips the
  # prefix, so the old in-line '/dns-ipv4only' test never matched).
  if [[ "${PFX_CONTAINER:-}" == dns-ipv4only ]] || grep -q '/dns-ipv4only' <<<"$line"; then
    if grep -qE '\[ERROR\]|SERVFAIL|plugin/errors' <<<"$line"; then
      printf "\n${R}┌─ PRAXIS DNS SIDECAR ERROR [${ts}] ${D}(${who})${N}\n"
      printf "${R}│${N} ${D}AAAA-strip/forward resolver failing — SSO JWKS lookups at risk${N}\n"
      printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${R}└─${N}\n"
    fi
    return 0
  fi
  if grep -qE 'path="?/readiness|path="?/health|agent-card' <<<"$line"; then
    return 0
  fi
  if grep -q 'no route matched' <<<"$line"; then
    printf "\n${Y}┌─ PRAXIS NO ROUTE [${ts}] ${D}(${who})${N}\n"
    if [[ "$who" == hub ]]; then
      printf "${Y}│${N} ${D}no target selected — the %s header is absent or names an${N}\n" "X-Hub-Cluster"
      printf "${Y}│${N} ${D}unregistered cluster (the caller×target allow-list is the${N}\n"
      printf "${Y}│${N} ${D}spokeGateways registry)${N}\n"
    else
      printf "${Y}│${N} ${D}no backend matched this request — Praxis fail-closes (no default)${N}\n"
    fi
    printf "${Y}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${Y}└─ the inbound token was forwarded but never reached a backend${N}\n"
    return 0
  fi
  if grep -q 'route matched' <<<"$line"; then
    local p_path p_cluster
    p_path=$(grep -oP 'route matched path=\K\S+' <<<"$line" || true)
    p_cluster=$(grep -m1 -oP 'cluster=\K\S+' <<<"$line" || true)
    if [[ "$who" == hub ]]; then
      printf "\n${B}┌─ PRAXIS HUB ROUTE [${ts}]${N} ${D}$(token_tag A)${N}\n"
      printf "${B}│${N} ${D}WHO     :${N} MAS acting for the caller, wearing ${W}A${N} ${D}(azp=acme-agent)${N}\n"
      printf "${B}│${N} ${D}path=${p_path} → ${p_cluster:-hub adapter}${N}\n"
      printf "${B}│${N} ${D}EXCHANGE:${N} ${D}delegate() exchanges A→H as client ${W}mas-a2a${N}\n"
      check_ip hubgw "$ts" "$(jwt_fingerprint_in "$line")"
      printf "${B}└─ policy filter accepted A; see TOKEN EXCHANGE A ──▶ H for the grant${N}\n"
    else
      printf "\n${B}┌─ PRAXIS SPOKE ROUTE [${ts}] ${D}(${who})${N}\n"
      printf "${B}│${N} ${D}WHO     :${N} the sandbox, on behalf of the caller (R's sub),${N}\n"
      printf "${B}│${N} ${D}          wearing ${W}R${N} ${D}for spoke ${W}${hop#spoke/}${N}\n"
      printf "${B}│${N} ${D}path=${p_path} → ${p_cluster}${N}\n"
      printf "${B}│${N} ${D}EXCHANGE:${N} ${D}delegate() exchanges R→U as client ${W}praxis-spoke-gateway${N}\n"
      check_ip spoke "$ts" "$SPOKE_FP"
      printf "${B}└─ U's audience is praxis-spoke-${hop#spoke/}: each spoke gets its own token${N}\n"
    fi
    return 0
  fi
  # The hub gateway's A→H exchange failing Keycloak auth is logged by the
  # delegator (2026-11-24: delegation.idp_rejected / unauthorized_client).
  # Catch it before the generic "rejected/denied" line so it is never
  # mistaken for the azp gate.
  if grep -qiE 'delegation\.idp_rejected|unauthorized_client|token exchange rejected' <<<"$line"; then
    printf "\n${R}┌─ PRAXIS EXCHANGE REJECTED [${ts}] ${D}(${who})${N}\n"
    printf "${R}│${N} ${D}STAGE   :${N} ${D}the A→H EXCHANGE failed at Keycloak, not the azp gate${N}\n"
    printf "${R}│${N} %s\n" "$(trunc "$line" 170)"
    printf "${R}│${N} ${D}why     :${N} ${D}--kc shows the TOKEN_EXCHANGE_ERROR for this client${N}\n"
    printf "${R}└─${N}\n"
    return 0
  fi
  # static_response "request rejected … status=200" is the agent card being
  # served (benign); only 4xx/5xx filter rejects are failures.
  if grep -q 'request rejected by filter' <<<"$line"; then
    local p_filter p_status
    p_filter=$(grep -oP 'request rejected by filter filter="\K[^"]+' <<<"$line" || true)
    p_status=$(grep -oP 'status=\K[0-9]+' <<<"$line" || true)
    if [[ "$p_status" =~ ^[45] ]]; then
      printf "\n${R}┌─ PRAXIS REJECT [${ts}] ${R}${p_status}${N} ${D}(${who})${N}\n"
      # Live 2026-11-24: the hub gateway's A→H exchange failing Keycloak auth
      # surfaced here as 403 with delegation.idp_rejected / unauthorized_client,
      # and in Keycloak as TOKEN_EXCHANGE_ERROR mas-a2a
      # invalid_client_credentials — the registered client secret had been
      # stored as a literal $(...) import placeholder.
      if grep -qiE 'delegation\.idp_rejected|unauthorized_client|token exchange rejected' <<<"$line"; then
        printf "${R}│${N} ${D}STAGE   :${N} ${D}the A→H EXCHANGE failed at Keycloak, not the azp gate${N}\n"
        printf "${R}│${N} %s\n" "$(trunc "$line" 170)"
        printf "${R}│${N} ${D}Keycloak says why: ${W}--kc${N} ${D}shows TOKEN_EXCHANGE_ERROR client=mas-a2a${N}\n"
        printf "${R}└─${N}\n"
        return 0
      fi
      if [[ "$p_filter" == "policy" && "$who" == hub ]]; then
        printf "${R}│${N} ${D}policy filter denied — caller JWT invalid/expired, or azp${N}\n"
        printf "${R}│${N} ${D}not in policy.allowedCallers (acme-agent + Red Hat SSO client)${N}\n"
        printf "${R}│${N} ${D}(note: an exchanged token's azp is the EXCHANGER; A is untouched)${N}\n"
      elif [[ "$p_filter" == "policy" ]]; then
        printf "${R}│${N} ${D}policy filter denied at spoke ${W}${hop#spoke/}${N} ${D}— R invalid/expired,${N}\n"
        printf "${R}│${N} ${D}or its azp is not in spokeGatewayConfig.allowedCallers${N}\n"
        printf "${R}│${N} ${D}(R's azp is the ADAPTER's exchanger, lightspeed-hub-a2a)${N}\n"
      else
        printf "${R}│${N} ${D}filter=${p_filter} — request denied before any downstream backend${N}\n"
      fi
      printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${R}└─${N}\n"
    fi
    # A PUBLIC agent-card rejection (the static_response 200 is filtered
    # below) is not a chain event; only 4xx/5xx matter.
    return 0
  fi
  if grep -q 'request rejected by filter' <<<"$line"; then
    return 0
  fi
  # The delegator's own exchange logging. UNCONFIRMED WATCHER: no captured
  # sample of this line exists in this repo, so it is matched narrowly (only
  # lines that name the delegator/exchange AND an audience) and labelled as
  # unconfirmed; the Keycloak feed (--kc) is the authoritative exchange record.
  if grep -qiE 'delegat' <<<"$line" && grep -qiE 'audience' <<<"$line"; then
    local ex_aud ex_client
    ex_aud=$(grep -oiP 'audience[^A-Za-z0-9_-]+\K[A-Za-z0-9_.-]+' <<<"$line" | head -1 || true)
    if [[ "$who" == hub ]]; then
      if exchange_seen "A>H"; then
        printf "${D}┄─ praxis hub gateway: delegator line (exchange A→H already recorded by Keycloak) [${ts}]${N}\n"
        printf "${D}   %s${N}\n" "$(trunc "$line" 150)"
        return 0
      fi
      record_exchange A H "mas-a2a" "${ex_aud:-lightspeed-hub-a2a}" "praxis hub gateway (delegator log, UNCONFIRMED)" "$ts"
    else
      if exchange_seen "R>U"; then
        printf "${D}┄─ praxis spoke gateway ${hop#spoke/}: delegator line (exchange R→U already recorded by Keycloak) [${ts}]${N}\n"
        printf "${D}   %s${N}\n" "$(trunc "$line" 150)"
        return 0
      fi
      record_exchange R U "praxis-spoke-gateway" "${ex_aud:-praxis-spoke-${hop#spoke/}}" "praxis spoke gateway ${hop#spoke/} (delegator log, UNCONFIRMED)" "$ts"
    fi
    printf "${D}│${N} ${D}UNCONFIRMED WATCHER — verbatim delegator line below; confirm a live sample${N}\n"
    printf "${D}│${N} %s\n" "$(trunc "$line" 170)"
    printf "${D}└─${N}\n"
    return 0
  fi
  # Routine identity-plugin internals: the module name contains "jwt", so the
  # old unanchored `jwt` keyword carded every JWKS refresh and claim-map miss
  # as if it were a denial (live: several noise cards per request). Only
  # actual denial vocabulary cards; the plugin's own lines are skipped.
  if grep -qiE 'refreshed JWKS|claim map: no candidate|praxis_policy_plugin_identity_jwt::' <<<"$line"; then
    return 0
  fi
  if grep -qiE 'deny|reject|unauthorized|forbidden|403|401' <<<"$line"; then
    printf "\n${B}┌─ PRAXIS [${ts}] ${D}(${who})${N}\n"
    printf "${B}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${B}└─${N}\n"
    return 0
  fi
  # Quiet by default when logLevel is empty — skip noise, but still verify a
  # bearer this gateway logged on a line no other card matched.
  if [[ "$who" != hub && -n "${SPOKE_FP:-}" ]]; then
    check_ip spoke "$ts" "$SPOKE_FP"
    flag_identity spoke "$ts" "$SPOKE_FP" "$IP_STATUS"
  fi
  return 0
}

# ── Praxis egress gateways (external agents: Ask Red Hat, partners) ─────────
# Patterns CONFIRMED LIVE (bm-cluster, 2026-11-27, ai:0.4.1, the chart's
# logLevel info,praxis_filter=debug,…); replayed from
# tests/scripts/fixtures/egress-gateway-live-2026-11-27.log. What the gateway
# logs, and what it does not:
#  - no "minted" line: the pinned delegator logs no successful mint. But
#    delegate() runs inside the policy filter BEFORE routing and a failed mint
#    denies, so "route matched → upstream selected" proves validate + allow-
#    list + mint all succeeded;
#  - a deny logs only `filter="policy" status=401|403`: the reason
#    (auth.*, not an allow-listed caller, delegation.idp_rejected,
#    delegation.scope_too_broad) is in the response BODY, which the caller
#    (MAS) sees -- never in this log;
#  - neither the caller's sub nor the agent's response status is logged.
#
# Gateway key from the pod name: praxis-egress-<key>-<rs hash>-<pod hash>.
egress_key_from_pod() {
  local pod="${1#praxis-egress-}"
  if [[ "$pod" =~ ^(.+)-[a-z0-9]{6,10}-[a-z0-9]{5}$ ]]; then
    pod="${BASH_REMATCH[1]}"
  fi
  printf '%s' "$pod"
}

parse_egress() {
  local line="$1" ts="$2" key="${3:-?}"
  if [[ "${PFX_CONTAINER:-}" == dns-ipv4only ]]; then
    if grep -qE '\[ERROR\]|SERVFAIL|plugin/errors' <<<"$line"; then
      printf "\n${R}┌─ EGRESS DNS SIDECAR ERROR [${ts}] ${D}(${key})${N}\n"
      printf "${R}│${N} ${D}AAAA-strip/forward resolver failing — JWKS, IdP and agent lookups at risk${N}\n"
      printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
      printf "${R}└─${N}\n"
    fi
    return 0
  fi
  if grep -qE 'path="?/readiness|/healthy' <<<"$line"; then
    return 0
  fi
  # The public card GET is answered from config (static_response 200): that
  # is what keeps MAS on the gateway, so it is worth one dim line.
  if grep -q 'filter="static_response" status=200' <<<"$line"; then
    if grep -q 'agent-card' <<<"$line"; then
      printf "${D}┄─ egress %s: agent card served from config (url = the gateway) [${ts}]${N}\n" "$key"
    fi
    return 0
  fi
  if grep -q 'no route matched' <<<"$line"; then
    local n_path
    n_path=$(grep -oP 'no route matched path=\K\S+' <<<"$line" || true)
    printf "\n${Y}┌─ EGRESS NO ROUTE [${ts}] ${D}(${key})${N}\n"
    printf "${Y}│${N} ${D}path=${n_path} is outside the agent's path prefix — Core 404${N}\n"
    printf "${Y}└─ Token A was valid and a token was minted; nothing was forwarded${N}\n"
    return 0
  fi
  if grep -q 'filter="router" status=404' <<<"$line"; then
    return 0
  fi
  if grep -q 'route matched' <<<"$line"; then
    kv_set "egress_path_${key}" "$(grep -oP 'route matched path=\K\S+' <<<"$line" || true)"
    return 0
  fi
  if grep -q 'upstream selected' <<<"$line"; then
    local u_up u_path
    u_up=$(grep -oP 'upstream=\K\S+' <<<"$line" || true)
    u_path=$(kv_get "egress_path_${key}")
    printf "\n${M}┌─ EGRESS MINT + FORWARD [${ts}]${N} ${D}$(token_tag A) → agent token (${key})${N}\n"
    printf "${M}│${N} ${D}WHO     :${N} MAS acting for the caller, wearing ${W}A${N} ${D}(sub not logged by Praxis)${N}\n"
    printf "${M}│${N} ${D}VERIFY  :${N} ${D}A validated against the local Keycloak + azp allow-list${N}\n"
    printf "${M}│${N} ${D}MINT    :${N} ${D}client_credentials at the agent's IdP as the gateway's client${N}\n"
    printf "${M}│${N} ${D}          → the agent sees ONE account, not the caller${N}\n"
    printf "${M}│${N} ${D}FORWARD :${N} ${W}${u_up}${N}${D}${u_path}${N}\n"
    printf "${M}└─ routing only runs after the policy filter passed: validate + mint succeeded${N}\n"
    return 0
  fi
  if grep -q 'request rejected by filter' <<<"$line"; then
    local r_filter r_status
    r_filter=$(grep -oP 'request rejected by filter filter="\K[^"]+' <<<"$line" || true)
    r_status=$(grep -oP 'status=\K[0-9]+' <<<"$line" || true)
    [[ "$r_status" =~ ^[45] ]] || return 0
    printf "\n${R}┌─ EGRESS REJECT [${ts}] ${R}${r_status}${N} ${D}(${key})${N}\n"
    if [[ "$r_filter" == policy && "$r_status" == 401 ]]; then
      printf "${R}│${N} ${D}Token A missing, malformed or expired — nothing minted${N}\n"
    elif [[ "$r_filter" == policy && "$r_status" == 403 ]]; then
      printf "${R}│${N} ${D}either A's azp is not allow-listed, OR the MINT failed at the agent's IdP${N}\n"
      printf "${R}│${N} ${D}(delegation.idp_rejected: wrong client/secret; delegation.scope_too_broad)${N}\n"
      printf "${R}│${N} ${D}the reason is only in the response body: see the MAS worker's error,${N}\n"
      printf "${R}│${N} ${D}or replay the call (docs/ADDING_AN_EXTERNAL_AGENT.md troubleshooting)${N}\n"
    else
      printf "${R}│${N} ${D}filter=${r_filter} — request denied before the agent${N}\n"
    fi
    printf "${R}└─${N}\n"
    return 0
  fi
  if grep -qiE 'refreshed JWKS|claim map: no candidate|praxis_policy_plugin_identity_jwt::|body done' <<<"$line"; then
    return 0
  fi
  if grep -qE '(^|[[:space:]])(ERROR)([[:space:]]|:)' <<<"$line"; then
    printf "\n${R}┌─ EGRESS ERROR [${ts}] ${D}(${key})${N}\n"
    printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${R}└─${N}\n"
  fi
  return 0
}

# ── lightspeed-hub A2A adapter ──────────────────────────────────────────────
# UNCONFIRMED WATCHER: no live adapter sample has been captured in this repo
# (the pinned image is still a placeholder). These matchers are written from
# the adapter's documented flags and error strings; replay a real log through
# parse_line and tighten them on first contact. The exchange itself is also
# proven by the Keycloak feed, and "H in / R stored" by the operator cards.
parse_adapter() {
  local line="$1" ts="$2"
  # The adapter speaks for the caller with H, and only H. Any bearer it logs
  # is verified against that (UNCONFIRMED WATCHER: only fires if the adapter
  # logs the JWT; Keycloak's H→R event is the authoritative exchange record).
  local fp=""
  ADAPTER_FP=""
  if [[ "$line" == *"Bearer "* ]]; then fp=$(jwt_fingerprint_in "$line"); ADAPTER_FP="$fp"; fi
  if grep -qiE 'already exists|duplicate|idempoten' <<<"$line"; then
    printf "\n${Y}┌─ ADAPTER DUPLICATE SUPPRESSED [${ts}]${N}\n"
    printf "${Y}│${N} ${D}same task/message already has a run — returns it instead of re-submitting${N}\n"
    printf "${Y}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${Y}└─ expected on redelivery; NOT a failure${N}\n"
    return 0
  fi
  if grep -qiE 'unknown target|not a known (target|cluster)|allowed.targets' <<<"$line"; then
    printf "\n${R}┌─ ADAPTER UNKNOWN TARGET [${ts}]${N}\n"
    printf "${R}│${N} ${D}the requested target is not in --allowed-targets (no AgenticRun)${N}\n"
    printf "${R}│${N} ${D}keep adapter.allowedTargets == praxis spokeGateways keys${N}\n"
    printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${R}└─ fail-fast before anything is created${N}\n"
    return 0
  fi
  if grep -qiE 'exchange' <<<"$line" && grep -qiE 'fail|error|invalid|audience not found' <<<"$line"; then
    printf "\n${R}┌─ ADAPTER TOKEN EXCHANGE FAILED (H→R) [${ts}]${N}\n"
    printf "${R}│${N} ${D}WHO     :${N} adapter authenticating as client ${W}lightspeed-hub-a2a${N}\n"
    printf "${R}│${N} ${D}WANTED  :${N} R, aud ${W}praxis-spoke-gateway${N}\n"
    if grep -qiE 'request_failed|tls|x509|ssl|certificate' <<<"$line"; then
      printf "${R}│${N} ${D}KIND    :${N} ${R}transport/TLS${N} ${D}— the adapter could not reach Keycloak${N}\n"
      printf "${R}│${N} ${D}          (a generic request_failed here hides a CA problem)${N}\n"
      printf "${R}│${N} ${D}FIX     :${N} ${D}adapter.keycloak.caTrust (SSL_CERT_FILE + issuer CA), not TLS off${N}\n"
    else
      printf "${R}│${N} ${D}KIND    :${N} ${D}Keycloak refused the grant (OAuth error) — check --kc${N}\n"
    fi
    printf "${R}│${N} %s\n" "$(trunc "$line" 170)"
    printf "${R}└─ with no R the sandbox cannot read the spoke (look for the Secret)${N}\n"
    return 0
  fi
  # A successful exchange (UNCONFIRMED WATCHER: wording varies by build).
  if grep -qiE 'exchange' <<<"$line" && grep -qiE 'ok|success|issued|stored|wrote|renewed' <<<"$line"; then
    if exchange_seen "H>R"; then
      printf "${D}┄─ lightspeed-hub adapter: exchange H→R already recorded by Keycloak [${ts}]${N}\n"
      return 0
    fi
    record_exchange H R "lightspeed-hub-a2a" "praxis-spoke-gateway" "lightspeed-hub adapter" "$ts"
    printf "${D}│${N} ${D}UNCONFIRMED WATCHER — verbatim adapter line:${N}\n"
    printf "${D}│${N} %s\n" "$(trunc "$line" 170)"
    printf "${D}└─${N}\n"
    return 0
  fi
  if grep -qiE 'run.{0,3}token|ls-run-token|credential.{0,3}secret' <<<"$line" && grep -qiE 'creat|updat|writ|patch|renew|stor|delet' <<<"$line"; then
    local verb="writes"
    grep -qiE 'renew|refresh' <<<"$line" && verb="renews"
    grep -qiE 'delet|sweep|cleanup' <<<"$line" && verb="deletes"
    printf "\n${M}┌─ ADAPTER RUN-TOKEN SECRET [${ts}]${N} ${D}$(token_tag R)${N}\n"
    if [[ "$verb" == "writes" ]]; then
      printf "${M}│${N} ${D}WHO     :${N} adapter (SA) ${W}writes${N} R into the Secret ${D}(not A, not H)${N}\n"
    else
      printf "${M}│${N} ${D}WHO     :${N} adapter (SA) ${W}${verb}${N} R ${D}(not A, not H)${N}\n"
    fi
    printf "${M}│${N} ${D}WHERE   :${N} ${D}name ls-run-token-<uid>, key token-a (operator's contract)${N}\n"
    printf "${M}│${N} ${D}WHY R   :${N} ${D}only the spoke gateway accepts R; H/A are dead past the adapter${N}\n"
    if [[ "$verb" == "deletes" ]]; then
      printf "${M}└─ terminal-run cleanup (grace period) — nothing long-lived is left${N}\n"
    else
      printf "${M}└─ the operator mounts it read-only as tokenFile (no exchange there)${N}\n"
    fi
    return 0
  fi
  if grep -qiE 'renewed|renewal (complete|succeeded)|renewing run' <<<"$line" \
     && grep -qiE 'credential|run.?token' <<<"$line" \
     && ! grep -qiE 'paus|auth.?required|waiting' <<<"$line"; then
    record_exchange H R "lightspeed-hub-a2a" "praxis-spoke-gateway" "lightspeed-hub adapter (renewal: H was freshly minted by the hub gateway)" "$ts"
    printf "${D}│${N} ${D}UNCONFIRMED WATCHER — verbatim renewal line:${N}\n"
    printf "${D}│${N} %s\n" "$(trunc "$line" 170)"
    printf "${D}└─${N}\n"
    return 0
  fi
  if grep -qiE 'credential' <<<"$line" && grep -qiE 'paus|auth.?required' <<<"$line"; then
    printf "\n${Y}┌─ ADAPTER PAUSED: credential renewal [${ts}]${N}\n"
    printf "${Y}│${N} ${D}the task is waiting for credentials; it has not failed yet${N}\n"
    printf "${Y}│${N} ${D}EXPECTED if R is near expiry: MAS sends a fresh A → fresh H → fresh R${N}\n"
    printf "${Y}│${N} ${D}PERSISTENT (and no ls-run-token-* ever appeared in the operator card):${N}\n"
    printf "${Y}│${N} ${D}the H→R exchange or the Secret WRITE failed — this message only${N}\n"
    printf "${Y}│${N} ${D}watches the operator's condition, it is not proof either succeeded${N}\n"
    printf "${Y}│${N} ${D}  typical: adapter cannot reach Keycloak's private CA (SSL_CERT_FILE)${N}\n"
    printf "${Y}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${Y}└─ a Keycloak H→R event without a later RUN-TOKEN SECRET card is the tell${N}\n"
    return 0
  fi
  if grep -qiE 'agenticrun' <<<"$line" && grep -qiE 'creat|exists|submitt' <<<"$line"; then
    printf "\n${C}┌─ ADAPTER AGENTICRUN [${ts}]${N} ${D}$(token_tag H)${N}\n"
    printf "${C}│${N} ${D}WHO     :${N} the run is created AS THE CALLER with H (not as the SA)${N}\n"
    printf "${C}│${N} ${D}H        : aud lightspeed-hub-a2a, group …:agenticrun-creator, 300 s${N}\n"
    check_ip adapter "$ts" "$fp"
    printf "${C}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${C}│${N} ${D}SEQUENCE:${N} ${D}create run as H → exchange H→R → write the run-token Secret${N}\n"
    printf "${C}│${N} ${D}a run existing does NOT prove the last two steps succeeded${N}\n"
    printf "${C}└─ hub API records user keycloak:<sub>; the operator then starts it${N}\n"
    return 0
  fi
  if grep -qiE 'task' <<<"$line" && grep -qiE 'complet|succeed|analyzed' <<<"$line"; then
    printf "\n${G}┌─ ADAPTER TASK COMPLETED [${ts}]${N}\n"
    printf "${G}│${N} ${D}result artifacts stream back to MAS on the caller's open stream${N}\n"
    printf "${G}└─ AgenticRun reached a terminal state; no token is presented again${N}\n"
    return 0
  fi
  if grep -qiE 'stream.closed|disconnect|context canceled' <<<"$line"; then
    printf "\n${Y}┌─ ADAPTER STREAM CLOSED [${ts}]${N}\n"
    printf "${Y}│${N} ${D}caller went away (MAS/ACME stream ended) — the run keeps its state${N}\n"
    printf "${Y}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${Y}└─ resubscribe to the SAME task, not a new one${N}\n"
    return 0
  fi
  if grep -qE '^E[0-9]{4}| level=(error|fatal)| ERROR |panic' <<<"$line"; then
    printf "\n${R}┌─ ADAPTER ERROR [${ts}]${N}\n"
    printf "${R}│${N} ${D}WHO     :${N} adapter itself (its own SA / an exchanger client)${N}\n"
    printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${R}└─ this is the adapter's work, not the caller's; see the hub chart README${N}\n"
    return 0
  fi
  return 0
}

# A card for the bearer the adapter saw. Kept separate from the activity
# cards so it is emitted exactly once per fingerprint (a chatty adapter must
# not spam the stream), and so an ALERT is never buried inside a normal card.
report_adapter_identity() {
  local ts="$1" fp="$2"
  [[ -z "$fp" || "$fp" == "$(kv_get adapter_reported_fp)" ]] && return 0
  kv_set adapter_reported_fp "$fp"
  check_ip adapter "$ts" "$fp"
  flag_identity adapter "$ts" "$fp" "$IP_STATUS"
}

# ── run-token Secret watcher (ls-run-token-* in the hub namespace) ──────────
# The adapter logs the Secret's DELETION ("deleted run-token Secret after
# terminal grace") but never its creation — there is no log line for the
# H→R exchange or the Secret write (verified live 2026-11-25). The kube API
# is the remaining witness: `oc get secrets -w` in the hub namespace. A new
# ls-run-token-* row proves steps 2+3 of the adapter sequence (exchange H→R,
# write the Secret) succeeded, and cards the H→R exchange when Keycloak
# events are unavailable.
parse_runcred() {
  local line="$1" ts="$2"
  local name created
  name=$(awk '{print $1}' <<<"$line" || true)
  created=$(awk '{print $2}' <<<"$line" || true)
  [[ "$name" == ls-run-token-* ]] || return 0
  # `oc get -w` reprints a row on every change: card each Secret once.
  if [[ "$(kv_get "runcred_${name}")" == "1" ]]; then
    return 0
  fi
  kv_set "runcred_${name}" "1"
  # creationTimestamp is the event time (UTC), not the watch's receipt time.
  if [[ "$created" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2} ]]; then
    ts=$(date -d "${created:0:19}Z" +%H:%M:%S 2>/dev/null || printf '%s' "$ts")
    ts="${ts#\~}"
  fi
  if ! exchange_seen "H>R"; then
    record_exchange H R "lightspeed-hub-a2a" "praxis-spoke-gateway" \
      "lightspeed-hub adapter (run-token Secret appeared)" "$ts"
  fi
  printf "\n${M}┌─ ADAPTER RUN-TOKEN SECRET (stored) [${ts}]${N} ${D}$(token_tag R)${N}\n"
  printf "${M}│${N} ${D}WHO     :${N} adapter (SA) ${W}stored${N} R ${D}into the Secret (not A, not H)${N}\n"
  printf "${M}│${N} ${D}WHERE   :${N} ${W}${name}${N} ${D}key token-a (operator's contract)${N}\n"
  printf "${M}│${N} ${D}WHY R   :${N} ${D}only the spoke gateway accepts R; H/A are dead past the adapter${N}\n"
  printf "${M}└─ steps 2+3 of the adapter sequence succeeded (exchange H→R, write)${N}\n"
  return 0
}

# ── lightspeed-agentic-operator (gateway credential mode) ────────────────────
# The operator's job in this chain is explicit: mount the stored credential and
# run the sandbox. It performs NO token exchange and holds NO caller token.
parse_operator() {
  local line="$1" ts="$2"
  # Reconcile-loop churn: the controller re-queues constantly, so "reconciling"
  # and "analysis already in progress, waiting" repeat many times per run with
  # zero new information (observed live: ~15 near-identical OPERATOR SANDBOX
  # cards per run buried the actual events). Drop them outright; "handling
  # <phase>" carries phase information, so it is kept but deduped per
  # run+phase — the transition cards exactly once.
  if grep -qE '(DEBUG|INFO)[[:space:]]+reconciling' <<<"$line"; then
    return 0
  fi
  if grep -q 'already in progress, waiting' <<<"$line"; then
    return 0
  fi
  if [[ "$line" =~ handling[[:space:]]+(analysis|execution) ]]; then
    local rname phase
    rname=$(grep -m1 -oP 'a2a-[0-9a-f]+' <<<"$line" || true)
    phase="${BASH_REMATCH[1]}"
    if [[ -n "$rname" && "$(kv_get "op_${rname}_${phase}")" == "1" ]]; then
      return 0
    fi
    [[ -n "$rname" ]] && kv_set "op_${rname}_${phase}" "1"
  fi
  if grep -qiE 'gateway credential mode|credential mode' <<<"$line"; then
    printf "\n${G}┌─ OPERATOR: GATEWAY CREDENTIAL MODE [${ts}]${N}\n"
    printf "${G}│${N} ${D}WHO     :${N} the operator's own ServiceAccount (no caller token)${N}\n"
    printf "${G}│${N} ${D}SANDBOX :${N} kubeconfig → spoke gateway Service, tokenFile = mounted R${N}\n"
    printf "${G}└─ no spoke SA, no spoke kubeconfig, NO EXCHANGE (by design)${N}\n"
    return 0
  fi
  if grep -qiE 'credentials?expired|secret .*not found|ls-run-token' <<<"$line" && \
     grep -qiE 'expired|not found|missing' <<<"$line"; then
    local rn
    rn=$(grep -m1 -oP 'a2a-[0-9a-z-]+|(run|uid)\s*[=:]\s*\K[^ ,]+' <<<"$line" || true)
    printf "\n${R}┌─ OPERATOR: RUN HAS NO CREDENTIAL [${ts}]${N} ${D}${rn:+run=${rn}}${N}\n"
    printf "${R}│${N} ${D}CredentialsExpired=True / ls-run-token-… Secret not found${N}\n"
    printf "${R}│${N} ${D}the AgenticRun EXISTS but the adapter never wrote its R Secret.${N}\n"
    printf "${R}│${N} ${D}A brief pause can be a creation race; persistent absence means${N}\n"
    printf "${R}│${N} ${D}the H→R exchange or the adapter's Secret write failed (often the${N}\n"
    printf "${R}│${N} ${D}Keycloak CA, surfaced by the adapter as a generic request_failed).${N}\n"
    printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${R}└─ no R → no spoke gateway call → no R→U → the sandbox cannot read${N}\n"
    return 0
  fi
  if grep -qiE 'sandbox|analy|execution|verification|releas' <<<"$line" && grep -qiE 'creat|start|reconcil|complete|fail|success' <<<"$line"; then
    printf "\n${C}┌─ OPERATOR SANDBOX [${ts}]${N}\n"
    printf "${C}│${N} ${D}WHO     :${N} operator's SA acts on the AgenticRun; the caller's rights${N}\n"
    printf "${C}│${N} ${D}          enter only through R→U at the spoke, never via the operator${N}\n"
    printf "${C}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${C}└─${N}\n"
    return 0
  fi
  if grep -qE ' ERROR |panic|reconcile (error|failed)' <<<"$line"; then
    printf "\n${R}┌─ OPERATOR ERROR [${ts}]${N}\n"
    printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${R}└─ run lifecycle failed before/while the sandbox ran${N}\n"
    return 0
  fi
  return 0
}

# ── OCP API server (--api; cluster-admin) ───────────────────────────────────
parse_api() {
  local line="$1" ts="$2"
  # The API server identifies tokens by the OIDC username it mapped them to:
  # keycloak:<sub> for H and U (same user!), which is exactly why RBAC binds
  # the audience-scoped GROUPS. A "user keycloak:..." line is therefore the
  # first place the chain shows a real Kubernetes identity.
  local kuser kgroup
  kuser=$(grep -m1 -oP 'user\.?name"?[:=] ?"?(keycloak:[^ ",]+)' <<<"$line" | sed 's/.*keycloak:/keycloak:/' || true)
  if [[ -n "$kuser" ]]; then
    printf "\n${B}┌─ API IDENTITY [${ts}]${N}\n"
    printf "${B}│${N} ${D}WHO     :${N} a token authenticated at a kube API as ${W}${kuser}${N}\n"
    printf "${B}│${N} ${D}K8S USER:${N} ${D}the same sub for H and U — the difference is the GROUPS${N}\n"
    printf "${B}│${N} ${D}          …:agenticrun-creator (H) vs …:reader (U)${N}\n"
    # Without a token on the line there is nothing to fingerprint; name the
    # hop whose credential is expected and do not imply a verification.
    if [[ "$line" == *"Bearer "* ]]; then
      check_ip kubeapi-spoke "$ts" "$(jwt_fingerprint_in "$line")"
    else
      printf "${B}│${N} ${D}IDENTITY:${N} ${D}H (run creation) or U (spoke reads) — no fingerprint${N}\n"
      printf "${B}│${N} ${D}          on the API line; verify which hop from the group${N}\n"
    fi
    printf "${B}│${N} ${D}${trunc "$line" 150}${N}\n"
    printf "${B}└─ never bind RBAC to keycloak:<sub> directly: it spans both tokens${N}\n"
  fi
  if grep -qiE 'unable to authenticate|invalid bearer token|oidc' <<<"$line"; then
    printf "\n${R}┌─ API TOKEN REJECT [${ts}]${N}\n"
    printf "${R}│${N} ${D}the API server's OIDC authenticator rejected a bearer token${N}\n"
    printf "${R}│${N} ${D}EXPECTED: A and R are both unusable at a kube API (by design)${N}\n"
    printf "${R}│${N} ${D}UNEXPECTED: H at the hub cannot create runs, U cannot read${N}\n"
    printf "${R}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${R}└─ a rejection is only a bug when the hop is exercising H or U${N}\n"
    return 0
  fi
  if grep -qiE 'forbidden' <<<"$line"; then
    printf "\n${Y}┌─ API AUTHZ DENY [${ts}]${N}\n"
    printf "${Y}│${N} ${D}WHO     :${N} a token authenticated but its mapped user/group lacks RBAC${N}\n"
    printf "${Y}│${N} ${D}CHECK   :${N} H needs …:agenticrun-creator (create runs on the hub);${N}\n"
    printf "${Y}│${N} ${D}          U needs …:reader (read on that spoke) — a deny is the${N}\n"
    printf "${Y}│${N} ${D}          state to expect on the OTHER side of the pair${N}\n"
    printf "${Y}│${N} %s\n" "$(trunc "$line" 160)"
    printf "${Y}└─ confirm with oc auth can-i (see README)${N}\n"
    return 0
  fi
  return 0
}

# ── Keycloak realm events: the authoritative record ─────────────────────────
# Keycloak logs, for the realm, every token the server issues: type, clientId
# (the client that authenticated / performed the exchange), userId (on whose
# behalf), sessionId, and details. For a token exchange the details name the
# audience; for a client_credentials grant the clientId is the minting client.
# That makes this feed the ground truth for "who exchanged what for whom",
# independent of any pod's log level. It also records LOGIN_ERROR and
# TOKEN_EXCHANGE_ERROR, so a failed exchange is visible here even if no hop
# logs it.
#
# Event shape (Keycloak 26 real: type, realmId, clientId, userId, sessionId,
# ipAddress, error, details{...}, time) is parsed with python3 (stdlib only,
# per the repo convention for scripts).
parse_kc_event() {
  local line="$1" ts="$2"
  local out
  out=$(python3 - "$line" <<'PY' 2>/dev/null || true
import json, sys
try:
    ev = json.loads(sys.argv[1])
except Exception:
    sys.exit(1)
if not isinstance(ev, dict):
    sys.exit(1)
details = ev.get("details") or {}
def g(*names):
    for n in names:
        v = details.get(n)
        if isinstance(v, list):
            v = ",".join(map(str, v))
        if v not in (None, ""):
            return str(v)
    return ""
sys.stdout.write("\x1f".join([
    str(ev.get("type") or ""),
    str(ev.get("clientId") or ""),
    str(ev.get("userId") or ""),
    str(ev.get("sessionId") or ""),
    g("audience", "aud", "requested_audience"),
    g("grant_type"),
    str(ev.get("error") or details.get("error") or ""),
    g("username", "auth_method", "client_auth_method"),
    str(ev.get("id") or ""),
]) + "\x1f")
PY
)
  [[ -z "$out" ]] && return 0
  # Unit-separator (0x1f) delimited, not tab: bash's IFS whitespace handling
  # collapses EMPTY fields, which silently shifted grant_type into the
  # audience slot whenever a field (sessionId, audience, grant_type) was
  # absent. 0x1f cannot occur in these values.
  local type client user session aud grant err extra evid
  local -a fields=()
  IFS=$'\x1f' read -r -a fields <<<"$out"
  type="${fields[0]:-}" client="${fields[1]:-}" user="${fields[2]:-}"
  session="${fields[3]:-}" aud="${fields[4]:-}" grant="${fields[5]:-}"
  err="${fields[6]:-}" extra="${fields[7]:-}" evid="${fields[8]:-}"
  [[ -z "$type" ]] && return 0
  # The poller's lookback deliberately overlaps previous windows, so the same
  # event can be fetched twice. Keycloak gives every event a unique id; a
  # re-fetch of a known id must not re-card (fake events without an id, such
  # as the replay tests', always pass through).
  if [[ -n "$evid" ]]; then
    if [[ "$(kv_get "kcev_${evid}")" == "1" ]]; then
      return 0
    fi
    kv_set "kcev_${evid}" "1"
  fi
  # The event's own time is the truth for ordering; the emitted line has no
  # log timestamp of its own.
  local evtime
  evtime=$(python3 -c 'import json,sys;print(json.loads(sys.argv[1]).get("time") or "")' "$line" 2>/dev/null || true)
  # Prefer the event's own timestamp: events lag the request slightly.
  if [[ -n "$evtime" && "$evtime" =~ ^[0-9]+$ ]]; then
    ts=$(date -d "@$((evtime / 1000))" +%H:%M:%S 2>/dev/null || echo "$ts")
  fi

  # Which token stage does this event describe? The client that acts names it:
  #   acme-agent          → A minted (client_credentials)
  #   mas-a2a             → A→H   (exchange by the hub gateway)
  # lightspeed-hub-a2a → H→R (exchange) or H used to create the run (login)
  #   praxis-spoke-gateway→ R→U   (exchange, one per spoke target)
  local stage="" who="" act=""
  case "$type" in
    TOKEN_EXCHANGE|TOKEN_EXCHANGE_ERROR)
      case "$client" in
        mas-a2a)             stage="A → H"; who="praxis hub gateway";;
        lightspeed-hub-a2a)  stage="H → R"; who="lightspeed-hub adapter";;
        praxis-spoke-gateway) stage="R → U"; who="praxis spoke gateway";;
        *)                   stage="exchange by ${client:-?}"; who="unclassified";;
      esac ;;
    # Live (2026-11-25, Keycloak 26): a workload client's client_credentials
    # grant is reported as CLIENT_LOGIN (never LOGIN) with
    # details.grant_type=client_credentials — including acme-agent's
    # SPIFFE client_assertion mint ("federated-jwt" auth method).
    LOGIN|LOGIN_ERROR|CLIENT_LOGIN|CLIENT_LOGIN_ERROR)
      if [[ "${err:-}" == "" && "$grant" == "client_credentials" ]]; then
        stage="mint"; who="${client:-?}"
        [[ "$client" == "acme-agent" ]] && stage="mint A"
      fi ;;
  esac
  # An exchange with an audience: the answer to "for what".
  local tgt=""
  [[ -n "$aud" ]] && tgt="aud ${aud}"

  if [[ "${type}" == *ERROR* || -n "$err" ]]; then
    printf "\n${R}┌─ KEYCLOAK ${type} [${ts}]${N}\n"
    [[ -n "$stage" ]] && printf "${R}│${N} ${D}HOP     :${N} ${stage} ${D}(${who})${N}\n"
    printf "${R}│${N} ${D}CLIENT  :${N} ${W}${client:-?}${N}  ${D}SUBJECT:${N} ${W}${user:-?}${N}\n"
    printf "${R}│${N} ${D}ERROR   :${N} ${W}${err:-?}${N} ${D}${extra:+· ${extra}}${N}\n"
    printf "${R}└─ Keycloak refused the grant — the hop cannot obtain its token${N}\n"
    return 0
  fi

  if [[ "$stage" == *"→"* ]]; then
    # The authoritative exchange card. Subject is the event userId.
    # The token crossing this hop is not carried by the event, so its
    # fingerprint is unknown; the card therefore shows client/audience/
    # subject, which is what the event proves.
    # Keycloak is authoritative. If a pod already reported this exchange,
    # say so instead of emitting a second, thinner card for the same grant.
    if exchange_seen "${stage%% *}>${stage##* }"; then
      # Recurring exchanges (the sandbox makes one R→U per spoke API call)
      # each used to print a full confirmation line; one is enough.
      if [[ "$(kv_get "kc_confirmed_${stage%% *}_${stage##* }")" != "1" ]]; then
        kv_set "kc_confirmed_${stage%% *}_${stage##* }" "1"
        printf "\n${D}┌─ KEYCLOAK confirmed ${stage} [${ts}]${N} ${D}(hop already carded)${N}\n"
        printf "${D}│${N} ${D}CLIENT  :${N} ${W}${client:-?}${N}  ${D}AUDIENCE:${N} ${W}${aud:-?}${N}\n"
        printf "${D}└─ later grants for the same hop corroborate it silently${N}\n"
      fi
      return 0
    fi
    record_exchange "${stage%% *}" "${stage##* }" "$client" "${aud:-?}" "Keycloak event (${who})" "$ts" "$user" "" ""
    if [[ -z "$aud" ]]; then
      printf "${D}│${N} ${D}(no audience in the event details — seen for a scope-based${N}\n"
      printf "${D}│${N} ${D} exchange; the audience is the client named in the request)${N}\n"
      printf "${D}└─${N}\n"
    fi
    return 0
  fi

  if [[ "$stage" == mint* ]]; then
    printf "\n${M}┌─ KEYCLOAK MINT ${stage#mint } [${ts}]${N}\n"
    printf "${M}│${N} ${D}WHO     :${N} ${W}${client:-?}${N} ${D}authenticated to Keycloak and got a token${N}\n"
    printf "${M}│${N} ${D}SUBJECT :${N} ${W}${user:-?}${N} ${D}(the principal the token carries)${N}\n"
    printf "${M}│${N} ${D}GRANT   :${N} ${grant:-?} ${D}${extra:+· ${extra}}${N}\n"
    printf "${M}└─${N}\n"
    return 0
  fi

  # Any other grant for the chain's clients is still worth one dim line --
  # it is how a renewal (a second grant for the same subject) becomes visible.
  case "$client" in
    acme-agent|mas-a2a|lightspeed-hub-a2a|praxis-spoke-gateway)
      printf "${D}┄─ keycloak ${type} client=${client} ${tgt:+${tgt} }user=${user:-?} ${extra:+(${extra})} [${ts}]${N}\n"
      ;;
  esac
  return 0
}

# ── Keycloak Admin REST helpers (used by the kc follower) ──────────────────
# One curl wrapper so TLS policy lives in one place. A private-CA Route is the
# normal case (KC_CA verifies it); -k is the fallback for a fresh cluster
# whose CA is not at hand yet, and it is called out at start-up.
kc_curl() {
  local args=(-s --max-time 15)
  if [[ -n "${KC_CA:-}" ]]; then args+=(--cacert "$KC_CA"); else args+=(-k); fi
  curl "${args[@]}" "$@"
}

# KC_URL empty derives https://keycloak.<cluster domain> from the kubeconfig
# (the RHBK operator puts Keycloak behind keycloak.<apps domain>).
kc_base_url() {
  if [[ -n "${KC_URL:-}" ]]; then printf '%s' "$KC_URL"; return 0; fi
  # Prefer the Route the keycloak-oidc chart actually created. Deriving from
  # the API server host guesses "keycloak.<domain>" — the route is
  # "keycloak.apps.<domain>", so the guessed name does not resolve and the
  # feed dies at DNS with only a stderr warning (observed live: the feed
  # never produced a card although every hop was healthy).
  local host=""
  host=$(oc get routes -n "$NS_KC" -o jsonpath='{range .items[*]}{.spec.host}{"\n"}{end}' 2>/dev/null \
    | grep -m1 -i keycloak || true)
  if [[ -z "$host" ]]; then
    host=$(oc whoami --show-server 2>/dev/null || true)
    host="${host#https://}"; host="${host%%/*}"; host="${host%%:*}"
    [[ -n "$host" ]] && host="keycloak.${host#api.}"
  fi
  [[ -n "$host" ]] && printf 'https://%s' "$host"
}

# admin-cli password grant with the Secret the keycloak-oidc chart keeps
# (realm-secrets-reconciler uses the same pair). Token on stdout; never logged.
kc_admin_token() {
  local base="$1" user pass
  user=$(oc get secret -n "$NS_KC" "$KC_ADMIN_SECRET" \
    -o jsonpath="{.data.${KC_ADMIN_USER_KEY}}" 2>/dev/null | base64 -d 2>/dev/null || true)
  pass=$(oc get secret -n "$NS_KC" "$KC_ADMIN_SECRET" \
    -o jsonpath="{.data.${KC_ADMIN_PASS_KEY}}" 2>/dev/null | base64 -d 2>/dev/null || true)
  if [[ -z "$user" || -z "$pass" ]]; then
    echo "could not read Secret $NS_KC/$KC_ADMIN_SECRET (${KC_ADMIN_USER_KEY}/${KC_ADMIN_PASS_KEY})" >&2
    return 1
  fi
  kc_curl --max-time 15 \
    -d client_id=admin-cli -d grant_type=password \
    --data-urlencode "username=$user" --data-urlencode "password=$pass" \
    "$base/realms/master/protocol/openid-connect/token" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null
}

# Emit realm events newer than $3 (epoch ms) as one JSON object per line,
# oldest first, page by page. Keycloak returns newest first with no cursor, so
# this re-reads the recent window and filters by time (+ id, in the caller).
#
# TOKEN_EXCHANGE / LOGIN / CLIENT_LOGIN grants are LOGIN events: they are
# served by /admin/realms/<realm>/events. The previous version queried
# /admin-events (admin API operations — client creates, role mappings), which
# can never contain a token grant; the feed was empty by construction.
#
# `timeout` execs a PROGRAM, so the curl call runs inside bash -c with the
# helper's own definition available ("timeout 25 kc_curl ..." fails: not a
# file — caught by the fake-curl integration test).
kc_events_since() {
  local base="$1" token="$2" since_ms="$3"
  timeout 25 bash -c '
      KC_CA="$1"; token="$2"; url="$3"
      '"$(declare -f kc_curl)"'
      kc_curl --max-time 20 -H "Authorization: Bearer $token" "$url"
    ' _ "${KC_CA:-}" "$token" \
    "$base/admin/realms/$KC_REALM/events?max=500" \
    | python3 -c '
import json, sys
since = int(sys.argv[1])
try:
    events = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(events, list):
    sys.exit(0)
for ev in reversed(events):
    if not isinstance(ev, dict):
        continue
    if (ev.get("time") or 0) > since:
        print(json.dumps(ev, separators=(",", ":")))
' "$since_ms" 2>/dev/null || true
}

# A feed that silently shows nothing is the worst failure mode: report the
# realm's event switch up front, and say exactly what to enable.
kc_probe() {
  local base="$1" token="$2" cfg
  cfg=$(kc_curl --max-time 15 -H "Authorization: Bearer $token" \
    "$base/admin/realms/$KC_REALM/events/config" 2>/dev/null || true)
  python3 - "$KC_REALM" "$cfg" <<'PY' 2>/dev/null || true
import json, sys
realm, raw = sys.argv[1], sys.argv[2]
try:
    cfg = json.loads(raw)
except Exception:
    print("  could not read the realm event configuration; the feed may be empty")
    sys.exit(0)
types = ",".join(cfg.get("enabledEventTypes") or []) or "(all)"
print("  realm %s events=%s types=%s" % (realm, cfg.get("eventsEnabled"), types))
if not cfg.get("eventsEnabled"):
    print("  \u2192 realm events are DISABLED: no exchange record will appear; enable")
    print("    Realm settings \u2192 Events \u2192 Save events (or the realm import).")
PY
}

# ── command line ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --acme)   MODE=acme; shift ;;
    --mas)    MODE=mas; shift ;;
    --worker) MODE=worker; shift ;;
    --praxis) MODE=praxis; shift ;;
    --egress) MODE=egress; shift ;;
    --hub)    MODE=hub; shift ;;
    --kc)     MODE=kc; shift ;;
    --api)    MODE=api; shift ;;
    --join)   MODE=join; shift ;;
    --no-kc)  WANT_KC=0; shift ;;
    --endpoint|--endpoints) MODE=endpoint; shift ;;
    -h|--help) MODE=help; shift ;;
    --since)
      if [[ $# -lt 2 ]]; then
        echo "error: --since requires a duration (e.g. 10m, 1h)" >&2
        exit 1
      fi
      SINCE="$2"
      shift 2
      ;;
    *)
      echo "Unknown arg: $1 (try --help)" >&2
      exit 1
      ;;
  esac
done

# Library mode: everything below is defined, nothing runs.
if [[ "$MONITOR_LIB" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi
