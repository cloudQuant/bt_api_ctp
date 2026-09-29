import sys

import pytest

from bt_api_ctp.ctp.client import (
    CtpExecutionGateError,
    CtpRuntimeSimNowCredentialBinding,
    TraderClient,
    _issue_ctp_execution_authority_for_core,
)
from bt_api_ctp.ctp_env_selector import official_simnow_fronts
from bt_api_ctp.feeds.live_ctp_feed import _resolve_ctp_runtime_kwargs

_PROFILE = "config_front_pair"
_TD_FRONT = "tcp://configured-td.invalid:41001"
_MD_FRONT = "tcp://configured-md.invalid:41002"


def test_missing_parent_approval_api_fails_closed_without_import_time_dependency(monkeypatch):
    client = TraderClient(
        _TD_FRONT,
        "broker",
        "account",
        "password",
        md_front=_MD_FRONT,
        ctp_env_profile=_PROFILE,
    )
    capability = _issue_ctp_execution_authority_for_core()
    client.configure_execution_gate(capability)
    binding = CtpRuntimeSimNowCredentialBinding(
        owner=object(),
        approval_context=object(),
        approval=object(),
        credential_binding_verifier=object(),
        td_front=_TD_FRONT,
        md_front=_MD_FRONT,
        environment_profile=_PROFILE,
    )
    monkeypatch.setitem(sys.modules, "bt_api_py", None)

    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_credential_binding_rejected"):
        client.configure_runtime_simnow_credential_binding(capability, binding)


def test_legacy_trader_client_constructor_keeps_its_existing_positional_shape():
    client = TraderClient(
        "legacy-front",
        "broker",
        "account",
        "password",
        "app-id",
        "auth-code",
        False,
    )

    assert client.front == "legacy-front"
    assert client.md_front is None
    assert client._bound_md_front is None
    assert client.ctp_env_profile is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ctp_env_profile": _PROFILE},
        {"md_front": _MD_FRONT},
        {"md_front": None, "ctp_env_profile": _PROFILE},
    ],
)
def test_config_front_pair_requires_both_explicit_fronts_and_profile(kwargs):
    with pytest.raises(ValueError, match="front pair and profile"):
        TraderClient(_TD_FRONT, "broker", "account", "password", **kwargs)


@pytest.mark.parametrize(
    "td_front,md_front",
    [
        ("https://td.invalid:41001", _MD_FRONT),
        (_TD_FRONT, "tcp://md.invalid"),
        (_TD_FRONT, "tcp://md.invalid:0"),
        (_TD_FRONT, "tcp://user@md.invalid:41002"),
        (_TD_FRONT, "tcp://md.invalid:41002/path"),
        (_TD_FRONT, " tcp://md.invalid:41002"),
    ],
)
def test_config_front_pair_rejects_malformed_explicit_endpoints(td_front, md_front):
    with pytest.raises(ValueError, match="invalid configured CTP front"):
        TraderClient(
            td_front,
            "broker",
            "account",
            "password",
            md_front=md_front,
            ctp_env_profile=_PROFILE,
        )


def test_config_front_pair_keeps_the_configured_pair_as_read_only_constructor_identity():
    client = TraderClient(
        _TD_FRONT,
        "broker",
        "account",
        "password",
        md_front=_MD_FRONT,
        ctp_env_profile=_PROFILE,
    )

    assert client._bound_md_front == _MD_FRONT
    assert client.ctp_env_profile == _PROFILE
    assert client._bound_identity_is_current() is True
    with pytest.raises(AttributeError):
        client._bound_md_front = "tcp://substituted.invalid:41003"
    with pytest.raises(AttributeError):
        client.ctp_env_profile = "set1_group1"

    client.md_front = "tcp://substituted.invalid:41003"
    assert client._bound_identity_is_current() is False


def test_neutral_pair_stays_closed_to_generic_arm_and_settlement_grants():
    client = TraderClient(
        _TD_FRONT,
        "broker",
        "account",
        "password",
        md_front=_MD_FRONT,
        ctp_env_profile=_PROFILE,
    )
    capability = _issue_ctp_execution_authority_for_core()
    client.configure_execution_gate(capability)

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_simnow_execution_not_admitted",
    ):
        client.arm_execution_gate(capability, object())

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_simnow_execution_not_admitted",
    ):
        client._issue_settlement_authorization_for_core(
            capability,
            environment_profile=_PROFILE,
            environment_verified=True,
        )

    assert client.get_request_counts()["settlement_confirm"] == 0
    assert client.get_request_counts()["order_insert"] == 0
    assert client.get_request_counts()["order_action"] == 0


def test_neutral_pair_resolution_keeps_explicit_fronts_without_claiming_verification():
    resolved, profile = _resolve_ctp_runtime_kwargs(
        {
            "td_front": _TD_FRONT,
            "md_front": _MD_FRONT,
            "ctp_env_profile": _PROFILE,
        }
    )
    assert profile == _PROFILE
    assert (resolved["td_front"], resolved["md_front"]) == (_TD_FRONT, _MD_FRONT)
    assert resolved["ctp_env_readiness"] == "explicit_configured_pair_unverified"


def test_official_td_front_rejects_generic_execution_grant():
    td_front, _md_front = official_simnow_fronts("set2_7x24")
    client = TraderClient(td_front, "broker", "account", "password")
    capability = _issue_ctp_execution_authority_for_core()
    client.configure_execution_gate(capability)

    with pytest.raises(CtpExecutionGateError, match="ctp_simnow_execution_not_admitted"):
        client._issue_settlement_authorization_for_core(
            capability,
            environment_profile="set2_7x24",
            environment_verified=True,
        )
