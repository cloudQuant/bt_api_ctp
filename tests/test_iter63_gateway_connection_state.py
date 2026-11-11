"""E059 source-bound connection state; all native objects are synthetic."""
from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest

from bt_api_ctp.ctp.client import _TraderSpi
from bt_api_ctp.gateway_session import CtpGatewaySessionError
from test_iter62_settlement_readback_on_write import TODAY
from test_iter63_gateway_session import prepared


def _open(prepared):
    create, _, _, feeds, natives, streams, _, fake_md = prepared
    session = create()
    session.connect(generation="gateway-e059", timeout=1)
    trader, native = feeds[0].trader_client, natives[0]
    return session, feeds[0], trader, native, streams, fake_md


def test_bound_spi_disconnect_authentication_and_login(prepared, monkeypatch):
    session, feed, trader, native, streams, fake_md = _open(prepared)
    spi = trader._spi
    calls = []
    monkeypatch.setattr(native, "ReqAuthenticate", lambda *_args: calls.append("authenticate") or 0)
    monkeypatch.setattr(native, "ReqUserLogin", lambda *_args: calls.append("login") or 0, raising=False)
    monkeypatch.setattr(feed, "get_query_session_scope", lambda: pytest.fail("status captured a scope"))
    monkeypatch.setattr(feed, "_ensure_connected", lambda *_args, **_kwargs: pytest.fail("status connected"))
    monkeypatch.setattr(session._reader, "refresh_session", lambda: pytest.fail("status refreshed"))

    assert session.connection_status() == "ready" and calls == []
    spi.OnFrontDisconnected(1)
    assert session.connection_status() == "reconnecting" and calls == []
    spi.OnFrontConnected()
    assert calls == ["authenticate"]
    assert session.connection_status() == "reconnecting" and calls == ["authenticate"]
    auth_id = trader._authentication_request_id
    spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=0, ErrorMsg=""), auth_id, True)
    assert calls == ["authenticate", "login"]
    assert session.connection_status() == "reconnecting" and calls == ["authenticate", "login"]
    login_id = trader._login_request_id
    spi.OnRspUserLogin(
        SimpleNamespace(BrokerID="9999", UserID="account", TradingDay=TODAY,
                        FrontID=7, SessionID=20, MaxOrderRef="0"),
        SimpleNamespace(ErrorID=0, ErrorMsg=""), login_id, True,
    )
    assert session.connection_status() == "ready"
    assert calls == ["authenticate", "login"]
    assert streams == [] and fake_md.instances == []
    assert session.close()
    with pytest.raises(CtpGatewaySessionError):
        session.connection_status()


def test_lock_busy_is_bounded_and_does_not_enter_native(prepared):
    session, _, trader, native, _, _ = _open(prepared)
    locked, unlock = threading.Event(), threading.Event()
    def hold():
        with trader._query_state_lock:
            locked.set()
            assert unlock.wait(2)
    worker = threading.Thread(target=hold)
    worker.start()
    assert locked.wait(2)
    try:
        assert session.connection_status() == "busy"
        assert native.calls == []
    finally:
        unlock.set()
        worker.join(2)
    assert not worker.is_alive()
    assert session.connection_status() == "ready"
    assert session.close()


