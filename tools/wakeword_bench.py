"""Benchmark wake-word models on audio they never trained on.

    uv run python tools/wakeword_bench.py make-clips          # synthesize test phrases with OpenAI TTS (once)
    uv run python tools/wakeword_bench.py run MODEL.onnx       # openWakeWord model, from this repo's environment
    ~/mww_training/.venv/bin/python tools/wakeword_bench.py run MODEL.tflite   # microWakeWord, from its training env
    uv run python tools/wakeword_bench.py compare              # side-by-side table of every model scored so far

Measures, per model and threshold:
  - recall on "hey TARS" said by 11 OpenAI TTS voices (a different engine from the Piper voices the models train on),
    clean and mixed with background noise
  - false accepts on near-miss phrases ("hey stars", "tars stop", ...)
  - false accepts per hour on long speech that never contains the phrase (LibriSpeech test-clean)
Real recordings (voice_data/<person>/hey_tars) are included automatically when present; they're the best test.
"""

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
BENCH = Path.home() / "wakeword_bench"
SR = 16_000

VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"]
STYLES = {
    "normal": "Say it naturally, like calling out to a smart speaker across the room.",
    "quick": "Say it quickly and casually, like you're busy.",
    "quiet": "Say it softly, almost under your breath.",
}
PHRASES = {
    "hey_tars": ["Hey TARS.", "Hey, TARS?", "Hey TARS!"],
    "tars_stop": ["TARS, stop.", "TARS stop!", "TARS, stop!"],
}
NEAR_MISSES = [
    "Hey stars.",
    "Hey cars.",
    "Hey Lars.",
    "Hey Mars.",
    "Hey Jarvis.",
    "Hey there.",
    "Hey, guitars!",
    "Hey Tara.",
    "Tar.",
    "Stop.",
    "Hey, stop.",
    "The stars are out.",
    "Stop the car.",
    "Hey, it's ours.",
    "Hey TARDIS.",
    "Heat tar.",
]
# Each model is also tested on the other model's phrase: "hey TARS" must not trigger the stop model and vice versa.
NOISE_SNRS_DB = [None, 10, 5]


def pronunciation_note() -> str:
    return "Say TARS like the robot in Interstellar, ending with a crisp S sound."


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path)) as f:
        audio = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        rate = f.getframerate()
    if rate != SR:
        from scipy.signal import resample_poly

        audio = resample_poly(audio.astype(np.float32), SR, rate).astype(np.int16)
    return audio


