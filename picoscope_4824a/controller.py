"""The controller — the one command surface, shared by the CLI, the web app and the UI.

Every capability the instrument has is a method here, and every method:

* takes plain JSON-able arguments (strings, numbers, bools) rather than enums
  or driver handles, so a CLI flag or an HTTP form field maps onto it directly;
* returns a plain JSON-able ``dict``; and
* **never raises**. Failures come back as ``{"ok": false, "error": ...}`` with a
  machine-readable ``error_type``.

That last point is what makes the instrument safe to drive unattended. A human
at a GUI can read a traceback and decide what to do; an agent driving the CLI
needs a *result*, every time, in a shape it can branch on. The house style in
the DG822 repo is "never raise: print and return a sentinel"; this is the same
instinct, but the sentinel carries the reason.

:mod:`picoscope_4824a.registry` names each of these methods and describes its
parameters, so the CLI and the HTTP routes are generated rather than written
twice.

The controller owns the whole stack for one scope::

    ScopeController
      ├── PicoScope4824A   the device
      ├── StreamSession    the acquisition thread
      └── StreamRecorder   the disk writer

and it is what a mounted web app holds, so a single process owns the exclusive
USB handle while the CLI, the browser and the DAQ panel all talk to it.
"""

from __future__ import annotations

import functools
import json
import os
import threading
from datetime import datetime
from typing import Any, Callable, Optional

import numpy as np

from . import ps4000a as ps
from .ps4000a import (Channel, Coupling, PicoError, RatioMode, Range,
                      ThresholdDirection, WaveType)
from .recorder import (MIN_FREE_BYTES, StreamRecorder, default_recordings_dir,
                       free_bytes, project_disk_usage)
from .scope import PicoScope4824A, enumerate_units
from .streaming import StreamConfig, StreamSession


def _presets_dir() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "presets")


