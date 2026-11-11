"""Regression tests for the per-spoke Praxis spoke-gateways (Phase 2 B1).

B1 is a PROPOSED integration, disabled by default: the spokeGateways map in
charts/all/praxis-proxy is empty unless explicitly set, and the Keycloak
exchanger client in charts/all/keycloak-oidc is behind
keycloak.spokeGatewayExchanger.enabled. These tests pin both the
disabled-by-default behavior and the rendered shape of one enabled entry
(including the serving-CA ConfigMap->Secret sync bridge and the egress
NetworkPolicy's parsed remote API port).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "charts/all/praxis-proxy"
KEYCLOAK_CHART = REPO_ROOT / "charts/all/keycloak-oidc"
VARIANT = REPO_ROOT / "variants/standalone/values-standalone.yaml"

SPOKE_SETS = [
    "--set", "spokeGateways.east.remoteAPIServer=https://api.east.example.test:6443",
    "--set", "spokeGateways.east.exchangeAudience=praxis-spoke-east",
    "--set", "spokeGateways.east.remoteCACert.secretName=praxis-spoke-east-api-ca",
]


def _helm(chart: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "praxis-proxy", str(chart), "-f", str(VARIANT), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _resources(stdout: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(stdout) if doc]


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_default_render_has_no_spoke_gateways() -> None:
    """An empty spokeGateways map must render nothing spoke-related."""
    rendered = _helm(CHART, "--set", "identity.keycloak.audience=mas-a2a")
    assert "praxis-spoke" not in rendered.stdout
    # The serving-CA sync bridge is gated on spoke entries existing too.
    assert "spoke-ca-sync" not in rendered.stdout
    assert "spoke-ca-bootstrap" not in rendered.stdout


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_spoke_gateway_entry_renders_expected_resources() -> None:
    rendered = _helm(CHART, "--set", "identity.keycloak.audience=mas-a2a", *SPOKE_SETS)
    docs = _resources(rendered.stdout)
    spoke_docs = [
        doc
        for doc in docs
        if "spoke" in str(doc.get("metadata", {}).get("name", ""))
    ]

    deployments = [d for d in spoke_docs if d["kind"] == "Deployment"]
    services = [d for d in spoke_docs if d["kind"] == "Service"]
    policies = [d for d in spoke_docs if d["kind"] == "NetworkPolicy"]
    configmaps = [d for d in spoke_docs if d["kind"] == "ConfigMap"]
    routes = [d for d in spoke_docs if d["kind"] == "Route"]
    jobs = [d for d in docs if d["kind"] == "Job" and "spoke-ca" in d["metadata"]["name"]]
    cronjobs = [d for d in docs if d["kind"] == "CronJob"]
    secrets = [d for d in docs if d["kind"] == "Secret"]

    # Deployment + Service + NetworkPolicy + per-spoke ConfigMap, plus the
    # shared service-CA bundle ConfigMap and the sync-bridge script
    # ConfigMap. Exactly one of each per entry.
    assert [d["metadata"]["name"] for d in deployments] == ["praxis-spoke-east"]
    assert [d["metadata"]["name"] for d in services] == ["praxis-spoke-east"]
    assert [d["metadata"]["name"] for d in policies] == ["praxis-spoke-east"]
    assert sorted(d["metadata"]["name"] for d in configmaps) == [
        "praxis-proxy-spoke-ca-sync-script",
        "praxis-spoke-east",
        "praxis-spoke-service-ca",
    ]
    # The plan forbids public gateway Routes: never render one for a spoke.
    assert routes == []

    # Serving-CA sync bridge (repo C's gatewayCASecret contract): one-shot
    # bootstrap Job + CronJob + RBAC + script ConfigMap. The target Secret
    # is created BY THE JOB, deliberately not rendered as a manifest
    # (Argo would fight the job's writes) -- pin that too.
    assert [j["metadata"]["name"] for j in jobs] == ["praxis-proxy-spoke-ca-bootstrap"]
    assert [c["metadata"]["name"] for c in cronjobs] == ["praxis-proxy-spoke-ca-sync"]
    sync_sa = next(
        d for d in docs
        if d["kind"] == "ServiceAccount"
        and d["metadata"]["name"] == "praxis-proxy-spoke-ca-sync"
    )
    assert sync_sa is not None
    target_roles = [
        d for d in docs
        if d["kind"] == "Role" and d["metadata"]["name"] == "praxis-proxy-spoke-ca-sync-target"
    ]
    assert len(target_roles) == 1
    # Default target namespace is the chart's own (release) namespace --
    # "default" here because the test's bare `helm template` sets no -n.
    assert target_roles[0]["metadata"]["namespace"] == "default"
    target_rules = target_roles[0]["rules"]
    secret_rules = [r for r in target_rules if "secrets" in r["resources"]]
    assert secret_rules, "target Role must grant secrets access"
    assert any("create" in r["verbs"] for r in secret_rules)
    assert not [s for s in secrets if s["metadata"]["name"] == "praxis-spoke-gateway-ca"], (
        "the gateway-CA Secret must be job-created, not a rendered manifest"
    )
    # The bootstrap Job's pod references the target Secret name via env.
    job_pod_spec = jobs[0]["spec"]["template"]["spec"]
    job_env = {
        e["name"]: e["value"]
        for e in job_pod_spec["containers"][0]["env"]
        if "value" in e
    }
    assert job_env["TARGET_SECRET"] == "praxis-spoke-gateway-ca"
    assert job_env["TARGET_KEY"] == "ca.crt"
    assert job_env["TARGET_NAMESPACE"] == "default"

    # The shared service-CA ConfigMap uses the in-cluster inject-cabundle
    # mechanism (the CA the sandbox must trust for the serving cert).
    ca_cm = next(
        d for d in configmaps if d["metadata"]["name"] == "praxis-spoke-service-ca"
    )
    assert (
        ca_cm["metadata"]["annotations"]["service.beta.openshift.io/inject-cabundle"]
        == "true"
    )

    # Service: internal ClusterIP on 8443 with a service-serving cert.
    svc = services[0]
    assert svc["spec"]["type"] == "ClusterIP"
    assert svc["spec"]["ports"] == [
        {"name": "https", "port": 8443, "targetPort": "https", "protocol": "TCP"}
    ]
    assert (
        svc["metadata"]["annotations"]["service.beta.openshift.io/serving-cert-secret-name"]
        == "praxis-spoke-east-serving-cert"
    )

    # Deployment: per-spoke labels, remote-CA + serving-cert + config mounts,
    # exchanger client secret env, no ServiceAccount token automount.
    dep = deployments[0]
    pod = dep["spec"]["template"]
    assert pod["metadata"]["labels"]["praxis-proxy.spoke-gateway"] == "east"
    assert pod["spec"]["automountServiceAccountToken"] is False
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    assert set(volumes) == {"config", "serving-cert", "spoke-remote-ca"}
    assert volumes["serving-cert"]["secret"]["secretName"] == (
        "praxis-spoke-east-serving-cert"
    )
    # Fail-closed: the remote CA is mounted from the referenced Secret (a
    # dangling reference leaves the pod unschedulable), not optional.
    assert volumes["spoke-remote-ca"]["secret"]["secretName"] == (
        "praxis-spoke-east-api-ca"
    )
    assert volumes["spoke-remote-ca"]["secret"]["items"] == [
        {"key": "ca.crt", "path": "ca.crt"}
    ]
    env = {
        e["name"]: e for e in pod["spec"]["containers"][0]["env"]
    }
    assert env["PRAXIS_SPOKE_EXCHANGE_CLIENT_SECRET"]["valueFrom"] == {
        "secretKeyRef": {
            "name": "praxis-spoke-gateway-client",
            "key": "client-secret",
        }
    }

    # Per-spoke config: fixed remote endpoint with verified CA/SNI, router
    # catch-all to that single cluster, readiness answered locally.
    cm = next(d for d in configmaps if d["metadata"]["name"] == "praxis-spoke-east")
    config = yaml.safe_load(cm["data"]["praxis.yaml"])
    assert config["listeners"][0]["address"] == "0.0.0.0:8443"
    filters = config["filter_chains"][0]["filters"]
    lb = next(f for f in filters if f["filter"] == "load_balancer")
    assert lb["clusters"] == [
        {
            "name": "spoke-api",
            "endpoints": ["api.east.example.test:6443"],
            "tls": {
                "sni": "api.east.example.test",
                "verify": True,
                "ca": {"ca_path": "/etc/praxis/spoke-remote-ca/ca.crt"},
            },
        }
    ]
    router = next(f for f in filters if f["filter"] == "router")
    assert router["routes"] == [{"path_prefix": "/", "cluster": "spoke-api"}]
    delegator = next(f for f in filters if f["filter"] == "oauth-delegator")
    assert delegator["audience"] == "praxis-spoke-east"
    # policy.yaml is re-rendered per spoke from the same file as the public
    # gateway, so it must parse as valid YAML.
    policy = yaml.safe_load(cm["data"]["policy.yaml"])
    assert policy["plugins"][0]["kind"] == "identity/jwt"

    # NetworkPolicy: ingress only from the configured sandbox namespaces on
    # 8443; egress limited to DNS + HTTPS 443 (Keycloak/JWKS) + the remote
    # spoke API port parsed from remoteAPIServer (6443 here -- remote kube
    # APIs do not listen on 443; port-scoped only, see chart README).
    np = policies[0]
    assert np["spec"]["policyTypes"] == ["Ingress", "Egress"]
    ingress = np["spec"]["ingress"][0]
    assert ingress["ports"] == [{"protocol": "TCP", "port": 8443}]
    assert ingress["from"] == [
        {
            "namespaceSelector": {
                "matchLabels": {"kubernetes.io/metadata.name": "mao"}
            }
        }
    ]
    egress_ports = np["spec"]["egress"][1]["ports"]
    assert {"protocol": "TCP", "port": 443} in egress_ports
    assert {"protocol": "TCP", "port": 6443} in egress_ports, (
        "egress must allow the parsed remote API port, not just 443"
    )


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_former_reserved_sibling_key_renders_as_a_spoke() -> None:
    """A spoke literally named networkPolicy must not be silently dropped.

    shared settings used to hide under spokeGateways.{exchangeClient,
    networkPolicy}; they now live in spokeGatewayConfig, so spokeGateways is
    purely per-spoke. A key named like a former reserved sibling must either
    render as a spoke or fail loudly -- never vanish. "networkPolicy" has an
    uppercase letter, so it fails the DNS-label validation (loudly).
    """
    result = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "spokeGateways.networkPolicy.remoteAPIServer=https://api.east.example.test:6443",
        "--set", "spokeGateways.networkPolicy.exchangeAudience=praxis-spoke-east",
        "--set", "spokeGateways.networkPolicy.remoteCACert.secretName=x",
        check=False,
    )
    if result.returncode == 0:
        docs = _resources(result.stdout)
        spoke_names = [
            d["metadata"]["name"]
            for d in docs
            if d["kind"] in ("Deployment", "Service", "NetworkPolicy")
            and "networkpolicy" in d["metadata"]["name"].lower()
        ]
        assert spoke_names, "reserved-name spoke must render its resources"
    else:
        # Failing loudly is acceptable; a silent drop is not.
        assert "spokeGateways key" in result.stderr


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_spoke_gateway_missing_required_fields_fail_loudly() -> None:
    result = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "spokeGateways.east.exchangeAudience=praxis-spoke-east",
        check=False,
    )
    assert result.returncode != 0
    assert "spokeGateways.east.remoteAPIServer is required" in result.stderr

    result = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "spokeGateways.east.remoteAPIServer=https://api.east.example.test:6443",
        "--set", "spokeGateways.east.exchangeAudience=praxis-spoke-east",
        check=False,
    )
    assert result.returncode != 0
    assert "remoteCACert is required" in result.stderr

    result = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "spokeGateways.east.remoteAPIServer=http://api.east.example.test:6443",
        "--set", "spokeGateways.east.exchangeAudience=praxis-spoke-east",
        "--set", "spokeGateways.east.remoteCACert.secretName=x",
        check=False,
    )
    assert result.returncode != 0
    assert "must be an https:// URL" in result.stderr


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_keycloak_spoke_gateway_exchanger_client() -> None:
    """The B1 exchanger client is absent by default and rendered when enabled."""
    base = subprocess.run(
        [
            "helm", "template", "keycloak-oidc", str(KEYCLOAK_CHART),
            "-f", str(VARIANT),
            "--set", "keycloak.spiffeIdentityProvider.enabled=false",
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    base_docs = _resources(base)
    base_clients = next(
        d["spec"]["realm"]["clients"]
        for d in base_docs if d["kind"] == "KeycloakRealmImport"
    )
    assert "praxis-spoke-gateway" not in [
        c["clientId"] for c in base_clients
    ]

    enabled = subprocess.run(
        [
            "helm", "template", "keycloak-oidc", str(KEYCLOAK_CHART),
            "-f", str(VARIANT),
            "--set", "keycloak.spiffeIdentityProvider.enabled=false",
            "--set", "keycloak.spokeGatewayExchanger.enabled=true",
            "--set", "keycloak.spokeGatewayExchanger.vaultKey=secret/data/global/praxis-spoke-gateway",
            "--set", "keycloak.spokeGatewayExchanger.spokeAudiences[0]=praxis-spoke-east",
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    docs = _resources(enabled)
    realm = next(d for d in docs if d["kind"] == "KeycloakRealmImport")
    clients = {c["clientId"]: c for c in realm["spec"]["realm"]["clients"]}

    exchanger = clients["praxis-spoke-gateway"]
    assert exchanger["publicClient"] is False
    assert exchanger["secret"] == "$(PRAXIS_SPOKE_EXCHANGER_SECRET)"
    assert exchanger["attributes"] == {"standard.token.exchange.enabled": "true"}
    assert [m["config"]["included.custom.audience"] for m in exchanger["protocolMappers"]] == [
        "praxis-spoke-east"
    ]
    # Subject-token audience prerequisite: the exchanger client ID is added
    # as an audience on the acme-agent client's tokens (Token A).
    acme = clients["acme-agent"]
    exchanger_aud_mappers = [
        m
        for m in acme["protocolMappers"]
        if m.get("config", {}).get("included.custom.audience") == "praxis-spoke-gateway"
    ]
    assert exchanger_aud_mappers, (
        "subjectTokenAudienceMapper must put the exchanger client ID on Token A"
    )

    # The Vault-backed secret is materialized into both the keycloak
    # namespace (realm-import placeholder) and the praxis-proxy namespace
    # (gateway pods), following the console-client-secret pattern.
    secrets = {
        (d["metadata"]["name"], d["metadata"]["namespace"])
        for d in docs if d["kind"] == "ExternalSecret"
    }
    assert ("praxis-spoke-gateway-client", "keycloak-system") in secrets
    assert ("praxis-spoke-gateway-client", "praxis-proxy") in secrets

    # Missing vaultKey fails loudly rather than rendering a broken secret.
    missing = subprocess.run(
        [
            "helm", "template", "keycloak-oidc", str(KEYCLOAK_CHART),
            "-f", str(VARIANT),
            "--set", "keycloak.spiffeIdentityProvider.enabled=false",
            "--set", "keycloak.spokeGatewayExchanger.enabled=true",
        ],
        check=False, capture_output=True, text=True,
    )
    assert missing.returncode != 0
    assert "spokeGatewayExchanger.vaultKey is required" in missing.stderr
