"""Align existing exchanger credentials with Vault-backed Kubernetes Secrets."""

import hmac
import urllib.parse


def reconcile(call, realm, desired_clients, environ):
    """Repair secrets only; never create clients or regenerate credentials."""
    credentials = []
    for desired in desired_clients:
        value = environ.get(desired["secretEnv"], "")
        if not value:
            raise ValueError(f"Missing credential {desired['secretEnv']}")
        credentials.append((desired["clientId"], value))

    # Resolve every client before writing, so a missing client cannot leave
    # earlier ones partially repaired. The one-shot import owns client creation.
    base = f"/admin/realms/{urllib.parse.quote(realm, safe='')}/clients"
    repairs = []
    for client_id, value in credentials:
        query = urllib.parse.urlencode({"clientId": client_id})
        clients = [
            client
            for client in call("GET", base + "?" + query)
            if client.get("clientId") == client_id
        ]
        if len(clients) != 1:
            raise ValueError(f"Expected one existing exchanger client: {client_id}")
        path = base + "/" + urllib.parse.quote(clients[0]["id"], safe="")
        current = call("GET", path + "/client-secret").get("value", "")
        matches = hmac.compare_digest(current.encode(), value.encode())
        repairs.append((client_id, path, value, matches))

    for client_id, path, value, matches in repairs:
        if not matches:
            # A partial ClientRepresentation changes only the managed secret,
            # preserving all client settings, mappers, roles and permissions.
            call("PUT", path, data={"secret": value})
            current = call("GET", path + "/client-secret").get("value", "")
            if not hmac.compare_digest(current.encode(), value.encode()):
                raise ValueError(f"Credential verification failed for {client_id}")
            print(f"Exchanger {client_id}: credential reconciled and verified.")
        else:
            print(f"Exchanger {client_id}: credential already matches.")
