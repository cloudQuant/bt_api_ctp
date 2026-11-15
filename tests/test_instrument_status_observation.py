"""UNIT/FAKE coverage for the provider-local OnRtnInstrumentStatus projection.

Every native callback here is a stand-in ``SimpleNamespace``.  No account,
front, network, native binary or wall clock is used.  The ingress builder is
the provider-local fake callback surface; it assigns the source sequence and
captures only the exact safe fields.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from bt_api_ctp import instrument_status_observation as module
from bt_api_ctp.ctp import callback_ingress as ingress
from bt_api_ctp.instrument_status_observation import (
    CODE_DUPLICATE,
    CODE_FIELD_INVALID,
    CODE_FIELDS_INCOMPLETE,
    CODE_FIELDS_INVALID,
    CODE_FIELDS_UNEXPECTED,
    CODE_GENERATION_INVALID,
    CODE_GENERATION_MISMATCH,
    CODE_IDENTITY_CONFLICT,
    CODE_NEVER_OBSERVED,
    CODE_OBSERVATION_INVALID,
    CODE_RECEIVE_TIME_INVALID,
    CODE_SEQUENCE_CONFLICT,
    CODE_SEQUENCE_INVALID,
    CODE_SEQUENCE_OUT_OF_ORDER,
    CODE_SESSION_INVALID,
    CODE_SOURCE_UNAVAILABLE,
    CODE_STALE_GENERATION,
    CODE_STALE_TRADING_DAY,
    CODE_STATUS_UNKNOWN,
    CODE_TRADING_DAY_INVALID,
    CODE_TRADING_DAY_MISMATCH,
    ENTER_REASON_AUTOMATIC,
    ENTER_REASON_FUSE,
    ENTER_REASON_MANUAL,
    ENTER_REASON_UNKNOWN,
    NATIVE_ENTER_REASONS,
    NATIVE_STATUS_CATEGORIES,
    RECEIVE_CLOCK_MONOTONIC_NS,
    RECEIVE_CLOCK_UTC_ISO8601,
    STATE_CURRENT,
    STATE_STALE,
    STATE_UNKNOWN,
    STATUS_AUCTION_BALANCE,
    STATUS_AUCTION_MATCH,
    STATUS_AUCTION_ORDERING,
    STATUS_BEFORE_TRADING,
    STATUS_CLOSED,
    STATUS_CONTINUOUS,
    STATUS_NO_TRADING,
    STATUS_TRANSACTION_PROCESSING,
    STATUS_UNKNOWN,
    CtpInstrumentStatusObservationError,
    InstrumentStatusLedger,
    InstrumentStatusObservation,
    InstrumentStatusSessionFacts,
    native_fields_from_ingress_payload,
    project_native_instrument_status,
)


def _fields(**overrides):
    values = {
        "ExchangeID": "SHFE",
        "ExchangeInstID": "rb2701",
        "InstrumentID": "rb2701",
        "InstrumentStatus": "2",
        "TradingSegmentSN": 2,
        "EnterTime": "09:00:00",
        "EnterReason": "1",
    }
    values.update(overrides)
    return values


def _session(**overrides):
    values = {
        "session_generation": "gen-1",
        "trading_day": "20261116",
        "source_sequence": 1,
        "receive_clock_domain": RECEIVE_CLOCK_MONOTONIC_NS,
        "receive_time": 1_000,
        "source_available": True,
    }
    values.update(overrides)
    return InstrumentStatusSessionFacts(**values)


_DEFAULT = object()


def _project(fields=_DEFAULT, session=None):
    values = _fields() if fields is _DEFAULT else fields
    return project_native_instrument_status(values, session=session or _session())


def _observation(
    *,
    status="2",
    generation="gen-1",
    trading_day="20261116",
    sequence=1,
    symbol="rb2701",
    exchange_symbol="rb2701",
    receive_time=1_000,
    reason="1",
):
    return _project(
        _fields(
            InstrumentID=symbol,
            ExchangeInstID=exchange_symbol,
            InstrumentStatus=status,
            EnterReason=reason,
        ),
        _session(
            session_generation=generation,
            trading_day=trading_day,
            source_sequence=sequence,
            receive_time=receive_time,
        ),
    )


def _tagged_native(**overrides):
    values = {
        "ExchangeID": "SHFE",
        "ExchangeInstID": "rb2701",
        "InstrumentID": "rb2701",
        "InstrumentStatus": "2",
        "TradingSegmentSN": 2,
        "EnterTime": "09:00:00",
        "EnterReason": "1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ingress_record(native, *, sequence=1, callback_monotonic_ns=777):
    return ingress.build_callback_ingress_record_v2(
        owner_intent_id="intent-1",
        callback_name="OnRtnInstrumentStatus",
        source_phase=ingress.CtpTraderCallbackPhase.ACTIVE,
        source_tags=ingress.CtpTraderCallbackSourceTagsV2(
            source_instance_id="source-1",
            native_client_epoch="epoch-1",
            native_api_source_id="api-1",
            native_spi_source_id="spi-1",
            native_api_generation=1,
            connection_generation=0,
        ),
        connection_generation=0,
        source_sequence=sequence,
        callback_monotonic_ns=callback_monotonic_ns,
        args=(native,),
    )


# --------------------------------------------------------------------------- #
# 1. Valid projection and neutral categories
# --------------------------------------------------------------------------- #


def test_valid_native_projection_is_neutral_versioned_and_value_free():
    fields = _fields()
    before = dict(fields)
    result = _project(fields)
    assert result.accepted is True and result.reason_code is None
    assert result.schema == module.OBSERVATION_SCHEMA
    assert (result.venue, result.native_symbol, result.exchange_symbol) == (
        "SHFE",
        "rb2701",
        "rb2701",
    )
    assert (result.status_category, result.native_status_code) == (STATUS_CONTINUOUS, "2")
    assert result.enter_reason == ENTER_REASON_AUTOMATIC
    assert (result.enter_time, result.trading_segment_sn) == ("09:00:00", 2)
    assert (result.trading_day, result.session_generation) == ("20261116", "gen-1")
    assert result.source_sequence == 1
    assert result.received_at == 1_000
    assert result.receive_clock_domain == RECEIVE_CLOCK_MONOTONIC_NS
    assert fields == before
    # A validated scalar projection keeps no raw payload.
    assert "ExchangeInstID" not in repr(result)
    assert "reserve" not in repr(result)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0", STATUS_BEFORE_TRADING),
        ("1", STATUS_NO_TRADING),
        ("2", STATUS_CONTINUOUS),
        ("3", STATUS_AUCTION_ORDERING),
        ("4", STATUS_AUCTION_BALANCE),
        ("5", STATUS_AUCTION_MATCH),
        ("6", STATUS_CLOSED),
        ("7", STATUS_TRANSACTION_PROCESSING),
    ],
)
def test_each_native_status_maps_to_distinct_neutral_category(raw, expected):
    result = _project(_fields(InstrumentStatus=raw))
    assert (result.accepted, result.status_category, result.native_status_code) == (
        True,
        expected,
        raw,
    )


@pytest.mark.parametrize("raw", ["8", "9", "A", "z", "#"])
def test_unknown_native_status_is_explicit_unknown_not_guessed(raw):
    result = _project(_fields(InstrumentStatus=raw))
    assert result.accepted is True
    assert result.status_category == STATUS_UNKNOWN
    assert result.native_status_code == raw


@pytest.mark.parametrize(
    "raw,expected",
    [("1", ENTER_REASON_AUTOMATIC), ("2", ENTER_REASON_MANUAL), ("3", ENTER_REASON_FUSE)],
)
def test_enter_reason_is_independent_and_neutral(raw, expected):
    result = _project(_fields(EnterReason=raw))
    assert (result.status_category, result.enter_reason) == (STATUS_CONTINUOUS, expected)


@pytest.mark.parametrize("raw", ["0", "4", "F", "?", "9"])
def test_unknown_enter_reason_is_explicit_unknown(raw):
    result = _project(_fields(EnterReason=raw))
    assert result.accepted is True
    assert result.enter_reason == ENTER_REASON_UNKNOWN


def test_mapping_tables_do_not_guess_beyond_documented_codes():
    assert set(NATIVE_STATUS_CATEGORIES) == set("01234567")
    assert set(NATIVE_ENTER_REASONS) == {"1", "2", "3"}


# --------------------------------------------------------------------------- #
# 2. Effective scope, generation invalidation and ordering
# --------------------------------------------------------------------------- #


def test_generation_change_invalidates_and_old_generation_cannot_resurrect():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    assert ledger.observe(_observation(status="6")).applied is True
    view = ledger.view("SHFE", "rb2701")
    assert (view.state, view.effective_status_category, view.usable) == (
        STATE_CURRENT,
        STATUS_CLOSED,
        True,
    )

    change = ledger.begin_session_generation("gen-2", "20261116")
    assert change.action == "invalidated" and change.reason_code == CODE_STALE_GENERATION
    stale = ledger.view("SHFE", "rb2701")
    assert (stale.state, stale.effective_status_category, stale.usable) == (
        STATE_STALE,
        STATUS_UNKNOWN,
        False,
    )
    assert stale.observed_status_category == STATUS_CLOSED
    assert stale.reason_code == CODE_STALE_GENERATION

    old = ledger.observe(_observation(status="6", generation="gen-1", sequence=2))
    assert old.applied is False
    assert old.action == "stale_generation" and old.reason_code == CODE_GENERATION_MISMATCH
    stale_again = ledger.view("SHFE", "rb2701")
    assert stale_again.state == STATE_STALE
    assert stale_again.effective_status_category == STATUS_UNKNOWN

    fresh = ledger.observe(_observation(status="2", generation="gen-2", sequence=1))
    assert fresh.applied is True
    view = ledger.view("SHFE", "rb2701")
    assert (view.state, view.effective_status_category, view.usable) == (
        STATE_CURRENT,
        STATUS_CONTINUOUS,
        True,
    )


def test_repeated_same_generation_is_idempotent_and_keeps_current_status():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    ledger.observe(_observation(status="6"))
    change = ledger.begin_session_generation("gen-1", "20261116")
    assert change.action == "unchanged" and change.reason_code is None
    view = ledger.view("SHFE", "rb2701")
    assert (view.state, view.effective_status_category) == (STATE_CURRENT, STATUS_CLOSED)


def test_trading_day_change_within_a_generation_invalidates():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    ledger.observe(_observation(status="6"))
    change = ledger.begin_session_generation("gen-1", "20261117")
    assert change.action == "invalidated" and change.reason_code == CODE_STALE_TRADING_DAY
    view = ledger.view("SHFE", "rb2701")
    assert (view.state, view.effective_status_category, view.usable) == (
        STATE_STALE,
        STATUS_UNKNOWN,
        False,
    )
    assert view.reason_code == CODE_STALE_TRADING_DAY

    mismatch = ledger.observe(_observation(status="6", trading_day="20261116", sequence=2))
    assert mismatch.applied is False and mismatch.reason_code == CODE_TRADING_DAY_MISMATCH
    assert ledger.view("SHFE", "rb2701").state == STATE_STALE


def test_out_of_order_callback_cannot_resurrect_an_older_status():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    assert ledger.observe(_observation(status="6", sequence=2)).applied is True
    late = ledger.observe(_observation(status="2", sequence=1))
    assert late.applied is False
    assert late.action == "out_of_order" and late.reason_code == CODE_SEQUENCE_OUT_OF_ORDER
    view = ledger.view("SHFE", "rb2701")
    assert (view.state, view.effective_status_category) == (STATE_CURRENT, STATUS_CLOSED)


def test_duplicate_callback_is_ignored_and_sequence_conflict_is_refused():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    ledger.observe(_observation(status="6", sequence=3))
    # A re-delivery arrives with a later receive stamp but the same source sequence.
    duplicate = ledger.observe(_observation(status="6", sequence=3, receive_time=9_999))
    assert duplicate.applied is False
    assert duplicate.action == "duplicate" and duplicate.reason_code == CODE_DUPLICATE
    assert ledger.view("SHFE", "rb2701").effective_status_category == STATUS_CLOSED

    conflict = ledger.observe(_observation(status="2", sequence=3))
    assert conflict.applied is False
    assert conflict.action == "sequence_conflict"
    assert conflict.reason_code == CODE_SEQUENCE_CONFLICT
    assert ledger.view("SHFE", "rb2701").effective_status_category == STATUS_CLOSED


def test_same_sequence_on_a_different_generation_is_stale_not_duplicate():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    ledger.observe(_observation(status="6", sequence=1))
    result = ledger.observe(_observation(status="6", generation="gen-2", sequence=1))
    assert result.applied is False and result.reason_code == CODE_GENERATION_MISMATCH


def test_identity_conflict_for_the_same_venue_and_native_symbol_is_refused():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    ledger.observe(_observation(status="6", sequence=1))
    conflict = ledger.observe(_observation(status="2", sequence=2, exchange_symbol="rb2701-alt"))
    assert conflict.applied is False
    assert conflict.action == "identity_conflict"
    assert conflict.reason_code == CODE_IDENTITY_CONFLICT
    assert ledger.view("SHFE", "rb2701").effective_status_category == STATUS_CLOSED


def test_unknown_category_is_current_but_not_usable_and_not_guessed():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    ledger.observe(_observation(status="8"))
    view = ledger.view("SHFE", "rb2701")
    assert view.state == STATE_CURRENT
    assert view.effective_status_category == STATUS_UNKNOWN
    assert view.observed_status_category == STATUS_UNKNOWN
    assert view.usable is False
    assert view.reason_code == CODE_STATUS_UNKNOWN


def test_never_observed_key_is_unknown():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    view = ledger.view("SHFE", "rb2701")
    assert (view.state, view.effective_status_category, view.usable) == (
        STATE_UNKNOWN,
        STATUS_UNKNOWN,
        False,
    )
    assert view.reason_code == CODE_NEVER_OBSERVED
    invalid = ledger.view("SHFE", "")
    assert invalid.state == STATE_UNKNOWN and invalid.reason_code == CODE_FIELDS_INVALID


# --------------------------------------------------------------------------- #
# 3. Fail-closed refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", ["ExchangeID", "InstrumentID"])
def test_missing_identity_refuses_with_finite_code(key):
    fields = _fields()
    del fields[key]
    result = _project(fields)
    assert result.accepted is False
    assert result.reason_code == CODE_FIELDS_INCOMPLETE
    assert result.missing_fields == (key,)
    assert (result.venue, result.native_symbol, result.exchange_symbol) is None or (
        result.venue,
        result.native_symbol,
        result.exchange_symbol,
    ) == (None, None, None)
    assert result.status_category == STATUS_UNKNOWN


@pytest.mark.parametrize(
    "key,value",
    [
        ("ExchangeID", " "),
        ("ExchangeID", "SHFE X"),
        ("ExchangeID", "S" * 9),
        ("InstrumentID", "x" * 81),
        ("ExchangeInstID", "中"),
        ("InstrumentStatus", 2),
        ("InstrumentStatus", b"2"),
        ("InstrumentStatus", "22"),
        ("EnterReason", 1),
        ("EnterReason", b"1"),
        ("EnterReason", "12"),
        ("TradingSegmentSN", -1),
        ("TradingSegmentSN", 2**31),
        ("TradingSegmentSN", True),
        ("TradingSegmentSN", 1.0),
        ("EnterTime", "24:00:00"),
        ("EnterTime", "9:00:00"),
        ("EnterTime", "09:00:00.000"),
    ],
)
def test_invalid_native_field_refuses_with_finite_code(key, value):
    result = _project(_fields(**{key: value}))
    assert result.accepted is False
    assert result.reason_code == CODE_FIELD_INVALID
    assert result.invalid_fields == (key,)
    assert (result.venue, result.native_symbol, result.status_category) == (
        None,
        None,
        STATUS_UNKNOWN,
    )


@pytest.mark.parametrize("key", ["InstrumentStatus", "EnterReason", "EnterTime"])
def test_missing_session_relevant_native_fact_refuses(key):
    fields = _fields()
    fields[key] = None
    result = _project(fields)
    assert result.accepted is False and result.reason_code == CODE_FIELDS_INCOMPLETE
    assert result.missing_fields == (key,)


@pytest.mark.parametrize(
    "session,code",
    [
        (_session(session_generation=""), CODE_GENERATION_INVALID),
        (_session(session_generation=1), CODE_GENERATION_INVALID),
        (_session(session_generation="g" * 129), CODE_GENERATION_INVALID),
        (_session(session_generation="gen 1"), CODE_GENERATION_INVALID),
        (_session(trading_day="20260230"), CODE_TRADING_DAY_INVALID),
        (_session(trading_day="2026-11-16"), CODE_TRADING_DAY_INVALID),
        (_session(trading_day=20261116), CODE_TRADING_DAY_INVALID),
        (_session(source_sequence=0), CODE_SEQUENCE_INVALID),
        (_session(source_sequence=-1), CODE_SEQUENCE_INVALID),
        (_session(source_sequence=True), CODE_SEQUENCE_INVALID),
        (_session(source_sequence=1.0), CODE_SEQUENCE_INVALID),
        (_session(receive_clock_domain="local"), CODE_RECEIVE_TIME_INVALID),
        (_session(receive_clock_domain=[]), CODE_RECEIVE_TIME_INVALID),
        (
            _session(receive_clock_domain=RECEIVE_CLOCK_MONOTONIC_NS, receive_time=0),
            CODE_RECEIVE_TIME_INVALID,
        ),
        (
            _session(receive_clock_domain=RECEIVE_CLOCK_MONOTONIC_NS, receive_time=True),
            CODE_RECEIVE_TIME_INVALID,
        ),
        (
            _session(receive_clock_domain=RECEIVE_CLOCK_MONOTONIC_NS, receive_time=1.0),
            CODE_RECEIVE_TIME_INVALID,
        ),
        (
            _session(
                receive_clock_domain=RECEIVE_CLOCK_UTC_ISO8601, receive_time="2026-11-16T09:00:00"
            ),
            CODE_RECEIVE_TIME_INVALID,
        ),
        (
            _session(
                receive_clock_domain=RECEIVE_CLOCK_UTC_ISO8601,
                receive_time="2026-11-16T09:00:00+01:00",
            ),
            CODE_RECEIVE_TIME_INVALID,
        ),
        (
            _session(
                receive_clock_domain=RECEIVE_CLOCK_UTC_ISO8601, receive_time="2026-02-30T09:00:00Z"
            ),
            CODE_RECEIVE_TIME_INVALID,
        ),
        (_session(source_available=False), CODE_SOURCE_UNAVAILABLE),
        (_session(source_available=0), CODE_SESSION_INVALID),
    ],
)
def test_invalid_session_facts_refuse_with_finite_code(session, code):
    result = _project(session=session)
    assert result.accepted is False
    assert result.reason_code == code


def test_forged_or_reconstructed_accepted_observation_is_refused():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    for forged in (
        InstrumentStatusObservation(accepted=True),
        InstrumentStatusObservation(
            accepted=True,
            venue="SHFE",
            native_symbol="rb2701",
            exchange_symbol="rb2701",
            status_category=STATUS_CLOSED,
            native_status_code="6",
            enter_reason=ENTER_REASON_AUTOMATIC,
            enter_time="09:00:00",
            trading_segment_sn=1,
            trading_day="20261116",
            session_generation="gen-1",
            source_sequence=0,
            received_at=1,
            receive_clock_domain=RECEIVE_CLOCK_MONOTONIC_NS,
        ),
    ):
        result = ledger.observe(forged)
        assert result.applied is False
        assert result.action == "refused"
        assert result.reason_code == CODE_OBSERVATION_INVALID
    assert ledger.view("SHFE", "rb2701").state == STATE_UNKNOWN


def test_non_session_object_is_refused_not_inspected():
    result = project_native_instrument_status(_fields(), session={"session_generation": "gen"})
    assert result.accepted is False and result.reason_code == CODE_SESSION_INVALID


def test_unavailable_source_refuses_and_invalidates_before_reuse():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    ledger.observe(_observation(status="6"))
    assert ledger.observe(_observation(status="2", sequence=2)).applied is True

    ledger.mark_source_unavailable()
    view = ledger.view("SHFE", "rb2701")
    assert (view.state, view.effective_status_category, view.usable) == (
        STATE_STALE,
        STATUS_UNKNOWN,
        False,
    )
    assert view.reason_code == CODE_SOURCE_UNAVAILABLE
    assert view.observed_status_category == STATUS_CONTINUOUS
    never = ledger.view("SHFE", "ag2701")
    assert never.state == STATE_UNKNOWN and never.reason_code == CODE_SOURCE_UNAVAILABLE

    refused = ledger.observe(_observation(status="2", sequence=3))
    assert refused.applied is False
    assert refused.action == "source_unavailable"
    assert refused.reason_code == CODE_SOURCE_UNAVAILABLE
    assert ledger.view("SHFE", "rb2701").state == STATE_STALE

    # Only a new generation with a fresh observation may become current again.
    ledger.begin_session_generation("gen-2", "20261116")
    assert ledger.view("SHFE", "rb2701").state == STATE_STALE
    assert ledger.observe(_observation(status="2", generation="gen-2", sequence=1)).applied
    assert ledger.view("SHFE", "rb2701").usable is True


# --------------------------------------------------------------------------- #
# 4. Input hygiene: no raw payload leaks, immutable values
# --------------------------------------------------------------------------- #


def test_unexpected_native_fields_are_rejected_without_echoing_values():
    secret = "private-account-token"
    result = _project(_fields(**{"UnknownSecret": secret}))
    assert result.accepted is False and result.reason_code == CODE_FIELDS_UNEXPECTED
    assert secret not in repr(result)
    assert _project({1: secret}).reason_code == CODE_FIELDS_INVALID
    assert secret not in repr(_project({1: secret}))
    assert _project(None).reason_code == CODE_FIELDS_INVALID


def test_invalid_value_is_not_echoed_and_hostile_equality_is_not_used():
    class Hazard:
        def __eq__(self, other):
            raise AssertionError("private exception")

        def __repr__(self):
            return "private-hazard"

    result = _project(_fields(InstrumentStatus=Hazard()))
    assert result.accepted is False and result.reason_code == CODE_FIELD_INVALID
    assert "private" not in repr(result)


def test_observation_and_view_are_immutable():
    observation = _observation()
    with pytest.raises(FrozenInstanceError):
        observation.status_category = STATUS_CLOSED
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    ledger.observe(observation)
    with pytest.raises(FrozenInstanceError):
        ledger.view("SHFE", "rb2701").usable = True


def test_bad_observation_object_is_refused():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    result = ledger.observe({"accepted": True})
    assert (
        result.applied is False
        and result.reason_code == "ctp_instrument_status_observation_invalid"
    )


# --------------------------------------------------------------------------- #
# 5. No clock guess: receive time is explicit and never used for ordering/expiry
# --------------------------------------------------------------------------- #


def test_module_never_reads_a_clock():
    source = inspect.getsource(module)
    for token in ("import time", "time.time(", "datetime.now(", ".utcnow(", ".today("):
        assert token not in source


def test_receive_time_domain_is_explicit_and_stored_verbatim():
    monotonic = _project(session=_session(receive_time=42))
    assert (monotonic.received_at, monotonic.receive_clock_domain) == (
        42,
        RECEIVE_CLOCK_MONOTONIC_NS,
    )
    utc = _project(
        session=_session(
            receive_clock_domain=RECEIVE_CLOCK_UTC_ISO8601,
            receive_time="2026-11-16T09:00:00Z",
        )
    )
    assert (utc.received_at, utc.receive_clock_domain) == (
        "2026-11-16T09:00:00Z",
        RECEIVE_CLOCK_UTC_ISO8601,
    )


def test_ordering_uses_source_sequence_never_the_receive_clock_or_expiry():
    ledger = InstrumentStatusLedger(session_generation="gen-1", trading_day="20261116")
    # A tiny monotonic stamp is still current: no age-based expiry exists.
    assert ledger.observe(_observation(status="6", sequence=1, receive_time=1)).applied
    assert ledger.view("SHFE", "rb2701").usable is True

    # Higher sequence wins even when its receive stamp is smaller.
    assert ledger.observe(_observation(status="2", sequence=2, receive_time=1)).applied
    assert ledger.view("SHFE", "rb2701").effective_status_category == STATUS_CONTINUOUS

    # A huge receive stamp with a lower sequence cannot resurrect the old status.
    late = ledger.observe(_observation(status="6", sequence=1, receive_time=2**62))
    assert late.applied is False and late.reason_code == CODE_SEQUENCE_OUT_OF_ORDER
    assert ledger.view("SHFE", "rb2701").effective_status_category == STATUS_CONTINUOUS


# --------------------------------------------------------------------------- #
# 6. Fake native callback surface through the provider-local ingress
# --------------------------------------------------------------------------- #


def test_ingress_surface_captures_only_the_exact_safe_fields():
    record = _ingress_record(_tagged_native())
    assert record.capture_complete is True
    payload = record.to_payload()
    fields = native_fields_from_ingress_payload(payload)
    assert fields == _fields()


def test_ingress_projection_is_accepted_without_leaking_extra_native_attributes():
    secret = "private-native-attribute"
    record = _ingress_record(_tagged_native(SecretAttribute=secret))
    fields = native_fields_from_ingress_payload(record.to_payload())
    assert secret not in repr(fields)
    result = project_native_instrument_status(fields, session=_session())
    assert result.accepted is True and result.status_category == STATUS_CONTINUOUS
    assert secret not in repr(result)


@pytest.mark.parametrize("callback", ["OnRtnOrder", "OnRtnTrade", "OnRspQryInstrument"])
def test_ingress_helper_rejects_other_callback_names(callback):
    record = _ingress_record(_tagged_native())
    payload = dict(record.to_payload())
    payload["callback_name"] = callback
    with pytest.raises(CtpInstrumentStatusObservationError) as caught:
        native_fields_from_ingress_payload(payload)
    assert caught.value.code == CODE_FIELDS_INVALID


def test_ingress_helper_refuses_incomplete_capture_and_unscalar_fields():
    native = _tagged_native()
    del native.EnterReason
    record = _ingress_record(native)
    # A missing optional safe-field getter is recorded as absent; the provider
    # helper must still refuse the incomplete native status projection.
    with pytest.raises(CtpInstrumentStatusObservationError) as caught:
        native_fields_from_ingress_payload(record.to_payload())
    assert caught.value.code == CODE_FIELDS_INCOMPLETE

    payload = dict(_ingress_record(_tagged_native()).to_payload())
    payload["flattened_fields"] = [
        dict(item) for item in payload["flattened_fields"] if item["field_name"] != "EnterReason"
    ]
    with pytest.raises(CtpInstrumentStatusObservationError) as caught:
        native_fields_from_ingress_payload(payload)
    assert caught.value.code == CODE_FIELDS_INCOMPLETE


def test_ingress_helper_refuses_hostile_equality_without_echoing_values():
    class Hazard:
        def __eq__(self, other):
            raise AssertionError("private exception")

        def __ne__(self, other):
            raise AssertionError("private exception")

    base = dict(_ingress_record(_tagged_native()).to_payload())
    for key in ("schema", "callback_name", "missing_getters"):
        payload = dict(base)
        payload[key] = Hazard()
        with pytest.raises(CtpInstrumentStatusObservationError) as caught:
            native_fields_from_ingress_payload(payload)
        assert caught.value.code in {CODE_FIELDS_INVALID, CODE_FIELDS_INCOMPLETE}
        assert "private exception" not in str(caught.value)

    payload = dict(base)
    payload["flattened_fields"] = [
        {**item, "argument_slot": Hazard()} for item in payload["flattened_fields"]
    ]
    with pytest.raises(CtpInstrumentStatusObservationError) as caught:
        native_fields_from_ingress_payload(payload)
    assert caught.value.code == CODE_FIELDS_INCOMPLETE


def test_ingress_helper_never_returns_unselected_fields():
    payload = dict(_ingress_record(_tagged_native()).to_payload())
    payload["flattened_fields"] = list(payload["flattened_fields"]) + [
        {
            "argument_slot": 0,
            "field_name": "UnexpectedNativeField",
            "present": True,
            "scalar_captured": True,
            "value": "private-value",
        }
    ]
    fields = native_fields_from_ingress_payload(payload)
    assert "UnexpectedNativeField" not in fields
    assert "private-value" not in repr(fields)
