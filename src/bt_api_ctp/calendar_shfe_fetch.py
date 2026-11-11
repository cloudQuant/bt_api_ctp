# Keep annotations resolvable by typing.get_type_hints on Python 3.9.
# ruff: noqa: UP045
"""Experimental bounded fetch of the public SHFE trading-hours page.

Only the fixed HTTPS endpoint is requested, with default certificate and host
verification, no redirect following, socket timeouts and a response byte limit.
Fetching and the existing page parser run inside an owned child process; the
parent supervises and kills only that child on timeout. Process creation and
uninterruptible kernel waits cannot be strictly bounded; an unreaped child is
reported explicitly. Windows, installation and power-failure behavior are not
verified. An injected runner or transport is trusted and may bypass safeguards.

The official URL, verified TLS, successful fetch and source hash do not prove
publication authority, freshness, venue ownership, effective periods, or
complete coverage. Parsed results remain UNVERIFIED and are never published or
wired into trading behavior here. No requests occur on import. Only parsed
candidates, never raw HTML or exception text, are returned from the child.
``collected_at`` is a local UTC observation after the runner finishes, not an
exchange publication time or trading date.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import math
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# Direct child execution loads the adjacent parser without importing provider
# session modules. The path is derived from this file, never from user input.
if __name__ == "__main__":
    _spec = importlib.util.spec_from_file_location(
        "_calendar_shfe_page_child", Path(__file__).with_name("calendar_shfe_page.py")
    )
    if _spec is None or _spec.loader is None:
        sys.exit(2)
    _parser_module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _parser_module
    _spec.loader.exec_module(_parser_module)
    PageCandidate = _parser_module.PageCandidate
    ProductCandidate = _parser_module.ProductCandidate
    StageCandidate = _parser_module.StageCandidate
    parse_shfe_page = _parser_module.parse_shfe_page
    MAX_BYTES = _parser_module.MAX_BYTES
else:
    from bt_api_ctp.calendar_shfe_page import (
        MAX_BYTES,
        PageCandidate,
        ProductCandidate,
        StageCandidate,
        parse_shfe_page,
    )

_HOST = "www.shfe.com.cn"
_PATH = "/services/calenderandholidays/tradinghours/"
_SOURCE = "https://www.shfe.com.cn/services/calenderandholidays/tradinghours/"
_MAX_RESULT = 2 * 1024 * 1024
_MAX_TEXT_TOTAL = 256 * 1024
_MAX_PRODUCTS = 1024
_MAX_STAGES = 256
_MAX_TOTAL_STAGES = 4096
_TRANSPORT_STATUSES = frozenset(
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
_PAGE_STATUSES = frozenset(
    {
        "CANDIDATE",
        "NEEDS_REVIEW",
        "CONFLICT",
        "STRUCTURE_CHANGED",
        "INVALID_UTF8",
        "INVALID_SOURCE",
        "INVALID_INPUT",
        "INPUT_TOO_LARGE",
    }
)


@dataclass(frozen=True)
class ShfeFetchResult:
    status: str
    transport_status: str
    collected_at: datetime
    page: Optional[PageCandidate] = None
    source: str = _SOURCE
    verification: str = "UNVERIFIED"


def _fetch_and_parse(
    connect_timeout: float,
    read_timeout: float,
    max_bytes: int,
    *,
    connection_factory: Callable = http.client.HTTPSConnection,
    parser: Callable = parse_shfe_page,
) -> tuple[str, Optional[PageCandidate]]:
    connection = None
    try:
        context = ssl.create_default_context()
        connection = connection_factory(_HOST, timeout=connect_timeout, context=context)
        connection.request("GET", _PATH, headers={"Accept-Encoding": "identity"})
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
        if not data:
            return "EMPTY_RESPONSE", None
        try:
            page = parser(source_id=_SOURCE, html=bytes(data))
        except Exception:
            return "PARSER_ERROR", None
        if (
            type(page) is not PageCandidate
            or page.source_id != _SOURCE
            or page.status not in _PAGE_STATUSES
            or page.verification != "UNVERIFIED"
        ):
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


def _encode_result(status: str, page: Optional[PageCandidate]) -> bytes:
    if page is not None:
        if (
            len(page.products) > _MAX_PRODUCTS
            or sum(len(product.stages) for product in page.products) > _MAX_TOTAL_STAGES
        ):
            status, page = "RESULT_TOO_LARGE", None
        else:
            size = sum(
                len(p.name)
                + len(p.raw_product_cell)
                + len(p.raw_time_cell)
                + sum(len(s.raw_line) for s in p.stages)
                for p in page.products
            )
            size += sum(len(note) for note in page.raw_notes)
            if size > _MAX_TEXT_TOTAL or any(len(p.stages) > _MAX_STAGES for p in page.products):
                status, page = "RESULT_TOO_LARGE", None
    payload = json.dumps(
        {"transport": status, "page": asdict(page) if page else None}, ensure_ascii=True
    ).encode("ascii")
    if len(payload) > _MAX_RESULT:
        return b'{"transport":"RESULT_TOO_LARGE","page":null}'
    return payload


def _bounded_string(value: object, limit: int = 8192) -> str:
    if type(value) is not str or len(value) > limit:
        raise ValueError("invalid child payload")
    return value


def _decode_result(output: bytes) -> tuple[str, Optional[PageCandidate]]:
    try:
        envelope = json.loads(output)
        if type(envelope) is not dict or set(envelope) != {"transport", "page"}:
            raise ValueError
        status, data = envelope["transport"], envelope["page"]
        if type(status) is not str or status not in _TRANSPORT_STATUSES:
            raise ValueError
        if status != "HTTP_OK":
            if data is not None:
                raise ValueError
            return status, None
        if type(data) is not dict or set(data) != {
            "status",
            "source_id",
            "sha256",
            "products",
            "raw_notes",
            "review_reasons",
            "verification",
        }:
            raise ValueError
        if (
            data["status"] not in _PAGE_STATUSES
            or data["source_id"] != _SOURCE
            or data["verification"] != "UNVERIFIED"
        ):
            raise ValueError
        digest = data["sha256"]
        if digest is not None and (
            type(digest) is not str
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError
        if type(data["products"]) is not list or len(data["products"]) > _MAX_PRODUCTS:
            raise ValueError
        if type(data["raw_notes"]) is not list or len(data["raw_notes"]) > 256:
            raise ValueError
        if type(data["review_reasons"]) is not list or len(data["review_reasons"]) > 256:
            raise ValueError
        products = []
        for product in data["products"]:
            if (
                type(product) is not dict
                or set(product) != {"kind", "name", "raw_product_cell", "raw_time_cell", "stages"}
                or product["kind"] not in {"future", "option"}
            ):
                raise ValueError
            if type(product["stages"]) is not list or len(product["stages"]) > _MAX_STAGES:
                raise ValueError
            stages = []
            for stage in product["stages"]:
                if type(stage) is not dict or set(stage) != {
                    "session",
                    "phase",
                    "start",
                    "end",
                    "day_transition",
                    "raw_line",
                }:
                    raise ValueError
                stages.append(
                    StageCandidate(**{key: _bounded_string(value) for key, value in stage.items()})
                )
            products.append(
                ProductCandidate(
                    kind=product["kind"],
                    name=_bounded_string(product["name"]),
                    raw_product_cell=_bounded_string(product["raw_product_cell"]),
                    raw_time_cell=_bounded_string(product["raw_time_cell"]),
                    stages=tuple(stages),
                )
            )
        page = PageCandidate(
            status=data["status"],
            source_id=_SOURCE,
            sha256=digest,
            products=tuple(products),
            raw_notes=tuple(_bounded_string(note) for note in data["raw_notes"]),
            review_reasons=tuple(_bounded_string(reason, 128) for reason in data["review_reasons"]),
        )
        return status, page
    except Exception:
        return "CHILD_ERROR", None


def _run_child(
    connect_timeout: float, read_timeout: float, total_timeout: float, max_bytes: int
) -> tuple[str, Optional[PageCandidate]]:
    deadline = time.monotonic() + total_timeout
    command = [
        sys.executable,
        str(Path(__file__).absolute()),
        "--child",
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
        output, _ = child.communicate(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        try:
            child.kill()
            child.communicate(timeout=1.0)
        except Exception:
            return "CHILD_CLEANUP_UNCERTAIN", None
        return "CHILD_TIMEOUT", None
    except Exception:
        try:
            child.kill()
            child.communicate(timeout=1.0)
        except Exception:
            return "CHILD_CLEANUP_UNCERTAIN", None
        return "CHILD_ERROR", None
    if child.returncode != 0 or len(output) > _MAX_RESULT:
        return "CHILD_ERROR", None
    return _decode_result(output)


def collect_shfe_page(
    *,
    connect_timeout: float = 3.0,
    read_timeout: float = 3.0,
    total_timeout: float = 10.0,
    max_bytes: int = MAX_BYTES,
    runner: Callable = _run_child,
) -> ShfeFetchResult:
    """Fetch and parse one official-page candidate without publishing it."""
    for timeout in (connect_timeout, read_timeout, total_timeout):
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("invalid timeout")
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES or not callable(runner):
        raise ValueError("invalid fetch settings")
    transport, page = runner(connect_timeout, read_timeout, total_timeout, max_bytes)
    if transport not in _TRANSPORT_STATUSES or (transport == "HTTP_OK") != (page is not None):
        raise ValueError("invalid runner result")
    if page is not None and (
        type(page) is not PageCandidate
        or page.verification != "UNVERIFIED"
        or page.source_id != _SOURCE
        or page.status not in _PAGE_STATUSES
    ):
        raise ValueError("invalid parser candidate")
    return ShfeFetchResult(
        status=page.status if page else transport,
        transport_status=transport,
        collected_at=datetime.now(timezone.utc),
        page=page,
    )


if __name__ == "__main__":
    try:
        if len(sys.argv) != 5 or sys.argv[1] != "--child":
            raise ValueError
        connect, read = float(sys.argv[2]), float(sys.argv[3])
        limit = int(sys.argv[4])
        if (
            any(not math.isfinite(value) or not 0 < value <= 60 for value in (connect, read))
            or not 0 < limit <= MAX_BYTES
        ):
            raise ValueError
        sys.stdout.buffer.write(_encode_result(*_fetch_and_parse(connect, read, limit)))
    except Exception:
        sys.exit(2)
