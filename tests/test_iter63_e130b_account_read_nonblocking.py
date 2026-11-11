"""I63-E130 (account read): whole-account queries and the account read as request and response.

Fake native only: the venue's answer arrives when the test delivers it, as a slow
SimNow front does. No account or network is contacted.
"""

import threading
import time
from types import SimpleNamespace

import pytest
from test_iter62_settlement_readback_on_write import TD_FRONT, _login, _NativeApi
from test_iter63_gateway_session import prepared  # noqa: F401  (fixture used by ``gateway``)

from bt_api_ctp.account_provider import CtpAccountSessionProviderError
from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.gateway_session import CtpGatewaySessionError

_ACCOUNT_ROW = dict(AccountID="account", BrokerID="9999", CurrencyID="CNY", Balance=1000,
                    Available=800)
_METHODS = {"account": "ReqQryTradingAccount", "positions": "ReqQryInvestorPosition",
            "orders": "ReqQryOrder", "trades": "ReqQryTrade"}


class HeldNative(_NativeApi):
    """Accepts every whole-account query and answers only on ``deliver``."""

    def __init__(self):
        super().__init__()
        self.rows = {"account": [_ACCOUNT_ROW], "positions": [], "orders": [], "trades": []}
        self.held, self.sent = [], []
        self.send_code = 0
        for kind, method in _METHODS.items():
            setattr(self, method, self._accept(kind))

    def _accept(self, kind):
        def submit(_field, request_id):
            self.sent.append((kind, time.monotonic()))
            if self.send_code:
                return self.send_code
            self.held.append((kind, request_id))
            return 0
        return submit

    def deliver(self, *, error_id=None):
        kind, request_id = self.held.pop(0)
        if error_id is not None:
            info = SimpleNamespace(ErrorID=error_id, ErrorMsg="synthetic")
            self.client._handle_query_callback(kind, None, info, request_id, True)
            return
        for row in self.rows[kind]:
            self.client._handle_query_callback(kind, row, None, request_id, False)
        self.client._handle_query_callback(kind, None, None, request_id, True)


def _client(interval=0.0):
    native = HeldNative()
    client = TraderClient(TD_FRONT, "9999", "account", "synthetic-only", auto_settlement_confirm=False)
    native.client = client
    client._api = native
    _login(client, native, session_id=19)
    client._query_interval = interval
    return client, native


def _timed(call):
    started = time.monotonic()
    value = call()
    return value, time.monotonic() - started


def _evidence(result):
    source = result.query_source
    return (source.request_type, source.request_intent_filters, source.request_filters,
            source.request_intent_parameters, source.request_parameters,
            source.explicit_request_filters, source.record_limit, source.broker_id,
            source.investor_id, source.trading_day, source.connection_generation)


# ---------------------------------------------------------------------------
# TraderClient.begin_query / poll_query / cancel_query
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["account", "positions", "orders", "trades"])
def test_query_never_waits_and_carries_the_blocking_query_evidence(kind):
    client, native = _client()
    handle = client.begin_query(kind, timeout=5)
    assert native.sent == []  # beginning sends nothing
    for _ in range(5):
        result, elapsed = _timed(lambda: client.poll_query(handle))
        assert result is None and elapsed < 0.2
    assert [entry[0] for entry in native.sent] == [kind]  # sent once, then only watched
    native.deliver()
    result = client.poll_query(handle)
    assert result.complete is True and result.request_type == kind
    with pytest.raises(ValueError, match="ctp_query_handle_invalid"):
        client.poll_query(handle)  # a finished query's handle is retired
    # The very same native request and evidence as the blocking unfiltered query.
    native.held.clear()
    for name, method in _METHODS.items():
        setattr(native, method, _answer_at_once(client, native, name))
    blocking = getattr(client, f"query_{kind}_result")(timeout=5)
    assert blocking.complete is True
    assert _evidence(result) == _evidence(blocking)


def _answer_at_once(client, native, kind):
    def submit(_field, request_id):
        for row in native.rows[kind]:
            client._handle_query_callback(kind, row, None, request_id, False)
        client._handle_query_callback(kind, None, None, request_id, True)
        return 0
    return submit


