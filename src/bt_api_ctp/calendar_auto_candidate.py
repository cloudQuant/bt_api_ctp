# ruff: noqa: UP045
"""Bounded in-memory automatic calendar candidate, not a release path.

The caller supplies already-parsed JSON-like data; this module performs no IO,
network access, collection, publication or authentication. It copies once into
bounded plain JSON, validates the existing supplement schema, and checks local
venue and obvious identity conflicts. The inclusive date range is a caller
claim only and need not prove complete data. This is not source, venue, UTC or
listing verification. Concurrent input mutation during capture is unsupported.
Manual YAML retains its independent byte, event and list limits unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Optional

from bt_api_ctp.calendar_supplement import MAX_LIST_ITEMS, SupplementCandidate, _validate

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_JSON_NODES = 100000
MAX_JSON_DEPTH = 24
_MAX_STRING_LENGTH = 4096
_MAX_MAP_KEYS = 32
_VENUES = frozenset({"SHFE", "INE", "CZCE", "DCE", "CFFEX", "GFEX"})


@dataclass(frozen=True)
class AutoCandidateResult:
    status: str
    candidate: Optional[SupplementCandidate] = None
    venue: Optional[str] = None
    coverage_start: Optional[date] = None
    coverage_end: Optional[date] = None
    reason: str = ""
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"


class _Budget:
    def __init__(self):
        self.nodes = 0
        self.bytes = 0
        self.active = set()

    def add(self, *, nodes=0, bytes_=0):
        self.nodes += nodes
        self.bytes += bytes_
        if self.nodes > MAX_JSON_NODES or self.bytes > MAX_JSON_BYTES:
            raise ValueError("JSON budget exceeded")

    def string(self, value):
        if len(value) > _MAX_STRING_LENGTH:
            raise ValueError("string too long")
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8", "strict")
        self.add(bytes_=len(raw))
        return value

    def copy(self, value, depth=0):
        if depth > MAX_JSON_DEPTH:
            raise ValueError("JSON depth exceeded")
        self.add(nodes=1)
        if value is None:
            self.add(bytes_=4)
            return None
        if type(value) is bool:
            self.add(bytes_=4 if value else 5)
            return value
        if type(value) is int:
            if value.bit_length() > 63:
                raise ValueError("integer too large")
            self.add(bytes_=len(str(value)))
            return value
        if type(value) is str:
            return self.string(value)
        if type(value) not in (dict, list):
            raise ValueError("unsupported JSON value")
        if (type(value) is list and len(value) > MAX_LIST_ITEMS) or (
            type(value) is dict and len(value) > _MAX_MAP_KEYS
        ):
            raise ValueError("container too large")
        identity = id(value)
        if identity in self.active:
            raise ValueError("cyclic input")
        self.active.add(identity)
        try:
            self.add(bytes_=2 + max(0, len(value) - 1))
            if type(value) is list:
                return [self.copy(child, depth + 1) for child in value]
            result = {}
            for key, child in value.items():
                if type(key) is not str:
                    raise ValueError("non-string JSON key")
                self.add(nodes=1, bytes_=1)
                self.string(key)
                result[key] = self.copy(child, depth + 1)
            return result
        finally:
            self.active.remove(identity)


def _check_local_consistency(content, venue):
    for name in ("trading_days", "trading_hours", "exceptions"):
        if any(row["venue"] != venue for row in content[name]):
            raise ValueError("cross-venue record")
    seen_days = set()
    for row in content["trading_days"]:
        key = (row["venue"], row["day"])
        if key in seen_days:
            raise ValueError("duplicate venue day")
        seen_days.add(key)
    owners = {}
    periods = {}
    for row in content["trading_hours"]:
        for contract in row["contracts"]:
            key = (venue, contract)
            owner = (row["kind"], row["product"])
            if key in owners and owners[key] != owner:
                raise ValueError("ambiguous contract owner")
            owners[key] = owner
            periods.setdefault((key, owner), []).append((row["valid_from"], row["valid_to"]))
    for ranges in periods.values():
        ranges.sort()
        if any(first[1] >= second[0] for first, second in zip(ranges, ranges[1:])):
            raise ValueError("overlapping contract ranges")
    seen_exceptions = set()
    exception_scopes = {}
    for row in content["exceptions"]:
        key = (
            row["venue"],
            row["kind"],
            row["product"],
            row["contract"],
            row["trading_day"],
            row["action"],
            row["window_id"],
        )
        if key in seen_exceptions:
            raise ValueError("duplicate exception")
        seen_exceptions.add(key)
        scope = (row["venue"], row["kind"], row["product"], row["trading_day"])
        for previous in exception_scopes.setdefault(scope, []):
            same_contract = (
                row["contract"] == previous["contract"]
                or row["contract"] is None
                or previous["contract"] is None
            )
            if same_contract and (
                row["action"] == "close_day"
                or previous["action"] == "close_day"
                or row["window_id"] == previous["window_id"]
            ):
                raise ValueError("ambiguous exception scope")
        exception_scopes[scope].append(row)


def build_auto_candidate(
    *, venue: str, coverage_start: date, coverage_end: date, layers: object
) -> AutoCandidateResult:
    """Construct one detached, unverified candidate from bounded caller input."""
    try:
        if (
            type(venue) is not str
            or venue not in _VENUES
            or type(coverage_start) is not date
            or type(coverage_end) is not date
            or coverage_start > coverage_end
            or (coverage_end - coverage_start).days + 1 > 731
        ):
            raise ValueError("invalid declared scope")
        budget = _Budget()
        snapshot = budget.copy(layers)
        # Check actual bytes as well as the incremental bound; never retain the
        # temporary encoding in the returned object.
        encoded = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8", "strict")
        if len(encoded) > MAX_JSON_BYTES or len(encoded) != budget.bytes:
            raise ValueError("JSON byte budget mismatch")
        validated = _validate(snapshot)
        _check_local_consistency(validated.content, venue)
        return AutoCandidateResult("CANDIDATE", validated, venue, coverage_start, coverage_end)
    except Exception:
        return AutoCandidateResult("INVALID_INPUT", reason="INVALID_OR_BOUNDED_INPUT")
