"""Small, intentionally scoped client for the AgenticRun API.

The client can create and read only the resources required by the RCA agent.
It never approves, executes, verifies, or mutates a run after creation.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException
from kubernetes.client.rest import ApiException

GROUP = "agentic.openshift.io"
VERSION = "v1alpha1"
RUN_PLURAL = "agenticruns"
ANALYSIS_RESULT_PLURAL = "analysisresults"
ANALYZED_CONDITION = "Analyzed"


def _load_kubernetes_config() -> None:
    """Load in-cluster configuration, falling back to the local kubeconfig."""

    try:
        config.load_incluster_config()
    except ConfigException:
        config.load_kube_config()


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


def build_analysis_only_run(
    request: str,
    analysis_agent: str,
    target_namespaces: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build an analysis-only AgenticRun manifest.

    Keeping this construction separate makes the no-execution/no-verification
    guarantee explicit and easy to test.
    """

    if not request or not request.strip():
        raise ValueError("request must not be empty")
    if len(request) > 32768:
        raise ValueError("request must be 32768 characters or shorter")
    _valid_dns_subdomain(analysis_agent, "analysis_agent")

    spec: dict[str, Any] = {
        "request": request.strip(),
        "analysis": {"agent": analysis_agent},
        "analysisOutput": {"mode": "Default"},
    }
    if target_namespaces:
        spec["targetNamespaces"] = [
            _valid_dns_label(namespace, "target namespace")
            for namespace in target_namespaces
        ]

    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "AgenticRun",
        "metadata": {
            "generateName": "rca-agent-",
            "labels": {"app.kubernetes.io/managed-by": "rca-agent"},
        },
        "spec": spec,
    }


class AgenticRunClient:
    """Kubernetes client restricted to the RCA agent's read/write surface."""

    def __init__(self, api: Any = None) -> None:
        if api is None:
            _load_kubernetes_config()
            api = client.CustomObjectsApi()
        self.api = api

    def create_run(self, namespace: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.api.create_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=RUN_PLURAL,
            body=body,
        )

    def get_run(self, namespace: str, name: str) -> dict[str, Any]:
        return self.api.get_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=RUN_PLURAL,
            name=name,
        )

    def get_analysis_result(self, namespace: str, name: str) -> dict[str, Any]:
        return self.api.get_namespaced_custom_object(
            group=GROUP,
            version=VERSION,
            namespace=namespace,
            plural=ANALYSIS_RESULT_PLURAL,
            name=name,
        )


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
    """Create an analysis-only AgenticRun and return its analysis proposals.

    This is the only tool exposed to the ADK agent. It never supplies execution
    or verification fields and only waits for the Analyzed condition.
    """

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

    run_client = AgenticRunClient()
    run = run_client.create_run(
        namespace,
        build_analysis_only_run(request, selected_agent, target_namespaces),
    )
    run_name = run["metadata"]["name"]
    deadline = time.monotonic() + timeout_seconds
    latest_run = run
    analysis_result: Optional[dict[str, Any]] = None

    while time.monotonic() < deadline:
        latest_run = run_client.get_run(namespace, run_name)
        condition = _condition(latest_run, ANALYZED_CONDITION)
        result_name = _analysis_result_name(latest_run)
        if condition and condition.get("status") == "False" and not result_name:
            break
        if condition and condition.get("status") in {"True", "False"} and result_name:
            try:
                analysis_result = run_client.get_analysis_result(namespace, result_name)
            except ApiException as error:
                if error.status != 404:
                    raise
                # The operator can update the run reference just before the
                # result object becomes readable. Poll again in that case.
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
