"""Regression coverage for real Keycloak interpolation and existing-realm repair."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "charts/all/keycloak-oidc"


def render(tmp_path, values):
    if shutil.which("helm") is None:
        pytest.skip("helm is required to render chart")
    overrides = tmp_path / "values.yaml"
    overrides.write_text(yaml.safe_dump(values))
    result = subprocess.run(
        [
            "helm",
            "template",
            "keycloak-oidc",
            str(CHART),
            "-f",
            str(overrides),
            "--set",
            "keycloak.spiffeIdentityProvider.enabled=false",
            "--set",
            "global.clusterBaseDomain=example.test",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [resource for resource in yaml.safe_load_all(result.stdout) if resource]


def exchange_values(mode):
    values = {}
    if mode in ("hub", "gateway", "all"):
        values["hubExchange"] = {
            "enabled": True,
            "vaultKey": "secret/data/global/adapter",
            "hubAudience": "hub-a2a",
            "secretName": "custom-adapter-credential",
        }
    if mode in ("gateway", "all"):
        values["hubExchange"].update(
            clientId="hub-a2a",
            subjectTokenAudienceMapper=False,
            runTokenAudience="praxis-spoke-gateway",
        )
        values["hubGatewayExchanger"] = {
            "enabled": True,
            "vaultKey": "secret/data/global/hub-gateway",
            "secretName": "custom-gateway-credential",
        }
    if mode in ("spoke", "all"):
        values["spokeGatewayExchanger"] = {
            "enabled": True,
            "vaultKey": "secret/data/global/spoke-gateway",
            "secretName": "custom-spoke-credential",
            "subjectTokenAudienceMapper": mode != "all",
        }
    return {"keycloak": values}


@pytest.mark.parametrize("mode", ["spoke", "hub", "gateway", "all"])
def test_exchange_only_hook_wires_import_secrets_and_helper(tmp_path, mode):
    """No console or external IdP is needed to trigger exchanger repair."""
    resources = render(tmp_path, exchange_values(mode))
    spec = next(r for r in resources if r["kind"] == "KeycloakRealmImport")["spec"]
    clients = {c["clientId"]: c for c in spec["realm"]["clients"]}
    job = next(r for r in resources if r["kind"] == "Job")
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PostSync"
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    desired = json.loads(env["EXCHANGE_CLIENTS"]["value"])
    expected = {
        "spoke": {"praxis-spoke-gateway"},
        "hub": {"lightspeed-hub-adapter"},
        "gateway": {"mas-a2a", "hub-a2a"},
        "all": {"mas-a2a", "hub-a2a", "praxis-spoke-gateway"},
    }[mode]
    assert {c["clientId"] for c in desired} == expected
    for client in desired:
        variable = client["secretEnv"]
        reference = spec["placeholders"][variable]["secret"]
        assert clients[client["clientId"]]["secret"] == "${" + variable + "}"
        assert reference == env[variable]["valueFrom"]["secretKeyRef"]
        assert reference == {"name": client["secretName"], "key": "client-secret"}
        assert reference["name"].startswith("custom-")
    volume = next(
        v for v in pod["volumes"] if v["name"] == "exchange-secrets-reconciler"
    )
    configmap = next(
        r for r in resources if r["metadata"]["name"] == volume["configMap"]["name"]
    )
    assert any(m["name"] == volume["name"] for m in container["volumeMounts"])
    compile(container["args"][0], "realm-reconciler.py", "exec")
    script = configmap["data"]["reconcile_exchange_secrets.py"]
    assert script == (CHART / "files/reconcile_exchange_secrets.py").read_text()
    compile(script, "reconcile_exchange_secrets.py", "exec")


def test_all_realm_credentials_use_keycloak_environment_syntax(tmp_path):
    values = exchange_values("all")
    values["keycloak"].update(
        consoleClientSecretVaultKey="secret/data/global/console",
        adminGroupName="test-admins",
        consoleAdminUser={
            "enabled": True,
            "passwordVaultKey": "secret/data/global/admin",
        },
        externalOidcProviders=[
            {
                "alias": "external",
                "issuerUrl": "https://sso.example.test/realms/external",
                "authorizationUrl": "https://sso.example.test/realms/external/auth",
                "tokenUrl": "https://sso.example.test/realms/external/token",
                "jwksUrl": "https://sso.example.test/realms/external/certs",
                "tokenIntrospectionUrl": "https://sso.example.test/realms/external/token/introspect",
                "userInfoUrl": "https://sso.example.test/realms/external/userinfo",
                "credentials": {"existingSecretName": "external-credentials"},
            }
        ],
    )
    resources = render(tmp_path, values)
    spec = next(r for r in resources if r["kind"] == "KeycloakRealmImport")["spec"]
    realm_json = json.dumps(spec["realm"])
    assert "$(" not in realm_json
    referenced = set(re.findall(r"\$\{([A-Z0-9_]+)\}", realm_json))
    assert (
        referenced
        == set(spec["placeholders"])
        == {
            "PRAXIS_SPOKE_EXCHANGER_SECRET",
            "HUB_EXCHANGE_SECRET",
            "HUB_GATEWAY_EXCHANGE_SECRET",
            "CONSOLE_CLIENT_SECRET",
            "CONSOLE_ADMIN_PASSWORD",
            "EXTERNAL_IDP_0_CLIENT_ID",
            "EXTERNAL_IDP_0_CLIENT_SECRET",
        }
    )


@pytest.mark.parametrize("disabled_import", [False, True])
def test_no_exchange_hook_or_script_when_disabled(tmp_path, disabled_import):
    values = exchange_values("all") if disabled_import else {}
    if disabled_import:
        values["realmImport"] = {"enabled": False}
    resources = render(tmp_path, values)
    assert not any(r["kind"] == "Job" for r in resources)
    assert not any(
        r["metadata"]["name"].endswith("-exchange-secrets-reconciler")
        for r in resources
    )


@pytest.fixture
def reconcile():
    spec = importlib.util.spec_from_file_location(
        "exchange_secrets", CHART / "files/reconcile_exchange_secrets.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.reconcile


class FakeKeycloak:
    def __init__(self):
        self.clients = [
            {
                "id": "gateway-id",
                "clientId": "mas-a2a",
                "enabled": True,
                "attributes": {"standard.token.exchange.enabled": "true"},
            },
            {
                "id": "adapter-id",
                "clientId": "hub-a2a",
                "protocolMappers": [{"name": "audience"}],
            },
        ]
        self.secrets = {
            "gateway-id": "$(HUB_GATEWAY_EXCHANGE_SECRET)",
            "adapter-id": "old-value",
        }
        self.writes = []
        self.ignore_writes = False

    def call(self, method, path, data=None):
        if method == "GET" and "?" in path:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            # Include a fuzzy match to ensure the reconciler filters exactly.
            return copy.deepcopy(
                [
                    c
                    for c in self.clients
                    if c["clientId"].startswith(query["clientId"][0])
                ]
            )
        if method == "GET" and path.endswith("/client-secret"):
            return {"value": self.secrets[path.split("/")[-2]]}
        assert method == "PUT" and not path.endswith("/client-secret")
        assert set(data) == {"secret"}
        self.writes.append((method, path, copy.deepcopy(data)))
        if not self.ignore_writes:
            self.secrets[path.rsplit("/", 1)[1]] = data["secret"]


DESIRED = [
    {"clientId": "mas-a2a", "secretEnv": "GATEWAY_SECRET"},
    {"clientId": "hub-a2a", "secretEnv": "ADAPTER_SECRET"},
]
ENVIRON = {
    "GATEWAY_SECRET": "test-only-gateway-value",
    "ADAPTER_SECRET": "test-only-adapter-value",
}


def test_repair_is_verified_idempotent_and_preserves_settings(reconcile, capsys):
    keycloak = FakeKeycloak()
    keycloak.clients.append({"id": "other-id", "clientId": "mas-a2a-other"})
    original = copy.deepcopy(keycloak.clients)
    reconcile(keycloak.call, "rca", DESIRED, ENVIRON)
    assert keycloak.secrets == {
        "gateway-id": ENVIRON["GATEWAY_SECRET"],
        "adapter-id": ENVIRON["ADAPTER_SECRET"],
    }
    assert keycloak.clients == original
    assert len(keycloak.writes) == 2
    reconcile(keycloak.call, "rca", DESIRED, ENVIRON)
    assert len(keycloak.writes) == 2
    rotated = {**ENVIRON, "ADAPTER_SECRET": "test-only-updated-value"}
    reconcile(keycloak.call, "rca", DESIRED, rotated)
    assert len(keycloak.writes) == 3
    assert keycloak.secrets["adapter-id"] == rotated["ADAPTER_SECRET"]
    output = capsys.readouterr().out
    assert "reconciled and verified" in output and "already matches" in output
    assert not any(
        value in output
        for value in [*ENVIRON.values(), rotated["ADAPTER_SECRET"], "old-value"]
    )


@pytest.mark.parametrize(
    "problem", ["missing-secret", "empty-secret", "missing-client", "duplicate-client"]
)
def test_invalid_configuration_fails_before_any_write(reconcile, problem):
    keycloak = FakeKeycloak()
    environ = dict(ENVIRON)
    if problem == "missing-secret":
        del environ["ADAPTER_SECRET"]
    elif problem == "empty-secret":
        environ["ADAPTER_SECRET"] = ""
    elif problem == "missing-client":
        keycloak.clients.pop()
    else:
        keycloak.clients.append(copy.deepcopy(keycloak.clients[-1]))
    with pytest.raises(ValueError):
        reconcile(keycloak.call, "rca", DESIRED, environ)
    assert not keycloak.writes


def test_failed_readback_does_not_log_credentials(reconcile, capsys):
    keycloak = FakeKeycloak()
    keycloak.ignore_writes = True
    with pytest.raises(ValueError, match="Credential verification failed") as error:
        reconcile(keycloak.call, "rca", DESIRED, ENVIRON)
    assert len(keycloak.writes) == 1
    output = capsys.readouterr().out + str(error.value)
    assert not any(value in output for value in ENVIRON.values())
