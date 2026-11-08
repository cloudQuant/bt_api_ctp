"""Owner-thread nonblocking CTP instrument queries using synthetic APIs only."""
import threading
import time
from types import SimpleNamespace

import pytest
from test_iter62_query_slot import _ready_client

from bt_api_ctp.ctp.client import CtpInstrumentQueryHandle


def begin(client, **kwargs):
    return client.begin_instruments_query(instrument_id="rb2701", exchange_id="SHFE", **kwargs)


def row(symbol="rb2701", exchange="SHFE"):
    return SimpleNamespace(InstrumentID=symbol, ExchangeID=exchange, PriceTick=1)


def test_synchronous_callback_exact_query_and_idempotent_result():
    client, api = _ready_client()
    api.rows["ReqQryInstrument"] = [row()]
    token = begin(client)
    assert api.calls == []
    result = client.poll_instruments_query(token)
    assert result.complete and result.records[0]["InstrumentID"] == "rb2701"
    assert dict(result.query_source.request_intent_filters) == {"InstrumentID": "rb2701", "ExchangeID": "SHFE"}
    assert result.query_source.request_filters == result.query_source.request_intent_filters
    assert result.query_source.explicit_request_filters == ("ExchangeID", "InstrumentID")
    assert result.record_limit == 2 and result.query_source.records_sha256 is not None
    assert client.poll_instruments_query(token) is result
    assert len(api.calls) == 1
    assert client.release_instruments_query(token)
    with pytest.raises(ValueError):
        client.poll_instruments_query(token)


def test_rate_and_inflight_are_pending_without_waiting_or_duplicate_submission():
    client, api = _ready_client()
    api.silent.add("ReqQryInstrument")
    client._query_interval = 100
    client._last_query_submitted_at = time.monotonic()
    first = begin(client)
    start = time.monotonic()
    assert client.poll_instruments_query(first) is None
    assert time.monotonic() - start < 0.1 and api.calls == []
    client._last_query_submitted_at = 0
    assert client.poll_instruments_query(first) is None
    assert len(api.calls) == 1
    second = begin(client)
    client._query_interval = 0
    start = time.monotonic()
    for _ in range(4):
        assert client.poll_instruments_query(second) is None
    assert time.monotonic() - start < 0.1 and len(api.calls) == 1
    api.rows["ReqQryInstrument"] = [row()]
    api.respond("ReqQryInstrument", api.outstanding)
    assert client.poll_instruments_query(first).complete
    assert client.poll_instruments_query(second) is None
    assert len(api.calls) == 2


def test_timeout_retains_slot_until_late_terminal_and_retirement():
    client, api = _ready_client()
    api.silent.add("ReqQryInstrument")
    token = begin(client, timeout=0.02)
    assert client.poll_instruments_query(token) is None
    request_id = api.outstanding
    time.sleep(0.03)
    result = client.poll_instruments_query(token)
    assert result.timed_out and not result.complete
    assert client.release_instruments_query(token)
    other = begin(client)
    assert client.poll_instruments_query(other) is None
    api.rows["ReqQryInstrument"] = [row()]
    api.respond("ReqQryInstrument", request_id)
    with pytest.raises(ValueError):
        client.poll_instruments_query(token)
    assert client.poll_instruments_query(other) is None
    assert len(api.calls) == 2


def test_cancel_before_and_after_native_issue():
    client, api = _ready_client()
    before = begin(client)
    assert client.cancel_instruments_query(before)
    assert client.poll_instruments_query(before).completion_origin == "local_cancel"
    assert api.calls == [] and client.release_instruments_query(before)
    api.silent.add("ReqQryInstrument")
    after = begin(client)
    assert client.poll_instruments_query(after) is None
    request_id = api.outstanding
    assert client.cancel_instruments_query(after)
    result = client.poll_instruments_query(after)
    assert not result.complete and result.completion_origin == "local_cancel"
    assert client.release_instruments_query(after)
    assert client._query_native_inflight is not None
    api.respond("ReqQryInstrument", request_id)
    assert client._query_native_inflight is None


