from __future__ import annotations

import sys
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from bt_api_base.account_protocol import AccountKey
from bt_api_base.market_protocol import MarketInstrumentKey

from test_account_provider_execution_observations import HmacIssuer

from bt_api_ctp.account_provider import (
    CtpAccountSessionProviderError,
    CtpReadOnlyAccountSessionProvider,
)
from bt_api_ctp.ctp import client as ctp_client_module
from bt_api_ctp.ctp.client import (
    CtpNativeStartupAttempt,
    CtpNativeStartupStopReceipt,
    CtpNativeStopReceipt,
)
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
from bt_api_ctp.query import (
    QueryResult,
    _attach_query_source,
    _new_query_session_scope,
    _new_query_source,
    _query_records_digest,
)

_ACCOUNT_KEY = AccountKey(
    provider="ctp",
    venue="simnow",
    environment="simnow",
    account_id="investor-test",
    subaccount_id="broker-test",
)
_STARTED = datetime(2026, 10, 17, 1, 29, tzinfo=timezone.utc)
_COMPLETED = datetime(2026, 10, 17, 1, 30, tzinfo=timezone.utc)
_FINGERPRINT = "a" * 16
_SOURCE_TAGS = ("source", "epoch", "api", "spi", 1, 7)
_INSTRUMENT = MarketInstrumentKey("ctp", "CFFEX", "simnow", "future", "IF2612", "IF2612")


class _Trader:
    def __init__(self) -> None:
        self.stop_receipts = [
            CtpNativeStopReceipt(7, False, True, False, False, False),
            CtpNativeStopReceipt(7, True, True, True, False, False),
        ]
        self.stop_calls = 0

    def stop_and_wait(self, *, timeout: float) -> CtpNativeStopReceipt:
        assert timeout > 0
        self.stop_calls += 1
        return self.stop_receipts.pop(0)


class _Feed:
    def __init__(self) -> None:
        self._request_feed = None
        self._trader = None
        self._connected = False
        self.connect_calls = 0
        self.rows = {
            "account": (
                {
                    "AccountID": "investor-test",
                    "BrokerID": "broker-test",
                    "CurrencyID": "CNY",
                    "Balance": 1000.0,
                    "Available": 800.0,
                },
            ),
            "positions": (
                {
                    "BrokerID": "broker-test",
                    "InvestorID": "investor-test",
                    "InstrumentID": "IF2612",
                    "ExchangeID": "CFFEX",
                    "PosiDirection": "2",
                    "Position": 2,
                },
                {
                    "BrokerID": "broker-test",
                    "InvestorID": "investor-test",
                    "InstrumentID": "IF2612",
                    "ExchangeID": "CFFEX",
                    "PosiDirection": "2",
                    "Position": 1,
                },
            ),
            "orders": (
                {
                    "BrokerID": "broker-test",
                    "InvestorID": "investor-test",
                    "OrderStatus": "1",
                    "ContingentCondition": "1",
                    "Direction": "1",
                    "CombOffsetFlag": "1",
                    "VolumeTotalOriginal": 3,
                    "VolumeTraded": 1,
                    "InstrumentID": "IF2612",
                    "ExchangeID": "CFFEX",
                    "OrderSysID": "sys-order-1",
                    "OrderRef": "client-order-1",
                    "TradingDay": "20261017",
                    "FrontID": 3,
                    "SessionID": 4,
                    "OrderPriceType": "2",
                    "LimitPrice": 4001.0,
                    "InsertDate": "20261017",
                    "InsertTime": "09:30:00",
                },
            ),
        }
        self.mutate_scope_after_orders = False
        self.mutate_source_after_query = False

    def connect(self) -> None:
        self.connect_calls += 1
        if self._trader is None:
            self._trader = _Trader()
        self._connected = True

    def get_session_state(self) -> dict[str, object]:
        return {"read_only_ready": True, "auto_settlement_confirm": False}

    def get_query_session_scope(self):
        generation = 8 if self.mutate_scope_after_orders else 7
        return _new_query_session_scope(
            issuer=self._trader,
            account_fingerprint=_FINGERPRINT,
            connection_generation=generation,
            trading_day="20261017",
            broker_id="broker-test",
            investor_id="investor-test",
            read_only_ready=True,
            captured_at_utc=_COMPLETED,
            captured_monotonic=100.0,
            source_tags=_SOURCE_TAGS,
        )

    def query_account_result(self, *, timeout: float):
        return self._query("account", timeout)

    def query_positions_result(self, *, timeout: float):
        return self._query("positions", timeout)

    def query_orders_result(self, *, timeout: float):
        result = self._query("orders", timeout)
        if self.mutate_scope_after_orders:
            self.mutate_scope_after_orders = True
        return result

    def _query(self, query_name: str, timeout: float):
        assert timeout > 0
        rows = list(self.rows[query_name])
        result = QueryResult(
            request_type=query_name,
            request_id={"account": 1, "positions": 2, "orders": 3}[query_name],
            connection_generation=7,
            account_fingerprint=_FINGERPRINT,
            started_at_utc=_STARTED,
            completed_at_utc=_COMPLETED,
            is_last_seen=True,
            error_code=0,
            error_message="",
            timed_out=False,
            complete=True,
            records=tuple(rows),
        )
        filters = _full_account_filters(query_name)
        source = _new_query_source(
            issuer=self._trader,
            request_type=query_name,
            request_id=result.request_id,
            account_fingerprint=_FINGERPRINT,
            connection_generation=7,
            trading_day="20261017",
            broker_id="broker-test",
            investor_id="investor-test",
            request_intent_filters=filters,
            request_filters=filters,
            started_at_utc=_STARTED,
            completed_at_utc=_COMPLETED,
            started_monotonic=100.0,
            completed_monotonic=101.0,
            records_sha256=_query_records_digest(result.records),
        )
        result = _attach_query_source(result, source)
        if self.mutate_source_after_query:
            rows[0]["Balance"] = 999.0
        return result


