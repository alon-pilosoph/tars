"""A throwaway demo of the self-learning web UI with every kind of event and voice.

    uv run python tools/webui_demo.py build /tmp/tars_demo      # make the demo log: wakes, voices, conversations, sent items
    uv run python tools/webui_demo.py serve /tmp/tars_demo 8099  # serve it, with a trained pair of models to switch back from
    uv run python tools/webui_demo.py serve /tmp/tars_empty 8098 # an empty one, for the empty states

Its audio is tools/demo_audio/ (synthetic voices, committed), so it builds the same on any machine.
"""

import argparse
import json
import shutil
import struct
import time
import zlib
from pathlib import Path

import numpy as np
import soundfile as sf

from voice_assistant.__main__ import installed_models
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
from voice_assistant.versions import ABOUT, CHECK, MODEL, ModelVersions
from voice_assistant.webui import serve

REPO = Path(__file__).parents[1]
AUDIO = REPO / "tools" / "demo_audio"  # made by make_demo_audio.py: Alon, Stacey, a guest and the TV


def clip(speaker: str, kind: str, i: int) -> np.ndarray:
    audio, _sr = sf.read(AUDIO / speaker / f"{kind}_{i:02d}.flac", dtype="int16")
    return audio


def build(folder: Path) -> None:
    cfg = load_config(REPO / "config.toml")
    # Its own voiceprints, of the demo's Alon: speaker ID gives the same guesses on any machine.
    sid = SpeakerID(REPO / cfg.speaker.model, folder / "voiceprints.npz", cfg.speaker.threshold)
    sid.enroll("alon", [clip("alon", "say", i) for i in range(6, 12)])
    log = EventLog(folder / "events")
    now = time.time()
    trained_pair(cfg, folder)

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
            wake(clip("alon", "wake", i), 0.92, "answer", "hey tars", 0.97, 400 - i * 45),
            clip("alon", "say", i),
            text,
            ASKED,
        )
    stacey = [clip("stacey", "say", i) for i in range(4)]
    for i, audio in enumerate(stacey[:3]):  # another household member
        request(
            wake(clip("stacey", "wake", i), 0.88, "answer", "hey tars", 0.9, 330 - i * 50),
            audio,
            ["Play some jazz.", "What's on my calendar today?", "Add milk to the shopping list."][i],
            ASKED,
        )
    guest = [clip("guest", "say", i) for i in range(2)]
    for i, audio in enumerate(guest):  # a guest, not named
        request(
            wake(clip("guest", "wake", i), 0.81, "answer", "hey darts", 0.74, 250 - i * 30),
            audio,
            ["Is it going to rain?", "What time is it in Tokyo?"][i],
            ASKED,
        )
    tv = [clip("tv", "say", i) for i in range(2)]
    for i, audio in enumerate(tv):  # the TV: woke it, and the "request" was TV audio
        request(
            wake(audio[:48000], 0.63, "answer", "hey tars", 0.41, 200 - i * 20),
            audio[48000:112000],
            ["and in tonight's top story the storm is moving east", "you won't believe what happened next"][i],
            NOT_FOR_US,
        )
    log.set_follow(
        wake(clip("alon", "wake", 6), 0.86, "answer", "hey tars", 0.93, 150), SAID_NOTHING
    )  # "Yes, Alon?" then silence
    request(
        wake(clip("alon", "lookalike", 0), 0.71, "ask", "hey cars", 0.18, 120),
        clip("alon", "say", 7),
        "Yes, set an alarm for seven.",
        ASKED,
    )  # asked "did you call me?" and they answered
    log.set_follow(wake(clip("alon", "lookalike", 1), 0.66, "ask", "hey bars", 0.12, 100), SAID_NOTHING)
    request(
        wake(clip("alon", "lookalike", 2), 0.64, "ask", "hey mars", 0.1, 90), clip("alon", "say", 8), "No.", NOT_FOR_US
    )
    wake(clip("alon", "lookalike", 3), 0.58, "ignore", "hey stars", 0.04, 80)
    wake(tv[0][:48000], 0.55, "ignore", "[unk] stars", 0.02, 70)
    relabeled = wake(
        clip("alon", "wake", 9), 0.73, "ignore", "bars", 0.16, 60
    )  # it WAS hey TARS: the check got it wrong
    log.add_near_miss(clip("alon", "wake", 7), 0.41, "models/generic/hey_tars.tflite", ts=now - 40 * 60)
    request(
        wake(clip("alon", "wake", 8), 0.9, "answer", "hey tars", 0.95, 40 - 0.05),
        clip("alon", "say", 6),
        "Turn off the lights.",
        ASKED,
    )  # said it again, louder: the near-miss above was a missed wake
    log.add_near_miss(clip("alon", "lookalike", 4), 0.36, "models/generic/hey_tars.tflite", ts=now - 20 * 60)
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
    speech = [clip("alon", "say", i) for i in range(12)]

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
                audio = extra.get("audio", speech[(i + 30) % len(speech)])
                tid = convos.add_person_turn(c, text, audio, extra.get("speaker", speaker), 0.8, ts=t)
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
            person("Yes, twice. She's lying.", aside=True, audio=stacey[3]),
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


def demo_versions(cfg, folder: Path) -> ModelVersions:
    return ModelVersions(
        folder / "events", REPO, cfg.wake.model, cfg.wake.check_model, cfg.wake.threshold, cfg.wake.check_window_s
    )


def trained_pair(cfg, folder: Path) -> None:
    """A second version, as training.household would install it (the installed files, with made-up results), so
    the Models page has a history and something to switch back to."""
    pair = folder / "trained_pair"
    pair.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / cfg.wake.model, pair / MODEL)
    shutil.copyfile(REPO / cfg.wake.check_model, pair / CHECK)
    low = {"lower_is_better": True}
    (pair / ABOUT).write_text(
        json.dumps(
            {
                "threshold": cfg.wake.threshold,
                "check_window_s": cfg.wake.check_window_s,
                "note": "Trained on 41 of your wakes (34 real, 7 not) and 6 missed ones.",
                "results": [
                    {"name": "Your held-out hey TARS", "current": "9 of 12", "candidate": "11 of 12"},
                    {
                        "name": "Your held-out wakes that weren't for TARS, let through",
                        "current": "1 of 3",
                        "candidate": "0 of 3",
                        **low,
                    },
                    {"name": "Other voices, quiet", "current": "94.4%", "candidate": "95.6%"},
                    {"name": "Other voices, TV and chatter", "current": "80.6%", "candidate": "80.2%"},
                    {"name": "Lookalikes let through", "current": "1.8%", "candidate": "1.8%", **low},
                    {"name": "False answers per hour, TV", "current": "0.0", "candidate": "0.0", **low},
                    {"name": "False answers per hour, audiobooks", "current": "0.0", "candidate": "0.0", **low},
                ],
            }
        )
    )
    demo_versions(cfg, folder).install(pair)


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
    cfg = load_config(REPO / "config.toml")
    serve(
        log,
        "127.0.0.1",
        args.port,
        recluster=lambda: recluster(log),
        versions=demo_versions(cfg, args.folder),
        installed=installed_models(cfg),
    )


if __name__ == "__main__":
    main()
