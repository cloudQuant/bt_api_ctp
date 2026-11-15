"""All HTML fixtures are synthetic, not copied from the public source."""

import hashlib

import pytest

from bt_api_ctp.calendar_shfe_page import MAX_BYTES, parse_shfe_page

DAY = (
    "日盘<br>集合竞价申报 08:55-08:59<br>集合竞价撮合 08:59-09:00<br>"
    "连续竞价交易 09:00-10:15<br>连续竞价交易 10:30-11:30<br>连续竞价交易 13:30-15:00"
)
NIGHT = (
    "夜盘<br>集合竞价申报 20:55-20:59<br>集合竞价撮合 20:59-21:00<br>连续竞价交易 21:00-01:00<br>"
)


def row(names="期货：合成甲、合成乙<br>期权：合成甲期权", times=DAY):
    return f"<tr><td>{names}</td><td>{times}</td></tr>"


def table(*rows):
    return "<table><tbody>" + "".join(rows) + "</tbody></table>"


def parse(content):
    return parse_shfe_page(source_id="synthetic:page", html=content.encode("utf-8"))


def test_separate_future_option_names_explicit_stages_and_no_venue_inference():
    source = table(row())
    result = parse(source)
    assert result.status == "CANDIDATE" and result.verification == "UNVERIFIED"
    assert result.source_id == "synthetic:page"
    assert result.sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert [(p.kind, p.name) for p in result.products] == [
        ("future", "合成甲"),
        ("future", "合成乙"),
        ("option", "合成甲期权"),
    ]
    first = result.products[0]
    assert [(s.session, s.phase, s.start, s.end) for s in first.stages] == [
        ("DAY", "AUCTION_ORDER", "08:55", "08:59"),
        ("DAY", "AUCTION_MATCH", "08:59", "09:00"),
        ("DAY", "CONTINUOUS", "09:00", "10:15"),
        ("DAY", "CONTINUOUS", "10:30", "11:30"),
        ("DAY", "CONTINUOUS", "13:30", "15:00"),
    ]
    assert "期货：合成甲" in first.raw_product_cell
    assert "连续竞价交易 09:00-10:15" in first.raw_time_cell
    assert not hasattr(first, "venue") and not hasattr(first, "contract")
    assert not hasattr(first, "effective_date")
    assert all(s.session == "DAY" for s in first.stages)  # not a claim that night is absent


def test_identical_duplicate_table_is_deduplicated_without_synthesizing_data():
    content = table(row(), row(names="期货：合成丙", times=DAY))
    result = parse(content + content)
    assert result.status == "CANDIDATE"
    assert len(result.products) == 4


def test_nonidentical_table_text_is_preserved_even_when_times_match():
    original = table(row(names="期货：合成甲"))
    changed_spacing = original.replace("日盘<br>", "日盘 <br>")
    result = parse(original + changed_spacing)
    assert result.status == "CANDIDATE"
    assert len(result.products) == 2
    assert result.products[0].raw_time_cell != result.products[1].raw_time_cell


def test_same_product_with_conflicting_periods_reports_conflict_and_keeps_both():
    original = table(row(names="期货：合成甲"))
    changed = table(row(names="期货：合成甲", times=DAY.replace("13:30-15:00", "13:30-15:30")))
    result = parse(original + changed)
    assert result.status == "CONFLICT"
    assert len(result.products) == 2
    assert result.products[0].stages[-1].end == "15:00"
    assert result.products[1].stages[-1].end == "15:30"
    assert "PRODUCT_OR_PHASE_CONFLICT" in result.review_reasons


def test_overlapping_stages_report_conflict():
    result = parse(table(row(times=DAY.replace("10:30-11:30", "10:00-11:30"))))
    assert result.status == "CONFLICT" and result.verification == "UNVERIFIED"


def test_conflicting_duplicate_auction_stage_reports_conflict():
    times = DAY.replace(
        "集合竞价申报 08:55-08:59<br>", "集合竞价申报 08:40-08:45<br>集合竞价申报 08:55-08:59<br>"
    )
    result = parse(table(row(times=times)))
    assert result.status == "CONFLICT"
    assert "PRODUCT_OR_PHASE_CONFLICT" in result.review_reasons


def test_unmarked_night_clock_descent_requires_review_not_calendar_inference():
    result = parse(table(row(times=NIGHT + DAY)))
    assert result.status == "NEEDS_REVIEW"
    night = [s for s in result.products[0].stages if s.session == "NIGHT"]
    assert night[-1].day_transition == "INFERRED_NEXT_DAY"
    assert "CROSS_MIDNIGHT_UNVERIFIED" in result.review_reasons
    assert not hasattr(night[-1], "trading_day")


def test_explicit_next_day_is_only_a_textual_declaration():
    result = parse(table(row(times=NIGHT.replace("21:00-01:00", "21:00-次日01:00") + DAY)))
    assert result.status == "CANDIDATE"
    assert result.products[0].stages[2].day_transition == "EXPLICIT_NEXT_DAY"
    assert result.verification == "UNVERIFIED"


def test_note_is_preserved_but_never_interpreted_as_trading_day_rule():
    note = "注：合成备注，须另核对"
    result = parse(table(row(), f'<tr><td colspan="2">{note}</td></tr>'))
    assert result.status == "NEEDS_REVIEW" and result.raw_notes == (note,)
    assert "PAGE_NOTE_UNVERIFIED" in result.review_reasons


@pytest.mark.parametrize(
    "content",
    [
        table(row(times=DAY.replace("集合竞价申报 08:55-08:59<br>", ""))),
        table(row(times=DAY.replace("连续竞价交易", "未知交易阶段", 1))),
        table(row(times=DAY.replace("09:00-10:15", "09:61-10:15"))),
        table(row(times=DAY.replace("09:00-10:15", "⓪⑨:00-10:15"))),
        table(row(times=DAY.replace("09:00-10:15", "09:00-10:15附注"))),
        table(row(names="期货：", times=DAY)),
        table(row(names="合成甲", times=DAY)),
        table(row(times=NIGHT)),  # day schedule absent; cannot assume complete
        table(row())[:-8],  # unclosed table
        "<div>site structure has changed</div>",
        table("<tr><td><table><tr><td>nested</td></tr></table></td><td>x</td></tr>"),
        table("<tr>unexpected<td>期货：合成甲</td><td>x</td></tr>"),
        table('<tr><td colspan="2" colspan="1">注：疑问</td></tr>'),
    ],
)
def test_missing_or_changed_structure_is_rejected_without_raw_html(content):
    result = parse(content)
    assert result.status == "STRUCTURE_CHANGED"
    assert result.products == () and result.raw_notes == ()
    assert "<table" not in repr(result)


def test_invalid_input_bounds_and_missing_source_have_explicit_statuses():
    assert parse_shfe_page(source_id="", html=b"").status == "INVALID_SOURCE"
    assert parse_shfe_page(source_id="source", html=None).status == "INVALID_INPUT"
    assert (
        parse_shfe_page(source_id="source", html=b"x" * (MAX_BYTES + 1)).status == "INPUT_TOO_LARGE"
    )
    assert parse_shfe_page(source_id="source", html=b"\xff").status == "INVALID_UTF8"
