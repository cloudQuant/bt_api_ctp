"""Read-only CTP account snapshots for the service-v2 account provider port.

The provider owns one ``CtpRequestDataFuture`` instance on its caller thread.
It accepts only complete, source-bound account/positions/orders query results
from the current TraderClient generation.  It does not expose order,
cancellation, settlement, or execution methods.

Optional Gateway execution observations (iteration 60): when constructed with
an observation issuer and the authorized instruments, the provider also offers
``read_execution_observations`` / ``confirm_execution_observations`` for
``GatewayAccountSessionWorker``. Per-fill observations come only from the
source-sealed trade query and order observations only from the source-sealed
order query, both projected by ``CtpExecutionQueryProjector`` (opaque issuer
ids, never native identities). CTP trade rows carry no fee, so a fill is
published without one (unknown, never zero); no fill is derived from an
order's cumulative quantity. A client order id is ``verified`` only through a
trusted resolver whose correlation the issuer validates and binds.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from types import MappingProxyType
from typing import Any

from bt_api_base.account_protocol import (
    AccountBalance,
    AccountKey,
    AccountObservation,
    AccountOpenOrder,
    AccountPosition,
    AccountResourceKey,
    AccountValuation,
    account_venue_contains_instrument_venue,
)
from bt_api_base.execution_correlation_protocol import ManagedOrderObservationIssuer
from bt_api_base.execution_query_protocol import ExecutionOrderRecord, ExecutionTradeRecord
from bt_api_base.execution_row_observation_protocol import (
    AccountExecutionOrderObservation,
    AccountExecutionTradeObservation,
    execution_observation_fact_digest,
    execution_observation_fact_id,
)
from bt_api_base.market_protocol import MarketInstrumentKey

from bt_api_ctp.ctp.client import (
    CtpNativeStartupAttempt,
    CtpNativeStartupStopReceipt,
    CtpNativeStopReceipt,
)
from bt_api_ctp.query import (
    _QUERY_EVIDENCE_MAX_TTL_SECONDS,
    _QUERY_MONOTONIC_CLOCK_DOMAIN,
    _QUERY_SOURCE_SEAL,
    QueryResult,
    _query_records_digest,
    _QuerySessionScope,
    _QuerySource,
)

_RESOURCE = AccountResourceKey(product_type="future", margin_pool="futures")
_QUERY_METHODS = {
    "account": "query_account_result",
    "positions": "query_positions_result",
    "orders": "query_orders_result",
}
_ACTIVE_ORDER_STATUS = frozenset({"1", "3", "b", "c"})
_TERMINAL_ORDER_STATUS = frozenset({"0", "2", "4", "5"})
_UTC = timezone.utc
_CHINA = timezone(timedelta(hours=8))
# Gateway execution observations (iteration 60). Trades are queried before
# orders: an order row exists before any of its trades, so every trade of the
# trade result has its order in the later order result.
_EXECUTION_QUERY_METHODS = (("trades", "query_trades_result"), ("orders", "query_orders_result"))
_MAX_EXECUTION_INSTRUMENTS = 256
_MAX_EXECUTION_QUERY_ROWS = 8192
# Per provider generation: distinct projected rows and published facts.
_MAX_EXECUTION_TRACKED = 32768
_MAX_EXECUTION_PAGE = 10_000
_EXECUTION_PRODUCT_TYPES = frozenset({"future", "option"})
_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        "filled",
        "canceled",
        "rejected",
        "partially_filled_terminal",
        "partially_filled_canceled",
        "terminal_unfilled",
    }
)
#: Finite refusal codes of execution query rows, counted once per distinct
#: row and provider generation (``execution_observation_counters``).
EXECUTION_ROW_REFUSAL_CODES = frozenset(
    {
        "FOREIGN_ACCOUNT_ROW",
        "FOREIGN_INSTRUMENT_ROW",
        "TRADING_DAY_MISMATCH",
        "ROW_UNPROJECTABLE",
        "CONFLICTING_DUPLICATE_ROW",
        "FACT_CONFLICT",
        "ORDER_STATE_REGRESSED",
        "ORDER_STATE_UNCONFIRMED",
    }
)
#: Non-refusal execution counters (also once per distinct row and generation):
#: merged duplicate rows of one fact, orders whose client attribution was
#: refused (published as ``unresolved``), and trades held back until an
#: observation of their order is published.
EXECUTION_NOTE_CODES = frozenset(
    {"DUPLICATE_ROW_MERGED", "ORDER_ATTRIBUTION_REFUSED", "TRADE_ORDER_UNOBSERVED"}
)


def _is_exact_prestart_ctp_request_feed(feed: object) -> bool:
    """Whether the exact loaded CTP feed failed before retaining its trader."""

    try:
        module = sys.modules.get("bt_api_ctp.feeds.live_ctp_feed")
        feed_type = vars(module).get("CtpRequestDataFuture") if module is not None else None
        return (
            feed_type is not None
            and type(feed) is feed_type
            and object.__getattribute__(feed, "_trader") is None
        )
    except BaseException:
        # Failure to establish the fixed feed's pre-start state is uncertain.
        return False


class CtpAccountSessionProviderError(RuntimeError):
    """Finite, value-free error from a read-only CTP account session."""

    _CODES = frozenset(
        {
            "INVALID_PROVIDER_CONFIGURATION",
            "INVALID_ACCOUNT_KEY",
            "SESSION_NOT_CONNECTED",
            "SESSION_SCOPE_UNTRUSTED",
            "QUERY_METHOD_UNAVAILABLE",
            "QUERY_FAILED",
            "QUERY_INCOMPLETE",
            "QUERY_SOURCE_UNTRUSTED",
            "QUERY_SCOPE_CHANGED",
            "QUERY_ROW_INVALID",
            "ORDER_OBSERVATION_IDENTITY_UNAVAILABLE",
            "QUERY_BUDGET_EXCEEDED",
            "OWNER_THREAD_REQUIRED",
            "NATIVE_CUSTODY_UNCERTAIN",
            "EXECUTION_OBSERVATIONS_UNAVAILABLE",
            "EXECUTION_GENERATION_STALE",
            "EXECUTION_LIMIT_INVALID",
            "EXECUTION_CONFIRM_INVALID",
            "EXECUTION_QUERY_TOO_LARGE",
            "EXECUTION_CAPACITY_EXCEEDED",
        }
    )

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in self._CODES:
            code = "CTP_ACCOUNT_PROVIDER_FAILED"
        self.code = code
        super().__init__(code)


class CtpReadOnlyAccountSessionProvider:
    """Adapt the real CTP request feed into a complete AccountObservation.

    ``request_feed_factory`` is trusted application code.  It creates one
    fresh, unconnected ``CtpRequestDataFuture`` with the locally authorized
    account/front pair.  The feed must own its direct native trader; wrappers
    borrowing another request feed or an already-created trader are rejected
    before ``connect``. Credentials remain in that closure and are never
    accepted from a request or serialized observation.

    Execution observations are off unless ``execution_observation_issuer``
    (a durable ``ManagedOrderObservationIssuer``: its opaque ids must be the
    same after a reconnect or process restart for idempotent redelivery) and
    ``execution_instruments`` (the authorized ``MarketInstrumentKey`` set) are
    both supplied. ``execution_order_resolver`` (trusted application code,
    with ``execution_scope_ref``) maps a native order reference to an issued
    ``OrderCorrelation``; without it every order stays ``unresolved``.
    """

    def __init__(
        self,
        request_feed_factory: Callable[[], object],
        *,
        stop_timeout_seconds: float = 3.0,
        monotonic_clock: Callable[[], float] | None = None,
        utc_clock: Callable[[], datetime] | None = None,
        execution_observation_issuer: object | None = None,
        execution_instruments: tuple[MarketInstrumentKey, ...] | None = None,
        execution_order_resolver: Callable[[str, MarketInstrumentKey], object] | None = None,
        execution_scope_ref: str | None = None,
        execution_query_interval_seconds: float = 1.0,
    ) -> None:
        if not callable(request_feed_factory):
            raise CtpAccountSessionProviderError("INVALID_PROVIDER_CONFIGURATION")
        self._execution_settings = _execution_settings(
            execution_observation_issuer,
            execution_instruments,
            execution_order_resolver,
            execution_scope_ref,
            execution_query_interval_seconds,
        )
        self._execution: _ExecutionGeneration | None = None
        self._execution_counters: dict[str, int] = {}
        if (
            isinstance(stop_timeout_seconds, bool)
            or not isinstance(stop_timeout_seconds, (int, float))
            or not isfinite(stop_timeout_seconds)
            or not 0.05 <= float(stop_timeout_seconds) <= 30.0
        ):
            raise CtpAccountSessionProviderError("INVALID_PROVIDER_CONFIGURATION")
        self._request_feed_factory = request_feed_factory
        self._stop_timeout_seconds = float(stop_timeout_seconds)
        if monotonic_clock is None:
            monotonic_clock = time.monotonic
        self._monotonic = monotonic_clock
        self._utc_clock = utc_clock or (lambda: datetime.now(_UTC))
        self._owner_thread_id: int | None = None
        self._request_feed: object | None = None
        self._trader: object | None = None
        self._account_key: AccountKey | None = None
        self._service_generation: str | None = None
        self._session_scope: object | None = None
        # These fields describe only the current/most recently closed provider
        # generation. None means startup may have allocated native resources
        # but no complete session scope or stop receipt proved their identity.
        self._native_owner_created: bool | None = False
        self._native_connection_generation: int | None = None
        self._last_native_stop_receipt: CtpNativeStopReceipt | None = None
        self._startup_attempt: CtpNativeStartupAttempt | None = None
        self._last_native_startup_stop_receipt: CtpNativeStartupStopReceipt | None = None

    @property
    def native_owner_created(self) -> bool:
        """Whether this provider generation created a CTP native owner.

        An interrupted startup is deliberately reported as uncertain until a
        typed stop receipt resolves it; callers must not turn that state into
        a no-native close acknowledgement.
        """

        self._check_owner(allow_unbound=True)
        if type(self._native_owner_created) is not bool:
            raise CtpAccountSessionProviderError("NATIVE_CUSTODY_UNCERTAIN")
        return self._native_owner_created

    @property
    def native_connection_generation(self) -> int | None:
        """Return the exact positive CTP generation, or None when no owner exists."""

        self._check_owner(allow_unbound=True)
        if type(self._native_owner_created) is not bool:
            raise CtpAccountSessionProviderError("NATIVE_CUSTODY_UNCERTAIN")
        generation = self._native_connection_generation
        if not self._native_owner_created:
            if generation is not None:
                raise CtpAccountSessionProviderError("NATIVE_CUSTODY_UNCERTAIN")
            return None
        if type(generation) is not int or generation <= 0:
            raise CtpAccountSessionProviderError("NATIVE_CUSTODY_UNCERTAIN")
        return generation

    @property
    def last_native_stop_receipt(self) -> CtpNativeStopReceipt | None:
        """Return only the exact complete receipt accepted for this generation."""

        self._check_owner(allow_unbound=True)
        receipt = self._last_native_stop_receipt
        if receipt is not None and (
            type(receipt) is not CtpNativeStopReceipt
            or receipt.complete is not True
            or self._native_owner_created is not True
            or receipt.connection_generation != self._native_connection_generation
        ):
            raise CtpAccountSessionProviderError("NATIVE_CUSTODY_UNCERTAIN")
        return receipt

    @property
    def native_startup_attempt(self) -> CtpNativeStartupAttempt | None:
        """Return only the immutable attempt captured from this provider's trader."""

        self._check_owner(allow_unbound=True)
        return self._startup_attempt

    @property
    def last_native_startup_stop_receipt(self) -> CtpNativeStartupStopReceipt | None:
        """Return only a complete generation-zero cleanup proof for that attempt."""

        self._check_owner(allow_unbound=True)
        receipt = self._last_native_startup_stop_receipt
        attempt = self._startup_attempt
        if receipt is not None and (
            type(receipt) is not CtpNativeStartupStopReceipt
            or type(attempt) is not CtpNativeStartupAttempt
            or receipt.api_attempt_id != attempt.api_attempt_id
            or receipt._client_binding is not attempt._client_binding
            or receipt.complete is not True
        ):
            raise CtpAccountSessionProviderError("NATIVE_CUSTODY_UNCERTAIN")
        return receipt

    @property
    def read_execution_observations(self) -> Callable[..., tuple[object, ...]] | None:
        """Optional Gateway capability; ``None`` unless execution is configured.

        ``GatewayAccountSessionWorker`` refuses an execution publisher for a
        provider without this callable, so an unconfigured provider fails at
        composition rather than on every drain.
        """

        if self._execution_settings is None:
            return None
        return self._read_execution_observations

    @property
    def confirm_execution_observations(self) -> Callable[..., None] | None:
        """Optional Gateway capability paired with ``read_execution_observations``."""

        if self._execution_settings is None:
            return None
        return self._confirm_execution_observations

    @property
    def execution_observation_counters(self) -> Mapping[str, int]:
        """Value-free refusal/note counts (finite codes) of execution rows."""

        return MappingProxyType(dict(self._execution_counters))

    def connect(self, *, account_key: Any, generation: str, timeout: float) -> None:
        if type(account_key) is not AccountKey:
            raise CtpAccountSessionProviderError("INVALID_ACCOUNT_KEY")
        if (
            account_key.provider != "ctp"
            or account_key.venue != "simnow"
            or account_key.environment != "simnow"
            or type(generation) is not str
            or not generation
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
            or not isfinite(timeout)
        ):
            raise CtpAccountSessionProviderError("INVALID_ACCOUNT_KEY")
        if self._request_feed is not None:
            raise CtpAccountSessionProviderError("SESSION_NOT_CONNECTED")
        settings = self._execution_settings
        if settings is not None and any(
            key.environment != account_key.environment
            or not account_venue_contains_instrument_venue(account_key, key.venue)
            for key in settings.instruments
        ):
            # An authorized instrument outside this account realm can never
            # produce a valid observation; refuse before any native work.
            raise CtpAccountSessionProviderError("INVALID_PROVIDER_CONFIGURATION")
        # Every generation starts with a fresh execution queue; unconfirmed
        # observations of an earlier generation are re-derived from queries.
        self._execution = None
        # GatewayAccountSessionWorker reuses the provider after an explicit
        # close-and-reconnect recovery. Reopening is safe only after close has
        # detached the exact old feed/trader above.
        self._owner_thread_id = threading.get_ident()
        self._account_key = account_key
        self._service_generation = generation
        self._native_owner_created = False
        self._native_connection_generation = None
        self._last_native_stop_receipt = None
        self._startup_attempt = None
        self._last_native_startup_stop_receipt = None
        try:
            feed = self._request_feed_factory()
        except Exception:
            raise CtpAccountSessionProviderError("QUERY_FAILED") from None
        if feed is None or not callable(getattr(feed, "connect", None)):
            raise CtpAccountSessionProviderError("INVALID_PROVIDER_CONFIGURATION")
        try:
            nested_feed = getattr(feed, "_request_feed", None)
            existing_trader = getattr(feed, "_trader", None)
        except Exception:
            raise CtpAccountSessionProviderError("INVALID_PROVIDER_CONFIGURATION") from None
        if nested_feed is not None or existing_trader is not None:
            # We have not called into this feed, so any nested/existing native
            # owner belongs to its creator.  Never adopt or stop that object.
            raise CtpAccountSessionProviderError("INVALID_PROVIDER_CONFIGURATION")
        # Keep the feed before calling into native code.  Gateway cleanup can
        # then retry against the exact TraderClient if startup is interrupted.
        self._request_feed = feed
        try:
            feed.connect()
            trader = getattr(feed, "_trader", None)
            state_reader = getattr(feed, "get_session_state", None)
            scope_reader = getattr(feed, "get_query_session_scope", None)
            if trader is None or not callable(state_reader) or not callable(scope_reader):
                raise CtpAccountSessionProviderError("SESSION_SCOPE_UNTRUSTED")
            state = state_reader()
            if (
                not isinstance(state, Mapping)
                or state.get("read_only_ready") is not True
                or state.get("auto_settlement_confirm") is not False
            ):
                raise CtpAccountSessionProviderError("SESSION_NOT_CONNECTED")
            scope = scope_reader()
            self._validate_scope(scope, trader, account_key)
            self._trader = trader
            self._session_scope = scope
            self._native_owner_created = True
            self._native_connection_generation = scope.connection_generation
            if settings is not None:
                self._execution = _ExecutionGeneration(generation)
        except CtpAccountSessionProviderError:
            self._capture_native_startup_attempt(feed)
            self._native_owner_created = self._startup_owner_state_after_failure(feed)
            raise
        except Exception:
            self._capture_native_startup_attempt(feed)
            self._native_owner_created = self._startup_owner_state_after_failure(feed)
            raise CtpAccountSessionProviderError("SESSION_NOT_CONNECTED") from None
        except BaseException:
            self._capture_native_startup_attempt(feed)
            self._native_owner_created = self._startup_owner_state_after_failure(feed)
            raise

    def _startup_owner_state_after_failure(self, feed: object) -> bool | None:
        attempt = self._startup_attempt
        if type(attempt) is CtpNativeStartupAttempt:
            if attempt.phase == "preflight_no_owner" and attempt.api_attempt_id is None:
                return False
            if attempt.phase in {"factory_uncertain", "api_created"}:
                return None
            return None
        return False if _is_exact_prestart_ctp_request_feed(feed) else None

    def _capture_native_startup_attempt(self, feed: object) -> None:
        try:
            trader = getattr(feed, "_trader", None)
            attempt = getattr(trader, "native_startup_attempt", None)
        except BaseException:
            return
        if type(attempt) is CtpNativeStartupAttempt:
            self._startup_attempt = attempt

    def read_account(self, *, generation: str, timeout: float) -> AccountObservation:
        self._check_owner()
        if (
            self._request_feed is None
            or self._trader is None
            or self._account_key is None
            or self._service_generation is None
            or generation != self._service_generation
        ):
            raise CtpAccountSessionProviderError("SESSION_NOT_CONNECTED")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
            or not isfinite(timeout)
        ):
            raise CtpAccountSessionProviderError("QUERY_BUDGET_EXCEEDED")
        started = self._monotonic()
        deadline = started + float(timeout)
        if not isfinite(started) or not isfinite(deadline):
            raise CtpAccountSessionProviderError("QUERY_BUDGET_EXCEEDED")
        feed = self._request_feed
        account_key = self._account_key
        service_generation = self._service_generation
        baseline_scope = self._session_scope
        results: dict[str, QueryResult[Any]] = {}
        for query_name, method_name in _QUERY_METHODS.items():
            now = self._monotonic()
            if not isfinite(now):
                raise CtpAccountSessionProviderError("QUERY_BUDGET_EXCEEDED")
            remaining = deadline - now
            if remaining <= 0:
                raise CtpAccountSessionProviderError("QUERY_BUDGET_EXCEEDED")
            method = getattr(feed, method_name, None)
            if not callable(method):
                raise CtpAccountSessionProviderError("QUERY_METHOD_UNAVAILABLE")
            try:
                result = method(timeout=remaining)
            except Exception:
                raise CtpAccountSessionProviderError("QUERY_FAILED") from None
            query_finished = self._monotonic()
            if not isfinite(query_finished) or query_finished > deadline:
                raise CtpAccountSessionProviderError("QUERY_BUDGET_EXCEEDED")
            self._validate_query_result(
                result,
                query_name=query_name,
                trader=self._trader,
                scope=baseline_scope,
                now_monotonic=query_finished,
            )
            results[query_name] = result
        finished = self._monotonic()
        if not isfinite(finished) or finished > deadline:
            raise CtpAccountSessionProviderError("QUERY_BUDGET_EXCEEDED")
        scope_reader = getattr(feed, "get_query_session_scope", None)
        if not callable(scope_reader):
            raise CtpAccountSessionProviderError("SESSION_SCOPE_UNTRUSTED")
        try:
            final_scope = scope_reader()
        except Exception:
            raise CtpAccountSessionProviderError("SESSION_SCOPE_UNTRUSTED") from None
        self._validate_scope(final_scope, self._trader, account_key)
        if not _same_session_scope(baseline_scope, final_scope):
            raise CtpAccountSessionProviderError("QUERY_SCOPE_CHANGED")
        if service_generation != self._service_generation:
            raise CtpAccountSessionProviderError("QUERY_SCOPE_CHANGED")
        return _build_observation(account_key, results, self._utc_clock())

    def close(self) -> bool:
        """Stop the exact native trader; false leaves its reference in custody."""

        self._check_owner(allow_unbound=True)
        feed = self._request_feed
        try:
            feed_trader = None if feed is None else getattr(feed, "_trader", None)
        except Exception:
            return False
        trader = self._trader if self._trader is not None else feed_trader
        if self._trader is not None and feed_trader is not self._trader:
            return False
        if trader is None:
            if type(self._native_owner_created) is not bool:
                # Unknown startup custody is not a no-native close proof.
                return False
            if self._native_owner_created is True:
                # A previously confirmed stop is idempotent. A missing
                # receipt for a claimed owner is not enough to release it.
                if self.last_native_stop_receipt is not None:
                    return True
                try:
                    startup_receipt = self.last_native_startup_stop_receipt
                except CtpAccountSessionProviderError:
                    return False
                return (
                    type(startup_receipt) is CtpNativeStartupStopReceipt
                    and startup_receipt.complete is True
                    and startup_receipt.outcome == "api_created_released"
                )
            if (
                self._native_connection_generation is not None
                or self._last_native_stop_receipt is not None
            ):
                return False
            self._request_feed = None
            self._trader = None
            self._session_scope = None
            self._service_generation = None
            self._execution = None
            self._account_key = None
            return True
        attempt = self._startup_attempt
        if type(attempt) is CtpNativeStartupAttempt:
            # Generation-zero cleanup is an additive startup-specific API.
            # Keep the established stop_and_wait() return contract for every
            # existing active-session caller.
            stop_and_wait = getattr(trader, "stop_startup_and_wait", None)
        else:
            stop_and_wait = getattr(trader, "stop_and_wait", None)
        if not callable(stop_and_wait):
            return False
        try:
            receipt = stop_and_wait(timeout=self._stop_timeout_seconds)
        except Exception:
            return False
        if type(receipt) is CtpNativeStartupStopReceipt:
            if (
                type(attempt) is not CtpNativeStartupAttempt
                or receipt.api_attempt_id != attempt.api_attempt_id
                or receipt._client_binding is not attempt._client_binding
                or receipt.complete is not True
            ):
                return False
            if (
                receipt.outcome == "preflight_no_owner"
                and attempt.phase == "preflight_no_owner"
                and attempt.api_attempt_id is None
            ):
                self._native_owner_created = False
                self._native_connection_generation = None
            elif (
                receipt.outcome == "api_created_released"
                and attempt.phase == "api_created"
                and type(attempt.api_attempt_id) is str
                and attempt.api_attempt_id == receipt.api_attempt_id
            ):
                # Startup cleanup closes an API attempt, not an active session;
                # keep its zero connection generation out of the active slot.
                self._native_owner_created = True
                self._native_connection_generation = None
            else:
                return False
            self._last_native_startup_stop_receipt = receipt
            if feed is not None and getattr(feed, "_trader", None) is trader:
                feed._trader = None
                feed._connected = False
            self._trader = None
            self._request_feed = None
            self._session_scope = None
            self._service_generation = None
            self._execution = None
            self._account_key = None
            return True
        scope_generation = (
            self._session_scope.connection_generation
            if type(self._session_scope) is _QuerySessionScope
            else None
        )
        expected_generation = self._native_connection_generation or scope_generation
        if not _native_stop_confirmed(receipt, expected_generation=expected_generation):
            return False
        self._native_connection_generation = receipt.connection_generation
        self._native_owner_created = True
        self._last_native_stop_receipt = receipt
        if feed is not None and getattr(feed, "_trader", None) is trader:
            # The exact native stop receipt succeeded.  Detach the already
            # stopped client so a later read cannot reconnect implicitly.
            feed._trader = None
            feed._connected = False
        self._trader = None
        self._request_feed = None
        self._session_scope = None
        self._service_generation = None
        self._execution = None
        self._account_key = None
        return True

    def _validate_scope(self, scope: object, trader: object, account_key: AccountKey) -> None:
        if (
            type(scope) is not _QuerySessionScope
            or scope.issuer is not trader
            or scope.read_only_ready is not True
            or type(scope.connection_generation) is not int
            or scope.connection_generation <= 0
            or type(scope.account_fingerprint) is not str
            or not scope.account_fingerprint
            or type(scope.investor_id) is not str
            or not scope.investor_id
            or type(scope.broker_id) is not str
            or not scope.broker_id
            or type(scope.trading_day) is not str
            or not scope.trading_day
            or scope.investor_id != account_key.account_id
            or scope.broker_id != account_key.subaccount_id
        ):
            raise CtpAccountSessionProviderError("SESSION_SCOPE_UNTRUSTED")

    def _validate_query_result(
        self,
        result: object,
        *,
        query_name: str,
        trader: object,
        scope: object,
        now_monotonic: float,
    ) -> None:
        if (
            type(result) is not QueryResult
            or type(scope) is not _QuerySessionScope
            or result.request_type != query_name
            or result.complete is not True
            or result.is_last_seen is not True
            or result.timed_out is not False
            or result.unsupported is not False
            or not (
                result.error_code is None
                or (type(result.error_code) is int and result.error_code == 0)
            )
            or result.connection_generation != scope.connection_generation
            or result.account_fingerprint != scope.account_fingerprint
            or type(result.query_source) is not _QuerySource
        ):
            raise CtpAccountSessionProviderError("QUERY_INCOMPLETE")
        source = result.query_source
        if (
            source._seal is not _QUERY_SOURCE_SEAL
            or source.issuer is not trader
            or source.request_type != query_name
            or type(result.request_id) is not int
            or result.request_id <= 0
            or type(source.request_id) is not int
            or source.request_id != result.request_id
            or source.connection_generation != scope.connection_generation
            or source.account_fingerprint != scope.account_fingerprint
            or source.trading_day != scope.trading_day
            or source.broker_id != scope.broker_id
            or source.investor_id != scope.investor_id
            or source.records_sha256 is None
            or source.started_at_utc != result.started_at_utc
            or source.completed_at_utc != result.completed_at_utc
            or source.clock_domain_id != _QUERY_MONOTONIC_CLOCK_DOMAIN
            or type(source.started_monotonic) not in (int, float)
            or isinstance(source.started_monotonic, bool)
            or not isfinite(source.started_monotonic)
            or type(source.completed_monotonic) not in (int, float)
            or isinstance(source.completed_monotonic, bool)
            or not isfinite(source.completed_monotonic)
            or source.completed_monotonic < source.started_monotonic
            or source.completed_monotonic > now_monotonic
            or now_monotonic - source.completed_monotonic > _QUERY_EVIDENCE_MAX_TTL_SECONDS
            or type(source.trusted_expires_monotonic) not in (int, float)
            or not isfinite(source.trusted_expires_monotonic)
            or source.trusted_expires_monotonic != (
                source.completed_monotonic + _QUERY_EVIDENCE_MAX_TTL_SECONDS
            )
            or now_monotonic > source.trusted_expires_monotonic
        ):
            raise CtpAccountSessionProviderError("QUERY_SOURCE_UNTRUSTED")
        try:
            current_digest = _query_records_digest(result.records)
        except Exception:
            raise CtpAccountSessionProviderError("QUERY_SOURCE_UNTRUSTED") from None
        if current_digest != source.records_sha256:
            raise CtpAccountSessionProviderError("QUERY_SOURCE_UNTRUSTED")

    def _check_owner(self, *, allow_unbound: bool = False) -> None:
        owner = self._owner_thread_id
        if owner is None and allow_unbound:
            return
        if owner is None or threading.get_ident() != owner:
            raise CtpAccountSessionProviderError("OWNER_THREAD_REQUIRED")

    # -- Gateway execution observations (iteration 60) -------------------------

    def _execution_for(self, generation: object) -> _ExecutionGeneration:
        self._check_owner()
        if self._execution_settings is None:
            raise CtpAccountSessionProviderError("EXECUTION_OBSERVATIONS_UNAVAILABLE")
        state = self._execution
        if (
            self._request_feed is None
            or self._trader is None
            or self._account_key is None
            or self._service_generation is None
            or state is None
        ):
            raise CtpAccountSessionProviderError("SESSION_NOT_CONNECTED")
        if (
            type(generation) is not str
            or generation != self._service_generation
            or generation != state.generation
        ):
            raise CtpAccountSessionProviderError("EXECUTION_GENERATION_STALE")
        return state

    def _read_execution_observations(
        self, *, generation: str, timeout: float, limit: int
    ) -> tuple[AccountExecutionOrderObservation | AccountExecutionTradeObservation, ...]:
        """Return up to ``limit`` pending observations of ``generation``.

        Nothing is removed: the same objects are returned again until
        ``confirm_execution_observations`` removes them. New facts are read
        only when nothing is pending and the query interval has elapsed, so
        Gateway maintenance does not exceed the CTP query rate.
        """

        state = self._execution_for(generation)
        if type(limit) is not int or not 1 <= limit <= _MAX_EXECUTION_PAGE:
            raise CtpAccountSessionProviderError("EXECUTION_LIMIT_INVALID")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
            or not isfinite(timeout)
        ):
            raise CtpAccountSessionProviderError("QUERY_BUDGET_EXCEEDED")
        if not state.pending:
            now = self._monotonic()
            if not isfinite(now):
                raise CtpAccountSessionProviderError("QUERY_BUDGET_EXCEEDED")
            settings = self._execution_settings
            if settings is None:
                raise CtpAccountSessionProviderError("EXECUTION_OBSERVATIONS_UNAVAILABLE")
            if state.next_query is None or now >= state.next_query:
                state.next_query = now + settings.query_interval
                self._poll_execution(state, float(timeout))
        page = tuple(state.pending[:limit])
        state.offered = len(page)
        return page

    def _confirm_execution_observations(self, *, generation: str, count: int) -> None:
        """Remove the first ``count`` observations returned by the last read."""

        state = self._execution_for(generation)
        if type(count) is not int or not 0 <= count <= state.offered:
            raise CtpAccountSessionProviderError("EXECUTION_CONFIRM_INVALID")
        del state.pending[:count]
        state.offered -= count
        return None

    def _poll_execution(self, state: _ExecutionGeneration, timeout: float) -> None:
        """Query trades then orders once and queue only new, consistent facts.

        The whole poll is staged first: any query, source, scope or capacity
        failure raises a finite error and leaves the queue unchanged.
        """

        started = self._monotonic()
        deadline = started + timeout
        if not isfinite(started) or not isfinite(deadline):
            raise CtpAccountSessionProviderError("QUERY_BUDGET_EXCEEDED")
        feed = self._request_feed
        trader = self._trader
        account_key = self._account_key
        service_generation = self._service_generation
        baseline_scope = self._session_scope
        settings = self._execution_settings
        if account_key is None or settings is None:
            raise CtpAccountSessionProviderError("SESSION_NOT_CONNECTED")
        results: dict[str, QueryResult[Any]] = {}
        for query_name, method_name in _EXECUTION_QUERY_METHODS:
            now = self._monotonic()
            if not isfinite(now) or deadline - now <= 0:
                raise CtpAccountSessionProviderError("QUERY_BUDGET_EXCEEDED")
            method = getattr(feed, method_name, None)
            if not callable(method):
                raise CtpAccountSessionProviderError("QUERY_METHOD_UNAVAILABLE")
            try:
                result = method(timeout=deadline - now)
            except Exception:
                raise CtpAccountSessionProviderError("QUERY_FAILED") from None
            query_finished = self._monotonic()
            if not isfinite(query_finished) or query_finished > deadline:
                raise CtpAccountSessionProviderError("QUERY_BUDGET_EXCEEDED")
            self._validate_query_result(
                result,
                query_name=query_name,
                trader=trader,
                scope=baseline_scope,
                now_monotonic=query_finished,
            )
            results[query_name] = result
        scope_reader = getattr(feed, "get_query_session_scope", None)
        if not callable(scope_reader):
            raise CtpAccountSessionProviderError("SESSION_SCOPE_UNTRUSTED")
        try:
            final_scope = scope_reader()
        except Exception:
            raise CtpAccountSessionProviderError("SESSION_SCOPE_UNTRUSTED") from None
        self._validate_scope(final_scope, trader, account_key)
        if not _same_session_scope(baseline_scope, final_scope):
            raise CtpAccountSessionProviderError("QUERY_SCOPE_CHANGED")
        if service_generation != self._service_generation or state is not self._execution:
            raise CtpAccountSessionProviderError("QUERY_SCOPE_CHANGED")
        trade_rows = tuple(results["trades"].records)
        order_rows = tuple(results["orders"].records)
        if len(trade_rows) > _MAX_EXECUTION_QUERY_ROWS or len(order_rows) > _MAX_EXECUTION_QUERY_ROWS:
            raise CtpAccountSessionProviderError("EXECUTION_QUERY_TOO_LARGE")
        observed_at = _execution_observed_at(results, self._utc_clock())
        if state.projector is None:
            state.projector = self._execution_projector(baseline_scope, account_key)
        _stage_execution_poll(
            state,
            account_key,
            settings,
            self._execution_counters,
            order_rows=order_rows,
            trade_rows=trade_rows,
            observed_at=observed_at,
        )

    def _execution_projector(self, scope: object, account_key: AccountKey) -> Any:
        from bt_api_ctp.execution_query_projection import CtpExecutionQueryProjector
        from bt_api_ctp.registered_sim_session import (
            CtpQuerySessionBindingError,
            _bind_validated_query_scope,
        )

        settings = self._execution_settings
        if settings is None:
            raise CtpAccountSessionProviderError("EXECUTION_OBSERVATIONS_UNAVAILABLE")
        try:
            session = _bind_validated_query_scope(scope, environment=account_key.environment)
        except CtpQuerySessionBindingError:
            raise CtpAccountSessionProviderError("SESSION_SCOPE_UNTRUSTED") from None
        try:
            return CtpExecutionQueryProjector(
                session,
                observation_issuer=settings.issuer,
                ledger_environment=account_key.environment,
                account_ref=account_key.account_key_id,
                scope_ref=settings.scope_ref,
                managed_order_resolver=settings.order_resolver,
            )
        except (TypeError, ValueError):
            raise CtpAccountSessionProviderError("INVALID_PROVIDER_CONFIGURATION") from None


