"""Focused tests for the native CTP quote-v2 ServiceMarket adapter."""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from bt_api_base.market_protocol import MarketInstrumentKey, MarketSubscriptionKey
from bt_api_base.market_tick_protocol import MARKET_TICK_SCHEMA, MARKET_TICK_SCHEMA_VERSION

from bt_api_ctp.ctp.client import CtpNativeStopReceipt
from bt_api_ctp.feeds import live_ctp_feed
from bt_api_ctp.feeds.live_ctp_feed import ConnectionState, CtpMarketStream
from bt_api_ctp import market_provider as market_provider_module
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
        # MarketSubscriptionKey uses the neutral channel; ctp.quote.v2 is
        # the payload schema version, not a subscription channel.
        "channel": "tick",
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

    def start(self, *, block: bool = False, join_observer: bool = True) -> None:
        assert block is False
        assert join_observer is False  # a stop must be provable (no Join observer)
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
        # The first post-ACK tick establishes readiness and, since D62-38
        # (iteration 62), is published like every later tick.
        assert queue.size == 1

        _emit_synthetic_tick(client, "IH2612", 3)
        assert queue.size == 2
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

    # The tick that established readiness is published first (D62-38).
    readiness, event = provider.poll(handle, limit=8)
    assert readiness.data["clock_source_session_id"] == scope.clock_source_session_id
    assert readiness.data["provider_sequence"] < event.data["provider_sequence"]
    assert event.generation == "hub-generation-r1"
    assert event.observed_at_utc.endswith("Z")
    assert event.data["schema"] == MARKET_TICK_SCHEMA
    assert event.data["schema_version"] == MARKET_TICK_SCHEMA_VERSION
    assert event.data["instrument"] == key.instrument.to_dict()
    assert event.data["provider_source"] == "ctp.native.md"
    assert event.data["clock_domain_id"] == scope.clock_domain_id
    assert event.data["clock_source_session_id"] == scope.clock_source_session_id
    assert event.data["clock_source"] == scope.clock_source
    assert event.data["source_clock_quality"] == "unknown"
    assert event.data["receive_clock_quality"] == "unknown"
    assert event.data["source_clock_error_ms"] is None
    assert event.data["receive_clock_error_ms"] is None
    assert event.data["quality_contract_ref"] is None
    assert event.data["freshness_status"] == "unavailable"
    assert event.data["execution_eligible"] is False
    assert event.data["last_price"] == 4_001.0
    assert event.data["bid_price"] == 4_000.0
    assert event.data["ask_price"] == 4_002.0
    assert event.data["received_at_utc"] == event.observed_at_utc
    assert event.data["provider_event_time_utc"].endswith("Z")
    assert event.data["trading_date"] == "2026-10-17"
    assert event.data["event_session_date"] == "2026-10-17"

    assert provider.unsubscribe(handle) is True
    assert client.stop_calls == 1
    assert provider.diagnostics["active_handles"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("rules_hash", None), ("rules_hash", "unattested"), ("source", "other")],
)
def test_ctp_market_provider_rejects_unbound_receipt_facts(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    _SyntheticMdClient.instances.clear()
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _SyntheticMdClient)
    project = market_provider_module.project_ctp_market_tick

    def project_with_tampered_receipt(
        item: object, key: object, scope: object, receipt: dict
    ):
        tampered = dict(receipt)
        tampered[field] = value
        return project(item, key, scope, tampered)

    monkeypatch.setattr(
        market_provider_module, "project_ctp_market_tick", project_with_tampered_receipt
    )
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=1.0,
    )
    handle = provider.subscribe(_key(), generation="hub-generation-r1")
    try:
        (client,) = _SyntheticMdClient.instances
        _emit_synthetic_tick(client, "IF2612", 2)
        with pytest.raises(CtpQuoteV2MarketProviderError) as error:
            provider.poll(handle, limit=1)
        assert error.value.code == "CTP_MARKET_EVENT_INVALID"
    finally:
        assert provider.unsubscribe(handle) is True


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
    # The first key also delivers the tick that made the session ready (D62-38).
    _readiness, first_event = provider.poll(first, limit=8)
    (second_event,) = provider.poll(second, limit=8)
    assert first_event.data["instrument"]["native_symbol"] == "IF2612"
    assert second_event.data["instrument"]["native_symbol"] == "IH2612"
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
    # per-key replay path while later keys are still being installed.  Its
    # queue also holds the published readiness tick (D62-38).
    assert first.queue.size == 1
    emit("IF2612", 1)
    assert first.queue.size == 2
    assert first.stream._quote_v2_subscription_metadata["IF2612"]["subscription_epoch"] == 1

    second = provider.subscribe(keys[1], generation="hub-generation-b")
    emit("IH2612", 2)
    assert first.queue.size == 2
    assert second.queue.size == 1

    third = provider.subscribe(keys[2], generation="hub-generation-c")
    assert first.queue.size == 2
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
    assert [event.data["instrument"]["native_symbol"] for event in first_events] == [
        "IF2612", "IF2612", "IF2612"
    ]
    assert [event.data["instrument"]["native_symbol"] for event in second_events] == [
        "IH2612", "IH2612"
    ]
    assert [event.data["instrument"]["native_symbol"] for event in third_events] == ["IC2612"]
    assert {
        event.data["provider_subscription_epoch"]
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
        # The stream loop can sleep for one second before observing stop;
        # allow scheduling time without weakening incomplete-receipt checks.
        stop_timeout_seconds=3.0,
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
    assert active_event.data["instrument"]["native_symbol"] == "IH2612"

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
    # Only the fresh session's own readiness tick is queued (D62-38).
    assert fresh.queue.size == old_queue_size == 1
    (readiness,) = provider.poll(fresh, limit=8)
    assert (
        readiness.data["clock_source_session_id"]
        == fresh.source_scope.clock_source_session_id
    )
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
        # Allow the one-second stream-loop sleep and thread scheduling to
        # finish; the incomplete native receipt still forces the first failure.
        stop_timeout_seconds=3.0,
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
    assert event.data["provider_connection_generation"] == new_scope.connection_generation
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
        # Iteration 62, defect 25: a start past its deadline is not final.  The
        # attempt is abandoned (cancelled, publication revoked) and the next
        # one begins only after this worker has returned.
        assert recovery.state == "abandoning"
        assert provider.source_recovery_state(handle) == "recovering"
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
        assert recovery.reconnect_fenced is True
        assert handle.stream.quote_v2_service_fail_closed is True
        assert handle.stream.get_quote_v2_service_ready_scope() is None
        assert provider.source_epoch_reset_required(handle) is False
        # The late client stays fenced; the next attempt stops it first.
        assert provider.poll(handle, limit=1) == ()
        retry = provider._source_recovery
        assert retry is not recovery and retry.attempt == 2
        assert retry.old_client is new_client and retry.state == "stopping"
        assert provider.poll(handle, limit=1) == ()  # starts the stop observer
        assert retry.stop_done.wait(timeout=1.0) is True
        assert provider.poll(handle, limit=1) == ()
        assert retry.state == "retry_wait"
        assert handle.stream._md_client is None
        assert new_client.stop_wait_calls >= 1
        assert len(created) == 2
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
    # Iteration 62, defect 25: a replacement that could not be constructed is
    # retried from the same confirmed old-client stop (not observed again).
    retry = provider._source_recovery
    assert retry is not recovery and retry.attempt == 2
    assert retry.state == "retry_wait" and retry.old_client is old_client
    assert retry.stop_receipt is exact_receipt
    assert provider.source_recovery_state(handle) == "recovering"
    assert provider.close() is True
    assert stream.state is live_ctp_feed.ConnectionState.DISCONNECTED
    assert stream._running is False
    assert stream._md_client is None
    assert old_client.stop_calls == 0
    assert old_client.stop_wait_calls == 1
    assert constructor_attempts == 2


def test_ctp_market_provider_recovery_without_fresh_ack_tick_publishes_nothing_while_waiting(
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
    # Iteration 62 continuity-7: the recovery deadline bounds only the native
    # stop and start.  A started replacement that is not yet ticking (a front
    # can stay closed for hours between sessions) is awaited, but nothing is
    # authorized meanwhile: no scope, no reset, no event, no descriptor.
    assert provider._source_recovery is recovery
    assert recovery.state == "connecting"
    assert handle.stream.quote_v2_service_fail_closed is False
    assert provider.source_epoch_reset_required(handle) is False
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


def _added_key() -> MarketSubscriptionKey:
    return _key(
        instrument=MarketInstrumentKey(
            provider="ctp",
            venue="CFFEX",
            environment="simnow",
            product_type="future",
            instrument_id="IH2612",
            native_symbol="IH2612",
        )
    )


class _AsyncAckMdClient(_SyntheticMdClient):
    """A real front acknowledges an added subscription later, on its own thread."""

    ack_delay_seconds: float | None = 0.15
    ack_error_id = 0

    def subscribe_additional(self, instruments: list[str]) -> tuple[str, ...]:
        self.subscribe_additional_calls += 1
        additions = tuple(item for item in instruments if item not in self.subscribed)
        self.subscribed.extend(additions)
        if additions and self.ack_delay_seconds is not None:
            request_id = self.next_request_id
            self.next_request_id += 1
            timer = threading.Timer(
                self.ack_delay_seconds,
                self.emit_subscription_batch,
                args=(additions,),
                kwargs={"request_id": request_id, "error_id": self.ack_error_id},
            )
            timer.daemon = True
            timer.start()
        return additions


def _async_ack_provider(
    monkeypatch: pytest.MonkeyPatch, *, delay: float | None, error_id: int = 0, timeout: float = 2.0
) -> CtpQuoteV2MarketProvider:
    _SyntheticMdClient.instances.clear()
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _AsyncAckMdClient)
    monkeypatch.setattr(_AsyncAckMdClient, "ack_delay_seconds", delay)
    monkeypatch.setattr(_AsyncAckMdClient, "ack_error_id", error_id)
    return CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=timeout,
    )


