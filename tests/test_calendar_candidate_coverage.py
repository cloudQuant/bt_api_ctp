"""Synthetic declarations only, never exchange or account evidence."""

import json
from copy import deepcopy
from datetime import date, datetime

import pytest

from bt_api_ctp import calendar_candidate_coverage as coverage
from bt_api_ctp.calendar_candidate_coverage import CoverageRequirement, check_candidate_coverage
from bt_api_ctp.calendar_supplement import SupplementCandidate, parse_supplement


def content():
    def template(kind, contract, night):
        return {
            "venue": "SHFE",
            "kind": kind,
            "product": "cu",
            "contracts": [contract],
            "valid_from": "2026-12-31",
            "valid_to": "2027-01-02",
            "day_windows": [
                {
                    "id": "day",
                    "phase": "continuous",
                    "start": "09:00",
                    "end": "10:00",
                    "end_day_offset": 0,
                }
            ],
            "night_windows": (
                [
                    {
                        "id": "night",
                        "phase": "continuous",
                        "start": "21:00",
                        "end": "01:00",
                        "end_day_offset": 1,
                    }
                ]
                if night
                else []
            ),
            "night_mappings": [
                {"trading_day": "2026-12-31", "start_date": None, "source_ids": ["synthetic"]},
                {
                    "trading_day": "2027-01-02",
                    "start_date": "2026-12-31" if night else None,
                    "source_ids": ["synthetic"],
                },
            ],
            "source_ids": ["synthetic"],
            "windows_complete": True,
            "exceptions_complete": True,
        }

    return {
        "sources": [{"id": "synthetic", "description": "private source description"}],
        "trading_days": [
            {"venue": "SHFE", "day": "2026-12-31", "status": "open", "source_ids": ["synthetic"]},
            {"venue": "SHFE", "day": "2027-01-01", "status": "closed", "source_ids": ["synthetic"]},
            {"venue": "SHFE", "day": "2027-01-02", "status": "open", "source_ids": ["synthetic"]},
        ],
        "trading_hours": [
            template("future", "cu2701", True),
            template("option", "cu2701-C-100", False),
        ],
        "exceptions": [],
    }


def candidate(value=None):
    return parse_supplement(json.dumps(content() if value is None else value).encode())


def requirement(**changes):
    values = {
        "venue": "SHFE",
        "kind": "future",
        "product": "cu",
        "contract": "cu2701",
        "start_day": date(2026, 12, 31),
        "end_day": date(2027, 1, 2),
    }
    values.update(changes)
    return CoverageRequirement(**values)


def check(value=None, *items):
    return check_candidate_coverage(
        candidate() if value is None else value, requirements=items or (requirement(),)
    )


def test_closed_open_and_cross_year_are_only_structurally_complete():
    value = candidate()
    before = deepcopy(value)
    option = requirement(kind="option", contract="cu2701-C-100")
    result = check(value, requirement(), option)
    assert (result.status, result.gaps, result.requested_days, result.checked_days) == (
        "STRUCTURAL_COMPLETE_UNVERIFIED",
        (),
        6,
        6,
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
        "NOT_AUTHENTICATED",
        "NOT_PROVEN",
        "NOT_AUTHORIZED",
    )
    assert "private source description" not in repr(result)
    assert value == before


def test_missing_dates_and_outside_template_bounds_are_exact_gaps():
    value = content()
    value["trading_days"] = [row for row in value["trading_days"] if row["day"] != "2027-01-01"]
    requested = requirement(start_day=date(2026, 12, 30), end_day=date(2027, 1, 3))
    result = check(candidate(value), requested)
    assert result.status == "STRUCTURAL_GAPS" and result.checked_days == 5
    assert [(gap.start_day, gap.end_day, gap.reason) for gap in result.gaps] == [
        (date(2026, 12, 30), date(2026, 12, 30), "COVERAGE_INSUFFICIENT"),
        (date(2027, 1, 1), date(2027, 1, 1), "COVERAGE_INSUFFICIENT"),
        (date(2027, 1, 3), date(2027, 1, 3), "COVERAGE_INSUFFICIENT"),
    ]


