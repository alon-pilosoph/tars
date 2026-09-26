"""Keep only synthetic clips that sound like what they're labeled as.

    uv run python -m training.data.clean            (the repo's environment: it uses the assistant's Vosk model)
    uv run python -m training.data.clean --vc       (the voice-converted clips instead)

Positives must be heard as "hey tars" (or "hey darts", a soft t) by Vosk restricted to the wake phrase, its
lookalikes and the mushy readings we found ("hate us", "haters"). Lookalikes must NOT be heard as "hey tars".
Writes DATA/clean/<source>_<kind>.txt with the paths that pass, plus summary.json (summary_vc.json).

The cleaned lists feed voice conversion (vc_librispeech.py), not the shipped wake model: training the wake model
on cleaned clips made it too strict (see training/README.md).
"""

import itertools
import json
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from training.common import REPO, Layout, parser

_model = _grammar = None


def _init():
    global _model, _grammar
    sys.path.insert(0, str(REPO / "src"))
    from vosk import Model, SetLogLevel

    from voice_assistant.verify import PHRASES

    SetLogLevel(-1)
    _model = Model(str(REPO / "models/vosk-model-small-en-us-0.15"))
    spec = PHRASES["hey tars"]
    _grammar = json.dumps(spec["accept"] + spec["lookalikes"] + ["hater", "haters", "hate us", "hate", "[unk]"])


def hear(path: str) -> tuple[str, str]:
    import soundfile as sf
    from vosk import KaldiRecognizer

    audio, sr = sf.read(path, dtype="int16")
    if sr != 16000 or audio.ndim != 1:
        return path, "<bad format>"
    r = KaldiRecognizer(_model, 16000, _grammar)
    r.AcceptWaveform(audio.tobytes())
    return path, json.loads(r.FinalResult())["text"]


def is_wake(text: str) -> bool:
    w = text.split()
    return any(a == "hey" and b in ("tars", "darts") for a, b in itertools.pairwise(w))


def main():
    p = parser(__doc__)
    p.add_argument("--vc", action="store_true", help="check the voice-converted clips (DATA/vc)")
    p.add_argument("--workers", type=int, default=2)
    args = p.parse_args()
    layout = Layout(args.data)
    if args.vc:
        sources = {"vc": {k: layout.vc / k for k in ["positive", "near_miss"]}}
    else:
        sources = {
            s: {k: layout.clip_dir(s, "hey_tars", k) for k in ["positive", "near_miss"]}
            for s in ["piper_libritts", "piper_voices", "kokoro", "openai"]
        }
        sources["prosody"] = {"positive": layout.clip_dir("prosody", "hey_tars", "positive")}
    out = layout.clean
    out.mkdir(parents=True, exist_ok=True)
    summary = {}
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init) as pool:
        for source, kinds in sources.items():
            for kind, folder in kinds.items():
                files = sorted(str(p) for p in folder.glob("*.wav"))
                keep, heard = [], Counter()
                for path, text in pool.map(hear, files, chunksize=64):
                    ok = is_wake(text) if kind == "positive" else not is_wake(text)
                    if ok:
                        keep.append(path)
                    else:
                        heard[text] += 1
                (out / f"{source}_{kind}.txt").write_text("\n".join(keep) + "\n")
                summary[f"{source}/{kind}"] = {
                    "total": len(files),
                    "kept": len(keep),
                    "dropped_as": dict(heard.most_common(8)),
                }
                print(
                    f"{source}/{kind}: kept {len(keep)}/{len(files)} ({len(keep) / max(1, len(files)):.0%})  "
                    f"dropped as {dict(heard.most_common(4))}",
                    flush=True,
                )
    (out / ("summary_vc.json" if args.vc else "summary.json")).write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
