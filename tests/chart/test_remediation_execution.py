"""Regression tests for the disabled-by-default remediation execution path.

Read-only investigations use R/U and the existing gateway. Explicitly
approved Execution uses a separate audience/exchanger, gateway, role group and
RBAC grant; these tests pin the contract at each chart boundary.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
VARIANT = ROOT / "variants/standalone/values-standalone.yaml"
KEYCLOAK = ROOT / "charts/all/keycloak-oidc"
PRAXIS = ROOT / "charts/all/praxis-proxy"
HUB = ROOT / "charts/all/lightspeed-hub"


def _render(chart: Path, release: str, *args: str, check: bool = True):
    return subprocess.run(
        ["helm", "template", release, str(chart), "-f", str(VARIANT), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _docs(stdout: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(stdout) if doc]


def _sets(*pairs: tuple[str, str]) -> list[str]:
    return [part for key, value in pairs for part in ("--set", f"{key}={value}")]


KEYCLOAK_EXEC = _sets(
    ("keycloak.spiffeIdentityProvider.enabled", "false"),
    ("keycloak.hubExchange.enabled", "true"),
    ("keycloak.hubExchange.vaultKey", "secret/data/global/test-hub"),
    ("keycloak.hubExchange.clientId", "lightspeed-hub-a2a"),
    ("keycloak.hubExchange.hubAudience", "lightspeed-hub-a2a"),
    ("keycloak.hubExchange.subjectTokenAudienceMapper", "false"),
    ("keycloak.hubExchange.runTokenAudience", "praxis-spoke-gateway"),
    ("keycloak.hubExchange.executionTokenAudience", "praxis-spoke-gateway-exec"),
    ("keycloak.hubGatewayExchanger.enabled", "true"),
    ("keycloak.hubGatewayExchanger.vaultKey", "secret/data/global/test-hub-gateway"),
    ("keycloak.spokeGatewayExchanger.enabled", "true"),
    ("keycloak.spokeGatewayExchanger.subjectTokenAudienceMapper", "false"),
    ("keycloak.spokeGatewayExchanger.vaultKey", "secret/data/global/test-read"),
    ("keycloak.spokeGatewayExchanger.spokeAudiences[0]", "praxis-spoke-east"),
    ("keycloak.spokeGatewayExecExchanger.enabled", "true"),
    ("keycloak.spokeGatewayExecExchanger.vaultKey", "secret/data/global/test-exec"),
    ("keycloak.spokeGatewayExecExchanger.spokeAudiences[0]", "praxis-spoke-east"),
    ("keycloak.exchangeRoles.enabled", "true"),
    ("keycloak.exchangeRoles.groups[0]", "acme-agent-rca"),
    ("keycloak.adminGroupName", "remediation-operators"),
    ("keycloak.exchangeRoles.remediation.groups[0]", "remediation-operators"),
)

HUB_ENABLE = _sets(
    ("enabled", "true"),
    ("adapter.hubAudience", "lightspeed-hub-a2a"),
    ("adapter.keycloak.clientId", "lightspeed-hub-a2a"),
    ("adapter.keycloak.clientSecretSecret.name", "lightspeed-hub-adapter-client"),
    (
        "agenticOperator.config.gatewayServerURLTemplate",
        "https://praxis-spoke-{targetCluster}.praxis-proxy.svc.cluster.local:8443",
    ),
    ("agenticOperator.config.gatewayCASecret", "praxis-spoke-gateway-ca"),
    ("agenticOperator.sandbox.image", "quay.io/example/lightspeed-agentic-sandbox:test"),
)


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required")
def test_keycloak_execution_exchanger_has_separate_audience_and_role_scopes():
    docs = _docs(_render(KEYCLOAK, "keycloak-oidc", *KEYCLOAK_EXEC).stdout)
    realm = next(d["spec"]["realm"] for d in docs if d["kind"] == "KeycloakRealmImport")
    clients = {client["clientId"]: client for client in realm["clients"]}

    hub_client = clients["lightspeed-hub-a2a"]
    hub_audiences = {
        mapper["config"].get("included.custom.audience")
        for mapper in hub_client.get("protocolMappers", [])
    }
    assert {"praxis-spoke-gateway", "praxis-spoke-gateway-exec"} <= hub_audiences

    read = clients["praxis-spoke-gateway"]
    execution = clients["praxis-spoke-gateway-exec"]
    assert read["fullScopeAllowed"] is False
    assert execution["fullScopeAllowed"] is False
    assert execution["serviceAccountsEnabled"] is False
    assert execution["attributes"]["standard.token.exchange.enabled"] == "true"
    assert execution["secret"] == "${PRAXIS_SPOKE_EXEC_EXCHANGER_SECRET}"
    execution_role_mapper = next(
        mapper for mapper in execution["protocolMappers"]
        if mapper.get("protocolMapper") == "oidc-usermodel-client-role-mapper"
    )
    assert execution_role_mapper["config"]["usermodel.clientRoleMapping.rolePrefix"] == "praxis-spoke-east:"

    scopes = realm["clientScopeMappings"]["praxis-spoke-east"]
    assert scopes == [
        {"client": "praxis-spoke-gateway", "roles": ["reader"]},
        {"client": "praxis-spoke-gateway-exec", "roles": ["reader", "writer"]},
    ]
    groups = {group["name"]: group["clientRoles"] for group in realm["groups"]}
    assert groups["acme-agent-rca"] == {
        "lightspeed-hub-a2a": ["agenticrun-creator"],
        "praxis-spoke-east": ["reader"],
    }
    assert groups["remediation-operators"] == {
        "lightspeed-hub-a2a": ["agenticrun-approver"],
        "praxis-spoke-east": ["writer"],
    }
    hub_scope = realm["clientScopeMappings"]["lightspeed-hub-a2a"]
    assert hub_scope == [{
        "client": "mas-a2a",
        "roles": ["agenticrun-creator", "agenticrun-approver"],
    }]

    # The same Vault record is copied only as ExternalSecrets, never as a
    # literal. Keycloak realm import and Praxis each receive their own copy.
    exec_secrets = {
        resource["metadata"]["namespace"]: resource
        for resource in docs
        if resource["kind"] == "ExternalSecret"
        and resource["metadata"]["name"] == "praxis-spoke-gateway-exec-client"
    }
    assert set(exec_secrets) == {"keycloak-system", "praxis-proxy"}
    for secret in exec_secrets.values():
        assert secret["spec"]["data"][0]["remoteRef"] == {
            "key": "secret/data/global/test-exec",
            "property": "praxis-spoke-gateway-exec-client-secret",
        }
    reconciler = next(d for d in docs if d["kind"] == "Job" and d["metadata"]["name"].endswith("-realm-secrets-reconciler"))
    env = {item["name"]: item for item in reconciler["spec"]["template"]["spec"]["containers"][0]["env"]}
    import json

    configured_clients = json.loads(env["EXCHANGE_CLIENTS"]["value"])
    assert any(client["clientId"] == "praxis-spoke-gateway-exec" for client in configured_clients)


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required")
def test_keycloak_creates_dedicated_empty_remediation_group():
    def without(args: list[str], *keys: str) -> list[str]:
        pairs = zip(args[::2], args[1::2])
        return [part for flag, value in pairs if not value.startswith(keys) for part in (flag, value)]

    base = without(KEYCLOAK_EXEC, "keycloak.adminGroupName=", "keycloak.exchangeRoles.remediation.groups[0]=")
    dedicated = base + _sets(("keycloak.exchangeRoles.remediation.groups[0]", "acme-remediation-mario-ipi"))
    docs = _docs(_render(KEYCLOAK, "keycloak-oidc", *dedicated).stdout)
    realm = next(d["spec"]["realm"] for d in docs if d["kind"] == "KeycloakRealmImport")
    groups = {group["name"]: group.get("clientRoles") for group in realm["groups"]}
    assert groups["acme-remediation-mario-ipi"] == {
        "lightspeed-hub-a2a": ["agenticrun-approver"],
        "praxis-spoke-east": ["writer"],
    }
    assert groups["acme-agent-rca"] == {
        "lightspeed-hub-a2a": ["agenticrun-creator"],
        "praxis-spoke-east": ["reader"],
    }
    # No user (including ACME's service account) joins it implicitly.
    for user in realm.get("users", []):
        assert "/acme-remediation-mario-ipi" not in user.get("groups", [])

    blank = _render(
        KEYCLOAK, "keycloak-oidc",
        *base, *_sets(("keycloak.exchangeRoles.remediation.groups[0]", "")),
        check=False,
    )
    assert blank.returncode != 0


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required")
def test_keycloak_writer_limited_to_listed_spokes_and_acme_membership_opt_in():
    def without(args: list[str], *keys: str) -> list[str]:
        pairs = zip(args[::2], args[1::2])
        return [part for flag, value in pairs if not value.startswith(keys) for part in (flag, value)]

    two_spokes = without(
        KEYCLOAK_EXEC,
        "keycloak.adminGroupName=", "keycloak.exchangeRoles.remediation.groups[0]=",
    ) + _sets(
        ("keycloak.spokeGatewayExchanger.spokeAudiences[1]", "praxis-spoke-west"),
        ("keycloak.spokeGatewayExecExchanger.spokeAudiences[1]", "praxis-spoke-west"),
        ("keycloak.exchangeRoles.remediation.groups[0]", "acme-remediation-east"),
        ("keycloak.exchangeRoles.remediation.spokeAudiences[0]", "praxis-spoke-east"),
    )

    def realm_for(*extra: str) -> dict:
        docs = _docs(_render(KEYCLOAK, "keycloak-oidc", *two_spokes, *extra).stdout)
        return next(d["spec"]["realm"] for d in docs if d["kind"] == "KeycloakRealmImport")

    realm = realm_for()
    roles = {aud: [r["name"] for r in items] for aud, items in realm["roles"]["client"].items()}
    assert roles["praxis-spoke-east"] == ["reader", "writer"]
    assert roles["praxis-spoke-west"] == ["reader"]
    groups = {g["name"]: g.get("clientRoles") for g in realm["groups"]}
    assert groups["acme-remediation-east"] == {
        "lightspeed-hub-a2a": ["agenticrun-approver"],
        "praxis-spoke-east": ["writer"],
    }
    scopes = realm["clientScopeMappings"]
    assert {"client": "praxis-spoke-gateway-exec", "roles": ["reader", "writer"]} in scopes["praxis-spoke-east"]
    assert {"client": "praxis-spoke-gateway-exec", "roles": ["reader"]} in scopes["praxis-spoke-west"]

    def acme_groups(realm_doc: dict) -> list[str]:
        return next(u for u in realm_doc["users"] if u.get("serviceAccountClientId") == "acme-agent")["groups"]

    flag = "keycloak.exchangeRoles.remediation.includeAcmeServiceAccount"
    assert acme_groups(realm) == ["/acme-agent-rca"]  # opt-in is off by default
    for off in (["--set", f"{flag}=false"], ["--set-string", f"{flag}=false"], ["--set-string", f"{flag}="]):
        assert acme_groups(realm_for(*off)) == ["/acme-agent-rca"], off
    for on in (["--set", f"{flag}=true"], ["--set-string", f"{flag}=true"]):
        assert acme_groups(realm_for(*on)) == ["/acme-agent-rca", "/acme-remediation-east"], on

    unknown = _render(
        KEYCLOAK, "keycloak-oidc", *two_spokes,
        *_sets(("keycloak.exchangeRoles.remediation.spokeAudiences[1]", "praxis-spoke-north")),
        check=False,
    )
    assert unknown.returncode != 0
    assert "not a registered spoke audience" in unknown.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required")
def test_keycloak_rejects_execution_audience_without_matching_exchange():
    orphan = _render(
        KEYCLOAK,
        "keycloak-oidc",
        "--set", "keycloak.spiffeIdentityProvider.enabled=false",
        "--set", "keycloak.hubExchange.enabled=true",
        "--set", "keycloak.hubExchange.vaultKey=secret/data/global/test-hub",
        "--set", "keycloak.hubExchange.hubAudience=hub-resource",
        "--set", "keycloak.hubExchange.executionTokenAudience=praxis-spoke-gateway-exec",
        check=False,
    )
    assert orphan.returncode != 0
    assert "executionTokenAudience requires keycloak.spokeGatewayExecExchanger.enabled" in orphan.stderr

    mismatch = _render(
        KEYCLOAK,
        "keycloak-oidc",
        *KEYCLOAK_EXEC,
        "--set", "keycloak.spokeGatewayExecExchanger.spokeAudiences[0]=praxis-spoke-west",
        check=False,
    )
    assert mismatch.returncode != 0
    assert "read and execution gateways must target the same spokes" in mismatch.stderr


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required")
def test_praxis_execution_mode_has_separate_service_config_and_secret():
    read_sets = _sets(
        ("identity.keycloak.audience", "mas-a2a"),
        ("spokeGateways.east.remoteAPIServer", "https://api.east.example.test:6443"),
        ("spokeGateways.east.exchangeAudience", "praxis-spoke-east"),
        ("spokeGateways.east.remoteCACert.secretName", "east-api-ca"),
    )
    base_docs = _docs(_render(PRAXIS, "praxis-proxy", *read_sets).stdout)
    assert not any(
        d.get("metadata", {}).get("name") == "praxis-spoke-east-exec"
        for d in base_docs
    )

    execution_sets = [
        *read_sets,
        "--set", "spokeGatewayConfig.execution.enabled=true",
        "--set", "spokeGatewayConfig.execution.exchangeClient.clientId=praxis-spoke-gateway-exec",
        "--set", "spokeGatewayConfig.execution.exchangeClient.secretName=praxis-spoke-gateway-exec-client",
    ]
    docs = _docs(_render(PRAXIS, "praxis-proxy", *execution_sets).stdout)
    dep_by_name = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}
    service_by_name = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Service"}
    policy_by_name = {d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"}
    assert {"praxis-spoke-east", "praxis-spoke-east-exec"} <= set(dep_by_name)
    assert service_by_name["praxis-spoke-east-exec"]["spec"]["selector"]["praxis-proxy.spoke-gateway-mode"] == "exec"
    assert policy_by_name["praxis-spoke-east"]["spec"]["podSelector"]["matchLabels"]["praxis-proxy.spoke-gateway-mode"] == "read"
    assert policy_by_name["praxis-spoke-east-exec"]["spec"]["podSelector"]["matchLabels"]["praxis-proxy.spoke-gateway-mode"] == "exec"

    read_service_selector = service_by_name["praxis-spoke-east"]["spec"]["selector"]
    # HEAD's read Service selector is unchanged: name + instance + spoke key.
    assert read_service_selector == {
        "app.kubernetes.io/name": "praxis-proxy",
        "app.kubernetes.io/instance": "praxis-proxy",
        "praxis-proxy.spoke-gateway": "east",
    }

    # HEAD's pre-execution selector was name + instance + spoke key. Keep it
    # byte-for-byte equivalent: Deployment selectors are immutable on upgrade.
    read_selector = dep_by_name["praxis-spoke-east"]["spec"]["selector"]
    assert read_selector == {
        "matchLabels": {
            "app.kubernetes.io/name": "praxis-proxy",
            "app.kubernetes.io/instance": "praxis-proxy",
            "praxis-proxy.spoke-gateway": "east",
        }
    }
    exec_selector = dep_by_name["praxis-spoke-east-exec"]["spec"]["selector"]
    read_pod_labels = dep_by_name["praxis-spoke-east"]["spec"]["template"]["metadata"]["labels"]
    exec_pod_labels = dep_by_name["praxis-spoke-east-exec"]["spec"]["template"]["metadata"]["labels"]

    def selector_matches(selector: dict, labels: dict) -> bool:
        return all(labels.get(key) == value for key, value in selector["matchLabels"].items())

    assert selector_matches(read_selector, read_pod_labels)
    assert selector_matches(exec_selector, exec_pod_labels)
    assert not selector_matches(read_selector, exec_pod_labels)
    assert not selector_matches(exec_selector, read_pod_labels)
    assert read_pod_labels["app.kubernetes.io/name"] == "praxis-proxy"
    assert exec_pod_labels["app.kubernetes.io/name"] != read_pod_labels["app.kubernetes.io/name"]

    exec_service_selector = service_by_name["praxis-spoke-east-exec"]["spec"]["selector"]

    def service_selector_matches(selector: dict, labels: dict) -> bool:
        return all(labels.get(key) == value for key, value in selector.items())

    assert service_selector_matches(read_service_selector, read_pod_labels)
    assert service_selector_matches(exec_service_selector, exec_pod_labels)
    assert not service_selector_matches(read_service_selector, exec_pod_labels)
    assert not service_selector_matches(exec_service_selector, read_pod_labels)

    read_peer = policy_by_name["praxis-spoke-east"]["spec"]["ingress"][0]["from"][0]
    exec_peer = policy_by_name["praxis-spoke-east-exec"]["spec"]["ingress"][0]["from"][0]
    step_label = "agentic.openshift.io/step"
    assert read_peer["podSelector"] == {
        "matchExpressions": [{"key": step_label, "operator": "Exists"}]
    }
    assert exec_peer["podSelector"] == {"matchLabels": {step_label: "execution"}}
    assert set(read_peer) == {"namespaceSelector", "podSelector"}
    assert set(exec_peer) == {"namespaceSelector", "podSelector"}

    custom_selector = _render(
        PRAXIS,
        "praxis-proxy",
        *execution_sets,
        "--set-json",
        'spokeGatewayConfig.networkPolicy.ingressPodSelectors.read={"matchLabels":{"sandbox":"true"}}',
    )
    custom_read_policy = next(
        d for d in _docs(custom_selector.stdout)
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "praxis-spoke-east"
    )
    assert custom_read_policy["spec"]["ingress"][0]["from"][0]["podSelector"] == {
        "matchExpressions": [{"key": step_label, "operator": "Exists"}],
        "matchLabels": {"sandbox": "true"},
    }

    empty_ingress_namespaces = _render(
        PRAXIS,
        "praxis-proxy",
        *read_sets,
        "--set-json", "spokeGatewayConfig.networkPolicy.ingressFromNamespaces=[]",
        check=False,
    )
    assert empty_ingress_namespaces.returncode != 0
    assert "ingressFromNamespaces must list at least one namespace" in empty_ingress_namespaces.stderr

    read_env = {e["name"]: e for e in dep_by_name["praxis-spoke-east"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    exec_env = {e["name"]: e for e in dep_by_name["praxis-spoke-east-exec"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "PRAXIS_SPOKE_EXCHANGE_CLIENT_SECRET" in read_env
    assert "PRAXIS_SPOKE_EXEC_EXCHANGE_CLIENT_SECRET" not in read_env
    assert "PRAXIS_SPOKE_EXEC_EXCHANGE_CLIENT_SECRET" in exec_env
    assert "PRAXIS_SPOKE_EXCHANGE_CLIENT_SECRET" not in exec_env
    assert exec_env["PRAXIS_SPOKE_EXEC_EXCHANGE_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "praxis-spoke-gateway-exec-client",
        "key": "client-secret",
    }

    configs = {
        d["metadata"]["name"]: yaml.safe_load(d["data"]["praxis.yaml"])
        for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"].startswith("praxis-spoke-east")
    }
    assert set(configs) == {"praxis-spoke-east", "praxis-spoke-east-exec"}
    policies = {
        d["metadata"]["name"]: yaml.safe_load(d["data"]["policy.yaml"])
        for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"].startswith("praxis-spoke-east")
    }
    for mode in ("praxis-spoke-east", "praxis-spoke-east-exec"):
        plugin = next(p for p in policies[mode]["plugins"] if p["name"] == "spoke-delegator")
        delegation = plugin["config"]
        assert delegation["client_id"] == (
            "praxis-spoke-gateway" if mode == "praxis-spoke-east" else "praxis-spoke-gateway-exec"
        )
        assert configs[mode]["filter_chains"][0]["filters"][-1]["filter"] == "load_balancer"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required")
@pytest.mark.parametrize("execution", [False, True])
def test_praxis_gateway_resource_names_are_unique(execution):
    spokes = {
        key: {
            "remoteAPIServer": f"https://api.{key}.example.test:6443",
            "exchangeAudience": f"praxis-spoke-{key}",
            "remoteCACert": {"secretName": f"{key}-api-ca"},
        }
        for key in ("east", "east-exec", "west")
    }
    import json

    args = [
        "--set", "identity.keycloak.audience=mas-a2a",
        "--set-json", f"spokeGateways={json.dumps(spokes)}",
        "--set", f"spokeGatewayConfig.execution.enabled={str(execution).lower()}",
        "--set", "spokeGatewayConfig.execution.exchangeClient.clientId=test-exec",
        "--set", "spokeGatewayConfig.execution.exchangeClient.secretName=test-exec",
    ]
    rendered = _render(PRAXIS, "praxis-proxy", *args, check=False)
    if execution:
        assert rendered.returncode != 0
        assert "resource name collision" in rendered.stderr
        assert "praxis-spoke-east-exec" in rendered.stderr
        # Noncolliding spokes still render both modes, with no identity reuse.
        del spokes["east-exec"]
        args[3] = f"spokeGateways={json.dumps(spokes)}"
        rendered = _render(PRAXIS, "praxis-proxy", *args)
    else:
        assert rendered.returncode == 0, rendered.stderr
    docs = _docs(rendered.stdout)
    identities = [
        (d["kind"], d["metadata"].get("namespace", "default"), d["metadata"]["name"])
        for d in docs
    ]
    assert len(identities) == len(set(identities))
    services = {d["metadata"]["name"] for d in docs if d["kind"] == "Service"}
    assert "praxis-spoke-east" in services
    assert ("praxis-spoke-east-exec" in services)


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required")
def test_lightspeed_hub_execution_flag_config_and_scoped_rbac():
    values = [
        *HUB_ENABLE,
        "--set", "adapter.runTokenAudience=praxis-spoke-gateway",
        "--set", "adapter.executionTokenAudience=praxis-spoke-gateway-exec",
        "--set", "agenticOperator.config.gatewayExecutionServerURLTemplate=https://praxis-spoke-{targetCluster}-exec.praxis-proxy.svc.cluster.local:8443",
        "--set", "adapter.hubRunApproversGroup=keycloak:lightspeed-hub-a2a:agenticrun-approver",
        "--set", "spokeExecutorRBAC.enabled=true",
        "--set", "spokeExecutorRBAC.groups[0]=keycloak:praxis-spoke-hub:writer",
        "--set", "spokeExecutorRBAC.namespaces[0]=payments",
    ]
    docs = _docs(_render(HUB, "lightspeed-hub", *values).stdout)

    adapter = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "lightspeed-hub")
    args = adapter["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--run-token-audience=praxis-spoke-gateway" in args
    assert "--execution-token-audience=praxis-spoke-gateway-exec" in args

    config = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "lightspeed-agentic-configuration")
    assert config["data"]["gateway-server-url-template"] == "https://praxis-spoke-{targetCluster}.praxis-proxy.svc.cluster.local:8443"
    assert config["data"]["gateway-execution-server-url-template"] == "https://praxis-spoke-{targetCluster}-exec.praxis-proxy.svc.cluster.local:8443"

    approval_role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "lightspeed-hub-run-approvers")
    assert approval_role["rules"] == [{
        "apiGroups": ["agentic.openshift.io"],
        "resources": ["agenticrunapprovals"],
        "verbs": ["get", "update"],
    }]
    approval_binding = next(d for d in docs if d["kind"] == "RoleBinding" and d["metadata"]["name"] == "lightspeed-hub-run-approvers")
    assert approval_binding["subjects"] == [{
        "kind": "Group",
        "name": "keycloak:lightspeed-hub-a2a:agenticrun-approver",
        "apiGroup": "rbac.authorization.k8s.io",
    }]

    executor = next(d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "lightspeed-hub-spoke-executor")
    all_resources = {resource for rule in executor["rules"] for resource in rule["resources"]}
    assert "pods/eviction" in all_resources
    assert not {"secrets", "pods/exec", "pods/attach", "pods/portforward"} & all_resources
    assert all("*" not in rule["resources"] and "*" not in rule["verbs"] for rule in executor["rules"])
    assert not any(d["kind"] == "ClusterRoleBinding" and d["metadata"]["name"] == "lightspeed-hub-spoke-executor" for d in docs)
    executor_binding = next(d for d in docs if d["kind"] == "RoleBinding" and d["metadata"]["name"] == "lightspeed-hub-spoke-executor")
    assert executor_binding["metadata"]["namespace"] == "payments"
    assert executor_binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "lightspeed-hub-spoke-executor",
    }
    assert executor_binding["subjects"] == [{
        "kind": "Group",
        "apiGroup": "rbac.authorization.k8s.io",
        "name": "keycloak:praxis-spoke-hub:writer",
    }]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required")
def test_lightspeed_hub_execution_dependencies_fail_closed():
    no_read_audience = _render(
        HUB,
        "lightspeed-hub",
        *HUB_ENABLE,
        "--set", "adapter.executionTokenAudience=praxis-spoke-gateway-exec",
        check=False,
    )
    assert no_read_audience.returncode != 0
    assert "requires adapter.runTokenAudience" in no_read_audience.stderr

    no_exec_url = _render(
        HUB,
        "lightspeed-hub",
        *HUB_ENABLE,
        "--set", "adapter.runTokenAudience=praxis-spoke-gateway",
        "--set", "adapter.executionTokenAudience=praxis-spoke-gateway-exec",
        check=False,
    )
    assert no_exec_url.returncode != 0
    assert "requires agenticOperator.config.gatewayExecutionServerURLTemplate" in no_exec_url.stderr

    no_exec_audience = _render(
        HUB,
        "lightspeed-hub",
        *HUB_ENABLE,
        "--set", "agenticOperator.config.gatewayExecutionServerURLTemplate=https://exec.example.test",
        check=False,
    )
    assert no_exec_audience.returncode != 0
    assert "requires adapter.executionTokenAudience" in no_exec_audience.stderr

    no_approver_group = _render(
        HUB,
        "lightspeed-hub",
        *HUB_ENABLE,
        "--set", "spokeExecutorRBAC.enabled=true",
        check=False,
    )
    assert no_approver_group.returncode != 0
    assert "spokeExecutorRBAC.groups must list at least one" in no_approver_group.stderr

    no_writer_namespaces = _render(
        HUB,
        "lightspeed-hub",
        *HUB_ENABLE,
        "--set", "spokeExecutorRBAC.enabled=true",
        "--set", "spokeExecutorRBAC.groups[0]=keycloak:praxis-spoke-hub:writer",
        check=False,
    )
    assert no_writer_namespaces.returncode != 0
    assert "spokeExecutorRBAC.namespaces must list at least one" in no_writer_namespaces.stderr

    empty_writer_namespaces = _render(
        HUB,
        "lightspeed-hub",
        *HUB_ENABLE,
        "--set", "spokeExecutorRBAC.enabled=true",
        "--set", "spokeExecutorRBAC.groups[0]=keycloak:praxis-spoke-hub:writer",
        "--set-json", "spokeExecutorRBAC.namespaces=[]",
        check=False,
    )
    assert empty_writer_namespaces.returncode != 0
    assert "spokeExecutorRBAC.namespaces must list at least one" in empty_writer_namespaces.stderr

    for namespace in ("default", "openshift-user-workload", "kube-system", "praxis-proxy", "keycloak-system"):
        blocked = _render(
            HUB,
            "lightspeed-hub",
            *HUB_ENABLE,
            "--set", "spokeExecutorRBAC.enabled=true",
            "--set", "spokeExecutorRBAC.groups[0]=keycloak:praxis-spoke-hub:writer",
            "--set", f"spokeExecutorRBAC.namespaces[0]={namespace}",
            check=False,
        )
        assert blocked.returncode != 0
        assert "spokeExecutorRBAC.namespaces contains forbidden namespace" in blocked.stderr

    blocked_run_namespace = _render(
        HUB,
        "lightspeed-hub",
        *HUB_ENABLE,
        "--set", "adapter.runNamespace=run-ops",
        "--set", "spokeExecutorRBAC.enabled=true",
        "--set", "spokeExecutorRBAC.groups[0]=keycloak:praxis-spoke-hub:writer",
        "--set", "spokeExecutorRBAC.namespaces[0]=run-ops",
        check=False,
    )
    assert blocked_run_namespace.returncode != 0
    assert "spokeExecutorRBAC.namespaces contains forbidden namespace" in blocked_run_namespace.stderr

    blocked_release_namespace = _render(
        HUB,
        "lightspeed-hub",
        *HUB_ENABLE,
        "--namespace", "workload-control",
        "--set", "spokeExecutorRBAC.enabled=true",
        "--set", "spokeExecutorRBAC.groups[0]=keycloak:praxis-spoke-hub:writer",
        "--set", "spokeExecutorRBAC.namespaces[0]=workload-control",
        check=False,
    )
    assert blocked_release_namespace.returncode != 0
    assert "spokeExecutorRBAC.namespaces contains forbidden namespace" in blocked_release_namespace.stderr


def test_standalone_variant_enables_a_consistent_scoped_execution_path():
    """Greenfield: one values file enables Keycloak, Praxis and Hub together.

    The lab grants write access only on the remote target (mario-ipi), so
    the hub chart must not render spoke writer bindings, and ACME's token
    debug/override must be off once its account may approve and write.
    """
    variant = yaml.safe_load(VARIANT.read_text())
    applications = variant["clusterGroup"]["applications"]

    def overrides(app: str) -> dict[str, str]:
        return {item["name"]: item["value"] for item in applications[app].get("overrides", [])}

    keycloak = overrides("keycloak-oidc")
    praxis = overrides("praxis-proxy")
    hub = overrides("lightspeed-hub")
    acme = overrides("acme-agent")
    keycloak_on = keycloak.get("keycloak.spokeGatewayExecExchanger.enabled") == "true"
    praxis_on = praxis.get("spokeGatewayConfig.execution.enabled") == "true"
    hub_on = "adapter.executionTokenAudience" in hub
    assert keycloak_on == praxis_on == hub_on, "enable the execution path in all three charts together"
    assert keycloak_on == ("keycloak.hubExchange.executionTokenAudience" in keycloak)
    assert hub_on == ("agenticOperator.config.gatewayExecutionServerURLTemplate" in hub)
    assert "spokeExecutorRBAC.enabled" not in hub
    if not keycloak_on:
        return

    assert acme.get("auth.tokenDebug.enabled") != "true"
    assert acme.get("auth.tokenOverride.enabled") != "true"
    remediation = keycloak["keycloak.exchangeRoles.remediation.groups[0]"]
    ordinary = {value for key, value in keycloak.items() if key.startswith("keycloak.exchangeRoles.groups[")}
    assert remediation not in ordinary | {keycloak.get("keycloak.adminGroupName")}
    writer = {value for key, value in keycloak.items()
              if key.startswith("keycloak.exchangeRoles.remediation.spokeAudiences[")}
    spokes = {value for key, value in keycloak.items()
              if key.startswith("keycloak.spokeGatewayExecExchanger.spokeAudiences[")}
    assert writer and writer <= spokes, "list the writer spokes explicitly"
    assert hub["adapter.hubRunApproversGroup"] == "keycloak:lightspeed-hub-a2a:agenticrun-approver"


SPOKE_WRITER_RBAC = ROOT / "scripts/remediation/spoke-writer-rbac.sh"


def test_spoke_writer_rbac_script_mirrors_chart_rules():
    """The spoke-side writer RBAC uses the chart's reviewed rules only."""
    out = subprocess.run(
        ["bash", str(SPOKE_WRITER_RBAC), "mario-ipi", "payments", "payments-canary"],
        check=True, capture_output=True, text=True,
    ).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    chart_rules = yaml.safe_load((HUB / "values.yaml").read_text())["spokeExecutorRBAC"]["rules"]
    role = next(d for d in docs if d["kind"] == "ClusterRole")
    bindings = [d for d in docs if d["kind"] == "RoleBinding"]
    assert role["rules"] == chart_rules
    assert [b["metadata"]["namespace"] for b in bindings] == ["payments", "payments-canary"]
    for binding in bindings:
        assert binding["subjects"] == [{
            "kind": "Group", "apiGroup": "rbac.authorization.k8s.io",
            "name": "keycloak:praxis-spoke-mario-ipi:writer",
        }]
        assert binding["roleRef"]["name"] == role["metadata"]["name"]
    assert {d["kind"] for d in docs} == {"ClusterRole", "RoleBinding"}
    for forbidden in ("default", "openshift-config", "kube-system", "lightspeed-hub"):
        refused = subprocess.run(["bash", str(SPOKE_WRITER_RBAC), "mario-ipi", forbidden],
                                 capture_output=True, text=True)
        assert refused.returncode != 0
