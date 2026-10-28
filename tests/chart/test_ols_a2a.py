"""Tests for the OLS chart's a2a-split bridge configuration."""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "charts/all/openshift-lightspeed-config"
PATCHER_PATH = CHART / "files/patch_appserver.py"
CA_SYNC_PATH = CHART / "files/sync_keycloak_ca.py"

_PATCHER_SPEC = importlib.util.spec_from_file_location(
    "ols_appserver_patcher", PATCHER_PATH
)
assert _PATCHER_SPEC and _PATCHER_SPEC.loader
PATCHER = importlib.util.module_from_spec(_PATCHER_SPEC)
_PATCHER_SPEC.loader.exec_module(PATCHER)

_CA_SYNC_SPEC = importlib.util.spec_from_file_location(
    "ols_keycloak_ca_sync", CA_SYNC_PATH
)
assert _CA_SYNC_SPEC and _CA_SYNC_SPEC.loader
CA_SYNC = importlib.util.module_from_spec(_CA_SYNC_SPEC)
_CA_SYNC_SPEC.loader.exec_module(CA_SYNC)


def test_config_editor_changes_only_exchange_setting_and_preserves_oidc() -> None:
    source = (
        "ols_config:\n"
        "  authentication_config:\n"
        "    module: oidc\n"
        "    oidc_issuer_url: https://identity.example/realm\n"
        "a2a:\n"
        "  enabled: true\n"
        "  token_exchange_enabled: false # set by operator\n"
        "llm_providers: []\n"
    )

    changed = PATCHER._set_a2a_token_exchange_enabled(source, True)

    assert "    module: oidc\n" in changed
    assert "    oidc_issuer_url: https://identity.example/realm\n" in changed
    assert "  enabled: true\n" in changed
    assert "  token_exchange_enabled: true # set by operator\n" in changed
    assert changed.count("token_exchange_enabled:") == 1


def test_config_editor_adds_exchange_setting_without_a2a_section() -> None:
    assert PATCHER._set_a2a_token_exchange_enabled("llm_providers: []\n", True) == (
        "llm_providers: []\n\na2a:\n  token_exchange_enabled: true\n"
    )


def test_config_editor_rejects_ambiguous_yaml() -> None:
    with pytest.raises(ValueError, match="block-style"):
        PATCHER._set_a2a_token_exchange_enabled("a2a: {enabled: true}\n", True)

    with pytest.raises(ValueError, match="one block-style"):
        PATCHER._set_a2a_token_exchange_enabled("a2a:\n  enabled: true\na2a:\n", True)

    with pytest.raises(ValueError, match="single YAML document"):
        PATCHER._set_a2a_token_exchange_enabled("ols_config: {}\n---\na2a: {}\n", True)


def test_oidc_editor_switches_from_k8s_and_preserves_other_auth_settings() -> None:
    source = (
        "ols_config:\n"
        "  authentication_config:\n"
        "    module: k8s\n"
        "    k8s_cluster_api: https://api.example.test:6443\n"
        "    oidc_username_claim: preferred_username\n"
        "  default_provider: vertex-google\n"
        "llm_providers: []\n"
    )

    changed = PATCHER._set_oidc_authentication(
        source,
        "https://keycloak.example.test/realms/rca",
        "lightspeed-a2a",
    )

    assert '    module: "oidc"\n' in changed
    assert (
        '    oidc_issuer_url: "https://keycloak.example.test/realms/rca"\n' in changed
    )
    assert '    oidc_audience: "lightspeed-a2a"\n' in changed
    assert "k8s_cluster_api:" not in changed
    assert "k8s_ca_cert_path:" not in changed
    assert "skip_tls_verification:" not in changed
    assert "    oidc_username_claim: preferred_username\n" in changed
    assert "  default_provider: vertex-google\n" in changed


