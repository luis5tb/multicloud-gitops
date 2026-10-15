#!/usr/bin/env python3
"""Temporary bridge: patch the operator-managed app-server Deployment with
the custom A2A-enabled OLS image and the a2a-split-supported A2A_* environment
variables, since these are not exposed through the OLSConfig CRD today (see
this chart's README.md, "Temporary A2A bridge" section). Delete this script
and its Job/RBAC templates once the Operator/OLSConfig CRD natively
supports a custom service image and A2A configuration.

The operator reconciles the app-server Deployment's entire container spec
from scratch on every pass -- confirmed live: a patched image AND env vars
both got silently wiped within minutes, far faster than any reasonable
periodic re-assert CronJob could outrun (observed 45+ Deployment
generations within ~90 minutes with at most two patch attempts in that
window, so the operator itself is the aggressor, not a ping-pong with this
script). Patching the app-server Deployment alone can never durably win
against that.

Instead, this stops the operator from reconciling at all, once:
1. REQUIRED / empirically confirmed: resolve the installed CSV via the
   Subscription, wait for `status.phase=Succeeded`, and scale the operator's
   own Deployment to
   0 replicas. Confirmed live to actually work: observed stable at
   spec.replicas=0 with zero drift-correction over several minutes, from
   both the operator itself (not running) and from OLM -- but only once the
   CSV has reached phase=Succeeded (OLM re-enforces the CSV's declared
   replica count while the CSV is still mid-install). A failed stop aborts
   before the optional management annotation or any operator-managed resource
   edits.
2. Best-effort pause request AFTER verifying the stop: annotate the OLSConfig
   CR (OLS_CONFIG_NAME, a cluster-scoped singleton) with
   operator.openshift.io/managementState: Unmanaged. Per Red Hat's Lightspeed
   docs this is a supported way to pause reconciliation, but this OLSConfig-CR
   path has not been verified on the target release and is not treated as proof
   of an effective pause.
3. Mode A MCP hardening (only when MCP_HARDENING_ENABLED=true): the
   operator-generated OpenShift MCP server ships with require_oauth=false,
   so a tokenless call falls back to the MCP pod's ServiceAccount -- a
   confused-deputy bypass of the whole identity chain. The OLSConfig CRD has
   no field to harden this (the operator hardcodes the MCP config TOML), so
   this patches the operator-generated MCP ConfigMap's TOML directly to set
   require_oauth=true + skip_jwt_verification=true +
   cluster_auth_mode=passthrough, then forces a rollout of the MCP
   Deployment. Done only AFTER the operator is stopped (steps 1-2), so the
   edit is not immediately reverted.
4. Configure the a2a-split OLS YAML config in exactly the explicitly
   configured ConfigMap: select OIDC inbound auth (`module`, issuer, audience)
   and set `a2a.token_exchange_enabled`. The bridge does not invent an
   `A2A_TOKEN_EXCHANGE_ENABLED` app-server env var: the service does not read
   one. A stable config hash on the Deployment template triggers a restart only
   when the config changes.
5. Only then patch the app-server Deployment's image/env/SPIRE and optional
   OIDC-CA mounts -- nothing is reconciling it anymore, so the changes persist
   without periodic re-assertion.

The OLSConfig annotation and MCP hardening are best-effort. The CSV-gated
scale-to-zero is mandatory and raises unless two GETs confirm an observed
zero-replica Deployment. The OLS config update is preflighted before stopping;
the app-server patch is required.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

_UNSUPPORTED_A2A_ENV_VARS = ("A2A_INBOUND_AUDIENCE", "A2A_INBOUND_AZP")
_EXCHANGE_A2A_ENV_VARS = (
    "A2A_KEYCLOAK_ISSUER_URL",
    "A2A_KEYCLOAK_CA_BUNDLE",
    "A2A_EXCHANGE_AUDIENCE",
    "A2A_EXCHANGE_CLIENT_ASSERTION_TYPE",
    "A2A_SPIFFE_JWT_AUDIENCE",
    "A2A_SPIFFE_ENDPOINT_SOCKET",
    "A2A_SPIFFE_TIMEOUT_SECONDS",
    "A2A_KEYCLOAK_TIMEOUT_SECONDS",
    "A2A_TOKEN_EXCHANGE_TIMEOUT_SECONDS",
    "A2A_KEYCLOAK_DISCOVERY_CACHE_SECONDS",
)
_OIDC_TRUST_ORIGINAL_SSL_ANNOTATION = (
    "openshift-lightspeed-config/original-ssl-cert-file"
)
_SPIFFE_VOLUME_NAME = "spiffe-workload-api"
_OIDC_CA_SECRET_VOLUME_NAME = "ols-keycloak-ingress-ca"


def _container_env_patch(
    container_env: list[dict], *, token_exchange_enabled: bool
) -> list[dict]:
    """Merge supported envs and delete entries owned by disabled A2A modes."""
    # The operator bridge previously set these for the old A2A branch. A
    # strategic merge patch otherwise leaves env entries absent from the new
    # list untouched, so explicitly delete the no-longer-consumed variables.
    delete_names = list(_UNSUPPORTED_A2A_ENV_VARS)
    if not token_exchange_enabled:
        delete_names.extend(_EXCHANGE_A2A_ENV_VARS)
    return [
        *(entry for entry in container_env if entry.get("name") not in delete_names),
        *({"name": name, "$patch": "delete"} for name in delete_names),
    ]


def _volume_mount_deletions(existing_mounts: list[dict], volume_name: str) -> list[dict]:
    """Delete named mounts by Kubernetes' strategic merge key, mountPath."""
    deletions = []
    for mount in existing_mounts:
        if mount.get("name") != volume_name:
            continue
        mount_path = mount.get("mountPath")
        if not mount_path:
            raise RuntimeError(
                f"Cannot remove volumeMount {volume_name!r} without its mountPath"
            )
        deletions.append(
            {"name": volume_name, "mountPath": mount_path, "$patch": "delete"}
        )
    return deletions


