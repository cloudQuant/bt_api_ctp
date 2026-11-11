"""Opt-in bounded fetch of a validated root-relative SHFE product path.

The fixed host, TLS, parsed fields, path, and observation hash do not establish
source authority, freshness, complete listings, effective dates or permission.
Path validation only checks syntax; it does not prove navigation membership.
Before batch use, the SDK must independently check the selected path against
the inspected candidate, its conflicts and digest, without treating that
candidate or those checks as authenticated source authority.
No request occurs on import; injected runners and transports are test ports.
Only parsed observations cross the owned process boundary, never raw HTML.
OS process creation and uninterruptible waits cannot be strictly bounded;
Windows, live account and production behavior have not been established.
"""

import http.client
import importlib.util
import json
import math
import os
import selectors
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

if __name__ == "__main__":

    def _load(name, file):
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
        if spec is None or spec.loader is None:
            raise ImportError("missing parser")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    nav = _load("_calendar_shfe_detail_nav", "calendar_shfe_product_navigation.py")
    product = _load("_calendar_shfe_detail_product", "calendar_shfe_product_page.py")
    _path_kind = nav._path_kind
    MAX_BYTES, ProductPage = product.MAX_BYTES, product.ProductPage
    parse_shfe_product_page, _record = product.parse_shfe_product_page, product._record
else:
    from bt_api_ctp.calendar_shfe_product_navigation import _path_kind
    from bt_api_ctp.calendar_shfe_product_page import (
        MAX_BYTES,
        ProductPage,
        _record,
        parse_shfe_product_page,
    )

_HOST = "www.shfe.com.cn"
_ORIGIN = "https://www.shfe.com.cn"
_MAX_RESULT = 8 * 1024 * 1024
_TRANSPORT = frozenset(
    {
        "HTTP_OK",
        "HTTP_ERROR",
        "REDIRECT",
        "TLS_ERROR",
        "NETWORK_TIMEOUT",
        "NETWORK_ERROR",
        "RESPONSE_TOO_LARGE",
        "EMPTY_RESPONSE",
        "PARSER_ERROR",
        "RESULT_TOO_LARGE",
        "CHILD_ERROR",
        "CHILD_TIMEOUT",
        "CHILD_CLEANUP_UNCERTAIN",
    }
)
_PARSE = frozenset({"CANDIDATE", "NEEDS_REVIEW", "INVALID_UTF8", "STRUCTURE_CHANGED"})


@dataclass(frozen=True)
class DetailFetchResult:
    status: str
    transport_status: str
    parse_status: str
    collected_at: datetime
    page: Optional[ProductPage]
    path: str
    kind: str
    transport_assurance: str
    identity_status: str
    verification: str = "UNVERIFIED"
    source_authentication: str = "NOT_PROVEN"
    current_record: str = "UNKNOWN"
    coverage: str = "UNKNOWN"
    publication: str = "NOT_AUTHORIZED"


def _fetch_and_parse(
    path,
    connect_timeout,
    read_timeout,
    max_bytes,
    *,
    connection_factory: Callable = http.client.HTTPSConnection,
    parser: Callable = parse_shfe_product_page,
):
    try:
        _path_kind(path)
    except Exception:
        return "CHILD_ERROR", None
    connection = None
    try:
        connection = connection_factory(
            _HOST, timeout=connect_timeout, context=ssl.create_default_context()
        )
        connection.request("GET", path, headers={"Accept-Encoding": "identity"})
        if connection.sock is not None:
            connection.sock.settimeout(read_timeout)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            return "REDIRECT", None
        if (
            response.status != 200
            or response.getheader("Content-Encoding", "identity").lower() != "identity"
        ):
            return "HTTP_ERROR", None
        length = response.getheader("Content-Length")
        if length is not None:
            if len(length) > 20 or not length.isascii() or not length.isdecimal():
                return "HTTP_ERROR", None
            if int(length) > max_bytes:
                return "RESPONSE_TOO_LARGE", None
        data = bytearray()
        while True:
            part = response.read(min(65536, max_bytes + 1 - len(data)))
            if not part:
                break
            data.extend(part)
            if len(data) > max_bytes:
                return "RESPONSE_TOO_LARGE", None
        if length is not None and len(data) != int(length):
            return "HTTP_ERROR", None
        if not data:
            return "EMPTY_RESPONSE", None
        try:
            page = parser(source_url=_ORIGIN + path, html=bytes(data))
            if type(page) is not ProductPage:
                raise ValueError("invalid parser result")
        except Exception:
            return "PARSER_ERROR", None
        return "HTTP_OK", page
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


