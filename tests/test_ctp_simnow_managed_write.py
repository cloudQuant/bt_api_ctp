from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import (
    CtpExecutionGateError,
    CtpRuntimeSimNowCredentialBinding,
    TraderClient,
    _issue_ctp_execution_authority_for_core,
    _TraderSpi,
)
from bt_api_ctp.ctp_env_selector import (
    is_official_simnow_td_front,
    official_simnow_fronts,
)


class _CountingNativeApi:
    def __init__(self):
        self.calls = []

    def ReqOrderInsert(self, field, request_id):
        self.calls.append(("insert", field, request_id))
        return 0

    def ReqOrderAction(self, field, request_id):
        self.calls.append(("cancel", field, request_id))
        return 0


def _utc_after(seconds: int) -> str:
    value = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _configured_simnow_client(
    *,
    approval_kind="entry",
    approval_expires_in=3600,
    write_intent_verifier=True,
    profile="set1_group1",
    td_front=None,
    md_front=None,
    bind_constructor_pair=True,
    install_binding=True,
):
    from bt_api_py._ctp_credential_binding import _new_scope, _new_test_verifier
    from bt_api_py._ctp_execution_authorization import (
        _CAPABILITY_SEAL,
        APPROVAL_PURPOSE,
        RECOVERY_APPROVAL_PURPOSE,
        SIMNOW_ENTRY_APPROVAL_SCHEMA_VERSION,
        SIMNOW_RECOVERY_APPROVAL_SCHEMA_VERSION,
        _new_runtime_context,
    )

    if td_front is None or md_front is None:
        default_td_front, default_md_front = official_simnow_fronts(profile)
        td_front = td_front or default_td_front
        md_front = md_front or default_md_front
    constructor_scope = (
        {"md_front": md_front, "ctp_env_profile": profile}
        if profile == "config_front_pair" and bind_constructor_pair
        else {}
    )
    client = TraderClient(
        td_front,
        "3070",
        "offline-account",
        "unused",
        **constructor_scope,
    )
    native = _CountingNativeApi()
    client._api = native
    client._session_native_api = native
    client._session_native_front = td_front
    client._front_connected_front = td_front
    client._connected = True
    client._authentication_state = "authenticated"
    client._login_state = "logging_in"
    client._connection_generation = 7
    client._login_request_id = 1
    client._login_connection_generation = 7
    _TraderSpi(client).OnRspUserLogin(
        SimpleNamespace(
            BrokerID="3070",
            UserID="offline-account",
            FrontID=1,
            SessionID=2,
            TradingDay="20260923",
            MaxOrderRef="7",
        ),
        SimpleNamespace(ErrorID=0),
        1,
        True,
    )
    client._settlement_state = "confirmed"
    client._settlement_readback_verified = True
    client._settlement_account_fingerprint = client._account_fingerprint
    client._settlement_trading_day = client._trading_day
    client._settlement_connection_generation = 7
    client._ready = True
    client._has_current_settlement_readback_locked = lambda: True

    capability = _issue_ctp_execution_authority_for_core()
    client.configure_execution_gate(capability)

    owner = SimpleNamespace(exchange_feeds={})
    feed = SimpleNamespace(
        _trader=client,
        _execution_bound_td_front=td_front,
        _execution_bound_md_front=md_front,
        _md_client=SimpleNamespace(front=md_front, connection_generation=1),
        _md_stream_generation=1,
    )
    owner.exchange_feeds["CTP___FUTURE"] = feed
    seed_digest = "a" * 64
    scope_values = {
        "account_fingerprint": f"acct_{client._account_fingerprint}",
        "trading_day": client._trading_day,
        "connection_generation": client._connection_generation,
        "environment_profile": profile,
        "td_front": td_front,
        "md_front": md_front,
        "td_front_sha256": hashlib.sha256(td_front.encode()).hexdigest(),
        "md_front_sha256": hashlib.sha256(md_front.encode()).hexdigest(),
        "backtrader_sha256": seed_digest,
        "backtrader_runtime_sha256": "0" * 64,
        "bt_api_py_sha256": seed_digest,
        "bt_api_ctp_sha256": seed_digest,
        "bt_api_base_sha256": seed_digest,
        "native_sha256": seed_digest,
        "dependency_hashes_sha256": seed_digest,
        "configuration_sha256": seed_digest,
        "strategy_identity_sha256": seed_digest,
        "preflight_sha256": seed_digest,
        "evidence_sha256": seed_digest,
        "md_connection_generation": 1,
        "md_stream_generation": 1,
    }
    scope = _new_scope(scope_values)
    verifier = _new_test_verifier(
        owner,
        lambda: {
            "credential_binding_key_id": "offline-key",
            "credential_binding_hmac_sha256": seed_digest,
        },
    )
    credential_binding = verifier.refresh(scope, owner=owner, operation="test")
    context_values = {
        **scope_values,
        "execution_cycle_id": "cycle-1",
        **credential_binding,
    }
    self = owner
    credential_binding_verifier = verifier

    def refresh_context():
        refreshed = credential_binding_verifier.refresh(scope, owner=self, operation="test")
        return _new_runtime_context(
            {**context_values, **refreshed}, owner=self, refresh=refresh_context
        )

    context = _new_runtime_context(context_values, owner=owner, refresh=refresh_context)

    instrument = {"instrument_id": "SA2701", "exchange_id": "CZCE"}
    if approval_kind == "entry":
        payload = {
            "purpose": APPROVAL_PURPOSE,
            "schema_version": SIMNOW_ENTRY_APPROVAL_SCHEMA_VERSION,
            "authorized_instruments": [instrument],
            "primary_instrument": instrument,
        }
    else:
        payload = {
            "purpose": RECOVERY_APPROVAL_PURPOSE,
            "schema_version": SIMNOW_RECOVERY_APPROVAL_SCHEMA_VERSION,
            "recovery_actions": [
                {
                    "action_id": "cancel-1",
                    "action_kind": "cancel",
                    "instrument_id": "SA2701",
                    "exchange_id": "CZCE",
                    "account_fingerprint": f"acct_{client._account_fingerprint}",
                    "trading_day": client._trading_day,
                    "connection_generation": client._connection_generation,
                    "environment_profile": profile,
                    "expires_at": _utc_after(approval_expires_in),
                }
            ],
        }
    payload.update(context_values)
    payload.update(
        {
            "approval_id": "offline-approval",
            "nonce": "offline-nonce",
            "not_before": _utc_after(-60),
            "expires_at": _utc_after(approval_expires_in),
            "revocation_snapshot_version": 1,
        }
    )
    approval = object.__new__(
        __import__(
            "bt_api_py._ctp_execution_authorization",
            fromlist=["CtpExecutionApproval"],
        ).CtpExecutionApproval
    )
    object.__setattr__(approval, "payload", MappingProxyType(payload))
    object.__setattr__(approval, "payload_sha256", "0" * 64)
    object.__setattr__(approval, "_seal", _CAPABILITY_SEAL)
    object.__setattr__(
        approval,
        "revocation_snapshot",
        MappingProxyType({"version": 1, "expires_at": _utc_after(approval_expires_in)}),
    )

    verified_scopes = []
    if write_intent_verifier is True:
        verifier_callback = lambda scope: verified_scopes.append(dict(scope)) or True
    else:
        verifier_callback = write_intent_verifier
    binding = CtpRuntimeSimNowCredentialBinding(
        owner=owner,
        approval_context=context,
        approval=approval,
        credential_binding_verifier=verifier,
        td_front=td_front,
        md_front=md_front,
        environment_profile=profile,
        write_intent_verifier=verifier_callback,
    )
    if install_binding:
        client.configure_runtime_simnow_credential_binding(capability, binding)
    return client, native, capability, binding, verified_scopes


