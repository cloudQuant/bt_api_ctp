"""Local fake-native tests for the account-owner's sealed CTP return reads."""

from dataclasses import replace
import threading

import pytest
from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.query import _attach_query_source, _query_records_digest
import bt_api_ctp.gateway_return_query as reader_module
from bt_api_ctp.gateway_return_query import CtpGatewayReturnQueryReader, CtpGatewayReturnQueryError
from test_iter62_settlement_readback_on_write import _NativeApi, _login, TD_FRONT, TODAY


class FakeNative(_NativeApi):
    def __init__(self):
        super().__init__()
        self.rows = {"orders": [], "trades": []}
        self.request_count = 0
        self.requests = []

    def _answer(self, kind, request_id):
        self.request_count += 1
        self.requests.append((kind, request_id))
        for row in self.rows[kind]:
            self.client._handle_query_callback(kind, row, None, request_id, False)
        self.client._handle_query_callback(kind, None, None, request_id, True)
        return 0

    def ReqQryOrder(self, field, request_id):
        return self._answer("orders", request_id)

    def ReqQryTrade(self, field, request_id):
        return self._answer("trades", request_id)


def _row(**changes):
    values = dict(
        BrokerID="9999",
        InvestorID="account",
        TradingDay=TODAY,
        ExchangeID="SHFE",
        InstrumentID="rb2701",
        Price=3000.5,
        Volume=2,
        TradeID="native-trade",
        OrderSysID="native-order",
    )
    values.update(changes)
    return values


def _setup():
    native = FakeNative()
    client = TraderClient(
        TD_FRONT, "9999", "account", "synthetic-only", auto_settlement_confirm=False
    )
    native.client = client
    client._api = native
    _login(client, native, session_id=19)
    reader = CtpGatewayReturnQueryReader(
        client, environment="simnow", broker_id="9999", investor_id="account"
    )
    return reader, client, native


@pytest.mark.parametrize("kind", ["orders", "trades"])
def test_real_trader_query_preserves_immutable_native_rows_and_empty_terminal(kind):
    reader, client, native = _setup()
    native.rows[kind] = [_row(InstrumentID="unknown-but-preserved")]
    batch = reader.read(kind=kind, timeout=1)
    assert batch.kind == kind and len(batch.rows) == 1
    assert batch.rows[0]["InstrumentID"] == "unknown-but-preserved"
    assert batch.rows[0]["Price"] == 3000.5 and batch.rows[0]["Volume"] == 2
    assert type(batch.rows[0]["Price"]) is float and type(batch.rows[0]["Volume"]) is int
    assert batch.trading_day == TODAY and batch.request_id > 0 and batch.records_sha256
    with pytest.raises(TypeError):
        batch.rows[0]["Volume"] = 3
    with pytest.raises(Exception):
        batch.kind = "other"
    native.rows[kind] = []
    empty = reader.read(kind=kind, timeout=1)
    assert empty.rows == () and native.request_count == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("BrokerID", "other"),
        ("InvestorID", "other"),
        ("TradingDay", "20261107"),
        ("TradingDay", ""),
    ],
)
def test_foreign_row_account_or_day_is_rejected(field, value):
    reader, _, native = _setup()
    native.rows["trades"] = [_row(**{field: value})]
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.read(kind="trades", timeout=1)
    assert native.request_count == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), [], {"x": 1}, object(), True, None])
def test_noncanonical_raw_values_are_rejected(value):
    reader, _, native = _setup()
    native.rows["trades"] = [_row(Price=value)]
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.read(kind="trades", timeout=1)
    assert native.request_count == 1