def write_wav(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(audio.astype(np.int16).tobytes())


# ---------- clip synthesis ----------


def make_clips() -> None:
    from dotenv import dotenv_values
    from openai import OpenAI
    from scipy.signal import resample_poly

    client = OpenAI(api_key=dotenv_values(REPO / ".env")["OPENAI_API_KEY"], timeout=30)
    jobs = []
    for model, phrases in PHRASES.items():
        for v in VOICES:
            for style, instruction in STYLES.items():
                for i, text in enumerate(phrases):
                    jobs.append(
                        (
                            BENCH / "clips" / model / f"{v}_{style}_{i}.wav",
                            text,
                            f"{instruction} {pronunciation_note()}",
                            v,
                        )
                    )
    for v in VOICES:
        for i, text in enumerate(NEAR_MISSES):
            jobs.append((BENCH / "clips" / "near_miss" / f"{v}_{i}.wav", text, STYLES["normal"], v))
    todo = [j for j in jobs if not j[0].exists()]
    print(f"{len(jobs)} clips, {len(todo)} to synthesize")
    for n, (path, text, instruction, voice) in enumerate(todo, 1):
        pcm = client.audio.speech.create(
            model="gpt-4o-mini-tts", voice=voice, input=text, instructions=instruction, response_format="pcm"
        ).content
        audio24 = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        write_wav(path, resample_poly(audio24, 2, 3))
        if n % 25 == 0:
            print(f"  {n}/{len(todo)}")
    print("done")


# ---------- engines ----------


class OpenWakeWord:
    frame = 1280  # 80 ms

    def __init__(self, path: Path):
        from openwakeword.model import Model

        self.model = Model(wakeword_models=[str(path)], inference_framework="onnx")
        self.name = path.stem

    def reset(self):
        self.model.reset()

    def scores(self, audio: np.ndarray) -> np.ndarray:
        out = []
        for i in range(0, len(audio) - self.frame + 1, self.frame):
            out.append(max(self.model.predict(audio[i : i + self.frame]).values()))
        return np.array(out)


class MicroWakeWord:
    frame = 480  # one prediction per 30 ms: the streaming model strides over three 10 ms feature slices

    def __init__(self, path: Path):
        sys.path.insert(0, str(Path.home() / "mww_training" / "microWakeWord"))
        from microwakeword.inference import Model

        self._model_class, self._path = Model, str(path)
        self.name = path.stem
        self.reset()

    def reset(self):
        # The streaming model keeps its state inside the interpreter; a fresh one is the only clean reset.
        self.model = self._model_class(self._path)

    def scores(self, audio: np.ndarray) -> np.ndarray:
        return np.array(self.model.predict_clip(audio, step_ms=10), dtype=np.float32)


def load_engine(path: Path):
    return OpenWakeWord(path) if path.suffix == ".onnx" else MicroWakeWord(path)


# ---------- scoring ----------


def noise_bank() -> list[np.ndarray]:
    """Background noise to mix into test clips: music and household audio if downloaded, else pink noise."""
    files = sorted((BENCH / "noise").glob("*.wav"))[:200]
    if files:
        return [read_wav(f) for f in files]
    rng = np.random.default_rng(0)
    white = rng.normal(0, 1, SR * 30)
    pink = np.cumsum(white) - np.convolve(np.cumsum(white), np.ones(400) / 400, mode="same")
    return [(pink / np.abs(pink).max() * 8000).astype(np.int16)]


def mix(clip: np.ndarray, noise: np.ndarray, snr_db, rng) -> np.ndarray:
    if snr_db is None:
        return clip
    start = rng.integers(0, max(1, len(noise) - len(clip)))
    n = np.resize(noise[start : start + len(clip)].astype(np.float32), len(clip))
    p_clip = np.mean(clip.astype(np.float32) ** 2) + 1e-9
    p_noise = np.mean(n**2) + 1e-9
    n *= np.sqrt(p_clip / (p_noise * 10 ** (snr_db / 10)))
    return np.clip(clip + n, -32768, 32767).astype(np.int16)


def clip_peak(engine, clip: np.ndarray) -> float:
    """Highest score while the clip plays, with room noise before and after so every model gets context."""
    rng = np.random.default_rng(len(clip))
    pad = rng.normal(0, 40, SR * 2).astype(np.int16)
    engine.reset()
    return float(engine.scores(np.concatenate([pad, clip, pad])).max())


def false_accepts(scores: np.ndarray, threshold: float, frames_per_s: float) -> int:
    """Detections in a long stream's scores, counting a burst of high scores (within 2 s) as one."""
    events, last = 0, -1e9
    for i in np.flatnonzero(scores >= threshold):
        if i - last > 2 * frames_per_s:
            events += 1
        last = i
    return events


def long_speech(max_hours: float) -> np.ndarray | None:
    flacs = sorted((BENCH / "LibriSpeech" / "test-clean").glob("**/*.flac"))
    if not flacs:
        return None
    import soundfile as sf

    parts, total = [], 0
    for f in flacs:
        a, _ = sf.read(f, dtype="int16")
        parts.append(a)
        total += len(a)
        if total > max_hours * 3600 * SR:
            break
    return np.concatenate(parts)


INTERFERENCE = BENCH / "interference" / "test"  # built by make_interference.py from sources no model trains on
# (condition name, interference bank or None, SNR dB, through a real recorded room?)
CONDITIONS = [
    ("clean", None, None, False),
    ("babble_5dB", "babble", 5, False),
    ("babble_0dB", "babble", 0, False),
    ("tv_5dB", "tv", 5, False),
    ("tv_0dB", "tv", 0, False),
    ("music_5dB", "music", 5, False),
    ("noise_5dB", "noise", 5, False),
    ("far_room+tv_10dB", "tv", 10, True),
]


def bank(name: str, limit: int = 200) -> list[np.ndarray]:
    return [read_wav(f) for f in sorted((INTERFERENCE / name).glob("*.wav"))[:limit]]


def in_room(clip: np.ndarray, rir: np.ndarray) -> np.ndarray:
    from scipy.signal import fftconvolve

    wet = fftconvolve(clip.astype(np.float32), rir.astype(np.float32) / (np.abs(rir).max() + 1e-9))[: len(clip)]
    wet *= (np.sqrt(np.mean(clip.astype(np.float32) ** 2)) + 1e-9) / (np.sqrt(np.mean(wet**2)) + 1e-9)
    return np.clip(wet, -32768, 32767).astype(np.int16)


def run(models: list[Path], thresholds: list[float], fa_hours: float) -> None:
    rng = np.random.default_rng(0)
    banks = {n: bank(n) for n in ["babble", "tv", "music", "noise"]} if INTERFERENCE.exists() else {}
    rooms = bank("rooms", limit=1000) if INTERFERENCE.exists() else []
    tv_hour_files = sorted((INTERFERENCE / "tv_hour").glob("*.wav"))
    tv_hour = np.concatenate([read_wav(f) for f in tv_hour_files]) if tv_hour_files else None
    speech = long_speech(fa_hours) if fa_hours > 0 else None  # --fa-hours 0: skip the audiobook scan
    results = {}
    for path in models:
        engine = load_engine(path)
        target = "tars_stop" if "stop" in path.stem else "hey_tars"
        other = "hey_tars" if target == "tars_stop" else "tars_stop"
        heldout = BENCH / "clips_heldout"
        positives = {
            "heldout_voices": sorted((heldout / target).glob("*.wav")),  # coral, sage, verse: never in any training set
            # One set per recorded session (person + mic), so a new mic gets its own numbers.
            # Only the test half: newer models train on takes 0-2 of every 6 (see train_mww_exp.py).
            **{
                f"real_{d.parent.parent.name}_{d.parent.name}": [
                    f for f in sorted(d.glob("*.wav")) if int(f.stem) % 6 >= 3
                ]
                for d in sorted((REPO / "voice_data").glob(f"*/*/{target}"))
            },
        }
        negatives = {
            "near_miss_heldout": sorted((heldout / f"{target}_near_miss").glob("*.wav"))
            + sorted((heldout / other).glob("*.wav")),
            "your_speech": sorted((REPO / "voice_data").glob("*/*/speech/*.wav"))[1::2],  # test half of the sentences
            # All 11 OpenAI voices; 8 of them are in newer models' training data, so these are for older models.
            "near_miss_all_voices": sorted((BENCH / "clips" / "near_miss").glob("*.wav"))
            + sorted((BENCH / "clips" / other).glob("*.wav")),
        }
        peaks = {}
        print(f"{path.name}: scoring clips...", flush=True)
        for name, files in positives.items():
            clips = [read_wav(f) for f in files]
            for cond, noise, snr, room in CONDITIONS:
                if noise and noise not in banks or room and not rooms:
                    continue
                scored = []
                for clip in clips:
                    c = in_room(clip, rooms[rng.integers(len(rooms))]) if room else clip
                    c = mix(c, banks[noise][rng.integers(len(banks[noise]))], snr, rng) if noise else c
                    scored.append(clip_peak(engine, c))
                peaks[f"{name}@{cond}"] = scored
        for name, files in negatives.items():
            peaks[name] = [clip_peak(engine, read_wav(f)) for f in files]
        row = {"clips": {k: len(v) for k, v in peaks.items()}, "by_threshold": {}}
        streams = {}
        if tv_hour is not None:
            print(f"  scanning {len(tv_hour) / SR / 3600:.1f} h of TV for false accepts...", flush=True)
            engine.reset()
            streams["tv"] = (engine.scores(tv_hour), len(tv_hour) / SR / 3600)
        if speech is not None:
            print(f"  scanning {len(speech) / SR / 3600:.1f} h of audiobook speech for false accepts...", flush=True)
            engine.reset()
            streams["speech"] = (engine.scores(speech), len(speech) / SR / 3600)
        for t in thresholds:
            entry = {}
            for key, p in peaks.items():
                if p:
                    negative = key in negatives
                    entry[("false accept " if negative else "recall ") + key] = round(
                        float(np.mean(np.array(p) >= t)), 3
                    )
            for name, (scores, hours) in streams.items():
                entry[f"false accepts/hour ({name})"] = round(false_accepts(scores, t, SR / engine.frame) / hours, 2)
            row["by_threshold"][t] = entry
        row["engine"] = type(engine).__name__
        results[path.name] = row
        print(f"\n== {path.name}  ({json.dumps(row['clips'])})")
        for t, entry in row["by_threshold"].items():
            print(f"  threshold {t}: " + ", ".join(f"{k} {v}" for k, v in entry.items()))
    for name, row in results.items():
        out = BENCH / f"results_v3_{name}.json"
        out.write_text(json.dumps(row, indent=2))
        print(f"Saved {out}")


def compare() -> None:
    """One table per phrase: each model at its best threshold (highest recall with at most 1 false accept/hour)."""
    rows = {p.stem.removeprefix("results_"): json.loads(p.read_text()) for p in sorted(BENCH.glob("results_*.json"))}
    for name, row in rows.items():
        print(f"\n== {name} ({row.get('engine', '?')})")
        for t, entry in row["by_threshold"].items():
            print(f"  t={t:<5} " + "  ".join(f"{k}={v}" for k, v in entry.items()))


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("make-clips")
    sub.add_parser("compare")
    r = sub.add_parser("run")
    r.add_argument("models", nargs="+", type=Path)
    r.add_argument("--thresholds", type=float, nargs="+", default=[0.3, 0.5, 0.7, 0.9, 0.95, 0.97])
    r.add_argument("--fa-hours", type=float, default=5.0, help="hours of LibriSpeech to scan for false accepts")
    args = parser.parse_args()
    if args.cmd == "make-clips":
        make_clips()
    elif args.cmd == "compare":
        compare()
    else:
        run(args.models, args.thresholds, args.fa_hours)


if __name__ == "__main__":
    sys.exit(main())
