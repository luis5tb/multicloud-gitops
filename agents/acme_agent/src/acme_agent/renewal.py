"""Automatic credential renewal for downstream A2A calls.

ACME authenticates downstream calls with its own workload token (Token A).
Long-running downstream work (for example the MAS orchestrator delegating to
OpenShift Lightspeed) can outlive that token. A downstream agent then either
rejects the request with HTTP 401, or pauses its task in
``TASK_STATE_AUTH_REQUIRED`` and waits for the caller to send a follow-up
message on the same task, authenticated with a fresh token.

ACME has no human credentials to ask for: it mints its own tokens. So this
transport renews transparently, below ADK:

* HTTP 401 on a send: mint a fresh token and resend the same request.
* A task (or stream event) in auth-required: mint a fresh token for the same
  identity provider the request started with, send a follow-up message on
  that task, and return the follow-up's response in place of the paused one.

ADK's ``RemoteA2aAgent`` never sees the pause. If renewal is impossible (no
token can be minted, or the renewal limit is reached), the original
auth-required response is passed through unchanged.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .auth import CapturedAuth, DownstreamAuth, token_fingerprint

logger = logging.getLogger(__name__)

# Text of the follow-up message. Agents that implement renewal (MAS, OLS)
# ignore the content: the fresh Authorization header is the renewal.
RENEWAL_MESSAGE_TEXT = "Fresh credentials are attached to this request. Continue the task."

DEFAULT_MAX_RENEWALS = 2

_SEND_METHODS = {
    "SendMessage": False,
    "SendStreamingMessage": True,
    "message/send": False,
    "message/stream": True,
}
_LEGACY_METHODS = {"message/send", "message/stream"}
_AUTH_REQUIRED_STATES = {"TASK_STATE_AUTH_REQUIRED", "auth-required", "auth_required"}
# Response headers that no longer describe a body this transport re-emits
# (httpx has already decoded it, and the length can change).
_STALE_BODY_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def _rpc_body(request: httpx.Request) -> dict[str, Any] | None:
    """Return the JSON-RPC body of an A2A send request, else ``None``."""
    if request.method != "POST":
        return None
    try:
        body = json.loads(request.content or b"")
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict) or body.get("method") not in _SEND_METHODS:
        return None
    message = (body.get("params") or {}).get("message")
    return body if isinstance(message, dict) else None


def _auth_required_task(result: Any) -> tuple[str, str] | None:
    """Return (task_id, context_id) when a JSON-RPC result is auth-required."""
    if not isinstance(result, dict):
        return None
    candidates = []
    for key in ("task", "statusUpdate", "status_update"):
        if isinstance(result.get(key), dict):
            candidates.append((key, result[key]))
    if result.get("kind") in {"task", "status-update"}:
        candidates.append((result["kind"], result))
    for kind, item in candidates:
        status = item.get("status")
        state = status.get("state") if isinstance(status, dict) else None
        if state not in _AUTH_REQUIRED_STATES:
            continue
        task_id = item.get("id") if kind == "task" else item.get("taskId")
        if not task_id and kind != "task":
            task_id = item.get("task_id")
        context_id = item.get("contextId") or item.get("context_id") or ""
        if isinstance(task_id, str) and task_id:
            return task_id, context_id if isinstance(context_id, str) else ""
    return None


def _follow_up_body(
    original: dict[str, Any], task_id: str, context_id: str
) -> dict[str, Any]:
    """Build a follow-up send on the paused task, preserving configuration."""
    params = dict(original.get("params") or {})
    previous = params.get("message") or {}
    legacy = original.get("method") in _LEGACY_METHODS
    message: dict[str, Any] = {
        "messageId": uuid.uuid4().hex,
        "role": "user" if legacy else "ROLE_USER",
        "parts": [
            {"kind": "text", "text": RENEWAL_MESSAGE_TEXT}
            if legacy
            else {"text": RENEWAL_MESSAGE_TEXT}
        ],
        "taskId": task_id,
    }
    if legacy:
        message["kind"] = "message"
    if context_id:
        message["contextId"] = context_id
    if isinstance(previous, dict) and previous.get("metadata"):
        message["metadata"] = previous["metadata"]
    params["message"] = message
    return {**original, "id": str(uuid.uuid4()), "params": params}


def _sse_events(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split complete SSE events off ``buffer``; return (events, remainder)."""
    normalized = buffer.replace(b"\r\n", b"\n")
    *events, remainder = normalized.split(b"\n\n")
    return [event + b"\n\n" for event in events], remainder


