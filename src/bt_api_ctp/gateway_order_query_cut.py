"""Private owner-local order-query / return-buffer cut coordination.

Only records actually accepted by this buffer after begin and through the
terminal callback are covered. Prior ACKed rows, other ingress paths and the
venue's atomicity or continuity are not established.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from bt_api_ctp.query import native_account_query_filters
from bt_api_ctp.registered_sim_session import capture_query_session, validate_query_session


class CtpGatewayOrderQueryCutHandle:
    """Opaque handle for a single explicit owner-bound order query."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class CtpGatewayOrderQueryCutView:
    """A verified local sequence window; not a complete venue order view."""

    begin_sequence: int
    end_sequence: int
    records: tuple = field(repr=False)

    @property
    def authorizes_trading(self) -> bool:
        return False

    @property
    def venue_atomicity_proven(self) -> bool:
        return False

    @property
    def continuity_proven(self) -> bool:
        return False

    @property
    def pre_begin_history_proven(self) -> bool:
        return False


@dataclass(slots=True, eq=False)
class _OrderCutState:
    session: object = field(repr=False)
    owner: object = field(repr=False)
    handle: CtpGatewayOrderQueryCutHandle = field(repr=False)
    query_handle: object = field(repr=False)
    query_state: object = field(repr=False)
    buffer: object = field(repr=False)
    binding: object = field(default=None, repr=False)
    window: object = field(default=None, repr=False)
    accumulator: object = field(default=None, repr=False)
    result: object = field(default=None, repr=False)
    request_id: int | None = None
    records_digest: str | None = field(default=None, repr=False)
    ended: bool = False
    invalid: bool = False
    cancelled: bool = False

    def _same_owner_locked(self, trader) -> bool:
        owner = self.owner
        return bool(
            trader is owner.trader
            and self.session._connection_owner_matches(owner)
            and self.session._connection_status_locked(owner) == "ready"
            and self.session._order_query_cut is self
            and self.session._return_buffer is self.buffer
            and owner.return_buffer is self.buffer
            and owner.feed._gateway_return_buffer is self.buffer
            and trader._gateway_return_buffer is self.buffer
            and trader._gateway_return_environment == owner.account.environment
            and trader._query_evidence_issuer is owner.issuer
            and trader._nonblocking_session_current_locked(self.query_state)
        )

    def begin_locked(self, trader, accumulator) -> None:
        """Run at the owner reservation point immediately before native send."""
        try:
            if (
                self.invalid
                or self.accumulator is not None
                or not self._same_owner_locked(trader)
                or self.query_state.kind != "orders"
                or self.query_state.record_limit is not None
            ):
                self.invalid = True
                return
            binding = capture_query_session(trader, environment=self.owner.account.environment)
            expected_filters = tuple(
                sorted(
                    native_account_query_filters(
                        "orders", broker_id=binding.broker_id, investor_id=binding.investor_id
                    ).items()
                )
            )
            if (
                binding._issuer is not self.owner.issuer
                or binding._source_tags != self.query_state.source_tags
                or binding.connection_generation != self.query_state.generation
                or binding.trading_day != self.query_state.trading_day
                or accumulator.request_type != "orders"
                or type(accumulator.request_id) is not int
                or accumulator.request_id <= 0
                or accumulator.source_issuer is not binding._issuer
                or accumulator.source_tags != binding._source_tags
                or accumulator.connection_generation != binding.connection_generation
                or accumulator.account_fingerprint != binding.account_fingerprint
                or accumulator.trading_day != binding.trading_day
                or accumulator.broker_id != binding.broker_id
                or accumulator.investor_id != binding.investor_id
                or accumulator.request_filters != expected_filters
                or accumulator.request_intent_filters != expected_filters
                or accumulator.request_parameters != ()
                or accumulator.request_intent_parameters != ()
                or accumulator.explicit_request_filters != ()
                or accumulator.record_limit is not None
            ):
                self.invalid = True
                return
            window = self.buffer._begin_order_cut_window()
            self.binding = binding
            self.window = window
            self.accumulator = accumulator
            self.request_id = accumulator.request_id
        except Exception:
            self.invalid = True

    def end_locked(self, trader, accumulator) -> None:
        """Seal only the matching successful terminal packet under the native lock."""
        try:
            if (
                self.invalid
                or self.ended
                or self.accumulator is not accumulator
                or self.window is None
                or not self._same_owner_locked(trader)
                or accumulator.request_id != self.request_id
                or accumulator.is_last_seen is not True
                or accumulator.error_code not in (None, 0)
                or accumulator.completion_origin != "success"
                or accumulator.record_limit is not None
                or accumulator.record_limit_exceeded
                or type(accumulator.source_records_sha256) is not str
                or not validate_query_session(trader, self.binding, now_monotonic=time.monotonic())
                or not self.buffer._finish_order_cut_window(self.window)
            ):
                self.invalid = True
                return
            self.records_digest = accumulator.source_records_sha256
            self.ended = True
        except Exception:
            self.invalid = True
