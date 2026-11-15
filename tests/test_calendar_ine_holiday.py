from datetime import date
from hashlib import sha256

import pytest

from bt_api_ctp.calendar_ine_holiday import MAX_BYTES, SOURCE_URL, parse_ine_holiday

ROWS = [
    "一、元旦：1月1日（星期四）至1月3日（星期六）休市，1月5日（星期一）起照常开市。1月4日（星期日）为周末休市。2025年12月31日（星期三）晚上不进行夜盘交易。",
    "二、春节：2月15日（星期日）至2月23日（星期一）休市，2月24日（星期二）起照常开市。2月14日（星期六）、2月28日（星期六）为周末休市。2月13日（星期五）晚上不进行夜盘交易。",
    "三、清明节：4月4日（星期六）至4月6日（星期一）休市，4月7日（星期二）起照常开市。4月3日（星期五）晚上不进行夜盘交易。",
    "四、劳动节：5月1日（星期五）至5月5日（星期二）休市，5月6日（星期三）起照常开市。5月9日（星期六）为周末休市。4月30日（星期四）晚上不进行夜盘交易。",
    "五、端午节：6月19日（星期五）至6月21日（星期日）休市，6月22日（星期一）起照常开市。6月18日（星期四）晚上不进行夜盘交易。",
    "六、中秋节：9月25日（星期五）至9月27日（星期日）休市，9月28日（星期一）起照常开市。9月24日（星期四）晚上不进行夜盘交易。",
    "七、国庆节：10月1日（星期四）至10月7日（星期三）休市，10月8日（星期四）起照常开市。9月20日（星期日）、10月10日（星期六）为周末休市。9月30日（星期三）晚上不进行夜盘交易。",
]


def page(desktop=ROWS, mobile=None, title="2026年休市安排", mobile_title=None):
    if mobile is None:
        mobile = desktop
    if mobile_title is None:
        mobile_title = title

    def editor(rows):
        return '<div class="TRS_Editor">' + "".join(f"<div>{r}</div>" for r in rows) + "</div>"

    return (
        '<div class="detail_content"><div id="no_article_empty">'
        f'<span class="table_select_title">{title}</span></div>'
        f'<div class="table_content">{editor(desktop)}</div></div>'
        f'<div class="m_content_tt">{mobile_title}</div>'
        f'<div class="m_content_test"><p>{editor(mobile)}</p></div>'
        '<script>let article_info = ["2025年休市安排", "2024年休市安排"];</script>'
    ).encode()


def parse(data):
    return parse_ine_holiday(source_id=SOURCE_URL, html=data)


def test_complete_article_and_previous_year_explicit_night():
    data = page()
    result = parse(data)
    assert (result.status, result.year, len(result.rows)) == ("CANDIDATE", 2026, 7)
    assert result.sha256 == sha256(data).hexdigest()
    assert result.rows[0].no_night == date(2025, 12, 31)
    assert result.rows[0].resume_date == date(2026, 1, 5)
    assert result.rows[1].weekend_closures == (date(2026, 2, 14), date(2026, 2, 28))
    assert (result.verification, result.coverage, result.publication) == (
        "UNVERIFIED",
        "NOT_PROVEN",
        "NOT_AUTHORIZED",
    )


def test_bom_is_hashed_in_raw_bytes():
    data = b"\xef\xbb\xbf" + page()
    result = parse(data)
    assert result.status == "CANDIDATE"
    assert result.sha256 == sha256(data).hexdigest()


def test_another_explicit_annual_article_uses_its_own_year():
    def token(value, explicit=False):
        year = f"{value.year}年" if explicit else ""
        return f"{year}{value.month}月{value.day}日（星期{'一二三四五六日'[value.weekday()]}）"

    names = ("元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节")
    months = (1, 2, 4, 5, 6, 9, 10)
    rows = []
    for ordinal, name, month in zip("一二三四五六七", names, months):
        start = date(2027, month, 15)
        end = date(2027, month, 16)
        resume = date(2027, month, 17)
        night = date(2027, month, 14)
        rows.append(
            f"{ordinal}、{name}：{token(start)}至{token(end)}休市，"
            f"{token(resume)}起照常开市。{token(night)}晚上不进行夜盘交易。"
        )
    result = parse(page(desktop=rows, title="2027年休市安排"))
    assert (result.status, result.year, len(result.rows)) == ("CANDIDATE", 2027, 7)
    assert result.rows[0].closure_start == date(2027, 1, 15)


def test_cross_year_closure_is_not_guessed():
    first = (
        "一、元旦：2026年12月31日（星期四）至2027年1月2日（星期六）休市，"
        "2027年1月4日（星期一）起照常开市。"
        "2026年12月30日（星期三）晚上不进行夜盘交易。"
    )
    result = parse(page(desktop=[first, *ROWS[1:]], title="2027年休市安排"))
    assert result.status == "NEEDS_REVIEW"
    assert result.rows == ()


@pytest.mark.parametrize(
    "data,reason",
    [
        (page(mobile_title="2025年休市安排"), "TITLE_CONFLICT"),
        (page(mobile=ROWS[:-1]), "ARTICLE_CONFLICT"),
        (page(desktop=ROWS[:-1]), "MISSING_OR_UNEXPECTED_SECTION"),
        (page(desktop=ROWS + [ROWS[-1]]), "MISSING_OR_UNEXPECTED_SECTION"),
        (page(title="2027年休市安排"), "UNRECOGNIZED_OR_AMBIGUOUS"),
        (
            page(desktop=[ROWS[0].replace("2025年", "2024年")] + ROWS[1:]),
            "UNRECOGNIZED_OR_AMBIGUOUS",
        ),
        (page(desktop=[ROWS[0].replace("2025年", "")] + ROWS[1:]), "UNRECOGNIZED_OR_AMBIGUOUS"),
        (page(desktop=[ROWS[0] + "另行通知。"] + ROWS[1:]), "UNRECOGNIZED_OR_AMBIGUOUS"),
        (
            page().replace(
                b'</div></div><div class="m_content_tt">',
                b'unexpected</div></div><div class="m_content_tt">',
            ),
            "UNRECOGNIZED_OR_AMBIGUOUS",
        ),
    ],
)
def test_ambiguous_input_never_returns_rows(data, reason):
    result = parse(data)
    assert result.status == "NEEDS_REVIEW"
    assert result.reason == reason
    assert result.rows == ()


def test_competing_extra_editor_fails_closed():
    data = page().replace(b"<script>", b'<div class="TRS_Editor"><div>x</div></div><script>')
    assert parse(data).status == "NEEDS_REVIEW"


def test_invalid_source_and_input():
    assert (
        parse_ine_holiday(source_id="https://example.com/", html=page()).status == "INVALID_SOURCE"
    )
    assert parse_ine_holiday(source_id=SOURCE_URL, html="x").status == "INVALID_INPUT"
    assert parse(b"\xff").status == "INVALID_UTF8"
    assert parse(b"x" * (MAX_BYTES + 1)).status == "INPUT_TOO_LARGE"
    assert parse(b"<html>human verification</html>").status == "NEEDS_REVIEW"
