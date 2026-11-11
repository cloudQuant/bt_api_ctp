"""Owner-thread CTP Gateway composition; no connections occur at construction.

The account reader owns the sole TD client, direct borrows that exact client,
while one quote provider owns the lazily started shared MD client. Configuration
labels are not account authentication or live-trading evidence.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bt_api_base.account_protocol import AccountKey
from bt_api_base.exceptions import ExchangeConnectionError
from bt_api_base.market_protocol import MarketInstrumentKey

from bt_api_ctp.account_provider import CtpReadOnlyAccountSessionProvider
from bt_api_ctp.ctp.client import TraderClient, _TraderSpi
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_ctp.feeds.live_ctp_feed import ConnectionState, CtpMarketStream, CtpRequestDataFuture
from bt_api_ctp.market_provider import CtpQuoteV2MarketProvider
from bt_api_ctp.gateway_return_buffer import CtpGatewayReturnBuffer, CtpGatewayReturnRecord
from bt_api_ctp.registered_sim_session import capture_query_session
from bt_api_ctp.gateway_return_query import CtpGatewayReturnQueryReader
from bt_api_ctp.trusted_flow import flow_binding, trusted_flow


_LOG = logging.getLogger(__name__)


class CtpGatewaySessionError(RuntimeError):
    """Finite local composition or owner-thread failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _timeout(value: object, maximum: float) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0.05 <= value <= maximum):
        raise CtpGatewaySessionError("INVALID_CONFIGURATION")
    return float(value)


@dataclass(frozen=True, slots=True)
class _ConnectionOwner:
    feed: object = field(repr=False)
    trader: object = field(repr=False)
    query_lock: object = field(repr=False)
    api: object = field(repr=False)
    spi: object = field(repr=False)
    account: object = field(repr=False)
    reader: object = field(repr=False)
    direct: object = field(repr=False)
    flow: object = field(repr=False)
    feed_flow: object = field(repr=False)
    trader_flow: object = field(repr=False)
    flow_config: tuple = field(repr=False)
    return_buffer: object = field(repr=False)
    issuer: object = field(repr=False)
    callback_ingress: object = field(repr=False)
    callback_tags: object = field(repr=False)
    generation: str = field(repr=False)
    source_tags: tuple = field(repr=False)
    feed_config: tuple = field(repr=False)
    trader_config: tuple = field(repr=False)
    api_config: tuple = field(repr=False)
    account_binding: str = field(repr=False)
    connection_generation: int = field(repr=False)


@dataclass(frozen=True, slots=True)
class _MarketDispatchBinding:
    coordinator: object = field(repr=False)
    account_scope: str = field(repr=False)
    service_generation: str = field(repr=False)


_FEED_BINDING_FIELDS = (
    "broker_id", "user_id", "td_front", "md_front", "ctp_env_profile", "ctp_environment",
    "_execution_bound_broker_id", "_execution_bound_user_id", "_execution_bound_td_front",
    "_execution_bound_md_front", "_execution_bound_profile", "_execution_bound_environment",
    "auto_settlement_confirm",
)
_TRADER_BINDING_FIELDS = (
    "broker_id", "user_id", "front", "md_front", "ctp_env_profile", "_bound_broker_id",
    "_bound_user_id", "_bound_front", "_bound_md_front", "_account_fingerprint",
    "_lifecycle_generation",
)
_SPI_BINDING_FIELDS = (
    "_native_spi_source_id", "_native_api_generation", "_native_client_epoch", "_native_api_source_id",
)


def _binding_fields(value: object, names: tuple[str, ...]) -> tuple:
    return tuple(getattr(value, name) for name in names)


def _binding_matches(actual: tuple, expected: tuple) -> bool:
    return len(actual) == len(expected) and all(
        type(value) is type(original) and value == original
        for value, original in zip(actual, expected)
    )


