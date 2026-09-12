"""Offline contracts for read-only futures/options discovery and reference queries."""

from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import TraderClient, _TraderSpi
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

QUERY_CASES = (
    (
        "query_depth_market_data_result",
        "ReqQryDepthMarketData",
        "OnRspQryDepthMarketData",
        "depth_market_data",
        {},
        {"InstrumentID": ""},
    ),
    (
        "query_option_instrument_trade_cost_result",
        "ReqQryOptionInstrTradeCost",
        "OnRspQryOptionInstrTradeCost",
        "option_trade_cost",
        {
            "instrument_id": "m2701-C-3000",
            "exchange_id": "DCE",
            "input_price": 123.5,
            "underlying_price": 3001.0,
        },
        {
            "InstrumentID": "m2701-C-3000",
            "ExchangeID": "DCE",
            "HedgeFlag": "1",
            "BrokerID": "9999",
            "InvestorID": "offline",
            "InputPrice": 123.5,
            "UnderlyingPrice": 3001.0,
        },
    ),
    (
        "query_option_instrument_commission_rate_result",
        "ReqQryOptionInstrCommRate",
        "OnRspQryOptionInstrCommRate",
        "option_commission_rate",
        {"instrument_id": "m2701-P-3000", "exchange_id": "DCE"},
        {
            "InstrumentID": "m2701-P-3000",
            "ExchangeID": "DCE",
            "BrokerID": "9999",
            "InvestorID": "offline",
        },
    ),
)


def read_client():
    client = TraderClient(
        "tcp://offline", "9999", "offline", "fixture", auto_settlement_confirm=False
    )
    client._connected = True
    client._authentication_state = "authenticated"
    client._login_state = "logged_in"
    client._connection_generation = 1
    client._query_interval = 0
    return client


@pytest.mark.parametrize("public,native,callback,kind,kwargs,expected", QUERY_CASES)
def test_queries_preserve_all_packets_and_exact_request_without_writes(
    public, native, callback, kind, kwargs, expected
):
    client = read_client()
    spi = _TraderSpi(client)

    def submit(field, request_id):
        assert {key: getattr(field, key) for key in expected} == expected
        row = SimpleNamespace(InstrumentID="m2701-C-3000", Value=3.5)
        getattr(spi, callback)(row, None, request_id, False)
        row.Value = 99
        getattr(spi, callback)(SimpleNamespace(InstrumentID="m2701-P-3000"), None, request_id, True)
        return 0

    client._api = SimpleNamespace(**{native: submit})
    result = getattr(client, public)(**kwargs, timeout=0.01)
    assert result.complete and result.is_last_seen
    assert result.connection_generation == 1
    assert result.account_fingerprint == client.get_session_state()["account_fingerprint"]
    assert len(result.records) == 2
    assert result.records[0]["Value"] == 3.5
    counts = client.get_request_counts()
    assert counts[f"query_{kind}"] == 1
    assert all(counts[key] == 0 for key in ("settlement_confirm", "order_insert", "order_action"))
    assert not client.is_trading_ready


@pytest.mark.parametrize("public,native,callback,kind,kwargs,expected", QUERY_CASES)
@pytest.mark.parametrize("outcome", ["unsupported", "timeout", "error", "stale_spi"])
def test_reference_queries_never_promote_incomplete_or_wrong_session_packets(
    public, native, callback, kind, kwargs, expected, outcome
):
    client = read_client()
    spi = _TraderSpi(client)

    def submit(field, request_id):
        if outcome == "error":
            getattr(spi, callback)(
                None, SimpleNamespace(ErrorID=7, ErrorMsg="denied"), request_id, True
            )
        elif outcome == "stale_spi":
            spi._native_api = object()
            getattr(spi, callback)(SimpleNamespace(InstrumentID="wrong"), None, request_id, True)
        return 0

    client._api = SimpleNamespace(**({} if outcome == "unsupported" else {native: submit}))
    result = getattr(client, public)(**kwargs, timeout=0.001)
    assert not result.complete and result.first is None
    assert not result.records
    assert result.unsupported == (outcome == "unsupported")
    assert result.timed_out == (outcome in {"timeout", "stale_spi"})
    assert client.get_request_counts()["settlement_confirm"] == 0


@pytest.mark.parametrize("public,native,callback,kind,kwargs,expected", QUERY_CASES)
def test_feed_queries_forward_to_existing_trader(public, native, callback, kind, kwargs, expected):
    feed = object.__new__(CtpRequestDataFuture)
    sentinel = object()
    calls = []
    feed._ensure_connected = lambda: calls.append("connected")
    feed._trader = SimpleNamespace(**{public: lambda **params: (calls.append(params), sentinel)[1]})
    assert getattr(feed, public)(**kwargs, timeout=2) is sentinel
    assert calls == ["connected", {**kwargs, "timeout": 2}]


