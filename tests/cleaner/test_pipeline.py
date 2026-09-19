"""集成测试：local 后端模拟多机，跑通 pull→verify→merge→kline→reclaim 全链路。"""

from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from bt_api_ctp.cleaner.config import load_config
from bt_api_ctp.cleaner.manifest import (
    STATUS_DELETED,
    STATUS_FAILED,
    Manifest,
)
from bt_api_ctp.cleaner.pipeline import kline_only, run
from bt_api_ctp.cleaner.report import build_payload, write_report
from bt_api_ctp.cleaner_ctp.symbols import classify_contract
from bt_api_ctp.collector.sink import TICK_ARROW_SCHEMA

DAY = "20260918"


def _tick_row(
    instrument_id: str,
    *,
    exchange_id: str,
    update_time: str,
    local_receive_time: int,
    last_price=100.0,
    volume=10,
):
    return {
        "trading_day": DAY,
        "action_day": DAY,
        "update_time": update_time,
        "update_millisec": 0,
        "exchange_id": exchange_id,
        "instrument_id": instrument_id,
        "local_receive_time": local_receive_time,
        "last_price": last_price,
        "volume": volume,
        "turnover": float(volume) * last_price,
        "open_interest": 500.0,
    }


def _write_parquet(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=TICK_ARROW_SCHEMA), path)


def _make_remote(root, day, exchange, instrument, rows, *, report=True, raw_bytes=None):
    day_dir = root / day
    parquet = day_dir / exchange / f"{instrument}.parquet"
    if raw_bytes is not None:
        parquet.parent.mkdir(parents=True, exist_ok=True)
        parquet.write_bytes(raw_bytes)
    else:
        _write_parquet(parquet, rows)
    if report:
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "report.json").write_text("{}", encoding="utf-8")
    return day_dir


def _config(tmp_path, host_roots):
    payload = {
        "tick_root": "tick",
        "kline_root": "kline",
        "pull": {
            "delete_remote_after_verify": True,
            "staging_root": "staging",
            "hosts": [
                {"name": name, "backend": "local", "remote_data_root": str(root)}
                for name, root in host_roots.items()
            ],
        },
        "kline": {"periods": [1, 5], "include_options": True},
    }
    path = tmp_path / "cleaner.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return load_config(path, base_dir=tmp_path)


def _two_hosts(tmp_path):
    remote_a = tmp_path / "remote-a"
    remote_b = tmp_path / "remote-b"
    _make_remote(
        remote_a,
        DAY,
        "SHFE",
        "rb2510",
        [
            _tick_row(
                "rb2510",
                exchange_id="SHFE",
                update_time="09:00:00",
                local_receive_time=100,
                last_price=3500.0,
            ),
            _tick_row(
                "rb2510",
                exchange_id="SHFE",
                update_time="09:00:30",
                local_receive_time=101,
                last_price=3510.0,
            ),
        ],
    )
    _make_remote(
        remote_b,
        DAY,
        "SHFE",
        "rb2510",
        [
            # 与 A 机同键但接收更晚：合并后应取这条
            _tick_row(
                "rb2510",
                exchange_id="SHFE",
                update_time="09:00:00",
                local_receive_time=200,
                last_price=3499.0,
            ),
        ],
    )
    _make_remote(
        remote_b,
        DAY,
        "DCE",
        "m2509-C-3000",
        [
            _tick_row(
                "m2509-C-3000",
                exchange_id="DCE",
                update_time="09:00:00",
                local_receive_time=1,
                last_price=50.0,
                volume=3,
            ),
        ],
    )
    return {"a": remote_a, "b": remote_b}


class TestFullRun:
    def test_pull_merge_kline_reclaim(self, tmp_path):
        hosts = _two_hosts(tmp_path)
        config = _config(tmp_path, hosts)

        report = run(config, classifier=classify_contract)

        # pull 阶段
        assert report.hosts["a"].days_pulled == [DAY]
        assert report.hosts["b"].days_pulled == [DAY]
        # merge 阶段：A 的 09:00 被 B 覆盖，加上 09:00:30 共 2 条
        merge = report.merge[DAY]
        assert merge.instruments == 2
        rows = pq.read_table(tmp_path / "tick" / DAY / "SHFE" / "rb2510.parquet").to_pylist()
        assert [row["update_time"] for row in rows] == ["09:00:00", "09:00:30"]
        assert rows[0]["last_price"] == 3499.0
        # kline 阶段：期货进 SHFE/rb，期权进 DCE/m
        assert (tmp_path / "kline" / "SHFE" / "rb" / "rb2510_1min.parquet").exists()
        assert (tmp_path / "kline" / "SHFE" / "rb" / "rb2510_5min.parquet").exists()
        assert (tmp_path / "kline" / "DCE" / "m" / "m2509-C-3000_1min.parquet").exists()
        # reclaim 阶段：K 线成功后才删除远程
        assert report.reclaim.deleted == ["a/20260918", "b/20260918"]
        assert not (hosts["a"] / DAY).exists()
        assert not (hosts["b"] / DAY).exists()

    def test_manifest_ends_deleted_and_klined(self, tmp_path):
        hosts = _two_hosts(tmp_path)
        config = _config(tmp_path, hosts)

        run(config, classifier=classify_contract)

        manifest = Manifest(config.manifest_path)
        for host in ("a", "b"):
            entry = manifest.entry(host, DAY)
            assert entry.status == STATUS_DELETED
            assert entry.klined_at is not None

    def test_report_payload_is_written_and_anomaly_free(self, tmp_path):
        hosts = _two_hosts(tmp_path)
        config = _config(tmp_path, hosts)

        report = run(config, classifier=classify_contract)
        path = write_report(report, config.report_dir)

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert path.name == f"clean-{DAY}.json"
        assert payload["dry_run"] is False
        assert payload["hosts"]["a"]["days_pulled"] == [DAY]
        assert payload["kline"][DAY]["instruments"] == 2
        assert payload["reclaim"]["deleted"] == ["a/20260918", "b/20260918"]
        assert set(payload["kline"][DAY]["bars_added"]) == {"1min", "5min"}
        assert payload["anomalies"] == []

    def test_staging_is_cleaned_after_merge(self, tmp_path):
        hosts = _two_hosts(tmp_path)
        config = _config(tmp_path, hosts)

        run(config, classifier=classify_contract)

        assert list((tmp_path / "staging").rglob("*.parquet")) == []

    def test_second_run_has_nothing_to_do(self, tmp_path):
        hosts = _two_hosts(tmp_path)
        config = _config(tmp_path, hosts)
        run(config, classifier=classify_contract)

        report = run(config, classifier=classify_contract)

        assert all(host.days_pulled == [] for host in report.hosts.values())
        assert report.merge == {}
        assert report.reclaim.planned == []


