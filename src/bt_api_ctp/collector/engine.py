"""The exchange-agnostic collection orchestrator.

``run_once`` drives one collection window: query the universe, apply the
shard filter, connect, subscribe, buffer ticks, flush in batches, then write
the completeness report.  It depends only on the protocols, so any venue
implementation can be plugged in.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bt_api_ctp.collector.buffer import TickBuffer
from bt_api_ctp.collector.protocols import (
    DEFAULT_ASSET_TYPES,
    EXCHANGES,
    InstrumentProvider,
    MarketDataSubscriber,
    TickRecord,
)
from bt_api_ctp.collector.shard import ShardConfig, select_instruments
from bt_api_ctp.collector.sink import ParquetSink, SinkReport

_MAX_POLL_SECONDS = 0.2

_logger = logging.getLogger(__name__)


@dataclass
class CollectionConfig:
    """Everything one collection run needs besides its venue wiring."""

    data_root: Path | str
    #: Defaults to the whole market; a real deployment always narrows this.
    shard: ShardConfig = field(
        default_factory=lambda: ShardConfig(strategy="by_exchange", exchanges=EXCHANGES)
    )
    asset_types: tuple[str, ...] = DEFAULT_ASSET_TYPES
    per_instrument_cap: int = 100_000
    overflow_policy: str = "drop"
    flush_interval_sec: float = 5.0
    merge_existing: bool = True
    gap_threshold_sec: float = 60.0
    #: 0 disables the heartbeat.  Unattended runs should keep it on.
    heartbeat_interval_sec: float = 60.0


class _BufferHandler:
    """Bridge the subscriber callback to the buffer (no IO on this path)."""

    def __init__(self, buffer: TickBuffer) -> None:
        self._buffer = buffer
        self.accepted = 0

    def on_tick(self, tick: TickRecord) -> None:
        if self._buffer.append(tick):
            self.accepted += 1


class TickCollectionEngine:
    """Orchestrate provider, subscriber, buffer and sink."""

    def __init__(
        self,
        *,
        provider: InstrumentProvider,
        subscriber: MarketDataSubscriber,
        config: CollectionConfig,
    ) -> None:
        self._provider = provider
        self._subscriber = subscriber
        self._config = config

    @property
    def config(self) -> CollectionConfig:
        return self._config

    def _select(self, instruments):
        if self._config.asset_types:
            instruments = [
                spec for spec in instruments if spec.asset_type in self._config.asset_types
            ]
        return select_instruments(instruments, self._config.shard)

    def run_once(
        self, *, duration_sec: float | None = None, stop_event: Any = None
    ) -> SinkReport:
        """Collect for one window, then flush and report."""
        try:
            instruments = self._provider.fetch_instruments()
        finally:
            # The reference query is done (or failed); release the venue's
            # query session either way so it never outlives this call.
            close_provider = getattr(self._provider, "close", None)
            if callable(close_provider):
                close_provider()
        selected = self._select(instruments)
        buffer = TickBuffer(
            per_instrument_cap=self._config.per_instrument_cap,
            overflow_policy=self._config.overflow_policy,
        )
        if not selected:
            _logger.info("collection skipped: no instruments selected")
            return SinkReport(trading_day="", dropped_ticks=buffer.dropped_count())

        sink = ParquetSink(
            self._config.data_root,
            merge_existing=self._config.merge_existing,
            gap_threshold_sec=self._config.gap_threshold_sec,
        )
        handler = _BufferHandler(buffer)
        self._subscriber.set_handler(handler)
        self._subscriber.connect()
        # CTP depth callbacks carry no ExchangeID; hand the venue the
        # instrument -> exchange map so ticks keep their exchange directory.
        set_exchanges = getattr(self._subscriber, "set_instrument_exchanges", None)
        if callable(set_exchanges):
            set_exchanges({spec.instrument_id: spec.exchange_id for spec in selected})
        self._subscriber.subscribe([spec.instrument_id for spec in selected])
        _logger.info(
            "subscribed %d instruments (shard=%s, flush=%ss)",
            len(selected),
            self._config.shard.strategy,
            self._config.flush_interval_sec,
        )

        poll_seconds = min(_MAX_POLL_SECONDS, max(self._config.flush_interval_sec, 0.001))
        deadline = None
        if duration_sec is not None:
            deadline = time.monotonic() + float(duration_sec)
        started = time.monotonic()
        last_flush = started
        last_heartbeat = started
        trading_day = ""

        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                time.sleep(poll_seconds)
                now = time.monotonic()
                if buffer.should_flush() or (now - last_flush) >= self._config.flush_interval_sec:
                    trading_day = self._flush(sink, buffer, trading_day)
                    last_flush = now
                if (
                    self._config.heartbeat_interval_sec > 0
                    and (now - last_heartbeat) >= self._config.heartbeat_interval_sec
                ):
                    stats_fn = getattr(self._subscriber, "subscription_stats", None)
                    stats = stats_fn() if callable(stats_fn) else {}
                    _logger.info(
                        "heartbeat: received=%d buffered=%d dropped=%d sub_ok=%s sub_failed=%s elapsed=%.0fs",
                        handler.accepted,
                        buffer.pending(),
                        buffer.dropped_count(),
                        stats.get("ok", "n/a"),
                        stats.get("failed", "n/a"),
                        now - started,
                    )
                    last_heartbeat = now
        finally:
            trading_day = self._flush(sink, buffer, trading_day)
            self._subscriber.close()

        if not trading_day:
            _logger.info(
                "collection finished: no data persisted (received=%d)", handler.accepted
            )
            return SinkReport(trading_day="", dropped_ticks=buffer.dropped_count())
        report = sink.finalize(trading_day, dropped_ticks=buffer.dropped_count())
        _logger.info(
            "collection finished: trading_day=%s instruments=%d received=%d dropped=%d",
            report.trading_day,
            len(report.instruments),
            handler.accepted,
            buffer.dropped_count(),
        )
        return report

    def _flush(self, sink: ParquetSink, buffer: TickBuffer, trading_day: str) -> str:
        drained = buffer.drain()
        if not drained:
            return trading_day
        rows = sum(len(ticks) for ticks in drained.values())
        try:
            report = sink.write(drained)
        except Exception:
            # Never lose a batch to a transient sink error (disk full, IO):
            # hand the ticks back to the buffer; dedup makes order irrelevant.
            _logger.exception(
                "flush failed; returning %d instruments / %d ticks to buffer",
                len(drained),
                rows,
            )
            for ticks in drained.values():
                for tick in ticks:
                    buffer.append(tick)
            return trading_day
        _logger.info("flushed %d instruments / %d ticks", len(drained), rows)
        return report.trading_day or trading_day


__all__ = ["CollectionConfig", "TickCollectionEngine"]
