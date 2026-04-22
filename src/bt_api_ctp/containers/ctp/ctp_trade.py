from __future__ import annotations

from typing import Any

from bt_api_base.containers.trades.trade import TradeData
from bt_api_base.functions.utils import (
    from_dict_get_float,
    from_dict_get_int,
    from_dict_get_string,
)


class CtpTradeData(TradeData):
    def __init__(
        self,
        trade_info: dict[str, Any],
        symbol_name: str | None,
        asset_type: str = 'FUTURE',
        has_been_json_encoded: bool = False,
    ) -> None:
        super().__init__(
            trade_info,
            has_been_json_encoded,
            symbol_name=symbol_name,
            asset_type=asset_type,
        )
        self.exchange_name = 'CTP'
        self._initialized = False
        self.instrument_id = None
        self.trade_id_value = None
        self.order_ref = None
        self.order_sys_id = None
        self.direction = None
        self.offset = None
        self.price = None
        self.volume = None
        self.trade_date = None
        self.trade_time = None
        self.trade_time_text = None
        self.exchange_id = None
        self.trade_fee = 0.0
        self.trade_fee_symbol = 'CNY'
        self._all_data = None

    def init_data(self):
        if self._initialized:
            return self
        info = self.trade_info
        if isinstance(info, dict):
            self.instrument_id = from_dict_get_string(info, 'InstrumentID')
            self.trade_id_value = from_dict_get_string(info, 'TradeID')
            self.order_ref = from_dict_get_string(info, 'OrderRef')
            self.order_sys_id = from_dict_get_string(info, 'OrderSysID')
            direction_key = from_dict_get_string(info, 'Direction', '0') or '0'
            self.direction = 'buy' if direction_key == '0' else 'sell'
            self.offset = {
                '0': 'open',
                '1': 'close',
                '3': 'close_today',
                '4': 'close_yesterday',
            }.get(from_dict_get_string(info, 'OffsetFlag', '0') or '0', 'open')
            self.price = from_dict_get_float(info, 'Price', 0.0)
            self.volume = from_dict_get_int(info, 'Volume', 0)
            self.trade_date = from_dict_get_string(info, 'TradeDate')
            self.trade_time = from_dict_get_string(info, 'TradeTime')
            self.trade_time_text = from_dict_get_string(info, 'TradeTime')
            self.exchange_id = from_dict_get_string(info, 'ExchangeID')
        self._initialized = True
        return self

    def get_exchange_name(self) -> str:
        return self.exchange_name or ''

    def get_asset_type(self) -> str:
        return self.asset_type or ''

    def get_symbol_name(self) -> str | None:
        return self.instrument_id or self.symbol_name or ''

    def get_server_time(self) -> str | None:
        return self.trade_time

    def get_trade_id(self) -> str | None:
        return self.trade_id_value

    def get_order_id(self) -> str | None:
        return self.order_sys_id

    def get_client_order_id(self) -> str | None:
        return self.order_ref

    def get_trade_side(self) -> str | None:
        return self.direction or ''

    def get_trade_offset(self) -> str:
        return self.offset or ''

    def get_trade_price(self) -> float | None:
        return self.price

    def get_trade_volume(self) -> int | None:
        return self.volume

    def get_trade_time(self) -> str | None:
        return self.trade_time

    def get_trade_fee(self) -> float:
        return float(self.trade_fee or 0.0)

    def get_trade_fee_symbol(self) -> str:
        return self.trade_fee_symbol or ''

    def get_all_data(self) -> dict[str, Any]:
        if self._all_data is None:
            self._all_data = {
                'exchange_name': self.exchange_name,
                'instrument_id': self.instrument_id,
                'trade_id': self.trade_id_value,
                'order_sys_id': self.order_sys_id,
                'direction': self.direction,
                'offset': self.offset,
                'price': self.price,
                'volume': self.volume,
                'trade_date': self.trade_date,
                'trade_time': self.trade_time,
                'exchange_id': self.exchange_id,
            }
        return self._all_data
