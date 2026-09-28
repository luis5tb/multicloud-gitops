import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[3]
HELM = shutil.which("helm")
pytestmark = pytest.mark.skipif(HELM is None, reason="Helm is required to render chart tests")


def _render(chart: str, *args: str) -> list[dict]:
    result = subprocess.run(
        [HELM, "template", "praxis-proxy", str(ROOT / chart), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _one(resources: list[dict], kind: str, name: str) -> dict:
    return next(
        resource
        for resource in resources
        if resource["kind"] == kind and resource["metadata"]["name"] == name
    )


def test_praxis_chart_renders_fail_closed_keycloak_authorization_policy():
    resources = _render(
        "charts/all/praxis-proxy",
        "--set",
        "identity.keycloak.issuerUrl=https://keycloak.example.com/realms/rca",
    )
    config = _one(resources, "ConfigMap", "praxis-proxy")["data"]
    policy = yaml.safe_load(config["policy.yaml"])

    identity_plugin = policy["plugins"][0]
    assert identity_plugin["kind"] == "identity/jwt"
    assert identity_plugin["config"]["role"] == "client"
    assert identity_plugin["config"]["header"] == "Authorization"
    assert identity_plugin["config"]["claims"]["include"] == ["azp"]
    assert identity_plugin["config"]["trusted_issuers"][0]["issuer"] == (
        "https://keycloak.example.com/realms/rca"
    )
    assert identity_plugin["config"]["trusted_issuers"][0]["audiences"] == ["rca-agent"]
    assert identity_plugin["config"]["claim_mapper"] == "keycloak"

    routes = policy["routes"]
    assert routes[0]["http"] == {
        "path": "/.well-known/agent-card.json",
        "method": "GET",
    }
    assert routes[0]["authorization"]["pre_invocation"] == ["allow"]
    assert routes[1]["http"] == {"path": "/health/ready", "method": "GET"}
    assert routes[1]["authorization"]["pre_invocation"] == ["allow"]

    catch_all = routes[2]
    assert catch_all["http"] == {"path_prefix": "/"}
    assert catch_all["authentication"]["steps"] == ["jwt-client"]
    assert catch_all["authorization"]["pre_invocation"] == [
        "require(claim.azp == 'ericsson-agent')",
    ]


def test_praxis_policy_filter_precedes_static_rca_routing_and_uses_pinned_image():
    resources = _render(
        "charts/all/praxis-proxy",
        "--set",
        "identity.keycloak.issuerUrl=https://keycloak.example.com/realms/rca",
    )
    config = _one(resources, "ConfigMap", "praxis-proxy")["data"]
    proxy_config = yaml.safe_load(config["praxis.yaml"])
    filters = proxy_config["filter_chains"][0]["filters"]

    assert [filter_config["filter"] for filter_config in filters] == [
        "policy",
        "router",
        "load_balancer",
    ]
    assert filters[0]["config_path"] == "/etc/praxis/config/policy.yaml"
    assert filters[0]["require_protocol_metadata"] is False
    assert filters[2]["clusters"][0]["endpoints"] == [
        "rca-agent.lightspeed-agentic-operator.svc.cluster.local:8000"
    ]
    assert proxy_config["insecure_options"]["allow_private_upstreams"] is True

    deployment = _one(resources, "Deployment", "praxis-proxy")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == (
        "ghcr.io/praxis-proxy/ai@sha256:"
        "83c86c059162cd17a0ee7bc3e5146e4be02f811bada6d38027acc6668b8be903"
    )
    assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


def test_empty_caller_allow_list_fails_chart_rendering():
    result = subprocess.run(
        [
            HELM,
            "template",
            "praxis-proxy",
            str(ROOT / "charts/all/praxis-proxy"),
            "--set-json",
            "policy.allowedCallers=[]",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "policy.allowedCallers must contain at least one client" in result.stderr


def test_enabled_praxis_route_requires_explicit_public_host():
    result = subprocess.run(
        [
            HELM,
            "template",
            "praxis-proxy",
            str(ROOT / "charts/all/praxis-proxy"),
            "--set",
            "route.enabled=true",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "route.host must be set" in result.stderr


def test_rca_service_ingress_defaults_to_praxis_only_without_direct_route():
    resources = _render(
        "charts/all/rca-agent",
        "--set",
        "route.enabled=false",
    )
    network_policy = _one(resources, "NetworkPolicy", "rca-agent")
    ingress = network_policy["spec"]["ingress"][0]

    assert ingress["from"][0]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "praxis-proxy",
        "app.kubernetes.io/instance": "praxis-proxy",
    }
    assert ingress["ports"] == [{"protocol": "TCP", "port": 8000}]
    assert not any(resource["kind"] == "Route" for resource in resources)

    deployment = _one(resources, "Deployment", "rca-agent")
    env_names = {
        env["name"]
        for env in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "OPA_URL" not in env_names
    assert "OPA_TIMEOUT_SECONDS" not in env_names
