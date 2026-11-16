"""Owner-local calendar binding; all CTP callbacks and accounts are synthetic."""
from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_iter62_settlement_readback_on_write import TODAY

from bt_api_ctp import gateway_session as gateway_module
from bt_api_ctp.gateway_session import CtpGatewaySessionError
from bt_api_ctp.registered_sim_session import CtpQuerySessionBinding

pytest_plugins = ("test_iter63_gateway_session",)


def _open(prepared):
    create, _, _, feeds, natives, _, _, _ = prepared
    session = create()
    session.connect(generation="calendar-session", timeout=1)
    return session, feeds[0], feeds[0].trader_client, natives[0]


def _reconnect(trader, native, monkeypatch, trading_day):
    spi = trader._spi
    monkeypatch.setattr(native, "ReqUserLogin", lambda *_: 0, raising=False)
    spi.OnFrontDisconnected(1)
    spi.OnFrontConnected()
    spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=0, ErrorMsg=""),
                          trader._authentication_request_id, True)
    spi.OnRspUserLogin(
        SimpleNamespace(BrokerID="9999", UserID="account", TradingDay=trading_day,
                        FrontID=7, SessionID=20, MaxOrderRef="0"),
        SimpleNamespace(ErrorID=0, ErrorMsg=""), trader._login_request_id, True,
    )


def test_binding_same_day_reconnect_next_day_and_no_native_or_disk_io(prepared, monkeypatch):
    session, feed, trader, native = _open(prepared)
    first = session.capture_calendar_session()
    assert type(first) is CtpQuerySessionBinding
    assert first.trading_day == TODAY
    assert session.calendar_session_is_current(first)
    assert native.calls == []
    assert not trader.is_trading_ready
    with monkeypatch.context() as patch:
        def forbidden(*_args, **_kwargs):
            pytest.fail("calendar read performed native, connection or filesystem work")
        for flow in (session._flow, feed._trusted_flow, trader._trusted_flow):
            patch.setattr(flow, "verify_root", forbidden)
            patch.setattr(flow, "_capture_root", forbidden)
        patch.setattr(session._reader, "borrow_request_feed", forbidden)
        patch.setattr(feed, "_ensure_connected", forbidden)
        patch.setattr(gateway_module.os, "stat", forbidden)
        patch.setattr(gateway_module.os, "open", forbidden)
        patch.setattr("builtins.open", forbidden)
        patch.setattr(native, "ReqAuthenticate", forbidden)
        patch.setattr(native, "ReqUserLogin", forbidden, raising=False)
        patch.setattr(native, "ReqQrySettlementInfoConfirm", forbidden)
        patch.setattr(native, "ReqSettlementInfoConfirm", forbidden)
        patch.setattr(native, "ReqOrderInsert", forbidden)
        patch.setattr(native, "ReqOrderAction", forbidden)
        again = session.capture_calendar_session()
        assert session.calendar_session_is_current(again)
        assert native.calls == []
    _reconnect(trader, native, monkeypatch, TODAY)
    assert not session.calendar_session_is_current(first)
    same_day = session.capture_calendar_session()
    assert same_day.trading_day == TODAY
    assert same_day.connection_generation > first.connection_generation
    assert session.calendar_session_is_current(same_day)
    _reconnect(trader, native, monkeypatch, "20261109")
    assert not session.calendar_session_is_current(same_day)
    new_day = session.capture_calendar_session()
    assert new_day.trading_day == "20261109"
    assert session.calendar_session_is_current(new_day)
    assert native.calls == []
    assert session.close()
    assert not session.calendar_session_is_current(new_day)
    with pytest.raises(CtpGatewaySessionError, match="^CALENDAR_SESSION_UNAVAILABLE$"):
        session.capture_calendar_session()


