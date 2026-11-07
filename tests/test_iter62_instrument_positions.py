"""Iteration 62 (D62-11a): whole-account vs per-instrument CTP position queries.

UNIT/FAKE evidence only: a real ``TraderClient`` with a fake native API, and the
direct write provider over a fake Trader session.  No network, no account.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bt_api_base.direct_write_protocol import (
    DirectAccountFacts,
    DirectPositionFact,
    DirectWriteError,
)
from bt_api_ctp.ctp.client import TraderClient, _TraderSpi
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
from bt_api_ctp.instrument_position_facts import InstrumentPositionFacts
from bt_api_ctp.query import QueryResult, _attach_query_source, _new_query_source

NOW = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# native filter on the real TraderClient (fake native API)
# ---------------------------------------------------------------------------
def _read_client() -> TraderClient:
    client = TraderClient("tcp://offline", "9999", "offline", "fixture")
    with client._query_state_lock:
        client._connected = True
        client._authentication_state = "authenticated"
        client._login_state = "logging_in"
        client._trading_day = ""
        client._connection_generation = 1
        request_id = client._next_request_id()
        client._login_request_id = request_id
        client._login_connection_generation = client._connection_generation
        native_api = client._api
        spi = _TraderSpi(client, native_api)
        client._spi = spi
    client._session_native_api = native_api
    client._session_native_front = client._bound_front
    client._query_interval = 0
    spi.OnRspUserLogin(
        SimpleNamespace(
            BrokerID=client._bound_broker_id,
            UserID=client._bound_user_id,
            TradingDay="20261106",
            FrontID=7,
            SessionID=19,
            MaxOrderRef="0",
        ),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        request_id,
        True,
    )
    assert client.is_read_only_ready is True
    return client


def _position_api(client, seen, rows):
    spi = _TraderSpi(client)

    def submit(field, request_id):
        seen.append({name: getattr(field, name) for name in (
            "BrokerID", "InvestorID", "InstrumentID", "ExchangeID"
        )})
        for index, row in enumerate(rows):
            spi.OnRspQryInvestorPosition(row, None, request_id, index == len(rows) - 1)
        if not rows:
            spi.OnRspQryInvestorPosition(None, None, request_id, True)
        return 0

    return SimpleNamespace(ReqQryInvestorPosition=submit)


def test_trader_filtered_position_query_writes_filter_into_native_request():
    client = _read_client()
    seen: list[dict] = []
    client._api = _position_api(
        client, seen, [SimpleNamespace(InstrumentID="rb2701", ExchangeID="SHFE", Position=2)]
    )
    result = client.query_positions_result(
        timeout=0.05, instrument_id="rb2701", exchange_id="SHFE"
    )
    assert result.complete is True
    assert seen == [
        {"BrokerID": "9999", "InvestorID": "offline", "InstrumentID": "rb2701", "ExchangeID": "SHFE"}
    ]
    filters = dict(result.query_source.request_filters)
    assert filters["InstrumentID"] == "rb2701"
    assert filters["ExchangeID"] == "SHFE"


def test_trader_unfiltered_position_query_is_unchanged():
    client = _read_client()
    seen: list[dict] = []
    client._api = _position_api(client, seen, [])
    result = client.query_positions_result(timeout=0.05)
    assert result.complete is True and result.records == ()
    assert seen == [
        {"BrokerID": "9999", "InvestorID": "offline", "InstrumentID": "", "ExchangeID": ""}
    ]
    # Request evidence of the whole-account call names only the account fields.
    assert dict(result.query_source.request_filters) == {
        "BrokerID": "9999",
        "InvestorID": "offline",
    }


def test_trader_position_filter_type_is_checked_before_the_native_call():
    client = _read_client()
    seen: list[dict] = []
    client._api = _position_api(client, seen, [])
    result = client.query_positions_result(timeout=0.05, instrument_id=2701)
    assert result.complete is False and result.unsupported is True
    assert result.error_message == "native_position_filter_invalid"
    assert seen == []


def test_feed_forwards_position_filters_only_when_given():
    calls = []

    class Trader:
        def query_positions_result(self, **kwargs):
            calls.append(kwargs)
            return "result"

    feed = _feed()
    feed._trader = Trader()
    feed._ensure_connected = lambda: None
    assert feed.query_positions_result(timeout=3) == "result"
    assert feed.query_positions_result(timeout=3, instrument_id="rb2701") == "result"
    assert calls == [
        {"timeout": 3},
        {"timeout": 3, "instrument_id": "rb2701", "exchange_id": ""},
    ]


# ---------------------------------------------------------------------------
# provider: instrument-scoped result
# ---------------------------------------------------------------------------
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
    def __init__(self):
        self.connection_generation = 4
        self.trading_day = "20261106"
        self.listeners = []

    def add_execution_listener(self, listener):
        self.listeners.append(listener)

    def remove_execution_listener(self, listener):
        self.listeners.remove(listener)

    def get_session_state(self):
        return {
            "front_id": 3,
            "session_id": 99,
            "trading_day": self.trading_day,
            "connection_generation": self.connection_generation,
        }


def _row(symbol, direction="2", position=1, today=0, exchange="SHFE"):
    return {
        "InstrumentID": symbol,
        "ExchangeID": exchange,
        "PosiDirection": direction,
        "Position": position,
        "TodayPosition": today,
    }


def _result(rows, *, complete=True, generation=4, timed_out=False, error_code=None, filters=None):
    result = QueryResult(
        request_type="positions",
        request_id=11,
        connection_generation=generation,
        account_fingerprint="acct_test",
        started_at_utc=NOW,
        completed_at_utc=NOW,
        is_last_seen=complete,
        error_code=error_code,
        error_message="" if complete else "query_timeout",
        timed_out=timed_out,
        complete=complete,
        records=tuple(rows),
        completion_origin="query_response" if complete else "timeout",
    )
    if filters is not None:
        source = _new_query_source(
            issuer=object(),
            request_type="positions",
            request_id=11,
            account_fingerprint="acct_test",
            connection_generation=generation,
            trading_day="20261106",
            broker_id="broker-test",
            investor_id="investor-test",
            request_intent_filters=tuple(sorted(filters.items())),
            request_filters=tuple(sorted(filters.items())),
            started_at_utc=NOW,
            completed_at_utc=NOW,
            started_monotonic=1.0,
            completed_monotonic=1.5,
            records_sha256=None,
        )
        _attach_query_source(result, source)
    return result


def _provider(answer):
    """Provider whose feed answers position queries with ``answer(kwargs)``."""

    feed = _feed()
    trader = FakeTrader()
    feed._trader = trader
    calls: list[dict] = []

    def query_positions_result(**kwargs):
        calls.append(kwargs)
        value = answer(kwargs)
        if isinstance(value, Exception):
            raise value
        return value

    feed.query_positions_result = query_positions_result
    return CtpDirectWriteProvider(feed), trader, calls


def test_instrument_positions_pass_the_filter_and_keep_instrument_scope():
    provider, _trader, calls = _provider(
        lambda kw: _result(
            [_row("rb2701", "2", 3, 1), _row("rb2701", "3", 0, 0)],
            filters={"InstrumentID": "rb2701", "ExchangeID": "SHFE"},
        )
    )
    facts = provider.instrument_positions("rb2701", "SHFE", timeout=2.0)
    assert calls == [{"timeout": 2.0, "instrument_id": "rb2701", "exchange_id": "SHFE"}]
    assert isinstance(facts, InstrumentPositionFacts)
    assert not isinstance(facts, DirectAccountFacts)
    assert not hasattr(facts, "active_orders")
    assert facts.scope == "instrument" and facts.proves_account_flat is False
    assert facts.complete is True and facts.blockers == ()
    assert facts.positions == (
        DirectPositionFact(
            symbol="rb2701", venue="SHFE", side="long", total=3.0, today=1.0, yesterday=2.0
        ),
    )
    assert facts.instrument_flat is False
    assert facts.native_filter_verified is True
    assert facts.filter_ignored is False and facts.dropped_rows == 0
    assert facts.trading_day == "20261106" and facts.session_generation == 4


def test_rows_of_other_instruments_are_dropped_and_reported_when_filter_ignored():
    provider, _trader, _calls = _provider(
        lambda kw: _result(
            [_row("ag2512", "2", 5, 5), _row("rb2701", "3", 2, 2), _row("cu2701", "2", 1, 0)]
        )
    )
    facts = provider.instrument_positions("rb2701")
    assert facts.complete is True
    assert [(p.symbol, p.side, p.total) for p in facts.positions] == [("rb2701", "short", 2.0)]
    assert facts.filter_ignored is True
    assert facts.dropped_rows == 2
    assert facts.dropped_instruments == ("ag2512", "cu2701")
    assert facts.native_filter_verified is None  # no request evidence attached


def test_only_foreign_rows_prove_the_instrument_not_the_account_flat():
    provider, _trader, _calls = _provider(lambda kw: _result([_row("ag2512", "2", 5, 5)]))
    facts = provider.instrument_positions("rb2701")
    assert facts.complete is True and facts.positions == ()
    assert facts.instrument_flat is True
    assert facts.proves_account_flat is False
    assert facts.filter_ignored is True and facts.dropped_instruments == ("ag2512",)


def test_empty_filtered_result_never_proves_the_account_flat():
    provider, _trader, _calls = _provider(lambda kw: _result([]))
    facts = provider.instrument_positions("rb2701", "SHFE")
    assert facts.complete is True and facts.positions == ()
    assert facts.instrument_flat is True
    assert facts.proves_account_flat is False and facts.scope == "instrument"
    with pytest.raises(ValueError):
        InstrumentPositionFacts(
            exchange_name="CTP___FUTURE",
            symbol="rb2701",
            venue=None,
            trading_day="20261106",
            session_generation=4,
            observed_at_utc="2026-11-06T01:00:00Z",
            positions=(),
            complete=True,
            proves_account_flat=True,
        )
    with pytest.raises(ValueError):
        InstrumentPositionFacts(
            exchange_name="CTP___FUTURE",
            symbol="rb2701",
            venue=None,
            trading_day="20261106",
            session_generation=4,
            observed_at_utc="2026-11-06T01:00:00Z",
            positions=(),
            complete=True,
            scope="account",
        )


def test_instrument_facts_refuse_positions_of_another_instrument_or_venue():
    common = dict(
        exchange_name="CTP___FUTURE",
        symbol="rb2701",
        trading_day="20261106",
        session_generation=4,
        observed_at_utc="2026-11-06T01:00:00Z",
        complete=True,
    )
    with pytest.raises(ValueError):
        InstrumentPositionFacts(
            venue=None,
            positions=(DirectPositionFact(symbol="ag2512", venue="SHFE", side="long", total=1.0),),
            **common,
        )
    with pytest.raises(ValueError):
        InstrumentPositionFacts(
            venue="SHFE",
            positions=(DirectPositionFact(symbol="rb2701", venue="INE", side="long", total=1.0),),
            **common,
        )


def test_wrong_venue_rows_are_dropped_and_missing_venue_blocks():
    provider, _trader, _calls = _provider(
        lambda kw: _result([_row("rb2701", "2", 4, 0, exchange="INE"), _row("rb2701", "2", 1, 1)])
    )
    facts = provider.instrument_positions("rb2701", "SHFE")
    assert facts.complete is True
    assert [(p.venue, p.total) for p in facts.positions] == [("SHFE", 1.0)]
    assert facts.filter_ignored is True and facts.dropped_instruments == ("INE.rb2701",)

    provider, _trader, _calls = _provider(
        lambda kw: _result([_row("rb2701", "2", 4, 0, exchange="")])
    )
    facts = provider.instrument_positions("rb2701", "SHFE")
    assert facts.complete is False and facts.positions is None
    assert "positions_row_venue_missing" in facts.blockers
    assert facts.instrument_flat is None


@pytest.mark.parametrize(
    "result",
    [
        _result([], complete=False, timed_out=True),
        _result([_row("rb2701", "2", 1, 1)], complete=False),
        _result([], error_code=-1),
        _result([], generation=5),  # another connection generation
    ],
)
def test_incomplete_timeout_error_or_stale_results_block(result):
    provider, _trader, _calls = _provider(lambda kw: result)
    facts = provider.instrument_positions("rb2701")
    assert facts.complete is False and facts.positions is None
    assert facts.instrument_flat is None
    assert facts.blockers and facts.blockers[0].startswith("positions_query_incomplete[")


def test_query_exception_blocks():
    provider, _trader, _calls = _provider(lambda kw: TypeError("old feed"))
    facts = provider.instrument_positions("rb2701")
    assert facts.complete is False
    assert facts.blockers == ("positions_query_failed[TypeError]",)


def test_request_evidence_for_another_instrument_blocks():
    provider, _trader, _calls = _provider(
        lambda kw: _result([], filters={"InstrumentID": "ag2512"})
    )
    facts = provider.instrument_positions("rb2701")
    assert facts.complete is False
    assert facts.blockers == ("positions_filter_mismatch",)


def test_request_evidence_without_the_filter_is_reported_not_trusted():
    provider, _trader, _calls = _provider(
        lambda kw: _result([_row("rb2701", "2", 1, 1)], filters={"BrokerID": "b"})
    )
    facts = provider.instrument_positions("rb2701")
    assert facts.complete is True and facts.native_filter_verified is False


def test_unreadable_rows_of_the_instrument_block_the_instrument_scope():
    bad_direction = _row("rb2701", "9", 2, 0)
    provider, _trader, _calls = _provider(lambda kw: _result([bad_direction]))
    facts = provider.instrument_positions("rb2701")
    assert facts.complete is False
    assert facts.blockers == ("position_row_direction_unknown",)

    bad_quantity = dict(_row("rb2701", "2", 0, 0), Position=None)
    provider, _trader, _calls = _provider(lambda kw: _result([bad_quantity]))
    facts = provider.instrument_positions("rb2701")
    assert facts.complete is False
    assert facts.blockers == ("position_row_quantity_unreadable",)


def test_session_change_during_the_query_blocks():
    def answer(kwargs):
        trader.connection_generation = 5  # reconnect while the query ran
        return _result([], generation=4)

    provider, trader, _calls = _provider(answer)
    facts = provider.instrument_positions("rb2701")
    assert facts.complete is False
    assert "positions_session_changed" in facts.blockers


@pytest.mark.parametrize("symbol", ["", " rb2701", "rb2701\n", 2701, "合约", "x" * 81])
def test_invalid_symbol_is_refused_before_any_query(symbol):
    provider, _trader, calls = _provider(lambda kw: _result([]))
    with pytest.raises(DirectWriteError) as info:
        provider.instrument_positions(symbol)
    assert info.value.code == "ctp_instrument_positions_symbol_invalid"
    assert calls == []


@pytest.mark.parametrize("exchange", ["SH FE", "SHFE.", "TOOLONGEXCH", 5])
def test_invalid_exchange_is_refused_before_any_query(exchange):
    provider, _trader, calls = _provider(lambda kw: _result([]))
    with pytest.raises(DirectWriteError) as info:
        provider.instrument_positions("rb2701", exchange)
    assert info.value.code == "ctp_instrument_positions_exchange_invalid"
    assert calls == []


def test_account_facts_block_on_rows_that_may_hold_an_unprojectable_position():
    """An unknown direction or unreadable quantity never yields a flat account."""
    bad_direction = _row("rb2701", "9", 2, 0)
    provider, _trader, _calls = _provider(
        lambda kw: _result([_row("ag2512", "2", 5, 5), bad_direction])
    )
    facts = provider.account_facts(positions=True, active_orders=False, balance=False)
    assert facts.complete is False and facts.positions is None
    assert facts.blockers == ("positions_position_row_direction_unknown",)

    bad_quantity = dict(_row("rb2701", "2", 0, 0), Position=None)
    provider, _trader, _calls = _provider(lambda kw: _result([bad_quantity]))
    facts = provider.account_facts(positions=True, active_orders=False, balance=False)
    assert facts.complete is False and facts.positions is None
    assert facts.blockers == ("positions_position_row_quantity_unreadable",)


def test_account_facts_keep_the_unfiltered_whole_account_contract():
    provider, _trader, calls = _provider(
        lambda kw: _result([_row("ag2512", "2", 5, 5), _row("rb2701", "3", 2, 0)])
    )
    facts = provider.account_facts(positions=True, active_orders=False, balance=False)
    assert calls == [{"timeout": 5.0}]  # no instrument filter on the whole-account read
    assert isinstance(facts, DirectAccountFacts) and facts.complete is True
    assert [(p.symbol, p.side, p.total) for p in facts.positions] == [
        ("ag2512", "long", 5.0),
        ("rb2701", "short", 2.0),
    ]


# ---------------------------------------------------------------------------
# account facts: one session from the first query to the last
# ---------------------------------------------------------------------------
def _order_row(ref="000000000041", status="3"):
    return {
        "InstrumentID": "rb2701",
        "ExchangeID": "SHFE",
        "OrderRef": ref,
        "FrontID": 3,
        "SessionID": 99,
        "OrderSysID": "  1234",
        "OrderSubmitStatus": "3",
        "OrderStatus": status,
        "Direction": "0",
        "CombOffsetFlag": "0",
        "VolumeTotalOriginal": 1,
        "VolumeTraded": 0,
        "VolumeTotal": 1,
        "LimitPrice": 3000.0,
        "TradingDay": "20261106",
    }


def _account_row():
    return {"CurrencyID": "CNY", "Balance": 1000.0, "Available": 900.0, "CurrMargin": 100.0}


def _full_facts_provider(change_on_last_query):
    """Every section answers a complete result of generation 4; the last query
    (funds) runs ``change_on_last_query(trader, feed)`` before answering."""

    provider, trader, _calls = _provider(lambda kw: _result([_row("ag2512", "2", 5, 5)]))
    feed = provider._feed
    feed.query_orders_result = lambda **kw: _result([_order_row()])
    feed.query_trades_result = lambda **kw: _result([])

    def query_account_result(**kwargs):
        change_on_last_query(trader, feed)
        return _result([_account_row()], generation=4)

    feed.query_account_result = query_account_result
    return provider


def _read_all(provider):
    return provider.account_facts(
        positions=True, active_orders=True, trades=True, balance=True, all_orders=True
    )


def test_account_facts_of_one_unchanged_session_are_complete():
    facts = _read_all(_full_facts_provider(lambda trader, feed: None))
    assert facts.complete is True and facts.blockers == ()
    assert facts.session_generation == 4 and facts.trading_day == "20261106"
    assert len(facts.positions) == 1 and len(facts.active_orders) == 1
    assert len(facts.orders) == 1 and facts.trades == ()
    assert facts.balance is not None and facts.balance.available == 900.0


def _reconnect(trader, feed):
    trader.connection_generation = 5


def _day_switch(trader, feed):
    trader.trading_day = "20261109"


def _trader_replaced(trader, feed):
    feed._trader = FakeTrader()  # same session state, another native client


def _session_lost(trader, feed):
    trader.connection_generation = None


@pytest.mark.parametrize(
    "change", [_reconnect, _day_switch, _trader_replaced, _session_lost],
    ids=["reconnect", "trading_day", "trader_replaced", "session_lost"],
)
def test_account_facts_read_across_a_session_change_are_incomplete(change):
    """Results labelled with the starting generation, but the session changed
    before the last query ended: the sections may mix two sessions."""

    facts = _read_all(_full_facts_provider(change))
    assert facts.complete is False
    assert "session_changed_during_query" in facts.blockers
    assert facts.positions is None and facts.active_orders is None
    assert facts.orders is None and facts.trades is None and facts.balance is None
    assert facts.session_generation == 4 and facts.trading_day == "20261106"
