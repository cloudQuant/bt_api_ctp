"""Compose the CTP market-session resolver for an SDK-selected route."""

from __future__ import annotations

from bt_api_base.market_session_protocol import (
    MarketSessionProviderRegistryPort,
    MarketSessionProviderRouteError,
    MarketSessionResolver,
    SelectedMarketSessionRoute,
)

_CTP_ROUTE = "CTP___FUTURE"


def _create_ctp_market_session_resolver(
    route: SelectedMarketSessionRoute,
) -> MarketSessionResolver:
    if type(route) is not SelectedMarketSessionRoute or route.exchange_name != _CTP_ROUTE:
        raise MarketSessionProviderRouteError("MARKET_SESSION_PROVIDER_ROUTE_INVALID")

    # The SDK passes its selected feed opaquely. Only this CTP-owned factory
    # unwraps the public feed accessor and checks the canonical SDK client.
    from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

    feed = route.client
    if type(feed) is not CtpRequestDataFuture:
        raise MarketSessionProviderRouteError("MARKET_SESSION_PROVIDER_CLIENT_UNAVAILABLE")
    try:
        client = feed.trader_client
    except Exception:
        raise MarketSessionProviderRouteError(
            "MARKET_SESSION_PROVIDER_CLIENT_UNAVAILABLE"
        ) from None

    from bt_api_ctp.ctp.client import TraderClient

    if type(client) is not TraderClient:
        raise MarketSessionProviderRouteError("MARKET_SESSION_PROVIDER_CLIENT_UNAVAILABLE")

    from bt_api_ctp.market_session_projection import CtpMarketSessionResolver

    return CtpMarketSessionResolver(route)


def register_ctp_market_session_provider(
    registry: MarketSessionProviderRegistryPort,
) -> None:
    """Register only the exact CTP SDK route with a neutral session router."""

    if not isinstance(registry, MarketSessionProviderRegistryPort):
        raise TypeError("registry must implement MarketSessionProviderRegistryPort")
    registry.register(_CTP_ROUTE, _create_ctp_market_session_resolver)


__all__ = ["register_ctp_market_session_provider"]
