"""A2A (Agent2Agent) endpoint for ACME's delegated OpenShift investigations.

Exposes two routes, registered without the ``/v1`` prefix (like ``health``
and ``metrics``):

* ``GET /.well-known/agent-card.json`` -- public, unauthenticated discovery
  document. Its advertised RPC origin (``A2A_RPC_URL``) is the public Praxis
  HTTPS route, not this pod's own address; it is a config/env value injected
  at deploy time, never a hardcoded hostname.
* ``POST /`` -- the JSON-RPC 2.0 endpoint ACME's Google ADK ``RemoteA2aAgent``
  sends requests to. It implements the two methods that agent actually needs
  (see ``agents/acme_agent/src/acme_agent/agent.py`` and the pinned
  ``a2a-sdk==1.1.5`` client, which sends method names in PascalCase, e.g.
  ``SendMessage``/``GetTask`` -- the legacy lowercase ``message/send``/
  ``tasks/get`` spellings used by ACME's own hand-written browser UI are also
  accepted as aliases for robustness): ``SendMessage`` (processes one query
  through OLS's existing pipeline and returns a terminal ``Task``) and
  ``GetTask`` (looks up a previously completed task). Streaming and
  cancellation are not implemented and are not claimed to work.

Every RPC call is authenticated and cluster-scoped (see ``a2a_auth.py``)
*before* this module runs any logic, and ``SendMessage`` performs a fresh
RFC 8693 token exchange per request so OLS's MCP client only ever sees the
exchanged token -- never the original caller's token, and never a
client-supplied override.

Task/conversation state is scoped to ``(authenticated caller, cluster id)``
(``CallerIdentity.task_scope``) and held in this process only; it does not
survive a restart and is not shared across replicas. That matches the scope
of this phase: OLS answers are produced synchronously within the
``SendMessage`` call itself, so no cross-replica task hand-off is needed yet.
A persistent, multi-replica-safe task store is tracked as later work (see the
migration plan's Praxis task-owner routing discussion).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ols.app.endpoints import a2a_auth
from ols.app.endpoints.a2a_auth import CallerIdentity
from ols.app.endpoints.ols import generate_response
from ols.app.models.models import LLMRequest
from ols.constants import QueryMode
from ols.utils import suid

logger = logging.getLogger(__name__)

router = APIRouter(tags=["a2a"])

AGENT_CARD_PATH = "/.well-known/agent-card.json"

_JSONRPC_VERSION = "2.0"

# Standard JSON-RPC 2.0 error codes, plus the A2A-specific "task not found"
# code used by the real a2a-sdk client (TaskNotFoundError == -32001), kept
# identical here so a genuine a2a-sdk client classifies it the same way.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_TASK_NOT_FOUND = -32001

_TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
_TASK_STATE_FAILED = "TASK_STATE_FAILED"

# JSON-RPC method name aliases this endpoint accepts. The real pinned
# ``a2a-sdk==1.1.5`` client used by ACME's ``RemoteA2aAgent`` sends the
# PascalCase names; the lowercase, slash-separated names are an older A2A
# spelling (used by ACME's own hand-written browser UI against its own
# server) accepted here too for robustness.
_SEND_MESSAGE_METHODS = frozenset({"SendMessage", "message/send"})
_GET_TASK_METHODS = frozenset({"GetTask", "tasks/get"})

# HTTP header a caller must never be able to use to inject its own MCP
# credential. OLS's own body field for this (``LLMRequest.mcp_headers``) is
# not reachable from the A2A JSON-RPC envelope at all; this header check (and
# the JSON-RPC param check in ``_rejects_mcp_header_override``) make the
# block explicit and independently testable rather than relying solely on
# "that field doesn't exist here".
_MCP_HEADERS_HTTP_HEADER = "mcp-headers"
_MCP_HEADERS_PARAM_KEYS = frozenset({"mcpheaders", "mcp_headers"})


@dataclass
class _TaskRecord:
    """A completed (or failed) A2A task, scoped to one (caller, cluster)."""

    task_scope: str
    task: dict[str, Any]
    created_at: float


_tasks: dict[str, _TaskRecord] = {}
_tasks_lock = threading.Lock()

# Tasks are kept only long enough for a client to poll them once or twice;
# unbounded growth of an in-memory dict is itself a resource-exhaustion risk.
_TASK_RETENTION_SECONDS = 600


def _prune_expired_tasks() -> None:
    cutoff = time.monotonic() - _TASK_RETENTION_SECONDS
    with _tasks_lock:
        expired = [tid for tid, record in _tasks.items() if record.created_at < cutoff]
        for tid in expired:
            del _tasks[tid]


def _store_task(task_id: str, task_scope: str, task: dict[str, Any]) -> None:
    _prune_expired_tasks()
    with _tasks_lock:
        _tasks[task_id] = _TaskRecord(
            task_scope=task_scope, task=task, created_at=time.monotonic()
        )


def _lookup_task(task_id: str, task_scope: str) -> Optional[dict[str, Any]]:
    with _tasks_lock:
        record = _tasks.get(task_id)
    # A task owned by a different (caller, cluster) is reported identically
    # to a missing one: existence must not leak across callers.
    if record is None or record.task_scope != task_scope:
        return None
    return record.task


def agent_card() -> dict[str, Any]:
    """Build the public A2A agent card.

    Shaped to match the real protobuf-JSON wire format emitted by
    ``google.protobuf.json_format.MessageToDict`` for ``a2a.types.AgentCard``
    (verified against the pinned ``a2a-sdk==1.1.5`` used by ACME): the RPC
    endpoint lives under ``supportedInterfaces``, not a bare top-level
    ``url``.
    """
    settings = a2a_auth.get_authenticator().settings
    return {
        "name": settings.agent_name,
        "description": settings.agent_description,
        "version": "1.0.0",
        "supportedInterfaces": [
            {
                "url": settings.rpc_url,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "investigate-openshift-cluster",
                "name": "investigate-openshift-cluster",
                "description": (
                    "Investigate and answer questions about the OpenShift "
                    "cluster this service is configured for, using that "
                    "cluster's own MCP tools."
                ),
                "tags": ["openshift", "troubleshooting"],
            }
        ],
    }


@router.get(AGENT_CARD_PATH)
async def get_agent_card() -> JSONResponse:
    """Serve the public, unauthenticated A2A discovery document."""
    return JSONResponse(agent_card())


def _jsonrpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": _JSONRPC_VERSION, "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": _JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _extract_text(message: dict[str, Any]) -> str:
    """Extract the query text from an A2A ``Message``'s ``parts``.

    Accepts both the real wire shape (``{"text": "..."}``, a protobuf
    ``oneof`` discriminator) and the legacy ``{"kind": "text", "text": "..."}``
    shape used by ACME's own hand-written UI -- both carry the text under a
    top-level ``text`` key, so one check covers both.
    """
    parts = message.get("parts") or []
    texts = [part.get("text", "") for part in parts if isinstance(part, dict) and part.get("text")]
    return "\n".join(texts).strip()


def _rejects_mcp_header_override(request: Request, body: dict[str, Any]) -> bool:
    """Return True if the caller attempted to inject an MCP credential override."""
    if request.headers.get(_MCP_HEADERS_HTTP_HEADER):
        return True
    params = body.get("params")
    if isinstance(params, dict):
        for key in params:
            if isinstance(key, str) and key.lower() in _MCP_HEADERS_PARAM_KEYS:
                return True
        message = params.get("message")
        if isinstance(message, dict):
            metadata = message.get("metadata")
            if isinstance(metadata, dict):
                for key in metadata:
                    if isinstance(key, str) and key.lower() in _MCP_HEADERS_PARAM_KEYS:
                        return True
    return False


def _agent_message(text: str, context_id: str) -> dict[str, Any]:
    return {
        "messageId": str(uuid.uuid4()),
        "contextId": context_id,
        "role": "ROLE_AGENT",
        "parts": [{"text": text}],
    }


def _completed_task(task_id: str, context_id: str, text: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": _TASK_STATE_COMPLETED,
            "message": _agent_message(text, context_id),
        },
    }


def _failed_task(task_id: str, context_id: str, reason: str) -> dict[str, Any]:
    return {
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": _TASK_STATE_FAILED,
            "message": _agent_message(reason, context_id),
        },
    }


def _derive_user_id(caller: CallerIdentity) -> str:
    """Derive a stable, UUID-shaped user id scoped to (caller, cluster).

    OLS's conversation cache and quota limiters expect a user id; this keeps
    conversation history isolated per (authenticated caller, cluster)
    without reusing a Kubernetes-style UID that does not exist for A2A
    callers.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"urn:ols:a2a:{caller.task_scope}"))


