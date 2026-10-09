"""OpenAI TTS clips: 8 voices for training, 3 held out for testing only. About $0.60 for both phrases.

    OPENAI_API_KEY=... DATA/tts/.venv/bin/python -m training.data.openai_clips

Each training voice says the phrase in 20 styles x 4 spellings, 12 more styles x 2, and 60 mood/pace/distance
combinations (164 clips per voice and phrase), plus 38 lookalikes (42 for "TARS stop"). The held-out voices (coral,
sage, verse) say it in 10 styles x 3 spellings, plus 14 lookalikes; nothing ever trains on them.
Output: DATA/clips/openai/PHRASE/{positive,near_miss}, and DATA/bench/clips_heldout/{PHRASE,PHRASE_near_miss}.
"""

import os
import random

import numpy as np

from training.common import Layout, log, parser, write_wav

TRAIN_VOICES = ["alloy", "ash", "ballad", "echo", "fable", "nova", "onyx", "shimmer"]
TEST_VOICES = ["coral", "sage", "verse"]
SAY = "Say 'Tars' as one word, like the robot in Interstellar, rhyming with 'stars' but without the first s. "
STYLES = [
    "naturally, to a smart speaker across the room",
    "quickly and casually, busy",
    "softly, almost whispering",
    "loudly, calling from another room",
    "tired, at the end of a long day",
    "cheerfully, in a good mood",
    "with a mouth half full, mumbling a bit",
    "impatiently, a little annoyed",
    "slowly and clearly",
    "as a quick aside in the middle of doing something",
    "with a yawn",
    "with a slight laugh",
    "in a deep, low register",
    "in a higher, lighter register",
    "with a strong British accent",
    "with a southern US accent",
    "with an Indian English accent",
    "with a French accent",
    "with an Israeli accent",
    "with a Spanish accent",
]
STYLES_2 = [
    "in a sing-song voice",
    "shouting",
    "drawn out, like 'Heyyy Taaars'",
    "sleepily, barely awake",
    "in a flat monotone",
    "over-enunciating every sound",
    "from far away, raising your voice",
    "sarcastically",
    "very fast, the two words almost merged",
    "with a long pause between the two words",
    "questioningly, unsure it's listening",
    "firmly, like giving an order",
]
EMOTIONS = ["happy", "bored", "stressed", "relaxed", "irritated", "curious", "excited", "sad", "amused", "distracted"]
PACES = ["quickly", "slowly", "at a normal pace"]
PLACES = ["up close", "from across the room"]
STYLES_3 = [f"{e}, {pace}, {place}" for e in EMOTIONS for pace in PACES for place in PLACES]
TEXTS = {
    "hey_tars": ["Hey Tars.", "Hey, Tars?", "Hey Tars!", "hey tars"],
    "tars_stop": ["Tars, stop.", "Tars stop!", "Tars, stop!", "Okay Tars, stop."],
}
NEAR = {
    "hey_tars": [
        "Hey stars.",
        "Hey cars.",
        "Hey Lars.",
        "Hey Mars.",
        "Hey Jarvis.",
        "Hey there.",
        "Hey darts.",
        "Hey Tara.",
        "Hey tar.",
        "Hey start.",
        "Hey tarot.",
        "Hey parts.",
        "Hey guards.",
        "Tars, stop.",
    ],
    "tars_stop": [
        "Stars, stop.",
        "Cars stop.",
        "Tar stop.",
        "Stop.",
        "Bus stop.",
        "Hard stop.",
        "Pit stop.",
        "Stars stopped.",
        "Stop the car.",
        "Tarts stop.",
        "Guitars stop.",
        "Hey Tars.",
        "Tars.",
        "Don't stop.",
    ],
}
NEAR_EXTRA = {  # training voices only
    "hey_tars": [
        "Hey Siri.",
        "Hey Google.",
        "Okay Google.",
        "Alexa.",
        "Hey Alexa.",
        "Hey Mycroft.",
        "Hey Taurus.",
        "Hey Tarzan.",
        "Hey Target.",
        "Hey guitar.",
        "Hey, tsars.",
        "Hey dollars.",
        "Hey Carlos.",
        "Hey tires.",
        "Hey cards.",
        "Hey Charles.",
        "Hey hearts.",
        "Hey Mark.",
        "Hey Clark.",
        "Hey tardy.",
        "Hate tar.",
        "Hey star wars.",
        "Hey TARDIS.",
        "Hey there, stars.",
    ],
    "tars_stop": [
        "Tsars, stop.",
        "Taurus, stop.",
        "Tarzan, stop.",
        "Stars, stop it.",
        "Tar stops.",
        "Bars stop.",
        "Charles, stop.",
        "Arthur, stop.",
        "Target stop.",
        "Guitar, stop.",
        "Stop, stop.",
        "Stoplight.",
        "Starstruck.",
        "Cards, stop.",
        "Stop the stars.",
        "Hearts stop.",
        "Top.",
        "Stop talking.",
        "Please stop.",
        "Okay, stop.",
        "Stop it.",
        "Stop there.",
        "I can't stop laughing.",
        "Star, stop.",
        "Timer stopped.",
        "I'll stop there.",
        "Stop me if you've heard this.",
        "Tars, start.",
    ],
}


