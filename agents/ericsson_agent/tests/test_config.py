import pytest

from ericsson_agent.config import agent_card_url, env_bool, remote_agents_from_env


def test_agent_card_url_appends_well_known_path():
    assert agent_card_url("https://example.test/a2a", "/.well-known/agent.json") == (
        "https://example.test/a2a/.well-known/agent.json"
    )


def test_agent_card_url_preserves_explicit_card():
    url = "https://example.test/.well-known/agent-card.json"
    assert agent_card_url(url, "/.well-known/agent.json") == url


def test_agent_card_url_rejects_relative_url():
    with pytest.raises(ValueError):
        agent_card_url("downstream-a2a:8080", "/.well-known/agent.json")


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_env_bool(value):
    assert env_bool(value) is True


def test_remote_agents_from_json(monkeypatch):
    monkeypatch.setenv(
        "REMOTE_A2A_AGENTS_JSON",
        '[{"name":"inventory","description":"Inventory agent",'
        '"endpoint":"https://inventory.example/a2a"}]',
    )
    agents = remote_agents_from_env()
    assert agents[0].name == "inventory"
    assert agents[0].endpoint == "https://inventory.example/a2a"


def test_remote_agents_single_agent_shorthand(monkeypatch):
    monkeypatch.delenv("REMOTE_A2A_AGENTS_JSON", raising=False)
    monkeypatch.setenv("DOWNSTREAM_A2A_ENDPOINT", "https://example.test/a2a")
    assert remote_agents_from_env()[0].name == "downstream_agent"

