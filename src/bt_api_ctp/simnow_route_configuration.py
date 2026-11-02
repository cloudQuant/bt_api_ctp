"""CTP-owned SimNow profile parsing and exact-route configuration factory."""

from __future__ import annotations

import datetime as _datetime
import os
import socket
import stat
import statistics
import time
from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from urllib.parse import urlsplit
from weakref import ReferenceType, WeakKeyDictionary, ref

from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_base.provider_configuration_protocol import (
    ProviderConfiguration,
    ProviderConfigurationError,
    ProviderConfigurationHandle,
    ProviderConfigurationRequest,
)


CTP_FUTURE_ROUTE = "CTP___FUTURE"
_MAX_CONFIG_BYTES = 1024 * 1024
_FRONT_PROBE_SAMPLES = 3
_FRONT_PROBE_TIMEOUT_SECONDS = 3.0
_DEFAULT_CREDENTIAL_NAMES = {
    "app_id": "CTP_APP_ID",
    "auth_code": "CTP_AUTH_CODE",
    "broker_id": "CTP_BROKER_ID",
    "investor_id": "CTP_USER_ID",
    "password": "CTP_PASSWORD",
}
_CREDENTIAL_FIELDS = ("broker_id", "investor_id", "password", "app_id", "auth_code")
_BEIJING = _datetime.timezone(_datetime.timedelta(hours=8))
_CZCE_SESSIONS = (("09:00", "10:15"), ("10:30", "11:30"), ("13:30", "15:00"))
_CONFIGURATION_VAULT_LOCK = RLock()
_CONFIGURATION_VAULT: WeakKeyDictionary[
    ProviderConfigurationHandle,
    tuple[object, ReferenceType[ProviderConfiguration]],
] = WeakKeyDictionary()


def register_ctp_provider_configuration(registry: object) -> None:
    """Register only the exact CTP futures route configuration capability."""

    register = getattr(registry, "register", None)
    if not callable(register):
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_REQUEST_INVALID")
    register(CTP_FUTURE_ROUTE, _configure_ctp_route)


def _configure_ctp_route(request: ProviderConfigurationRequest) -> ProviderConfiguration:
    if type(request) is not ProviderConfigurationRequest or (
        request.exchange_name != CTP_FUTURE_ROUTE
    ):
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_ROUTE_INVALID")
    if request.config_path is None or request.selection is not None:
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_INPUT_UNAVAILABLE")
    block = _read_ctp_block(request.config_path)
    instrument_id = _required_config_text(block, "instrument_id").upper()
    venue = _required_config_text(block, "exchange_id").upper()
    selected = _select_configured_fronts(
        _configured_pairs(block),
        samples=_FRONT_PROBE_SAMPLES,
        timeout=_FRONT_PROBE_TIMEOUT_SECONDS,
    )
    credential_names, credential_values = _resolve_credentials(request, block)
    profile_ref = _profile_for_pair(selected["td_front"], selected["md_front"])
    instrument = MarketInstrumentKey(
        provider="ctp",
        venue=venue,
        environment="simulation",
        product_type="future",
        instrument_id=instrument_id,
        native_symbol=instrument_id,
    )
    provider_options = {
        "environment": "simulation",
        "ctp_environment": "simnow",
        "td_front": selected["td_front"],
        "md_front": selected["md_front"],
        "auto_settlement_confirm": False,
        "supports_dual_side": True,
    }
    profile = _official_profile_for_pair(selected["td_front"], selected["md_front"])
    if profile:
        provider_options["ctp_env_profile"] = profile
    handle = ProviderConfigurationHandle()
    sealed_configuration = _CtpProviderConfigurationPayload(
        credential_values={
            field_name: credential_values[credential_names[field_name]]
            for field_name in _CREDENTIAL_FIELDS
        },
        provider_options=provider_options,
    )
    configuration = ProviderConfiguration(
        exchange_name=CTP_FUTURE_ROUTE,
        environment="simulation",
        profile_ref=profile_ref,
        instrument=instrument,
        selection_receipt=selected["receipt"],
        provider_configuration_handle=handle,
    )
    with _CONFIGURATION_VAULT_LOCK:
        _CONFIGURATION_VAULT[handle] = (sealed_configuration, ref(configuration))
    return configuration


