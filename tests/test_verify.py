"""The wake double-check: word matching, the audio window, and the trigger that ignores rejected wakes."""

import numpy as np
import pytest

from voice_assistant.audio import BLOCK_SAMPLES
import json
from pathlib import Path

from voice_assistant.verify import (
    ANSWER,
    ASK,
    IGNORE,
    PHRASES,
    PhraseVerifier,
    RecentAudio,
    TunedCheck,
    VerifiedTrigger,
)


def verifier_hearing(text: str) -> PhraseVerifier:
    """A verifier whose recognizer 'hears' fixed text, so the matching rules can be tested without the model."""
    v = PhraseVerifier.__new__(PhraseVerifier)
    v._accept = [a.split() for a in PHRASES["hey tars"]["accept"]]
    v._ask_after_hey = set(PHRASES["hey tars"]["ask_after_hey"])
    v._tuned, v.last_confidence = None, None
    v.heard = lambda pcm: text
    return v


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
def test_only_the_wake_phrase_passes_whole_words_not_substrings(text, ok):
    assert verifier_hearing(text).check(np.zeros(16000, np.int16))[0] is ok


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
def test_close_but_improbable_phrases_ask_instead_of_answering(heard, outcome):
    assert verifier_hearing(heard).decide(np.zeros(16000, np.int16))[0] == outcome


def test_unknown_speech_is_reported_as_the_recognizer_says_it():
    assert verifier_hearing("hey [unk]").check(np.zeros(16000, np.int16)) == (False, "hey [unk]")
    assert verifier_hearing("").check(np.zeros(16000, np.int16)) == (False, "nothing clear")


def test_recent_audio_keeps_only_the_last_window():
    recent = RecentAudio(seconds=0.24)  # three 80 ms blocks
    for i in range(10):
        recent.add(np.full(BLOCK_SAMPLES, i, np.int16))
    assert len(recent.audio()) == 3 * BLOCK_SAMPLES and recent.audio()[0] == 7


class FakeMic:
    def __init__(self, n):
        self.blocks = iter(np.zeros((n, BLOCK_SAMPLES), np.int16))

    def clear(self):
        pass

    def read(self):
        return next(self.blocks)


class FakeTrigger:
    """Fires on the given block numbers."""

    phrase, threshold = "hey tars", 0.9

    def __init__(self, fire_at):
        self.fire_at, self.n, self.resets = set(fire_at), 0, 0

    def score(self, block):
        self.n += 1
        return 1.0 if self.n in self.fire_at else 0.0

    def reset(self):
        self.resets += 1


def test_a_rejected_wake_keeps_listening_until_one_passes(capsys):
    trigger = FakeTrigger(fire_at=[5, 9])
    answers = iter([(False, "hey there"), (True, "hey tars")])
    verifier = verifier_hearing("")
    verifier.check = lambda pcm: next(answers)
    assert VerifiedTrigger(trigger, verifier).wait(FakeMic(20)) == ANSWER
    assert trigger.n == 9 and trigger.resets == 2
    assert "Heard 'hey there'" in capsys.readouterr().out


def test_a_close_call_returns_ask_right_away():
    trigger = FakeTrigger(fire_at=[3])
    verifier = verifier_hearing("hey cars")
    assert VerifiedTrigger(trigger, verifier).wait(FakeMic(20)) == ASK and trigger.n == 3


def test_unknown_phrase_is_a_clear_error(tmp_path):
    with pytest.raises(SystemExit, match="No wake check"):
        PhraseVerifier("hey jarvis", tmp_path)


TUNED = Path(__file__).parents[1] / "models" / "generic" / "hey_tars_check.json"


@pytest.mark.skipif(not TUNED.exists(), reason="no learned check trained yet")
def test_learned_check_prefers_a_clear_hey_tars_over_a_clear_lookalike():
    check = TunedCheck(json.loads(TUNED.read_text()))
    # Shaped like real Vosk output: a soft "t" is a near-tie with "darts"; a clear lookalike has no TARS guess at all.
    tars = [{"text": "hey tars", "confidence": 117.9}, {"text": "hey darts", "confidence": 117.1}]
    cars = [{"text": "hey cars", "confidence": 194.2}]
    assert check.confidence(tars) > check.threshold > check.confidence(cars)
    assert check.confidence([]) < check.threshold  # heard nothing