def test_oidc_editor_adds_missing_authentication_block() -> None:
    changed = PATCHER._set_oidc_authentication(
        "ols_config:\n  default_provider: vertex-google\nllm_providers: []\n",
        "https://keycloak.example.test/realms/rca",
        "lightspeed-a2a",
    )

    assert '  authentication_config:\n    module: "oidc"\n' in changed
    assert (
        '    oidc_issuer_url: "https://keycloak.example.test/realms/rca"\n' in changed
    )
    assert '    oidc_audience: "lightspeed-a2a"\n' in changed
    assert changed.index("authentication_config:") < changed.index("llm_providers:")


def test_deployment_patch_deletes_old_inbound_environment_variables() -> None:
    patch = PATCHER._container_env_patch(
        [{"name": "A2A_ENABLED", "value": "true"}], token_exchange_enabled=False
    )

    assert patch[0] == {"name": "A2A_ENABLED", "value": "true"}
    tombstones = [
        {"name": "A2A_INBOUND_AUDIENCE", "$patch": "delete"},
        {"name": "A2A_INBOUND_AZP", "$patch": "delete"},
    ]
    assert patch[1 : 1 + len(tombstones)] == tombstones
    assert {entry["name"] for entry in patch if entry.get("$patch") == "delete"} >= {
        "A2A_KEYCLOAK_ISSUER_URL",
        "A2A_EXCHANGE_AUDIENCE",
        "A2A_EXCHANGE_CLIENT_ASSERTION_TYPE",
        "A2A_SPIFFE_ENDPOINT_SOCKET",
    }
    enabled_patch = PATCHER._container_env_patch(
        [{"name": "A2A_EXCHANGE_AUDIENCE", "value": "openshift-mcp"}],
        token_exchange_enabled=True,
    )
    assert not any(
        entry.get("name") == "A2A_EXCHANGE_AUDIENCE" and entry.get("$patch") == "delete"
        for entry in enabled_patch
    )


def test_true_to_false_exchange_deletes_spiiffe_volume_and_mount() -> None:
    mounts, volumes = PATCHER._spiffe_volume_patches(
        "",
        [
            {
                "name": "spiffe-workload-api",
                "mountPath": "/spiffe-workload-api",
                "readOnly": True,
            }
        ],
    )

    assert mounts == [
        {
            "name": "spiffe-workload-api",
            "mountPath": "/spiffe-workload-api",
            "$patch": "delete",
        }
    ]
    assert volumes == [{"name": "spiffe-workload-api", "$patch": "delete"}]


