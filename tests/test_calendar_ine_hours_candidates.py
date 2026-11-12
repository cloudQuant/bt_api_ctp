from dataclasses import replace
from datetime import date

from test_calendar_ine_holiday import page as holiday_page

from bt_api_ctp.calendar_ine_holiday import SOURCE_URL, parse_ine_holiday
from bt_api_ctp.calendar_ine_hours_candidates import convert_ine_hours, merge_ine_holiday_hours
from bt_api_ctp.calendar_ine_hours_fetch import SOURCE
from bt_api_ctp.calendar_shfe_hours_candidates import HoursSelection
from bt_api_ctp.calendar_shfe_page import parse_shfe_page


def fixture():
    raw = (
        "<table><tr><td>期货：合成铜<br>期权：合成铜期权</td><td>"
        "夜盘<br>集合竞价申报 20:55-20:59<br>集合竞价撮合 20:59-21:00<br>"
        "连续竞价交易 21:00-01:00<br>日盘<br>集合竞价申报 08:55-08:59<br>集合竞价撮合 08:59-09:00<br>"
        "连续竞价交易 09:00-10:15<br>连续竞价交易 10:30-11:30<br>"
        "连续竞价交易 13:30-15:00</td></tr></table>"
    ).encode()
    page = parse_shfe_page(source_id=SOURCE, html=raw)
    selections = (
        HoursSelection(
            "INE", "future", "合成铜", "bc", ("bc2701",), date(2026, 1, 1), date(2027, 12, 31)
        ),
        HoursSelection(
            "INE",
            "option",
            "合成铜期权",
            "bcopt",
            ("bc2701-C-1",),
            date(2026, 1, 1),
            date(2027, 12, 31),
        ),
    )
    return raw, page, selections


def test_exact_bytes_keep_kinds_and_unresolved_night():
    raw, page, selections = fixture()
    result = convert_ine_hours(page, html=raw, selections=selections)
    assert result.status == "CANDIDATE"
    assert {x["kind"] for x in result.supplement.content["trading_hours"]} == {"future", "option"}
    assert result.supplement.content["sources"][0]["id"] == "ine.hours." + page.sha256
    assert all(
        not x["windows_complete"] and not x["exceptions_complete"]
        for x in result.supplement.content["trading_hours"]
    )
    assert len(result.unresolved_nights) == 2
    assert all(
        x.stages[-1].day_transition == "INFERRED_NEXT_DAY" and x.trading_day_mapping == "UNKNOWN"
        for x in result.unresolved_nights
    )
    assert result.publication == "NOT_AUTHORIZED"


def test_changed_bytes_forgery_wrong_venue_and_kind_fail():
    raw, page, selections = fixture()
    assert convert_ine_hours(page, html=raw + b" ", selections=selections).status == "INVALID_INPUT"
    assert (
        convert_ine_hours(replace(page, sha256="0" * 64), html=raw, selections=selections).status
        == "INVALID_INPUT"
    )
    assert (
        convert_ine_hours(page, html=raw, selections=(replace(selections[0], venue="SHFE"),)).status
        == "INVALID_INPUT"
    )
    assert (
        convert_ine_hours(
            page, html=raw, selections=(replace(selections[0], kind="option"),)
        ).status
        == "INVALID_INPUT"
    )


def test_merge_reconstructs_both_sources_and_keeps_all_exceptions():
    raw, page, selections = fixture()
    holiday_raw = holiday_page()
    holiday = parse_ine_holiday(source_id=SOURCE_URL, html=holiday_raw)
    result = merge_ine_holiday_hours(
        holiday=holiday, holiday_html=holiday_raw, page=page, hours_html=raw, selections=selections
    )
    assert result.status == "PARTIAL"
    assert len(result.supplement.content["trading_days"]) == 39
    assert len(result.holiday_unresolved_nights) == 7
    assert len(result.hours_unresolved_nights) == 2
    assert all(n.trading_day is None for n in result.holiday_unresolved_nights)
    assert result.publication == "NOT_AUTHORIZED"
    assert (
        merge_ine_holiday_hours(
            holiday=holiday,
            holiday_html=holiday_raw + b" ",
            page=page,
            hours_html=raw,
            selections=selections,
        ).status
        == "INVALID_INPUT"
    )


