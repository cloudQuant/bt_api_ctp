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
from bt_api_ctp.ctp_env_selector import official_simnow_fronts
from bt_api_ctp.feeds.live_ctp_feed import (
    CtpMarketStream,
    CtpQuoteClockSourceScope,
    CtpRequestDataFuture,
    CtpTradeStream,
    CtpVolumeDeltaTracker,
    _get_ctp_managed_quote_v2_receipt,
)
from bt_api_ctp.query import QueryResult


def _core_capability() -> object:
    """Use the deliberately private core/test authority seam."""

    return client_module._issue_ctp_execution_authority_for_test()


def _read_ready(client: TraderClient, *, trading_day: str = "20260909") -> TraderClient:
    # Use the same accepted terminal login callback as a successful SDK
    # session. Directly setting the visible state flags cannot establish the
    # candidate's sealed account/day observation.
    with client._query_state_lock:
        client._connected = True
        client._authentication_state = "authenticated"
        client._login_state = "logging_in"
        client._trading_day = ""
        client._connection_generation = max(client._connection_generation, 1)
        request_id = client._next_request_id()
        generation = client._connection_generation
        client._login_request_id = request_id
        client._login_connection_generation = generation
        native_api = client._api
        spi = _TraderSpi(client, native_api)
        client._spi = spi
    # Offline fixtures install a synthetic native API without calling start().
    # Mirror the immutable front binding that real start() records before
    # RegisterFront so managed-gate tests exercise the same final check.
    client._session_native_api = native_api
    client._session_native_front = client._bound_front
    client._query_interval = 0
    spi.OnRspUserLogin(
        SimpleNamespace(
            BrokerID=client._bound_broker_id,
            UserID=client._bound_user_id,
            TradingDay=trading_day,
            FrontID=7,
            SessionID=19,
            MaxOrderRef="0",
        ),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        request_id,
        True,
    )
    assert client.is_read_only_ready is True
    return client


def _settlement_authorization(
    client: TraderClient,
    capability: object,
    *,
    environment_profile: str = "controlled-test-environment",
) -> object:
    """Issue one controlled offline settlement token explicitly."""

    return client._issue_settlement_authorization_for_test(
        capability,
        environment_profile=environment_profile,
    )


def _settlement_environment_kwargs(
    environment_profile: str = "controlled-test-environment",
) -> dict[str, object]:
    return {
        "_settlement_environment_profile": environment_profile,
        "_settlement_environment_verified": True,
    }


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


def _execution_ready_feed():
    native_calls = []
    td_front = "tcp://offline-neutral-td.invalid:30001"
    md_front = "tcp://offline-neutral-md.invalid:30011"

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
        td_front,
        "9999",
        "account",
        "secret",
        auto_settlement_confirm=False,
    )
    client._api = Api()
    client = _read_ready(client)
    client._session_native_api = client._api
    client._session_native_front = client._bound_front
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
        td_front=td_front,
        md_front=md_front,
        auto_settlement_confirm=False,
    )
    feed._trader = client
    return feed, client, native_calls


@pytest.mark.parametrize("operation", ("make_order", "cancel_order"))
def test_unmanaged_ctp_feed_rejects_order_writes_before_native_submission(
    operation: str,
) -> None:
    feed, client, native_calls = _execution_ready_feed()

    with pytest.raises(client_module.CtpExecutionGateError) as excinfo:
        if operation == "make_order":
            feed.make_order(
                "SA2609",
                1,
                1200,
                "buy-limit",
                exchange_id="CZCE",
                time_in_force="GFD",
            )
        else:
            feed.cancel_order("SA2609", order_id="SYS", exchange_id="CZCE")

    assert excinfo.value.code == "ctp_execution_gate_capability_required"
    assert native_calls == []
    assert client.get_request_counts()["order_insert"] == 0
    assert client.get_request_counts()["order_action"] == 0


@pytest.mark.parametrize(
    ("method_name", "request_count"),
    (
        ("submit_order_insert", "order_insert"),
        ("submit_order_action", "order_action"),
    ),
)
def test_unmanaged_trader_client_rejects_direct_order_write_before_native_submission(
    method_name: str,
    request_count: str,
) -> None:
    _feed, client, native_calls = _execution_ready_feed()
    native_field = SimpleNamespace(InstrumentID="SA2609", ExchangeID="CZCE")

    with pytest.raises(client_module.CtpExecutionGateError) as excinfo:
        getattr(client, method_name)(native_field, 1)

    assert excinfo.value.code == "ctp_execution_gate_capability_required"
    assert native_calls == []
    assert client.get_request_counts()[request_count] == 0


def test_managed_ctp_feed_is_disarmed_before_proof_and_issues_zero_writes() -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = _core_capability()
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
    capability = _core_capability()
    feed.configure_execution_gate(capability)

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
    client._api.ReqQueryBankAccountMoneyByFuture = lambda *_args: raw_calls.append("query") or 0
    public_api = client.api
    cached_qry = public_api.ReqQryOrder
    cached_query = public_api.ReqQueryBankAccountMoneyByFuture

    assert cached_qry(object(), 1) == 0
    assert cached_query(object(), 2) == 0
    assert raw_calls == ["qry", "query"]

    capability = _core_capability()
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


