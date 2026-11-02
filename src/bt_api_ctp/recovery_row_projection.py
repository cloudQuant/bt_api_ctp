"""Project CTP recovery row fields without owning session or authority.

The SDK supplies its value selector and canonical instrument resolver. The
caller retains proof, query, journal, generation and authorization checks.
"""

from decimal import Decimal, InvalidOperation


def recovery_row_instrument(row, *, proof=None, recovery_value, canonicalize):
    symbol = recovery_value(row, "symbol", "instrument", "instrument_id", "InstrumentID")
    exchange_id = recovery_value(row, "exchange_id", "ExchangeID")
    return canonicalize(proof, symbol, exchange_id)


def recovery_row_side(row, *, position=False, recovery_value):
    names = (
        ("position_side", "position_direction", "PosiDirection", "direction")
        if position
        else ("side", "direction", "Direction")
    )
    value = str(recovery_value(row, *names) or "").strip().lower()
    if position:
        return {
            "2": "long",
            "3": "short",
            "buy": "long",
            "sell": "short",
        }.get(value, value)
    return {"0": "buy", "1": "sell"}.get(value, value)


def canonical_recovery_offset(value):
    value = str(value or "").strip().lower().replace("-", "_")
    return {
        "0": "open",
        "1": "close",
        "3": "close_today",
        "4": "close_yesterday",
        "closetoday": "close_today",
        "closeyesterday": "close_yesterday",
    }.get(value, value)


def recovery_order_active(row, *, recovery_value):
    status = (
        str(row.get("status") or row.get("OrderStatus") or row.get("order_status") or "")
        .strip()
        .lower()
    )
    if status in {
        "0",
        "2",
        "4",
        "5",
        "filled",
        "completed",
        "canceled",
        "cancelled",
        "expired",
        "rejected",
    }:
        return False
    remaining = recovery_value(row, "remaining", "VolumeTotal", "volume_total")
    if remaining not in (None, ""):
        try:
            return Decimal(str(remaining)) > 0
        except InvalidOperation:
            return True
    return True


def recovery_order_matches(left, right, *, recovery_value):
    fields = (
        ("client_order_id", "OrderRef", "order_ref"),
        ("order_id", "OrderSysID", "venue_order_id"),
    )
    compared = False
    for names in fields:
        left_value = recovery_value(left, *names)
        right_value = recovery_value(right, *names)
        if left_value in (None, "") or right_value in (None, ""):
            continue
        compared = True
        if str(left_value).strip() != str(right_value).strip():
            return False
    return compared


def recovery_active_order_matches_intent(
    intent,
    order,
    *,
    proof,
    recovery_row_instrument,
    recovery_order_matches,
    recovery_row_side,
    canonical_recovery_offset,
    recovery_value,
    recovery_quantity,
):
    if recovery_row_instrument(intent, proof=proof) != recovery_row_instrument(order, proof=proof):
        return False
    if not recovery_order_matches(intent, order):
        return False
    try:
        intent_side = recovery_row_side(intent)
        order_side = recovery_row_side(order)
        intent_offset = canonical_recovery_offset(
            recovery_value(intent, "offset", "CombOffsetFlag")
        )
        order_offset = canonical_recovery_offset(
            recovery_value(
                order,
                "offset",
                "CombOffsetFlag",
                "OffsetFlag",
            )
        )
        intent_quantity = recovery_quantity(intent, "quantity", "size")
        order_quantity = recovery_quantity(
            order,
            "quantity",
            "size",
            "VolumeTotalOriginal",
            "volume_total_original",
        )
    except (InvalidOperation, TypeError, ValueError):
        return False
    hedge_flag = str(
        recovery_value(order, "hedge_flag", "CombHedgeFlag", "HedgeFlag") or ""
    ).strip()
    return bool(
        intent_side in {"buy", "sell"}
        and order_side == intent_side
        and intent_offset
        in {
            "open",
            "close",
            "close_today",
            "close_yesterday",
        }
        and order_offset == intent_offset
        and intent_quantity == order_quantity
        and hedge_flag == "1"
    )


def recovery_trade_key(row, *, proof, recovery_value, recovery_row_instrument):
    return (
        str(recovery_value(row, "trading_day", "TradingDay") or ""),
        recovery_row_instrument(row, proof=proof),
        str(recovery_value(row, "trade_id", "TradeID") or ""),
    )
