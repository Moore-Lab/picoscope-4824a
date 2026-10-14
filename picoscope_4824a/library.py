"""The recordings library — browsing and plotting saved usphere-schema files.

What the viewer tab is made of, and what a script gets from the CLI: list the
runs on disk, read a waveform at the resolution a screen can show, and compute
spectra — one file at a time, or averaged over a whole run.

Averaging is the point of a run folder. DAQ mode writes one directory per run,
so "the averaged ASD of that run" is a directory, exactly as the usphere batch
analysis treats it: average the PSDs of every file, then take the root.

Two things this module is careful about.

**Paths stay inside the recordings root.** The panel binds 0.0.0.0, so a path
that came in over HTTP is not to be trusted with `open()`. :func:`resolve`
refuses anything that resolves outside the root, and every entry point goes
through it.

**Spectra are cached by file identity.** Averaging a hundred-file run means a
hundred Welch transforms, and the page re-asks whenever a checkbox moves. The
cache is keyed by path, size, mtime and the analysis settings, so an edited or
re-written file is never served from it.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Optional

import numpy as np

from . import spectra, usphere_h5

#: How many PSDs to keep. Each is a few tens of kB, so this is a few MB.
_CACHE_MAX = 512
_psd_cache: "OrderedDict[tuple, tuple]" = OrderedDict()

#: Series returned at once when not averaging, so a stray "select all" cannot
#: turn into a hundred-curve payload.
MAX_SERIES = 12


def resolve(root: str, path: str) -> str:
    """Absolute path for *path*, refusing anything outside *root*."""
    base = os.path.abspath(root)
    candidate = path if os.path.isabs(path) else os.path.join(base, path)
    full = os.path.abspath(candidate)
    if full != base and not full.startswith(base + os.sep):
        raise ValueError(f"{path!r} is outside the recordings folder")
    return full


def split_list(value) -> list:
    """A list parameter as it arrives: a real list, or a comma-separated string."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def list_dir(root: str, subdir: str = "", limit: int = 500) -> dict:
    """Folders and usphere-schema files in one directory of the library."""
    base = resolve(root, subdir or "")
    if not os.path.isdir(base):
        raise ValueError(f"{subdir or '.'} is not a folder")
    folders, files, skipped = [], [], []
    with os.scandir(base) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            if entry.is_dir():
                n_h5 = 0
                try:
                    with os.scandir(entry.path) as inner:
                        n_h5 = sum(1 for e in inner if e.name.lower().endswith(".h5"))
                except OSError:
                    pass
                folders.append({"name": entry.name,
                                "path": os.path.relpath(entry.path, root),
                                "files": n_h5})
            elif entry.name.lower().endswith((".h5", ".hdf5")):
                if len(files) >= limit:
                    continue
                try:
                    info = usphere_h5.describe(entry.path)
                    info["path"] = os.path.relpath(entry.path, root)
                    files.append(info)
                except Exception as exc:
                    # A scope block capture saved from the Scope tab is a
                    # different layout; say so rather than hiding the file.
                    skipped.append({"name": entry.name,
                                    "path": os.path.relpath(entry.path, root),
                                    "reason": f"{type(exc).__name__}: {exc}"})
    return {"root": os.path.abspath(root), "dir": subdir or "",
            "parent": (os.path.relpath(os.path.dirname(base), root)
                       if os.path.abspath(base) != os.path.abspath(root) else None),
            "folders": folders, "files": files, "unreadable": skipped}


def h5_files_in(root: str, subdir: str) -> list:
    """Every .h5 in a folder, sorted the way a run is numbered."""
    base = resolve(root, subdir or "")
    if not os.path.isdir(base):
        raise ValueError(f"{subdir or '.'} is not a folder")
    names = [n for n in os.listdir(base) if n.lower().endswith((".h5", ".hdf5"))]

    def key(name: str):
        stem = os.path.splitext(name)[0]
        tail = stem.rsplit("_", 1)[-1]
        return (0, int(tail)) if tail.isdigit() else (1, name)

    return [os.path.join(base, n) for n in sorted(names, key=key)]


def _channels_for(path: str, wanted: list) -> list:
    """Requested channels as ``ai*`` names, defaulting to whatever was recorded."""
    recorded = [ai for ai, yes in usphere_h5.recorded_channels(path).items() if yes]
    if not wanted:
        return recorded
    out = []
    for name in wanted:
        token = str(name).strip()
        ai = usphere_h5.CHANNEL_TO_AI.get(token.upper(), token)
        if ai in recorded or ai in usphere_h5.ALL_CHANNELS:
            out.append(ai)
    return out


def _label(path: str, ai: str, with_file: bool) -> str:
    scope_label = usphere_h5.AI_TO_CHANNEL.get(ai, ai)
    name = os.path.splitext(os.path.basename(path))[0]
    return f"{name} · {scope_label}" if with_file else scope_label


