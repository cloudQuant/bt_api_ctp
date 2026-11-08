"""Synthetic Trader-only source and native instrument query coverage."""
from dataclasses import replace
from decimal import Decimal
import threading
import time

import pytest
from bt_api_base.account_protocol import AccountKey
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.gateway_instrument_query import (
    CtpGatewayInstrumentQueryError, CtpGatewayInstrumentQueryReader,
)
from bt_api_ctp.query import _attach_query_source
from test_iter62_settlement_readback_on_write import _NativeApi, _login, TD_FRONT

ACCOUNT = AccountKey("ctp", "simnow", "simnow", "account", "9999")
KEY = MarketInstrumentKey("ctp", "SHFE", "simnow", "future", "rb2701", "rb2701")


class Native(_NativeApi):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.rows = []
        self.respond = True

    def ReqQryInstrument(self, field, request_id):
        self.requests.append((field.InstrumentID, field.ExchangeID, request_id))
        if self.respond:
            for row in self.rows:
                self.client._handle_query_callback("instruments", row, None, request_id, False)
            self.client._handle_query_callback("instruments", None, None, request_id, True)
        return 0


def row(**changes):
    result = dict(InstrumentID="rb2701", ExchangeID="SHFE", ProductID="rb",
                  ProductClass="1", VolumeMultiple=10, PriceTick=1.0,
                  MinLimitOrderVolume=3, MaxLimitOrderVolume=100, IsTrading=1)
    result.update(changes)
    return result


def setup():
    native = Native()
    client = TraderClient(TD_FRONT, "9999", "account", "synthetic-only",
                          auto_settlement_confirm=False)
    native.client = client
    client._api = native
    _login(client, native, session_id=19)
    reader = CtpGatewayInstrumentQueryReader(client, account_key=ACCOUNT)
    return reader, client, native


def finish(reader, handle):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = reader.poll(handle)
        if value is not None:
            return value
        time.sleep(.005)
    pytest.fail("query did not finish")


def test_one_exact_query_preserves_rules_and_completion_ticket():
    reader, _, native = setup()
    native.rows = [row()]
    handle = reader.begin(KEY)
    assert native.requests == []
    value = finish(reader, handle)
    assert [(a, b) for a, b, _ in native.requests] == [("rb2701", "SHFE")]
    assert value.rules["min_quantity"] == Decimal(3)
    assert value.rules["max_quantity"] == Decimal(100)
    assert value.rules["price_tick"] == Decimal("1.0")
    assert value.rules["contract_value"] == Decimal(10)
    assert value.rules["status"] == "trading"
    assert len(value.rules["raw_rule_fingerprint"]) == 64
    assert value.expires_monotonic == value._source.completed_monotonic + 35
    with pytest.raises(TypeError):
        value.rules["min_quantity"] = Decimal(1)
    assert reader.poll(handle) is value
    assert reader.release(handle) is True
    reader.assert_current(value)
    with pytest.raises(CtpGatewayInstrumentQueryError):
        reader.assert_current(replace(value))


@pytest.mark.parametrize("rows", [[], [row(), row()], [row(InstrumentID="wrong")],
                                  [row(ExchangeID="DCE")], [row(ProductClass="2")]])
def test_wrong_or_nonunique_rows_rejected(rows):
    reader, _, native = setup()
    native.rows = rows
    handle = reader.begin(KEY)
    with pytest.raises(CtpGatewayInstrumentQueryError):
        finish(reader, handle)
    assert reader.release(handle)


@pytest.mark.parametrize("field,value", [
    ("VolumeMultiple", 0), ("PriceTick", 0), ("PriceTick", float("nan")),
    ("MinLimitOrderVolume", 0), ("MinLimitOrderVolume", 101),
    ("MaxLimitOrderVolume", 0), ("IsTrading", 2), ("ProductID", ""),
])
def test_invalid_native_fields_rejected(field, value):
    reader, _, native = setup()
    native.rows = [row(**{field: value})]
    with pytest.raises(CtpGatewayInstrumentQueryError):
        finish(reader, reader.begin(KEY))


@pytest.mark.parametrize("field", ["ProductID", "VolumeMultiple", "PriceTick",
                                   "MinLimitOrderVolume", "MaxLimitOrderVolume", "IsTrading"])