def _insert_field(**changes):
    values = {
        "BrokerID": "3070",
        "InvestorID": "offline-account",
        "UserID": "offline-account",
        "InstrumentID": "SA2701",
        "ExchangeID": "CZCE",
        "OrderRef": "000000000101",
        "RequestID": 11,
        "Direction": "0",
        "CombOffsetFlag": "0",
        "CombHedgeFlag": "1",
        "VolumeTotalOriginal": 1,
        "LimitPrice": 1000.0,
        "OrderPriceType": "2",
        "TimeCondition": "3",
        "VolumeCondition": "1",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _cancel_field(**changes):
    values = {
        "BrokerID": "3070",
        "InvestorID": "offline-account",
        "InstrumentID": "SA2701",
        "ExchangeID": "CZCE",
        "OrderSysID": "SYS000101",
        "ActionFlag": "0",
        "OrderActionRef": 12,
        "RequestID": 12,
    }
    values.update(changes)
    return SimpleNamespace(**values)


_ORDER_ID = "bt-managed-v1:" + "a" * 64


def test_explicit_owner_bound_entry_approval_allows_one_managed_insert():
    client, native, capability, _binding, scopes = _configured_simnow_client()

    result = client.submit_order_insert(
        _insert_field(),
        11,
        execution_capability=capability,
        runtime_order_id=_ORDER_ID,
        managed_intent_id="entry-1",
    )

    assert result == 0
    assert [call[0] for call in native.calls] == ["insert"]
    assert client.get_execution_gate_state()["armed"] is False
    assert client.get_execution_gate_state()["runtime_simnow_credential_binding_configured"] is True
    assert client.get_execution_gate_state()["runtime_simnow_write_verifier_configured"] is True
    evidence = client.get_order_insert_evidence(11, order_ref="000000000101")
    assert evidence is not None
    assert evidence.status == "unknown"
    assert evidence.reason == "awaiting_native_callback"
    assert scopes == [
        {
            "schema_version": "ctp-simnow-managed-write-v1",
            "operation": "insert",
            "td_front": _binding.td_front,
            "md_front": _binding.md_front,
            "environment_profile": "set1_group1",
            "account_fingerprint": f"acct_{client._account_fingerprint}",
            "trading_day": client._trading_day,
            "connection_generation": client._connection_generation,
            "instrument_id": "SA2701",
            "exchange_id": "CZCE",
            "runtime_order_id": _ORDER_ID,
            "managed_intent_id": "entry-1",
            "runtime_action_id": None,
            "managed_cancel_intent_id": None,
            "request_id": 11,
            "approval_id": "offline-approval",
            "approval_nonce": "offline-nonce",
            "approval_payload_sha256": "0" * 64,
            "order_ref": "000000000101",
            "direction": "0",
            "offset_flag": "0",
            "hedge_flag": "1",
            "volume_total_original": 1,
            "limit_price": "1000",
            "order_price_type": "2",
            "time_condition": "3",
            "volume_condition": "1",
        }
    ]


def test_owner_bound_approval_accepts_exact_custom_configured_front_pair():
    td_front = "tcp://configured-td.invalid:41001"
    md_front = "tcp://configured-md.invalid:41002"
    client, native, capability, binding, scopes = _configured_simnow_client(
        td_front=td_front,
        md_front=md_front,
    )

    assert not is_official_simnow_td_front(td_front)
    assert (
        client.submit_order_insert(
            _insert_field(),
            11,
            execution_capability=capability,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-custom-front",
        )
        == 0
    )

    assert [call[0] for call in native.calls] == ["insert"]
    assert binding.td_front == td_front
    assert binding.md_front == md_front
    assert scopes[0]["td_front"] == td_front
    assert scopes[0]["md_front"] == md_front


def test_neutral_config_front_pair_marker_allows_only_the_bound_simnow_insert():
    td_front = "tcp://configured-neutral-td.invalid:41501"
    md_front = "tcp://configured-neutral-md.invalid:41502"
    client, native, capability, binding, scopes = _configured_simnow_client(
        profile="config_front_pair",
        td_front=td_front,
        md_front=md_front,
    )

    assert (
        client.submit_order_insert(
            _insert_field(),
            11,
            execution_capability=capability,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-neutral-front-pair",
        )
        == 0
    )

    assert [call[0] for call in native.calls] == ["insert"]
    assert client._bound_md_front == md_front
    assert client.ctp_env_profile == "config_front_pair"
    assert binding.environment_profile == "config_front_pair"
    assert scopes[0]["environment_profile"] == "config_front_pair"
    assert scopes[0]["td_front"] == td_front
    assert scopes[0]["md_front"] == md_front


def test_neutral_constructor_pair_must_match_owner_bound_managed_binding():
    client, native, capability, binding, _scopes = _configured_simnow_client(
        profile="config_front_pair",
        td_front="tcp://configured-neutral-td.invalid:41601",
        md_front="tcp://configured-neutral-md.invalid:41602",
    )
    changed_binding = replace(binding, md_front="tcp://different-md.invalid:41603")

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_simnow_credential_binding_rejected",
    ):
        client.configure_runtime_simnow_credential_binding(capability, changed_binding)

    assert native.calls == []


