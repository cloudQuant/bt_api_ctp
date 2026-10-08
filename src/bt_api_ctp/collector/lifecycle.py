"""Cooperative cancellation shared by collector startup and collection."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any


class CollectionCancelled(Exception):
    """The user requested a graceful stop before startup finished."""


class CollectionStopEvent(threading.Event):
    """Accept console signals or a stop marker from a background supervisor."""

    def __init__(
        self, stop_file: Path | str | None = None, *, deadline_monotonic: float | None = None
    ) -> None:
        super().__init__()
        self._stop_file = Path(stop_file) if stop_file is not None else None
        self._deadline = deadline_monotonic

    def is_set(self) -> bool:
        return (
            super().is_set()
            or (self._stop_file is not None and self._stop_file.is_file())
            or (self._deadline is not None and time.monotonic() >= self._deadline)
        )


def check_stop(stop_event: Any) -> None:
    """Cancel at a safe boundary without interrupting a disk write."""
    if stop_event is not None and stop_event.is_set():
        raise CollectionCancelled("collection stopped by user")
