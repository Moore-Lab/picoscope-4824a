"""Waveforms and spectra — the maths the usphere viewer does, without the GUI.

`usphere-DAQ`'s viewer is a PyQt widget, so it cannot be a tab in a web page;
what *can* be shared is everything underneath it. These are the same
conventions `daq_plot.py` uses, so a spectrum computed here and one computed
there are the same number:

* **Welch**, ``nperseg = min(2**bits, len(data))``, scipy's defaults — a Hann
  window, 50 % overlap, constant detrend, density scaling, segments averaged.
* **ASD is the square root of the PSD**, V/√Hz against the PSD's V²/Hz.
* **Averaging across files averages the PSDs, then takes the root**
  (`daq_plot`'s batch path does the same). Averaging ASDs instead would bias
  the result low: the mean of square roots is not the square root of the mean.
* **Band-pass** is a Butterworth SOS filter, same argument order and the same
  refusals for a band that does not fit under Nyquist.

Decimation for transport keeps the minimum *and* maximum of every bin, for the
reason the streaming envelope does: a spectral line one bin wide, or a notch,
survives being drawn a thousand pixels wide.
"""

from __future__ import annotations

import math

import numpy as np

#: What `daq_plot.py` opens with, and the range its spinner allows.
DEFAULT_NPERSEG_BITS = 14
NPERSEG_BITS_MIN = 6
NPERSEG_BITS_MAX = 22

KINDS = ("asd", "psd")


def welch_psd(data, fsamp: float, nperseg_bits: int = DEFAULT_NPERSEG_BITS) -> tuple:
    """``(f, Pxx, nperseg)`` — Welch PSD in V²/Hz, as the usphere viewer takes it."""
    from scipy.signal import welch          # late: only spectra need scipy

    v = np.asarray(data, dtype=np.float64)
    if v.size == 0:
        return np.zeros(0), np.zeros(0), 0
    bits = int(nperseg_bits)
    if not NPERSEG_BITS_MIN <= bits <= NPERSEG_BITS_MAX:
        raise ValueError(f"nperseg_bits must be {NPERSEG_BITS_MIN}..{NPERSEG_BITS_MAX}, "
                         f"got {bits}")
    nperseg = min(2 ** bits, v.size)
    f, pxx = welch(v, fs=float(fsamp), nperseg=nperseg)
    return f, pxx, nperseg


def to_kind(pxx, kind: str = "asd"):
    """PSD -> what to plot: the PSD itself, or its root for an ASD."""
    kind = str(kind).lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    return np.sqrt(pxx) if kind == "asd" else pxx


def units(kind: str = "asd") -> str:
    return "V/√Hz" if str(kind).lower() == "asd" else "V²/Hz"


def average_psd(psds) -> np.ndarray:
    """Mean of PSDs across acquisitions.

    In PSD space, never in ASD space: the square root is concave, so averaging
    roots pulls the answer below the truth. The usphere batch analysis averages
    PSDs and roots at the end, and so does this.
    """
    stack = np.vstack([np.asarray(p, dtype=np.float64) for p in psds])
    return stack.mean(axis=0)


def bandpass(data, fsamp: float, f_low: float, f_high: float, order: int = 3):
    """Butterworth band-pass, with the usphere viewer's validation.

    Refuses rather than returning a quietly wrong trace: a corner at or above
    Nyquist, or a band the wrong way round, is a mistake worth seeing.
    """
    from scipy.signal import butter, sosfilt

    v = np.asarray(data, dtype=np.float64)
    nyq = float(fsamp) / 2.0
    f_low, f_high, order = float(f_low), float(f_high), int(order)
    if f_low <= 0:
        raise ValueError(f"band-pass: f_low must be above 0 Hz (got {f_low:g})")
    if f_high >= nyq:
        raise ValueError(f"band-pass: f_high ({f_high:g} Hz) must be below "
                         f"Nyquist ({nyq:g} Hz)")
    if f_low >= f_high:
        raise ValueError(f"band-pass: f_low ({f_low:g}) must be below "
                         f"f_high ({f_high:g})")
    if not 1 <= order <= 10:
        raise ValueError(f"band-pass: order must be 1..10 (got {order})")
    sos = butter(order, [f_low / nyq, f_high / nyq], btype="band", output="sos")
    return sosfilt(sos, v)


# --- reduction for transport ----------------------------------------------

def envelope(values, bins: int) -> tuple:
    """Per-bin (min, max) over equal bins; the array itself if it is short enough."""
    v = np.asarray(values, dtype=np.float64)
    n = v.size
    if n == 0:
        return np.zeros(0), np.zeros(0)
    bins = max(1, int(bins))
    if n <= bins:
        return v, v
    edges = np.linspace(0, n, bins + 1).astype(np.int64)[:-1]
    return np.minimum.reduceat(v, edges), np.maximum.reduceat(v, edges)


def log_envelope(freqs, values, bins: int) -> tuple:
    """``(f, lo, hi)`` reduced into bins spaced evenly in log frequency.

    A spectrum is read on a log axis, where linear bins would spend most of
    their points in the top decade and smear the bottom one. The DC bin is
    dropped: it has no place on a log axis, and the usphere viewer drops it too.
    """
    f = np.asarray(freqs, dtype=np.float64)
    v = np.asarray(values, dtype=np.float64)
    keep = f > 0
    f, v = f[keep], v[keep]
    if f.size == 0:
        return f, v, v
    bins = max(1, int(bins))
    if f.size <= bins:
        return f, v, v
    edges = np.logspace(np.log10(f[0]), np.log10(f[-1]), bins + 1)
    idx = np.searchsorted(f, edges[:-1], side="left")
    # Empty bins would make reduceat read backwards; keep the ones that start
    # somewhere new and let the wide low-frequency bins hold a single point.
    starts, first = [], None
    for i in idx:
        if first is None or i > first:
            starts.append(int(i))
            first = int(i)
    starts = np.array(starts, dtype=np.int64)
    lo = np.minimum.reduceat(v, starts)
    hi = np.maximum.reduceat(v, starts)
    centres = f[starts]
    return centres, lo, hi


def summarise(volts, fsamp: float) -> dict:
    """The numbers worth printing beside a waveform."""
    v = np.asarray(volts, dtype=np.float64)
    if v.size == 0:
        return {}
    mean = float(v.mean())
    return {
        "n": int(v.size),
        "duration_s": float(v.size / fsamp) if fsamp else 0.0,
        "min": float(v.min()),
        "max": float(v.max()),
        "vpp": float(v.max() - v.min()),
        "mean": mean,
        "rms": float(math.sqrt(float(np.mean(v * v)))),
        # What is left once the DC level is removed — the number that matters
        # for a noise measurement, and what the ASD integrates to.
        "std": float(v.std()),
    }
