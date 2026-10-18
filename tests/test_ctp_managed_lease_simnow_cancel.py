"""Exercise explicit SimNow cancel IDs at the CTP native-call lease boundary.

The credential context and V2 binding verifier below are synthetic test leaves.
The tests cover the real TraderClient callback ingress, execution-write
require/consume path, and one-use native-call lease; they do not prove a
Store-backed facade or real account authorization.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import bt_api_ctp.ctp.client as client_module
from bt_api_ctp.ctp.client import CtpExecutionGateError, TraderClient
from bt_api_ctp.ctp.ctp_structs_order import (
    CThostFtdcInputOrderActionField,
    CThostFtdcInputOrderField,
)
from test_ctp_callback_ingress_client import (
    _FakeTraderApi,
    _OwnerIntent,
    _Sink,
    _binding,
    _field,
    _session_binder,
)
from test_ctp_simnow_managed_write import _configured_simnow_client


_CANCEL_ACTION_ID = "cancel-1"
_ORDER_REF = "000000000101"
_REQUEST_ID = 53
_ACTION_REF = 91


class _LeaseTraderApi(_FakeTraderApi):
    def __init__(self):
        super().__init__()
        self.client_for_lock_probe = None
        self.native_lock_was_free = False
        self.order_action_exception = None

    def ReqUserLogin(self, field, request_id):
        self.calls.append(("ReqUserLogin", request_id))
        response = SimpleNamespace(
            BrokerID=field.BrokerID,
            UserID=field.UserID,
            TradingDay="20260923",
            FrontID=7,
            SessionID=19,
            MaxOrderRef="17",
        )
        self.spi.OnRspUserLogin(response, SimpleNamespace(ErrorID=0), request_id, True)
        return 0

    def ReqOrderAction(self, field, request_id):
        self.calls.append(("ReqOrderAction", field, request_id))
        client = self.client_for_lock_probe
        if client is not None:
            acquired = threading.Event()

            def probe_sdk_lock():
                if client._query_state_lock.acquire(timeout=0.5):
                    client._query_state_lock.release()
                    acquired.set()

            probe = threading.Thread(target=probe_sdk_lock, daemon=True)
            probe.start()
            probe.join(1.0)
            self.native_lock_was_free = acquired.is_set()
        if self.order_action_exception is not None:
            raise self.order_action_exception
        return self.order_action_result


class _LeaseTraderApiFactory:
    instances = []

    @classmethod
    def CreateFtdcTraderApi(cls, _flow_path):
        api = _LeaseTraderApi()
        cls.instances.append(api)
        return api


def _cancel_payload():
    return {
        "BrokerID": "3070",
        "InvestorID": "offline-account",
        "UserID": "offline-account",
        "InstrumentID": "SA2701",
        "OrderRef": _ORDER_REF,
        "ExchangeID": "CZCE",
        "OrderSysID": "SYS000101",
        "FrontID": 7,
        "SessionID": 19,
        "ActionFlag": "0",
        "LimitPrice": 0.0,
        "VolumeChange": 0,
    }


def _submit_payload():
    return {
        "BrokerID": "3070",
        "InvestorID": "offline-account",
        "UserID": "offline-account",
        "InstrumentID": "SA2701",
        "OrderRef": _ORDER_REF,
        "Direction": "0",
        "CombOffsetFlag": "0",
        "CombHedgeFlag": "1",
        "OrderPriceType": "2",
        "LimitPrice": 1000.0,
        "VolumeTotalOriginal": 1,
        "TimeCondition": "3",
        "VolumeCondition": "1",
        "ExchangeID": "CZCE",
    }


def _active_simnow_client(monkeypatch, *, write_intent_verifier=True):
    _inactive_client, _unused_native, capability, credential_binding, scopes = (
        _configured_simnow_client(
            approval_kind="recovery",
            write_intent_verifier=write_intent_verifier,
            install_binding=False,
            connection_generation=1,
        )
    )
    _LeaseTraderApiFactory.instances = []
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "synthetic-flow")
    monkeypatch.setattr(client_module, "_register_ctp_native_api", lambda _api: None)
    monkeypatch.setattr(client_module, "CThostFtdcTraderApi", _LeaseTraderApiFactory)

    client = TraderClient(
        credential_binding.td_front,
        "3070",
        "offline-account",
        "synthetic-test-secret",
        md_front=credential_binding.md_front,
        ctp_env_profile="config_front_pair",
    )
    sink = _Sink()
    owner = _OwnerIntent()
    phase = "constructed"
    api = None
    try:
        client.install_callback_ingress_sink(
            owner,
            sink,
            _session_binder,
            lambda verified_owner, candidate: candidate if verified_owner is owner else None,
        )
        phase = "callback_sink_installed"
        client.start()
        phase = "native_started"
        assert _LeaseTraderApiFactory.instances
        api = _LeaseTraderApiFactory.instances[-1]
        assert api.join_started.wait(1.0)
        state = client._callback_ingress
        if state.phase != "ACTIVE":
            raise AssertionError(
                "synthetic callback setup failed: "
                f"phase={phase}; callback_phase={state.phase}; "
                f"poison_code={state.poison_reason!r}; "
                f"sink_poison_codes={tuple(item[1] for item in sink.poisons)!r}"
            )
        assert state.active_session.trading_day == "20260923"

        credential_binding.owner.exchange_feeds["CTP___FUTURE"]._trader = client
        client._settlement_state = "confirmed"
        client._settlement_readback_verified = True
        client._settlement_account_fingerprint = client._account_fingerprint
        client._settlement_trading_day = client._trading_day
        client._settlement_connection_generation = client._connection_generation
        client._ready = True
        client._has_current_settlement_readback_locked = lambda: True
        client.configure_execution_gate(capability)
        phase = "execution_gate_configured"
        client.configure_runtime_simnow_credential_binding(capability, credential_binding)
        phase = "credential_binding_configured"
        api.client_for_lock_probe = client
        return client, api, sink, owner, credential_binding, scopes
    except BaseException as exc:
        state = getattr(client, "_callback_ingress", None)
        if hasattr(exc, "add_note"):
            exc.add_note(
                "synthetic callback setup diagnostic: "
                f"phase={phase}; callback_phase={getattr(state, 'phase', None)!r}; "
                f"poison_code={getattr(state, 'poison_reason', None)!r}; "
                f"sink_poison_codes={tuple(item[1] for item in sink.poisons)!r}"
            )
        try:
            client.stop()
        except BaseException as cleanup_error:
            if hasattr(exc, "add_note"):
                exc.add_note(
                    "synthetic callback cleanup failed: "
                    f"error_type={type(cleanup_error).__name__}"
                )
        raise


def _managed_binding(owner, session, *, operation="CANCEL", managed_action_id=None):
    payload = _cancel_payload() if operation == "CANCEL" else _submit_payload()
    binding = _binding(
        owner,
        session,
        payload,
        operation=operation,
        action_ref=_ACTION_REF if operation == "CANCEL" else None,
        request_id=_REQUEST_ID,
    )
    # This is a synthetic external binding leaf. Keep its action identity equal
    # to the independent recovery approval used by the real client checks.
    binding.envelope["managed_action_id"] = managed_action_id or _CANCEL_ACTION_ID
    return binding, payload


def _lease(client, owner, binding):
    return client.acquire_managed_native_call_lease(owner, binding)


def test_explicit_simnow_cancel_tuple_runs_real_require_consume_and_is_one_use(
    monkeypatch,
):
    client, api, sink, owner, _credential_binding, scopes = _active_simnow_client(
        monkeypatch
    )
    try:
        binding, payload = _managed_binding(
            owner, client._callback_ingress.active_session
        )
        lease = _lease(client, owner, binding)
        field = _field(
            CThostFtdcInputOrderActionField,
            payload,
            RequestID=_REQUEST_ID,
            OrderActionRef=_ACTION_REF,
        )

        assert client.submit_order_action_with_lease(
            lease,
            field,
            _REQUEST_ID,
            runtime_action_id=_CANCEL_ACTION_ID,
            managed_cancel_intent_id=_CANCEL_ACTION_ID,
        ) == 0

        calls = [entry for entry in api.calls if entry[0] == "ReqOrderAction"]
        assert len(calls) == 1
        _name, detached, request_id = calls[0]
        assert request_id == _REQUEST_ID
        assert detached is not field
        assert detached.OrderActionRef == _ACTION_REF
        assert detached.RequestID == _REQUEST_ID
        assert detached.OrderActionRef != detached.RequestID
        assert api.native_lock_was_free is True
        assert scopes[-1]["operation"] == "cancel"
        assert scopes[-1]["runtime_action_id"] == _CANCEL_ACTION_ID
        assert scopes[-1]["managed_cancel_intent_id"] == _CANCEL_ACTION_ID
        assert client._callback_ingress.phase == "ACTIVE"
        assert [record.callback_name for record in sink.records] == [
            "OnFrontConnected",
            "OnRspAuthenticate",
            "OnRspUserLogin",
        ]
        assert client._native_request_inflight_refs == 0
        assert client._callback_ingress.native_call_refs == 0

        with pytest.raises(
            CtpExecutionGateError,
            match="ctp_managed_native_call_lease_invalid",
        ):
            client.submit_order_action_with_lease(
                lease,
                field,
                _REQUEST_ID,
                runtime_action_id=_CANCEL_ACTION_ID,
                managed_cancel_intent_id=_CANCEL_ACTION_ID,
            )
        with pytest.raises(
            CtpExecutionGateError,
            match="ctp_managed_native_call_binding_reused",
        ):
            _lease(client, owner, binding)
        assert len([entry for entry in api.calls if entry[0] == "ReqOrderAction"]) == 1
    finally:
        client.stop()


@pytest.mark.parametrize(
    ("runtime_action_id", "managed_cancel_intent_id", "binding_action_id", "error"),
    [
        (None, None, _CANCEL_ACTION_ID, "ctp_simnow_runtime_action_id_required"),
        (_CANCEL_ACTION_ID, None, _CANCEL_ACTION_ID, "ctp_execution_gate_managed_cancel_identity_incomplete"),
        ("cancel-other", _CANCEL_ACTION_ID, _CANCEL_ACTION_ID, "ctp_execution_gate_managed_cancel_action_mismatch"),
        (_CANCEL_ACTION_ID, _CANCEL_ACTION_ID, "other-action", "ctp_managed_native_cancel_identity_mismatch"),
    ],
)
def test_cancel_identity_tuple_failures_poison_before_native_call(
    monkeypatch,
    runtime_action_id,
    managed_cancel_intent_id,
    binding_action_id,
    error,
):
    client, api, _sink, owner, _credential_binding, _scopes = _active_simnow_client(
        monkeypatch
    )
    try:
        binding, payload = _managed_binding(
            owner,
            client._callback_ingress.active_session,
            managed_action_id=binding_action_id,
        )
        lease = _lease(client, owner, binding)
        field = _field(
            CThostFtdcInputOrderActionField,
            payload,
            RequestID=_REQUEST_ID,
            OrderActionRef=_ACTION_REF,
        )
        kwargs = {}
        if runtime_action_id is not None:
            kwargs["runtime_action_id"] = runtime_action_id
        if managed_cancel_intent_id is not None:
            kwargs["managed_cancel_intent_id"] = managed_cancel_intent_id

        with pytest.raises(CtpExecutionGateError, match=error):
            client.submit_order_action_with_lease(lease, field, _REQUEST_ID, **kwargs)

        assert not any(entry[0] == "ReqOrderAction" for entry in api.calls)
        assert client._callback_ingress.poisoned is True
        assert client._native_request_inflight_refs == 0
    finally:
        client.stop()


@pytest.mark.parametrize(
    ("write_intent_verifier", "error"),
    [
        (None, "ctp_simnow_write_verifier_required"),
        (lambda _scope: False, "ctp_simnow_write_verifier_rejected"),
    ],
)
def test_missing_or_rejected_simnow_write_verifier_never_calls_native(
    monkeypatch, write_intent_verifier, error
):
    client, api, _sink, owner, _credential_binding, _scopes = _active_simnow_client(
        monkeypatch, write_intent_verifier=write_intent_verifier
    )
    try:
        binding, payload = _managed_binding(
            owner, client._callback_ingress.active_session
        )
        lease = _lease(client, owner, binding)
        field = _field(
            CThostFtdcInputOrderActionField,
            payload,
            RequestID=_REQUEST_ID,
            OrderActionRef=_ACTION_REF,
        )

        with pytest.raises(CtpExecutionGateError, match=error):
            client.submit_order_action_with_lease(
                lease,
                field,
                _REQUEST_ID,
                runtime_action_id=_CANCEL_ACTION_ID,
                managed_cancel_intent_id=_CANCEL_ACTION_ID,
            )
        assert not any(entry[0] == "ReqOrderAction" for entry in api.calls)
        assert client._callback_ingress.poisoned is True
    finally:
        client.stop()


def test_simnow_cancel_tuple_cannot_be_reused_for_submit(monkeypatch):
    client, api, _sink, owner, _credential_binding, _scopes = _active_simnow_client(
        monkeypatch
    )
    try:
        binding, payload = _managed_binding(
            owner,
            client._callback_ingress.active_session,
            operation="SUBMIT",
        )
        lease = _lease(client, owner, binding)
        field = _field(CThostFtdcInputOrderField, payload, RequestID=_REQUEST_ID)

        with pytest.raises(
            CtpExecutionGateError,
            match="ctp_managed_native_cancel_identity_on_submit",
        ):
            client._submit_with_managed_native_call_lease(
                lease,
                field,
                _REQUEST_ID,
                method_name="ReqOrderInsert",
                runtime_action_id=_CANCEL_ACTION_ID,
                managed_cancel_intent_id=_CANCEL_ACTION_ID,
            )
        assert not any(entry[0] in {"ReqOrderInsert", "ReqOrderAction"} for entry in api.calls)
        assert client._callback_ingress.poisoned is True
    finally:
        client.stop()


def test_ambiguous_native_cancel_result_poisoned_and_not_retried(monkeypatch):
    client, api, _sink, owner, _credential_binding, _scopes = _active_simnow_client(
        monkeypatch
    )
    try:
        api.order_action_result = 1
        binding, payload = _managed_binding(
            owner, client._callback_ingress.active_session
        )
        lease = _lease(client, owner, binding)
        field = _field(
            CThostFtdcInputOrderActionField,
            payload,
            RequestID=_REQUEST_ID,
            OrderActionRef=_ACTION_REF,
        )

        with pytest.raises(
            CtpExecutionGateError,
            match="ctp_managed_native_call_result_ambiguous",
        ):
            client.submit_order_action_with_lease(
                lease,
                field,
                _REQUEST_ID,
                runtime_action_id=_CANCEL_ACTION_ID,
                managed_cancel_intent_id=_CANCEL_ACTION_ID,
            )

        assert len([entry for entry in api.calls if entry[0] == "ReqOrderAction"]) == 1
        assert client._callback_ingress.poisoned is True
        assert client._native_request_inflight_refs == 0
        with pytest.raises(CtpExecutionGateError):
            client.submit_order_action_with_lease(
                lease,
                field,
                _REQUEST_ID,
                runtime_action_id=_CANCEL_ACTION_ID,
                managed_cancel_intent_id=_CANCEL_ACTION_ID,
            )
        assert len([entry for entry in api.calls if entry[0] == "ReqOrderAction"]) == 1
    finally:
        client.stop()
