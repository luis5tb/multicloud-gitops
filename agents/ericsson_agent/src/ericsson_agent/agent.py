"""Ericsson's local ADK A2A routing agent."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.agents.llm_agent import Agent
from google.adk.agents.remote_a2a_agent import (
    AGENT_CARD_WELL_KNOWN_PATH,
    RemoteA2aAgent,
)
from google.adk.models.lite_llm import LiteLlm
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .auth import DownstreamAuth
from .config import agent_card_url, remote_agents_from_env
from .ui import UI_HTML


AUTH = DownstreamAuth()

# The event hook is used for both agent-card discovery and JSON-RPC calls. This
# keeps the auth behavior identical for the two types of downstream requests.
HTTP_CLIENT = httpx.AsyncClient(
    timeout=AUTH.settings.timeout,
    verify=AUTH.settings.tls_verify,
    event_hooks={"request": [AUTH.add_auth]},
)

USE_LEGACY = os.getenv("A2A_USE_LEGACY", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

remote_agents = [
    RemoteA2aAgent(
        name=remote.name,
        description=remote.description,
        agent_card=agent_card_url(remote.endpoint, AGENT_CARD_WELL_KNOWN_PATH),
        httpx_client=HTTP_CLIENT,
        timeout=AUTH.settings.timeout,
        use_legacy=USE_LEGACY,
    )
    for remote in remote_agents_from_env()
]

def _model() -> LiteLlm:
    """Build the ADK model through the configured LiteLLM proxy."""

    # LiteLlm forwards **kwargs to litellm.completion(), which does not read
    # LITELLM_API_BASE/LITELLM_API_KEY on its own -- those are this chart's
    # own env var names, not something litellm auto-detects for the
    # "openai/" model prefix (it only auto-reads OPENAI_API_KEY). Pass them
    # through explicitly; litellm.completion's base URL kwarg is base_url,
    # not api_base.
    return LiteLlm(
        model=os.getenv("ADK_MODEL", "openai/ericsson-agent"),
        base_url=os.getenv("LITELLM_API_BASE") or None,
        api_key=os.getenv("LITELLM_API_KEY") or None,
    )


# This local ADK agent is the public entrypoint. The RemoteA2aAgent instances
# above are configured downstream sub-agents, not the public endpoint.
root_agent = Agent(
    model=_model(),
    name=os.getenv("AGENT_NAME", "ericsson_agent"),
    description=(
        "A local A2A entrypoint that routes requests to configured downstream "
        "A2A agents."
    ),
    instruction=(
        "You are the Ericsson A2A entrypoint and router. Delegate every user "
        "request to exactly one configured remote A2A sub-agent. Choose the "
        "best matching sub-agent using its description, pass the user's request "
        "without rewriting or dropping important details, and return the "
        "sub-agent's result. Do not answer from your own knowledge and do not "
        "invent a result. If no configured sub-agent is suitable, explain that "
        "the request cannot be routed."
    ),
    sub_agents=remote_agents,
)


async def healthz(request) -> JSONResponse:
    return JSONResponse({"status": "ok", "agent": "ericsson_agent"})


async def ui(request) -> HTMLResponse:
    return HTMLResponse(UI_HTML)


@asynccontextmanager
async def lifespan(app) -> AsyncIterator[None]:
    yield
    await HTTP_CLIENT.aclose()
    await AUTH.close()


a2a_app = to_a2a(
    root_agent,
    host=os.getenv("A2A_PUBLIC_HOST", "localhost"),
    port=int(os.getenv("A2A_PUBLIC_PORT", "8080")),
    protocol=os.getenv("A2A_PUBLIC_PROTOCOL", "http"),
    lifespan=lifespan,
)

# ADK returns a Starlette application, so the UI and health endpoint can be
# added without replacing the A2A routes installed during application startup.
a2a_app.routes.extend(
    [
        Route("/", ui, methods=["GET"]),
        Route("/ui", ui, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
    ]
)

