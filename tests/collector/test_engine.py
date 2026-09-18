"""离线契约测试：采集引擎编排（分片过滤、缓冲刷盘、退出与报告）。"""

from __future__ import annotations

import threading

import pyarrow.parquet as pq
import pytest

from bt_api_ctp.collector.engine import CollectionConfig, TickCollectionEngine
from bt_api_ctp.collector.protocols import InstrumentSpec, TickRecord
from bt_api_ctp.collector.shard import ShardConfig


def _spec(instrument_id: str, exchange_id: str, asset_type: str = "future") -> InstrumentSpec:
    return InstrumentSpec(
        instrument_id=instrument_id, exchange_id=exchange_id, asset_type=asset_type
    )


def _tick(instrument_id: str = "rb2510", exchange_id: str = "SHFE", **overrides) -> TickRecord:
    base = {
        "exchange_id": exchange_id,
        "instrument_id": instrument_id,
        "trading_day": "20260916",
        "action_day": "20260916",
        "update_time": "09:00:00",
        "update_millisec": 0,
        "local_receive_time": 1,
        "last_price": 3500.0,
    }
    base.update(overrides)
    return TickRecord(**base)



class _FakeProvider:
    def __init__(self, specs):
        self._specs = list(specs)
        self.calls = 0

    def fetch_instruments(self):
        self.calls += 1
        return list(self._specs)


class _FakeSubscriber:
    def __init__(self, *, ticks_on_connect=(), stop_event=None, stop_after_connect=False):
        self.handler = None
        self.connected = False
        self.closed = False
        self.subscribed: list[str] | None = None
        self.exchange_map: dict[str, str] | None = None
        self._ticks_on_connect = list(ticks_on_connect)
        self._stop_event = stop_event
        self._stop_after_connect = stop_after_connect

    def set_handler(self, handler):
        self.handler = handler

    def set_instrument_exchanges(self, mapping):
        self.exchange_map = dict(mapping)

    def connect(self):
        self.connected = True
        for tick in self._ticks_on_connect:
            self.handler.on_tick(tick)
        if self._stop_after_connect and self._stop_event is not None:
            self._stop_event.set()

    def subscribe(self, instruments):
        self.subscribed = list(instruments)

    def subscription_stats(self):
        return {"ok": len(self.subscribed or []), "failed": 0}

    def run(self):
        return None

    def close(self):
        self.closed = True


def _engine(tmp_path, specs, subscriber, **config_overrides):
    config = CollectionConfig(data_root=tmp_path, flush_interval_sec=0.01, **config_overrides)
    provider = _FakeProvider(specs)
    engine = TickCollectionEngine(provider=provider, subscriber=subscriber, config=config)
    return engine, provider


