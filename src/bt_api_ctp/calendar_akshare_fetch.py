"""Bounded, optional AKShare single-future detail capture.

The child imports AKShare and pandas lazily. Its output is untrusted input, not
proof of source identity, contract, date, coverage, or authority. Only the
explicit 1.18.64 API is supported. POSIX supervisor limitations still apply.
The supervisor bounds parent waiting and IPC output, not memory, CPU or
decompression inside AKShare and its dependencies. It controls only the direct
child: descendants may survive even after a timeout and must not be presumed
cleaned up or safe to retry. Production integration needs separate controls.
"""
# Keep annotations resolvable on Python 3.9.
# ruff: noqa: UP045

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import date
from typing import Optional

if __name__ != "__main__":
    from bt_api_ctp._calendar_child_supervisor import supervise
    from bt_api_ctp.calendar_akshare_candidates import CandidateResult, collect_contract_candidate
    from bt_api_ctp.calendar_time_candidates import TimeTextCandidate, parse_time_candidates

VERSION = "1.18.64"
MAX_ROWS = 512
MAX_CELL = 4096
MAX_OUTPUT = 2_200_000
MAX_TIMEOUT = 60.0
_SOURCE_PREFIX = "akshare.futures_contract_detail.v1"
_CHILD_STATUSES = frozenset(
    {"OK", "DEPENDENCY_UNAVAILABLE", "VERSION_MISMATCH", "FETCH_ERROR", "STRUCTURE_CHANGED"}
)


@dataclass(frozen=True)
class TimeDiagnostic:
    upstream_status: str
    candidate: TimeTextCandidate
    status: str = "UPSTREAM_NOT_SUCCESS"


@dataclass(frozen=True)
class AkshareCapture:
    status: str
    transport_status: str
    candidate: CandidateResult
    source_id: Optional[str]
    capture_id: str
    response_digest: Optional[str]
    required_version: str
    observed_version: Optional[str]
    time_candidate: Optional[TimeTextCandidate] = None
    time_diagnostic: Optional[TimeDiagnostic] = None
    direct_child_cleanup: str = "UNKNOWN"
    descendant_cleanup: str = "NOT_PROVEN"
    timeout_retry: str = "NOT_APPLICABLE"
    verification: str = "UNVERIFIED"
    authorization: str = "NOT_AUTHORIZED"
    coverage: str = "UNKNOWN"

    @property
    def captured_at(self):
        return self.candidate.collected_at


class _Frame:
    columns = ("item", "value")

    def __init__(self, rows):
        self.rows = rows

    def itertuples(self, *, index, name):
        return iter(self.rows)


def _valid_rows(rows):
    if type(rows) is not list or len(rows) > MAX_ROWS:
        return False
    for row in rows:
        if type(row) is not list or len(row) != 2:
            return False
        for cell in row:
            if cell is None:
                continue
            if type(cell) is str:
                if len(cell) > MAX_CELL:
                    return False
            elif type(cell) is int:
                if cell.bit_length() > 256:
                    return False
            elif type(cell) is float:
                if not math.isfinite(cell):
                    return False
            else:
                return False
    return True


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode(raw):
    try:
        packet = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(packet) is not dict or set(packet) != {"status", "rows"}:
            raise ValueError
        status, rows = packet["status"], packet["rows"]
        if type(status) is not str or status not in _CHILD_STATUSES:
            raise ValueError
        if not _valid_rows(rows) or (status != "OK" and rows):
            raise ValueError
        return "CHILD_OK", packet
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return "CHILD_ERROR", None


