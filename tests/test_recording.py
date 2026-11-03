"""Recording sessions: the scripts, the check on each take, the sessions and their takes (recording.py); the
assistant's take desk (takes.py) and the socket the web UI asks it over (control.py); the assistant held by a
session; and the recording page's API (webui.py)."""

import base64
import random
import shutil
import tempfile
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from voice_assistant import control
from voice_assistant.audio import SAMPLE_RATE
from voice_assistant.control import Refused
from voice_assistant.recording import (
    CUT_OFF,
    HOUSE,
    LOUD,
    MOST_COMMANDS,
    NO_SPEECH,
    NOTHING,
    QUIET,
    SHORT,
    SILENCE,
    TAP,
    VOICE,
    Sessions,
    check,
    house_script,
    voice_script,
)
from voice_assistant.takes import BUSY, GONE, MUTED, TakeDesk
from voice_assistant.wake import DUE
from voice_assistant.webui import create_app

from .conftest import make_assistant


def take_audio(speech_s=1.0, before_s=0.5, after_s=0.5, loudness=0.2, room=0.001, seed=0) -> bytes:
    """Room noise, then a voice-like tone for `speech_s`, then room noise: 16 kHz int16 PCM."""
    rng = np.random.default_rng(seed)

    def n(s):
        return int(s * SAMPLE_RATE)

    t = np.arange(n(speech_s)) / SAMPLE_RATE
    voice = loudness * np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
    x = np.concatenate([np.zeros(n(before_s)), voice, np.zeros(n(after_s))])
    x += rng.normal(0, room, len(x))
    return (np.clip(x, -1, 0.99997) * 32768).astype(np.int16).tobytes()


# ---------- The check ----------


def test_a_clear_take_is_fine():
    assert check(take_audio(), SILENCE) is None


@pytest.mark.parametrize(
    "pcm, stopped, problem",
    [
        (None, NO_SPEECH, NOTHING),
        (take_audio(speech_s=0.0), SILENCE, NOTHING),  # only the room
        (take_audio(loudness=1.2), SILENCE, LOUD),  # clipped
        (take_audio(before_s=0.0), SILENCE, CUT_OFF),  # already talking when it started: the start is missing
        (take_audio(after_s=0.0), TAP, CUT_OFF),  # tapped mid-word
        (take_audio(speech_s=0.2), SILENCE, SHORT),
        (take_audio(loudness=0.012), SILENCE, QUIET),
    ],
    ids=["none", "room only", "clipped", "start cut", "tapped mid-word", "short", "quiet"],
)
def test_what_makes_a_take_worth_doing_again(pcm, stopped, problem):
    assert check(pcm, stopped) == problem


def test_tapping_after_a_pause_isnt_cutting_it_off():
    assert check(take_audio(after_s=0.5), TAP) is None


def test_quiet_is_fine_when_the_prompt_asks_for_it():
    soft = take_audio(loudness=0.012)
    assert check(soft, SILENCE) == QUIET
    assert check(soft, SILENCE, quiet_ok=True) is None


# ---------- Scripts ----------

DEVICES = [
    {"name": "front door", "entity": "lock.front_door"},
    {"name": "AC", "kind": "my_house.ac"},
    {"name": "bedroom AC", "entity": "climate.bedroom"},
    {"name": "TV", "kind": "androidtv", "address": "10.0.0.5"},
    {"name": "boiler", "entity": "switch.boiler"},
    {"name": "temperature", "entity": "sensor.temperature"},
]


