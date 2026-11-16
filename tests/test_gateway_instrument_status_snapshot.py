"""Synthetic provider-owner instrument-status snapshots; no native network."""

from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError, replace

import pytest
from test_ctp_callback_ingress_client import _FakeTraderApiFactory
from test_gateway_calendar_session import _reconnect
from test_iter62_settlement_readback_on_write import TODAY
from test_iter67_instrument_status_ingress import SYMBOL, VENUE, native

from bt_api_ctp import gateway_session as gateway_module
from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.ctp.callback_ingress import CtpTraderCallbackSourceTagsV2
from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.gateway_instrument_status_snapshot import CtpGatewayInstrumentStatusSnapshot
from bt_api_ctp.gateway_session import CtpGatewaySessionError

pytest_plugins = ("test_iter63_gateway_session",)

_TRADER_START = TraderClient.start
_TRADER_STOP_AND_WAIT = TraderClient.stop_and_wait


@pytest.fixture
def status_prepared(prepared, monkeypatch):
    original_start = TraderClient.start

    def start_with_source_tags(trader, **kwargs):
        original_start(trader, **kwargs)
        with trader._query_state_lock:
            tags = trader._query_source_tags_locked()
            trader._spi._callback_source_tags = CtpTraderCallbackSourceTagsV2(*tags)

    monkeypatch.setattr(TraderClient, "start", start_with_source_tags)
    return prepared


def _open(status_prepared):
    create, _, _, feeds, natives, _, _, _ = status_prepared
    session = create()
    session.connect(generation="status-snapshot-test", timeout=1)
    return session, feeds[0], feeds[0].trader_client, natives[0]


def _initial_snapshot(session):
    with pytest.raises(CtpGatewaySessionError, match="^INSTRUMENT_STATUS_UNAVAILABLE$"):
        session.capture_instrument_status(VENUE, SYMBOL)
    return session.capture_instrument_status(VENUE, SYMBOL)