def _context_id_from_params(message: dict[str, Any]) -> str:
    context_id = message.get("contextId")
    if isinstance(context_id, str) and suid.check_suid(context_id):
        return context_id
    return suid.get_suid()


async def _handle_send_message(
    request_id: Any, params: dict[str, Any], caller: CallerIdentity
) -> dict[str, Any]:
    message = params.get("message")
    if not isinstance(message, dict):
        return _jsonrpc_error(request_id, _INVALID_PARAMS, "params.message is required")

    text = _extract_text(message)
    if not text:
        return _jsonrpc_error(request_id, _INVALID_PARAMS, "message has no text content")

    context_id = _context_id_from_params(message)
    task_id = suid.get_suid()
    user_id = _derive_user_id(caller)

    try:
        exchanged_token = await a2a_auth.get_authenticator().exchange_for_mcp(caller)
    except (a2a_auth.A2AWorkloadIdentityError, a2a_auth.A2AExchangeError) as error:
        logger.error("A2A token exchange failed for cluster %s: %s", caller.cluster_id, error)
        task = _failed_task(
            task_id, context_id, "Unable to obtain credentials for the OpenShift MCP server"
        )
        _store_task(task_id, caller.task_scope, task)
        return _jsonrpc_result(request_id, {"task": task})

    llm_request = LLMRequest(query=text, conversation_id=context_id, mode=QueryMode.ASK)

    try:
        summarizer_response = await asyncio.to_thread(
            generate_response,
            context_id,
            llm_request,
            user_id,
            False,  # skip_user_id_check
            False,  # streaming
            exchanged_token,  # user_token -- Token B, never the caller's own token
            None,  # client_headers -- never honor a client-supplied MCP override
            None,  # audit_ctx
        )
    except HTTPException as error:
        detail = error.detail
        reason = (
            detail.get("response", "The request could not be processed")
            if isinstance(detail, dict)
            else str(detail)
        )
        logger.warning(
            "A2A query failed for cluster %s (status=%s): %s",
            caller.cluster_id,
            error.status_code,
            reason,
        )
        task = _failed_task(task_id, context_id, reason)
        _store_task(task_id, caller.task_scope, task)
        return _jsonrpc_result(request_id, {"task": task})
    except Exception:  # noqa: BLE001 - never leak internals, always fail closed
        logger.exception("Unexpected error while processing A2A query")
        task = _failed_task(task_id, context_id, "An internal error occurred")
        _store_task(task_id, caller.task_scope, task)
        return _jsonrpc_result(request_id, {"task": task})

    task = _completed_task(task_id, context_id, summarizer_response.response)
    _store_task(task_id, caller.task_scope, task)
    return _jsonrpc_result(request_id, {"task": task})


