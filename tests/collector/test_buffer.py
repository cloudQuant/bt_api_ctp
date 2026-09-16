"""离线契约测试：per-instrument tick 缓冲与上限保护。"""

from __future__ import annotations

import pytest

from bt_api_ctp.collector.buffer import TickBuffer
from bt_api_ctp.collector.protocols import TickRecord


def _tick(instrument_id: str = "rb2510", **overrides) -> TickRecord:
    base = {
        "exchange_id": "SHFE",
        "instrument_id": instrument_id,
        "trading_day": "20260916",
        "action_day": "20260916",
        "update_time": "09:00:00",
        "update_millisec": 0,
        "local_receive_time": 1,
    }
    base.update(overrides)
    return TickRecord(**base)


class TestTickBuffer:
    def test_append_and_pending(self):
        buffer = TickBuffer(per_instrument_cap=10)
        assert buffer.append(_tick()) is True
        assert buffer.pending() == 1
        assert buffer.instruments() == ["rb2510"]

    def test_drain_groups_by_instrument_and_clears(self):
        buffer = TickBuffer(per_instrument_cap=10)
        buffer.append(_tick("rb2510"))
        buffer.append(_tick("cu2510"))
        buffer.append(_tick("rb2510"))

        drained = buffer.drain()

        assert set(drained) == {"rb2510", "cu2510"}
        assert len(drained["rb2510"]) == 2
        assert len(drained["cu2510"]) == 1
        assert buffer.pending() == 0
        assert buffer.instruments() == []
        assert buffer.drain() == {}

    def test_drain_preserves_arrival_order(self):
        buffer = TickBuffer(per_instrument_cap=10)
        for millisec in (300, 100, 200):
            buffer.append(_tick(update_millisec=millisec))
        drained = buffer.drain()
        assert [tick.update_millisec for tick in drained["rb2510"]] == [300, 100, 200]

    def test_drop_policy_discards_at_cap(self):
        buffer = TickBuffer(per_instrument_cap=2, overflow_policy="drop")
        assert buffer.append(_tick()) is True
        assert buffer.append(_tick()) is True
        assert buffer.append(_tick()) is False
        assert buffer.dropped_count() == 1
        assert buffer.pending() == 2
        assert buffer.should_flush() is True

    def test_flush_policy_keeps_data_up_to_hard_cap(self):
        buffer = TickBuffer(per_instrument_cap=2, overflow_policy="flush")
        buffer.append(_tick())
        assert buffer.should_flush() is False
        buffer.append(_tick())
        assert buffer.should_flush() is True

        # 软上限之上继续接收，不丢数据（engine 会尽快 drain）
        assert buffer.append(_tick()) is True
        assert buffer.append(_tick()) is True
        assert buffer.pending() == 4
        assert buffer.dropped_count() == 0

        # 硬上限（cap * 2）之上才丢弃
        assert buffer.append(_tick()) is False
        assert buffer.dropped_count() == 1

    def test_drain_resets_flush_request(self):
        buffer = TickBuffer(per_instrument_cap=1, overflow_policy="flush")
        buffer.append(_tick())
        assert buffer.should_flush() is True
        buffer.drain()
        assert buffer.should_flush() is False

    def test_instruments_are_isolated(self):
        buffer = TickBuffer(per_instrument_cap=2, overflow_policy="drop")
        buffer.append(_tick("a"))
        buffer.append(_tick("a"))
        assert buffer.append(_tick("a")) is False
        assert buffer.append(_tick("b")) is True
        assert buffer.dropped_count() == 1

    def test_invalid_cap_rejected(self):
        with pytest.raises(ValueError):
            TickBuffer(per_instrument_cap=0)

    def test_invalid_overflow_policy_rejected(self):
        with pytest.raises(ValueError):
            TickBuffer(overflow_policy="bogus")

    def test_empty_buffer_state(self):
        buffer = TickBuffer()
        assert buffer.pending() == 0
        assert buffer.dropped_count() == 0
        assert buffer.should_flush() is False
        assert buffer.instruments() == []
