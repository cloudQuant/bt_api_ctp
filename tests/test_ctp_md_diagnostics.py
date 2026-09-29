"""Offline contracts for MD login diagnostics and the disposable probe."""

from __future__ import annotations

import threading
from dataclasses import FrozenInstanceError, fields
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import (
    MdClient,
    MdLoginBrokerIdShape,
    MdLoginCallbackDiagnostic,
    MdLoginCallbackDisposition,
    MdLoginRequestIdRelation,
    MdLoginResponseErrorStatus,
    MdLoginTradingDayShape,
    MdLoginUserIdShape,
    OneShotMdDiagnosticClient,
    _MdSpi,
)


class FakeMdApi:
    def __init__(self):
        self.login_calls = []
        self.subscription_calls = []
        self.register_spi_calls = []
        self.release_calls = 0

    def ReqUserLogin(self, field, request_id):
        self.login_calls.append((field, request_id))
        return 0

    def SubscribeMarketData(self, instruments):
        self.subscription_calls.append(list(instruments))
        return 0

    def RegisterSpi(self, spi):
        self.register_spi_calls.append(spi)

    def Release(self):
        self.release_calls += 1


def wire(client_type=MdClient):
    api = FakeMdApi()
    client = client_type("tcp://md.example:123", "9999", "user-a", "secret")
    spi = client._create_md_spi(api)
    client._api = api
    client._spi = spi
    return client, spi, api


def response(client, **changes):
    values = {"BrokerID": client.broker_id, "UserID": client.user_id, "TradingDay": "20260923"}
    values.update(changes)
    return SimpleNamespace(**values)


def test_regular_login_diagnostic_is_value_free_and_cannot_grant_identity():
    client, spi, api = wire()
    assert client.login_callback_diagnostic == MdLoginCallbackDiagnostic(
        callback_count=0, disposition=MdLoginCallbackDisposition.NONE
    )
    spi.OnFrontConnected()
    assert api.login_calls[0][1] == 1
    spi.OnRspUserLogin(response(client), SimpleNamespace(ErrorID=0), 0, True)
    diagnostic = client.login_callback_diagnostic
    assert diagnostic.request_id_relation is MdLoginRequestIdRelation.ZERO
    assert diagnostic.disposition is MdLoginCallbackDisposition.REQUEST_ID_MISMATCH
    assert client.active_md_identity is None
    assert "secret" not in repr(diagnostic)
    assert "user-a" not in repr(diagnostic)
    assert tuple(field.name for field in fields(diagnostic)) == (
        "callback_count",
        "disposition",
        "request_id_relation",
        "response_error_status",
        "broker_id_shape",
        "user_id_shape",
        "trading_day_shape",
        "native_broker_id_shape",
        "native_user_id_shape",
    )
    with pytest.raises(FrozenInstanceError):
        diagnostic.callback_count = 2


@pytest.mark.parametrize(
    "change", [{"BrokerID": "other"}, {"UserID": "other"}, {"TradingDay": "20260230"}]
)
def test_regular_success_error_id_cannot_authenticate_wrong_identity(change, caplog):
    client, spi, _api = wire()
    errors = []
    client.on_error = errors.append
    spi.OnFrontConnected()
    spi.OnRspUserLogin(
        response(client, **change),
        SimpleNamespace(ErrorID=0, ErrorMsg="secret user-a"),
        1,
        True,
    )
    assert client.active_md_identity is None
    assert client.is_ready is False
    assert (
        client.login_callback_diagnostic.disposition is MdLoginCallbackDisposition.IDENTITY_REJECTED
    )
    assert errors[0].ErrorMsg.startswith("login_identity_rejected:")
    assert "secret" not in caplog.text


def test_regular_provider_error_is_sanitized_and_stale_spi_is_counted():
    client, spi, api = wire()
    errors = []
    client.on_error = errors.append
    spi.OnFrontConnected()
    spi.OnRspUserLogin(response(client), SimpleNamespace(ErrorID=7, ErrorMsg="secret"), 1, True)
    assert errors[0].ErrorMsg == "provider_login_rejected"
    assert (
        client.login_callback_diagnostic.response_error_status is MdLoginResponseErrorStatus.NONZERO
    )
    replacement = _MdSpi(client, native_api=api)
    client._spi = replacement
    spi.OnRspUserLogin(response(client), SimpleNamespace(ErrorID=0), 1, True)
    assert client.login_callback_diagnostic.callback_count == 2
    assert client.login_callback_diagnostic.disposition is MdLoginCallbackDisposition.STALE_SPI


@pytest.mark.parametrize("last", [False, 1, "true"])
def test_regular_nonterminal_or_coercible_terminal_flag_cannot_authenticate(last):
    client, spi, _api = wire()
    spi.OnFrontConnected()
    spi.OnRspUserLogin(response(client), SimpleNamespace(ErrorID=0), 1, last)
    assert client.active_md_identity is None
    assert client.login_callback_diagnostic.disposition is MdLoginCallbackDisposition.NONTERMINAL


def test_regular_reconnect_rejects_old_request_and_resets_diagnostic():
    client, spi, api = wire()
    spi.OnFrontConnected()
    spi.OnRspUserLogin(response(client), SimpleNamespace(ErrorID=0), 1, True)
    assert client.active_md_identity is not None
    spi.OnFrontDisconnected(8193)
    spi.OnFrontConnected()
    assert [request_id for _field, request_id in api.login_calls] == [1, 2]
    assert client.login_callback_diagnostic.callback_count == 0
    spi.OnRspUserLogin(response(client), SimpleNamespace(ErrorID=0), 1, True)
    assert client.active_md_identity is None
    spi.OnRspUserLogin(response(client), SimpleNamespace(ErrorID=0), 2, True)
    assert client.active_md_identity.request_id == 2


