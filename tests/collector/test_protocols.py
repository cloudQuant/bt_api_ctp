"""离线契约测试：collector 协议与数据结构（不依赖 CTP 原生）。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from bt_api_ctp.collector.protocols import (
    DEFAULT_ASSET_TYPES,
    EXCHANGES,
    InstrumentSpec,
    TickRecord,
)


class TestExchangeConstants:
    def test_covers_six_exchanges_including_ine(self):
        assert EXCHANGES == ("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX")

    def test_default_asset_types_cover_futures_and_both_option_classes(self):
        # spot_option 必含：中金所股指期权（IO/MO/HO）在 CTP 中即此类
        assert DEFAULT_ASSET_TYPES == ("future", "option", "spot_option")


class TestInstrumentSpec:
    def test_future_has_no_option_fields(self):
        spec = InstrumentSpec(instrument_id="rb2510", exchange_id="SHFE", asset_type="future")
        assert spec.instrument_id == "rb2510"
        assert spec.exchange_id == "SHFE"
        assert spec.underlying_instrument is None
        assert spec.strike_price is None
        assert spec.option_type is None

    def test_option_keeps_identity_fields(self):
        spec = InstrumentSpec(
            instrument_id="m2701-C-3000",
            exchange_id="DCE",
            asset_type="option",
            underlying_instrument="m2701",
            strike_price=3000.0,
            option_type="call",
        )
        assert spec.underlying_instrument == "m2701"
        assert spec.strike_price == 3000.0
        assert spec.option_type == "call"

    def test_is_frozen(self):
        spec = InstrumentSpec(instrument_id="rb2510", exchange_id="SHFE", asset_type="future")
        with pytest.raises(FrozenInstanceError):
            spec.instrument_id = "rb2511"  # type: ignore[misc]


class TestTickRecord:
    def _record(self, **overrides):
        base = {
            "exchange_id": "SHFE",
            "instrument_id": "rb2510",
            "trading_day": "20260916",
            "action_day": "20260915",
            "update_time": "21:00:00",
            "update_millisec": 500,
            "local_receive_time": 1_760_000_000_000_000_000,
        }
        base.update(overrides)
        return TickRecord(**base)

    def test_required_fields_and_defaults(self):
        record = self._record()
        assert record.exchange_id == "SHFE"
        assert record.instrument_id == "rb2510"
        assert record.trading_day == "20260916"
        assert record.action_day == "20260915"
        assert record.update_time == "21:00:00"
        assert record.update_millisec == 500
        assert record.local_receive_time == 1_760_000_000_000_000_000
        # 可选行情字段默认为 None，五档默认为空 tuple
        assert record.last_price is None
        assert record.upper_limit is None
        assert record.bid_price == ()
        assert record.ask_volume == ()

    def test_carries_full_ctp_depth_fields(self):
        record = self._record(
            last_price=3500.0,
            pre_settlement=3450.0,
            pre_close=3460.0,
            pre_open_interest=40000.0,
            open_price=3480.0,
            highest_price=3550.0,
            lowest_price=3470.0,
            volume=10000,
            turnover=35_000_000.0,
            open_interest=50000.0,
            close_price=3490.0,
            settlement_price=3495.0,
            upper_limit=3800.0,
            lower_limit=3200.0,
            pre_delta=1.0,
            curr_delta=2.0,
            average_price=3492.0,
            banding_upper_price=3600.0,
            banding_lower_price=3400.0,
            bid_price=(3499.0, 3498.0, 3497.0, 3496.0, 3495.0),
            bid_volume=(10, 11, 12, 13, 14),
            ask_price=(3501.0, 3502.0, 3503.0, 3504.0, 3505.0),
            ask_volume=(15, 16, 17, 18, 19),
        )
        assert record.banding_upper_price == 3600.0
        assert record.bid_price[4] == 3495.0
        assert record.ask_volume[0] == 15
        # 五档可以含 None（CThostFtdcDepthMarketDataField 缺失档位）
        sparse = self._record(bid_price=(3499.0, None, None, None, None))
        assert sparse.bid_price[1] is None

    def test_is_frozen(self):
        record = self._record()
        with pytest.raises(FrozenInstanceError):
            record.last_price = 1.0  # type: ignore[misc]

    def test_equality_and_hash(self):
        assert self._record() == self._record()
        assert len({self._record(), self._record()}) == 1


class TestProtocolDuckTyping:
    """协议只约束结构，不要求继承——假交易所实现必须能直接通过。"""

    def test_fake_provider_subscriber_and_handler_shapes(self):
        from bt_api_ctp.collector.protocols import (
            InstrumentProvider,
        )

        provider = _FakeProvider()
        subscriber = _FakeSubscriber()
        normalizer = _FakeNormalizer()
        handler = _FakeHandler()

        assert isinstance(provider, InstrumentProvider.__class__) or hasattr(
            provider, "fetch_instruments"
        )
        assert hasattr(subscriber, "connect") and hasattr(subscriber, "subscribe")
        assert hasattr(subscriber, "set_handler") and hasattr(subscriber, "run")
        assert hasattr(subscriber, "close")
        assert hasattr(normalizer, "normalize")
        assert hasattr(handler, "on_tick")


class _FakeProvider:
    def fetch_instruments(self):
        return [InstrumentSpec(instrument_id="rb2510", exchange_id="SHFE", asset_type="future")]


class _FakeSubscriber:
    def __init__(self):
        self.handler = None

    def connect(self):
        return None

    def subscribe(self, instruments):
        return None

    def set_handler(self, handler):
        self.handler = handler

    def run(self):
        return None

    def close(self):
        return None


class _FakeNormalizer:
    def normalize(self, raw):
        return None


class _FakeHandler:
    def __init__(self):
        self.ticks = []

    def on_tick(self, tick):
        self.ticks.append(tick)
