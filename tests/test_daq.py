"""DAQ mode and the recordings library, on the simulator.

DAQ mode records a dataset, so the things worth pinning are the ones that
would quietly spoil one: the record length, the rate actually written into the
files, that a run's configuration cannot change underneath it, and that a
half-written file never appears to a reader.
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picoscope_4824a import library, usphere_h5     # noqa: E402
from picoscope_4824a.controller import ScopeController   # noqa: E402
from picoscope_4824a.daq import DaqConfig            # noqa: E402

pytest.importorskip("h5py")
pytest.importorskip("scipy.signal")


def wait_for(predicate, timeout=20.0):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def ctrl(tmp_path):
    c = ScopeController(simulate=True, recordings_dir=str(tmp_path),
                        presets_dir=str(tmp_path / "presets"))
    assert c.open()["ok"]
    yield c
    c.shutdown()


def run_once(ctrl, **kwargs):
    kwargs.setdefault("bits", 12)
    kwargs.setdefault("rate", 50_000.0)
    kwargs.setdefault("files", 2)
    kwargs.setdefault("name", "run")
    result = ctrl.start_daq(**kwargs)
    assert result["ok"], result
    wait_for(lambda: not ctrl.daq.running)
    return ctrl.daq_status()["daq"]


# --- settings --------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"n_bits": 1}, {"n_bits": 26}, {"sample_rate": 0}, {"n_files": -1},
    {"trigger": "sometimes"}, {"pretrigger": 101}, {"auto_ms": 99999},
    {"source": "Z"}, {"basename": "***"},
])
def test_invalid_settings_are_refused(bad):
    with pytest.raises(ValueError):
        DaqConfig().updated(**bad)


def test_unknown_settings_are_refused():
    with pytest.raises(TypeError):
        DaqConfig().updated(sample_rat=1000)


def test_record_length_is_a_power_of_two():
    cfg = DaqConfig().updated(n_bits=20, sample_rate=10_000.0)
    assert cfg.n_samples == 1_048_576
    assert cfg.requested_duration_s == pytest.approx(104.8576)


# --- planning --------------------------------------------------------------

def test_the_plan_reports_the_rate_that_will_be_written(ctrl):
    """A requested rate the timebase cannot give is rounded, and the files
    record what was really used — so the plan has to say so."""
    out = ctrl.plan_daq(bits=12, rate=33_333.0, files=1)
    assert out["ok"], out
    pr = out["projection"]
    actual = pr["plan"]["actual_rate_hz"]
    assert pr["plan"]["n_samples"] == 4096
    assert actual != 33_333.0
    assert any("timebase gives" in w for w in pr["warnings"])
    # and that is the number the file ends up with
    state = run_once(ctrl, bits=12, rate=33_333.0, files=1)
    path = os.path.join(state["directory"], "run_0.h5")
    assert usphere_h5.read_attrs(path)["Fsamp"] == pytest.approx(actual)


def test_the_plan_warns_before_the_disk_fills(ctrl, monkeypatch):
    monkeypatch.setattr("picoscope_4824a.daq.free_bytes", lambda *_: 50_000_000)
    pr = ctrl.plan_daq(bits=22, rate=1e6, files=20)["projection"]
    assert any("only" in w and "free" in w for w in pr["warnings"])


def test_mixed_channel_ranges_are_flagged(ctrl):
    ctrl.set_channel("B", True, range="R_100MV")
    pr = ctrl.plan_daq(bits=12, rate=50_000.0, files=1)["projection"]
    assert any("different ranges" in w for w in pr["warnings"])


# --- a run -----------------------------------------------------------------

def test_a_run_writes_the_files_usphere_expects(ctrl):
    state = run_once(ctrl, files=3, name="noise")
    assert state["files_written"] == 3
    names = sorted(os.listdir(state["directory"]))
    assert names == ["noise_0.h5", "noise_1.h5", "noise_2.h5"]
    assert not any(n.endswith(".part") for n in names), "a part file was left behind"

    path = os.path.join(state["directory"], names[0])
    ok, msg = usphere_h5.check_schema(path)
    assert ok, msg
    info = usphere_h5.describe(path)
    assert info["n_samples"] == 4096
    assert info["channels"] == ["ai0"]            # channel A only, by default
    assert info["picoscope"]["instrument"] == "PicoScope 4824A"
    assert info["picoscope"]["trigger_mode"] == "off"


def test_each_run_gets_its_own_folder(ctrl):
    first = run_once(ctrl, files=1, name="a")["directory"]
    second = run_once(ctrl, files=1, name="a")["directory"]
    assert first != second, "a second run must not overwrite the first"
    assert os.path.basename(first).startswith("a_")


def test_an_explicit_folder_is_honoured(ctrl):
    state = run_once(ctrl, files=1, name="x", directory="mydata")
    assert os.path.basename(state["directory"]) == "mydata"


def test_a_named_folder_is_not_written_over(ctrl):
    """Re-running into the same folder with the same root name would replace
    the first run's files one by one."""
    run_once(ctrl, files=1, name="x", directory="mydata2")
    again = ctrl.start_daq(bits=12, rate=50_000.0, files=1, name="x",
                           directory="mydata2")
    assert not again["ok"] and "never overwritten" in again["error"]
    assert ctrl.start_daq(bits=12, rate=50_000.0, files=1, name="y",
                          directory="mydata2")["ok"]
    wait_for(lambda: not ctrl.daq.running)