@pytest.mark.skipif(
    shutil.which("kubectl") is None,
    reason="kubectl is required for strategic merge semantics",
)
def test_true_to_false_exchange_removes_mounts_with_kubectl_strategic_merge(
    tmp_path: Path,
) -> None:
    current = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "lightspeed-app-server"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "lightspeed-service-api",
                            "volumeMounts": [
                                {
                                    "name": "spiffe-workload-api",
                                    "mountPath": "/spiffe-workload-api",
                                    "readOnly": True,
                                },
                                {
                                    "name": "ols-keycloak-ingress-ca",
                                    "mountPath": "/etc/certs/old-ca-path",
                                    "readOnly": True,
                                },
                                {"name": "service-config", "mountPath": "/etc/service"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "spiffe-workload-api",
                            "csi": {"driver": "csi.spiffe.io"},
                        },
                        {
                            "name": "ols-keycloak-ingress-ca",
                            "secret": {"secretName": "old-ca"},
                        },
                        {"name": "service-config", "configMap": {"name": "service-config"}},
                    ],
                }
            }
        },
    }
    current_mounts = PATCHER._container_volume_mounts(
        current, "lightspeed-service-api"
    )
    spiffe_mounts, spiffe_volumes = PATCHER._spiffe_volume_patches("", current_mounts)
    ca_mounts, ca_volumes = PATCHER._oidc_ca_volume_patches(
        enabled=False,
        secret_name="old-ca",
        secret_key="ca.crt",
        secret_mount_path="/etc/certs/unused",
        existing_mounts=current_mounts,
    )
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "lightspeed-service-api",
                            "volumeMounts": spiffe_mounts + ca_mounts,
                        }
                    ],
                    "volumes": spiffe_volumes + ca_volumes,
                }
            }
        }
    }
    manifest = tmp_path / "deployment.json"
    manifest.write_text(json.dumps(current), encoding="utf-8")
    result = subprocess.run(
        [
            "kubectl",
            "patch",
            "--local",
            "-f",
            str(manifest),
            "--type=strategic",
            "--patch",
            json.dumps(patch),
            "--output=json",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    patched = json.loads(result.stdout)
    patched_spec = patched["spec"]["template"]["spec"]
    mounts = patched_spec["containers"][0]["volumeMounts"]
    assert mounts == [{"name": "service-config", "mountPath": "/etc/service"}]
    assert patched_spec["volumes"] == [
        {"name": "service-config", "configMap": {"name": "service-config"}}
    ]


def test_oidc_trust_volumes_and_ssl_cert_file_are_reversible() -> None:
    mounts, volumes = PATCHER._oidc_ca_volume_patches(
        enabled=True,
        secret_name="ols-keycloak-ca",
        secret_key="ca.crt",
        secret_mount_path="/etc/certs/ols-keycloak-ingress",
    )
    assert mounts[0]["readOnly"] is True
    assert volumes[0]["secret"]["secretName"] == "ols-keycloak-ca"
    assert volumes[0]["secret"]["items"] == [
        {"key": "ca.crt", "path": "ingress-ca.crt"}
    ]
    assert len(volumes) == 1

    mounts, volumes = PATCHER._oidc_ca_volume_patches(
        enabled=False,
        secret_name="ols-keycloak-ca",
        secret_key="ca.crt",
        secret_mount_path="/etc/certs/ols-keycloak-ingress",
        existing_mounts=[
            {
                "name": "ols-keycloak-ingress-ca",
                "mountPath": "/etc/certs/ols-keycloak-ingress",
            }
        ],
    )
    assert mounts == [
        {
            "name": "ols-keycloak-ingress-ca",
            "mountPath": "/etc/certs/ols-keycloak-ingress",
            "$patch": "delete",
        }
    ]
    assert volumes == [{"name": "ols-keycloak-ingress-ca", "$patch": "delete"}]

    env, annotations = PATCHER._ssl_cert_file_transition(
        [{"name": "A2A_ENABLED", "value": "true"}],
        oidc_trust_enabled=False,
        original_ssl_cert_file="/etc/certs/operator-bundle/ca-bundle.crt",
        has_saved_original=True,
    )
    assert {
        "name": "SSL_CERT_FILE",
        "value": "/etc/certs/operator-bundle/ca-bundle.crt",
    } in env
    assert annotations == {PATCHER._OIDC_TRUST_ORIGINAL_SSL_ANNOTATION: None}

    enabled_env, enabled_annotations = PATCHER._ssl_cert_file_transition(
        [{"name": "A2A_ENABLED", "value": "true"}],
        oidc_trust_enabled=True,
        original_ssl_cert_file="/etc/certs/operator-bundle/ca-bundle.crt",
        has_saved_original=False,
    )
    assert {
        "name": "SSL_CERT_FILE",
        "value": "/etc/certs/operator-bundle/ca-bundle.crt",
    } in enabled_env
    assert enabled_annotations == {
        PATCHER._OIDC_TRUST_ORIGINAL_SSL_ANNOTATION: "/etc/certs/operator-bundle/ca-bundle.crt"
    }


def test_ca_rotation_rollout_uses_bundle_hash_and_is_idempotent() -> None:
    deployment = {
        "spec": {"template": {"metadata": {"annotations": {}}}}
    }
    calls: list[tuple[str, str, dict | None, str | None]] = []

    def fake_request(method, path, body=None, content_type=None):
        calls.append((method, path, body, content_type))
        if method == "GET":
            return 200, deployment
        assert method == "PATCH"
        assert content_type == "application/strategic-merge-patch+json"
        deployment["spec"]["template"]["metadata"]["annotations"].update(
            body["spec"]["template"]["metadata"]["annotations"]
        )
        return 200, {}

    options = {
        "enabled": True,
        "namespace": "ols",
        "deployment_name": "lightspeed-app-server",
    }
    CA_SYNC._rollout_appserver_if_needed(
        fake_request, **options, bundle="private CA generation 1"
    )
    first_digest = hashlib.sha256(b"private CA generation 1").hexdigest()
    assert calls[-1][0] == "PATCH"
    assert calls[-1][2]["spec"]["template"]["metadata"]["annotations"] == {
        CA_SYNC._CA_ROLLOUT_HASH_ANNOTATION: first_digest
    }
    call_count = len(calls)

    CA_SYNC._rollout_appserver_if_needed(
        fake_request, **options, bundle="private CA generation 1"
    )
    assert len(calls) == call_count + 1  # GET only, no repeat rollout
    assert calls[-1][0] == "GET"

    CA_SYNC._rollout_appserver_if_needed(
        fake_request, **options, bundle="private CA generation 2"
    )
    second_digest = hashlib.sha256(b"private CA generation 2").hexdigest()
    assert calls[-1][0] == "PATCH"
    assert calls[-1][2]["spec"]["template"]["metadata"]["annotations"] == {
        CA_SYNC._CA_ROLLOUT_HASH_ANNOTATION: second_digest
    }


def test_ca_rotation_rollout_tolerates_missing_initial_deployment() -> None:
    calls: list[tuple[str, str]] = []

    def fake_request(method, path, *_args):
        calls.append((method, path))
        return 404, {}

    CA_SYNC._rollout_appserver_if_needed(
        fake_request,
        enabled=True,
        namespace="ols",
        deployment_name="lightspeed-app-server",
        bundle="private CA",
    )
    assert calls == [
        ("GET", "/apis/apps/v1/namespaces/ols/deployments/lightspeed-app-server")
    ]

    CA_SYNC._rollout_appserver_if_needed(
        fake_request,
        enabled=False,
        namespace="ols",
        deployment_name="lightspeed-app-server",
        bundle="private CA",
    )
    assert len(calls) == 1


def test_ca_sync_retries_rollout_after_secret_was_already_updated(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = "private CA generation 2"
    calls: list[tuple[str, str]] = []
    deployment = {"spec": {"template": {"metadata": {"annotations": {}}}}}

    def fake_request(method, path, body=None, content_type=None):
        calls.append((method, path))
        if path.endswith("/configmaps/managed-ca"):
            return 200, {"data": {"ca-bundle.crt": bundle}}
        if path.endswith("/secrets/ols-keycloak-ca"):
            return 200, {"data": {"ca.crt": base64.b64encode(bundle.encode()).decode()}}
        if method == "GET":
            return 200, deployment
        assert content_type == "application/strategic-merge-patch+json"
        deployment["spec"]["template"]["metadata"]["annotations"].update(
            body["spec"]["template"]["metadata"]["annotations"]
        )
        return 200, {}

    # Exercise main's unchanged-Secret branch without accessing the cluster.
    class FakeResponse:
        status = 200

        def __init__(self, result):
            self.result = result

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.result).encode()

    class FakeOpener:
        def open(self, request, timeout=15):
            method = request.get_method()
            status, result = fake_request(
                method,
                request.full_url.removeprefix("https://kubernetes.default.svc"),
                json.loads(request.data) if request.data else None,
                request.headers.get("Content-type"),
            )
            assert status == 200
            return FakeResponse(result)

    token_file = tmp_path / "token"
    token_file.write_text("test-token", encoding="utf-8")
    original_open = open

    def fake_open(path, *args, **kwargs):
        return original_open(token_file if path.endswith("/serviceaccount/token") else path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(CA_SYNC.ssl, "create_default_context", lambda **_kwargs: object())
    monkeypatch.setattr(CA_SYNC, "build_opener", lambda *_args: FakeOpener())
    monkeypatch.setenv("SOURCE_NAMESPACE", "openshift-config-managed")
    monkeypatch.setenv("SOURCE_CONFIGMAP", "managed-ca")
    monkeypatch.setenv("SOURCE_KEY", "ca-bundle.crt")
    monkeypatch.setenv("TARGET_NAMESPACE", "ols")
    monkeypatch.setenv("TARGET_SECRET", "ols-keycloak-ca")
    monkeypatch.setenv("BUNDLE_KEY", "ca.crt")
    monkeypatch.setenv("OIDC_TRUST_ROLLOUT_ENABLED", "true")
    monkeypatch.setenv("OIDC_TRUST_DEPLOYMENT_NAME", "lightspeed-app-server")

    CA_SYNC.main()
    assert calls[-1] == ("PATCH", "/apis/apps/v1/namespaces/ols/deployments/lightspeed-app-server")
    assert deployment["spec"]["template"]["metadata"]["annotations"] == {
        CA_SYNC._CA_ROLLOUT_HASH_ANNOTATION: hashlib.sha256(bundle.encode()).hexdigest()
    }
    calls.clear()
    CA_SYNC.main()
    assert all(method != "PATCH" for method, _path in calls)


def test_ssl_cert_file_state_uses_original_operator_value_or_saved_marker(
    monkeypatch,
) -> None:
    deployment = {
        "metadata": {"annotations": {}},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "lightspeed-service-api",
                            "env": [
                                {
                                    "name": "SSL_CERT_FILE",
                                    "value": "/etc/certs/operator-bundle/ca-bundle.crt",
                                }
                            ],
                        }
                    ]
                }
            }
        },
    }
    monkeypatch.setattr(
        PATCHER, "_request", lambda *_args, **_kwargs: (200, deployment)
    )
    state = PATCHER._read_ssl_cert_file_state(
        None,
        {},
        "https://kubernetes.default.svc",
        "ols",
        "lightspeed-app-server",
        "lightspeed-service-api",
        required=True,
    )
    assert state == ("/etc/certs/operator-bundle/ca-bundle.crt", False)

    deployment["metadata"]["annotations"][
        PATCHER._OIDC_TRUST_ORIGINAL_SSL_ANNOTATION
    ] = "/etc/certs/original/ca-bundle.crt"
    state = PATCHER._read_ssl_cert_file_state(
        None,
        {},
        "https://kubernetes.default.svc",
        "ols",
        "lightspeed-app-server",
        "lightspeed-service-api",
        required=True,
    )
    assert state == ("/etc/certs/original/ca-bundle.crt", True)


