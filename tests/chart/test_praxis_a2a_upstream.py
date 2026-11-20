"""Regression tests for the a2aUpstream front-door hop (Task B request leg).

Leg 2 of the lightspeed-hub architecture (docs/lightspeed-hub-plan.html s3
"Praxis stays the front door"): when a2aUpstream is enabled, the MAIN
gateway forwards A2A traffic to the hub A2A adapter Service over verified
service-CA TLS, with caller x target rules in the router (one route per
registered spokeGateways key -- the allow-list IS the spoke map). Token A
is validated, NOT exchanged, at Praxis: the adapter performs A->H, and
a2aUpstream.exchangeAtPraxis fails the render loudly (no protected-A
handoff exists on the pinned image).

Disabled by default, and the disabled render must be BYTE-IDENTICAL to the
pre-a2aUpstream one: the router/load_balancer additions live inside a
single {{- if }} in files/praxis.yaml, pinned here by comparing the main
gateway ConfigMap across renders that vary every other knob.
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
    "--set", "spokeGateways.west.remoteAPIServer=https://api.west.example.test:6443",
    "--set", "spokeGateways.west.exchangeAudience=praxis-spoke-west",
    "--set", "spokeGateways.west.remoteCACert.secretName=praxis-spoke-west-api-ca",
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


def _main_configmap(docs: list[dict]) -> dict:
    return next(
        d for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "praxis-proxy"
    )


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_disabled_render_is_byte_identical() -> None:
    """The disabled a2aUpstream must not change the main gateway's config.

    Leak-isolation pattern (cf. test_praxis_spoke_gateways): compare the
    main gateway ConfigMap's praxis.yaml/policy.yaml BYTES across renders
    that vary everything else -- default values, a2aUpstream values set
    but enabled=false, and spoke gateways enabled. All three must be
    identical, proving the {{- if }} gates leave no residue.
    """
    base = _helm(CHART, "--set", "identity.keycloak.audience=mas-a2a")
    with_values = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "a2aUpstream.enabled=false",
        "--set", "a2aUpstream.service=some-other-service",
        "--set", "a2aUpstream.namespace=other-ns",
        "--set", "a2aUpstream.port=9999",
        "--set", "a2aUpstream.targetHeader=X-Other-Header",
    )
    with_spokes = _helm(
        CHART, "--set", "identity.keycloak.audience=mas-a2a", *SPOKE_SETS
    )

    base_cm = _main_configmap(_resources(base.stdout))
    for other in (with_values, with_spokes):
        cm = _main_configmap(_resources(other.stdout))
        assert cm["data"]["praxis.yaml"] == base_cm["data"]["praxis.yaml"]
        assert cm["data"]["policy.yaml"] == base_cm["data"]["policy.yaml"]

    # And the default render carries none of the request-leg machinery.
    assert "hub-adapter" not in base_cm["data"]["praxis.yaml"]
    config = yaml.safe_load(base_cm["data"]["praxis.yaml"])
    assert not any(
        f.get("filter") in ("router", "load_balancer")
        for f in config["filter_chains"][0]["filters"]
    )


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_enabled_renders_route_to_adapter_service() -> None:
    """Enabled: router routes per spoke + hub-adapter cluster with TLS."""
    rendered = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "a2aUpstream.enabled=true",
        *SPOKE_SETS,
    )
    docs = _resources(rendered.stdout)
    cm = _main_configmap(docs)
    config = yaml.safe_load(cm["data"]["praxis.yaml"])
    filters = config["filter_chains"][0]["filters"]

    router = next(f for f in filters if f["filter"] == "router")
    # One route per REGISTERED spoke (the allow-list IS the spokeGateways
    # map), each requiring an exact target-header value match.
    # The two header-less agent-card pass-through routes come FIRST; then
    # one route per REGISTERED spoke (the allow-list IS the spokeGateways
    # map), each requiring an exact target-header value match.
    assert router["routes"] == [
        {"path_prefix": "/a2a/.well-known/agent-card.json", "cluster": "hub-adapter"},
        {"path_prefix": "/.well-known/agent-card.json", "cluster": "hub-adapter"},
        {"path_prefix": "/", "headers": {"X-Hub-Cluster": "east"}, "cluster": "hub-adapter"},
        {"path_prefix": "/", "headers": {"X-Hub-Cluster": "west"}, "cluster": "hub-adapter"},
    ]
    # No catch-all route: a request with a MISSING or UNREGISTERED target
    # header matches no route and is answered 404 by Core (verified live
    # during the pre-Task-A routing era). Only the card paths are
    # header-less; every route that can match an arbitrary path requires
    # the target header.
    for r in router["routes"]:
        if "headers" not in r:
            assert r["path_prefix"].endswith("/.well-known/agent-card.json")

    # Single fixed upstream: the adapter Service over HTTPS with
    # service-CA verification (same mechanism as the spoke gateways).
    lb = next(f for f in filters if f["filter"] == "load_balancer")
    assert lb["clusters"] == [
        {
            "name": "hub-adapter",
            "endpoints": ["lightspeed-hub.lightspeed-hub.svc.cluster.local:8443"],
            "tls": {
                "sni": "lightspeed-hub.lightspeed-hub.svc.cluster.local",
                "verify": True,
                "ca": {"ca_path": "/etc/praxis/hub-adapter-ca/service-ca.crt"},
            },
        }
    ]

    # The service-CA bundle ConfigMap the cluster verifies TLS with.
    ca_cm = next(
        d for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "praxis-hub-adapter-ca"
    )
    assert (
        ca_cm["metadata"]["annotations"]["service.beta.openshift.io/inject-cabundle"]
        == "true"
    )
    # ...and the main Deployment mounts it.
    dep = next(
        d for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "praxis-proxy"
    )
    volumes = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
    assert volumes["hub-adapter-ca"]["configMap"]["name"] == "praxis-hub-adapter-ca"
    mounts = dep["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert any(
        m["name"] == "hub-adapter-ca"
        and m["mountPath"] == "/etc/praxis/hub-adapter-ca"
        for m in mounts
    )

    # A custom service/port override flows through to the endpoint/SNI.
    custom = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "a2aUpstream.enabled=true",
        "--set", "a2aUpstream.service=hub-adaptersvc",
        "--set", "a2aUpstream.namespace=hub-ns",
        "--set", "a2aUpstream.port=9443",
        *SPOKE_SETS,
    )
    cfg = yaml.safe_load(_main_configmap(_resources(custom.stdout))["data"]["praxis.yaml"])
    lbs = [f for f in cfg["filter_chains"][0]["filters"] if f["filter"] == "load_balancer"]
    assert lbs[0]["clusters"][0]["endpoints"] == ["hub-adaptersvc.hub-ns.svc.cluster.local:9443"]
    assert lbs[0]["clusters"][0]["tls"]["sni"] == "hub-adaptersvc.hub-ns.svc.cluster.local"


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_agent_card_is_passed_through_from_adapter() -> None:
    """The discovery card is the adapter's own, never a static Praxis copy.

    VERIFIED-BROKEN LIVE (2026-11): the static card Praxis used to serve
    advertised protocolVersion "1.0", so MAS (a2a-sdk 1.1.5) called
    SendStreamingMessage against the A2A 0.3 adapter -> "method not found"
    on every delegation. Now no static card filter is rendered; readiness
    stays local; the policy still exempts only GET on the two exact card
    paths (the router routes them header-less to hub-adapter -- asserted in
    test_enabled_renders_route_to_adapter_service).
    """
    rendered = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "a2aUpstream.enabled=true",
        *SPOKE_SETS,
    )
    cm = _main_configmap(_resources(rendered.stdout))
    filters = yaml.safe_load(cm["data"]["praxis.yaml"])["filter_chains"][0]["filters"]

    statics = [f for f in filters if f["filter"] == "static_response"]
    assert [s["conditions"] for s in statics] == [
        [{"when": {"path": "/readiness", "methods": ["GET"]}}]
    ], "only readiness may be answered locally -- no static agent card"

    for path in ("/.well-known/agent-card.json", "/a2a/.well-known/agent-card.json"):
        assert f"path: {path}\n      method: GET" in cm["data"]["policy.yaml"]


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_variant_has_no_static_card_overrides() -> None:
    """The variant must not carry overrides for the removed static card."""
    overrides = yaml.safe_load(VARIANT.read_text())["clusterGroup"]["applications"][
        "praxis-proxy"
    ]["overrides"]
    names = [o["name"] for o in overrides]
    assert not [n for n in names if n.startswith("agentCard.")]
    assert {o["name"]: o["value"] for o in overrides}["a2aUpstream.enabled"] == "true"


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_enabled_requires_registered_spokes() -> None:
    """Empty spokeGateways + enabled a2aUpstream must fail the render.

    The caller x target allow-list IS the spokeGateways map; an enabled
    front door with no registered spokes would route nothing while
    looking configured.
    """
    result = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "a2aUpstream.enabled=true",
        check=False,
    )
    assert result.returncode != 0
    assert "requires at least one spokeGateways entry" in result.stderr


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_exchange_at_praxis_fails_loudly() -> None:
    """exchangeAtPraxis=true is documented but NOT SUPPORTED: fail loudly.

    The ADAPTER performs the A->H exchange on this leg. Front-door
    delegation would replace the outbound Authorization with H and hide
    Token A from the adapter (no protected-A handoff on the pinned image)
    -- rendering the spoke-gateway delegator pattern here half-wired
    would silently destroy the run leg's subject token, so the render
    refuses instead.
    """
    result = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "a2aUpstream.enabled=true",
        "--set", "a2aUpstream.exchangeAtPraxis=true",
        *SPOKE_SETS,
        check=False,
    )
    assert result.returncode != 0
    assert "a2aUpstream.exchangeAtPraxis=true is NOT SUPPORTED" in result.stderr
    assert "protected-A handoff" in result.stderr


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_enabled_requires_policy() -> None:
    """The front door must keep validating: policy.enabled=false fails."""
    result = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "a2aUpstream.enabled=true",
        "--set", "policy.enabled=false",
        *SPOKE_SETS,
        check=False,
    )
    assert result.returncode != 0
    assert "a2aUpstream.enabled requires policy.enabled=true" in result.stderr


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_enabled_keeps_azp_allow_list() -> None:
    """Token A is validated, not exchanged: the azp allow-list stays.

    The catch-all route's pre_invocation must still deny callers whose
    claim.azp is not in policy.allowedCallers when a2aUpstream is enabled
    -- routing to the adapter must never bypass identity enforcement.
    """
    rendered = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "a2aUpstream.enabled=true",
        *SPOKE_SETS,
    )
    cm = _main_configmap(_resources(rendered.stdout))
    policy = yaml.safe_load(cm["data"]["policy.yaml"])
    catchall = policy["routes"][-1]
    assert catchall["authentication"] == {"steps": ["jwt-client"]}
    pre = catchall["authorization"]["pre_invocation"]
    deny_steps = [s for s in pre if "deny" in s]
    assert deny_steps, "azp allow-list deny conjunct must survive a2aUpstream"
    assert "claim.azp != 'acme-agent'" in deny_steps[0]
    # And no delegator leaked into the main gateway's policy.
    assert "delegate(" not in cm["data"]["policy.yaml"]
    assert [p["name"] for p in policy["plugins"]] == ["jwt-client"]


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_multi_valued_target_header_gap_is_pinned() -> None:
    """Pin the documented multi-valued-header gap on the pinned image.

    The pinned Core (v0.7.0, embedded in ai:0.4.1) OR-matches repeated
    header values (router matching.rs `get_all(key).any(...)`;
    guardrails `scan_header_values` has the same shape; the headers
    filter has no reject form), so NO chart configuration can reject a
    duplicate target header at this gateway. What the chart CAN do --
    and what this test pins -- is that every route requires an exact
    single registered value (a headerless or unknown-value request
    matches nothing), while duplicate-header rejection is delegated to
    the ADAPTER (plan adapter-scope item 5) plus the server-side
    admission policy. See charts/all/praxis-proxy/README.md
    "VERIFIED-UNFIXABLE on the pinned image: multi-valued target
    headers"; re-check on the next image bump.
    """
    rendered = _helm(
        CHART,
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set", "a2aUpstream.enabled=true",
        *SPOKE_SETS,
    )
    cm = _main_configmap(_resources(rendered.stdout))
    config = yaml.safe_load(cm["data"]["praxis.yaml"])
    router = next(
        f for f in config["filter_chains"][0]["filters"] if f["filter"] == "router"
    )
    registered = {"east", "west"}
    for route in router["routes"]:
        if route["path_prefix"].endswith("/.well-known/agent-card.json"):
            continue  # header-less agent-card pass-through (public discovery)
        # Exact-value match only: single header key, single spoke value.
        assert set(route["headers"]) == {"X-Hub-Cluster"}
        assert route["headers"]["X-Hub-Cluster"] in registered
    # The gap itself must stay documented next to the routes that cannot
    # close it, so the next image bump re-evaluates it.
    assert "MULTI-VALUED target header cannot" in cm["data"]["praxis.yaml"]


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_keycloak_hub_exchange_wiring() -> None:
    """H-side Keycloak wiring: absent by default, rendered when enabled."""
    def kc(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "helm", "template", "keycloak-oidc", str(KEYCLOAK_CHART),
                "-f", str(VARIANT),
                "--set", "keycloak.spiffeIdentityProvider.enabled=false",
                *args,
            ],
            check=check, capture_output=True, text=True,
        )

    base = kc()
    base_realm = next(
        d for d in _resources(base.stdout) if d["kind"] == "KeycloakRealmImport"
    )
    base_clients = base_realm["spec"]["realm"]["clients"]
    assert "lightspeed-hub-adapter" not in [c["clientId"] for c in base_clients]
    assert "HUB_EXCHANGE_SECRET" not in base.stdout

    enabled = kc(
        "--set", "keycloak.hubExchange.enabled=true",
        "--set", "keycloak.hubExchange.vaultKey=secret/data/global/lightspeed-hub-adapter",
        "--set", "keycloak.hubExchange.hubAudience=lightspeed-hub-a2a",
    )
    docs = _resources(enabled.stdout)
    realm = next(d for d in docs if d["kind"] == "KeycloakRealmImport")
    clients = {c["clientId"]: c for c in realm["spec"]["realm"]["clients"]}

    hub = clients["lightspeed-hub-adapter"]
    assert hub["publicClient"] is False
    assert hub["secret"] == "$(HUB_EXCHANGE_SECRET)"
    assert hub["attributes"] == {"standard.token.exchange.enabled": "true"}
    mappers = {
        (m["name"], m["protocolMapper"]): m.get("config", {})
        for m in hub["protocolMappers"]
    }
    assert mappers[("lightspeed-hub-a2a-audience", "oidc-audience-mapper")][
        "included.custom.audience"
    ] == "lightspeed-hub-a2a"
    # Group mappers so exchanged H carries groups while minted A stays
    # groupless (UNCONFIRMED GUESS on live Keycloak -- see values.yaml).
    assert ("groups", "oidc-group-membership-mapper") in mappers

    # RFC 8693 prerequisite (b): Token A carries the exchanger as audience.
    acme = clients["acme-agent"]
    assert any(
        m.get("config", {}).get("included.custom.audience") == "lightspeed-hub-adapter"
        for m in acme["protocolMappers"]
    )

    # Vault-backed secret materialized in BOTH namespaces (realm-import
    # placeholder + the adapter's namespace).
    secrets = {
        (d["metadata"]["name"], d["metadata"]["namespace"])
        for d in docs if d["kind"] == "ExternalSecret"
    }
    assert ("lightspeed-hub-adapter-client", "keycloak-system") in secrets
    assert ("lightspeed-hub-adapter-client", "lightspeed-hub") in secrets

    # groupMappers=false omits the group mapper (minters stay groupless
    # by other means; the audience mapper still renders).
    no_groups = kc(
        "--set", "keycloak.hubExchange.enabled=true",
        "--set", "keycloak.hubExchange.vaultKey=secret/data/global/lightspeed-hub-adapter",
        "--set", "keycloak.hubExchange.hubAudience=lightspeed-hub-a2a",
        "--set", "keycloak.hubExchange.groupMappers=false",
    )
    realm_ng = next(
        d for d in _resources(no_groups.stdout) if d["kind"] == "KeycloakRealmImport"
    )
    hub_ng = {
        c["clientId"]: c for c in realm_ng["spec"]["realm"]["clients"]
    }["lightspeed-hub-adapter"]
    assert not any(
        m["protocolMapper"] == "oidc-group-membership-mapper"
        for m in hub_ng["protocolMappers"]
    )


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_keycloak_hub_exchange_fail_guards() -> None:
    """hubAudience distinctness + required values fail loudly."""
    def kc(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "helm", "template", "keycloak-oidc", str(KEYCLOAK_CHART),
                "-f", str(VARIANT),
                "--set", "keycloak.spiffeIdentityProvider.enabled=false",
                *args,
            ],
            check=False, capture_output=True, text=True,
        )

    # Missing vaultKey.
    r = kc("--set", "keycloak.hubExchange.enabled=true")
    assert r.returncode != 0
    assert "hubExchange.vaultKey is required" in r.stderr

    # Missing hubAudience.
    r = kc(
        "--set", "keycloak.hubExchange.enabled=true",
        "--set", "keycloak.hubExchange.vaultKey=secret/data/global/x",
    )
    assert r.returncode != 0
    assert "hubExchange.hubAudience is required" in r.stderr

    # Audience distinctness: vs the platform audience, the MAS audiences,
    # the exchanger's own client ID, and spoke exchange audiences.
    base = [
        "--set", "keycloak.hubExchange.enabled=true",
        "--set", "keycloak.hubExchange.vaultKey=secret/data/global/x",
    ]
    for bad_aud, expect in [
        ("openshift", "distinct from openshift"),
        ("mas-a2a", "distinct from openshift"),
        ("mas-api", "distinct from openshift"),
        ("lightspeed-hub-adapter", "distinct from keycloak.hubExchange.clientId"),
    ]:
        r = kc(*base, f"--set=keycloak.hubExchange.hubAudience={bad_aud}")
        assert r.returncode != 0, bad_aud
        assert expect in r.stderr, bad_aud

    r = kc(
        *base,
        "--set", "keycloak.hubExchange.hubAudience=praxis-spoke-east",
        "--set", "keycloak.spokeGatewayExchanger.enabled=true",
        "--set", "keycloak.spokeGatewayExchanger.vaultKey=secret/data/global/y",
        "--set", "keycloak.spokeGatewayExchanger.spokeAudiences[0]=praxis-spoke-east",
    )
    assert r.returncode != 0
    assert "distinct from every keycloak.spokeGatewayExchanger.spokeAudiences entry" in r.stderr