class _CtpProviderConfigurationPayload:
    """Provider-private sealed payload retained behind an opaque handle."""

    __slots__ = ("credential_values", "provider_options")

    def __init__(
        self,
        *,
        credential_values: Mapping[str, str],
        provider_options: Mapping[str, object],
    ) -> None:
        self.credential_values = MappingProxyType(dict(credential_values))
        self.provider_options = MappingProxyType(dict(provider_options))

    def __repr__(self) -> str:
        return "<sealed CTP provider configuration>"


def _consume_ctp_provider_configuration(
    configuration: ProviderConfiguration,
) -> _CtpProviderConfigurationPayload:
    """Consume one exact CTP configuration inside the CTP runtime owner.

    This private provider function is the only value-bearing handoff.  BT and
    generic SDK facades pass the opaque handle without resolving it.
    """

    if (
        type(configuration) is not ProviderConfiguration
        or configuration.exchange_name != CTP_FUTURE_ROUTE
        or type(configuration.provider_configuration_handle)
        is not ProviderConfigurationHandle
    ):
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_RESULT_INVALID")
    with _CONFIGURATION_VAULT_LOCK:
        entry = _CONFIGURATION_VAULT.get(configuration.provider_configuration_handle)
        if entry is None or entry[1]() is not configuration:
            raise ProviderConfigurationError("PROVIDER_CONFIGURATION_RESULT_INVALID")
        _CONFIGURATION_VAULT.pop(configuration.provider_configuration_handle, None)
        payload = entry[0]
    if type(payload) is not _CtpProviderConfigurationPayload:
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_CAPABILITY_UNAVAILABLE")
    return payload


def _read_ctp_block(path: Path) -> Mapping[str, object]:
    try:
        raw = _read_bounded_regular_file(path, _MAX_CONFIG_BYTES)
        import yaml

        document = yaml.safe_load(raw.decode("utf-8-sig", errors="strict"))
    except ProviderConfigurationError:
        raise
    except Exception:
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED") from None
    if not isinstance(document, Mapping):
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
    block = document.get("ctp")
    if not isinstance(block, Mapping):
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
    return block


def _required_config_text(block: Mapping[str, object], name: str) -> str:
    value = block.get(name)
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
    return value


def _configured_pairs(block: Mapping[str, object]) -> tuple[dict[str, str], ...]:
    raw_pairs = block.get("front_pairs")
    if raw_pairs is None:
        raw_pairs = ({"md_front": block.get("md_front"), "td_front": block.get("td_front")},)
    if type(raw_pairs) not in (tuple, list) or not 1 <= len(raw_pairs) <= 8:
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
    pairs: list[dict[str, str]] = []
    for item in raw_pairs:
        if not isinstance(item, Mapping):
            raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
        md_front, td_front = item.get("md_front"), item.get("td_front")
        kind = item.get("kind", "standard")
        if any(
            type(value) is not str or not value.strip()
            for value in (md_front, td_front)
        ):
            raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
        if type(kind) is not str or not kind.strip() or kind != kind.strip():
            raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
        md_front = md_front.strip()
        td_front = td_front.strip()
        _endpoint(md_front)
        _endpoint(td_front)
        pairs.append({"md_front": md_front, "td_front": td_front, "kind": kind.lower()})
    return tuple(pairs)


def _resolve_credentials(
    request: ProviderConfigurationRequest, block: Mapping[str, object]
) -> tuple[dict[str, str], dict[str, str]]:
    configured_names = dict(request.credential_names)
    if "user_id" in configured_names:
        if "investor_id" in configured_names:
            raise ProviderConfigurationError("PROVIDER_CONFIGURATION_REQUEST_INVALID")
        configured_names["investor_id"] = configured_names.pop("user_id")
    if set(configured_names) - set(_CREDENTIAL_FIELDS):
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_REQUEST_INVALID")
    allowed_names = set(_DEFAULT_CREDENTIAL_NAMES.values()) | set(configured_names.values())
    file_values = _read_credential_env_file(request.credential_env_file, allowed_names)
    names: dict[str, str] = {}
    values: dict[str, str] = {}
    aliases = {"investor_id": ("investor_id", "user_id")}
    for field_name in _CREDENTIAL_FIELDS:
        credential_name = configured_names.get(
            field_name, _DEFAULT_CREDENTIAL_NAMES[field_name]
        )
        if type(credential_name) is not str or not credential_name.strip():
            raise ProviderConfigurationError("PROVIDER_CONFIGURATION_REQUEST_INVALID")
        name = credential_name.strip()
        value = (
            request.credential_values.get(name)
            or file_values.get(name)
            or os.environ.get(name)
            or None
        )
        if field_name in {"broker_id", "investor_id"} and value is None:
            for alias in aliases.get(field_name, (field_name,)):
                candidate = block.get(alias)
                if type(candidate) is str and candidate:
                    value = candidate
                    break
        if not value:
            for alias in (() if field_name in {"broker_id", "investor_id"} else aliases.get(field_name, (field_name,))):
                candidate = block.get(alias)
                if type(candidate) is str and candidate:
                    value = candidate
                    break
        if type(value) is not str or not value:
            raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
        if len(value.encode("utf-8", errors="strict")) > 4096:
            raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
        names[field_name] = name
        values[name] = value
    return names, values


