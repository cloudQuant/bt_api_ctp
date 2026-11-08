"""Prepared CTP cancel with synthetic native leaves; never connects a venue."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from bt_api_base.direct_cancel_protocol import PreparedCancelRequest
from bt_api_base.direct_write_protocol import DirectWriteError, NativeOrderIdentity

from bt_api_ctp.ctp import client as ctp_client
from bt_api_ctp.ctp import ctp_structs_order
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture


class Native:
    def __init__(self):
        self.calls = []
        self.error = None
        self.result = 0

    def ReqOrderAction(self, field, request_id):
        self.calls.append((field, request_id))
        if self.error:
            raise self.error
        return self.result


class Verifier:
    def __init__(self):
        self.calls = 0
        self.revoked = False

    def consume(self, grant, **kwargs):
        self.calls += 1
        if self.revoked:
            raise ValueError("revoked")


def make_system(monkeypatch):
    td = "tcp://182.254.243.31:30002"
    md = "tcp://182.254.243.31:30012"
    broker, user = "3070", "offline-account"
    feed = CtpRequestDataFuture(
        broker_id=broker,
        user_id=user,
        password="unused",
        td_front=td,
        md_front=md,
        auto_settlement_confirm=False,
    )
    native = Native()
    trader = ctp_client.TraderClient(td, broker, user, "unused")
    trader._api = native
    trader._session_native_api = native
    trader._session_native_front = td
    trader._connected = True
    trader._authentication_state = "authenticated"
    trader._login_state = "logging_in"
    trader._connection_generation = 7
    trader._login_request_id = 1
    trader._login_connection_generation = 7
    spi = ctp_client._TraderSpi(trader, native_api=native)
    trader._spi = spi
    spi.OnRspUserLogin(
        SimpleNamespace(
            BrokerID=broker,
            UserID=user,
            FrontID=1,
            SessionID=2,
            TradingDay="20261009",
            MaxOrderRef="7",
        ),
        SimpleNamespace(ErrorID=0),
        1,
        True,
    )
    trader._settlement_state = "confirmed"
    trader._settlement_readback_verified = True
    trader._settlement_account_fingerprint = trader._account_fingerprint
    trader._settlement_trading_day = trader._trading_day
    trader._settlement_connection_generation = 7
    trader._ready = True
    trader._has_current_settlement_readback_locked = lambda: True
    capability, _ = trader.arm_execution_for_registered_sim(
        instrument_id="SA2701",
        exchange_id="CZCE",
        md_front=md,
        strategy_identity_sha256="b" * 64,
        execution_cycle_id="test-cycle",
        preflight_sha256="c" * 64,
    )
    feed._trader = trader
    provider = CtpDirectWriteProvider(
        feed, background_settlement_readback=False, allow_implicit_connect=False
    )
    monkeypatch.setattr(provider, "_admitted_capability", lambda _: capability)
    monkeypatch.setattr(provider, "_ensure_armed", lambda *_: None)
    monkeypatch.setattr(ctp_structs_order, "CThostFtdcInputOrderActionField", SimpleNamespace)
    return provider, trader, native


def target(*, venue=False, trading_day="20261009", both=False):
    fields = {"trading_day": trading_day, "exchange_id": "CZCE"}
    if venue:
        fields.update(order_sys_id="123", order_sys_id_raw="  123")
        return NativeOrderIdentity("CTP___FUTURE", None, fields, 6)
    fields.update(order_ref="000000000101", front_id=3, session_id=9)
    if both:
        fields.update(order_sys_id="123", order_sys_id_raw="  123")
    return NativeOrderIdentity("CTP___FUTURE", "000000000101", fields, 6)


@pytest.fixture
def system(monkeypatch):
    return make_system(monkeypatch)


@pytest.mark.parametrize("venue", [False, True])
def test_prepare_and_release_never_send_and_preserve_target(system, venue):
    provider, trader, native = system
    prepared = provider.prepare_cancel_order(target(venue=venue), "SA2701")
    assert prepared.trading_day == "20261009"
    assert prepared.target_identity.fields["trading_day"] == "20261009"
    assert prepared.request_fields["RequestID"] == prepared.request_fields["OrderActionRef"]
    assert (prepared.request_fields["OrderSysID"] == "  123") is venue
    assert native.calls == []
    provider.release_cancel_reservation(prepared)
    assert native.calls == []
    with pytest.raises(DirectWriteError):
        provider.release_cancel_reservation(prepared)


@pytest.mark.parametrize("venue,both", [(False, False), (False, True), (True, False)])
def test_cross_day_target_is_unproven_without_allocating_or_sending(system, venue, both):
    provider, trader, native = system
    before = trader._req_id
    with pytest.raises(DirectWriteError, match="ctp_prepared_cancel_cross_day_unproven"):
        provider.prepare_cancel_order(
            target(venue=venue, both=both, trading_day="20261008"), "SA2701"
        )
    assert trader._req_id == before
    assert not native.calls


def test_no_trader_does_not_implicitly_connect(system, monkeypatch):
    provider, _, native = system
    provider._allow_implicit_connect = True
    provider._feed._trader = None
    monkeypatch.setattr(
        provider._feed, "ensure_read_only_connected", lambda: pytest.fail("connect")
    )
    with pytest.raises(DirectWriteError):
        provider.prepare_cancel_order(target(), "SA2701")
    assert not native.calls


@pytest.mark.parametrize("venue", [False, True])
def test_submit_single_use_keeps_exact_action_and_grant_at_final_gate(system, venue):
    provider, _, native = system
    prepared = provider.prepare_cancel_order(target(venue=venue), "SA2701")
    verifier = Verifier()
    result = provider.submit_prepared_cancel(prepared, object(), verifier)
    assert result.sent is True and result.identity == prepared.target_identity
    assert len(native.calls) == 1 and verifier.calls == 1
    field, req = native.calls[0]
    assert req == prepared.request_fields["RequestID"] == field.OrderActionRef
    assert field.OrderSysID == ("  123" if venue else "")
    with pytest.raises(DirectWriteError):
        provider.submit_prepared_cancel(prepared, object(), verifier)
    assert len(native.calls) == 1


def test_forged_and_tampered_preparation_rejected(system):
    provider, _, native = system
    prepared = provider.prepare_cancel_order(target(), "SA2701")
    copied = PreparedCancelRequest.from_mapping(prepared.to_mapping())
    with pytest.raises(DirectWriteError):
        provider.submit_prepared_cancel(copied, object(), Verifier())
    object.__setattr__(prepared, "trading_day", "20261010")
    with pytest.raises(DirectWriteError):
        provider.submit_prepared_cancel(prepared, object(), Verifier())
    assert native.calls == []


def test_two_attempts_same_target_have_distinct_keys(system):
    provider, _, _ = system
    one = provider.prepare_cancel_order(target(), "SA2701")
    two = provider.prepare_cancel_order(target(), "SA2701")
    assert one.request_keys != two.request_keys


@pytest.mark.parametrize("change", ["generation", "api", "spi", "front", "session", "account"])
def test_session_change_before_dispatch_is_not_sent(system, change):
    provider, trader, native = system
    prepared = provider.prepare_cancel_order(target(), "SA2701")
    if change == "generation":
        trader._connection_generation += 1
    if change == "api":
        trader._api = Native()
    if change == "spi":
        trader._spi = object()
    if change == "front":
        trader._front_id += 1
    if change == "session":
        trader._session_id += 1
    if change == "account":
        trader._bound_user_id = "other"
    result = provider.submit_prepared_cancel(prepared, object(), Verifier())
    assert result.sent is False and not native.calls


def test_revocation_at_final_gate_and_native_exception(system):
    provider, _, native = system
    prepared = provider.prepare_cancel_order(target(), "SA2701")
    verifier = Verifier()
    verifier.revoked = True
    assert provider.submit_prepared_cancel(prepared, object(), verifier).sent is False
    assert not native.calls
    prepared = provider.prepare_cancel_order(target(), "SA2701")
    native.error = RuntimeError("ambiguous native failure")
    assert provider.submit_prepared_cancel(prepared, object(), Verifier()).sent is None
    assert len(native.calls) == 1


def test_nonzero_and_noninteger_native_results(system):
    provider, _, native = system
    native.result = -1
    assert (
        provider.submit_prepared_cancel(
            provider.prepare_cancel_order(target(), "SA2701"), object(), Verifier()
        ).sent
        is False
    )
    native.result = None
    assert (
        provider.submit_prepared_cancel(
            provider.prepare_cancel_order(target(), "SA2701"), object(), Verifier()
        ).sent
        is None
    )


def test_wrong_thread_and_process_rejected(system):
    provider, _, native = system
    prepared = provider.prepare_cancel_order(target(), "SA2701")
    codes = []

    def wrong_thread():
        try:
            provider.submit_prepared_cancel(prepared, object(), Verifier())
        except DirectWriteError as error:
            codes.append(error.code)

    worker = threading.Thread(target=wrong_thread)
    worker.start()
    worker.join()
    assert codes == ["ctp_direct_wrong_owner"]
    provider._settlement_owner_pid = -1
    with pytest.raises(DirectWriteError):
        provider.release_cancel_reservation(prepared)
    assert not native.calls


class ObservedLock:
    def __init__(self):
        self.lock = threading.RLock()
        self.contended = threading.Event()

    def acquire(self, *args, **kwargs):
        if self.lock.acquire(False):
            return True
        self.contended.set()
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        self.lock.release()

    def _is_owned(self):
        return self.lock._is_owned()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


@pytest.mark.parametrize("change", ["api", "spi", "front", "session", "field", "grant"])
def test_final_gate_after_contended_native_lock_rejects_changed_state(system, change):
    provider, trader, native = system
    lock = ObservedLock()
    trader._query_state_lock = lock
    trader._native_callback_event_condition = threading.Condition(lock)
    prepared = provider.prepare_cancel_order(target(), "SA2701")
    verifier = Verifier()
    ready, held = threading.Event(), threading.Event()
    orig = trader._invoke_session_native_request
    captured = []
    worker_errors = []

    def delayed(api, method, *args, **kwargs):
        captured.append(args[0])
        ready.set()
        assert held.wait(2)
        return orig(api, method, *args, **kwargs)

    trader._invoke_session_native_request = delayed

    def alter_while_holding_lock():
        try:
            assert ready.wait(2)
            with lock:
                held.set()
                assert lock.contended.wait(2)
                if change == "api":
                    trader._api = Native()
                if change == "spi":
                    trader._spi = object()
                if change == "front":
                    trader._front_id += 1
                if change == "session":
                    trader._session_id += 1
                if change == "field":
                    captured[0].OrderRef = "999"
                if change == "grant":
                    verifier.revoked = True
        except BaseException as error:
            worker_errors.append(error)

    worker = threading.Thread(target=alter_while_holding_lock)
    worker.start()
    result = provider.submit_prepared_cancel(prepared, object(), verifier)
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert not worker_errors
    assert result.sent is False and not native.calls


def test_native_call_runs_without_query_state_lock(system):
    provider, trader, native = system
    original = native.ReqOrderAction

    def check_lock(field, request_id):
        assert not trader._query_state_lock._is_owned()
        return original(field, request_id)

    native.ReqOrderAction = check_lock
    result = provider.submit_prepared_cancel(
        provider.prepare_cancel_order(target(), "SA2701"), object(), Verifier()
    )
    assert result.sent is True and len(native.calls) == 1


def test_coordinator_commit_precedes_real_prepared_dispatch(system):
    from bt_api_py.direct_write import DirectWriteCoordinator
    from bt_api_py.write_admission import WriteAdmission

    provider, _, native = system
    admission = WriteAdmission({"CTP___FUTURE": True})
    coordinator = DirectWriteCoordinator(admission, lambda _: provider)
    records = []

    class Port:
        def __init__(self, accepted):
            self.accepted = accepted

        def commit(self, record):
            assert not native.calls
            records.append(record)
            return "marker" if self.accepted else None

    rejected = coordinator.cancel_prepared(
        "CTP___FUTURE", target(), "SA2701", commit_port=Port(False), host_scope="strategy"
    )
    assert rejected.sent is False and not native.calls
    assert not provider._cancel_reservations
    sent = coordinator.cancel_prepared(
        "CTP___FUTURE", target(), "SA2701", commit_port=Port(True), host_scope="strategy"
    )
    assert sent.sent is True and sent.marker_id == "marker" and len(native.calls) == 1
    assert records[-1].prepared_cancel.request_fields["RequestID"] == native.calls[-1][1]
    assert not provider._cancel_reservations


def test_fake_native_waits_for_real_spi_callback_without_deadlock(system):
    provider, trader, native = system
    trader.add_execution_listener(provider._on_native_return)
    completed = threading.Event()
    errors = []

    def send_with_callback(field, request_id):
        native.calls.append((field, request_id))

        def invoke_callback():
            try:
                trader._spi.OnRspOrderAction(
                    field, SimpleNamespace(ErrorID=5, ErrorMsg="synthetic"), request_id, True
                )
            except BaseException as error:
                errors.append(error)
            finally:
                completed.set()

        callback = threading.Thread(target=invoke_callback)
        callback.start()
        if not completed.wait(2):
            raise RuntimeError("synthetic callback did not finish")
        callback.join()
        return 0

    native.ReqOrderAction = send_with_callback
    result = provider.submit_prepared_cancel(
        provider.prepare_cancel_order(target(), "SA2701"), object(), Verifier()
    )
    assert result.sent is True and completed.is_set() and not errors
    events = provider.drain_execution_events(2)
    assert len(events) == 1 and events[0].error_code == "5"
