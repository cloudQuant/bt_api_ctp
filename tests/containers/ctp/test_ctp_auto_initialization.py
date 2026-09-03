"""Regression tests for CTP containers' lazy parsing contract."""

from __future__ import annotations

import pytest

from bt_api_ctp.containers.ctp.ctp_account import CtpAccountData
from bt_api_ctp.containers.ctp.ctp_bar import CtpBarData
from bt_api_ctp.containers.ctp.ctp_order import CtpOrderData
from bt_api_ctp.containers.ctp.ctp_position import CtpPositionData
from bt_api_ctp.containers.ctp.ctp_ticker import CtpTickerData
from bt_api_ctp.containers.ctp.ctp_trade import CtpTradeData


@pytest.mark.parametrize(
    ("container_factory", "getter_name", "expected"),
    [
        (lambda: CtpAccountData({"Balance": 123.5}), "get_margin", 123.5),
        (
            lambda: CtpBarData({"open_time": "20250404 09:00:00"}),
            "get_open_time",
            "20250404 09:00:00",
        ),
        (lambda: CtpOrderData({"OrderRef": "ORD-1"}), "get_client_order_id", "ORD-1"),
        (lambda: CtpPositionData({"InstrumentID": "rb2505"}), "get_symbol_name", "rb2505"),
        (lambda: CtpTickerData({"LastPrice": 3500.5}), "get_last_price", 3500.5),
        (
            lambda: CtpTradeData({"TradeID": "TRD-1"}, symbol_name="rb2505"),
            "get_trade_id",
            "TRD-1",
        ),
    ],
    ids=["account", "bar", "order", "position", "ticker", "trade"],
)
def test_getters_parse_ctp_data_on_first_access(container_factory, getter_name, expected):
    """Every CTP container parses its payload before exposing a getter result."""
    container = container_factory()

    assert getattr(container, getter_name)() == expected