def _full_account_filters(query_name: str) -> tuple[tuple[str, str], ...]:
    filters = {"BrokerID": "broker-test", "InvestorID": "investor-test"}
    if query_name == "account":
        filters.update(CurrencyID="", BizType="\x00", AccountID="")
    if query_name in ("positions", "orders"):
        filters.update(InstrumentID="", ExchangeID="", InvestUnitID="")
    if query_name == "orders":
        filters.update(OrderSysID="", InsertTimeStart="", InsertTimeEnd="")
    return tuple(sorted(filters.items()))


def _provider(feed: _Feed, **settings) -> CtpReadOnlyAccountSessionProvider:
    return CtpReadOnlyAccountSessionProvider(
        lambda: feed,
        monotonic_clock=lambda: 102.0,
        utc_clock=lambda: datetime(2026, 10, 17, 2, 0, tzinfo=timezone.utc),
        **settings,
    )


def test_ctp_account_provider_projects_complete_generation_bound_readonly_snapshot() -> None:
    feed = _Feed()
    # This legacy provider cannot project native active-order identifiers
    # without an SDK-issued observation binding.
    feed.rows["orders"] = ()
    provider = _provider(feed)
    assert provider.native_owner_created is False
    assert provider.native_connection_generation is None
    assert provider.last_native_stop_receipt is None

    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    assert provider.native_owner_created is True
    assert provider.native_connection_generation == 7
    assert provider.last_native_stop_receipt is None

    observation = provider.read_account(generation="service-gen-1", timeout=2.0)

    assert feed.connect_calls == 1
    assert observation.account_key is _ACCOUNT_KEY
    from bt_api_ctp.account_resource_coverage import CTP_ACCOUNT_RESOURCE_COVERAGE

    assert observation.coverage == CTP_ACCOUNT_RESOURCE_COVERAGE
    assert observation.consistency == "multi_query"
    assert observation.balances_complete is True
    assert observation.positions_complete is True
    assert observation.open_orders_complete is True
    assert observation.balances[0].to_dict() == {
        "resource": {"product_type": "future", "margin_pool": "futures"},
        "currency": "CNY",
        "total": "1000",
        "available": "800",
        "locked": None,
        "observed_at_utc": "2026-10-17T01:30:00.000000Z",
    }
    assert len(observation.positions) == 1
    position = observation.positions[0]
    assert (position.instrument.instrument_id, position.side, position.quantity) == (
        "IF2612",
        "long",
        "3",
    )
    assert observation.open_orders == ()
    original_trader = feed._trader
    complete_receipt = original_trader.stop_receipts[-1]
    assert provider.close() is False
    assert original_trader.stop_calls == 1
    assert provider.last_native_stop_receipt is None
    assert provider.close() is True
    assert original_trader.stop_calls == 2
    assert feed._trader is None
    assert provider.native_owner_created is True
    assert provider.native_connection_generation == 7
    assert provider.last_native_stop_receipt is complete_receipt
    assert provider.close() is True

    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-2", timeout=2.0)
    assert feed.connect_calls == 2
    assert feed._trader is not original_trader
    assert provider.native_owner_created is True
    assert provider.native_connection_generation == 7
    assert provider.last_native_stop_receipt is None
    assert provider.close() is False
    assert provider.close() is True


def test_ctp_account_provider_rejects_active_order_without_observation_binding() -> None:
    provider = _provider(_Feed())
    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    with pytest.raises(CtpAccountSessionProviderError, match="ORDER_OBSERVATION_IDENTITY_UNAVAILABLE"):
        provider.read_account(generation="service-gen-1", timeout=2.0)


