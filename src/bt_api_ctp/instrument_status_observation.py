# ruff: noqa: UP007, UP045
"""Provider-local neutral projection of CTP ``OnRtnInstrumentStatus``.

This module owns only CTP venue facts.  It turns the exact native status
callback into an immutable, versioned observation, attaches the authenticated
session facts supplied by the embedding composition, and keeps a per-instrument
validity scope in :class:`InstrumentStatusLedger`.  It does not contact a
front, read a clock, choose a route, compose a calendar answer, authorize
trading, or forward observations to another process; those belong to later SDK
and Gateway work.  ``calendar_instrument_status`` remains the earlier scalar
candidate helper; it carries no session scope or ordering and is not the
validity answer used here.

Native layout
-------------
``CThostFtdcInstrumentStatusField`` (bundled 6.7.7 header) carries
``ExchangeID``, ``ExchangeInstID``, ``InstrumentID``, ``InstrumentStatus``,
``TradingSegmentSN``, ``EnterTime`` and ``EnterReason``.  It carries **no**
``TradingDay``: the trading day and the session generation come from the
authenticated session owner, not from the callback and never from the local
clock.  ``EnterTime`` is a bare ``HH:MM:SS`` text with no date or timezone.
``TradingSegmentSN`` is a per-segment counter, not a global sequence, so this
module requires the source owner to supply an explicit arrival sequence.

Status codes ``0``-``7`` map to the neutral categories below.  Any other
single-character code becomes the explicit ``unknown`` category; it is never
guessed.  The same rule applies to ``EnterReason`` (``1`` automatic, ``2``
manual, ``3`` fuse).  The category meanings still require authorized native
observation before they can claim venue behaviour; this module only projects.

Validity
--------
An observation is valid only for the ``(venue, native symbol)`` identity, the
``session_generation`` and the ``trading_day`` that produced it.  A generation
or trading-day change marks every prior observation stale; a stale observation
is reported with ``effective_status_category == "unknown"`` and is never
silently reused.  Arrival sequence is monotonic per identity and validity
scope: duplicates and out-of-order callbacks leave the current observation
untouched, so a late old callback cannot resurrect a superseded status.  No
freshness or expiry is derived from ``received_at``; the domain is explicit
and only the owner may decide what the stamp means.

All refusals carry one finite code from :data:`INSTRUMENT_STATUS_CODES` and no
native value.  A malformed or unavailable source is refused rather than
partially projected.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Optional, Union

__all__ = (
    "CtpInstrumentStatusObservationError",
    "ENTER_REASON_AUTOMATIC",
    "ENTER_REASON_FUSE",
    "ENTER_REASON_MANUAL",
    "ENTER_REASON_UNKNOWN",
    "INSTRUMENT_STATUS_CODES",
    "InstrumentStatusApplyResult",
    "InstrumentStatusGenerationResult",
    "InstrumentStatusLedger",
    "InstrumentStatusObservation",
    "InstrumentStatusSessionFacts",
    "InstrumentStatusView",
    "NATIVE_ENTER_REASONS",
    "NATIVE_STATUS_CATEGORIES",
    "OBSERVATION_SCHEMA",
    "RECEIVE_CLOCK_MONOTONIC_NS",
    "RECEIVE_CLOCK_UTC_ISO8601",
    "STATE_CURRENT",
    "STATE_STALE",
    "STATE_UNKNOWN",
    "STATUS_AUCTION_BALANCE",
    "STATUS_AUCTION_MATCH",
    "STATUS_AUCTION_ORDERING",
    "STATUS_BEFORE_TRADING",
    "STATUS_CLOSED",
    "STATUS_CONTINUOUS",
    "STATUS_NO_TRADING",
    "STATUS_TRANSACTION_PROCESSING",
    "STATUS_UNKNOWN",
    "native_fields_from_ingress_payload",
    "project_native_instrument_status",
)

OBSERVATION_SCHEMA = "ctp_instrument_status_observation.v1"

STATUS_BEFORE_TRADING = "before_trading"
STATUS_NO_TRADING = "no_trading"
STATUS_CONTINUOUS = "continuous"
STATUS_AUCTION_ORDERING = "auction_ordering"
STATUS_AUCTION_BALANCE = "auction_balance"
STATUS_AUCTION_MATCH = "auction_match"
STATUS_CLOSED = "closed"
STATUS_TRANSACTION_PROCESSING = "transaction_processing"
STATUS_UNKNOWN = "unknown"

NATIVE_STATUS_CATEGORIES = MappingProxyType(
    {
        "0": STATUS_BEFORE_TRADING,
        "1": STATUS_NO_TRADING,
        "2": STATUS_CONTINUOUS,
        "3": STATUS_AUCTION_ORDERING,
        "4": STATUS_AUCTION_BALANCE,
        "5": STATUS_AUCTION_MATCH,
        "6": STATUS_CLOSED,
        "7": STATUS_TRANSACTION_PROCESSING,
    }
)

ENTER_REASON_AUTOMATIC = "automatic"
ENTER_REASON_MANUAL = "manual"
ENTER_REASON_FUSE = "fuse"
ENTER_REASON_UNKNOWN = "unknown"

NATIVE_ENTER_REASONS = MappingProxyType(
    {
        "1": ENTER_REASON_AUTOMATIC,
        "2": ENTER_REASON_MANUAL,
        "3": ENTER_REASON_FUSE,
    }
)

RECEIVE_CLOCK_MONOTONIC_NS = "monotonic_ns"
RECEIVE_CLOCK_UTC_ISO8601 = "utc_iso8601"
_RECEIVE_CLOCK_DOMAINS = frozenset({RECEIVE_CLOCK_MONOTONIC_NS, RECEIVE_CLOCK_UTC_ISO8601})
_STATUS_CATEGORY_SET = frozenset(NATIVE_STATUS_CATEGORIES.values()) | {STATUS_UNKNOWN}
_ENTER_REASON_SET = frozenset(NATIVE_ENTER_REASONS.values()) | {ENTER_REASON_UNKNOWN}

STATE_CURRENT = "current"
STATE_STALE = "stale"
STATE_UNKNOWN = "unknown"

ACTION_APPLIED = "applied"
ACTION_REFUSED = "refused"
ACTION_DUPLICATE = "duplicate"
ACTION_OUT_OF_ORDER = "out_of_order"
ACTION_SEQUENCE_CONFLICT = "sequence_conflict"
ACTION_IDENTITY_CONFLICT = "identity_conflict"
ACTION_STALE_GENERATION = "stale_generation"
ACTION_STALE_TRADING_DAY = "stale_trading_day"
ACTION_SOURCE_UNAVAILABLE = "source_unavailable"

GENERATION_INITIALIZED = "initialized"
GENERATION_UNCHANGED = "unchanged"
GENERATION_INVALIDATED = "invalidated"

CODE_FIELDS_INVALID = "ctp_instrument_status_fields_invalid"
CODE_FIELDS_UNEXPECTED = "ctp_instrument_status_fields_unexpected"
CODE_FIELDS_INCOMPLETE = "ctp_instrument_status_fields_incomplete"
CODE_FIELD_INVALID = "ctp_instrument_status_field_invalid"
CODE_SESSION_INVALID = "ctp_instrument_status_session_invalid"
CODE_GENERATION_INVALID = "ctp_instrument_status_generation_invalid"
CODE_GENERATION_MISMATCH = "ctp_instrument_status_generation_mismatch"
CODE_TRADING_DAY_INVALID = "ctp_instrument_status_trading_day_invalid"
CODE_TRADING_DAY_MISMATCH = "ctp_instrument_status_trading_day_mismatch"
CODE_RECEIVE_TIME_INVALID = "ctp_instrument_status_receive_time_invalid"
CODE_SEQUENCE_INVALID = "ctp_instrument_status_sequence_invalid"
CODE_SOURCE_UNAVAILABLE = "ctp_instrument_status_source_unavailable"
CODE_IDENTITY_CONFLICT = "ctp_instrument_status_identity_conflict"
CODE_SEQUENCE_OUT_OF_ORDER = "ctp_instrument_status_sequence_out_of_order"
CODE_DUPLICATE = "ctp_instrument_status_duplicate"
CODE_SEQUENCE_CONFLICT = "ctp_instrument_status_sequence_conflict"
CODE_STALE_GENERATION = "ctp_instrument_status_stale_generation"
CODE_STALE_TRADING_DAY = "ctp_instrument_status_stale_trading_day"
CODE_NEVER_OBSERVED = "ctp_instrument_status_never_observed"
CODE_STATUS_UNKNOWN = "ctp_instrument_status_status_unknown"
CODE_OBSERVATION_INVALID = "ctp_instrument_status_observation_invalid"

INSTRUMENT_STATUS_CODES = frozenset(
    {
        CODE_FIELDS_INVALID,
        CODE_FIELDS_UNEXPECTED,
        CODE_FIELDS_INCOMPLETE,
        CODE_FIELD_INVALID,
        CODE_SESSION_INVALID,
        CODE_GENERATION_INVALID,
        CODE_GENERATION_MISMATCH,
        CODE_TRADING_DAY_INVALID,
        CODE_TRADING_DAY_MISMATCH,
        CODE_RECEIVE_TIME_INVALID,
        CODE_SEQUENCE_INVALID,
        CODE_SOURCE_UNAVAILABLE,
        CODE_IDENTITY_CONFLICT,
        CODE_SEQUENCE_OUT_OF_ORDER,
        CODE_DUPLICATE,
        CODE_SEQUENCE_CONFLICT,
        CODE_STALE_GENERATION,
        CODE_STALE_TRADING_DAY,
        CODE_NEVER_OBSERVED,
        CODE_STATUS_UNKNOWN,
        CODE_OBSERVATION_INVALID,
    }
)


class CtpInstrumentStatusObservationError(ValueError):
    """Finite, value-free failure raised by the provider-local extractor."""

    def __init__(self, code: str) -> None:
        self.code = (
            code
            if type(code) is str and code in INSTRUMENT_STATUS_CODES
            else CODE_OBSERVATION_INVALID
        )
        super().__init__(self.code)


# Native field names selected from ``CThostFtdcInstrumentStatusField``.
_NATIVE_FIELDS = (
    "ExchangeID",
    "ExchangeInstID",
    "InstrumentID",
    "InstrumentStatus",
    "TradingSegmentSN",
    "EnterTime",
    "EnterReason",
)
_NATIVE_FIELD_SET = frozenset(_NATIVE_FIELDS)
# The struct also declares these fields; they are allowed but never copied.
_IGNORED_STRUCT_FIELDS = frozenset({"reserve1", "SettlementGroupID", "reserve2"})
_ALLOWED_FIELDS = _NATIVE_FIELD_SET | _IGNORED_STRUCT_FIELDS

_CLOCK_TEXT = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\Z", re.ASCII)
_TRADING_DAY_TEXT = re.compile(r"[0-9]{8}\Z", re.ASCII)
_UTC_TEXT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?Z\Z",
    re.ASCII,
)
_MAX_GENERATION_LENGTH = 128
_MAX_EXCHANGE_ID_LENGTH = 8
_MAX_INSTRUMENT_ID_LENGTH = 80
_MAX_SEGMENT_SN = 2**31 - 1
_UTC = timedelta(0)
_INGRESS_SCHEMA = "ctp_trader_callback_ingress.v2"
_CALLBACK_NAME = "OnRtnInstrumentStatus"


@dataclass(frozen=True)
class InstrumentStatusSessionFacts:
    """Untrusted session facts supplied by the authenticated session owner.

    ``session_generation`` is an opaque provider-local token (for example the
    native session generation bound by the embedding composition).  This
    module never derives it from the callback.  ``source_sequence`` is the
    arrival order assigned by the source owner, not ``TradingSegmentSN``.
    ``received_at`` is kept exactly as supplied together with its explicit
    clock domain; nothing is inferred from it.
    """

    session_generation: object
    trading_day: object
    source_sequence: object
    receive_clock_domain: object
    receive_time: object
    source_available: object = True


@dataclass(frozen=True)
class InstrumentStatusObservation:
    """Neutral projection result; refusals carry no native value."""

    accepted: bool
    reason_code: Optional[str] = None
    venue: Optional[str] = None
    native_symbol: Optional[str] = None
    exchange_symbol: Optional[str] = None
    status_category: str = STATUS_UNKNOWN
    native_status_code: Optional[str] = None
    enter_reason: str = ENTER_REASON_UNKNOWN
    enter_time: Optional[str] = None
    trading_segment_sn: Optional[int] = None
    trading_day: Optional[str] = None
    session_generation: Optional[str] = None
    source_sequence: Optional[int] = None
    received_at: Optional[Union[int, str]] = None
    receive_clock_domain: Optional[str] = None
    missing_fields: tuple[str, ...] = ()
    invalid_fields: tuple[str, ...] = ()
    schema: str = OBSERVATION_SCHEMA


@dataclass(frozen=True)
class InstrumentStatusApplyResult:
    applied: bool
    action: str
    reason_code: Optional[str] = None


@dataclass(frozen=True)
class InstrumentStatusGenerationResult:
    action: str
    reason_code: Optional[str] = None


@dataclass(frozen=True)
class InstrumentStatusView:
    """Effective scope answer for one instrument.

    ``effective_status_category`` is ``unknown`` unless the view is current and
    the observed category is known.  ``observed_status_category`` is kept only
    for diagnostics and must not be used as a current fact when ``usable`` is
    false.
    """

    venue: Optional[str]
    native_symbol: Optional[str]
    state: str
    effective_status_category: str
    observed_status_category: Optional[str]
    usable: bool
    reason_code: Optional[str]
    observation: Optional[InstrumentStatusObservation]
    session_generation: Optional[str]
    trading_day: Optional[str]


@dataclass(frozen=True)
class _Entry:
    observation: InstrumentStatusObservation
    state: str
    stale_reason: Optional[str]


def _identifier(value: object, max_length: int) -> bool:
    # ASCII printable scalars only: no whitespace, separators or embedded NUL.
    return (
        type(value) is str
        and 0 < len(value) <= max_length
        and all(0x21 <= ord(char) <= 0x7E for char in value)
    )


def _single_code(value: object) -> bool:
    return type(value) is str and len(value) == 1 and 0x21 <= ord(value) <= 0x7E


def _valid_generation(value: object) -> bool:
    return _identifier(value, _MAX_GENERATION_LENGTH)


def _valid_trading_day(value: object) -> bool:
    if type(value) is not str or _TRADING_DAY_TEXT.fullmatch(value) is None:
        return False
    try:
        date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return False
    return True


def _valid_utc_text(value: object) -> bool:
    if type(value) is not str or _UTC_TEXT.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == _UTC


def _valid_receive_stamp(domain: object, value: object) -> bool:
    if type(domain) is not str:
        return False
    if domain == RECEIVE_CLOCK_MONOTONIC_NS:
        return type(value) is int and value > 0
    if domain == RECEIVE_CLOCK_UTC_ISO8601:
        return _valid_utc_text(value)
    return False


def _session_issue(session: object) -> Optional[str]:
    if type(session) is not InstrumentStatusSessionFacts:
        return CODE_SESSION_INVALID
    if type(session.source_available) is not bool:
        return CODE_SESSION_INVALID
    if not _valid_generation(session.session_generation):
        return CODE_GENERATION_INVALID
    if not _valid_trading_day(session.trading_day):
        return CODE_TRADING_DAY_INVALID
    if type(session.source_sequence) is not int or session.source_sequence < 1:
        return CODE_SEQUENCE_INVALID
    if (
        type(session.receive_clock_domain) is not str
        or session.receive_clock_domain not in _RECEIVE_CLOCK_DOMAINS
    ):
        return CODE_RECEIVE_TIME_INVALID
    if not _valid_receive_stamp(session.receive_clock_domain, session.receive_time):
        return CODE_RECEIVE_TIME_INVALID
    if session.source_available is False:
        return CODE_SOURCE_UNAVAILABLE
    return None


def _observation_issue(observation: InstrumentStatusObservation) -> Optional[str]:
    # Re-validate an accepted projection so a forged or reconstructed object
    # cannot inject an identity, ordering key or category into the ledger.
    if observation.accepted is not True:
        return CODE_OBSERVATION_INVALID
    if (
        not _identifier(observation.venue, _MAX_EXCHANGE_ID_LENGTH)
        or not _identifier(observation.native_symbol, _MAX_INSTRUMENT_ID_LENGTH)
        or not _identifier(observation.exchange_symbol, _MAX_INSTRUMENT_ID_LENGTH)
    ):
        return CODE_OBSERVATION_INVALID
    if not _valid_generation(observation.session_generation):
        return CODE_OBSERVATION_INVALID
    if not _valid_trading_day(observation.trading_day):
        return CODE_OBSERVATION_INVALID
    if type(observation.source_sequence) is not int or observation.source_sequence < 1:
        return CODE_OBSERVATION_INVALID
    if (
        type(observation.status_category) is not str
        or observation.status_category not in _STATUS_CATEGORY_SET
    ):
        return CODE_OBSERVATION_INVALID
    if not _single_code(observation.native_status_code):
        return CODE_OBSERVATION_INVALID
    if (
        type(observation.enter_reason) is not str
        or observation.enter_reason not in _ENTER_REASON_SET
    ):
        return CODE_OBSERVATION_INVALID
    if (
        type(observation.enter_time) is not str
        or _CLOCK_TEXT.fullmatch(observation.enter_time) is None
    ):
        return CODE_OBSERVATION_INVALID
    if (
        type(observation.trading_segment_sn) is not int
        or not 0 <= observation.trading_segment_sn <= _MAX_SEGMENT_SN
    ):
        return CODE_OBSERVATION_INVALID
    if not _valid_receive_stamp(observation.receive_clock_domain, observation.received_at):
        return CODE_OBSERVATION_INVALID
    return None


def _refused(
    code: str,
    *,
    missing: tuple[str, ...] = (),
    invalid: tuple[str, ...] = (),
) -> InstrumentStatusObservation:
    return InstrumentStatusObservation(
        accepted=False,
        reason_code=code,
        missing_fields=missing,
        invalid_fields=invalid,
    )


def project_native_instrument_status(
    fields: object, *, session: InstrumentStatusSessionFacts
) -> InstrumentStatusObservation:
    """Project the selected native fields; refuse rather than guess.

    ``fields`` is a plain ``dict`` holding exactly the native status struct
    fields (the three other struct fields are accepted but ignored).  Unknown
    keys, missing fields, wrong types, an unusable session generation, an
    unavailable source, or an invalid receive stamp all produce a refusal with
    one finite code.  An unrecognised single-character native status maps to
    the explicit ``unknown`` category instead of failing the identity.
    """

    if type(fields) is not dict or any(type(key) is not str for key in fields):
        return _refused(CODE_FIELDS_INVALID)
    if not set(fields) <= _ALLOWED_FIELDS:
        return _refused(CODE_FIELDS_UNEXPECTED)

    missing: list[str] = []
    invalid: list[str] = []
    values: dict[str, object] = {}
    for key in _NATIVE_FIELDS:
        if (
            key not in fields
            or fields[key] is None
            or (type(fields[key]) is str and fields[key] == "")
        ):
            missing.append(key)
            continue
        value = fields[key]
        if key == "ExchangeID":
            valid = _identifier(value, _MAX_EXCHANGE_ID_LENGTH)
        elif key in ("ExchangeInstID", "InstrumentID"):
            valid = _identifier(value, _MAX_INSTRUMENT_ID_LENGTH)
        elif key in ("InstrumentStatus", "EnterReason"):
            valid = _single_code(value)
        elif key == "TradingSegmentSN":
            valid = type(value) is int and 0 <= value <= _MAX_SEGMENT_SN
        else:  # EnterTime
            valid = type(value) is str and _CLOCK_TEXT.fullmatch(value) is not None
        if valid:
            values[key] = value
        else:
            invalid.append(key)
    if missing:
        return _refused(CODE_FIELDS_INCOMPLETE, missing=tuple(missing), invalid=tuple(invalid))
    if invalid:
        return _refused(CODE_FIELD_INVALID, invalid=tuple(invalid))

    issue = _session_issue(session)
    if issue is not None:
        return _refused(issue)

    status_code = values["InstrumentStatus"]
    return InstrumentStatusObservation(
        accepted=True,
        venue=values["ExchangeID"],
        native_symbol=values["InstrumentID"],
        exchange_symbol=values["ExchangeInstID"],
        status_category=NATIVE_STATUS_CATEGORIES.get(status_code, STATUS_UNKNOWN),
        native_status_code=status_code,
        enter_reason=NATIVE_ENTER_REASONS.get(values["EnterReason"], ENTER_REASON_UNKNOWN),
        enter_time=values["EnterTime"],
        trading_segment_sn=values["TradingSegmentSN"],
        trading_day=session.trading_day,
        session_generation=session.session_generation,
        source_sequence=session.source_sequence,
        received_at=session.receive_time,
        receive_clock_domain=session.receive_clock_domain,
    )


def native_fields_from_ingress_payload(payload: object) -> dict[str, object]:
    """Select the seven safe native fields from a callback ingress payload.

    The ingress record is the provider-local fake/real callback surface.  This
    helper rejects a different callback, an incomplete capture, ambiguous
    duplicates, and any non-scalar value.  Unselected fields are ignored and
    never returned.  A missing selected field raises
    ``CtpInstrumentStatusObservationError(CODE_FIELDS_INCOMPLETE)``.
    """

    if type(payload) is not dict:
        raise CtpInstrumentStatusObservationError(CODE_FIELDS_INVALID)
    schema = payload.get("schema")
    callback_name = payload.get("callback_name")
    if (
        type(schema) is not str
        or schema != _INGRESS_SCHEMA
        or type(callback_name) is not str
        or callback_name != _CALLBACK_NAME
    ):
        raise CtpInstrumentStatusObservationError(CODE_FIELDS_INVALID)
    missing_getters = payload.get("missing_getters")
    if (
        payload.get("capture_complete") is not True
        or type(missing_getters) is not list
        or missing_getters != []
    ):
        raise CtpInstrumentStatusObservationError(CODE_FIELDS_INCOMPLETE)
    flattened = payload.get("flattened_fields")
    if type(flattened) is not list:
        raise CtpInstrumentStatusObservationError(CODE_FIELDS_INVALID)

    fields: dict[str, object] = {}
    for item in flattened:
        if type(item) is not dict:
            continue
        slot = item.get("argument_slot")
        if type(slot) is not int or slot != 0:
            continue
        name = item.get("field_name")
        if type(name) is not str or name not in _NATIVE_FIELD_SET:
            continue
        if name in fields:
            raise CtpInstrumentStatusObservationError(CODE_FIELDS_INVALID)
        if item.get("present") is not True or item.get("scalar_captured") is not True:
            continue
        fields[name] = item.get("value")
    if set(fields) != _NATIVE_FIELD_SET:
        raise CtpInstrumentStatusObservationError(CODE_FIELDS_INCOMPLETE)
    return fields


_SAME_FIELDS = (
    "venue",
    "native_symbol",
    "exchange_symbol",
    "status_category",
    "native_status_code",
    "enter_reason",
    "enter_time",
    "trading_segment_sn",
    "trading_day",
    "session_generation",
    "source_sequence",
)


def _same_observation(
    left: InstrumentStatusObservation, right: InstrumentStatusObservation
) -> bool:
    return all(getattr(left, name) == getattr(right, name) for name in _SAME_FIELDS)


class InstrumentStatusLedger:
    """Per-instrument validity scope for one provider session.

    The caller certifies session lifecycle transitions through
    :meth:`begin_session_generation`.  A changed generation or trading day
    invalidates every prior observation, including a prior "source
    unavailable" mark; a new observation is required before anything is
    usable again.
    """

    def __init__(self, *, session_generation: object, trading_day: object) -> None:
        issue = _session_issue(
            InstrumentStatusSessionFacts(
                session_generation=session_generation,
                trading_day=trading_day,
                source_sequence=1,
                receive_clock_domain=RECEIVE_CLOCK_MONOTONIC_NS,
                receive_time=1,
                source_available=True,
            )
        )
        if issue is not None:
            raise CtpInstrumentStatusObservationError(issue)
        self._lock = threading.Lock()
        self._session_generation = session_generation
        self._trading_day = trading_day
        self._source_available = True
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._last_sequence: dict[tuple[str, str], int] = {}

    def begin_session_generation(
        self, session_generation: object, trading_day: object
    ) -> InstrumentStatusGenerationResult:
        """Advance the trusted session lifecycle and invalidate old statuses."""

        issue = _session_issue(
            InstrumentStatusSessionFacts(
                session_generation=session_generation,
                trading_day=trading_day,
                source_sequence=1,
                receive_clock_domain=RECEIVE_CLOCK_MONOTONIC_NS,
                receive_time=1,
                source_available=True,
            )
        )
        if issue is not None:
            raise CtpInstrumentStatusObservationError(issue)
        with self._lock:
            if (session_generation, trading_day) == (
                self._session_generation,
                self._trading_day,
            ):
                return InstrumentStatusGenerationResult(GENERATION_UNCHANGED)
            reason = (
                CODE_STALE_GENERATION
                if session_generation != self._session_generation
                else CODE_STALE_TRADING_DAY
            )
            for key, entry in self._entries.items():
                self._entries[key] = _Entry(entry.observation, STATE_STALE, reason)
            self._session_generation = session_generation
            self._trading_day = trading_day
            self._source_available = True
            self._last_sequence.clear()
            return InstrumentStatusGenerationResult(GENERATION_INVALIDATED, reason)

    def observe(self, observation: object) -> InstrumentStatusApplyResult:
        """Apply one projection if it is current; never resurrect an old state."""

        if type(observation) is not InstrumentStatusObservation:
            return InstrumentStatusApplyResult(False, ACTION_REFUSED, CODE_OBSERVATION_INVALID)
        if observation.accepted is not True:
            return InstrumentStatusApplyResult(
                False, ACTION_REFUSED, observation.reason_code or CODE_OBSERVATION_INVALID
            )
        issue = _observation_issue(observation)
        if issue is not None:
            return InstrumentStatusApplyResult(False, ACTION_REFUSED, issue)
        with self._lock:
            if not self._source_available:
                return InstrumentStatusApplyResult(
                    False, ACTION_SOURCE_UNAVAILABLE, CODE_SOURCE_UNAVAILABLE
                )
            if observation.session_generation != self._session_generation:
                return InstrumentStatusApplyResult(
                    False, ACTION_STALE_GENERATION, CODE_GENERATION_MISMATCH
                )
            if observation.trading_day != self._trading_day:
                return InstrumentStatusApplyResult(
                    False, ACTION_STALE_TRADING_DAY, CODE_TRADING_DAY_MISMATCH
                )
            key = (observation.venue, observation.native_symbol)
            entry = self._entries.get(key)
            if (
                entry is not None
                and observation.exchange_symbol != entry.observation.exchange_symbol
            ):
                return InstrumentStatusApplyResult(
                    False, ACTION_IDENTITY_CONFLICT, CODE_IDENTITY_CONFLICT
                )
            last = self._last_sequence.get(key)
            if last is not None and observation.source_sequence <= last:
                if observation.source_sequence < last:
                    return InstrumentStatusApplyResult(
                        False, ACTION_OUT_OF_ORDER, CODE_SEQUENCE_OUT_OF_ORDER
                    )
                if entry is not None and _same_observation(observation, entry.observation):
                    return InstrumentStatusApplyResult(False, ACTION_DUPLICATE, CODE_DUPLICATE)
                return InstrumentStatusApplyResult(
                    False, ACTION_SEQUENCE_CONFLICT, CODE_SEQUENCE_CONFLICT
                )
            self._entries[key] = _Entry(observation, STATE_CURRENT, None)
            self._last_sequence[key] = observation.source_sequence
            return InstrumentStatusApplyResult(True, ACTION_APPLIED)

    def mark_source_unavailable(self) -> None:
        """Mark the source unavailable and every observation stale."""

        with self._lock:
            self._source_available = False
            for key, entry in self._entries.items():
                self._entries[key] = _Entry(entry.observation, STATE_STALE, CODE_SOURCE_UNAVAILABLE)

    def view(self, venue: object, native_symbol: object) -> InstrumentStatusView:
        """Return the effective, fail-closed answer for one instrument."""

        if not _identifier(venue, _MAX_EXCHANGE_ID_LENGTH) or not _identifier(
            native_symbol, _MAX_INSTRUMENT_ID_LENGTH
        ):
            with self._lock:
                return InstrumentStatusView(
                    None,
                    None,
                    STATE_UNKNOWN,
                    STATUS_UNKNOWN,
                    None,
                    False,
                    CODE_FIELDS_INVALID,
                    None,
                    self._session_generation,
                    self._trading_day,
                )
        key = (venue, native_symbol)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return InstrumentStatusView(
                    venue,
                    native_symbol,
                    STATE_UNKNOWN,
                    STATUS_UNKNOWN,
                    None,
                    False,
                    CODE_NEVER_OBSERVED if self._source_available else CODE_SOURCE_UNAVAILABLE,
                    None,
                    self._session_generation,
                    self._trading_day,
                )
            if entry.state == STATE_CURRENT:
                observed = entry.observation.status_category
                if observed == STATUS_UNKNOWN:
                    return InstrumentStatusView(
                        venue,
                        native_symbol,
                        STATE_CURRENT,
                        STATUS_UNKNOWN,
                        observed,
                        False,
                        CODE_STATUS_UNKNOWN,
                        entry.observation,
                        self._session_generation,
                        self._trading_day,
                    )
                return InstrumentStatusView(
                    venue,
                    native_symbol,
                    STATE_CURRENT,
                    observed,
                    observed,
                    True,
                    None,
                    entry.observation,
                    self._session_generation,
                    self._trading_day,
                )
            return InstrumentStatusView(
                venue,
                native_symbol,
                STATE_STALE,
                STATUS_UNKNOWN,
                entry.observation.status_category,
                False,
                entry.stale_reason,
                entry.observation,
                self._session_generation,
                self._trading_day,
            )
