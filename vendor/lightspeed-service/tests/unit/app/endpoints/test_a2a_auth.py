"""Unit tests for the A2A authentication/token-exchange module.

These tests exercise ``ols.app.endpoints.a2a_auth`` in isolation: they do not
import ``ols.app.endpoints.ols`` or anything else from the stock REST stack,
so they do not require the service's heavier (RAG/LLM) dependencies to be
installed. Keycloak HTTP calls are faked via a minimal async client double
(no network, no real Keycloak); the SPIFFE Workload API is mocked via
``unittest.mock``.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request

from ols.app.endpoints import a2a_auth

ISSUER = "https://keycloak.example.com/realms/rca"
INBOUND_AUDIENCE = "lightspeed-a2a"
INBOUND_AZP = "acme-agent"
EXCHANGE_AUDIENCE = "openshift-mcp"
CLUSTER_ID = "api-bm-cluster-e2e-bos-redhat-com-6443"
KID = "test-kid-1"


@pytest.fixture(scope="module")
def rsa_keypair():
    """Generate one RSA keypair, reused by every token built in this module."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture()
def jwk(rsa_keypair):
    """The public key as a JWK dict, as Keycloak's JWKS endpoint would serve it."""
    _, public_key = rsa_keypair
    raw = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    raw["kid"] = KID
    raw["use"] = "sig"
    raw["alg"] = "RS256"
    return raw


