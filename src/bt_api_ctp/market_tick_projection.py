"""Project native CTP ticker observations into the neutral SDK tick contract."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bt_api_base.market_protocol import MarketSubscriptionKey
from bt_api_base.market_tick_protocol import (
    MarketClockQuality,
    MarketFreshnessStatus,
    MarketTickPayload,
)
from bt_api_ctp.containers.ctp.ctp_ticker import CtpTickerData

_CTP_TZ = timezone(timedelta(hours=8))
_UTC = timezone.utc
_FRESHNESS_REASONS = (
    "GENERIC_EVENT_TIME_NOT_BOUND",
    "SOURCE_CLOCK_CALIBRATION_NOT_PROVIDED",
)


def _finite_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ValueError("invalid market number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid market number")
    return number


def _utc_text(value: Any) -> Optional[str]:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(_UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _iso_date(value: Any) -> Optional[str]:
    if type(value) is not str or not value:
        return None


def _volume_quality(value: Any) -> str:
    """Project provider volume status into a small neutral vocabulary."""

    if type(value) is not str:
        return "unavailable"
    normalized = value.casefold()
    if normalized == "continuous":
        return "continuous"
    if normalized == "baseline":
        return "baseline"
    if normalized in {"out_of_order", "connection_or_trading_day_reset", "volume_reset"}:
        return "discontinuous"
    return "unavailable"


def _quality_flags(values: Any) -> tuple[str, ...]:
    """Convert CTP-local diagnostics into stable neutral quality categories."""

    if not isinstance(values, (tuple, list)):
        return ("PROVIDER_QUALITY_INVALID",)
    normalized = {
        "INVALID_EVENT_TIME": "EVENT_TIME_INVALID",
        "AMBIGUOUS_EVENT_DATE": "EVENT_DATE_AMBIGUOUS",
        "OUT_OF_ORDER": "SOURCE_SEQUENCE_OUT_OF_ORDER",
        "VOLUME_GAP": "VOLUME_DISCONTINUITY",
    }
    result = []
    for value in values:
        if type(value) is not str or not value:
            result.append("PROVIDER_QUALITY_INVALID")
        else:
            result.append(normalized.get(value, "PROVIDER_QUALITY_FAILURE"))
    return tuple(sorted(set(result)))
    try:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def _provider_event_time(data: Mapping[str, Any]) -> Optional[str]:
    """Keep a provider-reported time only when it is derived from its event date.

    The result remains an unverified provider observation. This projection
    never qualifies it for freshness or strategy dispatch.
    """

    if data.get("event_time_source") != "action_day":
        return None
    action_day = data.get("action_day")
    update_time = data.get("update_time")
    millisec = data.get("update_millisec")
    if (
        type(action_day) is not str
        or type(update_time) is not str
        or type(millisec) is not int
        or not 0 <= millisec <= 999
    ):
        return None
    try:
        event_time = datetime.strptime(
            "%s %s" % (action_day, update_time), "%Y%m%d %H:%M:%S"
        ).replace(microsecond=millisec * 1000, tzinfo=_CTP_TZ)
    except ValueError:
        return None
    reported = data.get("event_time_utc")
    if type(reported) is not datetime or reported.tzinfo is None or reported.utcoffset() is None:
        return None
    if reported.astimezone(_UTC) != event_time.astimezone(_UTC):
        return None
    return _utc_text(event_time)


def project_ctp_market_tick(
    item: object,
    key: MarketSubscriptionKey,
    source_scope: object,
    receipt: object,
) -> MarketTickPayload:
    """Map one receipt-bound CTP ticker into a neutral, not-yet-bound payload.

    ``source_scope`` and ``receipt`` are produced by the CTP managed callback
    path. Source/cursor authentication is added later by the consuming SDK
    after it receives the corresponding market frame.
    """

    if type(item) is not CtpTickerData or type(key) is not MarketSubscriptionKey:
        raise ValueError("invalid CTP tick input")
    if not isinstance(receipt, Mapping) or source_scope is None:
        raise ValueError("missing CTP callback receipt")
    try:
        data = item.get_all_data()
    except Exception as exc:
        raise ValueError("invalid CTP ticker data") from exc
    instrument = key.instrument
    if (
        type(data) is not dict
        or instrument.provider != "ctp"
        or key.channel != "tick"
        or key.depth is not None
        or key.timeframe is not None
        or data.get("schema_version") != "ctp.quote.v2"
        or data.get("exchange_name") != "CTP"
        or data.get("exchange_id") != instrument.venue
        or data.get("asset_type") != instrument.product_type
        or data.get("instrument_id") != instrument.instrument_id
        or data.get("symbol") != instrument.native_symbol
        or receipt.get("symbol") != instrument.native_symbol
        or receipt.get("source") != "ctp.native.md"
        or receipt.get("source") != getattr(source_scope, "source", None)
        or receipt.get("connection_generation")
        != getattr(source_scope, "connection_generation", None)
        or receipt.get("clock_domain_id") != getattr(source_scope, "clock_domain_id", None)
        or receipt.get("clock_source_session_id")
        != getattr(source_scope, "clock_source_session_id", None)
        or receipt.get("clock_source") != getattr(source_scope, "clock_source", None)
        or receipt.get("connection_generation") != data.get("connection_generation")
        or receipt.get("subscription_epoch") != data.get("subscription_epoch")
        or receipt.get("ingest_seq") != data.get("ingest_seq")
        or receipt.get("clock_domain_id") != data.get("clock_domain_id")
        or receipt.get("source") != data.get("source")
        or type(receipt.get("rules_hash")) is not str
        or not receipt.get("rules_hash")
        or receipt.get("source_clock_quality") != data.get("source_clock_quality")
        or receipt.get("receive_clock_quality") != data.get("receive_clock_quality")
        or receipt.get("source_clock_error_ms") != data.get("source_clock_error_ms")
        or receipt.get("receive_clock_error_ms") != data.get("receive_clock_error_ms")
        or receipt.get("freshness_verified") != data.get("freshness_verified")
        or receipt.get("source_clock_quality") != "unknown"
        or receipt.get("receive_clock_quality") != "unknown"
        or receipt.get("source_clock_error_ms") is not None
        or receipt.get("receive_clock_error_ms") is not None
        or receipt.get("freshness_verified") is not False
        or receipt.get("execution_qualified") is not False
        or data.get("execution_eligible") is not False
    ):
        raise ValueError("CTP tick identity or receipt mismatch")

    source = receipt.get("source")
    clock_domain_id = receipt.get("clock_domain_id")
    clock_source_session_id = receipt.get("clock_source_session_id")
    clock_source = receipt.get("clock_source")
    received_at = _utc_text(data.get("recv_time_utc"))
    received_monotonic = data.get("recv_monotonic_ns")
    connection_generation = receipt.get("connection_generation")
    sequence = receipt.get("ingest_seq")
    subscription_epoch = receipt.get("subscription_epoch")
    if (
        type(received_at) is not str
        or type(received_monotonic) is not int
        or received_monotonic <= 0
        or type(connection_generation) is not int
        or connection_generation <= 0
        or type(sequence) is not int
        or sequence <= 0
        or type(subscription_epoch) is not int
        or subscription_epoch <= 0
        or any(type(value) is not str or not value for value in (
            source,
            clock_domain_id,
            clock_source_session_id,
            clock_source,
        ))
    ):
        raise ValueError("invalid CTP tick binding facts")

    quality_flags = data.get("quality_flags")
    if not isinstance(quality_flags, (tuple, list)) or any(
        type(flag) is not str or not flag for flag in quality_flags
    ):
        raise ValueError("invalid CTP tick quality facts")
    event_time = _provider_event_time(data)
    volume_quality = _volume_quality(data.get("volume_quality"))
    continuity_status = (
        "continuous" if data.get("continuity_status") == "continuous" else "gap"
    )
    return MarketTickPayload(
        instrument=instrument,
        last_price=_finite_number(data.get("last_price")),
        bid_price=_finite_number(data.get("bid_price")),
        ask_price=_finite_number(data.get("ask_price")),
        bid_size=_finite_number(data.get("bid_volume")),
        ask_size=_finite_number(data.get("ask_volume")),
        lower_limit=_finite_number(data.get("lower_limit_price")),
        upper_limit=_finite_number(data.get("upper_limit_price")),
        turnover=_finite_number(data.get("turnover")),
        open_interest=_finite_number(data.get("open_interest")),
        delta_volume=_finite_number(data.get("delta_volume")),
        cumulative_volume=_finite_number(data.get("cum_volume")),
        volume_complete=data.get("volume_complete"),
        volume_quality=volume_quality,
        continuity_status=continuity_status,
        trading_date=_iso_date(data.get("trading_day")),
        event_session_date=_iso_date(data.get("action_day")),
        provider_event_time_utc=event_time,
        event_time_provenance="provider_reported" if event_time is not None else "unavailable",
        received_at_utc=received_at,
        received_monotonic_ns=received_monotonic,
        provider_source=source,
        quality_contract_ref=receipt["rules_hash"],
        provider_connection_generation=connection_generation,
        provider_sequence=sequence,
        provider_subscription_epoch=subscription_epoch,
        clock_domain_id=clock_domain_id,
        clock_source_session_id=clock_source_session_id,
        clock_source=clock_source,
        source_clock_quality=MarketClockQuality.UNKNOWN,
        receive_clock_quality=MarketClockQuality.UNKNOWN,
        source_clock_error_ms=None,
        receive_clock_error_ms=None,
        freshness_status=MarketFreshnessStatus.UNAVAILABLE,
        freshness_reason_codes=_FRESHNESS_REASONS,
        execution_eligible=False,
        quality_flags=_quality_flags(quality_flags),
    )


__all__ = ["project_ctp_market_tick"]
