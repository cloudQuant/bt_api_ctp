"""CTP-native quote receipt checks for the SDK direct-ingress boundary.

This module returns checked fields; only the SDK issues its opaque attestation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


_CTP_QUOTE_V2_UNTRUSTED_IDENTITIES = frozenset(
    {
        "",
        "-",
        "--",
        "unknown",
        "unverified",
        "unset",
        "none",
        "null",
        "n/a",
        "na",
        "unavailable",
        "undefined",
        "missing",
        "pending",
        "tbd",
        "default",
        "placeholder",
    }
)


def _ctp_quote_v2_attestation_text(value: Any) -> str:
    """Return a usable parent-owned V2 provenance identity, if present."""

    text = str(value or "").strip()
    return "" if text.casefold() in _CTP_QUOTE_V2_UNTRUSTED_IDENTITIES else text


def _ctp_quote_v2_positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _ctp_quote_v2_nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _is_native_ctp_quote_v2_ticker(item: Any) -> bool:
    """Accept only the CTP plugin's native ticker at the direct ingress edge."""

    try:
        from bt_api_ctp.containers.ctp.ctp_ticker import CtpTickerData
    except Exception:
        # CTP is an optional plugin.  If its canonical native container cannot
        # be loaded, no object can establish the direct-ingress trust boundary.
        return False
    # Module and qualname strings are attacker-controlled Python attributes.
    # The native stream emits this exact plugin-owned class; rejecting subclasses
    # also prevents a caller from manufacturing a partially initialized ticker.
    return type(item) is CtpTickerData


def _ctp_managed_quote_v2_receipt(item: Any, stream: Any) -> Mapping[str, Any] | None:
    """Read the native stream's opaque V2 receipt without trusting config.

    The receipt verifier belongs to ``bt_api_ctp`` because only that native
    stream can bind a ticker to its callback capability and lifecycle token.
    Any missing plugin, forged ticker, copied receipt, or public stream object
    fails closed rather than falling back to quote/topic metadata.
    """

    try:
        from bt_api_ctp.feeds.live_ctp_feed import _get_ctp_managed_quote_v2_receipt
    except Exception:
        return None
    try:
        receipt = _get_ctp_managed_quote_v2_receipt(item, stream)
    except Exception:
        return None
    return receipt if isinstance(receipt, Mapping) else None


