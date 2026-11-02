"""Source issuer for CTP instrument execution-rule snapshots.

The issuer is constructed from an exact connected request feed and an already
sealed query-session binding.  It performs no work until the SDK-owned rules
port asks for a snapshot on the existing CTP owner thread.  Collection uses
the CTP client’s typed, sealed ``QueryResult`` objects and
``CtpNativeQueryCertificateBuilder``; it never accepts caller-authored facts
or certificates.

The resulting facts preserve CTP ``ByMoney`` / ``ByVolume`` values but leave
both price and fee currencies unavailable: CTP instrument and commission-rate
rows do not carry a verified currency.  Certificate completeness currently
requires the provider’s seven read-only query families.  Account, position,
order, and trade rows are used only inside the certificate builder and are
never copied into the neutral snapshot.

This source certifies a bounded set of reads, not one atomic exchange
snapshot, a balance lease, order authority, or permission to dispatch.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Dict, Mapping, Optional, Tuple, TYPE_CHECKING

from bt_api_base.execution_rules_protocol import (
    InstrumentExecutionFacts,
)
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_ctp.containers.ctp.ctp_native_query_certificate import (
    CtpNativeQueryCertificate,
    CtpNativeQueryCertificateBuilder,
)
from bt_api_ctp.execution_economics_registration import (
    CtpExecutionRulesSnapshotEvidence,
)
from bt_api_ctp.execution_rules import (
    ctp_commission_rules_from_native_row,
    ctp_option_metadata_from_native_row,
)
from bt_api_ctp.query import QueryResult
from bt_api_ctp.registered_sim_session import (
    CtpQuerySessionBinding,
    validate_query_session,
)

if TYPE_CHECKING:
    from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

_CTP_FUTURE_ROUTE = "CTP___FUTURE"
_REQUIRED_QUERY_TYPES = (
    "account",
    "positions",
    "orders",
    "trades",
    "instruments",
    "margin_rate",
    "commission_rate",
)
_HEDGE_FLAG_NAMES = {
    "speculation": "THOST_FTDC_HF_Speculation",
    "arbitrage": "THOST_FTDC_HF_Arbitrage",
    "hedge": "THOST_FTDC_HF_Hedge",
}
_SUPPORTED_HEDGE_CLASSES = frozenset(_HEDGE_FLAG_NAMES)
_MAX_RETAINED_EVIDENCE = 4096
_MIN_QUERY_REMAINING_SECONDS = 0.025


class CtpExecutionRulesSourceError(ValueError):
    """Finite error for an unavailable or mismatched source dependency."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class CtpExecutionRulesSnapshotSourceDependenciesV1:
    """Typed runtime injection for the exact CTP rules query source.

    ``query_session_binding`` must have been issued by
    ``capture_query_session`` for ``request_feed.trader_client``.  The source
    issuer revalidates that exact binding before and after collection and on
    every subsequent snapshot validation.  Neither object is inferred from a
    symbol, route name, or app-provided field.
    """

    request_feed: CtpRequestDataFuture = field(repr=False, compare=False)
    query_session_binding: CtpQuerySessionBinding = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        from bt_api_ctp.ctp.client import TraderClient
        from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

        if type(self.request_feed) is not CtpRequestDataFuture:
            raise TypeError("request_feed must be the exact CtpRequestDataFuture")
        if type(self.query_session_binding) is not CtpQuerySessionBinding:
            raise TypeError("query_session_binding must be a sealed CTP binding")
        client = self.request_feed.trader_client
        if type(client) is not TraderClient:
            raise TypeError("request_feed must own an exact TraderClient")
        if validate_query_session(
            client,
            self.query_session_binding,
            now_monotonic=time.monotonic(),
        ) is not True:
            raise ValueError("query_session_binding is not current for request_feed")

    def __repr__(self) -> str:
        return "<CTP execution-rules source dependencies>"


