from __future__ import annotations

import queue
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from bt_api_base.gateway.adapters.base import BaseGatewayAdapter
from bt_api_base.gateway.models import GatewayTick
from bt_api_base.gateway.protocol import CHANNEL_EVENT, CHANNEL_MARKET

from bt_api_ctp.containers.ctp.ctp_order import CtpOrderData
from bt_api_ctp.containers.ctp.ctp_ticker import CtpTickerData
from bt_api_ctp.containers.ctp.ctp_trade import CtpTradeData
from bt_api_ctp.ctp.client import _check_native_module
from bt_api_ctp.feeds.live_ctp_feed import (
    CTP_DIRECTION_FLAG,
    CTP_OFFSET_FLAG,
    CtpMarketStream,
    CtpRequestDataFuture,
    CtpTradeStream,
)

_CTP_EXCHANGES = frozenset({"SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"})
_CTP_TZ = timezone(timedelta(hours=8))
_CZCE_PRODUCT_PREFIXES = frozenset(
    {
        "AP",
        "CF",
        "CJ",
        "CY",
        "FG",
        "JR",
        "LR",
        "MA",
        "OI",
        "PF",
        "PK",
        "PM",
        "PX",
        "RI",
        "RM",
        "RS",
        "SA",
        "SF",
        "SM",
        "SR",
        "TA",
        "UR",
        "WH",
        "ZC",
    }
)


def _ctp_tick_timestamp_datetime(
    row: CtpTickerData, fallback_time: float | None = None
) -> tuple[float, datetime]:
    stamp = float(time.time() if fallback_time is None else fallback_time)
    tick_dt = datetime.fromtimestamp(stamp, timezone.utc)
    day = str(row.trading_day or "")
    update_time = str(row.update_time_val or "")
    if len(day) == 8 and day.isdigit() and update_time:
        tick_dt = datetime.strptime(f"{day} {update_time}", "%Y%m%d %H:%M:%S").replace(
            microsecond=int(row.update_millisec or 0) * 1000,
            tzinfo=_CTP_TZ,
        )
        stamp = tick_dt.timestamp()
    return stamp, tick_dt


