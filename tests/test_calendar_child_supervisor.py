"""Owned-child interruption tests, in isolated interpreters, no network."""

import subprocess
import sys

import pytest

_SCRIPT = r"""
import os, signal, subprocess, sys, threading
from bt_api_ctp import calendar_shfe_fetch as hours
from bt_api_ctp import calendar_shfe_product_fetch as product
from bt_api_ctp import calendar_shfe_detail_fetch as detail
from bt_api_ctp import _calendar_child_supervisor as sup
entry, mode = sys.argv[1:]
original = subprocess.Popen
original_handler = signal.getsignal(signal.SIGINT)
children, kills = [], []
external = lambda *_: None

def call():
    if entry == "hours": return hours.collect_shfe_page(total_timeout=.4)
    if entry == "holiday": return hours.collect_shfe_holiday(total_timeout=.4)
    if entry == "product": return product.collect_product_page(source_key="CU_FUTURE", total_timeout=.4)
    return detail.collect_detail_page(path="/products/futures/metal/nonferrousmetal/cu_f/", total_timeout=.4)

def forbidden(*args, **kwargs): raise AssertionError("child launched")
if mode in {"ignored", "custom_int", "multithread"}:
    if mode == "ignored": signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    if mode == "custom_int": signal.signal(signal.SIGINT, external)
    subprocess.Popen = forbidden
    if mode == "multithread":
        event=threading.Event(); thread=threading.Thread(target=event.wait); thread.start()
        try: result = call()
        finally: event.set(); thread.join()
    else: result = call()
    assert result.transport_status == "SUPERVISOR_UNSUPPORTED"
    assert result.transport_assurance == "SUPERVISED_DEFAULT_TLS_POLICY"
else:
    def spawn(command, **kwargs):
        code = "pass" if mode in {"after_wait", "restore_after"} else "import time; time.sleep(30)"
        child = original([sys.executable, "-c", code], **kwargs)
        children.append(child)
        child.actual_kill, child.actual_wait = child.kill, child.wait
        def counted(): kills.append(True); child.actual_kill()
        child.kill = counted
        if mode == "popen_interrupt": os.kill(os.getpid(), signal.SIGINT)
        if mode in {"systemexit", "handler_changed"}:
            if mode == "handler_changed": signal.signal(signal.SIGINT, external)
            def exit_handler(*_): raise SystemExit(37)
            signal.signal(signal.SIGUSR1, exit_handler)
            signal.signal(signal.SIGALRM, lambda *_: os.kill(os.getpid(), signal.SIGUSR1))
            signal.setitimer(signal.ITIMER_REAL, .1)
        if mode == "no_handle": raise KeyboardInterrupt("injected")
        if mode == "after_wait":
            def wait(*args, **kw):
                child.actual_wait(*args, **kw)
                raise SystemExit(37)
            child.wait = wait
        if mode == "restore_after":
            real_signal = signal.signal
            def during_restore(signum, handler):
                result = real_signal(signum, handler)
                if signum == signal.SIGINT and handler is original_handler:
                    os.kill(os.getpid(), signal.SIGINT)
                return result
            signal.signal = during_restore
        if mode == "stop_wait":
            def wait(*args, **kw): raise SystemExit(37)
            child.wait = wait
        return child
    subprocess.Popen = spawn
    try:
        if mode == "timeout":
            result = call()
            assert result.transport_status == "CHILD_TIMEOUT"
            assert len(kills) == 1 and children[0].poll() is not None
        else:
            try: call()
            except BaseException as exc:
                expected = KeyboardInterrupt if mode in {"popen_interrupt", "no_handle", "restore_after"} else SystemExit
                assert type(exc) is expected
                assert exc.child_cleanup == "CHILD_CLEANUP_UNCERTAIN"
                if isinstance(exc, SystemExit): assert exc.code == 37
            else: raise AssertionError("interrupt swallowed")
            if mode in {"popen_interrupt", "systemexit"}: assert len(kills) == 1 and children[0].poll() is not None
            if mode in {"no_handle", "handler_changed"}: assert not kills and children[0].poll() is None
            if mode in {"after_wait", "restore_after"}: assert not kills and children[0].poll() is not None
            if mode == "stop_wait": assert len(kills) == 1
        assert signal.getsignal(signal.SIGINT) is (external if mode == "handler_changed" else original_handler)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for child in children:
            if child.poll() is None:
                child.actual_kill()
                child.actual_wait(timeout=3)
"""


@pytest.mark.parametrize("entry", ["hours", "holiday", "product", "detail"])
@pytest.mark.parametrize(
    "mode",
    [
        "ignored",
        "custom_int",
        "multithread",
        "timeout",
        "popen_interrupt",
        "systemexit",
        "handler_changed",
        "no_handle",
        "after_wait",
        "restore_after",
        "stop_wait",
    ],
)
def test_entry_supervision_with_only_test_owned_children(entry, mode):
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, entry, mode], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
