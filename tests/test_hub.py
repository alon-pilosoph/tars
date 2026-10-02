"""The hosted training data: clips survive the trip through a shard exactly, and nothing escapes its folder."""

import io
import tarfile

import numpy as np
import pytest
import soundfile as sf

from training.common import Layout
from training.hub import _extract_shard, _write_shard, export, piper_voice


def test_clips_come_back_from_a_shard_sample_for_sample(tmp_path):
    rng = np.random.default_rng(0)
    clips = []
    for i in range(3):
        audio = rng.integers(-20000, 20000, 16000 + i * 800).astype(np.int16)
        clips.append(tmp_path / f"clip_{i}.wav")
        sf.write(clips[-1], audio, 16000, subtype="PCM_16")
    _write_shard(tmp_path / "000.tar", clips)
    _extract_shard(tmp_path / "000.tar", tmp_path / "out")
    for clip in clips:
        before, _ = sf.read(clip, dtype="int16")
        after, sr = sf.read(tmp_path / "out" / clip.name, dtype="int16")
        assert sr == 16000 and np.array_equal(before, after)


def test_a_shard_cannot_write_outside_its_folder(tmp_path):
    buf = io.BytesIO()
    sf.write(buf, np.zeros(1600, np.int16), 16000, format="FLAC")
    with tarfile.open(tmp_path / "bad.tar", "w") as tar:
        for name in ("../escape.flac", "sub/dir.flac", "notes.txt"):
            info = tarfile.TarInfo(name)
            info.size = len(buf.getvalue())
            tar.addfile(info, io.BytesIO(buf.getvalue()))
    _extract_shard(tmp_path / "bad.tar", tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == [] and not (tmp_path / "escape.wav").exists()


def test_piper_clips_are_sorted_by_their_voice():
    from pathlib import Path

    assert piper_voice(Path("en_US-lessac-medium_s000_012.wav")) == "en_US-lessac-medium"
    assert piper_voice(Path("en_GB-northern_english_male-medium_s003_001.wav")) == "en_GB-northern_english_male-medium"


def test_export_refuses_a_data_folder_with_a_voice_it_cant_host(tmp_path):
    # Pitch and tempo variants could be made from it, and they don't say which clip they came from.
    folder = Layout(tmp_path).clip_dir("piper_voices", "hey_tars", "positive")
    folder.mkdir(parents=True)
    for name in ("en_US-lessac-medium_s000_000.wav", "en_GB-alan-medium_s000_000.wav"):
        sf.write(folder / name, np.zeros(1600, np.int16), 16000, subtype="PCM_16")
    with pytest.raises(SystemExit, match="en_GB-alan-medium"):
        export(Layout(tmp_path), tmp_path / "out")
    assert not (tmp_path / "out").exists()
