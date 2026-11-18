"""Iteration 67 D67-16: InstrumentStatus prior-pause, recovery & listener-health linkage."""

from __future__ import annotations

import pytest
from test_ctp_callback_ingress_client import _FakeTraderApiFactory
from test_instrument_status_source_fence import _payload
from test_iter67_instrument_status_ingress import DAY, SYMBOL, VENUE, FakeTrader as _BaseFakeTrader, _feed_with, native


class FakeTrader(_BaseFakeTrader):
    def __init__(self, *, generation=7, day=DAY):
        super().__init__()
        self.trading_day = day
        self.session_state["trading_day"] = day
        self.generation = generation
        self.spi = object()

    @property
    def generation(self):
        return self.connection_generation

    @generation.setter
    def generation(self, value):
        self.connection_generation = value
        self.session_state["connection_generation"] = value

import bt_api_ctp.ctp.client as client_module
from bt_api_ctp.ctp.client import TraderClient, _TraderSpi
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_ctp.instrument_status_observation import (
    ACTION_APPLIED,
    ACTION_STALE_GENERATION,
    CODE_NEVER_OBSERVED,
    CODE_SOURCE_UNAVAILABLE,
    CODE_STALE_GENERATION,
    RECEIVE_CLOCK_MONOTONIC_NS,
    STATE_CURRENT,
    STATE_STALE,
    STATUS_CONTINUOUS,
    STATUS_NO_TRADING,
    STATUS_UNKNOWN,
    InstrumentStatusLedger,
    InstrumentStatusSessionFacts,
    project_native_instrument_status,
)


def _session(generation: str = "7", day: str = DAY, sequence: int = 1, available: bool = True):
    return InstrumentStatusSessionFacts(
        session_generation=generation,
        trading_day=day,
        source_sequence=sequence,
        receive_clock_domain=RECEIVE_CLOCK_MONOTONIC_NS,
        receive_time=1000 + sequence,
        source_available=available,
    )


def _obs(status: str = "1", *, generation: str = "7", day: str = DAY, sequence: int = 1):
    fields = {
        "ExchangeID": VENUE,
        "ExchangeInstID": SYMBOL,
        "InstrumentID": SYMBOL,
        "InstrumentStatus": status,
        "TradingSegmentSN": 1,
        "EnterTime": "09:30:00",
        "EnterReason": "1",
    }
    obs = project_native_instrument_status(
        fields,
        session=_session(generation=generation, day=day, sequence=sequence),
    )
    assert obs.accepted is True
    return obs


def test_ledger_preserves_invalidated_prior_pause_until_new_continuous():
    ledger = InstrumentStatusLedger(session_generation="1", trading_day=DAY)
    res = ledger.observe(_obs("1", generation="1", sequence=1))
    assert res.applied is True and res.action == ACTION_APPLIED
    assert ledger.prior_pause(VENUE, SYMBOL).invalidated is False
    assert ledger.view(VENUE, SYMBOL).effective_status_category == STATUS_NO_TRADING

    # Reconnect bumps generation: prior pause becomes invalidated, never ABSENT/usable.
    ledger.begin_session_generation("2", DAY)
    view = ledger.view(VENUE, SYMBOL)
    assert view.state == STATE_STALE
    assert view.usable is False
    assert view.reason_code == CODE_STALE_GENERATION
    assert view.reason_code != CODE_NEVER_OBSERVED
    assert view.prior_pause is not None
    assert view.prior_pause.invalidated is True
    assert view.prior_pause.session_generation == "1"
    assert view.prior_pause.status_category == STATUS_NO_TRADING

    # Late callback from generation 1 is refused and cannot clear gen 2 prior pause.
    late = ledger.observe(_obs("2", generation="1", sequence=9))
    assert late.applied is False and late.action == ACTION_STALE_GENERATION
    assert ledger.prior_pause(VENUE, SYMBOL) is not None

    # Fresh ledger seeded via carry_forward_from keeps prior pause invalidated.
    next_ledger = InstrumentStatusLedger(session_generation="3", trading_day=DAY)
    next_ledger.carry_forward_from(ledger)
    carried = next_ledger.view(VENUE, SYMBOL)
    assert carried.state == STATE_STALE
    assert carried.usable is False
    assert carried.prior_pause is not None
    assert carried.prior_pause.invalidated is True
    assert carried.prior_pause.session_generation == "1"

    # Explicit verified TRADABLE ('2') in current generation clears prior pause.
    res2 = next_ledger.observe(_obs("2", generation="3", sequence=2))
    assert res2.applied is True
    current_view = next_ledger.view(VENUE, SYMBOL)
    assert current_view.state == STATE_CURRENT
    assert current_view.usable is True
    assert current_view.effective_status_category == STATUS_CONTINUOUS
    assert current_view.prior_pause is None
    assert next_ledger.prior_pause(VENUE, SYMBOL) is None


