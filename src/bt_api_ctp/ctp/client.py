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
import importlib.machinery
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from bt_api_ctp.query import QueryResult

from ._ctp_base import (
    get_ctp_native_diagnostics as _get_vendored_ctp_native_diagnostics,
)
from ._ctp_base import (
    is_ctp_native_loaded as _is_vendored_ctp_native_loaded,
)

_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
CTP_REQUEST_COUNT_KEYS = (
    "authenticate",
    "login",
    "settlement_confirm",
    "order_insert",
    "order_action",
    "query_account",
    "query_positions",
    "query_orders",
    "query_trades",
    "query_instruments",
    "query_margin_rate",
    "query_commission_rate",
    "query_settlement_confirmation",
)
_CTP_EXECUTION_GATE_PROOF_FIELDS = (
    "account_fingerprint",
    "trading_day",
    "instrument",
    "connection_generation",
    "environment_profile",
    "preflight_sha256",
    "receipt_sha256",
    "native_sha256",
    "ctp_package_sha256",
    "source_hashes_sha256",
    "dependency_hashes_sha256",
)
_CTP_EXECUTION_GATE_HASH_FIELDS = (
    "preflight_sha256",
    "receipt_sha256",
    "native_sha256",
    "ctp_package_sha256",
    "source_hashes_sha256",
    "dependency_hashes_sha256",
)
_CTP_EXCHANGES = {"CFFEX", "CZCE", "DCE", "GFEX", "INE", "SHFE"}
_CTP_EXCHANGE_ALIASES = {"ZCE": "CZCE"}
_CTP_INSTRUMENT_RE = re.compile(r"^[A-Z]{1,3}[0-9]{3,4}$")
_CTP_GATE_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class CtpExecutionGateError(RuntimeError):
    """Credential-free, deterministic rejection from the managed CTP write gate."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ManagedTraderApiView:
    """Dynamically expose the current native API through the managed gate."""

    def __init__(self, client: Any) -> None:
        # Keep no native API reference here.  A caller may cache this view or a
        # Req* callable before the SDK configures the gate; every invocation
        # must still observe the client's current API and gate state.
        self.__client_ref = weakref.ref(client)

    def __getattr__(self, name: str) -> Any:
        client = self.__client_ref()
        if client is None:
            raise CtpExecutionGateError("ctp_execution_gate_client_unavailable")
        if name.startswith("Req"):

            def invoke(*args: Any, **kwargs: Any) -> Any:
                current = self.__client_ref()
                if current is None:
                    raise CtpExecutionGateError("ctp_execution_gate_client_unavailable")
                return current._invoke_public_api_request(name, args, kwargs)

            return invoke
        value = client._get_public_api_attribute(name)
        if callable(value):

            def invoke(*args: Any, **kwargs: Any) -> Any:
                current = self.__client_ref()
                if current is None:
                    raise CtpExecutionGateError("ctp_execution_gate_client_unavailable")
                return current._invoke_public_api_callable(name, args, kwargs)

            return invoke
        return value


def _canonical_ctp_exchange(value: Any) -> str:
    exchange = str(value or "").strip().upper()
    exchange = _CTP_EXCHANGE_ALIASES.get(exchange, exchange)
    return exchange if exchange in _CTP_EXCHANGES else ""


def canonical_ctp_instrument(value: Any, exchange_id: Any = None) -> str:
    """Return the execution-gate contract identity as ``EXCHANGE.INSTRUMENT``."""

    text = str(value or "").strip().upper()
    supplied_exchange = _canonical_ctp_exchange(exchange_id)
    if not text or (exchange_id not in (None, "") and not supplied_exchange):
        return ""
    parts = text.split(".")
    if len(parts) == 1:
        exchange = supplied_exchange
        instrument = parts[0]
    elif len(parts) == 2:
        first_exchange = _canonical_ctp_exchange(parts[0])
        last_exchange = _canonical_ctp_exchange(parts[1])
        if bool(first_exchange) == bool(last_exchange):
            return ""
        exchange = first_exchange or last_exchange
        instrument = parts[1] if first_exchange else parts[0]
        if supplied_exchange and supplied_exchange != exchange:
            return ""
    else:
        return ""
    if not exchange or not _CTP_INSTRUMENT_RE.fullmatch(instrument):
        return ""
    letters = instrument.rstrip("0123456789")
    digits = instrument[len(letters) :]
    if exchange == "CZCE" and len(digits) == 4:
        digits = digits[-3:]
    return f"{exchange}.{letters}{digits}"


def _execution_gate_proof(value: Any) -> tuple[dict[str, Any], str]:
    if not isinstance(value, Mapping) or set(value) != set(
        _CTP_EXECUTION_GATE_PROOF_FIELDS
    ):
        raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    proof = {field: value[field] for field in _CTP_EXECUTION_GATE_PROOF_FIELDS}
    for field in (
        "account_fingerprint",
        "trading_day",
        "instrument",
        "environment_profile",
    ):
        item = proof[field]
        if not isinstance(item, str) or not item or item != item.strip():
            raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    account_fingerprint = proof["account_fingerprint"]
    account_digest = account_fingerprint.removeprefix("acct_")
    if (
        account_fingerprint != account_fingerprint.lower()
        or not account_fingerprint.startswith("acct_")
        or len(account_digest) != 16
        or any(char not in "0123456789abcdef" for char in account_digest)
    ):
        raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    canonical_instrument = canonical_ctp_instrument(proof["instrument"])
    if not canonical_instrument or canonical_instrument != proof["instrument"]:
        raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    generation = proof["connection_generation"]
    if type(generation) is not int or generation <= 0:
        raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    try:
        parsed_day = datetime.strptime(proof["trading_day"], "%Y%m%d")
    except ValueError:
        raise CtpExecutionGateError("ctp_execution_gate_invalid_proof") from None
    if parsed_day.strftime("%Y%m%d") != proof["trading_day"]:
        raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    for field in _CTP_EXECUTION_GATE_HASH_FIELDS:
        digest = proof[field]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or digest != digest.lower()
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    serialized = json.dumps(
        proof, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return proof, hashlib.sha256(serialized).hexdigest()


def _execution_gate_reason(value: Any) -> str:
    reason = str(value or "execution_arm_revoked").strip().lower()
    return reason if _CTP_GATE_REASON_RE.fullmatch(reason) else "execution_arm_revoked"


def empty_ctp_request_counts() -> dict[str, int]:
    """Return the closed request-count schema used by strict read-only gates."""

    return dict.fromkeys(CTP_REQUEST_COUNT_KEYS, 0)


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


def _probe_external_runtime_import(runtime_source: str) -> None:
    if runtime_source == "external_ctp_python":
        code = (
            "import ctp; "
            "from ctp import CThostFtdcMdApi, CThostFtdcTraderApi; "
            "print('ctp import ok')"
        )
        runtime_name = "ctp"
    else:
        code = (
            "from openctp_ctp import mdapi, tdapi; "
            "assert mdapi.CThostFtdcMdApi and tdapi.CThostFtdcTraderApi; "
            "print('openctp_ctp import ok')"
        )
        runtime_name = "openctp_ctp"
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImportError(
            f"external CTP runtime {runtime_name} import preflight timed out"
        ) from exc

    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-2000:]
        stdout_tail = (result.stdout or "")[-500:]
        raise ImportError(
            f"external CTP runtime {runtime_name} failed import preflight "
            f"(returncode={result.returncode}). stderr={stderr_tail!r} stdout={stdout_tail!r}"
        )


def _probe_openctp_import() -> None:
    """Compatibility wrapper for callers that exercised the old helper."""
    _probe_external_runtime_import("external_openctp_ctp")


_CTP_RUNTIME_SOURCE = _select_ctp_runtime_source()

if _CTP_RUNTIME_SOURCE == "external_ctp_python":
    _probe_external_runtime_import(_CTP_RUNTIME_SOURCE)
    from ctp import (
        CThostFtdcMdApi,
        CThostFtdcMdSpi,
        CThostFtdcQryInstrumentCommissionRateField,
        CThostFtdcQryInstrumentField,
        CThostFtdcQryInstrumentMarginRateField,
        CThostFtdcQryInvestorPositionField,
        CThostFtdcQryOrderField,
        CThostFtdcQrySettlementInfoConfirmField,
        CThostFtdcQryTradeField,
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
    CThostFtdcQryInstrumentMarginRateField = (
        _openctp_tdapi.CThostFtdcQryInstrumentMarginRateField
    )
    CThostFtdcQryInvestorPositionField = (
        _openctp_tdapi.CThostFtdcQryInvestorPositionField
    )
    CThostFtdcQryOrderField = _openctp_tdapi.CThostFtdcQryOrderField
    CThostFtdcQrySettlementInfoConfirmField = (
        _openctp_tdapi.CThostFtdcQrySettlementInfoConfirmField
    )
    CThostFtdcQryTradeField = _openctp_tdapi.CThostFtdcQryTradeField
    CThostFtdcQryTradingAccountField = _openctp_tdapi.CThostFtdcQryTradingAccountField
    CThostFtdcReqAuthenticateField = _openctp_tdapi.CThostFtdcReqAuthenticateField
    CThostFtdcReqUserLoginField = _openctp_tdapi.CThostFtdcReqUserLoginField
    CThostFtdcSettlementInfoConfirmField = (
        _openctp_tdapi.CThostFtdcSettlementInfoConfirmField
    )
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
        CThostFtdcQrySettlementInfoConfirmField,
        CThostFtdcQryTradeField,
        CThostFtdcQryTradingAccountField,
    )
    from .ctp_trader_api import CThostFtdcTraderApi, CThostFtdcTraderSpi


def _is_native_extension_path(path: Path) -> bool:
    path_text = str(path)
    return any(
        path_text.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES
    )


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _ctp_python_package_identity(
    package_root: Path | None = None,
) -> tuple[list[dict[str, str]], str]:
    """Hash every controlled Python source under the installed CTP package."""

    if package_root is None:
        import bt_api_ctp

        package_root = Path(bt_api_ctp.__file__).resolve().parent
    else:
        package_root = Path(package_root).resolve()
    paths = sorted(
        (
            path
            for path in package_root.rglob("*.py")
            if path.is_file()
            and "__pycache__" not in path.relative_to(package_root).parts
        ),
        key=lambda path: path.relative_to(package_root).as_posix(),
    )
    manifest = [
        {
            "path": path.relative_to(package_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in paths
    ]
    serialized = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return manifest, hashlib.sha256(serialized).hexdigest()


def _runtime_module_prefix() -> str:
    if _CTP_RUNTIME_SOURCE == "external_ctp_python":
        return "ctp"
    if _CTP_RUNTIME_SOURCE == "external_openctp_ctp":
        return "openctp_ctp"
    return "bt_api_ctp.ctp"


def _selected_runtime_modules() -> dict[str, str]:
    prefix = _runtime_module_prefix()
    paths: dict[str, str] = {}
    for module_name, module in tuple(sys.modules.items()):
        if module_name != prefix and not module_name.startswith(f"{prefix}."):
            continue
        origin = str(getattr(module, "__file__", "") or "")
        if origin:
            paths[module_name] = str(Path(origin).resolve())
    return dict(sorted(paths.items()))


def get_ctp_native_diagnostics() -> dict[str, Any]:
    """Report exact native binaries loaded for the selected CTP runtime."""
    if _CTP_RUNTIME_SOURCE == "vendored_bt_api_py":
        diagnostics: dict[str, Any] = dict(_get_vendored_ctp_native_diagnostics())
    else:
        diagnostics = {}

    package_manifest, package_sha256 = _ctp_python_package_identity()
    runtime_modules = _selected_runtime_modules()
    native_modules = {
        module_name: module_path
        for module_name, module_path in runtime_modules.items()
        if _is_native_extension_path(Path(module_path))
    }
    native_hashes = {
        module_path: _sha256_file(Path(module_path))
        for module_path in sorted(set(native_modules.values()))
    }
    native_hashes = {path: digest for path, digest in native_hashes.items() if digest}
    binding_classes = {
        name: {
            "module": cls.__module__,
            "module_path": runtime_modules.get(cls.__module__, ""),
        }
        for name, cls in (
            ("CThostFtdcMdApi", CThostFtdcMdApi),
            ("CThostFtdcMdSpi", CThostFtdcMdSpi),
            ("CThostFtdcTraderApi", CThostFtdcTraderApi),
            ("CThostFtdcTraderSpi", CThostFtdcTraderSpi),
        )
    }
    native_loaded = (
        _is_vendored_ctp_native_loaded()
        if _CTP_RUNTIME_SOURCE == "vendored_bt_api_py"
        else bool(native_modules)
    )
    loaded_paths = sorted(set(native_modules.values()))
    selected_path = loaded_paths[0] if loaded_paths else ""
    diagnostics_override = {
        "runtime_source": _CTP_RUNTIME_SOURCE,
        "native_loaded": native_loaded,
        "reason": (
            "native_loaded"
            if native_loaded
            else "selected_runtime_has_no_native_extension"
        ),
        "runtime_module_paths": runtime_modules,
        "native_module_paths": native_modules,
        "native_module_sha256": native_hashes,
        "binding_classes": binding_classes,
        "ctp_package_manifest": package_manifest,
        "ctp_package_sha256": package_sha256,
        "loaded_module_path": selected_path,
        "loaded_module_sha256": native_hashes.get(selected_path, ""),
    }
    diagnostics.update(diagnostics_override)
    return diagnostics


def is_ctp_native_loaded() -> bool:
    """Return whether the selected runtime has a verified loaded native binary."""
    return bool(get_ctp_native_diagnostics()["native_loaded"])


def _format_selected_native_diagnostics(diagnostics: dict[str, Any]) -> str:
    if diagnostics["native_loaded"]:
        return (
            f"CTP runtime {diagnostics['runtime_source']} loaded native module "
            f"{diagnostics['loaded_module_path']}"
        )
    detail = str(
        diagnostics.get("import_error") or diagnostics.get("reason") or "unknown"
    )
    return f"CTP runtime {diagnostics['runtime_source']} has no verified native extension: {detail}"


def _check_native_module():
    """Raise ImportError early if the selected CTP native runtime is unavailable."""
    diagnostics = get_ctp_native_diagnostics()
    if diagnostics["native_loaded"]:
        return
    raise ImportError(
        f"{_format_selected_native_diagnostics(diagnostics)}. "
        "Install a native extension matching this OS, Python ABI, and selected CTP runtime."
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


class _QueryRecordSnapshot(dict[str, Any]):
    """Detached CTP query row retaining legacy attribute-style reads."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _snapshot_query_record(record: Any) -> _QueryRecordSnapshot:
    if isinstance(record, dict):
        return _QueryRecordSnapshot(record)
    return _QueryRecordSnapshot(_snapshot_ctp_field(record))


