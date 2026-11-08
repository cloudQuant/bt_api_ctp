"""Bounded in-process CTP callback capture; this buffer does not prove source.

A producer must establish source and session provenance separately.  Logical
byte accounting covers retained text and numeric fields, not Python heap size.
This volatile buffer makes no persistence or crash-losslessness guarantee.
"""

from __future__ import annotations

import math
import os
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import islice
from types import MappingProxyType

_MAX_TEXT_BYTES = 4096
_MAX_METADATA_BYTES = 256
_MAX_INT = 2**63 - 1
_MIN_INT = -(2**63)
_KINDS = frozenset(
    {"order", "trade", "insert_response", "insert_error", "action_response", "action_error"}
)
_GAP_CODES = frozenset({"invalid_record", "capacity_overflow", "byte_overflow", "source_failure"})
_BATCH_SEAL = object()


class CtpGatewayReturnBufferError(RuntimeError):
    """A finite, value-free buffer failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CtpGatewayReturnRecord:
    sequence: int
    kind: str
    row: Mapping[str, str | int | float] = field(repr=False)
    source_ref: str = field(repr=False)
    source_tags: tuple[str, str, str, str, int, int] = field(repr=False)
    connection_generation: int
    reception_trading_day: str
    front_id: int
    session_id: int
    request_id: int | None
    is_last: bool | None
    error_code: int | None
    login_request_id: int | None = None


@dataclass(frozen=True, slots=True)
class CtpGatewayReturnBatch:
    records: tuple[CtpGatewayReturnRecord, ...] = field(repr=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _BATCH_SEAL:
            raise CtpGatewayReturnBufferError("BATCH_UNTRUSTED")


@dataclass(frozen=True, slots=True)
class CtpGatewayReturnStatus:
    record_count: int
    byte_count: int
    last_sequence: int
    gap: bool
    gap_reason: str | None


def _text_bytes(value: object, limit: int, *, nonempty: bool = False) -> int:
    if type(value) is not str or len(value) > limit or (nonempty and not value):
        raise ValueError
    size = len(value.encode("utf-8", errors="strict"))
    if size > limit:
        raise ValueError
    return size


def _integer(value: object) -> bool:
    return type(value) is int and _MIN_INT <= value <= _MAX_INT


def _freeze_record_data(
    *,
    kind: object,
    row: object,
    source_ref: object,
    source_tags: object,
    connection_generation: object,
    reception_trading_day: object,
    front_id: object,
    session_id: object,
    request_id: object,
    is_last: object,
    error_code: object,
    login_request_id: object,
) -> tuple[Mapping[str, str | int | float], int]:
    if type(kind) is not str or kind not in _KINDS:
        raise ValueError
    total = len(kind) + _text_bytes(source_ref, _MAX_METADATA_BYTES, nonempty=True)
    total += _text_bytes(reception_trading_day, _MAX_METADATA_BYTES, nonempty=True)
    if type(source_tags) is not tuple or len(source_tags) != 6:
        raise ValueError
    for value in source_tags[:4]:
        total += _text_bytes(value, _MAX_METADATA_BYTES, nonempty=True)
    if not all(_integer(value) for value in source_tags[4:]):
        raise ValueError
    if not _integer(connection_generation) or connection_generation <= 0:
        raise ValueError
    if not _integer(front_id) or not _integer(session_id):
        raise ValueError
    if request_id is not None and not _integer(request_id):
        raise ValueError
    if error_code is not None and not _integer(error_code):
        raise ValueError
    if login_request_id is not None and (
        not _integer(login_request_id) or login_request_id <= 0
    ):
        raise ValueError
    if is_last is not None and type(is_last) is not bool:
        raise ValueError
    # Account for all retained fixed-width scalar slots, including sequence.
    total += 8 * 9 + 1
    if type(row) is not dict or not 1 <= len(row) <= 256:
        raise ValueError
    copied: dict[str, str | int | float] = {}
    for name, value in row.items():
        if (
            type(name) is not str
            or len(name) > 128
            or not name.isascii()
            or not name.isidentifier()
        ):
            raise ValueError
        total += len(name)
        if type(value) is str:
            total += _text_bytes(value, _MAX_TEXT_BYTES)
        elif _integer(value):
            total += 8
        elif type(value) is float and math.isfinite(value):
            total += 8
        else:
            raise ValueError
        copied[name] = value
    return MappingProxyType(copied), total


class CtpGatewayReturnBuffer:
    """Capture callback rows with one owner-thread offer/ack ticket at a time."""

    def __init__(
        self,
        *,
        broker_id: str,
        investor_id: str,
        capacity: int = 1024,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        try:
            _text_bytes(broker_id, _MAX_METADATA_BYTES, nonempty=True)
            _text_bytes(investor_id, _MAX_METADATA_BYTES, nonempty=True)
            if type(capacity) is not int or not 1 <= capacity <= 65536:
                raise ValueError
            if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
                raise ValueError
        except (ValueError, UnicodeError):
            raise CtpGatewayReturnBufferError("INVALID_CONFIGURATION") from None
        self._pid = os.getpid()
        self._owner_thread = threading.current_thread()
        self._broker_id, self._investor_id = broker_id, investor_id
        self._capacity, self._max_bytes = capacity, max_bytes
        self._lock = threading.Lock()
        self._rows: deque[tuple[CtpGatewayReturnRecord, int]] = deque()
        self._byte_count = 0
        self._last_sequence = 0
        self._gap_reason: str | None = None
        self._ticket: CtpGatewayReturnBatch | None = None
        self._ticket_records: tuple[CtpGatewayReturnRecord, ...] | None = None
        self._ticket_record_ids: frozenset[int] = frozenset()
        self._ticket_count = 0

    def _check_owner(self) -> None:
        if os.getpid() != self._pid or threading.current_thread() is not self._owner_thread:
            raise CtpGatewayReturnBufferError("OWNER_REQUIRED")

    def _set_gap_locked(self, code: str) -> None:
        if self._gap_reason is None:
            self._gap_reason = code

    def _mark_gap(self, code: str) -> None:
        if type(code) is not str or code not in _GAP_CODES:
            raise CtpGatewayReturnBufferError("INVALID_GAP_CODE")
        if os.getpid() != self._pid:
            raise CtpGatewayReturnBufferError("OWNER_REQUIRED")
        with self._lock:
            self._set_gap_locked(code)

    def _append(
        self,
        *,
        kind: str,
        row: dict[str, str | int | float],
        source_ref: str,
        source_tags: tuple[str, str, str, str, int, int],
        connection_generation: int,
        reception_trading_day: str,
        front_id: int,
        session_id: int,
        request_id: int | None = None,
        is_last: bool | None = None,
        error_code: int | None = None,
        login_request_id: int | None = None,
    ) -> bool:
        if os.getpid() != self._pid:
            raise CtpGatewayReturnBufferError("OWNER_REQUIRED")
        with self._lock:
            try:
                frozen_row, size = _freeze_record_data(
                    kind=kind,
                    row=row,
                    source_ref=source_ref,
                    source_tags=source_tags,
                    connection_generation=connection_generation,
                    reception_trading_day=reception_trading_day,
                    front_id=front_id,
                    session_id=session_id,
                    request_id=request_id,
                    is_last=is_last,
                    error_code=error_code,
                    login_request_id=login_request_id,
                )
            except Exception:
                self._set_gap_locked("invalid_record")
                return False
            if len(self._rows) >= self._capacity:
                self._set_gap_locked("capacity_overflow")
                return False
            if size > self._max_bytes - self._byte_count:
                self._set_gap_locked("byte_overflow")
                return False
            sequence = self._last_sequence + 1
            record = CtpGatewayReturnRecord(
                sequence,
                kind,
                frozen_row,
                source_ref,
                source_tags,
                connection_generation,
                reception_trading_day,
                front_id,
                session_id,
                request_id,
                is_last,
                error_code,
                login_request_id,
            )
            self._rows.append((record, size))
            self._byte_count += size
            self._last_sequence = sequence
            return True

    def offer(self, *, limit: int = 256) -> CtpGatewayReturnBatch:
        self._check_owner()
        with self._lock:
            if self._ticket is not None:
                if (
                    self._ticket._seal is not _BATCH_SEAL
                    or self._ticket.records is not self._ticket_records
                ):
                    raise CtpGatewayReturnBufferError("BATCH_UNTRUSTED")
                return self._ticket
            if type(limit) is not int or not 1 <= limit <= 256:
                raise CtpGatewayReturnBufferError("INVALID_LIMIT")
            records = tuple(record for record, _ in islice(self._rows, limit))
            self._ticket = CtpGatewayReturnBatch(records, _BATCH_SEAL)
            self._ticket_records = records
            self._ticket_record_ids = frozenset(id(record) for record in records)
            self._ticket_count = len(records)
            return self._ticket

    def ack(self, batch: CtpGatewayReturnBatch) -> None:
        self._check_owner()
        with self._lock:
            if (
                type(batch) is not CtpGatewayReturnBatch
                or self._ticket is not batch
                or batch._seal is not _BATCH_SEAL
                or batch.records is not self._ticket_records
            ):
                raise CtpGatewayReturnBufferError("BATCH_UNTRUSTED")
            for _ in range(self._ticket_count):
                _, size = self._rows.popleft()
                self._byte_count -= size
            self._ticket = None
            self._ticket_records = None
            self._ticket_record_ids = frozenset()
            self._ticket_count = 0

    def _contains_record(self, record: CtpGatewayReturnRecord) -> bool:
        """Check object identity in the active ticket, not native provenance."""
        self._check_owner()
        with self._lock:
            if self._ticket is None:
                return False
            if (
                self._ticket._seal is not _BATCH_SEAL
                or self._ticket.records is not self._ticket_records
            ):
                raise CtpGatewayReturnBufferError("BATCH_UNTRUSTED")
            return id(record) in self._ticket_record_ids

    def status(self) -> CtpGatewayReturnStatus:
        self._check_owner()
        with self._lock:
            return CtpGatewayReturnStatus(
                len(self._rows),
                self._byte_count,
                self._last_sequence,
                self._gap_reason is not None,
                self._gap_reason,
            )
