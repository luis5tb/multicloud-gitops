"""Downstream A2A authentication and request-scoped IdP selection."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import httpx

from .cluster_context import ClusterIdMissingError, get_cluster_id
from .config import env_bool

logger = logging.getLogger(__name__)

# Bound by stamp_request_id for the lifetime of an outbound httpx request so
# Keycloak mint audits can join the later dispatch line on the same id.
_outbound_request_id: ContextVar[str] = ContextVar("acme_outbound_request_id", default="")
_inbound_request_id: ContextVar[str] = ContextVar("acme_inbound_request_id", default="")
_auth_selection: ContextVar[AuthSelection | None] = ContextVar(
    "acme_auth_selection", default=None
)
_token_debug_id: ContextVar[str] = ContextVar("acme_token_debug_id", default="")
_token_override: ContextVar[TokenOverride | None] = ContextVar(
    "acme_token_override", default=None
)


def _tls_context_trusting(ca_bundle: str) -> ssl.SSLContext:
    """Trust the system's default CAs *plus* ca_bundle, not ca_bundle alone.

    httpx's ``verify=<path>`` (and ``ssl.create_default_context(cafile=...)``
    underneath it) replaces the default trust store entirely with just that
    file -- fine for a self-signed internal ingress CA (a single root cert
    is enough on its own), but breaks verification when the real ingress
    certificate chains to a public CA (e.g. ZeroSSL/Let's Encrypt) and the
    synced managed-ingress-CA bundle only has the leaf's intermediate chain,
    not the actual trusted root. load_verify_locations() on top of
    create_default_context() adds ca_bundle as an extra trust anchor
    instead, so both cases work.
    """
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=ca_bundle)
    return context


# The routing header name is a frozen interface decision shared with Praxis
# and the OLS cluster registry; see LIGHTSPEED_DESIGN.md.
OLS_CLUSTER_HEADER = "X-OLS-Cluster"

# Correlation id header stamped on outbound JSON-RPC calls. OLS reads the same
# header in a2a_auth.py and echoes the value into its per-request audit line,
# so an acme-side "dispatched" record and OLS's "did X on behalf of acme-agent"
# record share one id and can be joined across the two services.
REQUEST_ID_HEADER = "X-Request-Id"

# Request-scoped IdP selection headers. They are accepted only from the
# inbound ASGI middleware, then carried to the outbound httpx hook through a
# ContextVar; they are never copied into the downstream request.
AUTH_PROVIDER_HEADER = "X-ACME-Auth-Provider"
AUTH_ENVIRONMENT_HEADER = "X-ACME-Auth-Environment"
AUTH_TOKEN_TYPE_HEADER = "X-ACME-Auth-Token-Type"
TOKEN_DEBUG_ID_HEADER = "X-ACME-Token-Debug-Id"
# Testing aid, accepted only when ACME_ALLOW_TOKEN_OVERRIDE=true: a bearer
# token pasted in the UI that replaces Token A for this request's downstream
# call. Renewals always mint fresh tokens with the selected provider.
TOKEN_OVERRIDE_HEADER = "X-ACME-Auth-Token"
MAX_OVERRIDE_TOKEN_LENGTH = 16384


class SpiffeIdentityError(RuntimeError):
    """Raised when ZTWIM/SPIRE cannot issue this pod a JWT-SVID."""


@dataclass(frozen=True)
class AuthSelection:
    provider: str
    environment: str
    token_type: str


@dataclass
class TokenOverride:
    """A pasted token for one inbound request, until a renewal replaces it.

    Mutable on purpose: the renewal transport deactivates it from inside the
    downstream response stream, and that change must be visible to any later
    downstream call made for the same inbound request.
    """

    token: str
    active: bool = True


@dataclass(frozen=True)
class CapturedAuth:
    """Request-scoped auth state captured when a downstream call was sent."""

    selection: AuthSelection | None
    override: TokenOverride | None
    debug_id: str


@dataclass(frozen=True)
class RedHatSsoEnvironment:
    token_url: str
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class AuthSettings:
    mode: str
    default_provider: str
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
    tls_verify: bool | str | ssl.SSLContext
    timeout: float
    token_refresh_skew: int
    redhat_default_environment: str
    redhat_default_token_type: str
    redhat_scope: str
    redhat_environments: dict[str, RedHatSsoEnvironment]
    token_debug_enabled: bool
    token_debug_ttl: float
    token_override_enabled: bool = False

    @classmethod
    def from_env(cls) -> "AuthSettings":
        ca_bundle = os.getenv("A2A_CA_BUNDLE", "").strip()
        tls_verify: bool | str | ssl.SSLContext = (
            _tls_context_trusting(ca_bundle)
            if ca_bundle
            else env_bool(os.getenv("A2A_TLS_VERIFY"), default=True)
        )
        return cls(
            mode=os.getenv("A2A_AUTH_MODE", "none").strip().lower(),
            default_provider=os.getenv("A2A_AUTH_PROVIDER", "keycloak").strip().lower(),
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
            redhat_default_environment=os.getenv(
                "REDHAT_SSO_DEFAULT_ENVIRONMENT", "production"
            ).strip().lower(),
            redhat_default_token_type=os.getenv(
                "REDHAT_SSO_TOKEN_TYPE", "agent"
            ).strip().lower(),
            redhat_scope=os.getenv("REDHAT_SSO_SCOPE", "").strip(),
            redhat_environments={
                "production": RedHatSsoEnvironment(
                    token_url=os.getenv("REDHAT_SSO_PRODUCTION_TOKEN_URL", "").strip(),
                    client_id=os.getenv(
                        "REDHAT_SSO_PRODUCTION_CLIENT_ID", ""
                    ).strip(),
                    client_secret=os.getenv(
                        "REDHAT_SSO_PRODUCTION_CLIENT_SECRET", ""
                    ),
                ),
                "stage": RedHatSsoEnvironment(
                    token_url=os.getenv("REDHAT_SSO_STAGE_TOKEN_URL", "").strip(),
                    client_id=os.getenv("REDHAT_SSO_STAGE_CLIENT_ID", "").strip(),
                    client_secret=os.getenv("REDHAT_SSO_STAGE_CLIENT_SECRET", ""),
                ),
            },
            token_debug_enabled=env_bool(os.getenv("AUTH_TOKEN_DEBUG_ENABLED")),
            token_debug_ttl=float(os.getenv("AUTH_TOKEN_DEBUG_TTL_SECONDS", "300")),
            token_override_enabled=env_bool(os.getenv("ACME_ALLOW_TOKEN_OVERRIDE")),
        )

    def redhat_environment(self, environment: str) -> RedHatSsoEnvironment | None:
        configured = self.redhat_environments.get(environment)
        if (
            configured
            and configured.token_url
            and configured.client_id
            and configured.client_secret
        ):
            return configured
        return None

    def ui_options(self) -> dict[str, object]:
        """Describe the choices the embedded UI is allowed to present."""

        environments = [
            {
                "id": environment,
                "label": "sso.redhat.com"
                if environment == "production"
                else "sso.stage.redhat.com",
                "available": self.redhat_environment(environment) is not None,
            }
            for environment in ("production", "stage")
        ]
        return {
            "selectionEnabled": self.mode == "keycloak",
            "defaultProvider": self.default_provider,
            "defaultEnvironment": self.redhat_default_environment,
            "defaultTokenType": self.redhat_default_token_type,
            "redHatEnabled": any(item["available"] for item in environments),
            "environments": environments,
            "tokenDebugEnabled": self.token_debug_enabled,
            "tokenOverrideEnabled": self.token_override_enabled
            and self.mode != "none",
        }


class DownstreamAuth:
    """Fetch, cache, and attach credentials to downstream HTTP requests."""

    def __init__(self, settings: AuthSettings | None = None) -> None:
        self.settings = settings or AuthSettings.from_env()
        self._tokens: dict[tuple[str, str, str], tuple[str, float]] = {}
        self._lock = asyncio.Lock()
        self._debug_tokens: dict[str, tuple[str, AuthSelection, float]] = {}
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

    async def _redhat_sso_agent_token(self, environment: str) -> str:
        configured = self.settings.redhat_environment(environment)
        if configured is None:
            prefix = "REDHAT_SSO_" + environment.upper()
            raise RuntimeError(
                f"Red Hat SSO {environment} is not configured: "
                f"{prefix}_TOKEN_URL, {prefix}_CLIENT_ID, and "
                f"{prefix}_CLIENT_SECRET are required"
            )

        data = {
            "grant_type": "client_credentials",
            "client_id": configured.client_id,
            "client_secret": configured.client_secret,
        }
        if self.settings.redhat_scope:
            data["scope"] = self.settings.redhat_scope

        request_id = _outbound_request_id.get() or "-"
        cluster = get_cluster_id() or ""
        action = f'"mint Red Hat SSO {environment} agent Token A"'
        try:
            response = await self._token_client.post(configured.token_url, data=data)
            if response.is_error:
                error_detail = _oauth_error_detail(response)
                logger.info(
                    "acme_audit request_id=%s actor=acme-agent cluster=%s "
                    "action=%s outcome=error status=%s error=%s",
                    request_id,
                    cluster,
                    action,
                    response.status_code,
                    error_detail,
                )
                raise RuntimeError(
                    "Red Hat SSO token request failed with HTTP "
                    f"{response.status_code}: {error_detail}"
                )
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise RuntimeError(
                    "Red Hat SSO token response did not contain access_token"
                )
        except httpx.HTTPStatusError as error:
            logger.info(
                "acme_audit request_id=%s actor=acme-agent cluster=%s "
                "action=%s outcome=error status=%s",
                request_id,
                cluster,
                action,
                error.response.status_code,
            )
            raise
        except Exception:
            logger.info(
                "acme_audit request_id=%s actor=acme-agent cluster=%s "
                "action=%s outcome=error",
                request_id,
                cluster,
                action,
            )
            raise
        expires_in = int(payload.get("expires_in", 300))
        self._store_token(("redhat", environment, "agent"), token, expires_in)
        logger.info(
            "acme_audit request_id=%s actor=acme-agent cluster=%s "
            "action=%s outcome=ok expires_in=%s",
            request_id,
            cluster,
            action,
            expires_in,
        )
        return token

    def _store_token(
        self, cache_key: tuple[str, str, str], token: str, expires_in: int
    ) -> None:
        self._tokens[cache_key] = (
            token,
            time.monotonic() + max(expires_in, 1),
        )

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
            # No client_id here: Keycloak's JWT client validators reject the
            # request outright ("client_id parameter does not match sub
            # claim") whenever a client_id form parameter is present and
            # differs from the assertion's sub -- and for federated (SPIFFE)
            # assertions sub is a SPIFFE ID, never the Keycloak client_id.
            # The client is resolved from the assertion's sub instead.
            data.update(
                client_assertion_type=self.settings.client_assertion_type,
                client_assertion=self._identity_token(required=True),
            )
        else:
            raise RuntimeError(
                "KEYCLOAK_CLIENT_AUTH_METHOD must be client_assertion_post, "
                "client_secret_basic, or client_secret_post"
            )

        request_id = _outbound_request_id.get() or "-"
        cluster = get_cluster_id() or ""
        try:
            response = await self._token_client.post(
                self.settings.token_url, data=data, auth=auth
            )
            response.raise_for_status()
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                raise RuntimeError(
                    "Keycloak token response did not contain access_token"
                )
        except httpx.HTTPStatusError as error:
            logger.info(
                "acme_audit request_id=%s actor=acme-agent cluster=%s "
                "action=%s outcome=error status=%s",
                request_id,
                cluster,
                '"mint Keycloak Token A"',
                error.response.status_code,
            )
            raise
        except Exception:
            logger.info(
                "acme_audit request_id=%s actor=acme-agent cluster=%s "
                "action=%s outcome=error",
                request_id,
                cluster,
                '"mint Keycloak Token A"',
            )
            raise
        expires_in = int(payload.get("expires_in", 300))
        self._store_token(("keycloak", "", ""), token, expires_in)
        logger.info(
            "acme_audit request_id=%s actor=acme-agent cluster=%s "
            "action=%s outcome=ok expires_in=%s",
            request_id,
            cluster,
            '"mint Keycloak Token A"',
            expires_in,
        )
        return token

    def _default_selection(self) -> AuthSelection:
        return AuthSelection(
            provider=self.settings.default_provider,
            environment=self.settings.redhat_default_environment,
            token_type=self.settings.redhat_default_token_type,
        )

    async def bearer_token(
        self,
        selection: AuthSelection | None = None,
        *,
        override: TokenOverride | None = None,
        force_refresh: bool = False,
    ) -> str:
        if self.settings.mode == "none":
            return ""
        selection = selection or _auth_selection.get() or self._default_selection()
        override = override if override is not None else _token_override.get()
        if override is not None and override.active and override.token:
            self._store_debug_token(override.token, selection)
            return override.token
        if self.settings.mode == "static":
            token = os.getenv("A2A_BEARER_TOKEN", "").strip()
            if not token:
                raise RuntimeError("A2A_BEARER_TOKEN is required for static auth")
            self._store_debug_token(token, selection)
            return token
        if self.settings.mode != "keycloak":
            raise RuntimeError("A2A_AUTH_MODE must be none, static, or keycloak")

        async with self._lock:
            if selection.provider not in {"keycloak", "redhat"}:
                raise RuntimeError("A2A_AUTH_PROVIDER must be keycloak or redhat")
            if selection.provider == "keycloak":
                cache_key = ("keycloak", "", "")
                if force_refresh:
                    self._tokens.pop(cache_key, None)
                cached = self._cached_token(cache_key)
                if cached:
                    token = cached
                else:
                    token = await self._keycloak_token()
                self._store_debug_token(token, selection)
                return token

            if selection.environment not in {"production", "stage"}:
                raise RuntimeError(
                    "Red Hat SSO environment must be production or stage"
                )
            if selection.token_type != "agent":
                raise NotImplementedError(
                    "Red Hat IdP user tokens are not implemented yet; select the "
                    "agent token type"
                )
            cache_key = ("redhat", selection.environment, "agent")
            if force_refresh:
                self._tokens.pop(cache_key, None)
            cached = self._cached_token(cache_key)
            if cached:
                token = cached
            else:
                token = await self._redhat_sso_agent_token(selection.environment)
            self._store_debug_token(token, selection)
            return token

    def _cached_token(self, cache_key: tuple[str, str, str]) -> str:
        entry = self._tokens.get(cache_key)
        if not entry:
            return ""
        token, expires_at = entry
        if time.monotonic() < expires_at - self.settings.token_refresh_skew:
            return token
        self._tokens.pop(cache_key, None)
        return ""

    def _store_debug_token(self, token: str, selection: AuthSelection) -> None:
        debug_id = _token_debug_id.get()
        if not self.settings.token_debug_enabled or not debug_id or not token:
            return
        self._purge_expired_debug_tokens()
        self._debug_tokens[debug_id] = (
            token,
            selection,
            time.monotonic() + self.settings.token_debug_ttl,
        )

    def pop_debug_token(self, debug_id: str) -> dict[str, str] | None:
        if not self.settings.token_debug_enabled or not debug_id:
            return None
        self._purge_expired_debug_tokens()
        entry = self._debug_tokens.pop(debug_id, None)
        if not entry:
            return None
        token, selection, expires_at = entry
        if time.monotonic() >= expires_at:
            return None
        return {
            "accessToken": token,
            "provider": selection.provider,
            "environment": selection.environment,
            "tokenType": selection.token_type,
        }

    def _purge_expired_debug_tokens(self) -> None:
        now = time.monotonic()
        expired = [
            debug_id
            for debug_id, (_, _, expires_at) in self._debug_tokens.items()
            if now >= expires_at
        ]
        for debug_id in expired:
            self._debug_tokens.pop(debug_id, None)

    async def add_auth(self, request: httpx.Request) -> None:
        token = await self.bearer_token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        if self.settings.zto_forward_identity:
            identity = self._identity_token(required=True)
            request.headers[self.settings.zto_identity_header] = identity

    def renewal_supported(self) -> bool:
        """Whether ACME can mint a fresh token when a downstream asks for one."""
        return self.settings.mode == "keycloak"

    def capture(self) -> CapturedAuth:
        """Snapshot the request-scoped selection for later renewals."""
        return CapturedAuth(
            selection=_auth_selection.get(),
            override=_token_override.get(),
            debug_id=_token_debug_id.get(),
        )

    async def reauthorize(self, request: httpx.Request, captured: CapturedAuth) -> None:
        """Attach a freshly minted token for the captured provider selection.

        A downstream refused the current token, so a pasted override token is
        retired and the cached token for this selection is never reused: the
        renewal must carry a different, newly issued token for the same
        principal (same provider and environment as the original call).
        """
        if captured.override is not None:
            captured.override.active = False
        selection = captured.selection or self._default_selection()
        debug_token = _token_debug_id.set(captured.debug_id)
        try:
            token = await self.bearer_token(
                selection, override=captured.override, force_refresh=True
            )
        finally:
            _token_debug_id.reset(debug_token)
        if not token:
            raise RuntimeError("No downstream token could be minted for renewal")
        request.headers["Authorization"] = f"Bearer {token}"
        if self.settings.zto_forward_identity:
            identity = self._identity_token(required=True)
            request.headers[self.settings.zto_identity_header] = identity

    async def close(self) -> None:
        await self._token_client.aclose()


def _oauth_error_detail(response: httpx.Response) -> str:
    """Return a compact OAuth error without echoing credentials or a token."""

    try:
        payload = response.json()
    except ValueError:
        return response.text[:500].strip() or "(empty response body)"
    error = str(payload.get("error", "")).strip()
    description = str(payload.get("error_description", "")).strip()
    if error and description:
        return f"{error}: {description}"
    return error or description or "(empty OAuth error body)"


def _header(scope: dict[str, object], name: str) -> str:
    headers = scope.get("headers") or []
    if not isinstance(headers, list):
        return ""
    for header in headers:
        if not isinstance(header, (tuple, list)) or len(header) != 2:
            continue
        key, value = header
        if key.decode("latin-1").lower() == name.lower():
            return value.decode("latin-1").strip()
    return ""


class AuthSelectionMiddleware:
    """Validate and bind request-scoped IdP selection before ADK runs.

    The selection deliberately never becomes a downstream authorization claim.
    It only chooses which already-configured client credential ACME uses to
    mint Token A. The downstream service still independently validates that
    token's issuer, audience, signature, and principal allow-list.
    """

    def __init__(self, app, settings: AuthSettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        tokens = []
        provider = _header(scope, AUTH_PROVIDER_HEADER) or self.settings.default_provider
        environment = (
            _header(scope, AUTH_ENVIRONMENT_HEADER)
            or self.settings.redhat_default_environment
        )
        token_type = (
            _header(scope, AUTH_TOKEN_TYPE_HEADER)
            or self.settings.redhat_default_token_type
        )

        if self.settings.mode != "keycloak":
            selection = AuthSelection(provider, environment, token_type)
        elif provider not in {"keycloak", "redhat"}:
            message = f"Unsupported identity provider: {provider}"
            await self._reject(scope, send, message)
            return
        elif provider == "redhat":
            if environment not in {"production", "stage"}:
                message = f"Unsupported Red Hat SSO environment: {environment}"
                await self._reject(scope, send, message)
                return
            if token_type not in {"agent", "user"}:
                message = f"Unsupported Red Hat IdP token type: {token_type}"
                await self._reject(scope, send, message)
                return
            if token_type == "user":
                message = (
                    "Red Hat IdP user tokens are not implemented yet; select "
                    "agent token"
                )
                await self._reject(scope, send, message)
                return
            if self.settings.redhat_environment(environment) is None:
                message = f"Red Hat SSO {environment} is not configured"
                await self._reject(scope, send, message)
                return
            selection = AuthSelection(provider, environment, token_type)
        else:
            selection = AuthSelection(provider, "", "")

        override_token = _header(scope, TOKEN_OVERRIDE_HEADER)
        if override_token:
            if not self.settings.token_override_enabled or self.settings.mode == "none":
                await self._reject(scope, send, "Token override is not enabled")
                return
            if (
                len(override_token) > MAX_OVERRIDE_TOKEN_LENGTH
                or not override_token.isprintable()
                or " " in override_token
            ):
                await self._reject(scope, send, "Invalid override token")
                return

        request_id = _header(scope, REQUEST_ID_HEADER)
        debug_id = _header(scope, TOKEN_DEBUG_ID_HEADER)
        if request_id and (len(request_id) > 128 or not request_id.isprintable()):
            await self._reject(scope, send, "Invalid X-Request-Id")
            return
        if debug_id and (len(debug_id) > 128 or not debug_id.isprintable()):
            await self._reject(scope, send, "Invalid token debug id")
            return

        tokens.append(_auth_selection.set(selection))
        if request_id:
            tokens.append(_inbound_request_id.set(request_id))
        if debug_id:
            tokens.append(_token_debug_id.set(debug_id))
        if override_token:
            tokens.append(_token_override.set(TokenOverride(token=override_token)))
        try:
            await self.app(scope, receive, send)
        finally:
            for token in reversed(tokens):
                token.var.reset(token)

    async def _reject(self, scope, send, message: str) -> None:
        body = json.dumps({"detail": message}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})


async def add_cluster_header(request: httpx.Request) -> None:
    """Attach the invocation-scoped ``X-OLS-Cluster`` routing header.

    Registered as a second ``httpx`` request event hook alongside
    ``DownstreamAuth.add_auth`` (see ``agent.py``), so the existing
    Keycloak/SPIFFE bearer logic above is untouched; this only adds the
    routing header ACME must also send.

    The header is added only to authenticated JSON-RPC calls (HTTP POST),
    never to the public, unauthenticated agent-card fetch (HTTP GET): the
    card -- advertising the single Praxis HTTPS origin -- must stay
    discoverable before a cluster is ever selected. Its value comes from
    ``cluster_context``, a ``contextvars.ContextVar`` bound by
    ``routing.ClusterRoutingMiddleware`` for the lifetime of the inbound
    request that triggered this downstream RPC, so concurrent conversations
    targeting different clusters never share a value: there is no
    process-global mutable header on the shared ``httpx.AsyncClient``.

    Raises:
        ClusterIdMissingError: if a JSON-RPC POST would otherwise go out
            with no validated cluster id bound to this invocation. This
            should be unreachable -- the routing middleware rejects any
            message that fails validation before the agent runs -- but
            this hook fails closed rather than ever send an unrouted or
            wrongly-defaulted authenticated RPC.
    """

    if request.method != "POST":
        return
    cluster_id = get_cluster_id()
    if not cluster_id:
        raise ClusterIdMissingError(
            "refusing to send an authenticated A2A RPC with no validated "
            f"{OLS_CLUSTER_HEADER} id bound to this invocation"
        )
    request.headers[OLS_CLUSTER_HEADER] = cluster_id


async def stamp_request_id(request: httpx.Request) -> None:
    """Stamp ``X-Request-Id`` on outbound JSON-RPC POSTs (no audit line).

    Runs *before* ``DownstreamAuth.add_auth`` so a Keycloak Token A mint
    audit can join the later dispatch line on the same id. Preserves an
    existing inbound correlation id when present.
    """

    if request.method != "POST":
        return
    request_id = (
        request.headers.get(REQUEST_ID_HEADER)
        or _inbound_request_id.get()
        or uuid.uuid4().hex
    )
    request.headers[REQUEST_ID_HEADER] = request_id
    _outbound_request_id.set(request_id)


async def log_dispatch_audit(request: httpx.Request) -> None:
    """Emit the outbound ``acme_audit`` dispatch line after auth attaches.

    Registered last in the httpx hook chain (see ``agent.py``) so chronological
    order is: stamp id → mint Token A (if needed) → cluster header → dispatch.
    """

    if request.method != "POST":
        return
    request_id = request.headers.get(REQUEST_ID_HEADER) or _outbound_request_id.get()
    if not request_id:
        request_id = uuid.uuid4().hex
        request.headers[REQUEST_ID_HEADER] = request_id
    logger.info(
        "acme_audit request_id=%s actor=acme-agent cluster=%s "
        "action=%s outcome=sent",
        request_id,
        get_cluster_id() or "",
        '"dispatch A2A query to OLS"',
    )


async def add_request_id(request: httpx.Request) -> None:
    """Stamp ``X-Request-Id`` and log the dispatch audit (test/compat helper).

    Production hooks use ``stamp_request_id`` then ``log_dispatch_audit``
    around auth so mint and dispatch share one id; tests may call this
    combined helper.
    """

    await stamp_request_id(request)
    await log_dispatch_audit(request)
