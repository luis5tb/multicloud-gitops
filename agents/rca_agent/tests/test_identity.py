import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from rca_agent.identity import (
    AuthenticationError,
    KeycloakTokenExchangeError,
    KeycloakTokenExchanger,
    KeycloakTokenValidator,
)

ISSUER = "https://keycloak.example.com/realms/rca"
AUDIENCE = "rca-agent"


def _validator() -> KeycloakTokenValidator:
    validator = KeycloakTokenValidator(issuer_url=ISSUER, audience=AUDIENCE)
    # Bypass real HTTP discovery/JWKS fetches: the JWKS is injected directly
    # by _issue_token below, keyed by "kid".
    validator._discovery = {"issuer": ISSUER, "jwks_uri": "unused"}
    return validator


def _issue_token(validator: KeycloakTokenValidator, key: rsa.RSAPrivateKey, kid: str, **claim_overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "0e844946-0456-475b-9265-e17532b362c9",
        "azp": "ericsson-agent",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(claim_overrides)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = kid
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    validator._jwks = {"keys": [jwk]}
    validator._jwks_expiry = time.monotonic() + 300
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_validate_accepts_a_well_formed_token(key) -> None:
    validator = _validator()
    token = _issue_token(validator, key, "kid-1")

    claims = validator.validate(token)

    assert claims["azp"] == "ericsson-agent"


def test_validate_rejects_wrong_issuer(key) -> None:
    validator = _validator()
    token = _issue_token(validator, key, "kid-1", iss="https://attacker.example.com/realms/rca")

    with pytest.raises(AuthenticationError):
        validator.validate(token)


def test_validate_rejects_wrong_audience(key) -> None:
    validator = _validator()
    token = _issue_token(validator, key, "kid-1", aud="some-other-client")

    with pytest.raises(AuthenticationError):
        validator.validate(token)


def test_validate_rejects_expired_token(key) -> None:
    validator = _validator()
    now = int(time.time())
    token = _issue_token(validator, key, "kid-1", iat=now - 600, exp=now - 300)

    with pytest.raises(AuthenticationError):
        validator.validate(token)


def test_validate_rejects_invalid_signature(key) -> None:
    validator = _validator()
    token = _issue_token(validator, key, "kid-1")
    # Re-sign with a different key but keep the same kid, so the validator
    # looks up the *original* (non-matching) public key from its JWKS cache.
    forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged_token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "attacker", "iat": now, "exp": now + 300},
        forged_key,
        algorithm="RS256",
        headers={"kid": "kid-1"},
    )

    with pytest.raises(AuthenticationError):
        validator.validate(forged_token)


def test_validate_rejects_missing_token() -> None:
    validator = _validator()

    with pytest.raises(AuthenticationError):
        validator.validate("")


def test_token_exchange_failure_raises(monkeypatch) -> None:
    exchanger = KeycloakTokenExchanger(
        issuer_url=ISSUER,
        client_id="rca-agent-mcp",
        audience="openshift-mcp",
        token_url="https://keycloak.example.com/realms/rca/protocol/openid-connect/token",
    )

    class _Identity:
        caller_token = "caller-token"
        workload_identity = {"jwt_svid": "svid-token"}

    def _boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    import httpx

    monkeypatch.setattr(httpx.Client, "post", _boom)

    with pytest.raises(KeycloakTokenExchangeError):
        exchanger.exchange(_Identity())
