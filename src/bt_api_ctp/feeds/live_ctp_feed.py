from __future__ import annotations

import os
import time
import warnings
from pathlib import Path
from typing import Any

try:
    from dotenv import find_dotenv, load_dotenv
except ImportError:
    find_dotenv = None
    load_dotenv = None

from bt_api_base.containers.requestdatas.request_data import RequestData
from bt_api_base.exceptions import ExchangeConnectionAlias as BtConnectionError
from bt_api_base.feeds.capability import Capability
from bt_api_base.feeds.feed import Feed
from bt_api_base.logging_factory import get_logger

from bt_api_ctp.containers.ctp.ctp_account import CtpAccountData
from bt_api_ctp.containers.ctp.ctp_order import CtpOrderData
from bt_api_ctp.containers.ctp.ctp_position import CtpPositionData
from bt_api_ctp.containers.ctp.ctp_ticker import CtpTickerData
from bt_api_ctp.containers.ctp.ctp_trade import CtpTradeData
from bt_api_ctp.ctp import client as ctp_client
from bt_api_ctp.ctp.ctp_structs_order import (
    CThostFtdcInputOrderActionField,
    CThostFtdcInputOrderField,
)
from bt_api_ctp.ctp_env_selector import apply_ctp_env
from bt_api_ctp.exchange_data import CtpExchangeDataFuture
from bt_api_ctp.feeds.base_stream import BaseDataStream, ConnectionState

CTP_OFFSET_FLAG = {
    'open': '0',
    'close': '1',
    'force_close': '2',
    'close_today': '3',
    'close_yesterday': '4',
    'force_close_yesterday': '5',
    'local_force_close': '6',
}
CTP_DIRECTION_FLAG = {'buy': '0', 'sell': '1'}
_ctp_field_logger = get_logger('ctp_field_converter')


def _load_ctp_env() -> None:
    if load_dotenv is None:
        return
    env_file = Path(__file__).resolve().parents[4] / '.env'
    if env_file.exists():
        load_dotenv(env_file, override=False)
        return
    if find_dotenv is None:
        return
    env_path = find_dotenv(usecwd=True)
    if env_path:
        load_dotenv(env_path, override=False)


def _resolve_ctp_runtime_kwargs(kwargs: dict[str, Any]) -> tuple[dict[str, Any], str]:
    _load_ctp_env()
    resolved = dict(kwargs)
    broker_id = str(
        resolved.get('broker_id') or os.environ.get('CTP_BROKER_ID') or ''
    ).strip()
    user_id = str(
        resolved.get('user_id')
        or resolved.get('investor_id')
        or os.environ.get('CTP_USER_ID')
        or ''
    ).strip()
    password = str(
        resolved.get('password') or os.environ.get('CTP_PASSWORD') or ''
    ).strip()
    auth_code = str(
        resolved.get('auth_code')
        or os.environ.get('CTP_AUTH_CODE')
        or '0000000000000000'
    ).strip()
    app_id = str(
        resolved.get('app_id') or os.environ.get('CTP_APP_ID') or 'simnow_client_test'
    ).strip()
    td_front = str(resolved.get('td_front') or resolved.get('td_address') or '').strip()
    md_front = str(resolved.get('md_front') or resolved.get('md_address') or '').strip()
    env_name = ''
    if not td_front or not md_front:
        static_td = str(os.environ.get('CTP_TD_FRONT') or '').strip()
        static_md = str(os.environ.get('CTP_MD_FRONT') or '').strip()
        if os.environ.get('CTP_ENV') or not static_td or not static_md:
            selected_td, selected_md, env_name = apply_ctp_env()
            td_front = td_front or selected_td
            md_front = md_front or selected_md
        else:
            td_front = td_front or static_td
            md_front = md_front or static_md
    resolved.update(
        {
            'broker_id': broker_id,
            'user_id': user_id,
            'password': password,
            'auth_code': auth_code,
            'app_id': app_id,
            'td_front': td_front,
            'md_front': md_front,
        }
    )
    return resolved, env_name


def _sanitize_ctp_field_value(value):
    if isinstance(value, str):
        return value.encode('gbk', errors='ignore').decode('gbk', errors='ignore')
    return value


def _ctp_field_to_dict(field):
    if field is None:
        return {}
    result = {}
    for attr in dir(field):
        if attr.startswith('_') or attr in {'this', 'thisown'}:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', UnicodeWarning)
                value = getattr(field, attr)
            if not callable(value):
                result[attr] = _sanitize_ctp_field_value(value)
        except Exception as exc:
            _ctp_field_logger.debug(
                f"Failed to access CTP field attribute '{attr}': {exc}", exc_info=False
            )
    return result