def test_added_key_is_acquired_only_after_the_asynchronous_venue_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Iteration 62 (SimNow 2026-11-06): the Gateway Hub describes an added key's
    source in the same subscribe call; a real front acknowledges it later."""

    provider = _async_ack_provider(monkeypatch, delay=0.15)
    first_key, added_key = _key(), _added_key()
    first = provider.subscribe(first_key, generation="hub-generation-a")

    started = time.monotonic()
    added = provider.subscribe(added_key, generation="hub-generation-b")
    assert time.monotonic() - started >= 0.1  # returned after the acknowledgement

    descriptor = provider.describe_source(
        added,
        key=added_key,
        generation="hub-generation-b",
        stream_id=added_key.stream_id,
        epoch="hub-epoch-b",
    )
    assert descriptor is not None
    assert descriptor.key == added_key
    assert (
        provider.describe_source(
            first,
            key=first_key,
            generation="hub-generation-a",
            stream_id=first_key.stream_id,
            epoch="hub-epoch-a",
        )
        is not None
    )
    assert first.stream.quote_v2_service_fail_closed is False
    assert provider.unsubscribe(added) is True
    assert provider.unsubscribe(first) is True
    assert provider.diagnostics["active_sessions"] == 0


@pytest.mark.parametrize(
    ("delay", "error_id"),
    [(None, 0), (0.05, 16)],  # no acknowledgement at all / the venue rejects the add
)
def test_unacknowledged_added_key_fails_the_shared_session_closed(
    monkeypatch: pytest.MonkeyPatch, delay: float | None, error_id: int
) -> None:
    provider = _async_ack_provider(monkeypatch, delay=delay, error_id=error_id, timeout=0.4)
    first = provider.subscribe(_key(), generation="hub-generation-a")
    stream = first.stream

    started = time.monotonic()
    with pytest.raises(CtpQuoteV2MarketProviderError) as error:
        provider.subscribe(_added_key(), generation="hub-generation-b")
    elapsed = time.monotonic() - started

    assert error.value.code == "CTP_MARKET_SUBSCRIBE_UNACKNOWLEDGED"
    if delay is not None:
        assert elapsed < 0.35  # a rejection fails promptly, not at the timeout
    # The native add was sent: its outcome is ambiguous, so the shared session
    # is fenced and only the existing handle remains, as cleanup authority.
    assert stream._ambiguous_quote_v2_subscriptions == {"IH2612"}
    assert stream.get_quote_clock_source_scope() is None
    assert stream.state is ConnectionState.ERROR
    assert set(provider._handles_by_symbol) == {"IF2612"}
    assert provider.unsubscribe(first) is True
    assert provider.diagnostics["active_handles"] == 0


# ---------------------------------------------------------------------------
# D62-38 (iteration 62): continuity means the current scope with no locally
# known loss; volume accounting is bar-only.  Evidence level: UNIT/FAKE (the
# synthetic MdClient drives the real CtpMarketStream callbacks).

_CTP_TZ = timezone(timedelta(hours=8))


class _LiveBookMdClient(_SyntheticMdClient):
    """Every tick carries a full valid book stamped with the current CTP time."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.trading_day: str | None = None

    def emit_tick(self, **fields: object) -> None:
        now = datetime.now(_CTP_TZ)
        tick: dict[str, object] = {
            "ExchangeID": "CFFEX",
            "LastPrice": 4_001.0,
            "BidPrice1": 4_000.0,
            "BidVolume1": 2,
            "AskPrice1": 4_002.0,
            "AskVolume1": 3,
            "OpenInterest": 1_000.0,
            "Turnover": 1.0,
            "UpperLimitPrice": 4_200.0,
            "LowerLimitPrice": 3_800.0,
        }
        tick.update(fields)
        tick.update(
            TradingDay=self.trading_day or now.strftime("%Y%m%d"),
            ActionDay=now.strftime("%Y%m%d"),
            UpdateTime=now.strftime("%H:%M:%S"),
            UpdateMillisec=now.microsecond // 1000,
        )
        super().emit_tick(**tick)


