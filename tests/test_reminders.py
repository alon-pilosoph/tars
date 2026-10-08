"""Reminders, timers and messages: what can be set, when they're due, what TARS says, and how they end."""

from datetime import datetime

import pytest

from voice_assistant.reminders import (
    ACKNOWLEDGED,
    CANCELLED,
    LATE_S,
    MESSAGE,
    MISSED,
    REMINDER,
    SAID,
    SCHEDULED,
    TIMER,
    VOICE,
    WAITING,
    WEB,
    NewReminder,
    Reminders,
    due_at,
    late,
    line,
    says_line,
    when,
)

NOW = 1_800_000_000.0


@pytest.fixture
def reminders(log):
    return Reminders(log.store, repeat_every_s=120, max_tries=3)


def add(reminders, voices=(), **kw):
    kw.setdefault("kind", REMINDER)
    kw.setdefault("text", "call the bank")
    kw.setdefault("due", NOW + 60)
    return reminders.add(NewReminder(**kw), VOICE, voices=voices, now=NOW)


def test_a_reminder_is_scheduled_with_the_defaults(reminders):
    r = reminders.get(add(reminders, for_name="  alon "))
    assert r["status"] == SCHEDULED and r["next_at"] == NOW + 60 and r["for_name"] == "alon"
    assert (r["repeat_every_s"], r["max_tries"], r["needs_ack"]) == (120, 3, 1)


@pytest.mark.parametrize(
    "kw, why",
    [
        ({"kind": "alarm"}, "kind must be"),
        ({"text": "  "}, "needs something to say"),
        ({"kind": MESSAGE}, "needs someone"),
        ({"due": None}, "needs a time"),
        ({"due": None, "for_name": "stacey"}, "doesn't know stacey's voice"),
        ({"due": NOW - 3600}, "already passed"),
        ({"due": NOW + 400 * 86400}, "more than a year"),
        ({"repeat_every_s": 5}, "at most every"),
        ({"max_tries": 0}, "1 to"),
    ],
)
def test_what_cant_be_done_says_why(reminders, kw, why):
    with pytest.raises(ValueError, match=why):
        add(reminders, **kw)


def test_a_timer_needs_no_text_and_waiting_for_someone_needs_their_voice(reminders):
    assert add(reminders, kind=TIMER, text=None)
    held = add(reminders, kind=MESSAGE, for_name="Stacey", due=None, voices=["stacey"])
    assert reminders.get(held)["next_at"] is None and [r["id"] for r in reminders.held_for("STACEY")] == [held]
    assert reminders.held_for("alon") == [] and reminders.held_for(None) == []


def test_it_is_due_at_its_time_and_not_before(reminders):
    rid = add(reminders)
    assert reminders.due(NOW + 59) == [] and reminders.next_at() == NOW + 60
    assert [r["id"] for r in reminders.due(NOW + 60)] == [rid]


def test_waiting_for_an_acknowledgement_it_is_said_again_then_missed(reminders):
    rid = add(reminders)
    t = NOW + 60
    for n in range(1, 4):
        assert [r["id"] for r in reminders.due(t)] == [rid]
        reminders.said(rid, now=t)
        assert reminders.get(rid)["status"] == WAITING and reminders.get(rid)["tries"] == n
        assert reminders.due(t + 119) == []
        t += 120
    assert reminders.due(t) == [] and reminders.get(rid)["status"] == MISSED and reminders.next_at() is None


def test_one_that_doesnt_wait_is_said_once(reminders):
    rid = add(reminders, needs_ack=False)
    reminders.said(rid, now=NOW + 60)
    assert reminders.get(rid)["status"] == SAID and reminders.due(NOW + 10_000) == []


