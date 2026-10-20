"""Focused tests for the native CTP quote-v2 ServiceMarket adapter."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from bt_api_base.market_protocol import MarketInstrumentKey, MarketSubscriptionKey

from bt_api_ctp.ctp.client import CtpNativeStopReceipt
from bt_api_ctp.feeds import live_ctp_feed
from bt_api_ctp.feeds.live_ctp_feed import ConnectionState, CtpMarketStream
from bt_api_ctp.market_provider import (
    BoundedCtpQuoteV2Queue,
    CtpQuoteV2MarketProvider,
    CtpQuoteV2MarketProviderError,
)

_SOURCE_REF = "ctp-native-market-r1"
_ENTITLEMENT_REF = "ctp-quote-v2-entitlement-r1"


def _key(**updates: object) -> MarketSubscriptionKey:
    values: dict[str, object] = {
        "source_ref": _SOURCE_REF,
        "entitlement_ref": _ENTITLEMENT_REF,
        "instrument": MarketInstrumentKey(
            provider="ctp",
            venue="CFFEX",
            environment="simnow",
            product_type="future",
            instrument_id="IF2612",
            native_symbol="IF2612",
        ),
        "channel": "ctp.quote.v2",
    }
    values.update(updates)
    return MarketSubscriptionKey(**values)  # type: ignore[arg-type]


class _SyntheticMdClient:
    instances: list[_SyntheticMdClient] = []
    stop_receipts_on_create: list[CtpNativeStopReceipt] = []

    def __init__(self, *_args: object) -> None:
        self.connection_generation = 1
        self.on_login = None
        self.on_subscribe_response = None
        self.on_disconnect = None
        self.on_tick = None
        self.subscribed: list[str] = []
        self.next_request_id = 1
        self.subscribe_additional_calls = 0
        self.unsubscribed: list[str] = []
        self.fail_subscribe_additional_after_accept = False
        self.stop_calls = 0
        self.stop_wait_calls = 0
        self.stop_receipts = list(self.__class__.stop_receipts_on_create)
        self.__class__.stop_receipts_on_create = []
        self.emit_startup_acks = True
        self.reverse_subscription_acks = False
        self.emit_startup_readiness_tick = True
        self.emit_tick_before_startup_acks = False
        self.start_entered: threading.Event | None = None
        self.allow_start: threading.Event | None = None
        self.start_thread_id: int | None = None
        self.__class__.instances.append(self)

    def subscribe(self, instruments: list[str]) -> None:
        self.subscribed.extend(instruments)

    def subscribe_additional(self, instruments: list[str]) -> tuple[str, ...]:
        self.subscribe_additional_calls += 1
        additions = tuple(item for item in instruments if item not in self.subscribed)
        self.subscribed.extend(additions)
        if additions:
            request_id = self.next_request_id
            self.next_request_id += 1
            self.emit_subscription_batch(additions, request_id=request_id)
        if self.fail_subscribe_additional_after_accept:
            self.fail_subscribe_additional_after_accept = False
            raise RuntimeError("synthetic ambiguous native subscribe")
        return additions

    def unsubscribe(self, instruments: list[str]) -> bool:
        self.unsubscribed.extend(instruments)
        self.subscribed = [item for item in self.subscribed if item not in instruments]
        return True

    def start(self, *, block: bool = False) -> None:
        assert block is False
        self.start_thread_id = threading.get_ident()
        if self.start_entered is not None:
            self.start_entered.set()
            assert self.allow_start is not None
            if not self.allow_start.wait(timeout=3.0):
                raise TimeoutError("synthetic native startup was not released")
        if self.__class__.authenticate:
            assert callable(self.on_login)
            self.on_login(SimpleNamespace())
            acknowledgements = list(self.subscribed)
            if self.emit_tick_before_startup_acks and acknowledgements:
                self.emit_tick(
                    InstrumentID=acknowledgements[0],
                    ExchangeID="CFFEX",
                    LastPrice=4_001.0,
                    Volume=1,
                    TradingDay="20261020",
                    ActionDay="20261020",
                    UpdateTime="09:30:01",
                    UpdateMillisec=0,
                )
            if self.reverse_subscription_acks:
                acknowledgements.reverse()
            if self.emit_startup_acks:
                request_id = self.next_request_id
                self.next_request_id += 1
                self.emit_subscription_batch(acknowledgements, request_id=request_id)
            if self.emit_startup_readiness_tick and acknowledgements:
                self.emit_tick(
                    InstrumentID=acknowledgements[0],
                    ExchangeID="CFFEX",
                    LastPrice=4_001.0,
                    Volume=1,
                    TradingDay="20261020",
                    ActionDay="20261020",
                    UpdateTime="09:30:01",
                    UpdateMillisec=0,
                )

    def emit_subscription_ack(
        self,
        instrument: str,
        *,
        request_id: int = 1,
        is_last: bool = True,
        error_id: int = 0,
        connection_generation: int | None = None,
    ) -> None:
        assert callable(self.on_subscribe_response)
        self.on_subscribe_response(
            SimpleNamespace(InstrumentID=instrument),
            SimpleNamespace(ErrorID=error_id),
            request_id,
            is_last,
            self.connection_generation
            if connection_generation is None
            else connection_generation,
        )

    def emit_subscription_batch(
        self,
        instruments: tuple[str, ...] | list[str],
        *,
        request_id: int,
        error_id: int = 0,
    ) -> None:
        for index, instrument in enumerate(instruments):
            self.emit_subscription_ack(
                instrument,
                request_id=request_id,
                is_last=index == len(instruments) - 1,
                error_id=error_id,
            )

    def emit_tick(self, **fields: object) -> None:
        assert callable(self.on_tick)
        self.on_tick(SimpleNamespace(**fields))

    def emit_disconnect(self, reason: int = 0) -> None:
        assert callable(self.on_disconnect)
        self.on_disconnect(reason)

    def stop(self) -> None:
        self.stop_calls += 1

    def stop_and_wait(self, *, timeout: float) -> CtpNativeStopReceipt:
        assert timeout >= 0
        self.stop_wait_calls += 1
        if self.stop_receipts:
            return self.stop_receipts.pop(0)
        return CtpNativeStopReceipt(
            connection_generation=self.connection_generation,
            join_required=False,
            join_completed=True,
            native_released=True,
            thread_alive=False,
            timed_out=False,
        )

    authenticate = True


def _stream_factory(key: MarketSubscriptionKey, queue: BoundedCtpQuoteV2Queue) -> CtpMarketStream:
    del key
    # Explicit synthetic endpoints and identifiers prevent this fixture from
    # consulting process-level CTP configuration or creating a native session.
    return CtpMarketStream(
        queue,
        broker_id="synthetic-broker",
        user_id="synthetic-user",
        password="synthetic-password",
        auth_code="synthetic-auth-code",
        app_id="synthetic-app",
        td_front="tcp://127.0.0.1:1",
        md_front="tcp://127.0.0.1:1",
        ctp_env_profile="config_front_pair",
        ctp_environment="simnow",
    )


def _manual_ack_stream(
    monkeypatch: pytest.MonkeyPatch,
    symbols: tuple[str, ...],
) -> tuple[CtpMarketStream, _SyntheticMdClient, BoundedCtpQuoteV2Queue]:
    _SyntheticMdClient.instances.clear()
    _SyntheticMdClient.authenticate = True

    def make_client(*args: object) -> _SyntheticMdClient:
        client = _SyntheticMdClient(*args)
        client.emit_startup_acks = False
        client.emit_startup_readiness_tick = False
        return client

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", make_client)
    queue = BoundedCtpQuoteV2Queue(capacity=8)
    stream = _stream_factory(_key(), queue)
    assert stream.subscribe_topics(
        [{"topic": "tick", "symbol": symbol} for symbol in symbols]
    ) is None
    assert stream.enable_quote_v2_service_readiness() is True
    stream.connect()
    (client,) = _SyntheticMdClient.instances
    assert stream.state is ConnectionState.AUTHENTICATED
    assert stream.get_quote_v2_service_ready_scope() is None
    return stream, client, queue


def _emit_synthetic_tick(client: _SyntheticMdClient, symbol: str, second: int) -> None:
    client.emit_tick(
        InstrumentID=symbol,
        ExchangeID="CFFEX",
        LastPrice=4_001.0 + second,
        PreSettlementPrice=3_990.0,
        OpenPrice=3_995.0,
        HighestPrice=4_010.0,
        LowestPrice=3_980.0,
        BidPrice1=4_000.0,
        BidVolume1=2,
        AskPrice1=4_002.0,
        AskVolume1=3,
        OpenInterest=1_000.0,
        Volume=123 + second,
        Turnover=492_000.0,
        UpperLimitPrice=4_200.0,
        LowerLimitPrice=3_800.0,
        TradingDay="20261020",
        ActionDay="20261020",
        UpdateTime=f"09:30:{second:02d}",
        UpdateMillisec=0,
    )


def test_service_readiness_requires_all_symbols_and_terminal_ack_then_a_fresh_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream, client, queue = _manual_ack_stream(
        monkeypatch, ("IF2612", "IH2612")
    )
    try:
        # The response order is reversed from the requested topics. A tick for
        # the non-final response cannot make the shared stream ready.
        client.emit_subscription_ack("IH2612", request_id=31, is_last=False)
        _emit_synthetic_tick(client, "IH2612", 1)
        assert stream.get_quote_v2_service_ready_scope() is None
        assert queue.size == 0

        # bIsLast belongs to the request batch: readiness waits for the final
        # response as well as an ACK for each requested symbol.
        client.emit_subscription_ack("IF2612", request_id=31, is_last=True)
        assert stream.get_quote_v2_service_ready_scope() is None
        _emit_synthetic_tick(client, "IF2612", 2)
        scope = stream.get_quote_v2_service_ready_scope()
        assert scope is not None
        assert stream.is_quote_v2_service_symbol_ready("IF2612") is True
        assert stream.is_quote_v2_service_symbol_ready("IH2612") is True
        assert queue.size == 0  # the first post-ACK tick establishes readiness

        _emit_synthetic_tick(client, "IH2612", 3)
        assert queue.size == 1
    finally:
        stream.disconnect()


@pytest.mark.parametrize(
    ("request_id", "is_last", "error_id", "generation_offset"),
    (
        (31, False, 7, 0),  # A non-final per-symbol error is still an error.
        (31, 0, 0, 0),  # bIsLast must be an actual bool.
        (True, False, 0, 0),  # bool is not accepted as an integer request ID.
        (31, False, 0, 1),  # A callback from another generation is stale.
    ),
)
def test_invalid_subscribe_responses_fail_service_readiness_closed(
    monkeypatch: pytest.MonkeyPatch,
    request_id: int,
    is_last: object,
    error_id: int,
    generation_offset: int,
) -> None:
    stream, client, _queue = _manual_ack_stream(monkeypatch, ("IF2612",))
    try:
        client.emit_subscription_ack(
            "IF2612",
            request_id=request_id,
            is_last=is_last,  # type: ignore[arg-type]
            error_id=error_id,
            connection_generation=client.connection_generation + generation_offset,
        )
        assert stream.quote_v2_service_fail_closed is True
        assert stream.state is ConnectionState.ERROR
        assert stream.get_quote_v2_service_ready_scope() is None
    finally:
        stream.disconnect()


def test_terminal_subscribe_response_rejects_late_other_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream, client, _queue = _manual_ack_stream(
        monkeypatch, ("IF2612", "IH2612")
    )
    try:
        client.emit_subscription_ack("IF2612", request_id=41, is_last=True)
        client.emit_subscription_ack("IH2612", request_id=41, is_last=False)
        assert stream.quote_v2_service_fail_closed is True
        assert stream.state is ConnectionState.ERROR
        assert stream.get_quote_v2_service_ready_scope() is None
    finally:
        stream.disconnect()


def test_ctp_market_provider_projects_the_native_callback_and_opaque_clock_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _SyntheticMdClient)
    key = _key()
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=1.0,
    )

    handle = provider.subscribe(key, generation="hub-generation-r1")
    (client,) = _SyntheticMdClient.instances
    assert client.subscribed == ["IF2612"]
    assert handle.stream.get_quote_clock_source_scope() is not None
    scope = handle.stream.get_quote_clock_source_scope()
    assert scope is not None

    client.emit_tick(
        InstrumentID="IF2612",
        ExchangeID="CFFEX",
        LastPrice=4_001.0,
        PreSettlementPrice=3_990.0,
        OpenPrice=3_995.0,
        HighestPrice=4_010.0,
        LowestPrice=3_980.0,
        BidPrice1=4_000.0,
        BidVolume1=2,
        AskPrice1=4_002.0,
        AskVolume1=3,
        OpenInterest=1_000.0,
        Volume=123,
        Turnover=492_000.0,
        UpperLimitPrice=4_200.0,
        LowerLimitPrice=3_800.0,
        TradingDay="20261017",
        ActionDay="20261017",
        UpdateTime="09:30:01",
        UpdateMillisec=250,
    )

    (event,) = provider.poll(handle, limit=8)
    assert event.generation == "hub-generation-r1"
    assert event.observed_at_utc.endswith("Z")
    assert event.data["schema_version"] == "ctp.quote.v2"
    assert event.data["exchange_id"] == "CFFEX"
    assert event.data["instrument_id"] == "IF2612"
    assert event.data["symbol"] == "IF2612"
    assert event.data["asset_type"] == "future"
    assert event.data["source"] == "ctp.native.md"
    assert event.data["clock_domain_id"] == scope.clock_domain_id
    assert event.data["clock_source_session_id"] == scope.clock_source_session_id
    assert event.data["clock_source"] == scope.clock_source
    assert event.data["source_clock_quality"] == "unknown"
    assert event.data["receive_clock_quality"] == "unknown"
    assert event.data["source_clock_error_ms"] is None
    assert event.data["receive_clock_error_ms"] is None
    assert event.data["freshness_verified"] is False
    assert event.data["execution_eligible"] is False
    assert event.data["execution_qualified"] is False
    assert event.data["last_price"] == 4_001.0
    assert event.data["bid_price"] == 4_000.0
    assert event.data["ask_price"] == 4_002.0
    assert event.data["recv_time_utc"] == event.observed_at_utc
    assert event.data["event_time_utc"].endswith("Z")

    assert provider.unsubscribe(handle) is True
    assert client.stop_calls == 1
    assert provider.diagnostics["active_handles"] == 0


def test_ctp_market_provider_shares_one_authenticated_session_across_keys_and_unsubscribes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _SyntheticMdClient)
    first_key = _key()
    second_key = _key(
        instrument=MarketInstrumentKey(
            provider="ctp",
            venue="CFFEX",
            environment="simnow",
            product_type="future",
            instrument_id="IH2612",
            native_symbol="IH2612",
        )
    )
    streams: list[CtpMarketStream] = []

    def factory(key: MarketSubscriptionKey, sink: BoundedCtpQuoteV2Queue) -> CtpMarketStream:
        stream = _stream_factory(key, sink)
        streams.append(stream)
        return stream

    provider = CtpQuoteV2MarketProvider(
        stream_factory=factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=1.0,
    )
    first = provider.subscribe(first_key, generation="hub-generation-a")
    (client,) = _SyntheticMdClient.instances
    # A native request ID can be reused after a completed request. The stream
    # explicitly reopens it for this pending addition before its first ACK.
    client.next_request_id = 1
    second = provider.subscribe(second_key, generation="hub-generation-b")
    assert len(streams) == 1
    assert first.stream is second.stream is streams[0]
    assert client.subscribed == ["IF2612", "IH2612"]
    assert first.stream._quote_v2_service_symbol_request_ids == {
        "IF2612": 1,
        "IH2612": 1,
    }
    assert first.stream._quote_v2_service_completed_request_ids == {1}
    assert first.stream.get_quote_v2_service_ready_scope() is not None
    assert provider.diagnostics["active_sessions"] == 1

    first_descriptor = provider.describe_source(
        first,
        key=first_key,
        generation=first.generation,
        stream_id=first_key.stream_id,
        epoch="hub-epoch-a",
    )
    second_descriptor = provider.describe_source(
        second,
        key=second_key,
        generation=second.generation,
        stream_id=second_key.stream_id,
        epoch="hub-epoch-b",
    )
    assert first_descriptor is not None and second_descriptor is not None
    assert first_descriptor.key == first_key and second_descriptor.key == second_key
    assert first_descriptor.stream_id != second_descriptor.stream_id
    assert first_descriptor.epoch == "hub-epoch-a"
    assert second_descriptor.epoch == "hub-epoch-b"
    assert first_descriptor.connection_generation == second_descriptor.connection_generation == 1
    assert first_descriptor.clock_domain_id == second_descriptor.clock_domain_id
    assert first_descriptor.clock_source_session_id == second_descriptor.clock_source_session_id

    for symbol in ("IF2612", "IH2612"):
        client.emit_tick(
            InstrumentID=symbol,
            ExchangeID="CFFEX",
            LastPrice=4_001.0,
            PreSettlementPrice=3_990.0,
            OpenPrice=3_995.0,
            HighestPrice=4_010.0,
            LowestPrice=3_980.0,
            BidPrice1=4_000.0,
            BidVolume1=2,
            AskPrice1=4_002.0,
            AskVolume1=3,
            OpenInterest=1_000.0,
            Volume=123,
            Turnover=492_000.0,
            UpperLimitPrice=4_200.0,
            LowerLimitPrice=3_800.0,
            TradingDay="20261017",
            ActionDay="20261017",
            UpdateTime="09:30:01",
            UpdateMillisec=250,
        )
    (first_event,) = provider.poll(first, limit=8)
    (second_event,) = provider.poll(second, limit=8)
    assert first_event.data["symbol"] == "IF2612"
    assert second_event.data["symbol"] == "IH2612"
    assert first_event.data["clock_domain_id"] == second_event.data["clock_domain_id"]
    assert (
        first_event.data["clock_source_session_id"] == second_event.data["clock_source_session_id"]
    )

    assert provider.unsubscribe(first) is True
    assert client.unsubscribed == ["IF2612"]
    assert client.stop_calls == 0
    assert second.active is True
    assert provider.unsubscribe(second) is True
    assert client.unsubscribed == ["IF2612"]
    assert client.stop_calls == 1
    assert provider.diagnostics["active_sessions"] == 0
    assert provider.diagnostics["active_handles"] == 0


def test_incremental_keys_keep_one_md_epoch_and_preserve_queued_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _SyntheticMdClient)
    keys = tuple(
        _key(
            instrument=MarketInstrumentKey(
                provider="ctp",
                venue="CFFEX",
                environment="simnow",
                product_type="future",
                instrument_id=symbol,
                native_symbol=symbol,
            )
        )
        for symbol in ("IF2612", "IH2612", "IC2612")
    )
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=1.0,
    )

    first = provider.subscribe(keys[0], generation="hub-generation-a")
    (client,) = _SyntheticMdClient.instances

    def emit(symbol: str, second: int) -> None:
        client.emit_tick(
            InstrumentID=symbol,
            ExchangeID="CFFEX",
            LastPrice=4_001.0,
            PreSettlementPrice=3_990.0,
            OpenPrice=3_995.0,
            HighestPrice=4_010.0,
            LowestPrice=3_980.0,
            BidPrice1=4_000.0,
            BidVolume1=2,
            AskPrice1=4_002.0,
            AskVolume1=3,
            OpenInterest=1_000.0,
            Volume=123,
            Turnover=492_000.0,
            UpperLimitPrice=4_200.0,
            LowerLimitPrice=3_800.0,
            TradingDay="20261017",
            ActionDay="20261017",
            UpdateTime=f"09:30:0{second}",
            UpdateMillisec=250,
        )

    # The first key can already have a valid native callback in the Hub's
    # per-key replay path while later keys are still being installed.
    emit("IF2612", 1)
    assert first.queue.size == 1
    assert first.stream._quote_v2_subscription_metadata["IF2612"]["subscription_epoch"] == 1

    second = provider.subscribe(keys[1], generation="hub-generation-b")
    emit("IH2612", 2)
    assert first.queue.size == 1
    assert second.queue.size == 1

    third = provider.subscribe(keys[2], generation="hub-generation-c")
    assert first.queue.size == 1
    assert second.queue.size == 1
    assert third.queue.size == 0
    assert {
        symbol: metadata["subscription_epoch"]
        for symbol, metadata in first.stream._quote_v2_subscription_metadata.items()
    } == {"IF2612": 1, "IH2612": 1, "IC2612": 1}

    for symbol in ("IF2612", "IH2612", "IC2612"):
        emit(symbol, 3)

    first_events = provider.poll(first, limit=8)
    second_events = provider.poll(second, limit=8)
    third_events = provider.poll(third, limit=8)
    assert [event.data["symbol"] for event in first_events] == ["IF2612", "IF2612"]
    assert [event.data["symbol"] for event in second_events] == ["IH2612", "IH2612"]
    assert [event.data["symbol"] for event in third_events] == ["IC2612"]
    assert {
        event.data["subscription_epoch"]
        for events in (first_events, second_events, third_events)
        for event in events
    } == {1}

    assert provider.unsubscribe(first) is True
    assert provider.unsubscribe(second) is True
    assert provider.unsubscribe(third) is True
    assert client.stop_calls == 1
    assert provider.diagnostics["active_sessions"] == 0
    assert provider.diagnostics["active_handles"] == 0


def test_retired_symbol_requires_confirmed_fresh_stream_before_resubscribe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _SyntheticMdClient)
    keys = tuple(
        _key(
            instrument=MarketInstrumentKey(
                provider="ctp",
                venue="CFFEX",
                environment="simnow",
                product_type="future",
                instrument_id=symbol,
                native_symbol=symbol,
            )
        )
        for symbol in ("IF2612", "IH2612")
    )
    streams: list[CtpMarketStream] = []

    def factory(key: MarketSubscriptionKey, sink: BoundedCtpQuoteV2Queue) -> CtpMarketStream:
        stream = _stream_factory(key, sink)
        streams.append(stream)
        return stream

    provider = CtpQuoteV2MarketProvider(
        stream_factory=factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        stop_timeout_seconds=1.0,
    )
    first = provider.subscribe(keys[0], generation="hub-generation-a")
    second = provider.subscribe(keys[1], generation="hub-generation-b")
    (old_client,) = _SyntheticMdClient.instances
    old_callback = old_client.on_tick
    old_stream = first.stream

    assert provider.unsubscribe(first) is True
    assert old_stream._retired_quote_v2_subscriptions == {"IF2612": 1}
    subscribe_calls_before_readd = tuple(old_client.subscribed)
    source_scope_before_readd = provider._source_scope
    with pytest.raises(CtpQuoteV2MarketProviderError) as error:
        provider.subscribe(keys[0], generation="hub-generation-a2")
    assert error.value.code == "CTP_MARKET_FRESH_STREAM_REQUIRED"
    assert tuple(old_client.subscribed) == subscribe_calls_before_readd
    assert provider._source_scope is source_scope_before_readd is second.source_scope
    assert old_stream.get_quote_clock_source_scope() is second.source_scope
    assert provider.diagnostics["active_handles"] == 1
    assert second.active is True

    old_client.emit_tick(
        InstrumentID="IH2612",
        ExchangeID="CFFEX",
        LastPrice=4_001.0,
        PreSettlementPrice=3_990.0,
        OpenPrice=3_995.0,
        HighestPrice=4_010.0,
        LowestPrice=3_980.0,
        BidPrice1=4_000.0,
        BidVolume1=2,
        AskPrice1=4_002.0,
        AskVolume1=3,
        OpenInterest=1_000.0,
        Volume=123,
        Turnover=492_000.0,
        UpperLimitPrice=4_200.0,
        LowerLimitPrice=3_800.0,
        TradingDay="20261017",
        ActionDay="20261017",
        UpdateTime="09:30:01",
        UpdateMillisec=250,
    )
    (active_event,) = provider.poll(second, limit=8)
    assert active_event.data["symbol"] == "IH2612"

    old_client.stop_receipts.extend(
        (
            CtpNativeStopReceipt(
                connection_generation=1,
                join_required=False,
                join_completed=True,
                native_released=False,
                thread_alive=False,
                timed_out=False,
            ),
            CtpNativeStopReceipt(
                connection_generation=1,
                join_required=False,
                join_completed=True,
                native_released=True,
                thread_alive=False,
                timed_out=False,
            ),
        )
    )
    assert provider.close() is False
    assert provider.diagnostics["active_handles"] == 1
    assert len(_SyntheticMdClient.instances) == 1
    with pytest.raises(CtpQuoteV2MarketProviderError) as error:
        provider.subscribe(keys[0], generation="hub-generation-a2")
    assert error.value.code == "CTP_MARKET_FRESH_STREAM_REQUIRED"
    assert len(_SyntheticMdClient.instances) == 1

    assert provider.close() is True
    assert provider.diagnostics["active_handles"] == 0
    fresh = provider.subscribe(keys[0], generation="hub-generation-a3")
    assert len(streams) == 2
    assert fresh.stream is streams[1]
    assert fresh.stream is not old_stream
    (new_client,) = _SyntheticMdClient.instances[1:]
    assert new_client is not old_client

    # The old callback closure cannot be relabelled into the new stream after
    # confirmed full close and a fresh native client token.
    old_queue_size = fresh.queue.size
    old_callback(
        SimpleNamespace(
            InstrumentID="IF2612",
            ExchangeID="CFFEX",
            LastPrice=4_001.0,
            PreSettlementPrice=3_990.0,
            OpenPrice=3_995.0,
            HighestPrice=4_010.0,
            LowestPrice=3_980.0,
            BidPrice1=4_000.0,
            BidVolume1=2,
            AskPrice1=4_002.0,
            AskVolume1=3,
            OpenInterest=1_000.0,
            Volume=123,
            Turnover=492_000.0,
            UpperLimitPrice=4_200.0,
            LowerLimitPrice=3_800.0,
            TradingDay="20261017",
            ActionDay="20261017",
            UpdateTime="09:30:02",
            UpdateMillisec=250,
        )
    )
    assert fresh.queue.size == old_queue_size == 0
    assert provider.poll(fresh, limit=8) == ()
    assert provider.unsubscribe(fresh) is True


def test_retired_symbol_limit_fences_additions_but_allows_provider_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _SyntheticMdClient)
    keys = tuple(
        _key(
            instrument=MarketInstrumentKey(
                provider="ctp",
                venue="CFFEX",
                environment="simnow",
                product_type="future",
                instrument_id=symbol,
                native_symbol=symbol,
            )
        )
        for symbol in ("IF2612", "IH2612", "IC2612")
    )
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
    )
    first = provider.subscribe(keys[0], generation="hub-generation-a")
    second = provider.subscribe(keys[1], generation="hub-generation-b")
    (client,) = _SyntheticMdClient.instances
    retired = first.stream._retired_quote_v2_subscriptions
    retired.update(
        {
            f"retired-{index}": first.stream._md_client_token
            for index in range(live_ctp_feed._MAX_CTP_QUOTE_V2_RETIRED_SYMBOLS - 1)
        }
    )
    # Releasing this handle reaches the tombstone limit, fences additions for
    # this native client, and still allows the normal Hub-style per-handle
    # teardown sequence to continue.
    assert provider.unsubscribe(first) is True
    assert len(retired) == live_ctp_feed._MAX_CTP_QUOTE_V2_RETIRED_SYMBOLS
    assert first.active is False and second.active is True
    assert provider.diagnostics["active_handles"] == 1
    assert tuple(client.unsubscribed) == ("IF2612",)
    native_symbols_before_fenced_add = tuple(client.subscribed)
    native_add_calls_before_fenced_add = client.subscribe_additional_calls

    with pytest.raises(CtpQuoteV2MarketProviderError) as error:
        provider.subscribe(keys[2], generation="hub-generation-c")
    assert error.value.code == "CTP_MARKET_RETIRED_SYMBOL_LIMIT"
    assert tuple(client.subscribed) == native_symbols_before_fenced_add
    assert client.subscribe_additional_calls == native_add_calls_before_fenced_add
    assert provider._source_scope is second.source_scope
    assert second.stream.get_quote_clock_source_scope() is second.source_scope

    # The remaining handle can still be released, and its final release stops
    # the exact native session despite the full retired-symbol ledger.
    assert provider.unsubscribe(second) is True
    assert client.unsubscribed == ["IF2612"]
    assert client.stop_calls == 1
    assert provider.diagnostics["active_handles"] == 0
    assert provider.diagnostics["active_sessions"] == 0


def test_failed_additional_native_subscription_is_rolled_back_and_session_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _SyntheticMdClient)
    first_key = _key()
    second_key = _key(
        instrument=MarketInstrumentKey(
            provider="ctp",
            venue="CFFEX",
            environment="simnow",
            product_type="future",
            instrument_id="IH2612",
            native_symbol="IH2612",
        )
    )
    streams: list[CtpMarketStream] = []

    def factory(key: MarketSubscriptionKey, sink: BoundedCtpQuoteV2Queue) -> CtpMarketStream:
        stream = _stream_factory(key, sink)
        streams.append(stream)
        return stream

    provider = CtpQuoteV2MarketProvider(
        stream_factory=factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=1.0,
    )
    first = provider.subscribe(first_key, generation="hub-generation-a")
    stream = first.stream
    (client,) = _SyntheticMdClient.instances
    client.fail_subscribe_additional_after_accept = True

    with pytest.raises(CtpQuoteV2MarketProviderError) as error:
        provider.subscribe(second_key, generation="hub-generation-b")

    assert error.value.code == "CTP_MARKET_SUBSCRIBE_FAILED"
    assert streams == [stream]
    # The fake native API accepted the request before raising. The stream
    # retains that ambiguity while removing the failed symbol from the
    # provider-visible metadata/handle set and revoking the source scope.
    assert client.subscribed == ["IF2612", "IH2612"]
    assert set(stream._quote_v2_subscription_metadata) == {"IF2612"}
    assert stream._ambiguous_quote_v2_subscriptions == {"IH2612"}
    assert stream.get_quote_clock_source_scope() is None
    assert stream.state is ConnectionState.ERROR
    assert set(provider._handles_by_symbol) == {"IF2612"}
    assert provider._fanout is not None
    assert set(provider._fanout._queues) == {"IF2612"}
    assert provider.diagnostics["active_handles"] == 1
    client.on_login(SimpleNamespace())
    assert stream.state is ConnectionState.ERROR
    client.emit_tick(InstrumentID="IF2612", LastPrice=4_001.0)
    assert first.queue.size == 0

    with pytest.raises(CtpQuoteV2MarketProviderError) as poll_error:
        provider.poll(first, limit=1)
    assert poll_error.value.code == "CTP_MARKET_SOURCE_SCOPE_CHANGED"
    with pytest.raises(CtpQuoteV2MarketProviderError) as resubscribe_error:
        provider.subscribe(second_key, generation="hub-generation-c")
    assert resubscribe_error.value.code == "CTP_MARKET_SOURCE_SCOPE_CHANGED"
    assert client.subscribed == ["IF2612", "IH2612"]

    # The retained original handle is the cleanup authority for the whole
    # session, including the ambiguous native symbol not present in metadata.
    assert provider.unsubscribe(first) is True
    assert client.stop_calls == 1
    assert provider.diagnostics["active_handles"] == 0
    assert provider.diagnostics["active_sessions"] == 0


def test_post_subscribe_metadata_mismatch_fences_the_entire_shared_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _SyntheticMdClient)
    first_key = _key()
    second_key = _key(
        instrument=MarketInstrumentKey(
            provider="ctp",
            venue="CFFEX",
            environment="simnow",
            product_type="future",
            instrument_id="IH2612",
            native_symbol="IH2612",
        )
    )
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=1.0,
    )
    first = provider.subscribe(first_key, generation="hub-generation-a")
    stream = first.stream
    original_subscribe_topics = stream.subscribe_topics

    def subscribe_then_drop_metadata(topics: object) -> None:
        original_subscribe_topics(topics)
        stream._quote_v2_subscription_metadata.pop("IH2612", None)

    stream.subscribe_topics = subscribe_then_drop_metadata
    with pytest.raises(CtpQuoteV2MarketProviderError) as error:
        provider.subscribe(second_key, generation="hub-generation-b")

    assert error.value.code == "CTP_MARKET_SUBSCRIBE_FAILED"
    (client,) = _SyntheticMdClient.instances
    assert client.subscribed == ["IF2612", "IH2612"]
    assert set(stream._quote_v2_subscription_metadata) == {"IF2612"}
    assert stream._ambiguous_quote_v2_subscriptions == {"IH2612"}
    assert stream.get_quote_clock_source_scope() is None
    assert stream.state is ConnectionState.ERROR
    assert set(provider._handles_by_symbol) == {"IF2612"}
    assert provider._fanout is not None
    assert set(provider._fanout._queues) == {"IF2612"}
    assert provider.unsubscribe(first) is True
    assert client.stop_calls == 1
    assert provider.diagnostics["active_handles"] == 0


def test_ctp_market_provider_rejects_unbound_key_before_stream_factory() -> None:
    calls = 0

    def factory(_key: MarketSubscriptionKey, _queue: BoundedCtpQuoteV2Queue) -> object:
        nonlocal calls
        calls += 1
        return object()

    provider = CtpQuoteV2MarketProvider(
        stream_factory=factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
    )
    invalid_key = _key(channel="tickers")

    with pytest.raises(CtpQuoteV2MarketProviderError) as error:
        provider.subscribe(invalid_key, generation="hub-generation-r1")

    assert error.value.code == "UNSUPPORTED_CTP_MARKET_KEY"
    assert calls == 0


def test_ctp_market_provider_retains_unconfirmed_native_stop_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _SyntheticMdClient)
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        stop_timeout_seconds=1.0,
    )
    handle = provider.subscribe(_key(), generation="hub-generation-r1")
    (client,) = _SyntheticMdClient.instances
    client.stop_receipts.extend(
        (
            CtpNativeStopReceipt(
                connection_generation=1,
                join_required=False,
                join_completed=True,
                native_released=False,
                thread_alive=False,
                timed_out=False,
            ),
            CtpNativeStopReceipt(
                connection_generation=1,
                join_required=False,
                join_completed=True,
                native_released=True,
                thread_alive=False,
                timed_out=False,
            ),
        )
    )

    assert provider.unsubscribe(handle) is False
    assert handle.active is True
    assert provider.diagnostics["active_handles"] == 1
    assert provider.unsubscribe(handle) is True
    assert handle.active is False
    assert client.stop_calls == 1
    assert client.stop_wait_calls == 2
    assert provider.diagnostics["active_handles"] == 0


def test_ctp_market_provider_reconnect_requires_stop_ack_tick_and_hub_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    created: list[_SyntheticMdClient] = []

    def make_client(*args: object) -> _SyntheticMdClient:
        client = _SyntheticMdClient(*args)
        if created:
            client.reverse_subscription_acks = True
        created.append(client)
        return client

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", make_client)
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=1.0,
    )
    handle = provider.subscribe(_key(), generation="hub-generation-r1")
    second_key = _key(
        instrument=MarketInstrumentKey(
            provider="ctp",
            venue="CFFEX",
            environment="simnow",
            product_type="future",
            instrument_id="IH2612",
            native_symbol="IH2612",
        )
    )
    second_handle = provider.subscribe(second_key, generation="hub-generation-r1")
    old_client = created[0]
    old_scope = handle.source_scope
    assert provider.describe_source(
        handle,
        key=handle.key,
        generation=handle.generation,
        stream_id=handle.key.stream_id,
        epoch="hub-epoch-r1",
    ) is not None

    # This native observation is queued before disconnect and must be drained
    # with the old source epoch, never relabelled onto the replacement client.
    old_client.emit_tick(
        InstrumentID="IF2612",
        ExchangeID="CFFEX",
        LastPrice=4_002.0,
        Volume=2,
        TradingDay="20261020",
        ActionDay="20261020",
        UpdateTime="09:30:02",
        UpdateMillisec=0,
    )
    old_client.stop_receipts.extend(
        (
            CtpNativeStopReceipt(
                connection_generation=1,
                join_required=False,
                join_completed=True,
                native_released=False,
                thread_alive=False,
                timed_out=False,
            ),
            CtpNativeStopReceipt(
                connection_generation=1,
                join_required=False,
                join_completed=True,
                native_released=True,
                thread_alive=False,
                timed_out=False,
            ),
        )
    )
    old_client.emit_disconnect()

    assert provider.poll(handle, limit=1) == ()
    assert provider.diagnostics["source_recovery_active"] == 1
    recovery = provider._source_recovery
    assert recovery is not None
    assert recovery.stop_done.wait(timeout=1.0) is True
    assert old_client.stop_wait_calls == 2
    assert len(_SyntheticMdClient.instances) == 1

    # The owner consumes the exact complete receipt and starts one replacement
    # worker. Its synchronous startup completes off-poll; readiness does not
    # resume until a later owner poll consumes the result.
    assert provider.poll(handle, limit=1) == ()
    recovery = provider._source_recovery
    assert recovery is not None
    assert recovery.reconnect_done.wait(timeout=1.0) is True
    assert recovery.reconnect_worker is not None
    recovery.reconnect_worker.join(timeout=1.0)
    assert len(_SyntheticMdClient.instances) == 2
    new_client = created[1]
    assert new_client.subscribed == ["IF2612", "IH2612"]
    assert new_client.reverse_subscription_acks is True
    assert provider.poll(handle, limit=1) == ()
    assert provider.source_epoch_reset_required(handle) is False
    assert provider.poll(handle, limit=1) == ()
    assert provider.source_epoch_reset_required(handle) is True
    assert provider.source_epoch_reset_required(second_handle) is True
    new_scope = handle.source_scope
    assert new_scope is not old_scope
    assert new_scope.connection_generation > old_scope.connection_generation
    assert new_scope.clock_source_session_id != old_scope.clock_source_session_id
    assert provider.describe_source(
        handle,
        key=handle.key,
        generation=handle.generation,
        stream_id=handle.key.stream_id,
        epoch="hub-epoch-r1",
    ) is None
    assert provider.describe_source(
        second_handle,
        key=second_handle.key,
        generation=second_handle.generation,
        stream_id=second_handle.key.stream_id,
        epoch="hub-epoch-r1",
    ) is None

    # Hub reset/ack is an owner-thread barrier. Only after it accepts a new
    # epoch can a subsequent native tick become a public provider event.
    assert provider.acknowledge_source_epoch_reset(
        handle, epoch="hub-epoch-r2"
    ) is True
    assert provider.source_epoch_reset_required(handle) is False
    assert provider.source_epoch_reset_required(second_handle) is True
    assert provider.acknowledge_source_epoch_reset(
        second_handle, epoch="hub-epoch-r2"
    ) is True
    assert provider.source_epoch_reset_required(handle) is False
    old_client.emit_tick(
        InstrumentID="IF2612",
        ExchangeID="CFFEX",
        LastPrice=4_003.0,
        Volume=3,
        TradingDay="20261020",
        ActionDay="20261020",
        UpdateTime="09:30:03",
        UpdateMillisec=0,
    )
    assert handle.queue.drain(1) == ()
    new_client.emit_tick(
        InstrumentID="IF2612",
        ExchangeID="CFFEX",
        LastPrice=4_004.0,
        Volume=4,
        TradingDay="20261020",
        ActionDay="20261020",
        UpdateTime="09:30:04",
        UpdateMillisec=0,
    )
    (event,) = provider.poll(handle, limit=1)
    assert event.data["connection_generation"] == new_scope.connection_generation
    assert event.data["last_price"] == 4_004.0
    descriptor = provider.describe_source(
        handle,
        key=handle.key,
        generation=handle.generation,
        stream_id=handle.key.stream_id,
        epoch="hub-epoch-r2",
    )
    assert descriptor is not None
    assert descriptor.epoch == "hub-epoch-r2"
    assert descriptor.connection_generation == new_scope.connection_generation
    second_descriptor = provider.describe_source(
        second_handle,
        key=second_handle.key,
        generation=second_handle.generation,
        stream_id=second_handle.key.stream_id,
        epoch="hub-epoch-r2",
    )
    assert second_descriptor is not None
    assert second_descriptor.epoch == "hub-epoch-r2"
    assert second_descriptor.connection_generation == new_scope.connection_generation
    assert provider.close() is True


def test_ctp_market_provider_stop_worker_keeps_poll_nonblocking_until_exact_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    created: list[_SyntheticMdClient] = []

    def make_client(*args: object) -> _SyntheticMdClient:
        client = _SyntheticMdClient(*args)
        created.append(client)
        return client

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", make_client)
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=1.0,
    )
    handle = provider.subscribe(_key(), generation="hub-generation-r1")
    (old_client,) = created
    owner_thread_id = threading.get_ident()
    stop_entered = threading.Event()
    allow_stop_return = threading.Event()
    stop_thread_ids: list[int] = []
    exact_receipt = CtpNativeStopReceipt(
        connection_generation=old_client.connection_generation,
        join_required=False,
        join_completed=True,
        native_released=True,
        thread_alive=False,
        timed_out=False,
    )

    def blocked_stop_and_wait(*, timeout: float) -> CtpNativeStopReceipt:
        assert timeout == 0.0
        old_client.stop_wait_calls += 1
        stop_thread_ids.append(threading.get_ident())
        stop_entered.set()
        if not allow_stop_return.wait(timeout=3.0):
            raise TimeoutError("synthetic stop observer was not released")
        return exact_receipt

    old_client.stop_and_wait = blocked_stop_and_wait  # type: ignore[method-assign]
    try:
        old_client.emit_disconnect()
        started = time.monotonic()
        assert provider.poll(handle, limit=1) == ()
        assert time.monotonic() - started < 0.25
        assert stop_entered.wait(timeout=1.0) is True
        recovery = provider._source_recovery
        assert recovery is not None
        worker = recovery.stop_worker
        assert worker is not None
        assert recovery.old_client is old_client
        assert recovery.stop_done.is_set() is False
        assert recovery.stop_receipt is None
        assert stop_thread_ids == [worker.ident]
        assert worker.ident != owner_thread_id

        # Neither subsequent owner polls nor close wait on native stop or start
        # a replacement while the exact stop call remains blocked.
        for _ in range(3):
            started = time.monotonic()
            assert provider.poll(handle, limit=1) == ()
            assert time.monotonic() - started < 0.25
        assert provider.close() is False
        assert len(created) == 1
        assert old_client.stop_wait_calls == 1
        assert recovery.stop_worker is worker
        assert recovery.stop_done.is_set() is False

        allow_stop_return.set()
        assert recovery.stop_done.wait(timeout=1.0) is True
        assert recovery.stop_receipt is exact_receipt
        assert len(created) == 1  # the worker cannot reconnect by itself

        assert provider.poll(handle, limit=1) == ()
        reconnect_worker = recovery.reconnect_worker
        assert reconnect_worker is not None
        assert recovery.reconnect_done.wait(timeout=1.0) is True
        reconnect_worker.join(timeout=1.0)
        assert len(created) == 2  # only the owner starts the reconnect worker
        assert reconnect_worker.ident != owner_thread_id
        assert provider.poll(handle, limit=1) == ()  # owner consumes worker result
        assert provider.poll(handle, limit=1) == ()
        assert provider.source_epoch_reset_required(handle) is True
        assert provider.acknowledge_source_epoch_reset(
            handle, epoch="hub-epoch-r2"
        ) is True
        assert provider.close() is True
    finally:
        allow_stop_return.set()
        recovery = provider._source_recovery
        if recovery is not None and recovery.stop_worker is not None:
            recovery.stop_worker.join(timeout=1.0)


def test_ctp_market_provider_reconnect_startup_worker_times_out_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    created: list[_SyntheticMdClient] = []
    startup_entered = threading.Event()
    allow_startup = threading.Event()

    def make_client(*args: object) -> _SyntheticMdClient:
        client = _SyntheticMdClient(*args)
        if created:
            client.start_entered = startup_entered
            client.allow_start = allow_startup
        created.append(client)
        return client

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", make_client)
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=0.15,
    )
    handle = provider.subscribe(_key(), generation="hub-generation-r1")
    (old_client,) = created
    exact_receipt = CtpNativeStopReceipt(
        connection_generation=old_client.connection_generation,
        join_required=False,
        join_completed=True,
        native_released=True,
        thread_alive=False,
        timed_out=False,
    )
    old_client.stop_receipts.append(exact_receipt)
    owner_thread_id = threading.get_ident()
    old_client.emit_disconnect()

    try:
        assert provider.poll(handle, limit=1) == ()
        recovery = provider._source_recovery
        assert recovery is not None
        assert recovery.stop_done.wait(timeout=1.0) is True

        # The exact old-client receipt is consumed before the one reconnect
        # worker begins native startup. The owner poll only launches it.
        started = time.monotonic()
        assert provider.poll(handle, limit=1) == ()
        assert time.monotonic() - started < 0.25
        assert startup_entered.wait(timeout=1.0) is True
        assert created[0] is old_client
        new_client = created[1]
        worker = recovery.reconnect_worker
        assert worker is not None
        assert worker.ident != owner_thread_id
        assert recovery.stop_receipt is exact_receipt
        assert recovery.reconnect_done.is_set() is False
        assert recovery.reconnected_client is None
        assert new_client.start_thread_id == worker.ident

        # CtpMarketStream.connect holds its callback lock across start().
        # Repeated owner polls and close must remain bounded while that native
        # startup call is blocked, and neither may reset an epoch or open a
        # second replacement client.
        release_timer = threading.Timer(0.5, allow_startup.set)
        release_timer.start()
        for _ in range(3):
            started = time.monotonic()
            assert provider.poll(handle, limit=1) == ()
            assert time.monotonic() - started < 0.25
        started = time.monotonic()
        assert provider.close() is False
        assert time.monotonic() - started < 0.25
        assert provider.source_epoch_reset_required(handle) is False
        assert len(created) == 2
        assert recovery.reconnect_worker is worker
        assert recovery.reconnect_done.is_set() is False

        # Once the bounded recovery deadline expires, owner polling must still
        # return while native startup owns the stream lock. It fences provider
        # publication and retains custody until the exact worker exits.
        time.sleep(0.16)
        started = time.monotonic()
        assert provider.poll(handle, limit=1) == ()
        assert time.monotonic() - started < 0.25
        assert recovery.state == "failed"
        assert recovery.reconnect_cancelled.is_set() is True
        assert provider.source_epoch_reset_required(handle) is False
        started = time.monotonic()
        assert provider.close() is False
        assert time.monotonic() - started < 0.25

        allow_startup.set()
        release_timer.cancel()
        assert recovery.reconnect_done.wait(timeout=1.0) is True
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert recovery.reconnect_succeeded is True
        assert recovery.reconnect_error is None
        assert recovery.reconnected_client is new_client
        assert handle.stream.quote_v2_service_fail_closed is True
        assert handle.stream.get_quote_v2_service_ready_scope() is None
        assert provider.source_epoch_reset_required(handle) is False
        assert provider.poll(handle, limit=1) == ()
        assert provider.close() is True
    finally:
        allow_startup.set()
        if "release_timer" in locals():
            release_timer.cancel()
        recovery = provider._source_recovery
        if recovery is not None:
            if recovery.stop_worker is not None:
                recovery.stop_worker.join(timeout=1.0)
            if recovery.reconnect_worker is not None:
                recovery.reconnect_worker.join(timeout=1.0)


def test_ctp_market_provider_closes_constructor_failed_reconnect_from_exact_old_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    created: list[_SyntheticMdClient] = []
    constructor_attempts = 0

    def make_client(*args: object) -> _SyntheticMdClient:
        nonlocal constructor_attempts
        constructor_attempts += 1
        if constructor_attempts > 1:
            raise RuntimeError("synthetic replacement constructor failure")
        client = _SyntheticMdClient(*args)
        created.append(client)
        return client

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", make_client)
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=1.0,
    )
    handle = provider.subscribe(_key(), generation="hub-generation-r1")
    (old_client,) = created
    exact_receipt = CtpNativeStopReceipt(
        connection_generation=old_client.connection_generation,
        join_required=False,
        join_completed=True,
        native_released=True,
        thread_alive=False,
        timed_out=False,
    )
    old_client.stop_receipts.append(exact_receipt)
    old_client.emit_disconnect()

    assert provider.poll(handle, limit=1) == ()
    recovery = provider._source_recovery
    assert recovery is not None
    assert recovery.stop_done.wait(timeout=1.0) is True
    assert recovery.stop_receipt is exact_receipt

    assert provider.poll(handle, limit=1) == ()
    worker = recovery.reconnect_worker
    assert worker is not None
    assert recovery.reconnect_done.wait(timeout=1.0) is True
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert constructor_attempts == 2
    assert created == [old_client]
    assert recovery.reconnect_succeeded is False
    assert recovery.reconnected_client is None

    stream = handle.stream
    assert stream._md_client is None
    assert stream.state is live_ctp_feed.ConnectionState.ERROR
    assert stream._quote_v2_service_recovery_stopped_client is old_client
    assert stream._quote_v2_service_recovery_stopped_generation == exact_receipt.connection_generation
    assert old_client.stop_wait_calls == 1

    assert provider.poll(handle, limit=1) == ()
    assert recovery.state == "failed"
    assert provider.close() is True
    assert stream.state is live_ctp_feed.ConnectionState.DISCONNECTED
    assert stream._running is False
    assert stream._md_client is None
    assert old_client.stop_calls == 0
    assert old_client.stop_wait_calls == 1
    assert constructor_attempts == 2


def test_ctp_market_provider_recovery_without_fresh_ack_tick_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    original_factory = _SyntheticMdClient
    created: list[_SyntheticMdClient] = []

    def make_client(*args: object) -> _SyntheticMdClient:
        client = original_factory(*args)
        if created:
            client.emit_startup_readiness_tick = False
            client.emit_tick_before_startup_acks = True
        created.append(client)
        return client

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", make_client)
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=0.05,
    )
    handle = provider.subscribe(_key(), generation="hub-generation-r1")
    old_client = created[0]
    old_client.emit_disconnect()
    assert provider.poll(handle, limit=1) == ()
    recovery = provider._source_recovery
    assert recovery is not None
    assert recovery.stop_done.wait(timeout=1.0) is True
    assert provider.poll(handle, limit=1) == ()
    assert recovery.reconnect_done.wait(timeout=1.0) is True
    assert recovery.reconnect_worker is not None
    recovery.reconnect_worker.join(timeout=1.0)
    assert provider.poll(handle, limit=1) == ()
    new_client = created[1]
    assert provider.poll(handle, limit=1) == ()
    assert handle.stream.get_quote_v2_service_ready_scope() is None
    # The reconnect fake sent a tick only after all ACKs; with that fresh tick
    # deliberately omitted, no new scope or Hub reset is authorized.
    assert new_client.subscribed == ["IF2612"]
    assert provider.source_epoch_reset_required(handle) is False
    time.sleep(0.06)
    assert provider.poll(handle, limit=1) == ()
    assert handle.stream.quote_v2_service_fail_closed is True
    assert handle.stream.get_quote_v2_service_ready_scope() is None
    assert provider.describe_source(
        handle,
        key=handle.key,
        generation=handle.generation,
        stream_id=handle.key.stream_id,
        epoch="hub-epoch-r1",
    ) is None
    assert provider.close() is True


def test_ctp_market_provider_close_retries_failed_start_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SyntheticMdClient.instances.clear()
    _SyntheticMdClient.authenticate = False
    _SyntheticMdClient.stop_receipts_on_create = [
        CtpNativeStopReceipt(
            connection_generation=1,
            join_required=False,
            join_completed=True,
            native_released=False,
            thread_alive=False,
            timed_out=False,
        )
    ]
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _SyntheticMdClient)
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=0.05,
        stop_timeout_seconds=2.0,
    )
    try:
        with pytest.raises(CtpQuoteV2MarketProviderError) as error:
            provider.subscribe(_key(), generation="hub-generation-r1")
        assert error.value.code == "CTP_MARKET_AUTH_TIMEOUT"
        assert provider.diagnostics["pending_start_cleanup"] == 1
        (client,) = _SyntheticMdClient.instances
        client.stop_receipts.append(
            CtpNativeStopReceipt(
                connection_generation=1,
                join_required=False,
                join_completed=True,
                native_released=True,
                thread_alive=False,
                timed_out=False,
            )
        )
        assert provider.close() is True
        assert provider.diagnostics["pending_start_cleanup"] == 0
        assert client.stop_wait_calls == 2
    finally:
        _SyntheticMdClient.authenticate = True
        _SyntheticMdClient.stop_receipts_on_create = []


@pytest.mark.parametrize(
    ("start_failure_type", "cleanup_failure_type"),
    (
        (KeyboardInterrupt, SystemExit),
        (SystemExit, KeyboardInterrupt),
    ),
)
def test_ctp_market_provider_retains_stream_when_start_and_cleanup_are_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    start_failure_type: type[BaseException],
    cleanup_failure_type: type[BaseException],
) -> None:
    _SyntheticMdClient.instances.clear()
    _SyntheticMdClient.authenticate = True
    _SyntheticMdClient.stop_receipts_on_create = [
        CtpNativeStopReceipt(
            connection_generation=1,
            join_required=False,
            join_completed=True,
            native_released=False,
            thread_alive=False,
            timed_out=False,
        )
    ]
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _SyntheticMdClient)
    start_failure = start_failure_type("native startup interrupted")
    cleanup_failure = cleanup_failure_type("native cleanup interrupted")
    start_attempts: list[CtpMarketStream] = []
    cleanup_calls = 0
    original_stop = CtpMarketStream.stop

    def factory(
        key: MarketSubscriptionKey, queue: BoundedCtpQuoteV2Queue
    ) -> CtpMarketStream:
        stream = _stream_factory(key, queue)
        start_attempts.append(stream)
        return stream

    def start_then_interrupt(stream: CtpMarketStream) -> None:
        # Run the real CTP connect binding and login callback first, then
        # interrupt the caller after a native client has been installed.
        stream.connect()
        raise start_failure

    def cleanup_interrupts_once(stream: CtpMarketStream) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise cleanup_failure
        original_stop(stream)

    monkeypatch.setattr(CtpMarketStream, "start", start_then_interrupt)
    monkeypatch.setattr(CtpMarketStream, "stop", cleanup_interrupts_once)
    provider = CtpQuoteV2MarketProvider(
        stream_factory=factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
    )

    try:
        with pytest.raises(start_failure_type) as error:
            provider.subscribe(_key(), generation="hub-generation-interrupted")

        assert error.value is start_failure
        (stream,) = start_attempts
        (client,) = _SyntheticMdClient.instances
        (pending,) = provider._pending_start_cleanup
        assert pending.stream is stream
        assert pending.native_client is client
        assert pending.native_stop_confirmed is False
        assert provider.diagnostics["pending_start_cleanup"] == 1

        # A later owner-thread close retries the exact retained stream.  The
        # first native receipt remains uncertain; only the second confirms it.
        assert provider.close() is False
        (pending,) = provider._pending_start_cleanup
        assert pending.stream is stream
        assert pending.native_client is client
        assert client.stop_wait_calls == 1

        client.stop_receipts.append(
            CtpNativeStopReceipt(
                connection_generation=1,
                join_required=False,
                join_completed=True,
                native_released=True,
                thread_alive=False,
                timed_out=False,
            )
        )
        assert provider.close() is True
        assert provider.diagnostics["pending_start_cleanup"] == 0
        assert client.stop_wait_calls == 2
    finally:
        _SyntheticMdClient.authenticate = True
        _SyntheticMdClient.stop_receipts_on_create = []


def test_ctp_native_callback_queue_is_bounded_and_nonblocking() -> None:
    callback_queue = BoundedCtpQuoteV2Queue(capacity=2)
    callback_queue.put("first", block=True, timeout=0)
    callback_queue.put("second", block=True, timeout=0)
    callback_queue.put("third", block=True, timeout=0)

    assert callback_queue.dropped_oldest == 1
    assert callback_queue.drain(2) == ("second", "third")
    assert callback_queue.size == 0
