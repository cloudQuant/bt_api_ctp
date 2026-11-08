"""Synthetic CTP trade callback source fences; no native session or network."""

import threading
from types import SimpleNamespace

import pytest

import bt_api_ctp.ctp.client as client_module
from bt_api_ctp.ctp.client import TraderClient, _TraderSpi


def _client():
    client = TraderClient("tcp://offline.invalid:1", "broker", "investor", "fixture")
    api = object()
    client._api = api
    spi = _TraderSpi(client, api)
    client._spi = spi
    with client._query_state_lock:
        client._connection_generation = 7
        client._trading_day = "20261108"
        client._front_id = 3
        client._session_id = 4
    return client, api, spi


def _trade():
    return SimpleNamespace(InstrumentID="rb2701", TradeID="trade-old", Volume=1)


@pytest.mark.parametrize("change", ["api_spi", "same_spi_relogin"])
def test_late_trade_never_inherits_replacement_session(monkeypatch, change):
    client, old_api, old_spi = _client()
    entered = threading.Event()
    resume = threading.Event()
    events, callbacks = [], []
    client.add_execution_listener(lambda kind, payload: events.append((kind, payload)))
    client.on_trade = callbacks.append
    original = client._push_trade_event

    def pause(field, **kwargs):
        entered.set()
        assert resume.wait(2)
        return original(field, **kwargs)

    monkeypatch.setattr(client, "_push_trade_event", pause)
    thread = threading.Thread(target=old_spi.OnRtnTrade, args=(_trade(),), daemon=True)
    thread.start()
    try:
        assert entered.wait(1)
        with client._query_state_lock:
            if change == "api_spi":
                replacement = object()
                client._api = replacement
                client._spi = _TraderSpi(client, replacement)
            else:
                assert client._api is old_api and client._spi is old_spi
            client._connection_generation = 8
            client._trading_day = "20261109"
            client._front_id = 5
            client._session_id = 6
    finally:
        resume.set()
        thread.join(2)
    assert not thread.is_alive()
    assert events == [] and callbacks == []
    assert client.wait_trade_event(timeout=0) is None


def test_current_trade_has_matching_snapshot_scope_and_listeners_run_outside_lock():
    client, _api, spi = _client()
    observed, callbacks = [], []

    def listener(kind, payload):
        obtained = threading.Event()

        def acquire():
            with client._query_state_lock:
                obtained.set()

        helper = threading.Thread(target=acquire, daemon=True)
        helper.start()
        assert obtained.wait(1), "listener ran with query state lock held"
        helper.join(1)
        observed.append((kind, payload))

    client.add_execution_listener(listener)
    client.on_trade = callbacks.append
    trade = _trade()
    spi.OnRtnTrade(trade)
    assert callbacks == [trade]
    assert len(observed) == 1 and observed[0][0] == "trade"
    payload = observed[0][1]
    assert (
        payload["TradeID"],
        payload["_connection_generation"],
        payload["_trading_day"],
        payload["_login_front_id"],
        payload["_login_session_id"],
    ) == ("trade-old", 7, "20261108", 3, 4)
    assert client.wait_trade_event(timeout=0)["TradeID"] == "trade-old"


def test_synthetic_direct_helper_remains_usable():
    client, _api, _spi = _client()
    seen = []
    client.add_execution_listener(lambda kind, payload: seen.append((kind, payload)))
    client._push_trade_event(_trade())
    assert seen[0][0] == "trade" and seen[0][1]["TradeID"] == "trade-old"


def test_trade_snapshot_and_scope_share_the_generation_lock(monkeypatch):
    client, _api, spi = _client()
    entered, resume, attempted, replaced = (threading.Event() for _ in range(4))
    events = []
    client.add_execution_listener(lambda kind, payload: events.append((kind, payload)))
    snapshot = client_module._snapshot_ctp_field

    def paused(field):
        entered.set()
        assert resume.wait(2)
        return snapshot(field)

    monkeypatch.setattr(client_module, "_snapshot_ctp_field", paused)
    callback = threading.Thread(target=spi.OnRtnTrade, args=(_trade(),), daemon=True)
    callback.start()
    assert entered.wait(1)

    def replace():
        attempted.set()
        with client._query_state_lock:
            api = object()
            client._api = api
            client._spi = _TraderSpi(client, api)
            client._connection_generation = 8
            client._trading_day = "20261109"
        replaced.set()

    changer = threading.Thread(target=replace, daemon=True)
    changer.start()
    try:
        assert attempted.wait(1)
        assert not replaced.wait(0.05)
    finally:
        resume.set()
        callback.join(2)
        changer.join(2)
    assert not callback.is_alive() and not changer.is_alive() and replaced.is_set()
    assert len(events) == 1
    assert (events[0][1]["_connection_generation"], events[0][1]["_trading_day"]) == (7, "20261108")


def test_reentrant_native_getter_replacement_is_rechecked(monkeypatch):
    client, _api, spi = _client()
    events, callbacks = [], []
    client.add_execution_listener(lambda kind, payload: events.append((kind, payload)))
    client.on_trade = callbacks.append
    snapshot = client_module._snapshot_ctp_field

    def replace_during_snapshot(field):
        api = object()
        client._api = api
        client._spi = _TraderSpi(client, api)
        client._connection_generation = 8
        client._trading_day = "20261109"
        return snapshot(field)

    monkeypatch.setattr(client_module, "_snapshot_ctp_field", replace_during_snapshot)
    spi.OnRtnTrade(_trade())
    assert events == [] and callbacks == []
    assert client.wait_trade_event(timeout=0) is None


def test_session_api_scope_uses_identity_not_equal_value(monkeypatch):
    class EqualApi:
        def __eq__(self, other):
            return isinstance(other, EqualApi)

    client, _api, spi = _client()
    old_session, new_session = EqualApi(), EqualApi()
    client._session_native_api = old_session
    entered, resume = threading.Event(), threading.Event()
    events = []
    client.add_execution_listener(lambda kind, payload: events.append((kind, payload)))
    original = client._push_trade_event

    def pause(field, **kwargs):
        entered.set()
        assert resume.wait(2)
        return original(field, **kwargs)

    monkeypatch.setattr(client, "_push_trade_event", pause)
    thread = threading.Thread(target=spi.OnRtnTrade, args=(_trade(),), daemon=True)
    thread.start()
    try:
        assert entered.wait(1)
        with client._query_state_lock:
            client._session_native_api = new_session
    finally:
        resume.set()
        thread.join(2)
    assert not thread.is_alive() and events == []
    assert client.wait_trade_event(timeout=0) is None
