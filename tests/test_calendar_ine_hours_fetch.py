"""INE fixed-source fetch: local fakes and owned subprocesses, never network."""

import base64
import hashlib
import json
import socket
import ssl
import subprocess
import sys
from datetime import timezone

import pytest

from bt_api_ctp import _calendar_child_supervisor as supervisor
from bt_api_ctp import calendar_ine_hours_fetch as ine

HTML = (
    "<table><tr><td>期货：合成甲<br>期权：合成甲期权</td><td>"
    "夜盘<br>集合竞价申报 20:55-20:59<br>集合竞价撮合 20:59-21:00<br>"
    "连续竞价交易 21:00-01:00<br>日盘<br>集合竞价申报 08:55-08:59<br>"
    "集合竞价撮合 08:59-09:00<br>连续竞价交易 09:00-10:15</td></tr></table>"
).encode()


class Response:
    def __init__(self, status=200, body=HTML, headers=None, error=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.error = error
        self.reads = 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, size):
        self.reads += 1
        if self.error:
            raise self.error
        value, self.body = self.body[:size], self.body[size:]
        return value


class Connection:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.sock = self
        self.closed = False
        self.request_args = []
        self.read_timeout = None

    def request(self, *args, **kwargs):
        if self.error:
            raise self.error
        self.request_args.append((args, kwargs))

    def getresponse(self):
        return self.response

    def settimeout(self, value):
        self.read_timeout = value

    def close(self):
        self.closed = True


def fetch(connection, limit=1024 * 1024):
    calls = []

    def factory(host, *, timeout, context):
        calls.append((host, timeout, context.verify_mode, context.check_hostname))
        return connection

    result = ine._fetch_bytes(2, 3, limit, connection_factory=factory)
    assert calls == [("www.ine.cn", 2, ssl.CERT_REQUIRED, True)]
    assert connection.closed
    return result


def test_single_request_reuses_exact_bytes_for_separate_future_and_option_candidates():
    connection = Connection(Response())
    transport, raw = fetch(connection)
    assert connection.request_args == [
        (
            ("GET", "/services/calenderandholidays/tradinghours/"),
            {"headers": {"Accept-Encoding": "identity"}},
        )
    ]
    assert connection.read_timeout == 3
    assert (transport, raw) == ("HTTP_OK", HTML)
    assert ine._decode(ine._encode(transport, raw), len(HTML)) == (transport, HTML)
    result = ine.collect_ine_hours(runner=lambda *args: (transport, raw))
    assert result.status == "NEEDS_REVIEW"
    assert result.source == ine.SOURCE == result.page.source_id
    assert result.sha256 == result.page.sha256 == hashlib.sha256(HTML).hexdigest()
    assert result.raw_bytes == HTML and repr(HTML) not in repr(result)
    assert [(product.kind, product.name) for product in result.page.products] == [
        ("future", "合成甲"),
        ("option", "合成甲期权"),
    ]
    assert "CROSS_MIDNIGHT_UNVERIFIED" in result.page.review_reasons
    assert result.verification == "UNVERIFIED" and result.coverage == "NOT_PROVEN"
    assert result.effective_dates == result.association == "NOT_PROVEN"
    assert result.publication == "NOT_AUTHORIZED"
    assert result.transport_assurance == "INJECTED_UNVERIFIED"
    assert result.collected_at.utcoffset() == timezone.utc.utcoffset(result.collected_at)


@pytest.mark.parametrize(
    "response,expected",
    [
        (Response(status=301, headers={"Location": "https://other.invalid/"}), "REDIRECT"),
        (Response(status=503), "HTTP_ERROR"),
        (Response(headers={"Content-Encoding": "gzip"}), "HTTP_ERROR"),
        (Response(headers={"Content-Length": "a"}), "HTTP_ERROR"),
        (Response(headers={"Content-Length": "0" * 21}), "HTTP_ERROR"),
        (Response(headers={"Content-Length": "1000"}), "RESPONSE_TOO_LARGE"),
        (Response(body=b"x" * 101), "RESPONSE_TOO_LARGE"),
        (Response(body=b""), "EMPTY_RESPONSE"),
        (Response(body=b"abc", headers={"Content-Length": "4"}), "NETWORK_ERROR"),
        (Response(error=socket.timeout("secret")), "NETWORK_TIMEOUT"),
    ],
)
def test_http_response_failures_are_not_misreported_as_success(response, expected):
    assert fetch(Connection(response), limit=100) == (expected, None)
    if expected == "REDIRECT":
        assert response.reads == 0


@pytest.mark.parametrize(
    "error,expected",
    [
        (ssl.SSLCertVerificationError("private"), "TLS_ERROR"),
        (socket.timeout("private"), "NETWORK_TIMEOUT"),
        (OSError("private"), "NETWORK_ERROR"),
    ],
)
def test_transport_errors_hide_exception_contents(error, expected):
    assert fetch(Connection(error=error)) == (expected, None)


