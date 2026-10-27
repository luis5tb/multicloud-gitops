#!/usr/bin/env python3
"""Drive MAS's inbound A2A endpoint through approval and token-renewal scenarios.

The script plays ACME's role: it sends tasks to MAS with Token A minted for
ACME's own principal (by default inside the ACME pod, through
``python -m acme_agent.mint_token``), then reacts to the task states MAS
reports. It uses only the Python standard library.

Scenarios (see docs in scripts/README.md):

  approval          MAS pauses in TASK_STATE_INPUT_REQUIRED at the approval
                    gate; reply with a decision and wait for the outcome.
  expired-at-door   An expired token is rejected with HTTP 401 before any
                    task exists.
  expiry-in-mas     Token A runs out while the approval waits; after the
                    approval MAS pauses in TASK_STATE_AUTH_REQUIRED before the
                    next step, and continues once a fresh token is sent.

Approval and expiry-in-mas need a blueprint with an approval gate before the
OLS step (``setup-mas-blueprint.sh multi-cluster-hitl``) and a prompt that
makes MAS delegate to OLS, including the target cluster API URL.

Example:

  ./scripts/a2a-renewal-scenarios.py \\
      --mas-url https://mao-a2a-mas-a2a-mao.apps.example.com \\
      --prompt "On https://api.example.com:6443 list failing pods in openshift-monitoring" \\
      --scenario all
"""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

TERMINAL = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
}
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
DEFAULT_TOKEN_CMD = (
    "oc exec -n acme-agent deploy/acme-agent -- python -m acme_agent.mint_token"
)


class ScenarioFailed(Exception):
    """A scenario observed something other than the expected behavior."""


def jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying it (expiry and subject only)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


def seconds_left(token: str) -> float:
    """Seconds until the token's ``exp``; infinity when it has none."""
    exp = jwt_claims(token).get("exp")
    return float(exp) - time.time() if isinstance(exp, (int, float)) else float("inf")


@dataclass
class Client:
    """Minimal A2A 1.0 JSON-RPC client for MAS."""

    base_url: str
    token_cmd: str
    context: ssl.SSLContext | None
    poll_interval: float
    log: list[str] = field(default_factory=list)
    _poll_token: str = ""

    def mint(self) -> str:
        """Mint a fresh Token A for ACME's principal."""
        result = subprocess.run(
            shlex.split(self.token_cmd), capture_output=True, text=True, check=False
        )
        token = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if result.returncode != 0 or token.count(".") != 2:
            raise ScenarioFailed(
                f"token command failed (rc={result.returncode}): {result.stderr.strip()[:300]}"
            )
        return token

    def poll_token(self) -> str:
        """A token for reads, re-minted shortly before it expires."""
        if not self._poll_token or seconds_left(self._poll_token) < 30:
            self._poll_token = self.mint()
        return self._poll_token

    def rpc(self, method: str, params: dict, token: str) -> tuple[int, dict]:
        """POST one JSON-RPC call; return (HTTP status, JSON body)."""
        body = json.dumps(
            {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
        ).encode()
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/a2a/jsonrpc",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "A2A-Version": "1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=60) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read() or b"{}")
            except ValueError:
                payload = {}
            return error.code, payload

    def send(
        self,
        text: str,
        token: str,
        *,
        task_id: str = "",
        context_id: str = "",
    ) -> dict:
        """Send a message (returnImmediately) and return the resulting task."""
        message = {
            "messageId": uuid.uuid4().hex,
            "role": "ROLE_USER",
            "parts": [{"text": text}],
        }
        if task_id:
            message["taskId"] = task_id
        if context_id:
            message["contextId"] = context_id
        status, payload = self.rpc(
            "SendMessage",
            {"message": message, "configuration": {"returnImmediately": True}},
            token,
        )
        if status != 200 or "error" in payload:
            raise ScenarioFailed(f"SendMessage failed: HTTP {status} {payload.get('error')}")
        return payload["result"]["task"]

    def get(self, task_id: str) -> dict:
        """Fetch a task with a valid read token."""
        status, payload = self.rpc("GetTask", {"id": task_id}, self.poll_token())
        if status != 200 or "error" in payload:
            raise ScenarioFailed(f"GetTask failed: HTTP {status} {payload.get('error')}")
        return payload["result"]

    def wait(self, task: dict, states: set[str], timeout: float) -> dict:
        """Poll until the task reaches one of ``states`` (or any terminal state)."""
        deadline = time.monotonic() + timeout
        last = ""
        while True:
            state = task["status"]["state"]
            if state != last:
                note = status_text(task)
                say(f"    task {task['id'][:8]} -> {state}" + (f": {note}" if note else ""))
                last = state
            if state in states or state in TERMINAL:
                return task
            if time.monotonic() >= deadline:
                raise ScenarioFailed(f"timed out waiting for {sorted(states)}; last {state}")
            time.sleep(self.poll_interval)
            task = self.get(task["id"])


def status_text(task: dict) -> str:
    """Text of the task's status message, shortened."""
    parts = task.get("status", {}).get("message", {}).get("parts", [])
    text = " ".join(part.get("text", "") for part in parts).strip()
    return text[:200] + ("..." if len(text) > 200 else "")


def say(line: str) -> None:
    print(line, flush=True)


def expect(task: dict, state: str, what: str) -> None:
    if task["status"]["state"] != state:
        raise ScenarioFailed(
            f"expected {state} {what}, got {task['status']['state']}: {status_text(task)}"
        )


