"""Adapter outbound CA trust and the actual standalone cross-chart wiring."""

from __future__ import annotations

import copy
import json
import os
import shutil
import ssl
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts/all/lightspeed-hub"
VARIANT = ROOT / "variants/standalone/values-standalone.yaml"
SCRIPT = CHART / "files/build_keycloak_ca_bundle.sh"
TRUST_DIR = "/var/run/adapter-keycloak-trust"
SOURCE_DIR = "/var/run/adapter-keycloak-ca"
BASE = {
    "enabled": True,
    "global": {"clusterBaseDomain": "example.test"},
    "adapter": {
        "hubAudience": "hub-a2a",
        "keycloak": {
            "clientId": "hub-a2a",
            "clientSecretSecret": {"name": "adapter-client"},
            "caTrust": {
                "enabled": True,
                "caBundleConfigMapName": "issuer-ca",
                "caBundleKey": "custom.pem",
                "publicBundlePath": "/etc/ssl/certs/custom-public.pem",
            },
        },
    },
    "agenticOperator": {"enabled": False},
}


def render(tmp_path, values, *, check=True):
    if shutil.which("helm") is None:
        pytest.skip("helm is required to render chart")
    overrides = tmp_path / "values.yaml"
    overrides.write_text(yaml.safe_dump(values))
    return subprocess.run(
        ["helm", "template", "lightspeed-hub", str(CHART), "-f", str(overrides)],
        check=check,
        capture_output=True,
        text=True,
    )


def adapter_pod(stdout):
    return next(
        doc["spec"]["template"]["spec"]
        for doc in yaml.safe_load_all(stdout)
        if doc
        and doc["kind"] == "Deployment"
        and doc["metadata"]["name"] == "lightspeed-hub"
    )


def test_ca_trust_is_opt_in_and_chart_stays_dormant(tmp_path):
    values = copy.deepcopy(BASE)
    values["adapter"]["keycloak"]["caTrust"] = {"enabled": False}
    pod = adapter_pod(render(tmp_path, values).stdout)
    assert "initContainers" not in pod
    assert {v["name"] for v in pod["volumes"]} == {"serving-cert"}
    assert {e["name"] for e in pod["containers"][0]["env"]} == {
        "KEYCLOAK_CLIENT_SECRET"
    }
    values["enabled"] = False
    values["adapter"]["keycloak"]["caTrust"] = {"enabled": True}
    assert not list(yaml.safe_load_all(render(tmp_path, values).stdout))


