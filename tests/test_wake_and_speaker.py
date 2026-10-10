"""Wake-word model loading, speaker ID, and the recording session that feeds both. Tests that need the bundled or
downloaded models are skipped without them."""

from pathlib import Path

import numpy as np
import pytest

from voice_assistant import speaker
from voice_assistant.enroll import PROMPTS
from voice_assistant.speaker import SpeakerID
from voice_assistant.wake import (
    MicroWakeWordTrigger,
    WakeWordTrigger,
    wake_word_trigger,
)

from .conftest import GENERIC, needs_models

SPEAKER_MODEL = Path(__file__).parents[1] / "models" / "voxceleb_resnet34_LM.onnx"
HEY_TARS = GENERIC / "hey_tars.tflite"


def bundled_jarvis() -> Path:
    import openwakeword

    return Path(openwakeword.__file__).parent / "resources" / "models" / "hey_jarvis_v0.1.onnx"


def test_wake_model_by_name_and_by_path_give_the_same_scores():
    by_name = WakeWordTrigger("hey_jarvis", 0.5)
    by_path = WakeWordTrigger(str(bundled_jarvis()), 0.5)
    silence = np.zeros(1280, dtype=np.int16)
    assert by_name.score(silence) == pytest.approx(by_path.score(silence))
    assert by_name.phrase == "hey jarvis"


def test_custom_model_phrase_comes_from_the_filename(tmp_path):
    custom = tmp_path / "hey_tars.onnx"
    custom.write_bytes(bundled_jarvis().read_bytes())
    assert WakeWordTrigger(str(custom), 0.5).phrase == "hey tars"


def test_missing_wake_model_is_a_clear_error():
    with pytest.raises(FileNotFoundError, match="not found"):
        WakeWordTrigger("models/does_not_exist.onnx", 0.5)


@needs_models
def test_tflite_models_use_microwakeword():
    trigger = wake_word_trigger(str(HEY_TARS), 0.95)
    assert isinstance(trigger, MicroWakeWordTrigger)
    assert trigger.phrase == "hey tars"


@needs_models
def test_microwakeword_scores_do_not_depend_on_block_size():
    """The trigger buffers leftover audio between blocks, so any mic block size gives the same stream of scores."""
    rng = np.random.default_rng(0)
    audio = (rng.normal(0, 2000, 16000 * 3)).astype(np.int16)
    peaks = []
    for block in [1280, 480, 1000]:
        trigger = MicroWakeWordTrigger(str(HEY_TARS), 0.95)
        peaks.append(max(trigger.score(audio[i : i + block]) for i in range(0, len(audio), block)))
    assert peaks[0] == pytest.approx(peaks[1]) == pytest.approx(peaks[2])
    assert peaks[0] < 0.95  # noise isn't "hey TARS"


@needs_models
def test_a_reset_microwakeword_model_scores_nothing_on_silence():
    """A fresh model's zeroed streaming state scored about 0.16 at first, which a low threshold took for a wake."""
    trigger = MicroWakeWordTrigger(str(HEY_TARS), 0.2)
    silence = np.zeros(1280, np.int16)
    for _ in range(3):
        trigger.reset()
        assert max(trigger.score(silence) for _ in range(5)) < 0.05


@pytest.mark.skipif(not SPEAKER_MODEL.exists(), reason="speaker model not downloaded yet")
def test_speaker_id_enrolls_saves_and_rejects_short_clips(tmp_path):
    rng = np.random.default_rng(0)
    t = np.arange(16_000 * 3) / 16_000
    # Two synthetic "voices": different fundamentals and noise. Enough to test the plumbing, not accuracy.
    low = (np.sin(2 * np.pi * 110 * t) * 6000 + rng.normal(0, 300, t.size)).astype(np.int16)
    high = (np.sin(2 * np.pi * 260 * t) * 6000 + rng.normal(0, 300, t.size)).astype(np.int16)

    sid = SpeakerID(SPEAKER_MODEL, tmp_path / "vp.npz", threshold=0.0)
    sid.enroll("low", [low])
    sid.enroll("high", [high])
    reloaded = SpeakerID(SPEAKER_MODEL, tmp_path / "vp.npz", threshold=0.0)
    assert set(reloaded.voiceprints) == {"low", "high"}
    assert reloaded.identify(low.tobytes()) == "low"
    assert reloaded.identify(low[:8000].tobytes()) is None  # half a second: too short to judge

    strict = SpeakerID(SPEAKER_MODEL, tmp_path / "vp.npz", threshold=1.01)
    assert strict.identify(low.tobytes()) is None


