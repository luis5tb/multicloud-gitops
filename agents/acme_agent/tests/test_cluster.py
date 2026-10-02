import asyncio
import json

import httpx
import pytest

from acme_agent.cluster import (
    CLUSTER_HEADER,
    MAX_INBOUND_BODY_BYTES,
    ClusterRegistry,
    ClusterRegistryError,
    ClusterSelectionError,
    ClusterSelectionMiddleware,
    add_cluster_header,
    cluster_registry_from_env,
    downstream_request_hooks,
    normalize_api_url,
)
from acme_agent.ui import render_ui


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" HTTPS://API.Example.Test:6443 ", "https://api.example.test:6443"),
        ("https://api.example.test:443", "https://api.example.test"),
        ("https://api.example.test.", "https://api.example.test"),
        ("https://[2001:0db8::1]:6443", "https://[2001:db8::1]:6443"),
        ("https://bücher.example:6443", "https://xn--bcher-kva.example:6443"),
    ],
)
def test_normalize_api_url(value, expected):
    assert normalize_api_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://api.example.test:6443",
        "https://user:secret@api.example.test:6443",
        "https://api.example.test:6443/",
        "https://api.example.test:6443/version",
        "https://api.example.test:6443?token=value",
        "https://api.example.test:6443#fragment",
        "https://api.example.test:",
        "https://api.example.test:70000",
        "https://[fe80::1%25eth0]:6443",
    ],
)
def test_normalize_api_url_rejects_non_origin_urls(value):
    with pytest.raises(ValueError):
        normalize_api_url(value)


def test_registry_resolves_canonical_url_to_stable_id():
    registry = ClusterRegistry({"east-cluster": "https://API.EAST.example:6443"})

    target = registry.resolve("HTTPS://api.east.example:6443")

    assert target.cluster_id == "east-cluster"
    assert target.api_url == "https://api.east.example:6443"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.unknown.example:6443",
        "http://api.east.example:6443",
        "https://api.east.example:6443/path",
    ],
)
def test_registry_rejects_invalid_or_unregistered_urls(url):
    registry = ClusterRegistry({"east-cluster": "https://api.east.example:6443"})

    with pytest.raises(ClusterSelectionError):
        registry.resolve(url)


def test_registry_rejects_duplicate_canonical_urls():
    with pytest.raises(ClusterRegistryError, match="multiple IDs"):
        ClusterRegistry(
            {
                "east": "https://api.east.example:443",
                "also-east": "HTTPS://API.EAST.EXAMPLE",
            }
        )


def test_registry_requires_non_empty_configuration():
    with pytest.raises(ClusterRegistryError, match="non-empty"):
        ClusterRegistry.from_env("{}")


def test_disabled_cluster_selection_does_not_require_or_parse_registry(monkeypatch):
    monkeypatch.setenv("OLS_CLUSTER_REGISTRY_JSON", "not-json")

    assert cluster_registry_from_env(enabled=False) is None


def test_enabled_cluster_selection_fails_closed_without_registry(monkeypatch):
    monkeypatch.delenv("OLS_CLUSTER_REGISTRY_JSON", raising=False)

    with pytest.raises(ClusterRegistryError, match="must configure"):
        cluster_registry_from_env(enabled=True)


def test_header_hook_is_only_installed_when_cluster_selection_is_enabled():
    async def auth_hook(request):
        return None

    assert downstream_request_hooks(auth_hook, False) == [auth_hook]
    assert downstream_request_hooks(auth_hook, True) == [auth_hook, add_cluster_header]


def test_disabled_downstream_client_keeps_auth_without_cluster_header():
    async def run_test():
        sent = []

        async def auth_hook(request):
            request.headers["Authorization"] = "Bearer test-token"

        async def transport_handler(request):
            sent.append(request.headers)
            return httpx.Response(200)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport_handler),
            event_hooks={
                "request": downstream_request_hooks(auth_hook, False)
            },
        ) as client:
            await client.post("https://praxis.example/a2a", json={"method": "message/send"})

        assert sent[0]["Authorization"] == "Bearer test-token"
        assert CLUSTER_HEADER not in sent[0]

    asyncio.run(run_test())


def test_ui_selection_field_is_gated_with_the_feature():
    legacy_ui = render_ui(cluster_selection_enabled=False)
    enabled_ui = render_ui(cluster_selection_enabled=True)

    assert "const clusterSelectionEnabled = false" in legacy_ui
    assert "const clusterSelectionEnabled = true" in enabled_ui
    assert "Requests are forwarded to the configured downstream A2A agent." in legacy_ui
    assert "Include the OpenShift API URL on every request." in enabled_ui