def test_neutral_managed_binding_requires_the_explicit_constructor_pair():
    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_simnow_credential_binding_rejected",
    ):
        _configured_simnow_client(
            profile="config_front_pair",
            td_front="tcp://configured-neutral-td.invalid:41701",
            md_front="tcp://configured-neutral-md.invalid:41702",
            bind_constructor_pair=False,
        )


def test_custom_front_pair_is_bound_to_approval_and_cannot_be_substituted():
    client, native, capability, binding, _scopes = _configured_simnow_client(
        td_front="tcp://configured-td.invalid:42001",
        md_front="tcp://configured-md.invalid:42002",
    )
    changed_binding = replace(binding, md_front="tcp://different-md.invalid:42003")

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_simnow_credential_binding_rejected",
    ):
        client.configure_runtime_simnow_credential_binding(capability, changed_binding)

    assert native.calls == []


def test_custom_md_front_change_after_binding_rejects_before_native_dispatch():
    client, native, capability, binding, _scopes = _configured_simnow_client(
        td_front="tcp://configured-td.invalid:42501",
        md_front="tcp://configured-md.invalid:42502",
    )
    feed = binding.owner.exchange_feeds["CTP___FUTURE"]
    feed._execution_bound_md_front = "tcp://changed-md.invalid:42503"
    feed._md_client.front = feed._execution_bound_md_front

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_simnow_credential_binding_rejected",
    ):
        client.submit_order_insert(
            _insert_field(),
            11,
            execution_capability=capability,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-changed-md",
        )

    assert native.calls == []
    assert client.get_request_counts()["order_insert"] == 0


