"""离线契约测试：行情会话的断线/重连必须可观测（整改方案 P1-1）。

CTP 原生层只在 stdout 打印 ``OnSessionDisconnected``，且缓冲到进程退出才刷出。
如果这里不留下带时间戳、原因码与连接代次的记录，无人值守时断线完全不可见——
2026-09-17 的 12 次盘中断线就是这样被漏掉的。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from bt_api_ctp.ctp.client import MdClient, _MdSpi


def _client() -> MdClient:
    return MdClient("tcp://127.0.0.1:1", "9999", "user", "pass")


class TestDisconnectCallback:
    """断线必须能被上层订阅（用于把断线时间窗写进完整性报告）。"""

    def test_on_disconnect_defaults_to_none(self) -> None:
        assert _client().on_disconnect is None

    def test_spi_forwards_disconnect_reason(self) -> None:
        client = _client()
        spi = _MdSpi(client)  # native_api=None -> 可离线构造
        seen: list[object] = []
        client.on_disconnect = seen.append

        spi.OnFrontDisconnected(8193)

        assert seen == [8193]
        assert client._connected is False
        assert client._loggedin is False

    def test_spi_tolerates_missing_disconnect_callback(self) -> None:
        client = _client()
        spi = _MdSpi(client)

        spi.OnFrontDisconnected(4097)  # 不得抛出

    def test_retired_session_disconnect_is_not_forwarded(self) -> None:
        client = _client()
        api = SimpleNamespace()
        spi = _MdSpi(client, native_api=api)
        client._api = api
        client._spi = spi
        seen: list[object] = []
        client.on_disconnect = seen.append

        client._api = None  # 会话已停用

        spi.OnFrontDisconnected(8193)

        assert seen == []


class TestSessionLogging:
    """连接、断线、登录都必须在运行日志里留痕。"""

    def test_disconnect_is_logged_with_reason_and_generation(self, caplog) -> None:
        client = _client()
        spi = _MdSpi(client)
        client._connected = True
        client._connection_generation = 3

        with caplog.at_level(logging.WARNING):
            spi.OnFrontDisconnected(8193)

        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "8193" in messages
        assert "generation=3" in messages

    def test_front_connected_is_logged_with_generation(self, caplog) -> None:
        client = _client()
        spi = _MdSpi(client)

        with caplog.at_level(logging.INFO):
            spi.OnFrontConnected()

        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "generation=1" in messages

    def test_login_success_is_logged(self, caplog) -> None:
        client = _client()
        spi = _MdSpi(client)

        with caplog.at_level(logging.INFO):
            spi.OnRspUserLogin(
                SimpleNamespace(TradingDay="20260917"), SimpleNamespace(ErrorID=0), 1, True
            )

        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "login ok" in messages.lower()
        assert "20260917" in messages

    def test_login_failure_is_logged(self, caplog) -> None:
        client = _client()
        spi = _MdSpi(client)

        with caplog.at_level(logging.WARNING):
            spi.OnRspUserLogin(
                None, SimpleNamespace(ErrorID=3, ErrorMsg="bad password"), 1, True
            )

        messages = " ".join(record.getMessage() for record in caplog.records)
        assert "3" in messages
        assert "bad password" in messages
