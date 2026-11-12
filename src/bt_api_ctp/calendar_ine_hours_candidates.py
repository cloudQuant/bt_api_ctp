# ruff: noqa: UP045
"""INE public-page candidate conversion; no source or coverage is authenticated."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bt_api_ctp.calendar_ine_holiday import IneHolidayCandidate
from bt_api_ctp.calendar_ine_holiday_candidates import IneUnresolvedNight, convert_ine_holiday
from bt_api_ctp.calendar_ine_hours_fetch import SOURCE
from bt_api_ctp.calendar_shfe_hours_candidates import (
    HoursCandidateResult,
    HoursSelection,
    NightReview,
    _convert_hours,
)
from bt_api_ctp.calendar_shfe_page import PageCandidate, parse_shfe_page
from bt_api_ctp.calendar_supplement import SupplementCandidate, _validate


@dataclass(frozen=True)
class IneHoursMergeResult:
    status: str
    supplement: Optional[SupplementCandidate] = None
    holiday_unresolved_nights: tuple[IneUnresolvedNight, ...] = ()
    hours_unresolved_nights: tuple[NightReview, ...] = ()
    page_notes: tuple[str, ...] = ()
    page_review_reasons: tuple[str, ...] = ()
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"


def convert_ine_hours(
    page: PageCandidate, *, html: bytes, selections: tuple[HoursSelection, ...]
) -> HoursCandidateResult:
    """Reparse exact INE bytes and project only explicitly declared selections."""
    if type(html) is not bytes or type(page) is not PageCandidate:
        return HoursCandidateResult("INVALID_INPUT", reason="INVALID_OR_AMBIGUOUS_INPUT")
    parsed = parse_shfe_page(source_id=SOURCE, html=html)
    if parsed != page:
        return HoursCandidateResult("INVALID_INPUT", reason="INVALID_OR_AMBIGUOUS_INPUT")
    return _convert_hours(
        parsed, selections=selections, source_url=SOURCE, prefix="ine", venues={"INE"}
    )


def merge_ine_holiday_hours(
    *,
    holiday: IneHolidayCandidate,
    holiday_html: bytes,
    page: PageCandidate,
    hours_html: bytes,
    selections: tuple[HoursSelection, ...],
) -> IneHoursMergeResult:
    """Derive both inputs afresh, preserving every unresolved night declaration."""
    try:
        closures = convert_ine_holiday(holiday, html=holiday_html)
        hours = convert_ine_hours(page, html=hours_html, selections=selections)
        if closures.status != "CANDIDATE" or hours.status != "CANDIDATE":
            raise ValueError("candidate unavailable")
        old, new = closures.supplement.content, hours.supplement.content
        if old["trading_hours"] or old["exceptions"] or new["trading_days"] or new["exceptions"]:
            raise ValueError("unexpected layers")
        sources = old["sources"] + new["sources"]
        if len({row["id"] for row in sources}) != len(sources):
            raise ValueError("source conflict")
        content = _validate(
            {
                "sources": sorted(sources, key=lambda row: row["id"]),
                "trading_days": old["trading_days"],
                "trading_hours": new["trading_hours"],
                "exceptions": [],
            }
        )
        return IneHoursMergeResult(
            "PARTIAL",
            content,
            closures.unresolved_nights,
            hours.unresolved_nights,
            hours.page_notes,
            hours.page_review_reasons,
        )
    except Exception:
        return IneHoursMergeResult("INVALID_INPUT")
