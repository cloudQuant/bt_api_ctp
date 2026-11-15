# ruff: noqa: UP045
"""Unverified, in-memory partial merge of calendar and day-hours candidates.

The two caller-supplied inputs may be fabricated. Structural checks do not
verify source authority, identity, calendar completeness, trading dates or
publication permission. Only source collisions and specified template scope /
effective-period conflicts are checked; cross-layer, exceptions, remaining
sessions and all night mapping still need independent review. No data is
published and no account, filesystem or network access occurs. Inputs must not
be concurrently modified while they are being validated and copied.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, time
from typing import Optional

from bt_api_ctp.calendar_candidate_merge import PartialMergeResult, _copy_validated
from bt_api_ctp.calendar_shfe_candidates import UnresolvedNight
from bt_api_ctp.calendar_shfe_hours_candidates import HoursCandidateResult, NightReview
from bt_api_ctp.calendar_shfe_page import StageCandidate
from bt_api_ctp.calendar_supplement import MAX_LIST_ITEMS, SupplementCandidate, _validate

_SOURCE = "https://www.shfe.com.cn/services/calenderandholidays/tradinghours/"
_SOURCE_ID = re.compile(r"shfe\.hours\.([0-9a-f]{64})\Z", re.ASCII)
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z", re.ASCII)
_REQUIRED = frozenset(
    {
        "SOURCE_AUTHORITY_UNVERIFIED",
        "COVERAGE_NOT_PROVEN",
        "TRADING_HOURS_CONFLICTS_NOT_CHECKED",
        "EXCEPTIONS_CONFLICTS_NOT_CHECKED",
        "CROSS_LAYER_CONFLICTS_NOT_CHECKED",
        "NIGHT_TRADING_DAY_UNRESOLVED",
    }
)
_EXTRA = frozenset({"OTHER_SESSIONS_NOT_CHECKED", "HOURS_NIGHT_MAPPING_UNRESOLVED"})


@dataclass(frozen=True)
class HoursMergeResult:
    status: str
    supplement: Optional[SupplementCandidate] = None
    holiday_unresolved_nights: tuple[UnresolvedNight, ...] = ()
    hours_unresolved_nights: tuple[NightReview, ...] = ()
    page_notes: tuple[str, ...] = ()
    page_review_reasons: tuple[str, ...] = ()
    reason: str = ""
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"
    source_authentication: str = "NOT_CHECKED"
    cross_layer_conflicts: str = "NOT_CHECKED"
    other_session_conflicts: str = "NOT_CHECKED"
    remaining_checks: tuple[str, ...] = tuple(sorted(_REQUIRED | _EXTRA))


def _bounded_strings(value, *, max_items, max_length):
    return (
        type(value) is tuple
        and len(value) <= max_items
        and all(type(item) is str and 0 < len(item) <= max_length for item in value)
    )


def _partial(result):
    if (
        type(result) is not PartialMergeResult
        or result.status != "PARTIAL"
        or result.reason != ""
        or (result.verification, result.coverage, result.publication)
        != ("UNVERIFIED", "NOT_PROVEN", "NOT_AUTHORIZED")
        or not _bounded_strings(result.remaining_checks, max_items=64, max_length=128)
        or len(set(result.remaining_checks)) != len(result.remaining_checks)
        or not _REQUIRED <= set(result.remaining_checks)
        or type(result.unresolved_nights) is not tuple
        or len(result.unresolved_nights) > MAX_LIST_ITEMS
    ):
        raise ValueError
    candidate = _copy_validated(result.supplement)
    days = [(row["venue"], row["day"]) for row in candidate.content["trading_days"]]
    if len(days) != len(set(days)):
        raise ValueError
    source_ids = {item["id"] for item in candidate.content["sources"]}
    for night in result.unresolved_nights:
        if (
            type(night) is not UnresolvedNight
            or type(night.holiday) is not str
            or not night.holiday
            or len(night.holiday) > 256
            or type(night.local_date) is not date
            or type(night.source_id) is not str
            or night.source_id not in source_ids
            or night.trading_day is not None
            or type(night.raw_text) is not str
            or not night.raw_text
            or len(night.raw_text) > 4096
        ):
            raise ValueError
    return candidate


def _stage(stage):
    if (
        type(stage) is not StageCandidate
        or stage.session != "NIGHT"
        or stage.phase not in {"AUCTION_ORDER", "AUCTION_MATCH", "CONTINUOUS"}
        or type(stage.start) is not str
        or _CLOCK.fullmatch(stage.start) is None
        or type(stage.end) is not str
        or _CLOCK.fullmatch(stage.end) is None
        or stage.day_transition not in {"SAME_CLOCK_DAY", "INFERRED_NEXT_DAY", "EXPLICIT_NEXT_DAY"}
        or type(stage.raw_line) is not str
        or not stage.raw_line
        or len(stage.raw_line) > 8192
    ):
        raise ValueError
    start, end = time.fromisoformat(stage.start), time.fromisoformat(stage.end)
    if (stage.day_transition == "SAME_CLOCK_DAY" and end <= start) or (
        stage.day_transition != "SAME_CLOCK_DAY" and end >= start
    ):
        raise ValueError


def _hours(result):
    if (
        type(result) is not HoursCandidateResult
        or result.status != "CANDIDATE"
        or result.reason != ""
        or (result.verification, result.coverage, result.publication)
        != ("UNVERIFIED", "NOT_PROVEN", "NOT_AUTHORIZED")
        or type(result.unresolved_nights) is not tuple
        or not 1 <= len(result.unresolved_nights) <= 128
        or not _bounded_strings(result.page_notes, max_items=256, max_length=8192)
        or not _bounded_strings(result.page_review_reasons, max_items=256, max_length=128)
        or sum(map(len, result.page_notes)) > 1024 * 1024
    ):
        raise ValueError
    candidate = _copy_validated(result.supplement)
    content = candidate.content
    if (
        content["trading_days"]
        or content["exceptions"]
        or not content["trading_hours"]
        or len(content["trading_hours"]) != len(result.unresolved_nights)
        or len(content["sources"]) != 1
    ):
        raise ValueError
    source = content["sources"][0]
    match = _SOURCE_ID.fullmatch(source["id"])
    if match is None or source["description"] != f"{_SOURCE} sha256:{match.group(1)}":
        raise ValueError
    identities = set()
    for row in content["trading_hours"]:
        if (
            row["venue"] not in {"SHFE", "INE"}
            or row["night_windows"]
            or row["night_mappings"]
            or row["windows_complete"] is not False
            or row["exceptions_complete"] is not False
            or not row["day_windows"]
            or row["source_ids"] != [source["id"]]
            or row["contracts"] != sorted(row["contracts"])
        ):
            raise ValueError
        windows = sorted((item["start"], item["end"]) for item in row["day_windows"])
        if any(first[1] > second[0] for first, second in zip(windows, windows[1:])):
            raise ValueError
        identity = (row["venue"], row["kind"], row["product"], tuple(row["contracts"]))
        if identity in identities:
            raise ValueError
        identities.add(identity)
    reviewed = set()
    total_stages = 0
    total_raw = 0
    for night in result.unresolved_nights:
        if (
            type(night) is not NightReview
            or type(night.page_product_name) is not str
            or not night.page_product_name
            or len(night.page_product_name) > 256
            or type(night.contracts) is not tuple
            or type(night.stages) is not tuple
            or len(night.stages) > 256
            or (night.availability, night.trading_day_mapping) != ("UNKNOWN", "UNKNOWN")
        ):
            raise ValueError
        identity = (night.venue, night.kind, night.product, night.contracts)
        if identity not in identities or identity in reviewed:
            raise ValueError
        reviewed.add(identity)
        total_stages += len(night.stages)
        if total_stages > 4096:
            raise ValueError
        for stage in night.stages:
            _stage(stage)
            total_raw += len(stage.raw_line)
            if total_raw > 1024 * 1024:
                raise ValueError
    if reviewed != identities:
        raise ValueError
    return candidate


def _template_conflict(rows):
    owners = {}
    intervals = {}
    total_contracts = 0
    comparisons = 0
    for row in rows:
        total_contracts += len(row["contracts"])
        if total_contracts > 4096:
            raise ValueError
        for contract in row["contracts"]:
            key = (row["venue"], contract)
            owner = (row["kind"], row["product"])
            previous_owner = owners.setdefault(key, owner)
            if previous_owner != owner:
                return "SCOPE_CONFLICT"
            scope = (*key, *owner)
            for start, end in intervals.setdefault(scope, []):
                comparisons += 1
                if comparisons > 100000:
                    raise ValueError
                if row["valid_from"] <= end and start <= row["valid_to"]:
                    return "TEMPLATE_PERIOD_CONFLICT"
            intervals[scope].append((row["valid_from"], row["valid_to"]))
    return ""


def merge_hours_candidates(
    *, partial: PartialMergeResult, hours: HoursCandidateResult
) -> HoursMergeResult:
    """Merge explicit candidate rows; output never constitutes publishable facts."""
    try:
        base = _partial(partial)
        day = _hours(hours)
        sources = {}
        for source in base.content["sources"] + day.content["sources"]:
            previous = sources.get(source["id"])
            if previous is not None and previous != source:
                return HoursMergeResult("CONFLICT", reason="SOURCE_CONFLICT")
            sources[source["id"]] = source
        rows = base.content["trading_hours"] + day.content["trading_hours"]
        conflict = _template_conflict(rows)
        if conflict:
            return HoursMergeResult("CONFLICT", reason=conflict)
        content = {
            "sources": [sources[key] for key in sorted(sources)],
            "trading_days": base.content["trading_days"],
            "trading_hours": rows,
            "exceptions": base.content["exceptions"],
        }
        candidate = _validate(content)
        return HoursMergeResult(
            "PARTIAL",
            candidate,
            deepcopy(partial.unresolved_nights),
            deepcopy(hours.unresolved_nights),
            tuple(hours.page_notes),
            tuple(hours.page_review_reasons),
            remaining_checks=tuple(sorted(set(partial.remaining_checks) | _EXTRA)),
        )
    except Exception:
        return HoursMergeResult("INVALID_INPUT", reason="INVALID_OR_UNSUPPORTED_INPUT")
