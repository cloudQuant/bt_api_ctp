"""离线契约测试：Parquet 落盘、去重排序、中断恢复合并与完整性报告。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from bt_api_ctp.collector.protocols import TickRecord
from bt_api_ctp.collector.sink import TICK_ARROW_SCHEMA, ParquetSink


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """有界等待，避免用固定 sleep 制造不稳定测试。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _tick(
    instrument_id: str = "rb2510",
    *,
    exchange_id: str = "SHFE",
    trading_day: str = "20260916",
    action_day: str = "20260916",
    update_time: str = "09:00:00",
    update_millisec: int = 0,
    local_receive_time: int = 1,
    last_price: float | None = 3500.0,
    **overrides,
) -> TickRecord:
    base = {
        "exchange_id": exchange_id,
        "instrument_id": instrument_id,
        "trading_day": trading_day,
        "action_day": action_day,
        "update_time": update_time,
        "update_millisec": update_millisec,
        "local_receive_time": local_receive_time,
        "last_price": last_price,
    }
    base.update(overrides)
    return TickRecord(**base)


class TestStagedWriting:
    """盘中只追加段文件，收盘（或段数达阈值）才压缩成最终文件（整改方案 P1-5）。

    旧实现在每次刷盘时对该合约做"读旧文件 → 去重 → 整文件重写"，全市场一天
    重写 46 万次；新实现把最终文件的重写次数与刷盘次数解耦。
    """

    def test_write_appends_a_segment_without_touching_final_files(self, tmp_path):
        sink = ParquetSink(tmp_path)

        sink.write({"rb2510": [_tick()]})

        assert not (tmp_path / "20260916" / "SHFE" / "rb2510.parquet").exists()
        assert len(list((tmp_path / ".staging").rglob("*.parquet"))) == 1

    def test_finalize_merges_staging_into_final_files(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick(update_millisec=0), _tick(update_millisec=500)]})

        sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert [row["update_millisec"] for row in rows] == [0, 500]
        assert list((tmp_path / ".staging").rglob("*.parquet")) == []

    def test_final_files_are_rewritten_on_compaction_not_on_every_flush(self, tmp_path):
        """W-1：重写次数由段阈值决定，而不是每次刷盘。"""
        sink = ParquetSink(tmp_path, compact_segment_count=3)

        for millisec in range(2):
            sink.write({"rb2510": [_tick(update_millisec=millisec)]})

        assert not (tmp_path / "20260916" / "SHFE" / "rb2510.parquet").exists(), (
            "未达段阈值前不得产生任何最终文件重写"
        )

        sink.write({"rb2510": [_tick(update_millisec=2)]})  # 达到阈值 → 触发一次后台压缩

        final = tmp_path / "20260916" / "SHFE" / "rb2510.parquet"
        assert _wait_until(final.exists), "阈值触发的是后台压缩，需等待其完成"
        rows = pq.read_table(final).to_pylist()
        assert [row["update_millisec"] for row in rows] == [0, 1, 2]
        assert list((tmp_path / ".staging").rglob("*.parquet")) == []

    def test_compaction_dedups_across_segments(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick(local_receive_time=1)]})
        sink.write({"rb2510": [_tick(local_receive_time=9)]})  # 同时间键，接收时间更晚

        sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert len(rows) == 1
        assert rows[0]["local_receive_time"] == 9

    def test_a_segment_spanning_instruments_is_split_during_compaction(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick()], "cu2510": [_tick("cu2510")]})

        sink.finalize("20260916")

        assert (tmp_path / "20260916" / "SHFE" / "rb2510.parquet").exists()
        assert (tmp_path / "20260916" / "SHFE" / "cu2510.parquet").exists()

    def test_lock_is_only_taken_during_compaction(self, tmp_path):
        """W-4：盘中不等锁，跨进程锁只包住最终合并。"""
        sink = ParquetSink(tmp_path)

        sink.write({"rb2510": [_tick()]})

        assert list((tmp_path / ".locks").rglob("*.lock")) == []

        sink.finalize("20260916")

        assert list((tmp_path / ".locks").rglob("*.lock"))

    def test_finalize_twice_is_idempotent(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick()]})

        sink.finalize("20260916")
        sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert len(rows) == 1

    def test_merge_existing_false_overwrites_earlier_compaction(self, tmp_path):
        """钉住既有语义（非期望行为）：merge_existing=False 时后一次压缩覆盖前一次。

        生产配置必须保持 merge_existing=True；本用例把该风险显式化，
        详见整改方案 11.4 遗留项。
        """
        sink = ParquetSink(tmp_path, merge_existing=False)
        sink.write({"rb2510": [_tick(update_millisec=10)]})
        sink.finalize("20260916")
        sink.write({"rb2510": [_tick(update_millisec=20)]})
        sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert [row["update_millisec"] for row in rows] == [20]


