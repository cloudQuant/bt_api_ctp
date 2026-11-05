"""Provider-owned mapping of neutral order requests to CTP ``make_order`` arguments.

CTP accepts positive-price GFD limit orders only and needs an explicit
open/close flag; SHFE/INE positions opened today must be closed with
``close_today``.  The neutral ``offset`` (``open``, ``close``, ``close_today``,
``close_yesterday``) is passed through unchanged, so the strategy decides the
dated close explicitly instead of the SDK guessing it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from bt_api_base.contract_errors import CapabilityNotSupportedError
from bt_api_base.order_intent import position_intent


def _whole_lots(quantity: Any) -> int:
    value = Decimal(str(quantity))
    if not value.is_finite() or value <= 0 or value != value.to_integral_value():
        raise CapabilityNotSupportedError(
            "make_order", detail="CTP orders require a positive whole number of lots",
            definite_reject=True,
        )
    return int(value)


def map_order_request(request: Any) -> dict[str, Any]:
    """Map a v1 ``OrderRequest`` to CTP feed ``make_order`` arguments."""
    offset, _closing = position_intent(request)
    if request.order_type.value != "limit" or request.price is None or request.price <= 0:
        raise CapabilityNotSupportedError(
            "make_order", detail="CTP supports positive-price limit orders only",
            definite_reject=True,
        )
    if request.position_id is not None:
        raise CapabilityNotSupportedError(
            "make_order", detail="CTP does not support ticket-specific closes",
            definite_reject=True,
        )
    if request.time_in_force not in (None, "", "GFD", "gfd", "DAY", "day", "GTC", "gtc"):
        raise CapabilityNotSupportedError(
            "make_order", detail="CTP orders are good-for-day", definite_reject=True,
        )
    result: dict[str, Any] = {
        "symbol": request.symbol,
        "vol": _whole_lots(request.quantity),
        "price": float(request.price),
        "order_type": f"{request.side.value}-limit",
        "offset": offset,
        "post_only": False,
        "client_order_id": request.client_order_id,
        "time_in_force": "GFD",
    }
    if request.exchange_id:
        result["exchange_id"] = request.exchange_id
    return result


__all__ = ("map_order_request",)