def test_public_trader_api_blocks_cached_lifecycle_and_front_mutation_calls() -> None:
    feed, client, native_calls = _execution_ready_feed()
    lifecycle_calls = []
    client._api.Release = lambda: lifecycle_calls.append("release")
    client._api.RegisterFront = lambda value: lifecycle_calls.append(("front", value))
    client._api.Init = lambda: lifecycle_calls.append("init")
    public_api = client.api
    cached_release = public_api.Release
    cached_front = public_api.RegisterFront
    cached_init = public_api.Init

    for lifecycle_call, args in (
        (cached_release, ()),
        (cached_front, ("tcp://untrusted-front",)),
        (cached_init, ()),
    ):
        with pytest.raises(
            client_module.CtpExecutionGateError,
            match="ctp_execution_gate_native_lifecycle_blocked",
        ):
            lifecycle_call(*args)
    assert lifecycle_calls == []

    capability = _core_capability()
    feed.configure_execution_gate(capability)
    for lifecycle_call, args in (
        (cached_release, ()),
        (cached_front, ("tcp://untrusted-front",)),
        (cached_init, ()),
        (public_api.Release, ()),
        (public_api.RegisterFront, ("tcp://untrusted-front",)),
        (public_api.Init, ()),
        (client.api.Release, ()),
    ):
        with pytest.raises(
            client_module.CtpExecutionGateError,
            match="ctp_execution_gate_native_lifecycle_blocked",
        ):
            lifecycle_call(*args)

    assert lifecycle_calls == []
    assert native_calls == []


def test_cached_public_native_order_request_is_blocked_before_and_after_gate_installation() -> None:
    feed, client, native_calls = _execution_ready_feed()
    raw_api = client._api
    public_api = client.api
    cached_writes = (public_api.ReqOrderInsert, public_api.ReqOrderAction)
    field = SimpleNamespace(InstrumentID="SA2609")

    assert client.api is public_api
    assert all(value is not raw_api for value in vars(public_api).values())
    for write in cached_writes:
        with pytest.raises(
            client_module.CtpExecutionGateError,
            match="ctp_execution_gate_native_write_blocked",
        ):
            write(field, 1)
    assert native_calls == []

    capability = _core_capability()
    feed.configure_execution_gate(capability)
    for write in (
        *cached_writes,
        public_api.ReqOrderInsert,
        public_api.ReqOrderAction,
        client.api.ReqOrderInsert,
        client.api.ReqOrderAction,
    ):
        with pytest.raises(
            client_module.CtpExecutionGateError,
            match="ctp_execution_gate_native_write_blocked",
        ):
            write(field, 2)

    assert client.api is public_api
    assert native_calls == []


def test_api_swap_revokes_proof_and_old_public_handles_remain_blocked() -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = _core_capability()
    feed.configure_execution_gate(capability)
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


def test_official_simnow_legacy_settlement_requires_typed_admission() -> None:
    native_calls = []
    td_front, md_front = official_simnow_fronts("set1_group1")

    class Api:
        def ReqSettlementInfoConfirm(self, _field, _request_id):
            native_calls.append("settlement")
            return 0

    client = TraderClient(td_front, "9999", "account", "secret", auto_settlement_confirm=False)
    client._api = Api()
    _read_ready(client)
    client._session_native_api = client._api
    client._session_native_front = client._bound_front
    feed = CtpRequestDataFuture(
        broker_id="9999",
        user_id="account",
        password="secret",
        td_front=td_front,
        md_front=md_front,
        ctp_env_profile="set1_group1",
        auto_settlement_confirm=False,
    )
    feed._trader = client
    capability = _core_capability()
    feed.configure_execution_gate(capability)

    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_simnow_execution_not_admitted",
    ):
        _settlement_authorization(client, capability, environment_profile=feed.ctp_env_profile)
    assert native_calls == []
    assert client.get_request_counts()["settlement_confirm"] == 0


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
    client._session_native_front = client._bound_front
    client._settlement_state = "not_requested"
    capability = _core_capability()
    client.configure_execution_gate(capability)

    assert (
        client.confirm_settlement(
            timeout=0,
            _execution_capability=capability,
            _settlement_authorization=_settlement_authorization(client, capability),
            **_settlement_environment_kwargs(),
        )
        is False
    )
    assert (
        client.confirm_settlement(
            timeout=0,
            _execution_capability=capability,
            _settlement_authorization=_settlement_authorization(client, capability),
            **_settlement_environment_kwargs(),
        )
        is False
    )
    assert native_calls == ["confirm"]
    assert client.get_request_counts()["settlement_confirm"] == 1


def test_custom_front_settlement_requires_matching_readback_before_ready() -> None:
    native_calls = []
    query_calls = []
    client = TraderClient(
        "tcp://neutral-settlement.invalid:31001",
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

        def ReqQrySettlementInfoConfirm(self, _field, request_id):
            query_calls.append(request_id)
            client._handle_query_callback(
                "settlement_confirmation",
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
    _read_ready(client)
    client._session_native_api = client._api
    client._session_native_front = client._bound_front
    capability = _core_capability()
    client.configure_execution_gate(capability)

    assert client.confirm_settlement(
        timeout=0,
        _execution_capability=capability,
        _settlement_authorization=_settlement_authorization(client, capability),
        **_settlement_environment_kwargs(),
    ) is True

    state = client.get_session_state()
    assert native_calls == [("9999", "account", 2)]
    assert client.get_request_counts()["settlement_confirm"] == 1
    assert state["settlement_state"] == "confirmed"
    assert state["settlement_proof_source"] == "direct_confirmation"
    assert state["settlement_readback_verified"] is False
    assert state["trading_ready"] is False

    readback = client.verify_settlement_confirmation(timeout=0)

    state = client.get_session_state()
    assert readback.complete is True
    assert query_calls == [readback.request_id]
    assert native_calls == [("9999", "account", 2)]
    assert client.get_request_counts()["settlement_confirm"] == 1
    assert state["settlement_state"] == "confirmed"
    assert state["settlement_proof_source"] == "confirmation_query"
    assert state["settlement_readback_verified"] is True
    assert state["trading_ready"] is True


def test_managed_settlement_rejects_auto_confirmation_mode_before_write() -> None:
    native_calls = []
    client = TraderClient(
        "tcp://test",
        "9999",
        "account",
        "secret",
        auto_settlement_confirm=True,
    )
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda *_args: native_calls.append("confirm") or 0
    )
    client = _read_ready(client)
    client._session_native_api = client._api
    client._session_native_front = client._bound_front
    capability = _core_capability()
    client.configure_execution_gate(capability)

    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_auto_settlement_confirm_enabled",
    ):
        client.confirm_settlement(
            timeout=0,
            _execution_capability=capability,
            _settlement_authorization=_settlement_authorization(client, capability),
            **_settlement_environment_kwargs(),
        )
    assert native_calls == []
    assert client.get_request_counts()["settlement_confirm"] == 0