def synth(client, path, text, voice, instruction):
    from scipy.signal import resample_poly

    if path.exists():
        return
    pcm = client.audio.speech.create(
        model="gpt-4o-mini-tts", voice=voice, input=text, instructions=instruction, response_format="pcm"
    ).content
    audio = resample_poly(np.frombuffer(pcm, np.int16).astype(np.float32), 2, 3)  # 24 -> 16 kHz
    write_wav(path, audio.astype(np.int16))


def jobs(layout: Layout) -> list[tuple]:
    out, test_out = layout.clips / "openai", layout.heldout
    todo = []
    for phrase, texts in TEXTS.items():
        positive = out / phrase / "positive"
        for v in TRAIN_VOICES:
            for s, style in enumerate(STYLES):
                for t, text in enumerate(texts):
                    todo.append((positive / f"{v}_{s:02d}_{t}.wav", text, v, SAY + "Say it " + style + "."))
            for s2, style in enumerate(STYLES_2):
                for t, text in enumerate(texts[:2]):
                    todo.append((positive / f"{v}_b{s2:02d}_{t}.wav", text, v, SAY + "Say it " + style + "."))
            for s3, style in enumerate(STYLES_3):
                todo.append((positive / f"{v}_c{s3:02d}.wav", texts[s3 % len(texts)], v, SAY + "Say it " + style + "."))
            for n, text in enumerate(NEAR[phrase] + NEAR_EXTRA[phrase]):
                style = random.Random(f"{v}{n}").choice(STYLES)
                todo.append((out / phrase / "near_miss" / f"{v}_{n:02d}.wav", text, v, "Say it " + style + "."))
        for v in TEST_VOICES:
            for s, style in enumerate(STYLES[:10]):
                for t, text in enumerate(texts[:3]):
                    todo.append((test_out / phrase / f"{v}_{s:02d}_{t}.wav", text, v, SAY + "Say it " + style + "."))
            for n, text in enumerate(NEAR[phrase]):
                todo.append((test_out / f"{phrase}_near_miss" / f"{v}_{n:02d}.wav", text, v, "Say it naturally."))
    return todo


def main():
    args = parser(__doc__).parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY first.")
    from openai import OpenAI

    client = OpenAI(timeout=30)
    all_jobs = jobs(Layout(args.data))
    todo = [j for j in all_jobs if not j[0].exists()]
    log(f"{len(all_jobs)} clips, {len(todo)} to synthesize")
    for i, job in enumerate(todo, 1):
        synth(client, *job)
        if i % 100 == 0:
            log(f"  {i}/{len(todo)}")
    log("done")


if __name__ == "__main__":
    main()
