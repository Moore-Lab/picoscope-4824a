# picoscope-4824a — Development Session Log

Running record of development for **this repo only** (the PicoScope 4824A subsystem).
Changes to the panel that mounts it are logged in `xsphere-daq/docs/session-log.md` —
write to the log of the repo you are actually modifying.

Newest entries first. Keep entries short and factual; convert relative dates to absolute.

---

## 2026-09-09 — Subsystem built; simulator; mounted in the panel

**State of the repo: no commits yet.** Everything below is in the working tree only. An
initial commit here, plus a submodule pointer from `xsphere-daq`, is the next step — until
then none of this survives a clean checkout.

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

**Next.** Initial commit + submodule pointer. The live plot has been verified as data but
not as pixels — see the note in the top-level log.
