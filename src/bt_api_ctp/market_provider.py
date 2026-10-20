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
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from typing import Callable

from bt_api_base.market_protocol import (
    MarketProviderEvent,
    MarketSourceDescriptor,
    MarketSubscriptionKey,
)

from bt_api_ctp.containers.ctp.ctp_ticker import CtpTickerData
from bt_api_ctp.ctp.client import CtpNativeStopReceipt
from bt_api_ctp.feeds.live_ctp_feed import (
    ConnectionState,
    CtpMarketStream,
    CtpMarketStreamSubscriptionError,
    CtpQuoteClockSourceScope,
    _get_ctp_managed_quote_v2_receipt,
)

DEFAULT_CTP_QUOTE_QUEUE_CAPACITY = 1_024
MAX_CTP_QUOTE_QUEUE_CAPACITY = 65_536
MAX_CTP_PROVIDER_POLL_LIMIT = 256
MAX_CTP_QUOTE_EVENT_BYTES = 256 * 1024
MAX_CTP_CONNECT_TIMEOUT_SECONDS = 300.0
MAX_CTP_STOP_TIMEOUT_SECONDS = 30.0


class CtpQuoteV2MarketProviderError(RuntimeError):
    """Finite, value-free failure from the CTP market provider boundary."""

    _CODES = frozenset(
        {
            "INVALID_CTP_MARKET_PROVIDER_CONFIG",
            "INVALID_CTP_MARKET_KEY",
            "UNSUPPORTED_CTP_MARKET_KEY",
            "CTP_MARKET_OWNER_THREAD_REQUIRED",
            "CTP_MARKET_STARTUP_CLEANUP_PENDING",
            "CTP_MARKET_STREAM_FACTORY_FAILED",
            "CTP_MARKET_STREAM_INVALID",
            "CTP_MARKET_STREAM_NOT_FRESH",
            "CTP_MARKET_SUBSCRIBE_FAILED",
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
        }
    )

    def __init__(self, code: str) -> None:
        self.code = (
            code if type(code) is str and code in self._CODES else "CTP_MARKET_PROVIDER_FAILED"
        )
        super().__init__(self.code)


class BoundedCtpQuoteV2Queue:
    """Thread-safe, nonblocking sink for ``CtpMarketStream.push_data``."""

    def __init__(self, capacity: int = DEFAULT_CTP_QUOTE_QUEUE_CAPACITY) -> None:
        if type(capacity) is not int or not 1 <= capacity <= MAX_CTP_QUOTE_QUEUE_CAPACITY:
            raise CtpQuoteV2MarketProviderError("INVALID_CTP_MARKET_PROVIDER_CONFIG")
        self._capacity = capacity
        self._items: deque[object] = deque()
        self._lock = threading.Lock()
        self._dropped_oldest = 0

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
            self._items.append(item)

    def drain(self, limit: int) -> tuple[object, ...]:
        if type(limit) is not int or not 1 <= limit <= MAX_CTP_PROVIDER_POLL_LIMIT:
            raise CtpQuoteV2MarketProviderError("INVALID_CTP_MARKET_PROVIDER_CONFIG")
        with self._lock:
            count = min(limit, len(self._items))
            return tuple(self._items.popleft() for _ in range(count))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


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


@dataclass
class CtpQuoteV2MarketHandle:
    """One exact Hub key/registration projected from the owner's MD session."""

    key: MarketSubscriptionKey
    generation: str
    queue: BoundedCtpQuoteV2Queue
    stream: CtpMarketStream
    source_scope: CtpQuoteClockSourceScope
    owner_token: object
    active: bool = True
    source_epoch_reset_pending: bool = False
    source_epoch_reset_epoch: str | None = None
    last_hub_epoch: str | None = None


@dataclass
class _PendingStreamCleanup:
    stream: CtpMarketStream
    native_client: object | None
    native_stop_confirmed: bool