def test_message_selection_requires_one_registered_url():
    registry = ClusterRegistry({"east": "https://api.east.example:6443"})
    message = {
        "parts": [
            {
                "kind": "text",
                "text": "Check the cluster https://API.EAST.example:6443.",
            }
        ]
    }

    assert registry.resolve_message(message).cluster_id == "east"

    with pytest.raises(ClusterSelectionError, match="required"):
        registry.resolve_message({"parts": [{"text": "Check the cluster."}]})
    with pytest.raises(ClusterSelectionError, match="not registered"):
        registry.resolve_message(
            {"parts": [{"text": "Check https://api.unknown.example:6443"}]}
        )
    with pytest.raises(ClusterSelectionError, match="exactly one"):
        registry.resolve_message(
            {
                "parts": [
                    {
                        "text": "Compare https://api.east.example:6443 and "
                        "https://api.west.example:6443"
                    }
                ]
            }
        )
    with pytest.raises(ClusterSelectionError, match="HTTPS"):
        registry.resolve_message({"parts": [{"text": "Use http://api.east.example"}]})


def test_message_selection_preserves_unwrapped_ipv6_host_bracket():
    registry = ClusterRegistry({"local-ipv6": "https://[2001:db8::1]"})

    target = registry.resolve_message(
        {"parts": [{"text": "Inspect https://[2001:db8::1]"}]}
    )

    assert target.cluster_id == "local-ipv6"


def test_message_selection_trims_wrapping_bracket_after_url():
    registry = ClusterRegistry({"east": "https://api.east.example:6443"})

    target = registry.resolve_message(
        {"parts": [{"text": "Inspect [https://api.east.example:6443]"}]}
    )

    assert target.cluster_id == "east"


def test_rpc_header_is_request_scoped_under_concurrent_requests():
    async def run_test():
        registry = ClusterRegistry(
            {
                "east": "https://api.east.example:6443",
                "west": "https://api.west.example:6443",
            }
        )
        observed_headers = []
        both_requests_inside_app = asyncio.Event()
        entered_app = 0

        async def transport_handler(request):
            observed_headers.append(request.headers.get(CLUSTER_HEADER))
            return httpx.Response(200, json={"ok": True})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(transport_handler),
            event_hooks={"request": [add_cluster_header]},
        )

        async def downstream_app(scope, receive, send):
            nonlocal entered_app
            entered_app += 1
            if entered_app == 2:
                both_requests_inside_app.set()
            await both_requests_inside_app.wait()
            for method in ("message/send", "tasks/get", "tasks/cancel"):
                await client.post("https://praxis.example/a2a", json={"method": method})
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = ClusterSelectionMiddleware(
            downstream_app,
            registry,
        )

        async def inbound_request(api_url, request_id):
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "message/send",
                "params": {
                    "message": {
                        "parts": [{"kind": "text", "text": f"Inspect {api_url}"}]
                    }
                },
            }
            request = {
                "type": "http.request",
                "body": json.dumps(payload).encode(),
                "more_body": False,
            }
            receive_calls = 0
            sent = []

            async def receive():
                nonlocal receive_calls
                receive_calls += 1
                return request if receive_calls == 1 else {"type": "http.disconnect"}

            async def send(message):
                sent.append(message)

            await middleware(
                {"type": "http", "method": "POST", "path": "/"},
                receive,
                send,
            )
            return sent[0]["status"]

        try:
            statuses = await asyncio.wait_for(
                asyncio.gather(
                    inbound_request("https://api.east.example:6443", "east-request"),
                    inbound_request("https://api.west.example:6443", "west-request"),
                ),
                timeout=3,
            )
        finally:
            await client.aclose()

        assert statuses == [200, 200]
        assert sorted(observed_headers) == ["east"] * 3 + ["west"] * 3

    asyncio.run(run_test())


def test_card_get_is_target_neutral_and_rpc_without_selection_fails_closed():
    async def run_test():
        seen = []

        async def transport_handler(request):
            seen.append((request.method, request.headers.get(CLUSTER_HEADER)))
            return httpx.Response(200)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport_handler),
            event_hooks={"request": [add_cluster_header]},
        ) as client:
            await client.get("https://praxis.example/.well-known/agent-card.json")
            with pytest.raises(RuntimeError, match="without a validated"):
                await client.post("https://praxis.example/a2a", json={})

        assert seen == [("GET", None)]

    asyncio.run(run_test())


def test_middleware_rejects_before_invoking_agent_for_missing_or_bad_target():
    async def run_test():
        registry = ClusterRegistry({"east": "https://api.east.example:6443"})
        invocations = 0

        async def downstream_app(scope, receive, send):
            nonlocal invocations
            invocations += 1

        middleware = ClusterSelectionMiddleware(downstream_app, registry)

        async def request(text):
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "request-1",
                    "method": "message/send",
                    "params": {"message": {"parts": [{"text": text}]}},
                }
            ).encode()
            sent = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                sent.append(message)

            await middleware(
                {"type": "http", "method": "POST", "path": "/"},
                receive,
                send,
            )
            return sent

        missing = await request("Please investigate the issue.")
        unknown = await request("Please investigate https://api.unknown.example:6443")

        assert missing[0]["status"] == unknown[0]["status"] == 400
        assert invocations == 0

    asyncio.run(run_test())


