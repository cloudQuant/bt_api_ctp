"""CTP native field normalization shared by direct Store adapters.

This module is the single source of truth for CTP direction/offset/hedge flag
maps, order-status normalization, and safe SWIG-field extraction. It has no
Backtrader, execution-plugin, or network imports.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "DIRECTION_FLAG",
    "DIRECTION_MAP",
    "OFFSET_FLAG",
    "OFFSET_MAP",
    "ORDER_FIELDS",
    "ORDER_STATUS_MAP",
    "ORDER_SUBMIT_STATUS_MAP",
    "LOGIN_FIELDS",
    "RSPINFO_FIELDS",
    "TRADE_FIELDS",
    "QUERY_RECORD_FIELDS",
    "ctp_code",
    "ctp_direction",
    "ctp_offset",
    "ctp_position_direction",
    "extract_fields",
    "field_to_dict",
    "normalize_order_status",
    "safe_field_attr",
    "safe_text_attr",
]

OFFSET_FLAG = {
    "open": "0",
    "close": "1",
    "force_close": "2",
    "close_today": "3",
    "close_yesterday": "4",
    "force_close_yesterday": "5",
    "local_force_close": "6",
}
OFFSET_MAP = {value: key for key, value in OFFSET_FLAG.items()}
DIRECTION_FLAG = {"buy": "0", "sell": "1"}
DIRECTION_MAP = {value: key for key, value in DIRECTION_FLAG.items()}
ORDER_STATUS_MAP = {
    "0": "completed",
    "1": "partial",
    "2": "canceled",
    "3": "accepted",
    "4": "canceled",
    "5": "canceled",
    "a": "submitted",
    "b": "submitted",
    "c": "submitted",
}
ORDER_SUBMIT_STATUS_MAP = {
    "4": "rejected",
    "5": "cancel_rejected",
    "6": "rejected",
}
ORDER_FIELDS = (
    "AccountID",
    "ActiveTime",
    "ActiveTraderID",
    "ActiveUserID",
    "BranchID",
    "BrokerID",
    "BrokerOrderSeq",
    "BusinessUnit",
    "CancelTime",
    "ClearingPartID",
    "ClientID",
    "CombHedgeFlag",
    "CombOffsetFlag",
    "ContingentCondition",
    "CurrencyID",
    "Direction",
    "ExchangeID",
    "ExchangeInstID",
    "ForceCloseReason",
    "FrontID",
    "GTDDate",
    "IPAddress",
    "InsertDate",
    "InsertTime",
    "InstallID",
    "InstrumentID",
    "InvestUnitID",
    "InvestorID",
    "IsAutoSuspend",
    "IsSwapOrder",
    "LimitPrice",
    "MacAddress",
    "MinVolume",
    "NotifySequence",
    "OrderLocalID",
    "OrderMemo",
    "OrderPriceType",
    "OrderRef",
    "OrderSource",
    "OrderStatus",
    "OrderSubmitStatus",
    "OrderSysID",
    "OrderType",
    "ParticipantID",
    "RelativeOrderSysID",
    "RequestID",
    "SequenceNo",
    "SessionID",
    "SessionReqSeq",
    "SettlementID",
    "StatusMsg",
    "StopPrice",
    "SuspendTime",
    "TimeCondition",
    "TraderID",
    "TradingDay",
    "UpdateTime",
    "UserForceClose",
    "UserID",
    "UserProductInfo",
    "VolumeCondition",
    "VolumeTotal",
    "VolumeTotalOriginal",
    "VolumeTraded",
    "ZCETotalTradedVolume",
    "reserve1",
    "reserve2",
    "reserve3",
)


LOGIN_FIELDS = (
    "FrontID",
    "SessionID",
    "TradingDay",
    "LoginTime",
    "BrokerID",
    "UserID",
    "SystemName",
)
RSPINFO_FIELDS = (
    "ErrorID",
    "ErrorMsg",
)
TRADE_FIELDS = (
    "BrokerID",
    "BrokerOrderSeq",
    "BusinessUnit",
    "ClearingPartID",
    "ClientID",
    "Direction",
    "ExchangeID",
    "ExchangeInstID",
    "HedgeFlag",
    "InstrumentID",
    "InvestUnitID",
    "InvestorID",
    "OffsetFlag",
    "OrderLocalID",
    "OrderRef",
    "OrderSysID",
    "ParticipantID",
    "Price",
    "PriceSource",
    "SequenceNo",
    "SettlementID",
    "TradeDate",
    "TradeID",
    "TradeSource",
    "TradeTime",
    "TradeType",
    "TraderID",
    "TradingDay",
    "TradingRole",
    "UserID",
    "Volume",
    "reserve1",
    "reserve2",
)
QUERY_RECORD_FIELDS = tuple(
    dict.fromkeys(
        ORDER_FIELDS
        + TRADE_FIELDS
        + (
            "AccountID", "Available", "Balance", "CloseProfit", "Commission",
            "CloseRatioByMoney", "CloseRatioByVolume", "CloseTodayRatioByMoney",
            "CloseTodayRatioByVolume", "CombinationType", "CurrMargin", "CreateDate",
            "DeliveryMonth", "DeliveryYear", "EndDelivDate", "ExchangeID",
            "ExchangeInstID", "ExchFixedMargin", "ExchMiniMargin", "ExpireDate",
            "FixedMargin", "HedgeFlag", "InstrumentID", "InstLifePhase", "IsTrading",
            "InvestUnitID", "InvestorID", "LongFrozen", "LongMarginRatio",
            "LongMarginRatioByMoney", "LongMarginRatioByVolume", "LowerLimitPrice",
            "MaxMarginSideAlgorithm", "MaxLimitOrderVolume", "MaxMarketOrderVolume",
            "MinLimitOrderVolume", "MinMarketOrderVolume", "MiniMargin", "OpenDate",
            "OpenInterest", "OpenRatioByMoney", "OpenRatioByVolume", "OptionsType",
            "PosiDirection", "Position", "PositionCost", "PositionProfit",
            "PositionDateType", "PositionType", "PriceTick", "ProductID",
            "ProductClass", "Royalty", "ShortFrozen", "ShortMarginRatio",
            "ShortMarginRatioByMoney", "ShortMarginRatioByVolume", "StartDelivDate",
            "StrikePrice", "StrikeRatioByMoney", "StrikeRatioByVolume", "TodayPosition",
            "TradingDay", "UnderlyingInstrID", "UnderlyingMultiple", "UpperLimitPrice",
            "Volume", "VolumeMultiple", "YdPosition", "ranking_trading_day",
            "trading_days_to_expiry",
        )
    )
)


def _text(value: Any) -> str:
    """Convert a vendor scalar to stripped text without raising."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        for encoding in ("utf-8", "gbk", "latin1"):
            try:
                return value.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
        return value.decode("utf-8", errors="ignore").strip()
    try:
        return str(value).strip()
    except Exception:
        return ""


