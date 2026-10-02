"""Tests for the guarded A2A adapter."""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from ols import config

config.ols_config.authentication_config.module = "k8s"

from ols.app.endpoints import a2a, ols  # noqa: E402
from ols.src.auth.k8s import AuthDependency  # noqa: E402
from ols.src.query_helpers.a2a_context import (  # noqa: E402
    REQUIRE_OPERATOR_MCP_TOOLS,
    A2AMCPUnavailableError,
    require_operator_mcp_tools,
)
from ols.utils import suid  # noqa: E402

CLUSTER_ID = "api-bm-cluster-e2e-bos-redhat-com-6443"
CALLER_ID_1 = "a2a6b688-4dfb-4bbc-adde-8c6a60e733c7"
CALLER_ID_2 = "3f690d7d-cab6-4cee-85a4-9cefa399282b"


class StubIdentityProvider:
    """Resolve synthetic caller tokens to isolated test identities."""

    def __init__(self) -> None:
        """Create request capture state."""
        self.requests: list[tuple[str, str]] = []

    async def authenticate_and_exchange(
        self, caller_token: str, target_cluster_id: str
    ) -> a2a.A2ARequestContext:
        """Return a Token-B-shaped fixture for an accepted synthetic caller."""
        self.requests.append((caller_token, target_cluster_id))
        if caller_token == "expired":  # noqa: S105 - synthetic rejected Token A
            raise a2a.A2AIdentityError(401)
        if caller_token == "forbidden":  # noqa: S105 - synthetic rejected Token A
            raise a2a.A2AIdentityError(403)
        user_id = (
            CALLER_ID_1 if caller_token == "caller-one" else CALLER_ID_2  # noqa: S105
        )
        return a2a.A2ARequestContext(
            user_id=user_id,
            username=f"caller-{caller_token}",
            target_cluster_id=target_cluster_id,
            mcp_access_token=f"token-b-for-{caller_token}",
        )


def create_app(provider: StubIdentityProvider | None = None) -> FastAPI:
    """Create an isolated application containing only the A2A router."""
    app = FastAPI()
    app.include_router(a2a.router)
    if provider is not None:
        app.state.a2a_identity_provider = provider
    return app


def rpc_payload(
    query: str,
    *,
    context_id: str | None = None,
    request_id: str = "rpc-1",
) -> dict[str, Any]:
    """Create a current A2A JSON-RPC message/send request."""
    message: dict[str, Any] = {
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"kind": "text", "text": query}],
    }
    if context_id:
        message["contextId"] = context_id
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {"message": message},
    }


def request_headers(token: str = "caller-one") -> dict[str, str]:  # noqa: S107
    """Build valid invocation headers for a test request."""
    return {
        "Authorization": f"Bearer {token}",
        "X-OLS-Cluster": CLUSTER_ID,
    }


@pytest.mark.parametrize(
    "public_url",
    [
        "http://praxis.example.test",
        "https://praxis.example.test/a2a",
        "https://user:password@praxis.example.test",
        "https://praxis.example.test?target=cluster",
        "https://praxis.example.test#fragment",
    ],
)
def test_build_agent_card_rejects_non_origin_urls(public_url: str) -> None:
    """Reject insecure or non-origin endpoints instead of advertising them."""
    with pytest.raises(ValueError):
        a2a.build_agent_card(public_url)


