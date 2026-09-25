"""Analysis-only AgenticRun lifecycle through the OpenShift MCP server."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from typing import Any, Optional
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .identity import KeycloakTokenExchanger, RequestIdentity, current_request_identity

GROUP = "agentic.openshift.io"
VERSION = "v1alpha1"
ANALYZED_CONDITION = "Analyzed"

# Skills bundle attached to every AgenticRun this agent creates. Overridable
# via AGENTIC_RUN_SKILLS (JSON list of {"image": ..., "paths": [...]}) from
# the Helm chart; this is the static default used when that env var is unset.
_DEFAULT_SKILLS: list[dict[str, Any]] = [
    {
        "image": "quay.io/openshiftanalytics/agentic-skills:latest",
        "paths": ["/skills/cluster-troubleshoot/investigate-alert"],
    }
]


class OpenShiftMcpError(RuntimeError):
    """Raised when the OpenShift MCP tool call fails."""


def _valid_dns_label(value: str, field: str) -> str:
    if not value or len(value) > 63 or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value):
        raise ValueError(f"{field} must be a DNS label")
    return value


def _valid_dns_subdomain(value: str, field: str) -> str:
    if (
        not value
        or len(value) > 253
        or any(not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", part) for part in value.split("."))
    ):
        raise ValueError(f"{field} must be a DNS subdomain")
    return value


def _condition(obj: dict[str, Any], condition_type: str) -> Optional[dict[str, Any]]:
    for condition in obj.get("status", {}).get("conditions", []):
        if condition.get("type") == condition_type:
            return condition
    return None


def _run_status(run: dict[str, Any]) -> str:
    condition = _condition(run, ANALYZED_CONDITION)
    if condition is None:
        return "Pending"
    if condition.get("status") == "True":
        return "Proposed"
    if condition.get("status") == "False":
        return "Failed"
    return "Analyzing"


def _configured_skills() -> list[dict[str, Any]]:
    raw = os.getenv("AGENTIC_RUN_SKILLS", "").strip()
    if not raw:
        return _DEFAULT_SKILLS
    try:
        skills = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("AGENTIC_RUN_SKILLS must be a JSON list") from error
    if not isinstance(skills, list):
        raise ValueError("AGENTIC_RUN_SKILLS must be a JSON list")
    return skills


def build_analysis_only_run(
    request: str,
    analysis_agent: str,
    namespace: str,
    target_namespaces: Optional[list[str]] = None,
    identity: Optional[RequestIdentity] = None,
) -> dict[str, Any]:
    """Build an analysis-only CR with explicit delegated-operation audit data.

    metadata.name/namespace are set explicitly, not left to generateName:
    OpenShift MCP's resources_create_or_update applies resources via
    Kubernetes server-side apply (Apply(ctx, obj.GetName(), obj, ...) in
    upstream's resources.go), which is keyed on an already-known name --
    unlike a plain create, generateName is never resolved for it, so an
    unnamed manifest fails outright. namespace must also be explicit and
    match AGENTIC_RUN_NAMESPACE (what create_and_wait_for_analysis polls
    with), not whatever namespace MCP happens to default to when omitted.
    """

    if not request or not request.strip():
        raise ValueError("request must not be empty")
    if len(request) > 32768:
        raise ValueError("request must be 32768 characters or shorter")
    _valid_dns_subdomain(analysis_agent, "analysis_agent")
    _valid_dns_label(namespace, "namespace")

    spec: dict[str, Any] = {
        "request": request.strip(),
        "analysis": {"agent": analysis_agent},
        "analysisOutput": {"mode": "Default"},
    }
    if target_namespaces:
        spec["targetNamespaces"] = [
            _valid_dns_label(namespace, "target namespace") for namespace in target_namespaces
        ]
    skills = _configured_skills()
    if skills:
        spec["tools"] = {"skills": skills}

    annotations = {
        "agentic.openshift.io/executing-agent": "rca_agent",
        "agentic.openshift.io/on-behalf-of": identity.on_behalf_of if identity else "unknown-caller",
    }
    if identity is not None:
        annotations.update(
            {
                "agentic.openshift.io/request-id": identity.request_id,
                "agentic.openshift.io/caller-subject": str(
                    identity.caller_claims.get("sub", "unknown")
                ),
            }
        )
        if identity.actor:
            annotations["agentic.openshift.io/previous-actor"] = identity.actor

    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "AgenticRun",
        "metadata": {
            "name": f"rca-agent-{uuid4().hex[:20]}",
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "rca-agent"},
            "annotations": annotations,
        },
        "spec": spec,
    }


def _result_value(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        details = " ".join(
            str(content.text)
            for content in getattr(result, "content", [])
            if getattr(content, "type", None) == "text"
        )
        raise OpenShiftMcpError(details or "OpenShift MCP returned an error")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for content in getattr(result, "content", []):
        if getattr(content, "type", None) != "text":
            continue
        try:
            decoded = json.loads(content.text)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(decoded, dict):
            return decoded
    raise RuntimeError("OpenShift MCP returned no structured Kubernetes resource")


class OpenShiftMcpClient:
    """Short-lived MCP client using an exchanged, MCP-scoped access token."""

    def __init__(self, identity: RequestIdentity) -> None:
        self.url = os.getenv(
            "OPENSHIFT_MCP_URL",
            "http://openshift-mcp-server.openshift-mcp-server.svc.cluster.local:8080/mcp",
        )
        self.create_tool = os.getenv("OPENSHIFT_MCP_CREATE_TOOL", "resources_create_or_update")
        self.get_tool = os.getenv("OPENSHIFT_MCP_GET_TOOL", "resources_get")
        self.timeout_seconds = float(os.getenv("OPENSHIFT_MCP_TIMEOUT_SECONDS", "30"))
        self.identity = identity
        self.token = KeycloakTokenExchanger(
            issuer_url=os.getenv("KEYCLOAK_ISSUER_URL", ""),
            token_url=os.getenv("KEYCLOAK_TOKEN_URL", ""),
            client_id=os.getenv("KEYCLOAK_TOKEN_EXCHANGE_CLIENT_ID", "rca-agent"),
            audience=os.getenv("KEYCLOAK_TOKEN_EXCHANGE_AUDIENCE", "openshift-mcp"),
            scope=os.getenv("KEYCLOAK_TOKEN_EXCHANGE_SCOPE", ""),
            client_assertion_type=os.getenv(
                "KEYCLOAK_CLIENT_ASSERTION_TYPE",
                "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe",
            ),
            ca_bundle=os.getenv("KEYCLOAK_CA_BUNDLE"),
            timeout_seconds=float(os.getenv("KEYCLOAK_TIMEOUT_SECONDS", "5")),
        ).exchange(identity)
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Request-ID": identity.request_id,
            "X-Agent-Executing": "rca_agent",
            "X-Agent-On-Behalf-Of": identity.on_behalf_of,
        }
        if identity.actor:
            self.headers["X-Agent-Previous-Actor"] = identity.actor

    async def _call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        timeout = httpx.Timeout(self.timeout_seconds, read=self.timeout_seconds)
        async with httpx.AsyncClient(
            headers=self.headers, timeout=timeout
        ) as http_client:
            async with streamable_http_client(self.url, http_client=http_client) as (
                read_stream,
                write_stream,
                _,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(operation, arguments=arguments)
                    return _result_value(result)

    def create_or_update(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return _run_async(
            self._call(self.create_tool, {"resource": json.dumps(manifest, separators=(",", ":"))})
        )

    def get(self, namespace: str, kind: str, name: str) -> dict[str, Any]:
        return _run_async(
            self._call(
                self.get_tool,
                {
                    "apiVersion": f"{GROUP}/{VERSION}",
                    "kind": kind,
                    "namespace": namespace,
                    "name": name,
                },
            )
        )


def _run_async(coroutine: Any) -> Any:
    """Run an async MCP call from ADK's synchronous function-tool boundary."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: list[Any] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def _analysis_result_name(run: dict[str, Any]) -> Optional[str]:
    results = run.get("status", {}).get("steps", {}).get("analysis", {}).get("results", [])
    if not results:
        return None
    return results[-1].get("name")


def create_and_wait_for_analysis(
    request: str,
    target_namespaces: Optional[list[str]] = None,
    analysis_agent: Optional[str] = None,
) -> dict[str, Any]:
    """Create and poll an analysis-only AgenticRun through OpenShift MCP."""

    namespace = _valid_dns_label(
        os.getenv("AGENTIC_RUN_NAMESPACE", "default"), "AGENTIC_RUN_NAMESPACE"
    )
    selected_agent = analysis_agent or os.getenv("AGENTIC_RUN_ANALYSIS_AGENT", "default")
    timeout_seconds = int(os.getenv("AGENTIC_RUN_TIMEOUT_SECONDS", "900"))
    poll_interval_seconds = float(os.getenv("AGENTIC_RUN_POLL_INTERVAL_SECONDS", "5"))
    if timeout_seconds < 1:
        raise ValueError("AGENTIC_RUN_TIMEOUT_SECONDS must be positive")
    if poll_interval_seconds <= 0:
        raise ValueError("AGENTIC_RUN_POLL_INTERVAL_SECONDS must be positive")

    identity = current_request_identity()
    mcp_client = OpenShiftMcpClient(identity)
    run = mcp_client.create_or_update(
        build_analysis_only_run(request, selected_agent, namespace, target_namespaces, identity)
    )
    run_name = run["metadata"]["name"]
    deadline = time.monotonic() + timeout_seconds
    latest_run = run
    analysis_result: Optional[dict[str, Any]] = None

    while time.monotonic() < deadline:
        latest_run = mcp_client.get(namespace, "AgenticRun", run_name)
        condition = _condition(latest_run, ANALYZED_CONDITION)
        result_name = _analysis_result_name(latest_run)
        if condition and condition.get("status") == "False" and not result_name:
            break
        if condition and condition.get("status") in {"True", "False"} and result_name:
            try:
                analysis_result = mcp_client.get(namespace, "AnalysisResult", result_name)
            except OpenShiftMcpError as error:
                # MCP servers may race the operator while the result is created.
                if "not found" not in str(error).lower() and "404" not in str(error):
                    raise
                analysis_result = None
            if analysis_result is not None:
                break
        time.sleep(poll_interval_seconds)

    result_status = (analysis_result or {}).get("status", {})
    proposals = result_status.get("options", [])
    condition = _condition(latest_run, ANALYZED_CONDITION)
    return {
        "run": {
            "name": run_name,
            "namespace": namespace,
            "status": _run_status(latest_run),
            "conditions": latest_run.get("status", {}).get("conditions", []),
        },
        "analysis_result": analysis_result,
        "proposals": proposals,
        "timed_out": analysis_result is None,
        "message": (
            "Analysis completed. Execution and verification were not requested."
            if condition and condition.get("status") == "True" and analysis_result
            else "Analysis is still pending; use the run reference to inspect it."
        ),
    }
