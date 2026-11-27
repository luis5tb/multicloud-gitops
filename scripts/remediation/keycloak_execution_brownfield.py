#!/usr/bin/env python3
"""Add the remediation execution path to an ALREADY-IMPORTED Keycloak realm.

BROWNFIELD ONLY. A fresh install gets all of this from the keycloak-oidc
realm import (variants/standalone/values-standalone.yaml). An existing realm
never re-imports, so this script creates the same objects through the Admin
REST API, idempotently (existing objects are left unchanged and reported):

  1. confidential exchange-only client <exec-client> with one audience and one
     client-role->groups mapper per spoke audience (secret: aligned later
     from Vault by the keycloak-oidc PostSync reconciler);
  2. the R_exec audience mapper on the hub exchanger client;
  3. roles: <approver> on the hub audience, <writer> on --writer-audience;
  4. group <group> with those roles, and scope mappings: execution exchanger
     reader (+writer on writer audiences), hub gateway exchanger approver;
  5. optional --grant-acme-membership: ACME's service account joins <group>
     (shared principal; run only after ACME token debug/override are off);
  6. --verify: print the groups Keycloak would issue to ACME's service
     account through each exchanger (example claims, never a usable token)
     and fail if they differ from the intended scoping.

Defaults mirror the standalone values. Admin credentials are read in memory
from the keycloak-admin-user Secret through `oc` (never printed). Stdlib only.
See docs/A2A_REMEDIATION_BROWNFIELD.md.
"""

import argparse
import base64
import json
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request

NEW = "<to-be-created>"
# The chart's contract for the execution exchanger: confidential and
# exchange-only, with role scope restricted to its spoke audiences.
EXEC_CLIENT_SETTINGS = {
    "enabled": True,
    "publicClient": False,
    "standardFlowEnabled": False,
    "implicitFlowEnabled": False,
    "directAccessGrantsEnabled": False,
    "serviceAccountsEnabled": False,
    "fullScopeAllowed": False,
}
EXEC_CLIENT_ATTRIBUTES = {"standard.token.exchange.enabled": "true"}


def exec_client_problems(client):
    problems = [
        f"{key}={client.get(key)!r} (expected {want!r})"
        for key, want in EXEC_CLIENT_SETTINGS.items()
        if client.get(key) != want
    ]
    attributes = client.get("attributes") or {}
    problems += [
        f"attributes.{key}={attributes.get(key)!r} (expected {want!r})"
        for key, want in EXEC_CLIENT_ATTRIBUTES.items()
        if attributes.get(key) != want
    ]
    return problems


def oc(*args):
    return subprocess.run(
        ["oc", *args], capture_output=True, text=True, check=True
    ).stdout


def secret_value(namespace, name, key):
    raw = oc("-n", namespace, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}")
    return base64.b64decode(raw).decode()


class Admin:
    def __init__(self, base, realm, context, username, password, dry_run):
        self.base, self.realm, self.ctx, self.dry_run = (
            base.rstrip("/"),
            realm,
            context,
            dry_run,
        )
        body = urllib.parse.urlencode(
            {
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": username,
                "password": password,
            }
        ).encode()
        token = self._call(
            "POST",
            f"{self.base}/realms/master/protocol/openid-connect/token",
            body,
            {"Content-Type": "application/x-www-form-urlencoded"},
            auth=False,
        )
        self.token = token["access_token"]

    def _call(self, method, url, data=None, headers=None, auth=True):
        headers = dict(headers or {})
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(
                request, context=self.ctx, timeout=30
            ) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as err:
            detail = err.read().decode()[:300]
            raise SystemExit(
                f"{method} {url.split('?')[0]} -> HTTP {err.code}: {detail}"
            )
        return json.loads(text) if text else None

    def get(self, path, **query):
        url = f"{self.base}/admin/realms/{self.realm}/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return self._call("GET", url)

    def write(self, method, path, payload, summary):
        print(("PLAN " if self.dry_run else "DO   ") + summary)
        if self.dry_run:
            return
        self._call(
            method,
            f"{self.base}/admin/realms/{self.realm}/{path}",
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )

    # --- lookups ---------------------------------------------------------
    def client(self, client_id, required=True):
        found = [
            c
            for c in self.get("clients", clientId=client_id)
            if c["clientId"] == client_id
        ]
        if not found and required:
            raise SystemExit(
                f"client {client_id!r} does not exist in realm {self.realm!r}"
            )
        return found[0] if found else None

    def role(self, client_uuid, name):
        try:
            return self.get(f"clients/{client_uuid}/roles/{urllib.parse.quote(name)}")
        except SystemExit as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def group(self, name):
        for group in self.get("groups", search=name, exact="true"):
            if group["name"] == name:
                return group
        return None


