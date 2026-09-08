"""Iteration 22 CTP contracts; all tests are offline fault injection."""

from __future__ import annotations

import hashlib
import queue
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bt_api_ctp import get_ctp_native_diagnostics
from bt_api_ctp.containers.ctp.ctp_ticker import CtpTickerData
from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.ctp.client import TraderClient, _TraderSpi
from bt_api_ctp.feeds.live_ctp_feed import (
    CtpMarketStream,
    CtpRequestDataFuture,
    CtpTradeStream,
    CtpVolumeDeltaTracker,
)
from bt_api_ctp.gateway.adapter import CtpGatewayAdapter
from bt_api_ctp.query import QueryResult


def _read_ready(client: TraderClient, *, trading_day: str = "20260909") -> TraderClient:
    client._connected = True
    client._authentication_state = "authenticated"
    client._login_state = "logged_in"
    client._trading_day = trading_day
    client._connection_generation = max(client._connection_generation, 1)
    client._query_interval = 0
    return client


def _result(
    request_type: str,
    records=(),
    *,
    complete=True,
    request_id=7,
    generation=1,
    account_fingerprint="fixture",
) -> QueryResult:
    now = datetime.now(timezone.utc)
    return QueryResult(
        request_type=request_type,
        request_id=request_id,
        connection_generation=generation,
        account_fingerprint=account_fingerprint,
        started_at_utc=now,
        completed_at_utc=now,
        is_last_seen=complete,
        error_code=None,
        error_message="",
        timed_out=False,
        complete=complete,
        records=tuple(records),
    )


def test_query_result_accumulates_only_matching_request_and_terminal_packet() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Api:
        def ReqQryOrder(self, _field, request_id):
            client._handle_query_callback(
                "orders", {"OrderSysID": "A"}, None, request_id, False
            )
            client._handle_query_callback(
                "orders", {"OrderSysID": "B"}, None, request_id, True
            )
            return 0

    client._api = Api()
    result = client.query_orders_result(timeout=0.01)

    assert result.complete is True
    assert [row["OrderSysID"] for row in result.records] == ["A", "B"]
    assert result.request_id == 1 and result.connection_generation == 1
    assert result.evidence_complete is True
    assert client.get_request_counts() == {"query_orders": 1}


def test_instrument_query_exposes_expiry_without_inventing_trading_day_ranking() -> (
    None
):
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Api:
        def ReqQryInstrument(self, _field, request_id):
            client._handle_query_callback(
                "instruments",
                SimpleNamespace(InstrumentID="SA601", ExpireDate="20260115"),
                None,
                request_id,
                True,
            )
            return 0

    client._api = Api()
    result = client.query_instruments_result(timeout=0.01)
    row = result.records[0]
    assert result.complete is True and row["expiry_date"] == "20260115"
    assert row["trading_days_to_expiry"] is None
    assert row["remaining_trading_days"] is None
    assert row["trading_calendar_evidence_complete"] is False
    assert row["ranking_trading_day"] is None
    assert row["prior_trading_day_volume"] is None
    assert row["prior_trading_day_open_interest"] is None
    assert row["prior_day_ranking_evidence_complete"] is False


def test_query_timeout_and_late_callback_never_become_empty_success() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))
    client._api = SimpleNamespace(ReqQryTradingAccount=lambda _field, _request_id: 0)

    result = client.query_account_result(timeout=0)
    assert (
        result.complete is False and result.timed_out is True and result.records == ()
    )

    client._handle_query_callback(
        "account", {"Balance": 1}, None, result.request_id, True
    )
    refreshed = client.get_query_result(result.request_id)
    assert refreshed is not None
    assert refreshed.complete is False and refreshed.late_callback_count == 1
    assert refreshed.records == ()


def test_query_callback_detaches_reused_native_record_objects() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Record:
        OrderSysID = "A"

    record = Record()

    class Api:
        def ReqQryOrder(self, _field, request_id):
            client._handle_query_callback("orders", record, None, request_id, False)
            record.OrderSysID = "B"
            client._handle_query_callback("orders", record, None, request_id, True)
            return 0

    client._api = Api()
    result = client.query_orders_result(timeout=0.01)
    assert [row.OrderSysID for row in result.records] == ["A", "B"]