@dataclass
class _SourceRecovery:
    stream: CtpMarketStream
    old_client: object
    old_client_generation: int
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


class CtpQuoteV2MarketProvider:
    """Adapt one native CTP quote-v2 subscription to the Gateway provider port.

    ``stream_factory`` is trusted deployment code. It must construct a fresh,
    disconnected ``CtpMarketStream`` for the exact key and queue without
    starting it or pre-registering topics. Credentials, fronts, and any
    provider-specific diagnostic metadata remain inside that closure. The
    provider registers the sole ``ctp.quote.v2`` tick topic itself, then
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
    ) -> None:
        if (
            not callable(stream_factory)
            or not _valid_ref(source_ref)
            or not _valid_ref(entitlement_ref)
            or type(queue_capacity) is not int
            or not 1 <= queue_capacity <= MAX_CTP_QUOTE_QUEUE_CAPACITY
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
        self._owner_token = object()
        self._active_handles: dict[int, CtpQuoteV2MarketHandle] = {}
        self._handles_by_symbol: dict[str, CtpQuoteV2MarketHandle] = {}
        self._stream: CtpMarketStream | None = None
        self._fanout: _CtpQuoteV2Fanout | None = None
        self._native_client: object | None = None
        self._source_scope: CtpQuoteClockSourceScope | None = None
        self._session_environment: str | None = None
        self._pending_start_cleanup: list[_PendingStreamCleanup] = []
        self._invalid_events = 0
        self._source_recovery: _SourceRecovery | None = None

    @property
    def diagnostics(self) -> dict[str, int]:
        return {
            "pending_start_cleanup": len(self._pending_start_cleanup),
            "active_handles": len(self._active_handles),
            "active_sessions": int(self._stream is not None),
            "source_recovery_active": int(self._source_recovery is not None),
            "invalid_events": self._invalid_events,
            "dropped_oldest": sum(
                handle.queue.dropped_oldest for handle in self._active_handles.values()
            ),
        }

    def subscribe(self, key: MarketSubscriptionKey, *, generation: str) -> CtpQuoteV2MarketHandle:
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
        return self._subscribe_existing_stream(key, generation)

    def unsubscribe(self, handle: object) -> bool:
        self._check_owner()
        if (
            type(handle) is not CtpQuoteV2MarketHandle
            or handle.owner_token is not self._owner_token
        ):
            return False
        if not handle.active:
            return True
        if self._active_handles.get(id(handle)) is not handle:
            return False
        if self._source_recovery is not None:
            return False
        if len(self._active_handles) > 1:
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
        handle.active = False
        handle.queue.clear()
        self._active_handles.pop(id(handle), None)
        self._handles_by_symbol.pop(handle.key.instrument.native_symbol, None)
        self._reset_active_stream()
        return native_stop_confirmed

    def close(self) -> bool:
        """Stop the exact shared native session before discarding ownership.

        A full owner close is the recovery boundary for retired-symbol
        tombstones and bypasses per-symbol unsubscribe limits. On an
        unconfirmed native stop, every handle and the stream remain retained
        for a later owner-thread retry.
        """

        self._check_owner()
        stream = self._stream
        recovery = self._source_recovery
        recovery_stop_confirmed = False
        if recovery is not None:
            worker = recovery.stop_worker
            reconnect_worker = recovery.reconnect_worker
            receipt = recovery.stop_receipt
            if (
                recovery.state != "failed"
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
                or recovery.stream is not stream
            ):
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
                handle.active = False
                handle.queue.clear()
            self._active_handles.clear()
            self._handles_by_symbol.clear()
            self._reset_active_stream()
        try:
            self._retry_pending_start_cleanup()
        except CtpQuoteV2MarketProviderError:
            return False
        return not self._active_handles and not self._pending_start_cleanup and self._stream is None

    def poll(self, handle: object, *, limit: int) -> tuple[MarketProviderEvent, ...]:
        self._check_owner()
        if type(limit) is not int or not 1 <= limit <= MAX_CTP_PROVIDER_POLL_LIMIT:
            raise CtpQuoteV2MarketProviderError("INVALID_CTP_MARKET_PROVIDER_CONFIG")
        if (
            type(handle) is not CtpQuoteV2MarketHandle
            or handle.owner_token is not self._owner_token
            or not handle.active
            or self._active_handles.get(id(handle)) is not handle
        ):
            return ()
        stream = handle.stream
        if self._source_recovery is not None:
            self._drive_source_recovery()
            return ()
        if stream.quote_v2_service_recovery_required:
            self._begin_source_recovery(stream)
            if self._source_recovery is not None:
                self._drive_source_recovery()
            return ()
        if stream.quote_v2_service_fail_closed:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SOURCE_SCOPE_CHANGED")
        current_scope = self._current_source_scope(stream)
        if current_scope is None:
            return ()
        if current_scope is not handle.source_scope:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SOURCE_SCOPE_CHANGED")
        if (
            stream.is_quote_v2_service_symbol_ready(handle.key.instrument.native_symbol) is not True
            or handle.source_epoch_reset_pending
        ):
            return ()
        raw_items = handle.queue.drain(limit)
        events: list[MarketProviderEvent] = []
        for item in raw_items:
            try:
                events.append(_project_native_tick(item, handle))
            except Exception:
                self._invalid_events += 1
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_EVENT_INVALID") from None
        return tuple(events)

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
        if (
            type(handle) is not CtpQuoteV2MarketHandle
            or handle.owner_token is not self._owner_token
            or not handle.active
            or self._active_handles.get(id(handle)) is not handle
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
        if (
            type(handle) is not CtpQuoteV2MarketHandle
            or handle.owner_token is not self._owner_token
            or not handle.active
            or self._active_handles.get(id(handle)) is not handle
        ):
            return False
        recovery = self._source_recovery
        return bool(
            recovery is not None
            and recovery.state == "reset_pending"
            and handle.source_scope is recovery.new_scope
            and handle.source_epoch_reset_pending is True
        )

    def acknowledge_source_epoch_reset(self, handle: object, *, epoch: str) -> bool:
        """Accept only the Hub epoch bound to this exact current handle."""

        self._check_owner()
        if (
            type(handle) is not CtpQuoteV2MarketHandle
            or handle.owner_token is not self._owner_token
            or not handle.active
            or self._active_handles.get(id(handle)) is not handle
            or type(epoch) is not str
            or not epoch
        ):
            return False
        recovery = self._source_recovery
        current_scope = self._current_source_scope(handle.stream)
        if (
            recovery is None
            or recovery.state != "reset_pending"
            or recovery.new_scope is None
            or handle.stream is not recovery.stream
            or handle.source_scope is not recovery.new_scope
            or current_scope is not recovery.new_scope
            or handle.source_epoch_reset_pending is not True
            or (handle.last_hub_epoch is not None and epoch == handle.last_hub_epoch)
        ):
            if recovery is not None and recovery.state == "reset_pending":
                self._fail_source_recovery()
            return False
        handle.source_epoch_reset_pending = False
        handle.source_epoch_reset_epoch = epoch
        handle.last_hub_epoch = epoch
        if all(not active.source_epoch_reset_pending for active in self._active_handles.values()):
            self._source_scope = recovery.new_scope
            self._native_client = getattr(recovery.stream, "_md_client", None)
            self._source_recovery = None
        return True

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
        if type(subscribed) is not dict or set(subscribed) != set(self._handles_by_symbol):
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
            expected = set(self._handles_by_symbol) | {symbol}
            if type(subscribed) is not dict or set(subscribed) != expected:
                if type(subscribed) is dict:
                    ambiguous_symbols.update(expected.symmetric_difference(subscribed))
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_SUBSCRIBE_FAILED")
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

    def _register_handle(
        self,
        key: MarketSubscriptionKey,
        generation: str,
        callback_queue: BoundedCtpQuoteV2Queue,
        stream: CtpMarketStream,
        scope: CtpQuoteClockSourceScope,
    ) -> CtpQuoteV2MarketHandle:
        handle = CtpQuoteV2MarketHandle(
            key=key,
            generation=generation,
            queue=callback_queue,
            stream=stream,
            source_scope=scope,
            owner_token=self._owner_token,
        )
        self._active_handles[id(handle)] = handle
        self._handles_by_symbol[key.instrument.native_symbol] = handle
        return handle

    def _begin_source_recovery(self, stream: CtpMarketStream) -> None:
        if (
            self._source_recovery is not None
            or stream is not self._stream
            or stream.quote_v2_service_recovery_required is not True
            or stream.quote_v2_service_fail_closed is True
            or not self._active_handles
            or any(handle.stream is not stream for handle in self._active_handles.values())
        ):
            return
        old_client = self._native_client
        old_scope = self._source_scope
        handles = tuple(self._active_handles.values())
        symbols = frozenset(handle.key.instrument.native_symbol for handle in handles)
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

    def _drive_source_recovery(self) -> None:
        recovery = self._source_recovery
        if recovery is None or recovery.state == "failed":
            return
        reconnect_worker = recovery.reconnect_worker
        reconnect_inflight = (
            recovery.state == "reconnecting"
            and reconnect_worker is not None
            and reconnect_worker.is_alive()
        )
        if time.monotonic() >= recovery.deadline:
            if reconnect_inflight:
                # CtpMarketStream.connect holds its callback lock across the
                # synchronous vendor Create/Register/Init calls. Do not take
                # that lock from the Gateway poll thread while startup is in
                # flight; revoking the provider recovery state is sufficient
                # to keep this stream from publishing, and the worker will
                # mark the stream itself fail-closed if it eventually returns.
                self._fail_source_recovery_without_stream_lock(recovery)
                return
            self._fail_source_recovery()
            return
        stream = recovery.stream
        current_symbols = frozenset(
            handle.key.instrument.native_symbol for handle in self._active_handles.values()
        )
        subscribed = getattr(stream, "_quote_v2_subscription_metadata", None)
        if (
            stream is not self._stream
            or current_symbols != recovery.symbols
            or type(subscribed) is not dict
            or set(subscribed) != set(recovery.symbols)
            or (
                not reconnect_inflight
                and stream.quote_v2_service_binding_matches() is not True
            )
            or any(
                handle.source_scope is not recovery.old_scope
                and handle.source_scope is not recovery.new_scope
                for handle in self._active_handles.values()
            )
        ):
            self._fail_source_recovery()
            return

        if recovery.state == "stopping":
            if recovery.stop_worker is None:
                if not self._start_source_recovery_stop_worker(recovery):
                    self._fail_source_recovery()
                return
            if not recovery.stop_done.is_set():
                return
            receipt = recovery.stop_receipt
            if (
                recovery.stop_error is not None
                or recovery.stop_timed_out
                or type(receipt) is not CtpNativeStopReceipt
                or receipt.complete is not True
                or type(receipt.connection_generation) is not int
                or type(recovery.old_client_generation) is not int
                or receipt.connection_generation != recovery.old_client_generation
            ):
                self._fail_source_recovery()
                return
            if (
                getattr(stream, "_md_client", None) is not None
                or stream.state is not ConnectionState.DISCONNECTED
            ):
                self._fail_source_recovery()
                return
            recovery.state = "reconnecting"
            if not self._start_source_recovery_reconnect_worker(recovery, receipt):
                self._fail_source_recovery()
            return

        if recovery.state == "reconnecting":
            worker = recovery.reconnect_worker
            if worker is None:
                self._fail_source_recovery()
                return
            # Never inspect a stream property guarded by _md_callback_lock
            # while native startup is executing under that lock.
            if worker.is_alive():
                return
            if not recovery.reconnect_done.is_set():
                self._fail_source_recovery()
                return
            new_client = recovery.reconnected_client
            if (
                recovery.reconnect_error is not None
                or recovery.reconnect_succeeded is not True
                or new_client is None
                or new_client is recovery.old_client
                or getattr(stream, "_md_client", None) is not new_client
                or type(getattr(stream, "_connection_generation", None)) is not int
                or stream._connection_generation <= recovery.old_stream_generation
                or stream.state
                not in (ConnectionState.CONNECTING, ConnectionState.AUTHENTICATED)
                or stream.quote_v2_service_fail_closed is True
                or stream.quote_v2_service_binding_matches() is not True
                or frozenset(getattr(stream, "_quote_v2_subscription_metadata", {}))
                != recovery.symbols
            ):
                self._fail_source_recovery()
                return
            self._native_client = new_client
            recovery.state = "connecting"
            return

        if recovery.state == "connecting":
            if stream.quote_v2_service_fail_closed is True:
                self._fail_source_recovery()
                return
            new_scope = self._current_source_scope(stream)
            if new_scope is None:
                if stream.state is ConnectionState.ERROR:
                    self._fail_source_recovery()
                return
            if not self._valid_reconnected_scope(recovery, new_scope):
                self._fail_source_recovery()
                return
            recovery.new_scope = new_scope
            recovery.state = "reset_pending"
            for handle in self._active_handles.values():
                handle.source_scope = new_scope
                handle.source_epoch_reset_pending = True
                handle.source_epoch_reset_epoch = None
            return

        if recovery.state == "reset_pending":
            if recovery.new_scope is None or any(
                handle.source_scope is not recovery.new_scope
                for handle in self._active_handles.values()
            ):
                self._fail_source_recovery()

    @staticmethod
    def _observe_source_recovery_stop(recovery: _SourceRecovery) -> None:
        """Observe one retained client's stop off the Gateway owner thread.

        The first typed stop call initiates cleanup; subsequent zero-time calls
        only inspect that same MdClient's retained native session. A worker that
        blocks in vendor code remains strongly referenced by ``recovery`` and
        can never authorize a replacement client by itself.
        """

        try:
            stop_and_wait = getattr(recovery.old_client, "stop_and_wait", None)
            if not callable(stop_and_wait):
                recovery.stop_error = RuntimeError("ctp_native_stop_unavailable")
                return
            while time.monotonic() < recovery.deadline:
                recovery.stop_receipt_attempts += 1
                receipt = stop_and_wait(timeout=0.0)
                if (
                    type(receipt) is not CtpNativeStopReceipt
                    or type(recovery.old_client_generation) is not int
                    or type(receipt.connection_generation) is not int
                    or receipt.connection_generation != recovery.old_client_generation
                ):
                    recovery.stop_error = RuntimeError("ctp_native_stop_receipt_invalid")
                    return
                recovery.stop_receipt = receipt
                if receipt.complete is True:
                    return
                if receipt.complete is not False:
                    recovery.stop_error = RuntimeError("ctp_native_stop_receipt_invalid")
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
        except BaseException as exc:
            recovery.reconnected_client = getattr(stream, "_md_client", None)
            recovery.reconnect_error = exc
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

    def _fail_source_recovery(self, stream: CtpMarketStream | None = None) -> None:
        recovery = self._source_recovery
        if recovery is not None:
            worker = recovery.reconnect_worker
            if worker is not None and worker.is_alive():
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

    def _remove_handle(self, handle: CtpQuoteV2MarketHandle) -> None:
        handle.active = False
        handle.queue.clear()
        self._active_handles.pop(id(handle), None)
        self._handles_by_symbol.pop(handle.key.instrument.native_symbol, None)

    def _reset_active_stream(self) -> None:
        self._stream = None
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
        if type(key) is not MarketSubscriptionKey:
            raise CtpQuoteV2MarketProviderError("INVALID_CTP_MARKET_KEY")
        instrument = key.instrument
        if (
            key.source_ref != self._source_ref
            or key.entitlement_ref != self._entitlement_ref
            or instrument.provider != "ctp"
            or key.channel != "ctp.quote.v2"
            or key.depth is not None
            or key.timeframe is not None
            or instrument.instrument_id != instrument.native_symbol
        ):
            raise CtpQuoteV2MarketProviderError("UNSUPPORTED_CTP_MARKET_KEY")

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

    def _check_owner(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_OWNER_THREAD_REQUIRED")


def _project_native_tick(item: object, handle: CtpQuoteV2MarketHandle) -> MarketProviderEvent:
    if type(item) is not CtpTickerData:
        raise ValueError("invalid ticker type")
    receipt = _get_ctp_managed_quote_v2_receipt(item, handle.stream)
    if type(receipt) is not dict:
        raise ValueError("missing native callback receipt")
    data = item.get_all_data()
    key = handle.key
    instrument = key.instrument
    if (
        type(data) is not dict
        or data.get("schema_version") != "ctp.quote.v2"
        or data.get("exchange_name") != "CTP"
        or data.get("exchange_id") != instrument.venue
        or data.get("asset_type") != instrument.product_type
        or data.get("instrument_id") != instrument.instrument_id
        or data.get("symbol") != instrument.native_symbol
        or receipt.get("symbol") != instrument.native_symbol
        or receipt.get("source") != "ctp.native.md"
        or receipt.get("source") != handle.source_scope.source
        or receipt.get("connection_generation") != handle.source_scope.connection_generation
        or receipt.get("clock_domain_id") != handle.source_scope.clock_domain_id
        or receipt.get("clock_source_session_id") != handle.source_scope.clock_source_session_id
        or receipt.get("clock_source") != handle.source_scope.clock_source
        or receipt.get("connection_generation") != data.get("connection_generation")
        or receipt.get("subscription_epoch") != data.get("subscription_epoch")
        or receipt.get("ingest_seq") != data.get("ingest_seq")
        or receipt.get("clock_domain_id") != data.get("clock_domain_id")
        or receipt.get("source_clock_quality") != "unknown"
        or receipt.get("receive_clock_quality") != "unknown"
        or receipt.get("source_clock_error_ms") is not None
        or receipt.get("receive_clock_error_ms") is not None
        or receipt.get("freshness_verified") is not False
        or receipt.get("execution_qualified") is not False
    ):
        raise ValueError("ticker identity or native receipt mismatch")

    observed_at_utc = _datetime_utc_text(data.get("recv_time_utc"))
    projected = dict(data)
    projected["event_time_utc"] = _datetime_utc_text(data.get("event_time_utc"))
    projected["recv_time_utc"] = observed_at_utc
    for field in (
        "source",
        "rules_hash",
        "clock_domain_id",
        "connection_generation",
        "subscription_epoch",
        "ingest_seq",
        "source_clock_quality",
        "receive_clock_quality",
        "source_clock_error_ms",
        "receive_clock_error_ms",
        "freshness_verified",
    ):
        projected[field] = receipt[field]
    projected["clock_source_session_id"] = receipt["clock_source_session_id"]
    projected["clock_source"] = receipt["clock_source"]
    projected["execution_qualified"] = False
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
        observed_at_utc=observed_at_utc,
        data=projected,
    )


def _quote_topic(key: MarketSubscriptionKey) -> dict[str, str]:
    return {
        "topic": "tick",
        "symbol": key.instrument.native_symbol,
        "asset_type": key.instrument.product_type,
    }


def _datetime_utc_text(value: object) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid CTP timestamp")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


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
    "CtpQuoteV2MarketHandle",
    "CtpQuoteV2MarketProvider",
    "CtpQuoteV2MarketProviderError",
]