@dataclass(frozen=True)
class _ExecutionSettings:
    issuer: object
    instruments: tuple[MarketInstrumentKey, ...]
    catalog: Mapping[tuple[str, str], str]
    order_resolver: Callable[[str, MarketInstrumentKey], object] | None
    scope_ref: str | None
    query_interval: float


def _execution_settings(
    issuer: object,
    instruments: object,
    order_resolver: object,
    scope_ref: object,
    query_interval: object,
) -> _ExecutionSettings | None:
    if (
        isinstance(query_interval, bool)
        or not isinstance(query_interval, (int, float))
        or not isfinite(query_interval)
        or not 0.0 <= float(query_interval) <= 60.0
    ):
        raise CtpAccountSessionProviderError("INVALID_PROVIDER_CONFIGURATION")
    if issuer is None and instruments is None and order_resolver is None and scope_ref is None:
        return None
    if (
        issuer is None
        or instruments is None
        or not isinstance(issuer, ManagedOrderObservationIssuer)
        or type(instruments) is not tuple
        or not 1 <= len(instruments) <= _MAX_EXECUTION_INSTRUMENTS
        or (order_resolver is None) != (scope_ref is None)
        or (order_resolver is not None and not callable(order_resolver))
        or (
            scope_ref is not None
            and (
                type(scope_ref) is not str
                or not scope_ref
                or scope_ref != scope_ref.strip()
                or len(scope_ref) > 256
            )
        )
    ):
        raise CtpAccountSessionProviderError("INVALID_PROVIDER_CONFIGURATION")
    catalog: dict[tuple[str, str], str] = {}
    for key in instruments:
        if (
            type(key) is not MarketInstrumentKey
            or key.provider != "ctp"
            or key.product_type not in _EXECUTION_PRODUCT_TYPES
            or key.native_symbol != key.instrument_id
            or (key.venue, key.instrument_id) in catalog
        ):
            raise CtpAccountSessionProviderError("INVALID_PROVIDER_CONFIGURATION")
        catalog[(key.venue, key.instrument_id)] = key.product_type
    return _ExecutionSettings(
        issuer=issuer,
        instruments=instruments,
        catalog=MappingProxyType(catalog),
        order_resolver=order_resolver,  # type: ignore[arg-type]
        scope_ref=scope_ref,  # type: ignore[arg-type]
        query_interval=float(query_interval),
    )


