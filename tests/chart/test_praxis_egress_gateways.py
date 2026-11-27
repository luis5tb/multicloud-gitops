"""Regression tests for praxis-proxy's external-IdP egress gateways.

An egress gateway accepts the caller's local-Keycloak Token A, mints a token
at an external IdP with its own client (delegate(..., subject: this_workload))
and forwards to one fixed external upstream. The rendered shape pinned here is
the one verified end to end on the pinned image on 2026-11-26 (Red Hat SSO
stage + Ask Red Hat stage; see docs/EXTERNAL_IDP_EGRESS.md).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "charts/all/praxis-proxy"

BASE = [
    "--set", "identity.keycloak.audience=mas-a2a",
    "--set", "global.clusterBaseDomain=example.test",
    "--set", "policy.caBundleConfigMap.name=praxis-keycloak-ca",
    "--set", "policy.allowedCallers[0]=acme-agent",
]
EGRESS = [
    "--set", "egressGateways.ask-red-hat.upstreamURL=https://access.example.test/api/ask/v1/a2a",
    "--set", "egressGateways.ask-red-hat.tokenUrl=https://sso.example.test/auth/realms/x/protocol/openid-connect/token",
    "--set", "egressGateways.ask-red-hat.clientId=sso-client",
    "--set", "egressGateways.ask-red-hat.audience=api.ask_red_hat",
    "--set", "egressGateways.ask-red-hat.scopes={openid,api.ask_red_hat}",
    "--set", "egressGateways.ask-red-hat.clientSecret.vaultKey=secret/data/global/sso-stage",
]
NAME = "praxis-egress-ask-red-hat"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)


def _helm(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "praxis-proxy", str(CHART), "-n", "praxis-proxy", *BASE, *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _resources(*args: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(_helm(*args).stdout) if d]


def _get(resources: list[dict], kind: str, name: str = NAME) -> dict:
    return next(r for r in resources if r["kind"] == kind and r["metadata"]["name"] == name)


def _without(*flags: str) -> list[str]:
    """EGRESS minus the --set pairs whose key starts with any of flags."""
    out = []
    for key, value in zip(EGRESS[::2], EGRESS[1::2]):
        if not value.startswith(flags):
            out += [key, value]
    return out


def test_default_render_has_no_egress_gateways() -> None:
    assert "praxis-egress" not in _helm().stdout


def test_enabled_render_objects() -> None:
    kinds = {
        (r["kind"], r["metadata"]["name"])
        for r in _resources(*EGRESS)
        if "egress" in r["metadata"]["name"]
    }
    assert kinds == {
        ("ConfigMap", NAME),
        ("Deployment", NAME),
        ("Service", NAME),
        ("NetworkPolicy", NAME),
        ("ExternalSecret", f"{NAME}-client"),
    }


def test_gateway_config_matches_verified_shape() -> None:
    cm = _get(_resources(*EGRESS), "ConfigMap")
    praxis = yaml.safe_load(cm["data"]["praxis.yaml"])
    listener = praxis["listeners"][0]
    assert listener["address"] == "0.0.0.0:8443"
    assert listener["tls"]["certificates"] == [
        {"cert_path": "/etc/praxis/serving-cert/tls.crt", "key_path": "/etc/praxis/serving-cert/tls.key"}
    ]
    filters = {f["filter"]: f for f in praxis["filter_chains"][0]["filters"]}
    assert list(filters)[0] == "policy"
    # The inbound Host (the Service name) must never reach an external edge.
    assert {"name": "Host", "value": "access.example.test"} in filters["headers"]["request_set"]
    assert filters["router"]["routes"] == [{"path_prefix": "/api/ask/v1/a2a", "cluster": "egress-upstream"}]
    cluster = filters["load_balancer"]["clusters"][0]
    assert cluster["endpoints"] == ["access.example.test:443"]
    assert cluster["tls"] == {
        "sni": "access.example.test",
        "verify": True,
        "ca": {"ca_path": "/etc/praxis/ca-bundle/ca-bundle.crt"},
    }
    assert "insecure_options" not in praxis


def test_policy_trusts_only_local_keycloak_and_mints_as_this_workload() -> None:
    cm = _get(
        _resources(
            *EGRESS,
            # A front-door multi-issuer config must NOT leak into the egress gateway.
            "--set", "identity.oidcIssuers[0].issuerUrl=https://sso.example.test/auth/realms/x",
            "--set", "identity.oidcIssuers[0].jwksUrl=https://sso.example.test/certs",
            "--set", "identity.oidcIssuers[0].audience=api.ask_red_hat",
        ),
        "ConfigMap",
    )
    policy = yaml.safe_load(cm["data"]["policy.yaml"])
    jwt = next(p for p in policy["plugins"] if p["kind"] == "identity/jwt")
    issuers = jwt["config"]["trusted_issuers"]
    assert [i["issuer"] for i in issuers] == ["https://keycloak.apps.example.test/realms/rca"]
    assert issuers[0]["audiences"] == ["mas-a2a"]
    delegator = next(p for p in policy["plugins"] if p["kind"] == "delegator/oauth")
    assert delegator["config"]["token_endpoint"].startswith("https://sso.example.test/")
    assert delegator["config"]["client_id"] == "sso-client"
    assert delegator["config"]["client_secret_source"] == {"kind": "env_var", "name": "PRAXIS_EGRESS_CLIENT_SECRET"}
    routes = policy["routes"]
    card = next(r for r in routes if r["http"].get("path") == "/api/ask/v1/a2a/.well-known/agent-card.json")
    assert card["http"]["method"] == "GET" and card["authorization"]["pre_invocation"] == ["allow"]
    catch_all = next(r for r in routes if r["http"].get("path_prefix") == "/")
    assert catch_all["authentication"]["steps"] == ["jwt-client"]
    assert catch_all["authorization"]["pre_invocation"] == [
        "claim.azp != 'acme-agent': deny('not an allow-listed caller')",
        "delegate(egress-delegator, target: egress-upstream, audience: api.ask_red_hat, "
        "permissions: [openid, api.ask_red_hat], subject: this_workload)",
    ]


def test_per_entry_overrides() -> None:
    cm = _get(
        _resources(
            *EGRESS,
            "--set", "egressGateways.ask-red-hat.inboundAudience=other-aud",
            "--set", "egressGateways.ask-red-hat.allowedCallers[0]=caller-x",
            "--set", "egressGateways.ask-red-hat.upstreamURL=https://access.example.test:8443/api/ask/v1/a2a/",
        ),
        "ConfigMap",
    )
    policy = yaml.safe_load(cm["data"]["policy.yaml"])
    jwt = next(p for p in policy["plugins"] if p["kind"] == "identity/jwt")
    assert jwt["config"]["trusted_issuers"][0]["audiences"] == ["other-aud"]
    assert "claim.azp != 'caller-x'" in policy["routes"][-1]["authorization"]["pre_invocation"][0]
    praxis = yaml.safe_load(cm["data"]["praxis.yaml"])
    filters = {f["filter"]: f for f in praxis["filter_chains"][0]["filters"]}
    assert filters["load_balancer"]["clusters"][0]["endpoints"] == ["access.example.test:8443"]
    assert {"name": "Host", "value": "access.example.test:8443"} in filters["headers"]["request_set"]
    assert filters["router"]["routes"][0]["path_prefix"] == "/api/ask/v1/a2a"


def test_deployment_service_and_secret() -> None:
    res = _resources(*EGRESS)
    dep = _get(res, "Deployment")
    pod = dep["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    assert env["PRAXIS_EGRESS_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": f"{NAME}-client", "key": "client-secret",
    }
    assert env["SSL_CERT_FILE"]["value"] == "/etc/praxis/ca-bundle/ca-bundle.crt"
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["serving-cert"]["secret"]["secretName"] == f"{NAME}-serving-cert"
    assert vols["policy-ca-bundle"]["configMap"]["name"] == "praxis-keycloak-ca"
    # Distinct app name: the main praxis-proxy Service must never select these pods.
    labels = dep["spec"]["selector"]["matchLabels"]
    assert labels["app.kubernetes.io/name"] == "praxis-egress-gateway"
    main_svc = _get(res, "Service", "praxis-proxy")
    assert main_svc["spec"]["selector"]["app.kubernetes.io/name"] != "praxis-egress-gateway"

    svc = _get(res, "Service")
    assert svc["spec"]["type"] == "ClusterIP"
    assert svc["metadata"]["annotations"]["service.beta.openshift.io/serving-cert-secret-name"] == f"{NAME}-serving-cert"
    assert not [r for r in res if r["kind"] == "Route" and "egress" in r["metadata"]["name"]]

    es = _get(res, "ExternalSecret", f"{NAME}-client")
    assert es["spec"]["data"] == [{
        "secretKey": "client-secret",
        "remoteRef": {"key": "secret/data/global/sso-stage", "property": "client-secret"},
    }]


def test_existing_secret_skips_externalsecret() -> None:
    args = _without("egressGateways.ask-red-hat.clientSecret") + [
        "--set", "egressGateways.ask-red-hat.clientSecret.existingSecret.name=my-sso",
        "--set", "egressGateways.ask-red-hat.clientSecret.existingSecret.key=secret",
    ]
    res = _resources(*args)
    assert not [r for r in res if r["kind"] == "ExternalSecret" and "egress" in r["metadata"]["name"]]
    env = {e["name"]: e for e in _get(res, "Deployment")["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["PRAXIS_EGRESS_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {"name": "my-sso", "key": "secret"}


def test_networkpolicy() -> None:
    np = _get(_resources(*EGRESS), "NetworkPolicy")
    peer = np["spec"]["ingress"][0]["from"][0]
    assert peer["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "mao"}
    # mas-a2a builds the plan and fetches the card itself (live 2026-11-27).
    assert peer["podSelector"]["matchExpressions"][0]["values"] == ["mas-a2a", "mas-worker", "mas-api"]
    assert np["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8443}]
    https = np["spec"]["egress"][1]
    assert https["ports"] == [{"protocol": "TCP", "port": 443}]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (_without("egressGateways.ask-red-hat.clientSecret"), "clientSecret requires vaultKey"),
        (EGRESS + ["--set", "egressGateways.ask-red-hat.upstreamURL=http://access.example.test/api"], "must be an https:// URL"),
        (EGRESS + ["--set", "egressGateways.ask-red-hat.upstreamURL=https://access.example.test"], "must include the upstream's path"),
        (EGRESS + ["--set", "egressGateways.ask-red-hat.clientSecret.existingSecret.name=x"], "set exactly one of vaultKey or existingSecret"),
        (EGRESS + ["--set", "policy.caBundleConfigMap.name="], "requires policy.caBundleConfigMap.name"),
        (EGRESS + ["--set", "policy.enabled=false"], "requires policy.enabled"),
        (EGRESS + ["--set", "egressGatewayConfig.networkPolicy.ingressFromNamespaces=null"], "ingressFromNamespaces must list"),
        ([a.replace("ask-red-hat", "Bad_Key") for a in EGRESS], "must be a DNS label"),
    ],
)
def test_validation_fails_loud(args: list[str], message: str) -> None:
    result = _helm(*args, check=False)
    assert result.returncode != 0
    assert message in result.stderr


VARIANT_DIR = REPO_ROOT / "variants/standalone"
CARD_FILE = VARIANT_DIR / "praxis-egress-ask-red-hat-card.yaml"


def _card_filter(cm: dict) -> dict | None:
    praxis = yaml.safe_load(cm["data"]["praxis.yaml"])
    for f in praxis["filter_chains"][0]["filters"]:
        if f["filter"] == "static_response" and f["conditions"][0]["when"]["path"].endswith("agent-card.json"):
            return f
    return None


def test_no_agent_card_means_card_get_passes_through() -> None:
    assert _card_filter(_get(_resources(*EGRESS), "ConfigMap")) is None


def test_agent_card_is_served_with_gateway_url() -> None:
    import json

    cm = _get(
        _resources(
            *EGRESS,
            "--set", "egressGateways.ask-red-hat.agentCard.name=Upstream Agent",
            "--set", "egressGateways.ask-red-hat.agentCard.url=https://upstream.example.test/a2a/",
            "--set", "egressGateways.ask-red-hat.agentCard.additionalInterfaces[0].url=https://upstream.example.test/grpc",
            "--set", "egressGateways.ask-red-hat.agentCard.supportedInterfaces[0].url=https://upstream.example.test/a2a/",
            "--set", "egressGateways.ask-red-hat.agentCard.supportedInterfaces[0].protocolBinding=JSONRPC",
        ),
        "ConfigMap",
    )
    f = _card_filter(cm)
    assert f["conditions"][0]["when"] == {"path": "/api/ask/v1/a2a/.well-known/agent-card.json", "methods": ["GET"]}
    assert {"name": "Content-Type", "value": "application/json"} in f["headers"]
    card = json.loads(f["body"])
    gateway = "https://praxis-egress-ask-red-hat.praxis-proxy.svc:8443/api/ask/v1/a2a/"
    assert card["url"] == gateway
    assert card["supportedInterfaces"] == [{"url": gateway, "protocolBinding": "JSONRPC"}]
    assert "additionalInterfaces" not in card
    assert card["name"] == "Upstream Agent"
    # Served before routing, so the card GET never reaches the upstream.
    praxis = yaml.safe_load(cm["data"]["praxis.yaml"])
    order = [x["filter"] for x in praxis["filter_chains"][0]["filters"]]
    assert order.index("static_response") < order.index("router")


def test_agent_card_must_be_a_map() -> None:
    result = _helm(*EGRESS, "--set", "egressGateways.ask-red-hat.agentCard=oops", check=False)
    assert result.returncode != 0
    assert "agentCard must be a map" in result.stderr


@pytest.mark.parametrize(
    ("env_file", "upstream", "vault_key"),
    [
        ("praxis-egress-ask-red-hat-stage.yaml", "access.stage.redhat.com:443", "secret/data/global/acme-agent-redhat-sso-stage"),
        ("praxis-egress-ask-red-hat-production.yaml", "access.redhat.com:443", "secret/data/global/acme-agent-redhat-sso-production"),
    ],
)
def test_variant_environment_files(env_file: str, upstream: str, vault_key: str) -> None:
    """Switching stage <-> production swaps upstream and client; the card URL MAS
    stores as base_url stays the gateway's."""
    import json

    res = _resources("-f", str(CARD_FILE), "-f", str(VARIANT_DIR / env_file))
    cm = _get(res, "ConfigMap")
    praxis = yaml.safe_load(cm["data"]["praxis.yaml"])
    lb = next(x for x in praxis["filter_chains"][0]["filters"] if x["filter"] == "load_balancer")
    assert lb["clusters"][0]["endpoints"] == [upstream]
    policy = yaml.safe_load(cm["data"]["policy.yaml"])
    # No credentials in git: both client-id and client-secret come from the
    # environment's Vault record; the client ID is filled in at pod start.
    assert next(p for p in policy["plugins"] if p["kind"] == "delegator/oauth")["config"]["client_id"] == "__PRAXIS_EGRESS_CLIENT_ID__"
    es = _get(res, "ExternalSecret", f"{NAME}-client")
    assert {(d["secretKey"], d["remoteRef"]["key"], d["remoteRef"]["property"]) for d in es["spec"]["data"]} == {
        ("client-secret", vault_key, "client-secret"),
        ("client-id", vault_key, "client-id"),
    }
    card = json.loads(_card_filter(cm)["body"])
    assert card["url"] == "https://praxis-egress-ask-red-hat.praxis-proxy.svc:8443/api/ask/v1/a2a/"
    assert [s["id"] for s in card["skills"]] == ["general_search", "cve_search", "redhat_product_lifecycle", "red_hat_leadership"]