def test_auto_settlement_configuration_never_issues_implicit_terminal_write() -> None:
    client = TraderClient(
        "tcp://test",
        "9999",
        "account",
        "secret",
        auto_settlement_confirm=True,
    )
    native_calls = []
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda *_args: native_calls.append("confirm") or 0
    )
    _read_ready(client)

    assert client.wait_ready(timeout=0.01) is False
    state = client.get_session_state()
    assert state["read_only_ready"] is True
    assert state["trading_ready"] is False
    assert state["settlement_state"] == "not_requested"
    assert state["request_counts"]["settlement_confirm"] == 0
    assert native_calls == []


def test_reentrant_start_revokes_managed_proof_before_rejecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed, client, native_calls = _execution_ready_feed()
    capability = _core_capability()
    feed.configure_execution_gate(capability)
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
    capability = _core_capability()
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
    capability = _core_capability()
    feed.configure_execution_gate(capability)

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
    capability = _core_capability()
    feed.configure_execution_gate(capability)

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


def test_query_result_accumulates_only_matching_request_and_terminal_packet() -> None:
    client = _read_ready(TraderClient("tcp://test", "9999", "account", "secret"))

    class Api:
        def ReqQryOrder(self, _field, request_id):
            client._handle_query_callback("orders", {"OrderSysID": "A"}, None, request_id, False)
            client._handle_query_callback("orders", {"OrderSysID": "B"}, None, request_id, True)
            return 0

    client._api = Api()
    result = client.query_orders_result(timeout=0.01)

    assert result.complete is True
    assert [row["OrderSysID"] for row in result.records] == ["A", "B"]
    assert result.request_id == 2 and result.connection_generation == 1
    assert result.evidence_complete is True
    counts = client.get_request_counts()
    assert counts["query_orders"] == 1
    assert counts["settlement_confirm"] == 0
    assert counts["order_insert"] == 0
    assert counts["order_action"] == 0


def test_instrument_query_exposes_expiry_without_inventing_trading_day_ranking() -> None:
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
    monkeypatch.setattr(client_module, "CThostFtdcQryInstrumentField", FieldWithoutProductID)

    result = client.query_instruments_result(product_id="SA", timeout=0.01)

    assert result.complete is False
    assert result.unsupported is True
    assert result.error_code == -3
    assert result.error_message == "native_instrument_filter_unsupported:ProductID"
    assert client.get_request_counts()["query_instruments"] == 0


def test_instrument_product_filter_is_forwarded_by_typed_and_public_feed_proxies() -> None:
    seen = []

    class Trader:
        is_read_only_ready = True
        auto_settlement_confirm = False

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
        auto_settlement_confirm = False

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
    assert result.complete is False and result.timed_out is True and result.records == ()

    client._handle_query_callback("account", {"Balance": 1}, None, result.request_id, True)
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
            client._handle_query_callback("orders", {"OrderSysID": "late"}, None, request_id, True)
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
        TraderClient("tcp://test", "9999", "account", "secret", auto_settlement_confirm=False)
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

    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_capability_required",
    ):
        feed.make_order("IF2506", 1, 3500, "buy-limit", time_in_force="GFD")
    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_capability_required",
    ):
        feed.cancel_order("IF2506", order_id="SYS")
    assert calls == []
    assert client.get_request_counts().get("order_insert", 0) == 0
    assert client.get_request_counts().get("order_action", 0) == 0


def test_complete_empty_account_query_fails_account_snapshot() -> None:
    class Trader:
        is_read_only_ready = True
        auto_settlement_confirm = False

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


def test_public_exchange_info_joins_terminal_instrument_margin_and_fee_queries() -> None:
    class Trader:
        is_read_only_ready = True
        auto_settlement_confirm = False

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
        auto_settlement_confirm = False
        query_instruments_result = staticmethod(
            lambda **_kwargs: _result(
                "instruments",
                ({"InstrumentID": "SA601", "PriceTick": 1, "VolumeMultiple": 20},),
            )
        )
        query_instrument_margin_rate_result = staticmethod(
            lambda *_args, **_kwargs: _result("margin_rate", ({"InstrumentID": "SA601"},))
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
        auto_settlement_confirm = False

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
        auto_settlement_confirm = False

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
            BrokerID=client._bound_broker_id,
            UserID=client._bound_user_id,
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
            BrokerID=client._bound_broker_id,
            UserID=client._bound_user_id,
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
    client = TraderClient("tcp://test", "9999", "account", "secret")
    assert client.auto_settlement_confirm is False
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda *_args: pytest.fail(
            "read-only login must not confirm settlement"
        )
    )
    _read_ready(client)
    state = client.get_session_state()
    assert state["read_only_ready"] is True
    assert state["trading_ready"] is False
    assert state["account_fingerprint"] == client._account_fingerprint
    assert state["account_fingerprint"]
    assert state["settlement_state"] == "not_requested"
    assert state["request_counts"].get("settlement_confirm", 0) == 0


