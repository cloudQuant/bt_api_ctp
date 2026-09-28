"""Pure validation for callback-derived MD identity observations."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from bt_api_ctp.md_identity import MdIdentityObservation, md_identity_matches


def _observation(**changes) -> MdIdentityObservation:
    values = {
        "front": "tcp://configured-md:30011",
        "broker_id": "9999",
        "user_id": "account-7",
        "connection_generation": 4,
        "request_id": 19,
        "trading_day": "20260926",
        "authenticated": True,
    }
    values.update(changes)
    return MdIdentityObservation(**values)


def _matches(observation: MdIdentityObservation) -> bool:
    return md_identity_matches(
        observation,
        expected_front="tcp://configured-md:30011",
        expected_broker_id="9999",
        expected_user_id="account-7",
        expected_connection_generation=4,
        expected_request_id=19,
    )


@pytest.mark.unit
def test_md_identity_is_frozen_and_request_is_distinct_from_connection_generation() -> None:
    observation = _observation()

    assert observation.connection_generation == 4
    assert observation.request_id == 19
    assert _matches(observation) is True
    with pytest.raises(FrozenInstanceError):
        observation.request_id = 4  # type: ignore[misc]

    # The helper checks each identifier against its own expected source.
    assert _matches(replace(observation, request_id=4)) is False
    assert _matches(replace(observation, connection_generation=19)) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "changes",
    [
        {"front": "tcp://replacement-md:30011"},
        {"broker_id": "other-broker"},
        {"user_id": "other-user"},
        {"authenticated": False},
        {"broker_id": None},
        {"user_id": None},
        {"trading_day": None},
        {"trading_day": "20260230"},
        {"trading_day": "2026092"},
    ],
)
def test_incomplete_or_mismatched_callback_identity_is_not_current(changes) -> None:
    assert _matches(_observation(**changes)) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("front", 7),
        ("broker_id", b"9999"),
        ("user_id", 7),
        ("trading_day", 20260926),
        ("connection_generation", True),
        ("connection_generation", 0),
        ("connection_generation", -1),
        ("request_id", True),
        ("request_id", 0),
        ("request_id", -1),
        ("authenticated", 1),
    ],
)
def test_md_identity_rejects_wrong_scalar_types_and_invalid_counters(field_name, value) -> None:
    with pytest.raises(ValueError):
        _observation(**{field_name: value})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("expected_front", ""),
        ("expected_broker_id", " 9999"),
        ("expected_user_id", ""),
        ("expected_connection_generation", True),
        ("expected_request_id", False),
    ],
)
def test_md_identity_matcher_rejects_malformed_expected_binding(field_name, value) -> None:
    kwargs = {
        "expected_front": "tcp://configured-md:30011",
        "expected_broker_id": "9999",
        "expected_user_id": "account-7",
        "expected_connection_generation": 4,
        "expected_request_id": 19,
    }
    kwargs[field_name] = value

    with pytest.raises(ValueError):
        md_identity_matches(_observation(), **kwargs)


@pytest.mark.unit
def test_importing_md_identity_does_not_import_the_ctp_native_package() -> None:
    package_src = str(Path(__file__).resolve().parents[1] / "src")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = package_src
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import bt_api_ctp.md_identity; assert 'bt_api_ctp.ctp' not in sys.modules",
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
