"""Offline Gateway CTP composition tests with synthetic native owners."""

import os
import threading

import pytest
from bt_api_base.account_protocol import AccountKey
from bt_api_base.direct_write_protocol import DirectWriteError

from bt_api_ctp.ctp.client import CtpNativeStopReceipt, TraderClient
from bt_api_ctp.feeds import live_ctp_feed
from bt_api_ctp.feeds.live_ctp_feed import CtpMarketStream, CtpRequestDataFuture
from bt_api_ctp.gateway_session import CtpGatewaySession, CtpGatewaySessionError
from bt_api_ctp.market_provider import CtpQuoteV2MarketProviderError
from bt_api_ctp.trusted_flow import flow_binding, trusted_flow
from test_iter62_settlement_readback_on_write import _NativeApi, _login
from test_market_provider import _ENTITLEMENT_REF, _SOURCE_REF, _SyntheticMdClient, _key

KEY = AccountKey(provider="ctp", venue="simnow", environment="simnow", account_id="account", subaccount_id="9999")
TD = "tcp://offline-neutral-td.invalid:30001"
MD = "tcp://offline-neutral-md.invalid:30011"


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    root = tmp_path / "flow"
    root.mkdir(mode=0o700)
    feeds, natives, streams, stops = [], [], [], []

    def start(trader, **_kwargs):
        native = _NativeApi()
        native.client = trader
        trader._api = native
        _login(trader, native, session_id=19)
        natives.append(native)

    def stop(trader, *, timeout):
        stops.append(trader)
        return CtpNativeStopReceipt(trader.connection_generation, False, True, True, False, False)

    class FakeMd(_SyntheticMdClient):
        instances = []
        def __init__(self, *args, **kwargs):
            self.trusted_flow_binding = flow_binding(trusted_flow(kwargs["trusted_flow_root"], kwargs["flow_account_scope"]))
            self.last_stop_receipt = None
            super().__init__(*args)
        def stop_and_wait(self, *, timeout):
            self.last_stop_receipt = super().stop_and_wait(timeout=timeout)
            return self.last_stop_receipt

    monkeypatch.setattr(TraderClient, "start", start)
    monkeypatch.setattr(TraderClient, "stop_and_wait", stop)
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", FakeMd)

    def feed_factory(**overrides):
        params = dict(broker_id="9999", user_id="account", password="synthetic", td_front=TD,
            md_front=MD, auto_settlement_confirm=False, ctp_env_profile="config_front_pair",
            ctp_environment="simnow", trusted_flow_root=str(root), flow_account_scope=KEY.account_key_id)
        params.update(overrides)
        feed = CtpRequestDataFuture(None, **params)
        feeds.append(feed)
        return feed

    def stream_factory(key, queue, **overrides):
        del key
        params = dict(broker_id="9999", user_id="account", password="synthetic", td_front=TD,
            md_front=MD, ctp_env_profile="config_front_pair", ctp_environment="simnow",
            trusted_flow_root=str(root), flow_account_scope=KEY.account_key_id)
        params.update(overrides)
        stream = CtpMarketStream(queue, **params)
        streams.append(stream)
        return stream

    def create(feed=feed_factory, stream=stream_factory, **overrides):
        params = dict(trusted_flow_root=str(root), flow_account_scope=KEY.account_key_id,
            source_ref=_SOURCE_REF, entitlement_ref=_ENTITLEMENT_REF, connect_timeout_seconds=1.0,
            stop_timeout_seconds=1.0)
        params.update(overrides)
        return CtpGatewaySession(KEY, feed, stream, **params)

    return create, feed_factory, stream_factory, feeds, natives, streams, stops, FakeMd


