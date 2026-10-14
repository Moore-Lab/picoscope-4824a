"""The usphere-DAQ file schema, which is the whole point of DAQ mode.

These files are meant to be indistinguishable from the ones the NI card
writes, so that the usphere viewer and analysis scripts read them without
knowing better. That is a promise about bytes on disk, so it is checked
against the schema as documented — dataset path, shape, dtype, attribute
names — rather than against our own reader, which would agree with whatever
we wrote.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picoscope_4824a import usphere_h5 as U          # noqa: E402

h5py = pytest.importorskip("h5py")

FS = 50_000.0
N = 4096


@pytest.fixture
def written(tmp_path):
    t = np.arange(N) / FS
    data = {
        "ai0": 1.5 * np.sin(2 * np.pi * 1000.0 * t),
        "ai3": 0.25 * np.sin(2 * np.pi * 250.0 * t),
    }
    path = str(tmp_path / "run_0.h5")
    U.write(path, data, n_samples=N, fsamp=FS, voltage_min=-5.0, voltage_max=5.0,
            module_data={U.PICOSCOPE_MODULE: {"instrument": "PicoScope 4824A",
                                              "file_index": 0,
                                              "channel_ranges_volts": [5.0, 5.0]}})
    return path, data


def test_the_file_is_laid_out_as_the_schema_documents(written):
    path, _ = written
    with h5py.File(path, "r") as f:
        ds = f["beads/data/pos_data"]
        assert ds.shape == (32, N), "the row index is the channel; all 32 are present"
        assert ds.dtype == np.int16
        assert int(ds.attrs["schema_version"]) == 3
        assert float(ds.attrs["Fsamp"]) == FS
        assert float(ds.attrs["voltage_min"]) == -5.0
        assert float(ds.attrs["voltage_max"]) == 5.0
        assert float(ds.attrs["Time"]) > 1.6e9
        assert U.PICOSCOPE_MODULE in f["beads/data"]
        # Modules carry their values in attributes on an empty dataset.
        assert f[f"beads/data/{U.PICOSCOPE_MODULE}"].shape == (0,)


def test_rows_are_channels_and_unrecorded_rows_are_zero(written):
    path, data = written
    recorded = U.recorded_channels(path)
    assert [ai for ai, yes in recorded.items() if yes] == ["ai0", "ai3"]
    assert recorded["ai1"] is False


def test_volts_survive_the_round_trip(written):
    path, data = written
    lsb = 10.0 / 2 ** 16
    for ai, original in data.items():
        back, fsamp = U.read_channel(path, ai)
        assert fsamp == FS
        assert np.max(np.abs(back - original)) <= lsb


def test_counts_and_volts_paths_agree(tmp_path):
    """write_counts exists to avoid a float copy of every sample; it must put
    the same numbers on disk as going through volts would."""
    volts = np.linspace(-4.0, 4.0, 1000)
    by_volts = str(tmp_path / "v.h5")
    U.write(by_volts, {"ai0": volts}, n_samples=1000, fsamp=FS,
            voltage_min=-5.0, voltage_max=5.0)
    counts = U.volts_to_counts(volts, -5.0, 5.0)
    by_counts = str(tmp_path / "c.h5")
    U.write_counts(by_counts, {"ai0": counts}, n_samples=1000, fsamp=FS,
                   voltage_min=-5.0, voltage_max=5.0)
    assert np.array_equal(U.read_channel(by_volts, "ai0")[0],
                          U.read_channel(by_counts, "ai0")[0])


def test_scope_counts_scale_to_file_counts():
    """The scope reports ±32767 over its range; the file counts volts/LSB."""
    from picoscope_4824a import ps4000a as ps
    factor = U.scale_counts(5.0, -5.0, 5.0)
    scope_counts = np.array([ps.MAX_ADC, -ps.MAX_ADC, 0, 1000], dtype=np.int16)
    volts = scope_counts * (5.0 / ps.MAX_ADC)
    assert np.allclose(np.rint(scope_counts * factor),
                       U.volts_to_counts(volts, -5.0, 5.0), atol=1)


def test_describe_is_json_safe(written):
    """h5py returns numpy scalars; json.dumps refuses them, and a listing that
    carries one fails the whole folder rather than the one file."""
    path, _ = written
    info = U.describe(path)
    json.dumps(info)                               # must not raise
    assert info["n_samples"] == N
    assert info["fsamp"] == FS
    assert info["channels"] == ["ai0", "ai3"]
    assert info["labels"] == {"ai0": "A", "ai3": "D"}
    assert info["picoscope"]["file_index"] == 0
    assert isinstance(info["picoscope"]["file_index"], int)
    assert info["picoscope"]["channel_ranges_volts"] == [5.0, 5.0]
    json.dumps(U.read_module(path, U.PICOSCOPE_MODULE))


def test_the_channel_map_puts_A_first():
    assert U.CHANNEL_TO_AI["A"] == "ai0"
    assert U.CHANNEL_TO_AI["H"] == "ai7"
    assert U.AI_TO_CHANNEL["ai0"] == "A"
    assert len(U.ALL_CHANNELS) == 32


def test_schema_check_and_slicing(written):
    path, data = written
    ok, msg = U.check_schema(path)
    assert ok and "3" in msg
    part, _ = U.read_channel(path, "ai0", start=10, stop=20)
    assert part.size == 10
    whole, _ = U.read_channel(path, "ai0")
    assert np.array_equal(part, whole[10:20])


def test_an_unknown_channel_is_refused(written):
    path, _ = written
    with pytest.raises(ValueError):
        U.read_channel(path, "ai99")


@pytest.mark.parametrize("ranges, flagged", [
    ({"A": 5.0, "B": 5.0}, False),
    ({"A": 5.0}, False),
    ({"A": 5.0, "B": 0.1}, True),
])
def test_mixed_ranges_are_reported_not_hidden(ranges, flagged):
    """One voltage range per file is the schema's limit, and a narrow channel
    stored against wide rails quietly loses bits. Say so."""
    vmin, vmax, note = U.rails_for(ranges)
    assert (vmax, vmin) == (max(ranges.values()), -max(ranges.values()))
    assert (note is not None) is flagged
    if flagged:
        assert "B" in note and "resolution" in note
