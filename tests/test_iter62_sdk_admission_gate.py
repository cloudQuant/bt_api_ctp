"""Iteration 62: environment-neutral SDK write admission on the native gate.

The SDK decides admission (D62-01/02).  ``TraderClient`` keeps its native
capability / one-shot arm / per-write revalidation, but the SDK-admitted arm
path consults no front registry: a production-shaped front and an official
SimNow front behave the same once the SDK admitted writes for the session.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import (
    SDK_ADMITTED_PROFILE,
    CtpExecutionGateError,
    TraderClient,
    _issue_ctp_execution_authority_for_core,
    _TraderSpi,
)

_PRODUCTION_SHAPED_TD = "tcp://10.20.30.40:41205"
_SIMNOW_TD = "tcp://182.254.243.31:30001"


class _CountingNativeApi:
    def __init__(self):
        self.calls = []

    def ReqAuthenticate(self, field, request_id):
        return 0

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
            TradingDay="20261106",
            MaxOrderRef="7",
            SHFETime="21:30:05",
            CZCETime="21:30:05",
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
        "InstrumentID": "SA701",
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


def _admitted_and_armed(td_front):
    client, native = _trading_ready_client(td_front)
    client.record_sdk_write_admission()
    capability = _issue_ctp_execution_authority_for_core()
    client.configure_execution_gate(capability)
    state = client.arm_execution_for_sdk_admission(
        capability=capability,
        instrument_id="SA701",
        exchange_id="CZCE",
        strategy_identity_sha256="b" * 64,
        execution_cycle_id="iter62-cycle",
        preflight_sha256="c" * 64,
    )
    return client, native, capability, state


@pytest.mark.parametrize("td_front", [_PRODUCTION_SHAPED_TD, _SIMNOW_TD])
def test_sdk_admitted_session_arms_and_dispatches_regardless_of_environment(td_front):
    client, native, capability, state = _admitted_and_armed(td_front)
    assert state["armed"] is True
    field = _insert_field()
    assert client.submit_order_insert(field, field.RequestID, execution_capability=capability) == 0
    assert [call[0] for call in native.calls] == ["insert"]


def test_sdk_arm_without_admission_is_refused():
    client, _native = _trading_ready_client(_PRODUCTION_SHAPED_TD)
    capability = _issue_ctp_execution_authority_for_core()
    client.configure_execution_gate(capability)
    with pytest.raises(CtpExecutionGateError) as info:
        client.arm_execution_for_sdk_admission(
            capability=capability,
            instrument_id="SA701",
            exchange_id="CZCE",
            strategy_identity_sha256="b" * 64,
            execution_cycle_id="iter62-cycle",
            preflight_sha256="c" * 64,
        )
    assert str(info.value) == "ctp_execution_gate_environment_unverified"


def test_admission_is_bound_to_the_admitted_front():
    client, _native = _trading_ready_client(_PRODUCTION_SHAPED_TD)
    client.record_sdk_write_admission()
    with client._query_state_lock:
        assert client._direct_sim_admission_profile == SDK_ADMITTED_PROFILE
        assert client._direct_sim_admission_is_current_locked() is True
        client._sdk_admitted_front = "tcp://10.20.30.40:41206"
        assert client._direct_sim_admission_is_current_locked() is False
    client.clear_sdk_write_admission()
    with client._query_state_lock:
        assert client._direct_sim_admission_is_current_locked() is False


def test_execution_listener_receives_trade_returns_with_session_scope():
    client, _native = _trading_ready_client(_PRODUCTION_SHAPED_TD)
    received = []
    client.add_execution_listener(lambda kind, payload: received.append((kind, payload)))
    client._push_trade_event(
        SimpleNamespace(InstrumentID="SA701", TradeID="9", OrderRef="000000000101", Volume=1)
    )
    assert received and received[0][0] == "trade"
    payload = received[0][1]
    assert payload["TradeID"] == "9"
    assert payload["_login_front_id"] == 1 and payload["_login_session_id"] == 2
    assert payload["_trading_day"] == "20261106"


def test_listener_failure_does_not_break_native_return_handling():
    client, _native = _trading_ready_client(_PRODUCTION_SHAPED_TD)

    def broken(kind, payload):
        raise RuntimeError("listener bug")

    client.add_execution_listener(broken)
    client._push_trade_event(SimpleNamespace(InstrumentID="SA701", TradeID="9"))
    assert client._trade_events.qsize() == 1


def test_login_clock_observation_records_exchange_times():
    client, _native = _trading_ready_client(_PRODUCTION_SHAPED_TD)
    observation = client.login_clock_observation()
    assert observation["exchange_times"]["SHFE"] == "21:30:05"
    assert observation["exchange_times"]["CZCE"] == "21:30:05"
    assert isinstance(observation["local_received_ns"], int)
    assert observation["trading_day"] == "20261106"


def test_private_topic_defaults_to_quick_and_is_configurable():
    client = TraderClient(_PRODUCTION_SHAPED_TD, "3070", "offline-account", "unused")
    assert client.private_topic_resume_type == 2
    client.private_topic_resume_type = 0
    assert client.private_topic_resume_type == 0
