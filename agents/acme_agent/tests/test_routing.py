"""Tests for the inbound cluster-routing enforcement and its concurrency
isolation property (T5.2/T5.3): a rejected request never reaches the inner
ADK app, and two interleaved conversations targeting different clusters
never cross-contaminate the X-OLS-Cluster header on their downstream A2A
RPCs (and never carry it at all on an unauthenticated agent-card fetch).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from acme_agent.auth import OLS_CLUSTER_HEADER, add_cluster_header
from acme_agent.cluster_registry import ClusterRegistry
from acme_agent.routing import ClusterRoutingMiddleware


def _registry() -> ClusterRegistry:
    return ClusterRegistry.from_mapping(
        {
            "api-cluster-a-example-com-6443": "https://api.cluster-a.example.com:6443",
            "api-cluster-b-example-com-6443": "https://api.cluster-b.example.com:6443",
        }
    )


def _message_send_body(request_id: str, text: str) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": request_id,
                    "role": "user",
                    "parts": [{"kind": "text", "text": text}],
                }
            },
        }
    ).encode("utf-8")


async def _send_http_request(app, body: bytes):
    """Drives one ASGI HTTP request through ``app``, returning (status, json)."""

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    chunks = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )
    payload = json.loads(chunks) if chunks else None
    return status, payload


def _rejecting_inner_app_and_calls():
    calls: list[int] = []

    async def inner_app(scope, receive, send) -> None:
        calls.append(1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    return inner_app, calls


# ---------------------------------------------------------------------------
# Rejection: no downstream call ever happens
# ---------------------------------------------------------------------------


def test_rejects_missing_url_without_invoking_inner_app():
    inner_app, calls = _rejecting_inner_app_and_calls()
    app = ClusterRoutingMiddleware(inner_app, _registry())
    body = _message_send_body("1", "please check pods in payments")

    status, payload = asyncio.run(_send_http_request(app, body))

    assert not calls
    assert status == 400
    assert payload["error"]["code"] == -32001


def test_rejects_unknown_url_without_invoking_inner_app():
    inner_app, calls = _rejecting_inner_app_and_calls()
    app = ClusterRoutingMiddleware(inner_app, _registry())
    body = _message_send_body("1", "https://api.unregistered.example.com:6443 please")

    status, payload = asyncio.run(_send_http_request(app, body))

    assert not calls
    assert status == 400


def test_rejects_ambiguous_url_without_invoking_inner_app():
    inner_app, calls = _rejecting_inner_app_and_calls()
    app = ClusterRoutingMiddleware(inner_app, _registry())
    body = _message_send_body(
        "1",
        "https://api.cluster-a.example.com:6443 or "
        "https://api.cluster-b.example.com:6443?",
    )

    status, payload = asyncio.run(_send_http_request(app, body))

    assert not calls
    assert status == 400


def test_rejects_malformed_url_without_invoking_inner_app():
    inner_app, calls = _rejecting_inner_app_and_calls()
    app = ClusterRoutingMiddleware(inner_app, _registry())
    body = _message_send_body("1", "target http://api.cluster-a.example.com:6443 (not https)")

    status, _payload = asyncio.run(_send_http_request(app, body))

    assert not calls
    assert status == 400


def test_non_dispatch_method_passes_through_untouched():
    inner_app, calls = _rejecting_inner_app_and_calls()
    app = ClusterRoutingMiddleware(inner_app, _registry())
    body = json.dumps({"jsonrpc": "2.0", "id": "1", "method": "tasks/get", "params": {}}).encode()

    status, _payload = asyncio.run(_send_http_request(app, body))

    assert calls == [1]
    assert status == 200


def test_get_requests_bypass_the_middleware_entirely():
    inner_app, calls = _rejecting_inner_app_and_calls()
    app = ClusterRoutingMiddleware(inner_app, _registry())

    async def run():
        scope = {"type": "http", "method": "GET", "path": "/.well-known/agent-card.json"}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        messages = []

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)
        return messages

    messages = asyncio.run(run())
    assert calls == [1]
    assert messages[0]["status"] == 200


# ---------------------------------------------------------------------------
# Valid dispatch: downstream header isolation under concurrency
# ---------------------------------------------------------------------------


def _make_inner_app(client: httpx.AsyncClient, delay_before: float, delay_after: float):
    async def inner_app(scope, receive, send) -> None:
        await asyncio.sleep(delay_before)
        # Simulated public, unauthenticated agent-card fetch: must never
        # carry the routing header, regardless of the bound cluster id.
        await client.get("https://praxis.example.test/.well-known/agent-card.json")
        await asyncio.sleep(delay_after)
        # Simulated authenticated A2A RPC to Praxis.
        await client.post("https://praxis.example.test/", json={"ping": True})
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    return inner_app


def test_valid_dispatch_attaches_header_and_card_fetch_has_none():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [add_cluster_header]},
    )
    app = ClusterRoutingMiddleware(_make_inner_app(client, 0.0, 0.0), _registry())
    body = _message_send_body("1", "check pods on https://api.cluster-a.example.com:6443")

    async def run():
        status, _ = await _send_http_request(app, body)
        await client.aclose()
        return status

    status = asyncio.run(run())

    assert status == 200
    card_request, rpc_request = captured
    assert OLS_CLUSTER_HEADER not in card_request.headers
    assert rpc_request.headers[OLS_CLUSTER_HEADER] == "api-cluster-a-example-com-6443"


def test_two_interleaved_conversations_do_not_cross_contaminate_headers():
    """The concurrency test required by T5.3: two simulated conversations
    targeting two different registered clusters, interleaved via asyncio,
    must never see each other's X-OLS-Cluster id -- and neither ever
    attaches the header to the shared client's agent-card fetch.
    """

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    # One shared httpx.AsyncClient (and one shared event hook), exactly as
    # agent.py wires HTTP_CLIENT across every RemoteA2aAgent -- the scenario
    # a process-global header would get wrong.
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [add_cluster_header]},
    )

    registry = _registry()
    app_a = ClusterRoutingMiddleware(_make_inner_app(client, 0.01, 0.03), registry)
    app_b = ClusterRoutingMiddleware(_make_inner_app(client, 0.02, 0.01), registry)

    body_a = _message_send_body(
        "a", "investigate crashing pods on https://api.cluster-a.example.com:6443"
    )
    body_b = _message_send_body(
        "b", "investigate crashing pods on https://api.cluster-b.example.com:6443"
    )

    async def run():
        results = await asyncio.gather(
            _send_http_request(app_a, body_a),
            _send_http_request(app_b, body_b),
        )
        await client.aclose()
        return results

    (status_a, _), (status_b, _) = asyncio.run(run())
    assert status_a == 200
    assert status_b == 200

    card_requests = [r for r in captured if r.method == "GET"]
    rpc_requests = [r for r in captured if r.method == "POST"]

    assert len(card_requests) == 2
    assert len(rpc_requests) == 2

    # The card fetch must never carry the routing header for either
    # invocation -- it must stay discoverable before/without a cluster.
    for request in card_requests:
        assert OLS_CLUSTER_HEADER not in request.headers

    # Each authenticated RPC must carry exactly its own invocation's
    # cluster id -- no cross-talk despite running concurrently on one
    # shared client with one shared request hook.
    headers = sorted(r.headers.get(OLS_CLUSTER_HEADER) for r in rpc_requests)
    assert headers == [
        "api-cluster-a-example-com-6443",
        "api-cluster-b-example-com-6443",
    ]
