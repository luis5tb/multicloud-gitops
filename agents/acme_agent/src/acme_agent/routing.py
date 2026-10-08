"""ASGI middleware enforcing the target-cluster contract on inbound A2A calls.

Per the Lightspeed migration plan, ``agents/acme_agent/src/acme_agent/ui.py``
sends one-shot A2A requests with no persisted conversation/context id, so a
prior clarification can never be "remembered" -- every ``message/send`` (and
``message/stream``) call must carry its own valid, registered OpenShift
cluster API URL. This middleware is the deterministic, server-side gate in
front of the ADK app: it is not a substitute for the root agent's system
prompt (see ``agent.py``), it is the enforcement that does not trust it.

On a valid, registered URL, the resolved cluster id is bound into
``cluster_context`` for the lifetime of the request so every downstream A2A
RPC fired while handling it (see ``auth.add_cluster_header``) carries
``X-OLS-Cluster`` -- without ever touching the shared ``httpx.AsyncClient``'s
default headers. On a missing, ambiguous, malformed, or unregistered URL,
the request is rejected here, as a JSON-RPC error, and the ADK app (and so
any downstream RPC) is never invoked at all.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .cluster_context import cluster_id_scope
from .cluster_registry import ClusterRegistry, ClusterURLError, resolve_cluster_id_from_text

# Only these JSON-RPC methods start a new remote dispatch for which a target
# cluster must be (re)validated; task lookups/cancellations for a task that
# was already created carry no message text to validate and are let through
# unchanged -- ADK completes an initial message/send (including any internal
# polling) synchronously within the one request this middleware scopes, so a
# single bind already covers that case for the current one-shot UI.
_DISPATCH_METHODS = frozenset({"message/send", "message/stream"})

_MISSING_CLUSTER_RPC_CODE = -32001


class ClusterRoutingMiddleware:
    """Require and validate a target OpenShift cluster URL on every inbound
    A2A dispatch, binding the resolved id to this request's context.
    """

    def __init__(self, app: ASGIApp, registry: ClusterRegistry) -> None:
        self.app = app
        self.registry = registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)

        # Replay the buffered body exactly once, then defer to the real
        # `receive`. Returning the body on *every* call deadlocks streaming
        # (message/stream) responses: the SSE layer runs a "listen for client
        # disconnect" loop that awaits receive() expecting to block until an
        # http.disconnect, but a replay that always returns the body instantly
        # turns that into a busy-loop that pegs the single event loop -- which
        # starves /healthz and gets the pod liveness-killed mid-request. After
        # the body, the original receive() yields the genuine disconnect event.
        _body_replayed = False

        async def _replay_receive() -> dict[str, Any]:
            nonlocal _body_replayed
            if not _body_replayed:
                _body_replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        outcome = self._validate(body)
        if isinstance(outcome, JSONResponse):
            await outcome(scope, receive, send)
            return
        if outcome is None:
            # Not a dispatch call (e.g. a task lookup, or an unparseable/
            # non-JSON-RPC body the inner app will reject on its own terms).
            await self.app(scope, _replay_receive, send)
            return

        with cluster_id_scope(outcome):
            await self.app(scope, _replay_receive, send)

    def _validate(self, body: bytes) -> JSONResponse | str | None:
        """Returns a resolved cluster id, a rejection response, or ``None``.

        ``None`` means "not a message dispatch this middleware governs" --
        the request is passed through untouched.
        """

        if not body:
            return None
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or payload.get("method") not in _DISPATCH_METHODS:
            return None

        request_id = payload.get("id")
        text = _message_text(payload)
        try:
            return resolve_cluster_id_from_text(self.registry, text)
        except ClusterURLError as error:
            return _rpc_rejection(request_id, str(error))


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    more_body = True
    while more_body:
        message = await receive()
        chunks.append(message.get("body", b""))
        more_body = message.get("more_body", False)
    return b"".join(chunks)


def _message_text(payload: dict[str, Any]) -> str:
    params = payload.get("params")
    message = params.get("message") if isinstance(params, dict) else None
    parts = message.get("parts") if isinstance(message, dict) else None
    if not isinstance(parts, list):
        return ""
    texts = [
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return "\n".join(texts)


def _rpc_rejection(request_id: Any, detail: str) -> JSONResponse:
    """A clarification/rejection response shaped like a JSON-RPC reply.

    Returned instead of forwarding to the ADK app, so no downstream A2A RPC
    is ever made for a request that fails cluster validation.
    """

    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": _MISSING_CLUSTER_RPC_CODE,
                "message": (
                    "State exactly one explicit, registered OpenShift "
                    "cluster API URL (for example "
                    "https://api.example.com:6443) to proceed."
                ),
                "data": detail,
            },
        },
        status_code=400,
    )
