"""CTP provider for the neutral SDK order-rules evaluator capability.

The provider is selected only for the exact SDK route and reuses the same
CTP-owned execution-rules snapshot owner as the snapshot facade. Its result is
an advisory rule decision; it does not claim, authorize, reserve, or dispatch an
order. Position-dependent decisions remain unavailable until an issuer-bound
position-evidence validator is available to this capability.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from threading import RLock
from typing import Optional, Protocol, runtime_checkable

from bt_api_base.execution_correlation_protocol import SelectedExecutionRoute
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_base.execution_rules_protocol import (
    ExecutionRuleDecision,
    ExecutionRuleRequest,
    ExecutionRulesPort,
    ExecutionRulesPreparationPort,
    ExecutionRulesPreparationUnavailable,
    ExecutionRulesProviderRegistryPort,
    ExecutionRulesSnapshotPort,
    InstrumentExecutionRulesSnapshot,
)
from bt_api_ctp.execution_economics_registration import (
    CtpExecutionEconomicsOwner,
    CtpExecutionRulesSnapshotEvidenceIssuer,
)
from bt_api_ctp.execution_rules import CtpExecutionRulesEvaluator
from bt_api_ctp.order_provider import CtpOrderProviderRuntimeContext

__all__ = [
    "CtpOrderRulesPositionEvidenceIssuer",
    "CtpOrderRulesProvider",
    "register_ctp_order_rules_provider",
]

_CTP_FUTURE_ROUTE = "CTP___FUTURE"
_MAX_CACHED_RULES_SNAPSHOTS = 64


@runtime_checkable
class CtpOrderRulesPositionEvidenceIssuer(Protocol):
    """Optional source-bound position proof capability for order-rule closes.

    Implementations are injected as the trusted rules evidence issuer in the
    selected CTP runtime context. The validator runs on the CTP owner thread
    and may return exact ``True`` only when the opaque reference, generation,
    timestamps, and every supplied position quantity in ``request`` match the
    issuer's current source-bound evidence for ``rules_snapshot``. It returns
    only a boolean and grants no authorization.
    """

    def validate_order_rules_position_evidence(
        self,
        request: ExecutionRuleRequest,
        *,
        rules_snapshot: InstrumentExecutionRulesSnapshot,
        route: SelectedExecutionRoute,
        runtime_context: CtpOrderProviderRuntimeContext,
    ) -> bool: ...


class CtpOrderRulesProvider(ExecutionRulesPort, ExecutionRulesPreparationPort):
    """Route-bound, read-only evaluator over an SDK-issued current snapshot.

    The exact request object is passed unchanged to the existing CTP evaluator
    and echoed unchanged in its decision. In particular, this adapter does not
    fill side, time-in-force, offset, position mode, or hedge class from local
    Backtrader state. The evaluator owns the existing CTP normalization and
    rejection rules.

    Closes and net-offset inference require an issuer implementing
    :class:`CtpOrderRulesPositionEvidenceIssuer`. Without that capability they
    fail closed even when the request includes local position quantities or an
    unverified evidence reference. Explicit opens can be evaluated when the
    same-session instrument/fee snapshot supplies all required facts.
    """

    __slots__ = (
        "__route",
        "__runtime_context",
        "__snapshot_owner",
        "__position_evidence_issuer",
        "__snapshot_cache",
        "__snapshot_lock",
    )

    def __init__(
        self,
        route: SelectedExecutionRoute,
        runtime_context: CtpOrderProviderRuntimeContext,
        snapshot_owner: ExecutionRulesSnapshotPort,
        position_evidence_issuer: Optional[CtpOrderRulesPositionEvidenceIssuer] = None,
    ) -> None:
        if (
            type(route) is not SelectedExecutionRoute
            or route.exchange_name != _CTP_FUTURE_ROUTE
            or type(runtime_context) is not CtpOrderProviderRuntimeContext
            or type(snapshot_owner) is not CtpExecutionEconomicsOwner
            or not isinstance(snapshot_owner, ExecutionRulesSnapshotPort)
            or (
                position_evidence_issuer is not None
                and (
                    not isinstance(position_evidence_issuer, CtpOrderRulesPositionEvidenceIssuer)
                    or position_evidence_issuer
                    is not runtime_context.execution_economics_issuer
                )
            )
        ):
            raise TypeError("CTP order-rules provider binding is invalid")
        if snapshot_owner._bind_selected_route(route, runtime_context) is not True:
            raise TypeError("CTP order-rules provider route is not bound")
        self.__route = route
        self.__runtime_context = runtime_context
        self.__snapshot_owner = snapshot_owner
        self.__position_evidence_issuer = position_evidence_issuer
        self.__snapshot_cache: OrderedDict[
            tuple[object, str, str], InstrumentExecutionRulesSnapshot
        ] = OrderedDict()
        self.__snapshot_lock = RLock()

    def __repr__(self) -> str:
        return "<CTP route-bound order-rules provider>"

    def prepare_rules(
        self,
        instrument: MarketInstrumentKey,
        *,
        hedge_class: str,
    ) -> InstrumentExecutionRulesSnapshot:
        """Issue and cache rules facts for the evaluator that will consume them.

        This deliberately has no order-request parameter. Prewarming does not
        invent an open, price, side, offset, quantity, or authorization. The
        exact SDK snapshot object is stored in this provider's evaluator cache
        under its source-issued session generation so the subsequent decision
        path can consume and revalidate the same evidence.
        """

        if type(instrument) is not MarketInstrumentKey:
            raise ExecutionRulesPreparationUnavailable(
                "execution_rules_preparation_projection_invalid"
            )
        if type(hedge_class) is not str or hedge_class not in {
            "speculation",
            "arbitrage",
            "hedge",
        }:
            raise ExecutionRulesPreparationUnavailable(
                "execution_rules_preparation_hedge_class_unavailable"
            )
        if self.__route.environment is None:
            raise ExecutionRulesPreparationUnavailable(
                "execution_rules_preparation_route_unavailable"
            )
        if instrument.environment != self.__route.environment:
            raise ExecutionRulesPreparationUnavailable(
                "execution_rules_preparation_instrument_route_mismatch"
            )

        try:
            snapshot = self.__snapshot_owner.execution_rules_snapshot(
                instrument,
                hedge_class=hedge_class,
            )
        except Exception:
            raise ExecutionRulesPreparationUnavailable(
                "execution_rules_preparation_provider_unavailable"
            ) from None
        if (
            type(snapshot) is not InstrumentExecutionRulesSnapshot
            or snapshot.facts.instrument is not instrument
            or snapshot.facts.hedge_class != hedge_class
            or snapshot.facts.session_generation is None
            or snapshot.facts.status != "available"
        ):
            raise ExecutionRulesPreparationUnavailable(
                "execution_rules_preparation_projection_invalid"
            )

        if not self._snapshot_is_current(snapshot):
            raise ExecutionRulesPreparationUnavailable(
                "execution_rules_preparation_not_current"
            )

        key = (
            instrument,
            snapshot.facts.session_generation,
            hedge_class,
        )
        with self.__snapshot_lock:
            # A preparation call replaces any earlier source snapshot for the
            # same instrument/hedge pair. It never rewrites its facts or its
            # provider-issued receipt.
            stale_keys = tuple(
                cached_key
                for cached_key in self.__snapshot_cache
                if cached_key[0] == instrument and cached_key[2] == hedge_class
            )
            for cached_key in stale_keys:
                del self.__snapshot_cache[cached_key]
            self.__snapshot_cache[key] = snapshot
            self.__snapshot_cache.move_to_end(key)
            while len(self.__snapshot_cache) > _MAX_CACHED_RULES_SNAPSHOTS:
                self.__snapshot_cache.popitem(last=False)
        return snapshot

    def validate_prepared_rules(
        self,
        snapshot: InstrumentExecutionRulesSnapshot,
        *,
        now_utc: datetime,
    ) -> bool:
        """Revalidate only the exact prepared object retained by this evaluator."""

        if (
            type(snapshot) is not InstrumentExecutionRulesSnapshot
            or type(now_utc) is not datetime
            or now_utc.tzinfo is None
            or now_utc.utcoffset() != timezone.utc.utcoffset(now_utc)
        ):
            return False
        facts = snapshot.facts
        if (
            facts.instrument.environment != self.__route.environment
            or facts.session_generation is None
            or facts.hedge_class is None
            or facts.status != "available"
        ):
            return False
        key = (facts.instrument, facts.session_generation, facts.hedge_class)
        with self.__snapshot_lock:
            if self.__snapshot_cache.get(key) is not snapshot:
                return False
            try:
                current = self.__snapshot_owner.validate_execution_rules_snapshot(
                    snapshot,
                    now_utc=now_utc,
                )
            except Exception:
                current = False
            if type(current) is not bool or current is not True:
                if self.__snapshot_cache.get(key) is snapshot:
                    del self.__snapshot_cache[key]
                return False
            return True

    def evaluate_order_rules(self, request: ExecutionRuleRequest) -> ExecutionRuleDecision:
        if type(request) is not ExecutionRuleRequest:
            raise TypeError("request must be an ExecutionRuleRequest")
        if request.hedge_class is None:
            return _unavailable(request, "order_rules_hedge_class_unavailable")
        if request.session_generation is None:
            return _unavailable(request, "order_rules_session_generation_unavailable")
        if (
            self.__route.environment is None
            or request.instrument.environment != self.__route.environment
        ):
            return _unavailable(request, "order_rules_instrument_route_mismatch")
        needs_position_evidence = _request_needs_position_evidence(request)
        if needs_position_evidence and (
            self.__position_evidence_issuer is None
            or request.position_evidence_ref is None
            or request.position_evidence_generation is None
            or request.position_observed_at_utc is None
            or request.position_expires_at_utc is None
        ):
            # The request's numeric quantities and opaque reference are not
            # proof. Without every bound ref/time field and a source-bound
            # validator, do not let local quantities influence either an
            # acceptance or a rejection.
            return _unavailable(request, "position_evidence_validation_unavailable")

        snapshot = self._current_snapshot(request)
        if snapshot is None:
            return _unavailable(request, "execution_rules_snapshot_unavailable")

        position_evidence_validated = False
        if needs_position_evidence:
            position_evidence_validated = self._validate_position_evidence(request, snapshot)
            if not position_evidence_validated:
                return _unavailable(request, "position_evidence_validation_unavailable")

        evaluator = CtpExecutionRulesEvaluator(
            facts_resolver=lambda instrument, generation: (
                snapshot.facts
                if instrument == request.instrument
                and generation == request.session_generation
                and instrument == snapshot.facts.instrument
                and generation == snapshot.facts.session_generation
                else None
            ),
            # The provider validates exact source-bound evidence before the
            # evaluator reads position quantities for offset and size rules.
            # The decision remains bound to this same exact request object.
            position_evidence_validator=lambda candidate: (
                candidate is request and position_evidence_validated
            ),
        )
        try:
            decision = evaluator.evaluate_order_rules(request)
        except Exception:
            return _unavailable(request, "order_rules_evaluation_unavailable")

        if (
            type(decision) is not ExecutionRuleDecision
            or decision.request is not request
            or decision.facts is not snapshot.facts
        ):
            return _unavailable(request, "order_rules_projection_invalid")
        try:
            ExecutionRuleDecision.__post_init__(decision)
        except Exception:
            return _unavailable(request, "order_rules_projection_invalid")
        if not self._snapshot_is_current(snapshot):
            self._discard_snapshot(request, snapshot)
            return _unavailable(request, "execution_rules_snapshot_expired")
        return decision

    def _current_snapshot(
        self,
        request: ExecutionRuleRequest,
    ) -> Optional[InstrumentExecutionRulesSnapshot]:
        key = (request.instrument, request.session_generation, request.hedge_class)
        with self.__snapshot_lock:
            cached = self.__snapshot_cache.get(key)
            if cached is not None:
                if self._snapshot_matches_request(cached, request) and self._snapshot_is_current(
                    cached
                ):
                    self.__snapshot_cache.move_to_end(key)
                    return cached
                # A failed revalidation has an ambiguous cause. Drop the
                # cached object and report unavailable for this call instead
                # of issuing a replacement snapshot in the same evaluation.
                del self.__snapshot_cache[key]
                return None
            try:
                snapshot = self.__snapshot_owner.execution_rules_snapshot(
                    request.instrument,
                    hedge_class=request.hedge_class,
                )
            except Exception:
                return None
            if type(snapshot) is not InstrumentExecutionRulesSnapshot:
                return None
            if not self._snapshot_matches_request(
                snapshot, request
            ) or not self._snapshot_is_current(snapshot):
                return None
            self.__snapshot_cache[key] = snapshot
            self.__snapshot_cache.move_to_end(key)
            while len(self.__snapshot_cache) > _MAX_CACHED_RULES_SNAPSHOTS:
                self.__snapshot_cache.popitem(last=False)
            return snapshot

    @staticmethod
    def _snapshot_matches_request(
        snapshot: InstrumentExecutionRulesSnapshot,
        request: ExecutionRuleRequest,
    ) -> bool:
        facts = snapshot.facts
        return (
            facts.instrument == request.instrument
            and facts.session_generation == request.session_generation
            and facts.hedge_class == request.hedge_class
            and facts.status == "available"
        )

    def _discard_snapshot(
        self,
        request: ExecutionRuleRequest,
        snapshot: InstrumentExecutionRulesSnapshot,
    ) -> None:
        key = (request.instrument, request.session_generation, request.hedge_class)
        with self.__snapshot_lock:
            if self.__snapshot_cache.get(key) is snapshot:
                del self.__snapshot_cache[key]

    def _snapshot_is_current(self, snapshot: InstrumentExecutionRulesSnapshot) -> bool:
        try:
            return (
                self.__snapshot_owner.validate_execution_rules_snapshot(
                    snapshot,
                    now_utc=datetime.now(timezone.utc),
                )
                is True
            )
        except Exception:
            return False

    def _validate_position_evidence(
        self,
        request: ExecutionRuleRequest,
        snapshot: InstrumentExecutionRulesSnapshot,
    ) -> bool:
        issuer = self.__position_evidence_issuer
        if issuer is None:
            return False

        def validate_on_owner_thread() -> bool:
            try:
                return issuer.validate_order_rules_position_evidence(
                    request,
                    rules_snapshot=snapshot,
                    route=self.__route,
                    runtime_context=self.__runtime_context,
                )
            except Exception:
                return False

        try:
            result = self.__snapshot_owner._call_on_owner_thread(
                self.__route,
                self.__runtime_context,
                validate_on_owner_thread,
            )
        except Exception:
            return False
        return type(result) is bool and result is True


def register_ctp_order_rules_provider(registry: ExecutionRulesProviderRegistryPort) -> None:
    """Register the route-bound evaluator with the neutral SDK registry."""

    if not isinstance(registry, ExecutionRulesProviderRegistryPort):
        raise TypeError("registry must satisfy ExecutionRulesProviderRegistryPort")
    registry.register(_CTP_FUTURE_ROUTE, _create_ctp_order_rules_provider)


def _create_ctp_order_rules_provider(
    route: SelectedExecutionRoute,
) -> Optional[CtpOrderRulesProvider]:
    """Resolve only the exact CTP runtime owner already selected by the SDK."""

    if type(route) is not SelectedExecutionRoute or route.exchange_name != _CTP_FUTURE_ROUTE:
        return None
    context = route.execution_context
    if type(context) is not CtpOrderProviderRuntimeContext:
        return None
    owner = context.execution_economics_owner
    issuer = context.execution_economics_issuer
    if (
        type(owner) is not CtpExecutionEconomicsOwner
        or owner._issuer is not issuer
        or not isinstance(issuer, CtpExecutionRulesSnapshotEvidenceIssuer)
        or not isinstance(owner, ExecutionRulesSnapshotPort)
    ):
        return None
    try:
        position_evidence_issuer = (
            issuer
            if isinstance(issuer, CtpOrderRulesPositionEvidenceIssuer)
            else None
        )
        return CtpOrderRulesProvider(
            route,
            context,
            owner,
            position_evidence_issuer=position_evidence_issuer,
        )
    except Exception:
        return None


def _request_needs_position_evidence(request: ExecutionRuleRequest) -> bool:
    """Mirror the CTP evaluator's exact close-position dependency predicate."""

    return (
        request.position_mode == "net" and request.offset is None
    ) or (request.offset is not None and request.offset != "open")


def _unavailable(
    request: ExecutionRuleRequest,
    reason_code: str,
) -> ExecutionRuleDecision:
    """Create a typed advisory-unavailable result with no authorization facts."""

    return ExecutionRuleDecision(
        request=request,
        status="unavailable",
        reason_code=reason_code,
        facts=None,
        normalized_position_side=None,
        normalized_offset=None,
        normalized_time_in_force=None,
        commission_estimate=None,
        commission_currency=None,
        commission_charge_unit=None,
        position_evidence_validated=False,
    )
