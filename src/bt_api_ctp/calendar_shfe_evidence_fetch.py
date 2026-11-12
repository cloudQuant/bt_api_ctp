# ruff: noqa: UP045
"""Supervised one-request capture of unverified public SHFE page bytes.

Raw bytes are private evidence, never a publication or source-authenticity proof.
The runner has the same cooperative POSIX limitations as the child supervisor.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from bt_api_ctp import calendar_shfe_fetch as shfe
from bt_api_ctp._calendar_child_supervisor import supervise

MAX_IPC = 4 * 1024 * 1024
_CHILD_ONLY = frozenset(
    {"CHILD_ERROR", "CHILD_TIMEOUT", "CHILD_CLEANUP_UNCERTAIN", "SUPERVISOR_UNSUPPORTED"}
)


@dataclass(frozen=True)
class PublicPageEvidence:
    kind: str
    source: str
    sha256: str
    raw: bytes = field(repr=False)
    page: Any = field(repr=False)


@dataclass(frozen=True)
class EvidenceFetchResult:
    transport_status: str
    collected_at: datetime
    evidence: Optional[PublicPageEvidence] = field(default=None, repr=False)
    transport_assurance: str = "INJECTED_UNVERIFIED"
    source_authentication: str = "NOT_PROVEN"


def _unique(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def decode_evidence(output: bytes, kind: str, max_bytes: int):
    """Reparse child evidence and reject forged status, digest or page data."""
    try:
        if type(output) is not bytes or len(output) > MAX_IPC or kind not in {"hours", "holiday"}:
            raise ValueError
        obj = json.loads(output, object_pairs_hook=_unique)
        if type(obj) is not dict or set(obj) != {"schema", "result", "raw_base64"}:
            raise ValueError
        if obj["schema"] != "shfe-evidence-ipc/1" or type(obj["result"]) is not dict:
            raise ValueError
        result = obj["result"]
        # Child output can never assert a parent-supervisor terminal condition.
        if result.get("transport") in _CHILD_ONLY:
            raise ValueError
        encoded = json.dumps(result, ensure_ascii=True).encode("ascii")
        decoder = shfe._decode_result if kind == "hours" else shfe._decode_holiday_result
        status, page = decoder(encoded)
        if status in _CHILD_ONLY or status != result.get("transport"):
            raise ValueError
        if status != "HTTP_OK":
            if page is not None or obj["raw_base64"] is not None:
                raise ValueError
            return status, None
        text = obj["raw_base64"]
        if type(text) is not str or len(text) > 4 * ((max_bytes + 2) // 3):
            raise ValueError
        raw = base64.b64decode(text, validate=True)
        if not raw or len(raw) > max_bytes or base64.b64encode(raw).decode("ascii") != text:
            raise ValueError
        source = shfe._SOURCE if kind == "hours" else shfe._HOLIDAY_SOURCE
        parser = shfe.parse_shfe_page if kind == "hours" else shfe.parse_shfe_holiday
        digest = hashlib.sha256(raw).hexdigest()
        if page is None or page.sha256 != digest or parser(source_id=source, html=raw) != page:
            raise ValueError
        return status, PublicPageEvidence(kind, source, digest, raw, page)
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, RecursionError):
        return "CHILD_CLEANUP_UNCERTAIN", None


def _run(kind, connect_timeout, read_timeout, total_timeout, max_bytes):
    command = [
        sys.executable,
        str(Path(shfe.__file__).absolute()),
        f"--child-evidence-{kind}",
        str(connect_timeout),
        str(read_timeout),
        str(max_bytes),
    ]
    return supervise(
        command,
        total_timeout,
        MAX_IPC,
        lambda out: decode_evidence(out, kind, max_bytes),
        oversize="CHILD_ERROR",
    )


def collect_shfe_evidence(
    *,
    kind: str,
    connect_timeout: float = 3.0,
    read_timeout: float = 3.0,
    total_timeout: float = 10.0,
    max_bytes: int = shfe.MAX_BYTES,
    runner: Callable = _run,
) -> EvidenceFetchResult:
    if kind not in {"hours", "holiday"} or not callable(runner):
        raise ValueError("invalid evidence settings")
    for timeout in (connect_timeout, read_timeout, total_timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("invalid timeout")
    if type(max_bytes) is not int or not 0 < max_bytes <= shfe.MAX_BYTES:
        raise ValueError("invalid evidence limit")
    try:
        status, evidence = runner(kind, connect_timeout, read_timeout, total_timeout, max_bytes)
    except Exception:
        status, evidence = "CHILD_CLEANUP_UNCERTAIN", None
    if status not in shfe._TRANSPORT_STATUSES or (status == "HTTP_OK") != (evidence is not None):
        raise ValueError("invalid evidence runner result")
    if evidence is not None:
        if type(evidence) is not PublicPageEvidence or evidence.kind != kind:
            raise ValueError("invalid evidence")
        # Even injected runners are structurally checked before retention.
        raw, page = evidence.raw, evidence.page
        expected_source = shfe._SOURCE if kind == "hours" else shfe._HOLIDAY_SOURCE
        parser = shfe.parse_shfe_page if kind == "hours" else shfe.parse_shfe_holiday
        if (
            type(raw) is not bytes
            or not 0 < len(raw) <= max_bytes
            or evidence.source != expected_source
            or evidence.sha256 != hashlib.sha256(raw).hexdigest()
            or parser(source_id=expected_source, html=raw) != page
        ):
            raise ValueError("invalid evidence")
    return EvidenceFetchResult(
        status,
        datetime.now(timezone.utc),
        evidence,
        "SUPERVISED_DEFAULT_TLS_POLICY" if runner is _run else "INJECTED_UNVERIFIED",
    )


def evidence_utf8(evidence: PublicPageEvidence) -> Optional[str]:
    """Return lossless UTF-8 including BOM, or None; never replace bytes."""
    if type(evidence) is not PublicPageEvidence:
        return None
    try:
        text = evidence.raw.decode("utf-8", "strict")
        return text if text.encode("utf-8", "strict") == evidence.raw else None
    except (UnicodeError, AttributeError):
        return None
