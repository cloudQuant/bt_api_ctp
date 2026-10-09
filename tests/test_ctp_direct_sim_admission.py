"""Offline contract tests for the direct frozen-simulation write admission.

``arm_execution_for_registered_sim`` is the only direct (non-managed) write
entry.  It must accept exactly the frozen simulation registries — registered
broker simulation fronts and the official SimNow whitelist — and must keep
every other front, including near-miss endpoints, fenced.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import (
    CtpExecutionGateError,
    TraderClient,
    _TraderSpi,
    _issue_ctp_execution_authority_for_core,
)

_SIMNOW_TD = "tcp://182.254.243.31:30002"
_SIMNOW_MD = "tcp://182.254.243.31:30012"
_SIMNOW_TD_GROUP1 = "tcp://182.254.243.31:30001"
_SIMNOW_MD_GROUP1 = "tcp://182.254.243.31:30011"


class _CountingNativeApi:
    def __init__(self):
        self.calls = []

    def ReqOrderInsert(self, field, request_id):
        self.calls.append(("insert", field, request_id))
        return 0

    def ReqOrderAction(self, field, request_id):
        self.calls.append(("cancel", field, request_id))
        return 0


def _trading_ready_client(td_front):
    client = TraderClient(td_front, "3070", "offline-account", "unused")
    native = _CountingNativeApi()
    client._api = native
    client._session_native_api = native
    client._session_native_front = td_front
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
            TradingDay="20261009",
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
    return client, native


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


def _arm(client, md_front, *, instrument="SA2701", exchange="CZCE"):
    return client.arm_execution_for_registered_sim(
        instrument_id=instrument,
        exchange_id=exchange,
        md_front=md_front,
        strategy_identity_sha256="b" * 64,
        execution_cycle_id="cert-cycle-1",
        preflight_sha256="c" * 64,
    )


def test_official_simnow_pair_arms_and_dispatches_one_insert():
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, state = _arm(client, _SIMNOW_MD)
    assert state["armed"] is True

    field = _insert_field()
    result = client.submit_order_insert(field, field.RequestID, execution_capability=capability)
    assert result == 0
    assert len(native.calls) == 1
    assert native.calls[0][0] == "insert"


def test_official_simnow_pair_arms_without_md_front():
    client, _native = _trading_ready_client(_SIMNOW_TD_GROUP1)
    _capability, state = _arm(client, "")
    assert state["armed"] is True


def test_unregistered_front_cannot_arm():
    client, _native = _trading_ready_client("tcp://127.0.0.1:59999")
    with pytest.raises(CtpExecutionGateError, match="ctp_execution_gate_environment_unverified"):
        _arm(client, "")


def test_near_miss_official_port_cannot_arm():
    client, _native = _trading_ready_client("tcp://182.254.243.31:30004")
    with pytest.raises(CtpExecutionGateError, match="ctp_execution_gate_environment_unverified"):
        _arm(client, "")


def test_mismatched_md_front_cannot_arm():
    client, _native = _trading_ready_client(_SIMNOW_TD)
    with pytest.raises(CtpExecutionGateError, match="ctp_execution_gate_environment_unverified"):
        _arm(client, _SIMNOW_MD_GROUP1)


def test_official_simnow_stays_fenced_without_registered_admission():
    client, _native = _trading_ready_client(_SIMNOW_TD)
    capability = _issue_ctp_execution_authority_for_core()
    client.configure_execution_gate(capability)
    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        client._issue_execution_authorization_for_core(
            capability,
            {},
            environment_profile="set1_group2_vpn",
            environment_verified=True,
            strategy_identity_sha256="b" * 64,
            execution_cycle_id="cert-cycle-1",
        )


def test_rebound_front_loses_admission_before_dispatch():
    client, native = _trading_ready_client(_SIMNOW_TD)
    capability, _state = _arm(client, _SIMNOW_MD)

    # Simulate an identity rebind: the stored admission must not survive a
    # front that no longer matches any frozen registry.
    client._bound_front = "tcp://182.254.243.31:49999"
    with pytest.raises(CtpExecutionGateError):
        client.submit_order_insert(_insert_field(), 11, execution_capability=capability)
    assert native.calls == []
