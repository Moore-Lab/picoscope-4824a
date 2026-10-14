"""The usphere-DAQ HDF5 schema, so scope files are usphere files.

DAQ mode writes exactly the format `Moore-Lab/usphere-DAQ` writes for the NI
PXIe-6363, so the same analysis — and the DAQ script's own PyQt viewer
(``daq_plot.py``) — opens a file from this scope without knowing or caring that
a PicoScope recorded it. This module is a deliberate mirror of that repo's
``daq_h5.py`` (schema version 3), kept GUI-free and dependency-light so it can
be read from either side.

File structure, as written there::

    beads/data/pos_data    (32, n_samples) int16, gzip-1, ADC counts
      attrs: schema_version, Fsamp, Time, voltage_min, voltage_max
    beads/data/<Module>    (0,) float64 — a module's values live in its attrs

Two things about that layout are worth stating, because they shape what this
module can promise:

**32 rows, always.** The row index *is* the channel: `ai0` is row 0. A scope
with eight inputs fills `ai0`..`ai7` and leaves the rest zero, which is what
makes `recorded_channels` work — a row counts as recorded if anything in it is
non-zero. The zero rows cost almost nothing once gzipped.

**One voltage range per file.** The counts are scaled by a single pair of rails
for the whole file, while this scope sets a range per channel. So the rails are
taken from the widest enabled range, and a channel on a narrower range is
stored against a coarser LSB than its own hardware resolution. For mixed ranges
that is a real loss, so :func:`rails_for` reports it and the DAQ session warns
rather than letting it pass unnoticed. Each channel's own range is also written
into the ``PicoScope`` module, which the usphere reader ignores and this one
uses to reconstruct exactly.
"""

from __future__ import annotations

import time as _time
from pathlib import Path
from typing import Optional

import numpy as np

# --- the parts that must not drift from usphere-DAQ/daq_h5.py -------------

SCHEMA_VERSION: int = 3
ADC_BITS: int = 16
DATASET_PATH: str = "beads/data/pos_data"
GROUP_PATH: str = "beads/data"
N_STREAMS: int = 32
ALL_CHANNELS: list = [f"ai{i}" for i in range(N_STREAMS)]

#: The module dataset this scope adds. Additive, so files stay readable by the
#: usphere reader, which ignores modules it does not know.
PICOSCOPE_MODULE: str = "PicoScope"

#: Scope input -> usphere stream. A is ai0, so an eight-channel file occupies
#: ai0..ai7 and a reader that knows nothing about PicoScopes sees eight
#: recorded channels in the usual places.
CHANNEL_TO_AI: dict = {ch: f"ai{i}" for i, ch in enumerate("ABCDEFGH")}
AI_TO_CHANNEL: dict = {v: k for k, v in CHANNEL_TO_AI.items()}


