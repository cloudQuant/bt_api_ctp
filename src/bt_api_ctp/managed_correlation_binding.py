"""Durably join neutral SDK correlations to CTP order identities.

The neutral issuer and the CTP execution store are separate durable
authorities. This module joins them as a recoverable saga: it validates the
issuer's exact opaque reservation, then idempotently asks the existing CTP
execution store to reserve its native identity. A retry can recover the same
reservation after a process restart; it cannot create a second dispatch claim.

This module never invokes CTP native write methods. The returned provider
reservation is opaque and its native fields stay inside the CTP provider.
Dispatch and UNKNOWN handling remain owned by the execution store's one-shot
claim/session path. RegisteredSim capacity is an optional simulation-only
projection from the exact authority injected by the selected provider; this
module never constructs an authority or uses it for a live route.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Optional

from bt_api_base.execution_capacity_protocol import (
    OrderCapacitySnapshot,
    unavailable_order_capacity_snapshot,
)
from bt_api_base.execution_correlation_protocol import (
    ManagedOrderObservationIssuer,
    OrderCorrelation,
    OrderCorrelationIssuer,
)

if TYPE_CHECKING:
    from bt_api_execution.contracts import ExecutionScope
    from bt_api_execution.store import CtpOrderIdentityReservation, SqliteExecutionStore
    from bt_api_ctp.managed_order_authority import CtpRegisteredSimNativeAuthority


__all__ = (
    "CtpManagedCorrelationBinding",
    "CtpManagedCorrelationBindingError",
)


_CTP_FUTURE_ROUTE = "CTP___FUTURE"
_CTP_IDENTITY_PREFIX = "sdk-ctp-correlation-v1:"
_ORDER_REF_RE = re.compile(r"^[0-9]{12}$", re.ASCII)
_CAPACITY_SCHEMA = "bt_api_ctp.registered_sim_order_capacity.v1"


class CtpManagedCorrelationBindingError(RuntimeError):
    """Finite, provider-owned error for correlation binding failures."""

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code else "ctp_correlation_binding_failed"
        super().__init__(self.code)


class _ProviderReservation:
    """Opaque correlation handle; native identity is hidden from its repr."""

    __slots__ = (
        "_binding",
        "_correlation",
        "_managed_intent_id",
        "_identity",
        "_seal",
    )

    def __init__(
        self,
        binding: object,
        correlation: object,
        managed_intent_id: str,
        identity: object,
        seal: object,
    ) -> None:
        self._binding = binding
        self._correlation = correlation
        self._managed_intent_id = managed_intent_id
        self._identity = identity
        self._seal = seal

    def __repr__(self) -> str:
        return "_ProviderReservation(<opaque>)"


class CtpManagedCorrelationBinding:
    """Bind one neutral reservation to one durable CTP identity store scope.

    The selected ``CtpOrderRequestProvider`` passes itself as ``provider_owner``
    at construction and on every method call. This prevents an unrelated
    provider instance from borrowing the binding or its RegisteredSim capacity
    projection. ``registered_sim_authority`` must be the exact existing
    simulation authority selected for that provider instance. It is optional;
    when absent, capacity is explicitly unavailable.

    ``issuer=None`` selects a capacity-only instance. In that mode execution
    store and scope must also be absent, and every reservation/native-identity
    method fails closed. This lets a read-only capacity route use the exact
    selected RegisteredSim authority without retaining or inventing any write
    capability.

    The neutral correlation is durable at the issuer. This binding derives a
    stable managed intent key from that exact correlation, but it does not
    reserve a CTP OrderRef. The exact immutable OrderIntent is not known during
    correlation binding; the existing managed submit owner is the sole
    allocator of the OrderRef and its runtime ID after that intent is fixed.
    ``resolve_order_reservation`` revalidates the issuer and reads any native
    identity already written by that owner. Neither recovery path dispatches
    or clears UNKNOWN.
    """

    def __init__(
        self,
        *,
        provider_owner: object,
        issuer: Optional[OrderCorrelationIssuer],
        execution_store: Optional["SqliteExecutionStore"] = None,
        scope: Optional["ExecutionScope"] = None,
        selected_trader_client: Optional[object] = None,
        registered_sim_authority: Optional["CtpRegisteredSimNativeAuthority"] = None,
    ) -> None:
        if provider_owner is None:
            raise CtpManagedCorrelationBindingError("ctp_provider_owner_required")

        if issuer is None:
            if execution_store is not None or scope is not None:
                raise CtpManagedCorrelationBindingError(
                    "ctp_capacity_only_write_dependencies_invalid"
                )
        else:
            if not callable(getattr(issuer, "validate_order_correlation", None)):
                raise CtpManagedCorrelationBindingError(
                    "ctp_order_correlation_issuer_unavailable"
                )
            if execution_store is None or scope is None:
                raise CtpManagedCorrelationBindingError(
                    "ctp_execution_identity_store_unavailable"
                )
            try:
                from bt_api_execution.contracts import ExecutionScope
                from bt_api_execution.store import SqliteExecutionStore
            except ImportError:
                raise CtpManagedCorrelationBindingError(
                    "ctp_managed_execution_unavailable"
                ) from None

            if type(execution_store) is not SqliteExecutionStore:
                raise CtpManagedCorrelationBindingError(
                    "ctp_execution_identity_store_unavailable"
                )
            if type(scope) is not ExecutionScope or scope.provider != "ctp":
                raise CtpManagedCorrelationBindingError("ctp_execution_scope_invalid")
            if type(scope.trading_day) is not str or len(scope.trading_day) != 8:
                raise CtpManagedCorrelationBindingError(
                    "ctp_execution_trading_day_invalid"
                )
            if not scope.trading_day.isascii() or not scope.trading_day.isdigit():
                raise CtpManagedCorrelationBindingError(
                    "ctp_execution_trading_day_invalid"
                )

        if registered_sim_authority is not None:
            try:
                from bt_api_ctp.managed_order_authority import CtpRegisteredSimNativeAuthority
            except ImportError:
                raise CtpManagedCorrelationBindingError(
                    "ctp_registered_sim_authority_unavailable"
                ) from None
            if type(registered_sim_authority) is not CtpRegisteredSimNativeAuthority:
                raise CtpManagedCorrelationBindingError(
                    "ctp_registered_sim_authority_unavailable"
                )
            try:
                is_bound = registered_sim_authority.is_bound_to_client(selected_trader_client)
            except Exception:
                is_bound = False
            if type(is_bound) is not bool or is_bound is not True:
                raise CtpManagedCorrelationBindingError(
                    "ctp_registered_sim_authority_client_mismatch"
                )

        self._provider_owner = provider_owner
        self._issuer = issuer
        self._execution_store = execution_store
        self._scope = scope
        self._registered_sim_authority = registered_sim_authority
        self._seal = object()

    def bind_order_reservation(
        self,
        correlation: OrderCorrelation,
        *,
        issuer_reservation: object,
        provider_owner: object,
    ) -> object:
        """Bind an SDK-issued correlation without allocating native identity.

        The returned opaque handle is deterministic for this exact issuer
        reservation. The execution owner's submit port creates the CTP order
        identity only after it receives the complete immutable OrderIntent.
        """

        self._require_owner(provider_owner)
        self._require_write_dependencies()
        digest = self._validated_digest(correlation, issuer_reservation)
        managed_intent_id = self._managed_intent_id(digest)
        identity = self._read_authoritative_identity(managed_intent_id)
        self._bind_provider_reference(
            managed_intent_id,
            correlation,
            issuer_reservation=issuer_reservation,
        )
        return _ProviderReservation(
            self, correlation, managed_intent_id, identity, self._seal
        )

    def resolve_order_reservation(
        self,
        correlation: OrderCorrelation,
        *,
        issuer_reservation: object,
        provider_owner: object,
    ) -> object | None:
        """Restore the issuer-bound correlation and read any owner reservation.

        An absent CTP identity means the managed submit owner has not yet
        received the exact OrderIntent. This read never reserves an identity.
        """

        self._require_owner(provider_owner)
        self._require_write_dependencies()
        digest = self._validated_digest(correlation, issuer_reservation)
        managed_intent_id = self._managed_intent_id(digest)
        identity = self._read_authoritative_identity(managed_intent_id)
        self._bind_provider_reference(
            managed_intent_id,
            correlation,
            issuer_reservation=issuer_reservation,
        )
        return _ProviderReservation(
            self, correlation, managed_intent_id, identity, self._seal
        )

    def managed_intent_id(
        self,
        provider_reservation: object,
        correlation: OrderCorrelation,
        *,
        issuer_reservation: object,
        provider_owner: object,
    ) -> str:
        """Return this correlation's stable execution-intent key internally.

        The value is a journal identifier, not a provider-native ID. It binds
        the exact neutral correlation to the durable OrderIntent while leaving
        OrderRef allocation and runtime-ID derivation to the submit owner.
        """

        self._require_owner(provider_owner)
        digest = self._validated_digest(correlation, issuer_reservation)
        expected = self._managed_intent_id(digest)
        if (
            type(provider_reservation) is not _ProviderReservation
            or provider_reservation._binding is not self
            or provider_reservation._seal is not self._seal
            or provider_reservation._correlation != correlation
            or provider_reservation._managed_intent_id != expected
        ):
            raise CtpManagedCorrelationBindingError("ctp_provider_reservation_mismatch")
        self._read_authoritative_identity(expected)
        return expected

    def native_identity(
        self,
        provider_reservation: object,
        correlation: OrderCorrelation,
        *,
        issuer_reservation: object,
        provider_owner: object,
    ) -> "CtpOrderIdentityReservation":
        """Revalidate and return native identity for CTP provider-internal use.

        The returned SDK DTO contains the CTP ``order_ref`` and must never be
        returned by ``CtpOrderRequestProvider`` or passed through the SDK's
        public facade to Backtrader. It exists only for the selected CTP
        provider's native request and managed execution handoff.
        """

        self._require_owner(provider_owner)
        self._require_write_dependencies()
        digest = self._validated_digest(correlation, issuer_reservation)
        if (
            type(provider_reservation) is not _ProviderReservation
            or provider_reservation._binding is not self
            or provider_reservation._seal is not self._seal
            or provider_reservation._correlation != correlation
        ):
            raise CtpManagedCorrelationBindingError("ctp_provider_reservation_mismatch")

        managed_intent_id = self._managed_intent_id(digest)
        if provider_reservation._managed_intent_id != managed_intent_id:
            raise CtpManagedCorrelationBindingError("ctp_provider_reservation_mismatch")
        current = self._read_authoritative_identity(managed_intent_id)
        if current is None:
            raise CtpManagedCorrelationBindingError("ctp_provider_reservation_missing")
        if (
            provider_reservation._identity is not None
            and current != provider_reservation._identity
        ):
            raise CtpManagedCorrelationBindingError("ctp_provider_reservation_changed")
        return current

    def order_capacity_facts(self, *, provider_owner: object) -> OrderCapacitySnapshot:
        """Adapt only this selected provider's RegisteredSim authority.

        Other CTP routes, including production/live routes, get a typed
        UNAVAILABLE observation. ``bt_api_base`` owns the neutral DTO; this
        module never imports the parent facade or constructs an authority.
        """

        self._require_owner(provider_owner)
        authority = self._registered_sim_authority
        if authority is None:
            return unavailable_order_capacity_snapshot(
                _CTP_FUTURE_ROUTE,
                "order_capacity_authority_unavailable",
            )
        try:
            facts = authority.registered_sim_order_capacity_facts()
        except Exception:
            return unavailable_order_capacity_snapshot(
                _CTP_FUTURE_ROUTE,
                "order_capacity_authority_unavailable",
            )
        return self._capacity_snapshot(facts)

    def _capacity_snapshot(
        self,
        facts: object,
    ) -> OrderCapacitySnapshot:
        if not isinstance(facts, Mapping) or facts.get("schema") != _CAPACITY_SCHEMA:
            return unavailable_order_capacity_snapshot(
                _CTP_FUTURE_ROUTE,
                "order_capacity_projection_invalid",
            )
        status = facts.get("status")
        if status not in {"OBSERVED", "UNSTABLE_OR_OVER_CAPACITY", "UNAVAILABLE"}:
            return unavailable_order_capacity_snapshot(
                _CTP_FUTURE_ROUTE,
                "order_capacity_projection_invalid",
            )
        if status == "UNAVAILABLE":
            return unavailable_order_capacity_snapshot(
                _CTP_FUTURE_ROUTE,
                "order_capacity_authority_unavailable",
            )

        active = facts.get("active_order_count")
        unknown = facts.get("unknown_order_count")
        terminal = facts.get("terminal_order_count")
        dispatching = facts.get("dispatching_order_count")
        limit = facts.get("terminal_order_limit")
        respected = facts.get("terminal_limit_respected")
        if (
            any(type(value) is not int or value < 0 for value in (active, unknown, terminal, dispatching))
            or dispatching != 0
            or type(limit) is not int
            or limit <= 0
            or type(respected) is not bool
            or facts.get("active_unknown_limit") is not None
            or facts.get("active_unknown_bounded") is not False
            or respected is not (terminal <= limit)
        ):
            return unavailable_order_capacity_snapshot(
                _CTP_FUTURE_ROUTE,
                "order_capacity_projection_invalid",
            )

        expected_status = "OBSERVED" if respected else "UNSTABLE_OR_OVER_CAPACITY"
        if status != expected_status:
            return unavailable_order_capacity_snapshot(
                _CTP_FUTURE_ROUTE,
                "order_capacity_projection_invalid",
            )
        try:
            return OrderCapacitySnapshot(
                exchange_name=_CTP_FUTURE_ROUTE,
                status=expected_status,
                active_order_count=active,
                unknown_order_count=unknown,
                terminal_order_count=terminal,
                dispatching_order_count=0,
                terminal_order_limit=limit,
                terminal_limit_respected=respected,
                active_unknown_limit=None,
                active_unknown_bounded=False,
                reason_code=(
                    "order_capacity_over_capacity"
                    if expected_status == "UNSTABLE_OR_OVER_CAPACITY"
                    else None
                ),
            )
        except Exception:
            return unavailable_order_capacity_snapshot(
                _CTP_FUTURE_ROUTE,
                "order_capacity_projection_invalid",
            )

    def _validated_digest(self, correlation: OrderCorrelation, issuer_reservation: object) -> str:
        self._require_write_dependencies()
        if type(correlation) is not OrderCorrelation:
            raise CtpManagedCorrelationBindingError("ctp_order_correlation_untyped")
        if correlation.exchange_name != _CTP_FUTURE_ROUTE:
            raise CtpManagedCorrelationBindingError("ctp_order_correlation_route_mismatch")
        try:
            valid = self._issuer.validate_order_correlation(
                correlation,
                issuer_reservation=issuer_reservation,
            )
        except Exception:
            raise CtpManagedCorrelationBindingError("ctp_order_issuer_unavailable") from None
        if type(valid) is not bool or valid is not True:
            raise CtpManagedCorrelationBindingError("ctp_order_issuer_reservation_invalid")
        return self._correlation_digest(correlation)

    @staticmethod
    def _correlation_digest(correlation: OrderCorrelation) -> str:
        payload = {
            "schema": "provider_neutral.order_correlation.v1",
            "exchange_name": correlation.exchange_name,
            "symbol": correlation.symbol,
            "client_order_id": correlation.client_order_id,
            "reservation_ref": correlation.reservation_ref,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _managed_intent_id(digest: str) -> str:
        return _CTP_IDENTITY_PREFIX + digest

    def _read_authoritative_identity(
        self,
        managed_intent_id: str,
    ) -> "CtpOrderIdentityReservation | None":
        try:
            identity = self._execution_store.read_ctp_order_identity(
                self._scope,
                managed_intent_id,
            )
        except Exception as error:
            raise self._store_error(error) from None
        if identity is not None:
            self._validate_identity(identity, managed_intent_id)
        return identity

    def _bind_provider_reference(
        self,
        managed_intent_id: str,
        correlation: OrderCorrelation,
        *,
        issuer_reservation: object,
    ) -> None:
        """Persist the optional query-to-correlation restart join.

        This join is neutral identity evidence only. A missing observation
        extension leaves query correlation unavailable, but it does not grant
        or replace the provider's separate submit/cancel claims.
        """

        if not isinstance(self._issuer, ManagedOrderObservationIssuer):
            return
        try:
            bound = self._issuer.bind_provider_order_reference(
                managed_intent_id,
                correlation,
                issuer_reservation=issuer_reservation,
                provider_name=_CTP_FUTURE_ROUTE,
                environment=self._scope.environment,
                account_ref=self._scope.account_ref,
                scope_ref=self._scope.key,
            )
        except Exception:
            raise CtpManagedCorrelationBindingError(
                "ctp_order_observation_binding_unavailable"
            ) from None
        if type(bound) is not bool or bound is not True:
            raise CtpManagedCorrelationBindingError(
                "ctp_order_observation_binding_unavailable"
            )

    def _validate_identity(
        self,
        identity: object,
        managed_intent_id: str,
    ) -> None:
        try:
            from bt_api_execution.store import CtpOrderIdentityReservation
        except ImportError:
            raise CtpManagedCorrelationBindingError("ctp_execution_identity_store_unavailable") from None
        if (
            type(identity) is not CtpOrderIdentityReservation
            or identity.account_key != self._scope.account_key
            or identity.trading_day != self._scope.trading_day
            or identity.scope_key != self._scope.key
            or identity.managed_intent_id != managed_intent_id
            # The active managed submit owner is the only component that can
            # derive this value from the full durable OrderIntent. This module
            # only validates that the value belongs to the typed Store row.
            or type(identity.runtime_order_id) is not str
            or not identity.runtime_order_id
            or type(identity.order_ref) is not str
            or _ORDER_REF_RE.fullmatch(identity.order_ref) is None
            or type(identity.created_at_ns) is not int
            or identity.created_at_ns <= 0
        ):
            raise CtpManagedCorrelationBindingError("ctp_execution_identity_projection_invalid")

    @staticmethod
    def _store_error(error: Exception) -> CtpManagedCorrelationBindingError:
        try:
            from bt_api_execution.errors import IntentConflictError
        except ImportError:
            return CtpManagedCorrelationBindingError("ctp_order_identity_store_unavailable")
        if isinstance(error, IntentConflictError):
            return CtpManagedCorrelationBindingError("ctp_order_identity_conflict")
        return CtpManagedCorrelationBindingError("ctp_order_identity_store_unavailable")

    def _require_owner(self, provider_owner: object) -> None:
        if provider_owner is not self._provider_owner:
            raise CtpManagedCorrelationBindingError("ctp_provider_binding_owner_mismatch")

    def _require_write_dependencies(self) -> None:
        if self._issuer is None:
            raise CtpManagedCorrelationBindingError(
                "ctp_order_correlation_issuer_unavailable"
            )
        if self._execution_store is None or self._scope is None:
            raise CtpManagedCorrelationBindingError(
                "ctp_execution_identity_store_unavailable"
            )