class CtpGatewayAdapter(BaseGatewayAdapter):
    def __init__(self, **kwargs: Any) -> None:
        normalized = dict(kwargs)
        normalized["md_front"] = normalized.get("md_front") or normalized.get("md_address") or ""
        normalized["td_front"] = normalized.get("td_front") or normalized.get("td_address") or ""
        normalized["user_id"] = normalized.get("user_id") or normalized.get("investor_id") or ""
        super().__init__(**normalized)
        self.q: queue.Queue[Any] = queue.Queue()
        self._stream_kwargs = normalized
        self.market: CtpMarketStream
        self.trade: CtpTradeStream
        self.feed: CtpRequestDataFuture
        self._create_streams()
        self.aliases: dict[str, set[str]] = defaultdict(set)
        self.last_volume: dict[str, float] = {}
        self.last_price: dict[str, float] = {}
        self._price_ticks: dict[str, float] = {}
        self._symbol_specs: dict[str, dict[str, Any]] = {}
        self.running = False
        self.thread: threading.Thread | None = None
        self.timeout = float(normalized.get("gateway_startup_timeout_sec", 10.0) or 10.0)
        configured_attempts = normalized.get("gateway_startup_attempts")
        if configured_attempts is None:
            configured_attempts = 3 if self.timeout >= 30.0 else 1
        self.startup_attempts = max(1, int(configured_attempts or 1))
        self.retry_backoff = max(
            0.0,
            float(normalized.get("gateway_startup_retry_backoff_sec", 1.0) or 0.0),
        )

    def _create_streams(self) -> None:
        self.market = CtpMarketStream(self.q, **self._stream_kwargs)
        self.trade = CtpTradeStream(self.q, **self._stream_kwargs)
        self.feed = CtpRequestDataFuture(None, **self._stream_kwargs)

    def _startup_stream_timeout(self) -> float:
        if self.startup_attempts <= 1:
            return self.timeout
        return max(5.0, self.timeout / (self.startup_attempts * 2.0))

    def _stop_startup_streams(self) -> None:
        self.feed._trader = None
        self.feed._connected = False
        for stream in (self.market, self.trade):
            try:
                stream.stop()
            except Exception:
                pass

    def connect(self) -> None:
        if self.running:
            return
        _check_native_module()
        stream_timeout = self._startup_stream_timeout()
        last_error: Exception | None = None
        for attempt in range(1, self.startup_attempts + 1):
            try:
                self.market.start()
                self.trade.start()
                if not self.market.wait_connected(timeout=stream_timeout):
                    raise RuntimeError("ctp market not ready")
                if not self.trade.wait_connected(timeout=stream_timeout):
                    raise RuntimeError("ctp trade not ready")
                self.feed._trader = self.trade.trader_client
                self.feed._connected = True
                self.running = True
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()
                return
            except Exception as exc:
                last_error = exc
                self._stop_startup_streams()
                self._create_streams()
                if attempt < self.startup_attempts and self.retry_backoff > 0:
                    time.sleep(self.retry_backoff)
        if last_error is not None:
            raise RuntimeError(
                f"ctp gateway not ready after {self.startup_attempts} attempts: {last_error}"
            ) from last_error

    def disconnect(self) -> None:
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=1.0)
        self.feed._trader = None
        self.feed._connected = False
        self.market.stop()
        self.trade.stop()

    def get_session_state(self) -> dict[str, Any]:
        """Return the current CTP authentication/login state when available."""
        trader = getattr(getattr(self, "trade", None), "trader_client", None)
        getter = getattr(trader, "get_session_state", None)
        if callable(getter):
            state = getter()
            if isinstance(state, dict):
                return dict(state)
        return {"auth_state": "unknown", "login_state": "unknown"}

    def subscribe_symbols(self, symbols: list[str]) -> dict[str, Any]:
        topics = []
        done = []
        for raw in symbols:
            alias = str(raw or "").strip()
            instrument, _ = _split(alias)
            if not instrument:
                continue
            self.aliases[instrument].update({alias, instrument})
            topics.append({"topic": "tick", "symbol": instrument})
            done.append(alias)
        if topics:
            self.market.subscribe_topics(topics)
        return {"symbols": done}

    def get_balance(self) -> dict[str, Any]:
        rows = self.feed.get_account().get_data()
        if not rows:
            return {"cash": 0.0, "value": 0.0, "equity": 0.0, "margin": 0.0}
        row = rows[0].init_data()
        balance = float(getattr(row, "balance", None) or row.get_total_wallet_balance() or 0.0)
        available = float(getattr(row, "available", None) or row.get_available_margin() or 0.0)
        used_margin = float(getattr(row, "curr_margin", None) or 0.0)
        position_profit = float(getattr(row, "position_profit", None) or 0.0)
        return {
            "account_id": getattr(row, "account_id", None),
            "cash": available,
            "available": available,
            "available_funds": available,
            "margin_free": available,
            "value": balance,
            "equity": balance,
            "balance": balance,
            "margin": used_margin,
            "used_margin": used_margin,
            "profit": position_profit,
            "position_profit": position_profit,
            "close_profit": float(getattr(row, "close_profit", None) or 0.0),
            "commission": float(getattr(row, "commission", None) or 0.0),
            "frozen_margin": float(getattr(row, "frozen_margin", None) or 0.0),
            "pre_balance": float(getattr(row, "pre_balance", None) or 0.0),
            "risk_degree": float(getattr(row, "risk_degree", None) or 0.0),
        }

    def get_positions(self) -> list[dict[str, Any]]:
        out = []
        for raw in self.feed.get_position().get_data() or []:
            row = raw.init_data()
            instrument = row.get_symbol_name()
            exchange_id = row.exchange_id
            spec_symbol = f"{exchange_id}.{instrument}" if exchange_id else instrument
            spec = self.get_symbol_info(spec_symbol) if instrument else {}
            multiplier = _positive_float(spec.get("multiplier"), 1.0)
            avg_price = row.get_avg_price(multiplier)
            current_price = self.last_price.get(instrument) or row.get_mark_price()
            out.append(
                {
                    "instrument": instrument,
                    "symbol": instrument,
                    "direction": row.get_position_direction(),
                    "volume": row.get_position_volume(),
                    "price": avg_price,
                    "avg_price": avg_price,
                    "current_price": current_price,
                    "last_price": self.last_price.get(instrument),
                    "mark_price": row.get_mark_price(),
                    "profit": row.get_position_unrealized_pnl(),
                    "position_profit": row.get_position_unrealized_pnl(),
                    "close_profit": row.close_profit,
                    "commission": row.get_position_commission(),
                    "use_margin": row.get_initial_margin(),
                    "margin_value": row.get_initial_margin(),
                    "initial_margin": row.get_initial_margin(),
                    "today_position": row.get_today_position(),
                    "yd_position": row.get_yesterday_position(),
                    "position_cost": row.position_cost,
                    "open_cost": row.open_cost,
                    "exchange_id": exchange_id,
                    **spec,
                }
            )
        return out

    def get_open_orders(self) -> list[dict[str, Any]]:
        response = self.feed.get_open_orders()
        if not response.get_status():
            return []
        out = []
        for raw in response.get_data() or []:
            row = raw.init_data()
            item = _order(row, self.aliases)
            order_sys_id = item.get("external_order_id") or ""
            item.update(
                {
                    "id": order_sys_id,
                    "order_id": order_sys_id,
                }
            )
            if int(item.get("remaining") or 0) > 0:
                out.append(item)
        return out

    fetch_open_orders = get_open_orders

    def get_symbol_info(self, symbol: str) -> dict[str, Any]:
        instrument, exchange_id = _split(symbol)
        cache_keys = [key for key in (str(symbol or "").strip(), instrument) if key]
        for key in cache_keys:
            cached = self._symbol_specs.get(key)
            if cached:
                return dict(cached)

        trader = getattr(self.feed, "trader_client", None) or getattr(self.feed, "_trader", None)
        if trader is None:
            return {}

        instrument_info = _safe_query(
            getattr(trader, "query_instrument", None),
            instrument,
            exchange_id=exchange_id,
            timeout=2,
        )
        margin_info = _safe_query(
            getattr(trader, "query_instrument_margin_rate", None),
            instrument,
            exchange_id=exchange_id,
            timeout=2,
        )
        commission_info = _safe_query(
            getattr(trader, "query_instrument_commission_rate", None),
            instrument,
            exchange_id=exchange_id,
            timeout=2,
        )
        spec = _symbol_spec(instrument, exchange_id, instrument_info, margin_info, commission_info)
        if spec:
            for key in cache_keys + [spec.get("instrument", ""), spec.get("symbol", "")]:
                if key:
                    self._symbol_specs[str(key)] = dict(spec)
        return spec

    def _get_price_tick(self, instrument: str) -> float:
        cached = self._price_ticks.get(instrument)
        if cached is not None:
            return cached
        spec = self.get_symbol_info(instrument)
        tick = _positive_float(spec.get("price_tick") or spec.get("tick_size"), 0.0)
        if tick > 0:
            self._price_ticks[instrument] = tick
            return tick
        return 1.0

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(
            payload.get("data_name") or payload.get("symbol") or payload.get("instrument") or ""
        ).strip()
        instrument, exchange_id = _split(name)
        if not instrument:
            raise ValueError("CTP order rejected: missing instrument.")
        side = str(payload.get("side") or "buy").lower()
        if side not in CTP_DIRECTION_FLAG:
            raise ValueError(f"CTP order side {payload.get('side')!r} is unsupported.")
        requested_order_type = str(
            payload.get("order_type") or payload.get("type") or "limit"
        ).lower()
        if requested_order_type not in {"limit", "market"}:
            raise ValueError(f"CTP order type {requested_order_type!r} is unsupported.")
        offset = str(payload.get("offset") or "open").lower()
        if offset not in CTP_OFFSET_FLAG:
            raise ValueError(f"CTP order offset {payload.get('offset')!r} is unsupported.")
        volume = _positive_int_lot(
            payload["size"] if "size" in payload else payload.get("volume"),
            "size",
        )
        price = payload.get("price")
        if requested_order_type == "market":
            last_price = self.last_price.get(instrument or name)
            if not last_price or last_price <= 0:
                raise RuntimeError(
                    f"CTP order for {instrument or name} rejected: no recent tick price available"
                )
            price_tick = self._get_price_tick(instrument or name)
            slippage = price_tick * 5
            price = (
                (last_price + slippage) if side == "buy" else max(last_price - slippage, price_tick)
            )
            price = round(price, 4)
        else:
            try:
                price = float(price)
            except (TypeError, ValueError) as exc:
                raise ValueError("CTP limit order requires a positive price.") from exc
            if price <= 0:
                raise ValueError("CTP limit order requires a positive price.")
        client_order_id = _first_non_empty(
            payload,
            "client_order_id",
            "bt_order_ref",
            "request_id",
            "order_ref",
        )
        response = self.feed.make_order(
            instrument,
            volume=volume,
            price=price,
            order_type=f"{side}-limit",
            offset=offset,
            client_order_id=client_order_id,
            exchange_id=exchange_id or payload.get("exchange_id") or "",
        )
        if not response.get_status():
            raise RuntimeError("ctp order failed")
        row = response.get_data()[0].init_data()
        order_sys_id = row.get_order_id() or ""
        order_ref = row.get_client_order_id() or ""
        return {
            "id": order_sys_id,
            "order_id": order_sys_id,
            "external_order_id": order_sys_id,
            "order_sys_id": order_sys_id,
            "order_ref": order_ref,
            "client_order_id": client_order_id or order_ref,
            "front_id": row.front_id,
            "session_id": row.session_id,
            "exchange_id": row.get_order_exchange_id(),
            "id_source": "exchange" if order_sys_id else "local_pending",
            "details": {
                "bt_order_ref": payload.get("bt_order_ref"),
                "request_id": payload.get("request_id"),
                "client_order_id": client_order_id,
            },
        }

    def cancel_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(
            payload.get("data_name") or payload.get("symbol") or payload.get("instrument") or ""
        ).strip()
        instrument, exchange_id = _split(name)
        response = self.feed.cancel_order(
            instrument or name,
            order_id=payload.get("order_id") or payload.get("external_order_id"),
            exchange_id=exchange_id or payload.get("exchange_id") or "",
            front_id=payload.get("front_id"),
            session_id=payload.get("session_id"),
            order_ref=payload.get("order_ref"),
        )
        if not response.get_status():
            raise RuntimeError("ctp cancel failed")
        data = dict((response.get_data() or [{}])[0])
        order_sys_id = data.get("OrderSysID") or payload.get("order_id") or ""
        order_ref = data.get("OrderRef") or payload.get("order_ref") or ""
        return {
            "id": order_sys_id,
            "order_id": order_sys_id,
            "external_order_id": order_sys_id,
            "order_ref": order_ref,
            "order_sys_id": order_sys_id,
            "client_order_id": payload.get("client_order_id")
            or payload.get("bt_order_ref")
            or order_ref,
            "front_id": data.get("FrontID") or payload.get("front_id"),
            "session_id": data.get("SessionID") or payload.get("session_id"),
            "exchange_id": data.get("ExchangeID") or exchange_id,
            "id_source": "exchange" if order_sys_id else "local_pending",
        }

    def _run(self) -> None:
        while self.running:
            try:
                item = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(item, CtpTickerData):
                self._tick(item.init_data())
            elif isinstance(item, CtpOrderData):
                self.emit(CHANNEL_EVENT, _order(item.init_data(), self.aliases))
            elif isinstance(item, CtpTradeData):
                self.emit(CHANNEL_EVENT, _trade(item.init_data(), self.aliases))

    def _tick(self, row: CtpTickerData) -> None:
        instrument = row.get_symbol_name() or ""
        price = float(row.get_last_price() or 0.0)
        if not instrument or price <= 0:
            return
        self.last_price[instrument] = price
        total = float(row.get_last_volume() or 0.0)
        prev = self.last_volume.get(instrument)
        self.last_volume[instrument] = total
        volume = max(total - prev, 0.0) if prev is not None else 0.0
        stamp, dt = _ctp_tick_timestamp_datetime(row)
        for alias in self.aliases.get(instrument) or {instrument}:
            self.emit(
                CHANNEL_MARKET,
                GatewayTick(
                    timestamp=stamp,
                    symbol=alias,
                    exchange=row.exchange_id or "",
                    asset_type="futures",
                    local_time=time.time(),
                    price=price,
                    volume=volume,
                    datetime=dt,
                    instrument_id=instrument,
                    exchange_id=row.exchange_id or "",
                    trading_day=row.trading_day or "",
                    update_time=row.update_time_val or "",
                    update_millisec=int(row.update_millisec or 0),
                    bid_price=row.get_bid_price(),
                    ask_price=row.get_ask_price(),
                    bid_volume=float(row.get_bid_volume() or 0.0),
                    ask_volume=float(row.get_ask_volume() or 0.0),
                    openinterest=float(row.get_open_interest() or 0.0),
                    turnover=float(row.turnover or 0.0),
                    trade_id=f"{instrument}-{int(total)}",
                ),
            )


