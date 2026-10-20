"""The short lines made ahead: the fixed ones kept in memory and on disk, the active reminders' kept until they're
done, anything else made when it's said, and a line that failed tried again ahead only once a minute."""

import threading
import types

from voice_assistant.phrases import RETRY_S, Phrases


def voice(down=None, made=None):
    """A voice that says each line as its text, or raises while `down[0]`; `made` lists every line it was asked."""

    def stream(text):
        if made is not None:
            made.append(text)
        if down and down[0]:
            raise ConnectionError("no voice")
        return iter([text.encode()])

    return types.SimpleNamespace(stream=stream, sample_rate=24_000)


def test_a_fixed_line_is_made_once_and_read_back_after_a_restart(tmp_path):
    made = []
    first = Phrases(voice(made=made), tmp_path)
    assert first.get("Yes?").result() == [b"Yes?"] and first.get("Yes?").result() == [b"Yes?"]
    later = Phrases(voice(down=[True], made=made), tmp_path)
    assert later.get("Yes?").result() == [b"Yes?"] and made == ["Yes?"]


def test_a_failed_line_made_ahead_is_tried_again_once_a_minute_but_at_once_when_its_needed():
    now, made, down = [1000.0], [], [True]
    phrases = Phrases(voice(down, made), clock=lambda: now[0])
    for _ in range(5):  # every two seconds, as the reminders' clock does
        phrases.keep_only({"A reminder: pills."})
        phrases.drain()
    assert made == ["A reminder: pills."]
    now[0] += RETRY_S
    phrases.keep_only({"A reminder: pills."})
    phrases.drain()
    assert len(made) == 2
    down[0] = False
    assert phrases.get("A reminder: pills.", keep=False).result() == [b"A reminder: pills."]  # needed now


def test_a_reminders_line_is_let_go_once_its_done_and_anything_else_isnt_kept():
    made = []
    phrases = Phrases(voice(made=made))
    phrases.keep_only({"A reminder: pills."})
    phrases.get("Toy Story is playing.", keep=False).result()
    assert "A reminder: pills." in phrases and "Toy Story is playing." not in phrases
    phrases.keep_only(set())
    assert "A reminder: pills." not in phrases


def test_a_line_that_came_too_late_for_one_wait_is_there_for_the_next():
    made, slow = [], threading.Event()

    def stream(text):
        slow.wait()
        made.append(text)
        return iter([text.encode()])

    phrases = Phrases(types.SimpleNamespace(stream=stream))
    phrases.keep_only({"A reminder: pills."})
    try:
        assert not phrases.get("A reminder: pills.", keep=False).done()  # the wait would give up here
    finally:
        slow.set()
    phrases.drain()
    assert phrases.get("A reminder: pills.", keep=False).result() == [b"A reminder: pills."] and len(made) == 1