class CtpRequestData(Feed):
    @classmethod
    def _capabilities(cls):
        return {
            Capability.GET_TICK,
            Capability.MAKE_ORDER,
            Capability.CANCEL_ORDER,
            Capability.QUERY_ORDER,
            Capability.QUERY_OPEN_ORDERS,
            Capability.GET_DEALS,
            Capability.GET_BALANCE,
            Capability.GET_ACCOUNT,
            Capability.GET_POSITION,
            Capability.MARKET_STREAM,
            Capability.ACCOUNT_STREAM,
        }

    def __init__(self, data_queue: Any = None, **kwargs: Any) -> None:
        super().__init__(data_queue)
        resolved_kwargs, self.ctp_env_name = _resolve_ctp_runtime_kwargs(kwargs)
        self.data_queue = data_queue
        self.broker_id = resolved_kwargs.get('broker_id', '')
        self.user_id = resolved_kwargs.get('user_id', '')
        self.password = resolved_kwargs.get('password', '')
        self.auth_code = resolved_kwargs.get('auth_code', '0000000000000000')
        self.app_id = resolved_kwargs.get('app_id', 'simnow_client_test')
        self.td_front = resolved_kwargs.get('td_front', '')
        self.md_front = resolved_kwargs.get('md_front', '')
        self.asset_type = resolved_kwargs.get('asset_type', 'FUTURE')
        self.exchange_name = 'CTP'
        self._params = CtpExchangeDataFuture()
        self.request_logger = get_logger('ctp_feed')
        self._trader = None
        self._connected = False
        self._connect_timeout = resolved_kwargs.get('connect_timeout', 15)

    def translate_error(self, raw_response):
        if isinstance(raw_response, dict) and raw_response.get('ErrorID', 0) != 0:
            return raw_response
        return None

    def _ensure_connected(self):
        if self._trader is None or not self._trader.is_ready:
            self.connect()
        if not self._trader or not self._trader.is_ready:
            raise BtConnectionError('CTP', 'TraderClient not ready after connect()')

    def connect(self):
        if self._trader is not None and self._trader.is_ready:
            return
        self._trader = ctp_client.TraderClient(
            self.td_front,
            self.broker_id,
            self.user_id,
            self.password,
            app_id=self.app_id,
            auth_code=self.auth_code,
        )
        self._trader.start(block=False)
        self._connected = self._trader.wait_ready(timeout=self._connect_timeout)

    def disconnect(self):
        if self._trader is not None:
            self._trader.stop()
            self._trader = None
        self._connected = False

    def _make_request_data(
        self, data_list, request_type, symbol_name=None, extra_data=None, status=True
    ):
        payload = extra_data.copy() if extra_data else {}
        payload.setdefault('exchange_name', self.exchange_name)
        payload.setdefault('symbol_name', symbol_name or '')
        payload.setdefault('asset_type', self.asset_type)
        payload.setdefault('request_type', request_type)
        request = RequestData(data_list, payload, status=status)
        request.data = data_list
        request.has_been_init_data = True
        return request

    def get_account(self, symbol=None, extra_data=None, **kwargs):
        self._ensure_connected()
        trader = self._trader
        if trader is None:
            return self._make_request_data(
                [], 'get_account', symbol, extra_data, status=False
            )
        raw = trader.query_account(timeout=5)
        if raw is None:
            return self._make_request_data(
                [], 'get_account', symbol, extra_data, status=False
            )
        row = CtpAccountData(_ctp_field_to_dict(raw), symbol, self.asset_type, True)
        return self._make_request_data([row], 'get_account', symbol, extra_data)

    def get_balance(self, symbol=None, extra_data=None, **kwargs):
        return self.get_account(symbol, extra_data, **kwargs)

    def get_position(self, symbol=None, extra_data=None, **kwargs):
        self._ensure_connected()
        trader = self._trader
        if trader is None:
            return self._make_request_data(
                [], 'get_position', symbol, extra_data, status=False
            )
        rows = []
        for raw in trader.query_positions(timeout=5):
            data = _ctp_field_to_dict(raw)
            rows.append(
                CtpPositionData(
                    data, data.get('InstrumentID', symbol), self.asset_type, True
                )
            )
        return self._make_request_data(rows, 'get_position', symbol, extra_data)

    def get_tick(self, symbol, extra_data=None, **kwargs):
        return self._make_request_data([], 'get_tick', symbol, extra_data, status=False)

    def get_depth(self, symbol, count=5, extra_data=None, **kwargs):
        return self._make_request_data(
            [], 'get_depth', symbol, extra_data, status=False
        )

    def get_kline(self, symbol, period, count=100, extra_data=None, **kwargs):
        return self._make_request_data(
            [], 'get_kline', symbol, extra_data, status=False
        )

    def make_order(
        self,
        symbol,
        volume,
        price=None,
        order_type='buy-limit',
        offset='open',
        post_only=False,
        client_order_id=None,
        extra_data=None,
        **kwargs,
    ):
        self._ensure_connected()
        trader = self._trader
        if trader is None:
            return self._make_request_data(
                [], 'make_order', symbol, extra_data, status=False
            )
        side, order_kind = order_type.split('-')
        direction = CTP_DIRECTION_FLAG.get(side.lower(), '0')
        offset_flag = CTP_OFFSET_FLAG.get(offset, '0')
        exchange_id = kwargs.get('exchange_id', '')
        field = CThostFtdcInputOrderField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        field.UserID = self.user_id
        field.InstrumentID = symbol
        if exchange_id:
            field.ExchangeID = exchange_id
        field.Direction = direction
        field.CombOffsetFlag = offset_flag
        field.CombHedgeFlag = '1'
        field.VolumeTotalOriginal = int(volume)
        field.MinVolume = 1
        field.ForceCloseReason = '0'
        field.IsAutoSuspend = 0
        field.UserForceClose = 0
        field.ContingentCondition = '1'
        limit_price = float(price) if price else 0.0
        if limit_price <= 0:
            raise ValueError(
                f'CTP order for {symbol} rejected: price must be positive (got {price!r}).'
            )
        field.OrderPriceType = '2'
        field.TimeCondition = '3'
        field.VolumeCondition = '1'
        field.LimitPrice = limit_price
        if client_order_id is not None:
            field.OrderRef = str(client_order_id)
        elif hasattr(trader, 'next_order_ref'):
            field.OrderRef = trader.next_order_ref()
        else:
            field.OrderRef = str(trader._req_id + 1)
        next_req_id = trader._req_id + 1
        field.RequestID = next_req_id
        trader._req_id = next_req_id
        api = trader.api
        if api is None:
            return self._make_request_data(
                [], 'make_order', symbol, extra_data, status=False
            )
        ret = api.ReqOrderInsert(field, next_req_id)
        order_dict = _ctp_field_to_dict(field)
        order_dict['_ret'] = ret
        order_dict['FrontID'] = getattr(trader, '_front_id', 0)
        order_dict['SessionID'] = getattr(trader, '_session_id', 0)
        if ret != 0:
            return self._make_request_data(
                [], 'make_order', symbol, extra_data, status=False
            )
        return self._make_request_data(
            [CtpOrderData(order_dict, symbol, self.asset_type, True)],
            'make_order',
            symbol,
            extra_data,
        )

    def cancel_order(self, symbol, order_id=None, extra_data=None, **kwargs):
        self._ensure_connected()
        trader = self._trader
        if trader is None:
            return self._make_request_data(
                [], 'cancel_order', symbol, extra_data, status=False
            )
        field = CThostFtdcInputOrderActionField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        field.InstrumentID = symbol
        field.ActionFlag = '0'
        exchange_id = kwargs.get('exchange_id', '')
        if exchange_id:
            field.ExchangeID = exchange_id
        order_ref = kwargs.get('order_ref', '')
        front_id = kwargs.get('front_id', 0)
        session_id = kwargs.get('session_id', 0)
        if order_id:
            field.OrderSysID = str(order_id)
        if order_ref:
            field.OrderRef = str(order_ref)
            field.FrontID = int(front_id) if front_id else trader._front_id
            field.SessionID = int(session_id) if session_id else trader._session_id
        trader._req_id += 1
        api = trader.api
        if api is None:
            return self._make_request_data(
                [], 'cancel_order', symbol, extra_data, status=False
            )
        ret = api.ReqOrderAction(field, trader._req_id)
        return self._make_request_data(
            [_ctp_field_to_dict(field)],
            'cancel_order',
            symbol,
            extra_data,
            status=(ret == 0),
        )

    def query_order(self, symbol=None, order_id=None, extra_data=None, **kwargs):
        return self._make_request_data(
            [], 'query_order', symbol, extra_data, status=False
        )

    def get_open_orders(self, symbol=None, extra_data=None, **kwargs):
        return self._make_request_data(
            [], 'get_open_orders', symbol, extra_data, status=False
        )

    def get_deals(
        self,
        symbol=None,
        count=100,
        start_time=None,
        end_time=None,
        extra_data=None,
        **kwargs,
    ):
        return self._make_request_data(
            [], 'get_deals', symbol, extra_data, status=False
        )

    @property
    def trader_client(self):
        return self._trader


