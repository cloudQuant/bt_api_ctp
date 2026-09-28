"""Opt-in adapter for exact, managed CTP native order/cancel requests.

This module is intentionally not connected to ``live_ctp_feed`` or the SDK's
default runtime. Callers must already hold the SDK execution capability and
provide the pure, digest-bound request plus the exact native field snapshot.
The returned receipt describes only the local ReqOrder* call; a zero return is
``LOCAL_QUEUED`` and never a provider acknowledgement.

This adapter has fake-client coverage only. Native callback ordering and
cross-thread callback safety have not been accepted for provider deployment.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Any

from .managed_dispatch import (
    CtpManagedNativeDispatchCompletionV1,
    CtpManagedNativeDispatchRequestV1,
)

_ACCOUNT_FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}\Z", re.ASCII)
_TRADING_DAY_RE = re.compile(r"[0-9]{8}\Z", re.ASCII)


class CtpManagedNativeDispatchError(RuntimeError):
    """A native submit raised after dispatch began; inspect its UNKNOWN receipt."""

    def __init__(self, completion: CtpManagedNativeDispatchCompletionV1) -> None:
        self.completion = completion
        super().__init__("managed CTP native dispatch raised; outcome is UNKNOWN")


def dispatch_managed_order_insert(
    trader: Any,
    request: CtpManagedNativeDispatchRequestV1,
    native_fields: Mapping[str, Any],
    *,
    execution_capability: object,
) -> CtpManagedNativeDispatchCompletionV1:
    """Dispatch one exact managed insert through TraderClient's typed gate.

    ``native_fields`` must be the complete public field snapshot used to build
    ``request``. The adapter reconstructs a fresh SDK CTP struct and verifies
    its complete snapshot again before calling ``TraderClient.submit_order_insert``.
    """

    return _dispatch(
        trader,
        request,
        native_fields,
        execution_capability=execution_capability,
        operation="insert",
    )


def dispatch_managed_order_cancel(
    trader: Any,
    request: CtpManagedNativeDispatchRequestV1,
    native_fields: Mapping[str, Any],
    *,
    execution_capability: object,
) -> CtpManagedNativeDispatchCompletionV1:
    """Dispatch one exact managed cancel through TraderClient's typed gate.

    The cancel target must include its exact OrderSysID, ExchangeID, FrontID,
    and SessionID. This adapter never fills missing target fields from the
    current login session.
    """

    return _dispatch(
        trader,
        request,
        native_fields,
        execution_capability=execution_capability,
        operation="cancel",
    )


def _dispatch(
    trader: Any,
    request: CtpManagedNativeDispatchRequestV1,
    native_fields: Mapping[str, Any],
    *,
    execution_capability: object,
    operation: str,
) -> CtpManagedNativeDispatchCompletionV1:
    if type(request) is not CtpManagedNativeDispatchRequestV1:
        raise TypeError("request must be CtpManagedNativeDispatchRequestV1")
    if request.operation != operation:
        raise ValueError("managed request operation does not match dispatch")
    if execution_capability is None:
        raise ValueError("execution_capability is required")
    if not isinstance(native_fields, Mapping):
        raise ValueError("native_fields must be a mapping")
    # Freeze the caller's mapping before verifying or constructing a native
    # object, so a concurrent caller mutation cannot change the submitted data.
    fields = dict(native_fields)
    if not request.verifies_native_fields(fields):
        raise ValueError("native fields do not match the managed dispatch request")
    if operation == "cancel":
        _require_exact_cancel_target(request, fields)

    native_field = _build_native_field(operation, fields)
    rebuilt_fields = _native_field_snapshot(native_field)
    if any(name not in rebuilt_fields for name in fields):
        raise ValueError("rebuilt native fields omitted a request-bound CTP field")
    if any(
        name not in fields and not _is_zeroed_native_default(value)
        for name, value in rebuilt_fields.items()
    ):
        raise ValueError("unbound native CTP fields are not zeroed defaults")
    request_bound_fields = {name: rebuilt_fields[name] for name in fields}
    if not request.verifies_native_fields(request_bound_fields):
        raise ValueError("rebuilt native fields do not match the managed dispatch request")

    lock = getattr(trader, "_query_state_lock", None)
    if lock is None or not callable(getattr(lock, "__enter__", None)):
        raise TypeError("trader must expose the SDK request-state lock")
    with lock:
        account_fingerprint, account_fingerprint_sha256, trading_day, generation = (
            _require_current_scope(trader, request)
        )
    # Do not add an outer SDK lock around ReqOrder*. TraderClient's typed
    # methods perform their own final gate and native call. The captured scope
    # is checked again against the immutable evidence before a receipt can be
    # classified as locally queued.
    if operation == "insert":
        submit = getattr(trader, "submit_order_insert", None)
        evidence_reader = getattr(trader, "get_order_insert_evidence", None)
        evidence_key = {"order_ref": request.order_ref}
        submit_kwargs = {
            "runtime_order_id": request.runtime_order_id,
            "managed_intent_id": request.managed_intent_id,
        }
    else:
        submit = getattr(trader, "submit_order_action", None)
        evidence_reader = getattr(trader, "get_order_action_evidence", None)
        evidence_key = {"order_action_ref": request.native_action_ref}
        submit_kwargs = {
            "runtime_order_id": request.runtime_order_id,
            "managed_intent_id": request.managed_intent_id,
            "runtime_action_id": request.runtime_action_id,
            "managed_cancel_intent_id": request.managed_cancel_intent_id,
        }
    if not callable(submit) or not callable(evidence_reader):
        raise TypeError("trader does not expose the typed CTP submit/evidence API")

    try:
        native_result = submit(
            native_field,
            request.request_id,
            execution_capability=execution_capability,
            **submit_kwargs,
        )
    except Exception as exc:
        evidence = _read_evidence(evidence_reader, request.request_id, evidence_key)
        completion = _make_completion(
            request,
            fields,
            evidence,
            submit_code=None,
            account_fingerprint=account_fingerprint,
            account_fingerprint_sha256=account_fingerprint_sha256,
            trading_day=trading_day,
            connection_generation=generation,
        )
        raise CtpManagedNativeDispatchError(completion) from exc

    evidence = _read_evidence(evidence_reader, request.request_id, evidence_key)
    return _make_completion(
        request,
        fields,
        evidence,
        submit_code=native_result if type(native_result) is int else None,
        account_fingerprint=account_fingerprint,
        account_fingerprint_sha256=account_fingerprint_sha256,
        trading_day=trading_day,
        connection_generation=generation,
    )


def _require_current_scope(
    trader: Any,
    request: CtpManagedNativeDispatchRequestV1,
) -> tuple[str, str, str, int]:
    sdk_fingerprint = getattr(trader, "_account_fingerprint", None)
    if (
        type(sdk_fingerprint) is not str
        or _ACCOUNT_FINGERPRINT_RE.fullmatch(sdk_fingerprint) is None
    ):
        raise ValueError("SDK account identity is unavailable")
    account_fingerprint = f"acct_{sdk_fingerprint}"
    account_digest = hashlib.sha256(account_fingerprint.encode("ascii")).hexdigest()
    if not hmac.compare_digest(account_digest, request.account_fingerprint_sha256):
        raise ValueError("managed request account does not match the SDK session")

    trading_day = getattr(trader, "_trading_day", None)
    generation = getattr(trader, "_connection_generation", None)
    if type(trading_day) is not str or _TRADING_DAY_RE.fullmatch(trading_day) is None:
        raise ValueError("SDK trading day is unavailable")
    if type(generation) is not int or generation <= 0:
        raise ValueError("SDK connection generation is unavailable")
    if trading_day != request.trading_day or generation != request.connection_generation:
        raise ValueError("managed request session does not match the SDK session")
    return account_fingerprint, account_digest, trading_day, generation


def _require_exact_cancel_target(
    request: CtpManagedNativeDispatchRequestV1,
    fields: Mapping[str, Any],
) -> None:
    if (
        not request.target_order_sys_id
        or not request.target_exchange_id
        or type(request.target_front_id) is not int
        or request.target_front_id <= 0
        or type(request.target_session_id) is not int
        or request.target_session_id <= 0
    ):
        raise ValueError("cancel request is missing its exact native target")
    exact_values = {
        "OrderSysID": request.target_order_sys_id,
        "ExchangeID": request.target_exchange_id,
        "FrontID": request.target_front_id,
        "SessionID": request.target_session_id,
    }
    if any(fields.get(name) != expected for name, expected in exact_values.items()):
        raise ValueError("cancel native fields do not match the exact target")


def _build_native_field(operation: str, fields: Mapping[str, Any]) -> Any:
    # Keep CTP type imports lazy. Importing the pure contracts and adapter in
    # offline tooling must not require the native CTP extension to be loaded.
    from .ctp_structs_order import (
        CThostFtdcInputOrderActionField,
        CThostFtdcInputOrderField,
    )

    field_type = (
        CThostFtdcInputOrderField if operation == "insert" else CThostFtdcInputOrderActionField
    )
    native_field = field_type()
    available = set(dir(native_field))
    for name, value in fields.items():
        if (
            type(name) is not str
            or not name
            or name.startswith("_")
            or name in {"this", "thisown"}
            or name not in available
        ):
            raise ValueError("native field snapshot contains an unsupported CTP field")
        try:
            setattr(native_field, name, value)
        except Exception as exc:
            raise ValueError("native field snapshot could not be rebuilt") from exc
    return native_field


def _native_field_snapshot(native_field: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in dir(native_field):
        if name.startswith("_") or name in {"this", "thisown"}:
            continue
        try:
            value = getattr(native_field, name)
        except Exception as exc:
            raise ValueError("rebuilt native CTP field could not be read") from exc
        if not callable(value):
            result[name] = value
    return result


def _is_zeroed_native_default(value: Any) -> bool:
    return (
        value is None
        or (type(value) is str and value == "")
        or (type(value) in (int, float, bool) and value == 0)
    )


def _read_evidence(reader: Any, request_id: int, key: dict[str, str]) -> Any:
    try:
        return reader(request_id, **key)
    except Exception:
        # A missing or unreadable post-submit row cannot be promoted to a
        # registered request. The receipt will remain UNKNOWN.
        return None


def _make_completion(
    request: CtpManagedNativeDispatchRequestV1,
    native_fields: Mapping[str, Any],
    evidence: Any,
    *,
    submit_code: int | None,
    account_fingerprint: str,
    account_fingerprint_sha256: str,
    trading_day: str,
    connection_generation: int,
) -> CtpManagedNativeDispatchCompletionV1:
    request_registered = _evidence_matches_request(
        request,
        native_fields,
        evidence,
        account_fingerprint=account_fingerprint,
        trading_day=trading_day,
        connection_generation=connection_generation,
    )
    callback_received = bool(getattr(evidence, "callback_received", False))
    callback_identity_verified = None
    callback_status = None
    if callback_received:
        callback_identity_verified = bool(
            request_registered and getattr(evidence, "evidence_received", False)
        )
        observed_status = getattr(evidence, "status", "unknown")
        callback_status = (
            observed_status
            if callback_identity_verified and observed_status in {"accepted", "rejected", "unknown"}
            else "unknown"
        )
    return CtpManagedNativeDispatchCompletionV1(
        request=request,
        echoed_request=request,
        submit_code=submit_code,
        request_registered=request_registered,
        # Absence of a callback at the instant of return is not a complete
        # callback history. Therefore negative codes remain UNKNOWN here.
        callback_history_complete=False,
        callback_received=callback_received,
        callback_identity_verified=callback_identity_verified,
        callback_status=callback_status,
        account_fingerprint_sha256=account_fingerprint_sha256,
        trading_day=trading_day,
        connection_generation=connection_generation,
    )


def _evidence_matches_request(
    request: CtpManagedNativeDispatchRequestV1,
    native_fields: Mapping[str, Any],
    evidence: Any,
    *,
    account_fingerprint: str,
    trading_day: str,
    connection_generation: int,
) -> bool:
    if evidence is None:
        return False
    common = (
        getattr(evidence, "request_id", None) == request.request_id
        and getattr(evidence, "account_fingerprint", None) == account_fingerprint
        and getattr(evidence, "trading_day", None) == trading_day
        and getattr(evidence, "connection_generation", None) == connection_generation
        and getattr(evidence, "order_ref", None) == request.order_ref
        and getattr(evidence, "instrument_id", None) == native_fields.get("InstrumentID")
        and getattr(evidence, "exchange_id", None) == native_fields.get("ExchangeID")
    )
    if not common:
        return False
    if request.operation == "insert":
        return True
    return (
        getattr(evidence, "order_action_ref", None) == request.native_action_ref
        and getattr(evidence, "order_sys_id", None) == request.target_order_sys_id
        and getattr(evidence, "front_id", None) == request.target_front_id
        and getattr(evidence, "session_id", None) == request.target_session_id
        and getattr(evidence, "action_flag", None) == "0"
    )


__all__ = [
    "CtpManagedNativeDispatchError",
    "dispatch_managed_order_cancel",
    "dispatch_managed_order_insert",
]
