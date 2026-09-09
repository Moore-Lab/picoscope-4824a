"""The device layer: one open PicoScope 4824A.

:class:`PicoScope4824A` owns the driver handle and the instrument's configured
state (which channels are on, at what range, what timebase, what trigger). It
is a thin, *synchronous* object — every method maps onto one or two ps4000a
calls — and it raises :class:`~picoscope_4824a.ps4000a.PicoError` on failure.

Layering:

* :mod:`picoscope_4824a.ps4000a` — ctypes prototypes and enums. No state.
* **this module** — one device, synchronous, raises on error.
* :mod:`picoscope_4824a.streaming` — the acquisition thread that drives
  streaming mode without blocking its caller.
* :mod:`picoscope_4824a.controller` — the façade the CLI, the web app and the
  GUI all talk to. It catches errors and returns structured results.

Two hardware facts shape this class and are worth stating up front, because
both cost real debugging time to discover:

**Buffer lifetime.** ``ps4000aSetDataBuffer`` hands the driver a raw pointer and
the driver keeps writing through it for the whole run. If Python garbage-collects
the array, the driver writes into freed memory and the process dies with an
access violation (0xC0000005) somewhere unrelated. Every buffer this class
registers is therefore kept alive on ``self`` until it is explicitly replaced.

**Open is a two-stage handshake.** ``ps4000aOpenUnit`` returns a *valid handle*
together with a non-OK status describing how the unit is powered — our 4824A is
a USB 3.0 device on a USB 2.0 port, so it always reports
``PICO_USB3_0_DEVICE_NON_USB3_0_PORT``. That is not an error; you acknowledge it
by passing the same code back to ``ps4000aChangePowerSource``. Treating a non-zero
status from open as a failure means never opening the scope at all.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import byref, c_float, c_int16, c_int32, c_uint32, create_string_buffer
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Sequence

import numpy as np

from . import ps4000a as ps
from .ps4000a import (Channel, Coupling, PicoError, RatioMode, Range,
                      ThresholdDirection, TimeUnits, WaveType)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def enumerate_units() -> list[str]:
    """Serial numbers of every 4000A-series scope attached to this machine.

    Works while the PicoScope desktop application is running, so it is safe to
    call as a liveness probe.
    """
    lib = ps.load_library()
    count = c_int16(0)
    buf = create_string_buffer(1024)
    length = c_int16(1024)
    status = lib.ps4000aEnumerateUnits(byref(count), buf, byref(length))
    if (status & 0xFFFFFFFF) != ps.PICO_OK:
        return []
    raw = buf.value.decode("ascii", errors="replace").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


# --------------------------------------------------------------------------
# Configuration records
# --------------------------------------------------------------------------

@dataclass
class ChannelConfig:
    """How one analogue input is configured."""
    channel: Channel
    enabled: bool = False
    coupling: Coupling = Coupling.DC
    voltage_range: Range = Range.R_5V
    analogue_offset: float = 0.0

    def to_dict(self) -> dict:
        return {
            "channel": self.channel.label,
            "enabled": self.enabled,
            "coupling": self.coupling.name,
            "range": self.voltage_range.name,
            "range_volts": self.voltage_range.volts,
            "range_label": self.voltage_range.label,
            "analogue_offset": self.analogue_offset,
        }


@dataclass
class TriggerConfig:
    """Simple edge/level trigger state."""
    enabled: bool = False
    source: Channel = Channel.A
    threshold_volts: float = 0.0
    direction: ThresholdDirection = ThresholdDirection.RISING
    delay_samples: int = 0
    auto_trigger_ms: int = 1000

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "source": self.source.label,
            "threshold_volts": self.threshold_volts,
            "direction": self.direction.name,
            "delay_samples": self.delay_samples,
            "auto_trigger_ms": self.auto_trigger_ms,
        }


@dataclass
class Capture:
    """One block-mode capture: raw counts, volts, and the time axis."""
    channels: list[Channel]
    counts: dict[str, np.ndarray]
    volts: dict[str, np.ndarray]
    interval_ns: float
    n_samples: int
    overflow: dict[str, bool] = field(default_factory=dict)
    triggered_at: Optional[int] = None

    @property
    def time_s(self) -> np.ndarray:
        """Sample times in seconds, relative to the first sample."""
        return np.arange(self.n_samples, dtype=np.float64) * (self.interval_ns * 1e-9)

    def to_dict(self, include_data: bool = False, max_points: int = 2000) -> dict:
        """Summary dict; with *include_data* also carries decimated traces.

        The decimation is for transport (a JSON payload or a browser plot), not
        for analysis — :attr:`volts` always holds every sample.
        """
        out = {
            "channels": [c.label for c in self.channels],
            "n_samples": self.n_samples,
            "interval_ns": self.interval_ns,
            "sample_rate_hz": 1e9 / self.interval_ns if self.interval_ns else 0.0,
            "duration_s": self.n_samples * self.interval_ns * 1e-9,
            "overflow": self.overflow,
        }
        if include_data:
            step = max(1, self.n_samples // max_points)
            out["time_s"] = self.time_s[::step].tolist()
            out["volts"] = {k: v[::step].tolist() for k, v in self.volts.items()}
        return out


# --------------------------------------------------------------------------
# The device
# --------------------------------------------------------------------------

class PicoScope4824A:
    """One open PicoScope 4824A.

    Not thread-safe by itself: the ps4000a driver serialises per-handle, but
    this object's Python-side state is not locked. The streaming session and
    the controller above provide the locking.

    Use as a context manager, or call :meth:`open` / :meth:`close` explicitly::

        with PicoScope4824A() as scope:
            scope.set_channel(Channel.A, True, voltage_range=Range.R_5V)
            cap = scope.capture_block(n_samples=10_000, sample_rate_hz=1e6)
            print(cap.volts["A"].max())
    """

    def __init__(self) -> None:
        self._lib = ps.load_library()
        self._handle = c_int16(0)
        self._open = False
        self._power_status = ps.PICO_OK

        self.channels: dict[Channel, ChannelConfig] = {
            ch: ChannelConfig(ch, enabled=(ch is Channel.A)) for ch in Channel
        }
        self.trigger = TriggerConfig()

        self._max_adc = ps.MAX_ADC
        #: Buffers handed to the driver. MUST stay referenced for the whole run
        #: — see the module docstring.
        self._buffers: dict[tuple[Channel, str], np.ndarray] = {}
        self._lock = threading.RLock()

    # --- lifecycle --------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def handle(self) -> int:
        return self._handle.value

    @property
    def power_status(self) -> int:
        """The status ``ps4000aOpenUnit`` reported (0 on a true USB 3.0 port)."""
        return self._power_status

    @property
    def on_usb2_port(self) -> bool:
        """True if the scope is plugged into a USB 2.0 port.

        Worth surfacing: it caps sustained streaming at roughly
        :data:`~picoscope_4824a.ps4000a.USB2_AGGREGATE_SAMPLES_PER_SEC` samples
        per second summed over enabled channels.
        """
        return self._power_status == ps.PICO_USB3_0_DEVICE_NON_USB3_0_PORT

    def open(self, serial: Optional[str] = None) -> None:
        """Open the scope, completing the power-source handshake.

        *serial* selects a specific unit (e.g. ``"JY140/0294"``); ``None`` takes
        the first one found.
        """
        if self._open:
            return
        target = serial.encode("ascii") if serial else None
        status = self._lib.ps4000aOpenUnit(byref(self._handle), target) & 0xFFFFFFFF

        if status in ps.POWER_STATUSES:
            # Opened, but the driver wants the power arrangement acknowledged.
            self._power_status = status
            ack = self._lib.ps4000aChangePowerSource(
                self._handle, c_uint32(status)) & 0xFFFFFFFF
            if ack != ps.PICO_OK:
                self._lib.ps4000aCloseUnit(self._handle)
                self._handle = c_int16(0)
                raise PicoError(ack, "ps4000aChangePowerSource",
                                f"after open reported {ps.status_name(status)}")
        elif status != ps.PICO_OK:
            raise PicoError(status, "ps4000aOpenUnit",
                            "is the PicoScope desktop application holding the device?"
                            if status == 0x00000005 else "")
        else:
            self._power_status = ps.PICO_OK

        if self._handle.value <= 0:
            raise PicoError(status or 0x0C, "ps4000aOpenUnit", "no valid handle")

        self._open = True
        # Cache full-scale, then push our default channel config to the device
        # so software state and hardware state agree from the first moment.
        mx = c_int16(0)
        if self._lib.ps4000aMaximumValue(self._handle, byref(mx)) == ps.PICO_OK and mx.value:
            self._max_adc = mx.value
        self.apply_channels()

    def close(self) -> None:
        """Stop acquisition and release the device. Safe to call twice."""
        if not self._open:
            return
        try:
            self._lib.ps4000aStop(self._handle)
        except Exception:
            pass
        self._lib.ps4000aCloseUnit(self._handle)
        self._open = False
        self._handle = c_int16(0)
        self._buffers.clear()

    def __enter__(self) -> "PicoScope4824A":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _require_open(self) -> None:
        if not self._open:
            raise PicoError(0x0C, "", "scope is not open")

    # --- identity ---------------------------------------------------------

    def get_unit_info(self, info: ps.PicoInfo) -> str:
        """One ``ps4000aGetUnitInfo`` field as text."""
        self._require_open()
        buf = create_string_buffer(64)
        required = c_int16(0)
        status = self._lib.ps4000aGetUnitInfo(
            self._handle, buf, c_int16(64), byref(required), c_int32(int(info)))
        ps.check(status, "ps4000aGetUnitInfo", info.name)
        return buf.value.decode("ascii", errors="replace").strip()

    def info(self) -> dict:
        """Everything the unit will tell us about itself."""
        self._require_open()
        out: dict[str, object] = {}
        for item in ps.PicoInfo:
            try:
                out[item.name.lower()] = self.get_unit_info(item)
            except PicoError:
                out[item.name.lower()] = None
        out["max_adc"] = self._max_adc
        out["power_status"] = ps.status_name(self._power_status)
        out["on_usb2_port"] = self.on_usb2_port
        out["driver_path"] = ps.library_path()
        return out

    def ping(self) -> bool:
        """True if the unit is still responding (cheap liveness check)."""
        if not self._open:
            return False
        return (self._lib.ps4000aPingUnit(self._handle) & 0xFFFFFFFF) == ps.PICO_OK

    def flash_led(self, count: int = 3) -> None:
        """Flash the front-panel LED — used to identify a unit on a busy bench."""
        self._require_open()
        ps.check(self._lib.ps4000aFlashLed(self._handle, c_int16(count)),
                 "ps4000aFlashLed")

    # --- channels ---------------------------------------------------------

    def set_channel(self, channel: Channel, enabled: bool = True,
                    coupling: Coupling = Coupling.DC,
                    voltage_range: Range = Range.R_5V,
                    analogue_offset: float = 0.0) -> None:
        """Configure one input and push it to the device immediately."""
        self._require_open()
        cfg = ChannelConfig(channel, enabled, coupling, voltage_range, analogue_offset)
        status = self._lib.ps4000aSetChannel(
            self._handle, c_int32(int(channel)), c_int16(1 if enabled else 0),
            c_int32(int(coupling)), c_int32(int(voltage_range)),
            c_float(analogue_offset))
        ps.check(status, "ps4000aSetChannel", channel.label)
        self.channels[channel] = cfg

    def apply_channels(self) -> None:
        """Re-send every channel's configuration to the device."""
        self._require_open()
        for cfg in self.channels.values():
            status = self._lib.ps4000aSetChannel(
                self._handle, c_int32(int(cfg.channel)),
                c_int16(1 if cfg.enabled else 0), c_int32(int(cfg.coupling)),
                c_int32(int(cfg.voltage_range)), c_float(cfg.analogue_offset))
            ps.check(status, "ps4000aSetChannel", cfg.channel.label)

    @property
    def enabled_channels(self) -> list[Channel]:
        """Enabled inputs, in channel order."""
        return [c for c in Channel if self.channels[c].enabled]

    def analogue_offset_limits(self, voltage_range: Range,
                               coupling: Coupling = Coupling.DC) -> tuple[float, float]:
        """Permitted (max, min) analogue offset for a range/coupling pair."""
        self._require_open()
        mx, mn = c_float(0), c_float(0)
        status = self._lib.ps4000aGetAnalogueOffset(
            self._handle, c_int32(int(voltage_range)), c_int32(int(coupling)),
            byref(mx), byref(mn))
        ps.check(status, "ps4000aGetAnalogueOffset")
        return mx.value, mn.value

    # --- timebase ---------------------------------------------------------

    def get_timebase(self, timebase: int, n_samples: int = 1000,
                     segment: int = 0) -> tuple[float, int]:
        """Ask the driver what *timebase* means: ``(interval_ns, max_samples)``.

        Must be called **after** the channels are configured — the achievable
        top rate depends on how many are enabled (80 MS/s with 1-4 channels,
        40 MS/s with 5-8), so timebase 0 is rejected once a fifth comes on.
        """
        self._require_open()
        interval = c_float(0)
        max_samples = c_int32(0)
        status = self._lib.ps4000aGetTimebase2(
            self._handle, c_uint32(timebase), c_int32(n_samples),
            byref(interval), byref(max_samples), c_uint32(segment))
        ps.check(status, "ps4000aGetTimebase2", f"timebase={timebase}")
        return interval.value, max_samples.value

    def find_timebase(self, sample_rate_hz: float,
                      n_samples: int = 1000) -> tuple[int, float]:
        """Fastest timebase that is no faster than *sample_rate_hz*.

        Returns ``(timebase, actual_interval_ns)``. Walks upward from the ideal
        value so a rate the current channel count cannot sustain degrades to the
        nearest achievable one rather than raising.
        """
        self._require_open()
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        ideal_ns = 1e9 / sample_rate_hz
        start = ps.interval_ns_to_timebase(ideal_ns)
        last_error: Optional[PicoError] = None
        for tb in range(max(0, start), max(0, start) + 8):
            try:
                interval_ns, _ = self.get_timebase(tb, n_samples)
            except PicoError as exc:
                last_error = exc
                continue
            if interval_ns >= ideal_ns - 1e-9:
                return tb, interval_ns
        if last_error is not None:
            raise last_error
        raise PicoError(0x0E, "ps4000aGetTimebase2",
                        f"no timebase for {sample_rate_hz:g} Hz")

    def max_sample_rate(self) -> float:
        """Peak sample rate for the current channel count, in samples/second."""
        n = len(self.enabled_channels)
        return float(ps.MAX_RATE_1_TO_4_CHANNELS if n <= 4
                     else ps.MAX_RATE_5_TO_8_CHANNELS)

    # --- trigger ----------------------------------------------------------

    def set_simple_trigger(self, enabled: bool = True, source: Channel = Channel.A,
                           threshold_volts: float = 0.0,
                           direction: ThresholdDirection = ThresholdDirection.RISING,
                           delay_samples: int = 0,
                           auto_trigger_ms: int = 1000) -> None:
        """Arm (or disarm) the single-channel edge trigger.

        *auto_trigger_ms* is the rescue timer: after this long without an edge
        the scope captures anyway. Zero means wait forever — which will hang a
        block capture if the edge never comes, so the default is 1 s.
        """
        self._require_open()
        threshold = ps.volts_to_adc(threshold_volts,
                                    self.channels[source].voltage_range)
        status = self._lib.ps4000aSetSimpleTrigger(
            self._handle, c_int16(1 if enabled else 0), c_int32(int(source)),
            c_int16(threshold), c_int32(int(direction)),
            c_uint32(delay_samples), c_int16(auto_trigger_ms))
        ps.check(status, "ps4000aSetSimpleTrigger")
        self.trigger = TriggerConfig(enabled, source, threshold_volts, direction,
                                     delay_samples, auto_trigger_ms)

    # --- buffers ----------------------------------------------------------

    def _register_buffer(self, channel: Channel, n: int,
                         mode: RatioMode = RatioMode.NONE,
                         segment: int = 0) -> tuple[np.ndarray, Optional[np.ndarray]]:
        """Allocate and register driver buffer(s) for *channel*.

        ``AGGREGATE`` needs a max and a min buffer; every other mode needs one.
        Returns ``(max_buffer, min_buffer_or_None)``. Both are kept on ``self``
        so the driver's pointers stay valid for the whole run.
        """
        self._require_open()
        buf_max = np.zeros(n, dtype=np.int16)
        ptr_max = buf_max.ctypes.data_as(ctypes.POINTER(c_int16))

        if mode is RatioMode.AGGREGATE:
            buf_min = np.zeros(n, dtype=np.int16)
            ptr_min = buf_min.ctypes.data_as(ctypes.POINTER(c_int16))
            status = self._lib.ps4000aSetDataBuffers(
                self._handle, c_int32(int(channel)), ptr_max, ptr_min,
                c_int32(n), c_uint32(segment), c_int32(int(mode)))
            ps.check(status, "ps4000aSetDataBuffers", channel.label)
            self._buffers[(channel, "max")] = buf_max
            self._buffers[(channel, "min")] = buf_min
            return buf_max, buf_min

        status = self._lib.ps4000aSetDataBuffer(
            self._handle, c_int32(int(channel)), ptr_max, c_int32(n),
            c_uint32(segment), c_int32(int(mode)))
        ps.check(status, "ps4000aSetDataBuffer", channel.label)
        self._buffers[(channel, "max")] = buf_max
        self._buffers.pop((channel, "min"), None)
        return buf_max, None

    def clear_buffers(self) -> None:
        """Drop our references to driver buffers (call only when stopped)."""
        self._buffers.clear()

    # --- block mode -------------------------------------------------------

    def capture_block(self, n_samples: int = 10_000,
                      sample_rate_hz: Optional[float] = None,
                      timebase: Optional[int] = None,
                      pre_trigger_samples: int = 0,
                      timeout_s: float = 10.0) -> Capture:
        """Run one block capture and return the data.

        Give either *sample_rate_hz* (converted to the nearest achievable
        timebase) or an explicit *timebase*. Blocks until the capture completes
        or *timeout_s* elapses.
        """
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

        buffers = {ch: self._register_buffer(ch, n_samples)[0] for ch in active}

        post = n_samples - pre_trigger_samples
        time_indisposed = c_int32(0)
        status = self._lib.ps4000aRunBlock(
            self._handle, c_int32(pre_trigger_samples), c_int32(post),
            c_uint32(timebase), byref(time_indisposed), c_uint32(0),
            ps.BlockReady(0), None)
        ps.check(status, "ps4000aRunBlock")

        ready = c_int16(0)
        deadline = threading.Event()
        waited = 0.0
        step = 0.001
        while not ready.value:
            ps.check(self._lib.ps4000aIsReady(self._handle, byref(ready)),
                     "ps4000aIsReady")
            if ready.value:
                break
            deadline.wait(step)
            waited += step
            if waited > timeout_s:
                self._lib.ps4000aStop(self._handle)
                raise PicoError(0x3008, "capture_block",
                                f"no capture within {timeout_s:g} s "
                                "(waiting on a trigger that never came?)")

        got = c_uint32(n_samples)
        overflow = c_int16(0)
        status = self._lib.ps4000aGetValues(
            self._handle, c_uint32(0), byref(got), c_uint32(1),
            c_int32(int(RatioMode.NONE)), c_uint32(0), byref(overflow))
        ps.check(status, "ps4000aGetValues")
        self._lib.ps4000aStop(self._handle)

        n = got.value
        counts = {ch.label: buffers[ch][:n].copy() for ch in active}
        volts = {
            ch.label: ps.adc_to_volts(counts[ch.label].astype(np.float32),
                                      self.channels[ch].voltage_range)
            for ch in active
        }
        over = {ch.label: bool(overflow.value & (1 << int(ch))) for ch in active}
        return Capture(active, counts, volts, interval_ns, n, over)

    # --- streaming (raw driver calls; see streaming.py for the thread) -----

    def start_streaming(self, sample_interval_ns: int,
                        buffer_samples: int = 200_000,
                        downsample_ratio: int = 1,
                        downsample_mode: RatioMode = RatioMode.NONE,
                        auto_stop: bool = False,
                        max_pre_trigger: int = 0,
                        max_post_trigger: int = 0) -> tuple[float, dict]:
        """Begin streaming; return ``(actual_interval_ns, buffers)``.

        *buffers* maps channel label to ``{"max": array, "min": array|None}`` —
        the driver's circular buffers, already registered. Callers read from
        them inside the streaming callback, using the ``startIndex`` the driver
        supplies.

        Driver-side downsampling (*downsample_ratio* / *downsample_mode*) is
        applied **before** data crosses USB, so it cuts bus load as well as
        disk. ``AGGREGATE`` keeps a min and a max per bin and is the right
        choice for monitoring — see :class:`~picoscope_4824a.ps4000a.RatioMode`.
        """
        self._require_open()
        active = self.enabled_channels
        if not active:
            raise PicoError(0x10, "start_streaming", "no channels are enabled")

        buffers: dict[str, dict] = {}
        for ch in active:
            b_max, b_min = self._register_buffer(ch, buffer_samples, downsample_mode)
            buffers[ch.label] = {"max": b_max, "min": b_min}

        interval = c_uint32(int(sample_interval_ns))
        status = self._lib.ps4000aRunStreaming(
            self._handle, byref(interval), c_int32(int(TimeUnits.NS)),
            c_uint32(max_pre_trigger), c_uint32(max_post_trigger or 0xFFFFFFFF),
            c_int16(1 if auto_stop else 0), c_uint32(max(1, downsample_ratio)),
            c_int32(int(downsample_mode)), c_uint32(buffer_samples))
        ps.check(status, "ps4000aRunStreaming")
        return float(interval.value), buffers

    def poll_streaming(self, callback) -> int:
        """Pump the driver once; it invokes *callback* if data is waiting.

        *callback* must be a :data:`~picoscope_4824a.ps4000a.StreamingReady`
        instance whose reference the caller keeps alive. Returns the raw status
        so the caller can distinguish "no data yet" (``PICO_BUSY``) from a real
        fault without an exception on the hot path.
        """
        return self._lib.ps4000aGetStreamingLatestValues(
            self._handle, callback, None) & 0xFFFFFFFF

    def stop(self) -> None:
        """Stop any acquisition in progress."""
        if self._open:
            ps.check(self._lib.ps4000aStop(self._handle), "ps4000aStop")

    # --- signal generator -------------------------------------------------

    def set_signal_generator(self, wave_type: WaveType = WaveType.SINE,
                             frequency_hz: float = 1000.0,
                             amplitude_vpp: float = 2.0,
                             offset_v: float = 0.0,
                             stop_frequency_hz: Optional[float] = None,
                             increment_hz: float = 0.0,
                             dwell_time_s: float = 1.0,
                             sweep_type: ps.SweepType = ps.SweepType.UP,
                             shots: int = 0, sweeps: int = 0,
                             trigger_type: ps.SigGenTrigType = ps.SigGenTrigType.RISING,
                             trigger_source: ps.SigGenTrigSource = ps.SigGenTrigSource.NONE,
                             ) -> None:
        """Drive the built-in generator.

        Measured limits on this unit: **4 Vpp** maximum amplitude and **1 MHz**
        maximum frequency; exceeding either is rejected by the driver
        (``PICO_SIGGEN_PK_TO_PK`` / ``PICO_SIG_GEN_PARAM``). We check here so
        the error names the actual limit instead of a status code.
        """
        self._require_open()
        pk_to_pk_uv = int(round(amplitude_vpp * 1e6))
        if pk_to_pk_uv > ps.SIGGEN_MAX_PK_TO_PK_UV:
            raise ValueError(
                f"amplitude {amplitude_vpp:g} Vpp exceeds the 4824A's "
                f"{ps.SIGGEN_MAX_PK_TO_PK_UV / 1e6:g} Vpp maximum")
        if frequency_hz > ps.SIGGEN_MAX_FREQUENCY_HZ:
            raise ValueError(
                f"frequency {frequency_hz:g} Hz exceeds the 4824A's "
                f"{ps.SIGGEN_MAX_FREQUENCY_HZ / 1e6:g} MHz maximum")

        stop_hz = stop_frequency_hz if stop_frequency_hz is not None else frequency_hz
        status = self._lib.ps4000aSetSigGenBuiltIn(
            self._handle, c_int32(int(round(offset_v * 1e6))),
            c_uint32(pk_to_pk_uv), c_int32(int(wave_type)),
            ctypes.c_double(frequency_hz), ctypes.c_double(stop_hz),
            ctypes.c_double(increment_hz), ctypes.c_double(dwell_time_s),
            c_int32(int(sweep_type)), c_int32(int(ps.ExtraOperations.OFF)),
            c_uint32(shots), c_uint32(sweeps), c_int32(int(trigger_type)),
            c_int32(int(trigger_source)), c_int16(0))
        ps.check(status, "ps4000aSetSigGenBuiltIn")

    def signal_generator_off(self) -> None:
        """Silence the generator (DC at 0 V, zero amplitude)."""
        self._require_open()
        status = self._lib.ps4000aSetSigGenBuiltIn(
            self._handle, c_int32(0), c_uint32(0),
            c_int32(int(WaveType.DC_VOLTAGE)),
            ctypes.c_double(0.0), ctypes.c_double(0.0), ctypes.c_double(0.0),
            ctypes.c_double(1.0), c_int32(0), c_int32(0), c_uint32(0),
            c_uint32(0), c_int32(0), c_int32(0), c_int16(0))
        ps.check(status, "ps4000aSetSigGenBuiltIn", "off")

    def arbitrary_waveform_limits(self) -> dict:
        """AWG sample range and buffer-length limits, straight from the driver."""
        self._require_open()
        mn, mx = c_int16(0), c_int16(0)
        lo, hi = c_uint32(0), c_uint32(0)
        status = self._lib.ps4000aSigGenArbitraryMinMaxValues(
            self._handle, byref(mn), byref(mx), byref(lo), byref(hi))
        ps.check(status, "ps4000aSigGenArbitraryMinMaxValues")
        return {"sample_min": mn.value, "sample_max": mx.value,
                "buffer_min": lo.value, "buffer_max": hi.value}

    def frequency_to_phase(self, frequency_hz: float, buffer_length: int,
                           index_mode: ps.IndexMode = ps.IndexMode.SINGLE) -> int:
        """DDS phase increment that plays *buffer_length* points at *frequency_hz*."""
        self._require_open()
        phase = c_uint32(0)
        status = self._lib.ps4000aSigGenFrequencyToPhase(
            self._handle, ctypes.c_double(frequency_hz), c_int32(int(index_mode)),
            c_uint32(buffer_length), byref(phase))
        ps.check(status, "ps4000aSigGenFrequencyToPhase")
        return phase.value

    def set_arbitrary_waveform(self, samples: Sequence[float],
                               frequency_hz: float = 1000.0,
                               amplitude_vpp: float = 2.0,
                               offset_v: float = 0.0,
                               shots: int = 0, sweeps: int = 0) -> dict:
        """Upload a normalised waveform (values in [-1, +1]) and play it.

        The AWG buffer holds 1..16384 points — the same budget as the RIGOL
        DG822, so waveform-generation code written for that instrument (optimal
        point counts, frequency combs) carries over directly.
        """
        self._require_open()
        arr = np.asarray(samples, dtype=np.float64)
        if arr.size < ps.AWG_BUFFER_MIN or arr.size > ps.AWG_BUFFER_MAX:
            raise ValueError(
                f"waveform has {arr.size} points; the AWG buffer holds "
                f"{ps.AWG_BUFFER_MIN}..{ps.AWG_BUFFER_MAX}")
        peak = float(np.max(np.abs(arr))) or 1.0
        counts = np.clip(np.round(arr / peak * ps.MAX_ADC),
                         -ps.MAX_ADC, ps.MAX_ADC).astype(np.int16)
        self._awg_buffer = counts                     # keep alive for the driver
        delta = self.frequency_to_phase(frequency_hz, counts.size)

        pk_to_pk_uv = int(round(amplitude_vpp * 1e6))
        if pk_to_pk_uv > ps.SIGGEN_MAX_PK_TO_PK_UV:
            raise ValueError(
                f"amplitude {amplitude_vpp:g} Vpp exceeds the 4824A's "
                f"{ps.SIGGEN_MAX_PK_TO_PK_UV / 1e6:g} Vpp maximum")

        status = self._lib.ps4000aSetSigGenArbitrary(
            self._handle, c_int32(int(round(offset_v * 1e6))), c_uint32(pk_to_pk_uv),
            c_uint32(delta), c_uint32(delta), c_uint32(0), c_uint32(0),
            counts.ctypes.data_as(ctypes.POINTER(c_int16)), c_int32(counts.size),
            c_int32(int(ps.SweepType.UP)), c_int32(int(ps.ExtraOperations.OFF)),
            c_int32(int(ps.IndexMode.SINGLE)), c_uint32(shots), c_uint32(sweeps),
            c_int32(int(ps.SigGenTrigType.RISING)),
            c_int32(int(ps.SigGenTrigSource.NONE)), c_int16(0))
        ps.check(status, "ps4000aSetSigGenArbitrary")
        return {"points": int(counts.size), "delta_phase": delta,
                "frequency_hz": frequency_hz, "amplitude_vpp": amplitude_vpp}

    # --- state ------------------------------------------------------------

    def state(self) -> dict:
        """Everything the surfaces need to render the instrument's current setup."""
        n = len(self.enabled_channels)
        return {
            "open": self._open,
            "handle": self._handle.value,
            "channels": [c.to_dict() for c in self.channels.values()],
            "enabled_channels": [c.label for c in self.enabled_channels],
            "trigger": self.trigger.to_dict(),
            "max_sample_rate_hz": self.max_sample_rate() if self._open else None,
            "channel_count_note": (
                "80 MS/s with 1-4 channels, 40 MS/s with 5-8" if n else None),
            "on_usb2_port": self.on_usb2_port,
        }