@dataclass(frozen=True, repr=False)
class _IssuedEvidence:
    evidence: CtpExecutionRulesSnapshotEvidence = field(repr=False, compare=False)
    instrument: MarketInstrumentKey = field(repr=False, compare=False)
    hedge_class: str


class CtpNativeExecutionRulesSnapshotEvidenceIssuer:
    """Issue rules facts from current, certified CTP query results.

    Construct this object on the CTP owner thread and inject this same object
    into the sealed runtime context as ``execution_economics_issuer``.  The
    name of that existing typed injection slot is shared with the optional
    economics owner; this issuer implements the rules snapshot source only.

    Collection schedules within 80 percent of the runtime owner's call
    timeout, leaving room for certificate verification.  Each native request
    receives the same absolute monotonic deadline.  The vendor native call
    itself cannot be interrupted after entry; an enclosing owner timeout can
    therefore leave that read uncertain/in progress.  Such a timeout is never
    retried.  A timeout, partial query, changed session, missing raw fee
    dimension, or ambiguous row returns ``None`` when control returns here.

    The query client defaults to 1.05 seconds between requests.  Seven
    certified query families therefore need more than 6.3 seconds even when
    every callback is immediate.  With the 80-percent reserve, the default
    five-second owner timeout fails closed before issuing any query.  A caller
    must explicitly configure a larger owner timeout; this issuer never
    lengthens one silently.
    """

    def __init__(self, dependencies: CtpExecutionRulesSnapshotSourceDependenciesV1) -> None:
        if type(dependencies) is not CtpExecutionRulesSnapshotSourceDependenciesV1:
            raise TypeError("typed CTP execution-rules source dependencies are required")
        self._dependencies = dependencies
        self._request_feed = dependencies.request_feed
        self._client = dependencies.request_feed.trader_client
        self._session_binding = dependencies.query_session_binding
        self._owner_thread_id = threading.get_ident()
        self._issued: Dict[int, _IssuedEvidence] = {}
        self._lock = threading.RLock()

    def is_bound_to(self, *, route: object, runtime_context: object) -> bool:
        """Verify exact selected feed, issuer identity, seal, and query session."""

        try:
            from bt_api_base.execution_correlation_protocol import SelectedExecutionRoute
            from bt_api_ctp.ctp.client import TraderClient
            from bt_api_ctp.feeds.live_ctp_feed import (
                CtpRequestDataFuture,
                _CtpRuntimeOwnedRequestFeedBinding,
            )
            from bt_api_ctp.order_provider import CtpOrderProviderRuntimeContext

            if (
                threading.get_ident() != self._owner_thread_id
                or type(route) is not SelectedExecutionRoute
                or route.exchange_name != _CTP_FUTURE_ROUTE
                or route.environment != "demo"
                or type(runtime_context) is not CtpOrderProviderRuntimeContext
                or type(self._request_feed) is not CtpRequestDataFuture
                or route.client is not self._request_feed
                or self._request_feed._connected is not True
                or type(self._client) is not TraderClient
                or self._request_feed.trader_client is not self._client
                or self._session_binding.environment != route.environment
                or runtime_context.execution_economics_issuer is not self
                or runtime_context.execution_economics_owner is None
                or getattr(runtime_context.execution_economics_owner, "_issuer", None) is not self
                or runtime_context.scope is None
                or getattr(runtime_context.scope, "environment", None) != "simulation"
                or type(runtime_context.runtime_binding) is not _CtpRuntimeOwnedRequestFeedBinding
                or runtime_context.runtime_binding.validate_provider_context(
                    runtime_context,
                    exchange_name=_CTP_FUTURE_ROUTE,
                    selected_feed=self._request_feed,
                    trader_client=self._client,
                ) is not True
            ):
                return False
            return (
                validate_query_session(
                    self._client,
                    self._session_binding,
                    now_monotonic=time.monotonic(),
                )
                is True
            )
        except Exception:
            return False

    def collect_instrument_rules_snapshot(
        self,
        instrument: MarketInstrumentKey,
        *,
        hedge_class: str,
        route: object,
        runtime_context: object,
    ) -> Optional[CtpExecutionRulesSnapshotEvidence]:
        """Read and certify the exact instrument/hedge source bundle."""

        if (
            not self.is_bound_to(route=route, runtime_context=runtime_context)
            or not _instrument_matches_route(instrument, route)
            or type(hedge_class) is not str
            or hedge_class not in _SUPPORTED_HEDGE_CLASSES
        ):
            return None
        hedge_flag = _native_hedge_flag(hedge_class)
        if hedge_flag is None:
            return None

        with self._lock:
            _purge_expired_evidence(self._issued, datetime.now(timezone.utc))
            if len(self._issued) >= _MAX_RETAINED_EVIDENCE:
                return None

        try:
            certificate, results = self._collect_query_bundle(
                instrument,
                hedge_flag,
                runtime_context,
            )
            if not self._query_session_is_current():
                return None
            observed = datetime.now(timezone.utc)
            facts = _facts_from_certified_results(
                instrument,
                hedge_class,
                self._session_binding.generation,
                certificate,
                results,
                observed_at_utc=observed,
            )
            if facts is None:
                return None
            evidence = CtpExecutionRulesSnapshotEvidence(
                facts=facts,
                native_query_certificate=certificate,
            )
        except Exception:
            return None

        with self._lock:
            _purge_expired_evidence(self._issued, datetime.now(timezone.utc))
            if len(self._issued) >= _MAX_RETAINED_EVIDENCE:
                return None
            self._issued[id(evidence)] = _IssuedEvidence(
                evidence=evidence,
                instrument=instrument,
                hedge_class=hedge_class,
            )
        return evidence

    def validate_instrument_rules_snapshot(
        self,
        instrument: MarketInstrumentKey,
        evidence: CtpExecutionRulesSnapshotEvidence,
        *,
        hedge_class: str,
        route: object,
        runtime_context: object,
        now_utc: datetime,
    ) -> bool:
        """Recheck current sealed results and source TTL without new queries."""

        if (
            type(now_utc) is not datetime
            or now_utc.tzinfo is None
            or now_utc.utcoffset() != timezone.utc.utcoffset(now_utc)
            or type(evidence) is not CtpExecutionRulesSnapshotEvidence
            or not self.is_bound_to(route=route, runtime_context=runtime_context)
        ):
            return False
        trusted_now = datetime.now(timezone.utc)
        with self._lock:
            _purge_expired_evidence(self._issued, trusted_now)
            issued = self._issued.get(id(evidence))
        if (
            issued is None
            or issued.evidence is not evidence
            or issued.instrument is not instrument
            or issued.hedge_class != hedge_class
            or evidence.facts.instrument is not instrument
            or evidence.facts.hedge_class != hedge_class
        ):
            return False

        try:
            if (
                evidence.facts.observed_at_utc is None
                or evidence.facts.expires_at_utc is None
                or trusted_now < evidence.facts.observed_at_utc
                or trusted_now >= evidence.facts.expires_at_utc
                or not self._query_session_is_current()
            ):
                return False
            results = self._revalidate_certificate(
                evidence.native_query_certificate,
                instrument,
                hedge_class,
            )
            if results is None or not self._query_session_is_current():
                return False
            reconstructed = _facts_from_certified_results(
                instrument,
                hedge_class,
                self._session_binding.generation,
                evidence.native_query_certificate,
                results,
                observed_at_utc=evidence.facts.observed_at_utc,
                expires_at_utc=evidence.facts.expires_at_utc,
            )
            return reconstructed == evidence.facts
        except Exception:
            return False

    def _collect_query_bundle(
        self,
        instrument: MarketInstrumentKey,
        hedge_flag: str,
        runtime_context: object,
    ) -> Tuple[CtpNativeQueryCertificate, Mapping[str, QueryResult[object]]]:
        if not self._query_session_is_current():
            raise CtpExecutionRulesSourceError("QUERY_SESSION_UNAVAILABLE")
        timeout = getattr(runtime_context, "owner_call_timeout_seconds", None)
        if (
            type(timeout) not in (int, float)
            or isinstance(timeout, bool)
            or not isfinite(float(timeout))
            or float(timeout) < 0.1
        ):
            raise CtpExecutionRulesSourceError("OWNER_TIMEOUT_INVALID")
        started = time.monotonic()
        # Leave 20 percent for the enclosing provider's checks and immutable
        # evidence construction.  Never restart the series after a deadline.
        deadline = started + float(timeout) * 0.8
        query_interval, last_submitted = _query_rate_state(self._client)
        first_wait = max(0.0, query_interval - (started - last_submitted))
        minimum_rate_wait = first_wait + query_interval * (len(_REQUIRED_QUERY_TYPES) - 1)
        if deadline - started <= minimum_rate_wait + _MIN_QUERY_REMAINING_SECONDS:
            # The configured call budget cannot even fit the client's minimum
            # inter-request rate spacing.  Fail before making partial reads.
            raise CtpExecutionRulesSourceError("QUERY_BUDGET_TOO_SHORT")
        builder = CtpNativeQueryCertificateBuilder(
            self._client,
            instrument_id=instrument.native_symbol,
            exchange_id=instrument.venue,
            hedge_flag=hedge_flag,
        )
        results: Dict[str, QueryResult[object]] = {}

        def record(name: str, call: object) -> None:
            remaining = deadline - time.monotonic()
            if not isfinite(remaining) or remaining <= _MIN_QUERY_REMAINING_SECONDS:
                raise CtpExecutionRulesSourceError("QUERY_DEADLINE_EXHAUSTED")
            if not callable(call):
                raise CtpExecutionRulesSourceError("QUERY_SOURCE_UNAVAILABLE")
            result = call(remaining)
            if type(result) is not QueryResult:
                raise CtpExecutionRulesSourceError("QUERY_RESULT_INVALID")
            builder.add(result)
            results[name] = result

        record(
            "account",
            lambda remaining: self._client.query_account_result(
                timeout=remaining,
                deadline_monotonic=deadline,
            ),
        )
        record(
            "positions",
            lambda remaining: self._client.query_positions_result(
                timeout=remaining,
                deadline_monotonic=deadline,
            ),
        )
        record(
            "orders",
            lambda remaining: self._client.query_orders_result(
                instrument_id=instrument.native_symbol,
                exchange_id=instrument.venue,
                timeout=remaining,
                deadline_monotonic=deadline,
            ),
        )
        record(
            "trades",
            lambda remaining: self._client.query_trades_result(
                instrument_id=instrument.native_symbol,
                exchange_id=instrument.venue,
                timeout=remaining,
                deadline_monotonic=deadline,
            ),
        )
        record(
            "instruments",
            lambda remaining: self._client.query_instruments_result(
                instrument_id=instrument.native_symbol,
                exchange_id=instrument.venue,
                timeout=remaining,
                max_duration=remaining,
                deadline_monotonic=deadline,
            ),
        )
        def query_margin_rate(remaining: float) -> QueryResult[object]:
            return self._client.query_instrument_margin_rate_result(
                instrument.native_symbol,
                exchange_id=instrument.venue,
                hedge_flag=hedge_flag,
                timeout=remaining,
                deadline_monotonic=deadline,
            )

        record(
            "margin_rate",
            query_margin_rate,
        )
        record(
            "commission_rate",
            lambda remaining: self._client.query_instrument_commission_rate_result(
                instrument.native_symbol,
                exchange_id=instrument.venue,
                timeout=remaining,
                deadline_monotonic=deadline,
            ),
        )
        certificate = builder.finish()
        if (
            tuple(results) != _REQUIRED_QUERY_TYPES
            or certificate.connection_generation != self._session_binding.connection_generation
            or certificate.trading_day != self._session_binding.trading_day
            or not self._query_session_is_current()
        ):
            raise CtpExecutionRulesSourceError("QUERY_CERTIFICATE_SESSION_MISMATCH")
        return certificate, results

    def _revalidate_certificate(
        self,
        certificate: CtpNativeQueryCertificate,
        instrument: MarketInstrumentKey,
        hedge_class: str,
    ) -> Optional[Mapping[str, QueryResult[object]]]:
        if (
            type(certificate) is not CtpNativeQueryCertificate
            or not self._query_session_is_current()
        ):
            return None
        hedge_flag = _native_hedge_flag(hedge_class)
        if hedge_flag is None:
            return None
        try:
            public = certificate.as_public_dict()
            query_rows = public.get("queries") if type(public) is dict else None
            if type(query_rows) is not list or len(query_rows) != len(_REQUIRED_QUERY_TYPES):
                return None
            builder = CtpNativeQueryCertificateBuilder(
                self._client,
                instrument_id=instrument.native_symbol,
                exchange_id=instrument.venue,
                hedge_flag=hedge_flag,
            )
            current: Dict[str, QueryResult[object]] = {}
            for expected_type, row in zip(_REQUIRED_QUERY_TYPES, query_rows):
                if (
                    type(row) is not dict
                    or row.get("request_type") != expected_type
                    or type(row.get("request_id")) is not int
                    or row["request_id"] <= 0
                ):
                    return None
                result = self._client.get_query_result(row["request_id"])
                if type(result) is not QueryResult or result.request_type != expected_type:
                    return None
                builder.add(result)
                current[expected_type] = result
            rebuilt = builder.finish()
            if (
                rebuilt.certificate_sha256 != certificate.certificate_sha256
                or rebuilt.query_digests != certificate.query_digests
                or rebuilt.connection_generation != self._session_binding.connection_generation
                or rebuilt.trading_day != self._session_binding.trading_day
                or not self._query_session_is_current()
            ):
                return None
            return current
        except Exception:
            return None

    def _query_session_is_current(self) -> bool:
        try:
            return (
                validate_query_session(
                    self._client,
                    self._session_binding,
                    now_monotonic=time.monotonic(),
                )
                is True
            )
        except Exception:
            return False


