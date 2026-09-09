"""Threaded streaming acquisition — the monitoring engine.

Streaming mode is the 4824A's continuous, gap-free capture path, and it is what
this repo is built around: the scope's main job here is to *watch* signals for
hours or days, the way the camera dock watches the rig.

The shape mirrors :class:`camera_dock.engine.AcquisitionEngine`, deliberately —
one producer thread pulls data at the hardware's rate and fans it out to
consumers that each run at their own pace:

* a **rolling display** — a min/max/mean envelope at a few hundred bins per
  second, covering the last N seconds, which the web UI samples whenever it
  feels like rendering. Slow rendering can never throttle capture.
* an optional **sink** — the recorder, which receives *every* sample.

Concurrency contracts, same spirit as the camera engine:

* The driver's callback runs on **our** acquisition thread (it fires inside
  ``ps4000aGetStreamingLatestValues``, which we call), so there is no
  cross-thread hand-off in the hot path and no lock needed to read the driver's
  circular buffers.
* Data must be copied out of those buffers *during* the callback — the driver
  reuses them as soon as it returns.
* The sink is invoked under ``_sink_lock``, and ``set_sink`` takes the same
  lock, so ``set_sink(None)`` returning is a barrier: no ``submit`` is in
  flight afterwards and none will start.
* ``stop()`` stops the *device* before joining the thread, so a poll blocked
  inside the driver returns promptly.

**Why the rolling display is an envelope, not a decimation.** At 2 MS/s a
1000-pixel-wide plot covers 2000 samples per pixel. Plotting every 2000th
sample hides everything between; plotting min *and* max per bin shows the true
signal extent, so a 500 ns glitch is still visible on a 10-second view. This is
the same argument that makes ``RatioMode.AGGREGATE`` the right driver-side
downsampling mode, applied one level up.
"""

from __future__ import annotations

import ctypes
import platform
import threading
from dataclasses import dataclass, field
from time import perf_counter, time as unix_time
from typing import Callable, Optional

import numpy as np

from . import ps4000a as ps
from .ps4000a import Channel, RatioMode
from .scope import PicoScope4824A

#: A sink receives one chunk: ``(chunk, chunk_index, t_host)``. It runs on the
#: acquisition thread and must be cheap (append/enqueue) — it must never block.
Sink = Callable[["StreamChunk", int, float], None]


# --------------------------------------------------------------------------
# Configuration and data records
# --------------------------------------------------------------------------

@dataclass
class StreamConfig:
    """What to stream, and how hard.

    *sample_interval_ns* is the **raw** interval at the ADC. *downsample_ratio*
    and *downsample_mode* are applied inside the driver, before the data
    crosses USB, so the effective output rate is
    ``1e9 / sample_interval_ns / downsample_ratio`` per channel.
    """
    sample_interval_ns: int = 1000              # 1 MS/s raw
    downsample_ratio: int = 1
    downsample_mode: RatioMode = RatioMode.NONE
    buffer_samples: int = 200_000               # driver circular buffer, per channel
    auto_stop: bool = False
    #: Bins per second kept for the live display (not for recording).
    display_bin_rate_hz: float = 200.0
    #: Seconds of history the rolling display holds.
    display_window_s: float = 10.0
    #: Raise the acquisition thread's priority. Long unattended runs on a busy
    #: desktop otherwise drop samples when something else grabs the CPU.
    high_priority: bool = True

    @property
    def raw_rate_hz(self) -> float:
        """Raw per-channel sample rate before driver downsampling."""
        return 1e9 / self.sample_interval_ns if self.sample_interval_ns else 0.0

    @property
    def output_rate_hz(self) -> float:
        """Per-channel sample rate after driver downsampling."""
        return self.raw_rate_hz / max(1, self.downsample_ratio)

    def bytes_per_second(self, n_channels: int) -> float:
        """Bytes/s reaching the host — what the disk and the bus actually see."""
        per_sample = 4 if self.downsample_mode is RatioMode.AGGREGATE else 2
        return self.output_rate_hz * n_channels * per_sample

    def to_dict(self, n_channels: int = 1) -> dict:
        return {
            "sample_interval_ns": self.sample_interval_ns,
            "raw_rate_hz": self.raw_rate_hz,
            "downsample_ratio": self.downsample_ratio,
            "downsample_mode": self.downsample_mode.name,
            "output_rate_hz": self.output_rate_hz,
            "buffer_samples": self.buffer_samples,
            "auto_stop": self.auto_stop,
            "bytes_per_second": self.bytes_per_second(n_channels),
            "display_bin_rate_hz": self.display_bin_rate_hz,
            "display_window_s": self.display_window_s,
            "high_priority": self.high_priority,
        }


