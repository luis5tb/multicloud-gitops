#!/usr/bin/env python3
"""Copy the injected service-serving CA bundle into the Secret repo C expects.

Bridge (B1): OpenShift's service-ca-operator injects the serving-CA bundle
into a ConfigMap key (`service-ca.crt` via inject-cabundle), but repo C
(lightspeed-agentic-operator, a2a-support) expects a SECRET with key `ca.crt`
(`gatewayCASecret`, controller/agenticrun/gateway.go). This stdlib-only
script copies ConfigMap -> Secret, creating or patching the target so it
tracks service-CA rotation. The Secret is job-owned on purpose: rendering it
as a chart manifest would make Argo fight the job's writes.
"""

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
    target_namespace = os.environ["TARGET_NAMESPACE"]
    target_name = os.environ["TARGET_SECRET"]
    target_key = os.environ["TARGET_KEY"]
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
    if "BEGIN CERTIFICATE" not in bundle:
        raise RuntimeError(f"{source_namespace}/{source_name} {source_key} has no certificates")

    target_path = f"/api/v1/namespaces/{target_namespace}/secrets/{target_name}"
    status, target = request("GET", target_path)
    if status == 404:
        # Same shape repo C's own manifests use for CAs: opaque Secret,
        # chart labels for ownership (the sync job is the writer; the
        # chart never renders this Secret itself).
        target_body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": target_name,
                "namespace": target_namespace,
                "labels": {"app.kubernetes.io/managed-by": "praxis-proxy-spoke-ca-sync"},
            },
            "type": "Opaque",
            "stringData": {target_key: bundle},
        }
        create_path = f"/api/v1/namespaces/{target_namespace}/secrets"
        status, _ = request("POST", create_path, target_body)
        # Another bootstrap/cron run could have created it after our GET.
        if status == 201:
            print(f"Created {target_namespace}/{target_name} from {source_namespace}/{source_name}")
        elif status == 409:
            status, target = request("GET", target_path)
        else:
            raise RuntimeError(f"Creating {target_namespace}/{target_name} failed with HTTP {status}")

    if status == 200:
        import base64

        current = (target.get("data") or {}).get(target_key)
        current = base64.b64decode(current).decode("utf-8") if current else None
        if current == bundle:
            print(f"{target_namespace}/{target_name} already has the current service CA bundle")
            return
        patch = {"stringData": {target_key: bundle}}
        status, _ = request("PATCH", target_path, patch, "application/merge-patch+json")
        if status != 200:
            raise RuntimeError(f"Updating {target_namespace}/{target_name} failed with HTTP {status}")
        print(f"Updated {target_namespace}/{target_name} from {source_namespace}/{source_name}")
    elif status != 201:
        raise RuntimeError(f"Reading {target_namespace}/{target_name} failed with HTTP {status}")


if __name__ == "__main__":
    main()
