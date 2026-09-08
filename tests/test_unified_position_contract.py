"""Offline CTP request capture; no API connection is created."""

import queue
from types import SimpleNamespace

import pytest

from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture


@pytest.fixture
def feed():
    result = CtpRequestDataFuture(queue.Queue(), broker_id="fixture", user_id="fixture")
    calls = []
    result._trader = SimpleNamespace(
        is_ready=True,
        _req_id=0,
        _front_id=11,
        _session_id=22,
        api=SimpleNamespace(ReqOrderInsert=lambda field, ref: calls.append(field) or 0),
    )
    result._connected = True
    return result, calls


@pytest.mark.parametrize(
    "tif,time_condition,volume_condition,minimum",
    [
        ("GTC", "3", "1", 1),
        ("IOC", "1", "1", 1),
        ("FOK", "1", "3", 2),
    ],
)
@pytest.mark.parametrize("offset,flag", [("close_today", "3"), ("close_yesterday", "4")])
def test_native_time_in_force_preserves_dated_close(
    feed, tif, time_condition, volume_condition, minimum, offset, flag
):
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
    )
    assert result.get_status()
    field = calls[0]
    assert field.TimeCondition == time_condition
    assert field.VolumeCondition == volume_condition
    assert field.MinVolume == minimum
    assert field.CombOffsetFlag == flag and field.ExchangeID == "SHFE"


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

    def query(**kwargs):
        queries.append(kwargs)
        return [
            {**base, "FrontID": 11, "SessionID": 22, "OrderSysID": "SYS1"},
            {**base, "FrontID": 11, "SessionID": 23, "OrderSysID": "SYS2"},
            {**base, "FrontID": 12, "SessionID": 22, "OrderSysID": "SYS3"},
        ]

    client._trader.query_orders = query
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
        queue.Queue(), {"subscribe_account": subscribe_account}, [{"topic": "tick"}], api
    )
    assert len(api._subscription_streams) == expected
    assert all(stream.started for stream in api._subscription_streams)
    assert bool(api._subscription_flags.get("CTP___FUTURE_account")) == subscribe_account