def test_query_callback_after_terminal_packet_is_counted_as_late() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Api:
        def ReqQryOrder(self, _field, request_id):
            client._handle_query_callback(
                "orders", {"OrderSysID": "terminal"}, None, request_id, True
            )
            client._handle_query_callback(
                "orders", {"OrderSysID": "late"}, None, request_id, True
            )
            return 0

    client._api = Api()
    result = client.query_orders_result(timeout=0.01)
    assert [row.OrderSysID for row in result.records] == ["terminal"]
    assert result.complete is True and result.late_callback_count == 1


def test_old_generation_query_callback_is_orphaned() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Api:
        def ReqQryInvestorPosition(self, _field, request_id):
            client._connection_generation += 1
            client._handle_query_callback(
                "positions", {"InstrumentID": "IF"}, None, request_id, True
            )
            return 0

    client._api = Api()
    result = client.query_positions_result(timeout=0)
    assert result.complete is False and result.timed_out is True
    assert result.records == ()
    assert client._orphan_query_callbacks[-1]["request_id"] == result.request_id


def test_read_only_login_cannot_insert_or_cancel_and_counts_zero_writes() -> None:
    client = _read_ready(
        TraderClient(
            "tcp://test", "9999", "account", "secret", auto_settlement_confirm=False
        )
    )
    calls = []
    client._api = SimpleNamespace(
        ReqOrderInsert=lambda *_args: calls.append("insert") or 0,
        ReqOrderAction=lambda *_args: calls.append("cancel") or 0,
    )
    feed = CtpRequestDataFuture(
        queue.Queue(),
        broker_id="9999",
        user_id="account",
        td_front="tcp://test-td",
        md_front="tcp://test-md",
        auto_settlement_confirm=False,
    )
    feed._trader = client

    with pytest.raises(Exception, match="settlement is unconfirmed"):
        feed.make_order("IF2506", 1, 3500, "buy-limit", time_in_force="GFD")
    with pytest.raises(Exception, match="settlement is unconfirmed"):
        feed.cancel_order("IF2506", order_id="SYS")
    assert calls == []
    assert client.get_request_counts().get("order_insert", 0) == 0
    assert client.get_request_counts().get("order_action", 0) == 0


def test_complete_empty_account_query_fails_account_snapshot() -> None:
    class Trader:
        is_read_only_ready = True

        @staticmethod
        def query_account_result(timeout=5):
            return _result("account", ())

    feed = CtpRequestDataFuture(
        queue.Queue(),
        broker_id="9999",
        user_id="account",
        td_front="tcp://test-td",
        md_front="tcp://test-md",
    )
    feed._trader = Trader()
    response = feed.get_account()
    assert response.get_status() is False
    assert response.get_data() == []
    assert response.get_extra_data()["query_complete"] is True
    assert response.get_extra_data()["account_snapshot_complete"] is False


def test_public_exchange_info_joins_terminal_instrument_margin_and_fee_queries() -> (
    None
):
    class Trader:
        is_read_only_ready = True

        @staticmethod
        def get_session_state():
            return {"connection_generation": 1, "account_fingerprint": "fixture"}

        @staticmethod
        def query_instruments_result(**_kwargs):
            return _result(
                "instruments",
                (
                    {
                        "InstrumentID": "SA601",
                        "ExchangeID": "CZCE",
                        "ProductID": "SA",
                        "PriceTick": 1,
                        "VolumeMultiple": 20,
                        "ExpireDate": "20260115",
                        "IsTrading": 1,
                        "trading_calendar_evidence_complete": False,
                        "prior_day_ranking_evidence_complete": False,
                    },
                ),
            )

        @staticmethod
        def query_instrument_margin_rate_result(*_args, **_kwargs):
            return _result(
                "margin_rate",
                (
                    {
                        "InstrumentID": "SA601",
                        "LongMarginRatioByMoney": 0.12,
                        "ShortMarginRatioByMoney": 0.13,
                    },
                ),
            )

        @staticmethod
        def query_instrument_commission_rate_result(*_args, **_kwargs):
            return _result(
                "commission_rate",
                (
                    {
                        "InstrumentID": "SA601",
                        "OpenRatioByVolume": 3,
                        "CloseRatioByVolume": 3,
                        "CloseTodayRatioByVolume": 6,
                    },
                ),
            )

    feed = CtpRequestDataFuture(td_front="tcp://td", md_front="tcp://md")
    feed._trader = Trader()
    response = feed.get_exchange_info("SA601.CZCE", timeout=0)

    assert response.get_status() is True
    spec = response.get_data()[0]
    assert spec["symbol"] == "SA601.CZCE" and spec["instrument"] == "SA601"
    assert spec["price_tick"] == 1 and spec["contract_value"] == 20
    assert spec["margin_rate"] == 0.12
    assert spec["open_fee_amount"] == 3
    assert spec["close_today_fee_amount"] == 6
    assert spec["metadata_complete"] is True
    assert spec["trading_calendar_evidence_complete"] is False
    assert spec["prior_day_ranking_evidence_complete"] is False