def _py(value):
    """An HDF5 attribute as a plain Python value.

    h5py hands back numpy scalars and byte strings, and json.dumps refuses
    both: a listing carrying one 404s the whole folder rather than the file.
    """
    if isinstance(value, np.ndarray):
        return [_py(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def volts_to_counts(volts, voltage_min: float, voltage_max: float) -> np.ndarray:
    """Volts -> int16 counts, by usphere-DAQ's formula."""
    lsb = (voltage_max - voltage_min) / (2 ** ADC_BITS)
    return np.round(np.asarray(volts) / lsb).clip(-32768, 32767).astype(np.int16)


def counts_to_volts(counts, voltage_min: float, voltage_max: float) -> np.ndarray:
    """int16 counts -> volts, by usphere-DAQ's formula."""
    lsb = (voltage_max - voltage_min) / (2 ** ADC_BITS)
    return np.asarray(counts).astype(np.float64) * lsb


def rails_for(ranges: dict) -> tuple:
    """``(voltage_min, voltage_max, note)`` for channels on the given ranges.

    The schema carries one range for the file, so the widest wins. *note* is
    None when every channel shares a range, and otherwise says which channels
    lose resolution by how much — a ±100 mV channel written against ±5 V rails
    keeps about 5 bits fewer than the hardware gave it.
    """
    if not ranges:
        return -5.0, 5.0, None
    span = max(ranges.values())
    note = None
    narrower = {ch: v for ch, v in ranges.items() if v < span}
    if narrower:
        worst = min(narrower.values())
        bits = float(np.log2(span / worst))
        note = (
            f"channels are on different ranges: the file is scaled to ±{span:g} V, "
            f"so {', '.join(sorted(narrower))} "
            f"{'is' if len(narrower) == 1 else 'are'} stored with about "
            f"{bits:.1f} bits less resolution than the hardware gave. The schema "
            f"allows only one voltage range per file; put the channels on the "
            f"same range to avoid it.")
    return -span, span, note


# --- writing ---------------------------------------------------------------

def write(filepath, channel_data: dict, n_samples: int, fsamp: float,
          voltage_min: float = -10.0, voltage_max: float = 10.0,
          module_data: Optional[dict] = None,
          compression: Optional[str] = "gzip",
          compression_opts: Optional[int] = 1) -> str:
    """Write one usphere-schema file. *channel_data* maps ``ai*`` names to volts.

    Mirrors ``daq_h5.write``. Returns the path written.
    """
    import h5py                                  # late: only recording needs it

    path = str(filepath)
    pos = np.zeros((N_STREAMS, int(n_samples)), dtype=np.int16)
    for i, ai in enumerate(ALL_CHANNELS):
        if ai in channel_data:
            pos[i] = volts_to_counts(channel_data[ai], voltage_min, voltage_max)

    kwargs = {}
    if compression:
        kwargs["compression"] = compression
        if compression_opts is not None:
            kwargs["compression_opts"] = compression_opts

    with h5py.File(path, "w") as f:
        grp = f.require_group(GROUP_PATH)
        ds = grp.create_dataset("pos_data", data=pos, **kwargs)
        ds.attrs["schema_version"] = SCHEMA_VERSION
        ds.attrs["Fsamp"] = float(fsamp)
        ds.attrs["Time"] = _time.time()
        ds.attrs["voltage_min"] = float(voltage_min)
        ds.attrs["voltage_max"] = float(voltage_max)
        for name, attrs in (module_data or {}).items():
            mod = grp.create_dataset(name, shape=(0,), dtype=np.float64)
            for key, value in attrs.items():
                if value is None:
                    continue
                mod.attrs[key] = value
    return path


def write_counts(filepath, counts_by_ai: dict, n_samples: int, fsamp: float,
                 voltage_min: float, voltage_max: float,
                 module_data: Optional[dict] = None,
                 compression: Optional[str] = "gzip",
                 compression_opts: Optional[int] = 1) -> str:
    """:func:`write`, but taking rows that are already int16 counts.

    The acquisition path has counts from the driver and the scale factor that
    turns them into the file's counts, so it never materialises a float array
    per channel: at 2^22 samples that is the difference between tens of
    megabytes and hundreds.
    """
    import h5py

    path = str(filepath)
    pos = np.zeros((N_STREAMS, int(n_samples)), dtype=np.int16)
    for i, ai in enumerate(ALL_CHANNELS):
        if ai in counts_by_ai:
            row = np.asarray(counts_by_ai[ai], dtype=np.int16)
            pos[i, :row.size] = row

    kwargs = {}
    if compression:
        kwargs["compression"] = compression
        if compression_opts is not None:
            kwargs["compression_opts"] = compression_opts

    with h5py.File(path, "w") as f:
        grp = f.require_group(GROUP_PATH)
        ds = grp.create_dataset("pos_data", data=pos, **kwargs)
        ds.attrs["schema_version"] = SCHEMA_VERSION
        ds.attrs["Fsamp"] = float(fsamp)
        ds.attrs["Time"] = _time.time()
        ds.attrs["voltage_min"] = float(voltage_min)
        ds.attrs["voltage_max"] = float(voltage_max)
        for name, attrs in (module_data or {}).items():
            mod = grp.create_dataset(name, shape=(0,), dtype=np.float64)
            for key, value in attrs.items():
                if value is None:
                    continue
                mod.attrs[key] = value
    return path


def scale_counts(scope_range_volts: float, voltage_min: float,
                 voltage_max: float) -> float:
    """Factor from this scope's counts to the file's counts.

    The scope reports ±32767 over its channel range; the file's counts are
    volts divided by the file LSB. Multiplying by this is the same arithmetic
    as going through volts, without the float array.
    """
    from . import ps4000a as ps
    lsb = (voltage_max - voltage_min) / (2 ** ADC_BITS)
    return (scope_range_volts / ps.MAX_ADC) / lsb


# --- reading ---------------------------------------------------------------

def recorded_channels(filepath) -> dict:
    """``{ai_name: is_recorded}``; a row counts as recorded if it is not all zero."""
    import h5py

    with h5py.File(str(filepath), "r") as f:
        ds = f[DATASET_PATH]
        n_rows = ds.shape[0]
        return {ai: (i < n_rows and bool(np.any(ds[i, :] != 0)))
                for i, ai in enumerate(ALL_CHANNELS)}


def read_channel(filepath, channel: str, start: int = 0,
                 stop: Optional[int] = None) -> tuple:
    """``(volts, fsamp)`` for one ``ai*`` row, optionally a slice of it.

    Handles schema 3 (int16 counts) and the older float64-volts files, exactly
    as the usphere reader does. *start* / *stop* keep a viewer from pulling a
    whole 2^22-sample row in to draw a thousand pixels.
    """
    import h5py

    if channel not in ALL_CHANNELS:
        raise ValueError(f"unknown channel {channel!r}; expected ai0..ai{N_STREAMS - 1}")
    idx = ALL_CHANNELS.index(channel)
    with h5py.File(str(filepath), "r") as f:
        ds = f[DATASET_PATH]
        if idx >= ds.shape[0]:
            raise ValueError(f"{channel} is not in this file (it has {ds.shape[0]} rows)")
        raw = ds[idx, start:stop] if (start or stop is not None) else ds[idx, :]
        fsamp = float(ds.attrs["Fsamp"])
        if raw.dtype == np.int16:
            vmin = float(ds.attrs.get("voltage_min", -10.0))
            vmax = float(ds.attrs.get("voltage_max", 10.0))
            data = counts_to_volts(raw, vmin, vmax)
        else:
            data = raw.astype(np.float64)        # schema 2: already volts
    return data, fsamp


def read_attrs(filepath) -> dict:
    import h5py

    with h5py.File(str(filepath), "r") as f:
        ds = f[DATASET_PATH]
        return {
            "schema_version": int(ds.attrs.get("schema_version", 0)),
            "Fsamp": float(ds.attrs.get("Fsamp", 0.0)),
            "Time": float(ds.attrs.get("Time", 0.0)),
            "voltage_min": float(ds.attrs.get("voltage_min", -10.0)),
            "voltage_max": float(ds.attrs.get("voltage_max", 10.0)),
            "n_samples": int(ds.shape[1]),
            "n_rows": int(ds.shape[0]),
        }


def read_module(filepath, module_name: str) -> dict:
    import h5py

    with h5py.File(str(filepath), "r") as f:
        path = f"{GROUP_PATH}/{module_name}"
        if path not in f:
            return {}
        return {k: _py(v) for k, v in f[path].attrs.items()}


def list_modules(filepath) -> list:
    import h5py

    with h5py.File(str(filepath), "r") as f:
        grp = f.get(GROUP_PATH)
        if grp is None:
            return []
        return [k for k in grp.keys() if k != "pos_data"]


def check_schema(filepath) -> tuple:
    """``(ok, message)`` — whether the file's schema version is the one here."""
    import h5py

    try:
        with h5py.File(str(filepath), "r") as f:
            version = int(f[DATASET_PATH].attrs.get("schema_version", 0))
    except Exception as exc:
        return False, f"could not open file: {exc}"
    if version == SCHEMA_VERSION:
        return True, f"schema version {SCHEMA_VERSION} — OK"
    return False, (f"schema mismatch: file has version {version}, "
                   f"this code expects {SCHEMA_VERSION}")


def describe(filepath) -> dict:
    """Everything a file browser wants about one file, in one open."""
    import h5py

    path = Path(str(filepath))
    with h5py.File(str(path), "r") as f:
        ds = f[DATASET_PATH]
        fsamp = float(ds.attrs.get("Fsamp", 0.0))
        n = int(ds.shape[1])
        rows = int(ds.shape[0])
        recorded = [ALL_CHANNELS[i] for i in range(min(rows, N_STREAMS))
                    if bool(np.any(ds[i, :] != 0))]
        grp = f[GROUP_PATH]
        modules = [k for k in grp.keys() if k != "pos_data"]
        pico = {}
        if PICOSCOPE_MODULE in grp:
            pico = {k: _py(v) for k, v in grp[PICOSCOPE_MODULE].attrs.items()}
        out = {
            "name": path.name,
            "path": str(path),
            "mb": round(path.stat().st_size / 1e6, 3),
            "schema_version": int(ds.attrs.get("schema_version", 0)),
            "fsamp": fsamp,
            "n_samples": n,
            "duration_s": (n / fsamp) if fsamp else 0.0,
            "time": float(ds.attrs.get("Time", 0.0)),
            "voltage_min": float(ds.attrs.get("voltage_min", -10.0)),
            "voltage_max": float(ds.attrs.get("voltage_max", 10.0)),
            "channels": recorded,
            "labels": {ai: AI_TO_CHANNEL.get(ai, ai) for ai in recorded},
            "modules": modules,
        }
        if pico:
            out["picoscope"] = pico
    return out
