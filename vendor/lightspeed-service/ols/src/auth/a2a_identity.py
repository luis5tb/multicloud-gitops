"""Keycloak caller validation and per-request SPIFFE token exchange for A2A."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import jwt
from fastapi import status
from spiffe import WorkloadApiClient

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


EXPECTED_MCP_GROUP = "acme-agent-lightspeed"
SPIFFE_CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe"
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105
IDENTITY_TIMEOUT_SECONDS = 5.0
JWKS_CACHE_SECONDS = 300
UNKNOWN_KID_REFRESH_SECONDS = 5
JWT_LEEWAY_SECONDS = 30


class A2AIdentityConfigurationError(ValueError):
    """Raised when safe A2A identity configuration is missing or invalid."""


class InvalidCallerTokenError(Exception):
    """Raised when Token A fails cryptographic or claims validation."""


class KeycloakUnavailableError(Exception):
    """Raised when Keycloak discovery, JWKS, or token exchange is unavailable."""


class SPIFFEUnavailableError(Exception):
    """Raised when the Workload API cannot supply a fresh JWT-SVID."""


class A2AIdentityError(Exception):
    """Safe identity-provider failure with an HTTP status, without secret detail."""

    def __init__(self, status_code: int) -> None:
        """Store only a supported status code, never a provider error message."""
        if status_code not in {
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        }:
            raise ValueError("Unsupported A2A identity error status")
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class A2ARequestContext:
    """Validated caller context and the exchanged MCP token for one request."""

    user_id: str
    username: str
    target_cluster_id: str
    mcp_access_token: str = field(repr=False)


class A2AIdentityProvider(Protocol):
    """Authenticate Token A and return a request-scoped Token B context."""

    async def authenticate_and_exchange(
        self, caller_token: str, target_cluster_id: str
    ) -> A2ARequestContext:
        """Validate the caller, exchange its token, and return the trusted context."""


class KeycloakOIDCClient:
    """Validate Keycloak JWTs and exchange them over verified HTTPS."""

    def __init__(
        self,
        issuer_url: str,
        ca_bundle: str | None = None,
        timeout_seconds: float = IDENTITY_TIMEOUT_SECONDS,
        jwks_cache_seconds: int = JWKS_CACHE_SECONDS,
        transport_factory: Callable[[], httpx.BaseTransport] | None = None,
    ) -> None:
        """Create a client bound to one HTTPS Keycloak realm issuer."""
        try:
            parsed_issuer = urlsplit(issuer_url)
            issuer_origin = self._origin(issuer_url)
        except ValueError as error:
            raise A2AIdentityConfigurationError(
                "OLS_A2A_KEYCLOAK_ISSUER_URL must be a valid HTTPS realm issuer"
            ) from error
        if (
            parsed_issuer.scheme != "https"
            or not parsed_issuer.hostname
            or parsed_issuer.username
            or parsed_issuer.password
            or parsed_issuer.query
            or parsed_issuer.fragment
        ):
            raise A2AIdentityConfigurationError(
                "OLS_A2A_KEYCLOAK_ISSUER_URL must be an HTTPS realm issuer"
            )
        if ca_bundle and not Path(ca_bundle).is_file():
            raise A2AIdentityConfigurationError(
                "OLS_A2A_KEYCLOAK_CA_BUNDLE must name a readable CA file"
            )
        if timeout_seconds <= 0 or timeout_seconds > IDENTITY_TIMEOUT_SECONDS:
            raise A2AIdentityConfigurationError("A2A identity timeout is out of bounds")

        self.issuer_url = issuer_url.rstrip("/")
        self._issuer_origin = issuer_origin
        self._verify: str | bool = ca_bundle or True
        self._timeout = httpx.Timeout(timeout_seconds)
        self._jwks_cache_seconds = jwks_cache_seconds
        self._transport_factory = transport_factory
        self._lock = threading.RLock()
        self._discovery: dict[str, Any] | None = None
        self._discovery_expiry = 0.0
        self._keys: dict[str, Any] = {}
        self._jwks_expiry = 0.0
        self._last_unknown_kid_refresh = 0.0

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int]:
        """Return a normalized HTTPS origin or reject the supplied endpoint."""
        parsed = urlsplit(url)
        try:
            port = parsed.port or 443
        except ValueError as error:
            raise A2AIdentityConfigurationError(
                "Invalid Keycloak endpoint port"
            ) from error
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise A2AIdentityConfigurationError("Keycloak endpoints must use HTTPS")
        return parsed.scheme, parsed.hostname.lower(), port

    def _client(self) -> httpx.Client:
        """Create a short-lived HTTP client with certificate verification enabled."""
        transport = self._transport_factory() if self._transport_factory else None
        return httpx.Client(
            verify=self._verify,
            timeout=self._timeout,
            transport=transport,
        )

    def _request_json(
        self,
        method: str,
        url: str,
        data: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Fetch one Keycloak JSON document and hide transport details."""
        try:
            with self._client() as client:
                response = client.request(method, url, data=data)
                response.raise_for_status()
                document = response.json()
        except Exception as error:
            raise KeycloakUnavailableError from error
        if not isinstance(document, dict):
            raise KeycloakUnavailableError
        return document

    def _validate_discovery_endpoint(self, endpoint: Any) -> str:
        """Allow only HTTPS endpoints on the configured Keycloak origin."""
        if not isinstance(endpoint, str) or not endpoint:
            raise KeycloakUnavailableError
        try:
            if self._origin(endpoint) != self._issuer_origin:
                raise KeycloakUnavailableError
        except A2AIdentityConfigurationError as error:
            raise KeycloakUnavailableError from error
        return endpoint

    def _get_discovery(self) -> dict[str, Any]:
        """Load and briefly cache exact-issuer OIDC discovery metadata."""
        with self._lock:
            if self._discovery and time.monotonic() < self._discovery_expiry:
                return self._discovery
            discovery_url = f"{self.issuer_url}/.well-known/openid-configuration"
            document = self._request_json("GET", discovery_url)
            if document.get("issuer", "").rstrip("/") != self.issuer_url:
                raise KeycloakUnavailableError
            document["jwks_uri"] = self._validate_discovery_endpoint(
                document.get("jwks_uri")
            )
            document["token_endpoint"] = self._validate_discovery_endpoint(
                document.get("token_endpoint")
            )
            self._discovery = document
            self._discovery_expiry = time.monotonic() + JWKS_CACHE_SECONDS
            return document

    def _refresh_jwks(self) -> None:
        """Refresh public signing keys from the already-validated discovery URL."""
        discovery = self._get_discovery()
        document = self._request_json("GET", discovery["jwks_uri"])
        jwks = document.get("keys")
        if not isinstance(jwks, list):
            raise KeycloakUnavailableError

        keys: dict[str, Any] = {}
        try:
            for jwk_data in jwks:
                if not isinstance(jwk_data, dict):
                    continue
                key_id = jwk_data.get("kid")
                if (
                    not isinstance(key_id, str)
                    or jwk_data.get("use", "sig") != "sig"
                    or jwk_data.get("alg", "RS256") != "RS256"
                ):
                    continue
                keys[key_id] = jwt.PyJWK.from_dict(jwk_data, algorithm="RS256").key
        except (jwt.PyJWTError, TypeError, ValueError) as error:
            raise KeycloakUnavailableError from error
        if not keys:
            raise KeycloakUnavailableError
        self._keys = keys
        self._jwks_expiry = time.monotonic() + self._jwks_cache_seconds

    def _signing_key(self, key_id: str) -> Any:
        """Resolve a cached RS256 key and refresh promptly on key rotation."""
        with self._lock:
            now = time.monotonic()
            expired = now >= self._jwks_expiry
            unknown = key_id not in self._keys
            if expired or not self._keys:
                self._refresh_jwks()
            elif (
                unknown
                and now - self._last_unknown_kid_refresh >= UNKNOWN_KID_REFRESH_SECONDS
            ):
                self._last_unknown_kid_refresh = now
                self._refresh_jwks()
            signing_key = self._keys.get(key_id)
        if signing_key is None:
            raise InvalidCallerTokenError
        return signing_key

    @staticmethod
    def _audiences(claims: Mapping[str, Any]) -> set[str]:
        """Normalize a JWT audience claim without accepting malformed values."""
        audiences = claims.get("aud")
        if isinstance(audiences, str):
            return {audiences}
        if isinstance(audiences, list) and all(
            isinstance(audience, str) for audience in audiences
        ):
            return set(audiences)
        return set()

    def validate_token(
        self, token: str, required_audiences: Sequence[str]
    ) -> dict[str, Any]:
        """Verify an RS256 bearer and its required issuer/time/subject/audience claims."""
        try:
            header = jwt.get_unverified_header(token)
        except (jwt.PyJWTError, TypeError) as error:
            raise InvalidCallerTokenError from error
        key_id = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(key_id, str) or not key_id:
            raise InvalidCallerTokenError
        signing_key = self._signing_key(key_id)
        try:
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=["RS256"],
                issuer=self.issuer_url,
                options={
                    "require": ["exp", "iat", "iss", "sub"],
                    "verify_aud": False,
                },
                leeway=JWT_LEEWAY_SECONDS,
            )
        except (jwt.PyJWTError, TypeError, ValueError) as error:
            raise InvalidCallerTokenError from error

        if (
            not isinstance(claims.get("sub"), str)
            or not claims["sub"].strip()
            or claims.get("iss") != self.issuer_url
            or not required_audiences
            or not set(required_audiences).issubset(self._audiences(claims))
        ):
            raise InvalidCallerTokenError
        return claims

    def exchange_token(
        self,
        caller_token: str,
        client_assertion: str,
        mcp_audience: str,
    ) -> str:
        """Exchange Token A using a fresh SPIFFE assertion, omitting client_id."""
        token_endpoint = self._get_discovery()["token_endpoint"]
        form_data = {
            "grant_type": TOKEN_EXCHANGE_GRANT,
            "subject_token": caller_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "client_assertion_type": SPIFFE_CLIENT_ASSERTION_TYPE,
            "client_assertion": client_assertion,
            "audience": mcp_audience,
        }
        try:
            response = self._request_json("POST", token_endpoint, form_data)
        except KeycloakUnavailableError:
            raise
        access_token = response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise KeycloakUnavailableError
        return access_token