class TestStagingRecovery:
    """硬崩溃（SIGKILL/断电）留下的段文件必须能被后续运行合并，不能永久滞留。"""

    def _stage_as(self, tmp_path, run_id: str) -> Path:
        producer = ParquetSink(tmp_path)
        producer.write({"rb2510": [_tick()]})
        segment = next((tmp_path / ".staging").rglob("*.parquet"))
        target = tmp_path / ".staging" / run_id
        target.mkdir(parents=True, exist_ok=True)
        moved = target / segment.name
        segment.rename(moved)
        return moved

    def test_unreadable_segment_is_preserved_instead_of_dropped(self, tmp_path):
        """段损坏时必须保留现场并计数，绝不能连它承载的数据一起删除。"""
        producer = ParquetSink(tmp_path)
        producer.write({"rb2510": [_tick(update_millisec=0)]})
        producer.write({"rb2510": [_tick(update_millisec=1)]})
        segments = sorted((tmp_path / ".staging" / producer._run_id).glob("*.parquet"))
        assert len(segments) == 2
        segments[1].write_bytes(b"this is not parquet")

        sink = ParquetSink(tmp_path)
        sink._segments = list(segments)
        sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert [row["update_millisec"] for row in rows] == [0], "可读段必须照常落盘"
        assert list((tmp_path / ".staging").rglob("*.bad")), "损坏段必须保留现场"
        payload = json.loads(
            (tmp_path / "20260916" / "report.json").read_text(encoding="utf-8")
        )
        assert payload["compaction_failures"] == 1

    def test_segments_from_a_dead_run_are_merged(self, tmp_path):
        self._stage_as(tmp_path, "999999-deadbeef")

        ParquetSink(tmp_path).finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert len(rows) == 1
        assert list((tmp_path / ".staging").rglob("*.parquet")) == []

    def test_segments_from_a_live_run_are_left_alone(self, tmp_path):
        # pid 1 在任何 POSIX 系统上都必然存活
        staged = self._stage_as(tmp_path, "1-live")

        ParquetSink(tmp_path).finalize("20260916")

        assert not (tmp_path / "20260916" / "SHFE" / "rb2510.parquet").exists()
        assert staged.exists()

    def test_repeated_finalize_does_not_inflate_pending_or_failures(self, tmp_path, monkeypatch):
        """反复 finalize + 持续失败时，pending 与失败计数不得单调虚增。"""
        self._stage_as(tmp_path, "999999-deadbeef")

        def failing_compact(self, segments):
            raise OSError("disk full")

        monkeypatch.setattr(ParquetSink, "_compact", failing_compact)
        sink = ParquetSink(tmp_path)

        sink.finalize("20260916")
        first = sink.compaction_stats()
        sink.finalize("20260916")
        second = sink.compaction_stats()

        assert first["pending_segments"] == 1
        assert second["pending_segments"] == 1, "同一段不得被重复入队"
        assert second["failures"] == first["failures"] + 1, "每次收盘只应尝试一次"

    def test_a_batch_with_no_readable_segment_is_not_a_compaction(self, tmp_path):
        producer = ParquetSink(tmp_path)
        producer.write({"rb2510": [_tick()]})
        segment = next((tmp_path / ".staging").rglob("*.parquet"))
        segment.write_bytes(b"broken")

        sink = ParquetSink(tmp_path)
        sink._segments = [segment]
        sink.finalize("20260916")

        payload = json.loads(
            (tmp_path / "20260916" / "report.json").read_text(encoding="utf-8")
        )
        assert payload["compactions"] == 0, "没有写出任何最终文件就不算一次压缩"
        assert payload["compaction_failures"] == 1
        assert payload["compaction_last_error"]

    def test_recovery_failure_does_not_abort_finalize(self, tmp_path, monkeypatch):
        """认领崩溃遗留段失败时，报告仍必须生成，且失败要计入报告。"""
        self._stage_as(tmp_path, "999999-deadbeef")

        def failing_compact(self, segments):
            raise OSError("disk full")

        monkeypatch.setattr(ParquetSink, "_compact", failing_compact)
        report = ParquetSink(tmp_path).finalize("20260916")

        payload = json.loads(
            (tmp_path / "20260916" / "report.json").read_text(encoding="utf-8")
        )
        assert payload["compaction_failures"] == 1
        assert report.compaction_failures == 1
        assert payload["pending_segments"] == 1


