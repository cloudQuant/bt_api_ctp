"""CTP instrument resolution for the read-only SDK service observation flow."""

from __future__ import annotations

from typing import Any

from bt_api_base.market_protocol import MarketInstrumentKey, MarketSubscriptionKey
from bt_api_ctp.service_instrument_metadata import ServiceAccountInstrumentMetadata


def resolve_service_observation_instrument(
    item: Any,
    account_client: Any,
    account_view: Any,
    market: Any,
    account_key: Any,
    *,
    error_type: type[Exception],
) -> tuple[Any, Any]:
    """Bind CTP metadata to the selected account session and market key."""
    try:
        metadata = account_client.instrument_metadata(item.instrument_id, item.exchange_id)
    except Exception:
        raise error_type("SERVICE_INSTRUMENT_METADATA_READ_FAILED") from None
    if type(metadata) is not ServiceAccountInstrumentMetadata:
        raise error_type("SERVICE_INSTRUMENT_METADATA_TYPE_INVALID")
    if (
        metadata.instrument_id != item.instrument_id
        or metadata.exchange_id != item.exchange_id
        or metadata.account_session_generation != account_view.session_generation
        or metadata.quote_source != "ctp.native.md"
    ):
        raise error_type("SERVICE_INSTRUMENT_METADATA_IDENTITY_MISMATCH")
    try:
        key = MarketSubscriptionKey(
            source_ref=market["source_ref"],
            entitlement_ref=market["entitlement_ref"],
            instrument=MarketInstrumentKey(
                provider="ctp",
                venue=metadata.exchange_id,
                # SimNow accounts carry the provider identity label "simnow";
                # their market keys use the SDK route label "demo" (I62-10).
                environment=(
                    "demo" if account_key.environment == "simnow" else account_key.environment
                ),
                product_type=metadata.asset_type,
                instrument_id=metadata.instrument_id,
                native_symbol=metadata.instrument_id,
            ),
            # The market provider's neutral subscription channel (iteration 62);
            # ``ctp.quote.v2`` is the payload schema the provider projects.
            channel="tick",
        )
    except Exception:
        raise error_type("SERVICE_MARKET_KEY_INVALID") from None
    return metadata, key


__all__ = ("resolve_service_observation_instrument",)
