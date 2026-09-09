"""Raw ``ctypes`` binding to Pico Technology's **ps4000a** driver.

This is the bottom of the stack: it finds ``ps4000a.dll``, declares every driver
function this project uses with explicit ``argtypes``/``restype``, and turns
``PICO_STATUS`` return codes into Python exceptions. Nothing above this module
touches ``ctypes``.

**Why hand-rolled rather than Pico's own** ``picosdk`` **wrapper.** The official
package resolves the driver with :func:`ctypes.util.find_library`, which on
Windows searches only ``PATH``; it also loads the DLL at *import* time from a
module-level singleton. On a machine where the standalone PicoSDK was never
installed — and the only ``ps4000a.dll`` is the one bundled with the PicoScope 7
application — that import simply fails. Binding the ~40 calls we need directly
costs one file and buys explicit control over discovery, prototypes, and errors.

**Discovery order** (first hit wins), see :func:`load_library`:

1. ``$PICOSCOPE_PS4000A_DLL`` — full path to the DLL, for an unusual install.
2. ``C:\\Program Files\\Pico Technology\\SDK\\lib`` — the standalone PicoSDK.
3. The PicoScope 7 / PicoScope 6 application directories.
4. Bare ``ps4000a.dll``, letting the OS loader search ``PATH``.

``ps4000a.dll`` imports only System32 libraries (``winusb``, ``setupapi``,
``kernel32``, ...), so no sibling DLLs need to be on the search path;
:func:`os.add_dll_directory` is still called for the chosen directory, which
costs nothing and keeps us safe if a future driver build gains a private
dependency.

Reference: *PicoScope 4000 Series (A API) Programmer's Guide*, ps4000apg.en.
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys
from ctypes import (CFUNCTYPE, POINTER, c_char_p, c_double, c_float, c_int16,
                    c_int32, c_int64, c_uint32, c_void_p)
from enum import IntEnum
from typing import Optional

# --------------------------------------------------------------------------
# Library discovery
# --------------------------------------------------------------------------

#: Directories searched for ``ps4000a.dll``, in order, after the env override.
WINDOWS_SEARCH_DIRS = (
    r"C:\Program Files\Pico Technology\SDK\lib",
    r"C:\Program Files\Pico Technology\PicoScope 7 T&M Stable",
    r"C:\Program Files\Pico Technology\PicoScope 7 T&M Early Access",
    r"C:\Program Files\Pico Technology\PicoScope 7 T&M",
    r"C:\Program Files\Pico Technology\PicoScope6",
    r"C:\Program Files (x86)\Pico Technology\SDK\lib",
)

#: Where the driver lives on Linux (Pico ships a .deb/.rpm into /opt/picoscope).
POSIX_SEARCH_DIRS = (
    "/opt/picoscope/lib",
    "/usr/local/lib",
    "/usr/lib",
)

ENV_OVERRIDE = "PICOSCOPE_PS4000A_DLL"


class PicoSDKNotFound(RuntimeError):
    """Raised when ``ps4000a.dll`` (or ``libps4000a.so``) cannot be located."""


def _candidate_paths() -> list[str]:
    """Every path :func:`load_library` will try, in order."""
    override = os.environ.get(ENV_OVERRIDE)
    out: list[str] = []
    if override:
        out.append(override)
    if platform.system() == "Windows":
        for d in WINDOWS_SEARCH_DIRS:
            out.append(os.path.join(d, "ps4000a.dll"))
        out.append("ps4000a.dll")               # let the OS loader search PATH
    else:
        for d in POSIX_SEARCH_DIRS:
            out.append(os.path.join(d, "libps4000a.so"))
        out.append("libps4000a.so")
    return out


_lib: Optional[ctypes.CDLL] = None
_lib_path: Optional[str] = None


def load_library(force: bool = False) -> ctypes.CDLL:
    """Locate and load the ps4000a driver, declaring every prototype.

    The handle is cached: repeated calls return the same object. Raises
    :class:`PicoSDKNotFound` with the full list of paths tried if the driver is
    not present — a message you can act on, rather than a bare OSError.
    """
    global _lib, _lib_path
    if _lib is not None and not force:
        return _lib

    tried: list[str] = []
    for path in _candidate_paths():
        directory = os.path.dirname(path)
        # A bare filename ("ps4000a.dll") has no directory: fall through to the
        # OS loader. A real path that does not exist is not worth a load attempt.
        if directory:
            if not os.path.isfile(path):
                tried.append(f"{path}  (no such file)")
                continue
            if platform.system() == "Windows":
                try:
                    os.add_dll_directory(directory)
                except (OSError, AttributeError):
                    pass                        # non-fatal: no private deps today
        try:
            lib = ctypes.WinDLL(path) if platform.system() == "Windows" else ctypes.CDLL(path)
        except OSError as exc:
            tried.append(f"{path}  ({exc})")
            continue
        _declare(lib)
        _lib, _lib_path = lib, path
        return lib

    raise PicoSDKNotFound(
        "could not load the ps4000a driver. Install the PicoSDK, or the "
        "PicoScope 7 application (which bundles the driver), or point "
        f"${ENV_OVERRIDE} at the DLL.\n\nTried:\n  " + "\n  ".join(tried))


def library_path() -> Optional[str]:
    """Path the driver was loaded from, or ``None`` if not yet loaded."""
    return _lib_path


# --------------------------------------------------------------------------
# PICO_STATUS
# --------------------------------------------------------------------------

#: The subset of PicoStatus.h we can meaningfully report on. Unknown codes are
#: still surfaced, as hex — the driver has several hundred and most never occur.
PICO_STATUS_NAMES: dict[int, str] = {
    0x00000000: "PICO_OK",
    0x00000001: "PICO_MAX_UNITS_OPENED",
    0x00000002: "PICO_MEMORY_FAIL",
    0x00000003: "PICO_NOT_FOUND",
    0x00000004: "PICO_FW_FAIL",
    0x00000005: "PICO_OPEN_OPERATION_IN_PROGRESS",
    0x00000006: "PICO_OPERATION_FAILED",
    0x00000007: "PICO_NOT_RESPONDING",
    0x00000008: "PICO_CONFIG_FAIL",
    0x00000009: "PICO_KERNEL_DRIVER_TOO_OLD",
    0x0000000A: "PICO_EEPROM_CORRUPT",
    0x0000000B: "PICO_OS_NOT_SUPPORTED",
    0x0000000C: "PICO_INVALID_HANDLE",
    0x0000000D: "PICO_INVALID_PARAMETER",
    0x0000000E: "PICO_INVALID_TIMEBASE",
    0x0000000F: "PICO_INVALID_VOLTAGE_RANGE",
    0x00000010: "PICO_INVALID_CHANNEL",
    0x00000011: "PICO_INVALID_TRIGGER_CHANNEL",
    0x00000012: "PICO_INVALID_CONDITION_CHANNEL",
    0x00000013: "PICO_NO_SIGNAL_GENERATOR",
    0x00000014: "PICO_STREAMING_FAILED",
    0x00000015: "PICO_BLOCK_MODE_FAILED",
    0x00000016: "PICO_NULL_PARAMETER",
    0x00000018: "PICO_DATA_NOT_AVAILABLE",
    0x00000019: "PICO_STRING_BUFFER_TOO_SMALL",
    0x0000001A: "PICO_ETS_NOT_SUPPORTED",
    0x0000001B: "PICO_AUTO_TRIGGER_TIME_TOO_SHORT",
    0x0000001C: "PICO_BUFFER_STALL",
    0x0000001D: "PICO_TOO_MANY_SAMPLES",
    0x0000001E: "PICO_TOO_MANY_SEGMENTS",
    0x0000001F: "PICO_PULSE_WIDTH_QUALIFIER",
    0x00000020: "PICO_DELAY",
    0x00000021: "PICO_SOURCE_DETAILS",
    0x00000022: "PICO_CONDITIONS",
    0x00000023: "PICO_USER_CALLBACK",
    0x00000024: "PICO_DEVICE_SAMPLING",
    0x00000025: "PICO_NO_SAMPLES_AVAILABLE",
    0x00000026: "PICO_SEGMENT_OUT_OF_RANGE",
    0x00000027: "PICO_BUSY",
    0x00000028: "PICO_STARTINDEX_INVALID",
    0x00000029: "PICO_INVALID_INFO",
    0x0000002A: "PICO_INFO_UNAVAILABLE",
    0x0000002B: "PICO_INVALID_SAMPLE_INTERVAL",
    0x0000002C: "PICO_TRIGGER_ERROR",
    0x0000002D: "PICO_MEMORY",
    0x0000002E: "PICO_SIG_GEN_PARAM",
    0x0000002F: "PICO_SHOTS_SWEEPS_WARNING",
    0x00000030: "PICO_SIGGEN_TRIGGER_SOURCE",
    0x00000031: "PICO_AUX_OUTPUT_CONFLICT",
    0x00000032: "PICO_AUX_OUTPUT_ETS_CONFLICT",
    0x00000033: "PICO_WARNING_EXT_THRESHOLD_CONFLICT",
    0x00000034: "PICO_WARNING_AUX_OUTPUT_CONFLICT",
    0x00000035: "PICO_SIGGEN_OUTPUT_OVER_VOLTAGE",
    0x00000036: "PICO_DELAY_NULL",
    0x00000037: "PICO_INVALID_BUFFER",
    0x00000038: "PICO_SIGGEN_OFFSET_VOLTAGE",
    0x00000039: "PICO_SIGGEN_PK_TO_PK",
    0x0000003A: "PICO_CANCELLED",
    0x0000003B: "PICO_SEGMENT_NOT_USED",
    0x0000003C: "PICO_INVALID_CALL",
    0x0000003F: "PICO_NOT_USED",
    0x00000040: "PICO_INVALID_SAMPLERATIO",
    0x00000041: "PICO_INVALID_STATE",
    0x00000042: "PICO_NOT_ENOUGH_SEGMENTS",
    0x00000043: "PICO_DRIVER_FUNCTION",
    0x00000045: "PICO_INVALID_COUPLING",
    0x00000046: "PICO_BUFFERS_NOT_SET",
    0x00000047: "PICO_RATIO_MODE_NOT_SUPPORTED",
    0x0000004A: "PICO_INVALID_TRIGGER_PROPERTY",
    0x0000004B: "PICO_INTERFACE_NOT_CONNECTED",
    0x0000004E: "PICO_SIG_GEN_WAVEFORM_SETUP_FAILED",
    0x0000004F: "PICO_FPGA_FAIL",
    0x00000055: "PICO_ANALOG_BOARD",
    0x0000005C: "PICO_INVALID_ANALOGUE_OFFSET",
    0x0000005D: "PICO_PLL_LOCK_FAILED",
    0x0000005E: "PICO_ANALOG_BOARD_PLL_FAIL",
    0x00000101: "PICO_DEVICE_TIME_STAMP_RESET",
    0x00000103: "PICO_WATCHDOGTIMER",
    0x00000104: "PICO_IPP_NOT_FOUND",
    0x00000105: "PICO_IPP_NO_FUNCTION",
    0x00000106: "PICO_IPP_ERROR",
    0x00000107: "PICO_SHADOW_CAL_NOT_AVAILABLE",
    0x00000108: "PICO_DEVICE_MEMORY_OVERFLOW",
    0x0000010E: "PICO_RESOURCE_ERROR",
    0x0000011D: "PICO_POWER_SUPPLY_UNDERVOLTAGE",
    0x0000011E: "PICO_USB3_0_DEVICE_NON_USB3_0_PORT",
    0x00000119: "PICO_POWER_SUPPLY_CONNECTED",
    0x0000011A: "PICO_POWER_SUPPLY_NOT_CONNECTED",
    0x00000282: "PICO_PROBE_FAULT",
    0x00003000: "PICO_DEVICE_NOT_FUNCTIONING",
    0x00003001: "PICO_INTERNAL_ERROR",
    0x00003002: "PICO_UNKNOWN_DEVICE",
    0x00003008: "PICO_TIMEOUT",
}

PICO_OK = 0x00000000
PICO_POWER_SUPPLY_CONNECTED = 0x00000119
PICO_POWER_SUPPLY_NOT_CONNECTED = 0x0000011A
PICO_USB3_0_DEVICE_NON_USB3_0_PORT = 0x0000011E
PICO_POWER_SUPPLY_UNDERVOLTAGE = 0x0000011D

#: Statuses that :func:`ps4000aOpenUnit` returns alongside a **valid handle**.
#: They are not failures; they mean "opened — now tell me how it is powered",
#: and are resolved by passing the same code back to ``ps4000aChangePowerSource``.
#: Our 4824A is a USB 3.0 device on a USB 2.0 port, so it always returns
#: ``PICO_USB3_0_DEVICE_NON_USB3_0_PORT`` here.
POWER_STATUSES = frozenset({
    PICO_POWER_SUPPLY_CONNECTED,
    PICO_POWER_SUPPLY_NOT_CONNECTED,
    PICO_USB3_0_DEVICE_NON_USB3_0_PORT,
    PICO_POWER_SUPPLY_UNDERVOLTAGE,
})


def status_name(status: int) -> str:
    """Human name for a ``PICO_STATUS``, falling back to hex."""
    code = status & 0xFFFFFFFF
    return PICO_STATUS_NAMES.get(code, f"0x{code:08X}")


class PicoError(RuntimeError):
    """A ps4000a call returned a non-OK ``PICO_STATUS``.

    Carries the numeric ``status`` and the driver function name so callers can
    branch on specific codes (e.g. tolerate ``PICO_BUSY``) rather than parsing
    the message.
    """

    def __init__(self, status: int, function: str = "", detail: str = "") -> None:
        self.status = status & 0xFFFFFFFF
        self.function = function
        name = status_name(status)
        msg = f"{function}: {name}" if function else name
        if detail:
            msg = f"{msg} — {detail}"
        super().__init__(msg)

    @property
    def name(self) -> str:
        return status_name(self.status)


def check(status: int, function: str = "", detail: str = "") -> int:
    """Raise :class:`PicoError` unless *status* is ``PICO_OK``; else return it."""
    if (status & 0xFFFFFFFF) != PICO_OK:
        raise PicoError(status, function, detail)
    return status


# --------------------------------------------------------------------------
# Enumerations (PS4000A_* from ps4000aApi.h)
# --------------------------------------------------------------------------


class Channel(IntEnum):
    """Analogue input channels. The 4824A has eight, A through H."""
    A = 0
    B = 1
    C = 2
    D = 3
    E = 4
    F = 5
    G = 6
    H = 7

    @property
    def label(self) -> str:
        return self.name


class Coupling(IntEnum):
    AC = 0
    DC = 1


class Range(IntEnum):
    """Input ranges. The value is the enum the driver wants; :attr:`volts` is
    the full-scale deflection in volts (i.e. the range is ±:attr:`volts`)."""
    R_10MV = 0
    R_20MV = 1
    R_50MV = 2
    R_100MV = 3
    R_200MV = 4
    R_500MV = 5
    R_1V = 6
    R_2V = 7
    R_5V = 8
    R_10V = 9
    R_20V = 10
    R_50V = 11

    @property
    def volts(self) -> float:
        """Full-scale deflection in volts (the range is ±this)."""
        return _RANGE_VOLTS[int(self)]

    @property
    def label(self) -> str:
        """Human label, e.g. ``'±5 V'``."""
        v = self.volts
        return f"±{v * 1000:g} mV" if v < 1 else f"±{v:g} V"

    @classmethod
    def from_volts(cls, volts: float) -> "Range":
        """Smallest range that contains ±*volts* (the most sensitive that fits)."""
        for r in cls:
            if r.volts >= volts - 1e-12:
                return r
        return cls.R_50V


_RANGE_VOLTS = {
    0: 0.01, 1: 0.02, 2: 0.05, 3: 0.1, 4: 0.2, 5: 0.5,
    6: 1.0, 7: 2.0, 8: 5.0, 9: 10.0, 10: 20.0, 11: 50.0,
}


class RatioMode(IntEnum):
    """Driver-side downsampling applied before data crosses USB.

    This is the single most important knob for long monitoring runs: it cuts
    bus traffic *and* disk footprint together. ``AGGREGATE`` is the right
    default for monitoring — it keeps the minimum **and** maximum of every bin,
    so a transient far shorter than the output period still shows up in the
    envelope, which ``DECIMATE`` and ``AVERAGE`` would both hide.

    ``AGGREGATE`` needs two buffers per channel (:func:`ps4000aSetDataBuffers`);
    the others need one.
    """
    NONE = 0
    AGGREGATE = 1
    DECIMATE = 2
    AVERAGE = 4


class TimeUnits(IntEnum):
    FS = 0
    PS = 1
    NS = 2
    US = 3
    MS = 4
    S = 5

    @property
    def seconds(self) -> float:
        """One unit expressed in seconds."""
        return (1e-15, 1e-12, 1e-9, 1e-6, 1e-3, 1.0)[int(self)]


class ThresholdDirection(IntEnum):
    """Edge/level directions for :func:`ps4000aSetSimpleTrigger`."""
    ABOVE = 0
    BELOW = 1
    RISING = 2
    FALLING = 3
    RISING_OR_FALLING = 4
    NONE = 2                       # the driver reuses RISING as "don't care"


class WaveType(IntEnum):
    """Built-in signal generator waveforms."""
    SINE = 0
    SQUARE = 1
    TRIANGLE = 2
    RAMP_UP = 3
    RAMP_DOWN = 4
    SINC = 5
    GAUSSIAN = 6
    HALF_SINE = 7
    DC_VOLTAGE = 8
    WHITE_NOISE = 9


class SweepType(IntEnum):
    UP = 0
    DOWN = 1
    UPDOWN = 2
    DOWNUP = 3


class ExtraOperations(IntEnum):
    OFF = 0
    WHITENOISE = 1
    PRBS = 2


class SigGenTrigType(IntEnum):
    RISING = 0
    FALLING = 1
    GATE_HIGH = 2
    GATE_LOW = 3


class SigGenTrigSource(IntEnum):
    NONE = 0
    SCOPE_TRIG = 1
    AUX_IN = 2
    EXT_IN = 3
    SOFT_TRIG = 4


class IndexMode(IntEnum):
    SINGLE = 0
    DUAL = 1
    QUAD = 2


class PicoInfo(IntEnum):
    """``ps4000aGetUnitInfo`` selectors. Verified against our 4824A."""
    DRIVER_VERSION = 0
    USB_VERSION = 1
    HARDWARE_VERSION = 2
    VARIANT_INFO = 3
    BATCH_AND_SERIAL = 4
    CAL_DATE = 5
    KERNEL_VERSION = 6
    DIGITAL_HARDWARE_VERSION = 7
    ANALOGUE_HARDWARE_VERSION = 8
    FIRMWARE_VERSION_1 = 9
    FIRMWARE_VERSION_2 = 10


class BandwidthLimiter(IntEnum):
    FULL = 0
    BW_20MHZ = 1


# --------------------------------------------------------------------------
# Hardware constants for the 4824A (measured + Programmer's Guide)
# --------------------------------------------------------------------------

#: ADC full-scale count. 12-bit samples are left-aligned into an int16, so
#: full scale is 32767 — **not** 32768, and not 2047.
MAX_ADC = 32767
MIN_ADC = -32767

#: Sample interval for block-mode timebase *n*: ``12.5 ns × (n + 1)``.
#: Equivalently ``rate = 80 MHz / (n + 1)``. The 4000A series is linear in *n*;
#: the 2^n form quoted in some Pico docs applies to the 4444 only.
TIMEBASE_NS_PER_STEP = 12.5

#: Peak sample rate depends on how many channels are enabled: 80 MS/s with 1-4
#: channels, 40 MS/s with 5-8. So timebase 0 is rejected once a fifth channel
#: comes on, and timebase 1 (25 ns) becomes the floor. Always call
#: ``ps4000aGetTimebase2`` *after* ``ps4000aSetChannel``.
MAX_RATE_1_TO_4_CHANNELS = 80_000_000
MAX_RATE_5_TO_8_CHANNELS = 40_000_000

#: Total capture memory shared across enabled channels (256 MS).
TOTAL_CAPTURE_SAMPLES = 256 * 1024 * 1024

#: Signal generator limits, measured on this unit: 4 Vpp maximum (5 Vpp is
#: rejected with PICO_SIGGEN_PK_TO_PK), 1 MHz maximum (2 MHz is rejected with
#: PICO_SIG_GEN_PARAM), arbitrary buffer 1..16384 samples.
SIGGEN_MAX_PK_TO_PK_UV = 4_000_000
SIGGEN_MAX_FREQUENCY_HZ = 1_000_000.0
SIGGEN_MIN_FREQUENCY_HZ = 0.0
AWG_BUFFER_MIN = 1
AWG_BUFFER_MAX = 16384

#: Measured sustained streaming ceiling on a USB 2.0 port, aggregated over all
#: enabled channels. A USB 3.0 port lifts this substantially. Used by the
#: controller to warn before a run that will drop samples.
USB2_AGGREGATE_SAMPLES_PER_SEC = 21_000_000

CHANNEL_COUNT = 8


def adc_to_volts(counts, voltage_range: Range):
    """Convert raw ADC counts to volts for *voltage_range*.

    Works on a scalar or a numpy array: ``v = counts × range_volts / 32767``.
    """
    return counts * (voltage_range.volts / MAX_ADC)


def volts_to_adc(volts: float, voltage_range: Range) -> int:
    """Convert volts to ADC counts, clamped to the ADC's range."""
    counts = int(round(volts * MAX_ADC / voltage_range.volts))
    return max(MIN_ADC, min(MAX_ADC, counts))


