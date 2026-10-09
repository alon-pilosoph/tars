"""The wake double-check: word matching, the audio window, near-misses, and the trigger that ignores rejected wakes."""

import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from voice_assistant import verify
from voice_assistant.audio import BLOCK_SAMPLES
from voice_assistant.verify import (
    ANSWER,
    ASK,
    IGNORE,
    MODEL_NAME,
    NEAR_QUIET_S,
    NearMisses,
    PhraseVerifier,
    RecentAudio,
    TunedCheck,
    VerifiedTrigger,
    ensure_model,
)
from voice_assistant.versions import INSTALLED, FixedPair, Pair
from voice_assistant.wake import DUE

from .conftest import GENERIC, FakeTrigger, SilentMic, needs_models

SILENCE = np.zeros(16000, np.int16)


@pytest.fixture
def hearing(monkeypatch, tmp_path):
    """Makes verifiers whose recognizer 'hears' fixed text, so the matching rules can be tested without the model."""
    import vosk

    monkeypatch.setattr(vosk, "Model", lambda path: None)
    monkeypatch.setattr(verify, "ensure_model", lambda folder: folder)

    def verifier(text: str, phrase: str = "hey tars") -> PhraseVerifier:
        v = PhraseVerifier(phrase, tmp_path)
        v.heard = lambda pcm: text
        return v

    return verifier


def listening(trigger, verifier, journal=None, wake_model: str = "") -> VerifiedTrigger:
    """A VerifiedTrigger on a fixed pair, listening with `trigger` and checking with `verifier`."""
    pair = Pair(INSTALLED, wake_model, "", None, trigger.threshold, 2.5, Path(wake_model))
    return VerifiedTrigger(
        FixedPair(pair),
        Path("models"),
        journal,
        make_trigger=lambda path, threshold: trigger,
        make_verifier=lambda phrase, folder: verifier,
    )


@pytest.mark.parametrize(
    "text, ok",
    [
        ("hey tars", True),
        ("hey darts", True),
        ("okay hey tars", True),
        ("hey stars", False),
        ("hey guitars", False),
        ("tars stop", False),
        ("hey cars", False),
        ("", False),
    ],
)
def test_only_the_wake_phrase_passes_whole_words_not_substrings(hearing, text, ok):
    assert hearing(text).check(SILENCE)[0] is ok


@pytest.mark.parametrize(
    "text, ok",
    [
        ("tars stop", True),
        ("tar stop", True),
        ("darts stop", True),
        ("stop", False),
        ("bus stop", False),
        ("please stop", False),
        ("stars stop", False),
        ("hey tars", False),
    ],
)
def test_tars_stop_passes_but_stop_alone_or_after_another_word_does_not(hearing, text, ok):
    assert hearing(text, "tars stop").check(SILENCE)[0] is ok


@pytest.mark.parametrize(
    "heard, outcome",
    [
        ("hey tars", ANSWER),
        ("hey cars", ASK),
        ("hey darts stop", ANSWER),
        ("[unk] hey guitars", ASK),
        ("hey tarot", ASK),
        ("hey there", IGNORE),
        ("hey jarvis", IGNORE),
        ("hey tara", IGNORE),  # people do say these
        ("lars", IGNORE),
        ("[unk] tars", IGNORE),
        ("stars", IGNORE),
        ("", IGNORE),  # no clear "hey": TV fragments
    ],
)
def test_close_but_improbable_phrases_ask_instead_of_answering(hearing, heard, outcome):
    assert hearing(heard).decide(SILENCE)[0] == outcome


def test_unknown_speech_is_reported_as_the_recognizer_says_it(hearing):
    assert hearing("hey [unk]").check(SILENCE) == (False, "hey [unk]")
    assert hearing("").check(SILENCE) == (False, "nothing clear")


def test_unknown_phrase_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="No wake check"):
        PhraseVerifier("hey jarvis", tmp_path)


