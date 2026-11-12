"""Offline blueprint-structure checks for setup-mas-blueprint.sh.

Uses the script's --dry-run mode (no MAS API calls, placeholder resource
RIDs) to validate the emitted blueprint.save JSON payload in both modes:

- default (no HUB_ADAPTER_A2A_URL): the post-Task-A non-investigation
  blueprint -- Ask Red Hat chain unchanged, no investigation node;
- hub-adapter mode: the hub_investigate node/step/branch added, pointing at
  the Praxis public route, with the exact-spelling header rule in the
  orchestrator prompt.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[2] / "setup-mas-blueprint.sh"
HUB_URL = "https://praxis-proxy.apps.example.com/a2a"


def run_dry(blueprint_type="multi-cluster", **env):
    """Run the script in --dry-run; return the parsed blueprint dict."""
    proc = subprocess.run(
        [str(SCRIPT), "--dry-run", blueprint_type],
        capture_output=True,
        text=True,
        env={**os.environ, "KUBECONFIG": "/nonexistent", **env},
        timeout=120,
    )
    assert proc.returncode == 0, f"dry-run failed:\n{proc.stderr}"
    # The payload is the last JSON object printed ({"blueprintRaw": "..."}).
    lines = proc.stdout.splitlines()
    start = max(i for i, line in enumerate(lines) if line.startswith("{"))
    payload = json.loads("\n".join(lines[start:]))
    assert set(payload) == {"blueprintRaw"}, payload
    return yaml.safe_load(payload["blueprintRaw"])


class DefaultBlueprint(unittest.TestCase):
    """No HUB_ADAPTER_A2A_URL: byte-shape of the post-Task-A blueprint."""

    def setUp(self):
        self.bp = run_dry()

    def test_ask_red_hat_chain_unchanged(self):
        uids = [step["uid"] for step in self.bp["plan"]]
        self.assertEqual(
            uids, ["start", "orchestrate", "ask_red_hat", "finalize"]
        )
        ask = [n for n in self.bp["nodes"] if n["name"] == "Ask Red Hat"]
        self.assertEqual(len(ask), 1)
        self.assertEqual(
            ask[0]["config"]["base_url"],
            "https://access.stage.redhat.com/api/ask/v1/a2a",
        )
        # Execution-context credential, never a stored token.
        self.assertEqual(ask[0]["config"]["bearer_token"], "")

    def test_no_investigation_node(self):
        self.assertNotIn(
            "Hub Cluster Investigator", [n["name"] for n in self.bp["nodes"]]
        )
        for step in self.bp["plan"]:
            self.assertNotIn("hub_investigate", step.get("branches", {}))

    def test_graceful_degradation_wording(self):
        system_message = self._system_message()
        self.assertIn(
            "no cluster-investigation agent is currently available",
            system_message,
        )
        self.assertNotIn("X-Hub-Cluster", system_message)

    def _system_message(self):
        orch = [n for n in self.bp["nodes"] if n["type"] == "orchestrator_node"]
        self.assertEqual(len(orch), 1)
        return orch[0]["config"]["system_message"]


class HubAdapterBlueprint(unittest.TestCase):
    """HUB_ADAPTER_A2A_URL set: the investigation node is wired in."""

    def setUp(self):
        self.bp = run_dry(HUB_ADAPTER_A2A_URL=HUB_URL)

    def test_node_points_at_praxis_front_door(self):
        hub = [
            n for n in self.bp["nodes"] if n["name"] == "Hub Cluster Investigator"
        ]
        self.assertEqual(len(hub), 1)
        cfg = hub[0]["config"]
        self.assertEqual(cfg["base_url"], HUB_URL)
        # Token A is forwarded by MAS's credential machinery, not baked in.
        self.assertEqual(cfg["bearer_token"], "")

    def test_plan_and_branch_wiring(self):
        uids = [step["uid"] for step in self.bp["plan"]]
        self.assertEqual(
            uids,
            ["start", "orchestrate", "ask_red_hat", "hub_investigate", "finalize"],
        )
        orch = [s for s in self.bp["plan"] if s["uid"] == "orchestrate"][0]
        self.assertEqual(
            orch["branches"],
            {
                "ask_red_hat": "ask_red_hat",
                "hub_investigate": "hub_investigate",
                "finalize": "finalize",
            },
        )
        self.assertIn("hub_investigate", orch["after"])

    def test_ask_red_hat_still_present(self):
        ask = [n for n in self.bp["nodes"] if n["name"] == "Ask Red Hat"]
        self.assertEqual(len(ask), 1)

    def test_prompt_header_rule(self):
        system_message = next(
            n["config"]["system_message"]
            for n in self.bp["nodes"]
            if n["type"] == "orchestrator_node"
        )
        # Deterministic target handling: the target travels as the value of
        # the routing header inside the delegate_task data argument, with an
        # exact-spelling rule (known §4.2 deviation: the name is
        # model-spelled; mitigated by temperature 0 + this rule).
        self.assertIn('data={"headers": {"X-Hub-Cluster": "<cluster-id>"}}', system_message)
        self.assertIn('spelled EXACTLY "X-Hub-Cluster"', system_message)
        self.assertIn("Hub Cluster Investigator", system_message)
        self.assertNotIn(
            "no cluster-investigation agent is currently available",
            system_message,
        )


class SimpleType(unittest.TestCase):
    """The simple blueprint has no orchestrator; the URL is ignored."""

    def test_simple_unchanged_with_url(self):
        with_url = run_dry("simple", HUB_ADAPTER_A2A_URL=HUB_URL)
        self.assertEqual(
            [n["name"] for n in with_url["nodes"]],
            ["User Question", "Ask Red Hat", "Final Answer"],
        )
        self.assertEqual(
            [s["uid"] for s in with_url["plan"]],
            ["start", "ask_red_hat", "end"],
        )


if __name__ == "__main__":
    unittest.main()
