"""
高层封装 / High-level CTP Client Wrappers

提供简洁的 API，减少样板代码。3 行即可收行情或完成交易登录。

用法 / Usage:

    # 行情客户端
    from bt_api_py.ctp.client import MdClient

    def on_tick(data):
        print(data.InstrumentID, data.LastPrice)

    client = MdClient("tcp://182.254.243.31:30011", "9999", "user", "pass")
    client.on_tick = on_tick
    client.subscribe(["IF2603", "IC2603"])
    client.start()  # 阻塞

    # 交易客户端
    from bt_api_py.ctp.client import TraderClient

    client = TraderClient("tcp://182.254.243.31:30001", "9999", "user", "pass",
                          app_id="simnow_client_test", auth_code="0000000000000000")
    client.start()
    client.wait_ready(timeout=15)
    print(client.query_account())
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import json
import math
import os
import queue
import re
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from bt_api_ctp.instrument import normalize_ctp_instrument
from bt_api_ctp.query import (
    QueryResult,
    _attach_query_source,
    _new_query_session_scope,
    _new_query_source,
    _query_records_digest,
    _QuerySessionScope,
)

from . import _ctp_base
from ._ctp_base import (
    CtpNativeAbiError,
)
from ._ctp_base import (
    get_ctp_native_diagnostics as _get_vendored_ctp_native_diagnostics,
)
from ._ctp_base import (
    is_ctp_native_loaded as _is_vendored_ctp_native_loaded,
)
from .ctp_md_api import CThostFtdcMdApi, CThostFtdcMdSpi
from .ctp_structs_common import (
    CThostFtdcReqAuthenticateField,
    CThostFtdcReqUserLoginField,
    CThostFtdcSettlementInfoConfirmField,
)
from .ctp_structs_query import (
    CThostFtdcQryDepthMarketDataField,
    CThostFtdcQryInstrumentCommissionRateField,
    CThostFtdcQryInstrumentField,
    CThostFtdcQryInstrumentMarginRateField,
    CThostFtdcQryInvestorPositionField,
    CThostFtdcQryOptionInstrCommRateField,
    CThostFtdcQryOptionInstrTradeCostField,
    CThostFtdcQryOrderField,
    CThostFtdcQrySettlementInfoConfirmField,
    CThostFtdcQryTradeField,
    CThostFtdcQryTradingAccountField,
)
from .ctp_trader_api import CThostFtdcTraderApi, CThostFtdcTraderSpi

CTP_REQUEST_COUNT_KEYS = (
    "authenticate",
    "login",
    "settlement_confirm",
    "order_insert",
    "order_action",
    "query_account",
    "query_positions",
    "query_orders",
    "query_trades",
    "query_instruments",
    "query_margin_rate",
    "query_commission_rate",
    "query_depth_market_data",
    "query_option_trade_cost",
    "query_option_commission_rate",
    "query_settlement_confirmation",
)


# The CTP vendor API owns a native callback thread after ``Init()``.  On the
# macOS framework, calling ``Release()`` while a separate Python thread is
# blocked in ``Join()`` is unsafe.  A live native API still owns only a raw
# pointer to its SWIG director, however, so dropping ``_spi`` first turns the
# next callback into a use-after-free crash.  Keep a detached session alive
# after unregistering its callback from the native API.  The Join observer
# releases it once the vendor reports that its native thread has stopped.
#
# This registry is deliberately module-scoped rather than stored on a client:
# callers commonly discard a stopped feed/client before interpreter shutdown,
# while the vendor Join thread can still be alive.  There is no documented
# asynchronous CTP shutdown primitive that can safely replace this deferred
# lifetime on the audited macOS framework.  Entries must nevertheless be
# removed once Join returns so reconnects cannot retain completed sessions.
_RETIRED_CTP_NATIVE_SESSIONS_LOCK = threading.Lock()
_RETIRED_CTP_NATIVE_SESSIONS: list[tuple[Any, Any, threading.Thread | None]] = []


def _retain_live_ctp_native_session(
    api: Any, spi: Any, join_thread: threading.Thread | None
) -> None:
    """Retain a live CTP API and its SWIG director after logical disconnect."""

    if api is None:
        return
    with _RETIRED_CTP_NATIVE_SESSIONS_LOCK:
        if any(
            existing_api is api for existing_api, _, _ in _RETIRED_CTP_NATIVE_SESSIONS
        ):
            return
        _RETIRED_CTP_NATIVE_SESSIONS.append((api, spi, join_thread))


def _set_retired_ctp_native_session_join_thread(
    api: Any, join_thread: threading.Thread
) -> bool:
    """Associate a late-created Join observer with a retained native session."""

    with _RETIRED_CTP_NATIVE_SESSIONS_LOCK:
        for index, (existing_api, spi, _existing_thread) in enumerate(
            _RETIRED_CTP_NATIVE_SESSIONS
        ):
            if existing_api is api:
                _RETIRED_CTP_NATIVE_SESSIONS[index] = (api, spi, join_thread)
                return True
    return False


def _release_retired_ctp_native_session_after_join(api: Any) -> bool:
    """Release one retained session only after its native ``Join`` returned.

    The entry is removed while holding the registry lock so concurrent Join
    observers cannot issue a second ``Release``.  Its local tuple keeps the
    API and SPI strongly referenced until the release call has completed.
    """

    retained: tuple[Any, Any, threading.Thread | None] | None = None
    with _RETIRED_CTP_NATIVE_SESSIONS_LOCK:
        for index, entry in enumerate(_RETIRED_CTP_NATIVE_SESSIONS):
            if entry[0] is api:
                retained = _RETIRED_CTP_NATIVE_SESSIONS.pop(index)
                break
    if retained is None:
        return False

    # Join has returned, so the vendor callback thread is no longer live and
    # Release is safe on the audited macOS framework.  Do not call
    # RegisterSpi(None) again: stop() already made the documented detach
    # attempt before the session entered this registry.
    with suppress(Exception):
        api.Release()
    return True


_CTP_EXECUTION_GATE_PROOF_FIELDS = (
    "account_fingerprint",
    "trading_day",
    "instrument",
    "connection_generation",
    "environment_profile",
    "preflight_sha256",
    "receipt_sha256",
    "native_sha256",
    "ctp_package_sha256",
    "source_hashes_sha256",
    "dependency_hashes_sha256",
)
_CTP_EXECUTION_GATE_BUNDLE_SCOPE_VERSION = "ctp-contract-bundle-v1"
_CTP_EXECUTION_GATE_BUNDLE_PROOF_FIELDS = (
    *_CTP_EXECUTION_GATE_PROOF_FIELDS,
    "scope_version",
    "authorized_instruments",
)
_CTP_EXECUTION_GATE_HASH_FIELDS = (
    "preflight_sha256",
    "receipt_sha256",
    "native_sha256",
    "ctp_package_sha256",
    "source_hashes_sha256",
    "dependency_hashes_sha256",
)
_CTP_EXCHANGES = {"CFFEX", "CZCE", "DCE", "GFEX", "INE", "SHFE"}
_CTP_EXCHANGE_ALIASES = {"ZCE": "CZCE"}
_CTP_INSTRUMENT_RE = re.compile(r"^[A-Z]{1,3}[0-9]{3,4}$")
_CTP_BUNDLE_INSTRUMENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*$")
_CTP_GATE_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
# The public API view is intentionally much narrower than the SWIG object.
# Raw lifecycle calls can change the effective native connection while leaving
# the Python-side immutable front binding unchanged.  Keep the tiny allowlist
# to pure observation methods; typed query methods are handled separately.
_CTP_PUBLIC_NATIVE_READ_CALLS = frozenset({"GetApiVersion", "GetTradingDay"})


class CtpExecutionGateError(RuntimeError):
    """Credential-free, deterministic rejection from the managed CTP write gate."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# These objects deliberately have no public construction or retrieval API.
# A bare ``object()`` (or a mapping reconstructed from session-state fields)
# must never become a CTP write credential.  The parent SDK receives an owner
# capability through its private feed handshake and uses it only to install
# the managed gate.  The two one-shot grants below carry the mutable proof
# state; the capability itself is intentionally insufficient to arm orders or
# confirm settlement.
_CTP_CORE_EXECUTION_AUTHORITY_SEAL = object()
_CTP_EXECUTION_AUTHORIZATION_SEAL = object()


class _CtpCoreExecutionAuthority:
    """Opaque owner capability accepted only by the managed CTP boundary."""

    __slots__ = ("_seal", "_test_only")

    def __init__(self, seal: object, *, test_only: bool = False) -> None:
        if seal is not _CTP_CORE_EXECUTION_AUTHORITY_SEAL:
            raise CtpExecutionGateError("ctp_execution_gate_capability_required")
        self._seal = seal
        self._test_only = test_only


def _issue_ctp_execution_authority_for_core() -> object:
    """Private core/test seam; it is not part of the public feed contract.

    Production callers have no public route to this function.  It exists so
    the owning SDK can install a process-local capability before it exposes a
    CTP feed and so offline contract tests can exercise the native boundary.
    """

    return _CtpCoreExecutionAuthority(_CTP_CORE_EXECUTION_AUTHORITY_SEAL)


def _issue_ctp_execution_authority_for_test() -> object:
    """Private offline-test seam for exercising managed native transitions.

    This is intentionally separate from the parent/core issuer.  Only this
    marker permits compatibility construction of a one-shot token from a
    fixture mapping; a production core capability never does.
    """

    return _CtpCoreExecutionAuthority(
        _CTP_CORE_EXECUTION_AUTHORITY_SEAL, test_only=True
    )


def _is_ctp_core_execution_authority(value: object) -> bool:
    return bool(
        type(value) is _CtpCoreExecutionAuthority
        and getattr(value, "_seal", None) is _CTP_CORE_EXECUTION_AUTHORITY_SEAL
    )


def _is_ctp_test_execution_authority(value: object) -> bool:
    return bool(
        _is_ctp_core_execution_authority(value)
        and getattr(value, "_test_only", False) is True
    )


class _CtpExecutionAuthorization:
    """Native one-shot order-arm authorization, owned by one TraderClient."""

    __slots__ = (
        "_seal",
        "_client_ref",
        "_capability",
        "_proof",
        "_environment_profile",
        "_strategy_identity_sha256",
        "_execution_cycle_id",
        "_preflight_epoch",
        "_used",
    )

    def __init__(
        self,
        *,
        client: object,
        capability: object,
        proof: Mapping[str, Any],
        environment_profile: str,
        strategy_identity_sha256: str,
        execution_cycle_id: str,
        preflight_epoch: int,
    ) -> None:
        self._seal = _CTP_EXECUTION_AUTHORIZATION_SEAL
        self._client_ref = weakref.ref(client)
        self._capability = capability
        self._proof = dict(proof)
        self._environment_profile = environment_profile
        self._strategy_identity_sha256 = strategy_identity_sha256
        self._execution_cycle_id = execution_cycle_id
        self._preflight_epoch = preflight_epoch
        self._used = False


class _CtpSettlementAuthorization:
    """Native one-shot settlement-confirm authorization with a fixed budget."""

    __slots__ = (
        "_seal",
        "_client_ref",
        "_capability",
        "_account_fingerprint",
        "_trading_day",
        "_connection_generation",
        "_environment_profile",
        "_scope",
        "_budget",
        "_used",
    )

    def __init__(
        self,
        *,
        client: object,
        capability: object,
        account_fingerprint: str,
        trading_day: str,
        connection_generation: int,
        environment_profile: str,
        scope: str,
        budget: int,
    ) -> None:
        self._seal = _CTP_EXECUTION_AUTHORIZATION_SEAL
        self._client_ref = weakref.ref(client)
        self._capability = capability
        self._account_fingerprint = account_fingerprint
        self._trading_day = trading_day
        self._connection_generation = connection_generation
        self._environment_profile = environment_profile
        self._scope = scope
        self._budget = budget
        self._used = False


class _ManagedTraderApiView:
    """Dynamically expose the current native API through the managed gate."""

    def __init__(self, client: Any) -> None:
        # Keep no native API reference here.  A caller may cache this view or a
        # Req* callable before the SDK configures the gate; every invocation
        # must still observe the client's current API and gate state.
        self.__client_ref = weakref.ref(client)

    def __getattr__(self, name: str) -> Any:
        client = self.__client_ref()
        if client is None:
            raise CtpExecutionGateError("ctp_execution_gate_client_unavailable")
        if name.startswith("Req"):

            def invoke(*args: Any, **kwargs: Any) -> Any:
                current = self.__client_ref()
                if current is None:
                    raise CtpExecutionGateError("ctp_execution_gate_client_unavailable")
                return current._invoke_public_api_request(name, args, kwargs)

            return invoke
        value = client._get_public_api_attribute(name)
        if callable(value):

            def invoke(*args: Any, **kwargs: Any) -> Any:
                current = self.__client_ref()
                if current is None:
                    raise CtpExecutionGateError("ctp_execution_gate_client_unavailable")
                return current._invoke_public_api_callable(name, args, kwargs)

            return invoke
        return value


def _canonical_ctp_exchange(value: Any) -> str:
    exchange = str(value or "").strip().upper()
    exchange = _CTP_EXCHANGE_ALIASES.get(exchange, exchange)
    return exchange if exchange in _CTP_EXCHANGES else ""


def canonical_ctp_instrument(value: Any, exchange_id: Any = None) -> str:
    """Return the execution-gate contract identity as ``EXCHANGE.INSTRUMENT``."""

    text = str(value or "").strip().upper()
    supplied_exchange = _canonical_ctp_exchange(exchange_id)
    if not text or (exchange_id not in (None, "") and not supplied_exchange):
        return ""
    parts = text.split(".")
    if len(parts) == 1:
        exchange = supplied_exchange
        instrument = parts[0]
    elif len(parts) == 2:
        first_exchange = _canonical_ctp_exchange(parts[0])
        last_exchange = _canonical_ctp_exchange(parts[1])
        if bool(first_exchange) == bool(last_exchange):
            return ""
        exchange = first_exchange or last_exchange
        instrument = parts[1] if first_exchange else parts[0]
        if supplied_exchange and supplied_exchange != exchange:
            return ""
    else:
        return ""
    if not exchange or not _CTP_INSTRUMENT_RE.fullmatch(instrument):
        return ""
    letters = instrument.rstrip("0123456789")
    digits = instrument[len(letters) :]
    if exchange == "CZCE" and len(digits) == 4:
        digits = digits[-3:]
    return f"{exchange}.{letters}{digits}"


def canonical_ctp_bundle_instrument(value: Any, exchange_id: Any = None) -> str:
    """Return one exact raw native CTP contract identity for a V2 bundle.

    DCE option identifiers can contain hyphens and lowercase product codes.
    V2 deliberately preserves that native spelling and does not apply V1's
    legacy CZCE alias normalization before a submit/cancel reaches CTP.
    """
    text = str(value or "").strip()
    supplied_exchange = _canonical_ctp_exchange(exchange_id)
    if not text or (exchange_id not in (None, "") and not supplied_exchange):
        return ""
    parts = text.split(".")
    if len(parts) == 1:
        exchange = supplied_exchange
        instrument = parts[0]
    elif len(parts) == 2:
        first_exchange = _canonical_ctp_exchange(parts[0])
        last_exchange = _canonical_ctp_exchange(parts[1])
        if bool(first_exchange) == bool(last_exchange):
            return ""
        exchange = first_exchange or last_exchange
        instrument = parts[1] if first_exchange else parts[0]
        if supplied_exchange and supplied_exchange != exchange:
            return ""
    else:
        return ""
    if (
        not exchange
        or len(instrument) > 80
        or not _CTP_BUNDLE_INSTRUMENT_RE.fullmatch(instrument)
        or not any(character.isdigit() for character in instrument)
    ):
        return ""
    return f"{exchange}.{instrument}"