def _connected_snapshot(feed, issuer, instruments=(_INSTRUMENT,), **settings):
    provider = _provider(feed, execution_observation_issuer=issuer,
                         execution_instruments=instruments, **settings)
    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    return provider


def test_active_order_uses_stable_opaque_id_without_strategy_attribution() -> None:
    issuer = HmacIssuer()
    feed = _Feed()
    provider = _connected_snapshot(feed, issuer)
    first = provider.read_account(generation="service-gen-1", timeout=2.0)
    again = provider.read_account(generation="service-gen-1", timeout=2.0)
    other = _connected_snapshot(_Feed(), issuer).read_account(
        generation="service-gen-1", timeout=2.0
    )
    order = first.open_orders[0]
    assert order.order_id == again.open_orders[0].order_id == other.open_orders[0].order_id
    assert order.order_id.startswith("sdk-order-observation-v1-")
    assert order.client_order_id is None
    assert (order.status, order.quantity, order.filled_quantity) == ("partially_filled", "3", "1")
    assert order.resource.product_type == order.instrument.product_type == "future"
    public = str(first.to_dict())
    for value in ("sys-order-1", "client-order-1", "FrontID", "SessionID", "OrderRef"):
        assert value not in public


@pytest.mark.parametrize("fault", ["day", "account", "broker", "catalog", "missing", "time", "duplicate", "option"])
def test_active_order_any_unprojectable_row_blocks_complete_snapshot(fault):
    feed = _Feed()
    row = feed.rows["orders"][0]
    instruments = (_INSTRUMENT,)
    if fault == "day": row["TradingDay"] = "20261018"
    elif fault == "account": row["InvestorID"] = "other"
    elif fault == "broker": row["BrokerID"] = "other"
    elif fault == "catalog": row["InstrumentID"] = "UNKNOWN"
    elif fault == "missing": del row["Direction"]
    elif fault == "time": row["InsertTime"] = "invalid"
    elif fault == "duplicate": feed.rows["orders"] = (row, dict(row))
    else:
        instruments = (replace(_INSTRUMENT, product_type="option"),)
    provider = _connected_snapshot(feed, HmacIssuer(), instruments)
    with pytest.raises(CtpAccountSessionProviderError):
        provider.read_account(generation="service-gen-1", timeout=2.0)


@pytest.mark.parametrize("status", ["0", "2", "4", "5"])
def test_terminal_order_is_not_reported_as_open(status):
    feed = _Feed()
    feed.rows["orders"][0]["OrderStatus"] = status
    feed.rows["orders"][0]["VolumeTraded"] = {"0": 3, "2": 1, "4": 0, "5": 1}[status]
    observation = _connected_snapshot(feed, HmacIssuer()).read_account(
        generation="service-gen-1", timeout=2.0
    )
    assert observation.open_orders_complete is True and observation.open_orders == ()


@pytest.mark.parametrize(("status", "filled"), [("0", 0), ("2", 0), ("2", 3), ("4", 1), ("5", 4)])
@pytest.mark.parametrize("configured", [True, False])
def test_invalid_terminal_order_status_quantity_blocks_snapshot(status, filled, configured):
    feed = _Feed()
    feed.rows["orders"][0].update(OrderStatus=status, VolumeTraded=filled)
    provider = _connected_snapshot(feed, HmacIssuer()) if configured else _provider(feed)
    if not configured:
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_ROW_INVALID"):
        provider.read_account(generation="service-gen-1", timeout=2.0)


@pytest.mark.parametrize("configured", [True, False])
def test_terminal_order_from_other_trading_day_blocks_snapshot(configured):
    feed = _Feed()
    feed.rows["orders"][0].update(OrderStatus="0", VolumeTraded=3, TradingDay="20261016")
    provider = _connected_snapshot(feed, HmacIssuer()) if configured else _provider(feed)
    if not configured:
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_ROW_INVALID"):
        provider.read_account(generation="service-gen-1", timeout=2.0)


@pytest.mark.parametrize("condition", [None, "2", "3", "4"])
def test_condition_orders_cannot_be_published_as_plain_orders(condition):
    feed = _Feed()
    row = feed.rows["orders"][0]
    if condition is None:
        del row["ContingentCondition"]
    else:
        row["ContingentCondition"] = condition
    provider = _connected_snapshot(feed, HmacIssuer())
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_ROW_INVALID"):
        provider.read_account(generation="service-gen-1", timeout=2.0)


@pytest.mark.parametrize("status", ["b", "c"])
def test_triggered_or_pending_condition_orders_are_not_reported_as_plain_orders(status):
    feed = _Feed()
    feed.rows["orders"][0]["OrderStatus"] = status
    provider = _connected_snapshot(feed, HmacIssuer())
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_ROW_INVALID"):
        provider.read_account(generation="service-gen-1", timeout=2.0)


