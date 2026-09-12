#!/usr/bin/env python
"""Collect account-specific CTP pair costs through one bounded read-only session.

Discovery quotes are historical TD snapshots, never live execution evidence.
The budget and reserve columns are static research scenarios, not risk admission.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import discover_instruments as discovery
    from .screen_option_pairs import estimate_buy_option_pair
except ImportError:
    import discover_instruments as discovery
    from screen_option_pairs import estimate_buy_option_pair


def _number(value, *, positive=False):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return (
        number
        if math.isfinite(number) and 0 <= number < 1e308 and (not positive or number > 0)
        else None
    )


def _positive_int(value):
    result = int(value)
    if not 1 <= result <= 100:
        raise argparse.ArgumentTypeError("must be in [1,100]")
    return result


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--profile", required=True, choices=discovery.PROFILES)
    parser.add_argument("--max-products", type=_positive_int, default=15)
    parser.add_argument("--query-timeout", type=discovery._positive_timeout, default=6.0)
    parser.add_argument("--login-timeout", type=discovery._positive_timeout, default=30.0)
    parser.add_argument("--probe-timeout", type=discovery._positive_timeout, default=3.0)
    parser.add_argument("--query-interval", type=discovery._positive_timeout, default=1.1)
    return parser


def _key(row, *, quote=False):
    return (
        row.get("ExchangeID" if quote else "exchange_id"),
        row.get("InstrumentID" if quote else "instrument_id"),
    )


def _legal_lots(row, lots=1):
    low = _number(row.get("min_limit_order_volume"), positive=True)
    high = _number(row.get("max_limit_order_volume"), positive=True)
    return low is not None and high is not None and low <= lots <= high


def _price(quote):
    values = [
        _number(quote.get(name), positive=True)
        for name in ("AskPrice1", "LastPrice", "PreSettlementPrice")
    ]
    return max((value for value in values if value is not None), default=None)


def _has_book(quote):
    bid = _number(quote.get("BidPrice1"), positive=True)
    ask = _number(quote.get("AskPrice1"), positive=True)
    return (
        bid is not None
        and ask is not None
        and bid <= ask
        and (_number(quote.get("BidVolume1")) or 0) >= 1
        and (_number(quote.get("AskVolume1")) or 0) >= 1
    )


def load_discovery(directory, profile):
    directory = Path(directory)
    report = json.loads((directory / "discovery.json").read_text())
    if report.get("complete") is not True or not report.get("read_only_proof", {}).get(
        "zero_writes"
    ):
        raise discovery.DiscoveryBlocked("discovery_not_complete_and_read_only")
    if report.get("selected_profile") != profile:
        raise discovery.DiscoveryBlocked("discovery_profile_mismatch")
    day = report.get("session", {}).get("trading_day")
    try:
        datetime.strptime(day, "%Y%m%d")
    except (TypeError, ValueError):
        raise discovery.DiscoveryBlocked("discovery_trading_day_invalid") from None
    artifacts = report.get("artifacts_sha256", {})
    payloads = {}
    for name in ("instruments.json", "depth_market_data.json", "raw_instruments.json"):
        path = directory / name
        if name == "raw_instruments.json" and not path.exists():
            payloads[name] = []
            continue
        if not artifacts.get(name) or discovery._sha256(path) != artifacts[name]:
            raise discovery.DiscoveryBlocked("discovery_artifact_hash_mismatch")
        payload = json.loads(path.read_text())
        if payload.get("complete") is not True or not isinstance(payload.get("records"), list):
            raise discovery.DiscoveryBlocked("discovery_artifact_incomplete")
        payloads[name] = payload["records"]
    return report, payloads


def select_pairs(instruments, depth, raw_instruments, trading_day, max_products=15):
    """Select one most-active eligible future/product and a quoted ATM C/P pair."""
    quotes = {_key(row, quote=True): row for row in depth}
    raw = {_key(row, quote=True): row for row in raw_instruments}
    options = {}
    for row in instruments:
        if (
            row.get("asset_type") != "option"
            or row.get("is_trading") is not True
            or not _legal_lots(row)
            or row.get("option_type") not in ("call", "put")
            or not row.get("expiry_date")
            or row["expiry_date"] <= trading_day
            or _number(row.get("strike_price"), positive=True) is None
        ):
            continue
        group = (row.get("exchange_id"), row.get("underlying_instrument"))
        options.setdefault(group, []).append(row)
    products = {}
    for future in instruments:
        key = _key(future)
        if (
            future.get("asset_type") != "future"
            or future.get("is_trading") is not True
            or not _legal_lots(future)
            or key not in options
            or _number(future.get("multiplier"), positive=True) is None
        ):
            continue
        quote = quotes.get(key, {})
        price = _price(quote)
        volume = _number(quote.get("Volume"))
        if price is None or volume is None:
            continue
        groups = {}
        for option in options[key]:
            if _number(option.get("multiplier"), positive=True) is None:
                continue
            oquote = quotes.get(_key(option), {})
            if _price(oquote) is None:
                continue
            group = (option["expiry_date"], float(option["strike_price"]))
            by_kind = groups.setdefault(group, {})
            current = by_kind.get(option["option_type"])
            if current is None or option["instrument_id"] < current["instrument_id"]:
                by_kind[option["option_type"]] = option
        candidates = []
        for (expiry, strike), group in groups.items():
            if set(group) != {"call", "put"}:
                continue
            quality = sum(_has_book(quotes.get(_key(row), {})) for row in group.values())
            candidates.append(((2 - quality, abs(strike - price), expiry, strike), group))
        if not candidates:
            continue
        _, group = min(candidates, key=lambda item: item[0])
        record = {
            "future": future,
            "call": group["call"],
            "put": group["put"],
            "future_quote": quote,
            "call_quote": quotes[_key(group["call"])],
            "put_quote": quotes[_key(group["put"])],
            "future_volume": volume,
        }
        product = (future["exchange_id"], future.get("product_id"))
        if not product[1]:
            continue
        previous = products.get(product)
        if previous is None or (volume, future["instrument_id"]) > (
            previous["future_volume"],
            previous["future"]["instrument_id"],
        ):
            products[product] = record
    selected, rejected = [], []
    for pair in sorted(
        products.values(),
        key=lambda item: (-item["future_volume"], item["future"]["instrument_id"]),
    ):
        future = pair["future"]
        ref = raw.get(_key(future), {})
        rates = [
            _number(ref.get(name), positive=True)
            for name in ("LongMarginRatio", "ShortMarginRatio")
        ]
        rough = None
        if all(value is not None for value in rates):
            rough = max(rates) * _price(pair["future_quote"]) * float(future["multiplier"])
        pair["rough_future_margin"] = rough
        pair["rough_margin_status"] = "REFERENCE_ONLY" if rough is not None else "UNKNOWN"
        if rough is not None and rough > 10000:
            rejected.append(
                {
                    "future": future["instrument_id"],
                    "rough_future_margin": rough,
                    "reason": "rough_prefilter_only_not_account_margin",
                }
            )
        else:
            selected.append(pair)
    return selected[:max_products], rejected


def _cost_query(client, method, request, session, credentials):
    result = getattr(client, method)(**request)
    records = list(result.records)
    requested_exchange = request.get("exchange_id")
    identity_ok = isinstance(requested_exchange, str) and bool(requested_exchange)
    exchange_sources = set()
    for row in records:
        native_exchange = row.get("ExchangeID")
        if native_exchange == "" and requested_exchange:
            # Some authenticated CTP cost queries return an explicitly empty exchange.
            # Preserve that raw field and attribute only to the submitted query scope.
            exchange_sources.add("request_scope_native_exchange_empty")
        elif native_exchange == requested_exchange and requested_exchange:
            exchange_sources.add("native_exchange_exact")
        else:
            exchange_sources.add("UNVERIFIED")
            identity_ok = False
        if row.get("InstrumentID") != request["instrument_id"]:
            identity_ok = False
        if "hedge_flag" in request and row.get("HedgeFlag") != request["hedge_flag"]:
            identity_ok = False
        if method == "query_instrument_margin_rate_result" and (
            type(row.get("IsRelative")) is not int or row["IsRelative"] != 0
        ):
            identity_ok = False
        for name, value in (
            ("BrokerID", credentials["broker_id"]),
            ("InvestorID", credentials["user_id"]),
        ):
            if row.get(name) not in (None, "", value):
                identity_ok = False
    current = client.get_session_state()
    stable = discovery._valid_session(current) and discovery._session_identity(current) == session
    complete = discovery._query_complete(result, session) and stable
    usable = complete and identity_ok and len(records) == 1
    return {
        "request": request,
        "method": method,
        "complete": complete,
        "status": "COMPLETE" if usable else "UNKNOWN",
        "records": [discovery._public_record(row) for row in records],
        "evidence": discovery._query_evidence(result),
        "identity_match": identity_ok,
        "session_stable": stable,
        "exchange_identity_source": (
            next(iter(exchange_sources)) if len(exchange_sources) == 1 else "UNVERIFIED"
        ),
        "matching_rule": (
            "exact_native_instrument_and_requested_hedge; "
            "native_exchange_exact_or_explicit_empty_attributed_to_request_scope; "
            "missing_null_or_conflicting_native_exchange_rejected"
        ),
    }


def _capital_rows(pairs, entries, session_valid):
    rows = []
    for index, pair in enumerate(pairs):
        for kind in ("put", "call"):
            needed = ("future_margin", "future_fee", f"{kind}_fee")
            evidence = {name: entries.get(f"{index}:{name}") for name in needed}
            for lots in (1, 2):
                for reserve in (0, 2000):
                    row = {
                        "future": pair["future"]["instrument_id"],
                        "option": pair[kind]["instrument_id"],
                        "option_lots": lots,
                        "additional_reserve": reserve,
                        "budget": 10000,
                        "status": "UNKNOWN",
                        "capital_fits": None,
                        "evidence_complete": False,
                        "dependency_query_keys": ",".join(f"{index}:{name}" for name in needed),
                        "quote_basis": "HISTORICAL_DISCOVERY_SNAPSHOT",
                        "economic_signal": "NOT_EVALUATED",
                        "risk_admission": "NOT_EVALUATED",
                        "quote_execution": "NOT_VERIFIED",
                        "reason": "cost_evidence_incomplete",
                    }
                    if session_valid and all(
                        item and item["status"] == "COMPLETE" for item in evidence.values()
                    ):
                        try:
                            if not _legal_lots(pair[kind], lots):
                                raise ValueError("illegal_option_quantity")
                            estimate = estimate_buy_option_pair(
                                pair["future"],
                                pair[kind],
                                pair["future_quote"],
                                pair[kind + "_quote"],
                                evidence["future_margin"]["records"][0],
                                evidence["future_fee"]["records"][0],
                                evidence[kind + "_fee"]["records"][0],
                                option_lots=lots,
                                budget=10000,
                                reserve=reserve,
                            )
                            row.update(
                                estimate,
                                status="STATIC_CAPITAL_ESTIMATE",
                                reason=None,
                                evidence_complete=True,
                            )
                        except ValueError:
                            row["reason"] = "missing_invalid_or_insufficient_calculation_inputs"
                    rows.append(row)
    return rows


def run_collection(args, *, client_factory=None, selector=None, diagnostics_provider=None):
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": "bt_api_ctp.option-pair-costs.v1",
        "status": "PARTIAL",
        "complete": False,
        "requested_profile": args.profile,
        "errors": [],
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "session": {},
        "queries": {},
        "pairs": [],
        "rough_rejections": [],
        "read_only_proof": {"before": {}, "after": {}, "zero_writes": False},
        "provenance": {"collector_sha256": discovery._sha256(__file__)},
        "limitations": [
            "historical_discovery_quotes_not_live_execution",
            "rough_instrument_margin_not_account_margin",
            "reserve_0_and_2000_are_scenarios_not_user_requirements",
            "capital_sufficiency_not_risk_admission_or_arbitrage_signal",
        ],
    }
    client = None
    stable = False
    with discovery._quiet_native():
        try:
            source, payloads = load_discovery(args.discovery_dir, args.profile)
            report["provenance"]["discovery_sha256"] = discovery._sha256(
                Path(args.discovery_dir) / "discovery.json"
            )
            report["provenance"]["source_artifacts"] = source["artifacts_sha256"]
            pairs, rejected = select_pairs(
                payloads["instruments.json"],
                payloads["depth_market_data.json"],
                payloads["raw_instruments.json"],
                source["session"]["trading_day"],
                args.max_products,
            )
            report["pairs"], report["rough_rejections"] = pairs, rejected
            if not pairs:
                raise discovery.DiscoveryBlocked("no_reference_pairs_selected")
            credentials = discovery._credentials(args.env_file)
            if client_factory is None or diagnostics_provider is None:
                from bt_api_ctp.ctp.client import TraderClient, get_ctp_native_diagnostics

                client_factory = client_factory or TraderClient
                diagnostics_provider = diagnostics_provider or get_ctp_native_diagnostics
            diagnostics = diagnostics_provider()
            report["provenance"]["native"] = diagnostics
            if (
                diagnostics.get("native_loaded") is not True
                or diagnostics.get("trader_login_abi_verified") is False
            ):
                raise discovery.DiscoveryBlocked("native_or_login_abi_unverified")
            if selector is None:
                from bt_api_ctp.ctp_env_selector import select_reachable_ctp_environment

                selector = select_reachable_ctp_environment
            selection = selector(
                env="set1",
                profile=args.profile,
                require_profile=args.profile,
                timeout=args.probe_timeout,
            )
            if selection.environment != "simnow" or selection.profile != args.profile:
                raise discovery.DiscoveryBlocked("profile_mismatch")
            client = client_factory(
                front=selection.td_front, **credentials, auto_settlement_confirm=False
            )
            report["read_only_proof"]["before"] = client.get_request_counts()
            if not discovery._zero_writes(report["read_only_proof"]["before"]):
                raise discovery.DiscoveryBlocked("initial_writes_nonzero")
            client.start(block=False)
            deadline = time.monotonic() + args.login_timeout
            while not discovery._valid_session(client.get_session_state()):
                state = client.get_session_state()
                if state.get("login_state") == "failed" or state.get("auth_state") == "failed":
                    raise discovery.DiscoveryBlocked("read_only_login_failed")
                if time.monotonic() >= deadline:
                    raise discovery.DiscoveryBlocked("read_only_login_timeout")
                time.sleep(0.05)
            session = discovery._session_identity(client.get_session_state())
            report["session"] = session
            if session["trading_day"] != source["session"]["trading_day"]:
                raise discovery.DiscoveryBlocked("discovery_trading_day_mismatch")
            if session["account_fingerprint"] != source["session"]["account_fingerprint"]:
                raise discovery.DiscoveryBlocked("discovery_account_mismatch")
            for index, pair in enumerate(pairs):
                future = pair["future"]
                requests = [
                    (
                        "future_margin",
                        "query_instrument_margin_rate_result",
                        future,
                        {"hedge_flag": "1"},
                    ),
                    ("future_fee", "query_instrument_commission_rate_result", future, {}),
                ]
                for kind in ("call", "put"):
                    requests.extend(
                        [
                            (
                                kind + "_cost",
                                "query_option_instrument_trade_cost_result",
                                pair[kind],
                                {
                                    "hedge_flag": "1",
                                    "input_price": _price(pair[kind + "_quote"]),
                                    "underlying_price": _price(pair["future_quote"]),
                                },
                            ),
                            (
                                kind + "_fee",
                                "query_option_instrument_commission_rate_result",
                                pair[kind],
                                {},
                            ),
                        ]
                    )
                for name, method, instrument, extra in requests:
                    current = client.get_session_state()
                    if (
                        not discovery._valid_session(current)
                        or discovery._session_identity(current) != session
                        or not discovery._zero_writes(client.get_request_counts())
                    ):
                        raise discovery.DiscoveryBlocked("session_or_read_only_proof_changed")
                    request = {
                        "instrument_id": instrument["instrument_id"],
                        "exchange_id": instrument["exchange_id"],
                        "timeout": args.query_timeout,
                        **extra,
                    }
                    entry = _cost_query(client, method, request, session, credentials)
                    report["queries"][f"{index}:{name}"] = entry
                    discovery._write_json(output / "costs.partial.json", report)
                    if not entry["session_stable"]:
                        raise discovery.DiscoveryBlocked("session_identity_changed")
                    time.sleep(args.query_interval)
            stable = True
        except discovery.DiscoveryBlocked as exc:
            report["status"] = "BLOCKED"
            report["errors"].append(str(exc))
        except (Exception, KeyboardInterrupt) as exc:
            report["errors"].append(
                "interrupted"
                if isinstance(exc, KeyboardInterrupt)
                else "collection_failed:" + type(exc).__name__
            )
        finally:
            if client is not None:
                try:
                    current = client.get_session_state()
                    stable = (
                        stable
                        and discovery._valid_session(current)
                        and (discovery._session_identity(current) == report["session"])
                    )
                    client.stop()
                except Exception:
                    stable = False
                    report["errors"].append("stop_or_final_session_failed")
                try:
                    report["read_only_proof"]["after"] = client.get_request_counts()
                except Exception:
                    report["errors"].append("request_counts_unavailable")
    proof = report["read_only_proof"]
    proof["zero_writes"] = discovery._zero_writes(proof["before"]) and discovery._zero_writes(
        proof["after"]
    )
    if not proof["zero_writes"]:
        report["errors"].append("zero_write_proof_failed")
    valid = stable and proof["zero_writes"] and not report["errors"]
    report["complete"] = (
        valid
        and bool(report["queries"])
        and all(item["status"] == "COMPLETE" for item in report["queries"].values())
    )
    if report["complete"]:
        report["status"] = "COMPLETE_COST_COLLECTION"
    capital = _capital_rows(report["pairs"], report["queries"], valid)
    for row in capital:
        row["source_discovery_sha256"] = report["provenance"].get("discovery_sha256")
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["counts"] = {
        "pairs": len(report["pairs"]),
        "queries": len(report["queries"]),
        "unknown_queries": sum(item["status"] != "COMPLETE" for item in report["queries"].values()),
    }
    discovery._write_json(output / "costs.json", report)
    discovery._write_json(
        output / "capital_results.json",
        {"complete": report["complete"], "limitations": report["limitations"], "records": capital},
    )
    with (output / "capital_results.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=sorted({key for row in capital for key in row}) or ["status"]
        )
        writer.writeheader()
        writer.writerows(capital)
    return report


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = run_collection(args)
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({"status": "PARTIAL", "error": type(exc).__name__}))
        return 2
    print(json.dumps({key: report[key] for key in ("status", "complete", "counts", "errors")}))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
