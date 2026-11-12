"""Synthetic pages and transports only; no external network."""

import base64
import hashlib
import json
import ssl
import subprocess
import sys
from dataclasses import replace
from datetime import date

import pytest

from bt_api_ctp import calendar_shfe_evidence_fetch as evidence
from bt_api_ctp import calendar_shfe_fetch as shfe

HTML = (
    "<table><tr><td>期货：合成甲<br>期权：合成甲期权</td><td>"
    "日盘<br>集合竞价申报 08:55-08:59<br>集合竞价撮合 08:59-09:00<br>"
    "连续竞价交易 09:00-10:15<br>连续竞价交易 10:30-11:30<br>"
    "连续竞价交易 13:30-15:00</td></tr></table>"
).encode()


def holiday_html():
    weekdays = "一二三四五六日"
    names = ("元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节")
    months = (1, 2, 4, 5, 6, 9, 10)

    def display(day, year=False):
        return f"{str(day.year) + '年' if year else ''}{day.month}月{day.day}日（星期{weekdays[day.weekday()]}）"

    rows = []
    for index, (name, month) in enumerate(zip(names, months)):  # noqa: B905 - Python 3.9
        start = date(2026, month, 1 if index == 0 else 15)
        end = date(2026, month, 3 if index == 0 else 16)
        opened = date(2026, month, 5 if index == 0 else 17)
        night = date(2025, 12, 31) if index == 0 else date(2026, month, 14)
        rows.append(
            f"<div>{'一二三四五六七'[index]}、{name}：{display(start)}至{display(end)}休市，"
            f"{display(opened)}起照常开市。{display(night, index == 0)}晚上不进行夜盘交易。</div>"
        )
    return (
        '<div class="article_title" id="no_article_empty">2026年休市安排</div>'
        '<div class="article_content_item"><div class="TRS_Editor">'
        + "".join(rows)
        + "</div></div>"
    ).encode()


