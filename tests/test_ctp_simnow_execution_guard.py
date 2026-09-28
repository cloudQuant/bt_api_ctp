from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest

from bt_api_ctp import ctp_env_selector
from bt_api_ctp.ctp.client import (
    CtpExecutionGateError,
    CtpRuntimeSimNowCredentialBinding,
    TraderClient,
    _issue_ctp_execution_authority_for_core,
)
from bt_api_ctp.ctp_env_selector import (
    is_official_simnow_td_front,
    official_simnow_fronts,
    registered_broker_sim_fronts,
)
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestData

_SIMNOW_PROFILES = (
    "set1_group1",
    "set1_group1_vpn",
    "set1_group2",
    "set2_7x24",
    "set2_7x24_4000x",
    "set2_7x24_vpn",
)


def test_ctp_client_import_supports_python39_dataclass_signature() -> None:
    """Python 3.9's dataclass decorator does not accept the 3.10 ``slots`` option."""
    project_root = Path(__file__).resolve().parents[1]
    sibling_root = project_root.parent
    source_path = os.pathsep.join(
        str(path)
        for path in (
            project_root / "src",
            sibling_root / "bt_api_base" / "src",
        )
    )
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_path + (os.pathsep + existing if existing else "")
    script = r'''
import dataclasses

native_dataclass = dataclasses.dataclass

def python39_dataclass(cls=None, *, init=True, repr=True, eq=True, order=False,
                       unsafe_hash=False, frozen=False):
    return native_dataclass(
        cls, init=init, repr=repr, eq=eq, order=order,
        unsafe_hash=unsafe_hash, frozen=frozen,
    )

dataclasses.dataclass = python39_dataclass
import bt_api_ctp.ctp.client
'''
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )


class _CountingNativeApi:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if not name.startswith("Req"):
            raise AttributeError(name)

        def call(*_args):
            self.calls.append(name)
            return 0

        return call


class _CountingTrader:
    def __init__(self):
        self.calls = []
        self.auto_settlement_confirm = False

    def arm_execution_gate(self, *_args, **_kwargs):
        self.calls.append("arm")
        return {}

    def _issue_execution_authorization_for_core(self, *_args, **_kwargs):
        self.calls.append("execution_issuer")
        return object()

    def _issue_settlement_authorization_for_core(self, *_args, **_kwargs):
        self.calls.append("settlement_issuer")
        return object()

    def make_order(self, *_args, **_kwargs):
        self.calls.append("make_order")

    def cancel_order(self, *_args, **_kwargs):
        self.calls.append("cancel_order")

    def confirm_settlement(self, *_args, **_kwargs):
        self.calls.append("settlement")


def _feed(front, capability, trader):
    feed = object.__new__(CtpRequestData)
    feed._execution_bound_td_front = front
    feed._execution_gate_capability = capability
    feed._connect_lock = RLock()
    feed._trader = trader
    feed.auto_settlement_confirm = False
    feed.broker_id = "3070"
    feed.user_id = "offline-fake"
    feed.asset_type = "FUTURE"
    return feed


@pytest.mark.parametrize("profile", _SIMNOW_PROFILES)
def test_every_official_simnow_front_blocks_feed_and_native_write_issuers(profile):
    td_front, _md_front = official_simnow_fronts(profile)
    assert is_official_simnow_td_front(td_front)

    capability = _issue_ctp_execution_authority_for_core()
    native_api = _CountingNativeApi()
    trader = TraderClient(td_front, "3070", "offline-fake", "unused")
    trader._api = native_api
    trader._session_native_api = native_api
    trader._session_native_front = td_front
    trader._connected = True
    trader._connection_generation = 1
    trader._trading_day = "20260923"
    trader.configure_execution_gate(capability)

    feed_trader = _CountingTrader()
    feed = _feed(td_front, capability, feed_trader)

    # The feed must not mint the SDK's core authority for a SimNow profile.
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        feed._issue_execution_capability_for_core()

    # Feed issuer/arm/settlement and ordinary order/cancel entry points all
    # stop before delegating to a client or native API.
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        feed._issue_execution_authorization_for_core(
            capability,
            {},
            strategy_identity_sha256="0" * 64,
            execution_cycle_id="offline-cycle",
        )
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        feed._issue_settlement_authorization_for_core(capability)
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        feed.arm_execution_gate(capability, object())
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        feed.make_order("SA2701", 1, price=1000, _execution_capability=capability)
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        feed.cancel_order("SA2701", order_id="123", _execution_capability=capability)
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        feed.confirm_settlement(
            _execution_capability=capability,
            _settlement_authorization=object(),
        )

    # The native client also rejects direct issuer, arm, order/cancel and
    # settlement calls, so bypassing the feed's wrappers does not reach CTP.
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        trader._issue_execution_authorization_for_core(
            capability,
            {},
            environment_profile=profile,
            environment_verified=True,
            strategy_identity_sha256="0" * 64,
            execution_cycle_id="offline-cycle",
        )
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        trader._issue_settlement_authorization_for_core(
            capability,
            environment_profile=profile,
            environment_verified=True,
        )
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        trader.arm_execution_gate(capability, object())
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        trader.arm_execution_for_registered_sim(
            instrument_id="SA2701",
            exchange_id="CZCE",
            strategy_identity_sha256="0" * 64,
            execution_cycle_id="offline-cycle",
            preflight_sha256="1" * 64,
        )
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        trader.submit_order_insert(SimpleNamespace(), 1, execution_capability=capability)
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        trader.submit_order_action(SimpleNamespace(), 2, execution_capability=capability)
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        trader._request_settlement_confirmation(
            execution_capability=capability,
            settlement_authorization=object(),
            settlement_environment_profile=profile,
            settlement_environment_verified=True,
        )
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        trader.confirm_settlement(
            _execution_capability=capability,
            _settlement_authorization=object(),
            _settlement_environment_profile=profile,
            _settlement_environment_verified=True,
        )

    assert feed_trader.calls == []
    assert native_api.calls == []