def _live_provider(
    monkeypatch: pytest.MonkeyPatch, *, queue_capacity: int = 1_024
) -> tuple[CtpQuoteV2MarketProvider, object, _LiveBookMdClient]:
    _SyntheticMdClient.instances.clear()
    _SyntheticMdClient.authenticate = True
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _LiveBookMdClient)
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        queue_capacity=queue_capacity,
        connect_timeout_seconds=1.0,
    )
    handle = provider.subscribe(_key(), generation="hub-generation-c1")
    (client,) = _SyntheticMdClient.instances
    return provider, handle, client


def _tick_facts(event: object) -> tuple[object, ...]:
    data = event.data
    return (
        data["delta_volume"],
        data["volume_complete"],
        data["volume_quality"],
        data["continuity_status"],
    )


def test_the_readiness_tick_is_published_as_a_continuous_volume_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The startup tick (Volume=1) proves the source flows; it is no longer
    # discarded but published first, tradable for its price if fresh.
    provider, handle, client = _live_provider(monkeypatch)
    client.emit_tick(InstrumentID="IF2612", Volume=6)
    readiness, counted = provider.poll(handle, limit=8)
    assert readiness.data["cumulative_volume"] == 1.0
    assert _tick_facts(readiness) == (0.0, False, "baseline", "continuous")
    assert readiness.data["quality_flags"] == []
    assert readiness.data["execution_eligible"] is False  # the SDK qualifies later
    assert _tick_facts(counted) == (5.0, True, "continuous", "continuous")
    assert provider.close() is True


