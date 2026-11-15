from dataclasses import replace
from datetime import date
from hashlib import sha256

import pytest
from test_calendar_ine_holiday import ROWS, page

from bt_api_ctp.calendar_ine_holiday import SOURCE_URL, parse_ine_holiday
from bt_api_ctp.calendar_ine_holiday_candidates import convert_ine_holiday
from bt_api_ctp.calendar_supplement import _validate


def convert(raw):
    candidate = parse_ine_holiday(source_id=SOURCE_URL, html=raw)
    return convert_ine_holiday(candidate, html=raw)


def test_explicit_closures_and_unresolved_nights_only():
    raw = page()
    result = convert(raw)
    assert result.status == "CANDIDATE"
    assert (result.verification, result.coverage, result.publication) == (
        "UNVERIFIED",
        "NOT_PROVEN",
        "NOT_AUTHORIZED",
    )
    source_id = f"ine.holiday.2026.{sha256(raw).hexdigest()}"
    content = result.supplement.content
    assert _validate(content) == result.supplement
    days = [row["day"] for row in content["trading_days"]]
    assert len(days) == 39
    assert days == sorted(set(days))
    assert "2026-02-14" in days and "2026-02-28" in days
    assert "2026-01-05" not in days
    assert all(
        row["venue"] == "INE" and row["status"] == "closed" for row in content["trading_days"]
    )
    assert all(row["source_ids"] == [source_id] for row in content["trading_days"])
    assert content["trading_hours"] == content["exceptions"] == []
    assert len(result.unresolved_nights) == 7
    assert result.unresolved_nights[0].local_date == date(2025, 12, 31)
    assert all(
        item.trading_day is None and item.source_id == source_id
        for item in result.unresolved_nights
    )
    assert convert(raw) == result


def test_different_year_is_derived_from_the_supplied_article():
    def token(value):
        return f"{value.month}月{value.day}日（星期{'一二三四五六日'[value.weekday()]}）"

    rows = []
    for ordinal, name, month in zip(
        "一二三四五六七",
        ("元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节"),
        (1, 2, 4, 5, 6, 9, 10),
    ):
        rows.append(
            f"{ordinal}、{name}：{token(date(2027, month, 15))}至{token(date(2027, month, 16))}休市，"
            f"{token(date(2027, month, 17))}起照常开市。{token(date(2027, month, 14))}晚上不进行夜盘交易。"
        )
    raw = page(desktop=rows, title="2027年休市安排")
    result = convert(raw)
    assert result.status == "CANDIDATE"
    assert len(result.supplement.content["trading_days"]) == 14
    assert all(row["day"].startswith("2027-") for row in result.supplement.content["trading_days"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", "0" * 64),
        ("year", 2027),
        ("source_id", "https://example.com/"),
        ("coverage", "VERIFIED"),
        ("publication", "AUTHORIZED"),
        ("reason", "unexpected"),
    ],
)
def test_forged_candidate_is_rejected(field, value):
    raw = page()
    original = parse_ine_holiday(source_id=SOURCE_URL, html=raw)
    assert convert_ine_holiday(replace(original, **{field: value}), html=raw).status == "UNKNOWN"


def test_forged_row_or_changed_bytes_are_rejected():
    raw = page()
    original = parse_ine_holiday(source_id=SOURCE_URL, html=raw)
    forged = replace(original.rows[0], no_night=date(2026, 1, 1))
    assert (
        convert_ine_holiday(replace(original, rows=(forged, *original.rows[1:])), html=raw).status
        == "UNKNOWN"
    )
    assert convert_ine_holiday(original, html=raw + b" ").status == "UNKNOWN"


def test_invalid_documents_and_cross_year_ambiguity_are_unknown():
    raw = page()
    candidate = parse_ine_holiday(source_id=SOURCE_URL, html=raw)
    assert convert_ine_holiday(candidate, html="not bytes").status == "UNKNOWN"
    assert convert_ine_holiday(None, html=raw).status == "UNKNOWN"
    ambiguous = page(desktop=[ROWS[0].replace("2025年", "")] + ROWS[1:])
    assert convert(ambiguous).status == "UNKNOWN"
    overlapping = page(desktop=[ROWS[0], ROWS[0], *ROWS[2:]])
    assert convert(overlapping).status == "UNKNOWN"


def test_bom_bytes_are_preserved_in_source_digest():
    raw = b"\xef\xbb\xbf" + page()
    result = convert(raw)
    assert result.status == "CANDIDATE"
    assert result.supplement.content["sources"][0]["id"].endswith(sha256(raw).hexdigest())
