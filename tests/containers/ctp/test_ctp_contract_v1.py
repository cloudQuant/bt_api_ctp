"""CTP containers against Base container contract v1 (iteration 60, 60.03/60.05).

Every standard Base getter must answer on a CTP container: a fact CTP does not
supply is unknown (None), never NotImplementedError and never a fabricated
zero. A frozen (published) container must not change when consumers read it.
"""

from __future__ import annotations

import pytest
from bt_api_base.containers.auto_init_mixin import container_fingerprint
from bt_api_base.containers.bars.bar import BarData
from bt_api_base.containers.orders.order import OrderData
from bt_api_base.containers.positions.position import PositionData
from bt_api_base.containers.semantics import VOLUME_CUMULATIVE, VOLUME_DELTA, VOLUME_SEMANTICS
from bt_api_base.containers.tickers.ticker import TickerData
from bt_api_base.containers.trades.trade import TradeData

from bt_api_ctp.containers.ctp.ctp_bar import CtpBarData
from bt_api_ctp.containers.ctp.ctp_order import CtpOrderData
from bt_api_ctp.containers.ctp.ctp_position import CtpPositionData
from bt_api_ctp.containers.ctp.ctp_ticker import CtpTickerData
from bt_api_ctp.containers.ctp.ctp_trade import CtpTradeData

TICK = {
    "InstrumentID": "SA609",
    "ExchangeID": "CZCE",
    "LastPrice": 1500.0,
    "BidPrice1": 1499.0,
    "BidVolume1": 10,
    "AskPrice1": 1501.0,
    "AskVolume1": 12,
    "Volume": 12345,
    "Turnover": 1.0e8,
    "OpenInterest": 5000.0,
    "UpperLimitPrice": 1600.0,
    "LowerLimitPrice": 1400.0,
    "UpdateTime": "10:00:00",
    "UpdateMillisec": 500,
    "TradingDay": "20261102",
    "ActionDay": "20261102",
}
ORDER_CLOSE_LONG = {
    "InstrumentID": "SA609",
    "ExchangeID": "CZCE",
    "OrderRef": "7",
    "OrderSysID": "123",
    "Direction": "1",
    "CombOffsetFlag": "1",
    "LimitPrice": 1500.0,
    "VolumeTotalOriginal": 2,
    "VolumeTraded": 0,
    "VolumeTotal": 2,
    "OrderStatus": "3",
}
TRADE = {
    "InstrumentID": "SA609",
    "ExchangeID": "CZCE",
    "TradeID": "T1",
    "OrderRef": "7",
    "OrderSysID": "123",
    "Direction": "0",
    "OffsetFlag": "0",
    "Price": 1500.0,
    "Volume": 1,
    "TradeDate": "20261102",
    "TradeTime": "10:00:00",
    "TradingDay": "20261102",
}
POSITION = {
    "InstrumentID": "SA609",
    "ExchangeID": "CZCE",
    "InvestorID": "inv-1",
    "PosiDirection": "2",
    "Position": 3,
    "TodayPosition": 1,
    "YdPosition": 2,
    "PositionCost": 4500.0,
    "CloseProfit": 12.5,
    "Commission": 3.0,
    "UseMargin": 900.0,
}
BAR = {
    "open_time": "2026-11-02 10:00:00",
    "close_time": "2026-11-02 10:01:00",
    "open": 1500.0,
    "high": 1502.0,
    "low": 1499.0,
    "close": 1501.0,
    "volume": 30,
    "amount": 45000.0,
}

CASES = [
    (TickerData, lambda: CtpTickerData(TICK, symbol_name="SA609")),
    (BarData, lambda: CtpBarData(BAR, symbol_name="SA609")),
    (OrderData, lambda: CtpOrderData(ORDER_CLOSE_LONG, symbol_name="SA609")),
    (TradeData, lambda: CtpTradeData(TRADE, "SA609")),
    (PositionData, lambda: CtpPositionData(POSITION, symbol_name="SA609")),
]


def _standard_getters(base):
    return sorted(
        name
        for name, value in vars(base).items()
        if name.startswith("get_") and callable(value) and name != "get_all_data"
    )


@pytest.mark.parametrize("base, factory", CASES, ids=[c[0].__name__ for c in CASES])
def test_every_standard_getter_answers(base, factory):
    container = factory()
    for name in _standard_getters(base):
        getattr(container, name)()  # must not raise NotImplementedError