def test_bound_custom_simnow_client_cannot_fall_back_to_general_gate():
    client, native, capability, _binding, _scopes = _configured_simnow_client(
        td_front="tcp://configured-td.invalid:43001",
        md_front="tcp://configured-md.invalid:43002",
    )

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_simnow_execution_not_admitted",
    ):
        client.arm_execution_gate(
            capability,
            object(),
            _environment_profile="production",
            _environment_verified=True,
        )

    assert native.calls == []
    assert client.get_execution_gate_state()["armed"] is False


def test_production_environment_cannot_install_custom_simnow_binding():
    client, native, capability, _binding, _scopes = _configured_simnow_client(
        profile="production",
        td_front="tcp://configured-production-td.invalid:44001",
        md_front="tcp://configured-production-md.invalid:44002",
        install_binding=False,
    )
    candidate = CtpRuntimeSimNowCredentialBinding(
        owner=_binding.owner,
        approval_context=_binding.approval_context,
        approval=_binding.approval,
        credential_binding_verifier=_binding.credential_binding_verifier,
        td_front=_binding.td_front,
        md_front=_binding.md_front,
        environment_profile="production",
        write_intent_verifier=_binding.write_intent_verifier,
    )

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_simnow_bounded_profile_required",
    ):
        client.configure_runtime_simnow_credential_binding(capability, candidate)

    assert native.calls == []
    assert (
        client.get_execution_gate_state()["runtime_simnow_credential_binding_configured"] is False
    )


def test_explicit_recovery_approval_allows_only_the_named_managed_cancel():
    client, native, capability, _binding, scopes = _configured_simnow_client(
        approval_kind="recovery"
    )

    result = client.submit_order_action(
        _cancel_field(),
        12,
        execution_capability=capability,
        runtime_order_id=_ORDER_ID,
        managed_intent_id="entry-1",
        runtime_action_id="sdk-action-12",
        managed_cancel_intent_id="cancel-1",
    )

    assert result == 0
    assert [call[0] for call in native.calls] == ["cancel"]
    assert client.get_execution_gate_state()["armed"] is False
    evidence = client.get_order_action_evidence(12, order_action_ref="12")
    assert evidence is not None
    assert evidence.status == "unknown"
    assert evidence.reason == "awaiting_native_callback"
    assert scopes[0]["operation"] == "cancel"
    assert scopes[0]["managed_cancel_intent_id"] == "cancel-1"
    assert scopes[0]["target_order_sys_id"] == "SYS000101"


