"""Opt-in, bounded fetch of three SHFE-hosted product pages.

No request occurs at import. Standard TLS verifies certificates and hostnames;
redirects are never followed. The child sends parsed observations, not HTML.
A URL, TLS, local timestamp or hash is not source authority, freshness, record
validity or coverage. Injected runners/transports are test ports and cannot
establish authenticity. OS process creation and uninterruptible kernel waits
cannot be strictly bounded; Windows/network and production behavior are untested.
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
from types import MappingProxyType
from typing import Callable, Optional

if __name__ == "__main__":
    spec = importlib.util.spec_from_file_location(
        "_calendar_shfe_product_child", Path(__file__).with_name("calendar_shfe_product_page.py")
    )
    if spec is None or spec.loader is None:
        sys.exit(2)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    MAX_BYTES = module.MAX_BYTES
    ProductPage, ProductRecord = module.ProductPage, module.ProductRecord
    parse_shfe_product_page, _record = module.parse_shfe_product_page, module._record
else:
    from bt_api_ctp.calendar_shfe_product_page import (
        MAX_BYTES,
        ProductPage,
        _record,
        parse_shfe_product_page,
    )

SOURCES = MappingProxyType(
    {
        "CU_FUTURE": "https://www.shfe.com.cn/products/futures/metal/nonferrousmetal/cu_f/",
        "CU_OPTION": "https://www.shfe.com.cn/products/option/nonferrousmetal/cu_o/",
        "BC_OPTION": "https://www.shfe.com.cn/products/option/nonferrousmetal/bc_o/",
    }
)
_HOST = "www.shfe.com.cn"
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
_EXPECTED_FIELDS = {
    "CU_FUTURE": ("future", "SHFE", "CU"),
    "CU_OPTION": ("option", "SHFE", "CU"),
    "BC_OPTION": ("option", "INE", "BC"),
}


@dataclass(frozen=True)
class ProductFetchResult:
    status: str
    transport_status: str
    parse_status: str
    collected_at: datetime
    page: Optional[ProductPage]
    source: str
    transport_assurance: str
    identity_status: str
    verification: str = "UNVERIFIED"
    source_authentication: str = "NOT_PROVEN"
    current_record: str = "UNKNOWN"
    coverage: str = "UNKNOWN"
    publication: str = "NOT_AUTHORIZED"


def _fetch_and_parse(
    source_key,
    connect_timeout,
    read_timeout,
    max_bytes,
    *,
    connection_factory: Callable = http.client.HTTPSConnection,
    parser: Callable = parse_shfe_product_page,
):
    if source_key not in SOURCES:
        return "CHILD_ERROR", None
    url = SOURCES[source_key]
    path = url[len("https://" + _HOST) :]
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
            chunk = response.read(min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                return "RESPONSE_TOO_LARGE", None
        if length is not None and len(data) != int(length):
            return "HTTP_ERROR", None
        if not data:
            return "EMPTY_RESPONSE", None
        try:
            page = parser(source_url=url, html=bytes(data))
            if type(page) is not ProductPage:
                raise ValueError
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
        result = json.dumps(
            {"transport": status, "page": asdict(page) if page is not None else None},
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        return (
            result
            if len(result) <= _MAX_RESULT
            else b'{"transport":"RESULT_TOO_LARGE","page":null}'
        )
    except Exception:
        return b'{"transport":"PARSER_ERROR","page":null}'


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode(output, source_key):
    try:
        if type(output) is not bytes or len(output) > _MAX_RESULT or source_key not in SOURCES:
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
            or data["source_url"] != SOURCES[source_key]
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
        kind = "future" if source_key == "CU_FUTURE" else "option"
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


def _run_child(source_key, connect_timeout, read_timeout, total_timeout, max_bytes):
    deadline = time.monotonic() + total_timeout
    command = [
        sys.executable,
        str(Path(__file__).absolute()),
        "--child",
        source_key,
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
                left = deadline - time.monotonic()
                if left <= 0:
                    return _stop(child, "CHILD_TIMEOUT")
                if not selector.select(left):
                    return _stop(child, "CHILD_TIMEOUT")
                part = os.read(child.stdout.fileno(), min(65536, _MAX_RESULT + 1 - len(output)))
                if not part:
                    break
                output.extend(part)
                if len(output) > _MAX_RESULT:
                    return _stop(child, "RESULT_TOO_LARGE")
        left = deadline - time.monotonic()
        if left <= 0:
            return _stop(child, "CHILD_TIMEOUT")
        try:
            child.wait(timeout=left)
        except subprocess.TimeoutExpired:
            return _stop(child, "CHILD_TIMEOUT")
        if child.returncode != 0:
            return "CHILD_ERROR", None
        return _decode(bytes(output), source_key)
    except Exception:
        return _stop(child, "CHILD_ERROR")
    finally:
        if child.stdout is not None:
            child.stdout.close()


def collect_product_page(
    *,
    source_key,
    connect_timeout=3.0,
    read_timeout=3.0,
    total_timeout=10.0,
    max_bytes=MAX_BYTES,
    runner=_run_child,
):
    """Explicitly request one fixed page. Results are always unverified."""
    if type(source_key) is not str or source_key not in SOURCES:
        raise ValueError("invalid source")
    if any(
        type(t) not in (int, float) or not math.isfinite(t) or not 0 < t <= 60
        for t in (connect_timeout, read_timeout, total_timeout)
    ):
        raise ValueError("invalid timeout")
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES or not callable(runner):
        raise ValueError("invalid fetch settings")
    try:
        transport, page = runner(
            source_key, connect_timeout, read_timeout, total_timeout, max_bytes
        )
        # Revalidate even injected runner results using the same bounded child protocol.
        transport, page = _decode(_encode(transport, page), source_key)
    except Exception:
        transport, page = "CHILD_ERROR", None
    parse = page.status if page is not None else "NOT_OBSERVED"
    identity = "NOT_ASSESSED"
    if page is not None and parse in {"CANDIDATE", "NEEDS_REVIEW"}:
        expected = _EXPECTED_FIELDS[source_key]
        observed = [(row.kind, row.venue, row.symbol) for row in page.records]
        if any(
            any(value is not None and value != want for value, want in zip(item, expected))
            for item in observed
        ):
            identity = "SOURCE_FIELDS_MISMATCH"
        elif any(any(value is None for value in item) for item in observed):
            identity = "SOURCE_FIELDS_INCOMPLETE"
        else:
            identity = "SOURCE_FIELDS_MATCH_UNVERIFIED"
    result = parse if identity == "SOURCE_FIELDS_MATCH_UNVERIFIED" else "UNKNOWN"
    return ProductFetchResult(
        result,
        transport,
        parse,
        datetime.now(timezone.utc),
        page,
        SOURCES[source_key],
        "SUPERVISED_DEFAULT_TLS_POLICY" if runner is _run_child else "INJECTED_UNVERIFIED",
        identity,
    )


if __name__ == "__main__":
    try:
        if len(sys.argv) != 6 or sys.argv[1] != "--child" or sys.argv[2] not in SOURCES:
            raise ValueError
        connect, read, limit = float(sys.argv[3]), float(sys.argv[4]), int(sys.argv[5])
        if (
            any(not math.isfinite(t) or not 0 < t <= 60 for t in (connect, read))
            or not 0 < limit <= MAX_BYTES
        ):
            raise ValueError
        sys.stdout.buffer.write(_encode(*_fetch_and_parse(sys.argv[2], connect, read, limit)))
    except Exception:
        sys.exit(2)
