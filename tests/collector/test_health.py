"""离线契约测试：采集健康守卫（整改方案 P0-5）。

2026-09-17 的教训是：累计计数一直在涨，看起来正常，而实际数据流已经停摆数小时。
因此这里的用例直接回放当天观察到的数字。
"""

from __future__ import annotations

from bt_api_ctp.collector.health import CollectionHealthGuard, HealthThresholds


def _burst(guard: CollectionHealthGuard, *, instruments: int, ticks_each: int, now: float) -> None:
    guard.observe({f"i{index}": [None] * ticks_each for index in range(instruments)}, now=now)


class TestHealthyFeed:
    """健康行情不得误报。"""

    def test_healthy_rate_raises_no_alarm(self):
        guard = CollectionHealthGuard()
        # 实测健康分钟：上万条、上千个合约在动
        _burst(guard, instruments=1000, ticks_each=20, now=0.0)

        verdict = guard.evaluate(now=60.0, subscribed=16956, session=0)

        assert verdict.healthy
        assert verdict.ticks_in_window == 20000
        assert verdict.active_instruments == 1000
        assert verdict.silent_active == 0

    def test_quiet_instruments_that_went_quiet_long_ago_leave_the_window(self):
        """早已不再活跃的合约不应把静默占比抬高（否则尾盘必然误报）。"""
        guard = CollectionHealthGuard()
        _burst(guard, instruments=1000, ticks_each=1, now=0.0)  # 一小时前活跃过
        _burst(guard, instruments=20, ticks_each=50, now=3600.0)  # 当前只有这几个在动

        verdict = guard.evaluate(now=3660.0, subscribed=16956, session=0)

        assert verdict.tracked_instruments == 20
        assert verdict.healthy


class TestStallDetection:
    """完全停摆必须告警。"""

    def test_single_empty_interval_does_not_alarm(self):
        guard = CollectionHealthGuard()
        _burst(guard, instruments=100, ticks_each=5, now=0.0)

        verdict = guard.evaluate(now=60.0, subscribed=100, session=0)

        assert verdict.healthy

    def test_consecutive_empty_intervals_alarm(self):
        guard = CollectionHealthGuard()
        _burst(guard, instruments=100, ticks_each=5, now=0.0)
        guard.evaluate(now=60.0, subscribed=100, session=0)  # 有数

        guard.evaluate(now=120.0, subscribed=100, session=0)  # 第 1 个空档
        verdict = guard.evaluate(now=180.0, subscribed=100, session=0)  # 第 2 个空档

        assert not verdict.healthy
        assert any("stalled" in reason for reason in verdict.alarm_reasons)

    def test_outside_session_never_alarms(self):
        guard = CollectionHealthGuard()

        verdict = guard.evaluate(now=999999.0, subscribed=100, session=None)

        assert verdict.healthy
        assert verdict.ticks_in_window == 0

    def test_stall_counter_does_not_survive_a_session_break(self):
        guard = CollectionHealthGuard()
        guard.evaluate(now=0.0, subscribed=100, session=0)  # 停摆 1 次
        guard.evaluate(now=60.0, subscribed=100, session=None)  # 收盘重置

        verdict = guard.evaluate(now=120.0, subscribed=100, session=0)

        assert verdict.healthy


class TestSilentShareDetection:
    """部分退化（还零星有数，但活跃合约大面积静默）必须告警。"""

    def test_replays_the_20260917_partial_stall(self):
        """回放当天：先是 500 个合约持续出数，之后几乎全部静默。"""
        guard = CollectionHealthGuard()
        _burst(guard, instruments=500, ticks_each=5, now=0.0)
        assert guard.evaluate(now=60.0, subscribed=16956, session=0).healthy

        # 之后每分钟只有个位数合约在动
        verdict = None
        for minute in range(1, 7):
            _burst(guard, instruments=3, ticks_each=1, now=60.0 * minute)
            verdict = guard.evaluate(now=60.0 * (minute + 1), subscribed=16956, session=0)

        assert verdict is not None
        assert not verdict.healthy
        assert any("silent" in reason for reason in verdict.alarm_reasons)
        assert verdict.silent_share >= guard.thresholds.silent_share_alarm

    def test_thresholds_are_configurable(self):
        guard = CollectionHealthGuard(HealthThresholds(silent_after_sec=30.0, silent_share_alarm=0.5))
        _burst(guard, instruments=10, ticks_each=1, now=0.0)

        verdict = guard.evaluate(now=60.0, subscribed=10, session=0)

        assert not verdict.healthy
        assert verdict.max_silence_sec == 60.0


class TestSessionBoundaries:
    """时段切换必须清空记忆，否则 10:15 休息、午休、隔夜都会误报。"""

    def test_break_between_sessions_does_not_alarm(self):
        """09:00-10:15 活跃，10:15-10:30 休息，10:30 重开不得报静默。"""
        guard = CollectionHealthGuard()
        for minute in range(0, 75, 5):
            _burst(guard, instruments=1000, ticks_each=10, now=minute * 60.0)
            guard.evaluate(now=minute * 60.0, subscribed=16956, session=0)

        # 10:15 之后进入休息（session 为 None）
        guard.evaluate(now=16 * 60.0, subscribed=16956, session=None)
        # 10:30 重新开盘
        _burst(guard, instruments=1000, ticks_each=10, now=31 * 60.0)
        verdict = guard.evaluate(now=31 * 60.0, subscribed=16956, session=1)

        assert verdict.healthy, verdict.alarm_reasons
        assert verdict.tracked_instruments == 0  # 休息已清空记忆

    def test_lunch_break_is_not_an_alarm(self):
        guard = CollectionHealthGuard()
        _burst(guard, instruments=500, ticks_each=20, now=0.0)
        guard.evaluate(now=0.0, subscribed=500, session=1)

        guard.evaluate(now=5400.0, subscribed=500, session=None)  # 午休
        _burst(guard, instruments=500, ticks_each=20, now=5401.0)
        verdict = guard.evaluate(now=5401.0, subscribed=500, session=2)

        assert verdict.healthy


class TestWindowAccounting:
    def test_counters_reset_between_intervals(self):
        guard = CollectionHealthGuard()
        _burst(guard, instruments=10, ticks_each=3, now=0.0)

        first = guard.evaluate(now=60.0, subscribed=10, session=0)
        second = guard.evaluate(now=120.0, subscribed=10, session=0)

        assert first.ticks_in_window == 30
        assert second.ticks_in_window == 0
        assert second.active_instruments == 0

    def test_empty_batches_do_not_count_as_activity(self):
        guard = CollectionHealthGuard()
        guard.observe({"rb2510": []}, now=0.0)

        verdict = guard.evaluate(now=60.0, subscribed=1, session=0)

        assert verdict.ticks_in_window == 0
        assert verdict.active_instruments == 0