def _read_credential_env_file(
    path: Path | None,
    allowed_names: set[str],
) -> dict[str, str]:
    """Read only provider-selected names from one bounded regular file.

    The payload is held only inside the provider configuration module. Unknown
    keys are discarded, duplicate allowed keys fail closed, and no values or
    file content enter an exception, repr, receipt, or log.
    """

    if path is None:
        return {}
    try:
        os.lstat(os.fspath(path))
    except FileNotFoundError:
        # Preserve the supported process-environment fallback when the
        # explicitly named optional env file has not been created.
        return {}
    except Exception:
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED") from None
    raw = _read_bounded_regular_file(path, 64 * 1024)
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED") from None
    values: dict[str, str] = {}
    try:
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if key not in allowed_names:
                continue
            if key in values:
                raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
            value = value.strip()
            if len(value.encode("utf-8", errors="strict")) > 4096:
                raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value:
                values[key] = value
    except ProviderConfigurationError:
        raise
    except Exception:
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED") from None
    return values


def _read_bounded_regular_file(path: Path, maximum: int) -> bytes:
    """Read a stable bounded regular file without following reparse points."""

    descriptor = None
    try:
        raw_path = os.fspath(path)
        before_path = os.lstat(raw_path)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISREG(before_path.st_mode)
            or getattr(before_path, "st_file_attributes", 0) & reparse_flag
            or before_path.st_size < 0
            or before_path.st_size > maximum
        ):
            raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(raw_path, flags)
        opened = os.fstat(descriptor)
        after_open_path = os.lstat(raw_path)
        if not _same_file_identity(before_path, opened) or not _same_file_identity(
            opened, after_open_path
        ):
            raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
        if (
            not stat.S_ISREG(opened.st_mode)
            or getattr(opened, "st_file_attributes", 0) & reparse_flag
            or opened.st_size < 0
            or opened.st_size > maximum
        ):
            raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after_read = os.fstat(descriptor)
        final_path = os.lstat(raw_path)
        if (
            len(content) > maximum
            or len(content) != opened.st_size
            or not _same_file_identity(opened, after_read)
            or not _same_file_identity(after_read, final_path)
        ):
            raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
        return content
    except ProviderConfigurationError:
        raise
    except Exception:
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and getattr(left, "st_mtime_ns", None) == getattr(right, "st_mtime_ns", None)
        and getattr(left, "st_file_attributes", 0) == getattr(right, "st_file_attributes", 0)
    )


