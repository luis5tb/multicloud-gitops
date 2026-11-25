#!/usr/bin/env bash
# Real-time identity/delegation flow monitor for multicloud-gitops.
#
# Follows the caller's token through every stage of the agentic chain, and
# answers three questions per hop: WHO is acting, WHAT identity it wears, and
# WHAT it did on whose behalf.
#
#   acme-agent ─A─▶ MAO A2A endpoint ─A─▶ MAS orchestrator ─A─▶ praxis HUB gateway
#     ─H─▶ lightspeed-hub A2A adapter ──▶ hub kube API (AgenticRun as the caller)
#     ─R─▶ praxis SPOKE gateway ─U─▶ spoke kube API
#
#   Token chain (see agents/AUTHENTICATION.md):
#     A  acme-agent           → aud=mas-a2a               MAS + hub gateway
#     H  mas-a2a              → aud=lightspeed-hub-a2a    adapter + hub kube API
#     R  lightspeed-hub-a2a   → aud=praxis-spoke-gateway  spoke gateways only
#     U  praxis-spoke-gateway → aud=praxis-spoke-<id>     spoke kube API
#   Every token keeps the SAME sub: one principal, re-scoped once per hop.
#
# Usage:
#   bash scripts/monitor.sh                 # all hops + Keycloak event feed
#   bash scripts/monitor.sh --acme          # ACME only
#   bash scripts/monitor.sh --mas           # MAO A2A endpoint only (mas-a2a)
#   bash scripts/monitor.sh --worker        # MAS orchestrator (Temporal worker)
#   bash scripts/monitor.sh --praxis        # Praxis hub gateway + spoke gateways
#   bash scripts/monitor.sh --hub           # lightspeed-hub adapter + agentic operator
#   bash scripts/monitor.sh --kc            # Keycloak realm events only (AUTHORITATIVE)
#   bash scripts/monitor.sh --api           # OCP API server token rejects (cluster-admin)
#   bash scripts/monitor.sh --join          # all hops except --api (default)
#   bash scripts/monitor.sh --since 10m     # oc --since (default: 1s = attach from now)
#   bash scripts/monitor.sh --no-kc         # skip the Keycloak event feed
#   bash scripts/monitor.sh --endpoint      # print every log source, then exit
#   REQUEST_ID=abc123 bash scripts/monitor.sh --join   # filter one request id
#
# Unlike agentic-partners-integration/scripts/monitor.sh this uses `oc logs`
# (no Docker/Postgres audit_events). The parsing (monitor-lib.sh) is shared
# with tests/scripts/test_monitor.py, which replays captured lines through
# exactly this code. ACME's acme_audit, the MAS loggers and the Praxis tracing
# output were confirmed against live logs on bm-cluster (2026-10-23/26, mas
# image a80e09316564); the lightspeed-hub adapter/operator and the Praxis
# delegator line are marked "UNCONFIRMED WATCHER" in monitor-lib.sh until a
# live sample is replayed. Identities and exchanges do not depend on those
# matchers succeeding: the Keycloak event feed (--kc) is Keycloak's own record
# of every grant, and tokens are matched by fingerprint across hops.
#
# Tokens are never printed by design — audit lines and cards carry only short
# SHA-256 fingerprints (fp:…) that identify a token without being usable as
# one, and the Keycloak feed never prints secrets. Cards are stamped with the
# POD'S OWN timestamp (converted to local time); a leading ~ means the line
# carried no timestamp and the monitor's receipt time was used instead — cross-
# hop order between a ~ card and stamped cards is only approximate, because
# each hop is a separate log pipeline with its own delivery latency.
#
# See scripts/README.md ("Flow monitor") for selectors, overrides, and tips.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=monitor-lib.sh
source "$HERE/monitor-lib.sh"

# ── lifecycle ───────────────────────────────────────────────────────────────
# `timeout`, a deleted terminal or `kill` sends SIGTERM to THIS shell only.
# Without handling it the follower loops keep `sleep`ing and the monitor never
# exits (observed when testing the Keycloak poller under `timeout`). The flag
# makes every loop stop at its next wake-up.
RUNNING=1
stop_running() { RUNNING=0; }
trap stop_running TERM INT HUP
nap() {  # sleep that wakes immediately once RUNNING is cleared
  local s="$1"
  while (( s > 0 && RUNNING )); do sleep 1; s=$(( s - 1 )); done
}

