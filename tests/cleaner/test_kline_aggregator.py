"""离线契约测试：tick → 1 分钟 K 线 → 5/15 分钟 K 线的聚合规则。"""

from __future__ import annotations

from calendar import timegm
from datetime import datetime

from bt_api_ctp.cleaner.kline.aggregator import (
    aggregate_1min,
    aggregate_from_1min,
    dedup_bars,
)
from bt_api_ctp.cleaner.kline.schema import Bar

_NS = 1_000_000_000


def _ns(action_day: str, update_time: str, millisec: int = 0) -> int:
    moment = datetime.strptime(f"{action_day} {update_time}", "%Y%m%d %H:%M:%S")
    return timegm(moment.timetuple()) * _NS + millisec * 1_000_000


def _tick(
    update_time: str = "09:00:00",
    *,
    action_day: str = "20260918",
    trading_day: str = "20260918",
    update_millisec: int = 0,
    last_price=100.0,
    volume=0,
    turnover=0.0,
    open_interest=None,
    instrument_id: str = "rb2510",
    exchange_id: str = "SHFE",
):
    return {
        "trading_day": trading_day,
        "action_day": action_day,
        "update_time": update_time,
        "update_millisec": update_millisec,
        "exchange_id": exchange_id,
        "instrument_id": instrument_id,
        "last_price": last_price,
        "volume": volume,
        "turnover": turnover,
        "open_interest": open_interest,
    }


class TestOneMinuteOhlc:
    def test_open_high_low_close_come_from_last_price(self):
        rows = [
            _tick("09:00:00", last_price=100.0, volume=10),
            _tick("09:00:10", last_price=105.0, volume=15),
            _tick("09:00:20", last_price=95.0, volume=20),
        ]

        result = aggregate_1min(rows)

        assert len(result.bars) == 1
        bar = result.bars[0]
        assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 105.0, 95.0, 95.0)
        assert bar.datetime == _ns("20260918", "09:00:00")
        assert (bar.exchange_id, bar.instrument_id, bar.trading_day) == (
            "SHFE",
            "rb2510",
            "20260918",
        )

    def test_bucket_is_clock_aligned(self):
        rows = [
            _tick("09:00:59", update_millisec=999, last_price=100.0),
            _tick("09:01:00", update_millisec=0, last_price=101.0),
        ]

        result = aggregate_1min(rows)

        assert [bar.datetime for bar in result.bars] == [
            _ns("20260918", "09:00:00"),
            _ns("20260918", "09:01:00"),
        ]

    def test_snapshots_in_the_same_second_are_ordered_by_millisecond(self):
        rows = [
            _tick("09:00:00", update_millisec=500, last_price=102.0),
            _tick("09:00:00", update_millisec=0, last_price=100.0),
            _tick("09:00:00", update_millisec=250, last_price=101.0),
        ]

        result = aggregate_1min(rows)

        bar = result.bars[0]
        assert (bar.open, bar.close) == (100.0, 102.0)

    def test_unsorted_rows_are_sorted_before_bucketing(self):
        rows = [
            _tick("09:01:00", last_price=101.0, volume=20),
            _tick("09:00:00", last_price=100.0, volume=10),
        ]

        result = aggregate_1min(rows)

        assert [bar.close for bar in result.bars] == [100.0, 101.0]


class TestSessions:
    def test_night_session_buckets_across_midnight(self):
        rows = [
            _tick(
                "23:59:59",
                action_day="20260918",
                update_millisec=500,
                trading_day="20260919",
                last_price=100.0,
            ),
            _tick(
                "00:00:00",
                action_day="20260919",
                update_millisec=500,
                trading_day="20260919",
                last_price=101.0,
            ),
        ]

        result = aggregate_1min(rows)

        assert [bar.datetime for bar in result.bars] == [
            _ns("20260918", "23:59:00"),
            _ns("20260919", "00:00:00"),
        ]
        assert [bar.trading_day for bar in result.bars] == ["20260919", "20260919"]

    def test_session_break_produces_no_empty_bars(self):
        """小节休息没有 tick，就不该凭空造出空桶。"""
        rows = [
            _tick("10:14:59", update_millisec=500, last_price=100.0, volume=10),
            _tick("10:30:01", last_price=101.0, volume=20),
        ]

        result = aggregate_1min(rows)

        assert len(result.bars) == 2
        assert [bar.datetime for bar in result.bars] == [
            _ns("20260918", "10:14:00"),
            _ns("20260918", "10:30:00"),
        ]


class TestInvalidPrices:
    def test_invalid_prices_do_not_affect_ohlc(self):
        rows = [
            _tick("09:00:00", last_price=100.0, volume=10),
            _tick("09:00:10", last_price=0.0, volume=12),
            _tick("09:00:20", last_price=float("inf"), volume=14),
            _tick("09:00:30", last_price=float("nan"), volume=16),
            _tick("09:00:40", last_price=None, volume=18),
            _tick("09:00:50", last_price=98.0, volume=20),
        ]

        result = aggregate_1min(rows)

        bar = result.bars[0]
        assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 100.0, 98.0, 98.0)
        # 成交量仍然累计：无效价只影响 OHLC，不丢成交量。
        assert bar.volume == 20

    def test_bar_without_any_valid_price_is_dropped_and_counted(self):
        rows = [
            _tick("09:00:00", last_price=0.0, volume=10),
            _tick("09:00:10", last_price=float("inf"), volume=12),
        ]

        result = aggregate_1min(rows)

        assert result.bars == []
        assert result.bars_without_price == 1


