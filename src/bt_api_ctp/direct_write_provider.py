"""CTP direct write provider (iteration 62, no execution session).

Registered for ``bt_api.direct_write_providers`` / ``CTP___FUTURE``.  The SDK
(``bt_api_py.direct_write``) owns write admission and the pre-dispatch commit
ordering; this module owns only CTP facts and native calls:

* reserve a 12-digit ``OrderRef`` above the session ``MaxOrderRef`` together
  with ``FrontID``/``SessionID``/``TradingDay`` *before* any send;
* consume the SDK one-shot grant, arm the native gate for the instrument and
  send exactly once;
* cancel by the original venue identity (``OrderRef`` + ``FrontID`` +
  ``SessionID``, or ``ExchangeID`` + ``OrderSysID``), never mixing the two;
* confirm the trading day's settlement statement after SDK admission;
* after a same-day re-login, restore trading readiness with bounded,
  read-only settlement readbacks (never a confirmation): proactively on a
  background monitor thread, with a write-path fallback;
* convert native order/trade/error returns into neutral
  :class:`~bt_api_base.direct_write_protocol.DirectExecutionEvent` rows.

Nothing here decides admission, and no front registry is consulted: the same
path serves simulation and production accounts (D62-01/02).
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
import time
import weakref
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional
from uuid import uuid4

from bt_api_base.direct_cancel_protocol import PreparedCancelRequest
from bt_api_base.direct_place_protocol import PreparedPlaceRequest
from bt_api_base.direct_write_protocol import (
    DirectAccountFacts,
    DirectBalanceFact,
    DirectExecutionEvent,
    DirectExecutionEventKind,
    DirectOrderReservation,
    DirectOrderStatus,
    DirectPositionFact,
    DirectSubmitResult,
    DirectWriteError,
    DirectWriteErrorPolicy,
    NativeOrderIdentity,
    SettlementStatus,
    SettlementObservation,
    SettlementWriteBinding,
    WriteGrant,
    WriteGrantVerifier,
    WriteOperation,
)

from bt_api_ctp import trading_permission_projection as permission_projection
from bt_api_ctp.instrument_position_facts import InstrumentPositionFacts
from bt_api_ctp.trading_permission_facts import UNKNOWN as PERMISSION_UNKNOWN
from bt_api_ctp.trading_permission_facts import (
    InstrumentPermissionFacts,
    PermissionFact,
    TradingPermissionFacts,
)
from bt_api_ctp.native_session_id import is_ctp_session_id

__all__ = (
    "CTP_DIRECT_WRITE_EXCHANGE",
    "CtpDirectWriteProvider",
    "create_ctp_direct_write_provider",
    "project_ctp_execution_event",
    "register_ctp_direct_write_provider",
)

CTP_DIRECT_WRITE_EXCHANGE = "CTP___FUTURE"
_MAX_EVENTS = 50_000
# Seconds between WARNING lines about lost returns (the counter is exact).
_DROPPED_LOG_INTERVAL = 60.0
_RETURN_KINDS = frozenset({"order", "trade", "order_insert_error", "order_action_error"})
_ORDER_REF_DIGITS = 12

_logger = logging.getLogger(__name__)
# Monotonic clock of the readback cooldown/backoff (module alias for tests).
_monotonic = time.monotonic

# Settlement confirmation readback (read-only ReqQrySettlementInfoConfirm).
_SETTLEMENT_READBACK_TIMEOUT = 10.0
# The Gateway runs this readback on its single owner thread, which also serves
# dispatch claims, market and strategy calls. SimNow replies in 0.2-0.9 s or
# never; waiting 10 s blocked the owner (63.09, I63-E110). An unanswered
# readback is "unavailable" and the settlement controller retries it.
_GATEWAY_SETTLEMENT_READBACK_TIMEOUT = 2.0
# Background readback after a same-day re-login (I62-17 revision, 2026-11-06).
# One monitor thread per provider polls the in-memory readiness at this
# interval and retries a failed readback after this backoff (seconds after the
# 1st, 2nd, ... consecutive failure; the last value is the cap).
_READBACK_MONITOR_INTERVAL = 0.5
_READBACK_BACKOFF = (2.0, 5.0, 10.0, 20.0, 30.0)
_READBACK_MONITOR_THREAD_NAME = "ctp-direct-settlement-readback"
_READBACK_MONITOR_JOIN_TIMEOUT = 2.0
# A write runs its own readback only when no attempt ended this recently;
# otherwise it is refused locally (``settlement_readback_pending``).
_READBACK_WRITE_COOLDOWN = 5.0
# Readbacks (background plus write path) per trader session generation.  730
# lets the background schedule alone retry for just over six hours (2+5+10+20
# s, then 30 s apart; the longest CTP continuous session, the night session,
# is 5.5 h), and bounds a write loop that meets the 5 s cooldown to 730
# read-only queries per generation.
_MAX_READBACKS_PER_GENERATION = 730
_READBACK_NOT_SENT_ORIGINS = frozenset({"query_slot_busy", "submit_rejected"})
# Settlement states in which only this session's readback is missing:
# a fresh login, or a confirmation acknowledged without its readback.
_READBACK_SETTLEMENT_STATES = frozenset({"not_requested", "confirmed"})


@dataclass(repr=False)
class _SettlementObservationTicket:
    observation: SettlementObservation
    status: SettlementStatus
    observation_id: str
    trader: Any
    binding: Any
    result: Any
    api: Any
    spi: Any
    epoch: int
    pid: int
    expires_at: float
    consumed: bool = False


@dataclass(repr=False)
class _PreparedCancelTicket:
    prepared: PreparedCancelRequest
    digest: str
    trader: Any
    api: Any
    spi: Any
    session: tuple[Any, ...]
    owner_thread: threading.Thread
    pid: int


@dataclass(repr=False)
class _PreparedPlaceTicket:
    prepared: PreparedPlaceRequest
    digest: str
    trader: Any
    api: Any
    spi: Any
    session: tuple[Any, ...]
    owner_thread: threading.Thread
    pid: int

_DIRECTION = {"0": "buy", "1": "sell"}
_OFFSET = {
    "0": "open",
    "1": "close",
    "2": "close",  # force close
    "3": "close_today",
    "4": "close_yesterday",
    "5": "close",  # force off
    "6": "close",  # local force close
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value).strip()


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    # CTP reports DBL_MAX for unset prices.
    if abs(number) > 1e300:
        return None
    return number


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _order_status(snapshot: dict[str, Any]) -> DirectOrderStatus:
    submit_status = _text(snapshot.get("OrderSubmitStatus"))
    status = _text(snapshot.get("OrderStatus"))
    traded = _number(snapshot.get("VolumeTraded")) or 0.0
    if submit_status == "4":  # insert rejected
        return DirectOrderStatus.REJECTED
    if status == "0":  # all traded
        return DirectOrderStatus.FILLED
    if status == "1":  # part traded, queueing
        return DirectOrderStatus.PARTIALLY_FILLED
    if status in ("2", "5"):  # part traded not queueing / canceled
        return DirectOrderStatus.CANCELLED
    if status == "4":  # no trade, not queueing
        return DirectOrderStatus.CANCELLED if traded == 0 else DirectOrderStatus.PARTIALLY_FILLED
    if status == "3":  # no trade, queueing
        return DirectOrderStatus.ACCEPTED
    if status in ("a", "b", "c"):  # unknown / not touched / touched
        return DirectOrderStatus.SUBMITTED
    return DirectOrderStatus.UNKNOWN



_ACTIVE_ORDER_STATUS = frozenset({"1", "3", "a", "b", "c"})


def _row_value(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _row_mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    names = [name for name in dir(row) if name[:1].isupper()]
    return {name: getattr(row, name, None) for name in names}


def _float_or_none(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


_POSI_DIRECTION = {"1": "net", "2": "long", "3": "short"}


def _project_position_rows(
    rows: Any,
) -> tuple[tuple[DirectPositionFact, ...], tuple[str, ...]]:
    """Aggregate native position rows into neutral facts per symbol/venue/side.

    Zero rows are skipped.  The second value names rows that could not be
    projected although they may hold a position (unknown ``PosiDirection`` or
    unreadable ``Position``); both readers block on them, so such a row can
    never make an account or instrument look flat.
    """
    totals: dict[tuple[str, str, str], list[float]] = {}
    anomalies: list[str] = []
    for row in rows:
        symbol = _text(_row_value(row, "InstrumentID"))
        venue = _text(_row_value(row, "ExchangeID"))
        side = _POSI_DIRECTION.get(_text(_row_value(row, "PosiDirection")))
        raw_quantity = _float_or_none(_row_value(row, "Position"))
        quantity = raw_quantity or 0.0
        if not symbol or side is None or quantity <= 0:
            if symbol and raw_quantity is None:
                anomalies.append("position_row_quantity_unreadable")
            elif symbol and side is None and quantity > 0:
                anomalies.append("position_row_direction_unknown")
            continue
        today = _float_or_none(_row_value(row, "TodayPosition"))
        bucket = totals.setdefault((symbol, venue, side), [0.0, 0.0, 0.0])
        bucket[0] += quantity
        if today is None:
            bucket[2] = 1.0  # split unknown for this key
        else:
            bucket[1] += min(max(today, 0.0), quantity)
    facts = tuple(
        DirectPositionFact(
            symbol=symbol,
            venue=venue or None,
            side=side,
            total=values[0],
            today=None if values[2] else values[1],
            yesterday=None if values[2] else values[0] - values[1],
        )
        for (symbol, venue, side), values in sorted(totals.items())
    )
    return facts, tuple(sorted(set(anomalies)))


_MAX_DROPPED_INSTRUMENTS_REPORTED = 32


def _valid_instrument_filter(value: Any) -> bool:
    # CTP InstrumentID is char[81]; combination ids contain spaces and '&'.
    return (
        type(value) is str
        and 0 < len(value) <= 80
        and value == value.strip()
        and value.isascii()
        and value.isprintable()
    )


def _valid_exchange_filter(value: Any) -> bool:
    # CTP ExchangeID is char[9]: SHFE, DCE, CZCE, CFFEX, INE, GFEX, ...
    return type(value) is str and 0 < len(value) <= 8 and value.isascii() and value.isalnum()


_MAX_PERMISSION_SYMBOLS = 32


def _permission_symbols(symbols: Any) -> tuple[str, ...]:
    """Unique, valid instrument ids in request order (bounded)."""
    if symbols is None or isinstance(symbols, (str, bytes, Mapping)):
        raise DirectWriteError("ctp_trading_permissions_symbols_invalid")
    try:
        items = tuple(symbols)
    except TypeError:
        raise DirectWriteError("ctp_trading_permissions_symbols_invalid") from None
    if not items:
        raise DirectWriteError("ctp_trading_permissions_symbols_invalid")
    if any(not _valid_instrument_filter(item) for item in items):
        raise DirectWriteError("ctp_trading_permissions_symbol_invalid")
    names = tuple(dict.fromkeys(items))
    if len(names) > _MAX_PERMISSION_SYMBOLS:
        raise DirectWriteError("ctp_trading_permissions_too_many_symbols")
    return names


def _permission_venues(exchange_ids: Any, names: tuple[str, ...]) -> dict[str, str]:
    """Caller-given order venue per requested symbol (empty values ignored)."""
    if exchange_ids is None:
        return {}
    if not isinstance(exchange_ids, Mapping):
        raise DirectWriteError("ctp_trading_permissions_exchange_invalid")
    venues: dict[str, str] = {}
    for symbol, venue in exchange_ids.items():
        if symbol not in names:
            raise DirectWriteError("ctp_trading_permissions_exchange_invalid")
        if venue in (None, ""):
            continue
        if not _valid_exchange_filter(venue):
            raise DirectWriteError("ctp_trading_permissions_exchange_invalid")
        venues[symbol] = venue
    return venues


def _query_blocker(result: Any, name: str, generation: Any) -> Optional[str]:
    """``None`` when ``result`` is a complete query of the current session."""
    ok = (
        getattr(result, "complete", False) is True
        and getattr(result, "timed_out", False) is not True
        and getattr(result, "error_code", None) in (None, 0)
        and getattr(result, "connection_generation", generation) == generation
    )
    if ok:
        return None
    detail = "{}:{}:{}:{}".format(
        getattr(result, "error_code", None),
        "timeout" if getattr(result, "timed_out", False) else "",
        "complete" if getattr(result, "complete", False) else "partial",
        str(getattr(result, "error_message", "") or "")[:60],
    )
    return f"{name}_query_incomplete[{detail}]"


def _native_filter_evidence(
    result: Any, symbol: str, venue: Optional[str]
) -> tuple[Optional[bool], Optional[str]]:
    """Read the request-filter readback of a filtered query, if any.

    Returns ``(verified, blocker)``: evidence naming a *different* instrument or
    venue is a blocker; evidence without the filter is ``False``; no evidence
    at all is ``None``.  Local row filtering applies in every case.
    """
    source = getattr(result, "query_source", None)
    if source is None:
        return None, None
    try:
        filters = dict(getattr(source, "request_filters", ()) or ())
    except (TypeError, ValueError):
        return None, "positions_filter_evidence_unreadable"
    expected = {"InstrumentID": symbol}
    if venue is not None:
        expected["ExchangeID"] = venue
    verified = True
    for name, value in expected.items():
        if name not in filters:
            verified = False
        elif filters[name] != value:
            return None, "positions_filter_mismatch"
    return verified, None

def project_ctp_execution_event(
    kind: str, snapshot: dict[str, Any], *, sequence: int
) -> Optional[DirectExecutionEvent]:
    """Convert one native return snapshot into a neutral event (or ``None``)."""

    symbol = _text(snapshot.get("InstrumentID"))
    if not symbol:
        return None
    order_ref = _text(snapshot.get("OrderRef")) or None
    exchange_id = _text(snapshot.get("ExchangeID"))
    trading_day = _text(snapshot.get("TradingDay")) or _text(snapshot.get("_trading_day")) or None
    generation = _int_or_none(snapshot.get("_connection_generation"))
    order_sys_id = _text(snapshot.get("OrderSysID")) or None
    # CTP matches cancels on the exact venue string (right-aligned with
    # spaces); keep it verbatim next to the display form.
    raw_sys = snapshot.get("OrderSysID")
    order_sys_id_raw = (
        str(raw_sys).rstrip("\x00") if raw_sys not in (None, "") and order_sys_id else None
    )
    fields: dict[str, Any] = {"order_ref": order_ref, "exchange_id": exchange_id or None}
    if trading_day:
        fields["trading_day"] = trading_day
    if kind == "order":
        front_id = _int_or_none(snapshot.get("FrontID"))
        session_id = _int_or_none(snapshot.get("SessionID"))
        fields.update(front_id=front_id, session_id=session_id)
        if order_sys_id:
            fields["order_sys_id"] = order_sys_id
            if order_sys_id_raw:
                fields["order_sys_id_raw"] = order_sys_id_raw
        total = _number(snapshot.get("VolumeTotalOriginal"))
        traded = _number(snapshot.get("VolumeTraded"))
        return DirectExecutionEvent(
            kind=DirectExecutionEventKind.ORDER,
            exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
            symbol=symbol,
            identity=NativeOrderIdentity(
                exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                client_order_id=order_ref,
                fields=fields,
                session_generation=generation,
            ),
            exchange_order_id=order_sys_id,
            status=_order_status(snapshot),
            side=_DIRECTION.get(_text(snapshot.get("Direction"))),
            offset=_OFFSET.get(_text(snapshot.get("CombOffsetFlag"))[:1]),
            order_qty=total,
            filled_qty=traded,
            remaining_qty=_number(snapshot.get("VolumeTotal")),
            limit_price=_number(snapshot.get("LimitPrice")),
            trading_day=trading_day,
            raw_status=f"{_text(snapshot.get('OrderSubmitStatus'))}/{_text(snapshot.get('OrderStatus'))}",
            error_message=_text(snapshot.get("StatusMsg")) or None,
            session_generation=generation,
            sequence=sequence,
        )
    if kind == "trade":
        if order_sys_id:
            fields["order_sys_id"] = order_sys_id
            if order_sys_id_raw:
                fields["order_sys_id_raw"] = order_sys_id_raw
        trade_date = _text(snapshot.get("TradeDate"))
        trade_time = _text(snapshot.get("TradeTime"))
        return DirectExecutionEvent(
            kind=DirectExecutionEventKind.TRADE,
            exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
            symbol=symbol,
            identity=NativeOrderIdentity(
                exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                client_order_id=order_ref,
                fields=fields,
                session_generation=generation,
            ),
            exchange_order_id=order_sys_id,
            side=_DIRECTION.get(_text(snapshot.get("Direction"))),
            offset=_OFFSET.get(_text(snapshot.get("OffsetFlag"))[:1]),
            trade_id=_text(snapshot.get("TradeID")) or None,
            trade_price=_number(snapshot.get("Price")),
            trade_qty=_number(snapshot.get("Volume")),
            trade_time=f"{trade_date} {trade_time}".strip() or None,
            trading_day=trading_day,
            session_generation=generation,
            sequence=sequence,
        )
    if kind in ("order_insert_error", "order_action_error"):
        error_code = snapshot.get("_error_code")
        message = _text(snapshot.get("_error_message")) or None
        if kind == "order_insert_error" and snapshot.get("_source") == "OnRspOrderInsert":
            # CThostFtdcInputOrderField has no FrontID/SessionID, so the error
            # alone cannot name the order (OrderRef is unique only within one
            # session).  OnRspOrderInsert answers this session's own request,
            # so this session's login FrontID/SessionID complete the order key.
            # OnErrRtnOrderInsert arrives on the private flow, which other
            # sessions of the account share: it is left without them.
            front_id = _int_or_none(snapshot.get("_login_front_id"))
            session_id = _int_or_none(snapshot.get("_login_session_id"))
            if front_id is not None and session_id is not None:
                fields.update(front_id=front_id, session_id=session_id)
        if kind == "order_action_error":
            fields.update(
                front_id=_int_or_none(snapshot.get("FrontID")),
                session_id=_int_or_none(snapshot.get("SessionID")),
            )
            if order_sys_id:
                fields["order_sys_id"] = order_sys_id
            if order_sys_id_raw:
                fields["order_sys_id_raw"] = order_sys_id_raw
        return DirectExecutionEvent(
            kind=(
                DirectExecutionEventKind.ORDER_ERROR
                if kind == "order_insert_error"
                else DirectExecutionEventKind.CANCEL_ERROR
            ),
            exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
            symbol=symbol,
            identity=NativeOrderIdentity(
                exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                client_order_id=order_ref,
                fields=fields,
                session_generation=generation,
            ),
            exchange_order_id=order_sys_id,
            status=DirectOrderStatus.REJECTED if kind == "order_insert_error" else None,
            error_code=None if error_code in (None, 0) else str(error_code),
            error_message=message,
            trading_day=trading_day,
            session_generation=generation,
            sequence=sequence,
        )
    return None


class CtpDirectWriteProvider:
    """Direct write port for one CTP route (one ``CtpRequestDataFuture``)."""

    direct_write_error_policy = DirectWriteErrorPolicy(
        place_unknown_codes=frozenset({"ctp_direct_submit_outcome_unknown"}),
        cancel_unknown_codes=frozenset(),
    )

    # Venue fact for the SDK direct write coordinator: CTP execution ids
    # (OrderRef, FrontID/SessionID, OrderSysID, TradeID) are unique only within
    # one trading day and are reused on later days, so the SDK de-duplicates
    # returns per trading day.  A projected return names its day through the
    # native ``TradingDay``, else the receiving session's login day (see
    # ``project_ctp_execution_event``); one naming no day is never
    # de-duplicated by the SDK.
    execution_ids_trading_day_scoped = True
    settlement_required = True

    def __init__(
        self, feed: Any, *, catalog_cache: Any = None,
        owner_thread_only: bool = False,
        background_settlement_readback: bool = True,
        allow_implicit_connect: bool = True,
    ) -> None:
        from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

        if type(feed) is not CtpRequestDataFuture:
            raise DirectWriteError("ctp_direct_write_feed_invalid")
        if any(type(value) is not bool for value in (
            owner_thread_only, background_settlement_readback, allow_implicit_connect
        )):
            raise DirectWriteError("ctp_direct_configuration_invalid")
        self._feed = feed
        self._settlement_owner_pid = os.getpid()
        self._owner_thread = threading.current_thread()
        self._owner_thread_only = owner_thread_only
        self._background_settlement_readback = background_settlement_readback
        self._allow_implicit_connect = allow_implicit_connect
        self._bound_gateway_trader = None if allow_implicit_connect else feed.trader_client
        if not allow_implicit_connect and self._bound_gateway_trader is None:
            raise DirectWriteError("ctp_direct_session_unavailable")
        self._owner_closed = False
        self._owner_closing = False
        self._lock = threading.RLock()
        self._events: deque[DirectExecutionEvent] = deque()
        self._events_lock = threading.Lock()
        self._catalog_lock = threading.Lock()
        # Two-level trading-day catalog (D62-11b/11c); the process-wide
        # default cache unless one is injected.
        self._catalog_cache: Any = catalog_cache
        self.catalog_queries = 0
        self.last_catalog_error: Optional[str] = None
        self._sequence = 0
        # Returns lost before any poll (queue overflow, projection failure);
        # guarded by ``_events_lock`` like the time of the last WARNING line.
        self._dropped_events = 0
        self._dropped_logged_at: Optional[float] = None
        self._listening_trader: Any = None
        # Iteration 67.06: the same provider owns a read-only
        # OnRtnInstrumentStatus ledger for the exact current session scope.
        # It is fed only by the trader's native callback payloads and never
        # contacts a provider, reads a clock for ordering or sends an order.
        self._instrument_status_bridge: Any = None
        self._instrument_status_scope: Optional[tuple[str, str, object]] = None
        self._instrument_status_listening_trader: Any = None
        self._instrument_status_listener: Any = None
        # A leaf lock: the status tap runs on the native callback thread and
        # must never wait behind a write or settlement read that holds _lock.
        self._instrument_status_lock = threading.RLock()
        self._instrument_status_closed = False
        self._instrument_status_version = 0
        self._reservations: dict[str, DirectOrderReservation] = {}
        self._cancel_reservations: dict[str, _PreparedCancelTicket] = {}
        self._place_reservations: dict[str, _PreparedPlaceTicket] = {}
        self._settlement_ticket: _SettlementObservationTicket | None = None
        # Gateway settlement I/O can block in native code beyond the Python
        # query timeout.  Reserve a single operation without holding _lock
        # while entering native code.  Ordinary direct mode retains its
        # existing serialization and readback behavior.
        self._gateway_settlement_inflight: object | None = None
        self._cycle_id = f"ctp-direct-{uuid4().hex}"
        self._strategy_identity_sha256 = hashlib.sha256(
            f"ctp-direct-host|{self._cycle_id}".encode("utf-8")
        ).hexdigest()
        self._capability: Any = None
        # Settlement readback state (guarded by ``_lock``): the (trading day,
        # account fingerprint) this provider saw trading-ready, the (trader,
        # generation) that observation came from, and the attempts, last end
        # and next background slot for one (trader, connection generation).
        self._settled_day: Optional[tuple[str, str]] = None
        self._ready_scope: Optional[tuple[Any, int]] = None
        self._readback_scope: Optional[tuple[Any, int]] = None
        self._readback_attempts = 0
        self._readback_failures = 0
        self._readback_last_at: Optional[float] = None
        self._readback_next_at = 0.0
        # Background readback monitor (guarded by ``_monitor_lock``, a leaf
        # lock taken after ``_lock`` and never held while waiting).
        self._monitor_lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop: Optional[threading.Event] = None
        self._monitor_closed = False

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------
    def _check_access(self, *, allow_closed: bool = False) -> None:
        if os.getpid() != self._settlement_owner_pid:
            raise DirectWriteError("ctp_direct_wrong_owner")
        if self._owner_thread_only and threading.current_thread() is not self._owner_thread:
            raise DirectWriteError("ctp_direct_wrong_owner")
        if self._owner_thread_only and (self._owner_closed or self._owner_closing) and not allow_closed:
            raise DirectWriteError("ctp_direct_provider_closed")

    def _check_settlement_thread(self) -> None:
        if self._owner_thread_only and threading.current_thread() is not self._owner_thread:
            raise DirectWriteError("ctp_direct_wrong_owner")
        if self._owner_thread_only and (self._owner_closed or self._owner_closing):
            raise DirectWriteError("ctp_direct_provider_closed")

    def _begin_close(self) -> None:
        self._check_access(allow_closed=True)
        if self._owner_thread_only:
            self._owner_closing = True

    def _trader(self) -> Any:
        feed = self._feed
        if feed.trader_client is None:
            if not self._allow_implicit_connect:
                raise DirectWriteError("ctp_direct_session_unavailable")
            feed.ensure_read_only_connected()
        trader = feed.trader_client
        if trader is None or (
            not self._allow_implicit_connect and trader is not self._bound_gateway_trader
        ):
            raise DirectWriteError("ctp_direct_session_unavailable")
        if trader is not self._listening_trader:
            trader.add_execution_listener(self._on_native_return)
            self._listening_trader = trader
        self._instrument_status_trader(trader)
        return trader

    def _instrument_status_trader(self, trader: Any) -> None:
        """Attach the read-only instrument-status tap once per trader.

        The tap only receives already-built ingress payloads.  A trader without
        the listener contract leaves the port inert; it never falls back to
        reading native state or a calendar.
        """
        with self._instrument_status_lock:
            if self._instrument_status_closed or trader is self._instrument_status_listening_trader:
                return
        add = getattr(trader, "add_instrument_status_listener", None)
        if not callable(add):
            return

        def listener(payload: object, *, _source_generation: object = None,
                     _source_trading_day: object = None, _source_spi: object = None) -> object:
            return self._on_instrument_status_payload(
                payload, trader=trader, source_generation=_source_generation,
                source_trading_day=_source_trading_day, source_spi=_source_spi,
            )

        def source_failed(generation: int, trading_day: object, spi: object) -> None:
            # Native invokes this with its query lock held. Never reacquire it
            # from here; session sampling elsewhere occurs outside this lock.
            with self._instrument_status_lock:
                scope = self._instrument_status_scope
                if (not self._instrument_status_closed
                        and self._instrument_status_listening_trader is trader
                        and self._instrument_status_listener is listener
                        and self._feed.trader_client is trader
                        and scope is not None and scope[:2] == (str(generation), trading_day)
                        and scope[2] is spi and self._instrument_status_bridge is not None):
                    # Invalidate previously issued gateway snapshots even if
                    # an exceptional bridge cannot record its stale state.
                    self._instrument_status_version += 1
                    self._instrument_status_bridge.mark_source_unavailable()

        # Native registration can take _query_state_lock. Never hold our lock
        # across it: managed callbacks may already hold the native lock.
        add_provider = getattr(trader, "_add_instrument_status_provider_listener", None)
        if callable(add_provider):
            add_provider(listener, source_failed)
        else:
            add(listener)
        with self._instrument_status_lock:
            if self._instrument_status_closed or trader is not self._feed.trader_client:
                old_trader, old_listener = trader, listener
            else:
                old_trader = self._instrument_status_listening_trader
                old_listener = self._instrument_status_listener
                self._instrument_status_listening_trader = trader
                self._instrument_status_listener = listener
                self._instrument_status_bridge = None
                self._instrument_status_scope = None
                self._instrument_status_version += 1
        if old_trader is not None and old_listener is not None:
            remove = getattr(old_trader, "remove_instrument_status_listener", None)
            if callable(remove):
                try:
                    remove(old_listener)
                except Exception:
                    _logger.debug("ctp direct old instrument-status listener removal failed")

    # ------------------------------------------------------------------
    # iteration 67.06: read-only instrument-status observation (D67-08 B)
    # ------------------------------------------------------------------
    def _instrument_status_session_facts(self) -> Optional[tuple[str, str, object]]:
        """Return trusted generation, day and original SPI identity or ``None``.

        Both values come from the accepted login session facts the provider
        already uses for order identity.  They are never derived from the
        status callback, its stamp or a clock.
        """
        from bt_api_ctp.instrument_status_observation import _valid_trading_day

        try:
            trader = self._feed.trader_client
            if trader is None:
                return None
            listener = self._instrument_status_listener
            if trader is not self._instrument_status_listening_trader or listener is None:
                return None
            state = trader.get_session_state()
            if not isinstance(state, dict):
                return None
            day = state.get("trading_day")
            generation = state.get("connection_generation")
            if (
                state.get("connected") is not True
                or state.get("read_only_ready") is not True
                or not _valid_trading_day(day)
                or type(generation) is not int
                or generation < 0
            ):
                return None
            health_reader = getattr(trader, "instrument_status_listener_state", None)
            if not callable(health_reader):
                return None
            health = health_reader(listener)
            if (type(health) is not tuple or len(health) != 5
                    or type(health[0]) is not int or health[0] != generation
                    or type(health[1]) is not str or health[1] != day
                    or health[2] is not True or health[3] is not True or health[4] is None):
                return None
            return str(generation), day, health[4]
        except Exception:
            return None

    @staticmethod
    def _same_instrument_status_scope(first: object, second: object) -> bool:
        return (type(first) is tuple and len(first) == 3 and type(second) is tuple and len(second) == 3
                and first[:2] == second[:2] and first[2] is second[2])

    def _instrument_status_switch_scope(self, facts: tuple[str, str, object]) -> Any:
        """Create or advance the ledger for the current trusted scope."""

        from bt_api_ctp.instrument_status_ingress import InstrumentStatusIngressBridge

        with self._instrument_status_lock:
            if self._instrument_status_bridge is None:
                bridge = InstrumentStatusIngressBridge(
                    session_generation=facts[0], trading_day=facts[1]
                )
                self._instrument_status_bridge = bridge
                self._instrument_status_scope = facts
                self._instrument_status_version += 1
                return bridge
            bridge = self._instrument_status_bridge
            previous = self._instrument_status_scope
            if previous is not None and previous[2] is not facts[2]:
                bridge.mark_source_unavailable()
                bridge = InstrumentStatusIngressBridge(session_generation=facts[0], trading_day=facts[1])
                self._instrument_status_bridge = bridge
                self._instrument_status_scope = facts
                self._instrument_status_version += 1
            elif not self._same_instrument_status_scope(previous, facts):
                bridge.begin_session_generation(facts[0], facts[1])
                self._instrument_status_scope = facts
                self._instrument_status_version += 1
            return bridge

    @staticmethod
    def _instrument_status_old_payload(
        payload: object, generation: str, *, check_generation: bool = True
    ) -> bool:
        """Ignore only a complete, independently validated old native record."""
        from bt_api_ctp.ctp.callback_ingress import CtpTraderCallbackPhase
        from bt_api_ctp.instrument_status_ingress import _record_from_snapshot, _snapshot_payload

        try:
            record = _record_from_snapshot(_snapshot_payload(payload))
            return (record.callback_name == "OnRtnInstrumentStatus" and record.capture_complete
                    and ((check_generation and str(record.connection_generation) != generation)
                         or record.source_phase is not CtpTraderCallbackPhase.ACTIVE))
        except Exception:
            return False

    def _on_instrument_status_payload(
        self, payload: object, *, trader: Any, source_generation: object = None,
        source_trading_day: object = None, source_spi: object = None,
    ) -> object:
        """Consume one native ingress payload; never raise into the callback."""

        version = None
        facts = None
        try:
            for _ in range(3):
                with self._instrument_status_lock:
                    if (
                        self._instrument_status_closed
                        or trader is not self._instrument_status_listening_trader
                        or trader is not self._feed.trader_client
                    ):
                        return
                    version = self._instrument_status_version
                facts = self._instrument_status_session_facts()
                with self._instrument_status_lock:
                    if (
                        self._instrument_status_closed
                        or trader is not self._instrument_status_listening_trader
                        or trader is not self._feed.trader_client
                    ):
                        return
                    if version != self._instrument_status_version:
                        continue
                    bridge = self._instrument_status_bridge
                    if facts is None:
                        if (source_generation is not None and self._instrument_status_scope is not None
                                and ((str(source_generation), source_trading_day)
                                     != self._instrument_status_scope[:2]
                                     or source_spi is not self._instrument_status_scope[2])):
                            return
                        if bridge is not None:
                            bridge.mark_source_unavailable()
                            self._instrument_status_version += 1
                        return
                    if source_generation is not None and (
                        type(source_generation) is not int
                        or (str(source_generation), source_trading_day) != facts[:2]
                        or source_spi is not facts[2]
                    ):
                        return
                    bridge = self._instrument_status_switch_scope(facts)
                    version = self._instrument_status_version
                    result = bridge.observe_payload(payload)
                    if (not result.applied and result.action not in {"duplicate", "out_of_order"}
                            and not self._instrument_status_old_payload(
                                payload, facts[0], check_generation=source_generation is None)):
                        bridge.mark_source_unavailable()
                    self._instrument_status_version += 1
                    version = self._instrument_status_version
                current = self._instrument_status_session_facts()
                with self._instrument_status_lock:
                    if (
                        version != self._instrument_status_version
                        or trader is not self._instrument_status_listening_trader
                    ):
                        return
                    if current is None:
                        bridge.mark_source_unavailable()
                        self._instrument_status_version += 1
                    elif not self._same_instrument_status_scope(current, facts):
                        self._instrument_status_switch_scope(current)
                return
            with self._instrument_status_lock:
                if (
                    trader is self._instrument_status_listening_trader
                    and trader is self._feed.trader_client
                    and not self._instrument_status_closed
                    and self._instrument_status_bridge is not None
                    and self._instrument_status_scope is not None
                    and (source_generation is None or (
                        type(source_generation) is int
                        and (str(source_generation), source_trading_day) == self._instrument_status_scope[:2]
                        and source_spi is self._instrument_status_scope[2]))
                ):
                    # Revalidate ordering against the current ledger. Only a
                    # newer candidate can have been lost during contention.
                    # Unavailable remains sticky for this scope; a new session
                    # generation/day or trader binding is needed to recover.
                    bridge = self._instrument_status_bridge
                    facts = self._instrument_status_scope
                    version = self._instrument_status_version
                    result = bridge.observe_payload(payload)
                    if (result.applied or (result.action not in {"duplicate", "out_of_order"}
                            and not self._instrument_status_old_payload(
                                payload, self._instrument_status_scope[0],
                                check_generation=source_generation is None))):
                        bridge.mark_source_unavailable()
                        self._instrument_status_version += 1
        except BaseException:
            # A callback thread must never surface provider text or an
            # exception. Only the still-matching version and scope may be
            # invalidated: a newer callback may already have advanced them.
            try:
                with self._instrument_status_lock:
                    scope = self._instrument_status_scope
                    if (source_generation is None and scope is not None
                            and version == self._instrument_status_version
                            and (facts is None or self._same_instrument_status_scope(scope, facts))
                            and trader is self._instrument_status_listening_trader
                            and trader is self._feed.trader_client
                            and not self._instrument_status_closed
                            and self._instrument_status_bridge is not None):
                        self._instrument_status_bridge.mark_source_unavailable()
                        self._instrument_status_version += 1
            except BaseException:
                pass
            try:
                _logger.debug("ctp direct instrument-status callback was not usable")
            except BaseException:
                pass
            return False

    def instrument_status_view(self, venue: object, native_symbol: object) -> Any:
        """Return the provider ledger's view for one identity.

        ``None`` means this provider has no proven session scope yet, which
        callers must treat as an explicit absence.  A previously usable bridge
        whose session facts can no longer be read is marked unavailable so its
        observations become stale instead of being reused.
        """
        for _ in range(3):
            with self._instrument_status_lock:
                bridge = self._instrument_status_bridge
                if self._instrument_status_closed:
                    return None if bridge is None else bridge.view(venue, native_symbol)
                version = self._instrument_status_version
            facts = self._instrument_status_session_facts()
            with self._instrument_status_lock:
                if version != self._instrument_status_version:
                    continue
                bridge = self._instrument_status_bridge
                if facts is None:
                    if bridge is None:
                        return None
                    bridge.mark_source_unavailable()
                    self._instrument_status_version += 1
                    return bridge.view(venue, native_symbol)
                bridge = self._instrument_status_switch_scope(facts)
                view = bridge.view(venue, native_symbol)
                version = self._instrument_status_version
            current = self._instrument_status_session_facts()
            with self._instrument_status_lock:
                if version != self._instrument_status_version:
                    continue
                if current is None:
                    bridge.mark_source_unavailable()
                    self._instrument_status_version += 1
                    return bridge.view(venue, native_symbol)
                if not self._same_instrument_status_scope(current, facts):
                    bridge = self._instrument_status_switch_scope(current)
                    return bridge.view(venue, native_symbol)
                return view
        return None

    def ensure_listening(self) -> None:
        """Connect the TD session (read-only login) and attach the return tap."""
        self._check_access()

        with self._lock:
            self._trader()

    def session_generation(self) -> Optional[int]:
        self._check_access()
        trader = self._feed.trader_client
        if trader is None:
            return None
        try:
            return int(trader.connection_generation)
        except Exception:
            return None

    def trading_day(self) -> Optional[str]:
        self._check_access()
        trader = self._feed.trader_client
        if trader is None:
            return None
        state = trader.get_session_state()
        value = state.get("trading_day") if isinstance(state, dict) else None
        return str(value) if value else None

    def login_clock_observation(self) -> Optional[dict[str, Any]]:
        self._check_access()
        trader = self._feed.trader_client
        method = getattr(trader, "login_clock_observation", None)
        return method() if callable(method) else None

    def _session_fields(self, trader: Any) -> dict[str, Any]:
        state = trader.get_session_state()
        if not isinstance(state, dict):
            raise DirectWriteError("ctp_direct_session_unavailable")
        front_id = _int_or_none(state.get("front_id"))
        session_id = _int_or_none(state.get("session_id"))
        trading_day = _text(state.get("trading_day"))
        generation = _int_or_none(state.get("connection_generation"))
        if front_id is None or session_id is None or not trading_day or generation is None:
            raise DirectWriteError("ctp_direct_session_unavailable")
        return {
            "front_id": front_id,
            "session_id": session_id,
            "trading_day": trading_day,
            "connection_generation": generation,
        }

    # ------------------------------------------------------------------
    # admission / gate (native mechanism kept as an execution detail)
    # ------------------------------------------------------------------
    def _admitted_capability(self, trader: Any) -> Any:
        from bt_api_ctp.ctp import client as ctp_client

        feed = self._feed
        if not feed.sdk_write_admission_active():
            feed.activate_sdk_write_admission()
        else:
            trader.record_sdk_write_admission()
        capability = feed._execution_gate_capability
        if capability is None:
            capability = feed._issue_execution_capability_for_core()
            feed.configure_execution_gate(capability)
        if not ctp_client._is_ctp_core_execution_authority(capability):
            raise DirectWriteError("ctp_direct_capability_invalid")
        self._capability = capability
        return capability

    def _ensure_armed(self, trader: Any, capability: Any, symbol: str, exchange_id: str) -> None:
        from bt_api_ctp.ctp import client as ctp_client

        instrument = ctp_client.canonical_ctp_instrument(symbol, exchange_id or "")
        if not instrument:
            raise DirectWriteError("ctp_direct_instrument_invalid")
        state = trader.get_execution_gate_state()
        if isinstance(state, dict) and state.get("armed") is True:
            armed = state.get("authorized_instruments") or [state.get("instrument")]
            if instrument in armed and state.get("environment_profile") in (
                None,
                ctp_client.SDK_ADMITTED_PROFILE,
            ):
                return
            trader.disarm_execution_gate(capability, "ctp_direct_instrument_switch")
        preflight = hashlib.sha256(
            "|".join(("ctp-direct-preflight", self._cycle_id, instrument, str(time.time_ns()))).encode(
                "utf-8"
            )
        ).hexdigest()
        trader.arm_execution_for_sdk_admission(
            capability=capability,
            instrument_id=symbol,
            exchange_id=exchange_id or "",
            strategy_identity_sha256=self._strategy_identity_sha256,
            execution_cycle_id=self._cycle_id,
            preflight_sha256=preflight,
        )

    def _note_trading_ready(self, trader: Any, state: Any = None) -> None:
        """Remember the trading day and account seen trading-ready (lock held).

        Observation only: a failed state read records nothing and never
        changes the caller's result.  A recorded day also starts the
        background readback monitor (at most one per provider).
        """
        if state is None:
            try:
                state = trader.get_session_state()
            except Exception:
                return
        if not isinstance(state, dict) or state.get("trading_ready") is not True:
            return
        day = _text(state.get("trading_day"))
        account = _text(state.get("account_fingerprint"))
        if day and account:
            self._settled_day = (day, account)
            generation = _int_or_none(state.get("connection_generation"))
            self._ready_scope = None if generation is None else (trader, generation)
            self._ensure_readback_monitor()

    def _note_ready_generation(self, trader: Any) -> None:
        """Hot path of a trading-ready write (lock held).

        The day and account cannot change within one session generation, so
        the full session state is read only when the generation is new.
        """
        try:
            generation = trader.connection_generation
        except Exception:
            return
        scope = self._ready_scope
        if scope is not None and scope[0] is trader and scope[1] == generation:
            return
        self._note_trading_ready(trader)

    def _readback_candidate(self, trader: Any) -> Optional[int]:
        """Generation whose settlement readback may run now, else None (lock held).

        Only the same-day re-login qualifies: logged in, not trading-ready,
        only this session's readback missing, and this provider already saw
        the same account trading-ready on the same trading day.  A new or
        never-verified trading day and another account are left to the
        explicit SDK settlement step (R-3); any other not-ready state is left
        to the native gate, unchanged.
        """
        state = trader.get_session_state()
        if not isinstance(state, dict):
            return None
        if state.get("trading_ready") is True:
            self._note_trading_ready(trader, state)
            return None
        day = _text(state.get("trading_day"))
        account = _text(state.get("account_fingerprint"))
        generation = _int_or_none(state.get("connection_generation"))
        if (
            state.get("read_only_ready") is not True
            or state.get("settlement_state") not in _READBACK_SETTLEMENT_STATES
            or generation is None
            or not day
            or not account
            or self._settled_day != (day, account)
        ):
            return None
        return generation

    def _enter_readback_scope(self, trader: Any, generation: int) -> None:
        """Start a fresh attempt cycle for a new (trader, generation) (lock held)."""
        scope = self._readback_scope
        if scope is None or scope[0] is not trader or scope[1] != generation:
            self._readback_scope = (trader, generation)
            self._readback_attempts = 0
            self._readback_failures = 0
            self._readback_last_at = None
            self._readback_next_at = 0.0

    def _run_settlement_readback(self, trader: Any, should_stop: Any = None) -> Optional[str]:
        """Run one bounded, read-only readback (lock held); None when ready.

        Settlement is never confirmed here (an explicit SDK step, D62-20/29).
        A failure schedules the next background attempt on the backoff and
        returns a fixed, value-free detail.
        """
        self._readback_attempts += 1
        try:
            if should_stop is None:
                result = trader.verify_settlement_confirmation(
                    timeout=_SETTLEMENT_READBACK_TIMEOUT
                )
            else:
                result = trader.verify_settlement_confirmation(
                    timeout=_SETTLEMENT_READBACK_TIMEOUT, should_stop=should_stop
                )
        except Exception:
            result = None
        try:
            ready = trader.is_trading_ready is True
        except Exception:
            ready = False
        now = _monotonic()
        self._readback_last_at = now
        if ready:
            self._readback_failures = 0
            self._readback_next_at = 0.0
            self._note_trading_ready(trader)
            return None
        step = min(self._readback_failures, len(_READBACK_BACKOFF) - 1)
        self._readback_failures += 1
        self._readback_next_at = now + _READBACK_BACKOFF[step]
        if getattr(result, "completion_origin", None) in _READBACK_NOT_SENT_ORIGINS:
            return "settlement_readback_not_sent"
        if (
            getattr(result, "complete", False) is True
            and getattr(result, "timed_out", False) is not True
            and getattr(result, "error_code", None) in (None, 0)
        ):
            return "settlement_readback_unconfirmed"
        return "settlement_readback_incomplete"

    def _settlement_readback_for_write(self, trader: Any) -> bool:
        """Write-path fallback of the same-day settlement readback (lock held).

        Every CTP login resets readiness until the session reads back the
        trading day's settlement confirmation.  The background monitor
        normally restores it before the next write; a background readback in
        flight holds ``_lock``, so a write waits for it (bounded by the
        readback timeout) and then re-checks here without a second query.

        Trading-ready: go (no session-state read).  Same-day re-login and no
        attempt ended within the cooldown: one inline readback.  Otherwise
        refuse locally with ``ctp_direct_settlement_unverified`` /
        ``settlement_readback_pending``; at the per-generation cap with
        ``settlement_readback_not_repeated``.  Returns True when a readback
        ran (the caller re-reads the session); any other not-ready state is
        left to the native gate, unchanged.
        """
        try:
            ready = trader.is_trading_ready is True
        except Exception:
            ready = False
        if ready:
            self._note_ready_generation(trader)
            return False
        generation = self._readback_candidate(trader)
        if generation is None:
            return False
        self._enter_readback_scope(trader, generation)
        if self._readback_attempts >= _MAX_READBACKS_PER_GENERATION:
            raise DirectWriteError(
                "ctp_direct_settlement_unverified", "settlement_readback_not_repeated"
            )
        last = self._readback_last_at
        if last is not None and _monotonic() - last < _READBACK_WRITE_COOLDOWN:
            raise DirectWriteError(
                "ctp_direct_settlement_unverified", "settlement_readback_pending"
            )
        detail = self._run_settlement_readback(trader)
        if detail is None:
            return True
        raise DirectWriteError("ctp_direct_settlement_unverified", detail)

    # ------------------------------------------------------------------
    # background settlement readback (I62-17 revision)
    # ------------------------------------------------------------------
    def _ensure_readback_monitor(self) -> None:
        """Start the provider's monitor thread unless running or closed."""
        if not self._background_settlement_readback:
            return
        with self._monitor_lock:
            if self._monitor_closed or self._monitor_thread is not None:
                return
            stop = threading.Event()
            thread = threading.Thread(
                target=_settlement_readback_monitor,
                args=(weakref.ref(self), stop),
                name=_READBACK_MONITOR_THREAD_NAME,
                daemon=True,
            )
            self._monitor_thread = thread
            self._monitor_stop = stop
            thread.start()

    def _readback_monitor_exited(self, thread: threading.Thread) -> None:
        with self._monitor_lock:
            if self._monitor_thread is thread:
                self._monitor_thread = None
                self._monitor_stop = None

    def _readback_monitor_tick(self, stop: threading.Event) -> bool:
        """One monitor step; False ends the thread.

        Reads only in-memory readiness (never connects, never confirms).  The
        readback runs under ``_lock`` so writes and the explicit settlement
        steps are serialised with it; the lock is only tried for one interval
        so ``close`` is never held up by a long write.
        """
        trader = self._feed.trader_client
        if trader is None:
            return False  # the feed released its session
        try:
            if trader.is_trading_ready is True:
                return True
        except Exception:
            return True
        if not self._lock.acquire(timeout=_READBACK_MONITOR_INTERVAL):
            return True  # a write or settlement step holds the lock
        try:
            if stop.is_set():
                return False
            if self._feed.trader_client is not trader:
                return True
            generation = self._readback_candidate(trader)
            if generation is None:
                return True
            self._enter_readback_scope(trader, generation)
            if (
                self._readback_attempts >= _MAX_READBACKS_PER_GENERATION
                or _monotonic() < self._readback_next_at
            ):
                return True
            self._run_settlement_readback(trader, stop.is_set)
            return True
        finally:
            self._lock.release()

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    def reserve_order(self, request: Any) -> DirectOrderReservation:
        self._check_access()
        from bt_api_ctp.order_request_mapping import map_order_request

        # Unrepresentable requests (order type, validity, offset intent) fail
        # here, before any native identity or host marker exists (D62-33).
        try:
            map_order_request(request)
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            raise DirectWriteError("ctp_direct_request_unrepresentable", str(detail)[:200]) from None
        with self._lock:
            trader = self._trader()
            session = self._session_fields(trader)
            raw = int(trader.next_order_ref())
            order_ref = f"{raw:0{_ORDER_REF_DIGITS}d}"
            if len(order_ref) != _ORDER_REF_DIGITS:
                raise DirectWriteError("ctp_direct_order_ref_exhausted")
            exchange_id = _text(getattr(request, "exchange_id", None))
            if not exchange_id:
                exchange_id = self.resolve_exchange_id(_text(getattr(request, "symbol", None)))
            if not exchange_id:
                raise DirectWriteError("ctp_direct_exchange_id_required")
            identity = NativeOrderIdentity(
                exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                client_order_id=order_ref,
                fields={
                    "order_ref": order_ref,
                    "front_id": session["front_id"],
                    "session_id": session["session_id"],
                    "trading_day": session["trading_day"],
                    "exchange_id": exchange_id,
                },
                session_generation=session["connection_generation"],
            )
            reservation = DirectOrderReservation(
                identity=identity,
                symbol=_text(request.symbol),
                reservation_id=uuid4().hex,
            )
            self._reservations[reservation.reservation_id] = reservation
            return reservation

    def release_reservation(self, reservation: DirectOrderReservation) -> None:
        self._check_access()
        with self._lock:
            self._reservations.pop(reservation.reservation_id, None)

    def submit_order(
        self,
        reservation: DirectOrderReservation,
        request: Any,
        grant: WriteGrant,
        verifier: WriteGrantVerifier,
    ) -> DirectSubmitResult:
        self._check_access()
        from bt_api_ctp.ctp import client as ctp_client
        from bt_api_ctp.errors import CtpOrderSubmissionOutcomeUnknown
        from bt_api_ctp.order_request_mapping import map_order_request

        with self._lock:
            if self._reservations.pop(reservation.reservation_id, None) is None:
                raise DirectWriteError("ctp_direct_reservation_unknown")
            trader = self._trader()
            session = self._session_fields(trader)
            fields = reservation.identity.fields
            if (
                session["front_id"] != fields.get("front_id")
                or session["session_id"] != fields.get("session_id")
                or session["connection_generation"] != reservation.identity.session_generation
            ):
                raise DirectWriteError("ctp_direct_reservation_session_changed")
            mapped = map_order_request(request)
            exchange_id = _text(fields.get("exchange_id"))
            if self._settlement_readback_for_write(trader):
                session = self._session_fields(trader)
                if (
                    session["front_id"] != fields.get("front_id")
                    or session["session_id"] != fields.get("session_id")
                    or session["connection_generation"] != reservation.identity.session_generation
                ):
                    raise DirectWriteError("ctp_direct_reservation_session_changed")
            verifier.consume(
                grant,
                exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                operation=WriteOperation.PLACE,
                session_generation=session["connection_generation"],
            )
            try:
                capability = self._admitted_capability(trader)
                self._ensure_armed(trader, capability, mapped["symbol"], exchange_id)
            except (ctp_client.CtpExecutionGateError, ValueError) as exc:
                raise DirectWriteError(
                    getattr(exc, "code", None) or "ctp_direct_gate_unavailable", str(exc)
                ) from None
            try:
                result = self._feed._make_order_native(
                    mapped["symbol"],
                    mapped["vol"],
                    mapped["price"],
                    mapped["order_type"],
                    mapped["offset"],
                    False,
                    reservation.identity.client_order_id,
                    None,
                    _execution_capability=capability,
                    exchange_id=exchange_id,
                    time_in_force=mapped.get("time_in_force", "GFD"),
                )
            except (ctp_client.CtpExecutionGateError, ValueError) as exc:
                # Raised before ReqOrderInsert: definitely not sent.
                return DirectSubmitResult(
                    sent=False,
                    identity=reservation.identity,
                    error_code=getattr(exc, "code", None) or "ctp_direct_not_sent",
                    detail=str(exc)[:200],
                )
            except CtpOrderSubmissionOutcomeUnknown:
                raise DirectWriteError("ctp_direct_submit_outcome_unknown") from None
            status = result.get_status() if hasattr(result, "get_status") else None
            if status is True:
                return DirectSubmitResult(sent=True, identity=reservation.identity)
            return DirectSubmitResult(
                sent=False,
                identity=reservation.identity,
                error_code="ctp_native_insert_returned_nonzero",
            )

    def prepare_order(self, request: Any) -> PreparedPlaceRequest:
        """Reserve exact CTP place identity and fields without network or query I/O."""
        self._prepared_cancel_access()
        from bt_api_ctp.ctp.client import canonical_ctp_instrument
        from bt_api_ctp.execution_instrument import canonical_ctp_bundle_wire_instrument
        from bt_api_ctp.feeds.live_ctp_feed import (
            CTP_DIRECTION_FLAG,
            CTP_OFFSET_FLAG,
            _positive_ctp_price,
            _positive_int_lot,
        )
        from bt_api_ctp.gateway_place_source import capture_place_source_locked, place_attempt_key
        from bt_api_ctp.order_request_mapping import map_order_request

        try:
            exchange = getattr(request, "exchange_id", None)
            symbol = getattr(request, "symbol", None)
            hedge_flag = getattr(request, "hedge_flag", None)
            hedge_class = getattr(request, "hedge_class", None)
            quantity_unit = getattr(request, "quantity_unit", None)
            if (
                type(exchange) is not str
                or not exchange
                or exchange != exchange.strip()
                or type(symbol) is not str
                or not symbol
                or symbol != symbol.strip()
                or not canonical_ctp_instrument(symbol, exchange)
                or not canonical_ctp_bundle_wire_instrument(symbol, exchange)
                or (hedge_flag is not None and (type(hedge_flag) is not str or hedge_flag != "1"))
                or (
                    hedge_class is not None
                    and (type(hedge_class) is not str or hedge_class != "speculation")
                )
                or type(quantity_unit) is not str
                or quantity_unit not in {"contracts", "lots", "native"}
            ):
                raise ValueError
            mapped = map_order_request(request)
            side, order_kind = mapped["order_type"].split("-", 1)
            if (
                order_kind != "limit"
                or side not in CTP_DIRECTION_FLAG
                or mapped["offset"] not in CTP_OFFSET_FLAG
            ):
                raise ValueError
            quantity = _positive_int_lot(mapped["vol"], "volume")
            if not 0 < quantity < 2**31:
                raise ValueError
            price = _positive_ctp_price(mapped["price"])
            if type(price) is not float or not math.isfinite(price) or price <= 0:
                raise ValueError
            price_hex = price.hex()
            if float.fromhex(price_hex).hex() != price_hex:
                raise ValueError
        except Exception:
            raise DirectWriteError("ctp_prepared_place_request_invalid") from None
        with self._lock:
            if self._owner_closing or self._owner_closed or self._monitor_closed:
                raise DirectWriteError("ctp_direct_provider_closed")
            trader = self._feed.trader_client
            if trader is None or getattr(trader, "_query_state_lock", None) is None:
                raise DirectWriteError("ctp_prepared_place_session_unavailable")
            with trader._query_state_lock:
                try:
                    scope = self._cancel_session_locked(trader)
                    api, spi = trader._api, trader._spi
                    source = capture_place_source_locked(
                        trader, environment=trader._gateway_return_environment
                    )
                    if (
                        self._cancel_session_locked(trader) != scope
                        or trader._api is not api
                        or trader._spi is not spi
                    ):
                        raise ValueError
                    if len(self._place_reservations) >= 8192:
                        raise DirectWriteError("ctp_prepared_place_capacity")
                    broker, investor = scope[:2]
                    if (
                        any(
                            len(value.encode("utf-8", errors="strict")) > 256
                            for value in (broker, investor)
                        )
                        or type(trader._max_order_ref) is not int
                        or not 0 <= trader._max_order_ref < 999_999_999_999
                        or type(trader._req_id) is not int
                        or not 0 <= trader._req_id < 2**31 - 1
                    ):
                        raise ValueError
                    raw_ref = trader.next_order_ref()
                    if type(raw_ref) is not str or not raw_ref.isascii() or not raw_ref.isdigit():
                        raise ValueError
                    order_ref = f"{int(raw_ref):012d}"
                    request_id = trader._next_request_id()
                    if (
                        len(order_ref) != 12
                        or type(request_id) is not int
                        or not 0 < request_id < 2**31
                    ):
                        raise ValueError
                    if (
                        self._cancel_session_locked(trader) != scope
                        or trader._api is not api
                        or trader._spi is not spi
                        or capture_place_source_locked(
                            trader, environment=trader._gateway_return_environment
                        )
                        != source
                    ):
                        raise ValueError
                except DirectWriteError:
                    raise
                except Exception:
                    raise DirectWriteError("ctp_prepared_place_session_unavailable") from None
                broker, investor, _, day, generation, front, session = scope[:7]
                identity = NativeOrderIdentity(
                    CTP_DIRECT_WRITE_EXCHANGE,
                    order_ref,
                    {
                        "order_ref": order_ref,
                        "front_id": front,
                        "session_id": session,
                        "trading_day": day,
                        "exchange_id": exchange,
                    },
                    generation,
                )
                fields = {
                    "BrokerID": broker,
                    "InvestorID": investor,
                    "UserID": investor,
                    "InstrumentID": symbol,
                    "ExchangeID": exchange,
                    "Direction": CTP_DIRECTION_FLAG[side],
                    "CombOffsetFlag": CTP_OFFSET_FLAG[mapped["offset"]],
                    "CombHedgeFlag": "1",
                    "VolumeTotalOriginal": quantity,
                    "MinVolume": 1,
                    "ForceCloseReason": "0",
                    "IsAutoSuspend": 0,
                    "UserForceClose": 0,
                    "ContingentCondition": "1",
                    "OrderPriceType": "2",
                    "TimeCondition": "3",
                    "VolumeCondition": "1",
                    "LimitPrice": price_hex,
                    "OrderRef": order_ref,
                    "RequestID": request_id,
                }
                key = place_attempt_key(
                    broker=broker,
                    investor=investor,
                    day=day,
                    front=front,
                    session=session,
                    request_id=request_id,
                    order_ref=order_ref,
                    source_binding=source,
                )
                prepared = PreparedPlaceRequest(
                    uuid4().hex, identity, symbol, day, generation, fields, (key,), source
                )
                self._place_reservations[prepared.reservation_id] = _PreparedPlaceTicket(
                    prepared,
                    prepared.digest(),
                    trader,
                    api,
                    spi,
                    scope,
                    threading.current_thread(),
                    os.getpid(),
                )
                return prepared

    def _place_ticket(self, prepared: PreparedPlaceRequest) -> _PreparedPlaceTicket:
        if type(prepared) is not PreparedPlaceRequest:
            raise DirectWriteError("ctp_prepared_place_unknown")
        ticket = self._place_reservations.get(prepared.reservation_id)
        try:
            valid = (
                ticket is not None
                and ticket.prepared is prepared
                and ticket.pid == os.getpid()
                and ticket.owner_thread is threading.current_thread()
                and prepared.digest() == ticket.digest
            )
        except Exception:
            valid = False
        if not valid:
            raise DirectWriteError("ctp_prepared_place_unknown")
        return ticket

    def release_prepared_order(self, prepared: PreparedPlaceRequest) -> None:
        self._prepared_cancel_access()
        with self._lock:
            self._place_ticket(prepared)
            del self._place_reservations[prepared.reservation_id]

    def submit_prepared_order(
        self, prepared: PreparedPlaceRequest, grant: WriteGrant, verifier: WriteGrantVerifier,
        *, native_market_recheck=None,
    ) -> DirectSubmitResult:
        self._prepared_cancel_access()
        from bt_api_ctp.ctp import client as ctp_client
        from bt_api_ctp.ctp.ctp_structs_order import CThostFtdcInputOrderField
        from bt_api_ctp.gateway_place_source import capture_place_source_locked

        with self._lock:
            ticket = self._place_ticket(prepared)
            del self._place_reservations[prepared.reservation_id]
        trader = ticket.trader
        outcome = ctp_client._CtpNativeRequestDispatchOutcome()
        try:
            if native_market_recheck is not None and not callable(native_market_recheck):
                raise ValueError("invalid native market recheck")
            with trader._query_state_lock:
                if (
                    self._cancel_session_locked(trader) != ticket.session
                    or trader._api is not ticket.api
                    or trader._spi is not ticket.spi
                    or trader.is_trading_ready is not True
                    or self._owner_closing
                    or self._owner_closed
                    or self._monitor_closed
                ):
                    raise ValueError
            capability = self._admitted_capability(trader)
            self._ensure_armed(
                trader, capability, prepared.symbol, prepared.request_fields["ExchangeID"]
            )
            field = CThostFtdcInputOrderField()
            native_fields = dict(prepared.request_fields)
            price_hex = native_fields["LimitPrice"]
            price = float.fromhex(price_hex)
            if (
                type(price_hex) is not str
                or not math.isfinite(price)
                or price <= 0
                or price.hex() != price_hex
            ):
                raise ValueError
            for name, value in native_fields.items():
                setattr(field, name, price if name == "LimitPrice" else value)

            def check_source_and_fields() -> None:
                if (
                    os.getpid() != ticket.pid
                    or threading.current_thread() is not ticket.owner_thread
                    or self._owner_closing
                    or self._owner_closed
                    or self._monitor_closed
                    or self._cancel_session_locked(trader) != ticket.session
                    or trader._api is not ticket.api
                    or trader._spi is not ticket.spi
                    or trader.is_trading_ready is not True
                    or prepared.digest() != ticket.digest
                    or capture_place_source_locked(
                        trader, environment=trader._gateway_return_environment
                    )
                    != dict(prepared.source_binding)
                ):
                    raise ValueError
                for name, expected in native_fields.items():
                    actual = getattr(field, name)
                    if name == "LimitPrice":
                        if (
                            type(actual) is not float
                            or not math.isfinite(actual)
                            or actual.hex() != expected
                        ):
                            raise ValueError
                    elif type(actual) is not type(expected) or actual != expected:
                        raise ValueError

            def final_check() -> None:
                check_source_and_fields()
                verifier.consume(
                    grant,
                    exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                    operation=WriteOperation.PLACE,
                    session_generation=prepared.session_generation,
                )
                check_source_and_fields()
                if native_market_recheck is not None:
                    if native_market_recheck() is False:
                        raise ValueError("native market recheck failed")
                    check_source_and_fields()

            ret = trader.submit_order_insert(
                field,
                native_fields["RequestID"],
                execution_capability=capability,
                _prepared_place_pre_dispatch=final_check,
                _prepared_place_dispatch_outcome=outcome,
            )
        except Exception:
            return DirectSubmitResult(
                None if outcome.native_call_started else False,
                prepared.identity,
                "ctp_prepared_place_outcome_unknown"
                if outcome.native_call_started
                else "ctp_prepared_place_not_sent",
            )
        if type(ret) is not int:
            return DirectSubmitResult(None, prepared.identity, "ctp_prepared_place_outcome_unknown")
        return DirectSubmitResult(
            ret == 0, prepared.identity, None if ret == 0 else "ctp_native_insert_returned_nonzero"
        )

    def _prepared_cancel_access(self) -> None:
        self._check_access()
        if threading.current_thread() is not self._owner_thread:
            raise DirectWriteError("ctp_direct_wrong_owner")

    def assert_cancel_ready(self, *, expected_account_binding: str | None = None) -> None:
        """Check an existing login, optionally against a caller-pinned account binding.

        The expected value is a caller-established consistency check; neither
        it nor a successful observation grants cancellation authority.
        """
        self._prepared_cancel_access()
        if expected_account_binding is not None:
            try:
                if (
                    type(expected_account_binding) is not str
                    or not expected_account_binding
                    or len(expected_account_binding) > 256
                    or expected_account_binding != expected_account_binding.strip()
                    or not expected_account_binding.isprintable()
                    or len(expected_account_binding.encode("utf-8", errors="strict")) > 256
                ):
                    raise ValueError
            except (ValueError, UnicodeError):
                raise DirectWriteError("ctp_prepared_cancel_session_unavailable") from None
        with self._lock:
            self._prepared_cancel_access()
            if self._owner_closing or self._owner_closed or self._monitor_closed:
                raise DirectWriteError("ctp_direct_provider_closed")
            trader = self._feed.trader_client
            if trader is None or getattr(trader, "_query_state_lock", None) is None:
                raise DirectWriteError("ctp_prepared_cancel_session_unavailable")
            try:
                with trader._query_state_lock:
                    if self._owner_closing or self._owner_closed or self._monitor_closed:
                        raise DirectWriteError("ctp_direct_provider_closed")
                    scope = self._cancel_session_locked(trader)
                    if self._owner_closing or self._owner_closed or self._monitor_closed:
                        raise DirectWriteError("ctp_direct_provider_closed")
                    if expected_account_binding is not None and scope[2] != expected_account_binding:
                        raise DirectWriteError("ctp_prepared_cancel_session_unavailable")
            except DirectWriteError:
                raise
            except Exception:
                raise DirectWriteError("ctp_prepared_cancel_session_unavailable") from None

    def _cancel_session_locked(self, trader: Any) -> tuple[Any, ...]:
        """Capture the exact current login without connecting or issuing requests."""
        feed = self._feed
        api, spi = getattr(trader, "_api", None), getattr(trader, "_spi", None)
        broker, investor = (
            getattr(trader, "_bound_broker_id", None),
            getattr(trader, "_bound_user_id", None),
        )
        fingerprint = getattr(trader, "_account_fingerprint", None)
        if (
            feed.trader_client is not trader
            or (not self._allow_implicit_connect and self._bound_gateway_trader is not trader)
            or api is None
            or spi is None
            or getattr(trader, "_session_native_api", None) is not api
            or getattr(spi, "_native_api", None) is not api
            or type(broker) is not str
            or not broker
            or broker != feed.broker_id
            or type(investor) is not str
            or not investor
            or investor != feed.user_id
            or type(fingerprint) is not str
            or fingerprint
            != hashlib.sha256((broker + ":" + investor).encode("utf-8")).hexdigest()[:16]
            or trader.is_read_only_ready is not True
        ):
            raise DirectWriteError("ctp_prepared_cancel_session_unavailable")
        state = trader.get_session_state()
        if type(state) is not dict:
            raise DirectWriteError("ctp_prepared_cancel_session_unavailable")
        front, session = state.get("front_id"), state.get("session_id")
        generation, day = state.get("connection_generation"), state.get("trading_day")
        if (
            type(front) is not int
            or not 0 < front < 2**31
            or not is_ctp_session_id(session)
            or type(generation) is not int
            or not 0 < generation < 2**63
            or type(day) is not str
            or len(day) != 8
            or not day.isascii()
            or not day.isdigit()
            or state.get("account_fingerprint") != fingerprint
            or getattr(trader, "_session_native_front", None)
            != getattr(trader, "_bound_front", None)
            or type(getattr(trader, "_native_api_generation", None)) is not int
            or type(getattr(trader, "_native_api_source_id", None)) is not str
        ):
            raise DirectWriteError("ctp_prepared_cancel_session_unavailable")
        return (
            broker,
            investor,
            fingerprint,
            day,
            generation,
            front,
            session,
            trader._native_api_generation,
            trader._native_api_source_id,
            trader._session_native_front,
            trader._login_request_id,
        )

    def prepare_cancel_order(
        self, target: NativeOrderIdentity, symbol: str
    ) -> PreparedCancelRequest:
        """Prepare one source-bound action without invoking a native request."""
        self._prepared_cancel_access()
        from bt_api_base.gateway_direct_protocol import validate_gateway_native_identity

        from bt_api_ctp.ctp.client import canonical_ctp_instrument
        from bt_api_ctp.gateway_cancel_source import cancel_attempt_key, capture_cancel_source_locked

        try:
            validate_gateway_native_identity(target)
            if (
                target.exchange_name != CTP_DIRECT_WRITE_EXCHANGE
                or type(symbol) is not str
                or not symbol
                or symbol != symbol.strip()
            ):
                raise ValueError
            fields = target.fields
            original_day = fields.get("trading_day")
            if (
                type(original_day) is not str
                or len(original_day) != 8
                or not original_day.isascii()
                or not original_day.isdigit()
            ):
                raise ValueError
            exchange = fields.get("exchange_id", "")
            if (
                type(exchange) is not str
                or not exchange
                or exchange != exchange.strip()
                or not canonical_ctp_instrument(symbol, exchange)
            ):
                raise ValueError
            ref = fields.get("order_ref") or target.client_order_id
            front, session = fields.get("front_id"), fields.get("session_id")
            native_id, raw_id = fields.get("order_sys_id"), fields.get("order_sys_id_raw")
            if ref is not None and (type(ref) is not str or not ref or ref != ref.strip()):
                raise ValueError
            if (
                fields.get("order_ref")
                and target.client_order_id
                and fields["order_ref"] != target.client_order_id
            ):
                raise ValueError
            ref_target = bool(
                ref
                and type(front) is int
                and 0 < front < 2**31
                and is_ctp_session_id(session)
            )
            venue_target = bool(
                type(native_id) is str and native_id and native_id == native_id.strip()
            )
            if raw_id is not None and (
                type(raw_id) is not str or not venue_target or raw_id.strip() != native_id
            ):
                raise ValueError
            if not ref_target and not venue_target:
                raise ValueError
        except Exception:
            raise DirectWriteError("ctp_prepared_cancel_target_invalid") from None
        with self._lock:
            trader = self._feed.trader_client
            if trader is None:
                raise DirectWriteError("ctp_prepared_cancel_session_unavailable")
            lock = getattr(trader, "_query_state_lock", None)
            if lock is None:
                raise DirectWriteError("ctp_prepared_cancel_session_unavailable")
            with lock:
                scope = self._cancel_session_locked(trader)
                original_api, original_spi = trader._api, trader._spi
                if original_day != scope[3]:
                    raise DirectWriteError("ctp_prepared_cancel_cross_day_unproven")
                if len(self._cancel_reservations) >= 8192:
                    raise DirectWriteError("ctp_prepared_cancel_capacity")
                request_id = trader._next_request_id()
                if type(request_id) is not int or not 0 < request_id < 2**31:
                    raise DirectWriteError("ctp_prepared_cancel_request_id_invalid")
                broker, investor, fingerprint, day, generation, dispatch_front, dispatch_session = (
                    scope[:7]
                )
                try:
                    source_binding = capture_cancel_source_locked(
                        trader, environment=trader._gateway_return_environment
                    )
                    if (self._cancel_session_locked(trader) != scope
                            or trader._api is not original_api or trader._spi is not original_spi):
                        raise ValueError
                except Exception:
                    raise DirectWriteError("ctp_prepared_cancel_source_unavailable") from None
                action = {
                    "BrokerID": broker,
                    "InvestorID": investor,
                    "UserID": investor,
                    "InstrumentID": symbol,
                    "ExchangeID": exchange,
                    "ActionFlag": "0",
                    "RequestID": request_id,
                    "OrderActionRef": request_id,
                    "OrderRef": ref if ref_target else "",
                    "FrontID": front if ref_target else 0,
                    "SessionID": session if ref_target else 0,
                    "OrderSysID": ""
                    if ref_target
                    else (raw_id if raw_id is not None else native_id),
                    "LimitPrice": "0",
                    "VolumeChange": 0,
                    "reserve1": "",
                    "InvestUnitID": "",
                    "reserve2": "",
                    "MacAddress": "",
                    "IPAddress": "",
                    "OrderMemo": "",
                    "SessionReqSeq": 0,
                    "DispatchFrontID": dispatch_front,
                    "DispatchSessionID": dispatch_session,
                }
                request_key = cancel_attempt_key(
                    broker=broker, investor=investor, day=day, front=dispatch_front,
                    session=dispatch_session, request_id=request_id, action_ref=request_id,
                    source_binding=source_binding,
                )
                prepared = PreparedCancelRequest(
                    uuid4().hex, target, symbol, day, generation, action, (request_key,),
                    source_binding=source_binding,
                )
                ticket = _PreparedCancelTicket(
                    prepared,
                    prepared.digest(),
                    trader,
                    trader._api,
                    trader._spi,
                    scope,
                    threading.current_thread(),
                    os.getpid(),
                )
                self._cancel_reservations[prepared.reservation_id] = ticket
                return prepared

    def _cancel_ticket(self, prepared: PreparedCancelRequest) -> _PreparedCancelTicket:
        if type(prepared) is not PreparedCancelRequest:
            raise DirectWriteError("ctp_prepared_cancel_unknown")
        ticket = self._cancel_reservations.get(prepared.reservation_id)
        try:
            valid = (
                ticket is not None
                and ticket.prepared is prepared
                and ticket.pid == os.getpid()
                and ticket.owner_thread is threading.current_thread()
                and prepared.digest() == ticket.digest
            )
        except Exception:
            valid = False
        if not valid:
            raise DirectWriteError("ctp_prepared_cancel_unknown")
        return ticket

    def release_cancel_reservation(self, prepared: PreparedCancelRequest) -> None:
        self._prepared_cancel_access()
        with self._lock:
            self._cancel_ticket(prepared)
            del self._cancel_reservations[prepared.reservation_id]

    def submit_prepared_cancel(
        self, prepared: PreparedCancelRequest, grant: WriteGrant, verifier: WriteGrantVerifier
    ) -> DirectSubmitResult:
        self._prepared_cancel_access()
        from bt_api_ctp.ctp import client as ctp_client
        from bt_api_ctp.ctp.ctp_structs_order import CThostFtdcInputOrderActionField
        from bt_api_ctp.gateway_cancel_source import capture_cancel_source_locked

        with self._lock:
            ticket = self._cancel_ticket(prepared)
            del self._cancel_reservations[prepared.reservation_id]
            trader = ticket.trader
            outcome = ctp_client._CtpNativeRequestDispatchOutcome()
            try:
                with trader._query_state_lock:
                    if (
                        self._cancel_session_locked(trader) != ticket.session
                        or trader._api is not ticket.api
                        or trader._spi is not ticket.spi
                    ):
                        raise DirectWriteError("ctp_prepared_cancel_session_changed")
                self._settlement_readback_for_write(trader)
                capability = self._admitted_capability(trader)
                self._ensure_armed(
                    trader, capability, prepared.symbol, prepared.request_fields["ExchangeID"]
                )
                field = CThostFtdcInputOrderActionField()
                native_fields = {
                    k: v
                    for k, v in prepared.request_fields.items()
                    if k not in {"DispatchFrontID", "DispatchSessionID"}
                }
                for name, value in native_fields.items():
                    setattr(field, name, 0.0 if name == "LimitPrice" else value)

                def check_source_and_fields() -> None:
                    if (
                        os.getpid() != ticket.pid
                        or threading.current_thread() is not ticket.owner_thread
                        or self._cancel_session_locked(trader) != ticket.session
                        or trader._api is not ticket.api
                        or trader._spi is not ticket.spi
                        or trader.is_trading_ready is not True
                        or prepared.digest() != ticket.digest
                        or capture_cancel_source_locked(
                            trader, environment=trader._gateway_return_environment
                        ) != dict(prepared.source_binding or {})
                    ):
                        raise DirectWriteError("ctp_prepared_cancel_session_changed")
                    for name, expected in native_fields.items():
                        actual = getattr(field, name)
                        if name == "LimitPrice":
                            if type(actual) is not float or actual != 0.0:
                                raise DirectWriteError("ctp_prepared_cancel_field_changed")
                        elif type(actual) is not type(expected) or actual != expected:
                            raise DirectWriteError("ctp_prepared_cancel_field_changed")

                def final_check() -> None:
                    check_source_and_fields()
                    verifier.consume(
                        grant,
                        exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                        operation=WriteOperation.CANCEL,
                        session_generation=prepared.session_generation,
                    )
                    check_source_and_fields()

                ret = trader.submit_order_action(
                    field,
                    prepared.request_fields["RequestID"],
                    execution_capability=capability,
                    _prepared_cancel_pre_dispatch=final_check,
                    _prepared_cancel_dispatch_outcome=outcome,
                )
            except Exception:
                return DirectSubmitResult(
                    None if outcome.native_call_started else False,
                    prepared.target_identity,
                    "ctp_prepared_cancel_outcome_unknown"
                    if outcome.native_call_started
                    else "ctp_prepared_cancel_not_sent",
                )
            if type(ret) is not int:
                return DirectSubmitResult(
                    None, prepared.target_identity, "ctp_prepared_cancel_outcome_unknown"
                )
            return DirectSubmitResult(
                ret == 0,
                prepared.target_identity,
                None if ret == 0 else "ctp_native_action_returned_nonzero",
            )

    def cancel_order(
        self,
        target: NativeOrderIdentity,
        symbol: str,
        grant: WriteGrant,
        verifier: WriteGrantVerifier,
    ) -> DirectSubmitResult:
        self._check_access()
        from bt_api_ctp.ctp import client as ctp_client

        with self._lock:
            trader = self._trader()
            session = self._session_fields(trader)
            fields = target.fields
            exchange_id = _text(fields.get("exchange_id"))
            order_sys_id = _text(fields.get("order_sys_id"))
            order_sys_id_raw = fields.get("order_sys_id_raw")
            order_ref = _text(fields.get("order_ref") or target.client_order_id)
            front_id = _int_or_none(fields.get("front_id"))
            session_id = _int_or_none(fields.get("session_id"))
            kwargs: dict[str, Any] = {"exchange_id": exchange_id}
            order_id: Optional[str] = None
            if order_ref and front_id is not None and session_id is not None:
                # Original session identity (FrontID + SessionID + OrderRef):
                # valid from any session of this investor for the trading day.
                kwargs.update(order_ref=order_ref, front_id=front_id, session_id=session_id)
            elif order_sys_id and exchange_id:
                # Venue identity: CTP matches the exact (space-padded) string.
                order_id = str(order_sys_id_raw) if order_sys_id_raw else order_sys_id
            else:
                raise DirectWriteError("ctp_direct_cancel_identity_incomplete")
            if self._settlement_readback_for_write(trader):
                # Consume against the session after the readback: a reconnect
                # meanwhile fails the grant's session-generation check.
                session = self._session_fields(trader)
            verifier.consume(
                grant,
                exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                operation=WriteOperation.CANCEL,
                session_generation=session["connection_generation"],
            )
            try:
                capability = self._admitted_capability(trader)
                self._ensure_armed(trader, capability, symbol, exchange_id)
                result = self._feed._cancel_order_native(
                    symbol, order_id, None, _execution_capability=capability, **kwargs
                )
            except (ctp_client.CtpExecutionGateError, ValueError) as exc:
                return DirectSubmitResult(
                    sent=False,
                    identity=target,
                    error_code=getattr(exc, "code", None) or "ctp_direct_cancel_not_sent",
                    detail=str(exc)[:200],
                )
            status = result.get_status() if hasattr(result, "get_status") else None
            return DirectSubmitResult(
                sent=status is True,
                identity=target,
                error_code=None if status is True else "ctp_native_action_returned_nonzero",
            )

    # ------------------------------------------------------------------
    # settlement (D62-20/29)
    # ------------------------------------------------------------------
    def settlement_status(self) -> SettlementStatus:
        if os.getpid() != self._settlement_owner_pid:
            return SettlementStatus.UNKNOWN
        self._check_settlement_thread()
        observation = self.observe_settlement()
        return SettlementStatus.UNKNOWN if observation is None else observation.status

    def _observe_gateway_settlement(self) -> SettlementObservation | None:
        """Read a gateway observation without holding the provider over I/O."""
        from bt_api_ctp.ctp.client import TraderClient

        with self._lock:
            if self._gateway_settlement_inflight is not None:
                return None
            self._settlement_ticket = None
            try:
                trader = self._trader()
                if type(trader) is not TraderClient or self._feed.trader_client is not trader:
                    return None
                before = trader.get_session_state()
                if not isinstance(before, dict) or before.get("read_only_ready") is not True:
                    return None
                token = object()
                self._gateway_settlement_inflight = token
            except Exception:
                return None

        try:
            status, evidence = TraderClient._read_direct_settlement_status(
                trader, timeout=_GATEWAY_SETTLEMENT_READBACK_TIMEOUT, _include_evidence=True
            )
            after = trader.get_session_state()
            with self._lock:
                identity_fields = (
                    "connection_generation", "account_fingerprint", "trading_day",
                    "front_id", "session_id",
                )
                if (
                    self._gateway_settlement_inflight is not token
                    or self._owner_closing or self._owner_closed
                    or self._feed.trader_client is not trader
                    or not isinstance(after, dict)
                    or after.get("read_only_ready") is not True
                    or any(before.get(name) != after.get(name) for name in identity_fields)
                    or status not in ("confirmed", "unconfirmed")
                    or evidence is None
                ):
                    return None
                if (
                    status == "confirmed"
                    and after.get("trading_ready") is True
                    and after.get("settlement_readback_verified") is True
                    and after.get("settlement_proof_source") == "confirmation_query"
                    and after.get("settlement_proof_query_request_id") is not None
                    and after.get("settlement_connection_generation") == after.get("connection_generation")
                    and after.get("settlement_account_fingerprint") == after.get("account_fingerprint")
                    and after.get("settlement_trading_day") == after.get("trading_day")
                ):
                    self._note_trading_ready(trader)
                elif status != "unconfirmed":
                    return None
                binding, result, api, spi, epoch = evidence
                source = result.query_source
                if source is None or source.trusted_expires_monotonic is None:
                    return None
                observation = SettlementObservation(
                    exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                    account_binding=binding.account_fingerprint,
                    trading_day=binding.trading_day,
                    session_generation=binding.connection_generation,
                    status=SettlementStatus.CONFIRMED if status == "confirmed" else SettlementStatus.UNCONFIRMED,
                    observation_id=uuid4().hex,
                )
                self._settlement_ticket = _SettlementObservationTicket(
                    observation, observation.status, observation.observation_id,
                    trader, binding, result, api, spi, epoch, os.getpid(),
                    min(source.trusted_expires_monotonic, time.monotonic() + 30.0),
                )
                if not self.validate_settlement_observation(observation):
                    self._settlement_ticket = None
                    return None
                return observation
        except Exception:
            return None
        finally:
            with self._lock:
                if self._gateway_settlement_inflight is token:
                    self._gateway_settlement_inflight = None

    def observe_settlement(self) -> SettlementObservation | None:
        """Return only this provider's latest source-bound settlement observation."""
        if os.getpid() != self._settlement_owner_pid:
            return None
        self._check_settlement_thread()
        if self._owner_thread_only and not self._allow_implicit_connect:
            return self._observe_gateway_settlement()
        from bt_api_ctp.ctp.client import TraderClient

        with self._lock:
            self._settlement_ticket = None
            try:
                trader = self._trader()
                if type(trader) is not TraderClient or self._feed.trader_client is not trader:
                    return None
                before = trader.get_session_state()
                if not isinstance(before, dict) or before.get("read_only_ready") is not True:
                    return None
                identity_fields = (
                    "connection_generation", "account_fingerprint", "trading_day",
                    "front_id", "session_id",
                )
                status, evidence = TraderClient._read_direct_settlement_status(
                    trader, timeout=_SETTLEMENT_READBACK_TIMEOUT, _include_evidence=True
                )
                after = trader.get_session_state()
                if (
                    self._feed.trader_client is not trader
                    or not isinstance(after, dict)
                    or after.get("read_only_ready") is not True
                    or any(before.get(name) != after.get(name) for name in identity_fields)
                    or status not in ("confirmed", "unconfirmed")
                    or evidence is None
                ):
                    return None
                if (
                    status == "confirmed"
                    and after.get("trading_ready") is True
                    and after.get("settlement_readback_verified") is True
                    and after.get("settlement_proof_source") == "confirmation_query"
                    and after.get("settlement_proof_query_request_id") is not None
                    and after.get("settlement_connection_generation") == after.get("connection_generation")
                    and after.get("settlement_account_fingerprint") == after.get("account_fingerprint")
                    and after.get("settlement_trading_day") == after.get("trading_day")
                ):
                    self._note_trading_ready(trader)
                elif status != "unconfirmed":
                    return None
                binding, result, api, spi, epoch = evidence
                source = result.query_source
                if source is None or source.trusted_expires_monotonic is None:
                    return None
                observation = SettlementObservation(
                    exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                    account_binding=binding.account_fingerprint,
                    trading_day=binding.trading_day,
                    session_generation=binding.connection_generation,
                    status=SettlementStatus.CONFIRMED if status == "confirmed" else SettlementStatus.UNCONFIRMED,
                    observation_id=uuid4().hex,
                )
                self._settlement_ticket = _SettlementObservationTicket(
                    observation, observation.status, observation.observation_id,
                    trader, binding, result, api, spi, epoch, os.getpid(),
                    min(source.trusted_expires_monotonic, time.monotonic() + 30.0),
                )
                if not self.validate_settlement_observation(observation):
                    self._settlement_ticket = None
                    return None
                return observation
            except Exception:
                self._settlement_ticket = None
                return None

    def validate_settlement_observation(self, observation: SettlementObservation) -> bool:
        """Recheck the exact issued object and source without provider I/O."""
        if os.getpid() != self._settlement_owner_pid:
            return False
        self._check_settlement_thread()
        from bt_api_ctp.registered_sim_session import validate_query_session
        from bt_api_ctp.store_account_session import _valid_settlement_result

        with self._lock:
            ticket = self._settlement_ticket
            if (
                type(observation) is not SettlementObservation
                or ticket is None
                or ticket.observation is not observation
                or ticket.consumed
                or observation.exchange_name != CTP_DIRECT_WRITE_EXCHANGE
                or observation.account_binding != ticket.binding.account_fingerprint
                or observation.trading_day != ticket.binding.trading_day
                or observation.session_generation != ticket.binding.connection_generation
                or observation.status is not ticket.status
                or observation.observation_id != ticket.observation_id
                or ticket.pid != os.getpid()
                or time.monotonic() >= ticket.expires_at
                or self._feed.trader_client is not ticket.trader
            ):
                return False
            try:
                trader = ticket.trader
                with trader._query_state_lock:
                    return bool(
                        trader._api is ticket.api
                        and trader._spi is ticket.spi
                        and trader._session_native_api is ticket.api
                        and trader._settlement_observation_epoch == ticket.epoch
                        and trader.auto_settlement_confirm is False
                        and validate_query_session(trader, ticket.binding, now_monotonic=time.monotonic())
                        and _valid_settlement_result(ticket.result, ticket.binding)
                    )
            except Exception:
                return False

    def confirm_settlement(
        self, grant: WriteGrant, verifier: WriteGrantVerifier, *, observation: SettlementObservation
    ) -> SettlementStatus:
        if os.getpid() != self._settlement_owner_pid:
            raise DirectWriteError("ctp_settlement_observation_invalid")
        self._check_settlement_thread()
        if self._owner_thread_only and not self._allow_implicit_connect:
            return self._confirm_gateway_settlement(grant, verifier, observation)
        from bt_api_ctp.ctp import client as ctp_client

        with self._lock:
            binding = getattr(grant, "settlement_binding", None)
            if (
                type(observation) is not SettlementObservation
                or
                type(binding) is not SettlementWriteBinding
                or binding.observation is not observation
                or observation.status is not SettlementStatus.UNCONFIRMED
                or type(binding.marker_id) is not str
                or not binding.marker_id
                or binding.marker_id != binding.marker_id.strip()
                or len(binding.marker_id) > 256
                or getattr(grant, "exchange_name", None) != CTP_DIRECT_WRITE_EXCHANGE
                or getattr(grant, "operation", None) is not WriteOperation.SETTLEMENT_CONFIRM
                or getattr(grant, "session_generation", None) != observation.session_generation
                or not self.validate_settlement_observation(observation)
            ):
                raise DirectWriteError("ctp_settlement_observation_invalid")
            trader = self._trader()
            if self._settlement_ticket is None or trader is not self._settlement_ticket.trader:
                raise DirectWriteError("ctp_settlement_observation_invalid")
            session = self._session_fields(trader)
            verifier.consume(
                grant,
                exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                operation=WriteOperation.SETTLEMENT_CONFIRM,
                session_generation=session["connection_generation"],
                settlement_binding=binding,
            )

            owner_thread = threading.get_ident()
            owner_pid = os.getpid()
            marker_id = binding.marker_id

            def check() -> None:
                # confirm_settlement holds provider._lock in this owner thread.
                # Reject other threads before any provider lock acquisition.
                if os.getpid() != owner_pid or threading.get_ident() != owner_thread:
                    raise ctp_client.CtpExecutionGateError("ctp_settlement_observation_invalid")
                if (
                    getattr(grant, "settlement_binding", None) is not binding
                    or binding.observation is not observation
                    or binding.marker_id != marker_id
                    or getattr(grant, "exchange_name", None) != CTP_DIRECT_WRITE_EXCHANGE
                    or getattr(grant, "operation", None) is not WriteOperation.SETTLEMENT_CONFIRM
                    or getattr(grant, "session_generation", None) != observation.session_generation
                ):
                    raise ctp_client.CtpExecutionGateError("ctp_settlement_observation_invalid")
                if not self.validate_settlement_observation(observation):
                    raise ctp_client.CtpExecutionGateError("ctp_settlement_observation_invalid")

            def consume() -> None:
                check()
                assert self._settlement_ticket is not None
                self._settlement_ticket.consumed = True

            try:
                check()
                capability = self._admitted_capability(trader)
                state = trader.get_execution_gate_state()
                if isinstance(state, dict) and state.get("armed") is True:
                    trader.disarm_execution_gate(capability, "ctp_direct_settlement")
                ready = trader.confirm_settlement_for_sdk_admission(
                    capability, observation_check=check, observation_consume=consume, timeout=15.0
                )
            except ctp_client.CtpExecutionGateError as exc:
                raise DirectWriteError(
                    getattr(exc, "code", None) or "ctp_direct_settlement_failed", str(exc)
                ) from None
            if ready and self._feed.trader_client is trader:
                self._note_trading_ready(trader)
                return SettlementStatus.CONFIRMED
            return SettlementStatus.UNKNOWN

    def _confirm_gateway_settlement(
        self, grant: WriteGrant, verifier: WriteGrantVerifier, observation: SettlementObservation
    ) -> SettlementStatus:
        """Gateway decision A: confirm without holding the provider over native I/O.

        The Gateway owner has already persisted the daily attempt marker and
        made its final owner-thread claim (the attempt is in flight). Here the
        exact observation ticket and grant are validated and consumed under the
        provider lock; the lock is released before the native confirmation and
        its server readback, which may block. An in-flight token excludes
        concurrent settlement observation or confirmation meanwhile. The
        trader still rechecks the exact observation under its own state lock
        immediately before the native request. Only a readback proving this
        account and trading day confirmed returns CONFIRMED.
        """
        from bt_api_ctp.ctp import client as ctp_client

        with self._lock:
            binding = getattr(grant, "settlement_binding", None)
            if (
                self._gateway_settlement_inflight is not None
                or self._owner_closing or self._owner_closed
                or type(observation) is not SettlementObservation
                or type(binding) is not SettlementWriteBinding
                or binding.observation is not observation
                or observation.status is not SettlementStatus.UNCONFIRMED
                or type(binding.marker_id) is not str
                or not binding.marker_id
                or binding.marker_id != binding.marker_id.strip()
                or len(binding.marker_id) > 256
                or getattr(grant, "exchange_name", None) != CTP_DIRECT_WRITE_EXCHANGE
                or getattr(grant, "operation", None) is not WriteOperation.SETTLEMENT_CONFIRM
                or getattr(grant, "session_generation", None) != observation.session_generation
                or not self.validate_settlement_observation(observation)
            ):
                raise DirectWriteError("ctp_settlement_observation_invalid")
            trader = self._trader()
            if self._settlement_ticket is None or trader is not self._settlement_ticket.trader:
                raise DirectWriteError("ctp_settlement_observation_invalid")
            session = self._session_fields(trader)
            verifier.consume(
                grant,
                exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                operation=WriteOperation.SETTLEMENT_CONFIRM,
                session_generation=session["connection_generation"],
                settlement_binding=binding,
            )
            capability = self._admitted_capability(trader)
            state = trader.get_execution_gate_state()
            if isinstance(state, dict) and state.get("armed") is True:
                trader.disarm_execution_gate(capability, "ctp_direct_settlement")
            token = object()
            self._gateway_settlement_inflight = token

        owner_thread = threading.get_ident()
        owner_pid = os.getpid()
        marker_id = binding.marker_id
        ticket = self._settlement_ticket
        from bt_api_ctp.registered_sim_session import validate_query_session
        from bt_api_ctp.store_account_session import _valid_settlement_result

        def check() -> None:
            # The trader calls this under its state lock. Never take the
            # provider lock here: the readback monitor orders provider ->
            # trader state, so this recheck reads the pinned ticket instead.
            if os.getpid() != owner_pid or threading.get_ident() != owner_thread:
                raise ctp_client.CtpExecutionGateError("ctp_settlement_observation_invalid")
            if (
                getattr(grant, "settlement_binding", None) is not binding
                or binding.observation is not observation
                or binding.marker_id != marker_id
                or self._gateway_settlement_inflight is not token
                or self._owner_closing or self._owner_closed
                or self._settlement_ticket is not ticket
                or ticket.consumed
                or ticket.observation is not observation
                or ticket.pid != os.getpid()
                or time.monotonic() >= ticket.expires_at
                or self._feed.trader_client is not ticket.trader
            ):
                raise ctp_client.CtpExecutionGateError("ctp_settlement_observation_invalid")
            native = ticket.trader
            with native._query_state_lock:
                if not (
                    native._api is ticket.api
                    and native._spi is ticket.spi
                    and native._session_native_api is ticket.api
                    and native._settlement_observation_epoch == ticket.epoch
                    and native.auto_settlement_confirm is False
                    and validate_query_session(native, ticket.binding,
                                               now_monotonic=time.monotonic())
                    and _valid_settlement_result(ticket.result, ticket.binding)
                ):
                    raise ctp_client.CtpExecutionGateError("ctp_settlement_observation_invalid")

        def consume() -> None:
            check()
            ticket.consumed = True

        try:
            check()
            ready = trader.confirm_settlement_for_sdk_admission(
                capability, observation_check=check, observation_consume=consume, timeout=15.0
            )
        except ctp_client.CtpExecutionGateError as exc:
            raise DirectWriteError(
                getattr(exc, "code", None) or "ctp_direct_settlement_failed", str(exc)
            ) from None
        finally:
            with self._lock:
                if self._gateway_settlement_inflight is token:
                    self._gateway_settlement_inflight = None
        with self._lock:
            if ready and self._feed.trader_client is trader and not self._owner_closing:
                self._note_trading_ready(trader)
                return SettlementStatus.CONFIRMED
        return SettlementStatus.UNKNOWN

    # ------------------------------------------------------------------
    # returns
    # ------------------------------------------------------------------
    def _on_native_return(self, kind: str, payload: dict[str, Any]) -> None:
        with self._events_lock:
            self._sequence += 1
            sequence = self._sequence
        try:
            event = project_ctp_execution_event(kind, payload, sequence=sequence)
        except Exception as exc:
            # A return that cannot be projected is lost like an overflowed one.
            self._note_dropped("projection_failed", kind, type(exc).__name__)
            return
        if event is None:
            return
        overflow = False
        with self._events_lock:
            if len(self._events) >= _MAX_EVENTS:
                self._events.popleft()
                self._dropped_events += 1
                overflow = True
            self._events.append(event)
        if overflow:
            self._note_dropped("queue_overflow", kind, None, counted=True)

    def _note_dropped(
        self, cause: str, kind: str, error: Optional[str], *, counted: bool = False
    ) -> None:
        """Count one lost return (unless ``counted``) and log at most once per interval.

        The line names only the cause, the return kind and the exception type:
        never the native payload or its message (account fields).
        """
        now = _monotonic()
        with self._events_lock:
            if not counted:
                self._dropped_events += 1
            total = self._dropped_events
            last = self._dropped_logged_at
            if last is not None and now - last < _DROPPED_LOG_INTERVAL:
                return
            self._dropped_logged_at = now
        _logger.warning(
            "ctp direct returns dropped: cause=%s kind=%s error=%s total=%d",
            cause,
            kind if type(kind) is str and kind in _RETURN_KINDS else "other",
            error or "-",
            total,
        )

    def drain_execution_events(self, max_events: int) -> tuple[DirectExecutionEvent, ...]:
        self._check_access()
        limit = max(1, int(max_events))
        out: list[DirectExecutionEvent] = []
        with self._events_lock:
            while self._events and len(out) < limit:
                out.append(self._events.popleft())
        return tuple(out)

    def _instrument_catalog(self) -> Any:
        cache = self._catalog_cache
        if cache is None:
            from bt_api_ctp.instrument_catalog_cache import default_instrument_catalog_cache

            cache = self._catalog_cache = default_instrument_catalog_cache()
        return cache

    def instrument_catalog_counters(self) -> dict[str, int]:
        """Catalog cache counters (process-wide cache) plus this provider's queries."""
        self._check_access()

        counters = dict(self._instrument_catalog().counters())
        with self._catalog_lock:
            counters["provider_native_queries"] = self.catalog_queries
        return counters

    def warm_instrument_catalog(self) -> Any:
        """Build (or load) the current trading day's instrument catalog.

        The one full native query per scope and CTP trading day (D62-11b/11c)
        runs only here, never inside an order: it can take minutes on a slow
        front and holds the session's only query slot meanwhile (I62-12a).
        Call it when the session has no other queries to make, e.g. before
        trading starts; other processes then reuse the published file.
        Returns the cache's ``CatalogReady`` or raises
        ``InstrumentCatalogNotReady``.
        """
        self._check_access()
        trader = self._trader()
        self._session_fields(trader)
        from bt_api_ctp.instrument_catalog_cache import CtpFeedCatalogSource

        ready = self._instrument_catalog().ensure_ready(CtpFeedCatalogSource(self._feed))
        with self._catalog_lock:
            self.catalog_queries += ready.native_queries
            self.last_catalog_error = None
        return ready

    def resolve_exchange_id(self, symbol: str) -> Optional[str]:
        """Venue (``ExchangeID``) of ``symbol`` for an order (latency-bound).

        Uses the current trading day's catalog when it is already in memory
        or published by any process (missing instruments are then queried on
        demand and added, D62-11b/11c), otherwise the single-instrument query.
        It never starts the full catalog query (see
        :meth:`warm_instrument_catalog`).  Returns ``None`` when no unique
        venue is proven.  Catalog entries are reference data only, not quote,
        position or permission evidence.
        """
        self._check_access()
        if not symbol:
            return None
        trader = self._trader()
        self._session_fields(trader)  # a complete session is still required
        from bt_api_ctp.instrument_catalog_cache import (
            CtpFeedCatalogSource,
            InstrumentCatalogNotReady,
        )

        try:
            lookup = self._instrument_catalog().lookup(
                CtpFeedCatalogSource(self._feed), symbol, build=False
            )
        except InstrumentCatalogNotReady as exc:
            code = exc.code
        except Exception:
            code = "ctp_catalog_unavailable"
        else:
            with self._catalog_lock:
                self.catalog_queries += lookup.native_queries
                self.last_catalog_error = None
            return lookup.venue
        with self._catalog_lock:
            self.last_catalog_error = code
        return self._resolve_exchange_id_uncached(symbol)

    def _resolve_exchange_id_uncached(self, symbol: str) -> Optional[str]:
        """Single-instrument native query used when the catalog is not ready."""
        try:
            result = self._feed.query_instruments_result(instrument_id=symbol, timeout=10)
        except Exception:
            return None
        with self._catalog_lock:
            self.catalog_queries += 1
        if getattr(result, "complete", False) is not True:
            return None
        venues = {
            _text(_row_value(row, "ExchangeID"))
            for row in result.records
            if _text(_row_value(row, "InstrumentID")) == symbol
        }
        venues.discard("")
        return next(iter(venues)) if len(venues) == 1 else None

    # ------------------------------------------------------------------
    # read-only account facts for host reconciliation (D62-05/13/16/22)
    # ------------------------------------------------------------------
    def account_facts(
        self,
        *,
        positions: bool = True,
        active_orders: bool = True,
        trades: bool = False,
        balance: bool = True,
        all_orders: bool = False,
        timeout: float = 5.0,
        symbols: Any = None,
    ) -> DirectAccountFacts:
        """Query positions, working orders, today's trades and funds once.

        Each section is complete only when the native query completed without
        error for the current session.  The session (connection generation,
        trading day, native client) is read again after the last query; if it
        changed, every section is ``None`` with the blocker
        ``session_changed_during_query``.  Rows are projected verbatim;
        nothing is attributed to a strategy here.
        """
        self._check_access()
        blockers: list[str] = []
        trader = self._trader()
        session = self._session_fields(trader)
        observed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        def _complete(result: Any, name: str) -> bool:
            blocker = _query_blocker(result, name, session["connection_generation"])
            if blocker is not None:
                blockers.append(blocker)
            return blocker is None

        position_facts: Optional[tuple[DirectPositionFact, ...]] = None
        if positions:
            # Unfiltered whole-account read: the only position result that may
            # prove the whole account flat (D62-11a).
            result = self._feed.query_positions_result(timeout=timeout)
            if _complete(result, "positions"):
                position_facts, anomalies = _project_position_rows(result.records)
                if anomalies:
                    # A row that may hold a position but cannot be projected
                    # leaves the whole-account read unable to prove anything
                    # (in particular not a flat account for a first init).
                    blockers.extend(f"positions_{name}" for name in anomalies)
                    position_facts = None
        order_facts: Optional[tuple[DirectExecutionEvent, ...]] = None
        every_order: Optional[tuple[DirectExecutionEvent, ...]] = None
        if active_orders or all_orders:
            result = self._feed.query_orders_result(timeout=timeout)
            if _complete(result, "orders"):
                working, everything = [], []
                for index, row in enumerate(result.records):
                    event = project_ctp_execution_event(
                        "order", _row_mapping(row), sequence=index + 1
                    )
                    if event is None:
                        continue
                    everything.append(event)
                    if _text(_row_value(row, "OrderStatus")) in _ACTIVE_ORDER_STATUS:
                        working.append(event)
                if active_orders:
                    order_facts = tuple(working)
                if all_orders:
                    every_order = tuple(everything)
        trade_facts: Optional[tuple[DirectExecutionEvent, ...]] = None
        if trades:
            result = self._feed.query_trades_result(timeout=timeout)
            if _complete(result, "trades"):
                projected = []
                for index, row in enumerate(result.records):
                    event = project_ctp_execution_event(
                        "trade", _row_mapping(row), sequence=index + 1
                    )
                    if event is not None:
                        projected.append(event)
                trade_facts = tuple(projected)
        balance_fact: Optional[DirectBalanceFact] = None
        if balance:
            result = self._feed.query_account_result(timeout=timeout)
            if _complete(result, "account") and result.records:
                row = result.records[0]
                frozen = sum(
                    _float_or_none(_row_value(row, name)) or 0.0
                    for name in ("FrozenMargin", "FrozenCash", "FrozenCommission")
                )
                balance_fact = DirectBalanceFact(
                    currency=_text(_row_value(row, "CurrencyID")) or "CNY",
                    balance=_float_or_none(_row_value(row, "Balance")),
                    available=_float_or_none(_row_value(row, "Available")),
                    margin=_float_or_none(_row_value(row, "CurrMargin")),
                    frozen=frozen,
                    close_profit=_float_or_none(_row_value(row, "CloseProfit")),
                    position_profit=_float_or_none(_row_value(row, "PositionProfit")),
                    commission=_float_or_none(_row_value(row, "Commission")),
                )
            elif "account_query_incomplete" not in blockers:
                blockers.append("account_query_empty")
        try:
            after = self._session_fields(trader)
        except DirectWriteError:
            after = None
        if after != session or self._feed.trader_client is not trader:
            # A reconnect, trading-day change or new native client while the
            # queries ran: the sections may come from two sessions, so none of
            # them is a fact of one session.
            blockers.append("session_changed_during_query")
            position_facts = order_facts = every_order = trade_facts = None
            balance_fact = None
        return DirectAccountFacts(
            exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
            trading_day=session.get("trading_day"),
            session_generation=session.get("connection_generation"),
            observed_at_utc=observed,
            positions=position_facts,
            active_orders=order_facts,
            trades=trade_facts,
            balance=balance_fact,
            complete=not blockers,
            blockers=tuple(blockers),
            orders=every_order,
        )

    def instrument_positions(
        self,
        symbol: str,
        exchange_id: Optional[str] = None,
        *,
        timeout: float = 5.0,
    ) -> InstrumentPositionFacts:
        """Positions of one instrument through the natively filtered query.

        The ``InstrumentID`` (and ``ExchangeID`` when given) filter is passed to
        ``ReqQryInvestorPosition``.  Rows of other instruments or venues that a
        venue returns anyway are dropped and reported, never used.  The result
        has instrument scope only: an empty complete result does not prove the
        account (or any other instrument) flat; use :meth:`account_facts` for
        whole-account evidence (D62-11a).
        """
        self._check_access()
        if not _valid_instrument_filter(symbol):
            raise DirectWriteError("ctp_instrument_positions_symbol_invalid")
        venue: Optional[str] = None
        if exchange_id not in (None, ""):
            if not _valid_exchange_filter(exchange_id):
                raise DirectWriteError("ctp_instrument_positions_exchange_invalid")
            venue = exchange_id
        trader = self._trader()
        session = self._session_fields(trader)
        observed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        blockers: list[str] = []
        verified: Optional[bool] = None
        dropped = 0
        dropped_names: set[str] = set()
        facts: Optional[tuple[DirectPositionFact, ...]] = None
        try:
            result = self._feed.query_positions_result(
                timeout=timeout, instrument_id=symbol, exchange_id=venue or ""
            )
        except Exception as exc:
            result = None
            blockers.append(f"positions_query_failed[{type(exc).__name__}]")
        if result is not None:
            blocker = _query_blocker(result, "positions", session["connection_generation"])
            if blocker is not None:
                blockers.append(blocker)
            else:
                verified, evidence_blocker = _native_filter_evidence(result, symbol, venue)
                if evidence_blocker is not None:
                    blockers.append(evidence_blocker)
                kept = []
                for row in result.records:
                    row_symbol = _text(_row_value(row, "InstrumentID"))
                    row_venue = _text(_row_value(row, "ExchangeID"))
                    if row_symbol != symbol:
                        dropped += 1
                        dropped_names.add(row_symbol or "<missing>")
                        continue
                    if venue is not None and row_venue != venue:
                        if not row_venue:
                            # Cannot tell whether this row is the requested
                            # instrument: never drop (or count) it silently.
                            blockers.append("positions_row_venue_missing")
                            continue
                        dropped += 1
                        dropped_names.add(f"{row_venue}.{row_symbol}")
                        continue
                    kept.append(row)
                projected, anomalies = _project_position_rows(kept)
                blockers.extend(anomalies)
                if not blockers:
                    facts = projected
        try:
            after = self._session_fields(trader)
        except DirectWriteError:
            after = None
        if after is None or (
            after["connection_generation"] != session["connection_generation"]
            or after["trading_day"] != session["trading_day"]
        ):
            blockers.append("positions_session_changed")
            facts = None
        unique_blockers = tuple(dict.fromkeys(blockers))
        return InstrumentPositionFacts(
            exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
            symbol=symbol,
            venue=venue,
            trading_day=session.get("trading_day"),
            session_generation=session.get("connection_generation"),
            observed_at_utc=observed,
            positions=facts if not unique_blockers else None,
            complete=not unique_blockers,
            blockers=unique_blockers,
            filter_ignored=dropped > 0,
            dropped_rows=dropped,
            dropped_instruments=tuple(sorted(dropped_names))[:_MAX_DROPPED_INSTRUMENTS_REPORTED],
            native_filter_verified=verified,
        )

    def trading_permission_facts(
        self,
        symbols: Any,
        *,
        exchange_ids: Optional[Mapping[str, Any]] = None,
        include_rates: bool = True,
        timeout: float = 5.0,
        max_duration: Optional[float] = None,
    ) -> TradingPermissionFacts:
        """Trading-permission inputs of ``symbols`` in the current session (D62-25).

        Native queries, all read-only and filtered (never the full catalog):
        ``ReqQryInvestor`` once; ``ReqQryInstrument`` per instrument
        (``InstrumentID``); ``ReqQryTradingCode`` per distinct exchange
        (``ExchangeID``); with ``include_rates`` also
        ``ReqQryInstrumentCommissionRate`` and ``ReqQryInstrumentMarginRate``
        (speculation) per instrument.  ``exchange_ids`` maps a symbol to the
        exchange its orders are sent to; otherwise the instrument row's
        exchange is used.  ``max_duration`` bounds the whole read: queries not
        started before it are skipped and their facts stay unknown.

        The result is never a permission proof (``proves_permission`` is
        False).  A missing, failed or stale query leaves its facts unknown; a
        session change while reading makes every fact unknown.
        """
        self._check_access()
        names = _permission_symbols(symbols)
        venues_given = _permission_venues(exchange_ids, names)
        if type(include_rates) is not bool:
            raise DirectWriteError("ctp_trading_permissions_option_invalid")
        deadline: Optional[float] = None
        if max_duration is not None:
            limit = _float_or_none(max_duration)
            if type(max_duration) is bool or limit is None or limit <= 0:
                raise DirectWriteError("ctp_trading_permissions_option_invalid")
            deadline = time.monotonic() + limit
        trader = self._trader()
        session = self._session_fields(trader)
        generation = session["connection_generation"]
        observed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        blockers: list[str] = []
        queries = 0
        feed = self._feed

        def run(name: str, call: Any) -> Any:
            nonlocal queries
            if deadline is not None and time.monotonic() >= deadline:
                blockers.append("permissions_deadline_exceeded")
                return None
            queries += 1
            try:
                result = call()
            except Exception as exc:
                blockers.append(f"{name}_query_failed[{type(exc).__name__}]")
                return None
            blocker = _query_blocker(result, name, generation)
            if blocker is not None:
                blockers.append(blocker)
                return None
            return result

        investor = permission_projection.investor_fact(
            run("investor", lambda: feed.query_investor_result(timeout=timeout))
        )
        instruments: dict[str, tuple[bool, Any]] = {}
        for symbol in names:
            result = run(
                "instrument",
                lambda s=symbol: feed.query_instruments_result(instrument_id=s, timeout=timeout),
            )
            instruments[symbol] = (
                result is not None,
                permission_projection.instrument_fact(result, symbol, venues_given.get(symbol)),
            )
        order_venue = {
            symbol: venues_given.get(symbol) or instruments[symbol][1][1] for symbol in names
        }
        codes = {
            venue: run(
                "trading_code",
                lambda v=venue: feed.query_trading_codes_result(exchange_id=v, timeout=timeout),
            )
            for venue in sorted({venue for venue in order_venue.values() if venue})
        }
        rates: dict[str, tuple[Any, Any]] = {}
        if include_rates:
            for symbol in names:
                venue = order_venue[symbol] or ""
                commission = run(
                    "commission_rate",
                    lambda s=symbol, v=venue: feed.query_instrument_commission_rate_result(
                        s, exchange_id=v, timeout=timeout
                    ),
                )
                margin = run(
                    "margin_rate",
                    lambda s=symbol, v=venue: feed.query_instrument_margin_rate_result(
                        s, exchange_id=v, timeout=timeout
                    ),
                )
                rates[symbol] = (commission, margin)
        entries = []
        for symbol in names:
            completed, (instrument, _row_venue, product) = instruments[symbol]
            venue = order_venue[symbol]
            code, kinds, code_blocker = permission_projection.trading_code_fact(
                codes.get(venue) if venue else None, venue, venue_query_complete=completed
            )
            if code_blocker is not None:
                blockers.append(code_blocker)
            facts = [investor, instrument, code]
            if include_rates:
                commission, margin = rates[symbol]
                facts.append(
                    permission_projection.rate_fact(
                        "commission_rate", commission, symbol, venue, product
                    )
                )
                facts.append(
                    permission_projection.rate_fact("margin_rate", margin, symbol, venue, product)
                )
            entries.append(
                InstrumentPermissionFacts(
                    symbol=symbol, venue=venue, facts=tuple(facts), trading_code_kinds=kinds
                )
            )
        try:
            after = self._session_fields(trader)
        except DirectWriteError:
            after = None
        consistent = after == session and self._feed.trader_client is trader
        if not consistent:
            # Facts read across a reconnect or trading-day change prove
            # nothing about the current session: every fact becomes unknown.
            blockers.append("permissions_session_changed")
            entries = [
                InstrumentPermissionFacts(
                    symbol=entry.symbol,
                    venue=entry.venue,
                    facts=tuple(
                        PermissionFact(fact.name, PERMISSION_UNKNOWN, "session_changed", False)
                        for fact in entry.facts
                    ),
                )
                for entry in entries
            ]
        return TradingPermissionFacts(
            exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
            trading_day=session.get("trading_day"),
            session_generation=generation,
            observed_at_utc=observed,
            instruments=tuple(entries),
            session_consistent=consistent,
            blockers=tuple(dict.fromkeys(blockers)),
            native_queries=queries,
        )

    def account_reference(self) -> Optional[str]:
        """Hash-only fingerprint of the bound broker/investor (never the raw id)."""
        self._check_access()
        trader = self._trader()
        fingerprint = getattr(trader, "_account_fingerprint", None)
        return str(fingerprint) if fingerprint else None

    @property
    def dropped_events(self) -> int:
        """Cumulative returns lost before any poll (queue overflow, projection failure)."""
        self._check_access()
        with self._events_lock:
            return self._dropped_events

    def close(self) -> Any:
        # Stop the background readback for good (a reused provider falls back
        # to the write-path readback).  Joined without ``_lock``: the monitor
        # only tries that lock for one interval and its readback polls stop.
        self._check_access(allow_closed=True)
        with self._monitor_lock:
            self._monitor_closed = True
            thread, stop = self._monitor_thread, self._monitor_stop
            self._monitor_thread = None
            self._monitor_stop = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(_READBACK_MONITOR_JOIN_TIMEOUT)
        with self._lock:
            self._cancel_reservations.clear()
            self._place_reservations.clear()
        trader = self._listening_trader
        if trader is not None:
            try:
                trader.remove_execution_listener(self._on_native_return)
            except Exception:
                if self._owner_thread_only:
                    return False
        with self._instrument_status_lock:
            self._instrument_status_closed = True
            self._instrument_status_version += 1
            bridge = self._instrument_status_bridge
            if bridge is not None:
                bridge.mark_source_unavailable()
            status_trader = self._instrument_status_listening_trader
            status_listener = self._instrument_status_listener
            self._instrument_status_listening_trader = None
            self._instrument_status_listener = None
        if status_trader is not None:
            remove = getattr(status_trader, "remove_instrument_status_listener", None)
            if callable(remove) and status_listener is not None:
                try:
                    remove(status_listener)
                except Exception:
                    _logger.debug("ctp direct instrument-status listener removal failed")
        self._listening_trader = None
        if self._owner_thread_only:
            self._settlement_ticket = None
            self._owner_closed = True
            return True
        return None


