"""Recording sessions: a link TARS sends, whose page asks for one phrase at a time and records it on TARS's own
speakerphone, or, for a wake test, on the device the page is open on (record/Record.tsx in the web UI,
docs/web-ui.md#recording-sessions).

A session is a script of prompts (the exact words, and how to say them) for one person, and a token: the link's only
key, good for LINK_HOURS (a wake test, WAKE_LINK_HOURS) or until the person finishes it. The assistant does the
recording, since it holds the speakerphone (Assistant.take, over control.py); the web UI keeps each take here, after
`check` has listened for what would make it worth doing again. Three scripts: the person's voice (for speaker ID),
the house's commands (for measuring house commands on the Pi, tools/commands_bench.py), and "hey TARS" in the places
and noise it's said in (a wake test, for measuring wake models on real mics, docs/wake-word.md). A wake test made for
a device (--device) is recorded by the page itself, on that device's microphone, and uploaded; its takes only ever
test a model, never train one. A house session's takes stay on the Pi unless it was made with --cloud, which its page
says before it starts: then the bench may send them to Deepgram too.
"""

import json
import random
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

import numpy as np

from .audio import SAMPLE_RATE
from .store import Store

if TYPE_CHECKING:
    from .commands import DeviceWords

VOICE, HOUSE, WAKE = "voice", "house", "wake"
KINDS = (VOICE, HOUSE, WAKE)
# Where a session's takes are recorded: TARS's speakerphone, or the device the page is open on (a wake test only).
ON_TARS, ON_DEVICE = "tars", "here"
LINK_HOURS = 24
WAKE_LINK_HOURS = 24 * 7  # a wake test is done in parts, as the TV and other people are around
MIN_ENROLL_TAKES = 5  # good takes a voice session needs before it becomes a voiceprint, as --enroll does
SECONDS_A_PROMPT = 15  # reading, the beep, saying it, hearing it back: for "about N minutes"

# How a take ended (control.py's answer to stop; the page's own, for a take recorded on its device).
SILENCE, TAP, NO_SPEECH = "silence", "tap", "nobody"
STOPPED = (SILENCE, TAP, NO_SPEECH)
# What `check` can find wrong with a take (types.ts Problem).
NOTHING, QUIET, LOUD, CUT_OFF, SHORT = "nothing", "quiet", "loud", "cut_off", "short"

HOUSE_PURPOSE = "To measure TARS doing the house's commands on the Pi, and hearing them offline, before it's turned on."
ABOUT = {
    VOICE: (
        "Your voice",
        "So TARS knows it's you when you talk to it, and can call you by name.",
        "TARS has learned your voice from these. It will know you from your next request.",
    ),
    HOUSE: (
        "House commands",
        f"{HOUSE_PURPOSE} Your recordings stay on the Pi: none of them is sent anywhere.",
        "They're kept on the Pi, and played back to TARS there to check it never mistakes one for another.",
    ),
    WAKE: (
        "Hey TARS",
        (
            "To measure how well TARS hears its name on real microphones, in real rooms. Your recordings stay on TARS "
            "and are only used to test it: none of them is sent anywhere, or used to train it on your voice."
        ),
        "They're kept on TARS, and played to its wake models to see which one hears its name best.",
    ),
}
# A house session made with --cloud: when its takes are measured (tools/commands_bench.py --cloud), each is also sent
# to Deepgram's speech to text, and the person is told so before they start. Without it, the bench never sends them.
ABOUT_CLOUD = {
    HOUSE: (
        "House commands",
        (
            f"{HOUSE_PURPOSE} Your recordings are kept on the Pi, and when they're measured each one is also sent to "
            "Deepgram, a speech to text service in the cloud, to compare how it hears them."
        ),
        (
            "They're kept on the Pi and played back to TARS there to check it never mistakes one for another; when "
            "they're measured, each one is also sent to Deepgram, in the cloud, to compare how it hears them."
        ),
    ),
}


def about(kind: str, cloud: bool = False) -> tuple[str, str, str]:
    """A session's title, what it's for and where its recordings go (said before it starts), and what's said once
    it's done. `cloud`: a house session whose takes are also sent to Deepgram when they're measured."""
    return ABOUT_CLOUD[kind] if cloud else ABOUT[kind]


