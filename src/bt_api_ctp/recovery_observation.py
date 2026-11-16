"""Bounded, process-local observations of the provider's recovery barrier."""

from __future__ import annotations

import os
import time
from collections import deque
from uuid import uuid4

from bt_api_base.market_recovery_observation import (
    RecoveryCoverageObservation,
    RecoveryObservationMember,
    RecoveryObservationPage,
)


class RecoveryObservationUnavailable(RuntimeError):
    pass


class RecoveryObservationBuffer:
    """Owner-only memory buffer. It makes no claim about native network health."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._records = deque(maxlen=capacity)
        self._source_epoch = uuid4().hex
        self._pid = os.getpid()
        self._sequence = 0
        self._last_time = 0
        self._episode = None
        self._poisoned = False

    def _now(self) -> int:
        now = time.monotonic_ns()
        if type(now) is not int or now <= 0 or now < self._last_time or now > (1 << 63) - 1:
            raise ValueError("invalid monotonic clock")
        self._last_time = now
        return now

    @staticmethod
    def _members(handles):
        members = tuple(
            RecoveryObservationMember(handle.key, handle.generation) for handle in handles
        )
        return tuple(sorted(members, key=lambda member: (member.key.stream_id, member.generation)))

    def _append(self, state, reason, *, now, recovery=None, members=None):
        episode = self._episode
        if self._sequence >= (1 << 63) - 1:
            raise ValueError("observation sequence exhausted")
        generation = None
        if recovery is not None:
            generation = getattr(recovery.stream, "_connection_generation", None)
            if type(generation) is not int or generation <= 0:
                raise ValueError("invalid observed generation")
        record = RecoveryCoverageObservation(
            source_epoch=self._source_epoch,
            owner_pid=self._pid,
            sequence=self._sequence + 1,
            episode_id=episode["id"] if episode else None,
            state=state,
            reason=reason,
            observed_monotonic_ns=now,
            coverage_started_monotonic_ns=episode["start"]
            if episode and state != "UNKNOWN"
            else None,
            coverage_through_monotonic_ns=now if episode and state != "UNKNOWN" else None,
            origin_connection_generation=episode["origin"] if episode else None,
            observed_connection_generation=generation,
            members=episode["members"] if episode else (members or ()),
        )
        self._records.append(record)
        self._sequence += 1

    def _guard(self, operation, *args):
        if self._poisoned:
            return
        try:
            operation(*args)
        except Exception:
            self._poisoned = True
            self._episode = None

    def begin(self, recovery, handles, reentry=False):
        self._guard(self._begin, recovery, handles, reentry)

    def _begin(self, recovery, handles, reentry):
        members = self._members(handles)
        if reentry and self._episode is None:
            return
        if self._episode is not None:
            if (
                members != self._episode["members"]
                or recovery.stream is not self._episode["stream"]
            ):
                self._invalidate("SCOPE_CHANGED", recovery)
                return
            self._episode["recovery"] = recovery
            return
        now = self._now()
        self._episode = {
            "id": uuid4().hex,
            "start": now,
            "origin": recovery.old_stream_generation,
            "members": members,
            "stream": recovery.stream,
            "recovery": recovery,
        }
        self._append("RECOVERING", "BEGIN", now=now, recovery=recovery)

    def transfer(self, previous, replacement, handles):
        self._guard(self._transfer, previous, replacement, handles)

    def _transfer(self, previous, replacement, handles):
        if self._episode is None:
            return
        if (
            self._episode["recovery"] is not previous
            or self._episode["members"] != self._members(handles)
            or self._episode["stream"] is not replacement.stream
        ):
            self._invalidate("SCOPE_CHANGED", previous)
            return
        self._episode["recovery"] = replacement

    def invalidate(self, reason, recovery=None, handles=()):
        self._guard(self._invalidate, reason, recovery, handles)

    def _invalidate(self, reason, recovery=None, handles=()):
        if self._episode is None and reason not in ("BEGIN_FAILED",):
            return
        now = self._now()
        self._append(
            "UNKNOWN",
            reason,
            now=now,
            recovery=recovery,
            members=self._members(handles) if self._episode is None else None,
        )
        self._episode = None

    def fail(self, recovery, handles):
        self._guard(self._fail, recovery, handles)

    def _fail(self, recovery, handles):
        if self._episode is None:
            if recovery is None:
                self._invalidate("BEGIN_FAILED", None, handles)
        elif self._episode["recovery"] is not recovery:
            self._invalidate("REENTRY_FAILED", None)
        elif not self._matches(recovery, handles):
            self._invalidate("SCOPE_CHANGED", recovery)
        else:
            self._append("FAILED", "FAILED", now=self._now(), recovery=recovery)
            self._episode = None

    def complete(self, recovery, handles):
        self._guard(self._complete, recovery, handles)

    def _complete(self, recovery, handles):
        if self._episode is None:
            return
        if not self._matches(recovery, handles):
            self._invalidate("SCOPE_CHANGED", recovery)
            return
        self._append("RECOVERED", "COMPLETED", now=self._now(), recovery=recovery)
        self._episode = None

    def _matches(self, recovery, handles):
        episode = self._episode
        if (
            episode is None
            or recovery is not episode["recovery"]
            or recovery.stream is not episode["stream"]
            or self._members(handles) != episode["members"]
            or any(handle.stream is not recovery.stream for handle in handles)
        ):
            return False
        if recovery.state == "reset_pending":
            if recovery.new_scope is None or any(
                handle.stream is not recovery.stream
                or handle.source_scope is not recovery.new_scope
                for handle in handles
            ):
                return False
            with recovery.stream._md_callback_lock:
                current = recovery.stream._managed_quote_clock_source_scope
                if current is not None and (
                    current is not recovery.new_scope
                    or recovery.stream._connection_generation != current.connection_generation
                ):
                    return False
        elif any(handle.source_scope is not recovery.old_scope for handle in handles):
            return False
        return True

    def read(self, after_sequence, limit, recovery, handles):
        if self._poisoned:
            raise RecoveryObservationUnavailable
        if (
            type(after_sequence) is not int
            or after_sequence < 0
            or after_sequence > self._sequence
            or type(limit) is not int
            or not 1 <= limit <= 256
        ):
            raise ValueError("invalid observation cursor or limit")
        try:
            if self._episode is not None:
                if self._matches(recovery, handles) and recovery.state != "failed":
                    self._append("RECOVERING", "WATERMARK", now=self._now(), recovery=recovery)
                else:
                    self._invalidate("SCOPE_CHANGED", recovery)
            first = self._records[0].sequence if self._records else 1
            observations = tuple(item for item in self._records if item.sequence > after_sequence)[
                :limit
            ]
            return RecoveryObservationPage(
                self._source_epoch,
                self._pid,
                after_sequence,
                first,
                self._sequence,
                observations,
                after_sequence < first - 1,
            )
        except Exception as exc:
            self._poisoned = True
            self._episode = None
            raise RecoveryObservationUnavailable from exc
