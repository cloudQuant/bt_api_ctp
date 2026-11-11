"""Local, source-sealed raw order and trade queries for a borrowed CTP Trader.

This is a bounded single-query observation, not an atomic account snapshot or
proof of all venue history.  It performs no connection, projection, or write.

``read`` waits for the venue on the calling owner thread. ``begin_read`` and
``poll_read`` perform the same query as request and response on that thread
without ever waiting (I63-E130); both end in the same checks.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from bt_api_ctp.ctp.client import (
    TraderClient,
    _query_filter_items,
    _read_native_query_filter_items,
    _read_native_query_parameter_items,
    _TraderSpi,
)
from bt_api_ctp.ctp.ctp_structs_query import CThostFtdcQryOrderField, CThostFtdcQryTradeField
from bt_api_ctp.query import (
    QueryResult,
    _QUERY_EVIDENCE_MAX_TTL_SECONDS,
    _QuerySource,
    _query_records_digest,
    native_account_query_filters,
)
from bt_api_ctp.query_result_validation import validate_ctp_reconciliation_query_result
from bt_api_ctp.registered_sim_session import (
    CtpQuerySessionBinding,
    capture_query_session,
    validate_query_session,
)

_MAX_ROWS = 8192
_MAX_FIELDS = 256
_MAX_FIELD_NAME_BYTES = 128
_MAX_TEXT_BYTES = 4096
_MAX_BATCH_BYTES = 16 * 1024 * 1024
_BATCH_SEAL = object()
_UTC = timezone.utc
# Why a native result was not accepted (I63-E134 asks which one happens live).
# The code only names the reason; acceptance is decided by the checks alone.
_FAILURE_CODES = MappingProxyType(
    {
        "timeout": "QUERY_TIMEOUT",
        "deadline": "QUERY_TIMEOUT",
        "query_slot_busy": "QUERY_SLOT_BUSY",
        "submit_rejected": "QUERY_SUBMIT_REJECTED",
        "submit_exception": "QUERY_SUBMIT_FAILED",
        "response_error": "QUERY_VENUE_ERROR",
        "front_disconnected": "QUERY_DISCONNECTED",
        "client_stop": "QUERY_DISCONNECTED",
        "local_cancel": "QUERY_CANCELLED",
        "local_preflight": "QUERY_PREFLIGHT_FAILED",
        "record_limit": "ROW_LIMIT_EXCEEDED",
    }
)


class CtpGatewayReturnQueryError(RuntimeError):
    """Finite, value-free local reader failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _untrusted_code(result: object) -> str:
    """Name why a result is not trusted; never decides that it is trusted.

    A rejected send carries the native return code (-1 network, -2 too many
    outstanding requests, -3 more than the allowed requests per second), a
    venue error its ErrorID; both are bounded numbers, not account values.
    """
    if type(result) is not QueryResult:
        return "QUERY_UNTRUSTED"
    origin, number = result.first_completion_origin, result.error_code
    if origin == "query_response" and type(number) is int and number != 0:
        code = "QUERY_VENUE_ERROR"  # an error carried by the query's own answer
    else:
        code = _FAILURE_CODES.get(origin) if type(origin) is str else None
    if code is None:
        late = result.late_callback_count
        return "QUERY_LATE_CALLBACK" if type(late) is int and late > 0 else "QUERY_UNTRUSTED"
    if (code in ("QUERY_SUBMIT_REJECTED", "QUERY_VENUE_ERROR") and type(number) is int
            and 0 < abs(number) < 10_000):
        return f"{code}_{abs(number)}"
    return code


@dataclass(frozen=True, slots=True, eq=False)
class CtpGatewayReturnQueryRequest:
    """Opaque handle of one ``begin_read``; its state stays in the reader."""

    kind: str


@dataclass(slots=True, eq=False)
class _PendingRead:
    request: CtpGatewayReturnQueryRequest
    kind: str
    timeout: float
    binding: CtpQuerySessionBinding
    send_by: float
    accumulator: Any = None
    answer_by: float = 0.0
    batch: CtpGatewayReturnQueryBatch | None = None