def _spiffe_volume_patches(
    mount_path: str, existing_mounts: list[dict] | None = None
) -> tuple[list[dict], list[dict]]:
    """Return named merge patches that add or remove the SPIFFE CSI mount."""
    if mount_path:
        return (
            [
                {
                    "name": _SPIFFE_VOLUME_NAME,
                    "mountPath": mount_path,
                    "readOnly": True,
                }
            ],
            [
                {
                    "name": _SPIFFE_VOLUME_NAME,
                    "csi": {"driver": "csi.spiffe.io", "readOnly": True},
                }
            ],
        )
    return (
        _volume_mount_deletions(existing_mounts or [], _SPIFFE_VOLUME_NAME),
        [{"name": _SPIFFE_VOLUME_NAME, "$patch": "delete"}],
    )


def _oidc_ca_volume_patches(
    *,
    enabled: bool,
    secret_name: str,
    secret_key: str,
    secret_mount_path: str,
    existing_mounts: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return named merge patches for the managed-ingress CA Secret volume."""
    if enabled:
        return (
            [
                {
                    "name": _OIDC_CA_SECRET_VOLUME_NAME,
                    "mountPath": secret_mount_path,
                    "readOnly": True,
                }
            ],
            [
                {
                    "name": _OIDC_CA_SECRET_VOLUME_NAME,
                    "secret": {
                        "secretName": secret_name,
                        "items": [{"key": secret_key, "path": "ingress-ca.crt"}],
                    },
                }
            ],
        )
    return (
        _volume_mount_deletions(existing_mounts or [], _OIDC_CA_SECRET_VOLUME_NAME),
        [{"name": _OIDC_CA_SECRET_VOLUME_NAME, "$patch": "delete"}],
    )


def _container_volume_mounts(deployment: dict, container_name: str) -> list[dict]:
    """Read the named container's actual mounts before building delete patches."""
    containers = (
        deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    container = next(
        (item for item in containers if item.get("name") == container_name), None
    )
    return (container or {}).get("volumeMounts", [])


def _read_volume_mounts_when_available(
    opener,
    headers: dict,
    api_server: str,
    namespace: str,
    deployment_name: str,
    container_name: str,
    *,
    poll_interval: int,
    poll_deadline: int,
) -> list[dict]:
    """Wait for the app-server Deployment and return its current container mounts."""
    path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}"
    deadline = time.monotonic() + poll_deadline
    while True:
        status, deployment = _request(opener, headers, api_server, "GET", path)
        if status == 200:
            return _container_volume_mounts(deployment, container_name)
        if status != 404 or time.monotonic() >= deadline:
            raise RuntimeError(
                f"Could not inspect {namespace}/{deployment_name} volume mounts "
                f"(HTTP {status})"
            )
        print(
            f"{namespace}/{deployment_name} not found yet while reading mounts, "
            f"retrying in {poll_interval}s"
        )
        time.sleep(poll_interval)


def _ssl_cert_file_transition(
    container_env: list[dict],
    *,
    oidc_trust_enabled: bool,
    original_ssl_cert_file: str | None,
    has_saved_original: bool,
) -> tuple[list[dict], dict]:
    """Use the operator's generated bundle when opted in, restore it on disable."""
    env = [entry for entry in container_env if entry.get("name") != "SSL_CERT_FILE"]
    if oidc_trust_enabled:
        if not original_ssl_cert_file:
            raise RuntimeError(
                "OIDC CA trust requires an existing operator SSL_CERT_FILE value "
                "to preserve the service's system/public trust configuration"
            )
        env.append({"name": "SSL_CERT_FILE", "value": original_ssl_cert_file})
        return env, {_OIDC_TRUST_ORIGINAL_SSL_ANNOTATION: original_ssl_cert_file}
    if has_saved_original:
        if not original_ssl_cert_file:
            raise RuntimeError("Saved original SSL_CERT_FILE annotation is empty")
        env.append({"name": "SSL_CERT_FILE", "value": original_ssl_cert_file})
        return env, {_OIDC_TRUST_ORIGINAL_SSL_ANNOTATION: None}
    return container_env, {}


def _read_ssl_cert_file_state(
    opener,
    headers: dict,
    api_server: str,
    namespace: str,
    deployment_name: str,
    container_name: str,
    *,
    required: bool,
) -> tuple[str | None, bool]:
    """Read the original operator SSL_CERT_FILE so the optional patch is reversible."""
    deployment_path = (
        f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}"
    )
    status, deployment = _request(opener, headers, api_server, "GET", deployment_path)
    if status == 404 and not required:
        return None, False
    if status != 200:
        raise RuntimeError(
            f"Could not inspect {namespace}/{deployment_name} for SSL_CERT_FILE "
            f"(HTTP {status})"
        )

    annotations = deployment.get("metadata", {}).get("annotations", {})
    saved_original = annotations.get(_OIDC_TRUST_ORIGINAL_SSL_ANNOTATION)
    if saved_original:
        return saved_original, True

    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get(
        "containers", []
    )
    app_container = next(
        (item for item in containers if item.get("name") == container_name), None
    )
    ssl_env = next(
        (
            item
            for item in (app_container or {}).get("env", [])
            if item.get("name") == "SSL_CERT_FILE"
        ),
        None,
    )
    original = ssl_env.get("value") if ssl_env else None
    if required and not original:
        raise RuntimeError(
            f"{namespace}/{deployment_name} has no literal SSL_CERT_FILE env to "
            "preserve; refusing to enable the optional CA bundle"
        )
    if required and os.path.dirname(os.path.dirname(original)) != "/etc/certs":
        raise RuntimeError(
            f"{namespace}/{deployment_name} SSL_CERT_FILE must be inside a direct "
            "/etc/certs child directory for the a2a-split runner's CA discovery"
        )
    return original, False


