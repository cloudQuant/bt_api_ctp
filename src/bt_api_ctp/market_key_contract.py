"""Pure CTP market-subscription shape checks; these grant no entitlement."""

from __future__ import annotations

from bt_api_base.market_protocol import MarketSubscriptionKey


class CtpMarketKeyError(ValueError):
    """Finite, value-free key validation error."""

    def __init__(self, code: str) -> None:
        self.code = (
            code
            if type(code) is str
            and code in {"INVALID_CTP_MARKET_KEY", "UNSUPPORTED_CTP_MARKET_KEY"}
            else "INVALID_CTP_MARKET_KEY"
        )
        super().__init__(self.code)


def validate_ctp_market_key(key: object, *, source_ref: str, entitlement_ref: str) -> None:
    """Validate the existing CTP tick-key contract without opening a session."""
    if type(key) is not MarketSubscriptionKey:
        raise CtpMarketKeyError("INVALID_CTP_MARKET_KEY")
    instrument = key.instrument
    if (
        key.source_ref != source_ref
        or key.entitlement_ref != entitlement_ref
        or instrument.provider != "ctp"
        or key.channel != "tick"
        or key.depth is not None
        or key.timeframe is not None
        or instrument.instrument_id != instrument.native_symbol
    ):
        raise CtpMarketKeyError("UNSUPPORTED_CTP_MARKET_KEY")
