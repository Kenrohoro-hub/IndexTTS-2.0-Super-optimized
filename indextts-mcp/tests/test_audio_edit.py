from io import BytesIO

import numpy as np
import pytest
import soundfile as sf

from indextts.utils.audio_edit import edit_wav


def make_wav(samples, sample_rate=8000):
    stream = BytesIO()
    sf.write(stream, samples, sample_rate, subtype="PCM_16", format="WAV")
    return stream.getvalue()


def read_wav(data):
    return sf.read(BytesIO(data), dtype="float32", always_2d=True)


def test_noop_and_trim_preserve_channels():
    source = np.column_stack((np.full(8000, 0.2), np.full(8000, -0.2))).astype("float32")
    wav, meta = edit_wav(make_wav(source))
    result, sr = read_wav(wav)
    assert result.shape == source.shape
    assert sr == 8000 and meta["channels"] == 2
    assert meta["duration_seconds"] == 1.0 and meta["size_bytes"] == len(wav)
    trimmed, trim_meta = edit_wav(make_wav(source), 0.25, 0.75)
    assert read_wav(trimmed)[0].shape == (4000, 2)
    assert trim_meta["duration_seconds"] == 0.5


def test_rate_changes_duration_and_downmixes_to_mono():
    source = np.column_stack((np.sin(np.arange(8000) / 10), np.cos(np.arange(8000) / 10))).astype("float32") * 0.2
    wav, meta = edit_wav(make_wav(source), rate=2.0)
    result, sr = read_wav(wav)
    assert result.shape == (4000, 1)
    assert sr == 8000 and meta["duration_seconds"] == 0.5
    assert meta["channels"] == 1


def test_gain_clips_and_reports_safe_gain():
    wav, meta = edit_wav(make_wav(np.full(800, 0.8, dtype="float32")), gain_db=6)
    result, _ = read_wav(wav)
    assert meta["clipped"] is True
    assert meta["safe_gain_db"] == 1.5
    assert meta["warnings"]
    assert result.max() >= 0.9999


@pytest.mark.parametrize("kwargs", [
    {"start_seconds": -0.1},
    {"start_seconds": 0.5, "end_seconds": 0.5},
    {"start_seconds": 0.0, "end_seconds": 2.0},
    {"rate": 0.49},
    {"gain_db": 24.1},
    {"rate": float("nan")},
    {"gain_db": float("inf")},
])
def test_invalid_ranges_are_rejected(kwargs):
    wav = make_wav(np.zeros(800, dtype="float32"))
    with pytest.raises(ValueError):
        edit_wav(wav, **kwargs)


def test_invalid_and_empty_audio_are_rejected():
    with pytest.raises(ValueError):
        edit_wav(b"not a wav")
    with pytest.raises(ValueError):
        edit_wav(make_wav(np.empty(0, dtype="float32")))


def test_non_wav_container_is_rejected():
    stream = BytesIO()
    sf.write(stream, np.zeros(800, dtype="float32"), 8000, format="FLAC")
    with pytest.raises(ValueError, match="仅支持 WAV"):
        edit_wav(stream.getvalue())


def test_too_short_to_stretch_reports_user_visible_error():
    with pytest.raises(ValueError, match="过短"):
        edit_wav(make_wav(np.array([0.2], dtype="float32")), rate=2.0)
