"""Offline contracts for strict, raw CTP position query evidence."""

from __future__ import annotations

import inspect
import sys
import threading
import time
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from bt_api_ctp.containers.ctp.ctp_position_evidence import (
    CtpPositionEvidence,
    CtpPositionEvidenceError,
    build_ctp_position_evidence,
)
from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
from bt_api_ctp.query import QueryResult

UTC = timezone.utc
NOW = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
BROKER_ID = "fixture-broker"
INVESTOR_ID = "fixture-investor"


def _offline_client():
    """Use the real TraderClient query accumulator without native I/O."""

    client = TraderClient(
        "tcp://offline.invalid:0",
        BROKER_ID,
        INVESTOR_ID,
        "",
        auto_settlement_confirm=False,
    )
    client._connected = True
    client._authentication_state = "authenticated"
    client._login_state = "logged_in"
    client._trading_day = "20260910"
    client._connection_generation = 7
    client._req_id = 30
    client._query_interval = 0.0
    return client


TEST_CLIENT = _offline_client()
ACCOUNT_FINGERPRINT = TEST_CLIENT._account_fingerprint
SESSION = {
    "read_only_ready": True,
    "account_fingerprint": ACCOUNT_FINGERPRINT,
    "connection_generation": 7,
    "trading_day": "20260910",
    "broker_id": BROKER_ID,
    "investor_id": INVESTOR_ID,
}
FROZEN_FIELDS = {
    "LongFrozen": 0,
    "ShortFrozen": 0,
    "LongFrozenAmount": 0.0,
    "ShortFrozenAmount": 0.0,
    "FrozenMargin": 0.0,
    "FrozenCash": 0.0,
    "FrozenCommission": 0.0,
    "CombLongFrozen": 0,
    "CombShortFrozen": 0,
    "StrikeFrozen": 0,
    "StrikeFrozenAmount": 0.0,
    "YdStrikeFrozen": 0,
}


def position_row(**changes):
    row = {
        "InstrumentID": "m2701",
        "ExchangeID": "DCE",
        "PosiDirection": "2",
        "HedgeFlag": "1",
        "PositionDate": "2",
        "TradingDay": "20260910",
        "Position": 3,
        "TodayPosition": 1,
        "YdPosition": 2,
        **FROZEN_FIELDS,
    }
    row.update(changes)
    return row


def minimal_position_row(**changes):
    row = {
        "InstrumentID": "FG701C970",
        "ExchangeID": "CZCE",
        "PosiDirection": "2",
        "HedgeFlag": "1",
        "PositionDate": "2",
        "TradingDay": "20260910",
        "Position": 3,
        "TodayPosition": 1,
        "YdPosition": 4,
        "LongFrozen": 0,
        "ShortFrozen": 1,
    }
    row.update(changes)
    return row


def positions_result(records=(), *, live=False, **changes):
    """Create a source-issued result through the real callback accumulator."""

    accumulator = TEST_CLIENT._new_query_accumulator("positions")
    for record in records:
        TEST_CLIENT._handle_query_callback(
            "positions", record, None, accumulator.request_id, False
        )
    TEST_CLIENT._handle_query_callback(
        "positions", None, None, accumulator.request_id, True
    )
    if live:
        base = datetime.now(UTC)
        accumulator.started_at_utc = base - timedelta(seconds=1)
        accumulator.completed_at_utc = base - timedelta(milliseconds=10)
    else:
        accumulator.started_at_utc = NOW
        accumulator.completed_at_utc = NOW + timedelta(seconds=1)
    accumulator.completed_monotonic = time.monotonic()
    for name, value in changes.items():
        if name == "complete":
            accumulator.sealed = bool(value)
        elif hasattr(accumulator, name):
            setattr(accumulator, name, value)
        else:
            raise AssertionError(f"unknown QueryResult fixture field: {name}")
    accumulator.sealed = bool(changes.get("complete", True))
    result = accumulator.result()
    # Keep the historical envelope probes able to isolate each rejection
    # flag.  A production accumulator derives ``complete`` from these flags;
    # this fixture only forces it true when a test explicitly asks to inspect
    # the later validator branch.
    if "complete" not in changes and any(
        name in changes
        for name in ("is_last_seen", "error_code", "timed_out", "unsupported")
    ):
        object.__setattr__(result, "complete", True)
    return result


