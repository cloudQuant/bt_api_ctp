"""Iteration 62: settlement readback on the direct write path after a re-login.

Every CTP trader login resets trading readiness until the session reads back
the trading day's settlement confirmation.  After a same-day reconnect nobody
ran that readback, so every later place and cancel failed at the native gate
until the process restarted.  When this account was already verified for the
same trading day, the provider restores readiness with bounded, read-only
readbacks; it never confirms settlement itself (D62-20/29).

I62-17 revision (2026-11-06): a background monitor runs the readback as soon
as the same-day re-login is visible and retries it on a backoff; the write
path waits for an in-flight background readback, runs one inline readback
only outside a cooldown, and otherwise refuses locally
(``settlement_readback_pending``).

Two layers: the real ``TraderClient`` readiness and gate code with a fake
native API (the readback is a real ``ReqQrySettlementInfoConfirm`` round trip
through the query lane), and a scripted trader for exact retry accounting.
The monitor is idle (one-hour interval) unless a test opts in with
``fast_monitor``; cooldown and backoff use the fake ``clock``.
All evidence is UNIT/FAKE: no CTP library, front or account is involved.
"""

from __future__ import annotations

import gc
import threading
import time
import weakref
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bt_api_base.direct_write_protocol import (
    DirectWriteError,
    NativeOrderIdentity,
    SettlementStatus,
    WriteOperation,
)
from bt_api_ctp import direct_write_provider as provider_module
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
PENDING = "settlement_readback_pending"
MONITOR = "ctp-direct-settlement-readback"

_PROVIDERS: weakref.WeakSet[CtpDirectWriteProvider] = weakref.WeakSet()


def _monitor_threads():
    return [thread for thread in threading.enumerate() if thread.name == MONITOR]


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        threading.Event().wait(0.005)


def _settle():
    """Let a fast monitor run several ticks."""
    threading.Event().wait(0.15)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def _idle_monitor(monkeypatch):
    """Monitor idle by default; every provider closed and no thread left."""

    monkeypatch.setattr(provider_module, "_READBACK_MONITOR_INTERVAL", 3600.0, raising=False)
    yield
    for provider in list(_PROVIDERS):
        provider.close()
    assert _monitor_threads() == []


@pytest.fixture
def fast_monitor(monkeypatch):
    monkeypatch.setattr(provider_module, "_READBACK_MONITOR_INTERVAL", 0.01, raising=False)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(provider_module, "_monotonic", fake, raising=False)
    return fake


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


def _login(client, native, *, session_id, trading_day=TODAY, check=True):
    """Front connect + accepted login: a new generation, readiness reset.

    ``check=False`` when a fast monitor may already restore readiness.
    """

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
    if check:
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
    _PROVIDERS.add(provider)
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
    """Readiness state machine of ``TraderClient`` with scripted readbacks.

    One mutex stands in for the client's ``_query_state_lock`` so a monitor
    thread never sees half a re-login.
    """

    def __init__(self):
        self.mutex = threading.RLock()
        self.account = "fixture-account"
        self.state_reads = 0
        self.verify_threads = []
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
        with self.mutex:
            return (
                self.read_only_ready and self.settlement_state == "confirmed" and self.readback
            )

    def relogin(self, trading_day=None, account=None, settlement_state="not_requested"):
        with self.mutex:
            self.generation += 1
            self.session_id += 1
            if trading_day is not None:
                self.trading_day = trading_day
            if account is not None:
                self.account = account
            self.settlement_state = settlement_state
            self.readback = False
            self.armed = []

    def add_execution_listener(self, listener):
        self.listeners.append(listener)

    def remove_execution_listener(self, listener):
        self.listeners.remove(listener)

    def get_session_state(self):
        with self.mutex:
            self.state_reads += 1
            return {
                "read_only_ready": self.read_only_ready,
                "trading_ready": self.is_trading_ready,
                "settlement_state": self.settlement_state,
                "front_id": 3,
                "session_id": self.session_id,
                "trading_day": self.trading_day,
                "connection_generation": self.generation,
                "account_fingerprint": self.account,
            }

    def next_order_ref(self):
        self._next += 1
        return str(self._next)

    def verify_settlement_confirmation(self, timeout=5, *, should_stop=None):
        self.verify_timeouts.append(timeout)
        self.verify_threads.append(threading.current_thread().name)
        if self.on_verify is not None:
            self.on_verify()
        outcome = self.outcomes.pop(0) if self.outcomes else "confirmed"
        with self.mutex:
            self.readback = False  # a readback first clears the previous proof
            if outcome == "confirmed":
                self.settlement_state = "confirmed"
                self.readback = True
        if outcome == "confirmed":
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
    _PROVIDERS.add(provider)
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


