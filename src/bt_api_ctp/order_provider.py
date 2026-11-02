"""CTP's provider-internal port for intact SDK order requests.

The provider boundary belongs to this package so CTP mapping and identity do
not leak into ``bt_api_py``'s venue mapper registry. The neutral issuer and
durable CTP identity join are injected by SDK route composition. Native claim
validation, dispatch, instrument-catalog attestation, and callback-backed
acknowledgement evidence are delegated to the existing managed execution
facades and exact selected-session reader. A neutral client ID, CTP identity
echo, or feed's installed native gate is never treated as a reservation or
dispatch claim. Missing trusted runtime dependencies remain unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from math import isfinite
from typing import Any, Literal

from bt_api_base.execution_capacity_protocol import (
    OrderCapacitySnapshot,
    unavailable_order_capacity_snapshot,
)
from bt_api_base.execution_correlation_protocol import (
    CancelOrderRequestProtocol,
    OrderCorrelation,
    OrderCorrelationIssuer,
    OrderRequestProtocol,
    SelectedExecutionRoute,
    TypedOrderProviderRegistryPort,
    VerifiedOrderAcknowledgement,
)
from bt_api_base.managed_execution_runtime_protocol import (
    OwnerThreadCallError,
    OwnerThreadCallPort,
)
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_ctp.ctp.client import CtpExecutionGateError


_CTP_FUTURE_ROUTE = "CTP___FUTURE"
_CTP_BINDING_ERROR_CODES = frozenset(
    {
        "ctp_provider_owner_required",
        "ctp_managed_execution_unavailable",
        "ctp_order_correlation_issuer_unavailable",
        "ctp_execution_identity_store_unavailable",
        "ctp_execution_scope_invalid",
        "ctp_execution_trading_day_invalid",
        "ctp_registered_sim_authority_unavailable",
        "ctp_registered_sim_authority_client_mismatch",
        "ctp_registered_sim_client_without_authority",
        "ctp_provider_reservation_mismatch",
        "ctp_provider_reservation_missing",
        "ctp_provider_reservation_changed",
        "ctp_order_correlation_untyped",
        "ctp_order_correlation_route_mismatch",
        "ctp_order_issuer_unavailable",
        "ctp_order_issuer_reservation_invalid",
        "ctp_execution_identity_projection_invalid",
        "ctp_order_identity_store_unavailable",
        "ctp_order_identity_conflict",
        "ctp_capacity_only_write_dependencies_invalid",
    }
)
_OWNER_CALL_CODES = {
    "OWNER_THREAD_CALL_QUEUE_FULL": "ctp_order_owner_call_unavailable",
    "OWNER_THREAD_CALL_NOT_READY": "ctp_order_owner_call_unavailable",
    "OWNER_THREAD_CALL_TIMEOUT_BEFORE_START": "ctp_order_owner_call_unavailable",
    "OWNER_THREAD_CALL_TIMEOUT_AFTER_START": "ctp_order_owner_call_outcome_unknown",
    "OWNER_THREAD_CALL_CLOSED": "ctp_order_owner_call_unavailable",
    "OWNER_THREAD_CALL_UNAVAILABLE": "ctp_order_owner_call_unavailable",
    "OWNER_THREAD_RUNTIME_CLOSING": "ctp_order_owner_call_unavailable",
}


def _enum_text(value: object) -> str | None:
    """Normalize only an explicit string value without guessing a spelling."""

    if type(value) is str:
        return value
    try:
        selected = value.value
    except Exception:
        return None
    return selected if type(selected) is str else None


@dataclass(frozen=True, repr=False, eq=False)
class CtpOrderProviderRuntimeContext:
    """Opaque CTP runtime dependencies for one already-selected feed route.

    SDK composition supplies the exact execution-store scope and, for a
    registered-simulation route, the authority already bound to the selected
    TraderClient. This DTO creates neither object and carries no write grant.
    """

    execution_store: object | None = field(default=None, repr=False, compare=False)
    scope: object | None = field(default=None, repr=False, compare=False)
    registered_sim_authority: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    execution_facade: object | None = field(default=None, repr=False, compare=False)
    cancellation_facade: object | None = field(default=None, repr=False, compare=False)
    submit_port: object | None = field(default=None, repr=False, compare=False)
    cancel_port: object | None = field(default=None, repr=False, compare=False)
    owner_thread_invoker: OwnerThreadCallPort | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    runtime_binding: object | None = field(default=None, repr=False, compare=False)
    order_provider_authority_source: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    instrument_metadata_reader: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    request_account_validator: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    submit_context_resolver: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    cancel_authorization_validator: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    execution_economics_owner: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    execution_economics_issuer: object | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    owner_call_timeout_seconds: float = field(default=5.0, repr=False, compare=False)
    instrument_metadata_timeout_seconds: float = field(
        default=5.0,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.owner_thread_invoker is not None and not isinstance(
            self.owner_thread_invoker,
            OwnerThreadCallPort,
        ):
            raise TypeError("owner_thread_invoker must satisfy OwnerThreadCallPort")
        for name in (
            "request_account_validator",
            "submit_context_resolver",
            "cancel_authorization_validator",
        ):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise TypeError(f"{name} must be callable")
        if (self.execution_economics_owner is None) != (
            self.execution_economics_issuer is None
        ):
            raise TypeError("execution economics owner and issuer must be supplied together")
        if self.order_provider_authority_source is not None and type(
            self.order_provider_authority_source
        ) in (bool, str, bytes, int, float):
            raise TypeError("order_provider_authority_source must be opaque")
        for name in ("owner_call_timeout_seconds", "instrument_metadata_timeout_seconds"):
            value = getattr(self, name)
            if (
                type(value) not in (int, float)
                or isinstance(value, bool)
                or not isfinite(float(value))
                or not 0.01 <= float(value) <= 30.0
            ):
                raise TypeError(f"{name} must be a finite timeout from 0.01 to 30 seconds")

    def __repr__(self) -> str:
        return "<CTP order provider runtime context>"


def _binding_error_code(error: BaseException) -> str:
    """Return only a finite binding-owned code; hide arbitrary exception data."""

    try:
        from bt_api_ctp.managed_correlation_binding import CtpManagedCorrelationBindingError
    except Exception:
        return "ctp_managed_order_binding_unavailable"
    if type(error) is not CtpManagedCorrelationBindingError:
        return "ctp_managed_order_binding_unavailable"
    code = getattr(error, "code", None)
    if type(code) is str and code in _CTP_BINDING_ERROR_CODES:
        return code
    return "ctp_managed_order_binding_unavailable"


def _create_ctp_order_provider(
    route: SelectedExecutionRoute,
    issuer: OrderCorrelationIssuer | None,
) -> CtpOrderRequestProvider:
    """Create the CTP port only for the exact selected route and runtime type."""

    if type(route) is not SelectedExecutionRoute or route.exchange_name != _CTP_FUTURE_ROUTE:
        raise CtpExecutionGateError("ctp_order_provider_route_unavailable")
    context = route.execution_context
    if context is not None and type(context) is not CtpOrderProviderRuntimeContext:
        raise CtpExecutionGateError("ctp_order_provider_runtime_context_invalid")
    if context is not None and (
        context.runtime_binding is None or context.owner_thread_invoker is None
    ):
        raise CtpExecutionGateError("ctp_order_provider_runtime_binding_unavailable")
    if context is None:
        if issuer is not None:
            raise CtpExecutionGateError("ctp_order_provider_runtime_context_unavailable")
        return CtpOrderRequestProvider(route.client, environment=route.environment)

    if issuer is None:
        return CtpOrderRequestProvider(
            route.client,
            environment=route.environment,
            runtime_context=context,
            registered_sim_authority=context.registered_sim_authority,
        )
    if context.execution_store is None or context.scope is None:
        raise CtpExecutionGateError("ctp_order_provider_runtime_context_incomplete")
    return CtpOrderRequestProvider(
        route.client,
        environment=route.environment,
        issuer=issuer,
        execution_store=context.execution_store,
        scope=context.scope,
        registered_sim_authority=context.registered_sim_authority,
        runtime_context=context,
    )


def register_ctp_typed_order_provider(
    registry: TypedOrderProviderRegistryPort,
) -> None:
    """Register CTP's exact typed-order provider factory with SDK composition."""

    if not isinstance(registry, TypedOrderProviderRegistryPort):
        raise TypeError("registry must satisfy TypedOrderProviderRegistryPort")
    registry.register(_CTP_FUTURE_ROUTE, _create_ctp_order_provider)