def test_public_exchange_info_fails_when_fee_query_has_no_record() -> None:
    class Trader:
        is_read_only_ready = True
        query_instruments_result = staticmethod(
            lambda **_kwargs: _result(
                "instruments",
                ({"InstrumentID": "SA601", "PriceTick": 1, "VolumeMultiple": 20},),
            )
        )
        query_instrument_margin_rate_result = staticmethod(
            lambda *_args, **_kwargs: _result(
                "margin_rate", ({"InstrumentID": "SA601"},)
            )
        )
        query_instrument_commission_rate_result = staticmethod(
            lambda *_args, **_kwargs: _result("commission_rate", ())
        )

    feed = CtpRequestDataFuture(td_front="tcp://td", md_front="tcp://md")
    feed._trader = Trader()
    response = feed.get_exchange_info("SA601.CZCE", timeout=0)
    assert response.get_status() is False and response.get_data() == []
    assert response.get_extra_data()["metadata_complete"] is False


@pytest.mark.parametrize("empty_component", ["margin", "commission"])
def test_public_exchange_info_rejects_complete_but_empty_rate_evidence(
    empty_component: str,
) -> None:
    class Trader:
        is_read_only_ready = True

        @staticmethod
        def get_session_state():
            return {"connection_generation": 1, "account_fingerprint": "fixture"}

        @staticmethod
        def query_instruments_result(**_kwargs):
            return _result(
                "instruments",
                ({"InstrumentID": "SA601", "PriceTick": 1, "VolumeMultiple": 20},),
            )

        @staticmethod
        def query_instrument_margin_rate_result(*_args, **_kwargs):
            record = {"InstrumentID": "SA601"}
            if empty_component != "margin":
                record.update(LongMarginRatioByMoney=0.12, ShortMarginRatioByMoney=0.13)
            return _result("margin_rate", (record,))

        @staticmethod
        def query_instrument_commission_rate_result(*_args, **_kwargs):
            record = {"InstrumentID": "SA601"}
            if empty_component != "commission":
                record.update(
                    OpenRatioByVolume=3,
                    CloseRatioByVolume=3,
                    CloseTodayRatioByVolume=6,
                )
            return _result("commission_rate", (record,))

    feed = CtpRequestDataFuture(td_front="tcp://td", md_front="tcp://md")
    feed._trader = Trader()
    response = feed.get_exchange_info("SA601.CZCE", timeout=0)

    assert response.get_status() is False and response.get_data() == []
    extra = response.get_extra_data()
    assert extra["metadata_complete"] is False
    assert extra["evidence_complete"] is False
    assert (
        f"missing_{'long_margin' if empty_component == 'margin' else 'open_commission'}"
        in extra["metadata_error"]
    )


