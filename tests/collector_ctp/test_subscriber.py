"""离线契约测试：CTP 订阅器（回调隔离、分批转发、重连代次观察）。"""

from __future__ import annotations

import threading
import time

import pytest

from bt_api_ctp.collector_ctp.normalizer import CtpTickNormalizer
from bt_api_ctp.collector_ctp.subscriber import CtpMdSubscriber

RAW_TICK = {
    "TradingDay": "20260916",
    "ActionDay": "20260916",
    "InstrumentID": "rb2510",
    "ExchangeID": "SHFE",
    "LastPrice": 3500.0,
    "UpdateTime": "09:00:00",
    "UpdateMillisec": 500,
}


class _FakeMdClient:
    def __init__(self, generation: int = 1, ready: bool = True):
        self.on_tick = None
        self.start_calls: list[bool] = []
        self.stop_calls = 0
        self.subscribed: list[str] | None = None
        self.batch_kwargs: tuple[int, float] | None = None
        self.wait_ready_calls: list[float] = []
        self._generation = generation
        self._ready = ready

    @property
    def connection_generation(self) -> int:
        return self._generation

    def set_generation(self, generation: int) -> None:
        self._generation = generation

    def start(self, block: bool = True) -> None:
        self.start_calls.append(block)

    def stop(self) -> None:
        self.stop_calls += 1

    def wait_ready(self, timeout: float = 15) -> bool:
        self.wait_ready_calls.append(timeout)
        return self._ready

    def subscribe_batched(self, instruments, *, batch_size, interval_sec) -> None:
        self.subscribed = list(instruments)
        self.batch_kwargs = (batch_size, interval_sec)


class _MdClientWithoutWaitReady:
    """兼容不支持 wait_ready 的宿主（connect 不得因此崩溃）。"""

    def __init__(self) -> None:
        self.on_tick = None
        self.start_calls: list[bool] = []

    def start(self, block: bool = True) -> None:
        self.start_calls.append(block)

    def stop(self) -> None:
        return None


class _Handler:
    def __init__(self, *, raises: bool = False):
        self.ticks = []
        self.raises = raises

    def on_tick(self, tick) -> None:
        if self.raises:
            raise RuntimeError("handler boom")
        self.ticks.append(tick)


class _BrokenNormalizer:
    def normalize(self, raw, **_kwargs):
        raise ValueError("normalizer boom")


class TestCtpMdSubscriber:
    def test_connect_binds_callback_and_starts_in_background(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)

        subscriber.connect()

        assert md.on_tick is not None
        assert md.start_calls == [False]

    def test_connect_waits_for_login_so_batching_actually_happens(self):
        """订阅前必须已登录，否则 SubscribeMarketData 只会记录 pending 而不分批提交。"""
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md, login_timeout_sec=7.5)

        subscriber.connect()

        assert md.wait_ready_calls == [7.5]

    def test_connect_tolerates_host_without_wait_ready(self):
        md = _MdClientWithoutWaitReady()
        subscriber = CtpMdSubscriber(md)

        subscriber.connect()

        assert md.start_calls == [False]

    def test_subscribe_forwards_batch_configuration(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md, batch_size=50, batch_interval_sec=0.25)

        subscriber.subscribe(["rb2510", "cu2510"])

        assert md.subscribed == ["rb2510", "cu2510"]
        assert md.batch_kwargs == (50, 0.25)

    def test_callback_delivers_normalized_ticks(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)
        handler = _Handler()
        subscriber.set_handler(handler)
        subscriber.connect()

        md.on_tick(RAW_TICK)

        assert len(handler.ticks) == 1
        tick = handler.ticks[0]
        assert tick.instrument_id == "rb2510"
        assert tick.update_millisec == 500

    def test_default_normalizer_is_ctp(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)
        assert isinstance(subscriber.normalizer, CtpTickNormalizer)

    def test_unusable_payload_is_skipped(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)
        handler = _Handler()
        subscriber.set_handler(handler)
        subscriber.connect()

        md.on_tick({"ExchangeID": "SHFE"})  # 缺 InstrumentID

        assert handler.ticks == []

    def test_handler_exception_never_escapes_callback(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)
        subscriber.set_handler(_Handler(raises=True))
        subscriber.connect()

        md.on_tick(RAW_TICK)  # 不得抛出

        assert subscriber.error_count() == 1

    def test_normalizer_exception_never_escapes_callback(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md, normalizer=_BrokenNormalizer())
        subscriber.set_handler(_Handler())
        subscriber.connect()

        md.on_tick(RAW_TICK)  # 不得抛出

        assert subscriber.error_count() == 1

    def test_callback_without_handler_is_safe(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)
        subscriber.connect()

        md.on_tick(RAW_TICK)  # 不得抛出

        assert subscriber.error_count() == 0

    def test_generation_changes_are_recorded(self):
        md = _FakeMdClient(generation=1)
        subscriber = CtpMdSubscriber(md)
        subscriber.set_handler(_Handler())
        subscriber.connect()

        md.on_tick(RAW_TICK)
        assert subscriber.generation_changes() == []  # 初次连接不算变化

        md.set_generation(2)  # 模拟重连
        md.on_tick(RAW_TICK)

        changes = subscriber.generation_changes()
        assert len(changes) == 1
        assert changes[0]["generation"] == 2

    def test_run_blocks_until_close(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)

        thread = threading.Thread(target=subscriber.run, daemon=True)
        thread.start()
        time.sleep(0.05)
        assert thread.is_alive()

        subscriber.close()
        thread.join(timeout=1.0)

        assert not thread.is_alive()
        assert md.stop_calls == 1


