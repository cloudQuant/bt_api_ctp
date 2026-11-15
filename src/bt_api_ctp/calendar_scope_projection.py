"""Project one owner-day contract scope after validating its complete venue.

The input must remain stable during this call. All rows of the selected venue
are validated before projection, but other venues and cross-venue relations are
not validated. This is a local untrusted structural view, not official coverage
or authority. The full-input and venue-work budgets are estimates, not resource
or execution-time guarantees.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from bt_api_ctp.calendar_semantic_diff import CalendarScope, _bounded, _records
from bt_api_ctp.calendar_snapshot import CalendarSnapshot, CalendarSnapshotError

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_VENUES = frozenset({"SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX"})


@dataclass(frozen=True)
class ScopeProjection:
    status: str
    content: dict[str, Any] | None = None
    required_source_ids: tuple[str, ...] = ()
    reason: str = "INVALID_OR_INCOMPLETE"
    checked: str = "SELECTED_VENUE_STRUCTURE_ONLY"
    unchecked: str = "OTHER_VENUES_AND_CROSS_VENUE_RELATIONS"
    authentication: str = "NOT_PROVEN"


def _bound_full(value: Any) -> None:
    pending = [(value, 0)]
    nodes = size = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 1_000_000 or depth > 32:
            raise ValueError("structure limit")
        size += 8
        if type(item) is str:
            size += 6 * len(item)
        elif type(item) is dict:
            if len(item) > 1_000_000 - nodes - len(pending):
                raise ValueError("node limit")
            for k, v in item.items():
                if type(k) is not str:
                    raise ValueError("invalid key")
                size += 6 * len(k) + 4
                pending.append((v, depth + 1))
        elif type(item) is list:
            if len(item) > 1_000_000 - nodes - len(pending):
                raise ValueError("node limit")
            pending.extend((v, depth + 1) for v in item)
        elif item is None or type(item) is bool:
            pass
        elif type(item) is int and item.bit_length() <= 64:
            size += 24
        else:
            raise ValueError("invalid type")
        if size > 48_000_000:
            raise ValueError("size limit")


def _referenced(rows):
    found = set()
    pending = list(rows)
    while pending:
        item = pending.pop()
        if type(item) is dict:
            for key, value in item.items():
                if key == "source_ids":
                    if type(value) is not list or any(type(v) is not str for v in value):
                        raise ValueError("invalid references")
                    found.update(value)
                pending.append(value)
        elif type(item) is list:
            pending.extend(item)
    return found


def prepare_venue_scope(
    content: dict, *, scope: CalendarScope, start: date, end: date
) -> ScopeProjection:
    """Validate all selected-venue records, then project one exact scope.

    The returned source IDs are exactly the references needed by the existing
    scoped comparator for the requested inclusive owner trading-day interval.
    The view contains the whole selected scope's date history to retain night
    ownership and exception validation. Structural completeness is not proof of
    all contracts, source truth, freshness or permission to publish.
    """
    try:
        if (
            type(scope) is not CalendarScope
            or scope.venue not in _VENUES
            or scope.kind not in {"future", "option"}
            or any(
                type(v) is not str or not _ID.fullmatch(v) for v in (scope.product, scope.contract)
            )
            or type(start) is not date
            or type(end) is not date
            or start > end
            or (end - start).days + 1 > 731
        ):
            raise ValueError("invalid scope")
        _bound_full(content)
        if type(content) is not dict or set(content) != {
            "sources",
            "trading_days",
            "trading_hours",
            "exceptions",
        }:
            raise ValueError("invalid content")
        if any(type(content[k]) is not list for k in content):
            raise ValueError("invalid layer")
        if (
            len(content["trading_days"]) > 12000
            or len(content["trading_hours"]) > 3072
            or len(content["exceptions"]) > 12288
            or len(content["sources"]) > 6000
        ):
            raise ValueError("layer limit")
        sources = {}
        for source in content["sources"]:
            if (
                type(source) is not dict
                or set(source) != {"id", "description"}
                or type(source["id"]) is not str
                or not _ID.fullmatch(source["id"])
                or type(source["description"]) is not str
                or not source["description"].strip()
                or source["id"] in sources
            ):
                raise ValueError("invalid source inventory")
            sources[source["id"]] = source
        venue = {}
        for layer in ("trading_days", "trading_hours", "exceptions"):
            rows = content[layer]
            if any(
                type(row) is not dict
                or type(row.get("venue")) is not str
                or not _ID.fullmatch(row["venue"])
                for row in rows
            ):
                raise ValueError("invalid venue row")
            venue[layer] = [row for row in rows if row["venue"] == scope.venue]
        refs = _referenced(list(venue.values()))
        if not refs or not refs <= sources.keys():
            raise ValueError("missing source")
        venue["sources"] = [sources[key] for key in sorted(refs)]
        if _bounded(venue) > 4_000_000:
            raise ValueError("venue work limit")
        # Detach before structural validation or projection.
        venue = json.loads(json.dumps(venue, ensure_ascii=True, allow_nan=False))
        complete = CalendarSnapshot(venue, calendar_ref="venue-projection")
        for offset in range((end - start).days + 1):
            result = complete.query(
                venue=scope.venue,
                kind=scope.kind,
                product=scope.product,
                contract=scope.contract,
                trading_day=start + timedelta(days=offset),
            )
            if result.status != "COMPLETE":
                raise ValueError("scope incomplete")
        view = {
            "trading_days": venue["trading_days"],
            "trading_hours": [
                {**row, "contracts": [scope.contract]}
                for row in venue["trading_hours"]
                if (row["venue"], row["kind"], row["product"])
                == (scope.venue, scope.kind, scope.product)
                and scope.contract in row["contracts"]
            ],
            "exceptions": [
                row
                for row in venue["exceptions"]
                if (row["venue"], row["kind"], row["product"])
                == (scope.venue, scope.kind, scope.product)
                and row["contract"] in (None, scope.contract)
            ],
        }
        view_refs = _referenced(list(view.values()))
        view["sources"] = [sources[key] for key in sorted(view_refs)]
        if _bounded(view) > 4_000_000:
            raise ValueError("scope work limit")
        CalendarSnapshot(view, calendar_ref="scope-projection")
        records = _records(view, (scope,), start.isoformat(), end.isoformat())
        relevant = tuple(
            sorted(json.loads(key) for category, key in records if category == "source_basis")
        )
        if not relevant:
            raise ValueError("missing scope source")
        return ScopeProjection("READY_UNVERIFIED", view, relevant, "DECLARATION_VIEW_ONLY")
    except (
        CalendarSnapshotError,
        TypeError,
        ValueError,
        KeyError,
        OverflowError,
        UnicodeError,
        RecursionError,
    ):
        return ScopeProjection("UNKNOWN")
