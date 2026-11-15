# ruff: noqa: UP045
"""Unverified, offline SHFE holiday-to-supplement conversion.

Only explicit holiday ranges and separately listed weekend dates become closed
records. The unresolved no-night dates are local natural dates, never mapped to
trading days. Re-parsing preserved text checks structural consistency only; a
caller can fabricate both text and the claimed digest. No provenance, venue
truth, yearly coverage, trading sessions, review or publication is established.
There is no automatic publication or network access in this module.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from bt_api_ctp.calendar_shfe_holiday import HolidayCandidate, HolidayRow, parse_shfe_holiday
from bt_api_ctp.calendar_supplement import SupplementCandidate, SupplementError, _validate

_SOURCE_URL = "https://www.shfe.com.cn/services/calenderandholidays/holiday/"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MAX_RANGE_DAYS = 31
_MAX_CLOSED_DAYS = 366


@dataclass(frozen=True)
class UnresolvedNight:
    holiday: str
    local_date: date
    source_id: str
    raw_text: str
    trading_day: None = None


@dataclass(frozen=True)
class ShfeHolidayConversion:
    status: str
    supplement: Optional[SupplementCandidate] = None
    unresolved_nights: tuple[UnresolvedNight, ...] = ()
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"


def convert_shfe_holiday(candidate: HolidayCandidate) -> ShfeHolidayConversion:
    """Convert a structurally consistent unverified candidate; fail closed otherwise."""
    try:
        if (
            type(candidate) is not HolidayCandidate
            or candidate.status != "CANDIDATE"
            or candidate.verification != "UNVERIFIED"
            or candidate.coverage != "NOT_PROVEN"
            or candidate.source_id != _SOURCE_URL
            or type(candidate.sha256) is not str
            or _DIGEST.fullmatch(candidate.sha256) is None
            or type(candidate.year) is not int
            or not 1 <= candidate.year <= 9999
            or type(candidate.raw_title) is not str
            or len(candidate.raw_title) > 256
            or type(candidate.rows) is not tuple
            or len(candidate.rows) != 7
            or candidate.reasons != ()
        ):
            raise ValueError("invalid candidate")
        for row in candidate.rows:
            if (
                type(row) is not HolidayRow
                or type(row.name) is not str
                or type(row.raw_text) is not str
                or not row.raw_text
                or len(row.raw_text) > 4096
                or type(row.closure_start) is not date
                or type(row.closure_end) is not date
                or type(row.no_night) is not date
                or type(row.weekend_closures) is not tuple
                or len(row.weekend_closures) > 32
                or any(type(day) is not date for day in row.weekend_closures)
                or not 0 <= (row.closure_end - row.closure_start).days < _MAX_RANGE_DAYS
            ):
                raise ValueError("invalid holiday row")
        # Reconstruct only the accepted article syntax to independently check each
        # claimed field against its preserved text. It does not authenticate bytes.
        article = "".join(f"<div>{html.escape(row.raw_text)}</div>" for row in candidate.rows)
        document = (
            '<div class="article_title" id="no_article_empty">'
            + html.escape(candidate.raw_title)
            + '</div><div class="article_content_item"><div class="TRS_Editor">'
            + article
            + "</div></div>"
        ).encode("utf-8")
        parsed = parse_shfe_holiday(source_id=candidate.source_id, html=document)
        if (
            parsed.status != "CANDIDATE"
            or parsed.year != candidate.year
            or parsed.raw_title != candidate.raw_title
            or parsed.rows != candidate.rows
        ):
            raise ValueError("candidate does not match its text")
        source_id = f"shfe.holiday.{candidate.year}.{candidate.sha256}"
        days = set()
        for row in candidate.rows:
            span = (row.closure_end - row.closure_start).days
            days.update(row.closure_start + timedelta(days=offset) for offset in range(span + 1))
            days.update(row.weekend_closures)
        if (
            not days
            or len(days) > _MAX_CLOSED_DAYS
            or any(day.year != candidate.year for day in days)
        ):
            raise ValueError("invalid closed dates")
        content = {
            "sources": [
                {"id": source_id, "description": f"{_SOURCE_URL} sha256:{candidate.sha256}"}
            ],
            "trading_days": [
                {
                    "venue": "SHFE",
                    "day": day.isoformat(),
                    "status": "closed",
                    "source_ids": [source_id],
                }
                for day in sorted(days)
            ],
            "trading_hours": [],
            "exceptions": [],
        }
        supplement = _validate(content)
        nights = tuple(
            UnresolvedNight(row.name, row.no_night, source_id, row.raw_text)
            for row in candidate.rows
        )
        return ShfeHolidayConversion("CANDIDATE", supplement, nights)
    except (AttributeError, TypeError, ValueError, OverflowError, UnicodeError, SupplementError):
        return ShfeHolidayConversion("INVALID_CANDIDATE")
