"""All examples are synthetic; these tests never call AKShare or a network."""

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from bt_api_ctp.calendar_akshare_candidates import collect_contract_candidate


def collect(frame=None, **kwargs):
    args = dict(
        venue="SHFE",
        contract="RB2701",
        requested_date=date(2026, 11, 14),
        source_version="1.18.64",
        fetch=lambda **_: frame,
    )
    args.update(kwargs)
    return collect_contract_candidate(**args)


def test_success_retains_all_raw_fields_and_does_not_claim_date_or_authority():
    calls = []
    frame = pd.DataFrame(
        {
            "value": ["  09:00-10:15\n10:30-11:30  ", " 示例  合约 "],
            "item": ["交易时间", " 合约名称 "],
        }
    )
    fetch_end = []

    def fetch(**kw):
        calls.append(kw)
        fetch_end.append(datetime.now(timezone.utc))
        return frame

    result = collect(fetch=fetch)
    assert calls == [{"symbol": "RB2701"}]
    assert result.status == "SUCCESS"
    assert result.verification == "UNVERIFIED"
    assert result.requested_date == date(2026, 11, 14)  # metadata, even on a weekend
    assert fetch_end[0] <= result.collected_at <= datetime.now(timezone.utc)
    assert result.source == "akshare.futures_contract_detail"
    assert result.source_version == "1.18.64"
    assert result.timeout_enforced is False
    assert result.fields[0].raw_value == "  09:00-10:15\n10:30-11:30  "
    assert result.fields[0].value == "09:00-10:15 10:30-11:30"
    assert result.fields[1].name == "合约名称"
    assert result.fields[1].value == "示例 合约"
    assert not hasattr(result, "trading_day")
    assert not hasattr(result, "sessions")


@pytest.mark.parametrize(
    "frame,status",
    [
        (pd.DataFrame(columns=["item", "value"]), "EMPTY"),
        (pd.DataFrame({"item": ["合约名称"], "value": ["示例"]}), "MISSING_TRADING_TIME"),
        (pd.DataFrame({"item": ["交易时间"], "value": [float("nan")]}), "MISSING_TRADING_TIME"),
        (pd.DataFrame({"item": ["交易时间"], "value": [pd.NA]}), "MISSING_TRADING_TIME"),
        (pd.DataFrame({"item": ["交易时间"], "value": [123]}), "MISSING_TRADING_TIME"),
        (pd.DataFrame({"item": ["交易时间"], "value": [float("inf")]}), "MISSING_TRADING_TIME"),
        (pd.DataFrame({"item": ["交易时间"], "value": ["  "]}), "MISSING_TRADING_TIME"),
        (
            pd.DataFrame({"item": ["交易时间", "交易时间"], "value": ["a", "b"]}),
            "STRUCTURE_CHANGED",
        ),
        (
            pd.DataFrame({"item": ["交易时间", " 交易时间 "], "value": ["a", "b"]}),
            "STRUCTURE_CHANGED",
        ),
        (pd.DataFrame({"item": [None], "value": ["a"]}), "STRUCTURE_CHANGED"),
        (pd.DataFrame({"item": ["交易时间"]}), "STRUCTURE_CHANGED"),
        (pd.DataFrame({"item": ["交易时间"], "value": ["a"], "extra": [1]}), "STRUCTURE_CHANGED"),
        (pd.DataFrame([["交易时间", "a"]], columns=["item", "item"]), "STRUCTURE_CHANGED"),
    ],
)
def test_failure_classes(frame, status):
    result = collect(frame)
    assert result.status == status
    assert result.verification == "UNVERIFIED"
    if len(frame) and list(frame.columns) == ["item", "value"]:
        assert len(result.fields) == len(frame)


def test_nonstandard_time_is_preserved_without_interpretation():
    result = collect(
        pd.DataFrame({"item": ["交易时间"], "value": ["暂定上午；详见公告"]}), source_version=None
    )
    assert result.status == "SUCCESS"
    assert result.fields[0].value == "暂定上午;详见公告"
    assert result.source_version is None
    assert result.verification == "UNVERIFIED"


@pytest.mark.parametrize(
    "exception,status",
    [
        (TimeoutError("private endpoint"), "FETCH_TIMEOUT"),
        (RuntimeError("private endpoint"), "FETCH_ERROR"),
        (ModuleNotFoundError("akshare"), "DEPENDENCY_UNAVAILABLE"),
    ],
)
def test_fetch_errors_do_not_expose_exception(exception, status):
    def fail(**kwargs):
        raise exception

    result = collect(fetch=fail)
    assert result.status == status
    assert "private endpoint" not in repr(result)
    assert result.fields == ()


@pytest.mark.parametrize(
    "override",
    [
        {"venue": "shfe"},
        {"venue": "SSE"},
        {"venue": None},
        {"contract": "../RB2701"},
        {"contract": "RB2701-C-100"},
        {"contract": "RB"},
        {"contract": "RB2701 "},
        {"requested_date": "2026-11-14"},
        {"requested_date": datetime.now(timezone.utc)},
        {"source_version": " "},
        {"fetch": None},
    ],
)
def test_invalid_requests_are_rejected_before_port(override):
    calls = []
    args = {"fetch": lambda **kw: calls.append(kw)}
    args.update(override)
    with pytest.raises(ValueError):
        collect(**args)
    assert calls == []


def test_source_date_is_preserved_without_becoming_request_or_effective_date():
    frame = pd.DataFrame({"item": ["交易时间", "日期"], "value": ["09:00-15:00", "2025-01-01"]})
    result = collect(frame)
    assert result.requested_date == date(2026, 11, 14)
    assert result.fields[1].value == "2025-01-01"
    assert result.verification == "UNVERIFIED"
    assert not hasattr(result, "effective_date")
