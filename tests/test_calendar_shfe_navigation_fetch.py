"""Navigation fetch tests use synthetic responses and test-owned children only."""

import importlib
import json
import signal
import socket
import ssl
import subprocess
import sys
import threading
from dataclasses import replace

import pytest

from bt_api_ctp import calendar_shfe_navigation_fetch as fetch
from bt_api_ctp.calendar_shfe_product_navigation import NavigationLink

FUTURE = "/products/futures/metal/nonferrousmetal/cu_f/"
OPTION = "/products/option/nonferrousmetal/cu_o/"


@pytest.fixture
def supported_fake_host(monkeypatch):
    # Only used with test-owned Popen children; real host checks run separately.
    original = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    monkeypatch.setattr(threading, "active_count", lambda: 1)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, original)


def item(path, label="铜"):
    return f'<li class="isActive"><a href="{path}">{label}</a></li>'


def page(future=None, option=None):
    future = item(FUTURE) if future is None else future
    option = item(OPTION) if option is None else option
    return (
        '<div class="header-subs"><div class="header-sub"><div class="subContent sub">'
        '<div class="subItem">期货<div class="subItemContent"><ul class="itemContent">'
        + future
        + '</ul></div></div><div class="subItem">期权'
        '<div class="subItemContent"><ul class="itemContent">'
        + option
        + "</ul></div></div></div></div></div>"
    ).encode()


class Response:
    def __init__(self, body=None, status=200, headers=None, error=None):
        self.body = page() if body is None else body
        self.status, self.headers, self.error, self.reads = status, headers or {}, error, 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, count):
        self.reads += 1
        if self.error:
            raise self.error
        chunk, self.body = self.body[:count], self.body[count:]
        return chunk


class Connection:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.sock = response, error, self
        self.request_args = self.timeout = None
        self.closed = False

    def request(self, *args, **kwargs):
        if self.error:
            raise self.error
        self.request_args = (args, kwargs)

    def settimeout(self, value):
        self.timeout = value

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def fake(connection, limit=fetch.MAX_BYTES, **kwargs):
    calls = []

    def factory(host, *, timeout, context):
        calls.append((host, timeout, context.verify_mode, context.check_hostname))
        return connection

    result = fetch._fetch_and_parse(2, 3, limit, connection_factory=factory, **kwargs)
    assert calls == [("www.shfe.com.cn", 2, ssl.CERT_REQUIRED, True)]
    assert connection.closed
    return result


def test_fixed_request_and_candidate_are_unverified():
    connection = Connection(Response())
    observed = fake(connection)
    assert connection.request_args == (
        ("GET", FUTURE),
        {"headers": {"Accept-Encoding": "identity"}},
    )
    assert connection.timeout == 3
    result = fetch.collect_navigation(runner=lambda *args: observed)
    assert (
        result.status == result.parse_status == "CANDIDATE" and result.transport_status == "HTTP_OK"
    )
    assert result.transport_assurance == "INJECTED_UNVERIFIED"
    assert result.source_authentication == result.completeness == result.permission == "NOT_PROVEN"
    assert result.venue == result.product_identity == result.effective_date == "UNKNOWN"
    assert result.publication == "NOT_AUTHORIZED"
    assert result.navigation.source_url == fetch.SOURCE_URL
    assert len(result.navigation.links) == 2 and b"<div" not in fetch._encode(*observed)


@pytest.mark.parametrize(
    "response,status",
    [
        (Response(status=302, headers={"Location": "https://evil.invalid/"}), "REDIRECT"),
        (Response(status=503), "HTTP_ERROR"),
        (Response(headers={"Content-Encoding": "gzip"}), "HTTP_ERROR"),
        (Response(headers={"Content-Length": "bad"}), "HTTP_ERROR"),
        (Response(headers={"Content-Length": "1000"}), "RESPONSE_TOO_LARGE"),
        (Response(body=b"abcd", headers={"Content-Length": "5"}), "HTTP_ERROR"),
        (Response(body=b"abcd", headers={"Content-Length": "3"}), "HTTP_ERROR"),
        (Response(body=b"x" * 101), "RESPONSE_TOO_LARGE"),
        (Response(body=b""), "EMPTY_RESPONSE"),
        (Response(error=socket.timeout("private")), "NETWORK_TIMEOUT"),
    ],
)
def test_transport_errors_do_not_follow_or_return_page(response, status):
    assert fake(Connection(response), limit=100) == (status, None)
    if status == "REDIRECT":
        assert response.reads == 0


