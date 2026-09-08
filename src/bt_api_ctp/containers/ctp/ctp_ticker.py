from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from bt_api_base.containers.tickers.ticker import TickerData
from bt_api_base.functions.utils import (
    from_dict_get_float,
    from_dict_get_int,
    from_dict_get_string,
)


class CtpTickerData(TickerData):
    def __init__(
        self,
        ticker_info,
        symbol_name=None,
        asset_type="FUTURE",
        has_been_json_encoded=False,
        connection_generation=0,
        ingest_seq=0,
        recv_time_utc=None,
        recv_monotonic_ns=None,
    ):
        super().__init__(ticker_info, has_been_json_encoded)
        self.symbol_name = symbol_name
        self.asset_type = asset_type
        self.exchange_name = "CTP"
        self._initialized = False
        self._data_initialized = False
        self.instrument_id = None
        self.last_price_val = None
        self.pre_settlement_price = None
        self.open_price_val = None
        self.highest_price = None
        self.lowest_price = None
        self.bid_price_1 = None
        self.bid_volume_1 = None
        self.ask_price_1 = None
        self.ask_volume_1 = None
        self.volume_val = None
        self.turnover = None
        self.open_interest = None
        self.upper_limit_price = None
        self.lower_limit_price = None
        self.update_time_val = None
        self.update_millisec = None
        self.trading_day = None
        self.exchange_id = None
        self.action_day = None
        self.connection_generation = int(connection_generation or 0)
        self.ingest_seq = int(ingest_seq or 0)
        if isinstance(recv_time_utc, (int, float)):
            recv_time_utc = datetime.fromtimestamp(float(recv_time_utc), timezone.utc)
        elif isinstance(recv_time_utc, str):
            recv_time_utc = datetime.fromisoformat(recv_time_utc.replace("Z", "+00:00"))
        self.recv_time_utc = recv_time_utc or datetime.now(timezone.utc)
        if self.recv_time_utc.tzinfo is None:
            self.recv_time_utc = self.recv_time_utc.replace(tzinfo=timezone.utc)
        self.recv_monotonic_ns = int(recv_monotonic_ns or time.monotonic_ns())
        self.event_time_utc = None
        self.event_time_source = "unresolved"
        self.schema_version = "ctp.quote.v2"
        self.cum_volume = None
        self.delta_volume = None
        self.volume_semantics = "cumulative"
        self.volume_complete = False
        self.volume_quality = "uninitialized"
        self.quality_flags = []

    def init_data(self):
        if self._data_initialized:
            return self
        info = self.ticker_info
        if isinstance(info, dict):
            self.instrument_id = from_dict_get_string(info, "InstrumentID")
            self.last_price_val = from_dict_get_float(info, "LastPrice")
            self.pre_settlement_price = from_dict_get_float(info, "PreSettlementPrice")
            self.open_price_val = from_dict_get_float(info, "OpenPrice")
            self.highest_price = from_dict_get_float(info, "HighestPrice")
            self.lowest_price = from_dict_get_float(info, "LowestPrice")
            self.bid_price_1 = from_dict_get_float(info, "BidPrice1")
            self.bid_volume_1 = from_dict_get_int(info, "BidVolume1", 0)
            self.ask_price_1 = from_dict_get_float(info, "AskPrice1")
            self.ask_volume_1 = from_dict_get_int(info, "AskVolume1", 0)
            self.volume_val = from_dict_get_int(info, "Volume", 0)
            self.turnover = from_dict_get_float(info, "Turnover", 0.0)
            self.open_interest = from_dict_get_float(info, "OpenInterest", 0.0)
            self.upper_limit_price = from_dict_get_float(info, "UpperLimitPrice")
            self.lower_limit_price = from_dict_get_float(info, "LowerLimitPrice")
            self.update_time_val = from_dict_get_string(info, "UpdateTime")
            self.update_millisec = from_dict_get_int(info, "UpdateMillisec", 0)
            self.trading_day = from_dict_get_string(info, "TradingDay")
            self.action_day = from_dict_get_string(info, "ActionDay")
            self.exchange_id = from_dict_get_string(info, "ExchangeID")
            self.cum_volume = self.volume_val
        self._data_initialized = True
        self._initialized = True
        return self

    def get_exchange_name(self):
        self._ensure_init()
        return self.exchange_name or ""

    def get_local_update_time(self):
        self._ensure_init()
        return self.recv_time_utc.timestamp()

    def get_symbol_name(self):
        self._ensure_init()
        return self.instrument_id or self.symbol_name or ""

    def get_ticker_symbol_name(self):
        return self.instrument_id or self.symbol_name or ""

    def get_asset_type(self):
        return self.asset_type or ""

    def get_server_time(self):
        self._ensure_init()
        if self.event_time_utc is None:
            return None
        return self.event_time_utc.timestamp()

    def get_bid_price(self):
        self._ensure_init()
        return self.bid_price_1

    def get_ask_price(self):
        self._ensure_init()
        return self.ask_price_1

    def get_bid_volume(self):
        self._ensure_init()
        return self.bid_volume_1

    def get_ask_volume(self):
        self._ensure_init()
        return self.ask_volume_1

    def get_last_price(self):
        self._ensure_init()
        return self.last_price_val

    def get_last_volume(self):
        self._ensure_init()
        if self.volume_semantics == "delta" and self.delta_volume is not None:
            return self.delta_volume
        return self.volume_val

    def get_cumulative_volume(self):
        self._ensure_init()
        return self.cum_volume

    def get_delta_volume(self):
        self._ensure_init()
        return self.delta_volume

    def apply_volume_delta(self, delta, *, complete, quality):
        """Attach the one authoritative cumulative-to-delta conversion."""
        self._ensure_init()
        self.delta_volume = float(delta or 0.0)
        self.volume_semantics = "delta"
        self.volume_complete = bool(complete)
        self.volume_quality = str(quality)
        return self

    def resolve_event_time(self):
        """Resolve ActionDay + UpdateTime to UTC or mark receive-time fallback."""
        self._ensure_init()
        event_time = self.recv_time_utc
        day = str(self.action_day or "")
        update_time = str(self.update_time_val or "")
        if len(day) == 8 and day.isdigit() and update_time:
            try:
                local_time = datetime.strptime(
                    f"{day} {update_time}", "%Y%m%d %H:%M:%S"
                ).replace(
                    microsecond=int(self.update_millisec or 0) * 1000,
                    tzinfo=timezone(timedelta(hours=8)),
                )
            except (TypeError, ValueError):
                self.quality_flags.append("INVALID_EVENT_TIME")
                self.event_time_source = "receive_fallback"
            else:
                event_time = local_time.astimezone(timezone.utc)
                self.event_time_source = "action_day"
        else:
            self.quality_flags.append("AMBIGUOUS_EVENT_DATE")
            self.event_time_source = "receive_fallback"
        self.event_time_utc = event_time
        return event_time

    def get_open_interest(self):
        return self.open_interest

    def get_upper_limit_price(self):
        return self.upper_limit_price

    def get_lower_limit_price(self):
        return self.lower_limit_price

    def get_all_data(self):
        self._ensure_init()
        return {
            "exchange_name": self.exchange_name,
            "instrument_id": self.instrument_id,
            "last_price": self.last_price_val,
            "pre_settlement_price": self.pre_settlement_price,
            "open_price": self.open_price_val,
            "highest_price": self.highest_price,
            "lowest_price": self.lowest_price,
            "bid_price_1": self.bid_price_1,
            "bid_volume_1": self.bid_volume_1,
            "ask_price_1": self.ask_price_1,
            "ask_volume_1": self.ask_volume_1,
            "volume": self.get_last_volume(),
            "cum_volume": self.cum_volume,
            "cumulative_volume": self.cum_volume,
            "delta_volume": self.delta_volume,
            "volume_semantics": self.volume_semantics,
            "volume_complete": self.volume_complete,
            "volume_quality": self.volume_quality,
            "turnover": self.turnover,
            "open_interest": self.open_interest,
            "upper_limit_price": self.upper_limit_price,
            "lower_limit_price": self.lower_limit_price,
            "update_time": self.update_time_val,
            "update_millisec": self.update_millisec,
            "trading_day": self.trading_day,
            "action_day": self.action_day,
            "exchange_id": self.exchange_id,
            "event_time_utc": self.event_time_utc,
            "event_time_source": self.event_time_source,
            "recv_time_utc": self.recv_time_utc,
            "recv_monotonic_ns": self.recv_monotonic_ns,
            "connection_generation": self.connection_generation,
            "ingest_seq": self.ingest_seq,
            "schema_version": self.schema_version,
            "quality_flags": list(self.quality_flags),
        }

    def __str__(self):
        return str(self.get_all_data())

    def __repr__(self):
        return self.__str__()