def timebase_to_interval_ns(timebase: int) -> float:
    """Sample interval in nanoseconds for block-mode *timebase*."""
    return TIMEBASE_NS_PER_STEP * (timebase + 1)


def interval_ns_to_timebase(interval_ns: float) -> int:
    """Nearest block-mode timebase for a requested interval (never negative)."""
    return max(0, int(round(interval_ns / TIMEBASE_NS_PER_STEP)) - 1)


# --------------------------------------------------------------------------
# Callback prototypes
# --------------------------------------------------------------------------

#: ``void (*ps4000aStreamingReady)(int16_t handle, int32_t noOfSamples,
#:   uint32_t startIndex, int16_t overflow, uint32_t triggerAt,
#:   int16_t triggered, int16_t autoStop, void *pParameter)``
StreamingReady = CFUNCTYPE(None, c_int16, c_int32, c_uint32, c_int16,
                           c_uint32, c_int16, c_int16, c_void_p)

#: ``void (*ps4000aBlockReady)(int16_t handle, PICO_STATUS status,
#:   void *pParameter)``
BlockReady = CFUNCTYPE(None, c_int16, c_uint32, c_void_p)


# --------------------------------------------------------------------------
# Prototypes
# --------------------------------------------------------------------------

def _declare(lib: ctypes.CDLL) -> None:
    """Attach ``argtypes``/``restype`` to every function we call.

    Declaring these is not optional bookkeeping. ``ps4000aSetChannel`` takes a
    ``float`` analogue offset and ``ps4000aSetSigGenBuiltIn`` takes five
    ``double``s; without argtypes ctypes promotes Python floats using C's
    default argument promotion and the driver reads garbage.
    """
    S = c_uint32                                   # PICO_STATUS

    def sig(name: str, argtypes, restype=S) -> None:
        fn = getattr(lib, name, None)
        if fn is None:                             # older driver: skip silently
            return
        fn.argtypes = argtypes
        fn.restype = restype

    # --- lifecycle ---
    sig("ps4000aOpenUnit", [POINTER(c_int16), c_char_p])
    sig("ps4000aOpenUnitAsync", [POINTER(c_int16), c_char_p])
    sig("ps4000aOpenUnitProgress", [POINTER(c_int16), POINTER(c_int16), POINTER(c_int16)])
    sig("ps4000aCloseUnit", [c_int16])
    sig("ps4000aPingUnit", [c_int16])
    sig("ps4000aGetUnitInfo", [c_int16, c_char_p, c_int16, POINTER(c_int16), c_int32])
    sig("ps4000aEnumerateUnits", [POINTER(c_int16), c_char_p, POINTER(c_int16)])
    sig("ps4000aChangePowerSource", [c_int16, S])
    sig("ps4000aCurrentPowerSource", [c_int16])
    sig("ps4000aFlashLed", [c_int16, c_int16])

    # --- channels ---
    sig("ps4000aSetChannel", [c_int16, c_int32, c_int16, c_int32, c_int32, c_float])
    sig("ps4000aGetAnalogueOffset",
        [c_int16, c_int32, c_int32, POINTER(c_float), POINTER(c_float)])
    sig("ps4000aSetBandwidthFilter", [c_int16, c_int32, c_int32])
    sig("ps4000aMaximumValue", [c_int16, POINTER(c_int16)])
    sig("ps4000aMinimumValue", [c_int16, POINTER(c_int16)])

    # --- timebase ---
    sig("ps4000aGetTimebase2",
        [c_int16, c_uint32, c_int32, POINTER(c_float), POINTER(c_int32), c_uint32])
    sig("ps4000aGetMaxSegments", [c_int16, POINTER(c_uint32)])
    sig("ps4000aMemorySegments", [c_int16, c_uint32, POINTER(c_int32)])

    # --- buffers ---
    sig("ps4000aSetDataBuffer",
        [c_int16, c_int32, POINTER(c_int16), c_int32, c_uint32, c_int32])
    sig("ps4000aSetDataBuffers",
        [c_int16, c_int32, POINTER(c_int16), POINTER(c_int16), c_int32, c_uint32, c_int32])

    # --- block mode ---
    sig("ps4000aRunBlock",
        [c_int16, c_int32, c_int32, c_uint32, POINTER(c_int32), c_uint32,
         BlockReady, c_void_p])
    sig("ps4000aIsReady", [c_int16, POINTER(c_int16)])
    sig("ps4000aGetValues",
        [c_int16, c_uint32, POINTER(c_uint32), c_uint32, c_int32, c_uint32,
         POINTER(c_int16)])
    sig("ps4000aGetValuesBulk",
        [c_int16, POINTER(c_uint32), c_uint32, c_uint32, c_uint32, c_int32,
         POINTER(c_int16)])
    sig("ps4000aSetNoOfCaptures", [c_int16, c_uint32])
    sig("ps4000aGetNoOfCaptures", [c_int16, POINTER(c_uint32)])
    sig("ps4000aGetTriggerTimeOffset64",
        [c_int16, POINTER(c_int64), POINTER(c_int32), c_uint32])

    # --- streaming ---
    sig("ps4000aRunStreaming",
        [c_int16, POINTER(c_uint32), c_int32, c_uint32, c_uint32, c_int16,
         c_uint32, c_int32, c_uint32])
    sig("ps4000aGetStreamingLatestValues", [c_int16, StreamingReady, c_void_p])
    sig("ps4000aNoOfStreamingValues", [c_int16, POINTER(c_uint32)])
    sig("ps4000aStop", [c_int16])

    # --- triggering ---
    sig("ps4000aSetSimpleTrigger",
        [c_int16, c_int16, c_int32, c_int16, c_int32, c_uint32, c_int16])
    sig("ps4000aSetTriggerDelay", [c_int16, c_uint32])

    # --- signal generator ---
    sig("ps4000aSetSigGenBuiltIn",
        [c_int16, c_int32, c_uint32, c_int32, c_double, c_double, c_double,
         c_double, c_int32, c_int32, c_uint32, c_uint32, c_int32, c_int32, c_int16])
    # 17 args: handle, offsetVoltage, pkToPk, startDeltaPhase, stopDeltaPhase,
    # deltaPhaseIncrement, dwellCount, waveform, waveformSize, sweepType,
    # operation, indexMode, shots, sweeps, triggerType, triggerSource,
    # extInThreshold.
    sig("ps4000aSetSigGenArbitrary",
        [c_int16, c_int32, c_uint32, c_uint32, c_uint32, c_uint32, c_uint32,
         POINTER(c_int16), c_int32, c_int32, c_int32, c_int32, c_uint32,
         c_uint32, c_int32, c_int32, c_int16])
    sig("ps4000aSetSigGenPropertiesBuiltIn",
        [c_int16, c_double, c_double, c_double, c_double, c_int32, c_uint32,
         c_uint32, c_int32, c_int32, c_int16])
    sig("ps4000aSigGenSoftwareControl", [c_int16, c_int16])
    sig("ps4000aSigGenArbitraryMinMaxValues",
        [c_int16, POINTER(c_int16), POINTER(c_int16), POINTER(c_uint32),
         POINTER(c_uint32)])
    sig("ps4000aSigGenFrequencyToPhase",
        [c_int16, c_double, c_int32, c_uint32, POINTER(c_uint32)])


__all__ = [
    "load_library", "library_path", "PicoSDKNotFound", "ENV_OVERRIDE",
    "PicoError", "check", "status_name", "PICO_STATUS_NAMES", "PICO_OK",
    "POWER_STATUSES", "PICO_USB3_0_DEVICE_NON_USB3_0_PORT",
    "Channel", "Coupling", "Range", "RatioMode", "TimeUnits",
    "ThresholdDirection", "WaveType", "SweepType", "ExtraOperations",
    "SigGenTrigType", "SigGenTrigSource", "IndexMode", "PicoInfo",
    "BandwidthLimiter",
    "StreamingReady", "BlockReady",
    "MAX_ADC", "MIN_ADC", "CHANNEL_COUNT", "TOTAL_CAPTURE_SAMPLES",
    "SIGGEN_MAX_PK_TO_PK_UV", "SIGGEN_MAX_FREQUENCY_HZ",
    "AWG_BUFFER_MIN", "AWG_BUFFER_MAX", "USB2_AGGREGATE_SAMPLES_PER_SEC",
    "adc_to_volts", "volts_to_adc",
    "timebase_to_interval_ns", "interval_ns_to_timebase",
]
