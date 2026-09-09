# picoscope-4824a

Control for the **PicoScope 4824A** — an 8-channel, 12-bit USB oscilloscope — as used in
the xSphere experiment's fast-control system. Channel setup, block capture, continuous
streaming with a live min/max envelope, streaming capture to HDF5, and the built-in signal
generator.

It is a submodule of [`xsphere-daq`](https://github.com/Moore-Lab/xsphere-daq), the
experiment's fast-control system, where it appears as one pane of the single-window panel.
It also runs perfectly well on its own.

## The shape of it

The scope is **single-owner**: one process holds the device handle, and a second one cannot
have it. So the design is a server that holds the scope open, and everything else talks to
that server:

```
                 registry.py  ── one declaration per command (29 of them)
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

## Commands

29 commands, grouped as the CLI groups them. `pico <command> --help` for any one of
them, `pico describe --json` for the whole tree as machine-readable JSON.

| Group | Commands |
|---|---|
| Device | `list` `open` `close` `info` `status` `ping` `flash` |
| Channels | `channels` `channel-set` `channel-range-for` |
| Trigger | `trigger-set` |
| Block capture | `capture` `timebase` |
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

Working against both the simulator and the panel: all 29 commands reachable from the CLI
and the web page, streaming validated at 1 MS/s, and recording verified end-to-end
(channel A RMS 0.707 V for a 2 Vpp sine — exactly 1/√2, which pins the whole ADC-counts →
volts → RMS path). Not yet exercised against the physical 4824A.

Development is tracked in [`docs/session-log.md`](docs/session-log.md) — write to the log of
the repo being modified.
