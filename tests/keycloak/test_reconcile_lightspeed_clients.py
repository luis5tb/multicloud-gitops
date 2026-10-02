"""Unit checks for the idempotent Keycloak PostSync reconciler helpers."""

from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType
import unittest
from unittest.mock import Mock, patch


SCRIPT = (
    Path(__file__).parents[2]
    / "charts/all/keycloak-oidc/files/reconcile-lightspeed-clients.py"
)


class ReconcileLightspeedClientsTest(unittest.TestCase):
    """Exercise idempotent Keycloak Admin API reconciliation helpers."""

    def setUp(self):
        """Load the stdlib script with isolated test-only environment values."""
        values = {
            "KEYCLOAK_BASE_URL": "https://keycloak.example.test",
            "KEYCLOAK_REALM": "rca",
            "KEYCLOAK_ADMIN_USERNAME": "unused",
            "KEYCLOAK_ADMIN_PASSWORD": "unused",
            "ACME_CLIENT_ID": "acme-agent",
            "LIGHTSPEED_CLIENT_ID": "lightspeed-mcp",
            "LIGHTSPEED_CLIENT_NAME": "Lightspeed exchange client",
            "LIGHTSPEED_INBOUND_AUDIENCE": "lightspeed-a2a",
            "MCP_AUDIENCE_CLIENT_ID": "openshift-mcp",
            "SPIFFE_ALIAS": "spiffe",
            "LIGHTSPEED_SPIFFE_SUBJECT": "spiffe://example.test/ns/ols/sa/lightspeed",
            "ACME_GROUP_NAME": "acme-agent-lightspeed",
            "KEYCLOAK_SKIP_TLS_VERIFY": "false",
        }
        with patch.dict(os.environ, values):
            self.reconciler = ModuleType("lightspeed_reconciler")
            source = compile(SCRIPT.read_text(), str(SCRIPT), "exec")
            exec(source, self.reconciler.__dict__)

    def test_ensure_mapper_is_idempotent_when_mapper_matches(self):
        mapper = {
            "name": "groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "config": {"claim.name": "groups", "access.token.claim": "true"},
        }
        self.reconciler.call = Mock(return_value=[{**mapper, "id": "mapper-1"}])

        self.reconciler.ensure_mapper("admin-token", "client-1", mapper)

        self.reconciler.call.assert_called_once_with(
            "GET",
            "/admin/realms/rca/clients/client-1/protocol-mappers/models",
            "admin-token",
        )

    def test_ensure_mapper_updates_existing_drift(self):
        current = {
            "id": "mapper-1",
            "name": "groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "config": {"claim.name": "old-groups"},
        }
        mapper = {
            "name": "groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "config": {"claim.name": "groups", "access.token.claim": "true"},
        }
        self.reconciler.call = Mock(side_effect=[[current], None])

        self.reconciler.ensure_mapper("admin-token", "client-1", mapper)

        self.assertEqual(self.reconciler.call.call_count, 2)
        self.assertEqual(
            self.reconciler.call.call_args.args,
            (
                "PUT",
                "/admin/realms/rca/clients/client-1/protocol-mappers/models/mapper-1",
                "admin-token",
                {**mapper, "id": "mapper-1"},
            ),
        )

    def test_ensure_client_preserves_unmanaged_attributes(self):
        current = {
            "id": "lightspeed-uuid",
            "clientId": "lightspeed-mcp",
            "name": "old name",
            "attributes": {"operator-owned-note": "preserve-me"},
        }
        updated = {
            **current,
            "name": "Lightspeed exchange client",
            "attributes": {
                "operator-owned-note": "preserve-me",
                "jwt.credential.issuer": "spiffe",
                "jwt.credential.sub": "spiffe://example.test/ns/ols/sa/lightspeed",
                "standard.token.exchange.enabled": "true",
            },
        }
        self.reconciler.call = Mock(side_effect=[[current], None, [updated]])

        client_uuid = self.reconciler.ensure_client("admin-token")

        self.assertEqual(client_uuid, "lightspeed-uuid")
        self.assertEqual(self.reconciler.call.call_count, 3)
        put_body = self.reconciler.call.call_args_list[1].args[3]
        self.assertEqual(put_body["attributes"]["operator-owned-note"], "preserve-me")
        self.assertEqual(put_body["attributes"]["standard.token.exchange.enabled"], "true")

    def test_ensure_client_recovers_from_concurrent_create(self):
        concurrent_client = {
            "id": "lightspeed-uuid",
            "clientId": "lightspeed-mcp",
            "attributes": {"other": "keep"},
        }
        reconciler_error = self.reconciler.KeycloakAdminAPIError(
            "POST", "/admin/realms/rca/clients", 409
        )
        self.reconciler.call = Mock(
            side_effect=[[], reconciler_error, [concurrent_client], None, [concurrent_client]]
        )

        client_uuid = self.reconciler.ensure_client("admin-token")

        self.assertEqual(client_uuid, "lightspeed-uuid")
        self.assertEqual(self.reconciler.call.call_count, 5)
        put_body = self.reconciler.call.call_args_list[3].args[3]
        self.assertEqual(put_body["attributes"]["other"], "keep")
        self.assertEqual(put_body["attributes"]["standard.token.exchange.enabled"], "true")

    def test_ensure_mapper_recovers_from_concurrent_create(self):
        mapper = {
            "name": "groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "config": {"claim.name": "groups", "access.token.claim": "true"},
        }
        drifted = {**mapper, "id": "mapper-1", "config": {"claim.name": "old"}}
        conflict = self.reconciler.KeycloakAdminAPIError(
            "POST", "/admin/realms/rca/clients/client-1/protocol-mappers/models", 409
        )
        self.reconciler.call = Mock(side_effect=[[], conflict, [drifted], None])

        self.reconciler.ensure_mapper("admin-token", "client-1", mapper)

        self.assertEqual(self.reconciler.call.call_count, 4)
        self.assertEqual(
            self.reconciler.call.call_args.args,
            (
                "PUT",
                "/admin/realms/rca/clients/client-1/protocol-mappers/models/mapper-1",
                "admin-token",
                {**mapper, "id": "mapper-1"},
            ),
        )

    def test_ensure_group_recovers_from_concurrent_create(self):
        conflict = self.reconciler.KeycloakAdminAPIError(
            "POST", "/admin/realms/rca/groups", 409
        )
        self.reconciler.call = Mock(side_effect=[[], conflict, [{"id": "group-1", "name": "acme-agent-lightspeed"}]])

        group_id = self.reconciler.ensure_group("admin-token")

        self.assertEqual(group_id, "group-1")
        self.assertEqual(self.reconciler.call.call_count, 3)

    def test_admin_api_rejects_plaintext_http_origin(self):
        values = {
            "KEYCLOAK_BASE_URL": "http://keycloak.example.test",
            "KEYCLOAK_REALM": "rca",
            "KEYCLOAK_ADMIN_USERNAME": "unused",
            "KEYCLOAK_ADMIN_PASSWORD": "unused",
            "ACME_CLIENT_ID": "acme-agent",
            "LIGHTSPEED_CLIENT_ID": "lightspeed-mcp",
            "LIGHTSPEED_CLIENT_NAME": "Lightspeed exchange client",
            "LIGHTSPEED_INBOUND_AUDIENCE": "lightspeed-a2a",
            "MCP_AUDIENCE_CLIENT_ID": "openshift-mcp",
            "SPIFFE_ALIAS": "spiffe",
            "LIGHTSPEED_SPIFFE_SUBJECT": "spiffe://example.test/ns/ols/sa/lightspeed",
            "ACME_GROUP_NAME": "acme-agent-lightspeed",
            "KEYCLOAK_SKIP_TLS_VERIFY": "false",
        }
        with patch.dict(os.environ, values):
            invalid = ModuleType("lightspeed_reconciler_http")
            source = compile(SCRIPT.read_text(), str(SCRIPT), "exec")
            exec(source, invalid.__dict__)

        with self.assertRaisesRegex(RuntimeError, "HTTPS origin"):
            invalid.validate_admin_base_url()

    def test_group_membership_reconciler_does_not_add_existing_membership(self):
        self.reconciler.call = Mock(
            side_effect=[
                [{"id": "user-1", "username": "service-account-acme-agent"}],
                [{"id": "group-1"}],
            ]
        )

        self.reconciler.ensure_acme_group_membership("admin-token", "group-1")

        self.assertEqual(self.reconciler.call.call_count, 2)
        self.assertTrue(
            all(call.args[0] == "GET" for call in self.reconciler.call.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
