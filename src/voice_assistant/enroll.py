"""Guided recording of one person's voice.

The clips train and test the wake words ("hey TARS", "TARS stop", and lookalikes that must not wake it)
and enroll speaker identification (the read-aloud sentences).

The session runs in three batches, so you only move or change the room twice:
close to the mic, then from across the room, then with the TV or music on.
"""

import time
from dataclasses import dataclass
from pathlib import Path

from .audio import BLOCK_SECONDS, SAMPLE_RATE, Microphone, Speaker, save_wav
from .recorder import UtteranceRecorder

# The verifier runs the wake-word model over each clip, and the model only scores high once it has
# heard a little past the end of the phrase, so keep about a second of room audio on both sides.
PAD_BLOCKS = round(1.0 / BLOCK_SECONDS)

CLOSE, FAR, NOISY = "close", "far", "noisy"
BATCHES = {
    CLOSE: "Sit at your usual spot, close to the mic.",
    FAR: "Now move about 2-3 meters away from the mic (across the room is fine).",
    NOISY: "Now turn on the TV or some music at a normal listening volume, and come back to your usual spot.",
}
COUNTDOWN_BEEPS = 3

# (how to say it, batch). The order is fixed: a clip's file number comes from its position,
# and training splits takes by that number, so new variations only ever go at the end.
WAKE_VARIATIONS = [
    ("normally", CLOSE),
    ("", FAR),
    ("quickly, like you're busy", CLOSE),
    ("quietly", CLOSE),
    ("", NOISY),
]

# Everyday speech plus near-misses ("stars", "tar", "stop"), which make the best negatives for the verifier.
SENTENCES = [
    "What's the weather going to be like tomorrow morning?",
    "The stars are really bright tonight.",
    "Can you stop the car at the next corner?",
    "I left my keys on the kitchen table again.",
    "Hey, did anyone feed the cat today?",
    "The road crew spread fresh tar on the street.",
    "Remind me to call my mom after dinner.",
    "She bought three jars of strawberry jam.",
    "We should leave by half past seven to beat the traffic.",
    "Turn the music down a little, please.",
    "My favorite movie has a robot that tells jokes.",
    "The guitar was out of tune after the flight.",
    "How many tablespoons are in a quarter cup?",
    "Hey there, how's it going?",
    "The bus stop is two blocks from here.",
    "I think the meeting got moved to Thursday afternoon.",
    "Could you read that last message back to me?",
    "The children were counting the cars on the highway.",
    "Let's order pizza tonight, I'm too tired to cook.",
    "Water boils at one hundred degrees Celsius.",
    "Stop, that's enough for now.",
    "There's a strange noise coming from the garage.",
    "Tell me something interesting about black holes.",
    "The market closes early on Fridays.",
    "Thanks, that's all I needed.",
]

# Phrases that sound like "hey TARS" but must not wake it. In your own voice they're the hardest negatives:
# a model that has only heard you say the real phrase learns "your voice + hey ...ars".
LOOKALIKES = [
    "hey cars",
    "hey bars",
    "hey Mars",
    "hey stars",
    "hey Lars",
    "hey parts",
    "hey darts",
    "hey guitars",
    "hey guards",
    "hey Tara",
    "hey Jarvis",
    "hey there",
    "hey star",
    "hey tarot",
]
LOOKALIKE_VARIATIONS = [("normally", CLOSE), ("quickly", CLOSE), ("", FAR)]


@dataclass(frozen=True)
class Prompt:
    set_name: str
    index: int  # the clip's file number
    phrase: str
    how: str
    batch: str


def _prompts() -> list[Prompt]:
    prompts = []
    for set_name, phrase in [("hey_tars", "hey TARS"), ("tars_stop", "TARS stop")]:
        takes = [(how, batch) for how, batch in WAKE_VARIATIONS for _ in range(6)]
        prompts += [Prompt(set_name, i, phrase, how, batch) for i, (how, batch) in enumerate(takes)]
    prompts += [Prompt("speech", i, s, "read aloud", CLOSE) for i, s in enumerate(SENTENCES)]
    takes = [(p, how, batch) for p in LOOKALIKES for how, batch in LOOKALIKE_VARIATIONS]
    prompts += [
        Prompt("hey_tars_lookalikes", i, p, f"{how} (NOT hey TARS)".strip(), b) for i, (p, how, b) in enumerate(takes)
    ]
    return prompts


PROMPTS = _prompts()


def show(prompt: Prompt, n: int, total: int) -> None:
    """Big enough to read from across the room: the phrase on its own line, in bold."""
    phrase = prompt.phrase.upper() if len(prompt.phrase) < 25 else prompt.phrase
    print(f"\n[{n}/{total}] {prompt.how}\n\n        \033[1m{phrase}\033[0m\n")


def countdown(speaker: Speaker) -> None:
    """Beeps you can hear from across the room, then the first prompt's chime starts the recording."""
    for i in range(COUNTDOWN_BEEPS, 0, -1):
        print(f"  {i}...", flush=True)
        speaker.chime(freq=660.0, duration_s=0.15)
        time.sleep(0.85)


def record_voice(
    person: str, mic_name: str, mic: Microphone, speaker: Speaker, recorder: UtteranceRecorder, root: Path
) -> None:
    """Walk through every prompt batch by batch, skipping clips already recorded, so a session can be resumed.

    Clips go to root/person/mic_name/set, so the same person can have separate sessions per microphone.
    """

    def path(p: Prompt) -> Path:
        return root / person / mic_name / p.set_name / f"{p.index:03d}.wav"

    total = len(PROMPTS)
    done = sum(path(p).exists() for p in PROMPTS)
    for batch, intro in BATCHES.items():
        todo = [p for p in PROMPTS if p.batch == batch and not path(p).exists()]
        if not todo:
            continue
        print(f"\n=== {len(todo)} recordings {batch} ===\n{intro}")
        if batch != CLOSE:
            # You can't read the screen from across the room: write the list down, then say one per chime.
            print("\nWrite these down; you'll say them in this order, one after each chime:")
            for i, p in enumerate(todo, 1):
                print(f"  {i:>2}. {p.phrase}")
            input("\nPress Enter when you're ready; you'll hear 3 beeps to get into position. ")
            countdown(speaker)
        for p in todo:
            done += 1
            while True:
                show(p, done, total)
                # No rush while recording: keep the mic shut until the chime has fully left the speakers.
                with mic.paused(tail_s=0.3):
                    speaker.chime()
                pcm = recorder.record(mic, preroll_blocks=PAD_BLOCKS, tail_blocks=PAD_BLOCKS)
                if pcm is not None:
                    break
                print("  Didn't hear anything, let's try that one again.")
            save_wav(path(p), pcm)
            print(f"  Saved ({len(pcm) / 2 / SAMPLE_RATE:.1f}s)")
    print(f"\nAll {total} clips for {person} ({mic_name} mic) are in {root / person / mic_name}.")
