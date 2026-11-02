"""Managed typed-order bridge for the selected CTP simulation owner.

The bridge translates provider-neutral requests into durable execution SDK
intents, then delegates the complete claim/admission/dispatch lifecycle to the
already configured ``ManagedExecutionFacade`` or
``ManagedCancellationFacade``. It never constructs Risk or BasicAdmission
authority, allocates native identity, calls a TraderClient write method, or
exposes CTP identity in its result.

Only a CTP ``simulation`` scope backed by the exact registered-simulation
authority is supported. The current managed CTP owner does not implement a
production/live authority. Missing trusted account mapping or cancellation
authorization validators keep the affected operation unavailable.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock, current_thread
from typing import Callable, Optional, Protocol

from bt_api_base.execution_correlation_protocol import (
    CancelOrderRequestProtocol,
    OrderCorrelation,
    OrderRequestProtocol,
    VerifiedOrderAcknowledgement,
)
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_execution.basic_admission import ExecutionAdmissionContextV1
from bt_api_execution.cancellation import ManagedCancellationFacade
from bt_api_execution.contracts import (
    CancelIntent,
    CancelRecord,
    ExecutionScope,
    ExecutionState,
    OrderIntent,
    PositionEffect,
    Side,
    TimeInForce,
)
from bt_api_execution.facade import ManagedExecutionFacade
from bt_api_execution.store import ExecutionRecord

from .managed_correlation_binding import CtpManagedCorrelationBinding
from .managed_order_authority import CtpRegisteredSimNativeAuthority

__all__ = (
    "CtpManagedOrderBridgeError",
    "CtpManagedOrderExecutionBridge",
    "CtpOrderAccountValidator",
    "CtpCancelAuthorizationValidator",
    "CtpSubmitContextResolver",
)


class CtpManagedOrderBridgeError(RuntimeError):
    """Finite error code for provider-internal managed-order failures."""

    def __init__(self, code: str) -> None:
        self.code = (
            code
            if type(code) is str and re.fullmatch(r"[a-z0-9_]{1,64}", code)
            else "ctp_managed_order_unavailable"
        )
        super().__init__(self.code)


class CtpOrderAccountValidator(Protocol):
    """Trusted selected-session check for a typed request's account mapping.

    This callback is a routing check, not write authority. The registered
    Basic/Risk gates still make the actual submit authorization decision.
    It must be created by SDK composition from the selected account/session,
    never from the request alone.
    """

    def __call__(
        self,
        request: OrderRequestProtocol | CancelOrderRequestProtocol,
        scope: ExecutionScope,
        trader_client: object,
    ) -> bool: ...


class CtpCancelAuthorizationValidator(Protocol):
    """Trusted caller/action validator for one cancellation request.

    The result does not replace the cancellation Risk gate. It establishes
    the caller's exact cancellation authority before the durable cancel
    facade is entered; that facade then obtains its own one-use Risk receipt.
    """

    def __call__(
        self,
        authorization: object,
        correlation: OrderCorrelation,
        request: CancelOrderRequestProtocol,
        scope: ExecutionScope,
    ) -> bool: ...


class CtpSubmitContextResolver(Protocol):
    """Resolve caller authority into a trusted BasicAdmission context.

    ``ExecutionAdmissionContextV1`` is a data contract, not a signed token.
    This resolver must be supplied by the authenticated SDK runtime and must
    reject caller constructed or mismatched contexts before returning one.
    """

    def __call__(
        self,
        authorization: object,
        correlation: OrderCorrelation,
        request: OrderRequestProtocol,
        scope: ExecutionScope,
    ) -> ExecutionAdmissionContextV1 | None: ...


class _BridgeClaim:
    """One-shot local binding of an exact typed operation and its intent."""

    __slots__ = (
        "_bridge",
        "_operation",
        "_correlation",
        "_request",
        "_request_snapshot",
        "_issuer_reservation",
        "_provider_reservation",
        "_authorization",
        "_admission_context",
        "_intent",
        "_cancel_intent",
        "_response",
        "_used",
        "_seal",
    )

    def __init__(
        self,
        bridge: object,
        operation: str,
        correlation: OrderCorrelation,
        request: object,
        request_snapshot: tuple[object, ...],
        issuer_reservation: object,
        provider_reservation: object,
        authorization: object,
        admission_context: ExecutionAdmissionContextV1 | None,
        intent: OrderIntent | None,
        cancel_intent: CancelIntent | None,
        seal: object,
    ) -> None:
        self._bridge = bridge
        self._operation = operation
        self._correlation = correlation
        self._request = request
        self._request_snapshot = request_snapshot
        self._issuer_reservation = issuer_reservation
        self._provider_reservation = provider_reservation
        self._authorization = authorization
        self._admission_context = admission_context
        self._intent = intent
        self._cancel_intent = cancel_intent
        self._response = None
        self._used = False
        self._seal = seal

    def __repr__(self) -> str:
        return "<CTP managed order claim>"


class CtpManagedOrderExecutionBridge:
    """Route exact CTP typed requests through existing durable SDK facades.

    The provider reservation is only an opaque correlation binding. The
    submit facade's CTP port is the sole component that reserves ``OrderRef``
    and its runtime identity, after this bridge has constructed the exact
    immutable ``OrderIntent``.

    ``request_account_validator`` must bind the request to the selected CTP
    account/session. ``cancel_authorization_validator`` must validate the
    trusted caller's cancellation permission; no such permission is inferred
    from a CTP environment label, correlation, reservation, or truthy value.
    """

    def __init__(
        self,
        *,
        provider_owner: object,
        binding: CtpManagedCorrelationBinding,
        scope: ExecutionScope,
        execution_facade: ManagedExecutionFacade,
        cancellation_facade: ManagedCancellationFacade,
        submit_port: object,
        cancel_port: object,
        registered_sim_authority: CtpRegisteredSimNativeAuthority,
        selected_trader_client: object,
        request_account_validator: Optional[CtpOrderAccountValidator],
        cancel_authorization_validator: Optional[CtpCancelAuthorizationValidator],
        submit_context_resolver: Optional[CtpSubmitContextResolver],
    ) -> None:
        if provider_owner is None or type(binding) is not CtpManagedCorrelationBinding:
            raise CtpManagedOrderBridgeError("ctp_managed_order_binding_unavailable")
        if type(scope) is not ExecutionScope or (
            scope.provider != "ctp"
            or scope.environment != "simulation"
            or type(scope.trading_day) is not str
            or len(scope.trading_day) != 8
            or not scope.trading_day.isascii()
            or not scope.trading_day.isdigit()
        ):
            raise CtpManagedOrderBridgeError("ctp_simulation_scope_required")
        if type(execution_facade) is not ManagedExecutionFacade or (
            type(cancellation_facade) is not ManagedCancellationFacade
        ):
            raise CtpManagedOrderBridgeError("ctp_managed_execution_facades_unavailable")
        if (
            execution_facade.store is not cancellation_facade.store
            or execution_facade.store is not binding._execution_store
            or execution_facade.scope != scope
            or cancellation_facade.scope != scope
            or execution_facade._allow_unprotected is not False
            or cancellation_facade._allow_unprotected is not False
            or execution_facade._admission_gate is None
            or execution_facade._basic_admission_gate is None
            or cancellation_facade._admission_gate is None
        ):
            raise CtpManagedOrderBridgeError("ctp_managed_execution_binding_invalid")
        if (
            type(registered_sim_authority) is not CtpRegisteredSimNativeAuthority
            or selected_trader_client is None
        ):
            raise CtpManagedOrderBridgeError("ctp_registered_sim_authority_unavailable")
        try:
            authority_bound = registered_sim_authority.is_bound_to_client(
                selected_trader_client
            )
        except Exception:
            authority_bound = False
        if type(authority_bound) is not bool or authority_bound is not True:
            raise CtpManagedOrderBridgeError("ctp_registered_sim_client_mismatch")

        owner = self._get_submit_owner(submit_port)
        if owner is None or getattr(cancel_port, "_owner", None) is not owner:
            raise CtpManagedOrderBridgeError("ctp_managed_owner_mismatch")
        store = execution_facade.store
        if (
            getattr(submit_port, "_store", None) is not store
            or getattr(cancel_port, "_store", None) is not store
            or getattr(submit_port, "_scope", None) != scope
            or getattr(cancel_port, "_scope", None) != scope
            or getattr(owner, "_store", None) is not store
            or getattr(owner, "_scope", None) != scope
            or getattr(owner, "_trader", None) is not selected_trader_client
            or getattr(submit_port, "_writer_lease", None)
            is not getattr(cancel_port, "_writer_lease", None)
            or getattr(owner, "_writer_lease", None)
            is not getattr(submit_port, "_writer_lease", None)
            or execution_facade._borrowed_writer_lease
            is not getattr(submit_port, "_writer_lease", None)
            or getattr(binding, "_provider_owner", None) is not provider_owner
            or getattr(binding, "_registered_sim_authority", None)
            is not registered_sim_authority
        ):
            raise CtpManagedOrderBridgeError("ctp_managed_owner_binding_invalid")
        if (
            getattr(submit_port, "_gate", None)
            is not execution_facade._basic_admission_gate
            or getattr(submit_port, "_risk_bridge", None)
            is not execution_facade._admission_gate
            or getattr(cancel_port, "_admission_gate", None)
            is not cancellation_facade._admission_gate
        ):
            raise CtpManagedOrderBridgeError("ctp_managed_gate_binding_invalid")

        owner_thread = getattr(owner, "_thread", None)
        submit_thread = getattr(submit_port, "_thread", None)
        cancel_thread = getattr(cancel_port, "_thread", None)
        if (
            owner_thread is None
            or owner_thread is not submit_thread
            or owner_thread is not cancel_thread
            or owner_thread is not current_thread()
        ):
            raise CtpManagedOrderBridgeError("ctp_managed_owner_thread_unavailable")
        session_generation_id = getattr(owner, "_session_generation_id", None)
        if (
            type(session_generation_id) is not str
            or not session_generation_id
            or session_generation_id != session_generation_id.strip()
        ):
            raise CtpManagedOrderBridgeError("ctp_managed_session_generation_unavailable")
        if request_account_validator is not None and not callable(
            request_account_validator
        ):
            raise CtpManagedOrderBridgeError("ctp_order_account_validator_invalid")
        if cancel_authorization_validator is not None and not callable(
            cancel_authorization_validator
        ):
            raise CtpManagedOrderBridgeError("ctp_cancel_authorization_validator_invalid")
        if submit_context_resolver is not None and not callable(submit_context_resolver):
            raise CtpManagedOrderBridgeError("ctp_submit_context_resolver_invalid")

        self._provider_owner = provider_owner
        self._binding = binding
        self._scope = scope
        self._store = store
        self._execution_facade = execution_facade
        self._cancellation_facade = cancellation_facade
        self._submit_port = submit_port
        self._cancel_port = cancel_port
        self._owner = owner
        self._trader_client = selected_trader_client
        self._registered_sim_authority = registered_sim_authority
        self._request_account_validator = request_account_validator
        self._cancel_authorization_validator = cancel_authorization_validator
        self._submit_context_resolver = submit_context_resolver
        self._session_generation_id = session_generation_id
        self._thread = owner_thread
        self._seal = object()
        self._lock = RLock()
        self._claims: dict[tuple[str, str], _BridgeClaim] = {}
        self._responses: dict[tuple[str, str], object] = {}

    @property
    def session_generation_id(self) -> str:
        """Provider-internal generation of the exact selected execution owner."""

        self._require_owner_thread()
        current = getattr(self._owner, "_session_generation_id", None)
        if current != self._session_generation_id:
            raise CtpManagedOrderBridgeError("ctp_managed_session_generation_changed")
        return self._session_generation_id

    def claim_correlated_order(
        self,
        operation: str,
        correlation: OrderCorrelation,
        request: OrderRequestProtocol | CancelOrderRequestProtocol,
        *,
        issuer_reservation: object,
        provider_reservation: object,
        authorization: object,
    ) -> object:
        """Bind one exact typed request before its durable facade dispatch.

        This local handle is not a durable admission or dispatch claim. The
        ``ManagedExecutionFacade`` / ``ManagedCancellationFacade`` creates and
        validates those durable one-shot claims in the actual write call.
        """

        self._require_owner_thread()
        if operation not in {"submit", "cancel"}:
            raise CtpManagedOrderBridgeError("ctp_order_operation_unsupported")
        intent_id = self._managed_intent_id(
            correlation,
            issuer_reservation=issuer_reservation,
            provider_reservation=provider_reservation,
        )
        request_snapshot = self._request_snapshot(operation, request)
        self._validate_account(request)

        context: ExecutionAdmissionContextV1 | None = None
        if operation == "submit":
            if not isinstance(request, OrderRequestProtocol):
                raise CtpManagedOrderBridgeError("ctp_order_request_untyped")
            context = self._resolve_submit_context(authorization, correlation, request)
            intent = self._build_order_intent(intent_id, correlation, request)
            if context.execution_scope != intent.scope or context.action != "orders.submit":
                raise CtpManagedOrderBridgeError("ctp_order_admission_context_mismatch")
            cancel_intent = None
        else:
            if not isinstance(request, CancelOrderRequestProtocol):
                raise CtpManagedOrderBridgeError("ctp_cancel_request_untyped")
            self._validate_cancel_authorization(
                authorization, correlation, request
            )
            intent, cancel_intent = self._build_cancel_intent(
                intent_id,
                correlation,
                request,
            )

        claim = _BridgeClaim(
            self,
            operation,
            correlation,
            request,
            request_snapshot,
            issuer_reservation,
            provider_reservation,
            authorization,
            context,
            intent,
            cancel_intent,
            self._seal,
        )
        key = (correlation.reservation_ref, operation)
        with self._lock:
            if key in self._claims:
                raise CtpManagedOrderBridgeError("ctp_order_claim_already_used")
            if len(self._claims) >= 4096:
                raise CtpManagedOrderBridgeError("ctp_order_claim_capacity_exhausted")
            self._claims[key] = claim
        return claim

    def submit_correlated_order(
        self,
        correlation: OrderCorrelation,
        request: OrderRequestProtocol,
        *,
        issuer_reservation: object,
        provider_reservation: object,
        claim: object,
        authorization: object,
    ) -> ExecutionRecord:
        """Run one exact intent through the same configured managed submit facade."""

        self._require_owner_thread()
        selected = self._consume_claim(
            "submit",
            correlation,
            request,
            issuer_reservation,
            provider_reservation,
            claim,
            authorization,
        )
        self._validate_account(request)
        context = self._resolve_submit_context(authorization, correlation, request)
        if (
            selected._intent is None
            or context is not selected._admission_context
            or context.execution_scope != selected._intent.scope
        ):
            raise CtpManagedOrderBridgeError("ctp_order_admission_context_mismatch")
        # The correlated runtime is simulation-only.  After the trusted
        # dispatch-stage resolver has revalidated the operator bounds, retain
        # the preconfigured CTP native arm for this exact catalog-validated
        # instrument before entering the durable Basic/Risk facade.  This
        # call consumes only SDK-owned immutable arm templates; it never
        # constructs strategy, cycle, or preflight facts from the request.
        self._ensure_registered_sim_arm_for(request)
        try:
            record = self._execution_facade.submit(
                selected._intent,
                self._submit_port,
                admission_context=context,
            )
        except Exception:
            raise CtpManagedOrderBridgeError("ctp_managed_submit_outcome_unknown") from None
        if type(record) is not ExecutionRecord:
            raise CtpManagedOrderBridgeError("ctp_managed_submit_outcome_unknown")
        with self._lock:
            self._responses[(correlation.reservation_ref, "submit")] = record
        selected._response = record
        return record

    def _ensure_registered_sim_arm_for(
        self,
        request: OrderRequestProtocol,
    ) -> None:
        """Verify the selected simulation authority's existing exact arm."""

        if (
            current_thread() is not self._thread
            or self._scope.provider != "ctp"
            or self._scope.environment != "simulation"
            or getattr(self._owner, "_thread", None) is not self._thread
            or getattr(self._owner, "_trader", None) is not self._trader_client
        ):
            raise CtpManagedOrderBridgeError(
                "ctp_registered_sim_arm_unavailable"
            )
        authority = self._registered_sim_authority
        if (
            type(authority) is not CtpRegisteredSimNativeAuthority
            or authority.is_bound_to_client(self._trader_client) is not True
        ):
            raise CtpManagedOrderBridgeError(
                "ctp_registered_sim_arm_unavailable"
            )
        instrument = getattr(request, "instrument", None)
        if type(instrument) is not MarketInstrumentKey:
            raise CtpManagedOrderBridgeError(
                "ctp_registered_sim_arm_unavailable"
            )
        instrument_id = instrument.instrument_id
        exchange_id = instrument.venue
        if (
            type(instrument_id) is not str
            or not instrument_id
            or instrument_id != instrument_id.strip()
            or type(exchange_id) is not str
            or not exchange_id
            or exchange_id != exchange_id.strip()
            or instrument.provider != "ctp"
            or type(instrument.environment) is not str
            or not instrument.environment
            or instrument.native_symbol != request.symbol
        ):
            raise CtpManagedOrderBridgeError(
                "ctp_registered_sim_arm_unavailable"
            )
        try:
            result = authority.ensure_registered_sim_arm_for(
                instrument_id,
                exchange_id,
            )
        except Exception:
            raise CtpManagedOrderBridgeError(
                "ctp_registered_sim_arm_unavailable"
            ) from None
        if (
            not isinstance(result, Mapping)
            or result.get("armed") is not True
            or result.get("instrument") != instrument_id
            or (
                "exchange_id" in result
                and result.get("exchange_id") != exchange_id
            )
        ):
            raise CtpManagedOrderBridgeError(
                "ctp_registered_sim_arm_unavailable"
            )

    def cancel_correlated_order(
        self,
        correlation: OrderCorrelation,
        request: CancelOrderRequestProtocol,
        *,
        issuer_reservation: object,
        provider_reservation: object,
        claim: object,
        authorization: object,
    ) -> CancelRecord:
        """Run one cancellation through the durable facade and same CTP owner.

        ``prepare_target`` is an internal hook inside the single facade call:
        the facade first creates the durable local claim, then refreshes and
        retains the exact target before the Risk gate issues its one-use
        ``CancelDispatchClaimReceiptV1``. No split claim/preparation handle is
        exposed to Backtrader.
        """

        self._require_owner_thread()
        selected = self._consume_claim(
            "cancel",
            correlation,
            request,
            issuer_reservation,
            provider_reservation,
            claim,
            authorization,
        )
        self._validate_account(request)
        self._validate_cancel_authorization(authorization, correlation, request)
        cancel_intent = selected._cancel_intent
        if type(cancel_intent) is not CancelIntent:
            raise CtpManagedOrderBridgeError("ctp_cancel_intent_unavailable")
        prepare_target = getattr(self._cancel_port, "prepare_target", None)
        if not callable(prepare_target):
            raise CtpManagedOrderBridgeError("ctp_cancel_target_preparation_unavailable")
        try:
            record = self._cancellation_facade.cancel(
                cancel_intent,
                self._cancel_port,
                before_dispatch=prepare_target,
            )
        except Exception:
            raise CtpManagedOrderBridgeError("ctp_managed_cancel_outcome_unknown") from None
        if type(record) is not CancelRecord:
            raise CtpManagedOrderBridgeError("ctp_managed_cancel_outcome_unknown")
        with self._lock:
            self._responses[(correlation.reservation_ref, "cancel")] = record
        selected._response = record
        return record

    def verify_order_response(
        self,
        correlation: OrderCorrelation,
        response: object,
        *,
        issuer_reservation: object,
        provider_reservation: object,
    ) -> VerifiedOrderAcknowledgement | None:
        """Acknowledge only the exact persisted callback-derived submit record."""

        try:
            self._require_owner_thread()
            intent_id = self._managed_intent_id(
                correlation,
                issuer_reservation=issuer_reservation,
                provider_reservation=provider_reservation,
            )
            with self._lock:
                if self._responses.get((correlation.reservation_ref, "submit")) is not response:
                    return None
            if type(response) is not ExecutionRecord:
                return None
            current_intent = self._store.get_intent(intent_id, scope=self._scope)
            current_record = self._store.get(intent_id, scope=self._scope)
            if (
                type(current_intent) is not OrderIntent
                or current_intent.intent_id != intent_id
                or current_intent.scope != self._scope
                or type(current_record) is not ExecutionRecord
                or current_record != response
                or current_record.intent_id != intent_id
                or current_record.scope_key != self._scope.key
                or current_record.payload_sha256 != current_intent.fingerprint
                or current_record.dispatch_attempts != 1
                or current_record.review_required
                or current_record.updated_at_ns <= 0
            ):
                return None
            if current_record.state in {
                ExecutionState.ACKED,
                ExecutionState.PARTIALLY_FILLED,
                ExecutionState.FILLED,
            }:
                disposition = "accepted"
            elif current_record.state is ExecutionState.REJECTED:
                disposition = "rejected"
            else:
                return None
            acknowledgement = VerifiedOrderAcknowledgement(
                correlation=correlation,
                operation="submit",
                disposition=disposition,
                observed_at=_utc_from_ns(current_record.updated_at_ns),
            )
            with self._lock:
                self._responses.pop((correlation.reservation_ref, "submit"), None)
                self._claims.pop((correlation.reservation_ref, "submit"), None)
            return acknowledgement
        except Exception:
            return None

    def verify_cancel_response(
        self,
        correlation: OrderCorrelation,
        response: object,
        *,
        issuer_reservation: object,
        provider_reservation: object,
    ) -> VerifiedOrderAcknowledgement | None:
        """Acknowledge only a current callback-derived terminal cancel record."""

        try:
            self._require_owner_thread()
            intent_id = self._managed_intent_id(
                correlation,
                issuer_reservation=issuer_reservation,
                provider_reservation=provider_reservation,
            )
            with self._lock:
                if self._responses.get((correlation.reservation_ref, "cancel")) is not response:
                    return None
            if type(response) is not CancelRecord:
                return None
            current_intent = self._store.get_cancel_intent(
                response.cancel_id,
                scope=self._scope,
            )
            current_record = self._store.get_cancel(
                response.cancel_id,
                scope=self._scope,
            )
            if (
                type(current_intent) is not CancelIntent
                or current_intent != self._response_cancel_intent(correlation)
                or current_intent.target_intent_id != intent_id
                or current_intent.scope != self._scope
                or type(current_record) is not CancelRecord
                or current_record != response
                or current_record.cancel_id != current_intent.cancel_id
                or current_record.scope_key != self._scope.key
                or current_record.target_intent_id != intent_id
                or current_record.provider_order_id != current_intent.provider_order_id
                or current_record.payload_sha256 != current_intent.fingerprint
                or current_record.dispatch_attempts != 1
                or current_record.review_required
                or current_record.updated_at_ns <= 0
            ):
                return None
            if current_record.state is ExecutionState.CANCELLED:
                disposition = "accepted"
            elif current_record.state is ExecutionState.REJECTED:
                disposition = "rejected"
            else:
                return None
            acknowledgement = VerifiedOrderAcknowledgement(
                correlation=correlation,
                operation="cancel",
                disposition=disposition,
                observed_at=_utc_from_ns(current_record.updated_at_ns),
            )
            with self._lock:
                self._responses.pop((correlation.reservation_ref, "cancel"), None)
                self._claims.pop((correlation.reservation_ref, "cancel"), None)
            return acknowledgement
        except Exception:
            return None

    def _response_cancel_intent(self, correlation: OrderCorrelation) -> CancelIntent | None:
        with self._lock:
            claim = self._claims.get((correlation.reservation_ref, "cancel"))
        if (
            type(claim) is not _BridgeClaim
            or claim._correlation is not correlation
            or claim._seal is not self._seal
        ):
            return None
        return claim._cancel_intent

    def _consume_claim(
        self,
        operation: str,
        correlation: OrderCorrelation,
        request: object,
        issuer_reservation: object,
        provider_reservation: object,
        claim: object,
        authorization: object,
    ) -> _BridgeClaim:
        if type(correlation) is not OrderCorrelation:
            raise CtpManagedOrderBridgeError("ctp_order_correlation_untyped")
        key = (correlation.reservation_ref, operation)
        with self._lock:
            current = self._claims.get(key)
            if (
                type(claim) is not _BridgeClaim
                or current is not claim
                or claim._bridge is not self
                or claim._seal is not self._seal
                or claim._used
                or claim._operation != operation
                or claim._correlation is not correlation
                or claim._request is not request
                or claim._issuer_reservation is not issuer_reservation
                or claim._provider_reservation is not provider_reservation
                or claim._authorization is not authorization
                or claim._request_snapshot != self._request_snapshot(operation, request)
            ):
                raise CtpManagedOrderBridgeError("ctp_order_claim_binding_invalid")
            claim._used = True
            return claim

    def _managed_intent_id(
        self,
        correlation: OrderCorrelation,
        *,
        issuer_reservation: object,
        provider_reservation: object,
    ) -> str:
        try:
            return self._binding.managed_intent_id(
                provider_reservation,
                correlation,
                issuer_reservation=issuer_reservation,
                provider_owner=self._provider_owner,
            )
        except Exception:
            raise CtpManagedOrderBridgeError("ctp_order_reservation_invalid") from None

    def _resolve_submit_context(
        self,
        authorization: object,
        correlation: OrderCorrelation,
        request: OrderRequestProtocol,
    ) -> ExecutionAdmissionContextV1:
        resolver = self._submit_context_resolver
        if not callable(resolver):
            raise CtpManagedOrderBridgeError("ctp_order_admission_context_unavailable")
        try:
            context = resolver(authorization, correlation, request, self._scope)
        except Exception:
            context = None
        if (
            type(context) is not ExecutionAdmissionContextV1
            or context.execution_scope != self._scope
            or context.action != "orders.submit"
        ):
            raise CtpManagedOrderBridgeError("ctp_order_admission_context_invalid")
        return context

    def _validate_cancel_authorization(
        self,
        authorization: object,
        correlation: OrderCorrelation,
        request: CancelOrderRequestProtocol,
    ) -> None:
        validator = self._cancel_authorization_validator
        if not callable(validator):
            raise CtpManagedOrderBridgeError("ctp_cancel_authorization_unavailable")
        try:
            valid = validator(authorization, correlation, request, self._scope)
        except Exception:
            valid = False
        if type(valid) is not bool or valid is not True:
            raise CtpManagedOrderBridgeError("ctp_cancel_authorization_invalid")

    def _validate_account(
        self,
        request: OrderRequestProtocol | CancelOrderRequestProtocol,
    ) -> None:
        validator = self._request_account_validator
        if not callable(validator):
            raise CtpManagedOrderBridgeError("ctp_order_account_mapping_unavailable")
        try:
            valid = validator(request, self._scope, self._trader_client)
        except Exception:
            valid = False
        if type(valid) is not bool or valid is not True:
            raise CtpManagedOrderBridgeError("ctp_order_account_mapping_invalid")

    def _build_order_intent(
        self,
        intent_id: str,
        correlation: OrderCorrelation,
        request: OrderRequestProtocol,
    ) -> OrderIntent:
        if not isinstance(request, OrderRequestProtocol):
            raise CtpManagedOrderBridgeError("ctp_order_request_untyped")
        instrument = request.instrument
        if (
            type(instrument) is not MarketInstrumentKey
            or instrument.provider != "ctp"
            or instrument.native_symbol != request.symbol
        ):
            raise CtpManagedOrderBridgeError("ctp_order_instrument_identity_invalid")
        if (
            correlation.symbol != request.symbol
            or correlation.client_order_id != request.client_order_id
            or type(request.quantity) is not Decimal
            or not request.quantity.is_finite()
            or request.quantity <= 0
            or request.quantity != request.quantity.to_integral_value()
            or type(request.price) is not Decimal
            or not request.price.is_finite()
            or request.price <= 0
            or type(request.reduce_only) is not bool
            or request.time_in_force not in {"DAY", "GFD"}
        ):
            raise CtpManagedOrderBridgeError("ctp_order_request_invalid")
        side_text = _enum_value(request.side)
        if side_text not in {"buy", "sell"} or _enum_value(request.order_type) != "limit":
            raise CtpManagedOrderBridgeError("ctp_order_type_unsupported")
        offset = request.offset or ("close" if request.reduce_only else "open")
        effects = {
            "open": PositionEffect.OPEN,
            "close": PositionEffect.CLOSE,
            "close_today": PositionEffect.CLOSE_TODAY,
            "close_yesterday": PositionEffect.CLOSE_YESTERDAY,
        }
        effect = effects.get(offset)
        if effect is None or (effect is not PositionEffect.OPEN) != request.reduce_only:
            raise CtpManagedOrderBridgeError("ctp_order_position_effect_unsupported")
        hedge_class = request.hedge_class
        legacy_hedge_flag = getattr(request, "hedge_flag", None)
        if hedge_class is None and legacy_hedge_flag == "1":
            # The neutral request protocol keeps a legacy optional CTP flag for
            # callers that have not populated hedge_class yet. Normalize only
            # the exact speculation flag; all other provider flags stay
            # rejected until the neutral contract supports them.
            hedge_class = "speculation"
        if hedge_class != "speculation" or legacy_hedge_flag not in {None, "1"}:
            raise CtpManagedOrderBridgeError("ctp_order_hedge_class_unsupported")
        position_side_text = request.position_side
        if position_side_text is None:
            if request.position_mode == "dual_side":
                raise CtpManagedOrderBridgeError("ctp_order_position_side_required")
            position_side_text = "net"
        position_sides = {"net": "NET", "long": "LONG", "short": "SHORT"}
        position_side = position_sides.get(position_side_text)
        if position_side is None:
            raise CtpManagedOrderBridgeError("ctp_order_position_side_unsupported")
        try:
            return OrderIntent.limit(
                intent_id=intent_id,
                scope=self._scope,
                signal_id=intent_id,
                instrument=request.symbol,
                side=Side.BUY if side_text == "buy" else Side.SELL,
                quantity=request.quantity,
                price=request.price,
                position_effect=effect,
                time_in_force=TimeInForce.DAY,
                reduce_only=request.reduce_only,
                metadata_version="ctp-managed-correlation-v1",
                tags={},
                position_side=position_side,
                position_bucket={
                    PositionEffect.CLOSE_TODAY: "TODAY",
                    PositionEffect.CLOSE_YESTERDAY: "YESTERDAY",
                }.get(effect, "ALL"),
            )
        except Exception:
            raise CtpManagedOrderBridgeError("ctp_order_intent_invalid") from None

    def _build_cancel_intent(
        self,
        intent_id: str,
        correlation: OrderCorrelation,
        request: CancelOrderRequestProtocol,
    ) -> tuple[OrderIntent, CancelIntent]:
        if not isinstance(request, CancelOrderRequestProtocol) or (
            correlation.symbol != request.symbol
            or request.client_order_id not in {None, correlation.client_order_id}
            or request.order_id is not None
            or type(request.idempotency_key) is not str
            or not request.idempotency_key
        ):
            raise CtpManagedOrderBridgeError("ctp_cancel_request_invalid")
        try:
            intent = self._store.get_intent(intent_id, scope=self._scope)
            record = self._store.get(intent_id, scope=self._scope)
        except Exception:
            raise CtpManagedOrderBridgeError("ctp_cancel_target_unavailable") from None
        if (
            type(intent) is not OrderIntent
            or intent.scope != self._scope
            or type(record) is not ExecutionRecord
            or record.intent_id != intent_id
            or record.scope_key != self._scope.key
            or record.payload_sha256 != intent.fingerprint
            or record.state
            not in {
                ExecutionState.ACKED,
                ExecutionState.PARTIALLY_FILLED,
            }
            or record.dispatch_attempts != 1
            or record.review_required
            or type(record.provider_order_id) is not str
            or not record.provider_order_id
        ):
            raise CtpManagedOrderBridgeError("ctp_cancel_target_not_confirmed")
        cancel_id = _stable_cancel_id(
            self._scope,
            correlation,
            intent_id,
            request.idempotency_key,
        )
        try:
            cancel_intent = CancelIntent(
                cancel_id=cancel_id,
                scope=self._scope,
                target_intent_id=intent_id,
                provider_order_id=record.provider_order_id,
                metadata_version="ctp-managed-correlation-v1",
                tags={},
            )
        except Exception:
            raise CtpManagedOrderBridgeError("ctp_cancel_intent_invalid") from None
        return intent, cancel_intent

    def _request_snapshot(self, operation: str, request: object) -> tuple[object, ...]:
        try:
            if operation == "submit":
                if not isinstance(request, OrderRequestProtocol):
                    raise ValueError
                instrument = request.instrument
                instrument_value = (
                    None
                    if instrument is None
                    else (
                        instrument.provider,
                        instrument.environment,
                        instrument.native_symbol,
                        instrument.venue,
                        instrument.product_type,
                        instrument.instrument_id,
                    )
                )
                return (
                    request.symbol,
                    instrument_value,
                    request.side,
                    request.order_type,
                    request.quantity,
                    request.account_id,
                    request.client_order_id,
                    request.price,
                    request.time_in_force,
                    request.reduce_only,
                    request.idempotency_key,
                    request.quantity_unit,
                    request.position_side,
                    request.offset,
                    request.position_mode,
                    request.hedge_class,
                    getattr(request, "hedge_flag", None),
                )
            if operation == "cancel":
                if not isinstance(request, CancelOrderRequestProtocol):
                    raise ValueError
                return (
                    request.symbol,
                    request.account_id,
                    request.client_order_id,
                    request.order_id,
                    request.idempotency_key,
                )
        except Exception:
            raise CtpManagedOrderBridgeError("ctp_order_request_invalid") from None
        raise CtpManagedOrderBridgeError("ctp_order_operation_unsupported")

    @staticmethod
    def _get_submit_owner(submit_port: object) -> object | None:
        try:
            owner = getattr(submit_port, "execution_owner")
        except Exception:
            return None
        if owner is None or not callable(getattr(owner, "dispatch_submit", None)):
            return None
        return owner

    def _require_owner_thread(self) -> None:
        if current_thread() is not self._thread:
            raise CtpManagedOrderBridgeError("ctp_managed_owner_thread_mismatch")
        try:
            self._owner._assert_worker_thread()
        except Exception:
            raise CtpManagedOrderBridgeError("ctp_managed_owner_thread_mismatch") from None
        if (
            getattr(self._submit_port, "_thread", None) is not self._thread
            or getattr(self._cancel_port, "_thread", None) is not self._thread
            or getattr(self._owner, "_trader", None) is not self._trader_client
            or getattr(self._owner, "_session_generation_id", None)
            != self._session_generation_id
        ):
            raise CtpManagedOrderBridgeError("ctp_managed_owner_binding_changed")


def _enum_value(value: object) -> str | None:
    if type(value) is str:
        return value
    try:
        selected = value.value
    except Exception:
        return None
    return selected if type(selected) is str else None


def _stable_cancel_id(
    scope: ExecutionScope,
    correlation: OrderCorrelation,
    intent_id: str,
    idempotency_key: str,
) -> str:
    payload = {
        "schema": "bt_api_ctp.managed_cancel_correlation.v1",
        "scope_key": scope.key,
        "exchange_name": correlation.exchange_name,
        "symbol": correlation.symbol,
        "client_order_id": correlation.client_order_id,
        "reservation_ref": correlation.reservation_ref,
        "target_intent_id": intent_id,
        "idempotency_key": idempotency_key,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii", "strict")
    return "ctp-cancel-v1:" + hashlib.sha256(encoded).hexdigest()


def _utc_from_ns(value: int) -> datetime:
    if type(value) is not int or value <= 0:
        raise ValueError("invalid execution evidence timestamp")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return epoch + timedelta(microseconds=value // 1_000)