def test_a_run_until_stopped_keeps_going_until_stopped(ctrl):
    assert ctrl.start_daq(bits=10, rate=50_000.0, files=0, name="cont")["ok"]
    wait_for(lambda: ctrl.daq_status()["daq"]["files_written"] >= 2, timeout=15)
    assert ctrl.daq.running
    out = ctrl.stop_daq()
    assert out["ok"] and out["clean"]
    assert not ctrl.daq.running


def test_the_configuration_cannot_change_under_a_run(ctrl):
    """Every file in a run is supposed to have been taken the same way."""
    assert ctrl.start_daq(bits=14, rate=50_000.0, files=0, name="hold")["ok"]
    try:
        for what, result in (("channel", ctrl.set_channel("B", True)),
                             ("siggen", ctrl.set_siggen(frequency=100)),
                             ("trigger", ctrl.set_trigger(threshold=1.0)),
                             ("sweep", ctrl.start_sweep()),
                             ("stream", ctrl.start_stream()),
                             ("capture", ctrl.capture_block(samples=64)),
                             ("second run", ctrl.start_daq())):
            assert not result["ok"], f"{what} was allowed during a DAQ run"
            assert "DAQ run" in result["error"]
    finally:
        ctrl.stop_daq()
    assert ctrl.set_channel("B", True)["ok"], "and allowed again once it stops"


def test_the_preview_carries_the_waveform_the_spectrum_and_the_average(ctrl):
    state = run_once(ctrl, files=3, name="prev")
    assert state["average_n"] == 3
    out = ctrl.daq_preview(max_points=256)
    assert out["ok"]
    pv = out["preview"]
    assert pv["file"] == "prev_2.h5"
    assert pv["channels"] == ["A"]
    assert len(pv["time"]["A"]["max"]) == 256
    assert pv["spectrum"]["A"]["f"][0] > 0
    assert pv["average_n"] == 3 and "A" in pv["average"]
    assert pv["units"] == "V/√Hz"
    assert pv["measurements"]["A"]["rms"] > 0
    # 'since' skips a preview already seen
    assert ctrl.daq_preview(since=pv["id"])["preview"] is None


def test_a_normal_trigger_waits_and_writes_nothing(ctrl):
    """3 V never crosses the simulator's 1.5 V tone."""
    assert ctrl.start_daq(bits=12, rate=50_000.0, files=1, name="wait",
                          trigger="normal", level=3.0)["ok"]
    time.sleep(0.6)
    state = ctrl.daq_status()["daq"]
    assert state["running"] and state["phase"] == "waiting"
    assert state["files_written"] == 0
    assert state["capture"]["fraction"] is None, "waiting has no progress to show"
    t = time.perf_counter()
    ctrl.stop_daq()
    assert time.perf_counter() - t < 1.0


def test_an_auto_trigger_records_anyway(ctrl):
    state = run_once(ctrl, bits=12, rate=50_000.0, files=1, name="auto",
                     trigger="auto", level=3.0, auto_ms=150)
    assert state["files_written"] == 1
    path = os.path.join(state["directory"], "auto_0.h5")
    assert usphere_h5.read_module(path, usphere_h5.PICOSCOPE_MODULE)["triggered"] is False