@pytest.mark.parametrize(
    ("generation", "fingerprint", "session_state", "expected_error"),
    [
        (
            2,
            "fixture",
            {"connection_generation": 1, "account_fingerprint": "fixture"},
            "mixed_query_connection_generation",
        ),
        (
            1,
            "other",
            {"connection_generation": 1, "account_fingerprint": "fixture"},
            "mixed_query_account",
        ),
        (
            1,
            "fixture",
            {"account_fingerprint": "fixture"},
            "missing_current_session_generation",
        ),
        (
            1,
            "fixture",
            {"connection_generation": 1},
            "missing_current_session_account_fingerprint",
        ),
    ],
)
def test_public_exchange_info_rejects_mixed_query_identity(
    generation: int,
    fingerprint: str,
    session_state: dict[str, object],
    expected_error: str,
) -> None:
    class Trader:
        is_read_only_ready = True

        @staticmethod
        def get_session_state():
            return session_state

        @staticmethod
        def query_instruments_result(**_kwargs):
            return _result(
                "instruments",
                ({"InstrumentID": "SA601", "PriceTick": 1, "VolumeMultiple": 20},),
            )

        @staticmethod
        def query_instrument_margin_rate_result(*_args, **_kwargs):
            return _result(
                "margin_rate",
                (
                    {
                        "InstrumentID": "SA601",
                        "LongMarginRatioByMoney": 0.12,
                        "ShortMarginRatioByMoney": 0.13,
                    },
                ),
            )

        @staticmethod
        def query_instrument_commission_rate_result(*_args, **_kwargs):
            return _result(
                "commission_rate",
                (
                    {
                        "InstrumentID": "SA601",
                        "OpenRatioByVolume": 3,
                        "CloseRatioByVolume": 3,
                        "CloseTodayRatioByVolume": 6,
                    },
                ),
                generation=generation,
                account_fingerprint=fingerprint,
            )

    feed = CtpRequestDataFuture(td_front="tcp://td", md_front="tcp://md")
    feed._trader = Trader()
    response = feed.get_exchange_info("SA601.CZCE", timeout=0)

    assert response.get_status() is False and response.get_data() == []
    assert expected_error in response.get_extra_data()["metadata_error"]


def test_auth_failure_does_not_submit_login() -> None:
    client = TraderClient("tcp://test", "9999", "account", "secret")
    submitted = []
    client._api = SimpleNamespace(ReqUserLogin=lambda *_args: submitted.append("login"))
    client._authentication_state = "authenticating"
    info = SimpleNamespace(ErrorID=7, ErrorMsg="bad auth")

    _TraderSpi(client).OnRspAuthenticate(None, info, 1, True)

    assert client.get_session_state()["auth_state"] == "failed"
    assert client.get_session_state()["login_state"] == "disconnected"
    assert submitted == []


def test_read_only_login_issues_zero_implicit_settlement_writes() -> None:
    client = TraderClient(
        "tcp://test", "9999", "account", "secret", auto_settlement_confirm=False
    )
    client._connected = True
    client._authentication_state = "authenticated"
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda *_args: pytest.fail(
            "read-only login must not confirm settlement"
        )
    )
    _TraderSpi(client).OnRspUserLogin(
        SimpleNamespace(FrontID=1, SessionID=2, TradingDay="20260909", MaxOrderRef="7"),
        None,
        1,
        True,
    )
    state = client.get_session_state()
    assert state["read_only_ready"] is True
    assert state["trading_ready"] is False
    assert state["account_fingerprint"] == client._account_fingerprint
    assert state["account_fingerprint"]
    assert state["settlement_state"] == "not_requested"
    assert state["request_counts"].get("settlement_confirm", 0) == 0


def test_implicit_settlement_submit_exception_never_marks_session_ready() -> None:
    client = TraderClient("tcp://test", "9999", "account", "secret")
    client._connected = True
    client._authentication_state = "authenticated"
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
    )
    _TraderSpi(client).OnRspUserLogin(
        SimpleNamespace(FrontID=1, SessionID=2, TradingDay="20260909", MaxOrderRef="7"),
        None,
        1,
        True,
    )
    state = client.get_session_state()
    assert state["settlement_state"] == "failed"
    assert state["trading_ready"] is False
    assert state["request_counts"]["settlement_confirm"] == 1


