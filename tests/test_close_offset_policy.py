"""Venue close-offset rule shared with the Gateway strategy ledger (I63-E111)."""
import pytest

from bt_api_ctp import ctp_close_plan
from bt_api_ctp.close_offset_policy import (
    GENERIC_CLOSE_EXCHANGES,
    KNOWN_EXCHANGES,
    SPLIT_CLOSE_EXCHANGES,
    split_close_required,
)


def test_split_exchanges_match_the_close_planner_and_generic_ones_contain_its_set():
    assert SPLIT_CLOSE_EXCHANGES == ctp_close_plan._SPLIT_EXCHANGES == {"SHFE", "INE"}
    # The direct-path planner keeps its own generic set; D63-51 widened only this policy.
    assert ctp_close_plan._GENERIC_EXCHANGES == {"CZCE", "DCE"} < GENERIC_CLOSE_EXCHANGES
    assert GENERIC_CLOSE_EXCHANGES == {"CZCE", "DCE", "CFFEX", "GFEX"}
    assert KNOWN_EXCHANGES == SPLIT_CLOSE_EXCHANGES | GENERIC_CLOSE_EXCHANGES
    assert not SPLIT_CLOSE_EXCHANGES & GENERIC_CLOSE_EXCHANGES
    assert [split_close_required(name) for name in ("SHFE", "INE", "CZCE", "DCE")] == [True, True, False, False]


@pytest.mark.parametrize("exchange", ["CFFEX", "GFEX"])
def test_cffex_and_gfex_take_an_ordinary_close(exchange):
    """D63-51: same rule as CZCE and DCE (before: refused as CTP_CLOSE_RULE_UNKNOWN)."""
    assert split_close_required(exchange) is False


@pytest.mark.parametrize("exchange", ["SSE", "SZSE", "cffex", "shfe", "", None, 1])
def test_exchange_without_an_established_rule_is_refused(exchange):
    with pytest.raises(ValueError, match="CTP_CLOSE_RULE_UNKNOWN"):
        split_close_required(exchange)