def test_a_trading_day_switch_starts_a_continuous_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, handle, client = _live_provider(monkeypatch)
    client.emit_tick(InstrumentID="IF2612", Volume=900)
    client.trading_day = "20991231"  # the next trading day on the same session
    client.emit_tick(InstrumentID="IF2612", Volume=4)
    client.emit_tick(InstrumentID="IF2612", Volume=9)
    _readiness, _old_day, new_day, resumed = provider.poll(handle, limit=8)
    assert new_day.data["trading_date"] == "2099-12-31"
    assert _tick_facts(new_day) == (0.0, False, "baseline", "continuous")
    assert new_day.data["quality_flags"] == []
    assert _tick_facts(resumed) == (5.0, True, "continuous", "continuous")
    assert provider.close() is True


def test_a_volume_decrease_is_continuous_with_only_the_volume_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, handle, client = _live_provider(monkeypatch)
    client.emit_tick(InstrumentID="IF2612", Volume=50)
    client.emit_tick(InstrumentID="IF2612", Volume=40)
    _readiness, _counted, reset = provider.poll(handle, limit=8)
    assert _tick_facts(reset) == (0.0, False, "discontinuous", "continuous")
    assert reset.data["quality_flags"] == ["VOLUME_DISCONTINUITY"]
    assert provider.close() is True


