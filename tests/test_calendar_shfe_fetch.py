"""Synthetic HTML and fake transports only; these tests never access a network."""

import json
import socket
import ssl
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from bt_api_ctp import calendar_shfe_fetch as fetcher
from bt_api_ctp.calendar_shfe_page import PageCandidate

HTML = (
    "<table><tr><td>期货：合成甲<br>期权：合成甲期权</td><td>"
    "日盘<br>集合竞价申报 08:55-08:59<br>集合竞价撮合 08:59-09:00<br>"
    "连续竞价交易 09:00-10:15<br>连续竞价交易 10:30-11:30<br>"
    "连续竞价交易 13:30-15:00</td></tr></table>"
).encode()


class Response:
    def __init__(self, status=200, body=HTML, headers=None, read_error=None):
        self.status, self.body, self.headers, self.read_error = (
            status,
            body,
            headers or {},
            read_error,
        )
        self.reads = 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, size):
        self.reads += 1
        if self.read_error:
            raise self.read_error
        result, self.body = self.body[:size], self.body[size:]
        return result


class Connection:
    def __init__(self, response=None, error=None):
        self.sock = self
        self.response = response
        self.error = error
        self.request_args = None
        self.read_timeout = None
        self.closed = False

    def request(self, *args, **kwargs):
        if self.error:
            raise self.error
        self.request_args = (args, kwargs)

    def settimeout(self, value):
        self.read_timeout = value

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def fake_fetch(connection, limit=1024 * 1024, parser=fetcher.parse_shfe_page):
    calls = []

    def factory(host, *, timeout, context):
        calls.append((host, timeout, context.verify_mode, context.check_hostname))
        return connection

    result = fetcher._fetch_and_parse(2, 3, limit, connection_factory=factory, parser=parser)
    assert calls == [("www.shfe.com.cn", 2, ssl.CERT_REQUIRED, True)]
    assert connection.closed
    return result


def test_fixed_https_source_fetches_into_real_parser_without_official_fact_claim():
    conn = Connection(Response())
    transport, page = fake_fetch(conn)
    assert transport == "HTTP_OK" and page.status == "CANDIDATE"
    assert conn.request_args == (
        ("GET", "/services/calenderandholidays/tradinghours/"),
        {"headers": {"Accept-Encoding": "identity"}},
    )
    assert conn.read_timeout == 3 and page.source_id == fetcher._SOURCE
    assert [(p.kind, p.name) for p in page.products] == [
        ("future", "合成甲"),
        ("option", "合成甲期权"),
    ]
    assert page.verification == "UNVERIFIED"
    assert b"<table>" not in repr(page).encode()
    assert not hasattr(page.products[0], "venue")


@pytest.mark.parametrize(
    "response,status",
    [
        (Response(status=302, headers={"Location": "https://other.invalid/"}), "REDIRECT"),
        (Response(status=503), "HTTP_ERROR"),
        (Response(headers={"Content-Encoding": "gzip"}), "HTTP_ERROR"),
        (Response(headers={"Content-Length": "x"}), "HTTP_ERROR"),
        (Response(headers={"Content-Length": "0" * 21}), "HTTP_ERROR"),
        (Response(headers={"Content-Length": "1000"}), "RESPONSE_TOO_LARGE"),
        (Response(body=b"x" * 101), "RESPONSE_TOO_LARGE"),
        (Response(body=b""), "EMPTY_RESPONSE"),
        (Response(read_error=socket.timeout("sensitive")), "NETWORK_TIMEOUT"),
    ],
)
def test_http_status_and_read_size_failures_are_distinct(response, status):
    conn = Connection(response)
    result = fake_fetch(conn, limit=100)
    assert result == (status, None)
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
def test_transport_errors_never_expose_exception(error, status):
    result = fake_fetch(Connection(error=error))
    assert result == (status, None) and "private" not in repr(result)


def test_parser_statuses_are_not_misrepresented_as_success():
    transport, page = fake_fetch(Connection(Response(body=b"site changed")))
    assert transport == "HTTP_OK" and page.status == "STRUCTURE_CHANGED"
    assert page.products == () and page.verification == "UNVERIFIED"
    transport, page = fake_fetch(Connection(Response(body=b"\xff")))
    assert transport == "HTTP_OK" and page.status == "INVALID_UTF8"


def test_parser_exception_is_classified_without_outputting_raw_details():
    def parser(**kwargs):
        raise ValueError("private body")

    assert fake_fetch(Connection(Response()), parser=parser) == ("PARSER_ERROR", None)


