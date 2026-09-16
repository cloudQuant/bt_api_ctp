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