class SPIFFEJWTSource:
    """Fetch a fresh workload JWT-SVID from the configured SPIFFE socket."""

    def __init__(self, audience: str, socket_path: str, timeout_seconds: float) -> None:
        """Store the required Keycloak assertion audience and Workload API path."""
        if not audience or not socket_path:
            raise A2AIdentityConfigurationError(
                "OLS_A2A_SPIFFE_JWT_AUDIENCE and SPIFFE_ENDPOINT_SOCKET are required"
            )
        self.audience = audience
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    def fetch(self) -> str:
        """Request a fresh SVID and validate the claims used by Keycloak federation."""
        try:
            with WorkloadApiClient(
                socket_path=self.socket_path,
                default_timeout=self.timeout_seconds,
            ) as client:
                svid = client.fetch_jwt_svid(audience={self.audience})
        except Exception as error:
            raise SPIFFEUnavailableError from error

        token = str(getattr(svid, "token", "") or getattr(svid, "jwt_svid", ""))
        spiffe_id = str(getattr(svid, "spiffe_id", ""))
        if not token or not spiffe_id.startswith("spiffe://"):
            raise SPIFFEUnavailableError
        try:
            claims = jwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_aud": False,
                    "verify_iss": False,
                },
            )
        except (jwt.PyJWTError, TypeError, ValueError) as error:
            raise SPIFFEUnavailableError from error
        audiences = KeycloakOIDCClient._audiences(claims)
        if (
            claims.get("sub") != spiffe_id
            or self.audience not in audiences
            or not isinstance(claims.get("exp"), (int, float))
            or claims["exp"] <= time.time()
            or not isinstance(claims.get("iat"), (int, float))
            or claims["iat"] > time.time() + JWT_LEEWAY_SECONDS
        ):
            raise SPIFFEUnavailableError
        return token


