# ruff: noqa: UP045
"""Unverified CTP 6.7.7 instrument-status scalar projection, not a live gate.

Input is a plain dictionary of the seven selected native fields. The three
other fields in the CTP struct are allowed but ignored; unknown keys are
rejected. This isolated projector does not consume callbacks, authenticate
sources or establish an exchange, instrument scope, session, trading date,
observation order, current validity, or trading authorization. EnterTime is
only a wall-clock text candidate with no date or timezone. TradingSegmentSN is
a segment field, not a global sequence. The FUSE enter reason does not by
itself imply the instrument is currently halted. Python binding types and
actual callback delivery are not verified by this module.
The segment number accepts nonnegative signed-32-bit integers only as a
conservative local convention, not a claim about native valid values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_STATES = {
    "0": "BEFORE_TRADING",
    "1": "NO_TRADING",
    "2": "CONTINUOUS",
    "3": "AUCTION_ORDERING",
    "4": "AUCTION_BALANCE",
    "5": "AUCTION_MATCH",
    "6": "CLOSED",
    "7": "TRANSACTION_PROCESSING",
}
_REASONS = {"1": "AUTOMATIC", "2": "MANUAL", "3": "FUSE"}
_SELECTED = (
    "ExchangeID",
    "ExchangeInstID",
    "InstrumentID",
    "InstrumentStatus",
    "TradingSegmentSN",
    "EnterTime",
    "EnterReason",
)
_OTHER_STRUCT_FIELDS = frozenset({"reserve1", "SettlementGroupID", "reserve2"})
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\Z", re.ASCII)


@dataclass(frozen=True)
class InstrumentStatusCandidate:
    status: str
    instrument_status: str = "UNKNOWN"
    enter_reason: str = "UNKNOWN"
    exchange_id: Optional[str] = None
    exchange_instrument_id: Optional[str] = None
    instrument_id: Optional[str] = None
    trading_segment_sn: Optional[int] = None
    enter_time: Optional[str] = None
    missing_fields: tuple[str, ...] = ()
    invalid_fields: tuple[str, ...] = ()
    verification: str = "UNVERIFIED"
    scope: str = "UNKNOWN"
    trading_day: str = "UNKNOWN"
    timestamp: str = "UNKNOWN"
    source: str = "UNKNOWN"
    session: str = "UNKNOWN"
    order: str = "UNKNOWN"
    current_validity: str = "UNKNOWN"
    authorization: str = "NOT_AUTHORIZED"


def _identifier(value: object, max_length: int) -> bool:
    # ASCII printable scalar candidates only, no whitespace or C-string NUL.
    return (
        type(value) is str
        and 0 < len(value) <= max_length
        and all(0x21 <= ord(char) <= 0x7E for char in value)
    )


def project_instrument_status(fields: object) -> InstrumentStatusCandidate:
    """Project selected native scalar fields; unavailable/unknown stays unknown."""
    if type(fields) is not dict or any(type(key) is not str for key in fields):
        return InstrumentStatusCandidate("UNKNOWN", invalid_fields=("INPUT",))
    if not set(fields) <= set(_SELECTED) | _OTHER_STRUCT_FIELDS:
        return InstrumentStatusCandidate("UNKNOWN", invalid_fields=("INPUT_FIELDS",))
    missing = []
    invalid = []
    values = {}
    for key in _SELECTED:
        if (
            key not in fields
            or fields[key] is None
            or (type(fields[key]) is str and fields[key] == "")
        ):
            missing.append(key)
            continue
        value = fields[key]
        if key in ("ExchangeID", "ExchangeInstID", "InstrumentID"):
            valid = _identifier(value, 8 if key == "ExchangeID" else 80)
        elif key == "InstrumentStatus":
            valid = type(value) is str and value in _STATES
        elif key == "EnterReason":
            valid = type(value) is str and value in _REASONS
        elif key == "TradingSegmentSN":
            valid = type(value) is int and 0 <= value <= 2**31 - 1
        else:
            valid = type(value) is str and _CLOCK.fullmatch(value) is not None
        if valid:
            values[key] = value
        else:
            invalid.append(key)
    return InstrumentStatusCandidate(
        status="UNKNOWN" if invalid else "INCOMPLETE" if missing else "CANDIDATE",
        instrument_status=_STATES.get(values.get("InstrumentStatus"), "UNKNOWN"),
        enter_reason=_REASONS.get(values.get("EnterReason"), "UNKNOWN"),
        exchange_id=values.get("ExchangeID"),
        exchange_instrument_id=values.get("ExchangeInstID"),
        instrument_id=values.get("InstrumentID"),
        trading_segment_sn=values.get("TradingSegmentSN"),
        enter_time=values.get("EnterTime"),
        missing_fields=tuple(missing),
        invalid_fields=tuple(invalid),
    )
