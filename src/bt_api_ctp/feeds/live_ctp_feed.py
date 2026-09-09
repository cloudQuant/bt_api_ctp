from __future__ import annotations

import os
import threading
import time
import warnings
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from math import isfinite
from sys import float_info
from typing import Any

from bt_api_base.containers.orders.order import OrderStatus
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
from bt_api_ctp.ctp_env_selector import (
    official_simnow_fronts,
    select_ctp_environment,
    verify_official_simnow_profile,
)
from bt_api_ctp.exchange_data import CtpExchangeDataFuture
from bt_api_ctp.feeds.base_stream import BaseDataStream, ConnectionState
from bt_api_ctp.instrument import (
    build_ctp_instrument_spec,
    ctp_instrument_evidence_errors,
    ctp_query_bundle_errors,
)
from bt_api_ctp.query import QueryResult

CTP_OFFSET_FLAG = {
    "open": "0",
    "close": "1",
    "force_close": "2",
    "close_today": "3",
    "close_yesterday": "4",
    "force_close_yesterday": "5",
    "local_force_close": "6",
}
CTP_DIRECTION_FLAG = {"buy": "0", "sell": "1"}
_ctp_field_logger = get_logger("ctp_field_converter")


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _query_evidence(result: QueryResult[Any]) -> dict[str, Any]:
    evidence = result.as_dict(include_records=False)
    return {
        "query_result": evidence,
        "query_complete": result.complete,
        "evidence_complete": result.complete,
        "query_request_id": result.request_id,
        "query_connection_generation": result.connection_generation,
        "query_is_last_seen": result.is_last_seen,
        "query_error_code": result.error_code,
        "query_error_message": result.error_message,
        "query_timed_out": result.timed_out,
        "query_unsupported": result.unsupported,
    }


def _positive_int_lot(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"CTP order {field_name} must be a positive integer lot.")
    try:
        lot = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"CTP order {field_name} must be a positive integer lot."
        ) from exc
    if not lot.is_finite() or lot <= 0 or lot != lot.to_integral_value():
        raise ValueError(f"CTP order {field_name} must be a positive integer lot.")
    return int(lot)


_CTP_INVALID_FLOAT_THRESHOLD = Decimal(str(float_info.max)) / 2
_CTP_QUOTE_INVALID_FLOAT_THRESHOLD = float_info.max / 2


def _positive_ctp_price(value: Any, field_name: str = "price") -> float:
    """Reject non-finite values and CTP's DBL_MAX missing-value sentinel."""
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(
            f"CTP order {field_name} must be a positive price with a finite value."
        )
    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"CTP order {field_name} must be a positive price with a finite value."
        ) from exc
    if (
        not price.is_finite()
        or price <= 0
        or abs(price) >= _CTP_INVALID_FLOAT_THRESHOLD
    ):
        raise ValueError(
            f"CTP order {field_name} must be a positive price with a finite value."
        )
    return float(price)


def _append_quote_quality_flag(row: CtpTickerData, flag: str) -> None:
    if flag not in row.quality_flags:
        row.quality_flags.append(flag)


def _valid_ctp_quote_number(value: Any, *, positive: bool = False) -> bool:
    if isinstance(value, bool) or value in (None, ""):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if not isfinite(number) or abs(number) >= _CTP_QUOTE_INVALID_FLOAT_THRESHOLD:
        return False
    return number > 0 if positive else number >= 0


def _raw_quote_value(row: CtpTickerData, field_name: str, fallback: Any) -> Any:
    raw = getattr(row, "ticker_info", None)
    if isinstance(raw, dict):
        return raw.get(field_name)
    return fallback


def _validate_ctp_quote(row: CtpTickerData) -> bool:
    """Flag malformed required fields before they can update trusted state."""
    row.init_data()
    valid = True
    for field_name, raw_name, value in (
        ("LAST_PRICE", "LastPrice", row.get_last_price()),
        ("BID_PRICE", "BidPrice1", row.get_bid_price()),
        ("ASK_PRICE", "AskPrice1", row.get_ask_price()),
    ):
        if not _valid_ctp_quote_number(
            _raw_quote_value(row, raw_name, value), positive=True
        ):
            _append_quote_quality_flag(row, f"INVALID_{field_name}")
            valid = False
    for field_name, raw_name, value in (
        ("BID_VOLUME", "BidVolume1", row.get_bid_volume()),
        ("ASK_VOLUME", "AskVolume1", row.get_ask_volume()),
        ("CUM_VOLUME", "Volume", row.get_cumulative_volume()),
        ("OPEN_INTEREST", "OpenInterest", row.get_open_interest()),
    ):
        if not _valid_ctp_quote_number(_raw_quote_value(row, raw_name, value)):
            _append_quote_quality_flag(row, f"INVALID_{field_name}")
            valid = False
    bid = row.get_bid_price()
    ask = row.get_ask_price()
    if _valid_ctp_quote_number(bid, positive=True) and _valid_ctp_quote_number(
        ask, positive=True
    ):
        if float(bid) > float(ask):
            _append_quote_quality_flag(row, "CROSSED_BOOK")
            valid = False
        elif float(bid) == float(ask):
            _append_quote_quality_flag(row, "LOCKED_BOOK")
    if row.get_bid_volume() == 0 or row.get_ask_volume() == 0:
        _append_quote_quality_flag(row, "ZERO_DEPTH")
    return valid