def test_absent_contract_venue_and_future_option_are_not_inferred():
    requests = (
        requirement(contract="cu9999"),
        requirement(kind="option", contract="cu2701"),
        requirement(venue="DCE"),
    )
    result = check(candidate(), *requests)
    assert result.status == "STRUCTURAL_GAPS"
    assert len(result.gaps) == 3
    assert all(
        g.reason == "IDENTITY_NOT_IN_SNAPSHOT"
        and g.start_day == date(2026, 12, 31)
        and g.end_day == date(2027, 1, 2)
        for g in result.gaps
    )


def test_missing_night_mapping_invalidates_all_requested_dates_and_identifies_day():
    value = content()
    value["trading_hours"][0]["night_mappings"].pop()
    result = check(candidate(value))
    assert (result.status, result.reason, result.checked_days) == (
        "STRUCTURAL_GAPS",
        "SNAPSHOT_INVALID",
        0,
    )
    assert [(g.start_day, g.end_day, g.reason) for g in result.gaps] == [
        (date(2026, 12, 31), date(2027, 1, 1), "SNAPSHOT_INVALID"),
        (date(2027, 1, 2), date(2027, 1, 2), "MISSING_NIGHT_MAPPING"),
    ]


def test_unasserted_completeness_is_gap_even_with_known_dates():
    value = content()
    value["trading_hours"][0]["exceptions_complete"] = False
    result = check(candidate(value))
    assert result.status == "STRUCTURAL_GAPS"
    assert len(result.gaps) == 1
    assert (result.gaps[0].start_day, result.gaps[0].end_day, result.gaps[0].reason) == (
        date(2026, 12, 31),
        date(2027, 1, 2),
        "RULE_COVERAGE_INCOMPLETE",
    )


def test_constructor_overlap_cannot_leave_partial_success():
    value = content()
    value["trading_hours"].append(deepcopy(value["trading_hours"][0]))
    result = check(candidate(value))
    assert (result.status, result.reason, result.checked_days) == (
        "STRUCTURAL_GAPS",
        "SNAPSHOT_INVALID",
        0,
    )
    assert len(result.gaps) == 1 and result.gaps[0].reason == "SNAPSHOT_INVALID"


def test_query_exception_invalidates_whole_request(monkeypatch):
    original = coverage.CalendarSnapshot

    class Broken:
        def __init__(self, *args, **kwargs):
            self.wrapped = original(*args, **kwargs)
            self.count = 0

        def query(self, **kwargs):
            self.count += 1
            if self.count == 2:
                raise RuntimeError("private data")
            return self.wrapped.query(**kwargs)

    monkeypatch.setattr(coverage, "CalendarSnapshot", Broken)
    result = check(candidate())
    assert (result.status, result.reason, result.checked_days) == (
        "STRUCTURAL_GAPS",
        "QUERY_FAILED",
        0,
    )
    assert len(result.gaps) == 1 and result.gaps[0].reason == "QUERY_FAILED"
    assert "private data" not in repr(result)


@pytest.mark.parametrize(
    "req",
    [
        requirement(kind="FUTURE"),
        requirement(venue="UNKNOWN"),
        requirement(product=""),
        requirement(start_day=datetime(2026, 12, 31)),
        requirement(end_day=date(2026, 1, 1)),
        requirement(start_day=date(2026, 1, 1), end_day=date(2028, 1, 2)),
    ],
)
def test_bad_requirements_fail_before_query(req):
    result = check(candidate(), req)
    assert (result.status, result.reason, result.gaps) == (
        "INVALID_INPUT",
        "INVALID_REQUIREMENTS_OR_BUDGET",
        (),
    )


