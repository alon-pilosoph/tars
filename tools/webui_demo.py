"""A throwaway demo of the self-learning web UI with every kind of event, voice and model state.

    uv run python tools/webui_demo.py build /tmp/tars_demo      # make the demo log: wakes, voices, conversations, sent items
    uv run python tools/webui_demo.py serve /tmp/tars_demo 8099  # serve it (with mock model history)
    uv run python tools/webui_demo.py serve /tmp/tars_empty 8098 # an empty one, for the empty states

Uses the user's recordings (voice_data/) and held-out test audio (~/wakeword_bench); nothing is committed.
"""

import argparse
import struct
import time
import zlib
from pathlib import Path

import numpy as np
import soundfile as sf

from voice_assistant.clustering import recluster
from voice_assistant.config import load_config
from voice_assistant.conversations import (
    FILE,
    HOUSEHOLD,
    LINK,
    LIST,
    NOTE,
    ConversationLog,
    SentItem,
)
from voice_assistant.events import (
    ASKED,
    NOT_FOR_US,
    NOT_REAL,
    REAL,
    SAID_NOTHING,
    EventLog,
)
from voice_assistant.speaker import SpeakerID
from voice_assistant.webui import serve

REPO = Path(__file__).parents[1]
U = REPO / "voice_data/alon/laptop"
BENCH = Path.home() / "wakeword_bench"
LIBRI = BENCH / "LibriSpeech/test-clean"


def wav(path: Path) -> np.ndarray:
    audio, _sr = sf.read(path, dtype="int16")
    return audio if audio.ndim == 1 else audio[:, 0]


def libri(speaker: str, n: int) -> list[np.ndarray]:
    return [wav(f) for f in sorted((LIBRI / speaker).glob("*/*.flac"))[:n]]


