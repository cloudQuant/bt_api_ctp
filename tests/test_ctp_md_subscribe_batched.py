"""离线契约测试：MdClient.subscribe_batched 分批订阅与重连语义。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import MdClient


def _client() -> MdClient:
    return MdClient("tcp://127.0.0.1:1", "9999", "user", "pass")


class TestSubscribeBatched:
    def test_splits_into_configured_batches(self):
        client = _client()
        batches: list[list[str]] = []
        client._api = SimpleNamespace(
            SubscribeMarketData=lambda batch: batches.append(list(batch))
        )
        client._loggedin = True

        client.subscribe_batched(
            [f"i{index}" for index in range(250)], batch_size=100, interval_sec=0.0
        )

        assert [len(batch) for batch in batches] == [100, 100, 50]
        assert batches[0][0] == "i0"
        assert batches[2][-1] == "i249"

    def test_pending_always_holds_the_full_set_for_reconnect(self):
        client = _client()
        client._api = SimpleNamespace(SubscribeMarketData=lambda batch: None)
        client._loggedin = True
        instruments = [f"i{index}" for index in range(250)]

        client.subscribe_batched(instruments, batch_size=100, interval_sec=0.0)

        assert client._pending_instruments == instruments

    def test_before_login_only_records_pending(self):
        client = _client()
        client._api = SimpleNamespace(
            SubscribeMarketData=lambda batch: pytest.fail("must not submit before login")
        )
        client._loggedin = False

        client.subscribe_batched(["rb2510", "cu2510"], batch_size=1, interval_sec=0.0)

        assert client._pending_instruments == ["rb2510", "cu2510"]

    def test_single_batch_when_below_batch_size(self):
        client = _client()
        batches: list[list[str]] = []
        client._api = SimpleNamespace(
            SubscribeMarketData=lambda batch: batches.append(list(batch))
        )
        client._loggedin = True

        client.subscribe_batched(["rb2510", "cu2510"], batch_size=100, interval_sec=0.0)

        assert [len(batch) for batch in batches] == [2]

    def test_empty_list_is_a_noop(self):
        client = _client()
        batches: list[list[str]] = []
        client._api = SimpleNamespace(
            SubscribeMarketData=lambda batch: batches.append(list(batch))
        )
        client._loggedin = True

        client.subscribe_batched([], batch_size=10, interval_sec=0.0)

        assert batches == []
        assert client._pending_instruments == []

    def test_invalid_batch_size_rejected(self):
        client = _client()
        with pytest.raises(ValueError):
            client.subscribe_batched(["rb2510"], batch_size=0)

    def test_existing_subscribe_semantics_unchanged(self):
        client = _client()
        batches: list[list[str]] = []
        client._api = SimpleNamespace(
            SubscribeMarketData=lambda batch: batches.append(list(batch))
        )
        client._loggedin = True

        client.subscribe(["rb2510"])

        assert batches == [["rb2510"]]
        assert client._pending_instruments == ["rb2510"]


class TestMdSubscribeResponseCallback:
    """订阅响应必须可观测，否则无人值守时无法发现订阅失败。"""

    def test_on_subscribe_defaults_to_none(self):
        client = _client()
        assert client.on_subscribe is None

    def test_spi_forwards_subscribe_responses(self):
        from bt_api_ctp.ctp.client import _MdSpi

        client = _client()
        spi = _MdSpi(client)  # native_api=None -> 可离线构造
        seen = []
        client.on_subscribe = lambda field, info: seen.append((field, info))

        field = SimpleNamespace(InstrumentID="rb2701")
        info = SimpleNamespace(ErrorID=0)
        spi.OnRspSubMarketData(field, info, 1, True)

        assert seen == [(field, info)]

    def test_spi_tolerates_missing_callback(self):
        from bt_api_ctp.ctp.client import _MdSpi

        client = _client()
        spi = _MdSpi(client)

        spi.OnRspSubMarketData(SimpleNamespace(InstrumentID="rb2701"), None, 1, True)
