"""Tests for request-scoped downstream authentication."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from acme_agent.auth import (
    AuthSelection,
    AuthSelectionMiddleware,
    AuthSettings,
    DownstreamAuth,
    _auth_selection,
    _token_debug_id,
)


def _settings(monkeypatch: pytest.MonkeyPatch) -> AuthSettings:
    monkeypatch.setenv("A2A_AUTH_MODE", "keycloak")
    monkeypatch.setenv("A2A_AUTH_PROVIDER", "keycloak")
    monkeypatch.setenv("REDHAT_SSO_SCOPE", "openid api.ask_red_hat")
    monkeypatch.setenv("REDHAT_SSO_STAGE_TOKEN_URL", "https://sso.example.test/token")
    monkeypatch.setenv("REDHAT_SSO_STAGE_CLIENT_ID", "acme-stage")
    monkeypatch.setenv("REDHAT_SSO_STAGE_CLIENT_SECRET", "stage-secret")
    monkeypatch.setenv("AUTH_TOKEN_DEBUG_ENABLED", "true")
    return AuthSettings.from_env()


def test_redhat_agent_token_uses_client_credentials_and_debug_is_single_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    auth = DownstreamAuth(settings)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"access_token": "stage-token", "expires_in": 600})

    async def run() -> None:
        await auth._token_client.aclose()
        auth._token_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        selection = _auth_selection.set(
            AuthSelection(provider="redhat", environment="stage", token_type="agent")
        )
        debug = _token_debug_id.set("private-debug-nonce")
        try:
            assert await auth.bearer_token() == "stage-token"
            assert auth.pop_debug_token("private-debug-nonce") == {
                "accessToken": "stage-token",
                "provider": "redhat",
                "environment": "stage",
                "tokenType": "agent",
            }
            assert auth.pop_debug_token("private-debug-nonce") is None
        finally:
            _token_debug_id.reset(debug)
            _auth_selection.reset(selection)
        await auth.close()

    asyncio.run(run())
    assert len(requests) == 1
    form = parse_qs(requests[0].content.decode())
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == ["acme-stage"]
    assert form["client_secret"] == ["stage-secret"]
    assert form["scope"] == ["openid api.ask_red_hat"]


def test_redhat_user_token_is_explicitly_unimplemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = DownstreamAuth(_settings(monkeypatch))
    selection = _auth_selection.set(
        AuthSelection(provider="redhat", environment="production", token_type="user")
    )
    try:
        with pytest.raises(NotImplementedError, match="user tokens"):
            asyncio.run(auth.bearer_token())
    finally:
        _auth_selection.reset(selection)


def test_redhat_token_error_surfaces_oauth_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = DownstreamAuth(_settings(monkeypatch))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": "unauthorized_client",
                "error_description": "Client not enabled to retrieve service account",
            },
        )

    async def run() -> None:
        await auth._token_client.aclose()
        auth._token_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        selection = _auth_selection.set(
            AuthSelection(provider="redhat", environment="stage", token_type="agent")
        )
        try:
            with pytest.raises(
                RuntimeError,
                match="Client not enabled to retrieve service account",
            ):
                await auth.bearer_token()
        finally:
            _auth_selection.reset(selection)
        await auth.close()

    asyncio.run(run())


def test_middleware_rejects_user_token_before_the_agent_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)
    called = False

    async def inner_app(scope, receive, send) -> None:
        nonlocal called
        called = True

    async def run() -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "method": "POST",
            "headers": [
                (b"x-acme-auth-provider", b"redhat"),
                (b"x-acme-auth-environment", b"stage"),
                (b"x-acme-auth-token-type", b"user"),
            ],
        }
        await AuthSelectionMiddleware(inner_app, settings)(scope, receive, send)
        return messages

    messages = asyncio.run(run())
    assert not called
    assert messages[0]["status"] == 400
    assert b"user tokens are not implemented" in messages[1]["body"]