@pytest.mark.parametrize("failure", ["unconfirmed", "timeout"])
def test_failed_readback_refuses_writes_within_the_cooldown_then_retries(failure, clock):
    # Replaces "failed/timed-out readback is not repeated in the same
    # generation": the generation is no longer closed after one failure.
    provider, trader, sent = _scripted()
    trader.relogin()
    trader.outcomes = [failure]

    with pytest.raises(DirectWriteError) as first:
        _place(provider)
    assert first.value.code == UNVERIFIED
    assert first.value.detail in (
        "settlement_readback_unconfirmed",
        "settlement_readback_incomplete",
    )
    assert len(trader.verify_timeouts) == 1

    # Within the cooldown: a place and a cancel are refused at once, locally,
    # with the fixed pending detail and no further query.
    clock.advance(4.9)
    started = time.monotonic()
    with pytest.raises(DirectWriteError) as second:
        _place(provider)
    with pytest.raises(DirectWriteError) as third:
        _cancel(provider, trader)
    assert time.monotonic() - started < 1.0
    assert second.value.code == third.value.code == UNVERIFIED
    assert second.value.detail == third.value.detail == PENDING
    assert len(trader.verify_timeouts) == 1
    assert trader.confirm_calls == 0
    assert sent == []

    # After the cooldown the next write runs one more readback and sends.
    clock.advance(0.1)
    assert _place(provider).sent is True
    assert len(trader.verify_timeouts) == 2
    assert trader.verify_threads == ["MainThread", "MainThread"]
    assert trader.confirm_calls == 0
    assert sent == [("insert", "rb2701", trader.generation)]


def test_new_generation_resets_the_cooldown(clock):
    provider, trader, sent = _scripted()
    trader.relogin()
    trader.outcomes = ["timeout"]
    with pytest.raises(DirectWriteError):
        _place(provider)
    # A same-day reconnect starts a new cycle: no cooldown carried over.
    trader.relogin()
    assert _place(provider).sent is True
    assert len(trader.verify_timeouts) == 2


@pytest.mark.parametrize("not_sent", ["slot_busy", "submit_rejected"])
def test_not_sent_readback_is_retried_after_the_cooldown(not_sent, clock):
    # Replaces "may be retried by the next write": the cooldown now applies
    # to every attempt, including one that never reached the venue.
    provider, trader, sent = _scripted()
    trader.relogin()
    trader.outcomes = [not_sent, "confirmed"]

    with pytest.raises(DirectWriteError) as info:
        _place(provider)
    assert info.value.code == UNVERIFIED
    assert info.value.detail == "settlement_readback_not_sent"
    with pytest.raises(DirectWriteError) as pending:
        _place(provider)
    assert pending.value.detail == PENDING
    assert len(trader.verify_timeouts) == 1

    clock.advance(5.0)
    assert _place(provider).sent is True  # one more readback, then ready
    assert len(trader.verify_timeouts) == 2
    assert trader.confirm_calls == 0


