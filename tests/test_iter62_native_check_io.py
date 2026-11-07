"""Iteration 62, defect 25 follow-up: a client start reads no package file.

SimNow 7x24, 2026-11-07 12:50: the MD replacement start failed 0.2 s after it
began, before ``CreateFtdcMdApi``/``Init``.  ``MdClient.start`` called
``_check_native_module``, which built the full native diagnostics on every
start: it hashed every ``.py`` file of the package and the native binaries
with unguarded reads, and the package directory (under ``~/Documents``) was
very likely unreadable for that process tree at the time.

Covered (UNIT/FAKE; no CTP library call, front or account is involved):

* an MD and a trader start pass the native check and reach the native API
  factory without reading any package file while every read is refused;
* the diagnostics never raise on a file-system error: an unreadable file keeps
  its manifest entry with ``sha256 == ""`` and is counted, an unlistable
  directory marks the listing incomplete, unreadable native files hash to "";
* a runtime that is not loaded still fails with the same ImportError text.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import bt_api_ctp
from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.ctp.client import MdClient, TraderClient

PACKAGE_ROOT = Path(bt_api_ctp.__file__).resolve().parent
FRONT = "tcp://127.0.0.1:19803"
_NOT_LOADED_SUFFIX = (
    ". Install a native extension matching this OS, Python ABI, and selected CTP runtime."
)


def _under(path: object, roots: set[Path]) -> bool:
    resolved = Path(os.fspath(path)).resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


def _deny_package_reads(
    monkeypatch: pytest.MonkeyPatch,
    *,
    only: set[Path] | None = None,
    unlistable: set[Path] | None = None,
) -> list[str]:
    """Refuse content reads (and listings of ``unlistable``) like a lost EPERM.

    Returns the refused attempts, as package-relative paths.
    """

    refused: list[str] = []
    real_read_bytes = Path.read_bytes
    real_open = Path.open
    real_scandir = os.scandir
    real_glob = Path.glob
    targets = {path.resolve() for path in only} if only is not None else {PACKAGE_ROOT}

    def refuse(path: object) -> None:
        refused.append(Path(os.fspath(path)).resolve().relative_to(PACKAGE_ROOT).as_posix())
        raise PermissionError(1, "Operation not permitted")

    def read_bytes(self: Path) -> bytes:
        if _under(self, targets):
            refuse(self)
        return real_read_bytes(self)

    def open_(self: Path, *args: object, **kwargs: object):
        if _under(self, targets):
            refuse(self)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "open", open_)
    if unlistable:
        directories = {path.resolve() for path in unlistable}

        def scandir(path: object = "."):
            if Path(os.fspath(path)).resolve() in directories:
                refuse(path)
            return real_scandir(path)

        def glob(self: Path, pattern: str, *args: object, **kwargs: object):
            # Some Python versions propagate a listing error from glob.
            if self.resolve() in directories:
                refuse(self)
            return real_glob(self, pattern, *args, **kwargs)

        monkeypatch.setattr(os, "scandir", scandir)
        monkeypatch.setattr(Path, "glob", glob)
    return refused


class _FactoryReached(Exception):
    """The start passed the native check and asked for a native API."""


@pytest.mark.parametrize("kind", ["md", "trader"])
def test_a_client_start_reads_no_package_file(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    # Portable: the decision is the in-memory loaded check, not a file read.
    monkeypatch.setattr(client_module, "_is_vendored_ctp_native_loaded", lambda: True)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _prefix: "offline-flow")
    reached: list[str] = []

    def create(_flow: str) -> None:
        reached.append(kind)
        raise _FactoryReached()

    if kind == "md":
        monkeypatch.setattr(
            client_module, "CThostFtdcMdApi", SimpleNamespace(CreateFtdcMdApi=create)
        )
        client = MdClient(FRONT, "9999", "investor", "secret")
    else:
        monkeypatch.setattr(
            client_module, "CThostFtdcTraderApi", SimpleNamespace(CreateFtdcTraderApi=create)
        )
        client = TraderClient(FRONT, "9999", "investor", "secret", auto_settlement_confirm=False)
    refused = _deny_package_reads(monkeypatch)

    with pytest.raises(_FactoryReached):
        client.start(block=False, join_observer=False)
    assert reached == [kind]
    assert refused == []  # not one package file was read on the start path


def test_diagnostics_report_an_unreadable_package_file_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreadable = PACKAGE_ROOT / "market_provider.py"
    refused = _deny_package_reads(monkeypatch, only={unreadable})

    diagnostics = client_module.get_ctp_native_diagnostics()

    manifest = diagnostics["ctp_package_manifest"]
    digests = {entry["path"]: entry["sha256"] for entry in manifest}
    assert all(set(entry) == {"path", "sha256"} for entry in manifest)
    assert digests["market_provider.py"] == ""
    assert all(
        len(digest) == 64 for path, digest in digests.items() if path != "market_provider.py"
    )
    assert diagnostics["ctp_package_unreadable_files"] == 1
    assert diagnostics["ctp_package_listing_complete"] is True
    assert diagnostics["ctp_package_read_error"] == "PermissionError"
    assert len(diagnostics["ctp_package_sha256"]) == 64
    assert "market_provider.py" in refused
    # The two-value helper keeps its shape and the same marker.
    helper_manifest, helper_sha256 = client_module._ctp_python_package_identity()
    assert helper_manifest == manifest and helper_sha256 == diagnostics["ctp_package_sha256"]


def test_diagnostics_survive_an_unreadable_package_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_dir = PACKAGE_ROOT / "ctp"
    _deny_package_reads(monkeypatch, unlistable={native_dir})

    diagnostics = client_module.get_ctp_native_diagnostics()

    manifest = diagnostics["ctp_package_manifest"]
    assert manifest and all(entry["sha256"] == "" for entry in manifest)
    assert not any(entry["path"].startswith("ctp/") for entry in manifest)
    assert diagnostics["ctp_package_unreadable_files"] == len(manifest)
    assert diagnostics["ctp_package_listing_complete"] is False
    assert diagnostics["ctp_package_read_error"] == "PermissionError"
    assert diagnostics["matching_extension_sha256"] == {}
    assert diagnostics["native_module_sha256"] == {}
    assert diagnostics["loaded_module_sha256"] == ""


def test_the_not_loaded_check_still_raises_the_same_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "_is_vendored_ctp_native_loaded", lambda: False)
    monkeypatch.setattr(
        client_module,
        "_selected_runtime_modules",
        lambda: {"bt_api_ctp.ctp": "/tmp/bt_api_ctp/ctp/__init__.py"},
    )
    expected = (
        client_module._format_selected_native_diagnostics(
            client_module.get_ctp_native_diagnostics()
        )
        + _NOT_LOADED_SUFFIX
    )
    assert expected.startswith("CTP runtime vendored_bt_api_py has no verified native extension: ")
    with pytest.raises(ImportError) as readable:
        client_module._check_native_module()
    assert str(readable.value) == expected

    # Even with every package file unreadable, the error stays the same.
    _deny_package_reads(monkeypatch)
    with pytest.raises(ImportError) as unreadable:
        client_module._check_native_module()
    assert str(unreadable.value) == expected
