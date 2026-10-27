"""Offline contracts for typed query terminal origins and caller deadlines."""

import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import TraderClient, _TraderSpi
from bt_api_ctp.query import QUERY_COMPLETION_ORIGINS, QueryResult


class _FakeApi:
    def __init__(self):
        self.handlers = {}
        self.registered_spi = None
        self.release_calls = 0

    def ReqQryInstrument(self, field, request_id):
        handler = self.handlers.get("ReqQryInstrument")
        return 0 if handler is None else handler(field, request_id)

    def ReqQryDepthMarketData(self, field, request_id):
        handler = self.handlers.get("ReqQryDepthMarketData")
        return 0 if handler is None else handler(field, request_id)

    def RegisterSpi(self, spi):
        self.registered_spi = spi

    def Release(self):
        self.release_calls += 1


def _ready_client():
    client = TraderClient(
        "tcp://offline",
        "9999",
        "offline",
        "fixture",
        auto_settlement_confirm=False,
    )
    with client._query_state_lock:
        client._connected = True
        client._authentication_state = "authenticated"
        client._login_state = "logging_in"
        client._trading_day = ""
        client._connection_generation = 1
        login_request_id = client._next_request_id()
        client._login_request_id = login_request_id
        client._login_connection_generation = client._connection_generation
        login_spi = _TraderSpi(client, None)
        client._spi = login_spi
    client._session_native_api = None
    client._session_native_front = client._bound_front
    client._query_interval = 0
    login_spi.OnRspUserLogin(
        SimpleNamespace(
            BrokerID=client._bound_broker_id,
            UserID=client._bound_user_id,
            TradingDay="20260909",
            FrontID=7,
            SessionID=19,
            MaxOrderRef="0",
        ),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        login_request_id,
        True,
    )
    assert client.is_read_only_ready is True

    api = _FakeApi()
    spi = _TraderSpi(client, api)
    with client._query_state_lock:
        client._api = api
        client._session_native_api = api
        client._spi = spi
    return client, api, spi


def _call_query(client, query_kind, **kwargs):
    if query_kind == "instruments":
        return client.query_instruments_result(**kwargs)
    return client.query_depth_market_data_result(**kwargs)


def _response_error(code=7, message="provider failure"):
    return SimpleNamespace(ErrorID=code, ErrorMsg=message)


