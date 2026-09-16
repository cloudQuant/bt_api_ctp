"""离线契约测试：交易日历与夜盘判断。

参照日期：2026-09-16 为周三。
"""

from __future__ import annotations

import json

import pytest

from bt_api_ctp.collector.schedule import NIGHT_CLOSE_TIERS, TradingCalendar


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