def test_initial_scope_and_missing_view_are_diagnostic_only(status_prepared, monkeypatch):
    session, feed, trader, native_api = _open(status_prepared)
    snapshot = _initial_snapshot(session)
    assert type(snapshot) is CtpGatewayInstrumentStatusSnapshot
    assert snapshot.venue == VENUE and snapshot.native_symbol == SYMBOL
    assert snapshot.view is not None and not snapshot.view.usable
    assert snapshot.session_generation == str(trader.connection_generation)
    assert snapshot.trading_day == TODAY
    assert not snapshot.authorizes_trading and not snapshot.continuity_proven
    assert "account" not in repr(snapshot)
    assert session.snapshot_is_current(snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.venue = "other"
    with pytest.raises(ValueError):
        replace(snapshot, native_symbol="other")
    with monkeypatch.context() as patch:

        def forbidden(*_args, **_kwargs):
            pytest.fail("snapshot attempted native, filesystem or connection work")

        for flow in (session._flow, feed._trusted_flow, trader._trusted_flow):
            patch.setattr(flow, "verify_root", forbidden)
            patch.setattr(flow, "_capture_root", forbidden)
        patch.setattr(session._reader, "borrow_request_feed", forbidden)
        patch.setattr(feed, "_ensure_connected", forbidden)
        patch.setattr(gateway_module.os, "stat", forbidden)
        patch.setattr(gateway_module.os, "open", forbidden)
        patch.setattr("builtins.open", forbidden)
        patch.setattr(native_api, "ReqAuthenticate", forbidden)
        patch.setattr(native_api, "ReqUserLogin", forbidden, raising=False)
        patch.setattr(native_api, "ReqQrySettlementInfoConfirm", forbidden)
        patch.setattr(native_api, "ReqSettlementInfoConfirm", forbidden)
        patch.setattr(native_api, "ReqOrderInsert", forbidden)
        patch.setattr(native_api, "ReqOrderAction", forbidden)
        patch.setattr(session._direct, "instrument_status_view", lambda *_: None)
        absent = session.capture_instrument_status(VENUE, SYMBOL)
        assert absent.view is None and session.snapshot_is_current(absent)
        assert native_api.calls == []
    assert session.close()
    assert not session.snapshot_is_current(snapshot)


def test_halt_then_continuous_invalidates_prior_snapshot(status_prepared):
    session, _, trader, _ = _open(status_prepared)
    trader._spi.OnRtnInstrumentStatus(native("6"))
    halted = session.capture_instrument_status(VENUE, SYMBOL)
    assert halted.view.usable and halted.view.effective_status_category == "closed"
    assert session.snapshot_is_current(halted)
    trader._spi.OnRtnInstrumentStatus(native("2"))
    assert not session.snapshot_is_current(halted)
    current = session.capture_instrument_status(VENUE, SYMBOL)
    assert current.view.usable and current.view.effective_status_category == "continuous"
    assert session.snapshot_is_current(current)
    assert session.close()


def test_callback_during_view_causes_finite_refusal(status_prepared, monkeypatch):
    session, _, trader, _ = _open(status_prepared)
    trader._spi.OnRtnInstrumentStatus(native("6"))
    direct = session._direct
    original = direct.instrument_status_view

    def view_then_callback(*args):
        result = original(*args)
        trader._spi.OnRtnInstrumentStatus(native("2"))
        return result

    monkeypatch.setattr(direct, "instrument_status_view", view_then_callback)
    with pytest.raises(CtpGatewaySessionError, match="^INSTRUMENT_STATUS_UNAVAILABLE$"):
        session.capture_instrument_status(VENUE, SYMBOL)
    assert session.close()


def test_reconnect_day_change_and_source_replacement_invalidate(status_prepared, monkeypatch):
    session, _, trader, native_api = _open(status_prepared)
    trader._spi.OnRtnInstrumentStatus(native("6"))
    old = session.capture_instrument_status(VENUE, SYMBOL)
    _reconnect(trader, native_api, monkeypatch, TODAY)
    assert not session.snapshot_is_current(old)
    with pytest.raises(CtpGatewaySessionError, match="^INSTRUMENT_STATUS_UNAVAILABLE$"):
        session.capture_instrument_status(VENUE, SYMBOL)
    same_day = session.capture_instrument_status(VENUE, SYMBOL)
    assert session.snapshot_is_current(same_day)
    assert not same_day.view.usable
    assert same_day.view.observation.trading_day == TODAY
    _reconnect(trader, native_api, monkeypatch, "20261109")
    assert not session.snapshot_is_current(same_day)
    with pytest.raises(CtpGatewaySessionError, match="^INSTRUMENT_STATUS_UNAVAILABLE$"):
        session.capture_instrument_status(VENUE, SYMBOL)
    next_day = session.capture_instrument_status(VENUE, SYMBOL)
    assert next_day.trading_day == "20261109"
    assert not next_day.view.usable and next_day.view.observation.trading_day == TODAY
    with monkeypatch.context() as patch:
        patch.setattr(trader._spi, "_native_spi_source_id", "other")
        assert not session.snapshot_is_current(next_day)
        with pytest.raises(CtpGatewaySessionError, match="^INSTRUMENT_STATUS_UNAVAILABLE$"):
            session.capture_instrument_status(VENUE, SYMBOL)
    assert session.close()


def test_busy_wrong_owner_invalid_scope_and_other_session(status_prepared):
    session, _, trader, _ = _open(status_prepared)
    snapshot = _initial_snapshot(session)
    for venue, symbol in [("", SYMBOL), (VENUE, " " + SYMBOL), (object(), SYMBOL), (VENUE, True)]:
        with pytest.raises(CtpGatewaySessionError, match="^INSTRUMENT_STATUS_UNAVAILABLE$"):
            session.capture_instrument_status(venue, symbol)
    other, _, _, _ = _open(status_prepared)
    assert not other.snapshot_is_current(snapshot)
    assert not session.snapshot_is_current(object())
    assert not session.snapshot_is_current(object.__new__(CtpGatewayInstrumentStatusSnapshot))
    assert other.close()
    acquired, release = threading.Event(), threading.Event()

    def hold_lock():
        with trader._query_state_lock:
            acquired.set()
            assert release.wait(3)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert acquired.wait(1)
    try:
        with pytest.raises(CtpGatewaySessionError, match="^INSTRUMENT_STATUS_BUSY$"):
            session.capture_instrument_status(VENUE, SYMBOL)
        assert not session.snapshot_is_current(snapshot)
        errors = []

        def wrong_owner():
            for fn in (
                lambda: session.capture_instrument_status(VENUE, SYMBOL),
                lambda: session.snapshot_is_current(snapshot),
            ):
                try:
                    fn()
                except CtpGatewaySessionError as exc:
                    errors.append(exc.code)

        other_thread = threading.Thread(target=wrong_owner)
        other_thread.start()
        other_thread.join(1)
        assert errors == ["WRONG_OWNER", "WRONG_OWNER"]
    finally:
        release.set()
        thread.join(1)
    old_pid = session._owner_pid
    session._owner_pid += 1
    try:
        with pytest.raises(CtpGatewaySessionError, match="^WRONG_OWNER$"):
            session.capture_instrument_status(VENUE, SYMBOL)
        with pytest.raises(CtpGatewaySessionError, match="^WRONG_OWNER$"):
            session.snapshot_is_current(snapshot)
    finally:
        session._owner_pid = old_pid
    assert session.close()


def test_rejects_view_scope_mismatch_and_mismatched_usable_observation(
    status_prepared, monkeypatch
):
    session, _, trader, _ = _open(status_prepared)
    trader._spi.OnRtnInstrumentStatus(native("2"))
    valid = session.capture_instrument_status(VENUE, SYMBOL)
    direct = session._direct
    original = direct.instrument_status_view
    with monkeypatch.context() as patch:
        patch.setattr(
            direct, "instrument_status_view", lambda *_: replace(valid.view, trading_day="20261109")
        )
        with pytest.raises(CtpGatewaySessionError, match="^INSTRUMENT_STATUS_UNAVAILABLE$"):
            session.capture_instrument_status(VENUE, SYMBOL)
        mismatched = replace(valid.view.observation, session_generation="999")
        patch.setattr(
            direct, "instrument_status_view", lambda *_: replace(valid.view, observation=mismatched)
        )
        with pytest.raises(CtpGatewaySessionError, match="^INSTRUMENT_STATUS_UNAVAILABLE$"):
            session.capture_instrument_status(VENUE, SYMBOL)
    assert direct.instrument_status_view == original
    assert session.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("venue", "OTHER"),
        ("native_symbol", "other"),
        ("accepted", False),
        ("status_category", "closed"),
    ],
)
def test_usable_view_nested_observation_must_match_scope(
    status_prepared, monkeypatch, field, value
):
    session, _, trader, _ = _open(status_prepared)
    trader._spi.OnRtnInstrumentStatus(native("2"))
    valid = session.capture_instrument_status(VENUE, SYMBOL)
    invalid = replace(valid.view, observation=replace(valid.view.observation, **{field: value}))
    monkeypatch.setattr(session._direct, "instrument_status_view", lambda *_: invalid)
    with pytest.raises(CtpGatewaySessionError, match="^INSTRUMENT_STATUS_UNAVAILABLE$"):
        session.capture_instrument_status(VENUE, SYMBOL)
    assert session.close()


