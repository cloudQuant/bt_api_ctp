"""Synthetic products and fake transports; no real network or accounts."""

import importlib
import json
import socket
import ssl
import subprocess
import sys

import pytest

from bt_api_ctp import calendar_shfe_product_fetch as fetch


def html(symbol="CU"):
    rows = [
        {
            "ContractSymbol": symbol,
            "ListingExchange": "上海期货交易所",
            "data_standard": "2023-01-01",
        }
    ]
    return (
        "<script>let pageList = " + json.dumps(rows, ensure_ascii=False) + ";</script>"
    ).encode()


class Response:
    def __init__(self, status=200, body=None, headers=None, error=None):
        self.status, self.body, self.headers, self.error = (
            status,
            html() if body is None else body,
            headers or {},
            error,
        )
        self.reads = 0

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, size):
        self.reads += 1
        if self.error:
            raise self.error
        part, self.body = self.body[:size], self.body[size:]
        return part


class Connection:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.sock = self
        self.closed = False
        self.request_args = None
        self.timeout = None

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


def fake(connection, key="CU_FUTURE", limit=1024 * 1024, **kwargs):
    calls = []

    def factory(host, *, timeout, context):
        calls.append((host, timeout, context.verify_mode, context.check_hostname))
        return connection

    result = fetch._fetch_and_parse(key, 2, 3, limit, connection_factory=factory, **kwargs)
    assert calls == [("www.shfe.com.cn", 2, ssl.CERT_REQUIRED, True)]
    assert connection.closed
    return result


def test_success_returns_only_unverified_parsed_observation():
    conn = Connection(Response())
    transport, page = fake(conn)
    assert transport == "HTTP_OK" and page.status == "CANDIDATE"
    assert (
        page.source_authentication == "NOT_PROVEN" and page.records[0].effective_date == "UNKNOWN"
    )
    assert page.records[0].symbol == "CU" and page.records[0].venue == "SHFE"
    assert conn.timeout == 3
    assert conn.request_args == (
        ("GET", "/products/futures/metal/nonferrousmetal/cu_f/"),
        {"headers": {"Accept-Encoding": "identity"}},
    )
    encoded = fetch._encode(transport, page)
    assert b"<script>" not in encoded
    restored = fetch._decode(encoded, "CU_FUTURE")
    assert restored == (transport, page)
    result = fetch.collect_product_page(source_key="CU_FUTURE", runner=lambda *args: restored)
    assert result.status == "CANDIDATE" and result.transport_assurance == "INJECTED_UNVERIFIED"
    assert result.identity_status == "SOURCE_FIELDS_MATCH_UNVERIFIED"
    assert result.coverage == result.current_record == "UNKNOWN"
    assert result.source_authentication == "NOT_PROVEN" and result.publication == "NOT_AUTHORIZED"


def test_matching_content_length_is_accepted():
    body = html()
    transport, page = fake(
        Connection(Response(body=body, headers={"Content-Length": str(len(body))}))
    )
    assert transport == "HTTP_OK" and page.status == "CANDIDATE"


@pytest.mark.parametrize(
    ("row", "identity"),
    [
        (
            {"ContractSymbol": "BC", "ListingExchange": "上海国际能源交易中心"},
            "SOURCE_FIELDS_MISMATCH",
        ),
        ({"ContractSymbol": "CU", "ListingExchange": ""}, "SOURCE_FIELDS_INCOMPLETE"),
        ({"ContractSymbol": "", "ListingExchange": "上海期货交易所"}, "SOURCE_FIELDS_INCOMPLETE"),
    ],
)
def test_source_fields_must_match_without_authenticating_page(row, identity):
    row["data_standard"] = "2023-01-01"
    body = (
        "<script>let pageList = " + json.dumps([row], ensure_ascii=False) + ";</script>"
    ).encode()
    transport, page = fake(Connection(Response(body=body)))
    result = fetch.collect_product_page(
        source_key="CU_FUTURE", runner=lambda *args: (transport, page)
    )
    assert result.status == "UNKNOWN" and result.identity_status == identity
    assert result.page == page and len(result.page.records) == 1
    assert result.source_authentication == "NOT_PROVEN" and result.coverage == "UNKNOWN"