def _split(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if "." in text:
        left, right = text.split(".", 1)
        left_text = left.strip()
        right_text = right.strip()
        left_exchange = left_text.upper()
        right_exchange = right_text.upper()
        if left_exchange in _CTP_EXCHANGES:
            return _normalize_instrument(right_text, left_exchange), left_exchange
        if right_exchange in _CTP_EXCHANGES:
            return _normalize_instrument(left_text, right_exchange), right_exchange
        return _normalize_instrument(left_text, right_exchange), right_exchange
    if "_" in text:
        left, right = text.split("_", 1)
        left_text = left.strip()
        right_text = right.strip()
        left_exchange = left_text.upper()
        right_exchange = right_text.upper()
        if left_exchange in _CTP_EXCHANGES:
            return _normalize_instrument(right_text, left_exchange), left_exchange
        if right_exchange in _CTP_EXCHANGES:
            return _normalize_instrument(left_text, right_exchange), right_exchange
    return _normalize_instrument(text, ""), ""


def _normalize_instrument(instrument: str, exchange_id: str = "") -> str:
    text = str(instrument or "").strip()
    if not text:
        return ""
    match = re.fullmatch(r"([A-Za-z]+)(\d{4})", text)
    if not match:
        return text
    prefix, digits = match.groups()
    exchange = str(exchange_id or "").strip().upper()
    if exchange == "CZCE" or (not exchange and prefix.upper() in _CZCE_PRODUCT_PREFIXES):
        return f"{prefix}{digits[-3:]}"
    return text


def _field_value(source: Any, *names: str) -> Any:
    for name in names:
        if isinstance(source, dict) and name in source:
            value = source.get(name)
            if value not in (None, ""):
                return value
        if source is not None and hasattr(source, name):
            try:
                value = getattr(source, name)
            except Exception:
                continue
            if value not in (None, ""):
                return value
    return None


def _field_float(source: Any, *names: str) -> float | None:
    value = _field_value(source, *names)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _positive_int_lot(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"CTP order {field_name} must be a positive integer lot.")
    try:
        lot = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"CTP order {field_name} must be a positive integer lot.") from exc
    if not lot.is_finite() or lot <= 0 or lot != lot.to_integral_value():
        raise ValueError(f"CTP order {field_name} must be a positive integer lot.")
    return int(lot)