def test_default_request_feed_constructs_only_an_explicitly_safe_trader() -> None:
    constructed = []
    native_writes = []

    class SafeTrader:
        def __init__(self, *_args, **kwargs) -> None:
            constructed.append(kwargs["auto_settlement_confirm"])
            self.auto_settlement_confirm = kwargs["auto_settlement_confirm"]
            self.is_read_only_ready = True

        def start(self, *, block=False) -> None:
            assert block is False

        def confirm_settlement(self, *_args, **_kwargs) -> None:
            native_writes.append("settlement")

        def submit_order_insert(self, *_args, **_kwargs) -> None:
            native_writes.append("insert")

        def submit_order_action(self, *_args, **_kwargs) -> None:
            native_writes.append("cancel")

    original = client_module.TraderClient
    client_module.TraderClient = SafeTrader
    try:
        feed = CtpRequestDataFuture(
            broker_id="9999",
            user_id="account",
            password="secret",
            td_front="tcp://test-td",
            md_front="tcp://test-md",
        )
        assert feed.auto_settlement_confirm is False
        feed.connect()
    finally:
        client_module.TraderClient = original

    assert constructed == [False]
    assert native_writes == []


def test_read_only_preflight_rejects_auto_settlement_before_client_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed = []
    monkeypatch.setattr(
        "bt_api_ctp.feeds.live_ctp_feed.ctp_client.TraderClient",
        lambda *_args, **_kwargs: constructed.append(True),
    )
    feed = CtpRequestDataFuture(
        broker_id="9999",
        user_id="account",
        password="secret",
        td_front="tcp://test-td",
        md_front="tcp://test-md",
        auto_settlement_confirm=True,
    )

    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_auto_settlement_confirm_enabled",
    ):
        feed.get_account()
    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_auto_settlement_confirm_enabled",
    ):
        feed.configure_execution_gate(_core_capability())

    assert constructed == []


def test_read_only_preflight_rejects_existing_unsafe_trader_without_native_requests() -> None:
    query_calls = []
    unsafe_trader = SimpleNamespace(
        auto_settlement_confirm=True,
        is_read_only_ready=True,
        query_account_result=lambda *_args, **_kwargs: query_calls.append("account"),
    )
    feed = CtpRequestDataFuture(
        broker_id="9999",
        user_id="account",
        password="secret",
        td_front="tcp://test-td",
        md_front="tcp://test-md",
        auto_settlement_confirm=False,
    )
    feed._trader = unsafe_trader
    feed._connected = True

    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_auto_settlement_confirm_enabled",
    ):
        feed.get_account()
    with pytest.raises(
        client_module.CtpExecutionGateError,
        match="ctp_execution_gate_auto_settlement_confirm_enabled",
    ):
        feed.configure_execution_gate(_core_capability())

    assert query_calls == []


def test_login_with_automatic_settlement_enabled_never_submits_settlement_confirmation() -> None:
    client = TraderClient(
        "tcp://test",
        "9999",
        "account",
        "secret",
        auto_settlement_confirm=True,
    )
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda *_args: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    _read_ready(client)
    state = client.get_session_state()
    assert state["settlement_state"] == "not_requested"
    assert state["trading_ready"] is False
    assert state["request_counts"]["settlement_confirm"] == 0


def test_old_settlement_response_cannot_confirm_new_generation() -> None:
    client = _read_ready(
        TraderClient("tcp://test", "9999", "account", "secret", auto_settlement_confirm=False)
    )
    request_ids = []
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda _field, request_id: request_ids.append(request_id) or 0
    )
    client._session_native_api = client._api
    client._session_native_front = client._bound_front
    capability = _core_capability()
    client.configure_execution_gate(capability)
    assert (
        client._request_settlement_confirmation(
            execution_capability=capability,
            settlement_authorization=_settlement_authorization(client, capability),
            **{
                "settlement_environment_profile": "controlled-test-environment",
                "settlement_environment_verified": True,
            },
        )
        is True
    )
    old_request = request_ids[-1]

    client._on_front_disconnected(1)
    client._on_front_connected()
    _read_ready(client, trading_day="20260910")
    assert (
        client._request_settlement_confirmation(
            execution_capability=capability,
            settlement_authorization=_settlement_authorization(client, capability),
            **{
                "settlement_environment_profile": "controlled-test-environment",
                "settlement_environment_verified": True,
            },
        )
        is True
    )
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
    client = _read_ready(
        TraderClient("tcp://test", "9999", "account", "secret", auto_settlement_confirm=False)
    )
    request_ids = []
    client._api = SimpleNamespace(
        ReqSettlementInfoConfirm=lambda _field, request_id: request_ids.append(request_id) or 0
    )
    client._session_native_api = client._api
    client._session_native_front = client._bound_front
    capability = _core_capability()
    client.configure_execution_gate(capability)
    assert (
        client._request_settlement_confirmation(
            execution_capability=capability,
            settlement_authorization=_settlement_authorization(client, capability),
            **{
                "settlement_environment_profile": "controlled-test-environment",
                "settlement_environment_verified": True,
            },
        )
        is True
    )
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
        TraderClient("tcp://test", "9999", "account", "secret", auto_settlement_confirm=False)
    )

    class Api:
        def ReqQrySettlementInfoConfirm(self, _field, request_id):
            record = SimpleNamespace(BrokerID="9999", InvestorID="account", ConfirmDate="20260909")
            client._handle_query_callback("settlement_confirmation", record, None, request_id, True)
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
    capability = _core_capability()
    feed.configure_execution_gate(capability)
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
        TraderClient("tcp://test", "9999", "account", "secret", auto_settlement_confirm=False)
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
        "UpperLimitPrice": 4400,
        "LowerLimitPrice": 3600,
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




