class _ExecutionGeneration:
    """Bounded execution state of one provider generation (never persisted)."""

    __slots__ = (
        "generation",
        "projector",
        "pending",
        "offered",
        "next_query",
        "facts",
        "order_progress",
        "rows",
        "conflicted",
        "noted",
    )

    def __init__(self, generation: str) -> None:
        self.generation = generation
        self.projector: Any = None
        # Queued, not yet confirmed observations, oldest first.
        self.pending: list[AccountExecutionOrderObservation | AccountExecutionTradeObservation] = []
        self.offered = 0
        self.next_query: float | None = None
        # Published fact id -> fact digest, and order id -> (terminal, filled).
        self.facts: dict[str, str] = {}
        self.order_progress: dict[str, tuple[bool, Decimal]] = {}
        # (kind, native row digest) -> projection or refusal code.
        self.rows: dict[tuple[str, str], _ProjectedExecutionRow | str] = {}
        self.conflicted: set[tuple[str, str]] = set()
        self.noted: set[tuple[str, tuple[str, str]]] = set()


@dataclass(frozen=True)
class _ProjectedExecutionRow:
    observation: AccountExecutionOrderObservation | AccountExecutionTradeObservation
    identity: str
    fact_id: str
    fact_digest: str
    attribution_refused: bool