@pytest.mark.parametrize(
    ("key", "symbol", "venue"),
    [
        ("CU_OPTION", "CU", "上海期货交易所"),
        ("BC_OPTION", "BC", "上海国际能源交易中心"),
    ],
)
def test_option_source_fields_match_unverified(key, symbol, venue):
    pattern = f"看涨期权：{symbol}-合约月份-C-行权价格；看跌期权：{symbol}-合约月份-P-行权价格"
    row = {"ContractSymbol": pattern, "ListingExchange": venue, "data_standard": "2023-01-01"}
    body = (
        "<script>let pageList_o = " + json.dumps([row], ensure_ascii=False) + ";</script>"
    ).encode()
    transport, page = fake(Connection(Response(body=body)), key=key)
    result = fetch.collect_product_page(source_key=key, runner=lambda *args: (transport, page))
    assert (
        result.status == "CANDIDATE" and result.identity_status == "SOURCE_FIELDS_MATCH_UNVERIFIED"
    )
    assert result.source_authentication == "NOT_PROVEN"


@pytest.mark.parametrize(
    "key,path",
    [
        ("CU_OPTION", "/products/option/nonferrousmetal/cu_o/"),
        ("BC_OPTION", "/products/option/nonferrousmetal/bc_o/"),
    ],
)
def test_fixed_option_sources_are_only_paths_selected(key, path):
    conn = Connection(Response())
    status, parsed = fake(conn, key=key)
    assert status == "HTTP_OK" and parsed.status == "STRUCTURE_CHANGED"
    assert conn.request_args[0] == ("GET", path)
    assert fetch._decode(fetch._encode(status, parsed), key) == (status, parsed)


@pytest.mark.parametrize(
    "response,status",
    [
        (Response(status=302, headers={"Location": "https://evil.invalid/"}), "REDIRECT"),
        (Response(status=503), "HTTP_ERROR"),
        (Response(headers={"Content-Encoding": "gzip"}), "HTTP_ERROR"),
        (Response(headers={"Content-Length": "invalid"}), "HTTP_ERROR"),
        (Response(headers={"Content-Length": "1000"}), "RESPONSE_TOO_LARGE"),
        (Response(body=b"abc", headers={"Content-Length": "5"}), "HTTP_ERROR"),
        (Response(body=b"abc", headers={"Content-Length": "2"}), "HTTP_ERROR"),
        (Response(body=b"x" * 101), "RESPONSE_TOO_LARGE"),
        (Response(body=b""), "EMPTY_RESPONSE"),
        (Response(error=socket.timeout("secret")), "NETWORK_TIMEOUT"),
    ],
)
def test_response_failures_are_bounded_and_distinct(response, status):
    assert fake(Connection(response), limit=100) == (status, None)
    if status == "REDIRECT":
        assert response.reads == 0


@pytest.mark.parametrize(
    "error,status",
    [
        (ssl.SSLCertVerificationError("secret"), "TLS_ERROR"),
        (socket.timeout("secret"), "NETWORK_TIMEOUT"),
        (OSError("secret"), "NETWORK_ERROR"),
    ],
)
def test_connection_errors_are_finite(error, status):
    assert fake(Connection(error=error)) == (status, None)


def test_parser_failure_is_not_network_success_for_business_status():
    transport, page = fake(Connection(Response(body=b"changed")))
    result = fetch.collect_product_page(
        source_key="CU_FUTURE", runner=lambda *args: (transport, page)
    )
    assert result.status == "UNKNOWN" and result.transport_status == "HTTP_OK"
    assert result.parse_status == "STRUCTURE_CHANGED" and result.page.records == ()

    def broken(**kwargs):
        raise ValueError("secret")

    assert fake(Connection(Response()), parser=broken) == ("PARSER_ERROR", None)


def test_no_arbitrary_url_or_mutable_allowlist_and_no_request_on_import(monkeypatch):
    with pytest.raises(TypeError):
        fetch.SOURCES["evil"] = "https://evil.invalid/"
    for key in (
        "https://www.shfe.com.cn/products/futures/metal/nonferrousmetal/cu_f/",
        "evil",
        None,
    ):
        with pytest.raises(ValueError):
            fetch.collect_product_page(source_key=key)

    def forbidden(*args, **kwargs):
        raise AssertionError("must not request on import")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    importlib.reload(fetch)


