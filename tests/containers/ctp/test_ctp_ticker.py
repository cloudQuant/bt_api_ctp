"""Tests for CTP ticker container."""

from __future__ import annotations

from datetime import datetime, timezone

from bt_api_ctp.containers.ctp.ctp_ticker import CtpTickerData


class TestCtpTickerData:
    """Tests for CtpTickerData."""

    def test_init(self):
        """Test initialization."""
        ticker = CtpTickerData({}, symbol_name="rb2505", asset_type="FUTURE")

        assert ticker.exchange_name == "CTP"
        assert ticker.symbol_name == "rb2505"
        assert ticker.asset_type == "FUTURE"

    def test_init_data(self):
        """Test init_data with ticker info."""
        data = {
            "InstrumentID": "rb2505",
            "LastPrice": 3500.0,
            "PreSettlementPrice": 3450.0,
            "PreClosePrice": 3460.0,
            "OpenPrice": 3480.0,
            "HighestPrice": 3550.0,
            "LowestPrice": 3470.0,
            "BidPrice1": 3499.0,
            "BidVolume1": 10,
            "AskPrice1": 3501.0,
            "AskVolume1": 15,
            "Volume": 10000,
            "Turnover": 35000000.0,
            "OpenInterest": 50000.0,
            "UpperLimitPrice": 3800.0,
            "LowerLimitPrice": 3200.0,
            "UpdateTime": "14:30:00",
            "UpdateMillisec": 500,
            "TradingDay": "20250404",
            "ExchangeID": "SHFE",
        }
        ticker = CtpTickerData(data, symbol_name="rb2505", asset_type="FUTURE")
        ticker.init_data()

        assert ticker.instrument_id == "rb2505"
        assert ticker.last_price_val == 3500.0
        assert ticker.pre_settlement_price == 3450.0
        assert ticker.open_price_val == 3480.0
        assert ticker.highest_price == 3550.0
        assert ticker.lowest_price == 3470.0
        assert ticker.bid_price_1 == 3499.0
        assert ticker.bid_volume_1 == 10
        assert ticker.ask_price_1 == 3501.0
        assert ticker.ask_volume_1 == 15
        assert ticker.volume_val == 10000
        assert ticker.turnover == 35000000.0
        assert ticker.open_interest == 50000.0
        assert ticker.upper_limit_price == 3800.0
        assert ticker.lower_limit_price == 3200.0
        assert ticker.update_time_val == "14:30:00"
        assert ticker.trading_day == "20250404"
        assert ticker.exchange_id == "SHFE"

    def test_init_data_idempotent(self):
        """Test init_data is idempotent."""
        data = {
            "InstrumentID": "rb2505",
            "LastPrice": 3500.0,
        }
        ticker = CtpTickerData(data)
        ticker.init_data()
        first_price = ticker.last_price_val

        ticker.init_data()
        assert ticker.last_price_val == first_price

    def test_get_exchange_name(self):
        """Test get_exchange_name."""
        ticker = CtpTickerData({})
        assert ticker.get_exchange_name() == "CTP"

    def test_get_symbol_name(self):
        """Test get_symbol_name."""
        data = {"InstrumentID": "rb2505"}
        ticker = CtpTickerData(data)
        ticker.init_data()

        assert ticker.get_symbol_name() == "rb2505"

    def test_get_ticker_symbol_name(self):
        """Test get_ticker_symbol_name."""
        data = {"InstrumentID": "rb2505"}
        ticker = CtpTickerData(data)
        ticker.init_data()

        assert ticker.get_ticker_symbol_name() == "rb2505"

    def test_get_bid_price(self):
        """Test get_bid_price."""
        data = {"BidPrice1": 3499.0}
        ticker = CtpTickerData(data)
        ticker.init_data()

        assert ticker.get_bid_price() == 3499.0

    def test_get_ask_price(self):
        """Test get_ask_price."""
        data = {"AskPrice1": 3501.0}
        ticker = CtpTickerData(data)
        ticker.init_data()

        assert ticker.get_ask_price() == 3501.0

    def test_get_bid_volume(self):
        """Test get_bid_volume."""
        data = {"BidVolume1": 10}
        ticker = CtpTickerData(data)
        ticker.init_data()

        assert ticker.get_bid_volume() == 10

    def test_get_ask_volume(self):
        """Test get_ask_volume."""
        data = {"AskVolume1": 15}
        ticker = CtpTickerData(data)
        ticker.init_data()

        assert ticker.get_ask_volume() == 15

    def test_get_last_price(self):
        """Test get_last_price."""
        data = {"LastPrice": 3500.0}
        ticker = CtpTickerData(data)
        ticker.init_data()

        assert ticker.get_last_price() == 3500.0

    def test_get_last_volume(self):
        """Test get_last_volume."""
        data = {"Volume": 10000}
        ticker = CtpTickerData(data)
        ticker.init_data()

        assert ticker.get_last_volume() == 10000

    def test_get_upper_limit_price(self):
        """Test get_upper_limit_price."""
        data = {"UpperLimitPrice": 3800.0}
        ticker = CtpTickerData(data)
        ticker.init_data()

        assert ticker.get_upper_limit_price() == 3800.0

    def test_get_lower_limit_price(self):
        """Test get_lower_limit_price."""
        data = {"LowerLimitPrice": 3200.0}
        ticker = CtpTickerData(data)
        ticker.init_data()

        assert ticker.get_lower_limit_price() == 3200.0

    def test_get_open_interest(self):
        """Test get_open_interest."""
        data = {"OpenInterest": 50000.0}
        ticker = CtpTickerData(data)
        ticker.init_data()

        assert ticker.get_open_interest() == 50000.0

    def test_get_all_data(self):
        """Test get_all_data."""
        data = {
            "InstrumentID": "rb2505",
            "LastPrice": 3500.0,
            "BidPrice1": 3499.0,
            "AskPrice1": 3501.0,
        }
        ticker = CtpTickerData(data)
        ticker.init_data()

        result = ticker.get_all_data()

        assert result["exchange_name"] == "CTP"
        assert result["instrument_id"] == "rb2505"
        assert result["last_price"] == 3500.0
        assert result["bid_price_1"] == 3499.0
        assert result["ask_price_1"] == 3501.0

    def test_quote_v2_public_data_retains_identity_and_defaults_closed(self):
        ticker = CtpTickerData(
            {
                "InstrumentID": "SA701P1080",
                "ExchangeID": "CZCE",
                "LastPrice": 60,
                "BidPrice1": 59,
                "AskPrice1": 61,
                "BidVolume1": 2,
                "AskVolume1": 3,
                "Volume": 107,
                "OpenInterest": 1000,
                "UpperLimitPrice": 100,
                "LowerLimitPrice": 1,
                "TradingDay": "20260909",
                "ActionDay": "20260909",
                "UpdateTime": "09:30:01",
                "UpdateMillisec": 0,
            },
            asset_type="OPTION",
            connection_generation=3,
            ingest_seq=8,
            subscription_epoch=2,
            recv_time_utc=datetime(2026, 9, 9, 1, 30, 2, tzinfo=timezone.utc),
            recv_monotonic_ns=123,
            rules_hash="rules-sha256",
            clock_domain_id="ctp-md-clock-a",
            source="ctp.native.md",
            source_clock_quality="verified",
            receive_clock_quality="verified",
            source_clock_error_ms=1,
            receive_clock_error_ms=1,
            freshness_verified=True,
            product_class="2",
            contract_type="option",
            option_type="put",
            underlying_instrument="SA701",
            strike_price=1080,
        )
        ticker.init_data()
        ticker.resolve_event_time()
        ticker.apply_volume_delta(7, complete=True, quality="CONTINUOUS")

        data = ticker.get_all_data()

        assert data["schema_version"] == "ctp.quote.v2"
        assert data["asset_type"] == "OPTION"
        assert data["product_class"] == "2"
        assert data["contract_type"] == "option"
        assert data["option_type"] == "put"
        assert data["underlying_instrument"] == "SA701"
        assert data["strike_price"] == 1080.0
        assert data["bid_price"] == data["bid_price_1"] == 59.0
        assert data["ask_price"] == data["ask_price_1"] == 61.0
        assert data["bid_volume"] == data["bid_volume_1"] == 2
        assert data["ask_volume"] == data["ask_volume_1"] == 3
        assert data["volume_semantics"] == "delta"
        assert data["volume"] == data["delta_volume"] == 7.0
        assert data["cum_volume"] == data["cumulative_volume"] == 107
        assert data["volume_complete"] is True
        assert data["continuity_status"] == "continuous"
        assert data["lower_limit_price"] == 1.0
        assert data["upper_limit_price"] == 100.0
        assert data["trading_day"] == "20260909"
        assert data["action_day"] == "20260909"
        assert data["event_time_source"] == "action_day"
        assert data["connection_generation"] == 3
        assert data["ingest_seq"] == 8
        assert data["subscription_epoch"] == 2
        assert data["recv_monotonic_ns"] == 123
        assert data["rules_hash"] == "rules-sha256"
        assert data["clock_domain_id"] == "ctp-md-clock-a"
        assert data["source"] == "ctp.native.md"
        assert data["source_clock_quality"] == "verified"
        assert data["receive_clock_quality"] == "verified"
        assert data["source_clock_error_ms"] == 1
        assert data["receive_clock_error_ms"] == 1
        assert data["freshness_verified"] is True
        assert data["event_time_utc"] == datetime(2026, 9, 9, 1, 30, 1, tzinfo=timezone.utc)
        assert data["recv_time_utc"] == datetime(2026, 9, 9, 1, 30, 2, tzinfo=timezone.utc)
        assert data["quality_flags"] == []
        assert data["execution_eligible"] is False
        ticker.execution_eligible = True
        assert ticker.get_all_data()["execution_eligible"] is False

    def test_quote_v2_defaults_do_not_self_promote_execution(self):
        data = CtpTickerData(
            {"InstrumentID": "SA701C1080"},
            connection_generation=True,
            ingest_seq=True,
            subscription_epoch=True,
        ).get_all_data()

        assert data["asset_type"] == "UNKNOWN"
        assert data["contract_type"] == "unknown"
        assert data["option_type"] is None
        assert data["continuity_status"] == "gap"
        assert data["connection_generation"] == 0
        assert data["ingest_seq"] == 0
        assert data["subscription_epoch"] == 0
        assert data["execution_eligible"] is False
