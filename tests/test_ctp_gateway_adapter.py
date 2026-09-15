"""Regression tests for CTP gateway identity and session contracts."""

from __future__ import annotations

from typing import Any, NoReturn

import pytest

from bt_api_ctp.gateway.adapter import CtpGatewayAdapter


class _PayloadReadProbe(dict[str, Any]):
    """Fail if a direct gateway execution method inspects its payload."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def _fail(self, operation: str) -> NoReturn:
        self.reads += 1
        raise AssertionError(f"direct execution rejection inspected payload via {operation}")

    def get(self, key: str, default: Any = None) -> Any:
        self._fail("get")

    def __getitem__(self, key: str) -> Any:
        self._fail("getitem")

    def __contains__(self, key: object) -> bool:
        self._fail("contains")


class _ExecutionIoProbe:
    """Counts any metadata/network-like read and native write attempt."""

    def __init__(self) -> None:
        self.metadata_reads = 0
        self.network_like_reads = 0
        self.writes = 0

    def get_symbol_info(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        self.metadata_reads += 1
        raise AssertionError("direct execution rejection queried instrument metadata")

    def make_order(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        self.writes += 1
        raise AssertionError("direct execution rejection attempted an order write")

    def cancel_order(self, *_args: Any, **_kwargs: Any) -> NoReturn:
        self.writes += 1
        raise AssertionError("direct execution rejection attempted a cancel write")

    def __getattr__(self, name: str) -> NoReturn:
        self.network_like_reads += 1
        raise AssertionError(f"direct execution rejection accessed I/O attribute {name}")


class _FakeTraderClient:
    def get_session_state(self) -> dict[str, Any]:
        return {
            "auth_state": "authenticated",
            "login_state": "logged_in",
            "front_id": 7,
            "session_id": 8801,
            "trading_day": "20260618",
        }


class _FakeTradeStream:
    trader_client = _FakeTraderClient()


def test_get_session_state_forwards_underlying_trader_state() -> None:
    adapter = object.__new__(CtpGatewayAdapter)
    adapter.trade = _FakeTradeStream()

    state = adapter.get_session_state()

    assert state["auth_state"] == "authenticated"
    assert state["login_state"] == "logged_in"
    assert state["front_id"] == 7
    assert state["session_id"] == 8801
    assert state["trading_day"] == "20260618"


@pytest.mark.parametrize(
    ("method_name", "operation"),
    [
        ("place_order", "order submission"),
        ("cancel_order", "order cancellation"),
    ],
)
def test_gateway_adapter_rejects_direct_execution_before_payload_or_io(
    monkeypatch: pytest.MonkeyPatch, method_name: str, operation: str
) -> None:
    """The compatibility adapter must not even prepare a direct CTP request."""

    adapter = object.__new__(CtpGatewayAdapter)
    probe = _ExecutionIoProbe()
    payload = _PayloadReadProbe()
    adapter.feed = probe
    adapter.last_price = probe
    adapter._price_ticks = probe
    adapter._quote_execution_eligible = probe
    monkeypatch.setattr(CtpGatewayAdapter, "get_symbol_info", probe.get_symbol_info)

    with pytest.raises(RuntimeError) as excinfo:
        getattr(adapter, method_name)(payload)

    assert str(excinfo.value) == (
        f"CTP gateway {operation} is disabled without an SDK-managed "
        "execution capability, arm, and preflight"
    )
    assert payload.reads == 0
    assert probe.metadata_reads == 0
    assert probe.network_like_reads == 0
    assert probe.writes == 0
