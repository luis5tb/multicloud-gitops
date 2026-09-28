from rca_agent.agent import _SKILL_NAME, _SKILLS_DIR, _load_skill_toolset, _read_skill_body, root_agent


def test_root_agent_registers_skill_toolset() -> None:
    from google.adk.tools.skill_toolset import SkillToolset

    toolsets = [tool for tool in root_agent.tools if isinstance(tool, SkillToolset)]
    assert len(toolsets) == 1
    assert [skill.name for skill in toolsets[0].skills] == [_SKILL_NAME]


def test_root_agent_preloads_skill_body_into_instruction() -> None:
    body = _read_skill_body(_SKILLS_DIR / _SKILL_NAME)

    assert body in root_agent.instruction
    assert "create_and_wait_for_analysis" in body


def test_load_skill_toolset_matches_bundled_skill_name() -> None:
    toolset = _load_skill_toolset()

    assert [skill.name for skill in toolset.skills] == [_SKILL_NAME]
