from __future__ import annotations

from types import SimpleNamespace

import pytest

from bt_api_ctp.containers.ctp.ctp_account import CtpAccountData
from bt_api_ctp.containers.ctp.ctp_order import CtpOrderData
from bt_api_ctp.containers.ctp.ctp_position import CtpPositionData
from bt_api_ctp.feeds.live_ctp_feed import CTP_OFFSET_FLAG
from bt_api_ctp.gateway import adapter as adapter_module


class _FakeRequest:
    def __init__(self, rows, status=True):
        self._rows = rows
        self._status = status

    def get_data(self):
        return self._rows

    def get_status(self):
        return self._status


class _FakeTrader:
    def __init__(self):
        self.query_exchange_ids = []

    def query_instrument(self, instrument, exchange_id="", timeout=5):
        assert instrument == "IF2506"
        self.query_exchange_ids.append(("instrument", exchange_id))
        return SimpleNamespace(
            InstrumentID="IF2506",
            ExchangeID=exchange_id or "CFFEX",
            ProductID="IF",
            VolumeMultiple=300,
            PriceTick=0.2,
        )

    def query_instrument_margin_rate(self, instrument, exchange_id="", timeout=5):
        assert instrument == "IF2506"
        self.query_exchange_ids.append(("margin", exchange_id))
        return SimpleNamespace(
            InstrumentID="IF2506",
            ExchangeID=exchange_id or "CFFEX",
            LongMarginRatioByMoney=0.12,
            ShortMarginRatioByMoney=0.13,
            LongMarginRatioByVolume=0.0,
            ShortMarginRatioByVolume=0.0,
        )

    def query_instrument_commission_rate(self, instrument, exchange_id="", timeout=5):
        assert instrument == "IF2506"
        self.query_exchange_ids.append(("commission", exchange_id))
        return SimpleNamespace(
            InstrumentID="IF2506",
            ExchangeID=exchange_id or "CFFEX",
            OpenRatioByMoney=0.000023,
            OpenRatioByVolume=0.0,
            CloseRatioByMoney=0.000023,
            CloseRatioByVolume=0.0,
            CloseTodayRatioByMoney=0.000345,
            CloseTodayRatioByVolume=0.0,
        )


class _FakeFeed:
    def __init__(self, *_args, **_kwargs):
        self.trader_client = _FakeTrader()
        self._trader = self.trader_client
        self.last_order = None

    def get_position(self):
        row = CtpPositionData(
            {
                "InstrumentID": "IF2506",
                "ExchangeID": "CFFEX",
                "PosiDirection": "2",
                "Position": 10,
                "TodayPosition": 4,
                "YdPosition": 6,
                "PositionCost": 12_000_000.0,
                "OpenCost": 12_000_000.0,
                "UseMargin": 1_440_000.0,
                "PositionProfit": 30_000.0,
                "Commission": 45.0,
                "SettlementPrice": 4010.0,
            }
        )
        return _FakeRequest([row])

    def get_account(self):
        row = CtpAccountData(
            {
                "AccountID": "089763",
                "Balance": 500_000.0,
                "Available": 300_000.0,
                "CurrMargin": 150_000.0,
                "PositionProfit": 5_000.0,
                "CloseProfit": 1_000.0,
                "Commission": 200.0,
                "FrozenMargin": 10_000.0,
                "PreBalance": 495_000.0,
            }
        )
        return _FakeRequest([row])

    def make_order(
        self,
        symbol,
        volume,
        price=None,
        order_type="buy-limit",
        offset="open",
        client_order_id=None,
        exchange_id="",
        **_kwargs,
    ):
        self.last_order = {
            "symbol": symbol,
            "volume": volume,
            "price": price,
            "order_type": order_type,
            "offset": offset,
            "offset_flag": CTP_OFFSET_FLAG[offset],
            "client_order_id": client_order_id,
            "exchange_id": exchange_id,
        }
        side, _kind = order_type.split("-", 1)
        row = CtpOrderData(
            {
                "InstrumentID": symbol,
                "OrderRef": client_order_id,
                "Direction": "0" if side == "buy" else "1",
                "CombOffsetFlag": CTP_OFFSET_FLAG[offset],
                "LimitPrice": price,
                "VolumeTotalOriginal": volume,
                "VolumeTraded": 0,
                "VolumeTotal": volume,
                "OrderStatus": "a",
                "ExchangeID": exchange_id,
                "FrontID": 11,
                "SessionID": 22,
            },
            symbol,
            "FUTURE",
            True,
        )
        return _FakeRequest([row])

    def get_open_orders(self):
        return _FakeRequest(
            [
                CtpOrderData(
                    {
                        "InstrumentID": "IF2506",
                        "OrderRef": "open-1",
                        "OrderSysID": "SYS001",
                        "Direction": "1",
                        "CombOffsetFlag": "3",
                        "LimitPrice": 4010.0,
                        "VolumeTotalOriginal": 2,
                        "VolumeTraded": 1,
                        "VolumeTotal": 1,
                        "OrderStatus": "1",
                        "ExchangeID": "CFFEX",
                        "FrontID": 11,
                        "SessionID": 22,
                    },
                    "IF2506",
                    "FUTURE",
                    True,
                ),
                CtpOrderData(
                    {
                        "InstrumentID": "IF2506",
                        "OrderRef": "done-1",
                        "OrderSysID": "SYS002",
                        "Direction": "0",
                        "CombOffsetFlag": "0",
                        "LimitPrice": 4000.0,
                        "VolumeTotalOriginal": 1,
                        "VolumeTraded": 1,
                        "VolumeTotal": 0,
                        "OrderStatus": "0",
                        "ExchangeID": "CFFEX",
                    },
                    "IF2506",
                    "FUTURE",
                    True,
                ),
            ]
        )


