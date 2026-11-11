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
            "identity.keycloak.audience=mas-a2a",
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
    # The RPC endpoint auto-derives from global.clusterBaseDomain in the variant,
    # so compute the expected host from that value rather than pinning a domain.
    base_domain = yaml.safe_load(VARIANT.read_text())["global"]["clusterBaseDomain"]
    expected_rpc_url = f"https://praxis-proxy.apps.{base_domain}/a2a"
    # A top-level "url" is retained for backward compatibility with A2A 0.3.0
    # clients; newer clients read supportedInterfaces[].url. Both must agree.
    assert card["url"] == expected_rpc_url
    assert card["supportedInterfaces"][0]["url"] == expected_rpc_url
    # The no-investigator gateway advertises no OLS skills or extensions.
    assert "skills" not in card
    assert "extensions" not in card
    assert static_response["conditions"] == [
        {"when": {"path": "/.well-known/agent-card.json", "methods": ["GET"]}}
    ]

    # No upstream clusters are configured: the chart must not render a
    # load_balancer filter at all, and readiness is answered locally.
    assert not any(
        filter_config.get("filter") == "load_balancer"
        for filter_config in filters
    )
    # VERIFIED-BROKEN on the pinned image: a router filter with an empty
    # routes list is a fatal config-load error ("fatal: router: 'routes'
    # is empty; every request would fail with 404"), so the no-backend
    # config must carry NO router filter at all.
    assert not any(
        filter_config.get("filter") == "router"
        for filter_config in filters
    ), "no-backend config must not render a router (empty routes is fatal)"
    readiness = [
        filter_config
        for filter_config in filters
        if filter_config.get("filter") == "static_response"
        and filter_config["conditions"]
        == [{"when": {"path": "/readiness", "methods": ["GET"]}}]
    ]
    assert readiness and readiness[0]["status"] == 200