class TestTickCollectionEngine:
    def test_collects_and_persists_ticks(self, tmp_path):
        ticks = [_tick(update_millisec=index) for index in range(3)]
        subscriber = _FakeSubscriber(ticks_on_connect=ticks)
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        report = engine.run_once(duration_sec=0.05)

        path = tmp_path / "20260916" / "SHFE" / "rb2510.parquet"
        assert path.exists()
        assert pq.read_table(path).num_rows == 3
        assert report.trading_day == "20260916"
        assert (tmp_path / "20260916" / "report.json").exists()
        assert subscriber.closed is True

    def test_subscribes_only_sharded_instruments(self, tmp_path):
        specs = [_spec("rb2510", "SHFE"), _spec("m2701", "DCE"), _spec("sc2510", "INE")]
        subscriber = _FakeSubscriber()
        engine, _ = _engine(
            tmp_path,
            specs,
            subscriber,
            shard=ShardConfig(strategy="by_exchange", exchanges=("SHFE", "INE")),
        )

        engine.run_once(duration_sec=0.02)

        assert sorted(subscriber.subscribed) == ["rb2510", "sc2510"]

    def test_asset_type_filter_applies(self, tmp_path):
        specs = [
            _spec("rb2510", "SHFE", "future"),
            _spec("m2701-C-3000", "DCE", "option"),
        ]
        subscriber = _FakeSubscriber()
        engine, _ = _engine(tmp_path, specs, subscriber, asset_types=("option",))

        engine.run_once(duration_sec=0.02)

        assert subscriber.subscribed == ["m2701-C-3000"]

    def test_subscriber_receives_exchange_map(self, tmp_path):
        """CTP tick 不带 ExchangeID，engine 需把合约→交易所映射交给 subscriber。"""
        specs = [_spec("rb2510", "SHFE"), _spec("m2701", "DCE")]
        subscriber = _FakeSubscriber()
        engine, _ = _engine(tmp_path, specs, subscriber)

        engine.run_once(duration_sec=0.02)

        assert subscriber.exchange_map == {"rb2510": "SHFE", "m2701": "DCE"}

    def test_subscriber_without_exchange_map_support_is_tolerated(self, tmp_path):
        class _BareSubscriber:
            def __init__(self):
                self.handler = None
                self.subscribed = None
                self.connected = False

            def set_handler(self, handler):
                self.handler = handler

            def connect(self):
                self.connected = True

            def subscribe(self, instruments):
                self.subscribed = list(instruments)

            def run(self):
                return None

            def close(self):
                return None

        subscriber = _BareSubscriber()
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        engine.run_once(duration_sec=0.02)  # 不得抛出

        assert subscriber.subscribed == ["rb2510"]

    def test_empty_universe_does_not_connect(self, tmp_path):
        subscriber = _FakeSubscriber()
        engine, provider = _engine(tmp_path, [], subscriber)

        report = engine.run_once(duration_sec=0.02)

        assert provider.calls == 1
        assert subscriber.connected is False
        assert subscriber.subscribed is None
        assert report.instruments == []

    def test_stop_event_exits_and_flushes(self, tmp_path):
        stop_event = threading.Event()
        ticks = [_tick(update_millisec=1)]
        subscriber = _FakeSubscriber(
            ticks_on_connect=ticks, stop_event=stop_event, stop_after_connect=True
        )
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        report = engine.run_once(stop_event=stop_event)

        path = tmp_path / "20260916" / "SHFE" / "rb2510.parquet"
        assert path.exists()
        assert pq.read_table(path).num_rows == 1
        assert report.trading_day == "20260916"

    def test_finalize_reports_dropped_ticks(self, tmp_path):
        ticks = [_tick(update_millisec=index) for index in range(3)]
        subscriber = _FakeSubscriber(ticks_on_connect=ticks)
        engine, _ = _engine(
            tmp_path,
            [_spec("rb2510", "SHFE")],
            subscriber,
            per_instrument_cap=1,
            overflow_policy="drop",
        )

        report = engine.run_once(duration_sec=0.05)

        assert report.dropped_ticks == 2

    def test_report_contains_per_instrument_rows(self, tmp_path):
        ticks = [_tick(update_millisec=index) for index in range(4)]
        subscriber = _FakeSubscriber(ticks_on_connect=ticks)
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        report = engine.run_once(duration_sec=0.05)

        entry = {item.instrument_id: item for item in report.instruments}["rb2510"]
        assert entry.rows == 4
        assert entry.first_update == "20260916 09:00:00.000"
        assert entry.last_update == "20260916 09:00:00.003"


class TestProviderSessionRelease:
    """合约查询完成后必须释放 provider 的查询会话，避免分片进程长期占用连接。"""

    def test_provider_close_is_called_after_fetch(self, tmp_path):
        closed = []

        class _ClosableProvider:
            def fetch_instruments(self):
                return [_spec("rb2510", "SHFE")]

            def close(self):
                closed.append(True)

        subscriber = _FakeSubscriber()
        engine = TickCollectionEngine(
            provider=_ClosableProvider(),
            subscriber=subscriber,
            config=CollectionConfig(data_root=tmp_path, flush_interval_sec=0.01),
        )

        engine.run_once(duration_sec=0.02)

        assert closed == [True]

    def test_provider_without_close_is_tolerated(self, tmp_path):
        subscriber = _FakeSubscriber()
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        engine.run_once(duration_sec=0.02)  # 不得抛出

        assert subscriber.subscribed == ["rb2510"]


