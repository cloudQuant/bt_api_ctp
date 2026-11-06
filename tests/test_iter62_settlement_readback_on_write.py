"""Iteration 62: settlement readback on the direct write path after a re-login.

Every CTP trader login resets trading readiness until the session reads back
the trading day's settlement confirmation.  After a same-day reconnect nobody
ran that readback, so every later place and cancel failed at the native gate
until the process restarted.  The provider now runs one bounded, read-only
readback before arming a write when this account was already verified for the
same trading day; it never confirms settlement itself (D62-20/29).

Two layers: the real ``TraderClient`` readiness and gate code with a fake
native API (the readback is a real ``ReqQrySettlementInfoConfirm`` round trip
through the query lane), and a scripted trader for exact retry accounting.
All evidence is UNIT/FAKE: no CTP library, front or account is involved.
"""

from __future__ import annotations

import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bt_api_base.direct_write_protocol import (
    DirectWriteError,
    NativeOrderIdentity,
    SettlementStatus,
    WriteOperation,
)
from bt_api_ctp.ctp import client as ctp_client
from bt_api_ctp.ctp.client import (
    TraderClient,
    _issue_ctp_execution_authority_for_core,
    _TraderSpi,
)
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

TD_FRONT = "tcp://offline-neutral-td.invalid:30001"
MD_FRONT = "tcp://offline-neutral-md.invalid:30011"
TODAY = "20261106"
UNVERIFIED = "ctp_direct_settlement_unverified"


class Req:
    """Duck-typed v1 order request (the provider never imports the SDK models)."""

    symbol = "rb2701"
    exchange_id = "SHFE"
    order_type = SimpleNamespace(value="limit")
    side = SimpleNamespace(value="buy")
    price = Decimal("3000")
    quantity = Decimal("1")
    position_id = None
    time_in_force = "GFD"
    offset = "open"
    reduce_only = False
    position_side = None
    position_mode = None
    client_order_id = "t1"


class Verifier:
    """Records grant consumption; checks the grant's own session generation."""

    def __init__(self):
        self.consumed = []

    def consume(self, grant, *, exchange_name, operation, session_generation):
        assert exchange_name == "CTP___FUTURE"
        assert session_generation == grant.session_generation
        self.consumed.append((operation, session_generation))


def _grant(generation, operation=WriteOperation.PLACE):
    return SimpleNamespace(session_generation=generation, operation=operation)


def _feed():
    feed = CtpRequestDataFuture(
        broker_id="9999",
        user_id="account",
        password="secret",
        td_front=TD_FRONT,
        md_front=MD_FRONT,
        auto_settlement_confirm=False,
    )
    feed._connect_timeout = 0.01
    return feed


# ---------------------------------------------------------------- real client


class _NativeApi:
    """Fake native API: settlement readback answers synchronously."""

    def __init__(self):
        self.client = None
        self.calls = []
        self.confirmation_day = TODAY

    def ReqAuthenticate(self, field, request_id):
        return 0

    def ReqQrySettlementInfoConfirm(self, field, request_id):
        self.calls.append("query_settlement")
        self.client._handle_query_callback(
            "settlement_confirmation",
            SimpleNamespace(
                BrokerID="9999", InvestorID="account", ConfirmDate=self.confirmation_day
            ),
            None,
            request_id,
            True,
        )
        return 0

    def ReqSettlementInfoConfirm(self, field, request_id):
        self.calls.append("confirm_settlement")
        return 0

    def ReqOrderInsert(self, field, request_id):
        self.calls.append("insert")
        return 0

    def ReqOrderAction(self, field, request_id):
        self.calls.append("cancel")
        return 0


def _login(client, native, *, session_id, trading_day=TODAY):
    """Front connect + accepted login: a new generation, readiness reset."""

    client._on_front_connected()
    with client._query_state_lock:
        client._authentication_state = "authenticated"
        client._login_state = "logging_in"
        request_id = client._next_request_id()
        client._login_request_id = request_id
        client._login_connection_generation = client._connection_generation
        spi = _TraderSpi(client, native)
        client._spi = spi
    client._session_native_api = native
    client._session_native_front = client._bound_front
    client._query_interval = 0
    spi.OnRspUserLogin(
        SimpleNamespace(
            BrokerID="9999",
            UserID="account",
            TradingDay=trading_day,
            FrontID=7,
            SessionID=session_id,
            MaxOrderRef="0",
        ),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        request_id,
        True,
    )
    state = client.get_session_state()
    assert state["read_only_ready"] is True and state["trading_ready"] is False
    assert state["settlement_state"] == "not_requested"


