#!/usr/bin/env python3
"""Temporary bridge: patch the operator-managed app-server Deployment with
the custom A2A-enabled OLS image and the A2A_* environment variables
the OLS A2A image's ols/app/endpoints/a2a_auth.py requires, since
neither is exposed through the OLSConfig CRD today (see this chart's
README.md, "Temporary A2A bridge" section). Delete this script and its
Job/RBAC templates entirely once the Operator/OLSConfig CRD natively
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
1. PRIMARY / supported: annotate the OLSConfig CR (OLS_CONFIG_NAME, a
   cluster-scoped singleton) with
   operator.openshift.io/managementState: Unmanaged. Per Red Hat's
   Lightspeed docs this is THE supported way to pause the operator's
   reconcile of its managed resources, and a CRD investigation confirmed
   the target is the OLSConfig CR -- NOT the CSV. An earlier version of this
   script annotated the CSV instead; that was wrong (this operator's binary
   never references the annotation on the CSV) and has been corrected. This
   OLSConfig-CR path has not yet been verified live on this cluster, which
   is why step 2 is kept.
2. FALLBACK / empirically confirmed: scale the operator's own Deployment to
   0 replicas. Confirmed live to actually work: observed stable at
   spec.replicas=0 with zero drift-correction over several minutes, from
   both the operator itself (not running) and from OLM -- but only once the
   CSV has reached phase=Succeeded (OLM re-enforces the CSV's declared
   replica count while the CSV is still mid-install, so this waits for
   Succeeded first, resolving the CSV dynamically via the Subscription's
   status.installedCSV).
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
4. Only then patch the app-server Deployment's image/env -- nothing is
   reconciling it anymore, so a single patch is expected to persist
   indefinitely instead of needing periodic re-assertion.

Steps 1-3 are best-effort and idempotent (safe to repeat on every sync);
failures there are logged but do not abort the run, since step 4 is the
one that actually matters and may still succeed even if, say, the
Subscription lookup fails for some unrelated reason.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener


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


def _stop_operator_reconciling(opener, headers: dict, api_server: str, namespace: str) -> None:
    subscription_name = os.environ.get("OPERATOR_SUBSCRIPTION_NAME", "").strip()
    operator_deployment_name = os.environ.get("OPERATOR_DEPLOYMENT_NAME", "").strip()
    ols_config_name = os.environ.get("OLS_CONFIG_NAME", "").strip()
    management_state_annotation = os.environ.get(
        "OPERATOR_MANAGEMENT_STATE_ANNOTATION", "operator.openshift.io/managementState"
    ).strip()

    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
    poll_deadline = int(os.environ.get("POLL_DEADLINE_SECONDS", "150"))

    # PRIMARY / supported: annotate the OLSConfig CR (cluster-scoped singleton)
    # with managementState: Unmanaged. Per Red Hat's docs this is THE supported
    # way to pause the Lightspeed Operator's reconcile; a CRD investigation
    # confirmed the target is the OLSConfig CR, not the CSV (which an earlier
    # version of this script wrongly annotated). Not yet verified live on this
    # cluster, so the scale-to-0 below is kept as a confirmed fallback.
    if ols_config_name:
        olsconfig_path = f"/apis/ols.openshift.io/v1alpha1/olsconfigs/{ols_config_name}"
        annotate_body = {"metadata": {"annotations": {management_state_annotation: "Unmanaged"}}}
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
            f"Annotated OLSConfig/{ols_config_name} with "
            f"{management_state_annotation}=Unmanaged (HTTP {status}, primary/supported)"
        )

    if subscription_name and operator_deployment_name:
        sub_path = (
            f"/apis/operators.coreos.com/v1alpha1/namespaces/{namespace}"
            f"/subscriptions/{subscription_name}"
        )
        status, subscription = _request(opener, headers, api_server, "GET", sub_path)
        installed_csv = subscription.get("status", {}).get("installedCSV", "") if status == 200 else ""
        if installed_csv:
            csv_path = (
                f"/apis/operators.coreos.com/v1alpha1/namespaces/{namespace}"
                f"/clusterserviceversions/{installed_csv}"
            )

            # OLM actively enforces its own CSV-declared replica count while
            # a CSV is still mid-install (phase != Succeeded) -- confirmed
            # live: scaling the operator to 0 during this window gets
            # immediately scaled back to 1 by OLM itself (not the operator),
            # logged as "InstallWaiting ... Deployment does not have minimum
            # availability". That's a one-time install-phase behavior, not
            # OLM's steady-state behavior -- once Succeeded, a manual
            # scale-to-0 was observed to hold with no drift-correction from
            # OLM. So wait for Succeeded first; only then is scaling down
            # actually durable.
            deadline = time.monotonic() + poll_deadline
            phase = ""
            while time.monotonic() < deadline:
                status, csv = _request(opener, headers, api_server, "GET", csv_path)
                phase = csv.get("status", {}).get("phase", "") if status == 200 else ""
                if phase == "Succeeded":
                    break
                print(f"{namespace}/{installed_csv} phase={phase or 'unknown'} (HTTP {status}), waiting {poll_interval}s for Succeeded")
                time.sleep(poll_interval)
            if phase != "Succeeded":
                print(
                    f"{namespace}/{installed_csv} never reached Succeeded within "
                    f"{poll_deadline}s (last phase={phase or 'unknown'}) -- proceeding anyway, "
                    "but the operator may re-scale itself back up"
                )
        else:
            print(
                f"Could not resolve installedCSV from {namespace}/{subscription_name} "
                f"(HTTP {status}, best-effort, scaling down anyway)"
            )

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
            {"spec": {"replicas": 0}},
            content_type="application/strategic-merge-patch+json",
        )
        print(f"Scaled {namespace}/{operator_deployment_name} to 0 replicas (HTTP {status}, fallback)")


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

    _stop_operator_reconciling(opener, headers, api_server, namespace)

    # Mode A MCP hardening -- only after the operator is stopped above, so the
    # ConfigMap edit is not immediately reverted. Best-effort/idempotent.
    if os.environ.get("MCP_HARDENING_ENABLED", "").strip().lower() == "true":
        _harden_mcp(opener, headers, api_server, namespace)

    container_patch: dict = {"name": container_name}
    if image:
        container_patch["image"] = image
    if container_env:
        container_patch["env"] = container_env

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

    if len(container_patch) == 1:
        print("Nothing to patch: no image, env vars, or SPIFFE mount configured")
        return

    # A strategic merge patch (not a plain JSON merge patch) so the API
    # server merges `containers`/`env`/`volumeMounts`/`volumes` by their
    # patchMergeKey instead of replacing the whole list -- any other
    # container, env var, or volume the operator set stays untouched.
    pod_spec_patch["containers"] = [container_patch]
    patch_body = {"spec": {"template": {"spec": pod_spec_patch}}}
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
