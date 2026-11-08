"""Synthetic filesystem and native-factory checks; no CTP connection."""

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.ctp.client import MdClient, OneShotMdDiagnosticClient, TraderClient
from bt_api_ctp.feeds.live_ctp_feed import CtpMarketStream, CtpRequestData, CtpTradeStream
from bt_api_ctp.trusted_flow import trusted_flow


@pytest.fixture
def root(tmp_path):
    path = tmp_path.resolve() / "trusted"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def opts(root):
    return {"trusted_flow_root": str(root), "flow_account_scope": "account-A"}


def test_account_hash_isolation_and_td_md_separation(root):
    first = trusted_flow(root, "account-A")
    second = trusted_flow(root, "account-B")
    td = Path(first.native_path("td"))
    md = Path(first.native_path("md"))
    other = Path(second.native_path("td"))
    assert td == root / hashlib.sha256(b"account-A").hexdigest() / "td"
    assert md == td.parent / "md" and other.parent != td.parent
    assert all(
        path.stat().st_mode & 0o777 == 0o700 for path in (td.parent, td, md, other.parent, other)
    )
    # The opaque account scope is never treated as a path component.
    escaped = Path(trusted_flow(root, "../account").native_path("td"))
    assert escaped.parent.parent == root


@pytest.mark.parametrize(
    "root_value,scope",
    [(None, "A"), ("relative", "A"), ("/tmp", None), (None, ""), ("/tmp", ""), ("/tmp", " A ")],
)
def test_invalid_pair_or_scope_fails(root_value, scope):
    with pytest.raises(ValueError):
        trusted_flow(root_value, scope)


def test_nonexistent_file_dot_and_symlink_roots_rejected(root, tmp_path):
    with pytest.raises(ValueError):
        trusted_flow(root / "missing", "A")
    file = root / "file"
    file.write_text("x")
    with pytest.raises(ValueError):
        trusted_flow(file, "A")
    with pytest.raises(ValueError):
        trusted_flow(str(root) + "/../trusted", "A")
    link = root.parent / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError):
        trusted_flow(link, "A")
    nested_link = root.parent / "link-parent"
    nested_link.symlink_to(root.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        trusted_flow(nested_link / root.name, "A")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")
def test_wide_root_or_existing_child_is_rejected(root):
    root.chmod(0o755)
    with pytest.raises(ValueError):
        trusted_flow(root, "A")
    root.chmod(0o700)
    account = root / hashlib.sha256(b"A").hexdigest()
    account.mkdir(mode=0o755)
    account.chmod(0o755)
    with pytest.raises(ValueError):
        trusted_flow(root, "A").native_path("td")
    assert account.stat().st_mode & 0o777 == 0o755


def test_symlink_child_and_replaced_root_or_child_are_rejected(root, tmp_path):
    account = root / hashlib.sha256(b"A").hexdigest()
    account.symlink_to(root.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        trusted_flow(root, "A").native_path("td")
    account.unlink()
    flow = trusted_flow(root, "A")
    child = Path(flow.native_path("td"))
    child.rename(child.with_name("old-td"))
    child.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="identity changed"):
        flow.native_path("td")
    bound = trusted_flow(root, "B")
    root.rename(root.with_name("old-root"))
    root.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="identity changed"):
        bound.native_path("td")


class FactoryReached(Exception):
    pass


@pytest.mark.parametrize(
    "kind,cls,api_name,method",
    [
        ("td", TraderClient, "CThostFtdcTraderApi", "CreateFtdcTraderApi"),
        ("md", MdClient, "CThostFtdcMdApi", "CreateFtdcMdApi"),
    ],
)
def test_native_factory_receives_bound_path_and_legacy_temp(
    monkeypatch, root, opts, kind, cls, api_name, method
):
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _prefix: "legacy-temp-flow")
    seen = []

    def factory(path):
        seen.append(path)
        raise FactoryReached

    monkeypatch.setattr(client_module, api_name, SimpleNamespace(**{method: factory}))
    for kwargs in (opts, {}):
        instance = cls("tcp://test", "broker", "user", "password", **kwargs)
        with pytest.raises(FactoryReached):
            instance.start(block=False, join_observer=False)
    expected = root / hashlib.sha256(b"account-A").hexdigest() / kind
    assert seen == [str(expected) + os.sep, "legacy-temp-flow"]


