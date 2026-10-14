# picoscope-4824a — Development Session Log

Running record of development for **this repo only** (the PicoScope 4824A subsystem).
Changes to the panel that mounts it are logged in `xsphere-daq/docs/session-log.md` —
write to the log of the repo you are actually modifying.

Newest entries first. Keep entries short and factual; convert relative dates to absolute.

---

## 2026-10-14 — DAQ mode: the scope as a usphere DAQ, and a viewer for the files

The scope can now be used the way `Moore-Lab/usphere-DAQ` uses its NI PXIe-6363: a record
length as a power of two, a sample rate, a number of acquisitions, a root name — and each
acquisition written as its own HDF5 file. **The files are usphere files**, not merely
similar ones.

**Read their repo first, and matched it rather than inventing.** `daq_h5.py` is the schema
(version 3: `beads/data/pos_data`, 32 int16 rows, `Fsamp` / `Time` / voltage rails as
attributes, `ai0..ai31`); `daq_core.py` is the acquisition loop (`2**n_bits` samples,
`n_files` with 0 meaning until stopped, `{basename}_{index}.h5`); `daq_plot.py` is the
viewer — Welch with `nperseg = 2**bits`, ASD as the root of the PSD, an optional Butterworth
band-pass; and `coriolis_analysis.py` is where "averaged ASD" is defined: the mean of the
**PSDs** across files, rooted at the end.

**The one thing that could not be done as asked.** Their viewer is a PyQt5 + matplotlib
widget, so it cannot be a tab in a web page. What is shared instead is everything
underneath it — the file format and the conventions — and the sharing goes further in the
other direction than a common module would have: `daq_plot.py` opens a file this scope
recorded as it stands. That was checked by running their widget headless against one
(`QT_QPA_PLATFORM=offscreen`): it marked `ai0`/`ai1` as recorded, drew both traces, and put
the ASD peaks at 100 Hz and 200 Hz, which are the simulator's tones.

**What was built**
- `usphere_h5.py` — the schema, mirrored deliberately, GUI-free. Channel A is `ai0`. A
  `PicoScope` module dataset carries the instrument, timebase, trigger and per-channel
  ranges; the usphere reader ignores modules it does not know, so the file stays theirs.
  `write_counts` scales the driver's counts straight into the file's counts, so a 2^22-sample
  acquisition never needs a float copy of every sample. `rails_for` reports the schema's one
  real limitation — one voltage range per file, so mixed channel ranges cost resolution.
- `spectra.py` — Welch, ASD/PSD, PSD-space averaging, band-pass, and reductions that keep
  the minimum and maximum of every bin (log-spaced for a spectrum), so a one-bin line
  survives being drawn a thousand pixels wide.
- `daq.py` — the run: plan, arm, capture, write, repeat. One folder per run, which is what
  makes "average this run" a folder. **Nothing is ever overwritten** — found by a test, not
  by luck: the folder stamp is only good to the second, so two runs started in the same
  second shared a folder and the second wrote over the first. Files are written to `.part`
  and renamed, so a half-written file never appears to a reader.
- `library.py` — browsing and plotting saved files, with PSDs cached by file identity (a
  hundred-file average is a hundred transforms, and the page re-asks on every checkbox) and
  every path confined to the recordings folder, since the panel binds 0.0.0.0.
- `loop.py` — the acquisition thread, extracted from `sweep.py` so sweeps and DAQ runs share
  it: an interruptible capture, device changes applied between captures, and a stop that
  works from either. Duplicating that would have meant duplicating its subtleties.
- Eight commands: `daq-start`, `daq-stop`, `daq-status`, `daq-plan`, `daq-preview`, `files`,
  `file-waveform`, `file-spectrum`. 42 in the registry now.
- Page: **DAQ** and **Files** tabs. DAQ shows the plan before the run (real rate, size,
  duration, warnings), progress during it, and after each acquisition that file's waveform
  and ASD with the run's averaged ASD overlaid. Files browses the runs, overlays several
  files or averages a whole one, picks channels, switches ASD/PSD, log axes and band-pass.
  Both use a new general plotter with logarithmic axes.

