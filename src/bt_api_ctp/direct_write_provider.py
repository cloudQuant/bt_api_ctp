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
* after a same-day re-login, restore trading readiness before a write with
  one bounded, read-only settlement readback (never a confirmation);
* convert native order/trade/error returns into neutral
  :class:`~bt_api_base.direct_write_protocol.DirectExecutionEvent` rows.

Nothing here decides admission, and no front registry is consulted: the same
path serves simulation and production accounts (D62-01/02).
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import Any, Optional
from uuid import uuid4

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
    NativeOrderIdentity,
    SettlementStatus,
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

__all__ = (
    "CTP_DIRECT_WRITE_EXCHANGE",
    "CtpDirectWriteProvider",
    "create_ctp_direct_write_provider",
    "project_ctp_execution_event",
    "register_ctp_direct_write_provider",
)

CTP_DIRECT_WRITE_EXCHANGE = "CTP___FUTURE"
_MAX_EVENTS = 50_000
_ORDER_REF_DIGITS = 12

# Settlement confirmation readback (read-only ReqQrySettlementInfoConfirm).
_SETTLEMENT_READBACK_TIMEOUT = 10.0
# Write-path readbacks per trader session generation: the first one, plus a
# retry only after an attempt that provably never reached the venue.
_MAX_WRITE_READBACKS_PER_GENERATION = 3
_READBACK_NOT_SENT_ORIGINS = frozenset({"query_slot_busy", "submit_rejected"})
# Settlement states in which only this session's readback is missing:
# a fresh login, or a confirmation acknowledged without its readback.
_READBACK_SETTLEMENT_STATES = frozenset({"not_requested", "confirmed"})

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

    def __init__(self, feed: Any, *, catalog_cache: Any = None) -> None:
        from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

        if type(feed) is not CtpRequestDataFuture:
            raise DirectWriteError("ctp_direct_write_feed_invalid")
        self._feed = feed
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
        self._dropped_events = 0
        self._listening_trader: Any = None
        self._reservations: dict[str, DirectOrderReservation] = {}
        self._cycle_id = f"ctp-direct-{uuid4().hex}"
        self._strategy_identity_sha256 = hashlib.sha256(
            f"ctp-direct-host|{self._cycle_id}".encode("utf-8")
        ).hexdigest()
        self._capability: Any = None
        # Write-path settlement readback state (guarded by ``_lock``): the
        # (trading day, account fingerprint) this provider saw trading-ready,
        # and the attempts made for one (trader, connection generation).
        self._settled_day: Optional[tuple[str, str]] = None
        self._readback_scope: Optional[tuple[Any, int]] = None
        self._readback_attempts = 0
        self._readback_retry_allowed = False

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------
    def _trader(self) -> Any:
        feed = self._feed
        if feed.trader_client is None:
            feed.ensure_read_only_connected()
        trader = feed.trader_client
        if trader is None:
            raise DirectWriteError("ctp_direct_session_unavailable")
        if trader is not self._listening_trader:
            trader.add_execution_listener(self._on_native_return)
            self._listening_trader = trader
        return trader

    def ensure_listening(self) -> None:
        """Connect the TD session (read-only login) and attach the return tap."""

        with self._lock:
            self._trader()

    def session_generation(self) -> Optional[int]:
        trader = self._feed.trader_client
        if trader is None:
            return None
        try:
            return int(trader.connection_generation)
        except Exception:
            return None

    def trading_day(self) -> Optional[str]:
        trader = self._feed.trader_client
        if trader is None:
            return None
        state = trader.get_session_state()
        value = state.get("trading_day") if isinstance(state, dict) else None
        return str(value) if value else None

    def login_clock_observation(self) -> Optional[dict[str, Any]]:
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
        changes the caller's result.
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

    def _settlement_readback_for_write(self, trader: Any) -> bool:
        """Restore trading readiness after a same-day re-login (lock held).

        Every CTP login resets readiness until the session reads back the
        trading day's settlement confirmation.  When this account was already
        trading-ready on the session's trading day and only this session's
        readback is missing, run one bounded, read-only readback before the
        write is armed.  Settlement is never confirmed here (an explicit SDK
        step, D62-20/29), and a new trading day is left to that step.

        At most one readback per trader session generation; a later write may
        retry only after an attempt that provably never reached the venue
        (query slot busy, request rejected locally), capped per generation.
        Returns True when a readback ran (the caller re-reads the session);
        raises ``ctp_direct_settlement_unverified`` when the session is still
        not trading-ready.  Any other not-ready state is left to the native
        gate, unchanged.
        """
        state = trader.get_session_state()
        if not isinstance(state, dict):
            return False
        if state.get("trading_ready") is True:
            self._note_trading_ready(trader, state)
            return False
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
            return False
        scope = self._readback_scope
        if scope is None or scope[0] is not trader or scope[1] != generation:
            self._readback_scope = (trader, generation)
            self._readback_attempts = 0
            self._readback_retry_allowed = True
        if (
            not self._readback_retry_allowed
            or self._readback_attempts >= _MAX_WRITE_READBACKS_PER_GENERATION
        ):
            raise DirectWriteError(
                "ctp_direct_settlement_unverified", "settlement_readback_not_repeated"
            )
        self._readback_attempts += 1
        self._readback_retry_allowed = False
        try:
            result = trader.verify_settlement_confirmation(timeout=_SETTLEMENT_READBACK_TIMEOUT)
        except Exception:
            result = None
        if trader.is_trading_ready:
            self._note_trading_ready(trader)
            return True
        if getattr(result, "completion_origin", None) in _READBACK_NOT_SENT_ORIGINS:
            self._readback_retry_allowed = True
            detail = "settlement_readback_not_sent"
        elif (
            getattr(result, "complete", False) is True
            and getattr(result, "timed_out", False) is not True
            and getattr(result, "error_code", None) in (None, 0)
        ):
            detail = "settlement_readback_unconfirmed"
        else:
            detail = "settlement_readback_incomplete"
        raise DirectWriteError("ctp_direct_settlement_unverified", detail)

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    def reserve_order(self, request: Any) -> DirectOrderReservation:
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
        with self._lock:
            self._reservations.pop(reservation.reservation_id, None)

    def submit_order(
        self,
        reservation: DirectOrderReservation,
        request: Any,
        grant: WriteGrant,
        verifier: WriteGrantVerifier,
    ) -> DirectSubmitResult:
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

    def cancel_order(
        self,
        target: NativeOrderIdentity,
        symbol: str,
        grant: WriteGrant,
        verifier: WriteGrantVerifier,
    ) -> DirectSubmitResult:
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
        with self._lock:
            try:
                trader = self._trader()
                if trader.is_trading_ready:
                    self._note_trading_ready(trader)
                    return SettlementStatus.CONFIRMED
                result = trader.verify_settlement_confirmation(
                    timeout=_SETTLEMENT_READBACK_TIMEOUT
                )
                if trader.is_trading_ready:
                    self._note_trading_ready(trader)
                    return SettlementStatus.CONFIRMED
                # A complete, error-free readback for this session that holds
                # no confirmation for the current trading day means the
                # account still has to confirm.  Incomplete, timed-out or
                # failed readbacks stay UNKNOWN (no blind confirmation).
                error_code = getattr(result, "error_code", None)
                if (
                    getattr(result, "complete", False) is True
                    and getattr(result, "timed_out", False) is not True
                    and error_code in (None, 0)
                    and getattr(result, "error_message", "") != "query_cancelled"
                ):
                    return SettlementStatus.UNCONFIRMED
                return SettlementStatus.UNKNOWN
            except Exception:
                return SettlementStatus.UNKNOWN

    def confirm_settlement(
        self, grant: WriteGrant, verifier: WriteGrantVerifier
    ) -> SettlementStatus:
        from bt_api_ctp.ctp import client as ctp_client

        with self._lock:
            trader = self._trader()
            session = self._session_fields(trader)
            verifier.consume(
                grant,
                exchange_name=CTP_DIRECT_WRITE_EXCHANGE,
                operation=WriteOperation.SETTLEMENT_CONFIRM,
                session_generation=session["connection_generation"],
            )
            try:
                capability = self._admitted_capability(trader)
                state = trader.get_execution_gate_state()
                if isinstance(state, dict) and state.get("armed") is True:
                    trader.disarm_execution_gate(capability, "ctp_direct_settlement")
                ready = trader.confirm_settlement_for_sdk_admission(capability, timeout=15.0)
            except ctp_client.CtpExecutionGateError as exc:
                raise DirectWriteError(
                    getattr(exc, "code", None) or "ctp_direct_settlement_failed", str(exc)
                ) from None
            if ready:
                self._note_trading_ready(trader)
            return SettlementStatus.CONFIRMED if ready else SettlementStatus.UNKNOWN

    # ------------------------------------------------------------------
    # returns
    # ------------------------------------------------------------------
    def _on_native_return(self, kind: str, payload: dict[str, Any]) -> None:
        with self._events_lock:
            self._sequence += 1
            sequence = self._sequence
        try:
            event = project_ctp_execution_event(kind, payload, sequence=sequence)
        except Exception:
            event = None
        if event is None:
            return
        with self._events_lock:
            if len(self._events) >= _MAX_EVENTS:
                self._events.popleft()
                self._dropped_events += 1
            self._events.append(event)

    def drain_execution_events(self, max_events: int) -> tuple[DirectExecutionEvent, ...]:
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
        error for the current session.  Rows are projected verbatim; nothing is
        attributed to a strategy here.
        """
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
        trader = self._trader()
        fingerprint = getattr(trader, "_account_fingerprint", None)
        return str(fingerprint) if fingerprint else None

    @property
    def dropped_events(self) -> int:
        return self._dropped_events

    def close(self) -> None:
        trader = self._listening_trader
        if trader is not None:
            try:
                trader.remove_execution_listener(self._on_native_return)
            except Exception:
                pass
        self._listening_trader = None


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