def _close_after_bounded_md_wrapper_exit(session, market, direct):
    """Retry once only for a stopped fake MD whose wrapper missed the join budget."""
    if session.close() is True:
        return
    stream = market._stream
    client = market._native_client
    receipt = session._reader.last_native_stop_receipt
    assert stream is not None and client is not None
    assert stream._md_client is None and stream.state.name == "DISCONNECTED"
    md_receipt = client.last_stop_receipt
    assert client.stop_wait_calls >= 1 and direct._owner_closed is True
    assert type(md_receipt) is CtpNativeStopReceipt and md_receipt.complete is True
    assert md_receipt.native_released is True and md_receipt.connection_generation == client.connection_generation
    assert type(receipt) is CtpNativeStopReceipt and receipt.complete is True
    thread = stream._thread
    assert thread is not None
    thread.join(timeout=2.0)
    assert not thread.is_alive(), "synthetic MD wrapper did not exit within the retry bound"
    assert session.close() is True, "session remained unconfirmed after wrapper exit"


def test_one_trader_one_market_and_two_symbols_share_session(prepared):
    create, _, _, feeds, natives, streams, stops, fake_md = prepared
    session = create()
    assert feeds == [] and natives == [] and streams == []
    session.connect(generation="service-g1", timeout=1.0)
    assert len(feeds) == len(natives) == 1 and streams == []
    direct = session.direct_provider
    assert direct._feed is feeds[0] and direct._trader() is feeds[0].trader_client
    assert direct._background_settlement_readback is False
    assert direct._monitor_thread is None
    assert session.account_binding == feeds[0].trader_client._account_fingerprint
    market = session.market_provider
    first = market.subscribe(_key(), generation="market-g1")
    second_key = _key(instrument=__import__("dataclasses").replace(_key().instrument, instrument_id="IH2612", native_symbol="IH2612"))
    second = market.subscribe(second_key, generation="market-g1")
    assert len(streams) == len(fake_md.instances) == 1
    assert market._known_handles[first].stream is market._known_handles[second].stream is streams[0]
    assert len(natives) == 1
    _close_after_bounded_md_wrapper_exit(session, market, direct)
    assert len(stops) == 1
    with pytest.raises(DirectWriteError, match="provider_closed"):
        direct.session_generation()


@pytest.mark.parametrize("fault", ["account", "flow", "connected", "environment", "scope"])
def test_bad_feed_is_rejected_without_adopting_or_stopping(prepared, fault):
    create, feed_factory, _, feeds, natives, _, stops, _ = prepared
    if fault == "scope":
        with pytest.raises(CtpGatewaySessionError, match="INVALID_CONFIGURATION"):
            create(flow_account_scope="wrong")
        return
    def factory():
        kwargs = {"account": {"user_id": "other"}, "flow": {"flow_account_scope": "other"},
                  "environment": {"ctp_environment": "live"}}.get(fault, {})
        feed = feed_factory(**kwargs)
        if fault == "connected":
            feed._trader = object()
            feed._connected = True
        return feed
    session = create(feed=factory)
    with pytest.raises(Exception):
        session.connect(generation="g", timeout=1)
    original = feeds[0]._trader
    assert natives == [] and session.close() is True and stops == []
    assert feeds[0]._trader is original


@pytest.mark.parametrize("fault", ["front", "flow", "account", "connected"])
def test_bad_market_stream_is_not_started_or_stopped(prepared, fault):
    create, _, stream_factory, _, _, streams, _, fake_md = prepared
    def factory(key, queue):
        kwargs = {"front": {"md_front": "tcp://different.invalid:99"},
                  "flow": {"flow_account_scope": "other"}, "account": {"user_id": "other"}}.get(fault, {})
        stream = stream_factory(key, queue, **kwargs)
        if fault == "connected":
            stream._md_client = object()
        return stream
    session = create(stream=factory)
    session.connect(generation="g", timeout=1)
    with pytest.raises(CtpQuoteV2MarketProviderError):
        session.market_provider.subscribe(_key(), generation="m")
    original = streams[0]._md_client
    assert fake_md.instances == [] and session.close() is True
    assert streams[0]._md_client is original