def test_missing_native_fields_rejected(field):
    reader, _, native = setup()
    value = row()
    del value[field]
    native.rows = [value]
    with pytest.raises(CtpGatewayInstrumentQueryError):
        finish(reader, reader.begin(KEY))


@pytest.mark.parametrize("fault", ["filters", "intent", "source", "day", "digest", "limit", "terminal", "expiry"])
def test_tampered_query_certificate_rejected(monkeypatch, fault):
    reader, client, native = setup()
    native.rows = [row()]
    original = client.poll_instruments_query
    def tamper(handle):
        result = original(handle)
        if result is None:
            return None
        source = result.query_source
        if fault == "filters":
            source = replace(source, request_filters=())
        elif fault == "intent":
            source = replace(source, request_intent_filters=())
        elif fault == "source":
            source = replace(source, issuer=object())
        elif fault == "day":
            source = replace(source, trading_day="19990101")
        elif fault == "digest":
            source = replace(source, records_sha256="0" * 64)
        elif fault == "limit":
            source = replace(source, record_limit=3)
        elif fault == "terminal":
            result = replace(result, is_last_seen=False)
        else:
            source = replace(source, trusted_expires_monotonic=source.completed_monotonic)
        return _attach_query_source(result, source)
    monkeypatch.setattr(client, "poll_instruments_query", tamper)
    with pytest.raises(CtpGatewayInstrumentQueryError):
        finish(reader, reader.begin(KEY))


def test_cancel_and_wrong_owner():
    reader, _, native = setup()
    native.respond = False
    handle = reader.begin(KEY)
    assert reader.release(handle) is False
    errors = []
    def other_thread():
        try:
            reader.poll(handle)
        except CtpGatewayInstrumentQueryError as exc:
            errors.append(exc.code)
    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join(2)
    assert errors == ["OWNER_REQUIRED"]
    assert reader.cancel(handle) is True
    assert reader.release(handle) is True
    assert native.requests == []


def test_relogin_invalidates_observation_and_other_reader_refuses_it():
    reader, client, native = setup()
    native.rows = [row()]
    value = finish(reader, reader.begin(KEY))
    other = CtpGatewayInstrumentQueryReader(client, account_key=ACCOUNT)
    with pytest.raises(CtpGatewayInstrumentQueryError):
        other.assert_current(value)
    _login(client, native, session_id=20)
    with pytest.raises(CtpGatewayInstrumentQueryError):
        reader.assert_current(value)


def test_expired_observation_and_mutated_source_are_rejected(monkeypatch):
    from types import SimpleNamespace
    import bt_api_ctp.gateway_instrument_query as module
    reader, _, native = setup()
    native.rows = [row()]
    value = finish(reader, reader.begin(KEY))
    value._result.records[0]["MinLimitOrderVolume"] = 1
    with pytest.raises(CtpGatewayInstrumentQueryError):
        reader.assert_current(value)
    value._result.records[0]["MinLimitOrderVolume"] = 3
    reader.assert_current(value)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: value.expires_monotonic))
    with pytest.raises(CtpGatewayInstrumentQueryError):
        reader.assert_current(value)


def test_wrong_process_and_request_scope(monkeypatch):
    import bt_api_ctp.gateway_instrument_query as module
    reader, _, native = setup()
    with pytest.raises(CtpGatewayInstrumentQueryError):
        reader.begin(replace(KEY, venue="not-an-exchange"))
    with pytest.raises(CtpGatewayInstrumentQueryError):
        reader.begin(replace(KEY, native_symbol="other"))
    with pytest.raises(CtpGatewayInstrumentQueryError):
        reader.begin(KEY, timeout_seconds=True)
    monkeypatch.setattr(module.os, "getpid", lambda: reader._pid + 1)
    with pytest.raises(CtpGatewayInstrumentQueryError, match="OWNER_REQUIRED"):
        reader.begin(KEY)
    assert native.requests == []


def test_native_instrument_struct_projection():
    from bt_api_ctp.ctp.ctp_structs_market import CThostFtdcInstrumentField
    reader, _, native = setup()
    field = CThostFtdcInstrumentField()
    for name, value in row().items():
        setattr(field, name, value)
    native.rows = [field]
    observation = finish(reader, reader.begin(KEY))
    assert observation.rules["min_quantity"] == Decimal(3)
    assert observation.rules["max_quantity"] == Decimal(100)


