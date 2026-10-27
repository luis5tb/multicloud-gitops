"""Tests for transparent Token A renewal on downstream A2A calls."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx
import pytest

from acme_agent.auth import (
    AuthSelection,
    AuthSelectionMiddleware,
    AuthSettings,
    DownstreamAuth,
    TokenOverride,
    _auth_selection,
    _token_override,
)
from acme_agent.renewal import RENEWAL_MESSAGE_TEXT, CredentialRenewingTransport

KEYCLOAK_URL = "https://keycloak.example.test/token"
STAGE_URL = "https://sso.stage.example.test/token"
MAS_URL = "https://mas.example.test/a2a/jsonrpc"


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> AuthSettings:
    monkeypatch.setenv("A2A_AUTH_MODE", "keycloak")
    monkeypatch.setenv("A2A_AUTH_PROVIDER", "keycloak")
    monkeypatch.setenv("KEYCLOAK_TOKEN_URL", KEYCLOAK_URL)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "acme-agent")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "kc-secret")
    monkeypatch.setenv("KEYCLOAK_CLIENT_AUTH_METHOD", "client_secret_post")
    monkeypatch.setenv("REDHAT_SSO_STAGE_TOKEN_URL", STAGE_URL)
    monkeypatch.setenv("REDHAT_SSO_STAGE_CLIENT_ID", "acme-stage")
    monkeypatch.setenv("REDHAT_SSO_STAGE_CLIENT_SECRET", "stage-secret")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return AuthSettings.from_env()


class TokenIssuer:
    """Fake Keycloak / Red Hat SSO token endpoints issuing numbered tokens."""

    def __init__(self) -> None:
        self.issued: list[str] = []
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(503, json={"error": "temporarily_unavailable"})
        prefix = "stage" if str(request.url) == STAGE_URL else "kc"
        token = f"{prefix}-{sum(t.startswith(prefix) for t in self.issued) + 1}"
        self.issued.append(token)
        return httpx.Response(200, json={"access_token": token, "expires_in": 600})


class Downstream:
    """Scripted downstream A2A agent recording every request it receives."""

    def __init__(self, *responses: Callable[[httpx.Request], httpx.Response]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)(request)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]

    def tokens(self) -> list[str]:
        return [request.headers.get("Authorization", "") for request in self.requests]


def _sse(*results: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    def respond(_request: httpx.Request) -> httpx.Response:
        content = b"".join(
            b"data: " + json.dumps({"jsonrpc": "2.0", "id": "1", "result": r}).encode() + b"\n\n"
            for r in results
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    return respond


def _json(result: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(
        200, json={"jsonrpc": "2.0", "id": "1", "result": result}
    )


def _status(task_id: str, state: str, text: str = "") -> dict[str, Any]:
    status: dict[str, Any] = {"state": state}
    if text:
        status["message"] = {"role": "ROLE_AGENT", "messageId": "m", "parts": [{"text": text}]}
    return {"statusUpdate": {"taskId": task_id, "contextId": "ctx-1", "status": status}}


def _send_body(method: str = "SendStreamingMessage") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": method,
        "params": {
            "message": {
                "messageId": "user-msg-1",
                "role": "ROLE_USER",
                "parts": [{"text": "Investigate the cluster"}],
                "metadata": {"ols_mode": "troubleshooting"},
            }
        },
    }


def _client(auth: DownstreamAuth, downstream: Downstream, max_renewals: int = 2):
    return httpx.AsyncClient(
        transport=CredentialRenewingTransport(
            httpx.MockTransport(downstream.handler), auth, max_renewals=max_renewals
        ),
        event_hooks={"request": [auth.add_auth]},
    )


def _auth(monkeypatch: pytest.MonkeyPatch, issuer: TokenIssuer, **env: str) -> DownstreamAuth:
    auth = DownstreamAuth(_settings(monkeypatch, **env))
    asyncio.run(auth._token_client.aclose())
    auth._token_client = httpx.AsyncClient(transport=httpx.MockTransport(issuer.handler))
    return auth


async def _stream_results(client: httpx.AsyncClient, body: dict[str, Any]) -> list[dict]:
    results = []
    async with client.stream(
        "POST", MAS_URL, json=body, headers={"X-Request-Id": "rid-1"}
    ) as response:
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                event, buffer = buffer.split("\n\n", 1)
                results.append(json.loads(event.removeprefix("data: "))["result"])
    return results


def test_stream_pause_is_renewed_and_spliced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resume a paused downstream task with a fresh token; ADK never sees the pause."""
    issuer = TokenIssuer()
    auth = _auth(monkeypatch, issuer)
    downstream = Downstream(
        _sse(
            _status("task-1", "TASK_STATE_WORKING"),
            _status("task-1", "TASK_STATE_AUTH_REQUIRED", "OLS needs fresh credentials"),
        ),
        _sse(_status("task-1", "TASK_STATE_COMPLETED", "All checks passed")),
    )

    async def run() -> list[dict]:
        async with _client(auth, downstream) as client:
            return await _stream_results(client, _send_body())

    results = asyncio.run(run())

    states = [r["statusUpdate"]["status"]["state"] for r in results]
    assert states == ["TASK_STATE_WORKING", "TASK_STATE_COMPLETED"]
    assert downstream.tokens() == ["Bearer kc-1", "Bearer kc-2"]
    follow_up = downstream.bodies()[1]
    assert follow_up["method"] == "SendStreamingMessage"
    message = follow_up["params"]["message"]
    assert message["taskId"] == "task-1"
    assert message["contextId"] == "ctx-1"
    assert message["role"] == "ROLE_USER"
    assert message["parts"] == [{"text": RENEWAL_MESSAGE_TEXT}]
    assert message["messageId"] != "user-msg-1"
    assert message["metadata"] == {"ols_mode": "troubleshooting"}
    assert downstream.requests[1].headers["X-Request-Id"] == "rid-1"