@dataclass
class StreamChunk:
    """One batch of samples handed over by the driver.

    ``data`` maps channel label to an int16 array of ADC counts. In
    ``AGGREGATE`` mode ``data`` holds the per-bin maxima and ``data_min`` the
    minima; otherwise ``data_min`` is empty.
    """
    data: dict[str, np.ndarray]
    data_min: dict[str, np.ndarray] = field(default_factory=dict)
    n_samples: int = 0
    overflow: int = 0
    t_host: float = 0.0
    first_sample_index: int = 0

    @property
    def aggregated(self) -> bool:
        return bool(self.data_min)


@dataclass
class StreamStats:
    """Health of a run. ``dropped_estimate`` is the honest bit."""
    running: bool = False
    started_at: float = 0.0
    elapsed_s: float = 0.0
    chunks: int = 0
    samples_per_channel: int = 0
    overflow_events: int = 0
    poll_errors: int = 0
    last_error: Optional[str] = None
    measured_rate_hz: float = 0.0
    expected_rate_hz: float = 0.0
    #: Rate/drop maths run from the **first delivered chunk**, not from
    #: ``start()``. The driver takes a moment to hand over its first batch, and
    #: charging that startup latency against the capture makes every healthy run
    #: look like it dropped a percent or two of its samples.
    steady_elapsed_s: float = 0.0
    steady_samples: int = 0

    @property
    def dropped_estimate(self) -> int:
        """Samples the host never saw, inferred from elapsed time vs. received.

        The ps4000a driver reports an ``overflow`` flag for *voltage* overrange,
        not for lost samples, so a shortfall against the expected rate is the
        only signal that the bus or the host could not keep up. Reported rather
        than hidden.
        """
        if not self.steady_elapsed_s or not self.expected_rate_hz:
            return 0
        expected = int(self.expected_rate_hz * self.steady_elapsed_s)
        return max(0, expected - self.steady_samples)

    @property
    def capture_fraction(self) -> Optional[float]:
        """Fraction of expected samples actually received, once running."""
        expected = self.expected_rate_hz * self.steady_elapsed_s
        return self.steady_samples / expected if expected else None

    def to_dict(self) -> dict:
        frac = self.capture_fraction
        return {
            "running": self.running,
            "started_at": self.started_at,
            "elapsed_s": round(self.elapsed_s, 3),
            "chunks": self.chunks,
            "samples_per_channel": self.samples_per_channel,
            "measured_rate_hz": round(self.measured_rate_hz, 1),
            "expected_rate_hz": round(self.expected_rate_hz, 1),
            "capture_fraction": round(frac, 4) if frac is not None else None,
            "dropped_estimate": self.dropped_estimate,
            "overflow_events": self.overflow_events,
            "poll_errors": self.poll_errors,
            "last_error": self.last_error,
        }


# --------------------------------------------------------------------------
# Rolling display
# --------------------------------------------------------------------------