@pytest.mark.parametrize(
    ("book", "flag", "absent"),
    [
        ({"LastPrice": 4_200.0, "BidPrice1": 4_200.0, "BidVolume1": 80,
          "AskPrice1": sys.float_info.max, "AskVolume1": 0}, "LIMIT_LOCKED_UP", "ask_price"),
        ({"LastPrice": 3_800.0, "AskPrice1": 3_800.0, "AskVolume1": 80,
          "BidPrice1": sys.float_info.max, "BidVolume1": 0}, "LIMIT_LOCKED_DOWN", "bid_price"),
    ],
)
def test_a_limit_locked_one_sided_book_is_a_valid_continuous_quote(
    monkeypatch: pytest.MonkeyPatch, book: dict[str, float], flag: str, absent: str
) -> None:
    provider, handle, client = _live_provider(monkeypatch)
    client.emit_tick(InstrumentID="IF2612", Volume=7, **book)
    _readiness, locked = provider.poll(handle, limit=8)
    assert _tick_facts(locked) == (6.0, True, "continuous", "continuous")
    assert locked.data["quality_flags"] == sorted([flag, "ZERO_DEPTH"])
    # The empty side is absent, never CTP's DBL_MAX sentinel.
    assert locked.data[absent] is None
    assert locked.data["upper_limit"] == 4_200.0 and locked.data["lower_limit"] == 3_800.0
    assert provider.close() is True


def test_quote_warnings_are_not_reported_as_provider_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, handle, client = _live_provider(monkeypatch)
    client.emit_tick(InstrumentID="IF2612", Volume=2, BidPrice1=4_001.0, AskPrice1=4_001.0)
    client.emit_tick(InstrumentID="IF2612", Volume=3, AskVolume1=0)
    _readiness, locked, thin = provider.poll(handle, limit=8)
    assert locked.data["quality_flags"] == ["LOCKED_BOOK"]
    assert thin.data["quality_flags"] == ["ZERO_DEPTH"]
    assert {locked.data["continuity_status"], thin.data["continuity_status"]} == {"continuous"}
    assert provider.close() is True


def test_a_genuinely_invalid_quote_stays_a_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, handle, client = _live_provider(monkeypatch)
    client.emit_tick(InstrumentID="IF2612", Volume=5, BidPrice1=4_003.0, AskPrice1=4_002.0)
    client.emit_tick(InstrumentID="IF2612", Volume=6, AskPrice1=sys.float_info.max, AskVolume1=0)
    client.emit_tick(InstrumentID="IF2612", Volume=8)
    _readiness, crossed, one_sided, after = provider.poll(handle, limit=8)
    for invalid in (crossed, one_sided):
        assert _tick_facts(invalid) == (0.0, False, "unavailable", "gap")
        assert "PROVIDER_QUALITY_FAILURE" in invalid.data["quality_flags"]
        assert not {"LIMIT_LOCKED_UP", "LIMIT_LOCKED_DOWN"} & set(invalid.data["quality_flags"])
    assert one_sided.data["ask_price"] is None  # not at the limit: still invalid
    assert _tick_facts(after) == (7.0, True, "continuous", "continuous")
    assert provider.close() is True


def test_a_dropped_queue_entry_makes_the_next_drained_tick_a_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, handle, client = _live_provider(monkeypatch, queue_capacity=2)
    for volume in (10, 20, 30, 40):  # readiness + 4 ticks into 2 slots: 3 dropped
        client.emit_tick(InstrumentID="IF2612", Volume=volume)
    after_loss, next_tick = provider.poll(handle, limit=8)
    assert after_loss.data["cumulative_volume"] == 30.0
    # Its own delta is kept, but the dropped rows' volume is missing.
    assert _tick_facts(after_loss) == (10.0, False, "discontinuous", "gap")
    assert _tick_facts(next_tick) == (10.0, True, "continuous", "continuous")
    assert provider.diagnostics["dropped_oldest"] == 3
    client.emit_tick(InstrumentID="IF2612", Volume=41)
    (resumed,) = provider.poll(handle, limit=8)
    assert _tick_facts(resumed) == (1.0, True, "continuous", "continuous")
    assert provider.close() is True


