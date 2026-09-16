"""离线契约测试：CTP 深度行情归一化（不依赖 CTP 原生回调）。"""

from __future__ import annotations

from types import SimpleNamespace

from bt_api_ctp.collector_ctp.normalizer import CtpTickNormalizer

DBL_MAX = 1.7976931348623157e308

FULL_TICK = {
    "TradingDay": "20260916",
    "ActionDay": "20260915",
    "InstrumentID": "rb2510",
    "ExchangeID": "SHFE",
    "ExchangeInstID": "rb2510",
    "LastPrice": 3500.0,
    "PreSettlementPrice": 3450.0,
    "PreClosePrice": 3460.0,
    "PreOpenInterest": 40000.0,
    "OpenPrice": 3480.0,
    "HighestPrice": 3550.0,
    "LowestPrice": 3470.0,
    "Volume": 10000,
    "Turnover": 35_000_000.0,
    "OpenInterest": 50000.0,
    "ClosePrice": 3490.0,
    "SettlementPrice": 3495.0,
    "UpperLimitPrice": 3800.0,
    "LowerLimitPrice": 3200.0,
    "PreDelta": 1.0,
    "CurrDelta": 2.0,
    "AveragePrice": 3492.0,
    "BandingUpperPrice": 3600.0,
    "BandingLowerPrice": 3400.0,
    "UpdateTime": "21:00:00",
    "UpdateMillisec": 500,
    "BidPrice1": 3499.0,
    "BidPrice2": 3498.0,
    "BidPrice3": 3497.0,
    "BidPrice4": 3496.0,
    "BidPrice5": 3495.0,
    "BidVolume1": 10,
    "BidVolume2": 11,
    "BidVolume3": 12,
    "BidVolume4": 13,
    "BidVolume5": 14,
    "AskPrice1": 3501.0,
    "AskPrice2": 3502.0,
    "AskPrice3": 3503.0,
    "AskPrice4": 3504.0,
    "AskPrice5": 3505.0,
    "AskVolume1": 15,
    "AskVolume2": 16,
    "AskVolume3": 17,
    "AskVolume4": 18,
    "AskVolume5": 19,
}


class TestCtpTickNormalizer:
    def setup_method(self):
        self.normalizer = CtpTickNormalizer()

    def test_maps_every_depth_field(self):
        tick = self.normalizer.normalize(FULL_TICK, local_receive_time=1_760_000_000_000_000_000)
        assert tick is not None
        assert tick.instrument_id == "rb2510"
        assert tick.exchange_id == "SHFE"
        assert tick.exchange_inst_id == "rb2510"
        assert tick.trading_day == "20260916"
        assert tick.action_day == "20260915"
        assert tick.update_time == "21:00:00"
        assert tick.update_millisec == 500
        assert tick.local_receive_time == 1_760_000_000_000_000_000
        assert tick.last_price == 3500.0
        assert tick.pre_settlement == 3450.0
        assert tick.pre_close == 3460.0
        assert tick.pre_open_interest == 40000.0
        assert tick.open_price == 3480.0
        assert tick.highest_price == 3550.0
        assert tick.lowest_price == 3470.0
        assert tick.volume == 10000
        assert tick.turnover == 35_000_000.0
        assert tick.open_interest == 50000.0
        assert tick.close_price == 3490.0
        assert tick.settlement_price == 3495.0
        assert tick.upper_limit == 3800.0
        assert tick.lower_limit == 3200.0
        assert tick.pre_delta == 1.0
        assert tick.curr_delta == 2.0
        assert tick.average_price == 3492.0
        assert tick.banding_upper_price == 3600.0
        assert tick.banding_lower_price == 3400.0

    def test_maps_five_level_book(self):
        tick = self.normalizer.normalize(FULL_TICK)
        assert tick is not None
        assert tick.bid_price == (3499.0, 3498.0, 3497.0, 3496.0, 3495.0)
        assert tick.bid_volume == (10, 11, 12, 13, 14)
        assert tick.ask_price == (3501.0, 3502.0, 3503.0, 3504.0, 3505.0)
        assert tick.ask_volume == (15, 16, 17, 18, 19)

    def test_native_dbl_max_sentinel_becomes_none(self):
        raw = dict(FULL_TICK)
        raw["UpperLimitPrice"] = DBL_MAX
        raw["LowerLimitPrice"] = DBL_MAX
        raw["LastPrice"] = DBL_MAX
        tick = self.normalizer.normalize(raw)
        assert tick is not None
        assert tick.upper_limit is None
        assert tick.lower_limit is None
        assert tick.last_price is None

    def test_missing_book_levels_are_padded_with_none(self):
        raw = dict(FULL_TICK)
        for name in ("BidPrice4", "BidPrice5", "AskVolume4", "AskVolume5"):
            raw.pop(name)
        tick = self.normalizer.normalize(raw)
        assert tick is not None
        assert len(tick.bid_price) == 5
        assert tick.bid_price[3] is None and tick.bid_price[4] is None
        assert len(tick.ask_volume) == 5
        assert tick.ask_volume[3] is None and tick.ask_volume[4] is None

    def test_missing_instrument_id_is_dropped(self):
        raw = dict(FULL_TICK)
        raw.pop("InstrumentID")
        assert self.normalizer.normalize(raw) is None

    def test_native_object_and_dict_agree(self):
        native = SimpleNamespace(**FULL_TICK)
        from_object = self.normalizer.normalize(native, local_receive_time=123)
        from_dict = self.normalizer.normalize(FULL_TICK, local_receive_time=123)
        assert from_object is not None and from_dict is not None
        assert from_object == from_dict

    def test_local_receive_time_defaults_to_wall_clock(self):
        tick = self.normalizer.normalize(FULL_TICK)
        assert tick is not None
        assert tick.local_receive_time > 1_600_000_000_000_000_000

    def test_option_identity_fields_survive(self):
        raw = dict(FULL_TICK)
        raw["InstrumentID"] = "m2701-C-3000"
        raw["ExchangeID"] = "DCE"
        tick = self.normalizer.normalize(raw)
        assert tick is not None
        assert tick.instrument_id == "m2701-C-3000"
        assert tick.exchange_id == "DCE"