@pytest.mark.parametrize("disconnected", [False, True])
@pytest.mark.parametrize("change", [
    "session_feed", "session_trader", "feed_trader", "reader_feed", "reader_trader",
    "account", "reader_account", "service_generation", "api", "spi", "spi_api", "query_lock",
    "feed_broker", "feed_bound_broker", "feed_td", "feed_md", "feed_profile", "feed_environment",
    "trader_broker", "trader_bound_broker", "trader_td", "trader_bound_td", "trader_profile",
    "fingerprint", "issuer", "source_id", "source_generation", "spi_source_id", "lifecycle",
    "return_buffer", "feed_flow", "trader_flow", "flow_config", "direct", "direct_feed",
    "callback_tags", "login_identity",
])
def test_replaced_or_untrusted_binding_never_reports_reconnecting(prepared, change, disconnected):
    session, feed, trader, native, _, _ = _open(prepared)
    spi = trader._spi
    if disconnected:
        spi.OnFrontDisconnected(1)
    if change == "session_feed":
        session._feed = object()
    elif change == "session_trader":
        session._trader = object()
    elif change == "feed_trader":
        feed._trader = object()
    elif change == "reader_feed":
        session._reader._request_feed = object()
    elif change == "reader_trader":
        session._reader._trader = object()
    elif change == "account":
        session._account_key = replace(session._account_key, account_id="other")
    elif change == "reader_account":
        session._reader._account_key = replace(session._account_key, account_id="other")
    elif change == "service_generation":
        session._service_generation = "other-generation"
    elif change == "api":
        trader._api = object()
    elif change == "spi":
        trader._spi = _TraderSpi(trader, native)
    elif change == "spi_api":
        spi._native_api = object()
    elif change == "query_lock":
        trader._query_state_lock = threading.RLock()
    elif change == "feed_broker":
        feed.broker_id = "other"
    elif change == "feed_bound_broker":
        feed._execution_bound_broker_id = "other"
    elif change == "feed_td":
        feed.td_front = "tcp://other.invalid:1"
    elif change == "feed_md":
        feed.md_front = "tcp://other.invalid:1"
    elif change == "feed_profile":
        feed.ctp_env_profile = "other"
    elif change == "feed_environment":
        feed.ctp_environment = "other"
    elif change == "trader_broker":
        trader.broker_id = "other"
    elif change == "trader_bound_broker":
        trader._bound_broker_id = "other"
    elif change == "trader_td":
        trader.front = "tcp://other.invalid:1"
    elif change == "trader_bound_td":
        trader._bound_front = "tcp://other.invalid:1"
    elif change == "trader_profile":
        trader._TraderClient__bound_ctp_env_profile = "other"
    elif change == "fingerprint":
        trader._account_fingerprint = "other"
    elif change == "issuer":
        trader._query_evidence_issuer = object()
    elif change == "source_id":
        trader._callback_source_instance_id = "other"
    elif change == "source_generation":
        trader._native_api_generation += 1
    elif change == "spi_source_id":
        spi._native_spi_source_id = "other"
    elif change == "lifecycle":
        trader._lifecycle_generation += 1
    elif change == "return_buffer":
        feed._gateway_return_buffer = object()
    elif change == "feed_flow":
        feed._trusted_flow = object()
    elif change == "trader_flow":
        trader._trusted_flow = object()
    elif change == "flow_config":
        session._flow.scope_hash = "changed"
    elif change == "direct":
        session._direct = object()
    elif change == "direct_feed":
        session._direct._feed = object()
    elif change == "callback_tags":
        spi._callback_source_tags = object()
    else:
        trader._login_identity_observation = object()
    with pytest.raises(CtpGatewaySessionError):
        session.connection_status()


def test_wrong_owner_rejected_before_query_lock(prepared):
    session, _, trader, _, _, _ = _open(prepared)
    errors = []
    def other():
        try:
            session.connection_status()
        except CtpGatewaySessionError as exc:
            errors.append(exc.code)
    with trader._query_state_lock:
        worker = threading.Thread(target=other)
        worker.start()
        worker.join(1)
        assert not worker.is_alive()
    assert errors == ["WRONG_OWNER"]
    assert session.close()


def test_wrong_account_login_is_error_not_reconnecting(prepared, monkeypatch):
    session, _, trader, native, _, _ = _open(prepared)
    spi = trader._spi
    monkeypatch.setattr(native, "ReqUserLogin", lambda *_args: 0, raising=False)
    spi.OnFrontDisconnected(1)
    spi.OnFrontConnected()
    spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=0, ErrorMsg=""),
                          trader._authentication_request_id, True)
    spi.OnRspUserLogin(
        SimpleNamespace(BrokerID="9999", UserID="other", TradingDay=TODAY,
                        FrontID=7, SessionID=20, MaxOrderRef="0"),
        SimpleNamespace(ErrorID=0, ErrorMsg=""), trader._login_request_id, True,
    )
    with pytest.raises(CtpGatewaySessionError, match="SESSION_CHANGED"):
        session.connection_status()
    assert session.close()


