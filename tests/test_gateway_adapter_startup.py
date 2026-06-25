from __future__ import annotations

from bt_api_ctp.gateway import adapter as adapter_module


class _FakeFeed:
    def __init__(self, *_args, **_kwargs) -> None:
        self._trader = None
        self._connected = False


def _stream_class(outcomes: list[bool]):
    class _FakeStream:
        instances: list["_FakeStream"] = []

        def __init__(self, *_args, **_kwargs) -> None:
            self.started = 0
            self.stopped = 0
            self.timeouts: list[float] = []
            self.trader_client = object()
            self.__class__.instances.append(self)

        def start(self) -> None:
            self.started += 1

        def stop(self) -> None:
            self.stopped += 1

        def wait_connected(self, timeout: float = 30, interval: float = 0.5) -> bool:
            self.timeouts.append(timeout)
            return outcomes.pop(0)

    return _FakeStream


def test_ctp_gateway_adapter_retries_startup_and_cleans_failed_streams(monkeypatch) -> None:
    market_stream = _stream_class([False, True])
    trade_stream = _stream_class([True])

    monkeypatch.setattr(adapter_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(adapter_module, "CtpMarketStream", market_stream)
    monkeypatch.setattr(adapter_module, "CtpTradeStream", trade_stream)
    monkeypatch.setattr(adapter_module, "CtpRequestDataFuture", _FakeFeed)
    monkeypatch.setattr(adapter_module.time, "sleep", lambda _seconds: None)

    adapter = adapter_module.CtpGatewayAdapter(
        gateway_startup_timeout_sec=60.0,
        gateway_startup_attempts=2,
        gateway_startup_retry_backoff_sec=0.0,
    )

    adapter.connect()

    assert adapter.running is True
    assert len(market_stream.instances) == 2
    assert len(trade_stream.instances) == 2
    assert market_stream.instances[0].stopped == 1
    assert trade_stream.instances[0].stopped == 1
    assert market_stream.instances[0].timeouts == [15.0]
    assert market_stream.instances[1].timeouts == [15.0]
    assert trade_stream.instances[1].timeouts == [15.0]

    adapter.disconnect()
