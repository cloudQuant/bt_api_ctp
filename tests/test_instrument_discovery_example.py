"""Offline acceptance tests for the read-only instrument discovery example."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bt_api_ctp.query import QueryResult


@pytest.fixture
def example():
    path = Path(__file__).parents[1] / "examples" / "discover_instruments.py"
    spec = importlib.util.spec_from_file_location("instrument_discovery_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def result(*, complete=True, records=None, request_type="instruments"):
    now = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)
    return QueryResult(
        request_type=request_type,
        request_id=7,
        connection_generation=3,
        account_fingerprint="a" * 16,
        started_at_utc=now,
        completed_at_utc=now,
        is_last_seen=complete,
        error_code=0,
        error_message="",
        timed_out=not complete,
        complete=complete,
        records=tuple(records if records is not None else [
            {"InstrumentID": "m2701", "ExchangeID": "DCE", "ProductClass": "1",
             "VolumeMultiple": 10, "PriceTick": 1, "ExpireDate": "20270115"},
            {"InstrumentID": "m2701-C-3000", "ExchangeID": "DCE", "ProductClass": "2",
             "UnderlyingInstrID": "m2701", "StrikePrice": 3000, "OptionsType": "1",
             "VolumeMultiple": 10, "PriceTick": 0.5, "ExpireDate": "20261207"},
        ]),
    )


class FakeClient:
    def __init__(self, query=None, *, query_error=None, stop_error=False):
        self.query = query or result()
        self.query_error = query_error
        self.stop_error = stop_error
        self.stopped = False
        self.started = False
        self.queries = []
        self.counts = {"settlement_confirm": 0, "order_insert": 0, "order_action": 0,
                       "query_instruments": 0, "query_depth_market_data": 0}
        self.session = {"read_only_ready": True, "auto_settlement_confirm": False,
                        "account_fingerprint": "a" * 16, "connection_generation": 3,
                        "trading_day": "20260910"}

    def start(self, block=False):
        self.started = True

    def get_session_state(self):
        return dict(self.session)

    def get_request_counts(self):
        return dict(self.counts)

    def query_instruments_result(self, **kwargs):
        self.queries.append(("instruments", kwargs))
        self.counts["query_instruments"] += 1
        if self.query_error:
            print("sensitive-native-user-and-password")
            raise self.query_error
        return self.query

    def query_depth_market_data_result(self, **kwargs):
        self.queries.append(("depth", kwargs))
        self.counts["query_depth_market_data"] += 1
        return result(request_type="depth_market_data", records=[
            {"InstrumentID": "m2701", "ExchangeID": "DCE", "LastPrice": 3000}
        ])

    def stop(self):
        self.stopped = True
        if self.stop_error:
            raise RuntimeError("sensitive-stop-error")


@pytest.fixture
def harness(example, tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.upper().startswith(("CTP_", "SIMNOW_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("CTP_BROKER_ID", "9999")
    monkeypatch.setenv("CTP_USER_ID", "private-user")
    monkeypatch.setenv("CTP_PASSWORD", "private-password")
    args = example.build_parser().parse_args(["--output-dir", str(tmp_path / "out")])
    constructed = []
    selected = []

    def run(client=None, *, selector_override=None, diagnostics_override=None):
        client = client or FakeClient()

        def factory(**kwargs):
            constructed.append(kwargs)
            return client

        def selector(**kwargs):
            selected.append(kwargs)
            return SimpleNamespace(environment="simnow", profile="set1_group1",
                                   td_front="tcp://frozen", md_front="tcp://frozen-md")

        report = example.run_discovery(
            args, client_factory=factory, selector=selector_override or selector,
            diagnostics_provider=lambda: diagnostics_override or {"native_loaded": True,
                "runtime_source": "test_native", "loaded_module_sha256": "1" * 64,
                "ctp_package_sha256": "2" * 64},
        )
        return report, client

    return SimpleNamespace(run=run, args=args, constructed=constructed, selected=selected,
                           output=tmp_path / "out")


def test_complete_export_is_full_visible_query_and_zero_write(harness):
    report, client = harness.run()
    assert report["complete"] is True
    assert report["status"] == "COMPLETE_VISIBLE_UNIVERSE"
    assert client.stopped and client.started
    assert harness.constructed[0]["auto_settlement_confirm"] is False
    assert client.queries == [("instruments", {
        "instrument_id": "", "exchange_id": "", "product_id": "", "timeout": 120.0,
    })]
    assert harness.selected[0]["profile"] == "set1_group1"
    assert harness.selected[0]["require_profile"] == "set1_group1"
    assert report["read_only_proof"]["zero_writes"] is True
    records = json.loads((harness.output / "instruments.json").read_text())["records"]
    assert records[1]["asset_type"] == "option"
    assert records[1]["exercise_style"] is None
    assert "m2701-C-3000" in (harness.output / "instruments.csv").read_text()
    assert report["provenance"]["native"]["native_loaded"] is True
    assert len(report["provenance"]["example_sha256"]) == 64
    text = "".join(p.read_text() for p in harness.output.iterdir())
    assert "private-user" not in text and "private-password" not in text


@pytest.mark.parametrize("change", [
    {"complete": False, "is_last_seen": False, "timed_out": True},
    {"is_last_seen": False}, {"error_code": 7}, {"unsupported": True},
    {"connection_generation": 2}, {"account_fingerprint": "b" * 16},
])
def test_partial_or_mixed_identity_never_export_complete(harness, change):
    report, client = harness.run(FakeClient(replace(result(), **change)))
    assert report["complete"] is False
    assert report["status"] == "PARTIAL"
    assert report["errors"]
    assert client.stopped
    exported = json.loads((harness.output / "raw_instruments.json").read_text())
    assert exported["complete"] is False and len(exported["records"]) == 2


@pytest.mark.parametrize("key,value", [("order_insert", 1), ("order_action", 1),
                                      ("settlement_confirm", 1), ("order_insert", None)])
def test_write_counters_fail_closed(harness, key, value):
    client = FakeClient()
    if value is None:
        del client.counts[key]
    else:
        client.counts[key] = value
    report, _ = harness.run(client)
    assert report["complete"] is False
    assert report["read_only_proof"]["zero_writes"] is False
    assert client.stopped


def test_generation_change_during_query_is_partial(harness):
    client = FakeClient()
    query = client.query_instruments_result

    def changed(**kwargs):
        value = query(**kwargs)
        client.session["connection_generation"] = 4
        return value

    client.query_instruments_result = changed
    report, _ = harness.run(client)
    assert report["complete"] is False


def test_exception_and_native_stdout_are_sanitized_and_stop_runs(harness, capfd):
    report, client = harness.run(FakeClient(query_error=RuntimeError("private-password")))
    assert report["complete"] is False and client.stopped
    captured = capfd.readouterr()
    assert "sensitive-native" not in captured.out + captured.err
    text = "".join(p.read_text() for p in harness.output.iterdir())
    assert "private-password" not in text and "sensitive-native" not in text


def test_stop_failure_cannot_claim_success(harness):
    report, _ = harness.run(FakeClient(stop_error=True))
    assert report["complete"] is False and "stop_failed" in report["errors"]


def test_optional_depth_is_one_public_query(harness):
    harness.args.include_depth = True
    report, client = harness.run()
    assert report["complete"] is True
    assert client.queries[1] == ("depth", {"instrument_id": "", "exchange_id": "",
                                          "timeout": 120.0})
    assert (harness.output / "depth_market_data.json").exists()


def test_raw_output_strips_account_fields(harness):
    record = dict(result().records[0], InvestorID="secret-investor", Password="secret-pass")
    report, _ = harness.run(FakeClient(result(records=[record])))
    assert report["complete"] is True
    raw = json.loads((harness.output / "raw_instruments.json").read_text())["records"][0]
    assert "InstrumentID" in raw
    assert "InvestorID" not in raw and "Password" not in raw
    assert "secret-investor" not in "".join(p.read_text() for p in harness.output.iterdir())


def test_existing_output_directory_is_never_overwritten(harness):
    harness.output.mkdir()
    sentinel = harness.output / "important.txt"
    sentinel.write_text("preserve")
    with pytest.raises(FileExistsError):
        harness.run()
    assert sentinel.read_text() == "preserve"
    assert not harness.constructed and not harness.selected


def test_only_explicit_env_file_is_read_and_process_wins(harness, tmp_path, monkeypatch):
    monkeypatch.delenv("CTP_PASSWORD")
    monkeypatch.setenv("SIMNOW_PASSWORD", "process-secret")
    env_file = tmp_path / "explicit.env"
    env_file.write_text('CTP_PASSWORD="file secret"\nCTP_USER_ID="file user"\n')
    harness.args.env_file = env_file
    report, _ = harness.run()
    assert report["complete"] is True
    assert harness.constructed[0]["password"] == "process-secret"
    assert harness.constructed[0]["user_id"] == "private-user"


@pytest.mark.parametrize("option", [["--profile", "production"], ["--td-front", "tcp://x"],
                                    ["--query-timeout", "nan"]])
def test_cli_rejects_unfrozen_endpoint_and_invalid_timeouts(example, tmp_path, option):
    with pytest.raises(SystemExit):
        example.build_parser().parse_args(["--output-dir", str(tmp_path / "out"), *option])


def test_custom_selection_is_rejected_before_client_construction(harness):
    report, _ = harness.run(selector_override=lambda **_: SimpleNamespace(
        environment="custom", profile="set1_group1", td_front="tcp://custom"))
    assert report["complete"] is False
    assert not harness.constructed


def test_known_unsupported_login_abi_is_blocked_before_network_probe(harness):
    report, _ = harness.run(diagnostics_override={
        "native_loaded": True, "trader_login_abi_verified": False,
        "trader_login_abi_reason": "ctp_trader_login_abi_unverified",
    })
    assert report["status"] == "BLOCKED"
    assert "ctp_trader_login_abi_unverified" in report["errors"]
    assert not harness.selected and not harness.constructed


def test_existing_public_login_guard_produces_stable_blocked_code(harness):
    client = FakeClient()
    client.session.update(read_only_ready=False, login_state="failed",
                          last_error={"error": "login_submit_failed",
                                      "detail": "ctp_trader_login_abi_unverified"})
    report, _ = harness.run(client)
    assert report["status"] == "BLOCKED"
    assert "ctp_trader_login_abi_unverified" in report["errors"]
    assert client.stopped and not client.queries


def test_byte_reference_fields_are_preserved_in_json(harness):
    row = {"InstrumentID": b"m2701", "ExchangeID": b"DCE", "ProductClass": b"1"}
    report, _ = harness.run(FakeClient(result(records=[row])))
    assert report["complete"] is True
    records = json.loads((harness.output / "raw_instruments.json").read_text())["records"]
    assert records[0]["InstrumentID"] == "m2701"


def test_progress_is_visible_before_query_returns(harness):
    client = FakeClient()
    query = client.query_instruments_result

    def observed(**kwargs):
        progress = json.loads((harness.output / "progress.json").read_text())
        assert progress["phase"] == "querying_instruments"
        assert progress["write_counts"] == dict.fromkeys(
            ("settlement_confirm", "order_insert", "order_action"), 0)
        manifest = json.loads((harness.output / "discovery.json").read_text())
        assert manifest["complete"] is False
        return query(**kwargs)

    client.query_instruments_result = observed
    report, _ = harness.run(client)
    assert report["complete"] is True
    assert "discovery.json" not in report["artifacts_sha256"]


@pytest.mark.parametrize("changed,code", [
    ({"auth_state": "failed"}, "read_only_authentication_failed"),
    ({"login_state": "failed", "last_error": {"detail": "private-user"}}, "read_only_login_failed"),
    ({"auto_settlement_confirm": True}, "auto_settlement_confirm_not_disabled"),
])
def test_login_failures_are_diagnostic_without_secret_text(harness, changed, code):
    client = FakeClient()
    client.session.update(read_only_ready=False, **changed)
    report, _ = harness.run(client)
    assert code in report["errors"] and report["status"] == "BLOCKED"
    assert client.stopped and not client.queries


def test_writes_during_query_invalidate_initial_zero_snapshot(harness):
    client = FakeClient()
    query = client.query_instruments_result

    def changed(**kwargs):
        value = query(**kwargs)
        client.counts["settlement_confirm"] = 1
        return value

    client.query_instruments_result = changed
    report, _ = harness.run(client)
    assert report["complete"] is False
    assert report["read_only_proof"]["zero_writes"] is False


def test_depth_partial_prevents_whole_run_success(harness):
    harness.args.include_depth = True
    client = FakeClient()
    client.query_depth_market_data_result = lambda **_: result(
        complete=False, request_type="depth_market_data")
    report, _ = harness.run(client)
    assert report["complete"] is False
    assert "depth_query_incomplete_or_identity_mismatch" in report["errors"]


def test_output_failure_leaves_partial_manifest_and_stops_client(example, harness, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private-password disk failure")

    monkeypatch.setattr(example.csv, "DictWriter", fail)
    client = FakeClient()
    with pytest.raises(OSError):
        harness.run(client)
    assert client.stopped
    manifest = json.loads((harness.output / "discovery.json").read_text())
    assert manifest["complete"] is False


@pytest.mark.parametrize("complete,expected", [(True, 0), (False, 2)])
def test_main_exit_code_reflects_completion(example, tmp_path, monkeypatch, capsys, complete, expected):
    monkeypatch.setattr(example, "run_discovery", lambda args: {
        "complete": complete, "status": "COMPLETE_VISIBLE_UNIVERSE" if complete else "PARTIAL",
        "counts": {}, "read_only_proof": {"zero_writes": True},
    })
    assert example.main(["--output-dir", str(tmp_path / "out")]) == expected
    assert json.loads(capsys.readouterr().out)["complete"] is complete


def test_repeated_instrument_filters_keep_query_proofs_and_deduplicate_union(example, harness):
    parsed = example.build_parser().parse_args([
        "--output-dir", str(harness.output), "--include-depth",
        "--instrument-filter", "DCE:m2701", "--instrument-filter", "DCE:m2701-C",
    ])
    vars(harness.args).update(vars(parsed))
    client = FakeClient()
    report, client = harness.run(client)
    assert report["complete"] is True
    assert report["scope"] == "filtered_visible_universe"
    assert report["status"] == "COMPLETE_FILTERED_VISIBLE_UNIVERSE"
    assert len(report["queries"]) == 4
    assert "instruments" not in report["queries"]
    assert report["counts"]["instruments"] == 2
    assert report["counts"]["depth_market_data"] == 1
    assert [entry[1]["instrument_id"] for entry in client.queries] == [
        "m2701", "m2701", "m2701-C", "m2701-C"]
    assert len(report["selected_coverage"]) == 2
    assert all(row["complete"] for row in report["selected_coverage"])


def test_one_incomplete_filtered_query_makes_union_partial(harness):
    harness.args.instrument_filter = [("DCE", "m2701"), ("DCE", "m2705")]
    client = FakeClient()
    query = client.query_instruments_result

    def partial(**kwargs):
        value = query(**kwargs)
        return replace(value, complete=False, is_last_seen=False, timed_out=True)

    client.query_instruments_result = partial
    report, _ = harness.run(client)
    assert report["complete"] is False
    assert report["scope"] == "filtered_visible_universe"
    assert len(report["selected_coverage"]) == 2


@pytest.mark.parametrize("value", ["m2701", "UNKNOWN:m2701", "DCE:", "DCE:m2701:bad"])
def test_invalid_instrument_filter_is_rejected_by_parser(example, tmp_path, value):
    with pytest.raises(SystemExit):
        example.build_parser().parse_args([
            "--output-dir", str(tmp_path / "out"), "--instrument-filter", value])
