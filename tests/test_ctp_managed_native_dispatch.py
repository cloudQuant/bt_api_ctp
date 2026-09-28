from __future__ import annotations

import hashlib
from threading import RLock
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp import ctp_structs_order
from bt_api_ctp.ctp import managed_native_dispatch as adapter
from bt_api_ctp.ctp.managed_dispatch import CtpManagedNativeDispatchRequestV1

SCOPE = "1" * 64
PARENT_HANDOFF = "3" * 64
ORDER_REF = "000000001051"
TRADING_DAY = "20260925"
CONNECTION_GENERATION = 9
RUNTIME_ORDER_ID = "bt-managed-v1:" + "a" * 64
BROKER_ID = "3070"
INVESTOR_ID = "offline-account"
SDK_ACCOUNT_FINGERPRINT = hashlib.sha256(f"{BROKER_ID}:{INVESTOR_ID}".encode()).hexdigest()[:16]
ACCOUNT_FINGERPRINT_SHA256 = hashlib.sha256(
    f"acct_{SDK_ACCOUNT_FINGERPRINT}".encode("ascii")
).hexdigest()


def _insert_fields(**changes):
    fields = {
        "BrokerID": BROKER_ID,
        "InvestorID": INVESTOR_ID,
        "UserID": INVESTOR_ID,
        "InstrumentID": "SA609",
        "ExchangeID": "CZCE",
        "OrderRef": ORDER_REF,
        "RequestID": 701,
        "Direction": "0",
        "CombOffsetFlag": "0",
        "CombHedgeFlag": "2",
        "LimitPrice": 1500.0,
        "VolumeTotalOriginal": 1,
        "OrderPriceType": "2",
        "TimeCondition": "3",
        "VolumeCondition": "1",
    }
    fields.update(changes)
    return fields


def _cancel_fields(**changes):
    fields = {
        "BrokerID": BROKER_ID,
        "InvestorID": INVESTOR_ID,
        "InstrumentID": "SA609",
        "ExchangeID": "CZCE",
        "OrderRef": ORDER_REF,
        "OrderSysID": "SYS-1051",
        "FrontID": 17,
        "SessionID": 19,
        "RequestID": 702,
        "OrderActionRef": 702,
        "ActionFlag": "0",
    }
    fields.update(changes)
    return fields


def _insert_request(fields=None):
    fields = fields or _insert_fields()
    return CtpManagedNativeDispatchRequestV1.from_native_fields(
        operation="insert",
        scope_digest_sha256=SCOPE,
        account_fingerprint_sha256=ACCOUNT_FINGERPRINT_SHA256,
        parent_handoff_digest_sha256=PARENT_HANDOFF,
        managed_intent_id="managed-entry-1051",
        runtime_order_id=RUNTIME_ORDER_ID,
        order_ref=ORDER_REF,
        request_id=701,
        trading_day=TRADING_DAY,
        connection_generation=CONNECTION_GENERATION,
        native_fields=fields,
    )


def _cancel_request(fields=None):
    fields = fields or _cancel_fields()
    return CtpManagedNativeDispatchRequestV1.from_native_fields(
        operation="cancel",
        scope_digest_sha256=SCOPE,
        account_fingerprint_sha256=ACCOUNT_FINGERPRINT_SHA256,
        parent_handoff_digest_sha256=PARENT_HANDOFF,
        managed_intent_id="managed-entry-1051",
        runtime_order_id=RUNTIME_ORDER_ID,
        order_ref=ORDER_REF,
        request_id=702,
        trading_day=TRADING_DAY,
        connection_generation=CONNECTION_GENERATION,
        runtime_action_id="managed-cancel-1051-a1",
        managed_cancel_intent_id="managed-cancel-1051-a1",
        target_order_sys_id="SYS-1051",
        target_front_id=17,
        target_session_id=19,
        target_exchange_id="CZCE",
        native_fields=fields,
    )