@pytest.mark.parametrize("tls", [True, False])
@pytest.mark.parametrize("svid", [True, False])
def test_ca_trust_is_independent_of_serving_tls_and_client_auth(tmp_path, tls, svid):
    values = copy.deepcopy(BASE)
    values["adapter"]["tls"] = {"enabled": tls}
    values["adapter"]["svid"] = {"enabled": svid}
    # The init image must follow the same digest/tag selection as the adapter.
    values["adapter"]["image"] = {
        "repository": "quay.io/example/adapter",
        "digest": "sha256:" + "a" * 64,
        "pullPolicy": "Always",
    }
    if svid:
        values["adapter"]["keycloak"]["clientId"] = ""
    pod = adapter_pod(render(tmp_path, values).stdout)
    init = pod["initContainers"][0]
    container = pod["containers"][0]
    assert init["name"] == "build-keycloak-ca-bundle"
    assert (
        init["image"]
        == container["image"]
        == ("quay.io/example/adapter@sha256:" + "a" * 64)
    )
    assert init["imagePullPolicy"] == container["imagePullPolicy"] == "Always"
    assert init["command"] == ["/bin/sh", "-ec"]
    assert init["args"] == [SCRIPT.read_text()]
    assert {e["name"]: e["value"] for e in init["env"]} == {
        "PUBLIC_CA_BUNDLE": "/etc/ssl/certs/custom-public.pem",
        "KEYCLOAK_CA_BUNDLE": f"{SOURCE_DIR}/ca-bundle.crt",
        "COMBINED_CA_BUNDLE": f"{TRUST_DIR}/ca-bundle.crt",
    }
    assert init["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
        "capabilities": {"drop": ["ALL"]},
    }
    assert init["resources"]["limits"] == {"cpu": "100m", "memory": "64Mi"}
    assert init["volumeMounts"] == [
        {"name": "keycloak-ca", "mountPath": SOURCE_DIR, "readOnly": True},
        {"name": "keycloak-ca-trust", "mountPath": TRUST_DIR},
    ]
    env = {e["name"]: e for e in container["env"]}
    assert env["SSL_CERT_FILE"]["value"] == f"{TRUST_DIR}/ca-bundle.crt"
    assert set(env) == {
        "SSL_CERT_FILE",
        "SPIFFE_SOCKET" if svid else "KEYCLOAK_CLIENT_SECRET",
    }
    if not svid:
        assert env["KEYCLOAK_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {
            "name": "adapter-client",
            "key": "client-secret",
        }
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    assert mounts["keycloak-ca-trust"] == {
        "name": "keycloak-ca-trust",
        "mountPath": TRUST_DIR,
        "readOnly": True,
    }
    assert set(mounts) == {"keycloak-ca-trust"} | ({"serving-cert"} if tls else set())
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["keycloak-ca"]["configMap"] == {
        "name": "issuer-ca",
        "items": [{"key": "custom.pem", "path": "ca-bundle.crt"}],
    }
    assert volumes["keycloak-ca-trust"]["emptyDir"] == {}
    assert set(volumes) == {"keycloak-ca", "keycloak-ca-trust"} | (
        {"serving-cert"} if tls else set()
    )
    assert ("--tls-cert-file=/etc/a2a-adapter/tls/tls.crt" in container["args"]) == tls
    assert (
        "--keycloak-issuer=https://keycloak.apps.example.test/realms/rca"
        in container["args"]
    )


@pytest.mark.parametrize("field", ["caBundleConfigMapName", "caBundleKey"])
def test_missing_ca_source_configuration_fails_render(tmp_path, field):
    values = copy.deepcopy(BASE)
    values["adapter"]["keycloak"]["caTrust"][field] = ""
    result = render(tmp_path, values, check=False)
    assert result.returncode != 0
    assert f"adapter.keycloak.caTrust.{field} is required" in result.stderr


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is required to generate test CA certificates")
    directory = tmp_path_factory.mktemp("adapter-test-cas")
    result = []
    for name in ("public", "private"):
        cert = directory / f"{name}.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(directory / f"{name}.key"),
                "-out",
                str(cert),
                "-subj",
                f"/CN=test-only-{name}",
                "-days",
                "1",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
            ],
            check=True,
            capture_output=True,
        )
        result.append(cert.read_bytes())
    return result


@pytest.fixture
def bundle_builder(tmp_path):
    # Execute exactly the script embedded in the init container, not a copy.
    init = adapter_pod(render(tmp_path, BASE).stdout)["initContainers"][0]

    def build(public, private, *, public_exists=True, private_exists=True):
        public_path = tmp_path / "public.pem"
        private_path = tmp_path / "private.pem"
        output = tmp_path / "combined.pem"
        if public_exists:
            public_path.write_bytes(public)
        if private_exists:
            private_path.write_bytes(private)
        env = dict(os.environ)
        env.update(
            PUBLIC_CA_BUNDLE=str(public_path),
            KEYCLOAK_CA_BUNDLE=str(private_path),
            COMBINED_CA_BUNDLE=str(output),
        )
        result = subprocess.run(
            [*init["command"], *init["args"]],
            env=env,
            capture_output=True,
            text=True,
        )
        return result, output

    return build


def test_combined_bundle_preserves_both_roots(bundle_builder, certificates):
    public, private = certificates
    # Deliberately omit public's trailing newline to catch glued PEM blocks.
    public = public.rstrip()
    result, output = bundle_builder(public, private)
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == public + b"\n" + private
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(output))
    assert context.cert_store_stats()["x509_ca"] == 2
    assert not output.stat().st_mode & 0o022  # not group/world writable
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


