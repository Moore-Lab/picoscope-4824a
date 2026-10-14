"""Triggered sweeps — the scope-mode acquisition loop.

An oscilloscope does not stream. It arms, waits for its trigger, captures one
window of time around that instant, draws it, and arms again — so a repetitive
signal holds still on screen, because every sweep starts at the same point in
its cycle. :class:`SweepSession` runs that loop on a dedicated thread over
block-mode captures and keeps the latest sweep for the surfaces to fetch.

The trigger modes are the four a bench scope has:

* ``auto``   — wait for the trigger, but capture anyway if none arrives within
  the auto timeout, so the screen never freezes: a flat or badly levelled
  signal still shows. The default.
* ``normal`` — capture only on a trigger; otherwise keep waiting, indefinitely,
  and keep showing the last triggered sweep. For rare events, and for slow
  signals that auto mode would keep free-running past.
* ``single`` — normal, but stop after the first triggered sweep.
* ``none``   — free-run: trigger off, every sweep taken at once.

Facts this is built on, measured on the rig's 4824A through the driver rather
than taken from the manual:

* The trigger event lands exactly at the pre-trigger index of the buffer, so
  aligning sweeps is just ``t = (i - pre) × interval``.
* The auto timer starts only once the pre-trigger buffer has filled, so the
  time a capture spent *armed* is its elapsed time less its collection time.
* The simple trigger has hysteresis, so "was this sweep triggered?" is answered
  from that armed time, never by looking for a crossing in the data — see
  :func:`~picoscope_4824a.scope.classify_trigger`.
* ``autoTrigger_ms`` is an int16 on the ps4000a: 32.767 s at most.

The device does one acquisition at a time, so sweeps and streaming are
mutually exclusive; the controller enforces that. While sweeps run, any other
change to the device — a channel range, the signal generator — is handed to
this thread with ``run_on_loop`` and applied *between* captures. A normal-mode
capture can wait forever, and the device object is not thread-safe, so "between
captures" is the only safe moment there is. The thread itself, and the ordering
those guarantees need, is :class:`~picoscope_4824a.loop.AcquisitionLoop`, which
DAQ runs share.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from time import perf_counter
from time import time as unix_time
from typing import Any, Optional

import numpy as np

from .loop import AcquisitionLoop
from .ps4000a import Channel, ThresholdDirection
from .scope import Capture, CaptureCancelled
# Re-exported: the file viewer reduces traces the same way, for the same
# reason — a glitch one sample wide must survive being drawn a thousand
# pixels wide.
from .spectra import envelope

MODES = ("auto", "normal", "single", "none")
DIRECTIONS = ("RISING", "FALLING", "RISING_OR_FALLING", "ABOVE", "BELOW")

#: Horizontal divisions on the graticule. Fixed, as on a bench scope.
DIVISIONS = 10

#: Accepted time/div range. Below 10 ns/div a sweep is a handful of samples at
#: 80 MS/s; above 10 s/div a block capture is the wrong tool — stream instead.
TIME_PER_DIV_MIN = 10e-9
TIME_PER_DIV_MAX = 10.0

SAMPLES_MIN = 10
SAMPLES_MAX = 1_000_000

#: The driver's auto-trigger timeout is an int16 of milliseconds.
AUTO_MS_MAX = 32767

#: Automatic auto-timeout floor: short enough that a flat signal still refreshes
#: a few times a second, long enough for a signal that fills the screen to meet
#: its trigger first.
AUTO_MS_FLOOR = 200

#: Upper bound on sweeps per second. The display polls at ~8 Hz and has no
#: persistence, so racing the USB link faster only burns CPU.
MAX_SWEEPS_PER_S = 100.0

#: Nominal ADC resolution of the 4824A. The driver scales samples to ±32767,
#: but only 12 bits of that are real, which is what bounds "is this a signal
#: or quantisation noise?".
ADC_BITS = 12


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

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


@dataclass
class SweepConfig:
    """Trigger and timebase settings — the front panel of the scope."""
    mode: str = "auto"
    source: Channel = Channel.A
    level: float = 0.0
    direction: ThresholdDirection = ThresholdDirection.RISING
    #: Seconds per horizontal division; a sweep spans :data:`DIVISIONS`.
    time_per_div: float = 1e-3
    #: Where the trigger sits across the sweep, in percent: 0 = left edge.
    position: float = 50.0
    #: Target samples per sweep. Fewer at fast timebases, where the sample
    #: rate ceiling is what limits them.
    samples: int = 2000
    #: Auto-mode timeout. 0 = automatic (see :meth:`effective_auto_ms`).
    auto_ms: int = 0

    @property
    def sweep_s(self) -> float:
        return self.time_per_div * DIVISIONS

    def effective_auto_ms(self) -> int:
        """The auto timeout actually programmed: explicit, or max(200 ms, one sweep).

        One sweep, because a signal whose period fits on screen should get the
        chance to meet its trigger before auto mode gives up on it.
        """
        if self.auto_ms > 0:
            return min(int(self.auto_ms), AUTO_MS_MAX)
        return int(min(AUTO_MS_MAX, max(AUTO_MS_FLOOR, self.sweep_s * 1000.0)))

    def updated(self, **changes: Any) -> "SweepConfig":
        """A validated copy with *changes* applied. ``None`` values are ignored."""
        new = replace(self)
        for key, value in changes.items():
            if value is None:
                continue
            if key == "mode":
                value = str(value).strip().lower()
                if value not in MODES:
                    raise ValueError(f"unknown mode {value!r}; expected one of "
                                     f"{', '.join(MODES)}")
            elif key == "source":
                value = _as_channel(value)
            elif key == "direction":
                value = _as_direction(value)
            elif key == "level":
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError("level must be a finite number of volts")
            elif key == "time_per_div":
                value = float(value)
                if not TIME_PER_DIV_MIN <= value <= TIME_PER_DIV_MAX:
                    raise ValueError(
                        f"time_per_div must be {TIME_PER_DIV_MIN:g}.."
                        f"{TIME_PER_DIV_MAX:g} s/div, got {value:g} — for "
                        "slower views, stream instead")
            elif key == "position":
                value = float(value)
                if not 0.0 <= value <= 100.0:
                    raise ValueError(f"position must be 0..100 %, got {value:g}")
            elif key == "samples":
                value = int(value)
                if not SAMPLES_MIN <= value <= SAMPLES_MAX:
                    raise ValueError(f"samples must be {SAMPLES_MIN}..{SAMPLES_MAX}, "
                                     f"got {value}")
            elif key == "auto_ms":
                value = int(value)
                if not 0 <= value <= AUTO_MS_MAX:
                    raise ValueError(f"auto_ms must be 0..{AUTO_MS_MAX} "
                                     f"(the driver's limit), got {value}")
            else:
                raise TypeError(f"unknown sweep setting {key!r}")
            setattr(new, key, value)
        return new

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "source": self.source.label,
            "level": self.level,
            "direction": self.direction.name,
            "time_per_div": self.time_per_div,
            "divisions": DIVISIONS,
            "sweep_s": self.sweep_s,
            "position": self.position,
            "samples": self.samples,
            "auto_ms": self.auto_ms,
            "auto_ms_effective": self.effective_auto_ms(),
        }


@dataclass(frozen=True)
class SweepPlan:
    """What the settings resolve to on the current channel configuration."""
    timebase: int
    interval_ns: float
    n_samples: int
    pre_samples: int
    channels: tuple

    def to_dict(self) -> dict:
        interval_s = self.interval_ns * 1e-9
        return {
            "timebase": self.timebase,
            "interval_s": interval_s,
            "rate_hz": 1.0 / interval_s if interval_s else 0.0,
            "n_samples": self.n_samples,
            "pre_samples": self.pre_samples,
            "capture_s": self.n_samples * interval_s,
            "channels": list(self.channels),
        }


# --------------------------------------------------------------------------
# reduction
# --------------------------------------------------------------------------

def _schmitt_rising_edges(v: np.ndarray, low: float, high: float) -> np.ndarray:
    """Sample indices where *v* switches low -> high through a hysteresis band."""
    state = np.full(v.size, -1, dtype=np.int8)       # -1 = undecided
    state[v >= high] = 1
    state[v <= low] = 0
    idx = np.where(state >= 0, np.arange(v.size), 0)
    np.maximum.accumulate(idx, out=idx)             # forward-fill the last decision
    filled = state[idx]
    decided = state[idx] >= 0
    rising = (filled[1:] == 1) & (filled[:-1] == 0) & decided[:-1]
    return np.nonzero(rising)[0] + 1


def measure(volts: np.ndarray, interval_s: float, range_volts: float) -> dict:
    """What a scope's measurement row shows for one channel of one sweep.

    Frequency uses a Schmitt trigger at mid-level with 10 % hysteresis, and is
    reported only when it can be trusted: the swing has to be well clear of
    quantisation noise (ten 12-bit steps), there must be at least two full
    cycles on screen, and those cycles must be regular. Otherwise ``None`` —
    a blank beats a confident wrong number.
    """
    v = np.asarray(volts, dtype=np.float64)
    if v.size == 0:
        return {}
    vmin, vmax = float(v.min()), float(v.max())
    vpp = vmax - vmin
    out = {
        "min": vmin, "max": vmax, "vpp": vpp,
        "mean": float(v.mean()),
        "rms": float(math.sqrt(float(np.mean(v * v)))),
        "freq_hz": None, "period_s": None,
    }
    lsb = 2.0 * range_volts / (2 ** ADC_BITS)
    if vpp > 10 * lsb and v.size >= 8:
        mid = 0.5 * (vmax + vmin)
        hyst = max(0.1 * vpp, 2 * lsb)
        edges = _schmitt_rising_edges(v, mid - hyst / 2, mid + hyst / 2)
        if edges.size >= 3:
            gaps = np.diff(edges).astype(np.float64)
            if gaps.std() <= 0.25 * gaps.mean():
                period = (edges[-1] - edges[0]) / (edges.size - 1) * interval_s
                if period > 0:
                    out["period_s"] = float(period)
                    out["freq_hz"] = float(1.0 / period)
    return out


def _rounded(a: np.ndarray) -> list:
    # 0.1 µV is below the smallest range's LSB; the rounding roughly halves
    # the JSON without losing anything a screen could show.
    return np.round(np.asarray(a, dtype=np.float64), 7).tolist()


# --------------------------------------------------------------------------
# one sweep
# --------------------------------------------------------------------------

@dataclass
class Sweep:
    """One published sweep, with the settings and ranges it was taken with."""
    id: int
    capture: Capture
    config: SweepConfig
    plan: SweepPlan
    ranges: dict                      # label -> range volts at capture time
    t_host: float
    _measured: Optional[dict] = field(default=None, repr=False)

    @property
    def status(self) -> str:
        if self.capture.triggered is None:
            return "free-run"
        return "triggered" if self.capture.triggered else "auto"

    @property
    def trigger_index(self) -> int:
        idx = self.capture.trigger_index
        return self.plan.pre_samples if idx is None else idx

    def measurements(self) -> dict:
        """Computed on first request, not per sweep: at 100 sweeps/s nobody reads
        most of them, and a full-resolution pass per channel is the costly part."""
        if self._measured is None:
            dt = self.capture.interval_ns * 1e-9
            self._measured = {
                label: measure(v, dt, self.ranges.get(label, 5.0))
                for label, v in self.capture.volts.items()
            }
        return self._measured

    def meta(self) -> dict:
        """What a saved file records about how this sweep was taken."""
        cfg = self.config
        return {
            "sweep_id": self.id,
            "status": self.status,
            "mode": cfg.mode,
            "trigger_source": cfg.source.label,
            "trigger_level_volts": cfg.level,
            "trigger_direction": cfg.direction.name,
            "time_per_div_s": cfg.time_per_div,
            "position_percent": cfg.position,
            "captured_unix": self.t_host,
        }

    def to_dict(self, max_points: int = 1000) -> dict:
        cap = self.capture
        dt = cap.interval_ns * 1e-9
        trig = self.trigger_index
        bins = max(2, min(int(max_points), cap.n_samples))
        traces = {}
        for label, v in cap.volts.items():
            lo, hi = envelope(v, bins)
            traces[label] = {"min": _rounded(lo), "max": _rounded(hi)}
        return {
            "id": self.id,
            "t_host": self.t_host,
            "status": self.status,
            "triggered": cap.triggered,
            "channels": list(cap.volts.keys()),
            "n_samples": cap.n_samples,
            "interval_s": dt,
            "rate_hz": 1.0 / dt if dt else 0.0,
            # Sample times relative to the trigger event. The window a client
            # draws comes from the *current* settings, not these, so a stopped
            # sweep pans and zooms with the controls, as on a digital scope.
            "t_start_s": -trig * dt,
            "t_end_s": (cap.n_samples - 1 - trig) * dt,
            "bins": min(bins, cap.n_samples),
            "samples_per_bin": cap.n_samples / min(bins, cap.n_samples),
            "trigger": {
                "mode": self.config.mode,
                "source": self.config.source.label,
                "level": self.config.level,
                "direction": self.config.direction.name,
                "index": trig,
                "wait_s": cap.wait_s,
            },
            "time_per_div": self.config.time_per_div,
            "position": self.config.position,
            "ranges": dict(self.ranges),
            "overflow": dict(cap.overflow),
            "traces": traces,
            "measurements": self.measurements(),
        }


# --------------------------------------------------------------------------
# the session
# --------------------------------------------------------------------------

class SweepSession(AcquisitionLoop):
    """Runs triggered sweeps on a dedicated thread.

    ::

        sweeps = SweepSession(scope)
        sweeps.start(mode="normal", source="A", level=0.5, time_per_div=1e-3)
        latest = sweeps.latest()            # a Sweep, or None
        sweeps.configure(level=0.8)         # a waiting capture re-arms
        sweeps.stop()
    """

    thread_name = "ps4000a-sweep"

    def __init__(self, scope) -> None:
        super().__init__(scope)
        self._config = SweepConfig()
        self._gen = 0                       # bumps on every settings change
        self._plan: Optional[SweepPlan] = None
        self._plan_key: Optional[tuple] = None

        self._latest: Optional[Sweep] = None
        self._next_id = 1
        self._phase = "stopped"             # stopped | armed | error
        self._armed_at: Optional[float] = None
        self._error: Optional[str] = None
        self._count = 0
        self._rate_hz = 0.0
        self._last_sweep_t: Optional[float] = None

    # --- properties -------------------------------------------------------

    @property
    def config(self) -> SweepConfig:
        with self._lock:
            return replace(self._config)

    def latest(self) -> Optional[Sweep]:
        with self._lock:
            return self._latest

    # --- control ----------------------------------------------------------

    def configure(self, **changes: Any) -> dict:
        """Change settings. A running loop re-arms with them at once."""
        with self._lock:
            new = self._config.updated(**changes)
            if new != self._config:
                self._config = new
                self._gen += 1
                self._interrupt.set()
            return self.state()

    def start(self, **changes: Any) -> dict:
        """Begin sweeping, optionally changing settings first."""
        with self._lock:
            if self.running:
                raise RuntimeError("the scope is already running — stop it first, "
                                   "or change settings with sweep-set")
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("the previous sweep thread has not exited yet")
            new = self._config.updated(**changes)
            if new != self._config:
                self._config = new
                self._gen += 1
            # Refuse the obvious up front rather than starting a loop that can
            # only report errors.
            self._check_runnable(self._config)

            self._phase = "armed"
            self._armed_at = perf_counter()
            self._error = None
            self._count = 0
            self._rate_hz = 0.0
            self._last_sweep_t = None
            self._plan_key = None
            self._start_thread()
            return self.state()

    # --- state ------------------------------------------------------------

    def state(self) -> dict:
        with self._lock:
            cfg = self._config
            running = self.running
            waiting_s = (perf_counter() - self._armed_at
                         if running and self._phase == "armed" and self._armed_at
                         else 0.0)
            plan = self._plan.to_dict() if self._plan is not None else None
            latest = self._latest
            # The average only moves when a sweep lands, so on its own it
            # would keep reporting the last rate while a normal-mode scope sits
            # waiting. Once the gap is clearly longer than the average implies,
            # report the gap instead.
            rate = self._rate_hz
            if running and self._last_sweep_t is not None and rate > 0:
                since = perf_counter() - self._last_sweep_t
                if since > 2.0 / rate:
                    rate = 1.0 / since
            out = {
                "running": running,
                "phase": self._phase if running else
                         ("error" if self._phase == "error" else "stopped"),
                "indicator": self._indicator(running, waiting_s, plan),
                "waiting_s": waiting_s,
                "config": cfg.to_dict(),
                "plan": plan,
                "sweeps": self._count,
                "sweep_rate_hz": round(rate, 2),
                "last_id": latest.id if latest else None,
                "last_status": latest.status if latest else None,
                "last_age_s": (unix_time() - latest.t_host) if latest else None,
                "error": self._error,
                "stopped_reason": None if running else self._stopped_reason,
                "warnings": self._warnings(cfg, plan),
            }
        return out

    def payload(self, since: int = -1, max_points: int = 1000) -> dict:
        """State plus the latest sweep, if it is newer than *since*."""
        with self._lock:
            latest = self._latest
            state = self.state()
        sweep = None
        if latest is not None and latest.id > int(since):
            sweep = latest.to_dict(max_points)
        return {"state": state, "sweep": sweep}

    def _indicator(self, running: bool, waiting_s: float,
                   plan: Optional[dict]) -> str:
        """The one-word status a bench scope shows in its corner."""
        if not running:
            return "error" if self._phase == "error" else "stop"
        if self._phase == "error":
            return "error"
        mode = self._config.mode
        if mode == "none":
            return "free-run"
        latest = self._latest
        capture_s = plan["capture_s"] if plan else self._config.sweep_s
        if mode in ("normal", "single") and waiting_s > max(0.3, 2 * capture_s):
            return "wait"
        if latest is None or self._count == 0:
            return "armed"
        return "trig'd" if latest.status == "triggered" else latest.status

    def _warnings(self, cfg: SweepConfig, plan: Optional[dict]) -> list:
        out = []
        try:
            src = self._scope.channels[cfg.source]
        except Exception:
            return out
        if cfg.mode != "none":
            if not src.enabled:
                out.append(f"trigger source {cfg.source.label} is switched off")
            elif abs(cfg.level) >= src.voltage_range.volts:
                out.append(
                    f"level {cfg.level:g} V is at or beyond channel "
                    f"{cfg.source.label}'s ±{src.voltage_range.volts:g} V range — "
                    "the trigger can never fire")
        if plan is not None and plan["n_samples"] < 50:
            out.append(
                f"only {plan['n_samples']} samples per sweep at this timebase — "
                f"the sample rate tops out at "
                f"{self._scope.max_sample_rate() / 1e6:g} MS/s with the channels "
                "enabled now")
        return out

    # --- the loop ---------------------------------------------------------

    def _check_runnable(self, cfg: SweepConfig) -> None:
        enabled = self._scope.enabled_channels
        if not enabled:
            raise RuntimeError("no channels are enabled")
        if cfg.mode != "none" and cfg.source not in enabled:
            raise RuntimeError(
                f"trigger source {cfg.source.label} is switched off — enable it, "
                "or trigger on a channel that is on")

    def _plan_for(self, cfg: SweepConfig, gen: int) -> SweepPlan:
        # Keyed by the generation *cfg* was snapshotted at, not the current
        # one: a change landing in between must not file a plan built from the
        # old settings under the new key.
        channels = tuple(c.label for c in self._scope.enabled_channels)
        key = (gen, channels)
        if self._plan is not None and key == self._plan_key:
            return self._plan
        sweep_s = cfg.sweep_s
        rate = min(cfg.samples / sweep_s, self._scope.max_sample_rate())
        timebase, interval_ns = self._scope.find_timebase(rate, cfg.samples)
        n = max(2, int(round(sweep_s / (interval_ns * 1e-9))))
        pre = min(n - 1, max(0, int(round(n * cfg.position / 100.0))))
        plan = SweepPlan(timebase, interval_ns, n, pre, channels)
        with self._lock:
            self._plan, self._plan_key = plan, key
        return plan

    def _arm(self, cfg: SweepConfig) -> None:
        # Re-programmed every sweep: the threshold is sent in ADC counts, so a
        # range change on the source channel makes the previous one wrong.
        on = cfg.mode != "none"
        rng = self._scope.channels[cfg.source].voltage_range.volts
        self._scope.set_simple_trigger(
            enabled=on, source=cfg.source,
            threshold_volts=max(-rng, min(rng, cfg.level)),
            direction=cfg.direction, delay_samples=0,
            auto_trigger_ms=cfg.effective_auto_ms() if cfg.mode == "auto" else 0)

    def _fail(self, exc: BaseException) -> None:
        with self._lock:
            self._phase = "error"
            self._error = str(exc) or type(exc).__name__

    def _on_exit(self) -> None:
        if self._phase != "error":
            self._phase = "stopped"

    def _work(self) -> None:
        for _ in self.iterations():
            with self._lock:
                cfg = replace(self._config)
                gen = self._gen
            try:
                self._check_runnable(cfg)
                plan = self._plan_for(cfg, gen)
                self._arm(cfg)
            except Exception as exc:
                self._fail(exc)
                self._pause(0.5)                  # until the settings change
                continue

            with self._lock:
                if self._phase != "armed":
                    self._armed_at = perf_counter()
                self._phase = "armed"
                self._error = None
            t_sweep = perf_counter()
            try:
                cap = self._scope.capture_block(
                    n_samples=plan.n_samples, timebase=plan.timebase,
                    pre_trigger_samples=plan.pre_samples,
                    timeout_s=None, cancel=self._interrupt)
            except CaptureCancelled:
                continue                          # stop, new settings, or a call
            except Exception as exc:
                self._fail(exc)
                self._pause(0.5)
                continue

            ranges = {label: self._scope.channels[Channel[label]].voltage_range.volts
                      for label in cap.volts}
            with self._lock:
                sweep = Sweep(self._next_id, cap, cfg, plan, ranges, unix_time())
                self._next_id += 1
                self._latest = sweep
                self._count += 1
                now = perf_counter()
                if self._last_sweep_t is not None:
                    inst = 1.0 / max(1e-6, now - self._last_sweep_t)
                    self._rate_hz = (inst if self._count <= 2
                                     else 0.8 * self._rate_hz + 0.2 * inst)
                self._last_sweep_t = now
                self._armed_at = now

            if cfg.mode == "single" and cap.triggered:
                with self._lock:
                    self._stopped_reason = "single sweep captured"
                    self._stopping.set()
                return

            # Pace: a scope that sweeps faster than anyone can look only burns
            # CPU and USB. Cut short by any interrupt.
            spare = (1.0 / MAX_SWEEPS_PER_S) - (perf_counter() - t_sweep)
            if spare > 0:
                self._pause(spare)