def test_busy_does_not_wait_and_wrong_owner_still_raises(prepared):
    session, _, trader, native = _open(prepared)
    binding = session.capture_calendar_session()
    locked, release = threading.Event(), threading.Event()
    def hold():
        with trader._query_state_lock:
            locked.set()
            assert release.wait(3)
    thread = threading.Thread(target=hold)
    thread.start()
    assert locked.wait(1)
    try:
        with pytest.raises(CtpGatewaySessionError, match="^CALENDAR_SESSION_BUSY$"):
            session.capture_calendar_session()
        assert not session.calendar_session_is_current(binding)
        errors = []
        def wrong_owner():
            for callback in (session.capture_calendar_session,
                             lambda: session.calendar_session_is_current(binding)):
                try:
                    callback()
                except CtpGatewaySessionError as exc:
                    errors.append(exc.code)
        other = threading.Thread(target=wrong_owner)
        other.start()
        other.join(1)
        assert not other.is_alive() and errors == ["WRONG_OWNER", "WRONG_OWNER"]
        assert native.calls == []
    finally:
        release.set()
        thread.join(1)
    original_pid = session._owner_pid
    session._owner_pid += 1
    try:
        with pytest.raises(CtpGatewaySessionError, match="^WRONG_OWNER$"):
            session.capture_calendar_session()
        with pytest.raises(CtpGatewaySessionError, match="^WRONG_OWNER$"):
            session.calendar_session_is_current(binding)
    finally:
        session._owner_pid = original_pid
    assert session.calendar_session_is_current(binding)
    assert session.close()


@pytest.mark.parametrize("change", ["account", "feed", "api", "issuer", "source", "flow"])
def test_identity_or_source_replacement_rejects_without_io(prepared, monkeypatch, change):
    session, feed, trader, native = _open(prepared)
    binding = session.capture_calendar_session()
    with monkeypatch.context() as patch:
        if change == "account":
            patch.setattr(session, "_account_key", replace(session._account_key, account_id="other"))
        elif change == "feed":
            patch.setattr(feed, "_execution_bound_broker_id", "other")
        elif change == "api":
            patch.setattr(trader, "_api", object())
        elif change == "issuer":
            patch.setattr(trader, "_query_evidence_issuer", object())
        elif change == "source":
            patch.setattr(trader._spi, "_native_spi_source_id", "other")
        else:
            patch.setattr(trader, "_trusted_flow", object())
        assert not session.calendar_session_is_current(binding)
        with pytest.raises(CtpGatewaySessionError, match="^CALENDAR_SESSION_UNAVAILABLE$"):
            session.capture_calendar_session()
        assert native.calls == []
    assert session.close()


def test_rejects_foreign_forged_and_scope_changed_during_capture(prepared, monkeypatch):
    session, _, trader, native = _open(prepared)
    binding = session.capture_calendar_session()
    assert not session.calendar_session_is_current(object())
    assert not session.calendar_session_is_current(replace(binding, trading_day="20261109"))
    other, _, _, _ = _open(prepared)
    assert not other.calendar_session_is_current(binding)
    assert other.close()
    original = gateway_module.capture_query_session
    def capture_then_disconnect(*args, **kwargs):
        result = original(*args, **kwargs)
        trader._spi.OnFrontDisconnected(1)
        return result
    monkeypatch.setattr(gateway_module, "capture_query_session", capture_then_disconnect)
    with pytest.raises(CtpGatewaySessionError, match="^CALENDAR_SESSION_UNAVAILABLE$"):
        session.capture_calendar_session()
    assert not session.calendar_session_is_current(binding)
    assert native.calls == []
    assert session.close()


def test_native_disconnect_waits_for_capture_and_then_invalidates(prepared, monkeypatch):
    session, _, trader, native = _open(prepared)
    original = gateway_module.capture_query_session
    attempted, completed = threading.Event(), threading.Event()
    workers = []

    def capture_and_start_callback(*args, **kwargs):
        binding = original(*args, **kwargs)
        if not workers:
            def disconnect():
                attempted.set()
                trader._spi.OnFrontDisconnected(1)
                completed.set()
            worker = threading.Thread(target=disconnect)
            workers.append(worker)
            worker.start()
            assert attempted.wait(1)
            assert not completed.wait(0.2)
        return binding

    monkeypatch.setattr(gateway_module, "capture_query_session", capture_and_start_callback)
    binding = session.capture_calendar_session()
    workers[0].join(1)
    assert completed.is_set()
    assert not session.calendar_session_is_current(binding)
    with pytest.raises(CtpGatewaySessionError, match="^CALENDAR_SESSION_UNAVAILABLE$"):
        session.capture_calendar_session()
    assert native.calls == []
    assert session.close()
