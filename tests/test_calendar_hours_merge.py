"""Synthetic offline fixtures: no exchange truth, account, network or release."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest

from bt_api_ctp.calendar_candidate_merge import merge_calendar_candidates
from bt_api_ctp.calendar_hours_merge import merge_hours_candidates
from bt_api_ctp.calendar_shfe_candidates import convert_shfe_holiday
from bt_api_ctp.calendar_shfe_holiday import parse_shfe_holiday
from bt_api_ctp.calendar_shfe_hours_candidates import HoursSelection, convert_shfe_hours
from bt_api_ctp.calendar_shfe_page import parse_shfe_page
from bt_api_ctp.calendar_supplement import _validate, parse_supplement

HOLIDAY_URL = "https://www.shfe.com.cn/services/calenderandholidays/holiday/"
HOURS_URL = "https://www.shfe.com.cn/services/calenderandholidays/tradinghours/"
DAY = (
    "日盘<br>集合竞价申报 08:55-08:59<br>集合竞价撮合 08:59-09:00<br>"
    "连续竞价交易 09:00-10:15<br>连续竞价交易 10:30-11:30<br>连续竞价交易 13:30-15:00"
)
NIGHT = (
    "夜盘<br>集合竞价申报 20:55-20:59<br>集合竞价撮合 20:59-21:00<br>连续竞价交易 21:00-01:00<br>"
)


@pytest.fixture
def inputs():
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
    holiday_html = (
        '<div class="article_title" id="no_article_empty">2026年休市安排</div>'
        '<div class="article_content_item"><div class="TRS_Editor">'
        + "".join(rows)
        + "</div></div>"
    ).encode()
    parsed_holiday = parse_shfe_holiday(source_id=HOLIDAY_URL, html=holiday_html)
    holiday = convert_shfe_holiday(parsed_holiday)
    assert holiday.status == "CANDIDATE"
    raw = {
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
                "valid_to": "2027-12-31",
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
    base = parse_supplement(json.dumps(raw, ensure_ascii=False).encode())
    partial = merge_calendar_candidates(
        supplement=base,
        holiday=holiday,
        supplement_coverage="NOT_PROVEN",
        supplement_publication="NOT_AUTHORIZED",
    )
    assert partial.status == "PARTIAL"
    html = (
        "<table>"
        + f"<tr><td>期货：合成铜<br>期权：合成铜期权</td><td>{NIGHT}{DAY}</td></tr>"
        + f"<tr><td>期货：合成能源</td><td>{DAY}</td></tr>"
        + '<tr><td colspan="2">注：仅合成测试</td></tr></table>'
    ).encode()
    page = parse_shfe_page(source_id=HOURS_URL, html=html)
    selected = (
        HoursSelection(
            "SHFE", "future", "合成铜", "cu", ("cu2701",), date(2026, 1, 1), date(2027, 12, 31)
        ),
        HoursSelection(
            "SHFE",
            "option",
            "合成铜期权",
            "cu_option",
            ("cu2701-C-100",),
            date(2026, 1, 1),
            date(2027, 12, 31),
        ),
        HoursSelection(
            "INE", "future", "合成能源", "sc", ("sc2701",), date(2026, 1, 1), date(2027, 12, 31)
        ),
    )
    hours = convert_shfe_hours(page, selections=selected)
    assert page.status == "NEEDS_REVIEW" and hours.status == "CANDIDATE"
    return partial, hours


def merge(inputs):
    return merge_hours_candidates(partial=inputs[0], hours=inputs[1])


def assert_empty(result, status):
    assert result.status == status and result.supplement is None
    assert result.holiday_unresolved_nights == result.hours_unresolved_nights == ()
    assert result.page_notes == result.page_review_reasons == ()
    assert (result.verification, result.coverage, result.publication) == (
        "UNVERIFIED",
        "NOT_PROVEN",
        "NOT_AUTHORIZED",
    )


def with_base_content(partial, content):
    return replace(partial, supplement=_validate(content))


def with_hours_content(hours, content):
    return replace(hours, supplement=_validate(content))


def test_real_parsers_and_converters_preserve_day_candidates_and_unresolved_nights(inputs):
    partial, hours = inputs
    before = deepcopy(inputs)
    result = merge(inputs)
    assert (result.status, result.verification, result.coverage, result.publication) == (
        "PARTIAL",
        "UNVERIFIED",
        "NOT_PROVEN",
        "NOT_AUTHORIZED",
    )
    assert (
        result.source_authentication,
        result.cross_layer_conflicts,
        result.other_session_conflicts,
    ) == ("NOT_CHECKED", "NOT_CHECKED", "NOT_CHECKED")
    assert {
        "TRADING_HOURS_CONFLICTS_NOT_CHECKED",
        "EXCEPTIONS_CONFLICTS_NOT_CHECKED",
        "CROSS_LAYER_CONFLICTS_NOT_CHECKED",
        "NIGHT_TRADING_DAY_UNRESOLVED",
    } <= set(result.remaining_checks)
    output = result.supplement.content
    assert output["trading_days"] == partial.supplement.content["trading_days"]
    assert output["exceptions"] == partial.supplement.content["exceptions"]
    assert (
        output["trading_hours"]
        == partial.supplement.content["trading_hours"] + hours.supplement.content["trading_hours"]
    )
    assert {s["id"] for s in output["sources"]} == {
        s["id"] for s in partial.supplement.content["sources"] + hours.supplement.content["sources"]
    }
    assert (
        result.holiday_unresolved_nights == partial.unresolved_nights
        and len(result.holiday_unresolved_nights) == 7
    )
    assert (
        result.hours_unresolved_nights == hours.unresolved_nights
        and len(result.hours_unresolved_nights) == 3
    )
    assert (
        result.page_notes == hours.page_notes
        and result.page_review_reasons == hours.page_review_reasons
    )
    for row in hours.supplement.content["trading_hours"]:
        assert row["windows_complete"] is row["exceptions_complete"] is False
        assert not row["night_windows"] and not row["night_mappings"]
        assert any(
            window["start"] == "09:00" and window["end"] == "10:15" for window in row["day_windows"]
        )
    assert any(
        stage.day_transition == "INFERRED_NEXT_DAY"
        for review in result.hours_unresolved_nights
        for stage in review.stages
    )
    assert all(
        (review.availability, review.trading_day_mapping) == ("UNKNOWN", "UNKNOWN")
        for review in result.hours_unresolved_nights
    )
    assert inputs == before
    output["trading_days"].clear()
    assert inputs == before


def test_sources_are_preserved_and_conflicting_id_refused(inputs):
    partial, hours = inputs
    source = deepcopy(hours.supplement.content["sources"][0])
    base = deepcopy(partial.supplement.content)
    base["sources"].append(source)
    same = merge_hours_candidates(partial=with_base_content(partial, base), hours=hours)
    assert same.status == "PARTIAL"
    assert sum(item["id"] == source["id"] for item in same.supplement.content["sources"]) == 1
    source["description"] = "different synthetic description"
    failed = merge_hours_candidates(partial=with_base_content(partial, base), hours=hours)
    assert_empty(failed, "CONFLICT")
    assert failed.reason == "SOURCE_CONFLICT"


@pytest.mark.parametrize(
    "difference,reason", [("overlap", "TEMPLATE_PERIOD_CONFLICT"), ("owner", "SCOPE_CONFLICT")]
)
def test_template_overlaps_and_contract_owner_conflict_are_not_merged(inputs, difference, reason):
    partial, hours = inputs
    base = deepcopy(partial.supplement.content)
    row = deepcopy(
        next(
            row
            for row in hours.supplement.content["trading_hours"]
            if row["venue"] == "SHFE" and row["kind"] == "future"
        )
    )
    row["source_ids"] = ["manual"]
    if difference == "owner":
        row["product"] = "different_product"
    base["trading_hours"].append(row)
    result = merge_hours_candidates(partial=with_base_content(partial, base), hours=hours)
    assert_empty(result, "CONFLICT")
    assert result.reason == reason


def test_nonoverlapping_periods_and_same_contract_on_other_venue_are_not_silently_combined(inputs):
    partial, hours = inputs
    base = deepcopy(partial.supplement.content)
    row = deepcopy(
        next(
            row
            for row in hours.supplement.content["trading_hours"]
            if row["venue"] == "SHFE" and row["kind"] == "future"
        )
    )
    row.update(valid_from="2025-01-01", valid_to="2025-12-31", source_ids=["manual"])
    base["trading_hours"].append(row)
    other = deepcopy(row)
    other.update(venue="DCE", product="other", valid_from="2026-01-01", valid_to="2027-12-31")
    base["trading_hours"].append(other)
    result = merge_hours_candidates(partial=with_base_content(partial, base), hours=hours)
    assert (
        result.status == "PARTIAL"
        and len(result.supplement.content["trading_hours"]) == len(base["trading_hours"]) + 3
    )


@pytest.mark.parametrize(
    "which,change",
    [
        ("partial", {"status": "CONFLICT"}),
        ("partial", {"verification": "VERIFIED"}),
        ("partial", {"coverage": "COMPLETE"}),
        ("partial", {"publication": "AUTHORIZED"}),
        ("partial", {"supplement": None}),
        ("partial", {"remaining_checks": ()}),
        ("hours", {"status": "INVALID_INPUT"}),
        ("hours", {"verification": "VERIFIED"}),
        ("hours", {"coverage": "COMPLETE"}),
        ("hours", {"publication": "AUTHORIZED"}),
        ("hours", {"supplement": None}),
        ("hours", {"unresolved_nights": ()}),
        ("hours", {"page_notes": ("x" * 8193,)}),
    ],
)
def test_missing_or_forged_metadata_returns_no_partial_data(inputs, which, change):
    partial, hours = inputs
    if which == "partial":
        partial = replace(partial, **change)
    else:
        hours = replace(hours, **change)
    result = merge_hours_candidates(partial=partial, hours=hours)
    assert_empty(result, "INVALID_INPUT")
    assert result.reason == "INVALID_OR_UNSUPPORTED_INPUT"


@pytest.mark.parametrize(
    "change",
    [
        "days",
        "exceptions",
        "night_window",
        "night_mapping",
        "complete",
        "exception_complete",
        "bad_source",
        "foreign_venue",
        "overlapping_windows",
    ],
)
def test_hours_candidate_unexpected_layers_or_flags_fail_closed(inputs, change):
    partial, hours = inputs
    content = deepcopy(hours.supplement.content)
    row = content["trading_hours"][0]
    if change == "days":
        content["trading_days"].append(
            {
                "venue": "SHFE",
                "day": "2026-01-01",
                "status": "closed",
                "source_ids": [content["sources"][0]["id"]],
            }
        )
    elif change == "exceptions":
        content["exceptions"].append(
            {
                "venue": "SHFE",
                "kind": "future",
                "product": "cu",
                "contract": "cu2701",
                "trading_day": "2026-01-01",
                "action": "close_day",
                "window_id": None,
                "source_ids": [content["sources"][0]["id"]],
            }
        )
    elif change == "night_window":
        row["night_windows"] = [
            {
                "id": "night",
                "phase": "continuous",
                "start": "21:00",
                "end": "23:00",
                "end_day_offset": 0,
            }
        ]
    elif change == "night_mapping":
        row["night_mappings"] = [
            {
                "trading_day": "2026-01-02",
                "start_date": None,
                "source_ids": [content["sources"][0]["id"]],
            }
        ]
    elif change == "complete":
        row["windows_complete"] = True
    elif change == "exception_complete":
        row["exceptions_complete"] = True
    elif change == "bad_source":
        content["sources"][0]["description"] = "arbitrary fabricated source"
    elif change == "foreign_venue":
        row["venue"] = "DCE"
    else:
        row["day_windows"][0]["end"] = "09:30"
    result = merge_hours_candidates(partial=partial, hours=with_hours_content(hours, content))
    assert_empty(result, "INVALID_INPUT")


def test_unresolved_night_identity_and_source_must_remain_uncertain(inputs):
    partial, hours = inputs
    first = hours.unresolved_nights[0]
    for alteration in (
        {"availability": "KNOWN"},
        {"trading_day_mapping": "KNOWN"},
        {"product": "other"},
        {"contracts": ("unknown",)},
    ):
        forged = replace(
            hours, unresolved_nights=(replace(first, **alteration), *hours.unresolved_nights[1:])
        )
        assert_empty(merge_hours_candidates(partial=partial, hours=forged), "INVALID_INPUT")
    night = partial.unresolved_nights[0]
    forged_partial = replace(
        partial,
        unresolved_nights=(replace(night, source_id="unknown"), *partial.unresolved_nights[1:]),
    )
    assert_empty(merge_hours_candidates(partial=forged_partial, hours=hours), "INVALID_INPUT")


def test_unrelated_template_conflict_in_existing_partial_is_not_hidden(inputs):
    partial, hours = inputs
    content = deepcopy(partial.supplement.content)
    duplicate = deepcopy(content["trading_hours"][0])
    duplicate["valid_from"] = "2026-02-01"
    content["trading_hours"].append(duplicate)
    result = merge_hours_candidates(partial=with_base_content(partial, content), hours=hours)
    assert_empty(result, "CONFLICT")
    assert result.reason == "TEMPLATE_PERIOD_CONFLICT"


def test_duplicate_date_and_excess_contract_work_are_rejected(inputs):
    partial, hours = inputs
    content = deepcopy(partial.supplement.content)
    content["trading_days"].append(deepcopy(content["trading_days"][0]))
    assert_empty(
        merge_hours_candidates(partial=with_base_content(partial, content), hours=hours),
        "INVALID_INPUT",
    )
    content = deepcopy(partial.supplement.content)
    template = content["trading_hours"][0]
    content["trading_hours"] = []
    for group in range(5):
        row = deepcopy(template)
        row["contracts"] = [f"contract{group}_{index}" for index in range(1000)]
        content["trading_hours"].append(row)
    assert_empty(
        merge_hours_candidates(partial=with_base_content(partial, content), hours=hours),
        "INVALID_INPUT",
    )


def test_unresolved_night_text_budget_is_bounded(inputs):
    partial, hours = inputs
    first = next(review for review in hours.unresolved_nights if review.stages)
    large = replace(first.stages[0], raw_line="x" * 8192)
    oversized = replace(first, stages=(large,) * 129)
    altered = replace(
        hours,
        unresolved_nights=tuple(
            oversized if item is first else item for item in hours.unresolved_nights
        ),
    )
    assert_empty(merge_hours_candidates(partial=partial, hours=altered), "INVALID_INPUT")
