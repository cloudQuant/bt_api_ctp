"""Provider-neutral ServiceMarket adapter for native CTP quote-v2 ticks.

This module adapts the existing :class:`CtpMarketStream` callback to the
``MarketProvider`` port consumed by the service-v2 Gateway Market Hub.  It
does not import or route through the retired CTP gateway adapter.  A trusted
operator supplies a lazy ``stream_factory`` that closes over the configured
CTP session; the provider owns one instrument subscription and its bounded
callback queue.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Callable

from bt_api_base.market_protocol import (
    MarketProviderEvent,
    MarketSourceDescriptor,
    MarketSubscriptionKey,
)
from bt_api_base.market_recovery_observation import RecoveryObservationMember

from bt_api_ctp.ctp.client import CtpNativeStopReceipt
from bt_api_ctp.feeds.live_ctp_feed import (
    ConnectionState,
    CtpMarketStream,
    CtpMarketStreamSubscriptionError,
    CtpQuoteClockSourceScope,
    _get_ctp_managed_quote_v2_receipt,
)
from bt_api_ctp.market_key_contract import CtpMarketKeyError, validate_ctp_market_key
from bt_api_ctp.market_tick_projection import project_ctp_market_tick
from bt_api_ctp.md_identity import MdIdentityObservation

DEFAULT_CTP_QUOTE_QUEUE_CAPACITY = 1_024
MAX_CTP_QUOTE_QUEUE_CAPACITY = 65_536
MAX_CTP_PROVIDER_POLL_LIMIT = 256
# Symbols released by their last consumer while other symbols keep the shared
# MD session alive stay natively subscribed ("parked"), so that a consumer that
# comes back can be served on the same session (see ``_park_symbol``).
DEFAULT_CTP_PARKED_SYMBOLS = 128
MAX_CTP_PARKED_SYMBOLS = 1_024
MAX_CTP_QUOTE_EVENT_BYTES = 256 * 1024
MAX_CTP_CONNECT_TIMEOUT_SECONDS = 300.0
MAX_CTP_STOP_TIMEOUT_SECONDS = 30.0
_MAX_RECOVERY_SNAPSHOT_MEMBERS = 1024
_RECOVERY_MEMBER_SNAPSHOT_SEAL = object()

_logger = logging.getLogger(__name__)

# Recovery states in which the client being replaced has a confirmed stop and
# no recovery worker runs, so a channel can be released and the provider
# closed.  The attempt deadline bounds only the native stop and start; once a
# replacement was started ("connecting") the provider waits for the client's
# own reconnect and re-login (a front can stay closed for hours between
# sessions).  "retry_wait": a failed attempt's client is stopped and retained
# until the next attempt starts its replacement.
_RECOVERY_SETTLED_STATES = frozenset({"connecting", "reset_pending", "retry_wait"})

# Iteration 62, defect 25 (SimNow 7x24, 2026-11-07): one failed attempt made
# the loss of the MD source permanent.  A failed attempt that is not a
# contract violation now stops what it started and is retried after these
# delays (seconds; the last one repeats, with no attempt limit) while the
# provider stays open; consumers keep seeing "recovering" meanwhile.
_SOURCE_RECOVERY_RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)
# Failed attempts are logged when they start a series, then at most once per
# interval with the count of those not logged in between.
_SOURCE_RECOVERY_LOG_INTERVAL_SECONDS = 60.0


def _source_recovery_retry_delay(failed_attempts: int) -> float:
    """Backoff before the attempt that follows ``failed_attempts`` failures."""

    delays = _SOURCE_RECOVERY_RETRY_DELAYS_SECONDS
    return float(delays[min(max(int(failed_attempts), 1), len(delays)) - 1])


class _SourceRecoveryContractError(RuntimeError):
    """A recovery failure that no retry can fix (finite reason, no payload)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _SourceRecoveryRetryableError(RuntimeError):
    """A recovery failure the next attempt can fix (finite reason, no payload)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class CtpQuoteV2MarketProviderError(RuntimeError):
    """Finite, value-free failure from the CTP market provider boundary."""

    _CODES = frozenset(
        {
            "INVALID_CTP_MARKET_PROVIDER_CONFIG",
            "INVALID_CTP_MARKET_KEY",
            "UNSUPPORTED_CTP_MARKET_KEY",
            "CTP_MARKET_OWNER_THREAD_REQUIRED",
            "CTP_MARKET_OWNER_CLOSING",
            "CTP_MARKET_STARTUP_CLEANUP_PENDING",
            "CTP_MARKET_STREAM_FACTORY_FAILED",
            "CTP_MARKET_STREAM_INVALID",
            "CTP_MARKET_STREAM_NOT_FRESH",
            "CTP_MARKET_SUBSCRIBE_FAILED",
            "CTP_MARKET_SUBSCRIBE_UNACKNOWLEDGED",
            "CTP_MARKET_START_FAILED",
            "CTP_MARKET_AUTH_TIMEOUT",
            "CTP_MARKET_AUTH_FAILED",
            "CTP_MARKET_EVENT_INVALID",
            "CTP_MARKET_EVENT_KEY_MISMATCH",
            "CTP_MARKET_EVENT_RECEIPT_UNTRUSTED",
            "CTP_MARKET_SOURCE_SCOPE_CHANGED",
            "CTP_MARKET_FRESH_STREAM_REQUIRED",
            "CTP_MARKET_RETIRED_SYMBOL_LIMIT",
            "CTP_MARKET_SESSION_KEY_MISMATCH",
            "CTP_MARKET_UNSUBSCRIBE_FAILED",
            "CTP_MARKET_STOP_UNCONFIRMED",
            "CTP_MARKET_RECOVERY_IN_PROGRESS",
            "RECOVERY_OBSERVATION_DISABLED",
            "RECOVERY_OBSERVATION_UNAVAILABLE",
            "RECOVERY_OBSERVATION_INVALID",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = (
            code if type(code) is str and code in self._CODES else "CTP_MARKET_PROVIDER_FAILED"
        )
        super().__init__(self.code)


class BoundedCtpQuoteV2Queue:
    """Thread-safe, nonblocking sink for ``CtpMarketStream.push_data``.

    When full, the oldest entry is dropped.  That is a locally known message
    loss: :meth:`drain_marking_loss` reports it with the first entry drained
    after it, so the provider projects that entry as a ``gap`` (D62-38).
    """

    def __init__(self, capacity: int = DEFAULT_CTP_QUOTE_QUEUE_CAPACITY) -> None:
        if type(capacity) is not int or not 1 <= capacity <= MAX_CTP_QUOTE_QUEUE_CAPACITY:
            raise CtpQuoteV2MarketProviderError("INVALID_CTP_MARKET_PROVIDER_CONFIG")
        self._capacity = capacity
        self._items: deque[object] = deque()
        self._lock = threading.Lock()
        self._dropped_oldest = 0
        # Set by a drop; cleared when the next entry is drained or on clear().
        self._loss_pending = False

    @property
    def dropped_oldest(self) -> int:
        with self._lock:
            return self._dropped_oldest

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._items)

    def put(self, item: object, block: bool = True, timeout: float | None = None) -> None:
        """Append from a native callback without blocking that callback."""

        del block, timeout
        with self._lock:
            if len(self._items) == self._capacity:
                self._items.popleft()
                self._dropped_oldest += 1
                self._loss_pending = True
            self._items.append(item)

    def drain(self, limit: int) -> tuple[object, ...]:
        return self.drain_marking_loss(limit)[0]

    def drain_marking_loss(self, limit: int) -> tuple[tuple[object, ...], bool]:
        """Drain up to ``limit`` entries; ``True`` if an entry was dropped before the first.

        Drops only ever remove the oldest entry, so every drop since the last
        drain precedes the first entry returned now.
        """

        if type(limit) is not int or not 1 <= limit <= MAX_CTP_PROVIDER_POLL_LIMIT:
            raise CtpQuoteV2MarketProviderError("INVALID_CTP_MARKET_PROVIDER_CONFIG")
        with self._lock:
            count = min(limit, len(self._items))
            items = tuple(self._items.popleft() for _ in range(count))
            loss = self._loss_pending and count > 0
            if loss:
                self._loss_pending = False
            return items, loss

    def clear(self) -> None:
        # Everything queued is discarded with its scope (recovery or a failed
        # subscription); the next scope starts without a pending loss.
        with self._lock:
            self._items.clear()
            self._loss_pending = False


class _CtpQuoteV2Fanout:
    """Route one stream callback queue to exact per-instrument queues."""

    def __init__(self) -> None:
        self._queues: dict[str, BoundedCtpQuoteV2Queue] = {}
        self._lock = threading.Lock()

    def register(self, symbol: str, target: BoundedCtpQuoteV2Queue) -> None:
        with self._lock:
            if symbol in self._queues:
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_SESSION_KEY_MISMATCH")
            self._queues[symbol] = target

    def unregister(self, symbol: str, target: BoundedCtpQuoteV2Queue) -> bool:
        with self._lock:
            if self._queues.get(symbol) is not target:
                return False
            self._queues.pop(symbol, None)
            return True

    def put(self, item: object, block: bool = True, timeout: float | None = None) -> None:
        del block, timeout
        try:
            symbol = item.get_symbol_name()
        except Exception:
            return
        if type(symbol) is not str:
            return
        with self._lock:
            target = self._queues.get(symbol)
        if target is not None:
            target.put(item, block=False)


@dataclass(frozen=True, eq=False)
class CtpQuoteV2MarketHandle:
    """Opaque identity for one subscription; native resources remain private."""

    key: MarketSubscriptionKey
    generation: str


@dataclass(frozen=True, eq=False)
class CtpRecoveryMemberSnapshot:
    """Owner-local point observation, without coverage or future authority.

    This does not prove cross-TD/MD identity, a later BEGIN, historical
    ProductID, or continuity after capture. Private references are only local
    identity fences and this object has no serialization contract.
    ``md_generation`` is the stream source scope's connection generation,
    not a TD generation or an authorization fact.
    """

    source_epoch: str
    sequence: int
    md_generation: int
    members: tuple[RecoveryObservationMember, ...]
    _provider: object = dataclass_field(repr=False)
    _owner_pid: int = dataclass_field(repr=False)
    _owner_thread: object = dataclass_field(repr=False)
    _buffer: object = dataclass_field(repr=False)
    _stream: object = dataclass_field(repr=False)
    _client: object = dataclass_field(repr=False)
    _md_identity: object = dataclass_field(repr=False)
    _scope: object = dataclass_field(repr=False)
    _revision: int = dataclass_field(repr=False)
    _states: tuple = dataclass_field(repr=False)
    _seal: object = dataclass_field(repr=False)

    def __post_init__(self) -> None:
        if self._seal is not _RECOVERY_MEMBER_SNAPSHOT_SEAL:
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_INVALID")

    def __copy__(self):
        raise TypeError("recovery member snapshots cannot be copied")

    def __deepcopy__(self, _memo):
        raise TypeError("recovery member snapshots cannot be copied")

    def __reduce_ex__(self, _protocol):
        raise TypeError("recovery member snapshots cannot be serialized")


@dataclass(frozen=True, eq=False)
class _RecoveryMemberIssuance:
    """Private issuance values and identity references, independent of the DTO."""

    source_epoch: str
    sequence: int
    md_generation: int
    members: tuple
    provider: object
    owner_pid: int
    owner_thread: object
    buffer: object
    stream: object
    client: object
    md_identity: object
    md_identity_values: tuple
    scope: object
    scope_values: tuple
    revision: int
    states: tuple


def _md_identity_signature(identity: MdIdentityObservation) -> tuple:
    """Freeze all native login observation fields independently of its object."""
    if type(identity) is not MdIdentityObservation:
        raise ValueError("invalid MD identity")
    values = (identity.front, identity.broker_id, identity.user_id,
              identity.connection_generation, identity.request_id,
              identity.trading_day, identity.authenticated)
    MdIdentityObservation(*values)
    return tuple((type(value), value) for value in values)


def _recovery_scope_signature(scope: CtpQuoteClockSourceScope) -> tuple:
    """Freeze the public source identity without copying private capabilities."""
    if type(scope) is not CtpQuoteClockSourceScope:
        raise ValueError("invalid MD source scope")
    values = (scope.source, scope.clock_domain_id, scope.clock_source_session_id,
              scope.clock_source, scope.connection_generation)
    if any(type(value) is not str for value in values[:4]) or type(values[4]) is not int:
        raise ValueError("invalid MD source scope")
    return values


def _recovery_member_signature(members: tuple) -> tuple:
    """Copy and validate every member dimension into immutable builtins."""
    if type(members) is not tuple or not 1 <= len(members) <= _MAX_RECOVERY_SNAPSHOT_MEMBERS:
        raise ValueError("invalid recovery members")
    result = []
    for member in members:
        if type(member) is not RecoveryObservationMember or type(member.key) is not MarketSubscriptionKey:
            raise ValueError("invalid recovery member")
        key = MarketSubscriptionKey.from_dict(member.key.to_dict())
        RecoveryObservationMember(key, member.generation)
        instrument = key.instrument
        result.append((key.source_ref, key.entitlement_ref, instrument.provider,
                       instrument.venue, instrument.environment, instrument.product_type,
                       instrument.instrument_id, instrument.native_symbol, key.channel,
                       key.depth, key.timeframe, member.generation))
    return tuple(result)


@dataclass
class _HandleState:
    key: MarketSubscriptionKey
    generation: str
    public_handle: CtpQuoteV2MarketHandle | None
    queue: BoundedCtpQuoteV2Queue
    stream: CtpMarketStream
    source_scope: CtpQuoteClockSourceScope
    active: bool = True
    source_epoch_reset_pending: bool = False
    source_epoch_reset_epoch: str | None = None
    last_hub_epoch: str | None = None
    # Source facts for recently polled native events only.  A Hub must still
    # match the exact successfully published frame before requesting one.
    clock_events: deque = dataclass_field(default_factory=lambda: deque(maxlen=256), repr=False)


@dataclass(frozen=True)
class _InactiveHandle:
    """Identity-only tombstone: it cannot retain a stopped native session."""

    active: bool = False


_INACTIVE_HANDLE = _InactiveHandle()


@dataclass
class _PendingStreamCleanup:
    stream: CtpMarketStream
    native_client: object | None
    native_stop_confirmed: bool


@dataclass
class _SourceRecovery:
    """One attempt to replace a lost MD source (a retry is a new attempt).

    ``old_client`` is the client this attempt stops and replaces: the lost one
    for attempt 1, afterwards the failed replacement (or, when none was bound,
    the previous attempt's origin).  A retry learns ``old_client_generation``
    from that client's complete stop receipt; attempt 1 knows it beforehand.
    """

    stream: CtpMarketStream
    old_client: object
    old_client_generation: int | None
    old_scope: CtpQuoteClockSourceScope
    old_stream_generation: int
    symbols: frozenset[str]
    deadline: float
    state: str
    new_scope: CtpQuoteClockSourceScope | None = None
    stop_receipt_attempts: int = 0
    stop_worker: threading.Thread | None = None
    stop_done: threading.Event = dataclass_field(default_factory=threading.Event)
    stop_receipt: CtpNativeStopReceipt | None = None
    stop_error: BaseException | None = None
    stop_timed_out: bool = False
    reconnect_worker: threading.Thread | None = None
    reconnect_done: threading.Event = dataclass_field(default_factory=threading.Event)
    reconnect_succeeded: bool | None = None
    reconnect_error: BaseException | None = None
    reconnected_client: object | None = None
    reconnect_cancelled: threading.Event = dataclass_field(default_factory=threading.Event)
    # Finite reason of a reconnect that returned False or raised.
    reconnect_failure: str | None = None
    # The start returned after its deadline or cancellation and was fenced.
    reconnect_fenced: bool = False
    attempt: int = 1
    retry: bool = False
    # A retry starts its replacement no earlier than this (monotonic).
    retry_at: float = 0.0
    failure: str | None = None
    failed_state: str | None = None


class CtpQuoteV2MarketProvider:
    """Adapt one native CTP quote-v2 subscription to the Gateway provider port.

    ``stream_factory`` is trusted deployment code. It must construct a fresh,
    disconnected ``CtpMarketStream`` for the exact key and queue without
    starting it or pre-registering topics. Credentials, fronts, and any
    provider-specific diagnostic metadata remain inside that closure. The
    provider registers the sole native tick topic itself, then
    waits for the existing native login callback to issue its opaque source
    scope before returning the acquired handle.

    ``source_ref`` and ``entitlement_ref`` are explicit trusted profile
    bindings. They are compared exactly with every key; the provider invents
    neither authorization value nor account identity.

    A symbol successfully unsubscribed from a live native client cannot be
    safely re-added to that same client because its callbacks carry no
    subscription token. Re-adding it requires a confirmed full provider close
    and a fresh stream; other active symbol handles remain usable meanwhile.
    If the bounded retired-symbol ledger fills, new symbols are fenced for that
    client while existing handles can still be released normally.
    """

    def __init__(
        self,
        *,
        stream_factory: Callable[[MarketSubscriptionKey, BoundedCtpQuoteV2Queue], object],
        source_ref: str,
        entitlement_ref: str,
        queue_capacity: int = DEFAULT_CTP_QUOTE_QUEUE_CAPACITY,
        connect_timeout_seconds: float = 30.0,
        stop_timeout_seconds: float = 5.0,
        max_parked_symbols: int = DEFAULT_CTP_PARKED_SYMBOLS,
        recovery_observation_capacity: int = 0,
    ) -> None:
        if (
            not callable(stream_factory)
            or not _valid_ref(source_ref)
            or not _valid_ref(entitlement_ref)
            or type(queue_capacity) is not int
            or not 1 <= queue_capacity <= MAX_CTP_QUOTE_QUEUE_CAPACITY
            or type(max_parked_symbols) is not int
            or not 0 <= max_parked_symbols <= MAX_CTP_PARKED_SYMBOLS
            or type(recovery_observation_capacity) is not int
            or not (recovery_observation_capacity == 0 or 2 <= recovery_observation_capacity <= 4096)
            or not _valid_timeout(connect_timeout_seconds, MAX_CTP_CONNECT_TIMEOUT_SECONDS)
            or not _valid_timeout(stop_timeout_seconds, MAX_CTP_STOP_TIMEOUT_SECONDS)
        ):
            raise CtpQuoteV2MarketProviderError("INVALID_CTP_MARKET_PROVIDER_CONFIG")
        self._stream_factory = stream_factory
        self._source_ref = source_ref
        self._entitlement_ref = entitlement_ref
        self._queue_capacity = queue_capacity
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._stop_timeout_seconds = float(stop_timeout_seconds)
        self._owner_thread_id = threading.get_ident()
        self._owner_thread = threading.current_thread()
        self._owner_pid = os.getpid()
        self._close_only = False
        self._known_handles: weakref.WeakKeyDictionary[
            CtpQuoteV2MarketHandle, _HandleState | _InactiveHandle
        ] = weakref.WeakKeyDictionary()
        self._active_handles: dict[int, _HandleState] = {}
        self._handles_by_symbol: dict[str, _HandleState] = {}
        # Native symbol -> native topic of a subscription that no handle
        # consumes any more but that is kept on the current shared session.
        self._max_parked_symbols = max_parked_symbols
        self._parked_symbols: dict[str, dict[str, str]] = {}
        self._stream: CtpMarketStream | None = None
        self._fanout: _CtpQuoteV2Fanout | None = None
        self._native_client: object | None = None
        self._source_scope: CtpQuoteClockSourceScope | None = None
        self._session_environment: str | None = None
        self._pending_start_cleanup: list[_PendingStreamCleanup] = []
        self._invalid_events = 0
        self._source_recovery: _SourceRecovery | None = None
        self._recovery_member_revision = 0
        self._recovery_member_snapshots: weakref.WeakKeyDictionary[
            CtpRecoveryMemberSnapshot, _RecoveryMemberIssuance
        ] = weakref.WeakKeyDictionary()
        self._recovery_observation_enabled = recovery_observation_capacity != 0
        self._recovery_observation_unavailable = False
        self._recovery_observation = None
        if self._recovery_observation_enabled:
            try:
                from bt_api_ctp.recovery_observation import RecoveryObservationBuffer

                self._recovery_observation = RecoveryObservationBuffer(recovery_observation_capacity)
            except Exception:
                self._recovery_observation_unavailable = True
        # Throttle of the failed-attempt log lines of one recovery.
        self._recovery_retry_logged_at: float | None = None
        self._recovery_retry_suppressed = 0

    @property
    def diagnostics(self) -> dict[str, int]:
        self._check_owner()
        return {
            "pending_start_cleanup": len(self._pending_start_cleanup),
            "active_handles": len(self._active_handles),
            "active_sessions": int(self._stream is not None),
            "parked_symbols": len(self._parked_symbols),
            "source_recovery_active": int(self._source_recovery is not None),
            "invalid_events": self._invalid_events,
            "dropped_oldest": sum(
                handle.queue.dropped_oldest for handle in self._active_handles.values()
            ),
        }

    def _observe_recovery(self, operation: str, *args: object) -> None:
        if self._recovery_observation is None or self._recovery_observation_unavailable:
            return
        try:
            getattr(self._recovery_observation, operation)(*args)
        except Exception:
            self._recovery_observation_unavailable = True

    def read_recovery_observations(self, *, after_sequence: int, limit: int = 100):
        """Read local owner observations; monotonic values are process-local only."""
        self._check_owner(allow_closing=True)
        if not self._recovery_observation_enabled:
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_DISABLED")
        if self._recovery_observation_unavailable or self._recovery_observation is None:
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_UNAVAILABLE")
        from bt_api_ctp.recovery_observation import RecoveryObservationUnavailable

        try:
            if self._source_recovery is not None and self._source_recovery.stream is not self._stream:
                self._observe_recovery("invalidate", "SCOPE_CHANGED", self._source_recovery)
                if self._recovery_observation_unavailable:
                    raise RecoveryObservationUnavailable
            return self._recovery_observation.read(
                after_sequence, limit, self._source_recovery, tuple(self._active_handles.values())
            )
        except ValueError:
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_INVALID") from None
        except RecoveryObservationUnavailable:
            self._recovery_observation_unavailable = True
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_UNAVAILABLE") from None
        except Exception:
            self._recovery_observation_unavailable = True
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_UNAVAILABLE") from None

    def _recovery_member_inputs_locked(self, stream: CtpMarketStream, buffer: object) -> tuple:
        """Capture only healthy owner-local membership under the MD callback lock."""
        from bt_api_ctp.recovery_observation import RecoveryObservationBuffer

        epoch = getattr(buffer, "_source_epoch", None)
        sequence = getattr(buffer, "_sequence", None)
        scope = self._source_scope
        client = self._native_client
        identity = getattr(client, "active_md_identity", None)
        if not 1 <= len(self._active_handles) <= _MAX_RECOVERY_SNAPSHOT_MEMBERS:
            raise ValueError("recovery members unavailable")
        states = tuple(self._active_handles.values())
        fanout = self._fanout
        symbols = self._session_symbols()
        if (
            self._close_only
            or self._stream is not stream
            or self._recovery_observation_enabled is not True
            or self._recovery_observation is not buffer
            or type(buffer) is not RecoveryObservationBuffer
            or self._recovery_observation_unavailable
            or buffer._poisoned is not False
            or buffer._episode is not None
            or buffer._pid != self._owner_pid
            or type(epoch) is not str
            or len(epoch) != 32
            or any(char not in "0123456789abcdef" for char in epoch)
            or type(sequence) is not int
            or not 0 <= sequence <= (1 << 63) - 1
            or self._source_recovery is not None
            or type(scope) is not CtpQuoteClockSourceScope
            or client is None
            or type(identity) is not MdIdentityObservation
            or identity.authenticated is not True
            or identity.connection_generation != scope._client_generation
            or stream._md_client is not client
            or scope._client is not client
            or not stream.quote_v2_service_binding_matches()
            or self._current_source_scope(stream) is not scope
            or type(scope._client_generation) is not int
            or scope._client_generation <= 0
            or len(self._handles_by_symbol) != len(states)
            or set(self._handles_by_symbol).intersection(self._parked_symbols)
            or type(stream._quote_v2_subscription_metadata) is not dict
            or set(stream._quote_v2_subscription_metadata) != set(symbols)
            or stream._quote_v2_service_required_symbols != set(symbols)
            or type(fanout) is not _CtpQuoteV2Fanout
        ):
            raise ValueError("recovery members unavailable")
        pairs = []
        for state in states:
            public = state.public_handle
            if (
                type(state) is not _HandleState
                or type(public) is not CtpQuoteV2MarketHandle
                or state.active is not True
                or state.source_epoch_reset_pending is not False
                or self._active_handles.get(id(public)) is not state
                or self._known_handles.get(public) is not state
                or self._handles_by_symbol.get(state.key.instrument.native_symbol) is not state
                or state.stream is not stream
                or state.source_scope is not scope
                or type(state.key) is not MarketSubscriptionKey
                or state.key.instrument.environment != self._session_environment
                or public.key is not state.key
                or public.generation != state.generation
                or state.key.instrument.native_symbol not in stream._quote_v2_service_acknowledged_symbols
                or stream._quote_v2_service_symbol_request_ids.get(state.key.instrument.native_symbol)
                not in stream._quote_v2_service_completed_request_ids
            ):
                raise ValueError("recovery members unavailable")
            self._validate_key(state.key)
            detached_key = MarketSubscriptionKey.from_dict(state.key.to_dict())
            member = RecoveryObservationMember(detached_key, state.generation)
            pairs.append((member, state, public))
        pairs.sort(key=lambda item: (item[0].key.stream_id, item[0].generation))
        ordering = tuple((item[0].key.stream_id, item[0].generation) for item in pairs)
        if len(set(ordering)) != len(ordering):
            raise ValueError("recovery members unavailable")
        with fanout._lock:
            if (set(fanout._queues) != set(self._handles_by_symbol)
                    or any(fanout._queues.get(state.key.instrument.native_symbol) is not state.queue
                           for _, state, _ in pairs)):
                raise ValueError("recovery members unavailable")
        if (getattr(client, "active_md_identity", None) is not identity
                or not stream.quote_v2_service_binding_matches()):
            raise ValueError("recovery members unavailable")
        return (
            epoch, sequence, scope.connection_generation, tuple(item[0] for item in pairs),
            tuple((item[1], item[2]) for item in pairs), self._recovery_member_revision,
            client, identity, scope, _md_identity_signature(identity),
            _recovery_scope_signature(scope),
        )

    @staticmethod
    def _recovery_member_inputs_match(issued: _RecoveryMemberIssuance, inputs: tuple) -> bool:
        (epoch, sequence, generation, members, states, revision, client, identity, scope,
         identity_values, scope_values) = inputs
        return bool(
            issued.source_epoch == epoch
            and issued.sequence == sequence
            and issued.md_generation == generation
            and issued.members == _recovery_member_signature(members)
            and issued.revision == revision
            and issued.client is client
            and issued.md_identity is identity
            and issued.md_identity_values == identity_values
            and issued.md_identity_values == _md_identity_signature(identity)
            and issued.scope is scope
            and issued.scope_values == scope_values
            and issued.scope_values == _recovery_scope_signature(scope)
            and len(issued.states) == len(states)
            and all(old_state is state and old_handle is handle
                    for (old_state, old_handle), (state, handle) in zip(issued.states, states))
        )

    @staticmethod
    def _recovery_member_snapshot_matches(snapshot: CtpRecoveryMemberSnapshot,
                                          issued: _RecoveryMemberIssuance) -> bool:
        return bool(
            type(snapshot.source_epoch) is str and snapshot.source_epoch == issued.source_epoch
            and type(snapshot.sequence) is int and snapshot.sequence == issued.sequence
            and type(snapshot.md_generation) is int and snapshot.md_generation == issued.md_generation
            and _recovery_member_signature(snapshot.members) == issued.members
            and snapshot._provider is issued.provider
            and type(snapshot._owner_pid) is int and snapshot._owner_pid == issued.owner_pid
            and snapshot._owner_thread is issued.owner_thread
            and snapshot._buffer is issued.buffer and snapshot._stream is issued.stream
            and snapshot._client is issued.client and snapshot._md_identity is issued.md_identity
            and _md_identity_signature(snapshot._md_identity) == issued.md_identity_values
            and snapshot._scope is issued.scope
            and _recovery_scope_signature(snapshot._scope) == issued.scope_values
            and type(snapshot._revision) is int and snapshot._revision == issued.revision
            and snapshot._seal is _RECOVERY_MEMBER_SNAPSHOT_SEAL
            and type(snapshot._states) is tuple and len(snapshot._states) == len(issued.states)
            and all(type(pair) is tuple and len(pair) == 2
                    and pair[0] is old[0] and pair[1] is old[1]
                    for pair, old in zip(snapshot._states, issued.states))
        )

    def capture_recovery_member_snapshot(self) -> CtpRecoveryMemberSnapshot:
        """Capture current local members without emitting recovery observations.

        Only active handles are members; parked native symbols are excluded.
        This point observation does not establish coverage, cross-TD/MD
        identity, a future BEGIN, historical ProductID, or continuing validity.
        """
        self._check_owner()
        if not self._recovery_observation_enabled:
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_DISABLED")
        stream, buffer = self._stream, self._recovery_observation
        if type(stream) is not CtpMarketStream or buffer is None:
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_UNAVAILABLE")
        try:
            with stream._md_callback_lock:
                inputs = self._recovery_member_inputs_locked(stream, buffer)
                (epoch, sequence, generation, members, states, revision, client, identity, scope,
                 identity_values, scope_values) = inputs
                issued = _RecoveryMemberIssuance(
                    epoch, sequence, generation, _recovery_member_signature(members), self,
                    self._owner_pid, self._owner_thread, buffer, stream, client, identity,
                    identity_values, scope, scope_values, revision, states,
                )
                snapshot = CtpRecoveryMemberSnapshot(
                    epoch, sequence, generation, members, self, self._owner_pid,
                    self._owner_thread, buffer, stream, client, identity, scope, revision, states,
                    _RECOVERY_MEMBER_SNAPSHOT_SEAL,
                )
                if not self._recovery_member_inputs_match(
                    issued, self._recovery_member_inputs_locked(stream, buffer)
                ):
                    raise ValueError("recovery members changed")
                if not self._recovery_member_snapshot_matches(snapshot, issued):
                    raise ValueError("recovery members changed")
                self._recovery_member_snapshots[snapshot] = issued
                return snapshot
        except Exception:
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_UNAVAILABLE") from None

    def assert_recovery_member_snapshot_current(self, snapshot: object) -> None:
        """Recheck one originally issued point snapshot; no source I/O occurs."""
        self._check_owner()
        if type(snapshot) is not CtpRecoveryMemberSnapshot:
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_INVALID")
        issued = self._recovery_member_snapshots.get(snapshot)
        try:
            valid = (issued is not None and issued.provider is self
                     and issued.owner_pid == self._owner_pid
                     and issued.owner_thread is self._owner_thread
                     and self._recovery_member_snapshot_matches(snapshot, issued))
        except Exception:
            valid = False
        if not valid:
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_INVALID")
        stream, buffer = issued.stream, issued.buffer
        if type(stream) is not CtpMarketStream:
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_UNAVAILABLE")
        try:
            with stream._md_callback_lock:
                if not self._recovery_member_inputs_match(
                    issued, self._recovery_member_inputs_locked(stream, buffer)
                ) or not self._recovery_member_snapshot_matches(snapshot, issued):
                    raise ValueError("recovery members changed")
        except Exception:
            raise CtpQuoteV2MarketProviderError("RECOVERY_OBSERVATION_UNAVAILABLE") from None

    def subscribe(self, key: MarketSubscriptionKey, *, generation: str) -> CtpQuoteV2MarketHandle:
        """Acquire one key; a typed refusal says that no handle exists for it.

        Every ``CtpQuoteV2MarketProviderError`` raised here carries
        ``subscription_refused = True``: the handle is registered last on each
        path, so a refused key holds none, and whatever a failed native start
        or add left behind stays in this provider's own custody (pending start
        cleanup, or a session failed closed until its existing handles stop
        it). A caller may therefore drop the key at once instead of retaining
        an acquisition whose release it could never confirm. In the Gateway
        that retained acquisition kept the whole market plane not ready for
        every strategy (SimNow 2026-11-10).
        """
        try:
            return self._subscribe(key, generation)
        except CtpQuoteV2MarketProviderError as error:
            error.subscription_refused = True
            raise

    def _subscribe(self, key: MarketSubscriptionKey, generation: str) -> CtpQuoteV2MarketHandle:
        self._check_owner()
        self._validate_key(key)
        if type(generation) is not str or not generation or generation != generation.strip():
            raise CtpQuoteV2MarketProviderError("INVALID_CTP_MARKET_KEY")
        if self._source_recovery is not None:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_RECOVERY_IN_PROGRESS")
        self._retry_pending_start_cleanup()
        symbol = key.instrument.native_symbol
        existing = self._handles_by_symbol.get(symbol)
        if existing is not None:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SESSION_KEY_MISMATCH")
        if self._stream is None:
            return self._start_shared_stream(key, generation)
        if symbol in self._parked_symbols:
            return self._resume_parked_symbol(key, generation)
        return self._subscribe_existing_stream(key, generation)

    def unsubscribe(self, handle: object) -> bool:
        self._check_owner()
        handle = self._state_for_handle(handle)
        if handle is None:
            return False
        if not handle.active:
            return True
        recovery = self._source_recovery
        if recovery is not None:
            # While the native stop or start is in flight the recovery owns
            # the clients.  Afterwards (waiting for the replacement's login,
            # for a new consumer epoch, or failed) a channel can be released:
            # the last one closes the session, another one leaves the
            # recovery's symbol set.
            if not self._recovery_old_stop_settled(recovery):
                return False
            if len(self._active_handles) <= 1:
                return self.close()
            return self._unsubscribe_during_recovery(handle, recovery)
        if len(self._active_handles) > 1:
            if self._park_symbol(handle):
                return True
            symbol = handle.key.instrument.native_symbol
            try:
                handle.stream.unsubscribe_topics([{"topic": "tick", "symbol": symbol}])
            except CtpMarketStreamSubscriptionError as exc:
                if exc.code == "CTP_MD_RETIRED_SUBSCRIPTION_LIMIT":
                    raise CtpQuoteV2MarketProviderError("CTP_MARKET_RETIRED_SYMBOL_LIMIT") from None
                return False
            except Exception:
                return False
            fanout = self._fanout
            if fanout is None or not fanout.unregister(
                handle.key.instrument.native_symbol, handle.queue
            ):
                return False
            self._remove_handle(handle)
            return True

        stopped, native_stop_confirmed = self._stop_stream(
            handle.stream,
            self._native_client or getattr(handle.stream, "_md_client", None),
            False,
        )
        if not stopped:
            return False
        self._remove_handle(handle)
        self._reset_active_stream()
        return native_stop_confirmed

    def close(self) -> bool:
        """Stop the exact shared native session before discarding ownership.

        A full owner close is the recovery boundary for retired-symbol
        tombstones and bypasses per-symbol unsubscribe limits. On an
        unconfirmed native stop, every handle and the stream remain retained
        for a later owner-thread retry.
        """

        self._check_owner(allow_closing=True)
        self._recovery_member_revision += 1
        self._observe_recovery("invalidate", "CLOSED", self._source_recovery)
        stream = self._stream
        recovery = self._source_recovery
        recovery_stop_confirmed = False
        if recovery is not None:
            if not self._recovery_old_stop_settled(recovery):
                # Until the exact old stop has completed, recovery owns its
                # client and API. A failed reconnect may close only after the
                # worker is done and that old stop receipt is fully confirmed.
                return False
            recovery_stop_confirmed = True
        if stream is not None:
            current_client = getattr(stream, "_md_client", None)
            retained_client = self._native_client
            if current_client is not None:
                native_client = current_client
                native_stop_confirmed = False
            elif (
                recovery is not None
                and retained_client is not None
                and retained_client is not recovery.old_client
            ):
                # A replacement client may have failed readiness. Close that
                # exact client normally while retaining the old confirmed stop.
                native_client = retained_client
                native_stop_confirmed = False
            elif recovery is not None:
                native_client = recovery.old_client
                native_stop_confirmed = recovery_stop_confirmed
            else:
                native_client = retained_client or current_client
                native_stop_confirmed = False
            if (
                recovery is not None
                and recovery_stop_confirmed
                and native_client is recovery.old_client
                and current_client is None
                and stream.state is ConnectionState.ERROR
            ):
                normalize_failed_reconnect = getattr(
                    stream,
                    "normalize_quote_v2_reconnect_construction_failure",
                    None,
                )
                if (
                    not callable(normalize_failed_reconnect)
                    or normalize_failed_reconnect(
                        recovery.old_client,
                        recovery.stop_receipt,
                    )
                    is not True
                ):
                    return False
            stopped, _native_stop_confirmed = self._stop_stream(
                stream, native_client, native_stop_confirmed
            )
            if not stopped:
                return False
            for handle in tuple(self._active_handles.values()):
                self._remove_handle(handle)
            self._active_handles.clear()
            self._handles_by_symbol.clear()
            self._reset_active_stream()
        try:
            self._retry_pending_start_cleanup()
        except CtpQuoteV2MarketProviderError:
            return False
        return not self._active_handles and not self._pending_start_cleanup and self._stream is None

    def _recovery_old_stop_settled(self, recovery: _SourceRecovery) -> bool:
        """The lost client's stop is confirmed and no recovery worker runs.

        True only after the replacement was started (``connecting``,
        ``reset_pending``), while a retry waits (``retry_wait``) or once the
        recovery failed, for this exact stream.
        """

        worker = recovery.stop_worker
        reconnect_worker = recovery.reconnect_worker
        for done, thread in (
            (recovery.stop_done, worker),
            (recovery.reconnect_done, reconnect_worker),
        ):
            # A worker that reported done is only leaving its thread.
            if thread is not None and done.is_set() and thread.is_alive():
                thread.join(timeout=0.5)
        receipt = recovery.stop_receipt
        return not (
            (recovery.state != "failed" and recovery.state not in _RECOVERY_SETTLED_STATES)
            or not recovery.stop_done.is_set()
            or (worker is not None and worker.is_alive())
            or (
                reconnect_worker is not None
                and (
                    not recovery.reconnect_done.is_set()
                    or reconnect_worker.is_alive()
                )
            )
            or recovery.stop_error is not None
            or recovery.stop_timed_out
            or type(receipt) is not CtpNativeStopReceipt
            or receipt.complete is not True
            or type(receipt.connection_generation) is not int
            or receipt.connection_generation != recovery.old_client_generation
            or recovery.stream is not self._stream
        )

    def _unsubscribe_during_recovery(
        self, handle: _HandleState, recovery: _SourceRecovery
    ) -> bool:
        """Release one of several channels once the old stop is settled."""

        stream = recovery.stream
        symbol = handle.key.instrument.native_symbol
        # A parked symbol stays in the recovery's symbol set: the replacement
        # session subscribes it again like the consumed ones.
        if not self._park_symbol(handle):
            try:
                # On the replacement client (or with no client, metadata only).
                stream.unsubscribe_topics([{"topic": "tick", "symbol": symbol}])
            except CtpMarketStreamSubscriptionError as exc:
                if exc.code == "CTP_MD_RETIRED_SUBSCRIPTION_LIMIT":
                    raise CtpQuoteV2MarketProviderError(
                        "CTP_MARKET_RETIRED_SYMBOL_LIMIT"
                    ) from None
                return False
            except Exception:
                return False
            fanout = self._fanout
            if fanout is None or not fanout.unregister(symbol, handle.queue):
                return False
            self._remove_handle(handle)
            recovery.symbols = frozenset(recovery.symbols - {symbol})
        with stream._md_callback_lock:
            if self._recovery_completion_members_ready(recovery):
                self._complete_source_recovery_if_acknowledged(recovery)
        return True

    def poll(self, handle: object, *, limit: int) -> tuple[MarketProviderEvent, ...]:
        self._check_owner()
        if type(limit) is not int or not 1 <= limit <= MAX_CTP_PROVIDER_POLL_LIMIT:
            raise CtpQuoteV2MarketProviderError("INVALID_CTP_MARKET_PROVIDER_CONFIG")
        handle = self._state_for_handle(handle)
        if handle is None or not handle.active:
            return ()
        if not self._retained_source_ready(handle):
            return ()
        raw_items, loss_before_first = handle.queue.drain_marking_loss(limit)
        events: list[MarketProviderEvent] = []
        for index, item in enumerate(raw_items):
            try:
                events.append(
                    _project_native_tick(
                        item, handle, local_message_loss=loss_before_first and index == 0
                    )
                )
            except Exception:
                self._invalid_events += 1
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_EVENT_INVALID") from None
        for event in events:
            fields = ("provider_sequence", "provider_subscription_epoch",
                      "event_time_provenance", "trading_date", "provider_connection_generation",
                      "provider_event_time_utc", "received_at_utc", "received_monotonic_ns")
            handle.clock_events.append((weakref.ref(handle.source_scope),
                                        {name: event.data.get(name) for name in fields}))
        return tuple(events)

    def retained_events_ready(self, handle: object) -> bool:
        """Check the current source without draining or acknowledging queued events.

        This is an owner-thread observation; the source may change after it
        returns and it does not make any later network delivery atomic.
        """
        self._check_owner()
        state = self._state_for_handle(handle)
        if state is None or not state.active:
            return False
        return self._retained_source_ready(state)

    def _retained_source_ready(self, handle: _HandleState) -> bool:
        stream = handle.stream
        if self._source_recovery is not None:
            self._drive_source_recovery()
            return False
        if stream.quote_v2_service_recovery_required:
            self._begin_source_recovery(stream)
            if self._source_recovery is not None:
                self._drive_source_recovery()
            return False
        if stream.quote_v2_service_fail_closed:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SOURCE_SCOPE_CHANGED")
        current_scope = self._current_source_scope(stream)
        if current_scope is None:
            return False
        if current_scope is not handle.source_scope:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SOURCE_SCOPE_CHANGED")
        if (
            stream.is_quote_v2_service_symbol_ready(handle.key.instrument.native_symbol) is not True
            or handle.source_epoch_reset_pending
        ):
            return False
        return True

    def describe_source(
        self,
        handle: object,
        *,
        key: MarketSubscriptionKey,
        generation: str,
        stream_id: str,
        epoch: str,
    ) -> MarketSourceDescriptor | None:
        self._check_owner()
        handle = self._state_for_handle(handle)
        if (
            handle is None
            or not handle.active
            or handle.key != key
            or handle.generation != generation
            or type(stream_id) is not str
            or stream_id != key.stream_id
            or type(epoch) is not str
            or not epoch
        ):
            return None
        if self._source_recovery is not None or handle.source_epoch_reset_pending:
            return None
        scope = self._current_source_scope(handle.stream)
        if scope is None or scope is not handle.source_scope:
            return None
        if handle.stream.is_quote_v2_service_symbol_ready(key.instrument.native_symbol) is not True:
            return None
        try:
            descriptor = MarketSourceDescriptor(
                key=key,
                stream_id=stream_id,
                epoch=epoch,
                provider=key.instrument.provider,
                source=scope.source,
                connection_generation=scope.connection_generation,
                clock_domain_id=scope.clock_domain_id,
                clock_source_session_id=scope.clock_source_session_id,
                clock_source=scope.clock_source,
            )
            handle.last_hub_epoch = epoch
            return descriptor
        except Exception:
            return None

    def source_epoch_reset_required(self, handle: object) -> bool:
        """Typed optional Gateway hook for a confirmed provider source restart."""

        self._check_owner()
        handle = self._state_for_handle(handle)
        if handle is None or not handle.active:
            return False
        recovery = self._source_recovery
        return bool(
            recovery is not None
            and recovery.state == "reset_pending"
            and handle.source_scope is recovery.new_scope
            and handle.source_epoch_reset_pending is True
            and self._current_source_scope(handle.stream) is recovery.new_scope
        )

    def source_recovery_state(self, handle: object) -> str | None:
        """``"recovering"`` or ``"failed"`` while this handle's lost source is replaced.

        Typed optional hook for a direct consumer (iteration 62, D62-38 point 7:
        DELAYED during a disconnect or a recovery).  From a confirmed source loss
        until the replacement session is described the handle delivers nothing,
        so a consumer must not keep reporting the channel as live meanwhile.
        ``None`` when no recovery is in progress, and once the replacement awaits
        its new consumer epoch (see :meth:`source_epoch_reset_required`).
        A failed attempt that is not a contract violation is retried with a
        bounded backoff while the provider is open (iteration 62, defect 25),
        so this stays ``"recovering"`` through every retry; ``"failed"`` is
        reserved for a contract violation and is final until the close.
        Owner-thread only; never raises for a foreign or inactive handle.
        """

        self._check_owner()
        handle = self._state_for_handle(handle)
        if handle is None or not handle.active:
            return None
        recovery = self._source_recovery
        if recovery is None or recovery.state == "reset_pending":
            return None
        return "failed" if recovery.state == "failed" else "recovering"

    def maintain(self) -> None:
        """Drive a pending source recovery without draining any handle queue.

        Owner-thread only.  A direct consumer that holds a channel back until
        it takes a new epoch still needs the recovery to progress (stop the
        lost session, start and await its replacement); ``poll`` would drain
        that channel's queue.
        """

        self._check_owner()
        if self._source_recovery is not None:
            self._drive_source_recovery()
            return
        stream = self._stream
        if stream is not None and stream.quote_v2_service_recovery_required:
            self._begin_source_recovery(stream)
            if self._source_recovery is not None:
                self._drive_source_recovery()

    def acknowledge_source_epoch_reset(self, handle: object, *, epoch: str) -> bool:
        """Accept only the Hub epoch bound to this exact current handle."""

        self._check_owner()
        handle = self._state_for_handle(handle)
        if (
            handle is None
            or not handle.active
            or type(epoch) is not str
            or not epoch
        ):
            return False
        recovery = self._source_recovery
        with handle.stream._md_callback_lock:
            valid = bool(
                recovery is not None
                and self._recovery_completion_members_ready(recovery)
                and handle.stream is recovery.stream
                and handle.source_epoch_reset_pending is True
                and (handle.last_hub_epoch is None or epoch != handle.last_hub_epoch)
            )
            if valid:
                handle.source_epoch_reset_pending = False
                handle.source_epoch_reset_epoch = epoch
                handle.last_hub_epoch = epoch
                self._complete_source_recovery_if_acknowledged(recovery)
                return True
        if recovery is not None and recovery.state == "reset_pending":
            self._fail_source_recovery(reason="epoch_acknowledgement_invalid")
        return False

    def _recovery_completion_members_ready(self, recovery: _SourceRecovery) -> bool:
        """Validate the current member set while holding the native callback lock."""

        stream = recovery.stream
        scope = recovery.new_scope
        handles = tuple(self._active_handles.values())
        symbols = self._session_symbols()
        return bool(
            self._source_recovery is recovery
            and recovery.state == "reset_pending"
            and self._stream is stream
            and scope is not None
            and handles
            and self._current_source_scope(stream) is scope
            and stream._md_client is recovery.reconnected_client
            and symbols == recovery.symbols
            and set(stream._quote_v2_subscription_metadata) == set(symbols)
            and stream._quote_v2_service_required_symbols == set(symbols)
            and len(self._handles_by_symbol) == len(handles)
            and all(
                handle.active
                and handle.public_handle is not None
                and self._active_handles.get(id(handle.public_handle)) is handle
                and self._known_handles.get(handle.public_handle) is handle
                and self._handles_by_symbol.get(handle.key.instrument.native_symbol) is handle
                and handle.stream is stream
                and handle.source_scope is scope
                for handle in handles
            )
            and self._valid_reconnected_scope(recovery, scope)
        )

    def _complete_source_recovery_if_acknowledged(self, recovery: _SourceRecovery) -> None:
        """Commit the checked remaining set under the native callback lock."""

        if all(
            active.source_epoch_reset_pending is False
            and type(active.source_epoch_reset_epoch) is str
            and bool(active.source_epoch_reset_epoch)
            for active in self._active_handles.values()
        ):
            self._observe_recovery("complete", recovery, tuple(self._active_handles.values()))
            self._source_scope = recovery.new_scope
            self._native_client = recovery.stream._md_client
            self._source_recovery = None

    def _start_shared_stream(
        self, key: MarketSubscriptionKey, generation: str
    ) -> CtpQuoteV2MarketHandle:
        fanout = _CtpQuoteV2Fanout()
        callback_queue = BoundedCtpQuoteV2Queue(self._queue_capacity)
        symbol = key.instrument.native_symbol
        try:
            stream = self._stream_factory(key, fanout)
        except Exception:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_STREAM_FACTORY_FAILED") from None
        if type(stream) is not CtpMarketStream:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_STREAM_INVALID")
        if (
            stream.state is not ConnectionState.DISCONNECTED
            or bool(getattr(stream, "_running", False))
            or getattr(stream, "_md_client", None) is not None
            or bool(getattr(stream, "_quote_v2_subscription_metadata", {}))
        ):
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_STREAM_NOT_FRESH")
        fanout.register(symbol, callback_queue)
        native_client: object | None = None
        native_stop_confirmed = False
        try:
            stream.subscribe_topics([_quote_topic(key)])
            if stream.enable_quote_v2_service_readiness() is not True:
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_AUTH_FAILED")
            subscribed = getattr(stream, "_quote_v2_subscription_metadata", {})
            if type(subscribed) is not dict or set(subscribed) != {symbol}:
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_SUBSCRIBE_FAILED")
            stream.start()
            scope = self._await_authenticated_scope(stream)
            if scope is None:
                if stream.state is ConnectionState.ERROR:
                    raise CtpQuoteV2MarketProviderError("CTP_MARKET_AUTH_FAILED")
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_AUTH_TIMEOUT")
            native_client = getattr(stream, "_md_client", None)
            if native_client is None:
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_AUTH_FAILED")
        except BaseException as start_error:
            if native_client is None:
                native_client = getattr(stream, "_md_client", None)
            try:
                stopped, native_stop_confirmed = self._stop_stream(
                    stream, native_client, native_stop_confirmed
                )
            except BaseException:
                # Startup may have crossed the native worker boundary before
                # an interrupt.  Cleanup can itself be interrupted; keep the
                # exact stream/client in custody and preserve the original
                # startup exception for the caller.
                stopped = False
                native_stop_confirmed = False
            if not stopped:
                self._pending_start_cleanup.append(
                    _PendingStreamCleanup(stream, native_client, native_stop_confirmed)
                )
            if isinstance(start_error, CtpQuoteV2MarketProviderError):
                raise
            if isinstance(start_error, Exception):
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_START_FAILED") from None
            raise

        self._stream = stream
        self._fanout = fanout
        self._native_client = native_client
        self._source_scope = scope
        self._session_environment = key.instrument.environment
        return self._register_handle(key, generation, callback_queue, stream, scope)

    def _subscribe_existing_stream(
        self, key: MarketSubscriptionKey, generation: str
    ) -> CtpQuoteV2MarketHandle:
        stream = self._stream
        fanout = self._fanout
        scope = self._source_scope
        if key.instrument.environment != self._session_environment:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SESSION_KEY_MISMATCH")
        if stream is None or fanout is None:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SOURCE_SCOPE_CHANGED")
        symbol = key.instrument.native_symbol
        retired = getattr(stream, "_retired_quote_v2_subscriptions", {})
        if type(retired) is dict and symbol in retired:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_FRESH_STREAM_REQUIRED")
        if scope is None or self._current_source_scope(stream) is not scope:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SOURCE_SCOPE_CHANGED")
        subscribed = getattr(stream, "_quote_v2_subscription_metadata", {})
        if type(subscribed) is not dict or set(subscribed) != self._session_symbols():
            stream._fail_closed_quote_v2_subscription()
            self._source_scope = None
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SUBSCRIBE_FAILED")
        callback_queue = BoundedCtpQuoteV2Queue(self._queue_capacity)
        fanout.register(symbol, callback_queue)
        ambiguous_symbols = {symbol}
        try:
            # A new instrument joins the existing native MD generation. Keep
            # queued rows for active symbols: the Hub may already have
            # published them to per-key replay journals, and this operation
            # neither changes their source session nor relabels their epoch.
            stream.subscribe_topics([_quote_topic(key)])
            subscribed = getattr(stream, "_quote_v2_subscription_metadata", {})
            expected = set(self._session_symbols()) | {symbol}
            if type(subscribed) is not dict or set(subscribed) != expected:
                if type(subscribed) is dict:
                    ambiguous_symbols.update(expected.symmetric_difference(subscribed))
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_SUBSCRIBE_FAILED")
            # Like the first key (``_start_shared_stream`` waits for the
            # authenticated scope), an added key is acquired only after the
            # venue acknowledged it, so its source can be described at once:
            # the Gateway Hub describes it in the same subscribe call.  A real
            # front acknowledges asynchronously (SimNow, 2026-11-06: the second
            # key of a Gateway pair failed MARKET_SOURCE_DESCRIPTOR_UNAVAILABLE).
            # A missing or rejected acknowledgement leaves the native add
            # ambiguous and fails the shared session closed below.
            if not self._await_symbol_ready(stream, symbol):
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_SUBSCRIBE_UNACKNOWLEDGED")
        except CtpMarketStreamSubscriptionError as exc:
            # This typed stream exception denotes a pre-mutation rejection.
            # Remove only the tentative target; preserve active handle queues
            # and the current source scope for every code in this family.
            callback_queue.clear()
            fanout.unregister(symbol, callback_queue)
            if exc.code == "CTP_MD_RESUBSCRIBE_REQUIRES_FRESH_STREAM":
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_FRESH_STREAM_REQUIRED") from None
            if exc.code == "CTP_MD_RETIRED_SUBSCRIPTION_LIMIT":
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_RETIRED_SYMBOL_LIMIT") from None
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SUBSCRIBE_FAILED") from None
        except Exception as exc:
            # The native add may have partially succeeded even if stream
            # metadata or the API response did not. Retain that uncertainty
            # on the stream, revoke all source descriptors, and keep existing
            # handles solely as cleanup authorities for stopping the session.
            stream._fail_closed_quote_v2_subscription(tuple(ambiguous_symbols))
            self._source_scope = None
            callback_queue.clear()
            for active in self._active_handles.values():
                active.queue.clear()
            fanout.unregister(symbol, callback_queue)
            if isinstance(exc, CtpQuoteV2MarketProviderError):
                raise
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SUBSCRIBE_FAILED") from None
        return self._register_handle(key, generation, callback_queue, stream, scope)

    def _session_symbols(self) -> frozenset[str]:
        """Every symbol the shared native session carries: consumed and parked."""
        return frozenset(self._handles_by_symbol) | frozenset(self._parked_symbols)

    def _park_symbol(self, handle: _HandleState) -> bool:
        """Release a handle but keep its native subscription on the shared session.

        CTP quote callbacks carry no subscription token, so a symbol that was
        natively unsubscribed cannot be added again on the same MD client
        (``CTP_MARKET_FRESH_STREAM_REQUIRED``). In a Gateway that rule made one
        strategy's restart impossible while another strategy kept the session
        alive (SimNow 2026-11-10: the second subscribe of rb2701 was refused
        while SA701 stayed subscribed). A parked symbol is never unsubscribed:
        its ticks are dropped at the fan-out until a consumer returns, and a
        tick delivered after that is a current tick of an uninterrupted
        subscription. Parked symbols do not keep the session alive: releasing
        the last consumed symbol stops it. Past ``max_parked_symbols`` (or with
        0) the symbol is natively unsubscribed and retired as before.
        """
        symbol = handle.key.instrument.native_symbol
        fanout = self._fanout
        if (
            len(self._parked_symbols) >= self._max_parked_symbols
            or fanout is None
            or handle.stream is not self._stream
            or symbol in self._parked_symbols
        ):
            return False
        if not fanout.unregister(symbol, handle.queue):
            return False
        self._parked_symbols[symbol] = _quote_topic(handle.key)
        self._remove_handle(handle)
        return True

    def _resume_parked_symbol(
        self, key: MarketSubscriptionKey, generation: str
    ) -> CtpQuoteV2MarketHandle:
        """Serve a parked symbol again; no native request is made."""
        stream = self._stream
        fanout = self._fanout
        scope = self._source_scope
        symbol = key.instrument.native_symbol
        if key.instrument.environment != self._session_environment:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SESSION_KEY_MISMATCH")
        if stream is None or fanout is None:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SOURCE_SCOPE_CHANGED")
        if self._parked_symbols.get(symbol) != _quote_topic(key):
            # Another native topic under the same symbol is another subscription.
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_FRESH_STREAM_REQUIRED")
        if scope is None or self._current_source_scope(stream) is not scope:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SOURCE_SCOPE_CHANGED")
        subscribed = getattr(stream, "_quote_v2_subscription_metadata", {})
        if type(subscribed) is not dict or set(subscribed) != self._session_symbols():
            stream._fail_closed_quote_v2_subscription()
            self._source_scope = None
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SUBSCRIBE_FAILED")
        try:
            acknowledged = stream.is_quote_v2_service_symbol_ready(symbol) is True
        except Exception:
            acknowledged = False
        if not acknowledged:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SUBSCRIBE_UNACKNOWLEDGED")
        callback_queue = BoundedCtpQuoteV2Queue(self._queue_capacity)
        fanout.register(symbol, callback_queue)
        del self._parked_symbols[symbol]
        return self._register_handle(key, generation, callback_queue, stream, scope)

    def _register_handle(
        self,
        key: MarketSubscriptionKey,
        generation: str,
        callback_queue: BoundedCtpQuoteV2Queue,
        stream: CtpMarketStream,
        scope: CtpQuoteClockSourceScope,
    ) -> CtpQuoteV2MarketHandle:
        self._recovery_member_revision += 1
        handle = CtpQuoteV2MarketHandle(key=key, generation=generation)
        state = _HandleState(key, generation, handle, callback_queue, stream, scope)
        self._known_handles[handle] = state
        self._active_handles[id(handle)] = state
        self._handles_by_symbol[key.instrument.native_symbol] = state
        return handle

    def _state_for_handle(self, handle: object) -> _HandleState | _InactiveHandle | None:
        if type(handle) is not CtpQuoteV2MarketHandle:
            return None
        state = self._known_handles.get(handle)
        if state is not None and state.active:
            if state.public_handle is not handle or self._active_handles.get(id(handle)) is not state:
                return None
        return state

    def _begin_source_recovery(self, stream: CtpMarketStream, *, _reentry: bool = False) -> None:
        self._recovery_member_revision += 1
        if (
            self._source_recovery is not None
            or stream is not self._stream
            or stream.quote_v2_service_recovery_required is not True
            or stream.quote_v2_service_fail_closed is True
            or not self._active_handles
            or any(handle.stream is not stream for handle in self._active_handles.values())
        ):
            if self._recovery_observation is not None:
                try:
                    if (
                        self._source_recovery is None
                        and stream is self._stream
                        and stream.quote_v2_service_recovery_required
                    ):
                        self._observe_recovery(
                            "invalidate", "REENTRY_FAILED" if _reentry else "BEGIN_FAILED",
                            None, tuple(self._active_handles.values()),
                        )
                except Exception:
                    self._recovery_observation_unavailable = True
            return
        old_client = self._native_client
        old_scope = self._source_scope
        handles = tuple(self._active_handles.values())
        symbols = self._session_symbols()
        subscribed = getattr(stream, "_quote_v2_subscription_metadata", None)
        stream_generation = getattr(stream, "_connection_generation", None)
        if (
            old_client is None
            or getattr(stream, "_md_client", None) is not old_client
            or type(old_scope) is not CtpQuoteClockSourceScope
            or any(handle.source_scope is not old_scope for handle in handles)
            or type(subscribed) is not dict
            or set(subscribed) != set(symbols)
            or type(stream_generation) is not int
            or stream_generation != old_scope.connection_generation
            or stream.quote_v2_service_binding_matches() is not True
        ):
            self._fail_source_recovery(stream)
            return
        try:
            old_client_generation = getattr(old_client, "connection_generation", None)
        except Exception:
            old_client_generation = None
        if type(old_client_generation) is not int or old_client_generation <= 0:
            self._fail_source_recovery(stream)
            return
        for handle in handles:
            handle.queue.clear()
            handle.clock_events.clear()
            handle.source_epoch_reset_pending = False
            handle.source_epoch_reset_epoch = None
        if stream.detach_quote_v2_client_for_recovery(old_client) is not True:
            self._fail_source_recovery(stream)
            return
        if (
            getattr(stream, "_quote_v2_service_recovery_stopped_generation", None)
            != old_client_generation
        ):
            self._fail_source_recovery(stream)
            return
        self._source_recovery = _SourceRecovery(
            stream=stream,
            old_client=old_client,
            old_client_generation=old_client_generation,
            old_scope=old_scope,
            old_stream_generation=stream_generation,
            symbols=symbols,
            deadline=time.monotonic() + self._connect_timeout_seconds,
            state="stopping",
        )
        self._observe_recovery(
            "begin", self._source_recovery, tuple(self._active_handles.values()), _reentry
        )
        self._recovery_retry_logged_at = None
        self._recovery_retry_suppressed = 0
        _logger.warning(
            "CTP market source lost (stream_generation=%s, symbols=%d); "
            "stopping the MD session and starting a new one",
            stream_generation,
            len(symbols),
        )

    def _drive_source_recovery(self) -> None:
        """Advance the source recovery by one owner-thread step.

        Attempt states: ``stopping`` (confirm the replaced client's stop) ->
        [``retry_wait`` (a retry's backoff)] -> ``reconnecting`` (start the
        replacement off-thread) -> ``connecting`` (await its login, ACK set
        and fresh tick) -> ``reset_pending`` (await the consumer epochs).
        ``abandoning``: a failed attempt waits for its worker to return.
        A failure that is not a contract violation starts the next attempt
        (:meth:`_retry_source_recovery`); a contract violation is final
        (:meth:`_fail_source_recovery`).
        """

        recovery = self._source_recovery
        if recovery is None or recovery.state == "failed":
            return
        if recovery.state == "abandoning":
            if not self._source_recovery_worker_inflight(recovery):
                self._start_next_source_recovery_attempt(recovery)
            return
        reconnect_inflight = (
            recovery.reconnect_worker is not None and not recovery.reconnect_done.is_set()
        )
        if (
            recovery.state in ("stopping", "reconnecting")
            and self._source_recovery_worker_inflight(recovery)
            and time.monotonic() >= recovery.deadline
        ):
            # A native stop or start still in vendor code past the attempt
            # deadline.  CtpMarketStream.connect holds its callback lock across
            # the synchronous vendor Create/Register/Init calls: do not take
            # that lock from the Gateway poll thread while startup is in
            # flight.  Revoking the provider state keeps this stream from
            # publishing, the worker fences a late client when it returns, and
            # the next attempt starts only after that.
            self._retry_source_recovery(
                recovery,
                "stop_unconfirmed" if recovery.state == "stopping" else "replacement_start_late",
            )
            return
        stream = recovery.stream
        current_symbols = self._session_symbols()
        subscribed = getattr(stream, "_quote_v2_subscription_metadata", None)
        violation = None
        if stream is not self._stream:
            violation = "stream_replaced"
        elif current_symbols != recovery.symbols:
            violation = "symbol_set_changed"
        elif type(subscribed) is not dict or set(subscribed) != set(recovery.symbols):
            violation = "subscription_changed"
        elif not reconnect_inflight and stream.quote_v2_service_binding_matches() is not True:
            violation = "source_binding_changed"
        elif any(
            handle.source_scope is not recovery.old_scope
            and handle.source_scope is not recovery.new_scope
            for handle in self._active_handles.values()
        ):
            violation = "handle_scope_foreign"
        if violation is not None:
            self._fail_source_recovery(reason=violation)
            return

        if recovery.state == "stopping":
            if recovery.stop_worker is None:
                if not self._start_source_recovery_stop_worker(recovery):
                    self._retry_source_recovery(recovery, "stop_worker_unavailable")
                return
            if not recovery.stop_done.is_set():
                return
            receipt = recovery.stop_receipt
            stop_error = recovery.stop_error
            if isinstance(stop_error, _SourceRecoveryContractError):
                self._fail_source_recovery(reason=stop_error.reason)
                return
            if isinstance(stop_error, _SourceRecoveryRetryableError):
                self._retry_source_recovery(recovery, stop_error.reason)
                return
            if stop_error is not None:
                self._retry_source_recovery(recovery, f"stop_raised:{type(stop_error).__name__}")
                return
            if recovery.stop_timed_out:
                self._retry_source_recovery(recovery, "stop_unconfirmed")
                return
            if (
                type(receipt) is not CtpNativeStopReceipt
                or receipt.complete is not True
                or type(receipt.connection_generation) is not int
                or type(recovery.old_client_generation) is not int
                or receipt.connection_generation != recovery.old_client_generation
            ):
                self._fail_source_recovery(reason="stop_receipt_invalid")
                return
            if recovery.retry:
                # Detach a stopped failed replacement (or keep the retained
                # origin) and wait for the backoff before the next start.
                if stream.retain_quote_v2_stopped_client_for_retry(
                    recovery.old_client, receipt
                ) is not True:
                    self._fail_source_recovery(reason="retry_retain_refused")
                    return
                recovery.state = "retry_wait"
            else:
                if (
                    getattr(stream, "_md_client", None) is not None
                    or stream.state is not ConnectionState.DISCONNECTED
                ):
                    self._fail_source_recovery(reason="stream_rebound")
                    return
                self._start_source_recovery_reconnect(recovery, receipt)
                return

        if recovery.state == "retry_wait":
            if time.monotonic() < recovery.retry_at:
                return
            receipt = recovery.stop_receipt
            if stream.rearm_quote_v2_reconnect_after_confirmed_stop(
                recovery.old_client, receipt
            ) is not True:
                self._fail_source_recovery(reason="retry_rearm_refused")
                return
            self._start_source_recovery_reconnect(recovery, receipt)
            return

        if recovery.state == "reconnecting":
            worker = recovery.reconnect_worker
            if worker is None:
                self._fail_source_recovery(reason="reconnect_worker_missing")
                return
            # Never inspect a stream property guarded by _md_callback_lock
            # while native startup is executing under that lock.
            if not recovery.reconnect_done.is_set():
                if worker.is_alive():
                    return
                # A finished thread has set the event before exiting.
                if not recovery.reconnect_done.is_set():
                    self._fail_source_recovery(reason="reconnect_worker_lost")
                    return
            if recovery.reconnect_fenced:
                self._retry_source_recovery(recovery, "replacement_start_late")
                return
            if recovery.reconnect_error is not None or recovery.reconnect_succeeded is not True:
                failure = recovery.reconnect_failure
                if failure == "precondition":
                    # The stream refused a start the provider had just proven
                    # safe: its state changed under the recovery.
                    self._fail_source_recovery(reason="reconnect_refused")
                    return
                self._retry_source_recovery(
                    recovery, f"replacement_{failure or 'start_unconfirmed'}"
                )
                return
            new_client = recovery.reconnected_client
            if (
                new_client is None
                or new_client is recovery.old_client
                or getattr(stream, "_md_client", None) is not new_client
                or type(getattr(stream, "_connection_generation", None)) is not int
                or stream._connection_generation <= recovery.old_stream_generation
                or stream.quote_v2_service_binding_matches() is not True
                or frozenset(getattr(stream, "_quote_v2_subscription_metadata", {}))
                != recovery.symbols
            ):
                self._fail_source_recovery(reason="replacement_inconsistent")
                return
            fail_closed, state = self._replacement_status(stream)
            if fail_closed:
                # Refused by the venue or a protocol check (a final login
                # refusal, another account, a subscription anomaly).
                self._fail_source_recovery(reason="replacement_refused")
                return
            if state not in (ConnectionState.CONNECTING, ConnectionState.AUTHENTICATED):
                if state is ConnectionState.ERROR:
                    self._retry_source_recovery(recovery, "replacement_lost")
                else:
                    self._fail_source_recovery(reason="replacement_inconsistent")
                return
            self._native_client = new_client
            recovery.state = "connecting"
            return

        if recovery.state == "connecting":
            if self._replacement_status(stream)[0]:
                self._fail_source_recovery(reason="replacement_refused")
                return
            new_scope = self._current_source_scope(stream)
            if new_scope is None:
                # Both read under one lock: a final refusal sets them together.
                fail_closed, state = self._replacement_status(stream)
                if fail_closed:
                    self._fail_source_recovery(reason="replacement_refused")
                elif state is ConnectionState.ERROR:
                    # Lost before its scope was confirmed (no final refusal):
                    # stop it and start another one.
                    self._retry_source_recovery(recovery, "replacement_lost")
                return
            if not self._valid_reconnected_scope(recovery, new_scope):
                self._fail_source_recovery(reason="replacement_scope_invalid")
                return
            recovery.new_scope = new_scope
            recovery.state = "reset_pending"
            for handle in self._active_handles.values():
                handle.clock_events.clear()
                handle.source_scope = new_scope
                handle.source_epoch_reset_pending = True
                handle.source_epoch_reset_epoch = None
            _logger.warning(
                "CTP market source restarted on a new MD session "
                "(stream_generation=%s, symbols=%d, attempt=%d); awaiting a new consumer epoch",
                new_scope.connection_generation,
                len(recovery.symbols),
                recovery.attempt,
            )
            return

        if recovery.state == "reset_pending":
            if recovery.new_scope is None or any(
                handle.source_scope is not recovery.new_scope
                for handle in self._active_handles.values()
            ):
                self._fail_source_recovery(reason="replacement_scope_changed")
                return
            if (
                stream.quote_v2_service_recovery_required is True
                and stream.quote_v2_service_fail_closed is not True
            ):
                # The replacement session was lost before every consumer took
                # its new epoch.  Nothing of it is described any more; restart
                # the recovery from that replacement (stop it, start another).
                self._source_recovery = None
                self._source_scope = recovery.new_scope
                self._native_client = getattr(stream, "_md_client", None)
                reentered = False
                try:
                    self._begin_source_recovery(stream, _reentry=True)
                    reentered = self._source_recovery is not None
                finally:
                    if not reentered:
                        self._observe_recovery("invalidate", "REENTRY_FAILED")
                if self._source_recovery is not None:
                    self._drive_source_recovery()

    @staticmethod
    def _observe_source_recovery_stop(recovery: _SourceRecovery) -> None:
        """Observe one retained client's stop off the Gateway owner thread.

        The first typed stop call initiates cleanup; subsequent zero-time calls
        only inspect that same MdClient's retained native session. A worker that
        blocks in vendor code remains strongly referenced by ``recovery`` and
        can never authorize a replacement client by itself.

        A retry does not know the stopped client's generation beforehand (the
        client may have reconnected natively until its stop): it takes the
        generation of the exact client's complete receipt, which that stop
        froze.  A first attempt whose receipt names another generation is
        retried the same way.
        """

        try:
            stop_and_wait = getattr(recovery.old_client, "stop_and_wait", None)
            if not callable(stop_and_wait):
                recovery.stop_error = _SourceRecoveryContractError("stop_unavailable")
                return
            while time.monotonic() < recovery.deadline:
                recovery.stop_receipt_attempts += 1
                receipt = stop_and_wait(timeout=0.0)
                if (
                    type(receipt) is not CtpNativeStopReceipt
                    or type(receipt.connection_generation) is not int
                    or receipt.connection_generation < 0
                ):
                    recovery.stop_error = _SourceRecoveryContractError("stop_receipt_invalid")
                    return
                expected_generation = recovery.old_client_generation
                if expected_generation is None:
                    if receipt.complete is True:
                        recovery.old_client_generation = receipt.connection_generation
                        recovery.stop_receipt = receipt
                        return
                elif receipt.connection_generation != expected_generation:
                    recovery.stop_error = _SourceRecoveryRetryableError(
                        "stop_generation_changed"
                    )
                    return
                else:
                    recovery.stop_receipt = receipt
                    if receipt.complete is True:
                        return
                if receipt.complete is not False:
                    recovery.stop_error = _SourceRecoveryContractError("stop_receipt_invalid")
                    return
                remaining = recovery.deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.01, remaining))
            recovery.stop_timed_out = True
        except BaseException as exc:
            recovery.stop_error = exc
        finally:
            recovery.stop_done.set()

    @staticmethod
    def _start_source_recovery_stop_worker(recovery: _SourceRecovery) -> bool:
        if recovery.stop_worker is not None or recovery.stop_done.is_set():
            return False
        worker = threading.Thread(
            target=CtpQuoteV2MarketProvider._observe_source_recovery_stop,
            args=(recovery,),
            name="ctp-md-source-stop-observer",
            daemon=True,
        )
        recovery.stop_worker = worker
        try:
            worker.start()
        except BaseException as exc:
            recovery.stop_error = exc
            recovery.stop_done.set()
            return False
        return True

    @staticmethod
    def _observe_source_recovery_reconnect(
        recovery: _SourceRecovery,
        receipt: CtpNativeStopReceipt,
    ) -> None:
        """Start one replacement client off the Gateway owner thread.

        This worker is authorized only by the already validated receipt for
        ``recovery.old_client``. Its result is inert until a later owner poll
        validates and consumes it. A late or cancelled startup is fenced after
        the native call returns, never by blocking the owner on the stream lock.
        """

        stream = recovery.stream
        try:
            result = stream.reconnect_quote_v2_after_confirmed_stop(
                recovery.old_client, receipt
            )
            recovery.reconnected_client = getattr(stream, "_md_client", None)
            recovery.reconnect_succeeded = result is True
            if result is not True:
                recovery.reconnect_error = RuntimeError("ctp_md_reconnect_unconfirmed")
                try:
                    failure = stream.quote_v2_service_reconnect_failure
                except Exception:
                    failure = None
                # "precondition" or "start_raised:<exception class>" only.
                recovery.reconnect_failure = (
                    failure
                    if type(failure) is str and failure.isprintable() and len(failure) <= 96
                    else "start_unconfirmed"
                )
        except BaseException as exc:
            recovery.reconnected_client = getattr(stream, "_md_client", None)
            recovery.reconnect_error = exc
            recovery.reconnect_failure = f"start_worker_raised:{type(exc).__name__}"
        finally:
            if (
                recovery.reconnect_cancelled.is_set()
                or time.monotonic() >= recovery.deadline
            ):
                # The native startup method has returned, so its callback lock
                # is available. Keep a late successful client owned but fenced.
                with stream._md_callback_lock:
                    stream._managed_quote_clock_source_scope = None
                    stream._quote_v2_service_ready = False
                    stream._quote_v2_service_fail_closed = True
                    stream.state = ConnectionState.ERROR
                recovery.reconnect_fenced = True
            recovery.reconnect_done.set()

    @staticmethod
    def _start_source_recovery_reconnect_worker(
        recovery: _SourceRecovery,
        receipt: CtpNativeStopReceipt,
    ) -> bool:
        if recovery.reconnect_worker is not None or recovery.reconnect_done.is_set():
            return False
        worker = threading.Thread(
            target=CtpQuoteV2MarketProvider._observe_source_recovery_reconnect,
            args=(recovery, receipt),
            name="ctp-md-source-reconnect-starter",
            daemon=True,
        )
        recovery.reconnect_worker = worker
        try:
            worker.start()
        except BaseException as exc:
            recovery.reconnect_error = exc
            recovery.reconnect_done.set()
            return False
        return True

    def _valid_reconnected_scope(
        self,
        recovery: _SourceRecovery,
        scope: CtpQuoteClockSourceScope,
    ) -> bool:
        stream = recovery.stream
        client = getattr(stream, "_md_client", None)
        try:
            client_generation = getattr(client, "connection_generation", None)
            current_scope = stream.get_quote_v2_service_ready_scope()
            stream_generation = getattr(stream, "_connection_generation", None)
            client_token = getattr(stream, "_md_client_token", None)
            callback_capability = getattr(
                stream, "_managed_quote_v2_callback_capability", None
            )
        except Exception:
            return False
        old_scope = recovery.old_scope
        return bool(
            type(scope) is CtpQuoteClockSourceScope
            and current_scope is scope
            and scope is not old_scope
            and scope._stream is stream
            and scope._stream_token is getattr(stream, "_managed_quote_v2_stream_token", None)
            and scope._client is client
            and client is not recovery.old_client
            and type(client_token) is int
            and scope._client_token == client_token
            and scope._client_token > old_scope._client_token
            and scope._callback_capability is callback_capability
            and type(client_generation) is int
            and scope._client_generation == client_generation
            and type(stream_generation) is int
            and scope.connection_generation == stream_generation
            and scope.connection_generation > recovery.old_stream_generation
            and scope.source == old_scope.source == "ctp.native.md"
            and scope.clock_source == old_scope.clock_source
            and scope._clock_source_callable is old_scope._clock_source_callable
            and scope.clock_source_session_id != old_scope.clock_source_session_id
            and scope.clock_domain_id != old_scope.clock_domain_id
            and stream.state is ConnectionState.AUTHENTICATED
            and stream.quote_v2_service_binding_matches() is True
            and frozenset(getattr(stream, "_quote_v2_subscription_metadata", {}))
            == recovery.symbols
        )

    def _start_source_recovery_reconnect(
        self, recovery: _SourceRecovery, receipt: CtpNativeStopReceipt
    ) -> None:
        """Start this attempt's replacement; its own deadline starts now."""

        recovery.state = "reconnecting"
        recovery.deadline = time.monotonic() + self._connect_timeout_seconds
        if not self._start_source_recovery_reconnect_worker(recovery, receipt):
            self._retry_source_recovery(recovery, "start_worker_unavailable")

    @staticmethod
    def _replacement_status(stream: CtpMarketStream) -> tuple[bool, ConnectionState]:
        """``(fail_closed, state)`` of the stream, read atomically.

        Only while no recovery worker is in native startup (it holds the lock).
        """

        with stream._md_callback_lock:
            return stream._quote_v2_service_fail_closed is True, stream.state

    @staticmethod
    def _source_recovery_worker_inflight(recovery: _SourceRecovery) -> bool:
        """A stop or start worker of this attempt has not returned yet."""

        return bool(
            (recovery.stop_worker is not None and not recovery.stop_done.is_set())
            or (
                recovery.reconnect_worker is not None
                and not recovery.reconnect_done.is_set()
            )
        )

    def _fence_source_recovery_publication(self) -> None:
        self._source_scope = None
        for handle in self._active_handles.values():
            handle.queue.clear()
            handle.clock_events.clear()
            handle.source_epoch_reset_pending = False
            handle.source_epoch_reset_epoch = None

    def _retry_source_recovery(self, recovery: _SourceRecovery, reason: str) -> None:
        """Retry a failed attempt that is not a contract violation (defect 25).

        Publication stays revoked.  A worker still in native code is cancelled
        (a late replacement is fenced when it returns) and the next attempt
        starts once it has returned.
        """

        recovery.failure = reason
        recovery.failed_state = recovery.state
        self._fence_source_recovery_publication()
        if self._source_recovery_worker_inflight(recovery):
            recovery.reconnect_cancelled.set()
            recovery.state = "abandoning"
            return
        self._start_next_source_recovery_attempt(recovery)

    def _start_next_source_recovery_attempt(self, previous: _SourceRecovery) -> None:
        """Begin the attempt after ``previous`` failed (no worker of it runs).

        Its origin is the client bound to the stream (the failed replacement,
        stopped first) or, with none bound, the previous origin: its confirmed
        stop is reused, otherwise observed again.  The replacement starts
        after the backoff (:func:`_source_recovery_retry_delay`).
        """

        stream = previous.stream
        reason = previous.failure or "unknown"
        bound = getattr(stream, "_md_client", None)
        if (
            bound is not None
            and bound is not previous.old_client
            and bound is not previous.reconnected_client
        ):
            self._fail_source_recovery(reason="foreign_client_bound")
            return
        receipt = previous.stop_receipt
        reuse_stop = bool(
            bound is None
            and previous.stop_done.is_set()
            and previous.stop_error is None
            and not previous.stop_timed_out
            and type(receipt) is CtpNativeStopReceipt
            and receipt.complete is True
            and type(previous.old_client_generation) is int
            and receipt.connection_generation == previous.old_client_generation
        )
        if reuse_stop:
            if stream.retain_quote_v2_stopped_client_for_retry(
                previous.old_client, receipt
            ) is not True:
                self._fail_source_recovery(reason="retry_retain_refused")
                return
            origin = previous.old_client
        else:
            origin = bound if bound is not None else previous.old_client
            # No worker runs: the stream lock is free.  The bound client's
            # callbacks can publish nothing until the next attempt detaches it.
            with stream._md_callback_lock:
                stream._managed_quote_clock_source_scope = None
                stream._quote_v2_service_ready = False
                stream._quote_v2_service_fail_closed = True
                stream.state = ConnectionState.ERROR
        stream_generation = getattr(stream, "_connection_generation", None)
        if type(stream_generation) is not int:
            self._fail_source_recovery(reason="stream_generation_invalid")
            return
        now = time.monotonic()
        delay = _source_recovery_retry_delay(previous.attempt)
        recovery = _SourceRecovery(
            stream=stream,
            old_client=origin,
            old_client_generation=previous.old_client_generation if reuse_stop else None,
            old_scope=previous.old_scope,
            old_stream_generation=stream_generation,
            symbols=previous.symbols,
            deadline=now + self._connect_timeout_seconds,
            state="retry_wait" if reuse_stop else "stopping",
            attempt=previous.attempt + 1,
            retry=True,
            retry_at=now + delay,
        )
        if reuse_stop:
            recovery.stop_receipt = receipt
            recovery.stop_done.set()
        self._native_client = origin
        self._source_recovery = recovery
        self._observe_recovery("transfer", previous, recovery, tuple(self._active_handles.values()))
        self._log_source_recovery_retry(previous, reason, delay)

    def _log_source_recovery_retry(
        self, failed: _SourceRecovery, reason: str, delay: float
    ) -> None:
        now = time.monotonic()
        last = self._recovery_retry_logged_at
        if last is not None and now - last < _SOURCE_RECOVERY_LOG_INTERVAL_SECONDS:
            self._recovery_retry_suppressed += 1
            return
        suppressed = self._recovery_retry_suppressed
        self._recovery_retry_logged_at = now
        self._recovery_retry_suppressed = 0
        _logger.warning(
            "CTP market source recovery attempt %d failed in state %s (reason=%s); "
            "retrying in %g s (%d failed attempts not logged since the last line)",
            failed.attempt,
            failed.failed_state or failed.state,
            reason,
            delay,
            suppressed,
        )

    def _fail_source_recovery(
        self,
        stream: CtpMarketStream | None = None,
        *,
        reason: str = "contract_violation",
    ) -> None:
        """Fail the recovery for good: a contract violation (defect 25 list).

        Closed until the provider is closed: the source binding or symbol set
        changed under it, an invalid stop receipt or a stream refusing a
        proven-safe step, an inconsistent replacement, a replacement refused
        by the venue or a protocol check, or an invalid new scope or epoch.
        """

        recovery = self._source_recovery
        self._observe_recovery("fail", recovery, tuple(self._active_handles.values()))
        if recovery is not None:
            if recovery.state != "failed":
                _logger.error(
                    "CTP market source recovery failed in state %s (reason=%s, attempt=%d); "
                    "the MD session stays closed until the provider is closed",
                    recovery.state,
                    reason,
                    recovery.attempt,
                )
            if (
                recovery.reconnect_worker is not None
                and not recovery.reconnect_done.is_set()
            ):
                self._fail_source_recovery_without_stream_lock(recovery)
                return
            stream = recovery.stream
            recovery.state = "failed"
        if stream is None:
            return
        self._source_scope = None
        for handle in self._active_handles.values():
            handle.queue.clear()
            handle.source_epoch_reset_pending = False
            handle.source_epoch_reset_epoch = None
        with stream._md_callback_lock:
            stream._managed_quote_clock_source_scope = None
            stream._quote_v2_service_ready = False
            stream._quote_v2_service_fail_closed = True
            stream.state = ConnectionState.ERROR

    def _fail_source_recovery_without_stream_lock(
        self,
        recovery: _SourceRecovery,
    ) -> None:
        """Fence provider publication while a startup worker owns stream lock."""

        recovery.reconnect_cancelled.set()
        recovery.state = "failed"
        self._source_scope = None
        for handle in self._active_handles.values():
            handle.queue.clear()
            handle.source_epoch_reset_pending = False
            handle.source_epoch_reset_epoch = None

    def _remove_handle(self, handle: _HandleState) -> None:
        self._recovery_member_revision += 1
        self._observe_recovery("invalidate", "MEMBERS_CHANGED", self._source_recovery)
        handle.active = False
        handle.queue.clear()
        handle.clock_events.clear()
        public_handle = handle.public_handle
        self._active_handles.pop(id(public_handle), None)
        if public_handle is not None:
            self._known_handles[public_handle] = _INACTIVE_HANDLE
        handle.public_handle = None
        self._handles_by_symbol.pop(handle.key.instrument.native_symbol, None)

    def _reset_active_stream(self) -> None:
        self._recovery_member_revision += 1
        self._observe_recovery("invalidate", "CLOSED", self._source_recovery)
        self._stream = None
        self._parked_symbols.clear()
        self._fanout = None
        self._native_client = None
        self._source_scope = None
        self._session_environment = None
        self._source_recovery = None

    @staticmethod
    def _current_source_scope(stream: CtpMarketStream) -> CtpQuoteClockSourceScope | None:
        try:
            scope = stream.get_quote_v2_service_ready_scope()
        except Exception:
            return None
        if type(scope) is not CtpQuoteClockSourceScope:
            return None
        return scope

    def _validate_key(self, key: object) -> None:
        try:
            validate_ctp_market_key(
                key, source_ref=self._source_ref, entitlement_ref=self._entitlement_ref
            )
        except CtpMarketKeyError as error:
            raise CtpQuoteV2MarketProviderError(error.code) from None

    def _await_authenticated_scope(self, stream: CtpMarketStream) -> object | None:
        deadline = time.monotonic() + self._connect_timeout_seconds
        while True:
            try:
                scope = stream.get_quote_v2_service_ready_scope()
            except Exception:
                scope = None
            if scope is not None:
                return scope
            if stream.quote_v2_service_fail_closed or stream.quote_v2_service_recovery_required:
                return None
            if stream.state is ConnectionState.ERROR:
                return None
            thread = getattr(stream, "_thread", None)
            if thread is not None and not thread.is_alive():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.05, remaining))

    def _await_symbol_ready(self, stream: CtpMarketStream, symbol: str) -> bool:
        """Wait (bounded) until the venue acknowledged an added symbol."""
        deadline = time.monotonic() + self._connect_timeout_seconds
        while True:
            try:
                if stream.is_quote_v2_service_symbol_ready(symbol) is True:
                    return True
            except Exception:
                return False
            if stream.quote_v2_service_fail_closed or stream.quote_v2_service_recovery_required:
                return False
            if stream.state is ConnectionState.ERROR:
                return False
            thread = getattr(stream, "_thread", None)
            if thread is not None and not thread.is_alive():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.02, remaining))

    def _stop_stream(
        self,
        stream: CtpMarketStream,
        native_client: object | None,
        native_stop_confirmed: bool,
    ) -> tuple[bool, bool]:
        current_client = getattr(stream, "_md_client", None)
        if current_client is not None and current_client is not native_client:
            native_client = current_client
            native_stop_confirmed = False
        if not native_stop_confirmed:
            try:
                stream.stop()
            except Exception:
                # CtpMarketStream.disconnect clears its current binding before
                # delegating to MdClient.stop. The retained native client below
                # gives us a bounded typed receipt even if that delegation
                # raised after detaching the stream.
                pass
            if native_client is not None:
                stop_and_wait = getattr(native_client, "stop_and_wait", None)
                if not callable(stop_and_wait):
                    return False, False
                try:
                    receipt = stop_and_wait(timeout=self._stop_timeout_seconds)
                except Exception:
                    return False, False
                if (
                    type(receipt) is not CtpNativeStopReceipt
                    or receipt.complete is not True
                    or receipt.native_released is not True
                    or receipt.thread_alive is not False
                    or receipt.timed_out is not False
                    or (receipt.join_required and receipt.join_completed is not True)
                ):
                    return False, False
                native_stop_confirmed = True
        thread = getattr(stream, "_thread", None)
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(timeout=self._stop_timeout_seconds)
            except Exception:
                return False, native_stop_confirmed
            if thread.is_alive():
                return False, native_stop_confirmed
        confirmed = (
            stream.state is ConnectionState.DISCONNECTED
            and getattr(stream, "_md_client", None) is None
            and (thread is None or not thread.is_alive())
        )
        if getattr(stream, "_md_client", None) is not None:
            # A startup thread can race stop and bind its client afterward.
            # Keep the stream itself retained so the next owner-thread retry
            # captures and stops that exact client.
            return False, False
        if confirmed and native_client is None:
            # No native client was ever bound. This is confirmed only after
            # the wrapper has exited, ruling out a late connect-after-stop.
            native_stop_confirmed = True
        confirmed = confirmed and native_stop_confirmed
        return confirmed, native_stop_confirmed

    def _retry_pending_start_cleanup(self) -> None:
        pending: list[_PendingStreamCleanup] = []
        for cleanup in self._pending_start_cleanup:
            stopped, native_stop_confirmed = self._stop_stream(
                cleanup.stream,
                cleanup.native_client,
                cleanup.native_stop_confirmed,
            )
            if not stopped:
                pending.append(
                    _PendingStreamCleanup(
                        cleanup.stream,
                        cleanup.native_client,
                        native_stop_confirmed,
                    )
                )
        self._pending_start_cleanup = pending
        if pending:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_STARTUP_CLEANUP_PENDING")

    def _begin_close(self) -> None:
        self._check_owner(allow_closing=True)
        self._recovery_member_revision += 1
        self._observe_recovery("invalidate", "CLOSED", self._source_recovery)
        self._close_only = True

    def _check_owner(self, *, allow_closing: bool = False) -> None:
        if os.getpid() != self._owner_pid or threading.current_thread() is not self._owner_thread:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_OWNER_THREAD_REQUIRED")
        if self._close_only and not allow_closing:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_OWNER_CLOSING")