@pytest.fixture
def no_speaker_model(monkeypatch):
    """SpeakerIDs whose "embedding" is the clip's first samples, so voiceprint files can be tested offline."""
    import onnxruntime

    monkeypatch.setattr(speaker, "fetch", lambda path, url, what: path)
    monkeypatch.setattr(onnxruntime, "InferenceSession", lambda *a, **kw: None)
    monkeypatch.setattr(
        SpeakerID, "embed", lambda self, pcm: (v := np.asarray(pcm[:4], np.float32)) / np.linalg.norm(v)
    )


def speaker_id_without_the_model(path: Path) -> SpeakerID:
    return SpeakerID(Path("no-model.onnx"), path, threshold=0.5)


def clip(*first) -> np.ndarray:
    return np.array([*first] + [0] * 16_000, np.int16)


def test_voiceprints_are_shared_between_processes_whatever_the_names(tmp_path, no_speaker_model):
    web = speaker_id_without_the_model(tmp_path / "vp.npz")
    assistant = speaker_id_without_the_model(tmp_path / "vp.npz")
    web.enroll("file", [clip(1, 0, 0, 0)])  # np.savez can't take "file" as an array name
    web.set_cluster_voiceprints({"allow_pickle": web.voiceprint([clip(0, 1, 0, 0)])})
    assert assistant.identify(clip(1, 0, 0, 0).tobytes()) == "file"  # picked up without a restart
    assert assistant.identify(clip(0, 1, 0, 0).tobytes()) == "allow_pickle"


def test_voiceprints_made_from_named_voices_go_when_the_name_does(tmp_path, no_speaker_model):
    sid = speaker_id_without_the_model(tmp_path / "vp.npz")
    sid.enroll("alon", [clip(1, 0, 0, 0)])  # recorded sentences, --enroll
    sid.set_cluster_voiceprints({"stacey": sid.voiceprint([clip(0, 1, 0, 0)])})
    sid.set_cluster_voiceprints({})  # Stacey's voice was renamed, merged away or marked not a person
    assert set(speaker_id_without_the_model(tmp_path / "vp.npz").voiceprints) == {"alon"}
    sid.set_cluster_voiceprints({"alon": sid.voiceprint([clip(0, 0, 1, 0)])})  # naming a voice Alon replaces it
    sid.set_cluster_voiceprints({})
    assert speaker_id_without_the_model(tmp_path / "vp.npz").voiceprints == {}


def test_an_unreadable_voiceprint_file_keeps_the_ones_already_loaded(tmp_path, capsys, no_speaker_model):
    sid = speaker_id_without_the_model(tmp_path / "vp.npz")
    sid.enroll("alon", [clip(1, 0, 0, 0)])
    (tmp_path / "vp.npz").write_bytes(b"half a file")
    assert sid.identify(clip(1, 0, 0, 0).tobytes()) == "alon"
    assert "keeping the old ones" in capsys.readouterr().out


def test_the_first_voiceprint_format_still_loads(tmp_path, no_speaker_model):
    np.savez(tmp_path / "vp.npz", Alon=np.array([1.0, 0, 0, 0], np.float32))
    assert set(speaker_id_without_the_model(tmp_path / "vp.npz").voiceprints) == {"alon"}


def test_naming_a_voice_replaces_the_voiceprint_enrolled_under_that_name(tmp_path, no_speaker_model):
    sid = speaker_id_without_the_model(tmp_path / "vp.npz")
    sid.enroll("Alon ", [clip(1, 0, 0, 0)])  # --enroll keeps the name as typed
    sid.set_cluster_voiceprints({"alon": sid.voiceprint([clip(0, 1, 0, 0)])})  # keyed by events.person_key
    assert list(speaker_id_without_the_model(tmp_path / "vp.npz").voiceprints) == ["alon"]


def test_recorded_clips_keep_their_numbers():
    """Training splits a person's takes by file number, so a prompt's number must never change."""
    first = [(p.set_name, p.index, p.phrase) for p in PROMPTS[:3]]
    assert first == [("hey_tars", 0, "hey TARS"), ("hey_tars", 1, "hey TARS"), ("hey_tars", 2, "hey TARS")]
    starts = {p.set_name: p.phrase for p in PROMPTS if p.index == 0}
    assert starts == {
        "hey_tars": "hey TARS",
        "tars_stop": "TARS stop",
        "hey_tars_lookalikes": "hey cars",
        "speech": "What's the weather going to be like tomorrow morning?",
    }
    assert [p.phrase for p in PROMPTS if p.set_name == "hey_tars_lookalikes"][:4] == ["hey cars"] * 3 + ["hey bars"]
    assert len(PROMPTS) == 30 + 30 + 25 + 14 * 3
