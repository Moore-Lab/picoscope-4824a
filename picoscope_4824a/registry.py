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
SWEEP_MODES = ("auto", "normal", "single", "none")
DAQ_TRIGGERS = ("off", "auto", "normal")
SPECTRUM_KINDS = ("asd", "psd")

#: The trigger half of a DAQ run, shared by daq-start and daq-plan. Off by
#: default: the NI DAQ this mode mirrors free-runs, and most runs want that.
DAQ_TRIGGER_PARAMS = (
    Param("trigger", "enum",
          "off: start each acquisition straight away, back to back (what the "
          "usphere NI DAQ does). auto: wait for a trigger but record anyway "
          "after auto_ms. normal: wait for a trigger, however long it takes.",
          choices=DAQ_TRIGGERS, default="off"),
    Param("source", "enum", "Channel to trigger on.", choices=CHANNELS,
          default="A"),
    Param("level", "float", "Trigger threshold.", default=0.0, unit="V"),
    Param("direction", "enum", "Edge or level sense.", choices=DIRECTIONS,
          default="RISING"),
    Param("pretrigger", "float",
          "How much of each record to keep from before the trigger.",
          default=0.0, unit="%"),
    Param("auto_ms", "int", "How long auto mode waits before recording anyway.",
          default=1000, unit="ms"),
)

DAQ_RUN_PARAMS = (
    Param("bits", "int",
          "Record length as a power of two: 2^bits samples per file, the way "
          "usphere-DAQ specifies it. 20 is 1,048,576.", default=20),
    Param("rate", "float",
          "Sample rate per channel. The nearest timebase the scope can give is "
          "used, and that actual rate is what every file records.",
          default=10000.0, unit="Hz"),
    Param("files", "int",
          "How many acquisitions to take. 0 runs until stopped.", default=1),
    Param("name", "str", "Root name for the files: <name>_0.h5, <name>_1.h5, ...",
          default="run"),
    Param("directory", "str",
          "Where to write. Relative paths sit under the recordings folder. "
          "Omit for a new folder per run, which is what makes 'average this "
          "run' a folder."),
) + DAQ_TRIGGER_PARAMS

BAND_PARAMS = (
    Param("f_low", "float", "Band-pass lower corner. Omit for no filter.",
          unit="Hz"),
    Param("f_high", "float", "Band-pass upper corner, below Nyquist.", unit="Hz"),
    Param("order", "int", "Butterworth order.", default=3),
)

