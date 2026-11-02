"""CTP terminal-order query value contracts for managed execution.

These values do not issue evidence or grant dispatch authority; the SDK reader
retains its source-bound readback, lifecycle and session ownership.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, ClassVar

from bt_api_execution import ContractValidationError, payload_sha256
from .managed_order_identifier import encode_ctp_provider_order_id


_MAX_EVIDENCE_TTL_NS = 5_000_000_000


_TERMINAL_ORDER_STATES = frozenset({"CANCELLED", "REJECTED"})


_CANCEL_ACTION_STATES = frozenset({"ACKNOWLEDGED", "REJECTED", "TERMINAL"})


_DIGEST_FIELDS = frozenset(
    {
        "terminal_ingress_record_digest_sha256",
        "terminal_source_digest_sha256",
        "query_filter_sha256",
        "query_records_sha256",
        "query_source_sha256",
        "query_window_sha256",
        "reservation_digest_sha256",
        "session_binding_sha256",
        "query_account_fingerprint_sha256",
        "evidence_sha256",
    }
)


def _reject(message: str, *, cause: BaseException | None = None) -> None:
    error = ContractValidationError(message)
    if cause is None:
        raise error
    raise error from cause


def _digest_text(value: object, name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(char not in "0123456789abcdef" for char in value)
    ):
        _reject("invalid CTP terminal-query " + name)


@dataclass(frozen=True, slots=True)
class CtpTerminalOrderQueryNotReadyV1:
    """A finite reason why an exact zero-fill terminal query is unavailable."""

    reason_code: str

    _ALLOWED: ClassVar[frozenset[str]] = frozenset(
        {"CALLBACK_SOURCE_NOT_READY", "ORDER_NOT_TERMINAL", "ORDER_HAS_FILL"}
    )

    def __post_init__(self) -> None:
        if type(self.reason_code) is not str or self.reason_code not in self._ALLOWED:
            raise ContractValidationError("invalid CTP terminal-query not-ready reason")


@dataclass(frozen=True, slots=True)
class CtpTerminalOrderQueryEvidenceV1:
    """A short-lived, source-bound observation of one terminal zero-fill order.

    ``to_payload`` is an audit representation only.  Callers must ask the
    issuing reader to ``readback`` this exact object before treating it as
    evidence; its hash does not authenticate a reconstructed value.
    """

    terminal_state: str
    account_key: str
    scope_key: str
    trading_day: str
    managed_intent_id: str
    runtime_order_id: str
    order_ref: str
    submit_command_id: str
    provider_order_id: str
    instrument_id: str
    exchange_id: str
    order_sys_id: str
    callback_owner_intent_id: str
    session_generation_id: str
    callback_stream_id: str
    session_binding_sha256: str
    order_source_sequence: int
    terminal_ingress_record_digest_sha256: str
    terminal_source_digest_sha256: str
    request_id: int
    front_id: int
    session_id: int
    quantity: int
    volume_traded: int
    filled_quantity: int
    trade_fact_count: int
    trade_notional: str
    commission_quality: str
    cancel_command_id: str | None
    cancel_action_state: str | None
    reservation_digest_sha256: str
    query_fence_id: str
    query_start_source_sequence: int
    query_end_source_sequence: int
    query_applied_source_sequence: int
    query_request_id: int
    query_filter_sha256: str
    query_records_sha256: str
    query_source_sha256: str
    query_window_sha256: str
    query_callback_ingress_digests: tuple[str, ...]
    query_account_fingerprint_sha256: str
    query_connection_generation: int
    query_started_at_utc: str
    query_completed_at_utc: str
    query_started_monotonic: float
    query_completed_monotonic: float
    verified_at_ns: int
    expires_at_ns: int
    evidence_sha256: str

    schema: ClassVar[str] = "ctp_terminal_order_query_evidence.v1"

    def __post_init__(self) -> None:
        for name in (
            "account_key",
            "scope_key",
            "managed_intent_id",
            "runtime_order_id",
            "submit_command_id",
            "provider_order_id",
            "instrument_id",
            "exchange_id",
            "order_sys_id",
            "callback_owner_intent_id",
            "session_generation_id",
            "callback_stream_id",
        ):
            if type(getattr(self, name)) is not str or not getattr(self, name):
                raise ContractValidationError("invalid CTP terminal-query " + name)
        if (
            type(self.terminal_state) is not str
            or self.terminal_state not in _TERMINAL_ORDER_STATES
            or type(self.trading_day) is not str
            or len(self.trading_day) != 8
            or not self.trading_day.isascii()
            or not self.trading_day.isdigit()
            or type(self.order_ref) is not str
            or len(self.order_ref) != 12
            or not self.order_ref.isascii()
            or not self.order_ref.isdigit()
        ):
            raise ContractValidationError("invalid CTP terminal-query identity")
        for name in _DIGEST_FIELDS:
            if name != "evidence_sha256":
                _digest_text(getattr(self, name), name)
        _digest_text(self.evidence_sha256, "evidence digest")
        for name in (
            "order_source_sequence",
            "request_id",
            "front_id",
            "session_id",
            "quantity",
            "query_start_source_sequence",
            "query_end_source_sequence",
            "query_applied_source_sequence",
            "query_request_id",
            "query_connection_generation",
            "verified_at_ns",
            "expires_at_ns",
        ):
            minimum = 0 if name in {"query_start_source_sequence", "query_connection_generation"} else 1
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ContractValidationError("invalid CTP terminal-query " + name)
        if (
            type(self.volume_traded) is not int
            or self.volume_traded != 0
            or type(self.filled_quantity) is not int
            or self.filled_quantity != 0
            or type(self.trade_fact_count) is not int
            or self.trade_fact_count != 0
            or type(self.trade_notional) is not str
            or self.trade_notional != "0"
            or self.commission_quality != "INCOMPLETE"
            or self.query_end_source_sequence <= self.query_start_source_sequence
            or self.query_applied_source_sequence != self.query_end_source_sequence
            or self.expires_at_ns <= self.verified_at_ns
            or self.expires_at_ns - self.verified_at_ns > _MAX_EVIDENCE_TTL_NS
            or type(self.query_fence_id) is not str
            or not self.query_fence_id
            or type(self.query_callback_ingress_digests) is not tuple
            or len(self.query_callback_ingress_digests) != 2
            or any(type(item) is not str or len(item) != 64 for item in self.query_callback_ingress_digests)
        ):
            raise ContractValidationError("invalid CTP terminal-query source window")
        for name in self.query_callback_ingress_digests:
            _digest_text(name, "query callback ingress digest")
        if self.provider_order_id != encode_ctp_provider_order_id(self.exchange_id, self.order_sys_id):
            raise ContractValidationError("CTP provider order ID differs from its native identity")
        if (
            type(self.query_started_at_utc) is not str
            or type(self.query_completed_at_utc) is not str
        ):
            raise ContractValidationError("invalid CTP terminal-query UTC timestamps")
        try:
            started_utc = datetime.fromisoformat(self.query_started_at_utc)
            completed_utc = datetime.fromisoformat(self.query_completed_at_utc)
        except (TypeError, ValueError) as error:
            raise ContractValidationError("invalid CTP terminal-query UTC timestamps") from error
        if (
            started_utc.tzinfo is None
            or started_utc.utcoffset() is None
            or completed_utc.tzinfo is None
            or completed_utc.utcoffset() is None
            or completed_utc < started_utc
            or type(self.query_started_monotonic) not in (int, float)
            or isinstance(self.query_started_monotonic, bool)
            or not math.isfinite(self.query_started_monotonic)
            or type(self.query_completed_monotonic) not in (int, float)
            or isinstance(self.query_completed_monotonic, bool)
            or not math.isfinite(self.query_completed_monotonic)
            or self.query_completed_monotonic < self.query_started_monotonic
        ):
            raise ContractValidationError("invalid CTP terminal-query clock facts")
        if self.cancel_command_id is None:
            if self.cancel_action_state is not None:
                raise ContractValidationError("CTP terminal-query cancel state lacks command ID")
        elif (
            type(self.cancel_command_id) is not str
            or not self.cancel_command_id
            or type(self.cancel_action_state) is not str
            or self.cancel_action_state not in _CANCEL_ACTION_STATES
        ):
            raise ContractValidationError("invalid CTP terminal-query cancel binding")
        unsigned = self.to_payload()
        unsigned.pop("evidence_sha256")
        if payload_sha256(unsigned) != self.evidence_sha256:
            raise ContractValidationError("CTP terminal-query evidence digest differs")

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"schema": self.schema}
        payload.update({field.name: getattr(self, field.name) for field in fields(self)})
        return payload


__all__ = ("CtpTerminalOrderQueryNotReadyV1", "CtpTerminalOrderQueryEvidenceV1")