def _settlement_readback_monitor(provider_ref: Any, stop: threading.Event) -> None:
    """Background readback loop of one provider.

    Holds no strong reference to the provider while waiting, so the thread
    also ends when the provider is collected; ``close`` and a released feed
    session end it too.  It never runs on a CTP SPI callback thread.
    """
    current = threading.current_thread()
    try:
        while not stop.wait(_READBACK_MONITOR_INTERVAL):
            provider = provider_ref()
            if provider is None:
                return
            try:
                keep = provider._readback_monitor_tick(stop)
            except Exception:
                _logger.debug("ctp direct settlement readback monitor step failed")
                keep = True
            del provider
            if not keep:
                return
    finally:
        provider = provider_ref()
        if provider is not None:
            provider._readback_monitor_exited(current)


def create_ctp_direct_write_provider(feed: Any) -> CtpDirectWriteProvider:
    """Factory for ``bt_api.direct_write_providers``: wrap the selected CTP feed."""

    client = getattr(feed, "client", feed)
    return CtpDirectWriteProvider(client)


def register_ctp_direct_write_provider(registry: Any) -> None:
    """Register the CTP futures direct write provider factory."""

    register = getattr(registry, "register", None)
    if not callable(register):
        raise TypeError("registry must expose register(exchange_name, factory)")
    register(CTP_DIRECT_WRITE_EXCHANGE, create_ctp_direct_write_provider)
