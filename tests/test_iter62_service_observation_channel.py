"""Iteration 62 (62.08): the service-observation market key uses the provider channel.

``resolve_service_observation_instrument`` builds the market key a read-only
service observation subscribes on the Gateway MarketHub. That hub's CTP
provider (``CtpQuoteV2MarketProvider``) accepts only its neutral ``"tick"``
channel; ``ctp.quote.v2`` is the payload schema it projects. UNIT/FAKE.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bt_api_ctp.market_provider import CtpQuoteV2MarketProvider
from bt_api_ctp.service_instrument_metadata import ServiceAccountInstrumentMetadata
from bt_api_ctp.service_observation_instrument import resolve_service_observation_instrument

_SOURCE_REF = "ctp-simnow-profile-set1-group1-vpn-market-v1"
_ENTITLEMENT_REF = "ctp-simnow-profile-set1-group1-vpn-tick-v1"


class _Refused(RuntimeError):
    pass


def _metadata() -> ServiceAccountInstrumentMetadata:
    return ServiceAccountInstrumentMetadata(
        instrument_id="rb2701",
        exchange_id="SHFE",
        product_class="1",
        asset_type="future",
        price_tick="1",
        volume_multiple=10,
        lower_limit_price=None,
        upper_limit_price=None,
        price_limits_available=False,
        price_limits_source="unavailable",
        account_session_generation="session-generation-1",
        account_fingerprint="acct_0123456789abcdef",
        trading_day="20261106",
        source_connection_generation=1,
        source_request_id=1,
        source_records_sha256="0" * 64,
        source_revision="revision-1",
        source="ctp.native.td",
        query_started_at_utc="2026-11-05T13:00:00.000000Z",
        observed_at_utc="2026-11-05T13:00:00.100000Z",
        query_clock_domain_id="clock-domain",
        query_elapsed_seconds=0.1,
        quote_stream_generation=1,
        quote_source="ctp.native.md",
        clock_domain_id="clock-domain",
        clock_source_session_id="clock-session",
        clock_source="monotonic",
    )


def test_observation_market_key_is_accepted_by_the_ctp_market_provider() -> None:
    metadata = _metadata()
    account_client = SimpleNamespace(instrument_metadata=lambda _instrument, _exchange: metadata)
    resolved, key = resolve_service_observation_instrument(
        SimpleNamespace(instrument_id="rb2701", exchange_id="SHFE"),
        account_client,
        SimpleNamespace(session_generation="session-generation-1"),
        {"source_ref": _SOURCE_REF, "entitlement_ref": _ENTITLEMENT_REF},
        SimpleNamespace(environment="simnow"),
        error_type=_Refused,
    )

    assert resolved is metadata
    assert key.channel == "tick"
    # SimNow account identity "simnow" -> SDK route instrument label "demo".
    assert key.instrument.environment == "demo"
    provider = CtpQuoteV2MarketProvider(
        stream_factory=lambda *_args: pytest.fail("validation must not start a stream"),
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
    )
    provider._validate_key(key)  # raises UNSUPPORTED_CTP_MARKET_KEY on a schema "channel"