def test_all_instrument_query_keeps_options_combinations_and_native_fields():
    client = read_client()
    rows = [
        {"InstrumentID": "m2701", "ProductClass": "1", "IsTrading": 1},
        {
            "InstrumentID": "m2701-C-3000",
            "ProductClass": "2",
            "OptionsType": "1",
            "UnderlyingInstrID": "m2701",
            "StrikePrice": 3000,
            "UnderlyingMultiple": 1.0,
            "VolumeMultiple": 10,
            "PriceTick": 0.5,
            "ExpireDate": "20261207",
            "OpenDate": "20260105",
            "InstLifePhase": "1",
            "IsTrading": 1,
        },
        {"InstrumentID": "arbitrary-combination", "ProductClass": "3"},
    ]

    def submit(field, request_id):
        assert not field.InstrumentID
        for i, row in enumerate(rows):
            client._handle_query_callback("instruments", row, None, request_id, i == 2)
        return 0

    client._api = SimpleNamespace(ReqQryInstrument=submit)
    result = client.query_instruments_result(timeout=0.01)
    assert result.complete and len(result.records) == 3
    assert [row["asset_type"] for row in result.records] == ["future", "option", "combination"]
    option = result.records[1]
    assert all(option[key] == value for key, value in rows[1].items())
    assert option["underlying_instrument"] == "m2701"
    assert option["strike_price"] == 3000
    assert option["option_type"] == "call"
    assert option["multiplier"] == 10
    assert option["price_tick"] == 0.5
    assert option["underlying_multiple"] == 1
    assert option["exercise_style"] is None
    assert option["open_date"] == "20260105"
    assert option["life_phase"] == "started"
    assert option["is_trading"] is True


def test_normalization_missing_or_invalid_fields_remain_unknown():
    from bt_api_ctp import normalize_ctp_instrument

    row = normalize_ctp_instrument(
        {
            "InstrumentID": "looks-like-a-future2609",
            "PriceTick": float("inf"),
            "StrikePrice": float("nan"),
        }
    )
    assert row["asset_type"] == "unknown" and row["status"] == "unknown"
    assert row["is_trading"] is None and row["price_tick"] is None
    assert row["strike_price"] is None and row["exercise_style"] is None


@pytest.mark.parametrize("name", ["input_price", "underlying_price"])
@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True])
def test_invalid_trade_cost_price_is_rejected_before_native_query(name, value):
    client = read_client()
    client._api = SimpleNamespace(ReqQryOptionInstrTradeCost=lambda *_: pytest.fail("submitted"))
    with pytest.raises(ValueError, match="finite and non-negative"):
        client.query_option_instrument_trade_cost_result("m2701-C-3000", **{name: value})
    assert client.get_request_counts()["query_option_trade_cost"] == 0


def test_unsupported_native_reference_field_is_not_silently_omitted(monkeypatch):
    from bt_api_ctp.ctp import client as client_module

    class OldField:
        __slots__ = ("InstrumentID",)

    monkeypatch.setattr(client_module, "CThostFtdcQryDepthMarketDataField", OldField)
    client = read_client()
    client._api = SimpleNamespace(ReqQryDepthMarketData=lambda *_: pytest.fail("submitted"))
    result = client.query_depth_market_data_result(exchange_id="DCE")
    assert not result.complete and result.unsupported
    assert client.get_request_counts()["query_depth_market_data"] == 0


def test_instrument_normalization_preserves_explicit_native_quantity_limits():
    from bt_api_ctp import normalize_ctp_instrument

    result = normalize_ctp_instrument(
        {
            "ProductClass": "1",
            "MinLimitOrderVolume": 2,
            "MaxLimitOrderVolume": 100,
            "MinMarketOrderVolume": 3,
            "MaxMarketOrderVolume": 50,
        }
    )
    assert result["min_limit_order_volume"] == 2
    assert result["max_limit_order_volume"] == 100
    assert result["min_market_order_volume"] == 3
    assert result["max_market_order_volume"] == 50


@pytest.mark.parametrize(
    "product,kind",
    [("1", "future"), (b"2", "option"), ("6", "spot_option"), ("I", "mi"), ("?", "unknown")],
)
def test_spec_classifies_only_from_native_product_class(product, kind):
    from bt_api_ctp.instrument import build_ctp_instrument_spec

    spec = build_ctp_instrument_spec(
        "m2701-P-3000",
        "DCE",
        {"ProductClass": product, "OptionsType": "2", "IsTrading": 0, "VolumeMultiple": 10},
        None,
        None,
    )
    assert spec["asset_type"] == kind and spec["contract_type"] == kind
    assert spec["status"] == "disabled"
    if kind in {"option", "spot_option"}:
        assert spec["option_type"] == "put"
        assert spec["linear"] is False
