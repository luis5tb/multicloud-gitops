"""Tests for A2A Keycloak validation and workload token exchange."""

import asyncio
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from ols import config

config.ols_config.authentication_config.module = "k8s"

from ols.app.endpoints import a2a  # noqa: E402
from ols.src.auth import a2a_identity  # noqa: E402

ISSUER = "https://keycloak.example.test/realms/rca"
INBOUND_AUDIENCE = "lightspeed-a2a"
CALLER_CLIENT = "acme-agent"
EXCHANGE_CLIENT = "lightspeed-mcp"
MCP_AUDIENCE = "openshift-mcp"
CLUSTER_ID = "api-cluster-example-6443"
CALLER_SUBJECT_ONE = "caller-subject-one"
CALLER_SUBJECT_TWO = "caller-subject-two"
SVID_SUBJECT = "spiffe://example.test/ns/openshift-lightspeed/sa/app-server"


class MockKeycloak:
    """Serve deterministic OIDC discovery, JWKS, and exchange responses."""

    def __init__(self, signing_key: Any, key_id: str = "key-one") -> None:
        """Create the mock realm with one signing key."""
        self.signing_key = signing_key
        self.key_id = key_id
        self.jwks_requests = 0
        self.discovery_requests = 0
        self.exchange_forms: list[dict[str, list[str]]] = []
        self.exchanged_tokens: list[str] = []
        self.token_b_overrides: dict[str, Any] = {}
        self.fail_request: str | None = None

    def public_jwks(self) -> dict[str, Any]:
        """Return the currently active public RS256 key."""
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(
            self.signing_key.public_key(), as_dict=True
        )
        jwk.update({"kid": self.key_id, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def transport_factory(self) -> httpx.MockTransport:
        """Create an independent HTTP transport for a short-lived client."""
        return httpx.MockTransport(self.handle)

    def response(
        self, request: httpx.Request, payload: dict[str, Any]
    ) -> httpx.Response:
        """Build a JSON response tied to the original request."""
        return httpx.Response(200, json=payload, request=request)

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request without writing tokens to logs."""
        if self.fail_request == request.url.path:
            raise httpx.ConnectError(
                "synthetic TLS verification failure", request=request
            )
        if request.url.path.endswith("/.well-known/openid-configuration"):
            self.discovery_requests += 1
            return self.response(
                request,
                {
                    "issuer": ISSUER,
                    "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                },
            )
        if request.url.path.endswith("/protocol/openid-connect/certs"):
            self.jwks_requests += 1
            return self.response(request, self.public_jwks())
        if request.url.path.endswith("/protocol/openid-connect/token"):
            from urllib.parse import parse_qs

            form = parse_qs(request.content.decode("utf-8"))
            self.exchange_forms.append(form)
            caller_claims = jwt.decode(
                form["subject_token"][0],
                options={"verify_signature": False, "verify_aud": False},
            )
            token_b_claims = {
                "iss": ISSUER,
                "sub": caller_claims["sub"],
                "azp": EXCHANGE_CLIENT,
                "aud": [MCP_AUDIENCE],
                "groups": [a2a_identity.EXPECTED_MCP_GROUP],
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
            }
            token_b_claims.update(self.token_b_overrides)
            access_token = jwt.encode(
                token_b_claims,
                self.signing_key,
                algorithm="RS256",
                headers={"kid": self.key_id},
            )
            self.exchanged_tokens.append(access_token)
            return self.response(
                request,
                {"access_token": access_token},
            )
        return httpx.Response(404, request=request)


class FakeWorkloadApiClient:
    """Return fresh SVIDs and capture requested SPIFFE audiences."""

    def __init__(self, **kwargs: Any) -> None:
        """Capture the configured Workload API parameters."""
        self.kwargs = kwargs

    def __enter__(self) -> "FakeWorkloadApiClient":
        """Return the fake client from a with statement."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Close the fake client's context without side effects."""
        return None

    def fetch_jwt_svid(self, audience: set[str]) -> Any:
        """Return a short-lived SVID with the requested issuer audience."""
        assert audience == {ISSUER}
        claims = {
            "iss": "https://spire.example.test",
            "sub": SVID_SUBJECT,
            "aud": [ISSUER],
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        }
        return type(
            "SVID",
            (),
            {
                "spiffe_id": SVID_SUBJECT,
                "token": jwt.encode(
                    claims,
                    "synthetic-svid-test-key-32-bytes!",
                    algorithm="HS256",
                ),
            },
        )()


@pytest.fixture(scope="module")
def signing_key() -> Any:
    """Create one synthetic RSA key pair for the module's signed test tokens."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def keycloak(signing_key: Any) -> MockKeycloak:
    """Provide a fresh mock Keycloak realm per test."""
    return MockKeycloak(signing_key)


def make_provider(keycloak: MockKeycloak) -> a2a_identity.KeycloakA2AIdentityProvider:
    """Construct a provider with the test-only transport factory."""
    return a2a_identity.KeycloakA2AIdentityProvider(
        issuer_url=ISSUER,
        inbound_audience=INBOUND_AUDIENCE,
        allowed_caller_client_id=CALLER_CLIENT,
        exchange_client_id=EXCHANGE_CLIENT,
        mcp_audience=MCP_AUDIENCE,
        spiffe_audience=ISSUER,
        socket_path="unix:///tmp/fake-spire-agent.sock",
        transport_factory=keycloak.transport_factory,
    )


def make_caller_token(
    signing_key: Any,
    *,
    subject: str = CALLER_SUBJECT_ONE,
    key_id: str = "key-one",
    overrides: dict[str, Any] | None = None,
    omit: set[str] | None = None,
) -> str:
    """Create a signed Token A fixture with the requested claim mutations."""
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "sub": subject,
        "azp": CALLER_CLIENT,
        "aud": [INBOUND_AUDIENCE, EXCHANGE_CLIENT, "account"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    if overrides:
        claims.update(overrides)
    for name in omit or set():
        claims.pop(name, None)
    return jwt.encode(
        claims,
        signing_key,
        algorithm="RS256",
        headers={"kid": key_id},
    )


@pytest.mark.asyncio
async def test_exchange_validates_token_a_and_returns_only_token_b(
    keycloak: MockKeycloak,
    signing_key: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Perform the complete successful Keycloak/SPIRE exchange without logging JWTs."""
    monkeypatch.setattr(a2a_identity, "WorkloadApiClient", FakeWorkloadApiClient)
    provider = make_provider(keycloak)
    caller_token = make_caller_token(signing_key)

    context = await provider.authenticate_and_exchange(caller_token, CLUSTER_ID)

    form = keycloak.exchange_forms[0]
    assert form["subject_token"] == [caller_token]
    assert form["audience"] == [MCP_AUDIENCE]
    assert form["client_assertion_type"] == [a2a_identity.SPIFFE_CLIENT_ASSERTION_TYPE]
    assert form["client_assertion"][0] != caller_token
    assert "client_id" not in form
    assert context.target_cluster_id == CLUSTER_ID
    assert context.user_id == caller_claim_user_id(caller_token)
    assert context.mcp_access_token != caller_token
    assert "secret" not in repr(context)
    assert caller_token not in caplog.text
    assert context.mcp_access_token not in caplog.text


def caller_claim_user_id(token: str) -> str:
    """Return the pipeline's stable user UUID for an opaque subject fixture."""
    claims = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
    return str(
        a2a_identity.KeycloakA2AIdentityProvider._pipeline_user_id(
            ISSUER, claims["sub"]
        )
    )


@pytest.mark.parametrize(
    "overrides,omit",
    [
        ({"iss": "https://wrong.example.test/realms/rca"}, set()),
        ({"aud": ["other-audience", EXCHANGE_CLIENT]}, set()),
        ({"aud": [INBOUND_AUDIENCE]}, set()),
        ({"exp": int(time.time()) - 60}, set()),
        ({}, {"iat"}),
        ({}, {"exp"}),
        ({}, {"sub"}),
        ({}, {"aud"}),
        ({"sub": ""}, set()),
    ],
)
@pytest.mark.asyncio
async def test_token_a_claim_failures_are_rejected_before_exchange(
    keycloak: MockKeycloak,
    signing_key: Any,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    omit: set[str],
) -> None:
    """Reject malformed, expired, wrong-issuer/audience/client/subject Token A."""
    monkeypatch.setattr(a2a_identity, "WorkloadApiClient", FakeWorkloadApiClient)
    provider = make_provider(keycloak)
    caller_token = make_caller_token(signing_key, overrides=overrides, omit=omit)

    with pytest.raises(a2a_identity.A2AIdentityError) as error:
        await provider.authenticate_and_exchange(caller_token, CLUSTER_ID)

    assert error.value.status_code == 401
    assert keycloak.exchange_forms == []


@pytest.mark.asyncio
async def test_wrong_allowed_client_is_forbidden_before_workload_or_exchange(
    keycloak: MockKeycloak,
    signing_key: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require the exact ACME caller client after cryptographic validation."""
    workload_calls: list[bool] = []

    class CountingWorkload(FakeWorkloadApiClient):
        def fetch_jwt_svid(self, audience: set[str]) -> Any:
            workload_calls.append(True)
            return super().fetch_jwt_svid(audience)

    monkeypatch.setattr(a2a_identity, "WorkloadApiClient", CountingWorkload)
    provider = make_provider(keycloak)
    caller_token = make_caller_token(signing_key, overrides={"azp": "not-acme"})

    with pytest.raises(a2a_identity.A2AIdentityError) as error:
        await provider.authenticate_and_exchange(caller_token, CLUSTER_ID)

    assert error.value.status_code == 403
    assert workload_calls == []
    assert keycloak.exchange_forms == []


@pytest.mark.asyncio
async def test_spiffe_failure_fails_closed_without_exchange(
    keycloak: MockKeycloak,
    signing_key: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not exchange Token A if SPIRE cannot provide a workload assertion."""

    class BrokenWorkload:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "BrokenWorkload":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def fetch_jwt_svid(self, audience: set[str]) -> Any:
            raise RuntimeError("synthetic workload socket failure")

    monkeypatch.setattr(a2a_identity, "WorkloadApiClient", BrokenWorkload)
    provider = make_provider(keycloak)
    caller_token = make_caller_token(signing_key)

    with pytest.raises(a2a_identity.A2AIdentityError) as error:
        await provider.authenticate_and_exchange(caller_token, CLUSTER_ID)

    assert error.value.status_code == 503
    assert keycloak.exchange_forms == []


@pytest.mark.asyncio
async def test_exchange_failure_fails_closed(
    keycloak: MockKeycloak,
    signing_key: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return service unavailable when Keycloak cannot complete token exchange."""
    keycloak.fail_request = "/realms/rca/protocol/openid-connect/token"
    monkeypatch.setattr(a2a_identity, "WorkloadApiClient", FakeWorkloadApiClient)
    provider = make_provider(keycloak)

    with pytest.raises(a2a_identity.A2AIdentityError) as error:
        await provider.authenticate_and_exchange(
            make_caller_token(signing_key), CLUSTER_ID
        )

    assert error.value.status_code == 503


@pytest.mark.parametrize(
    "overrides",
    [
        {"sub": "different-subject"},
        {"azp": "wrong-exchange-client"},
        {"aud": ["wrong-audience"]},
        {"iss": "https://wrong.example.test/realms/rca"},
        {"groups": ["other-group"]},
        {"groups": "acme-agent-lightspeed"},
    ],
)
@pytest.mark.asyncio
async def test_token_b_claim_or_group_mismatch_fails_closed(
    keycloak: MockKeycloak,
    signing_key: Any,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
) -> None:
    """Reject exchanged tokens without exact audience, actor, subject, and group."""
    monkeypatch.setattr(a2a_identity, "WorkloadApiClient", FakeWorkloadApiClient)
    keycloak.token_b_overrides = overrides
    provider = make_provider(keycloak)

    with pytest.raises(a2a_identity.A2AIdentityError) as error:
        await provider.authenticate_and_exchange(
            make_caller_token(signing_key), CLUSTER_ID
        )

    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_unknown_kid_refreshes_jwks_after_key_rotation(
    keycloak: MockKeycloak,
    signing_key: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fetch a rotated JWKS key once when a new valid kid appears."""
    monkeypatch.setattr(a2a_identity, "WorkloadApiClient", FakeWorkloadApiClient)
    provider = make_provider(keycloak)
    old_token = make_caller_token(signing_key, key_id="key-one")
    await provider.authenticate_and_exchange(old_token, CLUSTER_ID)
    old_jwks_count = keycloak.jwks_requests

    rotated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    keycloak.signing_key = rotated_key
    keycloak.key_id = "key-two"
    rotated_token = make_caller_token(rotated_key, key_id="key-two")
    await provider.authenticate_and_exchange(rotated_token, CLUSTER_ID)

    assert keycloak.jwks_requests == old_jwks_count + 1


@pytest.mark.asyncio
async def test_tls_failure_returns_service_unavailable_with_verification_enabled(
    keycloak: MockKeycloak,
    signing_key: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed on TLS errors and keep default certificate verification on."""
    keycloak.fail_request = "/realms/rca/.well-known/openid-configuration"
    monkeypatch.setattr(a2a_identity, "WorkloadApiClient", FakeWorkloadApiClient)
    provider = make_provider(keycloak)

    with pytest.raises(a2a_identity.A2AIdentityError) as error:
        await provider.authenticate_and_exchange(
            make_caller_token(signing_key), CLUSTER_ID
        )

    assert provider.oidc._verify is True
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_jwks_fetch_failure_fails_closed(
    keycloak: MockKeycloak,
    signing_key: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not accept a token when its Keycloak signing key cannot be fetched."""
    keycloak.fail_request = "/realms/rca/protocol/openid-connect/certs"
    monkeypatch.setattr(a2a_identity, "WorkloadApiClient", FakeWorkloadApiClient)
    provider = make_provider(keycloak)

    with pytest.raises(a2a_identity.A2AIdentityError) as error:
        await provider.authenticate_and_exchange(
            make_caller_token(signing_key), CLUSTER_ID
        )

    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_concurrent_callers_receive_independent_exchanged_tokens(
    keycloak: MockKeycloak,
    signing_key: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep simultaneous caller subjects and MCP tokens separate per invocation."""
    monkeypatch.setattr(a2a_identity, "WorkloadApiClient", FakeWorkloadApiClient)
    provider = make_provider(keycloak)
    token_one = make_caller_token(signing_key, subject=CALLER_SUBJECT_ONE)
    token_two = make_caller_token(signing_key, subject=CALLER_SUBJECT_TWO)

    contexts = await asyncio.gather(
        provider.authenticate_and_exchange(token_one, CLUSTER_ID),
        provider.authenticate_and_exchange(token_two, CLUSTER_ID),
    )

    assert len(keycloak.exchange_forms) == 2
    assert Counter(
        form["subject_token"][0] for form in keycloak.exchange_forms
    ) == Counter([token_one, token_two])
    assert len({context.user_id for context in contexts}) == 2
    assert len({context.mcp_access_token for context in contexts}) == 2
    assert all(context.target_cluster_id == CLUSTER_ID for context in contexts)
    assert token_one not in repr(contexts)
    assert token_two not in repr(contexts)


@pytest.mark.asyncio
async def test_a2a_rpc_uses_the_real_provider_and_passes_only_token_b(
    keycloak: MockKeycloak,
    signing_key: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connect the authenticated A2A dependency to OLS with only Token B."""
    monkeypatch.setenv("OLS_A2A_CLUSTER_ID", CLUSTER_ID)
    monkeypatch.setattr(a2a_identity, "WorkloadApiClient", FakeWorkloadApiClient)
    provider = make_provider(keycloak)
    app = FastAPI()
    app.include_router(a2a.router)
    app.state.a2a_identity_provider = provider
    calls: list[tuple[Any, tuple[str, str, bool, str]]] = []

    def query_pipeline(request: Any, auth: tuple[str, str, bool, str]) -> Any:
        calls.append((request, auth))
        return SimpleNamespace(
            response="grounded", conversation_id=request.conversation_id
        )

    monkeypatch.setattr(a2a, "conversation_request", query_pipeline)
    caller_token = make_caller_token(signing_key)
    context_id = "b598d1f7-d1cb-445b-9d73-749c99d6c43b"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.post(
            "/",
            headers={
                "Authorization": f"Bearer {caller_token}",
                "X-OLS-Cluster": CLUSTER_ID,
            },
            json={
                "jsonrpc": "2.0",
                "id": "integration-request",
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": "integration-message",
                        "contextId": context_id,
                        "role": "user",
                        "parts": [{"kind": "text", "text": "inspect a pod"}],
                    }
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["result"]["parts"] == [{"kind": "text", "text": "grounded"}]
    assert len(calls) == 1
    assert calls[0][0].query == "inspect a pod"
    assert calls[0][0].mcp_headers is None
    assert calls[0][1][2] is False
    assert calls[0][1][3] != caller_token
    assert calls[0][1][3] == keycloak.exchanged_tokens[0]
    assert calls[0][1][0] == caller_claim_user_id(caller_token)


def test_identity_provider_factory_fails_closed_on_missing_configuration() -> None:
    """Require every environment contract item before installing a provider."""
    with pytest.raises(a2a_identity.A2AIdentityConfigurationError):
        a2a_identity.create_a2a_identity_provider_from_env({})


def test_identity_provider_factory_checks_spiffe_audience_and_ca_file(
    tmp_path: Path,
) -> None:
    """Reject mismatched SVID audience and missing configured CA files."""
    environment = {
        "OLS_A2A_KEYCLOAK_ISSUER_URL": ISSUER,
        "OLS_A2A_INBOUND_AUDIENCE": INBOUND_AUDIENCE,
        "OLS_A2A_ALLOWED_CALLER_CLIENT_ID": CALLER_CLIENT,
        "OLS_A2A_EXCHANGE_CLIENT_ID": EXCHANGE_CLIENT,
        "OLS_A2A_MCP_AUDIENCE": MCP_AUDIENCE,
        "OLS_A2A_SPIFFE_JWT_AUDIENCE": "https://wrong.example.test/realm",
        "SPIFFE_ENDPOINT_SOCKET": "unix:///tmp/spire.sock",
    }
    with pytest.raises(a2a_identity.A2AIdentityConfigurationError):
        a2a_identity.create_a2a_identity_provider_from_env(environment)

    environment["OLS_A2A_SPIFFE_JWT_AUDIENCE"] = ISSUER
    environment["OLS_A2A_KEYCLOAK_CA_BUNDLE"] = str(tmp_path / "missing-ca.crt")
    with pytest.raises(a2a_identity.A2AIdentityConfigurationError):
        a2a_identity.create_a2a_identity_provider_from_env(environment)


def test_app_wiring_installs_only_a_fully_configured_provider() -> None:
    """Leave A2A unavailable for missing/invalid settings without changing REST auth."""
    app = SimpleNamespace(state=SimpleNamespace(a2a_identity_provider=object()))
    assert not a2a_identity.install_a2a_identity_provider(app, {})
    assert not hasattr(app.state, "a2a_identity_provider")

    environment = {
        "OLS_A2A_KEYCLOAK_ISSUER_URL": ISSUER,
        "OLS_A2A_INBOUND_AUDIENCE": INBOUND_AUDIENCE,
        "OLS_A2A_ALLOWED_CALLER_CLIENT_ID": CALLER_CLIENT,
        "OLS_A2A_EXCHANGE_CLIENT_ID": EXCHANGE_CLIENT,
        "OLS_A2A_MCP_AUDIENCE": MCP_AUDIENCE,
        "OLS_A2A_SPIFFE_JWT_AUDIENCE": ISSUER,
        "SPIFFE_ENDPOINT_SOCKET": "unix:///tmp/spire.sock",
    }
    assert a2a_identity.install_a2a_identity_provider(app, environment)
    assert isinstance(
        app.state.a2a_identity_provider,
        a2a_identity.KeycloakA2AIdentityProvider,
    )


def test_identity_provider_keeps_custom_ca_verification_enabled(
    tmp_path: Path,
) -> None:
    """Pass a configured PEM bundle as the verifier rather than disabling TLS."""
    ca_bundle = tmp_path / "keycloak-ca.pem"
    ca_bundle.write_text("test CA placeholder", encoding="utf-8")
    client = a2a_identity.KeycloakOIDCClient(ISSUER, ca_bundle=str(ca_bundle))

    assert client._verify == str(ca_bundle)
