"""ACME's local ADK A2A routing agent."""

from __future__ import annotations

import json
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
    AuthSelectionMiddleware,
    DownstreamAuth,
    log_dispatch_audit,
    stamp_request_id,
)
from .config import agent_card_url, remote_agents_from_env
from .ols_mode import ols_mode_remote_agent_config
from .renewal import CredentialRenewingTransport
from .routing_prompt import ROUTER_INSTRUCTION
from .ui import UI_HTML


AUTH = DownstreamAuth()

# Hook order matters for audit joinability: stamp X-Request-Id first so a
# Keycloak Token A mint can log the same request_id as the later dispatch
# line; then auth, then the dispatch acme_audit. stamp / log only touch
# POSTs -- never the public unauthenticated agent-card GET.
#
# ACME no longer attaches X-OLS-Cluster. Cluster selection has moved
# downstream to the MAS A2A orchestrator, which validates the target cluster
# and sets X-OLS-Cluster on its own outbound call to Praxis. ACME just
# authenticates (Token A) and forwards the user's request verbatim, so the
# add_cluster_header hook is intentionally not registered here.
#
# The transport renews Token A transparently when a downstream agent answers
# HTTP 401 or pauses its task in TASK_STATE_AUTH_REQUIRED (see renewal.py).
# TLS verification lives on the inner transport: an explicit transport
# replaces the client's own verify setting.
HTTP_CLIENT = httpx.AsyncClient(
    timeout=AUTH.settings.timeout,
    transport=CredentialRenewingTransport(
        httpx.AsyncHTTPTransport(verify=AUTH.settings.tls_verify),
        AUTH,
        max_renewals=int(os.getenv("ACME_MAX_TOKEN_RENEWALS", "2")),
    ),
    event_hooks={
        "request": [
            stamp_request_id,
            AUTH.add_auth,
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

# Shared across every downstream RemoteA2aAgent: stamps ols_mode=troubleshooting
# (or OLS_A2A_MODE) on outbound A2A messages. OLS defaults to ask when the
# metadata key is absent; this pattern's investigation flow needs troubleshooting.
_OLS_MODE_CONFIG = ols_mode_remote_agent_config()

remote_agents = [
    RemoteA2aAgent(
        name=remote.name,
        description=remote.description,
        agent_card=agent_card_url(remote.endpoint, AGENT_CARD_WELL_KNOWN_PATH),
        httpx_client=HTTP_CLIENT,
        timeout=AUTH.settings.timeout,
        use_legacy=USE_LEGACY,
        config=_OLS_MODE_CONFIG,
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
# root LLM delegates via transfer_to_agent to the downstream agent (the MAS
# A2A orchestrator now owns target-cluster selection). ADK's RemoteA2aAgent calls the
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
# which the OLS A2A endpoint does not implement);
# the default mode streams events and treats stream end / TASK_STATE_COMPLETED
# as completion. (A PlanReActPlanner can be added to stream the router's own
# reasoning too; left out for now to validate sub_agents streaming on its own.)
root_agent = Agent(
    model=_model(),
    name=os.getenv("AGENT_NAME", "acme_agent"),
    description=(
        "A local A2A entrypoint that routes Red Hat-related requests through "
        "configured downstream workflows, including the MAO orchestrator."
    ),
    instruction=ROUTER_INSTRUCTION,
    sub_agents=remote_agents,
)


async def healthz(request) -> JSONResponse:
    return JSONResponse({"status": "ok", "agent": "acme_agent"})


async def ui(request) -> HTMLResponse:
    options = json.dumps(AUTH.settings.ui_options()).replace("</", "<\\/")
    return HTMLResponse(UI_HTML.replace("__AUTH_OPTIONS_JSON__", options))


async def token_debug(request) -> JSONResponse:
    token = AUTH.pop_debug_token(str(request.path_params["debug_id"]))
    if token is None:
        return JSONResponse({"detail": "token not found"}, status_code=404)
    return JSONResponse(
        token,
        headers={"cache-control": "no-store"},
    )


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
        Route("/ui/auth/token/{debug_id}", token_debug, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
    ]
)

# ACME no longer gates inbound requests on a target-cluster URL: that
# enforcement moved to the MAS A2A orchestrator, which validates the cluster
# and sets X-OLS-Cluster on its downstream call to Praxis. The ADK app is
# therefore served directly. The cluster-routing middleware (routing.py) and
# registry (cluster_registry.py) are retained for the direct
# ACME -> Praxis -> OLS path but intentionally not wired in here.
a2a_app = AuthSelectionMiddleware(_adk_app, AUTH.settings)
