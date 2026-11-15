"""Offline discovery of unverified links in a narrow SHFE desktop navigation.

Input bytes and source URL are caller-supplied; no request, file access or script
execution occurs. Labels and paths are observations, not product codes, venue,
effective dates, completeness, source authenticity or permission to use data.
"""

import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

MAX_BYTES = 1024 * 1024
MAX_EVENTS = 80_000
MAX_LINKS = 512
MAX_LIST_ITEMS = 1024
_SOURCES = frozenset(
    {
        "https://www.shfe.com.cn/products/futures/metal/nonferrousmetal/cu_f/",
        "https://www.shfe.com.cn/products/option/nonferrousmetal/cu_o/",
        "https://www.shfe.com.cn/products/option/nonferrousmetal/bc_o/",
    }
)
_PATH = re.compile(
    r"/products/(futures|option)/(?:[a-z0-9]+/|index_f/)+[a-z0-9]+_([fo])/\Z", re.ASCII
)


@dataclass(frozen=True)
class NavigationLink:
    kind: str
    label: str
    path: str


@dataclass(frozen=True)
class NavigationResult:
    status: str
    source_url: Optional[str] = None
    sha256: Optional[str] = None
    links: tuple[NavigationLink, ...] = ()
    review_reasons: tuple[str, ...] = ()
    verification: str = "UNVERIFIED"
    source_authentication: str = "NOT_PROVEN"
    venue: str = "UNKNOWN"
    product_identity: str = "UNKNOWN"
    effective_date: str = "UNKNOWN"
    completeness: str = "NOT_PROVEN"
    permission: str = "NOT_PROVEN"


class _InvalidNavigation(ValueError):
    pass


def _control(text):
    return any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in text)


def _classes(attrs):
    classes = [value for name, value in attrs if name == "class"]
    if len(classes) > 1 or (classes and type(classes[0]) is not str):
        raise _InvalidNavigation
    return set(classes[0].split()) if classes else set()


def _only_class(attrs, expected):
    return len(attrs) == 1 and attrs[0][0] == "class" and _classes(attrs) == expected


def _path_kind(path):
    if type(path) is not str or len(path) > 512 or _control(path):
        raise _InvalidNavigation
    match = _PATH.fullmatch(path)
    if not match or (match.group(1), match.group(2)) not in {("futures", "f"), ("option", "o")}:
        raise _InvalidNavigation
    parts = path.split("/")
    if (
        "index_f" in parts
        and re.fullmatch(r"/products/futures/index_f/[a-z0-9]+_f/", path, re.ASCII) is None
    ):
        raise _InvalidNavigation
    return "future" if match.group(1) == "futures" else "option"