@pytest.mark.parametrize(
    "fault",
    [
        "filtered",
        "missing_seal",
        "wrong_generation",
        "wrong_day",
        "wrong_source_tags",
        "nonexact_source_generation",
        "wrong_record_limit",
        "bad_digest",
        "late",
        "incomplete",
        "timeout",
        "error",
        "submit",
        "no_terminal",
        "wrong_request_id",
        "parameters",
        "expiry",
        "too_many",
    ],
)
def test_untrusted_query_evidence_is_rejected(monkeypatch, fault):
    reader, client, native = _setup()
    native.rows["trades"] = [_row()]
    original = client.query_trades_result

    def tampered(**kwargs):
        result = original(**kwargs)
        source = result.query_source
        if fault == "filtered":
            filters = dict(source.request_filters)
            filters["InstrumentID"] = "rb2701"
            source = replace(
                source,
                request_filters=tuple(sorted(filters.items())),
                request_intent_filters=tuple(sorted(filters.items())),
            )
        elif fault == "missing_seal":
            source = replace(source, _seal=object())
        elif fault == "wrong_generation":
            source = replace(source, connection_generation=source.connection_generation + 1)
        elif fault == "wrong_day":
            source = replace(source, trading_day="20261107")
        elif fault == "wrong_source_tags":
            source = replace(source, source_tags=("x",) * 4 + (1, 1))
        elif fault == "nonexact_source_generation":

            class IntLike(int):
                pass

            tags = source.source_tags
            source = replace(source, source_tags=(*tags[:4], IntLike(tags[4]), tags[5]))
        elif fault == "wrong_record_limit":
            source = replace(source, record_limit=100000)
        elif fault == "bad_digest":
            result.records[0]["Volume"] = 3
        elif fault == "late":
            result = replace(result, late_callback_count=1)
        elif fault == "incomplete":
            result = replace(result, complete=False)
        elif fault == "timeout":
            result = replace(result, timed_out=True)
        elif fault == "error":
            result = replace(result, error_code=5)
        elif fault == "submit":
            result = replace(result, submit_code=-1)
        elif fault == "no_terminal":
            result = replace(result, is_last_seen=False)
        elif fault == "wrong_request_id":
            source = replace(source, request_id=source.request_id + 1)
        elif fault == "parameters":
            source = replace(source, request_parameters=(("Other", 1.0),))
        elif fault == "expiry":
            source = replace(source, trusted_expires_monotonic=source.completed_monotonic)
        elif fault == "too_many":
            rows = tuple(_row() for _ in range(8193))
            result = replace(result, records=rows)
            source = replace(source, records_sha256=_query_records_digest(rows))
        if fault == "missing_seal":
            object.__setattr__(result, "_source", source)
            return result
        return _attach_query_source(result, source)

    monkeypatch.setattr(client, "query_trades_result", tampered)
    with pytest.raises(CtpGatewayReturnQueryError) as caught:
        reader.read(kind="trades", timeout=1)
    if fault == "too_many":
        assert caught.value.code == "ROW_LIMIT_EXCEEDED"
    assert native.request_count == 1


def test_thread_scope_invalid_input_and_relogin_are_rejected():
    reader, client, native = _setup()
    errors = []

    def wrong_thread():
        try:
            reader.read(kind="orders", timeout=1)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=wrong_thread)
    thread.start()
    thread.join(2)
    assert (
        not thread.is_alive()
        and len(errors) == 1
        and isinstance(errors[0], CtpGatewayReturnQueryError)
    )
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.read(kind="unknown", timeout=1)
    _login(client, native, session_id=20)
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.read(kind="orders", timeout=1)
    assert native.request_count == 0


def test_ticket_is_exact_latest_and_failed_read_revokes_it():
    reader, _, native = _setup()
    assert (reader.environment, reader.broker_id, reader.investor_id) == (
        "simnow",
        "9999",
        "account",
    )
    native.rows["orders"] = [_row()]
    first = reader.read(kind="orders", timeout=1)
    assert first.connection_generation > 0
    reader.assert_current(first)
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.assert_current(replace(first))
    assert native.request_count == 1  # assert_current is local and repeatable.
    second = reader.read(kind="orders", timeout=1)
    reader.assert_current(second)
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.assert_current(first)
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.read(kind="bad", timeout=1)
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.assert_current(second)
    assert native.request_count == 2


def test_ticket_uses_immutable_rows_without_rehashing_or_retaining_result(monkeypatch):
    reader, client, native = _setup()
    native.rows["trades"] = [_row()]
    results = []
    original = client.query_trades_result

    def capture(**kwargs):
        result = original(**kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(client, "query_trades_result", capture)
    batch = reader.read(kind="trades", timeout=1)
    results[0].records[0]["Volume"] = 99
    assert batch.rows[0]["Volume"] == 2

    def forbid_hash(_rows):
        raise AssertionError("assert_current must not rehash rows")

    monkeypatch.setattr(reader_module, "_query_records_digest", forbid_hash)
    reader.assert_current(batch)
    reader.assert_current(batch)
    assert not hasattr(reader, "_ticket_result")
    monkeypatch.undo()
    batch = reader.read(kind="trades", timeout=1)
    _login(client, native, session_id=20)
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.assert_current(batch)
    assert native.request_count == 2


def test_failed_wrong_thread_read_does_not_mutate_owner_ticket():
    reader, _, native = _setup()
    native.rows["orders"] = [_row()]
    batch = reader.read(kind="orders", timeout=1)
    errors = []

    def wrong_thread():
        try:
            reader.read(kind="orders", timeout=1)
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=wrong_thread)
    worker.start()
    worker.join(2)
    assert len(errors) == 1 and isinstance(errors[0], CtpGatewayReturnQueryError)
    reader.assert_current(batch)
    assert native.request_count == 1


