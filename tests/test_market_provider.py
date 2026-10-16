"""Focused tests for the native CTP quote-v2 ServiceMarket adapter."""

from __future__ import annotations

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
        self.on_tick = None
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.fail_subscribe_additional_after_accept = False
        self.stop_calls = 0
        self.stop_wait_calls = 0
        self.stop_receipts = list(self.__class__.stop_receipts_on_create)
        self.__class__.stop_receipts_on_create = []
        self.__class__.instances.append(self)

    def subscribe(self, instruments: list[str]) -> None:
        self.subscribed.extend(instruments)

    def subscribe_additional(self, instruments: list[str]) -> tuple[str, ...]:
        additions = tuple(item for item in instruments if item not in self.subscribed)
        self.subscribed.extend(additions)
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
        if self.__class__.authenticate:
            assert callable(self.on_login)
            self.on_login(SimpleNamespace())

    def emit_tick(self, **fields: object) -> None:
        assert callable(self.on_tick)
        self.on_tick(SimpleNamespace(**fields))

    def stop(self) -> None:
        self.stop_calls += 1

    def stop_and_wait(self, *, timeout: float) -> CtpNativeStopReceipt:
        assert timeout > 0
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
    second = provider.subscribe(second_key, generation="hub-generation-b")
    (client,) = _SyntheticMdClient.instances
    assert len(streams) == 1
    assert first.stream is second.stream is streams[0]
    assert client.subscribed == ["IF2612", "IH2612"]
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


def test_ctp_native_callback_queue_is_bounded_and_nonblocking() -> None:
    callback_queue = BoundedCtpQuoteV2Queue(capacity=2)
    callback_queue.put("first", block=True, timeout=0)
    callback_queue.put("second", block=True, timeout=0)
    callback_queue.put("third", block=True, timeout=0)

    assert callback_queue.dropped_oldest == 1
    assert callback_queue.drain(2) == ("second", "third")
    assert callback_queue.size == 0