def test_shutdown_releases_a_run_waiting_for_a_trigger(ctrl):
    ctrl.start_daq(bits=12, rate=50_000.0, files=0, name="hang",
                   trigger="normal", level=3.0)
    time.sleep(0.3)
    t = time.perf_counter()
    ctrl.shutdown()
    assert time.perf_counter() - t < 1.5
    assert not ctrl.daq.running and not ctrl.scope.is_open


# --- the library -----------------------------------------------------------

def test_the_library_lists_runs_and_their_files(ctrl, tmp_path):
    state = run_once(ctrl, files=2, name="lib")
    folder = os.path.basename(state["directory"])
    root = ctrl.list_files()
    assert root["ok"]
    assert any(f["name"] == folder and f["files"] == 2 for f in root["folders"])
    inside = ctrl.list_files(dir=folder)
    assert [f["name"] for f in inside["files"]] == ["lib_0.h5", "lib_1.h5"]
    assert inside["files"][0]["labels"] == {"ai0": "A"}
    assert inside["parent"] == "."


def test_a_file_that_is_not_a_usphere_file_is_named_not_hidden(ctrl, tmp_path):
    (tmp_path / "stray.h5").write_bytes(b"not an hdf5 file at all")
    out = ctrl.list_files()
    assert [f["name"] for f in out["unreadable"]] == ["stray.h5"]
    assert all(f["name"] != "stray.h5" for f in out["files"])


def test_paths_outside_the_recordings_folder_are_refused(ctrl):
    for attempt in ("../secrets.h5", "..", os.path.abspath(os.sep)):
        with pytest.raises(ValueError, match="outside"):
            library.resolve(ctrl.recordings_dir, attempt)
    bad = ctrl.file_waveform(paths="../../etc/passwd.h5")
    assert not bad["ok"] and "outside" in bad["error"]


def test_waveforms_and_spectra_come_back_for_plotting(ctrl):
    state = run_once(ctrl, files=2, name="plot")
    folder = os.path.basename(state["directory"])
    rel = f"{folder}/plot_0.h5"

    wave = ctrl.file_waveform(paths=rel, channels="A", max_points=300)
    assert wave["ok"] and len(wave["series"]) == 1
    s = wave["series"][0]
    assert len(s["max"]) == 300 and len(s["min"]) == 300
    assert s["channel"] == "A" and s["stats"]["rms"] > 0

    spec = ctrl.file_spectrum(paths=rel, channels="A", kind="asd", max_points=200)
    assert spec["ok"] and spec["units"] == "V/√Hz"
    assert len(spec["series"][0]["f"]) <= 200
    assert spec["series"][0]["averaged"] == 1


def test_averaging_a_run_is_a_folder_operation(ctrl):
    state = run_once(ctrl, files=3, name="avg")
    folder = os.path.basename(state["directory"])
    out = ctrl.file_spectrum(dir=folder, channels="A", average=True)
    assert out["ok"] and out["averaged"] is True
    assert len(out["series"]) == 1
    assert out["series"][0]["averaged"] == 3
    assert "mean of 3" in out["series"][0]["label"]


def test_the_spectrum_cache_returns_the_same_numbers(ctrl):
    state = run_once(ctrl, files=1, name="cache")
    rel = f"{os.path.basename(state['directory'])}/cache_0.h5"
    first = ctrl.file_spectrum(paths=rel, channels="A")
    second = ctrl.file_spectrum(paths=rel, channels="A")
    assert first["series"][0]["max"] == second["series"][0]["max"]


def test_a_psd_is_the_square_of_an_asd(ctrl):
    state = run_once(ctrl, files=1, name="kind")
    rel = f"{os.path.basename(state['directory'])}/kind_0.h5"
    asd = ctrl.file_spectrum(paths=rel, channels="A", kind="asd", max_points=100)
    psd = ctrl.file_spectrum(paths=rel, channels="A", kind="psd", max_points=100)
    a = np.array(asd["series"][0]["max"])
    p = np.array(psd["series"][0]["max"])
    assert np.allclose(a ** 2, p, rtol=1e-6)
    assert psd["units"] == "V²/Hz"


def test_a_band_pass_needs_both_corners(ctrl):
    state = run_once(ctrl, files=1, name="band")
    rel = f"{os.path.basename(state['directory'])}/band_0.h5"
    half = ctrl.file_waveform(paths=rel, f_low=10.0)
    assert not half["ok"] and "both" in half["error"]
    whole = ctrl.file_waveform(paths=rel, f_low=10.0, f_high=1000.0, max_points=100)
    assert whole["ok"]
