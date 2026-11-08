"""CTP native-row projection only; all fields here are synthetic."""

from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import SimpleNamespace

import pytest
from bt_api_base.direct_write_protocol import DirectExecutionEventKind, DirectOrderStatus
from bt_api_base.market_protocol import MarketInstrumentKey

from bt_api_ctp.gateway_return_projection import (
    CtpGatewayReturnProjectionError,
    project_gateway_return,
)

INSTRUMENT = MarketInstrumentKey("ctp", "SHFE", "simnow", "future", "rb2612", "rb2612")
OPTIONS = dict(
    account_scope="synthetic-account",
    source_ref="synthetic-source",
    expected_trading_day="20300101",
    expected_broker="0999",
    expected_investor="0001",
    instrument=INSTRUMENT,
    source_generation=7,
)


def order(**changes):
    value = dict(
        BrokerID="0999",
        InvestorID="0001",
        TradingDay="20300101",
        ExchangeID="SHFE",
        InstrumentID="rb2612",
        OrderRef="000000000001",
        FrontID=3,
        SessionID=9,
        OrderSysID="     sys-1",
        Direction="0",
        CombOffsetFlag="0",
        OrderStatus="1",
        OrderSubmitStatus="3",
        OrderPriceType="2",
        LimitPrice=123.45,
        VolumeTotalOriginal=5,
        VolumeTraded=2,
        VolumeTotal=3,
    )
    value.update(changes)
    return value


def trade(**changes):
    value = dict(
        BrokerID="0999",
        InvestorID="0001",
        TradingDay="20300101",
        ExchangeID="SHFE",
        InstrumentID="rb2612",
        OrderRef="000000000001",
        OrderSysID="     sys-1",
        TradeID="trade-1",
        Direction="0",
        OffsetFlag="0",
        Volume=2,
        Price=123.45,
        TradeTime="09:30:00",
    )
    value.update(changes)
    return value


def project(kind, row, **kwargs):
    return project_gateway_return(kind, row, **(OPTIONS | kwargs))


def test_order_retains_raw_and_separates_strong_identity_classes():
    result = project("order", order())
    assert result.fact.kind is DirectExecutionEventKind.ORDER
    assert result.fact.status is DirectOrderStatus.PARTIALLY_FILLED
    assert (
        result.fact.order_quantity,
        result.fact.filled_quantity,
        result.fact.remaining_quantity,
    ) == ("5", "2", "3")
    assert result.fact.limit_price == "123.45" and result.fact.trade_quantity is None
    assert (
        result.fact.identity_keys == result.fact.binding_keys == ()
        and result.fact.venue_key is None
    )
    assert result.binding_identity.client_order_id == "000000000001"
    assert result.binding_identity.fields["front_id"] == 3
    assert result.venue_identity.fields["order_sys_id"] == "sys-1"
    assert result.venue_identity.fields["order_sys_id_raw"] == "     sys-1"
    assert result.fact.identity.session_generation is None and result.fact.session_generation == 7
    with pytest.raises(FrozenInstanceError):
        result.binding_identity = None


def test_callback_and_query_rows_have_same_facts_and_reception_generation_is_diagnostic():
    callback = project("trade", trade(), source_ref="callback", source_generation=7)
    query = project("trade", SimpleNamespace(**trade()), source_ref="query", source_generation=8)
    assert callback.fact.fact_digest() == query.fact.fact_digest()
    assert callback.fact.trade_content_digest() == query.fact.trade_content_digest()
    assert (
        callback.fact.trade_key == query.fact.trade_key == ("20300101", "SHFE", "rb2612", "trade-1")
    )
    assert callback.fact.trade_quantity == "2" and callback.fact.trade_price == "123.45"
    assert callback.fact.order_quantity is callback.fact.filled_quantity is None


