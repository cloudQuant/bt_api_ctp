"""Iteration 62 (D62-25, N-1/2, 62.03): CTP trading-permission facts.

UNIT/FAKE evidence only: a real ``TraderClient`` with a fake native API for the
two new read-only queries (``ReqQryInvestor``, ``ReqQryTradingCode``), and the
direct write provider over a fake Trader session whose feed answers every
query.  No network, no account, no SimNow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bt_api_base.direct_write_protocol import DirectWriteError
from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.ctp.client import TraderClient, _TraderSpi
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
from bt_api_ctp.query import QueryResult, _attach_query_source, _new_query_source
from bt_api_ctp.trading_permission_facts import (
    KNOWN_NEGATIVE,
    KNOWN_POSITIVE,
    UNKNOWN,
    InstrumentPermissionFacts,
    PermissionFact,
    TradingPermissionFacts,
)

NOW = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc)
PERSONAL_FIELDS = (
    "InvestorName",
    "IdentifiedCardType",
    "IdentifiedCardNo",
    "Telephone",
    "Address",
    "Mobile",
    "OpenDate",
)


# ---------------------------------------------------------------------------
# native queries on the real TraderClient (fake native API)
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


def test_investor_query_is_filtered_to_the_bound_investor_and_drops_personal_fields():
    client = _read_client()
    spi = _TraderSpi(client)
    seen = []

    def submit(field, request_id):
        seen.append({name: getattr(field, name) for name in ("BrokerID", "InvestorID")})
        row = SimpleNamespace(
            BrokerID="9999",
            InvestorID="offline",
            IsActive=1,
            InvestorName="synthetic-name",
            IdentifiedCardType="1",
            IdentifiedCardNo="synthetic-card",
            Telephone="synthetic-phone",
            Address="synthetic-address",
            Mobile="synthetic-mobile",
            OpenDate="20200101",
        )
        spi.OnRspQryInvestor(row, None, request_id, True)
        return 0

    client._api = SimpleNamespace(ReqQryInvestor=submit)
    result = client.query_investor_result(timeout=0.05)
    assert result.complete is True and result.request_type == "investor"
    assert seen == [{"BrokerID": "9999", "InvestorID": "offline"}]
    (row,) = result.records
    assert row["IsActive"] == 1 and row["InvestorID"] == "offline"
    assert not any(name in row for name in PERSONAL_FIELDS)
    assert dict(result.query_source.request_filters) == {
        "BrokerID": "9999",
        "InvestorID": "offline",
    }
    counts = client.get_request_counts()
    assert counts["query_investor"] == 1
    assert all(counts[key] == 0 for key in ("settlement_confirm", "order_insert", "order_action"))


def test_trading_code_query_writes_the_exchange_filter_and_keeps_code_fields():
    client = _read_client()
    spi = _TraderSpi(client)
    seen = []

    def submit(field, request_id):
        seen.append(
            {name: getattr(field, name) for name in ("BrokerID", "InvestorID", "ExchangeID")}
        )
        spi.OnRspQryTradingCode(
            SimpleNamespace(
                BrokerID="9999",
                InvestorID="offline",
                ExchangeID="SHFE",
                ClientID="synthetic-client",
                IsActive=1,
                ClientIDType="1",
            ),
            None,
            request_id,
            True,
        )
        return 0

    client._api = SimpleNamespace(ReqQryTradingCode=submit)
    result = client.query_trading_codes_result(exchange_id="SHFE", timeout=0.05)
    assert result.complete is True and result.request_type == "trading_code"
    assert seen == [{"BrokerID": "9999", "InvestorID": "offline", "ExchangeID": "SHFE"}]
    assert dict(result.query_source.request_filters)["ExchangeID"] == "SHFE"
    assert result.records[0]["ClientIDType"] == "1"
    assert client.get_request_counts()["query_trading_code"] == 1


def test_trading_code_query_without_exchange_keeps_an_unfiltered_request():
    client = _read_client()
    spi = _TraderSpi(client)

    def submit(field, request_id):
        spi.OnRspQryTradingCode(None, None, request_id, True)
        return 0

    client._api = SimpleNamespace(ReqQryTradingCode=submit)
    result = client.query_trading_codes_result(timeout=0.05)
    assert result.complete is True and result.records == ()
    assert "ExchangeID" not in dict(result.query_source.request_filters)


def test_new_queries_fail_closed_without_native_support_or_with_bad_filters():
    client = _read_client()
    client._api = SimpleNamespace()
    for result in (
        client.query_investor_result(timeout=0.01),
        client.query_trading_codes_result(exchange_id="SHFE", timeout=0.01),
    ):
        assert result.complete is False and result.unsupported is True
    called = []
    client._api = SimpleNamespace(ReqQryTradingCode=lambda *args: called.append(args))
    result = client.query_trading_codes_result(exchange_id=5, timeout=0.01)
    assert result.complete is False and result.error_message == "native_trading_code_filter_invalid"
    assert called == []


def test_new_query_errors_and_stale_callbacks_never_complete():
    client = _read_client()
    spi = _TraderSpi(client)

    def denied(field, request_id):
        spi.OnRspQryInvestor(None, SimpleNamespace(ErrorID=7, ErrorMsg="denied"), request_id, True)
        return 0

    client._api = SimpleNamespace(ReqQryInvestor=denied)
    result = client.query_investor_result(timeout=0.01)
    assert result.complete is False and result.error_code == 7

    client = _read_client()
    spi = _TraderSpi(client)

    def stale(field, request_id):
        spi._native_api = object()
        spi.OnRspQryTradingCode(SimpleNamespace(ExchangeID="SHFE"), None, request_id, True)
        return 0

    client._api = SimpleNamespace(ReqQryTradingCode=stale)
    result = client.query_trading_codes_result(exchange_id="SHFE", timeout=0.01)
    assert result.complete is False and result.timed_out is True and result.records == ()


def test_new_query_kinds_map_to_their_callbacks_and_count_keys():
    assert client_module._ctp_query_callback_name("investor") == "OnRspQryInvestor"
    assert client_module._ctp_query_callback_name("trading_code") == "OnRspQryTradingCode"
    assert {"query_investor", "query_trading_code"} <= set(client_module.CTP_REQUEST_COUNT_KEYS)
    assert {"ReqQryInvestor", "ReqQryTradingCode"} <= client_module._CTP_INGRESS_SAFE_REQUEST_METHODS


@pytest.mark.parametrize(
    "public", ["query_investor_result", "query_trading_codes_result"]
)
def test_feed_forwards_the_new_queries_to_the_trader(public):
    feed = object.__new__(CtpRequestDataFuture)
    sentinel = object()
    calls = []
    feed._ensure_connected = lambda: calls.append("connected")
    feed._trader = SimpleNamespace(**{public: lambda **params: (calls.append(params), sentinel)[1]})
    assert getattr(feed, public)(timeout=2) is sentinel
    assert calls == ["connected", {"timeout": 2}]


# ---------------------------------------------------------------------------
# provider: trading-permission facts over a fake session
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


def _result(kind, rows, *, complete=True, generation=4, error_code=None, filters=None):
    result = QueryResult(
        request_type=kind,
        request_id=11,
        connection_generation=generation,
        account_fingerprint="acct_test",
        started_at_utc=NOW,
        completed_at_utc=NOW,
        is_last_seen=complete,
        error_code=error_code,
        error_message="" if complete else "query_timeout",
        timed_out=not complete,
        complete=complete,
        records=tuple(rows),
        completion_origin="query_response" if complete else "timeout",
    )
    if filters is not None:
        items = tuple(sorted(filters.items()))
        _attach_query_source(
            result,
            _new_query_source(
                issuer=object(),
                request_type=kind,
                request_id=11,
                account_fingerprint="acct_test",
                connection_generation=generation,
                trading_day="20261106",
                broker_id="broker-test",
                investor_id="investor-test",
                request_intent_filters=items,
                request_filters=items,
                started_at_utc=NOW,
                completed_at_utc=NOW,
                started_monotonic=1.0,
                completed_monotonic=1.5,
                records_sha256=None,
            ),
        )
    return result


def investor_row(active=1, investor="investor-test"):
    return {"BrokerID": "broker-test", "InvestorID": investor, "IsActive": active}


def code_row(exchange="SHFE", active=1, kind="1", client_id="synthetic-client"):
    return {
        "BrokerID": "broker-test",
        "InvestorID": "investor-test",
        "ExchangeID": exchange,
        "ClientID": client_id,
        "IsActive": active,
        "ClientIDType": kind,
    }


def instrument_row(symbol="rb2701", exchange="SHFE", trading=1, phase="1", product="rb"):
    return {
        "InstrumentID": symbol,
        "ExchangeID": exchange,
        "ProductID": product,
        "IsTrading": trading,
        "InstLifePhase": phase,
    }


def rate_row(instrument="rb", exchange=""):
    return {"InstrumentID": instrument, "ExchangeID": exchange}


class Venue:
    """Answers each permission query; tests replace individual answers."""

    def __init__(self):
        self.investor = lambda: _result("investor", [investor_row()])
        self.instruments = {
            "rb2701": [instrument_row()],
            "hc2701": [instrument_row("hc2701", product="hc")],
            "sc2701": [instrument_row("sc2701", exchange="INE", product="sc")],
        }
        self.codes = {"SHFE": [code_row()], "INE": [code_row("INE")]}
        self.commission = lambda symbol: _result("commission_rate", [rate_row(symbol[:2])])
        self.margin = lambda symbol: _result("margin_rate", [rate_row(symbol)])
        self.instrument_answer = None
        self.code_answer = None
        self.calls: list[tuple] = []

    def install(self, feed):
        def query_investor_result(**kwargs):
            self.calls.append(("investor", kwargs))
            return _value(self.investor())

        def query_instruments_result(**kwargs):
            self.calls.append(("instrument", kwargs))
            if self.instrument_answer is not None:
                return _value(self.instrument_answer(kwargs))
            symbol = kwargs["instrument_id"]
            return _result("instruments", self.instruments.get(symbol, []))

        def query_trading_codes_result(**kwargs):
            self.calls.append(("trading_code", kwargs))
            if self.code_answer is not None:
                return _value(self.code_answer(kwargs))
            venue = kwargs["exchange_id"]
            return _result(
                "trading_code", self.codes.get(venue, []), filters={"ExchangeID": venue}
            )

        def query_instrument_commission_rate_result(symbol, **kwargs):
            self.calls.append(("commission_rate", symbol, kwargs))
            return _value(self.commission(symbol))

        def query_instrument_margin_rate_result(symbol, **kwargs):
            self.calls.append(("margin_rate", symbol, kwargs))
            return _value(self.margin(symbol))

        feed.query_investor_result = query_investor_result
        feed.query_instruments_result = query_instruments_result
        feed.query_trading_codes_result = query_trading_codes_result
        feed.query_instrument_commission_rate_result = query_instrument_commission_rate_result
        feed.query_instrument_margin_rate_result = query_instrument_margin_rate_result


def _value(value):
    if isinstance(value, Exception):
        raise value
    return value


def _provider(venue=None):
    feed = _feed()
    trader = FakeTrader()
    feed._trader = trader
    venue = venue or Venue()
    venue.install(feed)
    return CtpDirectWriteProvider(feed), trader, venue


def _statuses(entry):
    return {fact.name: (fact.status, fact.reason) for fact in entry.facts}


def test_complete_positive_facts_report_no_negative_but_never_a_permission():
    provider, _trader, venue = _provider()
    facts = provider.trading_permission_facts(["rb2701", "hc2701"], timeout=2.0)
    assert isinstance(facts, TradingPermissionFacts)
    assert facts.scope == "instrument_permission" and facts.proves_permission is False
    assert facts.complete is True and facts.blockers == () and facts.session_consistent
    assert facts.trading_day == "20261106" and facts.session_generation == 4
    for entry in facts.instruments:
        assert entry.outcome == "no_negative_found"
        assert entry.proves_permission is False
        assert entry.venue == "SHFE" and entry.trading_code_kinds == ("speculation",)
        assert set(_statuses(entry).values()) == {
            (KNOWN_POSITIVE, "investor_active"),
            (KNOWN_POSITIVE, "instrument_trading"),
            (KNOWN_POSITIVE, "speculation_code_active"),
            (KNOWN_POSITIVE, "rate_row_present"),
        }
    # One investor read, one instrument read each, one trading-code read per
    # exchange, two rate reads per instrument; all filtered.
    kinds = [call[0] for call in venue.calls]
    assert kinds == [
        "investor",
        "instrument",
        "instrument",
        "trading_code",
        "commission_rate",
        "margin_rate",
        "commission_rate",
        "margin_rate",
    ]
    assert venue.calls[1] == ("instrument", {"instrument_id": "rb2701", "timeout": 2.0})
    assert venue.calls[3] == ("trading_code", {"exchange_id": "SHFE", "timeout": 2.0})
    assert venue.calls[4] == ("commission_rate", "rb2701", {"exchange_id": "SHFE", "timeout": 2.0})
    assert facts.native_queries == 8


def test_trading_codes_are_read_once_per_exchange():
    provider, _trader, venue = _provider()
    facts = provider.trading_permission_facts(["rb2701", "sc2701", "hc2701"], include_rates=False)
    assert facts.complete is True
    code_calls = [call[1]["exchange_id"] for call in venue.calls if call[0] == "trading_code"]
    assert code_calls == ["INE", "SHFE"]
    assert facts.for_symbol("sc2701").venue == "INE"


def test_investor_and_trading_code_alone_are_never_reported_as_permission():
    provider, _trader, venue = _provider()
    venue.instruments["rb2701"] = [instrument_row(phase="2")]  # paused: state unknown
    venue.commission = lambda symbol: _result("commission_rate", [])
    facts = provider.trading_permission_facts(["rb2701"])
    entry = facts.for_symbol("rb2701")
    statuses = _statuses(entry)
    assert statuses["investor_active"] == (KNOWN_POSITIVE, "investor_active")
    assert statuses["trading_code"] == (KNOWN_POSITIVE, "speculation_code_active")
    assert statuses["instrument_trading"] == (UNKNOWN, "instrument_paused")
    assert statuses["commission_rate"] == (UNKNOWN, "rate_row_absent")
    assert entry.outcome == "unknown" and entry.denial_reasons == ()
    assert facts.complete is True  # every query answered; the facts stay unknown
    assert facts.proves_permission is False


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda v: v.codes.__setitem__("SHFE", []), "trading_code_absent"),
        (lambda v: v.codes.__setitem__("SHFE", [code_row(active=0)]), "trading_code_inactive"),
        (
            lambda v: v.codes.__setitem__("SHFE", [code_row("DCE"), code_row("CZCE")]),
            "trading_code_absent",  # the venue ignored the filter: none for SHFE
        ),
        (
            lambda v: v.instruments.__setitem__("rb2701", [instrument_row(trading=0)]),
            "instrument_not_trading",
        ),
        (
            lambda v: v.instruments.__setitem__("rb2701", [instrument_row(phase="3")]),
            "instrument_expired",
        ),
        (
            lambda v: v.instruments.__setitem__("rb2701", [instrument_row(phase="0")]),
            "instrument_not_started",
        ),
        (
            lambda v: setattr(v, "investor", lambda: _result("investor", [investor_row(0)])),
            "investor_inactive",
        ),
    ],
)
def test_definitive_negatives_are_reported_with_finite_reasons(change, reason):
    venue = Venue()
    change(venue)
    provider, _trader, _venue = _provider(venue)
    facts = provider.trading_permission_facts(["rb2701"])
    entry = facts.for_symbol("rb2701")
    assert facts.complete is True
    assert entry.outcome == "known_negative"
    assert entry.denial_reasons == (reason,)


@pytest.mark.parametrize(
    "change,fact,reason",
    [
        (
            lambda v: v.codes.__setitem__("SHFE", [code_row(kind="3")]),
            "trading_code",
            "speculation_code_not_found",
        ),
        (
            lambda v: v.codes.__setitem__("SHFE", [code_row(exchange="")]),
            "trading_code",
            "trading_code_rows_unattributed",
        ),
        (
            lambda v: v.codes.__setitem__("SHFE", [code_row(active=None)]),
            "trading_code",
            "trading_code_state_unreadable",
        ),
        (
            lambda v: v.codes.__setitem__("SHFE", [code_row(client_id="")]),
            "trading_code",
            "trading_code_state_unreadable",
        ),
        (
            lambda v: v.instruments.__setitem__("rb2701", [instrument_row(trading=None, phase="")]),
            "instrument_trading",
            "instrument_state_unreadable",
        ),
        (
            lambda v: v.instruments.__setitem__(
                "rb2701", [instrument_row(), instrument_row(exchange="INE")]
            ),
            "instrument_trading",
            "instrument_rows_ambiguous",
        ),
        (
            lambda v: setattr(v, "investor", lambda: _result("investor", [])),
            "investor_active",
            "investor_row_absent",
        ),
        (
            lambda v: setattr(
                v,
                "investor",
                lambda: _result("investor", [investor_row(), investor_row(investor="other")]),
            ),
            "investor_active",
            "investor_rows_ambiguous",
        ),
        (
            lambda v: setattr(v, "margin", lambda symbol: _result("margin_rate", [rate_row("ag")])),
            "margin_rate",
            "rate_row_absent",
        ),
    ],
)
def test_unclear_rows_stay_unknown_and_are_never_denials(change, fact, reason):
    venue = Venue()
    change(venue)
    provider, _trader, _venue = _provider(venue)
    facts = provider.trading_permission_facts(["rb2701"])
    entry = facts.for_symbol("rb2701")
    assert _statuses(entry)[fact] == (UNKNOWN, reason)
    assert entry.outcome == "unknown" and entry.denial_reasons == ()


def test_investor_rows_of_another_investor_are_ignored_by_request_evidence():
    venue = Venue()
    venue.investor = lambda: _result(
        "investor",
        [investor_row(0, investor="other"), investor_row(1)],
        filters={"BrokerID": "broker-test", "InvestorID": "investor-test"},
    )
    provider, _trader, _venue = _provider(venue)
    entry = provider.trading_permission_facts(["rb2701"]).for_symbol("rb2701")
    assert _statuses(entry)["investor_active"] == (KNOWN_POSITIVE, "investor_active")


def test_an_unlisted_instrument_is_unknown_not_denied():
    provider, _trader, venue = _provider()
    facts = provider.trading_permission_facts(["zz9999"])
    entry = facts.for_symbol("zz9999")
    assert _statuses(entry)["instrument_trading"] == (UNKNOWN, "instrument_not_found")
    assert _statuses(entry)["trading_code"] == (UNKNOWN, "venue_unknown")
    assert entry.venue is None and entry.outcome == "unknown"
    assert not any(call[0] == "trading_code" for call in venue.calls)


def test_the_order_venue_given_by_the_caller_is_checked_and_a_mismatch_is_unknown():
    provider, _trader, venue = _provider()
    facts = provider.trading_permission_facts(["rb2701"], exchange_ids={"rb2701": "INE"})
    entry = facts.for_symbol("rb2701")
    assert entry.venue == "INE"
    assert _statuses(entry)["instrument_trading"] == (UNKNOWN, "instrument_venue_mismatch")
    assert [call[1]["exchange_id"] for call in venue.calls if call[0] == "trading_code"] == [
        "INE"
    ]


@pytest.mark.parametrize(
    "answer",
    [
        lambda kw: _result("trading_code", [], complete=False),
        lambda kw: _result("trading_code", [], error_code=-1),
        lambda kw: _result("trading_code", [], generation=5),  # stale connection
        lambda kw: TimeoutError("front busy"),
    ],
)
def test_an_incomplete_trading_code_query_leaves_the_fact_unknown(answer):
    provider, _trader, venue = _provider()
    venue.code_answer = answer
    facts = provider.trading_permission_facts(["rb2701"])
    entry = facts.for_symbol("rb2701")
    assert _statuses(entry)["trading_code"] == (UNKNOWN, "query_incomplete")
    assert entry.outcome == "unknown" and entry.complete is False
    assert facts.complete is False and facts.blockers
    assert facts.blockers[0].startswith("trading_code_query_")


def test_trading_code_request_evidence_for_another_exchange_blocks():
    provider, _trader, venue = _provider()
    venue.code_answer = lambda kw: _result("trading_code", [], filters={"ExchangeID": "DCE"})
    facts = provider.trading_permission_facts(["rb2701"])
    assert _statuses(facts.for_symbol("rb2701"))["trading_code"] == (
        UNKNOWN,
        "trading_code_filter_mismatch",
    )
    assert "trading_code_filter_mismatch" in facts.blockers and facts.complete is False


def test_an_old_feed_without_the_investor_query_leaves_the_investor_unknown():
    provider, _trader, venue = _provider()
    provider._feed.query_investor_result = None  # an old feed: calling it raises TypeError
    facts = provider.trading_permission_facts(["rb2701"])
    entry = facts.for_symbol("rb2701")
    assert _statuses(entry)["investor_active"] == (UNKNOWN, "query_incomplete")
    assert "investor_query_failed[TypeError]" in facts.blockers
    assert facts.complete is False and entry.outcome == "unknown"


def test_a_session_change_during_the_read_makes_every_fact_unknown():
    provider, trader, venue = _provider()
    venue.codes["SHFE"] = []  # would be a definitive negative in one session

    def reconnect(kwargs):
        trader.connection_generation = 5
        return _result("instruments", [instrument_row()], generation=4)

    venue.instrument_answer = reconnect
    facts = provider.trading_permission_facts(["rb2701"])
    entry = facts.for_symbol("rb2701")
    assert facts.session_consistent is False and facts.complete is False
    assert "permissions_session_changed" in facts.blockers
    assert {fact.status for fact in entry.facts} == {UNKNOWN}
    assert {fact.reason for fact in entry.facts} == {"session_changed"}
    assert entry.outcome == "unknown" and entry.denial_reasons == ()


def test_a_trading_day_change_during_the_read_makes_every_fact_unknown():
    provider, trader, venue = _provider()

    def roll(kwargs):
        trader.trading_day = "20261109"
        return _result("instruments", [instrument_row()])

    venue.instrument_answer = roll
    facts = provider.trading_permission_facts(["rb2701"])
    assert facts.session_consistent is False and facts.trading_day == "20261106"
    assert {f.status for f in facts.for_symbol("rb2701").facts} == {UNKNOWN}


def test_a_replaced_trader_during_the_read_is_a_session_change():
    provider, _trader, venue = _provider()

    def replace(kwargs):
        provider._feed._trader = FakeTrader()
        return _result("instruments", [instrument_row()])

    venue.instrument_answer = replace
    facts = provider.trading_permission_facts(["rb2701"])
    assert facts.session_consistent is False


def test_max_duration_skips_queries_not_started_in_time(monkeypatch):
    provider, _trader, venue = _provider()
    from bt_api_ctp import direct_write_provider as module

    clock = {"now": 100.0}
    monkeypatch.setattr(module.time, "monotonic", lambda: clock["now"])

    def slow_investor():
        clock["now"] += 30.0
        return _result("investor", [investor_row()])

    venue.investor = slow_investor
    facts = provider.trading_permission_facts(["rb2701"], max_duration=10.0)
    assert [call[0] for call in venue.calls] == ["investor"]
    assert "permissions_deadline_exceeded" in facts.blockers
    entry = facts.for_symbol("rb2701")
    assert _statuses(entry)["investor_active"] == (KNOWN_POSITIVE, "investor_active")
    assert entry.outcome == "unknown" and facts.complete is False


def test_without_rates_the_entry_has_only_the_denial_facts():
    provider, _trader, venue = _provider()
    facts = provider.trading_permission_facts(["rb2701"], include_rates=False)
    entry = facts.for_symbol("rb2701")
    assert [fact.name for fact in entry.facts] == [
        "investor_active",
        "instrument_trading",
        "trading_code",
    ]
    assert entry.outcome == "no_negative_found" and facts.native_queries == 3


@pytest.mark.parametrize(
    "symbols,kwargs,code",
    [
        ("rb2701", {}, "ctp_trading_permissions_symbols_invalid"),
        ([], {}, "ctp_trading_permissions_symbols_invalid"),
        (None, {}, "ctp_trading_permissions_symbols_invalid"),
        ({"rb2701": 1}, {}, "ctp_trading_permissions_symbols_invalid"),
        (["rb2701", " hc2701"], {}, "ctp_trading_permissions_symbol_invalid"),
        ([f"rb{n:04d}" for n in range(33)], {}, "ctp_trading_permissions_too_many_symbols"),
        (["rb2701"], {"exchange_ids": {"hc2701": "SHFE"}}, "ctp_trading_permissions_exchange_invalid"),
        (["rb2701"], {"exchange_ids": {"rb2701": "SH FE"}}, "ctp_trading_permissions_exchange_invalid"),
        (["rb2701"], {"exchange_ids": ["SHFE"]}, "ctp_trading_permissions_exchange_invalid"),
        (["rb2701"], {"include_rates": 1}, "ctp_trading_permissions_option_invalid"),
        (["rb2701"], {"max_duration": 0}, "ctp_trading_permissions_option_invalid"),
        (["rb2701"], {"max_duration": True}, "ctp_trading_permissions_option_invalid"),
    ],
)
def test_invalid_requests_are_refused_before_any_query(symbols, kwargs, code):
    provider, _trader, venue = _provider()
    with pytest.raises(DirectWriteError) as info:
        provider.trading_permission_facts(symbols, **kwargs)
    assert info.value.code == code
    assert venue.calls == []


def test_duplicate_symbols_are_read_once():
    provider, _trader, venue = _provider()
    facts = provider.trading_permission_facts(["rb2701", "rb2701"], include_rates=False)
    assert facts.symbols == ("rb2701",)
    assert [call[0] for call in venue.calls] == ["investor", "instrument", "trading_code"]


# ---------------------------------------------------------------------------
# neutral type invariants
# ---------------------------------------------------------------------------
def _entry(*facts, symbol="rb2701"):
    base = {
        "investor_active": PermissionFact("investor_active", KNOWN_POSITIVE, "investor_active", True),
        "trading_code": PermissionFact("trading_code", KNOWN_POSITIVE, "speculation_code_active", True),
        "instrument_trading": PermissionFact(
            "instrument_trading", KNOWN_POSITIVE, "instrument_trading", True
        ),
    }
    for fact in facts:
        base[fact.name] = fact
    return InstrumentPermissionFacts(symbol=symbol, venue="SHFE", facts=tuple(base.values()))


def _facts(*entries, **overrides):
    values = dict(
        exchange_name="CTP___FUTURE",
        trading_day="20261106",
        session_generation=4,
        observed_at_utc="2026-11-06T01:00:00Z",
        instruments=entries or (_entry(),),
        session_consistent=True,
    )
    values.update(overrides)
    return TradingPermissionFacts(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"proves_permission": True},
        {"scope": "account"},
        {"instruments": ()},
        {"instruments": (_entry(), _entry())},
        {"session_consistent": False},  # no blocker
        {"session_consistent": False, "blockers": ("permissions_session_changed",)},
        {"native_queries": -1},
    ],
)
def test_permission_facts_refuse_inconsistent_shapes(overrides):
    with pytest.raises(ValueError):
        _facts(**overrides)


def test_permission_fact_invariants():
    with pytest.raises(ValueError):
        PermissionFact("commission_rate", KNOWN_NEGATIVE, "rate_row_absent", True)
    with pytest.raises(ValueError):
        PermissionFact("trading_code", KNOWN_POSITIVE, "speculation_code_active", False)
    with pytest.raises(ValueError):
        PermissionFact("trading_code", UNKNOWN, "Free text with spaces", True)
    with pytest.raises(ValueError):
        PermissionFact("position_mode", UNKNOWN, "unknown", True)
    with pytest.raises(ValueError):
        InstrumentPermissionFacts(
            symbol="rb2701",
            venue="SHFE",
            facts=(PermissionFact("investor_active", KNOWN_POSITIVE, "investor_active", True),),
        )


def test_entry_outcomes():
    assert _entry().outcome == "no_negative_found" and _entry().complete
    negative = _entry(PermissionFact("trading_code", KNOWN_NEGATIVE, "trading_code_absent", True))
    assert negative.outcome == "known_negative"
    assert negative.denial_reasons == ("trading_code_absent",)
    unknown = _entry(PermissionFact("trading_code", UNKNOWN, "query_incomplete", False))
    assert unknown.outcome == "unknown" and unknown.complete is False
    assert _facts(unknown).complete is False