def _sse_payload(event: bytes) -> Any:
    """Return the decoded JSON-RPC payload of one SSE event, if any."""
    data = b"\n".join(
        line[5:].strip()
        for line in event.split(b"\n")
        if line.startswith(b"data:")
    )
    if not data:
        return None
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None


class _ReplayStream(httpx.AsyncByteStream):
    """Async byte stream driven by a generator, closing upstream on exit."""

    def __init__(self, chunks: AsyncIterator[bytes], on_close) -> None:
        self._chunks = chunks
        self._on_close = on_close

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        await self._on_close()


class CredentialRenewingTransport(httpx.AsyncBaseTransport):
    """Wrap a transport so downstream A2A sends renew expired credentials."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        auth: DownstreamAuth,
        *,
        max_renewals: int = DEFAULT_MAX_RENEWALS,
    ) -> None:
        self._inner = inner
        self._auth = auth
        self._max_renewals = max_renewals

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = _rpc_body(request)
        response = await self._inner.handle_async_request(request)
        if body is None or not self._auth.renewal_supported():
            return response
        # Captured now: the identity provider selection of the inbound request
        # that triggered this call, so renewals mint for the same principal.
        captured = self._auth.capture()
        return await self._settle(request, body, response, captured, renewals=0)

    async def _settle(
        self,
        request: httpx.Request,
        body: dict[str, Any],
        response: httpx.Response,
        captured: CapturedAuth,
        renewals: int,
    ) -> httpx.Response:
        if response.status_code == 401 and renewals < self._max_renewals:
            retry = await self._renewed_request(request, body, captured, "HTTP 401")
            if retry is not None:
                await response.aclose()
                follow_up = await self._inner.handle_async_request(retry)
                return await self._settle(
                    retry, body, follow_up, captured, renewals + 1
                )
            return response

        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return self._renewing_stream(request, body, response, captured, renewals)
        if "json" not in content_type or response.status_code >= 400:
            return response

        content = await response.aread()
        await response.aclose()
        replay = self._rebuild(response, content)
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeDecodeError):
            return replay
        paused = _auth_required_task(
            payload.get("result") if isinstance(payload, dict) else None
        )
        if paused is None or renewals >= self._max_renewals:
            return replay
        follow_up = await self._follow_up(request, body, paused, captured)
        if follow_up is None:
            return replay
        retry, retry_body = follow_up
        next_response = await self._inner.handle_async_request(retry)
        return await self._settle(
            retry, retry_body, next_response, captured, renewals + 1
        )

    def _renewing_stream(
        self,
        request: httpx.Request,
        body: dict[str, Any],
        response: httpx.Response,
        captured: CapturedAuth,
        renewals: int,
    ) -> httpx.Response:
        """Re-emit an SSE stream, splicing a renewed stream over an auth pause."""
        state = {"upstream": response}

        async def chunks() -> AsyncIterator[bytes]:
            current_request, current_body, count = request, body, renewals
            while True:
                upstream = state["upstream"]
                buffer = b""
                paused_event = None
                paused = None
                async for chunk in upstream.aiter_bytes():
                    events, buffer = _sse_events(buffer + chunk)
                    for event in events:
                        payload = _sse_payload(event)
                        found = _auth_required_task(
                            payload.get("result") if isinstance(payload, dict) else None
                        )
                        if found is not None and count < self._max_renewals:
                            paused_event, paused = event, found
                            break
                        yield event
                    if paused is not None:
                        break
                if paused is None:
                    if buffer:
                        yield buffer
                    return
                await upstream.aclose()
                follow_up = await self._follow_up(
                    current_request, current_body, paused, captured
                )
                if follow_up is None:
                    yield paused_event
                    return
                current_request, current_body = follow_up
                count += 1
                next_response = await self._inner.handle_async_request(current_request)
                if next_response.status_code >= 400 or "text/event-stream" not in (
                    next_response.headers.get("content-type", "")
                ):
                    logger.warning(
                        "acme_audit action=%s outcome=error status=%s",
                        '"resume downstream task with renewed Token A"',
                        next_response.status_code,
                    )
                    await next_response.aclose()
                    yield paused_event
                    return
                state["upstream"] = next_response

        async def close() -> None:
            await state["upstream"].aclose()

        headers = [
            (key, value)
            for key, value in response.headers.raw
            if key.decode("latin-1").lower() not in _STALE_BODY_HEADERS
        ]
        return httpx.Response(
            response.status_code,
            headers=headers,
            stream=_ReplayStream(chunks(), close),
            extensions=response.extensions,
        )

    async def _follow_up(
        self,
        request: httpx.Request,
        body: dict[str, Any],
        paused: tuple[str, str],
        captured: CapturedAuth,
    ) -> tuple[httpx.Request, dict[str, Any]] | None:
        task_id, context_id = paused
        follow_up_body = _follow_up_body(body, task_id, context_id)
        retry = await self._renewed_request(
            request, follow_up_body, captured, "task auth-required"
        )
        if retry is None:
            return None
        return retry, follow_up_body

    async def _renewed_request(
        self,
        request: httpx.Request,
        body: dict[str, Any],
        captured: CapturedAuth,
        reason: str,
    ) -> httpx.Request | None:
        """Build ``request`` again with ``body`` and a freshly minted token."""
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"authorization", "content-length"}
        }
        retry = httpx.Request(
            request.method,
            request.url,
            headers=headers,
            json=body,
            extensions=request.extensions,
        )
        try:
            await self._auth.reauthorize(retry, captured)
        except Exception as error:  # noqa: BLE001 -- surface the original pause
            logger.warning(
                "acme_audit action=%s outcome=error reason=%s error=%s",
                '"renew Token A"',
                reason,
                type(error).__name__,
            )
            return None
        # old → new fingerprints: the one line that proves a renewal actually
        # replaced the refused token (and retires a pasted override). Without
        # it, a 401 on a UI-pasted expired token is healed silently and the
        # token-debug view shows the renewed mint, leaving the operator
        # unable to tell whether the paste was ever sent (observed live
        # 2026-10-29 on bm-cluster).
        old_token = request.headers.get("Authorization", "")
        old_token = old_token[7:] if old_token.startswith("Bearer ") else old_token
        new_token = retry.headers.get("Authorization", "")
        new_token = new_token[7:] if new_token.startswith("Bearer ") else new_token
        logger.info(
            "acme_audit request_id=%s action=%s outcome=ok reason=%s "
            "renewed_from=%s token=%s source=minted override=%s",
            retry.headers.get("X-Request-Id", "-"),
            '"renew Token A"',
            reason,
            token_fingerprint(old_token),
            token_fingerprint(new_token),
            "retired" if captured.override is not None else "-",
        )
        return retry

    @staticmethod
    def _rebuild(response: httpx.Response, content: bytes) -> httpx.Response:
        headers = [
            (key, value)
            for key, value in response.headers.raw
            if key.decode("latin-1").lower() not in _STALE_BODY_HEADERS
        ]
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=content,
            extensions=response.extensions,
        )