def build(folder: Path) -> None:
    cfg = load_config(REPO / "config.toml")
    sid = SpeakerID(REPO / cfg.speaker.model, REPO / cfg.speaker.voiceprints, cfg.speaker.threshold)
    log = EventLog(folder / "events")
    now = time.time()

    def wake(audio, score, outcome, heard, conf, minutes_ago):
        return log.add_wake(
            audio,
            score,
            outcome,
            heard,
            conf,
            "models/generic/hey_tars.tflite",
            "models/generic/hey_tars_check.json",
            ts=now - minutes_ago * 60,
        )

    def request(e, audio, text, follow):
        name, score, emb = sid.describe(audio.tobytes())
        log.add_request(e, audio, text, name, score, emb)
        log.set_follow(e, follow)

    alon_requests = [
        "What's the weather going to be like tomorrow?",
        "Set a timer for ten minutes.",
        "Remind me to call my mom after dinner.",
        "How many tablespoons in a quarter cup?",
        "Turn the music down a little.",
        "Tell me something about black holes.",
    ]
    for i, text in enumerate(alon_requests):  # answered, a request followed (Alon)
        request(
            wake(wav(U / f"hey_tars/{i:03d}.wav"), 0.92, "answer", "hey tars", 0.97, 400 - i * 45),
            wav(U / f"speech/{i:03d}.wav"),
            text,
            ASKED,
        )
    stacey = libri("121", 3)
    for i, audio in enumerate(stacey):  # another household member
        request(
            wake(
                wav(BENCH / f"clips_heldout/hey_tars/coral_0{i}_1.wav"), 0.88, "answer", "hey tars", 0.9, 330 - i * 50
            ),
            audio,
            ["Play some jazz.", "What's on my calendar today?", "Add milk to the shopping list."][i],
            ASKED,
        )
    guest = libri("237", 2)
    for i, audio in enumerate(guest):  # a guest, not named
        request(
            wake(
                wav(BENCH / f"clips_heldout/hey_tars/sage_0{i}_1.wav"), 0.81, "answer", "hey darts", 0.74, 250 - i * 30
            ),
            audio,
            ["Is it going to rain?", "What time is it in Tokyo?"][i],
            ASKED,
        )
    tv = sorted((BENCH / "interference/test/tv").glob("*.wav"))[:2]
    for i, f in enumerate(tv):  # the TV: woke it, and the "request" was TV audio
        request(
            wake(wav(f)[:48000], 0.63, "answer", "hey tars", 0.41, 200 - i * 20),
            wav(f)[48000:112000],
            ["and in tonight's top story the storm is moving east", "you won't believe what happened next"][i],
            NOT_FOR_US,
        )
    log.set_follow(
        wake(wav(U / "hey_tars/007.wav"), 0.86, "answer", "hey tars", 0.93, 150), SAID_NOTHING
    )  # "Yes, Alon?" then silence
    request(
        wake(wav(U / "hey_tars_lookalikes/000.wav"), 0.71, "ask", "hey cars", 0.18, 120),
        wav(U / "speech/020.wav"),
        "Yes, set an alarm for seven.",
        ASKED,
    )  # asked "did you call me?" and they answered
    log.set_follow(wake(wav(U / "hey_tars_lookalikes/004.wav"), 0.66, "ask", "hey bars", 0.12, 100), SAID_NOTHING)
    request(
        wake(wav(U / "hey_tars_lookalikes/007.wav"), 0.64, "ask", "hey mars", 0.1, 90),
        wav(U / "speech/024.wav"),
        "No.",
        NOT_FOR_US,
    )
    wake(wav(U / "hey_tars_lookalikes/010.wav"), 0.58, "ignore", "hey stars", 0.04, 80)
    wake(wav(tv[0])[:48000], 0.55, "ignore", "[unk] stars", 0.02, 70)
    relabeled = wake(
        wav(U / "hey_tars/023.wav"), 0.73, "ignore", "bars", 0.16, 60
    )  # it WAS hey TARS: the check got it wrong
    log.add_near_miss(wav(U / "hey_tars/019.wav"), 0.41, "models/generic/hey_tars.tflite", ts=now - 40 * 60)
    request(
        wake(wav(U / "hey_tars/020.wav"), 0.9, "answer", "hey tars", 0.95, 40 - 0.05),
        wav(U / "speech/010.wav"),
        "Turn off the lights.",
        ASKED,
    )  # said it again, louder: the near-miss above was a missed wake
    log.add_near_miss(wav(U / "hey_tars_lookalikes/013.wav"), 0.36, "models/generic/hey_tars.tflite", ts=now - 20 * 60)
    recluster(log)
    rows = sorted(log.events(limit=1000), key=lambda r: r["ts"])
    by_text = {r["transcript"]: r for r in rows if r["transcript"]}
    alon = by_text[alon_requests[0]]["cluster_id"]
    log.rename_cluster(alon, "Alon")
    log.rename_cluster(by_text["Play some jazz."]["cluster_id"], "Stacey")
    tv_cluster = by_text["and in tonight's top story the storm is moving east"]["cluster_id"]
    log.rename_cluster(tv_cluster, None, kind="not_person")
    log.assign(by_text["Turn off the lights."]["id"], alon, pinned=True)  # moved by hand
    for r in rows[:8]:  # the older ones are reviewed; the recent ones are waiting
        log.set_label(r["id"], REAL if r["auto_label"] != NOT_REAL else NOT_REAL)
    log.set_label(relabeled, REAL)
    n = add_conversations(log, by_text, stacey)
    print(f"demo: {len(log.events())} events, {len(log.clusters())} voices, {n} conversations in {folder}")


