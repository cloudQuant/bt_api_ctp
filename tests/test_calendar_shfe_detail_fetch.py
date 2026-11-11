"""Bounded detail-page tests use synthetic transport and no real HTTP."""

import importlib
import json
import socket
import ssl
import subprocess
import sys

import pytest

from bt_api_ctp import calendar_shfe_detail_fetch as fetch

FUTURE = "/products/futures/metal/nonferrousmetal/cu_f/"
OPTION = "/products/option/nonferrousmetal/cu_o/"
INDEX = "/products/futures/index_f/ec_f/"


def future(*rows):
    return (
        "<script>let pageList = " + json.dumps(list(rows), ensure_ascii=False) + ";</script>"
    ).encode()


def option(*rows):
    return (
        "<script>let pageList_o = " + json.dumps(list(rows), ensure_ascii=False) + ";</script>"
    ).encode()


def row(symbol="CU", venue="上海期货交易所", **extra):
    return {
        "ContractSymbol": symbol,
        "ListingExchange": venue,
        "data_standard": "2023-03-29",
        **extra,
    }


class Response:
    def __init__(self, status=200, body=None, headers=None, error=None):
        self.status, self.body, self.headers, self.error = (
            status,
            future(row()) if body is None else body,
            headers or {},
            error,
        )
        self.reads = 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, count):
        self.reads += 1
        if self.error:
            raise self.error
        part, self.body = self.body[:count], self.body[count:]
        return part


class Connection:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.sock = response, error, self
        self.closed = False
        self.request_args = None
        self.read_timeout = None

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


def fake(connection, path=FUTURE, limit=1024 * 1024, **kwargs):
    calls = []

    def factory(host, *, timeout, context):
        calls.append((host, timeout, context.verify_mode, context.check_hostname))
        return connection

    result = fetch._fetch_and_parse(path, 2, 3, limit, connection_factory=factory, **kwargs)
    assert calls == [("www.shfe.com.cn", 2, ssl.CERT_REQUIRED, True)]
    assert connection.closed
    return result


def collect_from_fake(path, connection):
    parsed = fake(connection, path=path)
    return fetch.collect_detail_page(path=path, runner=lambda *args: parsed)


def test_fixed_host_tls_and_unverified_response_no_identity_from_basename():
    connection = Connection(Response(body=future(row("SC"))))
    result = collect_from_fake(FUTURE, connection)
    assert connection.request_args == (
        ("GET", FUTURE),
        {"headers": {"Accept-Encoding": "identity"}},
    )
    assert connection.read_timeout == 3
    assert result.status == "CANDIDATE" and result.kind == "future"
    assert result.page.records[0].symbol == "SC"  # path ends cu_f; no inferred identity
    assert result.identity_status == "CONSISTENT_FIELDS_UNVERIFIED"
    assert result.transport_assurance == "INJECTED_UNVERIFIED"
    assert result.source_authentication == "NOT_PROVEN" and result.coverage == "UNKNOWN"
    assert result.current_record == "UNKNOWN" and result.publication == "NOT_AUTHORIZED"
    assert result.page.records[0].effective_date == "UNKNOWN"
    assert b"<script>" not in fetch._encode("HTTP_OK", result.page)


@pytest.mark.parametrize(
    "path,body,expected_kind",
    [
        (INDEX, future(row("EC", venue="上海国际能源交易中心")), "future"),
        (
            OPTION,
            option(row("看涨期权：CU-合约月份-C-行权价格；看跌期权：CU-合约月份-P-行权价格")),
            "option",
        ),
    ],
)
def test_known_path_categories_are_preserved_without_venue_inference(path, body, expected_kind):
    result = collect_from_fake(path, Connection(Response(body=body)))
    assert result.status == "CANDIDATE" and result.kind == expected_kind
    assert (
        result.page.records[0].kind == expected_kind
        and result.source_authentication == "NOT_PROVEN"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "https://www.shfe.com.cn" + FUTURE,
        "//www.shfe.com.cn" + FUTURE,
        "http://127.0.0.1/",
        "/products//futures/metal/cu_f/",
        "/products/futures/metal/../cu_f/",
        "/products/futures/metal/%2e%2e/cu_f/",
        "/products/futures/metal/cu_f/?x=1",
        "/products/futures/metal/cu_f/#fragment",
        "/products/futures/metal\\cu_f/",
        "/products/futures/metal/cu_f/\x00",
        "/products/futures/metal/cu_o/",
        "/products/option/index_f/cu_o/",
        "/products/futures/index_f/a/b_f/",
        "/products/futures/metal/CU_f/",
        None,
    ],
)
def test_malicious_paths_never_start_runner_or_connection(bad):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not start")

    with pytest.raises(ValueError):
        fetch.collect_detail_page(path=bad, runner=forbidden)
    assert fetch._fetch_and_parse(bad, 1, 1, 100, connection_factory=forbidden) == (
        "CHILD_ERROR",
        None,
    )


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
def test_response_failures_are_distinct_and_bounded(response, status):
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
def test_connection_errors_do_not_expose_exception(error, status):
    assert fake(Connection(error=error)) == (status, None)


