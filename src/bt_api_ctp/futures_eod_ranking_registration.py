"""Entry-point registration for the explicit CTP public EOD ranking port."""

from __future__ import annotations

from bt_api_base.futures_eod_ranking_protocol import (
    FuturesEodRankingProviderRegistryPort,
    SelectedFuturesEodRankingRoute,
)

from .futures_eod.provider import CtpEodRankingProvider

_EXCHANGE_NAME = "CTP___FUTURE"


def register_ctp_eod_ranking_provider(
    registry: FuturesEodRankingProviderRegistryPort,
) -> None:
    """Register only the exact selected CTP futures EOD route."""

    if not isinstance(registry, FuturesEodRankingProviderRegistryPort):
        raise TypeError("registry does not implement FuturesEodRankingProviderRegistryPort")
    registry.register(_EXCHANGE_NAME, _create_provider)


def _create_provider(route: SelectedFuturesEodRankingRoute) -> CtpEodRankingProvider:
    if type(route) is not SelectedFuturesEodRankingRoute:
        raise TypeError("route must be SelectedFuturesEodRankingRoute")
    if route.exchange_name != _EXCHANGE_NAME:
        raise ValueError("route is not the CTP futures EOD capability")
    return CtpEodRankingProvider()


__all__ = ["register_ctp_eod_ranking_provider"]
