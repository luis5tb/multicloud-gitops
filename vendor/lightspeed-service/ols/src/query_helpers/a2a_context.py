"""Invocation-local controls for the A2A adapter."""

from contextvars import ContextVar
from typing import Any, Sequence

REQUIRE_OPERATOR_MCP_TOOLS: ContextVar[bool] = ContextVar(
    "require_operator_mcp_tools", default=False
)


class A2AMCPUnavailableError(Exception):
    """Raised when A2A cannot use the operator-managed OpenShift MCP tools."""


def require_operator_mcp_tools(servers: dict[str, Any], tools: Sequence[Any]) -> None:
    """Require at least one tool loaded from the built-in OpenShift MCP server."""
    if not REQUIRE_OPERATOR_MCP_TOOLS.get():
        return

    if "openshift" not in servers or not any(
        (getattr(tool, "metadata", None) or {}).get("mcp_server") == "openshift"
        for tool in tools
    ):
        raise A2AMCPUnavailableError


def select_mcp_servers(servers: Sequence[Any]) -> list[Any]:
    """Limit A2A calls to the one fixed operator-managed OpenShift MCP server."""
    if not REQUIRE_OPERATOR_MCP_TOOLS.get():
        return list(servers)
    return [
        server for server in servers if getattr(server, "name", None) == "openshift"
    ]