@dataclass(frozen=True)
class Prompt:
    id: str
    say: str  # the exact words
    how: str = ""  # how to say them, or where from; "" = as you'd normally talk to TARS
    quiet: bool = False  # said quietly or from across the room: a quiet take is what's wanted
    # House commands: what the words ask for ("AC|set:23"), or "" for words that mustn't count as a command.
    # Wake tests: WAKE_PHRASE when TARS should wake to it, "" when it mustn't.
    means: str | None = None
    seconds: float = 0.0  # > 0: record this long, whatever is said (the room's sound, for false wakes)
    # > 0: after the tap, this many seconds (a chime each) to put the device down and walk away, then the beep
    countdown: float = 0.0


# ---------- Scripts ----------

FAR = "From across the room"
SOFTLY = "Quietly, as if someone's asleep"
FAST = "A bit faster than usual"

VOICE_SENTENCES = [
    "What's the weather going to be like tomorrow morning?",
    "Remind me to call my mom after dinner.",
    "I left my keys on the kitchen table again.",
    "We should leave by half past seven to beat the traffic.",
    "Turn the music down a little, please.",
    "How many tablespoons are in a quarter cup?",
    "I think the meeting got moved to Thursday afternoon.",
    "Could you read that last message back to me?",
    "Let's order pizza tonight, I'm too tired to cook.",
    "Tell me something interesting about black holes.",
    "There's a strange noise coming from the garage.",
    "Thanks, that's all I needed.",
]


def voice_script() -> list[Prompt]:
    """Plain talking, in their normal voice, close to the mic: a voiceprint of how they sound, with nothing else in
    it (the owner's call, 2026-11-19: no TV, nothing from afar or quiet)."""
    return [Prompt(f"v{n}", say, "") for n, say in enumerate(VOICE_SENTENCES, 1)]


# How people ask for each thing a device does: {n} is its name, {t} a temperature.
PHRASINGS = {
    "on": ["turn on the {n}", "turn the {n} on", "switch on the {n}", "put the {n} on"],
    "off": ["turn off the {n}", "turn the {n} off", "switch off the {n}", "{n} off"],
    "lock": ["lock the {n}", "lock the {n} please"],
    "unlock": ["unlock the {n}", "open the {n} lock"],
    "locked": ["is the {n} locked", "did I lock the {n}"],
    "set": ["set the {n} to {t}", "{n} to {t} degrees", "make the {n} {t} degrees"],
    "open": ["open the {n}", "raise the {n}"],
    "close": ["close the {n}", "lower the {n}"],
}
OPENINGS = ["", "", "hey TARS, ", "can you ", "please "]
# Words that name a device and an action but ask for neither: the ones a wrong match would come from.
NOT_COMMANDS = [
    "remind me to {c} at eleven",
    "I'll {c} myself later",
    "don't {c} yet",
]
# Plain requests, which go on to the model: for how long they take when nothing is sent until the turn is over.
CHATTER = [
    "what's the weather like tomorrow",
    "how long is left on the timer",
    "tell me a joke about robots",
    "remind me to call my mother at six",
    "what's on my shopping list",
    "who's home",
]
HOUSE_HOWS = ["", FAR, "", SOFTLY, FAST, ""]
MOST_COMMANDS = 36


WAKE_PHRASE = "hey tars"
TV = "With the TV or music on, as loud as you'd usually have it"
TALKING = "While other people are talking nearby"
IN_ONE_BREATH = "In one breath, as you'd ask"
WAKE_REQUESTS = [
    "Hey TARS, what's the weather tomorrow?",
    "Hey TARS, turn off the lights.",
    "Hey TARS, set a timer for ten minutes.",
    "Hey TARS, what's new?",
    "Hey TARS, play some music.",
    "Hey TARS, is the front door locked?",
]
# What a wake model mistakes for "hey TARS": near misses TARS's double-check has turned down on the Pi.
LOOKALIKES = ["Hey cars", "Hey stars", "Guitars", "Hey Lars", "Hey there", "Hey Tara", "Hey darts", "Hey Mars"]
LOOKALIKE_HOW = "As written: it sounds like TARS, but isn't"
ROOM_SECONDS = 60.0
WALK_AWAY_S = 4.0  # a device recording "from across the room" counts down this long, so it can be put down first
ROOM_TAKES = 3
ROOM_SAY = "Just the room, for a minute"
ROOM_HOW = "With the TV on, and nobody saying \u201chey TARS\u201d"