def test_recent_audio_keeps_only_the_last_window():
    recent = RecentAudio(seconds=0.24)  # three 80 ms blocks
    for i in range(10):
        recent.add(np.full(BLOCK_SAMPLES, i, np.int16))
    assert len(recent.audio()) == 3 * BLOCK_SAMPLES and recent.audio()[0] == 7


def test_a_near_miss_ends_only_once_the_score_has_stayed_low():
    near, block = NearMisses(threshold=0.9), 0.08
    steady = [near.update(0.6, block) for _ in range(50)]  # the TV holding a score in the near band
    assert steady == [None] * 50 and near.peak == 0.6
    lows = [near.update(0.1, block) for _ in range(round(NEAR_QUIET_S / block) + 1)]
    assert [p for p in lows if p is not None] == [0.6]  # one near-miss, with its peak
    assert near.update(0.95, block) is None and near.peak == 0.0  # a wake is never a near-miss


def test_a_rejected_wake_keeps_listening_until_one_passes(hearing, capsys):
    trigger = FakeTrigger(fire_at=[5, 9])
    answers = iter([(False, "hey there"), (True, "hey tars")])
    verifier = hearing("")
    verifier.check = lambda pcm: next(answers)
    assert listening(trigger, verifier).wait(SilentMic(20)) == ANSWER
    assert trigger.n == 9 and trigger.resets == 2
    assert "heard 'hey there'" in capsys.readouterr().out


def test_a_close_call_returns_ask_right_away(hearing):
    trigger = FakeTrigger(fire_at=[3])
    assert listening(trigger, hearing("hey cars")).wait(SilentMic(20)) == ASK and trigger.n == 3


def test_a_learned_check_that_doesnt_fit_its_phrases_is_refused():
    spec = {
        "phrases": ["hey tars", "hey cars"],
        "extra_grammar": [],
        "max_alternatives": 8,
        "floor": -40.0,
        "weights": [1.0, 2.0],
        "bias": 0.0,
        "threshold": 0.5,
    }  # 2 phrases need 3 weights
    with pytest.raises(ValueError, match="2 weights for 2 phrases"):
        TunedCheck(spec)


def test_the_recognizer_is_unpacked_all_at_once(tmp_path, monkeypatch):
    def download(path, url, what):
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(f"{MODEL_NAME}/conf/model.conf", "x")
        return path

    monkeypatch.setattr(verify, "fetch", download)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", lambda self, to: (_ for _ in ()).throw(OSError("power cut")))
    with pytest.raises(OSError):
        ensure_model(tmp_path)
    assert not (tmp_path / MODEL_NAME).exists()  # nothing half-unpacked looks like a model
    monkeypatch.undo()
    monkeypatch.setattr(verify, "fetch", download)
    assert (ensure_model(tmp_path) / "conf" / "model.conf").read_text() == "x"
    assert not (tmp_path / f"{MODEL_NAME}.zip").exists()


@needs_models
def test_learned_check_prefers_a_clear_hey_tars_over_a_clear_lookalike():
    check = TunedCheck(json.loads((GENERIC / "hey_tars_check.json").read_text()))
    # Shaped like real Vosk output: a soft "t" is a near-tie with "darts"; a clear lookalike has no TARS guess at all.
    tars = [{"text": "hey tars", "confidence": 117.9}, {"text": "hey darts", "confidence": 117.1}]
    cars = [{"text": "hey cars", "confidence": 194.2}]
    assert check.confidence(tars) > check.threshold > check.confidence(cars)
    assert check.confidence([]) < check.threshold


def test_a_reminder_coming_due_ends_the_wait(hearing):
    trigger = FakeTrigger(fire_at=[])
    assert listening(trigger, hearing("hey tars")).wait(SilentMic(20), due=lambda: trigger.n >= 4) == DUE
    assert trigger.n == 4


def test_a_wake_in_the_same_block_as_a_due_reminder_wins(hearing):
    trigger = FakeTrigger(fire_at=[1])
    assert listening(trigger, hearing("hey tars")).wait(SilentMic(20), due=lambda: True) == ANSWER