def test_config_map_update_is_scoped_idempotent_and_preflightable(monkeypatch) -> None:
    monkeypatch.setenv("A2A_CONFIGMAP_NAME", "ols-config")
    monkeypatch.setenv("A2A_CONFIGMAP_KEY", "olsconfig.yaml")
    monkeypatch.setenv("A2A_TOKEN_EXCHANGE_ENABLED", "true")
    monkeypatch.setenv("A2A_OIDC_AUTH_ENABLED", "true")
    monkeypatch.setenv("A2A_OIDC_ISSUER_URL", "https://keycloak.example/realms/rca")
    monkeypatch.setenv("A2A_OIDC_AUDIENCE", "lightspeed-a2a")
    config_text = "ols_config:\n  authentication_config:\n    module: k8s\n"
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(_opener, _headers, _server, method, path, body=None, **_kwargs):
        nonlocal config_text
        calls.append((method, path, body))
        if method == "GET":
            return 200, {"data": {"olsconfig.yaml": config_text}}
        assert method == "PATCH"
        config_text = body["data"]["olsconfig.yaml"]
        return 200, {}

    monkeypatch.setattr(PATCHER, "_request", fake_request)
    preflight_hash = PATCHER._configure_a2a_service_config(
        None, {}, "https://kubernetes.default.svc", "ols", apply=False
    )
    assert len(calls) == 1

    applied_hash = PATCHER._configure_a2a_service_config(
        None, {}, "https://kubernetes.default.svc", "ols"
    )
    assert applied_hash == preflight_hash
    assert [call[0] for call in calls] == ["GET", "GET", "PATCH"]
    assert all(
        call[1] == "/api/v1/namespaces/ols/configmaps/ols-config" for call in calls
    )
    assert 'module: "oidc"' in config_text
    assert 'oidc_issuer_url: "https://keycloak.example/realms/rca"' in config_text
    assert 'oidc_audience: "lightspeed-a2a"' in config_text
    assert "token_exchange_enabled: true" in config_text