@pytest.mark.parametrize(
    "field_changes, call_changes, match",
    [
        ({}, {"managed_intent_id": None}, "ctp_simnow_credential_binding_rejected"),
        ({"InstrumentID": "RB2701"}, {}, "ctp_simnow_credential_binding_rejected"),
        (
            {"InvestorID": "other-account", "UserID": "other-account"},
            {},
            "native_field_identity_mismatch",
        ),
        ({"ExchangeID": "SHFE"}, {}, "ctp_simnow_credential_binding_rejected"),
    ],
)
def test_entry_scope_mismatch_rejects_before_native_insert(field_changes, call_changes, match):
    client, native, capability, _binding, _scopes = _configured_simnow_client()
    kwargs = {
        "execution_capability": capability,
        "runtime_order_id": _ORDER_ID,
        "managed_intent_id": "entry-1",
    }
    kwargs.update(call_changes)

    with pytest.raises(CtpExecutionGateError, match=match):
        client.submit_order_insert(_insert_field(**field_changes), 11, **kwargs)

    assert native.calls == []
    assert client.get_request_counts()["order_insert"] == 0


def test_missing_binding_or_capability_stays_closed_before_native_insert():
    td_front, _md_front = official_simnow_fronts("set1_group1")
    native = _CountingNativeApi()
    client = TraderClient(td_front, "3070", "offline-account", "unused")
    client._api = native
    client._session_native_api = native
    client._session_native_front = td_front
    client._front_connected_front = td_front
    client._connected = True
    client._connection_generation = 7
    client._trading_day = "20260923"

    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        client.submit_order_insert(
            _insert_field(),
            11,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-1",
        )

    assert native.calls == []

    client, native, _capability, binding, _scopes = _configured_simnow_client()
    client._execution_gate_capability = None
    with pytest.raises(CtpExecutionGateError, match="ctp_execution_gate_capability_mismatch"):
        client.submit_order_insert(
            _insert_field(),
            11,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-1",
        )

    assert native.calls == []
    assert binding.environment_profile == "set1_group1"


def test_binding_rejects_7x24_profile_and_never_dispatches_native_write():
    td_front, md_front = official_simnow_fronts("set2_7x24")
    client = TraderClient(td_front, "3070", "offline-account", "unused")
    native = _CountingNativeApi()
    client._api = native
    capability = _issue_ctp_execution_authority_for_core()
    client.configure_execution_gate(capability)
    binding = CtpRuntimeSimNowCredentialBinding(
        owner=object(),
        approval_context=object(),
        approval=object(),
        credential_binding_verifier=object(),
        td_front=td_front,
        md_front=md_front,
        environment_profile="set2_7x24",
    )

    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_bounded_profile_required"):
        client.configure_runtime_simnow_credential_binding(capability, binding)

    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        client.submit_order_insert(
            _insert_field(),
            11,
            execution_capability=capability,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-1",
        )
    assert native.calls == []


@pytest.mark.parametrize(
    "changed_field, changed_value", [("_trading_day", "20260924"), ("_connection_generation", 8)]
)
def test_stale_day_or_generation_binding_rejects_before_native_insert(changed_field, changed_value):
    client, native, capability, _binding, _scopes = _configured_simnow_client()
    setattr(client, changed_field, changed_value)

    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_credential_binding_rejected"):
        client.submit_order_insert(
            _insert_field(),
            11,
            execution_capability=capability,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-1",
        )

    assert native.calls == []
    assert client.get_request_counts()["order_insert"] == 0


def test_entry_approval_cannot_authorize_a_cancel_or_unapproved_recovery_id():
    client, native, capability, _binding, _scopes = _configured_simnow_client()
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_credential_binding_rejected"):
        client.submit_order_action(
            _cancel_field(),
            12,
            execution_capability=capability,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-1",
            runtime_action_id="sdk-action-12",
            managed_cancel_intent_id="cancel-1",
        )

    assert native.calls == []
    assert client.get_request_counts()["order_action"] == 0


def test_expired_entry_approval_rejects_before_native_insert():
    client, native, capability, binding, _scopes = _configured_simnow_client()
    expired_payload = dict(binding.approval.payload)
    expired_payload["expires_at"] = _utc_after(-1)
    object.__setattr__(binding.approval, "payload", MappingProxyType(expired_payload))
    object.__setattr__(
        binding.approval,
        "revocation_snapshot",
        MappingProxyType({"version": 1, "expires_at": _utc_after(3600)}),
    )

    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_credential_binding_rejected"):
        client.submit_order_insert(
            _insert_field(),
            11,
            execution_capability=capability,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-1",
        )

    assert native.calls == []
    assert client.get_request_counts()["order_insert"] == 0


