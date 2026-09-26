"""The end-of-turn model's ears: the same log-mel features Whisper computes, and the model itself when downloaded."""

from pathlib import Path

import numpy as np
import pytest

from voice_assistant.turn import SmartTurn, features

MODEL = Path(__file__).parents[1] / "models/smart-turn-v3.2-cpu.onnx"


def tone_in_noise() -> np.ndarray:
    rng = np.random.default_rng(7)
    t = np.arange(16000 * 2) / 16000
    return ((0.3 * np.sin(2 * np.pi * 440 * t) + 0.05 * rng.standard_normal(len(t))) * 32767).astype(np.int16)


def test_the_features_match_whispers_own_extractor():
    # From transformers' WhisperFeatureExtractor(chunk_length=8) on the same audio, zero-padded in front to 8 s.
    f = features(tone_in_noise())
    assert f.shape == (1, 80, 800)
    expected = [0.11192, 0.51067, 1.71893, 0.8892, 0.92799, -0.18103]
    got = [f.mean(), f.std(), f[0, 10, 700], f[0, 40, 750], f[0, 79, 790], f[0, 5, 10]]
    assert got == pytest.approx(expected, abs=1e-3)


@pytest.mark.skipif(not MODEL.exists(), reason="end-of-turn model not downloaded yet")
def test_the_model_gives_a_probability():
    p = SmartTurn(MODEL).finished(tone_in_noise().tobytes())
    assert 0.0 <= p <= 1.0
