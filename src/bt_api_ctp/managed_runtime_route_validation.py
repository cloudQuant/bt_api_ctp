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


#: Typed-command vocabulary the managed CTP runtime truthfully dispatches
#: (iteration 60): integral volumes (contracts/lots), a two-sided long/short
#: CTP position book, ``CombHedgeFlag`` speculation ("1"), CNY futures prices.
CTP_MANAGED_COMMAND_QUANTITY_UNITS = frozenset({"contracts", "lots"})
CTP_MANAGED_COMMAND_PRICE_CURRENCY = "CNY"
CTP_MANAGED_COMMAND_POSITION_MODE = "dual_side"
CTP_MANAGED_COMMAND_HEDGE_CLASS = "speculation"


def validate_ctp_managed_command_binding(
    declaration,
    *,
    provider,
    environment,
    account_ref,
    intent_instrument,
    provider_instrument,
    exchange_id,
    error_type,
) -> None:
    """Check one typed order-command declaration against the CTP runtime facts.

    ``declaration`` is the SDK's canonical plain declaration (neutral values
    only). The managed runtime dispatches exactly one identity-mapped futures
    instrument of one account, so the declaration must name that account and
    that instrument as a full ``MarketInstrumentKey``. Optional price
    currency, position mode and hedge class may be omitted (the command then
    states nothing); a declared value must be the one the runtime really
    uses. It grants nothing: authorization stays with the SDK runtime.
    """

    account = declaration.get("account_key") if isinstance(declaration, dict) else None
    if (
        not isinstance(account, dict)
        or provider != "ctp"
        or account.get("provider") != "ctp"
        or account.get("environment") != environment
        or account.get("account_id") != account_ref
    ):
        raise error_type("COMMAND_BINDING_ACCOUNT_MISMATCH")
    instruments = declaration.get("instruments")
    expected = {
        "provider": "ctp",
        "venue": exchange_id,
        "environment": environment,
        "product_type": "future",
        "instrument_id": intent_instrument,
        "native_symbol": provider_instrument,
    }
    if (
        not isinstance(instruments, (list, tuple))
        or len(instruments) != 1
        or not isinstance(instruments[0], dict)
        or instruments[0] != expected
    ):
        raise error_type("COMMAND_BINDING_INSTRUMENT_MISMATCH")
    if declaration.get("quantity_unit") not in CTP_MANAGED_COMMAND_QUANTITY_UNITS:
        raise error_type("COMMAND_BINDING_QUANTITY_UNIT_UNSUPPORTED")
    if declaration.get("price_currency") not in (None, CTP_MANAGED_COMMAND_PRICE_CURRENCY):
        raise error_type("COMMAND_BINDING_PRICE_CURRENCY_UNSUPPORTED")
    if declaration.get("position_mode") not in (None, CTP_MANAGED_COMMAND_POSITION_MODE):
        raise error_type("COMMAND_BINDING_POSITION_MODE_UNSUPPORTED")
    if declaration.get("hedge_class") not in (None, CTP_MANAGED_COMMAND_HEDGE_CLASS):
        raise error_type("COMMAND_BINDING_HEDGE_CLASS_UNSUPPORTED")


__all__ = (
    "CTP_FUTURE_MANAGED_RUNTIME_ROUTE",
    "CTP_MANAGED_COMMAND_HEDGE_CLASS",
    "CTP_MANAGED_COMMAND_POSITION_MODE",
    "CTP_MANAGED_COMMAND_PRICE_CURRENCY",
    "CTP_MANAGED_COMMAND_QUANTITY_UNITS",
    "validate_ctp_managed_command_binding",
    "validate_ctp_managed_runtime_route",
)