class _NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.events = 0
        self.depth = 0
        self.headers_depth = None
        self.header_depth = None
        self.product_depth = None
        self.item_depth = None
        self.content_depth = None
        self.item_label = []
        self.item_aux = False
        self.item_had_content = False
        self.kind = None
        self.seen_sections = set()
        self.header_count = 0
        self.container_count = 0
        self.nav_count = 0
        self.in_ul = False
        self.li = None
        self.anchor = None
        self.links = []
        self.list_items = 0
        self.aux_count = 0
        self.closed = False

    def _tick(self):
        self.events += 1
        if self.events > MAX_EVENTS:
            raise _InvalidNavigation

    def handle_starttag(self, tag, attrs):
        self._tick()
        if self.anchor is not None:
            raise _InvalidNavigation
        if tag == "div":
            self.depth += 1
            if self.content_depth is not None:
                raise _InvalidNavigation
            classes = _classes(attrs)
            if "header-subs" in classes:
                if not _only_class(attrs, {"header-subs"}):
                    raise _InvalidNavigation
                self.nav_count += 1
                if self.nav_count != 1:
                    raise _InvalidNavigation
                self.headers_depth = self.depth
            elif (
                self.headers_depth is not None
                and self.depth == self.headers_depth + 1
                and "header-sub" in classes
            ):
                self.header_count += 1
                if self.header_count == 1:
                    if not _only_class(attrs, {"header-sub"}):
                        raise _InvalidNavigation
                    self.header_depth = self.depth
            elif (
                self.header_depth is not None
                and self.depth == self.header_depth + 1
                and {"subContent", "sub"} <= classes
            ):
                if not _only_class(attrs, {"subContent", "sub"}):
                    raise _InvalidNavigation
                self.container_count += 1
                if self.container_count != 1:
                    raise _InvalidNavigation
                self.product_depth = self.depth
            elif (
                self.product_depth is not None
                and self.depth == self.product_depth + 1
                and "subItem" in classes
            ):
                if not _only_class(attrs, {"subItem"}):
                    raise _InvalidNavigation
                if self.item_depth is not None:
                    raise _InvalidNavigation
                self.item_depth = self.depth
                self.item_label = []
                self.item_aux = False
                self.item_had_content = False
            elif (
                self.item_depth is not None
                and self.depth == self.item_depth + 1
                and "subItemContent" in classes
            ):
                if not _only_class(attrs, {"subItemContent"}):
                    raise _InvalidNavigation
                if self.item_had_content:
                    raise _InvalidNavigation
                self.item_had_content = True
                self.content_depth = self.depth
                label = "".join(self.item_label).strip()
                if label == "期货":
                    self.kind = "future"
                elif label == "期权":
                    self.kind = "option"
                elif self.item_aux and not label:
                    self.kind = "aux"
                else:
                    raise _InvalidNavigation
                if self.kind != "aux":
                    if self.kind in self.seen_sections:
                        raise _InvalidNavigation
                    self.seen_sections.add(self.kind)
            elif self.product_depth is not None:
                if self.depth != self.product_depth + 1 or not _only_class(attrs, {"leftLine"}):
                    raise _InvalidNavigation
            return
        if self.product_depth is None:
            return
        if self.content_depth is not None and self.kind in {"future", "option"}:
            if tag == "ul":
                if self.in_ul or self.li is not None or not _only_class(attrs, {"itemContent"}):
                    raise _InvalidNavigation
                self.in_ul = True
            elif tag == "li":
                if not self.in_ul or self.li is not None:
                    raise _InvalidNavigation
                self.list_items += 1
                if self.list_items > MAX_LIST_ITEMS:
                    raise _InvalidNavigation
                classes = _classes(attrs)
                if not _only_class(attrs, classes) or classes not in ({"isActive"}, {"left"}):
                    raise _InvalidNavigation
                self.li = ["active" if "isActive" in classes else "left", 0]
            elif tag == "a":
                if self.li is None or self.li[0] != "active" or self.li[1] != 0:
                    raise _InvalidNavigation
                if len(attrs) != 1 or attrs[0][0] != "href" or "&" in self.get_starttag_text():
                    raise _InvalidNavigation
                path = attrs[0][1]
                if _path_kind(path) != self.kind:
                    raise _InvalidNavigation
                self.li[1] += 1
                self.anchor = ["link", path, []]
            elif self.li is not None:
                raise _InvalidNavigation
            elif tag != "ul":
                raise _InvalidNavigation
        elif tag == "a" and self.item_depth is not None and self.content_depth is None:
            # The only observed non-product entry in this container is ISIN.
            if self.item_aux or len(attrs) != 2 or len({name for name, _ in attrs}) != 2:
                raise _InvalidNavigation
            attr = dict(attrs)
            if (
                attr != {"class": "btn_lv", "href": "/products/isin/"}
                or "&" in self.get_starttag_text()
            ):
                raise _InvalidNavigation
            self.anchor = ["aux", "", []]
        elif tag == "a":
            raise _InvalidNavigation
        else:
            raise _InvalidNavigation

    def handle_startendtag(self, tag, attrs):
        self._tick()
        if self.product_depth is not None:
            raise _InvalidNavigation

    def handle_data(self, data):
        self._tick()
        if self.anchor is not None:
            self.anchor[2].append(data)
            if sum(map(len, self.anchor[2])) > 128:
                raise _InvalidNavigation
        elif (
            self.item_depth is not None
            and self.depth == self.item_depth
            and self.content_depth is None
        ):
            self.item_label.append(data)
            if sum(map(len, self.item_label)) > 1024:
                raise _InvalidNavigation
        elif self.content_depth is not None and self.kind in {"future", "option"}:
            if data.strip() and (self.li is None or self.li[0] != "left"):
                raise _InvalidNavigation
        elif self.product_depth is not None and data.strip():
            raise _InvalidNavigation

    def handle_entityref(self, name):
        self._tick()
        if self.product_depth is not None:
            raise _InvalidNavigation

    def handle_charref(self, name):
        self.handle_entityref(name)

    def handle_comment(self, data):
        self._tick()
        if self.product_depth is not None:
            raise _InvalidNavigation

    def handle_pi(self, data):
        self._tick()
        if self.product_depth is not None:
            raise _InvalidNavigation

    def handle_decl(self, decl):
        self._tick()
        if self.product_depth is not None:
            raise _InvalidNavigation

    def unknown_decl(self, data):
        self._tick()
        if self.product_depth is not None:
            raise _InvalidNavigation

    def handle_endtag(self, tag):
        self._tick()
        if self.anchor is not None:
            if tag != "a":
                raise _InvalidNavigation
            anchor, self.anchor = self.anchor, None
            label = "".join(anchor[2])
            if not label or not label.strip() or len(label) > 128 or _control(label):
                raise _InvalidNavigation
            if anchor[0] == "aux":
                if label != "ISIN编码":
                    raise _InvalidNavigation
                self.item_aux = True
                self.aux_count += 1
                if self.aux_count > 1:
                    raise _InvalidNavigation
            else:
                if len(self.links) >= MAX_LINKS:
                    raise _InvalidNavigation
                self.links.append(NavigationLink(self.kind, label, anchor[1]))
            return
        if (
            self.content_depth is not None
            and self.kind in {"future", "option"}
            and tag not in {"li", "ul", "div"}
        ):
            raise _InvalidNavigation
        if tag == "li" and self.content_depth is not None and self.kind in {"future", "option"}:
            if self.li is None or self.li[1] != (1 if self.li[0] == "active" else 0):
                raise _InvalidNavigation
            self.li = None
        if tag == "ul" and self.content_depth is not None and self.kind in {"future", "option"}:
            if not self.in_ul or self.li is not None:
                raise _InvalidNavigation
            self.in_ul = False
        if tag != "div":
            return
        if self.depth == self.content_depth:
            if self.li is not None or self.in_ul:
                raise _InvalidNavigation
            self.content_depth = None
            self.kind = None
        if self.depth == self.item_depth:
            if not self.item_had_content:
                raise _InvalidNavigation
            self.item_depth = None
        if self.depth == self.product_depth:
            self.product_depth = None
            self.closed = True
        if self.depth == self.header_depth:
            self.header_depth = None
        if self.depth == self.headers_depth:
            self.headers_depth = None
        self.depth -= 1