@pytest.mark.parametrize(
    "error,status",
    [
        (ssl.SSLCertVerificationError("private"), "TLS_ERROR"),
        (socket.timeout("private"), "NETWORK_TIMEOUT"),
        (OSError("private"), "NETWORK_ERROR"),
    ],
)
def test_connection_errors_do_not_expose_details(error, status):
    assert fake(Connection(error=error)) == (status, None)


def test_parser_failure_and_unknown_node_never_look_successful():
    def fail(**kwargs):
        raise RuntimeError("private")

    assert fake(Connection(Response()), parser=fail) == ("PARSER_ERROR", None)
    unknown = page().replace(
        b'<div class="subItem">', b'<section>unknown</section><div class="subItem">', 1
    )
    parsed = fake(Connection(Response(body=unknown)))
    result = fetch.collect_navigation(runner=lambda *args: parsed)
    assert result.status == "UNKNOWN" and result.parse_status == "STRUCTURE_CHANGED"
    assert result.navigation.links == () and result.navigation.review_reasons == (
        "NAVIGATION_INVALID",
    )
    invalid = fake(Connection(Response(body=b"\xff")))
    assert fetch.collect_navigation(runner=lambda *args: invalid).parse_status == "INVALID_UTF8"


def test_conflicts_are_preserved_and_parent_recalculates_status():
    raw = page(future=item(FUTURE) + item(FUTURE, "铜另一名称"))
    transport, nav = fake(Connection(Response(body=raw)))
    result = fetch.collect_navigation(runner=lambda *args: (transport, nav))
    assert result.status == "NEEDS_REVIEW" and len(result.navigation.links) == 3
    assert result.navigation.review_reasons == ("DUPLICATE_PATH_CONFLICT",)
    forged = replace(nav, status="CANDIDATE", review_reasons=())
    assert (
        fetch.collect_navigation(runner=lambda *args: (transport, forged)).transport_status
        == "CHILD_ERROR"
    )


def test_parent_rejects_tampered_protocol_paths_labels_and_duplicates():
    transport, nav = fake(Connection(Response()))
    encoded = fetch._encode(transport, nav)
    assert fetch._decode(encoded) == (transport, nav)
    variants = []
    doc = json.loads(encoded)
    doc["navigation"]["links"][0]["path"] = "https://evil.invalid/"
    variants.append(doc)
    doc = json.loads(encoded)
    doc["navigation"]["links"][0]["kind"] = "option"
    variants.append(doc)
    doc = json.loads(encoded)
    doc["navigation"]["links"][0]["label"] = "x\x1b"
    variants.append(doc)
    doc = json.loads(encoded)
    doc["navigation"]["links"].append(doc["navigation"]["links"][0])
    variants.append(doc)
    doc = json.loads(encoded)
    doc["navigation"]["completeness"] = "PROVEN"
    variants.append(doc)
    doc = json.loads(encoded)
    doc["navigation"]["source_url"] = "https://evil.invalid/"
    variants.append(doc)
    for variant in variants:
        assert fetch._decode(json.dumps(variant).encode()) == ("CHILD_ERROR", None)
    assert fetch._decode(
        b'{"transport":"TLS_ERROR","transport":"HTTP_ERROR","navigation":null}'
    ) == ("CHILD_ERROR", None)
    invalid = replace(nav, links=(NavigationLink("future", "\ud800", FUTURE), nav.links[1]))
    assert fetch.collect_navigation(runner=lambda *args: ("HTTP_OK", invalid)).status == "UNKNOWN"


