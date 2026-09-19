"""离线契约测试：六所合约命名规则与真实落盘清单校准。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bt_api_ctp.cleaner_ctp.symbols import (
    KIND_COMBINATION,
    KIND_FUTURE,
    KIND_OPTION,
    KIND_UNKNOWN,
    classify_contract,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
TICK_ROOT = REPO_ROOT / "ctp_data"


class TestFutures:
    @pytest.mark.parametrize(
        ("exchange", "instrument", "symbol"),
        [
            ("SHFE", "rb2510", "rb"),
            ("SHFE", "ad2610", "ad"),
            ("INE", "sc2610", "sc"),
            ("INE", "bc2610", "bc"),
            ("DCE", "a2611", "a"),
            ("DCE", "bz2611", "bz"),
            ("CZCE", "AP610", "AP"),
            ("CZCE", "CF701", "CF"),
            ("GFEX", "lc2610", "lc"),
            ("CFFEX", "IF2609", "IF"),
            ("CFFEX", "T2609", "T"),
            ("CFFEX", "TF2609", "TF"),
        ],
    )
    def test_future_symbol_is_the_letter_prefix(self, exchange, instrument, symbol):
        result = classify_contract(exchange, instrument)

        assert result.kind == KIND_FUTURE
        assert result.symbol == symbol

    def test_future_is_a_bar_source(self):
        assert classify_contract("SHFE", "rb2510").is_bar_source is True


class TestOptions:
    @pytest.mark.parametrize(
        ("exchange", "instrument", "symbol"),
        [
            # SHFE / INE: 月份 + C/P + 行权价，无连字符
            ("SHFE", "ad2610C20000", "ad"),
            ("INE", "bc2610C100000", "bc"),
            ("INE", "sc2610P600", "sc"),
            ("SHFE", "rb2510P3200", "rb"),
            # CZCE: 3 位月份 + C/P + 行权价
            ("CZCE", "FG611C1000", "FG"),
            ("CZCE", "SR509C5000", "SR"),
            ("CZCE", "AP2601P8000", "AP"),
            # DCE / GFEX: 月份 + -C-/-P- + 行权价
            ("DCE", "m2509-C-3000", "m"),
            ("DCE", "bz2611-P-5200", "bz"),
            ("GFEX", "lc2611-C-100000", "lc"),
            # CFFEX: 股指/国债期权
            ("CFFEX", "HO2609-C-2500", "HO"),
            ("CFFEX", "IO2609-P-4000", "IO"),
        ],
    )
    def test_option_symbol_is_the_underlying_product(self, exchange, instrument, symbol):
        result = classify_contract(exchange, instrument)

        assert result.kind == KIND_OPTION
        assert result.symbol == symbol

    def test_option_is_a_bar_source(self):
        assert classify_contract("DCE", "m2509-C-3000").is_bar_source is True

    def test_decimal_strike_is_supported(self):
        result = classify_contract("CFFEX", "IO2609-C-4000.5")

        assert result.kind == KIND_OPTION
        assert result.symbol == "IO"


class TestCombinations:
    """组合套利合约既不是期货也不是期权，不进 K 线（迭代06 明确范围）。"""

    @pytest.mark.parametrize(
        "instrument",
        [
            "RM701MSC2100",
            "RM701MSP2500",
            "SR701MSC4900",
            "c2701-MS-C-2000",
            "c2701-MS-P-2540",
        ],
    )
    def test_combination_is_recognised_but_not_a_bar_source(self, instrument):
        result = classify_contract("CZCE", instrument)

        assert result.kind == KIND_COMBINATION
        assert result.symbol is None
        assert result.is_bar_source is False

    def test_czce_option_is_not_mistaken_for_a_combination(self):
        """FG611C1000 只有一个 C 标记；组合需要额外的字母块。"""
        assert classify_contract("CZCE", "FG611C1000").kind == KIND_OPTION

    def test_dce_future_is_not_mistaken_for_a_combination(self):
        assert classify_contract("DCE", "c2611").kind == KIND_FUTURE


class TestUnknown:
    @pytest.mark.parametrize("instrument", ["", "rb", "2510", "rb2510C", "rb-X-Y", "ABC12345"])
    def test_unrecognised_names_are_unknown(self, instrument):
        result = classify_contract("SHFE", instrument)

        assert result.kind == KIND_UNKNOWN
        assert result.symbol is None
        assert result.is_bar_source is False


def _real_instrument_names() -> list[tuple[str, str]]:
    if not TICK_ROOT.is_dir():
        return []
    names: list[tuple[str, str]] = []
    for day_dir in sorted(path for path in TICK_ROOT.iterdir() if path.is_dir()):
        if not day_dir.name.isdigit():
            continue
        for exchange_dir in sorted(path for path in day_dir.iterdir() if path.is_dir()):
            for parquet in exchange_dir.glob("*.parquet"):
                names.append((exchange_dir.name, parquet.stem))
    return names


class TestRealDataCalibration:
    """真实清单校准：迭代06 硬门槛——识别率必须 100%。"""

    def test_every_real_instrument_is_classified(self):
        names = _real_instrument_names()
        if not names:
            pytest.skip("ctp_data 无真实落盘数据，跳过校准")

        unknown = [
            f"{exchange}/{instrument}"
            for exchange, instrument in names
            if classify_contract(exchange, instrument).kind == KIND_UNKNOWN
        ]

        assert unknown == [], f"{len(unknown)} 个合约无法识别: {unknown[:10]}"

    def test_real_data_contains_all_three_kinds(self):
        names = _real_instrument_names()
        if not names:
            pytest.skip("ctp_data 无真实落盘数据，跳过校准")

        kinds = {classify_contract(exchange, name).kind for exchange, name in names}

        assert {KIND_FUTURE, KIND_OPTION, KIND_COMBINATION} <= kinds
