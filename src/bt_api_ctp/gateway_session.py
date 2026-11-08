"""Owner-thread CTP Gateway composition; no connections occur at construction.

The account reader owns the sole TD client, direct borrows that exact client,
while one quote provider owns the lazily started shared MD client. Configuration
labels are not account authentication or live-trading evidence.
"""
from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from bt_api_base.account_protocol import AccountKey

from bt_api_ctp.account_provider import CtpReadOnlyAccountSessionProvider
from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_ctp.feeds.live_ctp_feed import ConnectionState, CtpMarketStream, CtpRequestDataFuture
from bt_api_ctp.market_provider import CtpQuoteV2MarketProvider
from bt_api_ctp.trusted_flow import flow_binding, trusted_flow


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


class CtpGatewaySession:
    """One process/thread's isolated read/direct/market resource composition.

    The factories are trusted, but their returned objects are checked before
    either native start. This class does not provide authority or authentication.
    """

    def __init__(self, account_key: AccountKey, request_feed_factory: Callable[[], object],
                 stream_factory: Callable[[Any, Any], object], *, trusted_flow_root: object,
                 flow_account_scope: str, source_ref: str, entitlement_ref: str,
                 connect_timeout_seconds: float = 30.0, stop_timeout_seconds: float = 3.0,
                 queue_capacity: int = 1024) -> None:
        if (type(account_key) is not AccountKey or account_key.provider != "ctp"
                or account_key.venue != "simnow" or account_key.environment != "simnow"
                or not account_key.subaccount_id or not callable(request_feed_factory)
                or not callable(stream_factory) or trusted_flow_root is None
                or flow_account_scope != account_key.account_key_id):
            raise CtpGatewaySessionError("INVALID_CONFIGURATION")
        self._owner_pid = os.getpid()
        self._owner_thread = threading.current_thread()
        self._account_key = account_key
        self._flow = trusted_flow(trusted_flow_root, flow_account_scope)
        if self._flow is None:
            raise CtpGatewaySessionError("INVALID_CONFIGURATION")
        self._feed_factory = request_feed_factory
        self._stream_factory = stream_factory
        self._connect_timeout = _timeout(connect_timeout_seconds, 300.0)
        self._stop_timeout = _timeout(stop_timeout_seconds, 30.0)
        self._state = "new"
        self._service_generation: str | None = None
        self._requested_timeout = self._connect_timeout
        self._feed: CtpRequestDataFuture | None = None
        self._trader: TraderClient | None = None
        self._account_binding: str | None = None
        self._direct: CtpDirectWriteProvider | None = None
        self._reader = CtpReadOnlyAccountSessionProvider(
            self._checked_feed, stop_timeout_seconds=self._stop_timeout
        )
        self._market = CtpQuoteV2MarketProvider(
            stream_factory=self._checked_stream, source_ref=source_ref,
            entitlement_ref=entitlement_ref, queue_capacity=queue_capacity,
            connect_timeout_seconds=self._connect_timeout, stop_timeout_seconds=self._stop_timeout,
        )

    def _check_owner(self) -> None:
        if os.getpid() != self._owner_pid or threading.current_thread() is not self._owner_thread:
            raise CtpGatewaySessionError("WRONG_OWNER")

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
        return stream

    def connect(self, *, generation: str, timeout: float) -> None:
        self._check_owner()
        if self._state != "new":
            raise CtpGatewaySessionError("SESSION_ALREADY_STARTED")
        if type(generation) is not str or not generation or generation != generation.strip() or len(generation) > 256:
            raise CtpGatewaySessionError("INVALID_CONFIGURATION")
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
            self._service_generation = generation
            self._state = "connected"
        except BaseException:
            self._state = "failed"
            raise

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

    def refresh_session(self) -> None:
        self._check_owner()
        if self._state != "connected" or self._feed is None or self._feed.trader_client is not self._trader:
            raise CtpGatewaySessionError("SESSION_NOT_CONNECTED")
        self._flow.verify_root()
        self._reader.refresh_session()
        scope = self._feed.get_query_session_scope()
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
        if market_stopped and direct_stopped and account_stopped:
            self._direct = None
            self._trader = None
            self._feed = None
            self._account_binding = None
            self._state = "closed"
            return True
        return False
