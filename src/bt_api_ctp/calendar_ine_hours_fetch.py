# Keep type hints resolvable on Python 3.9.
# ruff: noqa: UP045
"""Collect the fixed INE public trading-hours page as unverified evidence.

One HTTPS request in a supervised child supplies the exact raw bytes and their
digest. The parent validates the child protocol and parses those same bytes
using the existing bounded table parser. No candidate is published, installed,
or treated as proof of venue coverage, effective dates or trading permission.

The default supervisor is POSIX-only and may report cleanup uncertainty; it
does not prove hard deadlines, all descendant cleanup or network availability.
An injected runner or connection factory is an unverified test seam. The raw
public page is returned only to the explicit API caller, never printed by the
parent; the child emits it solely on the supervisor's private stdout pipe.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import math
import socket
import ssl
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

if __name__ != "__main__":
    from bt_api_ctp._calendar_child_supervisor import supervise
    from bt_api_ctp.calendar_shfe_page import MAX_BYTES, PageCandidate, parse_shfe_page
else:
    MAX_BYTES = 1024 * 1024

SOURCE = "https://www.ine.cn/services/calenderandholidays/tradinghours/"
_HOST = "www.ine.cn"
_PATH = "/services/calenderandholidays/tradinghours/"
_HOLIDAY_PATH = "/services/calenderandholidays/holiday/"
_SCHEMA = "bt_api_ctp.ine-hours-raw/1"
_HOLIDAY_SCHEMA = "bt_api_ctp.ine-holiday-raw/1"
_CHILD_LIMIT = 2 * 1024 * 1024
_CHILD_STATUSES = frozenset(
    {
        "HTTP_OK",
        "HTTP_ERROR",
        "REDIRECT",
        "TLS_ERROR",
        "NETWORK_TIMEOUT",
        "NETWORK_ERROR",
        "RESPONSE_TOO_LARGE",
        "EMPTY_RESPONSE",
    }
)
_PARENT_STATUSES = _CHILD_STATUSES | {
    "CHILD_ERROR",
    "CHILD_TIMEOUT",
    "CHILD_CLEANUP_UNCERTAIN",
    "SUPERVISOR_UNSUPPORTED",
}


@dataclass(frozen=True)
class IneHoursResult:
    status: str
    transport_status: str
    collected_at: datetime
    source: str = SOURCE
    sha256: Optional[str] = None
    page: Optional[Any] = None
    raw_bytes: Optional[bytes] = field(default=None, repr=False)
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    effective_dates: str = "NOT_PROVEN"
    association: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"
    transport_assurance: str = "INJECTED_UNVERIFIED"


def _fetch_bytes(
    connect_timeout: float,
    read_timeout: float,
    max_bytes: int,
    *,
    connection_factory: Callable = http.client.HTTPSConnection,
    _holiday: bool = False,
) -> tuple[str, Optional[bytes]]:
    connection = None
    try:
        context = ssl.create_default_context()
        connection = connection_factory(_HOST, timeout=connect_timeout, context=context)
        connection.request(
            "GET", _HOLIDAY_PATH if _holiday else _PATH, headers={"Accept-Encoding": "identity"}
        )
        if connection.sock is not None:
            connection.sock.settimeout(read_timeout)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            return "REDIRECT", None
        if response.status != 200:
            return "HTTP_ERROR", None
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            return "HTTP_ERROR", None
        length = response.getheader("Content-Length")
        if length is not None:
            if (
                type(length) is not str
                or len(length) > 20
                or not length.isascii()
                or not length.isdecimal()
            ):
                return "HTTP_ERROR", None
            if int(length) > max_bytes:
                return "RESPONSE_TOO_LARGE", None
        data = bytearray()
        while True:
            chunk = response.read(min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            if type(chunk) is not bytes:
                return "NETWORK_ERROR", None
            data.extend(chunk)
            if len(data) > max_bytes:
                return "RESPONSE_TOO_LARGE", None
        if not data:
            return "EMPTY_RESPONSE", None
        if length is not None and len(data) != int(length):
            return "NETWORK_ERROR", None
        return "HTTP_OK", bytes(data)
    except ssl.SSLError:
        return "TLS_ERROR", None
    except (TimeoutError, socket.timeout):
        return "NETWORK_TIMEOUT", None
    except Exception:
        return "NETWORK_ERROR", None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _encode(status: str, raw: Optional[bytes], *, _holiday: bool = False) -> bytes:
    if status not in _CHILD_STATUSES or (status == "HTTP_OK") != (type(raw) is bytes):
        raise ValueError("invalid child response")
    if raw is not None and (not raw or len(raw) > MAX_BYTES):
        raise ValueError("invalid response size")
    return json.dumps(
        {
            "schema": _HOLIDAY_SCHEMA if _holiday else _SCHEMA,
            "transport": status,
            "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
            "raw_base64": base64.b64encode(raw).decode("ascii") if raw is not None else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _decode(data: bytes, max_bytes: int, *, _holiday: bool = False) -> tuple[str, Optional[bytes]]:
    """Reject child attempts to claim supervisor-only statuses or forge bytes."""
    try:
        if type(data) is not bytes or len(data) > _CHILD_LIMIT:
            raise ValueError("invalid protocol size")
        item = json.loads(data, object_pairs_hook=_unique_pairs)
        if type(item) is not dict or set(item) != {"schema", "transport", "sha256", "raw_base64"}:
            raise ValueError("invalid protocol")
        status = item["transport"]
        schema = _HOLIDAY_SCHEMA if _holiday else _SCHEMA
        if item["schema"] != schema or type(status) is not str or status not in _CHILD_STATUSES:
            raise ValueError("invalid status")
        if status != "HTTP_OK":
            if item["sha256"] is not None or item["raw_base64"] is not None:
                raise ValueError("invalid failed response")
            return status, None
        encoded, digest = item["raw_base64"], item["sha256"]
        if type(encoded) is not str or len(encoded) > 4 * ((max_bytes + 2) // 3):
            raise ValueError("invalid encoded bytes")
        raw = base64.b64decode(encoded, validate=True)
        if (
            not raw
            or len(raw) > max_bytes
            or base64.b64encode(raw).decode("ascii") != encoded
            or type(digest) is not str
            or hashlib.sha256(raw).hexdigest() != digest
        ):
            raise ValueError("invalid raw digest")
        return status, raw
    except Exception:
        return "CHILD_ERROR", None


def _run_child(connect_timeout, read_timeout, total_timeout, max_bytes, *, _holiday=False):
    command = [
        sys.executable,
        str(Path(__file__).absolute()),
        "--child-holiday" if _holiday else "--child",
        str(connect_timeout),
        str(read_timeout),
        str(max_bytes),
    ]
    return supervise(
        command,
        total_timeout,
        _CHILD_LIMIT,
        lambda output: _decode(output, max_bytes, _holiday=_holiday),
        oversize="CHILD_ERROR",
    )


def _run_holiday_child(connect_timeout, read_timeout, total_timeout, max_bytes):
    return _run_child(connect_timeout, read_timeout, total_timeout, max_bytes, _holiday=True)


def collect_ine_hours(
    *,
    connect_timeout: float = 3.0,
    read_timeout: float = 3.0,
    total_timeout: float = 10.0,
    max_bytes: int = MAX_BYTES,
    runner: Callable = _run_child,
) -> IneHoursResult:
    """Fetch one public page and return its exact raw bytes and parsed candidate."""
    for value in (connect_timeout, read_timeout, total_timeout):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 60:
            raise ValueError("invalid timeout")
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES or not callable(runner):
        raise ValueError("invalid fetch settings")
    try:
        transport, raw = runner(connect_timeout, read_timeout, total_timeout, max_bytes)
    except Exception:
        transport, raw = "CHILD_CLEANUP_UNCERTAIN", None
    if (
        type(transport) is not str
        or transport not in _PARENT_STATUSES
        or (transport == "HTTP_OK") != (type(raw) is bytes)
        or (raw is not None and (not raw or len(raw) > max_bytes))
    ):
        raise ValueError("invalid runner result")
    now = datetime.now(timezone.utc)
    assurance = "SUPERVISED_DEFAULT_TLS_POLICY" if runner is _run_child else "INJECTED_UNVERIFIED"
    if raw is None:
        return IneHoursResult(transport, transport, now, transport_assurance=assurance)
    digest = hashlib.sha256(raw).hexdigest()
    # Recognize the observed access challenge without retrying or bypassing it.
    if "人机识别".encode() in raw and "应用防火墙".encode() in raw:
        return IneHoursResult(
            "ACCESS_CHALLENGE",
            transport,
            now,
            sha256=digest,
            raw_bytes=raw,
            transport_assurance=assurance,
        )
    try:
        page = parse_shfe_page(source_id=SOURCE, html=raw)
        if (
            type(page) is not PageCandidate
            or page.source_id != SOURCE
            or page.sha256 != digest
            or page.verification != "UNVERIFIED"
        ):
            raise ValueError("invalid parser result")
        return IneHoursResult(
            page.status,
            transport,
            now,
            sha256=digest,
            page=page,
            raw_bytes=raw,
            transport_assurance=assurance,
        )
    except Exception:
        return IneHoursResult(
            "PARSER_ERROR",
            transport,
            now,
            sha256=digest,
            raw_bytes=raw,
            transport_assurance=assurance,
        )


def _main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] not in {"--child", "--child-holiday"}:
        return 2
    try:
        connect, read, limit = float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
        if not all(math.isfinite(v) and 0 < v <= 60 for v in (connect, read)):
            return 2
        if not 0 < limit <= MAX_BYTES:
            return 2
        holiday = sys.argv[1] == "--child-holiday"
        status, raw = _fetch_bytes(connect, read, limit, _holiday=holiday)
        sys.stdout.buffer.write(_encode(status, raw, _holiday=holiday))
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    sys.exit(_main())