def test_roundtrip_preserves_parser_candidate_and_status():
    transport, page = fake_fetch(Connection(Response()))
    restored_status, restored = fetcher._decode_result(fetcher._encode_result(transport, page))
    assert restored_status == transport and restored == page
    result = fetcher.collect_shfe_page(runner=lambda *args: (restored_status, restored))
    assert result.status == page.status and result.transport_status == "HTTP_OK"
    assert result.collected_at.utcoffset() == timedelta(0)
    assert result.collected_at <= datetime.now(timezone.utc)
    assert result.verification == "UNVERIFIED"


def test_structure_changed_is_exposed_separately_from_http_success():
    page = fetcher.parse_shfe_page(source_id=fetcher._SOURCE, html=b"changed")
    result = fetcher.collect_shfe_page(runner=lambda *args: ("HTTP_OK", page))
    assert result.transport_status == "HTTP_OK" and result.status == "STRUCTURE_CHANGED"
    assert result.page.status == "STRUCTURE_CHANGED"


def test_unparseable_or_inconsistent_child_protocol_is_explicit_error():
    assert fetcher._decode_result(b"garbage") == ("CHILD_ERROR", None)
    assert fetcher._decode_result(json.dumps({"transport": "HTTP_OK", "page": None}).encode()) == (
        "CHILD_ERROR",
        None,
    )
    assert fetcher._decode_result(json.dumps({"transport": "TLS_ERROR", "page": {}}).encode()) == (
        "CHILD_ERROR",
        None,
    )


def test_output_amplification_has_explicit_bound():
    _, page = fake_fetch(Connection(Response()))
    enlarged = PageCandidate(
        status=page.status,
        source_id=page.source_id,
        sha256=page.sha256,
        products=page.products * 600,
    )
    assert fetcher._decode_result(fetcher._encode_result("HTTP_OK", enlarged)) == (
        "RESULT_TOO_LARGE",
        None,
    )


def test_parent_enforces_child_deadline_and_reaps_owned_child(monkeypatch):
    class SlowChild:
        returncode = None

        def __init__(self):
            self.killed = False
            self.calls = 0

        def communicate(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("child", timeout)
            return b"", b""

        def kill(self):
            self.killed = True

    child = SlowChild()
    launched = []

    def popen(command, **kwargs):
        launched.append((command, kwargs))
        return child

    monkeypatch.setattr(fetcher.subprocess, "Popen", popen)
    result = fetcher.collect_shfe_page(total_timeout=0.1)
    assert result.status == result.transport_status == "CHILD_TIMEOUT"
    assert child.killed and child.calls == 2
    assert launched[0][1]["shell"] is False and launched[0][1]["env"] == {}
    assert result.verification == "UNVERIFIED"


def test_local_owned_sleeper_is_killed_and_reaped_on_timeout(monkeypatch):
    original = subprocess.Popen
    spawned = []

    def launch_sleeper(_command, **kwargs):
        child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(fetcher.subprocess, "Popen", launch_sleeper)
    result = fetcher.collect_shfe_page(total_timeout=0.2)
    assert result.status == "CHILD_TIMEOUT"
    assert len(spawned) == 1 and spawned[0].poll() is not None


def test_uncertain_child_cleanup_is_not_misreported(monkeypatch):
    class StuckChild:
        def communicate(self, timeout):
            raise subprocess.TimeoutExpired("child", timeout)

        def kill(self):
            pass

    monkeypatch.setattr(fetcher.subprocess, "Popen", lambda *a, **k: StuckChild())
    assert fetcher.collect_shfe_page(total_timeout=0.1).status == "CHILD_CLEANUP_UNCERTAIN"


def test_child_non_timeout_communication_error_is_not_called_timeout(monkeypatch):
    class BrokenChild:
        calls = 0

        def communicate(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise OSError("private")
            return b"", b""

        def kill(self):
            pass

    monkeypatch.setattr(fetcher.subprocess, "Popen", lambda *a, **k: BrokenChild())
    assert fetcher.collect_shfe_page().status == "CHILD_ERROR"


def test_real_child_bootstrap_rejects_bad_arguments_without_network():
    result = subprocess.run(
        [sys.executable, fetcher.__file__, "--invalid"],
        env={},
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 2 and result.stdout == b""


@pytest.mark.parametrize(
    "kwargs",
    [
        {"connect_timeout": 0},
        {"read_timeout": float("inf")},
        {"total_timeout": True},
        {"max_bytes": 0},
        {"max_bytes": fetcher.MAX_BYTES + 1},
    ],
)
def test_invalid_settings_do_not_call_runner(kwargs):
    calls = []
    with pytest.raises(ValueError):
        fetcher.collect_shfe_page(runner=lambda *args: calls.append(args), **kwargs)
    assert calls == []
