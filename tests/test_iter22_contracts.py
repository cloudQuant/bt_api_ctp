"""Iteration 22 CTP contracts; all tests are offline fault injection."""

from __future__ import annotations

import hashlib
import json
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bt_api_ctp import get_ctp_native_diagnostics
from bt_api_ctp.containers.ctp.ctp_ticker import CtpTickerData
from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.ctp.client import TraderClient, _TraderSpi
from bt_api_ctp.feeds.live_ctp_feed import (
    CtpMarketStream,
    CtpRequestDataFuture,
    CtpTradeStream,
    CtpVolumeDeltaTracker,
)
from bt_api_ctp.gateway.adapter import CtpGatewayAdapter
from bt_api_ctp.query import QueryResult


def _read_ready(client: TraderClient, *, trading_day: str = "20260909") -> TraderClient:
    client._connected = True
    client._authentication_state = "authenticated"
    client._login_state = "logged_in"
    client._trading_day = trading_day
    client._connection_generation = max(client._connection_generation, 1)
    client._query_interval = 0
    return client


def _result(
    request_type: str,
    records=(),
    *,
    complete=True,
    request_id=7,
    generation=1,
    account_fingerprint="fixture",
) -> QueryResult:
    now = datetime.now(timezone.utc)
    return QueryResult(
        request_type=request_type,
        request_id=request_id,
        connection_generation=generation,
        account_fingerprint=account_fingerprint,
        started_at_utc=now,
        completed_at_utc=now,
        is_last_seen=complete,
        error_code=None,
        error_message="",
        timed_out=False,
        complete=complete,
        records=tuple(records),
    )


def test_request_count_contract_is_closed_even_before_any_request() -> None:
    counts = client_module.empty_ctp_request_counts()

    assert tuple(counts) == client_module.CTP_REQUEST_COUNT_KEYS
    assert all(type(value) is int and value == 0 for value in counts.values())
    assert {"settlement_confirm", "order_insert", "order_action"} <= counts.keys()


def _ctp_execution_proof(
    client: TraderClient,
    feed: CtpRequestDataFuture,
    **changes,
) -> dict:
    proof = {
        "account_fingerprint": f"acct_{client._account_fingerprint}",
        "trading_day": "20260909",
        "instrument": "CZCE.SA609",
        "connection_generation": 1,
        "environment_profile": feed.ctp_env_profile,
        "preflight_sha256": "0" * 64,
        "receipt_sha256": "1" * 64,
        "native_sha256": "2" * 64,
        "ctp_package_sha256": "3" * 64,
        "source_hashes_sha256": "4" * 64,
        "dependency_hashes_sha256": "5" * 64,
    }
    proof.update(changes)
    return proof


def _execution_ready_feed():
    native_calls = []

    class Api:
        def ReqQryOrder(self, _field, _request_id):
            return 0

        def ReqQueryBankAccountMoneyByFuture(self, _field, _request_id):
            return 0

        def ReqOrderInsert(self, field, request_id):
            native_calls.append(("insert", field.InstrumentID, request_id))
            return 0

        def ReqOrderAction(self, field, request_id):
            native_calls.append(("cancel", field.InstrumentID, request_id))
            return 0

    client = TraderClient(
        "tcp://test",
        "9999",
        "account",
        "secret",
        auto_settlement_confirm=False,
    )
    client._api = Api()
    client = _read_ready(client)
    client._session_native_api = client._api
    client._settlement_state = "confirmed"
    client._ready = True
    client._settlement_connection_generation = client._connection_generation
    client._settlement_account_fingerprint = client._account_fingerprint
    client._settlement_trading_day = client._trading_day
    client._settlement_proof_source = "confirmation_query"
    client._settlement_proof_query_request_id = 1
    client._settlement_readback_verified = True
    feed = CtpRequestDataFuture(
        broker_id="9999",
        user_id="account",
        password="secret",
        td_front="tcp://test-td",
        md_front="tcp://test-md",
        auto_settlement_confirm=False,
    )
    feed._trader = client
    return feed, client, native_calls


def test_unmanaged_ctp_feed_preserves_legacy_order_and_cancel_writes() -> None:
    feed, client, native_calls = _execution_ready_feed()

    order = feed.make_order(
        "SA2609", 1, 1200, "buy-limit", exchange_id="CZCE", time_in_force="GFD"
    )
    cancel = feed.cancel_order("SA2609", order_id="SYS", exchange_id="CZCE")

    assert order.get_status() is True and cancel.get_status() is True
    assert [call[0] for call in native_calls] == ["insert", "cancel"]
    assert client.get_request_counts()["order_insert"] == 1
    assert client.get_request_counts()["order_action"] == 1


def test_managed_ctp_feed_is_disarmed_before_proof_and_issues_zero_writes() -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = object()
    first = feed.configure_execution_gate(capability)
    second = feed.configure_execution_gate(capability)

    assert first == second and first["managed"] is True and first["armed"] is False
    for supplied in (None, capability):
        with pytest.raises(client_module.CtpExecutionGateError) as excinfo:
            feed.make_order(
                "SA2609",
                1,
                1200,
                "buy-limit",
                exchange_id="CZCE",
                _execution_capability=supplied,
            )
        assert "secret" not in str(excinfo.value)
    assert native_calls == []
    assert client.get_request_counts()["order_insert"] == 0


def test_managed_ctp_feed_writes_only_with_bound_proof_contract_and_token() -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = object()
    feed.configure_execution_gate(capability)
    proof = _ctp_execution_proof(client, feed)

    first = feed.arm_execution_gate(capability, proof)
    second = feed.arm_execution_gate(capability, dict(proof))
    assert first == second and first["armed"] is True
    assert first["instrument"] == "CZCE.SA609"
    assert len(first["proof_sha256"]) == 64

    feed.make_order(
        "SA2609",
        1,
        1200,
        "buy-limit",
        exchange_id="CZCE",
        _execution_capability=capability,
    )
    feed.cancel_order(
        "SA2609",
        order_id="SYS",
        exchange_id="CZCE",
        _execution_capability=capability,
    )

    assert [call[0] for call in native_calls] == ["insert", "cancel"]
    assert client.get_request_counts()["order_insert"] == 1
    assert client.get_request_counts()["order_action"] == 1


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda feed, client, capability: (object(), "SA2609", "CZCE"),
            "ctp_execution_gate_capability_mismatch",
        ),
        (
            lambda feed, client, capability: (capability, "SR609", "CZCE"),
            "ctp_execution_gate_instrument_mismatch",
        ),
    ],
)
@pytest.mark.parametrize("operation", ["make_order", "cancel_order"])
def test_managed_ctp_feed_rejects_wrong_token_or_contract_before_native_write(
    mutation, expected_code, operation
) -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = object()
    feed.configure_execution_gate(capability)
    feed.arm_execution_gate(capability, _ctp_execution_proof(client, feed))
    supplied, symbol, exchange_id = mutation(feed, client, capability)

    with pytest.raises(client_module.CtpExecutionGateError) as excinfo:
        if operation == "make_order":
            feed.make_order(
                symbol,
                1,
                1200,
                "buy-limit",
                exchange_id=exchange_id,
                _execution_capability=supplied,
            )
        else:
            feed.cancel_order(
                symbol,
                order_id="SYS",
                exchange_id=exchange_id,
                _execution_capability=supplied,
            )

    assert excinfo.value.code == expected_code
    assert native_calls == []
    assert client.get_request_counts()["order_insert"] == 0
    assert client.get_request_counts()["order_action"] == 0


def test_managed_ctp_feed_rejects_wrong_generation_before_native_write() -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = object()
    feed.configure_execution_gate(capability)
    feed.arm_execution_gate(capability, _ctp_execution_proof(client, feed))
    client._connection_generation += 1

    with pytest.raises(client_module.CtpExecutionGateError) as excinfo:
        feed.cancel_order(
            "SA2609",
            order_id="SYS",
            exchange_id="CZCE",
            _execution_capability=capability,
        )

    assert excinfo.value.code == "ctp_execution_gate_connection_generation_mismatch"
    assert feed.get_execution_gate_state()["armed"] is False
    assert native_calls == []
    assert client.get_request_counts()["order_action"] == 0


def test_managed_ctp_feed_rechecks_generation_at_native_submit_boundary() -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = object()
    feed.configure_execution_gate(capability)
    feed.arm_execution_gate(capability, _ctp_execution_proof(client, feed))
    next_request_id = client._next_request_id

    def change_generation_after_initial_gate_check() -> int:
        request_id = next_request_id()
        client._connection_generation += 1
        return request_id

    client._next_request_id = change_generation_after_initial_gate_check

    with pytest.raises(client_module.CtpExecutionGateError) as excinfo:
        feed.make_order(
            "SA2609",
            1,
            1200,
            "buy-limit",
            exchange_id="CZCE",
            _execution_capability=capability,
        )

    assert excinfo.value.code == "ctp_execution_gate_connection_generation_mismatch"
    assert native_calls == []
    assert client.get_request_counts()["order_insert"] == 0