def test_status_does_not_read_flow_filesystem_and_checks_process(prepared, monkeypatch):
    session, feed, trader, _, _, _ = _open(prepared)
    def forbid():
        pytest.fail("status must not read flow filesystem")
    with monkeypatch.context() as patches:
        for flow in (session._flow, feed._trusted_flow, trader._trusted_flow):
            patches.setattr(flow, "verify_root", forbid)
            patches.setattr(flow, "_capture_root", forbid)
        assert session.connection_status() == "ready"
        original_pid = session._owner_pid
        session._owner_pid += 1
        with pytest.raises(CtpGatewaySessionError, match="WRONG_OWNER"):
            session.connection_status()
        session._owner_pid = original_pid
    assert session.close()


def test_real_same_session_retry_for_auth_and_login_reaches_ready(prepared, monkeypatch):
    from bt_api_ctp.ctp import client as client_module
    session, _, trader, native, _, _ = _open(prepared)
    spi = trader._spi
    calls = []
    auth_retried, login_retried = threading.Event(), threading.Event()
    timers = []
    def authenticate(_field, request_id):
        calls.append(("auth", request_id))
        if sum(kind == "auth" for kind, _ in calls) == 2:
            auth_retried.set()
        return 0
    def login(_field, request_id):
        calls.append(("login", request_id))
        if sum(kind == "login" for kind, _ in calls) == 2:
            login_retried.set()
        return 0
    monkeypatch.setattr(client_module, "relogin_delay", lambda _attempt: .4)
    monkeypatch.setattr(native, "ReqAuthenticate", authenticate)
    monkeypatch.setattr(native, "ReqUserLogin", login, raising=False)
    trader.session_retry_enabled = True
    try:
        spi.OnFrontDisconnected(1)
        spi.OnFrontConnected()
        spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=7, ErrorMsg=""),
                              trader._authentication_request_id, True)
        timers.append(trader._session_retry_timer)
        assert type(timers[-1]) is threading.Timer
        assert session.connection_status() == "reconnecting"
        assert auth_retried.wait(3)
        assert session.connection_status() == "reconnecting"
        spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=0, ErrorMsg=""),
                              trader._authentication_request_id, True)
        spi.OnRspUserLogin(None, SimpleNamespace(ErrorID=8, ErrorMsg=""),
                           trader._login_request_id, True)
        timers.append(trader._session_retry_timer)
        assert type(timers[-1]) is threading.Timer
        assert session.connection_status() == "reconnecting"
        assert login_retried.wait(3)
        assert session.connection_status() == "reconnecting"
        spi.OnRspUserLogin(
            SimpleNamespace(BrokerID="9999", UserID="account", TradingDay=TODAY,
                            FrontID=7, SessionID=20, MaxOrderRef="0"),
            SimpleNamespace(ErrorID=0, ErrorMsg=""), trader._login_request_id, True,
        )
        assert session.connection_status() == "ready"
        assert [kind for kind, _ in calls] == ["auth", "auth", "login", "login"]
    finally:
        for timer in timers:
            if timer is not None:
                timer.cancel()
                timer.join(2)
                assert not timer.is_alive()
        assert session.close()


@pytest.mark.parametrize("change", ["cancelled", "token", "timer", "retry_generation",
                                   "binding_token", "binding_generation", "binding_api",
                                   "binding_spi", "binding_step", "timer_args", "timer_function"])
def test_untrusted_or_cancelled_retry_never_means_reconnecting(prepared, monkeypatch, change):
    from bt_api_ctp.ctp import client as client_module
    session, _, trader, native, _, _ = _open(prepared)
    spi = trader._spi
    monkeypatch.setattr(client_module, "relogin_delay", lambda _attempt: 30)
    trader.session_retry_enabled = True
    spi.OnFrontDisconnected(1)
    spi.OnFrontConnected()
    spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=7, ErrorMsg=""),
                          trader._authentication_request_id, True)
    timer = trader._session_retry_timer
    binding = trader._session_retry_binding
    assert session.connection_status() == "reconnecting"
    try:
        if change == "cancelled":
            timer.cancel()
        elif change == "token":
            trader._session_retry_token = object()
        elif change == "timer":
            trader._session_retry_timer = object()
        elif change == "retry_generation":
            trader._session_retry_generation += 1
        elif change == "binding_token":
            trader._session_retry_binding = replace(binding, token=object())
        elif change == "binding_generation":
            trader._session_retry_binding = replace(binding, generation=binding.generation + 1)
        elif change == "binding_api":
            trader._session_retry_binding = replace(binding, api=object())
        elif change == "binding_spi":
            trader._session_retry_binding = replace(binding, spi=object())
        elif change == "binding_step":
            trader._session_retry_binding = replace(binding, step="login")
        elif change == "timer_args":
            timer.args = (binding.token, binding.generation + 1, binding.step, binding.api, binding.spi)
        else:
            timer.function = lambda *_args: None
        with pytest.raises(CtpGatewaySessionError, match="SESSION_CHANGED"):
            session.connection_status()
    finally:
        timer.cancel()
        timer.join(2)
        assert not timer.is_alive()
        # Restore the real timer pointer so provider cleanup uses its normal path.
        trader._session_retry_timer = timer
        assert session.close()