def _wait_for_native_request(client, entered, worker):
    assert entered.wait(1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with client._query_state_lock:
            if client._native_request_inflight_refs == 0:
                return
        time.sleep(0.001)
    worker.join(timeout=0.1)
    pytest.fail("native query call did not leave its SDK lease")


def test_query_result_origins_are_additive_and_whitelisted():
    assert QUERY_COMPLETION_ORIGINS == frozenset(
        {
            "unknown",
            "query_response",
            "response_error",
            "front_disconnected",
            "client_stop",
            "local_cancel",
            "submit_exception",
            "submit_rejected",
            "timeout",
            "deadline",
            "success",
            "local_preflight",
        }
    )
    result = QueryResult(
        request_type="instruments",
        request_id=1,
        connection_generation=1,
        account_fingerprint="offline",
        started_at_utc=datetime.now(timezone.utc),
        completed_at_utc=None,
        is_last_seen=False,
        error_code=None,
        error_message="",
        timed_out=False,
        complete=False,
    )
    assert result.first_completion_origin == "unknown"
    assert result.completion_origin == "unknown"
    assert result.as_dict()["first_completion_origin"] == "unknown"
    assert result.as_dict()["completion_origin"] == "unknown"


@pytest.mark.parametrize("field", ["first_completion_origin", "completion_origin"])
@pytest.mark.parametrize("value", [None, True, "untrusted", ""])
def test_query_result_rejects_noncanonical_completion_origins(field, value):
    values = {
        "request_type": "instruments",
        "request_id": 1,
        "connection_generation": 1,
        "account_fingerprint": "offline",
        "started_at_utc": datetime.now(timezone.utc),
        "completed_at_utc": None,
        "is_last_seen": False,
        "error_code": None,
        "error_message": "",
        "timed_out": False,
        "complete": False,
        field: value,
    }
    with pytest.raises(ValueError, match=f"invalid {field}"):
        QueryResult(**values)


@pytest.mark.parametrize("query_kind", ["instruments", "depth"])
def test_typed_terminal_callback_records_success_origin(query_kind):
    client, api, spi = _ready_client()
    method_name = (
        "ReqQryInstrument" if query_kind == "instruments" else "ReqQryDepthMarketData"
    )
    callback_name = (
        "OnRspQryInstrument" if query_kind == "instruments" else "OnRspQryDepthMarketData"
    )

    def submit(_field, request_id):
        getattr(spi, callback_name)(
            SimpleNamespace(InstrumentID="rb2701", LastPrice=10.5),
            _response_error(0, ""),
            request_id,
            True,
        )
        return 0

    api.handlers[method_name] = submit
    result = _call_query(client, query_kind, timeout=1)

    assert result.complete is True
    assert result.first_completion_origin == "success"
    assert result.completion_origin == "success"
    assert result.as_dict()["completion_origin"] == "success"


@pytest.mark.parametrize(
    "callback_name,expected_origin",
    [
        ("OnRspQryInstrument", "query_response"),
        ("OnRspError", "response_error"),
    ],
)
def test_partial_rows_and_error_message_do_not_conflate_query_origins(
    callback_name, expected_origin
):
    client, api, spi = _ready_client()

    def submit(_field, request_id):
        spi.OnRspQryInstrument(
            SimpleNamespace(InstrumentID="rb2701", LastPrice=10.5),
            _response_error(0, ""),
            request_id,
            False,
        )
        if callback_name == "OnRspError":
            spi.OnRspError(_response_error(7, "query_cancelled"), request_id, False)
        else:
            spi.OnRspQryInstrument(
                None,
                _response_error(7, "query_cancelled"),
                request_id,
                False,
            )
        return 0

    api.handlers["ReqQryInstrument"] = submit
    result = client.query_instruments_result(timeout=1)

    assert result.complete is False
    assert result.timed_out is False
    assert result.error_message == "query_cancelled"
    assert len(result.records) == 1
    assert result.records[0]["InstrumentID"] == "rb2701"
    assert result.first_completion_origin == expected_origin
    assert result.completion_origin == expected_origin


def test_front_disconnect_after_terminal_response_keeps_first_and_latest_origins():
    client, api, spi = _ready_client()

    def submit(_field, request_id):
        spi.OnRspQryInstrument(
            SimpleNamespace(InstrumentID="rb2701"),
            _response_error(7, "typed response error"),
            request_id,
            True,
        )
        spi.OnFrontDisconnected(101)
        return 0

    api.handlers["ReqQryInstrument"] = submit
    result = client.query_instruments_result(timeout=1)

    assert result.complete is False
    assert result.first_completion_origin == "query_response"
    assert result.completion_origin == "front_disconnected"
    assert result.error_message == "connection_lost"
    assert len(result.records) == 1


@pytest.mark.parametrize("query_kind", ["instruments", "depth"])
def test_disconnect_origin_comes_from_current_sdk_spi(query_kind):
    client, api, _spi = _ready_client()
    method_name = (
        "ReqQryInstrument" if query_kind == "instruments" else "ReqQryDepthMarketData"
    )
    entered = threading.Event()
    result_holder = []

    def submit(_field, _request_id):
        entered.set()
        return 0

    api.handlers[method_name] = submit
    worker = threading.Thread(
        target=lambda: result_holder.append(_call_query(client, query_kind, timeout=5)),
        daemon=True,
    )
    worker.start()
    _wait_for_native_request(client, entered, worker)
    with client._query_state_lock:
        spi = client._spi
    spi.OnFrontDisconnected(101)
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert result_holder[0].error_message == "connection_lost"
    assert result_holder[0].first_completion_origin == "front_disconnected"
    assert result_holder[0].completion_origin == "front_disconnected"


def test_owner_stop_is_reported_separately_from_front_disconnect(monkeypatch):
    client, api, spi = _ready_client()
    entered = threading.Event()
    result_holder = []

    def submit(_field, request_id):
        spi.OnRspQryInstrument(
            SimpleNamespace(InstrumentID="rb2701"), None, request_id, False
        )
        entered.set()
        return 0

    api.handlers["ReqQryInstrument"] = submit
    monkeypatch.setattr(client, "_release_native_api_immediately", lambda _api, _spi: True)

    worker = threading.Thread(
        target=lambda: result_holder.append(client.query_instruments_result(timeout=5)),
        daemon=True,
    )
    worker.start()
    _wait_for_native_request(client, entered, worker)
    assert client._stop_native_session(expected_api=api) is True
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert result_holder[0].error_message == "connection_lost"
    assert result_holder[0].first_completion_origin == "client_stop"
    assert result_holder[0].completion_origin == "client_stop"
    assert len(result_holder[0].records) == 1


@pytest.mark.parametrize(
    "outcome,latest",
    [("exception", "submit_exception"), ("rejected", "submit_rejected")],
)
def test_submit_failure_after_terminal_callback_preserves_first_and_latest_origins(
    outcome, latest
):
    client, api, spi = _ready_client()

    def submit(_field, request_id):
        spi.OnRspQryInstrument(
            SimpleNamespace(InstrumentID="rb2701"), None, request_id, True
        )
        if outcome == "exception":
            raise RuntimeError("synthetic submit failure")
        return 17

    api.handlers["ReqQryInstrument"] = submit
    result = client.query_instruments_result(timeout=1)

    assert result.complete is False
    assert len(result.records) == 1
    assert result.first_completion_origin == "success"
    assert result.completion_origin == latest


def test_preflight_cancellation_and_silent_timeout_have_distinct_origins():
    client = TraderClient("tcp://offline", "9999", "offline", "fixture")
    not_ready = client.query_depth_market_data_result(timeout=0.1)
    assert not_ready.error_message == "trader_not_logged_in"
    assert not_ready.completion_origin == "local_preflight"

    client, api, _spi = _ready_client()
    api.handlers["ReqQryInstrument"] = lambda _field, _request_id: 0
    cancelled = client.query_instruments_result(
        timeout=1,
        should_stop=lambda: True,
    )
    assert cancelled.error_message == "query_cancelled"
    assert cancelled.completion_origin == "local_cancel"

    silent = client.query_instruments_result(timeout=0.01)
    assert silent.error_message == "query_timeout"
    assert silent.timed_out is True
    assert silent.completion_origin == "timeout"


def test_cancellation_during_response_wait_records_local_cancel_origin():
    client, api, spi = _ready_client()

    def submit(_field, request_id):
        spi.OnRspQryInstrument(
            SimpleNamespace(InstrumentID="rb2701"), None, request_id, False
        )
        return 0

    api.handlers["ReqQryInstrument"] = submit
    stop_event = threading.Event()
    timer = threading.Timer(0.02, stop_event.set)
    timer.daemon = True
    timer.start()
    try:
        result = client.query_instruments_result(
            timeout=5,
            max_duration=10,
            should_stop=stop_event.is_set,
        )
    finally:
        timer.join(timeout=1)

    assert stop_event.is_set()
    assert result.error_message == "query_cancelled"
    assert result.timed_out is False
    assert result.first_completion_origin == "local_cancel"
    assert result.completion_origin == "local_cancel"
    assert len(result.records) == 1


@pytest.mark.parametrize("query_kind", ["instruments", "depth"])
def test_invalid_absolute_deadline_is_local_preflight_and_not_submitted(query_kind):
    client, api, _spi = _ready_client()
    submissions = []
    method_name = (
        "ReqQryInstrument" if query_kind == "instruments" else "ReqQryDepthMarketData"
    )
    api.handlers[method_name] = lambda *_args: submissions.append(True) or 0

    result = _call_query(client, query_kind, deadline_monotonic=True, timeout=1)

    assert submissions == []
    assert result.unsupported is True
    assert result.error_message == "query_deadline_invalid"
    assert result.first_completion_origin == "local_preflight"
    assert result.completion_origin == "local_preflight"


@pytest.mark.parametrize("query_kind", ["instruments", "depth"])
@pytest.mark.parametrize("wait_kind", ["query_lock", "rate_limit"])
def test_absolute_deadline_bounds_lock_and_rate_wait_before_native_submit(
    query_kind, wait_kind
):
    client, api, _spi = _ready_client()
    submissions = []
    method_name = (
        "ReqQryInstrument" if query_kind == "instruments" else "ReqQryDepthMarketData"
    )
    api.handlers[method_name] = lambda *_args: submissions.append(True) or 0
    deadline = time.monotonic() + 0.03

    if wait_kind == "query_lock":
        assert client._query_lock.acquire(timeout=0.1)
        result_holder = []
        worker = threading.Thread(
            target=lambda: result_holder.append(
                _call_query(client, query_kind, deadline_monotonic=deadline, timeout=5)
            ),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=1)
        client._query_lock.release()
        assert not worker.is_alive()
        result = result_holder[0]
    else:
        client._query_interval = 10
        client._last_query_submitted_at = time.monotonic()
        started = time.monotonic()
        result = _call_query(
            client, query_kind, deadline_monotonic=deadline, timeout=5
        )
        assert time.monotonic() - started < 0.5

    assert submissions == []
    assert result.complete is False
    assert result.timed_out is True
    assert result.error_message == "query_deadline_exceeded"
    assert result.first_completion_origin == "deadline"
    assert result.completion_origin == "deadline"


@pytest.mark.parametrize("query_kind", ["instruments", "depth"])
def test_absolute_deadline_bounds_native_response_wait(query_kind):
    client, api, _spi = _ready_client()
    submissions = []
    method_name = (
        "ReqQryInstrument" if query_kind == "instruments" else "ReqQryDepthMarketData"
    )
    api.handlers[method_name] = lambda *_args: submissions.append(True) or 0
    deadline = time.monotonic() + 0.03

    started = time.monotonic()
    result = _call_query(
        client,
        query_kind,
        timeout=5,
        deadline_monotonic=deadline,
        **({"max_duration": 5} if query_kind == "instruments" else {}),
    )

    assert time.monotonic() - started < 0.5
    assert submissions == [True]
    assert result.complete is False
    assert result.timed_out is True
    assert result.error_message == "query_deadline_exceeded"
    assert result.first_completion_origin == "deadline"
    assert result.completion_origin == "deadline"


@pytest.mark.parametrize("query_kind", ["instruments", "depth"])
def test_absolute_deadline_is_reported_after_synchronous_native_submit_overrun(query_kind):
    client, api, _spi = _ready_client()
    submissions = []
    method_name = (
        "ReqQryInstrument" if query_kind == "instruments" else "ReqQryDepthMarketData"
    )

    def slow_submit(*_args):
        submissions.append(True)
        time.sleep(0.03)
        return 0

    api.handlers[method_name] = slow_submit
    deadline = time.monotonic() + 0.01
    result = _call_query(
        client,
        query_kind,
        timeout=1,
        deadline_monotonic=deadline,
    )

    assert submissions == [True]
    assert result.timed_out is True
    assert result.error_message == "query_deadline_exceeded"
    assert result.first_completion_origin == "deadline"
    assert result.completion_origin == "deadline"


def test_absolute_deadline_caps_progressive_instrument_callbacks():
    client, api, spi = _ready_client()
    stop_delivery = threading.Event()
    delivery_threads = []

    def submit(_field, request_id):
        def deliver_progress():
            while not stop_delivery.is_set():
                spi.OnRspQryInstrument(
                    SimpleNamespace(InstrumentID="rb2701"),
                    None,
                    request_id,
                    False,
                )
                time.sleep(0.002)

        worker = threading.Thread(target=deliver_progress, daemon=True)
        delivery_threads.append(worker)
        worker.start()
        return 0

    api.handlers["ReqQryInstrument"] = submit
    deadline = time.monotonic() + 0.04
    try:
        result = client.query_instruments_result(
            timeout=0.2,
            max_duration=1,
            deadline_monotonic=deadline,
        )
    finally:
        stop_delivery.set()
        for worker in delivery_threads:
            worker.join(timeout=1)

    assert result.complete is False
    assert result.timed_out is True
    assert result.error_message == "query_deadline_exceeded"
    assert result.first_completion_origin == "deadline"
    assert result.completion_origin == "deadline"