def test_reconnect_and_explicit_revoke_both_disable_managed_native_writes() -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = object()
    proof = _ctp_execution_proof(client, feed)
    feed.configure_execution_gate(capability)
    feed.arm_execution_gate(capability, proof)
    revoked = feed.disarm_execution_gate(capability, "test_revoked")
    repeated = feed.disarm_execution_gate(capability, "ignored_later_reason")
    assert revoked == repeated and revoked["revocation_reason"] == "test_revoked"

    with pytest.raises(client_module.CtpExecutionGateError, match="unarmed"):
        feed.cancel_order(
            "SA2609",
            order_id="SYS",
            exchange_id="CZCE",
            _execution_capability=capability,
        )

    feed.arm_execution_gate(capability, proof)
    client._on_front_disconnected(1)
    client._on_front_connected()
    with pytest.raises(client_module.CtpExecutionGateError, match="unarmed"):
        feed.make_order(
            "SA2609",
            1,
            1200,
            "buy-limit",
            exchange_id="CZCE",
            _execution_capability=capability,
        )
    assert native_calls == []
    assert client.get_request_counts()["order_insert"] == 0
    assert client.get_request_counts()["order_action"] == 0


@pytest.mark.parametrize(
    "method_name",
    [
        "ReqOrderInsert",
        "ReqOrderAction",
        "ReqSettlementInfoConfirm",
        "ReqParkedOrderInsert",
        "ReqParkedOrderAction",
        "ReqRemoveParkedOrder",
        "ReqRemoveParkedOrderAction",
        "ReqBatchOrderAction",
        "ReqExecOrderInsert",
        "ReqQuoteInsert",
        "ReqOptionSelfCloseInsert",
        "ReqOptionSelfCloseAction",
        "ReqFromBankToFutureByFuture",
        "ReqFromFutureToBankByFuture",
        "ReqUserPasswordUpdate",
        "ReqTradingAccountPasswordUpdate",
    ],
)
def test_managed_trader_api_blocks_direct_native_write_methods(
    method_name: str,
) -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = object()
    feed.configure_execution_gate(capability)
    feed.arm_execution_gate(
        capability,
        _ctp_execution_proof(client, feed),
    )

    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_native_write_blocked",
    ):
        getattr(client.api, method_name)(object(), 1)
    assert native_calls == []


def test_managed_trader_api_blocks_cached_raw_query_requests() -> None:
    feed, client, native_calls = _execution_ready_feed()
    raw_calls = []
    client._api.ReqQryOrder = lambda *_args: raw_calls.append("qry") or 0
    client._api.ReqQueryBankAccountMoneyByFuture = (
        lambda *_args: raw_calls.append("query") or 0
    )
    public_api = client.api
    cached_qry = public_api.ReqQryOrder
    cached_query = public_api.ReqQueryBankAccountMoneyByFuture

    assert cached_qry(object(), 1) == 0
    assert cached_query(object(), 2) == 0
    assert raw_calls == ["qry", "query"]

    capability = object()
    feed.configure_execution_gate(capability)

    for raw_query in (
        cached_qry,
        cached_query,
        public_api.ReqQryOrder,
        public_api.ReqQueryBankAccountMoneyByFuture,
        client.api.ReqQryOrder,
        client.api.ReqQueryBankAccountMoneyByFuture,
    ):
        with pytest.raises(
            client_module.CtpExecutionGateError,
            match="ctp_execution_gate_raw_request_blocked",
        ):
            raw_query(object(), 3)

    assert raw_calls == ["qry", "query"]
    assert native_calls == []


def test_managed_trader_api_blocks_cached_non_request_native_callables() -> None:
    feed, client, native_calls = _execution_ready_feed()
    lifecycle_calls = []
    client._api.Release = lambda: lifecycle_calls.append("release")
    public_api = client.api
    cached_release = public_api.Release

    cached_release()
    assert lifecycle_calls == ["release"]

    capability = object()
    feed.configure_execution_gate(capability)
    for lifecycle_call in (cached_release, public_api.Release, client.api.Release):
        with pytest.raises(
            client_module.CtpExecutionGateError,
            match="ctp_execution_gate_native_write_blocked",
        ):
            lifecycle_call()

    assert lifecycle_calls == ["release"]
    assert native_calls == []


def test_cached_public_api_and_req_callable_recheck_gate_at_invocation() -> None:
    feed, client, native_calls = _execution_ready_feed()
    raw_api = client._api
    public_api = client.api
    cached_insert = public_api.ReqOrderInsert
    field = SimpleNamespace(InstrumentID="SA2609")

    assert client.api is public_api
    assert all(value is not raw_api for value in vars(public_api).values())
    assert cached_insert(field, 1) == 0
    assert native_calls == [("insert", "SA2609", 1)]

    capability = object()
    feed.configure_execution_gate(capability)
    for write in (cached_insert, public_api.ReqOrderInsert, client.api.ReqOrderInsert):
        with pytest.raises(
            client_module.CtpExecutionGateError,
            match="ctp_execution_gate_native_write_blocked",
        ):
            write(field, 2)

    assert client.api is public_api
    assert native_calls == [("insert", "SA2609", 1)]


def test_api_swap_revokes_proof_and_old_public_handles_remain_blocked() -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = object()
    feed.configure_execution_gate(capability)
    feed.arm_execution_gate(capability, _ctp_execution_proof(client, feed))
    public_api = client.api
    cached_insert = public_api.ReqOrderInsert
    cached_query = public_api.ReqQryOrder

    replacement_calls = []
    client._api = SimpleNamespace(
        ReqOrderInsert=lambda *_args: replacement_calls.append("insert") or 0,
        ReqQryOrder=lambda *_args: replacement_calls.append("query") or 0,
    )

    state = client.get_execution_gate_state()
    assert state["armed"] is False
    assert state["revocation_reason"] == "ctp_execution_gate_native_api_changed"
    for write in (cached_insert, public_api.ReqOrderInsert, client.api.ReqOrderInsert):
        with pytest.raises(
            client_module.CtpExecutionGateError,
            match="ctp_execution_gate_native_write_blocked",
        ):
            write(SimpleNamespace(InstrumentID="SA2609"), 3)

    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_raw_request_blocked",
    ):
        cached_query(object(), 5)
    assert native_calls == []
    assert replacement_calls == []


def test_managed_settlement_requires_capability_and_submits_only_once() -> None:
    native_calls = []
    client = TraderClient(
        "tcp://test",
        "9999",
        "account",
        "secret",
        auto_settlement_confirm=False,
    )

    class Api:
        def ReqSettlementInfoConfirm(self, field, request_id):
            native_calls.append((field.BrokerID, field.InvestorID, request_id))
            _TraderSpi(client).OnRspSettlementInfoConfirm(
                SimpleNamespace(
                    BrokerID="9999",
                    InvestorID="account",
                    ConfirmDate="20260909",
                ),
                None,
                request_id,
                True,
            )
            return 0

    client._api = Api()
    client = _read_ready(client)
    client._session_native_api = client._api
    client._settlement_state = "not_requested"
    feed = CtpRequestDataFuture(
        broker_id="9999",
        user_id="account",
        password="secret",
        td_front="tcp://test-td",
        md_front="tcp://test-md",
        auto_settlement_confirm=False,
    )
    feed._trader = client
    capability = object()
    feed.configure_execution_gate(capability)

    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_capability_mismatch",
    ):
        client.confirm_settlement(timeout=0)
    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_capability_mismatch",
    ):
        feed.confirm_settlement(timeout=0, _execution_capability=object())
    assert native_calls == []
    assert client.get_request_counts()["settlement_confirm"] == 0

    assert feed.confirm_settlement(timeout=0, _execution_capability=capability) is True
    assert (
        client.confirm_settlement(timeout=0, _execution_capability=capability) is True
    )
    assert native_calls == [("9999", "account", 1)]
    assert client.get_request_counts()["settlement_confirm"] == 1

    state = client.get_session_state()
    assert state["settlement_state"] == "confirmed"
    assert state["settlement_proof_source"] == "direct_confirmation"
    assert state["settlement_readback_verified"] is False
    assert state["trading_ready"] is False
    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_session_not_trading_ready",
    ):
        feed.arm_execution_gate(capability, _ctp_execution_proof(client, feed))

    client._api.ReqQrySettlementInfoConfirm = (
        lambda _field, request_id: client._handle_query_callback(
            "settlement_confirmation",
            {
                "BrokerID": "9999",
                "InvestorID": "account",
                "ConfirmDate": "20260909",
            },
            None,
            request_id,
            True,
        )
        or 0
    )
    readback = feed.verify_settlement_confirmation(timeout=0)
    assert readback.complete is True
    assert client.get_session_state()["settlement_readback_verified"] is True

    feed.arm_execution_gate(capability, _ctp_execution_proof(client, feed))
    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_settlement_requires_disarmed",
    ):
        feed.confirm_settlement(timeout=0, _execution_capability=capability)
    assert len(native_calls) == 1


