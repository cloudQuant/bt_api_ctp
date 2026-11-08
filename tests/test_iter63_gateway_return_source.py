"""Offline CTP Gateway return-source composition and callback fences."""
import threading
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.gateway_return_query import CtpGatewayReturnQueryError
from bt_api_ctp.registered_sim_session import capture_query_session
from test_iter62_settlement_readback_on_write import _login
from test_iter63_gateway_session import prepared as prepared


def row(**changes):
    fields = dict(BrokerID="9999", InvestorID="account", TradingDay="20261105",
                  InstrumentID="IF2612", OrderRef="1", FrontID=11, SessionID=12,
                  TradeID="T1", Volume=1)
    fields.update(changes)
    return SimpleNamespace(**fields)


def test_same_trader_captures_during_start_and_reader_matches_source(prepared, monkeypatch):
    create, _, _, feeds, natives, _, _, _ = prepared
    start = TraderClient.start
    def early(trader, **kwargs):
        start(trader, **kwargs)
        assert trader._gateway_return_buffer is not None
        trader._spi.OnRtnTrade(row())
    monkeypatch.setattr(TraderClient, "start", early)
    session = create()
    session.connect(generation="service", timeout=1)
    trader = feeds[0].trader_client
    assert len(natives) == 1 and session.direct_provider._trader() is trader
    reader = session.create_return_query_reader()
    record = session.return_buffer.offer().records[0]
    binding = capture_query_session(trader, environment="simnow")
    assert reader._trader is trader
    assert record.kind == "trade" and record.row["TradingDay"] == "20261105"
    assert record.reception_trading_day == binding.trading_day
    assert record.source_ref == binding.source_ref == reader._binding.source_ref
    assert record.source_tags == binding._source_tags == reader._binding._source_tags
    assert session.close() is True
    assert session.return_buffer.status().record_count == 1


def test_callback_rows_and_error_metadata(prepared):
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="service", timeout=1)
    spi = feeds[0].trader_client._spi
    native = row()
    error = SimpleNamespace(ErrorID=42, ErrorMsg="error")
    spi.OnRtnOrder(native)
    spi.OnRtnTrade(native)
    spi.OnRspOrderInsert(native, error, 123, True)
    spi.OnErrRtnOrderInsert(native, error)
    spi.OnRspOrderAction(native, error, 124, False)
    spi.OnErrRtnOrderAction(native, error)
    spi.OnRspOrderInsert(native, SimpleNamespace(ErrorID=0), 125, True)
    spi.OnRspOrderInsert(None, None, 126, True)
    records = session.return_buffer.offer().records
    assert [r.kind for r in records] == ["order", "trade", "insert_response", "insert_error", "action_response", "action_error"]
    assert [(r.request_id, r.is_last, r.error_code) for r in records[2:]] == [
        (123, True, 42), (None, None, 42), (124, False, 42), (None, None, 42)]
    assert records[4].row["FrontID"] == 11 and records[4].front_id == 7
    assert all(r.row["TradingDay"] == "20261105" for r in records)
    assert not session.return_buffer.status().gap
    assert session.close() is True


@pytest.mark.parametrize("change", ["old_spi", "same_spi", "getter", "invalid"])
def test_source_failures_leave_sticky_gap(prepared, change):
    create, _, _, feeds, natives, _, _, _ = prepared
    session = create()
    session.connect(generation="service", timeout=1)
    trader = feeds[0].trader_client
    spi = trader._spi
    if change == "old_spi":
        _login(trader, natives[0], session_id=20)
    elif change == "same_spi":
        original = trader._capture_gateway_return_locked
        def reenter(current_spi, kind, args, scope, current):
            trader._connection_generation += 1
            return original(current_spi, kind, args, scope, current)
        trader._capture_gateway_return_locked = reenter
    elif change == "getter":
        class Reenter:
            @property
            def InstrumentID(self):
                trader._connection_generation += 1
                return "IF2612"
        spi.OnRtnOrder(Reenter())
    if change != "getter":
        spi.OnRtnTrade(row(Volume=[] if change == "invalid" else 1))
    status = session.return_buffer.status()
    assert status.gap and status.gap_reason in {"source_failure", "invalid_record"}
    assert status.record_count == 0
    # The same buffer survives close, including its unresolved gap.
    assert session.close() is True
    assert session.return_buffer.status().gap