def _request(
    opener,
    headers: dict,
    api_server: str,
    method: str,
    path: str,
    body: dict | None = None,
    content_type: str = "application/json",
) -> tuple[int, dict]:
    request_headers = dict(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = content_type
    request = Request(f"{api_server}{path}", data=data, headers=request_headers, method=method)
    try:
        with opener.open(request, timeout=15) as response:
            payload = response.read()
            return response.status, json.loads(payload) if payload else {}
    except HTTPError as error:
        payload = error.read()
        return error.code, json.loads(payload) if payload else {}


def _operator_deployment_is_stopped(deployment: dict) -> bool:
    """Require the Deployment controller to observe a zero-replica scale-down."""
    metadata = deployment.get("metadata", {})
    spec = deployment.get("spec", {})
    status = deployment.get("status", {})
    generation = metadata.get("generation")
    observed_generation = status.get("observedGeneration")
    if not isinstance(generation, int) or not isinstance(observed_generation, int):
        return False
    return (
        spec.get("replicas", 1) == 0
        and observed_generation >= generation
        and (status.get("replicas", 0) or 0) == 0
        and (status.get("availableReplicas", 0) or 0) == 0
    )


def _stop_operator_reconciling(opener, headers: dict, api_server: str, namespace: str) -> None:
    subscription_name = os.environ.get("OPERATOR_SUBSCRIPTION_NAME", "").strip()
    operator_deployment_name = os.environ.get("OPERATOR_DEPLOYMENT_NAME", "").strip()
    ols_config_name = os.environ.get("OLS_CONFIG_NAME", "").strip()
    management_state_annotation = os.environ.get(
        "OPERATOR_MANAGEMENT_STATE_ANNOTATION", "operator.openshift.io/managementState"
    ).strip()

    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
    poll_deadline = int(os.environ.get("POLL_DEADLINE_SECONDS", "150"))

    if not subscription_name or not operator_deployment_name:
        raise RuntimeError(
            "OPERATOR_SUBSCRIPTION_NAME and OPERATOR_DEPLOYMENT_NAME are required "
            "to verify the Lightspeed Operator is stopped before patching its resources"
        )

    sub_path = (
        f"/apis/operators.coreos.com/v1alpha1/namespaces/{namespace}"
        f"/subscriptions/{subscription_name}"
    )
    status, subscription = _request(opener, headers, api_server, "GET", sub_path)
    installed_csv = (
        subscription.get("status", {}).get("installedCSV", "") if status == 200 else ""
    )
    if not installed_csv:
        raise RuntimeError(
            f"Could not resolve installedCSV from {namespace}/{subscription_name} "
            f"(HTTP {status}); refusing to patch while the operator may still reconcile"
        )

    csv_path = (
        f"/apis/operators.coreos.com/v1alpha1/namespaces/{namespace}"
        f"/clusterserviceversions/{installed_csv}"
    )
    csv_deadline = time.monotonic() + poll_deadline
    while True:
        status, csv = _request(opener, headers, api_server, "GET", csv_path)
        phase = csv.get("status", {}).get("phase", "") if status == 200 else ""
        if phase == "Succeeded":
            break
        if time.monotonic() >= csv_deadline:
            raise RuntimeError(
                f"{namespace}/{installed_csv} did not reach Succeeded within "
                f"{poll_deadline}s (last phase={phase or 'unknown'}, HTTP {status}); "
                "refusing to scale or patch operator-managed resources"
            )
        print(
            f"{namespace}/{installed_csv} phase={phase or 'unknown'} (HTTP {status}); "
            f"waiting {poll_interval}s for Succeeded"
        )
        time.sleep(poll_interval)

    deployment_path = (
        f"/apis/apps/v1/namespaces/{namespace}/deployments/{operator_deployment_name}"
    )
    status, _ = _request(
        opener,
        headers,
        api_server,
        "PATCH",
        deployment_path,
        {"spec": {"replicas": 0}},
        content_type="application/strategic-merge-patch+json",
    )
    if status != 200:
        raise RuntimeError(
            f"Scaling {namespace}/{operator_deployment_name} to zero failed "
            f"with HTTP {status}; refusing to patch operator-managed resources"
        )
    print(f"Requested scale-to-zero for {namespace}/{operator_deployment_name}; verifying status")

    scale_deadline = time.monotonic() + poll_deadline
    zero_observations = 0
    while True:
        status, deployment = _request(
            opener,
            headers,
            api_server,
            "GET",
            deployment_path,
        )
        if status == 200 and _operator_deployment_is_stopped(deployment):
            zero_observations += 1
            if zero_observations >= 2:
                print(
                    f"Verified {namespace}/{operator_deployment_name} has observed "
                    "zero replicas and zero available replicas twice"
                )
                # Only request the optional operator-management annotation
                # after the mandatory CSV-gated scale-down has been observed.
                # Earlier ordering could leave a failed hook with the operator
                # paused but still running reconciliation resources.
                if ols_config_name:
                    olsconfig_path = (
                        f"/apis/ols.openshift.io/v1alpha1/olsconfigs/{ols_config_name}"
                    )
                    annotate_body = {
                        "metadata": {
                            "annotations": {
                                management_state_annotation: "Unmanaged"
                            }
                        }
                    }
                    try:
                        status, _ = _request(
                            opener,
                            headers,
                            api_server,
                            "PATCH",
                            olsconfig_path,
                            annotate_body,
                            content_type="application/merge-patch+json",
                        )
                        print(
                            f"Requested OLSConfig/{ols_config_name} managementState=Unmanaged "
                            f"after verifying scale-to-zero (HTTP {status})"
                        )
                    except Exception as error:  # verified scale-down is authoritative
                        print(
                            f"OLSConfig pause annotation failed ({error}); "
                            "operator scale-to-zero remains verified"
                        )
                return
        else:
            zero_observations = 0
            print(
                f"{namespace}/{operator_deployment_name} is not confirmed stopped "
                f"(HTTP {status}); waiting {poll_interval}s"
            )
        if time.monotonic() >= scale_deadline:
            raise RuntimeError(
                f"Could not verify {namespace}/{operator_deployment_name} stopped "
                f"with zero available replicas within {poll_deadline}s; refusing to patch"
            )
        time.sleep(poll_interval)


def _set_toml_top_level_key(text: str, key: str, value_literal: str) -> str:
    """Idempotently set a top-level TOML key to value_literal.

    If a line assigning `key` already exists (at any indent, top-level or in a
    table -- Mode A's keys are top-level in the operator's generated config),
    its value is replaced in place. Otherwise the assignment is prepended to
    the top of the file, before any `[section]` header, so it lands as a
    genuine top-level key rather than inside a trailing table.
    """
    pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=.*$", re.MULTILINE)
    assignment = f"{key} = {value_literal}"
    if pattern.search(text):
        # Use a function replacement so backslashes/sequences in value_literal
        # are not interpreted as regex backreferences.
        return pattern.sub(lambda _match: assignment, text, count=1)
    return f"{assignment}\n{text}"


def _replace_toml_key_in_place(text: str, key: str, value_literal: str) -> tuple[str, bool]:
    """Replace an EXISTING `key = ...` line's value, preserving its indentation.

    Unlike _set_toml_top_level_key this never *adds* the key: if it is absent
    (e.g. the operator reorganized its generated config and the key moved or
    was renamed), the text is returned unchanged with changed=False, so we
    never risk relocating a section-scoped key (like `alertmanager_url`, which
    lives under `[toolset_configs."observability/metrics"]`) to the top level.
    """
    pattern = re.compile(rf"^([ \t]*){re.escape(key)}[ \t]*=.*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return text, False
    indent = match.group(1)
    return pattern.sub(lambda _match: f"{indent}{key} = {value_literal}", text, count=1), True


def _harden_mcp(opener, headers: dict, api_server: str, namespace: str) -> None:
    """Mode A: patch the operator-generated MCP ConfigMap TOML to require a
    bearer (killing the tokenless pod-SA fallback) and let the API server
    validate it via passthrough, then roll the MCP Deployment onto it."""
    configmap_name = os.environ.get("MCP_CONFIGMAP_NAME", "openshift-mcp-server-config").strip()
    configmap_key = os.environ.get("MCP_CONFIGMAP_KEY", "config.toml").strip()
    deployment_name = os.environ.get("MCP_DEPLOYMENT_NAME", "").strip()
    require_oauth = os.environ.get("MCP_REQUIRE_OAUTH", "true").strip().lower()
    skip_jwt = os.environ.get("MCP_SKIP_JWT_VERIFICATION", "true").strip().lower()
    cluster_auth_mode = os.environ.get("MCP_CLUSTER_AUTH_MODE", "passthrough").strip()
    alertmanager_url = os.environ.get("MCP_ALERTMANAGER_URL", "").strip()
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
    poll_deadline = int(os.environ.get("POLL_DEADLINE_SECONDS", "150"))

    if not configmap_name or not configmap_key:
        print("MCP hardening enabled but configMapName/configMapKey is empty -- skipping")
        return

    configmap_path = f"/api/v1/namespaces/{namespace}/configmaps/{configmap_name}"

    # The operator must create the MCP ConfigMap first; on a cold install it
    # may not exist yet, so poll for it (same bounded pattern as the app-server
    # patch below).
    deadline = time.monotonic() + poll_deadline
    configmap: dict = {}
    while True:
        status, body = _request(opener, headers, api_server, "GET", configmap_path)
        if status == 200:
            configmap = body
            break
        if status == 404 and time.monotonic() < deadline:
            print(f"MCP ConfigMap {namespace}/{configmap_name} not found yet, retrying in {poll_interval}s")
            time.sleep(poll_interval)
            continue
        print(
            f"MCP hardening: could not GET {namespace}/{configmap_name} "
            f"(HTTP {status}) -- skipping (best-effort)"
        )
        return

    toml_text = configmap.get("data", {}).get(configmap_key)
    if toml_text is None:
        available = ", ".join(sorted(configmap.get("data", {}).keys())) or "<none>"
        print(
            f"MCP hardening: key {configmap_key!r} not in {namespace}/{configmap_name} "
            f"(keys present: {available}) -- skipping (best-effort)"
        )
        return

    hardened = _set_toml_top_level_key(toml_text, "require_oauth", require_oauth)
    hardened = _set_toml_top_level_key(hardened, "skip_jwt_verification", skip_jwt)
    hardened = _set_toml_top_level_key(hardened, "cluster_auth_mode", f'"{cluster_auth_mode}"')

    # Correct the operator-generated alertmanager_url. The operator ships a URL
    # whose port does not exist on OpenShift's alertmanager-main Service
    # (observed live: :9095, but the Service only has web=9094/tenancy=9092/
    # metrics=9097). A connection to an undefined Service port is blackholed, so
    # the MCP's GetAlertsHandler hangs 30s and fails with "context deadline
    # exceeded", stalling the whole agent request. Replace it in place (only if
    # the key exists -- it lives under [toolset_configs."observability/metrics"],
    # so we never relocate it to the top level).
    if alertmanager_url:
        hardened, am_changed = _replace_toml_key_in_place(
            hardened, "alertmanager_url", f'"{alertmanager_url}"'
        )
        if not am_changed:
            print(
                "MCP hardening: alertmanager_url key not found in config.toml -- "
                "skipping that override (operator config layout may have changed)"
            )

    if hardened == toml_text:
        # Idempotent: already hardened, so nothing to write AND no rollout to
        # force (stamping a fresh restart annotation on every ArgoCD sync would
        # pointlessly bounce the MCP pods each sync). Nothing more to do.
        print(
            f"MCP ConfigMap {namespace}/{configmap_name} already hardened (Mode A) "
            "-- no change, skipping rollout"
        )
        return

    status, _ = _request(
        opener,
        headers,
        api_server,
        "PATCH",
        configmap_path,
        {"data": {configmap_key: hardened}},
        content_type="application/merge-patch+json",
    )
    print(
        f"Hardened MCP ConfigMap {namespace}/{configmap_name} key {configmap_key} "
        f"(require_oauth={require_oauth}, skip_jwt_verification={skip_jwt}, "
        f"cluster_auth_mode={cluster_auth_mode}) (HTTP {status})"
    )

    if not deployment_name:
        print("MCP hardening: deploymentName empty -- not forcing a rollout (ConfigMap patched)")
        return

    # Only reached when the ConfigMap actually changed. Force the MCP pods onto
    # the hardened config via a restartedAt pod-template annotation, the same
    # mechanism `kubectl rollout restart` uses: the operator is stopped, so the
    # built-in Deployment controller rolls the pods on any pod-template change.
    # (The operator's OWN force-reload annotation, ols.openshift.io/force-reload,
    # is only meaningful while the operator reconciles, which it is not here.)
    restarted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rollout_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/restartedAt": restarted_at}
                }
            }
        }
    }
    deployment_path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}"
    deadline = time.monotonic() + poll_deadline
    while True:
        status, _ = _request(
            opener,
            headers,
            api_server,
            "PATCH",
            deployment_path,
            rollout_body,
            content_type="application/strategic-merge-patch+json",
        )
        if status == 200:
            print(f"Rolled out MCP Deployment {namespace}/{deployment_name} (restartedAt={restarted_at})")
            return
        if status == 404 and time.monotonic() < deadline:
            print(f"MCP Deployment {namespace}/{deployment_name} not found yet, retrying in {poll_interval}s")
            time.sleep(poll_interval)
            continue
        print(
            f"MCP hardening: could not roll out {namespace}/{deployment_name} "
            f"(HTTP {status}) -- ConfigMap is patched; pods will pick it up on next restart"
        )
        return


