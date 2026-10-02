"""Helm chart gates for greenfield OpenShift Lightspeed configuration."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
HELM = shutil.which("helm")
CHART = ROOT / "charts/all/openshift-lightspeed-config"
pytestmark = pytest.mark.skipif(HELM is None, reason="Helm is required to render chart tests")


def _render(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [HELM, "template", "openshift-lightspeed-config", str(CHART), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def test_chart_defaults_to_no_resources():
    rendered = _render("--namespace", "openshift-lightspeed")
    assert [doc for doc in yaml.safe_load_all(rendered.stdout) if doc] == []


def test_enabled_chart_waits_for_crd_before_applying_olsconfig():
    rendered = _render(
        "--namespace",
        "openshift-lightspeed",
        "--set",
        "olsConfig.enabled=true",
        "--set",
        "crdWait.cliImage=registry.example.test/openshift-cli@sha256:"
        + "a" * 64,
    )
    docs = [doc for doc in yaml.safe_load_all(rendered.stdout) if doc]

    job = next(doc for doc in docs if doc["kind"] == "Job")
    crd = next(doc for doc in docs if doc["kind"] == "ClusterRole")
    binding = next(doc for doc in docs if doc["kind"] == "ClusterRoleBinding")
    ols_config = next(doc for doc in docs if doc["kind"] == "OLSConfig")

    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "Sync"
    assert job["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-2"
    assert job["spec"]["template"]["spec"]["containers"][0]["image"].endswith(
        "@sha256:" + "a" * 64
    )
    assert crd["rules"] == [
        {
            "apiGroups": ["apiextensions.k8s.io"],
            "resources": ["customresourcedefinitions"],
            "resourceNames": ["olsconfigs.ols.openshift.io"],
            "verbs": ["get", "watch"],
        }
    ]
    assert binding["subjects"][0]["kind"] == "ServiceAccount"
    assert ols_config["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "1"


def test_enabled_chart_requires_digest_pinned_oc_image():
    result = _render("--set", "olsConfig.enabled=true", check=False)
    assert result.returncode != 0
    assert "crdWait.cliImage must be a deployment-supplied immutable" in result.stderr
