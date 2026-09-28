"""Immutable evidence captured from native CTP order-action callbacks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal

CtpOrderActionStatus = Literal["accepted", "rejected", "unknown"]


@dataclass(frozen=True)
class CtpOrderActionEvidence:
    """One request-bound view of a CTP order-action callback.

    ``accepted`` means CTP returned a successful ``OnRspOrderAction`` for this
    exact action request. It does not mean the exchange completed the cancel.
    CTP 6.7.7 exposes no request-scoped query that can promote it to
    ``cancelled``; order status callbacks are intentionally not used here.
    """

    request_id: int
    order_action_ref: str
    status: CtpOrderActionStatus
    account_fingerprint: str = field(repr=False)
    trading_day: str
    connection_generation: int
    order_ref: str
    order_sys_id: str
    front_id: int
    session_id: int
    instrument_id: str
    exchange_id: str
    action_flag: str
    evidence_source: str
    callback_received: bool
    evidence_received: bool
    error_code: int | None
    error_message: str
    reason: str
    submitted_at_utc: datetime
    observed_at_utc: datetime | None
    submit_code: int | None

    @property
    def is_known(self) -> bool:
        """Whether a matching native callback produced a non-UNKNOWN result."""

        return self.evidence_received and self.status != "unknown"

    def as_dict(self, *, include_error_message: bool = False) -> dict[str, object]:
        """Return a detached JSON-friendly representation for public adapters."""

        result = asdict(self)
        result["account_fingerprint"] = "<redacted>"
        if not include_error_message:
            result["error_message"] = ""
        result["submitted_at_utc"] = self.submitted_at_utc.isoformat()
        result["observed_at_utc"] = (
            self.observed_at_utc.isoformat() if self.observed_at_utc is not None else None
        )
        result["is_known"] = self.is_known
        return result


__all__ = ["CtpOrderActionEvidence", "CtpOrderActionStatus"]
