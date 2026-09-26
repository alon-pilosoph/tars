"""Wake-phrase and lookalike clips from about 1,100 more Piper voices, a different set from the LibriTTS-R one.

    DATA/tts/.venv/bin/python -m training.data.piper_voices_clips hey_tars

Voices: VCTK (109 speakers), L2-ARCTIC (24 speakers with accented English), ARCTIC (18), ARU (12), SEMAINE (4),
LibriTTS-high (904) and 17 single-speaker voices. "tarss" makes espeak say TARS with an S.
Resumable: names are deterministic and existing files are skipped.
Output: DATA/clips/piper_voices/PHRASE/{positive,near_miss}/<voice>_s<speaker>_<n>.wav
"""

import json
import os
import random
import urllib.request
import wave
from pathlib import Path

import numpy as np

from training.common import PHRASES, Layout, cap_onnxruntime_threads, parser

HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"

# model path: clips per speaker (multi-speaker voices) or per voice (single speaker)
VOICES = {
    "en/en_GB/vctk/medium/en_GB-vctk-medium": 40,
    "en/en_US/l2arctic/medium/en_US-l2arctic-medium": 80,
    "en/en_US/arctic/medium/en_US-arctic-medium": 40,
    "en/en_GB/aru/medium/en_GB-aru-medium": 40,
    "en/en_GB/semaine/medium/en_GB-semaine-medium": 60,
    "en/en_US/libritts/high/en_US-libritts-high": 12,
    **{
        p: 150
        for p in [
            "en/en_GB/alan/medium/en_GB-alan-medium",
            "en/en_GB/alba/medium/en_GB-alba-medium",
            "en/en_GB/cori/medium/en_GB-cori-medium",
            "en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium",
            "en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium",
            "en/en_GB/southern_english_female/low/en_GB-southern_english_female-low",
            "en/en_US/amy/medium/en_US-amy-medium",
            "en/en_US/bryce/medium/en_US-bryce-medium",
            "en/en_US/danny/low/en_US-danny-low",
            "en/en_US/hfc_female/medium/en_US-hfc_female-medium",
            "en/en_US/hfc_male/medium/en_US-hfc_male-medium",
            "en/en_US/joe/medium/en_US-joe-medium",
            "en/en_US/john/medium/en_US-john-medium",
            "en/en_US/kathleen/low/en_US-kathleen-low",
            "en/en_US/kristin/medium/en_US-kristin-medium",
            "en/en_US/kusal/medium/en_US-kusal-medium",
            "en/en_US/lessac/medium/en_US-lessac-medium",
        ]
    },
}
POSITIVE = {
    "hey_tars": ["Hey tarss.", "Hey, tarss?", "Hey tarss!", "hey tarss", "Hey... tarss."],
    "tars_stop": ["Tarss, stop.", "Tarss stop!", "Tarss, stop!", "tarss stop", "Okay tarss, stop."],
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
        "Hey darts.",
        "Hey carts.",
        "Hey parts.",
        "Hey star.",
        "Hey guards.",
        "Tarss, stop.",
        "Hey, stop.",
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
        "Pit stop.",
        "Hey tarss.",
        "Tarss.",
        "Carts stop.",
    ],
}
NEAR_MISS_RATIO = 1.2


def load(models: Path, path: str):
    from piper import PiperVoice

    local = models / Path(path).name
    for ext in [".onnx", ".onnx.json"]:
        if not Path(str(local) + ext).exists():
            models.mkdir(parents=True, exist_ok=True)
            # Download to a temp name, then rename: the other phrase's worker may load it at the same time.
            tmp = str(local) + ext + f".part{os.getpid()}"
            urllib.request.urlretrieve(HF + path + ext, tmp)
            os.replace(tmp, str(local) + ext)
    return PiperVoice.load(str(local) + ".onnx")


def main():
    p = parser(__doc__)
    p.add_argument("phrase", choices=PHRASES)
    args = p.parse_args()
    layout = Layout(args.data)
    cap_onnxruntime_threads()
    from piper import SynthesisConfig
    from scipy.signal import resample_poly

    for path, per_speaker in VOICES.items():
        voice = load(layout.piper_models, path)
        name = Path(path).name
        n_speakers = json.loads(Path(str(layout.piper_models / name) + ".onnx.json").read_text()).get("num_speakers", 1)
        for kind, texts, count in [
            ("positive", POSITIVE[args.phrase], per_speaker),
            ("near_miss", NEAR_MISS[args.phrase], int(per_speaker * NEAR_MISS_RATIO)),
        ]:
            folder = layout.clip_dir("piper_voices", args.phrase, kind)
            folder.mkdir(parents=True, exist_ok=True)
            rng = random.Random(f"{args.phrase}-{kind}-{name}")
            for speaker in range(n_speakers):
                for i in range(count):
                    text = rng.choice(texts)
                    cfg = SynthesisConfig(
                        speaker_id=speaker if n_speakers > 1 else None,
                        length_scale=rng.uniform(0.8, 1.35),
                        noise_scale=rng.uniform(0.45, 0.9),
                        noise_w_scale=rng.uniform(0.6, 1.0),
                    )
                    out = folder / f"{name}_s{speaker:03d}_{i:03d}.wav"
                    if out.exists():
                        continue
                    audio = np.concatenate([c.audio_int16_array for c in voice.synthesize(text, syn_config=cfg)])
                    rate = voice.config.sample_rate
                    if rate != 16000:
                        audio = resample_poly(audio.astype(np.float32), 16000, rate).astype(np.int16)
                    with wave.open(str(out), "wb") as f:
                        f.setnchannels(1)
                        f.setsampwidth(2)
                        f.setframerate(16000)
                        f.writeframes(audio.tobytes())
        print(f"{args.phrase}: {name} ({n_speakers} speakers) done", flush=True)
    print(f"{args.phrase}: ALL DONE", flush=True)


if __name__ == "__main__":
    main()
