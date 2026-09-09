"""Streaming recorder — pack samples to disk fast enough for a run that lasts days.

The hard constraint is that the acquisition thread must never block. The
ps4000a driver hands us data out of a finite circular buffer; if we stall while
holding it, the buffer wraps and those samples are gone permanently. So the
recorder splits into two halves with a bounded queue between them, the same
shape as :class:`camera_dock.recorder.HybridRecorder`:

1. **Submit** (hot path, acquisition thread) — copy the chunk onto a bounded
   queue and return. No encoding, no disk, no allocation beyond the copy. If
   the queue is full the chunk is dropped and **counted** — never silently
   lost, and never blocking capture.
2. **Write** (background thread) — pop chunks and append them to disk.

Two decisions worth explaining.

**Why int16 on disk, not volts.** The ADC gives 12-bit counts left-aligned in
an int16. Converting to float32 on the way to disk doubles the file for no
information gained. The range is stored as an attribute instead, so
``volts = counts * range_volts / 32767`` reconstructs exactly. Halving the
byte rate halves the odds of the writer falling behind.

**Why HDF5 by default.** A long run is one growing, chunked, self-describing
dataset per channel: readable by any analysis tool, resumable, and it survives
a crash with everything flushed so far intact. Raw binary is offered for the
highest rates, where even HDF5's per-append bookkeeping is unwelcome.

**Disk guard.** This machine has one drive with tens of gigabytes free, and an
un-downsampled 8-channel run writes 16 MB/s — enough to fill it in under an
hour and take the whole DAQ down with it. The recorder refuses to start
without room for the projected run, and stops itself with a clear reason if
free space falls below :data:`MIN_FREE_BYTES` while running.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter, time as unix_time
from typing import Optional

import numpy as np

from . import ps4000a as ps
from .streaming import StreamChunk, StreamConfig

#: Stop recording when free space drops below this. Leaving a machine with a
#: full system drive is worse than losing the tail of a run.
MIN_FREE_BYTES = 2 * 1024 ** 3          # 2 GiB

#: Default depth of the hand-off queue, in chunks. Deep enough to ride out a
#: slow write (a flush, an antivirus scan), shallow enough to bound RAM.
DEFAULT_QUEUE_CHUNKS = 512


def default_recordings_dir() -> str:
    """``<repo>/recordings`` — beside the camera dock's own recordings tree."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "recordings")


def free_bytes(path: str) -> int:
    """Free space on the filesystem holding *path* (creating it if needed)."""
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe or ".").free
    except OSError:
        return 0


def project_disk_usage(config: StreamConfig, n_channels: int,
                       duration_s: Optional[float] = None) -> dict:
    """What a run will cost on disk, before it starts.

    Mirrors the camera dock's timelapse card, which reports the cadence and the
    resulting frame count up front so the disk can be sized before committing.
    """
    per_sec = config.bytes_per_second(n_channels)
    out = {
        "bytes_per_second": per_sec,
        "mb_per_hour": per_sec * 3600 / 1e6,
        "gb_per_day": per_sec * 86400 / 1e9,
        "samples_per_second_per_channel": config.output_rate_hz,
        "channels": n_channels,
        "aggregated": config.downsample_mode is ps.RatioMode.AGGREGATE,
    }
    if duration_s:
        out["duration_s"] = duration_s
        out["total_bytes"] = per_sec * duration_s
        out["total_gb"] = per_sec * duration_s / 1e9
    return out


@dataclass
class RecorderStats:
    """What actually happened. ``dropped_chunks`` is the number that matters."""
    recording: bool = False
    path: Optional[str] = None
    started_at: float = 0.0
    elapsed_s: float = 0.0
    chunks_written: int = 0
    chunks_dropped: int = 0
    samples_per_channel: int = 0
    bytes_written: int = 0
    queue_depth: int = 0
    queue_peak: int = 0
    stopped_reason: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "recording": self.recording,
            "path": self.path,
            "started_at": self.started_at,
            "elapsed_s": round(self.elapsed_s, 3),
            "chunks_written": self.chunks_written,
            "chunks_dropped": self.chunks_dropped,
            "samples_per_channel": self.samples_per_channel,
            "bytes_written": self.bytes_written,
            "mb_written": round(self.bytes_written / 1e6, 3),
            "queue_depth": self.queue_depth,
            "queue_peak": self.queue_peak,
            "stopped_reason": self.stopped_reason,
            "last_error": self.last_error,
        }


