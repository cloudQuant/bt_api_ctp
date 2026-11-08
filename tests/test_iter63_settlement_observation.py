"""Source-bound CTP settlement observations; synthetic query/native calls only."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from bt_api_base.direct_write_protocol import (
    DirectWriteError,
    SettlementStatus,
    SettlementWriteBinding,
    WriteOperation,
)
from bt_api_py.write_admission import WriteAdmission
from test_iter62_settlement_readback_on_write import TODAY, _login, _real_setup

ROUTE = "CTP___FUTURE"


@pytest.fixture
def session(monkeypatch):
    from bt_api_ctp import direct_write_provider

    monkeypatch.setattr(direct_write_provider, "_READBACK_MONITOR_INTERVAL", 3600.0)
    provider, client, native = _real_setup()
    _login(client, native, session_id=20)
    native.confirmation_day = "20261105"
    yield provider, client, native
    provider.close()


def observe(session):
    provider, client, native = session
    value = provider.observe_settlement()
    assert value is not None and value.status is SettlementStatus.UNCONFIRMED
    assert provider.validate_settlement_observation(value) is True
    native.calls.clear()
    return value


def grant(observation):
    admission = WriteAdmission({ROUTE: True})
    binding = SettlementWriteBinding(observation, "synthetic-marker")
    token = admission.issue(ROUTE, WriteOperation.SETTLEMENT_CONFIRM, observation.session_generation, settlement_binding=binding)
    return token, admission


def test_observation_exact_object_copy_and_new_query_invalidate(session):
    provider, client, native = session
    observation = observe(session)
    assert provider.settlement_required is True
    assert not provider.validate_settlement_observation(replace(observation))
    native.confirmation_day = TODAY
    assert client.verify_settlement_confirmation(timeout=0.01).complete
    assert client.is_trading_ready
    assert not provider.validate_settlement_observation(observation)
    assert "confirm_settlement" not in native.calls


@pytest.mark.parametrize("change", ["trader", "generation", "day", "api", "spi", "pid", "expired", "readback"])
def test_old_observation_refuses_before_native(session, change, monkeypatch):
    provider, client, native = session
    observation = observe(session)
    token, admission = grant(observation)
    if change == "trader":
        provider._feed._trader = object()
    elif change == "generation":
        _login(client, native, session_id=21)
    elif change == "day":
        client._trading_day = "20261109"
    elif change == "api":
        client._api = object()
    elif change == "spi":
        client._spi = object()
    elif change == "pid":
        provider._settlement_ticket.pid = -1
    elif change == "expired":
        provider._settlement_ticket.expires_at = 0.0
    else:
        client.verify_settlement_confirmation(timeout=0.01)
    assert not provider.validate_settlement_observation(observation)
    with pytest.raises(DirectWriteError, match="ctp_settlement_observation_invalid"):
        provider.confirm_settlement(token, admission, observation=observation)
    assert "confirm_settlement" not in native.calls
    provider._feed._trader = client


def test_wrong_or_copied_binding_cannot_consume_or_send(session):
    provider, client, native = session
    observation = observe(session)
    copied = replace(observation)
    token, admission = grant(observation)
    with pytest.raises(DirectWriteError, match="ctp_settlement_observation_invalid"):
        provider.confirm_settlement(token, admission, observation=copied)
    assert provider.validate_settlement_observation(observation)
    assert "confirm_settlement" not in native.calls


def test_same_observation_authorizes_exactly_one_native_confirmation(session):
    provider, client, native = session
    observation = observe(session)
    token, admission = grant(observation)

    def confirm(_field, request_id):
        native.calls.append("confirm_settlement")
        native.confirmation_day = TODAY
        client._spi.OnRspSettlementInfoConfirm(
            SimpleNamespace(BrokerID="9999", InvestorID="account", ConfirmDate=TODAY),
            SimpleNamespace(ErrorID=0, ErrorMsg=""), request_id, True,
        )
        return 0

    native.ReqSettlementInfoConfirm = confirm
    result = provider.confirm_settlement(token, admission, observation=observation)
    assert result is SettlementStatus.CONFIRMED
    assert native.calls == ["confirm_settlement", "query_settlement"]
    assert not provider.validate_settlement_observation(observation)
    with pytest.raises(DirectWriteError, match="ctp_settlement_observation_invalid"):
        provider.confirm_settlement(token, admission, observation=observation)
    assert native.calls.count("confirm_settlement") == 1


def test_matching_confirmation_callback_invalidates_previous_observation(session):
    provider, client, native = session
    observation = observe(session)
    with client._query_state_lock:
        client._settlement_state = "confirming"
        client._settlement_request_id = 999
        client._settlement_connection_generation = client._connection_generation
        client._settlement_account_fingerprint = client._account_fingerprint
        client._settlement_trading_day = client._trading_day
    client._spi.OnRspSettlementInfoConfirm(
        SimpleNamespace(BrokerID="9999", InvestorID="account", ConfirmDate=TODAY),
        SimpleNamespace(ErrorID=0, ErrorMsg=""), 999, True,
    )
    assert not provider.validate_settlement_observation(observation)
    assert "confirm_settlement" not in native.calls


@pytest.mark.parametrize("change", ["generation", "day", "api", "spi", "readback", "expired"])
def test_native_final_gate_rejects_change_after_authorization(session, change, monkeypatch):
    provider, client, native = session
    observation = observe(session)
    token, admission = grant(observation)
    original = client._invoke_session_native_request

    def changed(api, method, *args, **kwargs):
        if method == "ReqSettlementInfoConfirm":
            if change == "generation":
                client._connection_generation += 1
            elif change == "day":
                client._trading_day = "20261109"
            elif change == "api":
                client._api = object()
            elif change == "spi":
                client._spi = object()
            elif change == "readback":
                client._settlement_observation_epoch += 1
            else:
                provider._settlement_ticket.expires_at = 0.0
        return original(api, method, *args, **kwargs)

    monkeypatch.setattr(client, "_invoke_session_native_request", changed)
    assert provider.confirm_settlement(token, admission, observation=observation) is SettlementStatus.UNKNOWN
    assert "confirm_settlement" not in native.calls
    with pytest.raises(DirectWriteError, match="ctp_settlement_observation_invalid"):
        provider.confirm_settlement(token, admission, observation=observation)


def test_ingress_settlement_final_gate_accepts_prefixed_identity_once(monkeypatch):
    from bt_api_ctp.ctp.client import _issue_ctp_execution_authority_for_core
    from test_ctp_callback_ingress_client import _new_active_client

    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        capability = _issue_ctp_execution_authority_for_core()
        client.configure_execution_gate(capability)
        authorization = client._issue_settlement_authorization_for_core(
            capability, environment_profile="synthetic", environment_verified=True
        )
        api.ReqSettlementInfoConfirm = lambda _field, request_id: api.calls.append(("confirm", request_id)) or 0
        assert client._request_settlement_confirmation(
            execution_capability=capability,
            settlement_authorization=authorization,
            settlement_environment_profile="synthetic",
            settlement_environment_verified=True,
        ) is True
        assert len([call for call in api.calls if call[0] == "confirm"]) == 1
        assert authorization._native_entered is True
    finally:
        client.stop()


def test_non_ingress_raw_native_entry_requires_authorization(session):
    _, client, native = session
    with pytest.raises(Exception, match="settlement_authorization_required"):
        client._invoke_session_native_request(native, "ReqSettlementInfoConfirm", object(), 1)
    assert "confirm_settlement" not in native.calls


def test_mutating_exact_observation_cannot_change_its_binding(session):
    provider, _client, native = session
    observation = observe(session)
    object.__setattr__(observation, "trading_day", "20261109")
    assert not provider.validate_settlement_observation(observation)
    assert "confirm_settlement" not in native.calls


def test_concurrent_readback_invalidates_prior_observation(session):
    import threading

    provider, client, native = session
    observation = observe(session)
    errors = []

    def query():
        try:
            client.verify_settlement_confirmation(timeout=0.2)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=query)
    worker.start()
    worker.join(2)
    assert not worker.is_alive() and errors == []
    assert not provider.validate_settlement_observation(observation)
    assert "confirm_settlement" not in native.calls


def test_sdk_profile_cannot_issue_without_observation_callbacks(session):
    from bt_api_ctp.ctp.client import CtpExecutionGateError, SDK_ADMITTED_PROFILE

    provider, client, native = session
    capability = provider._admitted_capability(client)
    with pytest.raises(CtpExecutionGateError, match="settlement_authorization_context_mismatch"):
        client._issue_settlement_authorization_for_core(
            capability, environment_profile=SDK_ADMITTED_PROFILE, environment_verified=True
        )
    assert "confirm_settlement" not in native.calls


def test_new_session_after_successful_native_readback_is_not_confirmed(session, monkeypatch):
    provider, client, native = session
    observation = observe(session)
    token, admission = grant(observation)

    def confirm(_field, request_id):
        native.calls.append("confirm_settlement")
        native.confirmation_day = TODAY
        client._spi.OnRspSettlementInfoConfirm(
            SimpleNamespace(BrokerID="9999", InvestorID="account", ConfirmDate=TODAY),
            SimpleNamespace(ErrorID=0, ErrorMsg=""), request_id, True,
        )
        return 0

    native.ReqSettlementInfoConfirm = confirm
    original = client._read_direct_settlement_status

    def new_session_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        _login(client, native, session_id=21)
        return result

    monkeypatch.setattr(client, "_read_direct_settlement_status", new_session_after_read)
    assert provider.confirm_settlement(token, admission, observation=observation) is SettlementStatus.UNKNOWN
    assert native.calls == ["confirm_settlement", "query_settlement"]
    assert not provider.validate_settlement_observation(observation)


def test_native_confirmation_does_not_hold_client_state_lock(session):
    import threading

    provider, client, native = session
    observation = observe(session)
    token, admission = grant(observation)

    def confirm(_field, request_id):
        event = threading.Event()

        def use_state():
            with client._query_state_lock:
                event.set()

        worker = threading.Thread(target=use_state)
        worker.start()
        worker.join(1)
        assert event.is_set() and not worker.is_alive()
        native.calls.append("confirm_settlement")
        native.confirmation_day = TODAY
        client._spi.OnRspSettlementInfoConfirm(
            SimpleNamespace(BrokerID="9999", InvestorID="account", ConfirmDate=TODAY),
            SimpleNamespace(ErrorID=0, ErrorMsg=""), request_id, True,
        )
        return 0

    native.ReqSettlementInfoConfirm = confirm
    assert provider.confirm_settlement(token, admission, observation=observation) is SettlementStatus.CONFIRMED
    assert native.calls == ["confirm_settlement", "query_settlement"]


def test_trader_replaced_after_confirmation_does_not_return_confirmed(session, monkeypatch):
    provider, client, native = session
    observation = observe(session)
    token, admission = grant(observation)

    def confirm(_field, request_id):
        native.calls.append("confirm_settlement")
        native.confirmation_day = TODAY
        client._spi.OnRspSettlementInfoConfirm(
            SimpleNamespace(BrokerID="9999", InvestorID="account", ConfirmDate=TODAY),
            SimpleNamespace(ErrorID=0, ErrorMsg=""), request_id, True,
        )
        return 0

    native.ReqSettlementInfoConfirm = confirm
    original = client.confirm_settlement_for_sdk_admission

    def replace_after(*args, **kwargs):
        result = original(*args, **kwargs)
        provider._feed._trader = object()
        return result

    monkeypatch.setattr(client, "confirm_settlement_for_sdk_admission", replace_after)
    try:
        assert provider.confirm_settlement(token, admission, observation=observation) is SettlementStatus.UNKNOWN
        assert native.calls == ["confirm_settlement", "query_settlement"]
    finally:
        provider._feed._trader = client


def test_ingress_final_gate_rejects_day_change_before_native(monkeypatch):
    from bt_api_ctp.ctp.client import _issue_ctp_execution_authority_for_core
    from test_ctp_callback_ingress_client import _new_active_client

    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        capability = _issue_ctp_execution_authority_for_core()
        client.configure_execution_gate(capability)
        authorization = client._issue_settlement_authorization_for_core(
            capability, environment_profile="synthetic", environment_verified=True
        )
        api.ReqSettlementInfoConfirm = lambda _field, request_id: api.calls.append(("confirm", request_id)) or 0
        original = client._invoke_session_native_request

        def changed(api_arg, method, *args, **kwargs):
            if method == "ReqSettlementInfoConfirm":
                client._trading_day = "20260927"
            return original(api_arg, method, *args, **kwargs)

        monkeypatch.setattr(client, "_invoke_session_native_request", changed)
        assert client._request_settlement_confirmation(
            execution_capability=capability,
            settlement_authorization=authorization,
            settlement_environment_profile="synthetic",
            settlement_environment_verified=True,
        ) is False
        assert not any(call[0] == "confirm" for call in api.calls)
    finally:
        client.stop()


def test_marker_mutation_after_grant_cannot_authorize_final_entry(session, monkeypatch):
    provider, _client, native = session
    observation = observe(session)
    token, admission = grant(observation)
    original = provider._admitted_capability

    def mutate(trader):
        result = original(trader)
        object.__setattr__(token.settlement_binding, "marker_id", "changed-marker")
        return result

    monkeypatch.setattr(provider, "_admitted_capability", mutate)
    with pytest.raises(DirectWriteError, match="ctp_settlement_observation_invalid"):
        provider.confirm_settlement(token, admission, observation=observation)
    assert "confirm_settlement" not in native.calls


def test_invalid_marker_is_rejected_even_for_exact_binding(session):
    provider, _client, native = session
    observation = observe(session)
    token, admission = grant(observation)
    object.__setattr__(token.settlement_binding, "marker_id", "")
    with pytest.raises(DirectWriteError, match="ctp_settlement_observation_invalid"):
        provider.confirm_settlement(token, admission, observation=observation)
    assert "confirm_settlement" not in native.calls


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="requires fork")
def test_fork_rejects_settlement_before_inherited_locks(session):
    import os
    import select
    import signal
    import threading

    provider, client, native = session
    observation = observe(session)
    token, admission = grant(observation)
    held = threading.Event()
    release = threading.Event()

    def hold_locks():
        with provider._lock, client._query_state_lock:
            held.set()
            release.wait(4)

    worker = threading.Thread(target=hold_locks)
    worker.start()
    assert held.wait(2)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        signal.alarm(2)
        try:
            assert provider.observe_settlement() is None
            assert provider.validate_settlement_observation(observation) is False
            assert provider.settlement_status() is SettlementStatus.UNKNOWN
            try:
                provider.confirm_settlement(token, admission, observation=observation)
            except DirectWriteError as exc:
                assert exc.code == "ctp_settlement_observation_invalid"
            else:
                raise AssertionError("fork confirmation was accepted")
            os.write(write_fd, b"ok")
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    try:
        readable, _, _ = select.select([read_fd], [], [], 3)
        result = os.read(read_fd, 16) if readable else b"timeout"
        _, status = os.waitpid(pid, 0)
        assert result == b"ok" and status == 0
        assert "confirm_settlement" not in native.calls
    finally:
        os.close(read_fd)
        release.set()
        worker.join(2)
        assert not worker.is_alive()
