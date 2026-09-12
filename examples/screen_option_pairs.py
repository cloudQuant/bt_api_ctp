"""Capital estimates for two-instrument futures/long-option research pairs.

This pure calculation consumes verified query records. It neither logs in nor
submits orders. Capital sufficiency is not a trading signal or risk admission.
Use account-specific rates; an absent rate must never be replaced with zero.
"""

from decimal import Decimal, InvalidOperation


def _number(record, name, *, positive=False):
    try:
        value = Decimal(str(record[name]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"missing_or_invalid:{name}") from exc
    if not value.is_finite() or value < 0 or value >= Decimal("1e308"):
        raise ValueError(f"missing_or_invalid:{name}")
    if positive and value == 0:
        raise ValueError(f"non_positive:{name}")
    return value


def _book(quote):
    bid = _number(quote, "BidPrice1", positive=True)
    ask = _number(quote, "AskPrice1", positive=True)
    if bid > ask:
        raise ValueError("crossed_book")
    return bid, ask


def _round_trip_fee(fee, notional):
    def amount(prefix):
        return notional * _number(fee, prefix + "RatioByMoney") + _number(
            fee, prefix + "RatioByVolume"
        )

    return amount("Open") + max(amount("Close"), amount("CloseToday"))


def estimate_buy_option_pair(
    future,
    option,
    future_quote,
    option_quote,
    margin,
    future_fee,
    option_fee,
    *,
    option_lots=1,
    budget=10000,
    reserve=0,
):
    """Estimate one future plus N long options at the observed top of book.

    Put pairs buy the future; call pairs sell it. One option lot is a protective
    pair only when contract economics match. N=2 is merely a quantity scenario,
    never assumed delta-neutral. No seller-margin discount is involved.

    The future margin uses the larger of bid/ask/valid last/prior settlement.
    Fees reserve open plus the larger close/close-today fee at that price basis;
    Future/option price changes can change cash needs and amount-based exit
    fees. Financing, exercise and emergency slippage are outside this static
    estimate. Caller supplies and labels a separate reserve, and verifies raw
    symbol/exchange, query completion, account/generation and quantity limits.
    """
    if type(option_lots) is not int or option_lots < 1:
        raise ValueError("option_lots_must_be_positive_integer")
    if (
        not future.get("instrument_id")
        or option.get("underlying_instrument") != future["instrument_id"]
        or not future.get("exchange_id")
        or option.get("exchange_id") != future["exchange_id"]
    ):
        raise ValueError("underlying_mismatch")
    kind = option.get("option_type")
    if kind not in ("call", "put"):
        raise ValueError("unknown_option_type")
    budget_d = _number({"budget": budget}, "budget", positive=True)
    reserve_d = _number({"reserve": reserve}, "reserve")
    fm = _number(future, "multiplier", positive=True)
    om = _number(option, "multiplier", positive=True)
    fbid, fask = _book(future_quote)
    _, oask = _book(option_quote)
    side = "buy" if kind == "put" else "sell"
    if _number(future_quote, "AskVolume1" if side == "buy" else "BidVolume1") < 1:
        raise ValueError("insufficient_future_depth")
    if _number(option_quote, "AskVolume1") < option_lots:
        raise ValueError("insufficient_option_depth")
    margin_price = fask
    for name in ("LastPrice", "PreSettlementPrice"):
        try:
            margin_price = max(margin_price, _number(future_quote, name, positive=True))
        except ValueError:
            pass
    prefix = "Long" if side == "buy" else "Short"
    future_margin = margin_price * fm * _number(margin, prefix + "MarginRatioByMoney") + _number(
        margin, prefix + "MarginRatioByVolume"
    )
    premium = oask * om * option_lots
    fees = (
        _round_trip_fee(future_fee, margin_price * fm)
        + _round_trip_fee(option_fee, oask * om) * option_lots
    )
    base = future_margin + premium + fees
    total = base + reserve_d
    return {
        "exchange": future["exchange_id"],
        "future": future["instrument_id"],
        "option": option["instrument_id"],
        "future_side": side,
        "future_lots": 1,
        "option_side": "buy",
        "option_lots": option_lots,
        "future_price": float(fask if side == "buy" else fbid),
        "option_ask": float(oask),
        "margin_price_basis": float(margin_price),
        "future_margin": float(future_margin),
        "option_premium": float(premium),
        "round_trip_fee_reserve": float(fees),
        "base_capital": float(base),
        "additional_reserve": float(reserve_d),
        "capital_with_reserve": float(total),
        "budget": float(budget_d),
        "budget_headroom": float(budget_d - total),
        "capital_fits": total <= budget_d,
        "economic_signal": "NOT_EVALUATED",
        "delta": None,
        "risk_admission": "NOT_EVALUATED",
        "quote_execution": "NOT_VERIFIED",
    }