def _configure_operator_stop_env(monkeypatch, *, deadline: str = "10") -> None:
    monkeypatch.setenv("OPERATOR_SUBSCRIPTION_NAME", "lightspeed-operator")
    monkeypatch.setenv(
        "OPERATOR_DEPLOYMENT_NAME", "lightspeed-operator-controller-manager"
    )
    monkeypatch.setenv("OLS_CONFIG_NAME", "")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("POLL_DEADLINE_SECONDS", deadline)


def test_operator_stop_requires_succeeded_csv_and_two_zero_observations(
    monkeypatch,
) -> None:
    _configure_operator_stop_env(monkeypatch)
    monkeypatch.setenv("OLS_CONFIG_NAME", "cluster")
    calls: list[tuple[str, str]] = []

    def fake_request(_opener, _headers, _server, method, path, _body=None, **_kwargs):
        calls.append((method, path))
        if path.endswith("/subscriptions/lightspeed-operator"):
            return 200, {"status": {"installedCSV": "lightspeed-operator.v1"}}
        if path.endswith("/clusterserviceversions/lightspeed-operator.v1"):
            return 200, {"status": {"phase": "Succeeded"}}
        if method == "PATCH":
            return 200, {}
        return 200, {
            "metadata": {"generation": 7},
            "spec": {"replicas": 0},
            "status": {
                "observedGeneration": 7,
                "replicas": 0,
                "availableReplicas": 0,
            },
        }

    monkeypatch.setattr(PATCHER, "_request", fake_request)
    PATCHER._stop_operator_reconciling(
        None, {}, "https://kubernetes.default.svc", "ols"
    )

    deployment_gets = [
        (method, path)
        for method, path in calls
        if method == "GET"
        and "/deployments/lightspeed-operator-controller-manager" in path
    ]
    assert len(deployment_gets) == 2
    assert any(
        path.endswith("/clusterserviceversions/lightspeed-operator.v1")
        for _, path in calls
    )
    olsconfig_annotation = calls.index(
        ("PATCH", "/apis/ols.openshift.io/v1alpha1/olsconfigs/cluster")
    )
    verified_zero_gets = [
        index
        for index, (method, path) in enumerate(calls)
        if method == "GET"
        and path.endswith("/deployments/lightspeed-operator-controller-manager")
    ]
    assert len(verified_zero_gets) == 2
    assert olsconfig_annotation > max(verified_zero_gets)


