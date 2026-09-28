from __future__ import annotations

import hashlib
import json
from threading import RLock
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.managed_outbox_pre_dispatch import (
    CtpOutboxDispatchAuthorityRequired,
    CtpOutboxPreDispatchError,
    prepare_ctp_managed_native_outbox_candidate,
)


def _sha256_json(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _scope():
    return SimpleNamespace(
        account_key="account:" + "b" * 64,
        key="scope:" + "a" * 64,
        trading_day="20260925",
    )


def _insert_fields(order_ref="000000000013"):
    return {
        "BrokerID": "broker",
        "InvestorID": "investor",
        "UserID": "investor",
        "InstrumentID": "rb2710",
        "ExchangeID": "SHFE",
        "OrderRef": order_ref,
        "RequestID": 701,
        "Direction": "0",
        "CombOffsetFlag": "0",
        "CombHedgeFlag": "1",
        "OrderPriceType": "2",
        "TimeCondition": "3",
        "VolumeCondition": "1",
        "LimitPrice": 3510.5,
        "VolumeTotalOriginal": 1,
    }


def _cancel_fields(order_ref="000000000013"):
    return {
        "BrokerID": "broker",
        "InvestorID": "investor",
        "InstrumentID": "rb2710",
        "ExchangeID": "SHFE",
        "OrderRef": order_ref,
        "OrderSysID": "native-order-13",
        "FrontID": 4,
        "SessionID": 91,
        "RequestID": 702,
        "OrderActionRef": 702,
        "ActionFlag": "0",
        "LimitPrice": 0.0,
        "VolumeChange": 0,
    }


class _FakeStore:
    def __init__(self, command, reservation):
        self.command = command
        self.reservation = reservation
        self.claim_calls = 0

    def assert_writer_lease(self, scope, writer_lease):
        assert writer_lease == "current-writer-lease"

    def read_ctp_dispatch_command(self, scope, command_id):
        assert command_id == self.command.command_id
        return self.command

    def read_ctp_order_identity(self, scope, managed_intent_id):
        assert scope.key == self.reservation.scope_key
        assert managed_intent_id == self.reservation.managed_intent_id
        return self.reservation

    def claim_ctp_dispatch_command(self, *args, **kwargs):
        self.claim_calls += 1
        raise AssertionError("pre-dispatch boundary must not claim commands")


class _FakeTrader:
    def __init__(self):
        self._query_state_lock = RLock()
        self._account_fingerprint = "a" * 16
        self._trading_day = "20260925"
        self._connection_generation = 8
        self.native_calls = []

    def submit_order_insert(self, *args, **kwargs):
        self.native_calls.append((args, kwargs))
        raise AssertionError("pre-dispatch boundary must not submit native calls")

    def submit_order_action(self, *args, **kwargs):
        self.native_calls.append((args, kwargs))
        raise AssertionError("pre-dispatch boundary must not submit native calls")


def _command(scope, fields, *, operation="SUBMIT", status="READY", command_id="cmd:entry-1"):
    binding = {"session_identity": "opaque-session-v1", "config_digest": "c" * 64}
    reservation = SimpleNamespace(
        account_key=scope.account_key,
        scope_key=scope.key,
        trading_day=scope.trading_day,
        managed_intent_id="managed-entry-1",
        runtime_order_id="bt-managed-v1:" + "d" * 64,
        order_ref="000000000013",
    )
    command = SimpleNamespace(
        account_key=scope.account_key,
        scope_key=scope.key,
        trading_day=scope.trading_day,
        operation=operation,
        command_id=command_id,
        request_payload=dict(fields),
        request_payload_sha256=_sha256_json(fields),
        reservation_managed_intent_id=reservation.managed_intent_id,
        order_ref=reservation.order_ref if operation == "SUBMIT" else None,
        cancel_target_order_ref=reservation.order_ref if operation == "CANCEL" else None,
        cancel_target_exchange_id="SHFE" if operation == "CANCEL" else None,
        cancel_target_order_sys_id="native-order-13" if operation == "CANCEL" else None,
        cancel_target_front_id=4 if operation == "CANCEL" else None,
        cancel_target_session_id=91 if operation == "CANCEL" else None,
        approval_use_id="approval-use-" + command_id,
        approval_digest="e" * 64,
        session_binding=binding,
        session_binding_sha256=_sha256_json(binding),
        status=status,
        completed_at_ns=None,
        unknown_at_ns=None,
        native_receipt_payload=None,
        native_receipt_sha256=None,
        completion_echo_sha256=None,
    )
    return command, reservation


def _prepare(store, scope, trader, fields, command_id="cmd:entry-1"):
    return prepare_ctp_managed_native_outbox_candidate(
        store,
        scope,
        command_id,
        trader,
        fields,
        writer_lease="current-writer-lease",
    )


@pytest.mark.unit
def test_insert_candidate_preserves_scope_orderref_and_source_digests_without_authorizing():
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields)
    store = _FakeStore(command, reservation)
    trader = _FakeTrader()

    candidate = _prepare(store, scope, trader, fields)

    assert candidate.authorizes_dispatch is False
    assert candidate.request.operation == "insert"
    assert candidate.request.scope_digest_sha256 == "a" * 64
    assert candidate.request.order_ref == "000000000013"
    assert candidate.request.managed_intent_id == reservation.managed_intent_id
    assert candidate.request.runtime_order_id == reservation.runtime_order_id
    assert candidate.request.parent_handoff_digest_sha256 == candidate.parent_handoff_digest_sha256
    assert candidate.approval_use_id == command.approval_use_id
    assert candidate.approval_digest == command.approval_digest
    assert candidate.session_binding_sha256 == command.session_binding_sha256
    with pytest.raises(CtpOutboxDispatchAuthorityRequired, match="no reviewed native verifier"):
        candidate.require_dispatch_authority()
    assert store.claim_calls == 0
    assert trader.native_calls == []


