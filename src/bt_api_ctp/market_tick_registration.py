"""Register the CTP neutral source adapter for SDK market tick routing."""

from __future__ import annotations

from dataclasses import dataclass

from bt_api_base.market_protocol import MarketInstrumentKey, MarketSubscriptionKey
from bt_api_base.market_tick_provider_protocol import (
    MarketTickProviderAdapter,
    MarketTickProviderRegistryPort,
    MarketTickProviderRouteError,
    MarketTickSourceBinding,
    MarketTickSourceBindingError,
    SelectedMarketTickRoute,
)

_CTP_ROUTE = "CTP___FUTURE"


@dataclass(frozen=True)
class _CtpMarketTickProviderAdapter:
    """Bind one neutral CTP instrument to runtime-authorized source refs.

    This adapter carries no source, entitlement, session, clock, or freshness
    claim. The SDK market runtime supplies source and entitlement references
    only after resolving its trusted route configuration.
    """

    instrument: MarketInstrumentKey

    def create_subscription_key(
        self,
        instrument: MarketInstrumentKey,
        *,
        source_ref: str,
        entitlement_ref: str,
        channel: str,
    ) -> MarketSubscriptionKey:
        if type(instrument) is not MarketInstrumentKey or instrument != self.instrument:
            raise MarketTickSourceBindingError(
                "MARKET_TICK_SOURCE_INSTRUMENT_MISMATCH"
            )
        if type(channel) is not str or channel != "tick":
            raise MarketTickSourceBindingError(
                "MARKET_TICK_SOURCE_CHANNEL_UNSUPPORTED"
            )
        try:
            return MarketSubscriptionKey(
                source_ref=source_ref,
                entitlement_ref=entitlement_ref,
                instrument=self.instrument,
                channel=channel,
            )
        except (TypeError, ValueError):
            raise MarketTickSourceBindingError(
                "MARKET_TICK_SOURCE_BINDING_INVALID"
            ) from None


def _create_ctp_market_tick_source_binding(
    route: SelectedMarketTickRoute,
) -> MarketTickSourceBinding:
    if type(route) is not SelectedMarketTickRoute or route.exchange_name != _CTP_ROUTE:
        raise MarketTickProviderRouteError("MARKET_TICK_PROVIDER_ROUTE_INVALID")
    if type(route.instrument) is not MarketInstrumentKey or route.instrument.provider != "ctp":
        raise MarketTickProviderRouteError("MARKET_TICK_PROVIDER_ROUTE_INVALID")
    if (
        route.environment is not None
        and route.instrument.environment != route.environment
    ):
        raise MarketTickProviderRouteError("MARKET_TICK_PROVIDER_ROUTE_INVALID")

    adapter: MarketTickProviderAdapter = _CtpMarketTickProviderAdapter(route.instrument)
    try:
        return MarketTickSourceBinding(
            instrument=route.instrument,
            channel="tick",
            provider_adapter=adapter,
        )
    except (TypeError, ValueError):
        raise MarketTickSourceBindingError("MARKET_TICK_SOURCE_ADAPTER_INVALID") from None


def register_ctp_market_tick_provider(
    registry: MarketTickProviderRegistryPort,
) -> None:
    """Register only the CTP route with the neutral market tick router."""

    if not isinstance(registry, MarketTickProviderRegistryPort):
        raise TypeError("registry must implement MarketTickProviderRegistryPort")
    registry.register(_CTP_ROUTE, _create_ctp_market_tick_source_binding)


__all__ = ["register_ctp_market_tick_provider"]
