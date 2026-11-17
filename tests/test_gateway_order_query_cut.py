"""Fake-only local order-query / Gateway return-buffer window evidence."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_ctp_callback_ingress_client import _FakeTraderApiFactory
from test_iter63_gateway_return_source import row

from bt_api_ctp import gateway_session as gateway_module
from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.gateway_session import CtpGatewaySessionError

pytest_plugins = ("test_iter63_gateway_session",)
_TRADER_START = TraderClient.start
_TRADER_STOP_AND_WAIT = TraderClient.stop_and_wait


def _open(prepared, **options):
    create, _, _, feeds, natives, _, _, _ = prepared
    session = create(**options)
    session.connect(generation="order-cut-test", timeout=1)
    trader, native = feeds[0].trader_client, natives[0]
    trader._query_interval = 0
    return session, trader, native


def _ack(session):
    buffer = session.return_buffer
    ticket = buffer.offer()
    buffer.ack(ticket)


def test_query_cut_pins_returns_across_normal_ack_and_binds_exact_result(prepared, monkeypatch):
    session, trader, native = _open(prepared)
    trader._spi.OnRtnOrder(row(OrderRef="before"))
    _ack(session)  # a pre-begin event is explicitly outside this local window

    def submit(_field, request_id):
        trader._spi.OnRtnOrder(row(OrderRef="in-window-1"))
        _ack(session)
        trader._spi.OnRtnOrder(row(OrderRef="in-window-2"))
        _ack(session)
        trader._spi.OnRspQryOrder(row(OrderRef="queried"), None, request_id, True)
        return 0

    monkeypatch.setattr(native, "ReqQryOrder", submit, raising=False)
    handle = session.begin_order_query_cut(timeout=1)
    assert session.return_buffer.status().last_sequence == 1
    result = session.poll_order_query_cut(handle)
    assert result is not None and result.complete
    assert session.return_buffer.status().record_count == 0
    cut = session.verify_order_query_cut(handle, result)
    assert cut is not None and (cut.begin_sequence, cut.end_sequence) == (1, 3)
    assert [record.sequence for record in cut.records] == [2, 3]
    assert not cut.authorizes_trading and not cut.venue_atomicity_proven
    copied = replace(result)
    object.__setattr__(copied, "_source", result.query_source)
    assert copied.query_source is result.query_source
    assert session.verify_order_query_cut(handle, copied) is None
    state = session._order_query_cut
    assert session.close_order_query_cut(handle)
    assert state.query_state.gateway_order_cut is None
    assert state.accumulator.gateway_order_cut is None and state.window is None
    assert session.verify_order_query_cut(handle, result) is None
    assert session.close()


def test_window_overflow_invalidates_only_the_explicit_pin(prepared, monkeypatch):
    session, trader, native = _open(prepared, return_buffer_capacity=1)

    def submit(_field, request_id):
        trader._spi.OnRtnOrder(row(OrderRef="first"))
        _ack(session)
        trader._spi.OnRtnOrder(row(OrderRef="second"))
        _ack(session)
        trader._spi.OnRspQryOrder(None, None, request_id, True)
        return 0

    monkeypatch.setattr(native, "ReqQryOrder", submit, raising=False)
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    assert result.complete and session.verify_order_query_cut(handle, result) is None
    assert not session.return_buffer.status().gap
    assert session.close_order_query_cut(handle) and session.close()


def test_buffer_gap_during_query_invalidates_cut(prepared, monkeypatch):
    session, trader, native = _open(prepared)

    def submit(_field, request_id):
        trader._spi.OnRtnOrder(row(Volume=[]))
        trader._spi.OnRspQryOrder(None, None, request_id, True)
        return 0

    monkeypatch.setattr(native, "ReqQryOrder", submit, raising=False)
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    assert result.complete and session.return_buffer.status().gap
    assert session.verify_order_query_cut(handle, result) is None
    assert session.close_order_query_cut(handle) and session.close()


def test_inflight_cancellation_retains_pin_until_terminal(prepared, monkeypatch):
    session, trader, native = _open(prepared)
    sent = []
    monkeypatch.setattr(
        native, "ReqQryOrder", lambda _field, rid: sent.append(rid) or 0, raising=False
    )
    handle = session.begin_order_query_cut(timeout=1)
    assert session.poll_order_query_cut(handle) is None
    assert sent and not session.close_order_query_cut(handle)
    assert session._order_query_cut is not None
    assert session._return_buffer._order_cut_window is not None
    assert not session.close()  # cannot discard an unknown in-flight native request
    trader._spi.OnRspQryOrder(None, None, sent[0], True)
    assert session.close_order_query_cut(handle)
    assert session.close()


def test_submission_rejection_even_after_synchronous_terminal_is_not_evidence(
    prepared, monkeypatch
):
    session, trader, native = _open(prepared)

    def submit(_field, request_id):
        trader._spi.OnRspQryOrder(None, None, request_id, True)
        return -1

    monkeypatch.setattr(native, "ReqQryOrder", submit, raising=False)
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    assert result is not None and session.verify_order_query_cut(handle, result) is None
    assert session.close_order_query_cut(handle) and session.close()


def test_session_change_and_query_expiry_are_unknown(prepared, monkeypatch):
    session, trader, native = _open(prepared)
    monkeypatch.setattr(
        native,
        "ReqQryOrder",
        lambda _field, rid: trader._spi.OnRspQryOrder(None, None, rid, True) or 0,
        raising=False,
    )
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    assert session.verify_order_query_cut(handle, result) is not None
    with monkeypatch.context() as patch:
        patch.setattr(gateway_module, "time", SimpleNamespace(monotonic=lambda: 10**12))
        assert session.verify_order_query_cut(handle, result) is None
    trader._spi.OnFrontDisconnected(1)
    assert session.verify_order_query_cut(handle, result) is None
    assert session.close_order_query_cut(handle) and session.close()


def test_mutated_raw_rows_and_wrong_scope_never_verify(prepared, monkeypatch):
    session, trader, native = _open(prepared)

    def submit(_field, request_id):
        trader._spi.OnRspQryOrder(row(OrderRef="queried"), None, request_id, True)
        return 0

    monkeypatch.setattr(native, "ReqQryOrder", submit, raising=False)
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    assert session.verify_order_query_cut(handle, result) is not None
    result.records[0]["OrderRef"] = "modified"
    assert session.verify_order_query_cut(handle, result) is None
    result.records[0]["OrderRef"] = "queried"
    with monkeypatch.context() as patch:
        patch.setattr(trader, "_query_evidence_issuer", object())
        assert session.verify_order_query_cut(handle, result) is None
    assert session.close_order_query_cut(handle) and session.close()


def test_failed_cancel_retains_original_window(prepared, monkeypatch):
    session, trader, native = _open(prepared)
    sent = []
    monkeypatch.setattr(
        native, "ReqQryOrder", lambda _field, rid: sent.append(rid) or 0, raising=False
    )
    handle = session.begin_order_query_cut(timeout=1)
    assert session.poll_order_query_cut(handle) is None
    window = session._return_buffer._order_cut_window
    with monkeypatch.context() as patch:

        def fail_cancel(_handle):
            raise RuntimeError("synthetic cancellation failure")

        patch.setattr(trader, "cancel_query", fail_cancel)
        assert not session.close_order_query_cut(handle)
        assert session._return_buffer._order_cut_window is window
        assert session.verify_order_query_cut(handle, object()) is None
    assert not session.close_order_query_cut(handle)
    trader._spi.OnRspQryOrder(None, None, sent[0], True)
    assert session.close_order_query_cut(handle) and session.close()


def test_invalid_scope_before_send_fails_without_native_query(prepared, monkeypatch):
    session, trader, native = _open(prepared)
    calls = []
    monkeypatch.setattr(native, "ReqQryOrder", lambda *_: calls.append(1) or 0, raising=False)
    handle = session.begin_order_query_cut(timeout=1)
    with monkeypatch.context() as patch:
        patch.setattr(trader, "_gateway_return_buffer", object())
        result = session.poll_order_query_cut(handle)
        assert result is not None and not result.complete and calls == []
        assert session.verify_order_query_cut(handle, result) is None
    assert session.close_order_query_cut(handle) and session.close()


def test_explicit_cut_owner_and_capacity_refuse_without_touching_other_history(prepared):
    import threading

    session, trader, _ = _open(prepared)
    trader._spi.OnRtnOrder(row(OrderRef="retained"))
    handle = session.begin_order_query_cut(timeout=1)
    with pytest.raises(CtpGatewaySessionError, match="^ORDER_CUT_UNAVAILABLE$"):
        session.begin_order_query_cut(timeout=1)
    errors = []

    def wrong_owner():
        for operation in (
            lambda: session.begin_order_query_cut(timeout=1),
            lambda: session.poll_order_query_cut(handle),
            lambda: session.verify_order_query_cut(handle, object()),
            lambda: session.close_order_query_cut(handle),
        ):
            try:
                operation()
            except CtpGatewaySessionError as exc:
                errors.append(exc.code)

    thread = threading.Thread(target=wrong_owner)
    thread.start()
    thread.join(1)
    assert errors == ["WRONG_OWNER"] * 4
    original_pid = session._owner_pid
    session._owner_pid += 1
    try:
        with pytest.raises(CtpGatewaySessionError, match="^WRONG_OWNER$"):
            session.verify_order_query_cut(handle, object())
        with pytest.raises(CtpGatewaySessionError, match="^WRONG_OWNER$"):
            session.close_order_query_cut(handle)
    finally:
        session._owner_pid = original_pid
    assert session.close_order_query_cut(handle)
    assert session.return_buffer.status().record_count == 1
    assert session.close()


def test_wrong_request_and_error_terminal_do_not_produce_cut(prepared, monkeypatch):
    session, trader, native = _open(prepared)
    sent = []
    monkeypatch.setattr(
        native, "ReqQryOrder", lambda _field, rid: sent.append(rid) or 0, raising=False
    )
    handle = session.begin_order_query_cut(timeout=1)
    assert session.poll_order_query_cut(handle) is None
    trader._spi.OnRspQryOrder(None, None, sent[0] + 1, True)
    assert session.poll_order_query_cut(handle) is None
    trader._spi.OnRspQryOrder(None, SimpleNamespace(ErrorID=7, ErrorMsg="synthetic"), sent[0], True)
    result = session.poll_order_query_cut(handle)
    assert not result.complete and session.verify_order_query_cut(handle, result) is None
    assert session.close_order_query_cut(handle) and session.close()


def test_modified_native_query_filters_cannot_certify_default_scope(prepared, monkeypatch):
    session, trader, native = _open(prepared)
    original = trader._nonblocking_request

    def filtered(kind):
        field, method, intent = original(kind)
        field.InstrumentID = "IF2612"
        intent["InstrumentID"] = "IF2612"
        return field, method, intent

    monkeypatch.setattr(trader, "_nonblocking_request", filtered)
    monkeypatch.setattr(
        native,
        "ReqQryOrder",
        lambda _field, rid: trader._spi.OnRspQryOrder(None, None, rid, True) or 0,
        raising=False,
    )
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    assert not result.complete and session.verify_order_query_cut(handle, result) is None
    assert session.close_order_query_cut(handle) and session.close()


def test_terminal_cut_is_atomic_against_native_return_callback(prepared, monkeypatch):
    import threading

    session, trader, native = _open(prepared)
    attempting, finished = threading.Event(), threading.Event()
    workers = []
    buffer = session._return_buffer
    finish = buffer._finish_order_cut_window

    def finish_with_racing_callback(window):
        def callback():
            attempting.set()
            trader._spi.OnRtnOrder(row(OrderRef="after-cut"))
            finished.set()

        worker = threading.Thread(target=callback)
        workers.append(worker)
        worker.start()
        assert attempting.wait(1)
        assert not finished.wait(0.1)
        return finish(window)

    monkeypatch.setattr(buffer, "_finish_order_cut_window", finish_with_racing_callback)
    monkeypatch.setattr(
        native,
        "ReqQryOrder",
        lambda _field, rid: trader._spi.OnRspQryOrder(None, None, rid, True) or 0,
        raising=False,
    )
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    workers[0].join(1)
    assert finished.is_set()
    cut = session.verify_order_query_cut(handle, result)
    assert cut is not None and cut.begin_sequence == cut.end_sequence == 0
    assert cut.records == () and buffer.status().last_sequence == 1
    assert session.close_order_query_cut(handle) and session.close()


def test_ticket_opened_before_window_does_not_erase_pinned_rows(prepared, monkeypatch):
    session, trader, native = _open(prepared)
    trader._spi.OnRtnOrder(row(OrderRef="older"))
    old_ticket = session.return_buffer.offer()

    def submit(_field, request_id):
        trader._spi.OnRtnOrder(row(OrderRef="during"))
        session.return_buffer.ack(old_ticket)
        _ack(session)
        trader._spi.OnRspQryOrder(None, None, request_id, True)
        return 0

    monkeypatch.setattr(native, "ReqQryOrder", submit, raising=False)
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    cut = session.verify_order_query_cut(handle, result)
    assert cut is not None and (cut.begin_sequence, cut.end_sequence) == (1, 2)
    assert [record.sequence for record in cut.records] == [2]
    assert session.return_buffer.status().record_count == 0
    assert session.close_order_query_cut(handle) and session.close()


def test_gap_after_terminal_also_revokes_window(prepared, monkeypatch):
    session, trader, native = _open(prepared)
    monkeypatch.setattr(
        native,
        "ReqQryOrder",
        lambda _field, rid: trader._spi.OnRspQryOrder(None, None, rid, True) or 0,
        raising=False,
    )
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    assert session.verify_order_query_cut(handle, result) is not None
    session._return_buffer._mark_gap("source_failure")
    assert session.verify_order_query_cut(handle, result) is None
    assert session.close_order_query_cut(handle) and session.close()


def test_preexisting_gap_refuses_cut_before_native_send(prepared, monkeypatch):
    session, trader, native = _open(prepared)
    session._return_buffer._mark_gap("source_failure")
    calls = []
    monkeypatch.setattr(native, "ReqQryOrder", lambda *_: calls.append(1) or 0, raising=False)
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    assert result is not None and not result.complete and calls == []
    assert session.verify_order_query_cut(handle, result) is None
    assert session.close_order_query_cut(handle) and session.close()


def test_cut_with_real_trader_start_and_synthetic_native(prepared, monkeypatch):
    """Use the production client registration and a fake, local native API."""
    _FakeTraderApiFactory.instances = []
    _FakeTraderApiFactory.block_join = False
    _FakeTraderApiFactory.register_spi_exception = None
    _FakeTraderApiFactory.init_exception = None
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "CThostFtdcTraderApi", _FakeTraderApiFactory)
    monkeypatch.setattr(TraderClient, "start", _TRADER_START)
    monkeypatch.setattr(TraderClient, "stop_and_wait", _TRADER_STOP_AND_WAIT)
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="order-cut-real-start-fake-native", timeout=1)
    trader = feeds[0].trader_client
    trader._query_interval = 0
    api = _FakeTraderApiFactory.instances[-1]

    def submit(_field, request_id):
        api.spi.OnRtnOrder(row(OrderRef="during"))
        _ack(session)
        api.spi.OnRspQryOrder(row(OrderRef="query"), None, request_id, True)
        return 0

    monkeypatch.setattr(api, "ReqQryOrder", submit, raising=False)
    try:
        handle = session.begin_order_query_cut(timeout=1)
        result = session.poll_order_query_cut(handle)
        cut = session.verify_order_query_cut(handle, result)
        assert cut is not None and (cut.begin_sequence, cut.end_sequence) == (0, 1)
        assert len(cut.records) == 1 and cut.records[0].kind == "order"
        assert session.close_order_query_cut(handle)
        assert {call[0] for call in api.calls} <= {"ReqAuthenticate", "ReqUserLogin"}
    finally:
        assert session.close()


@pytest.mark.parametrize("change", ["buffer", "spi_source", "generation", "account"])
def test_verifier_rejects_replaced_source_and_untrusted_query_scope(prepared, monkeypatch, change):
    session, trader, native = _open(prepared)
    monkeypatch.setattr(
        native,
        "ReqQryOrder",
        lambda _field, rid: trader._spi.OnRspQryOrder(None, None, rid, True) or 0,
        raising=False,
    )
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    assert session.verify_order_query_cut(handle, result) is not None
    with monkeypatch.context() as patch:
        if change == "buffer":
            patch.setattr(trader, "_gateway_return_buffer", object())
        elif change == "spi_source":
            patch.setattr(trader._spi, "_native_spi_source_id", "other")
        elif change == "generation":
            patch.setattr(trader, "_connection_generation", trader._connection_generation + 1)
        elif change == "account":
            patch.setattr(
                session, "_account_key", replace(session._account_key, account_id="other")
            )
        assert session.verify_order_query_cut(handle, result) is None
    assert session.close_order_query_cut(handle) and session.close()


def test_busy_close_and_release_failure_retain_original_pin(prepared, monkeypatch):
    import threading

    session, trader, native = _open(prepared)
    monkeypatch.setattr(
        native,
        "ReqQryOrder",
        lambda _field, rid: trader._spi.OnRspQryOrder(None, None, rid, True) or 0,
        raising=False,
    )
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    window = session._return_buffer._order_cut_window
    assert session.verify_order_query_cut(handle, result) is not None
    entered, release = threading.Event(), threading.Event()

    def hold():
        with trader._query_state_lock:
            entered.set()
            assert release.wait(3)

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(1)
    try:
        assert not session.close_order_query_cut(handle)
        assert session._return_buffer._order_cut_window is window
    finally:
        release.set()
        thread.join(1)
    assert session.verify_order_query_cut(handle, result) is None
    with monkeypatch.context() as patch:
        patch.setattr(session._return_buffer, "_close_order_cut_window", lambda _: False)
        assert not session.close_order_query_cut(handle)
        assert session._return_buffer._order_cut_window is window
    assert session.close_order_query_cut(handle) and session.close()


def test_malformed_poll_result_is_finite_and_never_evidence(prepared, monkeypatch):
    session, trader, _ = _open(prepared)
    handle = session.begin_order_query_cut(timeout=1)
    with monkeypatch.context() as patch:
        patch.setattr(trader, "poll_query", lambda _: object())
        with pytest.raises(CtpGatewaySessionError, match="^ORDER_CUT_UNAVAILABLE$"):
            session.poll_order_query_cut(handle)
        assert session.verify_order_query_cut(handle, object()) is None
    assert session.close_order_query_cut(handle) and session.close()


def test_transient_owner_flow_change_at_terminal_cannot_be_repaired_by_restore(
    prepared, monkeypatch
):
    session, trader, native = _open(prepared)
    feed = session._feed
    original_flow = feed._trusted_flow

    def submit(_field, request_id):
        feed._trusted_flow = object()
        try:
            trader._spi.OnRspQryOrder(None, None, request_id, True)
        finally:
            feed._trusted_flow = original_flow
        return 0

    monkeypatch.setattr(native, "ReqQryOrder", submit, raising=False)
    handle = session.begin_order_query_cut(timeout=1)
    result = session.poll_order_query_cut(handle)
    assert result.complete and session.connection_status() == "ready"
    assert session.verify_order_query_cut(handle, result) is None
    assert session.close_order_query_cut(handle) and session.close()
