"""Explicit bounded fetch of the fixed SHFE CU futures navigation page.

Import never starts network. The child sends parsed observations, never HTML;
TLS policy, URL, time and digest do not prove authority, completeness, identity,
freshness or permission. Injection ports are unverified test substitutes.
OS process creation and uninterruptible kernel waits cannot be fully bounded.
SIGTERM, SA_NOCLDWAIT, native child reapers and interruptions before Popen
returns a usable handle are not proven safe or cleaned up.
The fixed transport parallels detail_fetch and must be kept in sync with it.
"""

import contextlib
import http.client
import importlib.util
import json
import math
import os
import selectors
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

if __name__ == "__main__":
    spec = importlib.util.spec_from_file_location(
        "_calendar_shfe_navigation_child",
        Path(__file__).with_name("calendar_shfe_product_navigation.py"),
    )
    if spec is None or spec.loader is None:
        sys.exit(2)
    nav = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = nav
    spec.loader.exec_module(nav)
    MAX_BYTES, MAX_LINKS = nav.MAX_BYTES, nav.MAX_LINKS
    NavigationLink, NavigationResult = nav.NavigationLink, nav.NavigationResult
    parse_shfe_product_navigation, _path_kind = nav.parse_shfe_product_navigation, nav._path_kind
else:
    from bt_api_ctp.calendar_shfe_product_navigation import (
        MAX_BYTES,
        MAX_LINKS,
        NavigationLink,
        NavigationResult,
        _path_kind,
        parse_shfe_product_navigation,
    )

_HOST = "www.shfe.com.cn"
_PATH = "/products/futures/metal/nonferrousmetal/cu_f/"
SOURCE_URL = "https://www.shfe.com.cn" + _PATH
_MAX_RESULT = 1024 * 1024
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
        "SUPERVISOR_UNSUPPORTED",
    }
)
_PARSE = frozenset({"CANDIDATE", "NEEDS_REVIEW", "INVALID_UTF8", "STRUCTURE_CHANGED"})


@dataclass(frozen=True)
class NavigationFetchResult:
    status: str
    transport_status: str
    parse_status: str
    collected_at: datetime
    navigation: Optional[NavigationResult]
    transport_assurance: str
    verification: str = "UNVERIFIED"
    source_authentication: str = "NOT_PROVEN"
    venue: str = "UNKNOWN"
    product_identity: str = "UNKNOWN"
    effective_date: str = "UNKNOWN"
    completeness: str = "NOT_PROVEN"
    permission: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"


def _fetch_and_parse(
    connect_timeout,
    read_timeout,
    max_bytes,
    *,
    connection_factory: Callable = http.client.HTTPSConnection,
    parser: Callable = parse_shfe_product_navigation,
):
    connection = None
    try:
        connection = connection_factory(
            _HOST, timeout=connect_timeout, context=ssl.create_default_context()
        )
        connection.request("GET", _PATH, headers={"Accept-Encoding": "identity"})
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
            navigation = parser(source_url=SOURCE_URL, html=bytes(data))
            if type(navigation) is not NavigationResult:
                raise ValueError
        except Exception:
            return "PARSER_ERROR", None
        return "HTTP_OK", navigation
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


