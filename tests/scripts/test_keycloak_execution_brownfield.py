"""Exercise the brownfield Keycloak script against an in-memory Admin API."""

import importlib.util
import json
import re
import ssl
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts/remediation/keycloak_execution_brownfield.py"
)
spec = importlib.util.spec_from_file_location("keycloak_execution_brownfield", SCRIPT)
kb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kb)

HUB, MAS, READ, SPOKE_HUB, SPOKE_MARIO, ACME = (
    "lightspeed-hub-a2a",
    "mas-a2a",
    "praxis-spoke-gateway",
    "praxis-spoke-hub",
    "praxis-spoke-mario-ipi",
    "acme-agent",
)


class FakeRealm:
    """Just enough of the Keycloak Admin API for the script."""

    def __init__(self):
        self.clients, self.roles, self.mappers, self.scopes = {}, {}, {}, {}
        self.groups, self.group_roles, self.user_groups = {}, {}, {}
        self.writes = []
        for client_id in (HUB, MAS, READ, SPOKE_HUB, SPOKE_MARIO, ACME):
            self.add_client(client_id)
        self.roles[self.cid(HUB)] = ["agenticrun-creator"]
        self.roles[self.cid(SPOKE_HUB)] = ["reader"]
        self.roles[self.cid(SPOKE_MARIO)] = ["reader"]
        self.mappers[self.cid(MAS)].append(kb.roles_mapper(HUB))
        self.mappers[self.cid(HUB)].append(kb.audience_mapper(READ))
        self.scopes[(self.cid(MAS), self.cid(HUB))] = ["agenticrun-creator"]
        for aud in (SPOKE_HUB, SPOKE_MARIO):
            self.mappers[self.cid(READ)].append(kb.roles_mapper(aud))
            self.scopes[(self.cid(READ), self.cid(aud))] = ["reader"]
        rca = self.add_group("acme-agent-rca")
        self.group_roles[(rca, self.cid(HUB))] = ["agenticrun-creator"]
        for aud in (SPOKE_HUB, SPOKE_MARIO):
            self.group_roles[(rca, self.cid(aud))] = ["reader"]
        self.sa = "sa-acme"
        self.user_groups[self.sa] = [rca]

    def add_client(self, client_id, **attrs):
        uid = str(uuid.uuid4())
        self.clients[uid] = {"id": uid, "clientId": client_id, **attrs}
        self.mappers[uid] = []
        return uid

    def add_group(self, name):
        gid = str(uuid.uuid4())
        self.groups[gid] = {"id": gid, "name": name}
        return gid

    def cid(self, client_id):
        return next(u for u, c in self.clients.items() if c["clientId"] == client_id)

    def role_rep(self, cuid, name):
        return {"id": f"{cuid}:{name}", "name": name, "clientRole": True}

    def example_claims(self, client_uuid):
        """Model the mapper settings Keycloak applies to an access token."""
        groups, aud = [], []
        for mapper in self.mappers[client_uuid]:
            config = mapper["config"]
            if config.get("access.token.claim") != "true":
                continue
            if mapper["protocolMapper"] == "oidc-audience-mapper":
                aud.append(config["included.custom.audience"])
                continue
            if config.get("claim.name") != "groups":
                continue
            target = config["usermodel.clientRoleMapping.clientId"]
            target_uuid = self.cid(target)
            granted = {
                r
                for g in self.user_groups[self.sa]
                for r in self.group_roles.get((g, target_uuid), [])
            }
            scoped = set(self.scopes.get((client_uuid, target_uuid), []))
            prefix = config.get("usermodel.clientRoleMapping.rolePrefix", "")
            groups += [f"{prefix}{r}" for r in sorted(granted & scoped)]
        return {"groups": groups, "aud": aud}

    def handle(self, method, path, query, body):
        p = path.removeprefix("/admin/realms/rca/")
        if path == "/realms/master/protocol/openid-connect/token":
            return {"access_token": "admin-token"}
        if method != "GET":
            self.writes.append((method, p))
        if p == "clients":
            if method == "GET":
                return [
                    c
                    for c in self.clients.values()
                    if c["clientId"] == query["clientId"][0]
                ]
            self.add_client(
                body["clientId"], **{k: v for k, v in body.items() if k != "clientId"}
            )
            return None
        if p == "groups":
            if method == "GET":
                return [
                    g for g in self.groups.values() if g["name"] == query["search"][0]
                ]
            self.add_group(body["name"])
            return None
        if m := re.fullmatch(r"clients/([^/]+)/protocol-mappers/models", p):
            if method == "GET":
                return self.mappers[m[1]]
            self.mappers[m[1]].append(body)
            return None
        if m := re.fullmatch(r"clients/([^/]+)/roles/([^/]+)", p):
            if m[2] not in self.roles.get(m[1], []):
                raise LookupError
            return self.role_rep(m[1], m[2])
        if m := re.fullmatch(r"clients/([^/]+)/roles", p):
            self.roles.setdefault(m[1], []).append(body["name"])
            return None
        for pattern, store in (
            (r"groups/([^/]+)/role-mappings/clients/([^/]+)", self.group_roles),
            (r"clients/([^/]+)/scope-mappings/clients/([^/]+)", self.scopes),
        ):
            if m := re.fullmatch(pattern, p):
                key = (m[1], m[2])
                if method == "GET":
                    return [self.role_rep(m[2], r) for r in store.get(key, [])]
                store.setdefault(key, []).extend(r["name"] for r in body)
                return None
        if re.fullmatch(r"clients/[^/]+/service-account-user", p):
            return {"id": self.sa}
        if m := re.fullmatch(r"users/([^/]+)/groups", p):
            return [self.groups[g] for g in self.user_groups[m[1]]]
        if m := re.fullmatch(r"users/([^/]+)/groups/([^/]+)", p):
            self.user_groups[m[1]].append(m[2])
            return None
        if m := re.fullmatch(
            r"clients/([^/]+)/evaluate-scopes/generate-example-access-token", p
        ):
            return self.example_claims(m[1])
        raise AssertionError(f"unexpected {method} {p}")


