"""Offline CTP request capture; no API connection is created."""

import queue
from sys import float_info
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp import client as ctp_client
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture


@pytest.fixture
def feed():
    result = CtpRequestDataFuture(queue.Queue(), broker_id="fixture", user_id="fixture")
    calls = []
    # The managed feed no longer accepts a caller-created ``object()`` as a
    # write capability.  This isolated fixture uses the deliberately private
    # test authority seam while its fake trader verifies identity by object.
    capability = ctp_client._issue_ctp_execution_authority_for_test()
    trader = SimpleNamespace(
        is_ready=True,
        is_read_only_ready=True,
        is_trading_ready=True,
        auto_settlement_confirm=False,
        _req_id=0,
        _front_id=11,
        _session_id=22,
        # The raw request seam must never be used by the feed's managed path.
        api=SimpleNamespace(
            ReqOrderInsert=lambda *_args: pytest.fail("raw order request was used")
        ),
    )
    trader._next_request_id = lambda: (
        setattr(trader, "_req_id", trader._req_id + 1) or trader._req_id
    )
    trader._record_request = lambda _request_type: None
    trader.configure_execution_gate = lambda candidate: (
        {"managed": True, "armed": True}
        if candidate is capability
        else pytest.fail("unexpected execution capability")
    )
    trader.get_execution_gate_state = lambda: {"managed": True, "armed": True}

    def require_execution_write(candidate, _instrument, _exchange_id=""):
        if candidate is not capability:
            pytest.fail("unexpected execution capability")

    trader.require_execution_write = require_execution_write
    trader.submit_order_insert = lambda field, _request_id, *, execution_capability: (
        calls.append(field) or 0
        if execution_capability is capability
        else pytest.fail("unexpected execution capability")
    )
    result._trader = trader
    result.configure_execution_gate(capability)
    result._test_execution_capability = capability
    result._connected = True
    return result, calls


@pytest.mark.parametrize(
    "tif",
    [
        "GFD",
        "DAY",
    ],
)
@pytest.mark.parametrize("offset,flag", [("close_today", "3"), ("close_yesterday", "4")])
def test_native_time_in_force_preserves_dated_close(feed, tif, offset, flag):
    client, calls = feed
    result = client.make_order(
        "rb2610",
        2,
        3500,
        "sell-limit",
        offset=offset,
        exchange_id="SHFE",
        client_order_id="123",
        time_in_force=tif,
        _execution_capability=client._test_execution_capability,
    )
    assert result.get_status()
    field = calls[0]
    assert field.TimeCondition == "3"
    assert field.VolumeCondition == "1"
    assert field.MinVolume == 1
    assert field.CombOffsetFlag == flag and field.ExchangeID == "SHFE"


@pytest.mark.parametrize("tif", ["GTC", "IOC", "FOK", "UNKNOWN"])
def test_iteration22_rejects_non_gfd_time_in_force(feed, tif):
    client, calls = feed
    with pytest.raises(ValueError, match="requires GFD"):
        client.make_order(
            "rb2610",
            2,
            3500,
            "sell-limit",
            offset="close_today",
            exchange_id="SHFE",
            time_in_force=tif,
            _execution_capability=client._test_execution_capability,
        )
    assert calls == []


@pytest.mark.parametrize("price", [float("nan"), float("inf"), float_info.max, -1.0])
def test_iteration22_rejects_invalid_price_before_native_order_request(feed, price):
    client, calls = feed
    with pytest.raises(ValueError, match="positive price with a finite value"):
        client.make_order(
            "rb2610",
            2,
            price,
            "sell-limit",
            offset="close_today",
            exchange_id="SHFE",
            time_in_force="GFD",
            _execution_capability=client._test_execution_capability,
        )
    assert calls == []
    assert client._trader._req_id == 0


def test_query_local_ref_requires_matching_front_and_session(feed):
    client, _ = feed
    queries = []
    base = dict(
        InstrumentID="rb2610",
        OrderRef="123",
        Direction="0",
        CombOffsetFlag="0",
        LimitPrice=3500,
        VolumeTotalOriginal=1,
        VolumeTotal=1,
        VolumeTraded=0,
        OrderStatus="3",
        ExchangeID="SHFE",
    )

    class CompleteResult:
        complete = True
        request_id = 1
        connection_generation = 1
        is_last_seen = True
        error_code = None
        error_message = ""
        timed_out = False
        unsupported = False

        def __init__(self, records):
            self.records = tuple(records)

        def as_dict(self, **_kwargs):
            return {"complete": True}

    def query(**kwargs):
        queries.append(kwargs)
        return CompleteResult(
            [
                {**base, "FrontID": 11, "SessionID": 22, "OrderSysID": "SYS1"},
                {**base, "FrontID": 11, "SessionID": 23, "OrderSysID": "SYS2"},
                {**base, "FrontID": 12, "SessionID": 22, "OrderSysID": "SYS3"},
            ]
        )

    client._trader.query_orders_result = query
    response = client.query_order("rb2610", None, order_ref="123", front_id=11, session_id=22)
    assert queries[0]["order_sys_id"] == ""
    rows = [row.init_data() for row in response.get_data()]
    assert len(rows) == 1 and rows[0].get_order_id() == "SYS1"


@pytest.mark.parametrize("subscribe_account,expected", [(True, 2), (False, 1)])
def test_subscription_streams_are_owned_for_shutdown(monkeypatch, subscribe_account, expected):
    from bt_api_ctp import plugin

    class Stream:
        def __init__(self, *args, **kwargs):
            self.started = False

        def start(self):
            self.started = True

    monkeypatch.setattr(plugin, "CtpMarketStream", Stream)
    monkeypatch.setattr(plugin, "CtpTradeStream", Stream)
    api = SimpleNamespace(_subscription_streams=[], _subscription_flags={}, log=lambda _: None)
    plugin._ctp_future_subscribe_handler(
        queue.Queue(),
        {"subscribe_account": subscribe_account},
        [{"topic": "tick"}],
        api,
    )
    assert len(api._subscription_streams) == expected
    assert all(stream.started for stream in api._subscription_streams)
    assert bool(api._subscription_flags.get("CTP___FUTURE_account")) == subscribe_account
