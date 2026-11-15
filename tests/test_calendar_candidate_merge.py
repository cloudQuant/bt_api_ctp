"""All sources and dates are synthetic; no accounts, network or release."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest

from bt_api_ctp.calendar_candidate_merge import merge_calendar_candidates
from bt_api_ctp.calendar_shfe_candidates import convert_shfe_holiday
from bt_api_ctp.calendar_shfe_holiday import parse_shfe_holiday
from bt_api_ctp.calendar_supplement import SupplementCandidate, parse_supplement

URL = "https://www.shfe.com.cn/services/calenderandholidays/holiday/"


def holiday():
    weekdays = "一二三四五六日"
    names = ("元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节")
    months = (1, 2, 4, 5, 6, 9, 10)

    def text(day, year=False):
        return f"{str(day.year) + '年' if year else ''}{day.month}月{day.day}日（星期{weekdays[day.weekday()]}）"

    rows = []
    for index, (name, month) in enumerate(zip(names, months)):
        start = date(2026, month, 1 if index == 0 else 15)
        end = date(2026, month, 3 if index == 0 else 16)
        opened = date(2026, month, 5 if index == 0 else 17)
        night = date(2025, 12, 31) if index == 0 else date(2026, month, 14)
        rows.append(
            f"<div>{'一二三四五六七'[index]}、{name}：{text(start)}至{text(end)}休市，"
            f"{text(opened)}起照常开市。{text(night, index == 0)}晚上不进行夜盘交易。</div>"
        )
    document = (
        '<div class="article_title" id="no_article_empty">2026年休市安排</div>'
        '<div class="article_content_item"><div class="TRS_Editor">'
        + "".join(rows)
        + "</div></div>"
    ).encode()
    parsed = parse_shfe_holiday(source_id=URL, html=document)
    assert parsed.status == "CANDIDATE"
    converted = convert_shfe_holiday(parsed)
    assert converted.status == "CANDIDATE"
    return converted


def base_content():
    return {
        "sources": [{"id": "manual", "description": "synthetic input"}],
        "trading_days": [
            {"venue": "SHFE", "day": "2026-01-02", "status": "closed", "source_ids": ["manual"]},
            {"venue": "DCE", "day": "2026-01-02", "status": "open", "source_ids": ["manual"]},
        ],
        "trading_hours": [
            {
                "venue": "SHFE",
                "kind": "future",
                "product": "rb",
                "contracts": ["rb2701"],
                "valid_from": "2026-01-01",
                "valid_to": "2026-01-31",
                "day_windows": [],
                "night_windows": [],
                "night_mappings": [],
                "source_ids": ["manual"],
                "windows_complete": False,
                "exceptions_complete": False,
            }
        ],
        "exceptions": [
            {
                "venue": "SHFE",
                "kind": "future",
                "product": "rb",
                "contract": "rb2701",
                "trading_day": "2026-01-02",
                "action": "close_day",
                "window_id": None,
                "source_ids": ["manual"],
            }
        ],
    }


def yaml_candidate(content):
    # JSON is a YAML subset; use the real reader and its schema validation.
    return parse_supplement(json.dumps(content).encode())


def merge(supplement, converted, **kwargs):
    arguments = {"supplement_coverage": "NOT_PROVEN", "supplement_publication": "NOT_AUTHORIZED"}
    arguments.update(kwargs)
    return merge_calendar_candidates(supplement=supplement, holiday=converted, **arguments)


def test_real_candidates_merge_days_refs_and_preserve_unchecked_layers():
    raw = base_content()
    supplement = yaml_candidate(raw)
    converted = holiday()
    before_base, before_holiday = deepcopy(supplement), deepcopy(converted)
    result = merge(supplement, converted)
    assert (result.status, result.verification, result.coverage, result.publication) == (
        "PARTIAL",
        "UNVERIFIED",
        "NOT_PROVEN",
        "NOT_AUTHORIZED",
    )
    assert "TRADING_HOURS_CONFLICTS_NOT_CHECKED" in result.remaining_checks
    assert "EXCEPTIONS_CONFLICTS_NOT_CHECKED" in result.remaining_checks
    assert "CROSS_LAYER_CONFLICTS_NOT_CHECKED" in result.remaining_checks
    content = result.supplement.content
    assert content["trading_hours"] == raw["trading_hours"]
    assert content["exceptions"] == raw["exceptions"]
    days = {(row["venue"], row["day"]): row for row in content["trading_days"]}
    shfe = days[("SHFE", "2026-01-02")]
    source_id = converted.supplement.content["sources"][0]["id"]
    assert shfe["source_ids"] == sorted(["manual", source_id])
    assert days[("DCE", "2026-01-02")]["status"] == "open"
    assert ("SHFE", "2025-12-31") not in days
    assert result.unresolved_nights == converted.unresolved_nights
    assert result.unresolved_nights[0].local_date == date(2025, 12, 31)
    assert result.unresolved_nights[0].trading_day is None
    assert parse_supplement(json.dumps(content).encode()).content == content
    assert supplement == before_base and converted == before_holiday
    content["trading_hours"][0]["contracts"].append("modified")
    assert supplement == before_base and converted == before_holiday


def test_trading_day_conflicts_within_and_across_sources_reject_without_partial_result():
    content = base_content()
    content["trading_days"][0]["status"] = "open"
    result = merge(yaml_candidate(content), holiday())
    assert (result.status, result.reason, result.supplement, result.unresolved_nights) == (
        "CONFLICT",
        "TRADING_DAY_CONFLICT",
        None,
        (),
    )
    content = base_content()
    content["trading_days"].append({**content["trading_days"][0], "status": "open"})
    assert merge(yaml_candidate(content), holiday()).reason == "TRADING_DAY_CONFLICT"


def test_source_id_collisions_only_deduplicate_when_identical():
    converted = holiday()
    source = converted.supplement.content["sources"][0]
    content = base_content()
    content["sources"].append(deepcopy(source))
    result = merge(yaml_candidate(content), converted)
    assert result.status == "PARTIAL"
    assert sum(item["id"] == source["id"] for item in result.supplement.content["sources"]) == 1
    content["sources"][-1]["description"] = "different"
    result = merge(yaml_candidate(content), converted)
    assert (result.status, result.reason, result.supplement, result.unresolved_nights) == (
        "CONFLICT",
        "SOURCE_CONFLICT",
        None,
        (),
    )


def test_unchecked_duplicate_hours_and_exceptions_are_not_removed():
    content = base_content()
    content["trading_hours"].append(deepcopy(content["trading_hours"][0]))
    content["exceptions"].append(deepcopy(content["exceptions"][0]))
    result = merge(yaml_candidate(content), holiday())
    assert result.status == "PARTIAL"
    assert result.supplement.content["trading_hours"] == content["trading_hours"]
    assert result.supplement.content["exceptions"] == content["exceptions"]
    assert "TRADING_HOURS_CONFLICTS_NOT_CHECKED" in result.remaining_checks
    assert "EXCEPTIONS_CONFLICTS_NOT_CHECKED" in result.remaining_checks


@pytest.mark.parametrize(
    "kwargs",
    [
        {"supplement_coverage": "COMPLETE"},
        {"supplement_coverage": None},
        {"supplement_publication": "AUTHORIZED"},
        {"supplement_publication": True},
    ],
)
def test_self_declared_status_must_remain_conservative(kwargs):
    result = merge(yaml_candidate(base_content()), holiday(), **kwargs)
    assert result.status == "INVALID_INPUT" and result.supplement is None


def test_explicit_supplement_declarations_are_required():
    with pytest.raises(TypeError):
        merge_calendar_candidates(supplement=yaml_candidate(base_content()), holiday=holiday())


def test_invalid_or_forged_supplements_and_holiday_are_rejected():
    good = yaml_candidate(base_content())
    converted = holiday()
    forged = SupplementCandidate(good.content, good.unused_source_ids, verification="VERIFIED")
    assert merge(forged, converted).status == "INVALID_INPUT"
    assert merge(replace(good, unused_source_ids=("fake",)), converted).status == "INVALID_INPUT"
    assert merge(good, replace(converted, coverage="COMPLETE")).status == "INVALID_INPUT"
    assert merge(good, replace(converted, publication="AUTHORIZED")).status == "INVALID_INPUT"
    assert merge(good, replace(converted, verification="VERIFIED")).status == "INVALID_INPUT"
    assert merge(good, replace(converted, status="FAILED")).status == "INVALID_INPUT"
    assert merge(good, replace(converted, unresolved_nights=())).status == "INVALID_INPUT"
    bad_night = replace(converted.unresolved_nights[0], trading_day=date(2026, 1, 1))
    assert (
        merge(
            good,
            replace(converted, unresolved_nights=(bad_night, *converted.unresolved_nights[1:])),
        ).status
        == "INVALID_INPUT"
    )
    altered = deepcopy(converted.supplement.content)
    altered["trading_days"][0]["day"] = "2026-01-06"
    forged_candidate = yaml_candidate(altered)
    assert merge(good, replace(converted, supplement=forged_candidate)).status == "INVALID_INPUT"
