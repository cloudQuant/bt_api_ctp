# Keep annotations resolvable by typing.get_type_hints on Python 3.9.
# ruff: noqa: UP007, UP045
"""Experimental bounded local reader for unverified calendar supplements.

The caller must provide an absolute path resolved relative to its config file.
Walk each pathname component using directory descriptors and O_NOFOLLOW, then
read only a regular file and compare descriptor metadata and pathname identities
before and after the read. Platforms missing these primitives are rejected. This
reduces observed races; it cannot prove an editor finished: an in-place edit may
pause at a valid intermediate state or change and revert between observations.
Production still requires cooperative atomic replacement/locking and subsequent
source, completeness and conflict review before any publication. This module
never publishes, schedules, or accesses accounts. Local filesystem calls can
still block in the operating system; only bytes read are bounded. Windows and
network/shared filesystem behavior are NOT_RUN.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Optional, Union

from bt_api_ctp.calendar_supplement import (
    MAX_INPUT_BYTES,
    SupplementCandidate,
    SupplementError,
    parse_supplement,
)


@dataclass(frozen=True)
class SupplementReadResult:
    """Finite outcome without paths or exception messages in failure results."""

    status: str
    candidate: Optional[SupplementCandidate] = None
    verification: str = "UNVERIFIED"


class _ReadFailure(Exception):
    def __init__(self, status: str):
        self.status = status


def _metadata(value: os.stat_result) -> tuple:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _identity(value: os.stat_result) -> tuple:
    return (value.st_dev, value.st_ino, value.st_mode)


def _supported() -> bool:
    return (
        os.name == "posix"
        and all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
    )


def read_supplement(path: Union[os.PathLike, str]) -> SupplementReadResult:
    """Read a candidate, never claiming a stable or verified publication input."""
    if not _supported():
        return SupplementReadResult("UNSUPPORTED")
    try:
        raw_path = os.fspath(path)
        if (
            type(raw_path) is not str
            or not raw_path
            or "\x00" in raw_path
            or ".." in raw_path.split(os.sep)
            or not os.path.isabs(raw_path)
        ):
            return SupplementReadResult("INVALID_PATH")
        absolute = raw_path
        components = [part for part in absolute.split(os.sep) if part]
        if not components or len(components) > 128 or len(absolute) > 4096:
            return SupplementReadResult("INVALID_PATH")
    except (TypeError, ValueError, OSError):
        return SupplementReadResult("INVALID_PATH")

    descriptors = []
    chain = []
    try:
        root_fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(root_fd)
        parent_fd = root_fd
        for index, name in enumerate(components):
            last = index == len(components) - 1
            try:
                before_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise _ReadFailure("MISSING") from exc
            if stat.S_ISLNK(before_path.st_mode):
                raise _ReadFailure("SYMLINK")
            if last and not stat.S_ISREG(before_path.st_mode):
                raise _ReadFailure("NOT_REGULAR")
            if not last and not stat.S_ISDIR(before_path.st_mode):
                raise _ReadFailure("NOT_REGULAR")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if not last:
                flags |= os.O_DIRECTORY
            try:
                current_fd = os.open(name, flags, dir_fd=parent_fd)
            except FileNotFoundError as exc:
                raise _ReadFailure("CHANGED") from exc
            except OSError as exc:
                # The entry can be replaced by a symlink after the no-follow stat.
                try:
                    now = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except OSError:
                    raise _ReadFailure("CHANGED") from exc
                if _metadata(now) != _metadata(before_path):
                    raise _ReadFailure("CHANGED") from exc
                raise _ReadFailure("READ_ERROR") from exc
            descriptors.append(current_fd)
            before_fd = os.fstat(current_fd)
            if _metadata(before_path) != _metadata(before_fd):
                raise _ReadFailure("CHANGED")
            chain.append((parent_fd, name, current_fd, _identity(before_fd)))
            parent_fd = current_fd

        if before_fd.st_size > MAX_INPUT_BYTES:
            raise _ReadFailure("TOO_LARGE")
        data = bytearray()
        while len(data) <= MAX_INPUT_BYTES:
            chunk = os.read(current_fd, min(65536, MAX_INPUT_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_INPUT_BYTES:
            raise _ReadFailure("TOO_LARGE")
        if (
            _metadata(os.fstat(current_fd)) != _metadata(before_fd)
            or len(data) != before_fd.st_size
        ):
            raise _ReadFailure("CHANGED")
        for directory_fd, name, opened_fd, initial_identity in chain:
            try:
                path_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                current_stat = os.fstat(opened_fd)
            except OSError as exc:
                raise _ReadFailure("CHANGED") from exc
            if (
                _identity(path_stat) != initial_identity
                or _identity(current_stat) != initial_identity
            ):
                raise _ReadFailure("CHANGED")
        # Recheck complete file metadata after the pathname walk as well.
        if _metadata(os.fstat(current_fd)) != _metadata(before_fd):
            raise _ReadFailure("CHANGED")
        try:
            bytes(data).decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise _ReadFailure("INVALID_UTF8") from exc
        try:
            candidate = parse_supplement(bytes(data))
        except SupplementError as exc:
            if str(exc) == "optional PyYAML dependency unavailable":
                raise _ReadFailure("DEPENDENCY_UNAVAILABLE") from exc
            raise _ReadFailure("INVALID_CONTENT") from exc
        except Exception as exc:
            # Never expose parser exception messages, which may include YAML.
            raise _ReadFailure("PARSER_ERROR") from exc
        return SupplementReadResult("CANDIDATE", candidate)
    except _ReadFailure as exc:
        return SupplementReadResult(exc.status)
    except OSError:
        return SupplementReadResult("READ_ERROR")
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
