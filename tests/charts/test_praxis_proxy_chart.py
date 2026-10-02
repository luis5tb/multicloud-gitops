"""Helm-rendered safety checks for the inert, static Praxis gateway chart."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
HELM = shutil.which("helm")
CHART = ROOT / "charts/all/praxis-proxy"
pytestmark = pytest.mark.skipif(HELM is None, reason="Helm is required to render chart tests")


def _render(*args: str) -> list[dict]:
    result = subprocess.run(
        [HELM, "template", "praxis-proxy", str(CHART), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _routing_args(*extra: str) -> tuple[str, ...]:
    return (
        "--set",
        "enabled=true",
        "--set",
        "clusterRouting.enabled=true",
        "--set",
        "clusterRouting.guardFilterEnabled=true",
        "--set",
        "clusterRouting.discoveryCluster=acme",
        "--set",
        "image.repository=quay.io/example/praxis-ai",
        "--set",
        "image.digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--set",
        "identity.keycloak.issuerUrl=https://keycloak.example.test/realms/cluster",
        "--set-json",
        'global.olsClusters={"acme":{"apiURL":"https://api.example.test:6443","upstreamHost":"lightspeed-app-server.openshift-lightspeed.svc.cluster.local","upstreamPort":8443,"upstreamSNI":"lightspeed-app-server.openshift-lightspeed.svc"}}',
        *extra,
    )


def test_default_chart_is_inert():
    assert _render() == []


def test_routing_chart_has_only_static_guarded_ols_routes():
    resources = _render(*_routing_args())
    config_map = next(r for r in resources if r["kind"] == "ConfigMap")
    policy = yaml.safe_load(config_map["data"]["policy.yaml"])
    proxy = yaml.safe_load(config_map["data"]["praxis.yaml"])

    assert policy["plugins"][0]["config"]["trusted_issuers"][0]["issuer"] == (
        "https://keycloak.example.test/realms/cluster"
    )
    assert policy["plugins"][0]["config"]["trusted_issuers"][0]["audiences"] == [
        "lightspeed-a2a"
    ]
    assert policy["plugins"][0]["config"]["role"] == "user"
    assert policy["routes"][-1]["authorization"]["pre_invocation"] == [
        "deny(claim.azp != 'acme-agent')"
    ]

    filters = proxy["filter_chains"][0]["filters"]
    assert [item["filter"] for item in filters] == [
        "policy",
        "ols_cluster_guard",
        "router",
        "load_balancer",
    ]
    routes = filters[2]["routes"]
    assert routes[0] == {
        "path": "/.well-known/agent-card.json",
        "cluster": "acme",
    }
    assert routes[1]["headers"] == {"x-ols-cluster": "acme"}
    assert routes[1]["cluster"] == "acme"
    assert all("fallback" not in str(route).lower() for route in routes)
    assert "rca-agent" not in config_map["data"]["praxis.yaml"]

    cluster = filters[3]["clusters"][0]
    assert cluster["endpoints"] == [
        "lightspeed-app-server.openshift-lightspeed.svc.cluster.local:8443"
    ]
    assert cluster["tls"]["verify"] is True
    assert cluster["tls"]["sni"] == "lightspeed-app-server.openshift-lightspeed.svc"

    deployment = next(r for r in resources if r["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert any(
        volume.get("configMap", {}).get("name") == "praxis-ols-service-ca"
        for volume in pod["volumes"]
    )
    assert any(
        mount["mountPath"] == "/etc/praxis/upstream-ca/acme"
        for container in pod["containers"]
        for mount in container["volumeMounts"]
    )
    assert not any(r["kind"] == "Route" for r in resources)


def test_enabling_chart_without_static_cluster_routing_fails_closed():
    result = subprocess.run(
        [HELM, "template", "praxis-proxy", str(CHART), "--set", "enabled=true"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "legacy RCA fallback has been removed" in result.stderr


def test_enabled_route_requires_explicit_host():
    result = subprocess.run(
        [
            HELM,
            "template",
            "praxis-proxy",
            str(CHART),
            *_routing_args("--set", "route.enabled=true"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "route.host must be set" in result.stderr


def test_multiple_allowed_callers_are_combined_in_one_explicit_deny():
    resources = _render(
        *_routing_args(
            "--set-json",
            'policy.allowedCallers=["acme-agent","lightspeed-ui"]',
        )
    )
    config_map = next(r for r in resources if r["kind"] == "ConfigMap")
    policy = yaml.safe_load(config_map["data"]["policy.yaml"])
    assert policy["routes"][-1]["authorization"]["pre_invocation"] == [
        "deny(claim.azp != 'acme-agent' && claim.azp != 'lightspeed-ui')"
    ]


def test_allow_list_rejects_values_that_could_inject_apl():
    result = subprocess.run(
        [
            HELM,
            "template",
            "praxis-proxy",
            str(CHART),
            *_routing_args("--set-string", "policy.allowedCallers[0]=acme-agent' || true"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "safe Keycloak client IDs" in result.stderr
