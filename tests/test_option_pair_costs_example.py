"""Offline tests: selection, identity, complete evidence and zero-write collection."""

from __future__ import annotations

import contextlib
import importlib.util
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bt_api_ctp.query import QueryResult


@pytest.fixture
def example(monkeypatch):
    directory = Path(__file__).parents[1] / "examples"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "option_pair_costs_example", directory / "query_option_pair_costs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.discovery, "_quiet_native", contextlib.nullcontext)
    monkeypatch.setattr(
        module.discovery,
        "_credentials",
        lambda _: {
            "broker_id": "private-broker",
            "user_id": "private-user",
            "password": "private-password",
        },
    )
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    return module


def instruments(symbol="m2701", product="m"):
    common = {
        "exchange_id": "DCE",
        "is_trading": True,
        "multiplier": 10,
        "min_limit_order_volume": 1,
        "max_limit_order_volume": 100,
        "product_id": product,
    }
    future = dict(common, instrument_id=symbol, asset_type="future")
    options = [
        dict(
            common,
            instrument_id=f"{symbol}-{kind}-3000",
            asset_type="option",
            option_type=kind,
            expiry_date="20261207",
            strike_price=3000,
            underlying_instrument=symbol,
        )
        for kind in ("call", "put")
    ]
    return [future, *options]


def quotes(rows, volume=100):
    return [
        {
            "InstrumentID": row["instrument_id"],
            "ExchangeID": row["exchange_id"],
            "BidPrice1": 2999 if row["asset_type"] == "future" else 99,
            "AskPrice1": 3000 if row["asset_type"] == "future" else 100,
            "LastPrice": 3000 if row["asset_type"] == "future" else 100,
            "PreSettlementPrice": 3000 if row["asset_type"] == "future" else 100,
            "BidVolume1": 10,
            "AskVolume1": 10,
            "Volume": volume,
        }
        for row in rows
    ]


def query_result(request, method):
    row = {
        "InstrumentID": request["instrument_id"],
        "ExchangeID": request["exchange_id"],
        "BrokerID": "private-broker",
        "InvestorID": "private-user",
    }
    if "hedge_flag" in request:
        row["HedgeFlag"] = request["hedge_flag"]
    if "margin_rate" in method:
        row.update(
            LongMarginRatioByMoney=0.1,
            LongMarginRatioByVolume=0,
            ShortMarginRatioByMoney=0.12,
            ShortMarginRatioByVolume=0,
            IsRelative=0,
        )
    elif "commission_rate" in method:
        for prefix in ("Open", "Close", "CloseToday"):
            row[prefix + "RatioByMoney"] = 0
            row[prefix + "RatioByVolume"] = 2
    else:
        row.update(FixedMargin=3000, MiniMargin=2000, Royalty=1000)
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    return QueryResult(
        request_type=method,
        request_id=1,
        connection_generation=3,
        account_fingerprint="a" * 16,
        started_at_utc=now,
        completed_at_utc=now,
        is_last_seen=True,
        error_code=0,
        error_message="",
        timed_out=False,
        complete=True,
        records=(row,),
    )


class FakeClient:
    def __init__(self, mutate=None):
        self.session = {
            "read_only_ready": True,
            "auto_settlement_confirm": False,
            "account_fingerprint": "a" * 16,
            "connection_generation": 3,
            "trading_day": "20260910",
        }
        self.counts = {"settlement_confirm": 0, "order_insert": 0, "order_action": 0}
        self.calls, self.mutate, self.stopped = [], mutate, False

    def start(self, block=False):
        assert block is False

    def stop(self):
        self.stopped = True

    def get_session_state(self):
        return dict(self.session)

    def get_request_counts(self):
        return dict(self.counts)

    def __getattr__(self, name):
        if not name.startswith("query_"):
            raise AttributeError(name)

        def query(**request):
            self.calls.append((name, request))
            self.counts[name] = self.counts.get(name, 0) + 1
            result = query_result(request, name)
            return self.mutate(result, name, request, self) if self.mutate else result

        return query


