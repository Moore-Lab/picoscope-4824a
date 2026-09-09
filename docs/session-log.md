# picoscope-4824a — Development Session Log

Running record of development for **this repo only** (the PicoScope 4824A subsystem).
Changes to the panel that mounts it are logged in `xsphere-daq/docs/session-log.md` —
write to the log of the repo you are actually modifying.

Newest entries first. Keep entries short and factual; convert relative dates to absolute.

---

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