class RollingTrace:
    """Fixed-size min/max/mean envelope over a moving time window.

    One instance per channel. Data arrives in arbitrary-sized chunks and is
    reduced into bins of ``1 / bin_rate_hz`` seconds; the bins live in a ring
    buffer so memory is bounded no matter how long the run lasts.
    """

    def __init__(self, window_s: float, bin_rate_hz: float) -> None:
        self.bin_rate_hz = float(bin_rate_hz)
        self.n_bins = max(16, int(window_s * bin_rate_hz))
        self._min = np.full(self.n_bins, np.nan, dtype=np.float32)
        self._max = np.full(self.n_bins, np.nan, dtype=np.float32)
        self._mean = np.full(self.n_bins, np.nan, dtype=np.float32)
        self._write = 0
        self._filled = 0

    def add_bins(self, mins: np.ndarray, maxs: np.ndarray, means: np.ndarray) -> None:
        """Append already-reduced bins, wrapping the ring."""
        n = len(mins)
        if n == 0:
            return
        if n >= self.n_bins:                       # a chunk bigger than the window
            mins, maxs, means = mins[-self.n_bins:], maxs[-self.n_bins:], means[-self.n_bins:]
            n = self.n_bins
        end = self._write + n
        if end <= self.n_bins:
            self._min[self._write:end] = mins
            self._max[self._write:end] = maxs
            self._mean[self._write:end] = means
        else:
            split = self.n_bins - self._write
            self._min[self._write:] = mins[:split]
            self._max[self._write:] = maxs[:split]
            self._mean[self._write:] = means[:split]
            self._min[:end - self.n_bins] = mins[split:]
            self._max[:end - self.n_bins] = maxs[split:]
            self._mean[:end - self.n_bins] = means[split:]
        self._write = end % self.n_bins
        self._filled = min(self.n_bins, self._filled + n)

    def snapshot(self, max_points: int = 1000) -> dict:
        """Oldest-to-newest view of the window, decimated to *max_points* bins."""
        if not self._filled:
            return {"min": [], "max": [], "mean": [], "bin_rate_hz": self.bin_rate_hz}
        if self._filled < self.n_bins:
            sl = slice(0, self._filled)
            mn, mx, mean = self._min[sl], self._max[sl], self._mean[sl]
        else:
            idx = np.r_[self._write:self.n_bins, 0:self._write]
            mn, mx, mean = self._min[idx], self._max[idx], self._mean[idx]
        if len(mn) > max_points:
            # Re-bin rather than sample, so the envelope survives decimation.
            groups = np.array_split(np.arange(len(mn)), max_points)
            take = [g[0] for g in groups]
            ends = [g[-1] + 1 for g in groups]
            mn = np.array([np.nanmin(mn[a:b]) for a, b in zip(take, ends)])
            mx = np.array([np.nanmax(mx[a:b]) for a, b in zip(take, ends)])
            mean = np.array([np.nanmean(mean[a:b]) for a, b in zip(take, ends)])
        return {
            "min": np.nan_to_num(mn, nan=0.0).tolist(),
            "max": np.nan_to_num(mx, nan=0.0).tolist(),
            "mean": np.nan_to_num(mean, nan=0.0).tolist(),
            "bin_rate_hz": self.bin_rate_hz,
        }


# --------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------

def _boost_thread_priority() -> bool:
    """Raise the calling thread's priority (Windows only). True if it took.

    A long unattended run competes with everything else on the machine. The
    driver's circular buffer is finite, so a scheduling gap long enough to let
    it wrap loses samples permanently. ``THREAD_PRIORITY_HIGHEST`` (not
    TIME_CRITICAL — that can starve the UI) buys real headroom for a thread
    that spends most of its life blocked in the driver anyway.
    """
    if platform.system() != "Windows":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        THREAD_PRIORITY_HIGHEST = 2
        handle = kernel32.GetCurrentThread()
        return bool(kernel32.SetThreadPriority(handle, THREAD_PRIORITY_HIGHEST))
    except Exception:
        return False


