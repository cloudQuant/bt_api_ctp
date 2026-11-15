"""Parse bounded caller-supplied SHFE/INE product pages as unverified candidates.

This module does not fetch pages, execute JavaScript, choose a current record,
interpret trading hours, or establish source authenticity or effective dates.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlsplit

MAX_BYTES = 1024 * 1024
_MAX_SCRIPT = 512 * 1024
_MAX_RECORDS = 100
_MAX_FIELDS = 100
_MAX_FIELD = 8192
_PATH = re.compile(r"/products/(futures|option)/(?:[a-z0-9]+/|index_f/)+[a-z0-9]+_([fo])/")
_INDEX_FUTURE_PATH = re.compile(r"/products/futures/index_f/[a-z0-9]+_f/\Z", re.ASCII)
_DECLARATION = re.compile(r"\blet\s+(pageList(?:_o)?)\s*=")
_FUTURE_CODE = re.compile(r"[A-Z]{1,5}\Z")
_OPTION_CODE = re.compile(
    r"看涨期权：([A-Z]{1,5})-合约月份-C-行权价格；\s*"
    r"看跌期权：([A-Z]{1,5})-合约月份-P-行权价格\Z"
)
_VENUES = {"上海期货交易所": "SHFE", "上海国际能源交易中心": "INE"}


@dataclass(frozen=True)
class ProductRecord:
    kind: str
    symbol: Optional[str]
    venue: Optional[str]
    raw_date: Optional[str]
    fields: tuple[tuple[str, str], ...]
    unknown_reasons: tuple[str, ...]
    effective_date: str = "UNKNOWN"


@dataclass(frozen=True)
class ProductPage:
    status: str
    source_url: Optional[str] = None
    sha256: Optional[str] = None
    records: tuple[ProductRecord, ...] = ()
    review_reasons: tuple[str, ...] = ()
    verification: str = "UNVERIFIED"
    source_authentication: str = "NOT_PROVEN"
    current_record: str = "UNKNOWN"
    coverage: str = "UNKNOWN"


class _InvalidPage(ValueError):
    pass


class _ScriptCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_script = False
        self.parts = []
        self.length = 0
        self.scripts = []
        self.attrs = ()

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            if self.in_script:
                raise _InvalidPage
            self.in_script = True
            self.parts = []
            self.length = 0
            self.attrs = tuple(attrs)

    def handle_data(self, data):
        if self.in_script:
            self.length += len(data)
            if self.length > _MAX_SCRIPT:
                raise _InvalidPage
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_script:
            self.scripts.append(("".join(self.parts), self.attrs))
            self.in_script = False


def _url_kind(url):
    if type(url) is not str or len(url) > 512:
        return None
    try:
        p = urlsplit(url)
        if (
            p.scheme != "https"
            or p.netloc not in {"www.shfe.com.cn", "www.ine.cn"}
            or p.query
            or p.fragment
        ):
            return None
    except ValueError:
        return None
    match = _PATH.fullmatch(p.path)
    if not match or (match.group(1), match.group(2)) not in {
        ("futures", "f"),
        ("option", "o"),
    }:
        return None
    if "index_f" in p.path.split("/") and _INDEX_FUTURE_PATH.fullmatch(p.path) is None:
        return None
    return "future" if match.group(1) == "futures" else "option"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _InvalidPage
        result[key] = value
    return result


def _literal(script, kind):
    matches = list(_DECLARATION.finditer(script))
    expected = "pageList" if kind == "future" else "pageList_o"
    if len(matches) != 1 or matches[0].group(1) != expected or script[: matches[0].start()].strip():
        raise _InvalidPage
    literal = script[matches[0].end() :].strip()
    # Only whitespace, JSON tokens and a trailing comma before ] or } are
    # normalized. json.loads still rejects comments, JS expressions and keys.
    if literal.endswith(";"):
        literal = literal[:-1].rstrip()
    normalized = []
    in_string = False
    escaped = False
    index = 0
    while index < len(literal):
        char = literal[index]
        if in_string:
            normalized.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            normalized.append(char)
        elif char == "\u00a0":
            normalized.append(" ")
        elif char == ",":
            following = index + 1
            while following < len(literal) and literal[following] in " \t\r\n\u00a0":
                following += 1
            if following == len(literal) or literal[following] not in "]}":
                normalized.append(char)
        else:
            normalized.append(char)
        index += 1
    try:
        value = json.loads(
            "".join(normalized),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(_InvalidPage()),
        )
    except (ValueError, RecursionError) as exc:
        raise _InvalidPage from exc
    if type(value) is not list or not 0 < len(value) <= _MAX_RECORDS:
        raise _InvalidPage
    for row in value:
        if type(row) is not dict or not 0 < len(row) <= _MAX_FIELDS:
            raise _InvalidPage
        if any(
            type(k) is not str
            or type(v) is not str
            or len(k) > 100
            or len(v) > _MAX_FIELD
            or any(0xD800 <= ord(c) <= 0xDFFF for c in k + v)
            for k, v in row.items()
        ):
            raise _InvalidPage
    return value


def _record(row, kind):
    reasons = []
    code = row.get("ContractSymbol", "")
    if kind == "future":
        symbol = code if _FUTURE_CODE.fullmatch(code) else None
    else:
        match = _OPTION_CODE.fullmatch(code)
        symbol = match.group(1) if match and match.group(1) == match.group(2) else None
    if symbol is None:
        reasons.append("SYMBOL_UNKNOWN")
    exchanges = [row.get(k, "") for k in ("ListingExchange", "ListingExchange1")]
    nonempty = [exchange for exchange in exchanges if exchange]
    venue = _VENUES.get(nonempty[0]) if len(nonempty) == 1 else None
    if venue is None:
        reasons.append("VENUE_UNKNOWN")
    date = row.get("data_standard") or None
    if date is None:
        reasons.append("SOURCE_DATE_UNKNOWN")
    return ProductRecord(kind, symbol, venue, date, tuple(row.items()), tuple(reasons))


def parse_shfe_product_page(*, source_url: str, html: bytes) -> ProductPage:
    """Return raw records in source order; a URL or hash does not attest origin."""
    kind = _url_kind(source_url)
    if kind is None:
        return ProductPage("INVALID_SOURCE")
    if type(html) is not bytes:
        return ProductPage("INVALID_INPUT", source_url)
    if len(html) > MAX_BYTES:
        return ProductPage("INPUT_TOO_LARGE", source_url)
    digest = hashlib.sha256(html).hexdigest()
    try:
        text = html.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return ProductPage("INVALID_UTF8", source_url, digest)
    try:
        parser = _ScriptCollector()
        parser.feed(text)
        parser.close()
        if parser.in_script:
            raise _InvalidPage
        matches = [
            (script, attrs) for script, attrs in parser.scripts if _DECLARATION.search(script)
        ]
        if len(matches) != 1 or any(name == "src" for name, _ in matches[0][1]):
            raise _InvalidPage
        records = tuple(_record(row, kind) for row in _literal(matches[0][0], kind))
    except (ValueError, RecursionError):
        return ProductPage("STRUCTURE_CHANGED", source_url, digest)
    reasons = []
    if any(row.unknown_reasons for row in records):
        reasons.append("RECORD_UNRESOLVED")
    if len({row.symbol for row in records if row.symbol is not None}) > 1:
        reasons.append("SYMBOL_CONFLICT")
    if len({row.venue for row in records if row.venue is not None}) > 1:
        reasons.append("VENUE_CONFLICT")
    status = "NEEDS_REVIEW" if reasons else "CANDIDATE"
    return ProductPage(status, source_url, digest, records, tuple(reasons))
