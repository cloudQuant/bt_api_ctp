"""Venue close-offset rule shared with the Gateway strategy ledger (I63-E111)."""
import pytest

from bt_api_ctp import ctp_close_plan
from bt_api_ctp.close_offset_policy import (
    GENERIC_CLOSE_EXCHANGES,
    KNOWN_EXCHANGES,
    SPLIT_CLOSE_EXCHANGES,
    split_close_required,
)


def test_split_and_generic_exchanges_match_the_close_planner():
    assert SPLIT_CLOSE_EXCHANGES == ctp_close_plan._SPLIT_EXCHANGES == {"SHFE", "INE"}
    assert GENERIC_CLOSE_EXCHANGES == ctp_close_plan._GENERIC_EXCHANGES == {"CZCE", "DCE"}
    assert KNOWN_EXCHANGES == ctp_close_plan._KNOWN_EXCHANGES
    assert [split_close_required(name) for name in ("SHFE", "INE", "CZCE", "DCE")] == [True, True, False, False]


@pytest.mark.parametrize("exchange", ["CFFEX", "GFEX", "shfe", "", None, 1])
def test_exchange_without_an_established_rule_is_refused(exchange):
    with pytest.raises(ValueError, match="CTP_CLOSE_RULE_UNKNOWN"):
        split_close_required(exchange)
