"""Configuration helpers shared by the agent and its tests."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class RemoteAgentConfig:
    """Configuration for one downstream A2A agent."""

    name: str
    description: str
    endpoint: str


def env_bool(value: str | None, default: bool = False) -> bool:
    """Parse a conventional boolean environment value."""

    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def agent_card_url(endpoint: str, well_known_path: str) -> str:
    """Return an agent-card URL for a base endpoint or an explicit card URL."""

    normalized = endpoint.strip()
    if not normalized:
        raise ValueError("each remote agent's endpoint must not be empty")

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("each remote agent's endpoint must be an absolute HTTP(S) URL")

    if parsed.path.endswith(".json"):
        return normalized
    return f"{normalized.rstrip('/')}/{well_known_path.lstrip('/')}"


def remote_agents_from_env() -> list[RemoteAgentConfig]:
    """Load downstream A2A agents from REMOTE_A2A_AGENTS_JSON.

    There is no single-agent fallback: a missing/empty value is a
    misconfiguration, not "no downstream agents configured yet", so this
    fails loudly instead of silently routing to a nonexistent localhost
    agent.
    """

    raw = os.getenv("REMOTE_A2A_AGENTS_JSON", "").strip()
    if not raw:
        raise ValueError("REMOTE_A2A_AGENTS_JSON must be set to a non-empty JSON list")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("REMOTE_A2A_AGENTS_JSON must contain valid JSON") from exc
    if not isinstance(values, list) or not values:
        raise ValueError("REMOTE_A2A_AGENTS_JSON must be a non-empty JSON list")

    agents: list[RemoteAgentConfig] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Each remote A2A agent must be a JSON object")
        name = str(value.get("name", "")).strip()
        endpoint = str(value.get("endpoint", "")).strip()
        description = str(value.get("description", "")).strip()
        if not name or not endpoint:
            raise ValueError("Each remote A2A agent requires name and endpoint")
        agents.append(
            RemoteAgentConfig(
                name=name,
                description=description or f"Remote A2A agent named {name}",
                endpoint=endpoint,
            )
        )
    return agents

