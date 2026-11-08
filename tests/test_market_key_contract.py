"""CTP market key validation remains pure and preserves provider error codes."""

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from bt_api_base.market_protocol import MarketInstrumentKey, MarketSubscriptionKey

from bt_api_ctp.market_key_contract import CtpMarketKeyError, validate_ctp_market_key


def key():
    return MarketSubscriptionKey(
        "source",
        "entitlement",
        MarketInstrumentKey("ctp", "CFFEX", "simnow", "future", "IF2612", "IF2612"),
        "tick",
    )


def invalid_cases():
    original = key()
    return (
        (object(), "INVALID_CTP_MARKET_KEY"),
        (replace(original, source_ref="other"), "UNSUPPORTED_CTP_MARKET_KEY"),
        (replace(original, entitlement_ref="other"), "UNSUPPORTED_CTP_MARKET_KEY"),
        (
            replace(original, instrument=replace(original.instrument, provider="other")),
            "UNSUPPORTED_CTP_MARKET_KEY",
        ),
        (replace(original, channel="bar"), "UNSUPPORTED_CTP_MARKET_KEY"),
        (replace(original, depth=1), "UNSUPPORTED_CTP_MARKET_KEY"),
        (replace(original, timeframe="1m"), "UNSUPPORTED_CTP_MARKET_KEY"),
        (
            replace(original, instrument=replace(original.instrument, native_symbol="different")),
            "UNSUPPORTED_CTP_MARKET_KEY",
        ),
    )


def test_public_validation_and_existing_provider_mapping_are_equivalent():
    from bt_api_ctp.market_provider import CtpQuoteV2MarketProvider, CtpQuoteV2MarketProviderError

    provider = object.__new__(CtpQuoteV2MarketProvider)
    provider._source_ref = "source"
    provider._entitlement_ref = "entitlement"
    assert (
        validate_ctp_market_key(key(), source_ref="source", entitlement_ref="entitlement") is None
    )
    assert provider._validate_key(key()) is None
    for bad, code in invalid_cases():
        with pytest.raises(CtpMarketKeyError) as pure_error:
            validate_ctp_market_key(bad, source_ref="source", entitlement_ref="entitlement")
        with pytest.raises(CtpQuoteV2MarketProviderError) as provider_error:
            provider._validate_key(bad)
        assert pure_error.value.code == provider_error.value.code == code
        assert str(pure_error.value) == str(provider_error.value) == code
        assert provider_error.value.__cause__ is None


def test_existing_rule_does_not_invent_venue_or_environment_restrictions():
    original = key()
    varied = replace(
        original, instrument=replace(original.instrument, venue="other", environment="other")
    )
    assert (
        validate_ctp_market_key(varied, source_ref="source", entitlement_ref="entitlement") is None
    )


def test_lightweight_import_and_validation_do_not_load_native_or_feed():
    source_root = Path(__file__).resolve().parents[1] / "src"
    script = """
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
class BlockNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname == "bt_api_ctp.ctp.client" or fullname.startswith("bt_api_ctp.feeds")
                or fullname.startswith("openctp_ctp")):
            raise AssertionError("native/feed module imported")
sys.meta_path.insert(0, BlockNative())
from bt_api_base.market_protocol import MarketInstrumentKey, MarketSubscriptionKey
from bt_api_ctp.market_key_contract import validate_ctp_market_key
key = MarketSubscriptionKey("s", "e", MarketInstrumentKey("ctp", "v", "env", "future", "IF", "IF"), "tick")
validate_ctp_market_key(key, source_ref="s", entitlement_ref="e")
assert not any(n == "bt_api_ctp.ctp.client" or n.startswith("bt_api_ctp.feeds")
               or n.startswith("openctp_ctp") for n in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(source_root)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
