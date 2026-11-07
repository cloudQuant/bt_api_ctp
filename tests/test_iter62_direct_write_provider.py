"""Iteration 62: CTP direct write provider, return projection and session schedule."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bt_api_base.direct_write_protocol import (
    DirectExecutionEventKind,
    DirectOrderStatus,
    DirectWriteError,
    NativeOrderIdentity,
)
from bt_api_base.trading_session_protocol import TradingPhase
from bt_api_ctp import direct_write_provider as dwp
from bt_api_ctp.direct_write_provider import (
    CtpDirectWriteProvider,
    create_ctp_direct_write_provider,
    project_ctp_execution_event,
)
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
from bt_api_ctp.trading_session_schedule import CHINA_TIME, CtpTradingSessionSchedule


def _feed() -> CtpRequestDataFuture:
    feed = CtpRequestDataFuture(
        broker_id="broker-test",
        user_id="investor-test",
        password="synthetic-only",
        td_front="tcp://127.0.0.1:19991",
        md_front="tcp://127.0.0.1:19992",
        auto_settlement_confirm=False,
    )
    feed._connect_timeout = 0.01
    return feed


class FakeTrader:
    connection_generation = 4

    def __init__(self):
        self.listeners = []
        self._next = 40

    def add_execution_listener(self, listener):
        self.listeners.append(listener)

    def remove_execution_listener(self, listener):
        self.listeners.remove(listener)

    def get_session_state(self):
        return {
            "front_id": 3,
            "session_id": 99,
            "trading_day": "20261106",
            "connection_generation": self.connection_generation,
        }

    def next_order_ref(self):
        self._next += 1
        return str(self._next)


class Req:
    """Duck-typed v1 order request (the provider never imports the SDK models)."""

    symbol = "rb2701"
    exchange_id = "SHFE"
    order_type = SimpleNamespace(value="limit")
    side = SimpleNamespace(value="buy")
    price = Decimal("3000")
    quantity = Decimal("1")
    position_id = None
    time_in_force = "GFD"
    offset = "open"
    reduce_only = False
    position_side = None
    position_mode = None
    client_order_id = "t1"


def _provider_with_fake_trader():
    feed = _feed()
    trader = FakeTrader()
    feed._trader = trader
    return CtpDirectWriteProvider(feed), trader


def test_factory_wraps_only_the_canonical_feed():
    assert isinstance(create_ctp_direct_write_provider(_feed()), CtpDirectWriteProvider)
    with pytest.raises(DirectWriteError):
        CtpDirectWriteProvider(object())


def test_reserve_pads_order_ref_and_binds_session():
    provider, trader = _provider_with_fake_trader()
    reservation = provider.reserve_order(Req())
    identity = reservation.identity
    assert identity.client_order_id == "000000000041"
    assert dict(identity.fields) == {
        "order_ref": "000000000041",
        "front_id": 3,
        "session_id": 99,
        "trading_day": "20261106",
        "exchange_id": "SHFE",
    }
    assert identity.session_generation == 4
    assert trader.listeners == [provider._on_native_return]


def test_reserve_requires_exchange_id():
    provider, _trader = _provider_with_fake_trader()

    class NoExchange(Req):
        exchange_id = None

    with pytest.raises(DirectWriteError) as info:
        provider.reserve_order(NoExchange())
    assert info.value.code == "ctp_direct_exchange_id_required"


def test_submit_refuses_unknown_or_stale_reservations():
    provider, trader = _provider_with_fake_trader()
    reservation = provider.reserve_order(Req())
    trader.connection_generation = 5  # reconnect after reservation
    with pytest.raises(DirectWriteError) as info:
        provider.submit_order(reservation, Req(), grant=None, verifier=None)
    assert info.value.code == "ctp_direct_reservation_session_changed"
    with pytest.raises(DirectWriteError) as info:
        provider.submit_order(reservation, Req(), grant=None, verifier=None)
    assert info.value.code == "ctp_direct_reservation_unknown"


def test_cancel_requires_a_complete_original_identity():
    provider, _trader = _provider_with_fake_trader()
    incomplete = NativeOrderIdentity(
        exchange_name="CTP___FUTURE",
        client_order_id="000000000041",
        fields={"order_ref": "000000000041", "exchange_id": "SHFE"},
    )
    with pytest.raises(DirectWriteError) as info:
        provider.cancel_order(incomplete, "rb2701", grant=None, verifier=None)
    assert info.value.code == "ctp_direct_cancel_identity_incomplete"


def test_native_returns_are_queued_as_neutral_events():
    provider, trader = _provider_with_fake_trader()
    provider.reserve_order(Req())  # attaches the listener
    listener = trader.listeners[0]
    listener(
        "order",
        {
            "InstrumentID": "rb2701",
            "ExchangeID": "SHFE",
            "OrderRef": "000000000041",
            "FrontID": 3,
            "SessionID": 99,
            "OrderSysID": "  1234",
            "OrderSubmitStatus": "3",
            "OrderStatus": "3",
            "Direction": "0",
            "CombOffsetFlag": "0",
            "VolumeTotalOriginal": 1,
            "VolumeTraded": 0,
            "VolumeTotal": 1,
            "LimitPrice": 3000.0,
            "TradingDay": "20261106",
            "_connection_generation": 4,
        },
    )
    (event,) = provider.drain_execution_events(10)
    assert event.kind is DirectExecutionEventKind.ORDER
    assert event.status is DirectOrderStatus.ACCEPTED
    assert event.exchange_order_id == "1234"
    assert event.side == "buy" and event.offset == "open"
    assert event.identity.fields["front_id"] == 3


@pytest.mark.parametrize(
    "submit,status,traded,expected",
    [
        ("4", "5", 0, DirectOrderStatus.REJECTED),
        ("3", "0", 1, DirectOrderStatus.FILLED),
        ("3", "1", 1, DirectOrderStatus.PARTIALLY_FILLED),
        ("3", "5", 0, DirectOrderStatus.CANCELLED),
        ("3", "2", 1, DirectOrderStatus.CANCELLED),
        ("0", "a", 0, DirectOrderStatus.SUBMITTED),
    ],
)
def test_order_status_mapping(submit, status, traded, expected):
    event = project_ctp_execution_event(
        "order",
        {
            "InstrumentID": "rb2701",
            "OrderRef": "1",
            "OrderSubmitStatus": submit,
            "OrderStatus": status,
            "VolumeTraded": traded,
        },
        sequence=1,
    )
    assert event.status is expected


def test_trade_and_error_projection():
    trade = project_ctp_execution_event(
        "trade",
        {
            "InstrumentID": "rb2701",
            "ExchangeID": "SHFE",
            "OrderRef": "000000000041",
            "OrderSysID": "1234",
            "TradeID": " 77",
            "Direction": "1",
            "OffsetFlag": "3",
            "Price": 3001.0,
            "Volume": 1,
            "TradeDate": "20261105",
            "TradeTime": "21:30:01",
        },
        sequence=2,
    )
    assert trade.kind is DirectExecutionEventKind.TRADE
    assert trade.trade_id == "77" and trade.side == "sell" and trade.offset == "close_today"
    error = project_ctp_execution_event(
        "order_insert_error",
        {
            "InstrumentID": "rb2701",
            "OrderRef": "000000000042",
            "_error_code": 31,
            "_error_message": "资金不足",
        },
        sequence=3,
    )
    assert error.kind is DirectExecutionEventKind.ORDER_ERROR
    assert error.status is DirectOrderStatus.REJECTED and error.error_code == "31"


def _insert_error_snapshot(source):
    return {
        "InstrumentID": "rb2701",
        "ExchangeID": "SHFE",
        "OrderRef": "000000000042",
        "_error_code": 31,
        "_error_message": "资金不足",
        "_source": source,
        "_login_front_id": 1,
        "_login_session_id": 7,
        "_trading_day": "20261106",
        "_connection_generation": 3,
    }


def test_insert_error_response_names_the_order_by_its_session_triple():
    """OnRspOrderInsert answers this session's request: its error carries the
    session triple, so the host can match (and reject) its own order."""
    error = project_ctp_execution_event(
        "order_insert_error", _insert_error_snapshot("OnRspOrderInsert"), sequence=1
    )
    fields = error.identity.fields
    assert (fields["front_id"], fields["session_id"], fields["order_ref"]) == (
        1,
        7,
        "000000000042",
    )
    assert error.status is DirectOrderStatus.REJECTED


def test_private_flow_insert_error_is_not_attributed_to_this_session():
    """OnErrRtnOrderInsert comes on the private flow shared by the account's
    sessions; without its own FrontID/SessionID it is not claimed."""
    error = project_ctp_execution_event(
        "order_insert_error", _insert_error_snapshot("OnErrRtnOrderInsert"), sequence=1
    )
    assert "front_id" not in error.identity.fields
    assert "session_id" not in error.identity.fields


# ------------------------------------------------------------- lost returns


def _order_snapshot(ref):
    return {
        "InstrumentID": "rb2701",
        "ExchangeID": "SHFE",
        "OrderRef": ref,
        "FrontID": 3,
        "SessionID": 99,
        "OrderSubmitStatus": "3",
        "OrderStatus": "3",
        "TradingDay": "20261106",
    }


class _Unprintable:
    def __str__(self):
        raise RuntimeError("unprintable native field")


def _drop_records(caplog):
    return [
        r
        for r in caplog.records
        if r.name == dwp.__name__ and r.levelno == logging.WARNING and "dropped" in r.getMessage()
    ]


def test_queue_overflow_drops_the_oldest_return_and_counts_it(monkeypatch, caplog):
    monkeypatch.setattr(dwp, "_MAX_EVENTS", 2)
    provider, _trader = _provider_with_fake_trader()
    assert provider.dropped_events == 0
    with caplog.at_level(logging.WARNING, logger=dwp.__name__):
        for ref in ("41", "42", "43", "44"):
            provider._on_native_return("order", _order_snapshot(ref))
    assert provider.dropped_events == 2
    assert [e.identity.client_order_id for e in provider.drain_execution_events(10)] == [
        "43",
        "44",
    ]
    (record,) = _drop_records(caplog)  # rate-limited: one line for both drops
    assert "queue_overflow" in record.getMessage()


def test_projection_failure_is_counted_and_logged_without_the_payload(monkeypatch, caplog):
    clock = [1000.0]
    monkeypatch.setattr(dwp, "_monotonic", lambda: clock[0])
    provider, _trader = _provider_with_fake_trader()
    snapshot = dict(
        _order_snapshot("45"),
        OrderSysID=_Unprintable(),
        BrokerID="broker-test",
        InvestorID="investor-test",
    )
    with caplog.at_level(logging.WARNING, logger=dwp.__name__):
        provider._on_native_return("order", snapshot)
        clock[0] += 10.0
        provider._on_native_return("order", snapshot)  # inside the interval: counted only
    assert provider.dropped_events == 2
    assert provider.drain_execution_events(10) == ()
    (record,) = _drop_records(caplog)
    text = record.getMessage()
    assert "projection_failed" in text and "RuntimeError" in text
    for secret in ("broker-test", "investor-test", "synthetic-only", "unprintable native"):
        assert secret not in text
    # The next failure after the interval is logged again with the running total.
    clock[0] += dwp._DROPPED_LOG_INTERVAL
    with caplog.at_level(logging.WARNING, logger=dwp.__name__):
        provider._on_native_return("trade", snapshot)
    assert provider.dropped_events == 3
    records = _drop_records(caplog)
    assert len(records) == 2 and "total=3" in records[-1].getMessage()
    # A valid return still flows.
    provider._on_native_return("order", _order_snapshot("46"))
    (event,) = provider.drain_execution_events(10)
    assert event.identity.client_order_id == "46" and provider.dropped_events == 3


def test_a_return_without_an_instrument_is_not_a_drop():
    provider, _trader = _provider_with_fake_trader()
    provider._on_native_return("order", {"OrderRef": "47"})
    provider._on_native_return("unknown_kind", _order_snapshot("48"))
    assert provider.dropped_events == 0 and provider.drain_execution_events(10) == ()


# ------------------------------------------------------------- sessions


def _state(instrument, *args):
    return CtpTradingSessionSchedule().session_state(
        instrument, datetime(*args, tzinfo=CHINA_TIME)
    )


def test_night_session_and_closed_and_auction():
    assert _state("rb2701", 2026, 11, 5, 21, 50).phase is TradingPhase.CONTINUOUS
    closed = _state("rb2701", 2026, 11, 5, 23, 1)
    assert closed.phase is TradingPhase.CLOSED
    assert closed.next_continuous.session_id == "day-am-1"
    auction = _state("rb2701", 2026, 11, 6, 20, 56)
    assert auction.phase is TradingPhase.AUCTION and str(auction.trading_day) == "2026-11-09"


def test_no_night_session_before_holiday_and_day_auction_after():
    eve = _state("rb2701", 2026, 9, 30, 21, 30)
    assert eve.phase is TradingPhase.CLOSED and str(eve.trading_day) == "2026-10-08"
    after = _state("rb2701", 2026, 10, 8, 8, 56)
    assert after.phase is TradingPhase.AUCTION and after.current_window.session_id == "day-auction"


def test_cross_midnight_night_session_and_unknowns():
    assert _state("au2612", 2026, 11, 6, 2, 0).phase is TradingPhase.CONTINUOUS
    assert _state("rb2701", 2026, 12, 31, 21, 30).reason == "calendar_coverage_unavailable"
    assert _state("xx2701", 2026, 11, 6, 10, 0).reason == "product_session_unknown"
    assert _state("SA701", 2026, 11, 6, 10, 20).phase is TradingPhase.CLOSED  # 10:15 break


def test_unrepresentable_request_fails_before_any_reservation():
    provider, trader = _provider_with_fake_trader()

    class Gtc(Req):
        time_in_force = "GTC"

    class Market(Req):
        order_type = SimpleNamespace(value="market")
        price = None

    for request in (Gtc(), Market()):
        with pytest.raises(DirectWriteError) as info:
            provider.reserve_order(request)
        assert info.value.code == "ctp_direct_request_unrepresentable"
    assert trader._next == 40  # no OrderRef consumed
    assert provider._reservations == {}
