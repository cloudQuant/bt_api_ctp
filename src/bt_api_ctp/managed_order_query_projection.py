"""CTP native order-query field projection for managed execution.

This projection cannot establish the origin of a query, a callback, an owner,
or a lease. The SDK reader keeps its query fence and evidence issuance.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from bt_api_execution import (
    CtpOwnerCallbackObservationSnapshotV1,
    ContractValidationError, CtpCallbackSessionBindingV1, CtpOrderIdentityReservation,
    OrderIntent, OrderType, PositionEffect, Side, TimeInForce,
)


from .query import QueryResult
from .containers.ctp.ctp_native_query_certificate import _record_value


_SAFE_ORDER_FIELDS = (
    "BrokerID",
    "InvestorID",
    "UserID",
    "InstrumentID",
    "ExchangeID",
    "OrderRef",
    "OrderSysID",
    "RequestID",
    "FrontID",
    "SessionID",
    "TradingDay",
    "SequenceNo",
    "NotifySequence",
    "Direction",
    "CombOffsetFlag",
    "CombHedgeFlag",
    "OrderStatus",
    "OrderSubmitStatus",
    "VolumeTotalOriginal",
    "VolumeTotal",
    "VolumeTraded",
    "LimitPrice",
)


_DIRECTION = {Side.BUY: "0", Side.SELL: "1"}


_OFFSET = {
    (PositionEffect.OPEN, "ALL"): "0",
    (PositionEffect.CLOSE, "ALL"): "1",
    (PositionEffect.CLOSE, "TODAY"): "3",
    (PositionEffect.CLOSE, "YESTERDAY"): "4",
    (PositionEffect.CLOSE_TODAY, "TODAY"): "3",
    (PositionEffect.CLOSE_YESTERDAY, "YESTERDAY"): "4",
}


def _reject(message: str, *, cause: BaseException | None = None) -> None:
    error = ContractValidationError(message)
    if cause is None:
        raise error
    raise error from cause


def _ctp_text(value: object, name: str, limit: int) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or not value.isascii()
        or len(value.encode("ascii")) > limit
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value)
    ):
        _reject(f"invalid CTP {name}")
    return value


def _exact_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _reject(f"invalid CTP {name}")
    return value


def _order_offset(intent: OrderIntent, exchange_id: str) -> str:
    if (
        exchange_id in {"SHFE", "INE"}
        and intent.position_effect is PositionEffect.CLOSE
        and intent.position_bucket == "ALL"
    ):
        _reject("SHFE/INE close query requires an explicit position bucket")
    offset = _OFFSET.get((intent.position_effect, intent.position_bucket))
    if offset is None:
        _reject("unsupported CTP position effect/bucket")
    return offset


def _intent_native_payload(
    intent: OrderIntent,
    reservation: CtpOrderIdentityReservation,
    binding: CtpCallbackSessionBindingV1,
    broker_id: str,
    user_id: str,
    exchange_id: str,
) -> dict[str, Any]:
    if (
        type(intent) is not OrderIntent
        or intent.scope.provider != "ctp"
        or intent.scope.environment != "simulation"
        or intent.position_side != "NET"
        or intent.tags
        or intent.order_type is not OrderType.LIMIT
        or intent.time_in_force is not TimeInForce.DAY
        or intent.price is None
        or type(intent.price) is not Decimal
        or not intent.price.is_finite()
        or intent.price <= 0
        or type(intent.quantity) is not Decimal
        or not intent.quantity.is_finite()
        or intent.quantity <= 0
        or intent.quantity > Decimal(2_147_483_647)
        or intent.quantity != Decimal(int(intent.quantity))
    ):
        _reject("CTP order query requires the exact supported LIMIT/DAY intent")
    if intent.reduce_only is not (intent.position_effect is not PositionEffect.OPEN):
        _reject("CTP intent effect and reduce-only flag disagree")
    if reservation.managed_intent_id != intent.intent_id:
        _reject("CTP identity reservation differs from managed intent")
    if (
        reservation.account_key != intent.scope.account_key
        or reservation.scope_key != intent.scope.key
        or reservation.trading_day != intent.scope.trading_day
        or binding.account_key != reservation.account_key
        or binding.scope_key != reservation.scope_key
        or binding.trading_day != reservation.trading_day
    ):
        _reject("CTP intent reservation differs from active session scope")
    direction = _DIRECTION.get(intent.side)
    if direction is None:
        _reject("unsupported CTP side")
    native_price = float(intent.price)
    if (
        not math.isfinite(native_price)
        or native_price <= 0
        or Decimal(str(native_price)) != intent.price
    ):
        _reject("CTP LIMIT price is not exactly representable as a native double")
    return {
        "BrokerID": broker_id,
        "InvestorID": user_id,
        "UserID": user_id,
        "InstrumentID": intent.instrument,
        "ExchangeID": exchange_id,
        "OrderRef": reservation.order_ref,
        "Direction": direction,
        "CombOffsetFlag": _order_offset(intent, exchange_id),
        "CombHedgeFlag": "1",
        "VolumeTotalOriginal": int(intent.quantity),
        "MinVolume": 1,
        "ForceCloseReason": "0",
        "IsAutoSuspend": 0,
        "UserForceClose": 0,
        "ContingentCondition": "1",
        "LimitPrice": native_price,
        "OrderPriceType": "2",
        "TimeCondition": "3",
        "VolumeCondition": "1",
    }



def validate_open_order_query_row(
    result: QueryResult[Any],
    intent: OrderIntent,
    reservation: CtpOrderIdentityReservation,
    binding: CtpCallbackSessionBindingV1,
    broker_id: str,
    user_id: str,
    exchange_id: str,
    order_sys_id: str,
) -> tuple[dict[str, Any], str, int, int, int]:
    record = result.records[0]
    row: dict[str, Any] = {}
    for name in _SAFE_ORDER_FIELDS:
        present, value = _record_value(record, name)
        if not present:
            _reject(f"CTP query row is missing {name}")
        row[name] = value
    expected_native = _intent_native_payload(
        intent, reservation, binding, broker_id, user_id, exchange_id
    )
    exact = {
        "BrokerID": broker_id,
        "InvestorID": user_id,
        "UserID": user_id,
        "InstrumentID": intent.instrument,
        "ExchangeID": exchange_id,
        "OrderRef": reservation.order_ref,
        "OrderSysID": order_sys_id,
        "FrontID": binding.dispatch_front_id,
        "SessionID": binding.dispatch_session_id,
        "TradingDay": binding.trading_day,
        "Direction": expected_native["Direction"],
        "CombOffsetFlag": expected_native["CombOffsetFlag"],
        "CombHedgeFlag": "1",
        "OrderSubmitStatus": "3",
        "VolumeTotalOriginal": int(intent.quantity),
    }
    if any(type(row[name]) is not type(value) or row[name] != value for name, value in exact.items()):
        _reject("CTP query row differs from the reserved native order identity")
    if type(row["RequestID"]) is not int or row["RequestID"] < 0:
        _reject("CTP native order RequestID is invalid")
    for name in ("SequenceNo", "NotifySequence"):
        _exact_int(row[name], name)
    quantity = int(intent.quantity)
    traded = _exact_int(row["VolumeTraded"], "VolumeTraded")
    remaining = _exact_int(row["VolumeTotal"], "VolumeTotal")
    if traded >= quantity or remaining != quantity - traded:
        _reject("CTP query row open quantity is inconsistent")
    price = row["LimitPrice"]
    if (
        type(price) is not float
        or not math.isfinite(price)
        or price <= 0
        or Decimal(str(price)) != intent.price
    ):
        _reject("CTP query row LIMIT price differs from the durable intent")
    status = row["OrderStatus"]
    if status == "3" and traded == 0:
        provider_state = "OPEN"
    elif status == "1" and traded > 0:
        provider_state = "PARTIAL"
    else:
        _reject("CTP query row does not prove a live OPEN/PARTIAL order")
    return row, provider_state, quantity, traded, remaining


def validate_terminal_order_query_row(
    result: QueryResult[Any],
    intent: OrderIntent,
    reservation: CtpOrderIdentityReservation,
    binding: CtpCallbackSessionBindingV1,
    snapshot: CtpOwnerCallbackObservationSnapshotV1,
    broker_id: str,
    user_id: str,
    exchange_id: str,
    order_sys_id: str,
) -> tuple[dict[str, Any], str]:
    if type(result.records) is not tuple or len(result.records) != 1:
        _reject("CTP terminal query must return exactly one row")
    row: dict[str, Any] = {}
    record = result.records[0]
    from bt_api_ctp.containers.ctp.ctp_native_query_certificate import _record_value

    for name in _SAFE_ORDER_FIELDS:
        present, value = _record_value(record, name)
        if not present:
            _reject("CTP terminal query row is missing " + name)
        row[name] = value
    expected_native = _intent_native_payload(
        intent, reservation, binding, broker_id, user_id, exchange_id
    )
    exact = {
        "BrokerID": broker_id,
        "InvestorID": user_id,
        "UserID": user_id,
        "InstrumentID": intent.instrument,
        "ExchangeID": exchange_id,
        "OrderRef": reservation.order_ref,
        "OrderSysID": order_sys_id,
        "RequestID": snapshot.request_id,
        "FrontID": snapshot.front_id,
        "SessionID": snapshot.session_id,
        "TradingDay": snapshot.trading_day,
        "Direction": expected_native["Direction"],
        "CombOffsetFlag": expected_native["CombOffsetFlag"],
        "CombHedgeFlag": "1",
        "VolumeTotalOriginal": int(intent.quantity),
        "VolumeTraded": 0,
        "VolumeTotal": int(intent.quantity),
    }
    if any(type(row[name]) is not type(value) or row[name] != value for name, value in exact.items()):
        _reject("CTP terminal query row differs from the exact durable target")
    for name in ("SequenceNo", "NotifySequence"):
        _exact_int(row[name], name)
    state = snapshot.provider_state
    if state == "CANCELLED":
        if row["OrderStatus"] != "5" or row["OrderSubmitStatus"] not in {"1", "3"}:
            _reject("CTP query row does not prove the terminal cancelled callback state")
    elif state == "REJECTED":
        if row["OrderStatus"] != "4" or row["OrderSubmitStatus"] != "4":
            _reject("CTP query row does not prove the terminal rejected callback state")
    else:
        _reject("CTP callback state is not terminal")
    price = row["LimitPrice"]
    if (
        type(price) is not float
        or not math.isfinite(price)
        or price <= 0
        or Decimal(str(price)) != intent.price
    ):
        _reject("CTP terminal query row LIMIT price differs from the durable intent")
    return row, state

