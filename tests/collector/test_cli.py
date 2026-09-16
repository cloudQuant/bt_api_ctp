"""离线契约测试：采集 CLI（配置加载、校验、日历与分片子命令）。"""

from __future__ import annotations

import pytest
import yaml

from bt_api_ctp.collector.cli import (
    EXIT_CONFIG_ERROR,
    EXIT_NOT_TRADING_DAY,
    EXIT_OK,
    EXIT_SHARD_MISMATCH,
    build_collection_config,
    main,
    validate_config,
)


def _write_config(tmp_path, payload):
    path = tmp_path / "collector.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


class TestValidateConfig:
    def test_minimal_config_is_valid(self):
        assert validate_config({"data_root": "/tmp/ticks"}) == []

    def test_missing_data_root_is_flagged(self):
        errors = validate_config({})
        assert any("data_root" in error for error in errors)

    def test_unknown_shard_strategy_is_flagged(self):
        errors = validate_config({"data_root": "/tmp/x", "shard": {"strategy": "bogus"}})
        assert any("strategy" in error for error in errors)

    def test_bad_hash_shard_is_flagged(self):
        errors = validate_config(
            {
                "data_root": "/tmp/x",
                "shard": {"strategy": "hash_mod", "shard_id": 5, "total_shards": 3},
            }
        )
        assert errors


class TestBuildCollectionConfig:
    def test_maps_buffer_sink_and_shard(self, tmp_path):
        config = {
            "data_root": str(tmp_path),
            "shard": {"strategy": "hash_mod", "shard_id": 1, "total_shards": 3},
            "buffer": {
                "per_instrument_cap": 5,
                "flush_interval_sec": 0.5,
                "overflow_policy": "flush",
            },
            "sink": {"merge_existing": False, "gap_threshold_sec": 30},
            "asset_types": ["future"],
        }

        collection_config = build_collection_config(config)

        assert collection_config.shard.strategy == "hash_mod"
        assert collection_config.shard.shard_id == 1
        assert collection_config.shard.total_shards == 3
        assert collection_config.per_instrument_cap == 5
        assert collection_config.flush_interval_sec == 0.5
        assert collection_config.overflow_policy == "flush"
        assert collection_config.merge_existing is False
        assert collection_config.gap_threshold_sec == 30.0
        assert collection_config.asset_types == ("future",)

    def test_defaults_follow_documented_values(self, tmp_path):
        collection_config = build_collection_config({"data_root": str(tmp_path)})
        assert collection_config.shard.strategy == "by_exchange"
        assert collection_config.per_instrument_cap == 100_000
        assert collection_config.flush_interval_sec == 5.0
        assert collection_config.merge_existing is True
        assert collection_config.asset_types == ("future", "option", "spot_option")


class TestMainConfigErrors:
    def test_help_exits_zero(self):
        import pytest

        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])
        assert excinfo.value.code == 0

    def test_missing_config_file(self, tmp_path, capsys):
        assert main(["--config", str(tmp_path / "nope.yaml")]) == EXIT_CONFIG_ERROR
        assert capsys.readouterr().err

    def test_missing_data_root(self, tmp_path, capsys):
        path = _write_config(tmp_path, {"shard": {"strategy": "by_exchange"}})
        assert main(["--config", str(path)]) == EXIT_CONFIG_ERROR
        assert "data_root" in capsys.readouterr().err


class TestMainCalendar:
    def test_trading_day_reports_night_session(self, tmp_path, capsys):
        path = _write_config(tmp_path, {"data_root": str(tmp_path / "data")})

        code = main(["--config", str(path), "--check-calendar", "--date", "20260916"])

        out = capsys.readouterr().out
        assert code == EXIT_OK
        assert "20260916" in out
        assert "night_session: True" in out

    def test_friday_evening_carries_the_next_monday_night_session(self, tmp_path, capsys):
        path = _write_config(tmp_path, {"data_root": str(tmp_path / "data")})

        code = main(["--config", str(path), "--check-calendar", "--date", "20260918"])

        out = capsys.readouterr().out
        assert code == EXIT_OK
        # 周五晚的夜盘属于下周一交易日
        assert "night_session: True" in out

    def test_sunday_evening_has_no_night_session(self, tmp_path, capsys):
        path = _write_config(tmp_path, {"data_root": str(tmp_path / "data")})

        code = main(["--config", str(path), "--check-calendar", "--date", "20260920"])

        out = capsys.readouterr().out
        assert code == EXIT_NOT_TRADING_DAY
        assert "night_session: False" in out

    def test_weekend_is_not_a_trading_day(self, tmp_path, capsys):
        path = _write_config(tmp_path, {"data_root": str(tmp_path / "data")})

        code = main(["--config", str(path), "--check-calendar", "--date", "20260919"])

        assert code == EXIT_NOT_TRADING_DAY


