"""Two-level CTP instrument catalog cache per CTP trading day (D62-11b/11c).

Option A of the iteration-62 plan (section 5.4): the first need of a trading
day for one source scope runs one full ``ReqQryInstrument``; later needs in
any process of the same machine reuse it.

Lookup order
------------
1. In-process memory for the same scope and the session's current CTP trading
   day.
2. The local shared file ``<cache_dir>/<scope>/<trading_day>.json``, accepted
   only when format/version, scope, trading day, the completeness flag and the
   content SHA-256 all match.
3. A full native query, issued only when the session's trading day is known.
   One builder per (scope, trading day) holds an exclusive lock file
   (``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows; the OS releases it
   when the holder dies).  Other starters wait for a bounded time and re-check
   the published file.  The builder publishes atomically (temporary file in
   the same directory, ``fsync``, ``os.replace``).  A failed or aborted build,
   a lock timeout, or a trading-day/scope/session change during the build is
   reported as :class:`InstrumentCatalogNotReady`; partial or previous-day
   data are never served.

An instrument missing from a complete catalog is queried on its own and merged
under the same lock (re-read the published file, merge, atomic replace), so
concurrent supplements do not lose each other's rows and never mix days.
Unknown instruments are not cached (an instrument may be listed intraday).

Trust boundary
--------------
Entries are reference metadata only (ids, product, price tick, multiplier,
dates and the ``IsTrading`` flag *as first observed that trading day*).  The
cache issues no query evidence, extends no lease and is not proof of
positions, quotes, current trading status or trading permission; it never
replaces a fresh query where evidence is required (D62-11b).  The content hash
detects truncation and corruption, not tampering: any process that can write
the cache directory can change it.  The scope key is a SHA-256 of (broker id,
Trader front, environment profile) - no credentials, user or investor ids.
That different accounts behind the same broker and front see the same catalog
is an assumption of this key, not something this module proves.

Cache directory: the ``cache_dir`` argument, else ``$BT_API_CTP_CATALOG_CACHE_DIR``,
else ``%LOCALAPPDATA%\\bt_api_ctp\\catalog`` (Windows) or
``${XDG_CACHE_HOME:-~/.cache}/bt_api_ctp/catalog``.  Files of past trading days
are not pruned automatically.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt as _msvcrt  # type: ignore[import-not-found]
except ImportError:  # POSIX
    _msvcrt = None  # type: ignore[assignment]

__all__ = (
    "CATALOG_CACHE_DIR_ENV",
    "CATALOG_FORMAT",
    "CATALOG_FORMAT_VERSION",
    "CatalogLookup",
    "CatalogReady",
    "CatalogSource",
    "CtpFeedCatalogSource",
    "InstrumentCatalogCache",
    "InstrumentCatalogEntry",
    "InstrumentCatalogNotReady",
    "NOT_READY_CODES",
    "default_catalog_cache_dir",
    "default_instrument_catalog_cache",
    "instrument_catalog_scope_key",
    "project_catalog_entry",
)

CATALOG_FORMAT = "bt_api_ctp.instrument_catalog"
CATALOG_FORMAT_VERSION = 1
CATALOG_CACHE_DIR_ENV = "BT_API_CTP_CATALOG_CACHE_DIR"
_SCOPE_DOMAIN = "bt_api_ctp.instrument_catalog.scope.v1"
_TRADING_DAY_RE = re.compile(r"^[0-9]{8}$")
_SCOPE_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_ENTRIES = 100_000
_COUNTER_NAMES = (
    "full_queries",
    "incremental_queries",
    "file_hits",
    "memory_hits",
    "file_rejects",
    "lock_waits",
    "lock_timeouts",
    "build_failures",
    "publish_failures",
    "file_level_unavailable",
    "supplements_unpublished",
)

NOT_READY_CODES = frozenset(
    {
        "ctp_catalog_scope_unknown",
        "ctp_catalog_trading_day_unknown",
        "ctp_catalog_session_unknown",
        "ctp_catalog_instrument_invalid",
        "ctp_catalog_full_query_failed",
        "ctp_catalog_full_query_incomplete",
        "ctp_catalog_full_query_empty",
        "ctp_catalog_query_failed",
        "ctp_catalog_query_incomplete",
        "ctp_catalog_session_changed",
        "ctp_catalog_lock_timeout",
        "ctp_catalog_unavailable",
    }
)


class InstrumentCatalogNotReady(RuntimeError):
    """The catalog for the current scope and trading day is not available."""

    def __init__(self, code: str) -> None:
        if code not in NOT_READY_CODES:
            code = "ctp_catalog_unavailable"
        self.code = code
        super().__init__(code)


# ---------------------------------------------------------------------------
# scope and paths
# ---------------------------------------------------------------------------
def instrument_catalog_scope_key(*, broker_id: str, td_front: str, env_profile: str) -> str:
    """Secret-free scope key: SHA-256 of broker id, Trader front and profile."""

    parts = []
    for value in (broker_id, td_front, env_profile):
        if type(value) is not str:
            raise TypeError("catalog scope parts must be strings")
        parts.append(value.strip())
    if not parts[0] or not parts[1]:
        raise ValueError("catalog scope needs a broker id and a Trader front")
    text = "\x1f".join((_SCOPE_DOMAIN, *parts))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_catalog_cache_dir() -> Path:
    """Cache directory from the environment, else the user cache directory."""

    override = os.environ.get(CATALOG_CACHE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "bt_api_ctp" / "catalog"
    base = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".cache"
    return root / "bt_api_ctp" / "catalog"


# ---------------------------------------------------------------------------
# entries
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InstrumentCatalogEntry:
    """Reference metadata of one instrument as listed by the native catalog.

    Not evidence of quotes, positions, current trading status or permission.
    ``None`` means the native row did not provide a usable value.
    """

    instrument_id: str
    exchange_id: str
    exchange_instrument_id: Optional[str] = None
    product_id: Optional[str] = None
    product_class: Optional[str] = None
    price_tick: Optional[float] = None
    volume_multiple: Optional[int] = None
    expire_date: Optional[str] = None
    is_trading: Optional[bool] = None
    life_phase: Optional[str] = None
    delivery_year: Optional[int] = None
    delivery_month: Optional[int] = None
    create_date: Optional[str] = None
    open_date: Optional[str] = None
    start_delivery_date: Optional[str] = None
    end_delivery_date: Optional[str] = None
    max_market_order_volume: Optional[int] = None
    min_market_order_volume: Optional[int] = None
    max_limit_order_volume: Optional[int] = None
    min_limit_order_volume: Optional[int] = None
    position_type: Optional[str] = None
    position_date_type: Optional[str] = None
    underlying_instrument_id: Optional[str] = None
    strike_price: Optional[float] = None
    options_type: Optional[str] = None
    underlying_multiple: Optional[float] = None
    combination_type: Optional[str] = None

    def __post_init__(self) -> None:
        for spec in fields(self):
            _check_value(spec.name, getattr(self, spec.name))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# field -> (native field, kind)
_ENTRY_SCHEMA: dict[str, tuple[str, str]] = {
    "instrument_id": ("InstrumentID", "id"),
    "exchange_id": ("ExchangeID", "id"),
    "exchange_instrument_id": ("ExchangeInstID", "text"),
    "product_id": ("ProductID", "text"),
    "product_class": ("ProductClass", "text"),
    "price_tick": ("PriceTick", "float"),
    "volume_multiple": ("VolumeMultiple", "int"),
    "expire_date": ("ExpireDate", "text"),
    "is_trading": ("IsTrading", "bool"),
    "life_phase": ("InstLifePhase", "text"),
    "delivery_year": ("DeliveryYear", "int"),
    "delivery_month": ("DeliveryMonth", "int"),
    "create_date": ("CreateDate", "text"),
    "open_date": ("OpenDate", "text"),
    "start_delivery_date": ("StartDelivDate", "text"),
    "end_delivery_date": ("EndDelivDate", "text"),
    "max_market_order_volume": ("MaxMarketOrderVolume", "int"),
    "min_market_order_volume": ("MinMarketOrderVolume", "int"),
    "max_limit_order_volume": ("MaxLimitOrderVolume", "int"),
    "min_limit_order_volume": ("MinLimitOrderVolume", "int"),
    "position_type": ("PositionType", "text"),
    "position_date_type": ("PositionDateType", "text"),
    "underlying_instrument_id": ("UnderlyingInstrID", "text"),
    "strike_price": ("StrikePrice", "float"),
    "options_type": ("OptionsType", "text"),
    "underlying_multiple": ("UnderlyingMultiple", "float"),
    "combination_type": ("CombinationType", "text"),
}
_ENTRY_FIELDS = tuple(spec.name for spec in fields(InstrumentCatalogEntry))
if set(_ENTRY_FIELDS) != set(_ENTRY_SCHEMA):  # pragma: no cover - module invariant
    raise RuntimeError("catalog entry schema out of sync")


def _check_value(name: str, value: Any) -> None:
    kind = _ENTRY_SCHEMA[name][1]
    if kind == "id":
        ok = type(value) is str and 0 < len(value) <= 80 and value == value.strip()
    elif value is None:
        ok = True
    elif kind == "text":
        ok = type(value) is str and 0 < len(value) <= 80
    elif kind == "float":
        ok = type(value) is float and math.isfinite(value) and value > 0
    elif kind == "int":
        ok = type(value) is int and value > 0
    else:  # bool
        ok = type(value) is bool
    if not ok:
        raise ValueError(f"invalid catalog entry field: {name}")


def _row_value(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def _native_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).replace("\x00", "").strip()
    # Never truncate: a shortened id would name a different instrument.
    return text if 0 < len(text) <= 80 else None


def _native_value(kind: str, value: Any) -> Any:
    if kind in ("id", "text"):
        return _native_text(value)
    if kind == "bool":
        if type(value) is bool:
            return value
        if type(value) is not int:
            return None
        return {0: False, 1: True}.get(value)
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # CTP reports DBL_MAX for unset prices.
    if not math.isfinite(number) or number <= 0 or number > 1e300:
        return None
    if kind == "int":
        return int(number) if number == int(number) else None
    return number


def project_catalog_entry(row: Any) -> Optional[InstrumentCatalogEntry]:
    """Project one native instrument row; ``None`` without an instrument/venue id."""

    values = {
        name: _native_value(kind, _row_value(row, native))
        for name, (native, kind) in _ENTRY_SCHEMA.items()
    }
    if not values["instrument_id"] or not values["exchange_id"]:
        return None
    return InstrumentCatalogEntry(**values)


def _entry_from_json(data: Any) -> InstrumentCatalogEntry:
    if not isinstance(data, dict) or set(data) != set(_ENTRY_FIELDS):
        raise ValueError("catalog entry keys")
    values = dict(data)
    for name, (_native, kind) in _ENTRY_SCHEMA.items():
        # JSON has one number type; integral floats such as 5.0 stay floats.
        if kind == "float" and type(values[name]) is int:
            values[name] = float(values[name])
    return InstrumentCatalogEntry(**values)


# ---------------------------------------------------------------------------
# in-memory catalog
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Catalog:
    scope: str
    trading_day: str
    by_instrument: Mapping[str, tuple[InstrumentCatalogEntry, ...]]
    conflicts: frozenset[str]
    skipped_rows: int
    built_at_utc: str

    @property
    def entry_count(self) -> int:
        return sum(len(entries) for entries in self.by_instrument.values())

    def entries(self) -> list[InstrumentCatalogEntry]:
        return [
            entry
            for instrument in sorted(self.by_instrument)
            for entry in sorted(self.by_instrument[instrument], key=lambda e: e.exchange_id)
        ]

    def merged(self, new_entries: Iterable[InstrumentCatalogEntry]) -> "_Catalog":
        by_instrument = dict(self.by_instrument)
        conflicts = set(self.conflicts)
        _merge_into(by_instrument, conflicts, new_entries)
        return _Catalog(
            scope=self.scope,
            trading_day=self.trading_day,
            by_instrument=by_instrument,
            conflicts=frozenset(conflicts),
            skipped_rows=self.skipped_rows,
            built_at_utc=self.built_at_utc,
        )


def _merge_into(
    by_instrument: dict[str, tuple[InstrumentCatalogEntry, ...]],
    conflicts: set[str],
    new_entries: Iterable[InstrumentCatalogEntry],
) -> None:
    for entry in new_entries:
        instrument = entry.instrument_id
        if instrument in conflicts:
            continue
        current = by_instrument.get(instrument, ())
        same_venue = [item for item in current if item.exchange_id == entry.exchange_id]
        if not same_venue:
            by_instrument[instrument] = current + (entry,)
        elif same_venue[0] != entry:
            # Two different rows for one instrument and venue: neither is
            # trusted for the rest of the trading day.
            by_instrument.pop(instrument, None)
            conflicts.add(instrument)


def _catalog_from_entries(
    scope: str,
    trading_day: str,
    entries: Iterable[InstrumentCatalogEntry],
    *,
    skipped_rows: int = 0,
    conflicts: Iterable[str] = (),
    built_at_utc: Optional[str] = None,
) -> _Catalog:
    by_instrument: dict[str, tuple[InstrumentCatalogEntry, ...]] = {}
    conflict_set = set(conflicts)
    _merge_into(by_instrument, conflict_set, entries)
    return _Catalog(
        scope=scope,
        trading_day=trading_day,
        by_instrument=by_instrument,
        conflicts=frozenset(conflict_set),
        skipped_rows=skipped_rows,
        built_at_utc=built_at_utc or _utc_now(),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _content_digest(scope: str, trading_day: str, conflicts: list[str], entries: list[dict]) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "complete": True,
                "conflicts": conflicts,
                "entries": entries,
                "scope": scope,
                "trading_day": trading_day,
            }
        )
    ).hexdigest()


def _encode_catalog(catalog: _Catalog) -> bytes:
    entries = [entry.as_dict() for entry in catalog.entries()]
    conflicts = sorted(catalog.conflicts)
    document = {
        "format": CATALOG_FORMAT,
        "version": CATALOG_FORMAT_VERSION,
        "scope": catalog.scope,
        "trading_day": catalog.trading_day,
        "complete": True,
        "built_at_utc": catalog.built_at_utc,
        "published_at_utc": _utc_now(),
        "skipped_rows": catalog.skipped_rows,
        "conflicts": conflicts,
        "entry_count": len(entries),
        "content_sha256": _content_digest(catalog.scope, catalog.trading_day, conflicts, entries),
        "entries": entries,
    }
    return _canonical(document)


def _decode_catalog(raw: bytes, scope: str, trading_day: str) -> _Catalog:
    """Strictly decode a published file; any mismatch raises ``ValueError``."""

    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("catalog document")
    if document.get("format") != CATALOG_FORMAT:
        raise ValueError("catalog format")
    version = document.get("version")
    if type(version) is not int or version != CATALOG_FORMAT_VERSION:
        raise ValueError("catalog version")
    if document.get("scope") != scope:
        raise ValueError("catalog scope")
    if document.get("trading_day") != trading_day:
        raise ValueError("catalog trading day")
    if document.get("complete") is not True:
        raise ValueError("catalog incomplete")
    entries = document.get("entries")
    conflicts = document.get("conflicts")
    count = document.get("entry_count")
    skipped = document.get("skipped_rows")
    if not isinstance(entries, list) or len(entries) > _MAX_ENTRIES:
        raise ValueError("catalog entries")
    if type(count) is not int or count != len(entries):
        raise ValueError("catalog entry count")
    if not isinstance(conflicts, list) or any(type(item) is not str for item in conflicts):
        raise ValueError("catalog conflicts")
    if type(skipped) is not int or skipped < 0:
        raise ValueError("catalog skipped rows")
    digest = _content_digest(scope, trading_day, conflicts, entries)
    if document.get("content_sha256") != digest:
        raise ValueError("catalog content digest")
    parsed = [_entry_from_json(item) for item in entries]
    if len({(entry.instrument_id, entry.exchange_id) for entry in parsed}) != len(parsed):
        raise ValueError("catalog duplicate entries")
    if set(conflicts) & {entry.instrument_id for entry in parsed}:
        raise ValueError("catalog conflicted entries")
    built = document.get("built_at_utc")
    return _catalog_from_entries(
        scope,
        trading_day,
        parsed,
        skipped_rows=skipped,
        conflicts=conflicts,
        built_at_utc=built if type(built) is str else None,
    )


# ---------------------------------------------------------------------------
# cross-process lock
# ---------------------------------------------------------------------------
class _FileLock:
    """Exclusive, non-blocking lock on one lock file; released on close/exit."""

    def __init__(self, path: Path) -> None:
        self._fd: Optional[int] = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        self._held = False

    def try_acquire(self) -> bool:
        fd = self._fd
        if fd is None:
            return False
        if self._held:
            return True
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            elif _msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - no OS lock primitive
                return False
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK):
                return False
            raise
        self._held = True
        return True

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if self._held:
                if _fcntl is not None:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
                elif _msvcrt is not None:  # pragma: no cover - Windows
                    os.lseek(fd, 0, os.SEEK_SET)
                    _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        finally:
            self._held = False
            os.close(fd)


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
class CatalogSource(Protocol):
    """What the cache needs from one live session (provider-supplied)."""

    def catalog_scope(self) -> Optional[str]:
        """Secret-free scope key (see :func:`instrument_catalog_scope_key`)."""

    def trading_day(self) -> Optional[str]:
        """The session's CTP trading day (``YYYYMMDD``), ``None`` if unknown."""

    def session_generation(self) -> Optional[int]:
        """Connection generation of the session, ``None`` if not logged in."""

    def query_instruments(
        self, *, instrument_id: str, timeout: float, max_duration: Optional[float]
    ) -> Any:
        """Run ``ReqQryInstrument`` (empty id = full catalog); a ``QueryResult``."""


