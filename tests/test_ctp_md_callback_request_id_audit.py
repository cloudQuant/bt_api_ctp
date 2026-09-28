"""Fake-only audit regressions for the ordinary MD client's login request ID."""

from __future__ import annotations

from types import SimpleNamespace

from bt_api_ctp.ctp.client import MdClient, _MdSpi


def test_zero_callback_id_does_not_match_generation_one_login_request():
    login_requests = []
    subscriptions = []
    api = SimpleNamespace(
        ReqUserLogin=lambda field, request_id: login_requests.append((field, request_id)) or 0,
        SubscribeMarketData=lambda instruments: subscriptions.append(list(instruments)) or 0,
    )
    client = MdClient("tcp://md.test:30011", "9999", "user-a", "unused-test-marker")
    spi = _MdSpi(client, native_api=api)
    client._api = api
    client._spi = spi
    client.subscribe(["rb2701"])

    spi.OnFrontConnected()

    assert len(login_requests) == 1
    assert login_requests[0][1] == 1
    assert client._pending_login_request_id == 1

    response = SimpleNamespace(BrokerID="9999", UserID="user-a", TradingDay="20260923")
    spi.OnRspUserLogin(response, SimpleNamespace(ErrorID=0), 0, True)

    assert client._loggedin is False
    assert client.active_md_identity is None
    assert client._pending_login_request_id == 1
    assert subscriptions == []
