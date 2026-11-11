"""Experimental, unverified YAML supplement candidates; not a calendar authority.

The input is already-read UTF-8 bytes with exactly the four snapshot content
layers: ``sources``, ``trading_days``, ``trading_hours`` and ``exceptions``.
Their record fields match the experimental ``calendar_snapshot`` schema, but
individual layers may be partial. Quote dates and times in YAML. Sources contain
only ``id`` and ``description``; references and descriptions prove no authority.
This module never resolves conflicts, verifies sources, infers UTC sessions,
performs file IO or publishes. In particular, do not load its output directly as
a verified snapshot. Safe concurrent file reading and publication remain GAPs.
All returned material is UNVERIFIED, including explicit completeness claims.
Passing candidate content to ``CalendarSnapshot`` can yield ``COMPLETE`` when
the input claims both completeness flags. That status expresses the supplied
structural completeness claim, not verified source truth or publication approval;
``sources_verified`` remains false. Candidate verification does not travel with
the plain content dictionary. Never use that result alone for automatic release.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time
from typing import Any

MAX_INPUT_BYTES = 262144
MAX_YAML_EVENTS = 20000
MAX_DEPTH = 24
MAX_LIST_ITEMS = 1000
MAX_TEXT_LENGTH = 4096
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z", re.ASCII)
_VENUES = frozenset({"SHFE", "INE", "CZCE", "DCE", "CFFEX", "GFEX"})
_LAYERS = {"sources", "trading_days", "trading_hours", "exceptions"}


class SupplementError(ValueError):
    """Invalid supplement or unavailable optional YAML parser."""


@dataclass(frozen=True)
class SupplementCandidate:
    """Detached input, not proof of validity or suitability for publication."""

    content: dict[str, Any]
    unused_source_ids: tuple[str, ...]
    verification: str = "UNVERIFIED"


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise SupplementError("unexpected or missing fields")
    return value


def _list(value: Any) -> list:
    if type(value) is not list or len(value) > MAX_LIST_ITEMS:
        raise SupplementError("expected bounded list")
    return value


def _id(value: Any) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise SupplementError("invalid identifier")
    return value


def _date(value: Any) -> date:
    if type(value) is not str:
        raise SupplementError("date must be a quoted YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SupplementError("invalid date") from exc
    if parsed.isoformat() != value:
        raise SupplementError("date must be a quoted YYYY-MM-DD string")
    return parsed


def _clock(value: Any) -> time:
    if type(value) is not str or _CLOCK.fullmatch(value) is None:
        raise SupplementError("time must be a quoted HH:MM string")
    return time.fromisoformat(value)


def _venue(value: Any) -> None:
    if type(value) is not str or value not in _VENUES:
        raise SupplementError("unsupported venue")


def _scope(row: dict[str, Any]) -> None:
    _venue(row["venue"])
    if type(row["kind"]) is not str or row["kind"] not in {"future", "option"}:
        raise SupplementError("unsupported contract kind")
    _id(row["product"])


def _refs(value: Any, sources: set[str], used: set[str]) -> None:
    refs = [_id(item) for item in _list(value)]
    if not refs or len(set(refs)) != len(refs) or not set(refs) <= sources:
        raise SupplementError("missing, duplicate, or unknown source reference")
    used.update(refs)


def _windows(value: Any, *, night: bool) -> list[str]:
    names = []
    for item in _list(value):
        row = _object(item, {"id", "phase", "start", "end", "end_day_offset"})
        names.append(_id(row["id"]))
        if type(row["phase"]) is not str or row["phase"] not in {"continuous", "auction"}:
            raise SupplementError("invalid window phase")
        offset = row["end_day_offset"]
        if type(offset) is not int or offset not in (0, 1) or (not night and offset != 0):
            raise SupplementError("invalid window offset")
        start, end = _clock(row["start"]), _clock(row["end"])
        if (offset == 0 and end <= start) or (offset == 1 and end >= start):
            raise SupplementError("invalid window bounds")
    return names


def _validate(content: Any) -> SupplementCandidate:
    content = _object(content, _LAYERS)
    layers = {name: _list(content[name]) for name in _LAYERS}
    sources = set()
    for item in layers["sources"]:
        row = _object(item, {"id", "description"})
        source_id = _id(row["id"])
        description = row["description"]
        if (
            source_id in sources
            or type(description) is not str
            or not description.strip()
            or len(description) > MAX_TEXT_LENGTH
        ):
            raise SupplementError("duplicate or invalid source")
        sources.add(source_id)
    if not sources or not any(layers[name] for name in _LAYERS - {"sources"}):
        raise SupplementError("sources and at least one record are required")
    used: set[str] = set()
    for item in layers["trading_days"]:
        row = _object(item, {"venue", "day", "status", "source_ids"})
        _venue(row["venue"])
        _date(row["day"])
        if type(row["status"]) is not str or row["status"] not in {"open", "closed"}:
            raise SupplementError("invalid day status")
        _refs(row["source_ids"], sources, used)
    for item in layers["trading_hours"]:
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
        _scope(row)
        start, end = _date(row["valid_from"]), _date(row["valid_to"])
        if start > end:
            raise SupplementError("invalid effective period")
        contracts = [_id(contract) for contract in _list(row["contracts"])]
        if not contracts or len(contracts) != len(set(contracts)):
            raise SupplementError("empty or duplicate contracts")
        if (
            type(row["windows_complete"]) is not bool
            or type(row["exceptions_complete"]) is not bool
        ):
            raise SupplementError("completeness must be explicit booleans")
        names = _windows(row["day_windows"], night=False) + _windows(
            row["night_windows"], night=True
        )
        if len(names) != len(set(names)):
            raise SupplementError("duplicate window id")
        mappings = set()
        for item_mapping in _list(row["night_mappings"]):
            mapping = _object(item_mapping, {"trading_day", "start_date", "source_ids"})
            day = _date(mapping["trading_day"])
            local = None if mapping["start_date"] is None else _date(mapping["start_date"])
            if (
                day in mappings
                or not start <= day <= end
                or (local is not None and (local >= day or not row["night_windows"]))
            ):
                raise SupplementError("invalid night mapping")
            mappings.add(day)
            _refs(mapping["source_ids"], sources, used)
        _refs(row["source_ids"], sources, used)
    for item in layers["exceptions"]:
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
        _scope(row)
        if row["contract"] is not None:
            _id(row["contract"])
        _date(row["trading_day"])
        action = row["action"]
        if type(action) is not str or action not in {"close_day", "cancel_window"}:
            raise SupplementError("unsupported exception action")
        if action == "close_day" and row["window_id"] is not None:
            raise SupplementError("close_day cannot specify a window")
        if action == "cancel_window":
            _id(row["window_id"])
        _refs(row["source_ids"], sources, used)
    return SupplementCandidate(content=content, unused_source_ids=tuple(sorted(sources - used)))


def parse_supplement(data: bytes) -> SupplementCandidate:
    """Parse a bounded YAML document into an unverified candidate, without IO.

    ``CalendarSnapshot`` may report ``COMPLETE`` for author-supplied completeness
    flags; that status neither verifies sources nor authorizes publication.
    """
    if type(data) is not bytes or len(data) > MAX_INPUT_BYTES:
        raise SupplementError("input must be bounded bytes")
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise SupplementError("input is not UTF-8") from exc
    try:
        import yaml
    except ImportError as exc:
        raise SupplementError("optional PyYAML dependency unavailable") from exc

    class UniqueSafeLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if type(key) is not str or key in result:
                    raise SupplementError("duplicate or non-string mapping key")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    try:
        depth = 0
        for count, event in enumerate(yaml.parse(text, Loader=UniqueSafeLoader), 1):
            if count > MAX_YAML_EVENTS or isinstance(event, yaml.events.AliasEvent):
                raise SupplementError("YAML event limit exceeded or alias forbidden")
            if getattr(event, "anchor", None) is not None:
                raise SupplementError("YAML anchors are forbidden")
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                if depth > MAX_DEPTH:
                    raise SupplementError("YAML nesting limit exceeded")
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
        return _validate(yaml.load(text, Loader=UniqueSafeLoader))
    except yaml.YAMLError as exc:
        raise SupplementError("invalid or unsupported YAML") from exc