def _canonical_ctp_bundle_wire_instrument(value: Any, exchange_id: Any = None) -> str:
    """Return a V2 identity only when native CTP fields can stay verbatim.

    The V2 proof and recovery readers may canonicalize qualified forms such as
    ``DCE.m2701-C-3400``.  At the final native submit/cancel boundary the
    request fields are serialized unchanged, so only an exact bare
    ``InstrumentID`` paired with the canonical ``ExchangeID`` is safe.
    """
    if (
        not isinstance(value, str)
        or value != value.strip()
        or "." in value
        or not isinstance(exchange_id, str)
        or exchange_id != exchange_id.strip()
    ):
        return ""
    canonical = canonical_ctp_bundle_instrument(value, exchange_id)
    if not canonical:
        return ""
    exchange, instrument = canonical.split(".", 1)
    return canonical if instrument == value and exchange == exchange_id else ""


def _is_execution_gate_bundle(proof: Any) -> bool:
    return bool(
        isinstance(proof, Mapping)
        and proof.get("scope_version") == _CTP_EXECUTION_GATE_BUNDLE_SCOPE_VERSION
        and isinstance(proof.get("authorized_instruments"), (list, tuple))
    )


def _execution_gate_instruments(proof: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(proof, Mapping):
        return ()
    if _is_execution_gate_bundle(proof):
        return tuple(proof["authorized_instruments"])
    instrument = proof.get("instrument")
    return (instrument,) if isinstance(instrument, str) else ()


def _canonical_execution_gate_instrument(
    proof: Mapping[str, Any] | None,
    value: Any,
    exchange_id: Any = None,
    *,
    native_wire: bool = False,
) -> str:
    if _is_execution_gate_bundle(proof):
        if native_wire:
            return _canonical_ctp_bundle_wire_instrument(value, exchange_id)
        return canonical_ctp_bundle_instrument(value, exchange_id)
    return canonical_ctp_instrument(value, exchange_id)


def _execution_gate_proof(value: Any) -> tuple[dict[str, Any], str]:
    fields = set(value) if isinstance(value, Mapping) else set()
    is_bundle = fields == set(_CTP_EXECUTION_GATE_BUNDLE_PROOF_FIELDS)
    if fields == set(_CTP_EXECUTION_GATE_PROOF_FIELDS):
        proof = {field: value[field] for field in _CTP_EXECUTION_GATE_PROOF_FIELDS}
    elif is_bundle:
        proof = {
            field: value[field] for field in _CTP_EXECUTION_GATE_BUNDLE_PROOF_FIELDS
        }
    else:
        raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    for field in (
        "account_fingerprint",
        "trading_day",
        "instrument",
        "environment_profile",
    ):
        item = proof[field]
        if not isinstance(item, str) or not item or item != item.strip():
            raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    account_fingerprint = proof["account_fingerprint"]
    account_digest = account_fingerprint.removeprefix("acct_")
    if (
        account_fingerprint != account_fingerprint.lower()
        or not account_fingerprint.startswith("acct_")
        or len(account_digest) != 16
        or any(char not in "0123456789abcdef" for char in account_digest)
    ):
        raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    canonical_instrument = (
        canonical_ctp_bundle_instrument(proof["instrument"])
        if is_bundle
        else canonical_ctp_instrument(proof["instrument"])
    )
    if not canonical_instrument or canonical_instrument != proof["instrument"]:
        raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    if is_bundle:
        authorized = proof["authorized_instruments"]
        if (
            proof.get("scope_version") != _CTP_EXECUTION_GATE_BUNDLE_SCOPE_VERSION
            or not isinstance(authorized, (list, tuple))
            or not 2 <= len(authorized) <= 3
            or any(not isinstance(item, str) for item in authorized)
        ):
            raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
        canonical_authorized = tuple(
            canonical_ctp_bundle_instrument(item) for item in authorized
        )
        if (
            any(not item for item in canonical_authorized)
            or tuple(authorized) != canonical_authorized
            or tuple(sorted(canonical_authorized)) != canonical_authorized
            or len(set(canonical_authorized)) != len(canonical_authorized)
            or len({item.partition(".")[0] for item in canonical_authorized}) != 1
            or proof["instrument"] not in canonical_authorized
        ):
            raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
        proof["authorized_instruments"] = list(canonical_authorized)
    generation = proof["connection_generation"]
    if type(generation) is not int or generation <= 0:
        raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    try:
        parsed_day = datetime.strptime(proof["trading_day"], "%Y%m%d")
    except ValueError:
        raise CtpExecutionGateError("ctp_execution_gate_invalid_proof") from None
    if parsed_day.strftime("%Y%m%d") != proof["trading_day"]:
        raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    for field in _CTP_EXECUTION_GATE_HASH_FIELDS:
        digest = proof[field]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or digest != digest.lower()
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise CtpExecutionGateError("ctp_execution_gate_invalid_proof")
    serialized = json.dumps(
        proof, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return proof, hashlib.sha256(serialized).hexdigest()


def _execution_gate_reason(value: Any) -> str:
    reason = str(value or "execution_arm_revoked").strip().lower()
    return reason if _CTP_GATE_REASON_RE.fullmatch(reason) else "execution_arm_revoked"


def empty_ctp_request_counts() -> dict[str, int]:
    """Return the closed request-count schema used by strict read-only gates."""

    return dict.fromkeys(CTP_REQUEST_COUNT_KEYS, 0)


def _select_ctp_runtime_source() -> str:
    """Use only bt_api_ctp's bundled native extension at runtime.

    Switching a stateful CTP session between unrelated Python bindings changes
    callback ownership and native ABI assumptions.  The package therefore owns
    its runtime rather than accepting external ``ctp`` or ``openctp_ctp``
    process overrides.
    """
    return "vendored_bt_api_py"


_CTP_RUNTIME_SOURCE = _select_ctp_runtime_source()


def _is_native_extension_path(path: Path) -> bool:
    path_text = str(path)
    return any(
        path_text.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES
    )


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _is_vendored_native_trader_api(api: Any) -> bool:
    if _CTP_RUNTIME_SOURCE != "vendored_bt_api_py":
        return False
    try:
        return isinstance(api, CThostFtdcTraderApi)
    except TypeError:
        return False


def _submit_trader_user_login(
    api: Any, field: CThostFtdcReqUserLoginField, request_id: int
) -> Any:
    """Submit Trader login through the shared public ABI guard."""

    if not _is_vendored_native_trader_api(api):
        return api.ReqUserLogin(field, request_id)
    return _ctp_base._submit_public_trader_user_login(api, field, request_id)


def _ctp_python_package_identity(
    package_root: Path | None = None,
) -> tuple[list[dict[str, str]], str]:
    """Hash every controlled Python source under the installed CTP package."""

    if package_root is None:
        import bt_api_ctp

        package_root = Path(bt_api_ctp.__file__).resolve().parent
    else:
        package_root = Path(package_root).resolve()
    paths = sorted(
        (
            path
            for path in package_root.rglob("*.py")
            if path.is_file()
            and "__pycache__" not in path.relative_to(package_root).parts
        ),
        key=lambda path: path.relative_to(package_root).as_posix(),
    )
    manifest = [
        {
            "path": path.relative_to(package_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in paths
    ]
    serialized = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return manifest, hashlib.sha256(serialized).hexdigest()


def _runtime_module_prefix() -> str:
    return "bt_api_ctp.ctp"


def _selected_runtime_modules() -> dict[str, str]:
    prefix = _runtime_module_prefix()
    paths: dict[str, str] = {}
    for module_name, module in tuple(sys.modules.items()):
        if module_name != prefix and not module_name.startswith(f"{prefix}."):
            continue
        origin = str(getattr(module, "__file__", "") or "")
        if origin:
            paths[module_name] = str(Path(origin).resolve())
    return dict(sorted(paths.items()))


def get_ctp_native_diagnostics() -> dict[str, Any]:
    """Report exact native binaries loaded for the selected CTP runtime."""
    if _CTP_RUNTIME_SOURCE == "vendored_bt_api_py":
        diagnostics: dict[str, Any] = dict(_get_vendored_ctp_native_diagnostics())
    else:
        diagnostics = {}

    package_manifest, package_sha256 = _ctp_python_package_identity()
    runtime_modules = _selected_runtime_modules()
    native_modules = {
        module_name: module_path
        for module_name, module_path in runtime_modules.items()
        if _is_native_extension_path(Path(module_path))
    }
    native_hashes = {
        module_path: _sha256_file(Path(module_path))
        for module_path in sorted(set(native_modules.values()))
    }
    native_hashes = {path: digest for path, digest in native_hashes.items() if digest}
    binding_classes = {
        name: {
            "module": cls.__module__,
            "module_path": runtime_modules.get(cls.__module__, ""),
        }
        for name, cls in (
            ("CThostFtdcMdApi", CThostFtdcMdApi),
            ("CThostFtdcMdSpi", CThostFtdcMdSpi),
            ("CThostFtdcTraderApi", CThostFtdcTraderApi),
            ("CThostFtdcTraderSpi", CThostFtdcTraderSpi),
        )
    }
    native_loaded = _is_vendored_ctp_native_loaded()
    loaded_paths = sorted(set(native_modules.values()))
    selected_path = loaded_paths[0] if loaded_paths else ""
    diagnostics_override = {
        "runtime_source": _CTP_RUNTIME_SOURCE,
        "native_loaded": native_loaded,
        "reason": (
            "native_loaded"
            if native_loaded
            else "selected_runtime_has_no_native_extension"
        ),
        "runtime_module_paths": runtime_modules,
        "native_module_paths": native_modules,
        "native_module_sha256": native_hashes,
        "binding_classes": binding_classes,
        "ctp_package_manifest": package_manifest,
        "ctp_package_sha256": package_sha256,
        "loaded_module_path": selected_path,
        "loaded_module_sha256": native_hashes.get(selected_path, ""),
    }
    diagnostics.update(diagnostics_override)
    return diagnostics


def is_ctp_native_loaded() -> bool:
    """Return whether the selected runtime has a verified loaded native binary."""
    return bool(get_ctp_native_diagnostics()["native_loaded"])


def _format_selected_native_diagnostics(diagnostics: dict[str, Any]) -> str:
    if diagnostics["native_loaded"]:
        return (
            f"CTP runtime {diagnostics['runtime_source']} loaded native module "
            f"{diagnostics['loaded_module_path']}"
        )
    detail = str(
        diagnostics.get("import_error") or diagnostics.get("reason") or "unknown"
    )
    return f"CTP runtime {diagnostics['runtime_source']} has no verified native extension: {detail}"


def _check_native_module():
    """Raise ImportError early if the selected CTP native runtime is unavailable."""
    diagnostics = get_ctp_native_diagnostics()
    if diagnostics["native_loaded"]:
        return
    raise ImportError(
        f"{_format_selected_native_diagnostics(diagnostics)}. "
        "Install a native extension matching this OS, Python ABI, and selected CTP runtime."
    )


def get_ctp_runtime_source() -> str:
    return _CTP_RUNTIME_SOURCE


def _flow_dir(prefix):
    """Create a temp directory for CTP flow files."""
    h = hashlib.md5(prefix.encode("utf-8"), usedforsecurity=False).hexdigest()
    path = os.path.join(tempfile.gettempdir(), "ctp_client", h) + os.sep
    os.makedirs(path, exist_ok=True)
    return path


def _snapshot_ctp_field(field):
    """Create a plain dict snapshot from a SWIG field.

    Order / trade callbacks arrive on CTP's background thread. Converting the
    field to a plain dict inside the callback avoids leaking thread-bound SWIG
    objects to other threads or test assertions.
    """
    if field is None:
        return {}

    result = {}
    for attr in dir(field):
        if attr.startswith("_") or attr in {"this", "thisown"}:
            continue
        try:
            value = getattr(field, attr)
        except Exception:
            continue
        if not callable(value):
            result[attr] = value
    return result


class _QueryRecordSnapshot(dict[str, Any]):
    """Detached CTP query row retaining legacy attribute-style reads."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _snapshot_query_record(record: Any) -> _QueryRecordSnapshot:
    if isinstance(record, dict):
        return _QueryRecordSnapshot(record)
    return _QueryRecordSnapshot(_snapshot_ctp_field(record))


def _rsp_error(rsp_info: Any) -> tuple[int | None, str]:
    if rsp_info is None:
        return None, ""
    try:
        error_id = int(getattr(rsp_info, "ErrorID", 0) or 0)
    except (TypeError, ValueError):
        error_id = -1
    return error_id, str(getattr(rsp_info, "ErrorMsg", "") or "")


@dataclass
class _QueryAccumulator:
    request_type: str
    request_id: int
    connection_generation: int
    account_fingerprint: str
    started_at_utc: datetime
    event: threading.Event = dataclass_field(default_factory=threading.Event)
    records: list[Any] = dataclass_field(default_factory=list)
    completed_at_utc: datetime | None = None
    is_last_seen: bool = False
    error_code: int | None = None
    error_message: str = ""
    timed_out: bool = False
    sealed: bool = False
    late_callback_count: int = 0
    unsupported: bool = False
    submit_code: int | None = None
    source_issuer: object | None = None
    trading_day: str = ""
    broker_id: str = ""
    investor_id: str = ""
    started_monotonic: float = 0.0
    completed_monotonic: float | None = None
    source_records_sha256: str | None = None

    def result(self) -> QueryResult[Any]:
        complete = (
            self.sealed
            and self.is_last_seen
            and not self.timed_out
            and not self.unsupported
            and self.error_code in (None, 0)
        )
        result = QueryResult(
            request_type=self.request_type,
            request_id=self.request_id,
            connection_generation=self.connection_generation,
            account_fingerprint=self.account_fingerprint,
            started_at_utc=self.started_at_utc,
            completed_at_utc=self.completed_at_utc,
            is_last_seen=self.is_last_seen,
            error_code=self.error_code,
            error_message=self.error_message,
            timed_out=self.timed_out,
            complete=complete,
            records=tuple(self.records),
            late_callback_count=self.late_callback_count,
            unsupported=self.unsupported,
            submit_code=self.submit_code,
        )
        if self.source_issuer is None:
            return result
        return _attach_query_source(
            result,
            _new_query_source(
                issuer=self.source_issuer,
                request_type=self.request_type,
                request_id=self.request_id,
                account_fingerprint=self.account_fingerprint,
                connection_generation=self.connection_generation,
                trading_day=self.trading_day,
                broker_id=self.broker_id,
                investor_id=self.investor_id,
                started_at_utc=self.started_at_utc,
                completed_at_utc=self.completed_at_utc,
                started_monotonic=self.started_monotonic,
                completed_monotonic=self.completed_monotonic,
                records_sha256=self.source_records_sha256,
            ),
        )


# ===========================================================================
#  MdClient - 行情客户端
# ===========================================================================


class _MdSpi(CThostFtdcMdSpi):
    def __init__(self, client, native_api=None):
        super().__init__()
        self._c = client
        self._native_api = native_api

    def _is_current_locked(self) -> bool:
        if self._native_api is None:
            # Offline unit tests can construct an unbound SPI directly.
            return True
        return self._c._spi is self and self._c._api is self._native_api

    def _is_current(self) -> bool:
        with self._c._state_lock:
            return self._is_current_locked()

    def OnFrontConnected(self):
        with self._c._state_lock:
            if not self._is_current_locked():
                return
            self._c._connection_generation += 1
            self._c._connected = True
            field = CThostFtdcReqUserLoginField()
            field.BrokerID = self._c.broker_id
            field.UserID = self._c.user_id
            field.Password = self._c.password
            api = self._c._api
        if api is not None:
            api.ReqUserLogin(field, 1)

    def OnFrontDisconnected(self, nReason):
        with self._c._state_lock:
            if not self._is_current_locked():
                return
            self._c._connected = False
            self._c._loggedin = False

    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        subscribe = None
        callback = None
        error_callback = None
        error_info = None
        with self._c._state_lock:
            if not self._is_current_locked():
                return
            if pRspInfo and pRspInfo.ErrorID == 0:
                self._c._loggedin = True
                if self._c._pending_instruments:
                    subscribe = (self._c._api, list(self._c._pending_instruments))
                callback = self._c.on_login
            else:
                error_callback = self._c.on_error
                error_info = pRspInfo
        if subscribe is not None and subscribe[0] is not None:
            subscribe[0].SubscribeMarketData(subscribe[1])
        if callback is not None:
            callback(pRspUserLogin)
        if error_callback is not None:
            error_callback(error_info)

    def OnRtnDepthMarketData(self, pDepthMarketData):
        with self._c._state_lock:
            if not self._is_current_locked():
                return
            callback = self._c.on_tick
        if callback is not None:
            callback(pDepthMarketData)

    def OnRspSubMarketData(self, pSpecificInstrument, pRspInfo, nRequestID, bIsLast):
        pass

    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        with self._c._state_lock:
            if not self._is_current_locked():
                return
            callback = self._c.on_error
        if callback is not None:
            callback(pRspInfo)


class MdClient:
    """行情客户端封装

    Args:
        front: 前置地址，如 "tcp://182.254.243.31:30011"
        broker_id: 经纪商代码
        user_id: 投资者代码
        password: 密码
    """

    def __init__(self, front, broker_id, user_id, password):
        self.front = front
        self.broker_id = broker_id
        self.user_id = user_id
        self.password = password

        self.on_tick = None  # callback(CThostFtdcDepthMarketDataField)
        self.on_login = None  # callback(CThostFtdcRspUserLoginField)
        self.on_error = None  # callback(CThostFtdcRspInfoField)

        self._connected = False
        self._loggedin = False
        self._pending_instruments = []
        self._connection_generation = 0
        self._api = None
        self._spi = None
        self._thread = None
        self._join_active = False
        self._native_init_started = False
        self._lifecycle_generation = 0
        self._starting_generation: int | None = None
        self._startup_cancel_event = threading.Event()
        self._state_lock = threading.RLock()

    def _reserve_start_generation(self) -> int:
        """Reserve one startup generation before creating the native API."""

        with self._state_lock:
            if self._api is not None or self._starting_generation is not None:
                raise RuntimeError("ctp_md_client_already_started")
            self._lifecycle_generation += 1
            generation = self._lifecycle_generation
            self._starting_generation = generation
            self._startup_cancel_event.clear()
            return generation

    def _clear_start_reservation(self, generation: int) -> None:
        with self._state_lock:
            if self._starting_generation == generation:
                self._starting_generation = None

    def _is_start_current_locked(self, api: Any, spi: Any, generation: int) -> bool:
        return (
            self._lifecycle_generation == generation
            and self._starting_generation == generation
            and self._api is api
            and self._spi is spi
            and not self._startup_cancel_event.is_set()
        )

    def _run_startup_call(
        self,
        api: Any,
        spi: Any,
        generation: int,
        callback: Callable[[], Any],
        *,
        starts_native_thread: bool = False,
    ) -> tuple[bool, bool]:
        """Run one native startup call with pre/post cancellation fences.

        Holding the lifecycle lock only for an individual native call lets a
        concurrent ``stop`` take effect between registration calls.  The
        pre/post checks ensure it cannot be followed by a later
        ``RegisterFront`` or ``Init`` from the cancelled generation.
        """

        with self._state_lock:
            if not self._is_start_current_locked(api, spi, generation):
                return False, False
            if starts_native_thread:
                # A re-entrant stop from Init() must treat the API as live
                # before the vendor is allowed to create its callback thread.
                self._native_init_started = True
                self._join_active = True
            # stop() sets its event before it waits for this lock.  Repeat the
            # precondition immediately before entering native code so a stop
            # that arrived during the preceding bookkeeping cancels this step.
            if not self._is_start_current_locked(api, spi, generation):
                if starts_native_thread:
                    self._native_init_started = False
                    self._join_active = False
                return False, False
            callback()
            return True, self._is_start_current_locked(api, spi, generation)

    def _abort_startup(
        self,
        api: Any,
        spi: Any,
        generation: int,
        *,
        native_init_may_be_live: bool = False,
    ) -> bool:
        """Clean up a failed startup only while this generation owns it."""

        release_now = False
        observe_join = False
        with self._state_lock:
            if not self._is_start_current_locked(api, spi, generation):
                return False
            native_live = native_init_may_be_live or self._native_init_started
            if native_live:
                _retain_live_ctp_native_session(api, spi, self._thread)
                observe_join = True
            else:
                release_now = True
            self._api = None
            self._spi = None
            self._thread = None
            self._join_active = False
            self._native_init_started = False
            self._starting_generation = None
            self._lifecycle_generation += 1

        if observe_join:
            with suppress(Exception):
                api.RegisterSpi(None)
            self._start_join_observer(api)
        elif release_now:
            with suppress(Exception):
                api.RegisterSpi(None)
            with suppress(Exception):
                api.Release()
        return True

    def _join_native_api(self, api: Any) -> None:
        join_returned = False
        try:
            api.Join()
            join_returned = True
        finally:
            if join_returned:
                with self._state_lock:
                    if self._api is api:
                        self._join_active = False
                        self._native_init_started = False
                    if self._thread is threading.current_thread():
                        self._thread = None
                _release_retired_ctp_native_session_after_join(api)

    def _start_join_observer(self, api: Any) -> bool:
        """Start one Join observer for either the current or retired session."""

        thread = threading.Thread(
            target=self._join_native_api, args=(api,), daemon=True
        )
        with self._state_lock:
            if self._api is api:
                self._thread = thread
                self._join_active = True
            elif not _set_retired_ctp_native_session_join_thread(api, thread):
                return False
        thread.start()
        return True

    def subscribe(self, instruments):
        """订阅合约列表（可在 start 前或后调用）"""
        with self._state_lock:
            self._pending_instruments = list(instruments)
            api = self._api if self._loggedin else None
            pending_instruments = list(self._pending_instruments)
        if api is not None:
            api.SubscribeMarketData(pending_instruments)

    def start(self, block=True):
        """启动连接

        Args:
            block: True=阻塞直到断开, False=后台线程运行
        """
        _check_native_module()
        generation = self._reserve_start_generation()
        flow = _flow_dir(f"md_{self.broker_id}_{self.user_id}")
        try:
            api = CThostFtdcMdApi.CreateFtdcMdApi(flow)
        except Exception:
            self._clear_start_reservation(generation)
            raise
        spi = _MdSpi(self, api)
        with self._state_lock:
            if (
                self._starting_generation != generation
                or self._lifecycle_generation != generation
                or self._startup_cancel_event.is_set()
            ):
                cancelled_before_registration = True
            else:
                cancelled_before_registration = False
                self._api = api
                self._spi = spi
                self._native_init_started = False
                self._join_active = False
        if cancelled_before_registration:
            with suppress(Exception):
                api.RegisterSpi(None)
            with suppress(Exception):
                api.Release()
            return

        init_invoked = False

        def init_native_api() -> None:
            nonlocal init_invoked
            init_invoked = True
            api.Init()

        try:
            invoked, active = self._run_startup_call(
                api, spi, generation, lambda: api.RegisterSpi(spi)
            )
            if not invoked or not active:
                self._abort_startup(api, spi, generation)
                return
            invoked, active = self._run_startup_call(
                api, spi, generation, lambda: api.RegisterFront(self.front)
            )
            if not invoked or not active:
                self._abort_startup(api, spi, generation)
                return
            invoked, active = self._run_startup_call(
                api,
                spi,
                generation,
                init_native_api,
                starts_native_thread=True,
            )
            if not invoked:
                self._abort_startup(api, spi, generation)
                return
            if not active:
                # stop() detached this API while Init was running.  It is
                # retained already; attach a Join observer so the registry is
                # released when the native thread exits.
                self._start_join_observer(api)
                return
        except Exception:
            # Init is a void vendor call, but if a binding raises after it was
            # entered, fail safe and retain until Join proves native shutdown.
            handled = self._abort_startup(
                api,
                spi,
                generation,
                native_init_may_be_live=init_invoked,
            )
            if init_invoked and not handled:
                # A concurrent stop may have set the cancellation fence while
                # Init raised.  Attach the observer whether it already
                # retained the session or is about to do so.
                self._start_join_observer(api)
            raise

        if block:
            with self._state_lock:
                current = self._is_start_current_locked(api, spi, generation)
            if not current:
                self._start_join_observer(api)
                return
            try:
                self._join_native_api(api)
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()
            return

        self._start_join_observer(api)

    def wait_ready(self, timeout=15):
        """等待登录就绪"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._state_lock:
                if self._loggedin:
                    return True
            time.sleep(0.2)
        with self._state_lock:
            return self._loggedin

    def stop(self):
        """Stop a CTP market-data session without freeing a live SWIG director.

        The vendor macOS framework is unsafe if ``Release()`` races a live
        ``Join()``.  Detach the native callback first, then retain the API,
        director and Join thread until Join returns.  Once it has returned,
        the Join observer releases the retained session immediately.
        """

        # Set this before waiting for a native registration call's lock.  It
        # is the post-call fence that prevents RegisterFront/Init from running
        # when stop races RegisterSpi on another thread.
        self._startup_cancel_event.set()
        with self._state_lock:
            # A stop issued while CreateFtdc* is still running must cancel the
            # reserved generation before start() can register it.
            self._lifecycle_generation += 1
            self._starting_generation = None
            self._loggedin = False
            self._connected = False
            api = self._api
            spi = self._spi
            join_thread = self._thread
            native_may_be_live = self._native_init_started or self._join_active
            join_active = native_may_be_live and (
                self._join_active
                or (join_thread is not None and join_thread.is_alive())
            )
            if api is None:
                return
            if join_active:
                _retain_live_ctp_native_session(api, spi, join_thread)
            self._api = None
            self._spi = None
            self._thread = None
            self._join_active = False
            self._native_init_started = False

        if join_active:
            # RegisterSpi(None) is the vendor's documented callback
            # registration API; retaining ``spi`` above also protects a
            # callback already in flight while the registration is changed.
            with suppress(Exception):
                api.RegisterSpi(None)
            return

        with suppress(Exception):
            api.RegisterSpi(None)
            api.Release()

    @property
    def is_ready(self):
        with self._state_lock:
            return self._connected and self._loggedin

    @property
    def connection_generation(self):
        with self._state_lock:
            return self._connection_generation


# ===========================================================================
#  TraderClient - 交易客户端
# ===========================================================================


def _fence_trader_spi_callback(callback):
    @wraps(callback)
    def guarded(self, *args, **kwargs):
        with self._c._query_state_lock:
            if not self._is_current_locked():
                return None
        # Callback bodies take the state lock only while mutating SDK state.
        # In particular, user callbacks must never run while this lock is held:
        # they may synchronously start a typed query that needs _query_lock.
        return callback(self, *args, **kwargs)

    return guarded


class _TraderSpi(CThostFtdcTraderSpi):
    def __init__(self, client, native_api=None):
        super().__init__()
        self._c = client
        self._native_api = native_api

    def _is_current_locked(self) -> bool:
        if self._native_api is None:
            # Offline unit tests construct an unbound SPI directly.
            return True
        return self._c._spi is self and self._c._api is self._native_api

    def _is_current(self) -> bool:
        with self._c._query_state_lock:
            return self._is_current_locked()

    @_fence_trader_spi_callback
    def OnFrontConnected(self):
        with self._c._query_state_lock:
            if not self._is_current_locked():
                return
            self._c._on_front_connected()
            field = CThostFtdcReqAuthenticateField()
            field.BrokerID = self._c._bound_broker_id
            field.UserID = self._c._bound_user_id
            field.AppID = self._c.app_id
            field.AuthCode = self._c.auth_code
            request_id = self._c._next_request_id()
            generation = self._c._connection_generation
            self._c._authentication_request_id = request_id
            self._c._authentication_connection_generation = generation
            self._c._record_request("authenticate")
            api = self._c._api
        try:
            ret = api.ReqAuthenticate(field, request_id)
        except Exception as exc:
            with self._c._query_state_lock:
                if (
                    self._c._authentication_request_id == request_id
                    and self._c._authentication_connection_generation == generation
                    and self._c._connection_generation == generation
                ):
                    self._c._authentication_state = "failed"
                    self._c._last_session_error = {
                        "error": "authentication_submit_failed",
                        "detail": type(exc).__name__,
                    }
            return
        if ret not in (None, 0):
            with self._c._query_state_lock:
                if (
                    self._c._authentication_request_id == request_id
                    and self._c._authentication_connection_generation == generation
                    and self._c._connection_generation == generation
                ):
                    self._c._authentication_state = "failed"
                    self._c._last_session_error = {
                        "error": "authentication_submit_rejected",
                        "submit_code": ret,
                    }

    @_fence_trader_spi_callback
    def OnFrontDisconnected(self, nReason):
        with self._c._query_state_lock:
            if not self._is_current_locked():
                return
            self._c._on_front_disconnected(nReason)

    @_fence_trader_spi_callback
    def OnRspAuthenticate(self, pRspAuthenticateField, pRspInfo, nRequestID, bIsLast):
        error_callback = None
        login_submission = None
        with self._c._query_state_lock:
            accepted = (
                self._is_current_locked()
                and self._c._authentication_state == "authenticating"
                and self._c._authentication_request_id == int(nRequestID)
                and self._c._authentication_connection_generation
                == self._c._connection_generation
            )
            if not accepted:
                self._c._authentication_late_callback_count += 1
                return
            self._c._authentication_request_id = None
            self._c._authentication_connection_generation = None
            error_id, _ = _rsp_error(pRspInfo)
            if error_id not in (None, 0):
                self._c._authentication_state = "failed"
                self._c._last_session_error = _snapshot_ctp_field(pRspInfo)
                error_callback = self._c.on_error
            else:
                self._c._authentication_state = "authenticated"
                field = CThostFtdcReqUserLoginField()
                field.BrokerID = self._c._bound_broker_id
                field.UserID = self._c._bound_user_id
                field.Password = self._c.password
                request_id = self._c._next_request_id()
                generation = self._c._connection_generation
                self._c._login_state = "logging_in"
                self._c._login_request_id = request_id
                self._c._login_connection_generation = generation
                self._c._record_request("login")
                login_submission = (self._c._api, field, request_id, generation)
        if error_callback is not None:
            error_callback(pRspInfo)
            return
        if login_submission is None:
            return
        api, field, request_id, generation = login_submission
        try:
            ret = _submit_trader_user_login(api, field, request_id)
        except Exception as exc:
            with self._c._query_state_lock:
                if (
                    self._c._login_request_id == request_id
                    and self._c._login_connection_generation == generation
                    and self._c._connection_generation == generation
                ):
                    self._c._login_state = "failed"
                    self._c._last_session_error = {
                        "error": "login_submit_failed",
                        "detail": (
                            exc.code
                            if isinstance(exc, CtpNativeAbiError)
                            else type(exc).__name__
                        ),
                    }
            return
        if ret not in (None, 0):
            with self._c._query_state_lock:
                if (
                    self._c._login_request_id == request_id
                    and self._c._login_connection_generation == generation
                    and self._c._connection_generation == generation
                ):
                    self._c._login_state = "failed"
                    self._c._last_session_error = {
                        "error": "login_submit_rejected",
                        "submit_code": ret,
                    }

    @_fence_trader_spi_callback
    def OnRspUserLogin(self, pRspUserLogin, pRspInfo, nRequestID, bIsLast):
        login_callback = None
        error_callback = None
        with self._c._query_state_lock:
            accepted = (
                self._is_current_locked()
                and self._c._login_state == "logging_in"
                and self._c._login_request_id == int(nRequestID)
                and self._c._login_connection_generation
                == self._c._connection_generation
            )
            if not accepted:
                self._c._login_late_callback_count += 1
                return
            generation = self._c._connection_generation
            self._c._login_request_id = None
            self._c._login_connection_generation = None
            error_id, _ = _rsp_error(pRspInfo)
            if error_id in (None, 0):
                self._c._login_state = "logged_in"
                self._c._front_id = pRspUserLogin.FrontID
                self._c._session_id = pRspUserLogin.SessionID
                self._c._trading_day = str(
                    getattr(pRspUserLogin, "TradingDay", "") or ""
                )
                with suppress(TypeError, ValueError):
                    self._c._max_order_ref = max(
                        self._c._max_order_ref,
                        int(getattr(pRspUserLogin, "MaxOrderRef", "") or 0),
                    )
                self._c._ready = False
                self._c._settlement_readback_verified = False
                self._c._settlement_proof_source = "none"
                self._c._settlement_proof_query_request_id = None
                # Settlement confirmation is a real terminal write.  Do not
                # synthesize an authorization from ``auto_settlement_confirm``
                # during login: an SDK-managed, explicit confirmation with its
                # opaque capability is required before this session can trade.
                # Keeping the logged-in session read-only remains sufficient
                # for account, position, instrument and quote discovery.
                self._c._settlement_state = "not_requested"
                login_callback = self._c.on_login
            else:
                self._c._login_state = "failed"
                self._c._last_session_error = _snapshot_ctp_field(pRspInfo)
                error_callback = self._c.on_error
        if login_callback is not None:
            login_callback(pRspUserLogin)
        if error_callback is not None:
            error_callback(pRspInfo)

    @_fence_trader_spi_callback
    def OnRspSettlementInfoConfirm(
        self, pSettlementInfoConfirm, pRspInfo, nRequestID, bIsLast
    ):
        error_callback = None
        with self._c._query_state_lock:
            if not self._is_current_locked():
                return
            if not self._c._accept_settlement_callback(
                nRequestID, pSettlementInfoConfirm
            ):
                return
            error_id, _ = _rsp_error(pRspInfo)
            if error_id in (None, 0):
                self._c._settlement_state = "confirmed"
                self._c._ready = False
                self._c._settlement_readback_verified = False
                self._c._settlement_proof_source = "direct_confirmation"
                self._c._settlement_proof_query_request_id = None
                self._c._revoke_execution_gate_locked(
                    "ctp_execution_gate_settlement_direct_confirmation_requires_readback"
                )
            else:
                self._c._settlement_state = "failed"
                self._c._ready = False
                self._c._settlement_readback_verified = False
                self._c._last_session_error = _snapshot_ctp_field(pRspInfo)
                error_callback = self._c.on_error
            self._c._settlement_done.set()
        if error_callback is not None:
            error_callback(pRspInfo)

    @_fence_trader_spi_callback
    def OnRspQryTradingAccount(self, pTradingAccount, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._handle_query_callback(
            "account", pTradingAccount, pRspInfo, nRequestID, bIsLast
        )

    @_fence_trader_spi_callback
    def OnRspQryInvestorPosition(self, pPos, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._handle_query_callback("positions", pPos, pRspInfo, nRequestID, bIsLast)

    @_fence_trader_spi_callback
    def OnRspQryOrder(self, pOrder, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._handle_query_callback("orders", pOrder, pRspInfo, nRequestID, bIsLast)

    @_fence_trader_spi_callback
    def OnRspQryTrade(self, pTrade, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._handle_query_callback("trades", pTrade, pRspInfo, nRequestID, bIsLast)

    @_fence_trader_spi_callback
    def OnRspQryInstrument(self, pInstrument, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._handle_query_callback(
            "instruments", pInstrument, pRspInfo, nRequestID, bIsLast
        )

    @_fence_trader_spi_callback
    def OnRspQryDepthMarketData(self, pDepthMarketData, pRspInfo, nRequestID, bIsLast):
        self._c._handle_query_callback(
            "depth_market_data", pDepthMarketData, pRspInfo, nRequestID, bIsLast
        )

    @_fence_trader_spi_callback
    def OnRspQryOptionInstrTradeCost(
        self, pOptionInstrTradeCost, pRspInfo, nRequestID, bIsLast
    ):
        self._c._handle_query_callback(
            "option_trade_cost", pOptionInstrTradeCost, pRspInfo, nRequestID, bIsLast
        )

    @_fence_trader_spi_callback
    def OnRspQryOptionInstrCommRate(
        self, pOptionInstrCommRate, pRspInfo, nRequestID, bIsLast
    ):
        self._c._handle_query_callback(
            "option_commission_rate",
            pOptionInstrCommRate,
            pRspInfo,
            nRequestID,
            bIsLast,
        )

    @_fence_trader_spi_callback
    def OnRspQryInstrumentMarginRate(
        self, pInstrumentMarginRate, pRspInfo, nRequestID, bIsLast
    ):
        if not self._is_current():
            return
        self._c._handle_query_callback(
            "margin_rate", pInstrumentMarginRate, pRspInfo, nRequestID, bIsLast
        )

    @_fence_trader_spi_callback
    def OnRspQryInstrumentCommissionRate(
        self, pInstrumentCommissionRate, pRspInfo, nRequestID, bIsLast
    ):
        if not self._is_current():
            return
        self._c._handle_query_callback(
            "commission_rate",
            pInstrumentCommissionRate,
            pRspInfo,
            nRequestID,
            bIsLast,
        )

    @_fence_trader_spi_callback
    def OnRspQrySettlementInfoConfirm(
        self, pSettlementInfoConfirm, pRspInfo, nRequestID, bIsLast
    ):
        if not self._is_current():
            return
        self._c._handle_query_callback(
            "settlement_confirmation",
            pSettlementInfoConfirm,
            pRspInfo,
            nRequestID,
            bIsLast,
        )

    @_fence_trader_spi_callback
    def OnRtnOrder(self, pOrder):
        if not self._is_current():
            return
        self._c._push_order_event(pOrder)

    @_fence_trader_spi_callback
    def OnRtnTrade(self, pTrade):
        if not self._is_current():
            return
        self._c._push_trade_event(pTrade)

    @_fence_trader_spi_callback
    def OnRspOrderInsert(self, pInputOrder, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._push_error_event(
            event_type="order_insert_response",
            rsp_info=pRspInfo,
            field=pInputOrder,
            request_id=nRequestID,
        )

    @_fence_trader_spi_callback
    def OnErrRtnOrderInsert(self, pInputOrder, pRspInfo):
        if not self._is_current():
            return
        self._c._push_error_event(
            event_type="order_insert_error",
            rsp_info=pRspInfo,
            field=pInputOrder,
        )

    @_fence_trader_spi_callback
    def OnRspError(self, pRspInfo, nRequestID, bIsLast):
        if not self._is_current():
            return
        self._c._handle_query_error(pRspInfo, nRequestID, bIsLast)
        self._c._push_error_event(
            event_type="response_error",
            rsp_info=pRspInfo,
            request_id=nRequestID,
        )


class TraderClient:
    """交易客户端封装

    Args:
        front: 交易前置地址
        broker_id: 经纪商代码
        user_id: 投资者代码
        password: 密码
        app_id: 客户端 AppID
        auth_code: 认证码
    """

    def __init__(
        self,
        front,
        broker_id,
        user_id,
        password,
        app_id="simnow_client_test",
        auth_code="0000000000000000",
        auto_settlement_confirm=False,
    ):
        # Keep the identity and TD front that created this client separate
        # from the historically public compatibility attributes below.  A
        # caller can mutate ``broker_id``/``user_id``/``front`` on a Python
        # object, but that must not retarget a session whose preflight proof
        # was bound to the original account and native front.
        self._bound_front = str(front or "").strip()
        self._bound_broker_id = str(broker_id or "").strip()
        self._bound_user_id = str(user_id or "").strip()
        self.front = front
        self.broker_id = broker_id
        self.user_id = user_id
        self.password = password
        self.app_id = app_id
        self.auth_code = auth_code
        self.auto_settlement_confirm = bool(auto_settlement_confirm)
        self._account_fingerprint = hashlib.sha256(
            f"{self._bound_broker_id}:{self._bound_user_id}".encode()
        ).hexdigest()[:16]

        self.on_login = None  # callback(CThostFtdcRspUserLoginField)
        self.on_order = None  # callback(CThostFtdcOrderField)
        self.on_trade = None  # callback(CThostFtdcTradeField)
        self.on_error = None  # callback(CThostFtdcRspInfoField)

        self._connected = False
        self._ready = False
        self._authentication_state = "disconnected"
        self._login_state = "disconnected"
        self._settlement_state = "unknown"
        self._trading_day = ""
        self._last_session_error: dict[str, Any] = {}
        self._connection_generation = 0
        self._req_id = 0
        self._front_id = 0
        self._session_id = 0
        self._api_view = _ManagedTraderApiView(self)
        self._api = None
        self._session_native_api = None
        self._session_native_front: str | None = None
        self._spi = None
        self._thread = None
        self._join_active = False
        self._native_init_started = False
        self._lifecycle_generation = 0
        self._starting_generation: int | None = None
        self._startup_cancel_event = threading.Event()
        self._settlement_done = threading.Event()
        self._settlement_request_id: int | None = None
        self._settlement_connection_generation: int | None = None
        self._settlement_account_fingerprint: str | None = None
        self._settlement_trading_day: str | None = None
        self._settlement_late_callback_count = 0
        self._settlement_proof_source = "none"
        self._settlement_proof_query_request_id: int | None = None
        self._settlement_readback_verified = False
        self._authentication_request_id: int | None = None
        self._authentication_connection_generation: int | None = None
        self._authentication_late_callback_count = 0
        self._login_request_id: int | None = None
        self._login_connection_generation: int | None = None
        self._login_late_callback_count = 0
        self._query_done = threading.Event()
        self._last_account = None
        self._last_positions = []
        self._last_orders = []
        self._last_instrument = None
        self._last_margin_rate = None
        self._last_commission_rate = None
        self._query_lock = threading.Lock()
        self._query_state_lock = threading.RLock()
        self._query_history: dict[int, _QueryAccumulator] = {}
        # Every typed query source and session scope from this client share an
        # opaque issuer.  Equal account strings from a different object or a
        # hand-built mapping can therefore never certify the same query.
        self._query_evidence_issuer = object()
        self._orphan_query_callbacks: list[dict[str, Any]] = []
        self._request_counts: dict[str, int] = empty_ctp_request_counts()
        self._execution_gate_capability: object | None = None
        self._execution_gate_proof: dict[str, Any] | None = None
        self._execution_gate_proof_sha256: str | None = None
        self._execution_gate_environment_profile: str | None = None
        self._execution_gate_strategy_identity_sha256: str | None = None
        self._execution_gate_cycle_id: str | None = None
        self._execution_gate_revocation_reason: str | None = None
        self._execution_gate_native_api = None
        # A settlement confirmation deliberately invalidates every arm proof
        # issued before it.  It is a terminal account write, so a subsequent
        # order arm must be based on a fresh preflight rather than a cached
        # public session snapshot.
        self._execution_preflight_epoch = 0
        self._last_query_submitted_at = 0.0
        try:
            self._query_interval = max(
                0.0, float(os.environ.get("BT_API_PY_CTP_QUERY_INTERVAL_SEC") or 1.05)
            )
        except ValueError:
            self._query_interval = 1.05
        self._max_order_ref = 0
        self._order_ref_lock = threading.Lock()
        self._order_events = queue.Queue()
        self._trade_events = queue.Queue()
        self._error_events = queue.Queue()

    @property
    def _api(self) -> Any:
        return getattr(self, "_TraderClient__native_api", None)

    @_api.setter
    def _api(self, value: Any) -> None:
        current = getattr(self, "_TraderClient__native_api", None)
        if current is value:
            return
        lock = getattr(self, "_query_state_lock", None)
        if lock is None:
            self.__native_api = value
            return
        with lock:
            if getattr(self, "_execution_gate_capability", None) is not None:
                self._revoke_execution_gate_locked(
                    "ctp_execution_gate_native_api_changed"
                )
            if hasattr(self, "_execution_preflight_epoch"):
                self._execution_preflight_epoch += 1
            self._session_native_api = None
            self._session_native_front = None
            # Any callbacks from the previous SPI become stale immediately.
            self._spi = None
            self.__native_api = value

    def _bound_identity_is_current(self, *, require_active_front: bool = False) -> bool:
        """Check that public compatibility attributes still name this session.

        This predicate deliberately has no side effects because readiness
        readers use it.  Mutating a public feed/client field may never switch
        the account or front used by an already authenticated managed session.
        """

        current_fingerprint = hashlib.sha256(
            f"{str(self.broker_id or '').strip()}:{str(self.user_id or '').strip()}".encode()
        ).hexdigest()[:16]
        if (
            str(self.front or "").strip() != self._bound_front
            or str(self.broker_id or "").strip() != self._bound_broker_id
            or str(self.user_id or "").strip() != self._bound_user_id
            or current_fingerprint != self._account_fingerprint
        ):
            return False
        return (
            not require_active_front or self._session_native_front == self._bound_front
        )

    def _require_bound_identity_locked(
        self, *, require_active_front: bool = False
    ) -> None:
        """Fail closed before a managed native write can use mutable identity."""

        if not self._bound_identity_is_current(require_active_front=False):
            self._revoke_execution_gate_locked(
                "ctp_execution_gate_account_identity_changed"
            )
            raise CtpExecutionGateError("ctp_execution_gate_account_identity_changed")
        if require_active_front and self._session_native_front != self._bound_front:
            self._revoke_execution_gate_locked(
                "ctp_execution_gate_front_profile_mismatch"
            )
            raise CtpExecutionGateError("ctp_execution_gate_front_profile_mismatch")

    def _require_native_field_identity_locked(
        self,
        field: Any,
        *,
        require_user_id: bool,
    ) -> None:
        """Bind each typed order field to the immutable authenticated account."""

        self._require_bound_identity_locked(require_active_front=True)
        broker_id = str(getattr(field, "BrokerID", "") or "")
        investor_id = str(getattr(field, "InvestorID", "") or "")
        user_id = str(getattr(field, "UserID", "") or "")
        if (
            broker_id != self._bound_broker_id
            or investor_id != self._bound_user_id
            or (require_user_id and user_id != self._bound_user_id)
            or (not require_user_id and user_id and user_id != self._bound_user_id)
        ):
            self._revoke_execution_gate_locked(
                "ctp_execution_gate_native_field_identity_mismatch"
            )
            raise CtpExecutionGateError(
                "ctp_execution_gate_native_field_identity_mismatch"
            )

    def _invoke_public_api_request(
        self,
        name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Resolve a cached public Req* callable at invocation time."""

        with self._query_state_lock:
            is_read_query = name.startswith(("ReqQry", "ReqQuery"))
            if not is_read_query:
                # Never expose native request writes through the public API
                # view.  A caller could otherwise cache ``ReqOrderInsert``
                # before the SDK installs its capability, then bypass the
                # typed final-gate check entirely.  Authentication/login
                # requests are internal lifecycle operations for the same
                # reason.
                raise CtpExecutionGateError("ctp_execution_gate_native_write_blocked")
            if self._execution_gate_capability is not None:
                # Managed callers must use the typed query methods so request
                # IDs, generation fencing and the one-query-at-a-time lock
                # cannot be bypassed through a cached native Req* handle.
                raise CtpExecutionGateError("ctp_execution_gate_raw_request_blocked")
            api = self._api
            if api is None:
                raise CtpExecutionGateError("ctp_execution_gate_native_api_unavailable")
            target = getattr(api, name)
        return target(*args, **kwargs)

    def _get_public_api_attribute(self, name: str) -> Any:
        """Resolve a non-request native attribute without storing it in the view."""

        with self._query_state_lock:
            api = self._api
            if api is None:
                raise AttributeError(name)
            return getattr(api, name)

    def _invoke_public_api_callable(
        self,
        name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Fence cached non-request native callables after gate installation."""

        if name not in _CTP_PUBLIC_NATIVE_READ_CALLS:
            # This guard applies before a managed capability is installed as
            # well.  Otherwise a caller could cache/use RegisterFront, Init
            # or Release on an unmanaged API, then promote that altered native
            # session into a managed execution session later.
            raise CtpExecutionGateError("ctp_execution_gate_native_lifecycle_blocked")
        with self._query_state_lock:
            api = self._api
            if api is None:
                raise CtpExecutionGateError("ctp_execution_gate_native_api_unavailable")
            target = getattr(api, name)
            if not callable(target):
                raise TypeError(f"native API attribute {name!r} is not callable")
        return target(*args, **kwargs)

    def _execution_gate_state_locked(self) -> dict[str, Any]:
        proof = self._execution_gate_proof
        return {
            "managed": self._execution_gate_capability is not None,
            "armed": proof is not None,
            "connection_generation": (
                proof.get("connection_generation") if proof is not None else None
            ),
            "trading_day": proof.get("trading_day") if proof is not None else None,
            "instrument": proof.get("instrument") if proof is not None else None,
            "scope_version": proof.get("scope_version") if proof is not None else None,
            "authorized_instruments": (
                list(proof["authorized_instruments"])
                if _is_execution_gate_bundle(proof)
                else None
            ),
            "environment_profile": self._execution_gate_environment_profile,
            "proof_sha256": self._execution_gate_proof_sha256,
            "revocation_reason": self._execution_gate_revocation_reason,
        }

    def get_execution_gate_state(self) -> dict[str, Any]:
        """Return the managed native-order gate without exposing its capability."""

        with self._query_state_lock:
            return self._execution_gate_state_locked()

    def configure_execution_gate(self, capability: object) -> dict[str, Any]:
        """Install the SDK-owned opaque capability and start disarmed."""

        if not _is_ctp_core_execution_authority(capability):
            raise CtpExecutionGateError("ctp_execution_gate_capability_required")
        with self._query_state_lock:
            installed = self._execution_gate_capability
            if installed is not None and installed is not capability:
                raise CtpExecutionGateError("ctp_execution_gate_already_configured")
            if installed is None:
                self._execution_gate_capability = capability
                self._execution_gate_proof = None
                self._execution_gate_proof_sha256 = None
                self._execution_gate_environment_profile = None
                self._execution_gate_strategy_identity_sha256 = None
                self._execution_gate_cycle_id = None
                self._execution_gate_revocation_reason = None
            return self._execution_gate_state_locked()

    def _issue_execution_authorization_for_core(
        self,
        capability: object,
        proof: Mapping[str, Any],
        *,
        environment_profile: str,
        environment_verified: bool,
        strategy_identity_sha256: str,
        execution_cycle_id: str,
    ) -> object:
        """Create one native arm grant after the core has checked preflight.

        This intentionally private method is the only mapping-to-grant bridge.
        ``arm_execution_gate`` itself accepts the returned opaque grant, never
        a caller-provided proof mapping.  The grant is consumed by its first
        arm attempt, including a failed context check.
        """

        with self._query_state_lock:
            if not _is_ctp_core_execution_authority(capability):
                raise CtpExecutionGateError("ctp_execution_gate_capability_required")
            if capability is not self._execution_gate_capability:
                raise CtpExecutionGateError("ctp_execution_gate_capability_mismatch")
            self._require_bound_identity_locked(require_active_front=True)
            normalized, _proof_sha256 = _execution_gate_proof(proof)
            profile = str(environment_profile or "").strip()
            if self.auto_settlement_confirm is not False:
                raise CtpExecutionGateError(
                    "ctp_execution_gate_auto_settlement_confirm_enabled"
                )
            if not self.is_trading_ready:
                raise CtpExecutionGateError(
                    "ctp_execution_gate_session_not_trading_ready"
                )
            if (
                environment_verified is not True
                or not profile
                or profile != normalized["environment_profile"]
                or normalized["connection_generation"] != self._connection_generation
                or normalized["account_fingerprint"]
                != f"acct_{self._account_fingerprint}"
                or normalized["trading_day"] != self._trading_day
                or self._api is None
                or self._api is not self._session_native_api
            ):
                raise CtpExecutionGateError(
                    "ctp_execution_gate_authorization_context_mismatch"
                )
            if (
                not isinstance(strategy_identity_sha256, str)
                or len(strategy_identity_sha256) != 64
                or any(
                    char not in "0123456789abcdef" for char in strategy_identity_sha256
                )
                or not isinstance(execution_cycle_id, str)
                or not execution_cycle_id
                or execution_cycle_id != execution_cycle_id.strip()
                or len(execution_cycle_id) > 128
            ):
                raise CtpExecutionGateError(
                    "ctp_execution_gate_authorization_identity_invalid"
                )
            return _CtpExecutionAuthorization(
                client=self,
                capability=capability,
                proof=normalized,
                environment_profile=profile,
                strategy_identity_sha256=strategy_identity_sha256,
                execution_cycle_id=execution_cycle_id,
                preflight_epoch=self._execution_preflight_epoch,
            )

    def _issue_settlement_authorization_for_core(
        self,
        capability: object,
        *,
        environment_profile: str,
        environment_verified: bool,
        scope: str = "settlement_confirmation",
        budget: int = 1,
    ) -> object:
        """Create one independent, account/day/generation-bound write grant."""

        with self._query_state_lock:
            if not _is_ctp_core_execution_authority(capability):
                raise CtpExecutionGateError("ctp_execution_gate_capability_required")
            if capability is not self._execution_gate_capability:
                raise CtpExecutionGateError("ctp_execution_gate_capability_mismatch")
            self._require_bound_identity_locked(require_active_front=True)
            profile = str(environment_profile or "").strip()
            if self.auto_settlement_confirm is not False:
                raise CtpExecutionGateError(
                    "ctp_execution_gate_auto_settlement_confirm_enabled"
                )
            if (
                environment_verified is not True
                or not profile
                or self._execution_gate_proof is not None
                or not self.is_read_only_ready
                or self._connection_generation <= 0
                or not self._trading_day
                or self._api is None
                or self._api is not self._session_native_api
                or scope != "settlement_confirmation"
                or type(budget) is not int
                or budget != 1
            ):
                raise CtpExecutionGateError(
                    "ctp_settlement_authorization_context_mismatch"
                )
            return _CtpSettlementAuthorization(
                client=self,
                capability=capability,
                account_fingerprint=f"acct_{self._account_fingerprint}",
                trading_day=self._trading_day,
                connection_generation=self._connection_generation,
                environment_profile=profile,
                scope=scope,
                budget=budget,
            )

    def _issue_execution_authorization_for_test(
        self,
        capability: object,
        proof: Mapping[str, Any],
        *,
        strategy_identity_sha256: str = "0" * 64,
        execution_cycle_id: str = "controlled-test-cycle",
    ) -> object:
        """Explicit offline-only issuer for native contract tests.

        It remains private and requires the separately marked test authority.
        ``arm_execution_gate`` itself never accepts a proof mapping, including
        in tests, so production and fixture call paths exercise the same
        opaque one-shot consumption boundary.
        """

        if not _is_ctp_test_execution_authority(capability):
            raise CtpExecutionGateError("ctp_execution_gate_authorization_required")
        profile = str(proof.get("environment_profile") or "").strip()
        return self._issue_execution_authorization_for_core(
            capability,
            proof,
            environment_profile=profile,
            environment_verified=True,
            strategy_identity_sha256=strategy_identity_sha256,
            execution_cycle_id=execution_cycle_id,
        )

    def _issue_settlement_authorization_for_test(
        self,
        capability: object,
        *,
        environment_profile: str = "controlled-test-environment",
    ) -> object:
        """Explicit offline-only issuer for a single settlement write."""

        if not _is_ctp_test_execution_authority(capability):
            raise CtpExecutionGateError("ctp_settlement_authorization_required")
        return self._issue_settlement_authorization_for_core(
            capability,
            environment_profile=environment_profile,
            environment_verified=True,
        )

    def _revoke_execution_gate_locked(self, reason: Any) -> None:
        if self._execution_gate_capability is None:
            return
        # A revocation must invalidate every independently issued, unconsumed
        # arm grant from this native preflight.  The issuer permits more than
        # one opaque grant for controlled core workflows, so clearing only the
        # currently armed proof would otherwise leave a sibling grant usable
        # on the same account/day/generation.
        self._execution_preflight_epoch = (
            getattr(self, "_execution_preflight_epoch", 0) + 1
        )
        self._execution_gate_proof = None
        self._execution_gate_proof_sha256 = None
        self._execution_gate_environment_profile = None
        self._execution_gate_strategy_identity_sha256 = None
        self._execution_gate_cycle_id = None
        self._execution_gate_native_api = None
        self._execution_gate_revocation_reason = _execution_gate_reason(reason)

    def _require_execution_write_locked(
        self,
        capability: object | None,
        instrument: Any,
        exchange_id: Any = None,
    ) -> None:
        installed = self._execution_gate_capability
        if installed is None:
            raise CtpExecutionGateError("ctp_execution_gate_capability_required")
        if capability is not installed:
            raise CtpExecutionGateError("ctp_execution_gate_capability_mismatch")
        self._require_bound_identity_locked(require_active_front=True)
        proof = self._execution_gate_proof
        if proof is None:
            raise CtpExecutionGateError("ctp_execution_gate_unarmed")
        mismatches = (
            (
                self._api is None
                or self._api is not self._execution_gate_native_api
                or self._api is not self._session_native_api,
                "ctp_execution_gate_native_api_mismatch",
            ),
            (
                self._connection_generation != proof["connection_generation"],
                "ctp_execution_gate_connection_generation_mismatch",
            ),
            (
                f"acct_{self._account_fingerprint}" != proof["account_fingerprint"],
                "ctp_execution_gate_account_fingerprint_mismatch",
            ),
            (
                self._trading_day != proof["trading_day"],
                "ctp_execution_gate_trading_day_mismatch",
            ),
            (
                not self.is_trading_ready,
                "ctp_execution_gate_session_not_trading_ready",
            ),
            (
                _canonical_execution_gate_instrument(
                    proof,
                    instrument,
                    exchange_id,
                    native_wire=True,
                )
                not in _execution_gate_instruments(proof),
                "ctp_execution_gate_instrument_mismatch",
            ),
        )
        for mismatched, code in mismatches:
            if mismatched:
                self._revoke_execution_gate_locked(code)
                raise CtpExecutionGateError(code)

    def require_execution_write(
        self,
        capability: object | None,
        instrument: Any,
        exchange_id: Any = None,
    ) -> None:
        """Reject a managed order write before request IDs or native calls change."""

        with self._query_state_lock:
            self._require_execution_write_locked(capability, instrument, exchange_id)

    def arm_execution_gate(
        self,
        capability: object,
        authorization: object,
        *,
        _environment_profile: object | None = None,
        _environment_verified: object = False,
    ) -> dict[str, Any]:
        """Consume one core-issued grant to bind native order writes.

        A proof mapping is intentionally rejected here.  Public session state
        and its hashes are evidence only; they cannot establish a native write
        permission without the private, one-shot authorization created by the
        core-owned issuer above.
        """

        with self._query_state_lock:
            if (
                not _is_ctp_core_execution_authority(capability)
                or capability is not self._execution_gate_capability
            ):
                raise CtpExecutionGateError("ctp_execution_gate_capability_mismatch")
            try:
                if (
                    type(authorization) is not _CtpExecutionAuthorization
                    or authorization._seal is not _CTP_EXECUTION_AUTHORIZATION_SEAL
                    or authorization._client_ref() is not self
                    or authorization._capability is not capability
                    or authorization._used
                ):
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_authorization_required"
                    )
                self._require_bound_identity_locked(require_active_front=True)
                normalized, proof_sha256 = _execution_gate_proof(authorization._proof)
                profile = authorization._environment_profile
                if (
                    authorization._preflight_epoch != self._execution_preflight_epoch
                    or not profile
                    or profile != normalized["environment_profile"]
                    or _environment_verified is not True
                    or str(_environment_profile or "").strip() != profile
                ):
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_environment_profile_mismatch"
                    )
                if self.auto_settlement_confirm is not False:
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_auto_settlement_confirm_enabled"
                    )
                if normalized["connection_generation"] != self._connection_generation:
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_connection_generation_mismatch"
                    )
                if (
                    normalized["account_fingerprint"]
                    != f"acct_{self._account_fingerprint}"
                ):
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_account_fingerprint_mismatch"
                    )
                if normalized["trading_day"] != self._trading_day:
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_trading_day_mismatch"
                    )
                if self._api is None or self._api is not self._session_native_api:
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_native_api_mismatch"
                    )
                if not self.is_trading_ready:
                    raise CtpExecutionGateError(
                        "ctp_execution_gate_session_not_trading_ready"
                    )
                if self._execution_gate_proof is not None:
                    raise CtpExecutionGateError("ctp_execution_gate_already_armed")
            except CtpExecutionGateError as exc:
                if type(authorization) is _CtpExecutionAuthorization:
                    authorization._used = True
                self._revoke_execution_gate_locked(exc.code)
                raise
            authorization._used = True
            self._execution_gate_proof = normalized
            self._execution_gate_proof_sha256 = proof_sha256
            self._execution_gate_environment_profile = profile
            self._execution_gate_strategy_identity_sha256 = (
                authorization._strategy_identity_sha256
            )
            self._execution_gate_cycle_id = authorization._execution_cycle_id
            self._execution_gate_native_api = self._api
            self._execution_gate_revocation_reason = None
            return self._execution_gate_state_locked()

    def disarm_execution_gate(
        self,
        capability: object,
        reason: str = "execution_arm_revoked",
    ) -> dict[str, Any]:
        """Idempotently revoke managed native order writes."""

        with self._query_state_lock:
            if capability is not self._execution_gate_capability:
                raise CtpExecutionGateError("ctp_execution_gate_capability_mismatch")
            bounded_reason = _execution_gate_reason(reason)
            if self._execution_gate_proof is not None:
                self._revoke_execution_gate_locked(bounded_reason)
            else:
                # An explicit disarm is also a revocation boundary when no
                # proof is currently armed: sibling opaque grants may still
                # exist and must require a new preflight.
                self._execution_preflight_epoch = (
                    getattr(self, "_execution_preflight_epoch", 0) + 1
                )
                if self._execution_gate_revocation_reason is None:
                    self._execution_gate_revocation_reason = bounded_reason
            return self._execution_gate_state_locked()

    def submit_order_insert(
        self,
        field: Any,
        request_id: int,
        *,
        execution_capability: object | None = None,
    ) -> Any:
        """Submit one order under the same lock as the final managed-gate check."""

        with self._query_state_lock:
            self._require_execution_write_locked(
                execution_capability,
                getattr(field, "InstrumentID", ""),
                getattr(field, "ExchangeID", ""),
            )
            self._require_native_field_identity_locked(field, require_user_id=True)
            if self._api is None:
                raise CtpExecutionGateError("ctp_execution_gate_native_api_unavailable")
            self._record_request("order_insert")
            return self._api.ReqOrderInsert(field, request_id)

    def submit_order_action(
        self,
        field: Any,
        request_id: int,
        *,
        execution_capability: object | None = None,
    ) -> Any:
        """Submit one cancellation under the managed native-order gate."""

        with self._query_state_lock:
            self._require_execution_write_locked(
                execution_capability,
                getattr(field, "InstrumentID", ""),
                getattr(field, "ExchangeID", ""),
            )
            self._require_native_field_identity_locked(field, require_user_id=False)
            if self._api is None:
                raise CtpExecutionGateError("ctp_execution_gate_native_api_unavailable")
            self._record_request("order_action")
            return self._api.ReqOrderAction(field, request_id)

    def _next_request_id(self) -> int:
        with self._query_state_lock:
            self._req_id += 1
            return self._req_id

    def _record_request(self, request_type: str) -> None:
        with self._query_state_lock:
            self._request_counts[request_type] = (
                self._request_counts.get(request_type, 0) + 1
            )

    def _clear_settlement_readback_locked(
        self,
        reason: str,
        *,
        request_id: int | None = None,
    ) -> None:
        """Invalidate server-readback proof and revoke any native write grant."""

        self._ready = False
        self._settlement_readback_verified = False
        self._settlement_proof_source = "none"
        self._settlement_proof_query_request_id = None
        self._last_session_error = {
            "error": reason,
            "query_request_id": request_id,
        }
        self._revoke_execution_gate_locked(reason)

    def _has_current_settlement_readback_locked(self) -> bool:
        return (
            self._settlement_readback_verified
            and self._settlement_proof_source == "confirmation_query"
            and self._settlement_proof_query_request_id is not None
            and self._settlement_connection_generation == self._connection_generation
            and self._settlement_account_fingerprint == self._account_fingerprint
            and self._settlement_trading_day == self._trading_day
        )

    def _on_front_connected(self) -> None:
        with self._query_state_lock:
            self._revoke_execution_gate_locked(
                "ctp_execution_gate_connection_generation_changed"
            )
            self._session_native_api = self._api
            # The registered front is fixed by start(); a reconnect does not
            # accept a mutable public ``front`` replacement.
            self._session_native_front = self._bound_front
            self._connection_generation += 1
            self._execution_preflight_epoch += 1
            self._connected = True
            self._ready = False
            self._authentication_state = "authenticating"
            self._login_state = "not_started"
            self._authentication_request_id = None
            self._authentication_connection_generation = None
            self._login_request_id = None
            self._login_connection_generation = None
            self._settlement_state = "unknown"
            self._settlement_request_id = None
            self._settlement_connection_generation = None
            self._settlement_account_fingerprint = None
            self._settlement_trading_day = None
            self._settlement_proof_source = "none"
            self._settlement_proof_query_request_id = None
            self._settlement_readback_verified = False
            self._last_session_error = {}

    def _on_front_disconnected(self, reason: Any) -> None:
        with self._query_state_lock:
            self._revoke_execution_gate_locked("ctp_execution_gate_disconnected")
            self._session_native_api = None
            self._session_native_front = None
            self._execution_preflight_epoch += 1
            self._connected = False
            self._ready = False
            self._authentication_state = "disconnected"
            self._login_state = "disconnected"
            self._authentication_request_id = None
            self._authentication_connection_generation = None
            self._login_request_id = None
            self._login_connection_generation = None
            self._settlement_state = "unknown"
            self._settlement_request_id = None
            self._settlement_connection_generation = None
            self._settlement_account_fingerprint = None
            self._settlement_trading_day = None
            self._settlement_proof_source = "none"
            self._settlement_proof_query_request_id = None
            self._settlement_readback_verified = False
            self._last_session_error = {"disconnect_reason": reason}
            for accumulator in self._query_history.values():
                if accumulator.sealed:
                    continue
                accumulator.error_code = -2
                accumulator.error_message = "connection_lost"
                accumulator.completed_at_utc = datetime.now(timezone.utc)
                accumulator.sealed = True
                accumulator.event.set()

    def _require_settlement_write_locked(
        self,
        execution_capability: object | None,
        settlement_authorization: object | None,
        settlement_environment_profile: object | None,
        settlement_environment_verified: object,
    ) -> _CtpSettlementAuthorization:
        installed = self._execution_gate_capability
        if installed is None or not _is_ctp_core_execution_authority(installed):
            raise CtpExecutionGateError("ctp_execution_gate_capability_required")
        if execution_capability is not installed:
            raise CtpExecutionGateError("ctp_execution_gate_capability_mismatch")
        self._require_bound_identity_locked(require_active_front=True)
        if (
            type(settlement_authorization) is not _CtpSettlementAuthorization
            or settlement_authorization._seal is not _CTP_EXECUTION_AUTHORIZATION_SEAL
            or settlement_authorization._client_ref() is not self
            or settlement_authorization._capability is not installed
            or settlement_authorization._used
            or settlement_authorization._scope != "settlement_confirmation"
            or settlement_authorization._budget != 1
            or settlement_authorization._connection_generation
            != self._connection_generation
            or settlement_authorization._account_fingerprint
            != f"acct_{self._account_fingerprint}"
            or settlement_authorization._trading_day != self._trading_day
            or settlement_environment_verified is not True
            or str(settlement_environment_profile or "").strip()
            != settlement_authorization._environment_profile
        ):
            raise CtpExecutionGateError("ctp_settlement_authorization_required")
        if self.auto_settlement_confirm is not False:
            raise CtpExecutionGateError(
                "ctp_execution_gate_auto_settlement_confirm_enabled"
            )
        if self._execution_gate_proof is not None:
            raise CtpExecutionGateError(
                "ctp_execution_gate_settlement_requires_disarmed"
            )
        if (
            not self.is_read_only_ready
            or self._connection_generation <= 0
            or not self._trading_day
        ):
            raise CtpExecutionGateError(
                "ctp_execution_gate_session_not_read_only_ready"
            )
        if self._api is None or self._api is not self._session_native_api:
            raise CtpExecutionGateError("ctp_execution_gate_native_api_mismatch")
        return settlement_authorization

    def _request_settlement_confirmation(
        self,
        *,
        execution_capability: object | None = None,
        settlement_authorization: object | None = None,
        settlement_environment_profile: object | None = None,
        settlement_environment_verified: object = False,
        expected_generation: int | None = None,
    ) -> bool:
        with self._query_state_lock:
            authorization = self._require_settlement_write_locked(
                execution_capability,
                settlement_authorization,
                settlement_environment_profile,
                settlement_environment_verified,
            )
            if (
                expected_generation is not None
                and expected_generation != self._connection_generation
            ):
                return False
            if self._settlement_state == "confirmed":
                return True
            if (
                self._execution_gate_capability is not None
                and self._settlement_request_id is not None
            ):
                # One managed confirmation attempt is allowed per connection.
                return self._settlement_state == "confirming"
            api = self._api
            if api is None:
                raise CtpExecutionGateError("ctp_execution_gate_native_api_unavailable")
            # The terminal write consumes its independently issued budget and
            # invalidates every arm token/proof issued from the preceding
            # preflight epoch before a native method can run.
            authorization._used = True
            self._execution_preflight_epoch += 1
            self._revoke_execution_gate_locked(
                "ctp_settlement_confirmation_invalidated_preflight"
            )
            field = CThostFtdcSettlementInfoConfirmField()
            field.BrokerID = self._bound_broker_id
            field.InvestorID = self._bound_user_id
            request_id = self._next_request_id()
            self._settlement_done.clear()
            self._settlement_state = "confirming"
            self._settlement_request_id = request_id
            self._settlement_connection_generation = self._connection_generation
            self._settlement_account_fingerprint = self._account_fingerprint
            self._settlement_trading_day = self._trading_day
            self._ready = False
            self._settlement_readback_verified = False
            self._record_request("settlement_confirm")
            generation = self._connection_generation
        try:
            ret = api.ReqSettlementInfoConfirm(field, request_id)
        except Exception as exc:
            with self._query_state_lock:
                if (
                    self._settlement_state == "confirming"
                    and self._settlement_request_id == request_id
                    and self._settlement_connection_generation == generation
                    and self._connection_generation == generation
                ):
                    self._settlement_state = "failed"
                    self._last_session_error = {
                        "error": "settlement_confirm_submit_failed",
                        "detail": type(exc).__name__,
                    }
            return False
        if ret not in (None, 0):
            with self._query_state_lock:
                if (
                    self._settlement_state == "confirming"
                    and self._settlement_request_id == request_id
                    and self._settlement_connection_generation == generation
                    and self._connection_generation == generation
                ):
                    self._settlement_state = "failed"
                    self._last_session_error = {
                        "error": "settlement_confirm_submit_rejected",
                        "submit_code": ret,
                    }
            return False
        with self._query_state_lock:
            if self._connection_generation != generation:
                return False
            if self._settlement_request_id != request_id:
                return False
            if self._settlement_state == "failed":
                return False
            return True

    def _accept_settlement_callback(self, request_id: int, field: Any = None) -> bool:
        """Fence confirmation responses by request, generation and pending state."""
        with self._query_state_lock:
            accepted = (
                self._settlement_state == "confirming"
                and self._settlement_request_id == int(request_id)
                and self._settlement_connection_generation
                == self._connection_generation
                and self._settlement_account_fingerprint == self._account_fingerprint
                and self._settlement_trading_day == self._trading_day
            )
            field_broker = str(getattr(field, "BrokerID", "") or "")
            field_investor = str(getattr(field, "InvestorID", "") or "")
            field_trading_day = str(
                getattr(field, "TradingDay", "")
                or getattr(field, "ConfirmDate", "")
                or ""
            )
            if field_broker and field_broker != self._bound_broker_id:
                accepted = False
            if field_investor and field_investor != self._bound_user_id:
                accepted = False
            if field_trading_day and field_trading_day != self._trading_day:
                accepted = False
            if not accepted:
                self._settlement_late_callback_count += 1
                return False
            return True

    def confirm_settlement(
        self,
        timeout: float = 5.0,
        *,
        _execution_capability: object | None = None,
        _settlement_authorization: object | None = None,
        _settlement_environment_profile: object | None = None,
        _settlement_environment_verified: object = False,
    ) -> bool:
        """Submit confirmation; trading stays read-only until server readback."""
        with self._query_state_lock:
            self._require_settlement_write_locked(
                _execution_capability,
                _settlement_authorization,
                _settlement_environment_profile,
                _settlement_environment_verified,
            )
            if not self.is_read_only_ready:
                return False
            if self._settlement_state == "confirmed":
                return True
        if not self._request_settlement_confirmation(
            execution_capability=_execution_capability,
            settlement_authorization=_settlement_authorization,
            settlement_environment_profile=_settlement_environment_profile,
            settlement_environment_verified=_settlement_environment_verified,
        ):
            return False
        if not self._settlement_done.wait(max(float(timeout), 0.0)):
            with self._query_state_lock:
                if self._settlement_state == "confirmed":
                    return True
                if self._settlement_state == "confirming":
                    self._settlement_state = "failed"
                    self._ready = False
                    self._last_session_error = {"error": "settlement_confirm_timeout"}
                return False
        return self._settlement_state == "confirmed"

    def get_session_state(self) -> dict[str, Any]:
        """Return distinct authentication, login, settlement and query state."""
        with self._query_state_lock:
            execution_gate = self._execution_gate_state_locked()
            return {
                "connected": self._connected,
                "auth_state": self._authentication_state,
                "login_state": self._login_state,
                "settlement_state": self._settlement_state,
                "read_only_ready": self.is_read_only_ready,
                "trading_ready": self.is_trading_ready,
                "ready": self.is_ready,
                "auto_settlement_confirm": self.auto_settlement_confirm,
                "front_id": self._front_id,
                "session_id": self._session_id,
                "trading_day": self._trading_day,
                "connection_generation": self._connection_generation,
                "account_fingerprint": self._account_fingerprint,
                "settlement_request_id": self._settlement_request_id,
                "settlement_connection_generation": self._settlement_connection_generation,
                "settlement_account_fingerprint": self._settlement_account_fingerprint,
                "settlement_trading_day": self._settlement_trading_day,
                "settlement_late_callback_count": self._settlement_late_callback_count,
                "settlement_proof_source": self._settlement_proof_source,
                "settlement_proof_query_request_id": self._settlement_proof_query_request_id,
                "settlement_readback_verified": (
                    self._has_current_settlement_readback_locked()
                ),
                "authentication_request_id": self._authentication_request_id,
                "authentication_connection_generation": (
                    self._authentication_connection_generation
                ),
                "authentication_late_callback_count": (
                    self._authentication_late_callback_count
                ),
                "login_request_id": self._login_request_id,
                "login_connection_generation": self._login_connection_generation,
                "login_late_callback_count": self._login_late_callback_count,
                "request_counts": dict(self._request_counts),
                "last_error": dict(self._last_session_error),
                "execution_gate_managed": execution_gate["managed"],
                "execution_gate_armed": execution_gate["armed"],
                "execution_gate_connection_generation": execution_gate[
                    "connection_generation"
                ],
                "execution_gate_instrument": execution_gate["instrument"],
                "execution_gate_scope_version": execution_gate["scope_version"],
                "execution_gate_authorized_instruments": execution_gate[
                    "authorized_instruments"
                ],
                "execution_gate_proof_sha256": execution_gate["proof_sha256"],
                "execution_gate_revocation_reason": execution_gate["revocation_reason"],
            }

    def get_query_session_scope(self) -> _QuerySessionScope:
        """Return an opaque typed scope for strict read-only query consumers.

        The returned object is issued by this client instance and carries the
        current account/day/generation identity.  It is separate from the
        historical mapping returned by :meth:`get_session_state`, which stays
        a compatibility diagnostic view and cannot certify a query result.
        """

        # Capture this strict scope after the public session read.  This
        # preserves query-completion-to-scope latency in the same clock
        # domain when a client supplies a delayed session readback.  The
        # mapping returned by that read is deliberately ignored: only the
        # typed scope created below is trusted by evidence consumers.
        self.get_session_state()
        with self._query_state_lock:
            return _new_query_session_scope(
                issuer=self._query_evidence_issuer,
                account_fingerprint=self._account_fingerprint,
                connection_generation=self._connection_generation,
                trading_day=self._trading_day,
                broker_id=self._bound_broker_id,
                investor_id=self._bound_user_id,
                read_only_ready=self.is_read_only_ready,
                captured_at_utc=datetime.now(timezone.utc),
                captured_monotonic=time.monotonic(),
            )

    def get_request_counts(self) -> dict[str, int]:
        """Return a read-only snapshot of native requests issued this session."""
        with self._query_state_lock:
            return dict(self._request_counts)

    def _reserve_start_generation(self) -> int:
        """Reserve one startup generation before creating the native API."""

        with self._query_state_lock:
            # Preserve the managed-gate revocation contract even when this is
            # a rejected re-entrant start attempt.
            self._revoke_execution_gate_locked("ctp_execution_gate_client_start")
            if self._api is not None or self._starting_generation is not None:
                raise RuntimeError("ctp_trader_client_already_started")
            self._lifecycle_generation += 1
            generation = self._lifecycle_generation
            self._starting_generation = generation
            self._startup_cancel_event.clear()
            return generation

    def _clear_start_reservation(self, generation: int) -> None:
        with self._query_state_lock:
            if self._starting_generation == generation:
                self._starting_generation = None

    def _is_start_current_locked(self, api: Any, spi: Any, generation: int) -> bool:
        return (
            self._lifecycle_generation == generation
            and self._starting_generation == generation
            and self._api is api
            and self._spi is spi
            and not self._startup_cancel_event.is_set()
        )

    def _run_startup_call(
        self,
        api: Any,
        spi: Any,
        generation: int,
        callback: Callable[[], Any],
        *,
        starts_native_thread: bool = False,
    ) -> tuple[bool, bool]:
        """Run one native startup call with pre/post cancellation fences."""

        with self._query_state_lock:
            if not self._is_start_current_locked(api, spi, generation):
                return False, False
            if starts_native_thread:
                # A re-entrant stop from Init() must treat the API as live
                # before the vendor is allowed to create its callback thread.
                self._native_init_started = True
                self._join_active = True
            # stop() sets its event before it waits for this lock.  Repeat the
            # precondition immediately before entering native code so a stop
            # that arrived during the preceding bookkeeping cancels this step.
            if not self._is_start_current_locked(api, spi, generation):
                if starts_native_thread:
                    self._native_init_started = False
                    self._join_active = False
                return False, False
            callback()
            return True, self._is_start_current_locked(api, spi, generation)

    def _abort_startup(
        self,
        api: Any,
        spi: Any,
        generation: int,
        *,
        native_init_may_be_live: bool = False,
    ) -> bool:
        """Clean up a failed startup only while this generation owns it."""

        release_now = False
        observe_join = False
        with self._query_state_lock:
            if not self._is_start_current_locked(api, spi, generation):
                return False
            native_live = native_init_may_be_live or self._native_init_started
            if native_live:
                _retain_live_ctp_native_session(api, spi, self._thread)
                observe_join = True
            else:
                release_now = True
            self._api = None
            self._thread = None
            self._join_active = False
            self._native_init_started = False
            self._starting_generation = None
            self._lifecycle_generation += 1

        if observe_join:
            with suppress(Exception):
                api.RegisterSpi(None)
            self._start_join_observer(api)
        elif release_now:
            with suppress(Exception):
                api.RegisterSpi(None)
            with suppress(Exception):
                api.Release()
        return True

    def _join_native_api(self, api: Any) -> None:
        join_returned = False
        try:
            api.Join()
            join_returned = True
        finally:
            if join_returned:
                with self._query_state_lock:
                    if self._api is api:
                        self._join_active = False
                        self._native_init_started = False
                    if self._thread is threading.current_thread():
                        self._thread = None
                _release_retired_ctp_native_session_after_join(api)

    def _start_join_observer(self, api: Any) -> bool:
        """Start one Join observer for either the current or retired session."""

        thread = threading.Thread(
            target=self._join_native_api, args=(api,), daemon=True
        )
        with self._query_state_lock:
            if self._api is api:
                self._thread = thread
                self._join_active = True
            elif not _set_retired_ctp_native_session_join_thread(api, thread):
                return False
        thread.start()
        return True

    def start(self, block=False):
        """启动连接（默认后台运行）"""
        _check_native_module()
        generation = self._reserve_start_generation()
        with self._query_state_lock:
            self._require_bound_identity_locked(require_active_front=False)
        flow = _flow_dir(f"td_{self._bound_broker_id}_{self._bound_user_id}")
        try:
            api = CThostFtdcTraderApi.CreateFtdcTraderApi(flow)
        except Exception:
            self._clear_start_reservation(generation)
            raise
        spi = _TraderSpi(self, api)
        with self._query_state_lock:
            if (
                self._starting_generation != generation
                or self._lifecycle_generation != generation
                or self._startup_cancel_event.is_set()
            ):
                cancelled_before_registration = True
            else:
                cancelled_before_registration = False
                self._api = api
                self._spi = spi
                self._native_init_started = False
                self._join_active = False
        if cancelled_before_registration:
            with suppress(Exception):
                api.RegisterSpi(None)
            with suppress(Exception):
                api.Release()
            return

        init_invoked = False

        # ``RegisterFront`` below always receives this immutable value.  Keep
        # the exact native-front binding alongside the native API so a later
        # public ``front`` mutation cannot be mistaken for a verified
        # environment on reconnect.
        with self._query_state_lock:
            if self._api is not api or self._spi is not spi:
                self._abort_startup(api, spi, generation)
                return
            self._session_native_front = self._bound_front

        def init_native_api() -> None:
            nonlocal init_invoked
            init_invoked = True
            api.Init()

        try:
            startup_calls = (
                lambda: api.RegisterSpi(spi),
                lambda: api.SubscribePrivateTopic(2),
                lambda: api.SubscribePublicTopic(2),
                lambda: api.RegisterFront(self._bound_front),
            )
            for startup_call in startup_calls:
                invoked, active = self._run_startup_call(
                    api, spi, generation, startup_call
                )
                if not invoked or not active:
                    self._abort_startup(api, spi, generation)
                    return
            invoked, active = self._run_startup_call(
                api,
                spi,
                generation,
                init_native_api,
                starts_native_thread=True,
            )
            if not invoked:
                self._abort_startup(api, spi, generation)
                return
            if not active:
                # stop() detached this API while Init was running.  It is
                # retained already; attach a Join observer so the registry is
                # released when the native thread exits.
                self._start_join_observer(api)
                return
        except Exception:
            # Init is a void vendor call, but if a binding raises after it was
            # entered, fail safe and retain until Join proves native shutdown.
            handled = self._abort_startup(
                api,
                spi,
                generation,
                native_init_may_be_live=init_invoked,
            )
            if init_invoked and not handled:
                # A concurrent stop may have set the cancellation fence while
                # Init raised.  Attach the observer whether it already
                # retained the session or is about to do so.
                self._start_join_observer(api)
            raise

        if block:
            with self._query_state_lock:
                current = self._is_start_current_locked(api, spi, generation)
            if not current:
                self._start_join_observer(api)
                return
            try:
                self._join_native_api(api)
            except KeyboardInterrupt:
                pass
            finally:
                self.stop()
            return

        self._start_join_observer(api)

    def wait_ready(self, timeout=15):
        """Wait for login, direct confirmation and matching server readback."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_ready:
                return True
            with self._query_state_lock:
                should_read_back = (
                    self.auto_settlement_confirm
                    and self.is_read_only_ready
                    and self._settlement_state == "confirmed"
                    and self._settlement_proof_source == "direct_confirmation"
                    and not self._has_current_settlement_readback_locked()
                )
            if should_read_back:
                remaining = max(0.0, deadline - time.time())
                self.verify_settlement_confirmation(timeout=min(5.0, remaining))
                continue
            time.sleep(min(0.2, max(0.0, deadline - time.time())))
        return self.is_ready

    def _new_query_accumulator(self, request_type: str) -> _QueryAccumulator:
        request_id = self._next_request_id()
        accumulator = _QueryAccumulator(
            request_type=request_type,
            request_id=request_id,
            connection_generation=self._connection_generation,
            account_fingerprint=self._account_fingerprint,
            started_at_utc=datetime.now(timezone.utc),
            source_issuer=self._query_evidence_issuer,
            trading_day=self._trading_day,
            broker_id=self._bound_broker_id,
            investor_id=self._bound_user_id,
            started_monotonic=time.monotonic(),
        )
        with self._query_state_lock:
            self._query_history[request_id] = accumulator
            while len(self._query_history) > 256:
                oldest_request_id = next(iter(self._query_history))
                oldest = self._query_history[oldest_request_id]
                if not oldest.sealed:
                    break
                self._query_history.pop(oldest_request_id, None)
        return accumulator

    def _local_query_failure(
        self,
        request_type: str,
        message: str,
        *,
        unsupported: bool = False,
    ) -> QueryResult[Any]:
        accumulator = self._new_query_accumulator(request_type)
        accumulator.error_code = -3 if unsupported else -1
        accumulator.error_message = message
        accumulator.unsupported = unsupported
        accumulator.completed_at_utc = datetime.now(timezone.utc)
        accumulator.completed_monotonic = time.monotonic()
        accumulator.sealed = True
        accumulator.event.set()
        return accumulator.result()

    def _handle_query_callback(
        self,
        request_type: str,
        record: Any,
        rsp_info: Any,
        request_id: int,
        is_last: bool,
    ) -> None:
        error_code, error_message = _rsp_error(rsp_info)
        terminal_callback_at_utc = None
        terminal_callback_monotonic = None
        if is_last or error_code not in (None, 0):
            terminal_callback_at_utc = datetime.now(timezone.utc)
            terminal_callback_monotonic = time.monotonic()
        with self._query_state_lock:
            accumulator = self._query_history.get(int(request_id))
            if accumulator is None or accumulator.request_type != request_type:
                self._orphan_query_callbacks.append(
                    {
                        "request_type": request_type,
                        "request_id": request_id,
                        "connection_generation": self._connection_generation,
                        "is_last": bool(is_last),
                        "error_code": error_code,
                    }
                )
                del self._orphan_query_callbacks[:-256]
                return
            same_generation = (
                accumulator.connection_generation == self._connection_generation
            )
            if not same_generation:
                accumulator.late_callback_count += 1
                self._orphan_query_callbacks.append(
                    {
                        "request_type": request_type,
                        "request_id": request_id,
                        "connection_generation": self._connection_generation,
                        "is_last": bool(is_last),
                        "error_code": error_code,
                    }
                )
                del self._orphan_query_callbacks[:-256]
                return
            if (
                accumulator.sealed
                or accumulator.is_last_seen
                or accumulator.error_code not in (None, 0)
            ):
                accumulator.late_callback_count += 1
                return
            if record is not None:
                snapshot = _snapshot_query_record(record)
                if request_type == "instruments":
                    snapshot.update(normalize_ctp_instrument(snapshot))
                    # InstrumentField proves ExpireDate but carries neither an
                    # exchange trading calendar nor a prior-day market ranking.
                    snapshot.setdefault(
                        "expiry_date", snapshot.get("ExpireDate") or None
                    )
                    snapshot.setdefault("trading_days_to_expiry", None)
                    snapshot.setdefault("remaining_trading_days", None)
                    snapshot.setdefault("trading_calendar_evidence_complete", False)
                    snapshot.setdefault("ranking_trading_day", None)
                    snapshot.setdefault("prior_trading_day_volume", None)
                    snapshot.setdefault("prior_trading_day_open_interest", None)
                    snapshot.setdefault("prior_day_ranking_evidence_complete", False)
                accumulator.records.append(snapshot)
            if error_code not in (None, 0):
                accumulator.error_code = error_code
                accumulator.error_message = error_message
            if is_last:
                accumulator.is_last_seen = True
            if is_last or error_code not in (None, 0):
                # The terminal callback is the authoritative completion
                # boundary.  Capture both clocks and the raw-row digest before
                # releasing the state lock; a slow native return or a later
                # adapter conversion must not renew or mutate this evidence.
                if accumulator.completed_at_utc is None:
                    accumulator.completed_at_utc = terminal_callback_at_utc
                    accumulator.completed_monotonic = terminal_callback_monotonic
                if accumulator.source_records_sha256 is None:
                    try:
                        accumulator.source_records_sha256 = _query_records_digest(
                            tuple(accumulator.records)
                        )
                    except (TypeError, ValueError):
                        # A malformed native row remains visible to legacy
                        # callers, but cannot be promoted by the strict
                        # evidence consumer without a bound digest.
                        accumulator.source_records_sha256 = None
                accumulator.event.set()

    def _handle_query_error(
        self, rsp_info: Any, request_id: int, is_last: bool
    ) -> None:
        with self._query_state_lock:
            accumulator = self._query_history.get(int(request_id))
            request_type = (
                accumulator.request_type if accumulator is not None else "unknown"
            )
        self._handle_query_callback(request_type, None, rsp_info, request_id, is_last)

    def _execute_query(
        self,
        request_type: str,
        submit: Callable[[int], Any] | None,
        timeout: float,
    ) -> QueryResult[Any]:
        if not self.is_read_only_ready:
            return self._local_query_failure(request_type, "trader_not_logged_in")
        if not callable(submit):
            return self._local_query_failure(
                request_type,
                "native_query_unsupported",
                unsupported=True,
            )
        with self._query_lock:
            elapsed = time.monotonic() - self._last_query_submitted_at
            wait_time = self._query_interval - elapsed
            if wait_time > 0:
                time.sleep(wait_time)
            accumulator = self._new_query_accumulator(request_type)
            self._record_request(f"query_{request_type}")
            try:
                ret = submit(accumulator.request_id)
            except Exception as exc:
                accumulator.error_code = -4
                accumulator.error_message = (
                    f"query_submit_exception:{type(exc).__name__}"
                )
                accumulator.completed_at_utc = datetime.now(timezone.utc)
                accumulator.completed_monotonic = time.monotonic()
                accumulator.sealed = True
                accumulator.event.set()
                return accumulator.result()
            self._last_query_submitted_at = time.monotonic()
            accumulator.submit_code = None if ret is None else int(ret)
            if ret not in (None, 0):
                accumulator.error_code = int(ret)
                accumulator.error_message = "query_submit_rejected"
                accumulator.completed_at_utc = datetime.now(timezone.utc)
                accumulator.completed_monotonic = time.monotonic()
                accumulator.sealed = True
                accumulator.event.set()
                return accumulator.result()
            observed = accumulator.event.wait(max(float(timeout), 0.0))
            with self._query_state_lock:
                # If the terminal callback acquired the state lock at the
                # timeout boundary, it wins the race and remains complete.
                if (
                    not observed
                    and not accumulator.is_last_seen
                    and accumulator.error_code in (None, 0)
                ):
                    accumulator.timed_out = True
                    accumulator.error_message = "query_timeout"
                if accumulator.completed_at_utc is None:
                    accumulator.completed_at_utc = datetime.now(timezone.utc)
                    accumulator.completed_monotonic = time.monotonic()
                accumulator.sealed = True
                accumulator.event.set()
                return accumulator.result()

    def get_query_result(self, request_id: int) -> QueryResult[Any] | None:
        """Return current evidence, including callbacks arriving after timeout."""
        with self._query_state_lock:
            accumulator = self._query_history.get(int(request_id))
            return accumulator.result() if accumulator is not None else None

    def query_account_result(self, timeout=5) -> QueryResult[Any]:
        field = CThostFtdcQryTradingAccountField()
        field.BrokerID = self._bound_broker_id
        field.InvestorID = self._bound_user_id
        method = getattr(self._api, "ReqQryTradingAccount", None) if self._api else None
        return self._execute_query(
            "account",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_account(self, timeout=5):
        """Compatibility view; incomplete queries return ``None``."""
        return self.query_account_result(timeout=timeout).first

    def query_positions_result(self, timeout=5) -> QueryResult[Any]:
        field = CThostFtdcQryInvestorPositionField()
        field.BrokerID = self._bound_broker_id
        field.InvestorID = self._bound_user_id
        method = (
            getattr(self._api, "ReqQryInvestorPosition", None) if self._api else None
        )
        return self._execute_query(
            "positions",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_positions(self, timeout=5):
        result = self.query_positions_result(timeout=timeout)
        return list(result.records) if result.complete else []

    def query_orders_result(
        self, instrument_id="", exchange_id="", order_sys_id="", timeout=5
    ) -> QueryResult[Any]:
        field = CThostFtdcQryOrderField()
        field.BrokerID = self._bound_broker_id
        field.InvestorID = self._bound_user_id
        if instrument_id:
            field.InstrumentID = str(instrument_id)
        if exchange_id:
            field.ExchangeID = str(exchange_id)
        if order_sys_id:
            field.OrderSysID = str(order_sys_id)
        method = getattr(self._api, "ReqQryOrder", None) if self._api else None
        return self._execute_query(
            "orders",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_orders(
        self, instrument_id="", exchange_id="", order_sys_id="", timeout=5
    ):
        result = self.query_orders_result(
            instrument_id=instrument_id,
            exchange_id=exchange_id,
            order_sys_id=order_sys_id,
            timeout=timeout,
        )
        return list(result.records) if result.complete else []

    def query_trades_result(
        self,
        instrument_id="",
        exchange_id="",
        trade_id="",
        start_time="",
        end_time="",
        timeout=5,
    ) -> QueryResult[Any]:
        field = CThostFtdcQryTradeField()
        field.BrokerID = self._bound_broker_id
        field.InvestorID = self._bound_user_id
        for name, value in (
            ("InstrumentID", instrument_id),
            ("ExchangeID", exchange_id),
            ("TradeID", trade_id),
            ("TradeTimeStart", start_time),
            ("TradeTimeEnd", end_time),
        ):
            if value:
                try:
                    setattr(field, name, str(value))
                except (AttributeError, TypeError, ValueError):
                    return self._local_query_failure(
                        "trades",
                        f"native_trade_filter_unsupported:{name}",
                        unsupported=True,
                    )
        method = getattr(self._api, "ReqQryTrade", None) if self._api else None
        return self._execute_query(
            "trades",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_trades(self, **kwargs: Any) -> list[Any]:
        result = self.query_trades_result(**kwargs)
        return list(result.records) if result.complete else []

    def query_instruments_result(
        self, instrument_id="", exchange_id="", product_id="", timeout=5
    ) -> QueryResult[Any]:
        """Read all visible instruments with empty filters, including options.

        Native fields are preserved alongside normalized reference metadata.
        Only ``complete=True`` proves the matching terminal packet; visibility
        is limited to the connected front, account and trading day.
        """
        field = CThostFtdcQryInstrumentField()
        field.InstrumentID = str(instrument_id or "")
        if exchange_id:
            field.ExchangeID = str(exchange_id)
        if product_id:
            try:
                field.ProductID = str(product_id)
            except Exception:
                # Do not silently fall back to the potentially expensive,
                # unfiltered instrument query when this native ABI cannot
                # represent ProductID.
                return self._local_query_failure(
                    "instruments",
                    "native_instrument_filter_unsupported:ProductID",
                    unsupported=True,
                )
        method = getattr(self._api, "ReqQryInstrument", None) if self._api else None
        return self._execute_query(
            "instruments",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_instrument(self, instrument_id, exchange_id="", timeout=5):
        return self.query_instruments_result(
            instrument_id=instrument_id, exchange_id=exchange_id, timeout=timeout
        ).first

    def query_instrument_margin_rate_result(
        self, instrument_id, exchange_id="", hedge_flag="1", timeout=5
    ) -> QueryResult[Any]:
        field = CThostFtdcQryInstrumentMarginRateField()
        field.BrokerID = self._bound_broker_id
        field.InvestorID = self._bound_user_id
        field.InstrumentID = str(instrument_id or "")
        if exchange_id:
            field.ExchangeID = str(exchange_id)
        if hedge_flag:
            field.HedgeFlag = str(hedge_flag)
        method = (
            getattr(self._api, "ReqQryInstrumentMarginRate", None)
            if self._api
            else None
        )
        return self._execute_query(
            "margin_rate",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_instrument_margin_rate(
        self, instrument_id, exchange_id="", hedge_flag="1", timeout=5
    ):
        return self.query_instrument_margin_rate_result(
            instrument_id,
            exchange_id=exchange_id,
            hedge_flag=hedge_flag,
            timeout=timeout,
        ).first

    def query_instrument_commission_rate_result(
        self, instrument_id, exchange_id="", timeout=5
    ) -> QueryResult[Any]:
        field = CThostFtdcQryInstrumentCommissionRateField()
        field.BrokerID = self._bound_broker_id
        field.InvestorID = self._bound_user_id
        field.InstrumentID = str(instrument_id or "")
        if exchange_id:
            field.ExchangeID = str(exchange_id)
        method = (
            getattr(self._api, "ReqQryInstrumentCommissionRate", None)
            if self._api
            else None
        )
        return self._execute_query(
            "commission_rate",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_instrument_commission_rate(
        self, instrument_id, exchange_id="", timeout=5
    ):
        return self.query_instrument_commission_rate_result(
            instrument_id, exchange_id=exchange_id, timeout=timeout
        ).first

    def _query_reference_result(
        self, request_type, field_type, method_name, values, timeout
    ):
        """Build a read request without silently dropping unsupported ABI fields."""
        try:
            field = field_type()
            for name, value in values.items():
                setattr(field, name, value)
        except Exception:
            return self._local_query_failure(
                request_type, "native_query_fields_unsupported", unsupported=True
            )
        method = getattr(self._api, method_name, None) if self._api else None
        return self._execute_query(
            request_type,
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def query_depth_market_data_result(
        self, instrument_id="", exchange_id="", timeout=5
    ) -> QueryResult[Any]:
        """Query TD reference quotes, or all visible quotes with empty filters.

        Raw source timestamps and prices are retained. A successful query is
        not evidence that the quotes are fresh, synchronized or executable.
        """
        return self._query_reference_result(
            "depth_market_data",
            CThostFtdcQryDepthMarketDataField,
            "ReqQryDepthMarketData",
            {
                "InstrumentID": str(instrument_id or ""),
                "ExchangeID": str(exchange_id or ""),
            },
            timeout,
        )

    def query_option_instrument_trade_cost_result(
        self,
        instrument_id,
        exchange_id="",
        hedge_flag="1",
        input_price=0.0,
        underlying_price=0.0,
        timeout=5,
    ) -> QueryResult[Any]:
        """Read the broker's option trade-cost response for the supplied prices.

        This preserves native FixedMargin/MiniMargin/Royalty fields; it is not
        a portfolio margin calculation. Zero inputs are passed as native zero
        without claiming a current executable price or a known pricing basis.
        """
        prices = {}
        for name, value in (
            ("InputPrice", input_price),
            ("UnderlyingPrice", underlying_price),
        ):
            if isinstance(value, bool):
                raise ValueError(f"{name} must be finite and non-negative")
            price = float(value)
            if not math.isfinite(price) or price < 0:
                raise ValueError(f"{name} must be finite and non-negative")
            prices[name] = price
        return self._query_reference_result(
            "option_trade_cost",
            CThostFtdcQryOptionInstrTradeCostField,
            "ReqQryOptionInstrTradeCost",
            {
                "BrokerID": self._bound_broker_id,
                "InvestorID": self._bound_user_id,
                "InstrumentID": str(instrument_id or ""),
                "ExchangeID": str(exchange_id or ""),
                "HedgeFlag": str(hedge_flag or ""),
                **prices,
            },
            timeout,
        )

    def query_option_instrument_commission_rate_result(
        self, instrument_id, exchange_id="", timeout=5
    ) -> QueryResult[Any]:
        """Read native option open/close/close-today and strike commission fields."""
        return self._query_reference_result(
            "option_commission_rate",
            CThostFtdcQryOptionInstrCommRateField,
            "ReqQryOptionInstrCommRate",
            {
                "BrokerID": self._bound_broker_id,
                "InvestorID": self._bound_user_id,
                "InstrumentID": str(instrument_id or ""),
                "ExchangeID": str(exchange_id or ""),
            },
            timeout,
        )

    def query_settlement_confirmation_result(self, timeout=5) -> QueryResult[Any]:
        field = CThostFtdcQrySettlementInfoConfirmField()
        field.BrokerID = self._bound_broker_id
        field.InvestorID = self._bound_user_id
        method = (
            getattr(self._api, "ReqQrySettlementInfoConfirm", None)
            if self._api
            else None
        )
        return self._execute_query(
            "settlement_confirmation",
            (
                (lambda request_id: method(field, request_id))
                if callable(method)
                else None
            ),
            timeout,
        )

    def verify_settlement_confirmation(self, timeout=5) -> QueryResult[Any]:
        """Read server confirmation and promote this matching session to trading ready."""
        with self._query_state_lock:
            expected_generation = self._connection_generation
            expected_fingerprint = self._account_fingerprint
            expected_trading_day = self._trading_day
            self._clear_settlement_readback_locked(
                "ctp_execution_gate_settlement_readback_refresh"
            )
        result = self.query_settlement_confirmation_result(timeout=timeout)
        failure_reason = ""
        if not result.complete:
            failure_reason = "ctp_execution_gate_settlement_readback_incomplete"
        elif result.connection_generation != expected_generation:
            failure_reason = "ctp_execution_gate_settlement_readback_stale_generation"
        elif result.account_fingerprint != expected_fingerprint:
            failure_reason = "ctp_execution_gate_settlement_readback_wrong_account"

        matched = False
        for record in result.records if not failure_reason else ():
            if isinstance(record, dict):
                getter = record.get
            else:

                def getter(name: str, default: Any = "") -> Any:
                    return getattr(record, name, default)

            broker_id = str(getter("BrokerID", "") or "")
            investor_id = str(getter("InvestorID", "") or "")
            confirmation_day = str(
                getter("TradingDay", "") or getter("ConfirmDate", "") or ""
            )
            if broker_id != self._bound_broker_id:
                continue
            if investor_id != self._bound_user_id:
                continue
            if not confirmation_day or confirmation_day != expected_trading_day:
                continue
            matched = True
            break

        if not failure_reason and not matched:
            failure_reason = "ctp_execution_gate_settlement_readback_identity_mismatch"

        with self._query_state_lock:
            if not failure_reason and (
                self._connection_generation != expected_generation
                or self._account_fingerprint != expected_fingerprint
                or self._trading_day != expected_trading_day
            ):
                failure_reason = (
                    "ctp_execution_gate_settlement_readback_stale_generation"
                )
            if not failure_reason and not self.is_read_only_ready:
                failure_reason = (
                    "ctp_execution_gate_settlement_readback_session_not_ready"
                )
            if failure_reason:
                self._clear_settlement_readback_locked(
                    failure_reason,
                    request_id=result.request_id,
                )
            else:
                self._settlement_state = "confirmed"
                self._settlement_trading_day = expected_trading_day
                self._settlement_connection_generation = expected_generation
                self._settlement_account_fingerprint = expected_fingerprint
                self._settlement_proof_source = "confirmation_query"
                self._settlement_proof_query_request_id = result.request_id
                self._settlement_readback_verified = True
                self._ready = True
                self._last_session_error = {}
        return result

    def next_order_ref(self) -> str:
        """Return the next monotonic CTP OrderRef.

        CTP expects OrderRef to be unique and increasing during a session.
        """
        with self._order_ref_lock:
            self._max_order_ref += 1
            return str(self._max_order_ref)

    def wait_order_event(self, timeout=5):
        """Wait for the next order callback snapshot."""
        try:
            return self._order_events.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_trade_event(self, timeout=5):
        """Wait for the next trade callback snapshot."""
        try:
            return self._trade_events.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait_error_event(self, timeout=5):
        """Wait for the next error callback snapshot."""
        try:
            return self._error_events.get(timeout=timeout)
        except queue.Empty:
            return None

    def _push_order_event(self, order_field) -> None:
        snapshot = _snapshot_ctp_field(order_field)
        if snapshot:
            self._order_events.put(snapshot)
        if self.on_order:
            self.on_order(order_field)

    def _push_trade_event(self, trade_field) -> None:
        snapshot = _snapshot_ctp_field(trade_field)
        if snapshot:
            self._trade_events.put(snapshot)
        if self.on_trade:
            self.on_trade(trade_field)

    def _push_error_event(
        self, event_type, rsp_info=None, field=None, request_id=None
    ) -> None:
        payload = {
            "event": event_type,
            "request_id": request_id,
            "error_id": getattr(rsp_info, "ErrorID", 0) if rsp_info is not None else 0,
            "error_msg": (
                getattr(rsp_info, "ErrorMsg", "") if rsp_info is not None else ""
            ),
            "field": _snapshot_ctp_field(field),
        }
        self._error_events.put(payload)
        if self.on_error and rsp_info is not None:
            self.on_error(rsp_info)

    @property
    def api(self):
        """Return the stable public view of the current native trader API."""
        return self._api_view

    def stop(self):
        """Stop a CTP trader session without freeing a live SWIG director.

        The vendor macOS framework is unsafe if ``Release()`` races a live
        ``Join()``.  Detach the native callback first, then retain the API,
        director and Join thread until Join returns.  Once it has returned,
        the Join observer releases the retained session immediately.
        """

        # Set this before waiting for a native registration call's lock.  It
        # is the post-call fence that prevents later startup calls when stop
        # races RegisterSpi on another thread.
        self._startup_cancel_event.set()
        with self._query_state_lock:
            # A stop issued while CreateFtdc* is still running must cancel the
            # reserved generation before start() can register it.
            self._lifecycle_generation += 1
            self._starting_generation = None
            self._on_front_disconnected("client_stop")
            api = self._api
            spi = self._spi
            join_thread = self._thread
            native_may_be_live = self._native_init_started or self._join_active
            join_active = native_may_be_live and (
                self._join_active
                or (join_thread is not None and join_thread.is_alive())
            )
            if api is None:
                return
            if join_active:
                _retain_live_ctp_native_session(api, spi, join_thread)
            self._api = None
            self._thread = None
            self._join_active = False
            self._native_init_started = False

        if join_active:
            # RegisterSpi(None) is the vendor's documented callback
            # registration API; retaining ``spi`` above also protects a
            # callback already in flight while the registration is changed.
            with suppress(Exception):
                api.RegisterSpi(None)
            return

        with suppress(Exception):
            api.RegisterSpi(None)
            api.Release()

    @property
    def is_ready(self):
        if self.auto_settlement_confirm:
            return self.is_trading_ready
        return self.is_read_only_ready

    @property
    def is_read_only_ready(self):
        return (
            self._connected
            and self._authentication_state == "authenticated"
            and self._login_state == "logged_in"
            # Read-only discovery remains available with an offline/test
            # native API.  The stronger active-front check is enforced only
            # at arm, settlement, and native order final gates.
            and self._bound_identity_is_current(require_active_front=False)
        )

    @property
    def is_trading_ready(self):
        with self._query_state_lock:
            return (
                self.is_read_only_ready
                and self._settlement_state == "confirmed"
                and self._ready
                and self._has_current_settlement_readback_locked()
            )