def test_market_stream_keeps_per_subscription_option_and_future_metadata() -> None:
    """Direct/offline rows retain topic values for diagnostics only."""

    common = {
        "rules_hash": "rules-sha256",
        "clock_domain_id": "ctp-md-clock-a",
        "source_clock_quality": "verified",
        "receive_clock_quality": "verified",
        "source_clock_error_ms": 1,
        "receive_clock_error_ms": 1,
        "freshness_verified": True,
    }
    stream = CtpMarketStream(
        topics=[
            {
                "topic": "tick",
                "symbol": "SA701",
                "quote_v2": {
                    **common,
                    "asset_type": "future",
                    "product_class": "1",
                    "contract_type": "future",
                },
            },
            {
                "topic": "tick",
                "symbol": "SA701C1080",
                "quote_v2": {
                    **common,
                    "asset_type": "option",
                    "product_class": "2",
                    "contract_type": "option",
                    "option_type": "call",
                    "underlying_instrument": "SA701",
                    "strike_price": 1080,
                },
            },
        ]
    )
    stream._md_client = SimpleNamespace(connection_generation=11)
    rows = []
    stream.push_data = rows.append

    # This private direct-call seam has no connect-installed callback
    # capability, so its metadata cannot receive a managed quote receipt.
    for instrument, price, lower, upper in (
        ("SA701", 1200, 1000, 2000),
        ("SA701C1080", 60, 1, 100),
    ):
        stream._on_tick(
            SimpleNamespace(
                InstrumentID=instrument,
                ExchangeID="CZCE",
                LastPrice=price,
                BidPrice1=price - 1,
                AskPrice1=price + 1,
                BidVolume1=2,
                AskVolume1=3,
                OpenInterest=1000,
                UpperLimitPrice=upper,
                LowerLimitPrice=lower,
                Volume=100,
                TradingDay="20260909",
                ActionDay="20260909",
                UpdateTime="09:30:01",
                UpdateMillisec=0,
            )
        )

    assert [row.get_asset_type().lower() for row in rows] == ["future", "option"]
    assert [row.subscription_epoch for row in rows] == [1, 1]
    assert all(row.rules_hash == "rules-sha256" for row in rows)
    assert all(row.source_clock_quality == "verified" for row in rows)
    assert all(row.freshness_verified is True for row in rows)
    assert all(_get_ctp_managed_quote_v2_receipt(row, stream) is None for row in rows)
    assert [row.product_class for row in rows] == ["1", "2"]
    assert [row.contract_type for row in rows] == ["future", "option"]
    assert [row.option_type for row in rows] == [None, "call"]
    assert [row.underlying_instrument for row in rows] == [None, "SA701"]
    assert [row.strike_price for row in rows] == [None, 1080.0]


def test_market_stream_public_quote_metadata_cannot_qualify_native_receipt(
    monkeypatch,
) -> None:
    """A real callback gets a receipt, but public inputs cannot qualify it."""

    clients = []

    class FakeMdClient:
        def __init__(self, *_args) -> None:
            self.connection_generation = 1
            self.subscriptions = []
            clients.append(self)

        def subscribe(self, instruments) -> None:
            self.subscriptions.append(list(instruments))

        def start(self, *, block=False) -> None:
            self.started = block

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("bt_api_ctp.feeds.live_ctp_feed.ctp_client.MdClient", FakeMdClient)
    stream = CtpMarketStream(
        quote_v2_metadata={
            "asset_type": "option",
            "rules_hash": "kwargs-rules",
            "clock_domain_id": "kwargs-clock",
            "source": "kwargs-source",
            "source_clock_quality": "verified",
            "receive_clock_quality": "verified",
            "source_clock_error_ms": 1,
            "receive_clock_error_ms": 1,
            "freshness_verified": True,
        },
        topics=[
            {
                "topic": "tick",
                "symbol": "IF2506C4000",
                "quote_v2": {
                    "asset_type": "option",
                    "rules_hash": "topic-rules",
                    "clock_domain_id": "topic-clock",
                    "source": "topic-source",
                    "source_clock_quality": "verified",
                    "receive_clock_quality": "verified",
                    "source_clock_error_ms": 1,
                    "receive_clock_error_ms": 1,
                    "freshness_verified": True,
                },
            }
        ],
    )
    rows = []
    stream.push_data = rows.append
    stream.connect()
    client = clients[-1]
    client.on_login(SimpleNamespace())
    client.on_tick(
        SimpleNamespace(
            InstrumentID="IF2506C4000",
            ExchangeID="CFFEX",
            LastPrice=4000,
            BidPrice1=3999,
            AskPrice1=4001,
            BidVolume1=2,
            AskVolume1=3,
            OpenInterest=1000,
            UpperLimitPrice=5000,
            LowerLimitPrice=3000,
            Volume=100,
            TradingDay="20260909",
            ActionDay="20260909",
            UpdateTime="09:30:01",
            UpdateMillisec=0,
        )
    )

    row = rows[-1]
    # Public rules metadata can remain visible for diagnostics, but clock and
    # quality claims on a managed native callback must come from its producer.
    assert row.rules_hash == "topic-rules"
    scope = stream.get_quote_clock_source_scope()
    assert type(scope) is CtpQuoteClockSourceScope
    assert scope is not None
    assert scope.source == "ctp.native.md"
    assert scope.clock_domain_id.startswith("ctp-md-process:")
    assert scope.clock_domain_id != "kwargs-clock"
    assert scope.clock_domain_id != "topic-clock"
    assert scope.clock_source_session_id
    assert scope.clock_source
    assert scope.connection_generation == 1
    assert row.clock_domain_id == scope.clock_domain_id
    assert row.source == "ctp.native.md"
    assert row.source_clock_quality == "unknown"
    assert row.receive_clock_quality == "unknown"
    assert row.source_clock_error_ms is None
    assert row.receive_clock_error_ms is None
    assert row.freshness_verified is False

    receipt = _get_ctp_managed_quote_v2_receipt(row, stream)
    assert receipt is not None
    assert receipt["symbol"] == "IF2506C4000"
    assert receipt["connection_generation"] == 1
    assert receipt["subscription_epoch"] == 1
    # Producer identity is available, but no mapping/freshness authority is
    # inferred from configuration or from the receive clock itself.
    assert receipt["execution_qualified"] is False
    assert receipt["rules_hash"] == ""
    assert receipt["clock_domain_id"] == scope.clock_domain_id
    assert receipt["clock_source_session_id"] == scope.clock_source_session_id
    assert receipt["clock_source"] == scope.clock_source
    assert receipt["receive_clock_quality"] == "unknown"
    assert receipt["freshness_verified"] is False

    # The opaque receipt cannot be replayed across either lifecycle fence or
    # a distinct stream instance, even when public metadata is identical.
    stream._subscription_epoch += 1
    assert _get_ctp_managed_quote_v2_receipt(row, stream) is None
    stream._subscription_epoch -= 1
    stream._connection_generation += 1
    assert _get_ctp_managed_quote_v2_receipt(row, stream) is None
    other_stream = CtpMarketStream(topics=stream.topics)
    other_stream._connection_generation = 1
    other_stream._subscription_epoch = 1
    assert _get_ctp_managed_quote_v2_receipt(row, other_stream) is None