def test_reconnect_requires_refresh_and_stop_uses_current_generation(prepared, monkeypatch):
    create, _, _, feeds, natives, _, _, _ = prepared
    session = create()
    session.connect(generation="g", timeout=1)
    trader = feeds[0].trader_client
    old_scope = feeds[0].get_query_session_scope()
    def query_account(_field, request_id):
        trader._handle_query_callback("account", None, None, request_id, True)
        return 0
    monkeypatch.setattr(natives[0], "ReqQryTradingAccount", query_account, raising=False)
    old_result = feeds[0].query_account_result(timeout=1)
    assert old_result.complete
    _login(trader, natives[0], session_id=20)
    with pytest.raises(Exception):
        _ = session.account_binding
    session.refresh_session()
    assert feeds[0].get_query_session_scope().connection_generation != old_scope.connection_generation
    assert session.direct_provider._trader() is trader
    monkeypatch.setattr(feeds[0], "query_account_result", lambda *, timeout: old_result)
    with pytest.raises(Exception):
        session.read_account(generation="g", timeout=1)
    assert session.close() is True
    assert session._reader.last_native_stop_receipt.connection_generation == trader.connection_generation


def test_failed_close_keeps_custody_and_seals_borrowed_providers(prepared, monkeypatch):
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="g", timeout=1)
    direct, market = session.direct_provider, session.market_provider
    market.subscribe(_key(), generation="m")
    real_close = market.close
    monkeypatch.setattr(market, "close", lambda: False)
    assert session.close() is False
    assert session._feed is feeds[0]
    with pytest.raises(CtpQuoteV2MarketProviderError, match="CLOSING"):
        market.subscribe(_key(), generation="m")
    with pytest.raises(DirectWriteError, match="provider_closed"):
        direct.session_generation()
    monkeypatch.setattr(market, "close", real_close)
    _close_after_bounded_md_wrapper_exit(session, market, direct)


def test_wrong_thread_and_fork_reject_cached_providers_before_locks(prepared):
    import signal

    create, _, _, _, _, _, _, _ = prepared
    session = create()
    session.connect(generation="g", timeout=1)
    direct, market = session.direct_provider, session.market_provider
    errors = []
    def wrong_thread():
        for operation in (lambda: session.account_binding, direct.session_generation, lambda: market.diagnostics):
            try:
                operation()
            except Exception:
                errors.append(True)
    worker = threading.Thread(target=wrong_thread)
    worker.start()
    worker.join(timeout=2)
    assert len(errors) == 3
    # Hold the inherited lock across fork: a child must reject before acquiring it.
    ready, release = threading.Event(), threading.Event()
    def hold_lock():
        with direct._lock:
            ready.set()
            release.wait(5)
    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert ready.wait(2)
    pid = os.fork()
    if pid == 0:
        signal.alarm(2)
        try:
            for operation in (lambda: session.account_binding, direct.session_generation, lambda: market.diagnostics):
                try:
                    operation()
                except Exception:
                    continue
                os._exit(1)
            os._exit(0)
        except BaseException:
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    release.set()
    holder.join(timeout=2)
    assert os.waitstatus_to_exitcode(status) == 0
    assert session.close() is True


def test_interrupted_start_keeps_same_attempt_until_typed_stop(prepared, monkeypatch):
    from bt_api_ctp.ctp.client import CtpNativeStartupStopReceipt

    create, _, _, feeds, _, _, _, _ = prepared
    def interrupted(trader, **kwargs):
        trader._startup_api_attempt_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        trader._startup_api_phase = "factory_uncertain"
        trader._api = _NativeApi()
        raise RuntimeError("synthetic startup interrupted")
    def stop_startup(trader, *, timeout):
        if trader._startup_api_phase == "factory_uncertain":
            return CtpNativeStartupStopReceipt("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "factory_uncertain", 0,
                None, False, False, None, False, _client_binding=trader._startup_api_client_binding)
        return CtpNativeStartupStopReceipt("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "api_created_released", 0,
            True, False, True, False, False, _client_binding=trader._startup_api_client_binding)
    monkeypatch.setattr(TraderClient, "start", interrupted)
    monkeypatch.setattr(TraderClient, "stop_startup_and_wait", stop_startup)
    session = create()
    with pytest.raises(Exception):
        session.connect(generation="g", timeout=1)
    trader = feeds[0].trader_client
    assert trader is not None
    assert session.close() is False
    assert feeds[0].trader_client is trader
    trader._startup_api_phase = "api_created"
    assert session.close() is True
    assert feeds[0].trader_client is None