def test_identity_missing_and_historical_conflicts_are_not_candidate_success():
    incomplete = collect_from_fake(FUTURE, Connection(Response(body=future(row("", venue="")))))
    assert incomplete.status == "NEEDS_REVIEW" and incomplete.identity_status == "INCOMPLETE_FIELDS"
    assert incomplete.page.records and incomplete.source_authentication == "NOT_PROVEN"
    conflict = collect_from_fake(FUTURE, Connection(Response(body=future(row("CU"), row("AL")))))
    assert conflict.status == "NEEDS_REVIEW" and conflict.identity_status == "CONFLICTING_FIELDS"
    assert len(conflict.page.records) == 2 and "SYMBOL_CONFLICT" in conflict.page.review_reasons
    venue = collect_from_fake(
        FUTURE,
        Connection(Response(body=future(row("CU"), row("CU", venue="上海国际能源交易中心")))),
    )
    assert venue.status == "NEEDS_REVIEW" and venue.identity_status == "CONFLICTING_FIELDS"


def test_wrong_script_type_and_invalid_encoding_are_unknown():
    wrong = collect_from_fake(OPTION, Connection(Response(body=future(row()))))
    assert (
        wrong.status == "UNKNOWN"
        and wrong.transport_status == "HTTP_OK"
        and wrong.parse_status == "STRUCTURE_CHANGED"
    )
    invalid = collect_from_fake(FUTURE, Connection(Response(body=b"\xff")))
    assert invalid.status == "UNKNOWN" and invalid.parse_status == "INVALID_UTF8"


def test_child_protocol_rejects_tampering_wrong_path_and_control_chars():
    status, page = fake(Connection(Response()))
    encoded = fetch._encode(status, page)
    assert fetch._decode(encoded, FUTURE) == (status, page)
    assert fetch._decode(encoded, INDEX) == ("CHILD_ERROR", None)
    for key, value in (("kind", "option"), ("symbol", "AL"), ("effective_date", "2023-01-01")):
        doc = json.loads(encoded)
        doc["page"]["records"][0][key] = value
        assert fetch._decode(json.dumps(doc).encode(), FUTURE) == ("CHILD_ERROR", None)
    doc = json.loads(encoded)
    doc["page"]["source_authentication"] = "PROVEN"
    assert fetch._decode(json.dumps(doc).encode(), FUTURE) == ("CHILD_ERROR", None)
    assert fetch._decode(
        b'{"transport":"TLS_ERROR","transport":"HTTP_ERROR","page":null}', FUTURE
    ) == ("CHILD_ERROR", None)
    control = fake(Connection(Response(body=future(row(note="\x1b[31m")))))
    assert fetch._decode(fetch._encode(*control), FUTURE) == ("CHILD_ERROR", None)


def test_default_import_and_invalid_settings_do_not_start_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not start")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    importlib.reload(fetch)
    for settings in (
        {"connect_timeout": 0},
        {"read_timeout": float("inf")},
        {"total_timeout": True},
        {"max_bytes": 0},
    ):
        with pytest.raises(ValueError):
            fetch.collect_detail_page(path=FUTURE, runner=forbidden, **settings)


def test_total_timeout_kills_and_reaps_owned_child(monkeypatch):
    original = subprocess.Popen
    children = []

    def slow(_cmd, **kwargs):
        child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(fetch.subprocess, "Popen", slow)
    result = fetch.collect_detail_page(path=FUTURE, total_timeout=0.2)
    assert result.status == "UNKNOWN" and result.transport_status == "CHILD_TIMEOUT"
    assert (
        result.transport_assurance == "SUPERVISED_DEFAULT_TLS_POLICY"
        and result.source_authentication == "NOT_PROVEN"
    )
    assert len(children) == 1 and children[0].poll() is not None


def test_owned_child_protocol_roundtrip_uses_only_validated_path(monkeypatch):
    parsed = fake(Connection(Response(body=future(row("CU")))))
    payload = fetch._encode(*parsed)
    original = subprocess.Popen
    commands = []

    def synthetic_child(cmd, **kwargs):
        commands.append((cmd, kwargs))
        return original(
            [sys.executable, "-c", f"import sys; sys.stdout.buffer.write({payload!r})"], **kwargs
        )

    monkeypatch.setattr(fetch.subprocess, "Popen", synthetic_child)
    result = fetch.collect_detail_page(path=FUTURE, total_timeout=3)
    assert result.status == "CANDIDATE" and result.page.records[0].symbol == "CU"
    assert result.source_authentication == "NOT_PROVEN"  # synthetic process, no TLS claim
    assert commands[0][0][2:4] == ["--child", FUTURE]
    assert commands[0][1]["env"] == {} and commands[0][1]["shell"] is False


def test_child_output_limit_is_enforced_before_buffering_unbounded_output(monkeypatch):
    original = subprocess.Popen
    children = []
    monkeypatch.setattr(fetch, "_MAX_RESULT", 128)

    def noisy(_cmd, **kwargs):
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
    result = fetch.collect_detail_page(path=FUTURE, total_timeout=3)
    assert result.status == "UNKNOWN" and result.transport_status == "RESULT_TOO_LARGE"
    assert children[0].poll() is not None


def test_child_revalidates_path_before_any_network():
    child = subprocess.run(
        [sys.executable, fetch.__file__, "--child", "//evil.invalid/path", "1", "1", "100"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 2 and not child.stdout


def test_cleanup_failure_remains_explicitly_unknown():
    class Stuck:
        def kill(self):
            pass

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("child", timeout)

    assert fetch._stop(Stuck(), "CHILD_TIMEOUT") == ("CHILD_CLEANUP_UNCERTAIN", None)
