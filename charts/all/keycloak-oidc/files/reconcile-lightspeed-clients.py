"""Idempotently reconcile Keycloak clients/mappers needed by Lightspeed A2A."""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = os.environ["KEYCLOAK_BASE_URL"].rstrip("/")
REALM = urllib.parse.quote(os.environ["KEYCLOAK_REALM"], safe="")
ADMIN_USERNAME = os.environ["KEYCLOAK_ADMIN_USERNAME"]
ADMIN_PASSWORD = os.environ["KEYCLOAK_ADMIN_PASSWORD"]
ACME_CLIENT_ID = os.environ["ACME_CLIENT_ID"]
LIGHTSPEED_CLIENT_ID = os.environ["LIGHTSPEED_CLIENT_ID"]
LIGHTSPEED_CLIENT_NAME = os.environ["LIGHTSPEED_CLIENT_NAME"]
LIGHTSPEED_INBOUND_AUDIENCE = os.environ["LIGHTSPEED_INBOUND_AUDIENCE"]
MCP_AUDIENCE_CLIENT_ID = os.environ["MCP_AUDIENCE_CLIENT_ID"]
SPIFFE_ALIAS = os.environ["SPIFFE_ALIAS"]
LIGHTSPEED_SPIFFE_SUBJECT = os.environ["LIGHTSPEED_SPIFFE_SUBJECT"]
ACME_GROUP_NAME = os.environ["ACME_GROUP_NAME"]

ca_bundle = os.environ.get("KEYCLOAK_CA_BUNDLE", "")
skip_tls_verify = os.environ.get("KEYCLOAK_SKIP_TLS_VERIFY", "false").lower() == "true"
if skip_tls_verify:
    print("WARNING: TLS certificate verification is disabled for Keycloak requests.")
    TLS_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    TLS_CONTEXT.check_hostname = False
    TLS_CONTEXT.verify_mode = ssl.CERT_NONE
else:
    TLS_CONTEXT = ssl.create_default_context(cafile=ca_bundle or None)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail rather than forward Keycloak admin credentials to a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, new_url):
        return None


HTTP = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=TLS_CONTEXT), _NoRedirectHandler()
)


class KeycloakAdminAPIError(RuntimeError):
    """Admin API failure exposing only its HTTP status for safe retry logic."""

    def __init__(self, method: str, path: str, status_code: int) -> None:
        """Create an error without retaining the response body."""
        self.status_code = status_code
        super().__init__(f"Keycloak Admin API {method} {path} returned HTTP {status_code}")