def test_lane_and_spacing_are_shared_with_every_query_of_the_session():
    client, native = _client(interval=0.3)
    client._last_query_submitted_at = 0.0
    first = client.begin_query("account", timeout=5)
    assert client.poll_query(first) is None and len(native.sent) == 1
    second = client.begin_query("positions", timeout=5)
    # One native query per session: the second waits for the first's last packet.
    for _ in range(3):
        result, elapsed = _timed(lambda: client.poll_query(second))
        assert result is None and elapsed < 0.2
    assert len(native.sent) == 1 and client.query_lane_idle() is False
    # The blocking path sees the same reserved lane.
    blocked = client.query_orders_result(timeout=0.1)
    assert blocked.complete is False and blocked.first_completion_origin == "query_slot_busy"
    native.deliver()
    assert client.poll_query(first).complete is True
    # Spacing: no send before the interval has passed since the last send, no wait either.
    result, elapsed = _timed(lambda: client.poll_query(second))
    assert result is None and elapsed < 0.2 and len(native.sent) == 1
    assert client.query_lane_idle() is False
    deadline = time.monotonic() + 3
    while len(native.sent) < 2:
        assert time.monotonic() < deadline
        client.poll_query(second)
        time.sleep(0.01)
    assert native.sent[1][1] - native.sent[0][1] >= 0.3
    native.deliver()
    assert client.poll_query(second).complete is True


def test_unanswered_query_times_out_and_a_late_answer_only_counts_as_late():
    client, native = _client()
    handle = client.begin_query("orders", timeout=0.2)
    assert client.poll_query(handle) is None
    time.sleep(0.25)
    result = client.poll_query(handle)
    assert result.complete is False and result.timed_out is True
    assert result.first_completion_origin == "timeout"
    native.deliver()  # the venue answers after the local timeout
    assert client.get_query_result(result.request_id).late_callback_count == 1
    assert client.get_query_result(result.request_id).complete is False


def test_lane_busy_beyond_its_budget_ends_as_slot_busy_without_sending():
    client, native = _client()
    blocker = client.begin_query("trades", timeout=5)
    assert client.poll_query(blocker) is None
    handle = client.begin_query("account", timeout=0.1)
    # No spacing to wait for: only the outstanding native query keeps it back.
    assert client.poll_query(handle) is None and [kind for kind, _ in native.sent] == ["trades"]
    time.sleep(0.15)
    result = client.poll_query(handle)
    assert result.complete is False and result.first_completion_origin == "query_slot_busy"
    assert [entry[0] for entry in native.sent] == ["trades"]


@pytest.mark.parametrize("answered_first", [False, True])
def test_a_changed_session_ends_the_query_even_with_an_answer(answered_first):
    client, native = _client()
    handle = client.begin_query("positions", timeout=5)
    assert client.poll_query(handle) is None
    if answered_first:
        native.deliver()
    _login(client, native, session_id=20)  # a new login: a new connection generation
    if not answered_first:
        native.deliver()  # the old session's answer arrives after the new login
    result = client.poll_query(handle)
    assert result.complete is False and result.error_code == -2
    assert result.completion_origin == "front_disconnected"


def test_rejected_send_releases_the_lane():
    client, native = _client()
    native.send_code = -3
    handle = client.begin_query("account", timeout=5)
    result = client.poll_query(handle)
    assert result.complete is False and result.error_code == -3
    assert result.first_completion_origin == "submit_rejected"
    assert client.query_lane_idle() is True


def test_cancel_seals_locally_and_keeps_the_vendor_lane_until_its_last_packet():
    client, native = _client()
    handle = client.begin_query("account", timeout=5)
    assert client.poll_query(handle) is None
    request_id = client._query_native_inflight[0]
    client.cancel_query(handle)
    client.cancel_query(handle)  # a retired handle is ignored
    assert client.get_query_result(request_id).first_completion_origin == "local_cancel"
    assert client.query_lane_idle() is False
    native.deliver()
    assert client.query_lane_idle() is True
    unsent = client.begin_query("orders", timeout=5)
    client.cancel_query(unsent)
    assert native.sent[-1][0] == "account"  # an unsent query never reaches the venue


def test_query_is_bound_to_its_owner_thread_and_inputs_are_checked():
    client, _ = _client()
    handle = client.begin_query("account", timeout=5)
    errors = []

    def other():
        try:
            client.poll_query(handle)
        except RuntimeError as error:
            errors.append(str(error))

    thread = threading.Thread(target=other)
    thread.start()
    thread.join(5)
    assert errors == ["ctp_query_owner_mismatch"]
    for kind, timeout, records in (("instruments", 5, None), ("account", 0, None),
                                   ("account", float("inf"), None), ("account", True, None),
                                   ("account", 5, 0)):
        with pytest.raises(ValueError):
            client.begin_query(kind, timeout=timeout, max_records=records)


