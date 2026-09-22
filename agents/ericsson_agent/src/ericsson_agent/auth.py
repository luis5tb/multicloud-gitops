"""Downstream A2A authentication using Keycloak and ZTO identity tokens."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import env_bool


class SpiffeIdentityError(RuntimeError):
    """Raised when ZTWIM/SPIRE cannot issue this pod a JWT-SVID."""


@dataclass(frozen=True)
class AuthSettings:
    mode: str
    token_url: str
    client_id: str
    client_secret: str
    scope: str
    client_auth_method: str
    client_assertion_type: str
    zto_identity_token_file: str
    zto_identity_token: str
    zto_forward_identity: bool
    zto_identity_header: str
    spiffe_enabled: bool
    spiffe_endpoint_socket: str
    spiffe_jwt_audience: str
    spiffe_timeout: float
    tls_verify: bool | str
    timeout: float
    token_refresh_skew: int

    @classmethod
    def from_env(cls) -> "AuthSettings":
        ca_bundle = os.getenv("A2A_CA_BUNDLE", "").strip()
        tls_verify: bool | str = ca_bundle or env_bool(
            os.getenv("A2A_TLS_VERIFY"), default=True
        )
        return cls(
            mode=os.getenv("A2A_AUTH_MODE", "none").strip().lower(),
            token_url=os.getenv("KEYCLOAK_TOKEN_URL", "").strip(),
            client_id=os.getenv("KEYCLOAK_CLIENT_ID", "").strip(),
            client_secret=os.getenv("KEYCLOAK_CLIENT_SECRET", ""),
            scope=os.getenv("KEYCLOAK_SCOPE", "").strip(),
            client_auth_method=os.getenv(
                "KEYCLOAK_CLIENT_AUTH_METHOD", "client_assertion_post"
            ).strip().lower(),
            client_assertion_type=os.getenv(
                "KEYCLOAK_CLIENT_ASSERTION_TYPE",
                "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe",
            ).strip(),
            zto_identity_token_file=os.getenv(
                "ZTO_IDENTITY_TOKEN_FILE", "/var/run/secrets/zto/identity-token"
            ).strip(),
            zto_identity_token=os.getenv("ZTO_IDENTITY_TOKEN", "").strip(),
            zto_forward_identity=env_bool(os.getenv("ZTO_FORWARD_IDENTITY")),
            zto_identity_header=os.getenv(
                "ZTO_IDENTITY_HEADER", "X-ZTO-Identity"
            ).strip(),
            spiffe_enabled=env_bool(os.getenv("SPIFFE_ENABLED")),
            spiffe_endpoint_socket=os.getenv("SPIFFE_ENDPOINT_SOCKET", "").strip(),
            spiffe_jwt_audience=os.getenv("SPIFFE_JWT_AUDIENCE", "").strip(),
            spiffe_timeout=float(os.getenv("SPIFFE_TIMEOUT_SECONDS", "5")),
            tls_verify=tls_verify,
            timeout=float(os.getenv("A2A_REQUEST_TIMEOUT", "60")),
            token_refresh_skew=int(os.getenv("KEYCLOAK_TOKEN_REFRESH_SKEW", "30")),
        )


class DownstreamAuth:
    """Fetch, cache, and attach credentials to downstream HTTP requests."""

    def __init__(self, settings: AuthSettings | None = None) -> None:
        self.settings = settings or AuthSettings.from_env()
        self._access_token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._token_client = httpx.AsyncClient(
            timeout=self.settings.timeout, verify=self.settings.tls_verify
        )

    def _identity_token(self, *, required: bool = False) -> str:
        """Fetch the ZTO identity, preferring a fresh SPIFFE JWT-SVID.

        A JWT-SVID is short-lived (minutes), so it is fetched fresh from
        ZTWIM/SPIRE on every call rather than cached like the file/env
        fallback below.
        """

        if self.settings.spiffe_enabled:
            return self._spiffe_jwt_svid()

        token = ""
        if self.settings.zto_identity_token_file:
            try:
                token = Path(self.settings.zto_identity_token_file).read_text(
                    encoding="utf-8"
                ).strip()
            except FileNotFoundError:
                pass
        token = token or self.settings.zto_identity_token
        if required and not token:
            raise RuntimeError(
                "Keycloak client assertion authentication requires a ZTO identity "
                "token in ZTO_IDENTITY_TOKEN_FILE or ZTO_IDENTITY_TOKEN"
            )
        return token

    def _spiffe_jwt_svid(self) -> str:
        """Fetch a short-lived JWT-SVID from ZTWIM/SPIRE's Workload API."""

        from spiffe import WorkloadApiClient

        if not self.settings.spiffe_jwt_audience:
            raise SpiffeIdentityError("SPIFFE_JWT_AUDIENCE must be configured")
        try:
            with WorkloadApiClient(
                socket_path=self.settings.spiffe_endpoint_socket or None,
                default_timeout=self.settings.spiffe_timeout,
            ) as client:
                svid = client.fetch_jwt_svid(
                    audience={self.settings.spiffe_jwt_audience}
                )
        except Exception as error:
            raise SpiffeIdentityError(
                "ZTWIM/SPIRE did not issue a JWT-SVID"
            ) from error
        token = str(getattr(svid, "token", "") or getattr(svid, "jwt_svid", "") or "")
        if not token:
            raise SpiffeIdentityError("ZTWIM/SPIRE returned an empty JWT-SVID")
        return token

    async def _keycloak_token(self) -> str:
        if not self.settings.token_url or not self.settings.client_id:
            raise RuntimeError(
                "KEYCLOAK_TOKEN_URL and KEYCLOAK_CLIENT_ID are required when "
                "A2A_AUTH_MODE=keycloak"
            )

        data = {"grant_type": "client_credentials"}
        if self.settings.scope:
            data["scope"] = self.settings.scope

        auth: httpx.BasicAuth | None = None
        method = self.settings.client_auth_method
        if method == "client_secret_basic":
            if not self.settings.client_secret:
                raise RuntimeError("KEYCLOAK_CLIENT_SECRET is required for client_secret_basic")
            auth = httpx.BasicAuth(self.settings.client_id, self.settings.client_secret)
        elif method == "client_secret_post":
            if not self.settings.client_secret:
                raise RuntimeError("KEYCLOAK_CLIENT_SECRET is required for client_secret_post")
            data.update(
                client_id=self.settings.client_id,
                client_secret=self.settings.client_secret,
            )
        elif method == "client_assertion_post":
            data.update(
                client_id=self.settings.client_id,
                client_assertion_type=self.settings.client_assertion_type,
                client_assertion=self._identity_token(required=True),
            )
        else:
            raise RuntimeError(
                "KEYCLOAK_CLIENT_AUTH_METHOD must be client_assertion_post, "
                "client_secret_basic, or client_secret_post"
            )

        response = await self._token_client.post(
            self.settings.token_url, data=data, auth=auth
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Keycloak token response did not contain access_token")
        expires_in = int(payload.get("expires_in", 300))
        self._access_token = token
        self._expires_at = time.monotonic() + max(expires_in, 1)
        return token

    async def bearer_token(self) -> str:
        if self.settings.mode == "none":
            return ""
        if self.settings.mode == "static":
            token = os.getenv("A2A_BEARER_TOKEN", "").strip()
            if not token:
                raise RuntimeError("A2A_BEARER_TOKEN is required for static auth")
            return token
        if self.settings.mode != "keycloak":
            raise RuntimeError("A2A_AUTH_MODE must be none, static, or keycloak")

        async with self._lock:
            if self._access_token and time.monotonic() < (
                self._expires_at - self.settings.token_refresh_skew
            ):
                return self._access_token
            return await self._keycloak_token()

    async def add_auth(self, request: httpx.Request) -> None:
        token = await self.bearer_token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        if self.settings.zto_forward_identity:
            identity = self._identity_token(required=True)
            request.headers[self.settings.zto_identity_header] = identity

    async def close(self) -> None:
        await self._token_client.aclose()
