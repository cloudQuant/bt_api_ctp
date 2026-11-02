"""CTP trade-stream selection and native session readiness projection.

The SDK owns the subscription registry and decides when to ask. These checks
compare the selected provider's current session facts; they grant no authority.
"""

from collections.abc import Mapping

from .query_result_evidence import canonical_ctp_account_fingerprint


def select_ctp_trade_stream(streams):
    if not isinstance(streams, list):
        return None
    candidates = [
        stream
        for stream in streams
        if str(getattr(stream, "stream_name", "")) == "ctp_trade_stream"
    ]
    return candidates[0] if len(candidates) == 1 else None


def is_ctp_trade_stream_ready(stream, *, resolve_feed):
    if stream is None or getattr(stream, "_running", None) is not True:
        return False
    state = getattr(stream, "state", None)
    state_value = str(getattr(state, "value", state) or "").strip().lower()
    if state_value != "authenticated":
        return False
    feed = resolve_feed()
    request_trader = getattr(feed, "trader_client", None)
    stream_trader = getattr(stream, "trader_client", None)
    if (
        feed is None
        or request_trader is None
        or stream_trader is None
        or stream_trader is not request_trader
    ):
        return False
    state_reader = getattr(request_trader, "get_session_state", None)
    feed_state_reader = getattr(feed, "get_session_state", None)
    if not callable(state_reader) or not callable(feed_state_reader):
        return False
    try:
        trader_state = state_reader()
        feed_state = feed_state_reader()
    except Exception:
        return False
    if not isinstance(trader_state, Mapping) or not isinstance(feed_state, Mapping):
        return False
    generation = feed_state.get("connection_generation")
    account = canonical_ctp_account_fingerprint(feed_state.get("account_fingerprint"))
    trading_day = str(feed_state.get("trading_day") or "").strip()
    return bool(
        type(generation) is int
        and generation > 0
        and account
        and trading_day
        and feed_state.get("connected") is True
        and feed_state.get("trading_ready") is True
        and feed_state.get("settlement_readback_verified") is True
        and request_trader is stream_trader
        and trader_state.get("connected") is True
        and trader_state.get("trading_ready") is True
        and trader_state.get("settlement_readback_verified") is True
        and str(trader_state.get("auth_state") or "").strip().lower()
        in {"authenticated", "success", "logged_in"}
        and str(trader_state.get("login_state") or "").strip().lower() in {"logged_in", "ready"}
        and str(trader_state.get("settlement_state") or "").strip().lower()
        in {"confirmed", "ready"}
        and trader_state.get("connection_generation") == generation
        and canonical_ctp_account_fingerprint(trader_state.get("account_fingerprint")) == account
        and str(trader_state.get("trading_day") or "").strip() == trading_day
        and not trader_state.get("last_error")
    )


__all__ = ("select_ctp_trade_stream", "is_ctp_trade_stream_ready")