def test_old_settlement_response_cannot_confirm_new_generation() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))
    request_ids = []
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda _field, request_id: request_ids.append(
            request_id
        )
        or 0
    )
    assert client._request_settlement_confirmation() is True
    old_request = request_ids[-1]

    client._on_front_disconnected(1)
    client._on_front_connected()
    client._authentication_state = "authenticated"
    client._login_state = "logged_in"
    client._trading_day = "20260910"
    assert client._request_settlement_confirmation() is True
    new_request = request_ids[-1]

    spi = _TraderSpi(client)
    field = SimpleNamespace(BrokerID="9999", InvestorID="account")
    spi.OnRspSettlementInfoConfirm(field, None, old_request, True)
    assert client.get_session_state()["settlement_state"] == "confirming"
    assert client.get_session_state()["trading_ready"] is False
    spi.OnRspSettlementInfoConfirm(field, None, new_request, True)
    assert client.get_session_state()["trading_ready"] is True
    assert client.get_session_state()["settlement_late_callback_count"] == 1


def test_settlement_response_for_wrong_trading_day_is_rejected() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))
    request_ids = []
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda _field, request_id: request_ids.append(
            request_id
        )
        or 0
    )
    assert client._request_settlement_confirmation() is True
    _TraderSpi(client).OnRspSettlementInfoConfirm(
        SimpleNamespace(BrokerID="9999", InvestorID="account", ConfirmDate="20260908"),
        None,
        request_ids[-1],
        True,
    )
    assert client.get_session_state()["trading_ready"] is False
    assert client.get_session_state()["settlement_late_callback_count"] == 1


def test_server_confirmation_query_promotes_only_matching_account_and_day() -> None:
    client = _read_ready(
        TraderClient(
            "tcp://test", "9999", "account", "secret", auto_settlement_confirm=False
        )
    )

    class Api:
        def ReqQrySettlementInfoConfirm(self, _field, request_id):
            record = SimpleNamespace(
                BrokerID="9999", InvestorID="account", ConfirmDate="20260909"
            )
            client._handle_query_callback(
                "settlement_confirmation", record, None, request_id, True
            )
            return 0

    client._api = Api()
    result = client.verify_settlement_confirmation(timeout=0.01)
    state = client.get_session_state()
    assert result.complete is True
    assert state["trading_ready"] is True
    assert state["settlement_proof_source"] == "confirmation_query"
    assert state["settlement_proof_query_request_id"] == result.request_id


@pytest.mark.parametrize(
    ("broker_id", "investor_id"),
    [("", "account"), ("9999", ""), ("other", "account"), ("9999", "other")],
)
def test_server_confirmation_query_requires_exact_account_identity(
    broker_id: str, investor_id: str
) -> None:
    client = _read_ready(
        TraderClient(
            "tcp://test", "9999", "account", "secret", auto_settlement_confirm=False
        )
    )

    class Api:
        def ReqQrySettlementInfoConfirm(self, _field, request_id):
            client._handle_query_callback(
                "settlement_confirmation",
                SimpleNamespace(
                    BrokerID=broker_id,
                    InvestorID=investor_id,
                    ConfirmDate="20260909",
                ),
                None,
                request_id,
                True,
            )
            return 0

    client._api = Api()
    result = client.verify_settlement_confirmation(timeout=0.01)
    assert result.complete is True
    assert client.get_session_state()["trading_ready"] is False


def test_trade_time_filter_reports_unsupported_struct_instead_of_silently_dropping(
    monkeypatch,
) -> None:
    class TradeField:
        def __setattr__(self, name, value):
            if name in {"TradeTimeStart", "TradeTimeEnd"}:
                raise AttributeError(name)
            object.__setattr__(self, name, value)

    monkeypatch.setattr(client_module, "CThostFtdcQryTradeField", TradeField)
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))
    client._api = SimpleNamespace(ReqQryTrade=lambda *_args: 0)
    result = client.query_trades_result(start_time="09:00:00", timeout=0)
    assert result.complete is False and result.unsupported is True
    assert result.error_message == "native_trade_filter_unsupported:TradeTimeStart"


