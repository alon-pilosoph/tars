"""training.household's own decisions: which wakes become which clips, and when a new pair is good enough."""

import numpy as np
import pytest

from training import household
from voice_assistant.events import NOT_REAL, REAL, EventLog
from voice_assistant.verify import ANSWER, IGNORE


class FakeVAD:
    def __init__(self, speech_blocks):
        self.speech, self.n = set(speech_blocks), 0

    def reset(self):
        self.n = 0

    def __call__(self, block):
        self.n += 1
        return 0.9 if self.n in self.speech else 0.1


def test_a_real_wake_is_cut_shortly_after_its_last_speech():
    pcm = np.arange(48000, dtype=np.int16)  # 3 s, 37 blocks of 80 ms
    cut = household.trim_after_speech(pcm, FakeVAD({5, 6, 10}))
    assert len(cut) == 10 * 1280 + int(household.END_PAD_S * 16000)
    assert np.array_equal(household.trim_after_speech(pcm, FakeVAD(set())), pcm)  # no speech found: kept whole


def test_labeled_wakes_become_train_and_test_clips(tmp_path):
    log = EventLog(tmp_path / "events")
    pcm = np.zeros(16000, np.int16)
    for i in range(1, 11):
        e = log.add_wake(pcm, 0.9, ANSWER if i % 2 else IGNORE, "hey tars", 0.9, ts=1000.0 + 60 * i)
        log.set_label(e, REAL if i % 2 else NOT_REAL)
    log.add_wake(pcm, 0.9, IGNORE, "hey cars", 0.1, ts=2000.0)  # only the check's own verdict: left out
    counts = household.household_clips(log, tmp_path / "run", FakeVAD(set()))
    assert counts == {"train_positive": 4, "train_negative": 4, "test_positive": 1, "test_negative": 1}
    assert (tmp_path / "run/clips/test/negative/10.wav").exists()
    assert (tmp_path / "run/clips/test/positive/5.wav").exists()


def test_wakes_seconds_apart_are_never_split_between_train_and_test():
    # 5 is held out by id, and 6 came 3 s after it: its window may hold the same "hey TARS".
    events = [{"id": i, "ts": t} for i, t in ((4, 100.0), (5, 200.0), (6, 203.0), (7, 300.0), (10, 400.0))]
    assert household.splits(events) == {4: "train", 5: "test", 6: "test", 7: "train", 10: "test"}


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
