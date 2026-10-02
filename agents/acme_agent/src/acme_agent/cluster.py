"""Request-scoped OpenShift cluster selection for downstream A2A calls."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


CLUSTER_HEADER = "X-OLS-Cluster"
MAX_INBOUND_BODY_BYTES = 1024 * 1024
SUPPORTED_INBOUND_METHOD = "message/send"
_CLUSTER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+")
_ACTIVE_CLUSTER: ContextVar["ClusterTarget | None"] = ContextVar(
    "acme_active_cluster", default=None
)


class ClusterRegistryError(ValueError):
    """Raised when the configured cluster allow-list is invalid."""


class ClusterSelectionError(ValueError):
    """Raised when a request does not identify one registered cluster."""


@dataclass(frozen=True)
class ClusterTarget:
    """A canonical API URL and its stable, downstream routing identifier."""

    cluster_id: str
    api_url: str


def normalize_api_url(value: str) -> str:
    """Canonicalize an HTTPS API origin without accepting URL subresources."""

    if not isinstance(value, str):
        raise ValueError("OpenShift API URL must be a string")
    raw = value.strip()
    if not raw or any(character.isspace() for character in raw):
        raise ValueError("OpenShift API URL must not be empty or contain whitespace")

    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError("OpenShift API URL is malformed") from error

    if parsed.scheme.lower() != "https":
        raise ValueError("OpenShift API URL must use HTTPS")
    if not parsed.netloc or not hostname:
        raise ValueError("OpenShift API URL must include a host")
    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise ValueError("OpenShift API URL must not contain credentials")
    if parsed.path or "?" in raw or "#" in raw:
        raise ValueError(
            "OpenShift API URL must not contain a path, query, or fragment"
        )
    if parsed.netloc.endswith(":"):
        raise ValueError("OpenShift API URL contains an empty port")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("OpenShift API URL port must be between 1 and 65535")
    if "%" in hostname:
        # Reject IPv6 zone identifiers and escaped hostnames. They are not a
        # stable cluster identity and are unnecessary for public API origins.
        raise ValueError("OpenShift API URL host is not a canonical hostname")

    hostname = hostname.rstrip(".")
    if not hostname:
        raise ValueError("OpenShift API URL must include a host")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise ValueError("OpenShift API URL host is invalid") from error
        if len(ascii_hostname) > 253 or any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in ascii_hostname.split(".")
        ):
            raise ValueError("OpenShift API URL host is invalid")
        normalized_host = ascii_hostname
    else:
        if isinstance(address, ipaddress.IPv6Address):
            normalized_host = f"[{address.compressed}]"
        else:
            normalized_host = str(address)

    normalized_port = "" if port in (None, 443) else f":{port}"
    return f"https://{normalized_host}{normalized_port}"


class ClusterRegistry:
    """Immutable allow-list mapping canonical API origins to stable IDs."""

    def __init__(self, entries: dict[str, str]) -> None:
        if not isinstance(entries, dict) or not entries:
            raise ClusterRegistryError("cluster registry must be a non-empty object")

        by_url: dict[str, ClusterTarget] = {}
        for cluster_id, api_url in entries.items():
            if not isinstance(cluster_id, str) or not _CLUSTER_ID_RE.fullmatch(
                cluster_id
            ):
                raise ClusterRegistryError(
                    "cluster registry keys must be stable IDs containing only "
                    "letters, digits, dots, underscores, and hyphens"
                )
            try:
                canonical_url = normalize_api_url(api_url)
            except (TypeError, ValueError) as error:
                raise ClusterRegistryError(
                    f"cluster registry URL for {cluster_id!r} is invalid"
                ) from error
            if canonical_url in by_url:
                raise ClusterRegistryError(
                    "cluster registry contains multiple IDs for the same API URL"
                )
            by_url[canonical_url] = ClusterTarget(cluster_id, canonical_url)
        self._by_url = by_url

    @classmethod
    def from_env(cls, value: str | None = None) -> "ClusterRegistry":
        """Load the required ID-to-API-URL JSON map from the environment."""

        raw = os.getenv("OLS_CLUSTER_REGISTRY_JSON", "") if value is None else value
        if not raw.strip():
            raise ClusterRegistryError(
                "OLS_CLUSTER_REGISTRY_JSON must configure at least one approved cluster"
            )
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ClusterRegistryError(
                "OLS_CLUSTER_REGISTRY_JSON must contain valid JSON"
            ) from error
        if not isinstance(entries, dict):
            raise ClusterRegistryError(
                "OLS_CLUSTER_REGISTRY_JSON must be an ID-to-API-URL object"
            )
        return cls(entries)

    def resolve(self, api_url: str) -> ClusterTarget:
        """Return only the registered ID for a canonicalized API URL."""

        try:
            canonical_url = normalize_api_url(api_url)
        except (TypeError, ValueError) as error:
            raise ClusterSelectionError(
                "Provide an HTTPS OpenShift API URL without credentials, path, "
                "query, or fragment."
            ) from error
        target = self._by_url.get(canonical_url)
        if target is None:
            raise ClusterSelectionError("The OpenShift API URL is not registered.")
        return target

    def resolve_message(self, message: Any) -> ClusterTarget:
        """Require one unambiguous, registered URL in an A2A user message."""

        text_parts: list[str] = []

        def collect(value: Any) -> None:
            if isinstance(value, str):
                text_parts.append(value)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
            elif isinstance(value, dict):
                for key, child in value.items():
                    if key == "text" and isinstance(child, str):
                        text_parts.append(child)
                    elif key != "text":
                        collect(child)

        collect(message)
        candidates: set[str] = set()
        for text in text_parts:
            for match in _URL_RE.finditer(text):
                candidate = match.group(0).rstrip(".,;!?)}'`\"")
                if (
                    candidate.endswith("]")
                    and candidate.count("]") > candidate.count("[")
                ):
                    candidate = candidate[:-1]
                if not candidate:
                    continue
                # Normalize now so an invalid candidate cannot be ignored just
                # because another valid URL also appeared in the same prompt.
                try:
                    candidates.add(normalize_api_url(candidate))
                except ValueError as error:
                    raise ClusterSelectionError(
                        "Provide an HTTPS OpenShift API URL without credentials, "
                        "path, query, or fragment."
                    ) from error

        if not candidates:
            raise ClusterSelectionError(
                "An explicit OpenShift API URL is required in every request."
            )
        if len(candidates) != 1:
            raise ClusterSelectionError(
                "Provide exactly one OpenShift API URL per request."
            )
        return self.resolve(next(iter(candidates)))


async def add_cluster_header(request: httpx.Request) -> None:
    """Attach target metadata to RPC POSTs, never to card discovery GETs."""

    if request.method.upper() != "POST":
        return
    target = _ACTIVE_CLUSTER.get()
    if target is None:
        raise RuntimeError(
            "Refusing downstream A2A RPC without a validated OpenShift API URL"
        )
    request.headers[CLUSTER_HEADER] = target.cluster_id


def cluster_registry_from_env(enabled: bool) -> ClusterRegistry | None:
    """Require the registry only when cluster selection is enabled."""

    if not enabled:
        return None
    return ClusterRegistry.from_env()


def downstream_request_hooks(auth_hook, cluster_selection_enabled: bool) -> list:
    """Keep legacy auth-only behavior unless cluster selection is enabled."""

    hooks = [auth_hook]
    if cluster_selection_enabled:
        hooks.append(add_cluster_header)
    return hooks


class ClusterSelectionMiddleware:
    """Validate one-shot inbound A2A requests before they reach the ADK agent.

    ACME currently accepts only ``message/send``. It has no persisted mapping
    from public A2A task IDs to a validated cluster, so task polling and
    cancellation methods are rejected instead of running without a target.
    """

    def __init__(self, app, registry: ClusterRegistry) -> None:
        self.app = app
        self.registry = registry

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"].upper() != "POST":
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                # Do not hand incomplete request bytes to ADK. The peer is
                # already gone, so attempting to send a response is unreliable.
                return
            if message["type"] != "http.request":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_INBOUND_BODY_BYTES:
                await _send_error(
                    send,
                    None,
                    -32600,
                    "Request body exceeds the 1 MiB limit.",
                    status=413,
                )
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        request_id = None
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("JSON-RPC request must be an object")
            request_id = payload.get("id")
            method = payload.get("method")
            if not isinstance(method, str):
                await _send_error(send, request_id, -32600, "Invalid JSON-RPC method.")
                return
            if method != SUPPORTED_INBOUND_METHOD:
                await _send_error(
                    send,
                    request_id,
                    -32601,
                    "ACME currently supports only one-shot message/send; "
                    "task polling, cancellation, and streaming are not supported.",
                )
                return
            params = payload.get("params")
            message = params.get("message") if isinstance(params, dict) else None
            target = self.registry.resolve_message(message)
        except (json.JSONDecodeError, UnicodeDecodeError):
            await _send_error(
                send, request_id, -32700, "Invalid JSON-RPC request body."
            )
            return
        except ClusterSelectionError as error:
            await _send_error(send, request_id, -32602, str(error))
            return
        except (TypeError, ValueError):
            await _send_error(send, request_id, -32600, "Invalid JSON-RPC request.")
            return

        token = _ACTIVE_CLUSTER.set(target)
        try:
            await self.app(scope, _replay_body(bytes(body), receive), send)
        finally:
            _ACTIVE_CLUSTER.reset(token)


def _replay_body(body: bytes, receive):
    """Replay the bounded, complete request body consumed by middleware."""

    replayed = False

    async def replay():
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


async def _send_error(
    send,
    request_id: Any,
    code: int,
    message: str,
    *,
    status: int = 400,
) -> None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