class TestBackgroundCompaction:
    """压缩必须离开采集主循环，否则心跳仍会被长时间阻塞（整改方案 P1-6）。"""

    def test_write_returns_before_compaction_finishes(self, tmp_path, monkeypatch):
        original = ParquetSink._compact
        entered = threading.Event()
        release = threading.Event()

        def slow_compact(self, segments):
            entered.set()
            release.wait(timeout=5)
            original(self, segments)

        monkeypatch.setattr(ParquetSink, "_compact", slow_compact)
        sink = ParquetSink(tmp_path, compact_segment_count=2)

        sink.write({"rb2510": [_tick(update_millisec=0)]})
        sink.write({"rb2510": [_tick(update_millisec=1)]})  # 触发后台压缩

        assert entered.wait(timeout=2), "压缩应在后台线程里开始"
        assert not (tmp_path / "20260916" / "SHFE" / "rb2510.parquet").exists(), (
            "write 不得等压缩完成"
        )

        release.set()
        sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert [row["update_millisec"] for row in rows] == [0, 1]

    def test_compaction_failure_is_retried_at_close_and_reported(self, tmp_path, monkeypatch):
        original = ParquetSink._compact
        calls = {"count": 0}

        def flaky_compact(self, segments):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("disk full")
            original(self, segments)

        monkeypatch.setattr(ParquetSink, "_compact", flaky_compact)
        sink = ParquetSink(tmp_path, compact_segment_count=2)
        sink.write({"rb2510": [_tick(update_millisec=0)]})
        sink.write({"rb2510": [_tick(update_millisec=1)]})
        assert _wait_until(lambda: sink.compaction_stats()["failures"] == 1)

        sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert len(rows) == 2, "失败的段不得丢失，收盘必须重试成功"
        payload = json.loads(
            (tmp_path / "20260916" / "report.json").read_text(encoding="utf-8")
        )
        assert payload["compaction_failures"] == 1

    def test_report_exposes_compaction_stats(self, tmp_path):
        """W-3：队列（段积压）与压缩次数必须可从 report.json 观测。"""
        sink = ParquetSink(tmp_path, compact_segment_count=2)
        sink.write({"rb2510": [_tick(update_millisec=0)]})
        sink.write({"rb2510": [_tick(update_millisec=1)]})

        sink.finalize("20260916")

        payload = json.loads(
            (tmp_path / "20260916" / "report.json").read_text(encoding="utf-8")
        )
        assert payload["compactions"] >= 1
        assert payload["compaction_failures"] == 0
        assert payload["pending_segments"] == 0