# ── log followers ───────────────────────────────────────────────────────────
follow() {
  local name="$1" ns="$2" sel="$3"
  # --prefix=true stamps each line "[<ns>/<pod>:<container>] text": it keeps
  # multi-pod namespaces readable and is how parse_line tells the hub gateway
  # from a spoke gateway (same selector, different pod names). --all-containers
  # keeps the dnsIPv4Only sidecar's errors visible.
  # No pods → say so once and skip that hop (a chart may be dormant, such as
  # mas-a2a with a2a.enabled=false and the whole lightspeed-hub chart).
  if ! oc get pods -n "$ns" -l "$sel" --no-headers 2>/dev/null | grep -q .; then
    printf "${Y}warn: no pods in %s matching %s — skip %s${N}\n" "$ns" "$sel" "$name" >&2
    return 0
  fi
  # `oc logs -f` ENDS when the pod it follows is deleted or replaced — which
  # every ArgoCD sync of that chart does. A plain one-shot `oc logs -f` goes
  # silently dark until the monitor is restarted, so re-resolve the pods and
  # re-attach from "now".
  local since="$SINCE"
  while (( RUNNING )); do
    # --max-log-requests: a label that matches many containers (praxis-proxy
    # matches the hub gateway + every spoke + the CA-sync Jobs, 8+ streams)
    # exceeds `oc logs -f`'s default concurrency of 5. The client then prints
    # an error on stderr (discarded below) and HANGS — the hop goes dark with
    # no warning and no reattach (observed live: the praxis hop never printed
    # a single card while its pods logged normally).
    oc logs -n "$ns" -l "$sel" -f --since="$since" --prefix=true --all-containers=true \
        --max-log-requests=20 2>/dev/null \
      | while IFS= read -r line; do
          parse_line "$name" "$line"
        done
    # A hop that was removed (scale-to-zero, chart disabled) is reported once
    # here and then waited for quietly.
    if ! oc get pods -n "$ns" -l "$sel" --no-headers 2>/dev/null | grep -q .; then
      printf "${Y}warn: %s has no pods now (%s) — waiting for %s${N}\n" "$name" "$sel" "$ns" >&2
      nap 10
      since="$SINCE"
      continue
    fi
    printf "${D}◦ %s: log stream closed (pod rolled?) — reattaching${N}\n" "$name"
    since="1s"
    nap 1
  done
}

# ── run-token Secret watcher ─────────────────────────────────────────────────
# The adapter never logs the run-token Secret's creation (only its deletion),
# so the kube API is the only witness of "H→R exchange + Secret write". Watch
# the hub namespace's Secrets and hand rows to parse_runcred. The initial
# listing repeats rows for Secrets that already exist; parse_runcred cards
# each name once. Needs list/watch on secrets in NS_HUB (any user who can
# read the operator's logs normally has it).
watch_run_secrets() {
  while (( RUNNING )); do
    oc get secrets -n "$NS_HUB" -w --no-headers \
      -o custom-columns=NAME:.metadata.name,CTS:.metadata.creationTimestamp 2>/dev/null \
      | while IFS= read -r line; do
          parse_line runcred "$line"
        done
    nap 5
  done
}

# ── Keycloak event poller ───────────────────────────────────────────────────
# The realm's LOGIN/TOKEN_EXCHANGE events (the authoritative record of every
# grant in the chain) are fetched in windows and handed to parse_line as
# one JSON object per line (the same "one line = one event" contract the log
# followers have). Nothing here prints a secret; the admin token stays in a
# variable and is never echoed. Realm events must be ENABLED for this to see
# anything (kc_probe reports the realm's switch; the realm import sets it).
kc_follow() {
  local base token since now ev
  base=$(kc_base_url)
  if [[ -z "$base" ]]; then
    printf "${Y}warn: cannot derive the Keycloak URL (set KC_URL=…) — skip Keycloak events${N}\n" >&2
    return 0
  fi
  if ! token=$(kc_admin_token "$base"); then
    printf "${Y}warn: Keycloak admin login failed (Secret %s/%s) — skip Keycloak events${N}\n" "$NS_KC" "$KC_ADMIN_SECRET" >&2
    return 0
  fi
  printf "${D}Keycloak event feed: %s realm=%s (authoritative exchange record)${N}\n" "$base" "$KC_REALM"
  kc_probe "$base" "$token"
  # Start slightly in the past so an event already queued at launch is not lost.
  since=$(date +%s%3N)
  since=$(( since - 2000 ))
  local next_refresh=$(( $(date +%s) + 300 ))
  while (( RUNNING )); do
    now=$(date +%s%3N)
    # Read the feed through a NUL-free here-string-safe loop: the outer
    # while must not consume the script's own stdin (a scripted run often
    # pipes input in), so read in-process instead of a pipeline.
    while IFS= read -r ev; do
      [[ -n "$ev" ]] && parse_line kc "$ev"
    done < <(kc_events_since "$base" "$token" "$since")
    # A 30 s lookback, not 1 s: events can appear in the store seconds after
    # their `time` (observed live: the A mint and both chain exchanges were
    # written late and a 1 s window had already marched past them — the feed
    # skipped them forever). Re-fetches are deduped by event id in
    # parse_kc_event, so the overlap cannot double-card.
    since=$(( now - 30000 ))
    nap "$KC_POLL_SECONDS"
    # Re-authenticate occasionally: the admin token outlives a few polls but
    # not a long session.
    if (( $(date +%s) >= next_refresh )); then
      # A failed re-login must not leave the feed silently dead: keep the
      # previous token, retry soon, and say so.
      if ! token=$(kc_admin_token "$base"); then
        printf "${Y}warn: Keycloak admin re-login failed — retrying in 30 s${N}\n" >&2
        nap 30
      fi
      next_refresh=$(( $(date +%s) + 300 ))
    fi
  done
}

