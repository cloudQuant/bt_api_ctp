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
            "CTP_MARKET_SESSION_KEY_MISMATCH",
            "CTP_MARKET_UNSUBSCRIBE_FAILED",
            "CTP_MARKET_STOP_UNCONFIRMED",
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


@dataclass
class _PendingStreamCleanup:
    stream: CtpMarketStream
    native_client: object | None
    native_stop_confirmed: bool


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

    @property
    def diagnostics(self) -> dict[str, int]:
        return {
            "pending_start_cleanup": len(self._pending_start_cleanup),
            "active_handles": len(self._active_handles),
            "active_sessions": int(self._stream is not None),
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
        if len(self._active_handles) > 1:
            try:
                handle.stream.unsubscribe_topics(
                    [{"topic": "tick", "symbol": handle.key.instrument.native_symbol}]
                )
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
        """Retry every exact handle and startup cleanup still owned here.

        GatewayMarketHub normally releases active handles first. This provider
        close hook covers a native session whose startup failed before the Hub
        received a handle; an unconfirmed stop remains retained for a later
        owner-thread retry.
        """

        self._check_owner()
        for handle in tuple(self._active_handles.values()):
            self.unsubscribe(handle)
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
        if self._current_source_scope(handle.stream) is not handle.source_scope:
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SOURCE_SCOPE_CHANGED")
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
        scope = self._current_source_scope(handle.stream)
        if scope is None or scope is not handle.source_scope:
            return None
        try:
            return MarketSourceDescriptor(
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
        except Exception:
            return None

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
        except CtpQuoteV2MarketProviderError:
            native_client = native_client or getattr(stream, "_md_client", None)
            stopped, native_stop_confirmed = self._stop_stream(
                stream, native_client, native_stop_confirmed
            )
            if not stopped:
                self._pending_start_cleanup.append(
                    _PendingStreamCleanup(stream, native_client, native_stop_confirmed)
                )
            raise
        except Exception:
            native_client = native_client or getattr(stream, "_md_client", None)
            stopped, native_stop_confirmed = self._stop_stream(
                stream, native_client, native_stop_confirmed
            )
            if not stopped:
                self._pending_start_cleanup.append(
                    _PendingStreamCleanup(stream, native_client, native_stop_confirmed)
                )
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_START_FAILED") from None

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
        if (
            stream is None
            or fanout is None
            or scope is None
            or self._current_source_scope(stream) is not scope
        ):
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SOURCE_SCOPE_CHANGED")
        subscribed = getattr(stream, "_quote_v2_subscription_metadata", {})
        if type(subscribed) is not dict or set(subscribed) != set(self._handles_by_symbol):
            stream._fail_closed_quote_v2_subscription()
            self._source_scope = None
            raise CtpQuoteV2MarketProviderError("CTP_MARKET_SUBSCRIBE_FAILED")
        callback_queue = BoundedCtpQuoteV2Queue(self._queue_capacity)
        symbol = key.instrument.native_symbol
        fanout.register(symbol, callback_queue)
        ambiguous_symbols = {symbol}
        try:
            # Adding a symbol advances CtpMarketStream's callback epoch for
            # the active topic set. Drop queued rows with the previous epoch
            # before changing that fence; they cannot be safely re-labelled.
            for active in self._active_handles.values():
                active.queue.clear()
            stream.subscribe_topics([_quote_topic(key)])
            subscribed = getattr(stream, "_quote_v2_subscription_metadata", {})
            expected = set(self._handles_by_symbol) | {symbol}
            if type(subscribed) is not dict or set(subscribed) != expected:
                if type(subscribed) is dict:
                    ambiguous_symbols.update(expected.symmetric_difference(subscribed))
                raise CtpQuoteV2MarketProviderError("CTP_MARKET_SUBSCRIBE_FAILED")
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

    @staticmethod
    def _current_source_scope(stream: CtpMarketStream) -> CtpQuoteClockSourceScope | None:
        try:
            scope = stream.get_quote_clock_source_scope()
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
                scope = stream.get_quote_clock_source_scope()
            except Exception:
                scope = None
            if scope is not None:
                return scope
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
