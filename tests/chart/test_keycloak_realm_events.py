"""keycloak-oidc realm events: rendered as top-level RealmRepresentation fields.

The KeycloakRealmImport CRD prunes unknown realm keys. An earlier
`eventsConfig:` block was dropped by the API server, which left the ArgoCD
app permanently OutOfSync and a fresh realm without events (live 2026-11-27).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "charts/all/keycloak-oidc"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is required to render chart"
)


def _realm(*args: str) -> dict:
    out = subprocess.run(
        ["helm", "template", "keycloak-oidc", str(CHART),
         "--set", "keycloak.spiffeIdentityProvider.enabled=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout
    doc = next(d for d in yaml.safe_load_all(out) if d and d["kind"] == "KeycloakRealmImport")
    return doc["spec"]["realm"]


def test_events_are_top_level_realm_fields() -> None:
    realm = _realm()
    assert realm["eventsEnabled"] is True
    assert realm["eventsListeners"] == ["jboss-logging"]
    assert "eventsConfig" not in realm


def test_events_off_renders_nothing() -> None:
    realm = _realm("--set", "keycloak.realmEvents.enabled=false")
    assert "eventsEnabled" not in realm
    assert "eventsListeners" not in realm
