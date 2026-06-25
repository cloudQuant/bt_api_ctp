"""Shared SWIG infrastructure for split CTP wrapper modules."""

from __future__ import annotations

import importlib.machinery
import platform
import sys
import weakref
from pathlib import Path
from sys import float_info, stderr
from traceback import print_exception
from types import ModuleType


# Import the low-level C/C++ module.
class _FallbackSwigHandle:
    def __init__(self) -> None:
        self._owned = True

    def own(self, value=None):
        if value is None:
            return self._owned
        self._owned = bool(value)
        return self._owned

    def disown(self) -> None:
        self._owned = False

    def __repr__(self) -> str:
        return f"<fallback-ctp-handle owned={self._owned}>"


class _FallbackApiObject:
    def __init__(self, api_name: str) -> None:
        self._api_name = api_name

    def __getattr__(self, name: str):
        if name == "GetApiVersion":
            return lambda *args, **kwargs: "fallback-ctp"
        return lambda *args, **kwargs: 0

    def __repr__(self) -> str:
        return f"<fallback-ctp-api {self._api_name}>"


class _FallbackCtpModule(ModuleType):
    def __init__(self, import_error: Exception) -> None:
        super().__init__("_ctp_fallback")
        self._import_error = import_error
        self._constants: dict[str, object] = {}

    def __getattr__(self, name: str):
        if name.startswith("THOST_"):
            return self._constants.setdefault(name, name)

        if name.startswith("new_"):
            ctor_name = name[4:]
            if ctor_name.endswith("Api"):
                return lambda *args, **kwargs: _FallbackApiObject(ctor_name)
            return lambda *args, **kwargs: _FallbackSwigHandle()

        if name.startswith("delete_"):
            return lambda *args, **kwargs: None

        if name.startswith("disown_"):

            def _disown(instance, *args, **kwargs):
                handle = getattr(instance, "this", None)
                if handle is not None and hasattr(handle, "disown"):
                    handle.disown()
                return None

            return _disown

        if name.endswith("_swiginit"):

            def _swiginit(instance, handle):
                object.__setattr__(instance, "this", handle)
                return None

            return _swiginit

        if name.endswith("_swigregister"):
            return lambda *args, **kwargs: None

        if name.endswith("_CreateFtdcMdApi") or name.endswith("_CreateFtdcTraderApi"):
            api_name = name.split("_", 1)[0]
            return lambda *args, **kwargs: _FallbackApiObject(api_name)

        if name.endswith("_GetApiVersion"):
            return lambda *args, **kwargs: "fallback-ctp"

        if name.endswith("_get"):
            prop_name = name.rsplit("_", 2)[1]

            def _getter(instance):
                values = instance.__dict__.get("_fallback_values", {})
                return values.get(prop_name)

            return _getter

        if name.endswith("_set"):
            prop_name = name.rsplit("_", 2)[1]

            def _setter(instance, value):
                values = instance.__dict__.setdefault("_fallback_values", {})
                values[prop_name] = value
                return None

            return _setter

        return lambda *args, **kwargs: None


def _ctp_package_dir() -> Path:
    return Path(__file__).resolve().parent


def _expected_ctp_extension_names() -> list[str]:
    return [f'_ctp{suffix}' for suffix in importlib.machinery.EXTENSION_SUFFIXES]


def _available_ctp_extension_names() -> list[str]:
    package_dir = _ctp_package_dir()
    return sorted(
        p.name for p in package_dir.glob('_ctp*') if p.is_file() and p.name != '_ctp_base.py'
    )


def _format_ctp_import_warning(import_error: Exception) -> str:
    expected = _expected_ctp_extension_names()
    available = _available_ctp_extension_names()
    matching = [name for name in available if name in expected]
    if matching:
        reason = f"matching extension failed to load. matching={', '.join(matching)}"
    else:
        reason = 'no bundled extension matches this interpreter'
    return (
        'CTP C++ extension (_ctp) failed to load; '
        f"{reason}; expected={', '.join(expected)}; "
        f"available={', '.join(available) or 'none'}; "
        f'import_error={import_error}. '
        'All CTP operations will silently no-op.'
    )


try:
    if getattr(globals().get("__spec__"), "parent", None) or __package__ or "." in __name__:
        from . import _ctp
    else:
        import _ctp
except Exception as _ctp_import_error:
    import warnings as _warnings

    _warnings.warn(
        _format_ctp_import_warning(_ctp_import_error),
        RuntimeWarning,
        stacklevel=1,
    )
    _ctp = _FallbackCtpModule(_ctp_import_error)