def _real_setup():
    native = _NativeApi()
    client = TraderClient(TD_FRONT, "9999", "account", "secret", auto_settlement_confirm=False)
    native.client = client
    client._api = native
    _login(client, native, session_id=19)
    feed = _feed()
    feed._trader = client
    provider = CtpDirectWriteProvider(feed)
    capability = _issue_ctp_execution_authority_for_core()
    client.configure_execution_gate(capability)

    def admitted(trader):
        # Stand-in for the feed's SDK admission step (its fronts differ).
        trader.record_sdk_write_admission()
        return capability

    provider._admitted_capability = admitted

    def make_order_native(
        symbol, vol, price, order_type, offset, post_only, client_order_id, extra,
        *, _execution_capability, exchange_id, time_in_force,
    ):
        request_id = client._next_request_id()
        field = SimpleNamespace(
            BrokerID="9999", InvestorID="account", UserID="account",
            InstrumentID=symbol, ExchangeID=exchange_id, OrderRef=client_order_id,
            RequestID=request_id, Direction="0", CombOffsetFlag="0", CombHedgeFlag="1",
            VolumeTotalOriginal=int(vol), LimitPrice=float(price), OrderPriceType="2",
            TimeCondition="3", VolumeCondition="1",
        )
        # The real native gate: armed proof and trading readiness per write.
        ret = client.submit_order_insert(
            field, request_id, execution_capability=_execution_capability
        )
        return SimpleNamespace(get_status=lambda: ret == 0)

    def cancel_order_native(symbol, order_id, extra, *, _execution_capability, **kwargs):
        request_id = client._next_request_id()
        field = SimpleNamespace(
            BrokerID="9999", InvestorID="account", UserID="account",
            InstrumentID=symbol, ExchangeID=kwargs.get("exchange_id", ""), ActionFlag="0",
            OrderRef=kwargs.get("order_ref", ""), FrontID=kwargs.get("front_id", 0),
            SessionID=kwargs.get("session_id", 0), RequestID=request_id,
            OrderActionRef=request_id,
        )
        ret = client.submit_order_action(
            field, request_id, execution_capability=_execution_capability
        )
        return SimpleNamespace(get_status=lambda: ret == 0)

    feed._make_order_native = make_order_native
    feed._cancel_order_native = cancel_order_native
    # Startup: the host's explicit settlement step (ensure_settlement) reads
    # back the confirmation for today; this is the "verified" day.
    assert provider.settlement_status() is SettlementStatus.CONFIRMED
    assert native.calls == ["query_settlement"]
    native.calls.clear()
    return provider, client, native


def _place(provider, verifier=None):
    verifier = verifier or Verifier()
    reservation = provider.reserve_order(Req())
    return provider.submit_order(
        reservation, Req(), _grant(reservation.identity.session_generation), verifier
    )


def _target(order_ref="000000000001", front_id=7, session_id=19):
    return NativeOrderIdentity(
        exchange_name="CTP___FUTURE",
        client_order_id=order_ref,
        fields={
            "order_ref": order_ref,
            "front_id": front_id,
            "session_id": session_id,
            "exchange_id": "SHFE",
            "trading_day": TODAY,
        },
    )


def test_real_client_place_after_same_day_relogin_reads_back_once_and_sends():
    provider, client, native = _real_setup()
    assert _place(provider).sent is True  # trading-ready session: no query
    assert native.calls == ["insert"]
    native.calls.clear()

    _login(client, native, session_id=20)  # same-day reconnect
    verifier = Verifier()
    result = _place(provider, verifier)

    assert result.sent is True
    assert native.calls == ["query_settlement", "insert"]  # exactly one readback
    assert "confirm_settlement" not in native.calls
    assert client.get_request_counts()["settlement_confirm"] == 0
    state = client.get_session_state()
    assert state["trading_ready"] is True
    assert state["settlement_proof_source"] == "confirmation_query"
    assert verifier.consumed == [(WriteOperation.PLACE, state["connection_generation"])]

    native.calls.clear()
    assert _place(provider).sent is True  # ready again: no further readback
    assert native.calls == ["insert"]