def test_strong_key_classification_does_not_promote_trade_or_weak_ref():
    from bt_api_py.direct_write import DirectWriteError, project_direct_strong_identity_keys

    first = project("order", order())
    other = project("order", order(SessionID=10))
    first_binding = project_direct_strong_identity_keys(first.binding_identity, day_scoped_ids=True)
    assert first_binding != project_direct_strong_identity_keys(
        other.binding_identity, day_scoped_ids=True
    )
    assert project_direct_strong_identity_keys(
        first.venue_identity, day_scoped_ids=True
    ) == project_direct_strong_identity_keys(other.venue_identity, day_scoped_ids=True)
    fill = project("trade", trade(FrontID=3, SessionID=9))
    assert fill.binding_identity is None
    assert fill.fact.identity.fields["front_id"] is fill.fact.identity.fields["session_id"] is None
    assert project_direct_strong_identity_keys(
        fill.fact.identity, day_scoped_ids=True
    ) == project_direct_strong_identity_keys(fill.venue_identity, day_scoped_ids=True)
    weak = project("trade", trade(OrderSysID=""))
    assert weak.venue_identity is weak.binding_identity is None
    with pytest.raises(DirectWriteError):
        project_direct_strong_identity_keys(weak.fact.identity, day_scoped_ids=True)
    weak_order = project("order", order(FrontID=None, SessionID=None, OrderSysID=""))
    with pytest.raises(DirectWriteError):
        project_direct_strong_identity_keys(weak_order.fact.identity, day_scoped_ids=True)


def test_trading_day_is_native_and_part_of_both_strong_key_classes():
    from bt_api_py.direct_write import project_direct_strong_identity_keys

    first = project("order", order())
    second = project("order", order(TradingDay="20300102"), expected_trading_day="20300102")
    for name in ("binding_identity", "venue_identity"):
        assert project_direct_strong_identity_keys(
            getattr(first, name), day_scoped_ids=True
        ) != project_direct_strong_identity_keys(getattr(second, name), day_scoped_ids=True)
    original_trade = project("trade", trade()).fact
    next_day_trade = project(
        "trade", trade(TradingDay="20300102"), expected_trading_day="20300102"
    ).fact
    assert original_trade.trade_key == ("20300101", "SHFE", "rb2612", "trade-1")
    assert next_day_trade.trade_key == ("20300102", "SHFE", "rb2612", "trade-1")
    assert original_trade.trade_key != next_day_trade.trade_key
    assert original_trade.trade_content_digest() != next_day_trade.trade_content_digest()


@pytest.mark.parametrize(
    "field,value",
    [
        ("BrokerID", "wrong"),
        ("InvestorID", "wrong"),
        ("TradingDay", "20300102"),
        ("TradingDay", ""),
        ("InstrumentID", "other"),
        ("ExchangeID", "CFFEX"),
    ],
)
@pytest.mark.parametrize("kind, factory", [("order", order), ("trade", trade)])
def test_mismatched_or_missing_native_identity_is_rejected(kind, factory, field, value):
    with pytest.raises(CtpGatewayReturnProjectionError, match="INVALID_CTP_RETURN"):
        project(kind, factory(**{field: value}))


def test_large_native_integer_and_double_conversion_are_exact_at_received_precision():
    huge = 9007199254740993
    observed = project("trade", trade(Volume=huge, Price=0.1)).fact
    assert observed.trade_quantity == str(huge) and observed.trade_price == "0.1"
    assert project("trade", trade(Price=Decimal("123.4500"))).fact.trade_price == "123.45"
    assert project(
        "order", order(VolumeTotalOriginal=huge, VolumeTraded=0, VolumeTotal=huge)
    ).fact.order_quantity == str(huge)


@pytest.mark.parametrize("value", [None, 0, -1, 1.0, True, "1", 10**36])
def test_bad_trade_quantity_is_rejected(value):
    with pytest.raises(CtpGatewayReturnProjectionError):
        project("trade", trade(Volume=value))


@pytest.mark.parametrize(
    "value",
    [
        None,
        0,
        -1,
        True,
        float("nan"),
        float("inf"),
        1.7976931348623157e308,
        Decimal("0.0000000000000000001"),
    ],
)
def test_bad_trade_price_is_rejected(value):
    with pytest.raises(CtpGatewayReturnProjectionError):
        project("trade", trade(Price=value))


