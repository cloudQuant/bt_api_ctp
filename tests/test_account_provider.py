from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from bt_api_base.account_protocol import AccountKey

from bt_api_ctp.account_provider import (
    CtpAccountSessionProviderError,
    CtpReadOnlyAccountSessionProvider,
)
from bt_api_ctp.ctp import client as ctp_client_module
from bt_api_ctp.ctp.client import (
    CtpNativeStartupStopReceipt,
    CtpNativeStopReceipt,
)
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
from bt_api_ctp.query import (
    QueryResult,
    _attach_query_source,
    _new_query_session_scope,
    _new_query_source,
    _query_records_digest,
)

_ACCOUNT_KEY = AccountKey(
    provider="ctp",
    venue="simnow",
    environment="simnow",
    account_id="investor-test",
    subaccount_id="broker-test",
)
_STARTED = datetime(2026, 10, 17, 1, 29, tzinfo=timezone.utc)
_COMPLETED = datetime(2026, 10, 17, 1, 30, tzinfo=timezone.utc)
_FINGERPRINT = "a" * 64


class _Trader:
    def __init__(self) -> None:
        self.stop_receipts = [
            CtpNativeStopReceipt(7, False, True, False, False, False),
            CtpNativeStopReceipt(7, True, True, True, False, False),
        ]
        self.stop_calls = 0

    def stop_and_wait(self, *, timeout: float) -> CtpNativeStopReceipt:
        assert timeout > 0
        self.stop_calls += 1
        return self.stop_receipts.pop(0)


class _Feed:
    def __init__(self) -> None:
        self._request_feed = None
        self._trader = None
        self._connected = False
        self.connect_calls = 0
        self.rows = {
            "account": (
                {
                    "AccountID": "investor-test",
                    "BrokerID": "broker-test",
                    "CurrencyID": "CNY",
                    "Balance": 1000.0,
                    "Available": 800.0,
                },
            ),
            "positions": (
                {
                    "InstrumentID": "IF2612",
                    "ExchangeID": "CFFEX",
                    "PosiDirection": "2",
                    "Position": 2,
                },
                {
                    "InstrumentID": "IF2612",
                    "ExchangeID": "CFFEX",
                    "PosiDirection": "2",
                    "Position": 1,
                },
            ),
            "orders": (
                {
                    "OrderStatus": "1",
                    "Direction": "1",
                    "CombOffsetFlag": "1",
                    "VolumeTotalOriginal": 3,
                    "VolumeTraded": 1,
                    "InstrumentID": "IF2612",
                    "ExchangeID": "CFFEX",
                    "OrderSysID": "sys-order-1",
                    "OrderRef": "client-order-1",
                    "TradingDay": "20261017",
                    "FrontID": 3,
                    "SessionID": 4,
                    "OrderPriceType": "2",
                    "LimitPrice": 4001.0,
                    "InsertDate": "20261017",
                    "InsertTime": "09:30:00",
                },
            ),
        }
        self.mutate_scope_after_orders = False
        self.mutate_source_after_query = False

    def connect(self) -> None:
        self.connect_calls += 1
        if self._trader is None:
            self._trader = _Trader()
        self._connected = True

    def get_session_state(self) -> dict[str, object]:
        return {"read_only_ready": True, "auto_settlement_confirm": False}

    def get_query_session_scope(self):
        generation = 8 if self.mutate_scope_after_orders else 7
        return _new_query_session_scope(
            issuer=self._trader,
            account_fingerprint=_FINGERPRINT,
            connection_generation=generation,
            trading_day="20261017",
            broker_id="broker-test",
            investor_id="investor-test",
            read_only_ready=True,
            captured_at_utc=_COMPLETED,
            captured_monotonic=100.0,
        )

    def query_account_result(self, *, timeout: float):
        return self._query("account", timeout)

    def query_positions_result(self, *, timeout: float):
        return self._query("positions", timeout)

    def query_orders_result(self, *, timeout: float):
        result = self._query("orders", timeout)
        if self.mutate_scope_after_orders:
            self.mutate_scope_after_orders = True
        return result

    def _query(self, query_name: str, timeout: float):
        assert timeout > 0
        rows = list(self.rows[query_name])
        result = QueryResult(
            request_type=query_name,
            request_id={"account": 1, "positions": 2, "orders": 3}[query_name],
            connection_generation=7,
            account_fingerprint=_FINGERPRINT,
            started_at_utc=_STARTED,
            completed_at_utc=_COMPLETED,
            is_last_seen=True,
            error_code=0,
            error_message="",
            timed_out=False,
            complete=True,
            records=tuple(rows),
        )
        source = _new_query_source(
            issuer=self._trader,
            request_type=query_name,
            request_id=result.request_id,
            account_fingerprint=_FINGERPRINT,
            connection_generation=7,
            trading_day="20261017",
            broker_id="broker-test",
            investor_id="investor-test",
            started_at_utc=_STARTED,
            completed_at_utc=_COMPLETED,
            started_monotonic=100.0,
            completed_monotonic=101.0,
            records_sha256=_query_records_digest(result.records),
        )
        result = _attach_query_source(result, source)
        if self.mutate_source_after_query:
            rows[0]["Balance"] = 999.0
        return result