@pytest.mark.asyncio
async def test_agent_card_is_public_and_uses_configured_gateway_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serve public discovery without deriving the RPC URL from Host headers."""
    monkeypatch.setenv("OLS_A2A_PUBLIC_URL", "https://praxis.example.test/")
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/.well-known/agent-card.json", headers={"Host": "attacker.invalid"}
        )

    assert response.status_code == 200
    card = response.json()
    assert card["protocolVersion"] == "0.3.0"
    assert card["preferredTransport"] == "JSONRPC"
    assert card["url"] == "https://praxis.example.test"
    assert card["capabilities"]["streaming"] is False
    assert "cluster" not in card["url"]


@pytest.mark.asyncio
async def test_agent_card_fails_closed_without_public_gateway_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not publish a request-derived or placeholder RPC URL."""
    monkeypatch.delenv("OLS_A2A_PUBLIC_URL", raising=False)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.get("/.well-known/agent-card.json")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_rpc_rejects_missing_bearer_before_identity_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require an inbound bearer before attempting any identity work."""
    monkeypatch.setenv("OLS_A2A_CLUSTER_ID", CLUSTER_ID)
    provider = StubIdentityProvider()
    app = create_app(provider)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.post(
            "/", json=rpc_payload("inspect pods"), headers={"X-OLS-Cluster": CLUSTER_ID}
        )

    assert response.status_code == 401
    assert provider.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization_values",
    [["Basic not-a-bearer"], ["Bearer one", "Bearer two"]],
)
async def test_rpc_rejects_malformed_or_ambiguous_bearer(
    monkeypatch: pytest.MonkeyPatch,
    authorization_values: list[str],
) -> None:
    """Reject invalid or duplicate bearer headers before identity validation."""
    monkeypatch.setenv("OLS_A2A_CLUSTER_ID", CLUSTER_ID)
    provider = StubIdentityProvider()
    app = create_app(provider)
    headers = [("authorization", value) for value in authorization_values]
    headers.append(("x-ols-cluster", CLUSTER_ID))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.post(
            "/", json=rpc_payload("inspect pods"), headers=headers
        )

    assert response.status_code == 401
    assert provider.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_headers,expected_status",
    [
        ([], 400),
        ([CLUSTER_ID, CLUSTER_ID], 400),
        (["https://api.example.test"], 400),
        (["different-cluster"], 403),
    ],
)
async def test_rpc_rejects_missing_duplicate_malformed_or_unknown_target(
    monkeypatch: pytest.MonkeyPatch,
    target_headers: list[str],
    expected_status: int,
) -> None:
    """Validate the target as one ID and never interpret it as a URL."""
    monkeypatch.setenv("OLS_A2A_CLUSTER_ID", CLUSTER_ID)
    provider = StubIdentityProvider()
    app = create_app(provider)
    headers = [("authorization", "Bearer caller-one")]
    headers.extend(("x-ols-cluster", value) for value in target_headers)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.post(
            "/", json=rpc_payload("inspect pods"), headers=headers
        )

    assert response.status_code == expected_status
    assert provider.requests == []


@pytest.mark.asyncio
async def test_rpc_fails_closed_when_identity_provider_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never treat a bearer as authenticated without the configured validator."""
    monkeypatch.setenv("OLS_A2A_CLUSTER_ID", CLUSTER_ID)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.post(
            "/", json=rpc_payload("inspect pods"), headers=request_headers()
        )

    assert response.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token,expected_status", [("expired", 401), ("forbidden", 403)]
)
async def test_rpc_propagates_fail_closed_identity_decision(
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    expected_status: int,
) -> None:
    """Map only safe auth statuses and omit identity-provider error details."""
    monkeypatch.setenv("OLS_A2A_CLUSTER_ID", CLUSTER_ID)
    provider = StubIdentityProvider()
    app = create_app(provider)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.post(
            "/", json=rpc_payload("inspect pods"), headers=request_headers(token)
        )

    assert response.status_code == expected_status
    assert token not in response.text


@pytest.mark.asyncio
async def test_rpc_passes_only_exchanged_token_to_query_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the stock OLS query path with Token B and no client MCP headers."""
    monkeypatch.setenv("OLS_A2A_CLUSTER_ID", CLUSTER_ID)
    context_id = suid.get_suid()
    calls: list[tuple[Any, tuple[str, str, bool, str]]] = []

    def query_pipeline(
        llm_request: Any, auth: tuple[str, str, bool, str]
    ) -> SimpleNamespace:
        calls.append((llm_request, auth))
        return SimpleNamespace(response="grounded response", conversation_id=context_id)

    monkeypatch.setattr(a2a, "conversation_request", query_pipeline)
    provider = StubIdentityProvider()
    app = create_app(provider)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.post(
            "/",
            json=rpc_payload("investigate the pod", context_id=context_id),
            headers=request_headers(),
        )

    assert response.status_code == 200
    assert response.json()["result"]["parts"] == [
        {"kind": "text", "text": "grounded response"}
    ]
    assert calls[0][0].query == "investigate the pod"
    assert calls[0][0].conversation_id == context_id
    assert calls[0][0].mcp_headers is None
    assert calls[0][1] == (
        CALLER_ID_1,
        "caller-caller-one",
        False,
        "token-b-for-caller-one",
    )
    assert calls[0][1][3] != "caller-one"


@pytest.mark.asyncio
async def test_rpc_rejects_client_mcp_header_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not accept any RPC field capable of replacing operator MCP auth."""
    monkeypatch.setenv("OLS_A2A_CLUSTER_ID", CLUSTER_ID)
    app = create_app(StubIdentityProvider())
    payload = rpc_payload("investigate the pod")
    payload["params"]["mcp_headers"] = {"openshift": {"Authorization": "Bearer A"}}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.post("/", json=payload, headers=request_headers())

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_rpc_propagates_query_failure_without_internal_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return a generic terminal JSON-RPC error for query-pipeline failures."""
    monkeypatch.setenv("OLS_A2A_CLUSTER_ID", CLUSTER_ID)

    def failed_pipeline(*args: Any) -> None:
        raise HTTPException(status_code=503, detail="private provider detail")

    monkeypatch.setattr(a2a, "conversation_request", failed_pipeline)
    app = create_app(StubIdentityProvider())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.post(
            "/", json=rpc_payload("investigate"), headers=request_headers()
        )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32000
    assert response.json()["error"]["data"] == {"httpStatus": 503}
    assert "private provider detail" not in response.text


@pytest.mark.asyncio
async def test_rpc_does_not_claim_success_for_empty_pipeline_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return an error rather than fabricating an empty answer."""
    monkeypatch.setenv("OLS_A2A_CLUSTER_ID", CLUSTER_ID)
    monkeypatch.setattr(
        a2a,
        "conversation_request",
        lambda *args: SimpleNamespace(response="", conversation_id=suid.get_suid()),
    )
    app = create_app(StubIdentityProvider())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.post(
            "/", json=rpc_payload("investigate"), headers=request_headers()
        )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32000
    assert "result" not in response.json()