def _rsp_error(rsp_info: Any) -> tuple[int | None, str]:
    if rsp_info is None:
        return None, ""
    try:
        error_id = int(getattr(rsp_info, "ErrorID", 0) or 0)
    except (TypeError, ValueError):
        error_id = -1
    return error_id, str(getattr(rsp_info, "ErrorMsg", "") or "")


@dataclass
class _QueryAccumulator:
    request_type: str
    request_id: int
    connection_generation: int
    account_fingerprint: str
    started_at_utc: datetime
    event: threading.Event = dataclass_field(default_factory=threading.Event)
    records: list[Any] = dataclass_field(default_factory=list)
    completed_at_utc: datetime | None = None
    is_last_seen: bool = False
    error_code: int | None = None
    error_message: str = ""
    timed_out: bool = False
    sealed: bool = False
    late_callback_count: int = 0
    unsupported: bool = False
    submit_code: int | None = None

    def result(self) -> QueryResult[Any]:
        complete = (
            self.sealed
            and self.is_last_seen
            and not self.timed_out
            and not self.unsupported
            and self.error_code in (None, 0)
        )
        return QueryResult(
            request_type=self.request_type,
            request_id=self.request_id,
            connection_generation=self.connection_generation,
            account_fingerprint=self.account_fingerprint,
            started_at_utc=self.started_at_utc,
            completed_at_utc=self.completed_at_utc,
            is_last_seen=self.is_last_seen,
            error_code=self.error_code,
            error_message=self.error_message,
            timed_out=self.timed_out,
            complete=complete,
            records=tuple(self.records),
            late_callback_count=self.late_callback_count,
            unsupported=self.unsupported,
            submit_code=self.submit_code,
        )


# ===========================================================================
#  MdClient - 行情客户端
# ===========================================================================


class _MdSpi(CThostFtdcMdSpi):
    def __init__(self, client):
        super().__init__()
        self._c = client

    def OnFrontConnected(self):
        self._c._connection_generation += 1
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
        self._connection_generation = 0
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

    @property
    def connection_generation(self):
        return self._connection_generation