def _provider(feed: _Feed) -> CtpReadOnlyAccountSessionProvider:
    return CtpReadOnlyAccountSessionProvider(
        lambda: feed,
        monotonic_clock=lambda: 102.0,
        utc_clock=lambda: datetime(2026, 10, 17, 2, 0, tzinfo=timezone.utc),
    )


def test_ctp_account_provider_projects_complete_generation_bound_readonly_snapshot() -> None:
    feed = _Feed()
    provider = _provider(feed)
    assert provider.native_owner_created is False
    assert provider.native_connection_generation is None
    assert provider.last_native_stop_receipt is None

    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    assert provider.native_owner_created is True
    assert provider.native_connection_generation == 7
    assert provider.last_native_stop_receipt is None

    observation = provider.read_account(generation="service-gen-1", timeout=2.0)

    assert feed.connect_calls == 1
    assert observation.account_key is _ACCOUNT_KEY
    assert observation.consistency == "multi_query"
    assert observation.balances_complete is True
    assert observation.positions_complete is True
    assert observation.open_orders_complete is True
    assert observation.balances[0].to_dict() == {
        "resource": {"product_type": "future", "margin_pool": "futures"},
        "currency": "CNY",
        "total": "1000",
        "available": "800",
        "locked": None,
        "observed_at_utc": "2026-10-17T01:30:00.000000Z",
    }
    assert len(observation.positions) == 1
    position = observation.positions[0]
    assert (position.instrument.instrument_id, position.side, position.quantity) == (
        "IF2612",
        "long",
        "3",
    )
    assert len(observation.open_orders or ()) == 1
    order = observation.open_orders[0]
    assert (order.order_id, order.side, order.position_side, order.status) == (
        "CFFEX:sys-order-1",
        "sell",
        "long",
        "partially_filled",
    )
    assert (order.quantity, order.filled_quantity, order.limit_price) == ("3", "1", "4001")
    original_trader = feed._trader
    complete_receipt = original_trader.stop_receipts[-1]
    assert provider.close() is False
    assert original_trader.stop_calls == 1
    assert provider.last_native_stop_receipt is None
    assert provider.close() is True
    assert original_trader.stop_calls == 2
    assert feed._trader is None
    assert provider.native_owner_created is True
    assert provider.native_connection_generation == 7
    assert provider.last_native_stop_receipt is complete_receipt
    assert provider.close() is True

    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-2", timeout=2.0)
    assert feed.connect_calls == 2
    assert feed._trader is not original_trader
    assert provider.native_owner_created is True
    assert provider.native_connection_generation == 7
    assert provider.last_native_stop_receipt is None
    assert provider.close() is False
    assert provider.close() is True


def test_ctp_account_provider_keeps_interrupted_startup_uncertain_until_receipt() -> None:
    class InterruptedFeed(_Feed):
        def connect(self) -> None:
            self.connect_calls += 1
            self._trader = _Trader()
            raise KeyboardInterrupt

    feed = InterruptedFeed()
    provider = _provider(feed)
    with pytest.raises(KeyboardInterrupt):
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)

    with pytest.raises(CtpAccountSessionProviderError, match="NATIVE_CUSTODY_UNCERTAIN"):
        _ = provider.native_owner_created
    with pytest.raises(CtpAccountSessionProviderError, match="NATIVE_CUSTODY_UNCERTAIN"):
        _ = provider.native_connection_generation
    assert provider.last_native_stop_receipt is None

    complete_receipt = feed._trader.stop_receipts[-1]
    assert provider.close() is False
    assert provider.last_native_stop_receipt is None
    assert provider.close() is True
    assert provider.native_owner_created is True
    assert provider.native_connection_generation == 7
    assert provider.last_native_stop_receipt is complete_receipt


