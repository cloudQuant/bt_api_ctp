"""Local, source-sealed raw order and trade queries for a borrowed CTP Trader.

This is a bounded single-query observation, not an atomic account snapshot or
proof of all venue history.  It performs no connection, projection, or write.
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

from bt_api_ctp.ctp.client import TraderClient, _TraderSpi
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


class CtpGatewayReturnQueryError(RuntimeError):
    """Finite, value-free local reader failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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
            raise CtpGatewayReturnQueryError("QUERY_UNTRUSTED")
        source = result.query_source
        family = "orders" if kind == "orders" else "fills"
        try:
            valid = validate_ctp_reconciliation_query_result(
                result, binding, family, expected_record_limit=_MAX_ROWS
            )
        except Exception:
            valid = False
        if not valid:
            raise CtpGatewayReturnQueryError("QUERY_UNTRUSTED")
        expected = (
            native_account_query_filters(
                "orders", broker_id=binding.broker_id, investor_id=binding.investor_id
            )
            if kind == "orders"
            else {
                "BrokerID": binding.broker_id,
                "InvestorID": binding.investor_id,
                "InstrumentID": "",
                "ExchangeID": "",
                "TradeID": "",
                "TradeTimeStart": "",
                "TradeTimeEnd": "",
            }
        )
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
            raise CtpGatewayReturnQueryError("QUERY_UNTRUSTED")
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

    def read(self, *, kind: str, timeout: float) -> CtpGatewayReturnQueryBatch:
        self._check_owner()
        # A failed owner-thread read revokes any prior success ticket.
        self._ticket = None
        if (
            type(kind) is not str
            or kind not in {"orders", "trades"}
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 300
        ):
            raise CtpGatewayReturnQueryError("INVALID_REQUEST")
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