def test_reader_reconnect_and_owner_checks(prepared):
    create, _, _, feeds, natives, _, _, _ = prepared
    session = create()
    session.connect(generation="service", timeout=1)
    reader = session.create_return_query_reader()
    errors = []
    def outsider():
        for fn in (lambda: session.return_buffer, session.create_return_query_reader):
            try:
                fn()
            except Exception:
                errors.append(True)
    t = threading.Thread(target=outsider)
    t.start(); t.join(2)
    assert errors == [True, True]
    _login(feeds[0].trader_client, natives[0], session_id=20)
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.read(kind="orders", timeout=1)
    session.refresh_session()
    assert session.create_return_query_reader()._trader is feeds[0].trader_client
    assert session.close() is True


def test_offered_record_identity_and_relogin_fence(prepared):
    from dataclasses import replace
    from bt_api_ctp.gateway_session import CtpGatewaySessionError
    create, _, _, feeds, natives, _, _, _ = prepared
    session = create()
    session.connect(generation="service", timeout=1)
    trader = feeds[0].trader_client
    trader._spi.OnRtnTrade(row())
    batch = session.return_buffer.offer()
    record = batch.records[0]
    session.assert_return_record_current(record)
    with pytest.raises(CtpGatewaySessionError):
        session.assert_return_record_current(replace(record))
    _login(trader, natives[0], session_id=20)
    session.refresh_session()
    with pytest.raises(CtpGatewaySessionError):
        session.assert_return_record_current(record)
    session.return_buffer.ack(batch)
    with pytest.raises(CtpGatewaySessionError):
        session.assert_return_record_current(record)
    assert session.close() is True


def test_actual_provider_order_struct_is_captured(prepared):
    from bt_api_ctp.ctp.ctp_structs_order import CThostFtdcOrderField
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="service", timeout=1)
    native = CThostFtdcOrderField()
    native.BrokerID = "9999"
    native.InvestorID = "account"
    native.TradingDay = "20261105"
    native.OrderRef = "1"
    feeds[0].trader_client._spi.OnRtnOrder(native)
    records = session.return_buffer.offer().records
    assert len(records) == 1 and records[0].row["TradingDay"] == "20261105"
    assert session.return_buffer.status().gap is False
    assert session.close() is True


def test_failed_start_keeps_early_unacked_record(prepared, monkeypatch):
    create, _, _, _, _, _, _, _ = prepared
    start = TraderClient.start
    def fail_after_return(trader, **kwargs):
        start(trader, **kwargs)
        trader._spi.OnRtnTrade(row())
        raise RuntimeError("synthetic failure after callback")
    monkeypatch.setattr(TraderClient, "start", fail_after_return)
    session = create()
    with pytest.raises(Exception):
        session.connect(generation="service", timeout=1)
    assert session.return_buffer.status().record_count == 1
    # The synthetic interrupted startup has no native Release receipt.
    assert session.close() is False
    assert session.return_buffer.status().record_count == 1


@pytest.mark.parametrize("callback", ["OnErrRtnOrderInsert", "OnErrRtnOrderAction"])
@pytest.mark.parametrize("info", [None, SimpleNamespace(ErrorID=0)])
def test_error_return_without_nonzero_error_is_gap(prepared, callback, info):
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="service", timeout=1)
    getattr(feeds[0].trader_client._spi, callback)(row(), info)
    status = session.return_buffer.status()
    assert status.gap_reason == "source_failure" and status.record_count == 0
    assert session.close() is True