def _set_a2a_token_exchange_enabled(text: str, enabled: bool) -> str:
    """Set only a2a.token_exchange_enabled in the service's YAML config.

    a2a-split reads this flag from the OLS config file; it does not consume an
    A2A_TOKEN_EXCHANGE_ENABLED environment variable. Keep the existing OLS
    authentication config (including OIDC) and all other A2A settings intact.
    The operator-generated config is block-style YAML; reject inline/duplicate
    a2a declarations rather than risking a second, ambiguous YAML key.
    """
    lines = text.splitlines()
    if any(line.strip() == "---" for line in lines):
        raise ValueError("OLS config must be a single YAML document")
    a2a_headers = [
        index for index, line in enumerate(lines) if re.match(r"^a2a:", line)
    ]
    value = "true" if enabled else "false"
    if not a2a_headers:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["a2a:", f"  token_exchange_enabled: {value}"])
    else:
        if len(a2a_headers) != 1 or not re.fullmatch(
            r"a2a:[ \t]*(?:#.*)?", lines[a2a_headers[0]]
        ):
            raise ValueError("OLS config must contain one block-style top-level a2a: section")
        header = a2a_headers[0]
        section_end = len(lines)
        for index in range(header + 1, len(lines)):
            line = lines[index]
            if line and not line[0].isspace() and not line.lstrip().startswith("#"):
                section_end = index
                break

        child_indents = [
            match.group(1)
            for line in lines[header + 1 : section_end]
            if line.strip() and not line.lstrip().startswith("#")
            if (match := re.match(r"^([ \t]+)\S", line))
        ]
        child_indent = min(child_indents, key=len) if child_indents else "  "
        setting_pattern = re.compile(
            rf"^{re.escape(child_indent)}token_exchange_enabled[ \t]*:.*$"
        )
        settings = [
            index
            for index in range(header + 1, section_end)
            if setting_pattern.match(lines[index])
        ]
        if len(settings) > 1:
            raise ValueError(
                "OLS config has duplicate a2a.token_exchange_enabled settings"
            )
        if settings:
            index = settings[0]
            match = setting_pattern.match(lines[index])
            assert match is not None
            comment = re.search(r"[ \t]+#.*$", lines[index])
            suffix = comment.group(0) if comment else ""
            lines[index] = f"{child_indent}token_exchange_enabled: {value}{suffix}"
        else:
            lines.insert(section_end, f"{child_indent}token_exchange_enabled: {value}")

    result = "\n".join(lines)
    if text.endswith("\n"):
        result += "\n"
    return result


