"""离线契约测试：cleaner 命令行入口的退出码与报告落盘。"""

from __future__ import annotations

import json
from datetime import datetime

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from bt_api_ctp.cleaner.cli import (
    EXIT_CONFIG_ERROR,
    EXIT_NOT_TRADING_DAY,
    EXIT_OK,
    EXIT_TRANSFER_FAILED,
    main,
)
from bt_api_ctp.collector.sink import TICK_ARROW_SCHEMA

DAY = "20260918"


def _tick_row():
    return {
        "trading_day": DAY,
        "action_day": DAY,
        "update_time": "09:00:00",
        "update_millisec": 0,
        "exchange_id": "SHFE",
        "instrument_id": "rb2510",
        "local_receive_time": 1,
        "last_price": 3500.0,
        "volume": 10,
        "turnover": 35000.0,
        "open_interest": 500.0,
    }


def _remote(root):
    day_dir = root / DAY
    parquet = day_dir / "SHFE" / "rb2510.parquet"
    parquet.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([_tick_row()], schema=TICK_ARROW_SCHEMA), parquet)
    (day_dir / "report.json").write_text("{}", encoding="utf-8")
    return root


def _config_file(tmp_path, *, hosts, holidays_file=None):
    payload = {
        "tick_root": "tick",
        "kline_root": "kline",
        "pull": {
            "hosts": [
                {"name": name, "backend": "local", "remote_data_root": str(root)}
                for name, root in hosts.items()
            ]
        },
        "calendar": {"holidays_file": holidays_file or ""},
    }
    path = tmp_path / "cleaner.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


class TestConfigErrors:
    def test_missing_config_file_is_a_config_error(self, tmp_path, capsys):
        code = main(["--config", str(tmp_path / "nope.yaml"), "run"])

        assert code == EXIT_CONFIG_ERROR
        assert "配置错误" in capsys.readouterr().err

    def test_invalid_config_is_a_config_error(self, tmp_path, capsys):
        path = tmp_path / "cleaner.yaml"
        path.write_text(yaml.safe_dump({"tick_root": "tick"}), encoding="utf-8")

        code = main(["--config", str(path), "run"])

        assert code == EXIT_CONFIG_ERROR


class TestCheckHosts:
    def test_ok_when_every_host_is_reachable(self, tmp_path, capsys):
        remote = _remote(tmp_path / "remote")
        path = _config_file(tmp_path, hosts={"a": remote})

        code = main(["--config", str(path), "check-hosts"])

        assert code == EXIT_OK
        assert "a (local): ok" in capsys.readouterr().out

    def test_transfer_failure_code_when_a_root_is_missing(self, tmp_path):
        path = _config_file(tmp_path, hosts={"a": tmp_path / "nowhere"})

        code = main(["--config", str(path), "check-hosts"])

        assert code == EXIT_TRANSFER_FAILED


class TestTradingDayGate:
    def test_non_trading_day_exits_3(self, tmp_path, capsys):
        remote = _remote(tmp_path / "remote")
        today = datetime.now().strftime("%Y%m%d")
        holidays = tmp_path / "holidays.json"
        holidays.write_text(json.dumps([today]), encoding="utf-8")
        path = _config_file(tmp_path, hosts={"a": remote}, holidays_file=str(holidays))

        code = main(["--config", str(path), "run"])

        assert code == EXIT_NOT_TRADING_DAY
        assert "非交易日" in capsys.readouterr().out

    def test_explicit_day_bypasses_the_gate(self, tmp_path):
        remote = _remote(tmp_path / "remote")
        today = datetime.now().strftime("%Y%m%d")
        holidays = tmp_path / "holidays.json"
        holidays.write_text(json.dumps([today]), encoding="utf-8")
        path = _config_file(tmp_path, hosts={"a": remote}, holidays_file=str(holidays))

        code = main(["--config", str(path), "--base-dir", str(tmp_path), "run", "--day", DAY])

        assert code == EXIT_OK
        from bt_api_ctp.cleaner.manifest import Manifest

        assert (
            Manifest(tmp_path / "tick" / "cleaner" / "manifest.json").status("a", DAY) == "deleted"
        )


class TestRunCommands:
    def test_run_writes_a_report(self, tmp_path, capsys):
        remote = _remote(tmp_path / "remote")
        path = _config_file(tmp_path, hosts={"a": remote})

        code = main(["--config", str(path), "--base-dir", str(tmp_path), "run", "--day", DAY])

        assert code == EXIT_OK
        report = tmp_path / "tick" / "cleaner" / "reports" / f"clean-{DAY}.json"
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["kline"][DAY]["instruments"] == 1
        assert payload["reclaim"]["deleted"] == [f"a/{DAY}"]
        assert "kline" in capsys.readouterr().out

    def test_backfill_kline(self, tmp_path):
        remote = _remote(tmp_path / "remote")
        path = _config_file(tmp_path, hosts={"a": remote})
        main(["--config", str(path), "--base-dir", str(tmp_path), "run", "--day", DAY])

        code = main(["--config", str(path), "--base-dir", str(tmp_path), "kline", "--backfill"])

        assert code == EXIT_OK
        assert (tmp_path / "kline" / "SHFE" / "rb" / "rb2510_1min.parquet").exists()

    def test_dry_run_reports_without_changing_anything(self, tmp_path):
        remote = _remote(tmp_path / "remote")
        path = _config_file(tmp_path, hosts={"a": remote})

        code = main(
            ["--config", str(path), "--base-dir", str(tmp_path), "run", "--dry-run", "--day", DAY]
        )

        assert code == EXIT_OK
        assert (remote / DAY).exists()
        assert not (tmp_path / "tick" / DAY).exists()