def _endpoint(value: str) -> tuple[str, int]:
    text = value.strip()
    if "://" not in text:
        text = "tcp://" + text
    parsed = None
    try:
        parsed = urlsplit(text)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        host, port = None, None
    if (
        parsed is None
        or parsed.scheme != "tcp"
        or not host
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")
    return host, port


def _select_configured_fronts(
    pairs: tuple[dict[str, str], ...], *, samples: int, timeout: float
) -> dict[str, object]:
    from bt_api_ctp.ctp_env_selector import (
        official_simnow_profile_for_td_front,
        registered_broker_sim_profile_for_td_front,
    )

    candidates: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for index, pair in enumerate(pairs):
        details: dict[str, object] = {"index": index, "kind": pair["kind"]}
        md_samples = _probe_endpoint(pair["md_front"], samples=samples, timeout=timeout)
        td_samples = _probe_endpoint(pair["td_front"], samples=samples, timeout=timeout)
        details["md_success"] = sum(value is not None for value in md_samples)
        details["td_success"] = sum(value is not None for value in td_samples)
        md_median = _median_success(md_samples)
        td_median = _median_success(td_samples)
        if md_median is None or td_median is None:
            details["reason"] = "no_whole_pair_connection"
            rejected.append(details)
            continue
        try:
            whitelisted: bool | None = bool(
                official_simnow_profile_for_td_front(pair["td_front"])
                or registered_broker_sim_profile_for_td_front(pair["td_front"])
            )
        except Exception:
            whitelisted = None
        candidates.append(
            {
                **details,
                "md_front": pair["md_front"],
                "td_front": pair["td_front"],
                "whitelisted": whitelisted,
                "score_ms": round(max(md_median, td_median), 3),
                "md_median_ms": round(md_median, 3),
                "td_median_ms": round(td_median, 3),
            }
        )
    if not candidates:
        raise ProviderConfigurationError("PROVIDER_CONFIGURATION_FAILED")

    in_session = _in_czce_session()

    def _session_rank(item: Mapping[str, object]) -> int:
        kind = str(item["kind"]).lower()
        is_7x24 = kind in {"7x24", "engineering", "simnow_7x24"}
        return 0 if (in_session != is_7x24) else 1

    def _whitelist_rank(item: Mapping[str, object]) -> int:
        return {True: 0, None: 1, False: 2}[item["whitelisted"]]

    candidates.sort(
        key=lambda item: (
            _session_rank(item),
            _whitelist_rank(item),
            item["score_ms"],
            item["index"],
        )
    )
    chosen = candidates[0]
    receipt = {
        "selected_index": chosen["index"],
        "selected_kind": chosen["kind"],
        "selected_whitelisted": chosen["whitelisted"],
        "score_ms": chosen["score_ms"],
        "md_median_ms": chosen["md_median_ms"],
        "td_median_ms": chosen["td_median_ms"],
        "md_success": chosen["md_success"],
        "td_success": chosen["td_success"],
        "samples": samples,
        "eligible": [
            {
                key: item[key]
                for key in (
                    "index", "kind", "whitelisted", "score_ms", "md_success", "td_success"
                )
            }
            for item in candidates
        ],
        "rejected": rejected,
        "environment_switched_after_write": False,
    }
    return {"td_front": chosen["td_front"], "md_front": chosen["md_front"], "receipt": receipt}


def _probe_endpoint(value: str, *, samples: int, timeout: float) -> tuple[float | None, ...]:
    endpoint = _endpoint(value)
    observations: list[float | None] = []
    for _ in range(samples):
        started = time.monotonic()
        try:
            with socket.create_connection(endpoint, timeout=timeout):
                observations.append(round((time.monotonic() - started) * 1000.0, 3))
        except OSError:
            observations.append(None)
    return tuple(observations)


def _median_success(samples: tuple[float | None, ...]) -> float | None:
    good = tuple(value for value in samples if value is not None)
    return float(statistics.median(good)) if good else None


def _in_czce_session() -> bool:
    moment = _datetime.datetime.now(_datetime.timezone.utc).astimezone(_BEIJING)
    for start_text, end_text in _CZCE_SESSIONS:
        start_hour, start_minute = (int(part) for part in start_text.split(":"))
        end_hour, end_minute = (int(part) for part in end_text.split(":"))
        start = moment.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        end = moment.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        if start <= moment < end:
            return True
    return False


def _official_profile_for_pair(td_front: str, md_front: str) -> str:
    try:
        from bt_api_ctp.ctp_env_selector import (
            official_simnow_profile_for_td_front,
            registered_broker_sim_profile_for_td_front,
            verify_official_simnow_profile,
            verify_registered_broker_sim_profile,
        )

        official = official_simnow_profile_for_td_front(td_front)
        if official and verify_official_simnow_profile(td_front, md_front, official):
            return official
        registered = registered_broker_sim_profile_for_td_front(td_front)
        if registered and verify_registered_broker_sim_profile(td_front, md_front, registered):
            return registered
    except Exception:
        return ""
    return ""


def _profile_for_pair(td_front: str, md_front: str) -> str:
    profile = _official_profile_for_pair(td_front, md_front)
    return profile or "custom_simnow"


__all__ = ["CTP_FUTURE_ROUTE", "register_ctp_provider_configuration"]