class TestVolumeAndAmountDifferencing:
    def test_deltas_sum_to_the_final_cumulative_volume(self):
        rows = [
            _tick("09:00:00", volume=10, turnover=1000.0),
            _tick("09:00:10", volume=15, turnover=1600.0),
            _tick("09:01:00", volume=30, turnover=3100.0),
        ]

        result = aggregate_1min(rows)

        assert [bar.volume for bar in result.bars] == [15, 15]
        assert [bar.amount for bar in result.bars] == [1600.0, 1500.0]
        assert sum(bar.volume for bar in result.bars) == 30
        assert sum(bar.amount for bar in result.bars) == 3100.0

    def test_counter_reset_restarts_from_the_new_accumulation(self):
        rows = [
            _tick("09:00:00", volume=100, turnover=5000.0),
            _tick("09:01:00", volume=5, turnover=200.0),  # 累计值回退 = 跨日重置
        ]

        result = aggregate_1min(rows)

        assert [bar.volume for bar in result.bars] == [100, 5]

    def test_missing_volume_contributes_zero(self):
        rows = [
            _tick("09:00:00", last_price=100.0, volume=None),
            _tick("09:00:10", last_price=101.0, volume=7),
        ]

        result = aggregate_1min(rows)

        bar = result.bars[0]
        assert bar.volume == 7

    def test_open_interest_is_the_last_value_of_the_bucket(self):
        rows = [
            _tick("09:00:00", open_interest=1000.0),
            _tick("09:00:10", open_interest=1200.0),
            _tick("09:00:20", open_interest=None),
        ]

        result = aggregate_1min(rows)

        assert result.bars[0].open_interest == 1200.0


class TestUnparseableRows:
    def test_bad_timestamps_are_counted_not_silently_dropped(self):
        rows = [
            _tick("09:00:00", last_price=100.0),
            {"action_day": None, "update_time": None, "last_price": 100.0},
            {"action_day": "20260918", "update_time": "not-a-time", "last_price": 100.0},
        ]

        result = aggregate_1min(rows)

        assert result.rows == 1
        assert result.unparseable_rows == 2


class TestRollUp:
    def _one_minute_bars(self) -> list[Bar]:
        rows = []
        for minute in range(5):
            time = f"09:0{minute}:00"
            rows.append(_tick(time, last_price=100.0 + minute, volume=(minute + 1) * 10))
            rows.append(_tick(time, update_millisec=500, last_price=90.0 + minute))
        return aggregate_1min(rows).bars

    def test_five_minute_bar_aggregates_ohlc_and_sums_volume(self):
        bars = self._one_minute_bars()

        rolled = aggregate_from_1min(bars, 5)

        assert len(rolled) == 1
        bar = rolled[0]
        assert bar.datetime == _ns("20260918", "09:00:00")
        assert bar.open == 100.0
        assert bar.close == 94.0
        assert bar.high == 104.0
        assert bar.low == 90.0
        assert bar.volume == sum(one.volume for one in bars)

    def test_fifteen_minute_volume_equals_one_minute_total(self):
        bars = self._one_minute_bars()

        rolled = aggregate_from_1min(bars, 15)

        assert sum(bar.volume for bar in rolled) == sum(bar.volume for bar in bars)

    def test_roll_up_keeps_clock_alignment(self):
        rows = [
            _tick("09:00:00", last_price=100.0),
            _tick("09:04:59", update_millisec=500, last_price=101.0),
            _tick("09:05:00", last_price=102.0),
        ]
        bars = aggregate_1min(rows).bars

        rolled = aggregate_from_1min(bars, 5)

        assert [bar.datetime for bar in rolled] == [
            _ns("20260918", "09:00:00"),
            _ns("20260918", "09:05:00"),
        ]

    def test_minutes_below_two_returns_the_input(self):
        bars = self._one_minute_bars()

        assert aggregate_from_1min(bars, 1) == bars


class TestDedupBars:
    def test_same_bucket_keeps_the_newest_and_sorts(self):
        older = Bar(
            datetime=_ns("20260918", "09:01:00"),
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1,
            amount=1.0,
            open_interest=None,
            trading_day="20260918",
            exchange_id="SHFE",
            instrument_id="rb2510",
        )
        newer = Bar(
            datetime=_ns("20260918", "09:00:00"),
            open=2.0,
            high=2.0,
            low=2.0,
            close=2.0,
            volume=2,
            amount=2.0,
            open_interest=None,
            trading_day="20260918",
            exchange_id="SHFE",
            instrument_id="rb2510",
        )
        replacement = Bar(
            datetime=_ns("20260918", "09:01:00"),
            open=9.0,
            high=9.0,
            low=9.0,
            close=9.0,
            volume=9,
            amount=9.0,
            open_interest=None,
            trading_day="20260918",
            exchange_id="SHFE",
            instrument_id="rb2510",
        )

        result = dedup_bars([older, newer, replacement])

        assert [bar.datetime for bar in result] == [
            _ns("20260918", "09:00:00"),
            _ns("20260918", "09:01:00"),
        ]
        assert result[1].close == 9.0
