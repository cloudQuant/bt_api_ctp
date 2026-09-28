"""Offline contracts for the future managed CTP async completion seam."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from bt_api_ctp.ctp.managed_dispatch import (
    CtpManagedNativeDispatchCompletionV1,
    CtpManagedNativeDispatchRequestV1,
)

SCOPE = "1" * 64
ACCOUNT = "2" * 64
PARENT_HANDOFF = "3" * 64
ORDER_REF = "000000000042"
DAY = "20260925"
RUNTIME_ORDER_ID = "bt-managed-v1:" + "a" * 64


def _insert_request(
    *,
    order_ref=ORDER_REF,
    native_order_ref=None,
    managed_intent_id="intent-42",
    runtime_order_id=RUNTIME_ORDER_ID,
    parent_handoff_digest_sha256=PARENT_HANDOFF,
):
    return CtpManagedNativeDispatchRequestV1.from_native_fields(
        operation="insert",
        scope_digest_sha256=SCOPE,
        account_fingerprint_sha256=ACCOUNT,
        parent_handoff_digest_sha256=parent_handoff_digest_sha256,
        managed_intent_id=managed_intent_id,
        runtime_order_id=runtime_order_id,
        order_ref=order_ref,
        request_id=701,
        trading_day=DAY,
        connection_generation=9,
        native_fields={
            "BrokerID": "9999",
            "InvestorID": "offline-account",
            "InstrumentID": "SA609",
            "ExchangeID": "CZCE",
            "OrderRef": native_order_ref or order_ref,
            "RequestID": 701,
            "Direction": "0",
            "CombOffsetFlag": "0",
            "CombHedgeFlag": "2",
            "LimitPrice": 1500.0,
            "VolumeTotalOriginal": 1,
        },
    )


def _cancel_request():
    return CtpManagedNativeDispatchRequestV1.from_native_fields(
        operation="cancel",
        scope_digest_sha256=SCOPE,
        account_fingerprint_sha256=ACCOUNT,
        parent_handoff_digest_sha256=PARENT_HANDOFF,
        managed_intent_id="intent-42",
        runtime_order_id=RUNTIME_ORDER_ID,
        order_ref=ORDER_REF,
        request_id=702,
        trading_day=DAY,
        connection_generation=9,
        runtime_action_id="action-42-cancel-1",
        managed_cancel_intent_id="action-42-cancel-1",
        target_order_sys_id="sys-42",
        target_front_id=17,
        target_session_id=19,
        target_exchange_id="CZCE",
        native_fields={
            "BrokerID": "9999",
            "InvestorID": "offline-account",
            "InstrumentID": "SA609",
            "ExchangeID": "CZCE",
            "OrderRef": ORDER_REF,
            "OrderSysID": "sys-42",
            "FrontID": 17,
            "SessionID": 19,
            "RequestID": 702,
            "OrderActionRef": 702,
            "ActionFlag": "0",
        },
    )


def _completion(request, **overrides):
    values = {
        "request": request,
        "echoed_request": request,
        "submit_code": 0,
        "request_registered": True,
        "callback_history_complete": False,
        "callback_received": False,
        "callback_identity_verified": None,
        "callback_status": None,
        "account_fingerprint_sha256": ACCOUNT,
        "trading_day": DAY,
        "connection_generation": 9,
    }
    values.update(overrides)
    return CtpManagedNativeDispatchCompletionV1(**values)


def test_insert_digest_binds_exact_12_digit_order_ref_and_all_native_fields():
    request = _insert_request()
    changed_price = CtpManagedNativeDispatchRequestV1.from_native_fields(
        operation="insert",
        scope_digest_sha256=SCOPE,
        account_fingerprint_sha256=ACCOUNT,
        parent_handoff_digest_sha256=PARENT_HANDOFF,
        managed_intent_id="intent-42",
        runtime_order_id=RUNTIME_ORDER_ID,
        order_ref=ORDER_REF,
        request_id=701,
        trading_day=DAY,
        connection_generation=9,
        native_fields={
            "BrokerID": "9999",
            "InvestorID": "offline-account",
            "InstrumentID": "SA609",
            "ExchangeID": "CZCE",
            "OrderRef": ORDER_REF,
            "RequestID": 701,
            "Direction": "0",
            "CombOffsetFlag": "0",
            "CombHedgeFlag": "2",
            "LimitPrice": 1501.0,
            "VolumeTotalOriginal": 1,
        },
    )

    assert request.order_ref == ORDER_REF
    assert request.order_ref != request.runtime_order_id
    assert request.native_fields_sha256 != changed_price.native_fields_sha256
    assert request.request_digest_sha256 != changed_price.request_digest_sha256
    assert request.verifies_native_fields(
        {
            "RequestID": 701,
            "OrderRef": ORDER_REF,
            "ExchangeID": "CZCE",
            "InstrumentID": "SA609",
            "BrokerID": "9999",
            "InvestorID": "offline-account",
            "Direction": "0",
            "CombOffsetFlag": "0",
            "CombHedgeFlag": "2",
            "LimitPrice": 1500.0,
            "VolumeTotalOriginal": 1,
        }
    )

    with pytest.raises(FrozenInstanceError):
        request.order_ref = "000000000043"


def test_parent_handoff_digest_is_separate_bound_and_echoed():
    request = _insert_request()
    changed_parent = _insert_request(parent_handoff_digest_sha256="4" * 64)

    assert request.parent_handoff_digest_sha256 == PARENT_HANDOFF
    assert request.parent_handoff_digest_sha256 != request.request_digest_sha256
    assert request.native_fields_sha256 == changed_parent.native_fields_sha256
    assert request.request_digest_sha256 != changed_parent.request_digest_sha256
    assert _completion(request, echoed_request=changed_parent).dispatch_status == "UNKNOWN"


@pytest.mark.parametrize("bad_digest", ["", "4" * 63, "g" * 64])
def test_parent_handoff_digest_is_required_to_be_a_sha256(bad_digest):
    with pytest.raises(ValueError, match="parent_handoff_digest_sha256"):
        _insert_request(parent_handoff_digest_sha256=bad_digest)


@pytest.mark.parametrize("bad_ref", ["42", "00000000004é", "000000000042\x00"])
def test_insert_rejects_non_native_order_ref(bad_ref):
    with pytest.raises(ValueError, match="OrderRef|12 ASCII digits"):
        _insert_request(order_ref=bad_ref)


def test_insert_rejects_native_order_ref_that_differs_from_managed_mapping():
    with pytest.raises(ValueError, match="OrderRef"):
        _insert_request(native_order_ref="000000000043")


def test_managed_id_validation_matches_execution_ledger_token_intersection():
    longest = "x" * 128
    assert _insert_request(managed_intent_id=longest).managed_intent_id == longest

    for invalid in ("x" * 129, "intent/42", "intént-42", "intent 42"):
        with pytest.raises(ValueError, match="managed_intent_id"):
            _insert_request(managed_intent_id=invalid)

    with pytest.raises(ValueError, match="runtime_order_id"):
        _insert_request(runtime_order_id="bt-managed-v1:" + "A" * 64)


def test_cancel_binds_same_action_identity_and_complete_native_target_tuple():
    request = _cancel_request()

    assert request.runtime_action_id == request.managed_cancel_intent_id
    assert request.native_action_ref == "702"
    assert request.native_action_ref != request.runtime_action_id
    assert request.target_order_sys_id == "sys-42"
    assert (request.target_front_id, request.target_session_id) == (17, 19)
    assert request.verifies_native_fields(
        {
            "BrokerID": "9999",
            "InvestorID": "offline-account",
            "InstrumentID": "SA609",
            "ExchangeID": "CZCE",
            "OrderRef": ORDER_REF,
            "OrderSysID": "sys-42",
            "FrontID": 17,
            "SessionID": 19,
            "RequestID": 702,
            "OrderActionRef": 702,
            "ActionFlag": "0",
        }
    )


def test_cancel_rejects_mismatched_action_id_or_target():
    with pytest.raises(ValueError, match="action IDs must be equal"):
        CtpManagedNativeDispatchRequestV1.from_native_fields(
            operation="cancel",
            scope_digest_sha256=SCOPE,
            account_fingerprint_sha256=ACCOUNT,
            parent_handoff_digest_sha256=PARENT_HANDOFF,
            managed_intent_id="intent-42",
            runtime_order_id=RUNTIME_ORDER_ID,
            order_ref=ORDER_REF,
            request_id=702,
            trading_day=DAY,
            connection_generation=9,
            runtime_action_id="action-1",
            managed_cancel_intent_id="action-2",
            target_order_sys_id="sys-42",
            target_front_id=17,
            target_session_id=19,
            target_exchange_id="CZCE",
            native_fields={
                "InstrumentID": "SA609",
                "OrderRef": ORDER_REF,
                "OrderSysID": "sys-42",
                "FrontID": 17,
                "SessionID": 19,
                "ExchangeID": "CZCE",
                "BrokerID": "9999",
                "InvestorID": "offline-account",
                "RequestID": 702,
                "OrderActionRef": 702,
                "ActionFlag": "0",
            },
        )

    request = _cancel_request()
    changed_target = {
        "OrderRef": ORDER_REF,
        "OrderSysID": "sys-other",
        "FrontID": 17,
        "SessionID": 19,
        "ExchangeID": "CZCE",
        "RequestID": 702,
        "OrderActionRef": 702,
        "ActionFlag": "0",
    }
    assert not request.verifies_native_fields(changed_target)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("OrderRef", "000000000043"),
        ("OrderSysID", "sys-other"),
        ("FrontID", 18),
        ("SessionID", 20),
        ("ExchangeID", "SHFE"),
        ("OrderActionRef", 703),
        ("RequestID", 703),
    ],
)
def test_cancel_digest_rejects_any_changed_target_or_native_action_identity(field_name, value):
    request = _cancel_request()
    fields = {
        "BrokerID": "9999",
        "InvestorID": "offline-account",
        "InstrumentID": "SA609",
        "ExchangeID": "CZCE",
        "OrderRef": ORDER_REF,
        "OrderSysID": "sys-42",
        "FrontID": 17,
        "SessionID": 19,
        "RequestID": 702,
        "OrderActionRef": 702,
        "ActionFlag": "0",
    }
    fields[field_name] = value

    assert not request.verifies_native_fields(fields)


def test_zero_code_returns_local_queued_without_claiming_provider_ack():
    completion = _completion(
        _insert_request(),
        callback_history_complete=True,
        callback_received=True,
        callback_identity_verified=True,
        callback_status="accepted",
    )

    assert completion.dispatch_status == "LOCAL_QUEUED"
    assert completion.provider_ack is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"submit_code": -1, "callback_history_complete": False},
        {
            "submit_code": -1,
            "callback_history_complete": True,
            "callback_received": True,
            "callback_identity_verified": True,
            "callback_status": "rejected",
        },
        {"submit_code": None, "callback_history_complete": True},
        {"submit_code": 0, "request_registered": False},
    ],
)
def test_incomplete_or_ambiguous_dispatch_evidence_stays_unknown(overrides):
    assert _completion(_insert_request(), **overrides).dispatch_status == "UNKNOWN"


def test_negative_code_is_local_reject_only_with_complete_no_callback_evidence():
    completion = _completion(
        _insert_request(),
        submit_code=-3,
        callback_history_complete=True,
    )

    assert completion.dispatch_status == "LOCAL_REJECTED"
    assert completion.provider_ack is False


def test_mismatched_request_or_session_echo_fails_closed_as_unknown():
    request = _insert_request()
    changed_identity = replace(request, managed_intent_id="intent-other")
    assert _completion(request, echoed_request=changed_identity).dispatch_status == "UNKNOWN"
    changed_ref = replace(request, order_ref="000000000043")
    assert _completion(request, echoed_request=changed_ref).dispatch_status == "UNKNOWN"
    assert _completion(request, connection_generation=10).dispatch_status == "UNKNOWN"
    assert (
        _completion(
            request,
            callback_history_complete=True,
            callback_received=True,
            callback_identity_verified=False,
            callback_status="accepted",
        ).dispatch_status
        == "UNKNOWN"
    )