def test_access_challenge_and_broken_input_keep_evidence_without_claiming_success():
    challenge = "<title>WEB 应用防火墙</title>人机识别".encode()
    result = ine.collect_ine_hours(runner=lambda *args: ("HTTP_OK", challenge))
    assert result.status == "ACCESS_CHALLENGE" and result.page is None
    assert result.raw_bytes == challenge and result.transport_status == "HTTP_OK"
    invalid = ine.collect_ine_hours(runner=lambda *args: ("HTTP_OK", b"\xff"))
    assert invalid.status == "INVALID_UTF8" and invalid.raw_bytes == b"\xff"
    changed = ine.collect_ine_hours(runner=lambda *args: ("HTTP_OK", b"other page"))
    assert changed.status == "STRUCTURE_CHANGED" and changed.raw_bytes == b"other page"
    bom = b"\xef\xbb\xbf" + HTML
    with_bom = ine.collect_ine_hours(runner=lambda *args: ("HTTP_OK", bom))
    assert with_bom.sha256 == hashlib.sha256(bom).hexdigest() and with_bom.raw_bytes == bom


def test_child_rejects_forgery_duplicates_and_supervisor_only_states():
    data = json.loads(ine._encode("HTTP_OK", HTML))
    for name in (
        "CHILD_TIMEOUT",
        "CHILD_CLEANUP_UNCERTAIN",
        "SUPERVISOR_UNSUPPORTED",
        "CHILD_ERROR",
    ):
        assert ine._decode(json.dumps({**data, "transport": name}).encode(), len(HTML)) == (
            "CHILD_ERROR",
            None,
        )
    assert ine._decode(json.dumps({**data, "sha256": "0" * 64}).encode(), len(HTML)) == (
        "CHILD_ERROR",
        None,
    )
    assert ine._decode(json.dumps({**data, "raw_base64": "%%%"}).encode(), len(HTML)) == (
        "CHILD_ERROR",
        None,
    )
    assert ine._decode(ine._encode("HTTP_OK", HTML), len(HTML) - 1) == ("CHILD_ERROR", None)
    assert ine._decode(b'{"schema":1,"schema":1}', 100) == ("CHILD_ERROR", None)
    assert ine._decode(b"{" * (ine._CHILD_LIMIT + 1), 100) == ("CHILD_ERROR", None)
    assert base64.b64decode(data["raw_base64"]) == HTML


@pytest.mark.parametrize(
    "status", ["CHILD_TIMEOUT", "CHILD_CLEANUP_UNCERTAIN", "SUPERVISOR_UNSUPPORTED"]
)
def test_supervisor_statuses_are_preserved_without_candidate(status):
    result = ine.collect_ine_hours(runner=lambda *args: (status, None))
    assert result.status == result.transport_status == status
    assert result.raw_bytes is None and result.page is None


def test_invalid_runner_and_configuration_fail_explicitly():
    with pytest.raises(ValueError):
        ine.collect_ine_hours(runner=lambda *args: ("HTTP_OK", None))
    with pytest.raises(ValueError):
        ine.collect_ine_hours(runner=lambda *args: ("CHILD_TIMEOUT", b"forged"))
    with pytest.raises(ValueError):
        ine.collect_ine_hours(max_bytes=0)
    with pytest.raises(ValueError):
        ine.collect_ine_hours(total_timeout=float("nan"))
    result = ine.collect_ine_hours(runner=lambda *args: 1 / 0)
    assert result.transport_status == "CHILD_CLEANUP_UNCERTAIN"


def test_default_runner_kills_and_reaps_owned_child_on_timeout(
    monkeypatch, calendar_supported_host
):
    original = subprocess.Popen
    children = []

    def launch(command, **kwargs):
        child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append((command, child))
        return child

    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    result = ine.collect_ine_hours(total_timeout=0.1)
    assert result.transport_status == "CHILD_TIMEOUT"
    assert result.transport_assurance == "SUPERVISED_DEFAULT_TLS_POLICY"
    assert len(children) == 1 and children[0][1].poll() is not None
    assert children[0][0][2] == "--child"


def test_supervised_child_pipe_revalidates_exact_source_bytes_without_printing(
    monkeypatch, calendar_supported_host, capsys
):
    original = subprocess.Popen
    payload = ine._encode("HTTP_OK", HTML)
    children = []

    def launch(command, **kwargs):
        code = "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))"
        child = original([sys.executable, "-c", code, payload.hex()], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    result = ine.collect_ine_hours(total_timeout=5)
    assert result.transport_assurance == "SUPERVISED_DEFAULT_TLS_POLICY"
    assert result.status == "NEEDS_REVIEW" and result.raw_bytes == HTML
    assert result.page.sha256 == hashlib.sha256(HTML).hexdigest()
    assert len(children) == 1 and children[0].poll() == 0
    assert capsys.readouterr().out == ""


def test_supervised_child_cannot_claim_that_it_timed_out(monkeypatch, calendar_supported_host):
    original = subprocess.Popen
    payload = json.dumps(
        {"schema": ine._SCHEMA, "transport": "CHILD_TIMEOUT", "sha256": None, "raw_base64": None}
    )

    def launch(command, **kwargs):
        code = "import sys; sys.stdout.write(sys.argv[1])"
        return original([sys.executable, "-c", code, payload], **kwargs)

    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    result = ine.collect_ine_hours(total_timeout=5)
    assert result.transport_status == "CHILD_ERROR" and result.raw_bytes is None
