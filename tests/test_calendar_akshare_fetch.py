import builtins
import os
import re
import signal
import subprocess
import sys
import time
import types
from datetime import date

import pytest

from bt_api_ctp import calendar_akshare_fetch as fetch


def capture(packet=None, transport="CHILD_OK"):
    return fetch.collect_akshare_contract(
        venue="SHFE",
        contract="RB2701",
        requested_date=date(2026, 11, 13),
        _runner=lambda *_: (transport, packet),
    )


def packet(rows, status="OK"):
    return {"status": status, "rows": rows}


def test_success_retains_original_rows_and_unverified_time():
    result = capture(packet([["交易时间", "日盘 09:00-10:15 未知"], ["名称", "螺纹"]]))
    assert result.status == "SUCCESS"
    assert result.candidate.fields[0].raw_value == "日盘 09:00-10:15 未知"
    assert result.time_candidate.status == "NEEDS_REVIEW"
    assert result.time_candidate.uncertain_spans
    assert (result.verification, result.authorization, result.coverage) == (
        "UNVERIFIED",
        "NOT_AUTHORIZED",
        "UNKNOWN",
    )
    assert not result.candidate.timeout_enforced
    assert result.required_version == result.observed_version == fetch.VERSION
    assert result.candidate.source_version == fetch.VERSION
    assert result.time_candidate.source_id == result.capture_id
    assert result.raw_rows == (("交易时间", "日盘 09:00-10:15 未知"), ("名称", "螺纹"))


def test_typed_rows_preserve_numeric_value_and_old_default():
    result = capture(packet([["交易时间", 12], ["x", None]]))
    assert result.status == "MISSING_TRADING_TIME"
    assert result.raw_rows == (("交易时间", 12), ("x", None))
    from dataclasses import replace

    assert replace(result, raw_rows=None).raw_rows is None


@pytest.mark.parametrize(
    ("rows", "status"),
    [
        ([], "EMPTY"),
        ([["名称", "x"]], "MISSING_TRADING_TIME"),
        ([["交易时间", None]], "MISSING_TRADING_TIME"),
        ([["交易时间", 12]], "MISSING_TRADING_TIME"),
        ([["交易时间", "日盘 09:00-10:00"], ["交易时间", "日盘 10:00-11:00"]], "STRUCTURE_CHANGED"),
        ([[None, "x"]], "STRUCTURE_CHANGED"),
    ],
)
def test_collector_statuses(rows, status):
    result = capture(packet(rows))
    assert result.status == status
    assert result.observed_version == result.candidate.source_version == fetch.VERSION
    if len(rows) == 2:
        assert result.time_candidate is None
        assert result.time_diagnostic.upstream_status == "STRUCTURE_CHANGED"
        assert result.time_diagnostic.candidate.status == "DUPLICATE_FIELD"


@pytest.mark.parametrize(
    "status", ["DEPENDENCY_UNAVAILABLE", "VERSION_MISMATCH", "FETCH_ERROR", "STRUCTURE_CHANGED"]
)
def test_child_statuses(status):
    result = capture(packet([], status))
    assert result.status == status
    assert not result.candidate.fields
    assert result.required_version == fetch.VERSION
    assert result.observed_version is result.candidate.source_version is None
    assert result.source_id is result.response_digest is None


@pytest.mark.parametrize(
    ("transport", "status"),
    [
        ("CHILD_TIMEOUT", "FETCH_TIMEOUT"),
        ("CHILD_ERROR", "FETCH_ERROR"),
        ("CHILD_CLEANUP_UNCERTAIN", "UNKNOWN"),
        ("SUPERVISOR_UNSUPPORTED", "UNKNOWN"),
    ],
)
def test_transport_states(transport, status):
    result = capture(transport=transport)
    assert (result.status, result.transport_status) == (status, transport)
    assert result.required_version == fetch.VERSION
    assert result.observed_version is result.candidate.source_version is None
    assert not result.candidate.timeout_enforced
    assert result.direct_child_cleanup == "UNKNOWN"
    assert result.descendant_cleanup == "NOT_PROVEN"
    if status in {"FETCH_TIMEOUT", "UNKNOWN"}:
        assert result.timeout_retry == "NOT_PROVEN_SAFE"


@pytest.mark.parametrize(
    "bad",
    [
        [["交易时间", {"secret": "value"}]],
        [["x", True]],
        [["x", float("nan")]],
        [["x", "a" * (fetch.MAX_CELL + 1)]],
        [["x", "a"]] * (fetch.MAX_ROWS + 1),
        [["x"]],
        [["x", "a", "b"]],
    ],
)
def test_untrusted_payload_is_rejected(bad):
    assert capture(packet(bad)).status == "FETCH_ERROR"