@pytest.fixture
def harness(example, tmp_path):
    source = tmp_path / "discovery"
    source.mkdir()
    rows = instruments()
    contents = {
        "instruments.json": rows,
        "depth_market_data.json": quotes(rows),
        "raw_instruments.json": [
            {
                "InstrumentID": "m2701",
                "ExchangeID": "DCE",
                "LongMarginRatio": 0.1,
                "ShortMarginRatio": 0.12,
            }
        ],
    }
    for name, records in contents.items():
        example.discovery._write_json(source / name, {"complete": True, "records": records})
    manifest = {
        "complete": True,
        "read_only_proof": {"zero_writes": True},
        "selected_profile": "set1_group1",
        "session": {
            "trading_day": "20260910",
            "account_fingerprint": "a" * 16,
            "connection_generation": 2,
        },
        "artifacts_sha256": {name: example.discovery._sha256(source / name) for name in contents},
    }
    example.discovery._write_json(source / "discovery.json", manifest)
    args = example.build_parser().parse_args(
        [
            "--discovery-dir",
            str(source),
            "--output-dir",
            str(tmp_path / "costs"),
            "--env-file",
            str(tmp_path / "unused.env"),
            "--profile",
            "set1_group1",
        ]
    )

    def run(client=None, diagnostics=None):
        client = client or FakeClient()

        def factory(**kwargs):
            assert kwargs["auto_settlement_confirm"] is False
            return client

        def selector(**kwargs):
            assert kwargs["profile"] == kwargs["require_profile"] == "set1_group1"
            return SimpleNamespace(
                environment="simnow", profile="set1_group1", td_front="tcp://fixture"
            )

        report = example.run_collection(
            args,
            client_factory=factory,
            selector=selector,
            diagnostics_provider=lambda: (
                diagnostics or {"native_loaded": True, "trader_login_abi_verified": True}
            ),
        )
        return report, client

    return args, run


def test_complete_collection_has_six_read_queries_and_eight_static_rows(example, harness):
    args, run = harness
    report, client = run()
    assert report["complete"] and report["read_only_proof"]["zero_writes"]
    assert len(client.calls) == 6 and client.stopped
    for method, request in client.calls:
        if "trade_cost" in method:
            assert request["input_price"] == 100 and request["underlying_price"] == 3000
            assert request["hedge_flag"] == "1"
    capital = json.loads((args.output_dir / "capital_results.json").read_text())["records"]
    assert len(capital) == 8 and all(row["capital_fits"] is True for row in capital)
    row = next(
        row
        for row in capital
        if row["future_side"] == "buy"
        and row["option_lots"] == 1
        and row["additional_reserve"] == 0
    )
    assert row["base_capital"] == 4008  # 3000 futures margin + 1000 premium + 8 fees.
    assert {row["additional_reserve"] for row in capital} == {0, 2000}
    assert all(row["risk_admission"] == "NOT_EVALUATED" for row in capital)
    text = (args.output_dir / "costs.json").read_text()
    assert (
        "private-user" not in text
        and "private-password" not in text
        and "private-broker" not in text
    )


def test_explicit_empty_native_exchange_uses_request_scope_without_rewriting_raw(harness):
    args, run = harness

    def mutate(result, method, request, client):
        row = dict(result.records[0], ExchangeID="")
        return replace(result, records=(row,))

    report, _ = run(FakeClient(mutate))
    assert report["complete"] is True
    for entry in report["queries"].values():
        assert entry["status"] == "COMPLETE"
        assert entry["exchange_identity_source"] == "request_scope_native_exchange_empty"
        assert entry["request"]["exchange_id"] == "DCE"
        assert entry["records"][0]["ExchangeID"] == ""
        assert "request_scope" in entry["matching_rule"]
    rows = json.loads((args.output_dir / "capital_results.json").read_text())["records"]
    assert all(row["capital_fits"] is True and row["evidence_complete"] for row in rows)
    assert all(row["risk_admission"] == "NOT_EVALUATED" for row in rows)


@pytest.mark.parametrize(
    "failure",
    [
        "partial",
        "empty",
        "wrong_symbol",
        "wrong_exchange",
        "missing_exchange",
        "null_exchange",
        "wrong_hedge",
        "relative",
        "missing_relative",
        "generation",
        "account",
    ],
)
def test_one_bad_query_blocks_all_capital_fits(harness, failure):
    args, run = harness

    def mutate(result, method, request, client):
        if "margin_rate" not in method:
            return result
        row = dict(result.records[0])
        if failure == "partial":
            return replace(result, complete=False, timed_out=True, is_last_seen=False)
        if failure == "empty":
            return replace(result, records=())
        if failure == "generation":
            return replace(result, connection_generation=99)
        if failure == "account":
            return replace(result, account_fingerprint="b" * 16)
        if failure == "wrong_symbol":
            row["InstrumentID"] = "wrong"
        if failure == "wrong_exchange":
            row["ExchangeID"] = "wrong"
        if failure == "missing_exchange":
            row.pop("ExchangeID")
        if failure == "null_exchange":
            row["ExchangeID"] = None
        if failure == "wrong_hedge":
            row["HedgeFlag"] = "2"
        if failure == "relative":
            row["IsRelative"] = 1
        if failure == "missing_relative":
            row.pop("IsRelative")
        return replace(result, records=(row,))

    report, _ = run(FakeClient(mutate))
    assert (
        report["complete"] is False and report["queries"]["0:future_margin"]["status"] == "UNKNOWN"
    )
    rows = json.loads((args.output_dir / "capital_results.json").read_text())["records"]
    assert all(row["capital_fits"] is None for row in rows)