def test_managed_settlement_timeout_cannot_resubmit_same_connection() -> None:
    native_calls = []
    client = TraderClient(
        "tcp://test",
        "9999",
        "account",
        "secret",
        auto_settlement_confirm=False,
    )
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda *_args: native_calls.append("confirm") or 0
    )
    client = _read_ready(client)
    client._session_native_api = client._api
    client._settlement_state = "not_requested"
    capability = object()
    client.configure_execution_gate(capability)

    assert (
        client.confirm_settlement(timeout=0, _execution_capability=capability) is False
    )
    assert (
        client.confirm_settlement(timeout=0, _execution_capability=capability) is False
    )
    assert native_calls == ["confirm"]
    assert client.get_request_counts()["settlement_confirm"] == 1


def test_managed_settlement_rejects_auto_confirmation_mode_before_write() -> None:
    native_calls = []
    client = TraderClient("tcp://test", "9999", "account", "secret")
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda *_args: native_calls.append("confirm") or 0
    )
    client = _read_ready(client)
    client._session_native_api = client._api
    capability = object()
    client.configure_execution_gate(capability)

    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_auto_settlement_confirm_enabled",
    ):
        client.confirm_settlement(timeout=0, _execution_capability=capability)
    assert native_calls == []
    assert client.get_request_counts()["settlement_confirm"] == 0


def test_auto_settlement_wait_ready_promotes_only_after_matching_readback() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Api:
        def ReqSettlementInfoConfirm(self, _field, request_id):
            _TraderSpi(client).OnRspSettlementInfoConfirm(
                SimpleNamespace(
                    BrokerID="9999",
                    InvestorID="account",
                    ConfirmDate="20260909",
                ),
                None,
                request_id,
                True,
            )
            return 0

        def ReqQrySettlementInfoConfirm(self, _field, request_id):
            client._handle_query_callback(
                "settlement_confirmation",
                {
                    "BrokerID": "9999",
                    "InvestorID": "account",
                    "ConfirmDate": "20260909",
                },
                None,
                request_id,
                True,
            )
            return 0

    client._api = Api()
    client._session_native_api = client._api
    client._settlement_state = "not_requested"

    assert client._request_settlement_confirmation() is True
    assert client.get_session_state()["settlement_readback_verified"] is False
    assert client.is_trading_ready is False

    assert client.wait_ready(timeout=0.1) is True
    state = client.get_session_state()
    assert state["trading_ready"] is True
    assert state["settlement_readback_verified"] is True
    assert state["request_counts"]["settlement_confirm"] == 1
    assert state["request_counts"]["query_settlement_confirmation"] == 1


def test_reentrant_start_revokes_managed_proof_before_rejecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = object()
    feed.configure_execution_gate(capability)
    feed.arm_execution_gate(capability, _ctp_execution_proof(client, feed))
    cached_insert = client.api.ReqOrderInsert
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)

    with pytest.raises(RuntimeError, match="ctp_trader_client_already_started"):
        client.start(block=False)

    state = client.get_execution_gate_state()
    assert state["armed"] is False
    assert state["revocation_reason"] == "ctp_execution_gate_client_start"
    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_native_write_blocked",
    ):
        cached_insert(SimpleNamespace(InstrumentID="SA2609"), 4)
    assert native_calls == []


def test_cached_public_handle_stays_blocked_after_managed_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_calls = []

    class Api:
        def RegisterSpi(self, _spi):
            return None

        def SubscribePrivateTopic(self, _mode):
            return None

        def SubscribePublicTopic(self, _mode):
            return None

        def RegisterFront(self, _front):
            return None

        def Init(self):
            return None

        def Join(self):
            return None

        def Release(self):
            return None

        def ReqOrderInsert(self, *_args):
            native_calls.append("insert")
            return 0

        def ReqQryOrder(self, *_args):
            native_calls.append("query")
            return 0

    api = Api()
    client = TraderClient(
        "tcp://test",
        "9999",
        "account",
        "secret",
        auto_settlement_confirm=False,
    )
    capability = object()
    client.configure_execution_gate(capability)
    public_api = client.api
    cached_insert = public_api.ReqOrderInsert
    cached_query = public_api.ReqQryOrder
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(
        client_module,
        "CThostFtdcTraderApi",
        SimpleNamespace(CreateFtdcTraderApi=lambda _flow: api),
    )

    client.start(block=False)
    assert client.api is public_api
    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_native_write_blocked",
    ):
        cached_insert(object(), 1)
    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_raw_request_blocked",
    ):
        cached_query(object(), 2)
    assert native_calls == []
    client.stop()


def test_old_spi_callbacks_cannot_restore_state_after_api_swap() -> None:
    feed, client, native_calls = _execution_ready_feed()
    old_api = client._api
    old_spi = _TraderSpi(client, old_api)
    client._spi = old_spi
    capability = object()
    feed.configure_execution_gate(capability)
    feed.arm_execution_gate(capability, _ctp_execution_proof(client, feed))

    client._api = SimpleNamespace(ReqQryOrder=lambda *_args: 0)
    client._settlement_state = "confirming"
    client._settlement_request_id = 99
    client._settlement_connection_generation = client._connection_generation
    client._settlement_account_fingerprint = client._account_fingerprint
    client._settlement_trading_day = client._trading_day
    connected = client._connected
    generation = client._connection_generation
    trading_day = client._trading_day

    old_spi.OnFrontDisconnected(1)
    old_spi.OnRspUserLogin(
        SimpleNamespace(
            FrontID=9,
            SessionID=9,
            TradingDay="20260910",
            MaxOrderRef="9",
        ),
        None,
        9,
        True,
    )
    old_spi.OnRspSettlementInfoConfirm(
        SimpleNamespace(
            BrokerID="9999",
            InvestorID="account",
            ConfirmDate="20260909",
        ),
        None,
        99,
        True,
    )

    assert client._connected is connected
    assert client._connection_generation == generation
    assert client._trading_day == trading_day
    assert client._settlement_state == "confirming"
    assert client.get_execution_gate_state()["armed"] is False
    assert native_calls == []


def test_managed_feed_rejects_replaced_unmanaged_trader_client() -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = object()
    feed.configure_execution_gate(capability)
    feed.arm_execution_gate(capability, _ctp_execution_proof(client, feed))

    replacement = _read_ready(
        TraderClient(
            "tcp://replacement",
            "9999",
            "account",
            "secret",
            auto_settlement_confirm=False,
        )
    )
    replacement._settlement_state = "confirmed"
    replacement._ready = True
    replacement._api = client._api
    feed._trader = replacement

    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_native_contract_unavailable",
    ):
        feed.make_order(
            "SA2609",
            1,
            1200,
            "buy-limit",
            exchange_id="CZCE",
            _execution_capability=capability,
        )
    assert native_calls == []
    assert replacement.get_request_counts()["order_insert"] == 0


def test_bad_execution_proof_disarms_gate_without_leaking_credentials() -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = object()
    feed.configure_execution_gate(capability)
    bad_proof = _ctp_execution_proof(client, feed, receipt_sha256="secret")

    with pytest.raises(client_module.CtpExecutionGateError) as excinfo:
        feed.arm_execution_gate(capability, bad_proof)

    assert excinfo.value.code == "ctp_execution_gate_invalid_proof"
    assert "secret" not in str(excinfo.value)
    assert feed.get_execution_gate_state()["armed"] is False
    assert native_calls == []


def test_query_result_accumulates_only_matching_request_and_terminal_packet() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Api:
        def ReqQryOrder(self, _field, request_id):
            client._handle_query_callback(
                "orders", {"OrderSysID": "A"}, None, request_id, False
            )
            client._handle_query_callback(
                "orders", {"OrderSysID": "B"}, None, request_id, True
            )
            return 0

    client._api = Api()
    result = client.query_orders_result(timeout=0.01)

    assert result.complete is True
    assert [row["OrderSysID"] for row in result.records] == ["A", "B"]
    assert result.request_id == 1 and result.connection_generation == 1
    assert result.evidence_complete is True
    counts = client.get_request_counts()
    assert counts["query_orders"] == 1
    assert counts["settlement_confirm"] == 0
    assert counts["order_insert"] == 0
    assert counts["order_action"] == 0


def test_instrument_query_exposes_expiry_without_inventing_trading_day_ranking() -> (
    None
):
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Api:
        def ReqQryInstrument(self, _field, request_id):
            client._handle_query_callback(
                "instruments",
                SimpleNamespace(InstrumentID="SA601", ExpireDate="20260115"),
                None,
                request_id,
                True,
            )
            return 0

    client._api = Api()
    result = client.query_instruments_result(timeout=0.01)
    row = result.records[0]
    assert result.complete is True and row["expiry_date"] == "20260115"
    assert row["trading_days_to_expiry"] is None
    assert row["remaining_trading_days"] is None
    assert row["trading_calendar_evidence_complete"] is False
    assert row["ranking_trading_day"] is None
    assert row["prior_trading_day_volume"] is None
    assert row["prior_trading_day_open_interest"] is None
    assert row["prior_day_ranking_evidence_complete"] is False