def test_market_quote_clock_source_scope_is_bound_to_authenticated_session(
    monkeypatch,
) -> None:
    clients = []
    synthetic_clock_values = iter((101_001, 202_002, 303_003))

    def synthetic_monotonic_ns() -> int:
        return next(synthetic_clock_values)

    class FakeMdClient:
        def __init__(self, *_args) -> None:
            self.connection_generation = 1
            clients.append(self)

        def subscribe(self, _instruments) -> None:
            pass

        def start(self, *, block=False) -> None:
            self.started = block

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("bt_api_ctp.feeds.live_ctp_feed.ctp_client.MdClient", FakeMdClient)
    monkeypatch.setattr(
        "bt_api_ctp.feeds.live_ctp_feed._CTP_MONOTONIC_NS",
        synthetic_monotonic_ns,
    )
    stream = CtpMarketStream(topics=[{"topic": "tick", "symbol": "IF2506"}])
    rows = []
    stream.push_data = rows.append

    stream.connect()
    first_client = clients[-1]
    assert stream.get_quote_clock_source_scope() is None
    first_client.on_login(SimpleNamespace())
    first_scope = stream.get_quote_clock_source_scope()
    assert type(first_scope) is CtpQuoteClockSourceScope
    assert first_scope is not None
    assert first_scope.connection_generation == 1

    tick = SimpleNamespace(
        InstrumentID="IF2506",
        ExchangeID="CFFEX",
        LastPrice=4000,
        BidPrice1=3999,
        AskPrice1=4001,
        BidVolume1=2,
        AskVolume1=3,
        OpenInterest=1000,
        UpperLimitPrice=5000,
        LowerLimitPrice=3000,
        Volume=100,
        TradingDay="20260909",
        ActionDay="20260909",
        UpdateTime="09:30:01",
        UpdateMillisec=0,
    )
    first_client.on_tick(tick)
    first_row = rows[-1]
    assert first_row.clock_domain_id == first_scope.clock_domain_id
    assert first_row.recv_monotonic_ns == 101_001
    assert _get_ctp_managed_quote_v2_receipt(first_row, stream) is not None

    # An in-place native generation change also rotates the producer clock
    # session and fences receipts from the prior source generation. Until the
    # new login is observed, the changed native generation is unqualified.
    first_client.on_disconnect(0)
    assert stream.get_quote_clock_source_scope() is None
    assert _get_ctp_managed_quote_v2_receipt(first_row, stream) is None
    first_client.connection_generation = 2
    first_client.on_tick(tick)
    assert stream.get_quote_clock_source_scope() is None
    assert rows[-1].clock_domain_id == ""
    assert rows[-1].recv_monotonic_ns == 202_002
    first_client.on_login(SimpleNamespace())
    second_scope = stream.get_quote_clock_source_scope()
    assert type(second_scope) is CtpQuoteClockSourceScope
    assert second_scope is not None
    assert second_scope is not first_scope
    assert second_scope.connection_generation == 2
    assert second_scope.clock_source_session_id != first_scope.clock_source_session_id
    assert second_scope.clock_domain_id != first_scope.clock_domain_id
    assert _get_ctp_managed_quote_v2_receipt(first_row, stream) is None
    first_client.on_tick(tick)
    assert rows[-1].clock_domain_id == second_scope.clock_domain_id
    assert rows[-1].recv_monotonic_ns == 303_003

    second_row = rows[-1]
    stream.disconnect()
    assert stream.get_quote_clock_source_scope() is None
    before_stale_callback = len(rows)
    first_client.on_tick(tick)
    assert len(rows) == before_stale_callback
    assert _get_ctp_managed_quote_v2_receipt(second_row, stream) is None

    stream.connect()
    replacement_client = clients[-1]
    replacement_client.on_login(SimpleNamespace())
    replacement_scope = stream.get_quote_clock_source_scope()
    assert type(replacement_scope) is CtpQuoteClockSourceScope
    assert replacement_scope is not None
    assert replacement_scope.connection_generation > second_scope.connection_generation
    assert replacement_scope.clock_source_session_id not in {
        first_scope.clock_source_session_id,
        second_scope.clock_source_session_id,
    }
    assert replacement_scope.clock_domain_id not in {
        first_scope.clock_domain_id,
        second_scope.clock_domain_id,
    }