def _execution_observed_at(results: Mapping[str, QueryResult[Any]], utc_now: object) -> str:
    if type(utc_now) is not datetime or utc_now.utcoffset() != timedelta(0):
        raise CtpAccountSessionProviderError("QUERY_ROW_INVALID")
    completed = [result.completed_at_utc for result in results.values()]
    if any(
        not isinstance(value, datetime) or value.utcoffset() != timedelta(0) for value in completed
    ) or max(completed) > utc_now:
        raise CtpAccountSessionProviderError("QUERY_ROW_INVALID")
    return _utc_text(max(completed))


def _project_execution_row(
    kind: str,
    row: Any,
    projector: Any,
    account_key: AccountKey,
    settings: _ExecutionSettings,
    observed_at: str,
) -> _ProjectedExecutionRow | str:
    """One native query row -> one observation, or a finite refusal code."""

    try:
        investor_id = _text(_field(row, "InvestorID"))
        broker_id = _text(_field(row, "BrokerID"))
    except Exception:
        return "FOREIGN_ACCOUNT_ROW"
    if investor_id != account_key.account_id or broker_id != account_key.subaccount_id:
        return "FOREIGN_ACCOUNT_ROW"
    try:
        venue = _text(_field(row, "ExchangeID"))
        instrument_id = _text(_field(row, "InstrumentID"))
    except Exception:
        return "ROW_UNPROJECTABLE"
    if (venue, instrument_id) not in settings.catalog:
        return "FOREIGN_INSTRUMENT_ROW"
    project = projector.project_orders if kind == "order" else projector.project_trades
    try:
        records, blockers = project(
            (row,), observed_at=observed_at, catalog_types=settings.catalog, filters={}
        )
    except Exception:
        return "ROW_UNPROJECTABLE"
    if len(records) != 1:
        if any(str(code).endswith("trading_day_mismatch") for code in blockers):
            return "TRADING_DAY_MISMATCH"
        return "ROW_UNPROJECTABLE"
    try:
        observation: AccountExecutionOrderObservation | AccountExecutionTradeObservation
        if kind == "order":
            observation = AccountExecutionOrderObservation(
                account_key, ExecutionOrderRecord.from_dict(records[0])
            )
            if observation.order.status == "unknown":
                # Not yet acknowledged by the venue: no order fact to publish.
                return "ORDER_STATE_UNCONFIRMED"
            identity = observation.order.order_id
        else:
            # CTP trade rows carry no fee: the observation has none (unknown).
            observation = AccountExecutionTradeObservation(
                account_key, ExecutionTradeRecord.from_dict(records[0])
            )
            identity = observation.trade.trade_id
        fact_id = execution_observation_fact_id(observation)
        fact_digest = execution_observation_fact_digest(observation)
    except Exception:
        return "ROW_UNPROJECTABLE"
    return _ProjectedExecutionRow(
        observation, identity, fact_id, fact_digest, attribution_refused=bool(blockers)
    )


