"""Tests for the ACME router's downstream selection guidance."""

from acme_agent.routing_prompt import ROUTER_INSTRUCTION


def test_router_prompt_sends_red_hat_projects_to_mao():
    prompt = " ".join(ROUTER_INSTRUCTION.lower().split())

    assert "route every red hat-related request to mao, including" in prompt
    assert "unfamiliar or ambiguously named projects" in prompt
    assert "ask red hat" in prompt
    assert "openshift lightspeed (ols)" in prompt
    assert (
        "if mao is the only configured downstream agent, delegate every request to it"
        in prompt
    )