class _FakeStream:
    def __init__(self, *_args, **_kwargs):
        self.trader_client = _FakeTrader()

    def stop(self):
        pass


def test_ctp_gateway_positions_include_contract_specs_and_exchange_pnl(monkeypatch):
    monkeypatch.setattr(adapter_module, "CtpMarketStream", _FakeStream)
    monkeypatch.setattr(adapter_module, "CtpTradeStream", _FakeStream)
    monkeypatch.setattr(adapter_module, "CtpRequestDataFuture", _FakeFeed)

    adapter = adapter_module.CtpGatewayAdapter()
    adapter.last_price["IF2506"] = 4020.0

    rows = adapter.get_positions()

    assert len(rows) == 1
    row = rows[0]
    assert adapter.feed.trader_client.query_exchange_ids == [
        ("instrument", "CFFEX"),
        ("margin", "CFFEX"),
        ("commission", "CFFEX"),
    ]
    assert row["instrument"] == "IF2506"
    assert row["exchange_id"] == "CFFEX"
    assert row["price"] == pytest.approx(4000.0)
    assert row["current_price"] == pytest.approx(4020.0)
    assert row["multiplier"] == pytest.approx(300.0)
    assert row["margin_rate"] == pytest.approx(0.12)
    assert row["short_margin_rate"] == pytest.approx(0.13)
    assert row["open_fee_rate"] == pytest.approx(0.000023)
    assert row["close_yesterday_fee_rate"] == pytest.approx(0.000023)
    assert row["close_today_fee_rate"] == pytest.approx(0.000345)
    assert row["profit"] == pytest.approx(30_000.0)
    assert row["commission"] == pytest.approx(45.0)
    assert row["margin_value"] == pytest.approx(1_440_000.0)


def test_ctp_gateway_balance_uses_balance_as_equity_and_curr_margin_as_used_margin(monkeypatch):
    monkeypatch.setattr(adapter_module, "CtpMarketStream", _FakeStream)
    monkeypatch.setattr(adapter_module, "CtpTradeStream", _FakeStream)
    monkeypatch.setattr(adapter_module, "CtpRequestDataFuture", _FakeFeed)

    adapter = adapter_module.CtpGatewayAdapter()

    balance = adapter.get_balance()

    assert balance["account_id"] == "089763"
    assert balance["value"] == pytest.approx(500_000.0)
    assert balance["equity"] == pytest.approx(500_000.0)
    assert balance["balance"] == pytest.approx(500_000.0)
    assert balance["cash"] == pytest.approx(300_000.0)
    assert balance["margin_free"] == pytest.approx(300_000.0)
    assert balance["margin"] == pytest.approx(150_000.0)
    assert balance["used_margin"] == pytest.approx(150_000.0)
    assert balance["profit"] == pytest.approx(5_000.0)