def test_ledger_mark_source_unavailable_invalidates_prior_pause():
    ledger = InstrumentStatusLedger(session_generation="4", trading_day=DAY)
    assert ledger.observe(_obs("0", generation="4", sequence=1)).applied is True
    ledger.mark_source_unavailable()
    assert ledger.source_available is False
    view = ledger.view(VENUE, SYMBOL)
    assert view.state == STATE_STALE
    assert view.usable is False
    assert view.reason_code == CODE_SOURCE_UNAVAILABLE
    assert view.prior_pause is not None
    assert view.prior_pause.invalidated is True
    assert view.prior_pause.invalidation_reason == CODE_SOURCE_UNAVAILABLE


def test_trader_client_listener_state_verifies_connection_spi_and_ingress(monkeypatch):
    _FakeTraderApiFactory.instances = []
    _FakeTraderApiFactory.block_join = False
    _FakeTraderApiFactory.register_spi_exception = None
    _FakeTraderApiFactory.init_exception = None
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "fake-flow")
    monkeypatch.setattr(client_module, "_register_ctp_native_api", lambda _api: None)
    monkeypatch.setattr(client_module, "CThostFtdcTraderApi", _FakeTraderApiFactory)

    client = TraderClient(
        "tcp://fake", "9999", "investor-1", "not-a-real-secret", auto_settlement_confirm=False
    )
    client.start()
    try:
        listener = lambda _payload, **_kw: None
        client.add_instrument_status_listener(listener)
        gen, day, registered, healthy, spi = client.instrument_status_listener_state(listener)
        assert isinstance(gen, int) and gen >= 1
        assert day == "20260926"
        assert (registered, healthy) == (True, True)
        assert spi is client._spi

        # Unregistered listener is unregistered (even when client connection is healthy).
        other = lambda _payload, **_kw: None
        _, _, r2, h2, _ = client.instrument_status_listener_state(other)
        assert (r2, h2) == (False, True)

        # Replaced SPI makes current SPI binding fail closed (Wait: client returns spi when healthy, check healthy=False when replaced).
        orig_spi = client._spi
        client._spi = _TraderSpi(client, native_api=object())
        _, _, r3, h3, s3 = client.instrument_status_listener_state(listener)
        assert (r3, h3) == (True, False)
        client._spi = orig_spi

        # Disconnected client makes listener unhealthy.
        client._connected = False
        _, _, r4, h4, _ = client.instrument_status_listener_state(listener)
        assert h4 is False
        client._connected = True

        # Poisoned ingress makes listener unhealthy.
        from types import SimpleNamespace
        client._callback_ingress = SimpleNamespace(poisoned=True, native_call_refs=0, callback_refs=0)
        _, _, r5, h5, _ = client.instrument_status_listener_state(listener)
        assert h5 is False
        client._callback_ingress = None
    finally:
        client.stop()


def test_provider_preserves_prior_pause_across_spi_and_trader_reconnect():
    trader1 = FakeTrader(generation=7, day=DAY)
    feed = _feed_with(trader1)
    provider = CtpDirectWriteProvider(feed)
    try:
        provider.ensure_listening()
        trader1.status_listeners[0](_payload(generation=7, status="1", sequence=1))

        # Reconnect 1: SPI replaced on same trader with bumped generation.
        trader1.generation = 8
        trader1.spi = object()

        view = provider.instrument_status_view(VENUE, SYMBOL)
        assert view.state == STATE_STALE
        assert view.usable is False
        assert view.prior_pause is not None
        assert view.prior_pause.invalidated is True
        assert view.prior_pause.session_generation == "7"
        assert view.prior_pause.status_category == STATUS_NO_TRADING
        assert view.recovery_state is not None
        assert view.recovery_state.prior_pause_observed is True
        assert view.recovery_state.prior_pause_invalidated is True
        assert view.recovery_state.return_recovery_completed is False
        assert view.recovery_state.listener_healthy is True

        # Reconnect 2: trader client replaced completely (even before view() is called after a new pause).
        assert view.recovery_state.trader_id is not None
        old_trader_id = view.recovery_state.trader_id
        old_spi_id = view.recovery_state.spi_id

        # Observe another pause on trader1 gen 8 without calling instrument_status_view first.
        trader1.status_listeners[0](_payload(generation=8, status="3", sequence=2))

        trader2 = FakeTrader(generation=9, day=DAY)
        feed._trader = trader2
        # Before ensure_listening() rebinds listener to trader2, listener health is False!
        mid_health = provider.instrument_status_listener_health()
        assert mid_health.healthy is False
        assert mid_health.trader_bound is False
        mid_view = provider.instrument_status_view(VENUE, SYMBOL)
        assert mid_view.state == STATE_STALE
        assert mid_view.usable is False
        assert mid_view.prior_pause is not None
        assert mid_view.prior_pause.invalidated is True
        assert mid_view.recovery_state.listener_healthy is False

        # Re-bind listener on trader2 -> healthy in gen 9, prior pause still preserved & invalidated.
        provider.ensure_listening()
        health2 = provider.instrument_status_listener_health()
        assert health2.healthy is True
        assert health2.session_generation == "9"
        assert health2.trader_id != old_trader_id
        assert health2.spi_id != old_spi_id
        rec2 = provider.instrument_status_recovery_state(VENUE, SYMBOL)
        assert rec2.prior_pause_observed is True
        assert rec2.prior_pause_invalidated is True
        assert rec2.prior_pause_generation == "8"
        assert rec2.listener_healthy is True
        assert rec2.return_recovery_completed is False
    finally:
        provider.close()


