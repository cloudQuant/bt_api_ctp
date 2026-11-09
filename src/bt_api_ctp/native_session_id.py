"""CTP ``SessionID`` validation shared by every native identity check.

``TThostFtdcSessionIDType`` is a signed 32-bit ``int`` assigned by the front
at login. Fronts (SimNow included) issue negative values, so a positive-only
check rejects roughly half of all real logins. Zero stays reserved as the
"absent" sentinel used by row projections. ``FrontID`` is unaffected: fronts
number themselves from 1.
"""

from __future__ import annotations

CTP_SESSION_ID_MIN = -(2**31)
CTP_SESSION_ID_MAX = 2**31 - 1


def is_ctp_session_id(value: object) -> bool:
    """Return True for a nonzero signed 32-bit ``int`` (``bool`` excluded)."""
    return (
        type(value) is int
        and value != 0
        and CTP_SESSION_ID_MIN <= value <= CTP_SESSION_ID_MAX
    )


def parse_ctp_session_id(value: object) -> int | None:
    """Parse an exact ``int`` or an optionally signed ASCII decimal string."""
    if type(value) is int:
        return value if is_ctp_session_id(value) else None
    if type(value) is str and value.isascii():
        digits = value[1:] if value.startswith("-") else value
        if digits.isdigit():
            parsed = int(value)
            return parsed if is_ctp_session_id(parsed) else None
    return None
