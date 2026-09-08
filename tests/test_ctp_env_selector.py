from __future__ import annotations

from datetime import datetime

import pytest

from bt_api_ctp.ctp_env_selector import (
    apply_ctp_env,
    get_ctp_fronts,
    select_ctp_environment,
    verify_official_simnow_profile,
)
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture


def test_get_ctp_fronts_prefers_set1_during_weekday_session(monkeypatch) -> None:
    monkeypatch.setenv("CTP_SET1_GROUP", "2")
    monkeypatch.setenv("CTP_SET1_TD_FRONT_2", "tcp://set1-td")
    monkeypatch.setenv("CTP_SET1_MD_FRONT_2", "tcp://set1-md")

    td, md, env_name = get_ctp_fronts(
        now=datetime(2026, 3, 16, 10, 0, 0), is_trading_day=True
    )

    assert (td, md, env_name) == ("tcp://set1-td", "tcp://set1-md", "set1_group2")


def test_get_ctp_fronts_uses_set2_outside_trading_hours(monkeypatch) -> None:
    monkeypatch.setenv("CTP_SET2_TD_FRONT", "tcp://set2-td")
    monkeypatch.setenv("CTP_SET2_MD_FRONT", "tcp://set2-md")

    td, md, env_name = get_ctp_fronts(now=datetime(2026, 3, 16, 16, 30, 0))

    assert (td, md, env_name) == ("tcp://set2-td", "tcp://set2-md", "set2_7x24")


def test_auto_without_exchange_calendar_does_not_guess_set1(monkeypatch) -> None:
    monkeypatch.setenv("CTP_SET1_TD_FRONT_1", "tcp://set1-td")
    monkeypatch.setenv("CTP_SET1_MD_FRONT_1", "tcp://set1-md")
    selection = select_ctp_environment(now=datetime(2026, 3, 16, 10, 0, 0))

    assert selection.profile == "set2_7x24"
    assert selection.readiness == "calendar_unverified"
    assert selection.calendar_verified is False


def test_apply_ctp_env_uses_env_override(monkeypatch) -> None:
    monkeypatch.setenv("CTP_ENV", "set2")
    monkeypatch.setenv("CTP_SET2_TD_FRONT", "tcp://override-td")
    monkeypatch.setenv("CTP_SET2_MD_FRONT", "tcp://override-md")

    td, md, env_name = apply_ctp_env()

    assert (td, md, env_name) == ("tcp://override-td", "tcp://override-md", "set2_7x24")


def test_official_simnow_defaults_and_required_profile(monkeypatch) -> None:
    for name in (
        "CTP_ENV",
        "CTP_SET1_TD_FRONT_1",
        "CTP_SET1_MD_FRONT_1",
        "CTP_SET2_TD_FRONT",
        "CTP_SET2_MD_FRONT",
    ):
        monkeypatch.delenv(name, raising=False)

    assert get_ctp_fronts("set1")[:2] == (
        "tcp://180.168.146.187:10201",
        "tcp://180.168.146.187:10211",
    )
    assert get_ctp_fronts("set2")[:2] == (
        "tcp://180.168.146.187:10130",
        "tcp://180.168.146.187:10131",
    )
    with pytest.raises(RuntimeError, match="required CTP profile"):
        select_ctp_environment("auto", require_profile="set1")


def test_unknown_environment_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported CTP_ENV"):
        select_ctp_environment("typo")


def test_explicit_profile_claim_requires_exact_official_pair() -> None:
    assert verify_official_simnow_profile(
        "tcp://180.168.146.187:10202",
        "tcp://180.168.146.187:10212",
        "set1_group2",
    )
    assert not verify_official_simnow_profile(
        "tcp://180.168.146.187:10202",
        "tcp://180.168.146.187:10211",
        "set1_group2",
    )


def test_explicit_official_profile_is_verified_demo_without_global_env(
    monkeypatch,
) -> None:
    for name in ("CTP_ENV", "CTP_TD_FRONT", "CTP_MD_FRONT"):
        monkeypatch.delenv(name, raising=False)
    feed = CtpRequestDataFuture(
        ctp_env_profile="set1_group1",
        td_front="tcp://180.168.146.187:10201",
        md_front="tcp://180.168.146.187:10211",
    )

    assert feed.get_environment_info() == {
        "environment": "demo",
        "simulated": True,
        "verified": True,
        "profile": "set1_group1",
        "readiness": "explicit_official_pair",
    }


def test_custom_or_mixed_fronts_cannot_claim_verified_demo(monkeypatch) -> None:
    monkeypatch.setenv("CTP_ENV", "set2")
    feed = CtpRequestDataFuture(td_front="tcp://custom-td")
    assert feed.ctp_env_profile == "mixed_front_override"
    assert feed.get_environment_info()["environment"] == "unknown"
    assert feed.get_environment_info()["verified"] is False

    with pytest.raises(ValueError, match="does not match its official fronts"):
        CtpRequestDataFuture(
            ctp_env_profile="set1_group1",
            td_front="tcp://180.168.146.187:10201",
            md_front="tcp://180.168.146.187:10212",
        )


def test_required_profile_rejects_unclaimed_explicit_fronts() -> None:
    with pytest.raises(RuntimeError, match="cannot be proven"):
        CtpRequestDataFuture(
            require_ctp_profile="set1",
            td_front="tcp://180.168.146.187:10201",
            md_front="tcp://180.168.146.187:10211",
        )