class TestExchangeBackfill:
    """CTP 深度行情不填充 ExchangeID，需用订阅时的合约→交易所映射补全。"""

    def test_missing_exchange_is_filled_from_map(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)
        subscriber.set_instrument_exchanges({"rb2701": "SHFE"})
        handler = _Handler()
        subscriber.set_handler(handler)
        subscriber.connect()

        md.on_tick({**RAW_TICK, "InstrumentID": "rb2701", "ExchangeID": ""})

        assert handler.ticks[0].exchange_id == "SHFE"

    def test_native_exchange_wins_when_present(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)
        subscriber.set_instrument_exchanges({"rb2701": "SHFE"})
        handler = _Handler()
        subscriber.set_handler(handler)
        subscriber.connect()

        md.on_tick({**RAW_TICK, "InstrumentID": "rb2701", "ExchangeID": "DCE"})

        assert handler.ticks[0].exchange_id == "DCE"

    def test_unmapped_instrument_keeps_empty_exchange(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)
        subscriber.set_instrument_exchanges({"rb2701": "SHFE"})
        handler = _Handler()
        subscriber.set_handler(handler)
        subscriber.connect()

        md.on_tick({**RAW_TICK, "InstrumentID": "xx9999", "ExchangeID": ""})

        assert handler.ticks[0].exchange_id == ""


class TestSubscriptionStats:
    """订阅成功/失败统计，供心跳日志与无人值守告警使用。"""

    def test_counts_successes_and_failures(self):
        from types import SimpleNamespace

        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)
        subscriber.connect()
        assert callable(md.on_subscribe)

        md.on_subscribe(SimpleNamespace(InstrumentID="rb2701"), SimpleNamespace(ErrorID=0))
        md.on_subscribe(
            SimpleNamespace(InstrumentID="nope"), SimpleNamespace(ErrorID=42, ErrorMsg="unknown")
        )

        stats = subscriber.subscription_stats()
        assert stats["ok"] == 1
        assert stats["failed"] == 1
        assert stats["last_error_id"] == 42

    def test_tolerates_empty_response(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)
        subscriber.connect()

        md.on_subscribe(None, None)  # 不得抛出

        assert subscriber.subscription_stats()["failed"] == 0

    def test_stats_start_at_zero(self):
        subscriber = CtpMdSubscriber(_FakeMdClient())
        assert subscriber.subscription_stats() == {"ok": 0, "failed": 0, "last_error_id": None}


class TestConnectFailFast:
    """行情登录失败必须立即失败，而不是静默空采到收盘。"""

    def test_connect_raises_when_login_times_out(self):
        md = _FakeMdClient(ready=False)
        subscriber = CtpMdSubscriber(md, login_timeout_sec=0.01)

        with pytest.raises(RuntimeError, match="ctp_md_login_timeout"):
            subscriber.connect()

    def test_host_without_wait_ready_stays_silent(self):
        # 无法探测就绪的宿主保持原行为（不抛出）
        md = _MdClientWithoutWaitReady()
        subscriber = CtpMdSubscriber(md)

        subscriber.connect()
        assert md.start_calls == [False]
