# ruff: noqa: UP045
"""Deterministic bytes for an unverified, partial calendar supplement.

Only lists with explicit set semantics in the experimental schema are sorted.
Repeated day keys, conflicting scope ownership, overlapping same-contract
periods, ambiguous exception scope, and overlapping windows within a session
are rejected. This is not a complete conflict analysis: cross-layer relations,
source authority, coverage, release permission and actual trading rules remain
unverified. No data is published and no dates or sessions are inferred. Inputs
must not be modified concurrently during validation and copying.
This digest covers only the four supplied layers and preserves every field.
Unresolved no-night candidates held outside those layers are not included;
likewise a whole-page source digest can change on non-trading page edits.
It proves ordering stability for this structure, not absence of substantive
calendar changes and not a version-publication or release decision.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Optional

from bt_api_ctp.calendar_supplement import SupplementCandidate, SupplementError, _validate

_MAX_TOTAL_ITEMS = 20000


@dataclass(frozen=True)
class NormalizedCandidate:
    status: str
    json_bytes: Optional[bytes] = None
    sha256: Optional[str] = None
    reason: str = ""
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"
    comparison: str = "FOUR_LAYER_STRUCTURE_ONLY"
    unresolved_nights: str = "NOT_INCLUDED"


def _unique(values):
    values = list(values)
    if len(values) != len(set(values)):
        raise ValueError("duplicate key")


def _window_intervals(windows):
    intervals = []
    for window in windows:
        start_h, start_m = map(int, window["start"].split(":"))
        end_h, end_m = map(int, window["end"].split(":"))
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m + 1440 * window["end_day_offset"]
        intervals.append((start, end))
    intervals.sort()
    if any(first[1] > second[0] for first, second in zip(intervals, intervals[1:])):
        raise ValueError("overlapping windows")


def _check_and_order(content):
    content["sources"].sort(key=lambda row: row["id"])
    _unique((row["venue"], row["day"]) for row in content["trading_days"])
    for row in content["trading_days"]:
        row["source_ids"].sort()
    content["trading_days"].sort(key=lambda row: (row["venue"], row["day"]))

    owners = {}
    periods = {}
    for row in content["trading_hours"]:
        row["source_ids"].sort()
        row["contracts"].sort()
        for key in ("day_windows", "night_windows"):
            _window_intervals(row[key])
            row[key].sort(key=lambda window: window["id"])
        for mapping in row["night_mappings"]:
            mapping["source_ids"].sort()
        row["night_mappings"].sort(key=lambda mapping: mapping["trading_day"])
        for contract in row["contracts"]:
            owner_key = (row["venue"], contract)
            owner = (row["kind"], row["product"])
            if owner_key in owners and owners[owner_key] != owner:
                raise ValueError("ambiguous contract ownership")
            owners[owner_key] = owner
            scope = (row["venue"], row["kind"], row["product"], contract)
            for start, end in periods.setdefault(scope, []):
                if row["valid_from"] <= end and start <= row["valid_to"]:
                    raise ValueError("overlapping contract periods")
            periods[scope].append((row["valid_from"], row["valid_to"]))
    content["trading_hours"].sort(
        key=lambda row: (
            row["venue"],
            row["kind"],
            row["product"],
            row["valid_from"],
            row["valid_to"],
            tuple(row["contracts"]),
        )
    )

    exceptions = content["exceptions"]
    for row in exceptions:
        row["source_ids"].sort()
    for index, row in enumerate(exceptions):
        for previous in exceptions[:index]:
            same_scope = all(
                row[key] == previous[key] for key in ("venue", "kind", "product", "trading_day")
            ) and (
                row["contract"] == previous["contract"]
                or row["contract"] is None
                or previous["contract"] is None
            )
            if same_scope and (
                row["action"] == "close_day"
                or previous["action"] == "close_day"
                or row["window_id"] == previous["window_id"]
            ):
                raise ValueError("ambiguous exceptions")
    exceptions.sort(
        key=lambda row: (
            row["venue"],
            row["kind"],
            row["product"],
            row["trading_day"],
            row["contract"] or "",
            row["action"],
            row["window_id"] or "",
        )
    )
    return content


def _bounded_total(content):
    total = sum(len(value) for value in content.values())
    total += sum(len(row["source_ids"]) for row in content["trading_days"])
    total += sum(len(row["source_ids"]) for row in content["exceptions"])
    for row in content["trading_hours"]:
        total += sum(
            len(row[key])
            for key in ("source_ids", "contracts", "day_windows", "night_windows", "night_mappings")
        )
        total += sum(len(mapping["source_ids"]) for mapping in row["night_mappings"])
        if total > _MAX_TOTAL_ITEMS:
            raise ValueError("candidate exceeds total item bound")
    if total > _MAX_TOTAL_ITEMS:
        raise ValueError("candidate exceeds total item bound")


def normalize_calendar_candidate(candidate: SupplementCandidate) -> NormalizedCandidate:
    """Return canonical UTF-8 JSON and SHA-256, or no bytes for invalid input."""
    try:
        if type(candidate) is not SupplementCandidate or candidate.verification != "UNVERIFIED":
            raise ValueError("invalid candidate")
        validated = _validate(candidate.content)
        if type(candidate.unused_source_ids) is not tuple or (
            validated.unused_source_ids != candidate.unused_source_ids
        ):
            raise ValueError("invalid source bookkeeping")
        _bounded_total(validated.content)
        detached = deepcopy(validated.content)
        _validate(detached)
        canonical = json.dumps(
            _check_and_order(detached),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
        return NormalizedCandidate("NORMALIZED", canonical, hashlib.sha256(canonical).hexdigest())
    except (AttributeError, TypeError, ValueError, OverflowError, UnicodeError, SupplementError):
        return NormalizedCandidate("INVALID_INPUT", reason="INVALID_OR_AMBIGUOUS_INPUT")
