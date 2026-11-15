# ruff: noqa: UP045
"""Experimental partial merge of an unverified supplement and SHFE closures.

SupplementCandidate has no coverage or publication fields, so the caller must
explicitly declare both as NOT_PROVEN / NOT_AUTHORIZED. These declarations,
source descriptions and this structural merge do not prove source authority,
coverage or permission. No network, account or publication operation occurs.
Trading hours and exceptions are retained verbatim without conflict analysis.
The input objects must not be concurrently modified during this operation.
"""

from __future__ import annotations

import html
import re
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date
from typing import Optional

from bt_api_ctp.calendar_shfe_candidates import (
    ShfeHolidayConversion,
    UnresolvedNight,
    convert_shfe_holiday,
)
from bt_api_ctp.calendar_shfe_holiday import parse_shfe_holiday
from bt_api_ctp.calendar_supplement import SupplementCandidate, SupplementError, _validate

_SOURCE_URL = "https://www.shfe.com.cn/services/calenderandholidays/holiday/"
_SOURCE_ID = re.compile(r"shfe\.holiday\.([0-9]{4})\.([0-9a-f]{64})\Z", re.ASCII)
_REQUIRED_REVIEW = (
    "SOURCE_AUTHORITY_UNVERIFIED",
    "COVERAGE_NOT_PROVEN",
    "TRADING_HOURS_CONFLICTS_NOT_CHECKED",
    "EXCEPTIONS_CONFLICTS_NOT_CHECKED",
    "CROSS_LAYER_CONFLICTS_NOT_CHECKED",
    "NIGHT_TRADING_DAY_UNRESOLVED",
)


@dataclass(frozen=True)
class PartialMergeResult:
    status: str
    supplement: Optional[SupplementCandidate] = None
    unresolved_nights: tuple[UnresolvedNight, ...] = ()
    reason: str = ""
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"
    remaining_checks: tuple[str, ...] = _REQUIRED_REVIEW


def _copy_validated(candidate: SupplementCandidate) -> SupplementCandidate:
    if type(candidate) is not SupplementCandidate or candidate.verification != "UNVERIFIED":
        raise ValueError("invalid supplement")
    validated = _validate(candidate.content)
    if type(candidate.unused_source_ids) is not tuple or (
        validated.unused_source_ids != candidate.unused_source_ids
    ):
        raise ValueError("invalid source bookkeeping")
    return _validate(deepcopy(validated.content))


def _holiday_content(holiday: ShfeHolidayConversion) -> SupplementCandidate:
    if (
        type(holiday) is not ShfeHolidayConversion
        or holiday.status != "CANDIDATE"
        or holiday.verification != "UNVERIFIED"
        or holiday.coverage != "NOT_PROVEN"
        or holiday.publication != "NOT_AUTHORIZED"
        or type(holiday.unresolved_nights) is not tuple
        or len(holiday.unresolved_nights) != 7
    ):
        raise ValueError("invalid holiday result")
    supplement = _copy_validated(holiday.supplement)
    if len(supplement.content["sources"]) != 1:
        raise ValueError("invalid holiday source")
    source = supplement.content["sources"][0]
    match = _SOURCE_ID.fullmatch(source["id"])
    if match is None or source["description"] != f"{_SOURCE_URL} sha256:{match.group(2)}":
        raise ValueError("invalid holiday source")
    for night in holiday.unresolved_nights:
        if (
            type(night) is not UnresolvedNight
            or type(night.holiday) is not str
            or type(night.local_date) is not date
            or night.source_id != source["id"]
            or night.trading_day is not None
            or type(night.raw_text) is not str
            or not night.raw_text
            or len(night.raw_text) > 4096
        ):
            raise ValueError("invalid night candidate")
    # Reconstruct the existing parser/converter's result. The claimed SHA and
    # source remain unverified: original HTML bytes are not supplied here.
    rows = "".join(f"<div>{html.escape(n.raw_text)}</div>" for n in holiday.unresolved_nights)
    document = (
        f'<div class="article_title" id="no_article_empty">{match.group(1)}年休市安排</div>'
        '<div class="article_content_item"><div class="TRS_Editor">' + rows + "</div></div>"
    ).encode("utf-8")
    parsed = parse_shfe_holiday(source_id=_SOURCE_URL, html=document)
    reconstructed = convert_shfe_holiday(replace(parsed, sha256=match.group(2)))
    if reconstructed != holiday:
        raise ValueError("holiday result is internally inconsistent")
    return supplement


def merge_calendar_candidates(
    *,
    supplement: SupplementCandidate,
    holiday: ShfeHolidayConversion,
    supplement_coverage: str,
    supplement_publication: str,
) -> PartialMergeResult:
    """Merge two declared unverified inputs without establishing publishability."""
    try:
        if (
            type(supplement_coverage) is not str
            or supplement_coverage != "NOT_PROVEN"
            or type(supplement_publication) is not str
            or supplement_publication != "NOT_AUTHORIZED"
        ):
            raise ValueError("invalid declared status")
        base = _copy_validated(supplement)
        closures = _holiday_content(holiday)
        sources = {}
        for item in base.content["sources"] + closures.content["sources"]:
            previous = sources.get(item["id"])
            if previous is not None and previous != item:
                return PartialMergeResult("CONFLICT", reason="SOURCE_CONFLICT")
            sources[item["id"]] = item
        days = {}
        for row in base.content["trading_days"] + closures.content["trading_days"]:
            key = (row["venue"], row["day"])
            previous = days.get(key)
            if previous is None:
                days[key] = deepcopy(row)
            elif previous["status"] != row["status"]:
                return PartialMergeResult("CONFLICT", reason="TRADING_DAY_CONFLICT")
            else:
                previous["source_ids"] = sorted(
                    set(previous["source_ids"]) | set(row["source_ids"])
                )
        content = {
            "sources": [sources[key] for key in sorted(sources)],
            "trading_days": [days[key] for key in sorted(days)],
            "trading_hours": base.content["trading_hours"] + closures.content["trading_hours"],
            "exceptions": base.content["exceptions"] + closures.content["exceptions"],
        }
        return PartialMergeResult(
            "PARTIAL", _validate(content), deepcopy(holiday.unresolved_nights)
        )
    except (AttributeError, TypeError, ValueError, OverflowError, UnicodeError, SupplementError):
        return PartialMergeResult("INVALID_INPUT", reason="INVALID_OR_UNSUPPORTED_INPUT")
