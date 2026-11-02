"""CTP position-window field checks and instrument selection.

The caller retains source validation, transactions and authorization.
"""

from collections.abc import Mapping
from typing import Any


def ctp_window_field_value(position: Mapping[str, Any], name: str, *, error_type) -> Any:
    fields = position.get("fields")
    if not isinstance(fields, Mapping):
        raise error_type("stored CTP position fields are unavailable")
    field = fields.get(name)
    if not isinstance(field, Mapping) or field.get("state") != "value":
        raise error_type("stored CTP position field is not known")
    return field.get("value")


def ctp_position_window_instrument_rows(positions, instrument: str, *, error_type):
    matching: list[Mapping[str, Any]] = []
    for position in positions:
        if not isinstance(position, Mapping) or position.get("errors") not in ([], ()):
            raise error_type("stored CTP position row is incomplete")
        instrument_id = ctp_window_field_value(position, "InstrumentID", error_type=error_type)
        if type(instrument_id) is not str or not instrument_id:
            raise error_type("stored CTP position instrument is invalid")
        if instrument_id != instrument:
            continue
        total = ctp_window_field_value(position, "Position", error_type=error_type)
        today = ctp_window_field_value(position, "TodayPosition", error_type=error_type)
        yesterday = ctp_window_field_value(position, "YdPosition", error_type=error_type)
        direction = ctp_window_field_value(position, "PosiDirection", error_type=error_type)
        hedge = ctp_window_field_value(position, "HedgeFlag", error_type=error_type)
        if (
            any(type(value) is not int or value < 0 for value in (total, today, yesterday))
            or total != today + yesterday
            or direction not in {"2", "3"}
            or type(hedge) is not str
        ):
            raise error_type("stored CTP position quantities are invalid")
        matching.append(position)
    return tuple(matching)
