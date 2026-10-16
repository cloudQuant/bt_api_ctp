from __future__ import annotations

import pytest

from bt_api_ctp.containers.ctp.ctp_order import CtpOrderData
from bt_api_ctp.containers.ctp.ctp_trade import CtpTradeData
from bt_api_ctp.gateway import adapter


@pytest.mark.parametrize(
    ("direction", "offset", "expected_side", "expected_bucket"),
    [
        ("1", "3", "LONG", "TODAY"),
        ("0", "4", "SHORT", "YESTERDAY"),
        ("1", "1", "LONG", "ALL"),
    ],
)
def test_order_adapter_derives_position_dimensions_from_ctp_codes(
    direction, offset, expected_side, expected_bucket
):
    instrument = "IF2610"
    row = CtpOrderData(
        {
            "InstrumentID": instrument,
            "OrderRef": "projection-order",
            "OrderSysID": "ctp-order-1",
            "Direction": direction,
            "CombOffsetFlag": offset,
            "LimitPrice": 4000,
            "VolumeTotalOriginal": 1,
            "VolumeTraded": 1,
            "VolumeTotal": 0,
            "OrderStatus": "0",
            "ExchangeID": "CFFEX",
        },
        instrument,
        "FUTURE",
        True,
    ).init_data()

    projected = adapter._order(row, {instrument: {instrument}})

    assert projected["position_dimensions_source"] == "ctp_side_offset_v1"
    assert projected["ctp_direction_code"] == direction
    assert projected["ctp_offset_code"] == offset
    assert projected["position_side"] == expected_side
    assert projected["position_bucket"] == expected_bucket


@pytest.mark.parametrize(
    ("direction", "offset", "expected_side", "expected_bucket"),
    [
        ("1", "3", "LONG", "TODAY"),
        ("0", "4", "SHORT", "YESTERDAY"),
        ("1", "1", "LONG", "ALL"),
    ],
)
def test_trade_adapter_derives_position_dimensions_from_ctp_codes(
    direction, offset, expected_side, expected_bucket
):
    instrument = "IF2610"
    row = CtpTradeData(
        {
            "InstrumentID": instrument,
            "OrderRef": "projection-order",
            "OrderSysID": "ctp-order-1",
            "TradeID": "ctp-trade-1",
            "Direction": direction,
            "OffsetFlag": offset,
            "Price": 4000,
            "Volume": 1,
            "ExchangeID": "CFFEX",
        },
        instrument,
        "FUTURE",
        True,
    ).init_data()

    projected = adapter._trade(row, {instrument: {instrument}})

    assert projected["position_dimensions_source"] == "ctp_side_offset_v1"
    assert projected["ctp_direction_code"] == direction
    assert projected["ctp_offset_code"] == offset
    assert projected["position_side"] == expected_side
    assert projected["position_bucket"] == expected_bucket


@pytest.mark.parametrize(
    ("direction", "offset"),
    [("9", "3"), ("1", "9"), ("0", "2"), ("0", "5"), ("0", "6")],
)
def test_order_adapter_marks_unknown_or_force_close_dimensions_unresolved(direction, offset):
    instrument = "IF2610"
    row = CtpOrderData(
        {
            "InstrumentID": instrument,
            "OrderRef": "projection-order",
            "OrderSysID": "ctp-order-1",
            "Direction": direction,
            "CombOffsetFlag": offset,
            "LimitPrice": 4000,
            "VolumeTotalOriginal": 1,
            "VolumeTraded": 1,
            "VolumeTotal": 0,
            "OrderStatus": "0",
            "ExchangeID": "CFFEX",
        },
        instrument,
        "FUTURE",
        True,
    ).init_data()

    projected = adapter._order(row, {instrument: {instrument}})

    assert projected["position_dimensions_source"] == "ctp_side_offset_unresolved_v1"
    assert projected["position_side"] == "UNRESOLVED"
    assert projected["position_bucket"] == "UNRESOLVED"
