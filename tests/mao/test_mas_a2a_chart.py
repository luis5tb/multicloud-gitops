"""Render-level safety and configuration checks for the MAO A2A charts."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
HELM = "helm"


def helm_template(chart: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [HELM, "template", "test-release", str(ROOT / chart), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class MasA2AChartTests(unittest.TestCase):
    def test_standalone_activates_the_a2a_application(self) -> None:
        # The standalone variant deliberately activates inbound MAS A2A. Assert
        # it is wired on with the safety-critical configuration the chart's
        # render guards require (enabled, a blueprint, an HTTPS public base URL,
        # at least one allowed principal, and the Route).
        variant = ROOT / "variants/standalone/values-standalone.yaml"
        application = (
            variant.read_text(encoding="utf-8")
            .split("    mao-a2a:\n", 1)[1]
            .split("  argoCD:\n", 1)[0]
        )
        self.assertIn("name: mao-a2a", application)
        self.assertIn("namespace: mao", application)
        self.assertIn("path: charts/all/mao/mas-a2a", application)
        self.assertIn("- name: a2a.enabled", application)
        self.assertIn('value: "true"', application)
        self.assertIn("- name: a2a.blueprintId", application)
        self.assertIn("- name: a2a.publicBaseUrl", application)
        self.assertIn("value: \"https://", application)
        self.assertIn("- name: a2a.allowedPrincipals[0]", application)
        self.assertIn("- name: route.enabled", application)

    def test_default_is_dormant_and_renders_no_resources(self) -> None:
        result = helm_template("charts/all/mao/mas-a2a")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")

    def test_enabled_keycloak_deployment_wires_shared_dependencies(self) -> None:
        result = helm_template(
            "charts/all/mao/mas-a2a",
            "--set", "a2a.enabled=true",
            "--set-string", "a2a.blueprintId=existing-blueprint",
            "--set-string", "a2a.publicBaseUrl=https://mas-a2a.example.test",
            "--set-string", "a2a.allowedPrincipals[0]=test-subject",
            "--set", "route.enabled=true",
            "--set-string", "identity.keycloakAudience=mas-a2a",
            "--set-string", "global.clusterBaseDomain=example.test",
            "--set-string", "global.keycloakRealm=rca",
            "--set-string", "image.repository=registry.invalid/mas-api",
            "--set-string", "image.tag=0123456789ab",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = result.stdout
        self.assertIn("kind: Deployment", rendered)
        self.assertIn("kind: Service", rendered)
        self.assertIn("kind: Route", rendered)
        self.assertIn("A2A_ENABLED", rendered)
        self.assertIn("value: a2a-serve", rendered)
        self.assertIn('value: "existing-blueprint"', rendered)
        self.assertIn('value: "mas-a2a"', rendered)
        self.assertIn('value: "https://keycloak.apps.example.test/realms/rca"', rendered)
        self.assertIn("A2A_ALLOWED_PRINCIPALS", rendered)
        self.assertIn("MONGODB_IP", rendered)
        self.assertIn("REDIS_IP", rendered)
        self.assertIn("TEMPORAL_IP", rendered)
        self.assertIn("ROLE", rendered)
        self.assertNotIn(":latest", rendered)

    def test_activation_fails_closed_without_required_operator_values(self) -> None:
        base = (
            "--set", "a2a.enabled=true",
            "--set-string", "image.repository=registry.invalid/mas-api",
            "--set-string", "image.tag=0123456789ab",
            "--set-string", "global.clusterBaseDomain=example.test",
            "--set-string", "global.keycloakRealm=rca",
        )
        missing_blueprint = helm_template(
            "charts/all/mao/mas-a2a",
            *base,
            "--set-string", "a2a.publicBaseUrl=https://mas-a2a.example.test",
            "--set-string", "a2a.allowedPrincipals[0]=test-subject",
            "--set-string", "identity.keycloakAudience=mas-a2a",
        )
        self.assertNotEqual(missing_blueprint.returncode, 0)
        self.assertIn("a2a.blueprintId", missing_blueprint.stderr)

        missing_subjects = helm_template(
            "charts/all/mao/mas-a2a",
            *base,
            "--set-string", "a2a.blueprintId=existing-blueprint",
            "--set-string", "a2a.publicBaseUrl=https://mas-a2a.example.test",
            "--set-string", "identity.keycloakAudience=mas-a2a",
        )
        self.assertNotEqual(missing_subjects.returncode, 0)
        self.assertIn("a2a.allowedPrincipals", missing_subjects.stderr)

        missing_audience = helm_template(
            "charts/all/mao/mas-a2a",
            *base,
            "--set-string", "a2a.blueprintId=existing-blueprint",
            "--set-string", "a2a.publicBaseUrl=https://mas-a2a.example.test",
            "--set-string", "a2a.allowedPrincipals[0]=test-subject",
        )
        self.assertNotEqual(missing_audience.returncode, 0)
        self.assertIn("identity.keycloakAudience", missing_audience.stderr)

    def test_dev_mode_is_rejected_for_enabled_a2a(self) -> None:
        values = (
            "--set", "a2a.enabled=true",
            "--set", "identity.providerMode=dev",
            "--set-string", "a2a.blueprintId=development-blueprint",
            "--set-string", "a2a.allowedPrincipals[0]=dev-user",
            "--set-string", "image.repository=registry.invalid/mas-api",
            "--set-string", "image.tag=0123456789ab",
        )
        http_result = helm_template(
            "charts/all/mao/mas-a2a",
            *values,
            "--set-string", "a2a.publicBaseUrl=http://mas-a2a.local",
        )
        self.assertNotEqual(http_result.returncode, 0)
        self.assertIn("identity.providerMode=dev", http_result.stderr)

        https_result = helm_template(
            "charts/all/mao/mas-a2a",
            *values,
            "--set-string", "a2a.publicBaseUrl=https://mas-a2a.example.test",
        )
        self.assertNotEqual(https_result.returncode, 0)
        self.assertIn("identity.providerMode=dev", https_result.stderr)

    def test_keycloak_mode_rejects_delimiter_or_whitespace_only_audiences(self) -> None:
        # Helm's --set parser treats commas as value separators, so escape
        # each comma while retaining whitespace-only CSV fields in the value.
        audience = "identity.keycloakAudience= " + "\\," + "\t" + "\\," + "  "
        a2a = helm_template(
            "charts/all/mao/mas-a2a",
            "--set", "a2a.enabled=true",
            "--set-string", "a2a.blueprintId=existing-blueprint",
            "--set-string", "a2a.publicBaseUrl=https://mas-a2a.example.test",
            "--set-string", "a2a.allowedPrincipals[0]=test-subject",
            "--set-string", audience,
            "--set-string", "global.clusterBaseDomain=example.test",
            "--set-string", "global.keycloakRealm=rca",
            "--set-string", "image.repository=registry.invalid/mas-api",
            "--set-string", "image.tag=0123456789ab",
        )
        self.assertNotEqual(a2a.returncode, 0)
        self.assertIn("must contain at least one non-empty audience", a2a.stderr)

        for chart in ("mas-api", "mas-worker"):
            with self.subTest(chart=chart):
                dev_mode = helm_template(
                    f"charts/all/mao/{chart}",
                    "--set-string", "identity.providerMode=dev",
                    "--set", "identity.keycloakCaTrust.enabled=true",
                )
                self.assertEqual(dev_mode.returncode, 0, dev_mode.stderr)
                self.assertIn("name: build-keycloak-ca-bundle", dev_mode.stdout)
                self.assertIn("name: SSL_CERT_FILE", dev_mode.stdout)
                self.assertIn(
                    "name: CURL_CA_BUNDLE\n"
                    "              value: /var/run/mas-keycloak-trust/ca-bundle.crt",
                    dev_mode.stdout,
                )
                self.assertIn('name: "keycloak-oidc-ca"', dev_mode.stdout)

                result = helm_template(
                    f"charts/all/mao/{chart}",
                    "--set-string", "identity.providerMode=keycloak",
                    "--set-string", audience,
                    "--set-string", "global.clusterBaseDomain=example.test",
                    "--set-string", "global.keycloakRealm=rca",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must contain at least one non-empty audience", result.stderr)

    def test_enabled_a2a_rejects_reserved_audiences_in_comma_delimited_values(self) -> None:
        base = (
            "--set", "a2a.enabled=true",
            "--set-string", "a2a.blueprintId=existing-blueprint",
            "--set-string", "a2a.publicBaseUrl=https://mas-a2a.example.test",
            "--set-string", "a2a.allowedPrincipals[0]=test-subject",
            "--set-string", "global.clusterBaseDomain=example.test",
            "--set-string", "global.keycloakRealm=rca",
            "--set-string", "image.repository=registry.invalid/mas-api",
            "--set-string", "image.tag=0123456789ab",
        )
        for audience in ("openshift", "openshift-mcp", "lightspeed-a2a"):
            with self.subTest(audience=audience):
                result = helm_template(
                    "charts/all/mao/mas-a2a",
                    *base,
                    "--set-string", "identity.keycloakAudience=mas-a2a\\," + audience,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("reserved OpenShift/OLS/MCP audience", result.stderr)

        custom_reserved = helm_template(
            "charts/all/mao/mas-a2a",
            *base,
            "--set-string", "identity.keycloakAudience=mas-a2a\\, org-reserved",
            "--set-string", "identity.forbiddenAudiences[0]=org-reserved",
        )
        self.assertNotEqual(custom_reserved.returncode, 0)
        self.assertIn('"org-reserved" is a reserved', custom_reserved.stderr)

    def test_redis_channel_secret_is_required_when_streaming_and_limits_are_off(self) -> None:
        result = helm_template(
            "charts/all/mao/mas-a2a",
            "--set", "a2a.enabled=true",
            "--set", "a2a.streamingEnabled=false",
            "--set", "a2a.clusterLimitsEnabled=false",
            "--set-string", "a2a.blueprintId=existing-blueprint",
            "--set-string", "a2a.publicBaseUrl=https://mas-a2a.example.test",
            "--set-string", "a2a.allowedPrincipals[0]=test-subject",
            "--set-string", "identity.keycloakAudience=mas-a2a",
            "--set-string", "global.clusterBaseDomain=example.test",
            "--set-string", "global.keycloakRealm=rca",
            "--set-string", "image.repository=registry.invalid/mas-api",
            "--set-string", "image.tag=0123456789ab",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(
            result.stdout,
            r"name: REDIS_IP\s+valueFrom:\s+secretKeyRef:\s+name: mao-secret-redis\s+key: db\.endpoint",
        )
        self.assertRegex(
            result.stdout,
            r"name: REDIS_PORT\s+valueFrom:\s+secretKeyRef:\s+name: mao-secret-redis\s+key: db\.port",
        )

    def test_enabled_direct_route_requires_tls_and_redirect(self) -> None:
        values = (
            "--set", "a2a.enabled=true",
            "--set", "route.enabled=true",
            "--set-string", "a2a.blueprintId=existing-blueprint",
            "--set-string", "a2a.publicBaseUrl=https://mas-a2a.example.test",
            "--set-string", "a2a.allowedPrincipals[0]=test-subject",
            "--set-string", "identity.keycloakAudience=mas-a2a",
            "--set-string", "global.clusterBaseDomain=example.test",
            "--set-string", "global.keycloakRealm=rca",
            "--set-string", "image.repository=registry.invalid/mas-api",
            "--set-string", "image.tag=0123456789ab",
        )
        invalid_tls_values = (
            ("--set", "route.tls=null"),
            ("--set", "route.tls.termination=null"),
            ("--set-string", "route.tls.termination=insecure"),
            ("--set-string", "route.tls.termination=passthrough"),
            ("--set-string", "route.tls.termination=reencrypt"),
            ("--set-string", "route.tls.insecureEdgeTerminationPolicy=None"),
            ("--set", "route.tls.insecureEdgeTerminationPolicy=null"),
        )
        for tls_override in invalid_tls_values:
            with self.subTest(tls_override=tls_override):
                result = helm_template(
                    "charts/all/mao/mas-a2a",
                    *values,
                    *tls_override,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("route.tls", result.stderr)

    def test_api_and_worker_expose_keycloak_validator_settings(self) -> None:
        for chart in ("mas-api", "mas-worker"):
            with self.subTest(chart=chart):
                result = helm_template(
                    f"charts/all/mao/{chart}",
                    "--set-string", "identity.providerMode=keycloak",
                    "--set-string", "identity.keycloakAudience=mas-a2a",
                    "--set-string", "global.clusterBaseDomain=example.test",
                    "--set-string", "global.keycloakRealm=rca",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("KEYCLOAK_ISSUER_URI", result.stdout)
                self.assertIn("KEYCLOAK_JWKS_URI", result.stdout)
                self.assertIn("KEYCLOAK_AUDIENCE", result.stdout)
                self.assertIn('value: "mas-a2a"', result.stdout)
                self.assertIn(
                    'value: "https://keycloak.apps.example.test/realms/rca"',
                    result.stdout,
                )
                self.assertNotIn("build-keycloak-ca-bundle", result.stdout)

    def test_api_and_worker_support_image_digest_precedence(self) -> None:
        for chart in ("mas-api", "mas-worker"):
            with self.subTest(chart=chart):
                default = helm_template(f"charts/all/mao/{chart}")
                self.assertEqual(default.returncode, 0, default.stderr)
                self.assertIn(
                    'image: "quay.io/redhat-services-prod/mas-tenant/mas-api:latest"',
                    default.stdout,
                )
                result = helm_template(
                    f"charts/all/mao/{chart}",
                    "--set-string", "image.tag=release-tag",
                    "--set-string", "image.digest=sha256:0123456789abcdef",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(
                    "image: \"quay.io/redhat-services-prod/mas-tenant/mas-api@sha256:0123456789abcdef\"",
                    result.stdout,
                )
                self.assertNotIn(":release-tag", result.stdout)

    def test_mas_private_ca_trust_extends_existing_roots(self) -> None:
        a2a = helm_template(
            "charts/all/mao/mas-a2a",
            "--set", "a2a.enabled=true",
            "--set-string", "a2a.blueprintId=existing-blueprint",
            "--set-string", "a2a.publicBaseUrl=https://mas-a2a.example.test",
            "--set-string", "a2a.allowedPrincipals[0]=test-subject",
            "--set-string", "identity.keycloakAudience=mas-a2a",
            "--set", "identity.keycloakCaTrust.enabled=true",
            "--set-string", "global.clusterBaseDomain=example.test",
            "--set-string", "global.keycloakRealm=rca",
            "--set-string", "image.repository=registry.invalid/mas-api",
            "--set-string", "image.tag=0123456789ab",
        )
        self.assertEqual(a2a.returncode, 0, a2a.stderr)
        self.assertIn("name: build-keycloak-ca-bundle", a2a.stdout)
        self.assertIn("image: \"registry.invalid/mas-api:0123456789ab\"", a2a.stdout)
        self.assertIn("refusing private-CA-only trust", a2a.stdout)
        self.assertIn("name: SSL_CERT_FILE", a2a.stdout)
        self.assertIn("value: /var/run/mas-keycloak-trust/ca-bundle.crt", a2a.stdout)
        self.assertIn('name: KEYCLOAK_VERIFY_SSL\n              value: "true"', a2a.stdout)
        self.assertIn("name: keycloak-ca-trust", a2a.stdout)
        self.assertIn('name: "keycloak-oidc-ca"', a2a.stdout)
        self.assertIn("mountPath: /var/run/mas-keycloak-trust", a2a.stdout)

        for chart in ("mas-api", "mas-worker"):
            with self.subTest(chart=chart):
                result = helm_template(
                    f"charts/all/mao/{chart}",
                    "--set-string", "identity.providerMode=keycloak",
                    "--set-string", "identity.keycloakAudience=mas-api",
                    "--set", "identity.keycloakCaTrust.enabled=true",
                    "--set-string", "global.clusterBaseDomain=example.test",
                    "--set-string", "global.keycloakRealm=rca",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("name: build-keycloak-ca-bundle", result.stdout)
                self.assertIn("name: SSL_CERT_FILE", result.stdout)
                self.assertIn('name: KEYCLOAK_VERIFY_SSL\n              value: "true"', result.stdout)
                self.assertIn('name: "keycloak-oidc-ca"', result.stdout)
                self.assertIn("refusing private-CA-only trust", result.stdout)

                insecure = helm_template(
                    f"charts/all/mao/{chart}",
                    "--set-string", "identity.providerMode=keycloak",
                    "--set-string", "identity.keycloakAudience=mas-api",
                    "--set", "identity.keycloakCaTrust.enabled=true",
                    "--set", "identity.keycloakVerifySsl=false",
                    "--set-string", "global.clusterBaseDomain=example.test",
                    "--set-string", "global.keycloakRealm=rca",
                )
                self.assertNotEqual(insecure.returncode, 0)
                self.assertIn("requires identity.keycloakVerifySsl=true", insecure.stderr)

    def test_keycloak_ca_sync_can_publish_to_opt_in_additional_namespace(self) -> None:
        default_targets = helm_template(
            "charts/all/keycloak-oidc",
            "--set-string", "global.clusterBaseDomain=example.test",
            "--set", "keycloak.spiffeIdentityProvider.enabled=false",
            "--set", "openshiftOIDC.caBundleSync.enabled=true",
            "--set-string", "openshiftOIDC.caConfigMapName=keycloak-oidc-ca",
        )
        self.assertEqual(default_targets.returncode, 0, default_targets.stderr)
        self.assertIn('value: "[\\"openshift-config\\",\\"keycloak-system\\"]"', default_targets.stdout)
        self.assertNotIn("namespace: mao", default_targets.stdout)

        result = helm_template(
            "charts/all/keycloak-oidc",
            "--set-string", "global.clusterBaseDomain=example.test",
            "--set", "keycloak.spiffeIdentityProvider.enabled=false",
            "--set", "openshiftOIDC.caBundleSync.enabled=true",
            "--set-string", "openshiftOIDC.caConfigMapName=keycloak-oidc-ca",
            "--set-string", "openshiftOIDC.caBundleSync.additionalTargetNamespaces[0]=mao",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('value: "[\\"openshift-config\\",\\"keycloak-system\\",\\"mao\\"]"', result.stdout)
        self.assertRegex(result.stdout, r"kind: Role\s+metadata:\s+name: keycloak-oidc-keycloak-ca-sync-target\s+namespace: mao")
        self.assertRegex(result.stdout, r"kind: RoleBinding\s+metadata:\s+name: keycloak-oidc-keycloak-ca-sync-target\s+namespace: mao")
        self.assertIn('resourceNames: ["keycloak-oidc-ca"]', result.stdout)

    def test_keycloak_realm_import_declares_a_distinct_mas_audience(self) -> None:
        result = helm_template(
            "charts/all/keycloak-oidc",
            "--set-string", "global.clusterBaseDomain=example.test",
            "--set", "keycloak.spiffeIdentityProvider.enabled=false",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("name: mas-a2a-audience", result.stdout)
        self.assertIn('included.custom.audience: "mas-a2a"', result.stdout)
        self.assertNotIn('included.custom.audience: "openshift-mcp"', result.stdout)

    def test_keycloak_chart_rejects_a_shared_non_mas_audience(self) -> None:
        result = helm_template(
            "charts/all/keycloak-oidc",
            "--set", "keycloak.spiffeIdentityProvider.enabled=false",
            "--set-string", "global.clusterBaseDomain=example.test",
            "--set-string", "keycloak.masA2aAudience=openshift",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be distinct", result.stderr)


if __name__ == "__main__":
    unittest.main()
