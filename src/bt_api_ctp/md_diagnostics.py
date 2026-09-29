"""Value-free observations for market-data login callbacks.

These values describe what the callback looked like.  They are never an
authentication or native-session readiness proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any


class MdLoginCallbackDisposition(str, Enum):
    NONE = "none"
    STALE_SPI = "stale_spi"
    GENERATION_MISMATCH = "generation_mismatch"
    REQUEST_ID_TYPE_INVALID = "request_id_type_invalid"
    REQUEST_ID_MISMATCH = "request_id_mismatch"
    NONTERMINAL = "nonterminal"
    ACCEPTED = "accepted"
    PROVIDER_REJECTED = "provider_rejected"
    IDENTITY_REJECTED = "identity_rejected"
    TERMINAL = "terminal"


class MdLoginRequestIdRelation(str, Enum):
    NOT_OBSERVED = "not_observed"
    INVALID = "invalid"
    ZERO = "zero"
    LOWER = "lower"
    EQUAL = "equal"
    HIGHER = "higher"


class MdLoginResponseErrorStatus(str, Enum):
    NOT_OBSERVED = "not_observed"
    MISSING = "missing"
    INVALID = "invalid"
    ZERO = "zero"
    NONZERO = "nonzero"


class MdLoginBrokerIdShape(str, Enum):
    NOT_OBSERVED = "not_observed"
    UNREADABLE = "unreadable"
    EMPTY = "empty"
    ASCII_MISMATCH = "ascii_mismatch"
    NONASCII_OR_REPLACEMENT = "nonascii_or_replacement"
    WHITESPACE_OR_CONTROL = "whitespace_or_control"
    EXACT_MATCH = "exact_match"


class MdLoginUserIdShape(str, Enum):
    NOT_OBSERVED = "not_observed"
    UNREADABLE = "unreadable"
    EMPTY = "empty"
    ASCII_MISMATCH = "ascii_mismatch"
    NONASCII_OR_REPLACEMENT = "nonascii_or_replacement"
    WHITESPACE_OR_CONTROL = "whitespace_or_control"
    EXACT_MATCH = "exact_match"


class MdLoginTradingDayShape(str, Enum):
    NOT_OBSERVED = "not_observed"
    UNREADABLE = "unreadable"
    EMPTY = "empty"
    INVALID_FORMAT = "invalid_format"
    INVALID_CALENDAR = "invalid_calendar"
    VALID = "valid"


class MdLoginNativeFieldShape(str, Enum):
    """Reserved for an ABI-verified raw-field reader; Python cannot infer it."""

    NOT_OBSERVED = "not_observed"
    UNREADABLE = "unreadable"
    EMPTY = "empty"
    NONEMPTY_TERMINATED = "nonempty_terminated"
    UNTERMINATED = "unterminated"


@dataclass(frozen=True)
class MdLoginCallbackDiagnostic:
    callback_count: int
    disposition: MdLoginCallbackDisposition
    request_id_relation: MdLoginRequestIdRelation = MdLoginRequestIdRelation.NOT_OBSERVED
    response_error_status: MdLoginResponseErrorStatus = MdLoginResponseErrorStatus.NOT_OBSERVED
    broker_id_shape: MdLoginBrokerIdShape = MdLoginBrokerIdShape.NOT_OBSERVED
    user_id_shape: MdLoginUserIdShape = MdLoginUserIdShape.NOT_OBSERVED
    trading_day_shape: MdLoginTradingDayShape = MdLoginTradingDayShape.NOT_OBSERVED
    native_broker_id_shape: MdLoginNativeFieldShape = MdLoginNativeFieldShape.NOT_OBSERVED
    native_user_id_shape: MdLoginNativeFieldShape = MdLoginNativeFieldShape.NOT_OBSERVED


def request_id_relation(value: Any, expected: Any) -> MdLoginRequestIdRelation:
    if type(expected) is not int or expected < 0:
        return MdLoginRequestIdRelation.NOT_OBSERVED
    if type(value) is not int:
        return MdLoginRequestIdRelation.INVALID
    if value == 0:
        return MdLoginRequestIdRelation.ZERO
    if value < expected:
        return MdLoginRequestIdRelation.LOWER
    if value > expected:
        return MdLoginRequestIdRelation.HIGHER
    return MdLoginRequestIdRelation.EQUAL


def response_error_status(
    response_info: Any,
) -> tuple[MdLoginResponseErrorStatus, int | None]:
    if response_info is None:
        return MdLoginResponseErrorStatus.MISSING, None
    try:
        value = getattr(response_info, "ErrorID")
    except AttributeError:
        return MdLoginResponseErrorStatus.MISSING, None
    except Exception:
        return MdLoginResponseErrorStatus.INVALID, None
    if type(value) is not int:
        return MdLoginResponseErrorStatus.INVALID, None
    if value == 0:
        return MdLoginResponseErrorStatus.ZERO, 0
    return MdLoginResponseErrorStatus.NONZERO, value


def source_text(response: Any, name: str) -> tuple[str, bool]:
    """Read a callback field without retaining its value in a diagnostic."""

    if response is None:
        return "", False
    try:
        value = getattr(response, name)
    except Exception:
        return "", False
    if value is None:
        return "", False
    try:
        if type(value) is str:
            return value, True
        if type(value) is bytes:
            return value.decode("utf-8", errors="replace"), True
    except Exception:
        pass
    return "", False


def identity_shape(text: str, readable: bool, expected: str, enum_type: type[Enum]) -> Enum:
    if not readable:
        return enum_type["UNREADABLE"]
    if not text:
        return enum_type["EMPTY"]
    if not text.isascii():
        return enum_type["NONASCII_OR_REPLACEMENT"]
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return enum_type["WHITESPACE_OR_CONTROL"]
    if text == expected:
        return enum_type["EXACT_MATCH"]
    return enum_type["ASCII_MISMATCH"]


def trading_day_shape(text: str, readable: bool) -> MdLoginTradingDayShape:
    if not readable:
        return MdLoginTradingDayShape.UNREADABLE
    if not text:
        return MdLoginTradingDayShape.EMPTY
    if len(text) != 8 or not text.isascii() or not text.isdigit():
        return MdLoginTradingDayShape.INVALID_FORMAT
    try:
        date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return MdLoginTradingDayShape.INVALID_CALENDAR
    return MdLoginTradingDayShape.VALID
