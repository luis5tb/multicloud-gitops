"""Regression tests for Keycloak external OIDC provider rendering."""

from __future__ import annotations

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
    helm_args = ["helm", "template", "keycloak-oidc", str(CHART), "-f", str(VARIANT)]

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
        },
    ]

    for index, provider in enumerate(external_providers):
        for key, value in provider.items():
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