def test_ticket_expiry_is_checked_without_a_query(monkeypatch):
    from datetime import datetime, timedelta

    reader, _, native = _setup()
    native.rows["orders"] = [_row()]
    batch = reader.read(kind="orders", timeout=1)

    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return batch.expires_at_utc + timedelta(seconds=1)

    monkeypatch.setattr(reader_module, "datetime", Future)
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.assert_current(batch)
    assert native.request_count == 1


@pytest.mark.parametrize("clock", ["wall", "monotonic"])
def test_read_rejects_expiry_during_freeze_and_revokes_both_tickets(monkeypatch, clock):
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    import bt_api_ctp.registered_sim_session as session_module

    reader, _, native = _setup()
    native.rows["orders"] = [_row()]
    old = reader.read(kind="orders", timeout=1)
    original_freeze = reader_module._freeze_rows
    original_assert = reader.assert_current
    original_reader_time = reader_module.time
    original_session_time = session_module.time
    new = []

    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return old.expires_at_utc + timedelta(minutes=1)

    def freeze_after_expiry(rows, binding):
        frozen = original_freeze(rows, binding)
        if clock == "wall":
            monkeypatch.setattr(reader_module, "datetime", Future)
        else:
            future_time = SimpleNamespace(monotonic=lambda: old.expires_monotonic + 60)
            monkeypatch.setattr(reader_module, "time", future_time)
            monkeypatch.setattr(session_module, "time", future_time)
        return frozen

    def capture_assert(batch):
        new.append(batch)
        return original_assert(batch)

    monkeypatch.setattr(reader_module, "_freeze_rows", freeze_after_expiry)
    monkeypatch.setattr(reader, "assert_current", capture_assert)
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.read(kind="orders", timeout=1)
    assert len(new) == 1
    monkeypatch.setattr(reader_module, "datetime", datetime)
    monkeypatch.setattr(reader_module, "time", original_reader_time)
    monkeypatch.setattr(session_module, "time", original_session_time)
    for batch in (old, new[0]):
        with pytest.raises(CtpGatewayReturnQueryError):
            original_assert(batch)
    assert native.request_count == 2


def test_native_session_change_after_query_cannot_issue_batch(monkeypatch):
    reader, client, native = _setup()
    native.rows["trades"] = [_row()]
    original = client.query_trades_result

    def change_after_query(**kwargs):
        result = original(**kwargs)
        _login(client, native, session_id=20)
        return result

    monkeypatch.setattr(client, "query_trades_result", change_after_query)
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.read(kind="trades", timeout=1)
    assert native.request_count == 1


@pytest.mark.parametrize("name", ["BrokerID", "InvestorID", "TradingDay"])
def test_missing_native_scope_field_is_rejected(name):
    reader, _, native = _setup()
    row = _row()
    del row[name]
    native.rows["orders"] = [row]
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.read(kind="orders", timeout=1)
    assert native.request_count == 1


def test_constructor_requires_exact_trader_and_expected_account():
    reader, client, native = _setup()
    with pytest.raises(CtpGatewayReturnQueryError):
        CtpGatewayReturnQueryReader(
            object(), environment="simnow", broker_id="9999", investor_id="account"
        )
    with pytest.raises(CtpGatewayReturnQueryError):
        CtpGatewayReturnQueryReader(
            client, environment="simnow", broker_id="9999", investor_id="other"
        )
    assert native.request_count == 0


def test_constructor_rejects_unbounded_native_source_tags():
    _, client, native = _setup()
    client._spi._native_spi_source_id = "x" * 257
    with pytest.raises(CtpGatewayReturnQueryError):
        CtpGatewayReturnQueryReader(
            client, environment="simnow", broker_id="9999", investor_id="account"
        )
    assert native.request_count == 0


@pytest.mark.parametrize("kind", ["orders", "trades"])
def test_native_row_limit_rejects_8193_and_late_terminal_releases_slot(kind):
    reader, client, native = _setup()
    native.rows[kind] = [_row()] * 8193
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.read(kind=kind, timeout=3)
    assert native.request_count == 1
    result = client.get_query_result(native.requests[0][1])
    assert result.record_limit == 8192 and result.record_limit_exceeded is True
    assert len(result.records) == 8192 and result.complete is False
    assert result.late_callback_count >= 1
    assert client._query_native_inflight is None
    assert native.request_count == 1  # no automatic follow-up query
    native.rows[kind] = []
    assert reader.read(kind=kind, timeout=1).rows == ()  # explicitly requested next query
    assert native.request_count == 2


