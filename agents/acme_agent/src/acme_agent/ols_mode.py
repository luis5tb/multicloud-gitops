"""Inject OLS ``ols_mode`` metadata on outbound A2A messages to Lightspeed.

The OLS A2A executor (``luis5tb/lightspeed-service@a2a``) reads message
metadata key ``ols_mode`` and defaults to ``ask`` when absent. This pattern's
ACME → OLS path is a live-cluster investigation flow, so ACME stamps
``troubleshooting`` on every outbound message unless the caller already set
``ols_mode`` or ``OLS_A2A_MODE`` overrides the default.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from a2a.types import Message as A2AMessage
from google.adk.a2a.agent.config import ParametersConfig, RequestInterceptor
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event

OLS_MODE_METADATA_KEY = "ols_mode"
DEFAULT_OLS_A2A_MODE = "troubleshooting"
_ALLOWED_MODES = frozenset({"ask", "troubleshooting"})


def configured_ols_mode() -> str:
    """Return the OLS mode ACME should stamp on outbound A2A messages."""
    raw = os.getenv("OLS_A2A_MODE", DEFAULT_OLS_A2A_MODE).strip().lower()
    if raw not in _ALLOWED_MODES:
        raise ValueError(
            f"OLS_A2A_MODE must be one of {sorted(_ALLOWED_MODES)}, got {raw!r}"
        )
    return raw


def _metadata_has_ols_mode(message: A2AMessage) -> bool:
    fields = getattr(message.metadata, "fields", None)
    return bool(fields and OLS_MODE_METADATA_KEY in fields)


async def inject_ols_mode(
    _ctx: Optional[InvocationContext],
    message: A2AMessage,
    parameters: ParametersConfig,
) -> tuple[A2AMessage | Event, ParametersConfig]:
    """ADK ``before_request`` hook: set ``ols_mode`` when the message lacks it."""
    if not _metadata_has_ols_mode(message):
        message.metadata[OLS_MODE_METADATA_KEY] = configured_ols_mode()
    return message, parameters


def ols_mode_request_interceptor() -> RequestInterceptor:
    """Build the interceptor RemoteA2aAgent should attach for OLS mode stamping."""
    return RequestInterceptor(before_request=inject_ols_mode)


def ols_mode_remote_agent_config() -> Any:
    """``A2aRemoteAgentConfig`` that stamps ``ols_mode`` on outbound messages."""
    from google.adk.a2a.agent.config import A2aRemoteAgentConfig

    return A2aRemoteAgentConfig(
        request_interceptors=[ols_mode_request_interceptor()]
    )
