"""Regression tests: Praxis serves NO static A2A agent card.

The discovery card is always the a2aUpstream backend's own card, routed
through unchanged (see test_praxis_a2a_upstream.py). VERIFIED-BROKEN LIVE
(2026-11): the static card Praxis used to render advertised A2A
protocolVersion "1.0", which made MAS call v1.0 method names
(SendStreamingMessage/SendMessage) against the A2A 0.3 lightspeed-hub
adapter -> "method not found" on every delegation. With no backend
(a2aUpstream disabled) there is no agent behind the gateway, so there is no
card either.
"""

from __future__ import annotations

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
def test_no_backend_renders_no_card_and_no_router() -> None:
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
    filters = yaml.safe_load(configmap["data"]["praxis.yaml"])["filter_chains"][0][
        "filters"
    ]

    # Only readiness is answered locally; no static agent card.
    statics = [f for f in filters if f.get("filter") == "static_response"]
    assert [s["conditions"] for s in statics] == [
        [{"when": {"path": "/readiness", "methods": ["GET"]}}]
    ]
    assert statics[0]["status"] == 200

    # No upstream: no load_balancer, and NO router at all -- a router with
    # an empty routes list is a fatal config-load error on the pinned image
    # ("fatal: router: 'routes' is empty; every request would fail with 404").
    assert not any(f.get("filter") in ("load_balancer", "router") for f in filters)