def _encode(status, page):
    try:
        output = json.dumps(
            {"transport": status, "page": asdict(page) if page is not None else None},
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        return (
            output
            if len(output) <= _MAX_RESULT
            else b'{"transport":"RESULT_TOO_LARGE","page":null}'
        )
    except Exception:
        return b'{"transport":"PARSER_ERROR","page":null}'


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode(output, path):
    try:
        kind = _path_kind(path)
        if type(output) is not bytes or len(output) > _MAX_RESULT:
            raise ValueError
        envelope = json.loads(output, object_pairs_hook=_pairs)
        if type(envelope) is not dict or set(envelope) != {"transport", "page"}:
            raise ValueError
        status, data = envelope["transport"], envelope["page"]
        if type(status) is not str or status not in _TRANSPORT:
            raise ValueError
        if status != "HTTP_OK":
            if data is not None:
                raise ValueError
            return status, None
        if type(data) is not dict or set(data) != {
            "status",
            "source_url",
            "sha256",
            "records",
            "review_reasons",
            "verification",
            "source_authentication",
            "current_record",
            "coverage",
        }:
            raise ValueError
        if (
            data["status"] not in _PARSE
            or data["source_url"] != _ORIGIN + path
            or data["verification"] != "UNVERIFIED"
            or data["source_authentication"] != "NOT_PROVEN"
            or data["current_record"] != "UNKNOWN"
            or data["coverage"] != "UNKNOWN"
            or type(data["sha256"]) is not str
            or len(data["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in data["sha256"])
            or type(data["records"]) is not list
            or len(data["records"]) > 100
            or type(data["review_reasons"]) is not list
        ):
            raise ValueError
        records = []
        for row in data["records"]:
            if (
                type(row) is not dict
                or set(row)
                != {
                    "kind",
                    "symbol",
                    "venue",
                    "raw_date",
                    "fields",
                    "unknown_reasons",
                    "effective_date",
                }
                or type(row["fields"]) is not list
                or not 0 < len(row["fields"]) <= 100
            ):
                raise ValueError
            fields = {}
            for pair in row["fields"]:
                if (
                    type(pair) is not list
                    or len(pair) != 2
                    or type(pair[0]) is not str
                    or type(pair[1]) is not str
                    or len(pair[0]) > 100
                    or len(pair[1]) > 8192
                    or pair[0] in fields
                    or any(
                        ord(c) < 32 or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF
                        for c in pair[0] + pair[1]
                    )
                ):
                    raise ValueError
                fields[pair[0]] = pair[1]
            record = _record(fields, kind)
            if json.loads(json.dumps(asdict(record), ensure_ascii=True)) != row:
                raise ValueError
            records.append(record)
        reasons = []
        if any(record.unknown_reasons for record in records):
            reasons.append("RECORD_UNRESOLVED")
        if len({record.symbol for record in records if record.symbol is not None}) > 1:
            reasons.append("SYMBOL_CONFLICT")
        if len({record.venue for record in records if record.venue is not None}) > 1:
            reasons.append("VENUE_CONFLICT")
        if data["status"] in {"CANDIDATE", "NEEDS_REVIEW"}:
            if (
                not records
                or data["review_reasons"] != reasons
                or data["status"] != ("NEEDS_REVIEW" if reasons else "CANDIDATE")
            ):
                raise ValueError
        elif records or data["review_reasons"]:
            raise ValueError
        return status, ProductPage(
            status=data["status"],
            source_url=data["source_url"],
            sha256=data["sha256"],
            records=tuple(records),
            review_reasons=tuple(data["review_reasons"]),
        )
    except Exception:
        return "CHILD_ERROR", None


def _stop(child, status):
    try:
        child.kill()
        child.wait(timeout=1.0)
    except Exception:
        return "CHILD_CLEANUP_UNCERTAIN", None
    return status, None


def _run_child(path, connect_timeout, read_timeout, total_timeout, max_bytes):
    deadline = time.monotonic() + total_timeout
    command = [
        sys.executable,
        str(Path(__file__).absolute()),
        "--child",
        path,
        str(connect_timeout),
        str(read_timeout),
        str(max_bytes),
    ]
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
            shell=False,
        )
    except Exception:
        return "CHILD_ERROR", None
    try:
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return _stop(child, "CHILD_TIMEOUT")
                part = os.read(child.stdout.fileno(), min(65536, _MAX_RESULT + 1 - len(output)))
                if not part:
                    break
                output.extend(part)
                if len(output) > _MAX_RESULT:
                    return _stop(child, "RESULT_TOO_LARGE")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _stop(child, "CHILD_TIMEOUT")
        try:
            child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            return _stop(child, "CHILD_TIMEOUT")
        if child.returncode != 0:
            return "CHILD_ERROR", None
        return _decode(bytes(output), path)
    except Exception:
        return _stop(child, "CHILD_ERROR")
    finally:
        if child.stdout is not None:
            child.stdout.close()


def collect_detail_page(
    *,
    path,
    connect_timeout=3.0,
    read_timeout=3.0,
    total_timeout=10.0,
    max_bytes=MAX_BYTES,
    runner=_run_child,
):
    """Collect exactly one validated path; do not infer identity from its spelling."""
    try:
        kind = _path_kind(path)
    except Exception as exc:
        raise ValueError("invalid product path") from exc
    if any(
        type(t) not in (int, float) or not math.isfinite(t) or not 0 < t <= 60
        for t in (connect_timeout, read_timeout, total_timeout)
    ):
        raise ValueError("invalid timeout")
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES or not callable(runner):
        raise ValueError("invalid fetch settings")
    try:
        transport, page = runner(path, connect_timeout, read_timeout, total_timeout, max_bytes)
        transport, page = _decode(_encode(transport, page), path)
    except Exception:
        transport, page = "CHILD_ERROR", None
    parse = page.status if page is not None else "NOT_OBSERVED"
    identity = "NOT_ASSESSED"
    if page is not None and parse in {"CANDIDATE", "NEEDS_REVIEW"}:
        if (
            len({row.symbol for row in page.records if row.symbol is not None}) > 1
            or len({row.venue for row in page.records if row.venue is not None}) > 1
        ):
            identity = "CONFLICTING_FIELDS"
        elif any(row.symbol is None or row.venue is None for row in page.records):
            identity = "INCOMPLETE_FIELDS"
        else:
            identity = "CONSISTENT_FIELDS_UNVERIFIED"
    result = (
        parse
        if identity == "CONSISTENT_FIELDS_UNVERIFIED"
        else "NEEDS_REVIEW"
        if identity in {"CONFLICTING_FIELDS", "INCOMPLETE_FIELDS"}
        else "UNKNOWN"
    )
    return DetailFetchResult(
        result,
        transport,
        parse,
        datetime.now(timezone.utc),
        page,
        path,
        kind,
        "SUPERVISED_DEFAULT_TLS_POLICY" if runner is _run_child else "INJECTED_UNVERIFIED",
        identity,
    )


if __name__ == "__main__":
    try:
        if len(sys.argv) != 6 or sys.argv[1] != "--child":
            raise ValueError
        path = sys.argv[2]
        _path_kind(path)
        connect, read, limit = float(sys.argv[3]), float(sys.argv[4]), int(sys.argv[5])
        if (
            any(not math.isfinite(t) or not 0 < t <= 60 for t in (connect, read))
            or not 0 < limit <= MAX_BYTES
        ):
            raise ValueError
        sys.stdout.buffer.write(_encode(*_fetch_and_parse(path, connect, read, limit)))
    except Exception:
        sys.exit(2)