class _FakeManagedTrader:
    def __init__(self, *, submit_code=0, raise_on_submit=False):
        self._query_state_lock = RLock()
        self._account_fingerprint = SDK_ACCOUNT_FINGERPRINT
        self._trading_day = TRADING_DAY
        self._connection_generation = CONNECTION_GENERATION
        self.submit_code = submit_code
        self.raise_on_submit = raise_on_submit
        self.calls = []
        self.insert_evidence = None
        self.cancel_evidence = None

    def submit_order_insert(
        self,
        field,
        request_id,
        *,
        execution_capability,
        runtime_order_id,
        managed_intent_id,
    ):
        lock_owned = self._query_state_lock._is_owned()
        self.calls.append(
            {
                "operation": "insert",
                "fields": vars(field).copy(),
                "request_id": request_id,
                "execution_capability": execution_capability,
                "runtime_order_id": runtime_order_id,
                "managed_intent_id": managed_intent_id,
                "adapter_lock_owned": lock_owned,
            }
        )
        self.insert_evidence = SimpleNamespace(
            request_id=request_id,
            account_fingerprint=f"acct_{self._account_fingerprint}",
            trading_day=self._trading_day,
            connection_generation=self._connection_generation,
            order_ref=field.OrderRef,
            instrument_id=field.InstrumentID,
            exchange_id=field.ExchangeID,
            callback_received=False,
            evidence_received=False,
            status="unknown",
        )
        if self.raise_on_submit:
            raise RuntimeError("fake native failure")
        return self.submit_code

    def submit_order_action(
        self,
        field,
        request_id,
        *,
        execution_capability,
        runtime_order_id,
        managed_intent_id,
        runtime_action_id,
        managed_cancel_intent_id,
    ):
        lock_owned = self._query_state_lock._is_owned()
        self.calls.append(
            {
                "operation": "cancel",
                "fields": vars(field).copy(),
                "request_id": request_id,
                "execution_capability": execution_capability,
                "runtime_order_id": runtime_order_id,
                "managed_intent_id": managed_intent_id,
                "runtime_action_id": runtime_action_id,
                "managed_cancel_intent_id": managed_cancel_intent_id,
                "adapter_lock_owned": lock_owned,
            }
        )
        self.cancel_evidence = SimpleNamespace(
            request_id=request_id,
            account_fingerprint=f"acct_{self._account_fingerprint}",
            trading_day=self._trading_day,
            connection_generation=self._connection_generation,
            order_ref=field.OrderRef,
            order_sys_id=field.OrderSysID,
            front_id=field.FrontID,
            session_id=field.SessionID,
            instrument_id=field.InstrumentID,
            exchange_id=field.ExchangeID,
            order_action_ref=str(field.OrderActionRef),
            action_flag=field.ActionFlag,
            callback_received=False,
            evidence_received=False,
            status="unknown",
        )
        if self.raise_on_submit:
            raise RuntimeError("fake native failure")
        return self.submit_code

    def get_order_insert_evidence(self, request_id, *, order_ref):
        evidence = self.insert_evidence
        if evidence is None or evidence.request_id != request_id or evidence.order_ref != order_ref:
            return None
        return evidence

    def get_order_action_evidence(self, request_id, *, order_action_ref):
        evidence = self.cancel_evidence
        if (
            evidence is None
            or evidence.request_id != request_id
            or evidence.order_action_ref != order_action_ref
        ):
            return None
        return evidence


@pytest.fixture(autouse=True)
def _fake_native_struct_builder(monkeypatch):
    insert_type = type(
        "FakeInputOrderField",
        (SimpleNamespace,),
        {**{name: None for name in _insert_fields()}, "UnspecifiedFlag": 0},
    )
    cancel_type = type(
        "FakeInputOrderActionField",
        (SimpleNamespace,),
        {
            **{name: None for name in _cancel_fields()},
            "LimitPrice": 0.0,
            "VolumeChange": 0,
            "UnspecifiedFlag": 0,
        },
    )
    monkeypatch.setattr(ctp_structs_order, "CThostFtdcInputOrderField", insert_type)
    monkeypatch.setattr(
        ctp_structs_order,
        "CThostFtdcInputOrderActionField",
        cancel_type,
    )


