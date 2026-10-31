"""Source-owned registered-simulation receipt records for the CTP route.

These records describe one Store-owned dispatch, query or cancellation
observation. They are diagnostics and identity bindings, never an
authorization, a provider attestation or a retry command. ``_store``,
``_issuer`` and ``_seal`` are caller-supplied opaque values: this module keeps
the references without inspecting them and gives no object any authority.

This module is intentionally free of framework and provider imports. The
records it carries keep their original field names, defaults and invariants so
a durable consumer can adopt one owner without a cross-repository identity
check.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _exact_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty exact text")
    return value


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


@dataclass(frozen=True)
class RegisteredSimDispatchEvidence:
    """One native dispatch result; diagnostic only and never a retry command.

    ``status`` is ``KNOWN`` only when the SDK method returned its documented
    integer success code (zero). Any exception or other return is ``UNKNOWN``.
    The Store never automatically resends a native method, including when the
    outcome is UNKNOWN. UNKNOWN evidence is retained until the process ends.
    """

    sequence: int
    operation: str
    status: str
    request_id: int
    instrument_id: str
    exchange_id: str
    order_ref: str | None
    order_sys_id: str | None
    front_id: int | None
    session_id: int | None
    broker_id: str | None
    investor_id: str | None
    user_id: str | None
    native_return_code: int | None
    error_type: str | None
    observed_at_utc: str

    @property
    def resend_permitted(self) -> bool:
        """Native requests are never automatically safe to replay."""
        return False


@dataclass(frozen=True)
class RegisteredSimOrderIdentityQueryReceipt:
    """Current Store-owned complete orders query used for durable identity binding.

    This in-process receipt records a query that the Store revalidated against
    its live SDK session. It does not claim that the Journal independently
    authenticates a provider or the physical query source.
    """

    request_id: int
    connection_generation: int
    account_fingerprint: str
    trading_day: str
    started_at_utc: datetime
    completed_at_utc: datetime
    front_id: int
    session_id: int
    broker_id: str
    investor_id: str
    rows_sha256: str
    rows: tuple[Mapping, ...]
    source_expires_monotonic: float
    _store: Any
    _issuer: Any

    def validate_shape(self) -> None:
        """Check this receipt's own field shapes and raise ``ValueError``.

        These are the pure assertions a durable consumer previously performed
        inline: no clock, Store, SDK, session or provider state is read, and no
        field is resynthesized. The caller keeps its own ``_store`` identity,
        issuer-continuity and ``source_expires_monotonic`` freshness
        comparisons because those require live state that this record cannot
        trust. Passing does not authenticate the query source.
        """
        if type(self.request_id) is not int or self.request_id <= 0:
            raise ValueError(
                "CANNOT_ORDER_IDENTITY_RECEIPT_REQUEST_ID: request_id must be a positive exact int"
            )
        if type(self.connection_generation) is not int or self.connection_generation <= 0:
            raise ValueError(
                "CANNOT_ORDER_IDENTITY_RECEIPT_CONNECTION_GENERATION: "
                "connection_generation must be a positive exact int"
            )
        if self._issuer is None:
            raise ValueError(
                "CANNOT_ORDER_IDENTITY_RECEIPT_ISSUER: receipt requires its minting issuer"
            )
        for name, code in (
            ("account_fingerprint", "CANNOT_ORDER_IDENTITY_RECEIPT_ACCOUNT_FINGERPRINT"),
            ("broker_id", "CANNOT_ORDER_IDENTITY_RECEIPT_BROKER_ID"),
            ("investor_id", "CANNOT_ORDER_IDENTITY_RECEIPT_INVESTOR_ID"),
        ):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"{code}: {name} must be non-blank exact text")
        if type(self.trading_day) is not str or re.fullmatch(r"[0-9]{8}", self.trading_day) is None:
            raise ValueError(
                "CANNOT_ORDER_IDENTITY_RECEIPT_TRADING_DAY: "
                "trading_day must be an exact eight-digit YYYYMMDD"
            )
        for name, code in (
            ("started_at_utc", "CANNOT_ORDER_IDENTITY_RECEIPT_STARTED_AT_UTC"),
            ("completed_at_utc", "CANNOT_ORDER_IDENTITY_RECEIPT_COMPLETED_AT_UTC"),
        ):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{code}: {name} must be a timezone-aware datetime")
        if self.started_at_utc > self.completed_at_utc:
            raise ValueError(
                "CANNOT_ORDER_IDENTITY_RECEIPT_TIME_ORDER: "
                "started_at_utc must not exceed completed_at_utc"
            )
        for name, code in (
            ("front_id", "CANNOT_ORDER_IDENTITY_RECEIPT_FRONT_ID"),
            ("session_id", "CANNOT_ORDER_IDENTITY_RECEIPT_SESSION_ID"),
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{code}: {name} must be a positive exact int")
        if type(self.rows) is not tuple:
            raise ValueError("CANNOT_ORDER_IDENTITY_RECEIPT_ROWS: rows must be an exact tuple")
        if type(self.rows_sha256) is not str or _HASH_RE.fullmatch(self.rows_sha256) is None:
            raise ValueError(
                "CANNOT_ORDER_IDENTITY_RECEIPT_ROWS_DIGEST: rows_sha256 must be lowercase SHA-256"
            )
        if type(self.source_expires_monotonic) not in (int, float) or not math.isfinite(
            self.source_expires_monotonic
        ):
            raise ValueError(
                "CANNOT_ORDER_IDENTITY_RECEIPT_SOURCE_EXPIRY: "
                "source_expires_monotonic must be a finite number"
            )


@dataclass(frozen=True)
class RegisteredSimCancelTarget:
    """Exact one-shot CTP target issued by a Journal-validated Store path."""

    order_ref: str
    order_sys_id: str
    instrument_id: str
    exchange_id: str
    broker_id: str
    investor_id: str
    front_id: int
    session_id: int
    account_fingerprint: str
    connection_generation: int
    trading_day: str
    query_request_id: int
    query_rows_sha256: str
    matched_row_sha256: str
    source_expires_monotonic: float
    _issuer: Any
    _seal: Any

    def __post_init__(self) -> None:
        for name in (
            "order_ref",
            "order_sys_id",
            "instrument_id",
            "exchange_id",
            "broker_id",
            "investor_id",
            "account_fingerprint",
            "query_rows_sha256",
            "matched_row_sha256",
        ):
            _exact_text(getattr(self, name), name)
        for name in (
            "front_id",
            "session_id",
            "connection_generation",
            "query_request_id",
        ):
            if not _positive_int(getattr(self, name)):
                raise ValueError(f"{name} must be a positive int")
        if (
            type(self.trading_day) is not str
            or len(self.trading_day) != 8
            or not self.trading_day.isdigit()
        ):
            raise ValueError("trading_day must be an exact YYYYMMDD")
        if any(
            _HASH_RE.fullmatch(getattr(self, name)) is None
            for name in ("query_rows_sha256", "matched_row_sha256")
        ):
            raise ValueError("cancel target query digests must be lowercase SHA-256")
        if (
            type(self.source_expires_monotonic) not in (int, float)
            or not math.isfinite(self.source_expires_monotonic)
            or self.source_expires_monotonic <= 0
        ):
            raise ValueError("source_expires_monotonic must be a finite positive deadline")
        if self._issuer is None or self._seal is None:
            raise ValueError("cancel target requires its validated Store issuer")


@dataclass(frozen=True)
class RegisteredSimUnknownRetirement:
    """Result of retiring one exact, durably reconciled UNKNOWN insert."""

    status: str
    intent_id: str
    reconciliation_id: str
    disarmed: bool


@dataclass(frozen=True)
class RegisteredSimRouteEligibility:
    """Read-only route facts; these fields do not grant native permission.

    Unknown order IDs describe only this wrapper's observed in-process
    dispatches. They do not claim a complete account reconciliation; callers
    must separately require the source-owned full-scope query facts.
    """

    route_name: str
    instrument_id: str
    exchange_id: str
    account_fingerprint: str | None
    connection_generation: int | None
    trading_day: str | None
    allow_writes: bool
    frozen: bool
    sdk_frozen: bool | None
    unknown_order_ids: tuple[str, ...] | None
    unknown_order_count: int | None
    dispatching_order_ids: tuple[str, ...] | None
    capability_observed: bool
    sdk_capability_managed: bool | None
    sdk_capability_armed: bool | None
    sdk_capability_instrument: str | None
    sdk_authorized_instruments: tuple[str, ...] | None
    capability_matches_route: bool | None
    instrument_query_complete: bool
    instrument_eligible: bool | None
    reason_codes: tuple[str, ...]
    arm_template_configured: bool | None = None
    arm_templates_configured: bool | None = None
    arm_api_available: bool | None = None
    current_session_account_fingerprint: str | None = None
    current_session_connection_generation: int | None = None
    current_session_trading_day: str | None = None
    current_session_trading_ready: bool | None = None
    current_session_front_id: int | None = None
    current_session_id: int | None = None
    current_session_read_only_ready: bool | None = None
    native_admission_required: bool = True
    arm_template_membership: bool | None = None


__all__ = [
    "RegisteredSimCancelTarget",
    "RegisteredSimDispatchEvidence",
    "RegisteredSimOrderIdentityQueryReceipt",
    "RegisteredSimRouteEligibility",
    "RegisteredSimUnknownRetirement",
]
