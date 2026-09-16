"""离线契约测试：分片策略与多机分片校验。"""

from __future__ import annotations

import pytest

from bt_api_ctp.collector.protocols import InstrumentSpec
from bt_api_ctp.collector.shard import (
    ShardConfig,
    select_instruments,
    validate_shards,
)


def _specs():
    return [
        InstrumentSpec("rb2510", "SHFE", "future"),
        InstrumentSpec("ru2509-C-14000", "SHFE", "option"),
        InstrumentSpec("m2701", "DCE", "future"),
        InstrumentSpec("m2701-C-3000", "DCE", "option"),
        InstrumentSpec("SR509", "CZCE", "future"),
        InstrumentSpec("IF2509", "CFFEX", "future"),
        InstrumentSpec("sc2510", "INE", "future"),
        InstrumentSpec("si2511", "GFEX", "future"),
    ]


class TestSelectInstruments:
    def test_by_exchange(self):
        config = ShardConfig(strategy="by_exchange", exchanges=("SHFE", "INE"))
        selected = select_instruments(_specs(), config)
        assert {spec.exchange_id for spec in selected} == {"SHFE", "INE"}
        assert len(selected) == 3

    def test_by_exchange_can_cover_all_six(self):
        config = ShardConfig(
            strategy="by_exchange",
            exchanges=("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"),
        )
        assert len(select_instruments(_specs(), config)) == len(_specs())

    def test_by_prefix_is_case_insensitive(self):
        config = ShardConfig(strategy="by_prefix", prefixes=("RB", "m"))
        selected = select_instruments(_specs(), config)
        # "RB" 应匹配小写 rb2510，"m" 应匹配 m2701 与 m2701-C-3000
        assert {spec.instrument_id for spec in selected} == {
            "rb2510",
            "m2701",
            "m2701-C-3000",
        }

    def test_hash_mod_is_a_stable_partition(self):
        instruments = [
            InstrumentSpec(f"i{index}", "SHFE", "future") for index in range(100)
        ]
        configs = [
            ShardConfig(strategy="hash_mod", shard_id=shard, total_shards=4)
            for shard in range(4)
        ]
        parts = [
            {spec.instrument_id for spec in select_instruments(instruments, config)}
            for config in configs
        ]
        assert sum(len(part) for part in parts) == 100  # 无重叠
        assert set().union(*parts) == {spec.instrument_id for spec in instruments}  # 并集完整

    def test_hash_mod_specific_assignment_is_deterministic(self):
        import zlib

        config = ShardConfig(strategy="hash_mod", shard_id=0, total_shards=3)
        selected = select_instruments(_specs(), config)
        expected = {
            spec.instrument_id
            for spec in _specs()
            if zlib.crc32(spec.instrument_id.encode("utf-8")) % 3 == 0
        }
        assert {spec.instrument_id for spec in selected} == expected

    def test_unknown_strategy_rejected(self):
        with pytest.raises(ValueError):
            select_instruments(_specs(), ShardConfig(strategy="bogus"))

    def test_invalid_hash_config_rejected(self):
        with pytest.raises(ValueError):
            select_instruments(
                _specs(), ShardConfig(strategy="hash_mod", shard_id=3, total_shards=3)
            )


class TestValidateShards:
    def test_clean_partition_is_ok(self):
        configs = [
            ShardConfig(strategy="by_exchange", exchanges=("SHFE", "INE")),
            ShardConfig(strategy="by_exchange", exchanges=("DCE", "CZCE", "CFFEX", "GFEX")),
        ]
        report = validate_shards(configs, _specs())
        assert report.ok is True
        assert report.overlaps == []
        assert report.missing == []

    def test_overlap_is_detected(self):
        configs = [
            ShardConfig(strategy="by_exchange", exchanges=("SHFE",)),
            ShardConfig(strategy="by_exchange", exchanges=("SHFE", "DCE")),
        ]
        report = validate_shards(configs, _specs())
        assert report.ok is False
        assert "rb2510" in report.overlaps

    def test_missing_is_detected(self):
        configs = [ShardConfig(strategy="by_exchange", exchanges=("SHFE",))]
        report = validate_shards(configs, _specs())
        assert report.ok is False
        assert "m2701" in report.missing
        assert "rb2510" not in report.missing
