"""Offline tests for the declared CTP plugin registration entry point."""

from bt_api_base.testing import create_isolated_exchange_registry

from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
from bt_api_ctp.plugin import register_plugin


def test_ctp_plugin_registers_future_feed():
    registry = create_isolated_exchange_registry()

    info = register_plugin(registry)

    assert info.name == "bt_api_ctp"
    assert info.supported_exchanges == ("CTP___FUTURE",)
    assert registry.get_feed_class("CTP___FUTURE") is CtpRequestDataFuture
