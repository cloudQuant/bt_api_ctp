"""CTP source-field consistency for the SDK risk-order composition.

The caller retains provider selection, source provenance, request identity,
and authorization. This projection only checks CTP native direction/offset
codes against the already supplied neutral records and requested dimensions.
"""


def ctp_source_position_dimensions_error(
    source_values,
    normalized_record,
    *,
    projection_source,
    position_effect,
    position_side,
    position_bucket,
    canonical_source_position_side,
):
    """Return the existing finite error code, or ``None`` when fields agree."""
    if projection_source != "ctp_side_offset_v1" or position_effect != "CLOSE":
        return "PROVIDER_POSITION_DIMENSIONS_UNPROVEN"
    direction_code = source_values.get("ctp_direction_code")
    offset_code = source_values.get("ctp_offset_code")
    direction = {"0": "buy", "1": "sell"}.get(direction_code)
    offset_projection = {
        "1": ("close", "ALL"),
        "3": ("close_today", "TODAY"),
        "4": ("close_yesterday", "YESTERDAY"),
    }.get(offset_code)
    if direction is None or offset_projection is None:
        return "PROVIDER_POSITION_DIMENSIONS_UNPROVEN"
    expected_side = "LONG" if direction == "sell" else "SHORT"
    expected_offset, expected_bucket = offset_projection
    if (
        str(source_values.get("side", "")).strip().lower() != direction
        or source_values.get("offset") != expected_offset
        or source_values.get("position_side") != expected_side
        or source_values.get("position_bucket") != expected_bucket
        or normalized_record.get("side") != direction
        or normalized_record.get("offset") != expected_offset
        or canonical_source_position_side(
            normalized_record.get("position_side")
            if type(normalized_record.get("position_side")) is str
            else None
        )
        != expected_side
        or normalized_record.get("position_bucket") != expected_bucket
    ):
        return "PROVIDER_POSITION_DIMENSIONS_MISMATCH"
    requested_nondefault = (position_side, position_bucket) != ("NET", "ALL")
    if requested_nondefault and (position_side, position_bucket) != (
        expected_side,
        expected_bucket,
    ):
        return "PROVIDER_POSITION_DIMENSIONS_MISMATCH"
    return None