@pytest.mark.parametrize(
    "cls,api_name,method",
    [
        (TraderClient, "CThostFtdcTraderApi", "CreateFtdcTraderApi"),
        (MdClient, "CThostFtdcMdApi", "CreateFtdcMdApi"),
    ],
)
def test_replaced_root_before_start_is_refused_before_factory(
    monkeypatch, root, opts, cls, api_name, method
):
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    seen = []
    monkeypatch.setattr(
        client_module, api_name, SimpleNamespace(**{method: lambda path: seen.append(path)})
    )
    instance = cls("tcp://test", "broker", "user", "password", **opts)
    root.rename(root.with_name("old-root"))
    root.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="identity changed"):
        instance.start(block=False, join_observer=False)
    assert seen == [] and instance._starting_generation is None


def test_oneshot_rejects_override_and_keeps_temporary_lifetime(opts):
    with pytest.raises(TypeError):
        OneShotMdDiagnosticClient("front", "broker", "user", "password", **opts)
    instance = OneShotMdDiagnosticClient("front", "broker", "user", "password")
    flow = Path(instance._native_flow_dir())
    assert flow.is_dir() and flow.name.startswith("ctp-md-one-shot-")
    instance._diagnostic_flow_directory.cleanup()
    assert not flow.exists()


def test_request_market_and_owned_trade_pass_bound_configuration(monkeypatch, opts):
    monkeypatch.setattr(TraderClient, "start", lambda *_a, **_k: None)
    monkeypatch.setattr(TraderClient, "wait_ready", lambda *_a, **_k: False)
    monkeypatch.setattr(MdClient, "start", lambda *_a, **_k: None)
    request = CtpRequestData(None, **opts)
    request.connect()
    market = CtpMarketStream(None, **opts)
    market.connect()
    trade = CtpTradeStream(None, **opts)
    trade.connect()
    expected = request._trusted_flow.binding
    assert request.trader_client.trusted_flow_binding == expected
    assert market._md_client.trusted_flow_binding == expected
    assert trade.trader_client.trusted_flow_binding == expected


def test_borrowed_trader_must_match_request_and_stream_binding(monkeypatch, root, opts):
    monkeypatch.setattr(TraderClient, "start", lambda *_a, **_k: None)
    monkeypatch.setattr(TraderClient, "wait_ready", lambda *_a, **_k: False)
    request = CtpRequestData(None, **opts)
    request.connect()
    stream = CtpTradeStream(None, request_feed=request, **opts)
    stream.connect()
    assert stream.trader_client is request.trader_client
    stream.disconnect()
    bad = CtpTradeStream(
        None, request_feed=request, trusted_flow_root=str(root), flow_account_scope="B"
    )
    with pytest.raises(
        client_module.CtpExecutionGateError, match="ctp_trusted_flow_binding_mismatch"
    ):
        bad.connect()
    request._trader = TraderClient("front", "broker", "user", "password")
    with pytest.raises(
        client_module.CtpExecutionGateError, match="ctp_trusted_flow_binding_mismatch"
    ):
        CtpTradeStream(None, request_feed=request, **opts).connect()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory handle semantics")
def test_root_swap_during_open_cannot_create_under_replacement(monkeypatch, root):
    flow = trusted_flow(root, "A")
    original_open = os.open
    swapped = False

    def replace_on_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == root and not swapped:
            swapped = True
            root.rename(root.with_name("previous-root"))
            root.mkdir(mode=0o700)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_on_open)
    with pytest.raises(ValueError, match="parent identity changed"):
        flow.native_path("td")
    assert swapped and list(root.iterdir()) == []


def test_new_scope_root_binding_rejected_before_request_reuse(monkeypatch, root, opts):
    monkeypatch.setattr(TraderClient, "start", lambda *_a, **_k: None)
    monkeypatch.setattr(TraderClient, "wait_ready", lambda *_a, **_k: False)
    request = CtpRequestData(None, **opts)
    request._trader = TraderClient(
        "front", "broker", "user", "password", trusted_flow_root=root, flow_account_scope="B"
    )
    with pytest.raises(
        client_module.CtpExecutionGateError, match="ctp_trusted_flow_binding_mismatch"
    ):
        request.connect()