def call(method: str, path: str, token: str = "", data: object = None):
    """Call the Keycloak Admin API without logging credentials or response data."""
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        BASE_URL + path, data=body, headers=headers, method=method
    )
    try:
        with HTTP.open(request, timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        # Do not print response bodies: they can contain user/client data.
        raise KeycloakAdminAPIError(method, path, error.code) from None


def get_admin_token() -> str:
    """Acquire a short-lived master-realm admin token."""
    form = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": ADMIN_USERNAME,
            "password": ADMIN_PASSWORD,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/realms/master/protocol/openid-connect/token",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with HTTP.open(request, timeout=20) as response:
            payload = json.loads(response.read())
    except (urllib.error.HTTPError, json.JSONDecodeError) as error:
        status = getattr(error, "code", "invalid response")
        raise RuntimeError(f"Keycloak admin authentication failed: {status}") from None
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Keycloak admin token response did not contain access_token")
    return token


def find_client(token: str, client_id: str) -> dict:
    """Find exactly one existing client by its public clientId."""
    query = urllib.parse.urlencode({"clientId": client_id})
    for attempt in range(12):
        clients = call("GET", f"/admin/realms/{REALM}/clients?{query}", token)
        matches = [client for client in (clients or []) if client.get("clientId") == client_id]
        if len(matches) > 1:
            raise RuntimeError(
                f"Expected exactly one Keycloak client {client_id!r}; found {len(matches)}"
            )
        if matches:
            return matches[0]
        if attempt < 11:
            # On first install KeycloakRealmImport may still be creating the
            # source ACME/audience clients when this PostSync hook starts.
            time.sleep(5)
    raise RuntimeError(f"Keycloak client {client_id!r} was not found after waiting")


def ensure_client(token: str) -> str:
    """Create/update only the Lightspeed client's managed settings."""
    clients = call(
        "GET",
        f"/admin/realms/{REALM}/clients?{urllib.parse.urlencode({'clientId': LIGHTSPEED_CLIENT_ID})}",
        token,
    )
    matches = [
        client for client in (clients or [])
        if client.get("clientId") == LIGHTSPEED_CLIENT_ID
    ]
    desired = {
        "clientId": LIGHTSPEED_CLIENT_ID,
        "name": LIGHTSPEED_CLIENT_NAME,
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "standardFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "clientAuthenticatorType": "federated-jwt",
        "attributes": {
            "jwt.credential.issuer": SPIFFE_ALIAS,
            "jwt.credential.sub": LIGHTSPEED_SPIFFE_SUBJECT,
            "standard.token.exchange.enabled": "true",
        },
    }
    if len(matches) > 1:
        raise RuntimeError(f"Multiple Keycloak clients found for {LIGHTSPEED_CLIENT_ID!r}")
    if not matches:
        try:
            call("POST", f"/admin/realms/{REALM}/clients", token, desired)
        except KeycloakAdminAPIError as error:
            if error.status_code != 409:
                raise
            # Another reconciler or a fresh RealmImport created it between
            # our GET and POST. Re-read, then apply only our managed fields.
            matches = [find_client(token, LIGHTSPEED_CLIENT_ID)]
    existing = matches[0] if matches else find_client(token, LIGHTSPEED_CLIENT_ID)
    client_uuid = existing["id"]
    merged = dict(existing)
    merged.update({key: value for key, value in desired.items() if key != "attributes"})
    attributes = dict(existing.get("attributes") or {})
    attributes.update(desired["attributes"])
    merged["attributes"] = attributes
    call("PUT", f"/admin/realms/{REALM}/clients/{client_uuid}", token, merged)
    return find_client(token, LIGHTSPEED_CLIENT_ID)["id"]


def ensure_mapper(token: str, client_uuid: str, mapper: dict) -> None:
    """Create or update a named protocol mapper without duplicating it."""
    path = f"/admin/realms/{REALM}/clients/{client_uuid}/protocol-mappers/models"
    mappers = call("GET", path, token) or []
    existing = [item for item in mappers if item.get("name") == mapper["name"]]
    if len(existing) > 1:
        raise RuntimeError(
            f"Multiple protocol mappers named {mapper['name']!r} on a managed client"
        )
    if not existing:
        try:
            call("POST", path, token, mapper)
            return
        except KeycloakAdminAPIError as error:
            if error.status_code != 409:
                raise
            # Concurrent sync created the same mapper. Re-read and converge
            # its managed fields rather than failing or leaving drift.
            mappers = call("GET", path, token) or []
            existing = [item for item in mappers if item.get("name") == mapper["name"]]
            if len(existing) != 1:
                raise RuntimeError(
                    f"Could not reconcile protocol mapper {mapper['name']!r} after a create conflict"
                ) from None
    current = existing[0]
    desired = dict(mapper)
    desired["id"] = current["id"]
    if any(current.get(key) != desired.get(key) for key in ("protocol", "protocolMapper", "config")):
        call("PUT", f"{path}/{current['id']}", token, desired)


def ensure_group(token: str) -> str:
    """Find/create the ACME authorization group and return its id."""
    groups_path = f"/admin/realms/{REALM}/groups"
    query = urllib.parse.urlencode({"search": ACME_GROUP_NAME})
    groups = call("GET", f"{groups_path}?{query}", token) or []
    matches = [group for group in groups if group.get("name") == ACME_GROUP_NAME]
    if len(matches) > 1:
        raise RuntimeError(f"Multiple Keycloak groups found for {ACME_GROUP_NAME!r}")
    if not matches:
        try:
            call("POST", groups_path, token, {"name": ACME_GROUP_NAME})
        except KeycloakAdminAPIError as error:
            if error.status_code != 409:
                raise
            # A concurrent sync created it after our initial GET.
        groups = call("GET", f"{groups_path}?{query}", token) or []
        matches = [group for group in groups if group.get("name") == ACME_GROUP_NAME]
    if len(matches) != 1:
        raise RuntimeError(f"Could not reconcile Keycloak group {ACME_GROUP_NAME!r}")
    return matches[0]["id"]


def ensure_acme_group_membership(token: str, group_id: str) -> None:
    """Put ACME's service-account user in the group used by OpenShift RBAC."""
    username = f"service-account-{ACME_CLIENT_ID}"
    users = call(
        "GET",
        f"/admin/realms/{REALM}/users?{urllib.parse.urlencode({'username': username, 'exact': 'true'})}",
        token,
    ) or []
    matches = [item for item in users if item.get("username") == username]
    if len(matches) != 1:
        raise RuntimeError(f"Could not find exactly one ACME service-account user {username!r}")
    user_id = matches[0]["id"]
    current = call("GET", f"/admin/realms/{REALM}/users/{user_id}/groups", token) or []
    if any(group.get("id") == group_id for group in current):
        return
    try:
        call("PUT", f"/admin/realms/{REALM}/users/{user_id}/groups/{group_id}", token)
    except KeycloakAdminAPIError as error:
        if error.status_code != 409:
            raise
        # A competing PostSync run may have added the membership after our GET.
        current = call("GET", f"/admin/realms/{REALM}/users/{user_id}/groups", token) or []
        if not any(group.get("id") == group_id for group in current):
            raise RuntimeError("Could not reconcile ACME Keycloak group membership") from None


def validate_admin_base_url() -> None:
    """Require a bare HTTPS origin before sending Keycloak admin credentials."""
    try:
        parsed_base_url = urllib.parse.urlsplit(BASE_URL)
        valid_base_url = (
            parsed_base_url.scheme == "https"
            and bool(parsed_base_url.hostname)
            and parsed_base_url.username is None
            and parsed_base_url.password is None
            and parsed_base_url.path in {"", "/"}
            and not parsed_base_url.query
            and not parsed_base_url.fragment
        )
        if parsed_base_url.port is not None and not (
            1 <= parsed_base_url.port <= 65535
        ):
            valid_base_url = False
    except ValueError:
        valid_base_url = False
    if not valid_base_url:
        raise RuntimeError("Keycloak Admin API base URL must be a verified HTTPS origin")


def main() -> None:
    validate_admin_base_url()
    token = get_admin_token()
    # Wait until the imported realm and its prerequisite clients are usable.
    # This avoids racing a fresh KeycloakRealmImport on the first install.
    acme_uuid = find_client(token, ACME_CLIENT_ID)["id"]
    # Keycloak's RFC 8693 audience parameter must name a real realm client.
    find_client(token, MCP_AUDIENCE_CLIENT_ID)
    lightspeed_uuid = ensure_client(token)
    mcp_audience_mapper = {
        "name": "openshift-mcp-audience",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.client.audience": MCP_AUDIENCE_CLIENT_ID,
            "access.token.claim": "true",
        },
    }
    groups_mapper = {
        "name": "groups",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-group-membership-mapper",
        "config": {
            "claim.name": "groups",
            "full.path": "false",
            "access.token.claim": "true",
            "id.token.claim": "true",
            "userinfo.token.claim": "true",
        },
    }
    ensure_mapper(token, lightspeed_uuid, mcp_audience_mapper)
    ensure_mapper(token, lightspeed_uuid, groups_mapper)

    ensure_mapper(
        token,
        acme_uuid,
        {
            "name": f"{LIGHTSPEED_INBOUND_AUDIENCE}-audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.custom.audience": LIGHTSPEED_INBOUND_AUDIENCE,
                "access.token.claim": "true",
            },
        },
    )
    ensure_mapper(
        token,
        acme_uuid,
        {
            "name": f"{LIGHTSPEED_CLIENT_ID}-audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.client.audience": LIGHTSPEED_CLIENT_ID,
                "access.token.claim": "true",
            },
        },
    )
    ensure_mapper(token, acme_uuid, groups_mapper)

    group_id = ensure_group(token)
    ensure_acme_group_membership(token, group_id)
    print("Lightspeed Keycloak client, audiences, groups mapper and ACME membership reconciled.")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, RuntimeError, urllib.error.URLError, ssl.SSLError) as error:
        print(f"Lightspeed Keycloak reconciliation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