class StreamSession:
    """Runs streaming acquisition on a dedicated thread.

    ::

        session = StreamSession(scope)
        session.start(StreamConfig(sample_interval_ns=1000,
                                   downsample_ratio=1000,
                                   downsample_mode=RatioMode.AGGREGATE))
        ...
        session.stop()
    """

    def __init__(self, scope: PicoScope4824A) -> None:
        self._scope = scope
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._stopping = threading.Event()
        self._lock = threading.RLock()
        self._sink_lock = threading.Lock()
        self._sink: Optional[Sink] = None

        self.config = StreamConfig()
        self.stats = StreamStats()
        self._channels: list[Channel] = []
        self._buffers: dict[str, dict] = {}
        self._traces: dict[str, RollingTrace] = {}
        self._latest: dict[str, dict] = {}
        self._chunk_index = 0
        self._actual_interval_ns = 0.0
        #: Partial bin carried between chunks so the display grid stays regular.
        self._residual: dict[str, dict[str, np.ndarray]] = {}
        self._callback = None                      # must outlive the run
        self._t_first: Optional[float] = None      # perf_counter at first chunk

    # --- properties -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def channels(self) -> list[Channel]:
        return list(self._channels)

    @property
    def actual_interval_ns(self) -> float:
        """Interval the driver actually granted (it may round the request)."""
        return self._actual_interval_ns

    # --- sink -------------------------------------------------------------

    def set_sink(self, sink: Optional[Sink]) -> None:
        """Attach or detach the per-chunk consumer (the recorder).

        Returning is a barrier: after ``set_sink(None)`` returns, no submit is
        in flight and none will start.
        """
        with self._sink_lock:
            self._sink = sink

    # --- lifecycle --------------------------------------------------------

    def start(self, config: Optional[StreamConfig] = None) -> dict:
        """Begin streaming. Returns a dict describing what actually started."""
        with self._lock:
            if self._running:
                raise RuntimeError("already streaming")
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("previous acquisition thread still alive")
            if config is not None:
                self.config = config

            self._channels = self._scope.enabled_channels
            if not self._channels:
                raise RuntimeError("no channels are enabled")

            interval, buffers = self._scope.start_streaming(
                sample_interval_ns=self.config.sample_interval_ns,
                buffer_samples=self.config.buffer_samples,
                downsample_ratio=self.config.downsample_ratio,
                downsample_mode=self.config.downsample_mode,
                auto_stop=self.config.auto_stop)
            self._actual_interval_ns = interval
            self._buffers = buffers

            out_rate = (1e9 / interval / max(1, self.config.downsample_ratio)
                        if interval else 0.0)
            self._traces = {
                ch.label: RollingTrace(self.config.display_window_s,
                                       self.config.display_bin_rate_hz)
                for ch in self._channels
            }
            self._residual = {}
            self._latest = {}
            self._chunk_index = 0
            self._t_first = None
            self.stats = StreamStats(running=True, started_at=unix_time(),
                                     expected_rate_hz=out_rate)

            self._stopping.clear()
            self._running = True
            self._thread = threading.Thread(target=self._loop, name="ps4000a-stream",
                                            daemon=True)
            self._thread.start()

            return {
                "requested_interval_ns": self.config.sample_interval_ns,
                "actual_interval_ns": interval,
                "output_rate_hz": out_rate,
                "channels": [c.label for c in self._channels],
                "downsample_ratio": self.config.downsample_ratio,
                "downsample_mode": self.config.downsample_mode.name,
                "bytes_per_second": self.config.bytes_per_second(len(self._channels)),
            }

    def stop(self, timeout_s: float = 5.0) -> bool:
        """Stop streaming. True if the acquisition thread exited cleanly.

        The device is stopped *before* the join so a poll blocked in the driver
        returns promptly.
        """
        with self._lock:
            if not self._running:
                return True
            self._stopping.set()
            try:
                self._scope.stop()
            except Exception:
                pass
            thread, self._thread = self._thread, None
            self._running = False
        clean = True
        if thread is not None:
            thread.join(timeout=timeout_s)
            clean = not thread.is_alive()
        self.stats.running = False
        return clean

    # --- the hot path -----------------------------------------------------

    def _loop(self) -> None:
        """Producer thread: pump the driver, reduce, fan out."""
        if self.config.high_priority:
            _boost_thread_priority()

        aggregated = self.config.downsample_mode is RatioMode.AGGREGATE
        labels = [c.label for c in self._channels]
        t_start = perf_counter()

        def _on_data(handle, n_samples, start_index, overflow, trigger_at,
                     triggered, auto_stop, param):
            """Driver callback. Runs on this thread, inside poll_streaming.

            Everything here must copy: the driver reuses these buffers the
            moment we return.
            """
            if n_samples <= 0:
                return
            end = start_index + n_samples
            data, data_min = {}, {}
            for label in labels:
                buf = self._buffers[label]
                data[label] = np.array(buf["max"][start_index:end], dtype=np.int16)
                if aggregated and buf["min"] is not None:
                    data_min[label] = np.array(buf["min"][start_index:end],
                                               dtype=np.int16)
            chunk = StreamChunk(data=data, data_min=data_min, n_samples=n_samples,
                                overflow=overflow, t_host=unix_time(),
                                first_sample_index=self.stats.samples_per_channel)

            self.stats.chunks += 1
            self.stats.samples_per_channel += n_samples
            if self._t_first is None:
                # Start the rate clock here: samples in this first batch were
                # captured during the driver's start-up latency, so counting
                # them against wall time since start() understates the rate.
                self._t_first = perf_counter()
            else:
                self.stats.steady_samples += n_samples
            if overflow:
                self.stats.overflow_events += 1

            self._reduce_for_display(chunk)

            with self._sink_lock:
                sink = self._sink
                if sink is not None:
                    try:
                        sink(chunk, self._chunk_index, chunk.t_host)
                    except Exception as exc:      # a bad sink must not kill capture
                        self.stats.last_error = f"sink: {type(exc).__name__}: {exc}"
            self._chunk_index += 1

        self._callback = ps.StreamingReady(_on_data)

        try:
            while not self._stopping.is_set():
                status = self._scope.poll_streaming(self._callback)
                if status not in (ps.PICO_OK, 0x00000027):      # 0x27 = PICO_BUSY
                    self.stats.poll_errors += 1
                    self.stats.last_error = ps.status_name(status)
                    if self.stats.poll_errors > 100:
                        break
                now = perf_counter()
                self.stats.elapsed_s = now - t_start
                if self._t_first is not None:
                    self.stats.steady_elapsed_s = now - self._t_first
                    if self.stats.steady_elapsed_s > 0:
                        self.stats.measured_rate_hz = (
                            self.stats.steady_samples / self.stats.steady_elapsed_s)
                # The driver hands over data in overview-buffer-sized batches;
                # polling faster than they arrive just burns CPU.
                self._stopping.wait(0.001)
        except Exception as exc:
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._running = False
            self.stats.running = False

    def _reduce_for_display(self, chunk: StreamChunk) -> None:
        """Fold a chunk into the rolling envelope. Cheap numpy only.

        Chunks do not arrive on bin boundaries, so whatever does not fill a
        whole bin is carried to the next chunk. In ``AGGREGATE`` mode the max
        and min streams are two independent series and each carries its **own**
        residual — splicing one into the other would corrupt the envelope.
        """
        rate = self.stats.expected_rate_hz or 1.0
        per_bin = max(1, int(round(rate / self.config.display_bin_rate_hz)))

        for label, counts in chunk.data.items():
            trace = self._traces.get(label)
            if trace is None:
                continue
            rng = self._scope.channels[Channel[label]].voltage_range
            carried = self._residual.get(label) or {}

            def _splice(new: np.ndarray, key: str) -> np.ndarray:
                old = carried.get(key)
                return np.concatenate([old, new]) if old is not None and old.size else new

            hi = _splice(counts, "max")
            n_bins = hi.size // per_bin
            residual: dict[str, np.ndarray] = {"max": hi[n_bins * per_bin:]}

            if n_bins:
                block = hi[:n_bins * per_bin].reshape(n_bins, per_bin)
                maxs = ps.adc_to_volts(block.max(axis=1).astype(np.float32), rng)
                means = ps.adc_to_volts(block.mean(axis=1, dtype=np.float32), rng)
                mins = ps.adc_to_volts(block.min(axis=1).astype(np.float32), rng)

            if chunk.aggregated and label in chunk.data_min:
                # The driver already reduced each bin to a min and a max; the
                # true floor comes from the min buffer, not from the max one.
                lo = _splice(chunk.data_min[label], "min")
                residual["min"] = lo[n_bins * per_bin:]
                if n_bins:
                    mins = ps.adc_to_volts(
                        lo[:n_bins * per_bin].reshape(n_bins, per_bin)
                        .min(axis=1).astype(np.float32), rng)

            if n_bins:
                trace.add_bins(mins, maxs, means)
            self._residual[label] = residual

            if counts.size:
                volts = ps.adc_to_volts(counts.astype(np.float32), rng)
                self._latest[label] = {
                    "min": float(volts.min()), "max": float(volts.max()),
                    "mean": float(volts.mean()),
                    "rms": float(np.sqrt(np.mean(np.square(volts, dtype=np.float64)))),
                    "last": float(volts[-1]),
                }

    # --- readers ----------------------------------------------------------

    def traces(self, max_points: int = 1000) -> dict:
        """Rolling envelope per channel, ready to plot."""
        return {label: trace.snapshot(max_points)
                for label, trace in self._traces.items()}

    def measurements(self) -> dict:
        """Per-channel min/max/mean/rms from the most recent chunk."""
        return dict(self._latest)

    def state(self, max_points: int = 1000, include_traces: bool = True) -> dict:
        """Everything the UI polls for."""
        out = {
            "running": self._running,
            "channels": [c.label for c in self._channels],
            "config": self.config.to_dict(len(self._channels) or 1),
            "actual_interval_ns": self._actual_interval_ns,
            "stats": self.stats.to_dict(),
            "measurements": self.measurements(),
        }
        if include_traces:
            out["traces"] = self.traces(max_points)
        return out