def test_market_stream_rejects_callback_without_managed_capability() -> None:
    """A caller cannot exploit the initial ``None`` callback state as proof."""

    stream = CtpMarketStream(
        topics=[{"topic": "tick", "symbol": "IF2506"}],
    )
    client = SimpleNamespace(connection_generation=1)
    stream._md_client = client
    rows = []
    stream.push_data = rows.append

    # This resembles a callback closely enough to pass the client/token
    # identity check, but it did not receive ``connect``'s opaque capability.
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
            UpperLimitPrice=5000,
            LowerLimitPrice=3000,
            Volume=100,
            TradingDay="20260909",
            ActionDay="20260909",
            UpdateTime="09:30:01",
            UpdateMillisec=0,
        ),
        client=client,
        client_token=0,
    )

    assert len(rows) == 1
    assert _get_ctp_managed_quote_v2_receipt(rows[0], stream) is None


def test_market_stream_reconnect_advances_quote_v2_subscription_epoch(
    monkeypatch,
) -> None:
    clients = []

    class FakeMdClient:
        def __init__(self, *_args) -> None:
            self.connection_generation = 1
            self.subscriptions = []
            clients.append(self)

        def subscribe(self, instruments) -> None:
            self.subscriptions.append(list(instruments))

        def start(self, *, block=False) -> None:
            self.started = block

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("bt_api_ctp.feeds.live_ctp_feed.ctp_client.MdClient", FakeMdClient)
    stream = CtpMarketStream(
        topics=[
            {
                "topic": "tick",
                "symbol": "SA701C1080",
                "quote_v2": {"asset_type": "option"},
            }
        ]
    )
    rows = []
    stream.push_data = rows.append

    stream.connect()
    stream.disconnect()
    stream.connect()
    stream._on_tick(
        SimpleNamespace(
            InstrumentID="SA701C1080",
            ExchangeID="CZCE",
            LastPrice=60,
            BidPrice1=59,
            AskPrice1=61,
            BidVolume1=2,
            AskVolume1=3,
            OpenInterest=1000,
            UpperLimitPrice=100,
            LowerLimitPrice=1,
            Volume=100,
            TradingDay="20260909",
            ActionDay="20260909",
            UpdateTime="09:30:01",
            UpdateMillisec=0,
        )
    )

    assert [client.subscriptions for client in clients] == [
        [["SA701C1080"]],
        [["SA701C1080"]],
    ]
    assert rows[-1].subscription_epoch == 2


def test_market_stream_reconnect_fences_delayed_old_client_quotes(monkeypatch) -> None:
    clients = []

    class FakeMdClient:
        def __init__(self, *_args) -> None:
            # Each new native client starts at the same native generation.
            self.connection_generation = 1
            self.subscriptions = []
            clients.append(self)

        def subscribe(self, instruments) -> None:
            self.subscriptions.append(list(instruments))

        def start(self, *, block=False) -> None:
            self.started = block

        def stop(self) -> None:
            self.stopped = True

    def quote(total: int, second: int) -> SimpleNamespace:
        return SimpleNamespace(
            InstrumentID="SA701C1080",
            ExchangeID="CZCE",
            LastPrice=60,
            BidPrice1=59,
            AskPrice1=61,
            BidVolume1=2,
            AskVolume1=3,
            OpenInterest=1000,
            UpperLimitPrice=100,
            LowerLimitPrice=1,
            Volume=total,
            TradingDay="20260909",
            ActionDay="20260909",
            UpdateTime=f"09:30:0{second}",
            UpdateMillisec=0,
        )

    monkeypatch.setattr("bt_api_ctp.feeds.live_ctp_feed.ctp_client.MdClient", FakeMdClient)
    stream = CtpMarketStream(
        topics=[
            {
                "topic": "tick",
                "symbol": "SA701C1080",
                "quote_v2": {"asset_type": "option"},
            }
        ]
    )
    rows = []
    stream.push_data = rows.append

    stream.connect()
    old_client = clients[-1]
    old_client.on_tick(quote(100, 1))
    stream.disconnect()
    stream.connect()
    current_client = clients[-1]
    current_client.on_tick(quote(107, 2))
    # The old callback retains its first client's closure.  It must be dropped
    # rather than stamped with the current generation/epoch.
    old_client.on_tick(quote(110, 3))
    current_client.on_tick(quote(109, 4))

    assert len(rows) == 3
    assert [row.connection_generation for row in rows] == [1, 2, 2]
    assert [row.subscription_epoch for row in rows] == [1, 2, 2]
    assert [row.ingest_seq for row in rows] == [1, 1, 2]
    assert [row.volume_complete for row in rows] == [False, False, True]
    assert [row.delta_volume for row in rows] == [0.0, 0.0, 2.0]


