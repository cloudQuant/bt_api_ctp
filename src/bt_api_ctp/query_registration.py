"""Register the CTP provider-neutral query capability with ``bt_api_py``.

This module stays import-light: loading the provider entry point does not load
the CTP native extension or create a client. The provider service is imported
only after the SDK has selected a route and asks for its query port.
"""

from __future__ import annotations

from bt_api_base.query_protocol import ProviderQueryPort
from bt_api_base.query_provider_protocol import (
    QueryProviderRegistryPort,
    SelectedQueryRoute,
)

_CTP_ROUTE = "CTP___FUTURE"


def _create_ctp_query_port(route: SelectedQueryRoute) -> ProviderQueryPort:
    if type(route) is not SelectedQueryRoute or route.exchange_name != _CTP_ROUTE:
        raise RuntimeError("ctp_query_route_invalid")

    # BtApi owns route selection and passes its already-selected feed as an
    # opaque neutral client. Only this CTP-owned adapter unwraps the public
    # feed accessor; the SDK facade must not import or inspect CTP types.
    from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

    feed = route.client
    if type(feed) is not CtpRequestDataFuture:
        raise RuntimeError("ctp_query_feed_not_canonical")
    client = feed.trader_client
    from bt_api_ctp.ctp.client import TraderClient

    if type(client) is not TraderClient:
        raise RuntimeError("ctp_query_client_not_canonical")

    from bt_api_ctp.query_service import create_ctp_provider_query_service

    return create_ctp_provider_query_service(
        client,
        environment=route.environment,
        selected_feed=feed,
        execution_context=route.execution_context,
        order_observation_issuer=route.order_observation_issuer,
    )


def register_ctp_query_provider(registry: QueryProviderRegistryPort) -> None:
    """Register this package's exact CTP route with the SDK query registry.

    The SDK's ``bt_api.query_providers`` entry-point loader supplies the
    registry. This provider registration does not select a client, infer an
    environment, or connect to a CTP account.
    """

    if not isinstance(registry, QueryProviderRegistryPort):
        raise TypeError("registry must implement QueryProviderRegistryPort")

    registry.register(_CTP_ROUTE, _create_ctp_query_port)


__all__ = ["register_ctp_query_provider"]
