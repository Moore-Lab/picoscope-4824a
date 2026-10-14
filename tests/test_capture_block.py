"""The *hardware* block-capture path, driven by a stand-in for ps4000a.dll.

The simulator has its own capture_block, so the sweep tests never touch the
code that actually runs on the rig. This does: :class:`PicoScope4824A` is built
with a fake driver that implements just the block-mode calls and becomes
"ready" when told to, so the polling loop, the wall-clock timeout, the
cancellation path and the triggered/auto classification are all exercised
exactly as they run against the real DLL — with no scope and no DLL needed.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picoscope_4824a import ps4000a as ps                       # noqa: E402
from picoscope_4824a.ps4000a import Channel, PicoError           # noqa: E402
from picoscope_4824a.ps4000a import ThresholdDirection as D      # noqa: E402
from picoscope_4824a.scope import CaptureCancelled, PicoScope4824A  # noqa: E402

N = 1000                   # samples per capture
RATE = 1e6                 # -> timebase 79, 1000 ns: 1 ms of collection
PRE = 400


class FakeDriver:
    """Just enough of ps4000a.dll for :meth:`PicoScope4824A.capture_block`.

    A block becomes ready *ready_after* seconds after RunBlock (never, if
    None), and GetValues writes a known value into every registered buffer.
    """

    def __init__(self, ready_after):
        self.ready_after = ready_after
        self.t_run = None
        self.pre = None
        self.buffers = []
        self.calls = []

    def _note(self, name):
        self.calls.append(name)
        return ps.PICO_OK

    def ps4000aGetTimebase2(self, handle, timebase, n, interval, max_samples, seg):
        interval._obj.value = ps.timebase_to_interval_ns(timebase.value)
        max_samples._obj.value = 1 << 20
        return self._note("GetTimebase2")

    def ps4000aSetDataBuffer(self, handle, channel, ptr, n, seg, mode):
        self.buffers.append((ptr, n.value))
        return self._note("SetDataBuffer")

    def ps4000aSetSimpleTrigger(self, *args):
        return self._note("SetSimpleTrigger")

    def ps4000aRunBlock(self, handle, pre, post, timebase, indisposed, seg, cb, param):
        self.t_run = time.perf_counter()
        self.pre = pre.value
        interval_s = ps.timebase_to_interval_ns(timebase.value) * 1e-9
        indisposed._obj.value = int(round((pre.value + post.value) * interval_s * 1000))
        return self._note("RunBlock")

    def ps4000aIsReady(self, handle, ready):
        due = (self.ready_after is not None
               and time.perf_counter() - self.t_run >= self.ready_after)
        ready._obj.value = 1 if due else 0
        return ps.PICO_OK

    def ps4000aGetValues(self, handle, start, got, ratio, mode, seg, overflow):
        for ptr, n in self.buffers:
            for i in range(n):
                ptr[i] = 1000
        overflow._obj.value = 0
        return self._note("GetValues")

    def ps4000aStop(self, handle):
        return self._note("Stop")


def make_scope(monkeypatch, ready_after):
    driver = FakeDriver(ready_after)
    monkeypatch.setattr(ps, "load_library", lambda *a, **k: driver)
    scope = PicoScope4824A()
    scope._open = True                    # as if ps4000aOpenUnit had succeeded
    return scope, driver


def arm(scope, auto_ms):
    scope.set_simple_trigger(True, Channel.A, 0.5, D.RISING, 0, auto_ms)


def capture(scope, **kw):
    kw.setdefault("timeout_s", 5.0)
    return scope.capture_block(n_samples=N, sample_rate_hz=RATE,
                               pre_trigger_samples=PRE, **kw)


def test_normal_mode_capture_is_triggered_at_the_pre_trigger_index(monkeypatch):
    scope, driver = make_scope(monkeypatch, ready_after=0.05)
    arm(scope, auto_ms=0)
    cap = capture(scope)
    assert driver.pre == PRE
    assert cap.trigger_index == PRE
    assert cap.triggered is True
    assert cap.n_samples == N
    assert cap.volts["A"][0] == pytest.approx(1000 * 5.0 / ps.MAX_ADC)
    assert cap.wait_s == pytest.approx(0.05 - 0.001, abs=0.02)
    assert driver.calls[-2:] == ["GetValues", "Stop"]


def test_a_capture_back_before_the_auto_timeout_was_triggered(monkeypatch):
    scope, _ = make_scope(monkeypatch, ready_after=0.001 + 0.05)
    arm(scope, auto_ms=300)
    assert capture(scope).triggered is True


def test_a_capture_back_at_the_auto_timeout_was_not(monkeypatch):
    scope, _ = make_scope(monkeypatch, ready_after=0.001 + 0.2)
    arm(scope, auto_ms=150)
    cap = capture(scope)
    assert cap.triggered is False
    assert cap.wait_s >= 0.15


def test_trigger_off_means_no_trigger_facts(monkeypatch):
    scope, _ = make_scope(monkeypatch, ready_after=0.01)
    scope.set_simple_trigger(False)
    cap = capture(scope)
    assert cap.trigger_index is None and cap.triggered is None


def test_the_timeout_is_a_wall_clock_deadline(monkeypatch):
    """The old loop added 1 ms per ~14 ms Event.wait, so this would have run
    for several seconds before giving up."""
    scope, driver = make_scope(monkeypatch, ready_after=None)
    arm(scope, auto_ms=0)
    t = time.perf_counter()
    with pytest.raises(PicoError):
        capture(scope, timeout_s=0.3)
    assert time.perf_counter() - t < 0.6
    assert driver.calls[-1] == "Stop"
    assert "GetValues" not in driver.calls


def test_cancelling_stops_the_driver_and_reads_nothing(monkeypatch):
    scope, driver = make_scope(monkeypatch, ready_after=None)
    arm(scope, auto_ms=0)
    cancel = threading.Event()
    threading.Timer(0.1, cancel.set).start()
    t = time.perf_counter()
    with pytest.raises(CaptureCancelled):
        capture(scope, timeout_s=None, cancel=cancel)
    assert time.perf_counter() - t < 0.4
    assert driver.calls[-1] == "Stop"
    assert "GetValues" not in driver.calls


def test_pre_trigger_must_leave_a_post_trigger_sample(monkeypatch):
    scope, _ = make_scope(monkeypatch, ready_after=0.01)
    with pytest.raises(ValueError):
        scope.capture_block(n_samples=N, sample_rate_hz=RATE, pre_trigger_samples=N)