def test_snapshot_never_invokes_managed_resolver():
    feed = _Feed()
    def fail(*args):
        raise AssertionError("managed resolver must not be called by account snapshot")
    provider = _connected_snapshot(feed, HmacIssuer(), execution_order_resolver=fail,
                                   execution_scope_ref="strategy-test")
    order = provider.read_account(generation="service-gen-1", timeout=2.0).open_orders[0]
    assert order.client_order_id is None


def test_explicit_account_snapshot_issuer_takes_priority_over_execution_issuer():
    feed = _Feed()
    snapshot_issuer = HmacIssuer(b"account-observation-test-key")
    reader = _provider(
        feed,
        execution_observation_issuer=HmacIssuer(b"event-observation-test-key"),
        execution_instruments=(_INSTRUMENT,),
        account_observation_issuer=snapshot_issuer,
        account_observation_instruments=(_INSTRUMENT,),
    )
    reader.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    combined = reader.read_account(generation="service-gen-1", timeout=2.0).open_orders[0].order_id
    separate = _provider(_Feed(), account_observation_issuer=snapshot_issuer,
                         account_observation_instruments=(_INSTRUMENT,))
    separate.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    assert combined == separate.read_account(generation="service-gen-1", timeout=2.0).open_orders[0].order_id
    assert reader._execution is not None and separate._execution is None
    assert separate.read_execution_observations is None


@pytest.mark.parametrize(("status", "filled"), [("1", 0), ("3", 1)])
def test_active_native_status_and_quantity_conflict_blocks_snapshot(status, filled):
    feed = _Feed()
    feed.rows["orders"][0].update(OrderStatus=status, VolumeTraded=filled)
    provider = _connected_snapshot(feed, HmacIssuer())
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_ROW_INVALID"):
        provider.read_account(generation="service-gen-1", timeout=2.0)


@pytest.mark.parametrize("query_name", ["account", "positions", "orders"])
@pytest.mark.parametrize("field", ["request_intent_filters", "request_filters"])
@pytest.mark.parametrize("fault", ["missing", "foreign_account", "filtered"])
def test_full_account_read_rejects_unproved_or_filtered_native_request(
    monkeypatch, query_name: str, field: str, fault: str
) -> None:
    feed = _Feed()
    feed.rows["orders"] = ()
    original = feed._query

    def query(name, timeout):
        result = original(name, timeout)
        if name != query_name:
            return result
        values = dict(getattr(result.query_source, field))
        if fault == "missing":
            values.pop("InvestorID")
        elif fault == "foreign_account":
            values["BrokerID"] = "different-broker"
        else:
            values["InstrumentID"] = "IF2612"
        source = replace(result.query_source, **{field: tuple(sorted(values.items()))})
        return _attach_query_source(result, source)

    monkeypatch.setattr(feed, "_query", query)
    provider = _provider(feed)
    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_SOURCE_UNTRUSTED"):
        provider.read_account(generation="service-gen-1", timeout=2.0)


@pytest.mark.parametrize("query_name", ["account", "positions", "orders"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_intent_parameters", (("Other", 1.0),)),
        ("request_parameters", (("Other", 1.0),)),
        ("explicit_request_filters", ("InvestorID",)),
    ],
)
def test_full_account_read_rejects_additional_request_scope(
    monkeypatch, query_name: str, field: str, value: object
) -> None:
    feed = _Feed()
    feed.rows["orders"] = ()
    original = feed._query

    def query(name, timeout):
        result = original(name, timeout)
        if name == query_name:
            result = _attach_query_source(result, replace(result.query_source, **{field: value}))
        return result

    monkeypatch.setattr(feed, "_query", query)
    provider = _provider(feed)
    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_SOURCE_UNTRUSTED"):
        provider.read_account(generation="service-gen-1", timeout=2.0)


@pytest.mark.parametrize(
    ("query_name", "native_field", "nonempty"),
    [
        ("account", "CurrencyID", "CNY"),
        ("account", "BizType", "1"),
        ("account", "AccountID", "other-account"),
        ("positions", "ExchangeID", "CFFEX"),
        ("positions", "InstrumentID", "IF2612"),
        ("positions", "InvestUnitID", "unit"),
        ("orders", "ExchangeID", "CFFEX"),
        ("orders", "InstrumentID", "IF2612"),
        ("orders", "OrderSysID", "order"),
        ("orders", "InsertTimeStart", "09:00:00"),
        ("orders", "InsertTimeEnd", "10:00:00"),
        ("orders", "InvestUnitID", "unit"),
    ],
)
@pytest.mark.parametrize("fault", ["missing", "filtered"])
def test_full_account_read_checks_every_native_scope_field(
    monkeypatch, query_name: str, native_field: str, nonempty: str, fault: str
) -> None:
    feed = _Feed()
    feed.rows["orders"] = ()
    original = feed._query

    def query(name, timeout):
        result = original(name, timeout)
        if name == query_name:
            filters = dict(result.query_source.request_filters)
            if fault == "missing":
                del filters[native_field]
            else:
                filters[native_field] = nonempty
            values = tuple(sorted(filters.items()))
            result = _attach_query_source(result, replace(
                result.query_source, request_filters=values, request_intent_filters=values
            ))
        return result

    monkeypatch.setattr(feed, "_query", query)
    provider = _provider(feed)
    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_SOURCE_UNTRUSTED"):
        provider.read_account(generation="service-gen-1", timeout=2.0)