def command(fn: Callable) -> Callable:
    """Wrap a controller method so it always returns a result dict.

    Success passes the method's dict through with ``ok: true`` added; any
    exception becomes ``ok: false`` plus the error type and message. A
    :class:`~picoscope_4824a.ps4000a.PicoError` also carries its numeric status
    and the driver function that produced it, so a caller can branch on the
    exact condition rather than parsing prose.
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs) -> dict:
        try:
            out = fn(self, *args, **kwargs)
            if not isinstance(out, dict):
                out = {"result": out}
            out.setdefault("ok", True)
            return out
        except PicoError as exc:
            return {"ok": False, "error": str(exc), "error_type": "PicoError",
                    "status": exc.status, "status_name": exc.name,
                    "function": exc.function, "command": fn.__name__}
        except (ValueError, TypeError, KeyError) as exc:
            return {"ok": False, "error": str(exc),
                    "error_type": type(exc).__name__, "command": fn.__name__}
        except Exception as exc:
            return {"ok": False, "error": str(exc),
                    "error_type": type(exc).__name__, "command": fn.__name__}
    return wrapper


class ScopeController:
    """Everything you can do to one PicoScope 4824A."""

    def __init__(self, recordings_dir: Optional[str] = None,
                 presets_dir: Optional[str] = None,
                 simulate: bool = False) -> None:
        # The simulator duck-types the device, so nothing below this line
        # knows or cares which one it is holding.
        if simulate:
            from .sim import SimScope
            self.scope = SimScope()
        else:
            self.scope = PicoScope4824A()
        self.simulated = bool(simulate)
        self.session = StreamSession(self.scope)
        self.recordings_dir = recordings_dir or default_recordings_dir()
        self.presets_dir = presets_dir or _presets_dir()
        self.recorder = StreamRecorder(self.scope, self.session,
                                       directory=self.recordings_dir)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # device
    # ------------------------------------------------------------------

    @command
    def list_devices(self) -> dict:
        if self.simulated:
            from .sim import enumerate_units as sim_units
            return {"units": sim_units(), "count": 1, "simulated": True,
                    "driver_path": None}
        units = enumerate_units()
        return {"units": units, "count": len(units),
                "driver_path": ps.library_path()}

    @command
    def open(self, serial: Optional[str] = None) -> dict:
        with self._lock:
            if self.scope.is_open:
                return {"already_open": True, "info": self.scope.info()}
            self.scope.open(serial or None)
            info = self.scope.info()
            out = {"opened": True, "info": info}
            if self.scope.on_usb2_port:
                out["warning"] = (
                    "This 4824A is a USB 3.0 device on a USB 2.0 port. Sustained "
                    f"streaming is capped near "
                    f"{ps.USB2_AGGREGATE_SAMPLES_PER_SEC / 1e6:.0f} MS/s summed "
                    "over enabled channels (~42 MB/s). Moving it to a USB 3.0 "
                    "port raises that substantially.")
            return out

    @command
    def close(self) -> dict:
        with self._lock:
            if self.recorder.recording:
                self.session.set_sink(None)
                self.recorder.stop()
            if self.session.running:
                self.session.stop()
            self.scope.close()
            return {"closed": True}

    @command
    def info(self) -> dict:
        self._require_open()
        return {"info": self.scope.info()}

    @command
    def ping(self) -> dict:
        return {"alive": self.scope.ping(), "open": self.scope.is_open}

    @command
    def flash_led(self, count: int = 3) -> dict:
        self._require_open()
        self.scope.flash_led(int(count))
        return {"flashed": int(count)}

    @command
    def status(self) -> dict:
        """The whole instrument in one document — what an agent should poll."""
        out: dict[str, Any] = {
            "open": self.scope.is_open,
            "simulated": self.simulated,
            "driver_path": None if self.simulated else ps.library_path(),
            "recordings_dir": self.recordings_dir,
            "free_gb": round(free_bytes(self.recordings_dir) / 1e9, 2),
        }
        if self.scope.is_open:
            out["device"] = self.scope.state()
            try:
                info = self.scope.info()
                out["identity"] = {
                    "variant": info.get("variant_info"),
                    "serial": info.get("batch_and_serial"),
                    "driver": info.get("driver_version"),
                    "usb": info.get("usb_version"),
                    "on_usb2_port": info.get("on_usb2_port"),
                }
            except PicoError:
                pass
        out["stream"] = self.session.state(include_traces=False)
        out["recorder"] = self.recorder.state()
        return out

    # ------------------------------------------------------------------
    # channels
    # ------------------------------------------------------------------

    @command
    def get_channels(self) -> dict:
        self._require_open()
        return {"channels": [c.to_dict() for c in self.scope.channels.values()],
                "enabled": [c.label for c in self.scope.enabled_channels],
                "max_sample_rate_hz": self.scope.max_sample_rate()}

    @command
    def set_channel(self, channel: str, enabled: bool = True,
                    range: str = "R_5V", coupling: str = "DC",
                    offset: float = 0.0) -> dict:
        self._require_open()
        ch = _channel(channel)
        self.scope.set_channel(ch, enabled=_bool(enabled),
                               coupling=Coupling[str(coupling).upper()],
                               voltage_range=_range(range),
                               analogue_offset=float(offset))
        return {"channel": self.scope.channels[ch].to_dict(),
                "enabled_channels": [c.label for c in self.scope.enabled_channels],
                "max_sample_rate_hz": self.scope.max_sample_rate()}

    @command
    def range_for_volts(self, volts: float) -> dict:
        r = Range.from_volts(abs(float(volts)))
        return {"range": r.name, "range_volts": r.volts, "label": r.label}

    # ------------------------------------------------------------------
    # trigger
    # ------------------------------------------------------------------

    @command
    def set_trigger(self, enabled: bool = True, source: str = "A",
                    threshold: float = 0.0, direction: str = "RISING",
                    delay: int = 0, auto_trigger_ms: int = 1000) -> dict:
        self._require_open()
        self.scope.set_simple_trigger(
            enabled=_bool(enabled), source=_channel(source),
            threshold_volts=float(threshold),
            direction=ThresholdDirection[str(direction).upper()],
            delay_samples=int(delay), auto_trigger_ms=int(auto_trigger_ms))
        return {"trigger": self.scope.trigger.to_dict()}

    # ------------------------------------------------------------------
    # block capture
    # ------------------------------------------------------------------

    @command
    def describe_timebase(self, rate: float = 1e6) -> dict:
        self._require_open()
        n = len(self.scope.enabled_channels)
        tb, interval = self.scope.find_timebase(float(rate))
        _, max_samples = self.scope.get_timebase(tb, 1000)
        return {
            "requested_rate_hz": float(rate),
            "timebase": tb,
            "interval_ns": interval,
            "actual_rate_hz": 1e9 / interval if interval else 0.0,
            "max_samples": max_samples,
            "enabled_channels": n,
            "max_rate_hz": self.scope.max_sample_rate(),
            "note": "80 MS/s with 1-4 channels enabled, 40 MS/s with 5-8.",
        }

    @command
    def capture_block(self, samples: int = 10000, rate: float = 1e6,
                      pre_trigger: int = 0, timeout: float = 10.0,
                      save: Optional[str] = None,
                      max_points: int = 2000) -> dict:
        self._require_open()
        if self.session.running:
            raise RuntimeError(
                "cannot block-capture while streaming — stop the stream first")
        cap = self.scope.capture_block(
            n_samples=int(samples), sample_rate_hz=float(rate),
            pre_trigger_samples=int(pre_trigger), timeout_s=float(timeout))
        out = cap.to_dict(include_data=True, max_points=int(max_points))
        if save:
            out["saved"] = self._save_capture(cap, save)
        return {"capture": out}

    def _save_capture(self, cap, path: str) -> str:
        """Write a block capture to ``.h5`` or ``.csv``, every sample kept."""
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if path.lower().endswith(".csv"):
            labels = [c.label for c in cap.channels]
            data = np.column_stack([cap.time_s] + [cap.volts[l] for l in labels])
            np.savetxt(path, data, delimiter=",",
                       header="time_s," + ",".join(f"{l}_volts" for l in labels),
                       comments="")
        else:
            import h5py
            with h5py.File(path, "w") as f:
                f.attrs["instrument"] = "PicoScope 4824A"
                f.attrs["interval_ns"] = cap.interval_ns
                f.attrs["n_samples"] = cap.n_samples
                f.attrs["captured_iso"] = datetime.now().isoformat(timespec="seconds")
                f.attrs["max_adc"] = ps.MAX_ADC
                for ch in cap.channels:
                    ds = f.create_dataset(ch.label, data=cap.counts[ch.label])
                    ds.attrs["range_volts"] = self.scope.channels[ch].voltage_range.volts
                    ds.attrs["units"] = "adc_counts"
                    ds.attrs["volts_formula"] = "counts * range_volts / max_adc"
        return path

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------

    @command
    def plan_stream(self, rate: float = 1e6, downsample: int = 1,
                    mode: str = "NONE", duration: float = 0.0) -> dict:
        """Cost a run before committing to it — bus load and disk footprint."""
        n = len(self.scope.enabled_channels) if self.scope.is_open else 1
        cfg = _stream_config(rate, downsample, mode)
        projection = project_disk_usage(cfg, n, float(duration) or None)
        aggregate = cfg.raw_rate_hz * n
        available = free_bytes(self.recordings_dir)

        warnings = []
        if aggregate > ps.USB2_AGGREGATE_SAMPLES_PER_SEC and self.scope.on_usb2_port:
            warnings.append(
                f"{aggregate / 1e6:.1f} MS/s aggregate exceeds the "
                f"~{ps.USB2_AGGREGATE_SAMPLES_PER_SEC / 1e6:.0f} MS/s this USB 2.0 "
                "port sustains; samples will be dropped. Lower the rate, enable "
                "fewer channels, or move the scope to a USB 3.0 port.")
        need = projection.get("total_bytes")
        if need and need + MIN_FREE_BYTES > available:
            warnings.append(
                f"needs {need / 1e9:.1f} GB but only {available / 1e9:.1f} GB is free")
        if not duration and projection["gb_per_day"] > available / 1e9:
            warnings.append(
                f"at {projection['gb_per_day']:.1f} GB/day this fills the "
                f"remaining {available / 1e9:.1f} GB in "
                f"{available / 1e9 / projection['gb_per_day'] * 24:.1f} hours")

        return {"projection": projection, "aggregate_samples_per_sec": aggregate,
                "free_gb": round(available / 1e9, 2), "channels": n,
                "warnings": warnings, "config": cfg.to_dict(n)}

    @command
    def start_stream(self, rate: float = 1e6, downsample: int = 1,
                     mode: str = "NONE", buffer_samples: int = 200000,
                     window: float = 10.0, bin_rate: float = 200.0) -> dict:
        self._require_open()
        with self._lock:
            if self.session.running:
                raise RuntimeError("already streaming")
            cfg = _stream_config(rate, downsample, mode)
            cfg.buffer_samples = int(buffer_samples)
            cfg.display_window_s = float(window)
            cfg.display_bin_rate_hz = float(bin_rate)
            started = self.session.start(cfg)

            n = len(self.scope.enabled_channels)
            aggregate = cfg.raw_rate_hz * n
            if aggregate > ps.USB2_AGGREGATE_SAMPLES_PER_SEC and self.scope.on_usb2_port:
                started["warning"] = (
                    f"{aggregate / 1e6:.1f} MS/s aggregate is above what this "
                    "USB 2.0 port sustains; expect dropped samples. Watch "
                    "capture_fraction in stream-status.")
            return {"stream": started}

    @command
    def stop_stream(self) -> dict:
        with self._lock:
            was_recording = self.recorder.recording
            rec = None
            if was_recording:
                self.session.set_sink(None)
                rec = self.recorder.stop()
            clean = self.session.stop()
            out = {"stopped": True, "clean": clean,
                   "stats": self.session.stats.to_dict()}
            if rec is not None:
                out["recording"] = rec
            return out

    @command
    def stream_status(self) -> dict:
        return {"stream": self.session.state(include_traces=False)}

    @command
    def stream_traces(self, max_points: int = 1000) -> dict:
        return {"traces": self.session.traces(int(max_points)),
                "channels": [c.label for c in self.session.channels],
                "running": self.session.running,
                "window_s": self.session.config.display_window_s}

    @command
    def measure(self) -> dict:
        return {"measurements": self.session.measurements(),
                "running": self.session.running}

    # ------------------------------------------------------------------
    # recording
    # ------------------------------------------------------------------

    @command
    def start_recording(self, name: str = "stream", duration: float = 0.0,
                        format: str = "hdf5", compression: Optional[str] = None,
                        note: str = "") -> dict:
        with self._lock:
            if not self.session.running:
                raise RuntimeError(
                    "not streaming — start a stream before recording")
            if self.recorder.recording:
                raise RuntimeError("already recording")
            self.recorder.fmt = str(format)
            comp = (compression or "").strip().lower()
            self.recorder.compression = comp if comp and comp != "none" else None
            started = self.recorder.start(name=name,
                                          max_seconds=float(duration or 0.0),
                                          note=note or "")
            self.session.set_sink(self.recorder.submit)
            return {"recording": started}

    @command
    def stop_recording(self) -> dict:
        with self._lock:
            if not self.recorder.recording:
                return {"recording": self.recorder.stats.to_dict(),
                        "was_recording": False}
            self.session.set_sink(None)
            return {"recording": self.recorder.stop(), "was_recording": True}

    @command
    def recording_status(self) -> dict:
        return {"recorder": self.recorder.state()}

    @command
    def list_recordings(self) -> dict:
        directory = self.recordings_dir
        if not os.path.isdir(directory):
            return {"recordings": [], "directory": directory}
        out = []
        for fname in sorted(os.listdir(directory), reverse=True):
            if not fname.lower().endswith((".h5", ".bin")):
                continue
            full = os.path.join(directory, fname)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            out.append({
                "name": fname, "path": full,
                "mb": round(stat.st_size / 1e6, 2),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(
                    timespec="seconds"),
            })
        return {"recordings": out, "directory": directory,
                "free_gb": round(free_bytes(directory) / 1e9, 2)}

    # ------------------------------------------------------------------
    # signal generator
    # ------------------------------------------------------------------

    @command
    def set_siggen(self, wave: str = "SINE", frequency: float = 1000.0,
                   amplitude: float = 2.0, offset: float = 0.0,
                   stop_frequency: Optional[float] = None,
                   increment: float = 0.0, dwell: float = 1.0) -> dict:
        self._require_open()
        self.scope.set_signal_generator(
            wave_type=WaveType[str(wave).upper()],
            frequency_hz=float(frequency), amplitude_vpp=float(amplitude),
            offset_v=float(offset),
            stop_frequency_hz=float(stop_frequency) if stop_frequency else None,
            increment_hz=float(increment), dwell_time_s=float(dwell))
        return {"siggen": {"wave": str(wave).upper(),
                           "frequency_hz": float(frequency),
                           "amplitude_vpp": float(amplitude),
                           "offset_v": float(offset), "on": True}}

    @command
    def siggen_off(self) -> dict:
        self._require_open()
        self.scope.signal_generator_off()
        return {"siggen": {"on": False}}

    @command
    def siggen_info(self) -> dict:
        self._require_open()
        return {"siggen": {
            "max_amplitude_vpp": ps.SIGGEN_MAX_PK_TO_PK_UV / 1e6,
            "max_frequency_hz": ps.SIGGEN_MAX_FREQUENCY_HZ,
            "waves": [w.name for w in WaveType],
            "arbitrary": self.scope.arbitrary_waveform_limits(),
        }}

    # ------------------------------------------------------------------
    # presets
    # ------------------------------------------------------------------

    @command
    def save_preset(self, name: str) -> dict:
        self._require_open()
        os.makedirs(self.presets_dir, exist_ok=True)
        path = os.path.join(self.presets_dir, f"{_safe_name(name)}.json")
        doc = {
            "name": name,
            "saved_iso": datetime.now().isoformat(timespec="seconds"),
            "channels": [c.to_dict() for c in self.scope.channels.values()],
            "trigger": self.scope.trigger.to_dict(),
            "stream": self.session.config.to_dict(
                len(self.scope.enabled_channels) or 1),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        return {"preset": name, "path": path}

    @command
    def load_preset(self, name: str) -> dict:
        self._require_open()
        path = os.path.join(self.presets_dir, f"{_safe_name(name)}.json")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"no preset named {name!r}")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        for entry in doc.get("channels", []):
            self.scope.set_channel(
                _channel(entry["channel"]), enabled=bool(entry["enabled"]),
                coupling=Coupling[entry["coupling"]],
                voltage_range=Range[entry["range"]],
                analogue_offset=float(entry.get("analogue_offset", 0.0)))
        trig = doc.get("trigger") or {}
        if trig:
            self.scope.set_simple_trigger(
                enabled=bool(trig.get("enabled", False)),
                source=_channel(trig.get("source", "A")),
                threshold_volts=float(trig.get("threshold_volts", 0.0)),
                direction=ThresholdDirection[trig.get("direction", "RISING")],
                delay_samples=int(trig.get("delay_samples", 0)),
                auto_trigger_ms=int(trig.get("auto_trigger_ms", 1000)))
        return {"preset": name, "loaded": True,
                "channels": [c.to_dict() for c in self.scope.channels.values()]}

    @command
    def list_presets(self) -> dict:
        if not os.path.isdir(self.presets_dir):
            return {"presets": [], "directory": self.presets_dir}
        names = [os.path.splitext(f)[0]
                 for f in sorted(os.listdir(self.presets_dir))
                 if f.endswith(".json")]
        return {"presets": names, "directory": self.presets_dir}

    # ------------------------------------------------------------------
    # shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Bring everything down cleanly. Registered as the app's shutdown hook.

        This is not optional tidiness. A process that exits while the driver is
        mid-transfer can leave a thread spinning inside ps4000a.dll, surviving
        even ``taskkill /F`` and holding the device until it is physically
        re-plugged. Stop the stream, close the unit.
        """
        try:
            if self.recorder.recording:
                self.session.set_sink(None)
                self.recorder.stop()
        except Exception:
            pass
        try:
            if self.session.running:
                self.session.stop()
        except Exception:
            pass
        try:
            self.scope.close()
        except Exception:
            pass

    def _require_open(self) -> None:
        if not self.scope.is_open:
            raise RuntimeError("scope is not open — run 'open' first")


