"""Regression tests for Praxis's static A2A agent-card rendering."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "charts/all/praxis-proxy"
VARIANT = REPO_ROOT / "variants/standalone/values-standalone.yaml"


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_static_agent_card_uses_response_filter_not_lb_cluster() -> None:
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "praxis-proxy",
            str(CHART),
            "--namespace",
            "praxis-proxy",
            "--values",
            str(VARIANT),
            "--set",
            "agentCard.enabled=true",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    configmap = next(
        resource
        for resource in yaml.safe_load_all(rendered)
        if resource
        and resource.get("kind") == "ConfigMap"
        and resource.get("metadata", {}).get("name") == "praxis-proxy"
    )
    config = yaml.safe_load(configmap["data"]["praxis.yaml"])
    filters = config["filter_chains"][0]["filters"]

    static_response = next(
        filter_config
        for filter_config in filters
        if filter_config.get("filter") == "static_response"
    )
    card = json.loads(static_response["body"])
    assert card["rpc_url"] == "https://openshift-lightspeed.apps.ocp.phkl2.sandbox315.opentlc.com/a2a"
    assert static_response["conditions"] == [
        {"when": {"path": "/.well-known/agent-card.json", "methods": ["GET"]}}
    ]

    load_balancer = next(
        filter_config
        for filter_config in filters
        if filter_config.get("filter") == "load_balancer"
    )
    assert all(
        cluster["name"] != "static-agent-card"
        and "static_files" not in cluster
        for cluster in load_balancer["clusters"]
    )
