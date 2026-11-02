"""CTP native callback field mapping for optional managed execution.

The Execution package owns correlation and durable event types; mapping fields
does not attest their source or create execution authority.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from bt_api_execution.errors import ContractValidationError
from bt_api_execution.store import CtpDispatchCorrelationKey
from bt_api_execution.ctp_native_callbacks import (
    CtpNativeCallbackEnvelope,
    CtpNativeSessionContext,
    CtpNativeTradeFactV2,
    _ORDER_ACTION_SOURCES,
    _ORDER_INSERT_SOURCES,
    _make_callback_key,
    _validate_text,
)


def _read_field(field: object, name: str) -> object:
    try:
        if isinstance(field, Mapping):
            return field.get(name)
        return getattr(field, name)
    except Exception as exc:
        raise ContractValidationError("native CTP callback field is unavailable") from exc


def _native_text(field: object, name: str, *, required: bool = False) -> str | None:
    try:
        value = _read_field(field, name)
    except ContractValidationError:
        if required:
            raise
        return None
    if value is None:
        result = ""
    elif isinstance(value, bytes):
        try:
            result = value.decode("utf-8").rstrip("\x00").strip()
        except UnicodeDecodeError as exc:
            raise ContractValidationError("native CTP callback text is not UTF-8") from exc
    elif type(value) is str:
        result = value.rstrip("\x00").strip()
    elif type(value) is int and not isinstance(value, bool):
        result = str(value)
    else:
        raise ContractValidationError("native CTP callback text has an invalid type")
    if required and not result:
        raise ContractValidationError("native CTP callback field is required: " + name)
    return result or None


def _native_int(field: object, name: str, *, required: bool = False) -> int | None:
    try:
        value = _read_field(field, name)
    except ContractValidationError:
        if required:
            raise
        return None
    if value is None or value == "":
        if required:
            raise ContractValidationError("native CTP callback field is required: " + name)
        return None
    if type(value) is not int:
        raise ContractValidationError("native CTP callback integer has an invalid type")
    if required and value <= 0:
        raise ContractValidationError("native CTP callback integer must be positive")
    return value


def _native_bool(value: object, field_name: str) -> bool:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise ContractValidationError("native CTP callback flag is invalid: " + field_name)


def _error_id(response_info: object | None) -> int | None:
    if response_info is None:
        return None
    return _native_int(response_info, "ErrorID")


def _account_fingerprint(field: object) -> str:
    broker_id = _native_text(field, "BrokerID", required=True)
    user_id = _native_text(field, "UserID", required=True)
    digest = sha256(f"{broker_id}:{user_id}".encode()).hexdigest()[:16]
    return "acct_" + digest


def map_ctp_native_order_return(
    correlation: CtpDispatchCorrelationKey,
    session: CtpNativeSessionContext,
    order_field: object,
) -> CtpNativeCallbackEnvelope:
    """Map one ``OnRtnOrder`` field when it carries exact request and event ids.

    CTP 6.7.7's order field carries RequestID, OrderRef, TradingDay, FrontID,
    SessionID and native notification sequence fields.  Events without a
    positive ``NotifySequence`` or ``SequenceNo`` are rejected instead of
    receiving a generated timestamp or counter.
    """

    if type(correlation) is not CtpDispatchCorrelationKey:
        raise ContractValidationError("typed CTP dispatch correlation key is required")
    session.require_correlation(correlation)
    if correlation.version != 2:
        raise ContractValidationError("native CTP callbacks require V2 command correlation")
    if correlation.operation != "SUBMIT":
        raise ContractValidationError("native CTP order return requires a submit command")

    order_ref = _native_text(order_field, "OrderRef", required=True)
    request_id = _native_int(order_field, "RequestID", required=True)
    front_id = _native_int(order_field, "FrontID", required=True)
    session_id = _native_int(order_field, "SessionID", required=True)
    trading_day = _native_text(order_field, "TradingDay", required=True)
    if order_ref != correlation.order_ref:
        raise ContractValidationError("native CTP order OrderRef does not match command")
    if request_id != correlation.native_request_id:
        raise ContractValidationError("native CTP order RequestID does not match command")
    if (front_id, session_id) != (correlation.dispatch_front_id, correlation.dispatch_session_id):
        raise ContractValidationError("native CTP order session does not match command")
    if trading_day != correlation.trading_day:
        raise ContractValidationError("native CTP order TradingDay does not match command")
    if _account_fingerprint(order_field) != session.account_fingerprint:
        raise ContractValidationError("native CTP order account does not match session")

    notify_sequence = _native_int(order_field, "NotifySequence")
    sequence_no = _native_int(order_field, "SequenceNo")
    if notify_sequence is not None and notify_sequence > 0:
        event_sequence = notify_sequence
        event_basis = "CThostFtdcOrderField.NotifySequence"
    elif sequence_no is not None and sequence_no > 0:
        event_sequence = sequence_no
        event_basis = "CThostFtdcOrderField.SequenceNo"
    else:
        raise ContractValidationError("native CTP order has no stable event sequence")

    exchange_id = _native_text(order_field, "ExchangeID")
    order_sys_id = _native_text(order_field, "OrderSysID")
    if not exchange_id or not order_sys_id:
        exchange_id = None
        order_sys_id = None
    key = _make_callback_key(
        correlation,
        session,
        callback_family="ORDER",
        event_id=f"on-rtn-order:{event_sequence}:{order_ref}",
        native_request_id=request_id,
        native_action_ref=None,
        order_ref=order_ref,
        exchange_id=exchange_id,
        order_sys_id=order_sys_id,
    )
    fields = (
        ("BrokerID", _native_text(order_field, "BrokerID", required=True)),
        ("InvestorID", _native_text(order_field, "InvestorID", required=True)),
        ("UserID", _native_text(order_field, "UserID", required=True)),
        ("InstrumentID", _native_text(order_field, "InstrumentID", required=True)),
        ("RequestID", request_id),
        ("OrderRef", order_ref),
        ("ExchangeID", _native_text(order_field, "ExchangeID")),
        ("OrderSysID", _native_text(order_field, "OrderSysID")),
        ("FrontID", front_id),
        ("SessionID", session_id),
        ("TradingDay", trading_day),
        ("OrderStatus", _native_text(order_field, "OrderStatus")),
        ("OrderSubmitStatus", _native_text(order_field, "OrderSubmitStatus")),
        ("VolumeTraded", _native_int(order_field, "VolumeTraded")),
        ("VolumeTotal", _native_int(order_field, "VolumeTotal")),
        ("UpdateTime", _native_text(order_field, "UpdateTime")),
        ("NotifySequence", notify_sequence),
        ("SequenceNo", sequence_no),
    )
    return CtpNativeCallbackEnvelope(
        callback_key=key,
        source_callback="OnRtnOrder",
        source_scope=session,
        event_identity_basis=event_basis,
        native_fields=fields,
    )


def map_ctp_native_order_action_callback(
    correlation: CtpDispatchCorrelationKey,
    session: CtpNativeSessionContext,
    source_callback: str,
    action_field: object,
    response_info: object | None = None,
    *,
    request_id: int | None = None,
    is_last: object | None = None,
) -> CtpNativeCallbackEnvelope:
    """Map one exact native cancel action response or error callback.

    ``OnRspOrderAction`` supplies RequestID as a callback argument and the
    action field; both must agree and the response must be terminal.
    ``OnErrRtnOrderAction`` has no separate RequestID argument, so its field
    must carry the exact request id.
    """

    if type(correlation) is not CtpDispatchCorrelationKey:
        raise ContractValidationError("typed CTP dispatch correlation key is required")
    session.require_correlation(correlation)
    if correlation.version != 2:
        raise ContractValidationError("native CTP callbacks require V2 command correlation")
    if correlation.operation != "CANCEL":
        raise ContractValidationError("native CTP order action requires a cancel command")
    if source_callback not in _ORDER_ACTION_SOURCES:
        raise ContractValidationError("unsupported native CTP order action callback")

    field_request_id = _native_int(action_field, "RequestID", required=True)
    assert field_request_id is not None
    if source_callback == "OnRspOrderAction":
        if type(request_id) is not int or request_id <= 0 or request_id != field_request_id:
            raise ContractValidationError("native CTP action RequestID fields do not match")
        terminal = _native_bool(is_last, "bIsLast")
        if not terminal:
            raise ContractValidationError("native CTP action response is not terminal")
        native_request_id = request_id
        response_is_last: bool | None = terminal
    else:
        if request_id is not None or is_last is not None:
            raise ContractValidationError("native CTP action error has no response arguments")
        native_request_id = field_request_id
        response_is_last = None
    if native_request_id != correlation.native_request_id:
        raise ContractValidationError("native CTP action RequestID does not match command")

    order_ref = _native_text(action_field, "OrderRef", required=True)
    action_ref = _native_int(action_field, "OrderActionRef", required=True)
    exchange_id = _native_text(action_field, "ExchangeID", required=True)
    order_sys_id = _native_text(action_field, "OrderSysID", required=True)
    target_front_id = _native_int(action_field, "FrontID", required=True)
    target_session_id = _native_int(action_field, "SessionID", required=True)
    action_flag = _native_text(action_field, "ActionFlag", required=True)
    if (
        order_ref != correlation.order_ref
        or action_ref != correlation.native_action_ref
        or exchange_id != correlation.cancel_target_exchange_id
        or order_sys_id != correlation.cancel_target_order_sys_id
        or target_front_id != correlation.cancel_target_front_id
        or target_session_id != correlation.cancel_target_session_id
        or action_flag != "0"
    ):
        raise ContractValidationError("native CTP action target does not match command")
    if _account_fingerprint(action_field) != session.account_fingerprint:
        raise ContractValidationError("native CTP action account does not match session")

    error_id = _error_id(response_info)
    event_id = f"{source_callback.lower()}:{native_request_id}:{action_ref}"
    key = _make_callback_key(
        correlation,
        session,
        callback_family="CANCEL_ACTION",
        event_id=event_id,
        native_request_id=native_request_id,
        native_action_ref=action_ref,
        order_ref=order_ref,
        exchange_id=exchange_id,
        order_sys_id=order_sys_id,
        target_front_id=target_front_id,
        target_session_id=target_session_id,
    )
    fields = (
        ("BrokerID", _native_text(action_field, "BrokerID", required=True)),
        ("InvestorID", _native_text(action_field, "InvestorID", required=True)),
        ("UserID", _native_text(action_field, "UserID", required=True)),
        ("InstrumentID", _native_text(action_field, "InstrumentID", required=True)),
        ("RequestID", native_request_id),
        ("OrderActionRef", action_ref),
        ("OrderRef", order_ref),
        ("ExchangeID", exchange_id),
        ("OrderSysID", order_sys_id),
        ("FrontID", target_front_id),
        ("SessionID", target_session_id),
        ("ActionFlag", action_flag),
        ("ActionDate", _native_text(action_field, "ActionDate")),
        ("ActionTime", _native_text(action_field, "ActionTime")),
        ("OrderActionStatus", _native_text(action_field, "OrderActionStatus")),
    )
    return CtpNativeCallbackEnvelope(
        callback_key=key,
        source_callback=source_callback,
        source_scope=session,
        event_identity_basis="RequestID-OrderActionRef-callback-source",
        native_fields=fields,
        response_error_id=error_id,
        response_is_last=response_is_last,
    )


def map_ctp_native_order_insert_callback(
    correlation: CtpDispatchCorrelationKey,
    session: CtpNativeSessionContext,
    source_callback: str,
    order_field: object,
    response_info: object | None = None,
    *,
    request_id: int | None = None,
    is_last: object | None = None,
) -> CtpNativeCallbackEnvelope:
    """Map one exact order-insert response/error to its durable submit key.

    ``OnRspOrderInsert`` must be terminal and its callback RequestID must agree
    with the native input-order field. ``OnErrRtnOrderInsert`` has no separate
    RequestID argument and uses only the field's native RequestID.
    """

    if type(correlation) is not CtpDispatchCorrelationKey:
        raise ContractValidationError("typed CTP dispatch correlation key is required")
    session.require_correlation(correlation)
    if correlation.version != 2:
        raise ContractValidationError("native CTP callbacks require V2 command correlation")
    if correlation.operation != "SUBMIT":
        raise ContractValidationError("native CTP insert response requires a submit command")
    if source_callback not in _ORDER_INSERT_SOURCES:
        raise ContractValidationError("unsupported native CTP order insert callback")

    field_request_id = _native_int(order_field, "RequestID", required=True)
    assert field_request_id is not None
    if source_callback == "OnRspOrderInsert":
        if type(request_id) is not int or request_id <= 0 or request_id != field_request_id:
            raise ContractValidationError("native CTP insert RequestID fields do not match")
        if not _native_bool(is_last, "bIsLast"):
            raise ContractValidationError("native CTP insert response is not terminal")
        native_request_id = request_id
        response_is_last: bool | None = True
    else:
        if request_id is not None or is_last is not None:
            raise ContractValidationError("native CTP insert error has no response arguments")
        native_request_id = field_request_id
        response_is_last = None
    if native_request_id != correlation.native_request_id:
        raise ContractValidationError("native CTP insert RequestID does not match command")

    order_ref = _native_text(order_field, "OrderRef", required=True)
    exchange_id = _native_text(order_field, "ExchangeID", required=True)
    if order_ref != correlation.order_ref:
        raise ContractValidationError("native CTP insert OrderRef does not match command")
    if _account_fingerprint(order_field) != session.account_fingerprint:
        raise ContractValidationError("native CTP insert account does not match session")
    error_id = _error_id(response_info)
    if source_callback == "OnErrRtnOrderInsert" and (error_id is None or error_id == 0):
        raise ContractValidationError("native CTP insert error lacks an error code")
    key = _make_callback_key(
        correlation,
        session,
        callback_family="ORDER",
        event_id=(
            f"{source_callback.lower()}:{native_request_id}:{order_ref}:"
            f"{0 if error_id is None else error_id}"
        ),
        native_request_id=native_request_id,
        native_action_ref=None,
        order_ref=order_ref,
    )
    fields = (
        ("BrokerID", _native_text(order_field, "BrokerID", required=True)),
        ("InvestorID", _native_text(order_field, "InvestorID", required=True)),
        ("UserID", _native_text(order_field, "UserID", required=True)),
        ("InstrumentID", _native_text(order_field, "InstrumentID", required=True)),
        ("ExchangeID", exchange_id),
        ("OrderRef", order_ref),
        ("RequestID", native_request_id),
    )
    return CtpNativeCallbackEnvelope(
        callback_key=key,
        source_callback=source_callback,
        source_scope=session,
        event_identity_basis="RequestID-OrderRef-callback-source",
        native_fields=fields,
        response_error_id=error_id,
        response_is_last=response_is_last,
    )


def map_ctp_native_trade_fact_v2(
    session: CtpNativeSessionContext,
    trade_field: object,
) -> CtpNativeTradeFactV2:
    """Copy exact ``OnRtnTrade`` scalars into a command-free V2 fact.

    The durable router must correlate this fact through a unique prior SUBMIT
    reservation using account, trading day, and OrderRef, then independently
    check the returned exchange/order-system identity and instrument/side.
    This mapper does not claim that such a command exists or that the event is
    authentic.
    """

    if type(session) is not CtpNativeSessionContext:
        raise ContractValidationError("typed native CTP session context is required")

    broker_id = _native_text(trade_field, "BrokerID", required=True)
    investor_id = _native_text(trade_field, "InvestorID", required=True)
    user_id = _native_text(trade_field, "UserID", required=True)
    instrument_id = _native_text(trade_field, "InstrumentID", required=True)
    order_ref = _native_text(trade_field, "OrderRef", required=True)
    exchange_id = _native_text(trade_field, "ExchangeID", required=True)
    trade_id = _native_text(trade_field, "TradeID", required=True)
    order_sys_id = _native_text(trade_field, "OrderSysID", required=True)
    trading_day = _native_text(trade_field, "TradingDay", required=True)
    direction = _native_text(trade_field, "Direction", required=True)
    offset_flag = _native_text(trade_field, "OffsetFlag", required=True)
    hedge_flag = _native_text(trade_field, "HedgeFlag", required=True)
    for field_name, value in (
        ("BrokerID", broker_id),
        ("InvestorID", investor_id),
        ("UserID", user_id),
        ("InstrumentID", instrument_id),
        ("OrderRef", order_ref),
        ("ExchangeID", exchange_id),
        ("TradeID", trade_id),
        ("OrderSysID", order_sys_id),
        ("TradingDay", trading_day),
        ("Direction", direction),
        ("OffsetFlag", offset_flag),
        ("HedgeFlag", hedge_flag),
    ):
        _validate_text(value, "trade " + field_name)
    if trading_day != session.trading_day:
        raise ContractValidationError("native CTP trade TradingDay does not match source")
    if _account_fingerprint(trade_field) != session.account_fingerprint:
        raise ContractValidationError("native CTP trade account does not match source")

    raw_price = _read_field(trade_field, "Price")
    if type(raw_price) not in (int, float, Decimal) or type(raw_price) is bool:
        raise ContractValidationError("native CTP trade price has an invalid type")
    if type(raw_price) is float and not math.isfinite(raw_price):
        raise ContractValidationError("native CTP trade price is not finite")
    try:
        price = Decimal(str(raw_price))
    except (InvalidOperation, ValueError) as exc:
        raise ContractValidationError("native CTP trade price is invalid") from exc
    if not price.is_finite() or price <= 0:
        raise ContractValidationError("native CTP trade price must be positive and finite")
    exponent = price.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ContractValidationError("native CTP trade price is invalid")
    if len(price.as_tuple().digits) > 32 or abs(price.adjusted()) > 32 or abs(exponent) > 32:
        raise ContractValidationError("native CTP trade price exceeds the field bound")
    # Canonical price identity must not round to the caller's Decimal context.
    price_text = format(price, "f")
    if "." in price_text:
        price_text = price_text.rstrip("0").rstrip(".")
    if len(price_text) > 64:
        raise ContractValidationError("native CTP trade price exceeds the field bound")

    volume = _native_int(trade_field, "Volume")
    if volume is None:
        raise ContractValidationError("native CTP trade Volume is required")
    if volume <= 0:
        raise ContractValidationError("native CTP trade volume must be positive")
    sequence_no = _native_int(trade_field, "SequenceNo")
    broker_order_seq = _native_int(trade_field, "BrokerOrderSeq")
    trade_date = _native_text(trade_field, "TradeDate")
    trade_time = _native_text(trade_field, "TradeTime")
    trade_source = _native_text(trade_field, "TradeSource")
    for field_name, value in (
        ("TradeDate", trade_date),
        ("TradeTime", trade_time),
        ("TradeSource", trade_source),
    ):
        if value is not None:
            _validate_text(value, "trade " + field_name)

    identity_digest = sha256(f"{trading_day}:{exchange_id}:{trade_id}".encode("ascii")).hexdigest()
    event_id = "ctp-trade-v2:" + identity_digest
    return CtpNativeTradeFactV2(
        source_scope=session,
        event_id=event_id,
        native_fields=(
            ("BrokerID", broker_id),
            ("InvestorID", investor_id),
            ("UserID", user_id),
            ("InstrumentID", instrument_id),
            ("OrderRef", order_ref),
            ("ExchangeID", exchange_id),
            ("TradeID", trade_id),
            ("OrderSysID", order_sys_id),
            ("TradingDay", trading_day),
            ("Direction", direction),
            ("OffsetFlag", offset_flag),
            ("HedgeFlag", hedge_flag),
            ("Price", price_text),
            ("Volume", volume),
            ("TradeDate", trade_date),
            ("TradeTime", trade_time),
            ("SequenceNo", sequence_no),
            ("BrokerOrderSeq", broker_order_seq),
            ("TradeSource", trade_source),
        ),
    )


