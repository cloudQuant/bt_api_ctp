"""离线契约测试：CTP 订阅器（回调隔离、分批转发、重连代次观察）。"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

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

    def subscribe_batched(self, instruments, *, batch_size, interval_sec, should_stop=None) -> int:
        self.subscribed = list(instruments)
        self.batch_kwargs = (batch_size, interval_sec)
        return 0 if not instruments else -(-len(instruments) // batch_size)


class _MdClientWithoutWaitReady:
    """兼容不支持 wait_ready 的宿主（connect 不得因此崩溃）。"""

    def __init__(self) -> None:
        self.on_tick = None
        self.start_calls: list[bool] = []

    def start(self, block: bool = True) -> None:
        self.start_calls.append(block)

    def stop(self) -> None:
        return None


class _DeferrableFakeMdClient(_FakeMdClient):
    """模拟真实 MdClient：支持把重订阅交给调用方异步完成，并真实切批。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_login = None
        self.on_disconnect = None
        self.auto_resubscribe_on_login = True
        self.batch_calls: list[tuple[list[str], str]] = []
        self.batches: list[list[str]] = []
        self.should_stop_provided: list[bool] = []

    def subscribe_batched(
        self, instruments, *, batch_size, interval_sec, should_stop=None
    ) -> int:
        self.subscribed = list(instruments)
        self.batch_kwargs = (batch_size, interval_sec)
        self.batch_calls.append((list(instruments), threading.current_thread().name))
        self.should_stop_provided.append(should_stop is not None)
        submitted = 0
        for start in range(0, len(instruments), batch_size):
            if should_stop is not None and should_stop():
                return submitted
            self.batches.append(list(instruments[start : start + batch_size]))
            submitted += 1
            if interval_sec > 0 and start + batch_size < len(instruments):
                time.sleep(interval_sec)
        return submitted


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """有界等待，避免用固定 sleep 制造不稳定测试。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


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


class TestSessionLogging:
    """登录/登出必须留痕，便于判断行情会话是否真的建立。"""

    def test_connect_logs_market_data_login(self, caplog):
        import logging

        subscriber = CtpMdSubscriber(_FakeMdClient())

        with caplog.at_level(logging.INFO):
            subscriber.connect()

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "market-data" in messages
        assert "login" in messages

    def test_close_logs_market_data_logout(self, caplog):
        import logging

        subscriber = CtpMdSubscriber(_FakeMdClient())
        subscriber.connect()

        with caplog.at_level(logging.INFO):
            subscriber.close()

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "market-data" in messages
        assert "closed" in messages


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


class TestDeferredResubscribeAfterReconnect:
    """重连后的重订阅必须在独立线程分批进行（整改方案 P1-3）。

    在 CTP 原生回调线程里提交全量（或分批 + 等待）会阻塞心跳与行情回调，
    本身就是 ``OnFrontDisconnected(0x2001)`` 的成因。
    """

    def _connected(self, **kwargs):
        md = _DeferrableFakeMdClient()
        subscriber = CtpMdSubscriber(md, batch_interval_sec=0.0, **kwargs)
        subscriber.set_handler(_Handler())
        subscriber.connect()
        return subscriber, md

    def test_takes_over_resubscribe_from_the_host(self):
        _, md = self._connected()

        assert md.auto_resubscribe_on_login is False
        assert callable(md.on_login)

    def test_login_callback_returns_immediately(self):
        subscriber, md = self._connected()
        subscriber.subscribe([f"i{index}" for index in range(250)])

        started = time.monotonic()
        md.on_login(SimpleNamespace())
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, "登录回调不得在原生回调线程內做重订阅"

    def test_reconnect_resubscribes_the_full_set_off_the_caller_thread(self):
        subscriber, md = self._connected(batch_size=100)
        instruments = [f"i{index}" for index in range(250)]
        subscriber.subscribe(instruments)
        assert len(md.batch_calls) == 1  # 初次订阅

        md.on_disconnect(8193)
        md.on_login(SimpleNamespace())

        assert _wait_until(lambda: len(md.batch_calls) == 2)
        resent, thread_name = md.batch_calls[-1]
        assert resent == instruments
        assert md.batch_kwargs == (100, 0.0)
        assert thread_name != threading.current_thread().name

    def test_resubscribe_is_split_into_configured_batches(self):
        """契约 C-1：重连重订阅必须按 batch_size 真实切批。"""
        subscriber, md = self._connected(batch_size=100)
        subscriber.subscribe([f"i{index}" for index in range(250)])
        md.batches.clear()

        md.on_disconnect(8193)
        md.on_login(SimpleNamespace())

        assert _wait_until(lambda: len(md.batches) == 3)
        assert [len(batch) for batch in md.batches] == [100, 100, 50]

    def test_both_subscribe_paths_are_cancellable(self):
        """初次订阅与重连重订阅都必须可被关闭中断。"""
        subscriber, md = self._connected(batch_size=100)
        subscriber.subscribe([f"i{index}" for index in range(250)])

        md.on_disconnect(8193)
        md.on_login(SimpleNamespace())
        assert _wait_until(lambda: len(md.batch_calls) == 2)

        assert md.should_stop_provided == [True, True]

    def test_first_login_after_subscribe_does_not_resubscribe(self):
        """契约 C-2：首次登录不是重连，不得重复提交全量。"""
        subscriber, md = self._connected(batch_size=100)
        subscriber.subscribe([f"i{index}" for index in range(250)])

        md.on_login(SimpleNamespace())  # 首次登录：没有断线窗口

        assert not _wait_until(lambda: len(md.batch_calls) > 1, timeout=0.3)
        assert len(md.batch_calls) == 1

    def test_close_stops_submitting_at_a_batch_boundary(self):
        """契约 C-3：关闭时必须能在批边界停下，不能一边 close 一边还在发原生调用。

        分批总量刻意超过 close 的 join 上限：若不支持取消，close 会在工作线程
        仍在调用原生接口时就返回并去 stop 客户端。
        """
        md = _DeferrableFakeMdClient()
        subscriber = CtpMdSubscriber(md, batch_size=100, batch_interval_sec=0.05)
        subscriber.set_handler(_Handler())
        subscriber.connect()
        subscriber._subscribed = [f"i{index}" for index in range(5000)]  # 跳过初次订阅耗时

        md.on_disconnect(8193)
        md.on_login(SimpleNamespace())
        assert _wait_until(lambda: len(md.batches) >= 2), "重订阅分批进行中"

        subscriber.close()
        submitted = len(md.batches)
        time.sleep(0.2)

        assert not subscriber._resubscribe_thread.is_alive(), "关闭后工作线程必须已退出"
        assert len(md.batches) == submitted, "关闭后不得再提交新的批次"

    def test_login_before_any_subscription_is_a_noop(self):
        _, md = self._connected()

        md.on_login(SimpleNamespace())

        assert not _wait_until(lambda: bool(md.batch_calls), timeout=0.3)

    def test_close_stops_the_resubscribe_thread(self):
        subscriber, md = self._connected()
        subscriber.subscribe(["rb2510"])
        worker = subscriber._resubscribe_thread
        assert worker is not None and worker.is_alive()

        subscriber.close()

        assert not worker.is_alive()
        assert md.stop_calls == 1

    def test_host_without_deferral_support_is_left_untouched(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)

        subscriber.connect()

        assert not hasattr(md, "auto_resubscribe_on_login")
        assert not hasattr(md, "on_login")
        assert subscriber._resubscribe_thread is None


class TestDisconnectWindows:
    """断线时间窗必须可上报，否则缺口无法解释（整改方案 P1-2）。"""

    def test_pairs_disconnect_with_the_following_login(self):
        md = _DeferrableFakeMdClient(generation=3)
        subscriber = CtpMdSubscriber(md)
        subscriber.connect()

        md.on_disconnect(8193)
        md.set_generation(4)
        md.on_login(SimpleNamespace())

        windows = subscriber.disconnect_windows()
        assert len(windows) == 1
        assert windows[0]["reason"] == 8193
        assert windows[0]["generation_before"] == 3
        assert windows[0]["generation_after"] == 4
        assert windows[0]["start"] and windows[0]["end"]

    def test_unrecovered_disconnect_keeps_an_open_window(self):
        md = _DeferrableFakeMdClient()
        subscriber = CtpMdSubscriber(md)
        subscriber.connect()

        md.on_disconnect(4097)

        windows = subscriber.disconnect_windows()
        assert len(windows) == 1
        assert windows[0]["end"] is None
        assert windows[0]["generation_after"] is None

    def test_second_disconnect_opens_a_new_window(self):
        md = _DeferrableFakeMdClient()
        subscriber = CtpMdSubscriber(md)
        subscriber.connect()

        md.on_disconnect(8193)
        md.on_login(SimpleNamespace())
        md.on_disconnect(4097)

        windows = subscriber.disconnect_windows()
        assert [window["reason"] for window in windows] == [8193, 4097]
        assert windows[1]["end"] is None

    def test_host_without_disconnect_support_is_tolerated(self):
        md = _FakeMdClient()
        subscriber = CtpMdSubscriber(md)

        subscriber.connect()

        assert subscriber.disconnect_windows() == []

    def test_second_disconnect_without_login_archives_the_first_window(self):
        """两次断线之间没有登录时，前一窗口不得被覆盖丢失。"""
        md = _DeferrableFakeMdClient()
        subscriber = CtpMdSubscriber(md)
        subscriber.connect()

        md.on_disconnect(8193)
        md.on_disconnect(4097)

        windows = subscriber.disconnect_windows()
        assert [window["reason"] for window in windows] == [8193, 4097]
        assert windows[0]["end"] is None
        assert windows[1]["end"] is None


class TestSubscriptionDiagnostics:
    """订阅失败与重订阅周期必须可上报（整改方案 P1-3 契约第 4、5 条）。"""

    def test_records_failed_instruments_with_error_codes(self):
        md = _DeferrableFakeMdClient()
        subscriber = CtpMdSubscriber(md)
        subscriber.connect()

        md.on_subscribe(SimpleNamespace(InstrumentID="rb2510"), SimpleNamespace(ErrorID=0))
        md.on_subscribe(
            SimpleNamespace(InstrumentID="nope"), SimpleNamespace(ErrorID=42, ErrorMsg="unknown")
        )

        assert subscriber.failed_instruments() == {"nope": 42}

    def test_records_resubscribe_cycles(self):
        md = _DeferrableFakeMdClient()
        subscriber = CtpMdSubscriber(md, batch_size=100, batch_interval_sec=0.0)
        subscriber.set_handler(_Handler())
        subscriber.connect()
        subscriber.subscribe([f"i{index}" for index in range(250)])

        md.on_disconnect(8193)
        md.set_generation(4)
        md.on_login(SimpleNamespace())

        assert _wait_until(lambda: bool(subscriber.resubscribe_events()))
        event = subscriber.resubscribe_events()[0]
        assert event["requested"] == 250
        assert event["batches"] == 3
        assert event["generation"] == 4
        assert event["at"]

    def test_zero_batch_submission_is_not_recorded_as_a_resubscribe(self):
        """一个批次都没提交时不得记成一次重订阅，否则报告会虚报重订阅轮次。"""

        class _RefusingMdClient(_DeferrableFakeMdClient):
            def subscribe_batched(
                self, instruments, *, batch_size, interval_sec, should_stop=None
            ) -> int:
                return 0  # 提交前会话又断了

        md = _RefusingMdClient()
        subscriber = CtpMdSubscriber(md, batch_interval_sec=0.0)
        subscriber.set_handler(_Handler())
        subscriber.connect()
        subscriber.subscribe(["rb2510"])

        md.on_disconnect(8193)
        md.on_login(SimpleNamespace())

        assert not _wait_until(lambda: bool(subscriber.resubscribe_events()), timeout=0.3)

    def test_later_success_clears_an_earlier_failure(self):
        """重订阅成功后，失败清单必须同步清理，否则报告永远报假缺口。"""
        md = _DeferrableFakeMdClient()
        subscriber = CtpMdSubscriber(md)
        subscriber.connect()

        md.on_subscribe(SimpleNamespace(InstrumentID="rb2510"), SimpleNamespace(ErrorID=42))
        md.on_subscribe(SimpleNamespace(InstrumentID="rb2510"), SimpleNamespace(ErrorID=0))

        assert subscriber.failed_instruments() == {}

    def test_worker_survives_a_failing_resubscribe(self):
        """重订阅出错不能让工作线程静默退出，否则后续重连永久失去重订阅能力。"""

        def _boom():
            raise RuntimeError("stats boom")

        md = _DeferrableFakeMdClient()
        subscriber = CtpMdSubscriber(md, batch_interval_sec=0.0)
        subscriber.set_handler(_Handler())
        subscriber.connect()
        subscriber.subscribe(["rb2510"])
        subscriber.subscription_stats = _boom  # 提交后的统计调用抛错

        md.on_disconnect(8193)
        md.on_login(SimpleNamespace())
        assert _wait_until(lambda: len(md.batch_calls) == 2), "第一次重订阅已提交（随后统计抛错）"

        md.on_disconnect(4097)
        md.on_login(SimpleNamespace())

        assert _wait_until(lambda: len(md.batch_calls) == 3), "工作线程必须存活并继续重订阅"
