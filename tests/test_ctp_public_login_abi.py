"""Offline contracts for the public Trader login ABI guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp import _ctp_base, ctp_trader_api


def test_public_trader_login_delegates_to_the_abi_guard(monkeypatch) -> None:
    calls = []
    api = object.__new__(ctp_trader_api.CThostFtdcTraderApi)
    field = object()

    def submit(received_api, received_field, received_request_id):
        calls.append((received_api, received_field, received_request_id))
        return 31

    monkeypatch.setattr(ctp_trader_api, "_submit_public_trader_user_login", submit)

    assert api.ReqUserLogin(field, 17) == 31
    assert calls == [(api, field, 17)]


def test_public_login_uses_the_two_argument_call_off_darwin_arm64(monkeypatch) -> None:
    calls = []
    api = object()
    field = object()

    def direct(received_api, received_field, received_request_id):
        calls.append((received_api, received_field, received_request_id))
        return 37

    monkeypatch.setattr(_ctp_base, "_is_darwin_arm64", lambda: False)
    monkeypatch.setattr(
        _ctp_base,
        "_ctp",
        SimpleNamespace(CThostFtdcTraderApi_ReqUserLogin=direct),
    )

    assert _ctp_base._submit_public_trader_user_login(api, field, 19) == 37
    assert calls == [(api, field, 19)]


def test_public_login_fallback_uses_the_two_argument_call_on_darwin(
    monkeypatch,
) -> None:
    calls = []
    api = object()
    field = object()

    def direct(received_api, received_field, received_request_id):
        calls.append((received_api, received_field, received_request_id))
        return 41

    monkeypatch.setattr(_ctp_base, "_is_darwin_arm64", lambda: True)
    monkeypatch.setattr(_ctp_base, "is_ctp_native_loaded", lambda: False)
    monkeypatch.setattr(
        _ctp_base,
        "_ctp",
        SimpleNamespace(CThostFtdcTraderApi_ReqUserLogin=direct),
    )

    assert _ctp_base._submit_public_trader_user_login(api, field, 23) == 41
    assert calls == [(api, field, 23)]


def test_public_login_uses_the_verified_raw_shim_on_darwin_arm64(monkeypatch) -> None:
    calls = []
    api = object()
    field = object()

    def shim(received_api, received_field, received_request_id):
        calls.append((received_api, received_field, received_request_id))
        return 43

    monkeypatch.setattr(_ctp_base, "_is_darwin_arm64", lambda: True)
    monkeypatch.setattr(_ctp_base, "is_ctp_native_loaded", lambda: True)
    monkeypatch.setattr(
        _ctp_base,
        "_has_audited_darwin_arm64_login_shim",
        lambda: True,
    )
    monkeypatch.setattr(
        _ctp_base,
        "_ctp",
        SimpleNamespace(
            **{
                _ctp_base._DARWIN_ARM64_LOGIN_SHIM: shim,
                "CThostFtdcTraderApi_ReqUserLogin": lambda *_args: pytest.fail(
                    "Darwin arm64 native login must not use the direct binding"
                ),
            }
        ),
    )

    assert _ctp_base._submit_public_trader_user_login(api, field, 29) == 43
    assert calls == [(api, field, 29)]


def test_public_login_fails_closed_with_a_stable_code_when_unverified(
    monkeypatch,
) -> None:
    direct_calls = []
    api = object()
    field = object()

    def direct(*args):
        direct_calls.append(args)
        return 0

    monkeypatch.setattr(_ctp_base, "_is_darwin_arm64", lambda: True)
    monkeypatch.setattr(_ctp_base, "is_ctp_native_loaded", lambda: True)
    monkeypatch.setattr(
        _ctp_base,
        "_has_audited_darwin_arm64_login_shim",
        lambda: False,
    )
    monkeypatch.setattr(
        _ctp_base,
        "_ctp",
        SimpleNamespace(CThostFtdcTraderApi_ReqUserLogin=direct),
    )

    with pytest.raises(_ctp_base.CtpNativeAbiError) as excinfo:
        _ctp_base._submit_public_trader_user_login(api, field, 31)

    assert excinfo.value.code == "ctp_trader_login_abi_unverified"
    assert str(excinfo.value) == "ctp_trader_login_abi_unverified"
    assert direct_calls == []


def test_audited_public_login_shim_requires_exact_version_and_framework_hash(
    monkeypatch, tmp_path
) -> None:
    hash_paths = []

    monkeypatch.setattr(_ctp_base, "is_ctp_native_loaded", lambda: True)
    monkeypatch.setattr(_ctp_base, "_ctp_package_dir", lambda: tmp_path)
    monkeypatch.setattr(
        _ctp_base,
        "_sha256_file",
        lambda path: hash_paths.append(path)
        or _ctp_base._AUDITED_DARWIN_ARM64_TRADER_SHA256,
    )
    monkeypatch.setattr(
        _ctp_base,
        "_ctp",
        SimpleNamespace(
            **{
                _ctp_base._DARWIN_ARM64_LOGIN_SHIM: lambda *_args: 0,
                "CThostFtdcTraderApi_GetApiVersion": lambda: (
                    _ctp_base._AUDITED_DARWIN_ARM64_TRADER_VERSION
                ),
            }
        ),
    )

    assert _ctp_base._has_audited_darwin_arm64_login_shim() is True
    assert hash_paths == [
        tmp_path
        / "thosttraderapi_se.framework"
        / "Versions"
        / "A"
        / "thosttraderapi_se"
    ]


@pytest.mark.parametrize(
    ("version", "framework_sha256"),
    [
        (
            "v6.7.7_MacOS_20240716 15:00:01",
            _ctp_base._AUDITED_DARWIN_ARM64_TRADER_SHA256,
        ),
        (_ctp_base._AUDITED_DARWIN_ARM64_TRADER_VERSION, "0" * 64),
    ],
)
def test_audited_public_login_shim_rejects_version_or_hash_drift(
    monkeypatch, tmp_path, version, framework_sha256
) -> None:
    monkeypatch.setattr(_ctp_base, "is_ctp_native_loaded", lambda: True)
    monkeypatch.setattr(_ctp_base, "_ctp_package_dir", lambda: tmp_path)
    monkeypatch.setattr(_ctp_base, "_sha256_file", lambda _path: framework_sha256)
    monkeypatch.setattr(
        _ctp_base,
        "_ctp",
        SimpleNamespace(
            **{
                _ctp_base._DARWIN_ARM64_LOGIN_SHIM: lambda *_args: 0,
                "CThostFtdcTraderApi_GetApiVersion": lambda: version,
            }
        ),
    )

    assert _ctp_base._has_audited_darwin_arm64_login_shim() is False