def _instrument_matches_route(instrument: object, route: object) -> bool:
    try:
        from bt_api_base.execution_correlation_protocol import SelectedExecutionRoute

        return bool(
            type(instrument) is MarketInstrumentKey
            and type(route) is SelectedExecutionRoute
            and route.exchange_name == _CTP_FUTURE_ROUTE
            and route.environment == "demo"
            and instrument.provider == "ctp"
            and instrument.environment == route.environment
            and instrument.product_type in {"future", "option"}
            and type(instrument.instrument_id) is str
            and instrument.instrument_id == instrument.native_symbol
            and type(instrument.native_symbol) is str
            and bool(instrument.native_symbol)
            and type(instrument.venue) is str
            and bool(instrument.venue)
        )
    except Exception:
        return False


def _native_hedge_flag(hedge_class: str) -> Optional[str]:
    name = _HEDGE_FLAG_NAMES.get(hedge_class)
    if name is None:
        return None
    try:
        from bt_api_ctp.ctp import ctp_constants

        value = getattr(ctp_constants, name)
    except Exception:
        return None
    return value if type(value) is str and value and value == value.strip() else None


def _query_rate_state(client: object) -> Tuple[float, float]:
    """Read only the client's finite request-spacing state, without logging it."""

    interval = getattr(client, "_query_interval", None)
    last_submitted = getattr(client, "_last_query_submitted_at", None)
    if (
        type(interval) not in (int, float)
        or isinstance(interval, bool)
        or type(last_submitted) not in (int, float)
        or isinstance(last_submitted, bool)
    ):
        raise CtpExecutionRulesSourceError("QUERY_RATE_STATE_UNAVAILABLE")
    interval_value = float(interval)
    last_value = float(last_submitted)
    now = time.monotonic()
    if (
        not isfinite(interval_value)
        or interval_value < 0
        or not isfinite(last_value)
        or last_value < 0
        or last_value > now
    ):
        raise CtpExecutionRulesSourceError("QUERY_RATE_STATE_INVALID")
    return interval_value, last_value


