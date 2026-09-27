"""Voice the clips the web UI's demo is built from (tools/demo_audio/), so the demo and its checks run anywhere.

    uv run python tools/make_demo_audio.py        # needs OPENAI_API_KEY in .env; about a cent

Four speakers, each an OpenAI voice (never Onyx, which is TARS's own): the household's Alon and Stacey, a guest, and
the TV. What they say doesn't have to match the demo's transcripts; they have to sound like four different people, so
the voice clustering finds them. Speaker ID hears most of OpenAI's voices as close relatives (similarity 0.5-0.8);
these four stay below 0.4 of each other. The clips are committed; run this again only to change them.
"""

import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

REPO = Path(__file__).parents[1]
OUT = REPO / "tools" / "demo_audio"

SAY = [
    "What's the weather going to be like tomorrow?",
    "Set a timer for ten minutes.",
    "Remind me to call my mom after dinner.",
    "How many tablespoons are in a quarter cup?",
    "Turn the music down a little.",
    "Tell me something about black holes.",
    "Turn off the lights.",
    "Yes, set an alarm for seven.",
    "No, never mind.",
    "Send me the timetable.",
    "What's on my calendar today?",
    "Add eggs and milk to the shopping list.",
]
SPEAKERS = {
    "alon": (
        "ash",
        {
            "wake": [
                "Hey TARS.",
                "Hey, TARS.",
                "Hey TARS!",
                "Hey TARS?",
                "Hey TARS...",
                "hey TARS",
                "Hey TARS, hey.",
                "Hey. TARS.",
                "Hey TARS!!",
                "Hey, TARS?",
            ],
            "lookalike": ["Hey cars.", "Hey bars.", "Hey Mars.", "Hey stars.", "Hey guitars."],
            "say": SAY,
        },
    ),
    "stacey": (
        "nova",
        {
            "wake": ["Hey TARS.", "Hey, TARS.", "Hey TARS!"],
            "say": [
                "Play some jazz.",
                "What's on my calendar today?",
                "Add milk to the shopping list.",
                "Yes, twice. She's lying.",
            ],
        },
    ),
    "guest": (
        "fable",
        {"wake": ["Hey TARS.", "Hey, TARS?"], "say": ["Is it going to rain?", "What time is it in Tokyo?"]},
    ),
    "tv": (
        "alloy",
        {
            "say": [
                (
                    "And in tonight's top story, the storm is moving east, with heavy rain expected across the coast by morning. "
                    "Officials are asking drivers to stay off the roads."
                ),
                (
                    "You won't believe what happened next. After the break, the chef who turned a food truck into a national "
                    "chain, and the one ingredient she refuses to use."
                ),
            ]
        },
    ),
}


def main() -> None:
    sys.path.insert(0, str(REPO / "src"))
    from voice_assistant.__main__ import make_openai_client

    client = make_openai_client(REPO / ".env")
    for speaker, (voice, sets) in SPEAKERS.items():
        for kind, lines in sets.items():
            for i, text in enumerate(lines):
                path = OUT / speaker / f"{kind}_{i:02d}.flac"
                if path.exists():
                    continue
                pcm = client.audio.speech.create(
                    model="gpt-4o-mini-tts", voice=voice, input=text, response_format="pcm"
                ).content  # 24 kHz mono int16
                audio = resample_poly(np.frombuffer(pcm, np.int16).astype(np.float32), 2, 3)
                path.parent.mkdir(parents=True, exist_ok=True)
                sf.write(path, np.clip(audio, -32768, 32767).astype(np.int16), 16000)
                print(f"{path.relative_to(REPO)}: {text}")


if __name__ == "__main__":
    main()
