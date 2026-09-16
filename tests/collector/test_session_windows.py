"""离线契约测试：交易时段表（gap 检测与「采集到收盘」的共同基础）。

时段口径（宽区间，覆盖商品与中金所）：
    09:00-10:15  商品上午第一节
    10:30-11:30  商品上午第二节（中金所上午连续至 11:30）
    13:00-15:15  下午（覆盖中金所 15:15 收盘）
    21:00-02:30  夜盘（跨零点，覆盖 23:00/01:00/02:30 三档收盘）
"""

from __future__ import annotations

from datetime import datetime

import pytest

from bt_api_ctp.collector.schedule import (
    TRADING_SESSIONS,
    seconds_until_close,
    seconds_until_next_open,
    session_index,
)


def _at(hhmmss: str) -> datetime:
    hour, minute, second = (int(part) for part in hhmmss.split(":"))
    return datetime(2026, 9, 16, hour, minute, second)


class TestTradingSessionsDefinition:
    def test_four_sessions_with_night_crossing_midnight(self):
        assert len(TRADING_SESSIONS) == 4
        assert TRADING_SESSIONS[0].start_hhmm == "09:00"
        assert TRADING_SESSIONS[0].end_hhmm == "10:15"
        assert TRADING_SESSIONS[3].start_hhmm == "21:00"
        assert TRADING_SESSIONS[3].end_hhmm == "02:30"
        assert TRADING_SESSIONS[3].crosses_midnight is True


class TestSessionIndex:
    def test_morning_first_session(self):
        assert session_index(_at("09:00:00")) == 0
        assert session_index(_at("10:14:59")) == 0

    def test_morning_break_is_outside_sessions(self):
        assert session_index(_at("10:20:00")) is None

    def test_morning_second_session(self):
        assert session_index(_at("10:30:00")) == 1
        assert session_index(_at("11:30:00")) == 1

    def test_lunch_break_is_outside_sessions(self):
        assert session_index(_at("12:00:00")) is None

    def test_afternoon_session_includes_cffex_close(self):
        assert session_index(_at("13:00:00")) == 2
        assert session_index(_at("15:15:00")) == 2

    def test_after_close_is_outside_sessions(self):
        assert session_index(_at("16:00:00")) is None

    def test_night_session_including_after_midnight(self):
        assert session_index(_at("21:00:00")) == 3
        assert session_index(_at("23:59:59")) == 3
        assert session_index(_at("00:30:00")) == 3
        assert session_index(_at("02:30:00")) == 3

    def test_early_morning_and_pre_open_are_outside_sessions(self):
        assert session_index(_at("03:00:00")) is None
        assert session_index(_at("08:00:00")) is None


class TestSecondsUntilClose:
    """「跑到收盘」= 跑到本交易组最后一个小节的结束，而非当前小节结束。"""

    def test_morning_first_session_runs_to_day_close(self):
        # 10:00 属白盘组，应跑到 15:15，而不是 10:15
        assert seconds_until_close(_at("10:00:00")) == pytest.approx(18900.0)

    def test_afternoon_session_runs_to_day_close(self):
        assert seconds_until_close(_at("14:00:00")) == pytest.approx(4500.0)

    def test_night_session_before_midnight(self):
        assert seconds_until_close(_at("23:00:00")) == pytest.approx(12600.0)

    def test_night_session_after_midnight(self):
        assert seconds_until_close(_at("01:00:00")) == pytest.approx(5400.0)

    def test_outside_sessions_returns_none(self):
        assert seconds_until_close(_at("12:00:00")) is None
        assert seconds_until_close(_at("16:00:00")) is None


class TestSecondsUntilNextOpen:
    """开盘前调度（08:45/20:45）需要知道还要等多久开盘。"""

    def test_before_day_session(self):
        assert seconds_until_next_open(_at("08:45:00")) == pytest.approx(900.0)

    def test_during_lunch_break(self):
        assert seconds_until_next_open(_at("12:00:00")) == pytest.approx(3600.0)

    def test_after_day_close_waits_for_night(self):
        assert seconds_until_next_open(_at("16:00:00")) == pytest.approx(5 * 3600.0)

    def test_during_session_is_none(self):
        assert seconds_until_next_open(_at("10:00:00")) is None