def test_expired_approved_revocation_snapshot_rejects_before_native_insert():
    client, native, capability, binding, _scopes = _configured_simnow_client()
    object.__setattr__(
        binding.approval,
        "revocation_snapshot",
        MappingProxyType({"version": 1, "expires_at": _utc_after(-1)}),
    )

    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_credential_binding_rejected"):
        client.submit_order_insert(
            _insert_field(),
            11,
            execution_capability=capability,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-1",
        )

    assert native.calls == []
    assert client.get_request_counts()["order_insert"] == 0


def test_no_default_write_verifier_and_rejected_verifier_never_dispatch():
    client, native, capability, _binding, _scopes = _configured_simnow_client(
        write_intent_verifier=None
    )

    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_write_verifier_required"):
        client.submit_order_insert(
            _insert_field(),
            11,
            execution_capability=capability,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-1",
        )
    assert native.calls == []

    client, native, capability, _binding, _scopes = _configured_simnow_client(
        write_intent_verifier=lambda _scope: False
    )
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_write_verifier_rejected"):
        client.submit_order_insert(
            _insert_field(),
            11,
            execution_capability=capability,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-1",
        )
    assert native.calls == []


@pytest.mark.parametrize(
    "field_changes",
    [
        {"VolumeTotalOriginal": 0},
        {"VolumeTotalOriginal": True},
        {"LimitPrice": float("nan")},
        {"LimitPrice": 0},
        {"Direction": "9"},
        {"CombOffsetFlag": "9"},
        {"CombHedgeFlag": "9"},
        {"OrderPriceType": "1"},
        {"TimeCondition": "1"},
        {"VolumeCondition": "2"},
    ],
)
def test_invalid_native_order_limits_reject_before_verifier_or_dispatch(field_changes):
    client, native, capability, _binding, scopes = _configured_simnow_client()

    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_write_verifier_rejected"):
        client.submit_order_insert(
            _insert_field(**field_changes),
            11,
            execution_capability=capability,
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-1",
        )

    assert native.calls == []
    assert scopes == []


def test_unverified_approval_object_is_rejected_when_binding_is_installed():
    client, native, capability, binding, _scopes = _configured_simnow_client()
    unsealed = object.__new__(type(binding.approval))
    object.__setattr__(unsealed, "payload", binding.approval.payload)
    object.__setattr__(unsealed, "revocation_snapshot", binding.approval.revocation_snapshot)
    forged_binding = CtpRuntimeSimNowCredentialBinding(
        owner=binding.owner,
        approval_context=binding.approval_context,
        approval=unsealed,
        credential_binding_verifier=binding.credential_binding_verifier,
        td_front=binding.td_front,
        md_front=binding.md_front,
        environment_profile=binding.environment_profile,
        write_intent_verifier=binding.write_intent_verifier,
    )

    client._runtime_simnow_credential_binding = None
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_credential_binding_rejected"):
        client.configure_runtime_simnow_credential_binding(capability, forged_binding)
    assert native.calls == []


def test_one_shot_authorization_cannot_be_consumed_twice():
    client, native, capability, _binding, scopes = _configured_simnow_client()
    original_require = client._require_execution_write_locked
    issued = []

    def capture_authorization(*args, **kwargs):
        authorization = original_require(*args, **kwargs)
        issued.append(authorization)
        return authorization

    client._require_execution_write_locked = capture_authorization
    field = _insert_field()
    client.submit_order_insert(
        field,
        11,
        execution_capability=capability,
        runtime_order_id=_ORDER_ID,
        managed_intent_id="entry-1",
    )

    assert len(issued) == 1
    assert issued[0]._used is True
    assert len(scopes) == 1
    assert len(native.calls) == 1
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_write_authorization_required"):
        client._consume_runtime_simnow_write_authorization_locked(
            issued[0],
            capability=capability,
            operation="insert",
            native_field=field,
            request_id=11,
            instrument_id="SA2701",
            exchange_id="CZCE",
            runtime_order_id=_ORDER_ID,
            managed_intent_id="entry-1",
            runtime_action_id=None,
            managed_cancel_intent_id=None,
        )
    assert len(native.calls) == 1