def test_acknowledging_records_who_and_how_and_only_counts_once(reminders):
    rid = add(reminders)
    reminders.said(rid, now=NOW + 60)
    assert reminders.waiting()[0]["id"] == rid
    assert reminders.ack(rid, "stacey", VOICE, now=NOW + 70)
    r = reminders.get(rid)
    assert (r["status"], r["acked_by"], r["acked_via"], r["acked_at"]) == (ACKNOWLEDGED, "stacey", VOICE, NOW + 70)
    assert not reminders.ack(rid, None, WEB) and reminders.waiting() == [] and reminders.due(NOW + 10_000) == []


def test_snoozing_starts_it_over_even_once_missed_and_cancelling_ends_it(reminders):
    rid = add(reminders, max_tries=1)
    reminders.said(rid, now=NOW + 60)
    reminders.due(NOW + 180)
    assert reminders.get(rid)["status"] == MISSED
    assert reminders.snooze(rid, 10, now=NOW + 200)
    r = reminders.get(rid)
    assert (r["status"], r["tries"], r["next_at"]) == (SCHEDULED, 0, NOW + 800)
    assert reminders.cancel(rid) and reminders.get(rid)["status"] == CANCELLED and not reminders.cancel(rid)
    with pytest.raises(ValueError):
        reminders.snooze(add(reminders), 0)


def test_the_active_ones_come_first_soonest_first(reminders):
    later, sooner, done = add(reminders, due=NOW + 600), add(reminders, due=NOW + 60), add(reminders)
    reminders.cancel(done)
    assert [r["id"] for r in reminders.all()] == [sooner, later, done]
    assert [r["id"] for r in reminders.active()] == [sooner, later]


def row(**kw):
    return {"kind": REMINDER, "text": "call the bank", "for_name": None, "from_name": None, **kw}


@pytest.mark.parametrize(
    "r, said",
    [
        (row(), "A reminder: call the bank."),
        (row(for_name="alon"), "Alon, a reminder: call the bank."),
        (row(for_name="alon", from_name="Alon"), "Alon, a reminder: call the bank."),  # their own, no "from"
        (row(for_name="mary ann", from_name="alon"), "Mary Ann, a reminder from Alon: call the bank."),
        (row(kind=MESSAGE, for_name="stacey", from_name="alon", text="Dinner's at eight!"),
         "Stacey, a message from Alon: Dinner's at eight!"),
        (row(kind=MESSAGE, for_name="stacey", text="dinner's at eight"), "Stacey, a message: dinner's at eight."),
        (row(kind=TIMER, text=None), "Your timer is done."),
        (row(kind=TIMER, text="pasta", for_name="alon"), "Alon, your pasta timer is done."),
    ],
)  # fmt: skip
def test_what_tars_says(r, said):
    assert line(r) == said


def test_said_late_it_says_when_it_was_due():
    due = datetime(2026, 10, 6, 8, 0).timestamp()  # noqa: DTZ001 - local time, the way TARS says it
    r = {"tries": 0, "due": due, "next_at": due}
    assert late(r, now=due + LATE_S - 1) is None
    assert late(r, now=due + 3600) == "This was due at 8:00 AM."
    assert late(r, now=due + 86400) == "This was due Tuesday at 8:00 AM."
    assert late({**r, "tries": 1}, now=due + 3600) is None and late({"tries": 0, "next_at": None}) is None


def test_a_snoozed_reminder_said_at_its_new_time_isnt_late(reminders):
    rid = add(reminders)  # due at NOW + 60
    reminders.said(rid, now=NOW + 60)
    reminders.snooze(rid, 10, now=NOW + 70)
    (r,) = reminders.due(NOW + 670)
    assert late(r, now=NOW + 670) is None
    assert late(r, now=NOW + 670 + 3600) == f"This was due at {when(NOW + 670, NOW + 670)}."  # then TARS was off


def test_a_timer_rings_every_10_s_for_15_minutes_until_turned_off_whatever_was_asked(log):
    reminders = Reminders(log.store)  # the real defaults
    r = reminders.get(add(reminders, kind=TIMER, text="pasta", needs_ack=False))
    assert (r["needs_ack"], r["repeat_every_s"], r["max_tries"]) == (1, 10, 90)
    other = reminders.get(add(reminders))
    assert (other["repeat_every_s"], other["max_tries"]) == (300, 4)
    with pytest.raises(ValueError, match="at most every 5 seconds"):
        add(reminders, kind=TIMER, repeat_every_s=1)