def ctp_code(value: Any, default: str = "") -> str:
    """Normalize CTP enum-like fields that may arrive as numeric strings."""
    if value is None:
        return default
    text = _text(value).replace(",", "")
    if text == "":
        return default
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if not number.is_finite():
        return default
    if number == number.to_integral_value():
        return str(int(number))
    return text


def ctp_direction(value: Any, default: str = "buy") -> str:
    """Normalize CTP buy/sell direction flags."""
    code = ctp_code(value, "")
    direction = DIRECTION_MAP.get(code)
    if direction is not None:
        return direction
    text = code.lower().replace("-", "_")
    if text in {"buy", "long", "b"}:
        return "buy"
    if text in {"sell", "short", "s"}:
        return "sell"
    return default


def ctp_offset(value: Any, default: str = "open") -> str:
    """Normalize CTP offset flags."""
    code = ctp_code(value, "")
    offset = OFFSET_MAP.get(code) or OFFSET_MAP.get(code[:1])
    if offset is not None:
        return offset
    text = code.lower().replace("-", "_")
    if text in OFFSET_FLAG:
        return text
    return default


def ctp_position_direction(value: Any, default: str = "long") -> str:
    """Normalize CTP position direction flags (2 long, 3 short)."""
    code = ctp_code(value, "")
    text = code.lower().replace("-", "_")
    if text in {"3", "short", "sell", "s"}:
        return "short"
    if text in {"2", "long", "buy", "b"}:
        return "long"
    return default


def normalize_order_status(
    order_status: Any,
    submit_status: Any = None,
    default: str = "submitted",
) -> str:
    """Normalize CTP order status, letting explicit submit rejections win."""
    status = ORDER_STATUS_MAP.get(ctp_code(order_status, "a"), default)
    submit_override = ORDER_SUBMIT_STATUS_MAP.get(ctp_code(submit_status, ""))
    return submit_override or status


def safe_field_attr(obj: Any, attr: str, default: Any = None) -> Any:
    """Read an attribute from a SWIG field or a pre-snapshotted dict."""
    if isinstance(obj, dict):
        return obj.get(attr, default)
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def safe_text_attr(obj: Any, *attrs: str, default: str = "") -> str:
    """Return the first non-empty text attribute from a vendor object safely."""
    for attr in attrs:
        text = _text(safe_field_attr(obj, attr))
        if text:
            return text
    return default


def field_to_dict(field: Any) -> dict[str, Any]:
    """Convert a SWIG-generated CTP struct instance into a plain dict."""
    if field is None:
        return {}
    result: dict[str, Any] = {}
    for attr in dir(field):
        if attr.startswith("_") or attr in {"this", "thisown"}:
            continue
        try:
            value = getattr(field, attr)
        except Exception:
            continue
        if callable(value):
            continue
        result[attr] = value
    return result


def extract_fields(field: Any, attrs: Iterable[str]) -> dict[str, Any]:
    """Read only a whitelisted subset of SWIG CTP struct attributes safely."""
    if field is None:
        return {}
    result: dict[str, Any] = {}
    for attr in attrs:
        try:
            value = getattr(field, attr)
        except Exception:
            continue
        if callable(value):
            continue
        result[attr] = value
    return result