class TestDataQualityMetrics:
    """数据质量指标（整改方案 P2-1 ~ P2-5）。"""

    def test_outside_session_ticks_are_dropped_and_counted(self, tmp_path):
        """P2-1：订阅时前置推的伪快照（如夜盘前的 20:18）不属于任何时段。"""
        sink = ParquetSink(tmp_path)
        sink.write(
            {
                "rb2510": [
                    _tick(action_day="20260916", update_time="20:18:42"),
                    _tick(action_day="20260916", update_time="21:00:01"),
                ]
            }
        )

        report = sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert [row["update_time"] for row in rows] == ["21:00:01"]
        assert report.ticks_outside_session == 1

    def test_pre_open_auction_quotes_are_kept(self, tmp_path):
        """集合竞价（08:55 / 20:55）是真实行情，不得被时段过滤误删。"""
        sink = ParquetSink(tmp_path)
        sink.write(
            {
                "rb2510": [
                    _tick(trading_day="20260917", action_day="20260916", update_time="20:55:01"),
                    _tick(trading_day="20260917", action_day="20260917", update_time="08:55:03"),
                ]
            }
        )

        report = sink.finalize("20260917")

        assert report.ticks_outside_session == 0
        rows = pq.read_table(tmp_path / "20260917" / "SHFE" / "rb2510.parquet").to_pylist()
        assert [row["update_time"] for row in rows] == ["20:55:01", "08:55:03"]

    def test_night_session_across_midnight_is_kept(self, tmp_path):
        """夜盘 23:59 / 00:01 属同一段连续行情，不得被时段过滤误删。"""
        sink = ParquetSink(tmp_path)
        sink.write(
            {
                "rb2510": [
                    _tick(action_day="20260916", update_time="23:59:59"),
                    _tick(action_day="20260917", update_time="00:00:01"),
                ]
            }
        )

        report = sink.finalize("20260916")

        assert report.ticks_outside_session == 0
        assert report.instruments[0].rows == 2

    def test_max_gap_is_reported_even_below_the_adaptive_threshold(self, tmp_path):
        """自适应阈值只影响 gaps 计数；真实断流必须永远可见。"""
        sink = ParquetSink(tmp_path)
        sparse = [_tick(update_time=f"09:{minute:02d}:00") for minute in range(0, 12, 2)]
        sparse.append(_tick(update_time="09:25:00"))  # 15 分钟断流
        sink.write({"rb2510": sparse})

        report = sink.finalize("20260916")

        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert entry.gaps == [], "阈值被自适应抬高，不产生缺口条目"
        assert entry.max_gap_seconds == pytest.approx(900.0)
        assert entry.max_gap_after is not None and entry.max_gap_before is not None

    def test_outside_session_filtering_can_be_disabled(self, tmp_path):
        sink = ParquetSink(tmp_path, drop_outside_session=False)
        sink.write({"rb2510": [_tick(action_day="20260916", update_time="20:18:42")]})

        report = sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert len(rows) == 1
        assert report.ticks_outside_session == 0

    def test_coverage_scores_the_in_session_window(self, tmp_path):
        """P2-5：覆盖率 = 实收条数 / 窗口内按 500ms 应有多少条。"""
        sink = ParquetSink(tmp_path)
        ticks = [_tick(update_time=f"09:00:{second:02d}") for second in (0, 2, 4, 6, 10)]
        sink.write({"rb2510": ticks})

        report = sink.finalize("20260916")

        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert entry.expected_ticks == 20  # 09:00:00 -> 09:00:10 共 10s，500ms 一条
        assert entry.coverage == pytest.approx(0.25)

    def test_coverage_is_undefined_without_a_window(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick()]})

        report = sink.finalize("20260916")

        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert entry.coverage is None
        assert entry.expected_ticks is None

    def test_adaptive_threshold_ignores_sparse_instruments(self, tmp_path):
        """P2-4：阈值按合约自身间隔自适应，慢速合约不得误报。"""
        sink = ParquetSink(tmp_path)
        sparse = [_tick(update_time=f"09:{minute:02d}:00") for minute in range(0, 12, 2)]
        sink.write({"rb2510": sparse})  # 中位间隔 120s -> 阈值 1200s

        report = sink.finalize("20260916")

        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert entry.gaps == []

    def test_adaptive_threshold_still_reports_fast_instrument_gaps(self, tmp_path):
        sink = ParquetSink(tmp_path)
        fast = [_tick(update_time=f"09:00:{second:02d}") for second in range(6)]
        fast.append(_tick(update_time="09:01:30"))  # 90s 静默
        sink.write({"rb2510": fast})  # 中位间隔 1s -> 阈值保持 60s

        report = sink.finalize("20260916")

        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert len(entry.gaps) == 1

    def test_receive_clock_gaps_and_lag_are_reported(self, tmp_path):
        """P2-3：交易所时钟看不出缺口时，接收时钟必须能看出来。"""
        cst = timezone(timedelta(hours=8))
        first = int(datetime(2026, 9, 16, 9, 0, 0, tzinfo=cst).timestamp() * 1e9)
        second = int(datetime(2026, 9, 16, 10, 0, 0, tzinfo=cst).timestamp() * 1e9)
        sink = ParquetSink(tmp_path)
        sink.write(
            {
                "rb2510": [
                    _tick(update_time="09:00:00", local_receive_time=first),
                    _tick(update_time="09:00:01", local_receive_time=second),
                ]
            }
        )

        report = sink.finalize("20260916")

        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert entry.gaps == []  # 交易所时钟上只差 1 秒
        assert entry.receive_gaps == 1  # 接收时钟上差了 1 小时
        assert entry.max_receive_lag_seconds == pytest.approx(3599.0)

    def test_volume_jumps_estimate_missing_snapshots(self, tmp_path):
        """P2-2：相邻快照成交量异常跳增即丢失快照，按其估算条数。"""
        sink = ParquetSink(tmp_path)
        ticks = [
            _tick(update_time=f"09:00:{second:02d}", volume=volume)
            for second, volume in enumerate([1, 2, 3, 1000])
        ]
        sink.write({"rb2510": ticks})

        report = sink.finalize("20260916")

        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert entry.volume_jumps == 1
        assert entry.max_volume_jump == pytest.approx(997.0)
        assert entry.estimated_missing_ticks == 996

    def test_healthy_volume_progression_has_no_jumps(self, tmp_path):
        sink = ParquetSink(tmp_path)
        ticks = [
            _tick(update_time=f"09:00:{second:02d}", volume=second * 3) for second in range(6)
        ]
        sink.write({"rb2510": ticks})

        report = sink.finalize("20260916")

        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert entry.volume_jumps == 0
        assert entry.estimated_missing_ticks is None


