"""Wake-word model loading and speaker ID. Uses the bundled/downloaded models, so it's skipped if they're absent."""

from pathlib import Path

import numpy as np
import pytest

from voice_assistant.wake import WakeWordTrigger

REPO = Path(__file__).parents[1]
SPEAKER_MODEL = REPO / "models" / "voxceleb_resnet34_LM.onnx"


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
    with pytest.raises(SystemExit, match="not found"):
        WakeWordTrigger("models/does_not_exist.onnx", 0.5)


@pytest.mark.skipif(not SPEAKER_MODEL.exists(), reason="speaker model not downloaded yet")
def test_speaker_id_enrolls_saves_and_rejects_short_clips(tmp_path):
    from voice_assistant.speaker import SpeakerID

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
    assert strict.identify(low.tobytes()) is None  # nobody passes an impossible threshold