@pytest.fixture()
def realm():
    state = FakeRealm()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _serve(self):
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = (
                json.loads(raw)
                if raw and self.headers.get("Content-Type") == "application/json"
                else None
            )
            try:
                result = state.handle(self.command, url.path, parse_qs(url.query), body)
            except LookupError:
                self.send_response(404)
                self.end_headers()
                return
            payload = b"" if result is None else json.dumps(result).encode()
            self.send_response(200 if payload else 204)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = _serve

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()


def args(**overrides):
    values = dict(
        realm="rca",
        hub_audience=HUB,
        hub_gateway_exchanger=MAS,
        read_client=READ,
        exec_client="praxis-spoke-gateway-exec",
        spoke_audience=[SPOKE_HUB, SPOKE_MARIO],
        writer_audience=[SPOKE_MARIO],
        group="acme-remediation-mario-ipi",
        acme_client=ACME,
        reader_role="reader",
        writer_role="writer",
        creator_role="agenticrun-creator",
        approver_role="agenticrun-approver",
        grant_acme_membership=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def admin(realm, dry_run=False):
    return kb.Admin(
        realm.url, "rca", ssl.create_default_context(), "admin", "pw", dry_run
    )


def test_dry_run_changes_nothing(realm):
    kb.configure(admin(realm, dry_run=True), args(grant_acme_membership=True))
    assert realm.writes == []


def test_configures_scoped_execution_idempotently_and_verifies(realm, capsys):
    kb.configure(admin(realm), args())
    exec_uuid = realm.cid("praxis-spoke-gateway-exec")
    exec_client = realm.clients[exec_uuid]
    assert exec_client["serviceAccountsEnabled"] is False
    assert exec_client["fullScopeAllowed"] is False
    assert exec_client["attributes"] == {"standard.token.exchange.enabled": "true"}
    assert realm.scopes[(exec_uuid, realm.cid(SPOKE_MARIO))] == ["reader", "writer"]
    assert realm.scopes[(exec_uuid, realm.cid(SPOKE_HUB))] == ["reader"]
    assert "writer" not in realm.roles[realm.cid(SPOKE_HUB)]
    assert realm.scopes[(realm.cid(MAS), realm.cid(HUB))] == [
        "agenticrun-creator",
        "agenticrun-approver",
    ]
    audiences = {
        m["config"].get("included.custom.audience")
        for m in realm.mappers[realm.cid(HUB)]
    }
    assert "praxis-spoke-gateway-exec" in audiences

    # Without membership the shared principal must not verify yet.
    with pytest.raises(SystemExit, match="verification failed"):
        kb.verify(admin(realm), args())

    kb.configure(admin(realm), args(grant_acme_membership=True))
    kb.verify(admin(realm), args())
    out = capsys.readouterr().out
    assert (
        "ok   praxis-spoke-gateway-exec: groups="
        f"['{SPOKE_HUB}:reader', '{SPOKE_MARIO}:reader', '{SPOKE_MARIO}:writer']" in out
    )

    writes = len(realm.writes)
    kb.configure(admin(realm), args(grant_acme_membership=True))
    assert len(realm.writes) == writes  # second run is a no-op


def test_refuses_a_read_exchanger_that_scopes_writer(realm):
    realm.roles[realm.cid(SPOKE_MARIO)].append("writer")
    realm.scopes[(realm.cid(READ), realm.cid(SPOKE_MARIO))].append("writer")
    with pytest.raises(SystemExit, match="read exchanger"):
        kb.configure(admin(realm), args())


def test_refuses_a_drifted_execution_client_before_any_write(realm):
    realm.add_client(
        "praxis-spoke-gateway-exec",
        **{**kb.EXEC_CLIENT_SETTINGS, "serviceAccountsEnabled": True},
        attributes=dict(kb.EXEC_CLIENT_ATTRIBUTES),
    )
    with pytest.raises(SystemExit, match="serviceAccountsEnabled"):
        kb.configure(admin(realm), args(grant_acme_membership=True))
    assert realm.writes == []
    with pytest.raises(SystemExit, match="verification failed"):
        kb.verify(admin(realm), args())


def test_refuses_an_existing_mapper_with_wrong_settings(realm):
    exec_uuid = realm.add_client(
        "praxis-spoke-gateway-exec",
        **kb.EXEC_CLIENT_SETTINGS,
        attributes=dict(kb.EXEC_CLIENT_ATTRIBUTES),
    )
    broken = kb.audience_mapper(SPOKE_MARIO)
    broken["config"]["access.token.claim"] = "false"
    realm.mappers[exec_uuid].append(broken)
    with pytest.raises(SystemExit, match="different settings"):
        kb.configure(admin(realm), args())