def test_instrument_query_submits_exact_product_filter() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))
    submitted = {}

    class Api:
        def ReqQryInstrument(self, field, request_id):
            submitted["instrument_id"] = field.InstrumentID
            submitted["exchange_id"] = field.ExchangeID
            submitted["product_id"] = field.ProductID
            client._handle_query_callback(
                "instruments",
                {"InstrumentID": "SA601", "ProductID": "SA"},
                None,
                request_id,
                True,
            )
            return 0

    client._api = Api()
    result = client.query_instruments_result(
        exchange_id="CZCE",
        product_id="SA",
        timeout=0.01,
    )

    assert result.complete is True
    assert submitted == {
        "instrument_id": "",
        "exchange_id": "CZCE",
        "product_id": "SA",
    }
    assert client.get_request_counts()["query_instruments"] == 1


def test_instrument_product_filter_fails_closed_when_native_field_is_not_writable(
    monkeypatch,
) -> None:
    class FieldWithoutProductID:
        def __setattr__(self, name, value):
            if name == "ProductID":
                raise AttributeError("ProductID is unavailable")
            super().__setattr__(name, value)

    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))
    client._api = SimpleNamespace(
        ReqQryInstrument=lambda *_args: pytest.fail("unfiltered native query submitted")
    )
    monkeypatch.setattr(
        client_module, "CThostFtdcQryInstrumentField", FieldWithoutProductID
    )

    result = client.query_instruments_result(product_id="SA", timeout=0.01)

    assert result.complete is False
    assert result.unsupported is True
    assert result.error_code == -3
    assert result.error_message == "native_instrument_filter_unsupported:ProductID"
    assert client.get_request_counts()["query_instruments"] == 0


def test_instrument_product_filter_is_forwarded_by_typed_and_public_feed_proxies() -> (
    None
):
    seen = []

    class Trader:
        is_read_only_ready = True

        @staticmethod
        def query_instruments_result(**kwargs):
            seen.append(kwargs)
            return _result(
                "instruments",
                ({"InstrumentID": "SA601", "ProductID": "SA"},),
            )

    feed = CtpRequestDataFuture()
    feed._trader = Trader()

    typed_result = feed.query_instruments_result(
        exchange_id="CZCE",
        product_id="SA",
        timeout=2,
    )
    public_result = feed.get_instruments(
        exchange_id="CZCE",
        product_id="SA",
        timeout=3,
    )

    assert typed_result.complete is True
    assert public_result.get_status() is True
    assert seen == [
        {
            "instrument_id": "",
            "exchange_id": "CZCE",
            "product_id": "SA",
            "timeout": 2,
        },
        {
            "instrument_id": "",
            "exchange_id": "CZCE",
            "product_id": "SA",
            "timeout": 3,
        },
    ]


def test_instrument_feed_proxies_preserve_legacy_query_when_filter_is_omitted() -> None:
    seen = []

    class LegacyTrader:
        is_read_only_ready = True

        @staticmethod
        def query_instruments_result(*, instrument_id, exchange_id, timeout):
            seen.append(
                {
                    "instrument_id": instrument_id,
                    "exchange_id": exchange_id,
                    "timeout": timeout,
                }
            )
            return _result("instruments", ())

    feed = CtpRequestDataFuture()
    feed._trader = LegacyTrader()

    typed_result = feed.query_instruments_result(exchange_id="CZCE", timeout=2)
    public_result = feed.get_instruments(exchange_id="CZCE", timeout=3)

    assert typed_result.complete is True
    assert public_result.get_status() is True
    assert seen == [
        {"instrument_id": "", "exchange_id": "CZCE", "timeout": 2},
        {"instrument_id": "", "exchange_id": "CZCE", "timeout": 3},
    ]


def test_query_timeout_and_late_callback_never_become_empty_success() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))
    client._api = SimpleNamespace(ReqQryTradingAccount=lambda _field, _request_id: 0)

    result = client.query_account_result(timeout=0)
    assert (
        result.complete is False and result.timed_out is True and result.records == ()
    )

    client._handle_query_callback(
        "account", {"Balance": 1}, None, result.request_id, True
    )
    refreshed = client.get_query_result(result.request_id)
    assert refreshed is not None
    assert refreshed.complete is False and refreshed.late_callback_count == 1
    assert refreshed.records == ()


def test_query_callback_detaches_reused_native_record_objects() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Record:
        OrderSysID = "A"

    record = Record()

    class Api:
        def ReqQryOrder(self, _field, request_id):
            client._handle_query_callback("orders", record, None, request_id, False)
            record.OrderSysID = "B"
            client._handle_query_callback("orders", record, None, request_id, True)
            return 0

    client._api = Api()
    result = client.query_orders_result(timeout=0.01)
    assert [row.OrderSysID for row in result.records] == ["A", "B"]


def test_query_callback_after_terminal_packet_is_counted_as_late() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Api:
        def ReqQryOrder(self, _field, request_id):
            client._handle_query_callback(
                "orders", {"OrderSysID": "terminal"}, None, request_id, True
            )
            client._handle_query_callback(
                "orders", {"OrderSysID": "late"}, None, request_id, True
            )
            return 0

    client._api = Api()
    result = client.query_orders_result(timeout=0.01)
    assert [row.OrderSysID for row in result.records] == ["terminal"]
    assert result.complete is True and result.late_callback_count == 1


def test_old_generation_query_callback_is_orphaned() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Api:
        def ReqQryInvestorPosition(self, _field, request_id):
            client._connection_generation += 1
            client._handle_query_callback(
                "positions", {"InstrumentID": "IF"}, None, request_id, True
            )
            return 0

    client._api = Api()
    result = client.query_positions_result(timeout=0)
    assert result.complete is False and result.timed_out is True
    assert result.records == ()
    assert client._orphan_query_callbacks[-1]["request_id"] == result.request_id


def test_read_only_login_cannot_insert_or_cancel_and_counts_zero_writes() -> None:
    client = _read_ready(
        TraderClient(
            "tcp://test", "9999", "account", "secret", auto_settlement_confirm=False
        )
    )
    calls = []
    client._api = SimpleNamespace(
        ReqOrderInsert=lambda *_args: calls.append("insert") or 0,
        ReqOrderAction=lambda *_args: calls.append("cancel") or 0,
    )
    feed = CtpRequestDataFuture(
        queue.Queue(),
        broker_id="9999",
        user_id="account",
        td_front="tcp://test-td",
        md_front="tcp://test-md",
        auto_settlement_confirm=False,
    )
    feed._trader = client

    with pytest.raises(Exception, match="settlement is unconfirmed"):
        feed.make_order("IF2506", 1, 3500, "buy-limit", time_in_force="GFD")
    with pytest.raises(Exception, match="settlement is unconfirmed"):
        feed.cancel_order("IF2506", order_id="SYS")
    assert calls == []
    assert client.get_request_counts().get("order_insert", 0) == 0
    assert client.get_request_counts().get("order_action", 0) == 0


def test_complete_empty_account_query_fails_account_snapshot() -> None:
    class Trader:
        is_read_only_ready = True

        @staticmethod
        def query_account_result(timeout=5):
            return _result("account", ())

    feed = CtpRequestDataFuture(
        queue.Queue(),
        broker_id="9999",
        user_id="account",
        td_front="tcp://test-td",
        md_front="tcp://test-md",
    )
    feed._trader = Trader()
    response = feed.get_account()
    assert response.get_status() is False
    assert response.get_data() == []
    assert response.get_extra_data()["query_complete"] is True
    assert response.get_extra_data()["account_snapshot_complete"] is False


def test_public_exchange_info_joins_terminal_instrument_margin_and_fee_queries() -> (
    None
):
    class Trader:
        is_read_only_ready = True

        @staticmethod
        def get_session_state():
            return {"connection_generation": 1, "account_fingerprint": "fixture"}

        @staticmethod
        def query_instruments_result(**_kwargs):
            return _result(
                "instruments",
                (
                    {
                        "InstrumentID": "SA601",
                        "ExchangeID": "CZCE",
                        "ProductID": "SA",
                        "PriceTick": 1,
                        "VolumeMultiple": 20,
                        "ExpireDate": "20260115",
                        "IsTrading": 1,
                        "trading_calendar_evidence_complete": False,
                        "prior_day_ranking_evidence_complete": False,
                    },
                ),
            )

        @staticmethod
        def query_instrument_margin_rate_result(*_args, **_kwargs):
            return _result(
                "margin_rate",
                (
                    {
                        "InstrumentID": "SA601",
                        "LongMarginRatioByMoney": 0.12,
                        "ShortMarginRatioByMoney": 0.13,
                    },
                ),
            )

        @staticmethod
        def query_instrument_commission_rate_result(*_args, **_kwargs):
            return _result(
                "commission_rate",
                (
                    {
                        "InstrumentID": "SA601",
                        "OpenRatioByVolume": 3,
                        "CloseRatioByVolume": 3,
                        "CloseTodayRatioByVolume": 6,
                    },
                ),
            )

    feed = CtpRequestDataFuture(td_front="tcp://td", md_front="tcp://md")
    feed._trader = Trader()
    response = feed.get_exchange_info("SA601.CZCE", timeout=0)

    assert response.get_status() is True
    spec = response.get_data()[0]
    assert spec["symbol"] == "SA601.CZCE" and spec["instrument"] == "SA601"
    assert spec["price_tick"] == 1 and spec["contract_value"] == 20
    assert spec["margin_rate"] == 0.12
    assert spec["open_fee_amount"] == 3
    assert spec["close_today_fee_amount"] == 6
    assert spec["metadata_complete"] is True
    assert spec["trading_calendar_evidence_complete"] is False
    assert spec["prior_day_ranking_evidence_complete"] is False


