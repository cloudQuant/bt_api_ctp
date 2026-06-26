"""
高层封装 / High-level CTP Client Wrappers

提供简洁的 API，减少样板代码。3 行即可收行情或完成交易登录。

用法 / Usage:

    # 行情客户端
    from bt_api_py.ctp.client import MdClient

    def on_tick(data):
        print(data.InstrumentID, data.LastPrice)

    client = MdClient("tcp://182.254.243.31:30011", "9999", "user", "pass")
    client.on_tick = on_tick
    client.subscribe(["IF2603", "IC2603"])
    client.start()  # 阻塞

    # 交易客户端
    from bt_api_py.ctp.client import TraderClient

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
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress

from ._ctp_base import format_ctp_native_diagnostics, is_ctp_native_loaded

_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_text(name: str) -> str:
    return str(os.environ.get(name) or "").strip().lower()


def _select_ctp_runtime_source() -> str:
    requested = _env_text("BT_API_PY_CTP_RUNTIME")
    if requested in {"vendored", "bundled", "bt_api_ctp", "bt_api_py", ""}:
        return "vendored_bt_api_py"
    if requested in {"ctp", "external_ctp", "external_ctp_python"}:
        return "external_ctp_python"
    if requested in {"openctp", "openctp_ctp", "external_openctp_ctp"}:
        return "external_openctp_ctp"
    if _env_text("BT_API_PY_USE_OPENCTP_CTP") in _TRUE_ENV_VALUES:
        return "external_openctp_ctp"
    if _env_text("BT_API_PY_USE_EXTERNAL_CTP") in _TRUE_ENV_VALUES:
        return "external_ctp_python"
    return "vendored_bt_api_py"


def _probe_openctp_import() -> None:
    code = "from openctp_ctp import mdapi, tdapi; print('openctp_ctp import ok')"
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImportError("external CTP runtime openctp_ctp import preflight timed out") from exc

    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-2000:]
        stdout_tail = (result.stdout or "")[-500:]
        raise ImportError(
            "external CTP runtime openctp_ctp failed import preflight "
            f"(returncode={result.returncode}). stderr={stderr_tail!r} stdout={stdout_tail!r}"
        )


_CTP_RUNTIME_SOURCE = _select_ctp_runtime_source()

if _CTP_RUNTIME_SOURCE == "external_ctp_python":
    from ctp import (
        CThostFtdcMdApi,
        CThostFtdcMdSpi,
        CThostFtdcQryInstrumentCommissionRateField,
        CThostFtdcQryInstrumentField,
        CThostFtdcQryInstrumentMarginRateField,
        CThostFtdcQryInvestorPositionField,
        CThostFtdcQryOrderField,
        CThostFtdcQryTradingAccountField,
        CThostFtdcReqAuthenticateField,
        CThostFtdcReqUserLoginField,
        CThostFtdcSettlementInfoConfirmField,
        CThostFtdcTraderApi,
        CThostFtdcTraderSpi,
    )
elif _CTP_RUNTIME_SOURCE == "external_openctp_ctp":
    _probe_openctp_import()
    from openctp_ctp import mdapi as _openctp_mdapi
    from openctp_ctp import tdapi as _openctp_tdapi

    CThostFtdcMdApi = _openctp_mdapi.CThostFtdcMdApi
    CThostFtdcMdSpi = _openctp_mdapi.CThostFtdcMdSpi
    CThostFtdcQryInstrumentCommissionRateField = (
        _openctp_tdapi.CThostFtdcQryInstrumentCommissionRateField
    )
    CThostFtdcQryInstrumentField = _openctp_tdapi.CThostFtdcQryInstrumentField
    CThostFtdcQryInstrumentMarginRateField = _openctp_tdapi.CThostFtdcQryInstrumentMarginRateField
    CThostFtdcQryInvestorPositionField = _openctp_tdapi.CThostFtdcQryInvestorPositionField
    CThostFtdcQryOrderField = _openctp_tdapi.CThostFtdcQryOrderField
    CThostFtdcQryTradingAccountField = _openctp_tdapi.CThostFtdcQryTradingAccountField
    CThostFtdcReqAuthenticateField = _openctp_tdapi.CThostFtdcReqAuthenticateField
    CThostFtdcReqUserLoginField = _openctp_tdapi.CThostFtdcReqUserLoginField
    CThostFtdcSettlementInfoConfirmField = _openctp_tdapi.CThostFtdcSettlementInfoConfirmField
    CThostFtdcTraderApi = _openctp_tdapi.CThostFtdcTraderApi
    CThostFtdcTraderSpi = _openctp_tdapi.CThostFtdcTraderSpi
else:
    from .ctp_md_api import CThostFtdcMdApi, CThostFtdcMdSpi
    from .ctp_structs_common import (
        CThostFtdcReqAuthenticateField,
        CThostFtdcReqUserLoginField,
        CThostFtdcSettlementInfoConfirmField,
    )
    from .ctp_structs_query import (
        CThostFtdcQryInstrumentCommissionRateField,
        CThostFtdcQryInstrumentField,
        CThostFtdcQryInstrumentMarginRateField,
        CThostFtdcQryInvestorPositionField,
        CThostFtdcQryOrderField,
        CThostFtdcQryTradingAccountField,
    )
    from .ctp_trader_api import CThostFtdcTraderApi, CThostFtdcTraderSpi


def _check_native_module():
    """Raise ImportError early if the CTP C++ extension is not available."""
    if _CTP_RUNTIME_SOURCE.startswith("external_"):
        return
    if not is_ctp_native_loaded():
        raise ImportError(
            f"{format_ctp_native_diagnostics()}. "
            "Connections will silently fail. "
            "Install a native _ctp extension matching this OS, Python ABI, and CTP runtime, "
            "set BT_API_PY_USE_EXTERNAL_CTP=1 with a compatible ctp package, "
            "or set BT_API_PY_CTP_RUNTIME=openctp_ctp with a compatible openctp-ctp package."
        )


def get_ctp_runtime_source() -> str:
    return _CTP_RUNTIME_SOURCE


def _flow_dir(prefix):
    """Create a temp directory for CTP flow files."""
    h = hashlib.md5(prefix.encode("utf-8"), usedforsecurity=False).hexdigest()
    path = os.path.join(tempfile.gettempdir(), "ctp_client", h) + os.sep
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
        if attr.startswith("_") or attr in {"this", "thisown"}:
            continue
        try:
            value = getattr(field, attr)
        except Exception:
            continue
        if not callable(value):
            result[attr] = value
    return result


# ===========================================================================
#  MdClient - 行情客户端
# ===========================================================================


class _MdSpi(CThostFtdcMdSpi):
    def __init__(self, client):
        super().__init__()
        self._c = client

    def OnFrontConnected(self):
        self._c._connected = True
        field = CThostFtdcReqUserLoginField()
        field.BrokerID = self._c.broker_id
        field.UserID = self._c.user_id
        field.Password = self._c.password
        self._c._api.ReqUserLogin(field, 1)

    def OnFrontDisconnected(self, nReason):
        self._c._connected = False
        self._c._loggedin = False

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        if pRspInfo and pRspInfo.ErrorID == 0:
            self._c._loggedin = True
            if self._c._pending_instruments:
                self._c._api.SubscribeMarketData(self._c._pending_instruments)
            if self._c.on_login:
                self._c.on_login(pRspUserLogin)
        else:
            if self._c.on_error:
                self._c.on_error(pRspInfo)

    def OnRtnDepthMarketData(self, pDepthMarketData):
        if self._c.on_tick:
            self._c.on_tick(pDepthMarketData)

    def OnRspSubMarketData(self, pSpecificInstrument, pRspInfo, nRequestID, bIsLast):
        pass

    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        if self._c.on_error:
            self._c.on_error(pRspInfo)


class MdClient:
    """行情客户端封装

    Args:
        front: 前置地址，如 "tcp://182.254.243.31:30011"
        broker_id: 经纪商代码
        user_id: 投资者代码
        password: 密码
    """

    def __init__(self, front, broker_id, user_id, password):
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
        """订阅合约列表（可在 start 前或后调用）"""
        self._pending_instruments = list(instruments)
        if self._loggedin and self._api:
            self._api.SubscribeMarketData(self._pending_instruments)

    def start(self, block=True):
        """启动连接

        Args:
            block: True=阻塞直到断开, False=后台线程运行
        """
        _check_native_module()
        flow = _flow_dir(f"md_{self.broker_id}_{self.user_id}")
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
        """等待登录就绪"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._loggedin:
                return True
            time.sleep(0.2)
        return self._loggedin

    def stop(self):
        """停止并释放资源

        macOS 上 CTP C++ API 的 Release() 在 Join() 仍然运行于
        另一个线程时会触发 segfault。因此:
        - 非阻塞模式 (daemon thread): 仅置空引用，让 daemon 线程随进程退出
        - 阻塞模式 (Join 已返回): 安全调用 Release()
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
            # 如果 daemon thread 还活着，不调用 Release，
            # daemon=True 线程会在进程退出时自动终止

    @property
    def is_ready(self):
        return self._connected and self._loggedin


# ===========================================================================
#  TraderClient - 交易客户端
# ===========================================================================


class _TraderSpi(CThostFtdcTraderSpi):
    def __init__(self, client):
        super().__init__()
        self._c = client

    def OnFrontConnected(self):
        self._c._connected = True
        field = CThostFtdcReqAuthenticateField()
        field.BrokerID = self._c.broker_id
        field.UserID = self._c.user_id
        field.AppID = self._c.app_id
        field.AuthCode = self._c.auth_code
        self._c._req_id += 1
        self._c._api.ReqAuthenticate(field, self._c._req_id)

    def OnFrontDisconnected(self, nReason):
        self._c._connected = False
        self._c._ready = False

    def OnRspAuthenticate(self, pRspAuthenticateField, pRspInfo, nRequestID, bIsLast):
        # Some fronts may not populate RspInfo on success; treat missing as OK.
        ok = pRspInfo is None or getattr(pRspInfo, "ErrorID", 0) == 0
        if not ok and self._c.on_error:
            self._c.on_error(pRspInfo)

        # Fall back to login even if authentication fails (some broker setups skip auth).
        field = CThostFtdcReqUserLoginField()
        field.BrokerID = self._c.broker_id
        field.UserID = self._c.user_id
        field.Password = self._c.password
        self._c._req_id += 1
        self._c._api.ReqUserLogin(field, self._c._req_id)

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        ok = pRspInfo is None or getattr(pRspInfo, "ErrorID", 0) == 0
        if ok:
            self._c._front_id = pRspUserLogin.FrontID
            self._c._session_id = pRspUserLogin.SessionID
            with suppress(TypeError, ValueError):
                self._c._max_order_ref = max(
                    self._c._max_order_ref,
                    int(getattr(pRspUserLogin, "MaxOrderRef", "") or 0),
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
        elif self._c.on_error:
            self._c.on_error(pRspInfo)

    def OnRspSettlementInfoConfirm(self, pSettlementInfoConfirm, pRspInfo, nRequestID, bIsLast):
        ok = pRspInfo is None or getattr(pRspInfo, "ErrorID", 0) == 0
        if ok:
            self._c._ready = True

    def OnRspQryTradingAccount(self, pTradingAccount, pRspInfo, nRequestID, bIsLast):
        if pTradingAccount:
            self._c._last_account = pTradingAccount
        if bIsLast:
            self._c._query_done.set()

    def OnRspQryInvestorPosition(self, pPos, pRspInfo, nRequestID, bIsLast):
        if pPos and pPos.Position > 0:
            self._c._last_positions.append(pPos)
        if bIsLast:
            self._c._query_done.set()

    def OnRspQryOrder(self, pOrder, pRspInfo, nRequestID, bIsLast):
        if pOrder:
            self._c._last_orders.append(pOrder)
        if bIsLast:
            self._c._query_done.set()

    def OnRspQryInstrument(self, pInstrument, pRspInfo, nRequestID, bIsLast):
        if pInstrument:
            self._c._last_instrument = pInstrument
        if bIsLast:
            self._c._query_done.set()

    def OnRspQryInstrumentMarginRate(self, pInstrumentMarginRate, pRspInfo, nRequestID, bIsLast):
        if pInstrumentMarginRate:
            self._c._last_margin_rate = pInstrumentMarginRate
        if bIsLast:
            self._c._query_done.set()

    def OnRspQryInstrumentCommissionRate(
        self, pInstrumentCommissionRate, pRspInfo, nRequestID, bIsLast
    ):
        if pInstrumentCommissionRate:
            self._c._last_commission_rate = pInstrumentCommissionRate
        if bIsLast:
            self._c._query_done.set()

    def OnRtnOrder(self, pOrder):
        self._c._push_order_event(pOrder)

    def OnRtnTrade(self, pTrade):
        self._c._push_trade_event(pTrade)

    def OnRspOrderInsert(self, pInputOrder, pRspInfo, nRequestID, bIsLast):
        self._c._push_error_event(
            event_type="order_insert_response",
            rsp_info=pRspInfo,
            field=pInputOrder,
            request_id=nRequestID,
        )

    def OnErrRtnOrderInsert(self, pInputOrder, pRspInfo):
        self._c._push_error_event(
            event_type="order_insert_error",
            rsp_info=pRspInfo,
            field=pInputOrder,
        )

    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        self._c._push_error_event(
            event_type="response_error",
            rsp_info=pRspInfo,
            request_id=nRequestID,
        )


class TraderClient:
    """交易客户端封装

    Args:
        front: 交易前置地址
        broker_id: 经纪商代码
        user_id: 投资者代码
        password: 密码
        app_id: 客户端 AppID
        auth_code: 认证码
    """

    def __init__(
        self,
        front,
        broker_id,
        user_id,
        password,
        app_id="simnow_client_test",
        auth_code="0000000000000000",
    ):
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

        self._connected = False
        self._ready = False
        self._req_id = 0
        self._front_id = 0
        self._session_id = 0
        self._api = None
        self._spi = None
        self._thread = None
        self._query_done = threading.Event()
        self._last_account = None
        self._last_positions = []
        self._last_orders = []
        self._last_instrument = None
        self._last_margin_rate = None
        self._last_commission_rate = None
        self._query_lock = threading.Lock()
        self._last_query_submitted_at = 0.0
        try:
            self._query_interval = max(
                0.0, float(os.environ.get("BT_API_PY_CTP_QUERY_INTERVAL_SEC") or 1.05)
            )
        except ValueError:
            self._query_interval = 1.05
        self._max_order_ref = 0
        self._order_ref_lock = threading.Lock()
        self._order_events = queue.Queue()
        self._trade_events = queue.Queue()
        self._error_events = queue.Queue()

    def start(self, block=False):
        """启动连接（默认后台运行）"""
        _check_native_module()
        flow = _flow_dir(f"td_{self.broker_id}_{self.user_id}")
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
        """等待完成认证→登录→结算确认"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._ready:
                return True
            time.sleep(0.2)
        return self._ready

    def query_account(self, timeout=5):
        """查询资金账户，返回 CThostFtdcTradingAccountField 或 None"""
        if not self._ready:
            return None
        with self._query_lock:
            self._query_done.clear()
            self._last_account = None
            field = CThostFtdcQryTradingAccountField()
            field.BrokerID = self.broker_id
            field.InvestorID = self.user_id
            ok = self._submit_query(lambda req_id: self._api.ReqQryTradingAccount(field, req_id))
            if not ok:
                return None
            self._query_done.wait(timeout)
            return self._last_account

    def query_positions(self, timeout=5):
        """查询持仓，返回 list[CThostFtdcInvestorPositionField]"""
        if not self._ready:
            return []
        with self._query_lock:
            self._query_done.clear()
            self._last_positions = []
            field = CThostFtdcQryInvestorPositionField()
            field.BrokerID = self.broker_id
            field.InvestorID = self.user_id
            ok = self._submit_query(lambda req_id: self._api.ReqQryInvestorPosition(field, req_id))
            if not ok:
                return []
            self._query_done.wait(timeout)
            return self._last_positions

    def query_orders(self, instrument_id="", exchange_id="", order_sys_id="", timeout=5):
        """查询委托，返回 list[CThostFtdcOrderField]。"""
        if not self._ready:
            return []
        with self._query_lock:
            self._query_done.clear()
            self._last_orders = []
            field = CThostFtdcQryOrderField()
            field.BrokerID = self.broker_id
            field.InvestorID = self.user_id
            if instrument_id:
                field.InstrumentID = str(instrument_id)
            if exchange_id:
                field.ExchangeID = str(exchange_id)
            if order_sys_id:
                field.OrderSysID = str(order_sys_id)
            ok = self._submit_query(lambda req_id: self._api.ReqQryOrder(field, req_id))
            if not ok:
                return []
            self._query_done.wait(timeout)
            return self._last_orders

    def query_instrument(self, instrument_id, exchange_id="", timeout=5):
        """查询合约规格，返回 CThostFtdcInstrumentField 或 None"""
        if not self._ready:
            return None
        with self._query_lock:
            self._query_done.clear()
            self._last_instrument = None
            field = CThostFtdcQryInstrumentField()
            field.InstrumentID = str(instrument_id or "")
            if exchange_id:
                field.ExchangeID = str(exchange_id)
            ok = self._submit_query(lambda req_id: self._api.ReqQryInstrument(field, req_id))
            if not ok:
                return None
            self._query_done.wait(timeout)
            return self._last_instrument

    def query_instrument_margin_rate(
        self, instrument_id, exchange_id="", hedge_flag="1", timeout=5
    ):
        """查询投资者合约保证金率，返回 CThostFtdcInstrumentMarginRateField 或 None"""
        if not self._ready:
            return None
        with self._query_lock:
            self._query_done.clear()
            self._last_margin_rate = None
            field = CThostFtdcQryInstrumentMarginRateField()
            field.BrokerID = self.broker_id
            field.InvestorID = self.user_id
            field.InstrumentID = str(instrument_id or "")
            if exchange_id:
                field.ExchangeID = str(exchange_id)
            if hedge_flag:
                field.HedgeFlag = str(hedge_flag)
            ok = self._submit_query(
                lambda req_id: self._api.ReqQryInstrumentMarginRate(field, req_id)
            )
            if not ok:
                return None
            self._query_done.wait(timeout)
            return self._last_margin_rate

    def query_instrument_commission_rate(self, instrument_id, exchange_id="", timeout=5):
        """查询投资者合约手续费率，返回 CThostFtdcInstrumentCommissionRateField 或 None"""
        if not self._ready:
            return None
        with self._query_lock:
            self._query_done.clear()
            self._last_commission_rate = None
            field = CThostFtdcQryInstrumentCommissionRateField()
            field.BrokerID = self.broker_id
            field.InvestorID = self.user_id
            field.InstrumentID = str(instrument_id or "")
            if exchange_id:
                field.ExchangeID = str(exchange_id)
            ok = self._submit_query(
                lambda req_id: self._api.ReqQryInstrumentCommissionRate(field, req_id)
            )
            if not ok:
                return None
            self._query_done.wait(timeout)
            return self._last_commission_rate

    def _submit_query(self, submit):
        elapsed = time.monotonic() - self._last_query_submitted_at
        wait_time = self._query_interval - elapsed
        if wait_time > 0:
            time.sleep(wait_time)
        self._req_id += 1
        ret = submit(self._req_id)
        self._last_query_submitted_at = time.monotonic()
        return ret in (None, 0)

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
            "event": event_type,
            "request_id": request_id,
            "error_id": getattr(rsp_info, "ErrorID", 0) if rsp_info is not None else 0,
            "error_msg": getattr(rsp_info, "ErrorMsg", "") if rsp_info is not None else "",
            "field": _snapshot_ctp_field(field),
        }
        self._error_events.put(payload)
        if self.on_error and rsp_info is not None:
            self.on_error(rsp_info)

    @property
    def api(self):
        """获取底层 CThostFtdcTraderApi 对象，用于发送自定义请求"""
        return self._api

    def stop(self):
        """停止并释放资源

        macOS 上 CTP C++ API 的 Release() 在 Join() 仍然运行于
        另一个线程时会触发 segfault。因此:
        - 非阻塞模式 (daemon thread): 仅置空引用，让 daemon 线程随进程退出
        - 阻塞模式 (Join 已返回): 安全调用 Release()
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
        return self._connected and self._ready
