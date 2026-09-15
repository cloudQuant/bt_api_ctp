from __future__ import annotations

from datetime import datetime

import pytest

from bt_api_ctp.ctp_env_selector import (
    CtpEnvironmentSelection,
    apply_ctp_env,
    get_ctp_fronts,
    official_simnow_fronts,
    probe_ctp_environment_pair,
    select_ctp_environment,
    select_reachable_ctp_environment,
    verify_official_simnow_profile,
)
from bt_api_ctp.feeds import live_ctp_feed
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
from bt_api_ctp.gateway import adapter as adapter_module


def test_get_ctp_fronts_prefers_set1_during_weekday_session(monkeypatch) -> None:
    monkeypatch.setenv("CTP_SET1_GROUP", "2")
    monkeypatch.setenv("CTP_SET1_TD_FRONT_2", "tcp://set1-td")
    monkeypatch.setenv("CTP_SET1_MD_FRONT_2", "tcp://set1-md")

    td, md, env_name = get_ctp_fronts(now=datetime(2026, 3, 16, 10, 0, 0), is_trading_day=True)

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


def test_named_set2_4000x_profile_requires_its_complete_frozen_pair(
    monkeypatch,
) -> None:
    for name in ("CTP_ENV", "CTP_TD_FRONT", "CTP_MD_FRONT"):
        monkeypatch.delenv(name, raising=False)
    td_front, md_front = official_simnow_fronts("set2_7x24_4000x")

    feed = CtpRequestDataFuture(
        ctp_env_profile="set2_7x24_4000x",
        td_front=td_front,
        md_front=md_front,
    )

    assert feed.get_environment_info() == {
        "environment": "demo",
        "simulated": True,
        "verified": True,
        "profile": "set2_7x24_4000x",
        "readiness": "explicit_official_pair",
    }
    with pytest.raises(ValueError, match="does not match its official fronts"):
        CtpRequestDataFuture(
            ctp_env_profile="set2_7x24_4000x",
            td_front=td_front,
            md_front=official_simnow_fronts("set2_7x24")[1],
        )


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


def _offline_connector(*reachable_profiles: str):
    reachable_fronts = {
        front for profile in reachable_profiles for front in official_simnow_fronts(profile)
    }
    calls: list[tuple[str, float]] = []

    def connector(endpoint: str, timeout: float) -> bool:
        calls.append((endpoint, timeout))
        return endpoint in reachable_fronts

    return connector, calls


def test_reachable_selector_uses_named_set2_alternate_after_local_pair_fails() -> None:
    connector, calls = _offline_connector("set2_7x24_vpn")

    selection = select_reachable_ctp_environment(
        env="set2",
        require_profile="set2",
        connector=connector,
        timeout=0.25,
    )

    assert selection.profile == "set2_7x24_vpn"
    assert (selection.td_front, selection.md_front) == official_simnow_fronts("set2_7x24_vpn")
    assert selection.readiness == "tcp_pair_reachable"
    assert [endpoint for endpoint, _timeout in calls] == [
        *official_simnow_fronts("set2_7x24"),
        *official_simnow_fronts("set2_7x24_vpn"),
    ]
    assert all(timeout == 0.25 for _endpoint, timeout in calls)


@pytest.mark.parametrize(
    ("env", "profile", "expected_profile"),
    (
        ("set1", "set1", "set1_group1"),
        ("set2", "set2", "set2_7x24"),
    ),
)
def test_reachable_selector_resolves_group_aliases_to_named_profiles(
    env: str, profile: str, expected_profile: str
) -> None:
    connector, _calls = _offline_connector(expected_profile)

    selection = select_reachable_ctp_environment(
        env=env,
        profile=profile,
        connector=connector,
    )

    assert selection.profile == expected_profile


def test_reachable_selector_keeps_exact_required_profile_exact() -> None:
    connector, calls = _offline_connector("set2_7x24_vpn")

    with pytest.raises(RuntimeError, match="no reachable"):
        select_reachable_ctp_environment(
            env="set2",
            require_profile="set2_7x24",
            connector=connector,
        )

    assert [endpoint for endpoint, _timeout in calls] == list(official_simnow_fronts("set2_7x24"))


def test_reachable_selector_keeps_named_4000x_pair_exact() -> None:
    connector, calls = _offline_connector("set2_7x24_4000x")

    selection = select_reachable_ctp_environment(
        env="set2",
        profile="set2_7x24_4000x",
        require_profile="set2_7x24_4000x",
        connector=connector,
    )

    assert selection.profile == "set2_7x24_4000x"
    assert (selection.td_front, selection.md_front) == official_simnow_fronts("set2_7x24_4000x")
    assert [endpoint for endpoint, _timeout in calls] == list(
        official_simnow_fronts("set2_7x24_4000x")
    )


