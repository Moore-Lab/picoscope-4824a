"""Waveform and spectrum maths, pinned to what usphere-DAQ's viewer computes.

The value of sharing conventions is that a number here and the same number in
`daq_plot.py` are the same number, so these tests call scipy the way that
viewer calls it and demand identical output — not merely similar.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picoscope_4824a import spectra                 # noqa: E402

signal_mod = pytest.importorskip("scipy.signal")

FS = 100_000.0
N = 2 ** 16
AMPLITUDE = 1.5
TONE = 1234.0


@pytest.fixture
def tone():
    t = np.arange(N) / FS
    rng = np.random.default_rng(7)
    return AMPLITUDE * np.sin(2 * np.pi * TONE * t) + 0.01 * rng.normal(size=N)


def test_welch_matches_the_usphere_viewers_call(tone):
    """daq_plot.py: welch(data, fs=sr, nperseg=min(2**bits, len(data)))."""
    f_ours, p_ours, nperseg = spectra.welch_psd(tone, FS, nperseg_bits=14)
    f_theirs, p_theirs = signal_mod.welch(tone, fs=FS, nperseg=min(2 ** 14, len(tone)))
    assert nperseg == 2 ** 14
    assert np.array_equal(f_ours, f_theirs)
    assert np.array_equal(p_ours, p_theirs)


def test_asd_is_the_root_of_the_psd(tone):
    _, pxx, _ = spectra.welch_psd(tone, FS)
    assert np.array_equal(spectra.to_kind(pxx, "asd"), np.sqrt(pxx))
    assert np.array_equal(spectra.to_kind(pxx, "psd"), pxx)
    assert spectra.units("asd") == "V/√Hz" and spectra.units("psd") == "V²/Hz"
    with pytest.raises(ValueError):
        spectra.to_kind(pxx, "amplitude")


def test_the_psd_carries_the_right_power(tone):
    """A sine of amplitude A has A²/2 in its line: the density integrates to it."""
    f, pxx, _ = spectra.welch_psd(tone, FS, nperseg_bits=14)
    band = (f > TONE - 200) & (f < TONE + 200)
    line_rms = np.sqrt(np.trapezoid(pxx[band], f[band]))
    assert line_rms == pytest.approx(AMPLITUDE / np.sqrt(2), rel=0.02)
    # and the noise floor sits at sigma / sqrt(fs/2)
    floor = np.median(np.sqrt(pxx[(f > 20000) & (f < 45000)]))
    assert floor == pytest.approx(0.01 / np.sqrt(FS / 2), rel=0.25)


def test_averaging_happens_in_psd_space():
    """The mean of roots is below the root of the mean, so averaging ASDs
    would bias a noise measurement low. usphere's batch path averages PSDs."""
    a = np.array([1.0, 4.0, 9.0])
    b = np.array([9.0, 4.0, 1.0])
    avg = spectra.average_psd([a, b])
    assert np.array_equal(avg, np.array([5.0, 4.0, 5.0]))
    mean_of_roots = (np.sqrt(a) + np.sqrt(b)) / 2
    assert np.all(np.sqrt(avg) >= mean_of_roots)
    assert np.sqrt(avg)[0] > mean_of_roots[0]


def test_averaging_cuts_the_scatter_not_the_level():
    rng = np.random.default_rng(3)
    psds = [rng.chisquare(2, 400) * 1e-9 for _ in range(16)]
    avg = spectra.average_psd(psds)
    assert avg.mean() == pytest.approx(psds[0].mean(), rel=0.3)
    assert avg.std() < psds[0].std() / 2


@pytest.mark.parametrize("kwargs, message", [
    ({"f_low": 0, "f_high": 100}, "above 0"),
    ({"f_low": 10, "f_high": FS / 2}, "Nyquist"),
    ({"f_low": 500, "f_high": 100}, "below"),
    ({"f_low": 10, "f_high": 100, "order": 99}, "order"),
])
def test_a_bad_band_is_refused_rather_than_quietly_wrong(tone, kwargs, message):
    with pytest.raises(ValueError, match=message):
        spectra.bandpass(tone, FS, **kwargs)


def test_the_band_pass_removes_what_is_outside_it(tone):
    """Measured against the unfiltered signal, not against the noise floor:
    a 1.5 V tone cut by 10^-5 is still above 10 mV of noise."""
    f, raw, _ = spectra.welch_psd(tone, FS, nperseg_bits=14)
    f2, out, _ = spectra.welch_psd(spectra.bandpass(tone, FS, 100.0, 400.0, order=4),
                                   FS, nperseg_bits=14)
    assert np.array_equal(f, f2)
    at_tone = (f > TONE - 100) & (f < TONE + 100)
    in_band = (f > 150) & (f < 350)
    attenuation = out[at_tone].max() / raw[at_tone].max()
    kept = out[in_band].mean() / raw[in_band].mean()
    assert attenuation < 1e-3, f"the tone was only cut to {attenuation:.1e}"
    assert 0.5 < kept < 2.0, f"in-band content changed by {kept:.2f}x"


def test_envelope_keeps_both_extremes():
    v = np.zeros(10_000)
    v[1234] = 5.0
    v[5678] = -3.0
    lo, hi = spectra.envelope(v, 200)
    assert hi.size == lo.size == 200
    assert hi.max() == 5.0 and lo.min() == -3.0
    short = np.arange(10.0)
    lo2, hi2 = spectra.envelope(short, 100)
    assert np.array_equal(lo2, short) and np.array_equal(hi2, short)


def test_log_envelope_drops_dc_and_keeps_the_line(tone):
    f, pxx, _ = spectra.welch_psd(tone, FS, nperseg_bits=14)
    asd = spectra.to_kind(pxx, "asd")
    fr, lo, hi = spectra.log_envelope(f, asd, 300)
    assert fr[0] > 0, "the DC bin has no place on a log axis"
    assert np.all(np.diff(fr) > 0)
    assert fr.size <= 300 and lo.size == hi.size == fr.size
    # the tone must survive the reduction
    peak_f = fr[np.argmax(hi)]
    assert abs(peak_f - TONE) < TONE * 0.05
    assert hi.max() == pytest.approx(asd.max(), rel=1e-9)


def test_summarise_reports_what_a_waveform_panel_shows():
    v = np.array([-1.0, 1.0, -1.0, 1.0])
    out = spectra.summarise(v, 10.0)
    assert out["min"] == -1.0 and out["max"] == 1.0 and out["vpp"] == 2.0
    assert out["mean"] == 0.0 and out["rms"] == 1.0 and out["std"] == 1.0
    assert out["n"] == 4 and out["duration_s"] == pytest.approx(0.4)
    assert spectra.summarise(np.zeros(0), 10.0) == {}


def test_nperseg_bits_outside_the_viewers_range_are_refused(tone):
    with pytest.raises(ValueError):
        spectra.welch_psd(tone, FS, nperseg_bits=2)
    with pytest.raises(ValueError):
        spectra.welch_psd(tone, FS, nperseg_bits=30)


def test_a_short_record_uses_what_it_has(tone):
    """nperseg = min(2**bits, len) — a 1000-sample file still gives a spectrum."""
    f, pxx, nperseg = spectra.welch_psd(tone[:1000], FS, nperseg_bits=14)
    assert nperseg == 1000 and f.size == 501
