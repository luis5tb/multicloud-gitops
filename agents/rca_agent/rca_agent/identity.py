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
from typing import Any, Optional

import httpx
import jwt
from spiffe import WorkloadApiClient


class AuthenticationError(Exception):
    """Raised when an inbound caller token is not accepted."""


class WorkloadIdentityError(Exception):
    """Raised when ZTWIM/SPIRE cannot issue this pod an identity."""


class IdentityConfigurationError(Exception):
    """Raised when required identity configuration is missing."""


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
        audience: str,
        ca_bundle: Optional[str] = None,
        timeout_seconds: float = 5.0,
        jwks_cache_seconds: int = 300,
    ) -> None:
        if not issuer_url:
            raise IdentityConfigurationError("KEYCLOAK_ISSUER_URL must be configured")
        if not audience:
            raise IdentityConfigurationError("KEYCLOAK_AUDIENCE must be configured")
        self.issuer_url = issuer_url.rstrip("/")
        self.audience = audience
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
                audience=self.audience,
                issuer=self.issuer_url,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
        except jwt.PyJWTError as error:
            raise AuthenticationError("Bearer token was rejected by Keycloak validation") from error
        return claims

    def check_available(self) -> None:
        """Fail if Keycloak discovery cannot be reached or is misconfigured."""

        self._get_discovery()


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

    def __init__(self, app: Any, keycloak: KeycloakTokenValidator, workload: WorkloadIdentityProvider):
        self.app = app
        self.keycloak = keycloak
        self.workload = workload

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

        try:
            claims, workload_identity = await asyncio.gather(
                asyncio.to_thread(self.keycloak.validate, _bearer_token(scope)),
                asyncio.to_thread(self.workload.get_identity),
            )
            scope["rca_agent.identity"] = {
                "caller": claims,
                "workload": workload_identity,
            }
        except (AuthenticationError, WorkloadIdentityError, IdentityConfigurationError):
            await self._send_error(send, 401, "A valid Keycloak bearer token and workload identity are required")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _send_error(send: Any, status: int, message: str) -> None:
        await A2AAuthenticationMiddleware._send_json(send, status, {"error": message})

    @staticmethod
    async def _send_json(send: Any, status: int, payload: dict[str, str]) -> None:
        body = json.dumps(payload).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})
