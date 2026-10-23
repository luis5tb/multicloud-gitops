"""Regression tests for ACME Red Hat SSO agent-token rendering."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "charts/all/acme-agent"


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_redhat_sso_agent_credentials_render() -> None:
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "acme-agent",
            str(CHART),
            "--set",
            "auth.mode=keycloak",
            "--set",
            "auth.keycloak.tokenUrl=https://keycloak.example.test/realms/rca/protocol/openid-connect/token",
            "--set",
            "a2a.remoteAgents[0].name=test_agent",
            "--set",
            "a2a.remoteAgents[0].description=Test downstream agent",
            "--set",
            "a2a.remoteAgents[0].endpoint=https://downstream.example.test",
            "--set",
            "global.olsClusters.downstream-example-test.apiURL=https://downstream.example.test",
            "--set",
            "auth.redhatSso.enabled=true",
            "--set",
            "auth.redhatSso.production.tokenUrl=https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token",
            "--set",
            "auth.redhatSso.stage.tokenUrl=https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token",
            "--set",
            "auth.tokenDebug.enabled=true",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    resources = [
        resource for resource in yaml.safe_load_all(rendered) if resource is not None
    ]
    deployment = next(
        resource
        for resource in resources
        if resource.get("kind") == "Deployment"
        and resource["metadata"]["name"] == "acme-agent"
    )
    env = {
        item["name"]: item
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["A2A_AUTH_PROVIDER"]["value"] == "keycloak"
    assert env["AUTH_TOKEN_DEBUG_ENABLED"]["value"] == "true"
    assert env["REDHAT_SSO_SCOPE"]["value"] == "api.ask_red_hat"
    assert (
        env["REDHAT_SSO_PRODUCTION_TOKEN_URL"]["value"]
        == "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
    )
    assert (
        env["REDHAT_SSO_PRODUCTION_CLIENT_ID"]["valueFrom"]["secretKeyRef"]["key"]
        == "production-client-id"
    )
    assert (
        env["REDHAT_SSO_STAGE_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"]["key"]
        == "stage-client-secret"
    )

    external_secret = next(
        resource
        for resource in resources
        if resource.get("kind") == "ExternalSecret"
        and resource["metadata"]["name"] == "acme-agent-redhat-sso"
    )
    credentials = {
        item["secretKey"]: item["remoteRef"] for item in external_secret["spec"]["data"]
    }
    assert credentials["production_client_id"] == {
        "key": "secret/data/global/acme-agent-redhat-sso-production",
        "property": "client-id",
    }
    assert credentials["stage_client_secret"] == {
        "key": "secret/data/global/acme-agent-redhat-sso-stage",
        "property": "client-secret",
    }
