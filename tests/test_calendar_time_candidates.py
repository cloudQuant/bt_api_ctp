"""Synthetic strings only; no exchange or calendar truth is asserted."""

import pytest

from bt_api_ctp.calendar_time_candidates import MAX_FIELDS, MAX_TEXT, parse_time_candidates


def parse(text, *, source="synthetic.capture.1"):
    return parse_time_candidates(source_id=source, fields=[("名称", "示例"), ("交易时间", text)])


def test_full_width_punctuation_morning_afternoon_and_two_distinct_morning_ranges():
    raw = "上午：０９：００－１０：１５，１０：３０—１１：３０；下午：１３：３０－１５：００"
    result = parse(raw)
    assert result.status == "SYNTAX_CANDIDATE"
    assert result.verification == "UNVERIFIED"
    assert result.source_id == "synthetic.capture.1" and result.field_index == 1
    assert result.raw_text == raw
    assert result.normalized_text == "上午:09:00-10:15,10:30—11:30;下午:13:30-15:00"
    assert [(r.label, r.scope, r.start, r.end) for r in result.ranges] == [
        ("上午", "DAY", "09:00", "10:15"),
        ("上午", "DAY", "10:30", "11:30"),
        ("下午", "DAY", "13:30", "15:00"),
    ]
    for segment in result.ranges:
        a, b = segment.normalized_span
        assert result.normalized_text[a:b].startswith(segment.start)
    assert result.uncertain_spans == () and result.review_reasons == ()


def test_no_break_is_invented_inside_single_range():
    result = parse("上午 09:00-11:30")
    assert result.status == "SYNTAX_CANDIDATE"
    assert [(r.start, r.end) for r in result.ranges] == [("09:00", "11:30")]
    assert result.verification == "UNVERIFIED"


@pytest.mark.parametrize("text", ["上午 9:00-10:15", "上午９：００－１０：１５"])
def test_single_digit_hour_is_normalized_with_source_span_preserved(text):
    result = parse(text)
    assert result.status == "SYNTAX_CANDIDATE"
    assert result.ranges[0].start == "09:00"
    start, end = result.ranges[0].normalized_span
    assert result.normalized_text[start:end] == "9:00-10:15"
    assert result.raw_text == text


def test_sample_shape_preserves_unverified_weekday_and_continuous_text():
    text = "每周一至周五 上午9:00－10:15,10:30－11:30 下午13:30－15:00 连续交易时间21:00－23:00"
    result = parse(text)
    assert result.status == "NEEDS_REVIEW"
    assert result.raw_text == text
    assert [(r.label, r.start, r.end) for r in result.ranges] == [
        ("上午", "09:00", "10:15"),
        ("上午", "10:30", "11:30"),
        ("下午", "13:30", "15:00"),
    ]
    first = result.ranges[0]
    assert (
        result.normalized_text[first.normalized_span[0] : first.normalized_span[1]] == "9:00-10:15"
    )
    fragments = [result.normalized_text[a:b] for a, b in result.uncertain_spans]
    assert "每周一至周五" in fragments
    assert "连续交易时间" in fragments
    assert "21:00-23:00" in fragments
    assert "UNLABELED_RANGE" in result.review_reasons
    assert result.verification == "UNVERIFIED"


def test_day_and_explicit_next_day_night_are_text_candidates_only():
    result = parse("日盘 09:00—10:15；夜盘 21:00——次日01:00")
    assert result.status == "SYNTAX_CANDIDATE"
    assert [(r.scope, r.day_transition) for r in result.ranges] == [
        ("DAY", "SAME_CLOCK_DAY"),
        ("NIGHT", "EXPLICIT_NEXT_DAY"),
    ]
    assert not hasattr(result, "trading_day")
    assert not hasattr(result, "effective_date")
    assert result.verification == "UNVERIFIED"


def test_clock_only_descending_range_is_inferred_and_reviewed():
    result = parse("夜盘 21:00-01:00")
    assert result.status == "NEEDS_REVIEW"
    assert result.ranges[0].day_transition == "INFERRED_NEXT_DAY"
    assert "INFERRED_DAY_TRANSITION" in result.review_reasons