def test_regular_login_submit_exception_is_sanitized_and_clears_pending():
    client, spi, api = wire()
    errors = []
    client.on_error = errors.append

    def reject(_field, _request_id):
        raise RuntimeError("secret from provider")

    api.ReqUserLogin = reject
    spi.OnFrontConnected()
    assert client.active_md_identity is None
    assert client._login_request_pending is False
    assert errors[0].ErrorMsg == "login_request_rejected"


def test_one_shot_exact_zero_login_one_ack_one_tick_and_detach():
    client, spi, api = wire(OneShotMdDiagnosticClient)
    ticks = []
    client.on_tick = ticks.append
    spi.OnFrontConnected()
    assert [request_id for _field, request_id in api.login_calls] == [0]
    spi.OnRspUserLogin(response(client), SimpleNamespace(ErrorID=0), 0, True)
    assert client.active_md_identity is not None
    assert client.active_md_identity.request_id == 0
    assert client.login_callback_diagnostic.disposition is MdLoginCallbackDisposition.ACCEPTED
    assert client.login_callback_diagnostic.broker_id_shape is MdLoginBrokerIdShape.EXACT_MATCH
    assert client.login_callback_diagnostic.user_id_shape is MdLoginUserIdShape.EXACT_MATCH
    assert client.login_callback_diagnostic.trading_day_shape is MdLoginTradingDayShape.VALID
    client.subscribe("rb2701")
    spi.OnRspSubMarketData(
        SimpleNamespace(InstrumentID="rb2701"), SimpleNamespace(ErrorID=0), 0, True
    )
    tick = SimpleNamespace(InstrumentID="rb2701")
    spi.OnRtnDepthMarketData(tick)
    spi.OnRtnDepthMarketData(tick)
    assert api.subscription_calls == [["rb2701"]]
    assert ticks == [tick]
    assert client.diagnostic_terminal_reason == "diagnostic_complete"
    assert client.active_md_identity is None
    assert client.stop_and_wait(1).complete is True


@pytest.mark.parametrize("request_id", [True, -1, 1])
def test_one_shot_wrong_login_request_id_fails_closed(request_id):
    client, spi, _api = wire(OneShotMdDiagnosticClient)
    spi.OnFrontConnected()
    spi.OnRspUserLogin(response(client), SimpleNamespace(ErrorID=0), request_id, True)
    assert client.diagnostic_terminal is True
    assert client.active_md_identity is None


def test_one_shot_shape_metadata_preserves_failed_identity_without_values():
    client, spi, _api = wire(OneShotMdDiagnosticClient)
    spi.OnFrontConnected()
    spi.OnRspUserLogin(
        response(client, BrokerID="other", UserID="user-a\t", TradingDay="20260230"),
        SimpleNamespace(ErrorID=0, ErrorMsg="secret"),
        0,
        True,
    )
    diagnostic = client.login_callback_diagnostic
    assert diagnostic.broker_id_shape is MdLoginBrokerIdShape.ASCII_MISMATCH
    assert diagnostic.user_id_shape is MdLoginUserIdShape.WHITESPACE_OR_CONTROL
    assert diagnostic.trading_day_shape is MdLoginTradingDayShape.INVALID_CALENDAR
    assert client.diagnostic_terminal_reason == "broker_id_mismatch"
    assert "other" not in repr(diagnostic)
    assert "user-a" not in repr(diagnostic)
    assert "secret" not in repr(diagnostic)


def test_one_shot_duplicate_connect_and_callback_stop_fail_closed():
    client, spi, api = wire(OneShotMdDiagnosticClient)
    spi.OnFrontConnected()
    spi.OnFrontConnected()
    assert len(api.login_calls) == 1
    assert client.diagnostic_terminal_reason == "duplicate_front_connected"
    assert client.active_md_identity is None

    other, other_spi, other_api = wire(OneShotMdDiagnosticClient)
    callback_seen = threading.Event()

    def stop_in_callback(_response):
        other.stop()
        assert other_api.release_calls == 0
        callback_seen.set()

    other.on_login = stop_in_callback
    other_spi.OnFrontConnected()
    other_spi.OnRspUserLogin(response(other), SimpleNamespace(ErrorID=0), 0, True)
    assert callback_seen.is_set()
    assert other.stop_and_wait(1).complete is True


def test_one_shot_rejects_duplicate_login_and_tick_before_ack():
    client, spi, _api = wire(OneShotMdDiagnosticClient)
    spi.OnFrontConnected()
    spi.OnRspUserLogin(response(client), SimpleNamespace(ErrorID=0), 0, True)
    assert client.active_md_identity is not None
    spi.OnRspUserLogin(response(client), SimpleNamespace(ErrorID=0), 0, True)
    assert client.diagnostic_terminal_reason == "unexpected_login_callback"
    assert client.active_md_identity is None

    other, other_spi, _api = wire(OneShotMdDiagnosticClient)
    other_spi.OnFrontConnected()
    other_spi.OnRspUserLogin(response(other), SimpleNamespace(ErrorID=0), 0, True)
    other.subscribe("rb2701")
    other_spi.OnRtnDepthMarketData(SimpleNamespace(InstrumentID="rb2701"))
    assert other.diagnostic_terminal_reason == "tick_before_subscription_ack"
    assert other.diagnostic_first_tick_received is False


def test_one_shot_stop_and_wait_on_unstarted_client_is_complete():
    client = OneShotMdDiagnosticClient("tcp://md.example:123", "9999", "user-a", "secret")
    receipt = client.stop_and_wait(0)
    assert receipt.complete is True
    assert client.diagnostic_terminal is True
    with pytest.raises(RuntimeError, match="single_use"):
        client.start()