class TestHeartbeatLogging:
    """无人值守时必须有周期性心跳与刷盘日志，否则无从判断进程是否正常。"""

    def test_heartbeat_is_logged(self, tmp_path, caplog):
        import logging

        ticks = [_tick(update_millisec=index) for index in range(5)]
        subscriber = _FakeSubscriber(ticks_on_connect=ticks)
        engine, _ = _engine(
            tmp_path, [_spec("rb2510", "SHFE")], subscriber, heartbeat_interval_sec=0.01
        )

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.08)

        heartbeats = [r for r in caplog.records if "heartbeat" in r.getMessage()]
        assert heartbeats, "应输出心跳日志"
        assert "received=5" in heartbeats[0].getMessage()

    def test_heartbeat_can_be_disabled(self, tmp_path, caplog):
        import logging

        subscriber = _FakeSubscriber(ticks_on_connect=[_tick()])
        engine, _ = _engine(
            tmp_path, [_spec("rb2510", "SHFE")], subscriber, heartbeat_interval_sec=0.0
        )

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.05)

        assert not [r for r in caplog.records if "heartbeat" in r.getMessage()]

    def test_flush_is_logged(self, tmp_path, caplog):
        import logging

        subscriber = _FakeSubscriber(ticks_on_connect=[_tick()])
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.05)

        assert [r for r in caplog.records if "flushed" in r.getMessage()]

    def test_run_start_and_finish_are_logged(self, tmp_path, caplog):
        import logging

        subscriber = _FakeSubscriber()
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.02)

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "subscribed" in messages
        assert "finished" in messages


class TestTickMilestoneLogging:
    """每个合约累计到阈值要能报进度，否则无法确认全市场合约都在收数据。"""

    def _milestones(self, caplog):
        return [r.getMessage() for r in caplog.records if "tick milestone" in r.getMessage()]

    def test_every_mode_reports_each_multiple(self, tmp_path, caplog):
        import logging

        ticks = [_tick(update_millisec=index) for index in range(250)]
        subscriber = _FakeSubscriber(ticks_on_connect=ticks)
        engine, _ = _engine(
            tmp_path,
            [_spec("rb2510", "SHFE")],
            subscriber,
            tick_log_interval=100,
            tick_log_mode="every",
        )

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.05)

        milestones = self._milestones(caplog)
        assert len(milestones) == 2
        assert "reached 100 ticks" in milestones[0]
        assert "reached 200 ticks" in milestones[1]

    def test_below_threshold_logs_nothing(self, tmp_path, caplog):
        import logging

        subscriber = _FakeSubscriber(ticks_on_connect=[_tick()])
        engine, _ = _engine(
            tmp_path,
            [_spec("rb2510", "SHFE")],
            subscriber,
            tick_log_interval=100,
            tick_log_mode="every",
        )

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.05)

        assert self._milestones(caplog) == []

    def test_off_mode_logs_nothing(self, tmp_path, caplog):
        import logging

        ticks = [_tick(update_millisec=index) for index in range(250)]
        subscriber = _FakeSubscriber(ticks_on_connect=ticks)
        engine, _ = _engine(
            tmp_path,
            [_spec("rb2510", "SHFE")],
            subscriber,
            tick_log_interval=100,
            tick_log_mode="off",
        )

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.05)

        assert self._milestones(caplog) == []

    def test_zero_interval_disables_milestones(self, tmp_path, caplog):
        import logging

        ticks = [_tick(update_millisec=index) for index in range(250)]
        subscriber = _FakeSubscriber(ticks_on_connect=ticks)
        engine, _ = _engine(
            tmp_path,
            [_spec("rb2510", "SHFE")],
            subscriber,
            tick_log_interval=0,
            tick_log_mode="every",
        )

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.05)

        assert self._milestones(caplog) == []

    def test_milestones_are_tracked_per_instrument(self, tmp_path, caplog):
        import logging

        ticks = [_tick("rb2510", "SHFE", update_millisec=i) for i in range(120)]
        ticks += [_tick("m2701", "DCE", update_millisec=i) for i in range(30)]
        subscriber = _FakeSubscriber(ticks_on_connect=ticks)
        engine, _ = _engine(
            tmp_path,
            [_spec("rb2510", "SHFE"), _spec("m2701", "DCE")],
            subscriber,
            tick_log_interval=100,
            tick_log_mode="every",
        )

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.05)

        milestones = self._milestones(caplog)
        assert len(milestones) == 1
        assert "rb2510" in milestones[0]


