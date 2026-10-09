"""Unit tests for OLS ols_mode stamping on outbound A2A messages."""

from __future__ import annotations

import asyncio

import pytest
from a2a.types import Message as A2AMessage
from google.adk.a2a.agent.config import ParametersConfig
from google.protobuf.struct_pb2 import Struct

from acme_agent.ols_mode import (
    DEFAULT_OLS_A2A_MODE,
    OLS_MODE_METADATA_KEY,
    configured_ols_mode,
    inject_ols_mode,
)


def _message(*, ols_mode: str | None = None) -> A2AMessage:
    message = A2AMessage()
    if ols_mode is not None:
        meta = Struct()
        meta.update({OLS_MODE_METADATA_KEY: ols_mode})
        message.metadata.CopyFrom(meta)
    return message


def test_injects_default_troubleshooting_mode(monkeypatch):
    monkeypatch.delenv("OLS_A2A_MODE", raising=False)
    message = _message()

    out, _params = asyncio.run(inject_ols_mode(None, message, ParametersConfig()))

    assert out.metadata[OLS_MODE_METADATA_KEY] == DEFAULT_OLS_A2A_MODE
    assert configured_ols_mode() == "troubleshooting"


def test_respects_ols_a2a_mode_env(monkeypatch):
    monkeypatch.setenv("OLS_A2A_MODE", "ask")
    message = _message()

    out, _params = asyncio.run(inject_ols_mode(None, message, ParametersConfig()))

    assert out.metadata[OLS_MODE_METADATA_KEY] == "ask"


def test_does_not_overwrite_existing_mode(monkeypatch):
    monkeypatch.setenv("OLS_A2A_MODE", "troubleshooting")
    message = _message(ols_mode="ask")

    out, _params = asyncio.run(inject_ols_mode(None, message, ParametersConfig()))

    assert out.metadata[OLS_MODE_METADATA_KEY] == "ask"


def test_configured_ols_mode_rejects_unknown(monkeypatch):
    monkeypatch.setenv("OLS_A2A_MODE", "debug")
    with pytest.raises(ValueError, match="OLS_A2A_MODE"):
        configured_ols_mode()