def _clean_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


class CtpFeedCatalogSource:
    """:class:`CatalogSource` over one ``CtpRequestDataFuture`` session."""

    def __init__(self, feed: Any) -> None:
        self._feed = feed

    def _state(self) -> Optional[dict[str, Any]]:
        trader = getattr(self._feed, "trader_client", None)
        if trader is None:
            return None
        try:
            state = trader.get_session_state()
        except Exception:
            return None
        return state if isinstance(state, dict) else None

    def catalog_scope(self) -> Optional[str]:
        feed = self._feed
        trader = getattr(feed, "trader_client", None)
        broker = _clean_text(getattr(trader, "_bound_broker_id", None)) or _clean_text(
            getattr(feed, "broker_id", None)
        )
        front = _clean_text(getattr(trader, "_bound_front", None)) or _clean_text(
            getattr(feed, "td_front", None)
        )
        profile = _clean_text(getattr(feed, "ctp_env_profile", None))
        if not broker or not front:
            return None
        return instrument_catalog_scope_key(broker_id=broker, td_front=front, env_profile=profile)

    def trading_day(self) -> Optional[str]:
        state = self._state()
        value = _clean_text(state.get("trading_day")) if state else ""
        return value or None

    def session_generation(self) -> Optional[int]:
        state = self._state()
        value = state.get("connection_generation") if state else None
        return value if type(value) is int else None

    def query_instruments(
        self, *, instrument_id: str, timeout: float, max_duration: Optional[float]
    ) -> Any:
        kwargs: dict[str, Any] = {"instrument_id": instrument_id, "timeout": timeout}
        if max_duration is not None:
            kwargs["max_duration"] = max_duration
        return self._feed.query_instruments_result(**kwargs)


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CatalogLookup:
    """Catalog entries of one instrument for the current scope and trading day.

    ``origin`` is where the catalog came from (``memory``, ``file``,
    ``full_query``) or ``supplement`` when the instrument itself was queried.
    ``native_queries`` counts native requests issued by this lookup.
    """

    instrument_id: str
    scope: str
    trading_day: str
    entries: tuple[InstrumentCatalogEntry, ...]
    origin: str
    native_queries: int
    conflicted: bool = False

    @property
    def venue(self) -> Optional[str]:
        """The unique ``ExchangeID`` of the instrument, else ``None``."""
        if self.conflicted:
            return None
        venues = {entry.exchange_id for entry in self.entries}
        return next(iter(venues)) if len(venues) == 1 else None


