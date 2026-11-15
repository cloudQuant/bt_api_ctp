"""Synthetic page and caller mappings; none are official trading evidence."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime

import pytest

from bt_api_ctp.calendar_shfe_hours_candidates import HoursSelection, convert_shfe_hours
from bt_api_ctp.calendar_shfe_page import parse_shfe_page
from bt_api_ctp.calendar_supplement import parse_supplement

URL = "https://www.shfe.com.cn/services/calenderandholidays/tradinghours/"
DAY = (
    "日盘<br>集合竞价申报 08:55-08:59<br>集合竞价撮合 08:59-09:00<br>"
    "连续竞价交易 09:00-10:15<br>连续竞价交易 10:30-11:30<br>连续竞价交易 13:30-15:00"
)
NIGHT = (
    "夜盘<br>集合竞价申报 20:55-20:59<br>集合竞价撮合 20:59-21:00<br>连续竞价交易 21:00-01:00<br>"
)


def page():
    html = (
        "<table>"
        f"<tr><td>期货：合成铜、合成铝<br>期权：合成铜期权</td><td>{NIGHT}{DAY}</td></tr>"
        f"<tr><td>期货：合成能源</td><td>{DAY}</td></tr>"
        '<tr><td colspan="2">注：合成说明，待人工核对</td></tr>'
        "</table>"
    ).encode()
    result = parse_shfe_page(source_id=URL, html=html)
    assert result.status == "NEEDS_REVIEW"
    return result


def selections():
    return (
        HoursSelection(
            "SHFE",
            "future",
            "合成铜",
            "cu",
            ("cu2705", "cu2701"),
            date(2026, 1, 1),
            date(2027, 12, 31),
        ),
        HoursSelection(
            "SHFE", "future", "合成铝", "al", ("al2701",), date(2026, 1, 1), date(2027, 12, 31)
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


def test_stage_fields_reject_string_subclasses():
    class Deceptive(str):
        def __eq__(self, other):
            return True

        __hash__ = str.__hash__

    original = page()
    product = original.products[0]
    forged = replace(product.stages[0], phase=Deceptive(product.stages[0].phase))
    altered = replace(product, stages=(forged,) + product.stages[1:])
    candidate = replace(original, products=(altered,) + original.products[1:])
    assert convert_shfe_hours(candidate, selections=selections()).status == "INVALID_INPUT"


def test_explicit_multi_venue_future_option_day_phases_and_unresolved_nights():
    source = page()
    chosen = selections()
    before = deepcopy(source), deepcopy(chosen)
    result = convert_shfe_hours(source, selections=chosen)
    assert (result.status, result.verification, result.coverage, result.publication) == (
        "CANDIDATE",
        "UNVERIFIED",
        "NOT_PROVEN",
        "NOT_AUTHORIZED",
    )
    supplement = result.supplement
    assert supplement.verification == "UNVERIFIED"
    content = supplement.content
    assert content["trading_days"] == content["exceptions"] == []
    assert len(content["trading_hours"]) == 4
    assert content["sources"][0]["id"] == "shfe.hours." + source.sha256
    assert (
        URL in content["sources"][0]["description"]
        and source.sha256 in content["sources"][0]["description"]
    )
    assert parse_supplement(json.dumps(content, ensure_ascii=False).encode()).content == content
    for row in content["trading_hours"]:
        assert row["night_windows"] == row["night_mappings"] == []
        assert row["windows_complete"] is False and row["exceptions_complete"] is False
        assert [(item["id"], item["phase"]) for item in row["day_windows"]] == [
            ("day.auction_order", "auction"),
            ("day.auction_match", "auction"),
            ("day.continuous.0900.1015", "continuous"),
            ("day.continuous.1030.1130", "continuous"),
            ("day.continuous.1330.1500", "continuous"),
        ]
    reviews = {item.page_product_name: item for item in result.unresolved_nights}
    assert len(reviews["合成铜"].stages) == 3
    assert reviews["合成铜"].stages[-1].day_transition == "INFERRED_NEXT_DAY"
    assert reviews["合成能源"].stages == ()
    assert all(
        (item.availability, item.trading_day_mapping) == ("UNKNOWN", "UNKNOWN")
        for item in reviews.values()
    )
    assert (
        result.page_notes == source.raw_notes
        and result.page_review_reasons == source.review_reasons
    )
    assert (source, chosen) == before


def test_deterministic_with_explicit_mapping_and_page_product_order_changes():
    source = page()
    first = convert_shfe_hours(source, selections=selections())
    reordered = replace(source, products=tuple(reversed(source.products)))
    second = convert_shfe_hours(reordered, selections=tuple(reversed(selections())))
    assert first == second
    changed_contract_order = replace(
        selections()[0], contracts=tuple(reversed(selections()[0].contracts))
    )
    third = convert_shfe_hours(source, selections=(changed_contract_order, *selections()[1:]))
    assert third == first


def test_day_only_page_does_not_assert_no_night():
    html = f"<table><tr><td>期货：合成能源</td><td>{DAY}</td></tr></table>".encode()
    source = parse_shfe_page(source_id=URL, html=html)
    result = convert_shfe_hours(source, selections=(selections()[-1],))
    assert source.status == "CANDIDATE" and result.status == "CANDIDATE"
    assert result.unresolved_nights[0].stages == ()
    assert result.unresolved_nights[0].availability == "UNKNOWN"
    assert result.supplement.content["trading_hours"][0]["windows_complete"] is False


def test_explicit_next_day_night_stays_review_only():
    night = NIGHT.replace("21:00-01:00", "21:00-次日01:00")
    raw = f"<table><tr><td>期货：合成铜</td><td>{night}{DAY}</td></tr></table>".encode()
    source = parse_shfe_page(source_id=URL, html=raw)
    result = convert_shfe_hours(source, selections=(selections()[0],))
    assert source.status == "CANDIDATE" and result.status == "CANDIDATE"
    assert result.unresolved_nights[0].stages[-1].day_transition == "EXPLICIT_NEXT_DAY"
    assert result.unresolved_nights[0].trading_day_mapping == "UNKNOWN"
    assert result.supplement.content["trading_hours"][0]["night_mappings"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "CONFLICT"},
        {"status": "STRUCTURE_CHANGED"},
        {"verification": "VERIFIED"},
        {"source_id": "https://other.invalid/"},
        {"sha256": "z" * 64},
        {"raw_notes": ("injected",)},
        {"review_reasons": ()},
        {"products": ()},
    ],
)
def test_invalid_or_inconsistent_page_has_no_partial_output(changes):
    result = convert_shfe_hours(replace(page(), **changes), selections=selections())
    assert (result.status, result.supplement, result.unresolved_nights) == (
        "INVALID_INPUT",
        None,
        (),
    )
    assert result.publication == "NOT_AUTHORIZED"


def test_forged_stage_and_duplicate_page_identity_are_rejected():
    source = page()
    original = source.products[0]
    altered_stage = replace(original.stages[-1], end="15:30")
    forged = replace(original, stages=(*original.stages[:-1], altered_stage))
    assert (
        convert_shfe_hours(
            replace(source, products=(forged, *source.products[1:])), selections=selections()
        ).status
        == "INVALID_INPUT"
    )
    assert (
        convert_shfe_hours(
            replace(source, products=(*source.products, source.products[0])),
            selections=selections(),
        ).status
        == "INVALID_INPUT"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"venue": "DCE"},
        {"kind": "OPTION"},
        {"page_product_name": "未知产品"},
        {"kind": "option"},
        {"product": ""},
        {"contracts": ()},
        {"contracts": ("cu2701", "cu2701")},
        {"contracts": ["cu2701"]},
        {"contracts": ("../escape",)},
        {"valid_from": datetime(2026, 1, 1)},
        {"valid_from": date(2028, 1, 1)},
    ],
)
def test_invalid_identity_or_effective_period_is_rejected(changes):
    chosen = (replace(selections()[0], **changes), *selections()[1:])
    result = convert_shfe_hours(page(), selections=chosen)
    assert result.status == "INVALID_INPUT" and result.supplement is None


def test_duplicate_mapping_or_contract_identity_is_rejected():
    chosen = selections()
    assert convert_shfe_hours(page(), selections=(chosen[0], chosen[0])).status == "INVALID_INPUT"
    same_product = replace(chosen[1], product=chosen[0].product)
    assert (
        convert_shfe_hours(page(), selections=(chosen[0], same_product)).status == "INVALID_INPUT"
    )
    same_contract = replace(chosen[2], contracts=("cu2701",))
    assert (
        convert_shfe_hours(page(), selections=(chosen[0], same_contract)).status == "INVALID_INPUT"
    )
    assert convert_shfe_hours(page(), selections=[]).status == "INVALID_INPUT"