@pytest.mark.asyncio
async def test_rpc_does_not_claim_task_lookup_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail explicitly for task APIs that the synchronous adapter does not own."""
    monkeypatch.setenv("OLS_A2A_CLUSTER_ID", CLUSTER_ID)
    app = create_app(StubIdentityProvider())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": "task-1",
                "method": "tasks/get",
                "params": {},
            },
            headers=request_headers(),
        )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_a2a_context_is_request_scoped_across_threadpool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep caller tokens and the strict MCP mode isolated for interleaved calls."""
    monkeypatch.setenv("OLS_A2A_CLUSTER_ID", CLUSTER_ID)
    provider = StubIdentityProvider()
    calls: list[tuple[str, str, bool]] = []

    def query_pipeline(
        llm_request: Any, auth: tuple[str, str, bool, str]
    ) -> SimpleNamespace:
        strict_mcp = REQUIRE_OPERATOR_MCP_TOOLS.get()
        calls.append((llm_request.query, auth[3], strict_mcp))
        return SimpleNamespace(
            response=llm_request.query, conversation_id=suid.get_suid()
        )

    monkeypatch.setattr(a2a, "conversation_request", query_pipeline)
    app = create_app(provider)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        responses = await asyncio.gather(
            client.post(
                "/",
                json=rpc_payload("query-one"),
                headers=request_headers("caller-one"),
            ),
            client.post(
                "/",
                json=rpc_payload("query-two"),
                headers=request_headers("caller-two"),
            ),
        )

    assert [response.status_code for response in responses] == [200, 200]
    assert set(calls) == {
        ("query-one", "token-b-for-caller-one", True),
        ("query-two", "token-b-for-caller-two", True),
    }
    assert REQUIRE_OPERATOR_MCP_TOOLS.get() is False


def test_a2a_requires_builtin_mcp_tools_only_for_a2a_invocations() -> None:
    """Fail closed when A2A has no loaded built-in tools without changing REST."""
    reset_token = REQUIRE_OPERATOR_MCP_TOOLS.set(True)
    try:
        with pytest.raises(A2AMCPUnavailableError):
            require_operator_mcp_tools({"openshift": {}}, [])
        with pytest.raises(A2AMCPUnavailableError):
            require_operator_mcp_tools(
                {}, [SimpleNamespace(metadata={"mcp_server": "other"})]
            )
        require_operator_mcp_tools(
            {"openshift": {}}, [SimpleNamespace(metadata={"mcp_server": "openshift"})]
        )
    finally:
        REQUIRE_OPERATOR_MCP_TOOLS.reset(reset_token)

    require_operator_mcp_tools({}, [])


def test_rest_query_keeps_the_existing_kubernetes_auth_dependency() -> None:
    """Keep stock /v1/query authentication independent from the A2A hook."""
    query_route = next(route for route in ols.router.routes if route.path == "/query")

    assert isinstance(ols.auth_dependency, AuthDependency)
    assert query_route.dependant.dependencies[0].call is ols.auth_dependency


def test_identity_context_does_not_expose_token_in_repr() -> None:
    """Avoid accidental token exposure through context debugging/repr."""
    identity = a2a.A2ARequestContext(
        user_id=CALLER_ID_1,
        username="caller",
        target_cluster_id=CLUSTER_ID,
        mcp_access_token="fixture",  # noqa: S106
    )

    assert "fixture" not in repr(identity)