@pytest.mark.parametrize("failure", ["exception", "wrong_receipt"])
def test_failed_trader_stop_retains_exact_owner_until_receipt(prepared, monkeypatch, failure):
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="g", timeout=1)
    trader = feeds[0].trader_client
    real_stop = TraderClient.stop_and_wait
    def failed_stop(self, *, timeout):
        if failure == "exception":
            raise RuntimeError("synthetic stop failure")
        return CtpNativeStopReceipt(self.connection_generation + 1, False, True, True, False, False)
    monkeypatch.setattr(TraderClient, "stop_and_wait", failed_stop)
    assert session.close() is False
    assert feeds[0].trader_client is trader and session._feed is feeds[0]
    monkeypatch.setattr(TraderClient, "stop_and_wait", real_stop)
    assert session.close() is True


def test_public_market_handle_cannot_reach_owned_stream_and_copies_are_refused(prepared, monkeypatch):
    import copy
    from dataclasses import FrozenInstanceError, replace

    from bt_api_ctp.market_provider import CtpQuoteV2MarketHandle

    create, _, _, _, _, streams, _, _ = prepared
    session = create()
    session.connect(generation="g", timeout=1)
    market = session.market_provider
    handle = market.subscribe(_key(), generation="m")
    assert len(streams) == 1
    for name in ("stream", "queue", "owner_token", "source_scope", "active"):
        assert not hasattr(handle, name)
    with pytest.raises(FrozenInstanceError):
        handle.generation = "forged"
    for forged in (copy.copy(handle), replace(handle), CtpQuoteV2MarketHandle(handle.key, handle.generation)):
        assert market.unsubscribe(forged) is False
        assert market.poll(forged, limit=1) == ()
        assert market.source_epoch_reset_required(forged) is False
        assert market.source_recovery_state(forged) is None
        assert market.acknowledge_source_epoch_reset(forged, epoch="e") is False
        assert market.describe_source(forged, key=handle.key, generation="m",
                                      stream_id=handle.key.stream_id, epoch="e") is None
    assert market.diagnostics["active_handles"] == 1
    results = []
    def outsider():
        results.append(not hasattr(handle, "stream"))
        try:
            market.unsubscribe(handle)
        except CtpQuoteV2MarketProviderError:
            results.append(True)
    worker = threading.Thread(target=outsider)
    worker.start()
    worker.join(timeout=2)
    assert results == [True, True]
    close = market.close
    monkeypatch.setattr(market, "close", lambda: False)
    assert session.close() is False
    assert not hasattr(handle, "stream")
    with pytest.raises(CtpQuoteV2MarketProviderError):
        market.unsubscribe(handle)
    monkeypatch.setattr(market, "close", close)
    _close_after_bounded_md_wrapper_exit(session, market, session._direct)


def test_close_waits_for_exact_md_wrapper_even_after_native_receipt(prepared, monkeypatch):
    """A confirmed fake native stop cannot substitute for a live wrapper exit."""
    create, _, _, _, _, streams, stops, fake_md = prepared
    running, release = threading.Event(), threading.Event()

    def blocked_wrapper(stream):
        stream.connect()
        running.set()
        release.wait(timeout=3.0)

    monkeypatch.setattr(CtpMarketStream, "_run_loop", blocked_wrapper)
    session = create(stop_timeout_seconds=0.05)
    session.connect(generation="g", timeout=1)
    market, direct = session.market_provider, session.direct_provider
    market.subscribe(_key(), generation="m")
    assert running.wait(timeout=1)
    stream = streams[0]
    client = fake_md.instances[0]
    try:
        assert session.close() is False
        assert market._stream is stream and market._native_client is client
        assert stream._md_client is None and stream.state.name == "DISCONNECTED"
        assert stream._thread is not None and stream._thread.is_alive()
        assert client.stop_wait_calls == 1 and direct._owner_closed is True
        assert client.last_stop_receipt.complete is True
        assert len(stops) == 1
        assert session._reader.last_native_stop_receipt.complete is True
    finally:
        release.set()
        stream._thread.join(timeout=2)
    assert not stream._thread.is_alive()
    assert session.close() is True
    assert client.stop_wait_calls == 2 and len(stops) == 1
