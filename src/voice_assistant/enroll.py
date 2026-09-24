"""Guided recording of one person's voice.

The clips feed three things: the wake-word verifier (your "hey TARS" vs. your other speech),
the "TARS stop" model, and speaker identification (the read-aloud sentences).
"""

import wave
from pathlib import Path

from .audio import BLOCK_SECONDS, SAMPLE_RATE, Microphone, Speaker
from .recorder import UtteranceRecorder

# The verifier runs the wake-word model over each clip, and the model only scores high once it has
# heard a little past the end of the phrase, so keep about a second of room audio on both sides.
PAD_BLOCKS = round(1.0 / BLOCK_SECONDS)

WAKE_VARIATIONS = [
    "normally",
    "from a couple of meters away",
    "quickly, like you're busy",
    "quietly",
    "with the TV or music on, if you can",
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

SETS = [
    ("hey_tars", [f'Say "hey TARS" {v}' for v in WAKE_VARIATIONS for _ in range(6)]),
    ("tars_stop", [f'Say "TARS stop" {v}' for v in WAKE_VARIATIONS for _ in range(6)]),
    ("speech", [f'Read aloud: "{s}"' for s in SENTENCES]),
]


def save_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm)


def record_voice(person: str, mic: Microphone, speaker: Speaker, recorder: UtteranceRecorder, root: Path) -> None:
    """Walk through every prompt, skipping clips already recorded, so a session can be resumed."""
    total = sum(len(prompts) for _, prompts in SETS)
    done = 0
    for set_name, prompts in SETS:
        folder = root / person / set_name
        for i, prompt in enumerate(prompts):
            done += 1
            path = folder / f"{i:03d}.wav"
            if path.exists():
                continue
            while True:
                print(f"\n[{done}/{total}] {prompt}")
                with mic.paused(tail_s=0.05):
                    speaker.chime()
                pcm = recorder.record(mic, preroll_blocks=PAD_BLOCKS, tail_blocks=PAD_BLOCKS)
                if pcm is not None:
                    break
                print("  Didn't hear anything, let's try that one again.")
            save_wav(path, pcm)
            print(f"  Saved {path} ({len(pcm) / 2 / SAMPLE_RATE:.1f}s)")
    print(f"\nAll {total} clips for {person} are in {root / person}.")
