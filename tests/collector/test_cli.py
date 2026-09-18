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

    def test_non_positive_batch_size_is_flagged(self):
        """契约要求重连重订阅分批，批大小必须 >= 1（整改方案 P1-3）。"""
        errors = validate_config({"data_root": "/tmp/x", "subscription": {"batch_size": 0}})
        assert any("batch_size" in error for error in errors)

    def test_zero_batch_interval_is_flagged(self):
        """契约要求批间限速不得为 0（整改方案 P1-3）。"""
        errors = validate_config(
            {"data_root": "/tmp/x", "subscription": {"batch_interval_sec": 0}}
        )
        assert any("batch_interval_sec" in error for error in errors)

    def test_default_subscription_settings_are_valid(self):
        assert validate_config({"data_root": "/tmp/x", "subscription": {}}) == []

    def test_non_numeric_subscription_settings_are_flagged(self):
        """非法类型必须走配置错误分支，不得直接把异常抛给调用方。"""
        errors = validate_config({"data_root": "/tmp/x", "subscription": {"batch_size": "abc"}})
        assert any("batch_size" in error for error in errors)

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

    def test_logging_milestones_are_mapped(self, tmp_path):
        collection_config = build_collection_config(
            {
                "data_root": str(tmp_path),
                "logging": {"tick_interval": 500, "tick_mode": "every"},
            }
        )

        assert collection_config.tick_log_interval == 500
        assert collection_config.tick_log_mode == "every"

    def test_logging_milestone_defaults(self, tmp_path):
        collection_config = build_collection_config({"data_root": str(tmp_path)})

        assert collection_config.tick_log_interval == 1000
        assert collection_config.tick_log_mode == "first"


class TestDataQualityConfig:
    """健康守卫与数据质量参数必须能从 YAML 配置贯通（整改方案 P0-5 / P2）。"""

    def test_reads_health_thresholds(self):
        config = build_collection_config(
            {
                "data_root": "/tmp/x",
                "health": {"stall_intervals": 5, "silent_after_sec": 120},
            }
        )

        assert config.health.stall_intervals == 5
        assert config.health.silent_after_sec == 120
        assert config.health.silent_share_alarm == 0.8  # 未提供的沿用默认
        assert config.health_check_enabled is True

    def test_health_can_be_disabled(self):
        config = build_collection_config({"data_root": "/tmp/x", "health": {"enabled": False}})

        assert config.health_check_enabled is False

    def test_reads_sink_quality_settings(self):
        config = build_collection_config(
            {
                "data_root": "/tmp/x",
                "sink": {"gap_threshold_factor": 0, "drop_outside_session": False},
            }
        )

        assert config.gap_threshold_factor == 0.0
        assert config.drop_outside_session is False

    def test_unknown_health_setting_is_flagged(self):
        errors = validate_config({"data_root": "/tmp/x", "health": {"bogus": 1}})

        assert any("health" in error for error in errors)

    def test_non_numeric_health_threshold_is_flagged(self):
        errors = validate_config({"data_root": "/tmp/x", "health": {"stall_intervals": "abc"}})

        assert any("numeric" in error for error in errors)

    def test_out_of_range_health_share_is_flagged(self):
        errors = validate_config({"data_root": "/tmp/x", "health": {"silent_share_alarm": 1.5}})

        assert any("silent_share_alarm" in error for error in errors)

    def test_health_requires_a_heartbeat(self):
        """心跳关掉时守卫永远不会评估，必须当成配置错误而不是静默失效。"""
        errors = validate_config(
            {
                "data_root": "/tmp/x",
                "health": {"enabled": True},
                "buffer": {"heartbeat_interval_sec": 0},
            }
        )

        assert any("heartbeat" in error for error in errors)

    def test_calendar_is_passed_to_the_collection_config(self):
        config = build_collection_config(
            {"data_root": "/tmp/x", "calendar": {"holidays_file": ""}}
        )

        assert config.calendar is not None


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