# ---------------------------------------------------------------------------
# CtpGatewaySession / provider: begin_read_account / poll_read_account
# ---------------------------------------------------------------------------
@pytest.fixture
def gateway(request):
    """The offline Gateway session factory of ``test_iter63_gateway_session``."""
    return request.getfixturevalue("prepared")


def _session(gateway, interval=0.0):
    create, _, _, feeds, natives, _, _, _ = gateway
    session = create()
    session.connect(generation="g", timeout=1)
    trader = feeds[0].trader_client
    native = HeldNative()
    native.client = trader
    for name in _METHODS.values():
        setattr(natives[0], name, getattr(native, name))
    trader._query_interval = interval
    return session, trader, native, natives[0]


def _drive(session, request, native, *, deliver=True, limit=3.0):
    """Poll as the owner tick does; answer each sent query. Returns (observation, max poll)."""
    deadline, slowest = time.monotonic() + limit, 0.0
    while time.monotonic() < deadline:
        result, elapsed = _timed(lambda: session.poll_read_account(request))
        slowest = max(slowest, elapsed)
        if result is not None:
            return result, slowest
        assert len(native.held) <= 1  # never two native queries outstanding
        if deliver:
            while native.held:
                native.deliver()
        time.sleep(0.005)
    raise AssertionError("read did not complete")


def test_account_read_never_waits_and_equals_the_blocking_read(gateway):
    session, trader, native, _ = _session(gateway, interval=0.1)
    request = session.begin_read_account(generation="g", timeout=5)
    assert native.sent == [] and session._reader.account_read_pending is True
    observation, slowest = _drive(session, request, native)
    assert slowest < 0.1  # the owner never waited for the venue or the spacing
    assert [kind for kind, _ in native.sent] == ["account", "positions", "orders"]
    sent_at = [at for _, at in native.sent]
    assert all(later - earlier >= 0.1 for earlier, later in zip(sent_at, sent_at[1:]))
    assert session._reader.account_read_pending is False
    for name, method in _METHODS.items():
        setattr(native, method, _answer_at_once(trader, native, name))
    for name in _METHODS.values():
        setattr(gateway[4][0], name, getattr(native, name))
    blocking = session.read_account(generation="g", timeout=5)
    for view in (observation, blocking):
        assert view.positions_complete is True and view.open_orders_complete is True
    assert [(b.currency, b.total, b.available) for b in observation.balances] == [
        (b.currency, b.total, b.available) for b in blocking.balances] == [("CNY", "1000", "800")]
    assert observation.positions == blocking.positions == ()
    assert observation.open_orders == blocking.open_orders == ()
    assert session.close() is True


def test_one_read_at_a_time_and_the_blocking_read_refuses_meanwhile(gateway):
    session, _, native, _ = _session(gateway)
    request = session.begin_read_account(generation="g", timeout=5)
    assert session.poll_read_account(request) is None and len(native.sent) == 1
    for call in (lambda: session.read_account(generation="g", timeout=1),
                 lambda: session.begin_read_account(generation="g", timeout=1)):
        with pytest.raises(CtpAccountSessionProviderError, match="QUERY_PENDING"):
            call()
    assert len(native.sent) == 1  # the refusals sent nothing
    with pytest.raises(CtpGatewaySessionError, match="SESSION_CHANGED"):
        session.begin_read_account(generation="other", timeout=1)
    observation, _ = _drive(session, request, native)
    assert observation.positions_complete is True
    assert session.close() is True


def test_read_that_runs_out_of_budget_fails_and_is_never_used(gateway):
    session, _, native, _ = _session(gateway)
    request = session.begin_read_account(generation="g", timeout=0.3)
    assert session.poll_read_account(request) is None
    native.deliver()  # the account answer arrives; positions is not answered in time
    assert session.poll_read_account(request) is None
    assert [kind for kind, _ in native.sent] == ["account", "positions"]
    time.sleep(0.35)
    started = time.monotonic()
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_BUDGET_EXCEEDED"):
        session.poll_read_account(request)
    assert time.monotonic() - started < 0.1
    assert session._reader.account_read_pending is False
    native.deliver()  # the late positions answer
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_REQUEST_UNKNOWN"):
        session.poll_read_account(request)
    # The next read starts clean once the venue released the lane.
    fresh = session.begin_read_account(generation="g", timeout=5)
    observation, _ = _drive(session, fresh, native)
    assert observation.positions_complete is True
    assert session.close() is True


