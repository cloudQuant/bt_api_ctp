"""I63-E130: order and trade reads as request and response on the owner thread.

Fake native only: the venue's answer arrives when the test delivers it, as a
slow SimNow front does. No account or network is contacted.
"""

import threading
import time
from types import SimpleNamespace

import pytest
from test_iter62_settlement_readback_on_write import TD_FRONT, _login
from test_iter63_gateway_return_query import FakeNative, _row

from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.gateway_return_query import (
    CtpGatewayReturnQueryError,
    CtpGatewayReturnQueryReader,
)


class HeldNative(FakeNative):
    """Accepts each query and answers only on ``deliver``."""

    def __init__(self):
        super().__init__()
        self.held = []
        self.send_code = 0

    def _answer(self, kind, request_id):
        self.request_count += 1
        self.requests.append((kind, request_id))
        if self.send_code:
            return self.send_code
        self.held.append((kind, request_id))
        return 0

    def deliver(self, *, error_id=None):
        kind, request_id = self.held.pop(0)
        if error_id is not None:
            info = SimpleNamespace(ErrorID=error_id, ErrorMsg="synthetic")
            self.client._handle_query_callback(kind, None, info, request_id, True)
            return
        for row in self.rows[kind]:
            self.client._handle_query_callback(kind, row, None, request_id, False)
        self.client._handle_query_callback(kind, None, None, request_id, True)


def _setup(interval=0):
    native = HeldNative()
    client = TraderClient(TD_FRONT, "9999", "account", "synthetic-only", auto_settlement_confirm=False)
    native.client = client
    client._api = native
    _login(client, native, session_id=19)
    client._query_interval = interval
    reader = CtpGatewayReturnQueryReader(
        client, environment="simnow", broker_id="9999", investor_id="account"
    )
    return reader, client, native


def _timed(call):
    started = time.monotonic()
    value = call()
    return value, time.monotonic() - started


@pytest.mark.parametrize("kind", ["orders", "trades"])
def test_outstanding_read_never_waits_and_hands_over_the_trusted_batch(kind):
    reader, client, native = _setup()
    native.rows[kind] = [_row(), _row(TradeID="t-2", OrderSysID="o-2")]
    request = reader.begin_read(kind=kind, timeout=5)
    assert native.request_count == 0  # beginning sends nothing
    for _ in range(5):
        done, elapsed = _timed(lambda: reader.poll_read(request))
        assert done is False and elapsed < 0.2
    assert native.request_count == 1  # sent once, then only watched
    native.deliver()
    assert reader.poll_read(request) is True and reader.poll_read(request) is True
    batch = reader.read(kind=kind, timeout=5)  # the second phase: no native call
    assert native.request_count == 1
    assert batch.kind == kind and len(batch.rows) == 2 and batch.rows[1]["TradeID"] == "t-2"
    reader.assert_current(batch)
    assert client._query_native_inflight is None
    # Handed over once; the handle is spent.
    with pytest.raises(CtpGatewayReturnQueryError, match="^REQUEST_UNKNOWN$"):
        reader.poll_read(request)


def test_unanswered_read_ends_as_timeout_and_its_late_answer_is_not_used():
    reader, client, native = _setup()
    native.rows["orders"] = [_row()]
    request = reader.begin_read(kind="orders", timeout=0.3)
    started = time.monotonic()
    with pytest.raises(CtpGatewayReturnQueryError, match="^QUERY_TIMEOUT$"):
        while True:
            done, elapsed = _timed(lambda: reader.poll_read(request))
            assert done is False and elapsed < 0.2
            time.sleep(0.02)
    assert 0.3 <= time.monotonic() - started < 2
    request_id = native.requests[0][1]
    sealed = client.get_query_result(request_id)
    assert sealed.timed_out is True and sealed.completion_origin == "timeout"
    assert reader._ticket is None and reader._pending is None
    # The venue's request is still open: the lane stays reserved, nothing is sent.
    retry = reader.begin_read(kind="orders", timeout=5)
    assert reader.poll_read(retry) is False and native.request_count == 1
    native.deliver()  # late: counted, never accumulated into the sealed result
    late = client.get_query_result(request_id)
    assert late.records == () and late.late_callback_count >= 1
    assert client._query_native_inflight is None
    assert reader.poll_read(retry) is False and native.request_count == 2
    native.deliver()
    assert reader.poll_read(retry) is True
    assert reader.read(kind="orders", timeout=5).request_id == native.requests[1][1]


def test_reconnect_while_outstanding_fails_closed_and_the_old_answer_is_dropped():
    reader, client, native = _setup()
    native.rows["trades"] = [_row()]
    request = reader.begin_read(kind="trades", timeout=5)
    assert reader.poll_read(request) is False and native.request_count == 1
    _login(client, native, session_id=20)
    with pytest.raises(CtpGatewayReturnQueryError, match="^SESSION_CHANGED$"):
        reader.poll_read(request)
    old_id = native.requests[0][1]
    assert client.get_query_result(old_id).completion_origin == "local_cancel"
    native.deliver()  # the old generation's answer
    assert client.get_query_result(old_id).records == ()
    with pytest.raises(CtpGatewayReturnQueryError, match="^REQUEST_UNKNOWN$"):
        reader.poll_read(request)
    assert reader._ticket is None


