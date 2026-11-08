"""Configured front pair honors SDK admission while retaining native fences."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import (
    CtpExecutionGateError,
    TraderClient,
    _issue_ctp_execution_authority_for_core,
    _TraderSpi,
)
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

TD = "tcp://10.20.30.40:41205"
MD = "tcp://10.20.30.40:41206"


class _CountingNativeApi:
    def __init__(self):
        self.calls = []

    def ReqOrderInsert(self, field, request_id):
        self.calls.append(("insert", field, request_id))
        return 0

    def ReqOrderAction(self, field, request_id):
        self.calls.append(("cancel", field, request_id))
        return 0


def _pair():
    feed = CtpRequestDataFuture(
        td_front=TD,
        md_front=MD,
        ctp_env_profile="config_front_pair",
        broker_id="3070",
        user_id="offline-acct",
        password="unused",
    )
    trader = TraderClient(
        TD, "3070", "offline-acct", "unused", md_front=MD, ctp_env_profile="config_front_pair"
    )
    native = _CountingNativeApi()
    trader._api = native
    trader._session_native_api = native
    trader._session_native_front = TD
    trader._connected = True
    trader._authentication_state = "authenticated"
    trader._login_state = "logging_in"
    trader._connection_generation = 7
    trader._login_request_id = 1
    trader._login_connection_generation = 7
    _TraderSpi(trader).OnRspUserLogin(
        SimpleNamespace(
            BrokerID="3070",
            UserID="offline-acct",
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
    trader._settlement_state = "confirmed"
    trader._settlement_readback_verified = True
    trader._settlement_account_fingerprint = trader._account_fingerprint
    trader._settlement_trading_day = trader._trading_day
    trader._settlement_connection_generation = 7
    trader._ready = True
    trader._has_current_settlement_readback_locked = lambda: True
    feed._trader = trader
    capability = _issue_ctp_execution_authority_for_core()
    feed.configure_execution_gate(capability)
    feed.activate_sdk_write_admission()
    trader.arm_execution_for_sdk_admission(
        capability=capability,
        instrument_id="SA701",
        exchange_id="CZCE",
        strategy_identity_sha256="b" * 64,
        execution_cycle_id="iter63-cycle",
        preflight_sha256="c" * 64,
    )
    return feed, trader, native, capability


def test_configured_pair_sdk_admission_reaches_real_trader_insert_and_cancel():
    feed, _trader, native, capability = _pair()
    feed.make_order(
        "SA701",
        1,
        price=1000,
        client_order_id="000000000101",
        exchange_id="CZCE",
        _execution_capability=capability,
    )
    feed.cancel_order(
        "SA701", order_id="sys-1", exchange_id="CZCE", _execution_capability=capability
    )
    assert [call[0] for call in native.calls] == ["insert", "cancel"]


def _make_order(feed, capability, symbol="SA701", exchange_id="CZCE"):
    return feed.make_order(
        symbol,
        1,
        price=1000,
        client_order_id="000000000101",
        exchange_id=exchange_id,
        _execution_capability=capability,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("td_front", "tcp://10.20.30.40:41999"),
        ("md_front", "tcp://10.20.30.40:41998"),
        ("broker_id", "other"),
        ("user_id", "other"),
        ("ctp_env_profile", "other"),
    ],
)
def test_changed_feed_route_is_rejected_before_native(field, value):
    feed, _trader, native, capability = _pair()
    setattr(feed, field, value)
    with pytest.raises(CtpExecutionGateError):
        _make_order(feed, capability)
    assert native.calls == []


@pytest.mark.parametrize(
    "case",
    [
        "capability",
        "instrument",
        "exchange",
        "generation",
        "trading_day",
        "native_account",
        "native_front",
    ],
)
def test_final_trader_fences_remain_required(case):
    feed, trader, native, capability = _pair()
    symbol, exchange = "SA701", "CZCE"
    if case == "capability":
        capability = object()
    elif case == "instrument":
        symbol = "SA702"
    elif case == "exchange":
        exchange = "SHFE"
    elif case == "generation":
        trader._connection_generation += 1
    elif case == "trading_day":
        trader._trading_day = "20261107"
    elif case == "native_account":
        trader.user_id = "other"
    else:
        trader._session_native_front = "tcp://10.20.30.40:41999"
    with pytest.raises(CtpExecutionGateError):
        _make_order(feed, capability, symbol, exchange)
    assert native.calls == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("sdk_write_admission", False),
        ("sdk_write_admission", None),
        ("sdk_write_admission", 1),
        ("armed", False),
        ("armed", 1),
        ("environment_profile", "other"),
        ("runtime_simnow_credential_binding_configured", True),
        ("runtime_simnow_credential_binding_configured", None),
        ("runtime_simnow_write_verifier_configured", True),
        ("runtime_simnow_write_verifier_configured", None),
    ],
)
def test_mixed_or_malformed_native_state_never_falls_back(key, value):
    feed, trader, native, capability = _pair()
    native_state = trader.get_execution_gate_state()
    native_state[key] = value
    trader.get_execution_gate_state = lambda: native_state
    with pytest.raises(CtpExecutionGateError):
        _make_order(feed, capability)
    assert native.calls == []


@pytest.mark.parametrize("feed_active", [False, None, 1])
def test_feed_sdk_admission_mismatch_is_rejected(feed_active):
    feed, _trader, native, capability = _pair()
    feed.sdk_write_admission_active = lambda: feed_active
    with pytest.raises(CtpExecutionGateError):
        _make_order(feed, capability)
    assert native.calls == []


def test_feed_flag_and_accessor_disagree_fails_closed():
    feed, _trader, native, capability = _pair()
    feed._sdk_write_admission_active = False
    feed.sdk_write_admission_active = lambda: True
    with pytest.raises(CtpExecutionGateError):
        _make_order(feed, capability)
    assert native.calls == []


def test_cancel_also_refuses_stale_session_before_native():
    feed, trader, native, capability = _pair()
    trader._connection_generation += 1
    with pytest.raises(CtpExecutionGateError):
        feed.cancel_order(
            "SA701", order_id="sys-1", exchange_id="CZCE", _execution_capability=capability
        )
    assert native.calls == []


def test_managed_disarmed_pair_retains_existing_gate_behavior():
    feed, trader, native, capability = _pair()
    feed._sdk_write_admission_active = False
    state = trader.get_execution_gate_state()
    state.update(
        {
            "sdk_write_admission": False,
            "armed": False,
            "environment_profile": None,
            "runtime_simnow_credential_binding_configured": True,
            "runtime_simnow_write_verifier_configured": True,
        }
    )
    trader.get_execution_gate_state = lambda: state
    calls = []
    trader.require_execution_write = lambda *args: calls.append(args)
    feed._ensure_execution_permitted(capability, "SA701", "CZCE")
    assert calls == [] and native.calls == []


_MANAGED_IDENTITIES = {
    "runtime_order_id": "bt-managed-v1:" + "a" * 64,
    "managed_intent_id": "intent.order-1",
    "runtime_action_id": "cancel.action-1",
    "managed_cancel_intent_id": "cancel.action-1",
}


@pytest.mark.parametrize("operation", ["insert", "cancel"])
@pytest.mark.parametrize("identity", [*_MANAGED_IDENTITIES, "all", "empty"])
def test_sdk_feed_rejects_managed_identity_even_partial(operation, identity):
    feed, _trader, native, capability = _pair()
    kwargs = (
        dict(_MANAGED_IDENTITIES)
        if identity == "all"
        else {"runtime_order_id": ""}
        if identity == "empty"
        else {identity: _MANAGED_IDENTITIES[identity]}
    )
    with pytest.raises(CtpExecutionGateError, match="ctp_sdk_managed_identity_forbidden"):
        if operation == "insert":
            feed.make_order(
                "SA701",
                1,
                price=1000,
                client_order_id="000000000101",
                exchange_id="CZCE",
                _execution_capability=capability,
                **kwargs,
            )
        else:
            feed.cancel_order(
                "SA701",
                order_id="SYS-17",
                order_ref="000000000017",
                front_id=1,
                session_id=2,
                exchange_id="CZCE",
                _execution_capability=capability,
                **kwargs,
            )
    assert native.calls == []


@pytest.mark.parametrize("operation", ["insert", "cancel"])
def test_sdk_feed_accepts_explicit_none_managed_identities(operation):
    feed, _trader, native, capability = _pair()
    kwargs = dict.fromkeys(_MANAGED_IDENTITIES)
    if operation == "insert":
        feed.make_order(
            "SA701",
            1,
            price=1000,
            client_order_id="000000000101",
            exchange_id="CZCE",
            _execution_capability=capability,
            **kwargs,
        )
    else:
        feed.cancel_order(
            "SA701",
            order_id="SYS-17",
            exchange_id="CZCE",
            _execution_capability=capability,
            **kwargs,
        )
    assert [call[0] for call in native.calls] == [operation]


@pytest.mark.parametrize("identity", ["runtime_order_id", "managed_intent_id", "both"])
def test_direct_trader_insert_rejects_managed_identity(identity):
    _feed, trader, native, capability = _pair()
    field = SimpleNamespace(InstrumentID="SA701", ExchangeID="CZCE")
    kwargs = {
        key: _MANAGED_IDENTITIES[key]
        for key in ("runtime_order_id", "managed_intent_id")
        if identity in ("both", key)
    }
    with pytest.raises(CtpExecutionGateError, match="ctp_sdk_managed_identity_forbidden"):
        trader.submit_order_insert(field, 11, execution_capability=capability, **kwargs)
    assert native.calls == []


@pytest.mark.parametrize("identity", [*_MANAGED_IDENTITIES, "all"])
def test_direct_trader_cancel_rejects_managed_identity(identity):
    _feed, trader, native, capability = _pair()
    field = SimpleNamespace(
        BrokerID="3070",
        InvestorID="offline-acct",
        UserID="offline-acct",
        InstrumentID="SA701",
        ExchangeID="CZCE",
        OrderRef="000000000017",
        OrderSysID="SYS-17",
        FrontID=1,
        SessionID=2,
        RequestID=11,
        OrderActionRef=11,
        ActionFlag="0",
    )
    kwargs = (
        dict(_MANAGED_IDENTITIES)
        if identity == "all"
        else {identity: _MANAGED_IDENTITIES[identity]}
    )
    expected = "ctp_sdk_managed_identity_forbidden" if identity == "all" else "ctp_"
    with pytest.raises(CtpExecutionGateError, match=expected):
        trader.submit_order_action(field, 11, execution_capability=capability, **kwargs)
    assert native.calls == []


@pytest.mark.parametrize("operation", ["insert", "cancel"])
def test_direct_trader_explicit_none_identities_pass(operation):
    feed, _trader, native, capability = _pair()
    if operation == "insert":
        _make_order(feed, capability)
    else:
        feed.cancel_order(
            "SA701", order_id="SYS-17", exchange_id="CZCE", _execution_capability=capability
        )
    _, field, request_id = native.calls[0]
    _feed, trader, fresh_native, fresh_capability = _pair()
    if operation == "insert":
        trader.submit_order_insert(
            field,
            request_id,
            execution_capability=fresh_capability,
            runtime_order_id=None,
            managed_intent_id=None,
        )
    else:
        trader.submit_order_action(
            field,
            request_id,
            execution_capability=fresh_capability,
            **dict.fromkeys(_MANAGED_IDENTITIES),
        )
    assert [call[0] for call in fresh_native.calls] == [operation]
