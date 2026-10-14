"""A simulated 4824A — the whole stack, no hardware, no driver.

Mirrors the ``sim`` camera token the DAQ panel already understands: it lets the
web app, the CLI, the recorder and the parity tests run on a laptop with no
scope attached, and it gives the panel something to mount when the real device
is absent instead of a dead tab.

It is also the honest way to test the *software* after a crash. A ps4000a
process killed mid-stream can leave a thread spinning inside the driver that
holds the device until it is physically re-plugged; when that happens the
simulator keeps the rest of the system developable.

:class:`SimScope` duck-types :class:`~picoscope_4824a.scope.PicoScope4824A` —
same methods, same return shapes, same units — so
:class:`~picoscope_4824a.streaming.StreamSession`,
:class:`~picoscope_4824a.recorder.StreamRecorder` and
:class:`~picoscope_4824a.controller.ScopeController` drive it unchanged. It
deliberately does **not** import :mod:`ctypes` or load the driver, so it works
on a machine where the PicoSDK was never installed.

What it fakes, and how honestly:

* **Signals.** Each channel gets a distinct waveform (a sine whose frequency
  rises with the channel index) plus Gaussian noise, scaled into its configured
  range. If the signal generator is on, channel A carries *its* waveform
  instead — so a simulated loopback behaves like a real one.
* **Timing.** Streaming delivers samples paced by the wall clock at the
  configured rate, in overview-buffer-sized batches, exactly as the driver does.
  Ask for more than the USB link could carry and it drops samples and reports
  a capture fraction below 1, like the real thing.
* **Limits.** The same 4 Vpp / 1 MHz signal-generator ceilings and the same
  80/40 MS/s channel-count rule, so code that respects the simulator respects
  the instrument.
* **Triggering.** Block captures honour the simple trigger the way the rig's
  4824A was measured to: the pre-trigger buffer fills first, *then* the capture
  arms and the auto timer starts; the trigger event lands exactly at the
  pre-trigger index; with no auto timer it waits until cancelled. The trigger
  is ideal — it fires on the noise-free signal, as if the hardware hysteresis
  were wider than the noise — so a triggered display holds perfectly still.
  Captures are paced to the wall clock, so a 1 s sweep takes 1 s.
"""

from __future__ import annotations

import math
import threading
import time
from time import perf_counter
from typing import Optional

import numpy as np

from . import ps4000a as ps
from .ps4000a import (Channel, Coupling, PicoError, RatioMode, Range,
                      ThresholdDirection, WaveType)
from .scope import Capture, CaptureCancelled, ChannelConfig, TriggerConfig

SIM_SERIAL = "SIM0/0001"


