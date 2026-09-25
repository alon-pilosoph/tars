"""The mic test's event log: one line per wake, one per near-miss with its peak."""

from voice_assistant.mic_test import WakeLog, meter


def feed(log, scores, step=0.08, start=0.0):
    return [line for i, s in enumerate(scores) if (line := log.update(s, start + i * step))]


def test_a_wake_is_logged_once_even_while_the_score_stays_high():
    log = WakeLog(threshold=0.9)
    lines = feed(log, [0.1, 0.95, 0.99, 0.97, 0.2])
    assert len(lines) == 1 and "WAKE #1" in lines[0] and "0.95" in lines[0]


def test_a_near_miss_is_logged_with_its_peak_after_it_ends():
    log = WakeLog(threshold=0.9)
    lines = feed(log, [0.5, 0.7, 0.6] + [0.1] * 20)
    assert len(lines) == 1 and "close" in lines[0] and "0.70" in lines[0]
    assert (log.wakes, log.near_misses) == (0, 1)


def test_quiet_scores_log_nothing_and_history_keeps_every_event():
    log = WakeLog(threshold=0.9)
    assert feed(log, [0.1] * 50) == []
    feed(log, [0.95] + [0.1] * 30 + [0.96], start=10)
    assert log.wakes == 2 and len(log.history) == 2


def test_meter_marks_the_threshold_on_the_wake_bar():
    line = meter(level=0.5, score=0.5, threshold=0.95, speech=True)
    assert "SPEECH" in line and "|" in line.split("wake [")[1] and line.endswith("0.50")