def test_optional_order_quantities_and_market_price_never_invent_values():
    row = order(OrderPriceType="1", LimitPrice=1.7976931348623157e308)
    for key in ("VolumeTotalOriginal", "VolumeTraded", "VolumeTotal"):
        row.pop(key)
    result = project("order", row).fact
    assert result.order_quantity is result.filled_quantity is result.remaining_quantity is None
    assert result.limit_price is None
    row = order()
    row.pop("LimitPrice")
    assert project("order", row).fact.limit_price is None


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("trade", "Direction", None),
        ("trade", "OffsetFlag", "x"),
        ("trade", "TradeID", ""),
        ("order", "OrderStatus", "a"),
        ("order", "OrderStatus", "z"),
        ("order", "OrderSubmitStatus", "z"),
        ("order", "VolumeTraded", 2.5),
        ("order", "VolumeTotalOriginal", True),
        ("order", "FrontID", 1.5),
    ],
)
def test_missing_economics_unknown_status_and_bad_native_types_reject(kind, field, value):
    with pytest.raises(CtpGatewayReturnProjectionError):
        project(kind, (order if kind == "order" else trade)(**{field: value}))


@pytest.mark.parametrize(
    "status,expected",
    [
        ("0", DirectOrderStatus.FILLED),
        ("1", DirectOrderStatus.PARTIALLY_FILLED),
        ("2", DirectOrderStatus.CANCELLED),
        ("3", DirectOrderStatus.ACCEPTED),
        ("4", DirectOrderStatus.CANCELLED),
        ("5", DirectOrderStatus.CANCELLED),
        ("b", DirectOrderStatus.SUBMITTED),
        ("c", DirectOrderStatus.SUBMITTED),
    ],
)
def test_known_order_statuses_and_submit_reject(status, expected):
    assert project("order", order(OrderStatus=status)).fact.status is expected
    assert (
        project("order", order(OrderStatus=status, OrderSubmitStatus="4")).fact.status
        is DirectOrderStatus.REJECTED
    )


def test_native_padded_bytes_keep_system_id_and_never_complete_partial_session():
    row = order(OrderSysID=b"    SYS0001\x00\x00", SessionID=0)
    result = project("order", row)
    assert result.binding_identity is None
    assert result.venue_identity.fields["order_sys_id"] == "SYS0001"
    assert result.venue_identity.fields["order_sys_id_raw"] == "    SYS0001"
    assert result.fact.identity.fields["session_id"] is None


def test_explicit_rejections_do_not_leak_native_fields():
    with pytest.raises(CtpGatewayReturnProjectionError) as error:
        project("trade", trade(BrokerID="secret-synthetic-value"))
    assert str(error.value) == "INVALID_CTP_RETURN"
    with pytest.raises(CtpGatewayReturnProjectionError):
        project("trade", trade(TradeID=b"broken\xff"))


def test_insert_rejected_takes_priority_only_over_known_native_unknown_status():
    assert (
        project("order", order(OrderStatus="a", OrderSubmitStatus="4")).fact.status
        is DirectOrderStatus.REJECTED
    )
    for submit in (None, "0", "3", "5", "6"):
        with pytest.raises(CtpGatewayReturnProjectionError):
            project("order", order(OrderStatus="a", OrderSubmitStatus=submit))
    for submit in ("5", "6"):
        assert (
            project("order", order(OrderStatus="3", OrderSubmitStatus=submit)).fact.status
            is DirectOrderStatus.ACCEPTED
        )
    with pytest.raises(CtpGatewayReturnProjectionError):
        project("order", order(OrderStatus="z", OrderSubmitStatus="4"))
    missing = order(OrderSubmitStatus="4")
    missing.pop("OrderStatus")
    with pytest.raises(CtpGatewayReturnProjectionError):
        project("order", missing)