def test_query_interval_and_one_outstanding_query_are_kept_without_waiting():
    reader, client, native = _setup(interval=0.4)
    client._last_query_submitted_at = 0.0
    first = reader.begin_read(kind="orders", timeout=5)
    assert reader.poll_read(first) is False and native.request_count == 1
    sent = time.monotonic()
    native.deliver()
    assert reader.poll_read(first) is True
    reader.read(kind="orders", timeout=5)
    second = reader.begin_read(kind="trades", timeout=5)
    while native.request_count == 1:
        done, elapsed = _timed(lambda: reader.poll_read(second))
        assert done is False and elapsed < 0.2
        time.sleep(0.01)
    assert time.monotonic() - sent >= 0.4  # never back to back faster than the interval
    native.deliver()
    assert reader.poll_read(second) is True
    # Another query of this Trader (an account read, say) holds the lane.
    client._query_interval = 0
    with client._query_state_lock:
        client._query_native_inflight = (999_999, "account", client._connection_generation)
    third = reader.begin_read(kind="orders", timeout=0.2)
    assert reader.poll_read(third) is False and native.request_count == 2
    time.sleep(0.25)
    with pytest.raises(CtpGatewayReturnQueryError, match="^QUERY_SLOT_BUSY$"):
        reader.poll_read(third)
    assert native.request_count == 2


@pytest.mark.parametrize(
    "fault,code",
    [("venue_error", "QUERY_VENUE_ERROR_90"), ("rejected", "QUERY_SUBMIT_REJECTED_3")],
)
def test_untrusted_answers_name_their_reason(fault, code):
    """I63-E134: the log tells a slow venue from flow control or a venue error."""
    reader, client, native = _setup()
    if fault == "rejected":
        native.send_code = -3
    request = reader.begin_read(kind="orders", timeout=5)
    if fault == "venue_error":
        assert reader.poll_read(request) is False
        native.deliver(error_id=90)
    with pytest.raises(CtpGatewayReturnQueryError) as caught:
        reader.poll_read(request)
    assert caught.value.code == code
    assert client._query_native_inflight is None and reader._ticket is None
    # The blocking read reports the same reason.
    if fault == "rejected":
        with pytest.raises(CtpGatewayReturnQueryError) as caught:
            reader.read(kind="orders", timeout=1)
        assert caught.value.code == code


def test_one_read_at_a_time_and_cancel_seals_locally():
    reader, client, native = _setup()
    first = reader.begin_read(kind="orders", timeout=5)
    assert reader.poll_read(first) is False
    second = reader.begin_read(kind="trades", timeout=5)  # discards the first
    assert client.get_query_result(native.requests[0][1]).completion_origin == "local_cancel"
    with pytest.raises(CtpGatewayReturnQueryError, match="^REQUEST_UNKNOWN$"):
        reader.poll_read(first)
    reader.cancel_read(first)  # not the current read: ignored
    assert reader._pending is not None
    with pytest.raises(CtpGatewayReturnQueryError, match="^QUERY_PENDING$"):
        reader.read(kind="trades", timeout=1)  # never waits behind an outstanding read
    assert reader._pending is None
    third = reader.begin_read(kind="orders", timeout=5)
    native.held.clear()
    with client._query_state_lock:
        client._query_native_inflight = None
    assert reader.poll_read(third) is False
    reader.cancel_read(third)
    assert client.get_query_result(native.requests[-1][1]).completion_origin == "local_cancel"
    with pytest.raises(CtpGatewayReturnQueryError, match="^REQUEST_UNKNOWN$"):
        reader.poll_read(third)
    assert second is not third


def test_completed_batch_is_rechecked_when_handed_over():
    reader, client, native = _setup()
    native.rows["orders"] = [_row()]
    request = reader.begin_read(kind="orders", timeout=5)
    reader.poll_read(request)
    native.deliver()
    assert reader.poll_read(request) is True
    _login(client, native, session_id=20)
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.read(kind="orders", timeout=5)
    assert reader._ticket is None and reader._pending is None
    assert native.request_count == 1


def test_wrong_kind_handover_and_invalid_request_are_refused():
    reader, _, native = _setup()
    with pytest.raises(CtpGatewayReturnQueryError, match="^INVALID_REQUEST$"):
        reader.begin_read(kind="positions", timeout=5)
    with pytest.raises(CtpGatewayReturnQueryError, match="^INVALID_REQUEST$"):
        reader.begin_read(kind="orders", timeout=301)
    request = reader.begin_read(kind="orders", timeout=5)
    reader.poll_read(request)
    native.deliver()
    assert reader.poll_read(request) is True
    with pytest.raises(CtpGatewayReturnQueryError, match="^QUERY_PENDING$"):
        reader.read(kind="trades", timeout=5)
    assert reader._pending is None and native.request_count == 1


def test_new_methods_are_owner_thread_only():
    reader, _, native = _setup()
    request = reader.begin_read(kind="orders", timeout=5)
    errors = []

    def outsider():
        for call in (lambda: reader.begin_read(kind="orders", timeout=5),
                     lambda: reader.poll_read(request),
                     lambda: reader.cancel_read(request)):
            try:
                call()
            except CtpGatewayReturnQueryError as error:
                errors.append(error.code)

    thread = threading.Thread(target=outsider)
    thread.start()
    thread.join(2)
    assert errors == ["OWNER_REQUIRED"] * 3 and native.request_count == 0
    assert reader._pending is not None and reader._pending.request is request


def test_read_begun_before_a_relogin_is_never_sent():
    reader, client, native = _setup()
    request = reader.begin_read(kind="orders", timeout=5)
    _login(client, native, session_id=20)
    with pytest.raises(CtpGatewayReturnQueryError, match="^SESSION_CHANGED$"):
        reader.poll_read(request)
    # No query of the old binding occupies the new session's lane.
    assert native.request_count == 0 and client._query_native_inflight is None
