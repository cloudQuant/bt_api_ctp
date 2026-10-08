"""CTP instrument universe provider.

Reuses the packaged ``TraderClient.query_instruments_result`` contract: the
instrument reference query only exists on the trading API, and only a
``complete=True`` result proves the terminal packet was observed.  An
incomplete query fails closed rather than silently collecting a partial
universe.

Queries run **per exchange** so each callback stream can be checked independently.
The native query uses an idle timeout between callbacks and a separate hard
duration cap; an incomplete stream still fails closed.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from bt_api_ctp.collector.lifecycle import check_stop
from bt_api_ctp.collector.protocols import DEFAULT_ASSET_TYPES, EXCHANGES, InstrumentSpec
from bt_api_ctp.instrument import normalize_ctp_instrument

_logger = logging.getLogger(__name__)


class CtpInstrumentProvider:
    """Fetch today's CTP instrument universe through a logged-in trader."""

    def __init__(
        self,
        trader: Any,
        *,
        asset_types: tuple[str, ...] = DEFAULT_ASSET_TYPES,
        exchanges: tuple[str, ...] = EXCHANGES,
        query_timeout_sec: float = 60.0,
        query_max_duration_sec: float = 600.0,
        query_retries: int = 1,
        retry_backoff_sec: float = 2.0,
        stop_event: Any = None,
    ) -> None:
        self._trader = trader
        self._asset_types = tuple(asset_types)
        self._exchanges = tuple(exchanges)
        self._query_timeout_sec = float(query_timeout_sec)
        self._query_max_duration_sec = float(query_max_duration_sec)
        self._retry_backoff_sec = float(retry_backoff_sec)
        if (
            not math.isfinite(self._query_timeout_sec)
            or self._query_timeout_sec <= 0
            or not math.isfinite(self._query_max_duration_sec)
            or self._query_max_duration_sec < self._query_timeout_sec
        ):
            raise ValueError("instrument_query_duration_settings_invalid")
        if isinstance(query_retries, bool) or int(query_retries) < 0:
            raise ValueError("instrument_query_retries_invalid")
        if not math.isfinite(self._retry_backoff_sec) or self._retry_backoff_sec < 0:
            raise ValueError("instrument_query_retry_backoff_invalid")
        self._query_retries = int(query_retries)
        self._stop_event = stop_event

    def fetch_instruments(self) -> list[InstrumentSpec]:
        """Return every subscribable contract visible to this account."""
        specs: list[InstrumentSpec] = []
        seen: set[tuple[str, str]] = set()
        incomplete: list[str] = []
        # 查询阶段可能持续十几分钟（实测盘前 37 秒、盘中 11 分钟以上），先说明等待
        # 范围，否则运行中的进程与"卡死"无法区分。
        _logger.info(
            "instrument query start: exchanges=%s idle_timeout=%.1fs "
            "max_duration=%.1fs retries=%d",
            ",".join(self._exchanges),
            self._query_timeout_sec,
            self._query_max_duration_sec,
            self._query_retries,
        )

        complete_count = 0
        completed_record_count = 0
        partial_records_by_exchange: dict[str, int] = {}
        unqueried: list[str] = []
        for index, exchange in enumerate(self._exchanges):
            check_stop(self._stop_event)
            started = time.monotonic()
            result, attempts = self._query_exchange(exchange)
            elapsed = time.monotonic() - started
            if getattr(result, "complete", None) is not True:
                incomplete.append(exchange)
                partial_records_by_exchange[exchange] = len(
                    getattr(result, "records", ()) or ()
                )
                _logger.warning(
                    "instrument query %s exhausted: attempts=%d in %.1fs partial_records=%d "
                    "terminal=%s timed_out=%s idle_timeout=%.1fs max_duration=%.1fs "
                    "error_code=%s error=%s",
                    exchange,
                    attempts,
                    elapsed,
                    partial_records_by_exchange[exchange],
                    getattr(result, "is_last_seen", None),
                    getattr(result, "timed_out", None),
                    self._query_timeout_sec,
                    self._query_max_duration_sec,
                    getattr(result, "error_code", None),
                    getattr(result, "error_message", "") or "-",
                )
                if (
                    getattr(result, "timed_out", False) is True
                    or getattr(result, "error_message", "") == "query_timeout"
                ):
                    unqueried = list(self._exchanges[index + 1 :])
                    if unqueried:
                        _logger.warning(
                            "instrument query stopped after timeout to avoid overlapping "
                            "native requests: unqueried_exchanges=%s",
                            ",".join(unqueried),
                        )
                    break
                continue
            records = getattr(result, "records", ())
            _logger.info(
                "instrument query %s ok: records=%d elapsed=%.1fs",
                exchange,
                len(records),
                elapsed,
            )
            complete_count += 1
            completed_record_count += len(records)
            self._collect(records, specs, seen)

        _logger.info(
            "instrument query summary: exchanges=%d complete=%d complete_records=%d "
            "partial_records=%s collected_instruments=%d incomplete=%s unqueried=%s",
            len(self._exchanges),
            complete_count,
            completed_record_count,
            partial_records_by_exchange or "{}",
            len(specs),
            ",".join(incomplete) or "-",
            ",".join(unqueried) or "-",
        )
        if incomplete:
            raise RuntimeError("ctp_instrument_query_incomplete:" + ",".join(incomplete))
        return specs

    def _query_exchange(self, exchange: str) -> tuple[Any, int]:
        """Query one exchange, retrying with backoff when the counter refuses.

        Several shard processes starting together can trip the counter's
        per-session query throttle; a short backoff can clear it. A timed-out
        request is not resubmitted because its native callback stream may still
        be active on this trader session.
        """
        result: Any = None
        for attempt in range(self._query_retries + 1):
            check_stop(self._stop_event)
            attempt_started = time.monotonic()
            kwargs = {"exchange_id": exchange, "timeout": self._query_timeout_sec}
            kwargs["max_duration"] = self._query_max_duration_sec
            if self._stop_event is not None:
                kwargs["should_stop"] = self._stop_event.is_set
            result = self._trader.query_instruments_result(**kwargs)
            check_stop(self._stop_event)
            if getattr(result, "complete", None) is True:
                return result, attempt + 1
            elapsed = time.monotonic() - attempt_started
            partial_records = len(getattr(result, "records", ()) or ())
            timed_out = getattr(result, "timed_out", False) is True
            error_message = getattr(result, "error_message", "") or "-"
            if timed_out or error_message == "query_timeout":
                _logger.warning(
                    "instrument query %s timed out: partial_records=%d terminal=%s "
                    "timed_out=%s elapsed=%.1fs idle_timeout=%.1fs max_duration=%.1fs; "
                    "not retrying the active native request",
                    exchange,
                    partial_records,
                    getattr(result, "is_last_seen", None),
                    timed_out,
                    elapsed,
                    self._query_timeout_sec,
                    self._query_max_duration_sec,
                )
                return result, attempt + 1
            if attempt < self._query_retries:
                delay = self._retry_backoff_sec * (2**attempt)
                _logger.warning(
                    "instrument query %s attempt %d/%d incomplete: error_code=%s error=%s; "
                    "partial_records=%d terminal=%s elapsed=%.1fs; retrying in %.1fs",
                    exchange,
                    attempt + 1,
                    self._query_retries + 1,
                    getattr(result, "error_code", None),
                    error_message,
                    partial_records,
                    getattr(result, "is_last_seen", None),
                    elapsed,
                    delay,
                )
                if self._stop_event is None:
                    time.sleep(delay)
                else:
                    # Short slices let Python dispatch console signals on Windows.
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline:
                        check_stop(self._stop_event)
                        # Do not wait on the event set by the signal handler:
                        # its condition lock is not reentrant.
                        time.sleep(max(0.0, min(0.1, deadline - time.monotonic())))
                    check_stop(self._stop_event)
        return result, self._query_retries + 1

    def close(self) -> None:
        """Release the trader session once the universe has been fetched.

        Collection only needs the market-data session; holding one trader
        login per shard process can exhaust the counter's concurrent-session
        limit when several shards start together.
        """
        stop = getattr(self._trader, "stop", None)
        if callable(stop):
            _logger.info("closing CTP trader session")
            stop()
        _logger.info("CTP trader session closed")

    def _collect(self, records: Any, specs: list[InstrumentSpec], seen: set) -> None:
        for record in records:
            metadata = normalize_ctp_instrument(record)
            instrument_id = metadata.get("instrument_id")
            exchange_id = metadata.get("exchange_id")
            asset_type = metadata.get("asset_type")
            if not instrument_id or not exchange_id:
                continue
            if exchange_id not in EXCHANGES:
                continue
            if asset_type not in self._asset_types:
                continue
            key = (exchange_id, instrument_id)
            if key in seen:
                continue
            seen.add(key)
            specs.append(
                InstrumentSpec(
                    instrument_id=instrument_id,
                    exchange_id=exchange_id,
                    asset_type=asset_type,
                    underlying_instrument=metadata.get("underlying_instrument"),
                    strike_price=metadata.get("strike_price"),
                    option_type=metadata.get("option_type"),
                    product_id=metadata.get("product_id"),
                )
            )


__all__ = ["CtpInstrumentProvider"]