def test_request_validation_precedes_spawn():
    for contract in ("../../secret", "RB2701-option", "RB2701\n"):
        with pytest.raises(ValueError):
            fetch.collect_akshare_contract(
                venue="SHFE",
                contract=contract,
                requested_date=date.today(),
                _runner=lambda *_: pytest.fail("spawned"),
            )
    for timeout in (True, 0, -1, float("nan"), 61, 10**1000, "1"):
        with pytest.raises(ValueError):
            fetch.collect_akshare_contract(
                venue="SHFE", contract="RB2701", requested_date=date.today(), timeout=timeout
            )


def test_no_exception_or_path_leak(capsys):
    def broken(*_):
        raise RuntimeError("/private/path credential=secret")

    result = fetch.collect_akshare_contract(
        venue="SHFE", contract="RB2701", requested_date=date.today(), _runner=broken
    )
    assert result.status == "UNKNOWN"
    assert "/private/path" not in repr(result) and "secret" not in repr(result)
    assert capsys.readouterr() == ("", "")


@pytest.mark.skipif(os.name != "posix", reason="POSIX supervisor")
def test_supervisor_timeout_kills_owned_pid(tmp_path):
    pidfile = tmp_path / "pid"
    code = "import os,time,sys;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(30)"
    parent = (
        "import sys; from bt_api_ctp import _calendar_child_supervisor as s; "
        "print(s.supervise([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
        "0.5, 100, lambda x: ('OK', x))[0])"
    )
    run = subprocess.run(
        [sys.executable, "-c", parent, code, str(pidfile)],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert run.stdout.strip() == "CHILD_TIMEOUT"
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_supervisor_unknown_cleanup_contract():
    from bt_api_ctp import _calendar_child_supervisor as sup

    class Child:
        def kill(self):
            raise OSError("sensitive")

    assert sup.stop_child(Child(), "CHILD_TIMEOUT") == ("CHILD_CLEANUP_UNCERTAIN", None)


def test_decode_rejects_malformed_and_extra_fields():
    for raw in (
        b"not-json",
        b"{}",
        b'{"status":"OK","rows":[],"secret":1}',
        b'{"status":"FETCH_ERROR","status":"OK","rows":[]}',
        b'{"status":"OK","rows":[],"rows":[["x","y"]]}',
        b'{"status":"OK","rows":[["x",{"x":1,"x":2}]]}',
    ):
        assert fetch._decode(raw) == ("CHILD_ERROR", None)


def test_stable_scoped_source_and_capture_metadata():
    first = capture(packet([["交易时间", "日盘 09:00-10:00"], ["名称", "a"]]))
    same = capture(packet([["交易时间", "日盘 09:00-10:00"], ["名称", "a"]]))
    changed = capture(packet([["交易时间", "日盘 09:00-11:00"], ["名称", "a"]]))
    other = fetch.collect_akshare_contract(
        venue="SHFE",
        contract="RB2702",
        requested_date=date(2026, 11, 13),
        _runner=lambda *_: ("CHILD_OK", packet([["交易时间", "日盘 09:00-10:00"], ["名称", "a"]])),
    )
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", first.source_id)
    assert first.source_id == same.source_id == changed.source_id
    assert first.source_id != other.source_id
    assert first.capture_id != same.capture_id
    assert first.response_digest == same.response_digest != changed.response_digest
    assert first.captured_at == first.candidate.collected_at


def test_injected_unbounded_runner_does_not_claim_timeout():
    def slow(*_):
        time.sleep(0.03)
        return "CHILD_OK", packet([["交易时间", "日盘 09:00-10:00"]])

    result = fetch.collect_akshare_contract(
        venue="SHFE",
        contract="RB2701",
        requested_date=date.today(),
        timeout=0.001,
        _runner=slow,
    )
    assert result.status == "SUCCESS"
    assert result.candidate.timeout_enforced is False


def test_timeout_direct_child_does_not_assert_descendant_cleanup(monkeypatch):
    monkeypatch.setattr(fetch, "_run", lambda *_: ("CHILD_TIMEOUT", None))
    result = fetch.collect_akshare_contract(
        venue="SHFE", contract="RB2701", requested_date=date.today()
    )
    assert result.status == "FETCH_TIMEOUT"
    assert result.candidate.timeout_enforced
    assert result.direct_child_cleanup == "DIRECT_CHILD_REAPED"
    assert result.descendant_cleanup == "NOT_PROVEN"
    assert result.timeout_retry == "NOT_PROVEN_SAFE"


def test_other_duplicate_fields_cannot_produce_usable_time_candidate():
    result = capture(packet([["交易时间", "日盘 09:00-10:00"], ["名称", "a"], ["名称", "b"]]))
    assert result.status == "STRUCTURE_CHANGED"
    assert result.time_candidate is None
    assert result.time_diagnostic.status == "UPSTREAM_NOT_SUCCESS"
    assert result.time_diagnostic.upstream_status == "STRUCTURE_CHANGED"
    assert result.time_diagnostic.candidate.status == "SYNTAX_CANDIDATE"


def test_child_fake_dataframe_and_failure_do_not_echo(monkeypatch, capfd):
    class Frame:
        columns = ["item", "value"]

        def __len__(self):
            return 1

        def itertuples(self, **_):
            return iter([("交易时间", "日盘 09:00-10:00")])

    pandas = types.SimpleNamespace(DataFrame=Frame, NA=object(), NaT=object())

    def good(**kwargs):
        assert kwargs == {"symbol": "RB2701"}
        print("credential=secret /private/path")
        return Frame()

    akshare = types.SimpleNamespace(__version__=fetch.VERSION, futures_contract_detail=good)
    monkeypatch.setitem(sys.modules, "pandas", pandas)
    monkeypatch.setitem(sys.modules, "akshare", akshare)
    fetch._child("RB2701")
    result = capfd.readouterr()
    assert "secret" not in result.out and "/private/path" not in result.out
    assert result.err == ""
    assert fetch._decode(result.out.encode())[1]["rows"][0][0] == "交易时间"

    def broken(**_):
        raise RuntimeError("credential=secret /private/path")

    akshare.futures_contract_detail = broken
    fetch._child("RB2701")
    result = capfd.readouterr()
    assert "secret" not in result.out and "/private/path" not in result.out
    assert result.err == ""
    assert fetch._decode(result.out.encode())[1]["status"] == "FETCH_ERROR"


def test_child_bounds_lying_iterator_and_bad_cells(monkeypatch, capfd):
    class Frame:
        columns = ["item", "value"]
        cell = "ok"
        seen = 0

        def __len__(self):
            return 1

        def itertuples(self, **_):
            while True:
                self.seen += 1
                yield ("x", self.cell)

    frame = Frame()
    monkeypatch.setitem(
        sys.modules, "pandas", types.SimpleNamespace(DataFrame=Frame, NA=object(), NaT=object())
    )
    monkeypatch.setitem(
        sys.modules,
        "akshare",
        types.SimpleNamespace(__version__=fetch.VERSION, futures_contract_detail=lambda **_: frame),
    )
    fetch._child("RB2701")
    assert fetch._decode(capfd.readouterr().out.encode())[1]["status"] == "STRUCTURE_CHANGED"
    assert frame.seen == fetch.MAX_ROWS + 1
    frame.cell, frame.seen = object(), 0
    fetch._child("RB2701")
    assert fetch._decode(capfd.readouterr().out.encode())[1]["status"] == "STRUCTURE_CHANGED"
    assert frame.seen == 1


def test_child_dependency_and_version_handling(monkeypatch, capfd):
    real_import = builtins.__import__

    def without_akshare(name, *args, **kwargs):
        if name == "akshare":
            raise ImportError("/private/credential=secret")
        return real_import(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", without_akshare)
        fetch._child("RB2701")
    out = capfd.readouterr()
    assert "secret" not in out.out and not out.err
    assert fetch._decode(out.out.encode())[1]["status"] == "DEPENDENCY_UNAVAILABLE"

    monkeypatch.setitem(sys.modules, "akshare", types.SimpleNamespace(__version__="0"))
    monkeypatch.setitem(sys.modules, "pandas", types.SimpleNamespace())
    fetch._child("RB2701")
    assert fetch._decode(capfd.readouterr().out.encode())[1]["status"] == "VERSION_MISMATCH"


@pytest.mark.skipif(os.name != "posix", reason="POSIX supervisor")
def test_interrupt_marks_unknown_and_reaps_owned_child(tmp_path):
    pidfile = tmp_path / "pid"
    child = "import os,time,sys;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(30)"
    parent = (
        "import sys; from bt_api_ctp import _calendar_child_supervisor as s\n"
        "try: s.supervise([sys.executable,'-c',sys.argv[1],sys.argv[2]], 10, 100, lambda x: ('OK',x))\n"
        "except KeyboardInterrupt as e: print(getattr(e,'child_cleanup','MISSING'))\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", parent, child, str(pidfile)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid = None
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        pid = int(pidfile.read_text())
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=5)
        assert out.strip() == "CHILD_CLEANUP_UNCERTAIN"
        assert not err
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        pid = None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
