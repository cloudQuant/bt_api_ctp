"""Experimental, unverified Sina futures detail candidates, never calendar facts.

The default fetch runs in a newly created, separately supervised Python child.
It uses the fixed HTTPS host, certificate verification, no redirects, bounded
response bytes and socket timeouts. The parent imposes a deadline on the child
and kills only that child on timeout. OS process creation and a child stuck in
uninterruptible kernel state cannot be strictly bounded here; cleanup uncertainty
is reported, so a hard process-wide upper bound remains a GAP. This is not wired
into a trading runtime, updater, publisher or account and does not fetch on import.
Only the returned item/value rows leave the child; raw HTML and exception text do
not. Third-party content, venue, trading dates and sessions are never verified.
``collected_at`` is the local UTC time after the fetch runner returns; it is
neither a source update timestamp nor a trading date.

The injectable runner is trusted and may bypass these safeguards; guarantees
apply only to the default runner. Pandas and its HTML parser are optional and
loaded only inside the child parsing path. Windows child behavior is NOT_RUN.
"""

from __future__ import annotations

import http.client
import io
import json
import math
import re
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_HOST = "finance.sina.com.cn"
_SYMBOL = re.compile(r"[A-Za-z]{1,8}[0-9]{3,4}\Z", re.ASCII)
_MAX_BYTES = 1024 * 1024
_MAX_ROWS = 256
_MAX_CELL = 4096
_MAX_RESULT_BYTES = 2 * 1024 * 1024
_STATUSES = frozenset(
    {
        "SUCCESS",
        "EMPTY",
        "MISSING_TRADING_TIME",
        "MISSING_VALUE",
        "SCHEMA_CHANGED",
        "DEPENDENCY_UNAVAILABLE",
        "PARSE_ERROR",
        "HTTP_ERROR",
        "REDIRECT",
        "TLS_ERROR",
        "NETWORK_TIMEOUT",
        "NETWORK_ERROR",
        "RESPONSE_TOO_LARGE",
        "CHILD_TIMEOUT",
        "CHILD_ERROR",
        "CHILD_CLEANUP_UNCERTAIN",
    }
)


@dataclass(frozen=True)
class SinaCandidate:
    status: str
    symbol: str
    collected_at: datetime
    fields: tuple[tuple[str, str], ...] = ()
    source: str = "https://finance.sina.com.cn/futures/quotes/{symbol}.shtml"
    verification: str = "UNVERIFIED"


def _parse_html(body: bytes) -> tuple[str, tuple[tuple[str, str], ...]]:
    if not body:
        return "EMPTY", ()
    try:
        import pandas as pd
    except ImportError:
        return "DEPENDENCY_UNAVAILABLE", ()
    try:
        html = body.decode("gb2312", errors="strict")
        tables = pd.read_html(io.StringIO(html))
    except ImportError:
        return "DEPENDENCY_UNAVAILABLE", ()
    except UnicodeDecodeError:
        return "PARSE_ERROR", ()
    except ValueError:
        return "SCHEMA_CHANGED", ()
    except Exception:
        return "PARSE_ERROR", ()
    if len(tables) <= 6:
        return "SCHEMA_CHANGED", ()
    table = tables[6]
    if len(table.columns) != 6 or len(table) * 3 > _MAX_ROWS:
        return "SCHEMA_CHANGED", ()
    fields = []
    try:
        for index in (0, 2, 4):
            for name, value in table.iloc[:, index : index + 2].itertuples(index=False, name=None):
                name_missing, value_missing = bool(pd.isna(name)), bool(pd.isna(value))
                if name_missing and value_missing:
                    continue
                if name_missing or value_missing:
                    return "MISSING_VALUE", tuple(fields)
                if (
                    isinstance(name, str)
                    and name.strip() == "交易时间"
                    and not isinstance(value, str)
                ):
                    return "MISSING_VALUE", tuple(fields)
                name, value = str(name), str(value)
                if not name.strip() or not value.strip():
                    return "MISSING_VALUE", tuple(fields)
                if len(name) > _MAX_CELL or len(value) > _MAX_CELL:
                    return "SCHEMA_CHANGED", ()
                fields.append((name, value))
    except Exception:
        return "SCHEMA_CHANGED", ()
    if not fields:
        return "EMPTY", ()
    names = [name.strip() for name, _ in fields]
    if len(names) != len(set(names)):
        return "SCHEMA_CHANGED", tuple(fields)
    if "交易时间" not in names:
        return "MISSING_TRADING_TIME", tuple(fields)
    return "SUCCESS", tuple(fields)


