"""Workload identity and inbound Keycloak authentication.

The agent uses ZTWIM/SPIRE for its own short-lived workload identity and
Keycloak as the issuer of caller access tokens. Both checks are required for
an A2A request; there is no anonymous or development authentication mode.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional
from uuid import uuid4

import httpx
import jwt
from spiffe import WorkloadApiClient

from .opa import OpaAuthorizationError, OpaAuthorizer


class AuthenticationError(Exception):
    """Raised when an inbound caller token is not accepted."""


class WorkloadIdentityError(Exception):
    """Raised when ZTWIM/SPIRE cannot issue this pod an identity."""


class IdentityConfigurationError(Exception):
    """Raised when required identity configuration is missing."""


@dataclass(frozen=True)
class RequestIdentity:
    """Validated caller and RCA workload identity for one A2A request."""

    caller_token: str
    caller_claims: dict[str, Any]
    workload_identity: dict[str, str]
    request_id: str

    @property
    def on_behalf_of(self) -> str:
        """Stable caller identity derived only from validated JWT claims."""

        for claim in ("sub", "client_id", "azp", "preferred_username"):
            value = self.caller_claims.get(claim)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "unknown-caller"

    @property
    def actor(self) -> str:
        """The previous actor in a delegated token-exchange chain, if present."""

        act = self.caller_claims.get("act")
        if isinstance(act, dict):
            for claim in ("sub", "client_id"):
                value = act.get(claim)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""


_request_identity: ContextVar[Optional[RequestIdentity]] = ContextVar(
    "rca_agent_request_identity", default=None
)


def current_request_identity() -> RequestIdentity:
    identity = _request_identity.get()
    if identity is None:
        raise AuthenticationError("No authenticated request identity is available")
    return identity


def current_caller_token() -> str:
    """Return the current request's bearer token without persisting it."""

    return current_request_identity().caller_token


class WorkloadIdentityProvider:
    """Fetches the pod's short-lived JWT-SVID from the SPIFFE Workload API."""

    def __init__(self, audience: str, timeout_seconds: float = 5.0) -> None:
        if not audience:
            raise IdentityConfigurationError("SPIFFE_JWT_AUDIENCE must be configured")
        self.audience = audience
        self.timeout_seconds = timeout_seconds

    def get_identity(self) -> dict[str, str]:
        try:
            with WorkloadApiClient(default_timeout=self.timeout_seconds) as client:
                svid = client.fetch_jwt_svid(audience={self.audience})
        except Exception as error:
            raise WorkloadIdentityError("ZTWIM/SPIRE did not issue a JWT-SVID") from error

        spiffe_id = str(getattr(svid, "spiffe_id", ""))
        if not spiffe_id.startswith("spiffe://"):
            raise WorkloadIdentityError("ZTWIM/SPIRE returned an invalid SPIFFE identity")
        # Keep the short-lived JWT-SVID available to outbound clients. The
        # Kubernetes client still uses ServiceAccount RBAC; this token is for
        # services that authenticate workloads through SPIFFE/OIDC.
        jwt_svid = str(getattr(svid, "token", "") or getattr(svid, "jwt_svid", "") or "")
        if not jwt_svid:
            raise WorkloadIdentityError("ZTWIM/SPIRE returned an empty JWT-SVID")
        return {"spiffe_id": spiffe_id, "jwt_svid": jwt_svid}