@pytest.mark.parametrize("query_name", ["positions", "orders"])
@pytest.mark.parametrize("field", ["BrokerID", "InvestorID"])
@pytest.mark.parametrize("fault", ["missing", "foreign"])
def test_full_account_read_rejects_foreign_or_unidentified_native_rows(
    query_name: str, field: str, fault: str
) -> None:
    feed = _Feed()
    # Terminal orders are otherwise ignored, but still require account identity.
    feed.rows["orders"][0]["OrderStatus"] = "5"
    if fault == "missing":
        del feed.rows[query_name][0][field]
    else:
        feed.rows[query_name][0][field] = "other"
    provider = _provider(feed)
    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_ROW_INVALID"):
        provider.read_account(generation="service-gen-1", timeout=2.0)


def test_full_account_read_accepts_empty_complete_position_and_order_results() -> None:
    feed = _Feed()
    feed.rows["positions"] = feed.rows["orders"] = ()
    provider = _provider(feed)
    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    observation = provider.read_account(generation="service-gen-1", timeout=2.0)
    assert observation.positions_complete is True and observation.positions == ()
    assert observation.open_orders_complete is True and observation.open_orders == ()


@pytest.mark.parametrize(
    ("family", "native_family"),
    [("account", "account"), ("positions", "positions"),
     ("orders", "orders"), ("open_orders", "orders")],
)
def test_query_service_uses_exact_complete_native_scope(family, native_family) -> None:
    from bt_api_ctp.query_service import CtpProviderQueryService, _native_filter_map_matches

    service = object.__new__(CtpProviderQueryService)
    service._session = SimpleNamespace(
        broker_id="broker-test", investor_id="investor-test", environment="simnow",
        trading_day="20261017",
    )
    service._observation_route_ready = True
    request = SimpleNamespace(family=family, filters={}, require_complete_scope=True)
    prepared = service._prepare(request)
    expected = dict(_full_account_filters(native_family))
    assert prepared.blocker is None and prepared.native_filters == expected
    assert _native_filter_map_matches(tuple(sorted(expected.items())), expected)
    altered = dict(expected)
    altered.pop("InvestUnitID" if native_family != "account" else "BizType")
    assert not _native_filter_map_matches(tuple(sorted(altered.items())), expected)


@pytest.mark.parametrize("condition", ["1", "2", None])
def test_query_service_open_order_projection_never_hides_unsupported_conditions(condition):
    from bt_api_base.query_protocol import QueryCompleteness, QueryRequest
    from bt_api_ctp.query_service import CtpProviderQueryService

    feed = _Feed()
    if condition is None:
        del feed.rows["orders"][0]["ContingentCondition"]
    else:
        feed.rows["orders"][0]["ContingentCondition"] = condition
    reader = _connected_snapshot(feed, HmacIssuer())
    projector = reader._execution_projector(reader._session_scope, _ACCOUNT_KEY,
                                             managed_correlation=False)
    service = object.__new__(CtpProviderQueryService)
    service._session = projector._session
    service._execution_query_projector = projector
    result = feed.query_orders_result(timeout=1)
    request = QueryRequest("request-test", "open_orders")
    snapshot, _ = service._project_snapshot(
        request, result, ((result, dict(_full_account_filters("orders"))),),
        {("CFFEX", "IF2612"): "future"},
    )
    if condition == "1":
        assert snapshot.completeness is QueryCompleteness.COMPLETE
        assert snapshot.scope_complete is True and len(snapshot.records) == 1
    else:
        assert snapshot.completeness is QueryCompleteness.PARTIAL
        assert snapshot.scope_complete is False and snapshot.blockers