def bare_positions_result(records=(), **changes):
    """Build an intentionally unissued envelope for fail-closed tests."""

    values = {
        "request_type": "positions",
        "request_id": 31,
        "connection_generation": 7,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "started_at_utc": NOW,
        "completed_at_utc": NOW + timedelta(seconds=1),
        "is_last_seen": True,
        "error_code": None,
        "error_message": "",
        "timed_out": False,
        "complete": True,
        "records": tuple(records),
    }
    values.update(changes)
    return QueryResult(**values)


def build(
    result=None,
    *,
    session=None,
    now=None,
    expires=None,
    monotonic_now=None,
    monotonic_expires_at=None,
):
    result = result if result is not None else positions_result((position_row(),))
    source = result.query_source
    if source is not None:
        completed = source.completed_at_utc or NOW
        if now is None:
            now = completed + timedelta(seconds=2)
        if expires is None:
            expires = completed + timedelta(seconds=30)
        if monotonic_now is None and source.completed_monotonic is not None:
            monotonic_now = source.completed_monotonic + 2.0
        if monotonic_expires_at is None and source.completed_monotonic is not None:
            monotonic_expires_at = (
                source.completed_monotonic + (expires - completed).total_seconds()
            )
    else:
        now = NOW + timedelta(seconds=2) if now is None else now
        expires = NOW + timedelta(seconds=30) if expires is None else expires
    if session is None:
        session = (
            TEST_CLIENT.get_query_session_scope() if source is not None else SESSION
        )
    return build_ctp_position_evidence(
        result,
        session_state=session,
        now_utc=now,
        expires_at_utc=expires,
        monotonic_now=monotonic_now,
        monotonic_expires_at=monotonic_expires_at,
    )


def test_complete_query_preserves_raw_identity_quantities_and_source_hash():
    raw = position_row(InstrumentID="m2701", Position=0, TodayPosition=0, YdPosition=0)
    evidence = build(positions_result((raw,)))

    assert isinstance(evidence, CtpPositionEvidence)
    assert evidence.complete is True
    assert evidence.is_empty is False
    row = evidence.rows[0]
    assert row.instrument_id == "m2701"
    assert row.exchange_id == "DCE"
    assert row.posi_direction == "2"
    assert row.hedge_flag == "1"
    assert row.position_date == "2"
    assert row.trading_day == "20260910"
    assert row.field("Position").state == "value"
    assert row.field("Position").value == 0
    assert row.field("Position").raw_value == 0
    assert row.field("Position").is_explicit_zero is True
    assert len(evidence.source_hash) == 64
    assert evidence.source_hash == evidence.source_hash.lower()

    raw["InstrumentID"] = "MUTATED"
    raw["Position"] = 99
    assert row.instrument_id == "m2701"
    assert row.position == 0


def test_independent_raw_rows_keep_exchange_identity_and_do_not_derive_buckets():
    rows = (
        minimal_position_row(),
        minimal_position_row(
            InstrumentID="au2612",
            ExchangeID="SHFE",
            PositionDate="1",
            Position=2,
            TodayPosition=2,
            YdPosition=0,
            ShortFrozen=0,
        ),
        minimal_position_row(
            InstrumentID="au2612",
            ExchangeID="SHFE",
            PositionDate="2",
            Position=5,
            TodayPosition=0,
            YdPosition=5,
        ),
        minimal_position_row(
            InstrumentID="au2612",
            ExchangeID="SHFE",
            PosiDirection="3",
            HedgeFlag="3",
            PositionDate="1",
            Position=1,
            TodayPosition=1,
            YdPosition=0,
        ),
    )

    evidence = build(positions_result(rows))

    assert len(evidence.rows) == 4
    assert evidence.rows[0].position == 3
    assert evidence.rows[0].today_position == 1
    assert evidence.rows[0].yd_position == 4
    assert [
        (
            row.instrument_id,
            row.exchange_id,
            row.posi_direction,
            row.hedge_flag,
            row.position_date,
        )
        for row in evidence.rows[1:]
    ] == [
        ("au2612", "SHFE", "2", "1", "1"),
        ("au2612", "SHFE", "2", "1", "2"),
        ("au2612", "SHFE", "3", "3", "1"),
    ]