def test_old_retry_token_cannot_authorize_later_generation(prepared, monkeypatch):
    from bt_api_ctp.ctp import client as client_module
    session, _, trader, _, _, _ = _open(prepared)
    spi = trader._spi
    trader.session_retry_enabled = True
    monkeypatch.setattr(client_module, "relogin_delay", lambda _attempt: 30)
    timers = []
    try:
        spi.OnFrontDisconnected(1)
        spi.OnFrontConnected()
        spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=7, ErrorMsg=""),
                              trader._authentication_request_id, True)
        old = trader._session_retry_binding
        timers.append(old.timer)
        spi.OnFrontDisconnected(2)
        assert session.connection_status() == "reconnecting"
        assert trader._session_retry_binding is None
        spi.OnFrontConnected()
        spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=7, ErrorMsg=""),
                              trader._authentication_request_id, True)
        current = trader._session_retry_binding
        timers.append(current.timer)
        assert current.generation > old.generation
        trader._session_retry_due(old.token, old.generation, old.step, old.api, old.spi)
        assert trader._session_retry_binding is current
        assert session.connection_status() == "reconnecting"
        trader._session_retry_token = old.token
        with pytest.raises(CtpGatewaySessionError, match="SESSION_CHANGED"):
            session.connection_status()
    finally:
        for timer in timers:
            timer.cancel()
            timer.join(2)
            assert not timer.is_alive()
        assert session.close()


def test_nonretryable_credential_refusal_stays_fatal(prepared):
    session, _, trader, _, _, _ = _open(prepared)
    trader.session_retry_enabled = True
    spi = trader._spi
    spi.OnFrontDisconnected(1)
    spi.OnFrontConnected()
    spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=63, ErrorMsg=""),
                          trader._authentication_request_id, True)
    assert trader._session_retry_binding is None and trader._session_retry_timer is None
    with pytest.raises(CtpGatewaySessionError, match="SESSION_CHANGED"):
        session.connection_status()
    assert session.close()


def test_retry_start_failure_clears_private_binding(prepared, monkeypatch):
    from bt_api_ctp.ctp import client as client_module
    session, _, trader, _, _, _ = _open(prepared)
    trader.session_retry_enabled = True
    monkeypatch.setattr(client_module, "relogin_delay", lambda _attempt: 30)
    created = []
    class CannotStartTimer(threading.Timer):
        def start(self):
            assert trader._query_state_lock._is_owned()
            assert trader._session_retry_binding.timer is self
            assert trader._session_retry_binding.token is trader._session_retry_token
            raise RuntimeError("synthetic timer failure")
    def make_timer(delay, function, args):
        timer = CannotStartTimer(delay, function, args=args)
        created.append(timer)
        return timer
    trader._session_retry_timer_factory = make_timer
    spi = trader._spi
    spi.OnFrontDisconnected(1)
    spi.OnFrontConnected()
    with pytest.raises(RuntimeError, match="synthetic timer failure"):
        spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=7, ErrorMsg=""),
                              trader._authentication_request_id, True)
    assert len(created) == 1 and created[0].finished.is_set()
    assert trader._session_retry_binding is None
    assert trader._session_retry_timer is None and trader._session_retry_token is None
    with pytest.raises(CtpGatewaySessionError, match="SESSION_CHANGED"):
        session.connection_status()
    assert session.close()


