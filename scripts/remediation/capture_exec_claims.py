#!/usr/bin/env python3
"""Capture the claims of a run's R and R_exec tokens for the token gate.

Usage: capture_exec_claims.py <ls-run-token-secret> <agenticrun> <timeout-s>

Polls the run-token Secret in lightspeed-hub and decodes selected claims of
token-a and token-execution from the SAME read that first contains
token-execution. Never prints a token. Exits non-zero on timeout, or when
Execution finishes or the Secret disappears before the capture. Stdlib only;
needs `oc` logged in to the hub. See docs/lightspeed-hub-deployment-runbook.md
B3 (token-separation gate).
"""

import base64
import json
import subprocess
import sys
import time

secret, run, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
deadline = time.monotonic() + timeout


def oc_json(*args):
    remaining = deadline - time.monotonic()
    try:
        out = subprocess.run(
            ["oc", "-n", "lightspeed-hub", "get", *args, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=max(remaining, 1),
        )
    except subprocess.TimeoutExpired:
        sys.exit("FAIL: oc call exceeded the observer deadline; use a fresh run")
    return json.loads(out.stdout) if out.returncode == 0 else None


def claims(raw):
    payload = base64.b64decode(raw).decode().split(".")[1]
    payload += "=" * (-len(payload) % 4)
    c = json.loads(base64.urlsafe_b64decode(payload))
    return {k: c.get(k) for k in ("aud", "azp", "sub", "exp", "groups")}


while time.monotonic() < deadline:
    secret_obj = oc_json("secret", secret)
    if secret_obj is None:
        sys.exit(
            "FAIL: run-token Secret is gone before token-execution was seen; use a fresh run"
        )
    data = secret_obj.get("data", {})
    if data.get("token-execution"):
        print(
            json.dumps(
                {
                    "R": claims(data["token-a"]),
                    "R_exec": claims(data["token-execution"]),
                },
                indent=2,
            )
        )
        sys.exit(0)
    run_obj = oc_json("agenticrun", run) or {}
    for cond in run_obj.get("status", {}).get("conditions", []):
        if cond.get("type") == "Executed" and cond.get("status") in ("True", "False"):
            sys.exit(
                "FAIL: Execution finished before token-execution was seen; use a fresh run"
            )
    time.sleep(0.5)
sys.exit("FAIL: token-execution not seen before timeout; use a fresh run")