def test_real_client_unconfirmed_readback_refuses_place_locally():
    provider, client, native = _real_setup()
    _login(client, native, session_id=20)
    native.confirmation_day = "20261105"  # the venue shows no confirmation for today
    verifier = Verifier()

    with pytest.raises(DirectWriteError) as info:
        _place(provider, verifier)

    assert info.value.code == UNVERIFIED
    assert native.calls == ["query_settlement"]  # no confirmation, nothing sent
    assert client.get_request_counts()["settlement_confirm"] == 0
    assert client.get_request_counts()["order_insert"] == 0
    assert verifier.consumed == []  # refused before the grant is used
    assert client.get_session_state()["trading_ready"] is False


def test_real_client_cancel_after_same_day_relogin_reads_back_once_and_sends():
    provider, client, native = _real_setup()
    _login(client, native, session_id=20)
    verifier = Verifier()
    generation = client.connection_generation

    result = provider.cancel_order(
        _target(), "rb2701", _grant(generation, WriteOperation.CANCEL), verifier
    )

    assert result.sent is True
    assert native.calls == ["query_settlement", "cancel"]
    assert "confirm_settlement" not in native.calls
    assert verifier.consumed == [(WriteOperation.CANCEL, generation)]


def test_real_client_unconfirmed_readback_refuses_cancel_locally():
    provider, client, native = _real_setup()
    _login(client, native, session_id=20)
    native.confirmation_day = "20261105"
    verifier = Verifier()
    generation = client.connection_generation

    with pytest.raises(DirectWriteError) as info:
        provider.cancel_order(
            _target(), "rb2701", _grant(generation, WriteOperation.CANCEL), verifier
        )

    assert info.value.code == UNVERIFIED
    assert native.calls == ["query_settlement"]
    assert client.get_request_counts()["order_action"] == 0
    assert verifier.consumed == []


def test_real_client_new_trading_day_is_left_to_the_explicit_settlement_step():
    provider, client, native = _real_setup()
    _login(client, native, session_id=20, trading_day="20261109")  # day switch

    with pytest.raises(DirectWriteError) as info:
        _place(provider)

    # Unchanged behaviour: no implicit readback for a day this provider never
    # verified; the native gate refuses and the host runs ensure_settlement.
    assert info.value.code == "ctp_execution_gate_session_not_trading_ready"
    assert native.calls == []


# ------------------------------------------------------------ scripted trader


class ScriptedTrader:
    """Readiness state machine of ``TraderClient`` with scripted readbacks."""

    def __init__(self):
        self.listeners = []
        self.generation = 4
        self.session_id = 99
        self.trading_day = TODAY
        self.read_only_ready = True
        self.settlement_state = "confirmed"
        self.readback = True
        self.armed = []
        self.outcomes = []
        self.verify_timeouts = []
        self.confirm_calls = 0
        self.on_verify = None
        self._next = 40

    @property
    def connection_generation(self):
        return self.generation

    @property
    def is_trading_ready(self):
        return self.read_only_ready and self.settlement_state == "confirmed" and self.readback

    def relogin(self, trading_day=None):
        self.generation += 1
        self.session_id += 1
        if trading_day is not None:
            self.trading_day = trading_day
        self.settlement_state = "not_requested"
        self.readback = False
        self.armed = []

    def add_execution_listener(self, listener):
        self.listeners.append(listener)

    def remove_execution_listener(self, listener):
        self.listeners.remove(listener)

    def get_session_state(self):
        return {
            "read_only_ready": self.read_only_ready,
            "trading_ready": self.is_trading_ready,
            "settlement_state": self.settlement_state,
            "front_id": 3,
            "session_id": self.session_id,
            "trading_day": self.trading_day,
            "connection_generation": self.generation,
            "account_fingerprint": "fixture-account",
        }

    def next_order_ref(self):
        self._next += 1
        return str(self._next)

    def verify_settlement_confirmation(self, timeout=5, *, should_stop=None):
        self.verify_timeouts.append(timeout)
        if self.on_verify is not None:
            self.on_verify()
        outcome = self.outcomes.pop(0) if self.outcomes else "confirmed"
        self.readback = False  # a readback first clears the previous proof
        if outcome == "confirmed":
            self.settlement_state = "confirmed"
            self.readback = True
            return SimpleNamespace(
                complete=True, timed_out=False, error_code=None, error_message="",
                completion_origin="success",
            )
        if outcome == "unconfirmed":
            return SimpleNamespace(
                complete=True, timed_out=False, error_code=None, error_message="",
                completion_origin="success",
            )
        if outcome == "timeout":
            return SimpleNamespace(
                complete=False, timed_out=True, error_code=None, error_message="",
                completion_origin="timeout",
            )
        if outcome == "slot_busy":
            return SimpleNamespace(
                complete=False, timed_out=True, error_code=-1,
                error_message="query_slot_busy", completion_origin="query_slot_busy",
            )
        if outcome == "submit_rejected":
            return SimpleNamespace(
                complete=False, timed_out=False, error_code=-2,
                error_message="query_submit_rejected", completion_origin="submit_rejected",
            )
        raise AssertionError(outcome)

    def confirm_settlement_for_sdk_admission(self, capability, *, timeout=15.0):
        self.confirm_calls += 1
        self.settlement_state = "confirmed"
        self.readback = True
        return True

    def confirm_settlement(self, *args, **kwargs):  # pragma: no cover - must not run
        self.confirm_calls += 1
        raise AssertionError("implicit settlement confirmation")

    def get_execution_gate_state(self):
        return {"armed": bool(self.armed), "authorized_instruments": list(self.armed)}

    def disarm_execution_gate(self, capability, reason):
        self.armed = []

    def arm_execution_for_sdk_admission(self, *, capability, instrument_id, exchange_id, **_):
        if not self.is_trading_ready:
            raise ctp_client.CtpExecutionGateError(
                "ctp_execution_gate_session_not_trading_ready"
            )
        self.armed = [ctp_client.canonical_ctp_instrument(instrument_id, exchange_id)]
        return {"armed": True}


