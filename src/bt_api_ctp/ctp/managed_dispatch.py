"""Pure contracts for a future managed CTP async dispatch handoff.

These values do not authorize, arm, or perform native writes. The public feed
does not emit them yet. A dispatch result describes the local CTP API call;
``LOCAL_QUEUED`` is never a provider acknowledgement or order-state update.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ORDER_REF_RE = re.compile(r"[0-9]{12}\Z", re.ASCII)
_NATIVE_ACTION_REF_RE = re.compile(r"[0-9]{1,12}\Z", re.ASCII)
_MANAGED_TOKEN_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z", re.ASCII)
_RUNTIME_ORDER_ID_RE = re.compile(r"bt-managed-v1:[0-9a-f]{64}\Z", re.ASCII)
_CALLBACK_STATES = frozenset({"accepted", "rejected", "unknown"})


def _required_text(value: Any, name: str, *, ascii_only: bool = False) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or (ascii_only and not value.isascii())
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _required_digest(value: Any, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _required_managed_token(value: Any, name: str) -> str:
    if type(value) is not str or _MANAGED_TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be 1 to 128 ASCII managed-identity characters")
    return value


def _required_runtime_order_id(value: Any) -> str:
    if type(value) is not str or _RUNTIME_ORDER_ID_RE.fullmatch(value) is None:
        raise ValueError("runtime_order_id must be bt-managed-v1 plus a lowercase SHA-256 digest")
    return value


def _json_value(value: Any, name: str = "native_fields") -> Any:
    """Copy a native field snapshot into a deterministic JSON-safe tree."""

    if value is None or type(value) in (str, bool, int):
        if type(value) is str and any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise ValueError(f"{name} contains a control character")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{name} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str or not key:
                raise ValueError(f"{name} has a non-text field name")
            result[key] = _json_value(item, f"{name}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item, name) for item in value]
    raise ValueError(f"{name} contains an unsupported native field value")


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _native_text(fields: Mapping[str, Any], name: str) -> str:
    value = fields.get(name)
    if type(value) is not str or not value:
        raise ValueError(f"native {name} is required")
    return value


def _native_int(fields: Mapping[str, Any], name: str) -> int:
    value = fields.get(name)
    if type(value) is int:
        return value
    if type(value) is str and value.isascii() and value.isdigit():
        return int(value)
    raise ValueError(f"native {name} is invalid")


@dataclass(frozen=True)
class CtpManagedNativeDispatchRequestV1:
    """Digest-bound identity for one managed native CTP request.

    ``order_ref`` is the exact native 12-digit CTP reference. It is a separate
    field from runtime and client IDs. Cancellation also carries the complete
    target tuple and one stable action identity; ``native_action_ref`` is the
    CTP request-path value and is never derived by truncating that identity.
    The parent handoff digest and this SDK-native request digest are separate
    values with separate domains; the latter binds the former into its input.
    """

    operation: Literal["insert", "cancel"]
    scope_digest_sha256: str
    account_fingerprint_sha256: str
    parent_handoff_digest_sha256: str
    managed_intent_id: str
    runtime_order_id: str
    order_ref: str
    request_id: int
    trading_day: str
    connection_generation: int
    native_fields_sha256: str
    request_digest_sha256: str
    runtime_action_id: str | None = None
    managed_cancel_intent_id: str | None = None
    native_action_ref: str | None = None
    target_order_sys_id: str | None = None
    target_front_id: int | None = None
    target_session_id: int | None = None
    target_exchange_id: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in {"insert", "cancel"}:
            raise ValueError("operation must be insert or cancel")
        _required_digest(self.scope_digest_sha256, "scope_digest_sha256")
        _required_digest(self.account_fingerprint_sha256, "account_fingerprint_sha256")
        _required_digest(self.parent_handoff_digest_sha256, "parent_handoff_digest_sha256")
        _required_managed_token(self.managed_intent_id, "managed_intent_id")
        _required_runtime_order_id(self.runtime_order_id)
        if type(self.order_ref) is not str or _ORDER_REF_RE.fullmatch(self.order_ref) is None:
            raise ValueError("order_ref must be exactly 12 ASCII digits")
        if type(self.request_id) is not int or self.request_id <= 0:
            raise ValueError("request_id must be a positive integer")
        if (
            type(self.trading_day) is not str
            or re.fullmatch(r"[0-9]{8}", self.trading_day, re.ASCII) is None
        ):
            raise ValueError("trading_day must be YYYYMMDD ASCII digits")
        if type(self.connection_generation) is not int or self.connection_generation <= 0:
            raise ValueError("connection_generation must be positive")
        _required_digest(self.native_fields_sha256, "native_fields_sha256")
        _required_digest(self.request_digest_sha256, "request_digest_sha256")

        if self.operation == "insert":
            if any(
                value is not None
                for value in (
                    self.runtime_action_id,
                    self.managed_cancel_intent_id,
                    self.native_action_ref,
                    self.target_order_sys_id,
                    self.target_front_id,
                    self.target_session_id,
                    self.target_exchange_id,
                )
            ):
                raise ValueError("insert request cannot carry cancel identity")
            return

        _required_managed_token(self.runtime_action_id, "runtime_action_id")
        _required_managed_token(self.managed_cancel_intent_id, "managed_cancel_intent_id")
        if self.runtime_action_id != self.managed_cancel_intent_id:
            raise ValueError("cancel action IDs must be equal")
        _required_text(self.native_action_ref, "native_action_ref", ascii_only=True)
        if _NATIVE_ACTION_REF_RE.fullmatch(self.native_action_ref) is None:
            raise ValueError("native_action_ref must be 1 to 12 ASCII digits")
        _required_text(self.target_order_sys_id, "target_order_sys_id")
        _required_text(self.target_exchange_id, "target_exchange_id", ascii_only=True)
        if type(self.target_front_id) is not int or self.target_front_id <= 0:
            raise ValueError("target_front_id must be positive")
        if type(self.target_session_id) is not int or self.target_session_id <= 0:
            raise ValueError("target_session_id must be positive")

    @classmethod
    def from_native_fields(
        cls,
        *,
        operation: Literal["insert", "cancel"],
        scope_digest_sha256: str,
        account_fingerprint_sha256: str,
        parent_handoff_digest_sha256: str,
        managed_intent_id: str,
        runtime_order_id: str,
        order_ref: str,
        request_id: int,
        trading_day: str,
        connection_generation: int,
        native_fields: Mapping[str, Any],
        runtime_action_id: str | None = None,
        managed_cancel_intent_id: str | None = None,
        target_order_sys_id: str | None = None,
        target_front_id: int | None = None,
        target_session_id: int | None = None,
        target_exchange_id: str | None = None,
    ) -> CtpManagedNativeDispatchRequestV1:
        """Build the request digest from the exact native field snapshot."""

        if not isinstance(native_fields, Mapping):
            raise ValueError("native_fields must be a mapping")
        fields = _json_value(native_fields)
        if not isinstance(fields, dict):
            raise ValueError("native_fields must be a mapping")
        if _native_text(fields, "OrderRef") != order_ref:
            raise ValueError("native OrderRef does not match managed mapping")
        if _native_int(fields, "RequestID") != request_id:
            raise ValueError("native RequestID does not match request_id")
        _required_text(_native_text(fields, "InstrumentID"), "native InstrumentID")
        _required_text(_native_text(fields, "ExchangeID"), "native ExchangeID")

        native_action_ref = None
        if operation == "insert":
            if any(
                value is not None
                for value in (
                    runtime_action_id,
                    managed_cancel_intent_id,
                    target_order_sys_id,
                    target_front_id,
                    target_session_id,
                    target_exchange_id,
                )
            ):
                raise ValueError("insert request cannot carry cancel identity")
        elif operation == "cancel":
            if _native_text(fields, "OrderSysID") != target_order_sys_id:
                raise ValueError("native OrderSysID does not match cancel target")
            if _native_int(fields, "FrontID") != target_front_id:
                raise ValueError("native FrontID does not match cancel target")
            if _native_int(fields, "SessionID") != target_session_id:
                raise ValueError("native SessionID does not match cancel target")
            if _native_text(fields, "ExchangeID") != target_exchange_id:
                raise ValueError("native ExchangeID does not match cancel target")
            if _native_text(fields, "ActionFlag") != "0":
                raise ValueError("managed cancel ActionFlag must be delete")
            for mutation_field in ("LimitPrice", "VolumeChange"):
                if mutation_field in fields:
                    value = fields[mutation_field]
                    if type(value) not in (int, float) or value != 0:
                        raise ValueError(
                            f"managed cancel {mutation_field} must be zero or absent"
                        )
            action_value = fields.get("OrderActionRef")
            if type(action_value) is int and action_value > 0:
                native_action_ref = str(action_value)
            elif (
                type(action_value) is str
                and _NATIVE_ACTION_REF_RE.fullmatch(action_value) is not None
            ):
                native_action_ref = action_value
            else:
                raise ValueError("native OrderActionRef is invalid")
        else:
            raise ValueError("operation must be insert or cancel")

        _required_digest(scope_digest_sha256, "scope_digest_sha256")
        _required_digest(account_fingerprint_sha256, "account_fingerprint_sha256")
        _required_digest(parent_handoff_digest_sha256, "parent_handoff_digest_sha256")
        native_fields_sha256 = _sha256_json(fields)
        digest_payload = {
            "schema": "ctp.managed.native-dispatch.v1",
            "operation": operation,
            "scope_digest_sha256": scope_digest_sha256,
            "account_fingerprint_sha256": account_fingerprint_sha256,
            "parent_handoff_digest_sha256": parent_handoff_digest_sha256,
            "managed_intent_id": managed_intent_id,
            "runtime_order_id": runtime_order_id,
            "order_ref": order_ref,
            "request_id": request_id,
            "trading_day": trading_day,
            "connection_generation": connection_generation,
            "runtime_action_id": runtime_action_id,
            "managed_cancel_intent_id": managed_cancel_intent_id,
            "native_action_ref": native_action_ref,
            "target_order_sys_id": target_order_sys_id,
            "target_front_id": target_front_id,
            "target_session_id": target_session_id,
            "target_exchange_id": target_exchange_id,
            "native_fields": fields,
        }
        request_digest_sha256 = _sha256_json(digest_payload)
        return cls(
            operation=operation,
            scope_digest_sha256=scope_digest_sha256,
            account_fingerprint_sha256=account_fingerprint_sha256,
            parent_handoff_digest_sha256=parent_handoff_digest_sha256,
            managed_intent_id=managed_intent_id,
            runtime_order_id=runtime_order_id,
            order_ref=order_ref,
            request_id=request_id,
            trading_day=trading_day,
            connection_generation=connection_generation,
            native_fields_sha256=native_fields_sha256,
            request_digest_sha256=request_digest_sha256,
            runtime_action_id=runtime_action_id,
            managed_cancel_intent_id=managed_cancel_intent_id,
            native_action_ref=native_action_ref,
            target_order_sys_id=target_order_sys_id,
            target_front_id=target_front_id,
            target_session_id=target_session_id,
            target_exchange_id=target_exchange_id,
        )

    def verifies_native_fields(self, native_fields: Mapping[str, Any]) -> bool:
        """Check a native snapshot against both digests on this request."""

        try:
            rebuilt = type(self).from_native_fields(
                operation=self.operation,
                scope_digest_sha256=self.scope_digest_sha256,
                account_fingerprint_sha256=self.account_fingerprint_sha256,
                parent_handoff_digest_sha256=self.parent_handoff_digest_sha256,
                managed_intent_id=self.managed_intent_id,
                runtime_order_id=self.runtime_order_id,
                order_ref=self.order_ref,
                request_id=self.request_id,
                trading_day=self.trading_day,
                connection_generation=self.connection_generation,
                native_fields=native_fields,
                runtime_action_id=self.runtime_action_id,
                managed_cancel_intent_id=self.managed_cancel_intent_id,
                target_order_sys_id=self.target_order_sys_id,
                target_front_id=self.target_front_id,
                target_session_id=self.target_session_id,
                target_exchange_id=self.target_exchange_id,
            )
        except (TypeError, ValueError):
            return False
        return (
            rebuilt.native_fields_sha256 == self.native_fields_sha256
            and rebuilt.request_digest_sha256 == self.request_digest_sha256
            and rebuilt.native_action_ref == self.native_action_ref
        )


@dataclass(frozen=True)
class CtpManagedNativeDispatchCompletionV1:
    """Typed, request-bound observation of one local CTP API completion.

    ``LOCAL_QUEUED`` only means the exact native request returned code zero and
    its SDK request record was present. Callback facts remain separate and
    this type never reports a provider ACK or an order/cancel terminal state.
    """

    request: CtpManagedNativeDispatchRequestV1
    echoed_request: CtpManagedNativeDispatchRequestV1
    submit_code: int | None
    request_registered: bool
    callback_history_complete: bool
    callback_received: bool
    callback_identity_verified: bool | None
    callback_status: Literal["accepted", "rejected", "unknown"] | None
    account_fingerprint_sha256: str
    trading_day: str
    connection_generation: int

    def __post_init__(self) -> None:
        if (
            type(self.request) is not CtpManagedNativeDispatchRequestV1
            or type(self.echoed_request) is not CtpManagedNativeDispatchRequestV1
        ):
            raise ValueError("completion requires typed request and echo")
        if self.submit_code is not None and type(self.submit_code) is not int:
            raise ValueError("submit_code must be an integer or None")
        for name in (
            "request_registered",
            "callback_history_complete",
            "callback_received",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        if self.callback_received:
            if type(self.callback_identity_verified) is not bool:
                raise ValueError("callback identity verification is required")
            if self.callback_status not in _CALLBACK_STATES:
                raise ValueError("callback_status is invalid")
        elif (
            self.callback_identity_verified not in (None, False) or self.callback_status is not None
        ):
            raise ValueError("callback details require a received callback")
        _required_digest(self.account_fingerprint_sha256, "account_fingerprint_sha256")
        if (
            type(self.trading_day) is not str
            or re.fullmatch(r"[0-9]{8}", self.trading_day, re.ASCII) is None
        ):
            raise ValueError("trading_day must be YYYYMMDD ASCII digits")
        if type(self.connection_generation) is not int or self.connection_generation <= 0:
            raise ValueError("connection_generation must be positive")

    @property
    def identity_matches(self) -> bool:
        return (
            self.request == self.echoed_request
            and self.account_fingerprint_sha256 == self.request.account_fingerprint_sha256
            and self.trading_day == self.request.trading_day
            and self.connection_generation == self.request.connection_generation
            and (not self.callback_received or self.callback_identity_verified is True)
        )

    @property
    def dispatch_status(self) -> Literal["LOCAL_QUEUED", "LOCAL_REJECTED", "UNKNOWN"]:
        """Classify local dispatch evidence without promoting callback state."""

        if not self.identity_matches or not self.request_registered:
            return "UNKNOWN"
        if self.submit_code == 0:
            return "LOCAL_QUEUED"
        if (
            self.submit_code is not None
            and self.submit_code < 0
            and self.callback_history_complete
            and not self.callback_received
        ):
            return "LOCAL_REJECTED"
        return "UNKNOWN"

    @property
    def provider_ack(self) -> bool:
        """This local SDK completion can never claim provider acknowledgement."""

        return False


__all__ = [
    "CtpManagedNativeDispatchCompletionV1",
    "CtpManagedNativeDispatchRequestV1",
]
