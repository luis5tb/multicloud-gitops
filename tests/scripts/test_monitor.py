"""Replay captured hop log lines through the real monitor parser.

The monitor is a shell tool, so the test does what an operator does when a
new log line appears: feed it through ``parse_line`` and check the card the
monitor prints. Lines are shaped like each hop's real output (ACME's
``acme_audit``, the MAS structured contexts, Praxis tracing with the
``--prefix`` tag, Go-style adapter/operator logs, Keycloak admin events) so a
matcher regression shows up here instead of on a live cluster.

Run with the rest of the script tests:

    python3 -m unittest discover -s tests/scripts -v
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "monitor-lib.sh"
MAIN = ROOT / "scripts" / "monitor.sh"
SUB = "bb366787-080c-4d98-b69e-2774e15855cb"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _jwt(sub: str = SUB, azp: str = "acme-agent", aud: str = "mas-a2a",
         groups=("acme-agent-rca",)) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return ".".join([
        enc({"alg": "RS256", "typ": "JWT"}),
        enc({"sub": sub, "azp": azp, "aud": aud, "groups": list(groups)}),
        base64.urlsafe_b64encode(b"signature" * 8).rstrip(b"=").decode(),
    ])


A = _jwt()
H = _jwt(azp="mas-a2a", aud="lightspeed-hub-a2a",
         groups=("lightspeed-hub-a2a:agenticrun-creator",))
R = _jwt(azp="lightspeed-hub-a2a", aud="praxis-spoke-gateway", groups=())
U = _jwt(azp="praxis-spoke-gateway", aud="praxis-spoke-hub",
         groups=("praxis-spoke-hub:reader",))

# Keep in step with acme_agent.auth.token_fingerprint: sha256, first 12 hex.
def fp(token: str) -> str:
    import hashlib
    return "fp:" + hashlib.sha256(token.encode()).hexdigest()[:12]


def kc_event(type_: str, client: str, *, audience: str | None = None,
             error: str | None = None, grant: str | None = None,
             when: int = 1763632809000, user: str = SUB) -> str:
    details: dict[str, str] = {}
    if audience:
        details["audience"] = audience
    if grant:
        details["grant_type"] = grant
    event = {"type": type_, "clientId": client, "userId": user, "details": details,
             "time": when}
    if error:
        event["error"] = error
    return json.dumps(event)


class MonitorHarness:
    """One monitor process fed a scripted list of (source, line) pairs."""

    def __init__(self) -> None:
        self.state = tempfile.NamedTemporaryFile(prefix="monitor-test.", delete=False)
        self.state.close()

    def run(self, lines, request_id: str = "") -> str:
        script = textwrap.dedent(
            """
            export MONITOR_LIB=1
            export MONITOR_STATE_FILE="$STATE"
            export REQUEST_ID="$RID"
            export LC_ALL=C.UTF-8
            source "$LIB"
            while IFS= read -r encoded; do
              parse_line "${encoded%%|*}" "${encoded#*|}"
            done
            """
        )
        env = dict(os.environ, STATE=self.state.name, RID=request_id, LIB=str(LIB),
                   TZ="UTC")
        payload = "\n".join(f"{src}|{line}" for src, line in lines) + "\n"
        proc = subprocess.run(
            ["bash", "-c", script], input=payload, capture_output=True,
            text=True, env=env, check=False,
        )
        self.stderr = proc.stderr
        return ANSI.sub("", proc.stdout)

    def close(self) -> None:
        Path(self.state.name).unlink(missing_ok=True)


@unittest.skipUnless(shutil.which("bash"), "bash is required to replay the monitor")
class MonitorReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = MonitorHarness()

    def tearDown(self) -> None:
        self.monitor.close()

    def test_acme_mint_and_dispatch_name_token_a_and_its_identity(self) -> None:
        out = self.monitor.run([
            ("acme", f'2026-11-20 10:00:01,120 INFO [acme] acme_audit request_id=r1 '
                     f'actor=acme-agent action="mint Keycloak Token A" outcome=ok '
                     f'expires_in=300 token={fp(A)}'),
            ("acme", f'2026-11-20 10:00:01,180 INFO [acme] acme_audit request_id=r1 '
                     f'actor=acme-agent action="dispatch A2A query" outcome=sent '
                     f'token={fp(A)} source=minted exp=2030-03-17T00:00:00'),
        ])
        self.assertIn("TOKEN A MINT [10:00:01]", out)
        self.assertIn("azp=acme-agent", out)
        self.assertIn("aud=mas-a2a", out)
        self.assertIn("EXCHANGE: n/a", out)
        # A card must be stamped with the event time, not the token's exp=.
        self.assertIn("ACME DISPATCH [10:00:01]", out)
        self.assertIn("on behalf of the caller", out)
        self.assertNotIn("ACME DISPATCH [00:00:00]", out)

    def test_exchange_chain_reports_client_audience_and_subject(self) -> None:
        out = self.monitor.run([
            ("mas", "2026-11-20 10:00:05,004 INFO [inbound.a2a.middleware] A2A HTTP request "
                    "completed context={'request_id': 'db37d282', 'method': 'POST', "
                    f"'path': '/a2a/jsonrpc', 'status_code': 200, 'sub': '{SUB}'}}"),
            ("kc", kc_event("TOKEN_EXCHANGE", "mas-a2a", audience="lightspeed-hub-a2a")),
            ("kc", kc_event("TOKEN_EXCHANGE", "lightspeed-hub-a2a",
                            audience="praxis-spoke-gateway", when=1763632809500)),
            ("kc", kc_event("TOKEN_EXCHANGE", "praxis-spoke-gateway",
                            audience="praxis-spoke-hub", when=1763632812500)),
        ])
        for hop, client, audience in (
            ("A ──▶ H", "mas-a2a", "lightspeed-hub-a2a"),
            ("H ──▶ R", "lightspeed-hub-a2a", "praxis-spoke-gateway"),
            ("R ──▶ U", "praxis-spoke-gateway", "praxis-spoke-hub"),
        ):
            self.assertIn(f"TOKEN EXCHANGE {hop}", out)
            self.assertIn(f"CLIENT  : {client}", out)
            self.assertIn(f"AUDIENCE: {audience}", out)
        # The subject rides each exchange card: identity, not just token names.
        # (The MAS inbound card also reports it, once.)
        self.assertEqual(out.count("SUBJECT : " + SUB), 4)
        self.assertIn("the actor is now praxis-spoke-gateway", out)

    def test_failed_exchange_is_red_and_names_the_hop(self) -> None:
        out = self.monitor.run([
            ("kc", kc_event("TOKEN_EXCHANGE_ERROR", "lightspeed-hub-a2a",
                            audience="praxis-spoke-gateway", error="invalid_client")),
        ])
        self.assertIn("KEYCLOAK TOKEN_EXCHANGE_ERROR", out)
        self.assertIn("HOP     : H → R", out)
        self.assertIn("ERROR   : invalid_client", out)
        self.assertIn("Keycloak refused the grant", out)

    def test_client_login_mint_cards_and_event_ids_dedupe_refetches(self) -> None:
        """client_credentials grants arrive as CLIENT_LOGIN (live 2026-11-25).

        The poller's 30 s lookback re-fetches events, so every event carries
        Keycloak's unique id and a re-fetch must not re-card (nor print a
        "confirmed" corroboration for an already-carded exchange).
        """
        mint = json.dumps({"type": "CLIENT_LOGIN", "clientId": "acme-agent",
                           "userId": SUB, "details": {"grant_type": "client_credentials"},
                           "time": 1763632809000, "id": "ev-mint-1"})
        exch = json.dumps({"type": "TOKEN_EXCHANGE", "clientId": "mas-a2a",
                           "userId": SUB, "details": {"audience": "lightspeed-hub-a2a"},
                           "time": 1763632810000, "id": "ev-exch-1"})
        out = self.monitor.run([("kc", mint), ("kc", mint), ("kc", exch), ("kc", exch)])
        self.assertEqual(out.count("KEYCLOAK MINT A"), 1)
        self.assertIn("GRANT   : client_credentials", out)
        self.assertEqual(out.count("TOKEN EXCHANGE A ──▶ H"), 1)
        self.assertNotIn("hop already carded", out)

    def test_mint_event_reports_the_authenticating_client(self) -> None:
        out = self.monitor.run([
            ("kc", kc_event("LOGIN", "acme-agent", grant="client_credentials")),
        ])
        self.assertIn("KEYCLOAK MINT A", out)
        self.assertIn("WHO     : acme-agent", out)
        self.assertIn("SUBJECT : " + SUB, out)

    def test_keycloak_public_client_events_stay_quiet(self) -> None:
        out = self.monitor.run([("kc", kc_event("LOGIN", "security-admin-console"))])
        self.assertNotIn("┌─", out)

    def test_praxis_hub_and_spoke_are_told_apart_by_pod_name(self) -> None:
        out = self.monitor.run([
            ("praxis", "[praxis-proxy/praxis-proxy-7d9c-x2:praxis] 2026-11-20T10:00:08Z "
                       "DEBUG praxis_filter: route matched path=/a2a cluster=hub-adapter"),
            ("praxis", "[praxis-proxy/praxis-spoke-hub-6b8-zz:praxis] 2026-11-20T10:00:12Z "
                       "DEBUG praxis_filter: route matched path=/api/v1/namespaces/x/pods "
                       "cluster=spoke-api"),
        ])
        self.assertIn("PRAXIS HUB ROUTE", out)
        self.assertIn("wearing A", out)
        self.assertIn("PRAXIS SPOKE ROUTE [10:00:12] (spoke:hub-6b8-zz)", out)
        self.assertIn("wearing R", out)

    def test_spoke_403_explains_the_exchanged_azp_allow_list(self) -> None:
        out = self.monitor.run([
            ("praxis", "[praxis-proxy/praxis-spoke-hub-6b8-zz:praxis] 2026-11-20T10:00:12Z "
                       'INFO request rejected by filter filter="policy" status=403'),
        ])
        self.assertIn("PRAXIS REJECT [10:00:12] 403 (spoke:hub-6b8-zz)", out)
        self.assertIn("spokeGatewayConfig.allowedCallers", out)

    def test_adapter_cards_cover_run_creation_token_store_and_renewal(self) -> None:
        out = self.monitor.run([
            ("hub", '[lightspeed-hub/lightspeed-hub-5c6:adapter] 2026-11-20T10:00:09Z '
                    'level=info msg="created AgenticRun a2a-1410731 for target hub"'),
            ("hub", '[lightspeed-hub/lightspeed-hub-5c6:adapter] 2026-11-20T10:00:09Z '
                    'level=info msg="stored run token in secret ls-run-token-a2a-1410731"'),
            ("hub", '[lightspeed-hub/lightspeed-hub-5c6:adapter] 2026-11-20T10:00:10Z '
                    'level=warn msg="task paused for credential renewal"'),
            ("hub", '[lightspeed-hub/lightspeed-hub-5c6:adapter] 2026-11-20T10:00:15Z '
                    'level=info msg="renewed run credential"'),
        ])
        self.assertIn("ADAPTER AGENTICRUN", out)
        self.assertIn("AS THE CALLER with H", out)
        self.assertIn("ADAPTER RUN-TOKEN SECRET", out)
        self.assertIn("writes R into the Secret", out)
        self.assertIn("ADAPTER PAUSED: credential renewal", out)
        # The renewal is an exchange H→R, not a card about the pause.
        self.assertIn("TOKEN EXCHANGE H ──▶ R", out)

    def test_operator_card_states_it_performs_no_exchange(self) -> None:
        out = self.monitor.run([
            ("operator", "[lightspeed-hub/lightspeed-hub-agentic-operator-1:manager] "
                         '2026-11-20T10:00:10Z INFO controller.agenticrun '
                         '"gateway credential mode enabled"'),
        ])
        self.assertIn("OPERATOR: GATEWAY CREDENTIAL MODE", out)
        self.assertIn("NO EXCHANGE (by design)", out)
        self.assertIn("tokenFile = mounted R", out)

    def test_praxis_timestamp_survives_ansi_wrapping(self) -> None:
        """Praxis wraps even its leading timestamp in ANSI escapes.

        The strip used to run AFTER stamp extraction, so the anchored
        timestamp regex never matched and every praxis card fell back to
        "~" receipt time (cross-hop order unreadable).
        """
        out = self.monitor.run([
            ("praxis", "[praxis-proxy/praxis-proxy-7d9c-x2:praxis] \x1b[2m2026-11-20T10:00:08Z\x1b[0m"
                       " \x1b[34mDEBUG\x1b[0m praxis_filter: route matched path=/a2a cluster=hub-adapter"),
        ])
        self.assertIn("PRAXIS HUB ROUTE [10:00:08]", out)
        self.assertNotIn("PRAXIS HUB ROUTE [~", out)

    def test_praxis_identity_plugin_internals_are_silent(self) -> None:
        """The plugin's module name contains "jwt" — not a denial.

        Live 2026-11-25: JWKS refreshes and claim-map misses carded as bare
        "PRAXIS" noise because the denial matcher had an unanchored `jwt`.
        """
        out = self.monitor.run([
            ("praxis", "[praxis-proxy/praxis-proxy-7d9c-x2:praxis] 2026-11-25T10:57:51Z "
                       "INFO praxis_policy_plugin_identity_jwt::resolver: "
                       "refreshed JWKS on demand plugin=jwt-client"),
            ("praxis", "[praxis-proxy/praxis-spoke-hub-6b8-zz:praxis] 2026-11-25T10:57:51Z "
                       "DEBUG praxis_policy_plugin_identity_jwt::configured_mapper: "
                       'claim map: no candidate resolved for these fields fields=["roles"]'),
        ])
        self.assertNotIn("┌─", out)

    def test_runcred_secret_appearance_cards_h_to_r_and_dedupes(self) -> None:
        """The Secret watcher is the only witness of "exchange H→R + write".

        The adapter never logs the Secret's creation (only its deletion), so
        `oc get secrets -w` rows must card the exchange and the store — once,
        because the watch reprints rows on every change.
        """
        row = "ls-run-token-db01da4d-93b3-4789-836c-3a101079b160 2026-11-25T07:27:44Z"
        out = self.monitor.run([("runcred", row), ("runcred", row)])
        self.assertEqual(out.count("ADAPTER RUN-TOKEN SECRET (stored)"), 1)
        self.assertIn("ADAPTER RUN-TOKEN SECRET (stored) [07:27:44]", out)
        self.assertIn("ls-run-token-db01da4d-93b3-4789-836c-3a101079b160", out)
        # The Secret's existence proves the H→R exchange happened.
        self.assertIn("TOKEN EXCHANGE H ──▶ R", out)
        self.assertIn("run-token Secret appeared", out)

    def test_operator_reconcile_churn_is_silent_and_phases_dedupe(self) -> None:
        """Framework requeue spam must not bury the real operator events.

        Live 2026-11-25: ~15 near-identical OPERATOR SANDBOX cards per run
        (reconciling / handling analysis / already in progress, waiting).
        """
        base = ('[lightspeed-hub/lightspeed-hub-agentic-operator-1:manager] '
                '2026-11-25T07:27:4{}Z\tDEBUG\t{}\t{{"controller": "agenticrun", '
                '"AgenticRun": {{"name":"a2a-71bfcaaafe0f87b23574e06aa33089c233dfd6a1"}}, '
                '"reconcileID": "ae5d2592-51fb-4ccf-ab63-7a92ef7621e0"}}')
        out = self.monitor.run([
            ("operator", base.format(4, "reconciling")),
            ("operator", base.format(4, "handling analysis")),
            ("operator", base.format(4, "analysis already in progress, waiting")),
            ("operator", base.format(6, "handling analysis")),
        ])
        # Churn is gone entirely; the phase transition cards exactly once.
        self.assertNotIn("reconciling", out)
        self.assertNotIn("already in progress", out)
        self.assertEqual(out.count("OPERATOR SANDBOX"), 1)

    def test_duplicate_exchange_card_is_suppressed_when_keycloak_confirms(self) -> None:
        # Normal delivery order: Keycloak's event reaches the monitor first.
        out = self.monitor.run([
            ("kc", kc_event("TOKEN_EXCHANGE", "mas-a2a", audience="lightspeed-hub-a2a")),
            ("praxis", "[praxis-proxy/praxis-proxy-7d9c-x2:praxis] 2026-11-20T10:00:08Z "
                       "DEBUG delegator: delegating token for audience lightspeed-hub-a2a"),
        ])
        self.assertEqual(out.count("TOKEN EXCHANGE A ──▶ H"), 1)
        self.assertIn("already recorded by Keycloak", out)

    def test_keycloak_card_is_dropped_when_a_pod_reported_it_first(self) -> None:
        out = self.monitor.run([
            ("praxis", "[praxis-proxy/praxis-proxy-7d9c-x2:praxis] 2026-11-20T10:00:08Z "
                       "DEBUG delegator: delegating token for audience lightspeed-hub-a2a"),
            ("kc", kc_event("TOKEN_EXCHANGE", "mas-a2a", audience="lightspeed-hub-a2a")),
        ])
        # One exchange card (the pod's), plus Keycloak's corroboration line —
        # never a second, thinner card for the same grant.
        self.assertEqual(out.count("┌─ TOKEN EXCHANGE A"), 1)
        self.assertIn("KEYCLOAK confirmed A → H", out)

    def test_public_agent_card_fetches_never_open_a_chain(self) -> None:
        out = self.monitor.run([
            ("mas", "2026-11-20 10:00:00,001 INFO [inbound.a2a.middleware] A2A HTTP "
                    "request completed context={'path': '/a2a/.well-known/agent-card.json', "
                    "'status_code': 200}"),
        ])
        self.assertNotIn("MAS A2A INBOUND", out)

    def test_worker_passthrough_says_mas_acts_for_the_caller(self) -> None:
        out = self.monitor.run([
            ("worker", "2026-11-20 10:00:07,000 DEBUG [a2a_agent_node_factory] "
                       "A2A agent node [https://praxis/apps/a2a]: using user auth token"),
        ])
        self.assertIn("TOKEN A PASSTHROUGH", out)
        self.assertIn("mas-worker acting on behalf of the caller", out)

    def test_planner_grant_is_marked_as_not_the_callers_token(self) -> None:
        out = self.monitor.run([
            ("worker", '2026-11-20 10:00:06,000 INFO [workplan] tool.execute '
                       "name=workplan.plan_and_delegate tool_args={'agent': 'hub'}"),
        ])
        self.assertIn("ORCHESTRATOR PLAN", out)
        self.assertIn("not the caller's identity", out)

    def test_request_id_filter_keeps_only_matching_records(self) -> None:
        out = self.monitor.run([
            ("acme", f'2026-11-20 10:00:01,120 INFO [acme] acme_audit request_id=keep-me '
                     f'actor=acme-agent action="mint Keycloak Token A" outcome=ok token={fp(A)}'),
            ("acme", f'2026-11-20 10:00:02,120 INFO [acme] acme_audit request_id=other '
                     f'actor=acme-agent action="mint Keycloak Token A" outcome=ok token={fp(H)}'),
        ], request_id="keep-me")
        self.assertEqual(out.count("TOKEN A MINT"), 1)
        self.assertIn(fp(A), out)

    def test_no_token_is_ever_printed(self) -> None:
        out = self.monitor.run([
            ("acme", f'2026-11-20 10:00:01,120 INFO [acme] acme_audit request_id=r1 '
                     f'actor=acme-agent action="mint Keycloak Token A" outcome=ok token={fp(A)}'),
            ("hub", f'[lightspeed-hub/lightspeed-hub-5c6:adapter] 2026-11-20T10:00:09Z '
                    f'level=debug msg="inbound Authorization: Bearer {A}"'),
            ("praxis", f"[praxis-proxy/praxis-spoke-hub-6b8:praxis] 2026-11-20T10:00:12Z "
                       f"DEBUG auth: bearer {R}"),
        ])
        for token in (A, H, R, U):
            self.assertNotIn(token, out)


    def test_token_a_reaching_the_adapter_is_flagged_suspect(self) -> None:
        # ACME minted A; the hub gateway never exchanged it; the adapter then
        # logs the bearer it received — which is A.
        out = self.monitor.run([
            ("acme", f'2026-11-20 10:00:01,120 INFO [acme] acme_audit request_id=r1 '
                     f'actor=acme-agent action="mint Keycloak Token A" outcome=ok token={fp(A)}'),
            ("praxis", "[praxis-proxy/praxis-proxy-7d9c-x2:praxis] 2026-11-20T10:00:08Z "
                       "DEBUG praxis_filter: route matched path=/a2a cluster=hub-adapter"),
            ("hub", f'[lightspeed-hub/lightspeed-hub-5c6:adapter] 2026-11-20T10:00:09Z '
                    f'level=debug msg="inbound Authorization: Bearer {A}"'),
        ])
        self.assertIn("SUSPECT: A STILL WORN AT THE ADAPTER", out)
        self.assertIn("exchangeAtPraxis", out)

    def test_exchange_then_later_exchange_still_renders_both_hops(self) -> None:
        # A→H at the hub gateway, H→R at the adapter: the second must not be
        # mistaken for a repeat of the first (it is a different hop).
        out = self.monitor.run([
            ("kc", kc_event("TOKEN_EXCHANGE", "mas-a2a", audience="lightspeed-hub-a2a")),
            ("hub", '[lightspeed-hub/lightspeed-hub-5c6:adapter] 2026-11-20T10:00:09Z '
                    'level=info msg="token exchange ok: audience=praxis-spoke-gateway"'),
        ])
        self.assertEqual(out.count("TOKEN EXCHANGE A ──▶ H"), 1)
        self.assertEqual(out.count("TOKEN EXCHANGE H ──▶ R"), 1)

    def test_renewal_replaces_a_without_relabelling_the_refused_token(self) -> None:
        base = '2026-11-20 10:00:0{} , INFO [acme] acme_audit request_id=r1 '
        out = self.monitor.run([
            ("acme", f'2026-11-20 10:00:01,000 INFO [acme] acme_audit request_id=r1 '
                     f'actor=acme-agent action="mint Keycloak Token A" outcome=ok token={fp(A)}'),
            ("acme", f'2026-11-20 10:00:02,000 INFO [acme] acme_audit request_id=r1 '
                     f'action="renew Token A" outcome=ok reason=http_401 '
                     f'renewed_from={fp(A)} token={fp(H)} source=minted override=-'),
        ])
        self.assertIn("ACME TOKEN RENEWAL", out)
        self.assertIn(f"{fp(A)} ──▶ {fp(H)}", out)
        # A 401 renewal is a refusal: the card must say so.
        self.assertIn("downstream refused the current token", out)
        # A renewal is not an A→H exchange: no exchange card may claim it.
        self.assertNotIn("TOKEN EXCHANGE A ──▶ H", out)

    def test_auth_required_renewal_is_a_task_resume_not_a_refusal(self) -> None:
        """MAS pausing an ongoing task (near-expiry A) must not read as a 401.

        The reason value also carries spaces ("task auth-required"); an
        \\S+ matcher truncated it to "task" and the card hardcoded the
        refusal text, which made a routine mid-task renewal look like the
        request had been rejected and re-sent.
        """
        out = self.monitor.run([
            ("acme", f'2026-11-20 10:00:01,000 INFO [acme] acme_audit request_id=r1 '
                     f'actor=acme-agent action="mint Keycloak Token A" outcome=ok token={fp(A)}'),
            ("acme", f'2026-11-20 10:00:02,000 INFO [acme] acme_audit request_id=r1 '
                     f'action="renew Token A" outcome=ok reason=task auth-required '
                     f'renewed_from={fp(A)} token={fp(H)} source=minted override=-'),
        ])
        self.assertIn("ACME TOKEN RENEWAL", out)
        # Full multi-word reason, not just its first word.
        self.assertIn("REASON  : task auth-required", out)
        self.assertIn("MAS paused the ongoing task", out)
        self.assertIn("resumes the SAME task", out)
        self.assertNotIn("downstream refused the current token", out)

    def test_public_readiness_probes_are_silent_on_every_hop(self) -> None:
        out = self.monitor.run([
            ("praxis", '[praxis-proxy/praxis-proxy-7d9c-x2:praxis] 2026-11-20T10:00:00Z '
                       'DEBUG path="/readiness" status=200'),
            ("hub", '[lightspeed-hub/lightspeed-hub-5c6:adapter] 2026-11-20T10:00:01Z '
                    'level=info msg="GET /healthz 200"'),
            ("operator", '[lightspeed-hub/lightspeed-hub-agentic-operator-1:manager] '
                         '2026-11-20T10:00:02Z INFO healthz ok'),
        ])
        self.assertNotIn("┌─", out)


    def test_hub_route_verifies_the_caller_token(self) -> None:
        out = self.monitor.run([
            ("acme", f'2026-11-20 10:00:01,120 INFO [acme] acme_audit request_id=r1 '
                     f'actor=acme-agent action="mint Keycloak Token A" outcome=ok token={fp(A)}'),
            ("praxis", "[praxis-proxy/praxis-proxy-7d9:x] 2026-11-20T10:00:08Z DEBUG "
                       "praxis_filter: route matched path=/a2a cluster=hub-adapter"),
        ])
        self.assertIn("IDENTITY: A — A (caller token) is what this hop may see", out)

    def test_two_spokes_are_told_apart_and_each_verified(self) -> None:
        # Two spokes are registered upstream (hub + mario-ipi). The R→U exchange
        # produces a PER-SPOKE audience; the monitor must name the right one.
        out = self.monitor.run([
            ("praxis", "[praxis-proxy/praxis-spoke-hub-1:x] 2026-11-20T10:00:12Z DEBUG "
                       "praxis_filter: route matched path=/api/v1/pods cluster=spoke-api"),
            ("praxis", "[praxis-proxy/praxis-spoke-mario-ipi-1:x] 2026-11-20T10:00:13Z DEBUG "
                       "praxis_filter: route matched path=/api/v1/pods cluster=spoke-api"),
            ("kc", kc_event("TOKEN_EXCHANGE", "praxis-spoke-gateway",
                            audience="praxis-spoke-mario-ipi", when=1763632813000)),
        ])
        self.assertIn("(spoke:hub-1)", out)
        self.assertIn("(spoke:mario-ipi-1)", out)
        self.assertIn("wearing R for spoke mario-ipi-1", out)
        self.assertIn("AUDIENCE: praxis-spoke-mario-ipi", out)

    def test_hub_exchange_idp_rejection_is_explained(self) -> None:
        out = self.monitor.run([
            ("praxis", "[praxis-proxy/praxis-proxy-7d9:x] 2026-11-20T10:00:08Z INFO "
                       "praxis_filter: request rejected by filter filter=\"policy\" status=403"),
            ("praxis", "[praxis-proxy/praxis-proxy-7d9:x] 2026-11-20T10:00:08Z WARN "
                       "delegation.idp_rejected: token exchange rejected: unauthorized_client"),
            ("kc", kc_event("TOKEN_EXCHANGE_ERROR", "mas-a2a",
                            audience="lightspeed-hub-a2a", error="invalid_client_credentials")),
        ])
        self.assertIn("delegation.idp_rejected", out)
        self.assertIn("A→H EXCHANGE failed at Keycloak", out)
        self.assertIn("CLIENT  : mas-a2a", out)
        self.assertIn("ERROR   : invalid_client_credentials", out)

    def test_mas_403_points_at_the_process_cached_allow_list(self) -> None:
        # 2026-11-24: the Secret had the new sub, the running pod did not.
        out = self.monitor.run([
            ("mas", "2026-11-20 10:00:05,004 INFO [inbound.a2a.middleware] A2A HTTP request "
                    "completed context={'request_id': 'db37', 'method': 'POST', "
                    f"'path': '/a2a/jsonrpc', 'status_code': 403, 'sub': '{SUB}'}}"),
        ])
        self.assertIn("MAS A2A INBOUND", out)
        self.assertIn("403", out)
        self.assertIn("NOT a refreshed environment", out)
        self.assertIn("oc -n mao rollout restart deployment/mao-a2a-mas-a2a", out)

    def test_run_without_a_written_credential_is_reported(self) -> None:
        out = self.monitor.run([
            ("operator", "[lightspeed-hub/lightspeed-hub-agentic-operator-1:manager] "
                         "2026-11-20T10:00:10Z INFO controller.agenticrun "
                         'CredentialsExpired=True Secret ls-run-token-abc-123 not found'),
        ])
        self.assertIn("OPERATOR: RUN HAS NO CREDENTIAL", out)
        self.assertIn("AgenticRun EXISTS but the adapter never wrote its R Secret", out)

    def test_adapter_ws_exchange_failure_is_classified_as_tls(self) -> None:
        out = self.monitor.run([
            ("hub", '[lightspeed-hub/lightspeed-hub-5c6:adapter] 2026-11-20T10:00:09Z '
                    'level=error msg="token exchange failed kind=request_failed: '
                    'tls: failed to verify certificate"'),
        ])
        self.assertIn("ADAPTER TOKEN EXCHANGE FAILED (H→R)", out)
        self.assertIn("transport/TLS", out)
        self.assertIn("adapter.keycloak.caTrust", out)


class KeycloakFeedTest(unittest.TestCase):
    """The HTTP side of --kc, driven by a fake curl/oc on PATH.

    This is the code path the replay tests cannot reach: admin login, the
    event-config probe, the time window, and the process lifecycle.
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="monitor-kc.")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._write_fakes()
        self.state = os.path.join(self.dir, "state")

    def _write_fakes(self) -> None:
        curl = os.path.join(self.dir, "curl")
        with open(curl, "w") as fh:
            fh.write(
                "#!/usr/bin/env bash\n"
                'args="$*"\n'
                'case "$args" in\n'
                "  *\"/protocol/openid-connect/token\"*) echo '{\"access_token\":\"ADMIN-TOKEN\"}' ;;\n"
                "  *\"events/config\"*) echo '{\"eventsEnabled\":true,\"enabledEventTypes\":[\"TOKEN_EXCHANGE\"]}' ;;\n"
                "  *\"/events\"*)\n"
                "    now=$(date +%s%3N)\n"
                "    printf '[{\"type\":\"TOKEN_EXCHANGE\",\"clientId\":\"praxis-spoke-gateway\",\"userId\":\"U1\",\"details\":{\"audience\":\"praxis-spoke-hub\"},\"time\":%d},"
                "{\"type\":\"TOKEN_EXCHANGE\",\"clientId\":\"lightspeed-hub-a2a\",\"userId\":\"U1\",\"details\":{\"audience\":\"praxis-spoke-gateway\"},\"time\":%d},"
                "{\"type\":\"TOKEN_EXCHANGE\",\"clientId\":\"mas-a2a\",\"userId\":\"U1\",\"details\":{\"audience\":\"lightspeed-hub-a2a\"},\"time\":%d}]\\n'"
                " $((now)) $((now-500)) $((now-1000))  ;;\n"
                "  *) echo \"\" ;;\n"
                "esac\n"
            )
        oc = os.path.join(self.dir, "oc")
        with open(oc, "w") as fh:
            fh.write(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                "  *\"get secret\"*) printf '%s' \"$(printf admin | base64)\" ;;\n"
                "  *\"whoami\"*) echo system:admin ;;\n"
                "  *) echo \"\" ;;\n"
                "esac\n"
            )
        for path in (curl, oc):
            os.chmod(path, 0o755)

    def _env(self, **extra):
        return dict(os.environ, PATH=self.dir + ":/usr/bin:/bin",
                    MONITOR_STATE_FILE=self.state, KC_URL="https://keycloak.example.test",
                    MONITOR_KC_POLL="1", **extra)

    def test_login_probe_and_one_poll_render_the_whole_chain(self) -> None:
        body = (
            "cd %s\n"
            "source scripts/monitor-lib.sh\n"
            'base=$(kc_base_url); echo "BASE=$base"\n'
            'tok=$(kc_admin_token "$base"); echo "TOK=$tok"\n'
            'kc_probe "$base" "$tok"\n'
            'kc_events_since "$base" "$tok" "$(( $(date +%%s%%3N) - 2000 ))" '
            "| while IFS= read -r ev; do parse_line kc \"$ev\"; done\n"
        ) % ROOT
        proc = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                              env=self._env(), timeout=60, check=False)
        out = ANSI.sub("", proc.stdout)
        self.assertIn("BASE=https://keycloak.example.test", out)
        self.assertIn("TOK=ADMIN-TOKEN", out)
        self.assertIn("realm rca events=True", out)
        order = [out.find(n) for n in ("TOKEN EXCHANGE A ──▶ H",
                                       "TOKEN EXCHANGE H ──▶ R",
                                       "TOKEN EXCHANGE R ──▶ U")]
        self.assertTrue(all(i >= 0 for i in order), out)
        self.assertEqual(order, sorted(order), out)
        self.assertEqual(out.count("SUBJECT : U1"), 3)

    def test_poller_exits_on_sigterm(self) -> None:
        # `timeout` sends TERM to the shell only; the feed loop must stop at
        # its next wake-up instead of outliving the monitor.
        proc = subprocess.run(
            ["timeout", "4", "bash", "scripts/monitor.sh", "--kc"],
            cwd=ROOT, capture_output=True, text=True, timeout=60, check=False,
            env=self._env(),
        )
        self.assertEqual(proc.returncode, 124)
        leftovers = subprocess.run(["pgrep", "-f", "scripts/monitor.sh --kc"],
                                   capture_output=True, text=True)
        self.assertEqual(leftovers.stdout.strip(), "", "monitor outlived SIGTERM")


class MonitorEntryPointTest(unittest.TestCase):
    def test_help_and_endpoint_work_without_a_cluster(self) -> None:
        env = {"PATH": "/usr/bin:/bin"}
        for flag in ("--help", "--endpoint"):
            proc = subprocess.run(["bash", str(MAIN), flag], capture_output=True,
                                  text=True, env=env, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        endpoints = subprocess.run(["bash", str(MAIN), "--endpoint"],
                                   capture_output=True, text=True, env=env, check=True)
        for needle in ("lightspeed-hub", "praxis-proxy", "keycloak-admin-user",
                       "praxis-spoke"):
            self.assertIn(needle, endpoints.stdout)

    def test_shell_syntax(self) -> None:
        for path in (MAIN, LIB):
            proc = subprocess.run(["bash", "-n", str(path)], capture_output=True,
                                  text=True, check=False)
            self.assertEqual(proc.returncode, 0, f"{path}: {proc.stderr}")


if __name__ == "__main__":
    unittest.main()