@dataclass(frozen=True)
class CatalogReady:
    scope: str
    trading_day: str
    entry_count: int
    origin: str
    native_queries: int


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
class InstrumentCatalogCache:
    """Process-level (level 1) and shared-file (level 2) catalog cache."""

    def __init__(
        self,
        cache_dir: Any = None,
        *,
        lock_timeout: float = 100.0,
        lock_poll_interval: float = 0.05,
        full_query_idle_timeout: float = 10.0,
        full_query_max_duration: float = 90.0,
        instrument_query_timeout: float = 10.0,
    ) -> None:
        for name, value in (
            ("lock_timeout", lock_timeout),
            ("lock_poll_interval", lock_poll_interval),
            ("full_query_idle_timeout", full_query_idle_timeout),
            ("full_query_max_duration", full_query_max_duration),
            ("instrument_query_timeout", instrument_query_timeout),
        ):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        self._dir = Path(cache_dir).expanduser() if cache_dir else default_catalog_cache_dir()
        self._lock_timeout = float(lock_timeout)
        self._lock_poll = float(lock_poll_interval)
        self._full_idle = float(full_query_idle_timeout)
        self._full_max = float(full_query_max_duration)
        self._single_timeout = float(instrument_query_timeout)
        self._lock = threading.Lock()
        self._memory: dict[tuple[str, str], _Catalog] = {}
        self._build_locks: dict[tuple[str, str], threading.Lock] = {}
        self._counters = dict.fromkeys(_COUNTER_NAMES, 0)

    # -- public ------------------------------------------------------------
    @property
    def cache_dir(self) -> Path:
        return self._dir

    def catalog_path(self, scope: str, trading_day: str) -> Path:
        if not _SCOPE_RE.fullmatch(scope) or not _TRADING_DAY_RE.fullmatch(trading_day):
            raise ValueError("invalid catalog scope or trading day")
        return self._dir / scope / f"{trading_day}.json"

    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def ensure_ready(self, source: CatalogSource) -> CatalogReady:
        """Make the current day's catalog available (memory, file or query)."""

        scope, day, generation = self._session(source)
        catalog, origin = self._ready_catalog(source, scope, day, generation)
        return CatalogReady(
            scope=scope,
            trading_day=day,
            entry_count=catalog.entry_count,
            origin=origin,
            native_queries=1 if origin == "full_query" else 0,
        )

    def lookup(self, source: CatalogSource, instrument_id: str) -> CatalogLookup:
        """Entries for ``instrument_id``; queries it on demand when missing."""

        if (
            type(instrument_id) is not str
            or not instrument_id
            or instrument_id != instrument_id.strip()
            or len(instrument_id) > 80
        ):
            raise InstrumentCatalogNotReady("ctp_catalog_instrument_invalid")
        scope, day, generation = self._session(source)
        catalog, origin = self._ready_catalog(source, scope, day, generation)
        queries = 1 if origin == "full_query" else 0
        if instrument_id in catalog.conflicts:
            return CatalogLookup(instrument_id, scope, day, (), origin, queries, conflicted=True)
        entries = catalog.by_instrument.get(instrument_id)
        if entries:
            return CatalogLookup(instrument_id, scope, day, tuple(entries), origin, queries)
        catalog = self._supplement(source, scope, day, generation, catalog, instrument_id)
        queries += 1
        if instrument_id in catalog.conflicts:
            return CatalogLookup(
                instrument_id, scope, day, (), "supplement", queries, conflicted=True
            )
        entries = catalog.by_instrument.get(instrument_id, ())
        return CatalogLookup(instrument_id, scope, day, tuple(entries), "supplement", queries)

    # -- internals ---------------------------------------------------------
    def _count(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    @staticmethod
    def _session(source: CatalogSource) -> tuple[str, str, int]:
        try:
            scope = source.catalog_scope()
        except Exception:
            scope = None
        if type(scope) is not str or not _SCOPE_RE.fullmatch(scope):
            raise InstrumentCatalogNotReady("ctp_catalog_scope_unknown")
        try:
            day = source.trading_day()
        except Exception:
            day = None
        if type(day) is not str or not _TRADING_DAY_RE.fullmatch(day):
            raise InstrumentCatalogNotReady("ctp_catalog_trading_day_unknown")
        try:
            generation = source.session_generation()
        except Exception:
            generation = None
        if type(generation) is not int:
            raise InstrumentCatalogNotReady("ctp_catalog_session_unknown")
        return scope, day, generation

    def _require_unchanged(
        self, source: CatalogSource, scope: str, day: str, generation: int
    ) -> None:
        try:
            current = self._session(source)
        except InstrumentCatalogNotReady:
            raise InstrumentCatalogNotReady("ctp_catalog_session_changed") from None
        if current != (scope, day, generation):
            raise InstrumentCatalogNotReady("ctp_catalog_session_changed")

    @staticmethod
    def _verify_result(
        result: Any, day: str, generation: int, *, failed: str, incomplete: str
    ) -> None:
        if result is None:
            raise InstrumentCatalogNotReady(failed)
        ok = (
            getattr(result, "complete", False) is True
            and getattr(result, "timed_out", False) is not True
            and getattr(result, "error_code", None) in (None, 0)
            and getattr(result, "record_limit_exceeded", False) is not True
            and getattr(result, "connection_generation", None) == generation
        )
        if not ok:
            raise InstrumentCatalogNotReady(incomplete)
        source = getattr(result, "query_source", None)
        if source is not None:
            if getattr(source, "connection_generation", generation) != generation:
                raise InstrumentCatalogNotReady("ctp_catalog_session_changed")
            source_day = getattr(source, "trading_day", None)
            if source_day not in (None, "") and source_day != day:
                raise InstrumentCatalogNotReady("ctp_catalog_session_changed")

    def _memory_get(self, scope: str, day: str) -> Optional[_Catalog]:
        with self._lock:
            return self._memory.get((scope, day))

    def _install(self, catalog: _Catalog) -> None:
        with self._lock:
            for key in [key for key in self._memory if key[0] == catalog.scope]:
                if key[1] != catalog.trading_day:
                    del self._memory[key]
            self._memory[(catalog.scope, catalog.trading_day)] = catalog

    def _build_lock(self, scope: str, day: str) -> threading.Lock:
        with self._lock:
            lock = self._build_locks.get((scope, day))
            if lock is None:
                for key in [key for key in self._build_locks if key[0] == scope]:
                    if key[1] != day and not self._build_locks[key].locked():
                        del self._build_locks[key]
                lock = self._build_locks[(scope, day)] = threading.Lock()
            return lock

    def _read_file(self, scope: str, day: str, *, count_reject: bool = True) -> Optional[_Catalog]:
        path = self.catalog_path(scope, day)
        try:
            with open(path, "rb") as handle:
                raw = handle.read(_MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError:
            if count_reject:
                self._count("file_rejects")
            return None
        try:
            if len(raw) > _MAX_FILE_BYTES:
                raise ValueError("catalog file too large")
            return _decode_catalog(raw, scope, day)
        except (ValueError, TypeError, UnicodeDecodeError):
            if count_reject:
                self._count("file_rejects")
            return None

    def _open_file_lock(self, scope: str, day: str) -> _FileLock:
        directory = self._dir / scope
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return _FileLock(directory / f"{day}.lock")

    def _acquire(self, lock: _FileLock, scope: str, day: str, *, accept_published: bool) -> Optional[_Catalog]:
        """Hold ``lock`` (returns ``None``) or return a catalog published meanwhile."""

        deadline = time.monotonic() + self._lock_timeout
        waited = False
        while True:
            if lock.try_acquire():
                if waited:
                    self._count("lock_waits")
                return None
            waited = True
            if accept_published:
                published = self._read_file(scope, day, count_reject=False)
                if published is not None:
                    self._count("lock_waits")
                    return published
            if time.monotonic() >= deadline:
                self._count("lock_timeouts")
                raise InstrumentCatalogNotReady("ctp_catalog_lock_timeout")
            time.sleep(self._lock_poll)

    def _publish(self, catalog: _Catalog) -> bool:
        path = self.catalog_path(catalog.scope, catalog.trading_day)
        temp: Optional[str] = None
        try:
            payload = _encode_catalog(catalog)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temp = tempfile.mkstemp(
                prefix=f".{catalog.trading_day}.", suffix=".tmp", dir=str(path.parent)
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            temp = None
            _fsync_directory(path.parent)
            return True
        except (OSError, ValueError):
            self._count("publish_failures")
            return False
        finally:
            if temp is not None:
                try:
                    os.unlink(temp)
                except OSError:
                    pass

    def _remove_stale_temps(self, scope: str, day: str) -> None:
        """Remove temp files of ``day`` left by dead builders (lock is held)."""
        directory = self._dir / scope
        try:
            names = os.listdir(directory)
        except OSError:
            return
        for name in names:
            if name.startswith(f".{day}.") and name.endswith(".tmp"):
                try:
                    os.unlink(directory / name)
                except OSError:
                    pass

    def _build_full(self, source: CatalogSource, scope: str, day: str, generation: int) -> _Catalog:
        self._count("full_queries")
        try:
            try:
                result = source.query_instruments(
                    instrument_id="", timeout=self._full_idle, max_duration=self._full_max
                )
            except Exception:
                raise InstrumentCatalogNotReady("ctp_catalog_full_query_failed") from None
            self._verify_result(
                result,
                day,
                generation,
                failed="ctp_catalog_full_query_failed",
                incomplete="ctp_catalog_full_query_incomplete",
            )
            self._require_unchanged(source, scope, day, generation)
            entries, skipped = [], 0
            for row in getattr(result, "records", ()) or ():
                entry = project_catalog_entry(row)
                if entry is None:
                    skipped += 1
                else:
                    entries.append(entry)
            if not entries:
                raise InstrumentCatalogNotReady("ctp_catalog_full_query_empty")
            return _catalog_from_entries(scope, day, entries, skipped_rows=skipped)
        except InstrumentCatalogNotReady:
            self._count("build_failures")
            raise

    def _ready_catalog(
        self, source: CatalogSource, scope: str, day: str, generation: int
    ) -> tuple[_Catalog, str]:
        catalog = self._memory_get(scope, day)
        if catalog is not None:
            self._count("memory_hits")
            return catalog, "memory"
        build_lock = self._build_lock(scope, day)
        if not build_lock.acquire(timeout=self._lock_timeout):
            self._count("lock_timeouts")
            raise InstrumentCatalogNotReady("ctp_catalog_lock_timeout")
        try:
            catalog = self._memory_get(scope, day)
            if catalog is not None:
                self._count("memory_hits")
                return catalog, "memory"
            catalog = self._read_file(scope, day)
            if catalog is not None:
                self._install(catalog)
                self._count("file_hits")
                return catalog, "file"
            try:
                file_lock = self._open_file_lock(scope, day)
            except OSError:
                # No shared level (e.g. read-only cache directory): build for
                # this process only, without cross-process coordination.
                self._count("file_level_unavailable")
                catalog = self._build_full(source, scope, day, generation)
                self._install(catalog)
                return catalog, "full_query"
            try:
                try:
                    published = self._acquire(file_lock, scope, day, accept_published=True)
                except OSError:
                    # The OS refused the lock primitive (not contention):
                    # build for this process only, nothing is published.
                    self._count("file_level_unavailable")
                    catalog = self._build_full(source, scope, day, generation)
                    self._install(catalog)
                    return catalog, "full_query"
                if published is None:
                    # Lock held: someone may have published just before.
                    published = self._read_file(scope, day, count_reject=False)
                if published is not None:
                    self._install(published)
                    self._count("file_hits")
                    return published, "file"
                self._remove_stale_temps(scope, day)
                catalog = self._build_full(source, scope, day, generation)
                self._publish(catalog)
                self._install(catalog)
                return catalog, "full_query"
            finally:
                file_lock.close()
        finally:
            build_lock.release()

    def _supplement(
        self,
        source: CatalogSource,
        scope: str,
        day: str,
        generation: int,
        catalog: _Catalog,
        instrument_id: str,
    ) -> _Catalog:
        self._count("incremental_queries")
        try:
            result = source.query_instruments(
                instrument_id=instrument_id, timeout=self._single_timeout, max_duration=None
            )
        except Exception:
            raise InstrumentCatalogNotReady("ctp_catalog_query_failed") from None
        self._verify_result(
            result,
            day,
            generation,
            failed="ctp_catalog_query_failed",
            incomplete="ctp_catalog_query_incomplete",
        )
        self._require_unchanged(source, scope, day, generation)
        new_entries = []
        for row in getattr(result, "records", ()) or ():
            entry = project_catalog_entry(row)
            # A venue ignoring the filter must not add other instruments.
            if entry is not None and entry.instrument_id == instrument_id:
                new_entries.append(entry)
        if not new_entries:
            return catalog
        build_lock = self._build_lock(scope, day)
        if not build_lock.acquire(timeout=self._lock_timeout):
            return self._merge_unpublished(catalog, new_entries)
        try:
            try:
                file_lock = self._open_file_lock(scope, day)
            except OSError:
                self._count("file_level_unavailable")
                return self._merge_unpublished(catalog, new_entries)
            try:
                try:
                    self._acquire(file_lock, scope, day, accept_published=False)
                except (InstrumentCatalogNotReady, OSError):
                    return self._merge_unpublished(catalog, new_entries)
                # Merge into the latest published file (other processes may
                # have added rows) so that no update is lost.
                base = self._read_file(scope, day) or self._memory_get(scope, day) or catalog
                if base.scope != scope or base.trading_day != day:
                    raise InstrumentCatalogNotReady("ctp_catalog_session_changed")
                merged = base.merged(new_entries)
                self._publish(merged)
                self._install(merged)
                return merged
            finally:
                file_lock.close()
        finally:
            build_lock.release()

    def _merge_unpublished(
        self, catalog: _Catalog, new_entries: list[InstrumentCatalogEntry]
    ) -> _Catalog:
        self._count("supplements_unpublished")
        base = self._memory_get(catalog.scope, catalog.trading_day) or catalog
        merged = base.merged(new_entries)
        self._install(merged)
        return merged


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":  # pragma: no cover - directories cannot be fsynced
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


_DEFAULT_CACHES: dict[str, InstrumentCatalogCache] = {}
_DEFAULT_CACHES_LOCK = threading.Lock()


def default_instrument_catalog_cache() -> InstrumentCatalogCache:
    """Process-wide cache for the default (or ``$BT_API_CTP_CATALOG_CACHE_DIR``) path."""

    directory = default_catalog_cache_dir()
    key = str(directory)
    with _DEFAULT_CACHES_LOCK:
        cache = _DEFAULT_CACHES.get(key)
        if cache is None:
            cache = _DEFAULT_CACHES[key] = InstrumentCatalogCache(directory)
        return cache
