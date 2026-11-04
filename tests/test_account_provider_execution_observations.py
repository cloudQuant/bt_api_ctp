"""Iteration 60: Gateway execution observations of the read-only CTP provider.

UNIT/FAKE only. The request feed and its source-sealed query results are local
fakes built with the provider's own query-evidence constructors; the opaque
observation ids come from a local HMAC issuer that implements the Base
``ManagedOrderObservationIssuer`` protocol (a persisted secret stands in for
the SDK's durable issuer). No CTP front, SimNow or account is contacted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from bt_api_base.account_protocol import AccountKey
from bt_api_base.execution_correlation_protocol import (
    ManagedOrderObservationIssuer,
    OrderCorrelation,
)
from bt_api_base.execution_row_observation_protocol import (
    AccountExecutionOrderObservation,
    AccountExecutionTradeObservation,
    execution_observation_fact_digest,
    execution_observation_fact_id,
)
from bt_api_base.market_protocol import MarketInstrumentKey

import bt_api_ctp.account_provider as provider_module
from bt_api_ctp.account_provider import (
    CtpAccountSessionProviderError,
    CtpReadOnlyAccountSessionProvider,
)
from bt_api_ctp.ctp.client import CtpNativeStopReceipt
from bt_api_ctp.query import (
    QueryResult,
    _attach_query_source,
    _new_query_session_scope,
    _new_query_source,
    _query_records_digest,
)

_ACCOUNT = AccountKey("ctp", "simnow", "simnow", "investor-test", "broker-test")
_TRADING_DAY = "20261017"
_FINGERPRINT = "0123456789abcdef"
_SOURCE_TAGS = ("source-instance", "client-epoch", "api-source", "spi-source", 1, 7)
_COMPLETED = datetime(2026, 10, 17, 1, 30, tzinfo=timezone.utc)
_IF = MarketInstrumentKey("ctp", "CFFEX", "simnow", "future", "IF2612", "IF2612")
_RB = MarketInstrumentKey("ctp", "SHFE", "simnow", "future", "rb2610", "rb2610")
_SECRET = b"persisted-test-issuer-secret"


class Handle:
    """Opaque reservation handle (the projector refuses primitive handles)."""

    def __init__(self, mac: str) -> None:
        self.mac = mac


class HmacIssuer(ManagedOrderObservationIssuer):
    """Local stand-in for the SDK durable issuer: same secret, same ids."""

    def __init__(self, secret: bytes = _SECRET) -> None:
        self._secret = secret
        self.bindings: dict[str, OrderCorrelation] = {}

    def _mac(self, *parts: str) -> str:
        material = b"\0".join(part.encode("utf-8") for part in parts)
        return hmac.new(self._secret, material, hashlib.sha256).hexdigest()

    def issue_order_correlation(self, exchange_name: str, symbol: str, client_order_id: str):
        correlation = OrderCorrelation(
            exchange_name, symbol, client_order_id, "res-" + self._mac("res", client_order_id)
        )
        return correlation, Handle(self._mac("handle", correlation.reservation_ref))

    def validate_order_correlation(self, correlation, *, issuer_reservation) -> bool:
        return type(issuer_reservation) is Handle and hmac.compare_digest(
            issuer_reservation.mac, self._mac("handle", correlation.reservation_ref)
        )

    def issue_order_observation_id(self, provider_name, environment, account_ref, identity):
        return "sdk-order-observation-v1-" + self._mac(
            "order", provider_name, environment, account_ref, identity
        )

    def issue_trade_observation_id(self, provider_name, environment, account_ref, identity):
        return "sdk-trade-observation-v1-" + self._mac(
            "trade", provider_name, environment, account_ref, identity
        )

    def bind_order_observation(
        self, order_id, provider_name, environment, account_ref, scope_ref, identity,
        correlation, *, issuer_reservation,
    ) -> bool:
        if order_id != self.issue_order_observation_id(
            provider_name, environment, account_ref, identity
        ):
            raise ValueError("order_observation_id_mismatch")
        existing = self.bindings.setdefault(order_id, correlation)
        if existing != correlation:
            raise ValueError("order_observation_binding_conflict")
        return True

    def resolve_order_observation(self, order_id, *, provider_name, environment, account_ref,
                                  scope_ref):
        correlation = self.bindings.get(order_id)
        if correlation is None:
            return None
        return correlation, Handle(self._mac("handle", correlation.reservation_ref))

    def bind_provider_order_reference(self, *args, **kwargs) -> bool:  # pragma: no cover
        raise AssertionError("not used by the read-only provider")

    def resolve_provider_order_reference(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("not used by the read-only provider")


class Clock:
    def __init__(self) -> None:
        self.monotonic = 100.0

    def __call__(self) -> float:
        return self.monotonic


class Trader:
    def stop_and_wait(self, *, timeout: float) -> CtpNativeStopReceipt:
        return CtpNativeStopReceipt(7, True, True, True, False, False)


def order_row(**changes):
    row = {
        "BrokerID": "broker-test",
        "InvestorID": "investor-test",
        "InstrumentID": "IF2612",
        "ExchangeID": "CFFEX",
        "TradingDay": _TRADING_DAY,
        "OrderSysID": "   SYS1001",
        "OrderLocalID": "LOCAL-1",
        "OrderRef": "000000000001",
        "FrontID": 3,
        "SessionID": 4,
        "Direction": "0",
        "CombOffsetFlag": "0",
        "VolumeTotalOriginal": 3,
        "VolumeTraded": 1,
        "OrderStatus": "1",
        "OrderPriceType": "2",
        "LimitPrice": 4001.0,
        "InsertDate": _TRADING_DAY,
        "InsertTime": "09:30:00",
    }
    row.update(changes)
    return row


def trade_row(**changes):
    row = {
        "BrokerID": "broker-test",
        "InvestorID": "investor-test",
        "InstrumentID": "IF2612",
        "ExchangeID": "CFFEX",
        "TradingDay": _TRADING_DAY,
        "TradeID": "   TRD7001",
        "OrderSysID": "   SYS1001",
        "OrderRef": "000000000001",
        "OrderLocalID": "LOCAL-1",
        "Direction": "0",
        "OffsetFlag": "0",
        "Volume": 1,
        "Price": 4000.5,
        "TradeDate": _TRADING_DAY,
        "TradeTime": "09:30:01",
    }
    row.update(changes)
    return row


class Feed:
    """A fake CtpRequestDataFuture answering source-sealed queries."""

    def __init__(self, clock: Clock, *, orders=(), trades=()) -> None:
        self._request_feed = None
        self._trader = None
        self._connected = False
        self.clock = clock
        self.rows = {"orders": list(orders), "trades": list(trades)}
        self.queries: list[str] = []
        self.request_id = 0
        self.fail: set[str] = set()
        self.tamper: set[str] = set()

    def connect(self) -> None:
        self._trader = Trader()
        self._connected = True

    def get_session_state(self):
        return {"read_only_ready": True, "auto_settlement_confirm": False}

    def get_query_session_scope(self):
        return _new_query_session_scope(
            issuer=self._trader,
            account_fingerprint=_FINGERPRINT,
            connection_generation=7,
            trading_day=_TRADING_DAY,
            broker_id="broker-test",
            investor_id="investor-test",
            read_only_ready=True,
            captured_at_utc=_COMPLETED,
            captured_monotonic=100.0,
            source_tags=_SOURCE_TAGS,
        )

    def query_orders_result(self, *, timeout):
        return self._query("orders", timeout)

    def query_trades_result(self, *, timeout):
        return self._query("trades", timeout)

    def _query(self, name, timeout):
        assert timeout > 0
        self.queries.append(name)
        if name in self.fail:
            raise RuntimeError("synthetic native query failure")
        self.request_id += 1
        rows = [dict(row) for row in self.rows[name]]
        now = self.clock.monotonic
        result = QueryResult(
            request_type=name,
            request_id=self.request_id,
            connection_generation=7,
            account_fingerprint=_FINGERPRINT,
            started_at_utc=_COMPLETED - timedelta(seconds=1),
            completed_at_utc=_COMPLETED,
            is_last_seen=True,
            error_code=0,
            error_message="",
            timed_out=False,
            complete=True,
            records=tuple(rows),
        )
        source = _new_query_source(
            issuer=self._trader,
            request_type=name,
            request_id=result.request_id,
            account_fingerprint=_FINGERPRINT,
            connection_generation=7,
            trading_day=_TRADING_DAY,
            broker_id="broker-test",
            investor_id="investor-test",
            started_at_utc=result.started_at_utc,
            completed_at_utc=result.completed_at_utc,
            started_monotonic=now - 0.5,
            completed_monotonic=now,
            records_sha256=_query_records_digest(result.records),
        )
        result = _attach_query_source(result, source)
        if name in self.tamper and rows:
            rows[0]["Volume" if name == "trades" else "VolumeTraded"] = 99
        return result


def provider(feed, clock, *, issuer=None, instruments=(_IF,), **kwargs):
    return CtpReadOnlyAccountSessionProvider(
        lambda: feed,
        monotonic_clock=clock,
        utc_clock=lambda: _COMPLETED + timedelta(seconds=5),
        execution_observation_issuer=issuer or HmacIssuer(),
        execution_instruments=instruments,
        execution_query_interval_seconds=kwargs.pop("interval", 0.0),
        **kwargs,
    )


def connected(feed, clock, generation="gen-1", **kwargs):
    built = provider(feed, clock, **kwargs)
    built.connect(account_key=_ACCOUNT, generation=generation, timeout=2.0)
    return built


def read(built, generation="gen-1", limit=100):
    return built.read_execution_observations(generation=generation, timeout=2.0, limit=limit)


def facts(observations):
    return [
        (execution_observation_fact_id(item), execution_observation_fact_digest(item))
        for item in observations
    ]


def error_code(call):
    with pytest.raises(CtpAccountSessionProviderError) as error:
        call()
    return error.value.code


# -- projection -------------------------------------------------------------------


def test_projects_orders_then_fills_with_opaque_ids_and_no_fee():
    clock = Clock()
    feed = Feed(clock, orders=[order_row()], trades=[trade_row()])
    built = connected(feed, clock)
    page = read(built)
    assert feed.queries == ["trades", "orders"]  # trades first: their orders exist
    assert [type(item) for item in page] == [
        AccountExecutionOrderObservation,
        AccountExecutionTradeObservation,
    ]
    order, trade = page[0].order, page[1].trade
    assert order.order_id.startswith("sdk-order-observation-v1-")
    assert (order.client_order_id, order.intent_binding) == (None, "unresolved")
    assert (order.status, order.quantity, order.filled_quantity) == ("partially_filled", "3", "1")
    assert (order.side, order.position_effect, order.position_side) == ("buy", "open", "long")
    assert order.instrument == _IF and order.limit_price == "4001"
    assert trade.trade_id.startswith("sdk-trade-observation-v1-")
    assert trade.order_id == order.order_id and trade.order_binding == "provider_order_identity"
    assert (trade.quantity, trade.price, trade.qty_unit) == ("1", "4000.5", "contract")
    # CTP trade rows carry no fee: unknown, never zero.
    assert (page[1].fee, page[1].fee_currency) == (None, None)
    assert "fee" not in page[1].to_dict()
    # No native identity reaches the neutral observation.
    encoded = json.dumps([item.to_dict() for item in page])
    for native in ("SYS1001", "TRD7001", "000000000001", "LOCAL-1"):
        assert native not in encoded
    assert built.execution_observation_counters == {}


def test_capability_is_absent_unless_configured_and_configuration_is_strict():
    clock = Clock()
    plain = CtpReadOnlyAccountSessionProvider(lambda: Feed(clock))
    assert plain.read_execution_observations is None
    assert plain.confirm_execution_observations is None
    assert not callable(getattr(plain, "read_execution_observations", None))
    configured = provider(Feed(clock), clock)
    assert callable(configured.read_execution_observations)
    assert callable(configured.confirm_execution_observations)
    bad = [
        {"execution_observation_issuer": HmacIssuer()},  # no instruments
        {"execution_instruments": (_IF,)},  # no issuer
        {"execution_observation_issuer": object(), "execution_instruments": (_IF,)},
        {
            "execution_observation_issuer": HmacIssuer(),
            "execution_instruments": (_IF,),
            "execution_order_resolver": lambda *_: None,  # resolver without scope ref
        },
        {
            "execution_observation_issuer": HmacIssuer(),
            "execution_instruments": (
                MarketInstrumentKey("okx", "OKX", "simnow", "future", "IF2612", "IF2612"),
            ),
        },
        {
            "execution_observation_issuer": HmacIssuer(),
            "execution_instruments": (
                MarketInstrumentKey("ctp", "CFFEX", "simnow", "future", "IF2612", "if2612"),
            ),
        },
        {"execution_observation_issuer": HmacIssuer(), "execution_instruments": (_IF, _IF)},
        {"execution_observation_issuer": HmacIssuer(), "execution_instruments": [_IF]},
        {
            "execution_observation_issuer": HmacIssuer(),
            "execution_instruments": (_IF,),
            "execution_query_interval_seconds": -1.0,
        },
    ]
    for kwargs in bad:
        assert error_code(
            lambda: CtpReadOnlyAccountSessionProvider(lambda: Feed(clock), **kwargs)
        ) == "INVALID_PROVIDER_CONFIGURATION"
    # An authorized instrument outside the account realm is refused at connect.
    foreign = provider(
        Feed(clock),
        clock,
        instruments=(MarketInstrumentKey("ctp", "CFFEX", "other", "future", "IF2612", "IF2612"),),
    )
    assert error_code(
        lambda: foreign.connect(account_key=_ACCOUNT, generation="gen-1", timeout=2.0)
    ) == "INVALID_PROVIDER_CONFIGURATION"


# -- duplicates, order, conflicts ------------------------------------------------------


def test_duplicated_and_reordered_rows_produce_the_same_facts():
    clock = Clock()
    orders = [order_row(), order_row(OrderSysID="      1002", OrderRef="000000000002")]
    trades = [
        trade_row(),
        trade_row(TradeID="      7002", OrderSysID="      1002", OrderRef="000000000002"),
    ]
    first = read(connected(Feed(clock, orders=orders, trades=trades), clock))
    shuffled = Feed(
        clock,
        orders=[orders[1], orders[0], orders[1]],
        trades=[trades[1], trades[0], trades[0]],
    )
    second_provider = connected(shuffled, clock)
    second = read(second_provider)
    assert sorted(facts(first)) == sorted(facts(second))
    assert len(second) == 4
    assert second_provider.execution_observation_counters == {"DUPLICATE_ROW_MERGED": 2}


def test_conflicting_rows_of_one_identity_are_refused_not_merged():
    clock = Clock()
    feed = Feed(
        clock,
        orders=[order_row()],
        trades=[trade_row(), trade_row(Price=4100.0)],  # same TradeID, other price
    )
    built = connected(feed, clock)
    page = read(built)
    assert [type(item) for item in page] == [AccountExecutionOrderObservation]
    assert built.execution_observation_counters == {"CONFLICTING_DUPLICATE_ROW": 2}
    # A later poll does not count the same refused rows again.
    built.confirm_execution_observations(generation="gen-1", count=1)
    assert read(built) == ()
    assert built.execution_observation_counters == {"CONFLICTING_DUPLICATE_ROW": 2}


def test_a_changed_fact_under_a_published_id_is_refused():
    clock = Clock()
    feed = Feed(clock, orders=[order_row()], trades=[trade_row()])
    built = connected(feed, clock)
    page = read(built)
    built.confirm_execution_observations(generation="gen-1", count=len(page))
    feed.rows["trades"] = [trade_row(Volume=2)]  # same trade id, other content
    assert read(built) == ()
    assert built.execution_observation_counters == {"FACT_CONFLICT": 1}


def test_order_progress_is_published_but_fills_are_never_derived_from_it():
    clock = Clock()
    feed = Feed(clock, orders=[order_row()], trades=[trade_row()])
    built = connected(feed, clock)
    built.confirm_execution_observations(generation="gen-1", count=len(read(built)))
    # The order now reports 3 filled, but the trade query still has one fill.
    feed.rows["orders"] = [order_row(VolumeTraded=3, OrderStatus="0")]
    page = read(built)
    assert [type(item) for item in page] == [AccountExecutionOrderObservation]
    assert (page[0].order.status, page[0].order.filled_quantity) == ("filled", "3")
    built.confirm_execution_observations(generation="gen-1", count=1)
    feed.rows["trades"] = [trade_row(), trade_row(TradeID="      7002", Volume=2)]
    page = read(built)
    assert [type(item) for item in page] == [AccountExecutionTradeObservation]
    assert page[0].trade.quantity == "2"


def test_an_order_state_regression_is_refused():
    clock = Clock()
    feed = Feed(clock, orders=[order_row(VolumeTraded=2)], trades=[])
    built = connected(feed, clock)
    built.confirm_execution_observations(generation="gen-1", count=len(read(built)))
    feed.rows["orders"] = [order_row(VolumeTraded=1)]
    assert read(built) == ()
    feed.rows["orders"] = [order_row(VolumeTraded=0, OrderStatus="5")]
    assert read(built) == ()
    assert built.execution_observation_counters == {"ORDER_STATE_REGRESSED": 2}


def test_a_fill_waits_until_its_order_observation_is_published():
    clock = Clock()
    feed = Feed(
        clock,
        orders=[order_row(), order_row(VolumeTraded=2)],  # conflicting order rows
        trades=[trade_row()],
    )
    built = connected(feed, clock)
    assert read(built) == ()
    assert built.execution_observation_counters == {
        "CONFLICTING_DUPLICATE_ROW": 2,
        "TRADE_ORDER_UNOBSERVED": 1,
    }


def test_unacknowledged_orders_are_not_published():
    clock = Clock()
    feed = Feed(clock, orders=[order_row(OrderStatus="a", VolumeTraded=0)], trades=[])
    built = connected(feed, clock)
    assert read(built) == ()
    assert built.execution_observation_counters == {"ORDER_STATE_UNCONFIRMED": 1}


# -- account / instrument / day scope ----------------------------------------------------


def test_foreign_account_instrument_and_day_rows_are_refused_with_counts():
    clock = Clock()
    feed = Feed(
        clock,
        orders=[
            order_row(),
            order_row(OrderSysID="      2001", InvestorID="someone-else"),
            order_row(OrderSysID="      2002", InstrumentID="IC2612"),
            order_row(OrderSysID="      2003", TradingDay="20261016"),
        ],
        trades=[
            trade_row(),
            trade_row(TradeID="      8001", BrokerID="other-broker"),
            trade_row(TradeID="      8002", ExchangeID="SHFE", InstrumentID="rb2610"),
        ],
    )
    built = connected(feed, clock)
    page = read(built)
    assert len(page) == 2  # the authorized order and its fill
    expected = {
        "FOREIGN_ACCOUNT_ROW": 2,
        "FOREIGN_INSTRUMENT_ROW": 2,
        "TRADING_DAY_MISMATCH": 1,
    }
    assert built.execution_observation_counters == expected
    built.confirm_execution_observations(generation="gen-1", count=2)
    assert read(built) == ()
    assert built.execution_observation_counters == expected  # counted once per row


# -- queue, generations, reconnect, restart --------------------------------------------------


def test_partial_confirm_redelivers_the_remainder_identically():
    clock = Clock()
    feed = Feed(
        clock,
        orders=[order_row(), order_row(OrderSysID="      1002", OrderRef="000000000002")],
        trades=[trade_row()],
    )
    built = connected(feed, clock)
    first = read(built, limit=2)
    assert len(first) == 2
    again = read(built, limit=2)
    assert again == first and all(a is b for a, b in zip(again, first))
    assert feed.queries.count("orders") == 1  # pending items are redelivered, not re-read
    assert error_code(
        lambda: built.confirm_execution_observations(generation="gen-1", count=3)
    ) == "EXECUTION_CONFIRM_INVALID"
    built.confirm_execution_observations(generation="gen-1", count=1)
    rest = read(built, limit=10)
    assert rest[0] is first[1] and len(rest) == 2
    built.confirm_execution_observations(generation="gen-1", count=2)
    assert read(built) == ()
    for bad in (True, -1, "1"):
        assert error_code(
            lambda: built.confirm_execution_observations(generation="gen-1", count=bad)
        ) == "EXECUTION_CONFIRM_INVALID"
    for bad in (0, 10_001, True):
        assert error_code(lambda: read(built, limit=bad)) == "EXECUTION_LIMIT_INVALID"


def test_stale_generations_are_refused_and_each_generation_has_a_fresh_queue():
    clock = Clock()
    feed = Feed(clock, orders=[order_row()], trades=[trade_row()])
    built = connected(feed, clock)
    assert error_code(lambda: read(built, "gen-0")) == "EXECUTION_GENERATION_STALE"
    first = read(built)
    assert error_code(
        lambda: built.confirm_execution_observations(generation="gen-0", count=1)
    ) == "EXECUTION_GENERATION_STALE"
    assert built.close() is True
    assert error_code(lambda: read(built)) == "SESSION_NOT_CONNECTED"
    built.connect(account_key=_ACCOUNT, generation="gen-2", timeout=2.0)
    assert error_code(lambda: read(built, "gen-1")) == "EXECUTION_GENERATION_STALE"
    # Reconnect: the unconfirmed facts are re-derived with the same ids.
    redelivered = read(built, "gen-2")
    assert facts(redelivered) == facts(first)
    assert all(a is not b for a, b in zip(redelivered, first))


def test_restart_with_the_same_issuer_secret_gives_identical_fact_ids():
    clock = Clock()
    rows = {"orders": [order_row()], "trades": [trade_row()]}
    before = read(connected(Feed(clock, **rows), clock, issuer=HmacIssuer(_SECRET)))
    after = read(connected(Feed(clock, **rows), clock, issuer=HmacIssuer(_SECRET)))
    assert facts(before) == facts(after)
    other = read(connected(Feed(clock, **rows), clock, issuer=HmacIssuer(b"another-secret")))
    assert {fact for fact, _ in facts(other)}.isdisjoint({fact for fact, _ in facts(before)})


def test_query_interval_bounds_native_queries():
    clock = Clock()
    feed = Feed(clock, orders=[order_row()], trades=[])
    built = connected(feed, clock, interval=1.0)
    built.confirm_execution_observations(generation="gen-1", count=len(read(built)))
    assert read(built) == () and feed.queries == ["trades", "orders"]
    clock.monotonic += 1.0
    assert read(built) == () and feed.queries == ["trades", "orders"] * 2


def test_failed_or_untrusted_queries_leave_the_queue_unchanged():
    clock = Clock()
    feed = Feed(clock, orders=[order_row()], trades=[trade_row()])
    built = connected(feed, clock)
    feed.fail.add("orders")
    assert error_code(lambda: read(built)) == "QUERY_FAILED"
    feed.fail.clear()
    feed.tamper.add("trades")
    assert error_code(lambda: read(built)) == "QUERY_SOURCE_UNTRUSTED"
    feed.tamper.clear()
    clock.monotonic = 1000.0  # query evidence older than its trusted TTL
    original = feed._query

    def stale(name, timeout):
        clock.monotonic = 1000.0
        result = original(name, timeout)
        clock.monotonic = 1100.0
        return result

    feed._query = stale
    assert error_code(lambda: read(built)) in {"QUERY_BUDGET_EXCEEDED", "QUERY_SOURCE_UNTRUSTED"}
    feed._query = original
    clock.monotonic = 1200.0  # monotonic time only moves forward
    assert len(read(built)) == 2


def test_oversized_query_results_fail_closed(monkeypatch):
    clock = Clock()
    feed = Feed(clock, orders=[order_row()], trades=[trade_row(), trade_row(TradeID="x")])
    built = connected(feed, clock)
    monkeypatch.setattr(provider_module, "_MAX_EXECUTION_QUERY_ROWS", 1)
    assert error_code(lambda: read(built)) == "EXECUTION_QUERY_TOO_LARGE"
    monkeypatch.setattr(provider_module, "_MAX_EXECUTION_QUERY_ROWS", 8192)
    monkeypatch.setattr(provider_module, "_MAX_EXECUTION_TRACKED", 2)
    assert error_code(lambda: read(built)) == "EXECUTION_CAPACITY_EXCEEDED"


def test_reads_are_confined_to_the_owner_thread():
    clock = Clock()
    built = connected(Feed(clock, orders=[order_row()]), clock)
    codes: list[str] = []

    def other_thread():
        try:
            read(built)
        except CtpAccountSessionProviderError as error:
            codes.append(error.code)

    worker = threading.Thread(target=other_thread)
    worker.start()
    worker.join()
    assert codes == ["OWNER_THREAD_REQUIRED"]


# -- attribution -------------------------------------------------------------------------


def test_client_order_id_is_verified_only_through_an_issuer_validated_correlation():
    clock = Clock()
    issuer = HmacIssuer()
    correlation, handle = issuer.issue_order_correlation("CTP___FUTURE", "IF2612", "sa1b2c3d4-1")
    forged = OrderCorrelation("CTP___FUTURE", "IF2612", "sforged-1", "res-forged")
    references = {
        "000000000001": (correlation, handle),
        "000000000002": (forged, Handle("not-the-issuer-handle")),
    }
    feed = Feed(
        clock,
        orders=[
            order_row(),
            order_row(OrderSysID="      1002", OrderRef="000000000002"),
            order_row(OrderSysID="      1003", OrderRef="000000000003"),
        ],
        trades=[],
    )
    built = connected(
        feed,
        clock,
        issuer=issuer,
        execution_order_resolver=lambda order_ref, _instrument: references.get(order_ref),
        execution_scope_ref="scope:test",
    )
    verified, forged_order, unknown = (item.order for item in read(built))
    assert (verified.intent_binding, verified.client_order_id) == ("verified", "sa1b2c3d4-1")
    assert issuer.bindings[verified.order_id] == correlation
    assert (forged_order.intent_binding, forged_order.client_order_id) == ("unresolved", None)
    assert (unknown.intent_binding, unknown.client_order_id) == ("unresolved", None)
    assert built.execution_observation_counters == {"ORDER_ATTRIBUTION_REFUSED": 1}
