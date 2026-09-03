"""
/ High-level CTP Client Wrappers

API，。3 。

/ Usage:

   #
   from bt_api_ctp.ctp.client import MdClient

   def on_tick(data):
       print(data.InstrumentID, data.LastPrice)

   client = MdClient("tcp://182.254.243.31:30011", "9999", "user", "pass")
   client.on_tick = on_tick
   client.subscribe(["IF2603", "IC2603"])
   client.start()  #

   #
   from bt_api_ctp.ctp.client import TraderClient

   client = TraderClient("tcp://182.254.243.31:30001", "9999", "user", "pass",
                         app_id="simnow_client_test", auth_code="0000000000000000")
   client.start()
   client.wait_ready(timeout=15)
   print(client.query_account())
"""

from __future__ import annotations

import hashlib
import os
import queue
import tempfile
import threading
import time
from contextlib import suppress

from ._ctp_base import format_ctp_native_diagnostics, is_ctp_native_loaded

_USE_EXTERNAL_CTP = str(os.environ.get('BT_API_PY_USE_EXTERNAL_CTP') or '').strip().lower() in {
    '1',
    'true',
    'yes',
    'on',
}

if _USE_EXTERNAL_CTP:
    from ctp import (
        CThostFtdcMdApi,
        CThostFtdcMdSpi,
        CThostFtdcQryInvestorPositionField,
        CThostFtdcQryTradingAccountField,
        CThostFtdcReqAuthenticateField,
        CThostFtdcReqUserLoginField,
        CThostFtdcSettlementInfoConfirmField,
        CThostFtdcTraderApi,
        CThostFtdcTraderSpi,
    )

    _CTP_RUNTIME_SOURCE = 'external_ctp_python'
else:
    from .ctp_md_api import CThostFtdcMdApi, CThostFtdcMdSpi
    from .ctp_structs_common import (
        CThostFtdcReqAuthenticateField,
        CThostFtdcReqUserLoginField,
        CThostFtdcSettlementInfoConfirmField,
    )
    from .ctp_structs_query import (
        CThostFtdcQryInvestorPositionField,
        CThostFtdcQryTradingAccountField,
    )
    from .ctp_trader_api import CThostFtdcTraderApi, CThostFtdcTraderSpi

    _CTP_RUNTIME_SOURCE = 'vendored_bt_api_py'


def _check_native_module():
    """Raise ImportError early if the CTP C++ extension is not available."""
    if _CTP_RUNTIME_SOURCE == 'external_ctp_python':
        return
    if not is_ctp_native_loaded():
        raise ImportError(
            f'{format_ctp_native_diagnostics()}. '
            'Connections will silently fail. '
            'Install a native wheel matching this OS and Python ABI, or set '
            'BT_API_PY_USE_EXTERNAL_CTP=1 with a compatible ctp package.'
        )


def get_ctp_runtime_source() -> str:
    """get_ctp_runtime_source function"""
    return _CTP_RUNTIME_SOURCE


def _flow_dir(prefix):
    """Create a temp directory for CTP flow files."""
    h = hashlib.md5(prefix.encode('utf-8'), usedforsecurity=False).hexdigest()
    path = os.path.join(tempfile.gettempdir(), 'ctp_client', h) + os.sep
    os.makedirs(path, exist_ok=True)
    return path


def _snapshot_ctp_field(field):
    """Create a plain dict snapshot from a SWIG field.

    Order / trade callbacks arrive on CTP's background thread. Converting the
    field to a plain dict inside the callback avoids leaking thread-bound SWIG
    objects to other threads or test assertions.
    """
    if field is None:
        return {}

    result = {}
    for attr in dir(field):
        if attr.startswith('_') or attr in {'this', 'thisown'}:
            continue
        try:
            value = getattr(field, attr)
        except Exception:
            continue
        if not callable(value):
            result[attr] = value
    return result


def _snapshot_rsp_info(rsp_info):
    """Create a stable error snapshot from a CTP response info field."""
    if rsp_info is None:
        return None
    error_id = getattr(rsp_info, 'ErrorID', 0) or 0
    with suppress(TypeError, ValueError):
        error_id = int(error_id)
    return {
        'error_id': error_id,
        'error_msg': str(getattr(rsp_info, 'ErrorMsg', '') or ''),
    }