def wake_script(on_device: bool) -> list[Prompt]:
    """ "Hey TARS" as it's really said, in an order that needs the TV on once and other people only at the end:
    close, run into a request, lookalikes it mustn't wake to, from afar, softly, over the TV and over other people.
    On a device (not TARS's speakerphone), also a few minutes of the room with the TV on, for how often a model wakes
    to nothing."""
    steps = [
        *[("w", "Hey TARS", "", WAKE_PHRASE)] * 6,
        *[("r", words, IN_ONE_BREATH, WAKE_PHRASE) for words in WAKE_REQUESTS],
        *[("l", words, LOOKALIKE_HOW, "") for words in LOOKALIKES],
        *[("w", "Hey TARS", FAR, WAKE_PHRASE)] * 6,
        *[("w", "Hey TARS", SOFTLY, WAKE_PHRASE)] * 5,
        *[("w", "Hey TARS", TV, WAKE_PHRASE)] * 6,
        *[("b", ROOM_SAY, ROOM_HOW, "")] * (ROOM_TAKES if on_device else 0),
        *[("w", "Hey TARS", TALKING, WAKE_PHRASE)] * 4,
    ]
    counts: dict[str, int] = {}
    prompts = []
    for prefix, say, how, means in steps:
        counts[prefix] = counts.get(prefix, 0) + 1
        seconds = ROOM_SECONDS if prefix == "b" else 0.0
        countdown = WALK_AWAY_S if on_device and how == FAR else 0.0
        quiet = how in (FAR, SOFTLY)
        prompts.append(Prompt(f"{prefix}{counts[prefix]}", say, how, quiet, means, seconds, countdown))
    return prompts


def minutes(prompts: list[Prompt]) -> int:
    """About how long a session takes: a prompt's reading, beep and hearing it back, or its set time."""
    return max(1, round(sum(max(SECONDS_A_PROMPT, p.seconds + 5) for p in prompts) / 60))


def house_script(devices: list[DeviceWords], rng: random.Random) -> list[Prompt]:
    """Each thing each device does, in one of the ways people ask, from a few places and voices; then words that
    name devices without asking for anything, which must not count, and plain requests. `devices`: as the house made
    them (commands.device_words), each said by its label. At most MOST_COMMANDS commands."""
    commands: list[tuple[str, str, str]] = []  # (how it opens, the words, what they mean)
    for _, name, actions, warm in devices:
        name = name.strip()
        if not name:
            continue
        said = name if name.isupper() else name.lower()  # "the AC", "the front door"
        asked = [a for a in ("on", "off", "lock", "unlock", "open", "close") if a in actions]
        asked += ["locked"] * ("lock" in actions) + ["set"] * (warm and "set" in actions)
        for action in asked:
            t = rng.choice([21, 22, 23, 24, 25]) if action == "set" else None
            words = rng.choice(PHRASINGS[action]).format(n=said, t=t)
            commands.append((rng.choice(OPENINGS), words, f"{name}|{action}" + (f":{t}" if t else "")))
    rng.shuffle(commands)
    commands = commands[:MOST_COMMANDS]
    prompts = []
    for n, (opening, words, means) in enumerate(commands):
        how = HOUSE_HOWS[n % len(HOUSE_HOWS)]
        prompts.append(Prompt(f"h{n + 1}", _sentence(opening + words), how, quiet=how in (FAR, SOFTLY), means=means))
    if commands:
        for n, template in enumerate(NOT_COMMANDS):
            prompts.append(Prompt(f"n{n + 1}", _sentence(template.format(c=rng.choice(commands)[1])), means=""))
    prompts += [Prompt(f"c{n + 1}", _sentence(words), means="") for n, words in enumerate(CHATTER)]
    return prompts


def _sentence(words: str) -> str:
    return words[:1].upper() + words[1:]


# ---------- Checking a take ----------