def test_insert_preserves_managed_ids_order_ref_parent_digest_and_local_queue_only():
    trader = _FakeManagedTrader()
    capability = object()
    request = _insert_request()

    completion = adapter.dispatch_managed_order_insert(
        trader,
        request,
        _insert_fields(),
        execution_capability=capability,
    )

    call = trader.calls[0]
    assert call["operation"] == "insert"
    assert call["request_id"] == request.request_id
    assert call["execution_capability"] is capability
    assert call["runtime_order_id"] == request.runtime_order_id
    assert call["managed_intent_id"] == request.managed_intent_id
    assert call["adapter_lock_owned"] is False
    assert call["fields"]["OrderRef"] == "000000001051"
    assert len(call["fields"]["OrderRef"]) == 12
    assert completion.request is request
    assert completion.echoed_request is request
    assert completion.request.parent_handoff_digest_sha256 == PARENT_HANDOFF
    assert completion.identity_matches
    assert completion.dispatch_status == "LOCAL_QUEUED"
    assert completion.provider_ack is False


def test_cancel_preserves_exact_action_and_native_target_through_receipt():
    trader = _FakeManagedTrader()
    capability = object()
    fields = _cancel_fields(LimitPrice=0.0, VolumeChange=0)
    request = _cancel_request(fields)

    completion = adapter.dispatch_managed_order_cancel(
        trader,
        request,
        fields,
        execution_capability=capability,
    )

    call = trader.calls[0]
    assert call["operation"] == "cancel"
    assert call["request_id"] == request.request_id
    assert call["execution_capability"] is capability
    assert call["runtime_order_id"] == request.runtime_order_id
    assert call["managed_intent_id"] == request.managed_intent_id
    assert call["runtime_action_id"] == request.runtime_action_id
    assert call["managed_cancel_intent_id"] == request.managed_cancel_intent_id
    assert call["adapter_lock_owned"] is False
    assert call["fields"]["OrderRef"] == ORDER_REF
    assert call["fields"]["OrderSysID"] == request.target_order_sys_id
    assert call["fields"]["FrontID"] == request.target_front_id == 17
    assert call["fields"]["SessionID"] == request.target_session_id == 19
    assert call["fields"]["ExchangeID"] == request.target_exchange_id == "CZCE"
    assert completion.request.runtime_action_id == "managed-cancel-1051-a1"
    assert completion.request.target_order_sys_id == "SYS-1051"
    assert completion.request.target_front_id == 17
    assert completion.request.target_session_id == 19
    assert completion.request.parent_handoff_digest_sha256 == PARENT_HANDOFF
    assert completion.dispatch_status == "LOCAL_QUEUED"
    assert completion.provider_ack is False


@pytest.mark.parametrize(
    "changes",
    [
        {"ActionFlag": "3"},
        {"LimitPrice": 1500.0},
        {"VolumeChange": 1},
    ],
)
def test_cancel_rejects_modify_action_and_fields_before_typed_submit(changes):
    invalid_fields = _cancel_fields(**changes)
    with pytest.raises(ValueError):
        _cancel_request(invalid_fields)

    trader = _FakeManagedTrader()
    request = _cancel_request()
    assert request.verifies_native_fields(invalid_fields) is False
    with pytest.raises(ValueError, match="do not match"):
        adapter.dispatch_managed_order_cancel(
            trader,
            request,
            invalid_fields,
            execution_capability=object(),
        )

    assert trader.calls == []


def test_cancel_rejects_missing_exact_target_without_current_session_fallback(monkeypatch):
    trader = _FakeManagedTrader()
    request = _cancel_request()
    object.__setattr__(request, "target_front_id", None)
    monkeypatch.setattr(
        CtpManagedNativeDispatchRequestV1,
        "verifies_native_fields",
        lambda _self, _fields: True,
    )

    with pytest.raises(ValueError, match="missing its exact native target"):
        adapter.dispatch_managed_order_cancel(
            trader,
            request,
            _cancel_fields(),
            execution_capability=object(),
        )

    assert trader.calls == []