class CtpMarketStream(BaseDataStream):
    def __init__(self, data_queue: Any = None, **kwargs: Any) -> None:
        super().__init__(data_queue, **kwargs)
        resolved_kwargs, self.ctp_env_name = _resolve_ctp_runtime_kwargs(kwargs)
        self.md_front = resolved_kwargs.get('md_front', '')
        self.broker_id = resolved_kwargs.get('broker_id', '')
        self.user_id = resolved_kwargs.get('user_id', '')
        self.password = resolved_kwargs.get('password', '')
        self.topics = resolved_kwargs.get('topics', [])
        self.asset_type = resolved_kwargs.get('asset_type', 'FUTURE')
        self._md_client = None

    def connect(self):
        self.state = ConnectionState.CONNECTING
        self._md_client = ctp_client.MdClient(
            self.md_front, self.broker_id, self.user_id, self.password
        )
        self._md_client.on_tick = self._on_tick
        self._md_client.on_login = self._on_login
        self._md_client.on_error = self._on_error
        instruments = []
        for topic in self.topics:
            if topic.get('topic') in ('tick', 'ticker', 'depth'):
                symbol = topic.get('symbol', '')
                if symbol:
                    instruments.append(symbol)
                instruments.extend(topic.get('symbol_list', []))
        if instruments:
            self._md_client.subscribe(instruments)
        self._md_client.start(block=False)

    def _on_login(self, login_field):
        self.state = ConnectionState.AUTHENTICATED

    def _on_tick(self, tick_field):
        tick_dict = _ctp_field_to_dict(tick_field)
        symbol = tick_dict.get('InstrumentID', '')
        self.push_data(CtpTickerData(tick_dict, symbol, self.asset_type, True))

    def _on_error(self, rsp_info):
        self.state = ConnectionState.ERROR

    def disconnect(self):
        if self._md_client is not None:
            self._md_client.stop()
            self._md_client = None
        self.state = ConnectionState.DISCONNECTED

    def subscribe_topics(self, topics):
        instruments = []
        for topic in topics:
            if topic.get('topic') in ('tick', 'ticker', 'depth'):
                symbol = topic.get('symbol', '')
                if symbol:
                    instruments.append(symbol)
                instruments.extend(topic.get('symbol_list', []))
        if instruments and self._md_client is not None:
            self._md_client.subscribe(instruments)

    def _run_loop(self):
        self.connect()
        while self._running:
            time.sleep(1)