def _tick(
    total: int,
    seq: int,
    *,
    generation: int = 1,
    action_day: str = "20260909",
    **overrides,
) -> CtpTickerData:
    payload = {
        "InstrumentID": "IF2506",
        "ExchangeID": "CFFEX",
        "LastPrice": 4000,
        "BidPrice1": 3999,
        "AskPrice1": 4001,
        "BidVolume1": 2,
        "AskVolume1": 3,
        "OpenInterest": 1000,
        "Volume": total,
        "TradingDay": "20260909",
        "ActionDay": action_day,
        "UpdateTime": f"09:30:0{seq}",
        "UpdateMillisec": 0,
    }
    payload.update(overrides)
    return CtpTickerData(
        payload,
        connection_generation=generation,
        ingest_seq=seq,
    )


def test_volume_delta_continues_across_action_day_midnight_within_trading_day() -> None:
    tracker = CtpVolumeDeltaTracker()
    first = tracker.apply(_tick(100, 1, action_day="20260909"))
    second = tracker.apply(
        _tick(
            105,
            2,
            action_day="20260910",
            UpdateTime="00:00:01",
        )
    )

    assert first.delta_volume == 0 and first.volume_complete is False
    assert second.delta_volume == 5 and second.volume_complete is True
    assert second.volume_quality == "CONTINUOUS"


def test_quote_v2_is_single_authoritative_cumulative_to_delta_conversion() -> None:
    adapter = object.__new__(CtpGatewayAdapter)
    adapter.last_price = {}
    adapter.last_volume = {}
    adapter._volume_state = {}
    adapter.aliases = {"IF2506": {"IF2506.CFFEX"}}
    emitted = []
    adapter.emit = lambda channel, item: emitted.append((channel, item))

    adapter._tick(_tick(100, 1))
    adapter._tick(_tick(107, 2))
    adapter._tick(_tick(110, 3, generation=2))

    ticks = [item for _channel, item in emitted]
    assert [tick.volume for tick in ticks] == [0, 7, 0]
    assert [tick.cum_volume for tick in ticks] == [100, 107, 110]
    assert [tick.volume_complete for tick in ticks] == [False, True, False]
    assert all(tick.schema_version == "ctp.quote.v2" for tick in ticks)
    assert all(tick.volume_semantics == "delta" for tick in ticks)
    assert all(tick.action_day == "20260909" for tick in ticks)


def test_market_stream_converts_volume_once_before_gateway_consumption() -> None:
    stream = CtpMarketStream(topics=[])
    stream._md_client = SimpleNamespace(connection_generation=11)
    rows = []
    stream.push_data = rows.append

    for total, second in ((100, 1), (107, 2)):
        stream._on_tick(
            SimpleNamespace(
                InstrumentID="IF2506",
                ExchangeID="CFFEX",
                LastPrice=4000,
                BidPrice1=3999,
                AskPrice1=4001,
                BidVolume1=2,
                AskVolume1=3,
                OpenInterest=1000,
                Volume=total,
                TradingDay="20260909",
                ActionDay="20260909",
                UpdateTime=f"09:30:0{second}",
                UpdateMillisec=0,
            )
        )

    assert [row.volume_semantics for row in rows] == ["delta", "delta"]
    assert [row.cum_volume for row in rows] == [100, 107]
    assert [row.delta_volume for row in rows] == [0, 7]
    assert [row.volume_complete for row in rows] == [False, True]

    adapter = object.__new__(CtpGatewayAdapter)
    adapter.last_price = {}
    adapter.last_volume = {}
    adapter.aliases = {"IF2506": {"IF2506.CFFEX"}}
    emitted = []
    adapter.emit = lambda channel, item: emitted.append((channel, item))
    for row in rows:
        adapter._tick(row)
    assert [item.volume for _channel, item in emitted] == [0, 7]