def test_public_exchange_info_fails_when_fee_query_has_no_record() -> None:
    class Trader:
        is_read_only_ready = True
        query_instruments_result = staticmethod(
            lambda **_kwargs: _result(
                "instruments",
                ({"InstrumentID": "SA601", "PriceTick": 1, "VolumeMultiple": 20},),
            )
        )
        query_instrument_margin_rate_result = staticmethod(
            lambda *_args, **_kwargs: _result(
                "margin_rate", ({"InstrumentID": "SA601"},)
            )
        )
        query_instrument_commission_rate_result = staticmethod(
            lambda *_args, **_kwargs: _result("commission_rate", ())
        )

    feed = CtpRequestDataFuture(td_front="tcp://td", md_front="tcp://md")
    feed._trader = Trader()
    response = feed.get_exchange_info("SA601.CZCE", timeout=0)
    assert response.get_status() is False and response.get_data() == []
    assert response.get_extra_data()["metadata_complete"] is False


@pytest.mark.parametrize("empty_component", ["margin", "commission"])
def test_public_exchange_info_rejects_complete_but_empty_rate_evidence(
    empty_component: str,
) -> None:
    class Trader:
        is_read_only_ready = True

        @staticmethod
        def get_session_state():
            return {"connection_generation": 1, "account_fingerprint": "fixture"}

        @staticmethod
        def query_instruments_result(**_kwargs):
            return _result(
                "instruments",
                ({"InstrumentID": "SA601", "PriceTick": 1, "VolumeMultiple": 20},),
            )

        @staticmethod
        def query_instrument_margin_rate_result(*_args, **_kwargs):
            record = {"InstrumentID": "SA601"}
            if empty_component != "margin":
                record.update(LongMarginRatioByMoney=0.12, ShortMarginRatioByMoney=0.13)
            return _result("margin_rate", (record,))

        @staticmethod
        def query_instrument_commission_rate_result(*_args, **_kwargs):
            record = {"InstrumentID": "SA601"}
            if empty_component != "commission":
                record.update(
                    OpenRatioByVolume=3,
                    CloseRatioByVolume=3,
                    CloseTodayRatioByVolume=6,
                )
            return _result("commission_rate", (record,))

    feed = CtpRequestDataFuture(td_front="tcp://td", md_front="tcp://md")
    feed._trader = Trader()
    response = feed.get_exchange_info("SA601.CZCE", timeout=0)

    assert response.get_status() is False and response.get_data() == []
    extra = response.get_extra_data()
    assert extra["metadata_complete"] is False
    assert extra["evidence_complete"] is False
    assert (
        f"missing_{'long_margin' if empty_component == 'margin' else 'open_commission'}"
        in extra["metadata_error"]
    )


@pytest.mark.parametrize(
    ("generation", "fingerprint", "session_state", "expected_error"),
    [
        (
            2,
            "fixture",
            {"connection_generation": 1, "account_fingerprint": "fixture"},
            "mixed_query_connection_generation",
        ),
        (
            1,
            "other",
            {"connection_generation": 1, "account_fingerprint": "fixture"},
            "mixed_query_account",
        ),
        (
            1,
            "fixture",
            {"account_fingerprint": "fixture"},
            "missing_current_session_generation",
        ),
        (
            1,
            "fixture",
            {"connection_generation": 1},
            "missing_current_session_account_fingerprint",
        ),
    ],
)
def test_public_exchange_info_rejects_mixed_query_identity(
    generation: int,
    fingerprint: str,
    session_state: dict[str, object],
    expected_error: str,
) -> None:
    class Trader:
        is_read_only_ready = True

        @staticmethod
        def get_session_state():
            return session_state

        @staticmethod
        def query_instruments_result(**_kwargs):
            return _result(
                "instruments",
                ({"InstrumentID": "SA601", "PriceTick": 1, "VolumeMultiple": 20},),
            )

        @staticmethod
        def query_instrument_margin_rate_result(*_args, **_kwargs):
            return _result(
                "margin_rate",
                (
                    {
                        "InstrumentID": "SA601",
                        "LongMarginRatioByMoney": 0.12,
                        "ShortMarginRatioByMoney": 0.13,
                    },
                ),
            )

        @staticmethod
        def query_instrument_commission_rate_result(*_args, **_kwargs):
            return _result(
                "commission_rate",
                (
                    {
                        "InstrumentID": "SA601",
                        "OpenRatioByVolume": 3,
                        "CloseRatioByVolume": 3,
                        "CloseTodayRatioByVolume": 6,
                    },
                ),
                generation=generation,
                account_fingerprint=fingerprint,
            )

    feed = CtpRequestDataFuture(td_front="tcp://td", md_front="tcp://md")
    feed._trader = Trader()
    response = feed.get_exchange_info("SA601.CZCE", timeout=0)

    assert response.get_status() is False and response.get_data() == []
    assert expected_error in response.get_extra_data()["metadata_error"]


def test_auth_failure_does_not_submit_login() -> None:
    client = TraderClient("tcp://test", "9999", "account", "secret")
    submitted = []
    client._api = SimpleNamespace(ReqUserLogin=lambda *_args: submitted.append("login"))
    client._authentication_state = "authenticating"
    client._connection_generation = 1
    client._authentication_request_id = 1
    client._authentication_connection_generation = 1
    info = SimpleNamespace(ErrorID=7, ErrorMsg="bad auth")

    _TraderSpi(client).OnRspAuthenticate(None, info, 1, True)

    assert client.get_session_state()["auth_state"] == "failed"
    assert client.get_session_state()["login_state"] == "disconnected"
    assert submitted == []


def test_login_abi_rejection_records_its_stable_error_code(monkeypatch) -> None:
    client = TraderClient("tcp://test", "9999", "account", "secret")
    client._api = object()
    client._authentication_state = "authenticating"
    client._connection_generation = 1
    client._authentication_request_id = 1
    client._authentication_connection_generation = 1

    def reject_login(*_args):
        raise client_module.CtpNativeAbiError("ctp_trader_login_abi_unverified")

    monkeypatch.setattr(client_module, "_submit_trader_user_login", reject_login)

    _TraderSpi(client).OnRspAuthenticate(None, None, 1, True)

    state = client.get_session_state()
    assert state["login_state"] == "failed"
    assert state["last_error"] == {
        "error": "login_submit_failed",
        "detail": "ctp_trader_login_abi_unverified",
    }


def test_auth_and_login_responses_are_fenced_across_same_spi_reconnect() -> None:
    client = TraderClient(
        "tcp://test",
        "9999",
        "account",
        "secret",
        auto_settlement_confirm=False,
    )
    auth_requests = []
    login_requests = []

    class Api:
        def ReqAuthenticate(self, _field, request_id):
            auth_requests.append(request_id)
            return 0

        def ReqUserLogin(self, _field, request_id):
            login_requests.append(request_id)
            return 0

    api = Api()
    client._api = api
    spi = _TraderSpi(client, api)
    client._spi = spi

    spi.OnFrontConnected()
    first_auth_request = auth_requests[-1]
    spi.OnFrontDisconnected(1)
    spi.OnFrontConnected()
    second_auth_request = auth_requests[-1]

    spi.OnRspAuthenticate(None, None, first_auth_request, True)
    assert client.get_session_state()["auth_state"] == "authenticating"
    assert login_requests == []

    spi.OnRspAuthenticate(None, None, second_auth_request, True)
    old_login_request = login_requests[-1]
    spi.OnFrontDisconnected(2)
    spi.OnFrontConnected()
    third_auth_request = auth_requests[-1]

    spi.OnRspUserLogin(
        SimpleNamespace(
            FrontID=91,
            SessionID=92,
            TradingDay="20260908",
            MaxOrderRef="93",
        ),
        None,
        old_login_request,
        True,
    )
    stale_state = client.get_session_state()
    assert stale_state["login_state"] == "not_started"
    assert stale_state["front_id"] == 0
    assert stale_state["session_id"] == 0
    assert stale_state["trading_day"] == ""

    spi.OnRspAuthenticate(None, None, third_auth_request, True)
    current_login_request = login_requests[-1]
    spi.OnRspUserLogin(
        SimpleNamespace(
            FrontID=1,
            SessionID=2,
            TradingDay="20260909",
            MaxOrderRef="3",
        ),
        None,
        current_login_request,
        True,
    )
    state = client.get_session_state()
    assert state["auth_state"] == "authenticated"
    assert state["login_state"] == "logged_in"
    assert state["front_id"] == 1
    assert state["session_id"] == 2
    assert state["trading_day"] == "20260909"
    assert state["authentication_late_callback_count"] == 1
    assert state["login_late_callback_count"] == 1