@pytest.mark.parametrize(
    "attribute",
    ["_instrument_status_listener", "_instrument_status_bridge", "_instrument_status_lock"],
)
def test_provider_source_object_replacement_invalidates(status_prepared, monkeypatch, attribute):
    session, _, _, _ = _open(status_prepared)
    snapshot = _initial_snapshot(session)
    with monkeypatch.context() as patch:
        value = threading.RLock() if attribute == "_instrument_status_lock" else object()
        patch.setattr(session._direct, attribute, value)
        assert not session.snapshot_is_current(snapshot)
    assert session.close()


def test_disconnect_revokes_snapshot_before_relogin(status_prepared):
    session, _, trader, native_api = _open(status_prepared)
    trader._spi.OnRtnInstrumentStatus(native("6"))
    snapshot = session.capture_instrument_status(VENUE, SYMBOL)
    trader._spi.OnFrontDisconnected(1)
    assert not session.snapshot_is_current(snapshot)
    with pytest.raises(CtpGatewaySessionError, match="^INSTRUMENT_STATUS_UNAVAILABLE$"):
        session.capture_instrument_status(VENUE, SYMBOL)
    assert native_api.calls == []
    assert session.close()


def test_gateway_snapshot_uses_real_trader_start_with_fake_native(prepared, monkeypatch):
    """Exercise the registration path; fake native never contacts a front."""
    _FakeTraderApiFactory.instances = []
    _FakeTraderApiFactory.block_join = False
    _FakeTraderApiFactory.register_spi_exception = None
    _FakeTraderApiFactory.init_exception = None
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "CThostFtdcTraderApi", _FakeTraderApiFactory)
    monkeypatch.setattr(TraderClient, "start", _TRADER_START)
    monkeypatch.setattr(TraderClient, "stop_and_wait", _TRADER_STOP_AND_WAIT)
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="real-start-fake-native", timeout=1)
    trader = feeds[0].trader_client
    api = _FakeTraderApiFactory.instances[-1]
    try:
        assert api.spi is trader._spi
        assert type(api.spi._callback_source_tags) is CtpTraderCallbackSourceTagsV2
        assert api.spi._callback_source_tags.connection_generation == 0
        api.spi.OnRtnInstrumentStatus(native("6"))
        first = session.capture_instrument_status(VENUE, SYMBOL)
        assert first.view.usable and first.view.effective_status_category == "closed"
        assert session.snapshot_is_current(first)
        original_generation = first.session_generation
        api.spi.OnFrontDisconnected(1)
        assert not session.snapshot_is_current(first)
        api.spi.OnFrontConnected()  # fake API synchronously authenticates and logs in
        api.spi.OnRtnInstrumentStatus(native("2"))
        second = session.capture_instrument_status(VENUE, SYMBOL)
        assert second.view.usable and second.view.effective_status_category == "continuous"
        assert second.session_generation != original_generation
        assert session.snapshot_is_current(second)
        assert {call[0] for call in api.calls} <= {"ReqAuthenticate", "ReqUserLogin"}
    finally:
        assert session.close()