def test_json_pause_is_renewed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-streaming send that returns an auth-required task is resumed."""
    issuer = TokenIssuer()
    auth = _auth(monkeypatch, issuer)
    downstream = Downstream(
        _json({"task": {"id": "task-9", "contextId": "ctx-9",
                        "status": {"state": "TASK_STATE_AUTH_REQUIRED"}}}),
        _json({"task": {"id": "task-9", "contextId": "ctx-9",
                        "status": {"state": "TASK_STATE_COMPLETED"}}}),
    )

    async def run() -> dict:
        async with _client(auth, downstream) as client:
            response = await client.post(MAS_URL, json=_send_body("SendMessage"))
            return response.json()

    payload = asyncio.run(run())

    assert payload["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert downstream.tokens() == ["Bearer kc-1", "Bearer kc-2"]
    assert downstream.bodies()[1]["params"]["message"]["taskId"] == "task-9"


def test_http_401_resends_the_original_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected expired token is replaced and the same request resent."""
    issuer = TokenIssuer()
    auth = _auth(monkeypatch, issuer)
    downstream = Downstream(
        lambda _r: httpx.Response(401, json={"error": {"message": "expired"}}),
        _json({"task": {"id": "task-2", "status": {"state": "TASK_STATE_COMPLETED"}}}),
    )

    async def run() -> int:
        async with _client(auth, downstream) as client:
            response = await client.post(MAS_URL, json=_send_body("SendMessage"))
            return response.status_code

    assert asyncio.run(run()) == 200
    assert downstream.tokens() == ["Bearer kc-1", "Bearer kc-2"]
    first, second = downstream.bodies()
    assert first == second


def test_renewal_uses_the_requests_red_hat_sso_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task started with a stage token is renewed with a stage token."""
    issuer = TokenIssuer()
    auth = _auth(monkeypatch, issuer)
    downstream = Downstream(
        _sse(_status("task-3", "TASK_STATE_AUTH_REQUIRED")),
        _sse(_status("task-3", "TASK_STATE_COMPLETED")),
    )

    async def run() -> None:
        selection = _auth_selection.set(AuthSelection("redhat", "stage", "agent"))
        try:
            async with _client(auth, downstream) as client:
                await _stream_results(client, _send_body())
        finally:
            _auth_selection.reset(selection)

    asyncio.run(run())

    assert downstream.tokens() == ["Bearer stage-1", "Bearer stage-2"]
    assert issuer.issued == ["stage-1", "stage-2"]


def test_pasted_token_is_used_once_then_renewed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pasted override serves the first call; renewal mints a real token."""
    issuer = TokenIssuer()
    auth = _auth(monkeypatch, issuer, ACME_ALLOW_TOKEN_OVERRIDE="true")
    downstream = Downstream(
        _sse(_status("task-4", "TASK_STATE_AUTH_REQUIRED")),
        _sse(_status("task-4", "TASK_STATE_COMPLETED")),
    )
    override = TokenOverride(token="pasted-token")

    async def run() -> str:
        token = _token_override.set(override)
        try:
            async with _client(auth, downstream) as client:
                await _stream_results(client, _send_body())
            return await auth.bearer_token()
        finally:
            _token_override.reset(token)

    later = asyncio.run(run())

    assert downstream.tokens() == ["Bearer pasted-token", "Bearer kc-1"]
    assert override.active is False
    assert later == "kc-1"


