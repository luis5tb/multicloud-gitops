#!/usr/bin/env python3
"""Temporary bridge: patch the operator-managed app-server Deployment with
the custom A2A-enabled OLS image and the A2A_* environment variables
vendor/lightspeed-service/ols/app/endpoints/a2a_auth.py requires, since
neither is exposed through the OLSConfig CRD today (see this chart's
README.md, "Temporary A2A bridge" section). Delete this script and its
Job/CronJob/RBAC templates entirely once the Operator/OLSConfig CRD natively
supports a custom service image and A2A configuration.

Retries on 404 (not just once) because the operator may not have reconciled
OLSConfig into a Deployment yet on a fresh install; the CronJob variant of
this same script re-asserts the patch periodically because the operator's
own reconcile loop can silently overwrite it on a later pass.
"""

from __future__ import annotations

import json
import os
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener


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
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
    poll_deadline = int(os.environ.get("POLL_DEADLINE_SECONDS", "150"))
    api_server = os.environ.get("KUBERNETES_API", "https://kubernetes.default.svc")

    # In-cluster API request; do not route through a configured outbound HTTP
    # proxy, and verify with the projected service-account CA.
    tls_context = ssl.create_default_context(cafile=ca_path)
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=tls_context))

    container_patch: dict = {"name": container_name}
    if image:
        container_patch["image"] = image
    if container_env:
        container_patch["env"] = container_env

    if "image" not in container_patch and "env" not in container_patch:
        print("Nothing to patch: no image and no env vars configured")
        return

    # A strategic merge patch (not a plain JSON merge patch) so the API
    # server merges `containers`/`env` by their `name` patchMergeKey instead
    # of replacing the whole list -- any other container or env var the
    # operator set stays untouched.
    patch_body = {"spec": {"template": {"spec": {"containers": [container_patch]}}}}
    path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/strategic-merge-patch+json",
    }
    data = json.dumps(patch_body).encode("utf-8")

    deadline = time.monotonic() + poll_deadline
    while True:
        request = Request(f"{api_server}{path}", data=data, headers=headers, method="PATCH")
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