@pytest.mark.parametrize("event_type", ["order", "trade", "error"])
def test_spi_user_callbacks_do_not_hold_query_state_lock(event_type: str) -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Api:
        def ReqQryOrder(self, _field, request_id):
            client._handle_query_callback("orders", None, None, request_id, True)
            return 0

    client._api = Api()
    callback_entered = threading.Event()
    callback_finished = threading.Event()
    state_read_finished = threading.Event()

    def query_from_callback(_field) -> None:
        callback_entered.set()
        result = client.query_orders_result(timeout=0)
        assert result.complete is True
        callback_finished.set()

    if event_type == "order":
        client.on_order = query_from_callback
        dispatch = lambda: _TraderSpi(client).OnRtnOrder(SimpleNamespace(OrderRef="1"))
    elif event_type == "trade":
        client.on_trade = query_from_callback
        dispatch = lambda: _TraderSpi(client).OnRtnTrade(SimpleNamespace(TradeID="1"))
    else:
        client.on_error = query_from_callback
        dispatch = lambda: _TraderSpi(client).OnRspError(
            SimpleNamespace(ErrorID=1, ErrorMsg="fixture"),
            999,
            True,
        )

    client._query_lock.acquire()
    callback_thread = threading.Thread(target=dispatch, daemon=True)
    callback_thread.start()
    assert callback_entered.wait(1.0)

    state_thread = threading.Thread(
        target=lambda: (client.get_session_state(), state_read_finished.set()),
        daemon=True,
    )
    state_thread.start()
    try:
        assert state_read_finished.wait(1.0), "user callback retained query-state lock"
    finally:
        client._query_lock.release()

    callback_thread.join(2.0)
    state_thread.join(2.0)
    assert callback_finished.is_set()
    assert not callback_thread.is_alive()
    assert not state_thread.is_alive()


def test_read_only_login_issues_zero_implicit_settlement_writes() -> None:
    client = TraderClient(
        "tcp://test", "9999", "account", "secret", auto_settlement_confirm=False
    )
    client._connected = True
    client._authentication_state = "authenticated"
    client._login_state = "logging_in"
    client._connection_generation = 1
    client._login_request_id = 1
    client._login_connection_generation = 1
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda *_args: pytest.fail(
            "read-only login must not confirm settlement"
        )
    )
    _TraderSpi(client).OnRspUserLogin(
        SimpleNamespace(FrontID=1, SessionID=2, TradingDay="20260909", MaxOrderRef="7"),
        None,
        1,
        True,
    )
    state = client.get_session_state()
    assert state["read_only_ready"] is True
    assert state["trading_ready"] is False
    assert state["account_fingerprint"] == client._account_fingerprint
    assert state["account_fingerprint"]
    assert state["settlement_state"] == "not_requested"
    assert state["request_counts"].get("settlement_confirm", 0) == 0


def test_implicit_settlement_submit_exception_never_marks_session_ready() -> None:
    client = TraderClient("tcp://test", "9999", "account", "secret")
    client._connected = True
    client._authentication_state = "authenticated"
    client._login_state = "logging_in"
    client._connection_generation = 1
    client._login_request_id = 1
    client._login_connection_generation = 1
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
    )
    _TraderSpi(client).OnRspUserLogin(
        SimpleNamespace(FrontID=1, SessionID=2, TradingDay="20260909", MaxOrderRef="7"),
        None,
        1,
        True,
    )
    state = client.get_session_state()
    assert state["settlement_state"] == "failed"
    assert state["trading_ready"] is False
    assert state["request_counts"]["settlement_confirm"] == 1


def test_old_settlement_response_cannot_confirm_new_generation() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))
    request_ids = []
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda _field, request_id: request_ids.append(
            request_id
        )
        or 0
    )
    assert client._request_settlement_confirmation() is True
    old_request = request_ids[-1]

    client._on_front_disconnected(1)
    client._on_front_connected()
    client._authentication_state = "authenticated"
    client._login_state = "logged_in"
    client._trading_day = "20260910"
    assert client._request_settlement_confirmation() is True
    new_request = request_ids[-1]

    spi = _TraderSpi(client)
    field = SimpleNamespace(BrokerID="9999", InvestorID="account")
    spi.OnRspSettlementInfoConfirm(field, None, old_request, True)
    assert client.get_session_state()["settlement_state"] == "confirming"
    assert client.get_session_state()["trading_ready"] is False
    spi.OnRspSettlementInfoConfirm(field, None, new_request, True)
    state = client.get_session_state()
    assert state["settlement_state"] == "confirmed"
    assert state["settlement_proof_source"] == "direct_confirmation"
    assert state["settlement_readback_verified"] is False
    assert state["trading_ready"] is False
    assert client.get_session_state()["settlement_late_callback_count"] == 1


def test_settlement_response_for_wrong_trading_day_is_rejected() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))
    request_ids = []
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda _field, request_id: request_ids.append(
            request_id
        )
        or 0
    )
    assert client._request_settlement_confirmation() is True
    _TraderSpi(client).OnRspSettlementInfoConfirm(
        SimpleNamespace(BrokerID="9999", InvestorID="account", ConfirmDate="20260908"),
        None,
        request_ids[-1],
        True,
    )
    assert client.get_session_state()["trading_ready"] is False
    assert client.get_session_state()["settlement_late_callback_count"] == 1


def test_server_confirmation_query_promotes_only_matching_account_and_day() -> None:
    client = _read_ready(
        TraderClient(
            "tcp://test", "9999", "account", "secret", auto_settlement_confirm=False
        )
    )

    class Api:
        def ReqQrySettlementInfoConfirm(self, _field, request_id):
            record = SimpleNamespace(
                BrokerID="9999", InvestorID="account", ConfirmDate="20260909"
            )
            client._handle_query_callback(
                "settlement_confirmation", record, None, request_id, True
            )
            return 0

    client._api = Api()
    result = client.verify_settlement_confirmation(timeout=0.01)
    state = client.get_session_state()
    assert result.complete is True
    assert state["trading_ready"] is True
    assert state["settlement_proof_source"] == "confirmation_query"
    assert state["settlement_proof_query_request_id"] == result.request_id
    assert state["settlement_readback_verified"] is True


@pytest.mark.parametrize(
    ("records", "complete", "generation"),
    [
        ((), False, 1),
        ((), True, 1),
        (
            ({"BrokerID": "9999", "InvestorID": "other", "ConfirmDate": "20260909"},),
            True,
            1,
        ),
        (
            ({"BrokerID": "9999", "InvestorID": "account", "ConfirmDate": "20260908"},),
            True,
            1,
        ),
        (
            ({"BrokerID": "9999", "InvestorID": "account", "ConfirmDate": "20260909"},),
            True,
            0,
        ),
    ],
    ids=["incomplete", "empty", "wrong-account", "wrong-day", "stale-generation"],
)
def test_bad_settlement_readback_demotes_readiness_and_revokes_native_gate(
    monkeypatch: pytest.MonkeyPatch,
    records,
    complete: bool,
    generation: int,
) -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = object()
    feed.configure_execution_gate(capability)
    feed.arm_execution_gate(capability, _ctp_execution_proof(client, feed))
    result = _result(
        "settlement_confirmation",
        records,
        complete=complete,
        request_id=19,
        generation=generation,
        account_fingerprint=client._account_fingerprint,
    )
    monkeypatch.setattr(
        client,
        "query_settlement_confirmation_result",
        lambda timeout=5: result,
    )

    assert client.verify_settlement_confirmation(timeout=0) is result
    state = client.get_session_state()
    assert state["settlement_readback_verified"] is False
    assert state["trading_ready"] is False
    assert state["execution_gate_armed"] is False
    assert state["execution_gate_revocation_reason"].startswith(
        "ctp_execution_gate_settlement_readback_"
    )
    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_unarmed",
    ):
        feed.make_order(
            "SA2609",
            1,
            1200,
            "buy-limit",
            exchange_id="CZCE",
            _execution_capability=capability,
        )
    assert native_calls == []


@pytest.mark.parametrize(
    ("broker_id", "investor_id"),
    [("", "account"), ("9999", ""), ("other", "account"), ("9999", "other")],
)
def test_server_confirmation_query_requires_exact_account_identity(
    broker_id: str, investor_id: str
) -> None:
    client = _read_ready(
        TraderClient(
            "tcp://test", "9999", "account", "secret", auto_settlement_confirm=False
        )
    )

    class Api:
        def ReqQrySettlementInfoConfirm(self, _field, request_id):
            client._handle_query_callback(
                "settlement_confirmation",
                SimpleNamespace(
                    BrokerID=broker_id,
                    InvestorID=investor_id,
                    ConfirmDate="20260909",
                ),
                None,
                request_id,
                True,
            )
            return 0

    client._api = Api()
    result = client.verify_settlement_confirmation(timeout=0.01)
    assert result.complete is True
    assert client.get_session_state()["trading_ready"] is False


