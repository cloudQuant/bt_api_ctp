from __future__ import annotations

from bt_api_base.containers.positions.position import PositionData
from bt_api_base.functions.utils import from_dict_get_float, from_dict_get_int, from_dict_get_string

CTP_POS_DIRECTION_MAP = {"1": "net", "2": "long", "3": "short"}


class CtpPositionData(PositionData):
    def __init__(
        self, position_info, symbol_name=None, asset_type="FUTURE", has_been_json_encoded=False
    ):
        super().__init__(position_info, has_been_json_encoded)
        self.symbol_name = symbol_name
        self.asset_type = asset_type
        self.exchange_name = "CTP"
        self._initialized = False
        self.instrument_id = None
        self.position_direction = None
        self.position_volume = None
        self.today_position = None
        self.yd_position = None
        self.position_cost = None
        self.open_cost = None
        self.close_profit = None
        self.use_margin = None
        self.position_profit = None
        self.settlement_price = None
        self.exchange_id = None

    def init_data(self):
        if self._initialized:
            return self
        info = self.position_info
        if isinstance(info, dict):
            self.instrument_id = from_dict_get_string(info, "InstrumentID")
            pos_direction = from_dict_get_string(info, "PosiDirection", "1") or "1"
            self.position_direction = CTP_POS_DIRECTION_MAP.get(pos_direction, "net")
            self.position_volume = from_dict_get_int(info, "Position", 0)
            self.today_position = from_dict_get_int(info, "TodayPosition", 0)
            self.yd_position = from_dict_get_int(info, "YdPosition", 0)
            self.position_cost = from_dict_get_float(info, "PositionCost", 0.0)
            self.open_cost = from_dict_get_float(info, "OpenCost", 0.0)
            self.close_profit = from_dict_get_float(info, "CloseProfit", 0.0)
            self.use_margin = from_dict_get_float(info, "UseMargin", 0.0)
            self.position_profit = from_dict_get_float(info, "PositionProfit", 0.0)
            self.settlement_price = from_dict_get_float(info, "SettlementPrice", 0.0)
            self.exchange_id = from_dict_get_string(info, "ExchangeID")
        self._initialized = True
        return self

    def get_exchange_name(self):
        return self.exchange_name or ""

    def get_asset_type(self):
        return self.asset_type or ""

    def get_symbol_name(self):
        return self.instrument_id or self.symbol_name or ""

    def get_position_volume(self):
        return self.position_volume or 0

    def get_avg_price(self):
        if self.position_volume and self.position_volume > 0:
            return float(self.position_cost or 0.0) / self.position_volume
        return 0.0

    def get_mark_price(self):
        return self.settlement_price

    def get_liquidation_price(self):
        return None

    def get_initial_margin(self):
        return self.use_margin

    def get_maintain_margin(self):
        return self.use_margin

    def get_position_unrealized_pnl(self):
        return self.position_profit or 0.0

    def get_position_funding_value(self):
        return 0.0

    def get_position_direction(self):
        return self.position_direction

    def get_today_position(self):
        return self.today_position or 0

    def get_yesterday_position(self):
        return self.yd_position or 0

    def get_all_data(self):
        return {
            "exchange_name": self.exchange_name,
            "instrument_id": self.instrument_id,
            "symbol_name": self.symbol_name,
            "asset_type": self.asset_type,
            "position_direction": self.position_direction,
            "position_volume": self.position_volume,
            "today_position": self.today_position,
            "yd_position": self.yd_position,
            "position_cost": self.position_cost,
            "open_cost": self.open_cost,
            "close_profit": self.close_profit,
            "use_margin": self.use_margin,
            "position_profit": self.position_profit,
            "settlement_price": self.settlement_price,
            "exchange_id": self.exchange_id,
        }

    def __str__(self):
        return str(self.get_all_data())

    def __repr__(self):
        return self.__str__()
