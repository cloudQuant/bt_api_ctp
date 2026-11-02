"""Source-bound CTP position evidence over the selected neutral query facade.

This module is the CTP-side issuer for order preflight positions. It never
accepts caller-supplied position rows. SDK composition injects the query facade
selected for the same exact feed and environment; the CTP issuer binds that
facade to its already sealed runtime/session and returns evidence only after a
complete scoped positions snapshot and a current provider lease are verified.

CTP's neutral position projection does not preserve close-today/yesterday
buckets and represents ``net`` rows with unsigned quantities. Those facts stay
unavailable here; no side, sign, or bucket is inferred from a symbol or order.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Optional

from bt_api_base.account_protocol import AccountPosition, AccountResourceKey
from bt_api_base.execution_correlation_protocol import SelectedExecutionRoute
from bt_api_base.execution_rules_protocol import (
    ExecutionRuleRequest,
    InstrumentExecutionRulesSnapshot,
)
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_base.order_position_evidence_protocol import (
    OrderPositionEvidence,
    OrderPositionEvidenceProviderPort,
    OrderPositionEvidenceProviderRegistryPort,
    OrderPositionEvidenceUnavailable,
    OrderPositionQuantities,
    ProviderQueryFacadePort,
)
from bt_api_base.query_protocol import (
    MAX_QUERY_RECORDS,
    QueryBatchReceipt,
    QueryCompleteness,
    QueryRequest,
    QuerySnapshot,
    QuerySnapshotLease,
)

from bt_api_ctp.execution_economics_registration import (
    CtpExecutionEconomicsOwner,
    CtpExecutionRulesSnapshotEvidence,
    CtpExecutionRulesSnapshotEvidenceIssuer,
)
from bt_api_ctp.execution_rules_source_issuer import (
    CtpExecutionRulesSnapshotSourceDependenciesV1,
    CtpNativeExecutionRulesSnapshotEvidenceIssuer,
)
from bt_api_ctp.execution_economics_source_issuer import (
    CtpNativeExecutionEconomicsSourceMixin,
)
from bt_api_ctp.order_provider import CtpOrderProviderRuntimeContext
from bt_api_ctp.registered_sim_session import (
    CtpQuerySessionBinding,
    validate_query_session,
)

__all__ = (
    "CtpNativeOrderPositionEvidenceIssuer",
    "create_ctp_order_position_evidence_issuer",
    "register_ctp_order_position_evidence_provider",
)

_CTP_FUTURE_ROUTE = "CTP___FUTURE"
_UTC = timezone.utc
_MAX_ISSUED_POSITION_EVIDENCE = 128
_MAX_POSITION_RECORDS = 2048
_MAX_CACHED_RULES_EVIDENCE = 64
_POSITION_EVIDENCE_TTL_SECONDS = 5.0
_POSITION_QUERY_PURPOSE = "order-position-evidence"
_POSITION_RESOURCE = AccountResourceKey(product_type="future", margin_pool="futures")


@dataclass(frozen=True, repr=False, eq=False)
class _IssuedPositionEvidence:
    evidence: OrderPositionEvidence = field(repr=False, compare=False)
    evidence_digest: str
    instrument: MarketInstrumentKey = field(repr=False, compare=False)
    quantities: OrderPositionQuantities = field(repr=False, compare=False)
    route: SelectedExecutionRoute = field(repr=False, compare=False)
    runtime_context: CtpOrderProviderRuntimeContext = field(repr=False, compare=False)
    query_facade: ProviderQueryFacadePort = field(repr=False, compare=False)
    session_binding: CtpQuerySessionBinding = field(repr=False, compare=False)
    request: QueryRequest = field(repr=False, compare=False)
    receipt: QueryBatchReceipt = field(repr=False, compare=False)
    snapshot: QuerySnapshot = field(repr=False, compare=False)
    lease: QuerySnapshotLease = field(repr=False, compare=False)


class CtpNativeOrderPositionEvidenceIssuer(
    CtpNativeExecutionEconomicsSourceMixin,
    CtpNativeExecutionRulesSnapshotEvidenceIssuer
):
    """Combined CTP rules, partial economics facts, and position issuer.

    Inject this exact instance as the existing CTP runtime's
    ``execution_economics_issuer``. It preserves the existing instrument
    rules authority, proves futures product currency from the current CTP
    Product query, calculates open futures margin from the sealed margin-rate
    row, and adds an optional position-proof capability. CTP commission rows
    do not expose fee currency, so this issuer's economics readiness remains
    false until an exact same-provider fee-currency authority is available.
    The same route-bound QueryFacade is bound later by SDK composition;
    account reads are not used to guess that missing fee currency.
    """

    def __init__(self, dependencies: CtpExecutionRulesSnapshotSourceDependenciesV1) -> None:
        super().__init__(dependencies)
        self._initialize_economics_source()
        self.__position_secret = secrets.token_bytes(32)
        self.__position_lock = threading.RLock()
        self.__position_route: SelectedExecutionRoute | None = None
        self.__position_context: CtpOrderProviderRuntimeContext | None = None
        self.__position_query_facade: ProviderQueryFacadePort | None = None
        self.__position_issued: OrderedDict[str, _IssuedPositionEvidence] = OrderedDict()
        self.__rules_evidence_lock = threading.RLock()
        self.__rules_evidence: OrderedDict[
            tuple[int, str],
            tuple[MarketInstrumentKey, CtpExecutionRulesSnapshotEvidence],
        ] = OrderedDict()

    def is_bound_to(self, *, route: object, runtime_context: object) -> bool:
        """Validate both the trusted pre-context and the final sealed context.

        The existing CTP dependency resolver checks its source pair before it
        creates ``CtpOrderProviderRuntimeContext``. That preliminary context is
        SDK-owned and carries the exact selected feed/client/owner thread, but
        the CTP package intentionally does not import the parent SDK's context
        class. This structural pre-context check is only an injection
        prerequisite; every final provider operation still delegates to the
        existing CTP issuer validator, which requires the sealed CTP runtime
        context and runtime-feed binding.
        """

        if type(runtime_context) is CtpOrderProviderRuntimeContext:
            return super().is_bound_to(route=route, runtime_context=runtime_context)
        try:
            selected_feed = getattr(runtime_context, "selected_feed", None)
            selected_client = getattr(runtime_context, "trader_client", None)
            execution_scope = getattr(runtime_context, "execution_scope", None)
            owner_thread = getattr(runtime_context, "owner_thread", None)
            invoker = getattr(runtime_context, "owner_thread_invoker", None)
            return bool(
                type(route) is SelectedExecutionRoute
                and route.exchange_name == _CTP_FUTURE_ROUTE
                and route.environment == "demo"
                and route.client is self._client
                and route.execution_context is selected_feed
                and runtime_context is not None
                and selected_feed is self._request_feed
                and selected_client is self._client
                and getattr(selected_feed, "_connected", None) is True
                and getattr(execution_scope, "environment", None) == "simulation"
                and owner_thread is threading.current_thread()
                and callable(getattr(invoker, "call_on_owner_thread", None))
                and self._session_binding.environment == route.environment
                and validate_query_session(
                    self._client,
                    self._session_binding,
                    now_monotonic=time.monotonic(),
                )
                is True
            )
        except Exception:
            return False

    def bind_position_query_facade(
        self,
        route: SelectedExecutionRoute,
        runtime_context: CtpOrderProviderRuntimeContext,
        query_facade: ProviderQueryFacadePort,
    ) -> bool:
        """Bind the composer-created query facade to this issuer exactly once."""

        if (
            type(route) is not SelectedExecutionRoute
            or type(runtime_context) is not CtpOrderProviderRuntimeContext
            or route.execution_context is not runtime_context
            or route.exchange_name != _CTP_FUTURE_ROUTE
            or route.client is not self._request_feed
            or route.environment != self._session_binding.environment
            or not _is_query_facade(query_facade)
            or not self.is_bound_to(route=route, runtime_context=runtime_context)
        ):
            return False
        owner = runtime_context.execution_economics_owner
        if (
            type(owner) is not CtpExecutionEconomicsOwner
            or owner._issuer is not self
            or owner._bind_selected_route(route, runtime_context) is not True
        ):
            return False

        def bind_on_owner_thread() -> bool:
            with self.__position_lock:
                if self.__position_query_facade is not None and not (
                    _same_route(self.__position_route, route)
                    and self.__position_context is runtime_context
                    and self.__position_query_facade is query_facade
                ):
                    return False
                if (
                    self._bind_economics_query_facade(route, runtime_context, query_facade)
                    is not True
                ):
                    return False
                if self.__position_query_facade is None:
                    self.__position_route = route
                    self.__position_context = runtime_context
                    self.__position_query_facade = query_facade
                    return True
                return True

        result = owner._call_on_owner_thread(route, runtime_context, bind_on_owner_thread)
        return type(result) is bool and result is True

    def collect_instrument_rules_snapshot(
        self,
        instrument: MarketInstrumentKey,
        *,
        hedge_class: str,
        route: object,
        runtime_context: object,
    ) -> Optional[CtpExecutionRulesSnapshotEvidence]:
        """Reuse a still-current rules receipt across the Store's two phases.

        Store first obtains and validates the instrument/rules snapshot, then
        obtains short-lived position evidence, then submits the exact preflight
        request. The underlying CTP certificate must therefore be revalidated
        from its sealed original QueryResults rather than issuing the seven
        rate-limited queries a second time after the position lease starts.
        A stale cache entry is discarded and this call fails closed; it never
        retries the collection in the same decision.
        """

        if (
            type(instrument) is not MarketInstrumentKey
            or type(hedge_class) is not str
            or type(route) is not SelectedExecutionRoute
            or type(runtime_context) is not CtpOrderProviderRuntimeContext
            or not self.is_bound_to(route=route, runtime_context=runtime_context)
        ):
            return None
        key = (id(instrument), hedge_class)
        with self.__rules_evidence_lock:
            cached = self.__rules_evidence.get(key)
            if cached is not None:
                if cached[0] is not instrument:
                    del self.__rules_evidence[key]
                    return None
                valid = self.validate_instrument_rules_snapshot(
                    instrument,
                    cached[1],
                    hedge_class=hedge_class,
                    route=route,
                    runtime_context=runtime_context,
                    now_utc=datetime.now(_UTC),
                )
                if type(valid) is bool and valid is True:
                    enriched = self._economics_enrich_rules_snapshot(
                        instrument,
                        hedge_class,
                        route,
                        runtime_context,
                        cached[1],
                        deadline_monotonic=self._economics_native_query_deadline(
                            runtime_context,
                            request_count=1,
                        )
                        or time.monotonic(),
                    )
                    if enriched is not None:
                        self.__rules_evidence[key] = (instrument, enriched)
                        self.__rules_evidence.move_to_end(key)
                        return enriched
                    if self._is_enriched_rules_snapshot(cached[1]):
                        del self.__rules_evidence[key]
                        return None
                    self.__rules_evidence.move_to_end(key)
                    return cached[1]
                del self.__rules_evidence[key]
                return None

            # The Product lookup is an optional price-currency enrichment,
            # not part of the existing seven-query rules contract.  Check its
            # combined budget up front, but do not let a missing eighth-query
            # budget suppress a rules snapshot that the base issuer can still
            # collect within its own contract.
            enrichment_deadline = self._economics_native_query_deadline(
                runtime_context,
                request_count=8,
            )
            evidence = super().collect_instrument_rules_snapshot(
                instrument,
                hedge_class=hedge_class,
                route=route,
                runtime_context=runtime_context,
            )
            if (
                type(evidence) is not CtpExecutionRulesSnapshotEvidence
                or evidence.facts.instrument is not instrument
                or evidence.facts.hedge_class != hedge_class
            ):
                return None
            if enrichment_deadline is not None:
                enriched = self._economics_enrich_rules_snapshot(
                    instrument,
                    hedge_class,
                    route,
                    runtime_context,
                    evidence,
                    deadline_monotonic=enrichment_deadline,
                )
                if enriched is not None:
                    evidence = enriched
            now = datetime.now(_UTC)
            for existing_key, existing in tuple(self.__rules_evidence.items()):
                expires = existing[1].facts.expires_at_utc
                if expires is None or expires <= now:
                    self.__rules_evidence.pop(existing_key, None)
            if len(self.__rules_evidence) >= _MAX_CACHED_RULES_EVIDENCE:
                return None
            self.__rules_evidence[key] = (instrument, evidence)
            self.__rules_evidence.move_to_end(key)
            return evidence

    def snapshot(
        self,
        instrument: MarketInstrumentKey,
        *,
        timeout: float = 5.0,
    ) -> OrderPositionEvidence:
        """Issue complete position evidence or raise a finite unavailable code."""

        selected_timeout = _timeout(timeout)
        if type(instrument) is not MarketInstrumentKey:
            raise OrderPositionEvidenceUnavailable("POSITION_EVIDENCE_SCOPE_UNAVAILABLE")
        pair = self.__position_binding()
        if pair is None:
            raise OrderPositionEvidenceUnavailable("POSITION_EVIDENCE_PROVIDER_UNAVAILABLE")
        route, runtime_context, query_facade = pair
        if not _instrument_matches_route(instrument, route):
            raise OrderPositionEvidenceUnavailable("POSITION_EVIDENCE_SCOPE_UNAVAILABLE")
        owner = runtime_context.execution_economics_owner
        if (
            type(owner) is not CtpExecutionEconomicsOwner
            or owner._issuer is not self
            or owner._bind_selected_route(route, runtime_context) is not True
        ):
            raise OrderPositionEvidenceUnavailable("POSITION_EVIDENCE_ROUTE_UNAVAILABLE")
        result = owner._call_on_owner_thread(
            route,
            runtime_context,
            lambda: self._snapshot_on_owner_thread(
                instrument,
                route,
                runtime_context,
                query_facade,
                selected_timeout,
            ),
        )
        if type(result) is not OrderPositionEvidence:
            raise OrderPositionEvidenceUnavailable("POSITION_EVIDENCE_SOURCE_UNAVAILABLE")
        return result

    def validate(self, evidence: OrderPositionEvidence, *, now: datetime) -> bool:
        """Validate exact issuer identity and the current neutral query lease."""

        if type(evidence) is not OrderPositionEvidence or not _is_utc(now):
            return False
        pair = self.__position_binding()
        if pair is None:
            return False
        route, runtime_context, _query_facade = pair
        owner = runtime_context.execution_economics_owner
        if (
            type(owner) is not CtpExecutionEconomicsOwner
            or owner._issuer is not self
            or owner._bind_selected_route(route, runtime_context) is not True
        ):
            return False
        result = owner._call_on_owner_thread(
            route,
            runtime_context,
            lambda: self._validate_on_owner_thread(evidence, route, runtime_context),
        )
        return type(result) is bool and result is True

    def validate_order_rules_position_evidence(
        self,
        request: ExecutionRuleRequest,
        *,
        rules_snapshot: InstrumentExecutionRulesSnapshot,
        route: SelectedExecutionRoute,
        runtime_context: CtpOrderProviderRuntimeContext,
    ) -> bool:
        """Authorize only the exact quantities from this issuer's live lease.

        ``CtpOrderRulesProvider`` calls this method on the trusted CTP owner
        thread. It compares every provided total and day bucket to the issued
        neutral evidence before the existing rules evaluator can consume it.
        """

        if (
            threading.get_ident() != self._owner_thread_id
            or type(request) is not ExecutionRuleRequest
            or type(rules_snapshot) is not InstrumentExecutionRulesSnapshot
            or type(route) is not SelectedExecutionRoute
            or type(runtime_context) is not CtpOrderProviderRuntimeContext
            or not self.is_bound_to(route=route, runtime_context=runtime_context)
            or route.exchange_name != _CTP_FUTURE_ROUTE
            or route.execution_context is not runtime_context
            or rules_snapshot.facts.instrument != request.instrument
            or rules_snapshot.facts.status != "available"
            or rules_snapshot.facts.session_generation != self._session_binding.generation
            or request.session_generation != self._session_binding.generation
            or request.position_evidence_ref is None
            or request.position_evidence_generation != self._session_binding.generation
            or request.position_observed_at_utc is None
            or request.position_expires_at_utc is None
        ):
            return False
        with self.__position_lock:
            issued = self.__position_issued.get(request.position_evidence_ref)
        if issued is None or not _same_route(issued.route, route):
            return False
        if (
            issued.runtime_context is not runtime_context
            or issued.evidence.session_generation != request.position_evidence_generation
            or issued.evidence.observed_at_utc != request.position_observed_at_utc
            or issued.evidence.expires_at_utc != request.position_expires_at_utc
            or not _request_quantities_match(request, issued.quantities)
        ):
            return False
        return self._validate_issued_on_owner_thread(issued)

    def _snapshot_on_owner_thread(
        self,
        instrument: MarketInstrumentKey,
        route: SelectedExecutionRoute,
        runtime_context: CtpOrderProviderRuntimeContext,
        query_facade: ProviderQueryFacadePort,
        timeout: float,
    ) -> OrderPositionEvidence | None:
        bound = self.__position_binding()
        if (
            threading.get_ident() != self._owner_thread_id
            or not self.is_bound_to(route=route, runtime_context=runtime_context)
            or bound is None
            or not _same_route(bound[0], route)
            or bound[1] is not runtime_context
            or bound[2] is not query_facade
            or not validate_query_session(
                self._client,
                self._session_binding,
                now_monotonic=time.monotonic(),
            )
        ):
            return None
        owner_timeout = runtime_context.owner_call_timeout_seconds
        if (
            type(owner_timeout) not in (int, float)
            or isinstance(owner_timeout, bool)
            or float(owner_timeout) < 0.1
        ):
            return None
        query_budget = min(timeout, float(owner_timeout) * 0.75)
        if query_budget < 0.05:
            return None
        started = datetime.now(_UTC)
        deadline = started + timedelta(seconds=query_budget)
        request = QueryRequest(
            request_ref="position-request-" + uuid.uuid4().hex,
            family="positions",
            filters={},
            required=True,
            require_complete_scope=True,
        )
        try:
            receipt = query_facade.refresh((request,), deadline=deadline)
        except Exception:
            return None
        current = datetime.now(_UTC)
        snapshot = _complete_position_snapshot(receipt, request, self._session_binding)
        if (
            snapshot is None
            or query_facade.snapshot(receipt, request.request_ref) is not snapshot
            or current > deadline
            or snapshot.observed_at is None
            or snapshot.observed_at > current
            or not validate_query_session(
                self._client,
                self._session_binding,
                now_monotonic=time.monotonic(),
            )
        ):
            return None
        projected = _project_position_quantities(snapshot, instrument)
        if projected is None:
            return None
        quantities, instrument_record_count = projected
        # Bound the lease to the age of the source snapshot. A provider must
        # not turn an old cached position result into a fresh five-second
        # decision window merely because lease acquisition happened later.
        expiry_limit = snapshot.observed_at + timedelta(
            seconds=_POSITION_EVIDENCE_TTL_SECONDS
        )
        if datetime.now(_UTC) >= expiry_limit:
            return None
        try:
            lease = query_facade.acquire_snapshot_lease(
                receipt,
                purpose=_POSITION_QUERY_PURPOSE,
                expires_at=expiry_limit,
            )
        except Exception:
            return None
        leased_at = datetime.now(_UTC)
        if (
            not _lease_matches_snapshot(lease, receipt, snapshot)
            or lease.expires_at > expiry_limit
            or not leased_at < lease.expires_at
            or query_facade.validate_lease(lease, receipt, now=leased_at) is not True
            or not validate_query_session(
                self._client,
                self._session_binding,
                now_monotonic=time.monotonic(),
            )
        ):
            return None

        evidence = OrderPositionEvidence(
            quantities=quantities,
            evidence_ref=self._opaque("evidence", uuid.uuid4().hex),
            issuer_ref=self._opaque("issuer", "position-evidence-issuer"),
            source_ref=self._opaque("source", receipt.source_ref),
            session_generation=self._session_binding.generation,
            receipt_ref=self._opaque("receipt", receipt.receipt_digest),
            snapshot_ref=self._opaque("snapshot", snapshot.snapshot_ref),
            lease_ref=self._opaque("lease", lease.lease_ref),
            records_digest=self._opaque("records", snapshot.records_digest),
            observed_at_utc=snapshot.observed_at,
            expires_at_utc=lease.expires_at,
            snapshot_record_count=snapshot.record_count,
            instrument_record_count=instrument_record_count,
            complete=True,
            scope_complete=True,
            empty_result_proven=instrument_record_count == 0,
        )
        try:
            digest = _evidence_digest(evidence)
        except Exception:
            return None
        issued = _IssuedPositionEvidence(
            evidence=evidence,
            evidence_digest=digest,
            instrument=instrument,
            quantities=quantities,
            route=route,
            runtime_context=runtime_context,
            query_facade=query_facade,
            session_binding=self._session_binding,
            request=request,
            receipt=receipt,
            snapshot=snapshot,
            lease=lease,
        )
        with self.__position_lock:
            self._purge_position_evidence(datetime.now(_UTC))
            if len(self.__position_issued) >= _MAX_ISSUED_POSITION_EVIDENCE:
                return None
            self.__position_issued[evidence.evidence_ref] = issued
            self.__position_issued.move_to_end(evidence.evidence_ref)
        # The post-insertion validation prevents a lease/session change during
        # evidence construction from becoming a positive result.
        if not self._validate_issued_on_owner_thread(issued):
            with self.__position_lock:
                if self.__position_issued.get(evidence.evidence_ref) is issued:
                    del self.__position_issued[evidence.evidence_ref]
            return None
        return evidence

    def _validate_on_owner_thread(
        self,
        evidence: OrderPositionEvidence,
        route: SelectedExecutionRoute,
        runtime_context: CtpOrderProviderRuntimeContext,
    ) -> bool:
        with self.__position_lock:
            issued = self.__position_issued.get(evidence.evidence_ref)
        return bool(
            issued is not None
            and issued.evidence is evidence
            and _same_route(issued.route, route)
            and issued.runtime_context is runtime_context
            and self._validate_issued_on_owner_thread(issued)
        )

    def _validate_issued_on_owner_thread(self, issued: _IssuedPositionEvidence) -> bool:
        now = datetime.now(_UTC)
        evidence = issued.evidence
        bound = self.__position_binding()
        if (
            threading.get_ident() != self._owner_thread_id
            or bound is None
            or not _same_route(bound[0], issued.route)
            or bound[1] is not issued.runtime_context
            or bound[2] is not issued.query_facade
            or not self.is_bound_to(route=issued.route, runtime_context=issued.runtime_context)
            or issued.session_binding is not self._session_binding
            or not _is_current_evidence(evidence, now)
            or not validate_query_session(
                self._client,
                self._session_binding,
                now_monotonic=time.monotonic(),
            )
        ):
            return False
        with self.__position_lock:
            if self.__position_issued.get(evidence.evidence_ref) is not issued:
                return False
        try:
            OrderPositionEvidence.__post_init__(evidence)
            if (
                _evidence_digest(evidence) != issued.evidence_digest
                or evidence.quantities != issued.quantities
                or evidence.quantities.instrument != issued.instrument
                or evidence.session_generation != self._session_binding.generation
                or issued.receipt.source_ref != self._session_binding.source_ref
                or issued.receipt.generation != self._session_binding.generation
                or issued.snapshot.source_ref != issued.receipt.source_ref
                or issued.snapshot.generation != issued.receipt.generation
                or self._opaque("records", issued.snapshot.records_digest)
                != evidence.records_digest
                or issued.snapshot.record_count != evidence.snapshot_record_count
                or issued.lease.source_ref != issued.receipt.source_ref
                or issued.lease.generation != issued.receipt.generation
                or issued.lease.expires_at != evidence.expires_at_utc
                or issued.snapshot.observed_at is None
                or issued.lease.expires_at
                > issued.snapshot.observed_at
                + timedelta(seconds=_POSITION_EVIDENCE_TTL_SECONDS)
            ):
                return False
            snapshot = _complete_position_snapshot(
                issued.receipt,
                issued.request,
                self._session_binding,
            )
            projected = (
                None
                if snapshot is None or snapshot is not issued.snapshot
                else _project_position_quantities(snapshot, issued.instrument)
            )
            if projected is None or projected[0] != issued.quantities:
                return False
            if projected[1] != evidence.instrument_record_count:
                return False
            if issued.query_facade is not self.__position_query_facade:
                return False
            if issued.query_facade.snapshot(
                issued.receipt,
                issued.request.request_ref,
            ) is not issued.snapshot:
                return False
            if issued.query_facade.validate_lease(
                issued.lease,
                issued.receipt,
                now=now,
            ) is not True:
                return False
        except Exception:
            return False
        final_now = datetime.now(_UTC)
        return (
            _is_current_evidence(evidence, final_now)
            and validate_query_session(
                self._client,
                self._session_binding,
                now_monotonic=time.monotonic(),
            )
        )

    def _position_binding(
        self,
    ) -> tuple[
        SelectedExecutionRoute,
        CtpOrderProviderRuntimeContext,
        ProviderQueryFacadePort,
    ] | None:
        with self.__position_lock:
            route = self.__position_route
            runtime_context = self.__position_context
            query_facade = self.__position_query_facade
        if (
            type(route) is not SelectedExecutionRoute
            or type(runtime_context) is not CtpOrderProviderRuntimeContext
            or not _is_query_facade(query_facade)
            or route.execution_context is not runtime_context
            or route.client is not self._request_feed
        ):
            return None
        return route, runtime_context, query_facade

    def _opaque(self, label: str, value: str) -> str:
        payload = label.encode("ascii") + b"\x00" + value.encode("utf-8", errors="strict")
        digest = hmac.new(self.__position_secret, payload, sha256).hexdigest()
        return "opaque-" + digest

    def _purge_position_evidence(self, now: datetime) -> None:
        for ref, issued in tuple(self.__position_issued.items()):
            if issued.evidence.expires_at_utc <= now:
                self.__position_issued.pop(ref, None)


def create_ctp_order_position_evidence_issuer(
    dependencies: CtpExecutionRulesSnapshotSourceDependenciesV1,
) -> CtpNativeOrderPositionEvidenceIssuer:
    """Construct the combined issuer for injection into a trusted CTP runtime."""

    return CtpNativeOrderPositionEvidenceIssuer(dependencies)


def register_ctp_order_position_evidence_provider(
    registry: OrderPositionEvidenceProviderRegistryPort,
) -> None:
    """Register one exact CTP route factory; no client/session is created here."""

    if not isinstance(registry, OrderPositionEvidenceProviderRegistryPort):
        raise TypeError("registry must satisfy OrderPositionEvidenceProviderRegistryPort")
    registry.register(_CTP_FUTURE_ROUTE, _create_ctp_order_position_evidence_provider)


def _create_ctp_order_position_evidence_provider(
    route: SelectedExecutionRoute,
    query_facade: ProviderQueryFacadePort,
) -> Optional[OrderPositionEvidenceProviderPort]:
    if (
        type(route) is not SelectedExecutionRoute
        or route.exchange_name != _CTP_FUTURE_ROUTE
        or not _is_query_facade(query_facade)
    ):
        return None
    context = route.execution_context
    if type(context) is not CtpOrderProviderRuntimeContext:
        return None
    issuer = context.execution_economics_issuer
    owner = context.execution_economics_owner
    if (
        type(issuer) is not CtpNativeOrderPositionEvidenceIssuer
        or type(owner) is not CtpExecutionEconomicsOwner
        or owner._issuer is not issuer
        or not isinstance(issuer, CtpExecutionRulesSnapshotEvidenceIssuer)
        or not issuer.bind_position_query_facade(route, context, query_facade)
    ):
        return None
    return issuer


def _complete_position_snapshot(
    receipt: object,
    request: QueryRequest,
    session_binding: CtpQuerySessionBinding,
) -> QuerySnapshot | None:
    try:
        QueryRequest.__post_init__(request)
        if type(receipt) is QueryBatchReceipt:
            QueryBatchReceipt.__post_init__(receipt)
    except Exception:
        return None
    if (
        type(receipt) is not QueryBatchReceipt
        or receipt.requests != (request,)
        or len(receipt.requests) != 1
        or receipt.requests[0] is not request
        or receipt.blockers
        or receipt.complete is not True
        or receipt.source_ref != session_binding.source_ref
        or receipt.generation != session_binding.generation
        or len(receipt.snapshots) != 1
    ):
        return None
    snapshot = receipt.snapshots[0]
    try:
        QuerySnapshot.__post_init__(snapshot)
    except Exception:
        return None
    if (
        type(snapshot) is not QuerySnapshot
        or snapshot.request is not request
        or snapshot.request.family != "positions"
        or snapshot.request.filters
        or snapshot.request.required is not True
        or snapshot.request.require_complete_scope is not True
        or snapshot.source_ref != receipt.source_ref
        or snapshot.generation != receipt.generation
        or snapshot.completeness is not QueryCompleteness.COMPLETE
        or snapshot.scope_complete is not True
        or snapshot.is_complete is not True
        or snapshot.blockers
        or snapshot.continuation_ref is not None
        or snapshot.snapshot_ref is None
        or snapshot.observed_at is None
        or snapshot.started_at is None
        or snapshot.completed_at is None
        or snapshot.completed_at > receipt.completed_at
        or snapshot.record_count > MAX_QUERY_RECORDS
    ):
        return None
    if receipt.complete_snapshot_refs != (snapshot.snapshot_ref,):
        return None
    return snapshot


def _lease_matches_snapshot(
    lease: object,
    receipt: QueryBatchReceipt,
    snapshot: QuerySnapshot,
) -> bool:
    if type(lease) is not QuerySnapshotLease:
        return False
    try:
        QuerySnapshotLease.__post_init__(lease)
    except Exception:
        return False
    return bool(
        lease.receipt_digest == receipt.receipt_digest
        and lease.source_ref == receipt.source_ref
        and lease.generation == receipt.generation
        and lease.snapshot_refs == (snapshot.snapshot_ref,)
        and lease.purpose == _POSITION_QUERY_PURPOSE
        and lease.issued_at >= receipt.completed_at
        and lease.expires_at > lease.issued_at
    )


def _project_position_quantities(
    snapshot: QuerySnapshot,
    requested_instrument: MarketInstrumentKey,
) -> tuple[OrderPositionQuantities, int] | None:
    if (
        type(snapshot) is not QuerySnapshot
        or type(requested_instrument) is not MarketInstrumentKey
        or len(snapshot.records) > _MAX_POSITION_RECORDS
    ):
        return None
    seen: set[tuple[MarketInstrumentKey, str]] = set()
    long_total = Decimal(0)
    short_total = Decimal(0)
    instrument_record_count = 0
    target_had_net = False
    try:
        for row in snapshot.records:
            position = AccountPosition.from_dict(row)
            if position.to_dict() != row:
                return None
            if (
                position.resource != _POSITION_RESOURCE
                or position.instrument.provider != "ctp"
                or position.instrument.environment != requested_instrument.environment
                or position.instrument.product_type != "future"
                or position.qty_unit != "contract"
                or position.price_currency is not None
            ):
                return None
            row_time = _parse_utc(position.observed_at_utc)
            if row_time > snapshot.observed_at:
                return None
            quantity = Decimal(position.quantity)
            if (
                not quantity.is_finite()
                or quantity < 0
                or quantity != quantity.to_integral_value()
            ):
                return None
            key = (position.instrument, position.side)
            if key in seen:
                return None
            seen.add(key)
            if position.instrument != requested_instrument:
                continue
            instrument_record_count += 1
            if position.side == "long":
                long_total += quantity
            elif position.side == "short":
                short_total += quantity
            elif position.side == "net":
                # CTP's published AccountPosition quantity is unsigned. The
                # sign of net exposure cannot be reconstructed from it.
                target_had_net = True
            else:
                return None
    except (InvalidOperation, TypeError, ValueError):
        return None
    if target_had_net:
        return None
    try:
        quantities = OrderPositionQuantities(
            instrument=requested_instrument,
            qty_unit="contract",
            net_position_size=_decimal_text(long_total - short_total),
            long_position_size=_decimal_text(long_total),
            short_position_size=_decimal_text(short_total),
            long_close_today_size=None,
            long_close_yesterday_size=None,
            short_close_today_size=None,
            short_close_yesterday_size=None,
        )
    except (TypeError, ValueError):
        return None
    return quantities, instrument_record_count


def _request_quantities_match(
    request: ExecutionRuleRequest,
    quantities: OrderPositionQuantities,
) -> bool:
    if request.instrument != quantities.instrument:
        return False
    for name in (
        "net_position_size",
        "long_position_size",
        "short_position_size",
        "long_close_today_size",
        "long_close_yesterday_size",
        "short_close_today_size",
        "short_close_yesterday_size",
    ):
        requested = getattr(request, name)
        evidence = getattr(quantities, name)
        if requested is None or evidence is None:
            if requested is not evidence:
                return False
            continue
        try:
            if Decimal(requested) != Decimal(evidence):
                return False
        except (InvalidOperation, TypeError, ValueError):
            return False
    return True


def _evidence_digest(evidence: OrderPositionEvidence) -> str:
    """Hash all SDK-visible fields to detect mutation of a frozen DTO."""

    quantities = evidence.quantities
    instrument = quantities.instrument
    values = (
        instrument.provider,
        instrument.venue,
        instrument.environment,
        instrument.product_type,
        instrument.instrument_id,
        instrument.native_symbol,
        quantities.qty_unit,
        quantities.net_position_size,
        quantities.long_position_size,
        quantities.short_position_size,
        quantities.long_close_today_size,
        quantities.long_close_yesterday_size,
        quantities.short_close_today_size,
        quantities.short_close_yesterday_size,
        evidence.evidence_ref,
        evidence.issuer_ref,
        evidence.source_ref,
        evidence.session_generation,
        evidence.receipt_ref,
        evidence.snapshot_ref,
        evidence.lease_ref,
        evidence.records_digest,
        evidence.observed_at_utc.isoformat(),
        evidence.expires_at_utc.isoformat(),
        evidence.snapshot_record_count,
        evidence.instrument_record_count,
        evidence.complete,
        evidence.scope_complete,
        evidence.empty_result_proven,
    )
    payload = "\x1f".join("<none>" if value is None else str(value) for value in values)
    return sha256(payload.encode("utf-8", errors="strict")).hexdigest()


def _is_query_facade(value: object) -> bool:
    return bool(
        value is not None
        and all(
            callable(getattr(value, name, None))
            for name in ("refresh", "snapshot", "acquire_snapshot_lease", "validate_lease")
        )
    )


def _instrument_matches_route(
    instrument: MarketInstrumentKey,
    route: SelectedExecutionRoute,
) -> bool:
    return bool(
        type(instrument) is MarketInstrumentKey
        and type(route) is SelectedExecutionRoute
        and route.exchange_name == _CTP_FUTURE_ROUTE
        and route.environment == "demo"
        and instrument.provider == "ctp"
        and instrument.environment == route.environment
        and instrument.product_type == "future"
        and instrument.instrument_id
        and instrument.native_symbol
    )


def _same_route(
    left: SelectedExecutionRoute | None,
    right: SelectedExecutionRoute,
) -> bool:
    return bool(
        type(left) is SelectedExecutionRoute
        and type(right) is SelectedExecutionRoute
        and left.exchange_name == right.exchange_name
        and left.client is right.client
        and left.environment == right.environment
        and left.execution_context is right.execution_context
    )


def _timeout(value: object) -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        raise OrderPositionEvidenceUnavailable("POSITION_EVIDENCE_TIMEOUT")
    try:
        timeout = float(value)
    except (OverflowError, ValueError):
        raise OrderPositionEvidenceUnavailable("POSITION_EVIDENCE_TIMEOUT") from None
    if not 0.01 <= timeout <= 30.0:
        raise OrderPositionEvidenceUnavailable("POSITION_EVIDENCE_TIMEOUT")
    return timeout


def _is_utc(value: object) -> bool:
    if type(value) is not datetime or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() == timedelta(0)
    except Exception:
        return False


def _is_current_evidence(evidence: OrderPositionEvidence, now: datetime) -> bool:
    return bool(
        _is_utc(now)
        and evidence.observed_at_utc <= now < evidence.expires_at_utc
    )


def _parse_utc(value: object) -> datetime:
    if type(value) is not str or not value or not value.endswith("Z"):
        raise ValueError("position row timestamp invalid")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("position row timestamp invalid")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("position quantity invalid")
    if value == 0:
        return "0"
    result = format(value, "f")
    if len(result) > 128:
        raise ValueError("position quantity outside neutral bounds")
    return result