def test_production_feed_rejects_completed_preconnection_generation_zero_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_package = Path(__file__).resolve().parents[1] / "src" / "bt_api_ctp"
    assert Path(ctp_client_module.__file__).resolve() == source_package / "ctp" / "client.py"
    assert (
        Path(sys.modules[CtpReadOnlyAccountSessionProvider.__module__].__file__).resolve()
        == source_package / "account_provider.py"
    )
    assert (
        Path(sys.modules[CtpRequestDataFuture.__module__].__file__).resolve()
        == source_package / "feeds" / "live_ctp_feed.py"
    )

    class JoinGateApi:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []
            self.join_entered = threading.Event()
            self.allow_join_return = threading.Event()
            self.join_returned = threading.Event()
            self.released = threading.Event()

        def RegisterSpi(self, spi: object | None) -> None:
            self.calls.append(("register_spi", spi))

        def SubscribePrivateTopic(self, mode: int) -> None:
            self.calls.append(("subscribe_private", mode))

        def SubscribePublicTopic(self, mode: int) -> None:
            self.calls.append(("subscribe_public", mode))

        def RegisterFront(self, front: str) -> None:
            self.calls.append(("register_front", front))

        def Init(self) -> None:
            self.calls.append(("init", None))

        def Join(self) -> int:
            self.calls.append(("join", None))
            self.join_entered.set()
            if not self.allow_join_return.wait(2.0):
                raise AssertionError("synthetic Join was not released by the test")
            self.join_returned.set()
            return 0

        def Release(self) -> None:
            self.calls.append(("release", None))
            self.released.set()

    api = JoinGateApi()
    monkeypatch.setattr(ctp_client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(ctp_client_module, "_flow_dir", lambda _prefix: "synthetic-flow")
    monkeypatch.setattr(ctp_client_module, "_TraderSpi", lambda _client, _api: SimpleNamespace())
    monkeypatch.setattr(
        ctp_client_module,
        "CThostFtdcTraderApi",
        SimpleNamespace(CreateFtdcTraderApi=lambda _flow: api),
    )

    feed = CtpRequestDataFuture(
        broker_id="broker-test",
        user_id="investor-test",
        password="synthetic-only",
        td_front="tcp://127.0.0.1:19991",
        md_front="tcp://127.0.0.1:19992",
        auto_settlement_confirm=False,
    )
    feed._connect_timeout = 0.01
    provider = CtpReadOnlyAccountSessionProvider(
        lambda: feed,
        stop_timeout_seconds=0.05,
        monotonic_clock=lambda: 102.0,
        utc_clock=lambda: datetime(2026, 10, 17, 2, 0, tzinfo=timezone.utc),
    )
    trader = None

    try:
        with pytest.raises(CtpAccountSessionProviderError, match="SESSION_NOT_CONNECTED"):
            provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=1.0)

        trader = feed._trader
        assert trader is not None
        assert api.join_entered.wait(1.0)
        assert trader._connection_generation == 0
        with pytest.raises(CtpAccountSessionProviderError, match="NATIVE_CUSTODY_UNCERTAIN"):
            _ = provider.native_owner_created

        # A bounded close while Join is pending preserves the exact feed/API.
        assert provider.close() is False
        assert not api.released.is_set()
        assert provider._request_feed is feed
        assert feed._trader is trader

        # Once Join returns, the retained API is released and the provider can
        # confirm the exact generation-zero startup stop.
        api.allow_join_return.set()
        assert api.join_returned.wait(1.0)
        assert api.released.wait(1.0)
        assert provider.close() is True
        receipt = provider.last_native_startup_stop_receipt
        assert type(receipt) is CtpNativeStartupStopReceipt
        assert receipt.outcome == "api_created_released"
        assert receipt.connection_generation == 0
        assert receipt.complete is True
        assert receipt.native_released is True
        assert receipt.join_required is True
        assert receipt.join_completed is True
        assert receipt.thread_alive is False
        assert receipt.timed_out is False
        assert api.calls.count(("release", None)) == 1
        assert provider.last_native_stop_receipt is None
        assert provider._request_feed is None
        assert feed._trader is None
    finally:
        api.allow_join_return.set()
        current_trader = feed._trader or trader
        if current_trader is not None:
            join_thread = current_trader._last_stop_join_thread or current_trader._thread
            if join_thread is not None and join_thread is not threading.current_thread():
                join_thread.join(1.0)