def test_a_ringing_timer_says_its_line_on_the_first_ring_and_once_a_minute_and_only_chimes_between():
    rings = [says_line({"kind": TIMER, "tries": n}) for n in range(13)]
    assert [n for n, said in enumerate(rings) if said] == [0, 6, 12]
    assert all(says_line({"kind": REMINDER, "tries": n}) for n in range(5))


def test_numbers_that_arent_numbers_are_refused(reminders):
    for kw in ({"due": float("nan")}, {"due": float("inf")}, {"repeat_every_s": float("nan")}):
        with pytest.raises(ValueError, match="real numbers"):
            add(reminders, **kw)
    with pytest.raises(ValueError):
        reminders.snooze(add(reminders), float("nan"))


def test_only_an_active_one_can_be_cancelled_and_a_missed_one_can_still_be_snoozed(reminders):
    rid = add(reminders, max_tries=1)
    reminders.said(rid, now=NOW + 60)
    reminders.due(NOW + 1000)
    assert reminders.get(rid)["status"] == MISSED
    assert not reminders.cancel(rid) and reminders.snooze(rid, 5, now=NOW + 1000)


def local(text: str) -> float:
    return datetime.fromisoformat(text).astimezone().timestamp()


WEDNESDAY_5PM = "2026-10-07T17:00"


@pytest.mark.parametrize(
    "now, day, clock, due",
    [
        (WEDNESDAY_5PM, None, "18:30", "2026-10-07T18:30"),  # still to come today
        (WEDNESDAY_5PM, None, "16:00", "2026-10-08T16:00"),  # gone today: the next time the clock shows it
        (WEDNESDAY_5PM, "today", "16:00", "2026-10-07T16:00"),  # as said; check() then says it's passed
        (WEDNESDAY_5PM, "tomorrow", "07:00", "2026-10-08T07:00"),
        (WEDNESDAY_5PM, "Friday", "09:00", "2026-10-09T09:00"),
        (WEDNESDAY_5PM, "wednesday", "18:00", "2026-10-07T18:00"),  # today's, since it's still to come
        (WEDNESDAY_5PM, "wednesday", "16:00", "2026-10-14T16:00"),  # gone today: next week's
        (WEDNESDAY_5PM, "2026-11-03", "08:15", "2026-11-03T08:15"),
        (WEDNESDAY_5PM, None, "6:30 pm", "2026-10-07T18:30"),  # forgiven, though HH:MM was asked for
        ("2026-10-30T12:00", "monday", "09:00", "2026-11-02T09:00"),  # over a month's end
        ("2026-12-31T22:00", "tomorrow", "09:00", "2027-01-01T09:00"),  # and a year's
        ("2026-12-31T22:00", None, "08:00", "2027-01-01T08:00"),
        ("2026-10-07T00:30", "tomorrow", "09:00", "2026-10-07T09:00"),
        ("2026-10-07T00:30", "tomorrow", "00:15", "2026-10-08T00:15"),
        ("2026-10-07T05:00", "tomorrow", "09:00", "2026-10-08T09:00"),
        (WEDNESDAY_5PM, "tonight", "20:00", "2026-10-07T20:00"),
        (WEDNESDAY_5PM, None, "18:30:00", "2026-10-07T18:30"),
    ],
)
def test_a_day_and_time_as_said_become_the_right_moment(now, day, clock, due):
    assert due_at(day, clock, local(now)) == local(due)


@pytest.mark.parametrize("clock", ["25:00", "9-ish", "", None, "12:75"])
def test_a_time_that_isnt_one_is_refused(clock):
    with pytest.raises(ValueError, match="HH:MM"):
        due_at(None, clock, local(WEDNESDAY_5PM))