def waveform(root: str, paths, channels=None, max_points: int = 1500,
             band: Optional[dict] = None) -> dict:
    """Time traces, reduced to *max_points* bins each, min and max per bin."""
    files = [resolve(root, p) for p in split_list(paths)]
    if not files:
        raise ValueError("give at least one file")
    wanted = split_list(channels)
    series, notes = [], []
    many = len(files) > 1
    for path in files:
        for ai in _channels_for(path, wanted):
            if len(series) >= MAX_SERIES:
                notes.append(f"showing the first {MAX_SERIES} traces")
                break
            data, fsamp = usphere_h5.read_channel(path, ai)
            if band:
                data = spectra.bandpass(data, fsamp, band["f_low"], band["f_high"],
                                        band.get("order", 3))
            lo, hi = spectra.envelope(data, max_points)
            series.append({
                "label": _label(path, ai, many),
                "file": os.path.basename(path),
                "channel": usphere_h5.AI_TO_CHANNEL.get(ai, ai),
                "ai": ai,
                "n_samples": int(data.size),
                "fsamp": fsamp,
                "duration_s": float(data.size / fsamp) if fsamp else 0.0,
                "min": np.round(lo, 7).tolist(),
                "max": np.round(hi, 7).tolist(),
                "stats": spectra.summarise(data, fsamp),
            })
    return {"series": series, "notes": notes, "files": len(files)}


def _psd_for(path: str, ai: str, bits: int, band: Optional[dict]) -> tuple:
    """``(f, Pxx)`` for one channel of one file, cached by file identity."""
    try:
        stat = os.stat(path)
        key = (path, stat.st_size, int(stat.st_mtime_ns), ai, int(bits),
               None if not band else (band["f_low"], band["f_high"], band.get("order", 3)))
    except OSError:
        key = None
    if key is not None and key in _psd_cache:
        _psd_cache.move_to_end(key)
        return _psd_cache[key]

    data, fsamp = usphere_h5.read_channel(path, ai)
    if band:
        data = spectra.bandpass(data, fsamp, band["f_low"], band["f_high"],
                                band.get("order", 3))
    f, pxx, nperseg = spectra.welch_psd(data, fsamp, nperseg_bits=bits)
    value = (f, pxx, nperseg, fsamp)
    if key is not None:
        _psd_cache[key] = value
        while len(_psd_cache) > _CACHE_MAX:
            _psd_cache.popitem(last=False)
    return value


def spectrum(root: str, paths=None, directory: Optional[str] = None,
             channels=None, kind: str = "asd",
             nperseg_bits: int = spectra.DEFAULT_NPERSEG_BITS,
             average: bool = False, max_points: int = 1500,
             band: Optional[dict] = None) -> dict:
    """ASD or PSD for files, or one averaged curve per channel over a run.

    With *average*, PSDs are averaged across the files and the root is taken at
    the end — the way the usphere batch analysis does it, and the only way that
    does not bias the answer low.
    """
    files = [resolve(root, p) for p in split_list(paths)]
    if directory:
        files = h5_files_in(root, directory)
    if not files:
        raise ValueError("give files, or a folder to average over")
    wanted = split_list(channels)
    series, notes = [], []
    many = len(files) > 1
    nperseg_used, fsamp_used = 0, 0.0

    if average:
        sums: dict = {}
        counts: dict = {}
        freqs: dict = {}
        for path in files:
            for ai in _channels_for(path, wanted):
                f, pxx, nperseg, fsamp = _psd_for(path, ai, nperseg_bits, band)
                nperseg_used, fsamp_used = nperseg, fsamp
                if ai in sums and sums[ai].shape != pxx.shape:
                    notes.append(f"{os.path.basename(path)} has a different record "
                                 f"length or rate and was left out of the average")
                    continue
                sums[ai] = pxx if ai not in sums else sums[ai] + pxx
                counts[ai] = counts.get(ai, 0) + 1
                freqs[ai] = f
        for ai, total in sums.items():
            f, lo, hi = spectra.log_envelope(freqs[ai],
                                             spectra.to_kind(total / counts[ai], kind),
                                             max_points)
            series.append({
                "label": f"{usphere_h5.AI_TO_CHANNEL.get(ai, ai)} · mean of {counts[ai]}",
                "channel": usphere_h5.AI_TO_CHANNEL.get(ai, ai), "ai": ai,
                "averaged": counts[ai],
                "f": np.round(f, 4).tolist(),
                "min": lo.tolist(), "max": hi.tolist(),
            })
    else:
        for path in files:
            for ai in _channels_for(path, wanted):
                if len(series) >= MAX_SERIES:
                    notes.append(f"showing the first {MAX_SERIES} curves — tick "
                                 f"Average to combine a whole run instead")
                    break
                f, pxx, nperseg, fsamp = _psd_for(path, ai, nperseg_bits, band)
                nperseg_used, fsamp_used = nperseg, fsamp
                fr, lo, hi = spectra.log_envelope(f, spectra.to_kind(pxx, kind),
                                                  max_points)
                series.append({
                    "label": _label(path, ai, many),
                    "file": os.path.basename(path),
                    "channel": usphere_h5.AI_TO_CHANNEL.get(ai, ai), "ai": ai,
                    "averaged": 1,
                    "f": np.round(fr, 4).tolist(),
                    "min": lo.tolist(), "max": hi.tolist(),
                })
    return {
        "series": series, "notes": notes, "files": len(files),
        "kind": kind, "units": spectra.units(kind),
        "nperseg": nperseg_used, "nperseg_bits": int(nperseg_bits),
        "resolution_hz": (fsamp_used / nperseg_used) if nperseg_used else 0.0,
        "averaged": bool(average),
    }
