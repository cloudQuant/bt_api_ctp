"""离线契约测试：交易日历与夜盘判断。

参照日期：2026-09-16 为周三。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from bt_api_ctp.collector.schedule import (
    NIGHT_CLOSE_TIERS,
    TradingCalendar,
    covered_session_seconds,
    is_quote_window,
)


class TestCoveredSessionSeconds:
    """覆盖率评分需要一个"窗口内本应有多少个交易秒"的口径（整改方案 P2-5）。"""

    def test_within_one_session(self):
        start = datetime(2026, 9, 16, 9, 0, 0)
        end = datetime(2026, 9, 16, 9, 10, 0)
        assert covered_session_seconds(start, end) == pytest.approx(600.0)

    def test_scheduled_break_is_excluded(self):
        start = datetime(2026, 9, 16, 10, 0, 0)
        end = datetime(2026, 9, 16, 10, 40, 0)
        # 10:00-10:15 与 10:30-10:40；10:15-10:30 小节休息不计
        assert covered_session_seconds(start, end) == pytest.approx(1500.0)

    def test_night_session_crossing_midnight(self):
        start = datetime(2026, 9, 16, 23, 0, 0)
        end = datetime(2026, 9, 17, 1, 0, 0)
        assert covered_session_seconds(start, end) == pytest.approx(7200.0)

    def test_close_to_open_jump_is_excluded(self):
        start = datetime(2026, 9, 16, 15, 0, 0)
        end = datetime(2026, 9, 16, 21, 30, 0)
        # 15:00-15:15 + 21:00-21:30
        assert covered_session_seconds(start, end) == pytest.approx(2700.0)

    def test_outside_sessions_is_zero(self):
        start = datetime(2026, 9, 16, 3, 0, 0)
        end = datetime(2026, 9, 16, 5, 0, 0)
        assert covered_session_seconds(start, end) == 0.0

    def test_empty_window_is_zero(self):
        moment = datetime(2026, 9, 16, 9, 30, 0)
        assert covered_session_seconds(moment, moment) == 0.0

    def test_weekend_between_two_trading_days_is_not_counted(self):
        """周五夜盘 + 周一日盘同属一个交易日，中间两天休市不得计入分母。"""
        start = datetime(2026, 9, 18, 21, 0, 0)  # 周五晚（归属下周一交易日）
        end = datetime(2026, 9, 21, 15, 0, 0)  # 周一日盘 15:00

        covered = covered_session_seconds(start, end)

        # 周五夜盘 5.5h + 周一 09:00-10:15 / 10:30-11:30 / 13:00-15:00 共 4.25h
        assert covered == pytest.approx((5.5 + 4.25) * 3600)

    def test_holiday_is_not_counted(self):
        calendar = TradingCalendar(holidays=frozenset({"20260921"}))
        start = datetime(2026, 9, 18, 21, 0, 0)
        end = datetime(2026, 9, 22, 15, 0, 0)  # 周二

        covered = covered_session_seconds(start, end, calendar)

        # 周一休市（周五晚因此也没有夜盘），只剩周二日盘 4.25h
        assert covered == pytest.approx(4.25 * 3600)


class TestQuoteWindow:
    """开盘集合竞价（08:55 / 20:55）是真实行情，不能当非时段数据丢掉。"""

    def test_pre_open_auction_is_a_quote_window(self):
        assert is_quote_window(datetime(2026, 9, 16, 8, 55, 0)) is True
        assert is_quote_window(datetime(2026, 9, 16, 20, 55, 0)) is True

    def test_subscribe_time_snapshot_is_not_a_quote_window(self):
        assert is_quote_window(datetime(2026, 9, 16, 20, 18, 42)) is False

    def test_regular_session_is_a_quote_window(self):
        assert is_quote_window(datetime(2026, 9, 16, 9, 30, 0)) is True
        assert is_quote_window(datetime(2026, 9, 16, 23, 59, 0)) is True

    def test_after_close_is_not_a_quote_window(self):
        assert is_quote_window(datetime(2026, 9, 16, 15, 16, 0)) is False
        assert is_quote_window(datetime(2026, 9, 16, 20, 50, 0)) is False


class TestTradingCalendarBasics:
    def test_weekend_is_not_trading_day(self):
        calendar = TradingCalendar()
        assert calendar.is_trading_day("20260919") is False  # 周六
        assert calendar.is_trading_day("20260920") is False  # 周日

    def test_weekday_is_trading_day(self):
        calendar = TradingCalendar()
        assert calendar.is_trading_day("20260916") is True  # 周三
        assert calendar.is_trading_day("20260918") is True  # 周五

    def test_holiday_is_not_trading_day(self):
        calendar = TradingCalendar(holidays=frozenset({"20261001"}))
        assert calendar.is_trading_day("20261001") is False  # 周四，但为节假日

    def test_night_close_tiers_cover_three_schedules(self):
        assert NIGHT_CLOSE_TIERS == ("23:00", "01:00", "02:30")


class TestNightSessionOnCalendarDay:
    """某自然日的晚上是否有夜盘（夜盘归属规则）。

    - 周五晚的夜盘 + 周一日盘 = 一个完整交易日
    - 周日晚没有夜盘
    - 法定节假日前一交易日晚暂停夜盘
    """

    def test_friday_evening_opens_the_next_monday(self):
        # 周五(20260918)晚夜盘属于下周一(20260921)交易日
        assert TradingCalendar().has_night_session("20260918") is True

    def test_sunday_evening_never_trades(self):
        assert TradingCalendar().has_night_session("20260920") is False

    def test_saturday_evening_never_trades(self):
        assert TradingCalendar().has_night_session("20260919") is False

    def test_weekday_evening_trades(self):
        # 周三(20260916)晚夜盘属于周四
        assert TradingCalendar().has_night_session("20260916") is True

    def test_monday_evening_trades(self):
        # 周一(20260921)晚夜盘属于周二
        assert TradingCalendar().has_night_session("20260921") is True

    def test_holiday_evening_never_trades(self):
        calendar = TradingCalendar(holidays=frozenset({"20261001"}))
        assert calendar.has_night_session("20261001") is False

    def test_evening_before_holiday_break_is_suspended(self):
        holidays = frozenset(
            {"20261001", "20261002", "20261005", "20261006", "20261007"}
        )
        calendar = TradingCalendar(holidays=holidays)
        # 节前最后交易日 20260930(周三) 晚暂停夜盘
        assert calendar.has_night_session("20260930") is False

    def test_first_evening_after_the_break_trades(self):
        holidays = frozenset(
            {"20261001", "20261002", "20261005", "20261006", "20261007"}
        )
        calendar = TradingCalendar(holidays=holidays)
        # 节后首日 20261008(周四) 晚有夜盘（属于 20261009）
        assert calendar.has_night_session("20261008") is True


class TestPreviousTradingDay:
    def test_skips_weekend(self):
        calendar = TradingCalendar()
        assert calendar.previous_trading_day("20260921") == "20260918"  # 周一 → 上周五

    def test_skips_holiday(self):
        calendar = TradingCalendar(holidays=frozenset({"20260916"}))
        assert calendar.previous_trading_day("20260917") == "20260915"


class TestCalendarFromFile:
    def test_loads_holidays_json(self, tmp_path):
        path = tmp_path / "holidays.json"
        path.write_text(json.dumps(["20261001", "20261002"]), encoding="utf-8")

        calendar = TradingCalendar.from_file(path)

        assert calendar.is_trading_day("20261001") is False
        assert calendar.is_trading_day("20261002") is False
        assert calendar.is_trading_day("20261008") is True

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            TradingCalendar.from_file(tmp_path / "nope.json")

    def test_empty_holidays_default(self):
        calendar = TradingCalendar()
        assert calendar.holidays == frozenset()


class TestNextTradingDay:
    def test_after_friday_is_monday(self):
        calendar = TradingCalendar()
        assert calendar.next_trading_day("20260918") == "20260921"

    def test_skips_holiday(self):
        calendar = TradingCalendar(holidays=frozenset({"20260921"}))
        assert calendar.next_trading_day("20260918") == "20260922"

    def test_from_sunday_points_at_monday(self):
        calendar = TradingCalendar()
        assert calendar.next_trading_day("20260920") == "20260921"
