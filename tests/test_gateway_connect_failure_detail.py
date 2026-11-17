"""Synthetic, finite connect diagnostics on the retained Gateway session."""

import pytest
from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.account_provider import CtpAccountSessionProviderError, CtpReadOnlyAccountSessionProvider
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
from test_account_provider import _ACCOUNT_KEY, _Feed, _provider

pytest_plugins = ("test_iter63_gateway_session",)


@pytest.mark.parametrize("fault,expected", [
    ("factory", "FEED_FACTORY_FAILED"),
    ("preflight", "FEED_PREFLIGHT_FAILED"),
    ("native_start", "NATIVE_START_FAILED"),
    ("wait_exception", "NATIVE_WAIT_FAILED"),
    ("wait_missing", "NATIVE_WAIT_FAILED"),
    ("wait_false", "READINESS_FALSE"),
    ("scope", "SCOPE_UNTRUSTED"),
    ("binding", "SESSION_BINDING_CHANGED"),
])
def test_connect_detail_is_from_exact_observation(prepared, monkeypatch, fault, expected):
    create, _, _, feeds, natives, _, _, _ = prepared
    if fault == "factory":
        def fail_factory():
            raise ValueError("secret /private/path")
        session = create(feed=fail_factory)
    else:
        session = create()
    if fault == "preflight":
        def fail_preflight(_self):
            raise ValueError("secret /private/path")
        monkeypatch.setattr(CtpRequestDataFuture, "_ensure_read_only_connection_safe", fail_preflight)
    elif fault == "native_start":
        def fail_start(_self, **_kwargs):
            raise ValueError("secret /private/path")
        monkeypatch.setattr(TraderClient, "start", fail_start)
    elif fault.startswith("wait_"):
        monkeypatch.setattr(TraderClient, "start", lambda *_args, **_kwargs: None)
        if fault == "wait_missing":
            monkeypatch.setattr(TraderClient, "wait_ready", None)
        elif fault == "wait_exception":
            def fail_wait(*_args, **_kwargs):
                raise ValueError("secret /private/path")
            monkeypatch.setattr(TraderClient, "wait_ready", fail_wait)
        else:
            monkeypatch.setattr(TraderClient, "wait_ready", lambda *_args, **_kwargs: False)
    elif fault == "scope":
        monkeypatch.setattr(CtpRequestDataFuture, "get_query_session_scope", lambda _self: None)
    elif fault == "binding":
        # The binding check reads this before comparing feed identity.
        class Borrowed:
            trader_client = object()
        monkeypatch.setattr(session._reader, "borrow_request_feed", lambda: Borrowed())

    with pytest.raises(Exception):
        session.connect(generation="g1", timeout=1)
    assert session._gateway_connect_failure_detail == expected
    assert "secret" not in session._gateway_connect_failure_detail
    if fault in {"native_start", "wait_exception", "wait_missing", "wait_false"}:
        retained = feeds[0]._trader
        assert retained is not None and session._reader._request_feed is feeds[0]
        # A missing stop receipt retains exactly the same native owner.
        original_stop = TraderClient.stop_startup_and_wait
        monkeypatch.setattr(TraderClient, "stop_startup_and_wait", lambda *_args, **_kwargs: None)
        assert session.close() is False
        assert feeds[0]._trader is retained and session._reader._request_feed is feeds[0]
        monkeypatch.setattr(TraderClient, "stop_startup_and_wait", original_stop)
    if fault == "scope":
        natives[0].Release = lambda: None
    assert session.close() is True


def test_unknown_exception_does_not_turn_into_a_claimed_category(prepared, monkeypatch):
    create, _, _, _, _, _, _, _ = prepared
    session = create()
    monkeypatch.setattr(TraderClient, "__init__", lambda *_a, **_k: (_ for _ in ()).throw(
        ValueError("secret /private/path")))
    with pytest.raises(Exception):
        session.connect(generation="g1", timeout=1)
    assert session._gateway_connect_failure_detail == "UNKNOWN"
    assert session.close() is True


@pytest.mark.parametrize("failure", ["state_shape", "settlement", "scope_exception"])
def test_unproven_readiness_and_scope_failures_remain_unknown(monkeypatch, failure):
    feed = _Feed()
    if failure == "state_shape":
        monkeypatch.setattr(feed, "get_session_state", lambda: None)
    elif failure == "settlement":
        monkeypatch.setattr(feed, "get_session_state", lambda: {
            "read_only_ready": True, "auto_settlement_confirm": True,
        })
    else:
        def scope_exception():
            raise ValueError("secret /private/path")
        monkeypatch.setattr(feed, "get_query_session_scope", scope_exception)
    provider = _provider(feed)
    with pytest.raises(Exception):
        provider.connect(account_key=_ACCOUNT_KEY, generation="g1", timeout=1)
    assert provider._gateway_connect_failure_detail == "UNKNOWN"
    assert provider.close() is False
    assert provider.close() is True


@pytest.mark.parametrize("detail", ["raises", "secret /private/path", object()])
def test_diagnostic_reader_does_not_replace_original_exception(prepared, monkeypatch, detail):
    create, _, _, _, _, _, _, _ = prepared
    session = create()
    original_error = CtpAccountSessionProviderError("SESSION_NOT_CONNECTED")
    object.__setattr__(session._reader, "_gateway_connect_failure_detail", detail)

    def fail_connect(**_kwargs):
        raise original_error

    monkeypatch.setattr(session._reader, "connect", fail_connect)
    if detail == "raises":
        original_getattribute = CtpReadOnlyAccountSessionProvider.__getattribute__

        def getattribute(reader, name):
            if name == "_gateway_connect_failure_detail":
                raise RuntimeError("secret /private/path")
            return original_getattribute(reader, name)

        monkeypatch.setattr(CtpReadOnlyAccountSessionProvider, "__getattribute__", getattribute)
    with pytest.raises(CtpAccountSessionProviderError) as caught:
        session.connect(generation="g1", timeout=1)
    assert caught.value is original_error
    assert session._state == "failed" and session._gateway_connect_failure_detail == "UNKNOWN"
    assert session.close() is True
