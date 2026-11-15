"""Synthetic examples only; this module does not authenticate exchange facts."""

import json
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from bt_api_ctp.calendar_shfe_candidates import convert_shfe_holiday
from bt_api_ctp.calendar_shfe_holiday import HolidayCandidate, parse_shfe_holiday
from bt_api_ctp.calendar_supplement import parse_supplement

URL = "https://www.shfe.com.cn/services/calenderandholidays/holiday/"
ROWS = (
    "一、元旦：1月1日（星期四）至1月3日（星期六）休市，1月5日（星期一）起照常开市。"
    "1月3日（星期六）、1月4日（星期日）为周末休市。2025年12月31日（星期三）晚上不进行夜盘交易。",
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


@pytest.fixture
def candidate():
    document = (
        '<div class="article_title" id="no_article_empty">2026年休市安排</div>'
        '<div class="article_content_item"><div class="TRS_Editor">'
        + "".join(f"<div>{row}</div>" for row in ROWS)
        + "</div></div>"
    ).encode("utf-8")
    result = parse_shfe_holiday(source_id=URL, html=document)
    assert result.status == "CANDIDATE"
    return result


def test_explicit_closures_validate_as_four_layer_supplement(candidate):
    result = convert_shfe_holiday(candidate)
    assert (result.status, result.verification, result.coverage, result.publication) == (
        "CANDIDATE",
        "UNVERIFIED",
        "NOT_PROVEN",
        "NOT_AUTHORIZED",
    )
    supplement = result.supplement
    assert supplement.verification == "UNVERIFIED"
    content = supplement.content
    assert set(content) == {"sources", "trading_days", "trading_hours", "exceptions"}
    assert content["trading_hours"] == content["exceptions"] == []
    source = content["sources"][0]
    assert source["id"] == f"shfe.holiday.2026.{candidate.sha256}"
    assert URL in source["description"] and candidate.sha256 in source["description"]
    days = content["trading_days"]
    assert all(row["venue"] == "SHFE" and row["status"] == "closed" for row in days)
    assert all(row["source_ids"] == [source["id"]] for row in days)
    dates = [row["day"] for row in days]
    assert dates == sorted(set(dates))
    assert dates.count("2026-01-03") == 1  # range and separate weekend
    assert "2026-01-04" in dates and "2026-02-14" in dates
    assert "2026-01-05" not in dates and "2025-12-31" not in dates
    assert parse_supplement(json.dumps(content, ensure_ascii=False).encode()).content == content
    first = result.unresolved_nights[0]
    assert first.local_date == date(2025, 12, 31) and first.trading_day is None
    assert first.raw_text == ROWS[0] and first.source_id == source["id"]
    assert len(result.unresolved_nights) == 7


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "NEEDS_REVIEW"},
        {"status": "STRUCTURE_CHANGED"},
        {"verification": "VERIFIED"},
        {"coverage": "COMPLETE"},
        {"source_id": "https://example.invalid/"},
        {"source_id": URL + "?other=1"},
        {"sha256": None},
        {"sha256": "g" * 64},
        {"year": True},
        {"year": 2027},
        {"raw_title": "2027年休市安排"},
        {"reasons": ("unresolved",)},
        {"rows": ()},
    ],
)
def test_ineligible_candidate_is_not_partially_converted(candidate, changes):
    result = convert_shfe_holiday(replace(candidate, **changes))
    assert (result.status, result.supplement, result.unresolved_nights) == (
        "INVALID_CANDIDATE",
        None,
        (),
    )
    assert result.publication == "NOT_AUTHORIZED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("closure_start", date(2026, 1, 2)),
        ("closure_end", date(2026, 2, 28)),
        ("closure_end", date.max),
        ("no_night", date(2026, 1, 1)),
        ("closure_start", datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ("weekend_closures", (date(2026, 1, 5),)),
        ("raw_text", "unknown source text"),
    ],
)
def test_structural_mutation_or_large_span_fails_closed(candidate, field, value):
    row = replace(candidate.rows[0], **{field: value})
    result = convert_shfe_holiday(replace(candidate, rows=(row, *candidate.rows[1:])))
    assert result.status == "INVALID_CANDIDATE" and result.supplement is None


def test_invalid_types_and_conflicting_sections_are_rejected(candidate):
    assert convert_shfe_holiday(None).status == "INVALID_CANDIDATE"
    assert convert_shfe_holiday(HolidayCandidate("CANDIDATE")).status == "INVALID_CANDIDATE"
    conflicted = replace(
        candidate, rows=(candidate.rows[0], candidate.rows[0], *candidate.rows[2:])
    )
    assert convert_shfe_holiday(conflicted).status == "INVALID_CANDIDATE"


def test_parser_consistency_is_not_authentication(candidate):
    # Digest shape is checked, but this converter does not receive original HTML
    # and therefore cannot establish that caller-provided digest or source is true.
    forged = replace(candidate, sha256="0" * 64)
    result = convert_shfe_holiday(forged)
    assert result.status == "CANDIDATE" and result.verification == "UNVERIFIED"
    assert result.coverage == "NOT_PROVEN" and result.publication == "NOT_AUTHORIZED"