def _stage_execution_poll(
    state: _ExecutionGeneration,
    account_key: AccountKey,
    settings: _ExecutionSettings,
    counters: dict[str, int],
    *,
    order_rows: tuple[Any, ...],
    trade_rows: tuple[Any, ...],
    observed_at: str,
) -> None:
    """Stage one poll and commit it only when it fits the generation bounds.

    Facts are keyed by Base fact id, so duplicated or reordered rows give the
    same facts. A row whose identity has conflicting rows, whose fact id was
    published with another digest, or whose order state regresses is refused
    (fail closed) instead of queued. A trade waits until an observation of
    its order has been queued, so it always follows its order's route.
    """

    new_rows: dict[tuple[str, str], _ProjectedExecutionRow | str] = {}
    new_conflicts: dict[tuple[str, str], str] = {}
    new_notes: set[tuple[str, tuple[str, str]]] = set()

    def note(code: str, key: tuple[str, str]) -> None:
        if (code, key) not in state.noted:
            new_notes.add((code, key))

    def refuse(code: str, key: tuple[str, str]) -> None:
        if key not in state.conflicted:
            new_conflicts.setdefault(key, code)

    def collect(kind: str, rows: tuple[Any, ...]) -> list[tuple[tuple[str, str], _ProjectedExecutionRow]]:
        groups: dict[str, list[tuple[tuple[str, str], _ProjectedExecutionRow]]] = {}
        for row in rows:
            try:
                key = (kind, _query_records_digest((row,)))
            except Exception:
                raise CtpAccountSessionProviderError("QUERY_ROW_INVALID") from None
            projected = state.rows.get(key)
            if projected is None:
                projected = new_rows.get(key)
            if projected is None:
                projected = _project_execution_row(
                    kind, row, state.projector, account_key, settings, observed_at
                )
                new_rows[key] = projected
                if type(projected) is _ProjectedExecutionRow and projected.attribution_refused:
                    note("ORDER_ATTRIBUTION_REFUSED", key)
            if type(projected) is not _ProjectedExecutionRow:
                continue
            if key in state.conflicted or key in new_conflicts:
                continue
            groups.setdefault(projected.identity, []).append((key, projected))
        accepted: list[tuple[tuple[str, str], _ProjectedExecutionRow]] = []
        for members in groups.values():
            first_key, first = members[0]
            if any(
                item.fact_id != first.fact_id or item.fact_digest != first.fact_digest
                for _key, item in members[1:]
            ):
                for key, _item in members:
                    refuse("CONFLICTING_DUPLICATE_ROW", key)
                continue
            for key, _item in members[1:]:
                note("DUPLICATE_ROW_MERGED", key)
            accepted.append((first_key, first))
        return accepted

    orders = collect("order", order_rows)
    trades = collect("trade", trade_rows)
    staged_facts: dict[str, str] = {}
    staged_progress: dict[str, tuple[bool, Decimal]] = {}
    queued: list[AccountExecutionOrderObservation | AccountExecutionTradeObservation] = []

    def known_digest(fact_id: str) -> str | None:
        digest = state.facts.get(fact_id)
        return staged_facts.get(fact_id) if digest is None else digest

    for key, item in orders:
        known = known_digest(item.fact_id)
        if known is not None:
            if known != item.fact_digest:
                refuse("FACT_CONFLICT", key)
            continue
        order = item.observation.order  # type: ignore[union-attr]
        filled = Decimal(order.filled_quantity)
        previous = state.order_progress.get(order.order_id)
        if previous is not None and (previous[0] or filled < previous[1]):
            # A new state after a terminal one, or a lower cumulative fill.
            refuse("ORDER_STATE_REGRESSED", key)
            continue
        staged_facts[item.fact_id] = item.fact_digest
        staged_progress[order.order_id] = (order.status in _TERMINAL_EXECUTION_STATUSES, filled)
        queued.append(item.observation)
    for key, item in trades:
        known = known_digest(item.fact_id)
        if known is not None:
            if known != item.fact_digest:
                refuse("FACT_CONFLICT", key)
            continue
        order_id = item.observation.trade.order_id  # type: ignore[union-attr]
        if (
            order_id is not None
            and order_id not in state.order_progress
            and order_id not in staged_progress
        ):
            note("TRADE_ORDER_UNOBSERVED", key)
            continue
        staged_facts[item.fact_id] = item.fact_digest
        queued.append(item.observation)
    if (
        len(state.rows) + len(new_rows) > _MAX_EXECUTION_TRACKED
        or len(state.facts) + len(staged_facts) > _MAX_EXECUTION_TRACKED
    ):
        raise CtpAccountSessionProviderError("EXECUTION_CAPACITY_EXCEEDED")
    # Commit: nothing above mutated the generation state.
    for key, projected in new_rows.items():
        state.rows[key] = projected
        if type(projected) is str:
            counters[projected] = counters.get(projected, 0) + 1
    for key, code in new_conflicts.items():
        state.conflicted.add(key)
        counters[code] = counters.get(code, 0) + 1
    for code, key in new_notes:
        state.noted.add((code, key))
        counters[code] = counters.get(code, 0) + 1
    state.facts.update(staged_facts)
    state.order_progress.update(staged_progress)
    state.pending.extend(queued)


