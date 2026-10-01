"""Integration-style tests for the A2A JSON-RPC endpoint.

Keycloak and the SPIFFE Workload API are mocked (see ``test_a2a_auth.py`` for
the auth primitives tested in isolation); here the full request path through
``ols.app.endpoints.a2a.handle_rpc`` -- including the real
``a2a_auth.authenticate_request`` FastAPI dependency -- is exercised with a
minimal Starlette app, and OLS's own query pipeline
(``ols.app.endpoints.ols.generate_response``) is replaced with a stub so no
real LLM/MCP call is made. The stub's captured arguments are asserted on to
prove the *exchanged* token (Token B), never the original caller's token
(Token A), is what would reach MCP.
"""

from __future__ import annotations

import json
import time
import types
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ols import config

# ols.app.endpoints.a2a imports ols.app.endpoints.ols, whose module-level
# auth_dependency construction (and ols.app.metrics' own) requires an
# authentication module to already be configured -- same requirement as
# tests/unit/app/endpoints/test_ols.py, same fix.
config.ols_config.authentication_config.module = "k8s"

from ols.app.endpoints import a2a, a2a_auth  # noqa:E402
from tests.unit.app.endpoints.test_a2a_auth import (  # noqa:E402  # reuse the same fakes/fixtures
    CLUSTER_ID,
    ISSUER,
    _FakeAsyncClient,
    _FakeResponse,
    make_settings,
    make_token,
)


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture()
def jwk(rsa_keypair):
    _, public_key = rsa_keypair
    raw = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    raw["kid"] = "test-kid-1"
    raw["use"] = "sig"
    raw["alg"] = "RS256"
    return raw


@pytest.fixture()
def client(monkeypatch, jwk):
    """A TestClient for a minimal app exposing only the A2A router.

    Installs a real ``A2AAuthenticator`` (built from in-memory settings, not
    the environment) as the process-wide singleton, and fakes the Keycloak
    HTTP calls and SPIFFE fetch so no real network/workload identity is
    needed.
    """
    settings = make_settings()
    authenticator = a2a_auth.A2AAuthenticator(settings)
    monkeypatch.setattr(a2a_auth, "_authenticator", authenticator)

    def responder(url: str, data: Optional[dict]) -> _FakeResponse:
        if url.endswith("/.well-known/openid-configuration"):
            return _FakeResponse(
                {
                    "issuer": ISSUER,
                    "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                }
            )
        if url.endswith("/protocol/openid-connect/certs"):
            return _FakeResponse({"keys": [jwk]})
        if url.endswith("/protocol/openid-connect/token"):
            return _FakeResponse({"access_token": "exchanged-token-b"})
        raise AssertionError(f"Unexpected URL requested in test: {url}")

    monkeypatch.setattr(
        a2a_auth.httpx, "AsyncClient", lambda **_kwargs: _FakeAsyncClient(responder)
    )
    monkeypatch.setattr(
        a2a_auth.SpiffeWorkloadIdentity, "fetch", AsyncMock(return_value="jwt-svid-value")
    )

    app = FastAPI()
    app.include_router(a2a.router)
    return TestClient(app)


def auth_headers(token: str, cluster: Optional[str] = CLUSTER_ID) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if cluster is not None:
        headers[a2a_auth.CLUSTER_HEADER_NAME] = cluster
    return headers


def send_message_body(text: str = "why is my pod crashlooping?", **overrides: Any) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": "req-1",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "msg-1",
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            }
        },
    }
    body.update(overrides)
    return body


class TestAgentCard:
    def test_agent_card_is_public(self, client):
        response = client.get(a2a.AGENT_CARD_PATH)
        assert response.status_code == 200
        card = response.json()
        assert card["supportedInterfaces"][0]["url"] == "https://praxis.example.com/"
        assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"


class TestAuthenticationEnforcement:
    def test_missing_token_is_rejected(self, client):
        response = client.post("/", json=send_message_body(), headers={a2a_auth.CLUSTER_HEADER_NAME: CLUSTER_ID})
        assert response.status_code == 401

    def test_invalid_token_is_rejected(self, client):
        response = client.post(
            "/",
            json=send_message_body(),
            headers=auth_headers("not-a-valid-jwt"),
        )
        assert response.status_code == 401

    def test_missing_cluster_header_is_rejected(self, rsa_keypair, client):
        token = make_token(rsa_keypair)
        response = client.post(
            "/", json=send_message_body(), headers=auth_headers(token, cluster=None)
        )
        assert response.status_code == 403

    def test_unknown_cluster_header_is_rejected(self, rsa_keypair, client):
        token = make_token(rsa_keypair)
        response = client.post(
            "/", json=send_message_body(), headers=auth_headers(token, cluster="some-other-cluster")
        )
        assert response.status_code == 403

    def test_wrong_azp_is_rejected(self, rsa_keypair, client):
        token = make_token(rsa_keypair, azp="some-untrusted-client")
        response = client.post("/", json=send_message_body(), headers=auth_headers(token))
        assert response.status_code == 401


