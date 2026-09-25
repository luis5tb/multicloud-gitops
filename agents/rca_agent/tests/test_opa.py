import httpx
import pytest

from rca_agent.opa import OpaAuthorizationError, OpaAuthorizer


def _patch_transport(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)


def test_authorize_allows_when_opa_returns_true(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/data/rca/authorization/allow"
        assert request.method == "POST"
        assert request.read()  # body was sent
        return httpx.Response(200, json={"result": True})

    _patch_transport(monkeypatch, handler)
    authorizer = OpaAuthorizer(url="http://opa.example.com:8181")

    authorizer.authorize({"azp": "ericsson-agent"})


def test_authorize_denies_when_opa_returns_false(monkeypatch) -> None:
    _patch_transport(monkeypatch, lambda request: httpx.Response(200, json={"result": False}))
    authorizer = OpaAuthorizer(url="http://opa.example.com:8181")

    with pytest.raises(OpaAuthorizationError):
        authorizer.authorize({"azp": "company-b-agent"})


def test_authorize_denies_when_result_missing(monkeypatch) -> None:
    _patch_transport(monkeypatch, lambda request: httpx.Response(200, json={}))
    authorizer = OpaAuthorizer(url="http://opa.example.com:8181")

    with pytest.raises(OpaAuthorizationError):
        authorizer.authorize({"azp": "ericsson-agent"})


def test_authorize_fails_closed_on_transport_error(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_transport(monkeypatch, handler)
    authorizer = OpaAuthorizer(url="http://opa.example.com:8181")

    with pytest.raises(OpaAuthorizationError):
        authorizer.authorize({"azp": "ericsson-agent"})


def test_authorize_fails_closed_on_http_error(monkeypatch) -> None:
    _patch_transport(monkeypatch, lambda request: httpx.Response(500))
    authorizer = OpaAuthorizer(url="http://opa.example.com:8181")

    with pytest.raises(OpaAuthorizationError):
        authorizer.authorize({"azp": "ericsson-agent"})


def test_authorize_denies_when_azp_claim_missing(monkeypatch) -> None:
    import json as jsonlib

    def handler(request: httpx.Request) -> httpx.Response:
        assert jsonlib.loads(request.read())["input"]["azp"] is None
        return httpx.Response(200, json={"result": False})

    _patch_transport(monkeypatch, handler)
    authorizer = OpaAuthorizer(url="http://opa.example.com:8181")

    with pytest.raises(OpaAuthorizationError):
        authorizer.authorize({"sub": "some-caller-with-no-azp"})


def test_check_available_raises_when_opa_unreachable(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_transport(monkeypatch, handler)
    authorizer = OpaAuthorizer(url="http://opa.example.com:8181")

    with pytest.raises(OpaAuthorizationError):
        authorizer.check_available()


def test_check_available_succeeds_when_opa_healthy(monkeypatch) -> None:
    _patch_transport(monkeypatch, lambda request: httpx.Response(200))
    authorizer = OpaAuthorizer(url="http://opa.example.com:8181")

    authorizer.check_available()


def test_requires_url() -> None:
    with pytest.raises(ValueError):
        OpaAuthorizer(url="")
