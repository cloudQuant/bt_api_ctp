from __future__ import annotations

from typing import Any

from bt_api_base.containers.orders.order import OrderData, OrderStatus
from bt_api_base.functions.utils import (
    from_dict_get_float,
    from_dict_get_string,
)

from ._normalization import ctp_dict_code, ctp_int
from .position_dimensions import ctp_position_dimension_projection

CTP_ORDER_STATUS_MAP = {
    "0": OrderStatus.COMPLETED,
    "1": OrderStatus.PARTIAL,
    "2": OrderStatus.CANCELED,
    "3": OrderStatus.ACCEPTED,
    "4": OrderStatus.CANCELED,
    "5": OrderStatus.CANCELED,
    "a": OrderStatus.UNKNOWN,
    "b": OrderStatus.SUBMITTED,
    "c": OrderStatus.SUBMITTED,
}
CTP_DIRECTION_MAP = {"0": "buy", "1": "sell"}
CTP_OFFSET_MAP = {
    "0": "open",
    "1": "close",
    "2": "force_close",
    "3": "close_today",
    "4": "close_yesterday",
    "5": "force_close_yesterday",
    "6": "local_force_close",
}


class CtpOrderData(OrderData):
    def __init__(
        self,
        order_info: Any,
        symbol_name: str | None = None,
        asset_type: str = "FUTURE",
        has_been_json_encoded: bool = False,
    ) -> None:
        super().__init__(order_info, has_been_json_encoded)
        self.symbol_name = symbol_name
        self.asset_type = asset_type
        self._local_submission_projection = None
        self.exchange_name = "CTP"
        self._initialized = False
        self._data_initialized = False
        self.instrument_id = None
        self.order_ref = None
        self.order_sys_id = None
        self.direction = None
        self.offset = None
        self.limit_price = None
        self.volume_total_original = None
        self.volume_traded = None
        self.volume_total = None
        self.order_status = None
        self.order_status_source = None
        self.insert_time = None
        self.update_time = None
        self.status_msg = None
        self.exchange_id = None
        self.front_id = None
        self.session_id = None
        self.trading_day = None
        self.account_id = None

    def init_data(self):
        if self._data_initialized:
            return self
        info = self.order_info
        self.order_status = OrderStatus.UNKNOWN
        self.order_status_source = "provider_status_missing"
        if self._local_submission_projection is not None and not isinstance(info, dict):
            raise TypeError("local submission projection requires a field mapping")
        if isinstance(info, dict):
            self.instrument_id = from_dict_get_string(info, "InstrumentID")
            self.order_ref = from_dict_get_string(info, "OrderRef")
            self.order_sys_id = from_dict_get_string(info, "OrderSysID")
            direction_key = ctp_dict_code(info, "Direction", "0") or "0"
            self.direction = CTP_DIRECTION_MAP.get(direction_key, "buy")
            offset_char = from_dict_get_string(info, "CombOffsetFlag", "0") or "0"
            self.offset = CTP_OFFSET_MAP.get(offset_char[0] if offset_char else "0", "open")
            self.limit_price = from_dict_get_float(info, "LimitPrice", 0.0)
            self.volume_total_original = ctp_int(info, "VolumeTotalOriginal", 0)
            self.volume_traded = ctp_int(info, "VolumeTraded", 0)
            self.volume_total = ctp_int(info, "VolumeTotal", 0)
            status_key = ctp_dict_code(info, "OrderStatus", "")
            if self._local_submission_projection is not None:
                if status_key:
                    raise ValueError(
                        "local submission projection cannot contain provider OrderStatus"
                    )
                if self._local_submission_projection == "zero":
                    self.order_status = OrderStatus.SUBMITTED
                    self.order_status_source = "local_submit_return_zero"
                else:
                    self.order_status = OrderStatus.UNKNOWN
                    self.order_status_source = "local_submit_return_unrecognized"
            else:
                self.order_status = CTP_ORDER_STATUS_MAP.get(status_key, OrderStatus.UNKNOWN)
                self.order_status_source = "provider" if status_key else "provider_status_missing"
            self.insert_time = from_dict_get_string(info, "InsertTime")
            self.update_time = from_dict_get_string(info, "UpdateTime")
            self.status_msg = from_dict_get_string(info, "StatusMsg")
            self.exchange_id = from_dict_get_string(info, "ExchangeID")
            self.front_id = ctp_int(info, "FrontID")
            self.session_id = ctp_int(info, "SessionID")
            self.trading_day = from_dict_get_string(info, "TradingDay")
            self.account_id = from_dict_get_string(info, "AccountID") or from_dict_get_string(
                info, "InvestorID"
            )
        self._data_initialized = True
        self._initialized = True
        return self

    @classmethod
    def _from_local_submit_return(
        cls,
        order_info: Any,
        symbol_name: str | None = None,
        asset_type: str = "FUTURE",
        has_been_json_encoded: bool = False,
        *,
        return_code: Any,
    ) -> CtpOrderData:
        """Build a local SUBMITTED projection from the exact native zero return.

        This internal projection records only the immediate local call result.
        It is not a provider order callback, exchange acknowledgement or
        execution proof, and must not synthesize ``OrderStatus='a'``.
        """
        if type(return_code) is not int or return_code != 0:
            raise ValueError("local SUBMITTED projection requires an exact int zero return")
        instance = cls(order_info, symbol_name, asset_type, has_been_json_encoded)
        instance._local_submission_projection = "zero"
        return instance

    def get_exchange_name(self) -> str:
        return self.exchange_name or ""

    def get_asset_type(self) -> str:
        return self.asset_type or ""

    def get_symbol_name(self) -> str | None:
        return self.instrument_id or self.symbol_name or ""

    def get_server_time(self) -> float | None:
        return None

    def get_local_update_time(self) -> float | None:
        return None

    def get_order_id(self) -> str | None:
        return self.order_sys_id

    def get_client_order_id(self) -> str | None:
        return self.order_ref

    def get_order_size(self) -> int | None:
        return self.volume_total_original

    def get_order_price(self) -> float | None:
        return self.limit_price

    def get_order_side(self) -> str | None:
        return self.direction or ""

    def get_order_status(self) -> OrderStatus | None:
        return self.order_status if isinstance(self.order_status, OrderStatus) else None

    def get_order_offset(self) -> str | None:
        return self.offset or ""

    def get_order_exchange_id(self) -> str | None:
        return self.exchange_id or ""

    def get_executed_qty(self) -> int | None:
        return self.volume_traded

    def get_order_symbol_name(self) -> str | None:
        return self.instrument_id or self.symbol_name or ""

    def get_order_type(self) -> str:
        return "limit"

    def get_order_avg_price(self) -> float | None:
        return self.limit_price

    def get_order_time_in_force(self) -> str:
        return "GFD"

    def get_trading_day(self) -> str:
        self._ensure_init()
        return self.trading_day or ""

    def get_account_id(self) -> str:
        self._ensure_init()
        return self.account_id or ""

    def get_all_data(self) -> dict[str, Any]:
        self._ensure_init()
        data = super().get_all_data()
        data.update(
            ctp_position_dimension_projection(
                self.order_info,
                direction_key="Direction",
                offset_key="CombOffsetFlag",
                normalized_side=self.direction,
                normalized_offset=self.offset,
                order_offset=True,
            )
        )
        return data
