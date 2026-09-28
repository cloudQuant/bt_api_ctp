from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from threading import RLock
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.managed_outbox_pre_dispatch import (
    CtpOutboxDispatchAuthorityRequired,
    CtpOutboxPreDispatchError,
)
from bt_api_ctp.ctp.managed_outbox_verification import (
    CtpFreshActionUseReceiptV1,
    verify_and_claim_ctp_managed_native_outbox_action,
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


class _FakeTrader:
    def __init__(self):
        self._query_state_lock = RLock()
        self._account_fingerprint = "a" * 16
        self._trading_day = "20260925"
        self._connection_generation = 8
        self._login_connection_generation = 8
        self._front_id = 4
        self._session_id = 91
        self.native_calls = []

    def submit_order_insert(self, *args, **kwargs):
        self.native_calls.append((args, kwargs))
        raise AssertionError("verification boundary must not submit native calls")

    def submit_order_action(self, *args, **kwargs):
        self.native_calls.append((args, kwargs))
        raise AssertionError("verification boundary must not submit native calls")


class _FakeAtomicStore:
    """Small fake for the public read/atomic-claim interface; no provider I/O."""

    def __init__(self, command, reservation, *, claim_mode="claim"):
        self.command = command
        self.reservation = reservation
        self.claim_mode = claim_mode
        self.claim_calls = 0
        self._lock = RLock()

    def assert_writer_lease(self, scope, writer_lease):
        assert scope.account_key == self.command.account_key
        assert writer_lease.owner_id == "writer-1"
        assert writer_lease.fencing_token == 3

    def read_ctp_dispatch_command(self, scope, command_id):
        assert scope.key == self.command.scope_key
        assert command_id == self.command.command_id
        with self._lock:
            return self.command

    def read_ctp_order_identity(self, scope, managed_intent_id):
        assert scope.key == self.reservation.scope_key
        assert managed_intent_id == self.reservation.managed_intent_id
        return self.reservation

    def claim_ctp_dispatch_command(self, scope, command_id, *, writer_lease):
        with self._lock:
            self.claim_calls += 1
            if self.claim_mode == "lost-race":
                self.command.status = "CLAIMED"
                self.command.claimed_at_ns = 1
                self.command.claimed_owner_id = "other-writer"
                self.command.claimed_fencing_token = 99
                return None
            if self.command.status != "READY":
                return None
            self.command.status = "CLAIMED"
            self.command.claimed_at_ns = 1
            self.command.claimed_owner_id = writer_lease.owner_id
            self.command.claimed_fencing_token = writer_lease.fencing_token
            if self.claim_mode == "wrong-echo":
                return SimpleNamespace(**{**vars(self.command), "approval_digest": "f" * 64})
            return self.command


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
        created_at_ns=1,
        updated_at_ns=1,
        claimed_at_ns=None,
        claimed_owner_id=None,
        claimed_fencing_token=None,
        completed_at_ns=None,
        unknown_at_ns=None,
        unknown_reason=None,
        native_receipt_payload=None,
        native_receipt_sha256=None,
        completion_echo_sha256=None,
    )
    return command, reservation


class _FakeVerifier:
    def __init__(self, *, source_digest="f" * 64, error=None, receipt_changes=None):
        self.source_digest = source_digest
        self.error = error
        self.receipt_changes = receipt_changes or {}
        self.calls = []
        self._used = False
        self._lock = RLock()

    def verify_and_consume(self, request):
        with self._lock:
            self.calls.append(request)
            if self._used:
                raise RuntimeError("approval use already consumed")
            if self.error is not None:
                raise self.error
            self._used = True
        command = request.durable_command
        session = request.current_session
        receipt = CtpFreshActionUseReceiptV1(
            action_binding_sha256=request.action_binding_sha256,
            source_binding_sha256=self.source_digest,
            approval_use_id=command["approval_use_id"],
            approval_digest=command["approval_digest"],
            session_binding_sha256=command["session_binding_sha256"],
            scope_digest_sha256=request.candidate.request.scope_digest_sha256,
            account_fingerprint_sha256=session.account_fingerprint_sha256,
            trading_day=session.trading_day,
            connection_generation=session.connection_generation,
            front_id=session.front_id,
            session_id=session.session_id,
            consumption_id="use-once-" + command["command_id"],
        )
        return replace(receipt, **self.receipt_changes)


