# ruff: noqa: UP045
"""Convert explicit INE holiday statements to an unverified partial supplement.

The raw page and parsed candidate must match exactly. The conversion records
only declared closures; no opening days or night-session ownership are inferred.
Matching bytes do not authenticate the source or authorize publication.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from bt_api_ctp.calendar_ine_holiday import SOURCE_URL, IneHolidayCandidate, parse_ine_holiday
from bt_api_ctp.calendar_supplement import SupplementCandidate, SupplementError, _validate

_MAX_RANGE_DAYS = 31
_MAX_CLOSED_DAYS = 366


@dataclass(frozen=True)
class IneUnresolvedNight:
    holiday: str
    local_date: date
    source_id: str
    raw_text: str
    trading_day: None = None


@dataclass(frozen=True)
class IneHolidayConversion:
    status: str
    supplement: Optional[SupplementCandidate] = None
    unresolved_nights: tuple[IneUnresolvedNight, ...] = ()
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"


def convert_ine_holiday(candidate: IneHolidayCandidate, *, html: bytes) -> IneHolidayConversion:
    """Convert only a complete parser result rederived from the exact raw bytes."""
    try:
        if (
            type(candidate) is not IneHolidayCandidate
            or type(html) is not bytes
            or candidate.status != "CANDIDATE"
            or candidate.source_id != SOURCE_URL
            or candidate.verification != "UNVERIFIED"
            or candidate.coverage != "NOT_PROVEN"
            or candidate.publication != "NOT_AUTHORIZED"
            or type(candidate.year) is not int
            or type(candidate.sha256) is not str
            or type(candidate.rows) is not tuple
            or len(candidate.rows) != 7
            or candidate.reason != ""
        ):
            raise ValueError("invalid candidate")
        parsed = parse_ine_holiday(source_id=SOURCE_URL, html=html)
        if parsed != candidate or parsed.status != "CANDIDATE":
            raise ValueError("candidate does not match original bytes")
        days = set()
        for row in parsed.rows:
            span = (row.closure_end - row.closure_start).days
            if not 0 <= span < _MAX_RANGE_DAYS:
                raise ValueError("closure range too large")
            days.update(row.closure_start + timedelta(days=offset) for offset in range(span + 1))
            days.update(row.weekend_closures)
        if not days or len(days) > _MAX_CLOSED_DAYS or any(d.year != parsed.year for d in days):
            raise ValueError("ambiguous closure coverage")
        source_id = f"ine.holiday.{parsed.year}.{parsed.sha256}"
        content = {
            "sources": [{"id": source_id, "description": f"{SOURCE_URL} sha256:{parsed.sha256}"}],
            "trading_days": [
                {
                    "venue": "INE",
                    "day": day.isoformat(),
                    "status": "closed",
                    "source_ids": [source_id],
                }
                for day in sorted(days)
            ],
            "trading_hours": [],
            "exceptions": [],
        }
        nights = tuple(
            IneUnresolvedNight(row.name, row.no_night, source_id, row.raw_text)
            for row in parsed.rows
        )
        return IneHolidayConversion("CANDIDATE", _validate(content), nights)
    except (AttributeError, TypeError, ValueError, OverflowError, UnicodeError, SupplementError):
        return IneHolidayConversion("UNKNOWN")
