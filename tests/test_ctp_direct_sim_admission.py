"""Offline contract tests for the direct frozen-simulation write admission.

``arm_execution_for_registered_sim`` is the only direct (non-managed) write
entry.  It must accept exactly the frozen simulation registries — registered
broker simulation fronts and the official SimNow whitelist — and must keep
every other front, including near-miss endpoints, fenced.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import bt_api_ctp.ctp.client as ctp_client_module
from bt_api_ctp.ctp.client import (
    CtpExecutionGateError,
    TraderClient,
    _issue_ctp_execution_authority_for_core,
    _TraderSpi,
)

_SIMNOW_TD = "tcp://182.254.243.31:30002"
_SIMNOW_MD = "tcp://182.254.243.31:30012"
_SIMNOW_TD_GROUP1 = "tcp://182.254.243.31:30001"
_SIMNOW_MD_GROUP1 = "tcp://182.254.243.31:30011"


class _CountingNativeApi:
    def __init__(self):
        self.calls = []
        self.authenticate_calls = []
        self.order_action_error = None

    def ReqAuthenticate(self, field, request_id):
        self.authenticate_calls.append((field, request_id))
        return 0

    def ReqOrderInsert(self, field, request_id):
        self.calls.append(("insert", field, request_id))
        return 0

    def ReqOrderAction(self, field, request_id):
        self.calls.append(("cancel", field, request_id))
        if self.order_action_error is not None:
            raise self.order_action_error
        return 0


class _BlockingInsertNativeApi(_CountingNativeApi):
    def __init__(self):
        super().__init__()
        self.insert_entered = threading.Event()
        self.allow_insert = threading.Event()

    def ReqOrderInsert(self, field, request_id):
        self.calls.append(("insert", field, request_id))
        self.insert_entered.set()
        if not self.allow_insert.wait(timeout=5):
            raise RuntimeError("test native insert release timed out")
        return 0


def _trading_ready_client(td_front, native=None):
    client = TraderClient(td_front, "3070", "offline-account", "unused")
    native = native or _CountingNativeApi()
    client._api = native
    client._session_native_api = native
    client._session_native_front = td_front
    client._connected = True
    client._authentication_state = "authenticated"
    client._login_state = "logging_in"
    client._connection_generation = 7
    client._login_request_id = 1
    client._login_connection_generation = 7
    _TraderSpi(client).OnRspUserLogin(
        SimpleNamespace(
            BrokerID="3070",
            UserID="offline-account",
            FrontID=1,
            SessionID=2,
            TradingDay="20261009",
            MaxOrderRef="7",
        ),
        SimpleNamespace(ErrorID=0),
        1,
        True,
    )
    client._settlement_state = "confirmed"
    client._settlement_readback_verified = True
    client._settlement_account_fingerprint = client._account_fingerprint
    client._settlement_trading_day = client._trading_day
    client._settlement_connection_generation = 7
    client._ready = True
    client._has_current_settlement_readback_locked = lambda: True
    return client, native


def _insert_field(**changes):
    values = {
        "BrokerID": "3070",
        "InvestorID": "offline-account",
        "UserID": "offline-account",
        "InstrumentID": "SA2701",
        "ExchangeID": "CZCE",
        "OrderRef": "000000000101",
        "RequestID": 11,
        "Direction": "0",
        "CombOffsetFlag": "0",
        "CombHedgeFlag": "1",
        "VolumeTotalOriginal": 1,
        "LimitPrice": 1000.0,
        "OrderPriceType": "2",
        "TimeCondition": "3",
        "VolumeCondition": "1",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _cancel_field(**changes):
    values = {
        "BrokerID": "3070",
        "InvestorID": "offline-account",
        "UserID": "offline-account",
        "InstrumentID": "SA2701",
        "ExchangeID": "CZCE",
        "OrderRef": "000000000101",
        "OrderActionRef": "000000000201",
        "FrontID": 1,
        "SessionID": 2,
        "RequestID": 12,
        "ActionFlag": "0",
    }
    values.update(changes)
    return SimpleNamespace(**values)


class _FakeMonotonicClock:
    def __init__(self, now: float):
        self.now = now

    def monotonic(self) -> float:
        return self.now


class _ObservedRLock:
    """Expose contention while preserving a real reentrant lock acquisition."""

    def __init__(self):
        self._lock = threading.RLock()
        self._owner_lock = threading.Lock()
        self._owner_thread_id = None
        self._owner_depth = 0
        self.contended = threading.Event()
        self.native_lock_waited = threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        thread_id = threading.get_ident()
        with self._owner_lock:
            blocked_by_other = (
                self._owner_thread_id is not None and self._owner_thread_id != thread_id
            )
        if blocked_by_other:
            self.contended.set()
            if blocking:
                acquired = self._lock.acquire(timeout=0.01)
                if not acquired:
                    self.native_lock_waited.set()
                    acquired = self._lock.acquire(blocking, timeout)
            else:
                acquired = self._lock.acquire(False)
        else:
            acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            with self._owner_lock:
                if self._owner_thread_id == thread_id:
                    self._owner_depth += 1
                else:
                    self._owner_thread_id = thread_id
                    self._owner_depth = 1
        return acquired

    def release(self):
        thread_id = threading.get_ident()
        with self._owner_lock:
            if self._owner_thread_id != thread_id:
                raise RuntimeError("SDK state lock released by a non-owner")
            self._owner_depth -= 1
            if self._owner_depth == 0:
                self._owner_thread_id = None
        self._lock.release()

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("controlled SDK lock wait timed out")
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.release()
        return False


def _arm(client, md_front, *, instrument="SA2701", exchange="CZCE"):
    return client.arm_execution_for_registered_sim(
        instrument_id=instrument,
        exchange_id=exchange,
        md_front=md_front,
        strategy_identity_sha256="b" * 64,
        execution_cycle_id="cert-cycle-1",
        preflight_sha256="c" * 64,
    )


def _install_same_api_reconnect_before_dispatch(client, native):
    spi = _TraderSpi(client, native_api=native)
    client._spi = spi
    invoke_native_request = client._invoke_session_native_request

    def reconnect_then_invoke(api, method_name, *args, **kwargs):
        if method_name in {"ReqOrderInsert", "ReqOrderAction"}:
            spi.OnFrontDisconnected(-1)
            spi.OnFrontConnected()
        return invoke_native_request(api, method_name, *args, **kwargs)

    client._invoke_session_native_request = reconnect_then_invoke
    return spi


def test_official_simnow_pair_arms_and_dispatches_one_insert():
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, state = _arm(client, _SIMNOW_MD)
    assert state["armed"] is True

    field = _insert_field()
    result = client.submit_order_insert(field, field.RequestID, execution_capability=capability)
    assert result == 0
    assert len(native.calls) == 1
    assert native.calls[0][0] == "insert"


def test_direct_insert_rejects_same_api_reconnect_before_native_dispatch():
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, _state = _arm(client, _SIMNOW_MD)
    requests_before = client.get_request_counts()["order_insert"]
    _install_same_api_reconnect_before_dispatch(client, native)

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_execution_gate_direct_write_session_mismatch",
    ):
        client.submit_order_insert(
            _insert_field(), 11, execution_capability=capability
        )

    assert client._api is native
    assert client._session_native_api is native
    assert client._connection_generation == 8
    assert client.is_trading_ready is False
    assert len(native.authenticate_calls) == 1
    assert native.calls == []
    assert client._order_insert_history == {}
    assert client._order_insert_identities == {}
    assert client._native_request_inflight_refs == 0
    assert client.get_request_counts()["order_insert"] == requests_before


def test_direct_cancel_rejects_same_api_reconnect_before_native_dispatch():
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, _state = _arm(client, _SIMNOW_MD)
    requests_before = client.get_request_counts()["order_action"]
    _install_same_api_reconnect_before_dispatch(client, native)

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_execution_gate_direct_write_session_mismatch",
    ):
        client.submit_order_action(
            _cancel_field(), 12, execution_capability=capability
        )

    assert client._api is native
    assert client._session_native_api is native
    assert client._connection_generation == 8
    assert client.is_trading_ready is False
    assert len(native.authenticate_calls) == 1
    assert native.calls == []
    assert client._order_action_history == {}
    assert client._order_action_identities == {}
    assert client._native_request_inflight_refs == 0
    assert client.get_request_counts()["order_action"] == requests_before


def test_direct_cancel_rejects_caller_user_id_change_before_native_dispatch():
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, _state = _arm(client, _SIMNOW_MD)
    requests_before = client.get_request_counts()["order_action"]
    field = _cancel_field()
    invoke_native_request = client._invoke_session_native_request

    def change_user_id_before_final_gate(api, method_name, *args, **kwargs):
        if method_name == "ReqOrderAction":
            args[0].UserID = "another-account"
        return invoke_native_request(api, method_name, *args, **kwargs)

    client._invoke_session_native_request = change_user_id_before_final_gate

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_execution_gate_direct_write_authorization_scope_mismatch",
    ):
        client.submit_order_action(
            field,
            12,
            execution_capability=capability,
        )

    assert field.UserID == "another-account"
    assert native.calls == []
    assert client._order_action_history == {}
    assert client._order_action_identities == {}
    assert client._native_request_inflight_refs == 0
    assert client.get_request_counts()["order_action"] == requests_before


def test_direct_cancel_deadline_and_same_api_reconnect_are_rechecked_after_real_lock_wait(
    monkeypatch,
):
    deadline = 11.0
    clock = _FakeMonotonicClock(10.0)
    monkeypatch.setattr(ctp_client_module.time, "monotonic", clock.monotonic)
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, _state = _arm(client, _SIMNOW_MD)
    requests_before = client.get_request_counts()["order_action"]
    lock = _ObservedRLock()
    client._query_state_lock = lock
    spi = _TraderSpi(client, native_api=native)
    client._spi = spi
    invoke_ready = threading.Event()
    lock_held = threading.Event()
    invoke_native_request = client._invoke_session_native_request

    def wait_for_competing_lock_holder(api, method_name, *args, **kwargs):
        if method_name == "ReqOrderAction":
            invoke_ready.set()
            if not lock_held.wait(timeout=5):
                raise RuntimeError("SDK lock holder did not start")
        return invoke_native_request(api, method_name, *args, **kwargs)

    client._invoke_session_native_request = wait_for_competing_lock_holder

    def reconnect_while_holding_sdk_lock():
        if not invoke_ready.wait(timeout=5):
            return
        with lock:
            lock_held.set()
            if not lock.native_lock_waited.wait(timeout=5):
                return
            spi.OnFrontDisconnected(-1)
            spi.OnFrontConnected()
            clock.now = deadline + 0.25

    waiter = threading.Thread(target=reconnect_while_holding_sdk_lock)
    waiter.start()
    try:
        with pytest.raises(
            CtpExecutionGateError,
            match="ctp_execution_gate_direct_write_session_mismatch",
        ):
            client.submit_order_action(
                _cancel_field(),
                12,
                execution_capability=capability,
                deadline_monotonic=deadline,
            )
    finally:
        waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert lock.contended.is_set()
    assert lock.native_lock_waited.is_set()
    assert client._api is native
    assert client._session_native_api is native
    assert client._connection_generation == 8
    assert client.is_trading_ready is False
    assert len(native.authenticate_calls) == 1
    assert native.calls == []
    assert client._order_action_history == {}
    assert client._order_action_identities == {}
    assert client._native_request_inflight_refs == 0
    assert client.get_request_counts()["order_action"] == requests_before


def test_concurrent_direct_insert_same_key_cleans_only_its_own_predispatch_attempt():
    client, native = _trading_ready_client(
        _SIMNOW_TD,
        native=_BlockingInsertNativeApi(),
    )
    capability, _state = _arm(client, _SIMNOW_MD)
    requests_before = client.get_request_counts()["order_insert"]
    invoke_barrier = threading.Barrier(2)
    invoke_native_request = client._invoke_session_native_request
    invoke_start_lock = threading.Lock()
    worker_finished = threading.Event()
    worker_finish_count = 0
    results = []
    errors = []

    def wait_for_both_initial_admissions(api, method_name, *args, **kwargs):
        if method_name == "ReqOrderInsert":
            invoke_barrier.wait(timeout=5)
        return invoke_native_request(api, method_name, *args, **kwargs)

    client._invoke_session_native_request = wait_for_both_initial_admissions

    def submit_same_key():
        nonlocal worker_finish_count
        try:
            results.append(
                client.submit_order_insert(
                    _insert_field(),
                    11,
                    execution_capability=capability,
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            with invoke_start_lock:
                worker_finish_count += 1
                if worker_finish_count == 1:
                    worker_finished.set()

    workers = [threading.Thread(target=submit_same_key) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        assert native.insert_entered.wait(timeout=5)
        assert worker_finished.wait(timeout=5)
        assert len(native.calls) == 1
        assert len(results) == 0
        assert len(errors) == 1
        assert isinstance(errors[0], CtpExecutionGateError)
        assert errors[0].code == "ctp_execution_gate_order_insert_request_id_reused"
        assert client.get_order_insert_evidence(
            11, order_ref="000000000101"
        ) is not None
        assert client.get_request_counts()["order_insert"] == requests_before + 1
    finally:
        native.allow_insert.set()
        for worker in workers:
            worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert len(native.calls) == 1
    assert results == [0]
    assert len(errors) == 1
    evidence = client.get_order_insert_evidence(11, order_ref="000000000101")
    assert evidence is not None
    assert evidence.reason == "awaiting_native_callback"
    assert client._order_insert_identities[(11, "000000000101")].order_ref == "000000000101"
    assert client._native_request_inflight_refs == 0
    assert client.get_request_counts()["order_insert"] == requests_before + 1


def test_official_simnow_pair_arms_without_md_front():
    client, _native = _trading_ready_client(_SIMNOW_TD_GROUP1)
    _capability, state = _arm(client, "")
    assert state["armed"] is True


def test_unregistered_front_cannot_arm():
    client, _native = _trading_ready_client("tcp://127.0.0.1:59999")
    with pytest.raises(CtpExecutionGateError, match="ctp_execution_gate_environment_unverified"):
        _arm(client, "")


def test_near_miss_official_port_cannot_arm():
    client, _native = _trading_ready_client("tcp://182.254.243.31:30004")
    with pytest.raises(CtpExecutionGateError, match="ctp_execution_gate_environment_unverified"):
        _arm(client, "")


def test_mismatched_md_front_cannot_arm():
    client, _native = _trading_ready_client(_SIMNOW_TD)
    with pytest.raises(CtpExecutionGateError, match="ctp_execution_gate_environment_unverified"):
        _arm(client, _SIMNOW_MD_GROUP1)


def test_official_simnow_stays_fenced_without_registered_admission():
    client, _native = _trading_ready_client(_SIMNOW_TD)
    capability = _issue_ctp_execution_authority_for_core()
    client.configure_execution_gate(capability)
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        client._issue_execution_authorization_for_core(
            capability,
            {},
            environment_profile="set1_group2_vpn",
            environment_verified=True,
            strategy_identity_sha256="b" * 64,
            execution_cycle_id="cert-cycle-1",
        )


def test_rebound_front_loses_admission_before_dispatch():
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, _state = _arm(client, _SIMNOW_MD)

    # Simulate an identity rebind: the stored admission must not survive a
    # front that no longer matches any frozen registry.
    client._bound_front = "tcp://182.254.243.31:49999"
    with pytest.raises(CtpExecutionGateError):
        client.submit_order_insert(_insert_field(), 11, execution_capability=capability)
    assert native.calls == []


def test_direct_cancel_deadline_allows_one_native_action_before_expiry(monkeypatch):
    clock = _FakeMonotonicClock(10.0)
    monkeypatch.setattr(ctp_client_module.time, "monotonic", clock.monotonic)
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, _state = _arm(client, _SIMNOW_MD)
    requests_before = client.get_request_counts()["order_action"]

    result = client.submit_order_action(
        _cancel_field(),
        12,
        execution_capability=capability,
        deadline_monotonic=11.0,
    )

    assert result == 0
    assert len(native.calls) == 1
    assert native.calls[0][0] == "cancel"
    assert client.get_request_counts()["order_action"] == requests_before + 1


@pytest.mark.parametrize("record_concurrent_request", [False, True])
def test_direct_cancel_deadline_expiring_during_sdk_lock_wait_blocks_native_action(
    monkeypatch, record_concurrent_request
):
    deadline = 11.0
    clock = _FakeMonotonicClock(10.0)
    monkeypatch.setattr(ctp_client_module.time, "monotonic", clock.monotonic)
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, _state = _arm(client, _SIMNOW_MD)
    requests_before = client.get_request_counts()["order_action"]
    lock = _ObservedRLock()
    client._query_state_lock = lock

    invoke_ready = threading.Event()
    lock_held = threading.Event()
    invoke_native_request = client._invoke_session_native_request

    def wait_for_competing_lock_holder(api, method_name, *args, **kwargs):
        invoke_ready.set()
        if not lock_held.wait(timeout=5):
            raise RuntimeError("SDK lock holder did not start")
        return invoke_native_request(api, method_name, *args, **kwargs)

    client._invoke_session_native_request = wait_for_competing_lock_holder

    def hold_sdk_lock_until_native_request_contends():
        if not invoke_ready.wait(timeout=5):
            return
        with lock:
            lock_held.set()
            if record_concurrent_request:
                client._record_request("order_action")
            if lock.native_lock_waited.wait(timeout=5):
                clock.now = deadline + 0.25

    waiter = threading.Thread(target=hold_sdk_lock_until_native_request_contends)
    waiter.start()
    try:
        with pytest.raises(
            CtpExecutionGateError,
            match="ctp_execution_gate_cancel_deadline_expired",
        ):
            client.submit_order_action(
                _cancel_field(),
                12,
                execution_capability=capability,
                deadline_monotonic=deadline,
            )
    finally:
        waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert lock.contended.is_set()
    assert lock.native_lock_waited.is_set()
    assert native.calls == []
    assert client._order_action_history == {}
    assert client._order_action_identities == {}
    assert client._native_request_inflight_refs == 0
    assert (
        client.get_request_counts()["order_action"]
        == requests_before + int(record_concurrent_request)
    )


def test_native_leaf_same_deadline_exception_keeps_ambiguous_cancel_evidence(monkeypatch):
    clock = _FakeMonotonicClock(10.0)
    monkeypatch.setattr(ctp_client_module.time, "monotonic", clock.monotonic)
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, _state = _arm(client, _SIMNOW_MD)
    requests_before = client.get_request_counts()["order_action"]
    native.order_action_error = ctp_client_module._CtpNativeRequestDeadlineExpired(
        "ctp_execution_gate_cancel_deadline_expired"
    )

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_execution_gate_cancel_deadline_expired",
    ):
        client.submit_order_action(
            _cancel_field(),
            12,
            execution_capability=capability,
            deadline_monotonic=11.0,
        )

    assert len(native.calls) == 1
    evidence = client.get_order_action_evidence(12, order_action_ref="000000000201")
    assert evidence is not None
    assert evidence.status == "unknown"
    assert evidence.reason == "native_submit_exception"
    assert client._native_request_inflight_refs == 0
    assert client.get_request_counts()["order_action"] == requests_before + 1


def test_direct_cancel_without_deadline_keeps_legacy_dispatch_path():
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, _state = _arm(client, _SIMNOW_MD)

    result = client.submit_order_action(
        _cancel_field(),
        12,
        execution_capability=capability,
    )

    assert result == 0
    assert len(native.calls) == 1
    assert native.calls[0][0] == "cancel"


@pytest.mark.parametrize(
    "deadline",
    [True, 1, float("nan"), float("inf"), -1.0, lambda: 12.0],
)
def test_direct_cancel_rejects_invalid_deadline_before_native_action(deadline):
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, _state = _arm(client, _SIMNOW_MD)
    requests_before = client.get_request_counts()["order_action"]

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_execution_gate_cancel_deadline_invalid",
    ):
        client.submit_order_action(
            _cancel_field(),
            12,
            execution_capability=capability,
            deadline_monotonic=deadline,
        )

    assert native.calls == []
    assert client.get_request_counts()["order_action"] == requests_before
