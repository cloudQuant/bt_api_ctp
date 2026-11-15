# ruff: noqa: UP045
"""Extract explicit holiday statements from one INE public HTML page.

Only the single annual article actually present in both desktop and mobile
sections is interpreted. Year links or scripts are not annual evidence. This
is an offline parser: caller-supplied bytes, URL and SHA do not establish source
origin, current applicability, full exchange coverage or authority to publish.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Optional

from bt_api_ctp.calendar_shfe_holiday import _EXPECTED, _Invalid, _row

SOURCE_URL = "https://www.ine.cn/services/calenderandholidays/holiday/"
MAX_BYTES = 1024 * 1024
MAX_EVENTS = 40000
MAX_ROW = 4096
MAX_ROWS = 32
_TITLE = re.compile(r"([0-9]{4})年休市安排\Z")


@dataclass(frozen=True)
class IneHolidayRow:
    name: str
    closure_start: date
    closure_end: date
    resume_date: date
    no_night: date
    weekend_closures: tuple[date, ...]
    raw_text: str


@dataclass(frozen=True)
class IneHolidayCandidate:
    status: str
    source_id: Optional[str] = None
    sha256: Optional[str] = None
    year: Optional[int] = None
    rows: tuple[IneHolidayRow, ...] = ()
    reason: str = ""
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    publication: str = "NOT_AUTHORIZED"


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = 0
        self.divs = []
        self.articles = {"desktop": [], "mobile": []}
        self.titles = {"desktop": [], "mobile": []}
        self.editor = None
        self.editor_level = 0
        self.article = None
        self.row = None
        self.title = None
        self.title_parts = None
        self.editor_count = 0

    def _tick(self):
        self.events += 1
        if self.events > MAX_EVENTS:
            raise _Invalid

    def _inside(self, cls):
        return any(cls in classes for classes, _ in self.divs)

    def handle_starttag(self, tag, attrs):
        self._tick()
        if len(dict(attrs)) != len(attrs):
            raise _Invalid
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if self.editor is not None:
            if tag != "div" or self.editor_level != 1 or self.row is not None:
                raise _Invalid
            self.editor_level = 2
            self.row = []
        elif tag == "div" and "TRS_Editor" in classes:
            self.editor_count += 1
            if self._inside("detail_content") and self._inside("table_content"):
                self.editor = "desktop"
            elif self._inside("m_content_test"):
                self.editor = "mobile"
            else:
                raise _Invalid
            self.editor_level = 1
            self.article = []
        elif self.title is not None:
            raise _Invalid
        elif (
            tag == "span"
            and "table_select_title" in classes
            and self._inside("detail_content")
            and any(ident == "no_article_empty" for _, ident in self.divs)
        ):
            self.title = ("desktop", tag)
            self.title_parts = []
        elif tag == "div" and "m_content_tt" in classes:
            self.title = ("mobile", tag)
            self.title_parts = []
        if tag == "div":
            if len(self.divs) >= 128:
                raise _Invalid
            self.divs.append((classes, attrs.get("id")))

    def handle_endtag(self, tag):
        self._tick()
        if self.editor is not None:
            if tag != "div":
                raise _Invalid
            if self.editor_level == 2:
                text = "".join(self.row).strip()
                if not text or len(text) > MAX_ROW or len(self.article) >= MAX_ROWS:
                    raise _Invalid
                self.article.append(text)
                self.row = None
                self.editor_level = 1
            else:
                self.articles[self.editor].append(tuple(self.article))
                self.article = None
                self.editor = None
                self.editor_level = 0
        elif self.title is not None:
            if tag != self.title[1]:
                raise _Invalid
            self.titles[self.title[0]].append("".join(self.title_parts).strip())
            self.title = None
            self.title_parts = None
        if tag == "div":
            if not self.divs:
                raise _Invalid
            self.divs.pop()

    def handle_data(self, data):
        self._tick()
        if self.title is not None:
            self.title_parts.append(data)
            if sum(map(len, self.title_parts)) > MAX_ROW:
                raise _Invalid
        elif self.editor_level == 2:
            self.row.append(data)
            if sum(map(len, self.row)) > MAX_ROW:
                raise _Invalid
        elif self.editor_level == 1 and data.strip():
            raise _Invalid
        elif (
            self.editor is None
            and data.strip()
            and (
                (self._inside("detail_content") and self._inside("table_content"))
                or self._inside("m_content_test")
            )
        ):
            # Unparsed adjacent content may contain an additional condition.
            raise _Invalid

    def handle_comment(self, data):
        self._tick()
        if self.editor is not None or self.title is not None:
            raise _Invalid


def parse_ine_holiday(*, source_id: str, html: bytes) -> IneHolidayCandidate:
    """Extract the displayed annual article, retaining only explicit statements."""
    if source_id != SOURCE_URL or type(source_id) is not str:
        return IneHolidayCandidate("INVALID_SOURCE")
    if type(html) is not bytes:
        return IneHolidayCandidate("INVALID_INPUT", source_id)
    if len(html) > MAX_BYTES:
        return IneHolidayCandidate("INPUT_TOO_LARGE", source_id)
    digest = hashlib.sha256(html).hexdigest()
    try:
        text = html.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return IneHolidayCandidate("INVALID_UTF8", source_id, digest)
    try:
        parser = _Parser()
        parser.feed(text)
        parser.close()
        if (
            parser.editor is not None
            or parser.title is not None
            or parser.divs
            or parser.editor_count != 2
            or len(parser.titles["desktop"]) != 1
            or len(parser.titles["mobile"]) != 1
            or len(parser.articles["desktop"]) != 1
            or len(parser.articles["mobile"]) != 1
        ):
            raise _Invalid
        desktop_title, mobile_title = parser.titles["desktop"][0], parser.titles["mobile"][0]
        match = _TITLE.fullmatch(desktop_title)
        if not match or desktop_title != mobile_title:
            return IneHolidayCandidate("NEEDS_REVIEW", source_id, digest, reason="TITLE_CONFLICT")
        year = int(match.group(1))
        if year < 1 or year > 9999:
            raise _Invalid
        desktop, mobile = parser.articles["desktop"][0], parser.articles["mobile"][0]
        if desktop != mobile:
            return IneHolidayCandidate(
                "NEEDS_REVIEW", source_id, digest, year, reason="ARTICLE_CONFLICT"
            )
        parsed = [_row(text, year) for text in desktop]
        expected = tuple(zip("一二三四五六七", _EXPECTED))
        if len(parsed) != len(expected) or tuple((o, row.name) for o, row, _ in parsed) != expected:
            return IneHolidayCandidate(
                "NEEDS_REVIEW", source_id, digest, year, reason="MISSING_OR_UNEXPECTED_SECTION"
            )
        rows = tuple(
            IneHolidayRow(
                row.name,
                row.closure_start,
                row.closure_end,
                opened,
                row.no_night,
                row.weekend_closures,
                row.raw_text,
            )
            for _, row, opened in parsed
        )
        if any(a.closure_end >= b.closure_start for a, b in zip(rows, rows[1:])):
            raise _Invalid
        if any(
            row.closure_start <= other.resume_date <= row.closure_end
            or other.resume_date in row.weekend_closures
            for row in rows
            for other in rows
        ):
            raise _Invalid
        return IneHolidayCandidate("CANDIDATE", source_id, digest, year, rows)
    except (ValueError, AssertionError, _Invalid, RecursionError):
        return IneHolidayCandidate(
            "NEEDS_REVIEW", source_id, digest, reason="UNRECOGNIZED_OR_AMBIGUOUS"
        )