def _lease():
    return SimpleNamespace(scope_key="account:" + "b" * 64, owner_id="writer-1", fencing_token=3)


def _verify(store, scope, trader, fields, verifier, command_id="cmd:entry-1"):
    return verify_and_claim_ctp_managed_native_outbox_action(
        store,
        scope,
        command_id,
        trader,
        fields,
        writer_lease=_lease(),
        verifier=verifier,
    )


@pytest.mark.unit
def test_fresh_verifier_receives_exact_submit_bindings_then_command_is_claimed_once():
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields)
    store = _FakeAtomicStore(command, reservation)
    trader = _FakeTrader()
    verifier = _FakeVerifier()

    action = _verify(store, scope, trader, fields, verifier)

    request = verifier.calls[0]
    assert request.sealed_scope is scope
    assert request.durable_command["request_payload"] == fields
    assert request.native_fields == fields
    assert request.candidate.approval_use_id == command.approval_use_id
    assert request.candidate.approval_digest == command.approval_digest
    assert request.session_binding == command.session_binding
    assert request.current_session.front_id == 4
    assert request.current_session.session_id == 91
    assert action.claimed_command.status == "CLAIMED"
    assert action.use_receipt.source_binding_sha256 == verifier.source_digest
    assert action.authorizes_dispatch is False
    with pytest.raises(CtpOutboxDispatchAuthorityRequired, match="not native dispatch authority"):
        action.require_dispatch_authority()
    assert store.claim_calls == 1
    assert trader.native_calls == []

    with pytest.raises(CtpOutboxPreDispatchError, match="only a READY staged"):
        _verify(store, scope, trader, fields, verifier)
    assert len(verifier.calls) == 1
    assert store.claim_calls == 1
    assert trader.native_calls == []


@pytest.mark.unit
def test_cancel_verification_binds_exact_native_target_and_approval():
    scope = _scope()
    fields = _cancel_fields()
    command, reservation = _command(scope, fields, operation="CANCEL", command_id="cancel:13:1")
    store = _FakeAtomicStore(command, reservation)
    trader = _FakeTrader()
    verifier = _FakeVerifier()

    action = _verify(store, scope, trader, fields, verifier, command.command_id)

    request = verifier.calls[0]
    assert request.candidate.operation == "CANCEL"
    assert request.candidate.request.runtime_action_id == command.command_id
    assert request.candidate.request.target_order_sys_id == command.cancel_target_order_sys_id
    assert request.candidate.request.target_exchange_id == command.cancel_target_exchange_id
    assert request.candidate.request.target_front_id == command.cancel_target_front_id
    assert request.candidate.request.target_session_id == command.cancel_target_session_id
    assert request.durable_command["approval_use_id"] == command.approval_use_id
    assert action.claimed_command.status == "CLAIMED"
    assert store.claim_calls == 1
    assert trader.native_calls == []


@pytest.mark.unit
def test_missing_or_failing_verifier_never_claims_and_does_not_echo_error_text():
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields)
    store = _FakeAtomicStore(command, reservation)
    trader = _FakeTrader()

    with pytest.raises(CtpOutboxDispatchAuthorityRequired, match="verifier is required"):
        verify_and_claim_ctp_managed_native_outbox_action(
            store, scope, command.command_id, trader, fields, writer_lease=_lease(), verifier=None
        )
    with pytest.raises(CtpOutboxDispatchAuthorityRequired, match="verification failed") as caught:
        _verify(store, scope, trader, fields, _FakeVerifier(error=RuntimeError("secret material")))

    assert "secret material" not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert command.status == "READY"
    assert store.claim_calls == 0
    assert trader.native_calls == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "receipt_changes",
    [
        {"approval_digest": "0" * 64},
        {"session_binding_sha256": "0" * 64},
        {"account_fingerprint_sha256": "0" * 64},
        {"connection_generation": 9},
        {"front_id": 5},
        {"session_id": 92},
        {"scope_digest_sha256": "0" * 64},
        {"source_binding_sha256": "invalid"},
    ],
)
def test_mismatched_fresh_receipt_fails_before_atomic_claim(receipt_changes):
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields)
    store = _FakeAtomicStore(command, reservation)
    trader = _FakeTrader()
    verifier = _FakeVerifier(receipt_changes=receipt_changes)

    with pytest.raises(CtpOutboxDispatchAuthorityRequired):
        _verify(store, scope, trader, fields, verifier)

    assert len(verifier.calls) == 1
    assert command.status == "READY"
    assert store.claim_calls == 0
    assert trader.native_calls == []