def test_trade_time_filter_reports_unsupported_struct_instead_of_silently_dropping(
    monkeypatch,
) -> None:
    class TradeField:
        def __setattr__(self, name, value):
            if name in {"TradeTimeStart", "TradeTimeEnd"}:
                raise AttributeError(name)
            object.__setattr__(self, name, value)

    monkeypatch.setattr(client_module, "CThostFtdcQryTradeField", TradeField)
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))
    client._api = SimpleNamespace(ReqQryTrade=lambda *_args: 0)
    result = client.query_trades_result(start_time="09:00:00", timeout=0)
    assert result.complete is False and result.unsupported is True
    assert result.error_message == "native_trade_filter_unsupported:TradeTimeStart"


def _tick(
    total: int,
    seq: int,
    *,
    generation: int = 1,
    action_day: str = "20260909",
    **overrides,
) -> CtpTickerData:
    payload = {
        "InstrumentID": "IF2506",
        "ExchangeID": "CFFEX",
        "LastPrice": 4000,
        "BidPrice1": 3999,
        "AskPrice1": 4001,
        "BidVolume1": 2,
        "AskVolume1": 3,
        "OpenInterest": 1000,
        "Volume": total,
        "TradingDay": "20260909",
        "ActionDay": action_day,
        "UpdateTime": f"09:30:0{seq}",
        "UpdateMillisec": 0,
    }
    payload.update(overrides)
    return CtpTickerData(
        payload,
        connection_generation=generation,
        ingest_seq=seq,
    )


def test_volume_delta_continues_across_action_day_midnight_within_trading_day() -> None:
    tracker = CtpVolumeDeltaTracker()
    first = tracker.apply(_tick(100, 1, action_day="20260909"))
    second = tracker.apply(
        _tick(
            105,
            2,
            action_day="20260910",
            UpdateTime="00:00:01",
        )
    )

    assert first.delta_volume == 0 and first.volume_complete is False
    assert second.delta_volume == 5 and second.volume_complete is True
    assert second.volume_quality == "CONTINUOUS"


def test_quote_v2_is_single_authoritative_cumulative_to_delta_conversion() -> None:
    adapter = object.__new__(CtpGatewayAdapter)
    adapter.last_price = {}
    adapter.last_volume = {}
    adapter._volume_state = {}
    adapter.aliases = {"IF2506": {"IF2506.CFFEX"}}
    emitted = []
    adapter.emit = lambda channel, item: emitted.append((channel, item))

    adapter._tick(_tick(100, 1))
    adapter._tick(_tick(107, 2))
    adapter._tick(_tick(110, 3, generation=2))

    ticks = [item for _channel, item in emitted]
    assert [tick.volume for tick in ticks] == [0, 7, 0]
    assert [tick.cum_volume for tick in ticks] == [100, 107, 110]
    assert [tick.volume_complete for tick in ticks] == [False, True, False]
    assert all(tick.schema_version == "ctp.quote.v2" for tick in ticks)
    assert all(tick.volume_semantics == "delta" for tick in ticks)
    assert all(tick.action_day == "20260909" for tick in ticks)


def test_market_stream_converts_volume_once_before_gateway_consumption() -> None:
    stream = CtpMarketStream(topics=[])
    stream._md_client = SimpleNamespace(connection_generation=11)
    rows = []
    stream.push_data = rows.append

    for total, second in ((100, 1), (107, 2)):
        stream._on_tick(
            SimpleNamespace(
                InstrumentID="IF2506",
                ExchangeID="CFFEX",
                LastPrice=4000,
                BidPrice1=3999,
                AskPrice1=4001,
                BidVolume1=2,
                AskVolume1=3,
                OpenInterest=1000,
                Volume=total,
                TradingDay="20260909",
                ActionDay="20260909",
                UpdateTime=f"09:30:0{second}",
                UpdateMillisec=0,
            )
        )

    assert [row.volume_semantics for row in rows] == ["delta", "delta"]
    assert [row.cum_volume for row in rows] == [100, 107]
    assert [row.delta_volume for row in rows] == [0, 7]
    assert [row.volume_complete for row in rows] == [False, True]

    adapter = object.__new__(CtpGatewayAdapter)
    adapter.last_price = {}
    adapter.last_volume = {}
    adapter.aliases = {"IF2506": {"IF2506.CFFEX"}}
    emitted = []
    adapter.emit = lambda channel, item: emitted.append((channel, item))
    for row in rows:
        adapter._tick(row)
    assert [item.volume for _channel, item in emitted] == [0, 7]


def test_invalid_market_snapshot_does_not_advance_volume_baseline() -> None:
    stream = CtpMarketStream(topics=[])
    stream._md_client = SimpleNamespace(connection_generation=11)
    rows = []
    stream.push_data = rows.append

    def push(total: int, second: int, **overrides) -> None:
        fields = {
            "InstrumentID": "IF2506",
            "ExchangeID": "CFFEX",
            "LastPrice": 4000,
            "BidPrice1": 3999,
            "AskPrice1": 4001,
            "BidVolume1": 2,
            "AskVolume1": 3,
            "OpenInterest": 1000,
            "Volume": total,
            "TradingDay": "20260909",
            "ActionDay": "20260909",
            "UpdateTime": f"09:30:0{second}",
            "UpdateMillisec": 0,
        }
        fields.update(overrides)
        stream._on_tick(SimpleNamespace(**fields))

    push(100, 1)
    push(105, 2, LastPrice=float("nan"))
    push(110, 3)

    assert [row.delta_volume for row in rows] == [0, 0, 10]
    assert [row.volume_complete for row in rows] == [False, False, True]
    assert rows[1].volume_quality == "INVALID_QUOTE"
    assert "INVALID_LAST_PRICE" in rows[1].quality_flags


@pytest.mark.parametrize(
    ("overrides", "quality_flag"),
    [
        ({"LastPrice": float("nan")}, "INVALID_LAST_PRICE"),
        ({"AskVolume1": float("nan")}, "INVALID_ASK_VOLUME"),
        ({"BidPrice1": 4002, "AskPrice1": 4001}, "CROSSED_BOOK"),
        ({"BidVolume1": -1}, "INVALID_BID_VOLUME"),
    ],
)
def test_invalid_quote_is_published_as_ineligible_and_never_cached_or_traded(
    overrides: dict[str, float], quality_flag: str
) -> None:
    adapter = object.__new__(CtpGatewayAdapter)
    adapter.last_price = {}
    adapter.last_volume = {}
    adapter.aliases = {"IF2506": {"IF2506.CFFEX"}}
    emitted = []
    adapter.emit = lambda channel, item: emitted.append((channel, item))
    row = _tick(100, 1, **overrides)

    adapter._tick(row)

    assert len(emitted) == 1
    assert emitted[0][1].execution_eligible is False
    assert quality_flag in emitted[0][1].quality_flags
    assert adapter.last_price == {} and adapter.last_volume == {}
    assert quality_flag in row.quality_flags
    adapter.feed = SimpleNamespace(
        make_order=lambda *_args, **_kwargs: pytest.fail(
            "quality-rejected quote must block native order submission"
        )
    )
    with pytest.raises(RuntimeError, match="latest quote failed quality checks"):
        adapter.place_order(
            {
                "symbol": "IF2506.CFFEX",
                "side": "buy",
                "size": 1,
                "price": 4000,
                "order_type": "limit",
            }
        )


@pytest.mark.parametrize(
    ("overrides", "quality_flag"),
    [
        ({"BidVolume1": 0}, "ZERO_DEPTH"),
        ({"BidPrice1": 4001, "AskPrice1": 4001}, "LOCKED_BOOK"),
    ],
)
def test_zero_depth_and_locked_books_are_recorded_but_not_execution_eligible(
    overrides: dict[str, float], quality_flag: str
) -> None:
    adapter = object.__new__(CtpGatewayAdapter)
    adapter.last_price = {}
    adapter.last_volume = {}
    adapter.aliases = {"IF2506": {"IF2506.CFFEX"}}
    emitted = []
    adapter.emit = lambda channel, item: emitted.append((channel, item))

    adapter._tick(_tick(100, 1, **overrides))

    assert len(emitted) == 1
    tick = emitted[0][1]
    assert tick.execution_eligible is False
    assert quality_flag in tick.quality_flags
    assert adapter.last_price["IF2506"] == 4000


def test_trade_stream_reuses_request_feed_trader_without_second_td_client(
    monkeypatch,
) -> None:
    stopped = []
    trader = SimpleNamespace(
        is_read_only_ready=True,
        on_order=None,
        on_trade=None,
        on_login=None,
        on_error=None,
        stop=lambda: stopped.append(True),
    )

    class RequestFeed:
        trader_client = trader

        def __init__(self):
            self.connect_calls = 0

        def connect(self):
            self.connect_calls += 1

    request_feed = RequestFeed()
    monkeypatch.setattr(
        client_module,
        "TraderClient",
        lambda *_args, **_kwargs: pytest.fail("must reuse the request-feed TD client"),
    )
    stream = CtpTradeStream(request_feed=request_feed)
    stream.connect()

    assert request_feed.connect_calls == 1
    assert stream.trader_client is trader
    assert stream.state.value == "authenticated"
    assert callable(trader.on_order) and callable(trader.on_trade)
    stream.disconnect()
    assert stopped == []
    assert trader.on_order is None and trader.on_trade is None


