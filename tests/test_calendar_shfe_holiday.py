"""Offline synthetic examples; no source, coverage or exchange facts are verified."""

import hashlib
from datetime import date

import pytest

from bt_api_ctp.calendar_shfe_holiday import parse_shfe_holiday

ROWS = (
    "一、元旦：1月1日（星期四）至1月3日（星期六）休市，1月5日（星期一）起照常开市。"
    "1月4日（星期日）为周末休市。2025年12月31日（星期三）晚上不进行夜盘交易。",
    "二、春节：2月15日（星期日）至2月23日（星期一）休市，2月24日（星期二）起照常开市。"
    "2月14日（星期六）、2月28日（星期六）为周末休市。2月13日（星期五）晚上不进行夜盘交易。",
    "三、清明节：4月4日（星期六）至4月6日（星期一）休市，4月7日（星期二）起照常开市。"
    "4月3日（星期五）晚上不进行夜盘交易。",
    "四、劳动节：5月1日（星期五）至5月5日（星期二）休市，5月6日（星期三）起照常开市。"
    "5月9日（星期六）为周末休市。4月30日（星期四）晚上不进行夜盘交易。",
    "五、端午节：6月19日（星期五）至6月21日（星期日）休市，6月22日（星期一）起照常开市。"
    "6月18日（星期四）晚上不进行夜盘交易。",
    "六、中秋节：9月25日（星期五）至9月27日（星期日）休市，9月28日（星期一）起照常开市。"
    "9月24日（星期四）晚上不进行夜盘交易。",
    "七、国庆节：10月1日（星期四）至10月7日（星期三）休市，10月8日（星期四）起照常开市。"
    "9月20日（星期日）、10月10日（星期六）为周末休市。9月30日（星期三）晚上不进行夜盘交易。",
)


def page(rows=ROWS, *, title="2026年休市安排", copies=1):
    article = (
        '<div class="article_content_item"><div class="TRS_Editor">'
        + "".join(f"<div>{row}</div>" for row in rows)
        + "</div></div>"
    )
    return (
        f'<div class="article_title" id="no_article_empty">{title}</div>' + article * copies
    ).encode("utf-8")


def parse(html):
    return parse_shfe_holiday(source_id="synthetic", html=html)


def test_explicit_dates_and_no_inference_with_duplicate_displays():
    html = page(copies=2)
    result = parse(html)
    assert (result.status, result.source_id, result.year, result.raw_title) == (
        "CANDIDATE",
        "synthetic",
        2026,
        "2026年休市安排",
    )
    assert (result.verification, result.coverage, result.sha256) == (
        "UNVERIFIED",
        "NOT_PROVEN",
        hashlib.sha256(html).hexdigest(),
    )
    assert len(result.rows) == 7
    first, spring = result.rows[:2]
    assert (first.closure_start, first.closure_end, first.no_night) == (
        date(2026, 1, 1),
        date(2026, 1, 3),
        date(2025, 12, 31),
    )
    assert first.weekend_closures == (date(2026, 1, 4),)
    assert spring.weekend_closures == (date(2026, 2, 14), date(2026, 2, 28))
    assert first.raw_text == ROWS[0]
    assert not hasattr(result, "trading_days")


def test_duplicate_row_is_deduplicated():
    result = parse(page(rows=(ROWS[0], ROWS[0], *ROWS[1:])))
    assert result.status == "CANDIDATE"
    assert len(result.rows) == 7


@pytest.mark.parametrize(
    "old,new",
    [
        ("2025年12月31日", "12月31日"),  # ambiguous previous year
        ("2025年12月31日", "2024年12月31日"),
        ("2025年12月31日", "2027年12月31日"),
        ("12月31日（星期三）", "12月31日（星期四）"),
        ("1月3日（星期六）", "1月32日（星期六）"),
        ("1月1日（星期四）", "2025年1月1日（星期三）"),
        ("2月14日（星期六）", "2月16日（星期一）"),
        ("起照常开市", "提前开市"),
        ("晚上不进行夜盘交易", "晚上可能不进行夜盘交易"),
        ("1月1日（星期四）", "1月1日（星期三）"),
    ],
)
def test_unsupported_or_inconsistent_dates_fail_closed(old, new):
    assert parse(page().replace(old.encode(), new.encode())).status == "STRUCTURE_CHANGED"


def test_missing_section_and_unknown_section_are_not_complete_candidates():
    missing = parse(page(rows=ROWS[:-1]))
    assert (missing.status, missing.rows) == ("NEEDS_REVIEW", ())
    unknown = page(rows=ROWS + ("八、其他节日：另行通知。",))
    assert parse(unknown).status == "STRUCTURE_CHANGED"


def test_conflicting_duplicate_and_displays_fail_closed():
    conflict = ROWS[0].replace("1月3日（星期六）", "1月2日（星期五）")
    assert parse(page(rows=(ROWS[0], conflict, *ROWS[1:]))).status == "STRUCTURE_CHANGED"
    other = page(rows=(conflict, *ROWS[1:]))
    assert parse(page() + other).status == "STRUCTURE_CHANGED"


def test_unrecognised_text_markup_title_and_article_are_rejected():
    assert (
        parse(
            page().replace(
                "晚上不进行夜盘交易。".encode(), "晚上不进行夜盘交易。另行通知。".encode(), 1
            )
        ).status
        == "STRUCTURE_CHANGED"
    )
    assert (
        parse(page().replace("元旦".encode(), "<b>元旦</b>".encode(), 1)).status
        == "STRUCTURE_CHANGED"
    )
    assert parse(page(title="2026年交易安排")).status == "STRUCTURE_CHANGED"
    assert (
        parse(page() + '<div class="m_content_tt">2027年休市安排</div>'.encode()).status
        == "STRUCTURE_CHANGED"
    )
    assert parse(b"<html></html>").status == "STRUCTURE_CHANGED"


def test_duplicates_weekend_and_conflicting_ranges_rejected():
    repeated = ROWS[0].replace("1月4日（星期日）", "1月4日（星期日）、1月4日（星期日）")
    assert parse(page(rows=(repeated, *ROWS[1:]))).status == "STRUCTURE_CHANGED"
    swapped = ROWS[1].replace("2月15日（星期日）", "1月2日（星期五）")
    assert parse(page(rows=(ROWS[0], swapped, *ROWS[2:]))).status == "STRUCTURE_CHANGED"
    conflict = ROWS[0].replace("1月5日（星期一）", "2月15日（星期日）")
    assert parse(page(rows=(conflict, *ROWS[1:]))).status == "STRUCTURE_CHANGED"


def test_unknown_text_in_article_container_is_rejected():
    altered = page().replace(
        b'<div class="TRS_Editor">', '附加通知<div class="TRS_Editor">'.encode()
    )
    assert parse(altered).status == "STRUCTURE_CHANGED"


def test_bounded_invalid_input_and_utf8():
    assert parse_shfe_holiday(source_id="", html=page()).status == "INVALID_SOURCE"
    assert parse_shfe_holiday(source_id="x", html="not bytes").status == "INVALID_INPUT"
    assert parse(b"x" * (1024 * 1024 + 1)).status == "INPUT_TOO_LARGE"
    assert parse(b"\xff").status == "INVALID_UTF8"
    assert parse(b"<![broken>").status == "STRUCTURE_CHANGED"