class KeycloakTokenValidator:
    """Validates bearer tokens using Keycloak discovery and its JWKS keys."""

    def __init__(
        self,
        issuer_url: str,
        audience: str | list[str] | tuple[str, ...],
        ca_bundle: Optional[str] = None,
        timeout_seconds: float = 5.0,
        jwks_cache_seconds: int = 300,
    ) -> None:
        if not issuer_url:
            raise IdentityConfigurationError("KEYCLOAK_ISSUER_URL must be configured")
        audiences = [audience] if isinstance(audience, str) else list(audience)
        if not audiences or any(not item for item in audiences):
            raise IdentityConfigurationError("KEYCLOAK_AUDIENCE must be configured")
        self.issuer_url = issuer_url.rstrip("/")
        self.audiences = tuple(audiences)
        self.verify = ca_bundle or True
        self.timeout_seconds = timeout_seconds
        self.jwks_cache_seconds = jwks_cache_seconds
        self._discovery: Optional[dict[str, Any]] = None
        self._jwks: Optional[dict[str, Any]] = None
        self._jwks_expiry = 0.0
        self._lock = threading.Lock()

    def _get_discovery(self) -> dict[str, Any]:
        if self._discovery is None:
            url = f"{self.issuer_url}/.well-known/openid-configuration"
            try:
                with httpx.Client(verify=self.verify, timeout=self.timeout_seconds) as http:
                    response = http.get(url)
                    response.raise_for_status()
                    discovery = response.json()
            except Exception as error:
                raise AuthenticationError("Keycloak OIDC discovery is unavailable") from error
            if discovery.get("issuer", "").rstrip("/") != self.issuer_url:
                raise AuthenticationError("Keycloak issuer does not match configuration")
            if not discovery.get("jwks_uri"):
                raise AuthenticationError("Keycloak discovery does not contain jwks_uri")
            self._discovery = discovery
        return self._discovery

    def _get_jwks(self, force_refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._jwks is not None and not force_refresh and time.monotonic() < self._jwks_expiry:
                return self._jwks
            jwks_uri = self._get_discovery()["jwks_uri"]
            try:
                with httpx.Client(verify=self.verify, timeout=self.timeout_seconds) as http:
                    response = http.get(jwks_uri)
                    response.raise_for_status()
                    self._jwks = response.json()
            except Exception as error:
                raise AuthenticationError("Keycloak JWKS is unavailable") from error
            self._jwks_expiry = time.monotonic() + self.jwks_cache_seconds
            return self._jwks

    def _key_for_token(self, token: str) -> Any:
        try:
            header = jwt.get_unverified_header(token)
            key_id = header.get("kid")
            algorithm_name = header.get("alg")
        except jwt.PyJWTError as error:
            raise AuthenticationError("Bearer token has an invalid JWT header") from error
        if algorithm_name != "RS256" or not key_id:
            raise AuthenticationError("Bearer token uses an unsupported signing key")

        for refresh in (False, True):
            for jwk in self._get_jwks(force_refresh=refresh).get("keys", []):
                if jwk.get("kid") == key_id:
                    try:
                        return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
                    except (TypeError, ValueError) as error:
                        raise AuthenticationError("Keycloak signing key is invalid") from error
        raise AuthenticationError("Bearer token signing key is not trusted by Keycloak")

    def validate(self, token: str) -> dict[str, Any]:
        if not token:
            raise AuthenticationError("Bearer token is required")
        key = self._key_for_token(token)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=list(self.audiences),
                issuer=self.issuer_url,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
        except jwt.PyJWTError as error:
            raise AuthenticationError("Bearer token was rejected by Keycloak validation") from error
        return claims

    def check_available(self) -> None:
        """Fail if Keycloak discovery cannot be reached or is misconfigured."""

        self._get_discovery()


class KeycloakTokenExchangeError(AuthenticationError):
    """Raised when the RCA cannot exchange a caller token for MCP access."""


class KeycloakTokenExchanger:
    """Exchange the validated caller token for a token scoped to OpenShift MCP.

    The client authenticates with the RCA pod's JWT-SVID. The caller token is
    only the subject of the exchange and is never forwarded to MCP.
    """

    def __init__(
        self,
        issuer_url: str,
        client_id: str,
        audience: str,
        token_url: str = "",
        scope: str = "",
        client_assertion_type: str = "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe",
        ca_bundle: Optional[str] = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not issuer_url:
            raise IdentityConfigurationError("KEYCLOAK_ISSUER_URL must be configured")
        if not client_id:
            raise IdentityConfigurationError("KEYCLOAK_TOKEN_EXCHANGE_CLIENT_ID must be configured")
        if not audience:
            raise IdentityConfigurationError("KEYCLOAK_TOKEN_EXCHANGE_AUDIENCE must be configured")
        self.issuer_url = issuer_url.rstrip("/")
        self.client_id = client_id
        self.audience = audience
        self.token_url = token_url.rstrip("/")
        self.scope = scope
        self.client_assertion_type = client_assertion_type
        self.verify = ca_bundle or True
        self.timeout_seconds = timeout_seconds

    def _token_endpoint(self) -> str:
        if self.token_url:
            return self.token_url
        url = f"{self.issuer_url}/.well-known/openid-configuration"
        try:
            with httpx.Client(verify=self.verify, timeout=self.timeout_seconds) as http:
                response = http.get(url)
                response.raise_for_status()
                endpoint = response.json().get("token_endpoint", "")
        except Exception as error:
            raise KeycloakTokenExchangeError("Keycloak token endpoint discovery failed") from error
        if not endpoint:
            raise KeycloakTokenExchangeError("Keycloak discovery does not contain token_endpoint")
        return endpoint

    def exchange(self, identity: RequestIdentity) -> str:
        # No client_id: Keycloak's JWT client validators reject the request
        # outright ("client_id parameter does not match sub claim") whenever
        # a client_id form parameter is present and differs from the
        # assertion's sub -- and for federated (SPIFFE) assertions sub is a
        # SPIFFE ID, never the Keycloak client_id. The client is resolved
        # from the assertion's sub instead.
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": identity.caller_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "client_assertion_type": self.client_assertion_type,
            "client_assertion": identity.workload_identity["jwt_svid"],
            "audience": self.audience,
        }
        if self.scope:
            data["scope"] = self.scope
        try:
            with httpx.Client(verify=self.verify, timeout=self.timeout_seconds) as http:
                response = http.post(self._token_endpoint(), data=data)
                response.raise_for_status()
                token = response.json().get("access_token")
        except Exception as error:
            raise KeycloakTokenExchangeError("Keycloak token exchange for OpenShift MCP failed") from error
        if not token:
            raise KeycloakTokenExchangeError("Keycloak token exchange did not return access_token")
        return str(token)


def _bearer_token(scope: dict[str, Any]) -> Optional[str]:
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            decoded = value.decode("latin-1")
            scheme, _, token = decoded.partition(" ")
            if scheme.lower() == "bearer" and token:
                return token.strip()
    return None


class A2AAuthenticationMiddleware:
    """Protect the A2A app while leaving public agent-card discovery available."""

    def __init__(
        self,
        app: Any,
        keycloak: KeycloakTokenValidator,
        workload: WorkloadIdentityProvider,
        opa: Optional[OpaAuthorizer] = None,
    ):
        self.app = app
        self.keycloak = keycloak
        self.workload = workload
        # Optional: which callers may invoke this agent at all, decided by a
        # standalone OPA server keyed on the caller's `azp` claim (see
        # opa.py). None preserves this middleware's pre-OPA behavior of
        # trusting any caller Keycloak validates for the configured
        # audience.
        self.opa = opa

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.rstrip("/") == "/.well-known/agent-card.json":
            await self.app(scope, receive, send)
            return

        if path.rstrip("/") == "/health/ready":
            try:
                await asyncio.gather(
                    asyncio.to_thread(self.keycloak.check_available),
                    asyncio.to_thread(self.workload.get_identity),
                )
            except (AuthenticationError, WorkloadIdentityError, IdentityConfigurationError):
                await self._send_error(send, 503, "Keycloak and workload identity are not ready")
                return
            await self._send_json(send, 200, {"status": "ready"})
            return

        token = _bearer_token(scope)
        try:
            claims, workload_identity = await asyncio.gather(
                asyncio.to_thread(self.keycloak.validate, token),
                asyncio.to_thread(self.workload.get_identity),
            )
            scope["rca_agent.identity"] = {
                "caller": claims,
                "workload": workload_identity,
            }
        except (AuthenticationError, WorkloadIdentityError, IdentityConfigurationError):
            await self._send_error(send, 401, "A valid Keycloak bearer token and workload identity are required")
            return

        if self.opa is not None:
            try:
                await asyncio.to_thread(self.opa.authorize, claims)
            except OpaAuthorizationError:
                await self._send_error(send, 403, "Caller is not permitted to invoke this agent")
                return

        request_id = next(
            (value.decode("latin-1").strip() for name, value in scope.get("headers", [])
             if name.lower() == b"x-request-id" and value.strip()),
            "",
        ) or str(uuid4())
        identity = RequestIdentity(
            caller_token=token or "",
            caller_claims=claims,
            workload_identity=workload_identity,
            request_id=request_id,
        )
        scope["rca_agent.identity"] = {
            "caller": claims,
            "workload": workload_identity,
            "request_id": request_id,
        }
        token_context = _request_identity.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_identity.reset(token_context)

    @staticmethod
    async def _send_error(send: Any, status: int, message: str) -> None:
        await A2AAuthenticationMiddleware._send_json(send, status, {"error": message})

    @staticmethod
    async def _send_json(send: Any, status: int, payload: dict[str, str]) -> None:
        body = json.dumps(payload).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})