def _mapping_block(
    lines: list[str], key: str, indent: str, start: int, end: int
) -> tuple[int, int] | None:
    """Find one block-style YAML mapping at an exact indentation level."""
    prefix = f"{indent}{key}:"
    candidates = [
        index
        for index in range(start, end)
        if lines[index].startswith(prefix)
    ]
    if not candidates:
        return None
    if len(candidates) != 1 or not re.fullmatch(
        rf"{re.escape(indent)}{re.escape(key)}:[ \t]*(?:#.*)?", lines[candidates[0]]
    ):
        raise ValueError(f"OLS config must contain one block-style {key}: mapping")

    header = candidates[0]
    block_end = end
    for index in range(header + 1, end):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        line_indent = len(line) - len(line.lstrip(" "))
        if line_indent <= len(indent):
            block_end = index
            break
    return header, block_end


def _child_indent(lines: list[str], start: int, end: int, default: str) -> str:
    """Return the least-indented meaningful child line, or a safe default."""
    indents = [
        match.group(1)
        for line in lines[start:end]
        if line.strip() and not line.lstrip().startswith("#")
        if (match := re.match(r"^( +)\S", line))
    ]
    return min(indents, key=len) if indents else default


def _set_oidc_authentication(text: str, issuer_url: str, audience: str) -> str:
    """Select OIDC for inbound A2A and set its issuer and audience in OLS YAML."""
    if not issuer_url or not audience:
        raise ValueError("OLS OIDC issuer and audience must both be configured")
    try:
        parsed_issuer = urlsplit(issuer_url)
        parsed_issuer.port
    except ValueError as error:
        raise ValueError("OLS OIDC issuer must be a valid HTTPS URL") from error
    if (
        parsed_issuer.scheme != "https"
        or not parsed_issuer.hostname
        or parsed_issuer.username is not None
        or parsed_issuer.password is not None
        or parsed_issuer.query
        or parsed_issuer.fragment
    ):
        raise ValueError("OLS OIDC issuer must be a valid HTTPS URL")

    lines = text.splitlines()
    if any(line.strip() == "---" for line in lines):
        raise ValueError("OLS config must be a single YAML document")
    ols_block = _mapping_block(lines, "ols_config", "", 0, len(lines))
    if ols_block is None:
        raise ValueError("OLS config is missing the top-level ols_config: mapping")
    ols_header, ols_end = ols_block
    auth_indent = _child_indent(lines, ols_header + 1, ols_end, "  ")
    auth_block = _mapping_block(
        lines, "authentication_config", auth_indent, ols_header + 1, ols_end
    )

    settings = {
        "module": "oidc",
        "oidc_issuer_url": issuer_url,
        "oidc_audience": audience,
    }
    if auth_block is None:
        field_indent = auth_indent + "  "
        inserted = [f"{auth_indent}authentication_config:"] + [
            f"{field_indent}{key}: {json.dumps(value)}"
            for key, value in settings.items()
        ]
        lines[ols_end:ols_end] = inserted
    else:
        auth_header, auth_end = auth_block
        field_indent = _child_indent(lines, auth_header + 1, auth_end, auth_indent + "  ")
        additions: list[str] = []
        remove_indices: list[int] = []
        for key, value in settings.items():
            field_prefix = f"{field_indent}{key}:"
            matches = [
                index
                for index in range(auth_header + 1, auth_end)
                if lines[index].startswith(field_prefix)
            ]
            if len(matches) > 1:
                raise ValueError(f"OLS config has duplicate authentication_config.{key} fields")
            replacement = f"{field_indent}{key}: {json.dumps(value)}"
            if matches:
                lines[matches[0]] = replacement
            else:
                additions.append(replacement)
        # Remove k8s-only options after switching the shared auth module to
        # OIDC. In particular, k8s_ca_cert_path is a Pydantic FilePath and an
        # obsolete/unmounted path could otherwise make service config loading
        # fail even though OIDC does not use it.
        for key in ("k8s_cluster_api", "k8s_ca_cert_path", "skip_tls_verification"):
            field_prefix = f"{field_indent}{key}:"
            matches = [
                index
                for index in range(auth_header + 1, auth_end)
                if lines[index].startswith(field_prefix)
            ]
            if len(matches) > 1:
                raise ValueError(f"OLS config has duplicate authentication_config.{key} fields")
            remove_indices.extend(matches)
        for index in sorted(remove_indices, reverse=True):
            del lines[index]
        auth_end -= len(remove_indices)
        lines[auth_end:auth_end] = additions

    result = "\n".join(lines)
    if text.endswith("\n"):
        result += "\n"
    return result


