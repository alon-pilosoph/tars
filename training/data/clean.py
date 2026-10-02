"""Keep only synthetic clips that sound like what they're labeled as.

    uv run --group training python -m training.data.clean   (the repo's environment: the assistant's Vosk model)

Positives must be heard as "hey tars" (or "hey darts", a soft t) by Vosk restricted to the wake phrase, its
lookalikes and common mushy readings ("hate us", "haters"). Lookalikes must NOT be heard as "hey tars".
Writes DATA/clean/<source>_<kind>.txt with the paths that pass, plus summary.json.

The cleaned lists feed voice conversion (vc_librispeech.py), not the shipped wake model: training the wake model
on cleaned clips made it too strict (see docs/wake-word.md).
"""

import itertools
import json
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from training.common import REPO, SR, Layout, log, parser

_model = _grammar = None


def _init():
    global _model, _grammar
    sys.path.insert(0, str(REPO / "src"))
    from vosk import Model, SetLogLevel

    from voice_assistant.verify import PHRASES, ensure_model

    SetLogLevel(-1)
    _model = Model(str(ensure_model(REPO / "models")))
    spec = PHRASES["hey tars"]
    _grammar = json.dumps(spec["accept"] + spec["lookalikes"] + ["hater", "haters", "hate us", "hate", "[unk]"])


def hear(path: str) -> tuple[str, str]:
    import soundfile as sf
    from vosk import KaldiRecognizer

    audio, sr = sf.read(path, dtype="int16")
    if sr != SR or audio.ndim != 1:
        return path, "<bad format>"
    r = KaldiRecognizer(_model, SR, _grammar)
    r.AcceptWaveform(audio.tobytes())
    return path, json.loads(r.FinalResult())["text"]


def is_wake(text: str) -> bool:
    w = text.split()
    return any(a == "hey" and b in ("tars", "darts") for a, b in itertools.pairwise(w))


def main():
    p = parser(__doc__)
    p.add_argument("--workers", type=int, default=2)
    args = p.parse_args()
    layout = Layout(args.data)
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
                log(
                    f"{source}/{kind}: kept {len(keep)}/{len(files)} ({len(keep) / max(1, len(files)):.0%})  "
                    f"dropped as {dict(heard.most_common(4))}"
                )
    (out / "summary.json").write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