class TestLoggingSetup:
    """engine 的进度日志全是 INFO；CLI 不配置 logging 就会全程静默。"""

    def test_default_level_is_info(self, monkeypatch):
        from bt_api_ctp.collector import cli

        captured = {}
        monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: captured.update(kwargs))

        cli._configure_logging(verbosity=0, quiet=False)

        assert captured["level"] == cli.logging.INFO

    def test_verbose_raises_level_to_debug(self, monkeypatch):
        from bt_api_ctp.collector import cli

        captured = {}
        monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: captured.update(kwargs))

        cli._configure_logging(verbosity=1, quiet=False)

        assert captured["level"] == cli.logging.DEBUG

    def test_quiet_lowers_level_to_warning(self, monkeypatch):
        from bt_api_ctp.collector import cli

        captured = {}
        monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: captured.update(kwargs))

        cli._configure_logging(verbosity=0, quiet=True)

        assert captured["level"] == cli.logging.WARNING

    def test_verbose_flag_is_counted(self):
        from bt_api_ctp.collector import cli

        assert cli._parse_args(["--config", "c.yaml", "-vv"]).verbosity == 2

    def test_quiet_flag_parses(self):
        from bt_api_ctp.collector import cli

        assert cli._parse_args(["--config", "c.yaml", "--quiet"]).quiet is True

    def test_log_file_handler_is_added(self, tmp_path, monkeypatch):
        from bt_api_ctp.collector import cli

        captured = {}
        monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: captured.update(kwargs))

        log_file = tmp_path / "logs" / "collector-20260917.log"
        cli._configure_logging(verbosity=0, quiet=False, log_file=log_file)

        assert log_file.parent.is_dir(), "日志目录应自动创建"
        assert len(captured["handlers"]) == 2, "应同时输出终端与文件"

    def test_no_file_handler_without_log_file(self, monkeypatch):
        from bt_api_ctp.collector import cli

        captured = {}
        monkeypatch.setattr(cli.logging, "basicConfig", lambda **kwargs: captured.update(kwargs))

        cli._configure_logging(verbosity=0, quiet=False)

        assert len(captured["handlers"]) == 1

    def test_main_configures_logging(self, tmp_path, monkeypatch):
        from bt_api_ctp.collector import cli

        path = _write_config(tmp_path, {"data_root": str(tmp_path / "data")})
        calls = []
        monkeypatch.setattr(cli, "_configure_logging", lambda **kwargs: calls.append(kwargs))

        # 12:00 是午休：能走到日志配置，然后以"非交易时段"退出
        cli.main(["--config", str(path), "--until-close", "--now", "20260916T12:00:00"])

        assert calls, "main() 必须先配置 logging，否则采集进度不可见"


class TestBuildEngineLogging:
    """建立柜台会话必须留痕：能看到用了哪个前置、哪个账号。"""

    def test_trader_login_is_logged(self, tmp_path, monkeypatch, caplog):
        import logging

        from bt_api_ctp.collector import cli

        class _FakeTrader:
            def __init__(self, *_args, **_kwargs):
                pass

            def start(self, block: bool = True):
                return None

            def wait_ready(self, timeout: float = 30):
                return True

            def stop(self):
                return None

        class _FakeMd:
            def __init__(self, *_args, **_kwargs):
                pass

        class _FakeSubscriber:
            def __init__(self, *_args, **_kwargs):
                pass

        class _FakeProvider:
            def __init__(self, *_args, **_kwargs):
                pass

        monkeypatch.setattr("bt_api_ctp.ctp.client.TraderClient", _FakeTrader)
        monkeypatch.setattr("bt_api_ctp.ctp.client.MdClient", _FakeMd)
        monkeypatch.setattr("bt_api_ctp.collector_ctp.subscriber.CtpMdSubscriber", _FakeSubscriber)
        monkeypatch.setattr(
            "bt_api_ctp.collector_ctp.instrument_provider.CtpInstrumentProvider", _FakeProvider
        )

        config = {
            "data_root": str(tmp_path),
            "ctp": {
                "md_front": "tcp://md.example:1",
                "td_front": "tcp://td.example:1",
                "broker_id": "9999",
                "user_id": "u1",
                "password": "p1",
            },
        }

        with caplog.at_level(logging.INFO):
            cli.build_engine(config)

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "trader" in messages
        assert "login" in messages
        assert "tcp://td.example:1" in messages

    def test_trader_login_timeout_is_logged(self, tmp_path, monkeypatch, caplog):
        import logging

        from bt_api_ctp.collector import cli

        class _FakeTrader:
            def __init__(self, *_args, **_kwargs):
                pass

            def start(self, block: bool = True):
                return None

            def wait_ready(self, timeout: float = 30):
                return False

            def stop(self):
                return None

        monkeypatch.setattr("bt_api_ctp.ctp.client.TraderClient", _FakeTrader)

        config = {
            "data_root": str(tmp_path),
            "ctp": {"md_front": "tcp://md:1", "td_front": "tcp://td:1"},
        }

        with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
            cli.build_engine(config)

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "timeout" in messages