def _configure_a2a_service_config(
    opener, headers: dict, api_server: str, namespace: str, *, apply: bool = True
) -> str | None:
    """Configure OIDC inbound auth and the A2A exchange flag in one ConfigMap.

    The caller supplies a ConfigMap name because RBAC intentionally grants
    access to only that one object. An empty name is permitted only when A2A
    and token exchange are both disabled.
    """
    configmap_name = os.environ.get("A2A_CONFIGMAP_NAME", "").strip()
    configmap_key = os.environ.get("A2A_CONFIGMAP_KEY", "olsconfig.yaml").strip()
    exchange_enabled = (
        os.environ.get("A2A_TOKEN_EXCHANGE_ENABLED", "false").strip().lower() == "true"
    )
    oidc_auth_enabled = (
        os.environ.get("A2A_OIDC_AUTH_ENABLED", "false").strip().lower() == "true"
    )
    if not configmap_name:
        if exchange_enabled or oidc_auth_enabled:
            raise RuntimeError(
                "A2A OIDC or token exchange is enabled but A2A_CONFIGMAP_NAME is empty"
            )
        return None
    if not configmap_key:
        raise RuntimeError("A2A_CONFIGMAP_KEY must not be empty")

    path = f"/api/v1/namespaces/{namespace}/configmaps/{configmap_name}"
    status, configmap = _request(opener, headers, api_server, "GET", path)
    if status != 200:
        raise RuntimeError(
            f"Could not GET A2A OLS config ConfigMap {namespace}/{configmap_name} "
            f"(HTTP {status})"
        )
    data = configmap.get("data", {})
    config_text = data.get(configmap_key) if isinstance(data, dict) else None
    if not isinstance(config_text, str):
        raise RuntimeError(
            f"A2A OLS config key {configmap_key!r} is missing from "
            f"ConfigMap {namespace}/{configmap_name}"
        )

    updated = config_text
    oidc_issuer_url = os.environ.get("A2A_OIDC_ISSUER_URL", "").strip()
    oidc_audience = os.environ.get("A2A_OIDC_AUDIENCE", "").strip()
    if oidc_auth_enabled:
        updated = _set_oidc_authentication(updated, oidc_issuer_url, oidc_audience)
    updated = _set_a2a_token_exchange_enabled(updated, exchange_enabled)
    if updated != config_text and configmap.get("immutable") is True:
        raise RuntimeError(
            f"A2A OLS config ConfigMap {namespace}/{configmap_name} is immutable"
        )
    if updated != config_text and apply:
        status, _ = _request(
            opener,
            headers,
            api_server,
            "PATCH",
            path,
            {"data": {configmap_key: updated}},
            content_type="application/merge-patch+json",
        )
        if status != 200:
            raise RuntimeError(
                f"Could not patch A2A OLS config ConfigMap {namespace}/{configmap_name} "
                f"(HTTP {status})"
        )
        print(
            f"Configured OLS OIDC inbound auth (enabled={str(oidc_auth_enabled).lower()}) "
            f"and a2a.token_exchange_enabled={str(exchange_enabled).lower()} in "
            f"ConfigMap {namespace}/{configmap_name} key {configmap_key}"
        )
    elif updated == config_text:
        print(
            f"A2A OLS config already has requested OIDC/exchange settings "
            f"in ConfigMap {namespace}/{configmap_name}"
        )
    else:
        print(
            f"Validated requested OIDC/exchange settings in "
            f"ConfigMap {namespace}/{configmap_name}; will patch after pausing the operator"
        )
    return hashlib.sha256(updated.encode("utf-8")).hexdigest()


