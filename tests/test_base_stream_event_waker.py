from __future__ import annotations

import importlib.util
import queue
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_base_stream() -> ModuleType:
    source = Path(__file__).parents[1] / "src" / "bt_api_ctp" / "feeds" / "base_stream.py"
    spec = importlib.util.spec_from_file_location("ac50_ctp_base_stream", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _TestStream:
    def __init__(self, base: type, data_queue, **kwargs) -> None:
        class Stream(base):
            def connect(self):
                return None

            def disconnect(self):
                return None

            def subscribe_topics(self, topics):
                return None

            def _run_loop(self):
                return None

        self.stream = Stream(data_queue, **kwargs)


def _load_plugin_with_fake_streams(base_stream: ModuleType, monkeypatch):
    class FakeStream(base_stream.BaseDataStream):
        def start(self):
            self.started = True

        def connect(self):
            return None

        def disconnect(self):
            return None

        def subscribe_topics(self, topics):
            return None

        def _run_loop(self):
            return None

    package = ModuleType("bt_api_ctp")
    package.__path__ = []
    package.__version__ = "ac50-test"
    exchange_data = ModuleType("bt_api_ctp.exchange_data")
    exchange_data.CtpExchangeDataFuture = type("CtpExchangeDataFuture", (), {})
    feeds = ModuleType("bt_api_ctp.feeds")
    feeds.__path__ = []
    live_feed = ModuleType("bt_api_ctp.feeds.live_ctp_feed")
    live_feed.CtpMarketStream = FakeStream
    live_feed.CtpRequestDataFuture = type("CtpRequestDataFuture", (), {})
    live_feed.CtpTradeStream = FakeStream
    for name, module in (
        ("bt_api_ctp", package),
        ("bt_api_ctp.exchange_data", exchange_data),
        ("bt_api_ctp.feeds", feeds),
        ("bt_api_ctp.feeds.live_ctp_feed", live_feed),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    source = Path(__file__).parents[1] / "src" / "bt_api_ctp" / "plugin.py"
    spec = importlib.util.spec_from_file_location("ac50_ctp_plugin", source)
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    return plugin


def test_push_data_wakes_only_after_successful_queue_put():
    base_stream = _load_base_stream()
    operations = []

    class OrderedQueue:
        def put(self, item):
            operations.append(("put", item))

    stream = _TestStream(
        base_stream.BaseDataStream,
        OrderedQueue(),
        _event_waker=lambda: operations.append(("wake", None)),
    ).stream

    stream.push_data("tick")

    assert operations == [("put", "tick"), ("wake", None)]


def test_push_data_does_not_wake_when_queue_put_fails():
    base_stream = _load_base_stream()
    woke = []

    class FullQueue:
        def put(self, _item):
            raise queue.Full

    stream = _TestStream(
        base_stream.BaseDataStream,
        FullQueue(),
        _event_waker=lambda: woke.append(True),
    ).stream

    try:
        stream.push_data("tick")
    except queue.Full:
        pass
    else:
        raise AssertionError("queue failure must propagate")

    assert woke == []


def test_waker_exception_is_isolated_and_diagnosed_once():
    base_stream = _load_base_stream()
    target = queue.Queue()

    def broken_waker():
        raise RuntimeError("synthetic wake failure")

    stream = _TestStream(
        base_stream.BaseDataStream,
        target,
        _event_waker=broken_waker,
    ).stream

    stream.push_data("tick-1")
    stream.push_data("tick-2")

    assert target.qsize() == 2
    assert stream._event_waker_failure_count == 2
    assert stream._event_waker_failure_reported is True


def test_state_transition_wakes_after_new_state_is_visible_only_when_changed():
    base_stream = _load_base_stream()
    observations = []
    stream = _TestStream(
        base_stream.BaseDataStream,
        None,
        _event_waker=lambda: observations.append(stream.state),
    ).stream

    stream.state = base_stream.ConnectionState.ERROR
    stream.state = base_stream.ConnectionState.DISCONNECTED
    stream.state = base_stream.ConnectionState.DISCONNECTED

    assert observations == [
        base_stream.ConnectionState.ERROR,
        base_stream.ConnectionState.DISCONNECTED,
    ]


def test_production_plugin_binds_waker_to_market_and_trade_streams(monkeypatch):
    base_stream = _load_base_stream()
    plugin = _load_plugin_with_fake_streams(base_stream, monkeypatch)
    data_queue = queue.Queue()
    wakes = []
    callback = lambda: wakes.append(data_queue.qsize())
    api = SimpleNamespace(
        _notify_event_wakers=callback,
        _subscription_streams=[],
        _subscription_flags={},
        exchange_feeds={},
        log=lambda *_args: None,
    )

    plugin._ctp_future_subscribe_handler(
        data_queue,
        {"subscribe_account": True},
        [{"topic": "tick", "symbol": "IF2612"}],
        api,
    )

    assert len(api._subscription_streams) == 2
    assert all(stream.started for stream in api._subscription_streams)
    assert all(stream._event_waker is callback for stream in api._subscription_streams)
    for stream in api._subscription_streams:
        stream.push_data("tick")
    assert wakes == [1, 2]
