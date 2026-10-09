"""ACME's local ADK A2A routing agent."""

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

from .auth import (
    DownstreamAuth,
    add_cluster_header,
    log_dispatch_audit,
    stamp_request_id,
)
from .cluster_registry import ClusterRegistry
from .config import agent_card_url, remote_agents_from_env
from .routing import ClusterRoutingMiddleware
from .ui import UI_HTML


AUTH = DownstreamAuth()

# The GitOps-managed allow-list of {id: apiURL} this ACME instance may route
# to. An empty registry (e.g. before the chart's olsClusters value is
# configured) means every cluster URL is rejected as unregistered -- fail
# closed, never fall back to an unvalidated default.
CLUSTER_REGISTRY = ClusterRegistry.from_env()

# Hook order matters for audit joinability: stamp X-Request-Id first so a
# Keycloak Token A mint can log the same request_id as the later dispatch
# line; then auth, then the routing header, then the dispatch acme_audit.
# add_cluster_header / stamp / log only touch POSTs -- never the public
# unauthenticated agent-card GET.
HTTP_CLIENT = httpx.AsyncClient(
    timeout=AUTH.settings.timeout,
    verify=AUTH.settings.tls_verify,
    event_hooks={
        "request": [
            stamp_request_id,
            AUTH.add_auth,
            add_cluster_header,
            log_dispatch_audit,
        ]
    },
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
        model=os.getenv("ADK_MODEL", "openai/acme-agent"),
        base_url=os.getenv("LITELLM_API_BASE") or None,
        api_key=os.getenv("LITELLM_API_KEY") or None,
    )


# This local ADK agent is the public entrypoint and router. The RemoteA2aAgent
# instances above are attached as sub_agents (NOT wrapped as AgentTool): the
# root LLM validates the target cluster URL, then delegates via
# transfer_to_agent to the downstream agent. ADK's RemoteA2aAgent calls the
# remote with message/stream and yields each remote event (TaskStatusUpdate /
# TaskArtifactUpdate / Message) incrementally; as a sub_agent those events
# propagate through THIS agent's own A2A message/stream response, so OpenShift
# Lightspeed's per-iteration progress and its answer stream straight to the
# caller as they happen.
#
# AgentTool was used here before, but AgentTool.run_async buffers the sub-agent
# down to a single return value -- under message/stream that emits nothing until
# the entire investigation finishes, starving the connection on long
# troubleshooting runs (the client's idle connection then gets cut). sub_agents
# also means the final answer is OLS's streamed output delivered directly, not
# re-emitted by this LLM, so it is never truncated or wrapped in a {"result":...}
# envelope. We do NOT set mode="task" (that needs ADK's finish_task handshake,
# which vendor/lightspeed-service's hand-rolled A2A endpoint does not implement);
# the default mode streams events and treats stream end / TASK_STATE_COMPLETED
# as completion. (A PlanReActPlanner can be added to stream the router's own
# reasoning too; left out for now to validate sub_agents streaming on its own.)
root_agent = Agent(
    model=_model(),
    name=os.getenv("AGENT_NAME", "acme_agent"),
    description=(
        "A local A2A entrypoint that routes requests to configured downstream "
        "A2A agents."
    ),
    instruction=(
        "You are the ACME A2A entrypoint and router. Every request you "
        "delegate is ultimately answered by OpenShift Lightspeed against one "
        "specific OpenShift cluster, so you must first know exactly which "
        "cluster that is.\n\n"
        "Before delegating, require the user to state the target OpenShift "
        "cluster's API URL explicitly (for example "
        "https://api.example.com:6443). If the message does not contain "
        "exactly one explicit cluster API URL -- it is missing, or the "
        "message mentions more than one candidate URL -- do not delegate. "
        "Instead, ask a single, direct clarifying question naming what you "
        "need (the exact OpenShift API URL) and stop there.\n\n"
        "Never guess, assume, or silently reuse a cluster URL from earlier "
        "in the conversation, and never infer a target cluster from a "
        "resource name, namespace, project, or any other hint in the "
        "request -- a resource name is not a cluster identifier. If the "
        "user's URL looks malformed or is not one of the clusters you are "
        "configured to reach, say so and ask for the correct one rather "
        "than attempting delegation anyway; the final authorization of "
        "that URL happens server-side, not by your judgment.\n\n"
        "Once the request contains exactly one explicit, well-formed cluster "
        "API URL, delegate the request to the appropriate downstream agent by "
        "transferring to it, preserving the user's full request verbatim "
        "(including that URL) without rewriting or dropping details. The "
        "downstream agent answers the user directly -- do not restate, "
        "summarize, wrap, or add commentary to its response, and do not answer "
        "from your own knowledge. If no configured downstream agent is "
        "suitable, explain that the request cannot be routed."
    ),
    sub_agents=remote_agents,
)


async def healthz(request) -> JSONResponse:
    return JSONResponse({"status": "ok", "agent": "acme_agent"})


async def ui(request) -> HTMLResponse:
    return HTMLResponse(UI_HTML)


@asynccontextmanager
async def lifespan(app) -> AsyncIterator[None]:
    yield
    await HTTP_CLIENT.aclose()
    await AUTH.close()


_adk_app = to_a2a(
    root_agent,
    host=os.getenv("A2A_PUBLIC_HOST", "localhost"),
    port=int(os.getenv("A2A_PUBLIC_PORT", "8080")),
    protocol=os.getenv("A2A_PUBLIC_PROTOCOL", "http"),
    lifespan=lifespan,
)

# ADK returns a Starlette application, so the UI and health endpoint can be
# added without replacing the A2A routes installed during application startup.
_adk_app.routes.extend(
    [
        Route("/", ui, methods=["GET"]),
        Route("/ui", ui, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
    ]
)

# Wraps the ADK app, not replaces it: GET requests (card discovery, the UI,
# healthz) pass straight through untouched. Every POST (JSON-RPC) is
# inspected for a message/send|stream dispatch; see routing.py for why a
# rejection here never reaches the ADK app/root agent at all.
a2a_app = ClusterRoutingMiddleware(_adk_app, CLUSTER_REGISTRY)