def test_exact_ctp_feed_constructor_failure_confirms_native_free_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls = []
    start_calls = []

    class ConstructorFailure:
        def __init__(self, *_args, **_kwargs) -> None:
            constructor_calls.append(True)
            raise RuntimeError("synthetic TraderClient constructor failure")

        def start(self, *_args, **_kwargs) -> None:
            start_calls.append(True)

    monkeypatch.setattr(
        "bt_api_ctp.feeds.live_ctp_feed.ctp_client.TraderClient",
        ConstructorFailure,
    )
    feed = CtpRequestDataFuture(
        broker_id="broker-test",
        user_id="investor-test",
        password="synthetic-only",
        td_front="tcp://test-td",
        md_front="tcp://test-md",
        auto_settlement_confirm=False,
    )
    provider = _provider(feed)

    with pytest.raises(CtpAccountSessionProviderError, match="SESSION_NOT_CONNECTED"):
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)

    # The production feed assigns _trader only after TraderClient construction,
    # and calls start only after assignment. Its native API factory is in start.
    assert constructor_calls == [True]
    assert start_calls == []
    assert feed._trader is None
    assert provider.native_owner_created is False
    assert provider.native_connection_generation is None
    assert provider.close() is True
    assert provider.native_owner_created is False
    assert provider.native_connection_generation is None


def test_custom_feed_without_trader_remains_unknown_after_connect_failure() -> None:
    class CustomFeed:
        _request_feed = None
        _trader = None

        def connect(self) -> None:
            raise RuntimeError("synthetic custom feed failure")

    feed = CustomFeed()
    provider = _provider(feed)  # type: ignore[arg-type]

    with pytest.raises(CtpAccountSessionProviderError, match="SESSION_NOT_CONNECTED"):
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)

    with pytest.raises(CtpAccountSessionProviderError, match="NATIVE_CUSTODY_UNCERTAIN"):
        _ = provider.native_owner_created
    assert provider.close() is False
    assert provider._request_feed is feed
    with pytest.raises(CtpAccountSessionProviderError, match="NATIVE_CUSTODY_UNCERTAIN"):
        _ = provider.native_owner_created


def test_ctp_account_provider_rejects_mutated_query_rows_and_scope_change() -> None:
    mutated_feed = _Feed()
    mutated_feed.mutate_source_after_query = True
    provider = _provider(mutated_feed)
    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_SOURCE_UNTRUSTED"):
        provider.read_account(generation="service-gen-1", timeout=2.0)

    changed_feed = _Feed()
    changed_provider = _provider(changed_feed)
    changed_provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    changed_feed.mutate_scope_after_orders = True
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_SCOPE_CHANGED"):
        changed_provider.read_account(generation="service-gen-1", timeout=2.0)


def test_ctp_account_provider_rejects_borrowed_request_feed_before_connect_or_stop() -> None:
    class BorrowedRequestFeed:
        def __init__(self) -> None:
            self._trader = _Trader()
            self.connect_calls = 0

        def connect(self) -> None:
            self.connect_calls += 1

    class WrapperFeed(_Feed):
        def __init__(self, request_feed: BorrowedRequestFeed) -> None:
            super().__init__()
            self._request_feed = request_feed

    borrowed = BorrowedRequestFeed()
    provider = _provider(WrapperFeed(borrowed))

    with pytest.raises(
        CtpAccountSessionProviderError, match="INVALID_PROVIDER_CONFIGURATION"
    ):
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)

    assert borrowed.connect_calls == 0
    assert borrowed._trader.stop_calls == 0
    assert provider.native_owner_created is False
    assert provider.native_connection_generation is None
    assert provider.close() is True
    assert borrowed._trader.stop_calls == 0


def test_ctp_account_provider_rejects_preconnected_borrowed_trader_without_stopping_it() -> None:
    feed = _Feed()
    borrowed = _Trader()
    feed._trader = borrowed
    provider = _provider(feed)

    with pytest.raises(
        CtpAccountSessionProviderError, match="INVALID_PROVIDER_CONFIGURATION"
    ):
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)

    assert feed.connect_calls == 0
    assert borrowed.stop_calls == 0
    assert provider.close() is True
    assert borrowed.stop_calls == 0