# ===========================================================================
#  MdClient -
# ===========================================================================


class _MdSpi(CThostFtdcMdSpi):
    def __init__(self, client):
        """__init__ method"""
        super().__init__()
        self._c = client

    def OnFrontConnected(self):
        """OnFrontConnected method"""
        self._c._connected = True
        field = CThostFtdcReqUserLoginField()
        field.BrokerID = self._c.broker_id
        field.UserID = self._c.user_id
        field.Password = self._c.password
        self._c._api.ReqUserLogin(field, 1)

    def OnFrontDisconnected(self, nReason):
        """OnFrontDisconnected method"""
        self._c._connected = False
        self._c._loggedin = False

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        """OnRspUserLogin method"""
        ok = pRspInfo is None or getattr(pRspInfo, 'ErrorID', 0) == 0
        if ok:
            self._c._loggedin = True
            if self._c._pending_instruments:
                self._c._api.SubscribeMarketData(self._c._pending_instruments)
            if self._c.on_login:
                self._c.on_login(pRspUserLogin)
        else:
            if self._c.on_error:
                self._c.on_error(pRspInfo)

    def OnRtnDepthMarketData(self, pDepthMarketData):
        """OnRtnDepthMarketData method"""
        if self._c.on_tick:
            self._c.on_tick(pDepthMarketData)

    def OnRspSubMarketData(self, pSpecificInstrument, pRspInfo, nRequestID, bIsLast):
        """OnRspSubMarketData method"""
        pass

    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        """OnRspError method"""
        if self._c.on_error:
            self._c.on_error(pRspInfo)