def make_token(
    rsa_keypair,
    *,
    sub: str = "spiffe://trust-domain/ns/acme/sa/acme-agent",
    aud: str = INBOUND_AUDIENCE,
    azp: str = INBOUND_AZP,
    issuer: str = ISSUER,
    expires_in: int = 300,
    extra_claims: Optional[dict[str, Any]] = None,
    kid: str = KID,
) -> str:
    """Build an RS256 JWT signed by the module's test keypair."""
    private_key, _ = rsa_keypair
    now = int(time.time())
    payload = {
        "iss": issuer,
        "aud": aud,
        "azp": azp,
        "sub": sub,
        "iat": now,
        "exp": now + expires_in,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


def make_settings(**overrides: Any) -> a2a_auth.A2ASettings:
    defaults = dict(
        keycloak_issuer_url=ISSUER,
        keycloak_ca_bundle="",
        inbound_audience=INBOUND_AUDIENCE,
        inbound_azp=INBOUND_AZP,
        exchange_audience=EXCHANGE_AUDIENCE,
        exchange_client_assertion_type=a2a_auth.DEFAULT_EXCHANGE_CLIENT_ASSERTION_TYPE,
        spiffe_jwt_audience=ISSUER,
        spiffe_endpoint_socket="",
        spiffe_timeout_seconds=5.0,
        cluster_id=CLUSTER_ID,
        rpc_url="https://praxis.example.com/",
        agent_name="openshift-lightspeed",
        agent_description="test",
        keycloak_timeout_seconds=5.0,
        jwks_cache_seconds=300,
        exchange_timeout_seconds=5.0,
    )
    defaults.update(overrides)
    return a2a_auth.A2ASettings(**defaults)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("error", request=MagicMock(), response=self)


class _FakeAsyncClient:
    """Stands in for ``httpx.AsyncClient`` in tests: no real network calls."""

    def __init__(self, responder: Callable[[str, Optional[dict]], _FakeResponse]):
        self._responder = responder

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def get(self, url: str) -> _FakeResponse:
        return self._responder(url, None)

    async def post(self, url: str, data: Optional[dict] = None) -> _FakeResponse:
        return self._responder(url, data)


def patch_httpx(monkeypatch: pytest.MonkeyPatch, jwk: dict, token_response: Optional[dict] = None) -> list:
    """Patch ``a2a_auth.httpx.AsyncClient`` with a fake Keycloak server.

    Returns the list of ``(url, data)`` calls made, for assertions (e.g. to
    confirm no ``client_id`` form parameter was sent during exchange).
    """
    calls: list[tuple[str, Optional[dict]]] = []

    def responder(url: str, data: Optional[dict]) -> _FakeResponse:
        calls.append((url, data))
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
            if token_response is None:
                return _FakeResponse({"error": "invalid_request"}, status_code=400)
            return _FakeResponse(token_response)
        raise AssertionError(f"Unexpected URL requested in test: {url}")

    monkeypatch.setattr(
        a2a_auth.httpx, "AsyncClient", lambda **_kwargs: _FakeAsyncClient(responder)
    )
    return calls


def make_request(token: Optional[str], cluster_header: Optional[str]) -> Request:
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    if cluster_header is not None:
        headers.append((b"x-ols-cluster", cluster_header.encode()))
    scope = {"type": "http", "headers": headers}
    return Request(scope)


class TestKeycloakTokenValidator:
    """Covers T1.2's required cases: valid, expired, wrong audience, wrong azp."""

    @pytest.mark.asyncio
    async def test_valid_token_is_accepted(self, monkeypatch, rsa_keypair, jwk):
        patch_httpx(monkeypatch, jwk)
        validator = a2a_auth.KeycloakTokenValidator(make_settings())
        token = make_token(rsa_keypair)

        claims = await validator.validate(token)

        assert claims["aud"] == INBOUND_AUDIENCE
        assert claims["azp"] == INBOUND_AZP

    @pytest.mark.asyncio
    async def test_expired_token_is_rejected(self, monkeypatch, rsa_keypair, jwk):
        patch_httpx(monkeypatch, jwk)
        validator = a2a_auth.KeycloakTokenValidator(make_settings())
        token = make_token(rsa_keypair, expires_in=-60)

        with pytest.raises(a2a_auth.A2AAuthenticationError):
            await validator.validate(token)

    @pytest.mark.asyncio
    async def test_wrong_audience_is_rejected(self, monkeypatch, rsa_keypair, jwk):
        patch_httpx(monkeypatch, jwk)
        validator = a2a_auth.KeycloakTokenValidator(make_settings())
        token = make_token(rsa_keypair, aud="some-other-audience")

        with pytest.raises(a2a_auth.A2AAuthenticationError):
            await validator.validate(token)

    @pytest.mark.asyncio
    async def test_wrong_azp_is_rejected(self, monkeypatch, rsa_keypair, jwk):
        patch_httpx(monkeypatch, jwk)
        validator = a2a_auth.KeycloakTokenValidator(make_settings())
        token = make_token(rsa_keypair, azp="some-other-client")

        with pytest.raises(a2a_auth.A2AAuthenticationError):
            await validator.validate(token)

    @pytest.mark.asyncio
    async def test_empty_token_is_rejected(self, monkeypatch, jwk):
        patch_httpx(monkeypatch, jwk)
        validator = a2a_auth.KeycloakTokenValidator(make_settings())

        with pytest.raises(a2a_auth.A2AAuthenticationError):
            await validator.validate("")


class TestAuthenticateCaller:
    """Covers the FastAPI-facing dependency: header handling, HTTP status codes."""

    @pytest.mark.asyncio
    async def test_missing_bearer_token_is_401(self, monkeypatch, jwk):
        patch_httpx(monkeypatch, jwk)
        authenticator = a2a_auth.A2AAuthenticator(make_settings())
        request = make_request(token=None, cluster_header=CLUSTER_ID)

        with pytest.raises(HTTPException) as excinfo:
            await authenticator.authenticate_caller(request)
        assert excinfo.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_cluster_header_is_403(self, monkeypatch, rsa_keypair, jwk):
        patch_httpx(monkeypatch, jwk)
        authenticator = a2a_auth.A2AAuthenticator(make_settings())
        token = make_token(rsa_keypair)
        request = make_request(token=token, cluster_header=None)

        with pytest.raises(HTTPException) as excinfo:
            await authenticator.authenticate_caller(request)
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_cluster_header_is_403(self, monkeypatch, rsa_keypair, jwk):
        patch_httpx(monkeypatch, jwk)
        authenticator = a2a_auth.A2AAuthenticator(make_settings())
        token = make_token(rsa_keypair)
        request = make_request(token=token, cluster_header="some-other-cluster")

        with pytest.raises(HTTPException) as excinfo:
            await authenticator.authenticate_caller(request)
        assert excinfo.value.status_code == 403

    @pytest.mark.asyncio
    async def test_valid_request_returns_caller_identity(self, monkeypatch, rsa_keypair, jwk):
        patch_httpx(monkeypatch, jwk)
        authenticator = a2a_auth.A2AAuthenticator(make_settings())
        token = make_token(rsa_keypair, sub="spiffe://trust-domain/ns/acme/sa/acme-agent")
        request = make_request(token=token, cluster_header=CLUSTER_ID)

        caller = await authenticator.authenticate_caller(request)

        assert caller.cluster_id == CLUSTER_ID
        assert caller.sub == "spiffe://trust-domain/ns/acme/sa/acme-agent"
        assert caller.task_scope == f"{CLUSTER_ID}:{caller.sub}"
        # Token A must be retained only for the exchange, never discarded
        # silently (otherwise exchange_for_mcp could not use it).
        assert caller.token == token


class TestTokenExchange:
    """Covers T1.2's required successful mocked exchange case."""

    @pytest.mark.asyncio
    async def test_successful_exchange_returns_token_b(self, monkeypatch, rsa_keypair, jwk):
        calls = patch_httpx(
            monkeypatch, jwk, token_response={"access_token": "token-b-value"}
        )
        settings = make_settings()
        authenticator = a2a_auth.A2AAuthenticator(settings)
        token_a = make_token(rsa_keypair)
        caller = a2a_auth.CallerIdentity(
            sub="spiffe://trust-domain/ns/acme/sa/acme-agent",
            claims={"sub": "spiffe://trust-domain/ns/acme/sa/acme-agent"},
            cluster_id=CLUSTER_ID,
            token=token_a,
        )

        with patch.object(
            a2a_auth.SpiffeWorkloadIdentity, "fetch", AsyncMock(return_value="jwt-svid-value")
        ):
            token_b = await authenticator.exchange_for_mcp(caller)

        assert token_b == "token-b-value"

        token_calls = [data for url, data in calls if url.endswith("/token") and data]
        assert len(token_calls) == 1
        sent = token_calls[0]
        assert sent["subject_token"] == token_a
        assert sent["client_assertion"] == "jwt-svid-value"
        assert sent["audience"] == EXCHANGE_AUDIENCE
        assert sent["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
        # No client_id form parameter: required by the SPIFFE-federated flow.
        assert "client_id" not in sent

    @pytest.mark.asyncio
    async def test_exchange_failure_raises(self, monkeypatch, rsa_keypair, jwk):
        patch_httpx(monkeypatch, jwk, token_response=None)
        authenticator = a2a_auth.A2AAuthenticator(make_settings())
        caller = a2a_auth.CallerIdentity(
            sub="spiffe://trust-domain/ns/acme/sa/acme-agent",
            claims={},
            cluster_id=CLUSTER_ID,
            token=make_token(rsa_keypair),
        )

        with patch.object(
            a2a_auth.SpiffeWorkloadIdentity, "fetch", AsyncMock(return_value="jwt-svid-value")
        ):
            with pytest.raises(a2a_auth.A2AExchangeError):
                await authenticator.exchange_for_mcp(caller)

    @pytest.mark.asyncio
    async def test_workload_identity_failure_raises(self, monkeypatch, rsa_keypair, jwk):
        patch_httpx(monkeypatch, jwk, token_response={"access_token": "token-b-value"})
        authenticator = a2a_auth.A2AAuthenticator(make_settings())
        caller = a2a_auth.CallerIdentity(
            sub="spiffe://trust-domain/ns/acme/sa/acme-agent",
            claims={},
            cluster_id=CLUSTER_ID,
            token=make_token(rsa_keypair),
        )

        with patch.object(
            a2a_auth.SpiffeWorkloadIdentity,
            "fetch",
            AsyncMock(side_effect=a2a_auth.A2AWorkloadIdentityError("no SVID")),
        ):
            with pytest.raises(a2a_auth.A2AWorkloadIdentityError):
                await authenticator.exchange_for_mcp(caller)


class TestSettings:
    """Configuration validation."""

    def test_missing_required_env_raises(self, monkeypatch):
        monkeypatch.delenv("A2A_KEYCLOAK_ISSUER_URL", raising=False)
        monkeypatch.delenv("A2A_CLUSTER_ID", raising=False)
        monkeypatch.delenv("A2A_RPC_URL", raising=False)

        with pytest.raises(a2a_auth.A2AConfigurationError):
            a2a_auth.A2ASettings.from_env()

    def test_frozen_defaults_applied(self, monkeypatch):
        monkeypatch.setenv("A2A_KEYCLOAK_ISSUER_URL", ISSUER)
        monkeypatch.setenv("A2A_CLUSTER_ID", CLUSTER_ID)
        monkeypatch.setenv("A2A_RPC_URL", "https://praxis.example.com/")
        for name in (
            "A2A_INBOUND_AUDIENCE",
            "A2A_INBOUND_AZP",
            "A2A_EXCHANGE_AUDIENCE",
        ):
            monkeypatch.delenv(name, raising=False)

        settings = a2a_auth.A2ASettings.from_env()

        assert settings.inbound_audience == "lightspeed-a2a"
        assert settings.inbound_azp == "acme-agent"
        assert settings.exchange_audience == "openshift-mcp"
        # SPIFFE assertion audience defaults to the Keycloak issuer URL.
        assert settings.spiffe_jwt_audience == ISSUER