def test_plain_mapping_session_scope_is_not_trusted_as_live_scope():
    session = {**SESSION, "synthetic": True}

    with pytest.raises(
        CtpPositionEvidenceError, match="position_session_scope_untrusted"
    ):
        build(session=session)


def test_public_feed_adapter_consumes_query_positions_result_without_writes():
    feed = object.__new__(CtpRequestDataFuture)
    calls = []
    result = positions_result((position_row(),), live=True)
    feed._ensure_connected = lambda: calls.append("connected")
    feed.query_positions_result = lambda timeout=5.0: (
        calls.append(("query", timeout)) or result
    )
    feed.get_query_session_scope = lambda: TEST_CLIENT.get_query_session_scope()

    evidence = feed.query_positions_evidence(timeout=2.0, ttl_seconds=30.0)

    assert isinstance(evidence, CtpPositionEvidence)
    assert calls == ["connected", ("query", 2.0)]
    assert evidence.query_request_id > 0


def test_complete_empty_query_proves_empty_set_only_with_current_scope():
    evidence = build(positions_result(()))
    assert evidence.complete is True
    assert evidence.is_empty is True
    assert evidence.rows == ()

    with pytest.raises(CtpPositionEvidenceError, match="position_query_incomplete"):
        build(positions_result((), complete=False, is_last_seen=False, timed_out=True))


@pytest.mark.parametrize(
    "field",
    [
        "InstrumentID",
        "ExchangeID",
        "PosiDirection",
        "HedgeFlag",
        "PositionDate",
        "TradingDay",
        "Position",
        "TodayPosition",
        "YdPosition",
        "LongFrozen",
        "ShortFrozen",
    ],
)
def test_missing_position_field_is_explicitly_distinguished_and_rejected(field):
    raw = position_row()
    del raw[field]

    with pytest.raises(CtpPositionEvidenceError) as raised:
        build(positions_result((raw,)))

    assert raised.value.code == "position_row_incomplete"
    assert raised.value.rows[0].field(field).state == "missing"


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1, 0.5])
@pytest.mark.parametrize(
    "field", ["Position", "TodayPosition", "YdPosition", "LongFrozen"]
)
def test_invalid_position_quantity_is_unknown_and_rejected(field, value):
    raw = position_row(**{field: value})

    with pytest.raises(CtpPositionEvidenceError) as raised:
        build(positions_result((raw,)))

    assert raised.value.code == "position_row_incomplete"
    assert raised.value.rows[0].field(field).state == "unknown"


@pytest.mark.parametrize(
    "field,value",
    [
        ("PosiDirection", "9"),
        ("HedgeFlag", "9"),
        ("PositionDate", "9"),
        ("TradingDay", "20260931"),
        ("InstrumentID", ""),
        ("ExchangeID", ""),
    ],
)
def test_unknown_position_identity_is_not_defaulted(field, value):
    raw = position_row(**{field: value})

    with pytest.raises(CtpPositionEvidenceError) as raised:
        build(positions_result((raw,)))

    assert raised.value.code == "position_row_incomplete"
    assert raised.value.rows[0].field(field).state == "unknown"