**Three things DAQ mode refuses to do quietly**, because each ruins a dataset silently: it
records the rate the timebase actually gave (not the one typed, and `daq-plan` says when
they differ); it refuses channel, range, generator and trigger changes while a run is in
progress, so every file in a run was taken the same way; and it flags mixed channel ranges
rather than writing the narrow one against wide rails.

**Verified.** 126 tests. The arithmetic is checked against physics, not just itself: a
1.5 V tone integrates to 1.0607 V rms in the PSD (1.5/√2), the noise floor lands within 3 %
of σ/√(fs/2), and averaging three files cuts the floor's scatter by √3. `welch_psd` is
asserted bit-identical to the call `daq_plot.py` makes. A mutation pass broke eight things
on purpose — numpy attrs back in the JSON, averaging in ASD space, the path guard, spectral
decimation by first-point, the folder collision, overwriting a named folder, writing the
requested rate instead of the real one, and letting the scope change under a run — and each
was caught. In the browser: a run recorded and drawn, the folder browsed, three files
overlaid and then averaged into one curve.

**Fixed on the way.** h5py returns attributes as numpy scalars, which `json.dumps` refuses,
so listing a folder of our own files 500'd — found by clicking, not by reading. The
`[hidden]` trap that had already bitten this page twice bit a third time (the band-pass
row), so there is now one global rule for it. Checkbox labels stacked their box above the
text. Three files of one channel all drew in the same colour.

**Not yet done:** none of this has run on the physical 4824A. Worth measuring there: the
real file-to-file dead time over USB 2.0 at 2^20 samples, and whether long low-rate blocks
(a 2^20 record at 10 kS/s is 105 s inside one capture) behave as well on the driver as they
do on the simulator.


## 2026-09-16 — Scope mode: auto / normal / single triggered sweeps

The web page can now be used like a bench scope: it arms, waits for its trigger, captures
the window around it and repeats, so a repetitive signal stands still. The streaming view
is still there, as **Roll**.

**Measured on the rig first, then designed.** The live panel was holding the unit, idle, so
its existing `trigger-set` and `capture` commands were used to probe the real driver before
anything was built. The probe only touched inputs, and the trigger was restored exactly as
found. Channel A had no signal on it: just 1.4 mV rms of noise around −2.5 mV, quantised
in ~2.9 mV steps.
- The trigger event lands **exactly** at the pre-trigger index (a rare falling edge fired
  with its crossing at sample 2000 of a 2000-sample pre-trigger).
- The auto timer is honoured to within ~30 ms (300 → 333 ms, 1000 → 1023 ms), and it starts
  **only after the pre-trigger buffer fills**. A 4 s sweep with 2 s of pre-trigger took
  4.64 s at 500 ms auto and 7.63 s at 3.5 s auto: pre + auto + post, not max(pre, auto) +
  post.
- The simple trigger has **hysteresis**, bracketed at about 5.1–6.4 mV on ±5 V. A rising
  level 3σ into the noise never fired, although the buffer held 15 software crossings. A
  level at the noise mean auto-triggered at the timeout with a software crossing at sample
  1999, one sample from the trigger index.

So "was this sweep triggered?" is decided by **timing** (`scope.classify_trigger`): the
armed wait is the elapsed time less the collection time, and the capture auto-triggered iff
that wait reached the timeout. A crossing test on the data would have mislabelled the last
case above. The unit tests pin the rule to these measured numbers.

**What was built**
- `sweep.py` — `SweepSession`, the scope loop on its own thread: `SweepConfig` (mode, source,
  level, direction, time/div over 10 divisions, trigger position %, samples, auto timeout —
  automatic is the longer of 200 ms and one sweep), per-bin min/max `envelope` for transport,
  and `measure` (Vpp, mean, RMS, and a frequency from a Schmitt trigger that is reported only
  for ≥ 2 regular cycles well clear of quantisation noise). A settings change interrupts a
  waiting capture, which re-arms at once. Device changes from other threads go through
  `run_on_loop` and are applied between captures, because a normal-mode capture can wait
  forever and the device object is not thread-safe. The indicator is the bench-scope corner
  status: trig'd / auto / wait / armed / free-run / stop / error. The loop is capped at 100
  sweeps/s.
- `scope.capture_block` — cancellable (`CaptureCancelled`, after `ps4000aStop`), reports
  `trigger_index` / `triggered` / `wait_s`, and has a **wall-clock timeout**. The old loop
  added 1 ms per `Event.wait(0.001)`, which sleeps ~14 ms on Windows (measured), so a "10 s"
  timeout ran for about 140 s. It now polls with `time.sleep`, which honours ~1.5 ms.
