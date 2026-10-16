from __future__ import annotations

import pytest

from bt_api_ctp.containers.ctp.ctp_order import CtpOrderData
from bt_api_ctp.containers.ctp.ctp_trade import CtpTradeData


@pytest.mark.parametrize(
    ("direction", "offset", "expected_side", "expected_bucket"),
    [
        ("1", "3", "LONG", "TODAY"),
        ("0", "4", "SHORT", "YESTERDAY"),
        ("1", "1", "LONG", "ALL"),
    ],
)
def test_order_container_exposes_position_dimensions_from_ctp_codes(
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

    projected = row.get_all_data()

    assert projected["position_dimensions_source"] == "ctp_side_offset_v1"
    assert projected["ctp_direction_code"] == direction
    assert projected["ctp_offset_code"] == offset
    assert projected["position_side"] == expected_side
    assert projected["position_bucket"] == expected_bucket
    assert projected["exchange_name"] == "CTP"
    assert projected["symbol_name"] == instrument


@pytest.mark.parametrize(
    ("direction", "offset", "expected_side", "expected_bucket"),
    [
        ("1", "3", "LONG", "TODAY"),
        ("0", "4", "SHORT", "YESTERDAY"),
        ("1", "1", "LONG", "ALL"),
    ],
)
def test_trade_container_exposes_position_dimensions_from_ctp_codes(
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

    projected = row.get_all_data()

    assert projected["position_dimensions_source"] == "ctp_side_offset_v1"
    assert projected["ctp_direction_code"] == direction
    assert projected["ctp_offset_code"] == offset
    assert projected["position_side"] == expected_side
    assert projected["position_bucket"] == expected_bucket
    assert projected["exchange_name"] == "CTP"
    assert projected["instrument_id"] == instrument


@pytest.mark.parametrize(
    ("direction", "offset"),
    [("9", "3"), ("1", "9"), ("0", "2"), ("0", "5"), ("0", "6")],
)
def test_order_container_marks_unknown_or_force_close_dimensions_unresolved(direction, offset):
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

    projected = row.get_all_data()

    assert projected["position_dimensions_source"] == "ctp_side_offset_unresolved_v1"
    assert projected["position_side"] == "UNRESOLVED"
    assert projected["position_bucket"] == "UNRESOLVED"


@pytest.mark.parametrize(("direction", "offset"), [("9", "3"), ("1", "9"), ("0", "2")])
def test_trade_container_marks_unknown_or_force_close_dimensions_unresolved(direction, offset):
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

    projected = row.get_all_data()

    assert projected["position_dimensions_source"] == "ctp_side_offset_unresolved_v1"
    assert projected["position_side"] == "UNRESOLVED"
    assert projected["position_bucket"] == "UNRESOLVED"


def test_open_order_does_not_claim_a_close_position_bucket():
    row = CtpOrderData(
        {
            "InstrumentID": "IF2610",
            "Direction": "0",
            "CombOffsetFlag": "0",
            "VolumeTotalOriginal": 1,
        },
        "IF2610",
        "FUTURE",
    ).init_data()

    projected = row.get_all_data()

    assert "position_dimensions_source" not in projected
    assert "ctp_direction_code" not in projected
    assert "ctp_offset_code" not in projected
    assert projected["position_side"] is None


def test_normalized_side_mismatch_is_explicitly_unresolved():
    row = CtpTradeData(
        {
            "InstrumentID": "IF2610",
            "Direction": "1",
            "OffsetFlag": "3",
            "Volume": 1,
        },
        "IF2610",
        "FUTURE",
    ).init_data()
    row.direction = "buy"

    projected = row.get_all_data()

    assert projected["position_dimensions_source"] == "ctp_side_offset_unresolved_v1"
    assert projected["ctp_direction_code"] == "1"
    assert projected["ctp_offset_code"] == "3"
    assert projected["position_side"] == "UNRESOLVED"
    assert projected["position_bucket"] == "UNRESOLVED"