def test_same_instrument_with_different_hedge_or_position_date_is_retained():
    rows = (
        position_row(
            HedgeFlag="1", PositionDate="1", Position=1, TodayPosition=1, YdPosition=0
        ),
        position_row(
            HedgeFlag="2", PositionDate="2", Position=2, TodayPosition=0, YdPosition=2
        ),
    )
    evidence = build(positions_result(rows))

    assert [
        (row.instrument_id, row.hedge_flag, row.position_date) for row in evidence.rows
    ] == [
        ("m2701", "1", "1"),
        ("m2701", "2", "2"),
    ]

    with pytest.raises(CtpPositionEvidenceError, match="position_row_duplicate"):
        build(positions_result((position_row(), position_row())))


@pytest.mark.parametrize(
    "change",
    [
        {"account_fingerprint": "other-account"},
        {"connection_generation": 8},
    ],
)
def test_query_scope_mismatch_is_rejected(change):
    with pytest.raises(CtpPositionEvidenceError, match="position_query_scope_mismatch"):
        build(positions_result((position_row(),), **change))


@pytest.mark.parametrize(
    "session_change,code",
    [
        ({"account_fingerprint": "other-account"}, "position_query_scope_mismatch"),
        ({"connection_generation": 8}, "position_query_scope_mismatch"),
        ({"trading_day": "20260911"}, "position_query_scope_mismatch"),
        ({"read_only_ready": False}, "position_session_not_read_only_ready"),
    ],
)
def test_current_session_scope_mismatch_is_rejected(session_change, code):
    session = replace(TEST_CLIENT.get_query_session_scope(), **session_change)

    with pytest.raises(CtpPositionEvidenceError, match=code):
        build(session=session)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"is_last_seen": False}, "position_query_not_terminal"),
        ({"error_code": 7}, "position_query_error"),
        ({"timed_out": True}, "position_query_timed_out"),
        ({"unsupported": True}, "position_query_unsupported"),
    ],
)
def test_incomplete_query_envelopes_cannot_prove_positions(change, code):
    with pytest.raises(CtpPositionEvidenceError, match=code):
        build(positions_result((position_row(),), **change))


def test_query_times_and_expiry_are_aware_current_and_bound():
    with pytest.raises(CtpPositionEvidenceError, match="position_query_clock_invalid"):
        build(
            positions_result((position_row(),), started_at_utc=NOW.replace(tzinfo=None))
        )

    with pytest.raises(CtpPositionEvidenceError, match="position_query_expired"):
        build(expires=NOW + timedelta(seconds=1.5), now=NOW + timedelta(seconds=2))

    with pytest.raises(CtpPositionEvidenceError, match="position_query_time_invalid"):
        build(
            positions_result(
                (position_row(),),
                started_at_utc=NOW + timedelta(hours=1),
                completed_at_utc=NOW + timedelta(hours=1, seconds=1),
            ),
            now=NOW,
        )


def test_evidence_is_immutable_and_does_not_expose_offset_policy():
    evidence = build()

    with pytest.raises(FrozenInstanceError):
        evidence.trading_day = "20260911"
    with pytest.raises(TypeError):
        evidence.query_envelope["request_id"] = 99
    with pytest.raises(TypeError):
        evidence.rows[0].raw_record["Position"] = 99

    assert evidence.offset_policy is None
    assert evidence.rows[0].position == 3
    assert evidence.rows[0].today_position == 1
    assert evidence.rows[0].yd_position == 2


def test_feed_adapter_requires_session_day_instead_of_caller_override():
    feed = object.__new__(CtpRequestDataFuture)
    result = positions_result((position_row(),), live=True)
    feed._ensure_connected = lambda: None
    feed.query_positions_result = lambda timeout=5.0: result
    feed.get_query_session_scope = lambda: replace(
        TEST_CLIENT.get_query_session_scope(), trading_day=""
    )

    with pytest.raises(
        CtpPositionEvidenceError, match="position_session_trading_day_missing"
    ):
        feed.query_positions_evidence(timeout=1.0, ttl_seconds=30.0)