@pytest.mark.parametrize(
    "problem",
    [
        "missing-public",
        "empty-public",
        "bad-public",
        "missing-private",
        "empty-private",
        "bad-private",
    ],
)
def test_bundle_builder_refuses_missing_or_non_pem_inputs(
    bundle_builder, certificates, problem
):
    public, private = certificates
    if problem == "empty-public":
        public = b""
    elif problem == "bad-public":
        public = b"not a PEM certificate"
    elif problem == "empty-private":
        private = b""
    elif problem == "bad-private":
        private = b"-----BEGIN CERTIFICATE-----\nincomplete"
    result, output = bundle_builder(
        public,
        private,
        public_exists=problem != "missing-public",
        private_exists=problem != "missing-private",
    )
    assert result.returncode != 0
    assert "build-keycloak-ca-bundle:" in result.stderr
    assert not output.exists()


def render_application(variant, name):
    if shutil.which("helm") is None:
        pytest.skip("helm is required to render chart")
    app = variant["clusterGroup"]["applications"][name]
    cmd = [
        "helm",
        "template",
        name,
        str(ROOT / app["path"]),
        "--namespace",
        app["namespace"],
        "-f",
        str(VARIANT),
    ]
    for override in app["overrides"]:
        cmd.extend(["--set-string", f"{override['name']}={override['value']}"])
    return list(
        filter(
            None,
            yaml.safe_load_all(
                subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
            ),
        )
    )


def test_actual_standalone_ca_producer_and_adapter_consumer_match():
    """Apply real Application overrides: loading the variant alone is not enough."""
    variant = yaml.safe_load(VARIANT.read_text())
    hub = render_application(variant, "lightspeed-hub")
    pod = next(
        d["spec"]["template"]["spec"]
        for d in hub
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "lightspeed-hub"
    )
    source = next(v["configMap"] for v in pod["volumes"] if v["name"] == "keycloak-ca")
    init = pod["initContainers"][0]
    env = {e["name"]: e["value"] for e in init["env"]}
    assert env["PUBLIC_CA_BUNDLE"] == "/etc/pki/tls/certs/ca-bundle.crt"
    assert init["image"] == pod["containers"][0]["image"]
    assert env["COMBINED_CA_BUNDLE"] == next(
        e["value"] for e in pod["containers"][0]["env"] if e["name"] == "SSL_CERT_FILE"
    )
    keycloak = render_application(variant, "keycloak-oidc")
    namespace = variant["clusterGroup"]["applications"]["lightspeed-hub"]["namespace"]
    assert namespace in variant["clusterGroup"]["namespaces"]
    for kind in ("Job", "CronJob"):
        producer = next(
            d
            for d in keycloak
            if d["kind"] == kind and "keycloak-ca-" in d["metadata"]["name"]
        )
        spec = producer["spec"]
        if kind == "CronJob":
            spec = spec["jobTemplate"]["spec"]
        env = {
            e["name"]: e["value"]
            for e in spec["template"]["spec"]["containers"][0]["env"]
        }
        assert namespace in json.loads(env["TARGET_NAMESPACES"])
        assert "mao" in json.loads(env["TARGET_NAMESPACES"])
        assert env["TARGET_CONFIGMAP"] == source["name"]
        assert env["BUNDLE_KEY"] == source["items"][0]["key"]
    role = next(
        d
        for d in keycloak
        if d["kind"] == "Role" and d["metadata"].get("namespace") == namespace
    )
    assert role["rules"][0]["resourceNames"] == [source["name"]]
    assert role["rules"][0]["verbs"] == ["get", "patch"]
    binding = next(
        d
        for d in keycloak
        if d["kind"] == "RoleBinding" and d["metadata"].get("namespace") == namespace
    )
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    assert binding["subjects"][0]["namespace"] == "keycloak-system"