def quote_v2_attestation_fields(
    exchange_name: str,
    item: Any,
    *,
    sealed_row: Mapping[str, Any],
    feed: Any,
    streams: Any,
    market_queue: Any,
    cohort_now_monotonic_ns: int,
    cohort_now_epoch: float,
) -> dict[str, Any] | None:
    """Match a CTP native receipt to the already sealed direct-ingress row."""
    row = dict(sealed_row)
    if str(row.get("schema_version") or "") != "ctp.quote.v2":
        return None
    freshness = row.get("freshness")
    if bool(row.get("stale")) or (isinstance(freshness, Mapping) and bool(freshness.get("stale"))):
        return None
    symbol = str(
        row.get("symbol")
        or row.get("symbol_name")
        or row.get("instrument_id")
        or row.get("InstrumentID")
        or ""
    ).strip()
    row_source = _ctp_quote_v2_attestation_text(row.get("source"))
    row_rules_hash = _ctp_quote_v2_attestation_text(row.get("rules_hash"))
    row_clock_domain = _ctp_quote_v2_attestation_text(row.get("clock_domain_id"))
    row_generation = _ctp_quote_v2_positive_int(row.get("connection_generation"))
    row_epoch = _ctp_quote_v2_positive_int(row.get("subscription_epoch"))
    if not (
        symbol
        and row_source
        and row_rules_hash
        and row_clock_domain
        and row_generation
        and row_epoch
    ):
        return None

    get_session_state = getattr(feed, "get_session_state", None)
    if not callable(get_session_state):
        return None
    try:
        session = get_session_state()
    except Exception:
        return None
    if not isinstance(session, Mapping) or session.get("read_only_ready") is not True:
        return None
    session_generation = _ctp_quote_v2_positive_int(session.get("connection_generation"))
    if not session_generation:
        return None

    if not isinstance(streams, (list, tuple)):
        return None
    for stream in streams:
        if (
            str(getattr(stream, "stream_name", "")) != "ctp_market_stream"
            or getattr(stream, "data_queue", None) is not market_queue
            or getattr(stream, "_running", None) is not True
        ):
            continue
        state = getattr(stream, "state", None)
        state_value = str(getattr(state, "value", state) or "").strip().lower()
        if state_value != "authenticated":
            continue
        # ``quote_v2_metadata`` and topic fields are public input.  They
        # can describe a quote for diagnostics, but they cannot establish
        # clock quality, freshness, rule identity, or an eligible source.
        # The exact native stream must issue a one-row receipt from its
        # managed callback; no mapping or stream-local metadata fallback
        # is permitted here.
        receipt = _ctp_managed_quote_v2_receipt(item, stream)
        if not isinstance(receipt, Mapping):
            continue
        receipt_symbol = str(receipt.get("symbol") or "").strip()
        receipt_source = _ctp_quote_v2_attestation_text(receipt.get("source"))
        receipt_rules_hash = _ctp_quote_v2_attestation_text(receipt.get("rules_hash"))
        receipt_clock_domain = _ctp_quote_v2_attestation_text(receipt.get("clock_domain_id"))
        receipt_generation = _ctp_quote_v2_positive_int(receipt.get("connection_generation"))
        receipt_epoch = _ctp_quote_v2_positive_int(receipt.get("subscription_epoch"))
        receipt_seq = _ctp_quote_v2_positive_int(receipt.get("ingest_seq"))
        receipt_source_error = _ctp_quote_v2_nonnegative_number(
            receipt.get("source_clock_error_ms")
        )
        receipt_receive_error = _ctp_quote_v2_nonnegative_number(
            receipt.get("receive_clock_error_ms")
        )
        receipt_source_quality = str(receipt.get("source_clock_quality") or "").strip().lower()
        receipt_receive_quality = str(receipt.get("receive_clock_quality") or "").strip().lower()
        receipt_freshness = receipt.get("freshness_verified") is True
        if not (
            receipt.get("execution_qualified") is True
            and receipt_symbol == symbol
            and receipt_source == row_source
            and receipt_rules_hash == row_rules_hash
            and receipt_clock_domain == row_clock_domain
            and receipt_generation == row_generation
            and receipt_epoch == row_epoch
            and receipt_seq == _ctp_quote_v2_positive_int(row.get("ingest_seq"))
            and receipt_source_error
            == _ctp_quote_v2_nonnegative_number(row.get("source_clock_error_ms"))
            and receipt_receive_error
            == _ctp_quote_v2_nonnegative_number(row.get("receive_clock_error_ms"))
            and receipt_source_quality == str(row.get("source_clock_quality") or "").strip().lower()
            and receipt_receive_quality
            == str(row.get("receive_clock_quality") or "").strip().lower()
            and receipt_freshness == (row.get("freshness_verified") is True)
            and receipt_source_error is not None
            and receipt_receive_error is not None
            and receipt_source_quality == "verified"
            and receipt_receive_quality == "verified"
            and receipt_freshness
        ):
            continue
        return dict(
            exchange_name=exchange_name,
            symbol=symbol,
            source=receipt_source,
            rules_hash=receipt_rules_hash,
            clock_domain_id=receipt_clock_domain,
            connection_generation=receipt_generation,
            subscription_epoch=receipt_epoch,
            session_generation=session_generation,
            cohort_now_monotonic_ns=cohort_now_monotonic_ns,
            cohort_now_epoch=cohort_now_epoch,
            cohort_now_receive_clock_error_ms=receipt_receive_error,
            cohort_now_receive_clock_quality=receipt_receive_quality,
            cohort_now_freshness_verified=receipt_freshness,
        )
    return None


__all__ = ("quote_v2_attestation_fields",)