def _facts_from_certified_results(
    instrument: MarketInstrumentKey,
    hedge_class: str,
    session_generation: str,
    certificate: CtpNativeQueryCertificate,
    results: Mapping[str, QueryResult[object]],
    *,
    observed_at_utc: datetime,
    expires_at_utc: Optional[datetime] = None,
) -> Optional[InstrumentExecutionFacts]:
    if (
        type(certificate) is not CtpNativeQueryCertificate
        or type(results) not in (dict,)
        or tuple(results) != _REQUIRED_QUERY_TYPES
        or type(hedge_class) is not str
        or hedge_class not in _SUPPORTED_HEDGE_CLASSES
        or type(session_generation) is not str
        or not session_generation
        or type(observed_at_utc) is not datetime
        or observed_at_utc.tzinfo is None
        or observed_at_utc.utcoffset() != timezone.utc.utcoffset(observed_at_utc)
    ):
        return None
    try:
        payload = certificate.as_public_dict()
        query_rows = payload.get("queries") if type(payload) is dict else None
        query_digests = dict(certificate.query_digests)
        if (
            payload.get("schema") != "ctp_native_query_certificate.v5"
            or payload.get("complete") is not True
            or payload.get("atomic_snapshot") is not False
            or payload.get("execution_authorized") is not False
            or type(query_rows) is not list
            or len(query_rows) != len(_REQUIRED_QUERY_TYPES)
            or set(query_digests) != set(_REQUIRED_QUERY_TYPES)
        ):
            return None
        expiry_values = tuple(_parse_utc(row.get("expires_at_utc")) for row in query_rows)
        completion_values = tuple(_parse_utc(row.get("completed_at_utc")) for row in query_rows)
        earliest_expiry = min(expiry_values)
        latest_completion = max(completion_values)
        expiry = expires_at_utc if expires_at_utc is not None else earliest_expiry
        if (
            type(expiry) is not datetime
            or expiry.tzinfo is None
            or expiry.utcoffset() != timezone.utc.utcoffset(expiry)
            or observed_at_utc < latest_completion
            or expiry > earliest_expiry
            or expiry <= observed_at_utc
        ):
            return None

        instruments = results["instruments"].records
        commission_rows = results["commission_rate"].records
        if type(instruments) not in (tuple, list) or len(instruments) != 1:
            return None
        if type(commission_rows) not in (tuple, list) or len(commission_rows) != 1:
            return None
        instrument_row = instruments[0]
        commission_row = commission_rows[0]
        source_ref = query_digests["instruments"]
        commission_source_ref = query_digests["commission_rate"]
        if not _is_sha256(source_ref) or not _is_sha256(commission_source_ref):
            return None

        native_id = _row_text(instrument_row, "InstrumentID")
        exchange_id = _row_text(instrument_row, "ExchangeID")
        product_class = _row_text(instrument_row, "ProductClass")
        if native_id != instrument.native_symbol or exchange_id != instrument.venue:
            return None
        asset_kind = {"1": "future", "2": "option", "6": "option"}.get(product_class)
        if asset_kind != instrument.product_type:
            return None
        price_tick = _decimal_text(_row_decimal(instrument_row, "PriceTick", positive=True))
        multiplier = _decimal_text(
            _row_decimal(instrument_row, "VolumeMultiple", positive=True, integral=True)
        )
        if (
            _row_text(commission_row, "InstrumentID") != instrument.native_symbol
            or _row_text(commission_row, "ExchangeID") != instrument.venue
        ):
            return None
        commission_rules = ctp_commission_rules_from_native_row(
            commission_row,
            instrument,
            session_generation=session_generation,
            source_ref=commission_source_ref,
        )
        if (
            not commission_rules
            or any(
                rule.by_money_rate is None
                or rule.by_volume_amount is None
                or rule.currency_status != "unavailable"
                or rule.currency is not None
                for rule in commission_rules
            )
        ):
            return None

        option_fields: Dict[str, object] = {}
        if asset_kind == "option":
            # CTP option instrument prices are premium quotes by the provider's
            # native contract semantics; this does not establish any currency.
            option = ctp_option_metadata_from_native_row(
                instrument_row,
                instrument,
                source_ref=source_ref,
                premium_style="premium",
            )
            option_fields = {
                "option_right": option.right,
                "option_strike": option.strike,
                "option_expiry": option.expiry,
                "option_premium_style": option.premium_style,
                "option_underlying_instrument_id": option.underlying_instrument_id,
                "option_underlying_multiple": option.underlying_multiple,
            }
        elif asset_kind != "future":
            return None

        return InstrumentExecutionFacts(
            instrument=instrument,
            status="available",
            reason_code=None,
            session_generation=session_generation,
            source_ref=source_ref,
            observed_at_utc=observed_at_utc,
            expires_at_utc=expiry,
            asset_kind=asset_kind,
            price_tick=price_tick,
            contract_multiplier=multiplier,
            price_currency=None,
            price_currency_status="unavailable",
            price_currency_source_ref=None,
            price_currency_session_generation=None,
            commission_rules=commission_rules,
            hedge_class=hedge_class,
            **option_fields,
        )
    except Exception:
        return None


