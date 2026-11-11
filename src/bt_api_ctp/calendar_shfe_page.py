# Keep annotations resolvable by typing.get_type_hints on Python 3.9.
# ruff: noqa: UP045
"""Offline experimental parser for a caller-supplied trading-hours HTML page.

The format is modelled on the public SHFE trading-hours page, which also lists
INE products. The caller supplies UTF-8 bytes and a source identity; neither the
identity nor the SHA-256 of those bytes proves origin, correctness, currency,
venue, product codes, scope completeness or effective dates. Futures and option
names are kept separately: no inheritance, trading-day mapping or holiday rule
is inferred. Results are always UNVERIFIED and are not publishable data.

This parser understands a narrow two-cell product/time table and the explicit
auction-order, auction-match and continuous labels. Unknown structure fails
closed; raw page markup is never returned. An unmarked clock descent is only an
inferred next-day candidate requiring review, not an established session date.
No network, account or filesystem access is performed.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

MAX_BYTES = 1024 * 1024
MAX_EVENTS = 30000
MAX_TABLES = 16
MAX_ROWS = 256
MAX_CELL = 8192
_SOURCE_LIMIT = 512
_PRODUCT_LABEL = re.compile(r"(期货|期权)\s*:\s*")
_PRODUCT = re.compile(r"[A-Za-z0-9\u4e00-\u9fff()·]+\Z")
_STAGE = re.compile(
    r"(集合竞价申报|集合竞价撮合|连续竞价交易)\s*"
    r"([0-9]{1,2}:[0-9]{2})\s*(?:-|–|—|——|至|~)\s*(次日)?\s*([0-9]{1,2}:[0-9]{2})\Z"
)
_SAFE_COMPAT = frozenset("０１２３４５６７８９：－～")
_SYNTAX = frozenset("0123456789:-~")


@dataclass(frozen=True)
class StageCandidate:
    session: str
    phase: str
    start: str
    end: str
    day_transition: str
    raw_line: str


@dataclass(frozen=True)
class ProductCandidate:
    kind: str
    name: str
    raw_product_cell: str
    raw_time_cell: str
    stages: tuple[StageCandidate, ...]


@dataclass(frozen=True)
class PageCandidate:
    status: str
    source_id: Optional[str] = None
    sha256: Optional[str] = None
    products: tuple[ProductCandidate, ...] = ()
    raw_notes: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()
    verification: str = "UNVERIFIED"


class _InvalidPage(Exception):
    pass


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.table = None
        self.row = None
        self.cell = None
        self.cell_size = 0
        self.colspan = None
        self.events = 0

    def _tick(self):
        self.events += 1
        if self.events > MAX_EVENTS:
            raise _InvalidPage

    def handle_starttag(self, tag, attrs):
        self._tick()
        if tag == "table":
            if self.table is not None or len(self.tables) >= MAX_TABLES:
                raise _InvalidPage
            self.table = []
        elif tag == "tr" and self.table is not None:
            if self.row is not None or len(self.table) >= MAX_ROWS:
                raise _InvalidPage
            self.row = []
        elif tag in {"td", "th"} and self.table is not None:
            if self.row is None or self.cell is not None or len(self.row) >= 2:
                raise _InvalidPage
            self.cell = []
            self.cell_size = 0
            if len({name for name, _ in attrs}) != len(attrs):
                raise _InvalidPage
            attrs_map = dict(attrs)
            self.colspan = attrs_map.get("colspan")
        elif tag == "br" and self.cell is not None:
            self.cell.append("\n")
            self.cell_size += 1
            if self.cell_size > MAX_CELL:
                raise _InvalidPage

    def handle_endtag(self, tag):
        self._tick()
        if tag in {"td", "th"} and self.table is not None:
            if self.cell is None or self.row is None:
                raise _InvalidPage
            text = "".join(self.cell)
            if len(text) > MAX_CELL:
                raise _InvalidPage
            self.row.append((text, self.colspan))
            self.cell = None
            self.colspan = None
        elif tag == "tr" and self.table is not None:
            if self.row is None or self.cell is not None:
                raise _InvalidPage
            self.table.append(tuple(self.row))
            self.row = None
        elif tag == "table":
            if self.table is None or self.row is not None or self.cell is not None:
                raise _InvalidPage
            self.tables.append(tuple(self.table))
            self.table = None

    def handle_data(self, data):
        self._tick()
        if self.cell is not None:
            self.cell.append(data)
            self.cell_size += len(data)
            if self.cell_size > MAX_CELL:
                raise _InvalidPage
        elif self.table is not None and data.strip():
            raise _InvalidPage

    def handle_comment(self, data):
        self._tick()
        if self.table is not None:
            raise _InvalidPage


def _canonical(value: str) -> str:
    return "\n".join(" ".join(line.split()) for line in value.splitlines() if line.strip())


def _unsafe_compat(value: str) -> bool:
    return any(
        char not in _SAFE_COMPAT
        and unicodedata.normalize("NFKC", char) != char
        and any(part in _SYNTAX for part in unicodedata.normalize("NFKC", char))
        for char in value
    )


def _products(text: str) -> tuple[tuple[str, str], ...]:
    normalized = " ".join(unicodedata.normalize("NFKC", text).split())
    labels = list(_PRODUCT_LABEL.finditer(normalized))
    if not labels or normalized[: labels[0].start()].strip():
        raise _InvalidPage
    result = []
    used_kinds = set()
    for index, label in enumerate(labels):
        kind = "future" if label.group(1) == "期货" else "option"
        if kind in used_kinds:
            raise _InvalidPage
        used_kinds.add(kind)
        next_start = labels[index + 1].start() if index + 1 < len(labels) else len(normalized)
        names = normalized[label.end() : next_start].strip().split("、")
        for name in names:
            name = name.strip()
            if _PRODUCT.fullmatch(name) is None or (kind, name) in result:
                raise _InvalidPage
            result.append((kind, name))
    return tuple(result)


def _stages(text: str) -> tuple[tuple[StageCandidate, ...], bool, bool]:
    if _unsafe_compat(text):
        raise _InvalidPage
    session = None
    sessions = set()
    stages = []
    ranges = []
    uncertain = False
    for raw_line in text.splitlines():
        raw_line = " ".join(raw_line.split())
        if not raw_line:
            continue
        line = unicodedata.normalize("NFKC", raw_line)
        if line in {"日盘", "夜盘"}:
            session = "DAY" if line == "日盘" else "NIGHT"
            if session in sessions:
                raise _InvalidPage
            sessions.add(session)
            continue
        match = _STAGE.fullmatch(line)
        if match is None or session is None:
            raise _InvalidPage
        label, start, next_day, end = match.groups()
        start_h, start_m = map(int, start.split(":"))
        end_h, end_m = map(int, end.split(":"))
        if start_h > 23 or end_h > 23 or start_m > 59 or end_m > 59:
            raise _InvalidPage
        a, b = start_h * 60 + start_m, end_h * 60 + end_m
        if a == b or (next_day and (session != "NIGHT" or b >= a)):
            raise _InvalidPage
        transition = "EXPLICIT_NEXT_DAY" if next_day else "SAME_CLOCK_DAY"
        if b < a and not next_day:
            transition = "INFERRED_NEXT_DAY"
            uncertain = True
        if b < a and session != "NIGHT":
            raise _InvalidPage
        phase = {
            "集合竞价申报": "AUCTION_ORDER",
            "集合竞价撮合": "AUCTION_MATCH",
            "连续竞价交易": "CONTINUOUS",
        }[label]
        stages.append(
            StageCandidate(
                session,
                phase,
                f"{start_h:02}:{start_m:02}",
                f"{end_h:02}:{end_m:02}",
                transition,
                raw_line,
            )
        )
        ranges.append((session, a, b + (1440 if b < a else 0)))
    required_phases = {"AUCTION_ORDER", "AUCTION_MATCH", "CONTINUOUS"}
    if "DAY" not in sessions or any(
        {stage.phase for stage in stages if stage.session == name} != required_phases
        for name in sessions
    ):
        raise _InvalidPage
    auction_keys = [
        (stage.session, stage.phase)
        for stage in stages
        if stage.phase in {"AUCTION_ORDER", "AUCTION_MATCH"}
    ]
    conflict = any(
        sa == sb and a1 < b2 and a2 < b1
        for i, (sa, a1, b1) in enumerate(ranges)
        for sb, a2, b2 in ranges[i + 1 :]
    ) or len(auction_keys) != len(set(auction_keys))
    return tuple(stages), uncertain, conflict


def parse_shfe_page(*, source_id: str, html: bytes) -> PageCandidate:
    """Return bounded, unverified text candidates from caller-supplied bytes."""
    if type(source_id) is not str or not source_id.strip() or len(source_id) > _SOURCE_LIMIT:
        return PageCandidate("INVALID_SOURCE")
    if type(html) is not bytes:
        return PageCandidate("INVALID_INPUT", source_id)
    if len(html) > MAX_BYTES:
        return PageCandidate("INPUT_TOO_LARGE", source_id)
    digest = hashlib.sha256(html).hexdigest()
    try:
        text = html.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return PageCandidate("INVALID_UTF8", source_id, digest)
    try:
        parser = _TableParser()
        parser.feed(text)
        parser.close()
        if parser.table is not None or parser.row is not None or parser.cell is not None:
            raise _InvalidPage
        if not parser.tables:
            raise _InvalidPage
        signatures = set()
        unique_tables = []
        for table in parser.tables:
            signature = table
            if signature not in signatures:
                signatures.add(signature)
                unique_tables.append(table)
        products = []
        notes = []
        signatures_by_product = {}
        uncertain = False
        conflict = False
        for table in unique_tables:
            if not table:
                raise _InvalidPage
            for row in table:
                if len(row) == 1 and row[0][1] == "2" and _canonical(row[0][0]).startswith("注"):
                    notes.append(row[0][0])
                    continue
                if len(row) != 2 or any(span not in (None, "1") for _, span in row):
                    raise _InvalidPage
                raw_product, raw_times = row[0][0], row[1][0]
                names = _products(raw_product)
                stages, ambiguous, row_conflict = _stages(raw_times)
                uncertain |= ambiguous
                conflict |= row_conflict
                signature = tuple(
                    (s.session, s.phase, s.start, s.end, s.day_transition) for s in stages
                )
                for kind, name in names:
                    key = (kind, name)
                    if key in signatures_by_product and signatures_by_product[key] != signature:
                        conflict = True
                    signatures_by_product.setdefault(key, signature)
                    products.append(ProductCandidate(kind, name, raw_product, raw_times, stages))
        if not products:
            raise _InvalidPage
        reasons = []
        if uncertain:
            reasons.append("CROSS_MIDNIGHT_UNVERIFIED")
        if notes:
            reasons.append("PAGE_NOTE_UNVERIFIED")
        if conflict:
            reasons.append("PRODUCT_OR_PHASE_CONFLICT")
        return PageCandidate(
            "CONFLICT" if conflict else "NEEDS_REVIEW" if reasons else "CANDIDATE",
            source_id,
            digest,
            tuple(products),
            tuple(notes),
            tuple(reasons),
        )
    except Exception:
        # Never include source markup, parser exceptions or partially parsed rows.
        return PageCandidate("STRUCTURE_CHANGED", source_id, digest)
