"""Typed completion evidence for CTP multi-packet queries."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class QueryResult(Generic[T]):
    """Immutable result for one CTP request ID and connection generation.

    ``records`` may be empty only when ``complete`` proves that the matching
    successful terminal packet was observed.  Consumers must not interpret an
    empty incomplete result as an empty account, position, order, trade, fee,
    margin, or instrument set.
    """

    request_type: str
    request_id: int
    connection_generation: int
    account_fingerprint: str
    started_at_utc: datetime
    completed_at_utc: datetime | None
    is_last_seen: bool
    error_code: int | None
    error_message: str
    timed_out: bool
    complete: bool
    records: tuple[T, ...] = field(default_factory=tuple)
    late_callback_count: int = 0
    unsupported: bool = False
    submit_code: int | None = None

    @property
    def first(self) -> T | None:
        """Return the first record only for a complete query."""
        if not self.complete or not self.records:
            return None
        return self.records[0]

    @property
    def evidence_complete(self) -> bool:
        """Alias used by fail-closed consumers of heterogeneous query envelopes."""
        return self.complete

    def as_dict(self, *, include_records: bool = False) -> dict[str, Any]:
        """Return JSON-friendly completion evidence for public envelopes."""
        data: dict[str, Any] = {
            "request_type": self.request_type,
            "request_id": self.request_id,
            "connection_generation": self.connection_generation,
            "account_fingerprint": self.account_fingerprint,
            "started_at_utc": self.started_at_utc.isoformat(),
            "completed_at_utc": (
                self.completed_at_utc.isoformat()
                if self.completed_at_utc is not None
                else None
            ),
            "is_last_seen": self.is_last_seen,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "timed_out": self.timed_out,
            "complete": self.complete,
            "evidence_complete": self.evidence_complete,
            "record_count": len(self.records),
            "late_callback_count": self.late_callback_count,
            "unsupported": self.unsupported,
            "submit_code": self.submit_code,
        }
        if include_records:
            data["records"] = list(self.records)
        return data

    @classmethod
    def unavailable(
        cls,
        *,
        request_type: str,
        request_id: int,
        connection_generation: int,
        account_fingerprint: str,
        started_at_utc: datetime,
        error_message: str,
        error_code: int | None = None,
        unsupported: bool = False,
        records: Iterable[T] = (),
    ) -> QueryResult[T]:
        """Build a fail-closed local result when no valid terminal packet exists."""
        return cls(
            request_type=request_type,
            request_id=request_id,
            connection_generation=connection_generation,
            account_fingerprint=account_fingerprint,
            started_at_utc=started_at_utc,
            completed_at_utc=datetime.now(started_at_utc.tzinfo),
            is_last_seen=False,
            error_code=error_code,
            error_message=error_message,
            timed_out=False,
            complete=False,
            records=tuple(records),
            unsupported=unsupported,
        )


__all__ = ["QueryResult"]
