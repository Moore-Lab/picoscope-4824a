"""DAQ mode — fixed-length acquisitions written as usphere-DAQ files.

The scope used the way the NI PXIe-6363 is used in `Moore-Lab/usphere-DAQ`:
pick a record length as a power of two, a sample rate and a number of
acquisitions, and each one lands in its own HDF5 file in that project's schema.
The files are interchangeable with the NI ones — the usphere viewer and the
analysis scripts open them without being told which instrument recorded them.

Three differences from the bench-scope view in :mod:`picoscope_4824a.sweep`,
and they are what make this a separate mode rather than a setting:

* **The record length is exact.** 2^n samples at the rate the timebase really
  gives, which is what a spectrum needs: the sample rate is written into every
  file, and a rate that was quietly rounded would move every line in the ASD.
* **No trigger is needed.** The NI DAQ free-runs, and so does this by default,
  back to back. A trigger is available — auto or normal, on any channel —
  because this scope has one and the NI card's usual configuration does not.
* **Nothing is live.** The thread is inside one long capture for most of a run,
  so there is nothing to stream. After each acquisition it reduces what it
  captured to a waveform envelope and a Welch PSD, which is what the page
  draws, and keeps a running PSD average over the run.

Dead time is honest and unavoidable: block mode captures, then transfers, then
this writes and compresses, and only then does the next acquisition arm. The
NI loop has the same gap. :meth:`DaqSession.state` reports it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from time import perf_counter
from time import time as unix_time
from typing import Any, Optional

import numpy as np

from . import spectra, usphere_h5
from .loop import AcquisitionLoop
from .ps4000a import Channel, ThresholdDirection
from .recorder import MIN_FREE_BYTES, free_bytes
from .scope import CaptureCancelled

TRIGGER_MODES = ("off", "auto", "normal")
DIRECTIONS = ("RISING", "FALLING", "RISING_OR_FALLING", "ABOVE", "BELOW")

#: Record length, as a power of two. usphere-DAQ's own spinner stops at 25; a
#: 4824A cannot hold more than that across eight channels either.
BITS_MIN, BITS_MAX = 2, 25
FILES_MAX = 1_000_000

#: Bins kept for the post-acquisition waveform preview. The file holds every
#: sample; this is only what the page draws.
PREVIEW_BINS = 4000


def _as_channel(value) -> Channel:
    if isinstance(value, Channel):
        return value
    name = str(value).strip().upper()
    if name not in ("A", "B", "C", "D", "E", "F", "G", "H"):
        raise ValueError(f"unknown channel {value!r}; expected A-H")
    return Channel[name]


def _as_direction(value) -> ThresholdDirection:
    if isinstance(value, ThresholdDirection):
        return value
    name = str(value).strip().upper()
    if name == "EITHER":
        name = "RISING_OR_FALLING"
    if name not in DIRECTIONS:
        raise ValueError(f"unknown direction {value!r}; expected one of "
                         f"{', '.join(DIRECTIONS)}")
    return ThresholdDirection[name]


def _safe_name(name: str) -> str:
    cleaned = "".join(c for c in str(name) if c.isalnum() or c in "-_.").strip()
    if not cleaned:
        raise ValueError("the file name needs at least one letter or digit")
    return cleaned


@dataclass
class DaqConfig:
    """One run's settings — the fields usphere-DAQ's acquisition tab has, plus a trigger."""
    n_bits: int = 20                      # samples per file = 2 ** n_bits
    sample_rate: float = 10_000.0         # Hz, requested
    n_files: int = 1                      # 0 = until stopped
    basename: str = "run"
    directory: Optional[str] = None       # None = a new folder per run
    trigger: str = "off"                  # off | auto | normal
    source: Channel = Channel.A
    level: float = 0.0
    direction: ThresholdDirection = ThresholdDirection.RISING
    pretrigger: float = 0.0               # % of the record before the trigger
    auto_ms: int = 1000                   # auto mode's rescue timer
    preview: bool = True                  # reduce each file for the page

    @property
    def n_samples(self) -> int:
        return 2 ** int(self.n_bits)

    @property
    def requested_duration_s(self) -> float:
        return self.n_samples / self.sample_rate if self.sample_rate else 0.0

    def updated(self, **changes: Any) -> "DaqConfig":
        new = replace(self)
        for key, value in changes.items():
            if value is None:
                continue
            if key == "n_bits":
                value = int(value)
                if not BITS_MIN <= value <= BITS_MAX:
                    raise ValueError(f"n_bits must be {BITS_MIN}..{BITS_MAX} "
                                     f"(2^{BITS_MAX} samples is already more than "
                                     f"the scope's memory holds), got {value}")
            elif key == "sample_rate":
                value = float(value)
                if value <= 0:
                    raise ValueError("sample_rate must be above 0 Hz")
            elif key == "n_files":
                value = int(value)
                if not 0 <= value <= FILES_MAX:
                    raise ValueError(f"n_files must be 0..{FILES_MAX} "
                                     f"(0 means until stopped), got {value}")
            elif key == "basename":
                value = _safe_name(value)
            elif key == "directory":
                value = str(value) or None
            elif key == "trigger":
                value = str(value).strip().lower()
                if value not in TRIGGER_MODES:
                    raise ValueError(f"trigger must be one of "
                                     f"{', '.join(TRIGGER_MODES)}, got {value!r}")
            elif key == "source":
                value = _as_channel(value)
            elif key == "direction":
                value = _as_direction(value)
            elif key == "level":
                value = float(value)
            elif key == "pretrigger":
                value = float(value)
                if not 0.0 <= value <= 100.0:
                    raise ValueError(f"pretrigger must be 0..100 %, got {value:g}")
            elif key == "auto_ms":
                value = int(value)
                if not 0 <= value <= 32767:
                    raise ValueError("auto_ms must be 0..32767 (the driver's limit)")
            elif key == "preview":
                value = bool(value)
            else:
                raise TypeError(f"unknown DAQ setting {key!r}")
            setattr(new, key, value)
        return new

    def to_dict(self) -> dict:
        return {
            "n_bits": self.n_bits,
            "n_samples": self.n_samples,
            "sample_rate": self.sample_rate,
            "requested_duration_s": self.requested_duration_s,
            "n_files": self.n_files,
            "basename": self.basename,
            "directory": self.directory,
            "trigger": self.trigger,
            "source": self.source.label,
            "level": self.level,
            "direction": self.direction.name,
            "pretrigger": self.pretrigger,
            "auto_ms": self.auto_ms,
            "preview": self.preview,
        }


@dataclass(frozen=True)
class DaqPlan:
    """What the settings resolve to on this scope, with this channel set."""
    timebase: int
    interval_ns: float
    n_samples: int
    pre_samples: int
    channels: tuple
    ranges: dict
    voltage_min: float
    voltage_max: float
    rails_note: Optional[str]

    @property
    def actual_rate_hz(self) -> float:
        return 1e9 / self.interval_ns if self.interval_ns else 0.0

    @property
    def duration_s(self) -> float:
        return self.n_samples * self.interval_ns * 1e-9

    def bytes_per_file(self) -> int:
        """Rough size on disk. The 32-row layout is mostly zeros, which gzip
        removes almost entirely, so what is left is the recorded rows."""
        return int(len(self.channels) * self.n_samples * 2 * 0.95) + 8192

    def to_dict(self) -> dict:
        return {
            "timebase": self.timebase,
            "interval_s": self.interval_ns * 1e-9,
            "actual_rate_hz": self.actual_rate_hz,
            "n_samples": self.n_samples,
            "pre_samples": self.pre_samples,
            "duration_s": self.duration_s,
            "channels": list(self.channels),
            "ranges": dict(self.ranges),
            "voltage_min": self.voltage_min,
            "voltage_max": self.voltage_max,
            "rails_note": self.rails_note,
            "bytes_per_file": self.bytes_per_file(),
        }


@dataclass
class Acquisition:
    """One finished acquisition, reduced to what a page can draw."""
    index: int
    path: str
    name: str
    t_host: float
    fsamp: float
    n_samples: int
    triggered: Optional[bool]
    mb: float
    channels: list
    #: label -> (min, max) arrays over PREVIEW_BINS bins
    envelope: dict = field(default_factory=dict)
    #: label -> PSD (V^2/Hz) on *freqs*
    psd: dict = field(default_factory=dict)
    freqs: Optional[np.ndarray] = None
    nperseg: int = 0
    measurements: dict = field(default_factory=dict)

    def to_dict(self, max_points: int = 1200, kind: str = "asd",
                average: Optional[dict] = None, average_n: int = 0) -> dict:
        dt = 1.0 / self.fsamp if self.fsamp else 0.0
        out = {
            "id": self.index,
            "file": self.name,
            "path": self.path,
            "t_host": self.t_host,
            "fsamp": self.fsamp,
            "n_samples": self.n_samples,
            "duration_s": self.n_samples * dt,
            "triggered": self.triggered,
            "mb": self.mb,
            "channels": list(self.channels),
            "kind": kind,
            "units": spectra.units(kind),
            "nperseg": self.nperseg,
            "measurements": self.measurements,
            "time": {}, "spectrum": {}, "average": {}, "average_n": average_n,
        }
        for label, (lo, hi) in self.envelope.items():
            lo2, hi2 = spectra.envelope(lo, max_points)[0], spectra.envelope(hi, max_points)[1]
            out["time"][label] = {"min": np.round(lo2, 7).tolist(),
                                  "max": np.round(hi2, 7).tolist()}
        if self.freqs is not None:
            for label, pxx in self.psd.items():
                f, lo, hi = spectra.log_envelope(self.freqs,
                                                 spectra.to_kind(pxx, kind), max_points)
                out["spectrum"][label] = {"f": np.round(f, 4).tolist(),
                                          "min": lo.tolist(), "max": hi.tolist()}
            for label, pxx in (average or {}).items():
                f, lo, hi = spectra.log_envelope(self.freqs,
                                                 spectra.to_kind(pxx, kind), max_points)
                out["average"][label] = {"f": np.round(f, 4).tolist(),
                                         "min": lo.tolist(), "max": hi.tolist()}
        return out


class DaqSession(AcquisitionLoop):
    """Runs a DAQ acquisition series on the shared acquisition thread."""

    thread_name = "ps4000a-daq"

    def __init__(self, scope, recordings_dir: str) -> None:
        super().__init__(scope)
        self.recordings_dir = recordings_dir
        self._config = DaqConfig()
        self._plan: Optional[DaqPlan] = None
        self._directory: Optional[str] = None
        self._files: list = []
        self._index = 0
        self._latest: Optional[Acquisition] = None
        self._psd_sum: dict = {}
        self._psd_n = 0
        self._phase = "idle"              # idle | waiting | acquiring | writing | error
        self._error: Optional[str] = None
        self._started_at: Optional[float] = None
        self._capture_started: Optional[float] = None
        self._bytes = 0
        self._gap_s = 0.0

    # --- settings ---------------------------------------------------------

    @property
    def config(self) -> DaqConfig:
        with self._lock:
            return replace(self._config)

    def configure(self, **changes: Any) -> dict:
        with self._lock:
            if self.running:
                raise RuntimeError("a DAQ run is in progress — stop it before "
                                   "changing its settings")
            self._config = self._config.updated(**changes)
            return self.state()

    def latest(self) -> Optional[Acquisition]:
        with self._lock:
            return self._latest

    # --- planning ---------------------------------------------------------

    def plan(self, cfg: Optional[DaqConfig] = None) -> DaqPlan:
        """Resolve settings against the scope as it is configured now."""
        cfg = cfg or self.config
        channels = [c.label for c in self._scope.enabled_channels]
        if not channels:
            raise RuntimeError("no channels are enabled")
        if len(channels) > 8:
            raise RuntimeError("the usphere schema's first eight streams are "
                               "ai0..ai7; this scope has eight inputs")
        n = cfg.n_samples
        timebase, interval_ns = self._scope.find_timebase(cfg.sample_rate, n)
        _, max_samples = self._scope.get_timebase(timebase, n)
        if max_samples and n > max_samples:
            raise RuntimeError(
                f"2^{cfg.n_bits} = {n:,} samples is more than the scope can hold "
                f"at this rate with {len(channels)} channel(s) on "
                f"({max_samples:,} max). Use fewer bits or fewer channels.")
        ranges = {label: self._scope.channels[Channel[label]].voltage_range.volts
                  for label in channels}
        vmin, vmax, note = usphere_h5.rails_for(ranges)
        pre = min(n - 1, max(0, int(round(n * cfg.pretrigger / 100.0))))
        return DaqPlan(timebase, interval_ns, n, pre, tuple(channels), ranges,
                       vmin, vmax, note)

    def project(self, cfg: Optional[DaqConfig] = None) -> dict:
        """Cost a run before committing to it: time, size, and what will bite."""
        cfg = cfg or self.config
        out: dict = {"config": cfg.to_dict(), "warnings": []}
        try:
            plan = self.plan(cfg)
        except Exception as exc:
            out["error"] = str(exc)
            return out
        per_file = plan.bytes_per_file()
        files = cfg.n_files
        out["plan"] = plan.to_dict()
        out["bytes_per_file"] = per_file
        out["total_bytes"] = per_file * files if files else None
        out["free_bytes"] = free_bytes(self.recordings_dir)
        out["seconds_per_file"] = plan.duration_s
        out["total_seconds"] = plan.duration_s * files if files else None
        if plan.rails_note:
            out["warnings"].append(plan.rails_note)
        rate_error = abs(plan.actual_rate_hz - cfg.sample_rate)
        if rate_error > cfg.sample_rate * 1e-9:
            out["warnings"].append(
                f"the nearest timebase gives {plan.actual_rate_hz:,.4f} S/s, not "
                f"{cfg.sample_rate:,.4f}. Every file records the rate it was "
                f"taken at, so spectra stay correct — but the number is not the "
                f"one you typed.")
        if files and per_file * files + MIN_FREE_BYTES > out["free_bytes"]:
            out["warnings"].append(
                f"this run needs about {per_file * files / 1e9:.1f} GB and only "
                f"{out['free_bytes'] / 1e9:.1f} GB is free")
        if not files:
            per_hour = per_file * (3600.0 / plan.duration_s) if plan.duration_s else 0
            out["bytes_per_hour"] = per_hour
            if per_hour:
                out["warnings"].append(
                    f"running until stopped: about {per_hour / 1e9:.1f} GB/hour, "
                    f"which fills the free space in "
                    f"{out['free_bytes'] / per_hour:.1f} hours")
        return out

    # --- control ----------------------------------------------------------

    def start(self, **changes: Any) -> dict:
        with self._lock:
            if self.running:
                raise RuntimeError("a DAQ run is already in progress")
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("the previous DAQ thread has not exited yet")
            cfg = self._config.updated(**changes)
            self._config = cfg
            plan = self.plan(cfg)            # fail here, not inside the thread
            if free_bytes(self.recordings_dir) < plan.bytes_per_file() + MIN_FREE_BYTES:
                raise RuntimeError("not enough free disk space for even one file")

            self._directory = self._make_directory(cfg)
            self._plan = plan
            self._files = []
            self._index = 0
            self._latest = None
            self._psd_sum = {}
            self._psd_n = 0
            self._error = None
            self._bytes = 0
            self._gap_s = 0.0
            self._phase = "waiting" if cfg.trigger == "normal" else "acquiring"
            self._started_at = perf_counter()
            self._capture_started = None
            self._start_thread()
            return self.state()

    def _make_directory(self, cfg: DaqConfig) -> str:
        """Where this run's files go: one folder per run unless told otherwise.

        A folder per run is what makes "average the ASD over this run" a
        directory operation, which is how the usphere analysis treats a run.

        Nothing here ever overwrites a file. The stamped name is only good to
        the second, so two runs started in the same second would otherwise
        land in one folder and the second would write over the first run's
        data; a named folder that already holds a run of the same name is
        refused outright.
        """
        if cfg.directory:
            path = cfg.directory
            if not os.path.isabs(path):
                path = os.path.join(self.recordings_dir, path)
            first = os.path.join(path, f"{cfg.basename}_0.h5")
            if os.path.exists(first):
                raise RuntimeError(
                    f"{os.path.basename(path)} already holds {cfg.basename}_0.h5 — "
                    f"choose another folder or root name. Files are never "
                    f"overwritten.")
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = os.path.join(self.recordings_dir, f"{cfg.basename}_{stamp}")
            path, n = base, 2
            while os.path.isdir(path) and os.listdir(path):
                path, n = f"{base}_{n}", n + 1
        os.makedirs(path, exist_ok=True)
        return path

    # --- state ------------------------------------------------------------

    def state(self) -> dict:
        with self._lock:
            cfg = self._config
            running = self.running
            plan = self._plan
            now = perf_counter()
            capture = None
            if running and self._capture_started is not None and plan is not None:
                elapsed = now - self._capture_started
                expected = plan.duration_s
                capture = {
                    "elapsed_s": elapsed,
                    "expected_s": expected,
                    # Waiting for a trigger has no progress to report: the
                    # record has not started yet.
                    "fraction": (min(1.0, elapsed / expected)
                                 if expected and self._phase == "acquiring" else None),
                }
            return {
                "running": running,
                "phase": self._phase if running else (
                    "error" if self._phase == "error" else "idle"),
                "config": cfg.to_dict(),
                "plan": plan.to_dict() if plan else None,
                "directory": self._directory,
                "files_written": len(self._files),
                "files_target": cfg.n_files,
                "files": list(self._files[-10:]),
                "bytes_written": self._bytes,
                "elapsed_s": (now - self._started_at) if self._started_at else 0.0,
                "capture": capture,
                "gap_s": round(self._gap_s, 3),
                "average_n": self._psd_n,
                "last_id": self._latest.index if self._latest else None,
                "free_gb": round(free_bytes(self.recordings_dir) / 1e9, 2),
                "error": self._error,
                "stopped_reason": None if running else self._stopped_reason,
                "warnings": [plan.rails_note] if plan and plan.rails_note else [],
            }

    def payload(self, since: int = -1, max_points: int = 1200,
                kind: str = "asd") -> dict:
        """State, plus the last acquisition's preview if it is newer than *since*."""
        with self._lock:
            latest = self._latest
            average = dict(self._psd_sum)
            n = self._psd_n
            state = self.state()
        preview = None
        if latest is not None and latest.index > int(since):
            avg = {k: v / n for k, v in average.items()} if n else {}
            preview = latest.to_dict(max_points=max_points, kind=kind,
                                     average=avg, average_n=n)
        return {"state": state, "preview": preview}

    # --- the run ----------------------------------------------------------

    def _arm(self, cfg: DaqConfig) -> None:
        on = cfg.trigger != "off"
        rng = self._scope.channels[cfg.source].voltage_range.volts
        self._scope.set_simple_trigger(
            enabled=on, source=cfg.source,
            threshold_volts=max(-rng, min(rng, cfg.level)),
            direction=cfg.direction, delay_samples=0,
            auto_trigger_ms=cfg.auto_ms if cfg.trigger == "auto" else 0)

    def _on_exit(self) -> None:
        if self._phase != "error":
            self._phase = "idle"
        self._capture_started = None

    def _fail(self, exc: BaseException) -> None:
        with self._lock:
            self._phase = "error"
            self._error = str(exc) or type(exc).__name__

    def _work(self) -> None:
        for _ in self.iterations():
            with self._lock:
                cfg = replace(self._config)
                plan = self._plan
                index = self._index
            if cfg.n_files and index >= cfg.n_files:
                with self._lock:
                    self._stopped_reason = (
                        f"done — {index} file{'s' if index != 1 else ''} written")
                    self._stopping.set()
                return

            if free_bytes(self.recordings_dir) < plan.bytes_per_file() + MIN_FREE_BYTES:
                with self._lock:
                    self._stopped_reason = "stopped: the disk is nearly full"
                    self._error = self._stopped_reason
                    self._phase = "error"
                    self._stopping.set()
                return

            try:
                self._arm(cfg)
            except Exception as exc:
                self._fail(exc)
                self._pause(0.5)
                continue

            with self._lock:
                self._phase = "waiting" if cfg.trigger == "normal" else "acquiring"
                self._capture_started = perf_counter()
            t0 = perf_counter()
            try:
                cap = self._scope.capture_block(
                    n_samples=plan.n_samples, timebase=plan.timebase,
                    pre_trigger_samples=plan.pre_samples,
                    timeout_s=None, cancel=self._interrupt)
            except CaptureCancelled:
                with self._lock:
                    self._capture_started = None
                continue
            except Exception as exc:
                self._fail(exc)
                with self._lock:
                    self._capture_started = None
                self._pause(0.5)
                continue
            t_captured = perf_counter()

            with self._lock:
                self._phase = "writing"
            try:
                acq = self._write(cap, cfg, plan, index)
            except Exception as exc:
                self._fail(exc)
                self._pause(0.5)
                continue

            with self._lock:
                self._index += 1
                self._files.append({"index": acq.index, "name": acq.name,
                                    "mb": acq.mb, "t_host": acq.t_host,
                                    "triggered": acq.triggered})
                self._bytes += int(acq.mb * 1e6)
                self._latest = acq
                self._capture_started = None
                self._gap_s = perf_counter() - t_captured
            _ = t0

    # --- writing and reduction -------------------------------------------

    def _write(self, cap, cfg: DaqConfig, plan: DaqPlan, index: int) -> Acquisition:
        name = f"{cfg.basename}_{index}.h5"
        final = os.path.join(self._directory, name)
        # Write beside the final name and rename: a half-written file must
        # never appear in the viewer's file list, and a reader that opens one
        # mid-write gets an error rather than a truncated spectrum.
        partial = final + ".part"

        counts_by_ai = {}
        for label, counts in cap.counts.items():
            ai = usphere_h5.CHANNEL_TO_AI[label]
            factor = usphere_h5.scale_counts(plan.ranges[label],
                                             plan.voltage_min, plan.voltage_max)
            scaled = np.rint(counts.astype(np.float32) * factor)
            counts_by_ai[ai] = np.clip(scaled, -32768, 32767).astype(np.int16)

        fsamp = plan.actual_rate_hz
        module = {
            usphere_h5.PICOSCOPE_MODULE: {
                "instrument": "PicoScope 4824A",
                "interval_ns": cap.interval_ns,
                "timebase": int(plan.timebase),
                "basename": cfg.basename,
                "file_index": int(index),
                "trigger_mode": cfg.trigger,
                "trigger_source": cfg.source.label,
                "trigger_level_volts": float(cfg.level),
                "trigger_direction": cfg.direction.name,
                "pretrigger_samples": int(plan.pre_samples),
                "triggered": (bool(cap.triggered) if cap.triggered is not None
                              else False),
                "channel_labels": [str(x) for x in plan.channels],
                "channel_ranges_volts": [float(plan.ranges[c]) for c in plan.channels],
                "channel_ai": [usphere_h5.CHANNEL_TO_AI[c] for c in plan.channels],
            }
        }
        usphere_h5.write_counts(partial, counts_by_ai, n_samples=plan.n_samples,
                                fsamp=fsamp, voltage_min=plan.voltage_min,
                                voltage_max=plan.voltage_max, module_data=module)
        os.replace(partial, final)

        acq = Acquisition(
            index=index, path=final, name=name, t_host=unix_time(), fsamp=fsamp,
            n_samples=plan.n_samples, triggered=cap.triggered,
            mb=round(os.path.getsize(final) / 1e6, 3),
            channels=list(plan.channels))
        if cfg.preview:
            self._reduce(acq, cap)
        return acq

    def _reduce(self, acq: Acquisition, cap) -> None:
        """Turn a capture into what the page draws, and fold it into the average.

        Done here, on the acquisition thread between captures, because it needs
        the samples that are about to be dropped — the file keeps them, but
        reading 2^22 samples back off disk to draw them would be silly.
        """
        bits = min(spectra.DEFAULT_NPERSEG_BITS, max(spectra.NPERSEG_BITS_MIN,
                                                     int(np.log2(acq.n_samples))))
        for label, volts in cap.volts.items():
            v = np.asarray(volts, dtype=np.float64)
            lo, hi = spectra.envelope(v, PREVIEW_BINS)
            acq.envelope[label] = (lo, hi)
            acq.measurements[label] = spectra.summarise(v, acq.fsamp)
            f, pxx, nperseg = spectra.welch_psd(v, acq.fsamp, nperseg_bits=bits)
            acq.freqs = f
            acq.nperseg = nperseg
            acq.psd[label] = pxx
            with self._lock:
                if label in self._psd_sum and self._psd_sum[label].shape == pxx.shape:
                    self._psd_sum[label] = self._psd_sum[label] + pxx
                else:
                    self._psd_sum[label] = pxx.copy()
        with self._lock:
            self._psd_n += 1
