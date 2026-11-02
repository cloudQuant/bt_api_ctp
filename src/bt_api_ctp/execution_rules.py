"""CTP-specific order-rule calculations over provider-neutral SDK DTOs.

This module contains no Backtrader imports and exposes no order reference,
front/session identifier, native query object, or dispatch authority.  The
caller must supply facts already bound by the CTP query/session owner.  The
rules decision itself remains advisory validation; normal claim and write
authorization are separate capabilities.

CTP commission rows expose the native ``ByMoney`` and ``ByVolume`` dimensions
without a currency field.  This module preserves those raw dimensions and will
not create an account-currency estimate until a same-generation currency proof
is supplied.  The calculation, when complete, preserves the existing BT CTP
formula: quantity * (price * multiplier * ByMoney + ByVolume).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Callable

from bt_api_base.execution_rules_protocol import (
    CommissionRuleFacts,
    ExecutionRuleDecision,
    ExecutionRuleRequest,
    InstrumentExecutionFacts,
)
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_base.service_protocol import MAX_REFERENCE_BYTES
from bt_api_ctp.instrument_metadata import CtpInstrumentMetadata

_NATIVE_PRODUCT_CLASSES = frozenset({"2", "6"})
_SOURCE_REF_LENGTH = 64
_TICK_TOLERANCE = Decimal("0.000000001")


class CtpExecutionRulesError(ValueError):
    """Finite error for malformed, mismatched, or unsupported CTP rule facts."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CtpOptionMetadata:
    """Sanitized option identity facts extracted from a source-bound CTP row.

    The caller must pass the records digest from the exact validated instrument
    query.  The digest binds bytes but does not by itself prove the query source.
    """

    instrument_id: str
    exchange_id: str
    product_class: str
    right: str
    strike: str
    expiry: date
    premium_style: str | None
    source_ref: str
    underlying_instrument_id: str | None = None
    underlying_multiple: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "instrument_id",
            "exchange_id",
            "product_class",
            "right",
            "strike",
            "source_ref",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty trimmed text")
        if self.product_class not in _NATIVE_PRODUCT_CLASSES:
            raise ValueError("product_class must identify a CTP option")
        if self.right not in {"call", "put"}:
            raise ValueError("right must be call or put")
        strike = _decimal(self.strike, positive=True)
        object.__setattr__(self, "strike", _decimal_text(strike))
        if type(self.expiry) is not date:
            raise TypeError("expiry must be a datetime.date")
        if self.premium_style is not None:
            if (
                type(self.premium_style) is not str
                or self.premium_style != self.premium_style.strip()
            ):
                raise ValueError("premium_style must be trimmed text or None")
        if self.underlying_instrument_id is not None:
            underlying_id = self.underlying_instrument_id
            if (
                type(underlying_id) is not str
                or not underlying_id
                or underlying_id != underlying_id.strip()
                or not underlying_id.isprintable()
                or len(underlying_id.encode("utf-8", errors="strict")) > MAX_REFERENCE_BYTES
            ):
                raise ValueError("underlying_instrument_id must be bounded printable text or None")
        if self.underlying_multiple is not None:
            underlying_multiple = _decimal(self.underlying_multiple, positive=True)
            object.__setattr__(
                self,
                "underlying_multiple",
                _decimal_text(underlying_multiple),
            )
        if len(self.source_ref) != _SOURCE_REF_LENGTH or any(
            char not in "0123456789abcdef" for char in self.source_ref
        ):
            raise ValueError("source_ref must be a lowercase SHA-256 digest")


def _record_value(record: object, key: str) -> object:
    if isinstance(record, Mapping):
        return record.get(key)
    try:
        return getattr(record, key, None)
    except Exception as exc:
        raise CtpExecutionRulesError("NATIVE_ROW_INVALID") from exc