- `sim.py` — trigger-aware block capture, following the measured behaviour: arm after the
  pre-trigger fill, find the next crossing of the clean signal (coarse search over one
  period, then bisection), honour the auto timeout, wait indefinitely in normal mode until
  cancelled, and pace to the wall clock. The trigger is ideal, so simulated sweeps stand
  perfectly still.
- Controller: five commands — `sweep-start`, `sweep-stop`, `sweep-set`, `sweep-trace`
  (`since` skips sweeps already seen), `sweep-save` (every sample, time measured from the
  trigger). **One acquisition at a time**: sweeps, streaming and one-shot captures each refuse
  while another runs, and `trigger-set` refuses while the scope owns the trigger. Channel,
  signal-generator, LED and preset changes are routed through the sweep thread. Shutdown and
  close stop the sweeps first. Presets carry the scope settings. Saved sweeps are listed
  with the recordings.
- Registry: the sweep settings have **no defaults** on purpose. The CLI only forwards the
  flags you typed, so `sweep-set --level 0.8` moves the level and nothing else.
- CLI: `--help` crashed on any `%` in registry text (argparse %-formats help strings), and
  the new `position [%]` unit hit it. The renderer now escapes `%`; every subcommand's
  `--help` was checked.
- Page: **Scope | Roll** switch. It follows the instrument: another client starting a stream
  flips the page to Roll. Switching stops the other mode, but never a running recording.
  Run / Single / Stop / Save sweep. Trigger panel: mode, source, edge, level, auto timeout.
  Timebase panel: time/div 100 ns–1 s, samples, position. A 10×8 graticule with centre
  ticks. **Draggable** level flag (right edge) and trigger-position marker (top edge). The
  display window comes from the current settings, so a stopped sweep pans and zooms with the
  controls. The legend and the measurement table show Vpp and frequency.
- Also fixed on the page: channel rows declared four grid columns for five cells, so every
  coupling select wrapped under its checkbox. An error in the header is now held for 5 s
  instead of being overwritten by the 700 ms status line within a poll. `sweep_rate_hz` now
  decays while a normal-mode scope waits, instead of repeating the last rate.

**Verified.** 52 tests (`test_sweep.py`, and `test_capture_block.py`, which drives the real
`PicoScope4824A.capture_block` through a stand-in for `ps4000a.dll`) plus parity. A mutation
pass broke ten things on purpose — cancel ignored, timeout never firing, the classification
rule, configure not waking the loop, `run_on_loop` not waking it, shutdown not stopping it,
single never stopping, the simulator ignoring the trigger, streaming allowed while sweeping,
envelope decimating by stepping — and each was caught. In the browser, standalone and inside
the DAQ panel: Run, a real-mouse drag of the level flag (the trace re-phased to cross the new
level at t = 0), Normal + unreachable level → Wait with the sweep frozen, Auto → untriggered
sweeps at the 200 ms timeout, falling edge, 5 ms/div (100 Hz measured), 10 % position,
Single, re-Run, trigger source switched off → a clear error, then on → triggered on B, and
Scope ↔ Roll, including the refusal while recording. Every CLI example in the README was
run against a server.

**Not yet done:** the sweep loop has not run on the physical unit. The live panel holds it
and runs the previous code, so that needs a panel restart. Worth checking there: the
threshold conversion ignores a channel's analogue offset (as the whole codebase does), and
the real sweep rate over USB 2.0.


## 2026-09-09 (later) — Envelope band fix; README

- **The envelope band was painting flat opaque grey** (`181d370`). `COLORS` held 3-digit
  hex, and `drawPlot` builds the band fill by concatenating an alpha byte: `color + '38'`.
  That gives `'#5cf38'`, a 5-digit hex, which is not a valid CSS colour — and an invalid
  `fillStyle` is a **silent no-op** in Canvas2D, so the band kept whatever the axis labels
  left behind (`'#666'`). Two consequences, both invisible in the data and findable only in
  pixels: the band hid the grid, and with several channels enabled every band was the same
  grey, so later channels completely obscured earlier ones instead of blending. All eight
  channel colours were affected. Expanding `COLORS` to 6-digit makes `color + '38'` a valid
  8-digit hex; the colours are unchanged, since 3-digit hex expands by doubling each digit.
  Both sites now say the digit count is load-bearing, so it does not get "tidied" back.
  Verified by reading the canvas bitmap with two channels streaming: `rgba(87,205,255,56)`
  for A, `rgba(255,223,100,56)` for B, overlap at alpha 100, `#1e1e1e` grid visible through
  it. The same pixels previously read `(102,102,102,255)`.