def test_reachable_selector_rejects_pair_when_only_one_front_connects() -> None:
    td_front, _md_front = official_simnow_fronts("set2_7x24_vpn")

    probe = probe_ctp_environment_pair(
        "set2_7x24_vpn", connector=lambda endpoint, _timeout: endpoint == td_front
    )

    assert probe.td_reachable is True
    assert probe.md_reachable is False
    assert probe.reachable is False

    with pytest.raises(RuntimeError) as excinfo:
        select_reachable_ctp_environment(
            env="set2",
            profile="set2_7x24_vpn",
            connector=lambda endpoint, _timeout: endpoint == td_front,
        )
    assert "tcp://" not in str(excinfo.value)
    assert "182.254" not in str(excinfo.value)


def test_reachable_selector_never_crosses_set_groups_for_profile_or_requirement() -> None:
    connector, calls = _offline_connector("set2_7x24", "set2_7x24_vpn")

    with pytest.raises(RuntimeError, match="no reachable"):
        select_reachable_ctp_environment(
            env="set1",
            profile="set1_group1",
            require_profile="set1",
            connector=connector,
        )
    set1_fronts = set(official_simnow_fronts("set1_group1")) | set(
        official_simnow_fronts("set1_group1_vpn")
    )
    assert {endpoint for endpoint, _timeout in calls} <= set1_fronts

    with pytest.raises(RuntimeError, match="required CTP profile group"):
        select_reachable_ctp_environment(env="set1", require_profile="set2", connector=connector)


@pytest.mark.parametrize(
    "profile",
    [
        "set1_group1",
        "set1_group1_vpn",
        "set1_group2",
        "set2_7x24",
        "set2_7x24_4000x",
        "set2_7x24_vpn",
    ],
)
def test_each_named_simnow_profile_requires_its_exact_frozen_pair(profile: str) -> None:
    td_front, md_front = official_simnow_fronts(profile)

    assert verify_official_simnow_profile(td_front, md_front, profile)
    assert not verify_official_simnow_profile(f"{td_front}.invalid", md_front, profile)


