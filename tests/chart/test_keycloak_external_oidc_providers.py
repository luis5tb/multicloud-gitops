"""Regression tests for Keycloak external OIDC provider rendering."""

from __future__ import annotations

import json
import copy
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "charts/all/keycloak-oidc"
VARIANT = REPO_ROOT / "variants/standalone/values-standalone.yaml"


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_external_oidc_providers_for_token_exchange() -> None:
    """Verify external OIDC providers are configured for RFC 8693 token exchange."""
    helm_args = [
        "helm",
        "template",
        "keycloak-oidc",
        str(CHART),
        "-f",
        str(VARIANT),
        "--set",
        "keycloak.spiffeIdentityProvider.enabled=false",
    ]

    # External providers that should be configured for token exchange
    external_providers = [
        {
            "alias": "red-hat-sso-prod",
            "displayName": "Red Hat SSO Production",
            "issuerUrl": "https://sso.redhat.com/auth/realms/redhat-external",
            "jwksUrl": "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs",
            "tokenIntrospectionUrl": "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token/introspect",
            "userInfoUrl": "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/userinfo",
            "clientId": "red-hat-prod-client",
            "isAccessTokenJWT": "true",
            "allowTokenExchange": "true",
            "hardcodedGroups": ["acme-agent-rca"],
        },
        {
            "alias": "red-hat-sso-stage",
            "displayName": "Red Hat SSO Stage",
            "issuerUrl": "https://sso.stage.redhat.com/auth/realms/redhat-external",
            "jwksUrl": "https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs",
            "tokenIntrospectionUrl": "https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token/introspect",
            "userInfoUrl": "https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/userinfo",
            "clientId": "red-hat-stage-client",
            "isAccessTokenJWT": "true",
            "allowTokenExchange": "true",
            "hardcodedGroups": ["acme-agent-rca"],
        },
    ]

    for index, provider in enumerate(external_providers):
        for key, value in provider.items():
            if key == "hardcodedGroups":
                for group_index, group in enumerate(value):
                    helm_args.extend(
                        [
                            "--set",
                            f"keycloak.externalOidcProviders[{index}].{key}[{group_index}]={group}",
                        ]
                    )
            else:
                helm_args.extend(
                    ["--set", f"keycloak.externalOidcProviders[{index}].{key}={value}"]
                )

    rendered = subprocess.run(
        helm_args, check=True, capture_output=True, text=True
    ).stdout

    # Find the KeycloakRealmImport resource
    realm_import = next(
        resource
        for resource in yaml.safe_load_all(rendered)
        if resource and resource.get("kind") == "KeycloakRealmImport"
    )

    identity_providers = realm_import["spec"]["realm"]["identityProviders"]

    # Extract OIDC providers (not SPIFFE)
    oidc_providers = [
        provider
        for provider in identity_providers
        if provider.get("providerId") == "oidc"
    ]

    assert len(oidc_providers) == 2, (
        f"Expected 2 external OIDC providers, got {len(oidc_providers)}"
    )

    # Verify Red Hat SSO Production
    sso_prod = next(p for p in oidc_providers if p["alias"] == "red-hat-sso-prod")
    assert sso_prod["enabled"] is True
    assert sso_prod["displayName"] == "Red Hat SSO Production"
    assert (
        sso_prod["config"]["issuer"]
        == "https://sso.redhat.com/auth/realms/redhat-external"
    )
    assert (
        sso_prod["config"]["jwksUrl"]
        == "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs"
    )
    assert sso_prod["config"]["validateSignature"] == "true"
    assert sso_prod["config"]["useJwksUrl"] == "true"
    assert (
        sso_prod["config"]["tokenIntrospectionUrl"]
        == "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token/introspect"
    )
    assert (
        sso_prod["config"]["userInfoUrl"]
        == "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/userinfo"
    )
    assert sso_prod["config"]["clientId"] == "red-hat-prod-client"
    assert sso_prod["config"]["isAccessTokenJWT"] == "true"
    assert sso_prod["config"]["tokenExchangeEnabled"] == "true", (
        "allowTokenExchange must be true for OLS to exchange Red Hat SSO tokens"
    )

    # Verify Red Hat SSO Stage
    sso_stage = next(p for p in oidc_providers if p["alias"] == "red-hat-sso-stage")
    assert sso_stage["enabled"] is True
    assert sso_stage["displayName"] == "Red Hat SSO Stage"
    assert (
        sso_stage["config"]["issuer"]
        == "https://sso.stage.redhat.com/auth/realms/redhat-external"
    )
    assert (
        sso_stage["config"]["jwksUrl"]
        == "https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs"
    )
    assert sso_stage["config"]["validateSignature"] == "true"
    assert sso_stage["config"]["useJwksUrl"] == "true"
    assert (
        sso_stage["config"]["tokenIntrospectionUrl"]
        == "https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token/introspect"
    )
    assert (
        sso_stage["config"]["userInfoUrl"]
        == "https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/userinfo"
    )
    assert sso_stage["config"]["clientId"] == "red-hat-stage-client"
    assert sso_stage["config"]["isAccessTokenJWT"] == "true"
    assert sso_stage["config"]["tokenExchangeEnabled"] == "true"

    mappers = realm_import["spec"]["realm"]["identityProviderMappers"]
    assert mappers == [
        {
            "name": "acme-agent-rca-red-hat-sso-prod",
            "identityProviderAlias": "red-hat-sso-prod",
            "identityProviderMapper": "oidc-hardcoded-group-idp-mapper",
            "config": {"group": "/acme-agent-rca", "syncMode": "FORCE"},
        },
        {
            "name": "acme-agent-rca-red-hat-sso-stage",
            "identityProviderAlias": "red-hat-sso-stage",
            "identityProviderMapper": "oidc-hardcoded-group-idp-mapper",
            "config": {"group": "/acme-agent-rca", "syncMode": "FORCE"},
        },
    ]


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_external_oidc_group_mappings_render_reconciler() -> None:
    """Existing realms receive IdP group mappers through the PostSync hook."""
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "keycloak-oidc",
            str(CHART),
            "-f",
            str(VARIANT),
            "--set",
            "keycloak.spiffeIdentityProvider.enabled=false",
            "--set",
            "keycloak.externalOidcProviders[0].alias=legacy-alias",
            "--set",
            "keycloak.externalOidcProviders[0].issuerUrl=https://sso.example.com/realms/example",
            "--set",
            "keycloak.externalOidcProviders[0].hardcodedGroups[0]=acme-agent-rca",
            "--set",
            "keycloak.externalOidcProviders[0].jwksUrl=https://sso.example.com/realms/example/protocol/openid-connect/certs",
            "--set",
            "keycloak.consoleClientSecretVaultKey=",
            "--set",
            "keycloak.consoleAdminUser.enabled=false",
            "--set",
            "openshiftOIDC.enabled=false",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    job = next(
        resource
        for resource in yaml.safe_load_all(rendered)
        if resource and resource.get("kind") == "Job"
    )
    env = {
        variable["name"]: variable["value"]
        for variable in job["spec"]["template"]["spec"]["containers"][0]["env"]
        if "value" in variable
    }

    providers = json.loads(env["EXTERNAL_OIDC_PROVIDERS"])
    assert len(providers) == 1
    assert providers[0]["alias"] == "legacy-alias"
    assert providers[0]["issuerUrl"] == "https://sso.example.com/realms/example"
    assert providers[0]["groups"] == ["acme-agent-rca"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required")
def test_standalone_external_oidc_credentials_and_endpoints() -> None:
    """Real Application overrides wire both imports and existing-realm repair."""
    values = yaml.safe_load(VARIANT.read_text())
    overrides = values["clusterGroup"]["applications"]["keycloak-oidc"]["overrides"]
    command = ["helm", "template", "keycloak-oidc", str(CHART), "-f", str(VARIANT)]
    for override in overrides:
        command.extend(["--set-string", f"{override['name']}={override['value']}"])
    rendered = subprocess.run(
        command, check=True, capture_output=True, text=True
    ).stdout
    resources = [r for r in yaml.safe_load_all(rendered) if r]
    realm = next(r for r in resources if r["kind"] == "KeycloakRealmImport")["spec"]
    providers = [
        p for p in realm["realm"]["identityProviders"] if p["providerId"] == "oidc"
    ]
    job = next(
        r
        for r in resources
        if r["kind"] == "Job"
        and r["metadata"]["name"] == "rca-realm-secrets-reconciler"
    )
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    desired = json.loads(env["EXTERNAL_OIDC_PROVIDERS"]["value"])
    for index, (environment, host) in enumerate(
        [("production", "sso.redhat.com"), ("stage", "sso.stage.redhat.com")]
    ):
        config = providers[index]["config"]
        base = f"https://{host}/auth/realms/redhat-external/protocol/openid-connect"
        assert config["authorizationUrl"] == base + "/auth"
        assert config["tokenUrl"] == base + "/token"
        assert config["clientAuthMethod"] == "client_secret_post"
        assert config["defaultScope"] == "openid api.ask_red_hat"
        for field, suffix, key in [
            ("clientId", "CLIENT_ID", "client-id"),
            ("clientSecret", "CLIENT_SECRET", "client-secret"),
        ]:
            var = f"EXTERNAL_IDP_{index}_{suffix}"
            assert config[field] == f"$({var})"
            reference = realm["placeholders"][var]["secret"]
            assert reference == env[var]["valueFrom"]["secretKeyRef"]
            assert reference["key"] == key
            external_secret = next(
                r
                for r in resources
                if r["kind"] == "ExternalSecret"
                and r["metadata"]["name"] == reference["name"]
            )
            assert external_secret["metadata"]["namespace"] == "keycloak-system"
            remote = next(
                d["remoteRef"]
                for d in external_secret["spec"]["data"]
                if d["secretKey"] == key
            )
            assert remote == {
                "key": f"secret/data/global/acme-agent-redhat-sso-{environment}",
                "property": key,
            }
        assert "clientSecret" not in desired[index]["config"]
        assert (
            desired[index]["config"]["authorizationUrl"] == config["authorizationUrl"]
        )
    scripts = next(
        r
        for r in resources
        if r["kind"] == "ConfigMap"
        and r["metadata"]["name"].endswith("-external-oidc-reconciler")
    )
    compile(
        scripts["data"]["reconcile_external_oidc.py"],
        "reconcile_external_oidc.py",
        "exec",
    )
    compile(container["args"][0], "realm-reconciler.py", "exec")
    assert any(
        v["name"] == "external-oidc-reconciler" for v in container["volumeMounts"]
    )


def load_reconciler():
    spec = importlib.util.spec_from_file_location(
        "external_oidc", CHART / "files/reconcile_external_oidc.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.reconcile


class FakeKeycloak:
    def __init__(self, providers):
        self.providers = copy.deepcopy(providers)
        self.mappers = []
        self.writes = []

    def call(self, method, path, data=None):
        if method == "GET":
            return copy.deepcopy(
                self.mappers if path.endswith("/mappers") else self.providers
            )
        self.writes.append((method, path, copy.deepcopy(data)))
        if path.endswith("/mappers"):
            self.mappers.append({**copy.deepcopy(data), "id": "mapper-id"})
        elif "/mappers/" in path:
            self.mappers = [copy.deepcopy(data)]
        elif method == "POST":
            self.providers.append(copy.deepcopy(data))
        else:
            self.providers = [copy.deepcopy(data)]


@pytest.mark.parametrize("existing", [False, True])
def test_reconcile_repairs_credentials_preserves_alias_and_rotates(existing, capsys):
    issuer = "https://sso.example.test/realm"
    original = {
        "alias": "legacy-alias",
        "providerId": "oidc",
        "enabled": True,
        "firstBrokerLoginFlowAlias": "custom-flow",
        "config": {"issuer": issuer, "customSetting": "preserve-me"},
    }
    keycloak = FakeKeycloak([original] if existing else [])
    desired = [
        {
            "alias": "configured-alias",
            "issuerUrl": issuer,
            "groups": ["acme-agent-rca"],
            "config": {
                "issuer": issuer,
                "authorizationUrl": issuer + "/auth",
                "tokenUrl": issuer + "/token",
            },
            "clientIdEnv": "IDP_CLIENT",
            "clientSecretEnv": "IDP_SECRET",
        }
    ]
    credentials = {"IDP_CLIENT": "test-client", "IDP_SECRET": "test-credential-v1"}
    reconcile = load_reconciler()
    reconcile(keycloak.call, "rca", desired, credentials)
    provider = keycloak.providers[0]
    assert provider["alias"] == ("legacy-alias" if existing else "configured-alias")
    assert provider["config"]["clientSecret"] == credentials["IDP_SECRET"]
    assert provider["config"]["clientId"] == credentials["IDP_CLIENT"]
    assert provider["config"]["authorizationUrl"] == issuer + "/auth"
    assert provider["config"]["tokenUrl"] == issuer + "/token"
    if existing:
        assert provider["config"]["customSetting"] == "preserve-me"
        assert provider["firstBrokerLoginFlowAlias"] == "custom-flow"
    assert keycloak.mappers[0]["identityProviderAlias"] == provider["alias"]
    writes = len(keycloak.writes)
    reconcile(keycloak.call, "rca", desired, credentials)
    assert len(keycloak.writes) == writes
    credentials["IDP_SECRET"] = "test-credential-v2"
    reconcile(keycloak.call, "rca", desired, credentials)
    assert keycloak.providers[0]["config"]["clientSecret"] == "test-credential-v2"
    assert len(keycloak.writes) == writes + 1
    output = capsys.readouterr().out
    assert "test-credential" not in output
    assert "clientSecret" not in desired[0]["config"]


def test_missing_credentials_fail_before_any_keycloak_calls():
    calls = []
    with pytest.raises(ValueError, match="Missing credential IDP_SECRET"):
        load_reconciler()(
            lambda *args: calls.append(args),
            "rca",
            [
                {
                    "alias": "stage",
                    "config": {},
                    "clientSecretEnv": "IDP_SECRET",
                }
            ],
            {},
        )
    assert calls == []


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is required")
def test_existing_secret_and_disabled_exchange(tmp_path):
    base = "https://sso.example.test/realm"
    provider = {
        "alias": "stage",
        "issuerUrl": base,
        "authorizationUrl": base + "/auth",
        "tokenUrl": base + "/token",
        "jwksUrl": base + "/certs",
        "userInfoUrl": base + "/userinfo",
        "tokenIntrospectionUrl": base + "/introspect",
        "credentials": {"existingSecretName": "existing-stage-credentials"},
        "allowTokenExchange": False,
    }
    values = {
        "global": {"clusterBaseDomain": "example.test"},
        "keycloak": {
            "spiffeIdentityProvider": {"enabled": False},
            "externalOidcProviders": [provider],
        },
    }
    path = tmp_path / "values.yaml"

    def render():
        path.write_text(yaml.safe_dump(values))
        return subprocess.run(
            ["helm", "template", "test", str(CHART), "-f", str(path)],
            capture_output=True,
            text=True,
        )

    result = render()
    assert result.returncode == 0, result.stderr
    resources = [r for r in yaml.safe_load_all(result.stdout) if r]
    assert not any(
        r["kind"] == "ExternalSecret" and "idp-stage" in r["metadata"]["name"]
        for r in resources
    )
    realm = next(r for r in resources if r["kind"] == "KeycloakRealmImport")["spec"]
    assert (
        realm["placeholders"]["EXTERNAL_IDP_0_CLIENT_SECRET"]["secret"]["name"]
        == "existing-stage-credentials"
    )
    assert (
        realm["realm"]["identityProviders"][0]["config"]["tokenExchangeEnabled"]
        == "false"
    )
    provider["credentials"]["vaultKey"] = "secret/data/global/stage"
    result = render()
    assert result.returncode != 0
    assert "set only one of existingSecretName and vaultKey" in result.stderr
    del provider["credentials"]["vaultKey"]
    del provider["authorizationUrl"]
    result = render()
    assert result.returncode != 0
    assert "requires authorizationUrl" in result.stderr


def test_group_only_provider_does_not_clear_existing_credentials():
    keycloak = FakeKeycloak(
        [
            {
                "alias": "legacy",
                "providerId": "oidc",
                "enabled": True,
                "config": {
                    "issuer": "https://sso.example.test",
                    "clientSecret": "existing-credential",
                },
            }
        ]
    )
    load_reconciler()(
        keycloak.call,
        "rca",
        [
            {
                "alias": "stage",
                "issuerUrl": "https://sso.example.test",
                "config": {"issuer": "https://sso.example.test"},
                "groups": [],
            }
        ],
        {},
    )
    assert keycloak.writes == []
    assert keycloak.providers[0]["config"]["clientSecret"] == "existing-credential"
