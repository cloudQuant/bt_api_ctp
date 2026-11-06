"""Iteration 62: one outstanding native ReqQry* per CTP Trader session.

SimNow showed that a query which timed out locally is still pending at the
vendor; every later ReqQry* returned -2 until its terminal packet arrived. The
client now tracks that native request until its terminal callback (including a
late one after the local seal) and waits for it, bounded by the next query's
own timeout/deadline, before submitting.

UNIT/FAKE evidence only: a real ``TraderClient`` with a fake native API that
models the vendor's outstanding-request limit. No network, no account.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from test_ctp_callback_ingress_client import (
    _install_query_field_fakes,
    _new_active_client,
    _query_callback_info,
    _query_callback_row,
)

from bt_api_ctp.ctp.client import TraderClient, _TraderSpi

_OK = SimpleNamespace(ErrorID=0, ErrorMsg="")


class _VendorApi:
    """Fake Trader API: the vendor rejects a ReqQry* with -2 while one is pending."""

    _CALLBACKS = {
        "ReqQryInvestorPosition": "OnRspQryInvestorPosition",
        "ReqQryOrder": "OnRspQryOrder",
        "ReqQryTrade": "OnRspQryTrade",
        "ReqQryTradingAccount": "OnRspQryTradingAccount",
        "ReqQryInstrument": "OnRspQryInstrument",
    }

    def __init__(self):
        self.spi = None
        self.calls: list[tuple[str, int, int]] = []
        self.call_times: list[float] = []
        self.outstanding: int | None = None
        self.silent: set[str] = set()
        self.reject: dict[str, int] = {}
        self.raise_on: set[str] = set()
        self.rows: dict[str, list] = {}
        self._lock = threading.Lock()

    def _req(self, method, request_id):
        with self._lock:
            self.call_times.append(time.monotonic())
            if method in self.raise_on:
                self.calls.append((method, request_id, 0))
                raise RuntimeError("synthetic submit failure")
            if method in self.reject:
                code = self.reject[method]
                self.calls.append((method, request_id, code))
                return code
            if self.outstanding is not None:
                self.calls.append((method, request_id, -2))
                return -2
            self.calls.append((method, request_id, 0))
            self.outstanding = request_id
        if method not in self.silent:
            self.respond(method, request_id)
        return 0

    def respond(self, method, request_id, *, error=None):
        """Deliver the terminal packet; the vendor frees the request first."""
        with self._lock:
            if self.outstanding == request_id:
                self.outstanding = None
        callback = getattr(self.spi, self._CALLBACKS[method])
        rows = self.rows.get(method) or [None]
        for index, row in enumerate(rows):
            callback(row, error, request_id, index == len(rows) - 1)

    def submitted(self, method):
        return [call for call in self.calls if call[0] == method]

    def ReqQryInvestorPosition(self, _field, request_id):
        return self._req("ReqQryInvestorPosition", request_id)

    def ReqQryOrder(self, _field, request_id):
        return self._req("ReqQryOrder", request_id)

    def ReqQryTrade(self, _field, request_id):
        return self._req("ReqQryTrade", request_id)

    def ReqQryTradingAccount(self, _field, request_id):
        return self._req("ReqQryTradingAccount", request_id)

    def ReqQryInstrument(self, _field, request_id):
        return self._req("ReqQryInstrument", request_id)

    def RegisterSpi(self, spi):
        return None

    def Release(self):
        return None


def _login(client, generation):
    """Bind a logged-in read-only session at ``generation`` (offline pattern)."""
    bound_spi = client._spi
    with client._query_state_lock:
        client._connected = True
        client._authentication_state = "authenticated"
        client._login_state = "logging_in"
        client._trading_day = ""
        client._connection_generation = generation
        login_request_id = client._next_request_id()
        client._login_request_id = login_request_id
        client._login_connection_generation = generation
        login_spi = _TraderSpi(client, None)
        client._spi = login_spi
    login_spi.OnRspUserLogin(
        SimpleNamespace(
            BrokerID=client._bound_broker_id,
            UserID=client._bound_user_id,
            TradingDay="20261106",
            FrontID=7,
            SessionID=19,
            MaxOrderRef="0",
        ),
        _OK,
        login_request_id,
        True,
    )
    if bound_spi is not None:
        with client._query_state_lock:
            client._spi = bound_spi
    assert client.is_read_only_ready is True


def _ready_client():
    client = TraderClient(
        "tcp://offline", "9999", "offline", "fixture", auto_settlement_confirm=False
    )
    client._session_native_api = None
    client._session_native_front = client._bound_front
    client._query_interval = 0
    _login(client, 1)
    api = _VendorApi()
    spi = _TraderSpi(client, api)
    api.spi = spi
    with client._query_state_lock:
        client._api = api
        client._session_native_api = api
        client._spi = spi
    return client, api


def _time_out_positions(client, api):
    """A positions query the vendor accepts but answers only after the local timeout."""
    api.silent.add("ReqQryInvestorPosition")
    api.rows["ReqQryInvestorPosition"] = [
        SimpleNamespace(InstrumentID="rb2701", ExchangeID="SHFE", Position=2)
    ]
    result = client.query_positions_result(timeout=0.05)
    assert result.timed_out is True
    assert result.completion_origin == "timeout"
    assert [call[2] for call in api.submitted("ReqQryInvestorPosition")] == [0]
    # The vendor still owes this request its terminal packet.
    assert api.outstanding == result.request_id
    return result


def _slot(client):
    with client._query_state_lock:
        return client._query_native_inflight


def _deliver_later(api, method, request_id, delay):
    timer = threading.Timer(delay, api.respond, args=(method, request_id))
    timer.daemon = True
    timer.start()
    return timer


def test_late_islast_after_timeout_lets_the_next_query_submit_exactly_once():
    client, api = _ready_client()
    timed_out = _time_out_positions(client, api)

    late = _deliver_later(api, "ReqQryInvestorPosition", timed_out.request_id, 0.15)
    started = time.monotonic()
    orders = client.query_orders_result(timeout=2)
    elapsed = time.monotonic() - started
    late.join(timeout=1)

    # The orders Req waited for the late IsLast and was sent exactly once,
    # after it; the fake vendor never had to reject it with -2.
    assert [call[2] for call in api.submitted("ReqQryOrder")] == [0]
    assert all(code != -2 for _method, _request_id, code in api.calls)
    assert 0.1 <= elapsed < 1.5
    assert orders.complete is True
    assert orders.completion_origin == "success"
    assert _slot(client) is None

    # The sealed result stays timed out; the late rows are never added to it.
    sealed = client.get_query_result(timed_out.request_id)
    assert sealed.timed_out is True
    assert sealed.complete is False
    assert sealed.records == ()
    assert sealed.completion_origin == "timeout"
    assert sealed.late_callback_count == 1

    counts = client.get_request_counts()
    assert counts["query_positions"] == 1
    assert counts["query_orders"] == 1


def test_missing_late_islast_returns_query_slot_busy_without_a_native_req():
    client, api = _ready_client()
    timed_out = _time_out_positions(client, api)

    started = time.monotonic()
    busy = client.query_orders_result(timeout=0.1)
    elapsed = time.monotonic() - started

    assert api.submitted("ReqQryOrder") == []
    assert busy.complete is False
    assert busy.timed_out is True
    assert busy.error_code == -1
    assert busy.error_message == "query_slot_busy"
    assert busy.completion_origin == "query_slot_busy"
    assert busy.first_completion_origin == "query_slot_busy"
    assert busy.submit_code is None
    assert 0.08 <= elapsed < 1.0
    assert client.get_request_counts().get("query_orders", 0) == 0
    assert _slot(client) == (timed_out.request_id, "positions", 1)


def test_slot_wait_is_bounded_by_the_absolute_deadline_and_should_stop():
    client, api = _ready_client()
    _time_out_positions(client, api)

    started = time.monotonic()
    deadline_bound = client.query_orders_result(
        timeout=5, deadline_monotonic=time.monotonic() + 0.05
    )
    assert time.monotonic() - started < 1.0
    assert deadline_bound.error_message == "query_slot_busy"
    assert deadline_bound.timed_out is True

    stop = threading.Event()
    stopper = threading.Timer(0.05, stop.set)
    stopper.daemon = True
    stopper.start()
    started = time.monotonic()
    cancelled = client.query_instruments_result(timeout=5, should_stop=stop.is_set)
    stopper.join(timeout=1)
    assert time.monotonic() - started < 1.0
    assert cancelled.error_message == "query_cancelled"
    assert cancelled.completion_origin == "local_cancel"

    assert api.submitted("ReqQryOrder") == []
    assert api.submitted("ReqQryInstrument") == []


def test_late_error_response_also_clears_the_slot():
    client, api = _ready_client()
    timed_out = _time_out_positions(client, api)

    api.outstanding = None
    api.spi.OnRspError(SimpleNamespace(ErrorID=90, ErrorMsg="late"), timed_out.request_id, True)
    assert _slot(client) is None
    assert client.get_query_result(timed_out.request_id).timed_out is True

    account = client.query_account_result(timeout=0.5)
    assert account.complete is True
    assert [call[2] for call in api.submitted("ReqQryTradingAccount")] == [0]


def test_late_islast_for_an_evicted_accumulator_still_clears_the_slot():
    client, api = _ready_client()
    timed_out = _time_out_positions(client, api)
    with client._query_state_lock:
        client._query_history.pop(timed_out.request_id)

    api.respond("ReqQryInvestorPosition", timed_out.request_id)

    with client._query_state_lock:
        assert client._query_native_inflight is None
        assert client._orphan_query_callbacks[-1]["request_id"] == timed_out.request_id
    trades = client.query_trades_result(timeout=0.5)
    assert trades.complete is True


def test_terminal_packet_of_another_request_does_not_clear_the_slot():
    client, api = _ready_client()
    timed_out = _time_out_positions(client, api)

    api.spi.OnRspQryOrder(None, None, timed_out.request_id + 1000, True)
    api.spi.OnRspQryOrder(None, None, timed_out.request_id, True)  # wrong family

    assert _slot(client) == (timed_out.request_id, "positions", 1)


def test_connection_generation_change_clears_the_slot():
    client, api = _ready_client()
    timed_out = _time_out_positions(client, api)

    # A new vendor session (generation 2) has no outstanding request.
    api.outstanding = None
    _login(client, 2)
    assert _slot(client)[2] == 1

    started = time.monotonic()
    orders = client.query_orders_result(timeout=2)
    assert time.monotonic() - started < 0.5
    assert orders.complete is True
    assert orders.connection_generation == 2
    assert [call[2] for call in api.submitted("ReqQryOrder")] == [0]
    assert _slot(client) is None

    # The old generation's late packet never touches the new session's slot.
    api.silent.add("ReqQryTrade")
    pending = client.query_trades_result(timeout=0.05)
    assert _slot(client) == (pending.request_id, "trades", 2)
    api.spi.OnRspQryInvestorPosition(None, None, timed_out.request_id, True)
    assert _slot(client) == (pending.request_id, "trades", 2)


def test_disconnect_clears_the_slot_and_releases_a_waiting_query():
    client, api = _ready_client()
    _time_out_positions(client, api)
    holder = []
    worker = threading.Thread(
        target=lambda: holder.append(client.query_orders_result(timeout=3)), daemon=True
    )
    started = time.monotonic()
    worker.start()
    deadline = time.monotonic() + 1
    while not client._query_lock.locked() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert client._query_lock.locked()  # the waiter holds only the query lane
    # The waiter polls without the state lock, so the disconnect is not blocked.
    api.spi.OnFrontDisconnected(0x1001)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert time.monotonic() - started < 1.5
    assert _slot(client) is None
    assert holder[0].error_message == "trader_not_logged_in"
    assert holder[0].completion_origin == "local_preflight"
    assert api.submitted("ReqQryOrder") == []


def test_client_stop_clears_the_slot(monkeypatch):
    client, api = _ready_client()
    _time_out_positions(client, api)
    monkeypatch.setattr(client, "_release_native_api_immediately", lambda _api, _spi: True)

    assert client._stop_native_session(expected_api=api) is True

    assert _slot(client) is None


@pytest.mark.parametrize("outcome", ["rejected", "exception"])
def test_a_failed_submit_is_not_tracked_as_outstanding(outcome):
    client, api = _ready_client()
    if outcome == "rejected":
        api.reject["ReqQryInvestorPosition"] = -3
    else:
        api.raise_on.add("ReqQryInvestorPosition")

    failed = client.query_positions_result(timeout=0.5)
    assert failed.complete is False
    assert failed.completion_origin == (
        "submit_rejected" if outcome == "rejected" else "submit_exception"
    )
    assert _slot(client) is None

    started = time.monotonic()
    orders = client.query_orders_result(timeout=2)
    assert time.monotonic() - started < 0.5
    assert orders.complete is True


@pytest.mark.parametrize("window", ["cancel", "deadline_before_count", "deadline_after_count"])
def test_a_query_abandoned_between_reservation_and_submit_releases_the_slot(window, monkeypatch):
    client, api = _ready_client()
    slept = []
    deadline = None

    def should_stop():
        # The slot is reserved only between the query lane and the native Req.
        if _slot(client) is None:
            return False
        if window == "cancel":
            return True
        if not slept:
            slept.append(True)
            time.sleep(max(0.0, deadline - time.monotonic()) + 0.02)
        return False

    kwargs = {"timeout": 1}
    if window == "cancel":
        kwargs["should_stop"] = should_stop
    else:
        deadline = time.monotonic() + 0.3
        kwargs["deadline_monotonic"] = deadline
        if window == "deadline_before_count":
            kwargs["should_stop"] = should_stop
        else:
            original = client._record_request

            def slow_record(name):
                original(name)
                time.sleep(max(0.0, deadline - time.monotonic()) + 0.02)

            monkeypatch.setattr(client, "_record_request", slow_record)

    abandoned = client.query_instruments_result(**kwargs)

    assert api.submitted("ReqQryInstrument") == []
    if window == "cancel":
        assert abandoned.error_message == "query_cancelled"
        assert abandoned.completion_origin == "local_cancel"
    else:
        assert slept == ([True] if window == "deadline_before_count" else [])
        assert abandoned.error_message == "query_deadline_exceeded"
        assert abandoned.completion_origin == "deadline"
    assert _slot(client) is None
    monkeypatch.undo()
    started = time.monotonic()
    orders = client.query_orders_result(timeout=2)
    assert time.monotonic() - started < 0.5
    assert orders.complete is True


def test_a_query_started_on_the_spi_callback_thread_fails_fast_when_the_slot_is_busy():
    client, api = _ready_client()
    _time_out_positions(client, api)
    holder = []
    client.on_error = lambda _info: holder.append(client.query_orders_result(timeout=2))

    started = time.monotonic()
    # An unrelated error packet runs a user callback on the SPI thread.
    api.spi.OnRspError(SimpleNamespace(ErrorID=1, ErrorMsg="other"), 99_999, True)

    assert time.monotonic() - started < 0.5
    assert holder[0].error_message == "query_slot_busy"
    assert holder[0].completion_origin == "query_slot_busy"
    assert api.submitted("ReqQryOrder") == []


def test_back_to_back_fast_queries_behave_as_before():
    client, api = _ready_client()
    client._query_interval = 0.05
    api.rows["ReqQryInvestorPosition"] = [
        SimpleNamespace(InstrumentID="rb2701", ExchangeID="SHFE", Position=2)
    ]

    results = [
        client.query_positions_result(timeout=1),
        client.query_orders_result(timeout=1),
        client.query_trades_result(timeout=1),
        client.query_account_result(timeout=1),
    ]

    assert all(result.complete for result in results)
    assert all(result.completion_origin == "success" for result in results)
    assert all(result.late_callback_count == 0 for result in results)
    assert [call[0] for call in api.calls] == [
        "ReqQryInvestorPosition",
        "ReqQryOrder",
        "ReqQryTrade",
        "ReqQryTradingAccount",
    ]
    assert [call[2] for call in api.calls] == [0, 0, 0, 0]
    # The per-second rate gate still spaces native submissions.
    gaps = [b - a for a, b in zip(api.call_times, api.call_times[1:])]
    assert all(gap >= 0.04 for gap in gaps)
    assert len(results[0].records) == 1
    counts = client.get_request_counts()
    for name in ("query_positions", "query_orders", "query_trades", "query_account"):
        assert counts[name] == 1
    assert _slot(client) is None


def test_concurrent_query_waits_for_the_in_flight_one_instead_of_a_vendor_reject():
    client, api = _ready_client()
    api.silent.add("ReqQryInvestorPosition")
    first_holder = []
    first = threading.Thread(
        target=lambda: first_holder.append(client.query_positions_result(timeout=3)),
        daemon=True,
    )
    first.start()
    deadline = time.monotonic() + 1
    while not api.submitted("ReqQryInvestorPosition") and time.monotonic() < deadline:
        time.sleep(0.005)
    first_request_id = api.submitted("ReqQryInvestorPosition")[0][1]

    late = _deliver_later(api, "ReqQryInvestorPosition", first_request_id, 0.1)
    second = client.query_orders_result(timeout=2)
    first.join(timeout=2)
    late.join(timeout=1)

    assert first_holder[0].complete is True
    assert second.complete is True
    assert [call[0] for call in api.calls] == ["ReqQryInvestorPosition", "ReqQryOrder"]
    assert [call[2] for call in api.calls] == [0, 0]


def test_capped_query_late_terminal_skipped_by_ingress_still_clears_the_slot(monkeypatch):
    """A managed-ingress capped query's post-seal IsLast never reaches the handler."""
    _install_query_field_fakes(monkeypatch)
    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        client._query_interval = 0
        capped_ids = []

        def overflow_then_terminal(_field, request_id):
            capped_ids.append(request_id)
            for index in range(2):
                api.spi.OnRspQryInstrument(
                    _query_callback_row("OnRspQryInstrument", InstrumentID=f"rb{2710 + index}"),
                    _query_callback_info(),
                    request_id,
                    False,
                )
            # The terminal packet arrives after the record-limit seal.
            api.spi.OnRspQryInstrument(None, _query_callback_info(), request_id, True)

        api.query_callbacks["ReqQryInstrument"] = overflow_then_terminal
        capped = client.query_instruments_result(timeout=0.5, max_records=1)
        assert capped.record_limit_exceeded is True
        assert capped.late_callback_count == 1
        assert _slot(client) is None

        def answer(_field, request_id):
            api.spi.OnRspQryInstrument(None, _query_callback_info(), request_id, True)

        api.query_callbacks["ReqQryInstrument"] = answer
        started = time.monotonic()
        follow_up = client.query_instruments_result(timeout=0.5)
        assert time.monotonic() - started < 0.4
        assert follow_up.complete is True
        assert sum(call[0] == "ReqQryInstrument" for call in api.calls) == 2
    finally:
        client.stop()
