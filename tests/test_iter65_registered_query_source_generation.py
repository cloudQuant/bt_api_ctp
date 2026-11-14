"""Managed callback registration and live query generations are distinct.

All native calls use the existing fake Trader API; no CTP front or account is
contacted.  A registration tag of zero is not a live session generation.
"""

from dataclasses import replace
import time
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.callback_ingress import CtpTraderCallbackSourceTagsV2
from bt_api_ctp.registered_sim_session import (
    CtpQuerySessionBindingError,
    capture_query_session,
    validate_query_session,
)
from test_ctp_callback_ingress_client import _new_active_client


def test_managed_first_login_binds_zero_registration_generation(monkeypatch):
    client, _api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        tags = client._spi._callback_source_tags
        assert type(tags) is CtpTraderCallbackSourceTagsV2
        assert tags.connection_generation == 0
        assert client.connection_generation == 1
        binding = capture_query_session(client, environment="simnow")
        assert binding._source_tags[-1] == 0
        assert binding.connection_generation == 1
        assert validate_query_session(client, binding, now_monotonic=time.monotonic())
    finally:
        client.stop()


@pytest.mark.parametrize("bad", [-1, True, 0.0])
def test_invalid_registration_generation_remains_rejected(monkeypatch, bad):
    client, _api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        tags = client._spi._callback_source_tags
        client._spi._callback_source_tags = SimpleNamespace(
            source_instance_id=tags.source_instance_id,
            native_client_epoch=tags.native_client_epoch,
            native_api_source_id=tags.native_api_source_id,
            native_spi_source_id=tags.native_spi_source_id,
            native_api_generation=tags.native_api_generation,
            connection_generation=bad,
        )
        with pytest.raises(CtpQuerySessionBindingError):
            capture_query_session(client, environment="simnow")
    finally:
        client.stop()


def test_zero_registration_tag_does_not_allow_zero_live_generation(monkeypatch):
    client, _api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        scope = client.get_query_session_scope()
        assert scope.source_tags[-1] == 0 and scope.connection_generation == 1
        monkeypatch.setattr(client, "get_query_session_scope", lambda: replace(scope, connection_generation=0))
        with pytest.raises(CtpQuerySessionBindingError):
            capture_query_session(client, environment="simnow")
    finally:
        client.stop()


@pytest.mark.parametrize("field,value", [
    ("account_fingerprint", "0" * 16),
    ("broker_id", "other-broker"),
    ("investor_id", "other-investor"),
    ("trading_day", "20990101"),
])
def test_zero_registration_tag_does_not_relax_live_scope(monkeypatch, field, value):
    client, _api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        scope = client.get_query_session_scope()
        assert scope.source_tags[-1] == 0
        changed = replace(scope, **{field: value})
        monkeypatch.setattr(client, "get_query_session_scope", lambda: changed)
        with pytest.raises(CtpQuerySessionBindingError):
            capture_query_session(client, environment="simnow")
    finally:
        client.stop()


def test_disconnect_fences_previous_binding_with_zero_registration_tag(monkeypatch):
    client, api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        binding = capture_query_session(client, environment="simnow")
        assert binding._source_tags[-1] == 0
        api.spi.OnFrontDisconnected(1001)
        assert not validate_query_session(client, binding, now_monotonic=time.monotonic())
        with pytest.raises(CtpQuerySessionBindingError):
            capture_query_session(client, environment="simnow")
    finally:
        client.stop()


def test_new_managed_client_cannot_reuse_old_zero_tag_binding(monkeypatch):
    first, _api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        old = capture_query_session(first, environment="simnow")
        assert old._source_tags[-1] == 0
    finally:
        first.stop()

    second, _api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        new = capture_query_session(second, environment="simnow")
        assert new._source_tags[-1] == 0
        assert new.connection_generation == 1
        assert new.source_ref != old.source_ref
        assert not validate_query_session(second, old, now_monotonic=time.monotonic())
        assert validate_query_session(second, new, now_monotonic=time.monotonic())
    finally:
        second.stop()


def test_zero_generation_does_not_override_callback_source_identity(monkeypatch):
    client, _api, _sink, _owner = _new_active_client(monkeypatch)
    try:
        tags = client._spi._callback_source_tags
        client._spi._callback_source_tags = replace(tags, native_api_source_id="different-api")
        with pytest.raises(CtpQuerySessionBindingError):
            capture_query_session(client, environment="simnow")
    finally:
        client.stop()
