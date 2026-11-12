"""INE fixed holiday-source fetch; all transports and processes are local."""

import hashlib
import json
import socket
import ssl
import subprocess
import sys

import pytest

from bt_api_ctp import _calendar_child_supervisor as supervisor
from bt_api_ctp import calendar_ine_holiday_fetch as holiday
from bt_api_ctp import calendar_ine_hours_fetch as transport

ROWS = [
    "一、元旦：1月1日（星期四）至1月3日（星期六）休市，1月5日（星期一）起照常开市。1月4日（星期日）为周末休市。2025年12月31日（星期三）晚上不进行夜盘交易。",
    "二、春节：2月15日（星期日）至2月23日（星期一）休市，2月24日（星期二）起照常开市。2月14日（星期六）、2月28日（星期六）为周末休市。2月13日（星期五）晚上不进行夜盘交易。",
    "三、清明节：4月4日（星期六）至4月6日（星期一）休市，4月7日（星期二）起照常开市。4月3日（星期五）晚上不进行夜盘交易。",
    "四、劳动节：5月1日（星期五）至5月5日（星期二）休市，5月6日（星期三）起照常开市。5月9日（星期六）为周末休市。4月30日（星期四）晚上不进行夜盘交易。",
    "五、端午节：6月19日（星期五）至6月21日（星期日）休市，6月22日（星期一）起照常开市。6月18日（星期四）晚上不进行夜盘交易。",
    "六、中秋节：9月25日（星期五）至9月27日（星期日）休市，9月28日（星期一）起照常开市。9月24日（星期四）晚上不进行夜盘交易。",
    "七、国庆节：10月1日（星期四）至10月7日（星期三）休市，10月8日（星期四）起照常开市。9月20日（星期日）、10月10日（星期六）为周末休市。9月30日（星期三）晚上不进行夜盘交易。",
]


def _page(rows=ROWS):
    editor = '<div class="TRS_Editor">' + "".join(f"<div>{row}</div>" for row in rows) + "</div>"
    return (
        '<div class="detail_content"><div id="no_article_empty">'
        '<span class="table_select_title">2026年休市安排</span></div>'
        f'<div class="table_content">{editor}</div></div>'
        f'<div class="m_content_tt">2026年休市安排</div><div class="m_content_test">{editor}</div>'
    ).encode()


class Response:
    def __init__(self, status=200, body=None, headers=None, error=None):
        self.status, self.body = status, _page() if body is None else body
        self.headers, self.error, self.reads = headers or {}, error, 0

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
        self.response, self.error, self.sock = response, error, self
        self.requests, self.read_timeout, self.closed = [], None, False

    def request(self, *args, **kwargs):
        if self.error:
            raise self.error
        self.requests.append((args, kwargs))

    def settimeout(self, value):
        self.read_timeout = value

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def _fetch(connection, limit=1024 * 1024):
    calls = []

    def factory(host, *, timeout, context):
        calls.append((host, timeout, context.verify_mode, context.check_hostname))
        return connection

    result = transport._fetch_bytes(2, 3, limit, connection_factory=factory, _holiday=True)
    assert calls == [("www.ine.cn", 2, ssl.CERT_REQUIRED, True)]
    assert connection.closed
    return result


def test_one_fixed_holiday_request_same_bytes_digest_and_seven_explicit_rows():
    raw = _page()
    conn = Connection(Response(body=raw))
    status, captured = _fetch(conn)
    assert conn.requests == [
        (
            ("GET", "/services/calenderandholidays/holiday/"),
            {"headers": {"Accept-Encoding": "identity"}},
        )
    ]
    assert conn.read_timeout == 3 and (status, captured) == ("HTTP_OK", raw)
    encoded = transport._encode(status, captured, _holiday=True)
    assert transport._decode(encoded, len(raw), _holiday=True) == (status, captured)
    result = holiday.collect_ine_holiday(runner=lambda *args: (status, captured))
    assert (result.status, result.page.year, len(result.page.rows)) == ("CANDIDATE", 2026, 7)
    assert result.source == result.page.source_id == holiday.SOURCE_URL
    assert result.sha256 == result.page.sha256 == hashlib.sha256(raw).hexdigest()
    assert result.raw_bytes == raw and repr(raw) not in repr(result)
    assert (result.verification, result.coverage, result.publication) == (
        "UNVERIFIED",
        "NOT_PROVEN",
        "NOT_AUTHORIZED",
    )
    assert result.transport_assurance == "INJECTED_UNVERIFIED"


