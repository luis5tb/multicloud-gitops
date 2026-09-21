from rca_agent.mcp_agentic_run import build_analysis_only_run


def test_analysis_run_has_no_execution_or_verification() -> None:
    run = build_analysis_only_run("Why is the API failing?", "default", ["payments"])

    assert run["kind"] == "AgenticRun"
    assert run["spec"]["analysis"] == {"agent": "default"}
    assert run["spec"]["targetNamespaces"] == ["payments"]
    assert "execution" not in run["spec"]
    assert "verification" not in run["spec"]
    assert "mcpServers" not in run["spec"]