@pytest.mark.parametrize("family", ["positions", "orders"])
def test_reconciliation_accepts_full_scope_and_rejects_missing_filter(family) -> None:
    import time
    from bt_api_ctp.reconciliation_authority import CtpReconciliationAuthority

    issuer = object()
    current_time = datetime.now(timezone.utc)
    monotonic = time.monotonic()
    binding = SimpleNamespace(
        _issuer=issuer, _source_tags=None, _captured_monotonic=monotonic - 1,
        connection_generation=7, account_fingerprint=_FINGERPRINT,
        trading_day="20261017", broker_id="broker-test", investor_id="investor-test",
    )
    result = QueryResult(
        request_type=family, request_id=1, connection_generation=7,
        account_fingerprint=_FINGERPRINT, started_at_utc=current_time,
        completed_at_utc=current_time, is_last_seen=True, error_code=0,
        error_message="", timed_out=False, complete=True, records=(), submit_code=0,
        first_completion_origin="success", completion_origin="success",
    )
    filters = _full_account_filters(family)
    source = _new_query_source(
        issuer=issuer, request_type=family, request_id=1,
        account_fingerprint=_FINGERPRINT, connection_generation=7,
        trading_day="20261017", broker_id="broker-test", investor_id="investor-test",
        started_at_utc=current_time, completed_at_utc=current_time,
        started_monotonic=monotonic, completed_monotonic=monotonic,
        records_sha256=_query_records_digest(()), request_filters=filters,
        request_intent_filters=filters,
    )
    authority = object.__new__(CtpReconciliationAuthority)
    authority._binding = binding
    assert authority._validate_query_result(_attach_query_source(result, source), family)
    narrowed = tuple(pair for pair in filters if pair[0] != "InvestUnitID")
    altered = replace(source, request_filters=narrowed, request_intent_filters=narrowed)
    assert not authority._validate_query_result(_attach_query_source(result, altered), family)


def test_ctp_account_provider_keeps_interrupted_startup_uncertain_until_receipt() -> None:
    class InterruptedFeed(_Feed):
        def connect(self) -> None:
            self.connect_calls += 1
            self._trader = _Trader()
            raise KeyboardInterrupt

    feed = InterruptedFeed()
    provider = _provider(feed)
    with pytest.raises(KeyboardInterrupt):
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)

    with pytest.raises(CtpAccountSessionProviderError, match="NATIVE_CUSTODY_UNCERTAIN"):
        _ = provider.native_owner_created
    with pytest.raises(CtpAccountSessionProviderError, match="NATIVE_CUSTODY_UNCERTAIN"):
        _ = provider.native_connection_generation
    assert provider.last_native_stop_receipt is None

    complete_receipt = feed._trader.stop_receipts[-1]
    assert provider.close() is False
    assert provider.last_native_stop_receipt is None
    assert provider.close() is True
    assert provider.native_owner_created is True
    assert provider.native_connection_generation == 7
    assert provider.last_native_stop_receipt is complete_receipt


def test_production_feed_rejects_completed_preconnection_generation_zero_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_package = Path(__file__).resolve().parents[1] / "src" / "bt_api_ctp"
    assert Path(ctp_client_module.__file__).resolve() == source_package / "ctp" / "client.py"
    assert (
        Path(sys.modules[CtpReadOnlyAccountSessionProvider.__module__].__file__).resolve()
        == source_package / "account_provider.py"
    )
    assert (
        Path(sys.modules[CtpRequestDataFuture.__module__].__file__).resolve()
        == source_package / "feeds" / "live_ctp_feed.py"
    )

    class ReleaseGateApi:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []
            self.release_entered = threading.Event()
            self.allow_release = threading.Event()
            self.released = threading.Event()

        def RegisterSpi(self, spi: object | None) -> None:
            self.calls.append(("register_spi", spi))

        def SubscribePrivateTopic(self, mode: int) -> None:
            self.calls.append(("subscribe_private", mode))

        def SubscribePublicTopic(self, mode: int) -> None:
            self.calls.append(("subscribe_public", mode))

        def RegisterFront(self, front: str) -> None:
            self.calls.append(("register_front", front))

        def Init(self) -> None:
            self.calls.append(("init", None))

        def Join(self) -> int:
            # The vendor Join returns only after Release; a stop of a Join-
            # observed generation could never be proven (iteration 62).
            self.calls.append(("join", None))
            raise AssertionError("the production feed must start a no-Join trader")

        def Release(self) -> None:
            self.calls.append(("release", None))
            self.release_entered.set()
            if not self.allow_release.wait(2.0):
                raise AssertionError("synthetic Release was not allowed by the test")
            self.released.set()

    api = ReleaseGateApi()
    monkeypatch.setattr(ctp_client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(ctp_client_module, "_flow_dir", lambda _prefix: "synthetic-flow")
    monkeypatch.setattr(ctp_client_module, "_TraderSpi", lambda _client, _api: SimpleNamespace())
    monkeypatch.setattr(
        ctp_client_module,
        "CThostFtdcTraderApi",
        SimpleNamespace(CreateFtdcTraderApi=lambda _flow: api),
    )

    feed = CtpRequestDataFuture(
        broker_id="broker-test",
        user_id="investor-test",
        password="synthetic-only",
        td_front="tcp://127.0.0.1:19991",
        md_front="tcp://127.0.0.1:19992",
        auto_settlement_confirm=False,
    )
    feed._connect_timeout = 0.01
    provider = CtpReadOnlyAccountSessionProvider(
        lambda: feed,
        stop_timeout_seconds=0.05,
        monotonic_clock=lambda: 102.0,
        utc_clock=lambda: datetime(2026, 10, 17, 2, 0, tzinfo=timezone.utc),
    )

    try:
        with pytest.raises(CtpAccountSessionProviderError, match="SESSION_NOT_CONNECTED"):
            provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=1.0)

        trader = feed._trader
        assert trader is not None
        assert ("init", None) in api.calls
        assert trader._connection_generation == 0
        with pytest.raises(CtpAccountSessionProviderError, match="NATIVE_CUSTODY_UNCERTAIN"):
            _ = provider.native_owner_created

        # A bounded close while the no-Join Release is still running preserves
        # the exact feed/API.
        assert provider.close() is False
        assert api.release_entered.wait(1.0)
        assert not api.released.is_set()
        assert provider._request_feed is feed
        assert feed._trader is trader

        # Once Release returns, the provider confirms the exact generation-zero
        # startup stop without any Join.
        api.allow_release.set()
        assert api.released.wait(1.0)
        assert provider.close() is True
        receipt = provider.last_native_startup_stop_receipt
        assert type(receipt) is CtpNativeStartupStopReceipt
        assert receipt.outcome == "api_created_released"
        assert receipt.connection_generation == 0
        assert receipt.complete is True
        assert receipt.native_released is True
        assert receipt.join_required is False
        assert receipt.join_completed is True
        assert receipt.thread_alive is False
        assert receipt.timed_out is False
        assert api.calls.count(("release", None)) == 1
        assert ("join", None) not in api.calls
        assert provider.last_native_stop_receipt is None
        assert provider._request_feed is None
        assert feed._trader is None
    finally:
        api.allow_release.set()


