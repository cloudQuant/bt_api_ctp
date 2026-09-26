"""Fake-only integration tests for TraderClient's durable callback owner seam."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

import bt_api_ctp.ctp.client as client_module
from bt_api_ctp.ctp.callback_ingress import CtpTraderCallbackIngressAckV2
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
):
    _FakeTraderApiFactory.instances = []
    _FakeTraderApiFactory.block_join = block_join
    _FakeTraderApiFactory.register_spi_exception = None
    _FakeTraderApiFactory.init_exception = None
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "fake-flow")
    monkeypatch.setattr(client_module, "_register_ctp_native_api", lambda _api: None)
    monkeypatch.setattr(client_module, "CThostFtdcTraderApi", _FakeTraderApiFactory)

    client = TraderClient("tcp://fake", "9999", "investor-1", "not-a-real-secret")
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
    envelope = {
        "binding_type": "ctp_managed_native_call_binding.v1",
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

    class CtpManagedNativeCallBindingV1:
        def __init__(self):
            self.envelope = dict(envelope)

        def to_payload(self):
            return dict(self.envelope)

    return CtpManagedNativeCallBindingV1()


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
            action_ref="cancel-action-8",
            request_id=53,
        )
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = _field(CThostFtdcInputOrderActionField, payload)

        assert client.submit_order_action_with_lease(lease, field, 53) == 0
        calls = [call for call in api.calls if call[0] == "ReqOrderAction"]
        assert len(calls) == 1
        _name, detached, request_id = calls[0]
        assert request_id == detached.RequestID == 53
        assert detached.OrderActionRef == "cancel-action-8"
        assert detached.OrderActionRef != str(detached.RequestID)
        assert detached is not field
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
                action_ref="managed-action-ref-1",
            )
            field_type = CThostFtdcInputOrderActionField
            submit = client.submit_order_action_with_lease
            method_name = "ReqOrderAction"
        lease = client.acquire_managed_native_call_lease(owner, binding)
        field = _field(field_type, payload)

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