def test_submit_rejection_and_scope_mismatch_fail_closed():
    client, api = _ready_client()
    api.reject["ReqQryInstrument"] = -3
    rejected = client.poll_instruments_query(begin(client))
    assert not rejected.complete and rejected.completion_origin == "submit_rejected"
    assert client._query_native_inflight is None
    del api.reject["ReqQryInstrument"]
    api.rows["ReqQryInstrument"] = [row(exchange="DCE")]
    wrong = client.poll_instruments_query(begin(client))
    assert not wrong.complete and wrong.error_message == "instrument_query_scope_mismatch"


def test_record_limit_preserves_slot_until_own_late_terminal():
    client, api = _ready_client()
    api.silent.add("ReqQryInstrument")
    token = begin(client, max_records=1)
    assert client.poll_instruments_query(token) is None
    request_id = api.outstanding
    api.spi.OnRspQryInstrument(row(), None, request_id, False)
    api.spi.OnRspQryInstrument(row(), None, request_id, False)
    result = client.poll_instruments_query(token)
    assert result.record_limit_exceeded and not result.complete
    assert client.release_instruments_query(token)
    assert client._query_native_inflight is not None
    api.respond("ReqQryInstrument", request_id)
    assert client._query_native_inflight is None


@pytest.mark.parametrize("submit_raises", [False, True])
def test_synchronous_overflow_then_deadline_or_submit_exception(monkeypatch, submit_raises):
    from bt_api_ctp.ctp import client as module

    client, api = _ready_client()
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def overflow(_field, request_id):
        api.spi.OnRspQryInstrument(row(), None, request_id, False)
        api.spi.OnRspQryInstrument(row(), None, request_id, False)
        clock[0] = 102.0
        if submit_raises:
            raise RuntimeError("synthetic native exception")
        return 0

    monkeypatch.setattr(api, "ReqQryInstrument", overflow)
    token = begin(client, timeout=1.0, max_records=1)
    result = client.poll_instruments_query(token)
    assert result.record_limit_exceeded and not result.complete
    assert result.completion_origin == "record_limit"
    assert result.timed_out is (not submit_raises)
    assert client.release_instruments_query(token)
    assert client._query_native_inflight is not None


def test_wrong_owner_forged_handles_and_bounded_registry():
    client, _ = _ready_client()
    token = begin(client)
    errors = []
    def cross_thread():
        try:
            client.poll_instruments_query(token)
        except Exception as exc:
            errors.append(type(exc))
    thread = threading.Thread(target=cross_thread)
    thread.start()
    thread.join()
    assert errors == [RuntimeError]
    with pytest.raises(ValueError):
        client.poll_instruments_query(CtpInstrumentQueryHandle())
    handles = [begin(client) for _ in range(63)]
    with pytest.raises(RuntimeError, match="capacity"):
        begin(client)
    assert client.cancel_instruments_query(token) and client.release_instruments_query(token)
    assert begin(client) is not None
    assert len(handles) == 63


def test_session_change_never_reissues_on_new_session():
    client, api = _ready_client()
    first = begin(client)
    client._connection_generation += 1
    outcome = client.poll_instruments_query(first)
    assert not outcome.complete and api.calls == []


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan"), "1"])
def test_timeout_validation(timeout):
    client, _ = _ready_client()
    with pytest.raises(ValueError):
        begin(client, timeout=timeout)


def test_deadline_is_rechecked_at_native_gate_after_record_request(monkeypatch):
    from bt_api_ctp.ctp import client as module
    client, api = _ready_client()
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    token = begin(client, timeout=1.0)
    original = client._record_request
    def advance_clock(kind):
        original(kind)
        clock[0] = 102.0
    monkeypatch.setattr(client, "_record_request", advance_clock)
    result = client.poll_instruments_query(token)
    assert result.timed_out and result.completion_origin == "deadline"
    assert api.calls == [] and client._query_native_inflight is None
    assert client.release_instruments_query(token)


@pytest.mark.parametrize("changed", ["generation", "login", "api", "spi"])
def test_session_fence_after_slot_reservation_before_native(monkeypatch, changed):
    client, api = _ready_client()
    token = begin(client)
    original = client._record_request

    def change_session(kind):
        original(kind)
        assert client._query_native_inflight is not None
        if changed == "generation":
            client._connection_generation += 1
        elif changed == "login":
            client._login_identity_observation = None
        elif changed == "api":
            client._api = object()
        else:
            client._spi = object()

    monkeypatch.setattr(client, "_record_request", change_session)
    result = client.poll_instruments_query(token)
    assert result is not None and not result.complete
    assert api.calls == [] and client._query_native_inflight is None
    assert client.release_instruments_query(token)