class SimScope:
    """A PicoScope 4824A that exists only in software."""

    def __init__(self, serial: str = SIM_SERIAL) -> None:
        self._serial = serial
        self._open = False
        self.channels: dict[Channel, ChannelConfig] = {
            ch: ChannelConfig(ch, enabled=(ch is Channel.A)) for ch in Channel
        }
        self.trigger = TriggerConfig()
        self._lock = threading.RLock()

        # streaming state
        self._streaming = False
        self._interval_ns = 1000.0
        self._ratio = 1
        self._mode = RatioMode.NONE
        self._buffers: dict[str, dict] = {}
        self._buffer_samples = 200_000
        self._t_stream = 0.0
        self._delivered = 0
        self._phase = 0.0

        # signal generator state
        self._siggen = {"on": False, "wave": WaveType.SINE, "frequency_hz": 1000.0,
                        "amplitude_vpp": 0.0, "offset_v": 0.0}

    # --- lifecycle --------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def handle(self) -> int:
        return 1 if self._open else 0

    @property
    def power_status(self) -> int:
        return ps.PICO_OK

    @property
    def on_usb2_port(self) -> bool:
        return False

    def open(self, serial: Optional[str] = None) -> None:
        if serial and serial not in (self._serial, "sim"):
            raise PicoError(0x03, "ps4000aOpenUnit",
                            f"simulator is {self._serial}, not {serial!r}")
        self._open = True

    def close(self) -> None:
        self._streaming = False
        self._open = False

    def __enter__(self) -> "SimScope":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _require_open(self) -> None:
        if not self._open:
            raise PicoError(0x0C, "", "scope is not open")

    # --- identity ---------------------------------------------------------

    def get_unit_info(self, info: ps.PicoInfo) -> str:
        self._require_open()
        return {
            ps.PicoInfo.DRIVER_VERSION: "0.0.0.0 (simulated)",
            ps.PicoInfo.USB_VERSION: "3.0",
            ps.PicoInfo.HARDWARE_VERSION: "1",
            ps.PicoInfo.VARIANT_INFO: "4824A",
            ps.PicoInfo.BATCH_AND_SERIAL: self._serial,
            ps.PicoInfo.CAL_DATE: "01Jan70",
        }.get(info, "")

    def info(self) -> dict:
        self._require_open()
        out = {item.name.lower(): self.get_unit_info(item) for item in ps.PicoInfo}
        out.update({"max_adc": ps.MAX_ADC, "power_status": "PICO_OK",
                    "on_usb2_port": False, "driver_path": None,
                    "simulated": True})
        return out

    def ping(self) -> bool:
        return self._open

    def flash_led(self, count: int = 3) -> None:
        self._require_open()

    # --- channels ---------------------------------------------------------

    def set_channel(self, channel: Channel, enabled: bool = True,
                    coupling: Coupling = Coupling.DC,
                    voltage_range: Range = Range.R_5V,
                    analogue_offset: float = 0.0) -> None:
        self._require_open()
        self.channels[channel] = ChannelConfig(channel, enabled, coupling,
                                               voltage_range, analogue_offset)

    def apply_channels(self) -> None:
        self._require_open()

    @property
    def enabled_channels(self) -> list[Channel]:
        return [c for c in Channel if self.channels[c].enabled]

    def analogue_offset_limits(self, voltage_range: Range,
                               coupling: Coupling = Coupling.DC) -> tuple[float, float]:
        limit = min(voltage_range.volts / 2, 2.5)
        return limit, -limit

    # --- timebase ---------------------------------------------------------

    def max_sample_rate(self) -> float:
        n = len(self.enabled_channels)
        return float(ps.MAX_RATE_1_TO_4_CHANNELS if n <= 4
                     else ps.MAX_RATE_5_TO_8_CHANNELS)

    def get_timebase(self, timebase: int, n_samples: int = 1000,
                     segment: int = 0) -> tuple[float, int]:
        self._require_open()
        interval = ps.timebase_to_interval_ns(timebase)
        if 1e9 / interval > self.max_sample_rate():
            raise PicoError(0x0E, "ps4000aGetTimebase2",
                            f"timebase={timebase} needs more than "
                            f"{self.max_sample_rate() / 1e6:g} MS/s with "
                            f"{len(self.enabled_channels)} channels enabled")
        per_channel = ps.TOTAL_CAPTURE_SAMPLES // max(1, len(self.enabled_channels))
        return interval, per_channel

    def find_timebase(self, sample_rate_hz: float,
                      n_samples: int = 1000) -> tuple[int, float]:
        self._require_open()
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        ideal = 1e9 / sample_rate_hz
        start = ps.interval_ns_to_timebase(ideal)
        for tb in range(max(0, start), max(0, start) + 8):
            try:
                interval, _ = self.get_timebase(tb, n_samples)
            except PicoError:
                continue
            if interval >= ideal - 1e-9:
                return tb, interval
        raise PicoError(0x0E, "ps4000aGetTimebase2",
                        f"no timebase for {sample_rate_hz:g} Hz")

    # --- trigger ----------------------------------------------------------

    def set_simple_trigger(self, enabled: bool = True, source: Channel = Channel.A,
                           threshold_volts: float = 0.0,
                           direction: ThresholdDirection = ThresholdDirection.RISING,
                           delay_samples: int = 0,
                           auto_trigger_ms: int = 1000) -> None:
        self._require_open()
        self.trigger = TriggerConfig(enabled, source, threshold_volts, direction,
                                     delay_samples, auto_trigger_ms)

    # --- signal synthesis -------------------------------------------------

    def _clean_volts(self, channel: Channel, t: np.ndarray) -> np.ndarray:
        """The noise-free signal on *channel* at times *t*, in volts."""
        if channel is Channel.A and self._siggen["on"]:
            amp = self._siggen["amplitude_vpp"] / 2.0
            freq = self._siggen["frequency_hz"]
            return amp * _wave(self._siggen["wave"], freq, t) + self._siggen["offset_v"]
        # A distinct, recognisable tone per channel: 100 Hz × (index + 1).
        full = self.channels[channel].voltage_range.volts
        return full * 0.3 * np.sin(2 * math.pi * 100.0 * (int(channel) + 1) * t)

    def _source_period(self, channel: Channel) -> Optional[float]:
        """Period of the clean signal: ``inf`` if constant, ``None`` if noise."""
        if channel is Channel.A and self._siggen["on"]:
            wave = self._siggen["wave"]
            if wave is WaveType.WHITE_NOISE:
                return None
            freq = self._siggen["frequency_hz"]
            if wave is WaveType.DC_VOLTAGE or freq <= 0:
                return math.inf
            return 1.0 / freq
        return 1.0 / (100.0 * (int(channel) + 1))

    def _waveform(self, channel: Channel, n: int, t0: float,
                  interval_s: float) -> np.ndarray:
        """Synthetic ADC counts for *n* samples of *channel* starting at *t0*."""
        full = self.channels[channel].voltage_range.volts
        t = t0 + np.arange(n, dtype=np.float64) * interval_s
        volts = self._clean_volts(channel, t)
        volts = volts + np.random.normal(0.0, full * 0.002, n)
        counts = np.clip(volts / full * ps.MAX_ADC, -ps.MAX_ADC, ps.MAX_ADC)
        return counts.astype(np.int16)

    # --- trigger search -----------------------------------------------------

    def _next_trigger(self, trig: TriggerConfig, t_from: float,
                      limit_s: Optional[float]) -> Optional[float]:
        """Time of the first trigger event at or after *t_from*, or ``None``.

        *limit_s* bounds the search (the auto timeout); ``None`` means "as far
        as it takes" — which for a periodic signal is one period, because a
        crossing that exists at all recurs within every period. Searched on the
        clean signal: coarse over the window, then bisected to well below a
        sample, so the event sits exactly at the pre-trigger index.
        """
        ch, level, direction = trig.source, trig.threshold_volts, trig.direction
        period = self._source_period(ch)

        if period is None:                          # generator white noise
            amp = self._siggen["amplitude_vpp"] / 2.0
            off = self._siggen["offset_v"]
            if not (off - amp < level < off + amp):
                return None
            # Noise crosses any level inside its span almost at once. (The
            # capture draws fresh noise, so it will not show this exact edge.)
            return t_from + float(np.random.uniform(0.0, 1e-4))

        if math.isinf(period):                      # a constant level
            v = float(self._clean_volts(ch, np.array([t_from]))[0])
            if direction is ThresholdDirection.ABOVE:
                return t_from if v > level else None
            if direction is ThresholdDirection.BELOW:
                return t_from if v < level else None
            return None                             # a constant has no edges

        horizon = period * 1.02
        if limit_s is not None:
            horizon = min(horizon, limit_s)
        n = int(min(200_000, max(64, 4096 * horizon / period)))
        t = t_from + np.linspace(0.0, horizon, n)
        v = self._clean_volts(ch, t)

        def rising(a, b):
            return (a < level) & (b >= level)

        def falling(a, b):
            return (a > level) & (b <= level)

        if direction is ThresholdDirection.ABOVE:
            hits = np.nonzero(v > level)[0]
            if not hits.size:
                return None
            i = int(hits[0])
            if i == 0:
                return t_from
            return self._bisect(ch, t[i - 1], t[i], lambda x: x > level)
        if direction is ThresholdDirection.BELOW:
            hits = np.nonzero(v < level)[0]
            if not hits.size:
                return None
            i = int(hits[0])
            if i == 0:
                return t_from
            return self._bisect(ch, t[i - 1], t[i], lambda x: x < level)

        a, b = v[:-1], v[1:]
        if direction is ThresholdDirection.FALLING:
            mask = falling(a, b)
        elif direction is ThresholdDirection.RISING_OR_FALLING:
            mask = rising(a, b) | falling(a, b)
        else:                                       # RISING (and its NONE alias)
            mask = rising(a, b)
        hits = np.nonzero(mask)[0]
        if not hits.size:
            return None
        i = int(hits[0])
        went_up = v[i] < level
        return self._bisect(ch, t[i], t[i + 1],
                            (lambda x: x >= level) if went_up
                            else (lambda x: x <= level))

    def _bisect(self, ch: Channel, lo: float, hi: float, fired) -> float:
        """Earliest time in (lo, hi] where *fired* holds, to ~1e-15 of the span."""
        for _ in range(48):
            mid = 0.5 * (lo + hi)
            if fired(float(self._clean_volts(ch, np.array([mid]))[0])):
                hi = mid
            else:
                lo = mid
        return hi

    @staticmethod
    def _sleep_until(t_target: Optional[float], t_start: float,
                     timeout_s: Optional[float],
                     cancel: Optional[threading.Event]) -> None:
        """Wait for the wall clock to reach *t_target* (``None``: forever).

        Raises :class:`CaptureCancelled` if *cancel* is set, or the driver's
        timeout error if *timeout_s* runs out first — the same two exits a
        real block capture has.
        """
        deadline = None if timeout_s is None else t_start + float(timeout_s)
        while True:
            if cancel is not None and cancel.is_set():
                raise CaptureCancelled("block capture cancelled")
            now = perf_counter()
            if t_target is not None and now >= t_target:
                return
            if deadline is not None and now >= deadline:
                raise PicoError(0x3008, "capture_block",
                                f"no capture within {timeout_s:g} s "
                                "(waiting on a trigger that never came?)")
            step = 0.005
            if t_target is not None:
                step = min(step, t_target - now)
            if deadline is not None:
                step = min(step, deadline - now)
            time.sleep(max(0.0005, step))

    # --- block capture ----------------------------------------------------

    def capture_block(self, n_samples: int = 10_000,
                      sample_rate_hz: Optional[float] = None,
                      timebase: Optional[int] = None,
                      pre_trigger_samples: int = 0,
                      timeout_s: Optional[float] = 10.0,
                      cancel: Optional[threading.Event] = None) -> Capture:
        self._require_open()
        active = self.enabled_channels
        if not active:
            raise PicoError(0x10, "capture_block", "no channels are enabled")
        pre = int(pre_trigger_samples)
        post = int(n_samples) - pre
        if pre < 0 or post < 1:
            raise ValueError(f"pre_trigger_samples must be 0..{int(n_samples) - 1}, "
                             f"got {pre}")
        if timebase is None:
            if sample_rate_hz is None:
                raise ValueError("give sample_rate_hz or timebase")
            timebase, interval_ns = self.find_timebase(sample_rate_hz, n_samples)
        else:
            interval_ns, _ = self.get_timebase(timebase, n_samples)

        dt = interval_ns * 1e-9
        t_run = perf_counter()
        # The unit fills its pre-trigger buffer before it arms, and only then
        # starts the auto timer (measured on the rig).
        t_armed = t_run + pre * dt
        trig = self.trigger
        triggered: Optional[bool] = None
        wait_s = 0.0
        if trig.enabled:
            auto_s = trig.auto_trigger_ms / 1000.0 if trig.auto_trigger_ms > 0 else None
            t_hit = self._next_trigger(trig, t_armed, auto_s)
            if t_hit is not None:
                t_ref, triggered = t_hit, True
            elif auto_s is not None:
                t_ref, triggered = t_armed + auto_s, False
            else:
                # Normal mode on a signal that never meets the condition: the
                # real unit waits indefinitely, so this waits for the exits.
                self._sleep_until(None, t_run, timeout_s, cancel)
                raise AssertionError("unreachable")     # _sleep_until raised
            wait_s = t_ref - t_armed
            t_ref += trig.delay_samples * dt
        else:
            t_ref = t_armed

        self._sleep_until(t_ref + post * dt, t_run, timeout_s, cancel)

        t_first = t_ref - pre * dt
        counts = {ch.label: self._waveform(ch, n_samples, t_first, dt)
                  for ch in active}
        volts = {ch.label: ps.adc_to_volts(counts[ch.label].astype(np.float32),
                                           self.channels[ch].voltage_range)
                 for ch in active}
        return Capture(active, counts, volts, interval_ns, n_samples,
                       {ch.label: False for ch in active},
                       trigger_index=(pre - trig.delay_samples
                                      if trig.enabled else None),
                       triggered=triggered, wait_s=wait_s, timebase=timebase)

    # --- streaming --------------------------------------------------------

    def start_streaming(self, sample_interval_ns: int,
                        buffer_samples: int = 200_000,
                        downsample_ratio: int = 1,
                        downsample_mode: RatioMode = RatioMode.NONE,
                        auto_stop: bool = False,
                        max_pre_trigger: int = 0,
                        max_post_trigger: int = 0) -> tuple[float, dict]:
        self._require_open()
        active = self.enabled_channels
        if not active:
            raise PicoError(0x10, "start_streaming", "no channels are enabled")

        self._interval_ns = float(sample_interval_ns)
        self._ratio = max(1, int(downsample_ratio))
        self._mode = downsample_mode
        self._buffer_samples = int(buffer_samples)
        self._buffers = {}
        for ch in active:
            entry = {"max": np.zeros(buffer_samples, dtype=np.int16),
                     "min": (np.zeros(buffer_samples, dtype=np.int16)
                             if downsample_mode is RatioMode.AGGREGATE else None)}
            self._buffers[ch.label] = entry

        self._streaming = True
        self._t_stream = perf_counter()
        self._delivered = 0
        return self._interval_ns, self._buffers

    def poll_streaming(self, callback) -> int:
        """Deliver whatever the wall clock says is now due.

        Same contract as the driver: fills the registered buffers and invokes
        *callback* with a start index and a sample count, or returns
        ``PICO_BUSY`` when nothing is ready yet.
        """
        if not self._streaming:
            return 0x27                                   # PICO_BUSY
        out_rate = 1e9 / self._interval_ns / self._ratio
        elapsed = perf_counter() - self._t_stream
        due = int(elapsed * out_rate) - self._delivered
        if due < 1:
            return 0x27

        n = min(due, self._buffer_samples)
        interval_s = self._interval_ns * 1e-9 * self._ratio
        t0 = self._t_stream + self._delivered * interval_s

        for label, entry in self._buffers.items():
            ch = Channel[label]
            samples = self._waveform(ch, n, t0, interval_s)
            entry["max"][:n] = samples
            if entry["min"] is not None:
                # An aggregated bin spans `ratio` raw samples, so its extremes
                # sit a little outside the sampled value — mimic that spread.
                spread = np.abs(samples // 20) + 8
                entry["min"][:n] = np.clip(samples.astype(np.int32) - spread,
                                           -ps.MAX_ADC, ps.MAX_ADC).astype(np.int16)
                entry["max"][:n] = np.clip(samples.astype(np.int32) + spread,
                                           -ps.MAX_ADC, ps.MAX_ADC).astype(np.int16)

        self._delivered += n
        callback(1, n, 0, 0, 0, 0, 0, None)
        return ps.PICO_OK

    def stop(self) -> None:
        self._streaming = False

    def clear_buffers(self) -> None:
        self._buffers = {}

    # --- signal generator -------------------------------------------------

    def set_signal_generator(self, wave_type: WaveType = WaveType.SINE,
                             frequency_hz: float = 1000.0,
                             amplitude_vpp: float = 2.0,
                             offset_v: float = 0.0,
                             stop_frequency_hz: Optional[float] = None,
                             increment_hz: float = 0.0,
                             dwell_time_s: float = 1.0,
                             sweep_type=ps.SweepType.UP,
                             shots: int = 0, sweeps: int = 0,
                             trigger_type=ps.SigGenTrigType.RISING,
                             trigger_source=ps.SigGenTrigSource.NONE) -> None:
        self._require_open()
        # Same ceilings as the real unit, so code proven here is proven there.
        if amplitude_vpp * 1e6 > ps.SIGGEN_MAX_PK_TO_PK_UV:
            raise ValueError(
                f"amplitude {amplitude_vpp:g} Vpp exceeds the 4824A's "
                f"{ps.SIGGEN_MAX_PK_TO_PK_UV / 1e6:g} Vpp maximum")
        if frequency_hz > ps.SIGGEN_MAX_FREQUENCY_HZ:
            raise ValueError(
                f"frequency {frequency_hz:g} Hz exceeds the 4824A's "
                f"{ps.SIGGEN_MAX_FREQUENCY_HZ / 1e6:g} MHz maximum")
        self._siggen = {"on": True, "wave": WaveType(wave_type),
                        "frequency_hz": float(frequency_hz),
                        "amplitude_vpp": float(amplitude_vpp),
                        "offset_v": float(offset_v)}

    def signal_generator_off(self) -> None:
        self._require_open()
        self._siggen["on"] = False
        self._siggen["amplitude_vpp"] = 0.0

    def arbitrary_waveform_limits(self) -> dict:
        self._require_open()
        return {"sample_min": -32768, "sample_max": 32767,
                "buffer_min": ps.AWG_BUFFER_MIN, "buffer_max": ps.AWG_BUFFER_MAX}

    def frequency_to_phase(self, frequency_hz: float, buffer_length: int,
                           index_mode=ps.IndexMode.SINGLE) -> int:
        return int(frequency_hz * buffer_length)

    def set_arbitrary_waveform(self, samples, frequency_hz: float = 1000.0,
                               amplitude_vpp: float = 2.0, offset_v: float = 0.0,
                               shots: int = 0, sweeps: int = 0) -> dict:
        self._require_open()
        arr = np.asarray(samples, dtype=np.float64)
        if arr.size < ps.AWG_BUFFER_MIN or arr.size > ps.AWG_BUFFER_MAX:
            raise ValueError(
                f"waveform has {arr.size} points; the AWG buffer holds "
                f"{ps.AWG_BUFFER_MIN}..{ps.AWG_BUFFER_MAX}")
        self._siggen = {"on": True, "wave": WaveType.SINE,
                        "frequency_hz": float(frequency_hz),
                        "amplitude_vpp": float(amplitude_vpp),
                        "offset_v": float(offset_v)}
        return {"points": int(arr.size), "delta_phase": 0,
                "frequency_hz": frequency_hz, "amplitude_vpp": amplitude_vpp}

    # --- state ------------------------------------------------------------

    def state(self) -> dict:
        return {
            "open": self._open,
            "handle": self.handle,
            "simulated": True,
            "channels": [c.to_dict() for c in self.channels.values()],
            "enabled_channels": [c.label for c in self.enabled_channels],
            "trigger": self.trigger.to_dict(),
            "max_sample_rate_hz": self.max_sample_rate() if self._open else None,
            "channel_count_note": "80 MS/s with 1-4 channels, 40 MS/s with 5-8",
            "on_usb2_port": False,
        }


def _wave(wave: WaveType, freq: float, t: np.ndarray) -> np.ndarray:
    """Unit-amplitude waveform of the given type, evaluated at times *t*."""
    phase = (freq * t) % 1.0
    if wave is WaveType.SINE:
        return np.sin(2 * math.pi * freq * t)
    if wave is WaveType.SQUARE:
        return np.where(phase < 0.5, 1.0, -1.0)
    if wave is WaveType.TRIANGLE:
        return 4 * np.abs(phase - 0.5) - 1.0
    if wave is WaveType.RAMP_UP:
        return 2 * phase - 1.0
    if wave is WaveType.RAMP_DOWN:
        return 1.0 - 2 * phase
    if wave is WaveType.SINC:
        x = (phase - 0.5) * 20
        return np.sinc(x / math.pi)
    if wave is WaveType.GAUSSIAN:
        return np.exp(-((phase - 0.5) * 6) ** 2)
    if wave is WaveType.HALF_SINE:
        return np.abs(np.sin(math.pi * freq * t))
    if wave is WaveType.DC_VOLTAGE:
        return np.ones_like(t)
    if wave is WaveType.WHITE_NOISE:
        return np.random.uniform(-1.0, 1.0, t.size)
    return np.sin(2 * math.pi * freq * t)


def enumerate_units() -> list[str]:
    """The simulator is always present."""
    return [SIM_SERIAL]