class TestMainShardValidation:
    def test_clean_partition_passes(self, tmp_path, capsys):
        path = _write_config(
            tmp_path,
            {
                "data_root": str(tmp_path),
                "shard_all": [
                    {"strategy": "by_exchange", "exchanges": ["SHFE", "DCE", "CZCE"]},
                    {"strategy": "by_exchange", "exchanges": ["CFFEX", "INE", "GFEX"]},
                ],
            },
        )

        code = main(["--config", str(path), "--validate-shards"])

        assert code == EXIT_OK
        assert "ok: True" in capsys.readouterr().out

    def test_overlap_is_detected(self, tmp_path, capsys):
        path = _write_config(
            tmp_path,
            {
                "data_root": str(tmp_path),
                "shard_all": [
                    {"strategy": "by_exchange", "exchanges": ["SHFE"]},
                    {"strategy": "by_exchange", "exchanges": ["SHFE", "DCE"]},
                ],
            },
        )

        code = main(["--config", str(path), "--validate-shards"])

        out = capsys.readouterr().out
        assert code == EXIT_SHARD_MISMATCH
        assert "SHFE" in out

    def test_missing_exchange_is_detected(self, tmp_path, capsys):
        path = _write_config(
            tmp_path,
            {
                "data_root": str(tmp_path),
                "shard_all": [{"strategy": "by_exchange", "exchanges": ["SHFE"]}],
            },
        )

        code = main(["--config", str(path), "--validate-shards"])

        out = capsys.readouterr().out
        assert code == EXIT_SHARD_MISMATCH
        assert "missing" in out


class TestUntilCloseMode:
    """无人值守时由调度器在开盘前触发，脚本应自动跑到本时段收盘。"""

    def test_until_close_adds_grace_to_remaining_session(self):
        from datetime import datetime

        from bt_api_ctp.collector.cli import resolve_duration

        # 14:00 距下午收盘 15:15 还有 4500 秒
        duration = resolve_duration(
            duration=None, until_close=True, now=datetime(2026, 9, 16, 14, 0, 0), grace_sec=300.0
        )
        assert duration == pytest.approx(4800.0)

    def test_until_close_handles_night_session(self):
        from datetime import datetime

        from bt_api_ctp.collector.cli import resolve_duration

        # 23:00 距夜盘收盘 02:30 还有 3.5 小时
        duration = resolve_duration(
            duration=None, until_close=True, now=datetime(2026, 9, 16, 23, 0, 0), grace_sec=0.0
        )
        assert duration == pytest.approx(12600.0)

    def test_until_close_outside_session_raises(self):
        from datetime import datetime

        from bt_api_ctp.collector.cli import SessionClosedError, resolve_duration

        with pytest.raises(SessionClosedError):
            resolve_duration(
                duration=None, until_close=True, now=datetime(2026, 9, 16, 12, 0, 0)
            )

    def test_explicit_duration_is_returned_unchanged(self):
        from bt_api_ctp.collector.cli import resolve_duration

        assert resolve_duration(duration=60.0, until_close=False) == 60.0

    def test_until_close_conflicts_with_duration(self, tmp_path, capsys):
        path = _write_config(tmp_path, {"data_root": str(tmp_path / "data")})
        code = main(["--config", str(path), "--until-close", "--duration", "60"])
        assert code == EXIT_CONFIG_ERROR
        assert "until-close" in capsys.readouterr().err

    def test_until_close_outside_session_exit_code(self, tmp_path, capsys):
        path = _write_config(tmp_path, {"data_root": str(tmp_path / "data")})
        # 12:00 为午休，不在任何交易时段
        code = main(
            ["--config", str(path), "--until-close", "--now", "20260916T12:00:00"]
        )
        assert code == EXIT_NOT_TRADING_DAY

    def test_wait_open_covers_wait_plus_full_session(self):
        from datetime import datetime

        from bt_api_ctp.collector.cli import resolve_duration

        # 08:45 启动：等 900 秒到 09:00，再加白盘组 22500 秒（09:00-15:15）
        duration = resolve_duration(
            duration=None,
            until_close=True,
            now=datetime(2026, 9, 16, 8, 45, 0),
            grace_sec=0.0,
            wait_for_open=True,
        )
        assert duration == pytest.approx(900.0 + 22500.0)

    def test_wait_open_before_night_session(self):
        from datetime import datetime

        from bt_api_ctp.collector.cli import resolve_duration

        # 20:45 启动：等 900 秒到 21:00，再加夜盘组 19800 秒（21:00-02:30）
        duration = resolve_duration(
            duration=None,
            until_close=True,
            now=datetime(2026, 9, 16, 20, 45, 0),
            grace_sec=0.0,
            wait_for_open=True,
        )
        assert duration == pytest.approx(900.0 + 19800.0)

    def test_wait_open_still_raises_without_it(self):
        from datetime import datetime

        from bt_api_ctp.collector.cli import SessionClosedError, resolve_duration

        with pytest.raises(SessionClosedError):
            resolve_duration(
                duration=None,
                until_close=True,
                now=datetime(2026, 9, 16, 8, 45, 0),
                wait_for_open=False,
            )


class TestNightMode:
    """夜盘调度：周日晚无夜盘必须直接退出，不能白白连接柜台。"""

    def test_night_mode_exits_on_sunday(self, tmp_path, capsys):
        path = _write_config(tmp_path, {"data_root": str(tmp_path / "data")})

        code = main(["--config", str(path), "--night", "--date", "20260920"])

        assert code == EXIT_NOT_TRADING_DAY
        assert "no night session" in capsys.readouterr().err

    def test_night_mode_exits_on_saturday(self, tmp_path, capsys):
        path = _write_config(tmp_path, {"data_root": str(tmp_path / "data")})

        code = main(["--config", str(path), "--night", "--date", "20260919"])

        assert code == EXIT_NOT_TRADING_DAY