def _native_stop_confirmed(
    receipt: object, *, expected_generation: int | None
) -> bool:
    return bool(
        type(receipt) is CtpNativeStopReceipt
        and type(receipt.connection_generation) is int
        and receipt.connection_generation > 0
        and (
            expected_generation is None
            or receipt.connection_generation == expected_generation
        )
        and receipt.complete is True
        and receipt.native_released is True
        and receipt.thread_alive is False
        and receipt.timed_out is False
        and (receipt.join_required is False or receipt.join_completed is True)
    )


def _same_session_scope(left: object, right: object) -> bool:
    return all(
        getattr(left, field, None) == getattr(right, field, None)
        for field in (
            "issuer",
            "account_fingerprint",
            "connection_generation",
            "trading_day",
            "broker_id",
            "investor_id",
            "read_only_ready",
        )
    )


def _build_observation(
    account_key: AccountKey,
    results: Mapping[str, QueryResult[Any]],
    utc_clock: datetime,
) -> AccountObservation:
    if type(utc_clock) is not datetime or utc_clock.utcoffset() != timedelta(0):
        raise CtpAccountSessionProviderError("QUERY_ROW_INVALID")
    try:
        account_rows = tuple(results["account"].records)
        position_rows = tuple(results["positions"].records)
        order_rows = tuple(results["orders"].records)
        if not account_rows or len(account_rows) > 64 or len(position_rows) > 2048 or len(order_rows) > 2048:
            raise ValueError
        started = min(result.started_at_utc for result in results.values())
        completed = max(result.completed_at_utc for result in results.values())
        if (
            not isinstance(started, datetime)
            or not isinstance(completed, datetime)
            or started.utcoffset() != timedelta(0)
            or completed.utcoffset() != timedelta(0)
            or completed < started
            or completed > utc_clock
        ):
            raise ValueError
        started_text = _utc_text(started)
        completed_text = _utc_text(completed)
        observed_at = completed_text
        balances: list[AccountBalance] = []
        currencies: set[str] = set()
        for row in account_rows:
            if _text(_field(row, "AccountID")) != account_key.account_id:
                raise ValueError
            if _text(_field(row, "BrokerID")) != account_key.subaccount_id:
                raise ValueError
            currency = _text(_field(row, "CurrencyID"))
            if currency in currencies:
                raise ValueError
            currencies.add(currency)
            balances.append(
                AccountBalance(
                    resource=_RESOURCE,
                    currency=currency,
                    total=_decimal_text(_field(row, "Balance")),
                    available=_decimal_text(_field(row, "Available")),
                    locked=None,
                    observed_at_utc=observed_at,
                )
            )
        if not balances:
            raise ValueError
        positions = _project_positions(position_rows, observed_at)
        open_orders = _project_open_orders(order_rows, observed_at)
        valuation = None
        if len(account_rows) == 1:
            row = account_rows[0]
            valuation = AccountValuation(
                currency=_text(_field(row, "CurrencyID")),
                available_cash=_decimal_text(_field(row, "Available")),
                equity=_decimal_text(_field(row, "Balance")),
                observed_at_utc=observed_at,
            )
        observation_id = "ctp-" + uuid.uuid4().hex
        return AccountObservation(
            account_key=account_key,
            observation_id=observation_id,
            coverage=(_RESOURCE,),
            balances=tuple(balances),
            positions=positions,
            valuation=valuation,
            started_at_utc=started_text,
            completed_at_utc=completed_text,
            consistency="multi_query",
            balances_complete=True,
            positions_complete=True,
            open_orders=open_orders,
            open_orders_complete=True,
        )
    except CtpAccountSessionProviderError:
        raise
    except Exception:
        raise CtpAccountSessionProviderError("QUERY_ROW_INVALID") from None