def test_production_feed_preinit_start_failure_closes_with_released_startup_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrontFailureApi:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []
            self.release_thread_ids: list[int] = []

        def RegisterSpi(self, spi: object | None) -> None:
            self.calls.append(("register_spi", spi))

        def SubscribePrivateTopic(self, mode: int) -> None:
            self.calls.append(("subscribe_private", mode))

        def SubscribePublicTopic(self, mode: int) -> None:
            self.calls.append(("subscribe_public", mode))

        def RegisterFront(self, front: str) -> None:
            self.calls.append(("register_front", front))
            raise RuntimeError("synthetic RegisterFront failure")

        def Init(self) -> None:
            self.calls.append(("init", None))

        def Join(self) -> int:
            self.calls.append(("join", None))
            return 0

        def Release(self) -> None:
            self.calls.append(("release", None))
            self.release_thread_ids.append(threading.get_ident())

    api = FrontFailureApi()
    monkeypatch.setattr(ctp_client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(ctp_client_module, "_flow_dir", lambda _prefix: "synthetic-flow")
    monkeypatch.setattr(ctp_client_module, "_TraderSpi", lambda _client, _api: SimpleNamespace())
    monkeypatch.setattr(
        ctp_client_module,
        "CThostFtdcTraderApi",
        SimpleNamespace(CreateFtdcTraderApi=lambda _flow: api),
    )
    feed = CtpRequestDataFuture(
        broker_id="broker-test",
        user_id="investor-test",
        password="synthetic-only",
        td_front="tcp://127.0.0.1:19991",
        md_front="tcp://127.0.0.1:19992",
        auto_settlement_confirm=False,
    )
    provider = CtpReadOnlyAccountSessionProvider(
        lambda: feed,
        stop_timeout_seconds=0.05,
        monotonic_clock=lambda: 102.0,
        utc_clock=lambda: datetime(2026, 10, 17, 2, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(CtpAccountSessionProviderError, match="SESSION_NOT_CONNECTED"):
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=1.0)

    # The failed no-Join start never entered Init, so it released the exact
    # API on the connecting thread, after detaching the SPI, before raising.
    assert api.release_thread_ids == [threading.get_ident()]
    assert api.calls[-2:] == [("register_spi", None), ("release", None)]
    assert ("init", None) not in api.calls
    assert ("join", None) not in api.calls
    attempt = provider.native_startup_attempt
    assert type(attempt) is CtpNativeStartupAttempt
    assert attempt.phase == "api_created"
    with pytest.raises(CtpAccountSessionProviderError, match="NATIVE_CUSTODY_UNCERTAIN"):
        _ = provider.native_owner_created

    # The first close proves the generation-zero release.
    assert provider.close() is True
    receipt = provider.last_native_startup_stop_receipt
    assert type(receipt) is CtpNativeStartupStopReceipt
    assert receipt.outcome == "api_created_released"
    assert receipt.api_attempt_id == attempt.api_attempt_id
    assert receipt.complete is True
    assert receipt.native_released is True
    assert receipt.join_required is False
    assert receipt.timed_out is False
    assert provider.native_owner_created is True
    assert provider.last_native_stop_receipt is None
    assert feed._trader is None
    assert api.calls.count(("release", None)) == 1


def test_exact_ctp_feed_constructor_failure_confirms_native_free_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls = []
    start_calls = []

    class ConstructorFailure:
        def __init__(self, *_args, **_kwargs) -> None:
            constructor_calls.append(True)
            raise RuntimeError("synthetic TraderClient constructor failure")

        def start(self, *_args, **_kwargs) -> None:
            start_calls.append(True)

    monkeypatch.setattr(
        "bt_api_ctp.feeds.live_ctp_feed.ctp_client.TraderClient",
        ConstructorFailure,
    )
    feed = CtpRequestDataFuture(
        broker_id="broker-test",
        user_id="investor-test",
        password="synthetic-only",
        td_front="tcp://test-td",
        md_front="tcp://test-md",
        auto_settlement_confirm=False,
    )
    provider = _provider(feed)

    with pytest.raises(CtpAccountSessionProviderError, match="SESSION_NOT_CONNECTED"):
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)

    # The production feed assigns _trader only after TraderClient construction,
    # and calls start only after assignment. Its native API factory is in start.
    assert constructor_calls == [True]
    assert start_calls == []
    assert feed._trader is None
    assert provider.native_owner_created is False
    assert provider.native_connection_generation is None
    assert provider.close() is True
    assert provider.native_owner_created is False
    assert provider.native_connection_generation is None


def test_custom_feed_without_trader_remains_unknown_after_connect_failure() -> None:
    class CustomFeed:
        _request_feed = None
        _trader = None

        def connect(self) -> None:
            raise RuntimeError("synthetic custom feed failure")

    feed = CustomFeed()
    provider = _provider(feed)  # type: ignore[arg-type]

    with pytest.raises(CtpAccountSessionProviderError, match="SESSION_NOT_CONNECTED"):
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)

    with pytest.raises(CtpAccountSessionProviderError, match="NATIVE_CUSTODY_UNCERTAIN"):
        _ = provider.native_owner_created
    assert provider.close() is False
    assert provider._request_feed is feed
    with pytest.raises(CtpAccountSessionProviderError, match="NATIVE_CUSTODY_UNCERTAIN"):
        _ = provider.native_owner_created


