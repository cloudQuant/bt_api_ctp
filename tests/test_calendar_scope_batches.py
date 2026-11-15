"""Bounded synthetic data only, no network or claimed venue facts."""

from copy import deepcopy
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

from bt_api_ctp import calendar_scope_batches as module
from bt_api_ctp.calendar_auto_candidate import build_auto_candidate
from bt_api_ctp.calendar_candidate_coverage import CoverageRequirement
from bt_api_ctp.calendar_scope_batches import check_scope_batches
from bt_api_ctp.calendar_snapshot import CalendarSnapshot

START, END = date(2026, 1, 1), date(2027, 12, 31)
SOURCE = "shfe.holiday.2026." + "a" * 64


def content(*, start=START, end=END, scopes=12, shared=False):
    days, mappings = [], []
    for ordinal in range(start.toordinal(), end.toordinal() + 1):
        day = date.fromordinal(ordinal).isoformat()
        opened = (ordinal - start.toordinal()) % 7 < 5
        days.append(
            {
                "venue": "SHFE",
                "day": day,
                "status": "open" if opened else "closed",
                "source_ids": [SOURCE],
            }
        )
        if opened:
            mappings.append({"trading_day": day, "start_date": None, "source_ids": [SOURCE]})
    templates = []
    for index in range(1 if shared else scopes):
        kind = "future" if shared or index < scopes // 2 else "option"
        contracts = (
            [f"contract{i:02}" for i in range(scopes)] if shared else [f"contract{index:02}"]
        )
        templates.append(
            {
                "venue": "SHFE",
                "kind": kind,
                "product": "probe",
                "contracts": contracts,
                "valid_from": start.isoformat(),
                "valid_to": end.isoformat(),
                "day_windows": [
                    {
                        "id": "day",
                        "phase": "continuous",
                        "start": "09:00",
                        "end": "10:00",
                        "end_day_offset": 0,
                    }
                ],
                "night_windows": [],
                "night_mappings": deepcopy(mappings),
                "source_ids": [SOURCE],
                "windows_complete": True,
                "exceptions_complete": True,
            }
        )
    return {
        "sources": [{"id": SOURCE, "description": "synthetic"}],
        "trading_days": days,
        "trading_hours": templates,
        "exceptions": [],
    }


def build(value, *, start=START, end=END):
    result = build_auto_candidate(
        venue="SHFE", coverage_start=start, coverage_end=end, layers=value
    )
    assert result.status == "CANDIDATE"
    return result


def requests(*, start=START, end=END, scopes=12, shared=False):
    return tuple(
        CoverageRequirement(
            "SHFE",
            "future" if shared or i < scopes // 2 else "option",
            "probe",
            f"contract{i:02}",
            start,
            end,
        )
        for i in range(scopes)
    )


def test_twelve_scopes_two_years_checked_using_private_contract_views():
    original = content()
    auto = build(original)
    before = deepcopy(auto)
    result = check_scope_batches(auto, requirements=requests())
    assert (result.status, result.gaps, result.requested_days, result.checked_days) == (
        "SCOPES_CHECKED_UNVERIFIED",
        (),
        8760,
        8760,
    )
    assert (
        result.verification,
        result.coverage,
        result.source_authentication,
        result.utc_coverage,
        result.publication,
    ) == (
        "UNVERIFIED",
        "CALLER_DECLARED_SCOPE_ONLY",
        "SOURCE_NOT_AUTHENTICATED",
        "UTC_NOT_PROVEN",
        "NOT_AUTHORIZED",
    )
    assert (result.global_relations, result.final_composition) == (
        "OTHER_SCOPE_RELATIONS_NOT_CHECKED",
        "FINAL_COMPOSITION_AND_CONFLICT_CHECK_REQUIRED",
    )
    assert auto == before and original == content()
    assert not hasattr(result, "candidate")


def test_sixty_four_scope_leap_year_budget_at_maximum():
    start, end = date(2027, 1, 1), date(2028, 12, 31)
    auto = build(content(start=start, end=end, scopes=64, shared=True), start=start, end=end)
    result = check_scope_batches(
        auto, requirements=requests(start=start, end=end, scopes=64, shared=True)
    )
    assert (result.status, result.requested_days, result.checked_days) == (
        "SCOPES_CHECKED_UNVERIFIED",
        64 * 731,
        64 * 731,
    )
    assert result.requested_days > 16384 and result.requested_days == module.MAX_BATCHED_QUERY_DAYS