def test_readbacks_are_capped_per_generation(monkeypatch, clock):
    # Replaces the cap of three not-sent retries: one cap now counts every
    # attempt (background and write path) of a generation.
    monkeypatch.setattr(provider_module, "_MAX_READBACKS_PER_GENERATION", 3, raising=False)
    provider, trader, sent = _scripted()
    trader.relogin()
    trader.outcomes = ["slot_busy"] * 10
    details = []
    for _ in range(6):
        with pytest.raises(DirectWriteError) as info:
            _place(provider)
        assert info.value.code == UNVERIFIED
        details.append(info.value.detail)
        clock.advance(5.0)
    assert len(trader.verify_timeouts) == 3
    assert details[3:] == ["settlement_readback_not_repeated"] * 3
    assert sent == []

    trader.relogin()  # a new generation gets a new budget
    trader.outcomes = []
    assert _place(provider).sent is True
    assert len(trader.verify_timeouts) == 4


def test_generation_cap_covers_the_backoff_schedule_over_a_session():
    # The fixtures patch these names with raising=False: they must exist.
    assert callable(provider_module._monotonic)
    assert provider_module._READBACK_MONITOR_INTERVAL == 3600.0  # idle fixture
    backoff = provider_module._READBACK_BACKOFF
    assert backoff == (2.0, 5.0, 10.0, 20.0, 30.0)
    cap = provider_module._MAX_READBACKS_PER_GENERATION
    # Time the background retries alone take to use the whole budget.
    span = sum(backoff[min(i, len(backoff) - 1)] for i in range(cap - 1))
    assert span >= 6 * 3600  # longer than any CTP continuous session
    assert provider_module._READBACK_WRITE_COOLDOWN == 5.0


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


# ------------------------------------------------- background readback (I62-17)


def test_real_client_background_readback_after_same_day_relogin_without_a_write(fast_monitor):
    provider, client, native = _real_setup()
    _login(client, native, session_id=20, check=False)  # same-day reconnect

    # No write at all: the monitor reads back and the gate opens again.
    _wait_for(lambda: client.is_trading_ready)
    assert native.calls == ["query_settlement"]
    assert "confirm_settlement" not in native.calls
    assert client.get_request_counts()["settlement_confirm"] == 0
    assert client.get_session_state()["settlement_proof_source"] == "confirmation_query"

    # The next write goes straight through: no second readback.
    assert _place(provider).sent is True
    assert native.calls == ["query_settlement", "insert"]


def test_write_during_an_in_flight_background_readback_waits_and_sends(fast_monitor):
    provider, trader, sent = _scripted()
    in_flight = threading.Event()
    release = threading.Event()

    def hold_readback():
        in_flight.set()
        assert release.wait(5.0)

    trader.on_verify = hold_readback
    trader.relogin()
    assert in_flight.wait(3.0), "no background readback"

    results = []
    done = threading.Event()

    def strategy_order():
        try:
            results.append(_place(provider))
        except Exception as exc:  # pragma: no cover - reported below
            results.append(exc)
        done.set()

    worker = threading.Thread(target=strategy_order, daemon=True)
    worker.start()
    assert not done.wait(0.2)  # the write waits for the readback in flight
    release.set()
    assert done.wait(5.0)
    worker.join(5.0)

    assert len(results) == 1 and getattr(results[0], "sent", None) is True
    assert trader.verify_threads == [MONITOR]  # no second (inline) readback
    assert trader.confirm_calls == 0
    assert sent == [("insert", "rb2701", trader.generation)]