@pytest.mark.parametrize(
    "response,expected",
    [
        (Response(status=302, headers={"Location": "https://other.invalid/"}), "REDIRECT"),
        (Response(status=403), "HTTP_ERROR"),
        (Response(headers={"Content-Encoding": "gzip"}), "HTTP_ERROR"),
        (Response(headers={"Content-Length": "invalid"}), "HTTP_ERROR"),
        (Response(headers={"Content-Length": "101"}), "RESPONSE_TOO_LARGE"),
        (Response(body=b"x" * 101), "RESPONSE_TOO_LARGE"),
        (Response(body=b""), "EMPTY_RESPONSE"),
        (Response(body=b"abc", headers={"Content-Length": "4"}), "NETWORK_ERROR"),
        (Response(error=socket.timeout("private")), "NETWORK_TIMEOUT"),
    ],
)
def test_transport_failures_are_never_success(response, expected):
    assert _fetch(Connection(response), limit=100) == (expected, None)
    if expected == "REDIRECT":
        assert response.reads == 0


@pytest.mark.parametrize(
    "error,expected",
    [
        (ssl.SSLCertVerificationError("private"), "TLS_ERROR"),
        (TimeoutError("private"), "NETWORK_TIMEOUT"),
        (OSError("private"), "NETWORK_ERROR"),
    ],
)
def test_tls_and_other_errors_do_not_disclose_exceptions(error, expected):
    assert _fetch(Connection(error=error)) == (expected, None)


def test_waf_bom_broken_and_unknown_data_keep_untrusted_exact_evidence():
    challenge = "<title>WEB 应用防火墙</title>人机识别".encode()
    blocked = holiday.collect_ine_holiday(runner=lambda *args: ("HTTP_OK", challenge))
    assert blocked.status == "ACCESS_CHALLENGE" and blocked.page is None
    assert blocked.raw_bytes == challenge and blocked.transport_status == "HTTP_OK"
    invalid = holiday.collect_ine_holiday(runner=lambda *args: ("HTTP_OK", b"\xff"))
    assert invalid.status == "INVALID_UTF8" and invalid.raw_bytes == b"\xff"
    unknown = holiday.collect_ine_holiday(runner=lambda *args: ("HTTP_OK", b"other page"))
    assert unknown.status == "NEEDS_REVIEW" and unknown.page.rows == ()
    raw = b"\xef\xbb\xbf" + _page()
    bom = holiday.collect_ine_holiday(runner=lambda *args: ("HTTP_OK", raw))
    assert bom.page.status == "CANDIDATE" and bom.raw_bytes == raw
    assert bom.sha256 == hashlib.sha256(raw).hexdigest()


def test_two_fixed_sources_have_distinct_child_protocols_and_reject_forgery():
    raw = _page()
    encoded = transport._encode("HTTP_OK", raw, _holiday=True)
    assert transport._decode(encoded, len(raw), _holiday=True) == ("HTTP_OK", raw)
    assert transport._decode(encoded, len(raw)) == ("CHILD_ERROR", None)
    hours = transport._encode("HTTP_OK", raw)
    assert transport._decode(hours, len(raw), _holiday=True) == ("CHILD_ERROR", None)
    data = json.loads(encoded)
    for status in (
        "CHILD_TIMEOUT",
        "CHILD_ERROR",
        "CHILD_CLEANUP_UNCERTAIN",
        "SUPERVISOR_UNSUPPORTED",
    ):
        assert transport._decode(
            json.dumps({**data, "transport": status}).encode(), len(raw), _holiday=True
        ) == ("CHILD_ERROR", None)
    assert transport._decode(
        json.dumps({**data, "sha256": "0" * 64}).encode(), len(raw), _holiday=True
    ) == ("CHILD_ERROR", None)
    assert transport._decode(encoded, len(raw) - 1, _holiday=True) == ("CHILD_ERROR", None)
    assert transport._decode(b'{"schema":1,"schema":1}', len(raw), _holiday=True) == (
        "CHILD_ERROR",
        None,
    )