def _response_digest(rows):
    """Digest exact captured rows for correlation, not source authentication."""
    basis = json.dumps(rows, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode(
        "ascii"
    )
    return hashlib.sha256(basis).hexdigest()


def _run(symbol, timeout):
    command = [sys.executable, os.path.abspath(__file__), "--child", symbol]
    return supervise(command, timeout, MAX_OUTPUT, _decode, oversize="CHILD_ERROR")


def _child(symbol):
    """Internal IPC entry; arbitrary API output/errors never reach stderr."""
    packet = {"status": "FETCH_ERROR", "rows": []}
    try:
        if (
            type(symbol) is not str
            or re.fullmatch(r"[A-Za-z]{1,8}[0-9]{3,4}", symbol, re.ASCII) is None
        ):
            raise ValueError
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            try:
                import akshare
                import pandas as pd
            except ImportError:
                packet["status"] = "DEPENDENCY_UNAVAILABLE"
            else:
                if getattr(akshare, "__version__", None) != VERSION:
                    packet["status"] = "VERSION_MISMATCH"
                else:
                    frame = akshare.futures_contract_detail(symbol=symbol)
                    if type(frame) is not pd.DataFrame or len(frame) > MAX_ROWS:
                        packet["status"] = "STRUCTURE_CHANGED"
                    elif len(frame.columns) != 2 or list(frame.columns) != ["item", "value"]:
                        packet["status"] = "STRUCTURE_CHANGED"
                    else:
                        rows = []
                        encoded_size = 100
                        for row in frame.itertuples(index=False, name=None):
                            if len(rows) >= MAX_ROWS or type(row) is not tuple or len(row) != 2:
                                packet["status"] = "STRUCTURE_CHANGED"
                                break
                            converted = []
                            for cell in row:
                                if (
                                    cell is pd.NA
                                    or cell is pd.NaT
                                    or (type(cell) is float and math.isnan(cell))
                                ):
                                    converted.append(None)
                                else:
                                    converted.append(cell)
                            if not _valid_rows([converted]):
                                packet["status"] = "STRUCTURE_CHANGED"
                                break
                            encoded_size += len(json.dumps(converted, ensure_ascii=True)) + 1
                            if encoded_size > MAX_OUTPUT:
                                packet["status"] = "STRUCTURE_CHANGED"
                                break
                            rows.append(converted)
                        else:
                            packet = {"status": "OK", "rows": rows}
    except Exception:
        packet = {"status": "FETCH_ERROR", "rows": []}
    encoded = json.dumps(packet, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode(
        "ascii"
    )
    if len(encoded) > MAX_OUTPUT:
        encoded = b'{"status":"STRUCTURE_CHANGED","rows":[]}'
    sys.stdout.buffer.write(encoded)


def collect_akshare_contract(
    *, venue: str, contract: str, requested_date: date, timeout: float = 15.0, _runner=None
) -> AkshareCapture:
    """Capture one future. No option inference or automatic mock fallback.

    ``_runner`` is a test seam; production callers must leave it unset. The
    result's SUCCESS only describes extraction, never source or calendar truth.
    ``source_id`` identifies the logical API and requested venue/contract; it
    proves neither scope nor provenance. ``capture_id``, ``captured_at`` and
    the ordered response digest are trace metadata, not calendar content or an
    authentication mechanism. A response digest change alone does not imply a
    calendar change. On upstream failure, any parsed time is diagnostic only.
    ``observed_version`` is the child-reported module version check, not proof
    of distribution identity. The requested date is metadata, not validity.
    """
    if (
        type(timeout) not in (float, int)
        or not 0 < timeout <= MAX_TIMEOUT
        or not math.isfinite(timeout)
    ):
        raise ValueError("invalid timeout")
    # Existing collector validates the request before launching the child.
    collect_contract_candidate(
        venue=venue,
        contract=contract,
        requested_date=requested_date,
        source_version=None,
        fetch=lambda **_: _Frame([]),
    )
    runner = _run if _runner is None else _runner
    try:
        transport, packet = runner(contract, float(timeout))
    except Exception:
        transport, packet = "CHILD_CLEANUP_UNCERTAIN", None
    if transport == "CHILD_OK":
        # Test seams and future transports get the same strict validation.
        try:
            raw = json.dumps(
                packet, ensure_ascii=True, allow_nan=False, separators=(",", ":")
            ).encode("ascii")
            if len(raw) > MAX_OUTPUT:
                transport, packet = "CHILD_ERROR", None
            else:
                transport, packet = _decode(raw)
        except (ValueError, TypeError, OverflowError, RecursionError):
            transport, packet = "CHILD_ERROR", None

    if transport == "CHILD_OK":
        status = packet["status"]
    elif transport == "CHILD_TIMEOUT":
        status = "FETCH_TIMEOUT"
    elif transport in {"CHILD_CLEANUP_UNCERTAIN", "SUPERVISOR_UNSUPPORTED"}:
        status = "UNKNOWN"
    else:
        transport, status = "CHILD_ERROR", "FETCH_ERROR"

    def fetch(**_):
        if status == "DEPENDENCY_UNAVAILABLE":
            raise ImportError
        if status == "FETCH_TIMEOUT":
            raise TimeoutError
        if status != "OK":
            raise RuntimeError
        return _Frame(packet["rows"])

    # The version is a child observation only for a successful version check
    # and validated packet. It is not an authenticated distribution identity.
    observed_version = VERSION if transport == "CHILD_OK" and status == "OK" else None
    candidate = collect_contract_candidate(
        venue=venue,
        contract=contract,
        requested_date=requested_date,
        source_version=observed_version,
        fetch=fetch,
    )
    if status == "OK":
        status = candidate.status
    elif status in {"STRUCTURE_CHANGED", "VERSION_MISMATCH", "UNKNOWN"}:
        candidate = replace(candidate, status=status)
    candidate = replace(
        candidate,
        timeout_enforced=_runner is None and transport in {"CHILD_OK", "CHILD_TIMEOUT"},
    )
    # Logical source stays stable across captures and content changes. Response
    # hashes and capture IDs belong to trace metadata, not calendar versioning.
    source_id = f"{_SOURCE_PREFIX}.{venue}.{contract}" if observed_version else None
    response_digest = _response_digest(packet["rows"]) if observed_version else None
    capture_id = f"akshare.capture.{uuid.uuid4().hex}"
    time_candidate = None
    time_diagnostic = None
    if candidate.fields:
        parsed = parse_time_candidates(
            source_id=capture_id,
            fields=[(field.raw_name, field.raw_value) for field in candidate.fields],
        )
        if status == "SUCCESS":
            time_candidate = parsed
        else:
            time_diagnostic = TimeDiagnostic(upstream_status=status, candidate=parsed)
    return AkshareCapture(
        status=status,
        transport_status=transport,
        candidate=candidate,
        source_id=source_id,
        capture_id=capture_id,
        response_digest=response_digest,
        required_version=VERSION,
        observed_version=observed_version,
        time_candidate=time_candidate,
        time_diagnostic=time_diagnostic,
        direct_child_cleanup=(
            "DIRECT_CHILD_REAPED" if _runner is None and transport == "CHILD_TIMEOUT" else "UNKNOWN"
        ),
        timeout_retry=(
            "NOT_PROVEN_SAFE" if status in {"FETCH_TIMEOUT", "UNKNOWN"} else "NOT_APPLICABLE"
        ),
    )


if __name__ == "__main__":
    # Only the validated parent command is supported; never echo arguments.
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        _child(sys.argv[2])
