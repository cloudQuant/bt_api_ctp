from __future__ import annotations

from typing import Any

from bt_api_base.balance_utils import simple_balance_handler as _ctp_balance_handler
from bt_api_base.plugins.protocol import PluginInfo
from bt_api_base.registry import ExchangeRegistry

from bt_api_ctp import __version__
from bt_api_ctp.exchange_data import CtpExchangeDataFuture
from bt_api_ctp.feeds.live_ctp_feed import (
    CtpMarketStream,
    CtpRequestDataFuture,
    CtpTradeStream,
)


def _ctp_future_subscribe_handler(
    data_queue: Any,
    exchange_params: dict[str, Any],
    topics: list[dict[str, Any]],
    bt_api: Any,
) -> None:
    topic_list = [t.get("topic") for t in topics]
    has_tick = any(t in topic_list for t in ("tick", "ticker", "depth"))
    event_waker = getattr(bt_api, "_notify_event_wakers", None)
    if not callable(event_waker):
        event_waker = None
    if has_tick:
        market_kwargs = dict(exchange_params.items())
        market_kwargs["stream_name"] = "ctp_market_stream"
        market_kwargs["topics"] = topics
        if event_waker is not None:
            market_kwargs["_event_waker"] = event_waker
        market_stream = CtpMarketStream(data_queue, **market_kwargs)
        market_stream.start()
        bt_api._subscription_streams.append(market_stream)
        bt_api.log("CTP market stream started")

    if exchange_params.get("subscribe_account", True) and not bt_api._subscription_flags.get(
        "CTP___FUTURE_account", False
    ):
        trade_kwargs = dict(exchange_params.items())
        trade_kwargs["stream_name"] = "ctp_trade_stream"
        trade_kwargs["request_feed"] = getattr(bt_api, "exchange_feeds", {}).get("CTP___FUTURE")
        if event_waker is not None:
            trade_kwargs["_event_waker"] = event_waker
        trade_stream = CtpTradeStream(data_queue, **trade_kwargs)
        trade_stream.start()
        bt_api._subscription_streams.append(trade_stream)
        bt_api._subscription_flags["CTP___FUTURE_account"] = True
        bt_api.log("CTP trade stream started")


def register_plugin(registry: type[ExchangeRegistry]) -> PluginInfo:
    registry.register_feed("CTP___FUTURE", CtpRequestDataFuture)
    registry.register_exchange_data("CTP___FUTURE", CtpExchangeDataFuture)
    registry.register_balance_handler("CTP___FUTURE", _ctp_balance_handler)
    registry.register_stream("CTP___FUTURE", "subscribe", _ctp_future_subscribe_handler)
    registry.register_stream("CTP___FUTURE", "market", CtpMarketStream)
    registry.register_stream("CTP___FUTURE", "account", CtpTradeStream)
    return PluginInfo(
        name="bt_api_ctp",
        version=__version__,
        core_requires=">=0.15,<1.0",
        supported_exchanges=("CTP___FUTURE",),
        supported_asset_types=("FUTURE",),
    )