class TestParquetSinkLayout:
    """最终文件布局不变；数据在 finalize（压缩）后才出现在数据目录。

    盘中写入只追加段文件（见 TestStagedWriting），所以这些用例都在读最终路径
    之前先调用 ``finalize``——那是压缩发生、最终文件产生的时刻。
    """

    def test_write_creates_parquet_with_fixed_schema(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick()]})
        sink.finalize("20260916")

        path = tmp_path / "20260916" / "SHFE" / "rb2510.parquet"
        assert path.exists()
        table = pq.read_table(path)
        assert table.schema.equals(TICK_ARROW_SCHEMA, check_metadata=False)
        assert table.num_rows == 1
        assert table.column("instrument_id").to_pylist() == ["rb2510"]

    def test_expands_five_level_book_into_columns(self, tmp_path):
        sink = ParquetSink(tmp_path)
        tick = _tick(
            bid_price=(1.0, 2.0),
            bid_volume=(10,),
            ask_price=(3.0, 4.0, 5.0, 6.0, 7.0),
            ask_volume=(1, 2, 3, 4, 5),
        )
        sink.write({"rb2510": [tick]})
        sink.finalize("20260916")

        row = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()[0]
        assert row["bid_price_1"] == 1.0
        assert row["bid_price_2"] == 2.0
        assert row["bid_price_3"] is None
        assert row["bid_volume_1"] == 10
        assert row["bid_volume_2"] is None
        assert row["ask_price_5"] == 7.0
        assert row["ask_volume_5"] == 5

    def test_separates_exchanges_and_instruments(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.write(
            {
                "rb2510": [_tick("rb2510")],
                "cu2510": [_tick("cu2510")],
            }
        )
        sink.finalize("20260916")

        assert (tmp_path / "20260916" / "SHFE" / "rb2510.parquet").exists()
        assert (tmp_path / "20260916" / "SHFE" / "cu2510.parquet").exists()

    def test_night_session_is_filed_under_next_trading_day(self, tmp_path):
        sink = ParquetSink(tmp_path)
        night = _tick(trading_day="20260916", action_day="20260915", update_time="21:30:00")
        day = _tick(trading_day="20260916", action_day="20260916", update_time="09:05:00")

        sink.write({"rb2510": [night, day]})
        sink.finalize("20260916")

        path = tmp_path / "20260916" / "SHFE" / "rb2510.parquet"
        rows = pq.read_table(path).to_pylist()
        # 夜盘 action_day 为前一自然日，因此自然排在最前
        assert [row["action_day"] for row in rows] == ["20260915", "20260916"]


class TestParquetSinkCleaning:
    def test_dedup_by_time_key_keeps_latest_receive(self, tmp_path):
        sink = ParquetSink(tmp_path)
        first = _tick(local_receive_time=100, last_price=1.0)
        duplicate = _tick(local_receive_time=200, last_price=2.0)

        sink.write({"rb2510": [first, duplicate]})
        sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert len(rows) == 1
        assert rows[0]["last_price"] == 2.0
        assert rows[0]["local_receive_time"] == 200

    def test_distinct_millisec_in_same_second_is_kept(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick(update_millisec=0), _tick(update_millisec=500)]})
        sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert len(rows) == 2

    def test_orders_by_time_key(self, tmp_path):
        sink = ParquetSink(tmp_path)
        ticks = [
            _tick(update_time="09:00:01", update_millisec=500),
            _tick(update_time="09:00:00", update_millisec=0),
            _tick(update_time="09:00:00", update_millisec=500),
        ]
        sink.write({"rb2510": ticks})
        sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert [(row["update_time"], row["update_millisec"]) for row in rows] == [
            ("09:00:00", 0),
            ("09:00:00", 500),
            ("09:00:01", 500),
        ]

    def test_merge_existing_is_idempotent(self, tmp_path):
        sink = ParquetSink(tmp_path, merge_existing=True)
        ticks = [_tick(update_time="09:00:00"), _tick(update_time="09:00:01")]

        sink.write({"rb2510": ticks})
        sink.write({"rb2510": ticks})  # 中断后重跑同一批数据
        sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert len(rows) == 2

    def test_merge_appends_new_ticks(self, tmp_path):
        sink = ParquetSink(tmp_path, merge_existing=True)
        sink.write({"rb2510": [_tick(update_time="09:00:00")]})
        sink.finalize("20260916")  # 第一轮收尾，产生最终文件
        sink.write({"rb2510": [_tick(update_time="09:00:01")]})  # 第二轮续采
        sink.finalize("20260916")

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert [row["update_time"] for row in rows] == ["09:00:00", "09:00:01"]

    def test_empty_batch_is_a_noop(self, tmp_path):
        sink = ParquetSink(tmp_path)
        report = sink.write({})
        assert report.instruments == []
        assert not (tmp_path / "20260916").exists()

    def test_missing_trading_day_is_rejected(self, tmp_path):
        sink = ParquetSink(tmp_path)
        with pytest.raises(ValueError):
            sink.write({"rb2510": [_tick(trading_day="")]})

    def test_missing_exchange_id_is_rejected(self, tmp_path):
        """exchange_id 为空会导致路径缺交易所层级、finalize 失效，必须 fail-closed。"""
        sink = ParquetSink(tmp_path)
        with pytest.raises(ValueError):
            sink.write({"rb2510": [_tick(exchange_id="")]})

    def test_missing_instrument_id_is_rejected(self, tmp_path):
        """instrument_id 为空会成为 null 分组键、导致该笔数据被静默丢弃。"""
        sink = ParquetSink(tmp_path)
        with pytest.raises(ValueError):
            sink.write({"rb2510": [_tick(instrument_id="")]})