class CtpGatewaySession:
    """One process/thread's isolated read/direct/market resource composition.

    The factories are trusted, but their returned objects are checked before
    either native start. This class does not provide authority or authentication.
    The optional observation issuer is borrowed and never closed by this
    session; its paired instrument catalog is limited to futures snapshots.
    """

    def __init__(self, account_key: AccountKey, request_feed_factory: Callable[[], object],
                 stream_factory: Callable[[Any, Any], object], *, trusted_flow_root: object,
                 flow_account_scope: str, source_ref: str, entitlement_ref: str,
                 connect_timeout_seconds: float = 30.0, stop_timeout_seconds: float = 3.0,
                 queue_capacity: int = 1024, return_buffer_capacity: int = 1024,
                 observation_issuer: object | None = None,
                 observation_instruments: tuple[MarketInstrumentKey, ...] | None = None) -> None:
        if (type(account_key) is not AccountKey or account_key.provider != "ctp"
                or account_key.venue != "simnow" or account_key.environment != "simnow"
                or not account_key.subaccount_id or not callable(request_feed_factory)
                or not callable(stream_factory) or trusted_flow_root is None
                or flow_account_scope != account_key.account_key_id
                or (observation_issuer is None) != (observation_instruments is None)
                or (observation_instruments is not None and (
                    type(observation_instruments) is not tuple
                    or any(type(key) is not MarketInstrumentKey or key.product_type != "future"
                           for key in observation_instruments)
                ))):
            raise CtpGatewaySessionError("INVALID_CONFIGURATION")
        self._owner_pid = os.getpid()
        self._owner_thread = threading.current_thread()
        self._account_key = account_key
        self._return_buffer = CtpGatewayReturnBuffer(
            broker_id=account_key.subaccount_id, investor_id=account_key.account_id,
            capacity=return_buffer_capacity,
        )
        self._flow = trusted_flow(trusted_flow_root, flow_account_scope)
        if self._flow is None:
            raise CtpGatewaySessionError("INVALID_CONFIGURATION")
        self._feed_factory = request_feed_factory
        self._stream_factory = stream_factory
        self._connect_timeout = _timeout(connect_timeout_seconds, 300.0)
        self._stop_timeout = _timeout(stop_timeout_seconds, 30.0)
        self._state = "new"
        self._service_generation: str | None = None
        self._market_dispatch_binding: _MarketDispatchBinding | None = None
        self._requested_timeout = self._connect_timeout
        self._feed: CtpRequestDataFuture | None = None
        self._trader: TraderClient | None = None
        self._account_binding: str | None = None
        self._direct: CtpDirectWriteProvider | None = None
        self._connection_owner: _ConnectionOwner | None = None
        self._reader = CtpReadOnlyAccountSessionProvider(
            self._checked_feed, stop_timeout_seconds=self._stop_timeout,
            account_observation_issuer=observation_issuer,
            account_observation_instruments=observation_instruments,
        )
        self._market = CtpQuoteV2MarketProvider(
            stream_factory=self._checked_stream, source_ref=source_ref,
            entitlement_ref=entitlement_ref, queue_capacity=queue_capacity,
            connect_timeout_seconds=self._connect_timeout, stop_timeout_seconds=self._stop_timeout,
        )

    def _check_owner(self) -> None:
        if os.getpid() != self._owner_pid or threading.current_thread() is not self._owner_thread:
            raise CtpGatewaySessionError("WRONG_OWNER")

    def _bind_gateway_md_stream_dispatch_coordinator(
        self, coordinator, *, account_scope, service_generation
    ) -> None:
        """Bind partial MD invalidation before connecting this owner session.

        The binding is not a provider coverage proof and cannot enable
        dispatch. It is inherited only by a checked stream in this session.
        """
        from bt_api_py.gateway_dispatch_claim import GatewayDispatchClaimCoordinator

        self._check_owner()
        if (self._state != "new" or self._market_dispatch_binding is not None
                or type(coordinator) is not GatewayDispatchClaimCoordinator
                or type(account_scope) is not str
                or account_scope != self._account_key.account_key_id
                or type(service_generation) is not str or not service_generation
                or not coordinator.matches_owner(account_scope, service_generation)):
            raise CtpGatewaySessionError("MARKET_DISPATCH_BINDING_INVALID")
        self._assert_dispatch_coordinator_current(coordinator)
        self._market_dispatch_binding = _MarketDispatchBinding(
            coordinator, account_scope, service_generation)

    @staticmethod
    def _assert_dispatch_coordinator_current(coordinator) -> None:
        """Reject closed/poisoned coordinators without ever publishing READY."""
        token = None
        try:
            token = coordinator.begin_change("session_source")
            coordinator.finish_change(token, "UNAVAILABLE")
        except Exception:
            try:
                if token is not None:
                    coordinator.poison(token)
                else:
                    coordinator.poison_domain("session_source")
            except Exception:
                pass
            raise CtpGatewaySessionError("MARKET_DISPATCH_BINDING_UNAVAILABLE") from None

    def _require_connected(self) -> None:
        self._check_owner()
        if self._state != "connected":
            raise CtpGatewaySessionError("SESSION_NOT_CONNECTED")
        self._flow.verify_root()
        if self._reader.borrow_request_feed() is not self._feed or self._feed.trader_client is not self._trader:
            raise CtpGatewaySessionError("SESSION_CHANGED")

    def _checked_feed(self) -> CtpRequestDataFuture:
        self._check_owner()
        self._flow.verify_root()
        feed = self._feed_factory()
        if (type(feed) is not CtpRequestDataFuture or feed._trader is not None
                or feed._connected is not False or feed._managed_runtime_binding is not None
                or feed._gateway_return_buffer is not None
                or feed._execution_gate_capability is not None or feed.auto_settlement_confirm is not False
                or flow_binding(feed._trusted_flow) != flow_binding(self._flow)
                or feed._execution_bound_broker_id != self._account_key.subaccount_id
                or feed._execution_bound_user_id != self._account_key.account_id
                or feed.broker_id != feed._execution_bound_broker_id
                or feed.user_id != feed._execution_bound_user_id
                or feed.td_front != feed._execution_bound_td_front
                or feed.md_front != feed._execution_bound_md_front
                or feed.ctp_env_profile != feed._execution_bound_profile
                or feed.ctp_environment != "simnow"
                or feed._execution_bound_environment != "simnow"
                or not feed._execution_bound_td_front or not feed._execution_bound_md_front):
            raise CtpGatewaySessionError("FEED_BINDING_INVALID")
        feed._trusted_flow.verify_root()
        feed._install_gateway_return_buffer(self._return_buffer)
        feed._connect_timeout = min(self._connect_timeout, self._requested_timeout)
        self._feed = feed
        return feed

    def _checked_stream(self, key: Any, queue: Any) -> CtpMarketStream:
        self._require_connected()
        stream = self._stream_factory(key, queue)
        feed = self._feed
        if (type(stream) is not CtpMarketStream or stream.state is not ConnectionState.DISCONNECTED
                or bool(getattr(stream, "_running", False)) or stream._md_client is not None
                or bool(getattr(stream, "_quote_v2_subscription_metadata", {}))
                or bool(stream.topics) or getattr(stream, "_thread", None) is not None
                or stream.data_queue is not queue
                or flow_binding(stream._trusted_flow) != flow_binding(self._flow)
                or stream.broker_id != self._account_key.subaccount_id
                or stream.user_id != self._account_key.account_id
                or stream.md_front != feed._execution_bound_md_front):
            raise CtpGatewaySessionError("MARKET_BINDING_INVALID")
        stream._trusted_flow.verify_root()
        binding = self._market_dispatch_binding
        if binding is not None:
            if (type(binding) is not _MarketDispatchBinding
                    or binding.account_scope != self._account_key.account_key_id
                    or binding.service_generation != self._service_generation
                    or not binding.coordinator.matches_owner(
                        binding.account_scope, binding.service_generation)
                    or getattr(stream, "_gateway_md_stream_binding", None) is not None):
                raise CtpGatewaySessionError("MARKET_DISPATCH_BINDING_INVALID")
            self._assert_dispatch_coordinator_current(binding.coordinator)
            try:
                CtpMarketStream._bind_gateway_md_stream_dispatch_coordinator(
                    stream, binding.coordinator, account_scope=binding.account_scope,
                    service_generation=binding.service_generation)
            except Exception:
                raise CtpGatewaySessionError("MARKET_DISPATCH_BINDING_UNAVAILABLE") from None
            installed = getattr(stream, "_gateway_md_stream_binding", None)
            if (type(installed) is not tuple or len(installed) != 3
                    or installed[0] is not binding.coordinator
                    or installed[1:] != (binding.account_scope, binding.service_generation)
                    or self._market_dispatch_binding is not binding):
                raise CtpGatewaySessionError("MARKET_DISPATCH_BINDING_INVALID")
            self._assert_dispatch_coordinator_current(binding.coordinator)
        return stream

    def connect(self, *, generation: str, timeout: float) -> None:
        self._check_owner()
        if self._state != "new":
            raise CtpGatewaySessionError("SESSION_ALREADY_STARTED")
        if type(generation) is not str or not generation or generation != generation.strip() or len(generation) > 256:
            raise CtpGatewaySessionError("INVALID_CONFIGURATION")
        binding = self._market_dispatch_binding
        if binding is not None:
            if (type(binding) is not _MarketDispatchBinding
                    or generation != binding.service_generation
                    or binding.account_scope != self._account_key.account_key_id
                    or not binding.coordinator.matches_owner(binding.account_scope, generation)):
                raise CtpGatewaySessionError("MARKET_DISPATCH_BINDING_INVALID")
            self._assert_dispatch_coordinator_current(binding.coordinator)
        budget = _timeout(timeout, 300.0)
        self._requested_timeout = budget
        self._state = "starting"
        started = time.monotonic()
        try:
            self._reader.connect(account_key=self._account_key, generation=generation, timeout=budget)
            feed = self._reader.borrow_request_feed()
            trader = feed.trader_client
            if feed is not self._feed or type(trader) is not TraderClient or time.monotonic() - started > budget:
                raise CtpGatewaySessionError("SESSION_NOT_CONNECTED")
            scope = feed.get_query_session_scope()
            self._trader = trader
            self._account_binding = scope.account_fingerprint
            direct = CtpDirectWriteProvider(feed, owner_thread_only=True,
                background_settlement_readback=False, allow_implicit_connect=False)
            self._direct = direct
            direct.ensure_listening()
            if feed.trader_client is not trader:
                raise CtpGatewaySessionError("SESSION_CHANGED")
            with trader._query_state_lock:
                spi = trader._spi
                source_tags = trader._query_source_tags_locked()
                if (trader._api is None or type(spi) is not _TraderSpi
                        or spi._native_api is not trader._api or spi._c is not trader or not spi._is_current_locked()
                        or trader._session_native_api is not trader._api
                        or trader._current_login_identity_locked() is None
                        or type(source_tags) is not tuple or len(source_tags) != 6
                        or (trader._callback_ingress is not None and (
                            trader._callback_ingress.poisoned
                            or trader._callback_ingress.current_source_tags_object is not spi._callback_source_tags
                            or trader._callback_ingress.current_source_tags != source_tags))
                        or scope.issuer is not trader._query_evidence_issuer
                        or scope.source_tags != source_tags
                        or scope.account_fingerprint != trader._account_fingerprint
                        or scope.broker_id != self._account_key.subaccount_id
                        or scope.investor_id != self._account_key.account_id):
                    raise CtpGatewaySessionError("SESSION_CHANGED")
                self._connection_owner = _ConnectionOwner(
                    feed, trader, trader._query_state_lock, trader._api, spi, self._account_key, self._reader,
                    direct, self._flow, feed._trusted_flow, trader._trusted_flow,
                    (flow_binding(self._flow), flow_binding(feed._trusted_flow), flow_binding(trader._trusted_flow)),
                    self._return_buffer, scope.issuer, trader._callback_ingress,
                    spi._callback_source_tags, generation, source_tags[:5],
                    _binding_fields(feed, _FEED_BINDING_FIELDS),
                    _binding_fields(trader, _TRADER_BINDING_FIELDS),
                    _binding_fields(spi, _SPI_BINDING_FIELDS), scope.account_fingerprint,
                    scope.connection_generation,
                )
            self._service_generation = generation
            self._state = "connected"
        except BaseException:
            self._state = "failed"
            raise

    def _connection_owner_matches(self, owner: object) -> bool:
        try:
            return bool(type(owner) is _ConnectionOwner and self._connection_owner is owner
                and self._state == "connected" and self._feed is owner.feed and self._trader is owner.trader
                and self._account_key is owner.account and self._reader is owner.reader
                and self._direct is owner.direct and owner.direct._feed is owner.feed
                and owner.direct._owner_closing is False and owner.direct._owner_closed is False
                and self._flow is owner.flow and self._return_buffer is owner.return_buffer
                and self._service_generation == owner.generation
                and self._account_binding == owner.account_binding
                and owner.reader._request_feed is owner.feed and owner.reader._trader is owner.trader
                and owner.reader._account_key is owner.account
                and owner.reader._service_generation == owner.generation
                and owner.feed._trader is owner.trader and owner.trader._query_state_lock is owner.query_lock)
        except Exception:
            return False

    def connection_status(self) -> str:
        """Inspect the existing owner only; no connect, refresh or native I/O.

        Reconnecting is a local link/login state, not write or market readiness.
        The flow object binding is checked; filesystem identity is not re-read.
        """
        self._check_owner()
        owner = self._connection_owner
        if not self._connection_owner_matches(owner):
            raise CtpGatewaySessionError("SESSION_CHANGED")
        trader, feed, spi = owner.trader, owner.feed, owner.spi
        if not trader._query_state_lock.acquire(blocking=False):
            return "busy"
        try:
            source_tags = trader._query_source_tags_locked()
            if (not self._connection_owner_matches(owner)
                    or feed._trusted_flow is not owner.feed_flow or feed._gateway_return_buffer is not owner.return_buffer
                    or trader._trusted_flow is not owner.trader_flow
                    or (flow_binding(self._flow), flow_binding(feed._trusted_flow),
                        flow_binding(trader._trusted_flow)) != owner.flow_config
                    or not _binding_matches(_binding_fields(feed, _FEED_BINDING_FIELDS), owner.feed_config)
                    or not _binding_matches(_binding_fields(trader, _TRADER_BINDING_FIELDS), owner.trader_config)
                    or trader._api is not owner.api or trader._spi is not spi
                    or spi._native_api is not owner.api or spi._c is not trader or not spi._is_current_locked()
                    or trader._callback_ingress is not owner.callback_ingress
                    or spi._callback_source_tags is not owner.callback_tags
                    or not _binding_matches(_binding_fields(spi, _SPI_BINDING_FIELDS), owner.api_config)
                    or trader._query_evidence_issuer is not owner.issuer
                    or type(source_tags) is not tuple or source_tags[:5] != owner.source_tags
                    or (trader._callback_ingress is not None and (
                        trader._callback_ingress.current_source_tags_object is not owner.callback_tags
                        or trader._callback_ingress.current_source_tags != source_tags))
                    or type(trader._connection_generation) is not int
                    or trader._connection_generation < owner.connection_generation
                    or not trader._bound_identity_is_current(require_active_front=False)
                    or (trader._callback_ingress is not None and trader._callback_ingress.poisoned)):
                raise CtpGatewaySessionError("SESSION_CHANGED")
            if trader._connected is False:
                if (trader._session_native_api is not None or trader._session_native_front is not None
                        or trader._authentication_state != "disconnected"
                        or trader._login_state != "disconnected"
                        or trader._login_identity_observation is not None):
                    raise CtpGatewaySessionError("SESSION_CHANGED")
                return "reconnecting"
            if (trader._connected is not True or trader._session_native_api is not owner.api
                    or trader._session_native_front != trader._bound_front):
                raise CtpGatewaySessionError("SESSION_CHANGED")
            login = trader._current_login_identity_locked()
            if login is not None:
                if (login.broker_id != owner.account.subaccount_id
                        or login.user_id != owner.account.account_id
                        or login.connection_generation != trader._connection_generation
                        or login.trading_day != trader._trading_day):
                    raise CtpGatewaySessionError("SESSION_CHANGED")
                return "ready"
            if (trader._login_identity_observation is None
                    and ((trader._authentication_state == "authenticating"
                          and trader._login_state == "not_started")
                         or (trader._authentication_state == "authenticated"
                             and trader._login_state == "logging_in"))):
                return "reconnecting"
            if (trader._login_identity_observation is None
                    and trader._session_retry_current_locked(api=owner.api, spi=spi)):
                return "reconnecting"
            raise CtpGatewaySessionError("SESSION_CHANGED")
        except CtpGatewaySessionError:
            raise
        except Exception:
            raise CtpGatewaySessionError("SESSION_CHANGED") from None
        finally:
            trader._query_state_lock.release()

    @property
    def return_buffer(self) -> CtpGatewayReturnBuffer:
        self._check_owner()
        return self._return_buffer

    def create_return_query_reader(self) -> CtpGatewayReturnQueryReader:
        self._require_connected()
        return CtpGatewayReturnQueryReader(
            self._trader, environment=self._account_key.environment,
            broker_id=self._account_key.subaccount_id, investor_id=self._account_key.account_id,
        )

    def _require_instrument_connected(self) -> None:
        """Check the exact TD owner without borrowing an account-read epoch."""
        def check() -> None:
            status = self.connection_status()
            if status == "ready":
                return
            if status == "busy":
                raise CtpGatewaySessionError("INSTRUMENT_SESSION_BUSY")
            if status == "reconnecting":
                raise CtpGatewaySessionError("INSTRUMENT_SESSION_RECONNECTING")
            raise CtpGatewaySessionError("SESSION_CHANGED")

        check()
        try:
            self._flow.verify_root()
        except Exception:
            raise CtpGatewaySessionError("SESSION_CHANGED") from None
        check()

    def create_instrument_query_reader(self):
        """Borrow this session's Trader for source-bound static instrument rules."""
        self._require_instrument_connected()
        from bt_api_ctp.gateway_instrument_query import CtpGatewayInstrumentQueryReader

        reader = CtpGatewayInstrumentQueryReader(self._trader, account_key=self._account_key)
        reader._gateway_session = self
        reader._native_owner_gateway_session = self
        reader._gateway_service_generation = self._service_generation
        reader._check_session()
        return reader

    def read_market_login_clock(self, handle, *, key, generation, stream_id, epoch,
                                max_age_seconds=300.0):
        """Read a bounded TD clock bound to an actual current MD subscription.

        The Gateway owner must obtain handle and epoch from its authorized hub;
        they are not client-provided authentication or clock qualification.
        """
        from bt_api_ctp.gateway_clock_observation import read_market_login_clock

        return read_market_login_clock(
            self, handle, key=key, generation=generation, stream_id=stream_id,
            epoch=epoch, max_age_seconds=max_age_seconds,
        )

    def read_market_tick_clock(self, handle, *, key, generation, stream_id, epoch,
                               provider_sequence, provider_subscription_epoch,
                               max_age_seconds=300.0, _internal=False):
        """Read a source-bound native tick selected by the authorized Hub."""
        from bt_api_ctp.gateway_clock_observation import read_market_tick_clock

        return read_market_tick_clock(
            self, handle, key=key, generation=generation, stream_id=stream_id,
            epoch=epoch, provider_sequence=provider_sequence,
            provider_subscription_epoch=provider_subscription_epoch,
            max_age_seconds=max_age_seconds, _internal=_internal,
        )

    def assert_return_record_current(self, record: CtpGatewayReturnRecord) -> None:
        """Validate an offered record against the exact current native session."""
        self._require_connected()
        try:
            with self._trader._query_state_lock:
                trader = self._trader
                api, spi = trader._api, trader._spi
                if (not self._return_buffer._contains_record(record)
                        or api is None or spi is None or spi._native_api is not api
                        or not spi._is_current_locked() or trader._session_native_api is not api):
                    raise ValueError
                login = trader._current_login_identity_locked()
                binding = capture_query_session(trader, environment=self._account_key.environment)
                if (binding.broker_id != self._account_key.subaccount_id
                        or binding.investor_id != self._account_key.account_id
                        or record.source_ref != binding.source_ref
                        or record.source_tags != binding._source_tags
                        or record.connection_generation != binding.connection_generation
                        or record.reception_trading_day != binding.trading_day
                        or record.front_id != trader._front_id
                        or record.session_id != trader._session_id
                        or trader._api is not api or trader._spi is not spi
                        or trader._session_native_api is not api
                        or (record.kind in {"action_response", "action_error", "insert_response", "insert_error"} and (
                            login is None or login is not trader._current_login_identity_locked()
                            or type(record.login_request_id) is not int
                            or record.login_request_id != login.request_id))
                        or not self._return_buffer._contains_record(record)):
                    raise ValueError
        except Exception:
            raise CtpGatewaySessionError("RETURN_SOURCE_UNTRUSTED") from None

    @property
    def direct_provider(self) -> CtpDirectWriteProvider:
        self._require_connected()
        if self._direct is None:
            raise CtpGatewaySessionError("SESSION_NOT_CONNECTED")
        return self._direct

    @property
    def market_provider(self) -> CtpQuoteV2MarketProvider:
        self._require_connected()
        return self._market

    @property
    def account_binding(self) -> str:
        self._require_connected()
        if self._account_binding is None:
            raise CtpGatewaySessionError("SESSION_NOT_CONNECTED")
        return self._account_binding

    def read_account(self, *, generation: str, timeout: float) -> Any:
        self._require_connected()
        if generation != self._service_generation:
            raise CtpGatewaySessionError("SESSION_CHANGED")
        return self._reader.read_account(generation=generation, timeout=_timeout(timeout, 300.0))

    def begin_read_account(self, *, generation: str, timeout: float) -> Any:
        """Start ``read_account`` as request and response; no native I/O (I63-E130).

        ``poll_read_account`` sends and watches its queries on later owner
        ticks; the owner thread never waits for the venue meanwhile.
        """
        self._require_connected()
        if generation != self._service_generation:
            raise CtpGatewaySessionError("SESSION_CHANGED")
        return self._reader.begin_read_account(
            generation=generation, timeout=_timeout(timeout, 300.0)
        )

    def poll_read_account(self, request: Any) -> Any:
        """Advance a read begun by ``begin_read_account``; ``None`` while outstanding.

        A session that no longer passes its own checks ends the read failed.
        """
        self._check_owner()
        try:
            self._require_connected()
        except BaseException:
            self._reader.cancel_read_account(request)
            raise
        return self._reader.poll_read_account(request)

    def cancel_read_account(self, request: Any) -> None:
        """Discard a read begun by ``begin_read_account``; its answers are never used."""
        self._check_owner()
        self._reader.cancel_read_account(request)

    def query_lane_ready(self) -> bool:
        """Whether this session's Trader would send a query at once (local state only).

        Neither connects nor validates the session; a session that is not
        connected has no lane that is ready.
        """
        self._check_owner()
        trader = self._trader
        return self._state == "connected" and trader is not None and trader.query_lane_idle() is True

    def refresh_session(self) -> None:
        self._check_owner()
        if self._state != "connected" or self._feed is None or self._feed.trader_client is not self._trader:
            raise CtpGatewaySessionError("SESSION_NOT_CONNECTED")
        self._flow.verify_root()
        try:
            self._reader.refresh_session()
            scope = self._feed.get_query_session_scope()
        except ExchangeConnectionError as exc:
            # AuthenticationError is a subclass, but is not a link transition.
            if type(exc) is not ExchangeConnectionError:
                raise
            raise CtpGatewaySessionError("SESSION_UNAVAILABLE") from None
        if scope.account_fingerprint != self._account_binding:
            raise CtpGatewaySessionError("SESSION_CHANGED")

    def close(self) -> bool:
        """Retain ownership of every unconfirmed stop for owner-thread retry."""
        self._check_owner()
        if self._state == "closed":
            return True
        self._state = "closing"
        self._market._begin_close()
        if self._direct is not None:
            self._direct._begin_close()
        market_stopped = direct_stopped = account_stopped = False
        try:
            market_stopped = self._market.close() is True
        except BaseException:
            pass
        try:
            direct_stopped = self._direct is None or self._direct.close() is True
        except BaseException:
            pass
        try:
            account_stopped = self._reader.close() is True
        except BaseException:
            pass
        if not (market_stopped and direct_stopped and account_stopped):
            # Name the native part whose stop is still unconfirmed (I63-E107).
            _LOG.warning("CTP Gateway session stop unconfirmed: market=%s direct=%s account=%s",
                         market_stopped, direct_stopped, account_stopped)
        if market_stopped and direct_stopped and account_stopped:
            self._market_dispatch_binding = None
            self._connection_owner = None
            self._direct = None
            self._trader = None
            self._feed = None
            self._account_binding = None
            self._state = "closed"
            return True
        return False