def test_exact_and_product_level_exceptions_are_retained_in_matching_views(monkeypatch):
    value = content()

    def exception(day, contract):
        return {
            "venue": "SHFE",
            "kind": "future",
            "product": "probe",
            "contract": contract,
            "trading_day": day,
            "action": "close_day",
            "window_id": None,
            "source_ids": [SOURCE],
        }

    value["exceptions"] = [exception("2026-01-01", "contract00"), exception("2026-01-02", None)]
    auto = build(value)
    seen = {}
    original = module.check_candidate_coverage

    def spy(view, *, requirements):
        req = requirements[0]
        snapshot = CalendarSnapshot(view.content, calendar_ref="simulation")
        for day in (date(2026, 1, 1), date(2026, 1, 2)):
            queried = snapshot.query(
                venue=req.venue,
                kind=req.kind,
                product=req.product,
                contract=req.contract,
                trading_day=day,
            )
            seen[(req.contract, day)] = len(queried.windows)
        return original(view, requirements=requirements)

    monkeypatch.setattr(module, "check_candidate_coverage", spy)
    assert check_scope_batches(auto, requirements=requests()).status == "SCOPES_CHECKED_UNVERIFIED"
    assert seen[("contract00", date(2026, 1, 1))] == 0
    assert seen[("contract01", date(2026, 1, 1))] == 1
    assert seen[("contract01", date(2026, 1, 2))] == 0
    assert seen[("contract06", date(2026, 1, 2))] == 1


def test_missing_day_contract_and_mapping_never_pass():
    value = content()
    value["trading_days"] = [x for x in value["trading_days"] if x["day"] != "2026-01-06"]
    result = check_scope_batches(build(value), requirements=(requests()[0],))
    assert result.status == "STRUCTURAL_GAPS" and result.gaps[0].reason == "COVERAGE_INSUFFICIENT"
    auto = build(content())
    missing = replace(requests()[0], contract="missing")
    result = check_scope_batches(auto, requirements=(missing,))
    assert result.status == "STRUCTURAL_GAPS" and result.gaps
    value = content()
    value["trading_hours"][0]["night_mappings"].pop()
    result = check_scope_batches(build(value), requirements=(requests()[0],))
    assert result.status == "STRUCTURAL_GAPS"
    assert any(gap.reason == "MISSING_NIGHT_MAPPING" for gap in result.gaps)


def test_scope_external_invalid_data_and_missing_source_fail_closed():
    auto = build(content())
    auto.candidate.content["trading_hours"][11]["source_ids"] = ["missing"]
    result = check_scope_batches(auto, requirements=(requests()[0],))
    assert (
        result.status == "STRUCTURAL_GAPS" and result.reason == "INVALID_CANDIDATE_OR_REQUIREMENTS"
    )
    auto = build(content())
    auto.candidate.content["trading_hours"][11]["day_windows"].append(
        {
            "id": "conflict",
            "phase": "continuous",
            "start": "09:30",
            "end": "10:30",
            "end_day_offset": 0,
        }
    )
    result = check_scope_batches(auto, requirements=(requests()[0],))
    assert result.status == "STRUCTURAL_GAPS" and result.gaps[0].reason == "FULL_CANDIDATE_INVALID"


def test_duplicate_bad_scope_over_limits_and_forged_markers_fail_closed():
    auto = build(content())
    first = requests()[0]
    bad_sets = (
        (first, first),
        (replace(first, venue="DCE"),),
        (replace(first, end_day=date(2028, 1, 1)),),
        requests(scopes=65, shared=True),
        tuple(replace(first, contract=f"x{i}", end_day=date(2028, 1, 2)) for i in range(64)),
    )
    for bad in bad_sets:
        result = check_scope_batches(auto, requirements=bad)
        assert (result.status, result.reason) == (
            "STRUCTURAL_GAPS",
            "INVALID_CANDIDATE_OR_REQUIREMENTS",
        )
    assert (
        check_scope_batches(replace(auto, verification="VERIFIED"), requirements=(first,)).status
        == "STRUCTURAL_GAPS"
    )


def test_mutating_original_after_capture_cannot_change_private_snapshot(monkeypatch):
    auto = build(content())
    original_build = module.build_auto_candidate

    def capture_then_mutate(**kwargs):
        result = original_build(**kwargs)
        auto.candidate.content["trading_days"].clear()
        auto.candidate.content["trading_hours"].clear()
        return result

    monkeypatch.setattr(module, "build_auto_candidate", capture_then_mutate)
    result = check_scope_batches(auto, requirements=(requests()[0],))
    assert (result.status, result.requested_days, result.checked_days) == (
        "SCOPES_CHECKED_UNVERIFIED",
        730,
        730,
    )


def test_child_failure_and_ambiguous_response_are_gaps(monkeypatch):
    auto = build(content())
    monkeypatch.setattr(
        module,
        "check_candidate_coverage",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("private")),
    )
    result = check_scope_batches(auto, requirements=(requests()[0],))
    assert result.status == "STRUCTURAL_GAPS" and result.gaps[0].reason == "SCOPE_CHECK_FAILED"
    assert "private" not in repr(result)
    monkeypatch.setattr(
        module,
        "check_candidate_coverage",
        lambda *a, **k: SimpleNamespace(status="STRUCTURAL_GAPS", gaps=(), checked_days=0),
    )
    result = check_scope_batches(auto, requirements=(requests()[0],))
    assert result.status == "STRUCTURAL_GAPS" and result.gaps[0].reason == "SCOPE_CHECK_FAILED"
