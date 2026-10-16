"""Typed, read-only CTP instrument metadata from one current native session.

This is intentionally separate from the legacy gateway adapter's cached
``get_symbol_info`` projection.  Each read issues a fresh exact instrument
query and verifies the live TraderClient-issued query source, current-session
readback, terminal callback state, and the exact CTP market stream's opaque
quote-clock source scope.  It does not claim an atomic account snapshot or
price-limit data absent from ``InstrumentField``.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from bt_api_ctp.containers.ctp.ctp_native_query_certificate import (
    CtpNativeQueryCertificateError,
    _query_result_shape,
    _record_value,
    _same_scope,
    _scope_for_client,
    _sha256_text,
    _source_for_result,
    _validate_bound_filters,
    _validate_returned_rows,
)
from bt_api_ctp.feeds.live_ctp_feed import (
    CtpMarketStream,
    CtpQuoteClockSourceScope,
    CtpRequestDataFuture,
    _is_current_ctp_quote_clock_source_scope,
)
from bt_api_ctp.md_identity import MdIdentityObservation
from bt_api_ctp.query import QueryResult, _QuerySource, _query_records_digest

_SCHEMA_ID = "ctp.instrument_metadata"
_SCHEMA_VERSION = 1
_SOURCE = "ctp.native.instruments"
_SOURCE_REVISION = "ctp.native.instrument-query.v1"
_MARKET_SOURCE = "ctp.native.md"
_MAX_IDENTIFIER_BYTES = 128
_CTP_INSTRUMENT_METADATA_SEAL = object()
_QUOTE_CLOCK_DOMAIN = re.compile(r"^ctp-md-process:[1-9][0-9]*:monotonic:([0-9a-f]{32})$")


class CtpInstrumentMetadataError(RuntimeError):
    """A finite error for metadata that lacks current CTP provenance."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CtpInstrumentMetadata:
    """One immutable instrument row with separate trader and quote sessions."""

    instrument_id: str
    exchange_id: str
    product_class: str
    asset_type: str
    price_tick: str
    volume_multiple: int
    lower_limit_price: str | None
    upper_limit_price: str | None
    price_limits_available: bool
    price_limits_source: str
    account_session_generation: str
    account_fingerprint: str
    trading_day: str
    source_connection_generation: int
    source_request_id: int
    source_records_sha256: str
    source_revision: str
    source: str
    query_started_at_utc: str
    observed_at_utc: str
    query_clock_domain_id: str
    query_elapsed_seconds: float
    quote_stream_generation: int
    quote_source: str
    clock_domain_id: str
    clock_source_session_id: str
    clock_source: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _CTP_INSTRUMENT_METADATA_SEAL:
            raise ValueError("CTP instrument metadata must be issued by its live reader")

    def to_dict(self) -> dict[str, object]:
        """Return the exact JSON-safe ServiceHost response shape."""

        return {
            "schema_id": _SCHEMA_ID,
            "schema_version": _SCHEMA_VERSION,
            "instrument_id": self.instrument_id,
            "exchange_id": self.exchange_id,
            "product_class": self.product_class,
            "asset_type": self.asset_type,
            "price_tick": self.price_tick,
            "volume_multiple": self.volume_multiple,
            "lower_limit_price": self.lower_limit_price,
            "upper_limit_price": self.upper_limit_price,
            "price_limits_available": self.price_limits_available,
            "price_limits_source": self.price_limits_source,
            "account_session_generation": self.account_session_generation,
            "account_fingerprint": self.account_fingerprint,
            "trading_day": self.trading_day,
            "source_connection_generation": self.source_connection_generation,
            "source_request_id": self.source_request_id,
            "source_records_sha256": self.source_records_sha256,
            "source_revision": self.source_revision,
            "source": self.source,
            "query_started_at_utc": self.query_started_at_utc,
            "observed_at_utc": self.observed_at_utc,
            "query_clock_domain_id": self.query_clock_domain_id,
            "query_elapsed_seconds": self.query_elapsed_seconds,
            "quote_stream_generation": self.quote_stream_generation,
            "quote_source": self.quote_source,
            "clock_domain_id": self.clock_domain_id,
            "clock_source_session_id": self.clock_source_session_id,
            "clock_source": self.clock_source,
        }