def test_seller_trade_cost_partial_does_not_block_long_option_capital(harness):
    args, run = harness

    def mutate(result, method, request, client):
        if "call" in request["instrument_id"] and "trade_cost" in method:
            return replace(result, complete=False, is_last_seen=False)
        return result

    report, _ = run(FakeClient(mutate))
    assert not report["complete"]
    rows = json.loads((args.output_dir / "capital_results.json").read_text())["records"]
    assert all(row["capital_fits"] is True and row["evidence_complete"] for row in rows)
    assert all("call_cost" not in row["dependency_query_keys"] for row in rows)


def test_option_fee_partial_blocks_only_dependent_rows(harness):
    args, run = harness

    def mutate(result, method, request, client):
        if "call" in request["instrument_id"] and "commission_rate" in method:
            return replace(result, complete=False, is_last_seen=False)
        return result

    report, _ = run(FakeClient(mutate))
    assert not report["complete"]
    rows = json.loads((args.output_dir / "capital_results.json").read_text())["records"]
    assert all(row["capital_fits"] is None for row in rows if "call" in row["option"])
    assert all(row["capital_fits"] is True for row in rows if "put" in row["option"])


def test_absent_optional_abi_diagnostic_uses_public_login_guard(harness):
    _, run = harness
    report, client = run(diagnostics={"native_loaded": True})
    assert report["complete"] and len(client.calls) == 6


@pytest.mark.parametrize("failure", ["generation_change", "write_count"])
def test_global_session_or_zero_write_violation_blocks_every_row(harness, failure):
    args, run = harness

    def mutate(result, method, request, client):
        if failure == "generation_change":
            client.session["connection_generation"] += 1
        else:
            client.counts["settlement_confirm"] += 1
        return result

    report, client = run(FakeClient(mutate))
    assert not report["complete"] and client.stopped
    rows = json.loads((args.output_dir / "capital_results.json").read_text())["records"]
    assert all(row["capital_fits"] is None for row in rows)
    if failure == "write_count":
        assert report["read_only_proof"]["zero_writes"] is False


@pytest.mark.parametrize(
    "failure", ["source_hash", "source_profile", "source_incomplete", "native"]
)
def test_pre_network_blockers(example, harness, failure):
    args, run = harness
    if failure == "source_hash":
        (args.discovery_dir / "instruments.json").write_text("{}")
    elif failure.startswith("source_"):
        path = args.discovery_dir / "discovery.json"
        report = json.loads(path.read_text())
        report["selected_profile" if failure == "source_profile" else "complete"] = (
            "wrong" if failure == "source_profile" else False
        )
        path.write_text(json.dumps(report))
    diagnostics = (
        {"native_loaded": True, "trader_login_abi_verified": False} if failure == "native" else None
    )
    report, client = run(diagnostics=diagnostics)
    assert not report["complete"] and report["status"] == "BLOCKED" and not client.calls


def test_selection_active_future_expiry_and_margin_prefilter(example):
    first, second = instruments(), instruments("m2705")
    rows = first + second
    depth = quotes(first, 100) + quotes(second, 200)
    selected, _ = example.select_pairs(rows, depth, [], "20260910")
    assert selected[0]["future"]["instrument_id"] == "m2705"
    second[1]["expiry_date"] = "20260910"
    selected, _ = example.select_pairs(rows, depth, [], "20260910")
    assert selected[0]["future"]["instrument_id"] == "m2701"
    raw = [
        {
            "InstrumentID": "m2701",
            "ExchangeID": "DCE",
            "LongMarginRatio": 0.4,
            "ShortMarginRatio": 0.4,
        }
    ]
    selected, rejected = example.select_pairs(rows, depth, raw, "20260910")
    assert selected == [] and rejected[0]["rough_future_margin"] == 12000


def test_output_directory_must_be_new(harness):
    args, run = harness
    args.output_dir.mkdir()
    with pytest.raises(FileExistsError):
        run()