class TestParquetSinkReport:
    def test_write_stages_and_finalize_reports_per_instrument_stats(self, tmp_path):
        """write 只落段、不产最终文件；逐合约统计由 finalize 给出。"""
        sink = ParquetSink(tmp_path)
        write_report = sink.write(
            {
                "rb2510": [
                    _tick(update_time="09:00:00"),
                    _tick(update_time="09:00:01"),
                ]
            }
        )
        assert write_report.trading_day == "20260916"
        assert write_report.instruments == []

        report = sink.finalize("20260916")

        assert len(report.instruments) == 1
        entry = report.instruments[0]
        assert entry.instrument_id == "rb2510"
        assert entry.exchange_id == "SHFE"
        assert entry.rows == 2
        assert entry.first_update == "20260916 09:00:00.000"
        assert entry.last_update == "20260916 09:00:01.000"

    def test_finalize_writes_complete_report_json(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick(update_time="09:00:00")]})
        sink.write({"cu2510": [_tick("cu2510", update_time="09:00:05")]})

        report = sink.finalize("20260916", dropped_ticks=3)

        report_path = tmp_path / "20260916" / "report.json"
        assert report_path.exists()
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        assert payload["trading_day"] == "20260916"
        assert payload["dropped_ticks"] == 3
        assert {entry["instrument_id"] for entry in payload["instruments"]} == {"rb2510", "cu2510"}
        assert {entry["instrument_id"]: entry["rows"] for entry in payload["instruments"]} == {
            "rb2510": 1,
            "cu2510": 1,
        }
        assert report.dropped_ticks == 3

    def test_gap_detection_reports_long_silence(self, tmp_path):
        sink = ParquetSink(tmp_path, gap_threshold_sec=30.0)
        sink.write(
            {
                "rb2510": [
                    _tick(update_time="09:00:00"),
                    _tick(update_time="09:02:00"),  # 120s 缺口
                ]
            }
        )
        report = sink.finalize("20260916")
        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()

        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert len(entry.gaps) == 1
        assert entry.gaps[0]["seconds"] == pytest.approx(120.0)
        assert len(rows) == 2  # 仅用于确认落盘可读

    def test_finalize_without_files_is_empty_report(self, tmp_path):
        sink = ParquetSink(tmp_path)
        report = sink.finalize("20260916")
        assert report.instruments == []
        assert (tmp_path / "20260916" / "report.json").exists()

    def test_records_session_diagnostics(self, tmp_path):
        """断线时间窗、回调异常与连接代次必须进 report.json（整改方案 P1-2）。"""
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick()]})
        disconnects = [
            {
                "start": "2026-09-17T01:50:00+00:00",
                "reason": 8193,
                "generation_before": 3,
                "end": "2026-09-17T01:54:00+00:00",
                "generation_after": 4,
            }
        ]
        generations = [{"at": "2026-09-17T01:54:00+00:00", "generation": 4}]

        report = sink.finalize(
            "20260916",
            disconnects=disconnects,
            callback_errors=2,
            connection_generations=generations,
        )

        payload = json.loads(
            (tmp_path / "20260916" / "report.json").read_text(encoding="utf-8")
        )
        assert payload["disconnects"] == disconnects
        assert payload["callback_errors"] == 2
        assert payload["connection_generations"] == generations
        assert report.callback_errors == 2
        assert report.disconnects == disconnects

    def test_session_diagnostics_default_to_empty(self, tmp_path):
        sink = ParquetSink(tmp_path)
        report = sink.finalize("20260916")

        payload = json.loads(
            (tmp_path / "20260916" / "report.json").read_text(encoding="utf-8")
        )
        assert payload["disconnects"] == []
        assert payload["callback_errors"] == 0
        assert payload["connection_generations"] == []
        assert report.disconnects == []

    def test_records_subscription_diagnostics(self, tmp_path):
        """订阅失败与重订阅周期必须进 report.json（整改方案 P1-3 契约）。"""
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick()]})
        resubscribes = [{"at": "t0", "generation": 4, "requested": 250, "batches": 3}]

        report = sink.finalize(
            "20260916",
            failed_instruments={"nope": 42},
            resubscribes=resubscribes,
        )

        payload = json.loads(
            (tmp_path / "20260916" / "report.json").read_text(encoding="utf-8")
        )
        assert payload["failed_instruments"] == {"nope": 42}
        assert payload["resubscribes"] == resubscribes
        assert report.failed_instruments == {"nope": 42}
        assert report.resubscribes == resubscribes