def test_the_house_script_asks_for_everything_each_device_does_and_mixes_in_what_mustnt_count():
    prompts = house_script(DEVICES, random.Random(1))
    commands = [p for p in prompts if p.means]
    means = {p.means.split(":")[0] for p in commands}
    assert means == {
        "front door|lock", "front door|unlock", "front door|locked",
        "AC|on", "AC|off",
        "bedroom AC|on", "bedroom AC|off", "bedroom AC|set",
        "TV|on", "TV|off",
        "boiler|on", "boiler|off",
    }  # fmt: skip
    for p in commands:
        name = p.means.split("|")[0]
        assert name.lower() in p.say.lower()
    assert len({p.id for p in prompts}) == len(prompts)
    not_commands = [p for p in prompts if p.means == ""]
    assert len(not_commands) == 6  # three that name a device without asking, three of plain chatter
    assert any(p.how for p in commands) and all(p.quiet == (p.how.startswith(("Quietly", "From"))) for p in prompts)


def test_a_big_house_is_sampled_not_recorded_whole():
    devices = [{"name": f"light {n}", "entity": f"light.l{n}"} for n in range(40)]
    assert len([p for p in house_script(devices, random.Random(2)) if p.means]) == MOST_COMMANDS


def test_the_voice_script_has_enough_to_enroll_with_some_from_afar():
    prompts = voice_script()
    assert len(prompts) >= 10 and len({p.id for p in prompts}) == len(prompts)
    assert any(p.quiet for p in prompts) and all(p.means is None for p in prompts)


# ---------- Sessions ----------


@pytest.fixture
def sessions(log):
    now = [1000.0]
    s = Sessions(log.store, clock=lambda: now[0])
    s.now = now
    return s


def make_session(sessions, kind=VOICE):
    token = sessions.create(kind, "Alon", voice_script()[:3])
    return token, sessions.open(token)


def test_a_link_works_for_a_day_and_not_after_its_session_finishes(sessions):
    token, _ = make_session(sessions)
    assert sessions.open("not-the-token") is None
    sessions.now[0] += 23 * 3600
    assert sessions.open(token)
    sessions.now[0] += 2 * 3600
    assert sessions.open(token) is None


def test_a_redo_replaces_the_take_and_its_audio(sessions, log):
    _, session = make_session(sessions)
    prompt = sessions.prompts(session)[0]
    first = sessions.add_take(session, prompt, take_audio(), SILENCE)
    second = sessions.add_take(session, prompt, take_audio(loudness=0.012), SILENCE)
    assert [t["id"] for t in sessions.takes(session["id"])] == [second["id"]]
    assert second["problem"] == QUIET and second["seconds"] == pytest.approx(2.0)
    assert not (log.folder / first["audio"]).exists() and (log.folder / second["audio"]).exists()


def test_nothing_heard_is_a_take_without_audio(sessions):
    _, session = make_session(sessions)
    take = sessions.add_take(session, sessions.prompts(session)[0], None, NO_SPEECH)
    assert take["audio"] is None and take["problem"] == NOTHING


def test_a_session_finishes_once_every_prompt_has_a_take_or_was_skipped(sessions):
    token, session = make_session(sessions)
    first, second, third = sessions.prompts(session)
    sessions.add_take(session, first, take_audio(), SILENCE)
    sessions.skip(session, second.id)
    sessions.skip(session, second.id)  # twice is once
    assert not sessions.finish(session)
    sessions.add_take(session, third, take_audio(), SILENCE)
    assert sessions.finish(session) and not sessions.finish(session)  # what follows runs once
    assert sessions.open(token) is None


def test_the_page_isnt_told_what_a_command_means(sessions):
    token = sessions.create(HOUSE, "Alon", house_script(DEVICES, random.Random(3)))
    public = sessions.public(sessions.open(token))
    assert public["title"] == "House commands" and public["person"] == "Alon"
    assert all(set(p) == {"id", "say", "how"} for p in public["prompts"])
    assert public["minutes"] >= 1 and public["takes"] == [] and public["skipped"] == []


def test_an_unknown_kind_of_session_is_refused(sessions):
    with pytest.raises(ValueError):
        sessions.create("karaoke", "Alon", [])


# ---------- The take desk ----------


