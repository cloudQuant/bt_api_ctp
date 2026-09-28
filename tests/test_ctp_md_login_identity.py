"""Offline identity and callback-fencing contracts for ``MdClient``."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import MdClient, MdIdentityObservation, _MdSpi


def _wired_client():
    login_requests = []
    subscriptions = []
    api = SimpleNamespace(
        ReqUserLogin=lambda field, request_id: login_requests.append((field, request_id)) or 0,
        SubscribeMarketData=lambda instruments: subscriptions.append(list(instruments)) or 0,
    )
    client = MdClient("tcp://simnow.example:30011", "9999", "user-a", "secret")
    spi = _MdSpi(client, native_api=api)
    client._api = api
    client._spi = spi
    return client, spi, login_requests, subscriptions


def _login_response(client: MdClient, **overrides):
    fields = {
        "BrokerID": client.broker_id,
        "UserID": client.user_id,
        "TradingDay": "20260923",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_login_publishes_frozen_identity_only_after_terminal_bound_response():
    client, spi, login_requests, _subscriptions = _wired_client()
    seen = []
    client.on_login = seen.append

    spi.OnFrontConnected()
    assert login_requests[0][1] == 1
    assert login_requests[0][0].BrokerID == "9999"
    assert login_requests[0][0].UserID == "user-a"

    spi.OnRspUserLogin(_login_response(client), SimpleNamespace(ErrorID=0), 1, False)
    assert client.active_md_identity is None
    assert client._loggedin is False
    assert seen == []

    spi.OnRspUserLogin(_login_response(client), SimpleNamespace(ErrorID=0), 1, True)

    observation = client.active_md_identity
    assert isinstance(observation, MdIdentityObservation)
    assert observation == MdIdentityObservation(
        front="tcp://simnow.example:30011",
        broker_id="9999",
        user_id="user-a",
        connection_generation=1,
        request_id=1,
        trading_day="20260923",
        authenticated=True,
    )
    assert seen == [_login_response(client)]
    with pytest.raises(FrozenInstanceError):
        observation.user_id = "other"


@pytest.mark.parametrize(
    "response_overrides",
    [
        {"BrokerID": "other-broker"},
        {"UserID": "other-user"},
        {"BrokerID": ""},
        {"UserID": ""},
        {"TradingDay": ""},
    ],
)
def test_login_identity_mismatch_or_missing_fields_fails_closed(response_overrides):
    client, spi, _login_requests, subscriptions = _wired_client()
    client.subscribe(["rb2701"])
    seen = []
    client.on_login = seen.append
    spi.OnFrontConnected()

    spi.OnRspUserLogin(
        _login_response(client, **response_overrides), SimpleNamespace(ErrorID=0), 1, True
    )

    assert client._loggedin is False
    assert client.active_md_identity is None
    assert seen == []
    assert subscriptions == []


def test_identity_rejection_reports_a_sanitized_error_immediately():
    client, spi, _login_requests, _subscriptions = _wired_client()
    errors = []
    client.on_error = errors.append
    spi.OnFrontConnected()

    spi.OnRspUserLogin(
        _login_response(client, UserID="different-user"),
        SimpleNamespace(ErrorID=0, ErrorMsg="contains user-a and secret"),
        1,
        True,
    )

    assert len(errors) == 1
    assert errors[0].ErrorID != 0
    assert errors[0].ErrorMsg == "login_identity_rejected:user_id_mismatch"
    assert "secret" not in errors[0].ErrorMsg
    assert client.active_md_identity is None
    assert client.is_ready is False


@pytest.mark.parametrize(
    ("request_id", "is_last"),
    [(True, True), (1, 1), (2, True)],
)
def test_login_rejects_non_exact_request_id_or_terminal_flag(request_id, is_last):
    client, spi, _login_requests, _subscriptions = _wired_client()
    spi.OnFrontConnected()

    spi.OnRspUserLogin(_login_response(client), SimpleNamespace(ErrorID=0), request_id, is_last)

    assert client._loggedin is False
    assert client.active_md_identity is None


@pytest.mark.parametrize(
    "trading_day", ["20260230", "2026-09-23", "\u0662\u0660\u0662\u0666\u0660\u0669\u0662\u0663"]
)
def test_login_rejects_invalid_trading_day(trading_day):
    client, spi, _login_requests, _subscriptions = _wired_client()
    errors = []
    client.on_error = errors.append
    spi.OnFrontConnected()

    spi.OnRspUserLogin(
        _login_response(client, TradingDay=trading_day), SimpleNamespace(ErrorID=0), 1, True
    )

    assert client.active_md_identity is None
    assert errors[0].ErrorMsg == "login_identity_rejected:trading_day_invalid"


@pytest.mark.parametrize("submit_result", [-1, RuntimeError("secret-bearing error")])
def test_synchronous_login_submission_failure_clears_pending_identity(submit_result):
    client = MdClient("tcp://simnow.example:30011", "9999", "user-a", "secret")

    def reject(_field, _request_id):
        if isinstance(submit_result, Exception):
            raise submit_result
        return submit_result

    api = SimpleNamespace(ReqUserLogin=reject)
    spi = _MdSpi(client, native_api=api)
    client._api = api
    client._spi = spi
    errors = []
    client.on_error = errors.append

    spi.OnFrontConnected()

    assert client._connected is False
    assert client._loggedin is False
    assert client.active_md_identity is None
    assert client._pending_login_generation is None
    assert len(errors) == 1
    assert errors[0].ErrorID != 0
    assert "secret" not in errors[0].ErrorMsg


def test_login_rejects_missing_response_and_nonzero_native_error(caplog):
    client, spi, _login_requests, _subscriptions = _wired_client()
    errors = []
    client.on_error = errors.append
    spi.OnFrontConnected()

    spi.OnRspUserLogin(None, SimpleNamespace(ErrorID=0), 1, True)
    assert client._loggedin is False
    assert client.active_md_identity is None
    assert errors[-1].ErrorMsg == "login_identity_rejected:broker_id_mismatch"

    spi.OnFrontConnected()
    provider_error = "provider diagnostic contains user-a and secret"
    spi.OnRspUserLogin(
        _login_response(client),
        SimpleNamespace(ErrorID=7, ErrorMsg=provider_error),
        2,
        True,
    )
    assert client._loggedin is False
    assert client.active_md_identity is None
    assert errors[-1].ErrorID == 7
    assert errors[-1].ErrorMsg == "provider_login_rejected"
    assert provider_error not in caplog.text
    assert "secret" not in errors[-1].ErrorMsg


def test_login_rejects_missing_response_info_even_with_matching_identity():
    client, spi, _login_requests, _subscriptions = _wired_client()
    errors = []
    client.on_error = errors.append
    spi.OnFrontConnected()

    spi.OnRspUserLogin(_login_response(client), None, 1, True)

    assert client._loggedin is False
    assert client.active_md_identity is None
    assert errors[-1].ErrorID == -1
    assert errors[-1].ErrorMsg == "provider_login_rejected"


def test_old_request_id_and_disconnected_response_cannot_authenticate_reconnected_session():
    client, spi, login_requests, _subscriptions = _wired_client()
    spi.OnFrontConnected()
    spi.OnFrontDisconnected(8193)
    assert client.active_md_identity is None

    spi.OnFrontConnected()
    assert [request_id for _field, request_id in login_requests] == [1, 2]
    spi.OnRspUserLogin(_login_response(client), SimpleNamespace(ErrorID=0), 1, True)
    assert client.active_md_identity is None
    assert client._loggedin is False

    spi.OnRspUserLogin(_login_response(client), SimpleNamespace(ErrorID=0), 2, True)
    assert client.active_md_identity.connection_generation == 2
    assert client.active_md_identity.request_id == 2


@pytest.mark.parametrize("name", ["front", "broker_id", "user_id"])
def test_bound_front_and_account_properties_cannot_be_mutated(name):
    client = MdClient("tcp://simnow.example:30011", "9999", "user-a", "secret")

    with pytest.raises(AttributeError):
        setattr(client, name, "attacker-controlled")


def test_disconnect_clears_active_identity_observation():
    client, spi, _login_requests, _subscriptions = _wired_client()
    spi.OnFrontConnected()
    spi.OnRspUserLogin(_login_response(client), SimpleNamespace(ErrorID=0), 1, True)
    assert client.active_md_identity is not None

    spi.OnFrontDisconnected(8193)

    assert client.active_md_identity is None