@pytest.mark.parametrize("callback", ["OnRspOrderInsert", "OnRspOrderAction"])
@pytest.mark.parametrize("info", [None, SimpleNamespace(ErrorID=0)])
def test_successful_response_with_no_error_does_not_create_gap(prepared, callback, info):
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="service", timeout=1)
    getattr(feeds[0].trader_client._spi, callback)(None, info, 17, True)
    status = session.return_buffer.status()
    assert not status.gap and status.record_count == 0
    assert session.close() is True


@pytest.mark.parametrize("callback,kind,args,kwargs", [
    ("OnRtnOrder", "order", (), {"pOrder": row()}),
    ("OnRtnTrade", "trade", (), {"pTrade": row()}),
    ("OnRspOrderInsert", "insert_response", (row(),),
     {"pRspInfo": SimpleNamespace(ErrorID=9), "nRequestID": 101, "bIsLast": True}),
    ("OnErrRtnOrderInsert", "insert_error", (row(),),
     {"pRspInfo": SimpleNamespace(ErrorID=9)}),
    ("OnRspOrderAction", "action_response", (),
     {"pInputOrderAction": row(), "pRspInfo": SimpleNamespace(ErrorID=9),
      "nRequestID": 102, "bIsLast": False}),
    ("OnErrRtnOrderAction", "action_error", (),
     {"pOrderAction": row(), "pRspInfo": SimpleNamespace(ErrorID=9)}),
])
def test_capture_accepts_native_keyword_and_mixed_arguments(prepared, callback, kind, args, kwargs):
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="service", timeout=1)
    getattr(feeds[0].trader_client._spi, callback)(*args, **kwargs)
    records = session.return_buffer.offer().records
    assert len(records) == 1 and records[0].kind == kind
    assert records[0].row["InstrumentID"] == "IF2612"
    if kind.endswith("response"):
        assert records[0].request_id == kwargs["nRequestID"]
        assert records[0].is_last is kwargs["bIsLast"]
    assert not session.return_buffer.status().gap
    assert session.close() is True


@pytest.mark.parametrize("args,kwargs", [((row(),), {"pOrder": row()}), ((), {})])
def test_invalid_callback_arguments_mark_gap_and_retain_python_error(prepared, args, kwargs):
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="service", timeout=1)
    with pytest.raises(TypeError):
        feeds[0].trader_client._spi.OnRtnOrder(*args, **kwargs)
    assert session.return_buffer.status().gap_reason == "source_failure"
    assert session.return_buffer.status().record_count == 0
    assert session.close() is True


def test_action_capture_binds_sealed_login_and_rejects_login_replacement(prepared):
    from dataclasses import replace
    from bt_api_ctp.gateway_session import CtpGatewaySessionError
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="service", timeout=1)
    trader = feeds[0].trader_client
    trader._spi.OnRspOrderAction(row(), SimpleNamespace(ErrorID=42), 124, False)
    record = session.return_buffer.offer().records[0]
    assert record.login_request_id == trader._current_login_identity_locked().request_id
    session.assert_return_record_current(record)
    trader._login_identity_observation = replace(
        trader._login_identity_observation,
        request_id=trader._login_identity_observation.request_id + 1,
    )
    with pytest.raises(CtpGatewaySessionError, match="RETURN_SOURCE_UNTRUSTED"):
        session.assert_return_record_current(record)
    assert session.close() is True


def test_action_capture_rejects_login_mutation_during_native_field_read(prepared):
    from dataclasses import replace
    create, _, _, feeds, _, _, _, _ = prepared
    session = create()
    session.connect(generation="service", timeout=1)
    trader = feeds[0].trader_client
    class ChangedLogin:
        @property
        def BrokerID(self):
            login = trader._login_identity_observation
            trader._login_identity_observation = replace(login, request_id=login.request_id + 1)
            return "9999"
        InvestorID = "account"
    trader._spi.OnRspOrderAction(ChangedLogin(), SimpleNamespace(ErrorID=42), 124, True)
    assert session.return_buffer.status().gap
    assert session.return_buffer.status().record_count == 0
    assert session.close() is True
