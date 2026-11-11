"""Scalar fixtures only; no native callback, network or account evidence."""

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from bt_api_ctp.calendar_instrument_status import project_instrument_status


def fields():
    return {
        "ExchangeID": "SHFE",
        "ExchangeInstID": "rb2701",
        "InstrumentID": "rb2701",
        "InstrumentStatus": "2",
        "TradingSegmentSN": 2,
        "EnterTime": "09:00:00",
        "EnterReason": "1",
    }


def assert_unverified(result):
    assert result.verification == "UNVERIFIED" and result.authorization == "NOT_AUTHORIZED"
    assert (
        result.scope,
        result.trading_day,
        result.timestamp,
        result.source,
        result.session,
        result.order,
        result.current_validity,
    ) == ("UNKNOWN",) * 7


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0", "BEFORE_TRADING"),
        ("1", "NO_TRADING"),
        ("2", "CONTINUOUS"),
        ("3", "AUCTION_ORDERING"),
        ("4", "AUCTION_BALANCE"),
        ("5", "AUCTION_MATCH"),
        ("6", "CLOSED"),
        ("7", "TRANSACTION_PROCESSING"),
    ],
)
def test_native_statuses_are_distinct(raw, expected):
    data = fields()
    data["InstrumentStatus"] = raw
    before = deepcopy(data)
    result = project_instrument_status(data)
    assert (result.status, result.instrument_status) == ("CANDIDATE", expected)
    assert (result.exchange_id, result.exchange_instrument_id, result.instrument_id) == (
        "SHFE",
        "rb2701",
        "rb2701",
    )
    assert (result.trading_segment_sn, result.enter_time, result.enter_reason) == (
        2,
        "09:00:00",
        "AUTOMATIC",
    )
    assert data == before
    assert_unverified(result)


@pytest.mark.parametrize("raw,expected", [("1", "AUTOMATIC"), ("2", "MANUAL"), ("3", "FUSE")])
def test_reason_is_independent_of_status(raw, expected):
    result = project_instrument_status({**fields(), "EnterReason": raw, "InstrumentStatus": "2"})
    assert (result.status, result.enter_reason, result.instrument_status) == (
        "CANDIDATE",
        expected,
        "CONTINUOUS",
    )
    assert_unverified(result)


@pytest.mark.parametrize("key", list(fields()))
@pytest.mark.parametrize("missing_kind", ["absent", "none", "empty"])
def test_missing_scalar_is_reported_as_incomplete(key, missing_kind):
    data = fields()
    if missing_kind == "absent":
        del data[key]
    else:
        data[key] = None if missing_kind == "none" else ""
    result = project_instrument_status(data)
    assert (result.status, result.missing_fields, result.invalid_fields) == (
        "INCOMPLETE",
        (key,),
        (),
    )
    assert_unverified(result)


@pytest.mark.parametrize(
    "key,value",
    [
        ("InstrumentStatus", "8"),
        ("InstrumentStatus", 2),
        ("InstrumentStatus", b"2"),
        ("EnterReason", "0"),
        ("EnterReason", "4"),
        ("EnterReason", True),
        ("ExchangeID", "x" * 9),
        ("ExchangeID", "S HFE"),
        ("ExchangeID", "SHFE\x00"),
        ("ExchangeInstID", "x" * 81),
        ("InstrumentID", "x" * 81),
        ("InstrumentID", "中"),
        ("InstrumentID", ["rb2701"]),
        ("TradingSegmentSN", -1),
        ("TradingSegmentSN", 2**31),
        ("TradingSegmentSN", True),
        ("TradingSegmentSN", 1.0),
        ("EnterTime", "24:00:00"),
        ("EnterTime", "23:60:00"),
        ("EnterTime", "23:59:60"),
        ("EnterTime", "9:00:00"),
        ("EnterTime", "2026-01-01 09:00:00"),
    ],
)
def test_invalid_or_unknown_values_are_explicitly_unknown(key, value):
    result = project_instrument_status({**fields(), key: value})
    assert (result.status, result.invalid_fields) == ("UNKNOWN", (key,))
    assert_unverified(result)


def test_unknown_takes_precedence_over_incomplete_and_retains_both_diagnostics():
    data = fields()
    del data["ExchangeID"]
    data["InstrumentStatus"] = "8"
    result = project_instrument_status(data)
    assert result.status == "UNKNOWN"
    assert result.missing_fields == ("ExchangeID",) and result.invalid_fields == (
        "InstrumentStatus",
    )
    assert result.instrument_status == "UNKNOWN"


def test_native_width_and_time_boundaries():
    data = fields()
    data.update(
        ExchangeID="E" * 8,
        ExchangeInstID="x" * 80,
        InstrumentID="y" * 80,
        TradingSegmentSN=0,
        EnterTime="00:00:00",
    )
    assert project_instrument_status(data).status == "CANDIDATE"
    data.update(TradingSegmentSN=2**31 - 1, EnterTime="23:59:59")
    result = project_instrument_status(data)
    assert result.status == "CANDIDATE" and result.trading_segment_sn == 2**31 - 1
    assert result.enter_time == "23:59:59"


def test_extra_native_fields_ignored_but_unknown_fields_and_input_types_rejected():
    data = {**fields(), "reserve1": b"opaque", "SettlementGroupID": "group", "reserve2": object()}
    before = dict(data)
    result = project_instrument_status(data)
    assert result.status == "CANDIDATE" and data == before
    assert project_instrument_status(
        {**fields(), "unknown-secret-field": "secret"}
    ).invalid_fields == ("INPUT_FIELDS",)
    assert project_instrument_status({1: "secret"}).invalid_fields == ("INPUT",)
    assert project_instrument_status(None).invalid_fields == ("INPUT",)
    assert "secret" not in repr(project_instrument_status({**fields(), "unexpected": "secret"}))


def test_exceptional_scalar_is_not_coerced_or_compared():
    class Hazard:
        def __eq__(self, other):
            raise AssertionError("private exception")

    result = project_instrument_status({**fields(), "InstrumentStatus": Hazard()})
    assert result.status == "UNKNOWN" and result.invalid_fields == ("InstrumentStatus",)
    assert "private exception" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.instrument_status = "CONTINUOUS"