@pytest.mark.parametrize("base, factory", CASES, ids=[c[0].__name__ for c in CASES])
def test_reading_a_frozen_container_does_not_mutate_it(base, factory):
    container = factory().freeze()
    # get_all_data() may memoize its dict on first call; that derived cache is
    # the only state a reader is allowed to create.
    container.get_all_data()
    before = container_fingerprint(container)
    for name in _standard_getters(base):
        getattr(container, name)()
    container.get_all_data()
    assert container_fingerprint(container) == before


@pytest.mark.parametrize("base, factory", CASES, ids=[c[0].__name__ for c in CASES])
def test_get_all_data_result_cannot_corrupt_shared_state(base, factory):
    container = factory().freeze()
    first = container.get_all_data()
    snapshot = dict(first)
    first["injected_by_consumer"] = True
    for key in list(snapshot):
        first[key] = "corrupted"
    assert container.get_all_data() == snapshot


def test_ticker_volume_semantics():
    ticker = CtpTickerData(TICK, symbol_name="SA609")
    assert ticker.get_volume_semantics() == VOLUME_CUMULATIVE
    assert ticker.get_last_volume() == ticker.get_cumulative_volume() == 12345
    assert ticker.get_delta_volume() is None
    assert ticker.get_volume_complete() is False
    assert ticker.get_execution_eligible() is False
    ticker.apply_volume_delta(3, complete=True, quality="CONTINUOUS")
    assert ticker.get_volume_semantics() == VOLUME_DELTA
    assert ticker.get_last_volume() == ticker.get_delta_volume() == 3
    assert ticker.get_volume_complete() is True
    assert ticker.get_continuity_status() == "continuous"
    assert ticker.get_volume_semantics() in VOLUME_SEMANTICS


def test_ticker_event_time_source_and_flags():
    ticker = CtpTickerData(TICK, symbol_name="SA609")
    assert ticker.get_event_time_source() == "unresolved"
    ticker.resolve_event_time()
    assert ticker.get_event_time_source() == "action_day"
    assert ticker.get_quality_flags() == ()
    assert isinstance(ticker.get_quality_flags(), tuple)


@pytest.mark.parametrize(
    "direction, expected",
    [("2", "long"), ("3", "short"), ("1", "net")],
)
def test_position_side_is_normalized(direction, expected):
    position = CtpPositionData(dict(POSITION, PosiDirection=direction))
    assert position.get_position_side() == expected
    assert position.get_position_direction() == expected


def test_position_standard_facts():
    position = CtpPositionData(POSITION, symbol_name="SA609")
    assert position.get_position_symbol_name() == "SA609"
    assert position.get_account_id() == "inv-1"
    assert position.get_position_fee() == 3.0
    assert position.get_position_realized_pnl() == 12.5
    assert position.get_leverage() is None
    assert position.get_server_time() is None


@pytest.mark.parametrize(
    "direction, offset, expected",
    [
        ("1", "1", "LONG"),  # sell to close -> closes the long leg
        ("0", "1", "SHORT"),  # buy to close -> closes the short leg
        ("0", "3", "SHORT"),  # buy close-today
        ("1", "4", "LONG"),  # sell close-yesterday
        ("0", "0", None),  # opening order identifies no closed side
        ("1", "2", None),  # forced close stays unresolved
    ],
)
def test_order_position_side_follows_strict_projection(direction, offset, expected):
    order = CtpOrderData(dict(ORDER_CLOSE_LONG, Direction=direction, CombOffsetFlag=offset))
    assert order.get_position_side() == expected
    # Same native value the container already reported through get_all_data().
    if expected is not None:
        assert order.get_all_data()["position_side"] == expected


def test_order_unsupported_facts_are_unknown():
    order = CtpOrderData(ORDER_CLOSE_LONG)
    assert order.get_reduce_only() is None
    assert order.get_stop_loss_price() is None
    assert order.get_trade_id() is None


def test_trade_and_bar_unknown_facts():
    trade = CtpTradeData(TRADE, "SA609")
    assert trade.get_trade_symbol_name() == "SA609"
    assert trade.get_trade_type() is None
    assert trade.get_trade_accumulate_volume() is None
    bar = CtpBarData(BAR)
    assert bar.get_base_asset_volume() == 30
    assert bar.get_quote_asset_volume() == 45000.0
    assert bar.get_num_trades() is None