def test_variant_selects_card_and_one_environment() -> None:
    variant = yaml.safe_load((VARIANT_DIR / "values-standalone.yaml").read_text())
    files = variant["clusterGroup"]["applications"]["praxis-proxy"]["extraValueFiles"]
    assert files[0] == "/variants/standalone/praxis-egress-ask-red-hat-card.yaml"
    assert len(files) == 2 and files[1].startswith("/variants/standalone/praxis-egress-ask-red-hat-")
    # ignoreMissingValueFiles is on in the clustergroup chart: a typo would silently drop the gateway.
    for f in files:
        assert (REPO_ROOT / f.lstrip("/")).is_file(), f



def _vault_id_args() -> list[str]:
    return _without("egressGateways.ask-red-hat.clientId")


def test_literal_client_id_needs_no_init_container() -> None:
    res = _resources(*EGRESS)
    pod = _get(res, "Deployment")["spec"]["template"]["spec"]
    assert "initContainers" not in pod
    assert {v["name"]: v for v in pod["volumes"]}["config"]["configMap"]["name"] == NAME
    es = _get(res, "ExternalSecret", f"{NAME}-client")
    assert [d["secretKey"] for d in es["spec"]["data"]] == ["client-secret"]


def test_client_id_from_vault() -> None:
    res = _resources(*_vault_id_args())
    policy = yaml.safe_load(_get(res, "ConfigMap")["data"]["policy.yaml"])
    delegator = next(p for p in policy["plugins"] if p["kind"] == "delegator/oauth")
    assert delegator["config"]["client_id"] == "__PRAXIS_EGRESS_CLIENT_ID__"
    es = _get(res, "ExternalSecret", f"{NAME}-client")
    assert {d["secretKey"]: d["remoteRef"]["property"] for d in es["spec"]["data"]} == {
        "client-secret": "client-secret", "client-id": "client-id",
    }
    pod = _get(res, "Deployment")["spec"]["template"]["spec"]
    init = pod["initContainers"][0]
    assert init["name"] == "render-config"
    assert init["image"] == pod["containers"][0]["image"]  # same pinned image
    assert init["env"] == [{"name": "CLIENT_ID", "valueFrom": {"secretKeyRef": {"name": f"{NAME}-client", "key": "client-id"}}}]
    script = init["args"][0]
    assert "__PRAXIS_EGRESS_CLIENT_ID__" in script and "*[!A-Za-z0-9._-]*" in script
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["config-template"]["configMap"]["name"] == NAME
    assert "emptyDir" in vols["config"]
    mounts = {m["name"]: m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["config"] == "/etc/praxis/config"


def test_client_id_from_existing_secret_key() -> None:
    res = _resources(
        *_without("egressGateways.ask-red-hat.clientSecret", "egressGateways.ask-red-hat.clientId"),
        "--set", "egressGateways.ask-red-hat.clientSecret.existingSecret.name=my-sso",
        "--set", "egressGateways.ask-red-hat.clientSecret.existingSecret.clientIdKey=id",
    )
    init = _get(res, "Deployment")["spec"]["template"]["spec"]["initContainers"][0]
    assert init["env"][0]["valueFrom"]["secretKeyRef"] == {"name": "my-sso", "key": "id"}


def test_init_container_check_is_quote_agnostic_and_loud() -> None:
    script = _get(_resources(*_vault_id_args()), "Deployment")["spec"]["template"]["spec"]["initContainers"][0]["args"][0]
    assert 'grep -q "__PRAXIS_EGRESS_CLIENT_ID__"' in script
    assert 'grep -qF "$CLIENT_ID"' in script
    assert "could not write the client ID into policy.yaml" in script