class StreamRecorder:
    """Writes a :class:`~picoscope_4824a.streaming.StreamSession` to disk.

    Attach it as the session's sink::

        rec = StreamRecorder(scope, session)
        rec.start(name="overnight")
        session.set_sink(rec.submit)
        ...
        session.set_sink(None)
        rec.stop()

    :meth:`submit` runs on the acquisition thread and only ever enqueues.
    """

    def __init__(self, scope, session, directory: Optional[str] = None,
                 queue_chunks: int = DEFAULT_QUEUE_CHUNKS,
                 fmt: str = "hdf5", compression: Optional[str] = None) -> None:
        self._scope = scope
        self._session = session
        self.directory = directory or default_recordings_dir()
        self.fmt = fmt
        self.compression = compression
        self._queue: queue.Queue = queue.Queue(maxsize=queue_chunks)
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._lock = threading.RLock()
        self.stats = RecorderStats()
        self._max_seconds = 0.0
        self._t0 = 0.0
        self._writer = None

    # --- lifecycle --------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self.stats.recording

    def start(self, name: Optional[str] = None, max_seconds: float = 0.0,
              note: str = "") -> dict:
        """Open the output file and start the writer thread.

        *max_seconds* of 0 means "until stopped". Raises before creating
        anything if the projected run does not fit on disk.
        """
        with self._lock:
            if self.stats.recording:
                raise RuntimeError("already recording")

            channels = self._session.channels or self._scope.enabled_channels
            if not channels:
                raise RuntimeError("no channels are enabled")
            config = self._session.config

            projection = project_disk_usage(config, len(channels),
                                            max_seconds or None)
            available = free_bytes(self.directory)
            need = projection.get("total_bytes")
            if need and need + MIN_FREE_BYTES > available:
                raise RuntimeError(
                    f"not enough disk: this run needs "
                    f"{need / 1e9:.1f} GB but only {available / 1e9:.1f} GB is free "
                    f"(keeping {MIN_FREE_BYTES / 1e9:.0f} GB in reserve). "
                    f"Lower the rate, raise the downsample ratio, or shorten the run.")
            if available < MIN_FREE_BYTES:
                raise RuntimeError(
                    f"only {available / 1e9:.1f} GB free — refusing to start.")

            os.makedirs(self.directory, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = f"{name or 'stream'}_{stamp}"
            ext = ".h5" if self.fmt == "hdf5" else ".bin"
            path = os.path.join(self.directory, base + ext)

            meta = {
                "instrument": "PicoScope 4824A",
                "serial": self._safe_info("batch_and_serial"),
                "driver": self._safe_info("driver_version"),
                "started_at": unix_time(),
                "started_iso": datetime.now().isoformat(timespec="seconds"),
                "note": note,
                "channels": [c.label for c in channels],
                "ranges": {c.label: self._scope.channels[c].voltage_range.name
                           for c in channels},
                "range_volts": {c.label: self._scope.channels[c].voltage_range.volts
                                for c in channels},
                "coupling": {c.label: self._scope.channels[c].coupling.name
                             for c in channels},
                "max_adc": ps.MAX_ADC,
                "volts_formula": "volts = counts * range_volts / max_adc",
                "stream": config.to_dict(len(channels)),
                "actual_interval_ns": self._session.actual_interval_ns,
                "aggregated": config.downsample_mode is ps.RatioMode.AGGREGATE,
                "projection": projection,
                "format": self.fmt,
            }

            labels = [c.label for c in channels]
            if self.fmt == "hdf5":
                self._writer = _Hdf5Writer(path, labels, meta,
                                           aggregated=meta["aggregated"],
                                           compression=self.compression)
            else:
                self._writer = _RawWriter(path, labels, meta,
                                          aggregated=meta["aggregated"])
            self._writer.open()

            while not self._queue.empty():          # discard anything stale
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

            self._max_seconds = float(max_seconds or 0.0)
            self._t0 = perf_counter()
            self.stats = RecorderStats(recording=True, path=path,
                                       started_at=unix_time())
            self._stopping.clear()
            self._thread = threading.Thread(target=self._write_loop,
                                            name="ps4000a-recorder", daemon=True)
            self._thread.start()

            return {"path": path, "projection": projection,
                    "free_gb": round(available / 1e9, 2), "format": self.fmt,
                    "max_seconds": self._max_seconds or None}

    def stop(self, timeout_s: float = 30.0) -> dict:
        """Drain the queue, close the file, and return the final stats."""
        with self._lock:
            if not self.stats.recording:
                return self.stats.to_dict()
            self._stopping.set()
            thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout_s)
        with self._lock:
            if self._writer is not None:
                try:
                    self.stats.bytes_written = self._writer.close(
                        self.stats.samples_per_channel)
                except Exception as exc:
                    self.stats.last_error = f"close: {type(exc).__name__}: {exc}"
                self._writer = None
            self.stats.recording = False
            if self.stats.stopped_reason is None:
                self.stats.stopped_reason = "stopped"
        return self.stats.to_dict()

    # --- hot path ---------------------------------------------------------

    def submit(self, chunk: StreamChunk, index: int, t_host: float) -> None:
        """Sink callback. Runs on the acquisition thread: enqueue and return.

        A full queue means the writer is not keeping up. We drop the chunk and
        count it rather than blocking — a blocked acquisition thread loses
        samples inside the driver, which is strictly worse and invisible.
        """
        if not self.stats.recording:
            return
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            self.stats.chunks_dropped += 1
            return
        depth = self._queue.qsize()
        self.stats.queue_depth = depth
        if depth > self.stats.queue_peak:
            self.stats.queue_peak = depth

    # --- writer thread ----------------------------------------------------

    def _write_loop(self) -> None:
        checked_at = 0.0
        while True:
            try:
                chunk = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stopping.is_set():
                    break
                continue

            try:
                self._writer.append(chunk)
                self.stats.chunks_written += 1
                self.stats.samples_per_channel += chunk.n_samples
            except Exception as exc:
                self.stats.last_error = f"write: {type(exc).__name__}: {exc}"
                self.stats.stopped_reason = f"write failed: {exc}"
                break

            self.stats.elapsed_s = perf_counter() - self._t0
            self.stats.queue_depth = self._queue.qsize()

            if self._max_seconds and self.stats.elapsed_s >= self._max_seconds:
                self.stats.stopped_reason = "duration reached"
                break

            # Checking free space is a syscall; once a second is plenty.
            if self.stats.elapsed_s - checked_at > 1.0:
                checked_at = self.stats.elapsed_s
                if free_bytes(self.directory) < MIN_FREE_BYTES:
                    self.stats.stopped_reason = (
                        f"stopped: less than {MIN_FREE_BYTES / 1e9:.0f} GB free")
                    break

        # Drain whatever is still queued so a clean stop keeps every sample.
        while True:
            try:
                chunk = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._writer.append(chunk)
                self.stats.chunks_written += 1
                self.stats.samples_per_channel += chunk.n_samples
            except Exception:
                break
        self.stats.recording = False

    # --- helpers ----------------------------------------------------------

    def _safe_info(self, key: str) -> Optional[str]:
        try:
            return self._scope.info().get(key)
        except Exception:
            return None

    def state(self) -> dict:
        stats = self.stats.to_dict()
        stats["free_gb"] = round(free_bytes(self.directory) / 1e9, 2)
        stats["directory"] = self.directory
        return stats


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------