from test_iter63_gateway_session import prepared


def test_session_factory_borrows_exact_trader_without_md(prepared):
    create, _, _, feeds, natives, streams, _, fake_md = prepared
    session = create()
    session.connect(generation="g", timeout=1)
    native = natives[0]
    requests = []
    def query(field, request_id):
        requests.append((field.InstrumentID, field.ExchangeID))
        native.client._handle_query_callback("instruments", row(), None, request_id, False)
        native.client._handle_query_callback("instruments", None, None, request_id, True)
        return 0
    native.ReqQryInstrument = query
    reader = session.create_instrument_query_reader()
    assert reader._trader is feeds[0].trader_client
    observation = finish(reader, reader.begin(KEY))
    assert observation.rules["min_quantity"] == Decimal(3)
    assert requests == [("rb2701", "SHFE")]
    assert len(natives) == 1 and streams == [] and fake_md.instances == []
    assert session.close() is True
    with pytest.raises(CtpGatewayInstrumentQueryError):
        reader.assert_current(observation)


def test_limit_rules_do_not_use_market_order_limits():
    reader, _, native = setup()
    native.rows = [row(MinMarketOrderVolume=7, MaxMarketOrderVolume=9, IsTrading=0)]
    value = finish(reader, reader.begin(KEY))
    assert value.rules["min_quantity"] == Decimal(3)
    assert value.rules["max_quantity"] == Decimal(100)
    assert value.rules["status"] == "disabled"


def test_invalid_account_and_multiple_live_observations():
    reader, client, native = setup()
    with pytest.raises(CtpGatewayInstrumentQueryError):
        CtpGatewayInstrumentQueryReader(client, account_key=replace(ACCOUNT, account_id="other"))
    native.rows = [row()]
    first_handle = reader.begin(KEY)
    first = finish(reader, first_handle)
    assert reader.release(first_handle)
    second_handle = reader.begin(KEY)
    second = finish(reader, second_handle)
    assert reader.release(second_handle)
    reader.assert_current(first)
    reader.assert_current(second)


def test_issued_timeout_release_and_late_terminal():
    reader, client, native = setup()
    native.respond = False
    handle = reader.begin(KEY, timeout_seconds=.05)
    assert reader.poll(handle) is None
    assert len(native.requests) == 1
    request_id = native.requests[0][2]
    time.sleep(.07)
    with pytest.raises(CtpGatewayInstrumentQueryError):
        reader.poll(handle)
    assert client._query_native_inflight is not None
    assert reader.release(handle) is True
    assert client._query_native_inflight is not None
    client._handle_query_callback("instruments", None, None, request_id, True)
    assert client._query_native_inflight is None
    assert client.get_query_result(request_id).late_callback_count >= 1


def test_session_change_permits_local_cleanup_and_new_reader():
    reader, client, native = setup()
    handle = reader.begin(KEY)
    _login(client, native, session_id=20)
    with pytest.raises(CtpGatewayInstrumentQueryError):
        reader.poll(handle)
    assert reader.cancel(handle) is True
    assert reader.release(handle) is True
    fresh = CtpGatewayInstrumentQueryReader(client, account_key=ACCOUNT)
    native.rows = [row()]
    result = finish(fresh, fresh.begin(KEY))
    assert result.rules["min_quantity"] == Decimal(3)


def test_bounded_handles_and_cross_reader_rejection():
    reader, client, native = setup()
    other = CtpGatewayInstrumentQueryReader(client, account_key=ACCOUNT)
    handles = [reader.begin(KEY) for _ in range(8)]
    with pytest.raises(CtpGatewayInstrumentQueryError, match="CAPACITY_EXCEEDED"):
        reader.begin(KEY)
    for operation in (other.poll, other.cancel, other.release):
        with pytest.raises(CtpGatewayInstrumentQueryError, match="HANDLE_UNTRUSTED"):
            operation(handles[0])
    for handle in handles:
        assert reader.cancel(handle)
        assert reader.release(handle)
    assert native.requests == []


