"""Authenticated, synchronous A2A ingress backed by the OLS query pipeline."""

from __future__ import annotations

import os
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ols.app.endpoints.ols import conversation_request
from ols.app.models.models import LLMRequest
from ols.src.auth.a2a_identity import (
    A2AIdentityError,
    A2AIdentityProvider,
    A2ARequestContext,
)
from ols.src.query_helpers.a2a_context import REQUIRE_OPERATOR_MCP_TOOLS
from ols.utils import suid

router = APIRouter(tags=["a2a"])

_CLUSTER_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_BEARER_RE = re.compile(r"^Bearer ([^\s]+)$", re.IGNORECASE)
_MAX_QUERY_LENGTH = 32_000


async def get_a2a_request_context(request: Request) -> A2ARequestContext:
    """Validate invocation headers and resolve a per-request delegated identity."""
    authorization_values = request.headers.getlist("authorization")
    if len(authorization_values) != 1:
        raise HTTPException(status_code=401, detail="Unauthorized")
    bearer_match = _BEARER_RE.fullmatch(authorization_values[0])
    if bearer_match is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    caller_token = bearer_match.group(1)

    configured_cluster_id = os.getenv("OLS_A2A_CLUSTER_ID", "")
    if not _CLUSTER_ID_RE.fullmatch(configured_cluster_id):
        raise HTTPException(
            status_code=503, detail="A2A target identity is not configured"
        )

    cluster_values = request.headers.getlist("x-ols-cluster")
    if (
        len(cluster_values) != 1
        or not cluster_values[0]
        or "," in cluster_values[0]
        or not _CLUSTER_ID_RE.fullmatch(cluster_values[0])
    ):
        raise HTTPException(status_code=400, detail="Invalid X-OLS-Cluster")
    target_cluster_id = cluster_values[0]
    if target_cluster_id != configured_cluster_id:
        raise HTTPException(status_code=403, detail="Cluster target not allowed")

    identity_provider: A2AIdentityProvider | None = getattr(
        request.app.state, "a2a_identity_provider", None
    )
    if identity_provider is None:
        raise HTTPException(
            status_code=503, detail="A2A identity provider is not configured"
        )

    try:
        identity = await identity_provider.authenticate_and_exchange(
            caller_token, target_cluster_id
        )
    except A2AIdentityError as error:
        messages = {
            401: "Unauthorized",
            403: "Cluster target not allowed",
            503: "A2A identity service unavailable",
        }
        raise HTTPException(
            status_code=error.status_code,
            detail=messages[error.status_code],
        ) from None
    except Exception:
        raise HTTPException(
            status_code=503, detail="A2A identity service unavailable"
        ) from None

    if (
        not isinstance(identity, A2ARequestContext)
        or identity.target_cluster_id != target_cluster_id
        or not suid.check_suid(identity.user_id)
        or not identity.username
        or not identity.mcp_access_token
    ):
        raise HTTPException(
            status_code=503, detail="A2A identity provider returned invalid context"
        )
    return identity


def build_agent_card(public_url: str) -> dict[str, Any]:
    """Build the cluster-neutral A2A card for the statically configured gateway."""
    try:
        parsed_url = urlsplit(public_url)
    except ValueError:
        raise ValueError("A2A public URL must be a configured HTTPS origin") from None
    valid_origin = (
        parsed_url.scheme == "https"
        and parsed_url.hostname is not None
        and parsed_url.username is None
        and parsed_url.password is None
        and parsed_url.path in {"", "/"}
        and not parsed_url.query
        and not parsed_url.fragment
    )
    try:
        if parsed_url.port is not None and not (1 <= parsed_url.port <= 65535):
            valid_origin = False
    except ValueError:
        valid_origin = False
    if not valid_origin:
        raise ValueError("A2A public URL must be a configured HTTPS origin")

    return {
        "protocolVersion": "0.3.0",
        "name": "openshift_lightspeed",
        "description": "OpenShift Lightspeed query service",
        "url": public_url.rstrip("/"),
        "preferredTransport": "JSONRPC",
        "version": "0.0.1",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "openshift-lightspeed-query",
                "name": "OpenShift Lightspeed Query",
                "description": "Answer OpenShift questions using Lightspeed.",
                "tags": ["openshift", "kubernetes"],
                "examples": [],
            }
        ],
        "supportsAuthenticatedExtendedCard": False,
    }


