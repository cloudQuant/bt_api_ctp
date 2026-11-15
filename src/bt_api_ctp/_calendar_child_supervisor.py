"""Private cooperative POSIX supervisor for explicit public calendar fetches.

Only the owned child is signalled. Unsupported signal/thread hosts fail before
spawn. SIGTERM, SA_NOCLDWAIT, native reapers, Popen without a returned handle,
power loss and uninterruptible kernel waits are not proven safe or bounded.
"""

import contextlib
import os
import selectors
import signal
import subprocess
import threading
import time


class ChildCleanupUncertain(BaseException):
    """Fallback for an exception that cannot carry the cleanup marker."""


def _mark_uncertain(exc):
    try:
        exc.child_cleanup = "CHILD_CLEANUP_UNCERTAIN"
        if hasattr(exc, "add_note"):
            exc.add_note("CHILD_CLEANUP_UNCERTAIN")
    except BaseException:
        raise ChildCleanupUncertain("CHILD_CLEANUP_UNCERTAIN") from exc


def stop_child(child, status):
    try:
        child.kill()
        child.wait(timeout=1.0)
    except Exception:
        return "CHILD_CLEANUP_UNCERTAIN", None
    return status, None


def _supervise_core(
    command, total_timeout, max_result, decode, *, stop=stop_child, oversize="RESULT_TOO_LARGE"
):
    """Run a fixed child; default signal handlers and a single Python main thread required."""
    if (
        os.name != "posix"
        or not hasattr(signal, "SIGCHLD")
        or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
        or signal.getsignal(signal.SIGINT) != signal.default_int_handler
        or threading.current_thread() is not threading.main_thread()
        or threading.active_count() != 1
    ):
        return "SUPERVISOR_UNSUPPORTED", None
    interrupted = [False]

    def record_interrupt(signum, frame):
        interrupted[0] = True

    def safe_stop(child, status):
        if (
            child.returncode is not None
            or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
            or signal.getsignal(signal.SIGINT) is not record_interrupt
            or threading.current_thread() is not threading.main_thread()
            or threading.active_count() != 1
        ):
            return "CHILD_CLEANUP_UNCERTAIN", None
        return stop(child, status)

    handler_changed = False
    try:
        signal.signal(signal.SIGINT, record_interrupt)
        result = _owned_child(
            command, total_timeout, max_result, decode, oversize, interrupted, safe_stop
        )
    except BaseException as exc:
        _mark_uncertain(exc)
        raise
    finally:
        if signal.getsignal(signal.SIGINT) is record_interrupt:
            signal.signal(signal.SIGINT, signal.default_int_handler)
        else:
            handler_changed = True
    if interrupted[0]:
        exc = KeyboardInterrupt("CHILD_CLEANUP_UNCERTAIN")
        _mark_uncertain(exc)
        raise exc
    if handler_changed:
        return "CHILD_CLEANUP_UNCERTAIN", None
    return result


def supervise(
    command, total_timeout, max_result, decode, *, stop=stop_child, oversize="RESULT_TOO_LARGE"
):
    try:
        return _supervise_core(
            command, total_timeout, max_result, decode, stop=stop, oversize=oversize
        )
    except BaseException as exc:
        _mark_uncertain(exc)
        raise


def _owned_child(command, total_timeout, max_result, decode, oversize, interrupted, stop):
    deadline = time.monotonic() + total_timeout
    child = None
    wait_in_progress = False
    cleanup_in_progress = False

    def stop_once(status):
        nonlocal cleanup_in_progress
        cleanup_in_progress = True
        result = stop(child, status)
        cleanup_in_progress = False
        return result

    def interrupt():
        stop_once("CHILD_CLEANUP_UNCERTAIN")
        raise KeyboardInterrupt("CHILD_CLEANUP_UNCERTAIN")

    try:
        if interrupted[0]:
            raise KeyboardInterrupt("CHILD_CLEANUP_UNCERTAIN")
        try:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={},
                shell=False,
            )
        except Exception:
            return "CHILD_CLEANUP_UNCERTAIN", None
        if interrupted[0]:
            interrupt()
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                if interrupted[0]:
                    interrupt()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return stop_once("CHILD_TIMEOUT")
                if not selector.select(min(remaining, 0.2)):
                    continue
                part = os.read(child.stdout.fileno(), min(65536, max_result + 1 - len(output)))
                if interrupted[0]:
                    interrupt()
                if not part:
                    break
                output.extend(part)
                if len(output) > max_result:
                    return stop_once(oversize)
        while True:
            if interrupted[0]:
                interrupt()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return stop_once("CHILD_TIMEOUT")
            try:
                wait_in_progress = True
                child.wait(timeout=min(remaining, 0.2))
                wait_in_progress = False
                break
            except subprocess.TimeoutExpired:
                wait_in_progress = False
        if interrupted[0]:
            raise KeyboardInterrupt("CHILD_CLEANUP_UNCERTAIN")
        if child.returncode != 0:
            return "CHILD_ERROR", None
        return decode(bytes(output))
    except Exception:
        return (
            ("CHILD_CLEANUP_UNCERTAIN", None)
            if wait_in_progress or cleanup_in_progress
            else stop_once("CHILD_ERROR")
        )
    except BaseException as exc:
        if (
            child is not None
            and not wait_in_progress
            and not cleanup_in_progress
            and not interrupted[0]
            and child.returncode is None
        ):
            with contextlib.suppress(BaseException):
                stop(child, "CHILD_CLEANUP_UNCERTAIN")
        _mark_uncertain(exc)
        raise
    finally:
        if child is not None and child.stdout is not None:
            child.stdout.close()