@pytest.mark.parametrize("stage", ["reader", "feed"])
def test_refresh_normalizes_only_exact_connection_error(prepared, monkeypatch, stage):
    from bt_api_base.exceptions import ExchangeConnectionError
    session, feed, _, _, _, _ = _open(prepared)
    def broken():
        raise ExchangeConnectionError("CTP", "synthetic private detail")
    if stage == "reader":
        monkeypatch.setattr(session._reader, "refresh_session", broken)
    else:
        monkeypatch.setattr(session._reader, "refresh_session", lambda: None)
        monkeypatch.setattr(feed, "get_query_session_scope", broken)
    with pytest.raises(CtpGatewaySessionError) as caught:
        session.refresh_session()
    assert caught.value.code == "SESSION_UNAVAILABLE"
    assert str(caught.value) == "SESSION_UNAVAILABLE"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
    assert session.close()


def _entry_points(session):
    """Every owner call that starts with the session's connected check."""
    return {
        "account_binding": lambda: session.account_binding,
        "read_account": lambda: session.read_account(generation=session._service_generation, timeout=1),
        "begin_read_account": lambda: session.begin_read_account(
            generation=session._service_generation, timeout=1),
    }


@pytest.mark.parametrize("entry", ["account_binding", "read_account", "begin_read_account"])
def test_a_link_drop_seen_by_the_connected_check_is_a_finite_session_code(prepared, monkeypatch, entry):
    """I63-E147: the owner samples the connection, then calls into the session. A front that
    drops in between used to surface here as a raw connection error, which the owner does not
    recognise as a link transition: its maintenance stopped for good."""
    from bt_api_base.exceptions import ExchangeConnectionError
    session, feed, _, _, _, _ = _open(prepared)

    def broken():
        raise ExchangeConnectionError("CTP", "synthetic private detail")
    scope_reader = feed.get_query_session_scope
    monkeypatch.setattr(feed, "get_query_session_scope", broken)
    with pytest.raises(CtpGatewaySessionError) as caught:
        _entry_points(session)[entry]()
    assert caught.value.code == "SESSION_UNAVAILABLE" and str(caught.value) == "SESSION_UNAVAILABLE"
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
    monkeypatch.setattr(feed, "get_query_session_scope", scope_reader)
    assert session.account_binding                      # the session itself is intact afterwards
    assert session.close()


def test_the_connected_check_refuses_a_feed_that_is_not_the_sessions_own(prepared, monkeypatch):
    session, feed, _, _, _, _ = _open(prepared)
    borrow = session._reader.borrow_request_feed
    monkeypatch.setattr(session._reader, "borrow_request_feed", lambda: object())
    with pytest.raises(CtpGatewaySessionError, match="^SESSION_CHANGED$"):
        session.account_binding
    monkeypatch.setattr(session._reader, "borrow_request_feed", borrow)
    assert session.close()


@pytest.mark.parametrize("kind", ["value", "authentication"])
def test_the_connected_check_preserves_other_exceptions(prepared, monkeypatch, kind):
    from bt_api_base.exceptions import AuthenticationError
    session, feed, _, _, _, _ = _open(prepared)
    error = ValueError("synthetic") if kind == "value" else AuthenticationError("CTP")

    def broken():
        raise error
    scope_reader = feed.get_query_session_scope
    monkeypatch.setattr(feed, "get_query_session_scope", broken)
    with pytest.raises(type(error)) as caught:
        session.account_binding
    assert caught.value is error
    monkeypatch.setattr(feed, "get_query_session_scope", scope_reader)
    assert session.close()


@pytest.mark.parametrize("stage", ["reader", "feed"])
@pytest.mark.parametrize("kind", ["value", "authentication"])
def test_refresh_preserves_other_exceptions(prepared, monkeypatch, stage, kind):
    from bt_api_base.exceptions import AuthenticationError
    session, feed, _, _, _, _ = _open(prepared)
    error = ValueError("synthetic") if kind == "value" else AuthenticationError("CTP")
    def broken():
        raise error
    if stage == "reader":
        monkeypatch.setattr(session._reader, "refresh_session", broken)
    else:
        monkeypatch.setattr(session._reader, "refresh_session", lambda: None)
        monkeypatch.setattr(feed, "get_query_session_scope", broken)
    with pytest.raises(type(error)) as caught:
        session.refresh_session()
    assert caught.value is error
    assert session.close()