def test_official_simnow_classifier_does_not_reject_other_registered_ctp_profile():
    hongyuan_td_front, _md_front = registered_broker_sim_fronts("hongyuan_sim_telecom")
    assert not is_official_simnow_td_front(hongyuan_td_front)

    client = TraderClient(hongyuan_td_front, "3070", "offline-fake", "unused")
    client._reject_official_simnow_write_locked()


def test_runtime_simnow_binding_is_typed_and_default_native_writes_stay_closed():
    td_front, _md_front = official_simnow_fronts("set1_group1")
    native_api = _CountingNativeApi()
    client = TraderClient(td_front, "3070", "offline-fake", "unused")
    client._api = native_api

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_simnow_execution_not_admitted",
    ):
        client.submit_order_insert(
            SimpleNamespace(InstrumentID="SA2701", ExchangeID="CZCE"),
            1,
            runtime_order_id="bt-managed-v1:" + "a" * 64,
            managed_intent_id="entry-1",
        )

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_simnow_credential_binding_required",
    ):
        client.configure_runtime_simnow_credential_binding(object(), object())
    assert native_api.calls == []


def test_simnow_binding_refresh_runs_at_native_boundary_and_stale_hmac_cannot_write():
    import hashlib
    from types import MappingProxyType

    from bt_api_py._ctp_credential_binding import _new_scope, _new_test_verifier
    from bt_api_py._ctp_execution_authorization import (
        _CAPABILITY_SEAL,
        SIMNOW_ENTRY_APPROVAL_SCHEMA_VERSION,
        CtpExecutionApproval,
        _new_runtime_context,
    )

    td_front, md_front = official_simnow_fronts("set1_group1")
    native_api = _CountingNativeApi()
    client = TraderClient(td_front, "3070", "offline-fake", "unused")
    client._api = native_api
    owner = SimpleNamespace(exchange_feeds={})
    refresh_count = [0]

    def binding_provider():
        refresh_count[0] += 1
        return {
            "credential_binding_key_id": "offline-key",
            "credential_binding_hmac_sha256": str(refresh_count[0]) * 64,
        }

    verifier = _new_test_verifier(owner, binding_provider)
    scope_values = {
        "account_fingerprint": "acct_offline",
        "trading_day": "20260923",
        "connection_generation": 1,
        "environment_profile": "set1_group1",
        "td_front": td_front,
        "md_front": md_front,
        "td_front_sha256": hashlib.sha256(td_front.encode()).hexdigest(),
        "md_front_sha256": hashlib.sha256(md_front.encode()).hexdigest(),
        "md_connection_generation": 1,
        "md_stream_generation": 1,
    }
    for name in (
        "backtrader_sha256",
        "backtrader_runtime_sha256",
        "bt_api_py_sha256",
        "bt_api_ctp_sha256",
        "bt_api_base_sha256",
        "native_sha256",
        "dependency_hashes_sha256",
        "configuration_sha256",
        "strategy_identity_sha256",
        "preflight_sha256",
        "evidence_sha256",
    ):
        scope_values[name] = "0" * 64
    scope = _new_scope(scope_values)
    approved_binding = verifier.refresh(scope, owner=owner, operation="test")
    context_values = {
        "credential_binding_key_id": approved_binding["credential_binding_key_id"],
        "credential_binding_hmac_sha256": approved_binding[
            "credential_binding_hmac_sha256"
        ],
    }
    self = owner
    credential_binding_verifier = verifier

    def refresh_context():
        refreshed_binding = credential_binding_verifier.refresh(
            scope, owner=self, operation="test"
        )
        return _new_runtime_context(
            {**context_values, **refreshed_binding},
            owner=self,
            refresh=refresh_context,
        )

    context = _new_runtime_context(
        context_values,
        owner=owner,
        refresh=refresh_context,
    )
    approval = object.__new__(CtpExecutionApproval)
    object.__setattr__(
        approval,
        "payload",
        MappingProxyType(
            {
                "schema_version": SIMNOW_ENTRY_APPROVAL_SCHEMA_VERSION,
                **context_values,
            }
        ),
    )
    object.__setattr__(approval, "_seal", _CAPABILITY_SEAL)
    client._runtime_simnow_credential_binding = CtpRuntimeSimNowCredentialBinding(
        owner=owner,
        approval_context=context,
        approval=approval,
        credential_binding_verifier=verifier,
        td_front=td_front,
        md_front=md_front,
        environment_profile="set1_group1",
    )

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_simnow_credential_binding_rejected",
    ):
        client.submit_order_insert(
            SimpleNamespace(InstrumentID="SA2701", ExchangeID="CZCE"),
            1,
            runtime_order_id="bt-managed-v1:" + "a" * 64,
            managed_intent_id="entry-1",
        )

    assert refresh_count[0] == 2
    assert native_api.calls == []

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_simnow_credential_binding_rejected",
    ):
        client.submit_order_action(
            SimpleNamespace(InstrumentID="SA2701", ExchangeID="CZCE"),
            2,
            runtime_order_id="bt-managed-v1:" + "a" * 64,
            managed_intent_id="entry-1",
            runtime_action_id="sdk-action-2",
            managed_cancel_intent_id="cancel.entry-1",
        )

    assert refresh_count[0] == 3
    assert native_api.calls == []


