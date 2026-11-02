"""CTP quote V2 timestamp projection without local-clock substitution."""

from __future__ import annotations

from datetime import datetime


def project_quote_v2_timestamps(row, *, pick, seconds):
    """Project native fields without asserting source-clock or freshness proof."""
    def timestamp(value):
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return seconds(text)
            except (TypeError, ValueError):
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    return None
                return parsed.timestamp()
        return seconds(value)

    freshness = row.get("freshness") or {}
    if not isinstance(freshness, dict):
        freshness = {}
    try:
        exchange_time = timestamp(
            pick(row, "event_time_utc", "event_time", default=freshness.get("observed_at"))
        )
    except (TypeError, ValueError):
        exchange_time = None
    try:
        received_wall = timestamp(
            pick(row, "recv_time_utc", "received_wall_time", "receive_time")
        )
    except (TypeError, ValueError):
        received_wall = None
    received_monotonic = pick(
        row,
        "received_monotonic_ns",
        "local_monotonic_ns",
        "receive_monotonic_ns",
        "recv_monotonic_ns",
    )
    if received_monotonic in (None, ""):
        received_monotonic = 0
    else:
        try:
            received_monotonic = int(received_monotonic)
        except (TypeError, ValueError):
            received_monotonic = 0
    stale = bool(pick(row, "stale", default=freshness.get("stale", False)))
    stale_reason = pick(row, "stale_reason", default=freshness.get("stale_reason"))
    return {
        "timestamp": exchange_time,
        "local_time": received_wall,
        "exchange_time": exchange_time,
        "received_wall_time": received_wall,
        "received_monotonic_ns": received_monotonic,
        "clock_domain_id": str(pick(row, "clock_domain_id", default="")),
        "source": str(pick(row, "source", default="unknown")),
        "stale": stale,
        "stale_reason": stale_reason,
    }


__all__ = ("project_quote_v2_timestamps",)