def ensure_mapper(admin, client, mapper):
    """Create a missing mapper; refuse an existing one with different config."""
    if client["id"] == NEW:
        print(f"PLAN add mapper {mapper['name']} on {client['clientId']}")
        return
    existing = admin.get(f"clients/{client['id']}/protocol-mappers/models")
    target_key = next(
        k
        for k in ("included.custom.audience", "usermodel.clientRoleMapping.clientId")
        if k in mapper["config"]
    )
    same_target = [
        m
        for m in existing
        if m.get("protocolMapper") == mapper["protocolMapper"]
        and m.get("config", {}).get(target_key) == mapper["config"][target_key]
    ]
    for m in same_target:
        if all(m.get("config", {}).get(k) == v for k, v in mapper["config"].items()):
            print(f"ok   mapper {mapper['name']} on {client['clientId']}")
            return
    if same_target:
        raise SystemExit(
            f"mapper for {mapper['config'][target_key]} on {client['clientId']} exists "
            f"with different settings (expected {mapper['config']}); fix it first"
        )
    admin.write(
        "POST",
        f"clients/{client['id']}/protocol-mappers/models",
        mapper,
        f"add mapper {mapper['name']} on {client['clientId']}",
    )


def ensure_role(admin, client, name):
    if admin.role(client["id"], name):
        print(f"ok   role {client['clientId']}:{name}")
        return
    admin.write(
        "POST",
        f"clients/{client['id']}/roles",
        {"name": name},
        f"create role {client['clientId']}:{name}",
    )


def ensure_mapping(admin, path, client, names, summary):
    present = {r["name"] for r in admin.get(path)}
    missing = [n for n in names if n not in present]
    if not missing:
        print(f"ok   {summary}: {sorted(present)}")
        return
    reps = [admin.role(client["id"], n) or {"name": n} for n in missing]
    admin.write("POST", path, reps, f"{summary}: add {missing}")


def audience_mapper(aud):
    return {
        "name": f"{aud}-audience",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {"included.custom.audience": aud, "access.token.claim": "true"},
    }


def roles_mapper(aud):
    return {
        "name": f"{aud}-roles-as-groups",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-usermodel-client-role-mapper",
        "config": {
            "usermodel.clientRoleMapping.clientId": aud,
            "usermodel.clientRoleMapping.rolePrefix": f"{aud}:",
            "claim.name": "groups",
            "multivalued": "true",
            "jsonType.label": "String",
            "access.token.claim": "true",
            "id.token.claim": "false",
            "userinfo.token.claim": "false",
        },
    }