def is_ctp_native_loaded() -> bool:
    """Return True if the real SWIG C++ module loaded, False if using fallback."""
    return not isinstance(_ctp, _FallbackCtpModule)


def get_ctp_import_error():
    """Return the import error if using fallback module, else None."""
    if isinstance(_ctp, _FallbackCtpModule):
        return _ctp._import_error
    return None


def get_ctp_native_diagnostics() -> dict[str, object]:
    """Return actionable diagnostics for the vendored CTP native extension."""
    package_dir = _ctp_package_dir()
    suffixes = list(importlib.machinery.EXTENSION_SUFFIXES)
    expected = _expected_ctp_extension_names()
    available = _available_ctp_extension_names()
    matching = [name for name in available if name in expected]
    native_loaded = is_ctp_native_loaded()
    import_error = get_ctp_import_error()

    if native_loaded:
        reason = 'native_loaded'
    elif not matching:
        reason = 'missing_extension_for_platform'
    else:
        reason = 'matching_extension_failed_to_load'

    return {
        'native_loaded': native_loaded,
        'reason': reason,
        'package_dir': str(package_dir),
        'python_version': '.'.join(str(part) for part in sys.version_info[:3]),
        'platform': platform.platform(),
        'machine': platform.machine(),
        'extension_suffixes': suffixes,
        'expected_extensions': expected,
        'available_extensions': available,
        'matching_extensions': matching,
        'import_error': str(import_error) if import_error else '',
    }


def format_ctp_native_diagnostics() -> str:
    """Format CTP native diagnostics for health reports and startup errors."""
    diag = get_ctp_native_diagnostics()
    if diag['native_loaded']:
        return 'CTP C++ extension (_ctp) is loaded'

    expected = ', '.join(str(item) for item in diag['expected_extensions'])
    available = ', '.join(str(item) for item in diag['available_extensions']) or 'none'
    matching = ', '.join(str(item) for item in diag['matching_extensions']) or 'none'
    base = (
        'CTP C++ extension (_ctp) not available for '
        f"{diag['platform']} / Python {diag['python_version']}; "
        f"package_dir={diag['package_dir']}"
    )
    if diag['reason'] == 'missing_extension_for_platform':
        detail = f'no bundled extension matches this interpreter. expected={expected}; available={available}'
    else:
        detail = f'matching extension failed to load. matching={matching}; available={available}'
    if diag['import_error']:
        detail = f"{detail}; import_error={diag['import_error']}"
    return f'{base}; {detail}'


def _swig_setattr_nondynamic_instance_variable(setter):
    def set_instance_attr(self, name, value):
        if name == "this":
            setter(self, name, value)
        elif name == "thisown":
            self.this.own(value)
        elif hasattr(self, name) and isinstance(getattr(type(self), name), property):
            setter(self, name, value)
        else:
            raise AttributeError(f"You cannot add instance attributes to {self}")

    return set_instance_attr


def _swig_setattr_nondynamic_class_variable(setter):
    def set_class_attr(cls, name, value):
        if hasattr(cls, name) and not isinstance(getattr(cls, name), property):
            setter(cls, name, value)
        else:
            raise AttributeError(f"You cannot add class attributes to {cls}")

    return set_class_attr


def _swig_add_metaclass(metaclass):
    """Slimmed-down version of six.add_metaclass for SWIG wrapper classes."""

    def wrapper(cls):
        return metaclass(cls.__name__, cls.__bases__, cls.__dict__.copy())

    return wrapper


class _SwigNonDynamicMeta(type):
    """Meta class to enforce nondynamic attributes on wrapped classes."""

    __setattr__ = _swig_setattr_nondynamic_class_variable(type.__setattr__)


def _swig_repr(self):
    values = []
    for key in vars(self.__class__):
        if key.startswith("_"):
            continue
        value = getattr(self, key)
        if isinstance(value, float):
            if value == float_info.max:
                values.append(f"{key}: None")
            else:
                values.append(f"{key}: {value:.2f}")
        elif isinstance(value, int):
            values.append(f"{key}: {value}")
        else:
            values.append(f'{key}: "{value}"')

    return f"<{self.__class__.__module__}.{self.__class__.__name__}; {', '.join(values)}>"


__all__ = [
    "_ctp",
    "_swig_repr",
    "_swig_setattr_nondynamic_instance_variable",
    "_swig_setattr_nondynamic_class_variable",
    "_swig_add_metaclass",
    "_SwigNonDynamicMeta",
    "is_ctp_native_loaded",
    "get_ctp_import_error",
    "print_exception",
    "stderr",
    "weakref",
]
