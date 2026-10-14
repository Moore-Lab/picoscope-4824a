# picoscope-4824a

Control for the **PicoScope 4824A** — an 8-channel, 12-bit USB oscilloscope — as used in
the xSphere experiment's fast-control system. A bench-scope view with auto / normal / single
triggering, a **DAQ mode that records
[usphere-DAQ](https://github.com/Moore-Lab/usphere-DAQ) files**, a viewer for those files
(waveforms, ASD, averages), block capture, continuous streaming with a live min/max
envelope, and the built-in signal generator.

It is a submodule of [`xsphere-daq`](https://github.com/Moore-Lab/xsphere-daq), the
experiment's fast-control system, where it appears as one pane of the single-window panel.
It also runs perfectly well on its own.

## The shape of it

The scope is **single-owner**: one process holds the device handle, and a second one cannot
have it. So the design is a server that holds the scope open, and everything else talks to
that server:

```
                 registry.py  ── one declaration per command (42 of them)
                      │
        ┌─────────────┴─────────────┐
      cli.py                     webapp.py
   the `pico` CLI               /api/* + the web page
        └──────── HTTP ────────────┘
```

Both front ends are **generated from the same registry** — neither hand-writes a command —
so the CLI and the web page cannot drift apart. `tests/test_parity.py` enforces that in
both directions: every registry command reaches both surfaces, and neither surface has
anything the registry does not declare.

> There is no packaging yet, so the CLI is invoked as
> `python -m picoscope_4824a.cli`. It calls itself `pico` in its own help output; alias it
> if you use it often (`alias pico='python -m picoscope_4824a.cli'`), and read `pico` as
> that below.

That is why the CLI defaults to `--url http://127.0.0.1:8100` rather than opening the
device: it is a *client* of the running server. Use `--local` to open the device just for
one command — convenient for one-shot queries, but it cannot hold a stream running, because
the process exits.

## Quick start

No hardware and no driver needed — there is a full simulator:

```bash
pip install -r requirements.txt
python -m picoscope_4824a.webapp --port 8100 --sim
```

Open <http://127.0.0.1:8100/>. Or drive it from the command line:

```bash
python -m picoscope_4824a.cli --sim status
python -m picoscope_4824a.cli --sim measure --json
```

`--sim` implies `--local`. The simulator gives each channel a distinct tone — 100 Hz ×
(channel index + 1) at 0.3 × full scale, plus Gaussian noise — synthesised in **ADC counts**,
so the counts → volts path is exercised for real rather than bypassed.

Against a real scope, drop the `--sim`:

```bash
python -m picoscope_4824a.webapp --port 8100     # holds the scope open
python -m picoscope_4824a.cli list               # talks to that server
python -m picoscope_4824a.cli channel-set --channel A --range R_2V
python -m picoscope_4824a.cli stream-start --rate 1000000
```

## Scope mode: triggered sweeps

The web page opens in **Scope** view, which behaves like a bench oscilloscope: it arms,
waits for the trigger, captures the window around it and repeats, so a repetitive signal
stands still. **Run**, **Single** and **Stop** are in the toolbar; the trigger level and
the trigger position are markers on the display that you drag.

| Trigger mode | Behaviour |
|---|---|
| **Auto** (default) | Waits for the trigger, but captures anyway if none arrives within the auto timeout, so the trace never freezes. The timeout is automatic — the longer of 200 ms and one sweep — or set explicitly. |
| **Normal** | Captures only on a trigger and holds the last sweep meanwhile; the corner shows **Wait**. For rare events and slow signals. |
| **Single** | Normal, then stops after one triggered sweep. |
| **Free run** | Trigger off. |

Edges are rising, falling or either; *above* and *below* are level triggers. The timebase is
set per division (100 ns to 1 s on the page, 10 ns to 10 s through the API) across 10
divisions, with the trigger anywhere from the left edge to the right. The corner of the
display shows the scope's state — **Trig'd**, **Auto**, **Wait**, **Stop** — and the legend
and the Measurements panel give each channel's Vpp, mean, RMS and frequency. Frequency is
left blank unless it can be trusted: at least two regular cycles on screen, well clear of
quantisation noise. **Save sweep** writes every sample of the sweep on screen, with time
measured from the trigger.

The same thing from the command line, against a running server:

```bash
python -m picoscope_4824a.cli sweep-start --mode normal --source A --level 0.5 --time-per-div 1e-3
python -m picoscope_4824a.cli sweep-set --level 0.8        # only the level changes; the scope re-arms
python -m picoscope_4824a.cli sweep-trace --json           # latest sweep, state, measurements
python -m picoscope_4824a.cli sweep-save --format csv
python -m picoscope_4824a.cli sweep-stop
```

Scope and **Roll** (streaming) are two modes of one instrument: the 4824A runs one
acquisition at a time, so each refuses to start while the other runs, and switching views on
the page stops the other mode — except that it will not stop a recording. While the scope
runs, any other change to the device (a channel range, the signal generator) is applied
between sweeps; a normal-mode capture can wait indefinitely, and nothing should have to
queue behind it.

**Triggered or auto?** Each sweep reports which it was, decided by *timing*: a capture that
sat armed for the whole auto timeout was auto-triggered. Two measurements on the rig's unit
fix that design. The auto timer starts only after the pre-trigger buffer has filled, so the
armed time is the elapsed time less the collection time. And the trigger has hysteresis (about
5-6 mV on ±5 V), so a level inside the noise can show a crossing at exactly the trigger index
of a capture the hardware never fired on. A test on the waveform would mislabel that; the
clock does not. `tests/test_sweep.py` pins the rule to the numbers the unit produced.

## DAQ mode: the scope as a usphere DAQ

The same thing `usphere-DAQ` does with its NI PXIe-6363, on this scope: choose a record
length as a power of two, a sample rate and a number of acquisitions, and each one lands in
its own HDF5 file. **The files are that project's files** — schema 3,
`beads/data/pos_data`, 32 int16 rows with `Fsamp`, `Time` and the voltage rails as
attributes, channel A as `ai0` — so its viewer and analysis scripts open them without being
told a PicoScope recorded them.

```bash
python -m picoscope_4824a.cli daq-plan  --bits 20 --rate 10000 --files 60   # cost it first
python -m picoscope_4824a.cli daq-start --bits 20 --rate 10000 --files 60 --name run
python -m picoscope_4824a.cli daq-status
python -m picoscope_4824a.cli daq-stop
```

Each run gets its own folder, `run_<timestamp>/`, holding `run_0.h5`, `run_1.h5`, … That is
not decoration: it makes "the averaged ASD of that run" a folder, which is how the usphere
batch analysis treats one. Files are never overwritten — a second run in the same second
gets its own folder, and a named folder that already holds a run of that name is refused.

**No trigger by default**, because the NI card free-runs and most runs want that: the
acquisitions go back to back. `--trigger auto` waits for a trigger but records anyway after
`--auto-ms`; `--trigger normal` waits as long as it takes. Either way the trigger settings
are recorded in the file.

Three things DAQ mode is careful about, because each would quietly spoil a dataset:

* **The rate in the file is the rate used.** A requested rate the timebase cannot give is
  rounded, and `daq-plan` says so before the run; every file records what it actually got,
  so a spectrum is never off by the difference.
* **The configuration cannot change mid-run.** Channel, range, generator and trigger changes
  are refused while a run is in progress, so every file in a run was taken the same way.
* **One voltage range per file** is the schema's limit, so mixed channel ranges are flagged:
  a channel on ±100 mV written against ±5 V rails loses about five bits.

Nothing is live during a run — the thread is inside one long capture — so after each
acquisition the page shows that file's waveform and ASD, and the run's PSD-averaged ASD so
far. There is dead time between acquisitions while the file is written and compressed; the
NI loop has the same gap, and `daq-status` reports it.

## The recordings library

A viewer for those files, in the page's **Files** tab and on the command line: browse the
runs, overlay several files, pick channels, switch between ASD (V/√Hz) and PSD (V²/Hz),
band-pass, and average a whole run into one curve per channel.

```bash
python -m picoscope_4824a.cli files --dir run_20261014_120000
python -m picoscope_4824a.cli file-waveform --paths run_20261014_120000/run_0.h5 --channels A
python -m picoscope_4824a.cli file-spectrum --dir run_20261014_120000 --channels A --average true
```

The maths is `usphere-DAQ`'s, deliberately: Welch with `nperseg = 2^n` and scipy's defaults,
ASD as the square root of the PSD, and **averaging in PSD space with the root taken at the
end** — averaging ASDs instead biases a noise measurement low, because the mean of roots is
below the root of the mean. `picoscope_4824a/spectra.py` is GUI-free so the same functions
serve the page, the CLI and any script.

The viewer in `usphere-DAQ` is a PyQt widget and cannot be a tab in a web page, so the
rendering here is its own; what is shared is the file format and the conventions. In the
other direction it already works as it stands: point `daq_plot.py` at a file this scope
recorded and it opens.

## Commands

42 commands, grouped as the CLI groups them. `pico <command> --help` for any one of
them, `pico describe --json` for the whole tree as machine-readable JSON.

| Group | Commands |
|---|---|
| Device | `list` `open` `close` `info` `status` `ping` `flash` |
| Channels | `channels` `channel-set` `channel-range-for` |
| Trigger | `trigger-set` |
| Block capture | `capture` `timebase` |
| Scope (triggered sweeps) | `sweep-start` `sweep-stop` `sweep-set` `sweep-trace` `sweep-save` |
| DAQ mode | `daq-start` `daq-stop` `daq-status` `daq-plan` `daq-preview` |
| Recordings library | `files` `file-waveform` `file-spectrum` |
| Streaming | `stream-start` `stream-stop` `stream-status` `stream-traces` `measure` `stream-plan` |
| Recording | `record-start` `record-stop` `record-status` `recordings` |
| Signal generator | `siggen` `siggen-off` `siggen-info` |
| Presets | `preset-save` `preset-load` `presets` |

Plus two meta commands: `describe` and `serve`. Every command accepts `--json`.

## Streaming, and why the envelope

A long monitoring run cannot keep every sample, and the obvious ways to reduce it are the
wrong ways: decimating or averaging both **hide short transients**, which on this rig are
often the entire point.

So streaming defaults to the driver's `AGGREGATE` downsampling, which keeps the **minimum
and maximum of every bin**. A spike far shorter than the output period still appears, as a
tall bin in the envelope. The same idea drives the live plot: it paints a min/max band per
display bin rather than a line through samples, so nothing between samples is invisible.

`stream-plan` projects bus load and disk cost *before* a run starts, which is the cheapest
place to find out that a configuration will not fit.

```bash
python -m picoscope_4824a.cli --sim stream-plan --rate 1000000 --json
```

## Recording

`record-start` writes the running stream to HDF5 in `recordings/`. Samples are stored as
**raw ADC counts**, with `range_volts`, `max_adc`, `coupling` and `units` recorded as
dataset attributes, so converting to volts later is exact and needs nothing but the file.
In `AGGREGATE` mode each channel gets both its maxima (`A`) and its minima (`A_min`), so
the envelope survives into the archive rather than being flattened on the way to disk.

`h5py` is imported late, only when a recording actually starts — a missing `h5py` costs you
the recording, never the page.

## In the DAQ panel

`xsphere-daq` mounts this at `/scope`:

```bash
python -m xsphere_daq.panel zelux --scope auto   # first scope found
python -m xsphere_daq.panel sim   --scope sim    # simulated, no hardware
python -m xsphere_daq.panel zelux --scope off    # leave it out
```

The panel owns the lifecycle: it opens the scope on startup and releases it on shutdown.
That teardown matters more than it looks — see `_scope_safe_stop` in the panel, and the
note below.

## Requirements

`numpy` is the only hard dependency; `fastapi`/`uvicorn` serve the page, and `h5py` is
needed only to record. See [`requirements.txt`](requirements.txt).

Real hardware additionally needs **PicoSDK**, which supplies `ps4000a.dll` and is *not* a
pip package — install it (64-bit, matching your interpreter) from
[picotech.com/downloads](https://www.picotech.com/downloads). Its absence raises
`PicoSDKNotFound` at load rather than at import, so everything that does not touch hardware
still runs on a machine without it.

### Always shut down cleanly

Stop the server through its shutdown path, not by killing the process. A process that exits
while `ps4000a.dll` is mid-transfer can leave a thread spinning inside the DLL that survives
`taskkill /F` and holds the device until it is **physically unplugged**. `ScopeController.shutdown`
stops the recorder, stops the stream, then closes the unit, in that order, for this reason.

## Tests

```bash
python -m pytest tests/ -q
```

## Status

Working against both the simulator and the panel: all 42 commands reachable from the CLI
and the web page, streaming validated at 1 MS/s, and recording verified end-to-end
(channel A RMS 0.707 V for a 2 Vpp sine — exactly 1/√2, which pins the whole ADC-counts →
volts → RMS path).

Scope mode is built on trigger behaviour measured on the physical 4824A (through the
existing block-capture commands): where the trigger lands in the buffer, when the auto timer
starts, and how the hysteresis behaves. The sweep loop itself has run against the simulator
and against a stand-in for the driver, but not yet on the unit.

DAQ mode writes files that `usphere-DAQ`'s own `daq_h5` reads and its `daq_plot` viewer
plots — checked by running both against files this scope recorded. It has not yet run on the
physical unit either, so the sustained file-to-file timing over USB 2.0 is still unmeasured.

Development is tracked in [`docs/session-log.md`](docs/session-log.md) — write to the log of
the repo being modified.