def _decimal(value: object, *, positive: bool = False, signed: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CtpExecutionRulesError("DECIMAL_INVALID")
    if type(value) not in (str, int, float, Decimal):
        raise CtpExecutionRulesError("DECIMAL_INVALID")
    try:
        parsed = value if type(value) is Decimal else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CtpExecutionRulesError("DECIMAL_INVALID") from exc
    if (
        not parsed.is_finite()
        or (positive and parsed <= 0)
        or (not positive and not signed and parsed < 0)
    ):
        raise CtpExecutionRulesError("DECIMAL_INVALID")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if len(text) > 128:
        raise CtpExecutionRulesError("DECIMAL_INVALID")
    return text


def _read_optional_decimal(record: object, key: str) -> str | None:
    raw = _record_value(record, key)
    if raw in (None, ""):
        return None
    return _decimal_text(_decimal(raw))


def _read_optional_native_text(record: object, key: str) -> str | None:
    try:
        raw = _record_value(record, key)
        if raw is None or raw == "" or raw == b"":
            return None
        value = _native_text(raw)
        if (
            not value
            or not value.isprintable()
            or len(value.encode("utf-8", errors="strict")) > MAX_REFERENCE_BYTES
        ):
            return None
        return value
    except Exception:
        return None


def _read_optional_positive_decimal(record: object, key: str) -> str | None:
    try:
        raw = _record_value(record, key)
        if raw is None or raw == "" or raw == b"":
            return None
        return _decimal_text(_decimal(raw, positive=True))
    except Exception:
        return None


def _require_source_ref(value: str) -> str:
    if (
        type(value) is not str
        or len(value) != _SOURCE_REF_LENGTH
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise CtpExecutionRulesError("SOURCE_REF_INVALID")
    return value


def ctp_option_metadata_from_native_row(
    row: object,
    instrument: MarketInstrumentKey,
    *,
    source_ref: str,
    premium_style: str | None = None,
) -> CtpOptionMetadata:
    """Project native CTP option identity fields without inferring style.

    The row must already have passed the caller's exact ``QueryResult`` source,
    session, completeness, digest, and instrument-scope checks.  This pure
    parser only verifies the native row's instrument fields and field shapes.
    """

    _validate_ctp_instrument_key(instrument)
    source_ref = _require_source_ref(source_ref)
    instrument_id = _native_text(_record_value(row, "InstrumentID"))
    exchange_id = _native_text(_record_value(row, "ExchangeID"))
    product_class = _native_text(_record_value(row, "ProductClass"))
    if instrument_id != instrument.native_symbol or exchange_id != instrument.venue:
        raise CtpExecutionRulesError("OPTION_INSTRUMENT_SCOPE_MISMATCH")
    if product_class not in _NATIVE_PRODUCT_CLASSES:
        raise CtpExecutionRulesError("OPTION_PRODUCT_CLASS_UNSUPPORTED")
    option_right = _native_text(_record_value(row, "OptionsType")).upper()
    right = {"1": "call", "C": "call", "CALL": "call", "2": "put", "P": "put", "PUT": "put"}.get(
        option_right
    )
    if right is None:
        raise CtpExecutionRulesError("OPTION_RIGHT_UNKNOWN")
    strike = _decimal_text(_decimal(_record_value(row, "StrikePrice"), positive=True))
    raw_expiry = _native_text(_record_value(row, "ExpireDate"))
    if len(raw_expiry) != 8 or not raw_expiry.isascii() or not raw_expiry.isdigit():
        raise CtpExecutionRulesError("OPTION_EXPIRY_INVALID")
    try:
        expiry = date(int(raw_expiry[:4]), int(raw_expiry[4:6]), int(raw_expiry[6:8]))
    except ValueError as exc:
        raise CtpExecutionRulesError("OPTION_EXPIRY_INVALID") from exc
    style = premium_style
    if style is not None:
        style = style.strip().lower().replace("-", "_")
        if style not in {"premium", "premium_style"}:
            raise CtpExecutionRulesError("OPTION_PREMIUM_STYLE_UNSUPPORTED")
        style = "premium"
    return CtpOptionMetadata(
        instrument_id=instrument_id,
        exchange_id=exchange_id,
        product_class=product_class,
        right=right,
        strike=strike,
        expiry=expiry,
        premium_style=style,
        source_ref=source_ref,
        underlying_instrument_id=_read_optional_native_text(row, "UnderlyingInstrID"),
        underlying_multiple=_read_optional_positive_decimal(row, "UnderlyingMultiple"),
    )


def ctp_commission_rules_from_native_row(
    row: object,
    instrument: MarketInstrumentKey,
    *,
    session_generation: str,
    source_ref: str,
    currency: str | None = None,
    currency_source_ref: str | None = None,
    currency_session_generation: str | None = None,
) -> tuple[CommissionRuleFacts, ...]:
    """Preserve CTP native ByMoney/ByVolume pairs for open/close roles.

    Both standard futures and option commission rows use these field names.
    Neither native row family includes a fee currency.  Currency is therefore
    unavailable unless separately provided from a same-session, source-bound
    currency fact.  CTP has no native CloseYesterday commission fields, so the
    evaluator falls back to the explicit Close pair for that role, matching the
    prior Backtrader option accounting contract.
    """

    _validate_ctp_instrument_key(instrument)
    _validate_session_generation(session_generation)
    source_ref = _require_source_ref(source_ref)
    row_instrument = _native_text(_record_value(row, "InstrumentID"))
    row_exchange = _native_text(_record_value(row, "ExchangeID"))
    if row_instrument != instrument.native_symbol or row_exchange != instrument.venue:
        raise CtpExecutionRulesError("COMMISSION_INSTRUMENT_SCOPE_MISMATCH")

    currency_is_verified = (
        currency is not None
        and currency_source_ref is not None
        and currency_session_generation == session_generation
    )
    if currency_is_verified:
        currency_source_ref = _require_source_ref(currency_source_ref)
        currency = _native_currency(currency)
    else:
        currency = None
        currency_source_ref = None
        currency_session_generation = None

    output: list[CommissionRuleFacts] = []
    for role, suffix in (("open", "Open"), ("close", "Close"), ("close_today", "CloseToday")):
        by_money = _read_optional_decimal(row, f"{suffix}RatioByMoney")
        by_volume = _read_optional_decimal(row, f"{suffix}RatioByVolume")
        pair_verified = by_money is not None and by_volume is not None and currency_is_verified
        output.append(
            CommissionRuleFacts(
                role=role,
                by_money_rate=by_money,
                by_volume_amount=by_volume,
                currency=currency if pair_verified else None,
                currency_status="verified" if pair_verified else "unavailable",
                source_ref=source_ref,
                currency_source_ref=currency_source_ref if pair_verified else None,
                session_generation=session_generation,
                currency_session_generation=(
                    currency_session_generation if pair_verified else None
                ),
            )
        )
    return tuple(output)


def instrument_facts_from_ctp_metadata(
    metadata: CtpInstrumentMetadata,
    instrument: MarketInstrumentKey,
    *,
    expires_at_utc: datetime,
    option: CtpOptionMetadata | None = None,
    commission_rules: tuple[CommissionRuleFacts, ...] = (),
    price_currency: str | None = None,
    price_currency_source_ref: str | None = None,
    price_currency_session_generation: str | None = None,
) -> InstrumentExecutionFacts:
    """Convert an issuer-produced CTP instrument observation to neutral facts.

    ``expires_at_utc`` must be no later than the earliest expiry of all
    instrument, commission, option, and currency source evidence; this function
    does not extend an issuer's lifetime. Instrument and fee query digests are
    opaque references, never native IDs or account identifiers.
    """

    if type(metadata) is not CtpInstrumentMetadata:
        raise TypeError("metadata must be CtpInstrumentMetadata")
    _validate_ctp_instrument_key(instrument)
    if (
        instrument.native_symbol != metadata.instrument_id
        or instrument.venue != metadata.exchange_id
    ):
        raise CtpExecutionRulesError("INSTRUMENT_METADATA_SCOPE_MISMATCH")
    generation = metadata.account_session_generation
    _validate_session_generation(generation)
    source_ref = _require_source_ref(metadata.source_records_sha256)
    observed = _parse_utc_timestamp(metadata.observed_at_utc)
    if type(expires_at_utc) is not datetime or expires_at_utc.tzinfo is None:
        raise CtpExecutionRulesError("RULE_EXPIRY_INVALID")
    if (
        expires_at_utc.utcoffset() != timezone.utc.utcoffset(expires_at_utc)
        or expires_at_utc <= observed
    ):
        raise CtpExecutionRulesError("RULE_EXPIRY_INVALID")

    asset_kind = (
        "option"
        if metadata.asset_type in {"option", "spot_option"}
        else "future"
        if metadata.asset_type == "future"
        else "unknown"
    )
    if asset_kind == "unknown":
        return _unavailable_facts(
            instrument,
            generation,
            "CTP_ASSET_KIND_UNSUPPORTED",
            source_ref=source_ref,
            observed=observed,
            expires=expires_at_utc,
        )

    option_fields: dict[str, object] = {}
    if asset_kind == "option":
        if (
            type(option) is not CtpOptionMetadata
            or option.instrument_id != metadata.instrument_id
            or option.exchange_id != metadata.exchange_id
            or option.source_ref != source_ref
            or option.premium_style != "premium"
        ):
            return _unavailable_facts(
                instrument,
                generation,
                "CTP_OPTION_FACTS_UNAVAILABLE",
                source_ref=source_ref,
                observed=observed,
                expires=expires_at_utc,
            )
        option_fields = {
            "option_right": option.right,
            "option_strike": option.strike,
            "option_expiry": option.expiry,
            "option_premium_style": option.premium_style,
        }

    if type(commission_rules) is not tuple or any(
        type(rule) is not CommissionRuleFacts for rule in commission_rules
    ):
        raise TypeError("commission_rules must be a tuple of CommissionRuleFacts")
    for fee in commission_rules:
        if fee.session_generation != generation:
            raise CtpExecutionRulesError("COMMISSION_SESSION_MISMATCH")
        if fee.currency_status == "verified" and fee.currency_session_generation != generation:
            raise CtpExecutionRulesError("COMMISSION_CURRENCY_SESSION_MISMATCH")

    currency_is_verified = (
        price_currency is not None
        and price_currency_source_ref is not None
        and price_currency_session_generation == generation
    )
    if currency_is_verified:
        price_currency = _native_currency(price_currency)
        price_currency_source_ref = _require_source_ref(price_currency_source_ref)
    else:
        price_currency = None
        price_currency_source_ref = None
        price_currency_session_generation = None

    return InstrumentExecutionFacts(
        instrument=instrument,
        status="available",
        reason_code=None,
        session_generation=generation,
        source_ref=source_ref,
        observed_at_utc=observed,
        expires_at_utc=expires_at_utc,
        asset_kind=asset_kind,
        price_tick=_native_decimal_text(metadata.price_tick, positive=True),
        contract_multiplier=_decimal_text(_decimal(metadata.volume_multiple, positive=True)),
        price_currency=price_currency,
        price_currency_status="verified" if currency_is_verified else "unavailable",
        price_currency_source_ref=(price_currency_source_ref if currency_is_verified else None),
        price_currency_session_generation=(
            price_currency_session_generation if currency_is_verified else None
        ),
        commission_rules=commission_rules,
        **option_fields,
    )


def _unavailable_facts(
    instrument: MarketInstrumentKey,
    generation: str,
    reason: str,
    *,
    source_ref: str | None,
    observed: datetime | None,
    expires: datetime | None,
) -> InstrumentExecutionFacts:
    return InstrumentExecutionFacts(
        instrument=instrument,
        status="unavailable",
        reason_code=reason,
        session_generation=generation,
        source_ref=source_ref,
        observed_at_utc=observed,
        expires_at_utc=expires,
        asset_kind="unknown",
        price_tick=None,
        contract_multiplier=None,
        price_currency=None,
        price_currency_status="unavailable",
    )


def _validate_ctp_instrument_key(instrument: MarketInstrumentKey) -> None:
    if type(instrument) is not MarketInstrumentKey:
        raise TypeError("instrument must be a MarketInstrumentKey")
    if instrument.provider.lower() != "ctp":
        raise CtpExecutionRulesError("PROVIDER_MISMATCH")


def _validate_session_generation(value: str) -> None:
    if type(value) is not str or not value or value != value.strip() or not value.isprintable():
        raise CtpExecutionRulesError("SESSION_GENERATION_UNAVAILABLE")
    if len(value.encode("utf-8", errors="strict")) > 256:
        raise CtpExecutionRulesError("SESSION_GENERATION_INVALID")


def _native_text(value: object) -> str:
    if type(value) is str:
        return value.strip()
    if type(value) is bytes:
        try:
            return value.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise CtpExecutionRulesError("NATIVE_TEXT_INVALID") from exc
    if value is None:
        raise CtpExecutionRulesError("NATIVE_TEXT_MISSING")
    raise CtpExecutionRulesError("NATIVE_TEXT_INVALID")


def _native_currency(value: str) -> str:
    if type(value) is not str:
        raise CtpExecutionRulesError("CURRENCY_INVALID")
    currency = value.strip().upper()
    if (
        not currency
        or len(currency) > 12
        or not currency.isascii()
        or not currency.replace("_", "").isalnum()
    ):
        raise CtpExecutionRulesError("CURRENCY_INVALID")
    return currency


def _native_decimal_text(value: object, *, positive: bool = False) -> str:
    return _decimal_text(_decimal(value, positive=positive))


def _parse_utc_timestamp(value: object) -> datetime:
    if type(value) is not str or not value:
        raise CtpExecutionRulesError("OBSERVATION_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CtpExecutionRulesError("OBSERVATION_TIME_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise CtpExecutionRulesError("OBSERVATION_TIME_INVALID")
    return parsed


def evaluate_ctp_order_rules(
    request: ExecutionRuleRequest,
    facts: InstrumentExecutionFacts | None,
    *,
    now_utc: datetime | None = None,
    position_evidence_validated: bool = False,
) -> ExecutionRuleDecision:
    """Evaluate a CTP order against exact-session facts; never dispatch it.

    ``position_evidence_validated`` may be true only after the CTP authority
    has matched the request's position quantities, source reference, generation
    and freshness to its own issuer-bound position receipt. Local Broker
    quantities by themselves are insufficient for a close decision.
    """

    if type(request) is not ExecutionRuleRequest:
        raise TypeError("request must be an ExecutionRuleRequest")
    if type(position_evidence_validated) is not bool:
        raise TypeError("position_evidence_validated must be an exact bool")
    if facts is None:
        return _decision(request, "unavailable", "RULE_FACTS_UNAVAILABLE", None)
    if type(facts) is not InstrumentExecutionFacts:
        raise TypeError("facts must be InstrumentExecutionFacts or None")
    if facts.status != "available":
        return _decision(
            request,
            "unavailable",
            facts.reason_code or "RULE_FACTS_UNAVAILABLE",
            facts,
        )
    if request.session_generation is None:
        return _decision(request, "unavailable", "SESSION_GENERATION_UNAVAILABLE", facts)
    if facts.instrument != request.instrument:
        return _decision(request, "unavailable", "INSTRUMENT_SCOPE_MISMATCH", facts)
    if facts.session_generation != request.session_generation:
        return _decision(request, "unavailable", "SESSION_GENERATION_MISMATCH", facts)
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if (
        type(now) is not datetime
        or now.tzinfo is None
        or now.utcoffset() != timezone.utc.utcoffset(now)
    ):
        raise ValueError("now_utc must be timezone-aware UTC")
    if facts.expires_at_utc is None or now >= facts.expires_at_utc:
        return _decision(request, "unavailable", "RULE_FACTS_EXPIRED", facts)
    if facts.observed_at_utc is None or now < facts.observed_at_utc:
        return _decision(request, "unavailable", "RULE_FACTS_FROM_FUTURE", facts)

    side = request.side.strip().lower()
    if side not in {"buy", "sell"}:
        return _decision(request, "rejected", "ORDER_SIDE_UNSUPPORTED", facts)
    if request.order_type.strip().lower() != "limit":
        return _decision(request, "rejected", "CTP_LIMIT_ONLY", facts)
    tif = (request.time_in_force or "GFD").strip().upper().replace("-", "_")
    if tif not in {"GFD", "GOOD_FOR_DAY"}:
        return _decision(request, "rejected", "CTP_GFD_ONLY", facts)
    if request.limit_price is None:
        return _decision(request, "rejected", "LIMIT_PRICE_REQUIRED", facts)
    if facts.price_tick is None or facts.contract_multiplier is None:
        return _decision(request, "unavailable", "PRICE_OR_MULTIPLIER_UNAVAILABLE", facts)
    price = _decimal(request.limit_price, positive=True)
    tick = _decimal(facts.price_tick, positive=True)
    multiplier = _decimal(facts.contract_multiplier, positive=True)
    quantity = _decimal(request.quantity, positive=True)
    if quantity != quantity.to_integral_value():
        return _decision(request, "rejected", "CTP_CONTRACT_QUANTITY_MUST_BE_INTEGER", facts)
    if facts.price_currency_status != "verified" or facts.price_currency is None:
        return _decision(request, "unavailable", "PRICE_CURRENCY_UNVERIFIED", facts)
    if not _price_aligns_to_tick(price, tick):
        return _decision(request, "rejected", "INVALID_PRICE_TICK", facts)

    if facts.asset_kind == "option":
        if (
            facts.option_right not in {"call", "put"}
            or facts.option_strike is None
            or facts.option_expiry is None
            or facts.option_premium_style != "premium"
        ):
            return _decision(request, "unavailable", "OPTION_METADATA_INCOMPLETE", facts)

    position_status, position_code, position_side, offset = _position_action(
        request, side, quantity
    )
    if position_status != "accepted":
        return _decision(request, position_status, position_code, facts)
    if _request_needs_position_evidence(request):
        if (
            request.position_evidence_ref is None
            or request.position_evidence_generation != request.session_generation
            or request.position_observed_at_utc is None
            or request.position_expires_at_utc is None
        ):
            return _decision(request, "unavailable", "POSITION_EVIDENCE_UNAVAILABLE", facts)
        if (
            now < request.position_observed_at_utc
            or now >= request.position_expires_at_utc
        ):
            return _decision(request, "unavailable", "POSITION_EVIDENCE_STALE", facts)
        if not position_evidence_validated:
            return _decision(request, "unavailable", "POSITION_EVIDENCE_UNVERIFIED", facts)
    fee_rule = _fee_rule_for(facts.commission_rules, offset)
    if fee_rule is None:
        return _decision(request, "unavailable", "COMMISSION_RULE_UNAVAILABLE", facts)
    if (
        fee_rule.currency_status != "verified"
        or fee_rule.currency is None
        or fee_rule.by_money_rate is None
        or fee_rule.by_volume_amount is None
        or fee_rule.source_ref is None
        or fee_rule.session_generation != facts.session_generation
        or fee_rule.currency_session_generation != facts.session_generation
    ):
        return _decision(request, "unavailable", "COMMISSION_CURRENCY_OR_UNIT_UNVERIFIED", facts)
    if fee_rule.currency != facts.price_currency:
        return _decision(request, "unavailable", "COMMISSION_PRICE_CURRENCY_MISMATCH", facts)
    rate = _decimal(fee_rule.by_money_rate)
    amount = _decimal(fee_rule.by_volume_amount)
    try:
        with localcontext() as context:
            context.prec = 700
            commission = quantity * (price * multiplier * rate + amount)
    except (InvalidOperation, OverflowError):
        return _decision(request, "unavailable", "COMMISSION_ESTIMATE_INVALID", facts)
    if not commission.is_finite() or commission < 0:
        return _decision(request, "unavailable", "COMMISSION_ESTIMATE_INVALID", facts)
    try:
        estimate = _decimal_text(commission)
    except CtpExecutionRulesError:
        return _decision(request, "unavailable", "COMMISSION_ESTIMATE_INVALID", facts)

    return ExecutionRuleDecision(
        request=request,
        status="accepted",
        reason_code=None,
        facts=facts,
        normalized_position_side=position_side,
        normalized_offset=offset,
        normalized_time_in_force="GFD",
        commission_estimate=estimate,
        commission_currency=fee_rule.currency,
        commission_charge_unit="currency_amount",
        position_evidence_validated=position_evidence_validated,
    )


def _position_action(
    request: ExecutionRuleRequest,
    side: str,
    quantity: Decimal,
) -> tuple[str, str, str | None, str | None]:
    if request.position_mode == "dual_side":
        position_side = request.position_side
        offset = request.offset
        if position_side is None or offset is None:
            return "rejected", "DUAL_SIDE_METADATA_REQUIRED", None, None
        allowed = {
            ("buy", "long", "open"),
            ("sell", "long", "close"),
            ("sell", "short", "open"),
            ("buy", "short", "close"),
            ("sell", "long", "close_today"),
            ("sell", "long", "close_yesterday"),
            ("buy", "short", "close_today"),
            ("buy", "short", "close_yesterday"),
        }
        if (side, position_side, offset) not in allowed:
            return "rejected", "DUAL_SIDE_ACTION_INVALID", None, None
        if offset != "open":
            available_text = (
                request.long_position_size
                if position_side == "long"
                else request.short_position_size
            )
            if available_text is None:
                return "unavailable", "POSITION_SIZE_UNAVAILABLE", None, None
            if quantity > _decimal(available_text):
                return "rejected", "CLOSE_SIZE_EXCEEDS_POSITION", None, None
            if offset in {"close_today", "close_yesterday"}:
                bucket_field = (
                    f"{position_side}_{offset}_size"
                )
                bucket_text = getattr(request, bucket_field)
                if bucket_text is None:
                    return "unavailable", "POSITION_BUCKET_UNAVAILABLE", None, None
                if quantity > _decimal(bucket_text):
                    return "rejected", "CLOSE_SIZE_EXCEEDS_POSITION_BUCKET", None, None
        return "accepted", "", position_side, offset

    if request.position_mode != "net":
        return "rejected", "POSITION_MODE_UNSUPPORTED", None, None
    offset = request.offset
    net: Decimal | None = None
    if offset is None:
        if request.net_position_size is None:
            return "unavailable", "NET_POSITION_UNAVAILABLE", None, None
        net = _decimal(request.net_position_size, signed=True)
        if side == "buy" and net < 0:
            if quantity > abs(net):
                return "rejected", "NET_REVERSAL_REQUIRES_SPLIT", None, None
            offset = "close"
        elif side == "sell" and net > 0:
            if quantity > net:
                return "rejected", "NET_REVERSAL_REQUIRES_SPLIT", None, None
            offset = "close"
        else:
            offset = "open"
    position_side = ("long" if side == "buy" else "short") if offset == "open" else (
        "short" if side == "buy" else "long"
    )
    if request.position_side is not None and request.position_side != position_side:
        return "rejected", "NET_POSITION_SIDE_CONFLICT", None, None
    if offset != "open":
        if net is None:
            if request.net_position_size is None:
                return "unavailable", "NET_POSITION_UNAVAILABLE", None, None
            net = _decimal(request.net_position_size, signed=True)
        available = net if position_side == "long" else abs(net) if net < 0 else Decimal(0)
        if position_side == "long" and net < 0:
            available = Decimal(0)
        if position_side == "short" and net >= 0:
            available = Decimal(0)
        if quantity > available:
            return "rejected", "CLOSE_SIZE_EXCEEDS_POSITION", None, None
        if offset in {"close_today", "close_yesterday"}:
            bucket_field = f"{position_side}_{offset}_size"
            bucket_text = getattr(request, bucket_field)
            if bucket_text is None:
                return "unavailable", "POSITION_BUCKET_UNAVAILABLE", None, None
            if quantity > _decimal(bucket_text):
                return "rejected", "CLOSE_SIZE_EXCEEDS_POSITION_BUCKET", None, None
    return "accepted", "", position_side, offset


def _fee_rule_for(
    rules: tuple[CommissionRuleFacts, ...], offset: str | None
) -> CommissionRuleFacts | None:
    role = offset if offset in {"open", "close", "close_today", "close_yesterday"} else None
    if role is None:
        return None
    by_role = {item.role: item for item in rules}
    if role == "close_yesterday" and role not in by_role:
        return by_role.get("close")
    return by_role.get(role)


def _request_needs_position_evidence(request: ExecutionRuleRequest) -> bool:
    if request.position_mode == "net" and request.offset is None:
        return True
    return request.offset is not None and request.offset != "open"


def _price_aligns_to_tick(price: Decimal, tick: Decimal) -> bool:
    try:
        with localcontext() as context:
            context.prec = 300
            scaled = price / tick
            nearest = scaled.to_integral_value(rounding=ROUND_HALF_EVEN)
            return abs(scaled - nearest) <= _TICK_TOLERANCE
    except (InvalidOperation, OverflowError):
        return False


def _decision(
    request: ExecutionRuleRequest,
    status: str,
    reason_code: str,
    facts: InstrumentExecutionFacts | None,
    *,
    position_side: str | None = None,
    offset: str | None = None,
    tif: str | None = None,
    position_evidence_validated: bool = False,
) -> ExecutionRuleDecision:
    return ExecutionRuleDecision(
        request=request,
        status=status,
        reason_code=reason_code,
        facts=facts,
        normalized_position_side=position_side,
        normalized_offset=offset,
        normalized_time_in_force=tif,
        commission_estimate=None,
        commission_currency=None,
        commission_charge_unit=None,
        position_evidence_validated=position_evidence_validated,
    )


class CtpExecutionRulesEvaluator:
    """Adapter implementing the neutral port from an injected facts resolver.

    The resolver belongs to the CTP session/query owner and must return facts
    bound to the exact live session generation.  This class deliberately does
    not access native clients, perform queries, authorize writes, or dispatch.
    """

    def __init__(
        self,
        facts_resolver: Callable[[MarketInstrumentKey, str], InstrumentExecutionFacts | None],
        position_evidence_validator: Callable[[ExecutionRuleRequest], bool] | None = None,
    ) -> None:
        if not callable(facts_resolver):
            raise TypeError("facts_resolver must be callable")
        if position_evidence_validator is not None and not callable(position_evidence_validator):
            raise TypeError("position_evidence_validator must be callable or None")
        self._facts_resolver = facts_resolver
        self._position_evidence_validator = position_evidence_validator

    def evaluate_order_rules(self, request: ExecutionRuleRequest) -> ExecutionRuleDecision:
        if type(request) is not ExecutionRuleRequest:
            raise TypeError("request must be an ExecutionRuleRequest")
        if request.session_generation is None:
            return _decision(request, "unavailable", "SESSION_GENERATION_UNAVAILABLE", None)
        try:
            facts = self._facts_resolver(request.instrument, request.session_generation)
        except Exception:
            return _decision(request, "unavailable", "RULE_FACTS_UNAVAILABLE", None)
        position_evidence_validated = False
        if _request_needs_position_evidence(request):
            if self._position_evidence_validator is None:
                return _decision(
                    request,
                    "unavailable",
                    "POSITION_EVIDENCE_VALIDATOR_UNAVAILABLE",
                    facts,
                )
            try:
                position_evidence_validated = self._position_evidence_validator(request)
            except Exception:
                return _decision(
                    request,
                    "unavailable",
                    "POSITION_EVIDENCE_VALIDATOR_UNAVAILABLE",
                    facts,
                )
            if type(position_evidence_validated) is not bool:
                return _decision(
                    request,
                    "unavailable",
                    "POSITION_EVIDENCE_VALIDATOR_INVALID",
                    facts,
                )
        return evaluate_ctp_order_rules(
            request,
            facts,
            position_evidence_validated=position_evidence_validated,
        )


__all__ = [
    "CtpExecutionRulesError",
    "CtpExecutionRulesEvaluator",
    "CtpOptionMetadata",
    "ctp_commission_rules_from_native_row",
    "ctp_option_metadata_from_native_row",
    "evaluate_ctp_order_rules",
    "instrument_facts_from_ctp_metadata",
]