def test_tampered_child_fields_wrong_source_duplicate_and_oversize_rejected():
    transport, page = fake(Connection(Response()))
    encoded = fetch._encode(transport, page)
    doc = json.loads(encoded)
    for key, value in (("symbol", "BC"), ("effective_date", "2023-01-01")):
        altered = json.loads(encoded)
        altered["page"]["records"][0][key] = value
        assert fetch._decode(json.dumps(altered).encode(), "CU_FUTURE") == ("CHILD_ERROR", None)
    doc["page"]["source_authentication"] = "PROVEN"
    assert fetch._decode(json.dumps(doc).encode(), "CU_FUTURE") == ("CHILD_ERROR", None)
    assert fetch._decode(encoded, "BC_OPTION") == ("CHILD_ERROR", None)
    assert fetch._decode(
        b'{"transport":"HTTP_ERROR","transport":"TLS_ERROR","page":null}', "CU_FUTURE"
    ) == ("CHILD_ERROR", None)
    assert fetch._decode(b"x" * (fetch._MAX_RESULT + 1), "CU_FUTURE") == ("CHILD_ERROR", None)


@pytest.mark.parametrize("control", ["\x1b", "\x00", "\x7f", "\x85"])
def test_page_control_characters_are_unknown(control):
    rows = [
        {
            "ContractSymbol": "CU",
            "ListingExchange": "上海期货交易所",
            "data_standard": "2023-01-01",
            "note": control,
        }
    ]
    body = (
        "<script>let pageList = " + json.dumps(rows, ensure_ascii=False) + ";</script>"
    ).encode()
    transport, page = fake(Connection(Response(body=body)))
    assert transport == "HTTP_OK" and page.status == "CANDIDATE"
    result = fetch.collect_product_page(
        source_key="CU_FUTURE", runner=lambda *args: (transport, page)
    )
    assert result.status == "UNKNOWN" and result.transport_status == "CHILD_ERROR"
    assert result.page is None and result.source_authentication == "NOT_PROVEN"


def test_timeout_kills_and_reaps_owned_child(monkeypatch, calendar_supported_host):
    original = subprocess.Popen
    spawned = []

    def slow(_cmd, **kwargs):
        child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(fetch.subprocess, "Popen", slow)
    result = fetch.collect_product_page(source_key="CU_FUTURE", total_timeout=0.2)
    assert result.status == "UNKNOWN" and result.transport_status == "CHILD_TIMEOUT"
    assert result.transport_assurance == "SUPERVISED_DEFAULT_TLS_POLICY"
    assert result.source_authentication == "NOT_PROVEN"
    assert len(spawned) == 1 and spawned[0].poll() is not None


def test_child_output_limit_is_enforced_while_reading(monkeypatch, calendar_supported_host):
    original = subprocess.Popen
    spawned = []
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
        spawned.append(child)
        return child

    monkeypatch.setattr(fetch.subprocess, "Popen", noisy)
    result = fetch.collect_product_page(source_key="CU_FUTURE", total_timeout=3)
    assert result.status == "UNKNOWN" and result.transport_status == "RESULT_TOO_LARGE"
    assert len(spawned) == 1 and spawned[0].poll() is not None


def test_invalid_child_arguments_exit_without_network():
    child = subprocess.run(
        [sys.executable, fetch.__file__, "--child", "evil", "1", "1", "100"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 2 and not child.stdout


def test_unreaped_child_is_explicitly_unknown():
    class Stuck:
        def kill(self):
            pass

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("child", timeout)

    assert fetch._stop(Stuck(), "CHILD_TIMEOUT") == ("CHILD_CLEANUP_UNCERTAIN", None)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"connect_timeout": 0},
        {"read_timeout": float("inf")},
        {"total_timeout": True},
        {"max_bytes": 0},
        {"max_bytes": fetch.MAX_BYTES + 1},
    ],
)
def test_invalid_limits_rejected_without_invoking_runner(kwargs):
    def forbidden(*args):
        raise AssertionError("must not call")

    with pytest.raises(ValueError):
        fetch.collect_product_page(source_key="CU_FUTURE", runner=forbidden, **kwargs)
