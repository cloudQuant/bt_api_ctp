"""Parquet sink: dedup, sort, interrupted-run merge and integrity report.

Files land at ``<root>/<trading_day>/<exchange_id>/<instrument_id>.parquet``.
The trading day directory always names the CTP trading day, so night-session
ticks (whose ``action_day`` is the previous calendar day) are filed with the
session they belong to.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from bt_api_ctp.collector.protocols import TickRecord
from bt_api_ctp.collector.schedule import session_index

try:  # pragma: no cover - Windows has no fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

_LEVEL_COUNT = 5

_QUOTE_COLUMNS: tuple[tuple[str, pa.DataType], ...] = (
    ("last_price", pa.float64()),
    ("pre_settlement", pa.float64()),
    ("pre_close", pa.float64()),
    ("pre_open_interest", pa.float64()),
    ("open_price", pa.float64()),
    ("highest_price", pa.float64()),
    ("lowest_price", pa.float64()),
    ("volume", pa.int64()),
    ("turnover", pa.float64()),
    ("open_interest", pa.float64()),
    ("close_price", pa.float64()),
    ("settlement_price", pa.float64()),
    ("upper_limit", pa.float64()),
    ("lower_limit", pa.float64()),
    ("pre_delta", pa.float64()),
    ("curr_delta", pa.float64()),
    ("average_price", pa.float64()),
    ("banding_upper_price", pa.float64()),
    ("banding_lower_price", pa.float64()),
)

_BOOK_COLUMNS: tuple[tuple[str, pa.DataType], ...] = (
    ("bid_price", pa.float64()),
    ("bid_volume", pa.int64()),
    ("ask_price", pa.float64()),
    ("ask_volume", pa.int64()),
)


def _build_schema() -> pa.Schema:
    fields = [
        pa.field("trading_day", pa.string()),
        pa.field("action_day", pa.string()),
        pa.field("update_time", pa.string()),
        pa.field("update_millisec", pa.int32()),
        pa.field("local_receive_time", pa.int64()),
        pa.field("exchange_id", pa.string()),
        pa.field("instrument_id", pa.string()),
        pa.field("exchange_inst_id", pa.string()),
    ]
    fields.extend(pa.field(name, dtype) for name, dtype in _QUOTE_COLUMNS)
    for prefix, dtype in _BOOK_COLUMNS:
        for level in range(1, _LEVEL_COUNT + 1):
            fields.append(pa.field(f"{prefix}_{level}", dtype))
    return pa.schema(fields)


#: Fixed on-disk schema (NFR-5).  Column order never changes across runs.
TICK_ARROW_SCHEMA = _build_schema()


@dataclass
class InstrumentReport:
    """Per-instrument collection statistics."""

    exchange_id: str
    instrument_id: str
    rows: int
    first_update: str | None = None
    last_update: str | None = None
    gaps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SinkReport:
    """Result of one write or finalize call."""

    trading_day: str
    instruments: list[InstrumentReport] = field(default_factory=list)
    dropped_ticks: int = 0
    generated_at: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _time_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (row["action_day"] or "", row["update_time"] or "", int(row["update_millisec"] or 0))


def _stamp(row: dict[str, Any]) -> str:
    return (
        f"{row['action_day']} {row['update_time']}."
        f"{int(row['update_millisec'] or 0):03d}"
    )


def _row_moment(row: dict[str, Any]) -> datetime | None:
    try:
        return datetime.strptime(
            f"{row['action_day']} {row['update_time']}", "%Y%m%d %H:%M:%S"
        )
    except (TypeError, ValueError):
        return None


def _row_seconds(row: dict[str, Any]) -> float | None:
    moment = _row_moment(row)
    if moment is None:
        return None
    return moment.replace(tzinfo=timezone.utc).timestamp() + (
        int(row["update_millisec"] or 0) / 1000.0
    )


def _to_row(tick: TickRecord) -> dict[str, Any]:
    row: dict[str, Any] = {
        "trading_day": tick.trading_day,
        "action_day": tick.action_day,
        "update_time": tick.update_time,
        "update_millisec": int(tick.update_millisec),
        "local_receive_time": int(tick.local_receive_time),
        "exchange_id": tick.exchange_id,
        "instrument_id": tick.instrument_id,
        "exchange_inst_id": tick.exchange_inst_id,
    }
    for name, _ in _QUOTE_COLUMNS:
        row[name] = getattr(tick, name)
    for prefix, source in (
        ("bid_price", tick.bid_price),
        ("bid_volume", tick.bid_volume),
        ("ask_price", tick.ask_price),
        ("ask_volume", tick.ask_volume),
    ):
        for index in range(_LEVEL_COUNT):
            row[f"{prefix}_{index + 1}"] = source[index] if index < len(source) else None
    return row


def _dedup_sort(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate (action_day, update_time, update_millisec) keys.

    CTP may re-push a snapshot after a reconnect, so the newest local
    receive wins for an identical time key.
    """
    latest: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in rows:
        key = _time_key(row)
        current = latest.get(key)
        if current is None or int(row["local_receive_time"] or 0) >= int(
            current["local_receive_time"] or 0
        ):
            latest[key] = row
    return sorted(latest.values(), key=_time_key)


