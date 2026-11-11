# ruff: noqa: UP045
"""Read an explicitly selected CTP instrument cache without native queries.

The cache is reference data, not a source of venue, listing, clock, account or
trading-permission proof. Its existing digest excludes timestamps and skipped
row count. A matching digest and an explicit caller-supplied trading day do
not prove freshness or authenticity. This module never uses the process cache,
configuration/environment defaults, network, CTP client, or filesystem writes.

Only a caller-declared trusted, cooperating local POSIX filesystem is supported.
Path and inode checks cannot establish that an in-place editor has finished,
detect adversarial ABA replacement, or prove Windows/network-filesystem and
power-loss semantics. Inputs must not be modified concurrently.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from bt_api_ctp.instrument_catalog_cache import (
    _ENTRY_FIELDS,
    CATALOG_FORMAT,
    CATALOG_FORMAT_VERSION,
    InstrumentCatalogEntry,
    _decode_catalog,
)

TRUSTED_LOCAL_POSIX = "TRUSTED_LOCAL_POSIX"
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_CONFLICTS = 100_000
MAX_COMPONENTS = 64
MAX_PATH_BYTES = 4096
MAX_JSON_NODES = 3_000_000
_ENTRY_KEYS = frozenset(_ENTRY_FIELDS)
_SCOPE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_DAY = re.compile(r"[0-9]{8}\Z", re.ASCII)
_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CALENDAR_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_VENUES = frozenset({"SHFE", "INE", "CZCE", "DCE", "CFFEX", "GFEX"})
_DOCUMENT_FIELDS = frozenset(
    {
        "format",
        "version",
        "scope",
        "trading_day",
        "complete",
        "built_at_utc",
        "published_at_utc",
        "skipped_rows",
        "conflicts",
        "entry_count",
        "content_sha256",
        "entries",
    }
)


class _ResourceLimit(ValueError):
    """The explicitly bounded local reader cannot inspect this input."""


@dataclass(frozen=True)
class LocalCatalogItem:
    """One unverified row; date bounds and underlying relations are declarations."""

    entry: InstrumentCatalogEntry = field(repr=False)
    kind: str
    identity_status: str
    lifecycle_status: str
    underlying_relation: str
    gaps: tuple[str, ...]


@dataclass(frozen=True)
class LocalCatalogSnapshot:
    """A bounded cache observation, never evidence that an instrument exists."""

    status: str
    reason: str
    scope: Optional[str] = field(default=None, repr=False)
    trading_day: Optional[str] = None
    entries: tuple[LocalCatalogItem, ...] = field(default=(), repr=False)
    conflicts: tuple[str, ...] = field(default=(), repr=False)
    skipped_rows: Optional[int] = None
    built_at_utc: Optional[str] = None
    published_at_utc: Optional[str] = None
    content_sha256: Optional[str] = field(default=None, repr=False)
    completeness: str = "UNVERIFIED_DECLARATION"
    source_authentication: str = "NOT_PROVEN"
    freshness: str = "NOT_PROVEN"
    absence: str = "NOT_PROVEN"
    metadata_digest_protection: str = "NOT_COVERED"
    writer_quiescence: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"


def _unknown(reason: str) -> LocalCatalogSnapshot:
    return LocalCatalogSnapshot("UNKNOWN", reason)


def _valid_day(value: object) -> bool:
    if type(value) is not str or _DAY.fullmatch(value) is None:
        return False
    try:
        return datetime.strptime(value, "%Y%m%d").strftime("%Y%m%d") == value
    except ValueError:
        return False


def _valid_utc(value: object) -> bool:
    if type(value) is not str or len(value) != 20:
        return False
    try:
        return (
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%dT%H:%M:%SZ") == value
        )
    except ValueError:
        return False


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode


def _file_state(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _path_parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("invalid root")
    root = os.fspath(value)
    if type(root) is not str or not root.startswith("/") or root.startswith("//") or "\x00" in root:
        raise ValueError("invalid root")
    if len(os.fsencode(root)) > MAX_PATH_BYTES or ".." in Path(root).parts:
        raise ValueError("invalid root")
    parts = tuple(part for part in Path(root).parts if part not in ("/", "."))
    if len(parts) + 1 > MAX_COMPONENTS or any(len(os.fsencode(part)) > 255 for part in parts):
        raise ValueError("invalid root")
    return parts


def _open_directory(parent: int, name: str) -> tuple[int, tuple[int, int, int]]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError("not a directory")
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        identity = _identity(os.fstat(fd))
        if identity != _identity(before):
            raise ValueError("directory changed")
        return fd, identity
    except Exception:
        os.close(fd)
        raise


def _read_snapshot(root: object, scope: str, trading_day: str) -> bytes:
    parts = (*_path_parts(root), scope)
    descriptors = []
    chain = []
    file_fd = None
    try:
        root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(root_fd)
        for name in parts:
            parent = descriptors[-1]
            child, identity = _open_directory(parent, name)
            descriptors.append(child)
            chain.append((parent, name, child, identity))
        filename = f"{trading_day}.json"
        directory = descriptors[-1]
        file_fd = os.open(
            filename,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory,
        )
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("invalid cache file")
        if before.st_size > MAX_FILE_BYTES:
            raise _ResourceLimit("cache file too large")
        data = bytearray()
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(file_fd, min(65536, MAX_FILE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(file_fd)
        entry = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        if len(data) > MAX_FILE_BYTES:
            raise _ResourceLimit("cache file too large")
        if (
            len(data) != before.st_size
            or _file_state(before) != _file_state(after)
            or _file_state(after) != _file_state(entry)
        ):
            raise ValueError("cache file changed")
        for parent, name, child, identity in chain:
            if (
                _identity(os.fstat(child)) != identity
                or _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity
            ):
                raise ValueError("directory changed")
        return bytes(data)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for fd in reversed(descriptors):
            os.close(fd)


def _item(entry: InstrumentCatalogEntry, ambiguous: set[str], trading_day: str) -> LocalCatalogItem:
    # CTP 6.7.7 ThostFtdcUserApiDataType.h ProductClass: Futures='1', Options='2'.
    kind = {"1": "future", "2": "option"}.get(entry.product_class, "UNKNOWN")
    gaps = []
    if kind == "UNKNOWN":
        gaps.append("PRODUCT_CLASS_UNKNOWN")
    if not _identity_text(entry.instrument_id):
        gaps.append("INSTRUMENT_ID_UNKNOWN")
    if not _identity_text(entry.product_id):
        gaps.append("PRODUCT_ID_UNKNOWN")
    if kind == "option" and not _identity_text(entry.underlying_instrument_id):
        gaps.append("UNDERLYING_UNKNOWN")
    if entry.exchange_id not in _VENUES:
        gaps.append("VENUE_UNKNOWN")
    if entry.instrument_id in ambiguous:
        gaps.append("INSTRUMENT_ID_AMBIGUOUS")
    identity = "UNVERIFIED_DECLARATION" if not gaps else "UNKNOWN"
    if (
        not _valid_day(entry.open_date)
        or not _valid_day(entry.expire_date)
        or entry.open_date > entry.expire_date
    ):
        gaps.append("LIFECYCLE_DATES_UNKNOWN")
        lifecycle = "UNKNOWN"
    elif entry.open_date <= trading_day <= entry.expire_date:
        lifecycle = "WITHIN_DECLARED_BOUNDS"
    else:
        gaps.append("OUTSIDE_DECLARED_LIFECYCLE")
        lifecycle = "OUTSIDE_DECLARED_BOUNDS"
    underlying = (
        "NOT_PROVEN" if kind == "option" else "NOT_APPLICABLE" if kind == "future" else "UNKNOWN"
    )
    return LocalCatalogItem(entry, kind, identity, lifecycle, underlying, tuple(gaps))


def _identity_text(value: object) -> bool:
    return type(value) is str and _CALENDAR_ID.fullmatch(value) is not None


def read_local_catalog(
    *, cache_dir: object, scope: object, trading_day: object, filesystem: object
) -> LocalCatalogSnapshot:
    """Read exactly one explicit cached catalog without querying or writing.

    ``filesystem`` must be TRUSTED_LOCAL_POSIX, a caller declaration rather
    than a filesystem authentication. A PARTIAL result preserves entries and
    conflicts; no result establishes complete listing or source truth. An
    UNKNOWN result intentionally includes no raw exception, path or scope.
    """
    if (
        filesystem != TRUSTED_LOCAL_POSIX
        or os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
    ):
        return _unknown("FILESYSTEM_UNSUPPORTED")
    if type(scope) is not str or _SCOPE.fullmatch(scope) is None or not _valid_day(trading_day):
        return _unknown("SCOPE_OR_DAY_INVALID")
    try:
        raw = _read_snapshot(cache_dir, scope, trading_day)
    except _ResourceLimit:
        return _unknown("CACHE_RESOURCE_LIMIT")
    except Exception:
        return _unknown("CACHE_UNAVAILABLE_OR_CHANGED")
    try:
        document = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_pairs)
        if (
            type(document) is not dict
            or set(document) != _DOCUMENT_FIELDS
            or document["format"] != CATALOG_FORMAT
            or type(document["version"]) is not int
            or document["version"] != CATALOG_FORMAT_VERSION
            or document["scope"] != scope
            or document["trading_day"] != trading_day
            or document["complete"] is not True
            or not _valid_utc(document["built_at_utc"])
            or not _valid_utc(document["published_at_utc"])
            or type(document["skipped_rows"]) is not int
            or document["skipped_rows"] < 0
            or type(document["content_sha256"]) is not str
            or _HASH.fullmatch(document["content_sha256"]) is None
            or type(document["entries"]) is not list
            or type(document["conflicts"]) is not list
            or any(
                type(item) is not str or not item or len(item) > 80 or item != item.strip()
                for item in document["conflicts"]
            )
            or len(document["conflicts"]) != len(set(document["conflicts"]))
        ):
            raise ValueError("invalid cache format")
        if len(document["entries"]) > MAX_ENTRIES or len(document["conflicts"]) > MAX_CONFLICTS:
            raise _ResourceLimit("catalog row limit")
        # The existing catalog is intentionally a shallow fixed-key schema.
        # Reject nested data and excessive nodes before its second JSON decode
        # and hash computation; the initial decode remains bounded by bytes.
        nodes = 1 + len(document) + len(document["conflicts"])
        if nodes > MAX_JSON_NODES:
            raise _ResourceLimit("catalog node budget exceeded")
        for row in document["entries"]:
            if type(row) is not dict or row.keys() != _ENTRY_KEYS:
                raise ValueError("invalid catalog entry")
            nodes += 1 + len(row)
            if nodes > MAX_JSON_NODES:
                raise _ResourceLimit("catalog node budget exceeded")
            if any(
                value is not None and type(value) not in (str, int, float, bool)
                for value in row.values()
            ):
                raise ValueError("nested catalog value")
        # Preserve the original, explicitly checked metadata.  Release the
        # decoded JSON tree before using the existing cache decoder: holding
        # both decoded 100k-entry documents at once is unnecessary.  Timestamps
        # and skipped_rows remain uncovered by the legacy content digest.
        built_at_utc = document["built_at_utc"]
        published_at_utc = document["published_at_utc"]
        content_sha256 = document["content_sha256"]
        del document
        catalog = _decode_catalog(raw, scope, trading_day)
        rows = catalog.entries()
        venues = {}
        for entry in rows:
            venues.setdefault(entry.instrument_id, set()).add(entry.exchange_id)
        ambiguous = {name for name, ids in venues.items() if len(ids) != 1}
        entries = tuple(_item(entry, ambiguous, trading_day) for entry in rows)
        if not entries:
            status, reason = "UNKNOWN", "EMPTY_CATALOG"
        elif catalog.conflicts or catalog.skipped_rows or any(item.gaps for item in entries):
            status, reason = "PARTIAL", "REFERENCE_GAPS_PRESENT"
        else:
            status, reason = "CANDIDATE", "UNVERIFIED_CACHE"
        return LocalCatalogSnapshot(
            status,
            reason,
            scope=scope,
            trading_day=trading_day,
            entries=entries,
            conflicts=tuple(sorted(catalog.conflicts)),
            skipped_rows=catalog.skipped_rows,
            built_at_utc=built_at_utc,
            published_at_utc=published_at_utc,
            content_sha256=content_sha256,
        )
    except _ResourceLimit:
        return _unknown("CACHE_RESOURCE_LIMIT")
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError, KeyError):
        return _unknown("CACHE_INVALID")