def test_invalid_market_snapshot_does_not_advance_volume_baseline() -> None:
    stream = CtpMarketStream(topics=[])
    stream._md_client = SimpleNamespace(connection_generation=11)
    rows = []
    stream.push_data = rows.append

    def push(total: int, second: int, **overrides) -> None:
        fields = {
            "InstrumentID": "IF2506",
            "ExchangeID": "CFFEX",
            "LastPrice": 4000,
            "BidPrice1": 3999,
            "AskPrice1": 4001,
            "BidVolume1": 2,
            "AskVolume1": 3,
            "OpenInterest": 1000,
            "Volume": total,
            "TradingDay": "20260909",
            "ActionDay": "20260909",
            "UpdateTime": f"09:30:0{second}",
            "UpdateMillisec": 0,
        }
        fields.update(overrides)
        stream._on_tick(SimpleNamespace(**fields))

    push(100, 1)
    push(105, 2, LastPrice=float("nan"))
    push(110, 3)

    assert [row.delta_volume for row in rows] == [0, 0, 10]
    assert [row.volume_complete for row in rows] == [False, False, True]
    assert rows[1].volume_quality == "INVALID_QUOTE"
    assert "INVALID_LAST_PRICE" in rows[1].quality_flags


@pytest.mark.parametrize(
    ("overrides", "quality_flag"),
    [
        ({"LastPrice": float("nan")}, "INVALID_LAST_PRICE"),
        ({"AskVolume1": float("nan")}, "INVALID_ASK_VOLUME"),
        ({"BidPrice1": 4002, "AskPrice1": 4001}, "CROSSED_BOOK"),
        ({"BidVolume1": -1}, "INVALID_BID_VOLUME"),
    ],
)
def test_invalid_quote_is_published_as_ineligible_and_never_cached_or_traded(
    overrides: dict[str, float], quality_flag: str
) -> None:
    adapter = object.__new__(CtpGatewayAdapter)
    adapter.last_price = {}
    adapter.last_volume = {}
    adapter.aliases = {"IF2506": {"IF2506.CFFEX"}}
    emitted = []
    adapter.emit = lambda channel, item: emitted.append((channel, item))
    row = _tick(100, 1, **overrides)

    adapter._tick(row)

    assert len(emitted) == 1
    assert emitted[0][1].execution_eligible is False
    assert quality_flag in emitted[0][1].quality_flags
    assert adapter.last_price == {} and adapter.last_volume == {}
    assert quality_flag in row.quality_flags
    adapter.feed = SimpleNamespace(
        make_order=lambda *_args, **_kwargs: pytest.fail(
            "quality-rejected quote must block native order submission"
        )
    )
    with pytest.raises(RuntimeError, match="latest quote failed quality checks"):
        adapter.place_order(
            {
                "symbol": "IF2506.CFFEX",
                "side": "buy",
                "size": 1,
                "price": 4000,
                "order_type": "limit",
            }
        )


@pytest.mark.parametrize(
    ("overrides", "quality_flag"),
    [
        ({"BidVolume1": 0}, "ZERO_DEPTH"),
        ({"BidPrice1": 4001, "AskPrice1": 4001}, "LOCKED_BOOK"),
    ],
)
def test_zero_depth_and_locked_books_are_recorded_but_not_execution_eligible(
    overrides: dict[str, float], quality_flag: str
) -> None:
    adapter = object.__new__(CtpGatewayAdapter)
    adapter.last_price = {}
    adapter.last_volume = {}
    adapter.aliases = {"IF2506": {"IF2506.CFFEX"}}
    emitted = []
    adapter.emit = lambda channel, item: emitted.append((channel, item))

    adapter._tick(_tick(100, 1, **overrides))

    assert len(emitted) == 1
    tick = emitted[0][1]
    assert tick.execution_eligible is False
    assert quality_flag in tick.quality_flags
    assert adapter.last_price["IF2506"] == 4000


def test_trade_stream_reuses_request_feed_trader_without_second_td_client(
    monkeypatch,
) -> None:
    stopped = []
    trader = SimpleNamespace(
        is_read_only_ready=True,
        on_order=None,
        on_trade=None,
        on_login=None,
        on_error=None,
        stop=lambda: stopped.append(True),
    )

    class RequestFeed:
        trader_client = trader

        def __init__(self):
            self.connect_calls = 0

        def connect(self):
            self.connect_calls += 1

    request_feed = RequestFeed()
    monkeypatch.setattr(
        client_module,
        "TraderClient",
        lambda *_args, **_kwargs: pytest.fail("must reuse the request-feed TD client"),
    )
    stream = CtpTradeStream(request_feed=request_feed)
    stream.connect()

    assert request_feed.connect_calls == 1
    assert stream.trader_client is trader
    assert stream.state.value == "authenticated"
    assert callable(trader.on_order) and callable(trader.on_trade)
    stream.disconnect()
    assert stopped == []
    assert trader.on_order is None and trader.on_trade is None