def test_begin_session_race_returns_cleanup_handle_even_when_lock_busy(monkeypatch):
    _, client, native = setup()
    original = client.begin_instruments_query
    session_id = 20

    def begin_then_relogin(**kwargs):
        nonlocal session_id
        handle = original(**kwargs)
        _login(client, native, session_id=session_id)
        session_id += 1
        return handle

    monkeypatch.setattr(client, "begin_instruments_query", begin_then_relogin)
    # Repeat beyond both reader and Trader limits; inspect the actual registry
    # so a false successful cleanup cannot hide orphan tokens.
    for _ in range(65):
        reader = CtpGatewayInstrumentQueryReader(client, account_key=ACCOUNT)
        handle = reader.begin(KEY)
        with pytest.raises(CtpGatewayInstrumentQueryError, match="SESSION_CHANGED"):
            reader.poll(handle)
        assert len(reader._pending) == 1
        assert len(client._async_instrument_queries) == 1
        locked, unlock = threading.Event(), threading.Event()

        def hold_query_lock():
            with client._query_state_lock:
                locked.set()
                assert unlock.wait(2)

        worker = threading.Thread(target=hold_query_lock)
        worker.start()
        assert locked.wait(2)
        try:
            assert reader.cancel(handle) is False
            assert reader.release(handle) is False
            assert len(reader._pending) == 1
            assert len(client._async_instrument_queries) == 1
        finally:
            unlock.set()
            worker.join(2)
        assert not worker.is_alive()
        assert reader.cancel(handle) is True
        assert reader.release(handle) is True
        assert reader._pending == {}
        assert client._async_instrument_queries == {}
    assert native.requests == []


def test_gateway_session_borrowed_trader_real_spi_rows_and_stale_spi(prepared):
    from bt_api_ctp.ctp.client import _TraderSpi
    from bt_api_ctp.ctp.ctp_structs_market import CThostFtdcInstrumentField

    create, _, _, feeds, natives, streams, _, fake_md = prepared
    session = create()
    session.connect(generation="g-spi", timeout=1)
    trader, native = feeds[0].trader_client, natives[0]
    spi = trader._spi
    stale_spi = _TraderSpi(trader, native)
    assert stale_spi is not spi
    actual = CThostFtdcInstrumentField()
    stale = CThostFtdcInstrumentField()
    for field_name, field_value in row().items():
        setattr(actual, field_name, field_value)
        setattr(stale, field_name, field_value)
    stale.MinLimitOrderVolume = 77
    observed = []

    def query(field, request_id):
        observed.append((field.InstrumentID, field.ExchangeID))
        stale_spi.OnRspQryInstrument(stale, None, request_id, True)
        accumulator = trader._query_history[request_id]
        assert accumulator.records == [] and accumulator.is_last_seen is False
        spi.OnRspQryInstrument(actual, None, request_id, False)
        spi.OnRspQryInstrument(None, None, request_id, True)
        return 0

    native.ReqQryInstrument = query
    reader = session.create_instrument_query_reader()
    assert reader._trader is trader
    handle = reader.begin(KEY)
    result = finish(reader, handle)
    assert result.rules["min_quantity"] == Decimal(3)
    assert result.rules["max_quantity"] == Decimal(100)
    assert observed == [("rb2701", "SHFE")]
    assert len(natives) == 1 and streams == [] and fake_md.instances == []
    reader.assert_current(result)
    assert reader.release(handle)
    assert session.close()


def _probe_on_other_thread(reader, observation):
    values = []
    worker = threading.Thread(target=lambda: values.append(reader.observation_current(observation)))
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
    return values[0]