def test_background_retry_follows_the_backoff_and_success_opens_the_gate(fast_monitor, clock):
    provider, trader, sent = _scripted()
    trader.outcomes = ["timeout", "timeout", "confirmed"]
    trader.relogin()
    _wait_for(lambda: len(trader.verify_timeouts) == 1)
    _settle()
    assert len(trader.verify_timeouts) == 1 and not trader.is_trading_ready

    # A write right after the failed background attempt: refused at once.
    with pytest.raises(DirectWriteError) as info:
        _place(provider)
    assert (info.value.code, info.value.detail) == (UNVERIFIED, PENDING)
    assert len(trader.verify_timeouts) == 1

    clock.advance(1.9)
    _settle()
    assert len(trader.verify_timeouts) == 1  # first backoff is 2 s
    clock.advance(0.1)
    _wait_for(lambda: len(trader.verify_timeouts) == 2)
    clock.advance(4.9)
    _settle()
    assert len(trader.verify_timeouts) == 2  # second backoff is 5 s
    clock.advance(0.1)
    _wait_for(lambda: trader.is_trading_ready)

    assert trader.verify_timeouts == [10.0, 10.0, 10.0]
    assert trader.verify_threads == [MONITOR] * 3
    assert trader.confirm_calls == 0
    assert _place(provider).sent is True
    assert len(trader.verify_timeouts) == 3
    _settle()
    assert len(trader.verify_timeouts) == 3  # ready: no further readback


@pytest.mark.parametrize("case", ["new_day", "unverified_day", "other_account"])
def test_no_background_readback_outside_the_same_day_relogin(case, fast_monitor):
    provider, trader, sent = _scripted(verified=case != "unverified_day")
    if case == "new_day":
        trader.relogin(trading_day="20261109")
    elif case == "other_account":
        trader.relogin(account="other-account")
    else:
        assert _monitor_threads() == []  # nothing verified: no monitor at all
        trader.relogin()
    _settle()

    # Unchanged: left to the explicit SDK settlement step (R-3).
    assert trader.verify_timeouts == []
    with pytest.raises(DirectWriteError) as info:
        _place(provider)
    assert info.value.code == "ctp_execution_gate_session_not_trading_ready"
    assert trader.verify_timeouts == []
    assert trader.confirm_calls == 0


def test_one_monitor_thread_across_reconnects_and_none_after_close(fast_monitor):
    provider, trader, sent = _scripted()
    assert len(_monitor_threads()) == 1
    for _ in range(5):
        trader.relogin()
        _wait_for(lambda: trader.is_trading_ready)
    assert len(_monitor_threads()) == 1
    assert trader.verify_threads == [MONITOR] * 5
    assert trader.confirm_calls == 0

    provider.close()
    assert _monitor_threads() == []
    # A closed provider does not start it again.
    assert provider.settlement_status() is SettlementStatus.CONFIRMED
    assert _monitor_threads() == []


def test_monitor_ends_when_the_feed_releases_its_session(fast_monitor):
    provider, trader, sent = _scripted()
    assert len(_monitor_threads()) == 1
    provider._feed._trader = None  # feed disconnect
    _wait_for(lambda: _monitor_threads() == [])

    provider._feed._trader = trader
    assert provider.settlement_status() is SettlementStatus.CONFIRMED
    assert len(_monitor_threads()) == 1  # restarted by the next ready observation


def test_monitor_ends_when_the_provider_is_collected(fast_monitor):
    provider, trader, sent = _scripted()
    assert len(_monitor_threads()) == 1
    # The return tap is the session's only strong path back to the provider
    # (the feed's own loop thread keeps the feed and trader alive); the
    # monitor thread itself must not keep the provider alive.
    trader.listeners.clear()
    del provider, trader, sent
    gc.collect()
    _wait_for(lambda: _monitor_threads() == [])


def test_trading_ready_write_skips_the_session_state_read():
    provider, trader, sent = _scripted()
    reservation = provider.reserve_order(Req())
    trader.state_reads = 0
    result = provider.submit_order(
        reservation, Req(), _grant(reservation.identity.session_generation), Verifier()
    )
    assert result.sent is True
    assert trader.state_reads == 1  # the reservation's session check only

    trader.state_reads = 0
    assert _cancel(provider, trader).sent is True
    assert trader.state_reads == 1

    trader.state_reads = 0
    assert provider._settlement_readback_for_write(trader) is False
    assert trader.state_reads == 0
    assert trader.verify_timeouts == []