def hold_until_expiring(token: str, min_validity: float, slack: float = 5) -> None:
    """Sleep until ``token`` has less than ``min_validity`` seconds left."""
    wait = seconds_left(token) - min_validity + slack
    if wait == float("inf"):
        raise ScenarioFailed("Token A has no exp claim; cannot plan expiry")
    if wait > 0:
        say(f"    holding the approval {int(wait)}s until Token A is within "
            f"{int(min_validity)}s of expiry...")
        time.sleep(wait)


def renew(client: Client, task: dict, refused: str) -> dict:
    """Answer AUTH_REQUIRED with a freshly minted token on the same task."""
    fresh = client.mint()
    if fresh == refused:
        raise ScenarioFailed("token command returned the refused token again")
    say("    sending a renewal message with a fresh Token A")
    return client.send(
        "Renewed credentials attached.", fresh,
        task_id=task["id"], context_id=task["contextId"],
    )


def scenario_approval(client: Client, args: argparse.Namespace) -> None:
    token = client.mint()
    task = client.send(args.prompt, token)
    task = client.wait(task, {INPUT_REQUIRED}, args.timeout)
    expect(task, INPUT_REQUIRED, "at the approval gate")
    say(f"    replying '{args.decision}'")
    task = client.send(
        args.decision, client.mint(), task_id=task["id"], context_id=task["contextId"]
    )
    task = client.wait(task, TERMINAL, args.timeout)
    expect(task, "TASK_STATE_COMPLETED", "after the decision")


def scenario_expired_at_door(client: Client, args: argparse.Namespace) -> None:
    token = args.expired_token or ""
    if not token:
        token = client.mint()
        wait = seconds_left(token) + 2
        say(f"    waiting {int(wait)}s for a freshly minted token to expire...")
        time.sleep(max(wait, 0))
    elif seconds_left(token) > 0:
        raise ScenarioFailed("--expired-token has not expired yet")
    status, payload = client.rpc(
        "SendMessage",
        {"message": {"messageId": uuid.uuid4().hex, "role": "ROLE_USER",
                     "parts": [{"text": args.prompt}]},
         "configuration": {"returnImmediately": True}},
        token,
    )
    say(f"    MAS answered HTTP {status}")
    if status != 401:
        raise ScenarioFailed(f"expected HTTP 401 for an expired token, got {status}: {payload}")


def scenario_expiry_in_mas(client: Client, args: argparse.Namespace) -> None:
    token_a = client.mint()
    say(f"    Token A expires in {int(seconds_left(token_a))}s")
    task = client.send(args.prompt, token_a)
    task = client.wait(task, {INPUT_REQUIRED}, args.timeout)
    expect(task, INPUT_REQUIRED, "at the approval gate")
    hold_until_expiring(token_a, args.min_validity)
    say("    approving")
    task = client.send(
        "approve", client.mint(), task_id=task["id"], context_id=task["contextId"]
    )
    task = client.wait(task, {AUTH_REQUIRED}, args.timeout)
    expect(task, AUTH_REQUIRED, "before the next step")
    task = renew(client, task, token_a)
    task = client.wait(task, TERMINAL, args.timeout)
    expect(task, "TASK_STATE_COMPLETED", "after renewal")


SCENARIOS = {
    "approval": scenario_approval,
    "expired-at-door": scenario_expired_at_door,
    "expiry-in-mas": scenario_expiry_in_mas,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1],
    )
    parser.add_argument("--mas-url", required=True, help="MAS A2A base URL (no /a2a/jsonrpc)")
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--prompt", required=True,
                        help="Request that makes MAS delegate to OLS (include the cluster URL)")
    parser.add_argument("--token-cmd", default=DEFAULT_TOKEN_CMD,
                        help="Command printing a fresh Token A (default: mint inside ACME)")
    parser.add_argument("--provider", choices=["keycloak", "redhat"], default="keycloak",
                        help="Appended to the default token command")
    parser.add_argument("--environment", choices=["production", "stage"], default="stage",
                        help="Red Hat SSO environment for --provider redhat")
    parser.add_argument("--decision", default="approve", help="Reply for the approval scenario")
    parser.add_argument("--expired-token", default="",
                        help="Already-expired token for expired-at-door (else wait one lifetime)")
    parser.add_argument("--min-validity", type=float, default=60,
                        help="MAS worker CREDENTIAL_RENEWAL_MIN_VALIDITY_SECONDS")
    parser.add_argument("--timeout", type=float, default=900, help="Seconds to wait per state")
    parser.add_argument("--poll-interval", type=float, default=3)
    parser.add_argument("--ca-bundle", default="", help="CA bundle for MAS TLS")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification (testing)")
    args = parser.parse_args()

    if args.token_cmd == DEFAULT_TOKEN_CMD and args.provider == "redhat":
        args.token_cmd += f" --provider redhat --environment {args.environment}"

    if args.insecure:
        context = ssl._create_unverified_context()  # noqa: S323 -- explicit test opt-in
    elif args.ca_bundle:
        context = ssl.create_default_context(cafile=args.ca_bundle)
    else:
        context = None
    client = Client(args.mas_url, args.token_cmd, context, args.poll_interval)

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = {}
    for name in names:
        say(f"== {name}")
        started = time.monotonic()
        try:
            SCENARIOS[name](client, args)
            results[name] = "PASS"
        except ScenarioFailed as error:
            results[name] = f"FAIL: {error}"
        except KeyboardInterrupt:
            results[name] = "INTERRUPTED"
            break
        say(f"   {results[name]} ({int(time.monotonic() - started)}s)")

    say("\nSummary")
    for name, result in results.items():
        say(f"  {name:18} {result}")
    return 0 if all(result == "PASS" for result in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
