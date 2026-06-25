from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bt_api_ctp.gateway.adapter import _ctp_tick_timestamp_datetime


def test_ctp_tick_timestamp_datetime_uses_exchange_timezone() -> None:
    row = type(
        "Row",
        (),
        {
            "trading_day": "20260623",
            "update_time_val": "08:22:00",
            "update_millisec": 500,
        },
    )()

    stamp, tick_dt = _ctp_tick_timestamp_datetime(row)

    assert tick_dt == datetime(
        2026,
        6,
        23,
        8,
        22,
        0,
        500000,
        tzinfo=timezone(timedelta(hours=8)),
    )
    assert stamp == pytest.approx(1782174120.5)
    assert datetime.fromtimestamp(stamp, timezone.utc) == datetime(
        2026, 6, 23, 0, 22, 0, 500000, tzinfo=timezone.utc
    )


def test_ctp_tick_timestamp_datetime_fallback_is_utc_aware() -> None:
    row = type(
        "Row",
        (),
        {
            "trading_day": "",
            "update_time_val": "",
            "update_millisec": 0,
        },
    )()

    stamp, tick_dt = _ctp_tick_timestamp_datetime(row, fallback_time=1782174120.5)

    assert stamp == pytest.approx(1782174120.5)
    assert tick_dt == datetime(2026, 6, 23, 0, 22, 0, 500000, tzinfo=timezone.utc)
