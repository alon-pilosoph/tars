"""Recording sessions: a link TARS sends, whose page asks for one phrase at a time and records it on TARS's own
speakerphone (record/Record.tsx in the web UI, docs/web-ui.md#recording-sessions).

A session is a script of prompts (the exact words, and how to say them) for one person, and a token: the link's only
key, good for LINK_HOURS or until the last prompt is done. The assistant does the recording, since it holds the
speakerphone (Assistant.take, over control.py); the web UI keeps each take here, after `check` has listened for what
would make it worth doing again. Two scripts so far: the person's voice (for speaker ID), and the house's commands
(for testing offline recognizers on them, docs/roadmap.md).
"""

import json
import random
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

import numpy as np

from .audio import SAMPLE_RATE
from .store import Store

VOICE, HOUSE = "voice", "house"
KINDS = (VOICE, HOUSE)
LINK_HOURS = 24
MIN_ENROLL_TAKES = 5  # good takes a voice session needs before it becomes a voiceprint, as --enroll does
SECONDS_A_PROMPT = 15  # reading, the beep, saying it, hearing it back: for "about N minutes"

# How a take ended (control.py's answer to stop).
SILENCE, TAP, NO_SPEECH = "silence", "tap", "nobody"
# What `check` can find wrong with a take (types.ts Problem).
NOTHING, QUIET, LOUD, CUT_OFF, SHORT = "nothing", "quiet", "loud", "cut_off", "short"

ABOUT = {
    VOICE: (
        "Your voice",
        "So TARS knows it's you when you talk to it, and can call you by name.",
        "TARS has learned your voice from these. It will know you from your next request.",
    ),
    HOUSE: (
        "House commands",
        "To measure TARS doing the house's commands on the Pi, and hearing them offline, before it's turned on.",
        "They're kept on the Pi, and played back to TARS to check it never mistakes one for another.",
    ),
}


@dataclass(frozen=True)
class Prompt:
    id: str
    say: str  # the exact words
    how: str = ""  # how to say them, or where from; "" = as you'd normally talk to TARS
    quiet: bool = False  # said quietly or from across the room: a quiet take is what's wanted
    # House commands: what the words ask for ("AC|set:23"), or "" for words that mustn't count as a command.
    means: str | None = None


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
# The voiceprint should know you from where you really talk to TARS, so a few are from afar and a few quiet.
VOICE_HOWS = ["", "", "", "", "", "", "", FAR, FAR, SOFTLY, "", SOFTLY]


def voice_script() -> list[Prompt]:
    return [
        Prompt(f"v{n}", say, how, quiet=how in (FAR, SOFTLY))
        for n, (say, how) in enumerate(zip(VOICE_SENTENCES, VOICE_HOWS, strict=True), 1)
    ]


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


def house_script(devices: list[tuple[str, tuple[str, ...], bool]], rng: random.Random) -> list[Prompt]:
    """Each thing each device does, in one of the ways people ask, from a few places and voices; then words that
    name devices without asking for anything, which must not count, and plain requests. `devices`: each one's name,
    what it does (Device.actions) and whether it takes a temperature, as the house made them. At most MOST_COMMANDS
    commands."""
    commands: list[tuple[str, str, str]] = []  # (how it opens, the words, what they mean)
    for name, actions, warm in devices:
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


def check(pcm: bytes | None, stopped: str, quiet_ok: bool = False) -> str | None:
    """What would make this take worth doing again, or None: nothing heard, clipped, cut off, too short or too
    quiet, in that order. Loudness is measured, not guessed: frames well above the room's own level are speech."""
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

    def create(self, kind: str, person: str, prompts: list[Prompt]) -> str:
        if kind not in KINDS:
            raise ValueError(f"no such session: {kind!r} (one of {', '.join(KINDS)})")
        token = secrets.token_urlsafe(16)
        now = self._clock()
        self.store.write(
            "INSERT INTO sessions (token, kind, person, created, expires, prompts) VALUES (?, ?, ?, ?, ?, ?)",
            (token, kind, person, now, now + LINK_HOURS * 3600, json.dumps([asdict(p) for p in prompts])),
        )
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
        """Kept, checked, and in place of the prompt's earlier take, whose audio goes with it."""
        problem = check(pcm, stopped, prompt.quiet)
        seconds = len(pcm) / 2 / SAMPLE_RATE if pcm else 0.0
        name = f"take_{session['id']}_{prompt.id}_{secrets.token_hex(4)}"  # never the name of the take it replaces
        rel = self.store.save_audio(pcm, name) if pcm else None
        with self.store.removed_on_failure(rel), self.store.transaction() as db:
            old = [r["audio"] for r in db.execute(
                "SELECT audio FROM takes WHERE session_id = ? AND prompt = ?", (session["id"], prompt.id))]  # fmt: skip
            db.execute("DELETE FROM takes WHERE session_id = ? AND prompt = ?", (session["id"], prompt.id))
            take_id = db.execute(
                "INSERT INTO takes (session_id, prompt, ts, audio, seconds, problem, stopped) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session["id"], prompt.id, self._clock(), rel, seconds, problem, stopped),
            ).lastrowid
        self.store.remove(old)
        return self.take(session["id"], take_id)

    def skip(self, session: dict, prompt_id: str) -> None:
        with self.store.transaction() as db:
            row = db.execute("SELECT skipped FROM sessions WHERE id = ?", (session["id"],)).fetchone()
            skipped = json.loads(row["skipped"])
            if prompt_id not in skipped:
                db.execute(
                    "UPDATE sessions SET skipped = ? WHERE id = ?", (json.dumps([*skipped, prompt_id]), session["id"])
                )

    def done(self, session: dict) -> bool:
        """Every prompt has a take or was skipped."""
        row = self.store.row("SELECT skipped FROM sessions WHERE id = ?", (session["id"],))
        covered = {t["prompt"] for t in self.takes(session["id"])} | set(json.loads(row["skipped"]))
        return all(p.id in covered for p in self.prompts(session))

    def finish(self, session: dict) -> bool:
        """Closes the link once every prompt is covered; True if this call did (so what follows runs once)."""
        if not self.done(session):
            return False
        with self.store.transaction() as db:
            return bool(db.execute(
                "UPDATE sessions SET finished = ? WHERE id = ? AND finished IS NULL", (self._clock(), session["id"])
            ).rowcount)  # fmt: skip

    def public(self, session: dict) -> dict:
        """What the page gets (types.ts Session): the words and how to say them, not what a command means."""
        title, about, after = ABOUT[session["kind"]]
        prompts = self.prompts(session)
        return {
            "kind": session["kind"],
            "title": title,
            "person": session["person"],
            "about": about,
            "after": after,
            "prompts": [{"id": p.id, "say": p.say, "how": p.how} for p in prompts],
            "takes": [public_take(t) for t in self.takes(session["id"])],
            "skipped": json.loads(session["skipped"]),
            "minutes": max(1, round(len(prompts) * SECONDS_A_PROMPT / 60)),
        }


def public_take(t: dict) -> dict:
    return {"id": t["id"], "prompt": t["prompt"], "seconds": t["seconds"], "problem": t["problem"]}