FRAME_S = 0.02
SPEECH_ABOVE_FLOOR_DB = 12.0  # a frame this much louder than the room is speech
SPEECH_AT_LEAST_DB = -50.0
QUIET_DB = -36.0  # the loud part of the speech, below this: too quiet to be useful (a first guess, to tune on takes)
QUIET_ANYWAY_DB = -46.0  # the same, for a prompt that asks for quiet or distance
CLIPPED = 32_000
CLIPPED_SHARE = 0.001
SHORTEST_S = 0.35
EDGE_S = 0.06  # speech this close to where the take starts, or (stopped by a tap) ends, was cut


def check(pcm: bytes | None, stopped: str, quiet_ok: bool = False, timed: bool = False) -> str | None:
    """What would make this take worth doing again, or None: nothing heard, clipped, cut off, too short or too
    quiet, in that order. Loudness is measured, not guessed: frames well above the room's own level are speech.
    `timed`: a take of the room for a set time, where nobody speaks: only nothing at all is wrong with it."""
    if timed:
        return NOTHING if not pcm or not np.frombuffer(pcm, dtype=np.int16).any() else None
    if not pcm or stopped == NO_SPEECH:
        return NOTHING
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    frame = int(FRAME_S * SAMPLE_RATE)
    n = len(x) // frame
    if n < 3:
        return NOTHING
    frames = x[: n * frame].reshape(n, frame)
    db = 20 * np.log10(np.sqrt((frames**2).mean(axis=1)) / 32768 + 1e-9)
    floor = float(np.percentile(db, 10))
    speech = np.flatnonzero(db > max(floor + SPEECH_ABOVE_FLOOR_DB, SPEECH_AT_LEAST_DB))
    if not len(speech):
        return NOTHING
    if (np.abs(x) >= CLIPPED).mean() > CLIPPED_SHARE:
        return LOUD
    edge = max(1, round(EDGE_S / FRAME_S))
    if speech[0] < edge or (stopped == TAP and speech[-1] >= n - edge):
        return CUT_OFF
    if (speech[-1] - speech[0] + 1) * FRAME_S < SHORTEST_S:
        return SHORT
    if float(np.percentile(db[speech], 90)) < (QUIET_ANYWAY_DB if quiet_ok else QUIET_DB):
        return QUIET
    return None


# ---------- Sessions ----------


