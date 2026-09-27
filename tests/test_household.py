"""training.household's own decisions: which wakes become which clips, and when a new pair is good enough."""

from pathlib import Path

import numpy as np
import pytest

from voice_assistant.events import NOT_REAL, REAL, EventLog
from voice_assistant.verify import ANSWER, IGNORE

household = pytest.importorskip("training.household")
REPO = Path(__file__).parents[1]


class Silence:
    """A speech detector that hears speech in the blocks it's told to."""

    def __init__(self, speech_blocks):
        self.speech, self.n = set(speech_blocks), 0

    def reset(self):
        self.n = 0

    def __call__(self, block):
        self.n += 1
        return 0.9 if self.n in self.speech else 0.1


def test_a_real_wake_is_cut_shortly_after_its_last_speech():
    pcm = np.arange(48000, dtype=np.int16)  # 3 s, 37 blocks of 80 ms
    cut = household.trim_after_speech(pcm, Silence({5, 6, 10}))
    assert len(cut) == 10 * 1280 + int(household.END_PAD_S * 16000)
    assert np.array_equal(household.trim_after_speech(pcm, Silence(set())), pcm)  # no speech found: kept whole


@pytest.mark.skipif(not (REPO / "models" / "silero_vad.onnx").exists(), reason="no speech detector downloaded")
def test_labeled_wakes_become_train_and_test_clips(tmp_path):
    log = EventLog(tmp_path / "events")
    pcm = np.zeros(16000, np.int16)
    labels = {}
    for i in range(1, 11):
        e = log.add_wake(pcm, 0.9, ANSWER if i % 2 else IGNORE, "hey tars", 0.9)
        labels[e] = REAL if i % 2 else NOT_REAL
        log.set_label(e, labels[e])
    log.add_wake(pcm, 0.9, IGNORE, "hey cars", 0.1)  # only the check's own verdict: left out
    counts = household.household_clips(log, tmp_path / "run")
    assert counts == {"train_positive": 4, "train_negative": 4, "test_positive": 1, "test_negative": 1}
    assert (tmp_path / "run/clips/test/negative/10.wav").exists() and (
        tmp_path / "run/clips/test/positive/5.wav"
    ).exists()


def score(before, after, **kw):
    return household.Score("x", before, after, 10, **kw)


@pytest.mark.parametrize(
    "scores, install",
    [
        ([score(5, 6, household=True), score(90, 90)], True),  # better for you, same for others
        ([score(5, 6, household=True), score(90, 89)], False),  # one clip worse for others
        ([score(5, 5, household=True), score(90, 95)], False),  # better only for others
        ([score(2, 1, household=True, lower_is_better=True), score(0, 0, lower_is_better=True)], True),
    ],
)
def test_a_pair_is_installed_only_if_better_for_you_and_no_worse_for_anyone(scores, install):
    assert household.good_enough(scores) is install
