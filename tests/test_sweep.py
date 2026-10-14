"""Triggered sweeps — scope mode — against the simulator.

Two kinds of claim are pinned here. The ones about *this code* (sweeps stand
still, normal mode waits, settings re-arm a waiting capture, device changes
land between captures, shutdown never strands the driver) run on the
simulator. The one about *the hardware* — how to tell a triggered capture from
an auto-triggered one — cannot be simulated honestly, so it is pinned to the
numbers the rig's 4824A actually produced when it was probed (2026-09-16).

None of this needs a scope attached.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picoscope_4824a.controller import ScopeController          # noqa: E402
from picoscope_4824a.ps4000a import Channel, PicoError           # noqa: E402
from picoscope_4824a.ps4000a import ThresholdDirection as D      # noqa: E402
from picoscope_4824a.scope import (CaptureCancelled, TriggerConfig,  # noqa: E402
                                   classify_trigger)
from picoscope_4824a.sim import SimScope                         # noqa: E402
from picoscope_4824a.sweep import SweepConfig, envelope, measure  # noqa: E402

# The simulator's channel A: 100 Hz, 0.3 x full scale (1.5 V on ±5 V), plus
# Gaussian noise of 0.2 % of full scale (10 mV).
NOISE_TOLERANCE_V = 0.06


def wait_for(predicate, timeout=3.0):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if predicate():
            return True
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.fixture
def ctrl(tmp_path):
    c = ScopeController(simulate=True, recordings_dir=str(tmp_path),
                        presets_dir=str(tmp_path / "presets"))
    assert c.open()["ok"]
    yield c
    c.shutdown()


# --------------------------------------------------------------------------
# triggered or auto? — pinned to the rig
# --------------------------------------------------------------------------

@pytest.mark.parametrize("case, elapsed_s, collect_s, auto_ms, triggered", [
    # 4000 samples at 1 MS/s, half pre-trigger (4 ms of collection).
    ("level far above the signal, 300 ms auto", 0.333, 0.004, 300, False),
    ("level far above the signal, 1 s auto", 1.023, 0.004, 1000, False),
    # The level sat inside the noise: plenty of software crossings in the
    # buffer, one right at the trigger index, but the hardware hysteresis was
    # never armed and the capture came at the timeout. A crossing test would
    # call this triggered.
    ("level inside the noise band, 3 s auto", 3.040, 0.004, 3000, False),
    ("rare falling edge, fired at 1.8 s", 1.800, 0.004, 3000, True),
    # 4000 samples at 1 kS/s, 2 s of it pre-trigger: the auto timer only starts
    # once the pre-trigger buffer is full, so these took pre + auto + post.
    ("slow sweep, 500 ms auto", 4.64, 4.0, 500, False),
    ("slow sweep, 3.5 s auto", 7.63, 4.0, 3500, False),
])
def test_trigger_classification_matches_the_rig(case, elapsed_s, collect_s,
                                                 auto_ms, triggered):
    trig = TriggerConfig(enabled=True, auto_trigger_ms=auto_ms)
    assert classify_trigger(trig, max(0.0, elapsed_s - collect_s)) is triggered, case


def test_classification_without_a_timer():
    assert classify_trigger(TriggerConfig(enabled=False), 5.0) is None
    # auto_trigger_ms = 0: the driver only returns on a real trigger.
    assert classify_trigger(TriggerConfig(enabled=True, auto_trigger_ms=0), 99.0) is True


# --------------------------------------------------------------------------
# the simulator's trigger
# --------------------------------------------------------------------------

@pytest.fixture
def sim():
    s = SimScope()
    s.open()
    return s


@pytest.mark.parametrize("level, direction", [
    (0.5, D.RISING), (-1.0, D.FALLING), (1.2, D.RISING_OR_FALLING)])
def test_sim_trigger_lands_on_the_pre_trigger_index(sim, level, direction):
    sim.set_simple_trigger(True, Channel.A, level, direction, 0, 0)
    for _ in range(3):
        cap = sim.capture_block(n_samples=2000, sample_rate_hz=100_000,
                                pre_trigger_samples=1000, timeout_s=2)
        v = cap.volts["A"]
        assert cap.trigger_index == 1000 and cap.triggered is True
        assert abs(v[1000] - level) < NOISE_TOLERANCE_V
        slope = float(v[1015] - v[985])
        if direction is D.RISING:
            assert slope > 0
        elif direction is D.FALLING:
            assert slope < 0


def test_sim_auto_trigger_fires_after_the_timeout(sim):
    sim.set_simple_trigger(True, Channel.A, 3.0, D.RISING, 0, 150)
    t = time.perf_counter()
    cap = sim.capture_block(n_samples=2000, sample_rate_hz=100_000,
                            pre_trigger_samples=1000, timeout_s=2)
    elapsed = time.perf_counter() - t
    assert cap.triggered is False
    assert cap.wait_s == pytest.approx(0.150, abs=0.005)
    # pre-trigger fill (10 ms) + auto (150 ms) + post-trigger (10 ms)
    assert 0.16 <= elapsed < 0.4


def test_capture_timeout_is_wall_clock(sim):
    """The old loop counted each ~14 ms Event.wait as 1 ms: a "10 s" timeout
    ran for about 140 s. The timeout is now a real deadline."""
    sim.set_simple_trigger(True, Channel.A, 3.0, D.RISING, 0, 0)
    t = time.perf_counter()
    with pytest.raises(PicoError):
        sim.capture_block(n_samples=2000, sample_rate_hz=100_000,
                          pre_trigger_samples=1000, timeout_s=0.3)
    assert time.perf_counter() - t < 0.6


def test_a_waiting_capture_can_be_cancelled(sim):
    sim.set_simple_trigger(True, Channel.A, 3.0, D.RISING, 0, 0)
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    t = time.perf_counter()
    with pytest.raises(CaptureCancelled):
        sim.capture_block(n_samples=2000, sample_rate_hz=100_000,
                          pre_trigger_samples=1000, timeout_s=None, cancel=cancel)
    assert time.perf_counter() - t < 0.5


# --------------------------------------------------------------------------
# the scope loop
# --------------------------------------------------------------------------

def test_triggered_sweeps_stand_still(ctrl):
    assert ctrl.start_sweep(mode="normal", level=0.5, time_per_div=1e-3)["ok"]
    seen, values = set(), []
    while len(values) < 5:
        wait_for(lambda: ctrl.sweep.latest() is not None
                 and ctrl.sweep.latest().id not in seen)
        sw = ctrl.sweep.latest()
        seen.add(sw.id)
        assert sw.status == "triggered"
        values.append(float(sw.capture.volts["A"][sw.trigger_index]))
    assert max(abs(v - 0.5) for v in values) < NOISE_TOLERANCE_V


def test_normal_mode_waits_and_auto_mode_free_runs(ctrl):
    ctrl.start_sweep(mode="normal", level=3.0, time_per_div=1e-3)
    time.sleep(0.5)
    st = ctrl.sweep.state()
    assert st["sweeps"] == 0 and st["indicator"] == "wait"

    ctrl.set_sweep(mode="auto")
    wait_for(lambda: ctrl.sweep.state()["sweeps"] >= 2)
    assert ctrl.sweep.latest().status == "auto"
    assert ctrl.sweep.state()["indicator"] == "auto"


def test_new_settings_release_a_waiting_capture(ctrl):
    ctrl.start_sweep(mode="normal", level=3.0, time_per_div=1e-3)
    time.sleep(0.3)
    t = time.perf_counter()
    ctrl.set_sweep(level=0.0)
    wait_for(lambda: ctrl.sweep.state()["sweeps"] >= 1, timeout=1.0)
    assert time.perf_counter() - t < 0.5
    assert ctrl.sweep.latest().config.level == 0.0


def test_single_stops_after_one_triggered_sweep(ctrl):
    assert ctrl.start_sweep(mode="single", level=0.0, time_per_div=1e-3)["ok"]
    wait_for(lambda: not ctrl.sweep.running)
    st = ctrl.sweep.state()
    assert st["sweeps"] == 1
    assert st["stopped_reason"] == "single sweep captured"
    assert st["indicator"] == "stop"
    assert ctrl.sweep.latest().status == "triggered"


def test_device_changes_land_between_captures_even_mid_wait(ctrl):
    """A normal-mode capture that never fires must not block a range change."""
    ctrl.start_sweep(mode="normal", level=3.0, time_per_div=1e-3)
    time.sleep(0.2)
    t = time.perf_counter()
    assert ctrl.set_channel("A", True, range="R_2V")["ok"]
    assert time.perf_counter() - t < 0.5
    assert ctrl.set_siggen(wave="SQUARE", frequency=500, amplitude=1.0)["ok"]
    assert ctrl.siggen_off()["ok"]
    assert ctrl.scope.channels[Channel.A].voltage_range.name == "R_2V"
    assert ctrl.sweep.running


def test_one_acquisition_at_a_time(ctrl):
    assert ctrl.start_sweep(time_per_div=1e-3)["ok"]
    for name, result in (("stream", ctrl.start_stream(rate=1e4)),
                         ("capture", ctrl.capture_block(samples=100)),
                         ("trigger-set", ctrl.set_trigger(threshold=1.0)),
                         ("second run", ctrl.start_sweep())):
        assert not result["ok"], f"{name} was allowed while sweeping"
    assert ctrl.stop_sweep()["clean"]

    assert ctrl.start_stream(rate=1e4)["ok"]
    assert not ctrl.start_sweep()["ok"]
    assert ctrl.stop_stream()["ok"]


def test_the_trigger_source_must_be_on(ctrl):
    result = ctrl.start_sweep(source="B")
    assert not result["ok"] and "switched off" in result["error"]
    # Free-run needs no source at all.
    assert ctrl.start_sweep(mode="none")["ok"]


def test_window_follows_position_and_timebase(ctrl):
    ctrl.start_sweep(mode="auto", level=0.0, time_per_div=2e-3, position=10)
    wait_for(lambda: ctrl.sweep.latest() is not None)
    out = ctrl.sweep_trace(max_points=200)
    assert out["ok"]
    d = out["sweep"]
    assert d["t_start_s"] == pytest.approx(-2e-3, rel=0.02)    # 10 % of 20 ms
    assert d["t_end_s"] == pytest.approx(18e-3, rel=0.02)
    assert d["bins"] == 200 and len(d["traces"]["A"]["max"]) == 200


def test_since_skips_sweeps_already_seen(ctrl):
    ctrl.start_sweep(mode="single", level=0.0, time_per_div=1e-3)
    wait_for(lambda: not ctrl.sweep.running)
    last = ctrl.sweep.latest().id
    assert ctrl.sweep_trace(since=last - 1)["sweep"]["id"] == last
    unchanged = ctrl.sweep_trace(since=last)
    assert unchanged["sweep"] is None and unchanged["state"]["last_id"] == last


def test_measurements_on_a_known_tone(ctrl):
    # 50 ms on screen: five periods of the 100 Hz tone.
    ctrl.start_sweep(mode="auto", level=0.0, time_per_div=5e-3, samples=5000)
    wait_for(lambda: ctrl.sweep.latest() is not None)
    m = ctrl.sweep.latest().measurements()["A"]
    assert m["freq_hz"] == pytest.approx(100.0, rel=0.01)
    assert m["vpp"] == pytest.approx(3.0, rel=0.05)
    assert m["rms"] == pytest.approx(1.5 / 2 ** 0.5, rel=0.03)


def test_no_frequency_is_reported_for_noise():
    # The rig's real noise floor on ±5 V: 1.4 mV rms.
    v = np.random.default_rng(0).normal(0.0, 0.0014, 4000)
    assert measure(v, 1e-6, 5.0)["freq_hz"] is None


def test_envelope_keeps_a_one_sample_glitch():
    v = np.zeros(100_000)
    v[54_321] = 1.0
    lo, hi = envelope(v, 500)
    assert hi.size == 500 and hi.max() == 1.0 and lo.min() == 0.0


def test_saved_sweep_measures_time_from_the_trigger(ctrl):
    ctrl.start_sweep(mode="single", level=0.5, time_per_div=1e-3)
    wait_for(lambda: not ctrl.sweep.running)
    saved = ctrl.save_sweep(name="t", format="csv")
    assert saved["ok"], saved
    data = np.loadtxt(saved["saved"], delimiter=",", skiprows=1)
    with open(saved["saved"], encoding="utf-8") as fh:
        assert fh.readline().startswith("time_from_trigger_s,")
    at_zero = data[np.argmin(np.abs(data[:, 0])), 1]
    assert abs(data[:, 0]).min() < 1e-9
    assert abs(at_zero - 0.5) < NOISE_TOLERANCE_V
    assert any(r["name"] == os.path.basename(saved["saved"])
               for r in ctrl.list_recordings()["recordings"])


def test_scope_settings_survive_a_preset_round_trip(ctrl):
    ctrl.set_sweep(mode="normal", level=0.25, time_per_div=5e-4, position=20,
                   direction="FALLING")
    assert ctrl.save_preset("scope1")["ok"]
    ctrl.set_sweep(mode="auto", level=0.0, time_per_div=1e-3, position=50,
                   direction="RISING")
    assert ctrl.load_preset("scope1")["ok"]
    cfg = ctrl.sweep.config
    assert (cfg.mode, cfg.level, cfg.time_per_div, cfg.position, cfg.direction) == (
        "normal", 0.25, 5e-4, 20.0, D.FALLING)


def test_shutdown_releases_a_scope_waiting_forever(ctrl):
    """The teardown that matters most: a normal-mode capture parked in the
    driver on a trigger that will never come."""
    ctrl.start_sweep(mode="normal", level=3.0, time_per_div=1e-3)
    time.sleep(0.2)
    t = time.perf_counter()
    ctrl.shutdown()
    assert time.perf_counter() - t < 1.0
    assert not ctrl.sweep.running
    assert not ctrl.scope.is_open


@pytest.mark.parametrize("bad", [
    {"mode": "roll"}, {"time_per_div": 100.0}, {"position": 101},
    {"samples": 5}, {"auto_ms": 40000}, {"level": float("nan")},
    {"direction": "NONE"}, {"source": "Z"},
])
def test_invalid_settings_are_refused(bad):
    with pytest.raises(ValueError):
        SweepConfig().updated(**bad)


def test_unknown_settings_are_refused():
    with pytest.raises(TypeError):
        SweepConfig().updated(colour="red")