class TestSubscriptionLogging:
    """订阅日志要能看出期货/期权各多少，以及柜台是否确认了订阅。"""

    def test_reports_counts_by_asset_type(self, tmp_path, caplog):
        import logging

        specs = [
            _spec("rb2510", "SHFE", "future"),
            _spec("m2701", "DCE", "future"),
            _spec("m2701-C-3000", "DCE", "option"),
        ]
        subscriber = _FakeSubscriber()
        engine, _ = _engine(tmp_path, specs, subscriber)

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.02)

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "future=2" in messages
        assert "option=1" in messages

    def test_reports_subscribe_acknowledgement(self, tmp_path, caplog):
        import logging

        specs = [_spec("rb2510", "SHFE"), _spec("m2701", "DCE")]
        subscriber = _FakeSubscriber()
        engine, _ = _engine(tmp_path, specs, subscriber)

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.02)

        acks = [r.getMessage() for r in caplog.records if "acknowledged" in r.getMessage()]
        assert acks, "订阅完成后应记录确认结果"
        assert "requested=2" in acks[0]
        assert "acked=2" in acks[0]
        assert "failed=0" in acks[0]
        assert "timed_out=0" in acks[0]

    def test_warns_when_acknowledgement_is_incomplete(self, tmp_path, caplog):
        """acked != requested 时不得视为就绪（整改方案 P1-4）。"""
        import logging

        class _FailedAckSubscriber(_FakeSubscriber):
            def subscription_stats(self):
                return {"ok": 0, "failed": 2, "last_error_id": 42}

        subscriber = _FailedAckSubscriber()
        engine, _ = _engine(
            tmp_path, [_spec("rb2510", "SHFE"), _spec("m2701", "DCE")], subscriber
        )

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.02)

        messages = [r.getMessage() for r in caplog.records]
        acks = [message for message in messages if "acknowledged" in message]
        assert "requested=2" in acks[0]
        assert "acked=0" in acks[0]
        assert "failed=2" in acks[0]
        assert "timed_out=0" in acks[0]
        warnings = [message for message in messages if "not ready" in message]
        assert warnings, "未就绪必须给出告警"
        assert "42" in warnings[0]

    def test_counts_unacknowledged_instruments_as_timed_out(self, tmp_path):
        subscriber = _FakeSubscriber()
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        ack = engine._await_subscription_ack(5, timeout_sec=0.0)

        assert ack["requested"] == 5
        assert ack["acked"] == 0
        assert ack["failed"] == 0
        assert ack["timed_out"] == 5

    def test_extra_acknowledgements_do_not_raise_a_readiness_warning(self, tmp_path, caplog):
        """acked > requested 说明有重复提交，不是"未就绪"，不得误报。"""
        import logging

        class _OverAckSubscriber(_FakeSubscriber):
            def subscription_stats(self):
                return {"ok": 5, "failed": 0, "last_error_id": None}

        subscriber = _OverAckSubscriber()
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.02)

        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "requested=1" in messages
        assert "acked=5" in messages
        assert "not ready" not in messages

    def test_failed_subscriptions_are_never_reported_as_ready(self, tmp_path, caplog):
        """柜台报错的合约即使 ACK 计数被重复提交补平，也必须告警。"""
        import logging

        class _FailedButAckedSubscriber(_FakeSubscriber):
            def subscription_stats(self):
                return {"ok": 1, "failed": 1, "last_error_id": 42}

            def failed_instruments(self):
                return {"nope": 42}

        subscriber = _FailedButAckedSubscriber()
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.02)

        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "not ready" in messages
        assert "failed=1" in messages

    def test_subscriber_without_stats_still_logs_the_request(self, tmp_path, caplog):
        import logging

        class _NoStatsSubscriber(_FakeSubscriber):
            subscription_stats = None  # 宿主不提供统计接口

        subscriber = _NoStatsSubscriber()
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.02)  # 不得因此崩溃

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "future=1" in messages
        assert "acknowledged" not in messages

    def test_waits_for_late_acknowledgements(self, tmp_path, caplog, monkeypatch):
        import logging

        from bt_api_ctp.collector import engine as engine_module

        class _SlowAckSubscriber(_FakeSubscriber):
            def __init__(self):
                super().__init__()
                self.polls = 0

            def subscription_stats(self):
                self.polls += 1
                # 先报 0，第二次才给出真实结果，模拟响应陆续到达
                if self.polls < 2:
                    return {"ok": 0, "failed": 0}
                return {"ok": len(self.subscribed or []), "failed": 0}

        monkeypatch.setattr(engine_module.time, "sleep", lambda *_: None)
        subscriber = _SlowAckSubscriber()
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.02)

        acks = [r.getMessage() for r in caplog.records if "acknowledged" in r.getMessage()]
        assert acks
        assert "acked=1" in acks[0]


