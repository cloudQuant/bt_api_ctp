"""CTP market-data subscriber built on the packaged ``MdClient``.

The native callback must never raise back into the CTP thread: a handler or
normalizer failure is counted and swallowed so one bad payload cannot stop
the whole collection run.  Reconnects are observable through
``connection_generation`` so the completeness report can flag the window.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from bt_api_ctp.collector.protocols import TickHandler
from bt_api_ctp.collector_ctp.normalizer import CtpTickNormalizer

_RUN_POLL_SECONDS = 0.1

_logger = logging.getLogger(__name__)


class CtpMdSubscriber:
    """Adapt a ``MdClient`` to the ``MarketDataSubscriber`` contract."""

    def __init__(
        self,
        md_client: Any,
        *,
        normalizer: Any | None = None,
        batch_size: int = 100,
        batch_interval_sec: float = 0.1,
        login_timeout_sec: float = 15.0,
        instrument_exchanges: dict[str, str] | None = None,
    ) -> None:
        self._md_client = md_client
        self.normalizer = normalizer if normalizer is not None else CtpTickNormalizer()
        self._batch_size = int(batch_size)
        self._batch_interval_sec = float(batch_interval_sec)
        self._login_timeout_sec = float(login_timeout_sec)
        self._instrument_exchanges: dict[str, str] = dict(instrument_exchanges or {})
        self._handler: TickHandler | None = None
        self._errors = 0
        self._subscribe_ok = 0
        self._subscribe_failed = 0
        self._last_subscribe_error_id: int | None = None
        self._stop_event = threading.Event()
        self._last_generation: int | None = None
        self._generation_changes: list[dict[str, Any]] = []

    # -- MarketDataSubscriber -------------------------------------------------

    def set_handler(self, handler: TickHandler) -> None:
        self._handler = handler

    def set_instrument_exchanges(self, mapping: dict[str, str]) -> None:
        """Provide the instrument -> exchange map taken from the contract query.

        CTP depth-market callbacks leave ``ExchangeID`` empty, so the
        authoritative exchange for a tick comes from the reference universe
        rather than from the quote itself.
        """
        self._instrument_exchanges = {
            str(key): str(value) for key, value in (mapping or {}).items()
        }

    def connect(self) -> None:
        """Bind the native callback, start the client and wait for login.

        Waiting matters: ``MdClient.subscribe_batched`` only splits into
        batches once the session is logged in.  Before login it merely records
        the pending set, and the post-login callback would submit everything
        in one native call.
        """
        self._stop_event.clear()
        self._last_generation = self._current_generation()
        self._md_client.on_tick = self._on_raw_tick
        self._md_client.on_subscribe = self._on_subscribe_response
        self._md_client.start(block=False)
        self._wait_ready()
        _logger.info("CTP market-data session login OK")

    def _wait_ready(self) -> None:
        wait_ready = getattr(self._md_client, "wait_ready", None)
        if not callable(wait_ready):
            return  # host cannot report readiness; keep legacy behaviour
        if wait_ready(timeout=self._login_timeout_sec) is not True:
            # Fail fast: a silent login failure would collect nothing for the
            # whole window while looking healthy in the heartbeat log.
            raise RuntimeError("ctp_md_login_timeout")

    def subscribe(self, instruments: list[str]) -> None:
        """Subscribe in batches while keeping the full set for reconnects."""
        self._md_client.subscribe_batched(
            instruments,
            batch_size=self._batch_size,
            interval_sec=self._batch_interval_sec,
        )

    def run(self) -> None:
        """Block until :meth:`close` is called."""
        self._stop_event.wait()

    def close(self) -> None:
        """Stop the native client and release any blocking ``run``."""
        self._stop_event.set()
        self._md_client.stop()
        _logger.info("CTP market-data session closed")

    # -- diagnostics ----------------------------------------------------------

    def error_count(self) -> int:
        """Return how many callback payloads were rejected."""
        return self._errors

    def generation_changes(self) -> list[dict[str, Any]]:
        """Return observed reconnect generations (excluding the first)."""
        return list(self._generation_changes)

    def subscription_stats(self) -> dict[str, Any]:
        """Return how many subscribe responses succeeded or failed."""
        return {
            "ok": self._subscribe_ok,
            "failed": self._subscribe_failed,
            "last_error_id": self._last_subscribe_error_id,
        }

    # -- internals ------------------------------------------------------------

    def _current_generation(self) -> int | None:
        generation = getattr(self._md_client, "connection_generation", None)
        return generation if isinstance(generation, int) else None

    def _observe_generation(self) -> None:
        generation = self._current_generation()
        if generation is None:
            return
        if self._last_generation is None:
            self._last_generation = generation
            return
        if generation != self._last_generation:
            self._generation_changes.append(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "generation": generation,
                }
            )
            self._last_generation = generation

    def _on_subscribe_response(self, specific_instrument: Any, rsp_info: Any) -> None:
        """Record one SubscribeMarketData response.  Must never raise."""
        del specific_instrument  # identity is implied by the request order
        try:
            error_id = int(getattr(rsp_info, "ErrorID", 0) or 0) if rsp_info is not None else 0
        except Exception:
            error_id = 0
        if error_id == 0:
            self._subscribe_ok += 1
        else:
            self._subscribe_failed += 1
            self._last_subscribe_error_id = error_id

    def _on_raw_tick(self, raw: Any) -> None:
        """Native callback entry point.  Must never raise."""
        try:
            self._observe_generation()
            tick = self.normalizer.normalize(raw)
        except Exception:
            self._errors += 1
            return
        if tick is None:
            return
        if not tick.exchange_id:
            exchange = self._instrument_exchanges.get(tick.instrument_id)
            if exchange:
                tick = replace(tick, exchange_id=exchange)
        handler = self._handler
        if handler is None:
            return
        try:
            handler.on_tick(tick)
        except Exception:
            self._errors += 1


__all__ = ["CtpMdSubscriber"]
