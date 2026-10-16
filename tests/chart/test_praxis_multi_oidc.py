"""Regression tests for Praxis multi-issuer policy rendering."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "charts/all/praxis-proxy"
VARIANT = REPO_ROOT / "variants/standalone/values-standalone.yaml"


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_multi_oidc_policy_embeds_valid_yaml() -> None:
    helm_args = ["helm", "template", "praxis-proxy", str(CHART), "-f", str(VARIANT)]
    issuers = [
        {
            "name": "red-hat-sso-prod",
            "issuerUrl": "https://sso.redhat.com/auth/realms/redhat-external",
            "jwksUrl": "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs",
            "audience": "openshift-lightspeed",
        },
        {
            "name": "red-hat-sso-stage",
            "issuerUrl": "https://sso.stage.redhat.com/auth/realms/redhat-external",
            "jwksUrl": "https://sso.stage.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs",
            "audience": "openshift-lightspeed",
        },
    ]
    for index, issuer in enumerate(issuers):
        for key, value in issuer.items():
            helm_args.extend(
                ["--set", f"identity.oidcIssuers[{index}].{key}={value}"]
            )

    rendered = subprocess.run(
        helm_args, check=True, capture_output=True, text=True
    ).stdout
    configmap = next(
        resource
        for resource in yaml.safe_load_all(rendered)
        if resource and resource.get("kind") == "ConfigMap"
    )
    policy = yaml.safe_load(configmap["data"]["policy.yaml"])
    trusted_issuers = policy["plugins"][0]["config"]["trusted_issuers"]
    variant = yaml.safe_load(VARIANT.read_text())
    local_issuer = (
        f"https://keycloak.apps.{variant['global']['clusterBaseDomain']}"
        f"/realms/{variant['global']['keycloakRealm']}"
    )

    assert [entry["issuer"] for entry in trusted_issuers] == [
        issuers[0]["issuerUrl"],
        issuers[1]["issuerUrl"],
        local_issuer,
    ]
