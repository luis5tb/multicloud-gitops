#!/usr/bin/env python3
"""Copy OpenShift's ingress CA into the workload namespace and roll on change."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener


def main() -> None:
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    with open(token_path, encoding="utf-8") as token_file:
        token = token_file.read().strip()

    source_namespace = os.environ["SOURCE_NAMESPACE"]
    source_name = os.environ["SOURCE_CONFIGMAP"]
    source_key = os.environ["SOURCE_KEY"]
    target_namespace = os.environ["TARGET_NAMESPACE"]
    target_name = os.environ["TARGET_CONFIGMAP"]
    target_key = os.environ["BUNDLE_KEY"]
    deployment_name = os.environ["TARGET_DEPLOYMENT"]
    api_server = os.environ.get("KUBERNETES_API", "https://kubernetes.default.svc")

    # This is an in-cluster API request; do not route it through a configured
    # outbound HTTP proxy. Verify it with the projected service-account CA.
    tls_context = ssl.create_default_context(cafile=ca_path)
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=tls_context))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def request(
        method: str,
        path: str,
        body: dict | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict]:
        request_headers = headers.copy()
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = content_type
        http_request = Request(
            f"{api_server}{path}",
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with opener.open(http_request, timeout=15) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else {}
        except HTTPError as error:
            payload = error.read()
            return error.code, json.loads(payload) if payload else {}
        except URLError as error:
            raise RuntimeError(f"Kubernetes API request failed: {error.reason}") from error

    source_path = f"/api/v1/namespaces/{source_namespace}/configmaps/{source_name}"
    status, source = request("GET", source_path)
    if status != 200:
        raise RuntimeError(f"Reading {source_namespace}/{source_name} failed with HTTP {status}")

    bundle = source.get("data", {}).get(source_key)
    if not bundle:
        raise RuntimeError(f"{source_namespace}/{source_name} has no {source_key} data")

    target_path = f"/api/v1/namespaces/{target_namespace}/configmaps/{target_name}"
    status, target = request("GET", target_path)
    if status == 404:
        target_body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": target_name, "namespace": target_namespace},
            "data": {target_key: bundle},
        }
        create_path = f"/api/v1/namespaces/{target_namespace}/configmaps"
        status, _ = request("POST", create_path, target_body)
        # Another bootstrap/cron run could have created it after our GET.
        if status == 201:
            print(f"Created {target_namespace}/{target_name} from the managed ingress CA bundle")
        elif status == 409:
            status, target = request("GET", target_path)
        else:
            raise RuntimeError(f"Creating {target_namespace}/{target_name} failed with HTTP {status}")

    if status == 200 and target.get("data", {}).get(target_key) != bundle:
        patch = {"data": {target_key: bundle}}
        status, _ = request("PATCH", target_path, patch, "application/merge-patch+json")
        if status != 200:
            raise RuntimeError(f"Updating {target_namespace}/{target_name} failed with HTTP {status}")
        print(f"Updated {target_namespace}/{target_name} from the managed ingress CA bundle")
    elif status == 200:
        print(f"{target_namespace}/{target_name} already has the current ingress CA bundle")
    elif status != 201:
        raise RuntimeError(f"Reading {target_namespace}/{target_name} failed with HTTP {status}")

    rollout_for_ca_change(request, target_namespace, deployment_name, bundle)


def rollout_for_ca_change(request, namespace: str, deployment_name: str, bundle: str) -> None:
    """Restart the process so its long-lived HTTPX client reloads the CA file."""

    path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}"
    digest = hashlib.sha256(bundle.encode("utf-8")).hexdigest()
    status, deployment = request("GET", path)
    if status == 404:
        # On the first Argo sync, the bootstrap hook runs before the Deployment
        # exists. The pod will be created afterward with the newly copied CA.
        print(f"{namespace}/{deployment_name} does not exist yet; no rollout needed")
        return
    if status != 200:
        raise RuntimeError(f"Reading {namespace}/{deployment_name} failed with HTTP {status}")
    current_digest = (
        deployment.get("spec", {})
        .get("template", {})
        .get("metadata", {})
        .get("annotations", {})
        .get("ca-bundle-sync-sha256")
    )
    if current_digest == digest:
        print(f"{namespace}/{deployment_name} already uses the current CA bundle")
        return

    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"ca-bundle-sync-sha256": digest}
                }
            }
        }
    }
    status, _ = request("PATCH", path, patch, "application/merge-patch+json")
    if status == 200:
        print(f"Restarted {namespace}/{deployment_name} to load the updated CA bundle")
    else:
        raise RuntimeError(f"Patching {namespace}/{deployment_name} failed with HTTP {status}")


if __name__ == "__main__":
    main()
