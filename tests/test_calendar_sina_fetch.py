"""Only synthetic HTML, HTTP fakes and a local owned sleeper are used."""

import builtins
import json
import socket
import ssl
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from bt_api_ctp import calendar_sina_fetch as sina


def html(last=None):
    generic = "<table><tr><td>x</td></tr></table>"
    if last is None:
        last = (
            "<table><tr><td>交易时间</td><td>09:00-10:15 10:30-11:30</td>"
            "<td>名称</td><td>合成期货</td><td>备注</td><td>合成样例</td></tr></table>"
        )
    return (generic * 6 + last).encode("gb2312")


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}
        self.read_count = 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, size):
        self.read_count += 1
        result, self.body = self.body[:size], self.body[size:]
        return result


class FakeConnection:
    def __init__(self, response=None, failure=None):
        self.response = response
        self.failure = failure
        self.sock = self
        self.closed = False
        self.timeout = None
        self.requested = None

    def request(self, *args, **kwargs):
        if self.failure:
            raise self.failure
        self.requested = (args, kwargs)

    def settimeout(self, value):
        self.timeout = value

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def fetch_with(connection, *, limit=1024 * 1024):
    observations = []

    def factory(host, *, timeout, context):
        observations.append((host, timeout, context.verify_mode, context.check_hostname))
        return connection

    result = sina._fetch("RB2701", 2, 3, limit, factory)
    assert observations == [("finance.sina.com.cn", 2, ssl.CERT_REQUIRED, True)]
    assert connection.closed
    return result


def test_https_fixed_host_verified_and_parsed_without_raw_page():
    connection = FakeConnection(FakeResponse(body=html()))
    status, fields = fetch_with(connection)
    assert status == "SUCCESS"
    assert fields == (
        ("交易时间", "09:00-10:15 10:30-11:30"),
        ("名称", "合成期货"),
        ("备注", "合成样例"),
    )
    assert connection.timeout == 3
    assert connection.requested == (
        ("GET", "/futures/quotes/RB2701.shtml"),
        {"headers": {"Accept-Encoding": "identity"}},
    )
    assert "<table>" not in repr(fields)


@pytest.mark.parametrize(
    "response,status",
    [
        (FakeResponse(status=302, headers={"Location": "https://malicious.invalid"}), "REDIRECT"),
        (FakeResponse(status=500), "HTTP_ERROR"),
        (FakeResponse(headers={"Content-Length": "1000"}), "RESPONSE_TOO_LARGE"),
        (FakeResponse(headers={"Content-Length": "NaN"}), "HTTP_ERROR"),
        (FakeResponse(headers={"Content-Encoding": "gzip"}), "HTTP_ERROR"),
        (FakeResponse(body=b"a" * 101), "RESPONSE_TOO_LARGE"),
        (FakeResponse(body=b""), "EMPTY"),
        (FakeResponse(body=b"not a table"), "SCHEMA_CHANGED"),
    ],
)
def test_http_errors_redirects_and_size_limits(response, status):
    connection = FakeConnection(response)
    result = fetch_with(connection, limit=100)
    assert result[0] == status
    if status == "REDIRECT":
        assert response.read_count == 0


@pytest.mark.parametrize(
    "failure,status",
    [
        (ssl.SSLCertVerificationError("sensitive"), "TLS_ERROR"),
        (socket.timeout("sensitive"), "NETWORK_TIMEOUT"),
        (OSError("sensitive"), "NETWORK_ERROR"),
    ],
)
def test_transport_failures_are_finite_and_hide_details(failure, status):
    result = fetch_with(FakeConnection(failure=failure))
    assert result == (status, ())
    assert "sensitive" not in repr(result)


def test_slow_response_read_is_reported_as_timeout():
    class SlowResponse(FakeResponse):
        def read(self, size):
            raise socket.timeout("private details")

    assert fetch_with(FakeConnection(SlowResponse())) == ("NETWORK_TIMEOUT", ())