# ===========================================================================
#  TraderClient - 交易客户端
# ===========================================================================


def _fence_trader_spi_callback(callback):
    @wraps(callback)
    def guarded(self, *args, **kwargs):
        with self._c._query_state_lock:
            if not self._is_current_locked():
                return None
        # Callback bodies take the state lock only while mutating SDK state.
        # In particular, user callbacks must never run while this lock is held:
        # they may synchronously start a typed query that needs _query_lock.
        return callback(self, *args, **kwargs)

    return guarded


class _TraderSpi(CThostFtdcTraderSpi):
    def __init__(self, client, native_api=None):
        super().__init__()
        self._c = client
        self._native_api = native_api

    def _is_current_locked(self) -> bool:
        if self._native_api is None:
            # Offline unit tests construct an unbound SPI directly.
            return True
        return self._c._spi is self and self._c._api is self._native_api

    def _is_current(self) -> bool:
        with self._c._query_state_lock:
            return self._is_current_locked()

    @_fence_trader_spi_callback
    def OnFrontConnected(self):
        with self._c._query_state_lock:
            if not self._is_current_locked():
                return
            self._c._on_front_connected()
            field = CThostFtdcReqAuthenticateField()
            field.BrokerID = self._c.broker_id
            field.UserID = self._c.user_id
            field.AppID = self._c.app_id
            field.AuthCode = self._c.auth_code
            request_id = self._c._next_request_id()
            generation = self._c._connection_generation
            self._c._authentication_request_id = request_id
            self._c._authentication_connection_generation = generation
            self._c._record_request("authenticate")
            api = self._c._api
        try:
            ret = api.ReqAuthenticate(field, request_id)
        except Exception as exc:
            with self._c._query_state_lock:
                if (
                    self._c._authentication_request_id == request_id
                    and self._c._authentication_connection_generation == generation
                    and self._c._connection_generation == generation
                ):
                    self._c._authentication_state = "failed"
                    self._c._last_session_error = {
                        "error": "authentication_submit_failed",
                        "detail": type(exc).__name__,
                    }
            return
        if ret not in (None, 0):
            with self._c._query_state_lock:
                if (
                    self._c._authentication_request_id == request_id
                    and self._c._authentication_connection_generation == generation
                    and self._c._connection_generation == generation
                ):
                    self._c._authentication_state = "failed"
                    self._c._last_session_error = {
                        "error": "authentication_submit_rejected",
                        "submit_code": ret,
                    }

    @_fence_trader_spi_callback
    def OnFrontDisconnected(self, nReason):
        with self._c._query_state_lock:
            if not self._is_current_locked():
                return
            self._c._on_front_disconnected(nReason)

    @_fence_trader_spi_callback
    def OnRspAuthenticate(self, pRspAuthenticateField, pRspInfo, nRequestID, bIsLast):
        error_callback = None
        login_submission = None
        with self._c._query_state_lock:
            accepted = (
                self._is_current_locked()
                and self._c._authentication_state == "authenticating"
                and self._c._authentication_request_id == int(nRequestID)
                and self._c._authentication_connection_generation
                == self._c._connection_generation
            )
            if not accepted:
                self._c._authentication_late_callback_count += 1
                return
            self._c._authentication_request_id = None
            self._c._authentication_connection_generation = None
            error_id, _ = _rsp_error(pRspInfo)
            if error_id not in (None, 0):
                self._c._authentication_state = "failed"
                self._c._last_session_error = _snapshot_ctp_field(pRspInfo)
                error_callback = self._c.on_error
            else:
                self._c._authentication_state = "authenticated"
                field = CThostFtdcReqUserLoginField()
                field.BrokerID = self._c.broker_id
                field.UserID = self._c.user_id
                field.Password = self._c.password
                request_id = self._c._next_request_id()
                generation = self._c._connection_generation
                self._c._login_state = "logging_in"
                self._c._login_request_id = request_id
                self._c._login_connection_generation = generation
                self._c._record_request("login")
                login_submission = (self._c._api, field, request_id, generation)
        if error_callback is not None:
            error_callback(pRspInfo)
            return
        if login_submission is None:
            return
        api, field, request_id, generation = login_submission
        try:
            ret = api.ReqUserLogin(field, request_id)
        except Exception as exc:
            with self._c._query_state_lock:
                if (
                    self._c._login_request_id == request_id
                    and self._c._login_connection_generation == generation
                    and self._c._connection_generation == generation
                ):
                    self._c._login_state = "failed"
                    self._c._last_session_error = {
                        "error": "login_submit_failed",
                        "detail": type(exc).__name__,
                    }
            return
        if ret not in (None, 0):
            with self._c._query_state_lock:
                if (
                    self._c._login_request_id == request_id
                    and self._c._login_connection_generation == generation
                    and self._c._connection_generation == generation
                ):
                    self._c._login_state = "failed"
                    self._c._last_session_error = {
                        "error": "login_submit_rejected",
                        "submit_code": ret,
                    }

    @_fence_trader_spi_callback
    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        login_callback = None
        error_callback = None
        request_settlement_for_generation = None
        with self._c._query_state_lock:
            accepted = (
                self._is_current_locked()
                and self._c._login_state == "logging_in"
                and self._c._login_request_id == int(nRequestID)
                and self._c._login_connection_generation
                == self._c._connection_generation
            )
            if not accepted:
                self._c._login_late_callback_count += 1
                return
            generation = self._c._connection_generation
            self._c._login_request_id = None
            self._c._login_connection_generation = None
            error_id, _ = _rsp_error(pRspInfo)
            if error_id in (None, 0):
                self._c._login_state = "logged_in"
                self._c._front_id = pRspUserLogin.FrontID
                self._c._session_id = pRspUserLogin.SessionID
                self._c._trading_day = str(
                    getattr(pRspUserLogin, "TradingDay", "") or ""
                )
                with suppress(TypeError, ValueError):
                    self._c._max_order_ref = max(
                        self._c._max_order_ref,
                        int(getattr(pRspUserLogin, "MaxOrderRef", "") or 0),
                    )
                self._c._ready = False
                self._c._settlement_readback_verified = False
                self._c._settlement_proof_source = "none"
                self._c._settlement_proof_query_request_id = None
                if self._c.auto_settlement_confirm:
                    request_settlement_for_generation = generation
                else:
                    self._c._settlement_state = "not_requested"
                login_callback = self._c.on_login
            else:
                self._c._login_state = "failed"
                self._c._last_session_error = _snapshot_ctp_field(pRspInfo)
                error_callback = self._c.on_error
        if request_settlement_for_generation is not None:
            self._c._request_settlement_confirmation(
                expected_generation=request_settlement_for_generation
            )
        if login_callback is not None:
            login_callback(pRspUserLogin)
        if error_callback is not None:
            error_callback(pRspInfo)

    @_fence_trader_spi_callback
    def OnRspSettlementInfoConfirm(
        self, pSettlementInfoConfirm, pRspInfo, nRequestID, bIsLast
    ):
        error_callback = None
        with self._c._query_state_lock:
            if not self._is_current_locked():
                return
            if not self._c._accept_settlement_callback(
                nRequestID, pSettlementInfoConfirm
            ):
                return
            error_id, _ = _rsp_error(pRspInfo)
            if error_id in (None, 0):
                self._c._settlement_state = "confirmed"
                self._c._ready = False
                self._c._settlement_readback_verified = False
                self._c._settlement_proof_source = "direct_confirmation"
                self._c._settlement_proof_query_request_id = None
                self._c._revoke_execution_gate_locked(
                    "ctp_execution_gate_settlement_direct_confirmation_requires_readback"
                )
            else:
                self._c._settlement_state = "failed"
                self._c._ready = False
                self._c._settlement_readback_verified = False
                self._c._last_session_error = _snapshot_ctp_field(pRspInfo)
                error_callback = self._c.on_error
            self._c._settlement_done.set()
        if error_callback is not None:
            error_callback(pRspInfo)

    @_fence_trader_spi_callback
    def OnRspQryTradingAccount(self, pTradingAccount, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._handle_query_callback(
            "account", pTradingAccount, pRspInfo, nRequestID, bIsLast
        )

    @_fence_trader_spi_callback
    def OnRspQryInvestorPosition(self, pPos, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._handle_query_callback("positions", pPos, pRspInfo, nRequestID, bIsLast)

    @_fence_trader_spi_callback
    def OnRspQryOrder(self, pOrder, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._handle_query_callback("orders", pOrder, pRspInfo, nRequestID, bIsLast)

    @_fence_trader_spi_callback
    def OnRspQryTrade(self, pTrade, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._handle_query_callback("trades", pTrade, pRspInfo, nRequestID, bIsLast)

    @_fence_trader_spi_callback
    def OnRspQryInstrument(self, pInstrument, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._handle_query_callback(
            "instruments", pInstrument, pRspInfo, nRequestID, bIsLast
        )

    @_fence_trader_spi_callback
    def OnRspQryInstrumentMarginRate(
        self, pInstrumentMarginRate, pRspInfo, nRequestID, bIsLast
    ):
        if not self._is_current():
            return
        self._c._handle_query_callback(
            "margin_rate", pInstrumentMarginRate, pRspInfo, nRequestID, bIsLast
        )

    @_fence_trader_spi_callback
    def OnRspQryInstrumentCommissionRate(
        self, pInstrumentCommissionRate, pRspInfo, nRequestID, bIsLast
    ):
        if not self._is_current():
            return
        self._c._handle_query_callback(
            "commission_rate",
            pInstrumentCommissionRate,
            pRspInfo,
            nRequestID,
            bIsLast,
        )

    @_fence_trader_spi_callback
    def OnRspQrySettlementInfoConfirm(
        self, pSettlementInfoConfirm, pRspInfo, nRequestID, bIsLast
    ):
        if not self._is_current():
            return
        self._c._handle_query_callback(
            "settlement_confirmation",
            pSettlementInfoConfirm,
            pRspInfo,
            nRequestID,
            bIsLast,
        )

    @_fence_trader_spi_callback
    def OnRtnOrder(self, pOrder):
        if not self._is_current():
            return
        self._c._push_order_event(pOrder)

    @_fence_trader_spi_callback
    def OnRtnTrade(self, pTrade):
        if not self._is_current():
            return
        self._c._push_trade_event(pTrade)

    @_fence_trader_spi_callback
    def OnRspOrderInsert(self, pInputOrder, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._push_error_event(
            event_type="order_insert_response",
            rsp_info=pRspInfo,
            field=pInputOrder,
            request_id=nRequestID,
        )

    @_fence_trader_spi_callback
    def OnErrRtnOrderInsert(self, pInputOrder, pRspInfo):
        if not self._is_current():
            return
        self._c._push_error_event(
            event_type="order_insert_error",
            rsp_info=pRspInfo,
            field=pInputOrder,
        )

    @_fence_trader_spi_callback
    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._handle_query_error(pRspInfo, nRequestID, bIsLast)
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
        auto_settlement_confirm=True,
    ):
        self.front = front
        self.broker_id = broker_id
        self.user_id = user_id
        self.password = password
        self.app_id = app_id
        self.auth_code = auth_code
        self.auto_settlement_confirm = bool(auto_settlement_confirm)
        self._account_fingerprint = hashlib.sha256(
            f"{self.broker_id}:{self.user_id}".encode()
        ).hexdigest()[:16]

        self.on_login = None  # callback(CThostFtdcRspUserLoginField)
        self.on_order = None  # callback(CThostFtdcOrderField)
        self.on_trade = None  # callback(CThostFtdcTradeField)
        self.on_error = None  # callback(CThostFtdcRspInfoField)

        self._connected = False
        self._ready = False
        self._authentication_state = "disconnected"
        self._login_state = "disconnected"
        self._settlement_state = "unknown"
        self._trading_day = ""
        self._last_session_error: dict[str, Any] = {}
        self._connection_generation = 0
        self._req_id = 0
        self._front_id = 0
        self._session_id = 0
        self._api_view = _ManagedTraderApiView(self)
        self._api = None
        self._session_native_api = None
        self._spi = None
        self._thread = None
        self._settlement_done = threading.Event()
        self._settlement_request_id: int | None = None
        self._settlement_connection_generation: int | None = None
        self._settlement_account_fingerprint: str | None = None
        self._settlement_trading_day: str | None = None
        self._settlement_late_callback_count = 0
        self._settlement_proof_source = "none"
        self._settlement_proof_query_request_id: int | None = None
        self._settlement_readback_verified = False
        self._authentication_request_id: int | None = None
        self._authentication_connection_generation: int | None = None
        self._authentication_late_callback_count = 0
        self._login_request_id: int | None = None
        self._login_connection_generation: int | None = None
        self._login_late_callback_count = 0
        self._query_done = threading.Event()
        self._last_account = None
        self._last_positions = []
        self._last_orders = []
        self._last_instrument = None
        self._last_margin_rate = None
        self._last_commission_rate = None
        self._query_lock = threading.Lock()
        self._query_state_lock = threading.RLock()
        self._query_history: dict[int, _QueryAccumulator] = {}
        self._orphan_query_callbacks: list[dict[str, Any]] = []
        self._request_counts: dict[str, int] = empty_ctp_request_counts()
        self._execution_gate_capability: object | None = None
        self._execution_gate_proof: dict[str, Any] | None = None
        self._execution_gate_proof_sha256: str | None = None
        self._execution_gate_environment_profile: str | None = None
        self._execution_gate_revocation_reason: str | None = None
        self._execution_gate_native_api = None
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

    @property
    def _api(self) -> Any:
        return getattr(self, "_TraderClient__native_api", None)

    @_api.setter
    def _api(self, value: Any) -> None:
        current = getattr(self, "_TraderClient__native_api", None)
        if current is value:
            return
        lock = getattr(self, "_query_state_lock", None)
        if lock is None:
            self.__native_api = value
            return
        with lock:
            if getattr(self, "_execution_gate_capability", None) is not None:
                self._revoke_execution_gate_locked(
                    "ctp_execution_gate_native_api_changed"
                )
            self._session_native_api = None
            # Any callbacks from the previous SPI become stale immediately.
            self._spi = None
            self.__native_api = value

    def _invoke_public_api_request(
        self,
        name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Resolve a cached public Req* callable at invocation time."""

        with self._query_state_lock:
            if self._execution_gate_capability is not None:
                # Managed callers must use the typed query methods so request
                # IDs, generation fencing and the one-query-at-a-time lock
                # cannot be bypassed through a cached native Req* handle.
                code = (
                    "ctp_execution_gate_raw_request_blocked"
                    if name.startswith(("ReqQry", "ReqQuery"))
                    else "ctp_execution_gate_native_write_blocked"
                )
                raise CtpExecutionGateError(code)
            api = self._api
            if api is None:
                raise CtpExecutionGateError("ctp_execution_gate_native_api_unavailable")
            target = getattr(api, name)
        return target(*args, **kwargs)

    def _get_public_api_attribute(self, name: str) -> Any:
        """Resolve a non-request native attribute without storing it in the view."""

        with self._query_state_lock:
            api = self._api
            if api is None:
                raise AttributeError(name)
            return getattr(api, name)

    def _invoke_public_api_callable(
        self,
        name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Fence cached non-request native callables after gate installation."""

        with self._query_state_lock:
            if self._execution_gate_capability is not None:
                raise CtpExecutionGateError("ctp_execution_gate_native_write_blocked")
            api = self._api
            if api is None:
                raise CtpExecutionGateError("ctp_execution_gate_native_api_unavailable")
            target = getattr(api, name)
            if not callable(target):
                raise TypeError(f"native API attribute {name!r} is not callable")
        return target(*args, **kwargs)

    def _execution_gate_state_locked(self) -> dict[str, Any]:
        proof = self._execution_gate_proof
        return {
            "managed": self._execution_gate_capability is not None,
            "armed": proof is not None,
            "connection_generation": (
                proof.get("connection_generation") if proof is not None else None
            ),
            "trading_day": proof.get("trading_day") if proof is not None else None,
            "instrument": proof.get("instrument") if proof is not None else None,
            "environment_profile": self._execution_gate_environment_profile,
            "proof_sha256": self._execution_gate_proof_sha256,
            "revocation_reason": self._execution_gate_revocation_reason,
        }

    def get_execution_gate_state(self) -> dict[str, Any]:
        """Return the managed native-order gate without exposing its capability."""

        with self._query_state_lock:
            return self._execution_gate_state_locked()

    def configure_execution_gate(self, capability: object) -> dict[str, Any]:
        """Install the SDK-owned opaque capability and start disarmed."""

        if capability is None:
            raise CtpExecutionGateError("ctp_execution_gate_capability_required")
        with self._query_state_lock:
            installed = self._execution_gate_capability
            if installed is not None and installed is not capability:
                raise CtpExecutionGateError("ctp_execution_gate_already_configured")
            if installed is None:
                self._execution_gate_capability = capability
                self._execution_gate_proof = None
                self._execution_gate_proof_sha256 = None
                self._execution_gate_environment_profile = None
                self._execution_gate_revocation_reason = None
            return self._execution_gate_state_locked()

    def _revoke_execution_gate_locked(self, reason: Any) -> None:
        if self._execution_gate_capability is None:
            return
        self._execution_gate_proof = None
        self._execution_gate_proof_sha256 = None
        self._execution_gate_environment_profile = None
        self._execution_gate_native_api = None
        self._execution_gate_revocation_reason = _execution_gate_reason(reason)

    def _require_execution_write_locked(
        self,
        capability: object | None,
        instrument: Any,
        exchange_id: Any = None,
    ) -> None:
        installed = self._execution_gate_capability
        if installed is None:
            return
        if capability is not installed:
            raise CtpExecutionGateError("ctp_execution_gate_capability_mismatch")
        proof = self._execution_gate_proof
        if proof is None:
            raise CtpExecutionGateError("ctp_execution_gate_unarmed")
        mismatches = (
            (
                self._api is None
                or self._api is not self._execution_gate_native_api
                or self._api is not self._session_native_api,
                "ctp_execution_gate_native_api_mismatch",
            ),
            (
                self._connection_generation != proof["connection_generation"],
                "ctp_execution_gate_connection_generation_mismatch",
            ),
            (
                f"acct_{self._account_fingerprint}" != proof["account_fingerprint"],
                "ctp_execution_gate_account_fingerprint_mismatch",
            ),
            (
                self._trading_day != proof["trading_day"],
                "ctp_execution_gate_trading_day_mismatch",
            ),
            (
                not self.is_trading_ready,
                "ctp_execution_gate_session_not_trading_ready",
            ),
            (
                canonical_ctp_instrument(instrument, exchange_id)
                != proof["instrument"],
                "ctp_execution_gate_instrument_mismatch",
            ),
        )
        for mismatched, code in mismatches:
            if mismatched:
                self._revoke_execution_gate_locked(code)
                raise CtpExecutionGateError(code)

    def require_execution_write(
        self,
        capability: object | None,
        instrument: Any,
        exchange_id: Any = None,
    ) -> None:
        """Reject a managed order write before request IDs or native calls change."""

        with self._query_state_lock:
            self._require_execution_write_locked(capability, instrument, exchange_id)

    def arm_execution_gate(
        self,
        capability: object,
        proof: Mapping[str, Any],
        *,
        environment_profile: str,
    ) -> dict[str, Any]:
        """Bind native order writes to one current CTP connection and contract."""

        with self._query_state_lock:
            if capability is not self._execution_gate_capability:
                raise CtpExecutionGateError("ctp_execution_gate_capability_mismatch")
            try:
                normalized, proof_sha256 = _execution_gate_proof(proof)
                profile = str(environment_profile or "").strip()
                if not profile or profile != normalized["environment_profile"]:
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_environment_profile_mismatch"
                    )
                if self.auto_settlement_confirm:
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_auto_settlement_confirm_enabled"
                    )
                if not self.is_trading_ready:
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_session_not_trading_ready"
                    )
                if normalized["connection_generation"] != self._connection_generation:
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_connection_generation_mismatch"
                    )
                if (
                    normalized["account_fingerprint"]
                    != f"acct_{self._account_fingerprint}"
                ):
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_account_fingerprint_mismatch"
                    )
                if normalized["trading_day"] != self._trading_day:
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_trading_day_mismatch"
                    )
                if self._api is None or self._api is not self._session_native_api:
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_native_api_mismatch"
                    )
                if self._execution_gate_proof is not None:
                    if (
                        self._execution_gate_proof == normalized
                        and self._execution_gate_proof_sha256 == proof_sha256
                        and self._execution_gate_environment_profile == profile
                    ):
                        return self._execution_gate_state_locked()
                    raise CtpExecutionGateError("ctp_execution_gate_already_armed")
            except CtpExecutionGateError as exc:
                self._revoke_execution_gate_locked(exc.code)
                raise
            self._execution_gate_proof = normalized
            self._execution_gate_proof_sha256 = proof_sha256
            self._execution_gate_environment_profile = profile
            self._execution_gate_native_api = self._api
            self._execution_gate_revocation_reason = None
            return self._execution_gate_state_locked()

    def disarm_execution_gate(
        self,
        capability: object,
        reason: str = "execution_arm_revoked",
    ) -> dict[str, Any]:
        """Idempotently revoke managed native order writes."""

        with self._query_state_lock:
            if capability is not self._execution_gate_capability:
                raise CtpExecutionGateError("ctp_execution_gate_capability_mismatch")
            bounded_reason = _execution_gate_reason(reason)
            if self._execution_gate_proof is not None:
                self._revoke_execution_gate_locked(bounded_reason)
            elif self._execution_gate_revocation_reason is None:
                self._execution_gate_revocation_reason = bounded_reason
            return self._execution_gate_state_locked()

    def submit_order_insert(
        self,
        field: Any,
        request_id: int,
        *,
        execution_capability: object | None = None,
    ) -> Any:
        """Submit one order under the same lock as the final managed-gate check."""

        with self._query_state_lock:
            self._require_execution_write_locked(
                execution_capability,
                getattr(field, "InstrumentID", ""),
                getattr(field, "ExchangeID", ""),
            )
            if self._api is None:
                raise CtpExecutionGateError("ctp_execution_gate_native_api_unavailable")
            self._record_request("order_insert")
            return self._api.ReqOrderInsert(field, request_id)

    def submit_order_action(
        self,
        field: Any,
        request_id: int,
        *,
        execution_capability: object | None = None,
    ) -> Any:
        """Submit one cancellation under the managed native-order gate."""

        with self._query_state_lock:
            self._require_execution_write_locked(
                execution_capability,
                getattr(field, "InstrumentID", ""),
                getattr(field, "ExchangeID", ""),
            )
            if self._api is None:
                raise CtpExecutionGateError("ctp_execution_gate_native_api_unavailable")
            self._record_request("order_action")
            return self._api.ReqOrderAction(field, request_id)

    def _next_request_id(self) -> int:
        with self._query_state_lock:
            self._req_id += 1
            return self._req_id

    def _record_request(self, request_type: str) -> None:
        with self._query_state_lock:
            self._request_counts[request_type] = (
                self._request_counts.get(request_type, 0) + 1
            )

    def _clear_settlement_readback_locked(
        self,
        reason: str,
        *,
        request_id: int | None = None,
    ) -> None:
        """Invalidate server-readback proof and revoke any native write grant."""

        self._ready = False
        self._settlement_readback_verified = False
        self._settlement_proof_source = "none"
        self._settlement_proof_query_request_id = None
        self._last_session_error = {
            "error": reason,
            "query_request_id": request_id,
        }
        self._revoke_execution_gate_locked(reason)

    def _has_current_settlement_readback_locked(self) -> bool:
        return (
            self._settlement_readback_verified
            and self._settlement_proof_source == "confirmation_query"
            and self._settlement_proof_query_request_id is not None
            and self._settlement_connection_generation == self._connection_generation
            and self._settlement_account_fingerprint == self._account_fingerprint
            and self._settlement_trading_day == self._trading_day
        )

    def _on_front_connected(self) -> None:
        with self._query_state_lock:
            self._revoke_execution_gate_locked(
                "ctp_execution_gate_connection_generation_changed"
            )
            self._session_native_api = self._api
            self._connection_generation += 1
            self._connected = True
            self._ready = False
            self._authentication_state = "authenticating"
            self._login_state = "not_started"
            self._authentication_request_id = None
            self._authentication_connection_generation = None
            self._login_request_id = None
            self._login_connection_generation = None
            self._settlement_state = "unknown"
            self._settlement_request_id = None
            self._settlement_connection_generation = None
            self._settlement_account_fingerprint = None
            self._settlement_trading_day = None
            self._settlement_proof_source = "none"
            self._settlement_proof_query_request_id = None
            self._settlement_readback_verified = False
            self._last_session_error = {}

    def _on_front_disconnected(self, reason: Any) -> None:
        with self._query_state_lock:
            self._revoke_execution_gate_locked("ctp_execution_gate_disconnected")
            self._session_native_api = None
            self._connected = False
            self._ready = False
            self._authentication_state = "disconnected"
            self._login_state = "disconnected"
            self._authentication_request_id = None
            self._authentication_connection_generation = None
            self._login_request_id = None
            self._login_connection_generation = None
            self._settlement_state = "unknown"
            self._settlement_request_id = None
            self._settlement_connection_generation = None
            self._settlement_account_fingerprint = None
            self._settlement_trading_day = None
            self._settlement_proof_source = "none"
            self._settlement_proof_query_request_id = None
            self._settlement_readback_verified = False
            self._last_session_error = {"disconnect_reason": reason}
            for accumulator in self._query_history.values():
                if accumulator.sealed:
                    continue
                accumulator.error_code = -2
                accumulator.error_message = "connection_lost"
                accumulator.completed_at_utc = datetime.now(timezone.utc)
                accumulator.sealed = True
                accumulator.event.set()

    def _require_settlement_write_locked(
        self, execution_capability: object | None
    ) -> None:
        installed = self._execution_gate_capability
        if installed is None:
            return
        if execution_capability is not installed:
            raise CtpExecutionGateError("ctp_execution_gate_capability_mismatch")
        if self.auto_settlement_confirm:
            raise CtpExecutionGateError(
                "ctp_execution_gate_auto_settlement_confirm_enabled"
            )
        if self._execution_gate_proof is not None:
            raise CtpExecutionGateError(
                "ctp_execution_gate_settlement_requires_disarmed"
            )
        if (
            not self.is_read_only_ready
            or self._connection_generation <= 0
            or not self._trading_day
        ):
            raise CtpExecutionGateError(
                "ctp_execution_gate_session_not_read_only_ready"
            )
        if self._api is None or self._api is not self._session_native_api:
            raise CtpExecutionGateError("ctp_execution_gate_native_api_mismatch")

    def _request_settlement_confirmation(
        self,
        *,
        execution_capability: object | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        with self._query_state_lock:
            self._require_settlement_write_locked(execution_capability)
            if (
                expected_generation is not None
                and expected_generation != self._connection_generation
            ):
                return False
            if self._settlement_state == "confirmed":
                return True
            if (
                self._execution_gate_capability is not None
                and self._settlement_request_id is not None
            ):
                # One managed confirmation attempt is allowed per connection.
                return self._settlement_state == "confirming"
            api = self._api
            if api is None:
                raise CtpExecutionGateError("ctp_execution_gate_native_api_unavailable")
            field = CThostFtdcSettlementInfoConfirmField()
            field.BrokerID = self.broker_id
            field.InvestorID = self.user_id
            request_id = self._next_request_id()
            self._settlement_done.clear()
            self._settlement_state = "confirming"
            self._settlement_request_id = request_id
            self._settlement_connection_generation = self._connection_generation
            self._settlement_account_fingerprint = self._account_fingerprint
            self._settlement_trading_day = self._trading_day
            self._ready = False
            self._settlement_readback_verified = False
            self._record_request("settlement_confirm")
            generation = self._connection_generation
        try:
            ret = api.ReqSettlementInfoConfirm(field, request_id)
        except Exception as exc:
            with self._query_state_lock:
                if (
                    self._settlement_state == "confirming"
                    and self._settlement_request_id == request_id
                    and self._settlement_connection_generation == generation
                    and self._connection_generation == generation
                ):
                    self._settlement_state = "failed"
                    self._last_session_error = {
                        "error": "settlement_confirm_submit_failed",
                        "detail": type(exc).__name__,
                    }
            return False
        if ret not in (None, 0):
            with self._query_state_lock:
                if (
                    self._settlement_state == "confirming"
                    and self._settlement_request_id == request_id
                    and self._settlement_connection_generation == generation
                    and self._connection_generation == generation
                ):
                    self._settlement_state = "failed"
                    self._last_session_error = {
                        "error": "settlement_confirm_submit_rejected",
                        "submit_code": ret,
                    }
            return False
        with self._query_state_lock:
            if self._connection_generation != generation:
                return False
            if self._settlement_request_id != request_id:
                return False
            if self._settlement_state == "failed":
                return False
            return True

    def _accept_settlement_callback(self, request_id: int, field: Any = None) -> bool:
        """Fence confirmation responses by request, generation and pending state."""
        with self._query_state_lock:
            accepted = (
                self._settlement_state == "confirming"
                and self._settlement_request_id == int(request_id)
                and self._settlement_connection_generation
                == self._connection_generation
                and self._settlement_account_fingerprint == self._account_fingerprint
                and self._settlement_trading_day == self._trading_day
            )
            field_broker = str(getattr(field, "BrokerID", "") or "")
            field_investor = str(getattr(field, "InvestorID", "") or "")
            field_trading_day = str(
                getattr(field, "TradingDay", "")
                or getattr(field, "ConfirmDate", "")
                or ""
            )
            if field_broker and field_broker != self.broker_id:
                accepted = False
            if field_investor and field_investor != self.user_id:
                accepted = False
            if field_trading_day and field_trading_day != self._trading_day:
                accepted = False
            if not accepted:
                self._settlement_late_callback_count += 1
                return False
            return True

    def confirm_settlement(
        self,
        timeout: float = 5.0,
        *,
        _execution_capability: object | None = None,
    ) -> bool:
        """Submit confirmation; trading stays read-only until server readback."""
        with self._query_state_lock:
            self._require_settlement_write_locked(_execution_capability)
            if not self.is_read_only_ready:
                return False
            if self._settlement_state == "confirmed":
                return True
        if not self._request_settlement_confirmation(
            execution_capability=_execution_capability
        ):
            return False
        if not self._settlement_done.wait(max(float(timeout), 0.0)):
            with self._query_state_lock:
                if self._settlement_state == "confirmed":
                    return True
                if self._settlement_state == "confirming":
                    self._settlement_state = "failed"
                    self._ready = False
                    self._last_session_error = {"error": "settlement_confirm_timeout"}
                return False
        return self._settlement_state == "confirmed"

    def get_session_state(self) -> dict[str, Any]:
        """Return distinct authentication, login, settlement and query state."""
        with self._query_state_lock:
            execution_gate = self._execution_gate_state_locked()
            return {
                "connected": self._connected,
                "auth_state": self._authentication_state,
                "login_state": self._login_state,
                "settlement_state": self._settlement_state,
                "read_only_ready": self.is_read_only_ready,
                "trading_ready": self.is_trading_ready,
                "ready": self.is_ready,
                "auto_settlement_confirm": self.auto_settlement_confirm,
                "front_id": self._front_id,
                "session_id": self._session_id,
                "trading_day": self._trading_day,
                "connection_generation": self._connection_generation,
                "account_fingerprint": self._account_fingerprint,
                "settlement_request_id": self._settlement_request_id,
                "settlement_connection_generation": self._settlement_connection_generation,
                "settlement_account_fingerprint": self._settlement_account_fingerprint,
                "settlement_trading_day": self._settlement_trading_day,
                "settlement_late_callback_count": self._settlement_late_callback_count,
                "settlement_proof_source": self._settlement_proof_source,
                "settlement_proof_query_request_id": self._settlement_proof_query_request_id,
                "settlement_readback_verified": (
                    self._has_current_settlement_readback_locked()
                ),
                "authentication_request_id": self._authentication_request_id,
                "authentication_connection_generation": (
                    self._authentication_connection_generation
                ),
                "authentication_late_callback_count": (
                    self._authentication_late_callback_count
                ),
                "login_request_id": self._login_request_id,
                "login_connection_generation": self._login_connection_generation,
                "login_late_callback_count": self._login_late_callback_count,
                "request_counts": dict(self._request_counts),
                "last_error": dict(self._last_session_error),
                "execution_gate_managed": execution_gate["managed"],
                "execution_gate_armed": execution_gate["armed"],
                "execution_gate_connection_generation": execution_gate[
                    "connection_generation"
                ],
                "execution_gate_instrument": execution_gate["instrument"],
                "execution_gate_proof_sha256": execution_gate["proof_sha256"],
                "execution_gate_revocation_reason": execution_gate["revocation_reason"],
            }

    def get_request_counts(self) -> dict[str, int]:
        """Return a read-only snapshot of native requests issued this session."""
        with self._query_state_lock:
            return dict(self._request_counts)

    def start(self, block=False):
        """启动连接（默认后台运行）"""
        _check_native_module()
        with self._query_state_lock:
            self._revoke_execution_gate_locked("ctp_execution_gate_client_start")
            if self._api is not None:
                raise RuntimeError("ctp_trader_client_already_started")
        flow = _flow_dir(f"td_{self.broker_id}_{self.user_id}")
        api = CThostFtdcTraderApi.CreateFtdcTraderApi(flow)
        with self._query_state_lock:
            self._api = api
            spi = _TraderSpi(self, api)
            self._spi = spi
        try:
            api.RegisterSpi(spi)
            api.SubscribePrivateTopic(2)
            api.SubscribePublicTopic(2)
            api.RegisterFront(self.front)
            api.Init()
        except Exception:
            with self._query_state_lock:
                if self._api is api:
                    self._api = None
            with suppress(Exception):
                api.RegisterSpi(None)
                api.Release()
            raise

        if block:
            try:
                api.Join()
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()
        else:
            thread = threading.Thread(target=api.Join, daemon=True)
            with self._query_state_lock:
                self._thread = thread
            thread.start()

    def wait_ready(self, timeout=15):
        """Wait for login, direct confirmation and matching server readback."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_ready:
                return True
            with self._query_state_lock:
                should_read_back = (
                    self.auto_settlement_confirm
                    and self.is_read_only_ready
                    and self._settlement_state == "confirmed"
                    and self._settlement_proof_source == "direct_confirmation"
                    and not self._has_current_settlement_readback_locked()
                )
            if should_read_back:
                remaining = max(0.0, deadline - time.time())
                self.verify_settlement_confirmation(timeout=min(5.0, remaining))
                continue
            time.sleep(min(0.2, max(0.0, deadline - time.time())))
        return self.is_ready

    def _new_query_accumulator(self, request_type: str) -> _QueryAccumulator:
        request_id = self._next_request_id()
        accumulator = _QueryAccumulator(
            request_type=request_type,
            request_id=request_id,
            connection_generation=self._connection_generation,
            account_fingerprint=self._account_fingerprint,
            started_at_utc=datetime.now(timezone.utc),
        )
        with self._query_state_lock:
            self._query_history[request_id] = accumulator
            while len(self._query_history) > 256:
                oldest_request_id = next(iter(self._query_history))
                oldest = self._query_history[oldest_request_id]
                if not oldest.sealed:
                    break
                self._query_history.pop(oldest_request_id, None)
        return accumulator

    def _local_query_failure(
        self,
        request_type: str,
        message: str,
        *,
        unsupported: bool = False,
    ) -> QueryResult[Any]:
        accumulator = self._new_query_accumulator(request_type)
        accumulator.error_code = -3 if unsupported else -1
        accumulator.error_message = message
        accumulator.unsupported = unsupported
        accumulator.completed_at_utc = datetime.now(timezone.utc)
        accumulator.sealed = True
        accumulator.event.set()
        return accumulator.result()

    def _handle_query_callback(
        self,
        request_type: str,
        record: Any,
        rsp_info: Any,
        request_id: int,
        is_last: bool,
    ) -> None:
        error_code, error_message = _rsp_error(rsp_info)
        with self._query_state_lock:
            accumulator = self._query_history.get(int(request_id))
            if accumulator is None or accumulator.request_type != request_type:
                self._orphan_query_callbacks.append(
                    {
                        "request_type": request_type,
                        "request_id": request_id,
                        "connection_generation": self._connection_generation,
                        "is_last": bool(is_last),
                        "error_code": error_code,
                    }
                )
                del self._orphan_query_callbacks[:-256]
                return
            same_generation = (
                accumulator.connection_generation == self._connection_generation
            )
            if not same_generation:
                accumulator.late_callback_count += 1
                self._orphan_query_callbacks.append(
                    {
                        "request_type": request_type,
                        "request_id": request_id,
                        "connection_generation": self._connection_generation,
                        "is_last": bool(is_last),
                        "error_code": error_code,
                    }
                )
                del self._orphan_query_callbacks[:-256]
                return
            if (
                accumulator.sealed
                or accumulator.is_last_seen
                or accumulator.error_code not in (None, 0)
            ):
                accumulator.late_callback_count += 1
                return
            if record is not None:
                snapshot = _snapshot_query_record(record)
                if request_type == "instruments":
                    # InstrumentField proves ExpireDate but carries neither an
                    # exchange trading calendar nor a prior-day market ranking.
                    snapshot.setdefault(
                        "expiry_date", snapshot.get("ExpireDate") or None
                    )
                    snapshot.setdefault("trading_days_to_expiry", None)
                    snapshot.setdefault("remaining_trading_days", None)
                    snapshot.setdefault("trading_calendar_evidence_complete", False)
                    snapshot.setdefault("ranking_trading_day", None)
                    snapshot.setdefault("prior_trading_day_volume", None)
                    snapshot.setdefault("prior_trading_day_open_interest", None)
                    snapshot.setdefault("prior_day_ranking_evidence_complete", False)
                accumulator.records.append(snapshot)
            if error_code not in (None, 0):
                accumulator.error_code = error_code
                accumulator.error_message = error_message
            if is_last:
                accumulator.is_last_seen = True
            if is_last or error_code not in (None, 0):
                accumulator.event.set()

    def _handle_query_error(
        self, rsp_info: Any, request_id: int, is_last: bool
    ) -> None:
        with self._query_state_lock:
            accumulator = self._query_history.get(int(request_id))
            request_type = (
                accumulator.request_type if accumulator is not None else "unknown"
            )
        self._handle_query_callback(request_type, None, rsp_info, request_id, is_last)

    def _execute_query(
        self,
        request_type: str,
        submit: Callable[[int], Any] | None,
        timeout: float,
    ) -> QueryResult[Any]:
        if not self.is_read_only_ready:
            return self._local_query_failure(request_type, "trader_not_logged_in")
        if not callable(submit):
            return self._local_query_failure(
                request_type,
                "native_query_unsupported",
                unsupported=True,
            )
        with self._query_lock:
            elapsed = time.monotonic() - self._last_query_submitted_at
            wait_time = self._query_interval - elapsed
            if wait_time > 0:
                time.sleep(wait_time)
            accumulator = self._new_query_accumulator(request_type)
            self._record_request(f"query_{request_type}")
            try:
                ret = submit(accumulator.request_id)
            except Exception as exc:
                accumulator.error_code = -4
                accumulator.error_message = (
                    f"query_submit_exception:{type(exc).__name__}"
                )
                accumulator.completed_at_utc = datetime.now(timezone.utc)
                accumulator.sealed = True
                accumulator.event.set()
                return accumulator.result()
            self._last_query_submitted_at = time.monotonic()
            accumulator.submit_code = None if ret is None else int(ret)
            if ret not in (None, 0):
                accumulator.error_code = int(ret)
                accumulator.error_message = "query_submit_rejected"
                accumulator.completed_at_utc = datetime.now(timezone.utc)
                accumulator.sealed = True
                accumulator.event.set()
                return accumulator.result()
            observed = accumulator.event.wait(max(float(timeout), 0.0))
            with self._query_state_lock:
                # If the terminal callback acquired the state lock at the
                # timeout boundary, it wins the race and remains complete.
                if (
                    not observed
                    and not accumulator.is_last_seen
                    and accumulator.error_code in (None, 0)
                ):
                    accumulator.timed_out = True
                    accumulator.error_message = "query_timeout"
                accumulator.completed_at_utc = datetime.now(timezone.utc)
                accumulator.sealed = True
                accumulator.event.set()
                return accumulator.result()

    def get_query_result(self, request_id: int) -> QueryResult[Any] | None:
        """Return current evidence, including callbacks arriving after timeout."""
        with self._query_state_lock:
            accumulator = self._query_history.get(int(request_id))
            return accumulator.result() if accumulator is not None else None

    def query_account_result(self, timeout=5) -> QueryResult[Any]:
        field = CThostFtdcQryTradingAccountField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        method = getattr(self._api, "ReqQryTradingAccount", None) if self._api else None
        return self._execute_query(
            "account",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_account(self, timeout=5):
        """Compatibility view; incomplete queries return ``None``."""
        return self.query_account_result(timeout=timeout).first

    def query_positions_result(self, timeout=5) -> QueryResult[Any]:
        field = CThostFtdcQryInvestorPositionField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        method = (
            getattr(self._api, "ReqQryInvestorPosition", None) if self._api else None
        )
        return self._execute_query(
            "positions",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_positions(self, timeout=5):
        result = self.query_positions_result(timeout=timeout)
        return list(result.records) if result.complete else []

    def query_orders_result(
        self, instrument_id="", exchange_id="", order_sys_id="", timeout=5
    ) -> QueryResult[Any]:
        field = CThostFtdcQryOrderField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        if instrument_id:
            field.InstrumentID = str(instrument_id)
        if exchange_id:
            field.ExchangeID = str(exchange_id)
        if order_sys_id:
            field.OrderSysID = str(order_sys_id)
        method = getattr(self._api, "ReqQryOrder", None) if self._api else None
        return self._execute_query(
            "orders",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_orders(
        self, instrument_id="", exchange_id="", order_sys_id="", timeout=5
    ):
        result = self.query_orders_result(
            instrument_id=instrument_id,
            exchange_id=exchange_id,
            order_sys_id=order_sys_id,
            timeout=timeout,
        )
        return list(result.records) if result.complete else []

    def query_trades_result(
        self,
        instrument_id="",
        exchange_id="",
        trade_id="",
        start_time="",
        end_time="",
        timeout=5,
    ) -> QueryResult[Any]:
        field = CThostFtdcQryTradeField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        for name, value in (
            ("InstrumentID", instrument_id),
            ("ExchangeID", exchange_id),
            ("TradeID", trade_id),
            ("TradeTimeStart", start_time),
            ("TradeTimeEnd", end_time),
        ):
            if value:
                try:
                    setattr(field, name, str(value))
                except (AttributeError, TypeError, ValueError):
                    return self._local_query_failure(
                        "trades",
                        f"native_trade_filter_unsupported:{name}",
                        unsupported=True,
                    )
        method = getattr(self._api, "ReqQryTrade", None) if self._api else None
        return self._execute_query(
            "trades",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_trades(self, **kwargs: Any) -> list[Any]:
        result = self.query_trades_result(**kwargs)
        return list(result.records) if result.complete else []

    def query_instruments_result(
        self, instrument_id="", exchange_id="", timeout=5
    ) -> QueryResult[Any]:
        field = CThostFtdcQryInstrumentField()
        field.InstrumentID = str(instrument_id or "")
        if exchange_id:
            field.ExchangeID = str(exchange_id)
        method = getattr(self._api, "ReqQryInstrument", None) if self._api else None
        return self._execute_query(
            "instruments",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_instrument(self, instrument_id, exchange_id="", timeout=5):
        return self.query_instruments_result(
            instrument_id=instrument_id, exchange_id=exchange_id, timeout=timeout
        ).first

    def query_instrument_margin_rate_result(
        self, instrument_id, exchange_id="", hedge_flag="1", timeout=5
    ) -> QueryResult[Any]:
        field = CThostFtdcQryInstrumentMarginRateField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        field.InstrumentID = str(instrument_id or "")
        if exchange_id:
            field.ExchangeID = str(exchange_id)
        if hedge_flag:
            field.HedgeFlag = str(hedge_flag)
        method = (
            getattr(self._api, "ReqQryInstrumentMarginRate", None)
            if self._api
            else None
        )
        return self._execute_query(
            "margin_rate",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_instrument_margin_rate(
        self, instrument_id, exchange_id="", hedge_flag="1", timeout=5
    ):
        return self.query_instrument_margin_rate_result(
            instrument_id,
            exchange_id=exchange_id,
            hedge_flag=hedge_flag,
            timeout=timeout,
        ).first

    def query_instrument_commission_rate_result(
        self, instrument_id, exchange_id="", timeout=5
    ) -> QueryResult[Any]:
        field = CThostFtdcQryInstrumentCommissionRateField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        field.InstrumentID = str(instrument_id or "")
        if exchange_id:
            field.ExchangeID = str(exchange_id)
        method = (
            getattr(self._api, "ReqQryInstrumentCommissionRate", None)
            if self._api
            else None
        )
        return self._execute_query(
            "commission_rate",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_instrument_commission_rate(
        self, instrument_id, exchange_id="", timeout=5
    ):
        return self.query_instrument_commission_rate_result(
            instrument_id, exchange_id=exchange_id, timeout=timeout
        ).first

    def query_settlement_confirmation_result(self, timeout=5) -> QueryResult[Any]:
        field = CThostFtdcQrySettlementInfoConfirmField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        method = (
            getattr(self._api, "ReqQrySettlementInfoConfirm", None)
            if self._api
            else None
        )
        return self._execute_query(
            "settlement_confirmation",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def verify_settlement_confirmation(self, timeout=5) -> QueryResult[Any]:
        """Read server confirmation and promote this matching session to trading ready."""
        with self._query_state_lock:
            expected_generation = self._connection_generation
            expected_fingerprint = self._account_fingerprint
            expected_trading_day = self._trading_day
            self._clear_settlement_readback_locked(
                "ctp_execution_gate_settlement_readback_refresh"
            )
        result = self.query_settlement_confirmation_result(timeout=timeout)
        failure_reason = ""
        if not result.complete:
            failure_reason = "ctp_execution_gate_settlement_readback_incomplete"
        elif result.connection_generation != expected_generation:
            failure_reason = "ctp_execution_gate_settlement_readback_stale_generation"
        elif result.account_fingerprint != expected_fingerprint:
            failure_reason = "ctp_execution_gate_settlement_readback_wrong_account"

        matched = False
        for record in result.records if not failure_reason else ():
            if isinstance(record, dict):
                getter = record.get
            else:

                def getter(name: str, default: Any = "") -> Any:
                    return getattr(record, name, default)

            broker_id = str(getter("BrokerID", "") or "")
            investor_id = str(getter("InvestorID", "") or "")
            confirmation_day = str(
                getter("TradingDay", "") or getter("ConfirmDate", "") or ""
            )
            if broker_id != self.broker_id:
                continue
            if investor_id != self.user_id:
                continue
            if not confirmation_day or confirmation_day != expected_trading_day:
                continue
            matched = True
            break

        if not failure_reason and not matched:
            failure_reason = "ctp_execution_gate_settlement_readback_identity_mismatch"

        with self._query_state_lock:
            if not failure_reason and (
                self._connection_generation != expected_generation
                or self._account_fingerprint != expected_fingerprint
                or self._trading_day != expected_trading_day
            ):
                failure_reason = (
                    "ctp_execution_gate_settlement_readback_stale_generation"
                )
            if not failure_reason and not self.is_read_only_ready:
                failure_reason = (
                    "ctp_execution_gate_settlement_readback_session_not_ready"
                )
            if failure_reason:
                self._clear_settlement_readback_locked(
                    failure_reason,
                    request_id=result.request_id,
                )
            else:
                self._settlement_state = "confirmed"
                self._settlement_trading_day = expected_trading_day
                self._settlement_connection_generation = expected_generation
                self._settlement_account_fingerprint = expected_fingerprint
                self._settlement_proof_source = "confirmation_query"
                self._settlement_proof_query_request_id = result.request_id
                self._settlement_readback_verified = True
                self._ready = True
                self._last_session_error = {}
        return result

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

    def _push_error_event(
        self, event_type, rsp_info=None, field=None, request_id=None
    ) -> None:
        payload = {
            "event": event_type,
            "request_id": request_id,
            "error_id": getattr(rsp_info, "ErrorID", 0) if rsp_info is not None else 0,
            "error_msg": (
                getattr(rsp_info, "ErrorMsg", "") if rsp_info is not None else ""
            ),
            "field": _snapshot_ctp_field(field),
        }
        self._error_events.put(payload)
        if self.on_error and rsp_info is not None:
            self.on_error(rsp_info)

    @property
    def api(self):
        """Return the stable public view of the current native trader API."""
        return self._api_view

    def stop(self):
        """停止并释放资源

        macOS 上 CTP C++ API 的 Release() 在 Join() 仍然运行于
        另一个线程时会触发 segfault。因此:
        - 非阻塞模式 (daemon thread): 仅置空引用，让 daemon 线程随进程退出
        - 阻塞模式 (Join 已返回): 安全调用 Release()
        """
        with self._query_state_lock:
            self._on_front_disconnected("client_stop")
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
        if self.auto_settlement_confirm:
            return self.is_trading_ready
        return self.is_read_only_ready

    @property
    def is_read_only_ready(self):
        return (
            self._connected
            and self._authentication_state == "authenticated"
            and self._login_state == "logged_in"
        )

    @property
    def is_trading_ready(self):
        with self._query_state_lock:
            return (
                self.is_read_only_ready
                and self._settlement_state == "confirmed"
                and self._ready
                and self._has_current_settlement_readback_locked()
            )