def _row_text(row: object, field_name: str) -> str:
    try:
        value = row.get(field_name) if isinstance(row, Mapping) else getattr(row, field_name)
    except Exception as exc:
        raise ValueError("native row field unavailable") from exc
    if type(value) is bytes:
        value = value.decode("utf-8", errors="strict")
    if type(value) is not str:
        raise ValueError("native row field invalid")
    text = value.strip()
    if not text:
        raise ValueError("native row field missing")
    return text


def _row_decimal(
    row: object,
    field_name: str,
    *,
    positive: bool = False,
    integral: bool = False,
) -> Decimal:
    try:
        value = row.get(field_name) if isinstance(row, Mapping) else getattr(row, field_name)
    except Exception as exc:
        raise ValueError("native row numeric field unavailable") from exc
    if type(value) not in (str, int, float, Decimal) or isinstance(value, bool):
        raise ValueError("native row numeric field invalid")
    try:
        decimal = value if type(value) is Decimal else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("native row numeric field invalid") from exc
    if (
        not decimal.is_finite()
        or decimal <= 0
        or (integral and decimal != decimal.to_integral_value())
        or (positive and decimal <= 0)
    ):
        raise ValueError("native row numeric field invalid")
    return decimal


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if not text or len(text) > 128:
        raise ValueError("native row decimal is outside the neutral bound")
    return text


def _parse_utc(value: object) -> datetime:
    if type(value) is not str or not value:
        raise ValueError("query timestamp missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("query timestamp is not UTC")
    return parsed


def _is_sha256(value: object) -> bool:
    return bool(
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _purge_expired_evidence(
    issued: Dict[int, _IssuedEvidence],
    now_utc: datetime,
) -> None:
    for key, entry in tuple(issued.items()):
        expires = entry.evidence.facts.expires_at_utc
        if expires is None or expires <= now_utc:
            issued.pop(key, None)


__all__ = [
    "CtpExecutionRulesSnapshotSourceDependenciesV1",
    "CtpExecutionRulesSourceError",
    "CtpNativeExecutionRulesSnapshotEvidenceIssuer",
]
