"""CTP service instrument-metadata schema and source-specific parser."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


_INSTRUMENT_METADATA_SCHEMA_ID = "ctp.instrument_metadata"


_INSTRUMENT_METADATA_SCHEMA_VERSION = 1


_INSTRUMENT_METADATA_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "instrument_id",
        "exchange_id",
        "product_class",
        "asset_type",
        "price_tick",
        "volume_multiple",
        "lower_limit_price",
        "upper_limit_price",
        "price_limits_available",
        "price_limits_source",
        "account_session_generation",
        "account_fingerprint",
        "trading_day",
        "source_connection_generation",
        "source_request_id",
        "source_records_sha256",
        "source_revision",
        "source",
        "query_started_at_utc",
        "observed_at_utc",
        "query_clock_domain_id",
        "query_elapsed_seconds",
        "quote_stream_generation",
        "quote_source",
        "clock_domain_id",
        "clock_source_session_id",
        "clock_source",
    }
)


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ServiceAccountInstrumentMetadata:
    """One exact, current CTP instrument metadata row from the service host.

    This DTO is read-only evidence for strategy admission. It is not an
    account snapshot and does not establish price-limit availability when the
    native instrument query did not supply limits.
    """

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


def build_service_instrument_metadata_parser(*, error_type):
    """Bind the SDK account client's finite error type without importing the SDK."""
    ServiceAccountError = error_type

    def _parse_instrument_metadata_reply(
        reply: object,
        *,
        instrument_id: str,
        exchange_id: str,
        account_session_generation: str,
        max_elapsed_seconds: float,
    ) -> ServiceAccountInstrumentMetadata:
        if not isinstance(reply, Mapping) or set(reply) != {"metadata"}:
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA_REPLY")
        value = reply.get("metadata")
        if not isinstance(value, Mapping) or set(value) != _INSTRUMENT_METADATA_FIELDS:
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA_REPLY")
        if (
            value.get("schema_id") != _INSTRUMENT_METADATA_SCHEMA_ID
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != _INSTRUMENT_METADATA_SCHEMA_VERSION
        ):
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA_SCHEMA")

        product_class = value.get("product_class")
        asset_type = value.get("asset_type")
        product_to_asset = {
            "1": "future",
            "2": "option",
            "3": "combination",
            "4": "spot",
            "5": "efp",
            "6": "spot_option",
            "7": "tas",
            "I": "mi",
        }
        if (
            type(value.get("instrument_id")) is not str
            or value["instrument_id"] != instrument_id
            or type(value.get("exchange_id")) is not str
            or value["exchange_id"] != exchange_id
            or type(product_class) is not str
            or product_class not in product_to_asset
            or asset_type != product_to_asset[product_class]
        ):
            raise ServiceAccountError("INSTRUMENT_METADATA_IDENTITY_MISMATCH")

        price_tick = value.get("price_tick")
        _validate_positive_decimal_text(price_tick, "INVALID_INSTRUMENT_METADATA")
        volume_multiple = value.get("volume_multiple")
        if type(volume_multiple) is not int or volume_multiple <= 0:
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")

        limits_available = value.get("price_limits_available")
        lower = value.get("lower_limit_price")
        upper = value.get("upper_limit_price")
        limits_source = value.get("price_limits_source")
        if type(limits_available) is not bool or type(limits_source) is not str:
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")
        if limits_available:
            if limits_source != "ctp.native.instruments":
                raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")
            _validate_positive_decimal_text(lower, "INVALID_INSTRUMENT_METADATA")
            _validate_positive_decimal_text(upper, "INVALID_INSTRUMENT_METADATA")
            if Decimal(lower) >= Decimal(upper):
                raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")
        elif (
            lower is not None
            or upper is not None
            or limits_source != "unavailable_in_instrument_query"
        ):
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")

        generation = value.get("account_session_generation")
        account_fingerprint = value.get("account_fingerprint")
        trading_day = value.get("trading_day")
        if (
            type(trading_day) is str
            and len(trading_day) == 8
            and trading_day.isascii()
            and trading_day.isdigit()
        ):
            try:
                valid_trading_day = (
                    datetime.strptime(trading_day, "%Y%m%d").strftime("%Y%m%d")
                    == trading_day
                )
            except ValueError:
                valid_trading_day = False
        else:
            valid_trading_day = False
        if (
            type(generation) is not str
            or generation != account_session_generation
            or type(account_fingerprint) is not str
            or not _SHA256_PATTERN.fullmatch(account_fingerprint)
            or not valid_trading_day
        ):
            raise ServiceAccountError("ACCOUNT_GENERATION_MISMATCH")

        source_connection_generation = value.get("source_connection_generation")
        source_request_id = value.get("source_request_id")
        quote_stream_generation = value.get("quote_stream_generation")
        if any(
            type(item) is not int or item <= 0
            for item in (source_connection_generation, source_request_id, quote_stream_generation)
        ):
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")
        source_digest = value.get("source_records_sha256")
        if type(source_digest) is not str or not _SHA256_PATTERN.fullmatch(source_digest):
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")
        if (
            value.get("source_revision") != "ctp.native.instrument-query.v1"
            or value.get("source") != "ctp.native.instruments"
        ):
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")

        query_started = _parse_utc_timestamp(value.get("query_started_at_utc"))
        observed = _parse_utc_timestamp(value.get("observed_at_utc"))
        if observed < query_started:
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")
        elapsed = value.get("query_elapsed_seconds")
        if (
            type(elapsed) not in (int, float)
            or isinstance(elapsed, bool)
            or not math.isfinite(elapsed)
            or elapsed < 0
            or elapsed > max_elapsed_seconds
        ):
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")
        query_domain = value.get("query_clock_domain_id")
        quote_source = value.get("quote_source")
        clock_domain = value.get("clock_domain_id")
        clock_session = value.get("clock_source_session_id")
        clock_source = value.get("clock_source")
        domain_match = (
            re.fullmatch(r"ctp-md-process:[1-9][0-9]*:monotonic:([0-9a-f]{32})", clock_domain)
            if type(clock_domain) is str
            else None
        )
        if any(
            type(item) is not str or not item or item != item.strip()
            for item in (query_domain, quote_source, clock_domain, clock_session, clock_source)
        ) or (
            len(clock_source) > 128
            or query_domain != "python.time.monotonic"
            or quote_source != "ctp.native.md"
            or domain_match is None
            or domain_match.group(1) != clock_session
        ):
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")

        return ServiceAccountInstrumentMetadata(
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
            account_session_generation=generation,
            account_fingerprint=account_fingerprint,
            trading_day=trading_day,
            source_connection_generation=source_connection_generation,
            source_request_id=source_request_id,
            source_records_sha256=source_digest,
            source_revision="ctp.native.instrument-query.v1",
            source="ctp.native.instruments",
            query_started_at_utc=value["query_started_at_utc"],
            observed_at_utc=value["observed_at_utc"],
            query_clock_domain_id=query_domain,
            query_elapsed_seconds=float(elapsed),
            quote_stream_generation=quote_stream_generation,
            quote_source=quote_source,
            clock_domain_id=clock_domain,
            clock_source_session_id=clock_session,
            clock_source=value["clock_source"],
        )


    def _validate_positive_decimal_text(value: object, code: str) -> None:
        if type(value) is not str:
            raise ServiceAccountError(code)
        try:
            decimal_value = Decimal(value)
        except (InvalidOperation, ValueError):
            raise ServiceAccountError(code) from None
        if not decimal_value.is_finite() or decimal_value <= 0:
            raise ServiceAccountError(code)


    def _parse_utc_timestamp(value: object) -> datetime:
        if type(value) is not str:
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA") from None
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ServiceAccountError("INVALID_INSTRUMENT_METADATA")
        return parsed

    _parse_instrument_metadata_reply.metadata_type = ServiceAccountInstrumentMetadata
    return _parse_instrument_metadata_reply


__all__ = ("ServiceAccountInstrumentMetadata", "build_service_instrument_metadata_parser")