def test_ctp_gateway_place_order_preserves_request_id_and_exchange_prefixed_symbol(monkeypatch):
    monkeypatch.setattr(adapter_module, "CtpMarketStream", _FakeStream)
    monkeypatch.setattr(adapter_module, "CtpTradeStream", _FakeStream)
    monkeypatch.setattr(adapter_module, "CtpRequestDataFuture", _FakeFeed)

    adapter = adapter_module.CtpGatewayAdapter()
    adapter.last_price["rb2501"] = 3500.0

    result = adapter.place_order(
        {
            "symbol": "SHFE.rb2501",
            "side": "sell",
            "size": "2.0",
            "price": 0,
            "order_type": "market",
            "offset": "close_today",
            "request_id": "req-ctp-1",
        }
    )

    sent = adapter.feed.last_order
    assert sent == {
        "symbol": "rb2501",
        "volume": 2,
        "price": pytest.approx(3495.0),
        "order_type": "sell-limit",
        "offset": "close_today",
        "offset_flag": "3",
        "client_order_id": "req-ctp-1",
        "exchange_id": "SHFE",
    }
    assert result["order_ref"] == "req-ctp-1"
    assert result["details"]["request_id"] == "req-ctp-1"


@pytest.mark.parametrize(("offset", "flag"), [("close", "1"), ("close_yesterday", "4")])
def test_ctp_gateway_place_order_preserves_close_offsets(monkeypatch, offset, flag):
    monkeypatch.setattr(adapter_module, "CtpMarketStream", _FakeStream)
    monkeypatch.setattr(adapter_module, "CtpTradeStream", _FakeStream)
    monkeypatch.setattr(adapter_module, "CtpRequestDataFuture", _FakeFeed)

    adapter = adapter_module.CtpGatewayAdapter()
    adapter.last_price["IF2506"] = 4000.0

    result = adapter.place_order(
        {
            "symbol": "IF2506.CFFEX",
            "side": "buy",
            "size": 1,
            "price": 4000.0,
            "offset": offset,
            "request_id": f"req-{offset}",
        }
    )

    assert adapter.feed.last_order["offset"] == offset
    assert adapter.feed.last_order["offset_flag"] == flag
    assert result["order_ref"] == f"req-{offset}"


def test_ctp_gateway_get_open_orders_returns_remaining_orders(monkeypatch):
    monkeypatch.setattr(adapter_module, "CtpMarketStream", _FakeStream)
    monkeypatch.setattr(adapter_module, "CtpTradeStream", _FakeStream)
    monkeypatch.setattr(adapter_module, "CtpRequestDataFuture", _FakeFeed)

    adapter = adapter_module.CtpGatewayAdapter()

    orders = adapter.get_open_orders()

    assert len(orders) == 1
    assert orders[0]["id"] == "SYS001"
    assert orders[0]["order_ref"] == "open-1"
    assert orders[0]["data_name"] == "IF2506"
    assert orders[0]["side"] == "sell"
    assert orders[0]["offset"] == "close_today"
    assert orders[0]["remaining"] == 1


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"symbol": "IF2506.CFFEX", "side": "buy", "size": 1.5, "price": 4000}, "positive integer"),
        ({"symbol": "IF2506.CFFEX", "side": "buy", "size": 0, "price": 4000}, "positive integer"),
        ({"symbol": "IF2506.CFFEX", "side": "buy", "size": 1, "price": 0}, "positive price"),
        (
            {"symbol": "IF2506.CFFEX", "side": "buy", "size": 1, "offset": "bad", "price": 4000},
            "offset",
        ),
        ({"symbol": "IF2506.CFFEX", "side": "hold", "size": 1, "price": 4000}, "side"),
    ],
)
def test_ctp_gateway_place_order_rejects_unsafe_payload(monkeypatch, payload, error):
    monkeypatch.setattr(adapter_module, "CtpMarketStream", _FakeStream)
    monkeypatch.setattr(adapter_module, "CtpTradeStream", _FakeStream)
    monkeypatch.setattr(adapter_module, "CtpRequestDataFuture", _FakeFeed)

    adapter = adapter_module.CtpGatewayAdapter()

    with pytest.raises(ValueError, match=error):
        adapter.place_order(payload)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("SHFE.rb2501", ("rb2501", "SHFE")),
        ("rb2501.SHFE", ("rb2501", "SHFE")),
        ("SHFE_rb2501", ("rb2501", "SHFE")),
        ("rb2501_SHFE", ("rb2501", "SHFE")),
        ("CFFEX.IF2506", ("IF2506", "CFFEX")),
        ("IF2506.CFFEX", ("IF2506", "CFFEX")),
        ("CF2609.CZCE", ("CF609", "CZCE")),
    ],
)
def test_ctp_gateway_split_supports_exchange_and_symbol_orders(value, expected):
    assert adapter_module._split(value) == expected
