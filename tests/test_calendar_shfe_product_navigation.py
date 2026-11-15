"""Synthetic navigation only; never accesses network or private inputs."""

import hashlib

import pytest

from bt_api_ctp import calendar_shfe_product_navigation as nav

SOURCE = "https://www.shfe.com.cn/products/futures/metal/nonferrousmetal/cu_f/"
FUTURE = "/products/futures/metal/nonferrousmetal/cu_f/"
OPTION = "/products/option/nonferrousmetal/cu_o/"
INDEX = "/products/futures/index_f/ec_f/"


def item(path, label="铜"):
    return f'<li class="isActive"><a href="{path}">{label}</a></li>'


def page(futures=None, options=None):
    if futures is None:
        futures = item(FUTURE)
    if options is None:
        options = item(OPTION)
    return (
        '<div class="header-subs"><div class="header-sub"><div class="subContent sub">'
        '<div class="subItem">期货<div class="subItemContent"><ul class="itemContent">'
        '<li class="left">金属</li>' + futures + "</ul></div></div>"
        '<div class="subItem">期权<div class="subItemContent"><ul class="itemContent">'
        + options
        + "</ul></div></div>"
        '<div class="subItem"><a class="btn_lv" href="/products/isin/">ISIN编码</a>'
        '<div class="subItemContent"></div></div></div></div></div>'
    ).encode()


def parse(content):
    return nav.parse_shfe_product_navigation(source_url=SOURCE, html=content)


def test_explicit_desktop_sections_preserve_names_paths_and_unverified_status():
    source = page(futures=item(FUTURE) + item(INDEX, "SCFIS欧线"))
    result = parse(source)
    assert result.status == "CANDIDATE" and result.sha256 == hashlib.sha256(source).hexdigest()
    assert [(r.kind, r.label, r.path) for r in result.links] == [
        ("future", "铜", FUTURE),
        ("future", "SCFIS欧线", INDEX),
        ("option", "铜", OPTION),
    ]
    assert result.verification == "UNVERIFIED" and result.source_authentication == "NOT_PROVEN"
    assert result.venue == result.product_identity == result.effective_date == "UNKNOWN"
    assert result.completeness == result.permission == "NOT_PROVEN"
    assert not hasattr(result.links[0], "symbol")


def test_identical_duplicate_is_deduped_but_conflicting_label_is_preserved_for_review():
    result = parse(page(futures=item(FUTURE) + item(FUTURE)))
    assert result.status == "CANDIDATE" and len(result.links) == 2
    result = parse(page(futures=item(FUTURE) + item(FUTURE, "铜另一名称")))
    assert result.status == "NEEDS_REVIEW" and result.review_reasons == ("DUPLICATE_PATH_CONFLICT",)
    assert [(row.path, row.label) for row in result.links[:2]] == [
        (FUTURE, "铜"),
        (FUTURE, "铜另一名称"),
    ]


def test_same_label_different_paths_are_not_conflated_or_assigned_a_venue():
    bc = "/products/futures/metal/nonferrousmetal/bc_f/"
    result = parse(page(futures=item(FUTURE) + item(bc)))
    assert result.status == "CANDIDATE" and result.links[0].label == result.links[1].label == "铜"
    assert result.links[0].path != result.links[1].path and result.venue == "UNKNOWN"


@pytest.mark.parametrize(
    "path",
    [
        "//www.shfe.com.cn/products/futures/metal/cu_f/",
        "https://www.shfe.com.cn/products/futures/metal/cu_f/",
        "/products//futures/metal/cu_f/",
        "/products/futures/metal/cu_f/?x=1",
        "/products/futures/metal/cu_f/#x",
        "/products/futures/metal/../cu_f/",
        "/products/futures/metal/./cu_f/",
        "/products/futures/metal%2Fcu_f/",
        "/products/futures/metal&#x2f;cu_f/",
        "/products/futures/metal\\cu_f/",
        "/products/futures/metal/cu_f/\x01",
        "/products/futures/metal/cu_o/",
        "/products/option/metal/cu_o/",
        "/products/futures/metal/cu_f",
        "/products/futures/metal/CU_f/",
        "/products/futures/foo_bar/cu_f/",
        "/products/futures/metal/index_f/cu_f/",
        "/products/option/index_f/cu_o/",
    ],
)
def test_invalid_suspicious_or_wrong_section_path_rejects_entire_navigation(path):
    result = parse(page(futures=item(FUTURE) + item(path)))
    assert result.status == "STRUCTURE_CHANGED" and result.links == ()
    assert result.review_reasons == ("NAVIGATION_INVALID",)


