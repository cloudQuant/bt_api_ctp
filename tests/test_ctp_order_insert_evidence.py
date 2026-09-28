from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import TraderClient, _TraderSpi


class _FakeTraderApi:
    def __init__(self, submit_code=0, submit_error=None):
        self.submit_code = submit_code
        self.submit_error = submit_error
        self.fields = []
        self.request_ids = []

    def ReqOrderInsert(self, field, request_id):
        self.fields.append(field)
        self.request_ids.append(request_id)
        if self.submit_error is not None:
            raise self.submit_error
        return self.submit_code


def _field(**changes):
    values = {
        "BrokerID": "9999",
        "InvestorID": "sim-account",
        "UserID": "sim-account",
        "InstrumentID": "SA2701",
        "ExchangeID": "CZCE",
        "OrderRef": "000000000105",
        "RequestID": 17,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _client(submit_code=0, submit_error=None):
    client = TraderClient("tcp://test", "9999", "sim-account", "secret")
    client._connection_generation = 4
    client._trading_day = "20260923"
    client._api = _FakeTraderApi(submit_code=submit_code, submit_error=submit_error)
    # These guards are tested separately. This file isolates request callback
    # evidence after a test caller has crossed the write gate.
    client._require_execution_write_locked = lambda *_args, **_kwargs: None
    client._require_native_field_identity_locked = lambda *_args, **_kwargs: None
    return client


def _submit(client, *, field=None, request_id=17):
    result = client.submit_order_insert(field or _field(), request_id)
    assert result == client._api.submit_code
    return client.get_order_insert_evidence(request_id, order_ref="000000000105")


def _rsp_info(error_id=0, error_msg=""):
    return SimpleNamespace(ErrorID=error_id, ErrorMsg=error_msg)


def test_native_submit_code_without_matching_callback_stays_unknown():
    client = _client(submit_code=0)

    evidence = _submit(client)

    assert evidence.status == "unknown"
    assert evidence.evidence_received is False
    assert evidence.reason == "awaiting_native_callback"
    assert evidence.submit_code == 0
    assert evidence.account_fingerprint == f"acct_{client._account_fingerprint}"
    assert evidence.trading_day == "20260923"
    assert evidence.connection_generation == 4
    assert evidence.request_id == 17
    assert evidence.order_ref == "000000000105"
    assert client._api.request_ids == [17]


def test_exact_successful_rsp_callback_records_request_ack_and_preserves_event_queue():
    client = _client()
    _submit(client)

    _TraderSpi(client).OnRspOrderInsert(_field(), _rsp_info(), 17, True)

    evidence = client.get_order_insert_evidence(17, order_ref="000000000105")
    assert evidence.status == "accepted"
    assert evidence.evidence_source == "OnRspOrderInsert"
    assert evidence.callback_received is True
    assert evidence.evidence_received is True
    assert evidence.reason == "order_insert_request_accepted"
    assert client._account_fingerprint not in repr(evidence)
    assert "sim-account" not in repr(evidence)
    assert evidence.as_dict()["account_fingerprint"] == "<redacted>"
    assert evidence.as_dict()["error_message"] == ""
    with pytest.raises(FrozenInstanceError):
        evidence.status = "filled"

    event = client.wait_error_event(timeout=0.01)
    assert event["event"] == "order_insert_response"
    assert event["request_id"] == 17
    assert event["field"]["OrderRef"] == "000000000105"


@pytest.mark.parametrize(
    "changed",
    [
        {"BrokerID": "other-broker"},
        {"InvestorID": "other-account"},
        {"UserID": "other-user"},
        {"InstrumentID": "IF2701"},
        {"ExchangeID": "SHFE"},
        {"OrderRef": "000000000106"},
        {"RequestID": 18},
    ],
)
def test_mismatched_insert_callback_cannot_resolve_request(changed):
    client = _client()
    _submit(client)

    _TraderSpi(client).OnRspOrderInsert(_field(**changed), _rsp_info(), 17, True)

    evidence = client.get_order_insert_evidence(17, order_ref="000000000105")
    assert evidence.status == "unknown"
    assert evidence.evidence_received is False
    if changed.get("OrderRef") != "000000000106":
        assert evidence.reason in {
            "callback_identity_mismatch",
            "native_request_id_mismatch",
        }


def test_insert_rsp_error_and_error_return_reject_only_the_exact_request():
    client = _client()
    _submit(client)
    spi = _TraderSpi(client)

    spi.OnRspOrderInsert(_field(), _rsp_info(32, "insert rejected"), 17, True)
    evidence = client.get_order_insert_evidence(17, order_ref="000000000105")
    assert evidence.status == "rejected"
    assert evidence.error_code == 32
    assert evidence.error_message == "insert rejected"
    assert evidence.evidence_source == "OnRspOrderInsert"
    assert evidence.as_dict()["error_message"] == ""

    client = _client()
    _submit(client)
    _TraderSpi(client).OnErrRtnOrderInsert(
        _field(), _rsp_info(33, "exchange rejected insert")
    )
    evidence = client.get_order_insert_evidence(17, order_ref="000000000105")
    assert evidence.status == "rejected"
    assert evidence.error_code == 33
    assert evidence.evidence_source == "OnErrRtnOrderInsert"
    assert evidence.reason == "native_order_insert_rejected"


def test_stale_session_and_nonterminal_rsp_cannot_confirm_insert():
    client = _client()
    _submit(client)
    spi = _TraderSpi(client)

    spi.OnRspOrderInsert(_field(), _rsp_info(), 17, False)
    evidence = client.get_order_insert_evidence(17, order_ref="000000000105")
    assert evidence.status == "unknown"
    assert evidence.reason == "native_response_not_terminal"

    client._trading_day = "20260924"
    spi.OnRspOrderInsert(_field(), _rsp_info(), 17, True)
    evidence = client.get_order_insert_evidence(17, order_ref="000000000105")
    assert evidence.status == "unknown"
    assert evidence.evidence_received is False
    assert evidence.reason == "callback_session_scope_mismatch"

    client._trading_day = "20260923"
    client._connection_generation += 1
    spi.OnRspOrderInsert(_field(), _rsp_info(), 17, True)
    evidence = client.get_order_insert_evidence(17, order_ref="000000000105")
    assert evidence.status == "unknown"
    assert evidence.reason == "callback_session_scope_mismatch"


@pytest.mark.parametrize("callback_name", ["OnRspOrderInsert", "OnErrRtnOrderInsert"])
def test_insert_callback_rechecks_origin_after_api_swap(monkeypatch, callback_name):
    client = _client()
    _submit(client)
    original_api = client._native_api
    spi = _TraderSpi(client, original_api)
    with client._query_state_lock:
        client._spi = spi

    handler_name = (
        "_handle_order_insert_response"
        if callback_name == "OnRspOrderInsert"
        else "_handle_order_insert_error"
    )
    original_handler = getattr(client, handler_name)
    handler_entered = threading.Event()
    resume_handler = threading.Event()

    def pause_before_record(*args, **kwargs):
        handler_entered.set()
        if not resume_handler.wait(timeout=5):
            raise AssertionError("test did not resume the callback handler")
        return original_handler(*args, **kwargs)

    monkeypatch.setattr(client, handler_name, pause_before_record)
    callback_args = (
        (_field(), _rsp_info(), 17, True)
        if callback_name == "OnRspOrderInsert"
        else (_field(), _rsp_info(33, "stale insert rejection"))
    )
    callback_errors = []

    def invoke_callback():
        try:
            getattr(spi, callback_name)(*callback_args)
        except BaseException as exc:  # surfaced in the main test thread
            callback_errors.append(exc)

    callback_thread = threading.Thread(target=invoke_callback)
    callback_thread.start()
    if not handler_entered.wait(timeout=5):
        resume_handler.set()
        callback_thread.join(timeout=5)
        pytest.fail("callback did not reach the deterministic race window")

    client._api = _FakeTraderApi()
    resume_handler.set()
    callback_thread.join(timeout=5)

    assert not callback_thread.is_alive()
    assert callback_errors == []
    evidence = client.get_order_insert_evidence(17, order_ref="000000000105")
    assert evidence.status == "unknown"
    assert evidence.callback_received is False
    assert evidence.evidence_received is False


def test_order_status_callback_does_not_promote_missing_insert_response():
    client = _client()
    _submit(client)

    _TraderSpi(client).OnRtnOrder(
        SimpleNamespace(OrderRef="000000000105", OrderSysID="SYS105", OrderStatus="3")
    )

    evidence = client.get_order_insert_evidence(17, order_ref="000000000105")
    assert evidence.status == "unknown"
    assert evidence.evidence_received is False


def test_reused_insert_request_identity_is_rejected_before_second_native_call():
    client = _client()
    _submit(client)

    with pytest.raises(RuntimeError, match="order_insert_request_identity_reused"):
        client.submit_order_insert(_field(), 17)

    assert client.get_request_counts()["order_insert"] == 1
    assert client._api.request_ids == [17]


@pytest.mark.parametrize(
    ("field_changes", "request_id", "generation", "trading_day", "error_code"),
    [
        ({"RequestID": 0}, 0, 4, "20260923", "order_insert_request_id_invalid"),
        ({"RequestID": 1}, True, 4, "20260923", "order_insert_request_id_invalid"),
        (
            {"RequestID": True},
            1,
            4,
            "20260923",
            "order_insert_field_request_id_invalid",
        ),
        (
            {"RequestID": 18},
            17,
            4,
            "20260923",
            "order_insert_field_request_id_mismatch",
        ),
        ({"OrderRef": ""}, 17, 4, "20260923", "order_insert_order_ref_missing"),
        (
            {"InstrumentID": ""},
            17,
            4,
            "20260923",
            "order_insert_native_identity_incomplete",
        ),
        (
            {"ExchangeID": ""},
            17,
            4,
            "20260923",
            "order_insert_native_identity_incomplete",
        ),
        (
            {"BrokerID": ""},
            17,
            4,
            "20260923",
            "order_insert_native_identity_incomplete",
        ),
        (
            {"InvestorID": ""},
            17,
            4,
            "20260923",
            "order_insert_native_identity_incomplete",
        ),
        (
            {"UserID": ""},
            17,
            4,
            "20260923",
            "order_insert_native_identity_incomplete",
        ),
        ({}, 17, 0, "20260923", "order_insert_session_identity_incomplete"),
        ({}, 17, 4, "2026092", "order_insert_session_identity_incomplete"),
    ],
)
def test_malformed_insert_request_is_rejected_before_native_dispatch(
    field_changes, request_id, generation, trading_day, error_code
):
    client = _client()
    client._connection_generation = generation
    client._trading_day = trading_day

    with pytest.raises(RuntimeError, match=error_code):
        client.submit_order_insert(_field(**field_changes), request_id)

    assert client._api.request_ids == []
    assert client.get_request_counts()["order_insert"] == 0


def test_native_submit_exception_keeps_valid_dispatch_unknown():
    client = _client(submit_error=RuntimeError("native dispatch failed"))

    with pytest.raises(RuntimeError, match="native dispatch failed"):
        client.submit_order_insert(_field(), 17)

    evidence = client.get_order_insert_evidence(17, order_ref="000000000105")
    assert evidence.status == "unknown"
    assert evidence.reason == "native_submit_exception"
    assert evidence.submit_code is None
    assert client._api.request_ids == [17]
    assert client.get_request_counts()["order_insert"] == 1


def test_evidence_lookup_requires_unique_request_when_order_ref_is_omitted():
    client = _client()
    _submit(client)

    with pytest.raises(RuntimeError, match="order_insert_request_id_reused"):
        client.submit_order_insert(_field(OrderRef="000000000106"), 17)

    assert client.get_order_insert_evidence(17) is not None
    assert client.get_order_insert_evidence(17, order_ref="000000000105") is not None
    assert client.get_order_insert_evidence(17, order_ref="000000000106") is None
    assert client._api.request_ids == [17]
    assert client.get_request_counts()["order_insert"] == 1