class CtpInstrumentMetadataReader:
    """Read fresh CTP instrument facts from an exact feed/market pair.

    Construct this inside the trusted account provider factory with the same
    ``CtpRequestDataFuture`` and ``CtpMarketStream`` that provider owns.  The
    reader is owner-thread-only and never accepts a trader client or expected
    query result from the caller.
    """

    def __init__(self, request_feed: CtpRequestDataFuture, market_stream: CtpMarketStream) -> None:
        if type(request_feed) is not CtpRequestDataFuture:
            raise CtpInstrumentMetadataError("FEED_BINDING_INVALID")
        if type(market_stream) is not CtpMarketStream:
            raise CtpInstrumentMetadataError("MARKET_BINDING_INVALID")
        self._request_feed = request_feed
        self._market_stream = market_stream
        self._owner_thread_id = threading.get_ident()

    @property
    def request_feed(self) -> CtpRequestDataFuture:
        return self._request_feed

    @property
    def market_stream(self) -> CtpMarketStream:
        return self._market_stream

    def read(
        self,
        instrument_id: str,
        exchange_id: str,
        *,
        account_session_generation: str,
        timeout: float,
    ) -> CtpInstrumentMetadata:
        """Query one exact instrument and prove its current producer scope."""

        self._check_owner()
        _require_identifier(instrument_id, "INSTRUMENT_ID_INVALID")
        _require_identifier(exchange_id, "EXCHANGE_ID_INVALID")
        _require_identifier(account_session_generation, "ACCOUNT_GENERATION_INVALID")
        timeout_value = _positive_timeout(timeout)

        trader = self._current_trader()
        try:
            initial_query_scope = _scope_for_client(trader)
        except CtpNativeQueryCertificateError as exc:
            raise CtpInstrumentMetadataError("TRADER_SESSION_UNREADY") from exc
        initial_quote_scope = self._current_quote_scope()
        initial_quote_identity = _quote_scope_identity(
            initial_quote_scope,
            self._market_stream,
            broker_id=initial_query_scope.broker_id,
            investor_id=initial_query_scope.investor_id,
            trading_day=initial_query_scope.trading_day,
        )

        start_monotonic = time.monotonic()
        try:
            result = self._request_feed.query_instruments_result(
                instrument_id=instrument_id,
                exchange_id=exchange_id,
                timeout=timeout_value,
            )
        except Exception as exc:
            raise CtpInstrumentMetadataError("INSTRUMENT_QUERY_FAILED") from exc
        finish_monotonic = time.monotonic()
        if finish_monotonic < start_monotonic or finish_monotonic - start_monotonic > timeout_value:
            raise CtpInstrumentMetadataError("INSTRUMENT_QUERY_LATE")
        if self._current_trader() is not trader:
            raise CtpInstrumentMetadataError("TRADER_SESSION_CHANGED")

        try:
            current_query_scope = _scope_for_client(trader)
            if not _same_scope(initial_query_scope, current_query_scope):
                raise CtpInstrumentMetadataError("TRADER_SESSION_CHANGED")
            source = self._validate_query_result(
                trader,
                result,
                current_query_scope,
                instrument_id,
                exchange_id,
                timeout_value,
            )
        except CtpNativeQueryCertificateError as exc:
            raise CtpInstrumentMetadataError("INSTRUMENT_QUERY_UNPROVEN") from exc

        final_quote_scope = self._current_quote_scope()
        final_quote_identity = _quote_scope_identity(
            final_quote_scope,
            self._market_stream,
            broker_id=current_query_scope.broker_id,
            investor_id=current_query_scope.investor_id,
            trading_day=current_query_scope.trading_day,
        )
        if initial_quote_identity != final_quote_identity:
            raise CtpInstrumentMetadataError("QUOTE_SESSION_CHANGED")

        row = result.records[0]
        try:
            price_tick = _positive_decimal_field(row, "PriceTick", "price_tick")
            volume_multiple = _positive_integer_field(row, "VolumeMultiple", "multiplier")
            product_class = _product_class(row)
            asset_type = {
                "1": "future",
                "2": "option",
                "3": "combination",
                "4": "spot",
                "5": "efp",
                "6": "spot_option",
                "7": "tas",
                "I": "mi",
            }[product_class]
            lower, upper, limits_available, limits_source = _price_limits(row)
        except CtpNativeQueryCertificateError as exc:
            raise CtpInstrumentMetadataError("INSTRUMENT_QUERY_UNPROVEN") from exc

        source_started = source.started_at_utc
        source_completed = source.completed_at_utc
        elapsed = source.completed_monotonic - source.started_monotonic
        if not math.isfinite(elapsed) or elapsed < 0 or elapsed > timeout_value:
            raise CtpInstrumentMetadataError("INSTRUMENT_QUERY_LATE")

        return CtpInstrumentMetadata(
            instrument_id=instrument_id,
            exchange_id=exchange_id,
            product_class=product_class,
            asset_type=asset_type,
            price_tick=price_tick,
            volume_multiple=volume_multiple,
            lower_limit_price=lower,
            upper_limit_price=upper,
            price_limits_available=limits_available,
            price_limits_source=limits_source,
            account_session_generation=account_session_generation,
            account_fingerprint=_sha256_text(current_query_scope.account_fingerprint),
            trading_day=current_query_scope.trading_day,
            source_connection_generation=current_query_scope.connection_generation,
            source_request_id=result.request_id,
            source_records_sha256=source.records_sha256,
            source_revision=_SOURCE_REVISION,
            source=_SOURCE,
            query_started_at_utc=source_started.isoformat(),
            observed_at_utc=source_completed.isoformat(),
            query_clock_domain_id=source.clock_domain_id,
            query_elapsed_seconds=elapsed,
            quote_stream_generation=initial_quote_identity[1],
            quote_source=initial_quote_identity[0],
            clock_domain_id=initial_quote_identity[2],
            clock_source_session_id=initial_quote_identity[3],
            clock_source=initial_quote_identity[4],
            _seal=_CTP_INSTRUMENT_METADATA_SEAL,
        )

    def _current_trader(self) -> object:
        trader = self._request_feed.trader_client
        if trader is None:
            raise CtpInstrumentMetadataError("TRADER_SESSION_UNREADY")
        return trader

    def _current_quote_scope(self) -> object:
        getter = getattr(self._market_stream, "get_quote_clock_source_scope", None)
        if not callable(getter):
            raise CtpInstrumentMetadataError("QUOTE_SOURCE_UNAVAILABLE")
        try:
            scope = getter()
        except Exception as exc:
            raise CtpInstrumentMetadataError("QUOTE_SOURCE_UNAVAILABLE") from exc
        if scope is None:
            raise CtpInstrumentMetadataError("QUOTE_SOURCE_UNAVAILABLE")
        return scope

    def _validate_query_result(
        self,
        trader: object,
        result: object,
        scope: object,
        instrument_id: str,
        exchange_id: str,
        timeout: float,
    ) -> _QuerySource:
        if type(result) is not QueryResult:
            raise CtpNativeQueryCertificateError("query_result_type_invalid")
        _query_result_shape(result)
        if (
            result.request_type != "instruments"
            or not result.complete
            or not result.is_last_seen
            or result.timed_out
            or result.unsupported
            or result.error_code not in (None, 0)
            or result.error_message
            or result.submit_code not in (None, 0)
            or result.late_callback_count != 0
            or len(result.records) != 1
        ):
            raise CtpNativeQueryCertificateError("query_not_complete_terminal_row")
        now_utc = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        source = _source_for_result(
            result,
            scope,
            now_utc=now_utc,
            now_monotonic=now_monotonic,
        )
        if source.started_monotonic < scope.captured_monotonic:
            raise CtpNativeQueryCertificateError("query_started_before_session_scope")
        if (
            source.completed_monotonic - source.started_monotonic > timeout
            or now_monotonic - source.started_monotonic > timeout
        ):
            raise CtpNativeQueryCertificateError("query_exceeded_timeout")
        expected_filters = (
            ("ExchangeID", exchange_id),
            ("InstrumentID", instrument_id),
        )
        if (
            source.request_intent_filters != expected_filters
            or source.request_filters != expected_filters
            # CTP's instrument query API has no "explicit" filter flag; the
            # source certificate still proves exact intent and native-field
            # readback above, while this tuple stays empty by contract.
            or source.explicit_request_filters != ()
        ):
            raise CtpNativeQueryCertificateError("query_filter_provenance_invalid")
        filters = _validate_bound_filters(
            result,
            scope,
            instrument_id,
            exchange_id,
            "",
        )
        if filters != expected_filters:
            raise CtpNativeQueryCertificateError("query_filter_provenance_invalid")
        target_count, _ = _validate_returned_rows(
            result,
            scope,
            instrument_id,
            exchange_id,
            "",
        )
        if target_count != 1:
            raise CtpNativeQueryCertificateError("query_target_instrument_count_invalid")

        current_getter = getattr(trader, "get_query_result", None)
        if not callable(current_getter):
            raise CtpNativeQueryCertificateError("query_current_result_unavailable")
        try:
            current = current_getter(result.request_id)
        except Exception as exc:
            raise CtpNativeQueryCertificateError("query_current_result_unavailable") from exc
        if type(current) is not QueryResult:
            raise CtpNativeQueryCertificateError("query_current_result_mismatch")
        _query_result_shape(current)
        if (
            current.request_type != "instruments"
            or not current.complete
            or not current.is_last_seen
            or current.timed_out
            or current.unsupported
            or current.error_code not in (None, 0)
            or current.error_message
            or current.submit_code not in (None, 0)
            or current.late_callback_count != 0
            or len(current.records) != 1
        ):
            raise CtpNativeQueryCertificateError("query_current_result_mismatch")
        current_source = current.query_source
        if (
            type(current_source) is not _QuerySource
            or current_source.issuer is not source.issuer
            or current_source.request_type != source.request_type
            or current_source.request_id != source.request_id
            or current_source.records_sha256 != source.records_sha256
            or current_source.request_filters != source.request_filters
            or current_source.request_intent_filters != source.request_intent_filters
            or current_source.explicit_request_filters != source.explicit_request_filters
            or current.request_type != result.request_type
            or current.request_id != result.request_id
            or current.connection_generation != result.connection_generation
            or current.account_fingerprint != result.account_fingerprint
            or current.late_callback_count != 0
        ):
            raise CtpNativeQueryCertificateError("query_current_result_mismatch")
        try:
            current_digest = _query_records_digest(current.records)
        except (TypeError, ValueError) as exc:
            raise CtpNativeQueryCertificateError("query_current_result_mismatch") from exc
        if current_digest != source.records_sha256:
            raise CtpNativeQueryCertificateError("query_current_result_mismatch")
        _source_for_result(
            current,
            scope,
            now_utc=datetime.now(timezone.utc),
            now_monotonic=time.monotonic(),
        )
        return source

    def _check_owner(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise CtpInstrumentMetadataError("OWNER_THREAD_REQUIRED")


def _quote_scope_identity(
    scope: object,
    stream: CtpMarketStream,
    *,
    broker_id: str,
    investor_id: str,
    trading_day: str,
) -> tuple[str, int, str, str, str]:
    if type(scope) is not CtpQuoteClockSourceScope or not _is_current_ctp_quote_clock_source_scope(
        scope, stream, require_authenticated=True
    ):
        raise CtpInstrumentMetadataError("QUOTE_SOURCE_UNTRUSTED")
    source = getattr(scope, "source", None)
    generation = getattr(scope, "connection_generation", None)
    domain = getattr(scope, "clock_domain_id", None)
    session_id = getattr(scope, "clock_source_session_id", None)
    clock_source = getattr(scope, "clock_source", None)
    client = getattr(scope, "_client", None)
    try:
        md_identity = getattr(client, "active_md_identity", None)
    except Exception:
        md_identity = None
    domain_match = _QUOTE_CLOCK_DOMAIN.fullmatch(domain) if type(domain) is str else None
    if (
        source != _MARKET_SOURCE
        or type(generation) is not int
        or generation <= 0
        or any(
            type(value) is not str or not value or value != value.strip()
            for value in (domain, session_id, clock_source)
        )
        or domain_match is None
        or domain_match.group(1) != session_id
        or type(md_identity) is not MdIdentityObservation
        or md_identity.authenticated is not True
        or md_identity.broker_id != broker_id
        or md_identity.user_id != investor_id
        or md_identity.trading_day != trading_day
    ):
        raise CtpInstrumentMetadataError("QUOTE_SOURCE_UNTRUSTED")
    return source, generation, domain, session_id, clock_source


def _is_current_ctp_instrument_metadata(value: object) -> bool:
    """Recognize only typed results issued by this module's live reader."""

    return type(value) is CtpInstrumentMetadata and value._seal is _CTP_INSTRUMENT_METADATA_SEAL


def _require_identifier(value: object, code: str) -> None:
    if type(value) is not str or not value or value != value.strip():
        raise CtpInstrumentMetadataError(code)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CtpInstrumentMetadataError(code) from exc
    if len(encoded) > _MAX_IDENTIFIER_BYTES or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CtpInstrumentMetadataError(code)


def _positive_timeout(value: object) -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        raise CtpInstrumentMetadataError("TIMEOUT_INVALID")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise CtpInstrumentMetadataError("TIMEOUT_INVALID") from None
    if not math.isfinite(result) or result <= 0 or result > 30:
        raise CtpInstrumentMetadataError("TIMEOUT_INVALID")
    return result


def _read_alias(record: object, *names: str) -> tuple[bool, object]:
    for name in names:
        present, value = _record_value(record, name)
        if present:
            return True, value
    return False, None


def _decimal_text(value: object, code: str) -> str:
    if type(value) not in (str, int, float, Decimal) or isinstance(value, bool):
        raise CtpInstrumentMetadataError(code)
    if type(value) is float and not math.isfinite(value):
        raise CtpInstrumentMetadataError(code)
    raw = str(value)
    if len(raw) > 128:
        raise CtpInstrumentMetadataError(code)
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise CtpInstrumentMetadataError(code) from exc
    if not number.is_finite() or number <= 0:
        raise CtpInstrumentMetadataError(code)
    text = format(number.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not text or text == "0":
        raise CtpInstrumentMetadataError(code)
    return text


def _positive_decimal_field(record: object, *names: str) -> str:
    present, value = _read_alias(record, *names)
    if not present:
        raise CtpInstrumentMetadataError("PRICE_TICK_UNAVAILABLE")
    return _decimal_text(value, "PRICE_TICK_INVALID")


def _positive_integer_field(record: object, *names: str) -> int:
    present, value = _read_alias(record, *names)
    if not present or type(value) is not int or value <= 0:
        raise CtpInstrumentMetadataError("VOLUME_MULTIPLE_INVALID")
    return value


def _product_class(record: object) -> str:
    present, value = _read_alias(record, "ProductClass", "product_class")
    if not present:
        raise CtpInstrumentMetadataError("PRODUCT_CLASS_UNAVAILABLE")
    if type(value) is int and value > 0:
        value = str(value)
    if type(value) is not str or value not in {"1", "2", "3", "4", "5", "6", "7", "I"}:
        raise CtpInstrumentMetadataError("PRODUCT_CLASS_INVALID")
    return value


def _price_limits(record: object) -> tuple[str | None, str | None, bool, str]:
    lower_present, lower_value = _read_alias(record, "LowerLimitPrice", "lower_limit_price")
    upper_present, upper_value = _read_alias(record, "UpperLimitPrice", "upper_limit_price")
    if not lower_present and not upper_present:
        return None, None, False, "unavailable_in_instrument_query"
    if not lower_present or not upper_present:
        raise CtpInstrumentMetadataError("PRICE_LIMITS_PARTIAL")
    lower = _decimal_text(lower_value, "PRICE_LIMITS_INVALID")
    upper = _decimal_text(upper_value, "PRICE_LIMITS_INVALID")
    if Decimal(lower) >= Decimal(upper):
        raise CtpInstrumentMetadataError("PRICE_LIMITS_INVALID")
    return lower, upper, True, _SOURCE


__all__ = [
    "CtpInstrumentMetadata",
    "CtpInstrumentMetadataError",
    "CtpInstrumentMetadataReader",
]
