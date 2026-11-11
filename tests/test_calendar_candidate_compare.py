"""Only synthetic, offline comparison of partial unverified candidates."""

from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest
from test_calendar_hours_merge import merge

from bt_api_ctp.calendar_candidate_compare import compare_partial_candidates
from bt_api_ctp.calendar_supplement import _validate

pytest_plugins = ["test_calendar_hours_merge"]


def compared(left, right):
    result = compare_partial_candidates(left, right)
    assert (result.verification, result.coverage, result.publication) == (
        "UNVERIFIED",
        "NOT_PROVEN",
        "NOT_AUTHORIZED",
    )
    return result.status


def altered(result, fn):
    content = deepcopy(result.supplement.content)
    fn(content)
    return replace(result, supplement=_validate(content))


def test_reordering_collections_keeps_all_reviews_and_leaves_inputs_unchanged(inputs):
    original = merge(inputs)
    content = deepcopy(original.supplement.content)
    for key in content:
        content[key].reverse()
    for layer in ("trading_days", "trading_hours", "exceptions"):
        for row in content[layer]:
            row["source_ids"].reverse()
            if layer == "trading_hours":
                for key in ("contracts", "day_windows", "night_windows", "night_mappings"):
                    row[key].reverse()
                for mapping in row["night_mappings"]:
                    mapping["source_ids"].reverse()
    other = replace(
        original,
        supplement=_validate(content),
        holiday_unresolved_nights=tuple(reversed(original.holiday_unresolved_nights)),
        hours_unresolved_nights=tuple(reversed(original.hours_unresolved_nights)),
        remaining_checks=tuple(reversed(original.remaining_checks)),
    )
    originals = deepcopy((original, other))
    assert compared(original, other) == "SAME"
    assert (original, other) == originals


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: next(r for r in c["trading_days"] if r["venue"] == "DCE").update(status="closed"),
        lambda c: c["trading_hours"][0].update(valid_to="2028-01-01"),
        lambda c: c["exceptions"][0].update(trading_day="2026-01-03"),
        lambda c: next(r for r in c["trading_hours"] if r["day_windows"])["day_windows"][0].update(
            start="08:54"
        ),
    ],
)
def test_structured_rules_or_dates_change(inputs, mutation):
    original = merge(inputs)
    assert compared(original, altered(original, mutation)) == "CHANGED"


def test_hash_change_is_unknown_even_if_structured_rules_do_not_change(inputs):
    original = merge(inputs)
    content = deepcopy(original.supplement.content)
    source = next(s for s in content["sources"] if s["id"].startswith("shfe.hours."))
    old = source["id"]
    new = "shfe.hours." + "0" * 64
    source["id"] = new
    source["description"] = source["description"].replace(old.removeprefix("shfe.hours."), "0" * 64)
    for row in content["trading_hours"]:
        row["source_ids"] = [new if s == old else s for s in row["source_ids"]]
    assert compared(original, replace(original, supplement=_validate(content))) == "UNKNOWN"


def test_other_source_changes_and_references_require_review(inputs):
    original = merge(inputs)
    changed_description = altered(
        original,
        lambda c: next(s for s in c["sources"] if s["id"] == "manual").update(description="other"),
    )
    assert compared(original, changed_description) == "UNKNOWN"

    def alter_ref(content):
        row = next(
            r
            for r in content["trading_days"]
            if r["venue"] == "SHFE" and "manual" in r["source_ids"]
        )
        row["source_ids"].remove("manual")

    assert compared(original, altered(original, alter_ref)) == "UNKNOWN"

    def change_reference_and_rule(content):
        alter_ref(content)
        next(r for r in content["trading_days"] if r["venue"] == "DCE").update(status="closed")

    assert compared(original, altered(original, change_reference_and_rule)) == "UNKNOWN"


def test_unresolved_night_identity_and_stage_changes_are_not_ignored(inputs):
    original = merge(inputs)
    first = original.holiday_unresolved_nights[0]
    new = replace(first, local_date=date(2025, 12, 30))
    altered_holiday = replace(
        original, holiday_unresolved_nights=(new, *original.holiday_unresolved_nights[1:])
    )
    assert compared(original, altered_holiday) == "CHANGED"
    review = next(n for n in original.hours_unresolved_nights if n.stages)
    stage = replace(review.stages[-1], end="00:59")
    changed = replace(review, stages=(*review.stages[:-1], stage))
    altered_hours = replace(
        original,
        hours_unresolved_nights=tuple(
            changed if n == review else n for n in original.hours_unresolved_nights
        ),
    )
    assert compared(original, altered_hours) == "CHANGED"


def test_raw_text_notes_and_reasons_cannot_be_silently_ignored(inputs):
    original = merge(inputs)
    first = original.holiday_unresolved_nights[0]
    changed = replace(first, raw_text=first.raw_text + " 额外说明")
    assert (
        compared(
            original,
            replace(
                original,
                holiday_unresolved_nights=(changed, *original.holiday_unresolved_nights[1:]),
            ),
        )
        == "UNKNOWN"
    )
    assert (
        compared(original, replace(original, page_notes=(*original.page_notes, "新增说明")))
        == "UNKNOWN"
    )
    assert (
        compared(
            original,
            replace(original, page_review_reasons=(*original.page_review_reasons, "NEW_REASON")),
        )
        == "UNKNOWN"
    )
    review = next(n for n in original.hours_unresolved_nights if n.stages)
    stage = replace(review.stages[0], raw_line=review.stages[0].raw_line + " changed")
    new = replace(review, stages=(stage, *review.stages[1:]))
    assert (
        compared(
            original,
            replace(
                original,
                hours_unresolved_nights=tuple(
                    new if n == review else n for n in original.hours_unresolved_nights
                ),
            ),
        )
        == "UNKNOWN"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"status": "CONFLICT"},
        {"verification": "VERIFIED"},
        {"coverage": "COMPLETE"},
        {"publication": "AUTHORIZED"},
        {"source_authentication": "CHECKED"},
        {"remaining_checks": ()},
        {"supplement": None},
        {"hours_unresolved_nights": ()},
        {"holiday_unresolved_nights": ()},
        {"page_notes": ("x" * 8193,)},
    ],
)
def test_invalid_or_unexpected_metadata_is_never_same_or_changed(inputs, change):
    original = merge(inputs)
    assert compared(original, replace(original, **change)) == "UNKNOWN"
    assert compared(None, original) == "UNKNOWN"


def test_forged_or_missing_unresolved_hours_are_unknown(inputs):
    original = merge(inputs)
    first = original.hours_unresolved_nights[0]
    for change in ({"availability": "KNOWN"}, {"product": "unmatched"}, {"contracts": ("bad",)}):
        bad = replace(first, **change)
        assert (
            compared(
                original,
                replace(
                    original, hours_unresolved_nights=(bad, *original.hours_unresolved_nights[1:])
                ),
            )
            == "UNKNOWN"
        )
    assert (
        compared(
            original,
            replace(original, hours_unresolved_nights=original.hours_unresolved_nights[:-1]),
        )
        == "UNKNOWN"
    )
