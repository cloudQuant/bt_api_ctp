from __future__ import annotations

from importlib import import_module as _import_module

__all__ = [
    "CTP_DIRECTION_MAP",
    "CTP_ORDER_STATUS_MAP",
    "CTP_POS_DIRECTION_MAP",
    "CtpAccountData",
    "CtpBarData",
    "CtpOrderData",
    "CtpPositionData",
    "CtpTickerData",
    "CtpTradeData",
]

_EXPORT_MODULES = {
    "CTP_DIRECTION_MAP": ".ctp_order",
    "CTP_ORDER_STATUS_MAP": ".ctp_order",
    "CTP_POS_DIRECTION_MAP": ".ctp_position",
    "CtpAccountData": ".ctp_account",
    "CtpBarData": ".ctp_bar",
    "CtpOrderData": ".ctp_order",
    "CtpPositionData": ".ctp_position",
    "CtpTickerData": ".ctp_ticker",
    "CtpTradeData": ".ctp_trade",
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(_import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()).union(__all__))
