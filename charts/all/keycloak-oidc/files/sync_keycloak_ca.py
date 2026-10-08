#!/usr/bin/env python3
"""Copy OpenShift's managed ingress CA bundle into consumer namespaces."""

from __future__ import annotations

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
    target_namespaces = json.loads(os.environ["TARGET_NAMESPACES"])
    target_name = os.environ["TARGET_CONFIGMAP"]
    target_key = os.environ.get("BUNDLE_KEY", "ca-bundle.crt")
    api_server = os.environ.get("KUBERNETES_API", "https://kubernetes.default.svc")

    if not target_namespaces or any(not isinstance(ns, str) or not ns for ns in target_namespaces):
        raise RuntimeError("TARGET_NAMESPACES must be a non-empty JSON list of namespace names")

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

    for target_namespace in dict.fromkeys(target_namespaces):
        sync_target(target_namespace, target_name, target_key, bundle, request)


def sync_target(target_namespace: str, target_name: str, target_key: str, bundle: str, request) -> None:
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
            return
        if status != 409:
            raise RuntimeError(f"Creating {target_namespace}/{target_name} failed with HTTP {status}")
        status, target = request("GET", target_path)

    if status != 200:
        raise RuntimeError(f"Reading {target_namespace}/{target_name} failed with HTTP {status}")
    if target.get("data", {}).get(target_key) == bundle:
        print(f"{target_namespace}/{target_name} already has the current ingress CA bundle")
        return

    patch = {"data": {target_key: bundle}}
    status, _ = request("PATCH", target_path, patch, "application/merge-patch+json")
    if status != 200:
        raise RuntimeError(f"Updating {target_namespace}/{target_name} failed with HTTP {status}")
    print(f"Updated {target_namespace}/{target_name} from the managed ingress CA bundle")


if __name__ == "__main__":
    main()
