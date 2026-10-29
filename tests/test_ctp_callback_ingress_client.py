"""Fake-only integration tests for TraderClient's durable callback owner seam."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

import bt_api_ctp.ctp.client as client_module
from bt_api_ctp.ctp.callback_ingress import (
    CALLBACK_SPECS,
    SAFE_FIELD_KINDS,
    CtpTraderCallbackIngressAckV2,
)
from bt_api_ctp.ctp.client import CtpExecutionGateError, TraderClient
from bt_api_ctp.ctp.ctp_structs_order import (
    CThostFtdcInputOrderActionField,
    CThostFtdcInputOrderField,
)


class _OwnerIntent:
    owner_intent_id = "owner-intent-1"


class _Sink:
    def __init__(self):
        self.records = []
        self.poisons = []

    def append(self, record):
        self.records.append(record)
        return CtpTraderCallbackIngressAckV2(
            owner_id=record.owner_intent_id,
            sequence=record.source_sequence,
            digest=record.canonical_sha256,
            commit_state="COMMITTED",
            high_watermark=record.source_sequence,
        )

    def poison_ingress(self, owner, code, *, source_tags=None, last_sequence=None):
        self.poisons.append((owner, code, source_tags, last_sequence))

        class CtpTraderCallbackIngressPoisonAckV2:
            def __init__(self):
                self.owner_intent_id = owner.owner_intent_id
                self.durable_state = "POISONED"
                self.last_source_sequence = last_sequence
                self.poison_code = code
                self.committed = True

        return CtpTraderCallbackIngressPoisonAckV2()


def _typed(name, **values):
    cls = type(name, (), {})
    obj = cls()
    for key, value in values.items():
        setattr(obj, key, value)
    return obj


class _FakeTraderApi:
    def __init__(self):
        self.spi = None
        self.calls = []
        self.register_spi_exception = None
        self.init_exception = None
        self.order_insert_exception = None
        self.join_started = threading.Event()
        self.join_release = threading.Event()
        self.join_release.set()
        self.order_insert_result = 0
        self.order_action_result = 0
        self.query_result = 0
        self.query_callbacks = {}
        self.callback_during_insert = None
        self.block_insert = False
        self.insert_entered = threading.Event()
        self.insert_release = threading.Event()
        self.insert_release.set()
        self.release_completed = threading.Event()

    def RegisterSpi(self, spi):
        if spi is not None and self.register_spi_exception is not None:
            raise self.register_spi_exception
        self.spi = spi

    def SubscribePrivateTopic(self, _topic):
        return None

    def SubscribePublicTopic(self, _topic):
        return None

    def RegisterFront(self, _front):
        return None

    def Init(self):
        if self.init_exception is not None:
            raise self.init_exception
        self.spi.OnFrontConnected()

    def Join(self):
        self.join_started.set()
        self.join_release.wait(2)
        return 0

    def Release(self):
        self.calls.append(("Release",))
        self.release_completed.set()

    def ReqAuthenticate(self, field, request_id):
        self.calls.append(("ReqAuthenticate", request_id))
        response = SimpleNamespace(BrokerID=field.BrokerID, UserID=field.UserID)
        info = SimpleNamespace(ErrorID=0, ErrorMsg="")
        self.spi.OnRspAuthenticate(response, info, request_id, True)
        return 0

    def ReqUserLogin(self, field, request_id):
        self.calls.append(("ReqUserLogin", request_id))
        response = SimpleNamespace(
            BrokerID=field.BrokerID,
            UserID=field.UserID,
            TradingDay="20260926",
            FrontID=7,
            SessionID=19,
            MaxOrderRef="17",
        )
        info = SimpleNamespace(ErrorID=0, ErrorMsg="")
        self.spi.OnRspUserLogin(response, info, request_id, True)
        return 0

    def ReqOrderInsert(self, field, request_id):
        self.calls.append(("ReqOrderInsert", field, request_id))
        if self.order_insert_exception is not None:
            raise self.order_insert_exception
        if self.block_insert:
            self.insert_entered.set()
            self.insert_release.wait(2)
        if self.callback_during_insert == "same_thread":
            self.spi.OnFrontDisconnected(1001)
        elif self.callback_during_insert == "cross_thread":
            finished = threading.Event()

            def callback():
                self.spi.OnFrontDisconnected(1001)
                finished.set()

            thread = threading.Thread(target=callback)
            thread.start()
            thread.join(2)
            if thread.is_alive() or not finished.is_set():
                raise AssertionError(
                    "native request held an SDK lock across callback wait"
                )
        return self.order_insert_result

    def ReqOrderAction(self, field, request_id):
        self.calls.append(("ReqOrderAction", field, request_id))
        return self.order_action_result

    def ReqQryTradingAccount(self, field, request_id):
        self.calls.append(("ReqQryTradingAccount", field, request_id))
        return self.query_result

    def ReqQryProduct(self, field, request_id):
        self.calls.append(("ReqQryProduct", field, request_id))
        callback = self.query_callbacks.get("ReqQryProduct")
        if callback is not None:
            callback(field, request_id)
        return self.query_result

    def ReqQryInstrument(self, field, request_id):
        self.calls.append(("ReqQryInstrument", field, request_id))
        callback = self.query_callbacks.get("ReqQryInstrument")
        if callback is not None:
            callback(field, request_id)
        return self.query_result

    def ReqQryDepthMarketData(self, field, request_id):
        self.calls.append(("ReqQryDepthMarketData", field, request_id))
        callback = self.query_callbacks.get("ReqQryDepthMarketData")
        if callback is not None:
            callback(field, request_id)
        return self.query_result


class _FakeTraderApiFactory:
    instances = []
    block_join = False
    register_spi_exception = None
    init_exception = None

    @classmethod
    def CreateFtdcTraderApi(cls, _flow_path):
        api = _FakeTraderApi()
        if cls.block_join:
            api.join_release.clear()
        api.register_spi_exception = cls.register_spi_exception
        api.init_exception = cls.init_exception
        cls.instances.append(api)
        return api


def _session_binder(owner, *, observation, source_tags, high_watermark):
    return _typed(
        "CtpCallbackSessionBindingV1",
        owner_intent_id=owner.owner_intent_id,
        account_key="account-key-1",
        scope_key="scope-key-1",
        trading_day=observation.trading_day,
        session_generation_id="session-generation-1",
        dispatch_front_id=7,
        dispatch_session_id=19,
        source_instance_id=source_tags.source_instance_id,
        native_client_epoch=source_tags.native_client_epoch,
        native_api_source_id=source_tags.native_api_source_id,
        native_spi_source_id=source_tags.native_spi_source_id,
        native_api_generation=source_tags.native_api_generation,
        source_connection_generation=source_tags.connection_generation,
        connection_generation=observation.connection_generation,
        source_high_watermark=high_watermark,
        session_binding_sha256="a" * 64,
    )


def _new_active_client(
    monkeypatch,
    *,
    sink=None,
    expect_active=True,
    block_join=False,
    front="tcp://fake",
):
    _FakeTraderApiFactory.instances = []
    _FakeTraderApiFactory.block_join = block_join
    _FakeTraderApiFactory.register_spi_exception = None
    _FakeTraderApiFactory.init_exception = None
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "fake-flow")
    monkeypatch.setattr(client_module, "_register_ctp_native_api", lambda _api: None)
    monkeypatch.setattr(client_module, "CThostFtdcTraderApi", _FakeTraderApiFactory)

    client = TraderClient(front, "9999", "investor-1", "not-a-real-secret")
    sink = sink or _Sink()
    owner = _OwnerIntent()
    exposure = []
    client.on_login = lambda _field: exposure.append(
        (
            len(sink.records),
            client._callback_ingress.phase,
            client._callback_ingress.active_session is not None,
        )
    )
    client.install_callback_ingress_sink(
        owner,
        sink,
        _session_binder,
        lambda verified_owner, binding: binding if verified_owner is owner else None,
    )
    client.start()
    assert _FakeTraderApiFactory.instances
    api = _FakeTraderApiFactory.instances[-1]
    assert api.join_started.wait(1)
    assert client._callback_ingress is not None
    if expect_active:
        assert client._callback_ingress.phase == "ACTIVE", (
            client._callback_ingress.poison_reason,
            [record.callback_name for record in sink.records],
            sink.poisons,
        )
        assert [record.callback_name for record in sink.records] == [
            "OnFrontConnected",
            "OnRspAuthenticate",
            "OnRspUserLogin",
        ]
        assert client._callback_ingress.active_session.source_high_watermark == 3
    client._test_login_exposure = exposure
    return client, api, sink, owner


def _request_payload(**changes):
    values = {
        "BrokerID": "9999",
        "InvestorID": "investor-1",
        "UserID": "investor-1",
        "InstrumentID": "rb2610",
        "OrderRef": "000000000017",
        "Direction": "0",
        "CombOffsetFlag": "0",
        "CombHedgeFlag": "1",
        "OrderPriceType": "2",
        "LimitPrice": "3512.5",
        "VolumeTotalOriginal": 1,
        "TimeCondition": "3",
        "ExchangeID": "SHFE",
    }
    values.update(changes)
    return values


def _binding(
    owner, session, payload, *, operation="SUBMIT", action_ref=None, request_id=41
):
    payload_text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    native_payload = dict(payload)
    if operation == "CANCEL":
        native_payload["OrderActionRef"] = action_ref
    native_payload_text = json.dumps(
        native_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    envelope = {
        "binding_type": "ctp_managed_native_call_binding.v2",
        "owner_intent_id": owner.owner_intent_id,
        "account_key": session.account_key,
        "scope_key": session.scope_key,
        "command_id": f"command-{operation.lower()}-{request_id}",
        "operation": operation,
        "trading_day": session.trading_day,
        "request_payload_json": payload_text,
        "request_payload_sha256": hashlib.sha256(
            payload_text.encode("utf-8")
        ).hexdigest(),
        "native_request_payload_json": native_payload_text,
        "native_request_payload_sha256": hashlib.sha256(
            native_payload_text.encode("utf-8")
        ).hexdigest(),
        "reservation_managed_intent_id": "intent.order-1",
        "managed_action_id": "managed-action-1",
        "runtime_order_id": "bt-managed-v1:" + "b" * 64,
        "order_ref": payload["OrderRef"],
        "native_request_id": request_id,
        "native_action_ref": action_ref,
        "cancel_target_order_ref": payload["OrderRef"]
        if operation == "CANCEL"
        else None,
        "cancel_target_exchange_id": payload.get("ExchangeID")
        if operation == "CANCEL"
        else None,
        "cancel_target_order_sys_id": payload.get("OrderSysID")
        if operation == "CANCEL"
        else None,
        "cancel_target_front_id": payload.get("FrontID")
        if operation == "CANCEL"
        else None,
        "cancel_target_session_id": payload.get("SessionID")
        if operation == "CANCEL"
        else None,
        "session_binding_sha256": session.session_binding_sha256,
        "session_generation_id": session.session_generation_id,
        "dispatch_front_id": session.dispatch_front_id,
        "dispatch_session_id": session.dispatch_session_id,
        "writer_owner_id": "writer-1",
        "writer_fencing_token": 3,
        "expires_at_ns": time.time_ns() + 30_000_000_000,
    }

    class CtpManagedNativeCallBindingV2:
        def __init__(self):
            self.envelope = dict(envelope)

        def to_payload(self):
            return dict(self.envelope)

    return CtpManagedNativeCallBindingV2()


def _field(field_type, payload, **extras):
    field = field_type()
    for name, value in {**payload, **extras}.items():
        setattr(field, name, float(value) if name == "LimitPrice" else value)
    return field


def test_native_start_installs_owner_before_spi_and_acks_before_login_exposure(
    monkeypatch,
):
    client, api, sink, _owner = _new_active_client(monkeypatch)
    try:
        assert api.calls[:2] == [("ReqAuthenticate", 1), ("ReqUserLogin", 2)]
        assert client._callback_ingress.active_session.connection_generation == 1
        assert client._callback_ingress.active_session.source_connection_generation == 0
        assert client._callback_ingress.active_session.source_high_watermark == 3
        assert all(record.capture_complete for record in sink.records)
        assert client._test_login_exposure == [(3, "ACTIVE", True)]
    finally:
        client.stop()


def test_commission_query_native_stall_outlives_deadline_and_fences_session_work(
    monkeypatch,
):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    native_request_ids = []
    query_results = []
    query_errors = []
    deadline = time.monotonic() + 0.08
    observer = None

    def blocked_commission_query(_field, request_id):
        native_request_ids.append(request_id)
        entered.set()
        if not release.wait(2):
            raise AssertionError("test did not release the fake native request")
        return 0

    api.ReqQryInstrumentCommissionRate = blocked_commission_query
    client._query_interval = 0
    client._last_query_submitted_at = 0.0

    def run_blocked_query():
        try:
            query_results.append(
                client.query_instrument_commission_rate_result(
                    "rb2610",
                    "SHFE",
                    timeout=1,
                    deadline_monotonic=deadline,
                )
            )
        except BaseException as exc:
            query_errors.append(exc)

    query_thread = threading.Thread(target=run_blocked_query)
    try:
        query_thread.start()
        assert entered.wait(1)
        assert len(native_request_ids) == 1

        # The SDK's deadline cannot interrupt a synchronous native Req call.
        remaining = max(0.0, deadline - time.monotonic()) + 0.03
        assert release.wait(remaining) is False
        assert time.monotonic() > deadline
        assert query_thread.is_alive()
        with client._query_state_lock:
            assert client._callback_ingress.native_call_refs == 1
            assert client._native_request_inflight_refs == 1

        # Other threads can still read query state, while same-session native
        # work and a managed write lease remain fenced by the in-flight Req.
        visible = {}
        visible_done = threading.Event()

        def read_query_state():
            visible["counts"] = client.get_request_counts()
            with client._query_state_lock:
                visible["native_call_refs"] = client._callback_ingress.native_call_refs
            visible_done.set()

        observer = threading.Thread(target=read_query_state)
        observer.start()
        assert visible_done.wait(0.5)
        observer.join(timeout=1)
        assert not observer.is_alive()
        assert visible["counts"]["query_commission_rate"] == 1
        assert visible["native_call_refs"] == 1

        core_result = client.query_instruments_result(
            timeout=0.5,
            deadline_monotonic=time.monotonic() + 1,
        )
        assert core_result.complete is False
        assert core_result.error_message == "query_submit_exception:CtpExecutionGateError"
        assert not any(call[0] == "ReqQryInstrument" for call in api.calls)

        binding = _binding(owner, client._callback_ingress.active_session, _request_payload())
        with pytest.raises(
            CtpExecutionGateError,
            match="ctp_managed_native_call_owner_unavailable",
        ):
            client.acquire_managed_native_call_lease(owner, binding)
        assert not any(call[0] == "ReqOrderInsert" for call in api.calls)

        release.set()
        query_thread.join(timeout=1)
        assert not query_thread.is_alive()
        assert query_errors == []
        assert len(query_results) == 1
        assert query_results[0].timed_out is True
        assert query_results[0].error_message == "query_deadline_exceeded"
        with client._query_state_lock:
            assert client._callback_ingress.native_call_refs == 0
            assert client._native_request_inflight_refs == 0

        def complete_instrument_query(field, request_id):
            api.calls.append(("ReqQryInstrument", field, request_id))
            api.spi.OnRspQryInstrument(
                SimpleNamespace(InstrumentID="rb2610"), None, request_id, True
            )
            return 0

        def complete_commission_query(field, request_id):
            api.calls.append(("ReqQryInstrumentCommissionRate", field, request_id))
            api.spi.OnRspQryInstrumentCommissionRate(
                SimpleNamespace(InstrumentID=field.InstrumentID),
                None,
                request_id,
                True,
            )
            return 0

        api.ReqQryInstrument = complete_instrument_query
        api.ReqQryInstrumentCommissionRate = complete_commission_query
        recovered_core = client.query_instruments_result(
            timeout=0.5,
            deadline_monotonic=time.monotonic() + 1,
        )
        recovered_commission = client.query_instrument_commission_rate_result(
            "rb2610",
            "SHFE",
            timeout=0.5,
            deadline_monotonic=time.monotonic() + 1,
        )
        assert recovered_core.complete is True
        assert recovered_core.first["InstrumentID"] == "rb2610"
        assert recovered_commission.complete is True
        assert recovered_commission.first["InstrumentID"] == "rb2610"

        recovered_binding = _binding(
            owner,
            client._callback_ingress.active_session,
            _request_payload(),
            request_id=42,
        )
        assert client.acquire_managed_native_call_lease(owner, recovered_binding)
    finally:
        release.set()
        query_thread.join(timeout=2)
        if observer is not None:
            observer.join(timeout=1)
        client.stop()
        assert not query_thread.is_alive()
        assert observer is None or not observer.is_alive()


def test_store_binding_sends_detached_insert_and_consumes_one_lease(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        api.spi.OnHeartBeatWarning(1)
        assert client._callback_ingress.sequence == 4
        assert client._callback_ingress.active_session.source_high_watermark == 3
        payload = _request_payload()
        binding = _binding(owner, client._callback_ingress.active_session, payload)
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = _field(CThostFtdcInputOrderField, payload)

        assert client.submit_order_insert_with_lease(lease, field, 41) == 0
        calls = [call for call in api.calls if call[0] == "ReqOrderInsert"]
        assert len(calls) == 1
        _name, detached, request_id = calls[0]
        assert request_id == 41
        assert detached is not field
        assert {name: getattr(detached, name) for name in payload} == {
            **payload,
            "LimitPrice": 3512.5,
        }
        with pytest.raises(CtpExecutionGateError):
            client.submit_order_insert_with_lease(lease, field, 41)
    finally:
        client.stop()


def test_managed_native_lease_rejects_live_query_before_store_verifier(monkeypatch):
    client, _api, _sink, owner = _new_active_client(monkeypatch)
    accumulator = None
    try:
        payload = _request_payload()
        binding = _binding(owner, client._callback_ingress.active_session, payload)
        accumulator = client._new_query_accumulator("positions")
        verifier_calls = []
        client._callback_ingress.command_binding_verifier = (
            lambda *_args: verifier_calls.append(True)
        )

        with pytest.raises(
            CtpExecutionGateError,
            match="ctp_managed_native_call_owner_unavailable",
        ):
            client.acquire_managed_native_call_lease(owner, binding)

        assert verifier_calls == []
        assert not accumulator.sealed
    finally:
        if accumulator is not None:
            with client._query_state_lock:
                accumulator.sealed = True
                accumulator.event.set()
        client.stop()


def test_callback_between_lease_acquire_and_submit_fences_native_insert(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = _request_payload()
        binding = _binding(owner, client._callback_ingress.active_session, payload)
        state = client._callback_ingress
        lease = client.acquire_managed_native_call_lease(owner, binding)
        acquired_sequence = lease._source_sequence
        api.spi.OnHeartBeatWarning(1)
        assert state.sequence == acquired_sequence + 1

        field = _field(CThostFtdcInputOrderField, payload)
        with pytest.raises(
            CtpExecutionGateError,
            match="ctp_managed_native_call_source_sequence_changed",
        ):
            client.submit_order_insert_with_lease(lease, field, 41)

        assert not any(call[0] == "ReqOrderInsert" for call in api.calls)
        assert state.command_lease is None
        assert state.native_call_refs == 0
        assert client._native_request_inflight_refs == 0
        assert not client._managed_native_call_leases
        assert not state.poisoned
    finally:
        client.stop()


def test_new_query_is_rejected_while_managed_lease_is_active_and_sealed(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = _request_payload()
        binding = _binding(owner, client._callback_ingress.active_session, payload)
        lease = client.acquire_managed_native_call_lease(owner, binding)

        result = client.query_account_result(timeout=0)

        assert result.error_message == "query_submit_exception:CtpExecutionGateError"
        accumulator = client._query_history[result.request_id]
        assert accumulator.sealed
        assert accumulator.event.is_set()
        assert not any(call[0] == "ReqQryTradingAccount" for call in api.calls)
        assert client._callback_ingress.command_lease is lease
        assert client._callback_ingress.native_call_refs == 1

        field = _field(CThostFtdcInputOrderField, payload)
        assert client.submit_order_insert_with_lease(lease, field, 41) == 0
        assert any(call[0] == "ReqOrderInsert" for call in api.calls)
    finally:
        client.stop()


def test_synchronous_order_and_trade_callbacks_remain_allowed_during_insert(monkeypatch):
    client, api, sink, owner = _new_active_client(monkeypatch)
    try:
        payload = _request_payload()
        binding = _binding(owner, client._callback_ingress.active_session, payload)
        lease = client.acquire_managed_native_call_lease(owner, binding)
        state = client._callback_ingress
        callback_sequence = state.sequence
        delivered = []
        client.on_order = lambda value: delivered.append(("order", value))
        client.on_trade = lambda value: delivered.append(("trade", value))

        order = _typed(
            "Order",
            BrokerID="9999",
            InvestorID="investor-1",
            UserID="investor-1",
            InstrumentID="rb2610",
            ExchangeID="SHFE",
            OrderRef="000000000017",
            RequestID=41,
            TradingDay="20260926",
        )
        trade = _typed(
            "Trade",
            InstrumentID="rb2610",
            ExchangeID="SHFE",
            BrokerID="9999",
            InvestorID="investor-1",
            UserID="investor-1",
            TradeID="trade-1",
            OrderRef="000000000017",
            TradeDate="20260926",
            TradingDay="20260926",
            SequenceNo=1,
            Price=3512.5,
            Volume=1,
        )

        def req_order_insert(_field, request_id):
            assert request_id == 41
            assert state.command_lease is None
            assert state.native_call_refs == 1
            assert client._native_request_inflight_refs == 1
            api.spi.OnRtnOrder(order)
            api.spi.OnRtnTrade(trade)
            return 0

        api.ReqOrderInsert = req_order_insert
        field = _field(CThostFtdcInputOrderField, payload)
        assert client.submit_order_insert_with_lease(lease, field, 41) == 0

        assert delivered == [("order", order), ("trade", trade)]
        assert [record.callback_name for record in sink.records[-2:]] == [
            "OnRtnOrder",
            "OnRtnTrade",
        ]
        assert state.sequence == callback_sequence + 2
        assert not state.poisoned
        assert state.native_call_refs == 0
        assert client._native_request_inflight_refs == 0
    finally:
        client.stop()


def test_store_command_id_cannot_acquire_a_second_lease_after_insert(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = _request_payload()
        binding = _binding(owner, client._callback_ingress.active_session, payload)
        field = _field(CThostFtdcInputOrderField, payload)
        lease = client.acquire_managed_native_call_lease(owner, binding)

        assert client.submit_order_insert_with_lease(lease, field, 41) == 0
        with pytest.raises(
            CtpExecutionGateError,
            match="ctp_managed_native_call_binding_reused",
        ):
            client.acquire_managed_native_call_lease(owner, binding)

        assert len([call for call in api.calls if call[0] == "ReqOrderInsert"]) == 1
    finally:
        client.stop()


def test_source_replacement_after_lease_acquisition_fences_native_insert(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = _request_payload()
        binding = _binding(owner, client._callback_ingress.active_session, payload)
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = _field(CThostFtdcInputOrderField, payload)
        replacement_api = _FakeTraderApi()

        client._api = replacement_api

        with pytest.raises(
            CtpExecutionGateError,
            match="ctp_managed_native_call_lease_invalid",
        ):
            client.submit_order_insert_with_lease(lease, field, 41)

        assert not any(call[0] == "ReqOrderInsert" for call in api.calls)
        assert not any(call[0] == "ReqOrderInsert" for call in replacement_api.calls)
    finally:
        client.stop()


def test_managed_insert_rejects_caller_field_that_differs_from_store_binding(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = _request_payload()
        binding = _binding(owner, client._callback_ingress.active_session, payload)
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = _field(
            CThostFtdcInputOrderField,
            {**payload, "OrderRef": "000000000018"},
        )

        with pytest.raises(
            CtpExecutionGateError,
            match="ctp_managed_native_field_binding_mismatch",
        ):
            client.submit_order_insert_with_lease(lease, field, 41)

        assert not any(call[0] == "ReqOrderInsert" for call in api.calls)
    finally:
        client.stop()


def test_official_simnow_front_blocks_store_lease_before_native_insert(monkeypatch):
    from bt_api_ctp.ctp_env_selector import official_simnow_fronts

    td_front, _md_front = official_simnow_fronts("set2_7x24")
    client, api, _sink, owner = _new_active_client(monkeypatch, front=td_front)
    try:
        payload = _request_payload()
        binding = _binding(owner, client._callback_ingress.active_session, payload)
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = _field(CThostFtdcInputOrderField, payload)

        with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
            client.submit_order_insert_with_lease(lease, field, 41)
        assert not any(call[0] == "ReqOrderInsert" for call in api.calls)
    finally:
        client.stop()


def test_cancel_action_ref_is_independent_and_filled_from_store_binding(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = {
            "BrokerID": "9999",
            "InvestorID": "investor-1",
            "UserID": "investor-1",
            "InstrumentID": "rb2610",
            "OrderRef": "000000000017",
            "ExchangeID": "SHFE",
            "OrderSysID": "SYS-17",
            "FrontID": 7,
            "SessionID": 19,
            "ActionFlag": "0",
            "LimitPrice": 0,
            "VolumeChange": 0,
        }
        binding = _binding(
            owner,
            client._callback_ingress.active_session,
            payload,
            operation="CANCEL",
            action_ref=91,
            request_id=53,
        )
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = _field(
            CThostFtdcInputOrderActionField,
            payload,
            RequestID=53,
            OrderActionRef=91,
        )

        assert client.submit_order_action_with_lease(lease, field, 53) == 0
        calls = [call for call in api.calls if call[0] == "ReqOrderAction"]
        assert len(calls) == 1
        _name, detached, request_id = calls[0]
        assert request_id == detached.RequestID == 53
        assert type(detached.OrderActionRef) is int
        assert detached.OrderActionRef == 91
        assert detached.OrderActionRef != detached.RequestID
        assert detached is not field
    finally:
        client.stop()


@pytest.mark.parametrize("action_ref", [True, "91", 0, 2_147_483_648])
def test_managed_cancel_rejects_non_int32_store_action_ref_before_native_call(
    monkeypatch, action_ref
):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = {
            "BrokerID": "9999",
            "InvestorID": "investor-1",
            "UserID": "investor-1",
            "InstrumentID": "rb2610",
            "OrderRef": "000000000017",
            "ExchangeID": "SHFE",
            "OrderSysID": "SYS-17",
            "FrontID": 7,
            "SessionID": 19,
            "ActionFlag": "0",
            "LimitPrice": 0,
            "VolumeChange": 0,
        }
        binding = _binding(
            owner,
            client._callback_ingress.active_session,
            payload,
            operation="CANCEL",
            action_ref=action_ref,
            request_id=53,
        )
        with pytest.raises(CtpExecutionGateError, match="binding_action_ref_invalid"):
            client.acquire_managed_native_call_lease(owner, binding)
        assert not [call for call in api.calls if call[0] == "ReqOrderAction"]
    finally:
        client.stop()


@pytest.mark.parametrize("source_action_ref", [0, 444, "91", True])
def test_managed_cancel_requires_caller_field_to_match_store_action_ref(
    monkeypatch, source_action_ref
):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = {
            "BrokerID": "9999",
            "InvestorID": "investor-1",
            "UserID": "investor-1",
            "InstrumentID": "rb2610",
            "OrderRef": "000000000017",
            "ExchangeID": "SHFE",
            "OrderSysID": "SYS-17",
            "FrontID": 7,
            "SessionID": 19,
            "ActionFlag": "0",
            "LimitPrice": 0,
            "VolumeChange": 0,
        }
        binding = _binding(
            owner,
            client._callback_ingress.active_session,
            payload,
            operation="CANCEL",
            action_ref=91,
            request_id=53,
        )
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = _field(
            CThostFtdcInputOrderActionField,
            payload,
            RequestID=53,
            OrderActionRef=source_action_ref,
        )

        with pytest.raises(CtpExecutionGateError):
            client.submit_order_action_with_lease(lease, field, 53)
        assert not [call for call in api.calls if call[0] == "ReqOrderAction"]
    finally:
        client.stop()


def test_managed_cancel_requires_caller_field_request_id_match(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = {
            "BrokerID": "9999",
            "InvestorID": "investor-1",
            "UserID": "investor-1",
            "InstrumentID": "rb2610",
            "OrderRef": "000000000017",
            "ExchangeID": "SHFE",
            "OrderSysID": "SYS-17",
            "FrontID": 7,
            "SessionID": 19,
            "ActionFlag": "0",
            "LimitPrice": 0,
            "VolumeChange": 0,
        }
        binding = _binding(
            owner,
            client._callback_ingress.active_session,
            payload,
            operation="CANCEL",
            action_ref=91,
            request_id=53,
        )
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = _field(
            CThostFtdcInputOrderActionField,
            payload,
            RequestID=54,
            OrderActionRef=91,
        )

        with pytest.raises(CtpExecutionGateError):
            client.submit_order_action_with_lease(lease, field, 53)
        assert not [call for call in api.calls if call[0] == "ReqOrderAction"]
    finally:
        client.stop()


def test_managed_cancel_rejects_caller_field_without_action_ref_getter(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = {
            "BrokerID": "9999",
            "InvestorID": "investor-1",
            "UserID": "investor-1",
            "InstrumentID": "rb2610",
            "OrderRef": "000000000017",
            "ExchangeID": "SHFE",
            "OrderSysID": "SYS-17",
            "FrontID": 7,
            "SessionID": 19,
            "ActionFlag": "0",
            "LimitPrice": 0,
            "VolumeChange": 0,
        }
        binding = _binding(
            owner,
            client._callback_ingress.active_session,
            payload,
            operation="CANCEL",
            action_ref=91,
            request_id=53,
        )
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = SimpleNamespace(**payload, RequestID=53)

        with pytest.raises(CtpExecutionGateError):
            client.submit_order_action_with_lease(lease, field, 53)
        assert not [call for call in api.calls if call[0] == "ReqOrderAction"]
    finally:
        client.stop()


def test_managed_cancel_requires_native_payload_exactly_adds_store_action_ref(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = {
            "BrokerID": "9999",
            "InvestorID": "investor-1",
            "UserID": "investor-1",
            "InstrumentID": "rb2610",
            "OrderRef": "000000000017",
            "ExchangeID": "SHFE",
            "OrderSysID": "SYS-17",
            "FrontID": 7,
            "SessionID": 19,
            "ActionFlag": "0",
            "LimitPrice": 0,
            "VolumeChange": 0,
        }
        binding = _binding(
            owner,
            client._callback_ingress.active_session,
            payload,
            operation="CANCEL",
            action_ref=91,
            request_id=53,
        )
        binding.envelope["native_request_payload_json"] = json.dumps(
            {**payload, "OrderActionRef": 92},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        binding.envelope["native_request_payload_sha256"] = hashlib.sha256(
            binding.envelope["native_request_payload_json"].encode("utf-8")
        ).hexdigest()

        with pytest.raises(CtpExecutionGateError, match="payload_action_ref_mismatch"):
            client.acquire_managed_native_call_lease(owner, binding)
        assert not [call for call in api.calls if call[0] == "ReqOrderAction"]
    finally:
        client.stop()


def test_managed_cancel_rejects_caller_action_ref_in_logical_payload(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = {
            "BrokerID": "9999",
            "InvestorID": "investor-1",
            "UserID": "investor-1",
            "InstrumentID": "rb2610",
            "OrderRef": "000000000017",
            "ExchangeID": "SHFE",
            "OrderSysID": "SYS-17",
            "FrontID": 7,
            "SessionID": 19,
            "ActionFlag": "0",
            "LimitPrice": 0,
            "VolumeChange": 0,
            "OrderActionRef": 92,
        }
        binding = _binding(
            owner,
            client._callback_ingress.active_session,
            payload,
            operation="CANCEL",
            action_ref=91,
            request_id=53,
        )

        with pytest.raises(CtpExecutionGateError, match="logical_action_ref_forbidden"):
            client.acquire_managed_native_call_lease(owner, binding)
        assert not [call for call in api.calls if call[0] == "ReqOrderAction"]
    finally:
        client.stop()


def test_managed_submit_requires_native_payload_bytes_equal_logical(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = _request_payload()
        binding = _binding(
            owner,
            client._callback_ingress.active_session,
            payload,
            operation="SUBMIT",
            request_id=41,
        )
        native_payload = dict(payload)
        native_payload["OrderRef"] = "000000000018"
        binding.envelope["native_request_payload_json"] = json.dumps(
            native_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        binding.envelope["native_request_payload_sha256"] = hashlib.sha256(
            binding.envelope["native_request_payload_json"].encode("utf-8")
        ).hexdigest()

        with pytest.raises(CtpExecutionGateError, match="submit_payload_mismatch"):
            client.acquire_managed_native_call_lease(owner, binding)
        assert not [call for call in api.calls if call[0] == "ReqOrderInsert"]
    finally:
        client.stop()


def test_managed_cancel_rejects_native_action_ref_setter_normalization(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = {
            "BrokerID": "9999",
            "InvestorID": "investor-1",
            "UserID": "investor-1",
            "InstrumentID": "rb2610",
            "OrderRef": "000000000017",
            "ExchangeID": "SHFE",
            "OrderSysID": "SYS-17",
            "FrontID": 7,
            "SessionID": 19,
            "ActionFlag": "0",
            "LimitPrice": 0,
            "VolumeChange": 0,
        }

        class NormalizingActionField:
            instances = 0

            def __init__(self):
                type(self).instances += 1
                self.normalize_action_ref = type(self).instances > 1
                self.values = {}

            def __setattr__(self, name, value):
                if name in {"values", "normalize_action_ref"}:
                    object.__setattr__(self, name, value)
                elif name == "OrderActionRef" and self.normalize_action_ref:
                    self.values[name] = value + 1
                else:
                    self.values[name] = value

            def __getattr__(self, name):
                try:
                    return self.values[name]
                except KeyError as exc:
                    raise AttributeError(name) from exc

        binding = _binding(
            owner,
            client._callback_ingress.active_session,
            payload,
            operation="CANCEL",
            action_ref=91,
            request_id=53,
        )
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = _field(
            NormalizingActionField,
            payload,
            RequestID=53,
            OrderActionRef=91,
        )

        with pytest.raises(CtpExecutionGateError, match="field_snapshot_mismatch"):
            client.submit_order_action_with_lease(lease, field, 53)
        assert not [call for call in api.calls if call[0] == "ReqOrderAction"]
    finally:
        client.stop()


def test_managed_cancel_rejects_legacy_v1_string_binding(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = {
            "BrokerID": "9999",
            "InvestorID": "investor-1",
            "UserID": "investor-1",
            "InstrumentID": "rb2610",
            "OrderRef": "000000000017",
            "ExchangeID": "SHFE",
            "OrderSysID": "SYS-17",
            "FrontID": 7,
            "SessionID": 19,
            "ActionFlag": "0",
            "LimitPrice": 0,
            "VolumeChange": 0,
        }
        v2 = _binding(
            owner,
            client._callback_ingress.active_session,
            payload,
            operation="CANCEL",
            action_ref=91,
            request_id=53,
        )

        class CtpManagedNativeCallBindingV1:
            def to_payload(self):
                envelope = dict(v2.envelope)
                envelope["binding_type"] = "ctp_managed_native_call_binding.v1"
                return envelope

        with pytest.raises(CtpExecutionGateError, match="binding_untyped"):
            client.acquire_managed_native_call_lease(owner, CtpManagedNativeCallBindingV1())
        assert not [call for call in api.calls if call[0] == "ReqOrderAction"]
    finally:
        client.stop()


def test_native_request_can_join_callback_thread_without_sdk_lock_deadlock(monkeypatch):
    client, api, sink, owner = _new_active_client(monkeypatch)
    try:
        payload = _request_payload()
        binding = _binding(
            owner, client._callback_ingress.active_session, payload, request_id=43
        )
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = _field(CThostFtdcInputOrderField, payload)
        api.callback_during_insert = "cross_thread"

        with pytest.raises(
            CtpExecutionGateError, match="native_call_result_ambiguous"
        ):
            client.submit_order_insert_with_lease(lease, field, 43)
        disconnect_records = [
            record
            for record in sink.records
            if record.callback_name == "OnFrontDisconnected"
        ]
        assert len(disconnect_records) == 1
        assert client._callback_ingress.poisoned
        assert sink.poisons and sink.poisons[-1][1] == "disconnect"
        assert client._callback_ingress.native_call_refs == 0
        assert client._native_request_inflight_refs == 0
    finally:
        client.stop()


def test_stop_poison_defers_unregister_and_release_until_pinned_req_returns(
    monkeypatch,
):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    payload = _request_payload()
    binding = _binding(
        owner, client._callback_ingress.active_session, payload, request_id=45
    )
    lease = client.acquire_managed_native_call_lease(owner, binding)
    field = _field(CThostFtdcInputOrderField, payload)
    api.block_insert = True
    api.insert_release.clear()
    result = []
    errors = []

    def send():
        try:
            result.append(client.submit_order_insert_with_lease(lease, field, 45))
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    thread = threading.Thread(target=send)
    thread.start()
    assert api.insert_entered.wait(1)

    client.stop()

    assert client._callback_ingress.poisoned
    assert not api.release_completed.is_set()
    assert api.spi is not None
    api.insert_release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], CtpExecutionGateError)
    assert "native_call_result_ambiguous" in str(errors[0])
    assert not result
    assert api.release_completed.wait(2)
    assert api.spi is None


def test_same_thread_native_callback_reentry_is_recorded_before_req_returns(
    monkeypatch,
):
    client, api, sink, owner = _new_active_client(monkeypatch)
    try:
        payload = _request_payload()
        binding = _binding(
            owner, client._callback_ingress.active_session, payload, request_id=44
        )
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = _field(CThostFtdcInputOrderField, payload)
        api.callback_during_insert = "same_thread"

        with pytest.raises(
            CtpExecutionGateError, match="native_call_result_ambiguous"
        ):
            client.submit_order_insert_with_lease(lease, field, 44)
        assert [record.callback_name for record in sink.records][
            -1
        ] == "OnFrontDisconnected"
        assert client._callback_ingress.poisoned
    finally:
        client.stop()


def test_direct_legacy_send_is_closed_when_ingress_owner_is_installed(monkeypatch):
    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        with pytest.raises(CtpExecutionGateError, match="lease_required"):
            client.submit_order_insert(object(), 41)
        with pytest.raises(CtpExecutionGateError, match="lease_required"):
            client.submit_order_action(object(), 41)
        assert not any(
            call[0] in {"ReqOrderInsert", "ReqOrderAction"} for call in api.calls
        )
    finally:
        client.stop()


def test_generic_request_helper_preserves_settlement_grant_and_write_allowlist(
    monkeypatch,
):
    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        with pytest.raises(
            CtpExecutionGateError, match="settlement_authorization_required"
        ):
            client._invoke_session_native_request(
                api,
                "ReqSettlementInfoConfirm",
                object(),
                67,
            )
        with pytest.raises(CtpExecutionGateError, match="native_request_unsupported"):
            client._invoke_session_native_request(api, "ReqOrderAction", object(), 68)
        assert not any(
            call[0] in {"ReqSettlementInfoConfirm", "ReqOrderAction"}
            for call in api.calls
        )
        assert client._callback_ingress.poisoned
    finally:
        client.stop()


def test_unconfirmed_append_ack_poison_suppresses_original_login_flow(monkeypatch):
    class _BadAckSink(_Sink):
        def append(self, record):
            self.records.append(record)
            return CtpTraderCallbackIngressAckV2(
                owner_id=record.owner_intent_id,
                sequence=record.source_sequence,
                digest=record.canonical_sha256,
                commit_state="COMMITTED",
                high_watermark=record.source_sequence + 1,
            )

    sink = _BadAckSink()
    client, api, _sink, _owner = _new_active_client(
        monkeypatch, sink=sink, expect_active=False
    )
    try:
        assert client._callback_ingress.phase == "POISONED"
        assert client._callback_ingress.poison_reason == "append_commit_unknown"
        assert [record.callback_name for record in sink.records] == ["OnFrontConnected"]
        assert not any(
            call[0] in {"ReqAuthenticate", "ReqUserLogin"} for call in api.calls
        )
        assert sink.poisons and sink.poisons[-1][1] == "append_commit_unknown"
    finally:
        client.stop()


def test_stop_before_native_api_creation_permanently_fences_installed_owner(
    monkeypatch,
):
    _FakeTraderApiFactory.instances = []
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "fake-flow")
    monkeypatch.setattr(client_module, "_register_ctp_native_api", lambda _api: None)
    monkeypatch.setattr(client_module, "CThostFtdcTraderApi", _FakeTraderApiFactory)
    client = TraderClient("tcp://fake", "9999", "investor-1", "not-a-real-secret")
    sink = _Sink()
    owner = _OwnerIntent()
    client.install_callback_ingress_sink(
        owner,
        sink,
        _session_binder,
        lambda _owner, binding: binding,
    )

    client.stop()

    assert client._callback_ingress.poisoned
    assert client._callback_ingress.poison_reason == "owner_stop"
    with pytest.raises(CtpExecutionGateError, match="not_startable"):
        client.start()
    assert _FakeTraderApiFactory.instances == []
    assert sink.poisons and sink.poisons[-1][1] == "owner_stop"


def test_binding_rejects_bool_writer_token_before_lease_or_native_call(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = _request_payload()
        binding = _binding(owner, client._callback_ingress.active_session, payload)
        binding.envelope["writer_fencing_token"] = True

        with pytest.raises(CtpExecutionGateError, match="binding_integer_invalid"):
            client.acquire_managed_native_call_lease(owner, binding)

        assert not any(call[0] == "ReqOrderInsert" for call in api.calls)
        assert client._callback_ingress.poisoned
        assert client._callback_ingress.poison_reason == "native_call_lease_failure"
    finally:
        client.stop()


def test_api_replacement_retains_joined_spi_and_ignores_late_callback(monkeypatch):
    client, old_api, _sink, _owner = _new_active_client(monkeypatch, block_join=True)
    old_spi = old_api.spi
    sequence = client._callback_ingress.sequence
    replacement_api = _FakeTraderApi()
    try:
        client._api = replacement_api

        assert client._callback_ingress.poisoned
        assert client._callback_ingress.poison_reason == "source_replaced"
        assert client._api is replacement_api
        assert client._spi is None
        with client_module._RETIRED_CTP_NATIVE_SESSIONS_LOCK:
            assert any(
                api is old_api and spi is old_spi
                for api, spi, _join_thread in client_module._RETIRED_CTP_NATIVE_SESSIONS
            )

        old_spi.OnHeartBeatWarning(1)
        assert client._callback_ingress.sequence == sequence
        assert not old_api.release_completed.is_set()

        old_api.join_release.set()
        join_result = client.wait_native_join(2)
        assert join_result.observation.state == "returned"
        assert old_api.release_completed.wait(2)
    finally:
        old_api.join_release.set()
        client.stop()


@pytest.mark.parametrize("failure_point", ["registry", "register", "init"])
def test_startup_baseexception_releases_references_and_keeps_owner_poisoned(
    monkeypatch, failure_point
):
    _FakeTraderApiFactory.instances = []
    _FakeTraderApiFactory.block_join = False
    _FakeTraderApiFactory.register_spi_exception = (
        KeyboardInterrupt("fake RegisterSpi interrupt")
        if failure_point == "register"
        else None
    )
    _FakeTraderApiFactory.init_exception = (
        KeyboardInterrupt("fake Init interrupt") if failure_point == "init" else None
    )
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "fake-flow")
    if failure_point == "registry":

        def _interrupt_registry(_api):
            raise KeyboardInterrupt("fake registry interrupt")

        monkeypatch.setattr(
            client_module,
            "_register_ctp_native_api",
            _interrupt_registry,
        )
    else:
        monkeypatch.setattr(
            client_module, "_register_ctp_native_api", lambda _api: None
        )
    monkeypatch.setattr(client_module, "CThostFtdcTraderApi", _FakeTraderApiFactory)
    client = TraderClient("tcp://fake", "9999", "investor-1", "not-a-real-secret")
    owner = _OwnerIntent()
    client.install_callback_ingress_sink(
        owner, _Sink(), _session_binder, lambda _owner, binding: binding
    )

    try:
        with pytest.raises(KeyboardInterrupt):
            client.start()

        api = _FakeTraderApiFactory.instances[-1]
        assert client._native_request_inflight_refs == 0
        assert client._callback_ingress.native_call_refs == 0
        assert client._callback_ingress.poisoned
        if failure_point == "init":
            assert api.join_started.wait(1)
        assert api.release_completed.wait(2)
    finally:
        client.stop()


@pytest.mark.parametrize("native_result", [None, False, 0.0, -1, 1])
@pytest.mark.parametrize("operation", ["SUBMIT", "CANCEL"])
def test_managed_native_req_accepts_only_exact_integer_zero(
    native_result, operation, monkeypatch
):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        if operation == "SUBMIT":
            api.order_insert_result = native_result
            payload = _request_payload()
            binding = _binding(owner, client._callback_ingress.active_session, payload)
            field_type = CThostFtdcInputOrderField
            submit = client.submit_order_insert_with_lease
            method_name = "ReqOrderInsert"
        else:
            api.order_action_result = native_result
            payload = {
                "InstrumentID": "rb2610",
                "OrderRef": "000000000017",
                "ExchangeID": "SHFE",
                "OrderSysID": "sys-17",
                "FrontID": 7,
                "SessionID": 19,
                "ActionFlag": "0",
                "LimitPrice": 0.0,
                "VolumeChange": 0,
            }
            binding = _binding(
                owner,
                client._callback_ingress.active_session,
                payload,
                operation="CANCEL",
                action_ref=73,
                request_id=41,
            )
            field_type = CThostFtdcInputOrderActionField
            submit = client.submit_order_action_with_lease
            method_name = "ReqOrderAction"
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field_extras = (
            {"RequestID": 41, "OrderActionRef": 73}
            if operation == "CANCEL"
            else {}
        )
        field = _field(field_type, payload, **field_extras)

        with pytest.raises(CtpExecutionGateError, match="native_call_result_ambiguous"):
            submit(lease, field, 41)

        assert sum(call[0] == method_name for call in api.calls) == 1
        assert client._callback_ingress.poisoned
        assert client._callback_ingress.native_call_refs == 0
        assert client._native_request_inflight_refs == 0
    finally:
        client.stop()


def test_managed_native_req_baseexception_releases_lease_references(monkeypatch):
    client, api, _sink, owner = _new_active_client(monkeypatch)
    try:
        payload = _request_payload()
        binding = _binding(owner, client._callback_ingress.active_session, payload)
        lease = client.acquire_managed_native_call_lease(owner, binding)
        api.order_insert_exception = KeyboardInterrupt("fake ReqOrderInsert interrupt")
        field = _field(CThostFtdcInputOrderField, payload)

        with pytest.raises(KeyboardInterrupt):
            client.submit_order_insert_with_lease(lease, field, 41)

        assert client._callback_ingress.poisoned
        assert client._callback_ingress.native_call_refs == 0
        assert client._native_request_inflight_refs == 0
        assert not client._managed_native_call_leases
    finally:
        client.stop()


@pytest.mark.parametrize("native_result", [None, False, 0.0])
def test_generic_managed_req_rejects_non_integer_result(native_result, monkeypatch):
    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        api.query_result = native_result
        with pytest.raises(
            CtpExecutionGateError, match="native_request_result_invalid"
        ):
            client._invoke_session_native_request(
                api,
                "ReqQryTradingAccount",
                SimpleNamespace(),
                99,
            )

        assert sum(call[0] == "ReqQryTradingAccount" for call in api.calls) == 1
        assert client._callback_ingress.poisoned
        assert client._callback_ingress.native_call_refs == 0
        assert client._native_request_inflight_refs == 0
    finally:
        client.stop()


class _FakeNativeQueryField:
    """Attribute-backed stand-in for one native request struct."""


def _query_callback_row(callback_name, **overrides):
    safe_fields = dict(CALLBACK_SPECS[callback_name].safe_fields_by_slot)[0]
    defaults = {"string": "", "char": "1", "integer": 0, "float": 0.0}
    values = {name: defaults[SAFE_FIELD_KINDS[name]] for name in safe_fields}
    values.update(overrides)
    return SimpleNamespace(**values)


def _query_callback_info(error_id=0):
    return SimpleNamespace(ErrorID=error_id, ErrorMsg="not captured by safe ingress")


def _install_query_field_fakes(monkeypatch):
    monkeypatch.setattr(client_module, "CThostFtdcQryProductField", _FakeNativeQueryField)
    monkeypatch.setattr(client_module, "CThostFtdcQryInstrumentField", _FakeNativeQueryField)
    monkeypatch.setattr(
        client_module, "CThostFtdcQryDepthMarketDataField", _FakeNativeQueryField
    )


def test_query_products_uses_real_spi_ingress_and_preserves_exact_filter_case(monkeypatch):
    _install_query_field_fakes(monkeypatch)
    client, api, sink, _owner = _new_active_client(monkeypatch)
    try:
        callback_request_ids = []

        def respond(_field, request_id):
            callback_request_ids.append(request_id)
            api.spi.OnRspQryProduct(
                _query_callback_row(
                    "OnRspQryProduct",
                    ProductClass="1",
                    ExchangeID="DCE",
                    ProductID="i",
                ),
                _query_callback_info(),
                request_id,
                False,
            )
            api.spi.OnRspQryProduct(
                None, _query_callback_info(), request_id, True
            )

        api.query_callbacks["ReqQryProduct"] = respond
        result = client.query_products_result(
            product_class="1",
            exchange_id="DCE",
            product_id="i",
            timeout=0.5,
            max_duration=1.0,
            max_records=2,
        )

        request_calls = [call for call in api.calls if call[0] == "ReqQryProduct"]
        assert len(request_calls) == 1
        request_field, request_id = request_calls[0][1:]
        assert request_field.ProductClass == "1"
        assert request_field.ExchangeID == "DCE"
        assert request_field.ProductID == "i"
        assert callback_request_ids == [request_id]
        assert result.request_type == "products"
        assert result.request_id == request_id
        assert result.complete
        assert [row["ProductID"] for row in result.records] == ["i"]
        assert result.query_source.request_filters == (
            ("ExchangeID", "DCE"),
            ("ProductClass", "1"),
            ("ProductID", "i"),
        )
        product_callbacks = [
            record for record in sink.records if record.callback_name == "OnRspQryProduct"
        ]
        assert [record.scalar_argument(3) for record in product_callbacks] == [False, True]
        assert all(record.capture_complete for record in product_callbacks)
        product_id_value = product_callbacks[0].field_value(0, "ProductID")
        assert product_id_value.present and product_id_value.value == "i"
        assert product_callbacks[0].field_value(1, "ErrorMsg") is None
    finally:
        client.stop()


def test_query_products_empty_filters_accept_a_complete_empty_terminal_result(monkeypatch):
    _install_query_field_fakes(monkeypatch)
    client, api, sink, _owner = _new_active_client(monkeypatch)
    try:
        api.query_callbacks["ReqQryProduct"] = lambda _field, request_id: api.spi.OnRspQryProduct(
            None, _query_callback_info(), request_id, True
        )

        result = client.query_products_result(timeout=0.5)

        request_calls = [call for call in api.calls if call[0] == "ReqQryProduct"]
        assert len(request_calls) == 1
        request_field = request_calls[0][1]
        assert request_field.ProductClass == ""
        assert request_field.ExchangeID == ""
        assert request_field.ProductID == ""
        assert result.complete
        assert result.records == ()
        assert result.query_source.request_filters == (
            ("ExchangeID", ""),
            ("ProductClass", ""),
            ("ProductID", ""),
        )
        product_callback = next(
            record for record in reversed(sink.records) if record.callback_name == "OnRspQryProduct"
        )
        assert product_callback.capture_complete
        assert product_callback.named_args[0].pointer_is_null
    finally:
        client.stop()


def test_query_products_empty_char_filter_uses_real_swig_field_and_fake_req(monkeypatch):
    """Keep the SWIG char encoding local while exercising the fake native Req."""
    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        api.query_callbacks["ReqQryProduct"] = lambda _field, request_id: api.spi.OnRspQryProduct(
            None, _query_callback_info(), request_id, True
        )

        result = client.query_products_result(timeout=0.5)

        request_calls = [call for call in api.calls if call[0] == "ReqQryProduct"]
        assert len(request_calls) == 1
        request_field = request_calls[0][1]
        assert type(request_field) is client_module.CThostFtdcQryProductField
        # CThostFtdcQryProductField.ProductClass is a CTP char field, so the
        # actual SWIG setter represents an empty assignment with NUL.
        assert request_field.ProductClass == "\x00"
        assert result.complete
        assert result.query_source.request_intent_filters == (
            ("ExchangeID", ""),
            ("ProductClass", ""),
            ("ProductID", ""),
        )
        assert result.query_source.request_filters == result.query_source.request_intent_filters
    finally:
        client.stop()


def test_product_instrument_and_depth_filters_read_back_from_real_request_structs(
    monkeypatch,
):
    """Exercise the actual SWIG request fields while all API calls stay fake."""
    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        api.query_callbacks["ReqQryProduct"] = lambda _field, request_id: api.spi.OnRspQryProduct(
            None, _query_callback_info(), request_id, True
        )
        api.query_callbacks["ReqQryInstrument"] = (
            lambda _field, request_id: api.spi.OnRspQryInstrument(
                None, _query_callback_info(), request_id, True
            )
        )
        api.query_callbacks["ReqQryDepthMarketData"] = (
            lambda _field, request_id: api.spi.OnRspQryDepthMarketData(
                None, _query_callback_info(), request_id, True
            )
        )

        products = client.query_products_result(
            product_class="1", exchange_id="DCE", product_id="i", timeout=0.5
        )
        instruments = client.query_instruments_result(
            instrument_id="i2710",
            exchange_id="DCE",
            product_id="i",
            timeout=0.5,
        )
        depth = client.query_depth_market_data_result(
            instrument_id="i2710",
            exchange_id="DCE",
            product_class="1",
            timeout=0.5,
        )

        requests = {
            call[0]: call[1]
            for call in api.calls
            if call[0]
            in {"ReqQryProduct", "ReqQryInstrument", "ReqQryDepthMarketData"}
        }
        assert (
            type(requests["ReqQryProduct"])
            is client_module.CThostFtdcQryProductField
        )
        assert requests["ReqQryProduct"].ProductClass == "1"
        assert requests["ReqQryProduct"].ExchangeID == "DCE"
        assert requests["ReqQryProduct"].ProductID == "i"
        assert products.complete
        assert products.query_source.request_filters == (
            ("ExchangeID", "DCE"),
            ("ProductClass", "1"),
            ("ProductID", "i"),
        )

        assert (
            type(requests["ReqQryInstrument"])
            is client_module.CThostFtdcQryInstrumentField
        )
        assert requests["ReqQryInstrument"].InstrumentID == "i2710"
        assert requests["ReqQryInstrument"].ExchangeID == "DCE"
        assert requests["ReqQryInstrument"].ProductID == "i"
        assert instruments.complete
        assert instruments.query_source.request_filters == (
            ("ExchangeID", "DCE"),
            ("InstrumentID", "i2710"),
            ("ProductID", "i"),
        )

        assert (
            type(requests["ReqQryDepthMarketData"])
            is client_module.CThostFtdcQryDepthMarketDataField
        )
        assert requests["ReqQryDepthMarketData"].InstrumentID == "i2710"
        assert requests["ReqQryDepthMarketData"].ExchangeID == "DCE"
        assert requests["ReqQryDepthMarketData"].ProductClass == "1"
        assert depth.complete
        assert depth.query_source.request_filters == (
            ("ExchangeID", "DCE"),
            ("InstrumentID", "i2710"),
            ("ProductClass", "1"),
        )
    finally:
        client.stop()


def test_query_products_isolates_request_ids_and_counts_late_rows(monkeypatch):
    _install_query_field_fakes(monkeypatch)
    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        first_ids = []
        second_call = False

        def respond(_field, request_id):
            nonlocal second_call
            if not second_call:
                first_ids.append(request_id)
                api.spi.OnRspQryProduct(
                    _query_callback_row("OnRspQryProduct", ProductID="old"),
                    _query_callback_info(),
                    request_id,
                    True,
                )
                second_call = True
                return
            api.spi.OnRspQryProduct(
                _query_callback_row("OnRspQryProduct", ProductID="late"),
                _query_callback_info(),
                first_ids[0],
                False,
            )
            api.spi.OnRspQryProduct(
                _query_callback_row("OnRspQryProduct", ProductID="MA"),
                _query_callback_info(),
                request_id,
                True,
            )

        api.query_callbacks["ReqQryProduct"] = respond
        first = client.query_products_result(timeout=0.5)
        second = client.query_products_result(product_id="MA", timeout=0.5)

        assert first.request_id != second.request_id
        assert [row["ProductID"] for row in second.records] == ["MA"]
        assert second.query_source.request_filters[-1] == ("ProductID", "MA")
        first_current = client.get_query_result(first.request_id)
        assert first_current is not None
        assert first_current.late_callback_count == 1
        assert sum(call[0] == "ReqQryProduct" for call in api.calls) == 2
    finally:
        client.stop()


def test_query_products_error_is_incomplete_and_error_message_is_not_ingress_captured(
    monkeypatch,
):
    _install_query_field_fakes(monkeypatch)
    client, api, sink, _owner = _new_active_client(monkeypatch)
    try:
        api.query_callbacks["ReqQryProduct"] = lambda _field, request_id: api.spi.OnRspQryProduct(
            None, _query_callback_info(73), request_id, True
        )

        result = client.query_products_result(timeout=0.5)

        assert not result.complete
        assert result.error_code == 73
        assert result.completion_origin == "query_response"
        assert result.query_source is not None
        callback = next(
            record for record in reversed(sink.records) if record.callback_name == "OnRspQryProduct"
        )
        error_id = callback.field_value(1, "ErrorID")
        assert error_id.present and error_id.value == 73
        assert callback.field_value(1, "ErrorMsg") is None
    finally:
        client.stop()


@pytest.mark.parametrize(
    ("request_type", "request_method", "callback_method", "query_kwargs", "row_values"),
    [
        (
            "products",
            "ReqQryProduct",
            "OnRspQryProduct",
            {"product_id": "MA"},
            {"ProductID": "MA"},
        ),
        (
            "instruments",
            "ReqQryInstrument",
            "OnRspQryInstrument",
            {"exchange_id": "SHFE", "product_id": "rb"},
            {"InstrumentID": "rb2710", "ExchangeID": "SHFE", "ProductID": "rb"},
        ),
        (
            "depth_market_data",
            "ReqQryDepthMarketData",
            "OnRspQryDepthMarketData",
            {"instrument_id": "MA2610", "exchange_id": "CZCE", "product_class": "1"},
            {"InstrumentID": "MA2610", "ExchangeID": "CZCE"},
        ),
    ],
)
def test_catalog_query_caps_stop_before_appending_overflow_row(
    request_type,
    request_method,
    callback_method,
    query_kwargs,
    row_values,
    monkeypatch,
):
    _install_query_field_fakes(monkeypatch)
    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        rows_sent = []

        def respond(_field, request_id):
            for index in range(4):
                values = dict(row_values)
                if request_type == "instruments":
                    values["InstrumentID"] = f"rb{2710 + index}"
                elif request_type == "depth_market_data":
                    values["LastPrice"] = float(3000 + index)
                else:
                    values["ProductID"] = "MA" if index == 0 else f"p{index}"
                if index >= 2:
                    values["UnboundedPayload"] = f"overflow-{index}"
                rows_sent.append(values)
                getattr(api.spi, callback_method)(
                    _query_callback_row(callback_method, **values),
                    _query_callback_info(),
                    request_id,
                    index == 2,
                )

        api.query_callbacks[request_method] = respond
        result = getattr(client, f"query_{request_type}_result")(
            **query_kwargs,
            timeout=0.5,
            max_records=2,
        )

        assert len(rows_sent) == 4
        assert rows_sent[0] != rows_sent[1]
        assert rows_sent[1] != rows_sent[2]
        assert rows_sent[2] != rows_sent[3]
        assert len([call for call in api.calls if call[0] == request_method]) == 1
        assert len(result.records) == 2
        if request_type == "products":
            assert [row["ProductID"] for row in result.records] == ["MA", "p1"]
        elif request_type == "instruments":
            assert [row["InstrumentID"] for row in result.records] == [
                "rb2710",
                "rb2711",
            ]
        else:
            assert [row["LastPrice"] for row in result.records] == [3000.0, 3001.0]
        assert all("UnboundedPayload" not in row for row in result.records)
        assert result.record_limit == 2
        assert result.record_limit_exceeded is True
        assert result.completion_origin == "record_limit"
        assert result.first_completion_origin == "record_limit"
        assert result.error_message == "query_record_limit_exceeded"
        assert result.is_last_seen
        assert not result.complete
        assert result.query_source.record_limit == 2
        assert result.query_source.record_limit_exceeded is True
        assert result.late_callback_count == 1
        serialized = result.as_dict()
        assert serialized["record_limit"] == 2
        assert serialized["record_limit_exceeded"] is True
        assert serialized["evidence_complete"] is False
        # The first over-limit callback is captured as the bounded rejection
        # witness; the later callback is counted but not copied into ingress.
        assert sum(record.callback_name == callback_method for record in _sink.records) == 3
        overflow_witness = next(
            record
            for record in reversed(_sink.records)
            if record.callback_name == callback_method
        )
        assert overflow_witness.capture_complete
        assert overflow_witness.field_value(0, "UnboundedPayload") is None
        if request_type == "instruments":
            assert result.query_source.request_filters == (
                ("ExchangeID", "SHFE"),
                ("InstrumentID", ""),
                ("ProductID", "rb"),
            )
        elif request_type == "products":
            assert result.query_source.request_filters[-1] == ("ProductID", "MA")
        else:
            assert ("ProductClass", "1") in result.query_source.request_filters
    finally:
        client.stop()


def test_record_limit_ingress_reservation_is_bounded_under_concurrent_callbacks(
    monkeypatch,
):
    _install_query_field_fakes(monkeypatch)
    handler_entered = threading.Event()
    release_handler = threading.Event()
    late_callbacks_waiting = threading.Event()
    callback_errors = []
    callback_threads = []
    errors_lock = threading.Lock()

    class _ObservedRLock:
        """Expose that every overflow callback reached the per-query gate."""

        def __init__(self):
            self._lock = threading.RLock()
            self._state_lock = threading.Lock()
            self._waiting = 0

        def acquire(self):
            if self._lock.acquire(blocking=False):
                return True
            with self._state_lock:
                self._waiting += 1
                if self._waiting == 3:
                    late_callbacks_waiting.set()
            try:
                return self._lock.acquire()
            finally:
                with self._state_lock:
                    self._waiting -= 1

        def release(self):
            self._lock.release()

    dispatch_lock = _ObservedRLock()
    original_handler = TraderClient._handle_query_callback

    def delayed_handler(
        self, request_type, record, rsp_info, request_id, is_last, **kwargs
    ):
        if request_type == "products" and getattr(record, "ProductID", None) == "p1":
            handler_entered.set()
            if not release_handler.wait(5):
                with errors_lock:
                    callback_errors.append(AssertionError("delayed capped handler timed out"))
        return original_handler(
            self, request_type, record, rsp_info, request_id, is_last, **kwargs
        )

    monkeypatch.setattr(TraderClient, "_handle_query_callback", delayed_handler)
    client, api, sink, _owner = _new_active_client(monkeypatch)
    query_results = []
    query_errors = []

    def send_row(product_id, is_last=False):
        try:
            api.spi.OnRspQryProduct(
                _query_callback_row(
                    "OnRspQryProduct",
                    ProductClass="1",
                    ExchangeID="DCE",
                    ProductID=product_id,
                    UnboundedPayload=f"payload-{product_id}",
                ),
                _query_callback_info(),
                request_id,
                is_last,
            )
        except BaseException as exc:
            with errors_lock:
                callback_errors.append(exc)

    def respond(_field, current_request_id):
        nonlocal request_id
        request_id = current_request_id
        send_row("p0")
        with client._query_state_lock:
            accumulator = client._query_history[current_request_id]
            accumulator.callback_dispatch_lock = dispatch_lock

        first_row = threading.Thread(target=send_row, args=("p1",))
        callback_threads.append(first_row)
        first_row.start()
        if not handler_entered.wait(4):
            with errors_lock:
                callback_errors.append(AssertionError("N-th handler did not pause"))
            release_handler.set()
            return

        start_together = threading.Barrier(4)

        def concurrent_row(product_id, is_last):
            try:
                start_together.wait(timeout=3)
            except BaseException as exc:
                with errors_lock:
                    callback_errors.append(exc)
                return
            send_row(product_id, is_last=is_last)

        for product_id, is_last in (("p2", False), ("p3", False), ("p4", True)):
            thread = threading.Thread(
                target=concurrent_row,
                args=(product_id, is_last),
            )
            callback_threads.append(thread)
            thread.start()
        try:
            start_together.wait(timeout=3)
        except BaseException as exc:
            with errors_lock:
                callback_errors.append(exc)
            release_handler.set()
            return

        if not late_callbacks_waiting.wait(4):
            with errors_lock:
                callback_errors.append(
                    AssertionError("concurrent callbacks did not queue at the cap")
                )
        release_handler.set()
        for thread in callback_threads:
            thread.join(5)
            if thread.is_alive():
                with errors_lock:
                    callback_errors.append(AssertionError("callback thread did not finish"))

    api.query_callbacks["ReqQryProduct"] = respond
    request_id = None

    def run_query():
        try:
            query_results.append(
                client.query_products_result(timeout=0.5, max_records=2)
            )
        except BaseException as exc:
            query_errors.append(exc)

    query_thread = threading.Thread(target=run_query)
    query_thread.start()
    try:
        assert late_callbacks_waiting.wait(4), "concurrent callbacks never reached cap gate"
    finally:
        release_handler.set()
        query_thread.join(7)
        for thread in callback_threads:
            if thread.is_alive():
                thread.join(5)
        client.stop()

    assert not query_thread.is_alive()
    assert not any(thread.is_alive() for thread in callback_threads)
    assert not callback_errors
    assert not query_errors
    assert len(query_results) == 1
    result = query_results[0]
    assert [row["ProductID"] for row in result.records] == ["p0", "p1"]
    assert result.record_limit_exceeded is True
    assert result.completion_origin == "record_limit"
    assert not result.complete
    assert len(result.records) == 2
    assert result.late_callback_count == 2
    query_callback_records = [
        record for record in sink.records if record.callback_name == "OnRspQryProduct"
    ]
    assert len(query_callback_records) == 3
    assert all(record.capture_complete for record in query_callback_records)
    overflow_witness = query_callback_records[-1]
    assert overflow_witness.field_value(0, "UnboundedPayload") is None
    assert api.release_completed.wait(1)
    assert api.spi is None


def test_depth_product_class_is_forwarded_only_when_explicit(monkeypatch):
    _install_query_field_fakes(monkeypatch)
    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        api.query_callbacks["ReqQryDepthMarketData"] = (
            lambda _field, request_id: api.spi.OnRspQryDepthMarketData(
                None, _query_callback_info(), request_id, True
            )
        )

        scoped = client.query_depth_market_data_result(
            instrument_id="MA2610",
            exchange_id="CZCE",
            product_class="1",
            timeout=0.5,
            max_duration=1.0,
            max_records=4,
        )
        legacy = client.query_depth_market_data_result(timeout=0.5)

        request_calls = [call for call in api.calls if call[0] == "ReqQryDepthMarketData"]
        assert len(request_calls) == 2
        assert request_calls[0][1].ProductClass == "1"
        assert scoped.query_source.request_filters == (
            ("ExchangeID", "CZCE"),
            ("InstrumentID", "MA2610"),
            ("ProductClass", "1"),
        )
        assert not hasattr(request_calls[1][1], "ProductClass")
        assert legacy.query_source.request_filters == (("ExchangeID", ""), ("InstrumentID", ""))
    finally:
        client.stop()


def test_product_query_deadline_and_record_limit_validation_happen_before_req(monkeypatch):
    _install_query_field_fakes(monkeypatch)
    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        expired = client.query_products_result(deadline_monotonic=time.monotonic() - 1)
        assert expired.completion_origin == "deadline"
        assert not expired.complete
        assert not any(call[0] == "ReqQryProduct" for call in api.calls)

        for invalid in (True, 0, 100_001):
            with pytest.raises(ValueError, match="max_records"):
                client.query_products_result(max_records=invalid)
        cancelled = client.query_products_result(should_stop=lambda: True)
        assert cancelled.completion_origin == "local_cancel"
        assert cancelled.error_message == "query_cancelled"
        assert not cancelled.complete
        assert not any(call[0] == "ReqQryProduct" for call in api.calls)
    finally:
        client.stop()