def test_selection_limit_and_duplicate_contract_conflict():
    raw, page, selections = fixture()
    duplicated = (selections[0], replace(selections[1], contracts=selections[0].contracts))
    assert convert_ine_hours(page, html=raw, selections=duplicated).status == "INVALID_INPUT"
    assert convert_ine_hours(page, html=raw, selections=selections * 65).status == "INVALID_INPUT"
    session = (
        "日盘<br>集合竞价申报 08:55-08:59<br>集合竞价撮合 08:59-09:00<br>连续竞价交易 09:00-10:15"
    )
    raw = (
        "<table>"
        + "".join(f"<tr><td>期货：品种{i}</td><td>{session}</td></tr>" for i in range(129))
        + "</table>"
    ).encode()
    page = parse_shfe_page(source_id=SOURCE, html=raw)
    rows = tuple(
        HoursSelection(
            "INE", "future", f"品种{i}", f"p{i}", (f"c{i}",), date(2026, 1, 1), date(2026, 12, 31)
        )
        for i in range(129)
    )
    assert convert_ine_hours(page, html=raw, selections=rows[:128]).status == "CANDIDATE"
    assert convert_ine_hours(page, html=raw, selections=rows).status == "INVALID_INPUT"


def test_forged_phase_never_changes_raw_page_projection():
    class Deceptive(str):
        def __eq__(self, other):
            return True

        __hash__ = str.__hash__

    raw, page, selections = fixture()
    product = page.products[0]
    stages = list(product.stages)
    stages[-1] = replace(stages[-1], phase=Deceptive("AUCTION_ORDER"))
    forged = replace(page, products=(replace(product, stages=tuple(stages)),) + page.products[1:])
    result = convert_ine_hours(forged, html=raw, selections=selections)
    clean = convert_ine_hours(page, html=raw, selections=selections)
    assert result.status == "INVALID_INPUT" or result == clean


def test_multiple_tables_256_and_257_rows_are_rechecked_without_loss():
    session = (
        "日盘<br>集合竞价申报 08:55-08:59<br>集合竞价撮合 08:59-09:00<br>连续竞价交易 09:00-10:15"
    )
    for count in (256, 257, 512):
        rows = [f"<tr><td>期货：品种{i}</td><td>{session}</td></tr>" for i in range(count)]
        raw = (
            "<table>"
            + "".join(rows[:256])
            + "</table>"
            + ("<table>" + "".join(rows[256:]) + "</table>" if count > 256 else "")
        ).encode()
        page = parse_shfe_page(source_id=SOURCE, html=raw)
        selected = (
            HoursSelection(
                "INE",
                "future",
                f"品种{count - 1}",
                "p",
                ("c",),
                date(2026, 1, 1),
                date(2026, 12, 31),
            ),
        )
        assert page.status == "CANDIDATE"
        assert convert_ine_hours(page, html=raw, selections=selected).status == "CANDIDATE"


def test_multiple_tables_duplicate_and_conflicting_products_are_not_accepted():
    raw, page, selections = fixture()
    product = page.products[0]
    duplicate = replace(page, products=page.products + (product,))
    assert convert_ine_hours(duplicate, html=raw, selections=selections).status == "INVALID_INPUT"
    modified = raw.replace(b"09:00-10:15", b"09:00-10:10")
    mixed = raw + modified
    conflict = parse_shfe_page(source_id=SOURCE, html=mixed)
    assert conflict.status == "CONFLICT"
    assert convert_ine_hours(conflict, html=mixed, selections=selections).status == "INVALID_INPUT"