class MdClient:
    """

    Args: front: ， "tcp://182.254.243.31:30011"
        broker_id:
        user_id:
        password:
    """

    def __init__(self, front, broker_id, user_id, password):
        """__init__ method"""
        self.front = front
        self.broker_id = broker_id
        self.user_id = user_id
        self.password = password

        self.on_tick = None  # callback(CThostFtdcDepthMarketDataField)
        self.on_login = None  # callback(CThostFtdcRspUserLoginField)
        self.on_error = None  # callback(CThostFtdcRspInfoField)

        self._connected = False
        self._loggedin = False
        self._pending_instruments = []
        self._api = None
        self._spi = None
        self._thread = None

    def subscribe(self, instruments):
        """（ start ）"""
        self._pending_instruments = list(instruments)
        if self._loggedin and self._api:
            self._api.SubscribeMarketData(self._pending_instruments)

    def start(self, block=True):
        """

        Args: block: True=, False=
        """
        _check_native_module()
        flow = _flow_dir(f'md_{self.broker_id}_{self.user_id}')
        self._api = CThostFtdcMdApi.CreateFtdcMdApi(flow)
        self._spi = _MdSpi(self)
        self._api.RegisterSpi(self._spi)
        self._api.RegisterFront(self.front)
        self._api.Init()

        if block:
            try:
                self._api.Join()
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()
        else:
            self._thread = threading.Thread(target=self._api.Join, daemon=True)
            self._thread.start()

    def wait_ready(self, timeout=15):
        """"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._loggedin:
                return True
            time.sleep(0.2)
        return self._loggedin

    def stop(self):
        """

        macOS  CTP C++ API  Release()  Join()
         segfault。:
        -  (daemon thread): ， daemon
        -  (Join ):  Release()
        """
        self._loggedin = False
        self._connected = False
        api = self._api
        self._api = None
        self._spi = None
        if api is not None and (self._thread is None or not self._thread.is_alive()):
            try:
                api.RegisterSpi(None)
                api.Release()
            except Exception:
                pass
            #  daemon thread ， Release，
            # daemon=True

    @property
    def is_ready(self):
        """is_ready method"""
        return self._connected and self._loggedin


# ===========================================================================
#  TraderClient -
# ===========================================================================


class _TraderSpi(CThostFtdcTraderSpi):
    def __init__(self, client):
        """__init__ method"""
        super().__init__()
        self._c = client

    def OnFrontConnected(self):
        """OnFrontConnected method"""
        self._c._connected = True
        self._c._ready = False
        self._c._auth_state = 'pending'
        self._c._login_state = 'waiting_for_auth'
        self._c._auth_request = {
            'broker_id': self._c.broker_id,
            'user_id': self._c.user_id,
            'app_id': self._c.app_id,
            'has_auth_code': bool(self._c.auth_code),
        }
        field = CThostFtdcReqAuthenticateField()
        field.BrokerID = self._c.broker_id
        field.UserID = self._c.user_id
        field.AppID = self._c.app_id
        field.AuthCode = self._c.auth_code
        self._c._req_id += 1
        if self._c.on_auth_request:
            self._c.on_auth_request(dict(self._c._auth_request))
        self._c._api.ReqAuthenticate(field, self._c._req_id)

    def OnFrontDisconnected(self, nReason):
        """OnFrontDisconnected method"""
        self._c._connected = False
        self._c._ready = False
        if self._c._login_state not in {'blocked', 'failed'}:
            self._c._login_state = 'disconnected'

    def OnRspAuthenticate(self, pRspAuthenticateField, pRspInfo, nRequestID, bIsLast):
        # Some fronts may not populate RspInfo on success; treat missing as OK.
        """OnRspAuthenticate method"""
        self._c._connected = True
        ok = pRspInfo is None or getattr(pRspInfo, 'ErrorID', 0) == 0
        if not ok:
            self._c._auth_state = 'failed'
            self._c._login_state = 'blocked'
            self._c._ready = False
            self._c._last_auth_error = _snapshot_rsp_info(pRspInfo)
            self._c._push_error_event(
                event_type='authenticate_response',
                rsp_info=pRspInfo,
                field=pRspAuthenticateField,
                request_id=nRequestID,
            )
            return

        self._c._auth_state = 'authenticated'
        self._c._last_auth_error = None
        self._c._login_state = 'pending'
        field = CThostFtdcReqUserLoginField()
        field.BrokerID = self._c.broker_id
        field.UserID = self._c.user_id
        field.Password = self._c.password
        self._c._req_id += 1
        self._c._api.ReqUserLogin(field, self._c._req_id)

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        """OnRspUserLogin method"""
        self._c._connected = True
        ok = pRspInfo is None or getattr(pRspInfo, 'ErrorID', 0) == 0
        if ok:
            self._c._login_state = 'logged_in'
            self._c._last_login_error = None
            self._c._front_id = getattr(pRspUserLogin, 'FrontID', 0) or 0
            self._c._session_id = getattr(pRspUserLogin, 'SessionID', 0) or 0
            self._c._login_info = {
                'trading_day': str(getattr(pRspUserLogin, 'TradingDay', '') or ''),
                'login_time': str(getattr(pRspUserLogin, 'LoginTime', '') or ''),
                'system_name': str(getattr(pRspUserLogin, 'SystemName', '') or ''),
                'broker_id': str(getattr(pRspUserLogin, 'BrokerID', self._c.broker_id) or ''),
                'user_id': str(getattr(pRspUserLogin, 'UserID', self._c.user_id) or ''),
            }
            with suppress(TypeError, ValueError):
                self._c._max_order_ref = max(
                    self._c._max_order_ref,
                    int(getattr(pRspUserLogin, 'MaxOrderRef', '') or 0),
                )
            field = CThostFtdcSettlementInfoConfirmField()
            field.BrokerID = self._c.broker_id
            field.InvestorID = self._c.user_id
            self._c._req_id += 1
            try:
                self._c._api.ReqSettlementInfoConfirm(field, self._c._req_id)
            except Exception:
                # If the confirm request fails for any reason, keep the session usable.
                self._c._ready = True
            if self._c.on_login:
                self._c.on_login(pRspUserLogin)
        else:
            self._c._login_state = 'failed'
            self._c._ready = False
            self._c._last_login_error = _snapshot_rsp_info(pRspInfo)
            self._c._push_error_event(
                event_type='login_response',
                rsp_info=pRspInfo,
                field=pRspUserLogin,
                request_id=nRequestID,
            )

    def OnRspSettlementInfoConfirm(self, pSettlementInfoConfirm, pRspInfo, nRequestID, bIsLast):
        """OnRspSettlementInfoConfirm method"""
        ok = pRspInfo is None or getattr(pRspInfo, 'ErrorID', 0) == 0
        if ok:
            self._c._ready = True
        else:
            self._c._ready = False
            self._c._last_login_error = _snapshot_rsp_info(pRspInfo)
            self._c._push_error_event(
                event_type='settlement_confirm_response',
                rsp_info=pRspInfo,
                field=pSettlementInfoConfirm,
                request_id=nRequestID,
            )

    def OnRspQryTradingAccount(self, pTradingAccount, pRspInfo, nRequestID, bIsLast):
        """OnRspQryTradingAccount method"""
        if pTradingAccount:
            self._c._last_account = _snapshot_ctp_field(pTradingAccount)
        if bIsLast:
            self._c._query_done.set()

    def OnRspQryInvestorPosition(self, pPos, pRspInfo, nRequestID, bIsLast):
        """OnRspQryInvestorPosition method"""
        if pPos:
            snapshot = _snapshot_ctp_field(pPos)
            if snapshot and snapshot.get('Position', 0) > 0:
                self._c._last_positions.append(snapshot)
        if bIsLast:
            self._c._query_done.set()

    def OnRtnOrder(self, pOrder):
        """OnRtnOrder method"""
        self._c._push_order_event(pOrder)

    def OnRtnTrade(self, pTrade):
        """OnRtnTrade method"""
        self._c._push_trade_event(pTrade)

    def OnRspOrderInsert(self, pInputOrder, pRspInfo, nRequestID, bIsLast):
        """OnRspOrderInsert method"""
        self._c._push_error_event(
            event_type='order_insert_response',
            rsp_info=pRspInfo,
            field=pInputOrder,
            request_id=nRequestID,
        )

    def OnErrRtnOrderInsert(self, pInputOrder, pRspInfo):
        """OnErrRtnOrderInsert method"""
        self._c._push_error_event(
            event_type='order_insert_error',
            rsp_info=pRspInfo,
            field=pInputOrder,
        )

    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        """OnRspError method"""
        self._c._push_error_event(
            event_type='response_error',
            rsp_info=pRspInfo,
            request_id=nRequestID,
        )


class TraderClient:
    """

    Args: front:
        broker_id:
        user_id:
        password:
        app_id:  AppID
        auth_code:
    """

    def __init__(
        self,
        front,
        broker_id,
        user_id,
        password,
        app_id='simnow_client_test',
        auth_code='0000000000000000',
    ):
        """__init__ method"""
        self.front = front
        self.broker_id = broker_id
        self.user_id = user_id
        self.password = password
        self.app_id = app_id
        self.auth_code = auth_code

        self.on_login = None  # callback(CThostFtdcRspUserLoginField)
        self.on_order = None  # callback(CThostFtdcOrderField)
        self.on_trade = None  # callback(CThostFtdcTradeField)
        self.on_error = None  # callback(CThostFtdcRspInfoField)
        self.on_auth_request = None  # callback(dict)

        self._connected = False
        self._ready = False
        self._auth_state = 'idle'
        self._login_state = 'idle'
        self._auth_request = {}
        self._login_info = {}
        self._last_auth_error = None
        self._last_login_error = None
        self._req_id = 0
        self._front_id = 0
        self._session_id = 0
        self._api = None
        self._spi = None
        self._thread = None
        self._query_done = threading.Event()
        self._last_account = None
        self._last_positions = []
        self._max_order_ref = 0
        self._order_ref_lock = threading.Lock()
        self._order_events = queue.Queue()
        self._trade_events = queue.Queue()
        self._error_events = queue.Queue()

    def start(self, block=False):
        """（）"""
        _check_native_module()
        flow = _flow_dir(f'td_{self.broker_id}_{self.user_id}')
        self._api = CThostFtdcTraderApi.CreateFtdcTraderApi(flow)
        self._spi = _TraderSpi(self)
        self._api.RegisterSpi(self._spi)
        self._api.SubscribePrivateTopic(2)
        self._api.SubscribePublicTopic(2)
        self._api.RegisterFront(self.front)
        self._api.Init()

        if block:
            try:
                self._api.Join()
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()
        else:
            self._thread = threading.Thread(target=self._api.Join, daemon=True)
            self._thread.start()

    def wait_ready(self, timeout=15):
        """→→"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._ready:
                return True
            time.sleep(0.2)
        return self._ready

    def query_account(self, timeout=5):
        """， CThostFtdcTradingAccountField  None"""
        if not self._ready:
            return None
        self._query_done.clear()
        self._last_account = None
        field = CThostFtdcQryTradingAccountField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        self._req_id += 1
        self._api.ReqQryTradingAccount(field, self._req_id)
        self._query_done.wait(timeout)
        return self._last_account

    def query_positions(self, timeout=5):
        """， list[CThostFtdcInvestorPositionField]"""
        if not self._ready:
            return []
        self._query_done.clear()
        self._last_positions = []
        field = CThostFtdcQryInvestorPositionField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        self._req_id += 1
        self._api.ReqQryInvestorPosition(field, self._req_id)
        self._query_done.wait(timeout)
        return self._last_positions

    def next_order_ref(self) -> str:
        """Return the next monotonic CTP OrderRef.

        CTP expects OrderRef to be unique and increasing during a session.
        """
        with self._order_ref_lock:
            self._max_order_ref += 1
            return str(self._max_order_ref)

    def wait_order_event(self, timeout=5):
        """Wait for the next order callback snapshot."""
        try:
            return self._order_events.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_trade_event(self, timeout=5):
        """Wait for the next trade callback snapshot."""
        try:
            return self._trade_events.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_error_event(self, timeout=5):
        """Wait for the next error callback snapshot."""
        try:
            return self._error_events.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def auth_state(self):
        """Return the latest CTP penetration-authentication state."""
        return self._auth_state

    @property
    def login_state(self):
        """Return the latest CTP trader-login state."""
        return self._login_state

    def get_session_state(self):
        """Return structured CTP auth/login metadata for upper layers."""
        return {
            'connected': bool(self._connected),
            'ready': bool(self.is_ready),
            'auth_state': self._auth_state,
            'login_state': self._login_state,
            'front_id': self._front_id,
            'session_id': self._session_id,
            'trading_day': self._login_info.get('trading_day', ''),
            'login_time': self._login_info.get('login_time', ''),
            'system_name': self._login_info.get('system_name', ''),
            'broker_id': self._login_info.get('broker_id', self.broker_id),
            'user_id': self._login_info.get('user_id', self.user_id),
            'auth_request': dict(self._auth_request),
            'last_auth_error': dict(self._last_auth_error or {}),
            'last_login_error': dict(self._last_login_error or {}),
        }

    def _push_order_event(self, order_field) -> None:
        snapshot = _snapshot_ctp_field(order_field)
        if snapshot:
            self._order_events.put(snapshot)
        if self.on_order:
            self.on_order(order_field)

    def _push_trade_event(self, trade_field) -> None:
        snapshot = _snapshot_ctp_field(trade_field)
        if snapshot:
            self._trade_events.put(snapshot)
        if self.on_trade:
            self.on_trade(trade_field)

    def _push_error_event(self, event_type, rsp_info=None, field=None, request_id=None) -> None:
        payload = {
            'event': event_type,
            'request_id': request_id,
            'error_id': getattr(rsp_info, 'ErrorID', 0) if rsp_info is not None else 0,
            'error_msg': getattr(rsp_info, 'ErrorMsg', '') if rsp_info is not None else '',
            'field': _snapshot_ctp_field(field),
        }
        self._error_events.put(payload)
        if self.on_error and rsp_info is not None:
            self.on_error(rsp_info)

    @property
    def api(self):
        """CThostFtdcTraderApi ，"""
        return self._api

    def stop(self):
        """

        macOS  CTP C++ API  Release()  Join()
         segfault。:
        -  (daemon thread): ， daemon
        -  (Join ):  Release()
        """
        self._ready = False
        self._connected = False
        api = self._api
        self._api = None
        self._spi = None
        if api is not None and (self._thread is None or not self._thread.is_alive()):
            try:
                api.RegisterSpi(None)
                api.Release()
            except Exception:
                pass

    @property
    def is_ready(self):
        """is_ready method"""
        return self._connected and self._ready