@pytest.mark.parametrize(
    "body,status",
    [
        (html("<table><tr><td>new structure</td></tr></table>"), "SCHEMA_CHANGED"),
        (
            html(
                "<table><tr><td>交易时间</td><td></td><td>名称</td><td>A</td>"
                "<td>备注</td><td>B</td></tr></table>"
            ),
            "MISSING_VALUE",
        ),
        (
            html(
                "<table><tr><td>交易时间</td><td>123</td><td>名称</td><td>A</td>"
                "<td>备注</td><td>B</td></tr></table>"
            ),
            "MISSING_VALUE",
        ),
        (
            html(
                "<table><tr><td>名称</td><td>A</td><td>备注</td><td>B</td>"
                "<td>信息</td><td>C</td></tr></table>"
            ),
            "MISSING_TRADING_TIME",
        ),
        (
            html(
                "<table><tr><td>交易时间</td><td>A</td><td>交易时间</td><td>B</td>"
                "<td>信息</td><td>C</td></tr></table>"
            ),
            "SCHEMA_CHANGED",
        ),
        (b"\xff\xff", "PARSE_ERROR"),
    ],
)
def test_html_structure_missing_values_and_encoding(body, status):
    assert sina._parse_html(body)[0] == status


def test_optional_pandas_not_installed(monkeypatch):
    original = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pandas":
            raise ModuleNotFoundError("pandas")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert sina._parse_html(html()) == ("DEPENDENCY_UNAVAILABLE", ())


def test_optional_html_parser_not_installed(monkeypatch):
    import pandas

    def fail(*args, **kwargs):
        raise ImportError("optional parser")

    monkeypatch.setattr(pandas, "read_html", fail)
    assert sina._parse_html(html()) == ("DEPENDENCY_UNAVAILABLE", ())


def test_injected_runner_receives_validated_input_and_result_unverified():
    calls = []

    def runner(*args):
        calls.append(args)
        return "SUCCESS", (("交易时间", "候选"),)

    result = sina.collect_sina_candidate(symbol="RB2701", runner=runner)
    assert calls == [("RB2701", 3.0, 3.0, 10.0, 1024 * 1024)]
    assert result.status == "SUCCESS" and result.verification == "UNVERIFIED"
    assert not hasattr(result, "trading_day")
    assert result.source == "https://finance.sina.com.cn/futures/quotes/{symbol}.shtml"


@pytest.mark.parametrize("status", ["SUCCESS", "NETWORK_ERROR", "CHILD_TIMEOUT"])
def test_collection_timestamp_is_utc_and_sampled_after_runner(status):
    runner_finished = []

    def runner(*args):
        runner_finished.append(datetime.now(timezone.utc))
        return status, ()

    result = sina.collect_sina_candidate(symbol="RB2701", runner=runner)
    assert result.collected_at.utcoffset() == timedelta(0)
    assert runner_finished[0] <= result.collected_at <= datetime.now(timezone.utc)
    assert result.verification == "UNVERIFIED"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"symbol": "../RB2701"},
        {"symbol": "RB2701;id"},
        {"symbol": "RB2701-C-1"},
        {"symbol": "RB"},
        {"connect_timeout": 0},
        {"read_timeout": float("inf")},
        {"total_timeout": True},
        {"max_bytes": 0},
        {"max_bytes": 1024 * 1024 + 1},
    ],
)
def test_invalid_input_never_calls_runner(kwargs):
    calls = []
    args = {"symbol": "RB2701", "runner": lambda *a: calls.append(a)}
    args.update(kwargs)
    with pytest.raises(ValueError):
        sina.collect_sina_candidate(**args)
    assert calls == []


def test_timeout_kills_and_reaps_only_newly_created_subprocess(monkeypatch):
    original_popen = subprocess.Popen
    spawned = []

    def sleeper(_command, **kwargs):
        proc = original_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(sina.subprocess, "Popen", sleeper)
    result = sina.collect_sina_candidate(symbol="RB2701", total_timeout=0.2)
    assert result.status == "CHILD_TIMEOUT"
    assert len(spawned) == 1 and spawned[0].poll() is not None
    assert result.verification == "UNVERIFIED"


def test_cleanup_uncertainty_is_explicit(monkeypatch):
    class Stuck:
        def communicate(self, timeout):
            raise subprocess.TimeoutExpired("child", timeout)

        def kill(self):
            pass

    monkeypatch.setattr(sina.subprocess, "Popen", lambda *args, **kwargs: Stuck())
    assert sina.collect_sina_candidate(symbol="RB2701", total_timeout=0.1).status == (
        "CHILD_CLEANUP_UNCERTAIN"
    )


def test_child_result_protocol_is_checked(monkeypatch):
    class FakeChild:
        returncode = 0

        def communicate(self, timeout):
            return json.dumps({"status": "SUCCESS", "fields": [["x", "y"]]}).encode(), b""

    monkeypatch.setattr(sina.subprocess, "Popen", lambda *args, **kwargs: FakeChild())
    result = sina.collect_sina_candidate(symbol="RB2701")
    assert result.status == "SUCCESS" and result.fields == (("x", "y"),)
