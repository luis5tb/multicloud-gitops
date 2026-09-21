from rca_agent.kubernetes import build_analysis_only_run


def test_build_analysis_only_run_omits_execution_and_verification():
    run = build_analysis_only_run(
        "Investigate the CrashLoopBackOff in payments.",
        "default",
        ["payments"],
    )

    assert run["apiVersion"] == "agentic.openshift.io/v1alpha1"
    assert run["kind"] == "AgenticRun"
    assert run["spec"]["request"].startswith("Investigate")
    assert run["spec"]["analysis"] == {"agent": "default"}
    assert run["spec"]["targetNamespaces"] == ["payments"]
    assert "execution" not in run["spec"]
    assert "verification" not in run["spec"]


def test_build_analysis_only_run_rejects_invalid_namespace():
    try:
        build_analysis_only_run("Investigate the cluster.", "default", ["Bad_Namespace"])
    except ValueError as error:
        assert "DNS label" in str(error)
    else:
        raise AssertionError("invalid namespace should be rejected")

