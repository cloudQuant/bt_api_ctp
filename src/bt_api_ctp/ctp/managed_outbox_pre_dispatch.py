"""Non-authorizing pre-dispatch checks for the v5 CTP command outbox.

The execution outbox persists approval and source digests, but this package has
no verifier for those records and no atomic writer-lease handoff to the SDK.
This module therefore only builds a typed, digest-bound candidate from a READY
row. It never claims, submits, completes, retries, or interprets a queue
result. A separate reviewed composition must verify current per-action
approval/source evidence before using the native adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .managed_dispatch import CtpManagedNativeDispatchRequestV1

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ACCOUNT_KEY_RE = re.compile(r"account:[0-9a-f]{64}\Z", re.ASCII)
_SCOPE_KEY_RE = re.compile(r"scope:([0-9a-f]{64})\Z", re.ASCII)
_ACCOUNT_FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}\Z", re.ASCII)
_ORDER_REF_RE = re.compile(r"[0-9]{12}\Z", re.ASCII)


class CtpOutboxPreDispatchError(ValueError):
    """The durable command is not an exact candidate for native dispatch."""


class CtpOutboxDispatchAuthorityRequired(RuntimeError):
    """A candidate cannot dispatch without a separately reviewed verifier."""


@dataclass(frozen=True)
class CtpManagedNativeOutboxCandidateV1:
    """An exact native request candidate that carries no dispatch authority."""

    command_id: str
    account_key: str
    scope_key: str
    trading_day: str
    operation: str
    request_payload_sha256: str
    reservation_managed_intent_id: str
    approval_use_id: str
    approval_digest: str
    session_binding_sha256: str
    parent_handoff_digest_sha256: str
    request: CtpManagedNativeDispatchRequestV1

    @property
    def authorizes_dispatch(self) -> bool:
        """Approval digests are preserved evidence, never fresh authorization."""

        return False

    def require_dispatch_authority(self) -> None:
        """Fail closed until an independently reviewed verifier is integrated."""

        raise CtpOutboxDispatchAuthorityRequired(
            "outbox approval/source evidence has no reviewed native verifier"
        )


def prepare_ctp_managed_native_outbox_candidate(
    store: Any,
    scope: Any,
    command_id: str,
    trader: Any,
    native_fields: Mapping[str, Any],
    *,
    writer_lease: object,
) -> CtpManagedNativeOutboxCandidateV1:
    """Validate one existing READY v5 command without claiming or dispatching.

    ``scope`` is the exact config-derived execution scope used by the outbox.
    The command and order identity are re-read from the durable store, and the
    current local writer lease and SDK account/session snapshot are checked.
    Only a READY row is eligible. Claiming requires the outbox seed/fence gate
    and a fresh external per-action verifier, neither of which this candidate
    builder supplies. CLAIMED, UNKNOWN, and completed rows cannot be replayed
    through this boundary.
    """

    if type(command_id) is not str or not command_id or command_id != command_id.strip():
        raise CtpOutboxPreDispatchError("command_id is invalid")
    account_key, scope_key, trading_day, scope_digest = _scope_facts(scope)

    lease_assertion = getattr(store, "assert_writer_lease", None)
    command_reader = getattr(store, "read_ctp_dispatch_command", None)
    identity_reader = getattr(store, "read_ctp_order_identity", None)
    if not callable(lease_assertion) or not callable(command_reader) or not callable(identity_reader):
        raise TypeError("store must expose read-only CTP outbox and writer-lease APIs")
    lease_assertion(scope, writer_lease)

    command = command_reader(scope, command_id)
    if command is None:
        raise CtpOutboxPreDispatchError("unknown CTP command")
    if getattr(command, "command_id", None) != command_id:
        raise CtpOutboxPreDispatchError("stored command identity mismatch")
    # The claim transaction checks the seed watermark and account-level
    # unresolved-command fence. This boundary intentionally stops before it.
    if getattr(command, "status", None) != "READY":
        raise CtpOutboxPreDispatchError("only a READY staged command is eligible")
    if any(
        getattr(command, name, None) is not None
        for name in (
            "completed_at_ns",
            "unknown_at_ns",
            "native_receipt_payload",
            "native_receipt_sha256",
            "completion_echo_sha256",
        )
    ):
        raise CtpOutboxPreDispatchError("claimed command unexpectedly carries a receipt")

    if (
        getattr(command, "account_key", None) != account_key
        or getattr(command, "scope_key", None) != scope_key
        or getattr(command, "trading_day", None) != trading_day
    ):
        raise CtpOutboxPreDispatchError("command does not match the config-derived scope")

    managed_intent_id = _text_attr(command, "reservation_managed_intent_id")
    reservation = identity_reader(scope, managed_intent_id)
    if reservation is None:
        raise CtpOutboxPreDispatchError("command has no durable order identity reservation")
    if (
        getattr(reservation, "account_key", None) != account_key
        or getattr(reservation, "scope_key", None) != scope_key
        or getattr(reservation, "trading_day", None) != trading_day
        or getattr(reservation, "managed_intent_id", None) != managed_intent_id
    ):
        raise CtpOutboxPreDispatchError("reservation does not match the config-derived scope")

    order_ref = getattr(reservation, "order_ref", None)
    runtime_order_id = getattr(reservation, "runtime_order_id", None)
    if type(order_ref) is not str or _ORDER_REF_RE.fullmatch(order_ref) is None:
        raise CtpOutboxPreDispatchError("reserved OrderRef must be exactly 12 digits")
    if (
        type(runtime_order_id) is not str
        or not runtime_order_id.startswith("bt-managed-v1:")
        or len(runtime_order_id) != len("bt-managed-v1:") + 64
        or any(char not in "0123456789abcdef" for char in runtime_order_id[14:])
    ):
        raise CtpOutboxPreDispatchError("reservation runtime order identity is invalid")

    fields = _copy_json_mapping(native_fields, "native_fields")
    payload = _copy_json_mapping(getattr(command, "request_payload", None), "request_payload")
    if fields != payload:
        raise CtpOutboxPreDispatchError("native fields do not exactly match the durable payload")
    payload_digest = _required_digest(
        getattr(command, "request_payload_sha256", None), "request_payload_sha256"
    )
    if _sha256_json(payload) != payload_digest:
        raise CtpOutboxPreDispatchError("durable request payload digest mismatch")

    session_binding = _copy_json_mapping(
        getattr(command, "session_binding", None), "session_binding"
    )
    session_binding_digest = _required_digest(
        getattr(command, "session_binding_sha256", None), "session_binding_sha256"
    )
    if _sha256_json(session_binding) != session_binding_digest:
        raise CtpOutboxPreDispatchError("durable session binding digest mismatch")
    approval_use_id = _required_token(getattr(command, "approval_use_id", None), "approval_use_id")
    approval_digest = _required_digest(getattr(command, "approval_digest", None), "approval_digest")

    operation = getattr(command, "operation", None)
    if operation not in {"SUBMIT", "CANCEL"}:
        raise CtpOutboxPreDispatchError("unsupported CTP command operation")
    if _native_text(fields, "OrderRef") != order_ref:
        raise CtpOutboxPreDispatchError("native OrderRef does not match the reservation")

    current_account_digest, current_trading_day, connection_generation = _sdk_session_facts(
        trader
    )
    if current_trading_day != trading_day:
        raise CtpOutboxPreDispatchError("SDK trading day does not match the command")
    request_id = _native_int(fields, "RequestID")

    cancel_values: dict[str, Any] = {}
    runtime_action_id = None
    if operation == "SUBMIT":
        if (
            getattr(command, "order_ref", None) != order_ref
            or any(
                getattr(command, name, None) is not None
                for name in (
                    "cancel_target_order_ref",
                    "cancel_target_exchange_id",
                    "cancel_target_order_sys_id",
                    "cancel_target_front_id",
                    "cancel_target_session_id",
                )
            )
        ):
            raise CtpOutboxPreDispatchError("submit command does not match its reservation")
        native_operation = "insert"
    else:
        if getattr(command, "order_ref", None) is not None:
            raise CtpOutboxPreDispatchError("cancel command cannot reserve a second OrderRef")
        cancel_values = _exact_cancel_target(command, fields, order_ref)
        # The durable command identity is the only persisted action identity
        # available in outbox v5. It is preserved exactly, never truncated.
        runtime_action_id = command_id
        native_operation = "cancel"

    handoff_digest = _sha256_json(
        {
            "schema": "bt_api_execution.ctp_dispatch_command.handoff.v1",
            "account_key": account_key,
            "scope_key": scope_key,
            "trading_day": trading_day,
            "operation": operation,
            "command_id": command_id,
            "request_payload_sha256": payload_digest,
            "reservation_managed_intent_id": managed_intent_id,
            "runtime_order_id": runtime_order_id,
            "order_ref": getattr(command, "order_ref", None),
            "cancel_target_order_ref": getattr(command, "cancel_target_order_ref", None),
            "cancel_target_exchange_id": getattr(command, "cancel_target_exchange_id", None),
            "cancel_target_order_sys_id": getattr(command, "cancel_target_order_sys_id", None),
            "cancel_target_front_id": getattr(command, "cancel_target_front_id", None),
            "cancel_target_session_id": getattr(command, "cancel_target_session_id", None),
            "approval_use_id": approval_use_id,
            "approval_digest": approval_digest,
            "session_binding_sha256": session_binding_digest,
        }
    )
    request = CtpManagedNativeDispatchRequestV1.from_native_fields(
        operation=native_operation,
        scope_digest_sha256=scope_digest,
        account_fingerprint_sha256=current_account_digest,
        parent_handoff_digest_sha256=handoff_digest,
        managed_intent_id=managed_intent_id,
        runtime_order_id=runtime_order_id,
        order_ref=order_ref,
        request_id=request_id,
        trading_day=trading_day,
        connection_generation=connection_generation,
        native_fields=fields,
        runtime_action_id=runtime_action_id,
        managed_cancel_intent_id=runtime_action_id,
        target_order_sys_id=cancel_values.get("order_sys_id"),
        target_front_id=cancel_values.get("front_id"),
        target_session_id=cancel_values.get("session_id"),
        target_exchange_id=cancel_values.get("exchange_id"),
    )
    return CtpManagedNativeOutboxCandidateV1(
        command_id=command_id,
        account_key=account_key,
        scope_key=scope_key,
        trading_day=trading_day,
        operation=operation,
        request_payload_sha256=payload_digest,
        reservation_managed_intent_id=managed_intent_id,
        approval_use_id=approval_use_id,
        approval_digest=approval_digest,
        session_binding_sha256=session_binding_digest,
        parent_handoff_digest_sha256=handoff_digest,
        request=request,
    )


def _scope_facts(scope: Any) -> tuple[str, str, str, str]:
    account_key = getattr(scope, "account_key", None)
    scope_key = getattr(scope, "key", None)
    trading_day = getattr(scope, "trading_day", None)
    if (
        type(account_key) is not str
        or _ACCOUNT_KEY_RE.fullmatch(account_key) is None
        or type(scope_key) is not str
        or (match := _SCOPE_KEY_RE.fullmatch(scope_key)) is None
        or type(trading_day) is not str
        or re.fullmatch(r"[0-9]{8}", trading_day, re.ASCII) is None
    ):
        raise CtpOutboxPreDispatchError("scope must be an exact dated execution scope")
    return account_key, scope_key, trading_day, match.group(1)


def _sdk_session_facts(trader: Any) -> tuple[str, str, int]:
    lock = getattr(trader, "_query_state_lock", None)
    if lock is None or not callable(getattr(lock, "__enter__", None)):
        raise CtpOutboxPreDispatchError("trader does not expose the SDK state lock")
    with lock:
        fingerprint = getattr(trader, "_account_fingerprint", None)
        trading_day = getattr(trader, "_trading_day", None)
        generation = getattr(trader, "_connection_generation", None)
    if type(fingerprint) is not str or _ACCOUNT_FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise CtpOutboxPreDispatchError("SDK account identity is unavailable")
    if type(trading_day) is not str or re.fullmatch(r"[0-9]{8}", trading_day, re.ASCII) is None:
        raise CtpOutboxPreDispatchError("SDK trading day is unavailable")
    if type(generation) is not int or generation <= 0:
        raise CtpOutboxPreDispatchError("SDK connection generation is unavailable")
    account_digest = hashlib.sha256(f"acct_{fingerprint}".encode("ascii")).hexdigest()
    return account_digest, trading_day, generation


def _exact_cancel_target(command: Any, fields: Mapping[str, Any], order_ref: str) -> dict[str, Any]:
    target = {
        "order_ref": getattr(command, "cancel_target_order_ref", None),
        "exchange_id": getattr(command, "cancel_target_exchange_id", None),
        "order_sys_id": getattr(command, "cancel_target_order_sys_id", None),
        "front_id": getattr(command, "cancel_target_front_id", None),
        "session_id": getattr(command, "cancel_target_session_id", None),
    }
    if (
        target["order_ref"] != order_ref
        or type(target["exchange_id"]) is not str
        or not target["exchange_id"]
        or type(target["order_sys_id"]) is not str
        or not target["order_sys_id"]
        or type(target["front_id"]) is not int
        or target["front_id"] <= 0
        or type(target["session_id"]) is not int
        or target["session_id"] <= 0
    ):
        raise CtpOutboxPreDispatchError("cancel command is missing its exact target")
    expected_native = {
        "OrderRef": target["order_ref"],
        "ExchangeID": target["exchange_id"],
        "OrderSysID": target["order_sys_id"],
        "FrontID": target["front_id"],
        "SessionID": target["session_id"],
        "ActionFlag": "0",
    }
    if any(fields.get(name) != value for name, value in expected_native.items()):
        raise CtpOutboxPreDispatchError("cancel fields do not match the exact target")
    return target


def _copy_json_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CtpOutboxPreDispatchError(f"{name} must be a mapping")
    try:
        encoded = json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        copied = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise CtpOutboxPreDispatchError(f"{name} is not canonical JSON") from exc
    if not isinstance(copied, dict) or not copied:
        raise CtpOutboxPreDispatchError(f"{name} must be a non-empty object")
    return copied


def _sha256_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise CtpOutboxPreDispatchError("handoff contains non-canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _required_digest(value: Any, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise CtpOutboxPreDispatchError(f"{name} is invalid")
    return value


def _required_token(value: Any, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 128
        or not value.isascii()
        or any(not (char.isalnum() or char in "._:-") for char in value)
    ):
        raise CtpOutboxPreDispatchError(f"{name} is invalid")
    return value


def _text_attr(instance: Any, name: str) -> str:
    value = getattr(instance, name, None)
    if type(value) is not str or not value:
        raise CtpOutboxPreDispatchError(f"{name} is invalid")
    return value


def _native_text(fields: Mapping[str, Any], name: str) -> str:
    value = fields.get(name)
    if type(value) is not str or not value:
        raise CtpOutboxPreDispatchError(f"native {name} is required")
    return value


def _native_int(fields: Mapping[str, Any], name: str) -> int:
    value = fields.get(name)
    if type(value) is int and value > 0:
        return value
    if type(value) is str and value.isascii() and value.isdigit() and int(value) > 0:
        return int(value)
    raise CtpOutboxPreDispatchError(f"native {name} is invalid")


__all__ = [
    "CtpManagedNativeOutboxCandidateV1",
    "CtpOutboxDispatchAuthorityRequired",
    "CtpOutboxPreDispatchError",
    "prepare_ctp_managed_native_outbox_candidate",
]
