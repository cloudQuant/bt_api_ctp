"""Offline TradeDate propagation through existing source fences and projection."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from bt_api_base.gateway_execution_protocol import GatewayReturnFact
from bt_api_base.market_protocol import MarketInstrumentKey

from bt_api_ctp.gateway_return_projection import (
    CtpGatewayReturnProjectionError,
    project_gateway_return,
)
from bt_api_ctp.gateway_return_query import CtpGatewayReturnQueryError
from bt_api_ctp.gateway_session import CtpGatewaySessionError
from test_iter62_settlement_readback_on_write import TODAY
from test_iter63_gateway_return_query import _row, _setup
from test_iter63_gateway_return_projection import project, trade
from test_iter63_gateway_session import prepared as prepared


def _fact(row, source_ref):
    return project_gateway_return(
        "trade", row, account_scope="synthetic-account", source_ref=source_ref,
        expected_trading_day=TODAY, expected_broker="9999", expected_investor="account",
        instrument=MarketInstrumentKey("ctp", "SHFE", "simnow", "future", "rb2701", "rb2701"),
    ).fact


def _native_fields():
    return _row(OrderRef="1", Direction="0", OffsetFlag="0", TradeTime="21:10:00",
                TradeDate="20261105")


def _assert_roundtrip(fact):
    assert fact.schema_version == 4 and fact.trade_date == "20261105"
    assert fact.trading_day == TODAY and fact.trade_date != fact.trading_day
    assert fact.trade_time == "21:10:00"
    assert GatewayReturnFact.from_mapping(fact.to_mapping()) == fact


def test_callback_sealed_row_retains_natural_date_and_source_fence(prepared):
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="service", timeout=1)
    feeds[0].trader_client._spi.OnRtnTrade(SimpleNamespace(**_native_fields()))
    record = session.return_buffer.offer().records[0]
    session.assert_return_record_current(record)
    assert record.row["TradeDate"] == "20261105"
    _assert_roundtrip(_fact(record.row, record.source_ref))
    with pytest.raises(CtpGatewaySessionError):
        session.assert_return_record_current(replace(record))
    assert session.close() is True


def test_query_sealed_row_retains_natural_date_and_source_fence():
    reader, _, native = _setup()
    native.rows["trades"] = [_native_fields()]
    batch = reader.read(kind="trades", timeout=1)
    reader.assert_current(batch)
    assert batch.rows[0]["TradeDate"] == "20261105"
    _assert_roundtrip(_fact(batch.rows[0], batch.source_ref))
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.assert_current(replace(batch))
    assert native.request_count == 1


@pytest.mark.parametrize("value", ["20240229", b"20240229\x00", " 20240229 "])
def test_valid_date_uses_optional_native_text_semantics(value):
    fact = project("trade", trade(TradeDate=value)).fact
    assert fact.schema_version == 4 and fact.trade_date == "20240229"
    assert GatewayReturnFact.from_mapping(fact.to_mapping()) == fact


@pytest.mark.parametrize("value", [None, "", "  ", b"\x00"])
def test_missing_and_empty_native_date_are_unknown(value):
    assert project("trade", trade(TradeDate=value)).fact.trade_date is None
    assert project("trade", trade()).fact.trade_date is None


@pytest.mark.parametrize("value", ["20230229", "20241301", "00000101", "2024-02-29", "２０２４０２２９", b"2024022\xff", "20240229\x00x", 20240229])
def test_invalid_native_date_rejected_without_value_leak(value):
    with pytest.raises(CtpGatewayReturnProjectionError, match="^INVALID_CTP_RETURN$"):
        project("trade", trade(TradeDate=value))


def test_other_fact_versions_unchanged():
    from test_iter63_gateway_return_projection import order

    fact = project("order", order()).fact
    assert fact.schema_version == 1 and "trade_date" not in fact.to_mapping()
