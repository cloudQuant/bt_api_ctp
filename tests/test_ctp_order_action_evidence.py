from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import TraderClient, _TraderSpi


class _FakeTraderApi:
    def __init__(self, submit_code=0):
        self.submit_code = submit_code
        self.field = None
        self.request_id = None

    def ReqOrderAction(self, field, request_id):
        self.field = field
        self.request_id = request_id
        return self.submit_code


def _identity(**changes):
    values = {
        "BrokerID": "9999",
        "InvestorID": "sim-account",
        "OrderActionRef": "41",
        "OrderRef": "105",
        "RequestID": 17,
        "FrontID": 3,
        "SessionID": 8,
        "ExchangeID": "CZCE",
        "OrderSysID": "",
        "ActionFlag": "0",
        "InstrumentID": "SA2701",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _client(submit_code=0):
    client = TraderClient("tcp://test", "9999", "sim-account", "secret")
    client._connection_generation = 4
    client._trading_day = "20260923"
    client._api = _FakeTraderApi(submit_code=submit_code)
    # These two guards are tested elsewhere; this fixture exercises only the
    # native callback correlation after the caller has crossed the write gate.
    client._require_execution_write_locked = lambda *_args, **_kwargs: None
    client._require_native_field_identity_locked = lambda *_args, **_kwargs: None
    return client


def _submit(client):
    result = client.submit_order_action(_identity(), 17)
    assert result == client._api.submit_code
    return client.get_order_action_evidence(17, order_action_ref="41")


def test_submit_code_without_native_callback_stays_unknown():
    client = _client(submit_code=0)

    evidence = _submit(client)

    assert evidence.status == "unknown"
    assert evidence.evidence_received is False
    assert evidence.reason == "awaiting_native_callback"
    assert evidence.account_fingerprint == f"acct_{client._account_fingerprint}"
    assert evidence.trading_day == "20260923"
    assert evidence.connection_generation == 4
    assert evidence.request_id == 17
    assert evidence.order_action_ref == "41"
    assert evidence.order_ref == "105"
    assert evidence.front_id == 3
    assert evidence.session_id == 8


def test_exact_successful_rsp_callback_records_request_acceptance_only():
    client = _client()
    _submit(client)

    _TraderSpi(client).OnRspOrderAction(
        _identity(),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        17,
        True,
    )

    evidence = client.get_order_action_evidence(17, order_action_ref="41")
    assert evidence.status == "accepted"
    assert evidence.evidence_source == "OnRspOrderAction"
    assert evidence.callback_received is True
    assert evidence.evidence_received is True
    assert evidence.reason == "cancel_request_accepted"
    assert evidence.account_fingerprint not in repr(evidence)
    assert evidence.as_dict()["account_fingerprint"] == "<redacted>"
    with pytest.raises(FrozenInstanceError):
        evidence.status = "cancelled"


@pytest.mark.parametrize(
    "changed",
    [
        {"OrderActionRef": "42"},
        {"RequestID": 18},
        {"BrokerID": "other-broker"},
        {"InvestorID": "other-account"},
        {"OrderRef": "106"},
        {"FrontID": 4},
        {"SessionID": 9},
        {"InstrumentID": "IF2701"},
    ],
)
def test_mismatched_rsp_callback_cannot_resolve_cancel(changed):
    client = _client()
    _submit(client)

    _TraderSpi(client).OnRspOrderAction(
        _identity(**changed),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        17,
        True,
    )

    evidence = client.get_order_action_evidence(17, order_action_ref="41")
    assert evidence.status == "unknown"
    assert evidence.evidence_received is False
    assert evidence.reason == "callback_identity_mismatch"


def test_error_rsp_and_error_return_record_rejection_for_the_exact_action():
    client = _client()
    _submit(client)
    spi = _TraderSpi(client)

    spi.OnRspOrderAction(
        _identity(),
        SimpleNamespace(ErrorID=32, ErrorMsg="action rejected"),
        17,
        True,
    )
    evidence = client.get_order_action_evidence(17, order_action_ref="41")
    assert evidence.status == "rejected"
    assert evidence.error_code == 32
    assert evidence.evidence_source == "OnRspOrderAction"

    client = _client()
    _submit(client)
    spi = _TraderSpi(client)
    spi.OnRspOrderAction(
        _identity(),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        17,
        True,
    )
    spi.OnErrRtnOrderAction(
        _identity(),
        SimpleNamespace(ErrorID=33, ErrorMsg="exchange rejected action"),
    )
    evidence = client.get_order_action_evidence(17, order_action_ref="41")
    assert evidence.status == "rejected"
    assert evidence.error_code == 33
    assert evidence.evidence_source == "OnErrRtnOrderAction"
    assert evidence.error_message == "exchange rejected action"
    assert evidence.as_dict()["error_message"] == ""


def test_generation_trading_day_and_outer_request_id_are_part_of_the_match():
    client = _client()
    _submit(client)
    spi = _TraderSpi(client)

    spi.OnRspOrderAction(
        _identity(),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        18,
        True,
    )
    evidence = client.get_order_action_evidence(17, order_action_ref="41")
    assert evidence.status == "unknown"

    client._trading_day = "20260924"
    spi.OnRspOrderAction(
        _identity(),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        17,
        True,
    )
    evidence = client.get_order_action_evidence(17, order_action_ref="41")
    assert evidence.status == "unknown"
    assert evidence.reason == "callback_session_scope_mismatch"

    client._trading_day = "20260923"
    client._connection_generation += 1
    spi.OnRspOrderAction(
        _identity(),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        17,
        True,
    )
    evidence = client.get_order_action_evidence(17, order_action_ref="41")
    assert evidence.status == "unknown"
    assert evidence.reason == "callback_session_scope_mismatch"


@pytest.mark.parametrize("callback_name", ["OnRspOrderAction", "OnErrRtnOrderAction"])
def test_cancel_callback_rechecks_origin_after_api_swap(monkeypatch, callback_name):
    client = _client()
    _submit(client)
    original_api = client._native_api
    spi = _TraderSpi(client, original_api)
    with client._query_state_lock:
        client._spi = spi

    handler_name = (
        "_handle_order_action_response"
        if callback_name == "OnRspOrderAction"
        else "_handle_order_action_error"
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
        (_identity(), SimpleNamespace(ErrorID=0, ErrorMsg=""), 17, True)
        if callback_name == "OnRspOrderAction"
        else (_identity(), SimpleNamespace(ErrorID=33, ErrorMsg="stale cancel rejection"))
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
    evidence = client.get_order_action_evidence(17, order_action_ref="41")
    assert evidence.status == "unknown"
    assert evidence.callback_received is False
    assert evidence.evidence_received is False


def test_order_status_never_promotes_missing_cancel_callback():
    client = _client()
    _submit(client)

    _TraderSpi(client).OnRtnOrder(
        SimpleNamespace(OrderRef="105", OrderSysID="SYS105", OrderStatus="5")
    )

    evidence = client.get_order_action_evidence(17, order_action_ref="41")
    assert evidence.status == "unknown"
    assert evidence.evidence_received is False


def test_reused_request_and_action_identity_is_rejected_before_a_second_submit():
    client = _client()
    _submit(client)

    with pytest.raises(RuntimeError, match="cancel_request_identity_reused"):
        client.submit_order_action(_identity(), 17)

    with pytest.raises(RuntimeError, match="cancel_request_id_reused"):
        client.submit_order_action(_identity(OrderActionRef="42"), 17)

    assert client.get_request_counts()["order_action"] == 1


def test_live_feed_cancel_result_carries_both_request_ids_and_unknown_evidence():
    from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

    submitted = {}

    class _FeedTrader:
        _front_id = 3
        _session_id = 8

        def _next_request_id(self):
            return 17

        def submit_order_action(self, field, request_id, **_kwargs):
            submitted["field"] = field
            submitted["request_id"] = request_id
            return 0

        def get_order_action_evidence(self, request_id, *, order_action_ref):
            return None

    feed = CtpRequestDataFuture(broker_id="9999", user_id="sim-account")
    feed._trader = _FeedTrader()
    feed._ensure_execution_permitted = lambda *_args, **_kwargs: None
    feed._ensure_trading_ready = lambda: None

    response = feed.cancel_order(
        "SA2701",
        order_id="SYS105",
        exchange_id="CZCE",
    )

    assert submitted["request_id"] == 17
    assert submitted["field"].RequestID == 17
    assert submitted["field"].OrderActionRef == 17
    extra = response.get_extra_data()["ctp_cancel"]
    assert extra["request_id"] == 17
    assert extra["order_action_ref"] == "17"
    assert extra["evidence"]["status"] == "unknown"
    assert extra["evidence"]["evidence_received"] is False