class TestIncompleteRemote:
    def test_day_without_report_is_skipped_and_kept(self, tmp_path):
        hosts = _two_hosts(tmp_path)
        _make_remote(
            hosts["a"],
            "20260917",
            "SHFE",
            "rb2510",
            [_tick_row("rb2510", exchange_id="SHFE", update_time="09:00:00", local_receive_time=1)],
            report=False,
        )
        config = _config(tmp_path, hosts)

        report = run(config, classifier=classify_contract)

        assert report.hosts["a"].days_incomplete == ["20260917"]
        assert "20260917" not in report.reclaim.planned
        assert (hosts["a"] / "20260917").exists()

    def test_verify_failure_keeps_remote_and_marks_failed(self, tmp_path):
        hosts = _two_hosts(tmp_path)
        # 损坏的 parquet：能拉到本地，但校验阶段读不出来
        _make_remote(hosts["b"], DAY, "SHFE", "ag2612", [], report=False, raw_bytes=b"not parquet")
        (hosts["b"] / DAY / "report.json").write_text("{}", encoding="utf-8")
        config = _config(tmp_path, hosts)

        report = run(config, classifier=classify_contract)

        assert report.hosts["b"].verify_failures
        manifest = Manifest(config.manifest_path)
        assert manifest.status("b", DAY) == STATUS_FAILED
        # 校验失败的机器不被删除，也不进入合并
        assert (hosts["b"] / DAY).exists()
        assert "b/20260918" not in report.reclaim.planned

    def test_schema_drift_is_reported(self, tmp_path):
        hosts = _two_hosts(tmp_path)
        partial = pa.schema(
            [
                pa.field("trading_day", pa.string()),
                pa.field("action_day", pa.string()),
                pa.field("update_time", pa.string()),
                pa.field("update_millisec", pa.int32()),
                pa.field("local_receive_time", pa.int64()),
                pa.field("exchange_id", pa.string()),
                pa.field("instrument_id", pa.string()),
            ]
        )
        source = hosts["b"] / DAY / "SHFE" / "drifted.parquet"
        pq.write_table(
            pa.Table.from_pylist(
                [
                    _tick_row(
                        "drifted", exchange_id="SHFE", update_time="09:00:00", local_receive_time=1
                    )
                ],
                schema=partial,
            ),
            source,
        )
        config = _config(tmp_path, hosts)

        report = run(config, classifier=classify_contract)

        assert any("drifted" in note for note in report.merge[DAY].schema_drift)
        payload = build_payload(report)
        assert any("schema drift" in anomaly for anomaly in payload["anomalies"])


class TestDryRun:
    def test_changes_nothing(self, tmp_path):
        hosts = _two_hosts(tmp_path)
        config = _config(tmp_path, hosts)

        report = run(config, classifier=classify_contract, dry_run=True)

        assert report.dry_run is True
        assert (hosts["a"] / DAY).exists()
        assert (hosts["b"] / DAY).exists()
        assert not (tmp_path / "tick").exists()
        assert not (tmp_path / "staging").exists()
        assert Manifest(config.manifest_path).entry("a", DAY) is None


class TestKlineOnly:
    def test_backfill_rebuilds_every_tick_day(self, tmp_path):
        hosts = _two_hosts(tmp_path)
        config = _config(tmp_path, hosts)
        _write_parquet(
            tmp_path / "tick" / DAY / "SHFE" / "rb2510.parquet",
            [_tick_row("rb2510", exchange_id="SHFE", update_time="09:00:00", local_receive_time=1)],
        )

        report = kline_only(config, classifier=classify_contract, backfill=True)

        assert DAY in report.kline
        assert (tmp_path / "kline" / "SHFE" / "rb" / "rb2510_1min.parquet").exists()

    def test_explicit_days_are_honoured(self, tmp_path):
        hosts = _two_hosts(tmp_path)
        config = _config(tmp_path, hosts)

        report = kline_only(config, classifier=classify_contract, days=[DAY])

        assert list(report.kline) == [DAY]