@pytest.mark.unit
def test_unknown_and_prior_claimed_commands_are_never_verified_or_replayed():
    for status in ("CLAIMED", "UNKNOWN", "COMPLETED"):
        scope = _scope()
        fields = _insert_fields()
        command, reservation = _command(scope, fields, status=status)
        store = _FakeAtomicStore(command, reservation)
        trader = _FakeTrader()
        verifier = _FakeVerifier()

        with pytest.raises(CtpOutboxPreDispatchError, match="only a READY staged"):
            _verify(store, scope, trader, fields, verifier)

        assert verifier.calls == []
        assert store.claim_calls == 0
        assert trader.native_calls == []


@pytest.mark.unit
def test_lost_claim_race_burns_verifier_use_but_never_returns_dispatch_authority():
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields)
    store = _FakeAtomicStore(command, reservation, claim_mode="lost-race")
    trader = _FakeTrader()
    verifier = _FakeVerifier()

    with pytest.raises(CtpOutboxPreDispatchError, match="not atomically claimed"):
        _verify(store, scope, trader, fields, verifier)

    assert verifier.calls[0].candidate.command_id == command.command_id
    assert verifier._used is True
    assert command.status == "CLAIMED"
    assert store.claim_calls == 1
    assert trader.native_calls == []


@pytest.mark.unit
def test_imprecise_claim_echo_fails_closed_after_consumption():
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields)
    store = _FakeAtomicStore(command, reservation, claim_mode="wrong-echo")
    trader = _FakeTrader()
    verifier = _FakeVerifier()

    with pytest.raises(CtpOutboxPreDispatchError, match="exact durable command"):
        _verify(store, scope, trader, fields, verifier)

    assert verifier._used is True
    assert store.claim_calls == 1
    assert trader.native_calls == []


@pytest.mark.unit
def test_sdk_session_change_during_verification_prevents_claim():
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields)
    store = _FakeAtomicStore(command, reservation)
    trader = _FakeTrader()

    class _SessionChangingVerifier(_FakeVerifier):
        def verify_and_consume(self, request):
            receipt = super().verify_and_consume(request)
            with trader._query_state_lock:
                trader._connection_generation += 1
            return receipt

    verifier = _SessionChangingVerifier()
    with pytest.raises(CtpOutboxPreDispatchError, match="changed during verification"):
        _verify(store, scope, trader, fields, verifier)

    assert verifier._used is True
    assert command.status == "READY"
    assert store.claim_calls == 0
    assert trader.native_calls == []


@pytest.mark.unit
def test_sdk_session_change_during_atomic_claim_fails_closed_with_nonreplayable_row():
    scope = _scope()
    fields = _insert_fields()
    command, reservation = _command(scope, fields)
    trader = _FakeTrader()

    class _SessionChangingAtomicStore(_FakeAtomicStore):
        def claim_ctp_dispatch_command(self, scope, command_id, *, writer_lease):
            claimed = super().claim_ctp_dispatch_command(
                scope, command_id, writer_lease=writer_lease
            )
            with trader._query_state_lock:
                trader._connection_generation += 1
                trader._login_connection_generation += 1
            return claimed

    store = _SessionChangingAtomicStore(command, reservation)
    verifier = _FakeVerifier()

    with pytest.raises(CtpOutboxPreDispatchError, match="changed after atomic claim"):
        _verify(store, scope, trader, fields, verifier)

    assert verifier._used is True
    assert command.status == "CLAIMED"
    assert store.claim_calls == 1
    assert trader.native_calls == []
    with pytest.raises(CtpOutboxPreDispatchError, match="only a READY staged"):
        _verify(store, scope, trader, fields, _FakeVerifier())