class TestResolveLogFile:
    """日志文件按交易日命名：夜盘归属下一个交易日。"""

    def test_night_session_uses_next_trading_day(self, tmp_path):
        from datetime import datetime

        from bt_api_ctp.collector.cli import _resolve_log_file
        from bt_api_ctp.collector.schedule import TradingCalendar

        path = _resolve_log_file(
            {"data_root": str(tmp_path)},
            "20260916",
            TradingCalendar(),
            night=False,
            moment=datetime(2026, 9, 16, 21, 30),
        )

        assert path == tmp_path / "logs" / "collector-20260917.log"

    def test_day_session_uses_the_same_day(self, tmp_path):
        from datetime import datetime

        from bt_api_ctp.collector.cli import _resolve_log_file
        from bt_api_ctp.collector.schedule import TradingCalendar

        path = _resolve_log_file(
            {"data_root": str(tmp_path)},
            "20260916",
            TradingCalendar(),
            night=False,
            moment=datetime(2026, 9, 16, 10, 0),
        )

        assert path == tmp_path / "logs" / "collector-20260916.log"

    def test_explicit_night_flag_wins(self, tmp_path):
        from datetime import datetime

        from bt_api_ctp.collector.cli import _resolve_log_file
        from bt_api_ctp.collector.schedule import TradingCalendar

        path = _resolve_log_file(
            {"data_root": str(tmp_path)},
            "20260916",
            TradingCalendar(),
            night=True,
            moment=datetime(2026, 9, 16, 10, 0),
        )

        assert path.name == "collector-20260917.log"

    def test_custom_log_dir(self, tmp_path):
        from datetime import datetime

        from bt_api_ctp.collector.cli import _resolve_log_file
        from bt_api_ctp.collector.schedule import TradingCalendar

        path = _resolve_log_file(
            {"data_root": str(tmp_path), "logging": {"dir": "mylogs"}},
            "20260916",
            TradingCalendar(),
            night=False,
            moment=datetime(2026, 9, 16, 10, 0),
        )

        assert path == tmp_path / "mylogs" / "collector-20260916.log"

    def test_file_output_can_be_disabled(self, tmp_path):
        from bt_api_ctp.collector.cli import _resolve_log_file
        from bt_api_ctp.collector.schedule import TradingCalendar

        path = _resolve_log_file(
            {"data_root": str(tmp_path), "logging": {"file": False}},
            "20260916",
            TradingCalendar(),
            night=False,
            moment=None,
        )

        assert path is None

    def test_missing_data_root_returns_none(self):
        from bt_api_ctp.collector.cli import _resolve_log_file
        from bt_api_ctp.collector.schedule import TradingCalendar

        assert _resolve_log_file({}, "20260916", TradingCalendar(), night=False, moment=None) is None