def test_public_query_result_record_is_detached_from_later_input_mutation():
    raw = position_row()
    result = positions_result((raw,))
    evidence = build(result)
    raw["LongFrozen"] = 99

    assert evidence.rows[0].long_frozen == 0
    assert evidence.query_envelope["request_id"] == result.request_id


def test_optional_frozen_fields_remain_missing_without_becoming_zero():
    raw = position_row()
    del raw["FrozenMargin"]

    evidence = build(positions_result((raw,)))

    assert evidence.rows[0].field("FrozenMargin").state == "missing"
    assert evidence.rows[0].complete is True


def test_unissued_query_and_matching_mapping_cannot_certify_empty_account():
    with pytest.raises(
        CtpPositionEvidenceError, match="position_session_scope_untrusted"
    ):
        build(
            bare_positions_result(()),
            session=dict(SESSION),
            now=NOW + timedelta(seconds=2),
            expires=NOW + timedelta(seconds=30),
        )


def test_typed_scope_is_issuer_bound_even_when_account_strings_match():
    result = positions_result(())
    other_client = _offline_client()
    other_client._account_fingerprint = ACCOUNT_FINGERPRINT
    scope = other_client.get_query_session_scope()

    with pytest.raises(CtpPositionEvidenceError, match="position_query_scope_mismatch"):
        build(result, session=scope)


def test_query_expiry_is_bound_to_completion_in_both_clock_domains():
    result = positions_result(())
    source = result.query_source
    assert source is not None

    stale_now = source.completed_at_utc + timedelta(seconds=5, microseconds=1)
    with pytest.raises(CtpPositionEvidenceError, match="position_query_expired"):
        build(
            result,
            now=stale_now,
            expires=source.completed_at_utc + timedelta(seconds=5),
            monotonic_now=source.completed_monotonic + 5.000001,
            monotonic_expires_at=source.completed_monotonic + 5,
        )


def test_caller_deadlines_cannot_renew_a_trusted_query_source():
    result = positions_result(())
    source = result.query_source
    assert source is not None

    with pytest.raises(CtpPositionEvidenceError, match="position_query_expired"):
        build(
            result,
            now=source.completed_at_utc + timedelta(seconds=5, microseconds=1),
            expires=source.completed_at_utc + timedelta(seconds=10),
            monotonic_now=source.completed_monotonic + 5.000001,
            monotonic_expires_at=source.completed_monotonic + 10,
        )


def test_mutating_a_public_query_record_breaks_trusted_source_digest():
    result = positions_result((position_row(),))
    result.records[0]["Position"] = 999

    with pytest.raises(
        CtpPositionEvidenceError, match="position_query_payload_mismatch"
    ):
        build(result)


def test_record_snapshot_wins_when_result_mutates_during_strict_parse():
    result = positions_result((position_row(),))
    source_lines, first_line = inspect.getsourcelines(build_ctp_position_evidence)
    parse_line = next(
        first_line + offset
        for offset, line in enumerate(source_lines)
        if "rows = tuple(parse_ctp_position_row(record) for record in result.records)"
        in line
    )
    ready = threading.Event()
    done = threading.Event()
    trace_hit = threading.Event()

    def mutate_result():
        if not ready.wait(2.0):
            return
        result.records[0]["Position"] = 999
        done.set()

    worker = threading.Thread(target=mutate_result)
    worker.start()

    def trace(frame, event, _arg):
        if (
            event == "line"
            and frame.f_code is build_ctp_position_evidence.__code__
            and frame.f_lineno == parse_line
            and not trace_hit.is_set()
        ):
            trace_hit.set()
            ready.set()
            if not done.wait(2.0):
                raise RuntimeError("mutation thread timed out")
        return trace

    try:
        sys.settrace(trace)
        evidence = build(result)
    finally:
        sys.settrace(None)
        ready.set()
        worker.join(2.0)

    assert not worker.is_alive()
    assert trace_hit.is_set()
    assert result.records[0]["Position"] == 999
    assert evidence.rows[0].position == 3