class TestGapDetectionWithinSessions:
    """缺口只在连续交易时段内判定，避免把小节休息/午休/收盘误报为缺口。"""

    def test_lunch_break_is_not_a_gap(self, tmp_path):
        sink = ParquetSink(tmp_path, gap_threshold_sec=60.0)
        sink.write(
            {
                "rb2510": [
                    _tick(update_time="11:29:00"),
                    _tick(update_time="13:31:00"),  # 跨午休，相隔 2 小时
                ]
            }
        )
        report = sink.finalize("20260916")
        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert entry.gaps == []

    def test_short_break_is_not_a_gap(self, tmp_path):
        sink = ParquetSink(tmp_path, gap_threshold_sec=60.0)
        sink.write(
            {
                "rb2510": [
                    _tick(update_time="10:14:00"),
                    _tick(update_time="10:31:00"),  # 10:15-10:30 小节休息
                ]
            }
        )
        report = sink.finalize("20260916")
        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert entry.gaps == []

    def test_day_to_night_transition_is_not_a_gap(self, tmp_path):
        sink = ParquetSink(tmp_path, gap_threshold_sec=60.0)
        sink.write(
            {
                "rb2510": [
                    _tick(update_time="15:00:00"),
                    _tick(update_time="21:05:00"),  # 收盘到夜盘
                ]
            }
        )
        report = sink.finalize("20260916")
        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert entry.gaps == []

    def test_silence_inside_one_session_is_a_gap(self, tmp_path):
        sink = ParquetSink(tmp_path, gap_threshold_sec=60.0)
        sink.write(
            {
                "rb2510": [
                    _tick(update_time="09:00:00"),
                    _tick(update_time="09:05:00"),  # 同段内静默 5 分钟
                ]
            }
        )
        report = sink.finalize("20260916")
        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert len(entry.gaps) == 1
        assert entry.gaps[0]["seconds"] == pytest.approx(300.0)

    def test_night_session_spanning_midnight_is_one_session(self, tmp_path):
        sink = ParquetSink(tmp_path, gap_threshold_sec=60.0)
        sink.write(
            {
                "rb2510": [
                    _tick(action_day="20260915", update_time="23:59:00"),
                    _tick(action_day="20260916", update_time="00:01:00"),  # 跨午夜 120 秒
                ]
            }
        )
        report = sink.finalize("20260916")
        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        # 夜盘跨午夜仍属同一时段 -> 120 秒静默应判为缺口
        assert len(entry.gaps) == 1
        assert entry.gaps[0]["seconds"] == pytest.approx(120.0)

    def test_night_to_next_day_morning_is_not_a_gap(self, tmp_path):
        sink = ParquetSink(tmp_path, gap_threshold_sec=60.0)
        sink.write(
            {
                "rb2510": [
                    _tick(action_day="20260915", update_time="23:00:00"),
                    _tick(action_day="20260916", update_time="09:05:00"),  # 隔夜且跨时段
                ]
            }
        )
        report = sink.finalize("20260916")
        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert entry.gaps == []

    def test_outside_session_silence_is_not_a_gap(self, tmp_path):
        sink = ParquetSink(tmp_path, gap_threshold_sec=60.0, drop_outside_session=False)
        sink.write(
            {
                "rb2510": [
                    _tick(update_time="03:00:00"),
                    _tick(update_time="04:00:00"),  # 两者都在交易时段之外
                ]
            }
        )
        report = sink.finalize("20260916")
        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert entry.gaps == []


