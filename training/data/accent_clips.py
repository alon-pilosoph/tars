"""'hey TARS' (and lookalikes) in accented voices: every non-English Piper voice of medium or high quality.

    DATA/tts/.venv/bin/python -m training.data.accent_clips

Latin-script languages read "hey tars" with their own pronunciation rules, so each comes out with that accent,
and they also read the English lookalikes ("hey cars"...). Other scripts get "hey TARS" spelled the way a native
speaker would write it (Russian "хэй тарс", Hindi "हे टार्स"...), wake phrase only. The Piper voice catalog
(voices.json) is downloaded once; it listed 106 such voices (52 languages, 56 locales), 8 are left out (UNLICENSED),
and 92 of the rest produced clips (9,550 wake phrases, 6,030 lookalikes). Resumable; a voice that fails to load or
speak is skipped. Output: DATA/clips/accent/hey_tars/{positive,near_miss}/<voice>_s<spk>_<n>.wav
"""

import json
import random
import urllib.request
from pathlib import Path

import numpy as np

from training.common import SR, Layout, cap_onnxruntime_threads, log, parser, write_wav

HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
MAX_SPEAKERS = 15
PER_SINGLE, PER_MULTI = 100, 15  # clips per voice (single speaker) / per speaker (multi-speaker voices)
# No usable license, or built on a voice without one (ATTRIBUTION.md): never used.
UNLICENSED = {
    "ar_JO-kareem-medium",
    "zh_CN-huayan-medium",
    "sv_SE-lisa-medium",
    "eu_ES-antton-medium",
    "eu_ES-maider-medium",
    "ka_GE-natia-medium",
    "ru_RU-irina-medium",
    "es_MX-claude-high",
}

NATIVE = {
    "ru": ["хэй тарс", "хэй, тарс!"],
    "uk": ["хей тарс", "хей, тарс!"],
    "bg": ["хей тарс", "хей, тарс!"],
    "kk": ["хэй тарс", "хэй, тарс!"],
    "el": ["χέι ταρς", "χέι, ταρς!"],
    "ar": ["هاي تارس", "هاي، تارس!"],
    "fa": ["هی تارس", "هی، تارس!"],
    "ur": ["ہے تارس", "ہے، تارس!"],
    "he": ["היי טארס", "היי, טארס!"],
    "hi": ["हे टार्स", "हे, टार्स!"],
    "mr": ["हे टार्स", "हे, टार्स!"],
    "ne": ["हे टार्स", "हे, टार्स!"],
    "bn": ["হেই টার্স", "হেই, টার্স!"],
    "ml": ["ഹേ ടാർസ്", "ഹേ, ടാർസ്!"],
    "te": ["హే టార్స్", "హే, టార్స్!"],
    "ka": ["ჰეი ტარს", "ჰეი, ტარს!"],
    "hy": ["հեյ տարս", "հեյ, տարս!"],
    "th": ["เฮ้ ทาร์ส", "เฮ้ ทาร์ส!"],
    "zh": ["嘿 塔斯", "嘿，塔斯！"],
    "ja": ["ヘイ、タース", "ヘイ タース！"],
    "ko": ["헤이 타스", "헤이, 타스!"],
}
LATIN = ["hey tars", "hey, tars", "hey tars!", "hey... tars"]
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
    "hey tar",
    "stars",
    "tars stop",
    "stop",
]


def catalog(models: Path) -> dict:
    path = models / "voices.json"
    if not path.exists():
        models.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(HF + "voices.json", path)
    return json.loads(path.read_text())


def voices(info: dict) -> list[str]:
    return sorted(
        key
        for key, v in info.items()
        if key not in UNLICENSED and not v["language"]["code"].startswith("en") and v["quality"] in ("medium", "high")
    )


def load(models: Path, info: dict, key: str):
    from piper import PiperVoice

    onnx = next(f for f in info[key]["files"] if f.endswith(".onnx"))
    local = models / Path(onnx).name
    for rel, dst in [(onnx, local), (onnx + ".json", Path(str(local) + ".json"))]:
        if not dst.exists():
            tmp = dst.with_suffix(dst.suffix + ".part")
            urllib.request.urlretrieve(HF + rel, tmp)
            tmp.rename(dst)
    return PiperVoice.load(str(local)), info[key]["num_speakers"]


def speak(
    voice, key: str, n_speakers: int, kind: str, choices: list[str], speakers: list[int], per: int, out: Path
) -> int:
    """A voice that fails on one text is dropped for the rest of this kind."""
    from piper import SynthesisConfig
    from scipy.signal import resample_poly

    made = 0
    rng = random.Random(f"{key}-{kind}")
    for spk in speakers:
        for i in range(per):
            path = out / kind / f"{key}_s{spk:03d}_{i:03d}.wav"
            text = rng.choice(choices)
            cfg = SynthesisConfig(
                speaker_id=spk if n_speakers > 1 else None,
                length_scale=rng.uniform(0.8, 1.35),
                noise_scale=rng.uniform(0.45, 0.9),
                noise_w_scale=rng.uniform(0.6, 1.0),
            )
            if path.exists():
                continue
            try:
                audio = np.concatenate([c.audio_int16_array for c in voice.synthesize(text, syn_config=cfg)])
            except Exception as e:  # noqa: BLE001 - some voices can't say some scripts' text
                log(f"skip {key} ({kind}): {e}")
                return made
            rate = voice.config.sample_rate
            if rate != SR:
                audio = resample_poly(audio.astype(np.float32), SR, rate).astype(np.int16)
            write_wav(path, audio)
            made += 1
    return made


def main():
    args = parser(__doc__).parse_args()
    layout = Layout(args.data)
    cap_onnxruntime_threads()
    out = layout.clips / "accent" / "hey_tars"
    info = catalog(layout.piper_models)
    for key in voices(info):
        lang = key.split("_")[0]
        texts = {"positive": NATIVE.get(lang, LATIN)}
        if lang not in NATIVE:
            texts["near_miss"] = LOOKALIKES
        try:
            voice, n_speakers = load(layout.piper_models, info, key)
        except Exception as e:  # noqa: BLE001 - some catalog voices don't load; skip them
            log(f"skip {key}: {e}")
            continue
        speakers = list(range(n_speakers))
        random.Random(key).shuffle(speakers)
        speakers = speakers[:MAX_SPEAKERS]
        per = PER_MULTI if n_speakers > 1 else PER_SINGLE
        made = sum(speak(voice, key, n_speakers, kind, choices, speakers, per, out) for kind, choices in texts.items())
        log(f"{key}: {len(speakers)} speakers, {made} clips")
    log("ALL DONE")


if __name__ == "__main__":
    main()