class _Hdf5Writer:
    """One resizable, chunked int16 dataset per channel, plus a metadata group.

    Layout::

        /meta                  (attrs: JSON blob + the individual fields)
        /A                     int16 [n]        counts   (or per-bin maxima)
        /A_min                 int16 [n]        per-bin minima, AGGREGATE only
        /B ...

    Every dataset carries ``range_volts`` and ``max_adc`` attributes, so a
    reader needs nothing but the file to recover volts.
    """

    #: Rows per HDF5 chunk. Big enough that appends are cheap, small enough
    #: that a crash loses little.
    CHUNK_ROWS = 65_536

    def __init__(self, path: str, labels: list[str], meta: dict,
                 aggregated: bool = False, compression: Optional[str] = None) -> None:
        self.path = path
        self.labels = labels
        self.meta = meta
        self.aggregated = aggregated
        self.compression = compression
        self._f = None
        self._sets: dict[str, object] = {}
        self._n = 0

    def open(self) -> None:
        import h5py                                # imported late: optional dep

        self._f = h5py.File(self.path, "w", libver="latest")
        grp = self._f.create_group("meta")
        grp.attrs["json"] = json.dumps(self.meta, default=str)
        for key, value in self.meta.items():
            if isinstance(value, (str, int, float, bool)):
                grp.attrs[key] = value

        kwargs = {"dtype": "i2", "chunks": (self.CHUNK_ROWS,),
                  "maxshape": (None,)}
        if self.compression:
            kwargs["compression"] = self.compression
        for label in self.labels:
            names = [label] + ([f"{label}_min"] if self.aggregated else [])
            for name in names:
                ds = self._f.create_dataset(name, shape=(0,), **kwargs)
                ds.attrs["range_volts"] = self.meta["range_volts"][label]
                ds.attrs["max_adc"] = ps.MAX_ADC
                ds.attrs["coupling"] = self.meta["coupling"][label]
                ds.attrs["units"] = "adc_counts"
                self._sets[name] = ds

    def append(self, chunk: StreamChunk) -> None:
        n = chunk.n_samples
        if not n:
            return
        new_len = self._n + n
        for label in self.labels:
            ds = self._sets[label]
            ds.resize((new_len,))
            ds[self._n:new_len] = chunk.data[label][:n]
            if self.aggregated and label in chunk.data_min:
                ds_min = self._sets[f"{label}_min"]
                ds_min.resize((new_len,))
                ds_min[self._n:new_len] = chunk.data_min[label][:n]
        self._n = new_len

    def close(self, total_samples: int) -> int:
        if self._f is None:
            return 0
        try:
            self._f["meta"].attrs["samples_per_channel"] = self._n
            self._f["meta"].attrs["ended_at"] = unix_time()
            self._f["meta"].attrs["ended_iso"] = datetime.now().isoformat(
                timespec="seconds")
            self._f.flush()
        finally:
            self._f.close()
            self._f = None
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0


