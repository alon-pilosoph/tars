"""Benchmark stage 1 alone: wake models' scores on audio they never trained on, at several thresholds.

    DATA/mww/.venv/bin/python tools/wakeword_bench.py run MODEL.tflite   # microWakeWord, from its training env
    uv run python tools/wakeword_bench.py run MODEL.onnx                 # openWakeWord, from this repo's env
    uv run python tools/wakeword_bench.py compare                        # every model scored so far

Measures, per model and threshold:
  - recall on "hey TARS" said by the 3 held-out OpenAI voices (DATA/bench/clips_heldout), clean and in noise
  - false accepts on their lookalikes ("hey stars", "hey cars"...) and on the other phrase ("TARS stop")
  - false accepts per hour on an hour of test TV and on audiobooks (LibriSpeech test-clean)
With --recordings voice_data, the owner's recordings too (the test half of each session, voice_data/<name>/<mic>/):
the best test. The end-to-end benchmark, with the double-check, is training/eval/pipeline.py.
Writes DATA/results/wakeword_<model>.json.
"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from training.audio import Interference, long_speech, read_wav
from training.common import SR, Layout, parser, user_split

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


class OpenWakeWord:
    frame = 1280  # 80 ms

    def __init__(self, path: Path):
        from openwakeword.model import Model

        self.model = Model(wakeword_models=[str(path)], inference_framework="onnx")

    def reset(self):
        self.model.reset()

    def scores(self, audio: np.ndarray) -> np.ndarray:
        return np.array(
            [
                max(self.model.predict(audio[i : i + self.frame]).values())
                for i in range(0, len(audio) - self.frame + 1, self.frame)
            ]
        )


class MicroWakeWord:
    frame = 480  # one prediction per 30 ms: the streaming model strides over three 10 ms feature slices

    def __init__(self, path: Path):
        from microwakeword.inference import Model

        self._model_class, self._path = Model, str(path)
        self.reset()

    def reset(self):
        # The streaming model keeps its state inside the interpreter; a fresh one is the only clean reset.
        self.model = self._model_class(self._path)

    def scores(self, audio: np.ndarray) -> np.ndarray:
        return np.array(self.model.predict_clip(audio, step_ms=10), dtype=np.float32)


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


def bench_sets(layout: Layout, recordings: Path | None, target: str) -> tuple[dict, dict]:
    """(positives, negatives) as {name: files}. The owner's recordings count per session (person and mic), so a new
    mic gets its own numbers, and only their test half is used."""
    other = "hey_tars" if target == "tars_stop" else "tars_stop"
    positives = {"heldout_voices": sorted((layout.heldout / target).glob("*.wav"))}
    negatives = {
        "near_miss_heldout": sorted((layout.heldout / f"{target}_near_miss").glob("*.wav"))
        + sorted((layout.heldout / other).glob("*.wav"))
    }
    if recordings is not None:
        for session in sorted(p for p in recordings.glob("*/*") if (p / target).is_dir()):
            positives[f"real_{session.parent.name}_{session.name}"] = user_split(session, target)[1]
            negatives[f"speech_{session.parent.name}_{session.name}"] = user_split(session, "speech")[1]
    return positives, negatives


def run(layout: Layout, models: list[Path], recordings: Path | None, thresholds: list[float], fa_hours: float) -> None:
    rng = np.random.default_rng(0)
    noise = Interference(layout.interference, banks=("babble", "tv", "music", "noise"))
    tv_hour = np.concatenate([read_wav(f) for f in sorted((layout.interference / "tv_hour").glob("*.wav"))])
    streams_audio = {"tv": tv_hour}
    if fa_hours > 0:
        streams_audio["speech"] = long_speech(layout.librispeech_test, fa_hours)
    layout.results.mkdir(parents=True, exist_ok=True)
    for path in models:
        engine = MicroWakeWord(path) if path.suffix == ".tflite" else OpenWakeWord(path)
        positives, negatives = bench_sets(layout, recordings, "tars_stop" if "stop" in path.stem else "hey_tars")
        peaks = {}
        print(f"{path.name}: scoring clips...", flush=True)
        for name, files in positives.items():
            clips = [read_wav(f) for f in files]
            for condition in CONDITIONS:
                peaks[f"{name}@{condition[0]}"] = [clip_peak(engine, noise.apply(c, condition, rng)) for c in clips]
        for name, files in negatives.items():
            peaks[name] = [clip_peak(engine, read_wav(f)) for f in files]
        streams = {}
        for name, audio in streams_audio.items():
            print(f"  scanning {len(audio) / SR / 3600:.1f} h of {name} for false accepts...", flush=True)
            engine.reset()
            streams[name] = (engine.scores(audio), len(audio) / SR / 3600)
        row = {"engine": type(engine).__name__, "clips": {k: len(v) for k, v in peaks.items()}, "by_threshold": {}}
        for t in thresholds:
            entry = {}
            for key, p in peaks.items():
                if p:
                    what = "false accept " if key in negatives else "recall "
                    entry[what + key] = round(float(np.mean(np.array(p) >= t)), 3)
            for name, (scores, hours) in streams.items():
                entry[f"false accepts/hour ({name})"] = round(false_accepts(scores, t, SR / engine.frame) / hours, 2)
            row["by_threshold"][t] = entry
        out = layout.results / f"wakeword_{path.stem}.json"
        out.write_text(json.dumps(row, indent=2))
        print(f"\n== {path.name}  ({json.dumps(row['clips'])})")
        for t, entry in row["by_threshold"].items():
            print(f"  threshold {t}: " + ", ".join(f"{k} {v}" for k, v in entry.items()))
        print(f"Saved {out}")


def compare(layout: Layout) -> None:
    for path in sorted(layout.results.glob("wakeword_*.json")):
        row = json.loads(path.read_text())
        print(f"\n== {path.stem.removeprefix('wakeword_')} ({row.get('engine', '?')})")
        for t, entry in row["by_threshold"].items():
            print(f"  t={t:<5} " + "  ".join(f"{k}={v}" for k, v in entry.items()))


def main() -> None:
    p = parser(__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("compare")
    r = sub.add_parser("run")
    r.add_argument("models", nargs="+", type=Path)
    r.add_argument("--recordings", type=Path, help="the owner's recordings: voice_data/ (a folder per person and mic)")
    r.add_argument("--thresholds", type=float, nargs="+", default=[0.3, 0.5, 0.7, 0.9, 0.95, 0.97])
    r.add_argument(
        "--fa-hours", type=float, default=5.0, help="hours of audiobooks to scan for false accepts (0: none)"
    )
    args = p.parse_args()
    layout = Layout(args.data)
    if args.cmd == "compare":
        compare(layout)
    else:
        run(layout, args.models, args.recordings, args.thresholds, args.fa_hours)


if __name__ == "__main__":
    main()
