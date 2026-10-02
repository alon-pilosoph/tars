"""Compare speech recognizers as the wake double-check: does it hear "hey TARS" and reject lookalikes?

    DATA/eval/.venv/bin/python tools/verifier_bench.py CANDIDATE [--threads N] [--user voice_data/<name>/laptop]

CANDIDATE: vosk | kws | moonshine-tiny | moonshine-base | parakeet | whisper-tiny | whisper-base | whisper-small
(-hot: biased toward "TARS"). vosk runs as the assistant does, with verify.PHRASES' grammar. The others need
`uv pip install sherpa-onnx faster-whisper` in that environment, and sherpa-onnx's models unpacked in
DATA/asr_models. This is the comparison that picked Vosk (docs/wake-word.md); TARS itself uses only Vosk.
Each clip is heard as-is (clean), under TV or babble at 5 dB, and through a real recorded room with TV at 10 dB.
Only test data is used: the held-out OpenAI voices, every take of the owner's recordings (with --user), and
interference no model trains on. Writes DATA/results/verifier_<candidate>.json.
"""

import json
import re
import sys
import time
from itertools import pairwise
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "src")]
from training.audio import CONDITIONS, Interference, conditioned
from training.common import SR, Layout, add_user_arg, parser, test_sets
from voice_assistant.verify import PHRASES, ensure_model

WAKE = PHRASES["hey tars"]
# "darts" is how a soft t often comes out.
ACCEPT_WORDS = {"tars", "tarz", "tarse", "darts"}


def accepted(text: str) -> bool:
    words = re.sub(r"[^a-z' ]", " ", text.lower().replace("tar's", "tars")).split()
    return any(a == "hey" and b in ACCEPT_WORDS for a, b in pairwise(words))


def vosk(models, threads):
    from vosk import KaldiRecognizer, Model, SetLogLevel

    SetLogLevel(-1)
    model = Model(str(ensure_model(REPO / "models")))
    grammar = json.dumps(WAKE["accept"] + WAKE["lookalikes"] + ["[unk]"])

    def hear(pcm):
        r = KaldiRecognizer(model, SR, grammar)
        r.AcceptWaveform(pcm.tobytes())
        return json.loads(r.FinalResult())["text"]

    return hear


