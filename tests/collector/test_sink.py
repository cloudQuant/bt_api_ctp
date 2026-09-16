"""离线契约测试：Parquet 落盘、去重排序、中断恢复合并与完整性报告。"""

from __future__ import annotations

import json

import pyarrow.parquet as pq
import pytest

from bt_api_ctp.collector.protocols import TickRecord
from bt_api_ctp.collector.sink import TICK_ARROW_SCHEMA, ParquetSink


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


class TestParquetSinkLayout:
    def test_write_creates_parquet_with_fixed_schema(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick()]})

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
        assert (tmp_path / "20260916" / "SHFE" / "rb2510.parquet").exists()
        assert (tmp_path / "20260916" / "SHFE" / "cu2510.parquet").exists()

    def test_night_session_is_filed_under_next_trading_day(self, tmp_path):
        sink = ParquetSink(tmp_path)
        night = _tick(trading_day="20260916", action_day="20260915", update_time="21:30:00")
        day = _tick(trading_day="20260916", action_day="20260916", update_time="09:05:00")

        sink.write({"rb2510": [night, day]})

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

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert len(rows) == 1
        assert rows[0]["last_price"] == 2.0
        assert rows[0]["local_receive_time"] == 200

    def test_distinct_millisec_in_same_second_is_kept(self, tmp_path):
        sink = ParquetSink(tmp_path)
        sink.write({"rb2510": [_tick(update_millisec=0), _tick(update_millisec=500)]})
        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert len(rows) == 2

    def test_orders_by_time_key(self, tmp_path):
        sink = ParquetSink(tmp_path)
        ticks = [
            _tick(update_time="09:00:00", update_millisec=500),
            _tick(update_time="08:59:59", update_millisec=0),
            _tick(update_time="09:00:00", update_millisec=0),
        ]
        sink.write({"rb2510": ticks})

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert [(row["update_time"], row["update_millisec"]) for row in rows] == [
            ("08:59:59", 0),
            ("09:00:00", 0),
            ("09:00:00", 500),
        ]

    def test_merge_existing_is_idempotent(self, tmp_path):
        sink = ParquetSink(tmp_path, merge_existing=True)
        ticks = [_tick(update_time="09:00:00"), _tick(update_time="09:00:01")]

        sink.write({"rb2510": ticks})
        sink.write({"rb2510": ticks})  # 中断后重跑同一批数据

        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        assert len(rows) == 2

    def test_merge_appends_new_ticks(self, tmp_path):
        sink = ParquetSink(tmp_path, merge_existing=True)
        sink.write({"rb2510": [_tick(update_time="09:00:00")]})
        sink.write({"rb2510": [_tick(update_time="09:00:01")]})

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


class TestParquetSinkReport:
    def test_write_returns_per_instrument_stats(self, tmp_path):
        sink = ParquetSink(tmp_path)
        report = sink.write(
            {
                "rb2510": [
                    _tick(update_time="09:00:00"),
                    _tick(update_time="09:00:01"),
                ]
            }
        )
        assert report.trading_day == "20260916"
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
        rows = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        report = sink.finalize("20260916")

        entry = {e.instrument_id: e for e in report.instruments}["rb2510"]
        assert len(entry.gaps) == 1
        assert entry.gaps[0]["seconds"] == pytest.approx(120.0)
        del rows  # 仅用于确认落盘可读

    def test_finalize_without_files_is_empty_report(self, tmp_path):
        sink = ParquetSink(tmp_path)
        report = sink.finalize("20260916")
        assert report.instruments == []
        assert (tmp_path / "20260916" / "report.json").exists()


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
        sink = ParquetSink(tmp_path, gap_threshold_sec=60.0)
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
    """多进程重叠写同一文件时，必须有跨进程锁，否则读-改-写会丢数据。"""

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


class TestMixedTradingDays:
    """一批数据横跨两个交易日（夜盘开盘瞬间）时，必须分目录写入。"""

    def test_each_trading_day_gets_its_own_directory(self, tmp_path):
        sink = ParquetSink(tmp_path)
        old_day = _tick(trading_day="20260916", update_time="20:59:59")
        new_day = _tick(trading_day="20260917", update_time="21:00:01")

        report = sink.write({"rb2510": [old_day, new_day]})

        assert (tmp_path / "20260916" / "SHFE" / "rb2510.parquet").exists()
        assert (tmp_path / "20260917" / "SHFE" / "rb2510.parquet").exists()
        rows_old = pq.read_table(tmp_path / "20260916" / "SHFE" / "rb2510.parquet").to_pylist()
        rows_new = pq.read_table(tmp_path / "20260917" / "SHFE" / "rb2510.parquet").to_pylist()
        assert [r["update_time"] for r in rows_old] == ["20:59:59"]
        assert [r["update_time"] for r in rows_new] == ["21:00:01"]
        # 返回的 trading_day 取行数更多的一方，便于调用方记日志
        assert report.trading_day in {"20260916", "20260917"}