def _safe_ctp_quote_number(value: Any) -> float:
    """Return a finite transport value while retaining invalidity in quality flags."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not isfinite(number) or abs(number) >= _CTP_QUOTE_INVALID_FLOAT_THRESHOLD:
        return 0.0
    return number


def _resolve_ctp_runtime_kwargs(kwargs: dict[str, Any]) -> tuple[dict[str, Any], str]:
    resolved = dict(kwargs)
    broker_id = str(
        resolved.get("broker_id") or os.environ.get("CTP_BROKER_ID") or ""
    ).strip()
    user_id = str(
        resolved.get("user_id")
        or resolved.get("investor_id")
        or os.environ.get("CTP_USER_ID")
        or ""
    ).strip()
    password = str(
        resolved.get("password") or os.environ.get("CTP_PASSWORD") or ""
    ).strip()
    auth_code = str(
        resolved.get("auth_code")
        or os.environ.get("CTP_AUTH_CODE")
        or "0000000000000000"
    ).strip()
    app_id = str(
        resolved.get("app_id") or os.environ.get("CTP_APP_ID") or "simnow_client_test"
    ).strip()
    td_front = str(resolved.get("td_front") or resolved.get("td_address") or "").strip()
    md_front = str(resolved.get("md_front") or resolved.get("md_address") or "").strip()
    had_partial_explicit_front = bool(td_front) != bool(md_front)
    claimed_profile = (
        str(resolved.get("ctp_env_profile") or resolved.get("ctp_profile") or "")
        .strip()
        .lower()
    )
    if claimed_profile and bool(td_front) != bool(md_front):
        raise ValueError(
            "claimed CTP profile requires both td_front and md_front, or neither"
        )
    if claimed_profile and not td_front and not md_front:
        td_front, md_front = official_simnow_fronts(claimed_profile)
    required_profile = (
        str(
            resolved.get("require_ctp_profile")
            or resolved.get("ctp_required_profile")
            or ""
        )
        .strip()
        .lower()
    )
    if required_profile and (td_front or md_front) and not claimed_profile:
        raise RuntimeError(
            "required CTP profile cannot be proven from explicit fronts without "
            "ctp_env_profile"
        )
    if claimed_profile and required_profile:
        required_matches = (
            claimed_profile == required_profile
            or (required_profile == "set1" and claimed_profile.startswith("set1_"))
            or (required_profile == "set2" and claimed_profile.startswith("set2_"))
        )
        if not required_matches:
            raise RuntimeError(
                f"required CTP profile {required_profile!r}, configured {claimed_profile!r}"
            )
    env_name = ""
    selected_environment = "custom"
    env_readiness = "explicit_front_override" if td_front and md_front else "unknown"
    if not td_front or not md_front:
        static_td = str(os.environ.get("CTP_TD_FRONT") or "").strip()
        static_md = str(os.environ.get("CTP_MD_FRONT") or "").strip()
        if os.environ.get("CTP_ENV") or not static_td or not static_md:
            selection = select_ctp_environment(require_profile=required_profile)
            td_front = td_front or selection.td_front
            md_front = md_front or selection.md_front
            env_name = selection.profile
            env_readiness = selection.readiness
            selected_environment = selection.environment
        else:
            td_front = td_front or static_td
            md_front = md_front or static_md
            env_name = "custom_front_override"
            env_readiness = "explicit_front_override"
        if had_partial_explicit_front:
            env_name = "mixed_front_override"
            env_readiness = "mixed_front_unverified"
            selected_environment = "custom"
    elif claimed_profile:
        if not verify_official_simnow_profile(td_front, md_front, claimed_profile):
            raise ValueError(
                f"claimed CTP profile {claimed_profile!r} does not match its official fronts"
            )
        env_name = claimed_profile
        env_readiness = "explicit_official_pair"
        selected_environment = "simnow"
    else:
        env_name = "custom_front_override"
    resolved.update(
        {
            "broker_id": broker_id,
            "user_id": user_id,
            "password": password,
            "auth_code": auth_code,
            "app_id": app_id,
            "td_front": td_front,
            "md_front": md_front,
            "ctp_environment": selected_environment,
            "ctp_env_profile": env_name,
            "ctp_env_readiness": env_readiness,
        }
    )
    return resolved, env_name


def _sanitize_ctp_field_value(value):
    if isinstance(value, str):
        return value.encode("gbk", errors="ignore").decode("gbk", errors="ignore")
    return value


def _ctp_field_to_dict(field):
    if field is None:
        return {}
    result = {}
    for attr in dir(field):
        if attr.startswith("_") or attr in {"this", "thisown"}:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UnicodeWarning)
                value = getattr(field, attr)
            if not callable(value):
                result[attr] = _sanitize_ctp_field_value(value)
        except Exception as exc:
            _ctp_field_logger.debug(
                f"Failed to access CTP field attribute '{attr}': {exc}", exc_info=False
            )
    return result


class CtpVolumeDeltaTracker:
    """The single SDK authority for CTP cumulative-volume conversion."""

    def __init__(self) -> None:
        self._state: dict[tuple[str, str], dict[str, Any]] = {}

    def apply(self, row: CtpTickerData) -> CtpTickerData:
        row.init_data()
        instrument = row.get_symbol_name() or ""
        key = (str(row.exchange_id or ""), instrument)
        total = float(row.get_cumulative_volume() or 0.0)
        scope = (
            int(row.connection_generation or 0),
            str(row.trading_day or ""),
        )
        previous = self._state.get(key)
        delta = 0.0
        complete = False
        quality = "BASELINE"
        if previous is not None:
            if previous["scope"] != scope:
                quality = "CONNECTION_OR_TRADING_DAY_RESET"
            elif row.ingest_seq and row.ingest_seq <= previous["ingest_seq"]:
                quality = "OUT_OF_ORDER"
                row.quality_flags.append("OUT_OF_ORDER")
            elif total < previous["cum_volume"]:
                quality = "VOLUME_RESET"
                row.quality_flags.append("VOLUME_GAP")
            else:
                delta = total - previous["cum_volume"]
                complete = True
                quality = "CONTINUOUS"
        if quality != "OUT_OF_ORDER":
            self._state[key] = {
                "scope": scope,
                "cum_volume": total,
                "ingest_seq": int(row.ingest_seq or 0),
            }
        return row.apply_volume_delta(delta, complete=complete, quality=quality)


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
            Capability.GET_EXCHANGE_INFO,
            Capability.MARKET_STREAM,
            Capability.ACCOUNT_STREAM,
        }

    def __init__(self, data_queue: Any = None, **kwargs: Any) -> None:
        super().__init__(data_queue)
        resolved_kwargs, self.ctp_env_name = _resolve_ctp_runtime_kwargs(kwargs)
        self.ctp_env_profile = resolved_kwargs.get("ctp_env_profile", self.ctp_env_name)
        self.ctp_env_readiness = resolved_kwargs.get("ctp_env_readiness", "unknown")
        self.ctp_environment = resolved_kwargs.get("ctp_environment", "custom")
        self.data_queue = data_queue
        self.broker_id = resolved_kwargs.get("broker_id", "")
        self.user_id = resolved_kwargs.get("user_id", "")
        self.password = resolved_kwargs.get("password", "")
        self.auth_code = resolved_kwargs.get("auth_code", "0000000000000000")
        self.app_id = resolved_kwargs.get("app_id", "simnow_client_test")
        self.td_front = resolved_kwargs.get("td_front", "")
        self.md_front = resolved_kwargs.get("md_front", "")
        self.asset_type = resolved_kwargs.get("asset_type", "FUTURE")
        self.exchange_name = "CTP"
        self._params = CtpExchangeDataFuture()
        self.request_logger = get_logger("ctp_feed")
        self._trader = None
        self._connect_lock = threading.Lock()
        self._connected = False
        self._connect_timeout = resolved_kwargs.get("connect_timeout", 15)
        self._execution_gate_capability: object | None = None
        self.auto_settlement_confirm = _as_bool(
            resolved_kwargs.get("auto_settlement_confirm"), default=True
        )

    def translate_error(self, raw_response):
        if isinstance(raw_response, dict) and raw_response.get("ErrorID", 0) != 0:
            return raw_response
        return None

    def configure_execution_gate(self, capability: object) -> dict[str, Any]:
        """Install the SDK-owned gate without changing legacy feed behavior."""

        if capability is None:
            raise ctp_client.CtpExecutionGateError(
                "ctp_execution_gate_capability_required"
            )
        with self._connect_lock:
            installed = self._execution_gate_capability
            if installed is not None and installed is not capability:
                raise ctp_client.CtpExecutionGateError(
                    "ctp_execution_gate_already_configured"
                )
            self._execution_gate_capability = capability
            trader = self._trader
            if trader is not None:
                method = getattr(trader, "configure_execution_gate", None)
                if not callable(method):
                    raise ctp_client.CtpExecutionGateError(
                        "ctp_execution_gate_native_contract_unavailable"
                    )
                return dict(method(capability))
        return self.get_execution_gate_state()

    def arm_execution_gate(
        self,
        capability: object,
        proof: Any,
    ) -> dict[str, Any]:
        """Arm the existing native session for one proof-bound instrument."""

        with self._connect_lock:
            if capability is not self._execution_gate_capability:
                raise ctp_client.CtpExecutionGateError(
                    "ctp_execution_gate_capability_mismatch"
                )
            trader = self._trader
            method = getattr(trader, "arm_execution_gate", None)
            if trader is None or not callable(method):
                raise ctp_client.CtpExecutionGateError(
                    "ctp_execution_gate_native_contract_unavailable"
                )
            return dict(
                method(
                    capability,
                    proof,
                    environment_profile=self.ctp_env_profile,
                )
            )

    def disarm_execution_gate(
        self,
        capability: object,
        reason: str = "execution_arm_revoked",
    ) -> dict[str, Any]:
        """Idempotently revoke managed writes while retaining read access."""

        with self._connect_lock:
            if capability is not self._execution_gate_capability:
                raise ctp_client.CtpExecutionGateError(
                    "ctp_execution_gate_capability_mismatch"
                )
            trader = self._trader
            if trader is None:
                return self.get_execution_gate_state()
            method = getattr(trader, "disarm_execution_gate", None)
            if not callable(method):
                raise ctp_client.CtpExecutionGateError(
                    "ctp_execution_gate_native_contract_unavailable"
                )
            return dict(method(capability, reason))

    def get_execution_gate_state(self) -> dict[str, Any]:
        """Return gate evidence without returning its opaque capability."""

        trader = self._trader
        method = getattr(trader, "get_execution_gate_state", None)
        if callable(method):
            return dict(method())
        return {
            "managed": self._execution_gate_capability is not None,
            "armed": False,
            "connection_generation": None,
            "trading_day": None,
            "instrument": None,
            "environment_profile": None,
            "proof_sha256": None,
            "revocation_reason": None,
        }

    def _ensure_execution_permitted(
        self,
        capability: object | None,
        symbol: Any,
        exchange_id: Any = None,
    ) -> None:
        if self._execution_gate_capability is None:
            return
        trader = self._trader
        method = getattr(trader, "require_execution_write", None)
        state_reader = getattr(trader, "get_execution_gate_state", None)
        if trader is None or not callable(method) or not callable(state_reader):
            raise ctp_client.CtpExecutionGateError(
                "ctp_execution_gate_native_contract_unavailable"
            )
        state = state_reader()
        if not isinstance(state, dict) or state.get("managed") is not True:
            raise ctp_client.CtpExecutionGateError(
                "ctp_execution_gate_native_contract_unavailable"
            )
        method(capability, symbol, exchange_id)

    def _ensure_connected(self):
        if self._trader is None or not self._trader.is_read_only_ready:
            self.connect()
        if not self._trader or not self._trader.is_read_only_ready:
            raise BtConnectionError(
                "CTP", "TraderClient not read-only ready after connect()"
            )

    def _ensure_trading_ready(self):
        self._ensure_connected()
        if not self._trader or not self._trader.is_trading_ready:
            raise BtConnectionError(
                "CTP",
                "TraderClient settlement is unconfirmed; order writes are disabled",
            )

    def connect(self):
        with self._connect_lock:
            trader = self._trader
            if trader is None:
                trader = ctp_client.TraderClient(
                    self.td_front,
                    self.broker_id,
                    self.user_id,
                    self.password,
                    app_id=self.app_id,
                    auth_code=self.auth_code,
                    auto_settlement_confirm=self.auto_settlement_confirm,
                )
                if self._execution_gate_capability is not None:
                    trader.configure_execution_gate(self._execution_gate_capability)
                self._trader = trader
                trader.start(block=False)
            if getattr(trader, "is_read_only_ready", False):
                self._connected = True
                return
            wait_ready = getattr(trader, "wait_ready", None)
            self._connected = bool(
                callable(wait_ready) and wait_ready(timeout=self._connect_timeout)
            )

    def disconnect(self):
        with self._connect_lock:
            trader = self._trader
            self._trader = None
            capability = self._execution_gate_capability
        if trader is not None:
            if capability is not None:
                with suppress(Exception):
                    trader.disarm_execution_gate(
                        capability, "ctp_execution_gate_feed_disconnected"
                    )
            trader.stop()
        self._connected = False

    def _make_request_data(
        self, data_list, request_type, symbol_name=None, extra_data=None, status=True
    ):
        payload = extra_data.copy() if extra_data else {}
        payload.setdefault("exchange_name", self.exchange_name)
        payload.setdefault("symbol_name", symbol_name or "")
        payload.setdefault("asset_type", self.asset_type)
        payload.setdefault("request_type", request_type)
        request = RequestData(data_list, payload, status=status)
        request.data = data_list
        request.has_been_init_data = True
        return request

    def get_account(self, symbol=None, extra_data=None, **kwargs):
        self._ensure_connected()
        trader = self._trader
        if trader is None:
            return self._make_request_data(
                [], "get_account", symbol, extra_data, status=False
            )
        result = trader.query_account_result(timeout=kwargs.get("timeout", 5))
        payload = dict(extra_data or {})
        payload.update(_query_evidence(result))
        rows = [
            CtpAccountData(_ctp_field_to_dict(raw), symbol, self.asset_type, True)
            for raw in result.records
        ]
        snapshot_complete = result.complete and len(rows) == 1
        payload["account_snapshot_complete"] = snapshot_complete
        if result.complete and not rows:
            payload["account_snapshot_error"] = "complete_query_returned_no_account"
        elif result.complete and len(rows) > 1:
            payload["account_snapshot_error"] = (
                "complete_query_returned_multiple_accounts"
            )
        return self._make_request_data(
            rows if snapshot_complete else [],
            "get_account",
            symbol,
            payload,
            status=snapshot_complete,
        )

    def get_balance(self, symbol=None, extra_data=None, **kwargs):
        return self.get_account(symbol, extra_data, **kwargs)

    def get_position(self, symbol=None, extra_data=None, **kwargs):
        self._ensure_connected()
        trader = self._trader
        if trader is None:
            return self._make_request_data(
                [], "get_position", symbol, extra_data, status=False
            )
        result = trader.query_positions_result(timeout=kwargs.get("timeout", 5))
        payload = dict(extra_data or {})
        payload.update(_query_evidence(result))
        rows = []
        for raw in result.records:
            data = _ctp_field_to_dict(raw)
            rows.append(
                CtpPositionData(
                    data, data.get("InstrumentID", symbol), self.asset_type, True
                )
            )
        return self._make_request_data(
            rows if result.complete else [],
            "get_position",
            symbol,
            payload,
            status=result.complete,
        )

    def get_tick(self, symbol, extra_data=None, **kwargs):
        return self._make_request_data([], "get_tick", symbol, extra_data, status=False)

    def get_depth(self, symbol, count=5, extra_data=None, **kwargs):
        return self._make_request_data(
            [], "get_depth", symbol, extra_data, status=False
        )

    def get_kline(self, symbol, period, count=100, extra_data=None, **kwargs):
        return self._make_request_data(
            [], "get_kline", symbol, extra_data, status=False
        )

    def make_order(
        self,
        symbol,
        volume,
        price=None,
        order_type="buy-limit",
        offset="open",
        post_only=False,
        client_order_id=None,
        extra_data=None,
        **kwargs,
    ):
        execution_capability = kwargs.pop("_execution_capability", None)
        exchange_id = kwargs.get("exchange_id", "")
        self._ensure_execution_permitted(execution_capability, symbol, exchange_id)
        self._ensure_trading_ready()
        trader = self._trader
        if trader is None:
            return self._make_request_data(
                [], "make_order", symbol, extra_data, status=False
            )
        self._ensure_execution_permitted(execution_capability, symbol, exchange_id)
        try:
            side, order_kind = str(order_type or "").lower().split("-", 1)
        except ValueError as exc:
            raise ValueError(
                f"CTP order_type must be '<buy|sell>-limit' (got {order_type!r})."
            ) from exc
        if side not in CTP_DIRECTION_FLAG:
            raise ValueError(f"CTP order side must be buy or sell (got {side!r}).")
        if order_kind != "limit":
            raise ValueError(
                f"CTP order kind {order_kind!r} is unsupported; use a positive-price limit order."
            )
        offset_text = str(offset or "open").lower()
        if offset_text not in CTP_OFFSET_FLAG:
            raise ValueError(f"CTP order offset {offset!r} is unsupported.")
        order_volume = _positive_int_lot(volume, "volume")
        direction = CTP_DIRECTION_FLAG[side]
        offset_flag = CTP_OFFSET_FLAG[offset_text]
        field = CThostFtdcInputOrderField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        field.UserID = self.user_id
        field.InstrumentID = symbol
        if exchange_id:
            field.ExchangeID = exchange_id
        field.Direction = direction
        field.CombOffsetFlag = offset_flag
        field.CombHedgeFlag = "1"
        field.VolumeTotalOriginal = order_volume
        field.MinVolume = 1
        field.ForceCloseReason = "0"
        field.IsAutoSuspend = 0
        field.UserForceClose = 0
        field.ContingentCondition = "1"
        limit_price = _positive_ctp_price(price)
        field.OrderPriceType = "2"
        field.TimeCondition = "3"
        field.VolumeCondition = "1"
        time_in_force = str(
            kwargs.get("time_in_force") or kwargs.get("tif") or "GFD"
        ).upper()
        if time_in_force == "DAY":
            time_in_force = "GFD"
        if time_in_force != "GFD":
            raise ValueError(
                f"CTP time_in_force {time_in_force!r} is unsupported; iteration 22 requires GFD."
            )
        field.LimitPrice = limit_price
        if client_order_id is not None:
            field.OrderRef = str(client_order_id)
        elif hasattr(trader, "next_order_ref"):
            field.OrderRef = trader.next_order_ref()
        else:
            field.OrderRef = str(trader._req_id + 1)
        next_req_id = trader._next_request_id()
        field.RequestID = next_req_id
        submit = getattr(trader, "submit_order_insert", None)
        if callable(submit):
            ret = submit(
                field,
                next_req_id,
                execution_capability=execution_capability,
            )
        else:
            if self._execution_gate_capability is not None:
                raise ctp_client.CtpExecutionGateError(
                    "ctp_execution_gate_native_contract_unavailable"
                )
            api = trader.api
            if api is None:
                return self._make_request_data(
                    [], "make_order", symbol, extra_data, status=False
                )
            trader._record_request("order_insert")
            ret = api.ReqOrderInsert(field, next_req_id)
        order_dict = _ctp_field_to_dict(field)
        order_dict["_ret"] = ret
        order_dict["FrontID"] = getattr(trader, "_front_id", 0)
        order_dict["SessionID"] = getattr(trader, "_session_id", 0)
        if ret != 0:
            return self._make_request_data(
                [], "make_order", symbol, extra_data, status=False
            )
        return self._make_request_data(
            [CtpOrderData(order_dict, symbol, self.asset_type, True)],
            "make_order",
            symbol,
            extra_data,
        )

    def cancel_order(self, symbol, order_id=None, extra_data=None, **kwargs):
        execution_capability = kwargs.pop("_execution_capability", None)
        exchange_id = kwargs.get("exchange_id", "")
        self._ensure_execution_permitted(execution_capability, symbol, exchange_id)
        self._ensure_trading_ready()
        trader = self._trader
        if trader is None:
            return self._make_request_data(
                [], "cancel_order", symbol, extra_data, status=False
            )
        self._ensure_execution_permitted(execution_capability, symbol, exchange_id)
        field = CThostFtdcInputOrderActionField()
        field.BrokerID = self.broker_id
        field.InvestorID = self.user_id
        field.InstrumentID = symbol
        field.ActionFlag = "0"
        if exchange_id:
            field.ExchangeID = exchange_id
        order_ref = kwargs.get("order_ref", "")
        front_id = kwargs.get("front_id", 0)
        session_id = kwargs.get("session_id", 0)
        if order_id:
            field.OrderSysID = str(order_id)
        if order_ref:
            field.OrderRef = str(order_ref)
            field.FrontID = int(front_id) if front_id else trader._front_id
            field.SessionID = int(session_id) if session_id else trader._session_id
        request_id = trader._next_request_id()
        submit = getattr(trader, "submit_order_action", None)
        if callable(submit):
            ret = submit(
                field,
                request_id,
                execution_capability=execution_capability,
            )
        else:
            if self._execution_gate_capability is not None:
                raise ctp_client.CtpExecutionGateError(
                    "ctp_execution_gate_native_contract_unavailable"
                )
            api = trader.api
            if api is None:
                return self._make_request_data(
                    [], "cancel_order", symbol, extra_data, status=False
                )
            trader._record_request("order_action")
            ret = api.ReqOrderAction(field, request_id)
        return self._make_request_data(
            [_ctp_field_to_dict(field)],
            "cancel_order",
            symbol,
            extra_data,
            status=(ret == 0),
        )

    def query_order(self, symbol=None, order_id=None, extra_data=None, **kwargs):
        trader = self._trader
        if trader is None or not getattr(trader, "is_read_only_ready", False):
            return self._make_request_data(
                [], "query_order", symbol, extra_data, status=False
            )
        result = trader.query_orders_result(
            instrument_id=symbol or "",
            exchange_id=kwargs.get("exchange_id", ""),
            order_sys_id=order_id or "",
            timeout=kwargs.get("timeout", 5),
        )
        payload = dict(extra_data or {})
        payload.update(_query_evidence(result))
        rows = []
        for raw in result.records:
            data = raw if isinstance(raw, dict) else _ctp_field_to_dict(raw)
            # OrderRef is local to a front/session and is not an OrderSysID.
            # The native query filters OrderSysID; filter the alternative
            # client reference here before returning public order containers.
            if any(
                kwargs.get(key) is not None
                and str(data.get(native_key, "")) != str(kwargs[key])
                for key, native_key in (
                    ("order_ref", "OrderRef"),
                    ("front_id", "FrontID"),
                    ("session_id", "SessionID"),
                )
            ):
                continue
            rows.append(
                CtpOrderData(
                    data, data.get("InstrumentID", symbol), self.asset_type, True
                )
            )
        return self._make_request_data(
            rows if result.complete else [],
            "query_order",
            symbol,
            payload,
            status=result.complete,
        )

    def get_open_orders(self, symbol=None, extra_data=None, **kwargs):
        response = self.query_order(symbol=symbol, extra_data=extra_data, **kwargs)
        if not response.get_status():
            return self._make_request_data(
                [],
                "get_open_orders",
                symbol,
                response.get_extra_data(),
                status=False,
            )
        rows = []
        for row in response.get_data() or []:
            order = row.init_data()
            if order.get_order_status() in {
                OrderStatus.COMPLETED,
                OrderStatus.CANCELED,
                OrderStatus.REJECTED,
                OrderStatus.EXPIRED,
                OrderStatus.MMP_CANCELED,
                OrderStatus.EXPIRED_IN_MATCH,
            }:
                continue
            if int(order.volume_total or 0) > 0:
                rows.append(order)
        return self._make_request_data(
            rows, "get_open_orders", symbol, response.get_extra_data()
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
        trader = self._trader
        if trader is None or not getattr(trader, "is_read_only_ready", False):
            return self._make_request_data(
                [], "get_deals", symbol, extra_data, status=False
            )
        result = trader.query_trades_result(
            instrument_id=symbol or "",
            exchange_id=kwargs.get("exchange_id", ""),
            trade_id=kwargs.get("trade_id", ""),
            start_time=start_time or "",
            end_time=end_time or "",
            timeout=kwargs.get("timeout", 5),
        )
        payload = dict(extra_data or {})
        payload.update(_query_evidence(result))
        rows = []
        for raw in result.records:
            data = raw if isinstance(raw, dict) else _ctp_field_to_dict(raw)
            rows.append(
                CtpTradeData(
                    data, data.get("InstrumentID", symbol), self.asset_type, True
                )
            )
        total_records = len(rows)
        truncated = False
        if count is not None:
            limit = max(int(count), 0)
            truncated = total_records > limit
            rows = rows[-limit:] if limit else []
        snapshot_complete = result.complete and not truncated
        payload.update(
            trade_snapshot_complete=snapshot_complete,
            trade_record_count=total_records,
            returned_trade_record_count=len(rows),
            records_truncated=truncated,
            evidence_complete=snapshot_complete,
        )
        return self._make_request_data(
            rows if snapshot_complete else [],
            "get_deals",
            symbol,
            payload,
            status=snapshot_complete,
        )

    def get_instruments(self, symbol=None, extra_data=None, **kwargs):
        self._ensure_connected()
        trader = self._trader
        if trader is None:
            return self._make_request_data(
                [], "get_instruments", symbol, extra_data, status=False
            )
        result = trader.query_instruments_result(
            instrument_id=symbol or "",
            exchange_id=kwargs.get("exchange_id", ""),
            timeout=kwargs.get("timeout", 5),
        )
        payload = dict(extra_data or {})
        payload.update(_query_evidence(result))
        rows = [
            raw if isinstance(raw, dict) else _ctp_field_to_dict(raw)
            for raw in result.records
        ]
        return self._make_request_data(
            rows if result.complete else [],
            "get_instruments",
            symbol,
            payload,
            status=result.complete,
        )

    def get_instrument_margin_rate(self, symbol, extra_data=None, **kwargs):
        self._ensure_connected()
        result = self._trader.query_instrument_margin_rate_result(
            symbol,
            exchange_id=kwargs.get("exchange_id", ""),
            hedge_flag=kwargs.get("hedge_flag", "1"),
            timeout=kwargs.get("timeout", 5),
        )
        payload = dict(extra_data or {})
        payload.update(_query_evidence(result))
        rows = [
            raw if isinstance(raw, dict) else _ctp_field_to_dict(raw)
            for raw in result.records
        ]
        return self._make_request_data(
            rows if result.complete else [],
            "get_instrument_margin_rate",
            symbol,
            payload,
            status=result.complete,
        )

    def get_instrument_commission_rate(self, symbol, extra_data=None, **kwargs):
        self._ensure_connected()
        result = self._trader.query_instrument_commission_rate_result(
            symbol,
            exchange_id=kwargs.get("exchange_id", ""),
            timeout=kwargs.get("timeout", 5),
        )
        payload = dict(extra_data or {})
        payload.update(_query_evidence(result))
        rows = [
            raw if isinstance(raw, dict) else _ctp_field_to_dict(raw)
            for raw in result.records
        ]
        return self._make_request_data(
            rows if result.complete else [],
            "get_instrument_commission_rate",
            symbol,
            payload,
            status=result.complete,
        )

    def get_exchange_info(self, symbol=None, extra_data=None, **kwargs):
        """Bridge terminal instrument/margin/fee queries to one strict public spec."""
        if not symbol:
            response = self.get_instruments(
                symbol=None, extra_data=extra_data, **kwargs
            )
            payload = dict(response.get_extra_data() or {})
            payload.update(
                metadata_complete=False,
                metadata_evidence="instrument_enumeration_has_no_account_fee_or_margin_join",
            )
            records = []
            for raw in response.get_data() or []:
                record = dict(raw) if isinstance(raw, dict) else _ctp_field_to_dict(raw)
                record.update(metadata_complete=False, evidence_complete=False)
                records.append(record)
            return self._make_request_data(
                records,
                "get_exchange_info",
                symbol,
                payload,
                status=response.get_status(),
            )

        text = str(symbol).strip()
        exchange_id = str(kwargs.get("exchange_id") or "").strip().upper()
        instrument = text
        for separator in (".", "_"):
            if separator not in text:
                continue
            left, right = (part.strip() for part in text.split(separator, 1))
            if left.upper() in {"SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"}:
                exchange_id, instrument = left.upper(), right
            elif right.upper() in {"SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"}:
                instrument, exchange_id = left, right.upper()
            break

        timeout = kwargs.get("timeout", 5)
        hedge_flag = kwargs.get("hedge_flag", "1")
        instrument_result = self.query_instruments_result(
            instrument_id=instrument,
            exchange_id=exchange_id,
            timeout=timeout,
        )
        margin_result = self.query_instrument_margin_rate_result(
            instrument,
            exchange_id=exchange_id,
            hedge_flag=hedge_flag,
            timeout=timeout,
        )
        commission_result = self.query_instrument_commission_rate_result(
            instrument,
            exchange_id=exchange_id,
            timeout=timeout,
        )
        results = (instrument_result, margin_result, commission_result)
        payload = dict(extra_data or {})
        payload["component_queries"] = {
            result.request_type: result.as_dict(include_records=False)
            for result in results
        }
        record_complete = all(
            result.complete and len(result.records) == 1 for result in results
        )
        session_getter = getattr(self._trader, "get_session_state", None)
        current_session = session_getter() if callable(session_getter) else None
        bundle_errors = ctp_query_bundle_errors(
            results, current_session=current_session
        )
        if not record_complete or bundle_errors:
            payload["metadata_error"] = (
                "query_record_incomplete"
                if not record_complete
                else ",".join(bundle_errors)
            )
            payload.update(metadata_complete=False, evidence_complete=False)
            return self._make_request_data(
                [], "get_exchange_info", text, payload, status=False
            )

        spec = build_ctp_instrument_spec(
            instrument,
            exchange_id,
            instrument_result.records[0],
            margin_result.records[0],
            commission_result.records[0],
        )
        evidence_errors = ctp_instrument_evidence_errors(
            instrument,
            exchange_id,
            instrument_result.records[0],
            margin_result.records[0],
            commission_result.records[0],
        )
        metadata_complete = not evidence_errors
        if evidence_errors:
            payload["metadata_error"] = ",".join(evidence_errors)
        spec.update(
            symbol=text,
            instrument=instrument,
            metadata_complete=metadata_complete,
            evidence_complete=metadata_complete,
        )
        payload.update(
            metadata_complete=metadata_complete,
            evidence_complete=metadata_complete,
        )
        return self._make_request_data(
            [spec] if metadata_complete else [],
            "get_exchange_info",
            text,
            payload,
            status=metadata_complete,
        )

    @property
    def trader_client(self):
        return self._trader

    def get_environment_info(self) -> dict[str, Any]:
        official_simnow = self.ctp_environment == "simnow" and str(
            self.ctp_env_profile or ""
        ).startswith("set")
        return {
            "environment": "demo" if official_simnow else "unknown",
            "simulated": True if official_simnow else None,
            "verified": official_simnow,
            "profile": self.ctp_env_profile,
            "readiness": self.ctp_env_readiness,
        }

    def get_session_state(self) -> dict[str, Any]:
        if self._trader is None:
            return {
                "connected": False,
                "auth_state": "disconnected",
                "login_state": "disconnected",
                "settlement_state": "unknown",
                "read_only_ready": False,
                "trading_ready": False,
                "settlement_readback_verified": False,
                "request_counts": ctp_client.empty_ctp_request_counts(),
                "environment_profile": self.ctp_env_profile,
                "environment_readiness": self.ctp_env_readiness,
            }
        result = self._trader.get_session_state()
        result.update(
            environment_profile=self.ctp_env_profile,
            environment_readiness=self.ctp_env_readiness,
        )
        return result

    def get_request_counts(self) -> dict[str, int]:
        if self._trader is None:
            return ctp_client.empty_ctp_request_counts()
        return self._trader.get_request_counts()

    def confirm_settlement(
        self,
        timeout: float = 5.0,
        *,
        _execution_capability: object | None = None,
    ) -> bool:
        with self._connect_lock:
            installed = self._execution_gate_capability
            trader = self._trader
            if installed is not None:
                if _execution_capability is not installed:
                    raise ctp_client.CtpExecutionGateError(
                        "ctp_execution_gate_capability_mismatch"
                    )
                method = getattr(trader, "confirm_settlement", None)
                state_reader = getattr(trader, "get_execution_gate_state", None)
                if trader is None or not callable(method) or not callable(state_reader):
                    raise ctp_client.CtpExecutionGateError(
                        "ctp_execution_gate_native_contract_unavailable"
                    )
                state = state_reader()
                if not isinstance(state, dict) or state.get("managed") is not True:
                    raise ctp_client.CtpExecutionGateError(
                        "ctp_execution_gate_native_contract_unavailable"
                    )
                return bool(
                    method(
                        timeout=timeout,
                        _execution_capability=_execution_capability,
                    )
                )
        self._ensure_connected()
        return bool(self._trader.confirm_settlement(timeout=timeout))

    def verify_settlement_confirmation(self, timeout: float = 5.0) -> QueryResult[Any]:
        self._ensure_connected()
        return self._trader.verify_settlement_confirmation(timeout=timeout)

    def query_account_result(self, timeout: float = 5.0) -> QueryResult[Any]:
        self._ensure_connected()
        return self._trader.query_account_result(timeout=timeout)

    def query_positions_result(self, timeout: float = 5.0) -> QueryResult[Any]:
        self._ensure_connected()
        return self._trader.query_positions_result(timeout=timeout)

    def query_orders_result(self, **kwargs: Any) -> QueryResult[Any]:
        self._ensure_connected()
        return self._trader.query_orders_result(**kwargs)

    query_order_result = query_orders_result

    def query_trades_result(self, **kwargs: Any) -> QueryResult[Any]:
        self._ensure_connected()
        return self._trader.query_trades_result(**kwargs)

    def query_instruments_result(self, **kwargs: Any) -> QueryResult[Any]:
        self._ensure_connected()
        return self._trader.query_instruments_result(**kwargs)

    def query_instrument_margin_rate_result(
        self, *args: Any, **kwargs: Any
    ) -> QueryResult[Any]:
        self._ensure_connected()
        return self._trader.query_instrument_margin_rate_result(*args, **kwargs)

    def query_instrument_commission_rate_result(
        self, *args: Any, **kwargs: Any
    ) -> QueryResult[Any]:
        self._ensure_connected()
        return self._trader.query_instrument_commission_rate_result(*args, **kwargs)

    def query_settlement_confirmation_result(self, **kwargs: Any) -> QueryResult[Any]:
        self._ensure_connected()
        return self._trader.query_settlement_confirmation_result(**kwargs)


class CtpMarketStream(BaseDataStream):
    def __init__(self, data_queue: Any = None, **kwargs: Any) -> None:
        super().__init__(data_queue, **kwargs)
        resolved_kwargs, self.ctp_env_name = _resolve_ctp_runtime_kwargs(kwargs)
        self.ctp_env_profile = resolved_kwargs.get("ctp_env_profile", self.ctp_env_name)
        self.ctp_env_readiness = resolved_kwargs.get("ctp_env_readiness", "unknown")
        self.md_front = resolved_kwargs.get("md_front", "")
        self.broker_id = resolved_kwargs.get("broker_id", "")
        self.user_id = resolved_kwargs.get("user_id", "")
        self.password = resolved_kwargs.get("password", "")
        self.topics = resolved_kwargs.get("topics", [])
        self.asset_type = resolved_kwargs.get("asset_type", "FUTURE")
        self._md_client = None
        self._ingest_seq = 0
        self._observed_client_generation = 0
        self._connection_generation = 0
        self._volume_tracker = CtpVolumeDeltaTracker()

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
            if topic.get("topic") in ("tick", "ticker", "depth"):
                symbol = topic.get("symbol", "")
                if symbol:
                    instruments.append(symbol)
                instruments.extend(topic.get("symbol_list", []))
        if instruments:
            self._md_client.subscribe(instruments)
        self._md_client.start(block=False)

    def _on_login(self, login_field):
        self.state = ConnectionState.AUTHENTICATED

    def _on_tick(self, tick_field):
        tick_dict = _ctp_field_to_dict(tick_field)
        symbol = tick_dict.get("InstrumentID", "")
        client_generation = int(
            getattr(self._md_client, "connection_generation", 0) or 0
        )
        if client_generation != self._observed_client_generation:
            self._observed_client_generation = client_generation
            self._connection_generation += 1
            self._ingest_seq = 0
        self._ingest_seq += 1
        row = CtpTickerData(
            tick_dict,
            symbol,
            self.asset_type,
            True,
            connection_generation=self._connection_generation,
            ingest_seq=self._ingest_seq,
        )
        row.init_data()
        row.resolve_event_time()
        if _validate_ctp_quote(row):
            self._volume_tracker.apply(row)
        else:
            row.apply_volume_delta(0, complete=False, quality="INVALID_QUOTE")
        self.push_data(row)

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
            if topic.get("topic") in ("tick", "ticker", "depth"):
                symbol = topic.get("symbol", "")
                if symbol:
                    instruments.append(symbol)
                instruments.extend(topic.get("symbol_list", []))
        if instruments and self._md_client is not None:
            self._md_client.subscribe(instruments)

    def _run_loop(self):
        self.connect()
        while self._running:
            time.sleep(1)


class CtpTradeStream(BaseDataStream):
    def __init__(self, data_queue: Any = None, **kwargs: Any) -> None:
        self._request_feed = kwargs.pop("request_feed", None)
        super().__init__(data_queue, **kwargs)
        resolved_kwargs, self.ctp_env_name = _resolve_ctp_runtime_kwargs(kwargs)
        self.ctp_env_profile = resolved_kwargs.get("ctp_env_profile", self.ctp_env_name)
        self.ctp_env_readiness = resolved_kwargs.get("ctp_env_readiness", "unknown")
        self.td_front = resolved_kwargs.get("td_front", "")
        self.broker_id = resolved_kwargs.get("broker_id", "")
        self.user_id = resolved_kwargs.get("user_id", "")
        self.password = resolved_kwargs.get("password", "")
        self.auth_code = resolved_kwargs.get("auth_code", "0000000000000000")
        self.app_id = resolved_kwargs.get("app_id", "simnow_client_test")
        self.auto_settlement_confirm = _as_bool(
            resolved_kwargs.get("auto_settlement_confirm"), default=True
        )
        self.asset_type = resolved_kwargs.get("asset_type", "FUTURE")
        self._trader = None
        self._owns_trader = False

    def connect(self):
        self.state = ConnectionState.CONNECTING
        if self._request_feed is not None:
            self._request_feed.connect()
            self._trader = self._request_feed.trader_client
            self._owns_trader = False
            if self._trader is None:
                self.state = ConnectionState.ERROR
                raise BtConnectionError(
                    "CTP", "shared request feed has no TraderClient"
                )
        else:
            self._trader = ctp_client.TraderClient(
                self.td_front,
                self.broker_id,
                self.user_id,
                self.password,
                app_id=self.app_id,
                auth_code=self.auth_code,
                auto_settlement_confirm=self.auto_settlement_confirm,
            )
            self._owns_trader = True
        self._trader.on_order = self._on_order
        self._trader.on_trade = self._on_trade
        self._trader.on_login = self._on_login
        self._trader.on_error = self._on_error
        if self._owns_trader:
            self._trader.start(block=False)
        elif getattr(self._trader, "is_read_only_ready", False):
            self.state = ConnectionState.AUTHENTICATED

    def _on_login(self, login_field):
        self.state = ConnectionState.AUTHENTICATED

    def _on_order(self, order_field):
        order_dict = _ctp_field_to_dict(order_field)
        session = self._trader.get_session_state() if self._trader is not None else {}
        order_dict.setdefault("TradingDay", session.get("trading_day", ""))
        order_dict.setdefault("InvestorID", self.user_id)
        symbol = order_dict.get("InstrumentID", "")
        self.push_data(CtpOrderData(order_dict, symbol, self.asset_type, True))

    def _on_trade(self, trade_field):
        trade_dict = _ctp_field_to_dict(trade_field)
        session = self._trader.get_session_state() if self._trader is not None else {}
        trade_dict.setdefault("TradingDay", session.get("trading_day", ""))
        trade_dict.setdefault("InvestorID", self.user_id)
        symbol = trade_dict.get("InstrumentID", "")
        self.push_data(CtpTradeData(trade_dict, symbol, self.asset_type, True))

    def _on_error(self, rsp_info):
        self.state = ConnectionState.ERROR

    def disconnect(self):
        if self._trader is not None:
            if self._owns_trader:
                self._trader.stop()
            else:
                for callback_name, callback in (
                    ("on_order", self._on_order),
                    ("on_trade", self._on_trade),
                    ("on_login", self._on_login),
                    ("on_error", self._on_error),
                ):
                    if getattr(self._trader, callback_name, None) == callback:
                        setattr(self._trader, callback_name, None)
            self._trader = None
        self._owns_trader = False
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
        self.asset_type = kwargs.get("asset_type", "FUTURE")
        self._params = CtpExchangeDataFuture()


__all__ = [
    "CTP_DIRECTION_FLAG",
    "CTP_OFFSET_FLAG",
    "CtpMarketStream",
    "CtpRequestData",
    "CtpRequestDataFuture",
    "CtpTradeStream",
    "CtpVolumeDeltaTracker",
    "_ctp_field_to_dict",
]
