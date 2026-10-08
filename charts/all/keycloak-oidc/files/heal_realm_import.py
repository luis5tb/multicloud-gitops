"""Self-heal a stuck KeycloakRealmImport.

A KeycloakRealmImport gets exactly one shot: on a fresh install it can race
the Keycloak CR's own creation (the controller GETs the Keycloak CR, gets a
404, and records HasErrors) and then sits in HasErrors forever -- it does
not retry on its own, even once the Keycloak CR exists and is Ready
(observed live; see AGENTS.md "Hard-won lessons" and
keycloak-realm-import.yaml).

The documented manual recovery is `oc delete keycloakrealmimport <realm>`,
which lets ArgoCD's self-heal recreate it against the now-Ready Keycloak CR.
This script automates exactly that: once the Keycloak CR is Ready, if the
realm import is in HasErrors it deletes the import and exits, leaving ArgoCD
to recreate it. Run periodically (a CronJob) so it also heals any later
recurrence (e.g. an operator reconcile that re-breaks it).

Talks to the in-cluster API server only. Its certificate always chains to
the pod's own projected service-account CA, so an exclusive cafile= here is
correct (see AGENTS.md "TLS trust: extend the default store, don't
replace it" -- this is the one case where replacing it is right).
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"
GROUP = "k8s.keycloak.org"
VERSION = "v2alpha1"


def condition(obj, cond_type):
    """Return the status string ("True"/"False"/...) of a status condition."""
    for cond in (obj.get("status", {}) or {}).get("conditions", []) or []:
        if cond.get("type") == cond_type:
            return cond.get("status")
    return None


def main():
    namespace = os.environ["KEYCLOAK_NAMESPACE"]
    keycloak_name = os.environ["KEYCLOAK_NAME"]
    realm = os.environ["REALM_IMPORT_NAME"]

    with open(os.path.join(SA_DIR, "token"), encoding="utf-8") as handle:
        token = handle.read().strip()
    tls_context = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))

    def call(method, path):
        request = urllib.request.Request(
            API + path,
            headers={"Authorization": "Bearer " + token},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=tls_context) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            return exc.code, None

    base = f"/apis/{GROUP}/{VERSION}/namespaces/{namespace}"

    status, keycloak = call("GET", f"{base}/keycloaks/{keycloak_name}")
    if status == 404 or keycloak is None:
        print(f"Keycloak CR {keycloak_name} not found yet; nothing to do.")
        return
    if condition(keycloak, "Ready") != "True":
        print(f"Keycloak CR {keycloak_name} is not Ready yet; leaving the "
              "realm import alone until it is.")
        return

    status, realm_import = call("GET", f"{base}/keycloakrealmimports/{realm}")
    if status == 404 or realm_import is None:
        print(f"KeycloakRealmImport {realm} not found; ArgoCD will create it.")
        return

    if condition(realm_import, "HasErrors") != "True":
        done = condition(realm_import, "Done")
        print(f"KeycloakRealmImport {realm} is healthy (HasErrors!=True, "
              f"Done={done}); nothing to do.")
        return

    # HasErrors is terminal (the controller never retries), and the Keycloak
    # CR is Ready -- so a recreate will now succeed. Delete and let ArgoCD
    # self-heal recreate it. Pin the uid so we only ever delete the exact
    # errored object we just inspected, never a fresh replacement.
    uid = realm_import.get("metadata", {}).get("uid")
    print(f"KeycloakRealmImport {realm} is stuck in HasErrors while Keycloak "
          f"is Ready; deleting it (uid={uid}) so ArgoCD recreates it.")
    delete_options = {"apiVersion": "v1", "kind": "DeleteOptions"}
    if uid:
        delete_options["preconditions"] = {"uid": uid}
    body = json.dumps(delete_options).encode()
    request = urllib.request.Request(
        API + f"{base}/keycloakrealmimports/{realm}",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=tls_context):
            print("Deleted. ArgoCD self-heal will recreate the realm import.")
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 409):
            print(f"Delete raced another actor (HTTP {exc.code}); it is "
                  "already gone or replaced. Nothing to do.")
            return
        sys.exit(f"Failed to delete KeycloakRealmImport {realm}: HTTP {exc.code}")


if __name__ == "__main__":
    main()
