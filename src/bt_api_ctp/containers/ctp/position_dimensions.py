"""Position-side and close-bucket facts derived from native CTP fields."""

from __future__ import annotations

from collections.abc import Mapping

from ._normalization import ctp_code


def ctp_position_dimension_projection(
    raw: object,
    *,
    direction_key: str,
    offset_key: str,
    normalized_side: object,
    normalized_offset: object,
    order_offset: bool,
) -> dict[str, str]:
    """Project CTP side/offset provenance without guessing unknown variants.

    CTP orders and trades do not carry an independent position-side field.
    For closing actions, their native direction and offset pair is the source
    of truth. Unknown values, forced-close variants, and disagreement with the
    container's normalized direction/offset stay explicitly unresolved.
    Opening actions do not identify a close bucket and therefore add no fields.
    """

    if not isinstance(raw, Mapping):
        return {}

    def code(key: str) -> str:
        value = raw.get(key)
        if value is None:
            return ""
        text = str(value).strip().strip("\x00")
        return ctp_code(text) if text else ""

    direction_code = code(direction_key)
    offset_text = code(offset_key)
    offset_code = offset_text[:1] if order_offset else offset_text
    direction = {"0": "buy", "1": "sell"}.get(direction_code)
    normalized_direction = str(normalized_side or "").strip().lower()
    known_offset_codes = {"0", "1", "2", "3", "4", "5", "6"}
    if direction is None or normalized_direction != direction:
        return {
            "position_dimensions_source": "ctp_side_offset_unresolved_v1",
            "ctp_direction_code": direction_code if direction_code in {"0", "1"} else "UNKNOWN",
            "ctp_offset_code": offset_code if offset_code in known_offset_codes else "UNKNOWN",
            "position_side": "UNRESOLVED",
            "position_bucket": "UNRESOLVED",
        }

    if offset_code == "0":
        return {}

    close_projection = {
        "1": ("close", "ALL"),
        "3": ("close_today", "TODAY"),
        "4": ("close_yesterday", "YESTERDAY"),
    }.get(offset_code)
    if (
        close_projection is None
        or str(normalized_offset or "").strip().lower() != close_projection[0]
    ):
        return {
            "position_dimensions_source": "ctp_side_offset_unresolved_v1",
            "ctp_direction_code": direction_code,
            "ctp_offset_code": offset_code if offset_code in known_offset_codes else "UNKNOWN",
            "position_side": "UNRESOLVED",
            "position_bucket": "UNRESOLVED",
        }

    return {
        "position_dimensions_source": "ctp_side_offset_v1",
        "ctp_direction_code": direction_code,
        "ctp_offset_code": offset_code,
        "position_side": "LONG" if direction == "sell" else "SHORT",
        "position_bucket": close_projection[1],
    }