def _project_positions(rows: tuple[Any, ...], observed_at: str) -> tuple[AccountPosition, ...]:
    totals: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in rows:
        instrument_id = _text(_field(row, "InstrumentID"))
        exchange_id = _text(_field(row, "ExchangeID"))
        side_code = _text(_field(row, "PosiDirection"))
        side = {"1": "net", "2": "long", "3": "short"}.get(side_code)
        if side is None:
            raise ValueError
        quantity = _integer(_field(row, "Position"))
        if quantity < 0:
            raise ValueError
        if quantity:
            totals[(exchange_id, instrument_id, side)] += quantity
    if len(totals) > 2048:
        raise ValueError
    projected: list[AccountPosition] = []
    for (exchange_id, instrument_id, side), quantity in sorted(totals.items()):
        instrument = MarketInstrumentKey(
            provider="ctp",
            venue=exchange_id,
            environment="simnow",
            product_type="future",
            instrument_id=instrument_id,
            native_symbol=instrument_id,
        )
        projected.append(
            AccountPosition(
                resource=_RESOURCE,
                instrument=instrument,
                side=side,
                quantity=str(quantity),
                qty_unit="contract",
                average_price=None,
                price_currency=None,
                observed_at_utc=observed_at,
            )
        )
    return tuple(projected)


