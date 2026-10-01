"""Invocation-scoped propagation of the selected ``X-OLS-Cluster`` id.

``agent.py`` shares a single ``httpx.AsyncClient`` (and its auth request
hook, see ``auth.py``) across every downstream ``RemoteA2aAgent`` call,
including the public, unauthenticated agent-card fetch. A process-global
mutable header -- or any default set directly on that shared client -- would
leak one conversation's selected cluster into another concurrent
conversation's requests, or onto the card fetch.

A ``contextvars.ContextVar`` is used instead. ``ClusterRoutingMiddleware``
(see ``routing.py``) binds it once per inbound HTTP request, for the
duration of that request's handling: every ``await`` inside that scope,
including any downstream HTTP calls ADK/the A2A client make while completing
that single ``message/send`` invocation (card resolution is excluded
separately -- see ``auth.add_cluster_header``), sees the same value.

This works because asyncio (and the ASGI servers used here) run each inbound
HTTP request in its own ``asyncio.Task``; a ``Task`` captures a *copy* of the
ambient ``contextvars.Context`` at creation time, so sibling requests handled
concurrently in their own tasks never observe a value set by this one, and a
value set here is never visible before this request started or after its
task finishes.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Iterator, Optional

_CLUSTER_ID: ContextVar[Optional[str]] = ContextVar("acme_cluster_id", default=None)


class ClusterIdMissingError(RuntimeError):
    """Raised when an authenticated A2A RPC would go out with no bound id.

    This should be unreachable in normal operation -- ``routing.py`` rejects
    any inbound message that fails cluster validation before the agent ever
    runs -- but the request hook enforces it anyway as defense in depth: it
    must never silently send a routing-critical RPC with no header, and must
    never fall back to a default cluster.
    """


def get_cluster_id() -> Optional[str]:
    """Return the cluster id bound to the currently executing invocation."""

    return _CLUSTER_ID.get()


@contextlib.contextmanager
def cluster_id_scope(cluster_id: str) -> Iterator[None]:
    """Bind ``cluster_id`` to the current context for the scope's duration.

    On exit (including via exception), the previous value is restored
    rather than cleared unconditionally, so nested scopes compose correctly.
    """

    token = _CLUSTER_ID.set(cluster_id)
    try:
        yield
    finally:
        _CLUSTER_ID.reset(token)
