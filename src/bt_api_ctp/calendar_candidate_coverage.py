# ruff: noqa: UP045
"""Bounded structural coverage checks for explicit, unverified CTP candidates.

Each requested inclusive date is queried as supplied; no weekday or trading-day
calendar is generated. CalendarSnapshot completeness reflects input structure
and declarations only. It proves neither source truth nor listing, venue rules,
UTC coverage, full product inventory, real effective dates, or publication
permission. Construction/query failures are fail-closed for the entire request.
No network, account, publishing, or source text is involved in the output.
Inputs must not be changed concurrently while they are copied and validated.
One request may span 731 days; all requests together are limited to 8192.
The existing supplement schema independently limits each layer to 1000 rows,
so one candidate cannot represent two complete years for six separate venues.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date

from bt_api_ctp.calendar_snapshot import CalendarSnapshot
from bt_api_ctp.calendar_supplement import MAX_LIST_ITEMS, SupplementCandidate, _validate

MAX_REQUIREMENTS = 64
MAX_DAYS_PER_REQUIREMENT = 731
MAX_QUERY_DAYS = 8192
_MAX_SOURCES = 128
_MAX_DAYS = MAX_LIST_ITEMS
_MAX_RULES = 128
_MAX_EXCEPTIONS = 256
_MAX_CONTRACTS = 128
_MAX_NESTED = 8192
_MAX_CELLS = 8192
_MAX_WINDOW_WORK = 100000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_VENUES = frozenset({"SHFE", "INE", "CZCE", "DCE", "CFFEX", "GFEX"})


@dataclass(frozen=True)
class CoverageRequirement:
    venue: str
    kind: str
    product: str
    contract: str
    start_day: date
    end_day: date


@dataclass(frozen=True)
class CoverageGap:
    venue: str
    kind: str
    product: str
    contract: str
    start_day: date
    end_day: date
    reason: str


@dataclass(frozen=True)
class StructuralCoverageResult:
    status: str
    gaps: tuple[CoverageGap, ...] = ()
    requested_days: int = 0
    checked_days: int = 0
    reason: str = ""
    verification: str = "UNVERIFIED"
    coverage: str = "CALLER_DECLARED_SCOPE_ONLY"
    source_authentication: str = "NOT_AUTHENTICATED"
    utc_coverage: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"


def _identity(value: object) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _validate_requirements(requirements: object) -> tuple[tuple[CoverageRequirement, ...], int]:
    if type(requirements) is not tuple or not 1 <= len(requirements) <= MAX_REQUIREMENTS:
        raise ValueError("invalid requirement set")
    scopes = {}
    total = 0
    for item in requirements:
        if (
            type(item) is not CoverageRequirement
            or type(item.venue) is not str
            or item.venue not in _VENUES
            or type(item.kind) is not str
            or item.kind not in {"future", "option"}
            or not _identity(item.product)
            or not _identity(item.contract)
            or type(item.start_day) is not date
            or type(item.end_day) is not date
            or item.start_day > item.end_day
        ):
            raise ValueError("invalid scope or bounds")
        length = (item.end_day - item.start_day).days + 1
        if length > MAX_DAYS_PER_REQUIREMENT:
            raise ValueError("range exceeds budget")
        total += length
        if total > MAX_QUERY_DAYS:
            raise ValueError("query exceeds budget")
        key = (item.venue, item.kind, item.product, item.contract)
        for start, end in scopes.setdefault(key, []):
            if item.start_day <= end and start <= item.end_day:
                raise ValueError("overlapping requested ranges")
        scopes[key].append((item.start_day, item.end_day))
    return requirements, total


def _bounded_content(content: object) -> None:
    if type(content) is not dict or set(content) != {
        "sources",
        "trading_days",
        "trading_hours",
        "exceptions",
    }:
        raise ValueError("invalid content")
    if any(type(content[key]) is not list for key in content):
        raise ValueError("invalid layers")
    if (
        len(content["sources"]) > _MAX_SOURCES
        or len(content["trading_days"]) > _MAX_DAYS
        or len(content["trading_hours"]) > _MAX_RULES
        or len(content["exceptions"]) > _MAX_EXCEPTIONS
    ):
        raise ValueError("candidate exceeds budget")
    contracts = 0
    nested = 0
    window_work = 0
    day_count = len(content["trading_days"])
    for row in content["trading_hours"]:
        if type(row) is not dict:
            raise ValueError("invalid rule")
        keys = ("contracts", "day_windows", "night_windows", "night_mappings", "source_ids")
        if any(type(row.get(key)) is not list for key in keys):
            raise ValueError("invalid rule arrays")
        contracts += len(row["contracts"])
        nested += sum(len(row[key]) for key in keys)
        window_work += (
            day_count
            * len(row["contracts"])
            * (len(row["day_windows"]) + len(row["night_windows"]))
        )
        for mapping in row["night_mappings"]:
            if type(mapping) is not dict or type(mapping.get("source_ids")) is not list:
                raise ValueError("invalid mapping")
            nested += len(mapping["source_ids"])
        if (
            contracts > _MAX_CONTRACTS
            or nested > _MAX_NESTED
            or contracts * day_count > _MAX_CELLS
            or window_work > _MAX_WINDOW_WORK
        ):
            raise ValueError("candidate work exceeds budget")


def _append_gap(
    gaps: list[CoverageGap], requirement: CoverageRequirement, day: date, reason: str
) -> None:
    if gaps:
        last = gaps[-1]
        if (last.venue, last.kind, last.product, last.contract, last.reason) == (
            requirement.venue,
            requirement.kind,
            requirement.product,
            requirement.contract,
            reason,
        ) and last.end_day.toordinal() + 1 == day.toordinal():
            gaps[-1] = CoverageGap(
                last.venue, last.kind, last.product, last.contract, last.start_day, day, reason
            )
            return
    gaps.append(
        CoverageGap(
            requirement.venue,
            requirement.kind,
            requirement.product,
            requirement.contract,
            day,
            day,
            reason,
        )
    )


def _days(requirement: CoverageRequirement):
    for ordinal in range(requirement.start_day.toordinal(), requirement.end_day.toordinal() + 1):
        yield date.fromordinal(ordinal)


def _missing_night_mapping(content, requirement, day):
    day_rows = [
        row
        for row in content["trading_days"]
        if row["venue"] == requirement.venue and row["day"] == day.isoformat()
    ]
    if len(day_rows) != 1 or day_rows[0]["status"] != "open":
        return False
    rules = [
        row
        for row in content["trading_hours"]
        if (row["venue"], row["kind"], row["product"])
        == (requirement.venue, requirement.kind, requirement.product)
        and requirement.contract in row["contracts"]
        and row["valid_from"] <= day.isoformat() <= row["valid_to"]
    ]
    return len(rules) == 1 and all(
        m["trading_day"] != day.isoformat() for m in rules[0]["night_mappings"]
    )


def check_candidate_coverage(
    candidate: SupplementCandidate, *, requirements: tuple[CoverageRequirement, ...]
) -> StructuralCoverageResult:
    """Check only explicit caller scopes and dates, never grant publishability."""
    try:
        requests, total = _validate_requirements(requirements)
    except Exception:
        return StructuralCoverageResult("INVALID_INPUT", reason="INVALID_REQUIREMENTS_OR_BUDGET")
    try:
        if type(candidate) is not SupplementCandidate or candidate.verification != "UNVERIFIED":
            raise ValueError("invalid candidate")
        _bounded_content(candidate.content)
        validated = _validate(candidate.content)
        if (
            type(candidate.unused_source_ids) is not tuple
            or candidate.unused_source_ids != validated.unused_source_ids
        ):
            raise ValueError("invalid source bookkeeping")
        content = deepcopy(validated.content)
        _bounded_content(content)
        _validate(content)
    except Exception:
        return StructuralCoverageResult("INVALID_INPUT", reason="INVALID_CANDIDATE_OR_BUDGET")
    try:
        snapshot = CalendarSnapshot(content, calendar_ref="structural-coverage-candidate")
    except Exception:
        gaps = []
        for item in requests:
            for day in _days(item):
                reason = (
                    "MISSING_NIGHT_MAPPING"
                    if _missing_night_mapping(content, item, day)
                    else "SNAPSHOT_INVALID"
                )
                _append_gap(gaps, item, day, reason)
        return StructuralCoverageResult(
            "STRUCTURAL_GAPS", tuple(gaps), total, 0, "SNAPSHOT_INVALID"
        )
    try:
        gaps = []
        checked = 0
        for item in requests:
            for day in _days(item):
                result = snapshot.query(
                    venue=item.venue,
                    kind=item.kind,
                    product=item.product,
                    contract=item.contract,
                    trading_day=day,
                )
                checked += 1
                if (
                    result.status == "COMPLETE"
                    and result.reason == "EXPLICIT_DATA"
                    and result.sources_verified is False
                ):
                    continue
                if result.status == "UNKNOWN" and result.reason in {
                    "COVERAGE_INSUFFICIENT",
                    "RULE_COVERAGE_INCOMPLETE",
                }:
                    reason = result.reason
                elif (
                    result.status == "OUT_OF_SCOPE" and result.reason == "IDENTITY_NOT_IN_SNAPSHOT"
                ):
                    reason = "IDENTITY_NOT_IN_SNAPSHOT"
                else:
                    raise ValueError("unexpected query result")
                _append_gap(gaps, item, day, reason)
        return StructuralCoverageResult(
            "STRUCTURAL_GAPS" if gaps else "STRUCTURAL_COMPLETE_UNVERIFIED",
            tuple(gaps),
            total,
            checked,
        )
    except Exception:
        gaps = []
        for item in requests:
            for day in _days(item):
                _append_gap(gaps, item, day, "QUERY_FAILED")
        return StructuralCoverageResult("STRUCTURAL_GAPS", tuple(gaps), total, 0, "QUERY_FAILED")
