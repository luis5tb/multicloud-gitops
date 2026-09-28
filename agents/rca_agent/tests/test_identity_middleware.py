import asyncio

import pytest

from rca_agent.identity import (
    AuthenticationError,
    A2AAuthenticationMiddleware,
    WorkloadIdentityError,
    current_request_identity,
)


class StubKeycloak:
    def __init__(self, claims=None, validate_error=None, availability_error=None):
        self.claims = claims or {
            "iss": "https://keycloak.example.com/realms/rca",
            "sub": "caller-subject",
            "azp": "unlisted-client",
        }
        self.validate_error = validate_error
        self.availability_error = availability_error
        self.validated_tokens = []
        self.availability_checks = 0

    def validate(self, token):
        self.validated_tokens.append(token)
        if self.validate_error:
            raise self.validate_error
        if not token:
            raise AuthenticationError("missing token")
        return self.claims

    def check_available(self):
        self.availability_checks += 1
        if self.availability_error:
            raise self.availability_error


class StubWorkload:
    def __init__(self, error=None):
        self.error = error
        self.identity_checks = 0

    def get_identity(self):
        self.identity_checks += 1
        if self.error:
            raise self.error
        return {
            "spiffe_id": "spiffe://example.test/ns/rca/sa/rca-agent",
            "jwt_svid": "workload-svid",
        }


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _run_middleware(middleware, path, headers=(), identity_seen=None):
    events = []
    app_calls = []

    async def app(scope, receive, send):
        app_calls.append(scope)
        if identity_seen is not None:
            try:
                identity_seen.append(current_request_identity())
            except AuthenticationError:
                identity_seen.append(None)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(event):
        events.append(event)

    middleware.app = app
    await middleware(
        {"type": "http", "path": path, "headers": list(headers)},
        _receive,
        send,
    )
    status = next(event["status"] for event in events if event["type"] == "http.response.start")
    return status, events, app_calls


def _middleware(keycloak=None, workload=None):
    return A2AAuthenticationMiddleware(
        app=None,
        keycloak=keycloak or StubKeycloak(),
        workload=workload or StubWorkload(),
    )


def test_missing_bearer_token_is_rejected_before_the_app_runs():
    keycloak = StubKeycloak()
    workload = StubWorkload()

    status, _, app_calls = asyncio.run(
        _run_middleware(_middleware(keycloak, workload), "/")
    )

    assert status == 401
    assert app_calls == []
    assert keycloak.validated_tokens == [None]


def test_invalid_keycloak_token_is_rejected_before_the_app_runs():
    keycloak = StubKeycloak(validate_error=AuthenticationError("bad token"))
    workload = StubWorkload()

    status, _, app_calls = asyncio.run(
        _run_middleware(
            _middleware(keycloak, workload),
            "/",
            [(b"authorization", b"Bearer invalid-token")],
        )
    )

    assert status == 401
    assert app_calls == []


def test_public_agent_card_does_not_require_identity():
    keycloak = StubKeycloak()
    workload = StubWorkload()
    identities = []

    status, _, app_calls = asyncio.run(
        _run_middleware(
            _middleware(keycloak, workload),
            "/.well-known/agent-card.json",
            identity_seen=identities,
        )
    )

    assert status == 200
    assert len(app_calls) == 1
    assert identities == [None]
    assert keycloak.validated_tokens == []
    assert workload.identity_checks == 0


def test_readiness_checks_keycloak_and_workload_without_a_bearer_token():
    keycloak = StubKeycloak()
    workload = StubWorkload()

    status, events, app_calls = asyncio.run(
        _run_middleware(_middleware(keycloak, workload), "/health/ready")
    )

    assert status == 200
    assert events[-1]["body"] == b'{"status": "ready"}'
    assert app_calls == []
    assert keycloak.availability_checks == 1
    assert workload.identity_checks == 1
    assert keycloak.validated_tokens == []


@pytest.mark.parametrize(
    "keycloak,workload",
    [
        (StubKeycloak(availability_error=AuthenticationError("offline")), StubWorkload()),
        (StubKeycloak(), StubWorkload(error=WorkloadIdentityError("offline"))),
    ],
)
def test_readiness_fails_when_either_dependency_is_unavailable(keycloak, workload):
    status, events, app_calls = asyncio.run(
        _run_middleware(_middleware(keycloak, workload), "/health/ready")
    )

    assert status == 503
    assert b"not both ready" in events[-1]["body"]
    assert app_calls == []


def test_authenticated_request_retains_identity_for_token_exchange():
    claims = {
        "iss": "https://keycloak.example.com/realms/rca",
        "sub": "caller-subject",
        "azp": "unlisted-client",
    }
    keycloak = StubKeycloak(claims=claims)
    workload = StubWorkload()
    identities = []

    status, _, app_calls = asyncio.run(
        _run_middleware(
            _middleware(keycloak, workload),
            "/",
            [(b"authorization", b"Bearer caller-token")],
            identities,
        )
    )

    # The Praxis APL policy owns caller authorization. RCA validates the token
    # and preserves its claims for delegated token exchange, but does not
    # duplicate the proxy's client allow-list decision.
    assert status == 200
    assert len(app_calls) == 1
    assert keycloak.validated_tokens == ["caller-token"]
    assert identities[0].caller_token == "caller-token"
    assert identities[0].caller_claims == claims
    assert identities[0].workload_identity["jwt_svid"] == "workload-svid"