# --------------------------------------------------------------------------
# argument coercion
# --------------------------------------------------------------------------

def _channel(value) -> Channel:
    if isinstance(value, Channel):
        return value
    name = str(value).strip().upper()
    if name not in Channel.__members__:
        raise ValueError(f"unknown channel {value!r}; expected one of "
                         f"{', '.join(Channel.__members__)}")
    return Channel[name]


def _range(value) -> Range:
    if isinstance(value, Range):
        return value
    name = str(value).strip().upper()
    if not name.startswith("R_"):
        name = "R_" + name
    if name not in Range.__members__:
        raise ValueError(f"unknown range {value!r}; expected one of "
                         f"{', '.join(Range.__members__)}")
    return Range[name]


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _stream_config(rate: float, downsample: int, mode: str) -> StreamConfig:
    rate = float(rate)
    if rate <= 0:
        raise ValueError("rate must be positive")
    interval_ns = int(round(1e9 / rate))
    if interval_ns < 1:
        raise ValueError(
            f"{rate:g} Hz needs a sub-nanosecond interval; the streaming "
            "interval is expressed in whole nanoseconds (max 1 GS/s notional, "
            "and this unit tops out far below that)")
    return StreamConfig(sample_interval_ns=interval_ns,
                        downsample_ratio=max(1, int(downsample)),
                        downsample_mode=RatioMode[str(mode).upper()])


def _safe_name(name: str) -> str:
    keep = "-_. "
    cleaned = "".join(c for c in str(name) if c.isalnum() or c in keep).strip()
    if not cleaned:
        raise ValueError("preset name must contain at least one usable character")
    return cleaned