def test_request_feed_auto_detects_once_when_no_complete_pair_is_explicit(
    monkeypatch,
) -> None:
    for name in ("CTP_ENV", "CTP_TD_FRONT", "CTP_MD_FRONT", "CTP_ENV_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    selected_td, selected_md = official_simnow_fronts("set2_7x24_vpn")
    calls = []

    def select_reachable(env: str, **kwargs):
        calls.append((env, kwargs))
        return CtpEnvironmentSelection(
            td_front=selected_td,
            md_front=selected_md,
            environment="simnow",
            profile="set2_7x24_vpn",
            readiness="tcp_pair_reachable",
            reason="tcp_pair_reachable",
            calendar_verified=False,
            explicit=True,
        )

    monkeypatch.setattr(live_ctp_feed, "select_reachable_ctp_environment", select_reachable)
    feed = CtpRequestDataFuture(
        auto_detect_fronts=True,
        ctp_env_profile="set2_7x24",
        front_probe_timeout=0.25,
    )

    assert calls == [
        (
            "",
            {
                "profile": "set2_7x24",
                "require_profile": None,
                "front_probe_timeout": 0.25,
            },
        )
    ]
    assert (feed.td_front, feed.md_front, feed.ctp_env_profile) == (
        selected_td,
        selected_md,
        "set2_7x24_vpn",
    )
    assert feed.ctp_env_readiness == "tcp_pair_reachable"
    assert feed.get_environment_info()["verified"] is True


def test_request_feed_reprobes_stale_global_fronts_in_auto_detect_mode(
    monkeypatch,
) -> None:
    for name in ("CTP_ENV",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CTP_TD_FRONT", "tcp://stale-td")
    monkeypatch.setenv("CTP_MD_FRONT", "tcp://stale-md")
    monkeypatch.setenv("CTP_ENV_PROFILE", "set2_7x24")
    selected_td, selected_md = official_simnow_fronts("set2_7x24_vpn")
    calls = []

    def select_reachable(env: str, **kwargs):
        calls.append((env, kwargs))
        return CtpEnvironmentSelection(
            td_front=selected_td,
            md_front=selected_md,
            environment="simnow",
            profile="set2_7x24_vpn",
            readiness="tcp_pair_reachable",
            reason="tcp_pair_reachable",
            calendar_verified=False,
            explicit=True,
        )

    monkeypatch.setattr(live_ctp_feed, "select_reachable_ctp_environment", select_reachable)
    feed = CtpRequestDataFuture(auto_detect_fronts=True)

    assert calls == [
        (
            "",
            {
                "profile": "set2_7x24",
                "require_profile": None,
                "front_probe_timeout": 3.0,
            },
        )
    ]
    assert (feed.td_front, feed.md_front, feed.ctp_env_profile) == (
        selected_td,
        selected_md,
        "set2_7x24_vpn",
    )


def test_explicit_complete_pair_skips_auto_detection(monkeypatch) -> None:
    td_front, md_front = official_simnow_fronts("set2_7x24_vpn")

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("explicit complete pair must not trigger a TCP probe")

    monkeypatch.setattr(live_ctp_feed, "select_reachable_ctp_environment", unexpected_probe)
    feed = CtpRequestDataFuture(
        auto_detect_fronts=True,
        ctp_env_profile="set2_7x24_vpn",
        td_front=td_front,
        md_front=md_front,
    )

    assert (feed.td_front, feed.md_front, feed.ctp_env_profile) == (
        td_front,
        md_front,
        "set2_7x24_vpn",
    )


def test_environment_profile_keeps_apply_result_verified_for_direct_feed(
    monkeypatch,
) -> None:
    td_front, md_front = official_simnow_fronts("set2_7x24_vpn")
    monkeypatch.setenv("CTP_TD_FRONT", td_front)
    monkeypatch.setenv("CTP_MD_FRONT", md_front)
    monkeypatch.setenv("CTP_ENV_PROFILE", "set2_7x24_vpn")

    feed = CtpRequestDataFuture()

    assert (feed.td_front, feed.md_front) == (td_front, md_front)
    assert feed.ctp_env_profile == "set2_7x24_vpn"
    assert feed.get_environment_info()["environment"] == "demo"


def test_gateway_pins_one_auto_detected_pair_before_creating_streams(
    monkeypatch,
) -> None:
    for name in ("CTP_ENV", "CTP_TD_FRONT", "CTP_MD_FRONT", "CTP_ENV_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    selected_td, selected_md = official_simnow_fronts("set2_7x24_vpn")
    selector_calls = []
    stream_calls = []

    def select_reachable(env: str, **kwargs):
        selector_calls.append((env, kwargs))
        return CtpEnvironmentSelection(
            td_front=selected_td,
            md_front=selected_md,
            environment="simnow",
            profile="set2_7x24_vpn",
            readiness="tcp_pair_reachable",
            reason="tcp_pair_reachable",
            calendar_verified=False,
            explicit=True,
        )

    class FakeMarketStream:
        def __init__(self, _queue, **kwargs) -> None:
            stream_calls.append(("market", kwargs))

    class FakeTradeStream:
        def __init__(self, _queue, **kwargs) -> None:
            stream_calls.append(("trade", kwargs))

    monkeypatch.setattr(live_ctp_feed, "select_reachable_ctp_environment", select_reachable)
    monkeypatch.setattr(adapter_module, "CtpMarketStream", FakeMarketStream)
    monkeypatch.setattr(adapter_module, "CtpTradeStream", FakeTradeStream)

    adapter_module.CtpGatewayAdapter(
        auto_detect_fronts="true",
        ctp_env_profile="set2_7x24",
        front_probe_timeout=0.25,
    )

    assert len(selector_calls) == 1
    assert [name for name, _kwargs in stream_calls] == ["market", "trade"]
    for _name, kwargs in stream_calls:
        assert kwargs["td_front"] == selected_td
        assert kwargs["md_front"] == selected_md
        assert kwargs["ctp_env_profile"] == "set2_7x24_vpn"
        assert kwargs["ctp_env_readiness"] == "tcp_pair_reachable"


def test_gateway_never_pins_an_unverified_custom_request_pair(monkeypatch) -> None:
    stream_calls = []

    class UnverifiedRequestFeed:
        td_front = "tcp://custom-td"
        md_front = "tcp://custom-md"
        ctp_env_profile = "custom_front_override"
        ctp_env_readiness = "explicit_front_override"
        ctp_environment = "custom"

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_environment_info(self):
            return {
                "environment": "unknown",
                "simulated": False,
                "verified": False,
                "profile": self.ctp_env_profile,
                "readiness": self.ctp_env_readiness,
            }

    class FakeMarketStream:
        def __init__(self, _queue, **kwargs) -> None:
            stream_calls.append(("market", kwargs))

    class FakeTradeStream:
        def __init__(self, _queue, **kwargs) -> None:
            stream_calls.append(("trade", kwargs))

    monkeypatch.setattr(adapter_module, "CtpRequestDataFuture", UnverifiedRequestFeed)
    monkeypatch.setattr(adapter_module, "CtpMarketStream", FakeMarketStream)
    monkeypatch.setattr(adapter_module, "CtpTradeStream", FakeTradeStream)

    adapter_module.CtpGatewayAdapter(auto_detect_fronts=True)

    for _name, kwargs in stream_calls:
        assert kwargs["td_front"] != "tcp://custom-td"
        assert kwargs["md_front"] != "tcp://custom-md"
        assert kwargs.get("ctp_env_profile") != "custom_front_override"