class CtpTradeStream(BaseDataStream):
    def __init__(self, data_queue: Any = None, **kwargs: Any) -> None:
        super().__init__(data_queue, **kwargs)
        resolved_kwargs, self.ctp_env_name = _resolve_ctp_runtime_kwargs(kwargs)
        self.td_front = resolved_kwargs.get('td_front', '')
        self.broker_id = resolved_kwargs.get('broker_id', '')
        self.user_id = resolved_kwargs.get('user_id', '')
        self.password = resolved_kwargs.get('password', '')
        self.auth_code = resolved_kwargs.get('auth_code', '0000000000000000')
        self.app_id = resolved_kwargs.get('app_id', 'simnow_client_test')
        self.asset_type = resolved_kwargs.get('asset_type', 'FUTURE')
        self._trader = None

    def connect(self):
        self.state = ConnectionState.CONNECTING
        self._trader = ctp_client.TraderClient(
            self.td_front,
            self.broker_id,
            self.user_id,
            self.password,
            app_id=self.app_id,
            auth_code=self.auth_code,
        )
        self._trader.on_order = self._on_order
        self._trader.on_trade = self._on_trade
        self._trader.on_login = self._on_login
        self._trader.on_error = self._on_error
        self._trader.start(block=False)

    def _on_login(self, login_field):
        self.state = ConnectionState.AUTHENTICATED

    def _on_order(self, order_field):
        order_dict = _ctp_field_to_dict(order_field)
        symbol = order_dict.get('InstrumentID', '')
        self.push_data(CtpOrderData(order_dict, symbol, self.asset_type, True))

    def _on_trade(self, trade_field):
        trade_dict = _ctp_field_to_dict(trade_field)
        symbol = trade_dict.get('InstrumentID', '')
        self.push_data(CtpTradeData(trade_dict, symbol, self.asset_type, True))

    def _on_error(self, rsp_info):
        self.state = ConnectionState.ERROR

    def disconnect(self):
        if self._trader is not None:
            self._trader.stop()
            self._trader = None
        self.state = ConnectionState.DISCONNECTED

    def subscribe_topics(self, topics):
        return None

    def _run_loop(self):
        self.connect()
        while self._running:
            time.sleep(1)

    @property
    def trader_client(self):
        return self._trader


class CtpRequestDataFuture(CtpRequestData):
    def __init__(self, data_queue: Any = None, **kwargs: Any) -> None:
        super().__init__(data_queue, **kwargs)
        self.asset_type = kwargs.get('asset_type', 'FUTURE')
        self._params = CtpExchangeDataFuture()


__all__ = [
    'CTP_DIRECTION_FLAG',
    'CTP_OFFSET_FLAG',
    'CtpMarketStream',
    'CtpRequestData',
    'CtpRequestDataFuture',
    'CtpTradeStream',
    '_ctp_field_to_dict',
]