def test_import_and_invalid_options_start_no_request(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not request")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    importlib.reload(fetch)
    for settings in (
        {"connect_timeout": 0},
        {"read_timeout": float("inf")},
        {"total_timeout": True},
        {"max_bytes": 0},
    ):
        with pytest.raises(ValueError):
            fetch.collect_navigation(runner=forbidden, **settings)


def test_child_timeout_and_output_cap_reap_only_test_children(monkeypatch, supported_fake_host):
    original = subprocess.Popen
    children = []

    def slow(_command, **kwargs):
        child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(fetch.subprocess, "Popen", slow)
    result = fetch.collect_navigation(total_timeout=0.2)
    assert result.status == "UNKNOWN" and result.transport_status == "CHILD_TIMEOUT"
    assert children[0].poll() is not None
    monkeypatch.setattr(fetch, "_MAX_RESULT", 128)

    def noisy(_command, **kwargs):
        child = original(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x'*1000000); sys.stdout.flush()",
            ],
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setattr(fetch.subprocess, "Popen", noisy)
    result = fetch.collect_navigation(total_timeout=3)
    assert result.status == "UNKNOWN" and result.transport_status == "RESULT_TOO_LARGE"
    assert children[-1].poll() is not None


def test_synthetic_child_roundtrip_and_fixed_command(monkeypatch, supported_fake_host):
    parsed = fake(Connection(Response()))
    payload = fetch._encode(*parsed)
    original = subprocess.Popen
    calls = []

    def child(command, **kwargs):
        calls.append((command, kwargs))
        return original(
            [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({payload!r})"], **kwargs
        )

    monkeypatch.setattr(fetch.subprocess, "Popen", child)
    result = fetch.collect_navigation(total_timeout=3)
    assert result.status == "CANDIDATE" and result.source_authentication == "NOT_PROVEN"
    assert calls[0][0][2] == "--child" and len(calls[0][0]) == 6
    assert calls[0][1]["env"] == {} and calls[0][1]["shell"] is False


def test_cleanup_uncertainty_and_invalid_child_args():
    class Stuck:
        def kill(self):
            pass

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("child", timeout)

    assert fetch._stop(Stuck(), "CHILD_TIMEOUT") == ("CHILD_CLEANUP_UNCERTAIN", None)
    child = subprocess.run(
        [sys.executable, fetch.__file__, "--child", "https://evil.invalid", "1", "1"],
        capture_output=True,
        timeout=10,
    )
    assert child.returncode == 2 and not child.stdout


def test_runner_exception_cannot_assert_child_cleanup():
    def broken(*args):
        raise RuntimeError("private")

    result = fetch.collect_navigation(runner=broken)
    assert result.status == "UNKNOWN" and result.transport_status == "CHILD_CLEANUP_UNCERTAIN"


_SIGNAL_SCRIPT = r"""
import os, signal, subprocess, sys, threading
from bt_api_ctp import calendar_shfe_navigation_fetch as fetch
mode = sys.argv[1]
original_popen = subprocess.Popen
original_handler = signal.getsignal(signal.SIGINT)
children = []

def forbidden(*args, **kwargs):
    raise AssertionError("unsupported host started child")

if mode in {"ignored", "sigchld_handler", "custom_int", "thread", "multithread"}:
    if mode == "ignored": signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    if mode == "sigchld_handler": signal.signal(signal.SIGCHLD, lambda *_: None)
    if mode == "custom_int": signal.signal(signal.SIGINT, lambda *_: None)
    fetch.subprocess.Popen = forbidden
    results = []
    def run(): results.append(fetch.collect_navigation(total_timeout=1))
    if mode == "thread":
        t = threading.Thread(target=run); t.start(); t.join()
    elif mode == "multithread":
        event = threading.Event()
        t = threading.Thread(target=event.wait); t.start()
        try: run()
        finally: event.set(); t.join()
    else: run()
    assert results[0].status == "UNKNOWN"
    assert results[0].transport_status == "SUPERVISOR_UNSUPPORTED"
else:
    calls = []
    original_stop = fetch._stop
    def spy(child, status):
        calls.append(status)
        return original_stop(child, status)
    fetch._stop = spy
    def spawn(command, **kwargs):
        code = "pass" if mode in {"after_wait", "systemexit_during_wait"} else "import time; time.sleep(30)"
        child = original_popen([sys.executable, "-c", code], **kwargs)
        children.append(child)
        if mode == "during_popen": os.kill(os.getpid(), signal.SIGINT)
        elif mode == "during_select":
            signal.signal(signal.SIGALRM, lambda *_: os.kill(os.getpid(), signal.SIGINT))
            signal.setitimer(signal.ITIMER_REAL, .15)
        elif mode in {"systemexit", "systemexit_cleanup_failure"}:
            if mode == "systemexit_cleanup_failure":
                child.actual_kill = child.kill
                def fail_kill(): raise OSError("injected")
                child.kill = fail_kill
            def exit_handler(*_): raise SystemExit(37)
            signal.signal(signal.SIGUSR1, exit_handler)
            signal.signal(signal.SIGALRM, lambda *_: os.kill(os.getpid(), signal.SIGUSR1))
            signal.setitimer(signal.ITIMER_REAL, .15)
        elif mode == "after_wait":
            original_wait = child.wait
            def wait(*args, **kwargs):
                status = original_wait(*args, **kwargs)
                os.kill(os.getpid(), signal.SIGINT)
                return status
            child.wait = wait
        elif mode == "systemexit_during_wait":
            original_wait = child.wait
            def wait(*args, **kwargs):
                original_wait(*args, **kwargs)
                raise SystemExit(37)
            child.wait = wait
        elif mode == "no_handle":
            raise KeyboardInterrupt("injected")
        return child
    fetch.subprocess.Popen = spawn
    try:
        try: fetch.collect_navigation(total_timeout=3)
        except BaseException as exc:
            assert type(exc) is (SystemExit if mode.startswith("systemexit") else KeyboardInterrupt)
            assert exc.child_cleanup == "CHILD_CLEANUP_UNCERTAIN"
            if isinstance(exc, SystemExit): assert exc.code == 37
        else: raise AssertionError("interrupt swallowed")
        assert signal.getsignal(signal.SIGINT) == original_handler
        if mode in {"during_popen", "during_select", "systemexit"}:
            assert calls and children[0].poll() is not None
        elif mode == "systemexit_cleanup_failure":
            assert calls and children[0].poll() is None
        elif mode in {"after_wait", "systemexit_during_wait"}:
            assert not calls and children[0].poll() is not None
        else:
            assert not calls and children[0].poll() is None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for child in children:
            if child.poll() is None:
                getattr(child, "actual_kill", child.kill)()
                child.wait(timeout=3)
"""


@pytest.mark.parametrize(
    "mode",
    [
        "ignored",
        "sigchld_handler",
        "custom_int",
        "thread",
        "multithread",
        "during_popen",
        "during_select",
        "after_wait",
        "no_handle",
        "systemexit",
        "systemexit_cleanup_failure",
        "systemexit_during_wait",
    ],
)
def test_signal_supervision_in_isolated_process(mode):
    result = subprocess.run(
        [sys.executable, "-c", _SIGNAL_SCRIPT, mode], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


_HANDLER_SCRIPT = r"""
import os, signal, subprocess, sys
from bt_api_ctp import calendar_shfe_navigation_fetch as fetch
mode = sys.argv[1]
original = subprocess.Popen
children = []
calls = []
external = lambda *_: None
original_stop = fetch._stop
def stop(child, status):
    calls.append(status)
    return original_stop(child, status)
fetch._stop = stop

def spawn(command, **kwargs):
    code = "pass" if mode == "after_wait" else "import time; time.sleep(30)"
    child = original([sys.executable, "-c", code], **kwargs)
    children.append(child)
    if mode in {"timeout", "exception"}:
        signal.signal(signal.SIGINT, external)
    if mode == "exception":
        def exit_handler(*_): raise SystemExit(37)
        signal.signal(signal.SIGUSR1, exit_handler)
        signal.signal(signal.SIGALRM, lambda *_: os.kill(os.getpid(), signal.SIGUSR1))
        signal.setitimer(signal.ITIMER_REAL, .1)
    if mode == "after_wait":
        original_wait = child.wait
        def wait(*args, **kwargs):
            status = original_wait(*args, **kwargs)
            signal.signal(signal.SIGINT, external)
            return status
        child.wait = wait
    return child
fetch.subprocess.Popen = spawn
try:
    if mode == "exception":
        try: fetch.collect_navigation(total_timeout=1)
        except SystemExit as exc:
            assert exc.code == 37 and exc.child_cleanup == "CHILD_CLEANUP_UNCERTAIN"
        else: raise AssertionError("SystemExit swallowed")
    else:
        result = fetch.collect_navigation(total_timeout=.3)
        assert result.status == "UNKNOWN" and result.transport_status == "CHILD_CLEANUP_UNCERTAIN"
    assert signal.getsignal(signal.SIGINT) is external
    assert not calls
    if mode == "after_wait": assert children[0].poll() is not None
    else: assert children[0].poll() is None
finally:
    signal.setitimer(signal.ITIMER_REAL, 0)
    for child in children:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
"""


@pytest.mark.parametrize("mode", ["timeout", "after_wait", "exception"])
def test_changed_signal_handler_is_preserved_and_cleanup_unknown(mode):
    result = subprocess.run(
        [sys.executable, "-c", _HANDLER_SCRIPT, mode], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


_STOP_SCRIPT = r"""
import os, signal, subprocess, sys
from bt_api_ctp import calendar_shfe_navigation_fetch as fetch
mode = sys.argv[1]
original_popen, original_read = subprocess.Popen, os.read
children, kills = [], []
original_handler = signal.getsignal(signal.SIGINT)
def spawn(command, **kwargs):
    code = "import sys,time; sys.stdout.buffer.write(b'x'*1000000); sys.stdout.flush(); time.sleep(30)" if mode == "output" else "import sys,time; sys.stdout.write('x'); sys.stdout.flush(); time.sleep(30)" if mode == "communication" else "import time; time.sleep(30)"
    child = original_popen([sys.executable, "-c", code], **kwargs)
    children.append(child)
    child.actual_kill, child.actual_wait = child.kill, child.wait
    def fake_kill(): kills.append(True)
    def fake_wait(*args, **kwargs): raise SystemExit(37)
    child.kill, child.wait = fake_kill, fake_wait
    if mode == "interrupt":
        signal.signal(signal.SIGALRM, lambda *_: os.kill(os.getpid(), signal.SIGINT))
        signal.setitimer(signal.ITIMER_REAL, .1)
    if mode == "communication":
        def fail_read(fd, count):
            if fd == child.stdout.fileno(): raise OSError("injected")
            return original_read(fd, count)
        os.read = fail_read
    return child
fetch.subprocess.Popen = spawn
if mode in {"output", "communication"}: fetch._MAX_RESULT = 128
try:
    try: fetch.collect_navigation(total_timeout=.3 if mode == "timeout" else 2)
    except SystemExit as exc:
        assert exc.code == 37 and exc.child_cleanup == "CHILD_CLEANUP_UNCERTAIN"
    else: raise AssertionError("missing SystemExit")
    assert len(kills) == 1
    assert signal.getsignal(signal.SIGINT) == original_handler
finally:
    signal.setitimer(signal.ITIMER_REAL, 0)
    os.read = original_read
    for child in children:
        if child.poll() is None:
            child.actual_kill()
            child.actual_wait(timeout=3)
"""


@pytest.mark.parametrize("mode", ["timeout", "output", "interrupt", "communication"])
def test_stop_wait_exception_never_sends_second_signal(mode):
    result = subprocess.run(
        [sys.executable, "-c", _STOP_SCRIPT, mode], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
