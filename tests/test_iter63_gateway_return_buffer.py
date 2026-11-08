"""Volatile return buffer tests; no native connection or account is used."""

from dataclasses import FrozenInstanceError, replace
from threading import Thread

import pytest

from bt_api_ctp.gateway_return_buffer import (
    CtpGatewayReturnBuffer,
    CtpGatewayReturnBufferError,
)


def make_buffer(**kwargs):
    return CtpGatewayReturnBuffer(broker_id="b", investor_id="i", **kwargs)


def append(buffer, **changes):
    values = dict(
        kind="order",
        row={"TradingDay": "20261107", "Volume": 1},
        source_ref="source",
        source_tags=("a", "b", "c", "d", 1, 2),
        connection_generation=1,
        reception_trading_day="20261108",
        front_id=1,
        session_id=2,
    )
    values.update(changes)
    return buffer._append(**values)


def test_producer_offer_retry_append_ack_and_immutable_records():
    buffer = make_buffer()
    row = {"TradingDay": "20261107", "Price": 1.5}
    result = []
    producer = Thread(target=lambda: result.append(append(buffer, row=row)))
    producer.start()
    producer.join()
    assert result == [True]
    row["TradingDay"] = "changed"
    ticket = buffer.offer(limit=1)
    record = ticket.records[0]
    assert record.sequence == 1
    assert record.row["TradingDay"] == "20261107"
    assert record.reception_trading_day == "20261108"
    with pytest.raises(TypeError):
        record.row["Price"] = 2
    with pytest.raises(FrozenInstanceError):
        record.kind = "trade"
    with pytest.raises(FrozenInstanceError):
        ticket.records = ()
    assert append(buffer, kind="trade")
    assert buffer.offer(limit=2) is ticket
    buffer.ack(ticket)
    assert buffer.status().record_count == 1
    assert buffer.offer().records[0].sequence == 2
    with pytest.raises(CtpGatewayReturnBufferError, match="BATCH_UNTRUSTED"):
        buffer.ack(ticket)


def test_empty_offer_is_stable_and_does_not_ack_later_append():
    buffer = make_buffer()
    empty = buffer.offer()
    assert empty.records == ()
    assert append(buffer)
    assert buffer.offer() is empty
    buffer.ack(empty)
    assert len(buffer.offer().records) == 1


def test_offer_limit_and_membership_follow_each_ticket():
    buffer = make_buffer()
    for _ in range(257):
        assert append(buffer)
    with pytest.raises(CtpGatewayReturnBufferError, match="INVALID_LIMIT"):
        buffer.offer(limit=257)
    batch = buffer.offer(limit=256)
    assert len(batch.records) == 256
    assert all(buffer._contains_record(record) for record in batch.records)
    assert buffer.offer(limit=257) is batch
    buffer.ack(batch)
    next_batch = buffer.offer()
    assert len(next_batch.records) == 1
    assert buffer._contains_record(next_batch.records[0])
    assert not buffer._contains_record(batch.records[-1])


def test_ticket_identity_forgery_and_cross_buffer():
    left, right = make_buffer(), make_buffer()
    assert append(left)
    batch = left.offer()
    assert left._contains_record(batch.records[0])
    assert not left._contains_record(replace(batch.records[0]))
    assert not right._contains_record(batch.records[0])
    forged = replace(batch)
    with pytest.raises(CtpGatewayReturnBufferError, match="BATCH_UNTRUSTED"):
        left.ack(forged)
    with pytest.raises(CtpGatewayReturnBufferError, match="BATCH_UNTRUSTED"):
        right.ack(batch)
    assert left.status().record_count == 1
    left.ack(batch)
    assert not left._contains_record(batch.records[0])
    with pytest.raises(CtpGatewayReturnBufferError, match="BATCH_UNTRUSTED"):
        left.ack(batch)


def test_tampered_ticket_cannot_discard_rows():
    buffer = make_buffer()
    assert append(buffer)
    batch = buffer.offer()
    object.__setattr__(batch, "records", ())
    with pytest.raises(CtpGatewayReturnBufferError, match="BATCH_UNTRUSTED"):
        buffer.ack(batch)
    with pytest.raises(CtpGatewayReturnBufferError, match="BATCH_UNTRUSTED"):
        buffer.offer()
    with pytest.raises(CtpGatewayReturnBufferError, match="BATCH_UNTRUSTED"):
        buffer._contains_record(object())
    assert buffer.status().record_count == 1