def _project_open_orders(rows: tuple[Any, ...], observed_at: str) -> tuple[AccountOpenOrder, ...]:
    projected: list[AccountOpenOrder] = []
    seen_ids: set[str] = set()
    for row in rows:
        status_code = _text(_field(row, "OrderStatus"))
        if status_code in _TERMINAL_ORDER_STATUS:
            continue
        if status_code not in _ACTIVE_ORDER_STATUS:
            raise ValueError
        # This legacy process provider has no SDK-issued order-observation
        # route/issuer binding.  The public AccountOpenOrder DTO must not carry
        # CTP OrderSysID, FrontID, SessionID, or OrderRef as its identifiers.
        # Until the trusted SDK context is propagated into this process, an
        # active order makes this observation unavailable instead of leaking
        # native identity or claiming that the order set is empty.
        raise CtpAccountSessionProviderError(
            "ORDER_OBSERVATION_IDENTITY_UNAVAILABLE"
        )
        direction = _text(_field(row, "Direction"))
        if direction not in {"0", "1"}:
            raise ValueError
        offset = _text(_field(row, "CombOffsetFlag"))
        if type(offset) is not str or len(offset) != 1 or offset not in "0123456":
            raise ValueError
        side = "buy" if direction == "0" else "sell"
        position_side = (
            ("long" if side == "buy" else "short")
            if offset == "0"
            else ("short" if side == "buy" else "long")
        )
        quantity = _integer(_field(row, "VolumeTotalOriginal"))
        filled = _integer(_field(row, "VolumeTraded"))
        if quantity <= 0 or filled < 0 or filled >= quantity:
            raise ValueError
        instrument_id = _text(_field(row, "InstrumentID"))
        exchange_id = _text(_field(row, "ExchangeID"))
        order_id = _order_id(row)
        if order_id in seen_ids:
            raise ValueError
        seen_ids.add(order_id)
        order_type_code = _text(_field(row, "OrderPriceType"))
        order_type = {"1": "ctp_any_price", "2": "limit"}.get(order_type_code)
        if order_type is None:
            raise ValueError
        limit_price = (
            _decimal_text(_field(row, "LimitPrice")) if order_type_code == "2" else None
        )
        if limit_price is not None and Decimal(limit_price) <= 0:
            raise ValueError
        client_order_id = _optional_text(_field(row, "OrderRef"))
        projected.append(
            AccountOpenOrder(
                resource=_RESOURCE,
                instrument=MarketInstrumentKey(
                    provider="ctp",
                    venue=exchange_id,
                    environment="simnow",
                    product_type="future",
                    instrument_id=instrument_id,
                    native_symbol=instrument_id,
                ),
                order_id=order_id,
                side=side,
                position_side=position_side,
                order_type=order_type,
                status="partially_filled" if filled else "open",
                quantity=str(quantity),
                filled_quantity=str(filled),
                qty_unit="contract",
                created_at_utc=_ctp_datetime_utc(
                    _field(row, "InsertDate"), _field(row, "InsertTime")
                ),
                observed_at_utc=observed_at,
                client_order_id=client_order_id,
                limit_price=limit_price,
                stop_price=None,
                price_currency=None,
                reduce_only=None,
            )
        )
    if len(projected) > 2048:
        raise ValueError
    return tuple(projected)


def _field(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        if name not in row:
            raise ValueError
        return row[name]
    return getattr(row, name)


def _text(value: Any) -> str:
    if type(value) is bytes:
        value = value.decode("gbk", errors="strict")
    if type(value) not in (str, int) or not str(value).strip():
        raise ValueError
    result = str(value).strip()
    if not result.isprintable() or len(result.encode("utf-8")) > 256:
        raise ValueError
    return result


def _optional_text(value: Any) -> str | None:
    if value is None or (type(value) is str and not value) or (type(value) is bytes and not value):
        return None
    return _text(value)


def _decimal_text(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError from None
    if not number.is_finite():
        raise ValueError
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _integer(value: Any) -> int:
    number = Decimal(_decimal_text(value))
    if number != number.to_integral_value():
        raise ValueError
    return int(number)


def _order_id(row: Any) -> str:
    exchange_id = _text(_field(row, "ExchangeID"))
    system_id = _optional_text(_field(row, "OrderSysID"))
    if system_id is not None:
        return f"{exchange_id}:{system_id}"
    order_ref = _text(_field(row, "OrderRef"))
    front_id = _integer(_field(row, "FrontID"))
    session_id = _integer(_field(row, "SessionID"))
    trading_day = _text(_field(row, "TradingDay"))
    return f"{exchange_id}:{trading_day}:{front_id}:{session_id}:{order_ref}"


def _ctp_datetime_utc(date_value: Any, time_value: Any) -> str:
    date_text = _text(date_value)
    time_text = _text(time_value)
    created = datetime.strptime(date_text + time_text, "%Y%m%d%H:%M:%S").replace(
        tzinfo=_CHINA
    )
    return _utc_text(created.astimezone(_UTC))


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(_UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["CtpAccountSessionProviderError", "CtpReadOnlyAccountSessionProvider"]