def _first_non_empty(source: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = source.get(name)
        if value not in (None, ""):
            return value
    return None


def _safe_query(func: Any, *args: Any, **kwargs: Any) -> Any:
    if not callable(func):
        return None
    try:
        return func(*args, **kwargs)
    except TypeError:
        kwargs.pop("exchange_id", None)
        try:
            return func(*args, **kwargs)
        except Exception:
            return None
    except Exception:
        return None


def _symbol_spec(
    instrument: str,
    exchange_id: str,
    instrument_info: Any,
    margin_info: Any,
    commission_info: Any,
) -> dict[str, Any]:
    if not any((instrument_info, margin_info, commission_info)):
        return {}
    exchange = str(
        _field_value(instrument_info, "ExchangeID")
        or _field_value(margin_info, "ExchangeID")
        or _field_value(commission_info, "ExchangeID")
        or exchange_id
        or ""
    ).strip()
    symbol = str(
        _field_value(instrument_info, "InstrumentID")
        or _field_value(margin_info, "InstrumentID")
        or _field_value(commission_info, "InstrumentID")
        or instrument
        or ""
    ).strip()
    multiplier = _field_float(instrument_info, "VolumeMultiple", "contract_size", "multiplier")
    price_tick = _field_float(instrument_info, "PriceTick", "price_tick", "tick_size")
    long_margin_rate = _field_float(margin_info, "LongMarginRatioByMoney", "long_margin_rate")
    short_margin_rate = _field_float(margin_info, "ShortMarginRatioByMoney", "short_margin_rate")
    open_fee_rate = _field_float(commission_info, "OpenRatioByMoney", "open_fee_rate")
    open_fee_amount = _field_float(commission_info, "OpenRatioByVolume", "open_fee_amount")
    close_fee_rate = _field_float(commission_info, "CloseRatioByMoney", "close_fee_rate")
    close_fee_amount = _field_float(commission_info, "CloseRatioByVolume", "close_fee_amount")
    close_today_fee_rate = _field_float(
        commission_info,
        "CloseTodayRatioByMoney",
        "close_today_fee_rate",
    )
    close_today_fee_amount = _field_float(
        commission_info,
        "CloseTodayRatioByVolume",
        "close_today_fee_amount",
    )
    margin_rate = long_margin_rate if long_margin_rate is not None else short_margin_rate

    spec: dict[str, Any] = {
        "source": "ctp_gateway",
        "symbol": symbol,
        "instrument": symbol,
        "exchange": exchange,
        "exchange_id": exchange,
        "product_id": _field_value(instrument_info, "ProductID"),
        "price_tick": price_tick,
        "tick_size": price_tick,
        "multiplier": multiplier,
        "contract_multiplier": multiplier,
        "contract_size": multiplier,
        "volume_multiple": multiplier,
        "margin": margin_rate,
        "margin_rate": margin_rate,
        "long_margin_rate": long_margin_rate,
        "short_margin_rate": short_margin_rate,
        "long_margin_amount": _field_float(margin_info, "LongMarginRatioByVolume"),
        "short_margin_amount": _field_float(margin_info, "ShortMarginRatioByVolume"),
        "open_fee_rate": open_fee_rate,
        "open_commission_rate": open_fee_rate,
        "commission_rate": open_fee_rate,
        "open_fee_amount": open_fee_amount,
        "open_commission_amount": open_fee_amount,
        "commission_amount": open_fee_amount,
        "close_fee_rate": close_fee_rate,
        "close_commission_rate": close_fee_rate,
        "close_fee_amount": close_fee_amount,
        "close_commission_amount": close_fee_amount,
        "close_yesterday_fee_rate": close_fee_rate,
        "close_yesterday_commission_rate": close_fee_rate,
        "close_yesterday_fee_amount": close_fee_amount,
        "close_yesterday_commission_amount": close_fee_amount,
        "close_today_fee_rate": close_today_fee_rate,
        "close_today_commission_rate": close_today_fee_rate,
        "close_today_fee_amount": close_today_fee_amount,
        "close_today_commission_amount": close_today_fee_amount,
    }
    return {key: value for key, value in spec.items() if value not in (None, "")}


def _alias(aliases: dict[str, set[str]], instrument: str) -> str:
    return next(iter(aliases.get(instrument) or {instrument}), instrument)


def _status(value: Any) -> str:
    raw = str(getattr(value, "value", value) or "submitted").lower()
    return {
        "new": "accepted",
        "partially_filled": "partial",
        "filled": "completed",
    }.get(raw, raw)


def _order(row: CtpOrderData, aliases: dict[str, set[str]]) -> dict[str, Any]:
    instrument = row.get_symbol_name() or ""
    size = int(row.get_order_size() or 0)
    filled = int(row.get_executed_qty() or 0)
    order_sys_id = row.get_order_id() or ""
    order_ref = row.get_client_order_id() or ""
    return {
        "kind": "order",
        "client_order_id": order_ref,
        "order_ref": order_ref,
        "external_order_id": order_sys_id,
        "order_sys_id": order_sys_id,
        "data_name": _alias(aliases, instrument),
        "instrument": instrument,
        "exchange_id": row.get_order_exchange_id(),
        "front_id": row.front_id,
        "session_id": row.session_id,
        "status": _status(row.get_order_status()),
        "status_msg": row.status_msg or "",
        "side": row.get_order_side(),
        "offset": row.get_order_offset(),
        "price": row.get_order_price(),
        "size": size,
        "filled": filled,
        "remaining": max(size - filled, 0),
        "id_source": "exchange" if order_sys_id else "local_pending",
    }


def _trade(row: CtpTradeData, aliases: dict[str, set[str]]) -> dict[str, Any]:
    instrument = row.get_symbol_name() or ""
    order_sys_id = row.get_order_id() or ""
    order_ref = row.get_client_order_id() or ""
    trade_id = row.get_trade_id() or ""
    return {
        "kind": "trade",
        "client_order_id": order_ref,
        "trade_id": trade_id,
        "order_ref": order_ref,
        "external_order_id": order_sys_id,
        "order_sys_id": order_sys_id,
        "data_name": _alias(aliases, instrument),
        "instrument": instrument,
        "exchange_id": row.exchange_id,
        "side": row.get_trade_side(),
        "offset": row.get_trade_offset(),
        "price": row.get_trade_price(),
        "size": row.get_trade_volume(),
        "id_source": "exchange" if trade_id else "unknown",
    }