def kws(models, threads):
    """sherpa-onnx keyword spotter: listens only for the listed phrases (lookalikes included, so they can win)."""
    import sherpa_onnx

    d = models / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
    # A streaming spotter fires as soon as a phrase completes, so a lookalike that is the start of another
    # ("hey tar" in "hey tars") would always win: leave those out.
    every = WAKE["accept"] + WAKE["lookalikes"]
    phrases = [p.upper() for p in WAKE["accept"]] + [
        p.upper() for p in WAKE["lookalikes"] if not any(o != p and o.startswith(p) for o in every)
    ]
    tokens = sherpa_onnx.text2token(
        phrases, tokens=str(d / "tokens.txt"), tokens_type="bpe", bpe_model=str(d / "bpe.model")
    )
    keywords = models / "kws_keywords.txt"
    keywords.write_text(
        "".join(f"{' '.join(t)} @{p.replace(' ', '_')}\n" for p, t in zip(phrases, tokens, strict=True))
    )
    spotter = sherpa_onnx.KeywordSpotter(
        tokens=str(d / "tokens.txt"),
        encoder=str(d / "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
        decoder=str(d / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
        joiner=str(d / "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"),
        keywords_file=str(keywords),
        num_threads=threads,
        keywords_threshold=0.25,
        keywords_score=1.0,
    )

    def hear(pcm):
        s = spotter.create_stream()
        s.accept_waveform(SR, pcm.astype(np.float32) / 32768)
        s.accept_waveform(SR, np.zeros(SR // 2, np.float32))  # flush the streaming model
        s.input_finished()
        found = []
        while spotter.is_ready(s):
            spotter.decode_stream(s)
            if k := spotter.get_result(s):
                found.append(k.replace("_", " ").lower())
                spotter.reset_stream(s)
        return " ".join(found)

    return hear


def offline(recognizer):
    def hear(pcm):
        s = recognizer.create_stream()
        s.accept_waveform(SR, pcm.astype(np.float32) / 32768)
        recognizer.decode_stream(s)
        return s.result.text

    return hear


def moonshine(size):
    def make(models, threads):
        import sherpa_onnx

        d = models / f"sherpa-onnx-moonshine-{size}-en-int8"
        return offline(
            sherpa_onnx.OfflineRecognizer.from_moonshine(
                preprocessor=str(d / "preprocess.onnx"),
                encoder=str(d / "encode.int8.onnx"),
                uncached_decoder=str(d / "uncached_decode.int8.onnx"),
                cached_decoder=str(d / "cached_decode.int8.onnx"),
                tokens=str(d / "tokens.txt"),
                num_threads=threads,
            )
        )

    return make


def parakeet(models, threads, hot=False):
    import sherpa_onnx

    d = models / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
    extra = {}
    if hot:  # bias the decoder toward the word it has never seen
        hotwords = models / "parakeet_hotwords.txt"
        hotwords.write_text("TARS\nHEY TARS\n")
        extra = {"decoding_method": "modified_beam_search", "hotwords_file": str(hotwords), "hotwords_score": 2.0}
    return offline(
        sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(d / "encoder.int8.onnx"),
            decoder=str(d / "decoder.int8.onnx"),
            joiner=str(d / "joiner.int8.onnx"),
            tokens=str(d / "tokens.txt"),
            model_type="nemo_transducer",
            num_threads=threads,
            **extra,
        )
    )


def whisper(size, hotwords=None):
    def make(models, threads):
        from faster_whisper import WhisperModel

        model = WhisperModel(f"{size}.en", device="cpu", compute_type="int8", cpu_threads=threads)

        def hear(pcm):
            segments, _ = model.transcribe(
                pcm.astype(np.float32) / 32768,
                language="en",
                beam_size=5,
                condition_on_previous_text=False,
                vad_filter=False,
                hotwords=hotwords,
            )
            return " ".join(s.text for s in segments)

        return hear

    return make


CANDIDATES = {
    "vosk": vosk,
    "kws": kws,
    "moonshine-tiny": moonshine("tiny"),
    "moonshine-base": moonshine("base"),
    "parakeet": parakeet,
    "parakeet-hot": lambda m, t: parakeet(m, t, hot=True),
    "whisper-tiny": whisper("tiny"),
    "whisper-base": whisper("base"),
    "whisper-small": whisper("small"),
    "whisper-base-hot": whisper("base", "TARS"),
    "whisper-small-hot": whisper("small", "TARS"),
}


def main() -> None:
    p = parser(__doc__)
    p.add_argument("candidate", choices=CANDIDATES)
    p.add_argument("--threads", type=int, default=2)
    add_user_arg(p)
    args = p.parse_args()
    layout = Layout(args.data)
    hear = CANDIDATES[args.candidate](layout.root / "asr_models", args.threads)
    sets = test_sets(layout, args.user, all_user=True)
    hear(np.zeros(SR, np.int16))  # warm up, so loading isn't timed
    heard: dict[str, list[str]] = {}
    seconds, n = 0.0, 0
    for cond, name, _should, clip in conditioned(
        sets, Interference(layout.interference), np.random.default_rng(0), CONDITIONS[:4]
    ):
        t0 = time.perf_counter()
        heard.setdefault(f"{name}@{cond}", []).append(hear(clip))
        seconds += time.perf_counter() - t0
        n += 1
    results, examples = {}, {}
    for key, outs in heard.items():
        name, cond = key.split("@")
        should = sets[name][1]
        results[key] = round(float(np.mean([accepted(o) for o in outs])), 3)
        examples[key] = sorted({o.strip() for o in outs if accepted(o) != should})[:8]
        print(
            f"{args.candidate:<15} {cond:<17} {name:<28} accepts {results[key]:5.0%}  "
            f"{'(want 100%)' if should else '(want 0%)'}"
        )
    ms = seconds / n * 1000
    print(f"{args.candidate}: {ms:.0f} ms per clip on {args.threads} threads")
    layout.results.mkdir(parents=True, exist_ok=True)
    out = layout.results / f"verifier_{args.candidate}.json"
    out.write_text(
        json.dumps(
            {"accept_rates": results, "ms_per_clip": ms, "threads": args.threads, "mistakes": examples}, indent=2
        )
    )


if __name__ == "__main__":
    main()
