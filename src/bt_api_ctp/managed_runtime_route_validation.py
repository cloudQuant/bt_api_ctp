"""CTP route-field validation for SDK managed-runtime composition.

This checks only declared route/instrument shape. The SDK retains grant,
session, source, lifecycle, and authorization ownership.
"""
from __future__ import annotations

from bt_api_base.managed_runtime_protocol import (
    ManagedRuntimeBinding,
    OperatorWriteGrant,
    SelectedManagedRuntimeRoute,
)
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_base.provider_configuration_protocol import ProviderConfiguration

CTP_FUTURE_MANAGED_RUNTIME_ROUTE = "CTP___FUTURE"


def validate_ctp_managed_runtime_route(route, *, error_type, validate_grant_bounds) -> None:
    if (
        type(route) is not SelectedManagedRuntimeRoute
        or route.exchange_name != CTP_FUTURE_MANAGED_RUNTIME_ROUTE
        or type(route.runtime_binding) is not ManagedRuntimeBinding
        or route.runtime_binding.exchange_name != CTP_FUTURE_MANAGED_RUNTIME_ROUTE
        or route.runtime_binding.environment != "simulation"
        or type(route.configuration) is not ProviderConfiguration
        or route.configuration.exchange_name != CTP_FUTURE_MANAGED_RUNTIME_ROUTE
        or route.environment != "simulation"
        or route.configuration.environment != "simulation"
        or type(route.configuration.instrument) is not MarketInstrumentKey
        or route.configuration.instrument.provider != "ctp"
        or route.configuration.instrument.environment != "simulation"
        or route.configuration.instrument.product_type != "future"
    ):
        raise error_type(
            "ctp_managed_runtime_route_invalid"
        )
    instrument = route.configuration.instrument
    if (
        type(instrument.native_symbol) is not str
        or not instrument.native_symbol
        or type(instrument.venue) is not str
        or not instrument.venue
    ):
        raise error_type(
            "ctp_managed_runtime_configuration_invalid"
        )
    if route.access_mode == "read_only":
        if route.operator_grant is not None or route.authorization_source is not None:
            raise error_type(
                "ctp_managed_runtime_route_invalid"
            )
    elif route.access_mode == "managed_execution":
        grant = route.operator_grant
        if type(grant) is not OperatorWriteGrant:
            raise error_type(
                "ctp_managed_runtime_operator_grant_invalid"
            )
        validate_grant_bounds(route, grant)
    else:
        raise error_type(
            "ctp_managed_runtime_route_invalid"
        )


__all__ = ("CTP_FUTURE_MANAGED_RUNTIME_ROUTE", "validate_ctp_managed_runtime_route")