@pytest.mark.parametrize("kind", ["orders", "trades"])
def test_exact_native_limit_with_terminal_remains_complete(kind):
    reader, _, native = _setup()
    native.rows[kind] = [_row()] * 8192
    batch = reader.read(kind=kind, timeout=3)
    assert len(batch.rows) == 8192 and batch._source.record_limit == 8192
    reader.assert_current(batch)


@pytest.mark.parametrize("kind", ["orders", "trades"])
def test_real_typed_ctp_return_fields_survive_sealed_query(kind):
    from bt_api_ctp.ctp.ctp_structs_order import CThostFtdcOrderField
    from bt_api_ctp.ctp.ctp_structs_trade import CThostFtdcTradeField

    reader, _, native = _setup()
    row = CThostFtdcOrderField() if kind == "orders" else CThostFtdcTradeField()
    for name, value in {
        "BrokerID": "9999",
        "InvestorID": "account",
        "TradingDay": TODAY,
        "ExchangeID": "SHFE",
        "InstrumentID": "rb2701",
    }.items():
        setattr(row, name, value)
    native.rows[kind] = [row]
    batch = reader.read(kind=kind, timeout=1)
    assert batch.rows[0]["BrokerID"] == "9999" and batch.rows[0]["TradingDay"] == TODAY
    reader.assert_current(batch)


def test_assert_session_current_does_not_query_or_change_ticket():
    reader, _, native = _setup()
    reader.assert_session_current()
    assert native.request_count == 0 and reader._ticket is None
    batch = reader.read(kind="orders", timeout=1)
    reader.assert_session_current()
    assert native.request_count == 1 and reader._ticket is batch
    reader.assert_current(batch)


@pytest.mark.parametrize("change", ["api", "spi", "relogin", "source_tags"])
def test_assert_session_current_rejects_changed_source(change):
    from bt_api_ctp.ctp.client import _TraderSpi
    reader, client, native = _setup()
    if change == "api":
        client._api = object()
    elif change == "spi":
        client._spi = _TraderSpi(client, native)
    elif change == "relogin":
        _login(client, native, session_id=20)
    else:
        client._spi._native_spi_source_id = "replacement-source"
    with pytest.raises(CtpGatewayReturnQueryError, match="SESSION_CHANGED"):
        reader.assert_session_current()
    assert native.request_count == 0 and reader._ticket is None


def test_assert_session_current_rejects_nonowner_before_lock():
    reader, client, _ = _setup()
    errors = []
    def outsider():
        try:
            reader.assert_session_current()
        except CtpGatewayReturnQueryError as exc:
            errors.append(exc.code)
    with client._query_state_lock:
        worker = threading.Thread(target=outsider)
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
    assert errors == ["OWNER_REQUIRED"]


def test_assert_same_native_allows_same_api_spi_new_login(monkeypatch):
    import test_iter62_settlement_readback_on_write as helpers
    reader, client, native = _setup()
    original_spi = client._spi
    monkeypatch.setattr(helpers, "_TraderSpi", lambda _client, _native: original_spi)
    _login(client, native, session_id=20, trading_day="20261109")
    assert client._spi is original_spi and client._api is native
    reader.assert_same_native()
    with pytest.raises(CtpGatewayReturnQueryError, match="SESSION_CHANGED"):
        reader.assert_session_current()
    assert native.request_count == 0 and reader._ticket is None


@pytest.mark.parametrize("change", ["trader", "api", "spi", "spi_api", "session_api"])
def test_assert_same_native_rejects_changed_objects(change):
    from bt_api_ctp.ctp.client import _TraderSpi
    reader, client, native = _setup()
    if change == "trader":
        reader._trader = TraderClient(TD_FRONT, "9999", "account", "synthetic-only")
    elif change == "api":
        client._api = object()
    elif change == "spi":
        client._spi = _TraderSpi(client, native)
    elif change == "spi_api":
        client._spi._native_api = object()
    else:
        client._session_native_api = object()
    with pytest.raises(CtpGatewayReturnQueryError, match="SESSION_CHANGED"):
        reader.assert_same_native()
    assert native.request_count == 0 and reader._ticket is None