def png(w: int, h: int) -> bytes:
    """A small made-up photo (a sunset-ish gradient), for the image preview."""
    y, x = np.mgrid[0:h, 0:w]
    rgb = np.stack([200 + 55 * y / h, 120 + 80 * (1 - y / h), 90 + 60 * x / w], -1).astype(np.uint8)
    raw = b"".join(b"\0" + rgb[r].tobytes() for r in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def add_conversations(log: EventLog, by_text: dict, stacey: list) -> int:
    """Conversations on top of the demo wakes: follow-ups, an aside, "Did you call me?", an unknown voice,
    and one of each kind of sent item. Times follow each wake."""
    convos = ConversationLog(log)
    speech = sorted((U / "speech").glob("*.wav"))

    def talk(request: str, turns: list, speaker: str | None, before: str | None = None) -> int:
        """`turns`: after the first request, (role, text, extra) in order: TARS's reply, then person / tars pairs."""
        ev = by_text[request]
        c = convos.start(ev["id"], ts=ev["ts"])
        t = ev["ts"]
        if before:
            convos.add_tars_turn(c, before, ts=t)
            t += 2
        convos.add_person_turn(c, request, speaker=speaker, speaker_score=0.8, audio=ev["utterance_audio"], ts=t)
        for i, (role, text, extra) in enumerate(turns):
            t += 4
            if role == "person":
                clip = extra.get("audio", wav(speech[(i + 30) % len(speech)]))
                tid = convos.add_person_turn(c, text, clip, extra.get("speaker", speaker), 0.8, ts=t)
                if extra.get("aside"):
                    convos.mark_not_for_tars(tid)
                if extra.get("corrected"):
                    convos.correct(tid, extra["corrected"])
            else:
                tid = convos.add_tars_turn(c, text, ts=t)
                if extra.get("rating"):
                    convos.rate(tid, extra["rating"])
                for sent in extra.get("items", []):
                    sent = dict(sent)
                    seen, for_name = sent.pop("seen", True), sent.pop("for_name", None)
                    convos.mark_seen(convos.add_item(c, tid, SentItem(**sent), for_name=for_name, ts=t), seen)
        convos.end(c, ts=t + 4)
        return c

    def tars(text, **extra):
        return "tars", text, extra

    def person(text, **extra):
        return "person", text, extra

    talk(
        "What's the weather going to be like tomorrow?",
        [tars("Sunny, 24 degrees. Sunglasses advised. Irony optional.", rating="good")],
        "alon",
    )
    talk(
        "Set a timer for ten minutes.",
        [
            tars("Ten minutes. Starting now."),
            person("Actually, make it fifteen."),
            tars("Fifteen minutes. I've adjusted my expectations too."),
            person("And remind me to take the pasta out when it's done."),
            tars("I'll remind you. The pasta has no say in it."),
        ],
        "alon",
    )
    talk(
        "Remind me to call my mom after dinner.",
        [tars("Reminder set for eight. I'll be subtle. I won't.", rating="bad")],
        "alon",
    )
    talk(
        "Add milk to the shopping list.",
        [
            tars(
                "Added. The list is on the TARS page.",
                items=[
                    {
                        "kind": LIST,
                        "title": "Shopping",
                        "scope": HOUSEHOLD,
                        "entries": ["Milk", "Eggs", "Sourdough", "Tomatoes", "Basil"],
                        "seen": False,
                    }
                ],
            ),
            person("Did you feed the cat?", aside=True, speaker="alon"),
            person("Yes, twice. She's lying.", aside=True, audio=stacey[1] if len(stacey) > 1 else stacey[0]),
        ],
        "stacey",
    )
    talk("Play some jazz.", [tars("Playing jazz. Try to look sophisticated.")], "stacey")
    talk(
        "Is it going to rain?",
        [
            tars("No. Ten percent. I'd leave the umbrella."),
            person("And in Tokyo?"),
            tars("Tokyo, light rain all afternoon."),
        ],
        None,
    )
    talk(
        "Yes, set an alarm for seven.",
        [tars("Alarm set for seven. I'll be the loud one.")],
        "alon",
        before="Did you call me?",
    )
    talk("No.", [], "alon", before="Did you call me?")
    convos.mark_not_for_tars(convos.get(max(c["id"] for c in convos.conversations()))["turns"][1]["id"])
    talk(
        "Tell me something about black holes.",
        [
            tars("Nothing escapes them, not even light. Or a good excuse."),
            person("Send me something good to read about them."),
            tars(
                "Sent you two. They're on the TARS page.",
                items=[
                    {
                        "kind": LINK,
                        "title": "Black holes, explained",
                        "for_name": "alon",
                        "site": "NASA Science",
                        "url": "https://science.nasa.gov/universe/black-holes/",
                        "description": "What they are, how they form, and how we find them.",
                        "seen": False,
                    },
                    {
                        "kind": NOTE,
                        "title": "Black holes: the short version",
                        "for_name": "alon",
                        "seen": False,
                        "body": "**Event horizon**: the point of no return.\n\n- Stellar: 5–100 suns\n- Supermassive: "
                        "millions of suns, at the centre of most galaxies\n\nNearest known: *Gaia BH1*, 1,560 light years.",
                    },
                ],
            ),
        ],
        "alon",
    )
    talk(
        "How many tablespoons in a quarter cup?",
        [
            tars("Four."),
            person(
                "Great, and send me the pasta recipe from last Wednesday.",
                corrected="Great, and send me the pasta recipe from last week.",
            ),
            tars(
                "Sent. Food Network, Michael Symon's. It's on the TARS page.",
                items=[
                    {
                        "kind": LINK,
                        "title": "Pasta pomodoro",
                        "for_name": "alon",
                        "site": "Food Network",
                        "url": "https://www.foodnetwork.com/fnk/recipes/pasta-pomodoro-8290550",
                        "description": "Twenty minutes, five ingredients.",
                    }
                ],
            ),
        ],
        "alon",
    )
    talk(
        "Turn off the lights.",
        [
            tars("Lights off. Darkness suits me."),
            person("Make me a packing list for the beach this weekend and send it."),
            tars(
                "Done. It's on the TARS page.",
                items=[
                    {
                        "kind": LIST,
                        "title": "Beach weekend",
                        "for_name": "alon",
                        "entries": ["Swimsuits", "Towels", "Sunscreen", "Hats", "Chargers", "Snacks", "Book"],
                        "seen": False,
                    }
                ],
            ),
            person("And the plan for Saturday as a file."),
            tars(
                "Sent the plan. Also on the TARS page.",
                items=[
                    {
                        "kind": FILE,
                        "title": "Saturday plan",
                        "for_name": "alon",
                        "file_name": "saturday-plan.md",
                        "mime": "text/markdown",
                        "file_bytes": b"# Saturday\n\n- 9:00 leave\n- 10:30 beach\n- 13:00 lunch at the kiosk\n- 17:00 home\n",
                        "seen": False,
                    },
                    {
                        "kind": FILE,
                        "title": "Beach, last summer",
                        "for_name": "alon",
                        "file_name": "beach.png",
                        "mime": "image/png",
                        "file_bytes": png(480, 320),
                        "seen": False,
                    },
                ],
            ),
        ],
        "alon",
    )
    lst = next(i for i in convos.items() if i["title"] == "Beach weekend")
    for k in (0, 2, 4):
        convos.tick(lst["id"], k, True)
    convos.mark_seen(lst["id"], False)  # ticked on another phone, still new here
    return len(convos.conversations())


def mock_models() -> dict:
    now = time.time()
    return {
        "active": {
            "wake_model": "models/generic/hey_tars.tflite",
            "threshold": 0.5,
            "check_model": "models/generic/hey_tars_check.json",
            "check_window_s": 3.0,
        },
        "history": [
            {
                "version": "check v2",
                "ts": now - 3600 * 5,
                "active": True,
                "note": "Generic check retrained with accented voices (VCTK, 50 languages).",
            },
            {
                "version": "check v1",
                "ts": now - 3600 * 30,
                "active": False,
                "note": "Generic check: synthetic voices + 250 converted LibriSpeech speakers.",
            },
            {
                "version": "plain",
                "ts": now - 3600 * 60,
                "active": False,
                "note": "Plain phrase match, no learned layer.",
            },
        ],
        "last_retrain": None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build", help="make a demo log").add_argument("folder", type=Path)
    serve_it = commands.add_parser("serve", help="serve a demo log")
    serve_it.add_argument("folder", type=Path)
    serve_it.add_argument("port", type=int)
    args = parser.parse_args()
    if args.command == "build":
        build(args.folder)
        return
    log = EventLog(args.folder / "events")
    empty = not log.events(limit=1)  # an empty demo shows the "just started" model state too
    info = (lambda: {**mock_models(), "history": []}) if empty else mock_models
    serve(log, "127.0.0.1", args.port, recluster=lambda: recluster(log), models_info=info)


if __name__ == "__main__":
    main()