def test_duplicate_overlapping_and_large_budgets_rejected():
    first = requirement()
    assert check(candidate(), first, first).status == "INVALID_INPUT"
    many = tuple(requirement(contract=f"cu{i}") for i in range(coverage.MAX_REQUIREMENTS + 1))
    assert check(candidate(), *many).status == "INVALID_INPUT"
    long = tuple(
        requirement(contract=f"cu{i}", start_day=date(2027, 1, 1), end_day=date(2028, 12, 31))
        for i in range(12)
    )
    assert check(candidate(), *long).status == "INVALID_INPUT"
    assert check_candidate_coverage(candidate(), requirements=[]).status == "INVALID_INPUT"


@pytest.mark.parametrize(
    "start_year,end_year,expected_days", [(2026, 2027, 730), (2027, 2028, 731)]
)
def test_one_scope_complete_two_years_including_leap_year(start_year, end_year, expected_days):
    start, end = date(start_year, 1, 1), date(end_year, 12, 31)
    value = content()
    value["trading_days"] = [
        {
            "venue": "SHFE",
            "day": date.fromordinal(ordinal).isoformat(),
            "status": "closed",
            "source_ids": ["synthetic"],
        }
        for ordinal in range(start.toordinal(), end.toordinal() + 1)
    ]
    rule = value["trading_hours"][0]
    rule.update(valid_from=start.isoformat(), valid_to=end.isoformat(), night_mappings=[])
    value["trading_hours"] = [rule]
    result = check(candidate(value), requirement(start_day=start, end_day=end))
    assert (result.status, result.requested_days, result.checked_days, result.gaps) == (
        "STRUCTURAL_COMPLETE_UNVERIFIED",
        expected_days,
        expected_days,
        (),
    )
    assert result.verification == "UNVERIFIED" and result.publication == "NOT_AUTHORIZED"


@pytest.mark.parametrize(
    "start_year,end_year,expected_days", [(2026, 2027, 730), (2027, 2028, 731)]
)
def test_six_two_year_venue_requirements_fit_query_budget(start_year, end_year, expected_days):
    requirements = tuple(
        requirement(venue=venue, start_day=date(start_year, 1, 1), end_day=date(end_year, 12, 31))
        for venue in ("SHFE", "INE", "CZCE", "DCE", "CFFEX", "GFEX")
    )
    result = check(candidate(), *requirements)
    assert result.status == "STRUCTURAL_GAPS"
    assert (result.requested_days, result.checked_days) == (6 * expected_days, 6 * expected_days)
    assert result.verification == "UNVERIFIED" and result.publication == "NOT_AUTHORIZED"


def test_six_two_year_complete_venue_inputs_exceed_existing_supplement_layer_cap():
    value = content()
    start, end = date(2026, 1, 1), date(2027, 12, 31)
    value["trading_days"] = [
        {
            "venue": venue,
            "day": date.fromordinal(ordinal).isoformat(),
            "status": "closed",
            "source_ids": ["synthetic"],
        }
        for venue in ("SHFE", "INE", "CZCE", "DCE", "CFFEX", "GFEX")
        for ordinal in range(start.toordinal(), end.toordinal() + 1)
    ]
    result = check(SupplementCandidate(value, ()))
    assert (result.status, result.reason, result.gaps) == (
        "INVALID_INPUT",
        "INVALID_CANDIDATE_OR_BUDGET",
        (),
    )


def test_invalid_candidate_and_work_budget_are_fail_closed():
    good = candidate()
    forged = SupplementCandidate(good.content, good.unused_source_ids, verification="VERIFIED")
    assert check(forged).status == "INVALID_INPUT"
    assert check(SupplementCandidate(good.content, ("invalid",))).status == "INVALID_INPUT"
    value = content()
    value["trading_days"] *= 400
    result = check(SupplementCandidate(value, ()))
    assert (result.status, result.reason, result.gaps) == (
        "INVALID_INPUT",
        "INVALID_CANDIDATE_OR_BUDGET",
        (),
    )