def test_pre_dispatch_record_exception_releases_only_unissued_slot(monkeypatch):
    client, api = _ready_client()
    token = begin(client)
    def fail(_kind):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(client, "_record_request", fail)
    result = client.poll_instruments_query(token)
    assert not result.complete and result.completion_origin == "local_preflight"
    assert client._query_native_inflight is None and api.calls == []
    assert client.release_instruments_query(token)


def test_rate_lock_and_state_lock_contention_do_not_wait():
    client, api = _ready_client()
    token = begin(client)
    client._query_lock.acquire()
    try:
        started = time.monotonic()
        assert client.poll_instruments_query(token) is None
        assert time.monotonic() - started < 0.1
    finally:
        client._query_lock.release()
    held, finish = threading.Event(), threading.Event()
    def hold_state():
        with client._query_state_lock:
            held.set()
            finish.wait(2)
    thread = threading.Thread(target=hold_state)
    thread.start()
    assert held.wait(1)
    try:
        started = time.monotonic()
        assert client.poll_instruments_query(token) is None
        assert time.monotonic() - started < 0.1
        assert client.cancel_instruments_query(token) is False
    finally:
        finish.set()
        thread.join(timeout=1)
    assert api.calls == []


def test_request_filters_are_exact_native_readback():
    client, api = _ready_client()
    captured = []
    def submit(field, request_id):
        captured.append((field.InstrumentID, field.ExchangeID))
        api.spi.OnRspQryInstrument(row(), None, request_id, True)
        return 0
    api.ReqQryInstrument = submit
    result = client.poll_instruments_query(begin(client))
    assert result.complete and captured == [("rb2701", "SHFE")]
    assert dict(result.query_source.request_filters) == dict(result.query_source.request_intent_filters)


def test_native_exception_is_uncertain_and_retains_slot_even_if_error_mimics_guard():
    from bt_api_ctp.ctp.client import CtpExecutionGateError
    client, api = _ready_client()
    def submit(_field, _request_id):
        raise CtpExecutionGateError("ctp_async_query_session_changed")
    api.ReqQryInstrument = submit
    token = begin(client)
    result = client.poll_instruments_query(token)
    assert not result.complete and result.completion_origin == "submit_exception"
    assert client._query_native_inflight is not None
    assert client.release_instruments_query(token)
    assert client._query_native_inflight is not None


def test_session_change_after_native_issue_never_resubmits():
    client, api = _ready_client()
    api.silent.add("ReqQryInstrument")
    token = begin(client)
    assert client.poll_instruments_query(token) is None
    client._connection_generation += 1
    result = client.poll_instruments_query(token)
    assert not result.complete and len(api.calls) == 1
    assert client.poll_instruments_query(token) is result


def test_history_capacity_rejects_without_native_or_silent_eviction():
    from bt_api_ctp.ctp.client import _QueryAccumulator
    from datetime import datetime, timezone
    client, api = _ready_client()
    with client._query_state_lock:
        for request_id in range(1000, 1256):
            client._query_history[request_id] = _QueryAccumulator(
                "instruments", request_id, 1, "synthetic", datetime.now(timezone.utc))
    token = begin(client)
    result = client.poll_instruments_query(token)
    assert not result.complete and result.error_message == "query_history_capacity"
    assert len(client._query_history) == 256 and api.calls == []
    assert client.release_instruments_query(token)


def test_local_cancel_does_not_expand_shared_history():
    client, _ = _ready_client()
    before = len(client._query_history)
    for _ in range(300):
        token = begin(client)
        assert client.cancel_instruments_query(token)
        assert client.release_instruments_query(token)
    assert len(client._query_history) == before


def test_oversize_timeout_is_finitely_rejected():
    client, _ = _ready_client()
    with pytest.raises(ValueError, match="timeout_invalid"):
        begin(client, timeout=10 ** 1000)