def test_owner_thread_and_pid_checks(monkeypatch):
    buffer = make_buffer()
    batch = buffer.offer()
    errors = []

    def wrong_thread():
        for operation in (buffer.offer, buffer.status, lambda: buffer.ack(batch)):
            try:
                operation()
            except CtpGatewayReturnBufferError as exc:
                errors.append(exc.code)

    producer = Thread(target=wrong_thread)
    producer.start()
    producer.join()
    assert errors == ["OWNER_REQUIRED"] * 3
    monkeypatch.setattr("bt_api_ctp.gateway_return_buffer.os.getpid", lambda: buffer._pid + 1)
    for operation in (
        buffer.offer,
        buffer.status,
        lambda: buffer.ack(batch),
        lambda: append(buffer),
    ):
        with pytest.raises(CtpGatewayReturnBufferError, match="OWNER_REQUIRED"):
            operation()


def test_capacity_gap_persists_through_ack_and_recovery():
    buffer = make_buffer(capacity=1)
    assert append(buffer)
    ticket = buffer.offer()
    assert not append(buffer)
    assert buffer.status().gap_reason == "capacity_overflow"
    buffer.ack(ticket)
    assert buffer.status().byte_count == 0
    assert append(buffer)
    status = buffer.status()
    assert status.gap and status.gap_reason == "capacity_overflow"
    assert status.last_sequence == 2


def test_byte_limit_and_exact_boundary():
    probe = make_buffer()
    assert append(probe)
    size = probe.status().byte_count
    exact = make_buffer(max_bytes=size)
    assert append(exact)
    assert exact.status().byte_count == size
    assert not append(exact)
    assert exact.status().gap_reason == "byte_overflow"
    too_small = make_buffer(max_bytes=size - 1)
    assert not append(too_small)
    assert too_small.status().record_count == 0
    assert too_small.status().gap_reason == "byte_overflow"


@pytest.mark.parametrize(
    "changes",
    [
        {"row": {}},
        {"row": {"9bad": 1}},
        {"row": {"é": 1}},
        {"row": {"a" * 129: 1}},
        {"row": {"a": "x" * 4097}},
        {"row": {"field" + str(i): 1 for i in range(257)}},
        {"row": {"a": True}},
        {"row": {"a": 2**63}},
        {"row": {"a": float("nan")}},
        {"row": {"a": float("inf")}},
        {"row": {"a": object()}},
        {"row": {"a": "\ud800"}},
        {"source_ref": object()},
        {"source_ref": "x" * 257},
        {"source_tags": ("a", "b", "c", "d", 1, True)},
        {"connection_generation": 0},
        {"front_id": True},
        {"request_id": 2**63},
        {"is_last": 1},
        {"error_code": False},
        {"kind": "unknown"},
    ],
)
def test_invalid_record_marks_gap_and_preserves_existing(changes):
    buffer = make_buffer()
    assert append(buffer)
    assert not append(buffer, **changes)
    status = buffer.status()
    assert (status.record_count, status.last_sequence, status.gap_reason) == (
        1,
        1,
        "invalid_record",
    )
    assert len(buffer.offer().records) == 1


def test_source_gap_is_sticky_and_codes_are_finite():
    buffer = make_buffer()
    buffer._mark_gap("source_failure")
    assert not append(buffer, row={})
    assert buffer.status().gap_reason == "source_failure"
    with pytest.raises(CtpGatewayReturnBufferError, match="INVALID_GAP_CODE"):
        buffer._mark_gap("unbounded details")


@pytest.mark.parametrize(
    "values",
    [
        {"capacity": 0},
        {"capacity": 65537},
        {"capacity": True},
        {"max_bytes": 0},
        {"max_bytes": 64 * 1024 * 1024 + 1},
        {"max_bytes": False},
        {"broker_id": ""},
        {"investor_id": "x" * 257},
    ],
)
def test_configuration_bounds(values):
    values = {"broker_id": "b", "investor_id": "i", **values}
    with pytest.raises(CtpGatewayReturnBufferError, match="INVALID_CONFIGURATION"):
        CtpGatewayReturnBuffer(**values)


def test_valid_row_and_metadata_boundaries():
    buffer = make_buffer()
    row = {"a" * 128: "界" * 1365 + "x", "low": -(2**63), "high": 2**63 - 1}
    assert append(buffer, row=row, request_id=0, is_last=False, error_code=-1)
    record = buffer.offer().records[0]
    assert record.row == row
    assert record.request_id == 0 and record.is_last is False and record.error_code == -1