class Response:
    def __init__(self, *, body):
        self.status = 200
        self.body = body

    def getheader(self, name, default=None):
        return default

    def read(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk


class Connection:
    def __init__(self, response):
        self.response = response
        self.sock = self
        self.request_args = None

    def request(self, *args, **kwargs):
        self.request_args = (args, kwargs)

    def settimeout(self, value):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        pass


@pytest.mark.parametrize("kind,raw", [("hours", HTML), ("holiday", holiday_html())])
def test_single_request_capture_and_parent_reparse(kind, raw):
    conn = Connection(Response(body=raw))
    made = []

    def factory(host, *, timeout, context):
        made.append((host, context.verify_mode, context.check_hostname))
        return conn

    captured = []
    is_holiday = kind == "holiday"
    status, page = shfe._fetch_and_parse(
        2,
        3,
        shfe.MAX_BYTES,
        connection_factory=factory,
        parser=shfe.parse_shfe_holiday if is_holiday else shfe.parse_shfe_page,
        _holiday=is_holiday,
        _capture=captured.append,
    )
    assert made == [("www.shfe.com.cn", ssl.CERT_REQUIRED, True)]
    assert conn.request_args[0][0] == "GET" and len(captured) == 1 and captured[0] == raw
    assert status == "HTTP_OK" and page.status in {"CANDIDATE", "NEEDS_REVIEW"}
    encoder = shfe._encode_holiday_result if is_holiday else shfe._encode_result
    data = {
        "schema": "shfe-evidence-ipc/1",
        "result": json.loads(encoder(status, page)),
        "raw_base64": base64.b64encode(raw).decode(),
    }
    encoded = json.dumps(data).encode()
    status, decoded = evidence.decode_evidence(encoded, kind, shfe.MAX_BYTES)
    assert status == "HTTP_OK" and decoded.raw == raw
    assert decoded.sha256 == hashlib.sha256(raw).hexdigest() == decoded.page.sha256
    assert "<table>" not in repr(decoded)
    data["result"]["page"]["sha256"] = "0" * 64
    assert evidence.decode_evidence(json.dumps(data).encode(), kind, shfe.MAX_BYTES) == (
        "CHILD_CLEANUP_UNCERTAIN",
        None,
    )


@pytest.mark.parametrize("raw", [b"\xef\xbb\xbf" + HTML, b"\xff\xfe"])
def test_exact_bytes_and_lossless_conversion(raw):
    page = shfe.parse_shfe_page(source_id=shfe._SOURCE, html=raw)
    item = evidence.PublicPageEvidence(
        "hours", shfe._SOURCE, hashlib.sha256(raw).hexdigest(), raw, page
    )
    result = evidence.collect_shfe_evidence(kind="hours", runner=lambda *args: ("HTTP_OK", item))
    assert result.evidence.raw == raw and result.transport_assurance == "INJECTED_UNVERIFIED"
    assert evidence.evidence_utf8(item) == (
        raw.decode("utf-8") if raw.startswith(b"\xef\xbb\xbf") else None
    )
    with pytest.raises(ValueError):
        evidence.collect_shfe_evidence(
            kind="hours", runner=lambda *args: ("HTTP_OK", replace(item, sha256="0" * 64))
        )


@pytest.mark.parametrize(
    "status", ["CHILD_TIMEOUT", "CHILD_ERROR", "CHILD_CLEANUP_UNCERTAIN", "SUPERVISOR_UNSUPPORTED"]
)
def test_child_cannot_claim_supervisor_status(status):
    encoded = json.dumps(
        {
            "schema": "shfe-evidence-ipc/1",
            "result": {"transport": status, "page": None},
            "raw_base64": None,
        }
    ).encode()
    assert evidence.decode_evidence(encoded, "hours", shfe.MAX_BYTES) == (
        "CHILD_CLEANUP_UNCERTAIN",
        None,
    )


def test_missing_or_oversized_raw_is_rejected():
    page = shfe.parse_shfe_page(source_id=shfe._SOURCE, html=HTML)
    doc = {
        "schema": "shfe-evidence-ipc/1",
        "result": json.loads(shfe._encode_result("HTTP_OK", page)),
        "raw_base64": base64.b64encode(HTML).decode(),
    }
    assert evidence.decode_evidence(json.dumps(doc).encode(), "hours", len(HTML) - 1) == (
        "CHILD_CLEANUP_UNCERTAIN",
        None,
    )
    doc["raw_base64"] = "@"
    assert evidence.decode_evidence(json.dumps(doc).encode(), "hours", shfe.MAX_BYTES) == (
        "CHILD_CLEANUP_UNCERTAIN",
        None,
    )


def test_supervisor_cleanup_status_retained():
    result = evidence.collect_shfe_evidence(
        kind="hours", runner=lambda *args: ("CHILD_CLEANUP_UNCERTAIN", None)
    )
    assert result.transport_status == "CHILD_CLEANUP_UNCERTAIN" and result.evidence is None


@pytest.mark.parametrize("kind,raw", [("hours", HTML), ("holiday", holiday_html())])
def test_actual_child_mode_with_injected_no_network_transport(kind, raw):
    # Isolated real child executes the exact script entry point with synthetic
    # transport. Both the result parser and raw bytes travel over the IPC pipe.
    program = """
import base64, http.client, runpy, sys
raw = base64.b64decode(sys.argv[3])
class Response:
    status = 200
    def __init__(self): self.body = raw
    def getheader(self, name, default=None): return default
    def read(self, size):
        chunk, self.body = self.body[:size], self.body[size:]
        return chunk
class Connection:
    def __init__(self, *args, **kwargs): self.sock = self
    def request(self, *args, **kwargs): pass
    def settimeout(self, value): pass
    def getresponse(self): return Response()
    def close(self): pass
http.client.HTTPSConnection = Connection
script, kind = sys.argv[1:3]
sys.argv = [script, '--child-evidence-' + kind, '2', '3', '1048576']
runpy.run_path(script, run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", program, shfe.__file__, kind, base64.b64encode(raw).decode()],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0 and not result.stderr
    status, item = evidence.decode_evidence(result.stdout, kind, shfe.MAX_BYTES)
    assert status == "HTTP_OK" and item.raw == raw


def test_declared_content_length_must_match_actual_bytes():
    class Truncated(Response):
        def getheader(self, name, default=None):
            return str(len(HTML) + 1) if name == "Content-Length" else default

    captured = []
    result = shfe._fetch_and_parse(
        2,
        3,
        shfe.MAX_BYTES,
        connection_factory=lambda *_args, **_kwargs: Connection(Truncated(body=HTML)),
        _capture=captured.append,
    )
    assert result == ("HTTP_ERROR", None) and not captured
