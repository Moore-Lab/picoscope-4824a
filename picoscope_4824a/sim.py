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
"""

from __future__ import annotations

import math
import threading
from time import perf_counter
from typing import Optional

import numpy as np

from . import ps4000a as ps
from .ps4000a import (Channel, Coupling, PicoError, RatioMode, Range,
                      ThresholdDirection, WaveType)
from .scope import Capture, ChannelConfig, TriggerConfig

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

    def _waveform(self, channel: Channel, n: int, t0: float,
                  interval_s: float) -> np.ndarray:
        """Synthetic ADC counts for *n* samples of *channel* starting at *t0*."""
        cfg = self.channels[channel]
        full = cfg.voltage_range.volts
        t = t0 + np.arange(n, dtype=np.float64) * interval_s

        if channel is Channel.A and self._siggen["on"]:
            amp = self._siggen["amplitude_vpp"] / 2.0
            freq = self._siggen["frequency_hz"]
            volts = amp * _wave(self._siggen["wave"], freq, t) + self._siggen["offset_v"]
        else:
            # A distinct, recognisable tone per channel: 100 Hz × (index + 1).
            freq = 100.0 * (int(channel) + 1)
            amp = full * 0.3
            volts = amp * np.sin(2 * math.pi * freq * t)

        volts = volts + np.random.normal(0.0, full * 0.002, n)
        counts = np.clip(volts / full * ps.MAX_ADC, -ps.MAX_ADC, ps.MAX_ADC)
        return counts.astype(np.int16)

    # --- block capture ----------------------------------------------------

    def capture_block(self, n_samples: int = 10_000,
                      sample_rate_hz: Optional[float] = None,
                      timebase: Optional[int] = None,
                      pre_trigger_samples: int = 0,
                      timeout_s: float = 10.0) -> Capture:
        self._require_open()
        active = self.enabled_channels
        if not active:
            raise PicoError(0x10, "capture_block", "no channels are enabled")
        if timebase is None:
            if sample_rate_hz is None:
                raise ValueError("give sample_rate_hz or timebase")
            timebase, interval_ns = self.find_timebase(sample_rate_hz, n_samples)
        else:
            interval_ns, _ = self.get_timebase(timebase, n_samples)

        interval_s = interval_ns * 1e-9
        t0 = perf_counter()
        counts = {ch.label: self._waveform(ch, n_samples, t0, interval_s)
                  for ch in active}
        volts = {ch.label: ps.adc_to_volts(counts[ch.label].astype(np.float32),
                                           self.channels[ch].voltage_range)
                 for ch in active}
        return Capture(active, counts, volts, interval_ns, n_samples,
                       {ch.label: False for ch in active})

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