def recording_thread(desk, pcm, stopped=SILENCE, wait_for_tap=False):
    """The assistant's side: picks up one take, 'beeps', records, ends it."""

    def run():
        take = None
        while take is None:
            take = desk.next(0.05)
        if desk.recording(take):
            if wait_for_tap:
                take.stop.wait(2)
            desk.ended(take, pcm, stopped)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_a_take_is_answered_once_recording_and_its_audio_fetched_when_it_ends():
    desk, pcm = TakeDesk(), take_audio()
    recording_thread(desk, pcm, wait_for_tap=True)
    take = desk.ask()
    assert desk.state(take)["state"] == "recording" and desk.held()
    done = desk.finish(take.id)  # the tap
    assert done["state"] == "done" and done["stopped"] == SILENCE
    assert base64.b64decode(done["pcm"]) == pcm and done["seconds"] == pytest.approx(len(pcm) / 2 / SAMPLE_RATE)


def test_a_take_nobody_picks_up_is_refused_as_busy_and_never_recorded():
    desk = TakeDesk()
    with pytest.raises(Refused) as e:
        desk.ask(pickup_s=0.05)
    assert e.value.why == BUSY
    late = desk.next(0.05)
    assert late is None  # the assistant, back from its conversation, skips it


def test_a_take_long_forgotten_is_gone():
    desk = TakeDesk()
    with pytest.raises(Refused) as e:
        desk.find(99)
    assert e.value.why == GONE


def test_the_hold_lapses_unless_the_page_keeps_asking():
    now = [0.0]
    desk = TakeDesk(clock=lambda: now[0])
    desk.hold(seconds=180)
    now[0] = 179
    assert desk.held()
    now[0] = 181
    assert not desk.held()
    desk.hold()
    desk.release()
    assert not desk.held()


def test_each_session_holds_tars_on_its_own():
    desk = TakeDesk()
    desk.hold("1")
    desk.hold("2")
    desk.release("1")  # one page leaves: the other still holds it, so a "hey TARS" prompt doesn't wake it
    assert desk.held()
    desk.release("2")
    assert not desk.held()


def test_take_ids_dont_start_again_after_a_restart():
    # The web UI outlives the assistant and remembers takes by their id: a new run's mustn't be an old one's.
    assert TakeDesk()._ids.__next__() != TakeDesk()._ids.__next__()


# ---------- The socket ----------


@pytest.fixture
def short_folder():
    folder = Path(tempfile.mkdtemp(dir="/tmp"))  # a Unix socket's path must be short (about 100 characters)
    yield folder
    shutil.rmtree(folder)


def test_a_request_and_its_answer_over_the_socket(short_folder):
    def refuse(_):
        raise Refused(MUTED)

    server = control.Server(short_folder, {"echo": lambda r: {"ok": True, "got": r["x"]}, "no": refuse})
    try:
        assert control.ask(short_folder, "echo", x=[1, "two"])["got"] == [1, "two"]
        with pytest.raises(Refused) as e:
            control.ask(short_folder, "no")
        assert e.value.why == MUTED
        with pytest.raises(Refused):
            control.ask(short_folder, "nonsense")
        assert (short_folder / control.SOCKET).stat().st_mode & 0o077 == 0  # this user's only
    finally:
        server.close()
    with pytest.raises(control.Unavailable):
        control.ask(short_folder, "echo", x=1)


# ---------- The assistant, held ----------


def test_held_it_records_takes_instead_of_listening_for_its_name(speaker):
    pcm = take_audio()
    assistant, _ = make_assistant(speaker, [pcm], [])
    assistant.mic.clear = lambda: None
    ops = assistant.control_ops()
    ops["hold"]({})
    answers = {}

    def page():
        answers["record"] = ops["record"]({})
        answers["stop"] = ops["stop"]({"id": answers["record"]["id"]})
        ops["release"]({})

    threading.Thread(target=page, daemon=True).start()
    assert assistant._wait_held() is None  # until the page let go
    assert answers["stop"]["id"] == answers["record"]["id"]  # answered with the take it then stopped
    assert base64.b64decode(answers["stop"]["pcm"]) == pcm and answers["stop"]["stopped"] == SILENCE
    assert assistant.mic.pauses == 1  # the beep, with the mic shut
    assert assistant.talk().startswith("listening")


