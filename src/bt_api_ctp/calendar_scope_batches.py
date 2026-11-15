# ruff: noqa: UP045
"""Bounded per-contract checks of a private single-venue candidate snapshot.

The full input is recaptured and structurally checked before filtering. Every
scope view retains all date/source rows and applicable exact or product-level
exceptions. Batch outcomes describe only explicitly requested ranges; global
cross-scope relations, source authenticity, UTC coverage and final composition
still require independent checks. This module does not authorize or publish.
Inputs must not be concurrently mutated while the initial snapshot is captured.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from bt_api_ctp.calendar_auto_candidate import AutoCandidateResult, build_auto_candidate
from bt_api_ctp.calendar_candidate_coverage import (
    MAX_DAYS_PER_REQUIREMENT,
    MAX_QUERY_DAYS,
    MAX_REQUIREMENTS,
    CoverageGap,
    CoverageRequirement,
    _identity,
    check_candidate_coverage,
)
from bt_api_ctp.calendar_candidate_normalize import normalize_calendar_candidate
from bt_api_ctp.calendar_supplement import SupplementCandidate, _validate

MAX_BATCHED_QUERY_DAYS = MAX_REQUIREMENTS * MAX_DAYS_PER_REQUIREMENT


@dataclass(frozen=True)
class ScopeBatchResult:
    status: str
    gaps: tuple[CoverageGap, ...] = ()
    requested_days: int = 0
    checked_days: int = 0
    reason: str = ""
    verification: str = "UNVERIFIED"
    coverage: str = "CALLER_DECLARED_SCOPE_ONLY"
    source_authentication: str = "SOURCE_NOT_AUTHENTICATED"
    utc_coverage: str = "UTC_NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"
    global_relations: str = "OTHER_SCOPE_RELATIONS_NOT_CHECKED"
    final_composition: str = "FINAL_COMPOSITION_AND_CONFLICT_CHECK_REQUIRED"


def _requirements(items, venue, coverage_start, coverage_end):
    if type(items) is not tuple or not 1 <= len(items) <= MAX_REQUIREMENTS:
        raise ValueError("invalid request")
    grouped = {}
    total = 0
    for item in items:
        if (
            type(item) is not CoverageRequirement
            or type(item.venue) is not str
            or item.venue != venue
            or type(item.kind) is not str
            or item.kind not in {"future", "option"}
            or not _identity(item.product)
            or not _identity(item.contract)
            or type(item.start_day) is not date
            or type(item.end_day) is not date
            or item.start_day < coverage_start
            or item.end_day > coverage_end
            or item.start_day > item.end_day
        ):
            raise ValueError("invalid scope")
        length = (item.end_day - item.start_day).days + 1
        if length > MAX_DAYS_PER_REQUIREMENT:
            raise ValueError("scope too large")
        total += length
        if total > MAX_BATCHED_QUERY_DAYS:
            raise ValueError("total query budget exceeded")
        key = (item.venue, item.kind, item.product, item.contract)
        prior = grouped.setdefault(key, [])
        if any(
            item.start_day <= previous.end_day and previous.start_day <= item.end_day
            for previous in prior
        ):
            raise ValueError("overlapping requested ranges")
        prior.append(item)
    return grouped, total


def _view(content, key):
    venue, kind, product, contract = key
    rules = []
    for row in content["trading_hours"]:
        if (row["venue"], row["kind"], row["product"]) == (
            venue,
            kind,
            product,
        ) and contract in row["contracts"]:
            rules.append({**row, "contracts": [contract]})
    exceptions = [
        row
        for row in content["exceptions"]
        if (row["venue"], row["kind"], row["product"]) == (venue, kind, product)
        and row["contract"] in (None, contract)
    ]
    return _validate(
        {
            "sources": content["sources"],
            "trading_days": content["trading_days"],
            "trading_hours": rules,
            "exceptions": exceptions,
        }
    )


def _batches(items):
    pending = []
    count = 0
    for item in sorted(items, key=lambda row: (row.start_day, row.end_day)):
        length = (item.end_day - item.start_day).days + 1
        if pending and count + length > MAX_QUERY_DAYS:
            yield tuple(pending)
            pending = []
            count = 0
        pending.append(item)
        count += length
    if pending:
        yield tuple(pending)


def _gaps(items, reason):
    return tuple(
        CoverageGap(
            item.venue, item.kind, item.product, item.contract, item.start_day, item.end_day, reason
        )
        for item in items
    )


def check_scope_batches(
    candidate: AutoCandidateResult, *, requirements: tuple[CoverageRequirement, ...]
) -> ScopeBatchResult:
    """Recheck a full candidate and query each declared contract scope in batches."""
    try:
        if (
            type(candidate) is not AutoCandidateResult
            or candidate.status != "CANDIDATE"
            or (candidate.verification, candidate.coverage, candidate.publication)
            != ("UNVERIFIED", "NOT_PROVEN", "NOT_AUTHORIZED")
            or type(candidate.candidate) is not SupplementCandidate
            or candidate.candidate.verification != "UNVERIFIED"
            or type(candidate.candidate.unused_source_ids) is not tuple
        ):
            raise ValueError("invalid candidate")
        venue, start, end = candidate.venue, candidate.coverage_start, candidate.coverage_end
        old_unused = candidate.candidate.unused_source_ids
        captured = build_auto_candidate(
            venue=venue, coverage_start=start, coverage_end=end, layers=candidate.candidate.content
        )
        if (
            captured.status != "CANDIDATE"
            or captured.candidate is None
            or captured.candidate.unused_source_ids != old_unused
        ):
            raise ValueError("cannot capture candidate")
        grouped, total = _requirements(requirements, venue, start, end)
    except Exception:
        return ScopeBatchResult("STRUCTURAL_GAPS", reason="INVALID_CANDIDATE_OR_REQUIREMENTS")
    try:
        normalized = normalize_calendar_candidate(captured.candidate)
        local_consistent = normalized.status == "NORMALIZED"
    except Exception:
        local_consistent = False
    if not local_consistent:
        return ScopeBatchResult(
            "STRUCTURAL_GAPS",
            _gaps(requirements, "FULL_CANDIDATE_INVALID"),
            total,
            0,
            "FULL_CANDIDATE_INVALID",
        )
    gaps = []
    checked = 0
    for key in sorted(grouped):
        items = grouped[key]
        try:
            view = _view(captured.candidate.content, key)
            for batch in _batches(items):
                result = check_candidate_coverage(view, requirements=batch)
                if result.status == "STRUCTURAL_COMPLETE_UNVERIFIED":
                    if result.gaps or result.checked_days != sum(
                        (x.end_day - x.start_day).days + 1 for x in batch
                    ):
                        raise ValueError("invalid child result")
                    checked += result.checked_days
                elif result.status == "STRUCTURAL_GAPS":
                    if not result.gaps:
                        raise ValueError("missing child gap details")
                    gaps.extend(result.gaps)
                    checked += result.checked_days
                else:
                    gaps.extend(_gaps(batch, "SCOPE_CHECK_FAILED"))
        except Exception:
            gaps.extend(_gaps(items, "SCOPE_CHECK_FAILED"))
    if gaps:
        return ScopeBatchResult("STRUCTURAL_GAPS", tuple(gaps), total, checked, "SCOPE_GAPS")
    return ScopeBatchResult("SCOPES_CHECKED_UNVERIFIED", (), total, checked)