class CtpOrderRequestProvider:
    """Provider port selected by SDK composition for one CTP request feed.

    This object is not a BT-facing transport. It is intended to be held by the
    SDK's managed execution owner, which must retain and validate the exact
    request and reservation before invoking it. Issuer and store dependencies
    are injected from trusted SDK composition; no local ID allocator is used.
    """

    def __init__(
        self,
        feed: Any,
        *,
        environment: str | None = None,
        issuer: OrderCorrelationIssuer | None = None,
        execution_store: object | None = None,
        scope: object | None = None,
        registered_sim_authority: object | None = None,
        runtime_context: CtpOrderProviderRuntimeContext | None = None,
    ) -> None:
        self._feed = feed
        self._trader_client = self._require_selected_feed(feed)
        self._environment = environment if type(environment) is str else None
        self._binding: Any | None = None
        self._bridge: Any | None = None
        self._issuer = issuer
        self._execution_store = execution_store
        self._scope = scope
        if runtime_context is not None and type(runtime_context) is not CtpOrderProviderRuntimeContext:
            raise CtpExecutionGateError("ctp_order_provider_runtime_context_invalid")
        self._runtime_context = runtime_context
        if runtime_context is not None:
            if registered_sim_authority is not runtime_context.registered_sim_authority:
                raise CtpExecutionGateError("ctp_registered_sim_authority_context_mismatch")
            if issuer is not None and (
                execution_store is not runtime_context.execution_store
                or scope is not runtime_context.scope
            ):
                raise CtpExecutionGateError("ctp_order_provider_runtime_context_mismatch")
        if issuer is None:
            if execution_store is not None or scope is not None:
                raise CtpExecutionGateError("ctp_order_provider_capacity_context_invalid")
        elif execution_store is None or scope is None:
            raise CtpExecutionGateError("ctp_managed_order_binding_configuration_incomplete")

    @staticmethod
    def _require_selected_feed(feed: Any) -> Any:
        try:
            from bt_api_ctp.ctp.client import TraderClient
            from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
        except Exception:
            raise CtpExecutionGateError("ctp_order_provider_feed_unavailable") from None
        if type(feed) is not CtpRequestDataFuture:
            raise CtpExecutionGateError("ctp_order_provider_feed_not_canonical")
        try:
            client = feed.trader_client
        except Exception:
            raise CtpExecutionGateError("ctp_order_provider_client_unavailable") from None
        if type(client) is not TraderClient:
            raise CtpExecutionGateError("ctp_order_provider_client_not_canonical")
        return client

    def _call_on_owner_thread(self, callback: Any) -> Any:
        context = self._runtime_context
        invoker = None if context is None else context.owner_thread_invoker
        if invoker is None:
            raise CtpExecutionGateError("ctp_order_owner_call_unavailable")

        def checked_callback() -> Any:
            self._assert_selected_feed_current_on_owner_thread()
            self._validate_runtime_binding_on_owner_thread()
            return callback()

        try:
            return invoker.call_on_owner_thread(
                checked_callback,
                timeout_seconds=float(context.owner_call_timeout_seconds),
            )
        except OwnerThreadCallError as error:
            code = getattr(error, "code", None)
            raise CtpExecutionGateError(
                _OWNER_CALL_CODES.get(code, "ctp_order_owner_call_unavailable")
            ) from None
        except CtpExecutionGateError:
            raise
        except Exception:
            # A callback exception is post-entry. The managed facade below
            # converts uncertain native dispatch into UNKNOWN; this outer
            # boundary never retries the callback.
            raise CtpExecutionGateError("ctp_order_owner_call_failed") from None

    def _assert_selected_feed_current_on_owner_thread(self) -> None:
        try:
            connected = self._feed._connected
            current_client = self._feed.trader_client
        except Exception:
            raise CtpExecutionGateError("ctp_order_provider_feed_unavailable") from None
        if connected is not True or current_client is not self._trader_client:
            raise CtpExecutionGateError("ctp_order_provider_feed_binding_changed")

    def _validate_runtime_binding_on_owner_thread(self) -> None:
        context = self._runtime_context
        runtime_binding = None if context is None else context.runtime_binding
        if runtime_binding is None:
            raise CtpExecutionGateError("ctp_order_provider_runtime_binding_unavailable")
        try:
            from bt_api_ctp.feeds.live_ctp_feed import _CtpRuntimeOwnedRequestFeedBinding

            if type(runtime_binding) is not _CtpRuntimeOwnedRequestFeedBinding:
                raise CtpExecutionGateError("ctp_order_provider_runtime_binding_invalid")
            valid = runtime_binding.validate_provider_context(
                context,
                exchange_name=_CTP_FUTURE_ROUTE,
                selected_feed=self._feed,
                trader_client=self._trader_client,
            )
        except CtpExecutionGateError:
            raise
        except Exception:
            valid = False
        if type(valid) is not bool or valid is not True:
            raise CtpExecutionGateError("ctp_order_provider_runtime_binding_mismatch")

    def _ensure_binding_on_owner_thread(self) -> Any:
        if self._binding is not None:
            return self._binding
        context = self._runtime_context
        if context is None:
            raise CtpExecutionGateError("ctp_order_provider_runtime_context_unavailable")
        if self._issuer is None:
            execution_store = None
            scope = None
        else:
            execution_store = self._execution_store
            scope = self._scope
            if execution_store is None or scope is None:
                raise CtpExecutionGateError("ctp_order_identity_store_unavailable")
        try:
            from bt_api_ctp.managed_correlation_binding import CtpManagedCorrelationBinding

            binding = CtpManagedCorrelationBinding(
                provider_owner=self,
                issuer=self._issuer,
                execution_store=execution_store,
                scope=scope,
                selected_trader_client=(
                    self._trader_client
                    if context.registered_sim_authority is not None
                    else None
                ),
                registered_sim_authority=context.registered_sim_authority,
            )
        except Exception as error:
            raise CtpExecutionGateError(_binding_error_code(error)) from None
        self._binding = binding
        return binding

    def _ensure_bridge_on_owner_thread(self) -> Any:
        if self._bridge is not None:
            return self._bridge
        context = self._runtime_context
        if context is None or self._issuer is None:
            raise CtpExecutionGateError("ctp_managed_order_bridge_unavailable")
        if context.registered_sim_authority is None:
            raise CtpExecutionGateError("ctp_registered_sim_authority_unavailable")
        if context.order_provider_authority_source is None:
            raise CtpExecutionGateError("ctp_order_provider_authority_source_unavailable")
        if context.execution_store is None or context.scope is None:
            raise CtpExecutionGateError("ctp_managed_execution_unavailable")
        for value in (
            context.execution_facade,
            context.cancellation_facade,
            context.submit_port,
            context.cancel_port,
        ):
            if value is None:
                raise CtpExecutionGateError("ctp_managed_execution_unavailable")
        if not callable(context.request_account_validator):
            raise CtpExecutionGateError("ctp_order_account_mapping_unavailable")
        try:
            from bt_api_ctp.instrument_metadata import CtpInstrumentMetadataReader
            from bt_api_ctp.managed_order_bridge import CtpManagedOrderExecutionBridge

            reader = context.instrument_metadata_reader
            if reader is not None and (
                type(reader) is not CtpInstrumentMetadataReader
                or reader.request_feed is not self._feed
            ):
                raise CtpExecutionGateError("ctp_order_catalog_binding_invalid")
            binding = self._ensure_binding_on_owner_thread()
            bridge = CtpManagedOrderExecutionBridge(
                provider_owner=self,
                binding=binding,
                scope=context.scope,
                execution_facade=context.execution_facade,
                cancellation_facade=context.cancellation_facade,
                submit_port=context.submit_port,
                cancel_port=context.cancel_port,
                registered_sim_authority=context.registered_sim_authority,
                selected_trader_client=self._trader_client,
                request_account_validator=context.request_account_validator,
                cancel_authorization_validator=context.cancel_authorization_validator,
                submit_context_resolver=context.submit_context_resolver,
            )
        except CtpExecutionGateError:
            raise
        except Exception as error:
            try:
                from bt_api_ctp.managed_order_bridge import CtpManagedOrderBridgeError
            except Exception:
                CtpManagedOrderBridgeError = ()  # type: ignore[assignment,misc]
            if type(error) is CtpManagedOrderBridgeError:
                code = getattr(error, "code", None)
                if type(code) is str and code.isascii() and code.replace("_", "").isalnum():
                    raise CtpExecutionGateError(code) from None
            raise CtpExecutionGateError("ctp_managed_order_bridge_unavailable") from None
        self._bridge = bridge
        return bridge

    def _run_bridge_on_owner_thread(self, operation: Any) -> Any:
        def invoke() -> Any:
            try:
                bridge = self._ensure_bridge_on_owner_thread()
                return operation(bridge)
            except CtpExecutionGateError:
                raise
            except Exception as error:
                try:
                    from bt_api_ctp.managed_order_bridge import CtpManagedOrderBridgeError
                except Exception:
                    CtpManagedOrderBridgeError = ()  # type: ignore[assignment,misc]
                if type(error) is CtpManagedOrderBridgeError:
                    code = getattr(error, "code", None)
                    if type(code) is str and code.isascii() and code.replace("_", "").isalnum():
                        raise CtpExecutionGateError(code) from None
                try:
                    from bt_api_ctp.instrument_metadata import CtpInstrumentMetadataError
                except Exception:
                    CtpInstrumentMetadataError = ()  # type: ignore[assignment,misc]
                if type(error) is CtpInstrumentMetadataError:
                    raise CtpExecutionGateError(
                        "ctp_order_instrument_metadata_unavailable"
                    ) from None
                raise CtpExecutionGateError("ctp_order_provider_operation_unavailable") from None

        return self._call_on_owner_thread(invoke)

    def _read_verified_instrument_metadata(
        self,
        request: OrderRequestProtocol,
        bridge: Any,
    ) -> Any:
        context = self._runtime_context
        instrument = request.instrument
        if context is None or context.instrument_metadata_reader is None:
            raise CtpExecutionGateError("ctp_order_catalog_unavailable")
        if (
            type(instrument) is not MarketInstrumentKey
            or instrument.provider != "ctp"
            or instrument.product_type != "future"
            or instrument.native_symbol != request.symbol
            or instrument.venue != instrument.venue.strip()
        ):
            raise CtpExecutionGateError("ctp_order_catalog_binding_invalid")
        try:
            from bt_api_ctp.instrument_metadata import (
                CtpInstrumentMetadata,
                CtpInstrumentMetadataReader,
            )

            reader = context.instrument_metadata_reader
            if (
                type(reader) is not CtpInstrumentMetadataReader
                or reader.request_feed is not self._feed
            ):
                raise CtpExecutionGateError("ctp_order_catalog_binding_invalid")
            generation = bridge.session_generation_id
            metadata = reader.read(
                instrument.instrument_id,
                instrument.venue,
                account_session_generation=generation,
                timeout=float(context.instrument_metadata_timeout_seconds),
            )
            if (
                type(metadata) is not CtpInstrumentMetadata
                or metadata.instrument_id != instrument.instrument_id
                or metadata.exchange_id != instrument.venue
                or metadata.asset_type != "future"
                or metadata.account_session_generation != generation
                or metadata.trading_day != context.scope.trading_day
                or type(metadata.volume_multiple) is not int
                or metadata.volume_multiple <= 0
            ):
                raise CtpExecutionGateError("ctp_order_catalog_identity_mismatch")
            tick = Decimal(metadata.price_tick)
            if not tick.is_finite() or tick <= 0 or request.price % tick != 0:
                raise CtpExecutionGateError("ctp_order_price_tick_mismatch")
            if metadata.price_limits_available:
                if metadata.lower_limit_price is None or metadata.upper_limit_price is None:
                    raise CtpExecutionGateError("ctp_order_price_limits_invalid")
                lower = Decimal(metadata.lower_limit_price)
                upper = Decimal(metadata.upper_limit_price)
                if (
                    not lower.is_finite()
                    or not upper.is_finite()
                    or lower <= 0
                    or upper < lower
                    or request.price < lower
                    or request.price > upper
                ):
                    raise CtpExecutionGateError("ctp_order_price_outside_limits")
            return metadata
        except CtpExecutionGateError:
            raise
        except Exception:
            raise CtpExecutionGateError("ctp_order_instrument_metadata_unavailable") from None

    def order_capacity_facts(self) -> OrderCapacitySnapshot:
        """Project capacity from this provider's exact selected sim authority."""
        context = self._runtime_context
        if context is None or context.owner_thread_invoker is None:
            return unavailable_order_capacity_snapshot(
                _CTP_FUTURE_ROUTE,
                "order_capacity_authority_unavailable",
            )
        try:
            result = self._call_on_owner_thread(
                lambda: self._ensure_binding_on_owner_thread().order_capacity_facts(
                    provider_owner=self
                )
            )
        except Exception:
            return unavailable_order_capacity_snapshot(
                _CTP_FUTURE_ROUTE,
                "order_capacity_authority_unavailable",
            )
        if type(result) is not OrderCapacitySnapshot or result.exchange_name != _CTP_FUTURE_ROUTE:
            return unavailable_order_capacity_snapshot(
                _CTP_FUTURE_ROUTE,
                "order_capacity_projection_invalid",
            )
        return result

    def _require_order_request(self, correlation: Any, request: Any) -> None:
        if type(correlation) is not OrderCorrelation:
            raise CtpExecutionGateError("ctp_order_correlation_untyped")
        if not isinstance(request, OrderRequestProtocol):
            raise CtpExecutionGateError("ctp_typed_order_request_untyped")
        if (
            correlation.symbol != request.symbol
            or correlation.client_order_id != request.client_order_id
            or correlation.exchange_name != _CTP_FUTURE_ROUTE
        ):
            raise CtpExecutionGateError("ctp_order_correlation_request_mismatch")
        instrument = request.instrument
        if type(instrument) is not MarketInstrumentKey:
            raise CtpExecutionGateError("ctp_order_instrument_key_required")
        if (
            instrument.provider != "ctp"
            or instrument.environment != self._environment
            or self._environment not in {"production", "demo", "testnet"}
            or instrument.product_type != "future"
            or instrument.native_symbol != request.symbol
        ):
            raise CtpExecutionGateError("ctp_order_instrument_route_mismatch")

        side = _enum_text(request.side)
        order_type = _enum_text(request.order_type)
        if side not in {"buy", "sell"} or order_type != "limit":
            raise CtpExecutionGateError("ctp_order_type_unsupported")
        if (
            type(request.quantity) is not Decimal
            or not request.quantity.is_finite()
            or request.quantity <= 0
            or request.quantity != request.quantity.to_integral_value()
            or request.quantity_unit != "lots"
        ):
            raise CtpExecutionGateError("ctp_order_quantity_unit_unsupported")
        if (
            type(request.price) is not Decimal
            or not request.price.is_finite()
            or request.price <= 0
        ):
            raise CtpExecutionGateError("ctp_order_price_invalid")
        if request.time_in_force != "DAY":
            raise CtpExecutionGateError("ctp_order_time_in_force_unsupported")
        if type(request.reduce_only) is not bool:
            raise CtpExecutionGateError("ctp_order_reduce_only_invalid")
        offset = request.offset or ("close" if request.reduce_only else "open")
        if offset not in {"open", "close", "close_today", "close_yesterday"}:
            raise CtpExecutionGateError("ctp_order_offset_unsupported")
        closing = offset in {"close", "close_today", "close_yesterday"}
        if request.reduce_only and not closing:
            raise CtpExecutionGateError("ctp_order_position_intent_mismatch")
        position_side = request.position_side
        if request.position_mode == "dual_side" and position_side not in {"long", "short"}:
            raise CtpExecutionGateError("ctp_order_position_intent_incomplete")
        if position_side in {"long", "short"}:
            expected_buy = (position_side == "long") != closing
            if (side == "buy") != expected_buy:
                raise CtpExecutionGateError("ctp_order_position_intent_mismatch")
        if getattr(request, "position_id", None) is not None:
            raise CtpExecutionGateError("ctp_order_position_ticket_unsupported")

        hedge_class = request.hedge_class
        legacy_flag = getattr(request, "hedge_flag", None)
        flag_to_class = {"1": "speculation", "2": "arbitrage", "3": "hedge"}
        class_to_flag = {value: key for key, value in flag_to_class.items()}
        if legacy_flag is not None and legacy_flag not in flag_to_class:
            raise CtpExecutionGateError("ctp_order_hedge_class_unsupported")
        if hedge_class is None:
            raise CtpExecutionGateError("ctp_order_hedge_class_required")
        if hedge_class not in class_to_flag:
            raise CtpExecutionGateError("ctp_order_hedge_class_unsupported")
        if hedge_class != "speculation":
            raise CtpExecutionGateError("ctp_order_hedge_class_unsupported")
        if legacy_flag is not None and flag_to_class[legacy_flag] != hedge_class:
            raise CtpExecutionGateError("ctp_order_hedge_class_mismatch")

        if any(
            getattr(request, name, None) is not None
            for name in (
                "ctp_order_identity",
                "exchange_id",
                "managed_intent_id",
                "runtime_order_id",
            )
        ):
            raise CtpExecutionGateError("ctp_order_native_identity_must_remain_provider_owned")

    @staticmethod
    def _require_cancel_request(correlation: Any, request: Any) -> None:
        if type(correlation) is not OrderCorrelation:
            raise CtpExecutionGateError("ctp_order_correlation_untyped")
        if not isinstance(request, CancelOrderRequestProtocol):
            raise CtpExecutionGateError("ctp_typed_cancel_request_untyped")
        if (
            correlation.symbol != request.symbol
            or request.client_order_id not in {None, correlation.client_order_id}
            or getattr(request, "order_id", None) is not None
            or correlation.exchange_name != _CTP_FUTURE_ROUTE
        ):
            raise CtpExecutionGateError("ctp_order_correlation_request_mismatch")
        if any(
            getattr(request, name, None) is not None
            for name in (
                "exchange_id",
                "front_id",
                "session_id",
                "order_ref",
                "ctp_cancel_identity",
                "runtime_action_id",
                "runtime_order_id",
                "managed_cancel_intent_id",
            )
        ):
            raise CtpExecutionGateError("ctp_cancel_native_target_must_remain_provider_owned")

    def bind_order_reservation(
        self,
        correlation: OrderCorrelation,
        *,
        issuer_reservation: object,
    ) -> object:
        """Bind an SDK-issued neutral correlation to its durable CTP identity."""
        if type(correlation) is not OrderCorrelation:
            raise CtpExecutionGateError("ctp_order_correlation_untyped")
        if correlation.exchange_name != _CTP_FUTURE_ROUTE:
            raise CtpExecutionGateError("ctp_order_correlation_route_mismatch")
        if issuer_reservation is None or type(issuer_reservation) is bool:
            raise CtpExecutionGateError("ctp_order_issuer_reservation_required")
        def bind() -> object:
            binding = self._ensure_binding_on_owner_thread()
            try:
                return binding.bind_order_reservation(
                    correlation,
                    issuer_reservation=issuer_reservation,
                    provider_owner=self,
                )
            except CtpExecutionGateError:
                raise
            except Exception as error:
                raise CtpExecutionGateError(_binding_error_code(error)) from None

        return self._call_on_owner_thread(bind)

    def resolve_order_reservation(
        self,
        correlation: OrderCorrelation,
        *,
        issuer_reservation: object,
    ) -> object | None:
        """Resolve only an already persisted provider binding; never reissue."""
        if (
            type(correlation) is not OrderCorrelation
            or correlation.exchange_name != _CTP_FUTURE_ROUTE
            or issuer_reservation is None
            or type(issuer_reservation) is bool
        ):
            return None
        try:
            return self._call_on_owner_thread(
                lambda: self._ensure_binding_on_owner_thread().resolve_order_reservation(
                    correlation,
                    issuer_reservation=issuer_reservation,
                    provider_owner=self,
                )
            )
        except Exception:
            return None

    def claim_correlated_order(
        self,
        operation: Literal["submit", "cancel"],
        correlation: OrderCorrelation,
        request: OrderRequestProtocol | CancelOrderRequestProtocol,
        *,
        issuer_reservation: object,
        provider_reservation: object,
        authorization: object,
    ) -> object:
        """Bind an exact typed request to the provider's local one-shot handle."""

        if type(operation) is not str:
            raise CtpExecutionGateError("ctp_order_operation_unsupported")
        if operation == "submit":
            self._require_order_request(correlation, request)
        elif operation == "cancel":
            self._require_cancel_request(correlation, request)
        else:
            raise CtpExecutionGateError("ctp_order_operation_unsupported")
        if issuer_reservation is None or type(issuer_reservation) is bool:
            raise CtpExecutionGateError("ctp_order_issuer_reservation_required")
        if provider_reservation is None or type(provider_reservation) is bool:
            raise CtpExecutionGateError("ctp_order_provider_reservation_required")
        if authorization is None or type(authorization) is bool:
            raise CtpExecutionGateError("ctp_order_authorization_required")
        def claim(bridge: Any) -> object:
            if operation == "submit":
                self._read_verified_instrument_metadata(request, bridge)
            return bridge.claim_correlated_order(
                operation,
                correlation,
                request,
                issuer_reservation=issuer_reservation,
                provider_reservation=provider_reservation,
                authorization=authorization,
            )

        return self._run_bridge_on_owner_thread(claim)

    async def submit_correlated_order(
        self,
        correlation: OrderCorrelation,
        request: OrderRequestProtocol,
        *,
        issuer_reservation: object,
        provider_reservation: object,
        claim: object,
        authorization: object,
    ) -> object:
        """Dispatch through the selected runtime's single owner mailbox."""

        self._require_order_request(correlation, request)
        if issuer_reservation is None or type(issuer_reservation) is bool:
            raise CtpExecutionGateError("ctp_order_issuer_reservation_required")
        if provider_reservation is None or type(provider_reservation) is bool:
            raise CtpExecutionGateError("ctp_order_provider_reservation_required")
        if claim is None or type(claim) is bool:
            raise CtpExecutionGateError("ctp_order_claim_required")
        if authorization is None or type(authorization) is bool:
            raise CtpExecutionGateError("ctp_order_authorization_required")
        def submit(bridge: Any) -> object:
            self._read_verified_instrument_metadata(request, bridge)
            return bridge.submit_correlated_order(
                correlation,
                request,
                issuer_reservation=issuer_reservation,
                provider_reservation=provider_reservation,
                claim=claim,
                authorization=authorization,
            )

        return self._run_bridge_on_owner_thread(submit)

    async def cancel_correlated_order(
        self,
        correlation: OrderCorrelation,
        request: CancelOrderRequestProtocol,
        *,
        issuer_reservation: object,
        provider_reservation: object,
        claim: object,
        authorization: object,
    ) -> object:
        """Dispatch one cancellation through the durable managed cancellation owner."""

        self._require_cancel_request(correlation, request)
        if issuer_reservation is None or type(issuer_reservation) is bool:
            raise CtpExecutionGateError("ctp_order_issuer_reservation_required")
        if provider_reservation is None or type(provider_reservation) is bool:
            raise CtpExecutionGateError("ctp_order_provider_reservation_required")
        if claim is None or type(claim) is bool:
            raise CtpExecutionGateError("ctp_order_claim_required")
        if authorization is None or type(authorization) is bool:
            raise CtpExecutionGateError("ctp_order_authorization_required")
        return self._run_bridge_on_owner_thread(
            lambda bridge: bridge.cancel_correlated_order(
                correlation,
                request,
                issuer_reservation=issuer_reservation,
                provider_reservation=provider_reservation,
                claim=claim,
                authorization=authorization,
            )
        )

    def verify_order_response(
        self,
        correlation: OrderCorrelation,
        response: object,
        *,
        issuer_reservation: object,
        provider_reservation: object,
    ) -> VerifiedOrderAcknowledgement | None:
        """Verify only the exact callback-derived terminal record on its owner."""
        if (
            type(correlation) is not OrderCorrelation
            or correlation.exchange_name != _CTP_FUTURE_ROUTE
            or issuer_reservation is None
            or type(issuer_reservation) is bool
            or provider_reservation is None
            or type(provider_reservation) is bool
        ):
            return None
        try:
            return self._run_bridge_on_owner_thread(
                lambda bridge: bridge.verify_order_response(
                    correlation,
                    response,
                    issuer_reservation=issuer_reservation,
                    provider_reservation=provider_reservation,
                )
            )
        except Exception:
            return None

    def verify_cancel_response(
        self,
        correlation: "OrderCorrelation",
        response: object,
        *,
        issuer_reservation: object,
        provider_reservation: object,
    ) -> VerifiedOrderAcknowledgement | None:
        """Verify only the exact callback-derived terminal cancel record."""
        if (
            type(correlation) is not OrderCorrelation
            or correlation.exchange_name != _CTP_FUTURE_ROUTE
            or issuer_reservation is None
            or type(issuer_reservation) is bool
            or provider_reservation is None
            or type(provider_reservation) is bool
        ):
            return None
        try:
            return self._run_bridge_on_owner_thread(
                lambda bridge: bridge.verify_cancel_response(
                    correlation,
                    response,
                    issuer_reservation=issuer_reservation,
                    provider_reservation=provider_reservation,
                )
            )
        except Exception:
            return None


__all__ = [
    "CtpOrderProviderRuntimeContext",
    "CtpOrderRequestProvider",
    "register_ctp_typed_order_provider",
]