@pytest.mark.parametrize(
    "bad",
    [
        f'<li class="isActive"><a href="{FUTURE}" href="{FUTURE}">铜</a></li>',
        f'<li class="isActive"><a href="{FUTURE}" HREF="{FUTURE}">铜</a></li>',
        '<li class="isActive"><a>铜</a></li>',
        f'<li class="isActive"><a href="{FUTURE}" onclick="alert(1)">铜</a></li>',
        f'<li class="isActive"><a href="{FUTURE}">\x1b铜</a></li>',
        f'<li class="isActive"><a href="{FUTURE}">铜&#x41;</a></li>',
        f'<li class="isActive"><a href="{FUTURE}"><b>铜</b></a></li>',
        f'<li class="isActive"><a href="{FUTURE}">铜<!-- hidden --></a></li>',
        '<li class="isActive">not a link</li>',
        '<li class="unknown">unknown</li>',
        f'<a href="{FUTURE}">outside-list-item</a>',
        "<script>unexpected active content</script>",
    ],
)
def test_suspicious_nav_item_is_never_silently_skipped(bad):
    result = parse(page(futures=item(FUTURE) + bad))
    assert result.status == "STRUCTURE_CHANGED" and result.links == ()


def test_source_and_scope_are_explicit_and_mobile_or_breadcrumb_links_ignored():
    source = page() + f'<nav id="main-nav"><a href="{INDEX}">mobile</a></nav>'.encode()
    result = parse(source)
    assert result.status == "CANDIDATE" and len(result.links) == 2
    assert (
        nav.parse_shfe_product_navigation(source_url="https://evil.invalid/", html=source).status
        == "INVALID_SOURCE"
    )
    assert parse(f'<a href="{FUTURE}">breadcrumb</a>'.encode()).status == "STRUCTURE_CHANGED"


def test_both_sections_and_valid_nesting_are_required():
    content = page()
    variants = [
        content.replace("期权".encode(), "未认识".encode(), 1),
        content.replace(b'class="header-subs"', b'class="header-other"', 1),
        content.replace(b'class="itemContent"', b'class="unexpected"', 1),
        content.replace(b"</ul>", b"", 1),
        content + content,
    ]
    for variant in variants:
        assert parse(variant).status == "STRUCTURE_CHANGED"


@pytest.mark.parametrize(
    "unknown",
    [
        '<div class="newProducts">NEW PRODUCT SECTION</div>',
        "<section>NEW PRODUCT SECTION</section>",
        "NEW PRODUCT SECTION",
        '<div class="leftLine" hidden></div>',
        "&#65;",
        "&amp;",
        "<?unexpected foo?>",
        "<!DOCTYPE html>",
        "<!unexpected foo>",
        "<!-- unexpected product content -->",
    ],
)
def test_unknown_product_container_child_is_not_ignored(unknown):
    content = page().replace(
        b'<div class="subItem">', unknown.encode() + b'<div class="subItem">', 1
    )
    assert parse(content).status == "STRUCTURE_CHANGED"


@pytest.mark.parametrize(
    "old,new",
    [
        (b'<li class="isActive">', b'<li class="isActive" hidden>'),
        (b'<li class="isActive">', b'<li class="isActive" style="display:none">'),
        (b'<ul class="itemContent">', b'<ul class="itemContent" aria-hidden="true">'),
        (b'<div class="subItem">', b'<div class="subItem" hidden>'),
        (b'<div class="header-subs">', b'<div class="header-subs" hidden>'),
    ],
)
def test_unknown_visibility_attributes_are_rejected(old, new):
    assert parse(page().replace(old, new, 1)).status == "STRUCTURE_CHANGED"


def test_input_and_explicit_resource_limits(monkeypatch):
    assert nav.parse_shfe_product_navigation(source_url=SOURCE, html=None).status == "INVALID_INPUT"
    assert parse(b"x" * (nav.MAX_BYTES + 1)).status == "INPUT_TOO_LARGE"
    assert parse(b"\xff").status == "INVALID_UTF8"
    monkeypatch.setattr(nav, "MAX_LINKS", 1)
    assert parse(page()).status == "STRUCTURE_CHANGED"
    monkeypatch.setattr(nav, "MAX_LINKS", 512)
    monkeypatch.setattr(nav, "MAX_EVENTS", 4)
    assert parse(page()).status == "STRUCTURE_CHANGED"