def test_tapped_mid_sentence_what_was_said_so_far_is_the_take(speaker):
    assistant, _ = make_assistant(speaker, [], [])
    part = take_audio(speech_s=0.5, after_s=0.0)

    def record(mic, on_audio=None, stop=None, **kw):
        on_audio(part)
        while not stop():
            time.sleep(0.01)
        # None: the recorder's answer to a stop

    assistant.recorder = types.SimpleNamespace(record=record)
    started = threading.Thread(target=lambda: assistant._take(assistant.takes.next(2)), daemon=True)
    started.start()
    t = assistant.takes.ask()
    done = assistant.takes.finish(t.id)
    assert base64.b64decode(done["pcm"]) == part and done["stopped"] == TAP


def test_muted_no_take_is_recorded_and_held_whats_due_is_still_said(speaker, monkeypatch):
    assistant, _ = make_assistant(speaker, [], [])
    assistant.mic.clear = lambda: None
    assistant.mute(True)
    with pytest.raises(Refused) as e:
        assistant.control_ops()["record"]({})
    assert e.value.why == MUTED
    assistant.mute(False)
    assistant.takes.hold()
    assert assistant.talk().startswith("ignored: a recording session")
    due = iter([False, True])
    monkeypatch.setattr(assistant, "_due", lambda: next(due, False))
    assert assistant._wait_held() == DUE


# ---------- The page's API ----------


class FakeAssistant:
    """control.ask, as an assistant that records `pcm` for every take, or refuses with `why`."""

    def __init__(self, pcm=None, why=None):
        self.pcm, self.why, self.asked, self.ids = pcm or take_audio(), why, [], 0
        self.sessions = []  # the session each op was asked for

    def __call__(self, op, **args):
        self.asked.append(op)
        self.sessions.append(args.get("session"))
        if self.why == "down":
            raise control.Unavailable("no socket")
        if self.why and op == "record":
            raise Refused(self.why)
        if op == "record":
            self.ids += 1
            return {"ok": True, "id": self.ids, "state": "recording", "seconds": 0.0, "level": 0.0}
        if op == "status":
            return {"ok": True, "id": args["id"], "state": "done", "seconds": 2.0, "level": 0.0}
        if op == "stop":
            pcm = base64.b64encode(self.pcm).decode()
            return {"ok": True, "id": args["id"], "state": "done", "seconds": 2.0, "pcm": pcm, "stopped": SILENCE}
        return {"ok": True}


def page(log, fake=None, finished=None):
    fake = fake or FakeAssistant()
    client = TestClient(create_app(log, ask=fake, finished=finished), base_url="http://tars.local:8080")
    return client, fake


def test_a_take_id_the_assistant_gives_again_isnt_an_old_take(log):
    # Should an id come round again, say after a restart, the new take is recorded, not the old one shown.
    token = Sessions(log.store).create(VOICE, "Alon", voice_script()[:2])
    client, fake = page(log)
    first, second = client.get(f"/api/session/{token}").json()["prompts"]
    a = client.post(f"/api/session/{token}/record", json={"prompt": first["id"]}).json()
    client.get(f"/api/session/{token}/record/{a['id']}")
    fake.ids = 0
    b = client.post(f"/api/session/{token}/record", json={"prompt": second["id"]}).json()
    assert b["id"] == a["id"]
    heard = client.get(f"/api/session/{token}/record/{b['id']}").json()
    assert heard["take"]["prompt"] == second["id"]
    sessions = Sessions(log.store)
    kept = sorted(t["prompt"] for t in sessions.takes(sessions.open(token)["id"]))
    assert kept == sorted([first["id"], second["id"]])