async def _handle_get_task(
    request_id: Any, params: dict[str, Any], caller: CallerIdentity
) -> dict[str, Any]:
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id:
        return _jsonrpc_error(request_id, _INVALID_PARAMS, "params.id is required")
    task = _lookup_task(task_id, caller.task_scope)
    if task is None:
        return _jsonrpc_error(request_id, _TASK_NOT_FOUND, "Task not found")
    return _jsonrpc_result(request_id, {"task": task})


@router.post("/")
async def handle_rpc(
    request: Request, caller: CallerIdentity = Depends(a2a_auth.authenticate_request)
) -> JSONResponse:
    """Dispatch one authenticated A2A JSON-RPC request."""
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(_jsonrpc_error(None, _PARSE_ERROR, "Invalid JSON payload"))

    if not isinstance(body, dict):
        return JSONResponse(_jsonrpc_error(None, _INVALID_REQUEST, "Batch requests are not supported"))

    request_id = body.get("id")
    if not isinstance(request_id, (str, int)) and request_id is not None:
        request_id = None

    if body.get("jsonrpc") != _JSONRPC_VERSION:
        return JSONResponse(
            _jsonrpc_error(request_id, _INVALID_REQUEST, "jsonrpc must be '2.0'")
        )

    method = body.get("method")
    if not method:
        return JSONResponse(_jsonrpc_error(request_id, _INVALID_REQUEST, "method is required"))

    if _rejects_mcp_header_override(request, body):
        logger.warning(
            "Rejected A2A request from cluster %s: client-supplied MCP header override",
            caller.cluster_id,
        )
        return JSONResponse(
            _jsonrpc_error(
                request_id,
                _INVALID_PARAMS,
                "Client-supplied MCP credential overrides are not permitted",
            )
        )

    params = body.get("params") or {}
    if not isinstance(params, dict):
        return JSONResponse(_jsonrpc_error(request_id, _INVALID_PARAMS, "params must be an object"))

    if method in _SEND_MESSAGE_METHODS:
        result = await _handle_send_message(request_id, params, caller)
    elif method in _GET_TASK_METHODS:
        result = await _handle_get_task(request_id, params, caller)
    else:
        result = _jsonrpc_error(request_id, _METHOD_NOT_FOUND, f"Method {method} is not supported")

    return JSONResponse(result)
