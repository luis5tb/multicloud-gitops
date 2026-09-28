"""Google ADK root agent for analysis-only OpenShift RCA requests."""

import logging
import os
import pathlib

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.skills import load_skill_from_dir
from google.adk.tools.skill_toolset import SkillToolset

from .mcp_agentic_run import create_and_wait_for_analysis

logger = logging.getLogger(__name__)

# This is the only skill this agent has, and it applies to every request, so
# its SKILL.md body is preloaded into the system prompt below rather than
# left for the LLM to fetch on demand via load_skill -- there is no other
# skill to choose between, and the tool it governs must be used correctly on
# every call, not just when the model happens to ask.
_SKILL_NAME = "agentic-run-analysis"
_SKILLS_DIR = pathlib.Path(__file__).parent / "skills"

ROOT_AGENT_INSTRUCTION = """
You are the OpenShift Root Cause Analysis agent.

This service is advisory only. Never execute a proposed change, approve a
proposal, create an approval resource, run a verification step, or claim that
the cluster was modified.

See the preloaded "agentic-run-analysis" skill below for how to call
create_and_wait_for_analysis and how to present its results.
"""


def _read_skill_body(skill_dir: pathlib.Path) -> str:
    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    # Frontmatter is delimited by the first two '---' lines; the rest is the
    # skill's markdown body.
    _, _, body = content.split("---", 2)
    return body.strip()


def _load_skill_toolset() -> SkillToolset:
    skill = load_skill_from_dir(_SKILLS_DIR / _SKILL_NAME)
    logger.info("Loaded ADK skill '%s'", skill.name)
    return SkillToolset(skills=[skill])


def _model() -> LiteLlm:
    """Build the ADK model through the configured LiteLLM proxy."""

    # LiteLlm forwards **kwargs to litellm.completion(), which does not read
    # LITELLM_API_BASE/LITELLM_API_KEY on its own -- those are this chart's
    # own env var names, not something litellm auto-detects for the
    # "openai/" model prefix (it only auto-reads OPENAI_API_KEY). Pass them
    # through explicitly; litellm.completion's base URL kwarg is base_url,
    # not api_base.
    return LiteLlm(
        model=os.getenv("ADK_MODEL", "openai/rca-agent"),
        base_url=os.getenv("LITELLM_API_BASE") or None,
        api_key=os.getenv("LITELLM_API_KEY") or None,
    )


_skill_body = _read_skill_body(_SKILLS_DIR / _SKILL_NAME)

root_agent = Agent(
    name="rca_agent",
    model=_model(),
    description=(
        "Creates analysis-only OpenShift AgenticRuns and returns root-cause "
        "analysis with remediation proposals. It never executes or verifies changes."
    ),
    instruction=f"{ROOT_AGENT_INSTRUCTION}\n\n{_skill_body}",
    tools=[create_and_wait_for_analysis, _load_skill_toolset()],
)