class TestSessionDiagnosticsReporting:
    """断线时间窗与回调异常必须进入完整性报告（整改方案 P1-2）。"""

    def test_report_includes_disconnect_windows_and_callback_errors(self, tmp_path):
        import json

        class _DiagnosticSubscriber(_FakeSubscriber):
            def disconnect_windows(self):
                return [{"start": "t0", "reason": 8193, "end": "t1"}]

            def error_count(self):
                return 4

            def generation_changes(self):
                return [{"at": "t1", "generation": 2}]

        subscriber = _DiagnosticSubscriber(ticks_on_connect=[_tick()])
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        report = engine.run_once(duration_sec=0.02)

        payload = json.loads(
            (tmp_path / report.trading_day / "report.json").read_text(encoding="utf-8")
        )
        assert payload["disconnects"] == [{"start": "t0", "reason": 8193, "end": "t1"}]
        assert payload["callback_errors"] == 4
        assert payload["connection_generations"] == [{"at": "t1", "generation": 2}]

    def test_subscriber_without_diagnostics_is_tolerated(self, tmp_path):
        import json

        subscriber = _FakeSubscriber(ticks_on_connect=[_tick()])
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        report = engine.run_once(duration_sec=0.02)

        payload = json.loads(
            (tmp_path / report.trading_day / "report.json").read_text(encoding="utf-8")
        )
        assert payload["disconnects"] == []
        assert payload["callback_errors"] == 0
        assert payload["connection_generations"] == []

    def test_broken_diagnostics_hook_does_not_lose_the_report(self, tmp_path):
        """可选诊断钩子坏了不能连累整份报告（整改方案 P1-2）。"""
        import json

        class _BrokenDiagnosticsSubscriber(_FakeSubscriber):
            def disconnect_windows(self):
                raise RuntimeError("diagnostics boom")

        subscriber = _BrokenDiagnosticsSubscriber(ticks_on_connect=[_tick()])
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        report = engine.run_once(duration_sec=0.02)

        payload = json.loads(
            (tmp_path / report.trading_day / "report.json").read_text(encoding="utf-8")
        )
        assert payload["disconnects"] == []
        assert payload["instruments"]

    def test_report_includes_subscription_diagnostics(self, tmp_path):
        import json

        class _SubscriptionDiagnosticSubscriber(_FakeSubscriber):
            def failed_instruments(self):
                return {"nope": 42}

            def resubscribe_events(self):
                return [{"at": "t", "generation": 4, "requested": 250, "batches": 3}]

        subscriber = _SubscriptionDiagnosticSubscriber(ticks_on_connect=[_tick()])
        engine, _ = _engine(tmp_path, [_spec("rb2510", "SHFE")], subscriber)

        report = engine.run_once(duration_sec=0.02)

        payload = json.loads(
            (tmp_path / report.trading_day / "report.json").read_text(encoding="utf-8")
        )
        assert payload["failed_instruments"] == {"nope": 42}
        assert payload["resubscribes"] == [
            {"at": "t", "generation": 4, "requested": 250, "batches": 3}
        ]