def _scripted(*, verified=True):
    feed = _feed()
    trader = ScriptedTrader()
    feed._trader = trader
    provider = CtpDirectWriteProvider(feed)
    provider._admitted_capability = lambda _trader: "capability"
    sent = []

    def make_order_native(symbol, *args, _execution_capability, **kwargs):
        assert trader.is_trading_ready and trader.armed  # the native gate
        sent.append(("insert", symbol, trader.generation))
        return SimpleNamespace(get_status=lambda: True)

    def cancel_order_native(symbol, order_id, extra, *, _execution_capability, **kwargs):
        assert trader.is_trading_ready and trader.armed
        sent.append(("cancel", symbol, trader.generation))
        return SimpleNamespace(get_status=lambda: True)

    feed._make_order_native = make_order_native
    feed._cancel_order_native = cancel_order_native
    if verified:
        assert provider.settlement_status() is SettlementStatus.CONFIRMED
    return provider, trader, sent


def _cancel(provider, trader, verifier=None):
    target = _target(front_id=3, session_id=trader.session_id)
    return provider.cancel_order(
        target,
        "rb2701",
        _grant(trader.generation, WriteOperation.CANCEL),
        verifier or Verifier(),
    )


def test_trading_ready_session_does_no_readback_for_place_or_cancel():
    provider, trader, sent = _scripted()
    assert _place(provider).sent is True
    assert _cancel(provider, trader).sent is True
    assert _place(provider).sent is True
    assert trader.verify_timeouts == []  # settlement_status found it ready
    assert trader.confirm_calls == 0
    assert [kind for kind, *_ in sent] == ["insert", "cancel", "insert"]


def test_readback_uses_the_bounded_settlement_timeout():
    provider, trader, sent = _scripted()
    trader.relogin()
    assert _place(provider).sent is True
    assert trader.verify_timeouts == [10.0]
    assert trader.confirm_calls == 0


def test_failed_readback_is_not_repeated_in_the_same_generation():
    provider, trader, sent = _scripted()
    trader.relogin()
    trader.outcomes = ["unconfirmed"]

    with pytest.raises(DirectWriteError) as first:
        _place(provider)
    assert first.value.code == UNVERIFIED
    assert len(trader.verify_timeouts) == 1

    # Second place and a cancel in the same generation: refused locally with
    # the same code and no further query (no blind retry).
    with pytest.raises(DirectWriteError) as second:
        _place(provider)
    with pytest.raises(DirectWriteError) as third:
        _cancel(provider, trader)
    assert second.value.code == third.value.code == UNVERIFIED
    assert len(trader.verify_timeouts) == 1
    assert trader.confirm_calls == 0
    assert sent == []

    # A new session generation (same day) gets its own single readback.
    trader.relogin()
    assert _place(provider).sent is True
    assert len(trader.verify_timeouts) == 2
    assert sent == [("insert", "rb2701", trader.generation)]


