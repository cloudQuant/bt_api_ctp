"""Synthetic read-only CTP cancel readiness; no account or native network."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import replace

import pytest
from bt_api_base.direct_write_protocol import DirectWriteError
from test_iter63_prepared_cancel import ObservedLock, make_system


@pytest.fixture
def ready(monkeypatch):
    provider, trader, native = make_system(monkeypatch)
    calls = []

    def forbid(name):
        def fail(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError("readiness attempted an external operation")

        return fail

    for name in (
        "ensure_read_only_connected",
        "_cancel_order_native",
        "activate_sdk_write_admission",
    ):
        monkeypatch.setattr(provider._feed, name, forbid(name))
    for name in (
        "_next_request_id",
        "submit_order_action",
        "verify_settlement_confirmation",
        "query_orders_result",
    ):
        monkeypatch.setattr(trader, name, forbid(name))
    monkeypatch.setattr(provider, "_admitted_capability", forbid("capability"))
    monkeypatch.setattr(provider, "_ensure_armed", forbid("arm"))
    monkeypatch.setattr(native, "ReqOrderAction", forbid("native"))
    return provider, trader, native, calls


def test_valid_login_requires_no_connect_query_settlement_or_admission(ready):
    provider, trader, _, calls = ready
    trader._settlement_state = "failed"
    trader._ready = False
    assert provider.assert_cancel_ready() is None
    assert (
        provider.assert_cancel_ready(expected_account_binding=trader._account_fingerprint) is None
    )
    assert calls == []


def test_pinned_account_rejects_a_different_self_consistent_login(ready):
    provider, trader, _, calls = ready
    expected = trader._account_fingerprint
    replacement = "another-synthetic-account"
    trader.user_id = trader._bound_user_id = provider._feed.user_id = replacement
    trader._account_fingerprint = hashlib.sha256(
        (trader._bound_broker_id + ":" + replacement).encode("utf-8")
    ).hexdigest()[:16]
    trader._login_identity_observation = replace(
        trader._login_identity_observation, user_id=replacement
    )
    # This fixture models a separately self-consistent login so the failure
    # specifically exercises the original, caller-pinned account binding.
    assert provider.assert_cancel_ready() is None
    with pytest.raises(DirectWriteError) as exc:
        provider.assert_cancel_ready(expected_account_binding=expected)
    assert exc.value.code == "ctp_prepared_cancel_session_unavailable"
    assert calls == []


@pytest.mark.parametrize("binding", ["", " x ", "x" * 257, "\ud800", True, 1])
def test_invalid_account_binding_is_rejected_locally(ready, binding):
    provider, _, _, calls = ready
    with pytest.raises(DirectWriteError) as exc:
        provider.assert_cancel_ready(expected_account_binding=binding)
    assert exc.value.code == "ctp_prepared_cancel_session_unavailable"
    assert calls == []


def test_missing_trader_never_implicitly_connects(ready):
    provider, _, _, calls = ready
    provider._allow_implicit_connect = True
    provider._feed._trader = None
    with pytest.raises(DirectWriteError) as exc:
        provider.assert_cancel_ready()
    assert exc.value.code == "ctp_prepared_cancel_session_unavailable"
    assert calls == []


@pytest.mark.parametrize("kind", ["disconnected", "api", "spi", "account", "trader", "session"])
def test_untrusted_or_disconnected_login_rejected_without_external_calls(ready, kind):
    provider, trader, _, calls = ready
    if kind == "disconnected":
        trader._connected = False
    elif kind == "api":
        trader._session_native_api = object()
    elif kind == "spi":
        trader._spi = object()
    elif kind == "account":
        trader._bound_user_id = "other-synthetic-account"
    elif kind == "trader":
        provider._bound_gateway_trader = object()
    else:
        trader._front_id = 0
    with pytest.raises(DirectWriteError) as exc:
        provider.assert_cancel_ready()
    assert exc.value.code == "ctp_prepared_cancel_session_unavailable"
    assert calls == []


def test_owner_thread_and_pid_rejected_before_lock(ready):
    provider, _, _, calls = ready
    errors = []

    def wrong_thread():
        try:
            provider.assert_cancel_ready()
        except DirectWriteError as exc:
            errors.append(exc.code)

    thread = threading.Thread(target=wrong_thread)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors == ["ctp_direct_wrong_owner"]
    provider._settlement_owner_pid = -1
    with pytest.raises(DirectWriteError) as exc:
        provider.assert_cancel_ready()
    assert exc.value.code == "ctp_direct_wrong_owner"
    assert calls == []


def test_owner_begin_close_and_provider_close_rejected(ready):
    provider, _, _, calls = ready
    provider._owner_thread_only = True
    provider._begin_close()
    with pytest.raises(DirectWriteError) as exc:
        provider.assert_cancel_ready()
    assert exc.value.code == "ctp_direct_provider_closed"
    assert calls == []


def test_non_owner_only_provider_after_close_rejected(ready):
    provider, _, _, calls = ready
    assert provider._owner_thread_only is False
    provider.close()
    with pytest.raises(DirectWriteError) as exc:
        provider.assert_cancel_ready()
    assert exc.value.code == "ctp_direct_provider_closed"
    assert calls == []


def test_closing_while_waiting_for_trader_lock_is_rechecked(ready):
    provider, trader, _, calls = ready
    lock = ObservedLock()
    trader._query_state_lock = lock
    held = threading.Event()
    errors = []

    def close_while_locked():
        try:
            with lock:
                held.set()
                assert lock.contended.wait(2)
                provider._monitor_closed = True
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=close_while_locked)
    thread.start()
    assert held.wait(2)
    with pytest.raises(DirectWriteError) as exc:
        provider.assert_cancel_ready()
    thread.join(timeout=2)
    assert not thread.is_alive() and not errors
    assert exc.value.code == "ctp_direct_provider_closed"
    assert calls == []


def test_session_snapshot_failure_is_finite_and_does_not_connect(ready, monkeypatch):
    provider, trader, _, calls = ready

    def fail():
        raise RuntimeError("private detail")

    monkeypatch.setattr(trader, "get_session_state", fail)
    with pytest.raises(DirectWriteError) as exc:
        provider.assert_cancel_ready()
    assert exc.value.code == "ctp_prepared_cancel_session_unavailable"
    assert "private detail" not in str(exc.value)
    assert calls == []