class TestHealthGuardWiring:
    """数据健康守卫必须在停摆时输出 ERROR（整改方案 P0-5）。"""

    def test_stalled_feed_is_reported_as_an_error(self, tmp_path, caplog, monkeypatch):
        import logging

        from bt_api_ctp.collector.health import HealthThresholds

        subscriber = _FakeSubscriber()
        engine, _ = _engine(
            tmp_path,
            [_spec("rb2510", "SHFE")],
            subscriber,
            heartbeat_interval_sec=0.01,
            health=HealthThresholds(stall_intervals=1, min_ticks_per_interval=1),
        )
        monkeypatch.setattr(engine, "_current_session", lambda: 0)

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.05)

        messages = [record.getMessage() for record in caplog.records]
        health = [message for message in messages if message.startswith("health:")]
        alarms = [message for message in messages if "collection health alarm" in message]
        assert health, "每个心跳周期都应输出一行健康采样"
        assert alarms, "交易时段内没有任何数据必须告警"
        assert any("stalled" in message for message in alarms)

    def test_outside_session_is_never_an_alarm(self, tmp_path, caplog, monkeypatch):
        import logging

        from bt_api_ctp.collector.health import HealthThresholds

        engine, _ = _engine(
            tmp_path,
            [_spec("rb2510", "SHFE")],
            _FakeSubscriber(),
            heartbeat_interval_sec=0.01,
            health=HealthThresholds(stall_intervals=1),
        )
        monkeypatch.setattr(engine, "_current_session", lambda: None)

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.05)

        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "health:" in messages
        assert "alarm" not in messages

    def test_health_guard_can_be_disabled(self, tmp_path, caplog):
        import logging

        engine, _ = _engine(
            tmp_path,
            [_spec("rb2510", "SHFE")],
            _FakeSubscriber(),
            heartbeat_interval_sec=0.01,
            health_check_enabled=False,
        )

        with caplog.at_level(logging.INFO):
            engine.run_once(duration_sec=0.05)

        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "health:" not in messages


class TestFlushFailureSafety:
    """落盘失败（磁盘满/IO 错误）不得静默丢掉整批 tick。"""

    def test_failed_flush_returns_ticks_to_buffer(self, tmp_path, caplog, monkeypatch):
        import logging

        from bt_api_ctp.collector import engine as engine_module

        subscriber = _FakeSubscriber(ticks_on_connect=[_tick()])
        config = CollectionConfig(data_root=tmp_path, flush_interval_sec=0.01)
        engine = TickCollectionEngine(
            provider=_FakeProvider([_spec("rb2510", "SHFE")]),
            subscriber=subscriber,
            config=config,
        )

        real_write = engine_module.ParquetSink.write
        calls = {"n": 0}

        def flaky_write(self, drained, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_write(self, drained, **kwargs)

        monkeypatch.setattr(engine_module.ParquetSink, "write", flaky_write)
        with caplog.at_level(logging.WARNING):
            engine.run_once(duration_sec=0.08)

        # 第一次失败记 WARNING，数据留在缓冲；后续重试成功落盘
        assert any("flush failed" in r.getMessage() for r in caplog.records)
        assert (tmp_path / "20260916" / "SHFE" / "rb2510.parquet").exists()

    def test_provider_close_runs_even_when_fetch_fails(self, tmp_path):
        closed = []

        class _BrokenProvider:
            def fetch_instruments(self):
                raise RuntimeError("query failed")

            def close(self):
                closed.append(True)

        engine = TickCollectionEngine(
            provider=_BrokenProvider(),
            subscriber=_FakeSubscriber(),
            config=CollectionConfig(data_root=tmp_path),
        )

        with pytest.raises(RuntimeError, match="query failed"):
            engine.run_once(duration_sec=0.01)

        assert closed == [True]