@pytest.mark.parametrize(
    "text,reason",
    [
        ("09:00-10:15", "UNLABELED_RANGE"),
        ("日盘 09:00-10:15 10:00-11:00", "OVERLAPPING_RANGES"),
        ("日盘 09:00-10:15，节假日另行通知", "UNRECOGNIZED_TEXT"),
        ("日盘 09:00-10:15 或 夜盘 21:00-23:00", "UNRECOGNIZED_TEXT"),
        ("夜盘 21:00-次日23:00", "AMBIGUOUS_DAY_TRANSITION"),
        ("日盘 21:00-次日01:00", "AMBIGUOUS_DAY_TRANSITION"),
        ("下午 01:30-03:00", "AMBIGUOUS_DAY_LABEL"),
        ("日盘 24:00-25:00", "INVALID_TIME_RANGE"),
        ("日盘 09:60-10:15", "INVALID_TIME_RANGE"),
        ("日盘 09:00-09:00", "INVALID_TIME_RANGE"),
        ("日盘 009:00-10:15", "UNRECOGNIZED_TEXT"),
        ("日盘 9:000-10:15", "UNRECOGNIZED_TEXT"),
        ("日盘 9:00-010:15", "UNRECOGNIZED_TEXT"),
        ("日盘 9:00-24:00", "INVALID_TIME_RANGE"),
        ("日盘 约09:00-10:15", "UNRECOGNIZED_TEXT"),
        ("日盘", "LABEL_WITHOUT_RANGE"),
        ("", "NO_LABELED_RANGES"),
    ],
)
def test_ambiguity_invalid_or_unrecognized_text_requires_review(text, reason):
    result = parse(text)
    assert result.status == "NEEDS_REVIEW"
    assert reason in result.review_reasons
    assert result.raw_text == text
    assert result.verification == "UNVERIFIED"


def test_unlabeled_and_unknown_residual_are_locatable():
    result = parse("09:00-10:15; 日盘 10:30-11:30 备注未核对")
    assert result.status == "NEEDS_REVIEW"
    fragments = [result.normalized_text[a:b] for a, b in result.uncertain_spans]
    assert "09:00-10:15" in fragments and "备注未核对" in fragments
    assert [(r.start, r.end) for r in result.ranges] == [("10:30", "11:30")]


@pytest.mark.parametrize("text", ["日盘 ⁰⁹:00-10:15", "日盘 ⓪⑨:00-10:15", "日盘 09﹕00-10:15"])
def test_nfkc_does_not_turn_unsupported_clock_characters_into_valid_ranges(text):
    result = parse(text)
    assert result.status == "NEEDS_REVIEW"
    assert result.review_reasons == ("UNSAFE_NORMALIZATION",)
    assert result.ranges == () and result.raw_text == text


def test_field_missing_duplicate_and_nonstring_are_explicit():
    assert (
        parse_time_candidates(source_id="src", fields=[("名称", "示例")]).status == "MISSING_FIELD"
    )
    duplicate = parse_time_candidates(
        source_id="src", fields=[("交易时间", "日盘 09:00-10:15"), ("交易时间", "夜盘 21:00-23:00")]
    )
    assert duplicate.status == "DUPLICATE_FIELD"
    assert duplicate.duplicate_fields == ((0, "日盘 09:00-10:15"), (1, "夜盘 21:00-23:00"))
    assert duplicate.duplicate_indexes == (0, 1)
    assert duplicate.ranges == ()
    oversized = parse_time_candidates(
        source_id="src",
        fields=[("交易时间", "日盘 09:00-10:15"), ("交易时间", "x" * (MAX_TEXT + 1))],
    )
    assert oversized.duplicate_indexes == (0, 1)
    assert oversized.review_reasons == ("DUPLICATE_VALUES_OMITTED",)
    assert parse(123).status == "INVALID_FIELD"
    assert parse(float("nan")).status == "INVALID_FIELD"
    assert parse("x" * (MAX_TEXT + 1)).status == "INPUT_TOO_LONG"


@pytest.mark.parametrize(
    "fields",
    [
        None,
        {},
        [None],
        [("交易时间",)],
        [(None, "x")],
        [("x" * (MAX_TEXT + 1), "x")],
        [("x", "x")] * (MAX_FIELDS + 1),
    ],
)
def test_unexpected_field_shapes_are_rejected(fields):
    result = parse_time_candidates(source_id="src", fields=fields)
    assert result.status == "INVALID_FIELDS" and result.verification == "UNVERIFIED"


@pytest.mark.parametrize("source", [None, "", " ", "x" * 513])
def test_source_identity_required_without_exposing_raw_errors(source):
    with pytest.raises(ValueError, match="invalid source identity") as exc:
        parse_time_candidates(source_id=source, fields=[("交易时间", "private secret")])
    assert "private secret" not in str(exc.value)