@pytest.mark.parametrize(
    "status", ["CHILD_TIMEOUT", "CHILD_CLEANUP_UNCERTAIN", "SUPERVISOR_UNSUPPORTED"]
)
def test_supervision_status_never_becomes_a_page(status):
    result = holiday.collect_ine_holiday(runner=lambda *args: (status, None))
    assert result.status == result.transport_status == status
    assert result.raw_bytes is None and result.page is None


def test_invalid_or_failing_runner_is_not_trusted():
    with pytest.raises(ValueError):
        holiday.collect_ine_holiday(runner=lambda *args: ("HTTP_OK", None))
    with pytest.raises(ValueError):
        holiday.collect_ine_holiday(runner=lambda *args: ("CHILD_TIMEOUT", b"forged"))
    with pytest.raises(ValueError):
        holiday.collect_ine_holiday(max_bytes=0)
    with pytest.raises(ValueError):
        holiday.collect_ine_holiday(total_timeout=float("nan"))
    assert (
        holiday.collect_ine_holiday(runner=lambda *args: 1 / 0).status == "CHILD_CLEANUP_UNCERTAIN"
    )


def test_real_supervisor_private_pipe_uses_holiday_mode_and_never_prints_raw(
    monkeypatch, calendar_supported_host, capsys
):
    original = subprocess.Popen
    raw = _page()
    payload = transport._encode("HTTP_OK", raw, _holiday=True)
    children = []

    def launch(command, **kwargs):
        assert command[2] == "--child-holiday"
        code = "import sys; sys.stdout.buffer.write(bytes.fromhex(sys.argv[1]))"
        child = original([sys.executable, "-c", code, payload.hex()], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    result = holiday.collect_ine_holiday(total_timeout=5)
    assert result.status == "CANDIDATE" and len(result.page.rows) == 7
    assert result.raw_bytes == raw and children[0].poll() == 0
    assert result.transport_assurance == "SUPERVISED_DEFAULT_TLS_POLICY"
    assert capsys.readouterr().out == ""


def test_real_supervisor_kills_and_reaps_only_owned_child_on_timeout(
    monkeypatch, calendar_supported_host
):
    original = subprocess.Popen
    children = []

    def launch(command, **kwargs):
        assert command[2] == "--child-holiday"
        child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    result = holiday.collect_ine_holiday(total_timeout=0.1)
    assert result.transport_status == "CHILD_TIMEOUT"
    assert len(children) == 1 and children[0].poll() is not None


def test_real_child_script_dispatches_fixed_holiday_request(
    monkeypatch, calendar_supported_host, capsys
):
    original = subprocess.Popen
    raw = _page()
    children = []
    code = """
import http.client, runpy, sys
raw = bytes.fromhex(sys.argv[2])
class Response:
    status = 200
    def getheader(self, key, default=None):
        return str(len(raw)) if key == 'Content-Length' else default
    def read(self, size):
        if not hasattr(self, 'pending'):
            self.pending = raw
        result, self.pending = self.pending[:size], self.pending[size:]
        return result
class Connection:
    def __init__(self, host, *, timeout, context):
        assert host == 'www.ine.cn' and context.check_hostname
        self.sock = self
    def request(self, method, path, *, headers):
        assert method == 'GET' and path == '/services/calenderandholidays/holiday/'
        assert headers == {'Accept-Encoding': 'identity'}
    def settimeout(self, value):
        pass
    def getresponse(self):
        return Response()
    def close(self):
        pass
http.client.HTTPSConnection = Connection
path = sys.argv[1]
sys.argv = [path, '--child-holiday', '2', '3', '1048576']
runpy.run_path(path, run_name='__main__')
"""

    def launch(command, **kwargs):
        child = original([sys.executable, "-c", code, command[1], raw.hex()], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    result = holiday.collect_ine_holiday(total_timeout=5)
    assert result.status == "CANDIDATE" and len(result.page.rows) == 7
    assert result.raw_bytes == raw and result.sha256 == hashlib.sha256(raw).hexdigest()
    assert result.transport_assurance == "SUPERVISED_DEFAULT_TLS_POLICY"
    assert len(children) == 1 and children[0].poll() == 0
    assert capsys.readouterr().out == ""
