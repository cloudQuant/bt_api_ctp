"""Compare explicit calendar declarations, without establishing their authority.

The inclusive range is in owner trading days, not natural-day coverage. Inputs
must be detached and stable throughout the call. A digest binds the reported
comparison only; it is neither a source proof nor a publication approval.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from bt_api_ctp.calendar_snapshot import CalendarSnapshot, CalendarSnapshotError


@dataclass(frozen=True, order=True)
class CalendarScope:
    venue: str
    kind: str
    product: str
    contract: str


@dataclass(frozen=True)
class CalendarChange:
    category: str
    key: str
    operation: str
    before: str | None
    after: str | None


@dataclass(frozen=True)
class CalendarSemanticDiff:
    status: str
    reason: str
    changes: tuple[CalendarChange, ...] = ()
    digest: str | None = None
    range_basis: str = "OWNER_TRADING_DAY"
    source_authentication: str = "NOT_PROVEN"
    coverage_authentication: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _bounded(content: Any) -> int:
    """Bound traversal and estimate snapshot work before JSON or snapshot creation.

    The work units are conservative operation counts, not a runtime guarantee.
    A six-byte allowance per character covers JSON escaping and UTF-8 encoding.
    """
    if type(content) is not dict:
        raise ValueError("invalid content")
    stack = [(content, 0)]
    nodes = size = max_refs = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 100_000 or depth > 32:
            raise ValueError("content traversal budget exceeded")
        size += 8
        if type(item) is str:
            size += 6 * len(item)
        elif type(item) is dict:
            if len(item) > 100_000 - nodes - len(stack):
                raise ValueError("content node budget exceeded")
            for key, value in item.items():
                if type(key) is not str:
                    raise ValueError("invalid object key")
                size += 6 * len(key) + 4
                if key == "source_ids" and type(value) is list:
                    max_refs = max(max_refs, len(value))
                stack.append((value, depth + 1))
        elif type(item) is list:
            if len(item) > 100_000 - nodes - len(stack):
                raise ValueError("content node budget exceeded")
            stack.extend((value, depth + 1) for value in item)
        elif item is None or type(item) is bool:
            pass
        elif type(item) is int and item.bit_length() <= 64:
            size += 24
        else:
            raise ValueError("unsupported content value")
        if size > 8_000_000:
            raise ValueError("content byte budget exceeded")
    days, hours, exceptions = (
        content.get(name) for name in ("trading_days", "trading_hours", "exceptions")
    )
    if (
        type(days) is not list
        or type(hours) is not list
        or type(exceptions) is not list
        or len(days) > 4096
        or len(hours) > 512
        or len(exceptions) > 2048
    ):
        raise ValueError("invalid record count")
    contracts = max_windows = 0
    for row in hours:
        if type(row) is not dict or type(row.get("contracts")) is not list:
            raise ValueError("invalid contracts")
        if type(row.get("day_windows")) is not list or type(row.get("night_windows")) is not list:
            raise ValueError("invalid windows")
        contracts += len(row["contracts"])
        max_windows = max(max_windows, len(row["day_windows"]) + len(row["night_windows"]))
    d, h, e = len(days), len(hours), len(exceptions)
    values = contracts * d
    refs = (3 + e) * max_refs
    # Snapshot scans days per row, copies/windows and inspects exceptions per
    # contract-day, then scans all values once per contract scope. Include
    # conservative sort costs for windows and accumulated provenance.
    return (
        2 * h * d
        + contracts * h
        + h * d * max_windows * max(1, max_windows.bit_length())
        + values * (1 + max_windows + e * (1 + 2 * max_windows))
        + values * refs * max(1, refs.bit_length())
        + contracts * values
        + values * max_windows * max(1, (d * max_windows).bit_length())
    )


def _valid_source_texts(value: Any, records: dict) -> bool:
    ids = {json.loads(key) for category, key in records if category == "source_basis"}
    if (
        type(value) is not dict
        or len(value) != len(ids)
        or any(type(key) is not str or len(key) > 128 for key in value)
        or set(value) != ids
    ):
        return False
    size = 0
    for source_text in value.values():
        if type(source_text) is not str or len(source_text) > 2_000_000:
            return False
        if not source_text.strip() or re.fullmatch(
            r"(?i)(?:(?:sha-?(?:1|256|512)|md5)\s*[:=]\s*)?"
            r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64}|[0-9a-f]{128})",
            source_text.strip(),
        ):
            return False
        size += len(source_text.encode("utf-8", errors="strict"))
        if size > 2_000_000:
            return False
    return True


def _records(content: dict, scopes: tuple[CalendarScope, ...], start: str, end: str) -> dict:
    """Project every schema field, including declaration bounds and references."""
    result = {}
    products = {(s.venue, s.kind, s.product) for s in scopes}
    venues = {s.venue for s in scopes}
    relevant_refs = set()

    def put(category, key, value):
        identity = (category, _json(key))
        if identity in result:
            raise ValueError("ambiguous comparison identity")
        result[identity] = _json(value)
        if isinstance(value, dict):
            relevant_refs.update(value.get("source_ids", ()))

    for row in content["trading_days"]:
        if row["venue"] in venues and start <= row["day"] <= end:
            put(
                "trading_day",
                [row["venue"], row["day"]],
                {**row, "source_ids": sorted(row["source_ids"])},
            )
    for row in content["trading_hours"]:
        product = (row["venue"], row["kind"], row["product"])
        if product not in products or row["valid_to"] < start or row["valid_from"] > end:
            continue
        contracts = sorted(row["contracts"])
        contract_set = set(contracts)
        if not any(
            (s.venue, s.kind, s.product) == product and s.contract in contract_set for s in scopes
        ):
            continue
        key = [*product, contracts, row["valid_from"], row["valid_to"]]
        put(
            "applicability",
            key,
            {
                k: (sorted(v) if k in ("contracts", "source_ids") else v)
                for k, v in row.items()
                if k not in ("day_windows", "night_windows", "night_mappings")
            },
        )
        for field in ("day_windows", "night_windows"):
            for window in row[field]:
                put("trading_session", [*key, field, window["id"]], window)
        for mapping in row["night_mappings"]:
            if start <= mapping["trading_day"] <= end:
                put(
                    "night_mapping",
                    [*key, mapping["trading_day"]],
                    {**mapping, "source_ids": sorted(mapping["source_ids"])},
                )
    for row in content["exceptions"]:
        product = (row["venue"], row["kind"], row["product"])
        if (
            product in products
            and start <= row["trading_day"] <= end
            and (
                row["contract"] is None
                or any(
                    (s.venue, s.kind, s.product) == product and s.contract == row["contract"]
                    for s in scopes
                )
            )
        ):
            key = [*product, row["contract"], row["trading_day"], row["action"], row["window_id"]]
            put("exception", key, {**row, "source_ids": sorted(row["source_ids"])})
    for source in content["sources"]:
        if source["id"] in relevant_refs:
            put("source_basis", source["id"], source)
    return result


def compare_calendar_semantics(
    before: dict,
    after: dict,
    *,
    scopes: tuple[CalendarScope, ...],
    start: date,
    end: date,
    source_text_before: dict | None = None,
    source_text_after: dict | None = None,
) -> CalendarSemanticDiff:
    """Compare structural declarations in an explicit owner-day/contract scope.

    Changes to source references or descriptions are returned but remain UNKNOWN:
    this schema cannot explain whether an external source changed. Empty, partial,
    conflicting, or unrepresented scope is UNKNOWN. COMPLETE flags are only
    declarations and never establish official coverage or freshness. Caller-supplied
    source texts are not authenticated and are not coverage or freshness evidence.
    """
    try:
        if (
            type(scopes) is not tuple
            or not scopes
            or len(scopes) > 100_000
            or any(type(s) is not CalendarScope for s in scopes)
            or any(
                any(
                    type(part) is not str or len(part) > 128
                    for part in (s.venue, s.kind, s.product, s.contract)
                )
                for s in scopes
            )
            or len(set(scopes)) != len(scopes)
            or type(start) is not date
            or type(end) is not date
            or start > end
            or (end - start).days > 3660
            or len(scopes) * ((end - start).days + 1) > 100000
        ):
            raise ValueError("invalid scope")
        before_work = _bounded(before)
        after_work = _bounded(after)
        queries = 2 * len(scopes) * ((end - start).days + 1)
        projection = len(scopes) * sum(
            len(content["trading_hours"]) + len(content["exceptions"])
            for content in (before, after)
        )
        if before_work + after_work + queries + projection > 8_000_000:
            raise ValueError("comparison work budget exceeded")
        old = CalendarSnapshot(before, calendar_ref="semantic-before")
        new = CalendarSnapshot(after, calendar_ref="semantic-after")
        left = _records(before, scopes, start.isoformat(), end.isoformat())
        right = _records(after, scopes, start.isoformat(), end.isoformat())
        changes = []
        uncertain_source = False
        for category, key in sorted(left.keys() | right.keys()):
            a, b = left.get((category, key)), right.get((category, key))
            if a == b:
                continue
            op = "added" if a is None else "removed" if b is None else "modified"
            changes.append(CalendarChange(category, key, op, a, b))
            if category == "source_basis":
                uncertain_source = True
            elif a is not None and b is not None:
                av, bv = json.loads(a), json.loads(b)
                if isinstance(av, dict) and av.get("source_ids") != bv.get("source_ids"):
                    uncertain_source = True
        source_missing = not (
            _valid_source_texts(source_text_before, left)
            and _valid_source_texts(source_text_after, right)
        )
        source_changed = not source_missing and source_text_before != source_text_after
        missing = False
        for scope in scopes:
            for offset in range((end - start).days + 1):
                day = start + timedelta(days=offset)
                for snapshot in (old, new):
                    result = snapshot.query(
                        venue=scope.venue,
                        kind=scope.kind,
                        product=scope.product,
                        contract=scope.contract,
                        trading_day=day,
                    )
                    if result.status != "COMPLETE":
                        missing = True
        reason = (
            "SCOPE_INCOMPLETE"
            if missing
            else "SOURCE_EVIDENCE_MISSING"
            if source_missing
            else "SOURCE_CHANGE_UNEXPLAINED"
            if uncertain_source or source_changed
            else "DECLARATIONS_DIFFER"
            if changes
            else "DECLARATIONS_EQUAL"
        )
        status = (
            "UNKNOWN"
            if missing or source_missing or uncertain_source or source_changed
            else "CHANGED"
            if changes
            else "SAME"
        )
        payload = {
            "schema": "ctp.calendar-semantic-diff/1",
            "basis": "OWNER_TRADING_DAY",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "scopes": [[s.venue, s.kind, s.product, s.contract] for s in sorted(scopes)],
            "status": status,
            "reason": reason,
            "before": sorted((list(k), v) for k, v in left.items()),
            "after": sorted((list(k), v) for k, v in right.items()),
            "source_text_before": source_text_before if not source_missing else None,
            "source_text_after": source_text_after if not source_missing else None,
            "changes": [[c.category, c.key, c.operation, c.before, c.after] for c in changes],
        }
        digest = hashlib.sha256(_json(payload).encode("utf-8", errors="strict")).hexdigest()
        return CalendarSemanticDiff(status, reason, tuple(changes), digest)
    except (
        CalendarSnapshotError,
        TypeError,
        ValueError,
        KeyError,
        OverflowError,
        UnicodeError,
        RecursionError,
    ):
        return CalendarSemanticDiff("UNKNOWN", "INVALID_OR_CONFLICTING_INPUT")