def test_cross_thread_observation_probe_is_read_only_and_rejects_relogin(monkeypatch):
    import bt_api_ctp.registered_sim_session as sessions
    import bt_api_ctp.gateway_return_query as return_queries
    reader, client, native = setup()
    native.rows = [row()]
    result = finish(reader, reader.begin(KEY))
    # The cross-thread probe must not call any session-capture or native query.
    def forbid(*_args, **_kwargs):
        raise AssertionError("probe must not capture or submit")
    monkeypatch.setattr(sessions, "capture_query_session", forbid)
    monkeypatch.setattr(return_queries, "capture_query_session", forbid)
    monkeypatch.setattr(return_queries, "validate_query_session", forbid)
    monkeypatch.setattr(reader._guard, "_capture_locked", forbid)
    monkeypatch.setattr(client, "get_query_session_scope", forbid)
    monkeypatch.setattr(client, "get_session_state", forbid)
    monkeypatch.setattr(client, "begin_instruments_query", forbid)
    monkeypatch.setattr(client, "poll_instruments_query", forbid)
    assert _probe_on_other_thread(reader, result) is True
    result._result.records[0]["MinLimitOrderVolume"] = 4
    assert _probe_on_other_thread(reader, result) is False
    result._result.records[0]["MinLimitOrderVolume"] = 3
    assert _probe_on_other_thread(reader, result) is True
    assert _probe_on_other_thread(reader, replace(result)) is False
    # Restore the test login helper's session diagnostics before relogin.
    monkeypatch.undo()
    _login(client, native, session_id=20)
    assert _probe_on_other_thread(reader, result) is False


def test_cross_thread_probe_lock_contention_is_unknown():
    reader, client, native = setup()
    native.rows = [row()]
    result = finish(reader, reader.begin(KEY))
    with client._query_state_lock:
        assert _probe_on_other_thread(reader, result) is None
    with reader._observations_lock:
        assert _probe_on_other_thread(reader, result) is None
    assert _probe_on_other_thread(reader, result) is True


def test_cross_thread_probe_expiry_and_wrong_process(monkeypatch):
    from types import SimpleNamespace
    import bt_api_ctp.gateway_instrument_query as module
    reader, _, native = setup()
    native.rows = [row()]
    result = finish(reader, reader.begin(KEY))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: result.expires_monotonic))
    assert _probe_on_other_thread(reader, result) is False
    monkeypatch.undo()
    monkeypatch.setattr(module.os, "getpid", lambda: reader._pid + 1)
    assert _probe_on_other_thread(reader, result) is False


def test_cross_thread_probe_bound_gateway_session_generation(prepared):
    create, _, _, feeds, natives, _, _, _ = prepared
    session = create()
    session.connect(generation="original-service", timeout=1)
    trader, native = feeds[0].trader_client, natives[0]
    def query(_field, request_id):
        trader._spi.OnRspQryInstrument(row(), None, request_id, False)
        trader._spi.OnRspQryInstrument(None, None, request_id, True)
        return 0
    native.ReqQryInstrument = query
    reader = session.create_instrument_query_reader()
    result = finish(reader, reader.begin(KEY))
    assert _probe_on_other_thread(reader, result) is True
    session._service_generation = "other-service"
    assert _probe_on_other_thread(reader, result) is False
    session._service_generation = "original-service"
    assert session.close()
    assert _probe_on_other_thread(reader, result) is False