@router.get("/.well-known/agent-card.json")
def agent_card() -> dict[str, Any]:
    """Serve public discovery without reflecting request Host or forwarded headers."""
    public_url = os.getenv("OLS_A2A_PUBLIC_URL", "")
    try:
        return build_agent_card(public_url)
    except ValueError:
        raise HTTPException(
            status_code=503, detail="A2A public HTTPS origin is not configured"
        ) from None


def _rpc_error(
    request_id: str | int | None,
    code: int,
    message: str,
    *,
    http_status: int = 200,
    data: dict[str, int] | None = None,
) -> JSONResponse:
    """Create a JSON-RPC error without forwarding exception or credential data."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": error},
        status_code=http_status,
    )


def _message_text(message: Any) -> tuple[str, str | None]:
    """Extract user text and an optional OLS conversation ID from an A2A message."""
    if not isinstance(message, dict) or message.get("role") != "user":
        raise ValueError("A user message is required")
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("A message must contain text parts")

    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") != "text":
            raise ValueError("Only text message parts are supported")
        text = part.get("text")
        if not isinstance(text, str):
            raise ValueError("Text parts must contain a string")
        text_parts.append(text)
    query = "\n".join(text_parts).strip()
    if not query or len(query) > _MAX_QUERY_LENGTH:
        raise ValueError("The query is empty or exceeds the supported size")

    context_id = message.get("contextId")
    if context_id is not None and (
        not isinstance(context_id, str) or not suid.check_suid(context_id)
    ):
        raise ValueError("contextId must be a UUID")
    return query, context_id


def _parse_rpc_request(payload: Any) -> tuple[str | int, LLMRequest] | JSONResponse:
    """Validate one JSON-RPC message/send request and convert it to OLS input."""
    if not isinstance(payload, dict):
        return _rpc_error(None, -32600, "Invalid Request", http_status=400)

    request_id = payload.get("id")
    if (
        payload.get("jsonrpc") != "2.0"
        or isinstance(request_id, bool)
        or not isinstance(request_id, (str, int))
        or not isinstance(payload.get("method"), str)
    ):
        return _rpc_error(None, -32600, "Invalid Request", http_status=400)
    if payload["method"] != "message/send":
        return _rpc_error(request_id, -32601, "Method not found")

    params = payload.get("params")
    if not isinstance(params, dict) or "mcp_headers" in params:
        return _rpc_error(request_id, -32602, "Invalid params")
    configuration = params.get("configuration")
    if isinstance(configuration, dict) and configuration.get("blocking") is False:
        return _rpc_error(request_id, -32602, "Only blocking requests are supported")
    try:
        query, context_id = _message_text(params.get("message"))
        llm_request = LLMRequest(
            query=query,
            conversation_id=context_id,
            mcp_headers=None,
        )
    except (ValueError, ValidationError):
        return _rpc_error(request_id, -32602, "Invalid message parameters")
    return request_id, llm_request


async def _run_query_pipeline(
    request_id: str | int,
    llm_request: LLMRequest,
    identity: A2ARequestContext,
) -> Any | JSONResponse:
    """Run the stock query handler with this invocation's exchanged identity."""
    auth = (identity.user_id, identity.username, False, identity.mcp_access_token)
    strict_context_token = REQUIRE_OPERATOR_MCP_TOOLS.set(True)
    try:
        return await run_in_threadpool(conversation_request, llm_request, auth)
    except HTTPException as error:
        error_code = -32602 if error.status_code in {400, 413, 422} else -32000
        return _rpc_error(
            request_id,
            error_code,
            "Invalid query" if error_code == -32602 else "Lightspeed query failed",
            data={"httpStatus": error.status_code},
        )
    except Exception:
        return _rpc_error(
            request_id,
            -32000,
            "Lightspeed query failed",
            http_status=502,
        )
    finally:
        REQUIRE_OPERATOR_MCP_TOOLS.reset(strict_context_token)


@router.post("/")
async def a2a_jsonrpc(
    request: Request,
    identity: A2ARequestContext = Depends(get_a2a_request_context),
) -> JSONResponse:
    """Handle one blocking message/send through the normal OLS query pipeline."""
    try:
        payload = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error", http_status=400)

    parsed = _parse_rpc_request(payload)
    if isinstance(parsed, JSONResponse):
        return parsed
    request_id, llm_request = parsed
    response = await _run_query_pipeline(request_id, llm_request, identity)
    if isinstance(response, JSONResponse):
        return response

    if not response.response:
        return _rpc_error(request_id, -32000, "Lightspeed returned no answer")
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "role": "agent",
                "parts": [{"kind": "text", "text": response.response}],
                "contextId": response.conversation_id,
            },
        }
    )