def test_market_stream_native_reconnect_advances_generation_epoch_and_volume_scope() -> None:
    def quote(total: int, second: int) -> SimpleNamespace:
        return SimpleNamespace(
            InstrumentID="SA701C1080",
            ExchangeID="CZCE",
            LastPrice=60,
            BidPrice1=59,
            AskPrice1=61,
            BidVolume1=2,
            AskVolume1=3,
            OpenInterest=1000,
            UpperLimitPrice=100,
            LowerLimitPrice=1,
            Volume=total,
            TradingDay="20260909",
            ActionDay="20260909",
            UpdateTime=f"09:30:0{second}",
            UpdateMillisec=0,
        )

    stream = CtpMarketStream(
        topics=[
            {
                "topic": "tick",
                "symbol": "SA701C1080",
                "quote_v2": {"asset_type": "option"},
            }
        ]
    )
    client = SimpleNamespace(connection_generation=1)
    stream._md_client = client
    rows = []
    stream.push_data = rows.append

    stream._on_tick(quote(100, 1))
    client.connection_generation = 2
    stream._on_tick(quote(107, 2))

    assert [row.connection_generation for row in rows] == [1, 2]
    assert [row.subscription_epoch for row in rows] == [1, 2]
    assert [row.ingest_seq for row in rows] == [1, 1]
    assert [row.volume_complete for row in rows] == [False, False]


def test_market_stream_converts_cumulative_volume_to_delta_once() -> None:
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
        ({"BidPrice1": float("nan")}, "INVALID_BID_PRICE"),
        ({"AskPrice1": float("nan")}, "INVALID_ASK_PRICE"),
        ({"AskVolume1": -1}, "INVALID_ASK_VOLUME"),
        ({"BidVolume1": -1}, "INVALID_BID_VOLUME"),
        ({"Volume": -1}, "INVALID_CUM_VOLUME"),
        ({"OpenInterest": -1}, "INVALID_OPEN_INTEREST"),
        ({"BidPrice1": 4002, "AskPrice1": 4001}, "CROSSED_BOOK"),
    ],
)
def test_direct_market_stream_rejects_invalid_quote_without_advancing_volume(
    overrides: dict[str, float], quality_flag: str
) -> None:
    stream = CtpMarketStream(topics=[])
    stream._md_client = SimpleNamespace(connection_generation=12)
    rows = []
    stream.push_data = rows.append

    def push(total: int, second: int, **changes) -> None:
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
        fields.update(changes)
        stream._on_tick(SimpleNamespace(**fields))

    push(100, 1)
    push(105, 2, **overrides)
    push(110, 3)

    assert [row.delta_volume for row in rows] == [0, 0, 10]
    assert rows[1].volume_quality == "INVALID_QUOTE"
    assert quality_flag in rows[1].quality_flags
    assert all(row.get_all_data()["execution_eligible"] is False for row in rows)


@pytest.mark.parametrize(
    ("overrides", "quality_flag"),
    [
        ({"BidVolume1": 0}, "ZERO_DEPTH"),
        ({"BidPrice1": 4000, "AskPrice1": 4000}, "LOCKED_BOOK"),
    ],
)
def test_direct_market_stream_records_thin_or_locked_book_without_self_eligibility(
    overrides: dict[str, float], quality_flag: str
) -> None:
    stream = CtpMarketStream(topics=[])
    stream._md_client = SimpleNamespace(connection_generation=13)
    rows = []
    stream.push_data = rows.append
    fields = {
        "InstrumentID": "IF2506",
        "ExchangeID": "CFFEX",
        "LastPrice": 4000,
        "BidPrice1": 3999,
        "AskPrice1": 4001,
        "BidVolume1": 2,
        "AskVolume1": 3,
        "OpenInterest": 1000,
        "Volume": 100,
        "TradingDay": "20260909",
        "ActionDay": "20260909",
        "UpdateTime": "09:30:01",
        "UpdateMillisec": 0,
    }
    fields.update(overrides)
    stream._on_tick(SimpleNamespace(**fields))

    assert len(rows) == 1
    assert quality_flag in rows[0].quality_flags
    assert rows[0].get_all_data()["execution_eligible"] is False








def test_trade_stream_reuses_request_feed_trader_without_second_td_client(
    monkeypatch,
) -> None:
    stopped = []
    trader = SimpleNamespace(
        is_read_only_ready=True,
        auto_settlement_confirm=False,
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
        auto_settlement_confirm=False,
        wait_ready=lambda timeout: waited.append(timeout) or True,
        stop=lambda: stopped.append(True),
    )
    monkeypatch.setattr(
        client_module,
        "TraderClient",
        lambda *_args, **_kwargs: pytest.fail("existing TD client must not be replaced"),
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
        auto_settlement_confirm = False

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
        entry["sha256"] == hashlib.sha256((package_root / entry["path"]).read_bytes()).hexdigest()
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

    first_manifest, first_sha256 = client_module._ctp_python_package_identity(package_root)
    repeated_manifest, repeated_sha256 = client_module._ctp_python_package_identity(package_root)
    source.write_text("VALUE = 'second'\n", encoding="utf-8")
    changed_manifest, changed_sha256 = client_module._ctp_python_package_identity(package_root)

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
    monkeypatch.setattr(client_module, "_is_vendored_native_trader_api", lambda _api: True)

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
    monkeypatch.setattr(client_module, "_is_vendored_native_trader_api", lambda _api: False)
    monkeypatch.setattr(
        client_module._ctp_base,
        "_submit_public_trader_user_login",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected ABI guard")),
    )

    assert client_module._submit_trader_user_login(api, field, 23) == 29
    assert api.calls == [(field, 23)]