def test_timed_out_readback_is_not_retried_in_the_same_generation():
    provider, trader, sent = _scripted()
    trader.relogin()
    trader.outcomes = ["timeout", "confirmed"]
    for _ in range(3):
        with pytest.raises(DirectWriteError) as info:
            _place(provider)
        assert info.value.code == UNVERIFIED
    assert len(trader.verify_timeouts) == 1  # the outcome was not definite
    assert sent == []


@pytest.mark.parametrize("not_sent", ["slot_busy", "submit_rejected"])
def test_readback_that_never_reached_the_venue_may_be_retried_by_the_next_write(not_sent):
    provider, trader, sent = _scripted()
    trader.relogin()
    trader.outcomes = [not_sent, "confirmed"]

    with pytest.raises(DirectWriteError) as info:
        _place(provider)
    assert info.value.code == UNVERIFIED
    assert _place(provider).sent is True  # one more readback, then ready
    assert len(trader.verify_timeouts) == 2
    assert trader.confirm_calls == 0


def test_not_sent_readback_retries_are_capped_per_generation():
    provider, trader, sent = _scripted()
    trader.relogin()
    trader.outcomes = ["slot_busy"] * 10
    for _ in range(6):
        with pytest.raises(DirectWriteError) as info:
            _place(provider)
        assert info.value.code == UNVERIFIED
    assert len(trader.verify_timeouts) == 3
    assert sent == []


def test_no_readback_before_the_provider_verified_the_trading_day():
    provider, trader, sent = _scripted(verified=False)
    trader.relogin()
    with pytest.raises(DirectWriteError) as info:
        _place(provider)
    assert info.value.code == "ctp_execution_gate_session_not_trading_ready"
    assert trader.verify_timeouts == []
    assert trader.confirm_calls == 0


def test_no_readback_while_a_confirmation_is_pending_or_failed():
    provider, trader, sent = _scripted()
    for state in ("confirming", "failed", "unknown"):
        trader.relogin()
        trader.settlement_state = state
        with pytest.raises(DirectWriteError) as info:
            _place(provider)
        assert info.value.code == "ctp_execution_gate_session_not_trading_ready"
    assert trader.verify_timeouts == []
    assert trader.confirm_calls == 0


def test_explicit_confirmation_also_marks_the_day_verified():
    provider, trader, sent = _scripted(verified=False)
    trader.relogin()
    trader.outcomes = ["unconfirmed"]
    assert provider.settlement_status() is SettlementStatus.UNCONFIRMED
    assert provider.confirm_settlement(_grant(trader.generation), Verifier()) is (
        SettlementStatus.CONFIRMED
    )
    assert trader.confirm_calls == 1  # the explicit SDK step only
    trader.relogin()
    assert _place(provider).sent is True
    assert trader.confirm_calls == 1
    assert len(trader.verify_timeouts) == 2


def test_session_change_during_the_readback_refuses_the_place():
    provider, trader, sent = _scripted()
    trader.relogin()
    reservation = provider.reserve_order(Req())
    # A reconnect lands while the readback runs; the new session is ready.
    trader.on_verify = trader.relogin
    verifier = Verifier()
    with pytest.raises(DirectWriteError) as info:
        provider.submit_order(
            reservation, Req(), _grant(reservation.identity.session_generation), verifier
        )
    assert info.value.code == "ctp_direct_reservation_session_changed"
    assert verifier.consumed == [] and sent == []


def test_readback_does_not_block_return_draining_or_the_native_return_tap():
    provider, trader, sent = _scripted()
    trader.relogin()
    provider.ensure_listening()
    listener = trader.listeners[-1]
    drained = []
    finished = threading.Event()

    def other_thread():
        listener(
            "order",
            {
                "InstrumentID": "rb2701",
                "OrderRef": "000000000041",
                "OrderStatus": "3",
                "OrderSubmitStatus": "3",
                "_connection_generation": trader.generation,
            },
        )
        drained.extend(provider.drain_execution_events(10))
        finished.set()

    def verify_hook():
        thread = threading.Thread(target=other_thread, daemon=True)
        thread.start()
        # The readback holds the provider lock; returns must still flow.
        assert finished.wait(5.0), "drain blocked behind the settlement readback"

    trader.on_verify = verify_hook
    assert _place(provider).sent is True
    assert len(drained) == 1
