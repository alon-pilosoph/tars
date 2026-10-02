"""Wake-phrase and lookalike clips from Kokoro TTS: a second engine, 28 voices plus blends of pairs.

    DATA/tts/.venv/bin/python -m training.data.kokoro_clips hey_tars 20000 16000   (PHRASE N_POSITIVE N_NEAR_MISS)

Half the clips use a single voice, half a blend of two (a voice no single speaker has), at 0.8-1.3x speed.
Resumable: clip names are deterministic and existing files are skipped. Two workers can split one list: run a
second one with REVERSE=1 and it works from the end. Output: DATA/clips/kokoro/PHRASE/{positive,near_miss}/<n>.wav
"""

import os
import random

import numpy as np

from training.common import PHRASES, Layout, log, parser, write_wav

T = "[TARS](/tˈɑːɹs/)"  # forced pronunciation: TARS with an S (plain "TARS" gets spelled out as letters)

VOICES = [
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_heart",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
]
POSITIVE = {
    "hey_tars": [f"Hey {T}.", f"Hey, {T}?", f"Hey {T}!", f"hey {T}", f"Hey... {T}."],
    "tars_stop": [f"{T}, stop.", f"{T} stop!", f"{T}, stop!", f"{T} stop", f"Okay {T}, stop."],
}
NEAR_MISS = {
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
        "Hey stars.",
        "Hey cars.",
        "Hey Lars.",
        "Hey Mars.",
        "Hey Jarvis.",
        "Hey there.",
        "Hey, guitars!",
        "Hey Tara.",
        "Hey tar.",
        "Hey start.",
        "Hey tarot.",
        "Hey, it's ours.",
        "Hey darts.",
        "Hey carts.",
        "Hey parts.",
        "Hey star.",
        "Hey bars.",
        "Hey, tar pits.",
        "Hey stars, look!",
        "Hey guards.",
        f"{T}, stop.",
        "Hey, stop.",
        "Hey, tars are sticky.",
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
        "Stars, stop.",
        "Cars stop.",
        "Tar stop.",
        "Stop.",
        "Don't stop.",
        "Bus stop.",
        "Stars top.",
        "Hard stop.",
        "Stars stopped.",
        "Stop the car.",
        "Tarts stop.",
        "Guitars stop.",
        "Cars, stop!",
        "Stars stop!",
        "Start, stop.",
        "Pit stop.",
        "Hey, stop.",
        f"Hey {T}.",
        f"{T}.",
        "Carts stop.",
        "Darts, stop.",
        "Stop it, stars.",
    ],
}


def voice_for(rng: random.Random) -> str:
    if rng.random() < 0.5:
        return rng.choice(VOICES)
    a, b = rng.sample(VOICES, 2)
    return f"{a},{b}"


def main():
    p = parser(__doc__)
    p.add_argument("phrase", choices=PHRASES)
    p.add_argument("n_positive", type=int)
    p.add_argument("n_near_miss", type=int)
    args = p.parse_args()
    from kokoro import KPipeline
    from scipy.signal import resample_poly

    layout = Layout(args.data)
    pipes = {"a": KPipeline(lang_code="a"), "b": KPipeline(lang_code="b")}
    for kind, texts, n in [
        ("positive", POSITIVE[args.phrase], args.n_positive),
        ("near_miss", NEAR_MISS[args.phrase], args.n_near_miss),
    ]:
        folder = layout.clip_dir("kokoro", args.phrase, kind)
        rng = random.Random(f"{args.phrase}-{kind}")
        plan = [(voice_for(rng), rng.choice(texts), round(rng.uniform(0.8, 1.3), 2)) for _ in range(n)]
        order = range(n - 1, -1, -1) if os.environ.get("REVERSE") == "1" else range(n)
        for i in order:
            voice, text, speed = plan[i]
            path = folder / f"{i:06d}.wav"
            if path.exists():
                continue
            lang = "b" if voice.startswith("b") else "a"  # British voices get British G2P
            audio = np.concatenate(
                [a.numpy() if hasattr(a, "numpy") else a for _, _, a in pipes[lang](text, voice=voice, speed=speed)]
            )
            audio = np.clip(resample_poly(audio, 2, 3), -1, 1)  # 24 -> 16 kHz
            write_wav(path, (audio * 32767).astype(np.int16))
            if i % 500 == 0:
                log(f"{args.phrase} {kind}: {i}/{n}")
        log(f"{args.phrase} {kind}: done ({n})")


if __name__ == "__main__":
    main()