def configure(admin, a):
    hub_aud = admin.client(a.hub_audience)
    hub_gateway = admin.client(a.hub_gateway_exchanger)
    read = admin.client(a.read_client)
    spokes = {aud: admin.client(aud) for aud in a.spoke_audience}

    execution = admin.client(a.exec_client, required=False)
    if execution:
        problems = exec_client_problems(execution)
        if problems:
            raise SystemExit(
                f"existing client {a.exec_client} violates the exchange-only contract: "
                + "; ".join(problems)
                + ". Fix it before granting any execution authorization."
            )
        print(f"ok   client {a.exec_client} settings")
    else:
        admin.write(
            "POST",
            "clients",
            {
                "clientId": a.exec_client,
                "name": "Praxis spoke execution-gateway token exchanger",
                "protocol": "openid-connect",
                **EXEC_CLIENT_SETTINGS,
                "attributes": dict(EXEC_CLIENT_ATTRIBUTES),
            },
            f"create exchange-only client {a.exec_client}",
        )
        execution = admin.client(a.exec_client, required=not admin.dry_run) or {
            "id": NEW,
            "clientId": a.exec_client,
        }

    ensure_mapper(admin, hub_aud, audience_mapper(a.exec_client))
    for aud in a.spoke_audience:
        ensure_mapper(admin, execution, audience_mapper(aud))
        ensure_mapper(admin, execution, roles_mapper(aud))

    ensure_role(admin, hub_aud, a.approver_role)
    for aud in a.writer_audience:
        ensure_role(admin, spokes[aud], a.writer_role)

    group = admin.group(a.group)
    if not group:
        admin.write("POST", "groups", {"name": a.group}, f"create group {a.group}")
        group = admin.group(a.group) if not admin.dry_run else None
    if group:
        ensure_mapping(
            admin,
            f"groups/{group['id']}/role-mappings/clients/{hub_aud['id']}",
            hub_aud,
            [a.approver_role],
            f"group {a.group} -> {a.hub_audience}",
        )
        for aud in a.writer_audience:
            ensure_mapping(
                admin,
                f"groups/{group['id']}/role-mappings/clients/{spokes[aud]['id']}",
                spokes[aud],
                [a.writer_role],
                f"group {a.group} -> {aud}",
            )
    else:
        print(
            f"PLAN map {a.approver_role} and {a.writer_role} ({a.writer_audience}) to {a.group}"
        )

    if execution["id"] != NEW:
        for aud in a.spoke_audience:
            names = [a.reader_role] + (
                [a.writer_role] if aud in a.writer_audience else []
            )
            ensure_mapping(
                admin,
                f"clients/{execution['id']}/scope-mappings/clients/{spokes[aud]['id']}",
                spokes[aud],
                names,
                f"scope {a.exec_client} -> {aud}",
            )
    else:
        print(
            f"PLAN scope {a.exec_client}: {a.reader_role} on {a.spoke_audience}, "
            f"+{a.writer_role} on {a.writer_audience}"
        )
    ensure_mapping(
        admin,
        f"clients/{hub_gateway['id']}/scope-mappings/clients/{hub_aud['id']}",
        hub_aud,
        [a.approver_role],
        f"scope {a.hub_gateway_exchanger} -> {a.hub_audience}",
    )

    for aud, client in spokes.items():
        scoped = {
            r["name"]
            for r in admin.get(
                f"clients/{read['id']}/scope-mappings/clients/{client['id']}"
            )
        }
        if a.writer_role in scoped:
            raise SystemExit(
                f"read exchanger {a.read_client} scopes {aud}:{a.writer_role}; fix before enabling"
            )

    if a.grant_acme_membership:
        sa = admin.get(
            f"clients/{admin.client(a.acme_client)['id']}/service-account-user"
        )
        member = any(
            g["name"] == a.group for g in admin.get(f"users/{sa['id']}/groups")
        )
        if member:
            print(f"ok   service account of {a.acme_client} is in {a.group}")
        elif group:
            admin.write(
                "PUT",
                f"users/{sa['id']}/groups/{group['id']}",
                {},
                f"add service account of {a.acme_client} to {a.group}",
            )


