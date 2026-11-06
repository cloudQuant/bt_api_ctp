"""In-process (direct) market-stream provider for CTP ticks.

Registered for ``bt_api.market_stream_providers`` / ``CTP___FUTURE``.  It hosts
the existing :class:`~bt_api_ctp.market_provider.CtpQuoteV2MarketProvider` (the
same native quote-v2 adapter the Gateway Market Hub uses) inside the SDK's
direct ``MarketStreamPort`` owner, so a direct ``BtApi`` consumer receives typed
``MarketTickEvent`` objects without a Gateway or a service-market runtime.

Fronts and credentials are taken from the SDK-selected CTP request feed of the
same route; nothing is read from the environment and nothing is logged.  Every
session call must happen on the thread that opened the session (the SDK owner
thread), because the wrapped provider is owner-thread confined.

Source restart (iteration 62, continuity-7): after a front disconnect the
wrapped provider stops the lost MD session, starts a new one and, once it is
logged in, acknowledged and ticking, reports a confirmed source restart.  The
session then fails one ``poll`` so the SDK owner marks every channel for
recovery (:meth:`CtpDirectMarketStreamSession.channel_recovery_code` names the
cause), holds the channels back, and ``resync`` -- called only after the
consumer explicitly acknowledged the data loss -- binds each channel to the
new source under a new epoch whose cursor starts again at 1.  No tick of the
lost session is delivered after the switch (the provider fences its callbacks
and discarded its queues when the recovery began).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from bt_api_base.market_protocol import (
    MarketInstrumentKey,
    MarketSubscriptionKey,
)
from bt_api_base.market_stream_protocol import MarketStreamRequest
from bt_api_base.market_stream_provider_protocol import (
    MarketStreamChannelCapability,
    MarketStreamProviderBinding,
    MarketStreamProviderChannelBinding,
    MarketStreamProviderContractError,
    MarketStreamProviderRegistryPort,
    SelectedMarketStreamRoute,
)
from bt_api_base.market_tick_protocol import (
    MarketEventCursor,
    MarketTickPayload,
    bind_market_tick_event,
)

CTP_MARKET_STREAM_EXCHANGE = "CTP___FUTURE"
# The recovery reason this session declares to the SDK owner for a confirmed
# source restart (an existing neutral code: the provider requires a resync).
SOURCE_RESTART_RECOVERY_CODE = "PROVIDER_RESYNC_REQUIRED"
_DIRECT_SOURCE_REF = "ctp-direct-market"
_DIRECT_ENTITLEMENT_REF = "ctp-direct-entitlement"
_SOURCE_READY_TIMEOUT_SECONDS = 30.0
_MAX_PROVIDER_BATCH = 256
_MAX_POLL_TIMEOUT_MS = 60_000


def _invalid(code: str = "MARKET_STREAM_PROVIDER_SESSION_INVALID") -> MarketStreamProviderContractError:
    return MarketStreamProviderContractError(code)


def _validate_route(route: object) -> SelectedMarketStreamRoute:
    from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

    if (
        type(route) is not SelectedMarketStreamRoute
        or route.exchange_name != CTP_MARKET_STREAM_EXCHANGE
        or type(route.client) is not CtpRequestDataFuture
        or type(route.instrument) is not MarketInstrumentKey
        or route.instrument.provider != "ctp"
        or route.instrument.instrument_id != route.instrument.native_symbol
    ):
        raise _invalid("MARKET_STREAM_PROVIDER_ROUTE_INVALID")
    return route


class CtpDirectMarketStreamAdapter:
    """Pure route-bound adapter: declares the tick channel, opens sessions."""

    def __init__(self, route: SelectedMarketStreamRoute) -> None:
        self._route = _validate_route(route)

    def capabilities(self) -> tuple[MarketStreamChannelCapability, ...]:
        """Declare the native tick channel; CTP publishes no order book here."""
        return (MarketStreamChannelCapability(channel="tick", max_depth=None),)

    def open_session(
        self,
        *,
        wake_callback: Callable[[], None],
    ) -> CtpDirectMarketStreamSession:
        """Create an unstarted session shell; the first subscribe connects MD."""
        if not callable(wake_callback):
            raise _invalid()
        return CtpDirectMarketStreamSession(self._route, wake_callback=wake_callback)


@dataclass
class _Channel:
    binding: MarketStreamProviderChannelBinding
    handle: object
    sequence: int = 0
    # A confirmed source restart was reported; the channel delivers nothing
    # until the owner's resync hands it a new epoch.
    awaiting_resync: bool = False
    # The new-epoch binding prepared for this channel when a sibling's resync
    # acknowledged the restart for the whole session.
    pending_binding: MarketStreamProviderChannelBinding | None = None


def _new_epoch() -> str:
    return f"ctp-direct-epoch-{uuid4().hex}"


class CtpDirectMarketStreamSession:
    """One in-process MD session multiplexing tick subscriptions for a route."""

    def __init__(
        self,
        route: SelectedMarketStreamRoute,
        *,
        wake_callback: Callable[[], None],
    ) -> None:
        self._route = _validate_route(route)
        if not callable(wake_callback):
            raise _invalid()
        self._wake_callback = wake_callback
        self._owner_thread = threading.get_ident()
        self._provider: object | None = None
        self._channels: dict[object, _Channel] = {}
        self._generation = f"ctp-direct-{uuid4().hex[:16]}"
        self._epoch = f"ctp-direct-epoch-{uuid4().hex}"
        self._closed = False

    # -- provider plumbing -------------------------------------------------
    def _stream_factory(self, key: MarketSubscriptionKey, quote_queue: object) -> object:
        """Build a fresh, unstarted CtpMarketStream from the selected feed."""
        from bt_api_ctp.feeds.live_ctp_feed import CtpMarketStream

        if type(key) is not MarketSubscriptionKey:
            raise _invalid()
        feed = self._route.client
        return CtpMarketStream(
            data_queue=quote_queue,
            broker_id=feed.broker_id,
            user_id=feed.user_id,
            password=feed.password,
            td_front=feed.td_front,
            md_front=feed.md_front,
            ctp_env_profile=str(feed.ctp_env_profile or "").strip(),
            auto_detect_fronts=False,
            asset_type=key.instrument.product_type.upper(),
            allow_zero_md_login_response=True,
        )

    def _ensure_provider(self, queue_capacity: int) -> object:
        from bt_api_ctp.market_provider import (
            MAX_CTP_QUOTE_QUEUE_CAPACITY,
            CtpQuoteV2MarketProvider,
        )

        if self._provider is None:
            self._provider = CtpQuoteV2MarketProvider(
                stream_factory=self._stream_factory,
                source_ref=_DIRECT_SOURCE_REF,
                entitlement_ref=_DIRECT_ENTITLEMENT_REF,
                queue_capacity=max(1, min(int(queue_capacity), MAX_CTP_QUOTE_QUEUE_CAPACITY)),
                connect_timeout_seconds=30.0,
                stop_timeout_seconds=5.0,
            )
        return self._provider

    def _check_owner(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise _invalid()

    def _validate_request(self, request: object) -> MarketStreamRequest:
        if (
            type(request) is not MarketStreamRequest
            or request.exchange_name != CTP_MARKET_STREAM_EXCHANGE
            or request.channel != "tick"
            or request.depth is not None
            or request.instrument != self._route.instrument
        ):
            raise _invalid("MARKET_STREAM_PROVIDER_BINDING_INVALID")
        return request

    # -- MarketStreamProviderSession ----------------------------------------
    def subscribe(self, request: MarketStreamRequest, /) -> MarketStreamProviderChannelBinding:
        """Subscribe one tick channel and wait until its source is described."""
        self._check_owner()
        self._validate_request(request)
        if self._closed:
            raise _invalid()
        key = MarketSubscriptionKey(
            source_ref=_DIRECT_SOURCE_REF,
            entitlement_ref=_DIRECT_ENTITLEMENT_REF,
            instrument=request.instrument,
            channel="tick",
        )
        if any(channel.binding.key == key for channel in self._channels.values()):
            raise _invalid("MARKET_STREAM_PROVIDER_BINDING_INVALID")
        provider = self._ensure_provider(request.queue_capacity)
        handle = provider.subscribe(key, generation=self._generation)
        deadline = time.monotonic() + _SOURCE_READY_TIMEOUT_SECONDS
        source = None
        while source is None:
            source = provider.describe_source(
                handle,
                key=key,
                generation=self._generation,
                stream_id=key.stream_id,
                epoch=self._epoch,
            )
            if source is not None:
                break
            if time.monotonic() >= deadline:
                provider.unsubscribe(handle)
                raise _invalid()
            time.sleep(0.05)
        binding = MarketStreamProviderChannelBinding(
            token=object(),
            request=request,
            key=key,
            source=source,
            initial_cursor=None,
            initial_events=(),
            recovery_required=False,
        )
        self._channels[binding.token] = _Channel(binding, handle)
        return binding

    def poll(self, *, timeout_ms: int, max_events: int) -> tuple[object, ...]:
        """Return source-bound tick events, waiting up to ``timeout_ms``."""
        from bt_api_ctp.market_provider import MAX_CTP_PROVIDER_POLL_LIMIT

        self._check_owner()
        if (
            type(timeout_ms) is not int
            or not 0 <= timeout_ms <= _MAX_POLL_TIMEOUT_MS
            or type(max_events) is not int
            or not 1 <= max_events <= _MAX_PROVIDER_BATCH
        ):
            raise _invalid("MARKET_STREAM_PROVIDER_EVENT_INVALID")
        if self._closed:
            raise _invalid()
        provider = self._provider
        if provider is None or not self._channels:
            return ()
        deadline = time.monotonic() + timeout_ms / 1000.0
        events: list[object] = []
        while True:
            # Progress a source recovery even while every channel is held.
            provider.maintain()
            if self._note_source_restart(provider):
                # A confirmed native source restart needs a new epoch: fail
                # this poll so the SDK owner marks the channels for recovery.
                raise _invalid("MARKET_STREAM_PROVIDER_EVENT_INVALID")
            for channel in self._channels.values():
                if channel.awaiting_resync:
                    continue
                room = max_events - len(events)
                if room <= 0:
                    break
                source = channel.binding.source
                for item in provider.poll(
                    channel.handle, limit=min(room, MAX_CTP_PROVIDER_POLL_LIMIT)
                ):
                    payload = MarketTickPayload.from_dict(item.data)
                    channel.sequence += 1
                    events.append(
                        bind_market_tick_event(
                            payload,
                            source,
                            MarketEventCursor(source.stream_id, source.epoch, channel.sequence),
                        )
                    )
            remaining = deadline - time.monotonic()
            if events or remaining <= 0:
                return tuple(events)
            time.sleep(min(0.005, remaining))

    def resync(
        self,
        binding: MarketStreamProviderChannelBinding,
        *,
        acknowledge_data_loss: bool,
    ) -> MarketStreamProviderChannelBinding:
        """Rebind one channel under a new epoch after acknowledged data loss.

        After a confirmed source restart the channel is bound to the new MD
        session (the first resync of the restart acknowledges it for every
        held channel of this session).  Otherwise -- a loss on the consumer
        side -- it keeps the current source under a new epoch.  A tick channel
        has no book to rebuild: the binding carries no initial events and its
        cursor starts again at 1.  Refused while no current source exists
        (the provider is still recovering, or its recovery failed).
        """
        self._check_owner()
        if self._closed:
            raise _invalid()
        if acknowledge_data_loss is not True:
            raise _invalid("MARKET_STREAM_PROVIDER_BINDING_INVALID")
        channel = self._channels.get(getattr(binding, "token", None))
        if channel is None or channel.binding is not binding:
            raise _invalid("MARKET_STREAM_PROVIDER_BINDING_INVALID")
        provider = self._provider
        if provider is None:
            raise _invalid()
        if channel.awaiting_resync:
            if channel.pending_binding is None:
                if provider.source_epoch_reset_required(channel.handle) is True:
                    self._rebind_restarted_source(provider)
                else:
                    # The provider already took this restart's acknowledgement
                    # (an earlier rebind failed afterwards) or is still
                    # recovering: bind to its current source under a new
                    # epoch, refused while no current source exists.
                    channel.pending_binding = self._binding_for(provider, channel, _new_epoch())
            rebound = channel.pending_binding
            if rebound is None:
                raise _invalid()
        else:
            if provider.source_epoch_reset_required(channel.handle) is True:
                # A restart this session has not reported yet: the next poll
                # reports it and the restart path rebinds every channel.
                raise _invalid()
            rebound = self._binding_for(provider, channel, _new_epoch())
        channel.binding = rebound
        channel.pending_binding = None
        channel.awaiting_resync = False
        channel.sequence = 0
        return rebound

    def channel_recovery_code(
        self, binding: MarketStreamProviderChannelBinding, /
    ) -> str | None:
        """Why this channel needs recovery, when this session knows it."""
        channel = self._channels.get(getattr(binding, "token", None))
        if channel is None or channel.binding is not binding:
            return None
        return SOURCE_RESTART_RECOVERY_CODE if channel.awaiting_resync else None

    def _note_source_restart(self, provider: object) -> bool:
        """Hold every channel back once the provider confirms a new source."""
        restarted = any(
            not channel.awaiting_resync
            and provider.source_epoch_reset_required(channel.handle) is True
            for channel in self._channels.values()
        )
        if not restarted:
            return False
        for channel in self._channels.values():
            channel.awaiting_resync = True
            channel.pending_binding = None
        return True

    def _rebind_restarted_source(self, provider: object) -> None:
        """Acknowledge a confirmed restart for every held channel at once.

        The provider keeps all channels quiet until each handle took its new
        epoch, so the session acknowledges them together and keeps the new
        bindings of the siblings until their own resync.
        """
        held = [channel for channel in self._channels.values() if channel.awaiting_resync]
        if not held or not all(
            provider.source_epoch_reset_required(channel.handle) is True for channel in held
        ):
            # No confirmed new source (still recovering, lost again, failed).
            raise _invalid()
        epochs: dict[int, str] = {}
        for channel in held:
            epoch = _new_epoch()
            if provider.acknowledge_source_epoch_reset(channel.handle, epoch=epoch) is not True:
                raise _invalid()
            epochs[id(channel)] = epoch
        for channel in held:
            channel.pending_binding = self._binding_for(provider, channel, epochs[id(channel)])

    def _binding_for(
        self, provider: object, channel: _Channel, epoch: str
    ) -> MarketStreamProviderChannelBinding:
        key = channel.binding.key
        source = provider.describe_source(
            channel.handle,
            key=key,
            generation=self._generation,
            stream_id=key.stream_id,
            epoch=epoch,
        )
        if source is None:
            raise _invalid()
        return MarketStreamProviderChannelBinding(
            token=channel.binding.token,
            request=channel.binding.request,
            key=key,
            source=source,
            initial_cursor=None,
            initial_events=(),
            recovery_required=False,
        )

    def unsubscribe(self, binding: MarketStreamProviderChannelBinding, /) -> bool:
        """Stop exactly one channel; sibling channels keep running."""
        self._check_owner()
        channel = self._channels.pop(getattr(binding, "token", None), None)
        if channel is None or self._provider is None:
            return False
        return bool(self._provider.unsubscribe(channel.handle))

    def close(self) -> bool:
        """Stop the shared native MD session after its channels are removed."""
        self._check_owner()
        self._closed = True
        provider = self._provider
        self._provider = None
        self._channels.clear()
        if provider is None:
            return True
        return bool(provider.close())


def _create_ctp_market_stream_binding(
    route: SelectedMarketStreamRoute,
) -> MarketStreamProviderBinding:
    adapter = CtpDirectMarketStreamAdapter(route)
    return MarketStreamProviderBinding(instrument=route.instrument, adapter=adapter)


def register_ctp_market_stream_provider(registry: MarketStreamProviderRegistryPort) -> None:
    """Register the exact CTP futures direct market-stream provider route."""
    if not isinstance(registry, MarketStreamProviderRegistryPort):
        raise _invalid("MARKET_STREAM_PROVIDER_ADAPTER_INVALID")
    registry.register(CTP_MARKET_STREAM_EXCHANGE, _create_ctp_market_stream_binding)


__all__ = [
    "CTP_MARKET_STREAM_EXCHANGE",
    "SOURCE_RESTART_RECOVERY_CODE",
    "CtpDirectMarketStreamAdapter",
    "CtpDirectMarketStreamSession",
    "register_ctp_market_stream_provider",
]