class TestConcurrentWriteProtection:
    """多进程重叠写同一最终文件时，必须有跨进程锁，否则读-改-写会丢数据。

    新实现里锁只在压缩阶段（把段合并成最终文件）持有，因此这里让两个 sink
    各自压缩，验证重叠分片下最终文件不会互相覆盖。
    """

    def test_concurrent_writers_do_not_lose_rows(self, tmp_path):
        import threading

        sink_a = ParquetSink(tmp_path)
        sink_b = ParquetSink(tmp_path)
        ticks_a = [_tick(update_millisec=index) for index in range(5)]
        ticks_b = [_tick(update_millisec=100 + index) for index in range(5)]
        errors: list[Exception] = []

        def worker(sink, ticks):
            try:
                for _ in range(5):
                    sink.write({"rb2510": ticks})
                sink.finalize("20260916")
            except Exception as exc:  # pragma: no cover - 失败时记录
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(sink_a, ticks_a)),
            threading.Thread(target=worker, args=(sink_b, ticks_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert len(rows) == 10  # 两组各 5 条，无覆盖丢失

    def test_lock_files_do_not_pollute_parquet_scan(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick()]})

        report = sink.finalize("20260916")

        assert [entry.instrument_id for entry in report.instruments] == ["rb2510"]

    def test_lock_files_live_outside_the_data_tree(self, tmp_path):
        """锁文件不得污染数据目录（整改方案 P1-7）。"""
        sink = ParquetSink(tmp_path)

        sink.write({"rb2510": [_tick()]})
        sink.finalize("20260916")

        assert list((tmp_path / "20260916").rglob("*.lock")) == []
        assert list((tmp_path / ".locks").rglob("*.lock"))

    def test_lock_is_scoped_per_trading_day_exchange_and_instrument(self, tmp_path):
        sink = ParquetSink(tmp_path)

        sink.write({"rb2510": [_tick()]})
        sink.finalize("20260916")

        locks = sorted(
            path.relative_to(tmp_path / ".locks").as_posix()
            for path in (tmp_path / ".locks").rglob("*.lock")
        )
        assert locks == ["20260916/SHFE/rb2510.parquet.lock"]


class TestMixedTradingDays:
    """一批数据横跨两个交易日（夜盘开盘瞬间）时，必须分目录写入。"""

    def test_each_trading_day_gets_its_own_directory(self, tmp_path):
        sink = ParquetSink(tmp_path)
        old_day = _tick(trading_day="20260916", update_time="15:00:00")
        new_day = _tick(trading_day="20260917", update_time="21:00:01")

        report = sink.write({"rb2510": [old_day, new_day]})
        sink.finalize("20260917")

        assert (tmp_path / "20260916" / "SHFE" / "rb2510.parquet").exists()
        assert (tmp_path / "20260917" / "SHFE" / "rb2510.parquet").exists()
        rows_old = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        rows_new = pq.read_table(tmp_path / "20260917" / "SHFE" / "rb2510.parquet").to_pylist()
        assert [r["update_time"] for r in rows_old] == ["15:00:00"]
        assert [r["update_time"] for r in rows_new] == ["21:00:01"]
        # 返回的 trading_day 取行数更多的一方，便于调用方记日志
        assert report.trading_day in {"20260916", "20260917"}