@dataclass(frozen=True, slots=True)
class CtpGatewayReturnQueryBatch:
    """One immutable local batch; raw fields are not an RPC or authority token."""

    kind: str
    rows: tuple[Mapping[str, str | int | float], ...] = field(repr=False)
    request_id: int
    source_ref: str
    generation: str
    connection_generation: int
    trading_day: str
    source_tags: tuple[str, str, str, str, int, int] = field(repr=False)
    records_sha256: str
    started_at_utc: datetime
    completed_at_utc: datetime
    expires_at_utc: datetime
    expires_monotonic: float
    _binding: CtpQuerySessionBinding = field(repr=False, compare=False)
    _source: _QuerySource = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _BATCH_SEAL:
            raise CtpGatewayReturnQueryError("BATCH_UNTRUSTED")


def _full_day_filters(kind: str, broker_id: str, investor_id: str) -> dict[str, str]:
    """The exact native filters of an unfiltered full-day order or trade query."""
    if kind == "orders":
        return native_account_query_filters("orders", broker_id=broker_id, investor_id=investor_id)
    return {
        "BrokerID": broker_id,
        "InvestorID": investor_id,
        "InstrumentID": "",
        "ExchangeID": "",
        "TradeID": "",
        "TradeTimeStart": "",
        "TradeTimeEnd": "",
    }


def _scope_text(value: object) -> str:
    if type(value) is not str:
        raise CtpGatewayReturnQueryError("ROW_SCOPE_INVALID")
    text = value.strip().strip("\x00").strip()
    if not text or not text.isprintable() or len(text.encode("utf-8")) > 256:
        raise CtpGatewayReturnQueryError("ROW_SCOPE_INVALID")
    return text


def _freeze_rows(
    rows: tuple[Any, ...], binding: CtpQuerySessionBinding
) -> tuple[Mapping[str, str | int | float], ...]:
    if len(rows) > _MAX_ROWS:
        raise CtpGatewayReturnQueryError("ROW_LIMIT_EXCEEDED")
    result: list[Mapping[str, str | int | float]] = []
    total = 0
    for row in rows:
        if not isinstance(row, Mapping) or not 1 <= len(row) <= _MAX_FIELDS:
            raise CtpGatewayReturnQueryError("ROW_INVALID")
        copied: dict[str, str | int | float] = {}
        for name, value in row.items():
            if (
                type(name) is not str
                or not name.isascii()
                or not name.isidentifier()
                or len(name) > _MAX_FIELD_NAME_BYTES
            ):
                raise CtpGatewayReturnQueryError("ROW_INVALID")
            if type(value) is str:
                try:
                    length = len(value.encode("utf-8"))
                except UnicodeError:
                    raise CtpGatewayReturnQueryError("ROW_INVALID") from None
                if length > _MAX_TEXT_BYTES:
                    raise CtpGatewayReturnQueryError("ROW_INVALID")
                total += length
            elif type(value) is int:
                if not -(2**63) <= value < 2**63:
                    raise CtpGatewayReturnQueryError("ROW_INVALID")
                total += 8
            elif type(value) is float:
                if not math.isfinite(value):
                    raise CtpGatewayReturnQueryError("ROW_INVALID")
                total += 8
            else:
                raise CtpGatewayReturnQueryError("ROW_INVALID")
            total += len(name)
            if total > _MAX_BATCH_BYTES:
                raise CtpGatewayReturnQueryError("ROW_LIMIT_EXCEEDED")
            copied[name] = value
        if (
            _scope_text(copied.get("BrokerID")) != binding.broker_id
            or _scope_text(copied.get("InvestorID")) != binding.investor_id
            or _scope_text(copied.get("TradingDay")) != binding.trading_day
        ):
            raise CtpGatewayReturnQueryError("ROW_SCOPE_INVALID")
        result.append(MappingProxyType(copied))
    return tuple(result)