#: The scope's front panel, shared by sweep-start and sweep-set. Every one is
#: optional and has NO default here on purpose: an omitted setting stays as it
#: is, so `sweep-set --level 0.5` moves the level and nothing else. (A default
#: would be passed on every call and quietly reset the rest.)
SWEEP_SETTINGS = (
    Param("mode", "enum",
          "auto: capture anyway if no trigger arrives in time, so the trace "
          "never freezes (the usual choice). normal: capture only on a "
          "trigger and hold the last sweep meanwhile. single: stop after one "
          "triggered sweep. none: trigger off, free-run. Unchanged if omitted.",
          choices=SWEEP_MODES),
    Param("source", "enum", "Channel to trigger on. It must be switched on. "
                            "Unchanged if omitted.", choices=CHANNELS),
    Param("level", "float", "Trigger threshold. Unchanged if omitted.", unit="V"),
    Param("direction", "enum",
          "RISING / FALLING / RISING_OR_FALLING fire on an edge through the "
          "level; ABOVE / BELOW fire while the signal is past it. Unchanged if "
          "omitted.", choices=DIRECTIONS),
    Param("time_per_div", "float",
          "Horizontal scale; a sweep spans 10 divisions. 1e-8 to 10. "
          "Unchanged if omitted.", unit="s/div"),
    Param("position", "float",
          "Where the trigger sits across the screen: 0 = left edge, 50 = "
          "centre, 100 = right edge. Unchanged if omitted.", unit="%"),
    Param("samples", "int",
          "Target samples per sweep. At fast timebases the sample rate "
          "ceiling (80 MS/s with 1-4 channels on) allows fewer. Unchanged if "
          "omitted."),
    Param("auto_ms", "int",
          "How long auto mode waits before capturing untriggered. 0 = "
          "automatic: the longer of 200 ms and one sweep. Max 32767 (the "
          "driver's limit). Unchanged if omitted.", unit="ms"),
)


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

    # --- triggered sweeps (scope mode) -------------------------------------
    Command(
        name="sweep-start", group="sweep", handler="start_sweep", mutates=True,
        summary="Run the scope: repeated triggered sweeps (Run / Single).",
        detail="The bench-scope view: arm, wait for the trigger, capture the "
               "window around it, repeat — so a repetitive signal stands "
               "still. Settings given here change first; the rest stand. "
               "Uses block mode, so it cannot run alongside streaming. The "
               "trigger lands exactly on the sweep's time zero.",
        params=SWEEP_SETTINGS,
    ),
    Command(
        name="sweep-stop", group="sweep", handler="stop_sweep", mutates=True,
        summary="Stop the scope. The last sweep stays available.",
    ),
    Command(
        name="sweep-set", group="sweep", handler="set_sweep", mutates=True,
        summary="Change trigger or timebase settings; a running scope re-arms.",
        detail="Only the settings you give change. A capture waiting for a "
               "trigger is released and re-armed with the new ones at once; "
               "a stopped scope keeps them for its next run.",
        params=SWEEP_SETTINGS,
    ),
    Command(
        name="sweep-trace", group="sweep", handler="sweep_trace",
        summary="The latest sweep and the scope's state.",
        detail="Per-channel min/max per bin (so a one-sample glitch still "
               "shows), times relative to the trigger, and measurements: "
               "Vpp, mean, RMS and — when it can be trusted — frequency. "
               "state.indicator is the scope's corner status: trig'd, auto, "
               "wait, armed, free-run, stop or error.",
        params=(
            Param("since", "int",
                  "Only include trace data if a sweep newer than this id "
                  "exists. The state is always included.", default=-1),
            Param("max_points", "int", "Bins per channel.", default=1000),
        ),
    ),
    Command(
        name="sweep-save", group="sweep", handler="save_sweep", mutates=True,
        summary="Save the last sweep, every sample, to the recordings folder.",
        detail="Time is measured from the trigger. HDF5 keeps raw ADC counts "
               "plus the ranges and trigger settings; CSV is volts.",
        params=(
            Param("name", "str", "Name stem for the file.", default="sweep"),
            Param("format", "enum", "Output format.", choices=("hdf5", "csv"),
                  default="hdf5"),
        ),
    ),

    # --- DAQ mode ---------------------------------------------------------
    Command(
        name="daq-start", group="daq", handler="start_daq", mutates=True,
        summary="Record a run: N acquisitions of 2^bits samples, as HDF5 files.",
        detail="The scope used the way usphere-DAQ uses its NI card. Each "
               "acquisition is one file in that project's schema (schema 3, "
               "beads/data/pos_data, channel A as ai0), so the usphere viewer "
               "and analysis scripts read these files without knowing a "
               "PicoScope wrote them. No trigger by default: acquisitions run "
               "back to back, as the NI DAQ does.",
        params=DAQ_RUN_PARAMS + (
            Param("preview", "bool",
                  "Reduce each file to a waveform and a spectrum for the page, "
                  "and keep a running PSD average over the run. Costs a little "
                  "time between acquisitions.", default=True),
        ),
    ),
    Command(
        name="daq-stop", group="daq", handler="stop_daq", mutates=True,
        summary="Stop the run after the acquisition in progress.",
    ),
    Command(
        name="daq-status", group="daq", handler="daq_status",
        summary="Run progress: phase, files written, disk, the current record.",
    ),
    Command(
        name="daq-plan", group="daq", handler="plan_daq",
        summary="Cost a run before starting it: real rate, file size, duration.",
        detail="Reports the rate the timebase actually gives (which is what the "
               "files will record), the size per file and for the run, and "
               "warns about anything that would quietly spoil the data — a "
               "rounded rate, mixed channel ranges, or a disk that will fill.",
        params=DAQ_RUN_PARAMS,
    ),
    Command(
        name="daq-preview", group="daq", handler="daq_preview",
        summary="The last acquisition's waveform and spectrum, with the run state.",
        detail="What the page draws after each file: a min/max envelope of the "
               "record, its ASD, and the PSD-averaged ASD over the run so far. "
               "'since' skips a preview already seen.",
        params=(
            Param("since", "int", "Only return a preview newer than this id.",
                  default=-1),
            Param("max_points", "int", "Points per curve.", default=1200),
            Param("kind", "enum", "Spectrum to return.", choices=SPECTRUM_KINDS,
                  default="asd"),
        ),
    ),

    # --- the recordings library -------------------------------------------
    Command(
        name="files", group="files", handler="list_files",
        summary="Browse the recordings folder: runs, and the files in them.",
        params=(
            Param("dir", "str", "Folder to list, under the recordings folder."),
            Param("limit", "int", "Most files to return.", default=500),
        ),
    ),
    Command(
        name="file-waveform", group="files", handler="file_waveform",
        summary="Time traces from saved files, reduced for plotting.",
        detail="Min and max per bin, so a one-sample glitch survives being "
               "drawn a thousand pixels wide.",
        params=(
            Param("paths", "str", "Files, comma-separated, under the recordings "
                                  "folder.", required=True),
            Param("channels", "str",
                  "Channels, comma-separated: A-H, or ai0-ai31. Omit for every "
                  "channel the file recorded."),
            Param("max_points", "int", "Points per trace.", default=1500),
        ) + BAND_PARAMS,
    ),
    Command(
        name="file-spectrum", group="files", handler="file_spectrum",
        summary="ASD or PSD from saved files, per file or averaged over a run.",
        detail="Welch, with nperseg = 2^nperseg_bits — the same convention as "
               "usphere-DAQ's viewer, so the numbers match. Averaging means "
               "averaging the PSDs across the files and taking the root at the "
               "end; averaging ASDs instead would bias the result low.",
        params=(
            Param("paths", "str", "Files, comma-separated."),
            Param("dir", "str",
                  "Average over every .h5 in this folder — a whole run."),
            Param("channels", "str", "Channels, comma-separated. Omit for all."),
            Param("kind", "enum", "ASD (V/√Hz) or PSD (V²/Hz).",
                  choices=SPECTRUM_KINDS, default="asd"),
            Param("nperseg_bits", "int",
                  "Welch segment length as a power of two. Longer segments "
                  "resolve finer features and average fewer of them.",
                  default=14),
            Param("average", "bool",
                  "Combine the files into one curve per channel.", default=False),
            Param("max_points", "int", "Points per curve.", default=1500),
        ) + BAND_PARAMS,
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
    "sweep": "Scope (triggered sweeps)",
    "daq": "DAQ mode",
    "files": "Recordings library",
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
