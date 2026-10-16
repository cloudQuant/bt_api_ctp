from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bt_api_ctp.containers.ctp.ctp_ticker import CtpTickerData


def test_native_ticker_resolves_exchange_action_day_to_utc() -> None:
    row = CtpTickerData(
        {
            "InstrumentID": "rb2610",
            "ActionDay": "20260623",
            "UpdateTime": "08:22:00",
            "UpdateMillisec": 500,
        },
        recv_time_utc=datetime(2026, 6, 23, 0, 22, 1, tzinfo=timezone.utc),
    )

    event_time = row.resolve_event_time()

    assert event_time == datetime(2026, 6, 23, 0, 22, 0, 500000, tzinfo=timezone.utc)
    assert row.get_server_time() == pytest.approx(1782174120.5)
    assert row.event_time_source == "action_day"


def test_native_ticker_uses_explicit_receive_time_when_action_day_is_missing() -> None:
    received = datetime(2026, 6, 23, 0, 22, 0, 500000, tzinfo=timezone.utc)
    row = CtpTickerData(
        {
            "InstrumentID": "rb2610",
            "ActionDay": "",
            "UpdateTime": "",
            "UpdateMillisec": 0,
        },
        recv_time_utc=received,
    )

    event_time = row.resolve_event_time()

    assert event_time is received
    assert row.get_server_time() == pytest.approx(1782174120.5)
    assert row.event_time_source == "receive_fallback"
    assert "AMBIGUOUS_EVENT_DATE" in row.quality_flags


def test_native_account_and_position_dtos_keep_query_values() -> None:
    from bt_api_ctp.containers.ctp.ctp_account import CtpAccountData
    from bt_api_ctp.containers.ctp.ctp_position import CtpPositionData

    account = CtpAccountData(
        {
            "AccountID": "089763",
            "Balance": 500_000.0,
            "Available": 300_000.0,
            "CurrMargin": 150_000.0,
            "PositionProfit": 5_000.0,
            "Commission": 200.0,
        }
    ).get_all_data()
    position = CtpPositionData(
        {
            "InstrumentID": "IF2506",
            "ExchangeID": "CFFEX",
            "PosiDirection": "2",
            "Position": 10,
            "TodayPosition": 4,
            "YdPosition": 6,
            "PositionCost": 12_000_000.0,
            "UseMargin": 1_440_000.0,
            "PositionProfit": 30_000.0,
            "Commission": 45.0,
        }
    ).get_all_data()

    assert account["account_id"] == "089763"
    assert account["balance"] == 500_000.0
    assert account["available"] == 300_000.0
    assert account["curr_margin"] == 150_000.0
    assert account["position_profit"] == 5_000.0
    assert position["instrument_id"] == "IF2506"
    assert position["exchange_id"] == "CFFEX"
    assert position["position_direction"] == "long"
    assert position["position_volume"] == 10
    assert position["today_position"] == 4
    assert position["yd_position"] == 6
    assert position["position_cost"] == 12_000_000.0
    assert position["use_margin"] == 1_440_000.0
    assert position["position_profit"] == 30_000.0
    assert position["commission"] == 45.0