def _encode(status, navigation):
    try:
        result = json.dumps(
            {
                "transport": status,
                "navigation": asdict(navigation) if navigation is not None else None,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        return (
            result
            if len(result) <= _MAX_RESULT
            else b'{"transport":"RESULT_TOO_LARGE","navigation":null}'
        )
    except Exception:
        return b'{"transport":"PARSER_ERROR","navigation":null}'


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode(output):
    try:
        if type(output) is not bytes or len(output) > _MAX_RESULT:
            raise ValueError
        envelope = json.loads(output, object_pairs_hook=_pairs)
        if type(envelope) is not dict or set(envelope) != {"transport", "navigation"}:
            raise ValueError
        status, data = envelope["transport"], envelope["navigation"]
        if type(status) is not str or status not in _TRANSPORT:
            raise ValueError
        if status != "HTTP_OK":
            if data is not None:
                raise ValueError
            return status, None
        fields = {
            "status",
            "source_url",
            "sha256",
            "links",
            "review_reasons",
            "verification",
            "source_authentication",
            "venue",
            "product_identity",
            "effective_date",
            "completeness",
            "permission",
        }
        if type(data) is not dict or set(data) != fields:
            raise ValueError
        digest = data["sha256"]
        if (
            data["status"] not in _PARSE
            or data["source_url"] != SOURCE_URL
            or type(digest) is not str
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or data["verification"] != "UNVERIFIED"
            or data["source_authentication"] != "NOT_PROVEN"
            or any(data[k] != "UNKNOWN" for k in ("venue", "product_identity", "effective_date"))
            or data["completeness"] != "NOT_PROVEN"
            or data["permission"] != "NOT_PROVEN"
            or type(data["links"]) is not list
            or len(data["links"]) > MAX_LINKS
            or type(data["review_reasons"]) is not list
        ):
            raise ValueError
        links, seen, paths, conflict = [], set(), {}, False
        for item in data["links"]:
            if type(item) is not dict or set(item) != {"kind", "label", "path"}:
                raise ValueError
            kind, label, path = item["kind"], item["label"], item["path"]
            if (
                type(label) is not str
                or not label
                or not label.strip()
                or len(label) > 128
                or any(
                    ord(c) < 32 or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF
                    for c in label
                )
                or _path_kind(path) != kind
            ):
                raise ValueError
            key = (kind, label, path)
            if key in seen:
                raise ValueError
            seen.add(key)
            if path in paths and paths[path] != (kind, label):
                conflict = True
            paths[path] = (kind, label)
            links.append(NavigationLink(kind, label, path))
        if data["status"] in {"CANDIDATE", "NEEDS_REVIEW"}:
            if (
                {link.kind for link in links} != {"future", "option"}
                or data["status"] != ("NEEDS_REVIEW" if conflict else "CANDIDATE")
                or data["review_reasons"] != (["DUPLICATE_PATH_CONFLICT"] if conflict else [])
            ):
                raise ValueError
        elif links or data["review_reasons"] != (
            ["NAVIGATION_INVALID"] if data["status"] == "STRUCTURE_CHANGED" else []
        ):
            raise ValueError
        return status, NavigationResult(
            status=data["status"],
            source_url=SOURCE_URL,
            sha256=digest,
            links=tuple(links),
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


class ChildCleanupUncertain(BaseException):
    """Fallback if an unusual exception cannot carry the cleanup marker."""


def _mark_uncertain(exc):
    try:
        exc.child_cleanup = "CHILD_CLEANUP_UNCERTAIN"
        if hasattr(exc, "add_note"):
            exc.add_note("CHILD_CLEANUP_UNCERTAIN")
    except BaseException:
        raise ChildCleanupUncertain("CHILD_CLEANUP_UNCERTAIN") from exc


def _run_child_core(connect_timeout, read_timeout, total_timeout, max_bytes):
    if (
        os.name != "posix"
        or not hasattr(signal, "SIGCHLD")
        or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
        or signal.getsignal(signal.SIGINT) != signal.default_int_handler
        or threading.current_thread() is not threading.main_thread()
        or threading.active_count() != 1
    ):
        return "SUPERVISOR_UNSUPPORTED", None
    interrupted = [False]

    def record_interrupt(signum, frame):
        interrupted[0] = True

    def can_signal(child):
        return (
            child.returncode is None
            and signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
            and signal.getsignal(signal.SIGINT) is record_interrupt
            and threading.current_thread() is threading.main_thread()
            and threading.active_count() == 1
        )

    def stop(child, status):
        if not can_signal(child):
            return "CHILD_CLEANUP_UNCERTAIN", None
        return _stop(child, status)

    handler_changed = False
    try:
        signal.signal(signal.SIGINT, record_interrupt)
        result = _run_owned_child(
            connect_timeout, read_timeout, total_timeout, max_bytes, interrupted, stop
        )
    except BaseException as exc:
        _mark_uncertain(exc)
        raise
    finally:
        if signal.getsignal(signal.SIGINT) is record_interrupt:
            signal.signal(signal.SIGINT, signal.default_int_handler)
        else:
            handler_changed = True
    if interrupted[0]:
        exc = KeyboardInterrupt("CHILD_CLEANUP_UNCERTAIN")
        _mark_uncertain(exc)
        raise exc
    if handler_changed:
        return "CHILD_CLEANUP_UNCERTAIN", None
    return result


def _run_child(connect_timeout, read_timeout, total_timeout, max_bytes):
    try:
        return _run_child_core(connect_timeout, read_timeout, total_timeout, max_bytes)
    except BaseException as exc:
        _mark_uncertain(exc)
        raise


def _run_owned_child(connect_timeout, read_timeout, total_timeout, max_bytes, interrupted, stop):
    deadline = time.monotonic() + total_timeout
    command = [
        sys.executable,
        str(Path(__file__).absolute()),
        "--child",
        str(connect_timeout),
        str(read_timeout),
        str(max_bytes),
    ]
    child = None
    wait_in_progress = False
    cleanup_in_progress = False

    def stop_once(status):
        nonlocal cleanup_in_progress
        cleanup_in_progress = True
        result = stop(child, status)
        cleanup_in_progress = False
        return result

    def interrupt():
        stop_once("CHILD_CLEANUP_UNCERTAIN")
        raise KeyboardInterrupt("CHILD_CLEANUP_UNCERTAIN")

    try:
        if interrupted[0]:
            raise KeyboardInterrupt("CHILD_CLEANUP_UNCERTAIN")
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
        if interrupted[0]:
            interrupt()
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                if interrupted[0]:
                    interrupt()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return stop_once("CHILD_TIMEOUT")
                if not selector.select(min(remaining, 0.2)):
                    continue
                part = os.read(child.stdout.fileno(), min(65536, _MAX_RESULT + 1 - len(output)))
                if interrupted[0]:
                    interrupt()
                if not part:
                    break
                output.extend(part)
                if len(output) > _MAX_RESULT:
                    return stop_once("RESULT_TOO_LARGE")
        while True:
            if interrupted[0]:
                interrupt()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return stop_once("CHILD_TIMEOUT")
            try:
                wait_in_progress = True
                child.wait(timeout=min(remaining, 0.2))
                wait_in_progress = False
                break
            except subprocess.TimeoutExpired:
                wait_in_progress = False
        if interrupted[0]:
            # wait already returned; its PID must not be signalled again.
            raise KeyboardInterrupt("CHILD_CLEANUP_UNCERTAIN")
        if child.returncode != 0:
            return "CHILD_ERROR", None
        return _decode(bytes(output))
    except Exception:
        return (
            ("CHILD_CLEANUP_UNCERTAIN", None)
            if wait_in_progress or cleanup_in_progress
            else stop_once("CHILD_ERROR")
        )
    except BaseException as exc:
        if (
            child is not None
            and not wait_in_progress
            and not cleanup_in_progress
            and not interrupted[0]
            and child.returncode is None
        ):
            with contextlib.suppress(BaseException):
                stop(child, "CHILD_CLEANUP_UNCERTAIN")
        _mark_uncertain(exc)
        raise
    finally:
        if child is not None and child.stdout is not None:
            child.stdout.close()


def collect_navigation(
    *,
    connect_timeout=3.0,
    read_timeout=3.0,
    total_timeout=10.0,
    max_bytes=MAX_BYTES,
    runner=_run_child,
):
    """Fetch only the fixed page; no link following, identity inference or publication."""
    if any(
        type(t) not in (int, float) or not math.isfinite(t) or not 0 < t <= 60
        for t in (connect_timeout, read_timeout, total_timeout)
    ):
        raise ValueError("invalid timeout")
    if type(max_bytes) is not int or not 0 < max_bytes <= MAX_BYTES or not callable(runner):
        raise ValueError("invalid fetch settings")
    try:
        transport, navigation = runner(connect_timeout, read_timeout, total_timeout, max_bytes)
        transport, navigation = _decode(_encode(transport, navigation))
    except Exception:
        transport, navigation = "CHILD_CLEANUP_UNCERTAIN", None
    parsed = navigation.status if navigation is not None else "NOT_OBSERVED"
    status = parsed if parsed in {"CANDIDATE", "NEEDS_REVIEW"} else "UNKNOWN"
    return NavigationFetchResult(
        status,
        transport,
        parsed,
        datetime.now(timezone.utc),
        navigation,
        "SUPERVISED_DEFAULT_TLS_POLICY" if runner is _run_child else "INJECTED_UNVERIFIED",
    )


if __name__ == "__main__":
    try:
        if len(sys.argv) != 5 or sys.argv[1] != "--child":
            raise ValueError
        connect, read, limit = float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
        if (
            any(not math.isfinite(t) or not 0 < t <= 60 for t in (connect, read))
            or not 0 < limit <= MAX_BYTES
        ):
            raise ValueError
        sys.stdout.buffer.write(_encode(*_fetch_and_parse(connect, read, limit)))
    except Exception:
        sys.exit(2)
