"""mas-a2a / mas-worker / mas-api identity.serviceCaTrust: also trust the
OpenShift service CA (for the internal praxis-proxy egress gateways). Off by
default, in which case the render must not change at all. mas-a2a is included
because it builds the plan and fetches each A2A node's card itself (found live
2026-11-27: the first Ask Red Hat request failed there)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHARTS = ["mas-a2a", "mas-worker", "mas-api"]
COMMON = [
    "--set", "runCredentials.secretName=run-creds",
    "--set", "identity.keycloakCaTrust.enabled=true",
]
BASE = {
    "mas-worker": [*COMMON, "--set", "identity.providerMode=dev"],
    "mas-api": [*COMMON, "--set", "identity.providerMode=dev"],
    # mas-a2a renders nothing unless enabled, and only builds the CA bundle in
    # keycloak mode.
    "mas-a2a": [
        *COMMON,
        "--set", "identity.providerMode=keycloak",
        "--set", "identity.keycloakIssuerUri=https://kc.example/realms/r",
        "--set", "identity.keycloakAudience=mas-a2a",
        "--set", "a2a.enabled=true",
        "--set", "a2a.blueprintId=b",
        "--set", "a2a.publicBaseUrl=https://mas.example",
        "--set", "a2a.allowedPrincipals[0]=p",
        "--set", "image.repository=quay.io/example/mas",
        "--set", "image.tag=v1",
    ],
}

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)


def _render(chart: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", chart, str(REPO_ROOT / "charts/all/mao" / chart), *BASE[chart], *args],
        check=check, capture_output=True, text=True,
    )


def _pod(chart: str, *args: str) -> dict:
    docs = [d for d in yaml.safe_load_all(_render(chart, *args).stdout) if d]
    return next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]


@pytest.mark.parametrize("chart", CHARTS)
def test_disabled_by_default(chart: str) -> None:
    pod = _pod(chart)
    assert "service-ca" not in {v["name"] for v in pod["volumes"]}
    assert "mas-service-ca" not in pod["initContainers"][0]["args"][0]


@pytest.mark.parametrize("chart", CHARTS)
def test_enabled_mounts_and_appends_service_ca(chart: str) -> None:
    pod = _pod(chart, "--set", "identity.serviceCaTrust.enabled=true")
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["service-ca"]["configMap"] == {
        "name": "openshift-service-ca.crt",
        "items": [{"key": "service-ca.crt", "path": "service-ca.crt"}],
    }
    init = pod["initContainers"][0]
    assert {"name": "service-ca", "mountPath": "/var/run/mas-service-ca", "readOnly": True} in init["volumeMounts"]
    script = init["args"][0]
    assert 'service_ca = Path("/var/run/mas-service-ca/service-ca.crt")' in script
    assert "context.load_verify_locations(cafile=str(service_ca))" in script
    # Loaded after the context exists and before the trust-store sanity check.
    assert script.index("cafile=str(private_ca)") < script.index("cafile=str(service_ca)") < script.index("cert_store_stats")
    compile(script, f"{chart}-init", "exec")


@pytest.mark.parametrize("chart", CHARTS)
def test_requires_keycloak_ca_trust(chart: str) -> None:
    result = _render(
        chart,
        "--set", "identity.keycloakCaTrust.enabled=false",
        "--set", "identity.serviceCaTrust.enabled=true",
        check=False,
    )
    assert result.returncode != 0
    assert "identity.serviceCaTrust.enabled requires identity.keycloakCaTrust.enabled" in result.stderr