class Sessions:
    """The sessions and their takes, in the event database (store.py's sessions and takes)."""

    def __init__(self, store: Store, clock: Callable[[], float] = time.time):
        self.store, self._clock = store, clock

    def create(self, kind: str, person: str, prompts: list[Prompt], cloud: bool = False, device: str = "") -> str:
        """`cloud`: a house session whose takes may also be sent to Deepgram when they're measured, as its page says;
        without it, they never leave the Pi. `device`: a wake test recorded on the device the page is opened on,
        by this name ("Alon's phone"); "" records on TARS's speakerphone."""
        if kind not in KINDS:
            raise ValueError(f"no such session: {kind!r} (one of {', '.join(KINDS)})")
        if cloud and kind not in ABOUT_CLOUD:
            raise ValueError(f"a {kind} session's takes never go to the cloud")
        if device and kind != WAKE:
            raise ValueError(f"a {kind} session is recorded on TARS's speakerphone")
        token = secrets.token_urlsafe(16)
        now = self._clock()
        self.store.write(
            "INSERT INTO sessions (token, kind, person, created, expires, prompts, cloud, mic, device) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token, kind, person, now, now + (WAKE_LINK_HOURS if kind == WAKE else LINK_HOURS) * 3600,
                json.dumps([asdict(p) for p in prompts]),
                int(cloud), ON_DEVICE if device else ON_TARS, device,
            ),
        )  # fmt: skip
        return token

    def open(self, token: str) -> dict | None:
        """The session the token is for, while its link works: not expired, not finished."""
        s = self.store.row("SELECT * FROM sessions WHERE token = ?", (token,))
        if not s or s["finished"] or s["expires"] < self._clock():
            return None
        return s

    def prompts(self, session: dict) -> list[Prompt]:
        return [Prompt(**p) for p in json.loads(session["prompts"])]

    def prompt(self, session: dict, prompt_id: str) -> Prompt | None:
        return next((p for p in self.prompts(session) if p.id == prompt_id), None)

    def takes(self, session_id: int) -> list[dict]:
        return self.store.rows("SELECT * FROM takes WHERE session_id = ? ORDER BY id", (session_id,))

    def take(self, session_id: int, take_id: int) -> dict | None:
        return self.store.row("SELECT * FROM takes WHERE id = ? AND session_id = ?", (take_id, session_id))

    def add_take(self, session: dict, prompt: Prompt, pcm: bytes | None, stopped: str) -> dict:
        """Kept, checked, and in place of the prompt's earlier take, whose audio goes with it. A prompt left for later
        isn't any more."""
        problem = check(pcm, stopped, prompt.quiet, timed=prompt.seconds > 0)
        seconds = len(pcm) / 2 / SAMPLE_RATE if pcm else 0.0
        name = f"take_{session['id']}_{prompt.id}_{secrets.token_hex(4)}"  # never the name of the take it replaces
        rel = self.store.save_audio(pcm, name) if pcm else None
        with self.store.removed_on_failure(rel), self.store.transaction() as db:
            old = [r["audio"] for r in db.execute(
                "SELECT audio FROM takes WHERE session_id = ? AND prompt = ?", (session["id"], prompt.id))]  # fmt: skip
            db.execute("DELETE FROM takes WHERE session_id = ? AND prompt = ?", (session["id"], prompt.id))
            later = json.loads(db.execute("SELECT skipped FROM sessions WHERE id = ?", (session["id"],)).fetchone()[0])
            if prompt.id in later:
                later.remove(prompt.id)
                db.execute("UPDATE sessions SET skipped = ? WHERE id = ?", (json.dumps(later), session["id"]))
            take_id = db.execute(
                "INSERT INTO takes (session_id, prompt, ts, audio, seconds, problem, stopped) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session["id"], prompt.id, self._clock(), rel, seconds, problem, stopped),
            ).lastrowid
        self.store.remove(old)
        return self.take(session["id"], take_id)

    def skip(self, session: dict, prompt_id: str) -> None:
        """Left for later: the page comes back to it before the end, and it can be finished without it."""
        with self.store.transaction() as db:
            row = db.execute("SELECT skipped FROM sessions WHERE id = ?", (session["id"],)).fetchone()
            skipped = json.loads(row["skipped"])
            if prompt_id not in skipped:
                db.execute(
                    "UPDATE sessions SET skipped = ? WHERE id = ?", (json.dumps([*skipped, prompt_id]), session["id"])
                )

    def done(self, session: dict) -> bool:
        """Every prompt has a take or was left for later."""
        row = self.store.row("SELECT skipped FROM sessions WHERE id = ?", (session["id"],))
        covered = {t["prompt"] for t in self.takes(session["id"])} | set(json.loads(row["skipped"]))
        return all(p.id in covered for p in self.prompts(session))

    def finish(self, session: dict) -> bool:
        """Closes the link once every prompt is covered and the person says they're done; True if this call did (so
        what follows runs once)."""
        if not self.done(session):
            return False
        with self.store.transaction() as db:
            return bool(db.execute(
                "UPDATE sessions SET finished = ? WHERE id = ? AND finished IS NULL", (self._clock(), session["id"])
            ).rowcount)  # fmt: skip

    def public(self, session: dict) -> dict:
        """What the page gets (types.ts Session): the words and how to say them, not what a command means."""
        title, said_first, after = about(session["kind"], bool(session["cloud"]))
        prompts = self.prompts(session)
        return {
            "kind": session["kind"],
            "title": title,
            "person": session["person"],
            "about": said_first,
            "after": after,
            "mic": session["mic"],
            "device": session["device"],
            "prompts": [
                {"id": p.id, "say": p.say, "how": p.how, "seconds": p.seconds, "countdown": p.countdown}
                for p in prompts
            ],
            "takes": [public_take(t) for t in self.takes(session["id"])],
            "skipped": json.loads(session["skipped"]),
            "minutes": minutes(prompts),
        }


def public_take(t: dict) -> dict:
    return {"id": t["id"], "prompt": t["prompt"], "seconds": t["seconds"], "problem": t["problem"]}
