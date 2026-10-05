#!/usr/bin/env python3
"""Temporary bridge: patch the operator-managed app-server Deployment with
the custom A2A-enabled OLS image and the A2A_* environment variables
vendor/lightspeed-service/ols/app/endpoints/a2a_auth.py requires, since
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
1. Best-effort: annotate the installed CSV (resolved dynamically via the
   Subscription's status.installedCSV, since the CSV's name carries a
   version suffix that changes on upgrade) with
   operator.openshift.io/managementState: Unmanaged. This is the
   convention Cluster Version Operator-managed ClusterOperators use: this
   operator's own binary was checked directly (strings search) and does not
   reference that annotation key at all, so it is not confirmed to do
   anything here -- kept as defense-in-depth in case OLM itself honors it
   for CSV-owned deployments, not relied upon alone.
2. Scale the operator's own Deployment to 0 replicas. This is the part
   confirmed live to actually work: observed stable at spec.replicas=0
   with zero drift-correction over several minutes of direct observation,
   from both the operator itself (obviously, it is not running) and from
   OLM (which could in principle enforce the CSV's declared replica count,
   but empirically does not within that window).
3. Only then patch the app-server Deployment's image/env -- nothing is
   reconciling it anymore, so a single patch is expected to persist
   indefinitely instead of needing periodic re-assertion.

Steps 1-2 are best-effort and idempotent (safe to repeat on every sync);
failures there are logged but do not abort the run, since step 3 is the
one that actually matters and may still succeed even if, say, the
Subscription lookup fails for some unrelated reason.
"""

from __future__ import annotations

import json
import os
import ssl
import time
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
    management_state_annotation = os.environ.get(
        "OPERATOR_MANAGEMENT_STATE_ANNOTATION", "operator.openshift.io/managementState"
    ).strip()

    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
    poll_deadline = int(os.environ.get("POLL_DEADLINE_SECONDS", "150"))

    if subscription_name:
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

            annotate_body = {"metadata": {"annotations": {management_state_annotation: "Unmanaged"}}}
            status, _ = _request(
                opener,
                headers,
                api_server,
                "PATCH",
                csv_path,
                annotate_body,
                content_type="application/merge-patch+json",
            )
            print(
                f"Annotated {namespace}/{installed_csv} with "
                f"{management_state_annotation}=Unmanaged (HTTP {status}, best-effort)"
            )
        else:
            print(
                f"Could not resolve installedCSV from {namespace}/{subscription_name} "
                f"(HTTP {status}, best-effort, continuing)"
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
        print(f"Scaled {namespace}/{operator_deployment_name} to 0 replicas (HTTP {status})")


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

    _stop_operator_reconciling(opener, headers, api_server, namespace)

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