def test_renewals_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the limit, the auth-required event reaches the caller."""
    issuer = TokenIssuer()
    auth = _auth(monkeypatch, issuer)
    paused = _status("task-5", "TASK_STATE_AUTH_REQUIRED")
    downstream = Downstream(_sse(paused), _sse(paused), _sse(paused))

    async def run() -> list[dict]:
        async with _client(auth, downstream, max_renewals=2) as client:
            return await _stream_results(client, _send_body())

    results = asyncio.run(run())

    assert len(downstream.requests) == 3
    assert results[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"


def test_failed_mint_passes_the_pause_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """If no fresh token can be minted, the caller sees auth-required."""
    issuer = TokenIssuer()
    auth = _auth(monkeypatch, issuer)
    downstream = Downstream(_sse(_status("task-6", "TASK_STATE_AUTH_REQUIRED")))

    async def run() -> list[dict]:
        async with _client(auth, downstream) as client:
            await auth.bearer_token()  # warm the cache with kc-1
            issuer.fail = True
            return await _stream_results(client, _send_body())

    results = asyncio.run(run())

    assert len(downstream.requests) == 1
    assert results[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"


def test_legacy_protocol_follow_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """A2A 0.3 streams use kind-tagged events and a legacy message shape."""
    issuer = TokenIssuer()
    auth = _auth(monkeypatch, issuer)
    downstream = Downstream(
        _sse({"kind": "status-update", "taskId": "task-7", "contextId": "ctx-7",
              "status": {"state": "auth-required"}, "final": True}),
        _sse({"kind": "status-update", "taskId": "task-7", "contextId": "ctx-7",
              "status": {"state": "completed"}, "final": True}),
    )
    body = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "message/stream",
        "params": {"message": {"kind": "message", "messageId": "m1", "role": "user",
                               "parts": [{"kind": "text", "text": "hi"}]}},
    }

    async def run() -> list[dict]:
        async with _client(auth, downstream) as client:
            return await _stream_results(client, body)

    results = asyncio.run(run())

    assert [r["status"]["state"] for r in results] == ["completed"]
    message = downstream.bodies()[1]["params"]["message"]
    assert message["role"] == "user"
    assert message["kind"] == "message"
    assert message["parts"] == [{"kind": "text", "text": RENEWAL_MESSAGE_TEXT}]
    assert message["taskId"] == "task-7"


def test_non_send_requests_are_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent-card fetches and task reads pass straight through."""
    issuer = TokenIssuer()
    auth = _auth(monkeypatch, issuer)
    downstream = Downstream(
        lambda _r: httpx.Response(401),
        _json({"id": "task-8", "status": {"state": "TASK_STATE_AUTH_REQUIRED"}}),
    )

    async def run() -> tuple[int, dict]:
        async with _client(auth, downstream) as client:
            card = await client.get("https://mas.example.test/.well-known/agent-card.json")
            task = await client.post(
                MAS_URL,
                json={"jsonrpc": "2.0", "id": "g", "method": "GetTask", "params": {"id": "task-8"}},
            )
            return card.status_code, task.json()

    status, task = asyncio.run(run())

    assert status == 401
    assert task["result"]["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    assert len(downstream.requests) == 2


def _middleware_call(settings: AuthSettings, headers: list[tuple[bytes, bytes]]):
    seen: dict[str, Any] = {}

    async def app(scope, receive, send) -> None:
        seen["override"] = _token_override.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    asyncio.run(AuthSelectionMiddleware(app, settings)(scope, receive, send))
    return sent[0]["status"], seen.get("override")


def test_override_header_requires_the_testing_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject pasted tokens unless ACME_ALLOW_TOKEN_OVERRIDE is enabled."""
    status, override = _middleware_call(
        _settings(monkeypatch), [(b"x-acme-auth-token", b"pasted-token")]
    )

    assert status == 400
    assert override is None


def test_override_header_is_bound_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind a pasted token to the request when the testing flag is on."""
    settings = _settings(monkeypatch, ACME_ALLOW_TOKEN_OVERRIDE="true")
    status, override = _middleware_call(settings, [(b"x-acme-auth-token", b"pasted-token")])

    assert status == 200
    assert override == TokenOverride(token="pasted-token")
    assert settings.ui_options()["tokenOverrideEnabled"] is True