def test_middleware_rejects_oversized_body_before_invoking_agent():
    async def run_test():
        registry = ClusterRegistry({"east": "https://api.east.example:6443"})
        invocations = 0
        valid_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "large-request",
                "method": "message/send",
                "params": {
                    "message": {
                        "parts": [
                            {"text": "Inspect https://api.east.example:6443"}
                        ]
                    }
                },
            }
        ).encode()
        oversized_body = valid_body + b" " * (
            MAX_INBOUND_BODY_BYTES + 1 - len(valid_body)
        )

        async def downstream_app(scope, receive, send):
            nonlocal invocations
            invocations += 1

        middleware = ClusterSelectionMiddleware(downstream_app, registry)
        chunks = [
            oversized_body[: MAX_INBOUND_BODY_BYTES - 10],
            oversized_body[MAX_INBOUND_BODY_BYTES - 10 :],
        ]
        chunk_index = 0
        sent = []

        async def receive():
            nonlocal chunk_index
            chunk = chunks[chunk_index]
            chunk_index += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": chunk_index < len(chunks),
            }

        async def send(message):
            sent.append(message)

        await middleware(
            {"type": "http", "method": "POST", "path": "/"},
            receive,
            send,
        )

        response = json.loads(sent[1]["body"])
        assert sent[0]["status"] == 413
        assert "1 MiB" in response["error"]["message"]
        assert invocations == 0

    asyncio.run(run_test())


def test_middleware_accepts_body_at_limit_and_replays_complete_body():
    async def run_test():
        registry = ClusterRegistry({"east": "https://api.east.example:6443"})
        valid_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "at-limit",
                "method": "message/send",
                "params": {
                    "message": {
                        "parts": [
                            {"text": "Inspect https://api.east.example:6443"}
                        ]
                    }
                },
            }
        ).encode()
        body = valid_body + b" " * (MAX_INBOUND_BODY_BYTES - len(valid_body))
        app_bodies = []
        sent = []

        async def downstream_app(scope, receive, send):
            request = await receive()
            app_bodies.append(request["body"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = ClusterSelectionMiddleware(
            downstream_app,
            registry,
        )
        chunks = [body[:123], body[123:MAX_INBOUND_BODY_BYTES]]
        chunk_index = 0

        async def receive():
            nonlocal chunk_index
            chunk = chunks[chunk_index]
            chunk_index += 1
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": chunk_index < len(chunks),
            }

        async def send(message):
            sent.append(message)

        await middleware(
            {"type": "http", "method": "POST", "path": "/"},
            receive,
            send,
        )

        assert sent[0]["status"] == 200
        assert app_bodies == [body]

    asyncio.run(run_test())


def test_middleware_does_not_invoke_agent_after_partial_body_disconnect():
    async def run_test():
        registry = ClusterRegistry({"east": "https://api.east.example:6443"})
        invocations = 0
        receive_messages = iter(
            [
                {
                    "type": "http.request",
                    "body": b'{"jsonrpc":"2.0","method":"message/send",',
                    "more_body": True,
                },
                {"type": "http.disconnect"},
            ]
        )

        async def downstream_app(scope, receive, send):
            nonlocal invocations
            invocations += 1

        async def receive():
            return next(receive_messages)

        async def send(message):
            raise AssertionError("disconnected peer must not receive a response")

        middleware = ClusterSelectionMiddleware(
            downstream_app,
            registry,
        )
        await middleware(
            {"type": "http", "method": "POST", "path": "/"},
            receive,
            send,
        )

        assert invocations == 0

    asyncio.run(run_test())


def test_middleware_explicitly_rejects_task_and_stream_methods():
    async def run_test():
        registry = ClusterRegistry({"east": "https://api.east.example:6443"})
        invocations = 0

        async def downstream_app(scope, receive, send):
            nonlocal invocations
            invocations += 1

        middleware = ClusterSelectionMiddleware(
            downstream_app,
            registry,
        )

        for method in ("tasks/get", "tasks/cancel", "message/stream"):
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": method,
                    "method": method,
                    "params": {"id": "task-without-target"},
                }
            ).encode()
            sent = []

            async def receive():
                return {
                    "type": "http.request",
                    "body": payload,
                    "more_body": False,
                }

            async def send(message):
                sent.append(message)

            await middleware(
                {"type": "http", "method": "POST", "path": "/"},
                receive,
                send,
            )

            response = json.loads(sent[1]["body"])
            assert sent[0]["status"] == 400
            assert response["error"]["code"] == -32601
            assert "only one-shot message/send" in response["error"]["message"]

        assert invocations == 0

    asyncio.run(run_test())