def test_front_binding_state_only_reports_current_callback_registered_front():
    td_front, _md_front = official_simnow_fronts("set1_group1")
    client = TraderClient(td_front, "3070", "offline-fake", "unused")
    native_api = _CountingNativeApi()
    client._api = native_api

    before = client.get_front_binding_state()
    assert before == {
        "configured_front": td_front,
        "registered_front": None,
        "connection_confirmed_front": None,
        "connected": False,
        "connection_generation": 0,
        "native_api_current": False,
        "bound_identity_current": False,
    }

    client._on_front_connected()
    connected = client.get_front_binding_state()
    assert connected["configured_front"] == td_front
    assert connected["registered_front"] == td_front
    assert connected["connection_confirmed_front"] == td_front
    assert connected["connected"] is True
    assert connected["connection_generation"] == 1
    assert connected["native_api_current"] is True
    assert connected["bound_identity_current"] is True

    client.front = "tcp://changed.invalid:10000"
    changed = client.get_front_binding_state()
    assert changed["configured_front"] == td_front
    assert changed["connection_confirmed_front"] == td_front
    assert changed["bound_identity_current"] is False


def test_trader_client_raw_api_attribute_is_the_gated_view():
    td_front, _md_front = official_simnow_fronts("set1_group1")
    native_api = _CountingNativeApi()
    client = TraderClient(td_front, "3070", "offline-fake", "unused")
    client._api = native_api

    feed = _feed(td_front, object(), _CountingTrader())
    feed._trader = client
    exposed_api = feed.trader_client._api
    assert exposed_api is client.api

    for method_name in (
        "ReqOrderInsert",
        "ReqOrderAction",
        "ReqSettlementInfoConfirm",
        "ReqParkedOrderInsert",
        "ReqFromBankToFutureByFuture",
    ):
        with pytest.raises(CtpExecutionGateError, match="ctp_execution_gate_native_write_blocked"):
            getattr(exposed_api, method_name)(SimpleNamespace(), 1)

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_execution_gate_native_api_view_read_only",
    ):
        exposed_api.ReqOrderInsert = lambda *_args: 0
    assert native_api.calls == []


def test_official_simnow_front_classifier_survives_profile_map_mutation_attempts():
    td_front, _md_front = official_simnow_fronts("set1_group1")

    with pytest.raises(TypeError):
        ctp_env_selector._SIMNOW_PROFILE_FRONTS["attacker"] = ("tcp://elsewhere", "tcp://elsewhere")
    with pytest.raises(AttributeError):
        ctp_env_selector._SIMNOW_PROFILE_FRONTS.clear()

    assert is_official_simnow_td_front(td_front)
