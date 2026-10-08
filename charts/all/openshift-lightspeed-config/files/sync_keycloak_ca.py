#!/usr/bin/env python3
"""Copy OpenShift's managed ingress CA bundle into a Secret in this namespace.

Adapted from this pattern's established CA-sync scripts, which write the
bundle into a ConfigMap. This copy targets a Secret instead, RESERVED FOR A
FUTURE "MODE B" MCP hardening (MCP validating the Keycloak JWT itself via
authorization_url + a mounted certificate_authority), which would read its
trusted issuer CA from a Secret key -- see the chart README's "MCP
hardening" section. The MCP hardening this chart actually enables today is
"Mode A" (cluster_auth_mode=passthrough), which needs no CA, so this sync is
disabled by default. Writing to a Secret base64-decodes the existing
Secret's data for comparison and writes the bundle back via stringData (the
API server base64-encodes stringData into data on write/merge-patch).
"""

from __future__ import annotations

import base64
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
    target_key = os.environ["BUNDLE_KEY"]
    api_server = os.environ.get("KUBERNETES_API", "https://kubernetes.default.svc")

    # This is an in-cluster API request; do not route it through a configured
    # outbound HTTP proxy. Verify it with the projected service-account CA.
    tls_context = ssl.create_default_context(cafile=ca_path)
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=tls_context))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def request(method: str, path: str, body: dict | None = None, content_type: str = "application/json") -> tuple[int, dict]:
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

    target_path = f"/api/v1/namespaces/{target_namespace}/secrets/{target_name}"
    status, target = request("GET", target_path)
    if status == 404:
        target_body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {"name": target_name, "namespace": target_namespace},
            "stringData": {target_key: bundle},
        }
        create_path = f"/api/v1/namespaces/{target_namespace}/secrets"
        status, _ = request("POST", create_path, target_body)
        # Another bootstrap/cron run could have created it after our GET.
        if status == 201:
            print(f"Created {target_namespace}/{target_name} from {source_namespace}/{source_name}")
            return
        if status != 409:
            raise RuntimeError(f"Creating {target_namespace}/{target_name} failed with HTTP {status}")
        status, target = request("GET", target_path)

    if status != 200:
        raise RuntimeError(f"Reading {target_namespace}/{target_name} failed with HTTP {status}")

    existing_encoded = target.get("data", {}).get(target_key)
    existing = base64.b64decode(existing_encoded).decode("utf-8") if existing_encoded else None
    if existing == bundle:
        print(f"{target_namespace}/{target_name} already has the current ingress CA bundle")
        return

    patch = {"stringData": {target_key: bundle}}
    patch_request_headers = {
        **headers,
        "Accept": "application/json",
        "Content-Type": "application/merge-patch+json",
    }
    patch_request = Request(
        f"{api_server}{target_path}",
        data=json.dumps(patch).encode("utf-8"),
        headers=patch_request_headers,
        method="PATCH",
    )
    try:
        with opener.open(patch_request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(f"Updating {target_namespace}/{target_name} failed with HTTP {response.status}")
    except HTTPError as error:
        raise RuntimeError(f"Updating {target_namespace}/{target_name} failed with HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"Kubernetes API request failed: {error.reason}") from error
    print(f"Updated {target_namespace}/{target_name} from {source_namespace}/{source_name}")


if __name__ == "__main__":
    main()
