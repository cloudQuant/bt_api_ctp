"""Contracts for the source-owned registered-sim receipt records."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

import bt_api_ctp
from bt_api_ctp import fields as ctp_fields
from bt_api_ctp import status
from bt_api_ctp.receipts import (
    RegisteredSimCancelTarget,
    RegisteredSimDispatchEvidence,
    RegisteredSimOrderIdentityQueryReceipt,
    RegisteredSimRouteEligibility,
    RegisteredSimUnknownRetirement,
)

_DIGEST = "a" * 64


def _receipt(**changes) -> RegisteredSimOrderIdentityQueryReceipt:
    values = {
        "request_id": 41,
        "connection_generation": 4,
        "account_fingerprint": "fp-9999-account-7",
        "trading_day": "20261031",
        "started_at_utc": datetime(2026, 10, 31, 1, 2, 3, tzinfo=timezone.utc),
        "completed_at_utc": datetime(2026, 10, 31, 1, 2, 9, tzinfo=timezone.utc),
        "front_id": 7,
        "session_id": 9,
        "broker_id": "9999",
        "investor_id": "account-7",
        "rows_sha256": _DIGEST,
        "rows": (MappingProxyType({"OrderSysID": "1"}),),
        "source_expires_monotonic": 1_000_000.0,
        "_store": object(),
        "_issuer": object(),
    }
    values.update(changes)
    return RegisteredSimOrderIdentityQueryReceipt(**values)


def _cancel_target(**changes) -> RegisteredSimCancelTarget:
    values = {
        "order_ref": "123456",
        "order_sys_id": "300001",
        "instrument_id": "rb2510",
        "exchange_id": "SHFE",
        "broker_id": "9999",
        "investor_id": "account-7",
        "front_id": 7,
        "session_id": 9,
        "account_fingerprint": "fp-9999-account-7",
        "connection_generation": 4,
        "trading_day": "20261031",
        "query_request_id": 41,
        "query_rows_sha256": _DIGEST,
        "matched_row_sha256": "b" * 64,
        "source_expires_monotonic": 1_000_000.0,
        "_issuer": object(),
        "_seal": object(),
    }
    values.update(changes)
    return RegisteredSimCancelTarget(**values)


def _dispatch_evidence(**changes) -> RegisteredSimDispatchEvidence:
    values = {
        "sequence": 3,
        "operation": "SUBMIT",
        "status": "KNOWN",
        "request_id": 41,
        "instrument_id": "rb2510",
        "exchange_id": "SHFE",
        "order_ref": "123456",
        "order_sys_id": "300001",
        "front_id": 7,
        "session_id": 9,
        "broker_id": "9999",
        "investor_id": "account-7",
        "user_id": "account-7",
        "native_return_code": 0,
        "error_type": None,
        "observed_at_utc": "2026-10-31T01:02:03+00:00",
    }
    values.update(changes)
    return RegisteredSimDispatchEvidence(**values)


def _route_eligibility(**changes) -> RegisteredSimRouteEligibility:
    values = {
        "route_name": "registered_sim",
        "instrument_id": "rb2510",
        "exchange_id": "SHFE",
        "account_fingerprint": "fp-9999-account-7",
        "connection_generation": 4,
        "trading_day": "20261031",
        "allow_writes": True,
        "frozen": False,
        "sdk_frozen": False,
        "unknown_order_ids": None,
        "unknown_order_count": None,
        "dispatching_order_ids": (),
        "capability_observed": True,
        "sdk_capability_managed": True,
        "sdk_capability_armed": True,
        "sdk_capability_instrument": "rb2510",
        "sdk_authorized_instruments": ("rb2510",),
        "capability_matches_route": True,
        "instrument_query_complete": True,
        "instrument_eligible": True,
        "reason_codes": (),
    }
    values.update(changes)
    return RegisteredSimRouteEligibility(**values)


@pytest.mark.unit
def test_receipt_field_names_and_order_are_the_owned_contract() -> None:
    names = [field.name for field in dataclasses.fields(RegisteredSimOrderIdentityQueryReceipt)]

    assert names == [
        "request_id",
        "connection_generation",
        "account_fingerprint",
        "trading_day",
        "started_at_utc",
        "completed_at_utc",
        "front_id",
        "session_id",
        "broker_id",
        "investor_id",
        "rows_sha256",
        "rows",
        "source_expires_monotonic",
        "_store",
        "_issuer",
    ]
    cancel_names = [field.name for field in dataclasses.fields(RegisteredSimCancelTarget)]
    assert cancel_names[-2:] == ["_issuer", "_seal"]


@pytest.mark.unit
def test_dispatch_evidence_is_frozen_and_never_permits_resend() -> None:
    evidence = _dispatch_evidence()

    assert evidence.status == "KNOWN"
    assert evidence.resend_permitted is False
    assert _dispatch_evidence(status="UNKNOWN").resend_permitted is False
    with pytest.raises(FrozenInstanceError):
        evidence.status = "KNOWN"  # type: ignore[misc]


@pytest.mark.unit
def test_retirement_and_eligibility_are_frozen_value_records() -> None:
    retirement = RegisteredSimUnknownRetirement(
        status="RETIRED",
        intent_id="intent-1",
        reconciliation_id="recon-1",
        disarmed=True,
    )
    with pytest.raises(FrozenInstanceError):
        retirement.disarmed = False  # type: ignore[misc]

    eligibility = _route_eligibility()
    assert eligibility.native_admission_required is True
    assert eligibility.arm_template_configured is None
    assert eligibility.arm_template_membership is None
    assert _route_eligibility(native_admission_required=False).native_admission_required is False
    with pytest.raises(FrozenInstanceError):
        eligibility.frozen = True  # type: ignore[misc]


@pytest.mark.unit
def test_cancel_target_and_receipt_accept_complete_construction() -> None:
    target = _cancel_target()
    receipt = _receipt()

    assert target.query_request_id == 41
    assert target._seal is not None
    assert receipt.validate_shape() is None
    # A pure shape check is idempotent and never trusts the opaque bindings.
    assert receipt.validate_shape() is None


@pytest.mark.unit
def test_receipt_validate_shape_does_not_recheck_store_identity() -> None:
    foreign = _receipt(_store=object())

    assert foreign._store is not _receipt()._store
    assert foreign.validate_shape() is None


@pytest.mark.unit
def test_receipt_validate_shape_accepts_an_aware_non_utc_pair() -> None:
    offset = timezone(timedelta(hours=8))
    receipt = _receipt(
        started_at_utc=datetime(2026, 10, 31, 9, 2, 3, tzinfo=offset),
        completed_at_utc=datetime(2026, 10, 31, 1, 2, 9, tzinfo=timezone.utc),
    )

    assert receipt.validate_shape() is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"request_id": 0}, "CANNOT_ORDER_IDENTITY_RECEIPT_REQUEST_ID"),
        ({"request_id": True}, "CANNOT_ORDER_IDENTITY_RECEIPT_REQUEST_ID"),
        ({"request_id": "41"}, "CANNOT_ORDER_IDENTITY_RECEIPT_REQUEST_ID"),
        (
            {"connection_generation": 0},
            "CANNOT_ORDER_IDENTITY_RECEIPT_CONNECTION_GENERATION",
        ),
        (
            {"connection_generation": "4"},
            "CANNOT_ORDER_IDENTITY_RECEIPT_CONNECTION_GENERATION",
        ),
        ({"_issuer": None}, "CANNOT_ORDER_IDENTITY_RECEIPT_ISSUER"),
        (
            {"account_fingerprint": ""},
            "CANNOT_ORDER_IDENTITY_RECEIPT_ACCOUNT_FINGERPRINT",
        ),
        (
            {"account_fingerprint": " fp"},
            "CANNOT_ORDER_IDENTITY_RECEIPT_ACCOUNT_FINGERPRINT",
        ),
        (
            {"account_fingerprint": 7},
            "CANNOT_ORDER_IDENTITY_RECEIPT_ACCOUNT_FINGERPRINT",
        ),
        ({"broker_id": ""}, "CANNOT_ORDER_IDENTITY_RECEIPT_BROKER_ID"),
        ({"broker_id": 9999}, "CANNOT_ORDER_IDENTITY_RECEIPT_BROKER_ID"),
        ({"investor_id": " account-7"}, "CANNOT_ORDER_IDENTITY_RECEIPT_INVESTOR_ID"),
        ({"investor_id": None}, "CANNOT_ORDER_IDENTITY_RECEIPT_INVESTOR_ID"),
        ({"trading_day": "2026103"}, "CANNOT_ORDER_IDENTITY_RECEIPT_TRADING_DAY"),
        ({"trading_day": "202610310"}, "CANNOT_ORDER_IDENTITY_RECEIPT_TRADING_DAY"),
        ({"trading_day": "2026-10-31"}, "CANNOT_ORDER_IDENTITY_RECEIPT_TRADING_DAY"),
        (
            {"trading_day": "202610AB"},
            "CANNOT_ORDER_IDENTITY_RECEIPT_TRADING_DAY",
        ),
        (
            {"started_at_utc": datetime(2026, 10, 31, 1, 2, 3)},
            "CANNOT_ORDER_IDENTITY_RECEIPT_STARTED_AT_UTC",
        ),
        (
            {"completed_at_utc": datetime(2026, 10, 31, 1, 2, 9)},
            "CANNOT_ORDER_IDENTITY_RECEIPT_COMPLETED_AT_UTC",
        ),
        (
            {"started_at_utc": "2026-10-31T01:02:03+00:00"},
            "CANNOT_ORDER_IDENTITY_RECEIPT_STARTED_AT_UTC",
        ),
        (
            {
                "started_at_utc": datetime(2026, 10, 31, 1, 3, 0, tzinfo=timezone.utc),
                "completed_at_utc": datetime(2026, 10, 31, 1, 2, 9, tzinfo=timezone.utc),
            },
            "CANNOT_ORDER_IDENTITY_RECEIPT_TIME_ORDER",
        ),
        ({"front_id": 0}, "CANNOT_ORDER_IDENTITY_RECEIPT_FRONT_ID"),
        ({"front_id": "7"}, "CANNOT_ORDER_IDENTITY_RECEIPT_FRONT_ID"),
        ({"session_id": -1}, "CANNOT_ORDER_IDENTITY_RECEIPT_SESSION_ID"),
        ({"session_id": True}, "CANNOT_ORDER_IDENTITY_RECEIPT_SESSION_ID"),
        ({"rows": [{"OrderSysID": "1"}]}, "CANNOT_ORDER_IDENTITY_RECEIPT_ROWS"),
        ({"rows": []}, "CANNOT_ORDER_IDENTITY_RECEIPT_ROWS"),
        ({"rows_sha256": "A" * 64}, "CANNOT_ORDER_IDENTITY_RECEIPT_ROWS_DIGEST"),
        ({"rows_sha256": "abc"}, "CANNOT_ORDER_IDENTITY_RECEIPT_ROWS_DIGEST"),
        ({"rows_sha256": None}, "CANNOT_ORDER_IDENTITY_RECEIPT_ROWS_DIGEST"),
        (
            {"source_expires_monotonic": float("nan")},
            "CANNOT_ORDER_IDENTITY_RECEIPT_SOURCE_EXPIRY",
        ),
        (
            {"source_expires_monotonic": float("inf")},
            "CANNOT_ORDER_IDENTITY_RECEIPT_SOURCE_EXPIRY",
        ),
        (
            {"source_expires_monotonic": "1000000.0"},
            "CANNOT_ORDER_IDENTITY_RECEIPT_SOURCE_EXPIRY",
        ),
    ],
)
def test_receipt_validate_shape_rejects_each_invariant(changes, code) -> None:
    with pytest.raises(ValueError) as raised:
        replace(_receipt(), **changes).validate_shape()

    assert str(raised.value).startswith(code + ":")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"order_ref": ""}, "order_ref must be non-empty exact text"),
        ({"order_sys_id": " 300001"}, "order_sys_id must be non-empty exact text"),
        ({"instrument_id": None}, "instrument_id must be non-empty exact text"),
        ({"account_fingerprint": 7}, "account_fingerprint must be non-empty exact text"),
        ({"front_id": 0}, "front_id must be a positive int"),
        ({"session_id": True}, "session_id must be a positive int"),
        ({"connection_generation": -1}, "connection_generation must be a positive int"),
        ({"query_request_id": "41"}, "query_request_id must be a positive int"),
        ({"trading_day": "2026103"}, "trading_day must be an exact YYYYMMDD"),
        ({"trading_day": "20261031A"}, "trading_day must be an exact YYYYMMDD"),
        (
            {"query_rows_sha256": "A" * 64},
            "cancel target query digests must be lowercase SHA-256",
        ),
        (
            {"matched_row_sha256": "abc"},
            "cancel target query digests must be lowercase SHA-256",
        ),
        (
            {"source_expires_monotonic": float("nan")},
            "source_expires_monotonic must be a finite positive deadline",
        ),
        (
            {"source_expires_monotonic": 0.0},
            "source_expires_monotonic must be a finite positive deadline",
        ),
        ({"_issuer": None}, "cancel target requires its validated Store issuer"),
        ({"_seal": None}, "cancel target requires its validated Store issuer"),
    ],
)
def test_cancel_target_rejects_each_invariant(changes, message) -> None:
    with pytest.raises(ValueError) as raised:
        replace(_cancel_target(), **changes)

    assert str(raised.value) == message


@pytest.mark.unit
def test_status_tables_are_the_single_owned_values() -> None:
    assert status.TERMINAL_ORDER_STATUSES == frozenset({"0", "4", "5"})
    assert status.COMMISSION_REQUEST_FILTER_FIELDS == {
        "instrument_id": "InstrumentID",
        "exchange_id": "ExchangeID",
        "InstrumentID": "InstrumentID",
        "ExchangeID": "ExchangeID",
        "BrokerID": "BrokerID",
        "InvestorID": "InvestorID",
    }
    # The text normalization owner stays in ``fields``; this set is the distinct
    # native-code terminal projection and is not derived from it.
    assert "2" not in status.TERMINAL_ORDER_STATUSES
    assert status.TERMINAL_ORDER_STATUSES <= set(ctp_fields.ORDER_STATUS_MAP)


@pytest.mark.unit
def test_package_reexports_the_owned_names() -> None:
    assert (
        bt_api_ctp.RegisteredSimOrderIdentityQueryReceipt is RegisteredSimOrderIdentityQueryReceipt
    )
    assert bt_api_ctp.RegisteredSimCancelTarget is RegisteredSimCancelTarget
    assert bt_api_ctp.RegisteredSimDispatchEvidence is RegisteredSimDispatchEvidence
    assert bt_api_ctp.RegisteredSimUnknownRetirement is RegisteredSimUnknownRetirement
    assert bt_api_ctp.RegisteredSimRouteEligibility is RegisteredSimRouteEligibility
    assert bt_api_ctp.TERMINAL_ORDER_STATUSES is status.TERMINAL_ORDER_STATUSES
    assert bt_api_ctp.COMMISSION_REQUEST_FILTER_FIELDS is status.COMMISSION_REQUEST_FILTER_FIELDS
    for name in (
        "RegisteredSimCancelTarget",
        "RegisteredSimDispatchEvidence",
        "RegisteredSimOrderIdentityQueryReceipt",
        "RegisteredSimRouteEligibility",
        "RegisteredSimUnknownRetirement",
        "COMMISSION_REQUEST_FILTER_FIELDS",
        "TERMINAL_ORDER_STATUSES",
    ):
        assert name in bt_api_ctp.__all__


@pytest.mark.unit
@pytest.mark.parametrize("module_name", ("receipts", "status"))
def test_owned_modules_never_import_backtrader(module_name) -> None:
    package_src = str(Path(__file__).resolve().parents[1] / "src")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = package_src
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"import bt_api_ctp.{module_name}; "
                "assert 'backtrader' not in sys.modules, "
                "sorted(name for name in sys.modules if 'backtrader' in name); "
                "assert 'bt_api_ctp.ctp' not in sys.modules"
            ),
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