def parse_shfe_product_navigation(*, source_url: str, html: bytes) -> NavigationResult:
    """Return only candidates from the explicit desktop product navigation."""
    if type(source_url) is not str or source_url not in _SOURCES:
        return NavigationResult("INVALID_SOURCE")
    if type(html) is not bytes:
        return NavigationResult("INVALID_INPUT", source_url)
    if len(html) > MAX_BYTES:
        return NavigationResult("INPUT_TOO_LARGE", source_url)
    digest = hashlib.sha256(html).hexdigest()
    try:
        text = html.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return NavigationResult("INVALID_UTF8", source_url, digest)
    try:
        parser = _NavigationParser()
        parser.feed(text)
        parser.close()
        if (
            not parser.closed
            or parser.nav_count != 1
            or parser.container_count != 1
            or parser.seen_sections != {"future", "option"}
            or parser.anchor is not None
            or parser.li is not None
            or not all(
                any(row.kind == kind for row in parser.links) for kind in ("future", "option")
            )
        ):
            raise _InvalidNavigation
        rows = []
        seen = set()
        paths = {}
        conflict = False
        for row in parser.links:
            key = (row.kind, row.label, row.path)
            if key not in seen:
                rows.append(row)
                seen.add(key)
            if row.path in paths and paths[row.path] != (row.kind, row.label):
                conflict = True
            paths[row.path] = (row.kind, row.label)
        return NavigationResult(
            "NEEDS_REVIEW" if conflict else "CANDIDATE",
            source_url,
            digest,
            tuple(rows),
            ("DUPLICATE_PATH_CONFLICT",) if conflict else (),
        )
    except (ValueError, RecursionError):
        return NavigationResult(
            "STRUCTURE_CHANGED", source_url, digest, review_reasons=("NAVIGATION_INVALID",)
        )
