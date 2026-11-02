"""CTP-owned product-currency and open-margin evidence for one exact issuer.

This mixin is attached to the exact ``CtpNativeOrderPositionEvidenceIssuer``
instance.  It reuses that instance's sealed seven-query rules source and the
same neutral QueryFacade binding; it does not open another client or issue
execution authority. The extra native Product query proves only product
trading currency from ``TradeCurrencyID``. CTP's commission-rate rows have no
fee-currency field, so this source does not promote that product value to fee
currency; complete order economics remain unavailable until an explicit
same-provider fee-currency authority is added. Missing queries, leases, margin
rows, or owner-thread budget likewise remain unavailable.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
from math import isfinite
from typing import Optional

from bt_api_base.account_protocol import AccountBalance
from bt_api_base.execution_correlation_protocol import SelectedExecutionRoute
from bt_api_base.execution_economics_protocol import (
    EconomicsRevalidationStage,
    ExecutionEconomicsDecision,
    ExecutionEconomicsRequest,
)
from bt_api_base.execution_rules_protocol import (
    ExecutionRuleDecision,
    InstrumentExecutionFacts,
)
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_base.query_protocol import (
    QueryBatchReceipt,
    QueryRequest,
    QuerySnapshot,
    QuerySnapshotLease,
)
from bt_api_ctp.execution_economics_registration import (
    CtpExecutionEconomicsEvidence,
    CtpExecutionRulesSnapshotEvidence,
)
from bt_api_ctp.execution_rules_source_issuer import (
    CtpNativeExecutionRulesSnapshotEvidenceIssuer,
    _MIN_QUERY_REMAINING_SECONDS,
    _native_hedge_flag,
    _query_rate_state,
)
from bt_api_ctp.query import (
    QueryResult,
    _QUERY_MONOTONIC_CLOCK_DOMAIN,
    _QUERY_SOURCE_SEAL,
    _QuerySource,
    _query_records_digest,
    _scope_for_client,
)
from bt_api_ctp.registered_sim_session import (
    CtpQuerySessionBinding,
    validate_query_session,
)

_UTC = timezone.utc
_MAX_ISSUED_ECONOMICS = 256
_MAX_ENRICHED_RULES = 256
_ACCOUNT_LEASE_TTL_SECONDS = 5.0
_MIN_NATIVE_REMAINING_SECONDS = 0.05


@dataclass(frozen=True, repr=False, eq=False)
class _ProductCurrencyEvidence:
    request_id: int
    product_class: str
    product_id: str
    exchange_id: str
    currency: str
    records_sha256: str
    connection_generation: int
    trading_day: str
    completed_at_utc: datetime
    expires_at_utc: datetime
    expires_at_monotonic: float = field(repr=False, compare=False)


@dataclass(frozen=True, repr=False, eq=False)
class _EnrichedRulesEvidence:
    evidence: CtpExecutionRulesSnapshotEvidence = field(repr=False, compare=False)
    original: CtpExecutionRulesSnapshotEvidence = field(repr=False, compare=False)
    currency: _ProductCurrencyEvidence = field(repr=False, compare=False)
    instrument: MarketInstrumentKey = field(repr=False, compare=False)
    hedge_class: str


@dataclass(frozen=True, repr=False, eq=False)
class _IssuedEconomicsEvidence:
    evidence: CtpExecutionEconomicsEvidence = field(repr=False, compare=False)
    request: ExecutionEconomicsRequest = field(repr=False, compare=False)
    route: SelectedExecutionRoute = field(repr=False, compare=False)
    runtime_context: object = field(repr=False, compare=False)
    rules: _EnrichedRulesEvidence = field(repr=False, compare=False)
    account_receipt: QueryBatchReceipt = field(repr=False, compare=False)
    account_snapshot: QuerySnapshot = field(repr=False, compare=False)
    account_lease: QuerySnapshotLease = field(repr=False, compare=False)


class CtpNativeExecutionEconomicsSourceMixin:
    """Partial economics facts for the existing exact CTP rules/position issuer.

    The owner thread calls these methods through the existing CTP runtime.
    This source implements product-currency facts and open-futures margin.
    Complete order economics is explicitly disabled because CTP exposes no
    fee currency in its commission rows and no trusted same-provider fee
    currency source is currently bound. Options and non-open offsets are also
    unavailable until their margin formulas have source evidence.
    """

    def economics_source_ready(self) -> bool:
        """Report readiness only after a fee-currency source is explicitly bound."""

        return False

    def _initialize_economics_source(self) -> None:
        self.__economics_lock = threading.RLock()
        self.__economics_route = None
        self.__economics_context = None
        self.__economics_query_facade = None
        self.__economics_rules: OrderedDict[int, _EnrichedRulesEvidence] = OrderedDict()
        self.__economics_issued: OrderedDict[int, _IssuedEconomicsEvidence] = OrderedDict()

    def _bind_economics_query_facade(
        self,
        route: SelectedExecutionRoute,
        runtime_context: object,
        query_facade: object,
    ) -> bool:
        if (
            threading.get_ident() != self._owner_thread_id
            or type(route) is not SelectedExecutionRoute
            or route.client is not self._request_feed
            or route.execution_context is not runtime_context
            or not _is_query_facade(query_facade)
            or self.is_bound_to(route=route, runtime_context=runtime_context) is not True
        ):
            return False
        with self.__economics_lock:
            if self.__economics_query_facade is None:
                self.__economics_route = route
                self.__economics_context = runtime_context
                self.__economics_query_facade = query_facade
                return True
            return (
                self.__economics_route is route
                and self.__economics_context is runtime_context
                and self.__economics_query_facade is query_facade
            )

    def _economics_enrich_rules_snapshot(
        self,
        instrument: MarketInstrumentKey,
        hedge_class: str,
        route: SelectedExecutionRoute,
        runtime_context: object,
        original: CtpExecutionRulesSnapshotEvidence,
        *,
        deadline_monotonic: float,
    ) -> Optional[CtpExecutionRulesSnapshotEvidence]:
        if (
            threading.get_ident() != self._owner_thread_id
            or type(original) is not CtpExecutionRulesSnapshotEvidence
            or not self.is_bound_to(route=route, runtime_context=runtime_context)
            or not _is_current_query_session(self)
        ):
            return None

        with self.__economics_lock:
            cached = self.__economics_rules.get(id(original))
        if cached is not None and cached.evidence is original:
            if self._validate_product_currency(cached.currency):
                return cached.evidence
            with self.__economics_lock:
                if self.__economics_rules.get(id(original)) is cached:
                    del self.__economics_rules[id(original)]
            return None
        if cached is not None and cached.original is original:
            if self._validate_product_currency(cached.currency):
                return cached.evidence
            with self.__economics_lock:
                if self.__economics_rules.get(id(original)) is cached:
                    del self.__economics_rules[id(original)]
            # A stale/changed native product result is not silently refreshed
            # inside the same evidence request.
            return None

        facts = original.facts
        if (
            type(facts) is not InstrumentExecutionFacts
            or facts.instrument is not instrument
            or facts.status != "available"
            or facts.hedge_class != hedge_class
            or facts.session_generation != self._session_binding.generation
            or facts.contract_multiplier is None
        ):
            return None

        results = self._revalidate_certificate(
            original.native_query_certificate,
            instrument,
            hedge_class,
        )
        if type(results) is not dict:
            return None
        instrument_rows = results.get("instruments")
        if type(instrument_rows) is not QueryResult or len(instrument_rows.records) != 1:
            return None
        instrument_row = instrument_rows.records[0]
        try:
            product_class = _native_text(_field(instrument_row, "ProductClass"))
            product_id = _native_text(_field(instrument_row, "ProductID"))
            exchange_id = _native_text(_field(instrument_row, "ExchangeID"))
        except Exception:
            return None
        if product_class != "1" or exchange_id != instrument.venue or not product_id:
            return None

        currency_proof = self._query_product_currency(
            product_class,
            product_id,
            exchange_id,
            deadline_monotonic=deadline_monotonic,
        )
        if currency_proof is None:
            return None

        try:
            currency_source_ref = currency_proof.records_sha256
            enriched_facts = replace(
                facts,
                price_currency=currency_proof.currency,
                price_currency_status="verified",
                price_currency_source_ref=currency_source_ref,
                price_currency_session_generation=self._session_binding.generation,
            )
            enriched = CtpExecutionRulesSnapshotEvidence(
                facts=enriched_facts,
                native_query_certificate=original.native_query_certificate,
            )
            issued = _EnrichedRulesEvidence(
                evidence=enriched,
                original=original,
                currency=currency_proof,
                instrument=instrument,
                hedge_class=hedge_class,
            )
        except Exception:
            return None

        with self.__economics_lock:
            self._purge_economics_rules(datetime.now(_UTC))
            if len(self.__economics_rules) >= _MAX_ENRICHED_RULES:
                return None
            self.__economics_rules[id(enriched)] = issued
            self.__economics_rules.move_to_end(id(enriched))
        return enriched

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
        if (
            threading.get_ident() != self._owner_thread_id
            or type(evidence) is not CtpExecutionRulesSnapshotEvidence
            or type(now_utc) is not datetime
            or not _is_utc(now_utc)
            or not self.is_bound_to(route=route, runtime_context=runtime_context)
        ):
            return False
        with self.__economics_lock:
            issued = self.__economics_rules.get(id(evidence))
        if issued is None or issued.evidence is not evidence:
            # Raw facts issued by the underlying rules source remain useful to
            # read-only consumers, but they retain unavailable currency and
            # therefore cannot authorize an economics decision.
            return CtpNativeExecutionRulesSnapshotEvidenceIssuer.validate_instrument_rules_snapshot(
                self,
                instrument,
                evidence,
                hedge_class=hedge_class,
                route=route,
                runtime_context=runtime_context,
                now_utc=now_utc,
            ) is True
        if (
            issued.instrument is not instrument
            or issued.hedge_class != hedge_class
            or evidence.facts.instrument is not instrument
            or evidence.facts.hedge_class != hedge_class
            or not self._validate_product_currency(issued.currency)
        ):
            return False
        return (
            CtpNativeExecutionRulesSnapshotEvidenceIssuer.validate_instrument_rules_snapshot(
                self,
                instrument,
                issued.original,
                hedge_class=hedge_class,
                route=route,
                runtime_context=runtime_context,
                now_utc=datetime.now(_UTC),
            )
            is True
        )

    def is_economics_source_bound_to(
        self,
        *,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool:
        binding = self._economics_binding()
        return bool(
            binding is not None
            and binding[0] is route
            and binding[1] is runtime_context
            and self.is_bound_to(route=route, runtime_context=runtime_context) is True
        )

    def is_economics_bound_to(
        self,
        *,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool:
        """Alias used by runtime source checks; proves binding, not permission."""

        return self.is_economics_source_bound_to(route=route, runtime_context=runtime_context)

    def is_bound_to_economics(
        self,
        *,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool:
        return self.is_economics_source_bound_to(route=route, runtime_context=runtime_context)

    def collect_evidence(
        self,
        request: ExecutionEconomicsRequest,
        *,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> Optional[CtpExecutionEconomicsEvidence]:
        if (
            threading.get_ident() != self._owner_thread_id
            or type(request) is not ExecutionEconomicsRequest
            or self.economics_source_ready() is not True
            or not self.is_economics_source_bound_to(route=route, runtime_context=runtime_context)
            or not _is_current_query_session(self)
        ):
            return None
        rule = request.rule_decision
        if (
            type(rule) is not ExecutionRuleDecision
            or rule.status != "accepted"
            or rule.request is not request.rule_request
            or rule.facts is None
            or rule.facts.instrument is not request.instrument
            or rule.normalized_offset != "open"
            or rule.facts.asset_kind != "future"
        ):
            return None
        with self.__economics_lock:
            rules = self.__economics_rules.get(id(rule.facts))
        if (
            rules is None
            or rules.evidence.facts is not rule.facts
            or rules.instrument is not request.instrument
            or rules.hedge_class != rule.facts.hedge_class
            or not self.validate_instrument_rules_snapshot(
                request.instrument,
                rules.evidence,
                hedge_class=rules.hedge_class,
                route=route,
                runtime_context=runtime_context,
                now_utc=datetime.now(_UTC),
            )
        ):
            return None
        open_fee = next(
            (fee for fee in rules.evidence.facts.commission_rules if fee.role == "open"),
            None,
        )
        if (
            open_fee is None
            or open_fee.currency_status != "verified"
            or open_fee.currency != rules.currency.currency
            or open_fee.currency_source_ref is None
            or open_fee.currency_session_generation != self._session_binding.generation
        ):
            # Product TradeCurrencyID establishes product trade currency only.
            # CTP's commission-rate rows contain no fee currency, and no
            # explicit same-provider fee-currency authority is currently bound.
            return None

        results = self._revalidate_certificate(
            rules.evidence.native_query_certificate,
            request.instrument,
            rules.hedge_class,
        )
        if type(results) is not dict:
            return None
        margin_result = results.get("margin_rate")
        if type(margin_result) is not QueryResult or len(margin_result.records) != 1:
            return None
        hedge_flag = _native_hedge_flag(rules.hedge_class)
        if hedge_flag is None:
            return None
        margin = _margin_requirement_before_fee(
            request,
            rule,
            rules.evidence.facts,
            margin_result.records[0],
            hedge_flag,
        )
        if margin is None:
            return None

        query_binding = self._economics_binding()
        if (
            query_binding is None
            or query_binding[0] is not route
            or query_binding[1] is not runtime_context
        ):
            return None
        account = self._collect_account_snapshot(
            request,
            route,
            runtime_context,
            query_binding[2],
            currency=rules.currency.currency,
            expires_limit=min(
                rules.evidence.facts.expires_at_utc,
                rules.currency.expires_at_utc,
            ),
        )
        if account is None:
            return None
        receipt, snapshot, lease, available = account

        cert_rows = rules.evidence.native_query_certificate.as_public_dict().get("queries")
        if type(cert_rows) is not list or len(cert_rows) != 7:
            return None
        try:
            digests = dict(rules.evidence.native_query_certificate.query_digests)
            cert_completed = max(_parse_utc(row["completed_at_utc"]) for row in cert_rows)
            cert_expires = min(_parse_utc(row["expires_at_utc"]) for row in cert_rows)
        except Exception:
            return None
        now = datetime.now(_UTC)
        observed = max(
            cert_completed,
            rules.currency.completed_at_utc,
            snapshot.observed_at,
            lease.issued_at,
            now,
        )
        expires = min(
            cert_expires,
            rules.evidence.facts.expires_at_utc,
            rules.currency.expires_at_utc,
            lease.expires_at,
        )
        if expires is None or observed >= expires:
            return None
        try:
            ref_digest = sha256(
                "\x00".join(
                    (
                        rules.evidence.native_query_certificate.certificate_sha256,
                        rules.currency.records_sha256,
                        receipt.receipt_digest,
                        request.instrument.native_symbol,
                        request.rule_request.quantity,
                        request.rule_request.limit_price,
                        uuid.uuid4().hex,
                    )
                ).encode("utf-8", errors="strict")
            ).hexdigest()
            evidence = CtpExecutionEconomicsEvidence(
                request=request,
                requirement_kind="margin",
                required_amount_before_fee=margin,
                available_amount=available,
                currency=rules.currency.currency,
                source_ref="ctp-economics:sha256:" + ref_digest,
                account_balance_source_ref=snapshot.snapshot_ref,
                account_scope_ref="ctp-account-scope:" + uuid.uuid4().hex,
                session_generation=self._session_binding.generation,
                observed_at_utc=observed,
                expires_at_utc=expires,
                lease_ref="ctp-economics-lease:" + uuid.uuid4().hex,
                native_query_certificate=rules.evidence.native_query_certificate,
                account_receipt=receipt,
                account_snapshot=snapshot,
                account_lease=lease,
                evaluated_quantity=request.rule_request.quantity,
                quantity_unit=request.quantity_unit,
                side=request.rule_request.side.lower(),
                position_side=rule.normalized_position_side,
                offset=rule.normalized_offset,
                limit_price=request.rule_request.limit_price,
                asset_kind=rules.evidence.facts.asset_kind,
                hedge_flag=hedge_flag,
                hedge_flag_source_ref=sha256(hedge_flag.encode("utf-8")).hexdigest(),
                margin_rate_source_ref=digests["margin_rate"],
                commission_rate_source_ref=digests["commission_rate"],
                price_currency_source_ref=rules.currency.records_sha256,
                fee_currency_source_ref=open_fee.currency_source_ref,
                position_evidence_ref=request.rule_request.position_evidence_ref,
                position_evidence_generation=request.rule_request.position_evidence_generation,
                position_observed_at_utc=request.rule_request.position_observed_at_utc,
                position_expires_at_utc=request.rule_request.position_expires_at_utc,
            )
            issued = _IssuedEconomicsEvidence(
                evidence=evidence,
                request=request,
                route=route,
                runtime_context=runtime_context,
                rules=rules,
                account_receipt=receipt,
                account_snapshot=snapshot,
                account_lease=lease,
            )
        except Exception:
            return None
        with self.__economics_lock:
            self._purge_economics_decisions(datetime.now(_UTC))
            if len(self.__economics_issued) >= _MAX_ISSUED_ECONOMICS:
                return None
            self.__economics_issued[id(evidence)] = issued
            self.__economics_issued.move_to_end(id(evidence))
        if not self.validate_evidence(
            request,
            evidence,
            route=route,
            runtime_context=runtime_context,
        ):
            with self.__economics_lock:
                if self.__economics_issued.get(id(evidence)) is issued:
                    del self.__economics_issued[id(evidence)]
            return None
        return evidence

    def validate_evidence(
        self,
        request: ExecutionEconomicsRequest,
        evidence: CtpExecutionEconomicsEvidence,
        *,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool:
        if (
            threading.get_ident() != self._owner_thread_id
            or type(request) is not ExecutionEconomicsRequest
            or type(evidence) is not CtpExecutionEconomicsEvidence
            or self.economics_source_ready() is not True
            or not self.is_economics_source_bound_to(route=route, runtime_context=runtime_context)
            or not _is_current_query_session(self)
        ):
            return False
        with self.__economics_lock:
            issued = self.__economics_issued.get(id(evidence))
        if (
            issued is None
            or issued.evidence is not evidence
            or issued.request is not request
            or issued.route is not route
            or issued.runtime_context is not runtime_context
            or evidence.request is not request
        ):
            return False
        if not self.validate_instrument_rules_snapshot(
            request.instrument,
            issued.rules.evidence,
            hedge_class=issued.rules.hedge_class,
            route=route,
            runtime_context=runtime_context,
            now_utc=datetime.now(_UTC),
        ):
            return False
        results = self._revalidate_certificate(
            evidence.native_query_certificate,
            request.instrument,
            issued.rules.hedge_class,
        )
        margin_result = results.get("margin_rate") if type(results) is dict else None
        if type(margin_result) is not QueryResult or len(margin_result.records) != 1:
            return False
        expected_margin = _margin_requirement_before_fee(
            request,
            request.rule_decision,
            issued.rules.evidence.facts,
            margin_result.records[0],
            evidence.hedge_flag,
        )
        if expected_margin is None or expected_margin != evidence.required_amount_before_fee:
            return False
        facade = self._economics_binding()
        if facade is None or facade[2] is not self.__economics_query_facade:
            return False
        try:
            return bool(
                self._validate_product_currency(issued.rules.currency)
                and issued.account_snapshot is evidence.account_snapshot
                and issued.account_receipt is evidence.account_receipt
                and issued.account_lease is evidence.account_lease
                and issued.account_snapshot.request.family == "account"
                and issued.account_snapshot.generation == self._session_binding.generation
                and issued.account_snapshot.source_ref == self._session_binding.source_ref
                and issued.account_lease.purpose == "execution_economics"
                and issued.account_lease.expires_at == evidence.expires_at_utc
                and facade[2].snapshot(
                    issued.account_receipt,
                    issued.account_snapshot.request.request_ref,
                )
                is issued.account_snapshot
                and facade[2].validate_lease(
                    issued.account_lease,
                    issued.account_receipt,
                    now=datetime.now(_UTC),
                )
                is True
                and validate_query_session(
                    self._client,
                    self._session_binding,
                    now_monotonic=time.monotonic(),
                )
                is True
            )
        except Exception:
            return False

    def revalidate_evidence(
        self,
        request: ExecutionEconomicsRequest,
        decision: ExecutionEconomicsDecision,
        evidence: CtpExecutionEconomicsEvidence,
        *,
        stage: EconomicsRevalidationStage,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool:
        return bool(
            type(decision) is ExecutionEconomicsDecision
            and decision.request is request
            and decision.status == "accepted"
            and type(stage) is str
            and stage in {"claim", "dispatch"}
            and self.validate_evidence(
                request,
                evidence,
                route=route,
                runtime_context=runtime_context,
            )
        )

    def _query_product_currency(
        self,
        product_class: str,
        product_id: str,
        exchange_id: str,
        *,
        deadline_monotonic: float,
    ) -> Optional[_ProductCurrencyEvidence]:
        if (
            threading.get_ident() != self._owner_thread_id
            or not _valid_text(product_class)
            or not _valid_text(product_id)
            or not _valid_text(exchange_id)
            or not _is_current_query_session(self)
        ):
            return None
        remaining = deadline_monotonic - time.monotonic()
        if not isfinite(remaining) or remaining <= _MIN_NATIVE_REMAINING_SECONDS:
            return None
        query = getattr(self._client, "query_products_result", None)
        if not callable(query):
            return None
        try:
            result = query(
                product_class=product_class,
                exchange_id=exchange_id,
                product_id=product_id,
                timeout=remaining,
                max_duration=remaining,
                deadline_monotonic=deadline_monotonic,
                max_records=8,
            )
            return _product_currency_evidence(
                result,
                self._client,
                self._session_binding,
                product_class=product_class,
                product_id=product_id,
                exchange_id=exchange_id,
                now_utc=datetime.now(_UTC),
                now_monotonic=time.monotonic(),
            )
        except Exception:
            return None

    def _validate_product_currency(self, evidence: _ProductCurrencyEvidence) -> bool:
        if (
            type(evidence) is not _ProductCurrencyEvidence
            or threading.get_ident() != self._owner_thread_id
            or not _is_current_query_session(self)
        ):
            return False
        try:
            current = self._client.get_query_result(evidence.request_id)
            rebuilt = _product_currency_evidence(
                current,
                self._client,
                self._session_binding,
                product_class=evidence.product_class,
                product_id=evidence.product_id,
                exchange_id=evidence.exchange_id,
                now_utc=datetime.now(_UTC),
                now_monotonic=time.monotonic(),
            )
            return bool(
                rebuilt is not None
                and rebuilt.currency == evidence.currency
                and rebuilt.records_sha256 == evidence.records_sha256
                and rebuilt.request_id == evidence.request_id
                and rebuilt.connection_generation == evidence.connection_generation
                and rebuilt.trading_day == evidence.trading_day
                and rebuilt.completed_at_utc == evidence.completed_at_utc
                and rebuilt.expires_at_utc == evidence.expires_at_utc
                and rebuilt.expires_at_monotonic == evidence.expires_at_monotonic
            )
        except Exception:
            return False

    def _collect_account_snapshot(
        self,
        request: ExecutionEconomicsRequest,
        route: SelectedExecutionRoute,
        runtime_context: object,
        query_facade: object,
        *,
        currency: str,
        expires_limit: datetime,
    ) -> tuple[QueryBatchReceipt, QuerySnapshot, QuerySnapshotLease, str] | None:
        if (
            not _is_query_facade(query_facade)
            or not _is_utc(expires_limit)
            or not _is_current_query_session(self)
        ):
            return None
        timeout = getattr(runtime_context, "owner_call_timeout_seconds", None)
        if (
            type(timeout) not in (int, float)
            or isinstance(timeout, bool)
            or not isfinite(float(timeout))
            or float(timeout) < 0.1
        ):
            return None
        started = datetime.now(_UTC)
        deadline = started + timedelta(seconds=float(timeout) * 0.75)
        query_request = QueryRequest(
            request_ref="economics-account-" + uuid.uuid4().hex,
            family="account",
            filters={},
            required=True,
            require_complete_scope=True,
        )
        try:
            receipt = query_facade.refresh((query_request,), deadline=deadline)
        except Exception:
            return None
        snapshot = _complete_account_snapshot(receipt, query_request, self._session_binding)
        if (
            snapshot is None
            or query_facade.snapshot(receipt, query_request.request_ref) is not snapshot
            or snapshot.observed_at is None
            or snapshot.observed_at > datetime.now(_UTC)
            or not _is_current_query_session(self)
        ):
            return None
        balances = _account_balances(snapshot)
        if balances is None:
            return None
        matching = [balance for balance in balances if balance.currency == currency]
        if len(matching) != 1 or matching[0].available is None:
            return None

        expiry_limit = min(
            expires_limit,
            snapshot.observed_at + timedelta(seconds=_ACCOUNT_LEASE_TTL_SECONDS),
        )
        if datetime.now(_UTC) >= expiry_limit:
            return None
        try:
            lease = query_facade.acquire_snapshot_lease(
                receipt,
                purpose="execution_economics",
                expires_at=expiry_limit,
            )
        except Exception:
            return None
        now = datetime.now(_UTC)
        if (
            not _lease_matches_account(lease, receipt, snapshot)
            or lease.expires_at > expiry_limit
            or now >= lease.expires_at
            or query_facade.validate_lease(lease, receipt, now=now) is not True
            or not _is_current_query_session(self)
        ):
            return None
        return receipt, snapshot, lease, matching[0].available

    def _economics_binding(self) -> tuple[object, object, object] | None:
        with self.__economics_lock:
            route = self.__economics_route
            context = self.__economics_context
            facade = self.__economics_query_facade
        if (
            type(route) is not SelectedExecutionRoute
            or route.client is not self._request_feed
            or route.execution_context is not context
            or not _is_query_facade(facade)
        ):
            return None
        return route, context, facade

    def _economics_native_query_deadline(
        self,
        runtime_context: object,
        *,
        request_count: int,
    ) -> Optional[float]:
        """Preflight the rate-spacing budget before an economics source read."""

        if (
            threading.get_ident() != self._owner_thread_id
            or type(request_count) is not int
            or request_count < 1
        ):
            return None
        timeout = getattr(runtime_context, "owner_call_timeout_seconds", None)
        if (
            type(timeout) not in (int, float)
            or isinstance(timeout, bool)
            or not isfinite(float(timeout))
            or float(timeout) < 0.1
        ):
            return None
        started = time.monotonic()
        try:
            query_interval, last_submitted = _query_rate_state(self._client)
        except Exception:
            return None
        first_wait = max(0.0, query_interval - (started - last_submitted))
        minimum_spacing = first_wait + query_interval * (request_count - 1)
        deadline = started + float(timeout) * 0.8
        if deadline - started <= minimum_spacing + _MIN_QUERY_REMAINING_SECONDS:
            return None
        return deadline

    def _is_enriched_rules_snapshot(
        self,
        evidence: CtpExecutionRulesSnapshotEvidence,
    ) -> bool:
        with self.__economics_lock:
            issued = self.__economics_rules.get(id(evidence))
        return bool(issued is not None and issued.evidence is evidence)

    def _purge_economics_rules(self, now: datetime) -> None:
        for key, issued in tuple(self.__economics_rules.items()):
            expiry = issued.evidence.facts.expires_at_utc
            if expiry is None or expiry <= now:
                self.__economics_rules.pop(key, None)

    def _purge_economics_decisions(self, now: datetime) -> None:
        for key, issued in tuple(self.__economics_issued.items()):
            if issued.evidence.expires_at_utc <= now:
                self.__economics_issued.pop(key, None)


def _product_currency_evidence(
    result: object,
    client: object,
    session_binding: object,
    *,
    product_class: str,
    product_id: str,
    exchange_id: str,
    now_utc: datetime,
    now_monotonic: float,
) -> Optional[_ProductCurrencyEvidence]:
    try:
        if (
            type(result) is not QueryResult
            or result.request_type != "products"
            or type(result.records) is not tuple
            or len(result.records) != 1
            or result.complete is not True
            or result.is_last_seen is not True
            or result.timed_out is not False
            or result.unsupported is not False
            or result.error_code not in (None, 0)
            or result.error_message != ""
            or result.submit_code not in (None, 0)
            or result.late_callback_count != 0
            or result.record_limit_exceeded is not False
            or result.completion_origin != "success"
            or type(session_binding) is not CtpQuerySessionBinding
            or type(result.request_id) is not int
            or result.request_id <= 0
            or not _is_utc(now_utc)
            or type(now_monotonic) not in (int, float)
            or isinstance(now_monotonic, bool)
            or not isfinite(float(now_monotonic))
            or validate_query_session(
                client,
                session_binding,
                now_monotonic=float(now_monotonic),
            )
            is not True
        ):
            return None
        scope = _scope_for_client(client)
        source = result.query_source
        if (
            type(source) is not _QuerySource
            or source._seal is not _QUERY_SOURCE_SEAL
            or source.issuer is not scope.issuer
            or source.account_fingerprint != scope.account_fingerprint
            or source.connection_generation != scope.connection_generation
            or source.trading_day != scope.trading_day
            or source.broker_id != scope.broker_id
            or source.investor_id != scope.investor_id
            or source.source_tags != session_binding._source_tags
            or source.request_type != "products"
            or source.request_id != result.request_id
            or source.account_fingerprint != session_binding.account_fingerprint
            or source.connection_generation != session_binding.connection_generation
            or source.trading_day != session_binding.trading_day
            or source.broker_id != session_binding.broker_id
            or source.investor_id != session_binding.investor_id
            or result.connection_generation != session_binding.connection_generation
            or result.account_fingerprint != session_binding.account_fingerprint
            or source.clock_domain_id != _QUERY_MONOTONIC_CLOCK_DOMAIN
            or source.started_at_utc != result.started_at_utc
            or source.completed_at_utc != result.completed_at_utc
            or not _is_utc(source.started_at_utc)
            or not _is_utc(source.completed_at_utc)
            or source.started_at_utc > source.completed_at_utc
            or type(source.completed_monotonic) not in (int, float)
            or isinstance(source.completed_monotonic, bool)
            or not isfinite(float(source.completed_monotonic))
            or source.completed_monotonic is None
            or source.completed_monotonic > float(now_monotonic)
            or source.trusted_expires_at_utc is None
            or source.trusted_expires_monotonic is None
            or now_utc >= source.trusted_expires_at_utc
            or float(now_monotonic) >= source.trusted_expires_monotonic
            or type(source.records_sha256) is not str
            or source.records_sha256 != _query_records_digest(result.records)
            or source.record_limit != 8
        ):
            return None
        expected = tuple(sorted({
            "ProductClass": product_class,
            "ProductID": product_id,
            "ExchangeID": exchange_id,
        }.items()))
        if (
            source.request_intent_filters != expected
            or source.request_filters != expected
            or source.explicit_request_filters
            != tuple(sorted(name for name, _ in expected))
        ):
            return None
        row = result.records[0]
        if (
            _native_text(_field(row, "ProductClass")) != product_class
            or _native_text(_field(row, "ProductID")) != product_id
            or _native_text(_field(row, "ExchangeID")) != exchange_id
        ):
            return None
        currency = _native_text(_field(row, "TradeCurrencyID"))
        if (
            len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or currency != currency.upper()
        ):
            return None
        return _ProductCurrencyEvidence(
            request_id=result.request_id,
            product_class=product_class,
            product_id=product_id,
            exchange_id=exchange_id,
            currency=currency,
            records_sha256=source.records_sha256,
            connection_generation=session_binding.connection_generation,
            trading_day=session_binding.trading_day,
            completed_at_utc=source.completed_at_utc,
            expires_at_utc=source.trusted_expires_at_utc,
            expires_at_monotonic=source.trusted_expires_monotonic,
        )
    except Exception:
        return None


def _margin_requirement_before_fee(
    request: ExecutionEconomicsRequest,
    decision: ExecutionRuleDecision,
    facts: InstrumentExecutionFacts,
    row: object,
    hedge_flag: str,
) -> Optional[str]:
    if (
        type(request) is not ExecutionEconomicsRequest
        or type(decision) is not ExecutionRuleDecision
        or type(facts) is not InstrumentExecutionFacts
        or facts.asset_kind != "future"
        or decision.normalized_offset != "open"
        or request.rule_request.instrument is not request.instrument
            or (not isinstance(row, dict) and not hasattr(row, "InstrumentID"))
            or request.quantity_unit != "contract"
    ):
        return None
    try:
        if (
            _native_text(_field(row, "InstrumentID")) != request.instrument.native_symbol
            or _native_text(_field(row, "ExchangeID")) != request.instrument.venue
            or _native_text(_field(row, "HedgeFlag")) != hedge_flag
        ):
            return None
        side = request.rule_request.side.lower()
        if side not in {"buy", "sell"}:
            return None
        prefix = "Long" if side == "buy" else "Short"
        rate = _nonnegative_decimal(_field(row, prefix + "MarginRatioByMoney"))
        amount = _nonnegative_decimal(_field(row, prefix + "MarginRatioByVolume"))
        quantity = _positive_decimal(request.rule_request.quantity)
        price = _positive_decimal(request.rule_request.limit_price)
        multiplier = _positive_decimal(facts.contract_multiplier)
        if quantity != quantity.to_integral_value():
            return None
        with localcontext() as context:
            context.prec = 700
            required = quantity * (price * multiplier * rate + amount)
        result = format(required, "f")
        if not required.is_finite() or len(result) > 128:
            return None
        return result
    except Exception:
        return None


def _complete_account_snapshot(
    receipt: object,
    request: QueryRequest,
    session_binding: object,
) -> Optional[QuerySnapshot]:
    try:
        if (
            type(receipt) is not QueryBatchReceipt
            or type(request) is not QueryRequest
            or type(session_binding) is not CtpQuerySessionBinding
            or receipt.requests != (request,)
            or receipt.requests[0] is not request
            or receipt.complete is not True
            or receipt.blockers
            or receipt.source_ref != session_binding.source_ref
            or receipt.generation != session_binding.generation
            or len(receipt.snapshots) != 1
        ):
            return None
        snapshot = receipt.snapshots[0]
        if (
            type(snapshot) is not QuerySnapshot
            or snapshot.request is not request
            or snapshot.request.family != "account"
            or snapshot.request.required is not True
            or snapshot.request.require_complete_scope is not True
            or snapshot.is_complete is not True
            or snapshot.scope_complete is not True
            or snapshot.source_ref != session_binding.source_ref
            or snapshot.generation != session_binding.generation
            or snapshot.snapshot_ref is None
            or not snapshot.records
            or snapshot.observed_at is None
            or snapshot.completed_at is None
        ):
            return None
        return snapshot
    except Exception:
        return None


def _account_balances(snapshot: QuerySnapshot) -> Optional[tuple[AccountBalance, ...]]:
    balances: list[AccountBalance] = []
    currencies: set[str] = set()
    try:
        for record in snapshot.records:
            balance = AccountBalance.from_dict(record)
            if balance.currency in currencies:
                return None
            currencies.add(balance.currency)
            balances.append(balance)
    except Exception:
        return None
    return tuple(balances) if balances else None


def _lease_matches_account(
    lease: object,
    receipt: QueryBatchReceipt,
    snapshot: QuerySnapshot,
) -> bool:
    return bool(
        type(lease) is QuerySnapshotLease
        and lease.receipt_digest == receipt.receipt_digest
        and lease.source_ref == receipt.source_ref
        and lease.generation == receipt.generation
        and lease.snapshot_refs == receipt.complete_snapshot_refs
        and snapshot.snapshot_ref in lease.snapshot_refs
        and lease.purpose == "execution_economics"
        and receipt.completed_at <= lease.issued_at
        and lease.issued_at < lease.expires_at
    )


def _is_current_query_session(issuer: object) -> bool:
    try:
        return validate_query_session(
            issuer._client,
            issuer._session_binding,
            now_monotonic=time.monotonic(),
        ) is True
    except Exception:
        return False


def _is_query_facade(value: object) -> bool:
    return all(
        callable(getattr(value, name, None))
        for name in (
            "refresh",
            "snapshot",
            "acquire_snapshot_lease",
            "validate_lease",
        )
    )


def _field(row: object, name: str) -> object:
    try:
        if isinstance(row, dict):
            return row.get(name)
        return getattr(row, name)
    except Exception:
        return None


def _native_text(value: object) -> str:
    if type(value) is bytes:
        value = value.decode("utf-8", errors="strict")
    if type(value) is not str:
        raise ValueError("native text is unavailable")
    value = value.strip()
    if not value or len(value) > 128:
        raise ValueError("native text is invalid")
    return value


def _valid_text(value: object) -> bool:
    return type(value) is str and bool(value) and value == value.strip() and len(value) <= 128


def _nonnegative_decimal(value: object) -> Decimal:
    parsed = _decimal(value)
    if parsed < 0:
        raise ValueError("negative native amount")
    return parsed


def _positive_decimal(value: object) -> Decimal:
    parsed = _decimal(value)
    if parsed <= 0:
        raise ValueError("non-positive native amount")
    return parsed


def _decimal(value: object) -> Decimal:
    if value is None or type(value) is bool:
        raise ValueError("invalid native decimal")
    if type(value) not in (str, int, float, Decimal):
        raise ValueError("invalid native decimal")
    parsed = value if type(value) is Decimal else Decimal(str(value))
    if not parsed.is_finite() or len(format(parsed, "f")) > 128:
        raise ValueError("invalid native decimal")
    return parsed


def _parse_utc(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("UTC timestamp is unavailable")
    parsed = datetime.fromisoformat(value)
    if not _is_utc(parsed):
        raise ValueError("UTC timestamp is invalid")
    return parsed


def _is_utc(value: object) -> bool:
    return (
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )

