"""Shared CTP source-sealed reconciliation query validation."""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from bt_api_ctp.query import (
    _QUERY_EVIDENCE_MAX_TTL_SECONDS,
    _QUERY_MONOTONIC_CLOCK_DOMAIN,
    _QUERY_SOURCE_SEAL,
    _query_records_digest,
    QueryResult,
    native_account_query_filters,
    _QuerySource,
)
from bt_api_ctp.registered_sim_session import CtpQuerySessionBinding

_UTC = timezone.utc


def validate_ctp_reconciliation_query_result(
    result: Any,
    binding: CtpQuerySessionBinding,
    family: str,
    *,
    expected_record_limit: int | None = None,
) -> bool:
    """Apply the original reconciliation result and sealed-source checks."""
    expected_request_type = {
        "orders": "orders",
        "fills": "trades",
        "positions": "positions",
    }.get(family)
    if (
        expected_request_type is None
        or type(result) is not QueryResult
        or result.request_type != expected_request_type
        or result.complete is not True
        or result.is_last_seen is not True
        or result.timed_out is not False
        or result.unsupported is not False
        or result.record_limit_exceeded is not False
        or (expected_record_limit is not None and result.record_limit != expected_record_limit)
        or type(result.late_callback_count) is not int
        or result.late_callback_count != 0
        or type(result.error_message) is not str
        or result.error_message != ""
        or not (
            result.error_code is None or (type(result.error_code) is int and result.error_code == 0)
        )
        or type(result.submit_code) is not int
        or result.submit_code != 0
        or type(result.records) is not tuple
        or type(result.request_id) is not int
        or result.request_id <= 0
        or type(result.connection_generation) is not int
        or result.connection_generation != binding.connection_generation
        or type(result.account_fingerprint) is not str
        or result.account_fingerprint != binding.account_fingerprint
        or result.completion_origin != "success"
        or type(result.query_source) is not _QuerySource
    ):
        return False
    source = result.query_source
    now_monotonic = time.monotonic()
    now_utc = datetime.now(_UTC)
    if (
        source._seal is not _QUERY_SOURCE_SEAL
        or source.issuer is not binding._issuer
        or source.request_type != expected_request_type
        or source.request_id != result.request_id
        or source.account_fingerprint != binding.account_fingerprint
        or source.connection_generation != binding.connection_generation
        or source.trading_day != binding.trading_day
        or source.broker_id != binding.broker_id
        or source.investor_id != binding.investor_id
        or source.source_tags != binding._source_tags
        or source.clock_domain_id != _QUERY_MONOTONIC_CLOCK_DOMAIN
        or source.started_at_utc != result.started_at_utc
        or source.completed_at_utc != result.completed_at_utc
        or type(source.started_at_utc) is not datetime
        or type(source.completed_at_utc) is not datetime
        or source.started_at_utc.tzinfo is None
        or source.completed_at_utc.tzinfo is None
        or source.started_at_utc.utcoffset() != timedelta(0)
        or source.completed_at_utc.utcoffset() != timedelta(0)
        or source.started_at_utc > source.completed_at_utc
        or type(source.started_monotonic) not in (int, float)
        or isinstance(source.started_monotonic, bool)
        or not math.isfinite(source.started_monotonic)
        or source.started_monotonic < binding._captured_monotonic
        or type(source.completed_monotonic) not in (int, float)
        or isinstance(source.completed_monotonic, bool)
        or not math.isfinite(source.completed_monotonic)
        or source.completed_monotonic < source.started_monotonic
        or source.completed_monotonic > now_monotonic
        or type(source.trusted_expires_monotonic) not in (int, float)
        or isinstance(source.trusted_expires_monotonic, bool)
        or not math.isfinite(source.trusted_expires_monotonic)
        or now_monotonic >= source.trusted_expires_monotonic
        or type(source.trusted_expires_at_utc) is not datetime
        or source.trusted_expires_at_utc.tzinfo is None
        or source.trusted_expires_at_utc.utcoffset() != timedelta(0)
        or now_utc >= source.trusted_expires_at_utc
        or abs(
            (source.trusted_expires_at_utc - source.completed_at_utc).total_seconds()
            - _QUERY_EVIDENCE_MAX_TTL_SECONDS
        )
        > 1e-6
        or type(source.records_sha256) is not str
        or source.record_limit != expected_record_limit
        or (expected_record_limit is not None and type(source.record_limit) is not int)
        or source.record_limit_exceeded is not False
        or result.started_at_utc != source.started_at_utc
        or result.completed_at_utc != source.completed_at_utc
    ):
        return False
    expected_filters = {
        "positions": native_account_query_filters(
            "positions", broker_id=binding.broker_id, investor_id=binding.investor_id
        ),
        "orders": native_account_query_filters(
            "orders", broker_id=binding.broker_id, investor_id=binding.investor_id
        ),
        "trades": {
            "BrokerID": binding.broker_id,
            "ExchangeID": "",
            "InstrumentID": "",
            "InvestorID": binding.investor_id,
            "TradeID": "",
            "TradeTimeEnd": "",
            "TradeTimeStart": "",
        },
    }.get(expected_request_type)
    if expected_filters is None:
        return False
    try:
        if dict(source.request_filters) != expected_filters:
            return False
        if dict(source.request_intent_filters) != expected_filters:
            return False
        if source.request_parameters or source.request_intent_parameters:
            return False
        if _query_records_digest(result.records) != source.records_sha256:
            return False
    except Exception:
        return False
    return True
