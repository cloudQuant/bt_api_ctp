"""Route-bound CTP adapters for instrument rules and order economics.

The existing order-economics capability composes an explicitly injected issuer
that owns the exact query service/session binding, margin and currency evidence,
and a current account-balance lease. Instrument-rules snapshots use a separate
optional capability on that same sealed runtime owner; it can retain verified
instrument and raw ByMoney/ByVolume facts when currency remains unavailable.
Both capabilities use the CTP native query certificate as a source join, run
only on the selected owner thread, and fail closed when their own evidence is
missing. A rules snapshot is not an order decision, reservation, or dispatch
permission. Economics decisions do not reserve cash or margin, and neither
capability makes the seven native reads an atomic exchange snapshot. There is
no default issuer.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
from math import isfinite
from time import monotonic
from typing import Any, Optional, Protocol, Tuple, runtime_checkable

from bt_api_base.account_protocol import AccountBalance
from bt_api_base.execution_correlation_protocol import SelectedExecutionRoute
from bt_api_base.execution_economics_protocol import (
    EconomicsRevalidationStage,
    ExecutionEconomicsDecision,
    ExecutionEconomicsPort,
    ExecutionEconomicsProviderRegistryPort,
    ExecutionEconomicsRequest,
)
from bt_api_base.execution_rules_protocol import (
    ExecutionRuleDecision,
    ExecutionRulesSnapshotPort,
    InstrumentExecutionFacts,
    InstrumentExecutionRulesSnapshot,
)
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_base.managed_execution_runtime_protocol import OwnerThreadCallError
from bt_api_base.query_protocol import QueryBatchReceipt, QuerySnapshot, QuerySnapshotLease
from bt_api_ctp.containers.ctp.ctp_native_query_certificate import (
    CtpNativeQueryCertificate,
)

__all__ = [
    "CtpExecutionEconomicsEvidence",
    "CtpExecutionEconomicsEvidenceIssuer",
    "CtpExecutionEconomicsOwner",
    "CtpExecutionRulesSnapshotEvidence",
    "CtpExecutionRulesSnapshotEvidenceIssuer",
    "CtpExecutionRulesSnapshotError",
    "register_ctp_execution_economics_provider",
]

_CTP_FUTURE_ROUTE = "CTP___FUTURE"
_CERTIFICATE_SCHEMA = "ctp_native_query_certificate.v5"
_REQUIRED_NATIVE_QUERIES = (
    "account",
    "positions",
    "orders",
    "trades",
    "instruments",
    "margin_rate",
    "commission_rate",
)
_RULES_HEDGE_CLASSES = frozenset({"speculation", "arbitrage", "hedge"})
_REQUIRED_CTP_FEE_ROLES = frozenset({"open", "close", "close_today"})
_MAX_RETAINED_DECISIONS = 4096
_MAX_RETAINED_RULE_SNAPSHOTS = 4096
_DECIMAL_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z", re.ASCII)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ECONOMICS_SOURCE_REF_PATTERN = re.compile(
    r"ctp-economics:sha256:[0-9a-f]{64}\Z", re.ASCII
)
_ACCOUNT_SCOPE_REF_PATTERN = re.compile(
    r"ctp-account-scope:[0-9a-f]{32}\Z", re.ASCII
)
_ECONOMICS_LEASE_REF_PATTERN = re.compile(
    r"ctp-economics-lease:[0-9a-f]{32}\Z", re.ASCII
)
_QUERY_SNAPSHOT_REF_PATTERN = re.compile(
    r"ctp-query-snapshot:[0-9a-f]{32}\Z", re.ASCII
)
_QUERY_LEASE_REF_PATTERN = re.compile(r"ctp-query-lease:[0-9a-f]{32}\Z", re.ASCII)


class CtpExecutionEconomicsError(ValueError):
    """Finite, value-free error for malformed CTP economics evidence."""

    _CODES = frozenset(
        {
            "evidence_type_invalid",
            "evidence_request_mismatch",
            "request_route_mismatch",
            "rule_decision_invalid",
            "rule_facts_unavailable",
            "fee_facts_unavailable",
            "fee_currency_mismatch",
            "fee_source_mismatch",
            "quantity_unit_unsupported",
            "position_evidence_unavailable",
            "native_certificate_untrusted",
            "native_certificate_scope_mismatch",
            "native_certificate_incomplete",
            "native_query_source_mismatch",
            "account_evidence_untrusted",
            "account_snapshot_scope_mismatch",
            "account_lease_scope_mismatch",
            "account_currency_unavailable",
            "account_amount_invalid",
            "evidence_time_invalid",
            "evidence_stale",
            "evidence_reference_invalid",
            "economics_amount_invalid",
            "evidence_issuer_unavailable",
            "runtime_binding_invalid",
            "owner_thread_unavailable",
            "decision_capacity_exhausted",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in self._CODES else "evidence_type_invalid"
        super().__init__(self.code)


class CtpExecutionRulesSnapshotError(ValueError):
    """Finite failure from the route-bound CTP rules snapshot provider."""

    _CODES = frozenset(
        {
            "rules_snapshot_request_invalid",
            "rules_snapshot_unavailable",
            "rules_snapshot_evidence_invalid",
            "rules_snapshot_capacity_exhausted",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = (
            code
            if type(code) is str and code in self._CODES
            else "rules_snapshot_unavailable"
        )
        super().__init__(self.code)


@dataclass(frozen=True, repr=False)
class CtpExecutionEconomicsEvidence:
    """Issuer-produced facts for one exact rule request and CTP read window.

    ``required_amount_before_fee`` is the provider's verified order-specific
    cash or margin requirement before the rule decision's commission estimate.
    The owner adds that verified fee exactly once. References are neutral
    opaque IDs/digests; native CTP identifiers and account identifiers are not
    fields in this DTO. The certificate is digest-only and explicitly does not
    assert atomicity or execution authority.
    """

    request: ExecutionEconomicsRequest = field(repr=False, compare=False)
    requirement_kind: str
    required_amount_before_fee: str
    available_amount: str
    currency: str
    source_ref: str
    account_balance_source_ref: str
    account_scope_ref: str
    session_generation: str
    observed_at_utc: datetime
    expires_at_utc: datetime
    lease_ref: str
    native_query_certificate: CtpNativeQueryCertificate = field(repr=False, compare=False)
    account_receipt: QueryBatchReceipt = field(repr=False, compare=False)
    account_snapshot: QuerySnapshot = field(repr=False, compare=False)
    account_lease: QuerySnapshotLease = field(repr=False, compare=False)
    evaluated_quantity: str
    quantity_unit: str
    side: str
    position_side: Optional[str]
    offset: str
    limit_price: str
    asset_kind: str
    hedge_flag: str
    hedge_flag_source_ref: str
    margin_rate_source_ref: str
    commission_rate_source_ref: str
    price_currency_source_ref: str
    fee_currency_source_ref: str
    position_evidence_ref: Optional[str] = None
    position_evidence_generation: Optional[str] = None
    position_observed_at_utc: Optional[datetime] = None
    position_expires_at_utc: Optional[datetime] = None

    def __post_init__(self) -> None:
        if type(self.request) is not ExecutionEconomicsRequest:
            raise TypeError("request must be an ExecutionEconomicsRequest")
        if self.requirement_kind not in {"cash", "margin"}:
            raise ValueError("requirement_kind must be cash or margin")
        for name in (
            "required_amount_before_fee",
            "available_amount",
            "evaluated_quantity",
            "limit_price",
        ):
            _parse_amount(getattr(self, name), name)
        for name in (
            "currency",
            "source_ref",
            "account_balance_source_ref",
            "account_scope_ref",
            "session_generation",
            "lease_ref",
            "quantity_unit",
            "side",
            "offset",
            "asset_kind",
            "hedge_flag",
            "hedge_flag_source_ref",
            "margin_rate_source_ref",
            "commission_rate_source_ref",
            "price_currency_source_ref",
            "fee_currency_source_ref",
        ):
            _require_text(getattr(self, name), name)
        if self.position_side is not None:
            _require_text(self.position_side, "position_side")
        if self.position_evidence_ref is not None:
            _require_text(self.position_evidence_ref, "position_evidence_ref")
        if self.position_evidence_generation is not None:
            _require_text(self.position_evidence_generation, "position_evidence_generation")
        for name in ("observed_at_utc", "expires_at_utc"):
            _require_utc(getattr(self, name), name)
        if self.expires_at_utc <= self.observed_at_utc:
            raise ValueError("evidence expiry must follow its observation")
        if (self.position_observed_at_utc is None) != (self.position_expires_at_utc is None):
            raise ValueError("position evidence times must be supplied together")
        if self.position_observed_at_utc is not None:
            _require_utc(self.position_observed_at_utc, "position_observed_at_utc")
            _require_utc(self.position_expires_at_utc, "position_expires_at_utc")
        if type(self.native_query_certificate) is not CtpNativeQueryCertificate:
            raise TypeError("native_query_certificate must be a CTP native query certificate")
        if type(self.account_receipt) is not QueryBatchReceipt:
            raise TypeError("account_receipt must be a QueryBatchReceipt")
        if type(self.account_snapshot) is not QuerySnapshot:
            raise TypeError("account_snapshot must be a QuerySnapshot")
        if type(self.account_lease) is not QuerySnapshotLease:
            raise TypeError("account_lease must be a QuerySnapshotLease")

    def __repr__(self) -> str:
        return "<CTP execution economics evidence>"


@dataclass(frozen=True, repr=False, eq=False)
class CtpExecutionRulesSnapshotEvidence:
    """CTP-private source receipt for one exact instrument/hedge-class snapshot.

    The neutral facts are copied into the base snapshot, while the sealed CTP
    certificate remains reachable only through the opaque provider receipt.
    This evidence is neither an account snapshot nor an order authorization.
    """

    facts: InstrumentExecutionFacts = field(repr=False, compare=False)
    native_query_certificate: CtpNativeQueryCertificate = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.facts) is not InstrumentExecutionFacts:
            raise TypeError("facts must be InstrumentExecutionFacts")
        if self.facts.status != "available":
            raise ValueError("rules snapshot evidence requires available facts")
        if self.facts.hedge_class not in _RULES_HEDGE_CLASSES:
            raise ValueError("rules snapshot evidence requires an exact hedge class")
        if type(self.native_query_certificate) is not CtpNativeQueryCertificate:
            raise TypeError("native_query_certificate must be a CTP native query certificate")

    def __repr__(self) -> str:
        return "<CTP execution-rules snapshot evidence>"


@runtime_checkable
class CtpExecutionEconomicsEvidenceIssuer(Protocol):
    """Trusted CTP source owner injected by SDK runtime composition.

    Implementations privately own the exact native query/session scope and
    ``CtpProviderQueryService``. They collect the seven current source-bound
    CTP queries with ``CtpNativeQueryCertificateBuilder`` plus a leased
    account snapshot, resolve currency/fee/margin/hedge/option formulas, and
    validate the exact rule decision and any close-position evidence against
    those same source rows. All methods are called inside the already-running
    CTP owner-thread mailbox.

    The implementation must return ``None`` if any required unit, currency,
    formula, account obligation, position receipt, query, or lease is missing
    or ambiguous. Revalidation must check the same evidence and current
    session/balance; it cannot replace stale amounts with a new snapshot. An
    exact ``True`` reports evidence validity only, never authorization or a
    reservation.
    """

    def is_bound_to(
        self,
        *,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool: ...

    def collect_evidence(
        self,
        request: ExecutionEconomicsRequest,
        *,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> Optional[CtpExecutionEconomicsEvidence]: ...

    def validate_evidence(
        self,
        request: ExecutionEconomicsRequest,
        evidence: CtpExecutionEconomicsEvidence,
        *,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool: ...

    def revalidate_evidence(
        self,
        request: ExecutionEconomicsRequest,
        decision: ExecutionEconomicsDecision,
        evidence: CtpExecutionEconomicsEvidence,
        *,
        stage: EconomicsRevalidationStage,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool: ...


@runtime_checkable
class CtpExecutionRulesSnapshotEvidenceIssuer(Protocol):
    """Trusted CTP query owner for hedge-scoped instrument/fee snapshots.

    Implementations own the exact selected TraderClient, source/query service,
    session generation, instrument and currency provenance. Collection must
    use the requested neutral ``hedge_class`` to select the corresponding CTP
    native hedge flag, bind instrument and commission-rate rows in a sealed
    ``CtpNativeQueryCertificate``, and return the neutral facts derived from
    those exact source rows. A missing fee currency may leave the raw native
    commission dimensions present while keeping all currency fields unavailable.
    The facts observation time must be at or after the latest certified query
    completion, and their expiry must not exceed the earliest certified query
    expiry. Currency references must be neutral source digests, never native IDs.

    ``validate_instrument_rules_snapshot`` must prove that the same source and
    session generation are still current and the evidence remains usable at
    the supplied trusted UTC time. The methods run only in the existing CTP
    owner-thread mailbox. They issue neither a cash lease nor order authority.
    """

    def is_bound_to(
        self,
        *,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool: ...

    def collect_instrument_rules_snapshot(
        self,
        instrument: MarketInstrumentKey,
        *,
        hedge_class: str,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> Optional[CtpExecutionRulesSnapshotEvidence]: ...

    def validate_instrument_rules_snapshot(
        self,
        instrument: MarketInstrumentKey,
        evidence: CtpExecutionRulesSnapshotEvidence,
        *,
        hedge_class: str,
        route: SelectedExecutionRoute,
        runtime_context: object,
        now_utc: datetime,
    ) -> bool: ...


@dataclass(frozen=True, repr=False)
class _IssuedDecision:
    request: ExecutionEconomicsRequest = field(repr=False, compare=False)
    decision: ExecutionEconomicsDecision = field(repr=False, compare=False)
    evidence: CtpExecutionEconomicsEvidence = field(repr=False, compare=False)


class _CtpExecutionRulesSnapshotReceipt:
    """Opaque token with no attributes that could expose private evidence."""

    __slots__ = ()


@dataclass(frozen=True, repr=False, eq=False)
class _IssuedRulesSnapshot:
    snapshot: InstrumentExecutionRulesSnapshot = field(repr=False, compare=False)
    instrument: MarketInstrumentKey = field(repr=False, compare=False)
    hedge_class: str
    evidence: CtpExecutionRulesSnapshotEvidence = field(repr=False, compare=False)
    receipt: _CtpExecutionRulesSnapshotReceipt = field(repr=False, compare=False)


class CtpExecutionEconomicsOwner:
    """CTP-owned adapter for optional rules and economics issuer capabilities.

    Construct this object in the trusted CTP dependency factory with the exact
    issuer placed in ``CtpOrderProviderRuntimeContext.execution_economics_issuer``.
    The issuer may implement order economics, instrument-rules snapshots, or
    both. Complete order economics additionally requires the issuer's exact
    ``economics_source_ready() is True`` marker, which must be backed by its
    required source evidence; method shape alone is not capability evidence.
    The provider registries bind this same owner to one exact selected route.
    Missing capability, invalid seal, callback timeout, or source uncertainty
    fails closed. No query is made on the caller thread.
    """

    def __init__(self, issuer: object) -> None:
        if not _issuer_shape_is_valid(issuer):
            raise TypeError("issuer must implement a supported CTP evidence capability")
        self._issuer = issuer
        self._supports_economics = _economics_issuer_shape_is_valid(issuer)
        self._supports_rules_snapshots = _rules_snapshot_issuer_shape_is_valid(issuer)
        self._lock = threading.RLock()
        self._route: Optional[SelectedExecutionRoute] = None
        self._runtime_context: Optional[object] = None
        self._issued: OrderedDict[str, _IssuedDecision] = OrderedDict()
        self._issued_rules: OrderedDict[int, _IssuedRulesSnapshot] = OrderedDict()

    def __repr__(self) -> str:
        return "<CTP execution economics owner>"

    def _bind_selected_route(
        self,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool:
        if type(route) is not SelectedExecutionRoute or route.exchange_name != _CTP_FUTURE_ROUTE:
            return False
        if not self._call_on_owner_thread(
            route,
            runtime_context,
            lambda: self._validate_runtime_context_on_owner_thread(route, runtime_context),
        ):
            return False
        with self._lock:
            if self._runtime_context is None:
                self._route = route
                self._runtime_context = runtime_context
                return True
            return _same_route_and_context(
                self._route,
                self._runtime_context,
                route,
                runtime_context,
            )

    def evaluate_order_economics(
        self,
        request: ExecutionEconomicsRequest,
    ) -> ExecutionEconomicsDecision:
        if type(request) is not ExecutionEconomicsRequest:
            raise TypeError("request must be an ExecutionEconomicsRequest")
        if not self._supports_economics:
            return _unavailable_decision(request, "CTP_ECONOMICS_EVIDENCE_UNAVAILABLE")
        route, context = self._bound_pair()
        if route is None or context is None:
            return _unavailable_decision(request, "CTP_ECONOMICS_RUNTIME_UNAVAILABLE")
        try:
            result = self._call_on_owner_thread(
                route,
                context,
                lambda: self._evaluate_on_owner_thread(request, route, context),
            )
        except Exception:
            return _unavailable_decision(request, "CTP_ECONOMICS_EVIDENCE_UNAVAILABLE")
        if type(result) is not ExecutionEconomicsDecision or result.request is not request:
            return _unavailable_decision(request, "CTP_ECONOMICS_EVIDENCE_UNAVAILABLE")
        return result

    def revalidate_order_economics(
        self,
        request: ExecutionEconomicsRequest,
        decision: ExecutionEconomicsDecision,
        *,
        stage: EconomicsRevalidationStage,
    ) -> bool:
        if (
            type(request) is not ExecutionEconomicsRequest
            or type(decision) is not ExecutionEconomicsDecision
            or decision.request is not request
            or type(stage) is not str
            or stage not in {"claim", "dispatch"}
            or decision.status != "accepted"
            or not self._supports_economics
        ):
            return False
        route, context = self._bound_pair()
        if route is None or context is None:
            return False
        try:
            result = self._call_on_owner_thread(
                route,
                context,
                lambda: self._revalidate_on_owner_thread(
                    request,
                    decision,
                    stage,
                    route,
                    context,
                ),
            )
        except Exception:
            return False
        return type(result) is bool and result is True

    def execution_rules_snapshot(
        self,
        instrument: MarketInstrumentKey,
        *,
        hedge_class: str,
    ) -> InstrumentExecutionRulesSnapshot:
        """Issue exact current neutral facts for one instrument and hedge class."""

        if (
            type(instrument) is not MarketInstrumentKey
            or type(hedge_class) is not str
            or hedge_class not in _RULES_HEDGE_CLASSES
        ):
            raise CtpExecutionRulesSnapshotError("rules_snapshot_request_invalid")
        if not self._supports_rules_snapshots:
            raise CtpExecutionRulesSnapshotError("rules_snapshot_unavailable")
        route, context = self._bound_pair()
        if route is None or context is None:
            raise CtpExecutionRulesSnapshotError("rules_snapshot_unavailable")
        result = self._call_on_owner_thread(
            route,
            context,
            lambda: self._execution_rules_snapshot_on_owner_thread(
                instrument,
                hedge_class,
                route,
                context,
            ),
        )
        if type(result) is CtpExecutionRulesSnapshotError:
            raise result
        if (
            type(result) is not InstrumentExecutionRulesSnapshot
            or result.facts.instrument is not instrument
            or result.facts.hedge_class != hedge_class
        ):
            raise CtpExecutionRulesSnapshotError("rules_snapshot_unavailable")
        return result

    def validate_execution_rules_snapshot(
        self,
        snapshot: InstrumentExecutionRulesSnapshot,
        *,
        now_utc: datetime,
    ) -> bool:
        """Validate this exact SDK-issued receipt using the provider's own clock."""

        if (
            type(snapshot) is not InstrumentExecutionRulesSnapshot
            or type(now_utc) is not datetime
            or now_utc.tzinfo is None
            or now_utc.utcoffset() != timezone.utc.utcoffset(now_utc)
            or not self._supports_rules_snapshots
        ):
            return False
        route, context = self._bound_pair()
        if route is None or context is None:
            return False
        result = self._call_on_owner_thread(
            route,
            context,
            lambda: self._validate_execution_rules_snapshot_on_owner_thread(
                snapshot,
                now_utc,
                route,
                context,
            ),
        )
        return type(result) is bool and result is True

    def _execution_rules_snapshot_on_owner_thread(
        self,
        instrument: MarketInstrumentKey,
        hedge_class: str,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> object:
        if (
            self._supports_rules_snapshots is not True
            or self._validate_runtime_context_on_owner_thread(route, runtime_context) is not True
            or not _rules_instrument_matches_route(instrument, route)
        ):
            return CtpExecutionRulesSnapshotError("rules_snapshot_unavailable")
        try:
            evidence = self._issuer.collect_instrument_rules_snapshot(
                instrument,
                hedge_class=hedge_class,
                route=route,
                runtime_context=runtime_context,
            )
            if type(evidence) is not CtpExecutionRulesSnapshotEvidence:
                return CtpExecutionRulesSnapshotError("rules_snapshot_unavailable")
            observed_now = datetime.now(timezone.utc)
            _validate_rules_snapshot_evidence(
                instrument,
                hedge_class,
                evidence,
                route,
                observed_now,
            )
            valid = self._issuer.validate_instrument_rules_snapshot(
                instrument,
                evidence,
                hedge_class=hedge_class,
                route=route,
                runtime_context=runtime_context,
                now_utc=observed_now,
            )
            if type(valid) is not bool or valid is not True:
                return CtpExecutionRulesSnapshotError("rules_snapshot_unavailable")
            if self._validate_runtime_context_on_owner_thread(route, runtime_context) is not True:
                return CtpExecutionRulesSnapshotError("rules_snapshot_unavailable")
            final_now = datetime.now(timezone.utc)
            _validate_rules_snapshot_evidence(
                instrument,
                hedge_class,
                evidence,
                route,
                final_now,
            )
            snapshot = InstrumentExecutionRulesSnapshot(
                facts=evidence.facts,
                receipt=_CtpExecutionRulesSnapshotReceipt(),
            )
        except CtpExecutionRulesSnapshotError as exc:
            return exc
        except Exception:
            return CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")

        with self._lock:
            insertion_now = datetime.now(timezone.utc)
            try:
                _validate_rules_snapshot_evidence(
                    instrument,
                    hedge_class,
                    evidence,
                    route,
                    insertion_now,
                )
            except Exception:
                return CtpExecutionRulesSnapshotError("rules_snapshot_unavailable")
            _purge_expired_rules(self._issued_rules, insertion_now)
            if len(self._issued_rules) >= _MAX_RETAINED_RULE_SNAPSHOTS:
                return CtpExecutionRulesSnapshotError("rules_snapshot_capacity_exhausted")
            self._issued_rules[id(snapshot)] = _IssuedRulesSnapshot(
                snapshot=snapshot,
                instrument=instrument,
                hedge_class=hedge_class,
                evidence=evidence,
                receipt=snapshot.receipt,
            )
        return snapshot

    def _validate_execution_rules_snapshot_on_owner_thread(
        self,
        snapshot: InstrumentExecutionRulesSnapshot,
        caller_now_utc: datetime,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool:
        del caller_now_utc  # Caller clocks cannot extend or shorten provider authority.
        if self._validate_runtime_context_on_owner_thread(route, runtime_context) is not True:
            return False
        with self._lock:
            issued = self._issued_rules.get(id(snapshot))
        if (
            issued is None
            or issued.snapshot is not snapshot
            or issued.receipt is not snapshot.receipt
            or issued.instrument is not snapshot.facts.instrument
            or issued.hedge_class != snapshot.facts.hedge_class
            or issued.evidence.facts is not snapshot.facts
        ):
            return False
        try:
            trusted_now = datetime.now(timezone.utc)
            _validate_rules_snapshot_evidence(
                issued.instrument,
                issued.hedge_class,
                issued.evidence,
                route,
                trusted_now,
            )
            valid = self._issuer.validate_instrument_rules_snapshot(
                issued.instrument,
                issued.evidence,
                hedge_class=issued.hedge_class,
                route=route,
                runtime_context=runtime_context,
                now_utc=trusted_now,
            )
            if type(valid) is not bool or valid is not True:
                return False
            if self._validate_runtime_context_on_owner_thread(route, runtime_context) is not True:
                return False
            final_now = datetime.now(timezone.utc)
            _validate_rules_snapshot_evidence(
                issued.instrument,
                issued.hedge_class,
                issued.evidence,
                route,
                final_now,
            )
        except Exception:
            return False
        return True

    def _bound_pair(self) -> Tuple[Optional[SelectedExecutionRoute], Optional[object]]:
        with self._lock:
            return self._route, self._runtime_context

    def _call_on_owner_thread(
        self,
        route: SelectedExecutionRoute,
        runtime_context: object,
        callback: Any,
    ) -> Any:
        invoker = getattr(runtime_context, "owner_thread_invoker", None)
        timeout = getattr(runtime_context, "owner_call_timeout_seconds", None)
        if invoker is None or type(timeout) not in (int, float) or isinstance(timeout, bool):
            return False

        def checked_callback() -> Any:
            if self._validate_runtime_context_on_owner_thread(route, runtime_context) is not True:
                return False
            return callback()

        try:
            return invoker.call_on_owner_thread(
                checked_callback,
                timeout_seconds=float(timeout),
            )
        except OwnerThreadCallError:
            # After-start timeout is uncertain. This operation is read-only;
            # never retry the callback or substitute its evidence.
            return False
        except Exception:
            return False

    def _validate_runtime_context_on_owner_thread(
        self,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool:
        try:
            from bt_api_ctp.feeds.live_ctp_feed import (
                CtpRequestDataFuture,
                _CtpRuntimeOwnedRequestFeedBinding,
            )
            from bt_api_ctp.order_provider import CtpOrderProviderRuntimeContext
            from bt_api_ctp.ctp.client import TraderClient

            feed = route.client
            if (
                route.exchange_name != _CTP_FUTURE_ROUTE
                or type(runtime_context) is not CtpOrderProviderRuntimeContext
                or type(feed) is not CtpRequestDataFuture
                or runtime_context.runtime_binding is None
                or type(runtime_context.runtime_binding) is not _CtpRuntimeOwnedRequestFeedBinding
                or runtime_context.execution_economics_owner is not self
                or runtime_context.execution_economics_issuer is not self._issuer
                or runtime_context.scope is None
                or route.environment != "demo"
                or getattr(runtime_context.scope, "environment", None) != "simulation"
                or feed._connected is not True
            ):
                return False
            trader = feed.trader_client
            if type(trader) is not TraderClient:
                return False
            if runtime_context.runtime_binding.validate_provider_context(
                runtime_context,
                exchange_name=_CTP_FUTURE_ROUTE,
                selected_feed=feed,
                trader_client=trader,
            ) is not True:
                return False
            return self._issuer.is_bound_to(
                route=route,
                runtime_context=runtime_context,
            ) is True
        except Exception:
            return False

    def _evaluate_on_owner_thread(
        self,
        request: ExecutionEconomicsRequest,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> ExecutionEconomicsDecision:
        if self._validate_runtime_context_on_owner_thread(route, runtime_context) is not True:
            return _unavailable_decision(request, "CTP_ECONOMICS_RUNTIME_UNAVAILABLE")
        if _request_route_matches(request, route) is not True:
            return _unavailable_decision(request, "CTP_ECONOMICS_REQUEST_ROUTE_MISMATCH")
        try:
            evidence = self._issuer.collect_evidence(
                request,
                route=route,
                runtime_context=runtime_context,
            )
        except Exception:
            evidence = None
        if type(evidence) is not CtpExecutionEconomicsEvidence:
            return _unavailable_decision(request, "CTP_ECONOMICS_EVIDENCE_UNAVAILABLE")
        try:
            # Capture the comparison time after all source reads have returned.
            # Native query completion and snapshot observation timestamps must
            # not be compared against a clock sampled before those reads.
            validation_now = datetime.now(timezone.utc)
            _validate_evidence(request, evidence, route, validation_now)
            valid = self._issuer.validate_evidence(
                request,
                evidence,
                route=route,
                runtime_context=runtime_context,
            )
        except Exception:
            return _unavailable_decision(request, "CTP_ECONOMICS_EVIDENCE_UNAVAILABLE")
        if type(valid) is not bool or valid is not True:
            return _unavailable_decision(request, "CTP_ECONOMICS_EVIDENCE_UNAVAILABLE")
        if self._validate_runtime_context_on_owner_thread(route, runtime_context) is not True:
            return _unavailable_decision(request, "CTP_ECONOMICS_RUNTIME_UNAVAILABLE")

        try:
            decision_now = datetime.now(timezone.utc)
            # The issuer's validation may itself take time. Recheck freshness
            # immediately before creating and retaining the returned decision.
            _validate_evidence(request, evidence, route, decision_now)
            decision = _decision_from_evidence(request, evidence, decision_now)
        except Exception:
            return _unavailable_decision(request, "CTP_ECONOMICS_EVIDENCE_UNAVAILABLE")
        if decision.status != "accepted":
            return decision

        lease_ref = decision.lease_ref
        if lease_ref is None:
            return _unavailable_decision(request, "CTP_ECONOMICS_EVIDENCE_UNAVAILABLE")
        with self._lock:
            _purge_expired(self._issued, decision_now)
            if len(self._issued) >= _MAX_RETAINED_DECISIONS:
                return _unavailable_decision(request, "CTP_ECONOMICS_CAPACITY_EXHAUSTED")
            if lease_ref in self._issued:
                return _unavailable_decision(request, "CTP_ECONOMICS_EVIDENCE_UNAVAILABLE")
            self._issued[lease_ref] = _IssuedDecision(request, decision, evidence)
        return decision

    def _revalidate_on_owner_thread(
        self,
        request: ExecutionEconomicsRequest,
        decision: ExecutionEconomicsDecision,
        stage: EconomicsRevalidationStage,
        route: SelectedExecutionRoute,
        runtime_context: object,
    ) -> bool:
        now = datetime.now(timezone.utc)
        if self._validate_runtime_context_on_owner_thread(route, runtime_context) is not True:
            return False
        if _request_route_matches(request, route) is not True:
            return False
        lease_ref = decision.lease_ref
        if lease_ref is None:
            return False
        with self._lock:
            issued = self._issued.get(lease_ref)
        if (
            issued is None
            or issued.request is not request
            or issued.decision is not decision
            or decision.status != "accepted"
        ):
            return False
        try:
            _validate_evidence(request, issued.evidence, route, now)
            expected = _decision_from_evidence(request, issued.evidence, now)
            if expected != decision or expected.status != "accepted":
                return False
            valid = self._issuer.revalidate_evidence(
                request,
                decision,
                issued.evidence,
                stage=stage,
                route=route,
                runtime_context=runtime_context,
            )
        except Exception:
            return False
        if type(valid) is not bool or valid is not True:
            return False
        if self._validate_runtime_context_on_owner_thread(route, runtime_context) is not True:
            return False
        try:
            # A provider check can involve several current-source reads. Do
            # not let a lease that expired during that check return as valid.
            final_now = datetime.now(timezone.utc)
            _validate_evidence(request, issued.evidence, route, final_now)
            final_expected = _decision_from_evidence(request, issued.evidence, final_now)
        except Exception:
            return False
        return final_expected == decision and final_expected.status == "accepted"


def _create_ctp_execution_economics_owner(
    route: SelectedExecutionRoute,
) -> Optional[ExecutionEconomicsPort]:
    """Return only the runtime-sealed owner already held by this CTP route."""

    if type(route) is not SelectedExecutionRoute or route.exchange_name != _CTP_FUTURE_ROUTE:
        return None
    try:
        from bt_api_ctp.order_provider import CtpOrderProviderRuntimeContext

        context = route.execution_context
        if type(context) is not CtpOrderProviderRuntimeContext:
            return None
        owner = context.execution_economics_owner
        issuer = context.execution_economics_issuer
        if (
            type(owner) is not CtpExecutionEconomicsOwner
            or owner._issuer is not issuer
            or owner._bind_selected_route(route, context) is not True
        ):
            return None
        return owner
    except Exception:
        return None


def register_ctp_execution_economics_provider(
    registry: ExecutionEconomicsProviderRegistryPort,
) -> None:
    """Register the CTP economics owner factory for the exact futures route."""

    if not isinstance(registry, ExecutionEconomicsProviderRegistryPort):
        raise TypeError("registry must satisfy ExecutionEconomicsProviderRegistryPort")
    registry.register(_CTP_FUTURE_ROUTE, _create_ctp_execution_economics_owner)


def _issuer_shape_is_valid(issuer: object) -> bool:
    return callable(getattr(issuer, "is_bound_to", None)) and (
        _economics_issuer_shape_is_valid(issuer)
        or _rules_snapshot_issuer_shape_is_valid(issuer)
    )


def _economics_issuer_shape_is_valid(issuer: object) -> bool:
    if not all(
        callable(getattr(issuer, name, None))
        for name in (
            "collect_evidence",
            "validate_evidence",
            "revalidate_evidence",
        )
    ):
        return False
    readiness = getattr(issuer, "economics_source_ready", None)
    if not callable(readiness):
        return False
    try:
        result = readiness()
    except Exception:
        return False
    return type(result) is bool and result is True


def _rules_snapshot_issuer_shape_is_valid(issuer: object) -> bool:
    return all(
        callable(getattr(issuer, name, None))
        for name in (
            "collect_instrument_rules_snapshot",
            "validate_instrument_rules_snapshot",
        )
    )


def _rules_instrument_matches_route(
    instrument: MarketInstrumentKey,
    route: SelectedExecutionRoute,
) -> bool:
    return (
        type(instrument) is MarketInstrumentKey
        and type(route) is SelectedExecutionRoute
        and route.exchange_name == _CTP_FUTURE_ROUTE
        and route.environment == "demo"
        and instrument.provider == "ctp"
        and instrument.environment == route.environment
        and instrument.product_type in {"future", "option"}
        and instrument.instrument_id == instrument.native_symbol
        and type(instrument.native_symbol) is str
        and bool(instrument.native_symbol)
        and type(instrument.venue) is str
        and bool(instrument.venue)
    )


def _ctp_native_hedge_flag(hedge_class: str) -> Optional[str]:
    names = {
        "speculation": "THOST_FTDC_HF_Speculation",
        "arbitrage": "THOST_FTDC_HF_Arbitrage",
        "hedge": "THOST_FTDC_HF_Hedge",
    }
    name = names.get(hedge_class)
    if name is None:
        return None
    try:
        from bt_api_ctp.ctp import ctp_constants

        value = getattr(ctp_constants, name)
    except Exception:
        return None
    return value if type(value) is str and value and value == value.strip() else None


def _validate_rules_snapshot_evidence(
    instrument: MarketInstrumentKey,
    hedge_class: str,
    evidence: CtpExecutionRulesSnapshotEvidence,
    route: SelectedExecutionRoute,
    now: datetime,
) -> None:
    if type(evidence) is not CtpExecutionRulesSnapshotEvidence:
        raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
    if not _rules_instrument_matches_route(instrument, route):
        raise CtpExecutionRulesSnapshotError("rules_snapshot_request_invalid")
    facts = evidence.facts
    if (
        type(facts) is not InstrumentExecutionFacts
        or facts.instrument is not instrument
        or facts.status != "available"
        or facts.hedge_class != hedge_class
        or hedge_class not in _RULES_HEDGE_CLASSES
        or facts.asset_kind != instrument.product_type
        or facts.session_generation is None
        or facts.source_ref is None
        or _SHA256_PATTERN.fullmatch(facts.source_ref) is None
        or facts.price_tick is None
        or facts.contract_multiplier is None
    ):
        raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
    if facts.asset_kind == "option":
        if (
            facts.option_right not in {"call", "put"}
            or facts.option_strike is None
            or facts.option_expiry is None
            or facts.option_premium_style != "premium"
        ):
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
    elif any(
        value is not None
        for value in (
            facts.option_right,
            facts.option_strike,
            facts.option_expiry,
            facts.option_premium_style,
        )
    ):
        raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")

    try:
        if (
            _parse_amount(facts.price_tick, "price_tick") <= 0
            or _parse_amount(facts.contract_multiplier, "contract_multiplier") <= 0
        ):
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
    except Exception:
        raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid") from None

    if facts.price_currency_status == "verified":
        if (
            facts.price_currency is None
            or facts.price_currency_source_ref is None
            or _SHA256_PATTERN.fullmatch(facts.price_currency_source_ref) is None
            or facts.price_currency_session_generation != facts.session_generation
        ):
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
    elif (
        facts.price_currency is not None
        or facts.price_currency_source_ref is not None
        or facts.price_currency_session_generation is not None
    ):
        raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")

    fees = {fee.role: fee for fee in facts.commission_rules}
    if set(fees) != _REQUIRED_CTP_FEE_ROLES:
        raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
    for fee in fees.values():
        if (
            fee.source_ref is None
            or fee.session_generation != facts.session_generation
            or fee.by_money_rate is None
            or fee.by_volume_amount is None
        ):
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
        try:
            _parse_amount(fee.by_money_rate, "by_money_rate")
            _parse_amount(fee.by_volume_amount, "by_volume_amount")
        except Exception:
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid") from None
        if fee.currency_status == "verified":
            if (
                fee.currency is None
                or fee.currency_source_ref is None
                or _SHA256_PATTERN.fullmatch(fee.currency_source_ref) is None
                or fee.currency_session_generation != facts.session_generation
            ):
                raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
        elif (
            fee.currency is not None
            or fee.currency_source_ref is not None
            or fee.currency_session_generation is not None
        ):
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")

    digests, certificate_expiry, certificate_observed = _validate_rules_native_certificate(
        evidence.native_query_certificate,
        instrument,
        hedge_class,
    )
    if (
        facts.source_ref != digests.get("instruments")
        or facts.observed_at_utc is None
        or facts.expires_at_utc is None
        or facts.observed_at_utc < certificate_observed
        or facts.expires_at_utc > certificate_expiry
        or facts.expires_at_utc <= facts.observed_at_utc
        or now < facts.observed_at_utc
        or now >= facts.expires_at_utc
    ):
        raise CtpExecutionRulesSnapshotError("rules_snapshot_unavailable")
    if any(fee.source_ref != digests.get("commission_rate") for fee in fees.values()):
        raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")


def _validate_rules_native_certificate(
    certificate: CtpNativeQueryCertificate,
    instrument: MarketInstrumentKey,
    hedge_class: str,
) -> Tuple[dict[str, str], datetime, datetime]:
    if type(certificate) is not CtpNativeQueryCertificate:
        raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
    expected_hedge = _ctp_native_hedge_flag(hedge_class)
    if expected_hedge is None:
        raise CtpExecutionRulesSnapshotError("rules_snapshot_request_invalid")
    try:
        payload = certificate.as_public_dict()
        query_digests = certificate.query_digests
        certificate_digest = certificate.certificate_sha256
    except Exception:
        raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid") from None
    scope = payload.get("scope") if type(payload) is dict else None
    queries = payload.get("queries") if type(payload) is dict else None
    if (
        type(payload) is not dict
        or payload.get("schema") != _CERTIFICATE_SCHEMA
        or payload.get("complete") is not True
        or payload.get("atomic_snapshot") is not False
        or payload.get("execution_authorized") is not False
        or type(scope) is not dict
        or scope.get("instrument_id_sha256") != _sha256_text(instrument.native_symbol)
        or scope.get("exchange_id_sha256") != _sha256_text(instrument.venue)
        or scope.get("hedge_flag_sha256") != _sha256_text(expected_hedge)
        or scope.get("connection_generation") != certificate.connection_generation
        or scope.get("trading_day") != certificate.trading_day
        or certificate.instrument_id_sha256 != _sha256_text(instrument.native_symbol)
        or certificate.exchange_id_sha256 != _sha256_text(instrument.venue)
        or certificate.hedge_flag_sha256 != _sha256_text(expected_hedge)
        or type(certificate.connection_generation) is not int
        or certificate.connection_generation <= 0
        or type(queries) is not list
        or len(queries) != len(_REQUIRED_NATIVE_QUERIES)
        or type(certificate.queries) is not tuple
        or len(certificate.queries) != len(_REQUIRED_NATIVE_QUERIES)
        or type(query_digests) is not tuple
        or len(query_digests) != len(_REQUIRED_NATIVE_QUERIES)
        or certificate_digest != payload.get("certificate_sha256")
        or certificate_digest != payload.get("query_digest")
        or _SHA256_PATTERN.fullmatch(certificate_digest) is None
    ):
        raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")

    digests: dict[str, str] = {}
    expiries: list[datetime] = []
    observations: list[datetime] = []
    for expected_type, row, digest_pair, native_query in zip(
        _REQUIRED_NATIVE_QUERIES,
        queries,
        query_digests,
        certificate.queries,
    ):
        if (
            type(row) is not dict
            or row.get("request_type") != expected_type
            or row.get("source_provenance_validated") is not True
            or row.get("complete") is not True
            or row.get("is_last_seen") is not True
            or row.get("timed_out") is not False
            or row.get("unsupported") is not False
            or row.get("error_code") not in (None, 0)
            or row.get("error_message_present") is not False
            or row.get("submit_code") not in (None, 0)
            or type(row.get("late_callback_count")) is not int
            or row.get("late_callback_count") != 0
            or type(digest_pair) is not tuple
            or len(digest_pair) != 2
            or digest_pair[0] != expected_type
            or digest_pair[1] != row.get("records_sha256")
            or type(digest_pair[1]) is not str
            or _SHA256_PATTERN.fullmatch(digest_pair[1]) is None
            or getattr(native_query, "request_type", None) != expected_type
            or getattr(native_query, "records_sha256", None) != digest_pair[1]
            or type(getattr(native_query, "expires_at_monotonic", None))
            not in (int, float)
            or isinstance(getattr(native_query, "expires_at_monotonic", None), bool)
            or not isfinite(getattr(native_query, "expires_at_monotonic", float("nan")))
            or monotonic() >= getattr(native_query, "expires_at_monotonic", 0.0)
        ):
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
        filters = row.get("request_filter_names")
        explicit_filters = row.get("explicit_request_filter_names")
        if (
            type(filters) is not list
            or type(explicit_filters) is not list
            or not {"InstrumentID", "ExchangeID"}.issubset(filters)
        ) and expected_type in {"instruments", "margin_rate", "commission_rate"}:
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
        if expected_type == "margin_rate" and "HedgeFlag" not in explicit_filters:
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
        if expected_type == "instruments" and (
            row.get("instrument_row_scope") != "same_exchange"
            or row.get("target_instrument_match_count") != 1
        ):
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
        if expected_type == "commission_rate" and row.get("rate_exchange_scope") != "exact":
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
        try:
            started = _parse_iso_utc(row.get("started_at_utc"), "query_started_at_utc")
            completed = _parse_iso_utc(row.get("completed_at_utc"), "query_completed_at_utc")
            expires = _parse_iso_utc(row.get("expires_at_utc"), "query_expires_at_utc")
        except Exception:
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid") from None
        if started > completed or completed >= expires:
            raise CtpExecutionRulesSnapshotError("rules_snapshot_evidence_invalid")
        digests[expected_type] = digest_pair[1]
        observations.append(completed)
        expiries.append(expires)
    return digests, min(expiries), max(observations)


def _same_route_and_context(
    left_route: Optional[SelectedExecutionRoute],
    left_context: Optional[object],
    right_route: SelectedExecutionRoute,
    right_context: object,
) -> bool:
    return (
        type(left_route) is SelectedExecutionRoute
        and left_route.exchange_name == right_route.exchange_name
        and left_route.client is right_route.client
        and left_route.environment == right_route.environment
        and left_context is right_context
    )


def _request_route_matches(
    request: ExecutionEconomicsRequest,
    route: SelectedExecutionRoute,
) -> bool:
    instrument = request.instrument
    rule_request = request.rule_request
    return (
        type(route) is SelectedExecutionRoute
        and route.exchange_name == _CTP_FUTURE_ROUTE
        and route.environment is not None
        and instrument.provider == "ctp"
        and instrument.environment == route.environment
        and instrument.product_type in {"future", "option"}
        and type(instrument.instrument_id) is str
        and instrument.instrument_id == instrument.native_symbol
        and request.quantity_unit == "contract"
        and request.rule_decision.request is rule_request
        and rule_request.instrument is instrument
        and rule_request.session_generation is not None
        and request.rule_decision.status == "accepted"
        and request.rule_decision.facts is not None
        and request.rule_decision.facts.instrument == instrument
        and request.rule_decision.facts.session_generation == rule_request.session_generation
    )


def _validate_evidence(
    request: ExecutionEconomicsRequest,
    evidence: CtpExecutionEconomicsEvidence,
    route: SelectedExecutionRoute,
    now: datetime,
) -> None:
    if type(evidence) is not CtpExecutionEconomicsEvidence:
        raise CtpExecutionEconomicsError("evidence_type_invalid")
    if evidence.request is not request:
        raise CtpExecutionEconomicsError("evidence_request_mismatch")
    if not _request_route_matches(request, route):
        raise CtpExecutionEconomicsError("request_route_mismatch")

    rule = request.rule_decision
    facts = rule.facts
    if (
        type(rule) is not ExecutionRuleDecision
        or rule.status != "accepted"
        or rule.request is not request.rule_request
        or type(facts) is not InstrumentExecutionFacts
        or facts is None
        or facts.status != "available"
    ):
        raise CtpExecutionEconomicsError("rule_decision_invalid")
    if request.quantity_unit != "contract":
        raise CtpExecutionEconomicsError("quantity_unit_unsupported")
    if facts.asset_kind != request.instrument.product_type:
        raise CtpExecutionEconomicsError("rule_facts_unavailable")
    if evidence.evaluated_quantity != request.rule_request.quantity:
        raise CtpExecutionEconomicsError("evidence_request_mismatch")
    if evidence.quantity_unit != request.quantity_unit:
        raise CtpExecutionEconomicsError("evidence_request_mismatch")
    if evidence.side != request.rule_request.side.lower():
        raise CtpExecutionEconomicsError("evidence_request_mismatch")
    if evidence.position_side != rule.normalized_position_side:
        raise CtpExecutionEconomicsError("evidence_request_mismatch")
    if evidence.offset != rule.normalized_offset:
        raise CtpExecutionEconomicsError("evidence_request_mismatch")
    if evidence.limit_price != request.rule_request.limit_price:
        raise CtpExecutionEconomicsError("evidence_request_mismatch")
    if evidence.asset_kind != facts.asset_kind:
        raise CtpExecutionEconomicsError("evidence_request_mismatch")
    if evidence.session_generation != request.rule_request.session_generation:
        raise CtpExecutionEconomicsError("evidence_request_mismatch")
    _validate_position_binding(request, rule, evidence)

    fee_amount = rule.commission_estimate
    if (
        type(fee_amount) is not str
        or rule.commission_currency is None
        or rule.commission_charge_unit != "currency_amount"
        or facts.price_currency_status != "verified"
        or facts.price_currency is None
        or facts.price_currency_source_ref is None
        or facts.price_currency_session_generation != evidence.session_generation
        or rule.commission_currency != evidence.currency
        or facts.price_currency != evidence.currency
        or evidence.price_currency_source_ref != facts.price_currency_source_ref
    ):
        raise CtpExecutionEconomicsError("fee_currency_mismatch")
    _parse_amount(fee_amount, "commission_estimate")
    fee_rule = _fee_rule_for(facts.commission_rules, rule.normalized_offset)
    if (
        fee_rule is None
        or fee_rule.currency_status != "verified"
        or fee_rule.currency != evidence.currency
        or fee_rule.source_ref is None
        or fee_rule.currency_source_ref is None
        or fee_rule.session_generation != evidence.session_generation
        or fee_rule.currency_session_generation != evidence.session_generation
        or evidence.fee_currency_source_ref != fee_rule.currency_source_ref
    ):
        raise CtpExecutionEconomicsError("fee_facts_unavailable")

    # ExecutionRuleDecision is a neutral value object and can be constructed by
    # callers. Recompute its fee from the exact selected source rule so an
    # under-reported commission cannot reduce the affordability requirement.
    if (
        facts.contract_multiplier is None
        or fee_rule.by_money_rate is None
        or fee_rule.by_volume_amount is None
    ):
        raise CtpExecutionEconomicsError("fee_facts_unavailable")
    quantity = _parse_amount(request.rule_request.quantity, "quantity")
    price = _parse_amount(request.rule_request.limit_price, "limit_price")
    multiplier = _parse_amount(facts.contract_multiplier, "contract_multiplier")
    by_money = _parse_amount(fee_rule.by_money_rate, "by_money_rate")
    by_volume = _parse_amount(fee_rule.by_volume_amount, "by_volume_amount")
    if (
        quantity <= 0
        or quantity != quantity.to_integral_value()
        or price <= 0
        or multiplier <= 0
    ):
        raise CtpExecutionEconomicsError("fee_facts_unavailable")
    with localcontext() as context:
        context.prec = 700
        expected_fee = quantity * (price * multiplier * by_money + by_volume)
    if not expected_fee.is_finite() or expected_fee != _parse_amount(
        fee_amount,
        "commission_estimate",
    ):
        raise CtpExecutionEconomicsError("fee_source_mismatch")

    certificate_digests, certificate_expiry, certificate_observed = (
        _validate_native_certificate(evidence.native_query_certificate, request, evidence)
    )
    if (
        facts.source_ref != certificate_digests.get("instruments")
        or fee_rule.source_ref != evidence.commission_rate_source_ref
        or evidence.commission_rate_source_ref != certificate_digests.get("commission_rate")
        or evidence.margin_rate_source_ref != certificate_digests.get("margin_rate")
    ):
        raise CtpExecutionEconomicsError("native_query_source_mismatch")

    _validate_account_evidence(evidence, now)
    if (
        evidence.source_ref is None
        or _ECONOMICS_SOURCE_REF_PATTERN.fullmatch(evidence.source_ref) is None
        or _ACCOUNT_SCOPE_REF_PATTERN.fullmatch(evidence.account_scope_ref) is None
        or _ECONOMICS_LEASE_REF_PATTERN.fullmatch(evidence.lease_ref) is None
        or _QUERY_SNAPSHOT_REF_PATTERN.fullmatch(evidence.account_balance_source_ref) is None
        or _SHA256_PATTERN.fullmatch(evidence.hedge_flag_source_ref) is None
        or _SHA256_PATTERN.fullmatch(evidence.margin_rate_source_ref) is None
        or _SHA256_PATTERN.fullmatch(evidence.commission_rate_source_ref) is None
        or _SHA256_PATTERN.fullmatch(evidence.price_currency_source_ref) is None
        or _SHA256_PATTERN.fullmatch(evidence.fee_currency_source_ref) is None
    ):
        raise CtpExecutionEconomicsError("evidence_reference_invalid")
    if (
        evidence.account_balance_source_ref != evidence.account_snapshot.snapshot_ref
        or evidence.account_lease.source_ref != evidence.account_snapshot.source_ref
        or evidence.lease_ref == evidence.account_lease.lease_ref
    ):
        raise CtpExecutionEconomicsError("account_lease_scope_mismatch")

    rule_expiry = facts.expires_at_utc
    if rule_expiry is None or evidence.expires_at_utc > rule_expiry:
        raise CtpExecutionEconomicsError("evidence_time_invalid")
    if evidence.expires_at_utc > certificate_expiry:
        raise CtpExecutionEconomicsError("evidence_time_invalid")
    if evidence.observed_at_utc < certificate_observed:
        raise CtpExecutionEconomicsError("evidence_time_invalid")
    if now < evidence.observed_at_utc or now >= evidence.expires_at_utc:
        raise CtpExecutionEconomicsError("evidence_stale")


def _validate_position_binding(
    request: ExecutionEconomicsRequest,
    rule: Any,
    evidence: CtpExecutionEconomicsEvidence,
) -> None:
    source_request = request.rule_request
    position_values = (
        source_request.position_evidence_ref,
        source_request.position_evidence_generation,
        source_request.position_observed_at_utc,
        source_request.position_expires_at_utc,
    )
    evidence_values = (
        evidence.position_evidence_ref,
        evidence.position_evidence_generation,
        evidence.position_observed_at_utc,
        evidence.position_expires_at_utc,
    )
    if position_values != evidence_values:
        raise CtpExecutionEconomicsError("position_evidence_unavailable")
    if rule.normalized_offset != "open":
        if (
            rule.position_evidence_validated is not True
            or source_request.position_evidence_ref is None
            or source_request.position_evidence_generation != evidence.session_generation
            or source_request.position_observed_at_utc is None
            or source_request.position_expires_at_utc is None
        ):
            raise CtpExecutionEconomicsError("position_evidence_unavailable")
        if evidence.expires_at_utc > source_request.position_expires_at_utc:
            raise CtpExecutionEconomicsError("evidence_time_invalid")
        if evidence.observed_at_utc < source_request.position_observed_at_utc:
            raise CtpExecutionEconomicsError("evidence_time_invalid")


def _validate_native_certificate(
    certificate: CtpNativeQueryCertificate,
    request: ExecutionEconomicsRequest,
    evidence: CtpExecutionEconomicsEvidence,
) -> Tuple[dict[str, str], datetime, datetime]:
    if type(certificate) is not CtpNativeQueryCertificate:
        raise CtpExecutionEconomicsError("native_certificate_untrusted")
    try:
        payload = certificate.as_public_dict()
        query_digests = certificate.query_digests
        certificate_digest = certificate.certificate_sha256
    except Exception:
        raise CtpExecutionEconomicsError("native_certificate_untrusted") from None
    scope = payload.get("scope")
    queries = payload.get("queries")
    instrument = request.instrument
    if (
        type(payload) is not dict
        or payload.get("schema") != _CERTIFICATE_SCHEMA
        or payload.get("complete") is not True
        or payload.get("atomic_snapshot") is not False
        or payload.get("execution_authorized") is not False
        or type(scope) is not dict
        or scope.get("instrument_id_sha256") != _sha256_text(instrument.native_symbol)
        or scope.get("exchange_id_sha256") != _sha256_text(instrument.venue)
        or scope.get("hedge_flag_sha256") != _sha256_text(evidence.hedge_flag)
        or certificate.instrument_id_sha256 != _sha256_text(instrument.native_symbol)
        or certificate.exchange_id_sha256 != _sha256_text(instrument.venue)
        or certificate.hedge_flag_sha256 != _sha256_text(evidence.hedge_flag)
        or type(certificate.connection_generation) is not int
        or certificate.connection_generation <= 0
        or type(queries) is not list
        or len(queries) != len(_REQUIRED_NATIVE_QUERIES)
        or type(query_digests) is not tuple
        or len(query_digests) != len(_REQUIRED_NATIVE_QUERIES)
        or certificate_digest != payload.get("certificate_sha256")
    ):
        raise CtpExecutionEconomicsError("native_certificate_scope_mismatch")

    digests: dict[str, str] = {}
    expiries: list[datetime] = []
    observations: list[datetime] = []
    for expected_type, row, digest_pair in zip(
        _REQUIRED_NATIVE_QUERIES,
        queries,
        query_digests,
    ):
        if (
            type(row) is not dict
            or row.get("request_type") != expected_type
            or row.get("source_provenance_validated") is not True
            or row.get("complete") is not True
            or row.get("is_last_seen") is not True
            or row.get("timed_out") is not False
            or row.get("unsupported") is not False
            or row.get("error_code") not in (None, 0)
            or row.get("error_message_present") is not False
            or row.get("submit_code") not in (None, 0)
            or type(row.get("late_callback_count")) is not int
            or row.get("late_callback_count") != 0
            or type(digest_pair) is not tuple
            or len(digest_pair) != 2
            or digest_pair[0] != expected_type
            or digest_pair[1] != row.get("records_sha256")
            or type(digest_pair[1]) is not str
            or _SHA256_PATTERN.fullmatch(digest_pair[1]) is None
        ):
            raise CtpExecutionEconomicsError("native_certificate_incomplete")
        started = _parse_iso_utc(row.get("started_at_utc"), "query_started_at_utc")
        completed = _parse_iso_utc(row.get("completed_at_utc"), "query_completed_at_utc")
        expires = _parse_iso_utc(row.get("expires_at_utc"), "query_expires_at_utc")
        if started > completed or completed >= expires:
            raise CtpExecutionEconomicsError("native_certificate_incomplete")
        digests[expected_type] = digest_pair[1]
        observations.append(completed)
        expiries.append(expires)

    if (
        _SHA256_PATTERN.fullmatch(certificate_digest) is None
        or digests["margin_rate"] != evidence.margin_rate_source_ref
        or digests["commission_rate"] != evidence.commission_rate_source_ref
        or type(certificate.trading_day) is not str
        or certificate.trading_day != certificate.trading_day.strip()
    ):
        raise CtpExecutionEconomicsError("native_query_source_mismatch")
    return digests, min(expiries), max(observations)


def _validate_account_evidence(
    evidence: CtpExecutionEconomicsEvidence,
    now: datetime,
) -> None:
    receipt = evidence.account_receipt
    snapshot = evidence.account_snapshot
    lease = evidence.account_lease
    if (
        type(receipt) is not QueryBatchReceipt
        or type(snapshot) is not QuerySnapshot
        or type(lease) is not QuerySnapshotLease
        or receipt.complete is not True
        or len(receipt.requests) != 1
        or len(receipt.snapshots) != 1
        or len(snapshot.records) == 0
        or snapshot is not receipt.snapshots[0]
        or snapshot.request.family != "account"
        or snapshot.request.required is not True
        or snapshot.request.require_complete_scope is not True
        or snapshot.is_complete is not True
        or receipt.source_ref != evidence.account_snapshot.source_ref
        or receipt.generation != evidence.session_generation
        or snapshot.generation != evidence.session_generation
        or lease.generation != evidence.session_generation
        or lease.receipt_digest != receipt.receipt_digest
        or lease.source_ref != receipt.source_ref
        or lease.snapshot_refs != receipt.complete_snapshot_refs
        or snapshot.snapshot_ref not in lease.snapshot_refs
        or lease.purpose != "execution_economics"
        or snapshot.started_at is None
        or snapshot.completed_at is None
        or receipt.started_at > snapshot.started_at
        or snapshot.started_at > snapshot.completed_at
        or snapshot.completed_at > receipt.completed_at
        or receipt.completed_at > lease.issued_at
        or lease.issued_at > evidence.observed_at_utc
        or receipt.completed_at > evidence.observed_at_utc
        or evidence.expires_at_utc > lease.expires_at
        or now < lease.issued_at
        or now >= lease.expires_at
        or _QUERY_SNAPSHOT_REF_PATTERN.fullmatch(str(snapshot.snapshot_ref)) is None
        or _QUERY_LEASE_REF_PATTERN.fullmatch(lease.lease_ref) is None
    ):
        raise CtpExecutionEconomicsError("account_evidence_untrusted")

    if snapshot.observed_at is None or evidence.observed_at_utc < snapshot.observed_at:
        raise CtpExecutionEconomicsError("evidence_time_invalid")
    if (
        len(receipt.requests) != 1
        or receipt.requests[0].family != "account"
        or receipt.requests[0].required is not True
        or receipt.requests[0].require_complete_scope is not True
        or snapshot.request != receipt.requests[0]
    ):
        raise CtpExecutionEconomicsError("account_snapshot_scope_mismatch")

    balances: list[AccountBalance] = []
    currencies: set[str] = set()
    try:
        for row in snapshot.records:
            balance = AccountBalance.from_dict(row)
            if balance.currency in currencies:
                raise CtpExecutionEconomicsError("account_currency_unavailable")
            currencies.add(balance.currency)
            balances.append(balance)
    except CtpExecutionEconomicsError:
        raise
    except Exception:
        raise CtpExecutionEconomicsError("account_evidence_untrusted") from None
    matching = [balance for balance in balances if balance.currency == evidence.currency]
    if len(matching) != 1 or matching[0].available is None:
        raise CtpExecutionEconomicsError("account_currency_unavailable")
    provider_available = _parse_amount(matching[0].available, "account_available")
    reported_available = _parse_amount(evidence.available_amount, "available_amount")
    if reported_available > provider_available:
        raise CtpExecutionEconomicsError("account_amount_invalid")


def _decision_from_evidence(
    request: ExecutionEconomicsRequest,
    evidence: CtpExecutionEconomicsEvidence,
    now: datetime,
) -> ExecutionEconomicsDecision:
    if evidence.observed_at_utc > now or now >= evidence.expires_at_utc:
        return _unavailable_decision(request, "CTP_ECONOMICS_EVIDENCE_STALE")
    fee_amount = _parse_amount(
        request.rule_decision.commission_estimate,
        "commission_estimate",
    )
    base_required = _parse_amount(
        evidence.required_amount_before_fee,
        "required_amount_before_fee",
    )
    available = _parse_amount(evidence.available_amount, "available_amount")
    with localcontext() as context:
        context.prec = 300
        required = base_required + fee_amount
    required_text = format(required, "f")
    if len(required_text) > 128:
        raise CtpExecutionEconomicsError("economics_amount_invalid")
    status = "accepted" if required <= available else "rejected"
    reason_code = None if status == "accepted" else "CTP_INSUFFICIENT_AVAILABLE_AMOUNT"
    return ExecutionEconomicsDecision(
        request=request,
        status=status,
        reason_code=reason_code,
        requirement_kind=evidence.requirement_kind,
        instrument=request.instrument,
        evaluated_quantity=request.rule_request.quantity,
        quantity_unit=request.quantity_unit,
        required_amount=required_text,
        available_amount=evidence.available_amount,
        currency=evidence.currency,
        amount_unit="currency_amount",
        fee_included=True,
        source_ref=evidence.source_ref,
        account_balance_source_ref=evidence.account_balance_source_ref,
        account_scope_ref=evidence.account_scope_ref,
        session_generation=evidence.session_generation,
        observed_at_utc=evidence.observed_at_utc,
        expires_at_utc=evidence.expires_at_utc,
        lease_ref=evidence.lease_ref,
    )


def _unavailable_decision(
    request: ExecutionEconomicsRequest,
    reason_code: str,
) -> ExecutionEconomicsDecision:
    return ExecutionEconomicsDecision(
        request=request,
        status="unavailable",
        reason_code=reason_code,
        requirement_kind=None,
        instrument=request.instrument,
        evaluated_quantity=None,
        quantity_unit=None,
        required_amount=None,
        available_amount=None,
        currency=None,
        amount_unit=None,
        fee_included=False,
        source_ref=None,
        account_balance_source_ref=None,
        account_scope_ref=None,
        session_generation=None,
        observed_at_utc=None,
        expires_at_utc=None,
        lease_ref=None,
    )


def _fee_rule_for(rules: tuple[Any, ...], offset: Optional[str]) -> Optional[Any]:
    if offset not in {"open", "close", "close_today", "close_yesterday"}:
        return None
    by_role = {item.role: item for item in rules}
    if offset == "close_yesterday" and offset not in by_role:
        return by_role.get("close")
    return by_role.get(offset)


def _purge_expired(issued: OrderedDict[str, _IssuedDecision], now: datetime) -> None:
    for lease_ref, record in tuple(issued.items()):
        expiry = record.decision.expires_at_utc
        if expiry is None or now >= expiry:
            issued.pop(lease_ref, None)


def _purge_expired_rules(
    issued: OrderedDict[int, _IssuedRulesSnapshot],
    now: datetime,
) -> None:
    for snapshot_id, record in tuple(issued.items()):
        expiry = record.snapshot.facts.expires_at_utc
        if expiry is None or now >= expiry:
            issued.pop(snapshot_id, None)


def _parse_amount(value: object, field_name: str) -> Decimal:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or value != value.strip()
        or _DECIMAL_PATTERN.fullmatch(value) is None
    ):
        raise CtpExecutionEconomicsError("economics_amount_invalid")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise CtpExecutionEconomicsError("economics_amount_invalid") from None
    if not amount.is_finite() or amount < 0:
        raise CtpExecutionEconomicsError("economics_amount_invalid")
    return amount


def _require_text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ValueError(f"{name} must be non-empty trimmed printable text")
    value.encode("utf-8", errors="strict")
    return value


def _require_utc(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(f"{name} must be an aware UTC datetime")
    return value


def _parse_iso_utc(value: object, name: str) -> datetime:
    if type(value) is not str or not value:
        raise CtpExecutionEconomicsError("native_certificate_incomplete")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CtpExecutionEconomicsError("native_certificate_incomplete") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise CtpExecutionEconomicsError("native_certificate_incomplete")
    return parsed


def _sha256_text(value: str) -> str:
    return sha256(value.encode("utf-8", errors="strict")).hexdigest()


