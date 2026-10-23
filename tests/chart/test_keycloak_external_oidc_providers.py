"""Regression tests for Keycloak external OIDC provider rendering."""

from __future__ import annotations

import json
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
        provider for provider in identity_providers
        if provider.get("providerId") == "oidc"
    ]

    assert len(oidc_providers) == 2, (
        f"Expected 2 external OIDC providers, got {len(oidc_providers)}"
    )

    # Verify Red Hat SSO Production
    sso_prod = next(
        p for p in oidc_providers if p["alias"] == "red-hat-sso-prod"
    )
    assert sso_prod["enabled"] is True
    assert sso_prod["displayName"] == "Red Hat SSO Production"
    assert sso_prod["config"]["issuer"] == "https://sso.redhat.com/auth/realms/redhat-external"
    assert sso_prod["config"]["jwksUrl"] == "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs"
    assert sso_prod["config"]["validateSignature"] == "true"
    assert sso_prod["config"]["useJwksUrl"] == "true"
    assert sso_prod["config"]["tokenIntrospectionUrl"] == "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token/introspect"
    assert sso_prod["config"]["userInfoUrl"] == "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/userinfo"
    assert sso_prod["config"]["clientId"] == "red-hat-prod-client"
    assert sso_prod["config"]["isAccessTokenJWT"] == "true"
    assert sso_prod["config"]["tokenExchangeEnabled"] == "true", (
        "allowTokenExchange must be true for OLS to exchange Red Hat SSO tokens"
    )

    # Verify Red Hat SSO Stage
    sso_stage = next(
        p for p in oidc_providers if p["alias"] == "red-hat-sso-stage"
    )
    assert sso_stage["enabled"] is True
    assert sso_stage["displayName"] == "Red Hat SSO Stage"
    assert sso_stage["config"]["issuer"] == "https://sso.stage.redhat.com/auth/realms/redhat-external"
    assert sso_stage["config"]["jwksUrl"] == "https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs"
    assert sso_stage["config"]["validateSignature"] == "true"
    assert sso_stage["config"]["useJwksUrl"] == "true"
    assert sso_stage["config"]["tokenIntrospectionUrl"] == "https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token/introspect"
    assert sso_stage["config"]["userInfoUrl"] == "https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/userinfo"
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

    assert json.loads(env["EXTERNAL_IDP_GROUP_MAPPINGS"]) == [
        {
            "alias": "legacy-alias",
            "issuerUrl": "https://sso.example.com/realms/example",
            "groups": ["acme-agent-rca"],
        }
    ]