def _read_existing(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return pq.read_table(path).to_pylist()


def _write_atomically(path: Path, rows: list[dict[str, Any]]) -> None:
    table = pa.Table.from_pylist(rows, schema=TICK_ARROW_SCHEMA)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name: two processes writing the same instrument must never
    # fight over one scratch file.
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        pq.write_table(table, tmp_path)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


@contextmanager
def _instrument_lock(path: Path):
    """Cross-process exclusive lock guarding one instrument's read-modify-write.

    Callers may run several collectors over the same data root (for example
    overlapping shards or an accidental double start); without this lock the
    read-merge-write cycle silently loses rows.
    """
    if fcntl is None:  # pragma: no cover - Windows fallback
        yield
        return
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ParquetSink:
    """Write cleaned tick batches to Parquet and report on completeness."""

    def __init__(
        self,
        root: Path | str,
        *,
        merge_existing: bool = True,
        gap_threshold_sec: float = 60.0,
    ) -> None:
        self._root = Path(root)
        self._merge_existing = merge_existing
        self._gap_threshold_sec = float(gap_threshold_sec)

    @property
    def root(self) -> Path:
        return self._root

    def _instrument_report(
        self, exchange_id: str, instrument_id: str, rows: list[dict[str, Any]]
    ) -> InstrumentReport:
        gaps: list[dict[str, Any]] = []
        previous: tuple[float, dict[str, Any], int | None] | None = None
        for row in rows:
            moment = _row_moment(row)
            seconds = _row_seconds(row)
            if moment is None or seconds is None:
                continue
            current_session = session_index(moment)
            if previous is not None:
                previous_seconds, previous_row, previous_session = previous
                # Only silence inside one continuous session is a data gap;
                # scheduled breaks and the close-to-open jump are not.
                if current_session is not None and current_session == previous_session:
                    delta = seconds - previous_seconds
                    if delta > self._gap_threshold_sec:
                        gaps.append(
                            {
                                "after": _stamp(previous_row),
                                "before": _stamp(row),
                                "seconds": delta,
                            }
                        )
            previous = (seconds, row, current_session)
        return InstrumentReport(
            exchange_id=exchange_id,
            instrument_id=instrument_id,
            rows=len(rows),
            first_update=_stamp(rows[0]) if rows else None,
            last_update=_stamp(rows[-1]) if rows else None,
            gaps=gaps,
        )

    def write(
        self, ticks_by_instrument: dict[str, list[TickRecord]], *, dropped_ticks: int = 0
    ) -> SinkReport:
        """Clean and persist one batch, merging with existing files.

        A batch may straddle two trading days (the night session opens at
        21:00 and CTP flips ``TradingDay`` instantly), so rows are grouped by
        their own trading day rather than a single batch-wide value.
        """
        grouped: dict[tuple[str, str, str], list[TickRecord]] = {}
        for ticks in ticks_by_instrument.values():
            for tick in ticks:
                if not tick.trading_day:
                    raise ValueError("tick without trading_day")
                if not tick.exchange_id:
                    raise ValueError("tick without exchange_id")
                grouped.setdefault(
                    (tick.trading_day, tick.exchange_id, tick.instrument_id), []
                ).append(tick)

        if not grouped:
            return SinkReport(trading_day="", dropped_ticks=dropped_ticks)

        trading_day = Counter(key[0] for key in grouped).most_common(1)[0][0]
        instruments: list[InstrumentReport] = []
        for (row_trading_day, exchange_id, instrument_id), ticks in sorted(grouped.items()):
            path = self._root / row_trading_day / exchange_id / f"{instrument_id}.parquet"
            with _instrument_lock(path):
                rows = [_to_row(tick) for tick in ticks]
                if self._merge_existing:
                    rows = _read_existing(path) + rows
                rows = _dedup_sort(rows)
                _write_atomically(path, rows)
            instruments.append(self._instrument_report(exchange_id, instrument_id, rows))

        return SinkReport(
            trading_day=trading_day,
            instruments=instruments,
            dropped_ticks=dropped_ticks,
            generated_at=_now_iso(),
        )

    def finalize(self, trading_day: str, *, dropped_ticks: int = 0) -> SinkReport:
        """Scan every persisted file and write the completeness report."""
        base = self._root / trading_day
        instruments: list[InstrumentReport] = []
        if base.exists():
            for exchange_dir in sorted(path for path in base.iterdir() if path.is_dir()):
                for parquet_path in sorted(exchange_dir.glob("*.parquet")):
                    rows = _read_existing(parquet_path)
                    if not rows:
                        continue
                    instruments.append(
                        self._instrument_report(exchange_dir.name, parquet_path.stem, rows)
                    )

        report = SinkReport(
            trading_day=trading_day,
            instruments=instruments,
            dropped_ticks=dropped_ticks,
            generated_at=_now_iso(),
        )
        base.mkdir(parents=True, exist_ok=True)
        payload = {
            "trading_day": report.trading_day,
            "generated_at": report.generated_at,
            "dropped_ticks": report.dropped_ticks,
            "instruments": [
                {
                    "exchange_id": entry.exchange_id,
                    "instrument_id": entry.instrument_id,
                    "rows": entry.rows,
                    "first_update": entry.first_update,
                    "last_update": entry.last_update,
                    "gaps": entry.gaps,
                }
                for entry in report.instruments
            ],
        }
        (base / "report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report


__all__ = ["TICK_ARROW_SCHEMA", "InstrumentReport", "ParquetSink", "SinkReport"]