def _ensure_operator_provisioned(opener, headers: dict, api_server: str, namespace: str) -> bool:
    """Make sure the operator has FINISHED provisioning the OLS stack before we
    stop it.

    Stopping the operator as soon as its CSV reports Succeeded (what
    _stop_operator_reconciling waits for) races the operator's own reconcile of
    the OLSConfig children: Succeeded means the operator is installed, not that
    it has created everything the OLSConfig implies. Observed live -- the
    operator got as far as the postgres/app-server/otel Deployments but was
    frozen (Unmanaged + scaled to 0) before it created the
    `lightspeed-postgres-bootstrap` Secret, so postgres stuck forever in
    ContainerCreating (secret not found) and the app-server's wait-for-postgres
    init crash-looped. Nothing reconciles a stopped operator, so it never
    self-corrected.

    Gate on the postgres Deployment reporting an available replica: that can
    only happen once the operator created the bootstrap Secret AND postgres
    actually started, which is exactly the state that makes a later stop safe.
    If postgres is not available, the operator may be mid-reconcile OR was
    frozen by a previous run of this Job -- so un-pause it (OLSConfig -> Managed,
    scale the operator back up) and wait. This makes the Job self-healing: it
    recovers an already-frozen half-built stack, not just prevents the race.

    Returns True once provisioning is confirmed; False if it could not be
    confirmed within the deadline -- the caller then leaves the operator RUNNING
    and skips the stop/patch this run, so the next sync retries instead of
    re-freezing a broken stack. Returns True immediately (old behavior) when the
    gate is disabled (POSTGRES_DEPLOYMENT_NAME empty).
    """
    postgres_deployment_name = os.environ.get("POSTGRES_DEPLOYMENT_NAME", "").strip()
    if not postgres_deployment_name:
        return True

    operator_deployment_name = os.environ.get("OPERATOR_DEPLOYMENT_NAME", "").strip()
    ols_config_name = os.environ.get("OLS_CONFIG_NAME", "").strip()
    management_state_annotation = os.environ.get(
        "OPERATOR_MANAGEMENT_STATE_ANNOTATION", "operator.openshift.io/managementState"
    ).strip()
    operator_replicas = int(os.environ.get("OPERATOR_REPLICAS", "1"))
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
    provision_deadline = int(os.environ.get("PROVISION_DEADLINE_SECONDS", "300"))

    pg_path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{postgres_deployment_name}"

    def postgres_available() -> bool:
        status, body = _request(opener, headers, api_server, "GET", pg_path)
        if status != 200:
            return False
        return (body.get("status", {}).get("availableReplicas", 0) or 0) >= 1

    if postgres_available():
        print(
            f"{namespace}/{postgres_deployment_name} is available -- the operator has "
            "finished provisioning the stack; safe to stop."
        )
        return True

    # Not available: the operator is either mid-reconcile or was frozen by a
    # previous run before it finished. Un-pause it so it can create/finish the
    # missing resources (e.g. the postgres bootstrap Secret), then wait.
    print(
        f"{namespace}/{postgres_deployment_name} is not available yet -- un-pausing the "
        "operator so it can finish provisioning before any stop."
    )
    if ols_config_name:
        olsconfig_path = f"/apis/ols.openshift.io/v1alpha1/olsconfigs/{ols_config_name}"
        status, _ = _request(
            opener,
            headers,
            api_server,
            "PATCH",
            olsconfig_path,
            {"metadata": {"annotations": {management_state_annotation: "Managed"}}},
            content_type="application/merge-patch+json",
        )
        print(f"Set OLSConfig/{ols_config_name} {management_state_annotation}=Managed (HTTP {status})")
    if operator_deployment_name:
        deployment_path = (
            f"/apis/apps/v1/namespaces/{namespace}/deployments/{operator_deployment_name}"
        )
        status, _ = _request(
            opener,
            headers,
            api_server,
            "PATCH",
            deployment_path,
            {"spec": {"replicas": operator_replicas}},
            content_type="application/strategic-merge-patch+json",
        )
        print(f"Scaled {namespace}/{operator_deployment_name} to {operator_replicas} replica(s) (HTTP {status})")

    deadline = time.monotonic() + provision_deadline
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        if postgres_available():
            print(
                f"{namespace}/{postgres_deployment_name} is now available -- operator finished "
                "provisioning; safe to stop."
            )
            return True
        print(f"Waiting {poll_interval}s for {namespace}/{postgres_deployment_name} to become available...")

    print(
        f"{namespace}/{postgres_deployment_name} did not become available within {provision_deadline}s -- "
        "leaving the operator RUNNING and skipping the stop/patch this run so it can keep "
        "reconciling; the next sync will retry."
    )
    return False


