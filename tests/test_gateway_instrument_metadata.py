"""CTP side of the Gateway's provider-neutral instrument metadata capability.

The binding lives in bt_api_ctp, so the Gateway never imports a provider
package. These tests pin the binding's own contract; the end-to-end read is
covered by the SDK integration test ``test_service_account_instrument_metadata``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import bt_api_ctp.gateway_instrument_metadata as binding_module
from bt_api_ctp.gateway_instrument_metadata import (
    CtpGatewayMetadataError,
    GatewayCtpInstrumentMetadataBindingError,
    bind_ctp_instrument_metadata_provider,
)


class _Provider:
    ctp_request_feed = object()
    ctp_market_stream = object()

    def connect(self, **_kwargs):
        return None

    def read_account(self, **_kwargs):
        return None

    def close(self):
        return True


def test_module_does_not_depend_on_the_gateway_package() -> None:
    tree = ast.parse(Path(binding_module.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(name.startswith("bt_api_gateway") for name in imported)


def test_provider_without_lifecycle_methods_is_rejected() -> None:
    with pytest.raises(GatewayCtpInstrumentMetadataBindingError) as raised:
        bind_ctp_instrument_metadata_provider(
            object(), request_feed=object(), market_stream=object()
        )
    assert raised.value.code == "INVALID_ACCOUNT_PROVIDER"


def test_non_ctp_feed_objects_are_rejected() -> None:
    provider = _Provider()
    with pytest.raises(GatewayCtpInstrumentMetadataBindingError) as raised:
        bind_ctp_instrument_metadata_provider(
            provider,
            request_feed=provider.ctp_request_feed,
            market_stream=provider.ctp_market_stream,
        )
    assert raised.value.code == "INVALID_CTP_FEED_BINDING"


def test_binding_error_codes_are_finite() -> None:
    assert GatewayCtpInstrumentMetadataBindingError("anything").code == (
        "CTP_SESSION_BINDING_INVALID"
    )


def test_seal_check_accepts_only_live_reader_results(monkeypatch) -> None:
    calls = []

    def seal(value):
        calls.append(value)
        return value == "sealed"

    monkeypatch.setattr(binding_module, "_is_current_ctp_instrument_metadata", seal)
    # The check is a method of the bound provider; it needs no live session.
    bound = object.__new__(binding_module.GatewayCtpInstrumentMetadataProvider)
    assert bound.is_current_instrument_metadata("sealed") is True
    assert bound.is_current_instrument_metadata("forged") is False
    assert calls == ["sealed", "forged"]


def test_metadata_errors_carry_a_finite_code() -> None:
    error = CtpGatewayMetadataError("PROVIDER_RESULT_GENERATION_STALE")
    assert error.code == "PROVIDER_RESULT_GENERATION_STALE"
    assert str(error) == "PROVIDER_RESULT_GENERATION_STALE"