def _fetch(
    symbol: str,
    connect_timeout: float,
    read_timeout: float,
    max_bytes: int,
    connection_factory: Callable = http.client.HTTPSConnection,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Fetch only the fixed public source; no auth, redirects or HTML return."""
    connection = None
    try:
        context = ssl.create_default_context()
        connection = connection_factory(_HOST, timeout=connect_timeout, context=context)
        connection.request(
            "GET", f"/futures/quotes/{symbol}.shtml", headers={"Accept-Encoding": "identity"}
        )
        if connection.sock is not None:
            connection.sock.settimeout(read_timeout)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            return "REDIRECT", ()
        if response.status != 200:
            return "HTTP_ERROR", ()
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            return "HTTP_ERROR", ()
        length = response.getheader("Content-Length")
        if length is not None:
            if not length.isascii() or not length.isdecimal():
                return "HTTP_ERROR", ()
            if int(length) > max_bytes:
                return "RESPONSE_TOO_LARGE", ()
        data = bytearray()
        while True:
            chunk = response.read(min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                return "RESPONSE_TOO_LARGE", ()
        return _parse_html(bytes(data))
    except ssl.SSLError:
        return "TLS_ERROR", ()
    except (TimeoutError, socket.timeout):
        return "NETWORK_TIMEOUT", ()
    except Exception:
        return "NETWORK_ERROR", ()
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _child_payload(
    symbol: str, connect_timeout: float, read_timeout: float, max_bytes: int
) -> bytes:
    status, fields = _fetch(symbol, connect_timeout, read_timeout, max_bytes)
    payload = json.dumps({"status": status, "fields": fields}, ensure_ascii=True).encode("ascii")
    if len(payload) > _MAX_RESULT_BYTES:
        return b'{"status":"SCHEMA_CHANGED","fields":[]}'
    return payload


def _run_child(
    symbol: str, connect_timeout: float, read_timeout: float, total_timeout: float, max_bytes: int
) -> tuple[str, tuple[tuple[str, str], ...]]:
    deadline = time.monotonic() + total_timeout
    command = [
        sys.executable,
        str(Path(__file__).absolute()),
        "--child",
        symbol,
        str(connect_timeout),
        str(read_timeout),
        str(max_bytes),
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
            shell=False,
        )
    except Exception:
        return "CHILD_ERROR", ()
    try:
        output, _ = process.communicate(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.communicate(timeout=1.0)
        except Exception:
            return "CHILD_CLEANUP_UNCERTAIN", ()
        return "CHILD_TIMEOUT", ()
    except Exception:
        try:
            process.kill()
            process.communicate(timeout=1.0)
        except Exception:
            return "CHILD_CLEANUP_UNCERTAIN", ()
        return "CHILD_ERROR", ()
    if process.returncode != 0 or len(output) > _MAX_RESULT_BYTES:
        return "CHILD_ERROR", ()
    try:
        payload = json.loads(output)
        if type(payload) is not dict or set(payload) != {"status", "fields"}:
            return "CHILD_ERROR", ()
        status, rows = payload["status"], payload["fields"]
        if type(status) is not str or status not in _STATUSES or type(rows) is not list:
            return "CHILD_ERROR", ()
        if len(rows) > _MAX_ROWS or any(
            type(row) is not list
            or len(row) != 2
            or any(type(cell) is not str or len(cell) > _MAX_CELL for cell in row)
            for row in rows
        ):
            return "CHILD_ERROR", ()
        return status, tuple((row[0], row[1]) for row in rows)
    except (ValueError, TypeError):
        return "CHILD_ERROR", ()


def collect_sina_candidate(
    *,
    symbol: str,
    connect_timeout: float = 3.0,
    read_timeout: float = 3.0,
    total_timeout: float = 10.0,
    max_bytes: int = _MAX_BYTES,
    runner: Callable = _run_child,
) -> SinaCandidate:
    """Collect one unverified public detail record via a supervised child."""
    if type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None:
        raise ValueError("invalid futures symbol")
    for timeout in (connect_timeout, read_timeout, total_timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("invalid timeout")
    if type(max_bytes) is not int or not 0 < max_bytes <= _MAX_BYTES:
        raise ValueError("invalid response size limit")
    if not callable(runner):
        raise ValueError("invalid runner")
    status, fields = runner(symbol, connect_timeout, read_timeout, total_timeout, max_bytes)
    if status not in _STATUSES:
        raise ValueError("invalid runner status")
    return SinaCandidate(
        status=status, symbol=symbol, fields=fields, collected_at=datetime.now(timezone.utc)
    )


if __name__ == "__main__":
    # This private child entrypoint only accepts validated scalar parameters;
    # no shell, input program or arbitrary endpoint is accepted.
    try:
        if len(sys.argv) != 6 or sys.argv[1] != "--child":
            raise ValueError("invalid child arguments")
        symbol_arg = sys.argv[2]
        connect_arg, read_arg = float(sys.argv[3]), float(sys.argv[4])
        size_arg = int(sys.argv[5])
        if (
            _SYMBOL.fullmatch(symbol_arg) is None
            or any(not math.isfinite(n) or not 0 < n <= 60 for n in (connect_arg, read_arg))
            or not 0 < size_arg <= _MAX_BYTES
        ):
            raise ValueError("invalid child arguments")
        sys.stdout.buffer.write(_child_payload(symbol_arg, connect_arg, read_arg, size_arg))
    except Exception:
        sys.exit(2)
