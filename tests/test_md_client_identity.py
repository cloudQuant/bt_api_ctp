"""Offline lifecycle tests for constructor-bound MD identity observations."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bt_api_ctp import md_identity
from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.ctp.client import MdClient, _MdSpi


class _FakeMdApi:
    def __init__(self, *, request_result=0):
        self.login_requests = []
        self.registered_front = None
        self.spi = None
        self.request_result = request_result

    def RegisterSpi(self, spi):
        self.spi = spi

    def RegisterFront(self, front):
        self.registered_front = front

    def Init(self):
        return None

    def ReqUserLogin(self, field, request_id):
        self.login_requests.append((field, request_id))
        return self.request_result

    def SubscribeMarketData(self, instruments):
        return None


def _wired_client(*, api=None, allow_zero_login_response=False):
    client = MdClient(
        "tcp://md.example:123",
        "broker-1",
        "user-1",
        "secret",
        allow_zero_login_response=allow_zero_login_response,
    )
    api = api or _FakeMdApi()
    spi = _MdSpi(client, native_api=api)
    client._api = api
    client._spi = spi
    return client, api, spi


def test_collector_zero_id_login_can_collect_without_publishing_correlated_identity():
    client, api, spi = _wired_client(allow_zero_login_response=True)
    _connect(client, api, spi)
    _login_response(spi, 0)

    assert client.is_ready is True
    assert client.wait_ready(timeout=0) is True
    assert client.active_md_identity is None
    assert client._login_request_pending is False
    # Duplicate success after a completed request must not reopen readiness.
    client._loggedin = False
    _login_response(spi, 0)
    assert client.is_ready is False


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": 2},
        {"request_id": False},
        {"terminal": False},
        {"error_id": 7},
        {"response": SimpleNamespace(BrokerID="other", UserID="user-1", TradingDay="20260926")},
        {"response": SimpleNamespace(BrokerID="broker-1", UserID="other", TradingDay="20260926")},
    ],
)
def test_zero_id_compatibility_still_rejects_invalid_login(changes):
    client, api, spi = _wired_client(allow_zero_login_response=True)
    _connect(client, api, spi)
    arguments = {"request_id": 0, **changes}
    _login_response(spi, **arguments)
    assert client.is_ready is False
    assert client.active_md_identity is None


def test_zero_id_compatibility_cannot_accept_disconnected_or_old_spi():
    client, api, spi = _wired_client(allow_zero_login_response=True)
    _connect(client, api, spi)
    spi.OnFrontDisconnected(0x1001)
    _login_response(spi, 0)
    assert client.is_ready is False
    _connect(client, api, spi)
    client._spi = _MdSpi(client, native_api=api)
    _login_response(spi, 0)
    assert client.is_ready is False


def test_read_only_zero_id_login_accepts_empty_echoed_account_fields():
    client, api, spi = _wired_client(allow_zero_login_response=True)
    _connect(client, api, spi)
    _login_response(spi, 0, response=SimpleNamespace(BrokerID="", UserID="", TradingDay="20260926"))
    assert client.is_ready is True
    assert client.active_md_identity is None
    assert client.login_callback_diagnostic.disposition.value == "read_only_accepted"


@pytest.mark.parametrize("request_id,day", [(1, "20260926"), (0, "20260230"), (0, "")])
def test_compatibility_rejects_blank_account_on_correlated_login_or_invalid_day(request_id, day):
    client, api, spi = _wired_client(allow_zero_login_response=True)
    _connect(client, api, spi)
    _login_response(
        spi, request_id, response=SimpleNamespace(BrokerID="", UserID="", TradingDay=day)
    )
    assert client.is_ready is False
    assert client.active_md_identity is None


def test_read_only_zero_id_front_can_reconnect_and_resubscribe():
    client, api, spi = _wired_client(allow_zero_login_response=True)
    subscriptions = []
    api.SubscribeMarketData = lambda items: subscriptions.append(list(items))
    client._pending_instruments = ["rb2701"]
    for attempt in range(2):
        _connect(client, api, spi)
        _login_response(
            spi, 0, response=SimpleNamespace(BrokerID="", UserID="", TradingDay="20260926")
        )
        assert client.is_ready is True
        assert client.active_md_identity is None
        if attempt == 0:
            spi.OnFrontDisconnected(0x1001)
            assert client.is_ready is False
    assert subscriptions == [["rb2701"], ["rb2701"]]


def _connect(client, api, spi):
    spi.OnFrontConnected()
    field, request_id = api.login_requests[-1]
    return field, request_id


def _login_response(
    spi,
    request_id,
    *,
    response=None,
    error_id=0,
    terminal=True,
):
    if response is None:
        response = SimpleNamespace(BrokerID="broker-1", UserID="user-1", TradingDay="20260926")
    spi.OnRspUserLogin(
        response,
        SimpleNamespace(ErrorID=error_id, ErrorMsg="fake" if error_id else ""),
        request_id,
        terminal,
    )


def test_terminal_success_publishes_exact_callback_identity_and_separate_counters():
    client, api, spi = _wired_client()
    client._connection_generation = 8

    field, request_id = _connect(client, api, spi)
    assert (field.BrokerID, field.UserID) == ("broker-1", "user-1")
    assert request_id == 1
    _login_response(spi, request_id)

    identity = client.active_md_identity
    assert identity == md_identity.MdIdentityObservation(
        front="tcp://md.example:123",
        broker_id="broker-1",
        user_id="user-1",
        connection_generation=9,
        request_id=1,
        trading_day="20260926",
        authenticated=True,
    )
    assert md_identity.md_identity_matches(
        identity,
        expected_front="tcp://md.example:123",
        expected_broker_id="broker-1",
        expected_user_id="user-1",
        expected_connection_generation=9,
        expected_request_id=1,
    )


def test_stale_request_and_nonterminal_success_do_not_publish_identity():
    client, api, spi = _wired_client()
    _, request_id = _connect(client, api, spi)

    # The historical I2 callback carried request ID zero.  It must remain an
    # unrelated callback rather than being treated as a wildcard for the
    # current login request.
    _login_response(spi, 0)
    assert client.active_md_identity is None
    assert client._login_request_pending is True

    _login_response(spi, request_id + 1)
    assert client.active_md_identity is None
    assert client._login_request_pending is True

    _login_response(spi, request_id, terminal=False)
    assert client.active_md_identity is None
    assert client._login_request_pending is True

    _login_response(spi, request_id)
    assert client.active_md_identity is not None


def test_missing_callback_identity_stays_missing_and_unverified():
    client, api, spi = _wired_client()
    _, request_id = _connect(client, api, spi)

    _login_response(spi, request_id, response=SimpleNamespace(TradingDay=""))

    assert client.active_md_identity is None
    assert client.is_ready is False


def test_failed_terminal_login_clears_identity_and_readiness():
    client, api, spi = _wired_client()
    _, request_id = _connect(client, api, spi)
    _login_response(spi, request_id, error_id=7)

    assert client.active_md_identity is None
    assert client.is_ready is False
    assert client._login_request_pending is False


def test_synchronous_request_rejection_cannot_become_authenticated_later():
    client, api, spi = _wired_client(api=_FakeMdApi(request_result=-1))
    _, request_id = _connect(client, api, spi)

    assert client._login_request_pending is False
    assert client._login_request_id is None
    _login_response(spi, request_id)
    assert client.active_md_identity is None
    assert client.is_ready is False


def test_request_id_space_exhaustion_fails_closed_without_wrapping():
    client, api, spi = _wired_client()
    client._login_request_counter = 2_147_483_647

    spi.OnFrontConnected()

    assert api.login_requests == []
    assert client._login_request_pending is False
    _login_response(spi, 1)
    assert client.active_md_identity is None


def test_reconnect_increments_generation_and_rejects_previous_request():
    client, api, spi = _wired_client()
    _, first_request_id = _connect(client, api, spi)
    _login_response(spi, first_request_id)
    assert client.active_md_identity is not None

    spi.OnFrontDisconnected(8193)
    assert client.active_md_identity is None
    _, second_request_id = _connect(client, api, spi)
    assert second_request_id == first_request_id + 1

    _login_response(spi, first_request_id)
    assert client.active_md_identity is None
    assert client._login_request_pending is True

    _login_response(spi, second_request_id)
    assert client.active_md_identity.connection_generation == 2
    assert client.active_md_identity.request_id == second_request_id


def test_old_api_callback_is_rejected_after_stop_and_replacement(monkeypatch):
    client, old_api, old_spi = _wired_client()
    _, old_request_id = _connect(client, old_api, old_spi)
    _login_response(old_spi, old_request_id)
    assert client.active_md_identity is not None

    monkeypatch.setattr(client_module, "_ctp_native_join_claimed", lambda _api: False)

    def release_fake_api(_api, _spi, *, pending_api_ids, **_kwargs):
        pending_api_ids.discard(id(_api))

    monkeypatch.setattr(client_module, "_release_ctp_native_api_immediately", release_fake_api)
    client._stop_native_session()

    new_api = _FakeMdApi()
    new_spi = _MdSpi(client, native_api=new_api)
    client._api = new_api
    client._spi = new_spi
    _, new_request_id = _connect(client, new_api, new_spi)
    assert new_request_id > old_request_id

    _login_response(old_spi, old_request_id)
    assert client.active_md_identity is None
    assert client._login_request_pending is True

    _login_response(new_spi, new_request_id)
    assert client.active_md_identity.request_id == new_request_id
    assert client.active_md_identity.connection_generation == 2


def test_public_front_and_account_mutation_cannot_retarget_md_scope(monkeypatch):
    client = MdClient("tcp://md.bound:123", "bound-bkr", "bound-user", "secret")
    api = _FakeMdApi()

    # Keep the lifecycle offline by replacing only the native factory seams.
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "offline-flow")
    monkeypatch.setattr(client_module, "_register_ctp_native_api", lambda _api: None)
    monkeypatch.setattr(
        client_module.CThostFtdcMdApi,
        "CreateFtdcMdApi",
        staticmethod(lambda _flow: api),
    )
    client._start_join_observer = lambda _api: False
    client.front = "tcp://md.changed:456"
    client.broker_id = "other-bkr"
    client.user_id = "changed-user"

    client.start(block=False)
    assert api.registered_front == "tcp://md.bound:123"
    spi = client._spi
    spi.OnFrontConnected()
    field, request_id = api.login_requests[-1]
    assert (field.BrokerID, field.UserID) == ("bound-bkr", "bound-user")
    _login_response(
        spi,
        request_id,
        response=SimpleNamespace(BrokerID="bound-bkr", UserID="bound-user", TradingDay="20260926"),
    )
    identity = client.active_md_identity
    assert identity.front == "tcp://md.bound:123"
    assert identity.broker_id == "bound-bkr"
    assert identity.user_id == "bound-user"


def test_active_identity_property_rejects_mixed_or_stale_cached_state():
    client, api, spi = _wired_client()
    _, request_id = _connect(client, api, spi)
    _login_response(spi, request_id)
    assert client.active_md_identity is not None

    client._login_request_generation += 1
    assert client.active_md_identity is None

    client._login_request_generation = client._connection_generation
    replacement_api = _FakeMdApi()
    client._api = replacement_api
    client._spi = _MdSpi(client, native_api=replacement_api)
    assert client.active_md_identity is None


def test_disconnect_stop_and_start_reservation_clear_identity(monkeypatch):
    client, api, spi = _wired_client()
    _, request_id = _connect(client, api, spi)
    _login_response(spi, request_id)
    assert client.active_md_identity is not None

    spi.OnFrontDisconnected(8193)
    assert client.active_md_identity is None

    # Reconnect once, then exercise the SDK-owned stop cleanup without any
    # native API or provider interaction.
    _, request_id = _connect(client, api, spi)
    _login_response(spi, request_id)
    assert client.active_md_identity is not None
    monkeypatch.setattr(client_module, "_ctp_native_join_claimed", lambda _api: False)

    def release_fake_api(_api, _spi, *, pending_api_ids, **_kwargs):
        pending_api_ids.discard(id(_api))

    monkeypatch.setattr(client_module, "_release_ctp_native_api_immediately", release_fake_api)
    client._stop_native_session()
    assert client.active_md_identity is None
    assert client._login_request_id is None

    # A subsequent startup reservation is a new API lifecycle and stays empty.
    client._api = None
    generation = client._reserve_start_generation()
    assert client.active_md_identity is None
    client._clear_start_reservation(generation)
