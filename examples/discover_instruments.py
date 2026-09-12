#!/usr/bin/env python
"""Export the authenticated SimNow-visible instrument universe without writes.

Only the packaged public TraderClient query APIs are used. Completion describes
the counter's visible query result, not all contracts offered by an exchange.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import ctypes
import hashlib
import json
import math
import os
import re
import shlex
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROFILES = ("set1_group1", "set1_group1_vpn", "set1_group2")
WRITE_KEYS = ("settlement_confirm", "order_insert", "order_action")
CREDENTIAL_KEYS = {
    "broker_id": ("CTP_BROKER_ID", "SIMNOW_BROKER_ID", "simnow_broker_id"),
    "user_id": ("CTP_USER_ID", "CTP_INVESTOR_ID", "SIMNOW_USER_ID",
                "SIMNOW_INVESTOR_ID", "simnow_account", "simnow_user_id"),
    "password": ("CTP_PASSWORD", "SIMNOW_PASSWORD", "simnow_password"),
    "app_id": ("CTP_APP_ID", "SIMNOW_APP_ID", "simnow_app_id"),
    "auth_code": ("CTP_AUTH_CODE", "SIMNOW_AUTH_CODE", "simnow_auth_code"),
}
PRIVATE_FIELD_PARTS = ("account", "investor", "broker", "password", "authcode",
                       "appid", "userid", "username", "secret", "token")


class DiscoveryBlocked(RuntimeError):
    """A local stable reason that is safe to export without native error text."""


def _positive_timeout(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > 3600:
        raise argparse.ArgumentTypeError("timeout must be finite and in (0, 3600]")
    return number


def _instrument_filter(value):
    exchange, separator, instrument = value.strip().partition(":")
    exchange = exchange.upper()
    if (not separator or exchange not in {"CFFEX", "CZCE", "DCE", "GFEX", "INE", "SHFE"}
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", instrument) is None):
        raise argparse.ArgumentTypeError("instrument filter must be EXCHANGE:INSTRUMENT")
    return exchange, instrument


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="New directory; existing directories are never overwritten")
    parser.add_argument("--env-file", type=Path, help="Only this explicitly named env file is read")
    parser.add_argument("--profile", choices=PROFILES, default="set1_group1")
    parser.add_argument("--query-timeout", type=_positive_timeout, default=120.0)
    parser.add_argument("--login-timeout", type=_positive_timeout, default=30.0)
    parser.add_argument("--probe-timeout", type=_positive_timeout, default=3.0)
    parser.add_argument("--include-depth", action="store_true",
                        help="Also issue one complete visible depth-snapshot query")
    parser.add_argument("--instrument-filter", action="append", type=_instrument_filter,
                        default=[], metavar="EXCHANGE:INSTRUMENT",
                        help="Repeatable counter filter (may prefix-match); omitted means all visible")
    return parser


def _credentials(env_file):
    file_values = {}
    if env_file is not None:
        # Parse only explicit assignments; never execute shell substitutions,
        # expand ${...}, or search parent directories for another .env file.
        try:
            lines = Path(env_file).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            raise DiscoveryBlocked("env_file_unreadable") from None
        for line in lines:
            try:
                tokens = shlex.split(line, comments=True, posix=True)
            except ValueError:
                raise DiscoveryBlocked("invalid_env_file") from None
            if tokens[:1] == ["export"]:
                tokens = tokens[1:]
            if not tokens:
                continue
            if len(tokens) != 1 or "=" not in tokens[0]:
                raise DiscoveryBlocked("invalid_env_file")
            key, value = tokens[0].split("=", 1)
            file_values[key] = value
    values = {}
    for name, aliases in CREDENTIAL_KEYS.items():
        # Process environment wins even when it uses a different supported alias.
        for source in (os.environ, file_values):
            value = next((source[key] for key in aliases if source.get(key)), None)
            if value is not None:
                values[name] = value
                break
    if any(not values.get(key) for key in ("broker_id", "user_id", "password")):
        raise DiscoveryBlocked("credentials_missing")
    return values


@contextlib.contextmanager
def _quiet_native():
    """Suppress Python and native callback output for this standalone process.

    Exceptions are exported by type only. File-descriptor redirection also
    catches C/C++ printf output, which Python redirect_stdout cannot intercept.
    """
    saved = []
    with open(os.devnull, "w", encoding="utf-8") as sink:
        try:
            for stream in (sys.stdout, sys.stderr):
                with contextlib.suppress(Exception):
                    stream.flush()
            for descriptor in (1, 2):
                saved.append((descriptor, os.dup(descriptor)))
                os.dup2(sink.fileno(), descriptor)
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                yield
        finally:
            # C stdio may still be buffered even after Python streams flush.
            with contextlib.suppress(Exception):
                ctypes.CDLL("msvcrt" if os.name == "nt" else None).fflush(None)
            for descriptor, duplicate in saved:
                os.dup2(duplicate, descriptor)
                os.close(duplicate)


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _public_record(record, *, native_only=False):
    """Keep instrument/depth facts while excluding any accidental account data."""
    output = {}
    for key, value in dict(record).items():
        if not isinstance(key, str):
            continue
        compact = key.lower().replace("_", "")
        if any(part in compact for part in PRIVATE_FIELD_PARTS):
            continue
        if native_only and (not key or not key[0].isupper()):
            continue
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError:
                value = value.decode("gb18030", errors="replace")
            value = value.rstrip("\x00")
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        if value is None or isinstance(value, (str, int, float, bool)):
            output[key] = value
    return output


def _session_identity(session):
    return {
        "account_fingerprint": session.get("account_fingerprint"),
        "connection_generation": session.get("connection_generation"),
        "trading_day": session.get("trading_day"),
    }


def _valid_session(session):
    fingerprint = str(session.get("account_fingerprint") or "")
    day = str(session.get("trading_day") or "")
    generation = session.get("connection_generation")
    return (
        session.get("read_only_ready") is True
        and session.get("auto_settlement_confirm") is False
        and type(generation) is int and generation > 0
        and len(fingerprint) == 16 and all(c in "0123456789abcdef" for c in fingerprint)
        and len(day) == 8 and day.isdigit()
    )


def _zero_writes(counts):
    return all(type(counts.get(key)) is int and counts[key] == 0 for key in WRITE_KEYS)


def _query_evidence(result):
    # ErrorMessage may contain a native log fragment or account identifier.
    evidence = result.as_dict(include_records=False)
    evidence.pop("error_message", None)
    return evidence


def _query_complete(result, session):
    return (
        result.complete is True and result.is_last_seen is True
        and result.timed_out is False and result.unsupported is False
        and result.error_code in (None, 0)
        and result.connection_generation == session["connection_generation"]
        and result.account_fingerprint == session["account_fingerprint"]
    )


def _write_json(path, payload):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_discovery(args, *, client_factory=None, selector=None, diagnostics_provider=None):
    """Run one bounded read-only query session and save complete or partial evidence."""
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    requested_filters = getattr(args, "instrument_filter", [])
    scopes = [{"exchange_id": exchange, "instrument_id": instrument, "product_id": ""}
              for exchange, instrument in requested_filters] or [
                  {"exchange_id": "", "instrument_id": "", "product_id": ""}]
    report = {
        "schema_version": "bt_api_ctp.instrument-discovery.v1",
        "status": "PARTIAL", "complete": False,
        "scope": ("filtered_visible_universe" if requested_filters
                  else "authenticated_counter_visible_universe"),
        "requested_profile": args.profile,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "filters": scopes if requested_filters else scopes[0],
        "selected_coverage": [],
        "filter_semantics": "counter-defined; may prefix-match futures and options",
        "deduplication": "exchange_id/instrument_id; last returned record wins in query order",
        "errors": [], "queries": {}, "session": {},
        "read_only_proof": {"before": {}, "after": {}, "zero_writes": False},
        "provenance": {"example_sha256": _sha256(__file__)},
    }
    client = None
    instruments = []
    raw_instruments = []
    depth = []
    instrument_complete = False
    depth_complete = not args.include_depth
    _write_json(output / "discovery.json", report)

    def checkpoint(phase, record_count=None):
        report["last_phase"] = phase
        try:
            counts = client.get_request_counts() if client is not None else {}
        except Exception:
            counts = {}
        _write_json(output / "progress.json", {
            "phase": phase, "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "record_count": record_count,
            "filter": report.get("active_filter"),
            "write_counts": {key: counts.get(key) for key in WRITE_KEYS},
        })

    checkpoint("initializing")
    with _quiet_native():
        try:
            import bt_api_ctp
            from bt_api_ctp import instrument as instrument_module

            credentials = _credentials(args.env_file)
            if args.profile not in PROFILES:
                raise ValueError("invalid_profile")
            if selector is None:
                from bt_api_ctp.ctp_env_selector import select_reachable_ctp_environment
                selector = select_reachable_ctp_environment
            if client_factory is None or diagnostics_provider is None:
                from bt_api_ctp.ctp.client import TraderClient, get_ctp_native_diagnostics
                client_factory = client_factory or TraderClient
                diagnostics_provider = diagnostics_provider or get_ctp_native_diagnostics
            normalize = instrument_module.normalize_ctp_instrument
            diagnostics = diagnostics_provider()
            report["provenance"].update({
                "package_version": bt_api_ctp.__version__,
                "package_path": str(Path(bt_api_ctp.__file__).resolve()),
                "normalizer_sha256": _sha256(instrument_module.__file__),
                "record_source": "TraderClient.query_instruments_result/CTP InstrumentField",
                "raw_export_policy": "native public fields; decoded bytes; nonfinite numbers=null",
                "native": {key: diagnostics.get(key) for key in (
                    "native_loaded", "runtime_source", "loaded_module_path",
                    "loaded_module_sha256", "native_module_paths", "native_module_sha256",
                    "ctp_package_sha256", "api_version", "trader_login_abi_verified",
                    "trader_login_abi_reason")},
            })
            if diagnostics.get("native_loaded") is not True:
                raise DiscoveryBlocked("native_unavailable")
            if diagnostics.get("trader_login_abi_verified") is False:
                raise DiscoveryBlocked("ctp_trader_login_abi_unverified")
            # Older public diagnostics do not expose this optional capability.
            # In that case retain the public client's guarded login path.
            try:
                selection = selector(env="set1", profile=args.profile,
                                     require_profile=args.profile, timeout=args.probe_timeout)
            except (ValueError, RuntimeError):
                raise DiscoveryBlocked("profile_selection_failed") from None
            if selection.environment != "simnow" or selection.profile != args.profile:
                raise DiscoveryBlocked("unfrozen_environment")
            report["selected_profile"] = selection.profile
            checkpoint("selected_profile")
            client = client_factory(front=selection.td_front, **credentials,
                                    auto_settlement_confirm=False)
            report["read_only_proof"]["before"] = client.get_request_counts()
            if not _zero_writes(report["read_only_proof"]["before"]):
                raise RuntimeError("initial_write_counters_invalid")
            client.start(block=False)
            checkpoint("awaiting_read_only_login")
            deadline = time.monotonic() + args.login_timeout
            while True:
                session = client.get_session_state()
                if _valid_session(session):
                    break
                if session.get("auto_settlement_confirm") is not False:
                    raise DiscoveryBlocked("auto_settlement_confirm_not_disabled")
                if session.get("login_state") == "failed":
                    detail = (session.get("last_error") or {}).get("detail")
                    reason = ("ctp_trader_login_abi_unverified"
                              if detail == "ctp_trader_login_abi_unverified"
                              else "read_only_login_failed")
                    raise DiscoveryBlocked(reason)
                if session.get("auth_state") == "failed":
                    raise DiscoveryBlocked("read_only_authentication_failed")
                if time.monotonic() >= deadline:
                    raise DiscoveryBlocked("read_only_login_timeout")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            report["session"] = _session_identity(session)
            checkpoint("read_only_ready")
            instrument_union = {}
            depth_union = {}
            instrument_complete = True
            depth_complete = True
            for index, scope in enumerate(scopes):
                coverage = {"filter": dict(scope), "complete": False}
                report["selected_coverage"].append(coverage)
                report["active_filter"] = dict(scope)
                checkpoint("querying_instruments", len(instrument_union))
                queried = client.query_instruments_result(**scope, timeout=args.query_timeout)
                instrument_key = f"instruments:{index}" if requested_filters else "instruments"
                evidence = _query_evidence(queried)
                evidence["filter"] = dict(scope)
                report["queries"][instrument_key] = evidence
                coverage["instruments_query_key"] = instrument_key
                coverage["instrument_rows"] = len(queried.records)
                for record in queried.records:
                    row = _public_record(record, native_only=True)
                    key = (row.get("ExchangeID"), row.get("InstrumentID"))
                    if not all(key):
                        report["errors"].append("instrument_identity_missing")
                    instrument_union[key] = row
                raw_instruments = list(instrument_union.values())
                instruments = [_public_record(normalize(row)) for row in raw_instruments]
                checkpoint("instruments_query_finished", len(raw_instruments))
                current_complete = _query_complete(queried, session)
                instrument_complete = instrument_complete and current_complete
                if not current_complete:
                    report["errors"].append("instrument_query_incomplete_or_identity_mismatch")
                current_depth_complete = not args.include_depth
                if args.include_depth and current_complete:
                    checkpoint("querying_depth", len(depth_union))
                    queried_depth = client.query_depth_market_data_result(
                        instrument_id=scope["instrument_id"], exchange_id=scope["exchange_id"],
                        timeout=args.query_timeout)
                    depth_key = f"depth_market_data:{index}" if requested_filters else "depth_market_data"
                    depth_evidence = _query_evidence(queried_depth)
                    depth_evidence["filter"] = {key: scope[key] for key in ("instrument_id", "exchange_id")}
                    report["queries"][depth_key] = depth_evidence
                    coverage["depth_query_key"] = depth_key
                    coverage["depth_rows"] = len(queried_depth.records)
                    for record in queried_depth.records:
                        row = _public_record(record, native_only=True)
                        key = (row.get("ExchangeID"), row.get("InstrumentID"))
                        if not all(key):
                            report["errors"].append("depth_identity_missing")
                        depth_union[key] = row
                    depth = list(depth_union.values())
                    checkpoint("depth_query_finished", len(depth))
                    current_depth_complete = _query_complete(queried_depth, session)
                    if not current_depth_complete:
                        report["errors"].append("depth_query_incomplete_or_identity_mismatch")
                depth_complete = depth_complete and current_depth_complete
                coverage["complete"] = current_complete and current_depth_complete
                current_session = client.get_session_state()
                if (not _valid_session(current_session)
                        or _session_identity(current_session) != report["session"]):
                    report["errors"].append("session_identity_changed")
                    instrument_complete = False
                    break
            final_session = client.get_session_state()
            if not _valid_session(final_session) or _session_identity(final_session) != report["session"]:
                report["errors"].append("session_identity_changed")
        except DiscoveryBlocked as exc:
            report["status"] = "BLOCKED"
            report["errors"].append(str(exc))
        except (Exception, KeyboardInterrupt) as exc:
            # Never persist str(exc): native exceptions may contain credentials.
            report["errors"].append("interrupted" if isinstance(exc, KeyboardInterrupt)
                                    else f"{report['last_phase']}_failed:{type(exc).__name__}")
        finally:
            if client is not None:
                try:
                    client.stop()
                except Exception:
                    report["errors"].append("stop_failed")
                try:
                    report["read_only_proof"]["after"] = client.get_request_counts()
                except Exception:
                    report["errors"].append("request_counts_unavailable")
    checkpoint("stop_failed" if "stop_failed" in report["errors"] else "stopped",
               len(raw_instruments))
    proof = report["read_only_proof"]
    proof["zero_writes"] = _zero_writes(proof["before"]) and _zero_writes(proof["after"])
    if not proof["zero_writes"]:
        report["errors"].append("zero_write_proof_failed")
    report["complete"] = instrument_complete and depth_complete and not report["errors"]
    if report["complete"]:
        report["status"] = ("COMPLETE_FILTERED_VISIBLE_UNIVERSE" if requested_filters
                            else "COMPLETE_VISIBLE_UNIVERSE")
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["counts"] = {
        "raw_instruments": len(raw_instruments), "instruments": len(instruments),
        "depth_market_data": len(depth),
        "by_asset_type": dict(Counter(row.get("asset_type", "unknown") for row in instruments)),
        "by_exchange": dict(Counter(row.get("exchange_id", "unknown") for row in instruments)),
    }
    _write_json(output / "raw_instruments.json", {"complete": report["complete"],
                                                  "records": raw_instruments})
    _write_json(output / "instruments.json", {"complete": report["complete"],
                                              "records": instruments})
    columns = sorted({key for row in instruments for key in row}) or ["instrument"]
    with (output / "instruments.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(instruments)
    if args.include_depth:
        _write_json(output / "depth_market_data.json", {"complete": report["complete"],
                                                         "records": depth})
    report["artifacts_sha256"] = {path.name: _sha256(path) for path in sorted(output.iterdir())
                                   if path.name != "discovery.json"}
    _write_json(output / "discovery.json", report)
    return report


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = run_discovery(args)
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({"status": "PARTIAL", "complete": False,
                          "error": type(exc).__name__}))
        return 2
    print(json.dumps({"status": report["status"], "complete": report["complete"],
                      "counts": report["counts"],
                      "zero_writes": report["read_only_proof"]["zero_writes"]},
                     ensure_ascii=False))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