class CtpGatewayReturnQueryReader:
    """Read bounded order/trade results on the owner thread of one live Trader.

    Rows contain at most 256 fields, 4096 UTF-8 bytes per string and 16 MiB per
    batch, and only exact strings, signed 64-bit integers and finite floats.
    The caller owns the Trader and its lifetime; this reader never starts it.
    """

    def __init__(
        self, trader: TraderClient, *, environment: str, broker_id: str, investor_id: str
    ) -> None:
        if (
            type(trader) is not TraderClient
            or type(environment) is not str
            or not environment
            or environment != environment.strip()
            or type(broker_id) is not str
            or not broker_id
            or broker_id != broker_id.strip()
            or type(investor_id) is not str
            or not investor_id
            or investor_id != investor_id.strip()
        ):
            raise CtpGatewayReturnQueryError("INVALID_CONFIGURATION")
        self._pid = os.getpid()
        self._thread = threading.current_thread()
        self._trader = trader
        self._native_owner_trader = trader
        self._environment = environment
        try:
            with trader._query_state_lock:
                scope = trader._trade_callback_scope_locked()
                api, spi = trader._api, trader._spi
                binding = capture_query_session(trader, environment=environment)
                if (
                    api is None
                    or type(spi) is not _TraderSpi
                    or spi._native_api is not api
                    or trader._api is not api or trader._spi is not spi
                    or trader._session_native_api is not api
                    or not spi._is_current_locked()
                    or binding.broker_id != broker_id
                    or binding.investor_id != investor_id
                    or any(len(value.encode("utf-8")) > 256 for value in binding._source_tags[:4])
                    or not validate_query_session(trader, binding, now_monotonic=time.monotonic())
                    or trader._api is not api or trader._spi is not spi
                    or spi._native_api is not api or trader._session_native_api is not api
                    or not spi._is_current_locked()
                    or not trader._trade_callback_scope_matches_locked(scope)
                ):
                    raise ValueError
                self._binding, self._api, self._spi = binding, api, spi
                self._callback_scope = scope
                self._ticket: CtpGatewayReturnQueryBatch | None = None
                self._pending: _PendingRead | None = None
        except Exception:
            raise CtpGatewayReturnQueryError("SESSION_UNTRUSTED") from None

    def _check_owner(self) -> None:
        if os.getpid() != self._pid or threading.current_thread() is not self._thread:
            raise CtpGatewayReturnQueryError("OWNER_REQUIRED")

    @property
    def environment(self) -> str:
        self._check_owner()
        return self._environment

    @property
    def broker_id(self) -> str:
        self._check_owner()
        return self._binding.broker_id

    @property
    def investor_id(self) -> str:
        self._check_owner()
        return self._binding.investor_id

    def _capture_locked(self) -> CtpQuerySessionBinding:
        trader = self._trader
        if (
            trader is not self._native_owner_trader
            or trader._api is not self._api
            or trader._spi is not self._spi
            or self._spi._native_api is not self._api
            or trader._session_native_api is not self._api
            or not self._spi._is_current_locked()
            or not trader._trade_callback_scope_matches_locked(self._callback_scope)
        ):
            raise CtpGatewayReturnQueryError("SESSION_CHANGED")
        try:
            if not validate_query_session(trader, self._binding, now_monotonic=time.monotonic()):
                raise ValueError
            current = capture_query_session(trader, environment=self._environment)
            if not validate_query_session(trader, current, now_monotonic=time.monotonic()):
                raise ValueError
            if (
                current._issuer is not self._binding._issuer
                or current._source_tags != self._binding._source_tags
                or current.source_ref != self._binding.source_ref
                or current.generation != self._binding.generation
                or current.trading_day != self._binding.trading_day
                or current.broker_id != self._binding.broker_id
                or current.investor_id != self._binding.investor_id
                or self._trader is not trader or self._native_owner_trader is not trader
                or trader._api is not self._api or trader._spi is not self._spi
                or self._spi._native_api is not self._api
                or trader._session_native_api is not self._api
                or not self._spi._is_current_locked()
                or not trader._trade_callback_scope_matches_locked(self._callback_scope)
            ):
                raise ValueError
            return current
        except Exception:
            raise CtpGatewayReturnQueryError("SESSION_CHANGED") from None

    def _assert_same_native_locked(self) -> None:
        trader = self._native_owner_trader
        if (self._trader is not trader or trader._api is not self._api
                or trader._spi is not self._spi or self._spi._native_api is not self._api
                or trader._session_native_api is not self._api
                or not self._spi._is_current_locked()):
            raise CtpGatewayReturnQueryError("SESSION_CHANGED")

    def assert_same_native(self) -> None:
        """Check exact native objects only; a changed login remains unverified."""
        self._check_owner()
        with self._native_owner_trader._query_state_lock:
            self._assert_same_native_locked()

    def rebind_same_native(self) -> CtpGatewayReturnQueryReader:
        """Bind the current login atomically to this reader's exact native owner."""
        self._check_owner()
        trader = self._native_owner_trader
        with trader._query_state_lock:
            self._assert_same_native_locked()
            rebound = CtpGatewayReturnQueryReader(
                trader, environment=self._environment, broker_id=self._binding.broker_id,
                investor_id=self._binding.investor_id,
            )
            self._assert_same_native_locked()
            rebound._capture_locked()
            self._assert_same_native_locked()
            return rebound

    def assert_session_current(self) -> None:
        """Recheck the captured login and source without issuing a query or ticket."""
        self._check_owner()
        with self._trader._query_state_lock:
            self._capture_locked()

    @staticmethod
    def _validate_result(
        result: object, binding: CtpQuerySessionBinding, kind: str
    ) -> _QuerySource:
        if type(result) is not QueryResult or type(result.query_source) is not _QuerySource:
            raise CtpGatewayReturnQueryError(_untrusted_code(result))
        source = result.query_source
        family = "orders" if kind == "orders" else "fills"
        try:
            valid = validate_ctp_reconciliation_query_result(
                result, binding, family, expected_record_limit=_MAX_ROWS
            )
        except Exception:
            valid = False
        if not valid:
            raise CtpGatewayReturnQueryError(_untrusted_code(result))
        expected = _full_day_filters(kind, binding.broker_id, binding.investor_id)
        if (
            not 0 < result.request_id <= 2_147_483_647
            or type(source.request_id) is not int
            or type(source.trading_day) is not str
            or type(source.broker_id) is not str
            or type(source.investor_id) is not str
            or type(source.source_tags) is not tuple
            or len(source.source_tags) != 6
            or any(type(value) is not str for value in source.source_tags[:4])
            or any(type(value) is not int for value in source.source_tags[4:])
            or any(
                type(filters) is not tuple
                or any(
                    type(item) is not tuple
                    or len(item) != 2
                    or type(item[0]) is not str
                    or type(item[1]) is not str
                    for item in filters
                )
                for filters in (source.request_filters, source.request_intent_filters)
            )
            or source.request_filters != tuple(sorted(expected.items()))
            or source.request_intent_filters != tuple(sorted(expected.items()))
            or source.request_parameters != ()
            or source.request_intent_parameters != ()
            or source.explicit_request_filters != ()
            or type(source.record_limit) is not int
            or source.record_limit != _MAX_ROWS
            or type(result.record_limit) is not int
            or result.record_limit != _MAX_ROWS
            or source.record_limit_exceeded is not False
            or result.first_completion_origin != "success"
            or source.completed_at_utc > datetime.now(_UTC)
            or source.trusted_expires_monotonic
            != source.completed_monotonic + _QUERY_EVIDENCE_MAX_TTL_SECONDS
        ):
            raise CtpGatewayReturnQueryError(_untrusted_code(result))
        return source

    def assert_current(self, batch: CtpGatewayReturnQueryBatch) -> None:
        """Recheck the exact latest batch, current login and sealed query TTL."""
        self._check_owner()
        if type(batch) is not CtpGatewayReturnQueryBatch or self._ticket is not batch:
            raise CtpGatewayReturnQueryError("BATCH_UNTRUSTED")
        with self._trader._query_state_lock:
            self._capture_locked()
            source = batch._source
            if (
                self._ticket is not batch
                or batch._seal is not _BATCH_SEAL
                or type(source) is not _QuerySource
                or source.issuer is not self._binding._issuer
                or source.source_tags != self._binding._source_tags
                or batch.source_ref != self._binding.source_ref
                or batch.generation != self._binding.generation
                or batch.records_sha256 != source.records_sha256
                or batch.request_id != source.request_id
                or batch.connection_generation != source.connection_generation
                or batch.trading_day != source.trading_day
                or batch.expires_at_utc != source.trusted_expires_at_utc
                or batch.expires_monotonic != source.trusted_expires_monotonic
                or time.monotonic() >= batch.expires_monotonic
                or datetime.now(_UTC) >= batch.expires_at_utc
            ):
                raise CtpGatewayReturnQueryError("BATCH_UNTRUSTED")
            self._capture_locked()
            if self._ticket is not batch:
                raise CtpGatewayReturnQueryError("BATCH_UNTRUSTED")

    @staticmethod
    def _check_request(kind: object, timeout: object) -> None:
        if (
            type(kind) is not str
            or kind not in {"orders", "trades"}
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 300
        ):
            raise CtpGatewayReturnQueryError("INVALID_REQUEST")

    def read(self, *, kind: str, timeout: float) -> CtpGatewayReturnQueryBatch:
        """Return one sealed batch of ``kind``.

        After ``poll_read`` reported a read of the same kind complete, this
        hands over that batch without native I/O. Otherwise it queries and
        waits for the venue on this owner thread, as it always did.
        """
        self._check_owner()
        pending = self._pending
        if pending is not None and pending.batch is not None and pending.kind == kind:
            self._pending = None
            try:
                self._check_request(kind, timeout)
                self.assert_current(pending.batch)
            except BaseException:
                self._ticket = None
                raise
            return pending.batch
        # A failed owner-thread read revokes any prior success ticket.
        self._ticket = None
        if pending is not None:
            # One read at a time; an outstanding one is discarded, never waited for.
            self._discard_pending()
            raise CtpGatewayReturnQueryError("QUERY_PENDING")
        self._check_request(kind, timeout)
        with self._trader._query_state_lock:
            binding = self._capture_locked()
        try:
            result = (
                self._trader.query_orders_result(timeout=timeout, max_records=_MAX_ROWS)
                if kind == "orders"
                else self._trader.query_trades_result(timeout=timeout, max_records=_MAX_ROWS)
            )
        except Exception:
            raise CtpGatewayReturnQueryError("QUERY_FAILED") from None
        return self._issue_batch(kind, binding, result)

    def begin_read(self, *, kind: str, timeout: float) -> CtpGatewayReturnQueryRequest:
        """Start one order or trade read without native I/O or any wait (I63-E130).

        ``poll_read`` sends the query once this Trader has no other query
        outstanding and its query interval has passed, then watches for the
        answer. A read that cannot be sent within ``timeout`` plus one query
        interval, or is not answered within ``timeout`` after it was sent,
        fails like ``read``. Only one read exists at a time: beginning another
        discards the previous one.
        """
        self._check_owner()
        self._ticket = None
        self._discard_pending()
        self._check_request(kind, timeout)
        with self._trader._query_state_lock:
            binding = self._capture_locked()
            interval = self._trader._query_interval
        if (type(interval) not in (int, float) or not math.isfinite(interval)
                or not 0 <= interval <= 300):
            raise CtpGatewayReturnQueryError("INVALID_CONFIGURATION")
        request = CtpGatewayReturnQueryRequest(kind)
        self._pending = _PendingRead(
            request, kind, float(timeout), binding, time.monotonic() + timeout + interval
        )
        return request

    def poll_read(self, request: CtpGatewayReturnQueryRequest) -> bool:
        """Advance a read begun by ``begin_read``; never sleeps or waits (I63-E130).

        False: still outstanding. True: the sealed batch is ready and the next
        ``read`` of the same kind hands it over. A failed read raises a finite
        ``CtpGatewayReturnQueryError`` and is discarded.
        """
        self._check_owner()
        pending = self._pending
        if pending is None or pending.request is not request:
            raise CtpGatewayReturnQueryError("REQUEST_UNKNOWN")
        if pending.batch is not None:
            return True
        try:
            if pending.accumulator is None and not self._send_pending(pending):
                return False
            result = self._pending_result(pending)
            if result is None:
                return False
            pending.batch = self._issue_batch(pending.kind, pending.binding, result)
        except CtpGatewayReturnQueryError:
            self._discard_pending()
            raise
        except Exception:
            self._discard_pending()
            raise CtpGatewayReturnQueryError("QUERY_FAILED") from None
        except BaseException:
            self._discard_pending()
            raise
        return True

    def cancel_read(self, request: CtpGatewayReturnQueryRequest) -> None:
        """Discard a read begun by ``begin_read``; a sent query is sealed locally.

        CTP has no query cancel: the Trader keeps its query lane reserved until
        the venue's last packet or a new connection, as after a local timeout.
        A handle that is not the current read is ignored.
        """
        self._check_owner()
        pending = self._pending
        if pending is not None and pending.request is request:
            if pending.batch is not None and self._ticket is pending.batch:
                self._ticket = None
            self._discard_pending()

    def _discard_pending(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None and pending.accumulator is not None:
            with self._trader._query_state_lock:
                self._seal_locked(pending.accumulator, "local_cancel", -1, "query_cancelled")

    @staticmethod
    def _seal_locked(
        accumulator: Any,
        origin: str | None = None,
        error_code: int | None = None,
        message: str = "",
        *,
        timed_out: bool = False,
    ) -> None:
        """End an accumulator as ``TraderClient._execute_query`` does; a received
        final packet keeps its own outcome. Caller holds ``_query_state_lock``."""
        if origin is not None and not (
            accumulator.event.is_set()
            or accumulator.is_last_seen
            or accumulator.error_code not in (None, 0)
        ):
            accumulator.record_completion_origin(origin)
            if error_code is not None:
                accumulator.error_code = error_code
            accumulator.error_message = message
            accumulator.timed_out = timed_out
        if accumulator.completed_at_utc is None:
            accumulator.completed_at_utc = datetime.now(_UTC)
            accumulator.completed_monotonic = time.monotonic()
        accumulator.sealed = True
        accumulator.event.set()

    def _send_pending(self, pending: _PendingRead) -> bool:
        """Send the query when the Trader's query lane is free; False: try later.

        The lane rules are those of ``TraderClient._execute_query``: one native
        query outstanding per session and the query interval between sends,
        shared with every other query of this Trader. A busy lane is left to a
        later poll instead of being waited for.
        """
        trader = self._trader
        if time.monotonic() >= pending.send_by:
            raise CtpGatewayReturnQueryError("QUERY_SLOT_BUSY")
        if getattr(trader._native_callback_thread_state, "depth", 0) > 0:
            return False
        if not trader._query_lock.acquire(blocking=False):
            return False
        try:
            if not trader._query_state_lock.acquire(blocking=False):
                return False
            try:
                self._capture_locked()
                if (
                    trader._query_slot_blocking_locked()
                    or time.monotonic() - trader._last_query_submitted_at < trader._query_interval
                ):
                    return False
                api = trader._api
                method = "ReqQryOrder" if pending.kind == "orders" else "ReqQryTrade"
                try:
                    native = (
                        CThostFtdcQryOrderField()
                        if pending.kind == "orders"
                        else CThostFtdcQryTradeField()
                    )
                    intent = _full_day_filters(
                        pending.kind, trader._bound_broker_id, trader._bound_user_id
                    )
                    for name, value in intent.items():
                        setattr(native, name, value)
                    intent_items = _query_filter_items(intent)
                    filter_items = _read_native_query_filter_items(native, intent_items)
                    parameter_items = _read_native_query_parameter_items(native, ())
                    if not callable(getattr(api, method, None)):
                        raise ValueError
                except Exception:
                    raise CtpGatewayReturnQueryError("QUERY_PREFLIGHT_FAILED") from None
                accumulator = trader._new_query_accumulator(
                    pending.kind,
                    dict(filter_items),
                    request_intent_filters=dict(intent_items),
                    request_parameters=dict(parameter_items),
                    request_intent_parameters={},
                    record_limit=_MAX_ROWS,
                )
                # Reserve the lane before releasing it, as the blocking path does.
                trader._query_native_inflight = (
                    accumulator.request_id,
                    pending.kind,
                    accumulator.connection_generation,
                )
                trader._last_query_submitted_at = time.monotonic()
                pending.accumulator = accumulator
            finally:
                trader._query_state_lock.release()
        finally:
            trader._query_lock.release()
        # Native code runs with every SDK lock released; a fake or vendor Req
        # may deliver its callbacks synchronously on this thread.
        try:
            trader._record_request(f"query_{pending.kind}")
            sent = trader._invoke_session_native_request(
                api, method, native, accumulator.request_id
            )
            if sent is not None and type(sent) is not int:
                raise TypeError
        except Exception:
            with trader._query_state_lock:
                # A failed send is not tracked as outstanding.
                trader._release_query_slot_locked(accumulator.request_id, pending.kind)
                self._seal_locked(accumulator, "submit_exception", -4, "query_submit_exception")
            raise CtpGatewayReturnQueryError("QUERY_SUBMIT_FAILED") from None
        accumulator.submit_code = sent
        if sent not in (None, 0):
            # A rejected Req (-1/-2/-3) never became a vendor-side request.
            with trader._query_state_lock:
                trader._release_query_slot_locked(accumulator.request_id, pending.kind)
                self._seal_locked(accumulator, "submit_rejected", sent, "query_submit_rejected")
            raise CtpGatewayReturnQueryError(_untrusted_code(accumulator.result()))
        pending.answer_by = time.monotonic() + pending.timeout
        return True

    def _pending_result(self, pending: _PendingRead) -> QueryResult | None:
        """The sent query's result once answered or timed out; None while waiting."""
        accumulator = pending.accumulator
        with self._trader._query_state_lock:
            self._capture_locked()
            if not (
                accumulator.event.is_set()
                or accumulator.is_last_seen
                or accumulator.error_code not in (None, 0)
            ):
                if time.monotonic() < pending.answer_by:
                    return None
                # Sealed under the callback lock: a later packet is only late.
                self._seal_locked(accumulator, "timeout", None, "query_timeout", timed_out=True)
            else:
                self._seal_locked(accumulator)
            return accumulator.result()

    def _issue_batch(
        self, kind: str, binding: CtpQuerySessionBinding, result: object
    ) -> CtpGatewayReturnQueryBatch:
        """Check one native result and issue it as the reader's only ticket."""
        with self._trader._query_state_lock:
            self._capture_locked()
            if (
                type(result) is QueryResult
                and type(result.records) is tuple
                and len(result.records) > _MAX_ROWS
            ):
                raise CtpGatewayReturnQueryError("ROW_LIMIT_EXCEEDED")
            source = self._validate_result(result, binding, kind)
            try:
                rows = _freeze_rows(result.records, binding)
            except CtpGatewayReturnQueryError:
                raise
            except Exception:
                raise CtpGatewayReturnQueryError("ROW_INVALID") from None
            try:
                if _query_records_digest(rows) != source.records_sha256:
                    raise ValueError
            except Exception:
                raise CtpGatewayReturnQueryError("QUERY_UNTRUSTED") from None
            if result.query_source is not source:
                raise CtpGatewayReturnQueryError("QUERY_UNTRUSTED")
            self._capture_locked()
            batch = CtpGatewayReturnQueryBatch(
                kind=kind,
                rows=rows,
                request_id=result.request_id,
                source_ref=binding.source_ref,
                generation=binding.generation,
                connection_generation=binding.connection_generation,
                trading_day=binding.trading_day,
                source_tags=binding._source_tags,
                records_sha256=source.records_sha256,
                started_at_utc=source.started_at_utc,
                completed_at_utc=source.completed_at_utc,
                expires_at_utc=source.trusted_expires_at_utc,
                expires_monotonic=source.trusted_expires_monotonic,
                _binding=binding,
                _source=source,
                _seal=_BATCH_SEAL,
            )
            self._ticket = batch
            try:
                # Freezing and hashing may consume the remaining evidence TTL.
                self.assert_current(batch)
            except BaseException:
                self._ticket = None
                raise
            return batch