def test_request_feed_reuses_disconnected_td_client_instead_of_replacing_it(
    monkeypatch,
) -> None:
    waited = []
    stopped = []
    trader = SimpleNamespace(
        is_read_only_ready=False,
        wait_ready=lambda timeout: waited.append(timeout) or True,
        stop=lambda: stopped.append(True),
    )
    monkeypatch.setattr(
        client_module,
        "TraderClient",
        lambda *_args, **_kwargs: pytest.fail(
            "existing TD client must not be replaced"
        ),
    )
    feed = CtpRequestDataFuture(td_front="tcp://td", md_front="tcp://md")
    feed._trader = trader

    feed.connect()

    assert feed.trader_client is trader
    assert feed._connected is True
    assert waited == [feed._connect_timeout]
    assert stopped == []


def test_complete_trade_query_cannot_claim_complete_after_public_truncation() -> None:
    records = tuple(
        {
            "InstrumentID": "IF2506",
            "ExchangeID": "CFFEX",
            "TradeID": f"T{index:03d}",
            "Price": 4000,
            "Volume": 1,
        }
        for index in range(101)
    )

    class Trader:
        is_read_only_ready = True

        @staticmethod
        def query_trades_result(**_kwargs):
            return _result("trades", records)

    feed = CtpRequestDataFuture(td_front="tcp://td", md_front="tcp://md")
    feed._trader = Trader()
    response = feed.get_deals(count=100)

    assert response.get_status() is False and response.get_data() == []
    extra = response.get_extra_data()
    assert extra["query_complete"] is True
    assert extra["trade_snapshot_complete"] is False
    assert extra["records_truncated"] is True
    assert extra["trade_record_count"] == 101
    assert extra["returned_trade_record_count"] == 100
    assert extra["evidence_complete"] is False


def test_native_diagnostics_expose_only_matching_extension_hashes() -> None:
    diagnostics = get_ctp_native_diagnostics()
    paths = diagnostics["matching_extension_paths"]
    hashes = diagnostics["matching_extension_sha256"]
    assert diagnostics["package_dir"]
    assert diagnostics["native_loaded"] in {True, False}
    assert set(hashes) == set(paths)
    assert all(len(value) == 64 for value in hashes.values())
    assert diagnostics["runtime_source"] in {
        "vendored_bt_api_py",
        "external_ctp_python",
        "external_openctp_ctp",
    }
    if diagnostics["native_loaded"]:
        loaded_path = Path(diagnostics["loaded_module_path"])
        assert loaded_path.is_file()
        assert (
            diagnostics["loaded_module_sha256"]
            == hashlib.sha256(loaded_path.read_bytes()).hexdigest()
        )
        assert (
            diagnostics["native_module_sha256"][str(loaded_path)]
            == diagnostics["loaded_module_sha256"]
        )
    else:
        assert diagnostics["loaded_module_path"] == ""
        assert diagnostics["loaded_module_sha256"] == ""


def test_native_check_fails_closed_when_selected_runtime_has_only_python_modules(
    monkeypatch,
) -> None:
    monkeypatch.setattr(client_module, "_CTP_RUNTIME_SOURCE", "external_ctp_python")
    monkeypatch.setattr(
        client_module,
        "_selected_runtime_modules",
        lambda: {"ctp": "/tmp/ctp/__init__.py"},
    )

    diagnostics = client_module.get_ctp_native_diagnostics()
    assert diagnostics["native_loaded"] is False
    assert diagnostics["native_module_paths"] == {}
    with pytest.raises(ImportError, match="no verified native extension"):
        client_module._check_native_module()


def test_missing_price_tick_never_falls_back_to_one() -> None:
    adapter = object.__new__(CtpGatewayAdapter)
    adapter._price_ticks = {}
    adapter.get_symbol_info = lambda _symbol: {}
    with pytest.raises(RuntimeError, match="positive PriceTick required"):
        adapter._get_price_tick("IF2506")