class _RawWriter:
    """Channel-interleaved int16 straight to a file, with a JSON sidecar.

    The fastest thing that can be written: one ``ndarray.tofile`` per chunk, no
    per-append bookkeeping. Use it when the writer must keep up with the bus
    and HDF5 will not. Read it back with::

        meta = json.load(open("run.json"))
        n = len(meta["channels"])
        a = np.fromfile("run.bin", dtype="<i2").reshape(-1, n)
    """

    def __init__(self, path: str, labels: list[str], meta: dict,
                 aggregated: bool = False) -> None:
        self.path = path
        self.labels = labels
        self.meta = dict(meta)
        self.aggregated = aggregated
        self._f = None
        self._n = 0

    def open(self) -> None:
        self._f = open(self.path, "wb", buffering=1024 * 1024)
        self.meta["layout"] = (
            "interleaved int16 little-endian, column order: "
            + ", ".join(self._columns()))
        self.meta["columns"] = self._columns()
        with open(os.path.splitext(self.path)[0] + ".json", "w",
                  encoding="utf-8") as fh:
            json.dump(self.meta, fh, indent=2, default=str)

    def _columns(self) -> list[str]:
        cols: list[str] = []
        for label in self.labels:
            cols.append(label)
            if self.aggregated:
                cols.append(f"{label}_min")
        return cols

    def append(self, chunk: StreamChunk) -> None:
        n = chunk.n_samples
        if not n:
            return
        parts = []
        for label in self.labels:
            parts.append(chunk.data[label][:n])
            if self.aggregated and label in chunk.data_min:
                parts.append(chunk.data_min[label][:n])
        block = np.stack(parts, axis=1).astype("<i2", copy=False)
        block.tofile(self._f)
        self._n += n

    def close(self, total_samples: int) -> int:
        if self._f is not None:
            self._f.flush()
            self._f.close()
            self._f = None
        self.meta["samples_per_channel"] = self._n
        self.meta["ended_at"] = unix_time()
        with open(os.path.splitext(self.path)[0] + ".json", "w",
                  encoding="utf-8") as fh:
            json.dump(self.meta, fh, indent=2, default=str)
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0