def test_request_feed_reuses_disconnected_td_client_instead_of_replacing_it(
    monkeypatch,
) -> None:
    waited = []
    stopped = []
    trader = SimpleNamespace(
        is_read_only_ready=False,
        wait_ready=lambda timeout: waited.append(timeout) or True,
        stop=lambda: stopped.append(True),
    )
    monkeypatch.setattr(
        client_module,
        "TraderClient",
        lambda *_args, **_kwargs: pytest.fail(
            "existing TD client must not be replaced"
        ),
    )
    feed = CtpRequestDataFuture(td_front="tcp://td", md_front="tcp://md")
    feed._trader = trader

    feed.connect()

    assert feed.trader_client is trader
    assert feed._connected is True
    assert waited == [feed._connect_timeout]
    assert stopped == []


def test_complete_trade_query_cannot_claim_complete_after_public_truncation() -> None:
    records = tuple(
        {
            "InstrumentID": "IF2506",
            "ExchangeID": "CFFEX",
            "TradeID": f"T{index:03d}",
            "Price": 4000,
            "Volume": 1,
        }
        for index in range(101)
    )

    class Trader:
        is_read_only_ready = True

        @staticmethod
        def query_trades_result(**_kwargs):
            return _result("trades", records)

    feed = CtpRequestDataFuture(td_front="tcp://td", md_front="tcp://md")
    feed._trader = Trader()
    response = feed.get_deals(count=100)

    assert response.get_status() is False and response.get_data() == []
    extra = response.get_extra_data()
    assert extra["query_complete"] is True
    assert extra["trade_snapshot_complete"] is False
    assert extra["records_truncated"] is True
    assert extra["trade_record_count"] == 101
    assert extra["returned_trade_record_count"] == 100
    assert extra["evidence_complete"] is False


def test_native_diagnostics_expose_only_matching_extension_hashes() -> None:
    diagnostics = get_ctp_native_diagnostics()
    paths = diagnostics["matching_extension_paths"]
    hashes = diagnostics["matching_extension_sha256"]
    assert diagnostics["package_dir"]
    assert diagnostics["native_loaded"] in {True, False}
    assert set(hashes) == set(paths)
    assert all(len(value) == 64 for value in hashes.values())
    assert diagnostics["runtime_source"] == "vendored_bt_api_py"
    if diagnostics["native_loaded"]:
        loaded_path = Path(diagnostics["loaded_module_path"])
        assert loaded_path.is_file()
        assert (
            diagnostics["loaded_module_sha256"]
            == hashlib.sha256(loaded_path.read_bytes()).hexdigest()
        )
        assert (
            diagnostics["native_module_sha256"][str(loaded_path)]
            == diagnostics["loaded_module_sha256"]
        )
    else:
        assert diagnostics["loaded_module_path"] == ""
        assert diagnostics["loaded_module_sha256"] == ""


def test_ctp_package_manifest_is_sorted_and_deterministic() -> None:
    import bt_api_ctp

    first = client_module.get_ctp_native_diagnostics()
    second = client_module.get_ctp_native_diagnostics()
    manifest = first["ctp_package_manifest"]
    package_root = Path(bt_api_ctp.__file__).resolve().parent
    expected_paths = sorted(
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.relative_to(package_root).parts
    )

    assert manifest == second["ctp_package_manifest"]
    assert first["ctp_package_sha256"] == second["ctp_package_sha256"]
    assert [entry["path"] for entry in manifest] == expected_paths
    assert all(set(entry) == {"path", "sha256"} for entry in manifest)
    assert all(
        entry["sha256"]
        == hashlib.sha256((package_root / entry["path"]).read_bytes()).hexdigest()
        for entry in manifest
    )
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert first["ctp_package_sha256"] == hashlib.sha256(canonical).hexdigest()


def test_ctp_package_identity_excludes_caches_and_detects_source_drift(
    tmp_path,
) -> None:
    package_root = tmp_path / "bt_api_ctp"
    subpackage = package_root / "feeds"
    cache = subpackage / "__pycache__"
    cache.mkdir(parents=True)
    (package_root / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
    source = subpackage / "live.py"
    source.write_text("VALUE = 'first'\n", encoding="utf-8")
    (cache / "ignored.py").write_text("IGNORE = True\n", encoding="utf-8")
    (subpackage / "ignored.pyc").write_bytes(b"compiled")

    first_manifest, first_sha256 = client_module._ctp_python_package_identity(
        package_root
    )
    repeated_manifest, repeated_sha256 = client_module._ctp_python_package_identity(
        package_root
    )
    source.write_text("VALUE = 'second'\n", encoding="utf-8")
    changed_manifest, changed_sha256 = client_module._ctp_python_package_identity(
        package_root
    )

    assert first_manifest == repeated_manifest
    assert first_sha256 == repeated_sha256
    assert [entry["path"] for entry in first_manifest] == [
        "__init__.py",
        "feeds/live.py",
    ]
    assert changed_manifest[0] == first_manifest[0]
    assert changed_manifest[1]["sha256"] != first_manifest[1]["sha256"]
    assert changed_sha256 != first_sha256


def test_native_check_fails_closed_when_vendored_runtime_has_only_python_modules(
    monkeypatch,
) -> None:
    monkeypatch.setattr(client_module, "_CTP_RUNTIME_SOURCE", "vendored_bt_api_py")
    monkeypatch.setattr(
        client_module,
        "_selected_runtime_modules",
        lambda: {"bt_api_ctp.ctp": "/tmp/bt_api_ctp/ctp/__init__.py"},
    )
    monkeypatch.setattr(client_module, "_is_vendored_ctp_native_loaded", lambda: False)

    diagnostics = client_module.get_ctp_native_diagnostics()
    assert diagnostics["native_loaded"] is False
    assert diagnostics["native_module_paths"] == {}
    with pytest.raises(ImportError, match="no verified native extension"):
        client_module._check_native_module()


def test_submit_trader_user_login_uses_verified_bundled_shim(monkeypatch) -> None:
    calls = []

    class Api:
        def ReqUserLogin(self, _field, _request_id):
            raise AssertionError("verified Darwin arm64 login must use the shim")

    api = Api()
    field = object()

    def guarded_submit(received_api, received_field, received_request_id):
        calls.append((received_api, received_field, received_request_id))
        return 23

    monkeypatch.setattr(
        client_module,
        "_is_vendored_native_trader_api",
        lambda candidate: candidate is api,
    )
    monkeypatch.setattr(
        client_module._ctp_base,
        "_submit_public_trader_user_login",
        guarded_submit,
    )

    assert client_module._submit_trader_user_login(api, field, 17) == 23
    assert calls == [(api, field, 17)]


def test_submit_trader_user_login_fails_closed_when_shim_is_unverified(
    monkeypatch,
) -> None:
    class Api:
        def __init__(self) -> None:
            self.direct_calls = []

        def ReqUserLogin(self, field, request_id):
            self.direct_calls.append((field, request_id))
            return 0

    api = Api()
    field = object()
    monkeypatch.setattr(
        client_module, "_is_vendored_native_trader_api", lambda _api: True
    )

    def reject_login(*_args):
        raise client_module.CtpNativeAbiError("ctp_trader_login_abi_unverified")

    monkeypatch.setattr(
        client_module._ctp_base,
        "_submit_public_trader_user_login",
        reject_login,
    )

    with pytest.raises(
        client_module.CtpNativeAbiError,
        match="ctp_trader_login_abi_unverified",
    ):
        client_module._submit_trader_user_login(api, field, 19)

    assert api.direct_calls == []


def test_submit_trader_user_login_uses_mock_fallback(monkeypatch) -> None:
    class MockApi:
        def __init__(self) -> None:
            self.calls = []

        def ReqUserLogin(self, field, request_id):
            self.calls.append((field, request_id))
            return 29

    api = MockApi()
    field = object()
    monkeypatch.setattr(
        client_module, "_is_vendored_native_trader_api", lambda _api: False
    )
    monkeypatch.setattr(
        client_module._ctp_base,
        "_submit_public_trader_user_login",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected ABI guard")),
    )

    assert client_module._submit_trader_user_login(api, field, 23) == 29
    assert api.calls == [(field, 23)]


def test_missing_price_tick_never_falls_back_to_one() -> None:
    adapter = object.__new__(CtpGatewayAdapter)
    adapter._price_ticks = {}
    adapter.get_symbol_info = lambda _symbol: {}
    with pytest.raises(RuntimeError, match="positive PriceTick required"):
        adapter._get_price_tick("IF2506")