@pytest.mark.unit
def test_cancel_candidate_preserves_command_action_id_and_exact_native_target():
    scope = _scope()
    fields = _cancel_fields()
    command, reservation = _command(scope, fields, operation="CANCEL", command_id="cancel:13:1")
    store = _FakeStore(command, reservation)
    trader = _FakeTrader()

    candidate = _prepare(store, scope, trader, fields, command.command_id)

    assert candidate.request.operation == "cancel"
    assert candidate.request.runtime_action_id == command.command_id
    assert candidate.request.managed_cancel_intent_id == command.command_id
    assert candidate.request.native_action_ref == "702"
    assert candidate.request.target_order_sys_id == "native-order-13"
    assert candidate.request.target_exchange_id == "SHFE"
    assert candidate.request.target_front_id == 4
    assert candidate.request.target_session_id == 91
    assert candidate.request.order_ref == reservation.order_ref
    assert store.claim_calls == 0
    assert trader.native_calls == []


@pytest.mark.unit
def test_missing_verifier_stops_before_claim_and_native_submit():
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields)
    store = _FakeStore(command, reservation)
    trader = _FakeTrader()

    candidate = _prepare(store, scope, trader, fields)
    with pytest.raises(CtpOutboxDispatchAuthorityRequired, match="no reviewed native verifier"):
        candidate.require_dispatch_authority()

    assert store.claim_calls == 0
    assert trader.native_calls == []


@pytest.mark.unit
@pytest.mark.parametrize("status", ["CLAIMED", "UNKNOWN", "COMPLETED"])
def test_claimed_unknown_and_completed_commands_are_never_replayed(status):
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields, status=status)
    store = _FakeStore(command, reservation)
    trader = _FakeTrader()

    with pytest.raises(CtpOutboxPreDispatchError, match="only a READY staged"):
        _prepare(store, scope, trader, fields)

    assert store.claim_calls == 0
    assert trader.native_calls == []


@pytest.mark.unit
def test_scope_payload_and_cancel_target_mismatches_reject_before_any_write():
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields)
    trader = _FakeTrader()

    other_scope = SimpleNamespace(
        account_key=scope.account_key,
        key="scope:" + "f" * 64,
        trading_day=scope.trading_day,
    )
    with pytest.raises(CtpOutboxPreDispatchError, match="config-derived scope"):
        _prepare(_FakeStore(command, reservation), other_scope, trader, fields)

    with pytest.raises(CtpOutboxPreDispatchError, match="exactly match"):
        _prepare(_FakeStore(command, reservation), scope, trader, _insert_fields("000000000014"))

    cancel_fields = _cancel_fields()
    cancel, cancel_reservation = _command(scope, cancel_fields, operation="CANCEL")
    cancel.cancel_target_session_id = 92
    with pytest.raises(CtpOutboxPreDispatchError, match="exact target"):
        _prepare(
            _FakeStore(cancel, cancel_reservation),
            scope,
            trader,
            cancel_fields,
            cancel.command_id,
        )
    assert trader.native_calls == []


@pytest.mark.unit
def test_stale_writer_lease_fails_before_read_or_native_dispatch():
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields)
    store = _FakeStore(command, reservation)
    store.assert_writer_lease = lambda *_args: (_ for _ in ()).throw(RuntimeError("stale lease"))
    trader = _FakeTrader()

    with pytest.raises(RuntimeError, match="stale lease"):
        _prepare(store, scope, trader, fields)

    assert trader.native_calls == []


@pytest.mark.unit
def test_approval_and_source_evidence_are_bound_into_parent_handoff_digest():
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields)
    trader = _FakeTrader()
    first = _prepare(_FakeStore(command, reservation), scope, trader, fields)

    changed_values = vars(command).copy()
    changed_values["approval_digest"] = "f" * 64
    changed = SimpleNamespace(**changed_values)
    second = _prepare(_FakeStore(changed, reservation), scope, trader, fields)

    assert first.parent_handoff_digest_sha256 != second.parent_handoff_digest_sha256
    assert first.request.parent_handoff_digest_sha256 == first.parent_handoff_digest_sha256
    assert second.request.parent_handoff_digest_sha256 == second.parent_handoff_digest_sha256
    assert trader.native_calls == []
