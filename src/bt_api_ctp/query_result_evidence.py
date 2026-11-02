"""CTP-native query result validation and bounded evidence projection.

The caller must hold and recheck its own session authorization around the
query. This module neither issues that authority nor starts a native request.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_CTP_QUERY_METHODS = {
    "account": "query_account_result",
    "positions": "query_positions_result",
    "orders": "query_orders_result",
    "trades": "query_trades_result",
    "instruments": "query_instruments_result",
    "depth_market_data": "query_depth_market_data_result",
    "margin_rate": "query_instrument_margin_rate_result",
    "commission_rate": "query_instrument_commission_rate_result",
    "option_trade_cost": "query_option_instrument_trade_cost_result",
    "option_commission_rate": "query_option_instrument_commission_rate_result",
    # The matching account/day readback can promote this session's readiness.
    "settlement_confirmation": "verify_settlement_confirmation",
}

_CTP_PUBLIC_SESSION_FIELDS = frozenset({
    "connected", "auth_state", "login_state", "settlement_state",
    "settlement_readback_verified", "read_only_ready", "trading_ready", "ready",
    "auto_settlement_confirm", "front_id", "session_id", "trading_day",
    "connection_generation", "account_fingerprint", "settlement_request_id",
    "settlement_connection_generation", "settlement_trading_day",
    "settlement_late_callback_count", "settlement_proof_source",
    "settlement_proof_query_request_id", "request_counts", "last_error",
    "environment_profile", "environment_readiness", "execution_gate_armed",
    "execution_gate_connection_generation", "execution_gate_instrument",
    "execution_gate_scope_version", "execution_gate_authorized_instruments",
    "execution_gate_proof_sha256", "execution_gate_revocation_reason",
})


def ctp_query_method_name(query_type):
    """Select the CTP-native method for a supported bounded query kind."""
    try:
        return _CTP_QUERY_METHODS[str(query_type).strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unknown CTP query_type {query_type!r}") from exc


def project_ctp_session_state(raw_state):
    """Limit native session state to the pre-existing public field set."""
    state = dict(raw_state)
    return {name: value for name, value in state.items() if name in _CTP_PUBLIC_SESSION_FIELDS}


class CtpQueryRecordsInvalid(ValueError):
    """A completed query returned a row that cannot be projected."""


def canonical_ctp_account_fingerprint(value: Any) -> str:
    """Map the native short account hash to the public receipt identity."""
    fingerprint = str(value or "").strip().lower()
    digest = fingerprint[5:] if fingerprint.startswith("acct_") else fingerprint
    if len(digest) != 16 or any(character not in "0123456789abcdef" for character in digest):
        return ""
    return f"acct_{digest}"


def inspect_ctp_query_result(result, *, query_type, expected_generation, expected_fingerprint):
    """Return the exact completed query rows and identity, or ``None``."""
    records = result.records
    request_id = result.request_id
    fingerprint = canonical_ctp_account_fingerprint(result.account_fingerprint)
    valid = bool(
        getattr(result, "request_type", None) == query_type
        and type(request_id) is int
        and request_id > 0
        and getattr(result, "complete", None) is True
        and getattr(result, "evidence_complete", None) is True
        and getattr(result, "is_last_seen", None) is True
        and getattr(result, "timed_out", None) is False
        and getattr(result, "unsupported", None) is False
        and getattr(result, "error_code", None) in (None, 0)
        and getattr(result, "late_callback_count", None) == 0
        and getattr(result, "connection_generation", None) == expected_generation
        and fingerprint == expected_fingerprint
        and isinstance(records, tuple)
    )
    return (records, fingerprint, request_id) if valid else None


def project_ctp_query_records(records, *, fingerprint, generation, trading_day):
    """Bind validated CTP rows to their query identity and redact account fields."""
    safe_rows = []
    for record in records:
        if not isinstance(record, Mapping):
            raise CtpQueryRecordsInvalid("ctp_query_records_invalid")
        row = dict(record)
        for key in ("AccountID", "InvestorID", "account_id"):
            if key in row:
                row[key] = fingerprint
        row["account_id"] = fingerprint
        row["trading_day"] = trading_day
        row["connection_generation"] = generation
        row["evidence_complete"] = True
        safe_rows.append(row)
    return tuple(safe_rows)


def ctp_recovery_row_matches_identity(row, *, account, trading_day):
    """Compare a projected CTP query row with its expected account and day."""
    account_value = next(
        (
            row[name]
            for name in ("AccountID", "InvestorID", "account_id")
            if row.get(name) not in (None, "")
        ),
        None,
    )
    day_value = next(
        (
            row[name]
            for name in ("TradingDay", "trading_day")
            if row.get(name) not in (None, "")
        ),
        None,
    )
    return str(account_value or "") == account and str(day_value or "") == trading_day


def project_ctp_active_order_rows(rows, *, fingerprint):
    """Apply the existing CTP status selection before SDK normalization.

    This projection does not establish completeness or validate native status
    semantics; the caller retains the query and authorization lifecycle.
    """
    active_rows = []
    for row in rows:
        status = str(row.get("OrderStatus", row.get("status", ""))).strip().lower()
        if status in {
            "0",
            "2",
            "4",
            "5",
            "filled",
            "completed",
            "canceled",
            "cancelled",
            "expired",
            "rejected",
        }:
            continue
        active_rows.append({**row, "account_id": fingerprint})
    return active_rows


__all__ = (
    "CtpQueryRecordsInvalid",
    "ctp_query_method_name",
    "project_ctp_session_state",
    "canonical_ctp_account_fingerprint",
    "inspect_ctp_query_result",
    "project_ctp_query_records",
    "ctp_recovery_row_matches_identity",
    "project_ctp_active_order_rows",
)
