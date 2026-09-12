"""Independent capital arithmetic and rejection cases for the read-only example."""

import importlib.util
from pathlib import Path

import pytest


def _load():
    path = Path(__file__).parents[1] / "examples" / "screen_option_pairs.py"
    spec = importlib.util.spec_from_file_location("option_pair_screen", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inputs():
    future = {"instrument_id": "m2701", "exchange_id": "DCE", "multiplier": 10}
    option = {
        "instrument_id": "m2701-P-3000",
        "underlying_instrument": "m2701",
        "exchange_id": "DCE",
        "multiplier": 10,
        "option_type": "put",
    }
    fquote = {"BidPrice1": 2999, "AskPrice1": 3000, "BidVolume1": 3, "AskVolume1": 3}
    oquote = {"BidPrice1": 49, "AskPrice1": 50, "BidVolume1": 3, "AskVolume1": 3}
    margin = {
        "LongMarginRatioByMoney": 0.1,
        "LongMarginRatioByVolume": 0,
        "ShortMarginRatioByMoney": 0.1,
        "ShortMarginRatioByVolume": 0,
    }
    fee = {
        "OpenRatioByMoney": 0,
        "OpenRatioByVolume": 2,
        "CloseRatioByMoney": 0,
        "CloseRatioByVolume": 2,
        "CloseTodayRatioByMoney": 0,
        "CloseTodayRatioByVolume": 3,
    }
    return future, option, fquote, oquote, margin, fee, fee.copy()


def test_capital_uses_paid_premium_native_multipliers_and_round_trip_fees():
    screen = _load()
    row = screen.estimate_buy_option_pair(*_inputs(), option_lots=2, reserve=1000)
    # F margin=3000; premium=2*50*10=1000; round trip=3*(2+max(2,3))=15.
    assert row["capital_with_reserve"] == 5015
    assert row["budget_headroom"] == 4985
    assert row["option_lots"] == 2
    assert row["capital_fits"] is True
    assert row["economic_signal"] == "NOT_EVALUATED"
    assert row["delta"] is None


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -1])
def test_missing_or_invalid_account_margin_is_unknown_not_free(bad):
    args = list(_inputs())
    args[4]["LongMarginRatioByMoney"] = bad
    with pytest.raises(ValueError):
        _load().estimate_buy_option_pair(*args)


def test_rejects_incomplete_fees_mismatched_underlying_and_sentinel_quotes():
    screen = _load()
    args = list(_inputs())
    del args[5]["CloseTodayRatioByVolume"]
    with pytest.raises(ValueError):
        screen.estimate_buy_option_pair(*args)
    args = list(_inputs())
    args[1]["underlying_instrument"] = "m2705"
    with pytest.raises(ValueError):
        screen.estimate_buy_option_pair(*args)
    args = list(_inputs())
    args[3]["AskPrice1"] = 1.7976931348623157e308
    with pytest.raises(ValueError):
        screen.estimate_buy_option_pair(*args)


def test_call_hedge_uses_short_margin_and_only_observed_depth():
    args = list(_inputs())
    args[1]["option_type"] = "call"
    args[4]["ShortMarginRatioByMoney"] = 0.2
    row = _load().estimate_buy_option_pair(*args)
    assert row["future_side"] == "sell"
    assert row["future_margin"] == 6000
    args[3]["AskVolume1"] = 1
    with pytest.raises(ValueError):
        _load().estimate_buy_option_pair(*args, option_lots=2)