def verify(admin, a):
    """Check client contract, issued groups and audiences (example claims only)."""
    problems = exec_client_problems(admin.client(a.exec_client))
    sa = admin.get(f"clients/{admin.client(a.acme_client)['id']}/service-account-user")

    def claims(client_id):
        return admin.get(
            f"clients/{admin.client(client_id)['id']}/evaluate-scopes/"
            "generate-example-access-token",
            userId=sa["id"],
            scope="openid",
        )

    def audiences(claim_set):
        aud = claim_set.get("aud") or []
        return {aud} if isinstance(aud, str) else set(aud)

    expected_groups = {
        a.exec_client: sorted(
            [f"{aud}:{a.reader_role}" for aud in a.spoke_audience]
            + [f"{aud}:{a.writer_role}" for aud in a.writer_audience]
        ),
        a.read_client: sorted(f"{aud}:{a.reader_role}" for aud in a.spoke_audience),
        a.hub_gateway_exchanger: sorted(
            [
                f"{a.hub_audience}:{a.creator_role}",
                f"{a.hub_audience}:{a.approver_role}",
            ]
        ),
    }
    expected_audiences = {
        a.exec_client: set(a.spoke_audience),
        a.hub_audience: {a.exec_client},
    }
    if problems:
        print(f"FAIL {a.exec_client}: " + "; ".join(problems))
    failed = bool(problems)
    for client_id in dict.fromkeys([*expected_groups, *expected_audiences]):
        issued = claims(client_id)
        lines = []
        if client_id in expected_groups:
            got, want = sorted(issued.get("groups") or []), expected_groups[client_id]
            failed |= got != want
            lines.append(f"groups={got}" + ("" if got == want else f" expected={want}"))
        if client_id in expected_audiences:
            missing = expected_audiences[client_id] - audiences(issued)
            failed |= bool(missing)
            lines.append(f"aud missing={sorted(missing)}" if missing else "aud ok")
        bad = any("expected=" in line or "missing=" in line for line in lines)
        print(("FAIL " if bad else "ok   ") + f"{client_id}: " + ", ".join(lines))
    if failed:
        raise SystemExit(
            "verification failed: do not enable or test the execution path"
        )


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--keycloak-url",
        required=True,
        help="Keycloak base URL, e.g. https://keycloak.apps.<base>",
    )
    p.add_argument("--namespace", default="keycloak-system")
    p.add_argument("--admin-secret", default="keycloak-admin-user")
    p.add_argument(
        "--ca-bundle",
        help="PEM file; default: the hub's managed ingress CA plus system roots",
    )
    p.add_argument("--realm", default="rca")
    p.add_argument("--hub-audience", default="lightspeed-hub-a2a")
    p.add_argument("--hub-gateway-exchanger", default="mas-a2a")
    p.add_argument("--read-client", default="praxis-spoke-gateway")
    p.add_argument("--exec-client", default="praxis-spoke-gateway-exec")
    p.add_argument("--spoke-audience", action="append")
    p.add_argument("--writer-audience", action="append")
    p.add_argument("--group", default="acme-remediation-mario-ipi")
    p.add_argument("--acme-client", default="acme-agent")
    p.add_argument("--reader-role", default="reader")
    p.add_argument("--writer-role", default="writer")
    p.add_argument("--creator-role", default="agenticrun-creator")
    p.add_argument("--approver-role", default="agenticrun-approver")
    p.add_argument("--grant-acme-membership", action="store_true")
    p.add_argument("--verify", action="store_true", help="only check the issued groups")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    a.spoke_audience = a.spoke_audience or [
        "praxis-spoke-hub",
        "praxis-spoke-mario-ipi",
    ]
    a.writer_audience = a.writer_audience or ["praxis-spoke-mario-ipi"]
    if not set(a.writer_audience) <= set(a.spoke_audience):
        p.error("--writer-audience must be one of --spoke-audience")

    url = a.keycloak_url
    ctx = ssl.create_default_context()
    if a.ca_bundle:
        ctx.load_verify_locations(cafile=a.ca_bundle)
    else:
        ingress_ca = oc(
            "-n",
            "openshift-config-managed",
            "get",
            "configmap",
            "default-ingress-cert",
            "-o",
            "jsonpath={.data.ca-bundle\\.crt}",
        )
        if ingress_ca.strip():
            ctx.load_verify_locations(cadata=ingress_ca)
    admin = Admin(
        url,
        a.realm,
        ctx,
        secret_value(a.namespace, a.admin_secret, "username"),
        secret_value(a.namespace, a.admin_secret, "password"),
        a.dry_run,
    )
    if a.verify:
        verify(admin, a)
    else:
        configure(admin, a)
        if a.dry_run:
            print("dry run: no changes made")


if __name__ == "__main__":
    main()
