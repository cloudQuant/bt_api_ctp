"""Explicit local simulation adapters over one caller-supplied calendar view.

These ports do not authenticate sources, identities, clocks or owner closure and
must not be registered as production calendar, broker or gateway providers.
The caller asserts the closure, including the absence of outside owners; the
adapter cannot verify that assertion from the simulated snapshot.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from threading import RLock
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from bt_api_base.futures_expected_calendar_protocol import FuturesExpectedCalendarError
from bt_api_base.trading_session_protocol import (
    TradingPhase,
    TradingSessionState,
    TradingWindow,
    TradingWindowKind,
)

_VENUES = frozenset({"SHFE", "INE", "CZCE", "DCE", "CFFEX", "GFEX"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ZONE = ZoneInfo("Asia/Shanghai")
_MAX_OWNERS = 800
_MAX_MAPPINGS = 128
_MAX_WINDOWS_PER_DAY = 128
_MAX_CACHED_WINDOWS = 16384
_MAX_CACHED_SCOPES = 8
_MAX_CACHED_YEARS = 12
_MAX_CACHED_DAYS = 4800
_PREPARED_DEFAULT_WINDOWS = 131072
_PREPARED_MAX_WINDOWS = 262144


class SimulationCalendarUnavailable(FuturesExpectedCalendarError):
    """The requested simulation declaration cannot be completely determined."""

    _REASONS = frozenset(
        {
            "SIMULATION_BINDING_CHANGED",
            "SIMULATION_BINDING_UNAVAILABLE",
            "SIMULATION_COMPOSITION_UNAVAILABLE",
            "SIMULATION_COVERAGE_UNAVAILABLE",
            "SIMULATION_DAY_UNAVAILABLE",
            "SIMULATION_HORIZON_UNAVAILABLE",
            "SIMULATION_INSTANT_UNAVAILABLE",
            "SIMULATION_INSTRUMENT_UNAVAILABLE",
            "SIMULATION_MAPPING_UNAVAILABLE",
            "SIMULATION_MODE_REQUIRED",
            "SIMULATION_OWNER_CLOSURE_UNAVAILABLE",
            "SIMULATION_OWNER_UNAVAILABLE",
            "SIMULATION_RESEARCH_BUILD_UNAVAILABLE",
            "SIMULATION_WINDOWS_UNAVAILABLE",
            "SIMULATION_WINDOW_LIMIT",
            "SIMULATION_WINDOW_OVERLAP",
            "SIMULATION_WINDOW_SOURCE_UNAVAILABLE",
            "SIMULATION_WINDOW_UNAVAILABLE",
            "SIMULATION_YEAR_UNAVAILABLE",
            "SIMULATION_PREPARED_SCOPE_UNAVAILABLE",
            "SIMULATION_PREPARED_BUDGET_UNAVAILABLE",
        }
    )

    def __init__(self, reason: str) -> None:
        self.reason = (
            reason if type(reason) is str and reason in self._REASONS else "SIMULATION_UNAVAILABLE"
        )
        super().__init__("FUTURES_EXPECTED_CALENDAR_PROVIDER_UNAVAILABLE")


class SimulationReadView(Protocol):
    def status(self) -> Any: ...
    def query(
        self, *, venue: str, kind: str, product: str, contract: str, trading_day: date
    ) -> Any: ...
    def query_venue_day(self, *, mode: str, venue: str, trading_day: date) -> Any: ...


def _text(value: Any) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _instant(value: Any) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise SimulationCalendarUnavailable("SIMULATION_INSTANT_UNAVAILABLE")
    return value.astimezone(UTC)


def _identity(status: Any) -> tuple:
    names = (
        "mode",
        "route",
        "source_generation",
        "calendar_ref",
        "publication_id",
        "content_sha256",
        "coverage_manifest_sha256",
        "binding_sha256",
        "version_sha256",
        "bound_trading_day",
        "last_sequence",
    )
    values = tuple(getattr(status, name, None) for name in names)
    if (
        type(values[0]) is not str
        or values[0] != "SIMULATION_ONLY"
        or any(not _text(values[i]) for i in (1, 2, 3, 4))
        or any(
            type(values[i]) is not str or _HASH.fullmatch(values[i]) is None for i in (5, 6, 7, 8)
        )
        or type(values[9]) is not date
        or type(values[10]) is not int
        or values[10] < 0
        or getattr(status, "coverage_status", None) != "DECLARED_UNVERIFIED"
        or getattr(status, "load_error", None) is not None
    ):
        raise SimulationCalendarUnavailable("SIMULATION_BINDING_UNAVAILABLE")
    rows = getattr(status, "declared_coverage", None)
    origin = getattr(status, "data_origin", None)
    if type(rows) is not tuple or not 1 <= len(rows) <= 64 or not _text(origin):
        raise SimulationCalendarUnavailable("SIMULATION_BINDING_UNAVAILABLE")
    coverage = []
    for row in rows:
        venue = getattr(row, "venue", None)
        start = getattr(row, "start_day", None)
        end = getattr(row, "end_day", None)
        if not _text(venue) or type(start) is not date or type(end) is not date or end < start:
            raise SimulationCalendarUnavailable("SIMULATION_BINDING_UNAVAILABLE")
        coverage.append((venue, start, end))
    if len({row[0] for row in coverage}) != len(coverage):
        raise SimulationCalendarUnavailable("SIMULATION_BINDING_UNAVAILABLE")
    return (*values, tuple(coverage), origin)


@dataclass(frozen=True, slots=True)
class SimulationIdentity:
    mode: str
    route: str
    source_generation: str
    calendar_ref: str
    publication_id: str
    content_sha256: str
    coverage_manifest_sha256: str
    binding_sha256: str
    version_sha256: str
    bound_trading_day: date
    last_sequence: int
    declared_coverage: tuple[tuple[str, date, date], ...]
    data_origin: str
    source_authentication: str = "NOT_PROVEN"
    identity_binding: str = "CALLER_DECLARED_NOT_PROVEN"
    owner_closure: str = "CALLER_DECLARED_NOT_PROVEN"
    trade_authorization: str = "NONE"
    window_time_sources: str = "UNVERIFIED_AKSHARE_OR_MOCK_DECLARATIONS"
    window_phase_basis: str = "MOCK_ASSUMPTION_OR_MOCK_DECLARATION"


@dataclass(frozen=True, slots=True)
class SimulationMapping:
    instrument_id: str
    venue: str
    kind: str
    product: str
    mock_contract: str


@dataclass(frozen=True, slots=True)
class SimulationOwnerClosure:
    """Caller declaration of complete owners for the horizon, never a fact."""

    version_sha256: str
    start_utc: datetime
    end_utc: datetime
    owner_start: date
    owner_end: date
    max_advance_days: int
    max_spill_days: int


@dataclass(frozen=True, slots=True)
class SimulationCalendarPorts:
    identity: SimulationIdentity
    mappings: tuple[SimulationMapping, ...]
    closure: SimulationOwnerClosure
    expected_days: Any
    schedule: Any


@dataclass(frozen=True, slots=True)
class SimulationPreparedLimits:
    required_instruments: tuple[str, ...]
    required_years: tuple[tuple[str, int], ...]
    max_instruments: int
    max_years: int
    max_owner_days: int
    max_windows_per_owner: int
    max_cached_windows: int
    hard_max_cached_windows: int
    cached_windows: int


@dataclass(frozen=True, slots=True)
class SimulationPreparedCalendarPorts:
    """Eagerly validated simulation scopes, not authenticated venue facts."""

    identity: SimulationIdentity
    mappings: tuple[SimulationMapping, ...]
    closure: SimulationOwnerClosure
    expected_days: Any
    schedule: Any
    limits: SimulationPreparedLimits


@dataclass(frozen=True, slots=True)
class _DayDeclaration:
    day_status: str


def _coverage(status: Any, venue: str, start: date, end: date) -> None:
    rows = getattr(status, "declared_coverage", None)
    if type(rows) is not tuple:
        raise SimulationCalendarUnavailable("SIMULATION_COVERAGE_UNAVAILABLE")
    matches = [r for r in rows if getattr(r, "venue", None) == venue]
    if (
        len(matches) != 1
        or type(getattr(matches[0], "start_day", None)) is not date
        or type(getattr(matches[0], "end_day", None)) is not date
        or not matches[0].start_day <= start <= end <= matches[0].end_day
    ):
        raise SimulationCalendarUnavailable("SIMULATION_COVERAGE_UNAVAILABLE")


def _sources(value: Any) -> bool:
    return (
        type(value) is tuple
        and bool(value)
        and len(set(value)) == len(value)
        and all(_text(x) for x in value)
    )


class _Shared:
    def __init__(self, view: SimulationReadView, identity: SimulationIdentity):
        self.view = view
        self.identity = identity
        self.key = tuple(
            getattr(identity, name)
            for name in (
                "mode",
                "route",
                "source_generation",
                "calendar_ref",
                "publication_id",
                "content_sha256",
                "coverage_manifest_sha256",
                "binding_sha256",
                "version_sha256",
                "bound_trading_day",
                "last_sequence",
                "declared_coverage",
                "data_origin",
            )
        )
        self.lock = RLock()
        self.days: OrderedDict[tuple[str, date], _DayDeclaration] = OrderedDict()

    def check(self, status: Any) -> None:
        if _identity(status) != self.key or _identity(self.view.status()) != self.key:
            raise SimulationCalendarUnavailable("SIMULATION_BINDING_CHANGED")

    def day(self, venue: str, day: date) -> Any:
        try:
            self.check(self.view.status())
            key = (venue, day)
            if key in self.days:
                self.days.move_to_end(key)
                return self.days[key]
            row = self.view.query_venue_day(mode="SIMULATION_ONLY", venue=venue, trading_day=day)
            self.check(row.binding)
            if (
                row.status != "COMPLETE"
                or row.mode != "SIMULATION_ONLY"
                or row.venue != venue
                or type(row.trading_day) is not date
                or row.trading_day != day
                or row.day_status not in {"open", "closed"}
                or not _sources(row.source_ids)
                or row.source_authentication != "NOT_PROVEN"
                or row.trade_authorization != "NONE"
            ):
                raise SimulationCalendarUnavailable("SIMULATION_DAY_UNAVAILABLE")
            value = _DayDeclaration(row.day_status)
            self.days[key] = value
            if len(self.days) > _MAX_CACHED_DAYS:
                self.days.popitem(last=False)
            return value
        except SimulationCalendarUnavailable:
            raise
        except Exception:
            raise SimulationCalendarUnavailable("SIMULATION_DAY_UNAVAILABLE") from None


class _ExpectedDays:
    def __init__(self, shared: _Shared, *, allowed: frozenset[tuple[str, int]] | None = None):
        self._shared = shared
        self._allowed = allowed
        self._cache: OrderedDict[tuple[str, int], tuple[date, ...]] = OrderedDict()

    def build(self, request: Any) -> Any:
        raise SimulationCalendarUnavailable("SIMULATION_RESEARCH_BUILD_UNAVAILABLE")

    def validate(self, snapshot: Any) -> bool:
        return False

    def expected_trading_days(self, venue: str, year: int) -> tuple[date, ...]:
        if (
            type(venue) is not str
            or venue not in _VENUES
            or type(year) is not int
            or not 1 <= year <= 9999
        ):
            raise SimulationCalendarUnavailable("SIMULATION_YEAR_UNAVAILABLE")
        start, end = date(year, 1, 1), date(year, 12, 31)
        if self._allowed is not None and (venue, year) not in self._allowed:
            raise SimulationCalendarUnavailable("SIMULATION_PREPARED_SCOPE_UNAVAILABLE")
        with self._shared.lock:
            self._shared.check(self._shared.view.status())
            key = (venue, year)
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            _coverage(self._shared.view.status(), venue, start, end)
            days = []
            for offset in range((end - start).days + 1):
                day = start + timedelta(days=offset)
                if self._shared.day(venue, day).day_status == "open":
                    days.append(day)
            self._shared.check(self._shared.view.status())
            result = tuple(days)
            self._cache[key] = result
            if len(self._cache) > _MAX_CACHED_YEARS:
                self._cache.popitem(last=False)
            return result


def _window_sources(query: Any, result: Any, mapping: SimulationMapping) -> None:
    attribution = query.simulation_provenance
    if attribution is None:
        attribution = query.declared_mock_provenance
    if not result.windows:
        return
    if type(attribution) is not dict or attribution.get("environment") != "SIMULATION_ONLY":
        raise SimulationCalendarUnavailable("SIMULATION_WINDOW_SOURCE_UNAVAILABLE")
    schema = attribution.get("schema")
    rows = attribution.get("windows")
    if type(rows) is not list or len(rows) != len(result.windows):
        raise SimulationCalendarUnavailable("SIMULATION_WINDOW_SOURCE_UNAVAILABLE")
    ids = [getattr(w, "window_id", None) for w in result.windows]
    if (
        any(not _text(x) for x in ids)
        or len(set(ids)) != len(ids)
        or any(type(r) is not dict or not _text(r.get("window_id")) for r in rows)
        or len({r["window_id"] for r in rows}) != len(rows)
        or {r["window_id"] for r in rows} != set(ids)
    ):
        raise SimulationCalendarUnavailable("SIMULATION_WINDOW_SOURCE_UNAVAILABLE")
    if schema == "bt_api.calendar-mixed-window-provenance/2":
        target = attribution.get("target")
        if (
            type(target) is not dict
            or tuple(target.get(k) for k in ("venue", "kind", "product", "mock_contract"))
            != (mapping.venue, mapping.kind, mapping.product, mapping.mock_contract)
            or type(target.get("valid_from")) is not str
            or type(target.get("valid_to")) is not str
            or not target["valid_from"] <= result.trading_day.isoformat() <= target["valid_to"]
        ):
            raise SimulationCalendarUnavailable("SIMULATION_WINDOW_SOURCE_UNAVAILABLE")
        locator = attribution.get("evidence_locator")
        if (
            type(locator) is not dict
            or any(
                not _text(locator.get(k))
                for k in (
                    "candidate_id",
                    "parent_candidate_id",
                    "observation_id",
                    "observation_run_id",
                    "capture_id",
                )
            )
            or type(locator.get("response_digest")) is not str
            or _HASH.fullmatch(locator["response_digest"]) is None
        ):
            raise SimulationCalendarUnavailable("SIMULATION_WINDOW_SOURCE_UNAVAILABLE")
        for row in rows:
            source = row.get("source_type")
            expected = {
                "AKSHARE": "AKSHARE.simulation_unverified",
                "MOCK": "MOCK.simulation_only",
            }.get(source)
            window = next(w for w in result.windows if w.window_id == row["window_id"])
            local_start, local_end = (
                window.start_utc.astimezone(_ZONE),
                window.end_utc.astimezone(_ZONE),
            )
            offset = (local_end.date() - local_start.date()).days
            basis = "AKSHARE_SYNTAX_CANDIDATE" if source == "AKSHARE" else "MOCK_ASSUMPTION"
            transition = (
                ("EXPLICIT_NEXT_DAY" if offset == 1 else "SAME_CLOCK_DAY")
                if source == "AKSHARE"
                else offset
            )
            fields = row.get("fields")
            if (
                expected not in result.source_ids
                or row.get("scope") not in {"DAY", "NIGHT"}
                or local_start.second != 0
                or local_start.microsecond != 0
                or local_end.second != 0
                or local_end.microsecond != 0
                or offset not in {0, 1}
                or type(fields) is not dict
                or fields.get("scope") != {"value": row["scope"], "basis": basis}
                or fields.get("start") != {"value": local_start.strftime("%H:%M"), "basis": basis}
                or fields.get("end") != {"value": local_end.strftime("%H:%M"), "basis": basis}
                or fields.get("day_transition") != {"value": transition, "basis": basis}
                or type(row.get("simulation")) is not dict
                or row["simulation"].get("phase")
                != {"value": window.phase, "basis": "MOCK_ASSUMPTION"}
            ):
                raise SimulationCalendarUnavailable("SIMULATION_WINDOW_SOURCE_UNAVAILABLE")
    elif schema == "bt_api.calendar-declared-mock-windows/1":
        if any(
            row.get("source_type") != "MOCK"
            or row.get("source_id") != "MOCK.simulation_only"
            or row.get("source_authentication") != "NOT_PROVEN"
            or row.get("scope") not in {"DAY", "NIGHT"}
            for row in rows
        ):
            raise SimulationCalendarUnavailable("SIMULATION_WINDOW_SOURCE_UNAVAILABLE")
        if "MOCK.simulation_only" not in result.source_ids:
            raise SimulationCalendarUnavailable("SIMULATION_WINDOW_SOURCE_UNAVAILABLE")
    else:
        raise SimulationCalendarUnavailable("SIMULATION_WINDOW_SOURCE_UNAVAILABLE")


class _Schedule:
    def __init__(
        self,
        shared: _Shared,
        mappings: tuple[SimulationMapping, ...],
        closure: SimulationOwnerClosure,
        *,
        pinned: bool = False,
        max_windows: int = _MAX_CACHED_WINDOWS,
    ):
        self._shared = shared
        self._mappings = {m.instrument_id: m for m in mappings}
        self._closure = closure
        self._pinned = pinned
        self._max_windows = max_windows
        self._cache: OrderedDict[
            str,
            tuple[
                tuple[TradingWindow, ...],
                tuple[datetime, ...],
                tuple[TradingWindow, ...],
                tuple[datetime, ...],
            ],
        ] = OrderedDict()
        self._cached_count = 0

    def _get(self, instrument: str):
        if type(instrument) is not str or instrument not in self._mappings:
            raise SimulationCalendarUnavailable("SIMULATION_INSTRUMENT_UNAVAILABLE")
        shared, c = self._shared, self._closure
        with shared.lock:
            shared.check(shared.view.status())
            if instrument in self._cache:
                self._cache.move_to_end(instrument)
                return self._cache[instrument]
            mapping = self._mappings[instrument]
            _coverage(shared.view.status(), mapping.venue, c.owner_start, c.owner_end)
            windows = []
            for offset in range((c.owner_end - c.owner_start).days + 1):
                owner = c.owner_start + timedelta(days=offset)
                day = shared.day(mapping.venue, owner)
                try:
                    captured = shared.view.query(
                        venue=mapping.venue,
                        kind=mapping.kind,
                        product=mapping.product,
                        contract=mapping.mock_contract,
                        trading_day=owner,
                    )
                    shared.check(captured.binding)
                    result = captured.result
                    if (
                        result.status != "COMPLETE"
                        or result.calendar_ref != shared.identity.calendar_ref
                        or result.sources_verified is not False
                        or (
                            result.venue,
                            result.kind,
                            result.product,
                            result.contract,
                            result.trading_day,
                        )
                        != (
                            mapping.venue,
                            mapping.kind,
                            mapping.product,
                            mapping.mock_contract,
                            owner,
                        )
                        or not _sources(result.source_ids)
                        or type(result.windows) is not tuple
                        or len(result.windows) > _MAX_WINDOWS_PER_DAY
                        or (day.day_status == "closed" and result.windows)
                    ):
                        raise SimulationCalendarUnavailable("SIMULATION_OWNER_UNAVAILABLE")
                    _window_sources(captured, result, mapping)
                    for w in result.windows:
                        if (
                            w.trading_day != owner
                            or w.phase not in {"continuous", "auction"}
                            or not _text(w.window_id)
                            or type(w.start_utc) is not datetime
                            or type(w.end_utc) is not datetime
                            or w.start_utc.utcoffset() != timedelta(0)
                            or w.end_utc.utcoffset() != timedelta(0)
                            or w.start_utc >= w.end_utc
                        ):
                            raise SimulationCalendarUnavailable("SIMULATION_WINDOW_UNAVAILABLE")
                        local_start = w.start_utc.astimezone(_ZONE)
                        local_end = w.end_utc.astimezone(_ZONE)
                        earliest = datetime.combine(
                            owner - timedelta(days=c.max_advance_days), time.min, _ZONE
                        )
                        latest_start = datetime.combine(owner + timedelta(days=1), time.min, _ZONE)
                        latest_end = datetime.combine(
                            owner + timedelta(days=c.max_spill_days + 1), time.min, _ZONE
                        )
                        if not earliest <= local_start < latest_start or local_end > latest_end:
                            raise SimulationCalendarUnavailable(
                                "SIMULATION_OWNER_CLOSURE_UNAVAILABLE"
                            )
                        windows.append(
                            TradingWindow(
                                owner,
                                TradingWindowKind(w.phase),
                                w.start_utc,
                                w.end_utc,
                                w.window_id,
                            )
                        )
                        if len(windows) > self._max_windows or (
                            self._pinned and self._cached_count + len(windows) > self._max_windows
                        ):
                            raise SimulationCalendarUnavailable("SIMULATION_WINDOW_LIMIT")
                except SimulationCalendarUnavailable:
                    raise
                except Exception:
                    raise SimulationCalendarUnavailable("SIMULATION_OWNER_UNAVAILABLE") from None
            windows.sort(key=lambda w: w.start_utc)
            if any(a.end_utc > b.start_utc for a, b in zip(windows, windows[1:])):
                raise SimulationCalendarUnavailable("SIMULATION_WINDOW_OVERLAP")
            shared.check(shared.view.status())
            all_windows = tuple(windows)
            continuous = tuple(w for w in all_windows if w.kind is TradingWindowKind.CONTINUOUS)
            result = (
                all_windows,
                tuple(w.start_utc for w in all_windows),
                continuous,
                tuple(w.start_utc for w in continuous),
            )
            while (
                not self._pinned
                and self._cache
                and (
                    len(self._cache) >= _MAX_CACHED_SCOPES
                    or self._cached_count + len(windows) > _MAX_CACHED_WINDOWS
                )
            ):
                _, removed = self._cache.popitem(last=False)
                self._cached_count -= len(removed[0])
            self._cache[instrument] = result
            self._cached_count += len(windows)
            return result

    def windows(
        self, instrument_id: str, start_utc: datetime, end_utc: datetime
    ) -> tuple[TradingWindow, ...]:
        try:
            start, end = _instant(start_utc), _instant(end_utc)
            if not self._closure.start_utc <= start < end <= self._closure.end_utc:
                raise SimulationCalendarUnavailable("SIMULATION_HORIZON_UNAVAILABLE")
            windows, starts, _, _ = self._get(instrument_id)
            first = max(0, bisect_right(starts, start) - 1)
            last = bisect_left(starts, end)
            return tuple(w for w in windows[first:last] if w.end_utc > start)
        except SimulationCalendarUnavailable:
            raise
        except Exception:
            raise SimulationCalendarUnavailable("SIMULATION_WINDOWS_UNAVAILABLE") from None

    def session_state(self, instrument_id: str, at_utc: datetime) -> TradingSessionState:
        at = _instant(at_utc)
        ref = self._shared.identity.calendar_ref
        try:
            if not self._closure.start_utc <= at < self._closure.end_utc:
                raise SimulationCalendarUnavailable("SIMULATION_HORIZON_UNAVAILABLE")
            windows, starts, continuous, continuous_starts = self._get(instrument_id)
            index = bisect_right(starts, at) - 1
            current = windows[index] if index >= 0 and windows[index].contains(at) else None
            next_index = bisect_right(continuous_starts, at)
            following = continuous[next_index] if next_index < len(continuous) else None
            if following is not None and following.start_utc >= self._closure.end_utc:
                following = None
            if current is not None:
                phase = TradingPhase(current.kind.value)
                return TradingSessionState(
                    instrument_id, at, phase, current.trading_day, current, following, ref
                )
            if following is not None:
                return TradingSessionState(
                    instrument_id,
                    at,
                    TradingPhase.CLOSED,
                    following.trading_day,
                    None,
                    following,
                    ref,
                )
        except Exception:
            pass
        return TradingSessionState(
            instrument_id,
            at,
            TradingPhase.UNKNOWN,
            None,
            None,
            None,
            ref,
            reason="SIMULATION_SESSION_UNAVAILABLE",
        )


def create_simulation_calendar_ports(
    *,
    mode: str,
    view: SimulationReadView,
    mappings: tuple[SimulationMapping, ...],
    closure: SimulationOwnerClosure,
) -> SimulationCalendarPorts:
    """Compose both legacy-shaped ports around one explicitly declared simulated view."""
    try:
        if type(mode) is not str or mode != "SIMULATION_ONLY":
            raise SimulationCalendarUnavailable("SIMULATION_MODE_REQUIRED")
        identity = SimulationIdentity(*_identity(view.status()))
        if (
            type(mappings) is not tuple
            or not 1 <= len(mappings) <= _MAX_MAPPINGS
            or any(
                type(m) is not SimulationMapping
                or not _text(m.instrument_id)
                or m.venue not in _VENUES
                or m.kind not in {"future", "option"}
                or not _text(m.product)
                or not _text(m.mock_contract)
                or not m.mock_contract.startswith("mock_")
                for m in mappings
            )
            or len({m.instrument_id for m in mappings}) != len(mappings)
        ):
            raise SimulationCalendarUnavailable("SIMULATION_MAPPING_UNAVAILABLE")
        if (
            type(closure) is not SimulationOwnerClosure
            or closure.version_sha256 != identity.version_sha256
            or type(closure.owner_start) is not date
            or type(closure.owner_end) is not date
            or not closure.owner_start <= closure.owner_end
            or (closure.owner_end - closure.owner_start).days + 1 > _MAX_OWNERS
            or type(closure.max_advance_days) is not int
            or not 0 <= closure.max_advance_days <= 7
            or type(closure.max_spill_days) is not int
            or not 0 <= closure.max_spill_days <= 1
        ):
            raise SimulationCalendarUnavailable("SIMULATION_OWNER_CLOSURE_UNAVAILABLE")
        start, end = _instant(closure.start_utc), _instant(closure.end_utc)
        if start >= end:
            raise SimulationCalendarUnavailable("SIMULATION_OWNER_CLOSURE_UNAVAILABLE")
        earliest = start.astimezone(_ZONE).date() - timedelta(days=closure.max_spill_days)
        latest = (end - timedelta(microseconds=1)).astimezone(_ZONE).date() + timedelta(
            days=closure.max_advance_days
        )
        if not closure.owner_start <= earliest <= latest <= closure.owner_end:
            raise SimulationCalendarUnavailable("SIMULATION_OWNER_CLOSURE_UNAVAILABLE")
        closure = SimulationOwnerClosure(
            closure.version_sha256,
            start,
            end,
            closure.owner_start,
            closure.owner_end,
            closure.max_advance_days,
            closure.max_spill_days,
        )
        shared = _Shared(view, identity)
        shared.check(view.status())
        return SimulationCalendarPorts(
            identity, mappings, closure, _ExpectedDays(shared), _Schedule(shared, mappings, closure)
        )
    except SimulationCalendarUnavailable:
        raise
    except Exception:
        raise SimulationCalendarUnavailable("SIMULATION_COMPOSITION_UNAVAILABLE") from None


def create_prepared_simulation_calendar_ports(
    *,
    mode: str,
    view: SimulationReadView,
    mappings: tuple[SimulationMapping, ...],
    closure: SimulationOwnerClosure,
    required_instruments: tuple[str, ...],
    required_years: tuple[tuple[str, int], ...],
    max_cached_windows: int = _PREPARED_DEFAULT_WINDOWS,
) -> SimulationPreparedCalendarPorts:
    """Eagerly validate and pin only explicitly requested simulation scopes.

    Failure exposes no ports. The caller's owner closure and identities remain
    declarations, not proof of exchange facts or permission to trade.
    """
    try:
        if (
            type(max_cached_windows) is not int
            or not 1 <= max_cached_windows <= _PREPARED_MAX_WINDOWS
        ):
            raise SimulationCalendarUnavailable("SIMULATION_PREPARED_BUDGET_UNAVAILABLE")
        if (
            type(required_instruments) is not tuple
            or not 1 <= len(required_instruments) <= _MAX_MAPPINGS
            or any(not _text(item) for item in required_instruments)
            or len(set(required_instruments)) != len(required_instruments)
            or type(required_years) is not tuple
            or not 1 <= len(required_years) <= _MAX_CACHED_YEARS
            or any(
                type(row) is not tuple
                or len(row) != 2
                or type(row[0]) is not str
                or row[0] not in _VENUES
                or type(row[1]) is not int
                or not 1 <= row[1] <= 9999
                for row in required_years
            )
            or len(set(required_years)) != len(required_years)
            or type(mappings) is not tuple
            or any(
                type(item) is not SimulationMapping
                or any(
                    type(getattr(item, name)) is not str
                    for name in ("instrument_id", "venue", "kind", "product", "mock_contract")
                )
                for item in mappings
            )
            or type(closure) is not SimulationOwnerClosure
            or type(closure.version_sha256) is not str
        ):
            raise SimulationCalendarUnavailable("SIMULATION_PREPARED_SCOPE_UNAVAILABLE")
        # The legacy factory retains its normal construction and LRU behavior.
        # Its transient objects are private and never returned to callers here.
        baseline = create_simulation_calendar_ports(
            mode=mode, view=view, mappings=mappings, closure=closure
        )
        by_instrument = {item.instrument_id: item for item in baseline.mappings}
        if any(item not in by_instrument for item in required_instruments):
            raise SimulationCalendarUnavailable("SIMULATION_PREPARED_SCOPE_UNAVAILABLE")
        selected = tuple(
            SimulationMapping(
                by_instrument[name].instrument_id,
                by_instrument[name].venue,
                by_instrument[name].kind,
                by_instrument[name].product,
                by_instrument[name].mock_contract,
            )
            for name in required_instruments
        )
        years = tuple((venue, year) for venue, year in required_years)
        needed = frozenset(
            (item.venue, year)
            for item in selected
            for year in range(
                baseline.closure.owner_start.year, baseline.closure.owner_end.year + 1
            )
        )
        if not needed <= frozenset(years):
            raise SimulationCalendarUnavailable("SIMULATION_PREPARED_SCOPE_UNAVAILABLE")
        shared = _Shared(view, baseline.identity)
        dates = _ExpectedDays(shared, allowed=frozenset(years))
        schedule = _Schedule(
            shared, selected, baseline.closure, pinned=True, max_windows=max_cached_windows
        )
        for venue, year in years:
            dates.expected_trading_days(venue, year)
        for item in selected:
            schedule.windows(
                item.instrument_id, baseline.closure.start_utc, baseline.closure.end_utc
            )
        shared.check(view.status())
        limits = SimulationPreparedLimits(
            tuple(required_instruments),
            years,
            _MAX_MAPPINGS,
            _MAX_CACHED_YEARS,
            _MAX_OWNERS,
            _MAX_WINDOWS_PER_DAY,
            max_cached_windows,
            _PREPARED_MAX_WINDOWS,
            schedule._cached_count,
        )
        return SimulationPreparedCalendarPorts(
            baseline.identity, selected, baseline.closure, dates, schedule, limits
        )
    except SimulationCalendarUnavailable:
        raise
    except Exception:
        raise SimulationCalendarUnavailable("SIMULATION_COMPOSITION_UNAVAILABLE") from None
