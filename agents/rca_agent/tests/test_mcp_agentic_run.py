import json

from rca_agent.mcp_agentic_run import _DEFAULT_SKILLS, build_analysis_only_run


def test_analysis_run_has_no_execution_or_verification() -> None:
    run = build_analysis_only_run("Why is the API failing?", "default", ["payments"])

    assert run["kind"] == "AgenticRun"
    assert run["spec"]["analysis"] == {"agent": "default"}
    assert run["spec"]["targetNamespaces"] == ["payments"]
    assert "execution" not in run["spec"]
    assert "verification" not in run["spec"]
    assert "mcpServers" not in run["spec"]


def test_analysis_run_uses_default_skills_bundle() -> None:
    run = build_analysis_only_run("Why is the API failing?", "default", ["payments"])

    assert run["spec"]["tools"] == {"skills": _DEFAULT_SKILLS}


def test_analysis_run_honors_configured_skills(monkeypatch) -> None:
    custom_skills = [{"image": "quay.io/example/skills:v1", "paths": ["/skills/example"]}]
    monkeypatch.setenv("AGENTIC_RUN_SKILLS", json.dumps(custom_skills))

    run = build_analysis_only_run("Why is the API failing?", "default", ["payments"])

    assert run["spec"]["tools"] == {"skills": custom_skills}