def test_operator_stop_fails_closed_when_csv_is_not_succeeded(monkeypatch) -> None:
    _configure_operator_stop_env(monkeypatch, deadline="0")
    monkeypatch.setenv("OLS_CONFIG_NAME", "cluster")
    calls: list[tuple[str, str]] = []

    def fake_request(_opener, _headers, _server, method, path, _body=None, **_kwargs):
        calls.append((method, path))
        if path.endswith("/subscriptions/lightspeed-operator"):
            return 200, {"status": {"installedCSV": "lightspeed-operator.v1"}}
        return 200, {"status": {"phase": "Installing"}}

    monkeypatch.setattr(PATCHER, "_request", fake_request)
    with pytest.raises(RuntimeError, match="refusing to scale or patch"):
        PATCHER._stop_operator_reconciling(
            None, {}, "https://kubernetes.default.svc", "ols"
        )

    assert not any(method == "PATCH" for method, _ in calls)


def test_operator_stop_fails_closed_without_observed_zero_availability(
    monkeypatch,
) -> None:
    _configure_operator_stop_env(monkeypatch, deadline="0")
    monkeypatch.setenv("OLS_CONFIG_NAME", "cluster")
    calls: list[tuple[str, str]] = []

    def fake_request(_opener, _headers, _server, method, path, _body=None, **_kwargs):
        calls.append((method, path))
        if path.endswith("/subscriptions/lightspeed-operator"):
            return 200, {"status": {"installedCSV": "lightspeed-operator.v1"}}
        if path.endswith("/clusterserviceversions/lightspeed-operator.v1"):
            return 200, {"status": {"phase": "Succeeded"}}
        if method == "PATCH":
            return 200, {}
        return 200, {
            "metadata": {"generation": 8},
            "spec": {"replicas": 0},
            "status": {
                "observedGeneration": 7,
                "replicas": 1,
                "availableReplicas": 1,
            },
        }

    monkeypatch.setattr(PATCHER, "_request", fake_request)
    with pytest.raises(RuntimeError, match="refusing to patch"):
        PATCHER._stop_operator_reconciling(
            None, {}, "https://kubernetes.default.svc", "ols"
        )
    assert not any(
        method == "PATCH" and path.endswith("/olsconfigs/cluster")
        for method, path in calls
    )


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required for values parsing"
)
def test_standalone_values_file_parses() -> None:
    result = subprocess.run(
        [
            "helm",
            "template",
            "ols-values-check",
            str(CHART),
            "--values",
            str(REPO_ROOT / "variants/standalone/values-standalone.yaml"),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required for chart rendering"
)
def test_standalone_ols_application_overrides_render_enabled_bridge() -> None:
    """Render the *application* overrides, not just the variant's global values."""
    variant = REPO_ROOT / "variants/standalone/values-standalone.yaml"
    lines = variant.read_text(encoding="utf-8").splitlines()
    start = lines.index("    openshift-lightspeed-config:")
    overrides: list[tuple[str, str]] = []
    name: str | None = None
    for line in lines[start + 1 :]:
        if line.startswith("    ") and not line.startswith("     "):
            break  # next clusterGroup application
        if line.startswith("        - name: "):
            name = line.partition("- name: ")[2]
        elif line.startswith("          value: ") and name is not None:
            raw = line.partition("value: ")[2]
            value = ast.literal_eval(raw) if raw.startswith(("'", '"')) else raw
            overrides.append((name, str(value)))
            name = None
    assert name is None
    assert len(overrides) >= 8  # fail if the application block moved or was empty

    command = ["helm", "template", "ols-standalone", str(CHART), "-f", str(variant)]
    for key, value in overrides:
        command.extend(("--set-string", f"{key}={value}"))
    result = subprocess.run(command, capture_output=True, check=False, text=True)
    assert result.returncode == 0, result.stderr
    rendered = result.stdout
    assert "kind: Job" in rendered
    assert 'name: A2A_CONFIGMAP_NAME\n              value: "olsconfig"' in rendered
    assert 'name: A2A_TOKEN_EXCHANGE_ENABLED\n              value: "true"' in rendered
    assert 'name: A2A_OIDC_AUTH_ENABLED\n              value: "true"' in rendered
    assert 'name: OIDC_CA_TRUST_ENABLED\n              value: "true"' in rendered
    assert re.search(r'name: OIDC_TRUST_ROLLOUT_ENABLED\s+value: "true"', rendered)
    # The app-server image tag must be wired through from the variant, not a
    # literal pinned here (that broke on every image bump). Assert whatever tag
    # the variant currently sets is present in the rendered output.
    image_tag = next(value for key, value in overrides if key == "appServerPatch.image.tag")
    assert image_tag and image_tag in rendered


def _render_chart(
    tmp_path: Path,
    *,
    token_exchange: bool,
    config_map_name: str = "ols-config",
    oidc_trust: bool = False,
) -> str:
    values = tmp_path / "values.yaml"
    values.write_text(
        """global:
  clusterBaseDomain: example.test
  olsClusters:
    api-example-test-6443:
      apiURL: https://api.example.test:6443
appServerPatch:
  enabled: true
  a2a:
    clusterId: api-example-test-6443
    tokenExchangeEnabled: %s
    configMapName: %s
  image:
    repository: quay.io/example/ols
    tag: test
  oidcTrust:
    enabled: %s
identity:
  keycloak:
    caBundleSync:
      enabled: %s
"""
        % (
            str(token_exchange).lower(),
            json.dumps(config_map_name),
            str(oidc_trust).lower(),
            str(oidc_trust).lower(),
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["helm", "template", "ols-test", str(CHART), "-f", str(values)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


def _app_container_env(rendered: str) -> list[dict[str, str]]:
    lines = rendered.splitlines()
    env_index = next(
        index for index, line in enumerate(lines) if "name: CONTAINER_ENV_JSON" in line
    )
    value = lines[env_index + 1].split("value: ", 1)[1].strip()
    return json.loads(json.loads(value))


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required for chart rendering"
)
def test_chart_renders_only_a2a_split_auth_and_exchange_inputs(tmp_path: Path) -> None:
    rendered = _render_chart(tmp_path, token_exchange=True)
    app_env = _app_container_env(rendered)
    env_names = {entry["name"] for entry in app_env}

    assert {"A2A_ENABLED", "A2A_CLUSTER_ID", "A2A_RPC_URL"} <= env_names
    assert {
        "A2A_KEYCLOAK_ISSUER_URL",
        "A2A_EXCHANGE_AUDIENCE",
        "A2A_EXCHANGE_CLIENT_ASSERTION_TYPE",
        "A2A_SPIFFE_ENDPOINT_SOCKET",
        "A2A_SPIFFE_TIMEOUT_SECONDS",
        "A2A_KEYCLOAK_TIMEOUT_SECONDS",
        "A2A_TOKEN_EXCHANGE_TIMEOUT_SECONDS",
        "A2A_KEYCLOAK_DISCOVERY_CACHE_SECONDS",
    } <= env_names
    assert "A2A_INBOUND_AUDIENCE" not in env_names
    assert "A2A_INBOUND_AZP" not in env_names
    assert "A2A_TOKEN_EXCHANGE_ENABLED" not in env_names
    assert "SSL_CERT_FILE" not in env_names
    assert 'resourceNames:\n      - "ols-config"' in rendered
    assert (
        "name: A2A_OIDC_ISSUER_URL\n"
        '              value: "https://keycloak.apps.example.test/realms/rca"'
        in rendered
    )
    assert "openshift-lightspeed-config/a2a-config-sha256" in rendered


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required for chart rendering"
)
def test_chart_can_disable_exchange_without_disabling_inbound_a2a(
    tmp_path: Path,
) -> None:
    rendered = _render_chart(tmp_path, token_exchange=False)
    app_env = _app_container_env(rendered)
    env_names = {entry["name"] for entry in app_env}

    assert {"A2A_ENABLED", "A2A_CLUSTER_ID", "A2A_RPC_URL"} <= env_names
    assert "A2A_KEYCLOAK_ISSUER_URL" not in env_names
    assert "A2A_EXCHANGE_AUDIENCE" not in env_names
    assert "A2A_INBOUND_AUDIENCE" not in env_names
    assert 'name: A2A_OIDC_AUTH_ENABLED\n              value: "true"' in rendered
    assert 'name: A2A_OIDC_AUDIENCE\n              value: "lightspeed-a2a"' in rendered
    assert 'name: SPIFFE_VOLUME_MOUNT_PATH\n              value: ""' in rendered


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required for chart rendering"
)
def test_chart_opt_in_mounts_managed_ingress_ca_and_sets_ssl_cert_file(
    tmp_path: Path,
) -> None:
    rendered = _render_chart(tmp_path, token_exchange=True, oidc_trust=True)
    app_env = _app_container_env(rendered)

    assert "SSL_CERT_FILE" not in {entry["name"] for entry in app_env}
    assert 'name: OIDC_CA_TRUST_ENABLED\n              value: "true"' in rendered
    assert (
        'name: OIDC_CA_SECRET_NAME\n              value: "ols-keycloak-ca"' in rendered
    )
    assert 'name: OIDC_CA_SECRET_KEY\n              value: "ca.crt"' in rendered
    assert (
        'name: OIDC_CA_SECRET_MOUNT_PATH\n              value: "/etc/certs/ols-keycloak-ingress"'
        in rendered
    )
    assert 'name: OIDC_TRUST_ROLLOUT_ENABLED\n              value: "true"' in rendered
    assert (
        'resourceNames:\n      - "lightspeed-app-server"\n    verbs:\n      - get\n      - patch'
        in rendered
    )


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required for chart rendering"
)
def test_chart_requires_named_config_for_token_exchange(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="configMapName must name"):
        _render_chart(tmp_path, token_exchange=True, config_map_name="")