def test_provider_recovery_fence_and_listener_health_coupling():
    trader = FakeTrader(generation=10, day=DAY)
    provider = CtpDirectWriteProvider(_feed_with(trader))
    try:
        provider.ensure_listening()
        trader.status_listeners[0](_payload(generation=10, status="1", sequence=1))

        # Reconnect to gen 11.
        trader.generation = 11
        trader.spi = object()

        fence_flag = {"ready": False, "gen": 11}

        def fence():
            if not fence_flag["ready"]:
                return False
            return (fence_flag["gen"], "ctp_gateway_return_maintenance")

        provider.bind_instrument_status_recovery_fence(fence)

        # Incomplete recovery fence -> return_recovery_completed is False.
        rec = provider.instrument_status_recovery_state(VENUE, SYMBOL)
        assert rec.prior_pause_observed is True
        assert rec.prior_pause_invalidated is True
        assert rec.listener_healthy is True
        assert rec.return_recovery_completed is False
        assert rec.recovery_fence is None

        # Fence returning old generation -> still False!
        fence_flag["ready"] = True
        fence_flag["gen"] = 10
        assert provider.instrument_status_recovery_state(VENUE, SYMBOL).return_recovery_completed is False

        # Fence returning matching generation -> recovery completed!
        fence_flag["gen"] = 11
        rec_ok = provider.instrument_status_recovery_state(VENUE, SYMBOL)
        assert rec_ok.return_recovery_completed is True
        assert rec_ok.listener_healthy is True
        assert rec_ok.recovery_fence == "ctp_gateway_return_maintenance"
        assert rec_ok.trader_id and rec_ok.listener_id and rec_ok.spi_id

        # Breaking listener health revokes both listener_healthy AND return_recovery_completed.
        bound = trader.status_listeners[0]
        trader.remove_instrument_status_listener(bound)
        rec_broken = provider.instrument_status_recovery_state(VENUE, SYMBOL)
        assert rec_broken.listener_healthy is False
        assert rec_broken.return_recovery_completed is False

        # Restore listener and deliver explicit new-generation TRADABLE ('2').
        trader.add_instrument_status_listener(bound)
        fence_flag["ready"] = False
        trader.status_listeners[0](_payload(generation=11, status="2", sequence=2))
        view_tradable = provider.instrument_status_view(VENUE, SYMBOL)
        assert view_tradable.usable is True
        assert view_tradable.effective_status_category == STATUS_CONTINUOUS
        assert view_tradable.prior_pause is None
        assert view_tradable.recovery_state.prior_pause_observed is False
    finally:
        provider.close()


def test_provider_manual_recovery_marking_is_bound_to_generation_and_spi():
    trader = FakeTrader(generation=5, day=DAY)
    provider = CtpDirectWriteProvider(_feed_with(trader))
    try:
        assert provider.mark_instrument_status_return_recovery_completed() is False
        provider.ensure_listening()
        assert provider.mark_instrument_status_return_recovery_completed(
            recovery_fence="manual_return_fence"
        ) is True
        rec = provider.instrument_status_recovery_state(VENUE, SYMBOL)
        assert rec.return_recovery_completed is True
        assert rec.recovery_fence == "manual_return_fence"

        # SPI change invalidates manual return recovery automatically.
        trader.generation = 6
        trader.spi = object()
        rec2 = provider.instrument_status_recovery_state(VENUE, SYMBOL)
        assert rec2.return_recovery_completed is False
        assert rec2.recovery_fence is None

        # explicit clear clears even same-generation recovery.
        assert provider.mark_instrument_status_return_recovery_completed() is True
        assert provider.instrument_status_recovery_state(VENUE, SYMBOL).return_recovery_completed is True
        provider.clear_instrument_status_return_recovery()
        assert provider.instrument_status_recovery_state(VENUE, SYMBOL).return_recovery_completed is False
    finally:
        provider.close()