class TestSendMessage:
    def test_valid_call_uses_exchanged_token_not_caller_token(self, rsa_keypair, client, monkeypatch):
        captured: dict[str, Any] = {}

        def fake_generate_response(
            conversation_id, llm_request, user_id, skip_user_id_check, streaming,
            user_token, client_headers, audit_ctx,
        ):
            captured["user_token"] = user_token
            captured["client_headers"] = client_headers
            captured["query"] = llm_request.query
            return types.SimpleNamespace(response="the pod is crashlooping because of an OOM kill")

        monkeypatch.setattr(a2a, "generate_response", fake_generate_response)

        token_a = make_token(rsa_keypair)
        response = client.post("/", json=send_message_body(), headers=auth_headers(token_a))

        assert response.status_code == 200
        body = response.json()
        assert body["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        text = body["result"]["task"]["status"]["message"]["parts"][0]["text"]
        assert text == "the pod is crashlooping because of an OOM kill"

        # The defining security property: MCP (via generate_response's
        # user_token) must see the *exchanged* token, never Token A.
        assert captured["user_token"] == "exchanged-token-b"
        assert captured["user_token"] != token_a
        # And no client-supplied MCP header override is ever forwarded.
        assert captured["client_headers"] is None

    def test_get_task_returns_previously_completed_task(self, rsa_keypair, client, monkeypatch):
        monkeypatch.setattr(
            a2a,
            "generate_response",
            lambda *a, **k: types.SimpleNamespace(response="answer"),
        )
        token_a = make_token(rsa_keypair)
        send_response = client.post("/", json=send_message_body(), headers=auth_headers(token_a))
        task_id = send_response.json()["result"]["task"]["id"]

        get_response = client.post(
            "/",
            json={"jsonrpc": "2.0", "id": "req-2", "method": "GetTask", "params": {"id": task_id}},
            headers=auth_headers(token_a),
        )

        assert get_response.status_code == 200
        body = get_response.json()
        assert body["result"]["task"]["id"] == task_id
        assert body["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    def test_get_task_unknown_id_returns_task_not_found_error(self, rsa_keypair, client, monkeypatch):
        token_a = make_token(rsa_keypair)
        response = client.post(
            "/",
            json={"jsonrpc": "2.0", "id": "req-3", "method": "GetTask", "params": {"id": "does-not-exist"}},
            headers=auth_headers(token_a),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["error"]["code"] == -32001

    def test_legacy_method_name_alias_is_accepted(self, rsa_keypair, client, monkeypatch):
        monkeypatch.setattr(
            a2a,
            "generate_response",
            lambda *a, **k: types.SimpleNamespace(response="answer"),
        )
        token_a = make_token(rsa_keypair)
        response = client.post(
            "/",
            json=send_message_body(method="message/send"),
            headers=auth_headers(token_a),
        )
        assert response.status_code == 200
        assert response.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    def test_query_failure_becomes_failed_task_not_raw_500(self, rsa_keypair, client, monkeypatch):
        from fastapi import HTTPException

        def failing_generate_response(*args, **kwargs):
            raise HTTPException(status_code=500, detail={"response": "LLM is not accessible", "cause": "boom"})

        monkeypatch.setattr(a2a, "generate_response", failing_generate_response)
        token_a = make_token(rsa_keypair)

        response = client.post("/", json=send_message_body(), headers=auth_headers(token_a))

        assert response.status_code == 200  # JSON-RPC: transport succeeds, task fails
        task = response.json()["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_FAILED"
        assert "LLM is not accessible" in task["status"]["message"]["parts"][0]["text"]


class TestMcpHeaderInjectionIsRejected:
    def test_mcp_headers_http_header_is_rejected(self, rsa_keypair, client, monkeypatch):
        monkeypatch.setattr(
            a2a,
            "generate_response",
            lambda *a, **k: pytest.fail("generate_response must not be called"),
        )
        token_a = make_token(rsa_keypair)
        headers = auth_headers(token_a)
        headers["MCP-Headers"] = json.dumps({"github-mcp": {"Authorization": "Bearer stolen"}})

        response = client.post("/", json=send_message_body(), headers=headers)

        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32602

    def test_mcp_headers_param_key_is_rejected(self, rsa_keypair, client, monkeypatch):
        monkeypatch.setattr(
            a2a,
            "generate_response",
            lambda *a, **k: pytest.fail("generate_response must not be called"),
        )
        token_a = make_token(rsa_keypair)
        body = send_message_body()
        body["params"]["mcpHeaders"] = {"github-mcp": {"Authorization": "Bearer stolen"}}

        response = client.post("/", json=body, headers=auth_headers(token_a))

        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32602


class TestTaskIsolation:
    def test_task_not_visible_to_a_different_caller(self, rsa_keypair, client, monkeypatch):
        monkeypatch.setattr(
            a2a,
            "generate_response",
            lambda *a, **k: types.SimpleNamespace(response="answer"),
        )
        caller_one = make_token(rsa_keypair, sub="spiffe://trust-domain/ns/acme/sa/caller-one")
        caller_two = make_token(rsa_keypair, sub="spiffe://trust-domain/ns/acme/sa/caller-two")

        send_response = client.post("/", json=send_message_body(), headers=auth_headers(caller_one))
        task_id = send_response.json()["result"]["task"]["id"]

        get_response = client.post(
            "/",
            json={"jsonrpc": "2.0", "id": "req-x", "method": "GetTask", "params": {"id": task_id}},
            headers=auth_headers(caller_two),
        )

        assert get_response.json()["error"]["code"] == -32001