def test_ctp_account_provider_rejects_mutated_query_rows_and_scope_change() -> None:
    mutated_feed = _Feed()
    mutated_feed.mutate_source_after_query = True
    provider = _provider(mutated_feed)
    provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_SOURCE_UNTRUSTED"):
        provider.read_account(generation="service-gen-1", timeout=2.0)

    changed_feed = _Feed()
    changed_provider = _provider(changed_feed)
    changed_provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)
    changed_feed.mutate_scope_after_orders = True
    with pytest.raises(CtpAccountSessionProviderError, match="QUERY_SCOPE_CHANGED"):
        changed_provider.read_account(generation="service-gen-1", timeout=2.0)


def test_ctp_account_provider_rejects_borrowed_request_feed_before_connect_or_stop() -> None:
    class BorrowedRequestFeed:
        def __init__(self) -> None:
            self._trader = _Trader()
            self.connect_calls = 0

        def connect(self) -> None:
            self.connect_calls += 1

    class WrapperFeed(_Feed):
        def __init__(self, request_feed: BorrowedRequestFeed) -> None:
            super().__init__()
            self._request_feed = request_feed

    borrowed = BorrowedRequestFeed()
    provider = _provider(WrapperFeed(borrowed))

    with pytest.raises(
        CtpAccountSessionProviderError, match="INVALID_PROVIDER_CONFIGURATION"
    ):
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)

    assert borrowed.connect_calls == 0
    assert borrowed._trader.stop_calls == 0
    assert provider.native_owner_created is False
    assert provider.native_connection_generation is None
    assert provider.close() is True
    assert borrowed._trader.stop_calls == 0


def test_ctp_account_provider_rejects_preconnected_borrowed_trader_without_stopping_it() -> None:
    feed = _Feed()
    borrowed = _Trader()
    feed._trader = borrowed
    provider = _provider(feed)

    with pytest.raises(
        CtpAccountSessionProviderError, match="INVALID_PROVIDER_CONFIGURATION"
    ):
        provider.connect(account_key=_ACCOUNT_KEY, generation="service-gen-1", timeout=2.0)

    assert feed.connect_calls == 0
    assert borrowed.stop_calls == 0
    assert provider.close() is True
    assert borrowed.stop_calls == 0
