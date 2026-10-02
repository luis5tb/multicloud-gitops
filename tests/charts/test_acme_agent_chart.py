"""Helm checks for the greenfield ACME deployment gate."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
HELM = shutil.which("helm")
CHART = ROOT / "charts/all/acme-agent"
pytestmark = pytest.mark.skipif(HELM is None, reason="Helm is required to render chart tests")


def test_acme_defaults_to_no_resources():
    result = subprocess.run(
        [HELM, "template", "acme-agent", str(CHART), "--namespace", "acme-agent"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert [doc for doc in yaml.safe_load_all(result.stdout) if doc] == []


def test_enabled_acme_requires_a_downstream_agent():
    result = subprocess.run(
        [
            HELM,
            "template",
            "acme-agent",
            str(CHART),
            "--namespace",
            "acme-agent",
            "--set",
            "enabled=true",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "a2a.remoteAgents must have at least one entry" in result.stderr


def test_enabled_acme_renders_configured_praxis_peer():
    result = subprocess.run(
        [
            HELM,
            "template",
            "acme-agent",
            str(CHART),
            "--namespace",
            "acme-agent",
            "--set",
            "enabled=true",
            "--set",
            "a2a.remoteAgents[0].name=openshift_lightspeed",
            "--set",
            "a2a.remoteAgents[0].description=OpenShift Lightspeed",
            "--set",
            "a2a.remoteAgents[0].endpoint=https://praxis.example.test",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}
    agents = yaml.safe_load(env["REMOTE_A2A_AGENTS_JSON"])
    assert agents == [
        {
            "name": "openshift_lightspeed",
            "description": "OpenShift Lightspeed",
            "endpoint": "https://praxis.example.test",
        }
    ]
    assert env["ACME_CLUSTER_SELECTION_ENABLED"] == "false"