def test_instrument_reader_rebinds_without_refreshing_account_epoch(prepared, monkeypatch):
    from types import SimpleNamespace
    from bt_api_ctp.account_provider import CtpAccountSessionProviderError
    from test_iter62_settlement_readback_on_write import TODAY

    create, _, _, feeds, natives, streams, stops, fake_md = prepared
    session = create()
    session.connect(generation="g-static-rules", timeout=1)
    feed, native = feeds[0], natives[0]
    trader, spi = feed.trader_client, feed.trader_client._spi
    calls = []
    def query(field, request_id):
        calls.append((field.InstrumentID, field.ExchangeID))
        spi.OnRspQryInstrument(row(), None, request_id, False)
        spi.OnRspQryInstrument(None, None, request_id, True)
        return 0
    monkeypatch.setattr(native, "ReqQryInstrument", query, raising=False)
    monkeypatch.setattr(native, "ReqUserLogin", lambda *_args: 0, raising=False)
    old_reader = session.create_instrument_query_reader()
    old_handle = old_reader.begin(KEY)
    old = finish(old_reader, old_handle)
    assert old_reader.release(old_handle)
    old_account_scope = session._reader._session_scope
    old_generation = trader._connection_generation

    spi.OnFrontDisconnected(1)
    spi.OnFrontConnected()
    spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=0, ErrorMsg=""),
                          trader._authentication_request_id, True)
    spi.OnRspUserLogin(
        SimpleNamespace(BrokerID="9999", UserID="account", TradingDay=TODAY,
                        FrontID=7, SessionID=20, MaxOrderRef="0"),
        SimpleNamespace(ErrorID=0, ErrorMsg=""), trader._login_request_id, True,
    )
    assert trader._spi is spi and trader._connection_generation > old_generation
    assert session.connection_status() == "ready"
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_SCOPE_CHANGED"):
        session._reader.borrow_request_feed()
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_SCOPE_CHANGED"):
        session.create_return_query_reader()
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_SCOPE_CHANGED"):
        _ = session.market_provider
    assert session._reader._session_scope is old_account_scope
    monkeypatch.setattr(session._reader, "refresh_session", lambda: pytest.fail("implicit refresh"))
    monkeypatch.setattr(feed, "connect", lambda: pytest.fail("implicit feed connect"))
    new_reader = session.create_instrument_query_reader()
    new_handle = new_reader.begin(KEY)
    new = finish(new_reader, new_handle)
    assert new.rules["min_quantity"] == Decimal(3)
    assert new.connection_generation == trader._connection_generation
    assert new_reader.release(new_handle)
    assert old_reader.observation_current(old) is False
    with pytest.raises(CtpGatewayInstrumentQueryError):
        old_reader.assert_current(old)
    assert calls == [("rb2701", "SHFE"), ("rb2701", "SHFE")]
    assert session._reader._session_scope is old_account_scope
    assert len(feeds) == len(natives) == 1 and streams == [] and fake_md.instances == []
    assert session.close() and len(stops) == 1


def test_instrument_session_busy_and_reconnecting_have_distinct_errors(prepared):
    from bt_api_ctp.gateway_session import CtpGatewaySessionError
    session, _, trader, _, _, _ = _open_session_for_instrument(prepared)
    reader = session.create_instrument_query_reader()
    locked, unlock = threading.Event(), threading.Event()
    def hold():
        with trader._query_state_lock:
            locked.set()
            assert unlock.wait(2)
    worker = threading.Thread(target=hold)
    worker.start()
    assert locked.wait(2)
    try:
        with pytest.raises(CtpGatewaySessionError, match="INSTRUMENT_SESSION_BUSY"):
            session.create_instrument_query_reader()
        with pytest.raises(CtpGatewayInstrumentQueryError, match="INSTRUMENT_SESSION_BUSY"):
            reader.begin(KEY)
    finally:
        unlock.set()
        worker.join(2)
    assert not worker.is_alive()
    trader._spi.OnFrontDisconnected(1)
    with pytest.raises(CtpGatewaySessionError, match="INSTRUMENT_SESSION_RECONNECTING"):
        session.create_instrument_query_reader()
    with pytest.raises(CtpGatewayInstrumentQueryError, match="INSTRUMENT_SESSION_RECONNECTING"):
        reader.begin(KEY)
    assert session.close()


def _open_session_for_instrument(prepared):
    create, _, _, feeds, natives, streams, _, fake_md = prepared
    session = create()
    session.connect(generation="g-instrument", timeout=1)
    return session, feeds[0], feeds[0].trader_client, natives[0], streams, fake_md


@pytest.mark.parametrize("change", ["flow_root", "account", "api", "spi"])
def test_instrument_factory_rejects_changed_integrity(prepared, monkeypatch, change):
    from bt_api_ctp.ctp.client import _TraderSpi
    from bt_api_ctp.gateway_session import CtpGatewaySessionError
    session, _, trader, native, _, _ = _open_session_for_instrument(prepared)
    with monkeypatch.context() as patches:
        if change == "flow_root":
            def bad_flow():
                raise ValueError("synthetic flow change")
            patches.setattr(session._flow, "verify_root", bad_flow)
        elif change == "account":
            patches.setattr(session, "_account_key", replace(ACCOUNT, account_id="other"))
        elif change == "api":
            patches.setattr(trader, "_api", object())
        else:
            patches.setattr(trader, "_spi", _TraderSpi(trader, native))
        with pytest.raises(CtpGatewaySessionError, match="SESSION_CHANGED"):
            session.create_instrument_query_reader()
    assert session.close()