def _project_native_tick(
    item: object,
    handle: _HandleState,
    *,
    local_message_loss: bool = False,
) -> MarketProviderEvent:
    receipt = _get_ctp_managed_quote_v2_receipt(item, handle.stream)
    payload = project_ctp_market_tick(
        item,
        handle.key,
        handle.source_scope,
        receipt,
        local_message_loss=local_message_loss,
    )
    projected = payload.to_dict()
    encoded = json.dumps(
        projected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")
    if len(encoded) > MAX_CTP_QUOTE_EVENT_BYTES:
        raise ValueError("ticker event too large")
    return MarketProviderEvent(
        generation=handle.generation,
        observed_at_utc=payload.received_at_utc,
        data=projected,
    )


def _quote_topic(key: MarketSubscriptionKey) -> dict[str, str]:
    return {
        "topic": "tick",
        "symbol": key.instrument.native_symbol,
        "asset_type": key.instrument.product_type,
    }




def _valid_ref(value: object) -> bool:
    if type(value) is not str or not value or value != value.strip() or not value.isprintable():
        return False
    try:
        return 0 < len(value.encode("utf-8", errors="strict")) <= 256
    except UnicodeEncodeError:
        return False


def _valid_timeout(value: object, maximum: float) -> bool:
    if type(value) not in (int, float) or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(number) and 0 < number <= maximum


__all__ = [
    "BoundedCtpQuoteV2Queue",
    "CtpRecoveryMemberSnapshot",
    "CtpQuoteV2MarketHandle",
    "CtpQuoteV2MarketProvider",
    "CtpQuoteV2MarketProviderError",
]
