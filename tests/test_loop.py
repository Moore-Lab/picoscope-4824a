"""The shared acquisition thread, with no device involved.

:class:`~picoscope_4824a.loop.AcquisitionLoop` is what keeps a capture
releasable, lets device changes land between captures, and makes stop
reliable. Both the scope's sweeps and DAQ runs depend on it, so it is tested
on its own here against a stand-in whose "capture" simply waits until it is
interrupted — the behaviour of a trigger that never comes, which is the case
every one of these guarantees exists for.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picoscope_4824a.loop import AcquisitionLoop          # noqa: E402


class WaitingLoop(AcquisitionLoop):
    """A loop whose capture never finishes on its own."""

    thread_name = "test-loop"

    def __init__(self):
        super().__init__(scope=None)
        self.passes = 0
        self.hook = None                      # called after each drain

    def start(self):
        with self._lock:
            self._start_thread()

    def _drain_calls(self):
        super()._drain_calls()
        if self.hook is not None:
            self.hook()

    def _work(self):
        for _ in self.iterations():
            self.passes += 1
            while not self._interrupt.is_set() and not self._stopping.is_set():
                time.sleep(0.002)


@pytest.fixture
def loop():
    lp = WaitingLoop()
    yield lp
    lp.stop(timeout_s=2.0)


def test_work_queued_during_a_capture_runs_promptly(loop):
    loop.start()
    time.sleep(0.1)
    t = time.perf_counter()
    assert loop.run_on_loop(lambda: "done", timeout_s=2.0) == "done"
    assert time.perf_counter() - t < 0.5


def test_work_queued_at_the_drain_boundary_is_not_lost(loop):
    """The race the guard in iterations() exists for.

    A call that arrives between the drain and the interrupt being cleared
    would have its wakeup cleared with it, and the next capture — which may
    wait for ever — would strand the caller behind it. Injected exactly there
    rather than hoped for: the window is microseconds wide.
    """
    queued = threading.Event()
    result = []

    def inject_once():
        if queued.is_set():
            return
        queued.set()
        # Another thread, so it takes the lock after this drain returns.
        t = threading.Thread(
            target=lambda: result.append(loop.run_on_loop(lambda: 42, timeout_s=2.0)),
            daemon=True)
        t.start()
        time.sleep(0.05)              # let it append and set the interrupt

    loop.hook = inject_once
    loop.start()
    deadline = time.perf_counter() + 3.0
    while not result and time.perf_counter() < deadline:
        time.sleep(0.01)
    assert result == [42], "a call queued at the drain boundary was swallowed"


def test_results_and_errors_come_back_to_the_caller(loop):
    loop.start()
    time.sleep(0.05)
    assert loop.run_on_loop(lambda: 6 * 7, timeout_s=2.0) == 42

    def boom():
        raise ValueError("no")

    with pytest.raises(ValueError, match="no"):
        loop.run_on_loop(boom, timeout_s=2.0)
    assert loop.running, "a failing call must not take the loop down with it"


def test_calls_run_inline_when_the_loop_is_not_running(loop):
    assert not loop.running
    assert loop.run_on_loop(lambda: "inline", timeout_s=0.1) == "inline"


def test_a_call_racing_the_stop_still_runs(loop):
    """Queued as the loop exits: it runs in the teardown rather than timing out,
    because by then the device is idle and the caller is still waiting."""
    loop.start()
    time.sleep(0.05)
    done = []

    def late():
        done.append(loop.run_on_loop(lambda: "late", timeout_s=3.0))

    t = threading.Thread(target=late, daemon=True)
    t.start()
    loop.stop(timeout_s=2.0)
    t.join(timeout=3.0)
    assert done == ["late"]


def test_stop_releases_a_capture_that_would_wait_for_ever(loop):
    loop.start()
    time.sleep(0.1)
    t = time.perf_counter()
    assert loop.stop(timeout_s=2.0) is True
    assert time.perf_counter() - t < 0.5
    assert not loop.running


def test_stop_is_safe_before_starting_and_twice(loop):
    assert loop.stop() is True
    loop.start()
    time.sleep(0.05)
    assert loop.stop(timeout_s=2.0) is True
    assert loop.stop(timeout_s=2.0) is True


def test_a_crash_in_the_body_still_closes_the_loop_down():
    class Exploding(AcquisitionLoop):
        thread_name = "test-boom"

        def _work(self):
            raise RuntimeError("bang")

    lp = Exploding(scope=None)
    # The crash is deliberate, and the loop deliberately re-raises so a real
    # driver fault reaches the log; keep it out of the test report.
    previous, threading.excepthook = threading.excepthook, lambda args: None
    try:
        with lp._lock:
            lp._start_thread()
        lp._thread.join(timeout=2.0)
    finally:
        threading.excepthook = previous
    assert not lp.running
    assert "bang" in (lp._stopped_reason or "")
    # Accepting work after the thread is gone would hang the caller until its
    # timeout; it has to fall back to running inline.
    assert lp.run_on_loop(lambda: "inline", timeout_s=0.5) == "inline"