@pytest.mark.parametrize("failing", ["account", "positions", "orders"])
def test_an_answer_that_fails_its_checks_fails_the_read(gateway, failing):
    session, _, native, _ = _session(gateway)
    request = session.begin_read_account(generation="g", timeout=5)
    while True:
        assert session.poll_read_account(request) is None
        kind = native.held[0][0]
        if kind == failing:
            native.deliver(error_id=90)  # the venue answers with an error
            break
        native.deliver()
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_INCOMPLETE"):
        session.poll_read_account(request)
    assert session._reader.account_read_pending is False
    assert session.close() is True


def test_read_whose_budget_runs_out_while_it_waits_for_the_spacing_fails(gateway):
    # The default 2 s budget is shorter than three queries spaced 1.05 s apart.
    session, trader, native, _ = _session(gateway, interval=1.0)
    trader._last_query_submitted_at = 0.0
    request = session.begin_read_account(generation="g", timeout=0.3)
    assert session.poll_read_account(request) is None
    native.deliver()
    assert session.poll_read_account(request) is None  # positions waits for the spacing
    assert [kind for kind, _ in native.sent] == ["account"]
    time.sleep(0.35)
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_BUDGET_EXCEEDED"):
        session.poll_read_account(request)
    assert session._reader.account_read_pending is False and trader._nonblocking_queries == {}
    assert [kind for kind, _ in native.sent] == ["account"]  # positions was never sent
    assert session.close() is True


@pytest.mark.parametrize("refreshed", [False, True])
def test_read_whose_session_changed_fails_and_is_never_used(gateway, refreshed):
    session, trader, native, natives = _session(gateway)
    request = session.begin_read_account(generation="g", timeout=5)
    assert session.poll_read_account(request) is None
    native.deliver()  # account answered on the old session
    assert session.poll_read_account(request) is None  # positions sent
    _login(trader, natives, session_id=20)  # reconnect: a new native session
    native.deliver()  # the old session's positions answer arrives late
    if refreshed:
        session.refresh_session()  # what the owner tick does first
    with pytest.raises((CtpAccountSessionProviderError, CtpGatewaySessionError)) as raised:
        session.poll_read_account(request)
    assert raised.value.code in {"QUERY_SCOPE_CHANGED", "SESSION_CHANGED", "QUERY_INCOMPLETE"}
    assert session._reader.account_read_pending is False
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_REQUEST_UNKNOWN"):
        session._reader.poll_read_account(request)  # discarded: nothing of it is ever used
    assert session.close() is True


def test_a_generation_change_between_answers_fails_the_read(gateway):
    session, _, native, _ = _session(gateway)
    request = session.begin_read_account(generation="g", timeout=5)
    assert session.poll_read_account(request) is None
    native.deliver()
    session._reader._service_generation = "g2"  # the provider was rebound meanwhile
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_SCOPE_CHANGED"):
        session._reader.poll_read_account(request)
    assert session._reader.account_read_pending is False
    session._reader._service_generation = "g"
    assert session.close() is True


def test_session_lane_is_ready_only_when_a_query_would_leave_at_once(gateway):
    session, trader, native, _ = _session(gateway, interval=0.2)
    trader._last_query_submitted_at = 0.0
    assert session.query_lane_ready() is True
    request = session.begin_read_account(generation="g", timeout=5)
    assert session.poll_read_account(request) is None and len(native.sent) == 1
    assert session.query_lane_ready() is False  # a native query is outstanding
    native.deliver()
    assert session.poll_read_account(request) is None  # positions waits for the spacing
    assert len(native.sent) == 1 and session.query_lane_ready() is False
    time.sleep(0.25)
    assert session.query_lane_ready() is True
    session.cancel_read_account(request)
    assert session.close() is True
    assert session.query_lane_ready() is False  # a closed session has no ready lane


def test_cancel_and_close_discard_the_outstanding_read(gateway):
    session, trader, native, _ = _session(gateway)
    request = session.begin_read_account(generation="g", timeout=5)
    assert session.poll_read_account(request) is None
    session.cancel_read_account(object())  # another handle is ignored
    assert session._reader.account_read_pending is True
    session.cancel_read_account(request)
    assert session._reader.account_read_pending is False and trader._nonblocking_queries == {}
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_REQUEST_UNKNOWN"):
        session.poll_read_account(request)
    native.deliver()
    again = session.begin_read_account(generation="g", timeout=5)
    assert session.poll_read_account(again) is None
    assert session.close() is True
    assert session._reader.account_read_pending is False and trader._nonblocking_queries == {}