def test_a_queue_loss_is_reported_once_and_cleared_with_the_queue() -> None:
    queue = BoundedCtpQuoteV2Queue(capacity=1)
    queue.put("a")
    queue.put("b")
    assert queue.drain_marking_loss(4) == (("b",), True)
    assert queue.drain_marking_loss(4) == ((), False)
    queue.put("c")
    assert queue.drain_marking_loss(4) == (("c",), False)
    queue.put("d")
    queue.put("e")
    queue.clear()  # a recovery discards the scope with its pending loss
    queue.put("f")
    assert queue.drain_marking_loss(4) == (("f",), False)


def _finish_recovery(provider: CtpQuoteV2MarketProvider, handle: object, epoch: str) -> None:
    """Drive the owner polls until the replacement source needs a Hub epoch."""

    deadline = time.monotonic() + 3.0
    while not provider.source_epoch_reset_required(handle):
        assert provider.poll(handle, limit=8) == ()  # nothing published meanwhile
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert provider.acknowledge_source_epoch_reset(handle, epoch=epoch) is True


def test_a_disconnect_still_publishes_nothing_until_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, handle, client = _live_provider(monkeypatch)
    client.emit_tick(InstrumentID="IF2612", Volume=2)
    assert len(provider.poll(handle, limit=8)) == 2
    client.emit_disconnect()
    client.emit_tick(InstrumentID="IF2612", Volume=3)  # late callback, old session
    assert provider.poll(handle, limit=8) == ()
    assert provider.diagnostics["source_recovery_active"] == 1
    assert handle.queue.size == 0
    _finish_recovery(provider, handle, "hub-epoch-c2")
    events = provider.poll(handle, limit=8)
    assert events and all(
        event.data["provider_connection_generation"] == handle.source_scope.connection_generation
        for event in events
    )
    assert 3.0 not in {event.data["cumulative_volume"] for event in events}
    assert provider.close() is True


def test_the_first_tick_after_a_reconnect_is_a_continuous_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, handle, client = _live_provider(monkeypatch)
    client.emit_tick(InstrumentID="IF2612", Volume=2)
    assert len(provider.poll(handle, limit=8)) == 2
    old_scope = handle.source_scope
    client.emit_disconnect()
    _finish_recovery(provider, handle, "hub-epoch-c2")
    # The replacement session's readiness tick (Volume=1) is published first.
    readiness, *_ = provider.poll(handle, limit=8)
    assert handle.source_scope is not old_scope
    assert readiness.data["provider_connection_generation"] > old_scope.connection_generation
    assert readiness.data["clock_source_session_id"] == handle.source_scope.clock_source_session_id
    assert _tick_facts(readiness) == (0.0, False, "baseline", "continuous")
    assert provider.close() is True


