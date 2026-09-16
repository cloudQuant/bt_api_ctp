"""CTP instrument universe provider.

Reuses the packaged ``TraderClient.query_instruments_result`` contract: the
instrument reference query only exists on the trading API, and only a
``complete=True`` result proves the terminal packet was observed.  An
incomplete query fails closed rather than silently collecting a partial
universe.

Queries run **per exchange**: a single whole-market query returns tens of
thousands of rows and does not terminate within a practical timeout on
SimNow, whereas one exchange at a time completes reliably.
"""

from __future__ import annotations

import logging
import time
from typing import Any

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
        query_retries: int = 3,
        retry_backoff_sec: float = 2.0,
    ) -> None:
        self._trader = trader
        self._asset_types = tuple(asset_types)
        self._exchanges = tuple(exchanges)
        self._query_timeout_sec = float(query_timeout_sec)
        self._query_retries = max(0, int(query_retries))
        self._retry_backoff_sec = float(retry_backoff_sec)

    def fetch_instruments(self) -> list[InstrumentSpec]:
        """Return every subscribable contract visible to this account."""
        specs: list[InstrumentSpec] = []
        seen: set[tuple[str, str]] = set()
        incomplete: list[str] = []

        for exchange in self._exchanges:
            result = self._query_exchange(exchange)
            if getattr(result, "complete", None) is not True:
                incomplete.append(exchange)
                continue
            self._collect(getattr(result, "records", ()), specs, seen)

        if incomplete:
            raise RuntimeError(
                "ctp_instrument_query_incomplete:" + ",".join(incomplete)
            )
        return specs

    def _query_exchange(self, exchange: str) -> Any:
        """Query one exchange, retrying with backoff when the counter refuses.

        Several shard processes starting together can trip the counter's
        per-session query throttle; a short backoff usually clears it.
        """
        result: Any = None
        for attempt in range(self._query_retries + 1):
            result = self._trader.query_instruments_result(
                exchange_id=exchange, timeout=self._query_timeout_sec
            )
            if getattr(result, "complete", None) is True:
                return result
            if attempt < self._query_retries:
                time.sleep(self._retry_backoff_sec * (2**attempt))
        return result

    def close(self) -> None:
        """Release the trader session once the universe has been fetched.

        Collection only needs the market-data session; holding one trader
        login per shard process can exhaust the counter's concurrent-session
        limit when several shards start together.
        """
        stop = getattr(self._trader, "stop", None)
        if callable(stop):
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
