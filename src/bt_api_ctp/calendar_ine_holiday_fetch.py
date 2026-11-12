# Keep type hints resolvable on Python 3.9.
# ruff: noqa: UP045
"""Collect the fixed INE public holiday page as unverified evidence.

One supervised HTTPS request returns the same raw bytes that the parent hashes
and parses. The result is not a complete annual calendar, source authenticity
proof, publication authorization or evidence of trading permission. No raw
page is logged or printed by this API; its child uses a private protocol pipe.
The existing POSIX supervisor's cleanup and hard-deadline limitations apply.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from bt_api_ctp.calendar_ine_holiday import (
    MAX_BYTES,
    SOURCE_URL,
    IneHolidayCandidate,
    parse_ine_holiday,
)
from bt_api_ctp.calendar_ine_hours_fetch import _PARENT_STATUSES, _run_holiday_child


@dataclass(frozen=True)
class IneHolidayFetchResult:
    status: str
    transport_status: str
    collected_at: datetime
    source: str = SOURCE_URL
    sha256: Optional[str] = None
    page: Optional[Any] = None
    raw_bytes: Optional[bytes] = field(default=None, repr=False)
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"
    transport_assurance: str = "INJECTED_UNVERIFIED"


def collect_ine_holiday(
    *,
    connect_timeout: float = 3.0,
    read_timeout: float = 3.0,
    total_timeout: float = 10.0,
    max_bytes: int = MAX_BYTES,
    runner: Callable = _run_holiday_child,
) -> IneHolidayFetchResult:
    """Return one explicit INE holiday observation, retaining exact bytes."""
    for value in (connect_timeout, read_timeout, total_timeout):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
            raise ValueError("invalid timeout")
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES or not callable(runner):
        raise ValueError("invalid fetch settings")
    try:
        transport, raw = runner(connect_timeout, read_timeout, total_timeout, max_bytes)
    except Exception:
        transport, raw = "CHILD_CLEANUP_UNCERTAIN", None
    if (
        type(transport) is not str
        or transport not in _PARENT_STATUSES
        or (transport == "HTTP_OK") != (type(raw) is bytes)
        or (raw is not None and (not raw or len(raw) > max_bytes))
    ):
        raise ValueError("invalid runner result")
    now = datetime.now(timezone.utc)
    assurance = (
        "SUPERVISED_DEFAULT_TLS_POLICY" if runner is _run_holiday_child else "INJECTED_UNVERIFIED"
    )
    if raw is None:
        return IneHolidayFetchResult(transport, transport, now, transport_assurance=assurance)
    digest = hashlib.sha256(raw).hexdigest()
    if "人机识别".encode() in raw and "应用防火墙".encode() in raw:
        return IneHolidayFetchResult(
            "ACCESS_CHALLENGE",
            transport,
            now,
            sha256=digest,
            raw_bytes=raw,
            transport_assurance=assurance,
        )
    try:
        page = parse_ine_holiday(source_id=SOURCE_URL, html=raw)
        if (
            type(page) is not IneHolidayCandidate
            or page.source_id != SOURCE_URL
            or page.sha256 != digest
            or page.verification != "UNVERIFIED"
            or page.coverage != "NOT_PROVEN"
            or page.publication != "NOT_AUTHORIZED"
        ):
            raise ValueError("invalid parser result")
        return IneHolidayFetchResult(
            page.status,
            transport,
            now,
            sha256=digest,
            page=page,
            raw_bytes=raw,
            transport_assurance=assurance,
        )
    except Exception:
        return IneHolidayFetchResult(
            "PARSER_ERROR",
            transport,
            now,
            sha256=digest,
            raw_bytes=raw,
            transport_assurance=assurance,
        )