class KeycloakA2AIdentityProvider:
    """Validate Token A and issue a claim-checked OpenShift MCP Token B."""

    def __init__(
        self,
        issuer_url: str,
        inbound_audience: str,
        allowed_caller_client_id: str,
        exchange_client_id: str,
        mcp_audience: str,
        spiffe_audience: str,
        socket_path: str,
        ca_bundle: str | None = None,
        timeout_seconds: float = IDENTITY_TIMEOUT_SECONDS,
        transport_factory: Callable[[], httpx.BaseTransport] | None = None,
    ) -> None:
        """Create the complete per-request federation and exchange flow."""
        required_values = (
            issuer_url,
            inbound_audience,
            allowed_caller_client_id,
            exchange_client_id,
            mcp_audience,
            spiffe_audience,
            socket_path,
        )
        if not all(value.strip() for value in required_values):
            raise A2AIdentityConfigurationError(
                "Required A2A identity settings are missing"
            )
        if spiffe_audience.rstrip("/") != issuer_url.rstrip("/"):
            raise A2AIdentityConfigurationError(
                "OLS_A2A_SPIFFE_JWT_AUDIENCE must equal the Keycloak issuer URL"
            )
        if timeout_seconds <= 0 or timeout_seconds > IDENTITY_TIMEOUT_SECONDS:
            raise A2AIdentityConfigurationError("A2A identity timeout is out of bounds")

        self.issuer_url = issuer_url.rstrip("/")
        self.inbound_audience = inbound_audience
        self.allowed_caller_client_id = allowed_caller_client_id
        self.exchange_client_id = exchange_client_id
        self.mcp_audience = mcp_audience
        self.oidc = KeycloakOIDCClient(
            self.issuer_url,
            ca_bundle=ca_bundle,
            timeout_seconds=timeout_seconds,
            transport_factory=transport_factory,
        )
        self.workload = SPIFFEJWTSource(
            audience=self.issuer_url,
            socket_path=socket_path,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _client_id(claims: Mapping[str, Any]) -> str | None:
        """Resolve Keycloak's authorized-party/client claim and reject conflicts."""
        azp = claims.get("azp")
        client_id = claims.get("client_id")
        if azp is not None and client_id is not None and azp != client_id:
            return None
        value = azp if azp is not None else client_id
        return value if isinstance(value, str) else None

    @staticmethod
    def _pipeline_user_id(issuer: str, subject: str) -> str:
        """Return a stable UUID suitable for the OLS cache and transcript paths."""
        try:
            return str(UUID(subject))
        except ValueError:
            return str(uuid5(NAMESPACE_URL, f"{issuer}\x00{subject}"))

    def _exchange(self, caller_token: str, target_cluster_id: str) -> A2ARequestContext:
        """Perform all synchronous identity steps for one caller invocation."""
        caller_claims = self.oidc.validate_token(
            caller_token,
            (self.inbound_audience, self.exchange_client_id),
        )
        if self._client_id(caller_claims) != self.allowed_caller_client_id:
            raise A2AIdentityError(status.HTTP_403_FORBIDDEN)

        caller_subject = caller_claims["sub"]
        assertion = self.workload.fetch()
        token_b = self.oidc.exchange_token(
            caller_token,
            assertion,
            self.mcp_audience,
        )
        try:
            exchanged_claims = self.oidc.validate_token(token_b, (self.mcp_audience,))
        except (InvalidCallerTokenError, KeycloakUnavailableError) as error:
            raise KeycloakUnavailableError from error

        groups = exchanged_claims.get("groups")
        if (
            exchanged_claims["sub"] != caller_subject
            or self._client_id(exchanged_claims) != self.exchange_client_id
            or not isinstance(groups, list)
            or EXPECTED_MCP_GROUP not in groups
        ):
            raise KeycloakUnavailableError

        username = caller_claims.get("preferred_username")
        if not isinstance(username, str) or not username.strip():
            username = caller_subject
        return A2ARequestContext(
            user_id=self._pipeline_user_id(self.issuer_url, caller_subject),
            username=username,
            target_cluster_id=target_cluster_id,
            mcp_access_token=token_b,
        )

    async def authenticate_and_exchange(
        self, caller_token: str, target_cluster_id: str
    ) -> A2ARequestContext:
        """Run network and SPIFFE operations off-loop with no shared bearer state."""
        try:
            return await asyncio.to_thread(
                self._exchange, caller_token, target_cluster_id
            )
        except A2AIdentityError:
            raise
        except InvalidCallerTokenError as error:
            raise A2AIdentityError(status.HTTP_401_UNAUTHORIZED) from error
        except (KeycloakUnavailableError, SPIFFEUnavailableError) as error:
            raise A2AIdentityError(status.HTTP_503_SERVICE_UNAVAILABLE) from error
        except Exception as error:
            raise A2AIdentityError(status.HTTP_503_SERVICE_UNAVAILABLE) from error


def create_a2a_identity_provider_from_env(
    environ: Mapping[str, str] | None = None,
) -> KeycloakA2AIdentityProvider:
    """Build the production identity provider from the explicit environment contract."""
    values = os.environ if environ is None else environ
    required = {
        "OLS_A2A_KEYCLOAK_ISSUER_URL": values.get("OLS_A2A_KEYCLOAK_ISSUER_URL", ""),
        "OLS_A2A_INBOUND_AUDIENCE": values.get("OLS_A2A_INBOUND_AUDIENCE", ""),
        "OLS_A2A_ALLOWED_CALLER_CLIENT_ID": values.get(
            "OLS_A2A_ALLOWED_CALLER_CLIENT_ID", ""
        ),
        "OLS_A2A_EXCHANGE_CLIENT_ID": values.get("OLS_A2A_EXCHANGE_CLIENT_ID", ""),
        "OLS_A2A_MCP_AUDIENCE": values.get("OLS_A2A_MCP_AUDIENCE", ""),
        "OLS_A2A_SPIFFE_JWT_AUDIENCE": values.get("OLS_A2A_SPIFFE_JWT_AUDIENCE", ""),
        "SPIFFE_ENDPOINT_SOCKET": values.get("SPIFFE_ENDPOINT_SOCKET", ""),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise A2AIdentityConfigurationError(
            "Required A2A identity settings are missing"
        )
    return KeycloakA2AIdentityProvider(
        issuer_url=required["OLS_A2A_KEYCLOAK_ISSUER_URL"],
        inbound_audience=required["OLS_A2A_INBOUND_AUDIENCE"],
        allowed_caller_client_id=required["OLS_A2A_ALLOWED_CALLER_CLIENT_ID"],
        exchange_client_id=required["OLS_A2A_EXCHANGE_CLIENT_ID"],
        mcp_audience=required["OLS_A2A_MCP_AUDIENCE"],
        spiffe_audience=required["OLS_A2A_SPIFFE_JWT_AUDIENCE"],
        socket_path=required["SPIFFE_ENDPOINT_SOCKET"],
        ca_bundle=values.get("OLS_A2A_KEYCLOAK_CA_BUNDLE") or None,
    )


def install_a2a_identity_provider(
    application: Any, environ: Mapping[str, str] | None = None
) -> bool:
    """Install a fully configured provider or leave A2A explicitly unavailable."""
    try:
        provider = create_a2a_identity_provider_from_env(environ)
    except A2AIdentityConfigurationError:
        if hasattr(application.state, "a2a_identity_provider"):
            delattr(application.state, "a2a_identity_provider")
        return False
    application.state.a2a_identity_provider = provider
    return True
