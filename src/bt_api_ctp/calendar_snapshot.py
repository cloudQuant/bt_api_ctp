"""Internal experimental, local-only calendar snapshot; not a venue authority.

Input is the SDK store's four content layers, not its publication envelope.
Dates are explicit, effective bounds are inclusive, and every open product-day
requires a night mapping (null means explicitly no night). Only whole-window
cancellation and whole-day closure are supported. No identifiers or sessions
are inferred, and source IDs establish references, not correctness or authority.
This module is not wired into an existing CTP or research calendar interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


class CalendarSnapshotError(ValueError):
    """Invalid, ambiguous, or unsupported experimental snapshot."""


@dataclass(frozen=True)
class CalendarWindow:
    window_id: str
    phase: str
    trading_day: date
    start_utc: datetime
    end_utc: datetime
    start_local: datetime
    end_local: datetime

    def contains(self, at_utc: datetime) -> bool:
        _utc(at_utc)
        return self.start_utc <= at_utc < self.end_utc


@dataclass(frozen=True)
class CalendarResult:
    status: str
    reason: str
    calendar_ref: str
    venue: str
    kind: str
    product: str
    contract: str
    trading_day: date
    windows: tuple[CalendarWindow, ...]
    source_ids: tuple[str, ...]
    sources_verified: bool = False


@dataclass(frozen=True)
class VenueDayResult:
    status: str
    reason: str
    calendar_ref: str
    venue: str
    trading_day: date
    day_status: str | None
    source_ids: tuple[str, ...]
    sources_verified: bool = False


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise CalendarSnapshotError("unexpected or missing fields")
    return value


def _list(value: Any) -> list:
    if type(value) is not list:
        raise CalendarSnapshotError("expected array")
    return value


def _identifier(value: Any) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise CalendarSnapshotError("invalid identifier")
    return value


def _date(value: Any) -> date:
    if type(value) is not str:
        raise CalendarSnapshotError("invalid date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CalendarSnapshotError("invalid date") from exc
    if parsed.isoformat() != value:
        raise CalendarSnapshotError("date must use YYYY-MM-DD")
    return parsed


def _time(value: Any) -> time:
    if type(value) is not str or _CLOCK.fullmatch(value) is None:
        raise CalendarSnapshotError("time must use HH:MM")
    return time.fromisoformat(value)


def _utc(value: Any) -> None:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CalendarSnapshotError("an aware UTC datetime is required")


def _refs(value: Any, source_ids: set[str]) -> tuple[str, ...]:
    refs = tuple(_identifier(item) for item in _list(value))
    if not refs or len(set(refs)) != len(refs) or not set(refs) <= source_ids:
        raise CalendarSnapshotError("missing, duplicate, or unknown source reference")
    return refs


def _scope(row: dict[str, Any]) -> tuple[str, str, str]:
    venue, kind, product = (_identifier(row[key]) for key in ("venue", "kind", "product"))
    if kind not in ("future", "option"):
        raise CalendarSnapshotError("unsupported contract kind")
    return venue, kind, product


def _templates(value: Any, *, night: bool) -> tuple[tuple[str, str, time, time, int], ...]:
    output = []
    for entry in _list(value):
        row = _object(entry, {"id", "phase", "start", "end", "end_day_offset"})
        name = _identifier(row["id"])
        if row["phase"] not in ("continuous", "auction"):
            raise CalendarSnapshotError("unsupported phase")
        offset = row["end_day_offset"]
        if type(offset) is not int or offset not in (0, 1) or (not night and offset != 0):
            raise CalendarSnapshotError("unsupported session day offset")
        start, end = _time(row["start"]), _time(row["end"])
        if (offset == 0 and end <= start) or (offset == 1 and end >= start):
            raise CalendarSnapshotError("invalid session interval")
        output.append((name, row["phase"], start, end, offset))
    return tuple(output)


def _window(day: date, local_date: date, template: tuple) -> CalendarWindow:
    name, phase, start, end, offset = template
    try:
        start_local = datetime.combine(local_date, start, tzinfo=_SHANGHAI)
        end_local = datetime.combine(local_date + timedelta(days=offset), end, tzinfo=_SHANGHAI)
    except (OverflowError, ValueError) as exc:
        raise CalendarSnapshotError("session date out of range") from exc
    return CalendarWindow(
        name,
        phase,
        day,
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
        start_local,
        end_local,
    )


class CalendarSnapshot:
    """Validated detached snapshot; only exact contract identities are in scope.

    Trading-day records are explicit coverage; a missing date is UNKNOWN. Each
    query returns the complete planned windows for one trading day, not a claim
    about current venue state or a query over arbitrary UTC coverage.
    """

    __slots__ = ("_ref", "_scopes", "_values", "_days", "_day_venues", "_sealed")

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("calendar snapshot is read-only")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("calendar snapshot is read-only")
        object.__delattr__(self, name)

    def __init__(self, content: dict[str, Any], *, calendar_ref: str):
        self._ref = _identifier(calendar_ref)
        content = _object(content, {"trading_days", "trading_hours", "exceptions", "sources"})
        sources: set[str] = set()
        for item in _list(content["sources"]):
            source = _object(item, {"id", "description"})
            source_id = _identifier(source["id"])
            if (
                source_id in sources
                or type(source["description"]) is not str
                or not source["description"].strip()
            ):
                raise CalendarSnapshotError("duplicate or invalid source")
            sources.add(source_id)
        if not sources:
            raise CalendarSnapshotError("source inventory is empty")

        days: dict[tuple[str, date], tuple[str, tuple[str, ...]]] = {}
        for item in _list(content["trading_days"]):
            row = _object(item, {"venue", "day", "status", "source_ids"})
            key = (_identifier(row["venue"]), _date(row["day"]))
            if key in days or row["status"] not in ("open", "closed"):
                raise CalendarSnapshotError("duplicate day or invalid status")
            days[key] = (row["status"], _refs(row["source_ids"], sources))

        # Normalized rows hold only immutable values and are never exposed.
        rows = []
        identities: dict[tuple[str, str], tuple[str, str]] = {}
        scopes: dict[tuple[str, str, str, str], list[tuple[date, date]]] = {}
        for item in _list(content["trading_hours"]):
            row = _object(
                item,
                {
                    "venue",
                    "kind",
                    "product",
                    "contracts",
                    "valid_from",
                    "valid_to",
                    "day_windows",
                    "night_windows",
                    "night_mappings",
                    "source_ids",
                    "windows_complete",
                    "exceptions_complete",
                },
            )
            scope = _scope(row)
            if (
                type(row["windows_complete"]) is not bool
                or type(row["exceptions_complete"]) is not bool
            ):
                raise CalendarSnapshotError("completeness must be explicit booleans")
            start, end = _date(row["valid_from"]), _date(row["valid_to"])
            if start > end:
                raise CalendarSnapshotError("invalid effective period")
            contracts = tuple(_identifier(c) for c in _list(row["contracts"]))
            if not contracts or len(set(contracts)) != len(contracts):
                raise CalendarSnapshotError("empty or duplicate contracts")
            day_templates = _templates(row["day_windows"], night=False)
            night_templates = _templates(row["night_windows"], night=True)
            names = [entry[0] for entry in day_templates + night_templates]
            if len(set(names)) != len(names):
                raise CalendarSnapshotError("duplicate session id")
            mappings = {}
            for item_mapping in _list(row["night_mappings"]):
                mapping = _object(item_mapping, {"trading_day", "start_date", "source_ids"})
                day = _date(mapping["trading_day"])
                local = None if mapping["start_date"] is None else _date(mapping["start_date"])
                if (
                    day in mappings
                    or not start <= day <= end
                    or days.get((scope[0], day), (None,))[0] != "open"
                    or (local is not None and (local >= day or not night_templates))
                ):
                    raise CalendarSnapshotError("invalid night mapping")
                mappings[day] = (local, _refs(mapping["source_ids"], sources))
            for (venue, day), (status, _) in days.items():
                if (
                    venue == scope[0]
                    and status == "open"
                    and start <= day <= end
                    and day not in mappings
                ):
                    raise CalendarSnapshotError("missing explicit night mapping")
            for contract in contracts:
                owner_key = (scope[0], contract)
                owner = (scope[1], scope[2])
                if owner_key in identities and identities[owner_key] != owner:
                    raise CalendarSnapshotError("ambiguous contract ownership")
                identities[owner_key] = owner
                full_scope = (*scope, contract)
                for old_start, old_end in scopes.setdefault(full_scope, []):
                    if start <= old_end and old_start <= end:
                        raise CalendarSnapshotError("overlapping effective periods")
                scopes[full_scope].append((start, end))
            rows.append(
                (
                    scope,
                    contracts,
                    start,
                    end,
                    day_templates,
                    night_templates,
                    mappings,
                    _refs(row["source_ids"], sources),
                    row["windows_complete"] and row["exceptions_complete"],
                )
            )
        if not rows:
            raise CalendarSnapshotError("no contract scope")

        exceptions = []
        for item in _list(content["exceptions"]):
            row = _object(
                item,
                {
                    "venue",
                    "kind",
                    "product",
                    "contract",
                    "trading_day",
                    "action",
                    "window_id",
                    "source_ids",
                },
            )
            scope = _scope(row)
            contract = None if row["contract"] is None else _identifier(row["contract"])
            day = _date(row["trading_day"])
            action = row["action"]
            name = row["window_id"]
            if action not in ("close_day", "cancel_window"):
                raise CalendarSnapshotError("unsupported exception action")
            if action == "close_day" and name is not None:
                raise CalendarSnapshotError("close_day must not specify a window")
            if action == "cancel_window":
                name = _identifier(name)
            if days.get((scope[0], day), (None,))[0] != "open":
                raise CalendarSnapshotError("exception requires known open day")
            exceptions.append(
                (scope, contract, day, action, name, _refs(row["source_ids"], sources))
            )

        # Retain original indices and order: wildcard and contract-specific
        # exceptions must still interact exactly as they did in input order.
        exception_groups = {}
        for index, exception in enumerate(exceptions):
            exception_groups.setdefault((exception[0], exception[2]), []).append((index, exception))
        exception_index = MappingProxyType(
            {key: tuple(group) for key, group in exception_groups.items()}
        )

        values: dict[
            tuple[str, str, str, str, date],
            tuple[tuple[CalendarWindow, ...], tuple[str, ...], bool],
        ] = {}
        applied = set()
        for (
            scope,
            contracts,
            start,
            end,
            day_templates,
            night_templates,
            mappings,
            rule_sources,
            complete,
        ) in rows:
            for (venue, day), (status, day_sources) in days.items():
                if venue != scope[0] or not start <= day <= end:
                    continue
                windows = []
                mapping_sources: tuple[str, ...] = ()
                if status == "open":
                    local_date, mapping_sources = mappings[day]
                    windows.extend(_window(day, day, template) for template in day_templates)
                    if local_date is not None:
                        windows.extend(
                            _window(day, local_date, template) for template in night_templates
                        )
                windows.sort(key=lambda window: window.start_utc)
                if any(a.end_utc > b.start_utc for a, b in zip(windows, windows[1:])):
                    raise CalendarSnapshotError("overlapping session windows")
                for contract in contracts:
                    local_windows = list(windows)
                    provenance = set(day_sources + rule_sources + mapping_sources)
                    used_names: set[str] = set()
                    closed = False
                    for index, (_, ex_contract, _, action, name, refs) in exception_index.get(
                        (scope, day), ()
                    ):
                        if ex_contract not in (None, contract):
                            continue
                        applied.add(index)
                        if closed or (action == "close_day" and used_names):
                            raise CalendarSnapshotError("overlapping exceptions")
                        if action == "close_day":
                            closed = True
                            local_windows.clear()
                        else:
                            if name in used_names or not any(
                                w.window_id == name for w in local_windows
                            ):
                                raise CalendarSnapshotError(
                                    "duplicate or unmatched exception window"
                                )
                            used_names.add(name)
                            local_windows = [w for w in local_windows if w.window_id != name]
                        provenance.update(refs)
                    values[(*scope, contract, day)] = (
                        tuple(local_windows),
                        tuple(sorted(provenance)),
                        complete,
                    )
        if len(applied) != len(exceptions):
            raise CalendarSnapshotError("exception scope or effective period is unknown")
        # Distinct trading days cannot claim overlapping instants for one contract.
        for scope in scopes:
            all_windows = sorted(
                (w for key, (ws, _, _) in values.items() if key[:4] == scope for w in ws),
                key=lambda w: w.start_utc,
            )
            if any(a.end_utc > b.start_utc for a, b in zip(all_windows, all_windows[1:])):
                raise CalendarSnapshotError("overlapping trading day ownership")
        self._scopes = frozenset(scopes)
        self._values = MappingProxyType(values)
        self._days = MappingProxyType(days)
        self._day_venues = frozenset({venue for venue, _ in days} | {scope[0] for scope in scopes})
        self._sealed = True

    def query_venue_day(self, *, venue: str, trading_day: date) -> VenueDayResult:
        """Return the explicit venue-day declaration, without inferring from windows.

        Source IDs identify snapshot references only; they do not authenticate
        those sources or establish trading authority.
        """
        if type(trading_day) is not date:
            raise CalendarSnapshotError("trading_day must be a date")
        venue = _identifier(venue)
        if venue not in self._day_venues:
            status, reason, day_status, sources = (
                "OUT_OF_SCOPE",
                "VENUE_NOT_IN_SNAPSHOT",
                None,
                (),
            )
        elif (venue, trading_day) not in self._days:
            status, reason, day_status, sources = ("UNKNOWN", "COVERAGE_INSUFFICIENT", None, ())
        else:
            day_status, sources = self._days[(venue, trading_day)]
            status, reason = "COMPLETE", "EXPLICIT_DATA"
        return VenueDayResult(status, reason, self._ref, venue, trading_day, day_status, sources)

    def query(
        self, *, venue: str, kind: str, product: str, contract: str, trading_day: date
    ) -> CalendarResult:
        """Return COMPLETE, UNKNOWN coverage, or OUT_OF_SCOPE for a single day."""
        if type(trading_day) is not date:
            raise CalendarSnapshotError("trading_day must be a date")
        scope = tuple(_identifier(value) for value in (venue, kind, product, contract))
        if scope not in self._scopes:
            status, reason, windows, sources = "OUT_OF_SCOPE", "IDENTITY_NOT_IN_SNAPSHOT", (), ()
        elif (*scope, trading_day) not in self._values:
            status, reason, windows, sources = "UNKNOWN", "COVERAGE_INSUFFICIENT", (), ()
        else:
            windows, sources, complete = self._values[(*scope, trading_day)]
            if complete:
                status, reason = "COMPLETE", "EXPLICIT_DATA"
            else:
                status, reason, windows = "UNKNOWN", "RULE_COVERAGE_INCOMPLETE", ()
        return CalendarResult(
            status, reason, self._ref, venue, kind, product, contract, trading_day, windows, sources
        )
