"""Conservative offline comparison of unverified partial calendar candidates.

SAME means only the supplied partial structures compare equal; it is not a
claim of completeness, authoritative data, or permission to skip a check or
publish. Source metadata or preserved prose changes are UNKNOWN. In particular,
a changed whole-page hash does not prove either a changed rule or unchanged
rules. Inputs must not be modified concurrently while being checked and copied.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace

from bt_api_ctp.calendar_candidate_merge import PartialMergeResult
from bt_api_ctp.calendar_candidate_normalize import normalize_calendar_candidate
from bt_api_ctp.calendar_hours_merge import (
    _EXTRA,
    HoursMergeResult,
    _hours,
    _partial,
    _template_conflict,
)
from bt_api_ctp.calendar_shfe_hours_candidates import HoursCandidateResult, NightReview
from bt_api_ctp.calendar_supplement import _validate


@dataclass(frozen=True)
class CandidateComparison:
    status: str
    reason: str
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"


def _encode(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _identity(venue, kind, product, contracts):
    return venue, kind, product, tuple(sorted(contracts))


def _without_refs(value):
    if type(value) is dict:
        return {key: _without_refs(item) for key, item in value.items() if key != "source_ids"}
    if type(value) is list:
        return [_without_refs(item) for item in value]
    return value


def _source_bindings(content):
    """Keep references associated with scopes even when rules also change."""
    bindings = defaultdict(list)
    for row in content["trading_days"]:
        bindings[("day", row["venue"], row["day"])].append(tuple(row["source_ids"]))
    for row in content["trading_hours"]:
        scope = ("hours", row["venue"], row["kind"], row["product"], tuple(row["contracts"]))
        bindings[scope].append(tuple(row["source_ids"]))
        for mapping in row["night_mappings"]:
            bindings[(*scope, "night", mapping["trading_day"])].append(tuple(mapping["source_ids"]))
    for row in content["exceptions"]:
        scope = ("exception", row["venue"], row["kind"], row["product"], row["contract"])
        bindings[scope].append(tuple(row["source_ids"]))
    return {key: sorted(value) for key, value in bindings.items()}


def _prepare(result):
    if (
        type(result) is not HoursMergeResult
        or result.status != "PARTIAL"
        or result.reason != ""
        or (result.verification, result.coverage, result.publication)
        != ("UNVERIFIED", "NOT_PROVEN", "NOT_AUTHORIZED")
        or (
            result.source_authentication,
            result.cross_layer_conflicts,
            result.other_session_conflicts,
        )
        != ("NOT_CHECKED", "NOT_CHECKED", "NOT_CHECKED")
        or type(result.holiday_unresolved_nights) is not tuple
        or len(result.holiday_unresolved_nights) != 7
        or type(result.hours_unresolved_nights) is not tuple
        or not 1 <= len(result.hours_unresolved_nights) <= 128
        or type(result.remaining_checks) is not tuple
        or not _EXTRA <= set(result.remaining_checks)
    ):
        raise ValueError("unsupported result")

    # Existing validators perform a detached schema check, bounded work, and
    # preserve only explicitly partial, unverified inputs.
    candidate = _partial(
        PartialMergeResult(
            "PARTIAL",
            result.supplement,
            result.holiday_unresolved_nights,
            remaining_checks=result.remaining_checks,
        )
    )
    if _template_conflict(candidate.content["trading_hours"]):
        raise ValueError("ambiguous templates")
    normalized = normalize_calendar_candidate(candidate)
    if normalized.status != "NORMALIZED" or normalized.json_bytes is None:
        raise ValueError("invalid candidate")
    content = json.loads(normalized.json_bytes)
    by_identity = {}
    for row in content["trading_hours"]:
        key = _identity(row["venue"], row["kind"], row["product"], row["contracts"])
        by_identity.setdefault(key, []).append(row)
    selected = []
    seen = set()
    for night in result.hours_unresolved_nights:
        if type(night) is not NightReview or type(night.contracts) is not tuple:
            raise ValueError("invalid night review")
        key = _identity(night.venue, night.kind, night.product, night.contracts)
        rows = by_identity.get(key, [])
        matches = [
            row
            for row in rows
            if len(row["source_ids"]) == 1 and row["source_ids"][0].startswith("shfe.hours.")
        ]
        if len(matches) != 1 or key in seen:
            raise ValueError("unmatched review")
        seen.add(key)
        selected.append(matches[0])
    page_rows = [
        row
        for row in content["trading_hours"]
        if any(s.startswith("shfe.hours.") for s in row["source_ids"])
    ]
    if len(page_rows) != len(selected):
        raise ValueError("unreviewed page hours")
    source_ids = {row["source_ids"][0] for row in selected}
    sources = [source for source in content["sources"] if source["id"] in source_ids]
    hours = _validate(
        {"sources": sources, "trading_days": [], "trading_hours": selected, "exceptions": []}
    )
    ordered_nights = tuple(
        replace(night, contracts=tuple(sorted(night.contracts)))
        for night in result.hours_unresolved_nights
    )
    _hours(
        HoursCandidateResult(
            "CANDIDATE", hours, ordered_nights, result.page_notes, result.page_review_reasons
        )
    )

    # The prose may encode rules that the structured parser has not understood.
    # Never dismiss its changes as equivalent or call them a trading rule change.
    holiday_text = sorted(
        (n.holiday, n.local_date.isoformat(), n.source_id, n.raw_text, n.trading_day)
        for n in result.holiday_unresolved_nights
    )
    if len(
        {(n.holiday, n.local_date, n.source_id) for n in result.holiday_unresolved_nights}
    ) != len(holiday_text):
        raise ValueError("duplicate holiday review")
    hours_struct, hours_text = [], []
    for night in ordered_nights:
        key = _identity(night.venue, night.kind, night.product, night.contracts)
        stages = sorted(
            (s.session, s.phase, s.start, s.end, s.day_transition) for s in night.stages
        )
        if len(stages) != len(set(stages)):
            raise ValueError("duplicate night stage")
        raw = sorted(
            (s.session, s.phase, s.start, s.end, s.day_transition, s.raw_line) for s in night.stages
        )
        hours_struct.append(
            (key, night.page_product_name, night.availability, night.trading_day_mapping, stages)
        )
        hours_text.append((key, raw))
    hours_struct.sort()
    hours_text.sort()
    _encode((holiday_text, hours_text, result.page_notes, result.page_review_reasons)).encode(
        "utf-8", errors="strict"
    )
    evidence = (
        content["sources"],
        tuple(sorted(result.page_notes)),
        tuple(sorted(result.page_review_reasons)),
        tuple(sorted(result.remaining_checks)),
    )
    holiday_structure = sorted(
        (n.holiday, n.local_date.isoformat(), n.source_id, n.trading_day)
        for n in result.holiday_unresolved_nights
    )
    return content, evidence, holiday_structure, holiday_text, hours_struct, hours_text


def compare_partial_candidates(
    before: HoursMergeResult, after: HoursMergeResult
) -> CandidateComparison:
    """Compare complete partial results, including separately retained reviews."""
    try:
        left = _prepare(before)
        right = _prepare(after)
        if left[1] != right[1]:
            return CandidateComparison("UNKNOWN", "SOURCE_OR_REVIEW_METADATA_CHANGED")
        if _source_bindings(left[0]) != _source_bindings(right[0]):
            return CandidateComparison("UNKNOWN", "SOURCE_BINDINGS_OR_SCOPE_CHANGED")
        # Preserve all raw prose. A changed structured identity is a detectable
        # change; otherwise changed prose requires an independent review.
        if left[2] == right[2] and left[3] != right[3]:
            return CandidateComparison("UNKNOWN", "HOLIDAY_TEXT_CHANGED")
        if left[4] == right[4] and left[5] != right[5]:
            return CandidateComparison("UNKNOWN", "HOURS_TEXT_CHANGED")
        if left[2] != right[2] or left[4] != right[4]:
            return CandidateComparison("CHANGED", "REVIEW_STRUCTURE_CHANGED")
        if left[0] == right[0]:
            return CandidateComparison("SAME", "PARTIAL_STRUCTURES_EQUAL")
        left_rules = {
            key: _without_refs(value) for key, value in left[0].items() if key != "sources"
        }
        right_rules = {
            key: _without_refs(value) for key, value in right[0].items() if key != "sources"
        }
        if _encode(left_rules) == _encode(right_rules):
            return CandidateComparison("UNKNOWN", "SOURCE_REFERENCES_CHANGED")
        return CandidateComparison("CHANGED", "STRUCTURE_CHANGED")
    except (AttributeError, TypeError, ValueError, OverflowError, UnicodeError):
        return CandidateComparison("UNKNOWN", "INVALID_OR_UNSUPPORTED_INPUT")
