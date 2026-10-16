"""Offline tests for the live, exact-source CTP instrument metadata reader."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import bt_api_ctp.ctp.client as ctp_client
from bt_api_ctp.ctp.client import MdClient, TraderClient, _TraderSpi
from bt_api_ctp.feeds.live_ctp_feed import CtpMarketStream, CtpRequestDataFuture
from bt_api_ctp.instrument_metadata import CtpInstrumentMetadataError, CtpInstrumentMetadataReader


_BROKER = "metadata-fixture-broker"
_INVESTOR = "metadata-fixture-investor"
_DAY = "20261016"
_INSTRUMENT = "IF2612"
_EXCHANGE = "CFFEX"


class _SyntheticLoginField:
    """Python stand-in for the native fixed-char login struct in callback tests."""

    BrokerID = ""
    UserID = ""
    Password = ""


class _FakeMdApi:
    def __init__(self) -> None:
        self.login_calls: list[tuple[object, int]] = []

    def ReqUserLogin(self, field, request_id: int) -> int:
        self.login_calls.append((field, request_id))
        return 0


class _FakeTraderApi:
    def __init__(self, trader: TraderClient, *, returned_instrument: str = _INSTRUMENT) -> None:
        self.trader = trader
        self.returned_instrument = returned_instrument
        self.query_fields: list[tuple[str, str, str]] = []

    def ReqQryInstrument(self, field, request_id: int) -> int:
        self.query_fields.append((field.InstrumentID, field.ExchangeID, field.ProductID))
        self.trader._handle_query_callback(
            "instruments",
            SimpleNamespace(
                InstrumentID=self.returned_instrument,
                ExchangeID=field.ExchangeID,
                ProductClass="1",
                PriceTick=0.2,
                VolumeMultiple=300,
                LowerLimitPrice=1000.0,
                UpperLimitPrice=2000.0,
            ),
            None,
            request_id,
            True,
        )
        return 0


def _read_ready_trader() -> TraderClient:
    trader = TraderClient(
        "tcp://offline.invalid:0",
        _BROKER,
        _INVESTOR,
        "",
        auto_settlement_confirm=False,
    )
    trader._connected = True
    trader._authentication_state = "authenticated"
    trader._login_state = "logging_in"
    trader._connection_generation = 7
    trader._req_id = 30
    trader._login_request_id = 30
    trader._login_connection_generation = 7
    trader._query_interval = 0.0
    _TraderSpi(trader).OnRspUserLogin(
        SimpleNamespace(
            BrokerID=trader._bound_broker_id,
            InvestorID=trader._bound_user_id,
            UserID=trader._bound_user_id,
            TradingDay=_DAY,
            FrontID=0,
            SessionID=0,
            MaxOrderRef="",
        ),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        30,
        True,
    )
    return trader


def _ready_market(monkeypatch: pytest.MonkeyPatch) -> CtpMarketStream:
    monkeypatch.setattr(ctp_client, "CThostFtdcReqUserLoginField", _SyntheticLoginField)

    def start_without_native_library(client: MdClient, *, block: bool = True) -> None:
        assert block is False
        api = _FakeMdApi()
        spi = client._create_md_spi(api)
        client._api = api
        client._spi = spi
        spi.OnFrontConnected()
        request_id = api.login_calls[-1][1]
        spi.OnRspUserLogin(
            SimpleNamespace(
                BrokerID=client._bound_broker_id,
                UserID=client._bound_user_id,
                TradingDay=_DAY,
            ),
            SimpleNamespace(ErrorID=0, ErrorMsg=""),
            request_id,
            True,
        )

    monkeypatch.setattr(MdClient, "start", start_without_native_library)
    market = CtpMarketStream(
        md_front="tcp://offline.invalid:0",
        broker_id=_BROKER,
        user_id=_INVESTOR,
        password="",
        topics=(),
    )
    market.connect()
    assert market.get_quote_clock_source_scope() is not None
    return market


def _reader(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returned_instrument: str = _INSTRUMENT,
) -> tuple[CtpInstrumentMetadataReader, _FakeTraderApi, CtpMarketStream]:
    trader = _read_ready_trader()
    api = _FakeTraderApi(trader, returned_instrument=returned_instrument)
    trader._api = api
    request_feed = CtpRequestDataFuture()
    request_feed._trader = trader
    market = _ready_market(monkeypatch)
    return CtpInstrumentMetadataReader(request_feed, market), api, market


def test_reader_issues_exact_native_query_and_binds_both_current_sessions(monkeypatch):
    reader, api, market = _reader(monkeypatch)

    fact = reader.read(
        _INSTRUMENT,
        _EXCHANGE,
        account_session_generation="account-generation-7",
        timeout=2.0,
    )

    assert api.query_fields == [(_INSTRUMENT, _EXCHANGE, "")]
    assert fact.instrument_id == _INSTRUMENT
    assert fact.exchange_id == _EXCHANGE
    assert fact.asset_type == "future"
    assert fact.price_tick == "0.2"
    assert fact.volume_multiple == 300
    assert fact.lower_limit_price == "1000"
    assert fact.upper_limit_price == "2000"
    assert fact.price_limits_available is True
    assert fact.source == "ctp.native.instruments"
    assert fact.source_connection_generation == 7
    assert fact.account_session_generation == "account-generation-7"
    assert fact.quote_source == "ctp.native.md"
    assert fact.quote_stream_generation == market.get_quote_clock_source_scope().connection_generation
    assert fact.clock_domain_id == market.get_quote_clock_source_scope().clock_domain_id
    assert fact.clock_source_session_id == market.get_quote_clock_source_scope().clock_source_session_id
    assert fact.clock_source == market.get_quote_clock_source_scope().clock_source
    assert fact.query_elapsed_seconds >= 0
    assert len(fact.source_records_sha256) == 64
    assert fact.to_dict()["schema_id"] == "ctp.instrument_metadata"


def test_reader_rejects_wrong_native_returned_instrument(monkeypatch):
    reader, api, _market = _reader(monkeypatch, returned_instrument="IH2612")

    with pytest.raises(CtpInstrumentMetadataError) as caught:
        reader.read(
            _INSTRUMENT,
            _EXCHANGE,
            account_session_generation="account-generation-7",
            timeout=2.0,
        )

    assert caught.value.code == "INSTRUMENT_QUERY_UNPROVEN"
    assert api.query_fields == [(_INSTRUMENT, _EXCHANGE, "")]


def test_reader_fails_closed_without_authenticated_market_scope(monkeypatch):
    trader = _read_ready_trader()
    api = _FakeTraderApi(trader)
    trader._api = api
    request_feed = CtpRequestDataFuture()
    request_feed._trader = trader
    market = CtpMarketStream(
        md_front="tcp://offline.invalid:0",
        broker_id=_BROKER,
        user_id=_INVESTOR,
        password="",
        topics=(),
    )
    del monkeypatch
    reader = CtpInstrumentMetadataReader(request_feed, market)

    with pytest.raises(CtpInstrumentMetadataError) as caught:
        reader.read(
            _INSTRUMENT,
            _EXCHANGE,
            account_session_generation="account-generation-7",
            timeout=2.0,
        )

    assert caught.value.code == "QUOTE_SOURCE_UNAVAILABLE"
    assert api.query_fields == []