def test_changed_native_fields_account_or_session_reject_before_dispatch():
    request = _insert_request()

    trader = _FakeManagedTrader()
    changed_fields = _insert_fields(LimitPrice=1501.0)
    with pytest.raises(ValueError, match="do not match"):
        adapter.dispatch_managed_order_insert(
            trader,
            request,
            changed_fields,
            execution_capability=object(),
        )
    assert trader.calls == []

    trader = _FakeManagedTrader()
    trader._connection_generation += 1
    with pytest.raises(ValueError, match="session"):
        adapter.dispatch_managed_order_insert(
            trader,
            request,
            _insert_fields(),
            execution_capability=object(),
        )
    assert trader.calls == []

    trader = _FakeManagedTrader()
    trader._account_fingerprint = "f" * 16
    with pytest.raises(ValueError, match="account"):
        adapter.dispatch_managed_order_insert(
            trader,
            request,
            _insert_fields(),
            execution_capability=object(),
        )
    assert trader.calls == []


def test_nonzero_unbound_native_struct_default_rejects_before_dispatch(monkeypatch):
    bad_insert_type = type(
        "FakeInputOrderFieldWithUnboundValue",
        (SimpleNamespace,),
        {**{name: None for name in _insert_fields()}, "UnspecifiedFlag": 1},
    )
    monkeypatch.setattr(ctp_structs_order, "CThostFtdcInputOrderField", bad_insert_type)
    trader = _FakeManagedTrader()

    with pytest.raises(ValueError, match="not zeroed defaults"):
        adapter.dispatch_managed_order_insert(
            trader,
            _insert_request(),
            _insert_fields(),
            execution_capability=object(),
        )

    assert trader.calls == []


def test_negative_native_code_without_complete_callback_history_stays_unknown():
    trader = _FakeManagedTrader(submit_code=-3)
    request = _insert_request()

    completion = adapter.dispatch_managed_order_insert(
        trader,
        request,
        _insert_fields(),
        execution_capability=object(),
    )

    assert completion.request_registered
    assert completion.submit_code == -3
    assert completion.callback_history_complete is False
    assert completion.dispatch_status == "UNKNOWN"
    assert completion.provider_ack is False


def test_zero_code_with_mismatched_registered_evidence_stays_unknown():
    trader = _FakeManagedTrader()
    request = _insert_request()
    read_exact_evidence = trader.get_order_insert_evidence

    def mismatched_evidence(request_id, *, order_ref):
        evidence = read_exact_evidence(request_id, order_ref=order_ref)
        evidence.request_id += 1
        return evidence

    trader.get_order_insert_evidence = mismatched_evidence
    completion = adapter.dispatch_managed_order_insert(
        trader,
        request,
        _insert_fields(),
        execution_capability=object(),
    )

    assert completion.submit_code == 0
    assert completion.request_registered is False
    assert completion.dispatch_status == "UNKNOWN"
    assert completion.provider_ack is False


def test_submit_exception_keeps_exact_request_in_unknown_receipt():
    trader = _FakeManagedTrader(raise_on_submit=True)
    request = _insert_request()

    with pytest.raises(adapter.CtpManagedNativeDispatchError) as raised:
        adapter.dispatch_managed_order_insert(
            trader,
            request,
            _insert_fields(),
            execution_capability=object(),
        )

    completion = raised.value.completion
    assert completion.request is request
    assert completion.echoed_request is request
    assert completion.request.parent_handoff_digest_sha256 == PARENT_HANDOFF
    assert completion.request.managed_intent_id == "managed-entry-1051"
    assert completion.request.order_ref == ORDER_REF
    assert completion.dispatch_status == "UNKNOWN"
    assert completion.provider_ack is False