def test_assert_same_native_rejects_wrong_thread_and_pid_before_lock(monkeypatch):
    import bt_api_ctp.gateway_return_query as module
    reader, client, _ = _setup()
    errors = []
    def outsider():
        try:
            reader.assert_same_native()
        except CtpGatewayReturnQueryError as exc:
            errors.append(exc.code)
    with client._query_state_lock:
        worker = threading.Thread(target=outsider)
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
    assert errors == ["OWNER_REQUIRED"]
    monkeypatch.setattr(module.os, "getpid", lambda: reader._pid + 1)
    with pytest.raises(CtpGatewayReturnQueryError, match="OWNER_REQUIRED"):
        reader.assert_same_native()


@pytest.mark.parametrize("field", ["_front_id", "_session_id"])
def test_session_scope_rejects_login_id_change_without_generation(field):
    reader, client, _ = _setup()
    generation = client._connection_generation
    setattr(client, field, getattr(client, field) + 1)
    with pytest.raises(CtpGatewayReturnQueryError, match="SESSION_CHANGED"):
        reader.assert_session_current()
    assert client._connection_generation == generation


def test_reader_construction_rejects_reentrant_login_scope_change(monkeypatch):
    import bt_api_ctp.gateway_return_query as module
    _, client, _ = _setup()
    original = module.capture_query_session
    def change_scope(*args, **kwargs):
        binding = original(*args, **kwargs)
        client._session_id += 1
        return binding
    monkeypatch.setattr(module, "capture_query_session", change_scope)
    with pytest.raises(CtpGatewayReturnQueryError, match="SESSION_UNTRUSTED"):
        CtpGatewayReturnQueryReader(
            client, environment="simnow", broker_id="9999", investor_id="account"
        )


def test_session_current_rechecks_scope_after_source_capture(monkeypatch):
    import bt_api_ctp.gateway_return_query as module
    reader, client, _ = _setup()
    original = module.capture_query_session
    def change_scope(*args, **kwargs):
        binding = original(*args, **kwargs)
        client._front_id += 1
        return binding
    monkeypatch.setattr(module, "capture_query_session", change_scope)
    with pytest.raises(CtpGatewayReturnQueryError, match="SESSION_CHANGED"):
        reader.assert_session_current()


def test_rebind_same_native_accepts_new_login_and_preserves_old_ticket(monkeypatch):
    import test_iter62_settlement_readback_on_write as helpers
    reader, client, native = _setup()
    ticket = reader.read(kind="orders", timeout=1)
    original_spi = client._spi
    monkeypatch.setattr(helpers, "_TraderSpi", lambda _client, _native: original_spi)
    _login(client, native, session_id=20, trading_day="20261109")
    rebound = reader.rebind_same_native()
    assert rebound is not reader and rebound._trader is client
    assert rebound._api is native and rebound._spi is original_spi
    assert reader._ticket is ticket and rebound._ticket is None and native.request_count == 1
    rebound.assert_session_current()
    with pytest.raises(CtpGatewayReturnQueryError, match="SESSION_CHANGED"):
        reader.assert_session_current()


@pytest.mark.parametrize("change", ["api", "spi"])
@pytest.mark.parametrize("check_number", [1, 2])
def test_rebind_rejects_reentrant_replacement_between_check_and_construction(monkeypatch, change, check_number):
    from bt_api_ctp.ctp.client import _TraderSpi
    reader, client, native = _setup()
    original = reader._assert_same_native_locked
    checks = 0
    def replace_after_check():
        nonlocal checks
        original()
        checks += 1
        if checks == check_number:
            if change == "api":
                client._api = object()
            else:
                client._spi = _TraderSpi(client, native)
    monkeypatch.setattr(reader, "_assert_same_native_locked", replace_after_check)
    with pytest.raises(CtpGatewayReturnQueryError):
        reader.rebind_same_native()
    assert reader._ticket is None and native.request_count == 0


def test_rebind_rejects_wrong_owner_before_lock(monkeypatch):
    import bt_api_ctp.gateway_return_query as module
    reader, client, _ = _setup()
    errors = []
    def outsider():
        try:
            reader.rebind_same_native()
        except CtpGatewayReturnQueryError as exc:
            errors.append(exc.code)
    with client._query_state_lock:
        worker = threading.Thread(target=outsider)
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()
    assert errors == ["OWNER_REQUIRED"]
    monkeypatch.setattr(module.os, "getpid", lambda: reader._pid + 1)
    with pytest.raises(CtpGatewayReturnQueryError, match="OWNER_REQUIRED"):
        reader.rebind_same_native()