- **README** (`2d5e11d`) — leads with why the repo is shaped as it is (the scope is
  single-owner, so one process holds the handle and the CLI is a client of it), then the
  simulator, real hardware, the 29 commands by group, why streaming keeps a min/max envelope
  instead of decimating, the HDF5 layout, and the clean-shutdown warning. Every command in
  it was run first; two were wrong on the first pass (`--sim` not `--simulate`, and `pico`
  is only the argparse prog name — there is no packaging yet).

## 2026-09-09 — Subsystem built; simulator; mounted in the panel

**Initial commit: `6c8463b`** (19 files, 5,764 lines), on `main` — the branch was renamed
from `master` to match the rest of the project. Registered as a submodule from `xsphere-daq`
at that commit. Not yet pushed: `github.com/Moore-Lab/picoscope-4824a` does not exist yet,
so until it is created and this is pushed, a clean `clone --recurse-submodules` of the top
level cannot fetch this.

Built this session (preceding context) — `picoscope_4824a/`:

- `ps4000a.py` — ctypes binding to the native `ps4000a.dll`, raising `PicoSDKNotFound` when
  the SDK is absent rather than dying at import.
- `scope.py` / `controller.py` — the device, and `ScopeController`: everything you can do to
  one 4824A, with a registry-driven command surface.
- `registry.py` / `cli.py` / `webapp.py` — **29 commands** declared once in the registry and
  projected into both front ends: the CLI and the generated `/api/*` routes. Neither hand-
  writes a command, so the two cannot drift.
- `streaming.py` — streaming session with a rolling **min/max/mean envelope**: a transient
  shorter than one display bin still shows, because each bin keeps both extremes rather
  than a sample.
- `recorder.py` — stream capture to HDF5 (`h5py`, imported late so it is only needed to
  record).
- `sim.py` — `SimScope`, duck-typing the device: a distinct tone per channel (100 Hz ×
  (index+1)) at 0.3 × full scale plus Gaussian noise, synthesized in ADC counts so the
  counts → volts path is exercised for real. Makes the whole stack testable with no
  hardware and no PicoSDK.
- `tests/test_parity.py` — enforces the CLI/GUI parity guarantee in both directions (8
  checks, passing).

Added this session:

- `requirements.txt` — numpy (hard); fastapi/uvicorn for the page; h5py to record; and a
  note that **PicoSDK is a native install, not a pip package**, so an SDK-less machine still
  runs `--scope sim` and everything that does not touch hardware.

**Validated against the simulator.** Full stack end-to-end including recording: channel A
RMS **0.707 V** for a 2 Vpp sine — exactly 1/√2, which pins the whole volts pipeline (ADC
counts → range scaling → RMS) as numerically correct. AGGREGATE mode wrote both the `A` and
`A_min` datasets with zero drops. Through the panel mount, streaming at 500 kS/s moved
499,296 samples in 1 s, and `/describe` still lists all 29 routes — mounting does not narrow
the command surface.

**Display binning, for reference** (it looks wrong at a glance and is not): the rolling ring
holds `window_s × display_bin_rate_hz` bins — 2000 bins of 5 ms for the 10 s / 200 Hz
default — and `snapshot()` decimates to `max_points` for transport by **re-binning**
(min-of-mins, max-of-maxes), so the envelope survives. The transported array is therefore
shorter than `window_s × bin_rate_hz`, and `bin_rate_hz` in the payload is the *internal*
rate, not `len(min)/window_s`. The front end scales its x-axis by `window_s`, never by
`bin_rate_hz`, so the decimation cannot compress the trace.

**Next.** Create the GitHub repo and push. A README is still missing — every other repo
here has one, with the session log linked from a Status section. The live plot has been
verified as data but not as pixels — see the note in the top-level log.