def test_holds_and_releases_are_by_session(log):
    token = Sessions(log.store).create(VOICE, "Alon", voice_script()[:2])
    client, fake = page(log)
    session_id = Sessions(log.store).open(token)["id"]
    client.post(f"/api/session/{token}/begin")
    client.post(f"/api/session/{token}/leave")
    assert list(zip(fake.asked, fake.sessions, strict=True)) == [("hold", session_id), ("release", session_id)]


def test_enrolling_waits_for_a_regroup_of_the_voices(log):
    # A finished voice session changes the voices, so it waits its turn behind a regroup, and the other way round.
    token = Sessions(log.store).create(VOICE, "Alon", voice_script()[:1])
    regrouping, let_go, enrolled = threading.Event(), threading.Event(), threading.Event()

    def regroup():
        regrouping.set()
        let_go.wait(5)
        return {"ok": True}

    client = TestClient(
        create_app(log, ask=FakeAssistant(), recluster=regroup, finished=lambda s: enrolled.set()),
        base_url="http://tars.local:8080",
    )
    (prompt,) = client.get(f"/api/session/{token}").json()["prompts"]
    rec = client.post(f"/api/session/{token}/record", json={"prompt": prompt["id"]}).json()
    client.post(f"/api/session/{token}/record/{rec['id']}/stop")
    threading.Thread(target=lambda: client.post("/api/recluster"), daemon=True).start()
    assert regrouping.wait(5)
    assert client.post(f"/api/session/{token}/finish").status_code == 200
    assert not enrolled.wait(0.3)  # still regrouping
    let_go.set()
    assert enrolled.wait(5)


def test_a_whole_session_from_the_page(log):
    token = Sessions(log.store).create(VOICE, "Alon", voice_script()[:2])
    ended = threading.Event()
    client, fake = page(log, finished=lambda s: ended.set())
    first, second = client.get(f"/api/session/{token}").json()["prompts"]
    assert client.post(f"/api/session/{token}/begin").status_code == 200
    rec = client.post(f"/api/session/{token}/record", json={"prompt": first["id"]}).json()
    assert rec["state"] == "recording"
    heard = client.get(f"/api/session/{token}/record/{rec['id']}").json()  # it ended at a pause
    assert heard["state"] == "done" and heard["take"]["problem"] is None
    assert client.post(f"/api/session/{token}/record/{rec['id']}/stop").json() == heard  # kept once
    audio = client.get(f"/api/session/{token}/takes/{heard['take']['id']}/audio")
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"
    assert client.post(f"/api/session/{token}/finish").status_code == 409  # the second isn't done
    client.post(f"/api/session/{token}/skip", json={"prompt": second["id"]})
    assert client.post(f"/api/session/{token}/finish").status_code == 200
    assert ended.wait(2) and fake.asked.count("release") == 1
    assert client.get(f"/api/session/{token}").status_code == 404  # the link is used up


@pytest.mark.parametrize(
    "why, status, says",
    [("busy", 409, "middle of a conversation"), ("muted", 409, "muted"), ("down", 503, "isn't running")],
)
def test_when_tars_cant_record_the_page_says_why(log, why, status, says):
    token = Sessions(log.store).create(VOICE, "Alon", voice_script()[:1])
    client, _ = page(log, FakeAssistant(why=why))
    r = client.post(f"/api/session/{token}/record", json={"prompt": "v1"})
    assert r.status_code == status and says in r.json()["detail"]


def test_a_link_only_reaches_its_own_session(log):
    sessions = Sessions(log.store)
    mine, theirs = (sessions.create(VOICE, who, voice_script()[:1]) for who in ("Alon", "Stacey"))
    client, _ = page(log)
    rec = client.post(f"/api/session/{theirs}/record", json={"prompt": "v1"}).json()
    assert client.get(f"/api/session/{mine}/record/{rec['id']}").status_code == 404
    assert client.post(f"/api/session/{mine}/record", json={"prompt": "nope"}).status_code == 404
    assert client.get("/api/session/made-up").status_code == 404