def test_sdk_qualification_trades_the_fresh_first_tick_but_never_a_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unchanged SDK rule (FRESH and continuous) now implements D62-38."""

    clock = pytest.importorskip("bt_api_py.clock_verification")
    from bt_api_base.market_tick_protocol import (
        MarketEventCursor,
        MarketFreshnessStatus,
        MarketTickPayload,
        bind_market_tick_event,
    )

    provider, handle, client = _live_provider(monkeypatch, queue_capacity=2)
    source = provider.describe_source(
        handle,
        key=handle.key,
        generation=handle.generation,
        stream_id=handle.key.stream_id,
        epoch="hub-epoch-c1",
    )
    assert source is not None
    evidence = clock.ClockEvidence(
        offset_ns=0, error_bound_ns=1_000_000, server="fake-sntp", method="fake",
        measured_monotonic_ns=0, valid_until_monotonic_ns=2**62,
    )
    check = clock.VenueClockCheck(
        consistent=True, difference_ms=0.0, tolerance_ms=50.0, source="fake"
    )
    sequence = 0

    def qualified(events: tuple[object, ...]) -> list[object]:
        nonlocal sequence
        result = []
        for event in events:
            sequence += 1
            bound = bind_market_tick_event(
                MarketTickPayload.from_dict(event.data),
                source,
                MarketEventCursor(source.stream_id, source.epoch, sequence),
            )
            result.append(
                clock.qualify_market_tick_event(bound, evidence, check, max_latency_seconds=60.0)
            )
        return result

    client.emit_tick(InstrumentID="IF2612", Volume=1, BidPrice1=4_003.0, AskPrice1=4_002.0)
    readiness, crossed = qualified(provider.poll(handle, limit=8))
    for tick in (readiness, crossed):
        assert tick.data.freshness_status is MarketFreshnessStatus.FRESH
    assert readiness.data.volume_quality == "baseline"
    assert readiness.data.execution_eligible is True  # the first tick trades on its price
    assert crossed.data.execution_eligible is False

    client.emit_tick(
        InstrumentID="IF2612", Volume=2, LastPrice=4_200.0, BidPrice1=4_200.0,
        AskPrice1=sys.float_info.max, AskVolume1=0,
    )
    (locked,) = qualified(provider.poll(handle, limit=8))
    assert locked.data.execution_eligible is True  # the venue judges the order (D62-33)

    for volume in (3, 4, 5):  # one more than the two queue slots: one dropped
        client.emit_tick(InstrumentID="IF2612", Volume=volume)
    after_loss, next_tick = qualified(provider.poll(handle, limit=8))
    assert after_loss.data.continuity_status == "gap"
    assert after_loss.data.execution_eligible is False
    assert next_tick.data.execution_eligible is True
    assert provider.close() is True


# --------------------------------------------------------------------------
# iteration 62, defect 25: a failed source recovery attempt is retried
# --------------------------------------------------------------------------


def _instrument_key(symbol: str) -> MarketSubscriptionKey:
    return _key(
        instrument=MarketInstrumentKey(
            provider="ctp",
            venue="CFFEX",
            environment="simnow",
            product_type="future",
            instrument_id=symbol,
            native_symbol=symbol,
        )
    )


def _failing_construction_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CtpQuoteV2MarketProvider, list[_SyntheticMdClient], list[float], list[bool]]:
    """A provider whose replacement clients fail construction while ``fail[0]``."""

    _SyntheticMdClient.instances.clear()
    _SyntheticMdClient.authenticate = True
    created: list[_SyntheticMdClient] = []
    constructions: list[float] = []
    fail = [False]

    def make_client(*args: object) -> _SyntheticMdClient:
        constructions.append(time.monotonic())
        if fail[0]:
            raise OSError("synthetic replacement construction failure")
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
    return provider, created, constructions, fail


def _drive_until(provider: CtpQuoteV2MarketProvider, predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        provider.maintain()
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("condition not reached")


def test_source_recovery_backoff_is_bounded_and_repeats_its_last_delay() -> None:
    delays = [market_provider_module._source_recovery_retry_delay(n) for n in range(1, 9)]
    assert delays == [1.0, 2.0, 5.0, 10.0, 30.0, 30.0, 30.0, 30.0]
    assert market_provider_module._source_recovery_retry_delay(10_000) == 30.0


def test_failed_recovery_attempts_are_retried_without_limit_and_logged_throttled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        market_provider_module, "_SOURCE_RECOVERY_RETRY_DELAYS_SECONDS", (0.01, 0.02, 0.03)
    )
    caplog.set_level("WARNING", logger="bt_api_ctp.market_provider")
    provider, created, constructions, fail = _failing_construction_provider(monkeypatch)
    handle = provider.subscribe(_key(), generation="hub-generation-r1")
    fail[0] = True
    created[0].emit_disconnect()
    _drive_until(
        provider,
        lambda: provider._source_recovery is not None and provider._source_recovery.attempt >= 8,
    )
    assert provider.source_recovery_state(handle) == "recovering"
    retry_lines = [r.getMessage() for r in caplog.records if "recovery attempt" in r.getMessage()]
    # Within the interval only the first failed attempt is logged.
    assert retry_lines == [
        "CTP market source recovery attempt 1 failed in state reconnecting "
        "(reason=replacement_start_raised:OSError); retrying in 0.01 s "
        "(0 failed attempts not logged since the last line)"
    ]
    # Each replacement start waited for its delay; the last delay repeats.
    gaps = [later - earlier for earlier, later in zip(constructions[1:], constructions[2:])]
    assert len(gaps) >= 6
    assert gaps[0] >= 0.009 and gaps[1] >= 0.018
    assert all(0.027 <= gap < 0.5 for gap in gaps[2:6])

    # Once the interval passed, the next failure is logged with the count of
    # those not logged in between.
    monkeypatch.setattr(market_provider_module, "_SOURCE_RECOVERY_LOG_INTERVAL_SECONDS", 0.05)
    time.sleep(0.06)
    attempt = provider._source_recovery.attempt
    _drive_until(provider, lambda: provider._source_recovery.attempt > attempt)
    retry_lines = [r.getMessage() for r in caplog.records if "recovery attempt" in r.getMessage()]
    assert len(retry_lines) == 2
    assert "retrying in 0.03 s" in retry_lines[1]
    assert "(0 failed attempts" not in retry_lines[1]
    assert not any("stays closed" in r.getMessage() for r in caplog.records)
    assert provider.close() is True


def test_symbols_change_during_a_retry_wait_release_allowed_addition_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defined behavior while a lost source is being recovered (defect 25).

    Adding a symbol is refused (``CTP_MARKET_RECOVERY_IN_PROGRESS``; retry
    after the restart).  Releasing one is allowed whenever no native stop or
    start is in flight; the next attempt subscribes only the remaining set.
    """

    monkeypatch.setattr(market_provider_module, "_SOURCE_RECOVERY_RETRY_DELAYS_SECONDS", (0.05,))
    provider, created, _constructions, fail = _failing_construction_provider(monkeypatch)
    handle = provider.subscribe(_key(), generation="hub-generation-r1")
    second_handle = provider.subscribe(_instrument_key("IH2612"), generation="hub-generation-r1")
    fail[0] = True
    created[0].emit_disconnect()
    _drive_until(
        provider,
        lambda: provider._source_recovery is not None
        and provider._source_recovery.state == "retry_wait",
    )
    with pytest.raises(CtpQuoteV2MarketProviderError) as refused:
        provider.subscribe(_instrument_key("IC2612"), generation="hub-generation-r1")
    assert refused.value.code == "CTP_MARKET_RECOVERY_IN_PROGRESS"
    assert provider.unsubscribe(second_handle) is True
    assert second_handle.active is False
    assert provider._source_recovery.symbols == frozenset({"IF2612"})

    fail[0] = False
    _drive_until(provider, lambda: provider.source_epoch_reset_required(handle) is True)
    new_client = created[-1]
    assert new_client is not created[0]
    assert new_client.subscribed == ["IF2612"]
    assert provider.acknowledge_source_epoch_reset(handle, epoch="hub-epoch-r2") is True
    assert provider._source_recovery is None
    _emit_synthetic_tick(new_client, "IF2612", 5)
    events = provider.poll(handle, limit=4)  # the readiness tick, then this one
    assert [event.data["last_price"] for event in events][-1] == 4_006.0
    assert {event.data["provider_connection_generation"] for event in events} == {
        handle.source_scope.connection_generation
    }
    assert provider.close() is True