cleanup() {
  RUNNING=0
  # The state file is this run's private mktemp (or the pinned
  # MONITOR_STATE_FILE, which the operator keeps for inspection).
  rm -f "$STATE_FILE" 2>/dev/null || true
  # Followers may be blocked inside `oc logs -f` (not just `sleep`), so the
  # job PIDs must be signalled too, not only waited on.
  local pids
  pids=$(jobs -p 2>/dev/null || true)
  [[ -n "$pids" ]] && kill $pids 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

print_endpoints() {
  cat <<ENDPOINTS
hop        namespace               selector
---------  ----------------------  ------------------------------------------------
acme       ${NS_ACME}  ${SEL_ACME}
mas        ${NS_MAS}  ${SEL_MAS}
worker     ${NS_WORKER}  ${SEL_WORKER}
praxis     ${NS_PRAXIS}  ${SEL_PRAXIS}
           (hub = the praxis-proxy pod; spokes = praxis-spoke-<id>)
hub        ${NS_HUB}  ${SEL_HUB}
operator   ${NS_OPERATOR}  ${SEL_OPERATOR}
runcred    ${NS_HUB}  Secret watch: ls-run-token-* (creation has no log line)
kc         ${NS_KC}  Secret ${KC_ADMIN_SECRET} → realm events (realm ${KC_REALM})
kc         (KC_URL=https://keycloak.<cluster domain>; KC_CA for a private Route)
api        ${NS_API}  ${SEL_API}  (needs cluster-admin)

Overrides: NS_*/SEL_* as above, KC_URL, KC_CA, KC_REALM, KC_ADMIN_SECRET,
MONITOR_KC_POLL (seconds), MONITOR_STATE_FILE, MONITOR_SINCE / --since,
REQUEST_ID / --help.
ENDPOINTS
}

# --endpoint / --help are how you debug an empty stream, so they work without
# a cluster (print_endpoints needs no oc call).
case "$MODE" in
  help)     sed -n '2,53p' "$0"; exit 0 ;;
  endpoint) print_endpoints; exit 0 ;;
esac
require_oc
print_diagram

echo -e "${D}since=${SINCE}  mode=${MODE}  request_id filter=${FILTER_REQ:-none}  keycloak=${WANT_KC}${N}"
echo -e "${D}Tokens are never printed; cards name the token stage (A/H/R/U) and fp:… fingerprint.${N}"
echo -e "${D}Keycloak events are the authoritative exchange record; pod logs corroborate.${N}"
echo -e "${D}Tip: for Praxis JWT/routing detail set praxis-proxy logLevel (RUST_LOG), e.g.${N}"
echo -e "${D}  info,praxis_filter=debug,praxis_policy_plugin_identity_jwt=debug${N}"
echo -e "${D}Tip: final RBAC hop — confirm the mapped role group (needs cluster-admin):${N}"
echo -e "${D}  oc auth can-i get pods --as-group=keycloak:praxis-spoke-hub:reader -n <namespace>${N}"
echo -e "${D}Tip: bash scripts/monitor.sh --api also tails the API server's token rejects${N}"
echo -e "${D}Docs: scripts/README.md  ·  agents/AUTHENTICATION.md  ·  docs/agentic-e2e-token-flow.html${N}"
echo

case "$MODE" in
  acme)   follow acme   "$NS_ACME"   "$SEL_ACME" ;;
  mas)    follow mas    "$NS_MAS"    "$SEL_MAS" ;;
  worker) follow worker "$NS_WORKER" "$SEL_WORKER" ;;
  praxis) follow praxis "$NS_PRAXIS" "$SEL_PRAXIS" ;;
  hub)    follow hub      "$NS_HUB"      "$SEL_HUB" &
          follow operator "$NS_OPERATOR" "$SEL_OPERATOR" &
          watch_run_secrets &
          wait || true ;;
  kc)     kc_follow ;;
  api)    follow api    "$NS_API"    "$SEL_API" ;;
  join|all)
    # --api is deliberately not in the default join: it needs cluster-admin
    # and the API hop is normally observed via downstream deny cards instead.
    follow acme   "$NS_ACME"   "$SEL_ACME"   &
    follow mas    "$NS_MAS"    "$SEL_MAS"    &
    follow worker "$NS_WORKER" "$SEL_WORKER" &
    follow praxis "$NS_PRAXIS" "$SEL_PRAXIS" &
    follow hub      "$NS_HUB"      "$SEL_HUB" &
    follow operator "$NS_OPERATOR" "$SEL_OPERATOR" &
    watch_run_secrets &
    [[ "$WANT_KC" == "1" ]] && kc_follow &
    # `wait` returns 128+signal when a follower is killed; that is shutdown,
    # not a failure of the monitor.
    wait || true
    ;;
esac
