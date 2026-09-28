from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import TraderClient, _TraderSpi


def _logging_in_client(*, generation: int = 3, request_id: int = 19) -> TraderClient:
    client = TraderClient("tcp://configured-td", "9999", "account-7", "unused")
    client._connected = True
    client._authentication_state = "authenticated"
    client._login_state = "logging_in"
    client._connection_generation = generation
    client._login_request_id = request_id
    client._login_connection_generation = generation
    return client


def _response(client: TraderClient, **changes):
    values = {
        "BrokerID": client._bound_broker_id,
        "UserID": client._bound_user_id,
        "TradingDay": "20260924",
        "FrontID": 4,
        "SessionID": 5,
        "MaxOrderRef": "8",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _complete_login(
    client: TraderClient,
    response=None,
    *,
    request_id: int = 19,
    is_last=True,
    rsp_info=None,
):
    _TraderSpi(client).OnRspUserLogin(
        response or _response(client),
        rsp_info or SimpleNamespace(ErrorID=0, ErrorMsg=""),
        request_id,
        is_last,
    )


def test_query_scope_uses_fenced_native_login_identity_and_disconnect_clears_it():
    client = _logging_in_client()
    _complete_login(client, is_last=1, rsp_info=None)

    scope = client.get_query_session_scope()
    expected_fingerprint = hashlib.sha256(b"9999:account-7").hexdigest()[:16]
    assert client.is_read_only_ready is True
    assert scope.read_only_ready is True
    assert scope.account_fingerprint == expected_fingerprint
    assert scope.broker_id == "9999"
    assert scope.investor_id == "account-7"
    assert scope.trading_day == "20260924"
    assert scope.connection_generation == 3

    _TraderSpi(client).OnFrontDisconnected(1)
    stale_scope = client.get_query_session_scope()
    assert client.is_read_only_ready is False
    assert stale_scope.read_only_ready is False
    assert stale_scope.account_fingerprint == ""
    assert stale_scope.broker_id == ""
    assert stale_scope.investor_id == ""
    assert stale_scope.trading_day == ""


@pytest.mark.parametrize(
    ("field", "replacement", "expected_reason"),
    [
        ("BrokerID", "other-broker", "broker_id_mismatch"),
        ("UserID", "other-account", "user_id_mismatch"),
        ("TradingDay", "", "trading_day_invalid"),
        ("TradingDay", "20260230", "trading_day_invalid"),
    ],
)
def test_success_response_with_incorrect_native_identity_never_becomes_read_ready(
    field, replacement, expected_reason
):
    client = _logging_in_client()
    _complete_login(client, _response(client, **{field: replacement}))

    scope = client.get_query_session_scope()
    assert client.get_session_state()["login_state"] == "failed"
    assert client.get_session_state()["last_error"]["reason"] == expected_reason
    assert client.is_read_only_ready is False
    assert scope.read_only_ready is False
    assert scope.account_fingerprint == ""
    assert scope.broker_id == ""
    assert scope.investor_id == ""
    assert scope.trading_day == ""


def test_nonterminal_login_packet_cannot_publish_identity():
    client = _logging_in_client()
    _TraderSpi(client).OnRspUserLogin(_response(client), SimpleNamespace(ErrorID=0), 19, False)

    assert client.get_session_state()["login_state"] == "logging_in"
    assert client.get_query_session_scope().read_only_ready is False
    assert client.is_read_only_ready is False

    _complete_login(client)
    assert client.get_query_session_scope().read_only_ready is True


def test_stale_login_request_generation_cannot_publish_configured_identity():
    client = _logging_in_client(generation=4)
    client._login_connection_generation = 3
    _complete_login(client)

    scope = client.get_query_session_scope()
    assert client.get_session_state()["login_state"] == "logging_in"
    assert client.get_session_state()["login_late_callback_count"] == 1
    assert client.is_read_only_ready is False
    assert scope.read_only_ready is False
    assert scope.broker_id == ""
    assert scope.investor_id == ""


def test_new_connection_generation_clears_prior_native_login_observation():
    client = _logging_in_client()
    _complete_login(client)
    assert client.is_read_only_ready is True

    client._on_front_connected()
    scope = client.get_query_session_scope()
    assert client.get_session_state()["connection_generation"] == 4
    assert client.get_session_state()["login_state"] == "not_started"
    assert client.is_read_only_ready is False
    assert scope.read_only_ready is False
    assert scope.account_fingerprint == ""
    assert scope.trading_day == ""


def test_authentication_requires_terminal_packet_before_submitting_login():
    auth_requests = []
    login_requests = []

    class NativeApi:
        def ReqAuthenticate(self, _field, request_id):
            auth_requests.append(request_id)
            return 0

        def ReqUserLogin(self, _field, request_id):
            login_requests.append(request_id)
            return 0

    client = TraderClient("tcp://configured-td", "9999", "account-7", "unused")
    native = NativeApi()
    client._api = native
    spi = _TraderSpi(client, native)
    client._spi = spi
    spi.OnFrontConnected()
    request_id = auth_requests[-1]

    spi.OnRspAuthenticate(None, None, request_id, 0)
    assert client.get_session_state()["auth_state"] == "authenticating"
    assert login_requests == []

    # SWIG may expose the C++ bool as Python bool or as integer 1.
    spi.OnRspAuthenticate(None, None, request_id, 1)
    assert client.get_session_state()["auth_state"] == "authenticated"
    assert len(login_requests) == 1


def test_provider_login_error_does_not_publish_identity_observation():
    client = _logging_in_client()
    _complete_login(
        client,
        rsp_info=SimpleNamespace(ErrorID=7, ErrorMsg="provider rejected login"),
    )

    scope = client.get_query_session_scope()
    assert client.get_session_state()["login_state"] == "failed"
    assert client.get_session_state()["last_error"]["reason"] == "provider_login_rejected"
    assert client.is_read_only_ready is False
    assert scope.read_only_ready is False
    assert scope.broker_id == ""
    assert scope.investor_id == ""
