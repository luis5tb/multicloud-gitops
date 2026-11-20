"""Regression tests for the lightspeed-hub chart (Task B / M4 skeleton).

The chart is DISABLED by default (enabled=false renders nothing), mirroring
the mas-a2a a2a.enabled gating. These tests pin the disabled-by-default
behavior, the rendered shape of an enabled deployment (adapter
Deployment/Service/SA/NetworkPolicy, run-token Secret writer RBAC, admission
policy + binding + param ConfigMap, agentic-operator stack with vendored
CRDs and operator RBAC), the deliberate
absence of any Route, and the fail-loud required values.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "charts/all/lightspeed-hub"
VARIANT = REPO_ROOT / "variants/standalone/values-standalone.yaml"

# Minimum values for an enabled render: the chart fails loudly without the
# hub audience, the Keycloak exchange client, and (in gateway mode, the
# default) the two repo C gateway keys. The issuer derives from the variant's
# global.clusterBaseDomain.
ENABLE_SETS = [
    "--set", "enabled=true",
    "--set", "adapter.hubAudience=lightspeed-hub-a2a",
    "--set", "adapter.keycloak.clientId=lightspeed-hub-adapter",
    "--set", "adapter.keycloak.clientSecretSecret.name=lightspeed-hub-adapter-client",
    "--set",
    "agenticOperator.config.gatewayServerURLTemplate=https://praxis-spoke-{targetCluster}.praxis-proxy.svc.cluster.local:8443",
    "--set", "agenticOperator.config.gatewayCASecret=praxis-spoke-gateway-ca",
]


def _helm(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "lightspeed-hub", str(CHART), "-f", str(VARIANT), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _resources(stdout: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(stdout) if doc]


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_default_render_is_empty() -> None:
    """Disabled by default: the chart must render no resources at all."""
    rendered = _helm()
    assert _resources(rendered.stdout) == []


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_enabled_render_has_expected_resources() -> None:
    rendered = _helm(*ENABLE_SETS)
    docs = _resources(rendered.stdout)

    kinds = {}
    for doc in docs:
        kinds.setdefault(doc["kind"], []).append(doc["metadata"]["name"])

    # Exactly the skeleton's resource set, with the expected names.
    assert sorted(kinds["Deployment"]) == [
        "lightspeed-hub",
        "lightspeed-hub-agentic-operator",
    ]
    assert sorted(kinds["Service"]) == [
        "lightspeed-hub",
        "lightspeed-hub-agentic-operator-webhook",
    ]
    assert sorted(kinds["ServiceAccount"]) == [
        "lightspeed-hub",
        "lightspeed-hub-agentic-operator",
    ]
    assert kinds["NetworkPolicy"] == ["lightspeed-hub"]
    assert sorted(kinds["Role"]) == ["lightspeed-hub-run-token-writer"]
    assert sorted(kinds["RoleBinding"]) == ["lightspeed-hub-run-token-writer"]
    # Operator stack RBAC (vendored from repo C's config/rbac/role.yaml):
    # a ClusterRole + ClusterRoleBinding, chart-named.
    assert sorted(kinds["ClusterRole"]) == [
        "lightspeed-hub-agentic-operator-manager"
    ]
    assert sorted(kinds["ClusterRoleBinding"]) == [
        "lightspeed-hub-agentic-operator-manager"
    ]
    assert sorted(kinds["ValidatingAdmissionPolicy"]) == [
        "lightspeed-hub-agenticrun-target-authz"
    ]
    assert sorted(kinds["ValidatingAdmissionPolicyBinding"]) == [
        "lightspeed-hub-agenticrun-target-authz"
    ]
    assert sorted(kinds["ConfigMap"]) == [
        "lightspeed-agentic-configuration",
        "lightspeed-hub-target-policy",
    ]
    # The full vendored CRD set (repo C config/crd/bases, all ten).
    assert sorted(kinds["CustomResourceDefinition"]) == [
        "agenticolsconfigs.agentic.openshift.io",
        "agenticrunapprovals.agentic.openshift.io",
        "agenticruns.agentic.openshift.io",
        "agents.agentic.openshift.io",
        "analysisresults.agentic.openshift.io",
        "approvalpolicies.agentic.openshift.io",
        "escalationresults.agentic.openshift.io",
        "executionresults.agentic.openshift.io",
        "llmproviders.agentic.openshift.io",
        "verificationresults.agentic.openshift.io",
    ]
    # Internal-only by design (design doc §1): NEVER a Route.
    assert "Route" not in kinds


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_run_token_writer_role_verbs() -> None:
    """RBAC reconciled against the M3 adapter implementation:
    - secrets: create/get/update + DELETE (the §5.5 terminal-run cleanup
      and run-deletion cleanup are implemented, hub-adapter watch.go);
    - agenticruns: get/list/watch + update (update is ONLY for the §6.4
      pause-count annotation, a2a.lightspeed.io/credential-pauses);
    - agenticruns/status: NOT granted (the adapter never writes status or
      conditions, design §6.1);
    - agenticrunapprovals: get/list/watch (the SA reads approvals both
      directly -- pending-stage derivation -- and via the M3 informer
      watch, which LISTs and WATCHes them in the run namespace; get-only
      starves the informer and the adapter never binds its listener --
      live incident. DECISIONS are still patched as the caller, Token H
      -- hub-side RoleBinding wiring, not this chart).
    """
    rendered = _helm(*ENABLE_SETS)
    docs = _resources(rendered.stdout)
    role = next(
        d for d in docs
        if d["kind"] == "Role" and d["metadata"]["name"] == "lightspeed-hub-run-token-writer"
    )
    by_resource = {
        tuple(r["resources"]): sorted(r["verbs"])
        for r in role["rules"]
        if r["apiGroups"] == ["agentic.openshift.io"] or r["apiGroups"] == [""]
    }
    assert by_resource[("secrets",)] == ["create", "delete", "get", "update"]
    assert by_resource[("agenticruns",)] == ["get", "list", "update", "watch"]
    assert ("agenticruns/status",) not in by_resource
    assert by_resource[("agenticrunapprovals",)] == ["get", "list", "watch"]
    assert by_resource[
        ("analysisresults", "executionresults", "verificationresults", "escalationresults")
    ] == ["get", "list", "watch"]
    # Namespace-scoped only (isolation is the real boundary), and the
    # binding subjects the adapter SA in the release namespace.
    binding = next(
        d for d in docs
        if d["kind"] == "RoleBinding"
        and d["metadata"]["name"] == "lightspeed-hub-run-token-writer"
    )
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "lightspeed-hub", "namespace": "default"}
    ]


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_adapter_deployment_carries_real_flags() -> None:
    """The Deployment args are the adapter's REAL flags
    (lightspeed-hub repo, branch a2a, nested module
    a2a-adapter/internal/config/config.go is the source of truth), not
    placeholder env vars; the client secret rides KEYCLOAK_CLIENT_SECRET
    via secretKeyRef; TLS serving is wired to the service-serving cert
    (adapter.tls, default enabled)."""
    rendered = _helm(*ENABLE_SETS)
    docs = _resources(rendered.stdout)
    dep = next(
        d for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "lightspeed-hub"
    )
    container = dep["spec"]["template"]["spec"]["containers"][0]
    args = container.get("args", [])
    assert args, "adapter container must carry flag args"

    required_flags = [
        "--listen=:8443",
        "--namespace=default",
        "--hub-audience=lightspeed-hub-a2a",
        "--target-header=X-Hub-Cluster",
        # Fail-fast allow-list: empty default renders an empty value (the
        # adapter's no-enforcement default).
        "--allowed-targets=",
        "--run-agent=default",
        "--card-name=lightspeed-hub",
        "--card-version=0.1.0",
        "--card-protocol-version=0.3.0",
        "--clock-skew=30s",
        "--stream-deadline=30m",
        "--renewal-timeout=15m",
        "--max-credential-pauses=3",
        "--terminal-cleanup-grace=1m",
        "--sweep-interval=5s",
        # TLS serving (production shape): both flags, pointing at the
        # mounted service-serving cert.
        "--tls-cert-file=/etc/a2a-adapter/tls/tls.crt",
        "--tls-key-file=/etc/a2a-adapter/tls/tls.key",
    ]
    for flag in required_flags:
        assert flag in args, f"missing adapter flag {flag}"

    # Keycloak issuer/token endpoint flags carry the derived https URLs.
    issuer_flags = [a for a in args if a.startswith("--keycloak-issuer=")]
    assert len(issuer_flags) == 1 and issuer_flags[0].startswith("--keycloak-issuer=https://")
    token_flags = [a for a in args if a.startswith("--keycloak-token-endpoint=")]
    assert len(token_flags) == 1 and "/protocol/openid-connect/token" in token_flags[0]

    # Secret mode: the confidential client id is a flag, the secret an env
    # from the referenced Secret.
    assert "--keycloak-client-id=lightspeed-hub-adapter" in args
    env = {e["name"]: e for e in container["env"]}
    assert set(env) == {"KEYCLOAK_CLIENT_SECRET"}
    assert env["KEYCLOAK_CLIENT_SECRET"]["valueFrom"]["secretKeyRef"] == {
        "name": "lightspeed-hub-adapter-client",
        "key": "client-secret",
    }

    # TLS shape: container port named https on 8443, the serving-cert
    # Secret mounted read-only at the flag paths, probes HTTPS.
    port = container["ports"][0]
    assert port == {"name": "https", "containerPort": 8443, "protocol": "TCP"}
    mounts = container["volumeMounts"]
    assert mounts == [
        {"name": "serving-cert", "mountPath": "/etc/a2a-adapter/tls", "readOnly": True}
    ]
    volumes = dep["spec"]["template"]["spec"]["volumes"]
    assert volumes == [
        {"name": "serving-cert", "secret": {"secretName": "lightspeed-hub-serving-cert"}}
    ]
    for probe in ("livenessProbe", "readinessProbe"):
        assert container[probe]["httpGet"]["path"] == "/healthz"
        assert container[probe]["httpGet"]["port"] == "https"
        assert container[probe]["httpGet"]["scheme"] == "HTTPS"

    # The old placeholder env names are gone entirely.
    rendered_text = rendered.stdout
    assert "HUB_ADAPTER_" not in rendered_text


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_adapter_card_url_derives_praxis_endpoint() -> None:
    """--card-url defaults to the public Praxis /a2a endpoint.

    praxis-proxy routes A2A discovery to the adapter's own card for
    callers, and a2a-sdk only maps a 0.3 card's protocolVersion onto an
    interface when its url is non-empty -- an empty url silently drops MAS
    back to the v1.0 transport (SendMessage/SendStreamingMessage), which
    the a2a-go v0.3 adapter rejects with "method not found" (seen live).
    An explicit adapter.agentCard.url still wins.
    """
    def card_flags(*extra: str) -> list[str]:
        docs = _resources(_helm(*ENABLE_SETS, *extra).stdout)
        dep = next(
            d for d in docs
            if d["kind"] == "Deployment" and d["metadata"]["name"] == "lightspeed-hub"
        )
        args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
        return [a for a in args if a.startswith("--card-url=")]

    base_domain = yaml.safe_load(VARIANT.read_text())["global"]["clusterBaseDomain"]
    assert card_flags() == [f"--card-url=https://praxis-proxy.apps.{base_domain}/a2a"]
    assert card_flags("--set", "adapter.agentCard.url=https://gw.example.test/a2a") == [
        "--card-url=https://gw.example.test/a2a"
    ]


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_adapter_allowed_targets_flag() -> None:
    """adapter.allowedTargets (comma-joined into the adapter's
    --allowed-targets flag): set values render verbatim; the empty default
    renders an empty value (the adapter's no-enforcement default)."""
    rendered = _helm(*ENABLE_SETS)
    docs = _resources(rendered.stdout)
    dep = next(
        d for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "lightspeed-hub"
    )
    args = dep["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--allowed-targets=" in args

    rendered_targets = _helm(
        *ENABLE_SETS,
        "--set", "adapter.allowedTargets[0]=east",
        "--set", "adapter.allowedTargets[1]=west",
    )
    docs_targets = _resources(rendered_targets.stdout)
    dep_targets = next(
        d for d in docs_targets
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "lightspeed-hub"
    )
    args_targets = dep_targets["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--allowed-targets=east,west" in args_targets


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_adapter_tls_disabled_is_plain_http_dev_shape() -> None:
    """adapter.tls.enabled=false (LOCAL DEV ONLY override): no TLS flags,
    no serving-cert annotation/mount/volume, port named http, plain-HTTP
    probes."""
    rendered = _helm(*ENABLE_SETS, "--set", "adapter.tls.enabled=false")
    docs = _resources(rendered.stdout)
    dep = next(
        d for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "lightspeed-hub"
    )
    pod_spec = dep["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    args = container["args"]
    assert not [a for a in args if a.startswith("--tls-")]
    assert container["ports"][0]["name"] == "http"
    assert "volumeMounts" not in container
    assert "volumes" not in pod_spec
    for probe in ("livenessProbe", "readinessProbe"):
        http_get = container[probe]["httpGet"]
        assert http_get["port"] == "http"
        assert "scheme" not in http_get

    svc = next(
        d
        for d in docs
        if d["kind"] == "Service" and d["metadata"]["name"] == "lightspeed-hub"
    )
    assert "annotations" not in svc["metadata"]
    assert svc["spec"]["ports"] == [
        {"name": "http", "port": 8443, "targetPort": "http", "protocol": "TCP"}
    ]


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_adapter_service_carries_serving_cert_annotation() -> None:
    """The Service annotation mints the serving cert the Deployment
    mounts: name defaults to <fullname>-serving-cert and is overridable."""
    rendered = _helm(*ENABLE_SETS)
    docs = _resources(rendered.stdout)
    svc = next(
        d
        for d in docs
        if d["kind"] == "Service" and d["metadata"]["name"] == "lightspeed-hub"
    )
    assert svc["metadata"]["annotations"] == {
        "service.beta.openshift.io/serving-cert-secret-name": "lightspeed-hub-serving-cert"
    }
    assert svc["spec"]["ports"] == [
        {"name": "https", "port": 8443, "targetPort": "https", "protocol": "TCP"}
    ]

    rendered_named = _helm(
        *ENABLE_SETS,
        "--set", "adapter.tls.servingCertSecretName=my-cert",
    )
    docs_named = _resources(rendered_named.stdout)
    svc_named = next(
        d
        for d in docs_named
        if d["kind"] == "Service" and d["metadata"]["name"] == "lightspeed-hub"
    )
    assert svc_named["metadata"]["annotations"] == {
        "service.beta.openshift.io/serving-cert-secret-name": "my-cert"
    }
    dep_named = next(
        d for d in docs_named
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "lightspeed-hub"
    )
    assert dep_named["spec"]["template"]["spec"]["volumes"] == [
        {"name": "serving-cert", "secret": {"secretName": "my-cert"}}
    ]


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_adapter_svid_mode() -> None:
    """SVID mode (adapter.svid.enabled): no client-id flag, no client
    secret env, SPIFFE_SOCKET passthrough -- mirroring the adapter's
    config.Validate client-auth exclusivity. clientId alongside SVID mode
    fails loudly at render time (the adapter would reject it at startup).
    """
    svid_sets = [
        "--set", "enabled=true",
        "--set", "adapter.hubAudience=lightspeed-hub-a2a",
        "--set", "adapter.svid.enabled=true",
    ]
    rendered = _helm(*svid_sets, *ENABLE_SETS[8:])  # gateway keys only
    docs = _resources(rendered.stdout)
    dep = next(
        d for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "lightspeed-hub"
    )
    container = dep["spec"]["template"]["spec"]["containers"][0]
    args = container["args"]
    assert not [a for a in args if a.startswith("--keycloak-client-id")]
    env = {e["name"]: e for e in container["env"]}
    assert set(env) == {"SPIFFE_SOCKET"}
    assert env["SPIFFE_SOCKET"]["value"] == "unix:///run/spire/sockets/agent.sock"

    # SVID mode + clientId is rejected at render time.
    result = _helm(*svid_sets, "--set", "adapter.keycloak.clientId=x", check=False)
    assert result.returncode != 0
    assert "adapter.keycloak.clientId must be EMPTY" in result.stderr


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_network_policy_admits_only_praxis_namespace() -> None:
    rendered = _helm(*ENABLE_SETS)
    docs = _resources(rendered.stdout)
    np = next(d for d in docs if d["kind"] == "NetworkPolicy")
    assert np["spec"]["policyTypes"] == ["Ingress"]
    # podSelector is a LabelSelector: the adapter's labels MUST sit under
    # matchLabels -- placed directly on podSelector they are unknown fields
    # the API server silently drops (live incident: applied as {}, selecting
    # every pod in the namespace, with a permanent Argo diff).
    assert np["spec"]["podSelector"] == {
        "matchLabels": {
            "app.kubernetes.io/name": "lightspeed-hub",
            "app.kubernetes.io/instance": "lightspeed-hub",
        }
    }
    assert np["spec"]["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": "praxis-proxy"
                        }
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 8443}],
        }
    ]


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_admission_policy_binding_references_param_configmap() -> None:
    rendered = _helm(*ENABLE_SETS)
    docs = _resources(rendered.stdout)
    policy = next(
        d for d in docs if d["kind"] == "ValidatingAdmissionPolicy"
    )
    binding = next(
        d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding"
    )
    # paramKind is the chart-managed ConfigMap...
    assert policy["spec"]["paramKind"] == {"apiVersion": "v1", "kind": "ConfigMap"}
    assert policy["spec"]["failurePolicy"] == "Fail"
    ops = policy["spec"]["matchConstraints"]["resourceRules"][0]
    assert ops["operations"] == ["CREATE", "UPDATE"]
    assert ops["resources"] == ["agenticruns"]
    # ...and the binding references it by name, denying when it is missing
    # (never fail open).
    assert binding["spec"]["policyName"] == policy["metadata"]["name"]
    assert binding["spec"]["validationActions"] == ["Deny"]
    assert binding["spec"]["paramRef"] == {
        "name": "lightspeed-hub-target-policy",
        "namespace": "default",
        "parameterNotFoundAction": "Deny",
    }
    # v1 bindings have no spec.namespace (v1beta1 shape; the API server
    # drops it and Argo shows a permanent diff) -- the run-namespace scope
    # is spelled matchResources.namespaceSelector instead:
    assert binding["spec"]["matchResources"]["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": "default"}
    }
    assert "namespace" not in binding["spec"]
    # Deny-all default: the rendered param ConfigMap carries no entries
    # until allowTargets is populated.
    param = next(
        d for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "lightspeed-hub-target-policy"
    )
    assert param["data"] in (None, {}, "")
    # With an allow entry, the mapping renders as key -> comma-joined targets.
    # Group names contain ':' which is invalid in ConfigMap keys: the chart
    # sanitizes them (':' -> '.') and the CEL expression applies the same
    # transformation before lookup.
    rendered_map = _helm(
        *ENABLE_SETS,
        "--set", "admissionPolicy.allowTargets.keycloak\\:acme-agent-rca[0]=example-spoke",
    )
    docs_map = _resources(rendered_map.stdout)
    param_map = next(
        d for d in docs_map
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "lightspeed-hub-target-policy"
    )
    assert param_map["data"] == {"keycloak.acme-agent-rca": "example-spoke"}


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_agentic_operator_configmap_carries_gateway_keys() -> None:
    rendered = _helm(*ENABLE_SETS)
    docs = _resources(rendered.stdout)
    cm = next(
        d for d in docs
        if d["kind"] == "ConfigMap"
        and d["metadata"]["name"] == "lightspeed-agentic-configuration"
    )
    assert cm["data"]["sandbox-credential-mode"] == "gateway"
    assert cm["data"]["gateway-server-url-template"] == (
        "https://praxis-spoke-{targetCluster}.praxis-proxy.svc.cluster.local:8443"
    )
    assert cm["data"]["gateway-ca-secret"] == "praxis-spoke-gateway-ca"


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_missing_required_values_fail_loudly() -> None:
    # Missing hub audience.
    result = _helm(
        "--set", "enabled=true",
        "--set", "adapter.keycloak.clientId=x",
        "--set", "adapter.keycloak.clientSecretSecret.name=x",
        check=False,
    )
    assert result.returncode != 0
    assert "adapter.hubAudience" in result.stderr

    # Missing exchange client.
    result = _helm(
        "--set", "enabled=true",
        "--set", "adapter.hubAudience=lightspeed-hub-a2a",
        check=False,
    )
    assert result.returncode != 0
    assert "adapter.keycloak.clientId" in result.stderr

    # Gateway mode without the repo C gateway keys (either one missing).
    result = _helm(
        "--set", "enabled=true",
        "--set", "adapter.hubAudience=lightspeed-hub-a2a",
        "--set", "adapter.keycloak.clientId=x",
        "--set", "adapter.keycloak.clientSecretSecret.name=x",
        "--set", "agenticOperator.config.gatewayCASecret=praxis-spoke-gateway-ca",
        check=False,
    )
    assert result.returncode != 0
    assert "gatewayServerURLTemplate" in result.stderr


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_variant_registers_application() -> None:
    """The pattern must carry the app entry (activated, hub-as-spoke) and
    its namespace.

    The standalone variant activates the Task B stack with the hub cluster
    as its own spoke ("hub"): enabled=true, one allowedTarget, the
    run-creators group bound to it, and the image pins for the adapter and
    agentic-operator.
    """
    variant = yaml.safe_load(VARIANT.read_text())
    cluster_group = variant["clusterGroup"]
    assert "lightspeed-hub" in cluster_group["namespaces"]
    app = cluster_group["applications"]["lightspeed-hub"]
    assert app["path"] == "charts/all/lightspeed-hub"
    assert app["namespace"] == "lightspeed-hub"
    assert app["argoProject"] == "hub"
    overrides = {o["name"]: o["value"] for o in app["overrides"]}
    assert overrides["enabled"] == "true"
    assert overrides["adapter.allowedTargets[0]"] == "hub"
    assert overrides["adapter.hubRunCreatorsGroup"] == "keycloak:acme-agent-rca"
    assert overrides["adapter.image.repository"] == "quay.io/ltomasbo/lightspeed-hub-a2a-adapter"
    assert overrides["agenticOperator.image.repository"] == "quay.io/ltomasbo/lightspeed-agentic-operator"
    assert overrides["agenticOperator.config.gatewayCASecret"] == "praxis-spoke-gateway-ca"


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_run_creators_rbac_gates_agenticrun_creation() -> None:
    """hubRunCreatorsGroup binds a Group to create-on-agenticruns only.

    "Only the configured group can create AgenticRuns" (Phase 1 RBAC
    goal): the value names the Kubernetes group as the hub API maps it
    from Token H's groups claim (e.g. keycloak:acme-agent-rca with the
    default OIDC groupsPrefix). Empty (default) renders nothing -- already
    pinned by test_enabled_render_has_expected_resources's exact Role /
    RoleBinding name lists.
    """
    rendered = _helm(
        *ENABLE_SETS,
        "--set", "adapter.hubRunCreatorsGroup=keycloak:acme-agent-rca",
    )
    docs = _resources(rendered.stdout)

    role = next(
        d for d in docs
        if d["kind"] == "Role" and d["metadata"]["name"] == "lightspeed-hub-run-creators"
    )
    # CREATE only, by design: creation is the gated act. The admission
    # policy (not RBAC) restricts WHICH targetCluster.
    assert role["rules"] == [
        {
            "apiGroups": ["agentic.openshift.io"],
            "resources": ["agenticruns"],
            "verbs": ["create"],
        }
    ]

    binding = next(
        d for d in docs
        if d["kind"] == "RoleBinding"
        and d["metadata"]["name"] == "lightspeed-hub-run-creators"
    )
    assert binding["subjects"] == [
        {
            "kind": "Group",
            "name": "keycloak:acme-agent-rca",
            "apiGroup": "rbac.authorization.k8s.io",
        }
    ]
    assert binding["roleRef"]["kind"] == "Role"
    assert binding["roleRef"]["name"] == "lightspeed-hub-run-creators"

    # Both land in the run namespace (the chart's release namespace here).
    run_ns = role["metadata"]["namespace"]
    assert binding["metadata"]["namespace"] == run_ns


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_agentic_operator_crds_render_and_land_before_deployment() -> None:
    """The vendored CRD set (repo C config/crd/bases, branch a2a-support)
    renders with the enabled chart: all ten agentic.openshift.io CRDs,
    each at argocd sync-wave -1 so ArgoCD applies them BEFORE the operator
    Deployment (default wave 0). agenticOperator.crds.install=false
    (opt-out) or agenticOperator.enabled=false renders none of them."""
    rendered = _helm(*ENABLE_SETS)
    docs = _resources(rendered.stdout)
    crds = [d for d in docs if d["kind"] == "CustomResourceDefinition"]

    # All ten, in the repo's API group (verified against the vendored
    # YAMLs, not assumed), with the CRD apiVersion.
    assert len(crds) == 10
    for crd in crds:
        assert crd["apiVersion"] == "apiextensions.k8s.io/v1"
        assert crd["spec"]["group"] == "agentic.openshift.io"
        # Sync-wave -1: before the operator Deployment.
        assert crd["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-1"

    dep = next(
        d for d in docs
        if d["kind"] == "Deployment"
        and d["metadata"]["name"] == "lightspeed-hub-agentic-operator"
    )
    dep_wave = int(
        dep["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", 0)
    )
    assert dep_wave > -1

    # The vendored bytes survive rendering: the agents CRD's prompt
    # descriptions contain literal Go-template examples ({{.Request}}),
    # which the chart escapes for Helm -- they must round-trip intact.
    agents = next(
        d for d in crds if d["metadata"]["name"] == "agents.agentic.openshift.io"
    )
    assert "{{.Request}}" in yaml.safe_dump(agents)

    # Opt-out: crds.install=false renders no CRDs (the rest stays).
    rendered_off = _helm(*ENABLE_SETS, "--set", "agenticOperator.crds.install=false")
    docs_off = _resources(rendered_off.stdout)
    assert not [d for d in docs_off if d["kind"] == "CustomResourceDefinition"]
    assert any(
        d["kind"] == "Deployment" and d["metadata"]["name"] == "lightspeed-hub-agentic-operator"
        for d in docs_off
    )


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_agentic_operator_webhook_cert_wiring() -> None:
    """Repo C's manager registers its admission webhooks unconditionally
    and exits at startup without tls.crt at its CertDir. The chart must
    provision a service-ca serving cert: a webhook Service with the
    serving-cert annotation, and the generated Secret mounted read-only at
    /tmp/k8s-webhook-server/serving-certs with port 9443 exposed. (No
    WebhookConfigurations are rendered yet -- documented limitation.)"""
    rendered = _helm(*ENABLE_SETS)
    docs = _resources(rendered.stdout)

    svc = next(
        d for d in docs
        if d["kind"] == "Service"
        and d["metadata"]["name"] == "lightspeed-hub-agentic-operator-webhook"
    )
    assert svc["metadata"]["annotations"][
        "service.beta.openshift.io/serving-cert-secret-name"
    ] == "lightspeed-hub-agentic-operator-webhook"
    assert svc["spec"]["ports"][0]["port"] == 9443

    dep = next(
        d for d in docs
        if d["kind"] == "Deployment"
        and d["metadata"]["name"] == "lightspeed-hub-agentic-operator"
    )
    container = dep["spec"]["template"]["spec"]["containers"][0]
    mounts = container["volumeMounts"]
    assert any(
        m["mountPath"] == "/tmp/k8s-webhook-server/serving-certs" and m["readOnly"]
        for m in mounts
    )
    assert any(p["name"] == "webhook" and p["containerPort"] == 9443 for p in container["ports"])
    volumes = dep["spec"]["template"]["spec"]["volumes"]
    assert any(
        v["secret"]["secretName"] == "lightspeed-hub-agentic-operator-webhook"
        for v in volumes
    )

    # The operator stack gate drops the CRDs too.
    rendered_noop = _helm(*ENABLE_SETS, "--set", "agenticOperator.enabled=false")
    docs_noop = _resources(rendered_noop.stdout)
    assert not [d for d in docs_noop if d["kind"] == "CustomResourceDefinition"]


@pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)
def test_agentic_operator_rbac_matches_vendored_upstream() -> None:
    """The operator RBAC is repo C's config/rbac/role.yaml, adapted only in
    naming: ClusterRole + ClusterRoleBinding carry the chart fullname, the
    binding subjects THIS chart's operator SA in the run namespace
    (upstream: controller-manager in a placeholder namespace), and both sit
    at sync-wave -1 (before the operator Deployment). The rules themselves
    are upstream's, verbatim -- including the deliberately cluster-wide
    secrets grant (see README)."""
    rendered = _helm(*ENABLE_SETS)
    docs = _resources(rendered.stdout)
    role = next(
        d for d in docs
        if d["kind"] == "ClusterRole"
        and d["metadata"]["name"] == "lightspeed-hub-agentic-operator-manager"
    )
    binding = next(
        d for d in docs
        if d["kind"] == "ClusterRoleBinding"
        and d["metadata"]["name"] == "lightspeed-hub-agentic-operator-manager"
    )

    # Spot-check the vendored rule set (full equality is pinned against
    # the upstream repo by its own drift test; here we pin the shape).
    by_resource = {
        tuple(r["resources"]): sorted(r["verbs"])
        for r in role["rules"]
        if r["apiGroups"] == [""]
    }
    assert by_resource[("secrets",)] == [
        "create", "delete", "get", "list", "update", "watch",
    ]
    agentic = [
        r for r in role["rules"] if r["apiGroups"] == ["agentic.openshift.io"]
    ]
    assert {
        tuple(r["resources"]): sorted(r["verbs"]) for r in agentic
    }[("agenticrunapprovals", "agenticruns")] == [
        "create", "delete", "get", "list", "patch", "update", "watch",
    ]
    # Cluster-scoped grants that force the ClusterRole shape upstream
    # (narrowing them into a namespaced Role would be invalid).
    groups = {tuple(r["apiGroups"]) for r in role["rules"]}
    assert ("rbac.authorization.k8s.io",) in groups
    assert ("apiextensions.k8s.io",) in groups

    # Binding: chart-named roleRef, this chart's operator SA in the run
    # namespace (release namespace here), sync-wave -1.
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "lightspeed-hub-agentic-operator-manager",
    }
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "lightspeed-hub-agentic-operator",
            "namespace": "default",
        }
    ]
    for obj in (role, binding):
        assert obj["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-1"
