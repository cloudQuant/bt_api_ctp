"""Bind CTP flow directories to an explicitly trusted account root.

Native CTP accepts a path string, not a directory handle. Even after the
identity recheck below, protection against replacement between that recheck
and the vendor's open depends on trusted directory permissions. This is not a
process lock or cross-host writer fence.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


def _directory_identity(path: Path, *, private: bool) -> tuple[int, int]:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode) or attributes & reparse or not stat.S_ISDIR(info.st_mode):
        raise ValueError("trusted flow path must contain only real directories")
    if private and os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("trusted flow directories must not grant group or other access")
    return info.st_dev, info.st_ino


def _ancestors(path: Path) -> list[Path]:
    return list(reversed((path, *path.parents)))


class TrustedFlow:
    """Constructor-bound root and opaque canonical scope; no native calls."""

    def __init__(self, root, account_scope):
        if not isinstance(root, (str, os.PathLike)):
            raise ValueError("trusted_flow_root must be an absolute directory")
        raw = os.fspath(root)
        if not isinstance(raw, str) or not raw or "\x00" in raw or not os.path.isabs(raw):
            raise ValueError("trusted_flow_root must be an absolute directory")
        if any(part in (".", "..") for part in raw.replace("\\", "/").split("/")):
            raise ValueError("trusted_flow_root must not contain dot components")
        if (
            type(account_scope) is not str
            or not account_scope
            or account_scope != account_scope.strip()
        ):
            raise ValueError("flow_account_scope must be a nonempty canonical string")
        if "\x00" in account_scope:
            raise ValueError("flow_account_scope must be a nonempty canonical string")
        self.root = Path(raw)
        self.account_scope = account_scope
        self.scope_hash = hashlib.sha256(account_scope.encode("utf-8")).hexdigest()
        self._root_chain = self._capture_root()
        self._child_chains: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {}

    def _capture_root(self):
        try:
            return tuple(
                _directory_identity(path, private=path == self.root)
                for path in _ancestors(self.root)
            )
        except OSError as exc:
            raise ValueError("trusted flow root must be an existing real directory") from exc

    @property
    def binding(self):
        return (str(self.root), self.scope_hash, self._root_chain)

    def verify_root(self):
        if self._capture_root() != self._root_chain:
            raise ValueError("trusted flow root identity changed")

    def _child(self, parent: Path, name: str, expected_parent: tuple[int, int]) -> Path:
        child = parent / name
        try:
            if os.name == "posix":
                # Creation through a checked, no-follow parent handle avoids
                # silently following a replaced account-directory symlink.
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                fd = os.open(parent, flags)
                try:
                    parent_stat = os.fstat(fd)
                    if (
                        parent_stat.st_dev,
                        parent_stat.st_ino,
                    ) != expected_parent or _directory_identity(
                        parent, private=True
                    ) != expected_parent:
                        raise ValueError("trusted flow parent identity changed")
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                        raise ValueError("trusted flow child must be a private real directory")
                    if (info.st_dev, info.st_ino) != _directory_identity(child, private=True):
                        raise ValueError("trusted flow child identity changed")
                finally:
                    os.close(fd)
            else:
                if _directory_identity(parent, private=True) != expected_parent:
                    raise ValueError("trusted flow parent identity changed")
                try:
                    child.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                _directory_identity(child, private=True)
        except OSError as exc:
            raise ValueError("cannot create or inspect trusted flow directory") from exc
        return child

    def native_path(self, kind: str) -> str:
        if kind not in ("td", "md"):
            raise ValueError("invalid CTP flow kind")
        self.verify_root()
        account = self._child(self.root, self.scope_hash, self._root_chain[-1])
        account_identity = _directory_identity(account, private=True)
        if any(prior[0] != account_identity for prior in self._child_chains.values()):
            raise ValueError("trusted flow account directory identity changed")
        child = self._child(account, kind, account_identity)
        chain = (account_identity, _directory_identity(child, private=True))
        previous = self._child_chains.setdefault(kind, chain)
        if previous != chain:
            raise ValueError("trusted flow directory identity changed")
        self.verify_root()
        if chain != (
            _directory_identity(account, private=True),
            _directory_identity(child, private=True),
        ):
            raise ValueError("trusted flow directory identity changed")
        return str(child) + os.sep


def trusted_flow(root, account_scope):
    if (root is None) != (account_scope is None):
        raise ValueError("trusted_flow_root and flow_account_scope must be supplied together")
    return None if root is None else TrustedFlow(root, account_scope)


def flow_binding(flow):
    return None if flow is None else flow.binding


def flow_options(flow):
    if flow is None:
        return {}
    return {"trusted_flow_root": str(flow.root), "flow_account_scope": flow.account_scope}