def main() -> None:
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    with open(token_path, encoding="utf-8") as token_file:
        token = token_file.read().strip()

    namespace = os.environ["TARGET_NAMESPACE"]
    deployment_name = os.environ["DEPLOYMENT_NAME"]
    container_name = os.environ["CONTAINER_NAME"]
    image = os.environ.get("CONTAINER_IMAGE", "").strip()
    container_env = json.loads(os.environ.get("CONTAINER_ENV_JSON", "[]"))
    spiffe_mount_path = os.environ.get("SPIFFE_VOLUME_MOUNT_PATH", "").strip()
    token_exchange_enabled = (
        os.environ.get("A2A_TOKEN_EXCHANGE_ENABLED", "false").strip().lower() == "true"
    )
    oidc_ca_trust_enabled = (
        os.environ.get("OIDC_CA_TRUST_ENABLED", "false").strip().lower() == "true"
    )
    oidc_ca_secret_name = os.environ.get("OIDC_CA_SECRET_NAME", "").strip()
    oidc_ca_secret_key = os.environ.get("OIDC_CA_SECRET_KEY", "").strip()
    oidc_ca_secret_mount_path = os.environ.get(
        "OIDC_CA_SECRET_MOUNT_PATH", "/etc/certs/ols-keycloak-ingress"
    ).strip()
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
    poll_deadline = int(os.environ.get("POLL_DEADLINE_SECONDS", "150"))
    api_server = os.environ.get("KUBERNETES_API", "https://kubernetes.default.svc")

    # In-cluster API request; do not route through a configured outbound HTTP
    # proxy, and verify with the projected service-account CA.
    tls_context = ssl.create_default_context(cafile=ca_path)
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=tls_context))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    # Do not stop the operator until it has actually finished provisioning the
    # OLS stack -- otherwise we freeze a half-built stack (see the function's
    # docstring). Self-healing: un-pauses a previously-frozen operator too.
    if not _ensure_operator_provisioned(opener, headers, api_server, namespace):
        # Left the operator RUNNING so it can keep reconciling. Fail the Job
        # (non-zero exit) so ArgoCD retries the sync and this converges, rather
        # than reporting success with the app-server still unpatched.
        raise SystemExit(
            "Operator provisioning not confirmed within the deadline -- operator left "
            "running to finish; failing so ArgoCD retries the sync."
        )

    # Remember the operator's SSL_CERT_FILE so the optional Keycloak trust
    # bundle can be disabled later without leaving the app pointed at a removed
    # emptyDir. This GET is required when enabling trust; a missing Deployment
    # is allowed only for a first-install run with trust disabled.
    original_ssl_cert_file, has_saved_ssl_cert_file = _read_ssl_cert_file_state(
        opener,
        headers,
        api_server,
        namespace,
        deployment_name,
        container_name,
        required=oidc_ca_trust_enabled,
    )

    # Validate the named ConfigMap/key and block-style YAML before stopping the
    # operator. A misconfigured bridge must not freeze an otherwise healthy OLS
    # stack before discovering that its config cannot be edited safely.
    _configure_a2a_service_config(
        opener, headers, api_server, namespace, apply=False
    )
    _stop_operator_reconciling(opener, headers, api_server, namespace)

    # Mode A MCP hardening -- only after the operator is stopped above, so the
    # ConfigMap edit is not immediately reverted. Best-effort/idempotent.
    if os.environ.get("MCP_HARDENING_ENABLED", "").strip().lower() == "true":
        _harden_mcp(opener, headers, api_server, namespace)

    # a2a-split reads both inbound OIDC settings and token_exchange_enabled from
    # its OLS YAML config (not an exchange-enable app env var). Only touch the
    # configured, explicitly named ConfigMap; a stable hash annotation below
    # restarts the app-server only when the effective config changes.
    a2a_config_hash = _configure_a2a_service_config(
        opener, headers, api_server, namespace
    )

    existing_mounts = _read_volume_mounts_when_available(
        opener,
        headers,
        api_server,
        namespace,
        deployment_name,
        container_name,
        poll_interval=poll_interval,
        poll_deadline=poll_deadline,
    )

    container_env, ssl_cert_annotations = _ssl_cert_file_transition(
        container_env,
        oidc_trust_enabled=oidc_ca_trust_enabled,
        original_ssl_cert_file=original_ssl_cert_file,
        has_saved_original=has_saved_ssl_cert_file,
    )
    container_patch: dict = {"name": container_name}
    if image:
        container_patch["image"] = image
    container_patch["env"] = _container_env_patch(
        container_env, token_exchange_enabled=token_exchange_enabled
    )

    pod_spec_patch: dict = {}
    if spiffe_mount_path:
        # SPIRE's workload API is a CSI driver volume, not something a
        # mutating webhook injects -- every workload that needs it declares
        # this exact volume itself (see charts/all/acme-agent/templates/
        # deployment.yaml, the only other consumer in this pattern). OLSConfig
        # has no field for it and the operator has no flag for it either, so
        # it goes in alongside the image/env patch above.
        container_patch["volumeMounts"] = [
            {"name": "spiffe-workload-api", "mountPath": spiffe_mount_path, "readOnly": True}
        ]
        pod_spec_patch["volumes"] = [
            {"name": "spiffe-workload-api", "csi": {"driver": "csi.spiffe.io", "readOnly": True}}
        ]

    # Use merge-keyed patches that can add or remove named resources. Strategic
    # merge omission alone leaves prior CSI mounts, Secret volumes, and env
    # values in place when their options are switched off.
    spiffe_mounts, spiffe_volumes = _spiffe_volume_patches(
        spiffe_mount_path, existing_mounts
    )
    oidc_ca_mounts, oidc_ca_volumes = _oidc_ca_volume_patches(
        enabled=oidc_ca_trust_enabled,
        secret_name=oidc_ca_secret_name,
        secret_key=oidc_ca_secret_key,
        secret_mount_path=oidc_ca_secret_mount_path,
        existing_mounts=existing_mounts,
    )
    container_patch["volumeMounts"] = spiffe_mounts + oidc_ca_mounts
    pod_spec_patch["volumes"] = spiffe_volumes + oidc_ca_volumes

    if len(container_patch) == 1:
        print("Nothing to patch: no image, env vars, or SPIFFE mount configured")
        return

    # A strategic merge patch (not a plain JSON merge patch) so the API
    # server merges `containers`/`env`/`volumeMounts`/`volumes` by their
    # patchMergeKey instead of replacing the whole list -- any other
    # container, env var, or volume the operator set stays untouched.
    pod_spec_patch["containers"] = [container_patch]
    pod_template: dict = {"spec": pod_spec_patch}
    if a2a_config_hash:
        pod_template["metadata"] = {
            "annotations": {
                "openshift-lightspeed-config/a2a-config-sha256": a2a_config_hash
            }
        }
    patch_body = {"spec": {"template": pod_template}}
    if ssl_cert_annotations:
        patch_body["metadata"] = {"annotations": ssl_cert_annotations}
    path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}"
    patch_headers = {**headers, "Content-Type": "application/strategic-merge-patch+json"}
    data = json.dumps(patch_body).encode("utf-8")

    deadline = time.monotonic() + poll_deadline
    while True:
        request = Request(f"{api_server}{path}", data=data, headers=patch_headers, method="PATCH")
        try:
            with opener.open(request, timeout=15) as response:
                if response.status == 200:
                    print(f"Patched {namespace}/{deployment_name} container {container_name}")
                    return
                raise RuntimeError(
                    f"Patching {namespace}/{deployment_name} returned unexpected HTTP {response.status}"
                )
        except HTTPError as error:
            if error.code == 404 and time.monotonic() < deadline:
                print(f"{namespace}/{deployment_name} not found yet, retrying in {poll_interval}s")
                time.sleep(poll_interval)
                continue
            payload = error.read()
            raise RuntimeError(
                f"Patching {namespace}/{deployment_name} failed with HTTP {error.code}: {payload}"
            ) from error
        except URLError as error:
            raise RuntimeError(f"Kubernetes API request failed: {error.reason}") from error


if __name__ == "__main__":
    main()
