# ruff: noqa: UP045
"""Offline experimental extractor for the SHFE annual holiday page.

Only the recognised annual article is examined. A candidate records explicit
closures, not all closed or open days, trading days, venue authority or coverage.
The source ID and digest identify caller-supplied bytes, not their provenance.
No network, filesystem, account, review or publication operation is performed.
Unsupported page changes fail closed; all results remain UNVERIFIED.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Optional

_MAX_BYTES = 1024 * 1024
_MAX_EVENTS = 40000
_MAX_ARTICLES = 4
_MAX_ROWS = 32
_MAX_ROW = 4096
_TITLE = re.compile(r"([0-9]{4})年休市安排\Z")
_DATE = r"(?:(?P<{p}year>[0-9]{{4}})年)?(?P<{p}month>[0-9]{{1,2}})月(?P<{p}day>[0-9]{{1,2}})日（星期(?P<{p}weekday>[一二三四五六日])）"
_DATE_ITEM = _DATE.format(p="x")
_FIRST = re.compile(
    r"(?P<ordinal>[一二三四五六七])、(?P<name>元旦|春节|清明节|劳动节|端午节|中秋节|国庆节)："
    + _DATE.format(p="a")
    + "至"
    + _DATE.format(p="b")
    + "休市，"
    + _DATE.format(p="c")
    + r"起照常开市\Z"
)
_DATE_TOKEN = re.compile(_DATE_ITEM)
_NIGHT = re.compile(_DATE.format(p="n") + r"晚上不进行夜盘交易\Z")
_WEEKDAYS = "一二三四五六日"
_EXPECTED = ("元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节")


@dataclass(frozen=True)
class HolidayRow:
    name: str
    closure_start: date
    closure_end: date
    no_night: date
    weekend_closures: tuple[date, ...]
    raw_text: str


@dataclass(frozen=True)
class HolidayCandidate:
    status: str
    source_id: Optional[str] = None
    sha256: Optional[str] = None
    year: Optional[int] = None
    rows: tuple[HolidayRow, ...] = ()
    reasons: tuple[str, ...] = ()
    verification: str = "UNVERIFIED"
    coverage: str = "NOT_PROVEN"
    raw_title: Optional[str] = None


class _Invalid(Exception):
    pass


class _ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events = 0
        self.depth = 0
        self.article = None
        self.row = None
        self.articles = []
        self.titles = []
        self.title = None
        self.container = None
        self.wrapper = False

    def tick(self):
        self.events += 1
        if self.events > _MAX_EVENTS:
            raise _Invalid

    def handle_starttag(self, tag, attrs):
        self.tick()
        if len(dict(attrs)) != len(attrs):
            raise _Invalid
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if self.depth:
            if tag != "div" or self.depth != 1 or self.row is not None:
                raise _Invalid
            self.depth = 2
            self.row = []
            return
        if self.title is not None:
            raise _Invalid
        if self.container is not None:
            if tag == "p" and self.container == "m_content_test" and not self.wrapper:
                self.wrapper = True
                return
            if tag != "div" or "TRS_Editor" not in classes:
                raise _Invalid
            if len(self.articles) >= _MAX_ARTICLES:
                raise _Invalid
            self.article = []
            self.depth = 1
        elif tag == "div" and ("article_content_item" in classes or "m_content_test" in classes):
            self.container = (
                "article_content_item" if "article_content_item" in classes else "m_content_test"
            )
        elif tag == "div" and "TRS_Editor" in classes:
            raise _Invalid
        elif tag == "div" and (
            ("article_title" in classes and attrs.get("id") == "no_article_empty")
            or "m_content_tt" in classes
        ):
            self.title = []

    def handle_endtag(self, tag):
        self.tick()
        if self.depth:
            if tag != "div":
                raise _Invalid
            if self.depth == 2:
                text = "".join(self.row).strip()
                if not text or len(text) > _MAX_ROW or len(self.article) >= _MAX_ROWS:
                    raise _Invalid
                self.article.append(text)
                self.row = None
                self.depth = 1
            else:
                if not self.article:
                    raise _Invalid
                self.articles.append(tuple(self.article))
                self.article = None
                self.depth = 0
        elif self.title is not None:
            if tag != "div":
                raise _Invalid
            self.titles.append("".join(self.title).strip())
            self.title = None
        elif self.container is not None:
            if tag == "p" and self.wrapper:
                self.wrapper = False
            elif tag == "div" and not self.wrapper:
                self.container = None
            else:
                raise _Invalid

    def handle_data(self, data):
        self.tick()
        if self.title is not None:
            self.title.append(data)
        elif self.depth == 2:
            self.row.append(data)
            if sum(map(len, self.row)) > _MAX_ROW:
                raise _Invalid
        elif self.depth == 1 and data.strip():
            raise _Invalid
        elif self.container is not None and data.strip():
            raise _Invalid

    def handle_comment(self, data):
        self.tick()
        if self.depth or self.title is not None or self.container is not None:
            raise _Invalid


def _day(match, prefix, year, *, allow_previous=False):
    explicit = match.group(prefix + "year")
    resolved_year = int(explicit) if explicit else year
    if resolved_year != year and not (allow_previous and explicit and resolved_year == year - 1):
        raise _Invalid
    try:
        value = date(
            resolved_year, int(match.group(prefix + "month")), int(match.group(prefix + "day"))
        )
    except ValueError as exc:
        raise _Invalid from exc
    if _WEEKDAYS[value.weekday()] != match.group(prefix + "weekday"):
        raise _Invalid
    return value


def _row(text, year):
    # Whitespace is not used as syntax in this narrow page format.
    clean = "".join(text.split())
    sentences = clean.split("。")
    if not sentences or sentences[-1] != "":
        raise _Invalid
    sentences.pop()
    if len(sentences) not in (2, 3):
        raise _Invalid
    first = _FIRST.fullmatch(sentences[0])
    night = _NIGHT.fullmatch(sentences[-1])
    if not first or not night:
        raise _Invalid
    start, end, opened = (_day(first, prefix, year) for prefix in ("a", "b", "c"))
    no_night = _day(night, "n", year, allow_previous=True)
    if start > end or opened <= end or no_night >= start:
        raise _Invalid
    if no_night.year != year and not (start.month == 1 and no_night.month == 12):
        raise _Invalid
    weekends = []
    if len(sentences) == 3:
        weekend = sentences[1]
        if not weekend.endswith("为周末休市"):
            raise _Invalid
        for item in weekend[: -len("为周末休市")].split("、"):
            parsed = _DATE_TOKEN.fullmatch(item)
            if parsed is None:
                raise _Invalid
            day = _day(parsed, "x", year)
            if day.weekday() not in (5, 6) or day in weekends:
                raise _Invalid
            weekends.append(day)
    return (
        first.group("ordinal"),
        HolidayRow(first.group("name"), start, end, no_night, tuple(weekends), text),
        opened,
    )


def parse_shfe_holiday(*, source_id: str, html: bytes) -> HolidayCandidate:
    """Extract explicit annual page statements; never infer a complete calendar."""
    if type(source_id) is not str or not source_id.strip() or len(source_id) > 512:
        return HolidayCandidate("INVALID_SOURCE")
    if type(html) is not bytes:
        return HolidayCandidate("INVALID_INPUT", source_id)
    if len(html) > _MAX_BYTES:
        return HolidayCandidate("INPUT_TOO_LARGE", source_id)
    digest = hashlib.sha256(html).hexdigest()
    try:
        text = html.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return HolidayCandidate("INVALID_UTF8", source_id, digest)
    try:
        parser = _ArticleParser()
        parser.feed(text)
        parser.close()
        if (
            parser.depth
            or parser.container is not None
            or parser.title is not None
            or not parser.titles
            or not parser.articles
        ):
            raise _Invalid
        titles = {
            _TITLE.fullmatch(title).group(1) for title in parser.titles if _TITLE.fullmatch(title)
        }
        if len(titles) != 1 or any(_TITLE.fullmatch(title) is None for title in parser.titles):
            raise _Invalid
        year = int(titles.pop())
        if any(article != parser.articles[0] for article in parser.articles[1:]):
            raise _Invalid
        parsed = [_row(row, year) for row in parser.articles[0]]
        unique = {}
        openings = {}
        for ordinal, row, opened in parsed:
            key = (ordinal, row.name)
            if key in unique and unique[key] != row:
                raise _Invalid
            unique[key] = row
            openings[key] = opened
        expected = tuple(zip("一二三四五六七", _EXPECTED))
        if set(unique) != set(expected):
            return HolidayCandidate(
                "NEEDS_REVIEW", source_id, digest, year, reasons=("MISSING_OR_UNEXPECTED_SECTION",)
            )
        rows = tuple(unique[key] for key in expected)
        if any(a.closure_end >= b.closure_start for a, b in zip(rows, rows[1:])):
            raise _Invalid
        if any(
            row.closure_start <= opened <= row.closure_end or opened in row.weekend_closures
            for opened in openings.values()
            for row in rows
        ):
            raise _Invalid
        return HolidayCandidate(
            "CANDIDATE", source_id, digest, year, rows, raw_title=parser.titles[0]
        )
    except (ValueError, AssertionError, _Invalid, RecursionError):
        return HolidayCandidate("STRUCTURE_CHANGED", source_id, digest)
