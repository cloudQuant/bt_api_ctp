"""Pure CTP order/trade projection for a caller that already checked provenance.

No source or session is authenticated here. In particular, no trading day or
front/session identity is filled from the current login. CTP prices are native
doubles: ``Decimal(str(value))`` avoids an additional binary conversion but
cannot recover precision lost before the CTP double was received.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from bt_api_base.direct_write_protocol import (
    DirectExecutionEventKind,
    DirectOrderStatus,
    NativeOrderIdentity,
)
from bt_api_base.gateway_execution_protocol import GatewayReturnFact
from bt_api_base.market_protocol import MarketInstrumentKey

from bt_api_ctp.native_session_id import is_ctp_session_id

ROUTE = "CTP___FUTURE"
_MISSING = object()
_SIDE = {"0": "buy", "1": "sell"}
_OFFSET = {
    "0": "open",
    "1": "close",
    "2": "close",
    "3": "close_today",
    "4": "close_yesterday",
    "5": "close",
    "6": "close",
}
_STATUS = {
    "0": DirectOrderStatus.FILLED,
    "1": DirectOrderStatus.PARTIALLY_FILLED,
    "2": DirectOrderStatus.CANCELLED,
    "3": DirectOrderStatus.ACCEPTED,
    "4": DirectOrderStatus.CANCELLED,
    "5": DirectOrderStatus.CANCELLED,
    "b": DirectOrderStatus.SUBMITTED,
    "c": DirectOrderStatus.SUBMITTED,
}


class CtpGatewayReturnProjectionError(ValueError):
    """Finite value-free failure; raw native values never appear in errors."""

    def __init__(self) -> None:
        self.code = "INVALID_CTP_RETURN"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class CtpGatewayReturnProjection:
    """Facts and separately classed native identities, not authorization."""

    fact: GatewayReturnFact
    binding_identity: NativeOrderIdentity | None
    venue_identity: NativeOrderIdentity | None


def _field(row: Any, name: str) -> object:
    if isinstance(row, Mapping):
        return row.get(name, _MISSING)
    return getattr(row, name, _MISSING)


def _raw_text(value: object, *, optional: bool = False) -> str | None:
    if value is _MISSING or value is None:
        if optional:
            return None
        raise ValueError
    if type(value) is bytes:
        value = value.decode("ascii", errors="strict")
    if type(value) is not str:
        raise ValueError
    value = value.rstrip("\x00")
    if "\x00" in value or len(value.encode("utf-8", errors="strict")) > 256:
        raise ValueError
    if not value.strip():
        if optional:
            return None
        raise ValueError
    if not value.isprintable():
        raise ValueError
    return value


def _text(value: object, *, optional: bool = False) -> str | None:
    raw = _raw_text(value, optional=optional)
    return None if raw is None else raw.strip()


def _label(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError
    return value


def _native_int(value: object, *, optional: bool, positive: bool = False) -> str | None:
    if value is _MISSING or value is None:
        if optional:
            return None
        raise ValueError
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError
    text = str(value)
    if len(text) > 36:
        raise ValueError
    return text


def _price(value: object, *, optional: bool) -> str | None:
    if value is _MISSING or value is None:
        if optional:
            return None
        raise ValueError
    if type(value) not in (int, float, Decimal) or (
        type(value) is float and not math.isfinite(value)
    ):
        raise ValueError
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError from None
    if not number.is_finite() or number <= 0 or number.adjusted() > 35:
        raise ValueError
    if abs(number.as_tuple().exponent) > 128:
        raise ValueError
    result = format(number, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    whole, _, fraction = result.partition(".")
    if len(whole) > 36 or len(fraction) > 18:
        raise ValueError
    return result


def _component(value: object) -> int | None:
    if value is _MISSING or value is None:
        return None
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError
    return value if value > 0 else None


def _session_component(value: object) -> int | None:
    """CTP SessionID is a signed 32-bit int; SimNow issues negative ones (I63)."""
    if value is _MISSING or value is None or value == 0:
        return None
    if not is_ctp_session_id(value):
        raise ValueError
    return value


def project_gateway_return(
    kind: str,
    row: Any,
    *,
    account_scope: str,
    source_ref: str,
    expected_trading_day: str,
    expected_broker: str,
    expected_investor: str,
    instrument: MarketInstrumentKey,
    source_generation: int | None = None,
) -> CtpGatewayReturnProjection:
    """Project one source-checked native order or trade without inventing keys."""
    try:
        if kind not in ("order", "trade") or type(kind) is not str:
            raise ValueError
        _label(account_scope)
        _label(source_ref)
        _label(expected_broker)
        _label(expected_investor)
        _label(expected_trading_day)
        if source_generation is not None and (
            type(source_generation) is not int or not 0 <= source_generation < 2**63
        ):
            raise ValueError
        if (
            type(instrument) is not MarketInstrumentKey
            or instrument.provider != "ctp"
            or instrument.product_type != "future"
            or instrument.native_symbol != instrument.instrument_id
        ):
            raise ValueError
        day = _text(_field(row, "TradingDay"))
        exchange = _text(_field(row, "ExchangeID"))
        symbol = _text(_field(row, "InstrumentID"))
        if (
            day != expected_trading_day
            or _text(_field(row, "BrokerID")) != expected_broker
            or _text(_field(row, "InvestorID")) != expected_investor
            or exchange != instrument.venue
            or symbol != instrument.native_symbol
        ):
            raise ValueError
        ref = _text(_field(row, "OrderRef"), optional=True)
        raw_venue = _raw_text(_field(row, "OrderSysID"), optional=True)
        venue = None if raw_venue is None else raw_venue.strip()
        front = _component(_field(row, "FrontID")) if kind == "order" else None
        session = _session_component(_field(row, "SessionID")) if kind == "order" else None
        fields: dict[str, Any] = {
            "trading_day": day,
            "order_ref": ref,
            "exchange_id": exchange,
            "front_id": front,
            "session_id": session,
        }
        if venue is not None:
            fields.update(order_sys_id=venue, order_sys_id_raw=raw_venue)
        identity = NativeOrderIdentity(ROUTE, ref, fields, None)
        binding = None
        if kind == "order" and ref is not None and front is not None and session is not None:
            binding = NativeOrderIdentity(
                ROUTE,
                ref,
                {"trading_day": day, "order_ref": ref, "front_id": front, "session_id": session},
                None,
            )
        venue_identity = None
        if venue is not None:
            venue_identity = NativeOrderIdentity(
                ROUTE,
                None,
                {
                    "trading_day": day,
                    "front_id": None,
                    "session_id": None,
                    "exchange_id": exchange,
                    "order_sys_id": venue,
                    "order_sys_id_raw": raw_venue,
                },
                None,
            )
        side_raw = _text(_field(row, "Direction"), optional=kind == "order")
        offset_raw = _text(
            _field(row, "CombOffsetFlag" if kind == "order" else "OffsetFlag"),
            optional=kind == "order",
        )
        side = None if side_raw is None else _SIDE.get(side_raw)
        offset = None if offset_raw is None else _OFFSET.get(offset_raw)
        if (side_raw is not None and side is None) or (offset_raw is not None and offset is None):
            raise ValueError
        common = dict(
            account_scope=account_scope,
            route=ROUTE,
            instrument=instrument,
            trading_day=day,
            source_ref=source_ref,
            identity=identity,
            side=side,
            offset=offset,
            session_generation=source_generation,
        )
        if kind == "order":
            status_raw = _text(_field(row, "OrderStatus"))
            submit_status = _text(_field(row, "OrderSubmitStatus"), optional=True)
            if submit_status is not None and submit_status not in {
                "0",
                "1",
                "2",
                "3",
                "4",
                "5",
                "6",
            }:
                raise ValueError
            if status_raw == "a" and submit_status in {"0", "1", "2"}:
                # 'a' (unknown) with an insert/cancel/modify submitted: the front
                # accepted it and the exchange has not answered. CTP sends it as
                # the first OnRtnOrder of every order (SimNow 63.09, 2026-11-10).
                status = DirectOrderStatus.SUBMITTED
            elif status_raw not in _STATUS and not (status_raw == "a" and submit_status == "4"):
                raise ValueError
            else:
                status = DirectOrderStatus.REJECTED if submit_status == "4" else _STATUS[status_raw]
            quantity = _native_int(_field(row, "VolumeTotalOriginal"), optional=True, positive=True)
            filled = _native_int(_field(row, "VolumeTraded"), optional=True)
            remaining = _native_int(_field(row, "VolumeTotal"), optional=True)
            if quantity is not None and (
                (filled is not None and int(filled) > int(quantity))
                or (remaining is not None and int(remaining) > int(quantity))
            ):
                raise ValueError
            order_type = _text(_field(row, "OrderPriceType"), optional=True)
            if order_type not in (None, "1", "2"):
                raise ValueError
            limit = None if order_type == "1" else _price(_field(row, "LimitPrice"), optional=True)
            fact = GatewayReturnFact(
                kind=DirectExecutionEventKind.ORDER,
                status=status,
                order_quantity=quantity,
                filled_quantity=filled,
                remaining_quantity=remaining,
                limit_price=limit,
                **common,
            )
        else:
            trade_id = _text(_field(row, "TradeID"))
            quantity = _native_int(_field(row, "Volume"), optional=False, positive=True)
            price = _price(_field(row, "Price"), optional=False)
            trade_time = _text(_field(row, "TradeTime"), optional=True)
            trade_date = _text(_field(row, "TradeDate"), optional=True)
            fact = GatewayReturnFact(
                kind=DirectExecutionEventKind.TRADE,
                schema_version=4,
                trade_key=(day, exchange, symbol, trade_id),
                trade_quantity=quantity,
                trade_price=price,
                trade_time=trade_time,
                trade_date=trade_date,
                **common,
            )
        return CtpGatewayReturnProjection(fact, binding, venue_identity)
    except Exception:
        raise CtpGatewayReturnProjectionError() from None