def test_terminal_callback_clocks_survive_a_slow_native_query_return():
    class DelayedQueryApi:
        def __init__(self, client):
            self.client = client
            self.callback_monotonic = None

        def ReqQryInvestorPosition(self, _field, request_id):
            self.client._handle_query_callback(
                "positions", position_row(), None, request_id, True
            )
            self.callback_monotonic = time.monotonic()
            time.sleep(0.04)
            return 0

    client = TraderClient(
        "tcp://offline.invalid:0",
        "BRK",
        "INV",
        "",
        auto_settlement_confirm=False,
    )
    client._connected = True
    client._authentication_state = "authenticated"
    client._login_state = "logged_in"
    client._trading_day = "20260910"
    client._connection_generation = 7
    client._query_interval = 0.0
    api = DelayedQueryApi(client)
    client._api = api
    result = client.query_positions_result(timeout=1.0)
    source = result.query_source
    assert source is not None
    assert api.callback_monotonic is not None
    assert source.completed_monotonic <= api.callback_monotonic

    scope = client.get_query_session_scope()
    with pytest.raises(CtpPositionEvidenceError, match="position_query_expired"):
        build_ctp_position_evidence(
            result,
            session_state=scope,
            expires_at_utc=source.completed_at_utc + timedelta(seconds=0.01),
            monotonic_now=source.completed_monotonic + 0.04,
            monotonic_expires_at=source.completed_monotonic + 0.01,
        )


def test_present_row_account_fields_are_checked_against_bound_session():
    raw = position_row(BrokerID=BROKER_ID, InvestorID=INVESTOR_ID)
    assert build(positions_result((raw,))).rows[0].broker_id == BROKER_ID

    with pytest.raises(CtpPositionEvidenceError, match="position_row_account_mismatch"):
        build(
            positions_result(
                (position_row(BrokerID="other-broker", InvestorID=INVESTOR_ID),)
            )
        )


class _MutableQuantity:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return str(self.value)


def test_mutable_quantity_is_unknown_instead_of_being_normalized_by_str():
    raw = position_row(Position=_MutableQuantity(3))

    with pytest.raises(CtpPositionEvidenceError) as raised:
        build(positions_result((raw,)))

    assert raised.value.code == "position_row_incomplete"
    assert raised.value.rows[0].field("Position").state == "unknown"


def test_feed_adapter_uses_query_source_completion_for_delayed_scope_capture():
    class DelayedClient(TraderClient):
        def get_session_state(self):
            time.sleep(0.02)
            return super().get_session_state()

    client = DelayedClient(
        "tcp://offline.invalid:0",
        BROKER_ID,
        INVESTOR_ID,
        "",
        auto_settlement_confirm=False,
    )
    client._connected = True
    client._authentication_state = "authenticated"
    client._login_state = "logged_in"
    client._trading_day = "20260910"
    client._connection_generation = 7
    client._query_interval = 0.0
    # Build the empty query through this delayed client so the feed's opaque
    # issuer matches the strict scope returned below.
    accumulator = client._new_query_accumulator("positions")
    client._handle_query_callback("positions", None, None, accumulator.request_id, True)
    base = datetime.now(UTC)
    accumulator.started_at_utc = base - timedelta(seconds=1)
    accumulator.completed_at_utc = base - timedelta(milliseconds=10)
    accumulator.completed_monotonic = time.monotonic()
    accumulator.sealed = True
    result = accumulator.result()
    feed = object.__new__(CtpRequestDataFuture)
    feed._ensure_connected = lambda: None
    feed.query_positions_result = lambda timeout=5.0: result
    feed.get_query_session_scope = client.get_query_session_scope

    with pytest.raises(CtpPositionEvidenceError, match="position_query_expired"):
        feed.query_positions_evidence(timeout=1.0, ttl_seconds=0.01)