def test_a_final_refusal_racing_the_connecting_check_stays_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A final refusal is a contract violation even when it lands mid-check.

    The replacement's refusal sets ``fail_closed`` and ``ERROR`` together; the
    provider must read them together, never as a lost replacement to retry.
    """

    _SyntheticMdClient.instances.clear()
    _SyntheticMdClient.authenticate = True
    created: list[_SyntheticMdClient] = []

    def make_client(*args: object) -> _SyntheticMdClient:
        client = _SyntheticMdClient(*args)
        if created:  # the replacement logs in but is not ready yet
            client.emit_startup_acks = False
            client.emit_startup_readiness_tick = False
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
    created[0].emit_disconnect()
    _drive_until(
        provider,
        lambda: provider._source_recovery is not None
        and provider._source_recovery.state == "connecting",
    )
    stream = handle.stream

    def refused_during_the_check(checked: CtpMarketStream) -> None:
        checked._on_error(
            SimpleNamespace(), client=checked._md_client, client_token=checked._md_client_token
        )
        return None

    monkeypatch.setattr(provider, "_current_source_scope", refused_during_the_check)
    provider.maintain()
    monkeypatch.undo()
    assert provider._source_recovery.state == "failed"
    assert provider.source_recovery_state(handle) == "failed"
    assert stream.quote_v2_service_fail_closed is True
    assert len(created) == 2
    assert provider.close() is True
