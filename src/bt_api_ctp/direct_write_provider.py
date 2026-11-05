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

    def __init__(self, feed: Any) -> None:
        from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

        if type(feed) is not CtpRequestDataFuture:
            raise DirectWriteError("ctp_direct_write_feed_invalid")
        self._feed = feed
        self._lock = threading.RLock()
        self._events: deque[DirectExecutionEvent] = deque()
        self._events_lock = threading.Lock()
        self._catalog_lock = threading.Lock()
        self._catalog_day: Optional[str] = None
        self._catalog_venues: dict[str, Optional[str]] = {}
        self.catalog_queries = 0
        self._sequence = 0
        self._dropped_events = 0
        self._listening_trader: Any = None
        self._reservations: dict[str, DirectOrderReservation] = {}
        self._cycle_id = f"ctp-direct-{uuid4().hex}"
        self._strategy_identity_sha256 = hashlib.sha256(
            f"ctp-direct-host|{self._cycle_id}".encode("utf-8")
        ).hexdigest()
        self._capability: Any = None

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
                    return SettlementStatus.CONFIRMED
                result = trader.verify_settlement_confirmation(timeout=10.0)
                if trader.is_trading_ready:
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

    def resolve_exchange_id(self, symbol: str) -> Optional[str]:
        """Venue (``ExchangeID``) of ``symbol`` from the instrument catalog.

        Cached per trading day in this process; one catalog query fills the
        cache for every returned instrument (D62-11 first level).  Returns
        ``None`` when the catalog does not prove a unique venue.
        """
        if not symbol:
            return None
        trader = self._trader()
        day = self._session_fields(trader).get("trading_day")
        with self._catalog_lock:
            if self._catalog_day != day:
                self._catalog_day = day
                self._catalog_venues = {}
            if symbol in self._catalog_venues:
                return self._catalog_venues[symbol]
        try:
            result = self._feed.query_instruments_result(instrument_id=symbol, timeout=10)
        except Exception:
            return None
        if getattr(result, "complete", False) is not True:
            return None
        venues: dict[str, set[str]] = {}
        for row in result.records:
            instrument = _text(_row_value(row, "InstrumentID"))
            venue = _text(_row_value(row, "ExchangeID"))
            if instrument and venue:
                venues.setdefault(instrument, set()).add(venue)
        with self._catalog_lock:
            for instrument, found in venues.items():
                self._catalog_venues[instrument] = next(iter(found)) if len(found) == 1 else None
            self.catalog_queries += 1
            return self._catalog_venues.get(symbol)

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
            ok = (
                getattr(result, "complete", False) is True
                and getattr(result, "timed_out", False) is not True
                and getattr(result, "error_code", None) in (None, 0)
                and getattr(result, "connection_generation", session["connection_generation"])
                == session["connection_generation"]
            )
            if not ok:
                detail = "{}:{}:{}:{}".format(
                    getattr(result, "error_code", None),
                    "timeout" if getattr(result, "timed_out", False) else "",
                    "complete" if getattr(result, "complete", False) else "partial",
                    str(getattr(result, "error_message", "") or "")[:60],
                )
                blockers.append(f"{name}_query_incomplete[{detail}]")
            return ok

        position_facts: Optional[tuple[DirectPositionFact, ...]] = None
        if positions:
            result = self._feed.query_positions_result(timeout=timeout)
            if _complete(result, "positions"):
                totals: dict[tuple[str, str, str], list[float]] = {}
                for row in result.records:
                    symbol = _text(_row_value(row, "InstrumentID"))
                    venue = _text(_row_value(row, "ExchangeID"))
                    side = {"1": "net", "2": "long", "3": "short"}.get(
                        _text(_row_value(row, "PosiDirection"))
                    )
                    quantity = _float_or_none(_row_value(row, "Position")) or 0.0
                    if not symbol or side is None or quantity <= 0:
                        continue
                    today = _float_or_none(_row_value(row, "TodayPosition"))
                    bucket = totals.setdefault((symbol, venue, side), [0.0, 0.0, 0.0])
                    bucket[0] += quantity
                    if today is None:
                        bucket[2] = 1.0  # split unknown for this key
                    else:
                        bucket[1] += min(max(today, 0.0), quantity)
                position_facts = tuple(
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
