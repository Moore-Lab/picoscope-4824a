"""The command table — one description of what the instrument can do.

This module is the reason the CLI and the web UI cannot drift apart. Every
capability is declared **once**, here, as data: a name, a group, a summary, and
its parameters with types and defaults. The three surfaces then *render* this
table rather than re-implementing it:

* :mod:`picoscope_4824a.cli` builds an ``argparse`` subcommand per entry.
* :mod:`picoscope_4824a.webapp` builds an HTTP route per entry.
* the browser UI fetches ``/describe`` and builds its forms from the same JSON.

Each :class:`Command` names a method on
:class:`~picoscope_4824a.controller.ScopeController`; the surfaces call that
method with the parameters they collected. Adding a capability means adding a
controller method and one entry here — and it appears in all three places at
once. ``tests/test_parity.py`` walks the table and fails if any command is
missing a handler or a route, so the guarantee is enforced rather than merely
intended.

The table is also what makes the instrument **legible to an agent**: ``pico
describe --json`` dumps the whole tree — every command, every parameter, every
type, every choice — so a caller that has never seen this tool can discover its
full capability in one call, then invoke anything in it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Param:
    """One parameter of one command."""
    name: str
    type: str = "str"                 # str | int | float | bool | enum
    help: str = ""
    default: Any = None
    choices: Optional[tuple] = None
    required: bool = False
    #: Unit for display, e.g. ``"V"``, ``"Hz"``, ``"ns"``. Documentation only.
    unit: str = ""

    def to_dict(self) -> dict:
        out = {
            "name": self.name,
            "type": self.type,
            "help": self.help,
            "required": self.required,
        }
        if self.default is not None:
            out["default"] = self.default
        if self.choices:
            out["choices"] = list(self.choices)
        if self.unit:
            out["unit"] = self.unit
        return out


@dataclass(frozen=True)
class Command:
    """One capability: a controller method plus the parameters it accepts."""
    name: str                          # CLI subcommand, e.g. "channel-set"
    group: str                         # "device" | "channel" | "stream" | ...
    summary: str
    handler: str                       # ScopeController method name
    params: tuple[Param, ...] = ()
    #: True if the command changes instrument or run state. Read-only commands
    #: are safe to call at any time, which matters for an agent exploring.
    mutates: bool = False
    #: Longer prose shown by ``--help`` and in the web UI's tooltip.
    detail: str = ""

    @property
    def route(self) -> str:
        """HTTP path for this command, e.g. ``/api/channel-set``."""
        return f"/api/{self.name}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "group": self.group,
            "summary": self.summary,
            "detail": self.detail,
            "handler": self.handler,
            "mutates": self.mutates,
            "method": "POST" if self.mutates else "GET",
            "route": self.route,
            "params": [p.to_dict() for p in self.params],
        }


CHANNELS = ("A", "B", "C", "D", "E", "F", "G", "H")
RANGES = ("R_10MV", "R_20MV", "R_50MV", "R_100MV", "R_200MV", "R_500MV",
          "R_1V", "R_2V", "R_5V", "R_10V", "R_20V", "R_50V")
COUPLINGS = ("AC", "DC")
RATIO_MODES = ("NONE", "AGGREGATE", "DECIMATE", "AVERAGE")
DIRECTIONS = ("ABOVE", "BELOW", "RISING", "FALLING", "RISING_OR_FALLING")
WAVES = ("SINE", "SQUARE", "TRIANGLE", "RAMP_UP", "RAMP_DOWN", "SINC",
         "GAUSSIAN", "HALF_SINE", "DC_VOLTAGE", "WHITE_NOISE")


COMMANDS: tuple[Command, ...] = (

    # --- device -----------------------------------------------------------
    Command(
        name="list", group="device", handler="list_devices",
        summary="List every 4000A-series scope attached to this machine.",
        detail="Safe to call with the scope closed, and while the PicoScope "
               "desktop application is running.",
    ),
    Command(
        name="open", group="device", handler="open", mutates=True,
        summary="Open the scope and complete the power-source handshake.",
        detail="A 4824A on a USB 2.0 port reports "
               "PICO_USB3_0_DEVICE_NON_USB3_0_PORT on open; that is a warning, "
               "not a failure, and is acknowledged automatically.",
        params=(
            Param("serial", "str", "Serial number, e.g. JY140/0294. "
                                   "Omit to take the first unit found."),
        ),
    ),
    Command(
        name="close", group="device", handler="close", mutates=True,
        summary="Stop acquisition and release the scope.",
        detail="Always close before exiting. A process killed mid-stream can "
               "leave a thread spinning inside the driver, which wedges the "
               "device until it is physically re-plugged.",
    ),
    Command(
        name="info", group="device", handler="info",
        summary="Model, serial, driver and firmware versions, power state.",
    ),
    Command(
        name="status", group="device", handler="status",
        summary="Everything at once: device, channels, trigger, stream, recorder.",
        detail="The single call an agent should poll. Returns the whole "
               "instrument state as one JSON document.",
    ),
    Command(
        name="ping", group="device", handler="ping",
        summary="Check the scope is still responding.",
    ),
    Command(
        name="flash", group="device", handler="flash_led", mutates=True,
        summary="Flash the front-panel LED to identify the unit.",
        params=(Param("count", "int", "Number of flashes.", default=3),),
    ),

    # --- channels ---------------------------------------------------------
    Command(
        name="channels", group="channel", handler="get_channels",
        summary="Show how every input is configured.",
    ),
    Command(
        name="channel-set", group="channel", handler="set_channel", mutates=True,
        summary="Configure one input: enable, coupling, range, offset.",
        detail="Ranges are the full-scale deflection, so R_5V means ±5 V. "
               "Fewer enabled channels allow a higher sample rate: 80 MS/s "
               "with 1-4 channels, 40 MS/s with 5-8.",
        params=(
            Param("channel", "enum", "Which input.", choices=CHANNELS,
                  required=True),
            Param("enabled", "bool", "Turn the channel on or off.", default=True),
            Param("range", "enum", "Full-scale deflection (±).", choices=RANGES,
                  default="R_5V"),
            Param("coupling", "enum", "Input coupling.", choices=COUPLINGS,
                  default="DC"),
            Param("offset", "float", "Analogue offset added before the ADC.",
                  default=0.0, unit="V"),
        ),
    ),
    Command(
        name="channel-range-for", group="channel", handler="range_for_volts",
        summary="Smallest range that contains a given signal amplitude.",
        params=(Param("volts", "float", "Peak amplitude to accommodate.",
                      required=True, unit="V"),),
    ),

    # --- trigger ----------------------------------------------------------
    Command(
        name="trigger-set", group="trigger", handler="set_trigger", mutates=True,
        summary="Arm or disarm the simple edge/level trigger.",
        detail="auto_trigger_ms is a rescue timer: after this long without an "
               "edge the scope captures anyway. Zero waits forever, which will "
               "hang a block capture if the edge never arrives.",
        params=(
            Param("enabled", "bool", "Arm (true) or disarm (false).", default=True),
            Param("source", "enum", "Channel to trigger on.", choices=CHANNELS,
                  default="A"),
            Param("threshold", "float", "Trigger level.", default=0.0, unit="V"),
            Param("direction", "enum", "Edge or level sense.",
                  choices=DIRECTIONS, default="RISING"),
            Param("delay", "int", "Samples to wait after the trigger.", default=0),
            Param("auto_trigger_ms", "int",
                  "Capture anyway after this long. 0 = wait forever.",
                  default=1000, unit="ms"),
        ),
    ),

    # --- block capture ----------------------------------------------------
    Command(
        name="capture", group="capture", handler="capture_block", mutates=True,
        summary="Take one block capture and return the traces.",
        detail="The classic scope shot: arm, capture n samples at the given "
               "rate, read back. Use --save to write it to disk.",
        params=(
            Param("samples", "int", "Samples per channel.", default=10000),
            Param("rate", "float", "Sample rate per channel.", default=1e6,
                  unit="Hz"),
            Param("pre_trigger", "int", "Samples captured before the trigger.",
                  default=0),
            Param("timeout", "float", "Give up after this long.", default=10.0,
                  unit="s"),
            Param("save", "str", "Write the capture to this path (.h5 or .csv). "
                                 "Omit to return data only."),
            Param("max_points", "int",
                  "Decimate returned traces to at most this many points. "
                  "The saved file always holds every sample.", default=2000),
        ),
    ),
    Command(
        name="timebase", group="capture", handler="describe_timebase",
        summary="What sample rates are achievable right now.",
        detail="Depends on how many channels are enabled, so it reflects the "
               "current configuration.",
        params=(Param("rate", "float", "Rate to resolve to a timebase.",
                      default=1e6, unit="Hz"),),
    ),

    # --- streaming --------------------------------------------------------
    Command(
        name="stream-start", group="stream", handler="start_stream", mutates=True,
        summary="Begin continuous streaming acquisition.",
        detail="The monitoring path. Driver-side downsampling (ratio + mode) is "
               "applied before data crosses USB, so it cuts bus load and disk "
               "together. AGGREGATE keeps the min AND max of every bin, so a "
               "transient far shorter than the output period is still visible — "
               "which is why it is the right default for long watches.",
        params=(
            Param("rate", "float", "Raw sample rate per channel, before "
                                   "downsampling.", default=1e6, unit="Hz"),
            Param("downsample", "int",
                  "Driver-side downsample ratio. 1 = keep every sample.",
                  default=1),
            Param("mode", "enum", "How to downsample.", choices=RATIO_MODES,
                  default="NONE"),
            Param("buffer_samples", "int",
                  "Driver circular buffer size, per channel.", default=200000),
            Param("window", "float", "Seconds of history the live view keeps.",
                  default=10.0, unit="s"),
            Param("bin_rate", "float", "Envelope bins per second for the live "
                                       "view.", default=200.0, unit="Hz"),
        ),
    ),
    Command(
        name="stream-stop", group="stream", handler="stop_stream", mutates=True,
        summary="Stop streaming acquisition.",
    ),
    Command(
        name="stream-status", group="stream", handler="stream_status",
        summary="Streaming health: rate achieved, capture fraction, drops.",
        detail="capture_fraction below 1.0 means the host or the bus could not "
               "keep up. Measured from the first delivered chunk, so start-up "
               "latency is not charged against the run.",
    ),
    Command(
        name="stream-traces", group="stream", handler="stream_traces",
        summary="The live min/max/mean envelope per channel.",
        params=(Param("max_points", "int", "Points per channel.", default=1000),),
    ),
    Command(
        name="measure", group="stream", handler="measure",
        summary="Per-channel min, max, mean and RMS from the latest data.",
    ),
    Command(
        name="stream-plan", group="stream", handler="plan_stream",
        summary="Project bus load and disk cost for a run, before starting it.",
        detail="Reports bytes/s, MB/hour and GB/day for the given settings, and "
               "warns if the aggregate rate exceeds what the USB link can "
               "sustain. Check this before committing to a long run.",
        params=(
            Param("rate", "float", "Raw sample rate per channel.", default=1e6,
                  unit="Hz"),
            Param("downsample", "int", "Downsample ratio.", default=1),
            Param("mode", "enum", "Downsample mode.", choices=RATIO_MODES,
                  default="NONE"),
            Param("duration", "float", "Planned run length. 0 = indefinite.",
                  default=0.0, unit="s"),
        ),
    ),

    # --- recording --------------------------------------------------------
    Command(
        name="record-start", group="record", handler="start_recording",
        mutates=True,
        summary="Start writing the running stream to disk.",
        detail="Refuses to start if the projected run will not fit, and stops "
               "itself if free space runs low. Samples are stored as int16 ADC "
               "counts with the range recorded alongside, so volts reconstruct "
               "exactly at half the file size.",
        params=(
            Param("name", "str", "Name stem for the file.", default="stream"),
            Param("duration", "float", "Stop after this long. 0 = until "
                                       "stopped.", default=0.0, unit="s"),
            Param("format", "enum", "Output format.", choices=("hdf5", "raw"),
                  default="hdf5"),
            Param("compression", "str",
                  "HDF5 compression: none, lzf or gzip. lzf is fast; gzip is "
                  "small but may not keep up at high rates."),
            Param("note", "str", "Free text stored in the file's metadata."),
        ),
    ),
    Command(
        name="record-stop", group="record", handler="stop_recording", mutates=True,
        summary="Stop recording, flush the queue and close the file.",
    ),
    Command(
        name="record-status", group="record", handler="recording_status",
        summary="Recording progress, queue depth, dropped chunks, free disk.",
    ),
    Command(
        name="recordings", group="record", handler="list_recordings",
        summary="List recordings already on disk.",
    ),

    # --- signal generator -------------------------------------------------
    Command(
        name="siggen", group="siggen", handler="set_siggen", mutates=True,
        summary="Drive the built-in signal generator.",
        detail="This unit's limits: 4 Vpp maximum amplitude, 1 MHz maximum "
               "frequency. Useful for a loopback test — feed the generator "
               "output back into an input to exercise the whole chain.",
        params=(
            Param("wave", "enum", "Waveform.", choices=WAVES, default="SINE"),
            Param("frequency", "float", "Output frequency.", default=1000.0,
                  unit="Hz"),
            Param("amplitude", "float", "Peak-to-peak amplitude, max 4.",
                  default=2.0, unit="Vpp"),
            Param("offset", "float", "DC offset.", default=0.0, unit="V"),
            Param("stop_frequency", "float",
                  "Sweep end frequency. Omit for a fixed tone.", unit="Hz"),
            Param("increment", "float", "Sweep step per dwell.", default=0.0,
                  unit="Hz"),
            Param("dwell", "float", "Seconds at each sweep step.", default=1.0,
                  unit="s"),
        ),
    ),
    Command(
        name="siggen-off", group="siggen", handler="siggen_off", mutates=True,
        summary="Silence the signal generator.",
    ),
    Command(
        name="siggen-info", group="siggen", handler="siggen_info",
        summary="Generator limits: amplitude, frequency, AWG buffer size.",
    ),

    # --- presets ----------------------------------------------------------
    Command(
        name="preset-save", group="preset", handler="save_preset", mutates=True,
        summary="Save the current setup so it can be restored exactly.",
        params=(Param("name", "str", "Preset name.", required=True),),
    ),
    Command(
        name="preset-load", group="preset", handler="load_preset", mutates=True,
        summary="Restore a saved setup.",
        params=(Param("name", "str", "Preset name.", required=True),),
    ),
    Command(
        name="presets", group="preset", handler="list_presets",
        summary="List saved presets.",
    ),
)


#: Human-readable group names, for help output and the UI's section headings.
GROUPS = {
    "device": "Device",
    "channel": "Channels",
    "trigger": "Trigger",
    "capture": "Block capture",
    "stream": "Streaming",
    "record": "Recording",
    "siggen": "Signal generator",
    "preset": "Presets",
}


def by_name(name: str) -> Optional[Command]:
    """Look up a command by its CLI/route name."""
    for cmd in COMMANDS:
        if cmd.name == name:
            return cmd
    return None


def describe() -> dict:
    """The whole command tree as JSON — what ``describe`` returns.

    This is the discovery entry point: one call tells a caller everything the
    instrument can do and exactly how to ask for it.
    """
    return {
        "instrument": "PicoScope 4824A",
        "groups": GROUPS,
        "commands": [c.to_dict() for c in COMMANDS],
        "notes": {
            "json": "Every command accepts --json for machine-readable output.",
            "exit_codes": {
                "0": "success",
                "1": "command failed (see .error in the JSON)",
                "2": "bad usage / unknown command",
                "3": "no scope connected",
            },
            "session": (
                "The scope is an exclusive USB device, so it is held open by a "
                "server. Point the CLI at one with --url, or use --local to "
                "open and close the device for a single command."),
        },
    }
