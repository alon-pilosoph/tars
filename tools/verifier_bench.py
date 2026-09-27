"""Compare speech recognizers as the wake double-check: does it hear "hey TARS" and reject lookalikes?

    ~/asr_eval/.venv/bin/python tools/verifier_bench.py CANDIDATE [--threads N]

CANDIDATE: vosk | kws | moonshine-tiny | moonshine-base | parakeet | whisper-tiny | whisper-base | whisper-small.
Each clip is heard as-is (clean), under TV or babble at 5 dB, and through a real recorded room with TV at 10 dB.
Only test data is used: the user's recordings, held-out OpenAI voices, and interference no model trains on.
Writes ~/wakeword_bench/verifier_<candidate>.json.
"""

import argparse
import json
import re
import sys
import time
from itertools import pairwise
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import wakeword_bench as wb

REPO = Path(__file__).parents[1]
MODELS = Path.home() / "asr_eval" / "models"
USER = REPO / "voice_data" / "alon" / "laptop"
HELD = wb.BENCH / "clips_heldout"

LOOKALIKES = [
    "hey cars",
    "hey bars",
    "hey mars",
    "hey stars",
    "hey lars",
    "hey parts",
    "hey guitars",
    "hey guards",
    "hey tara",
    "hey jarvis",
    "hey there",
    "hey star",
    "hey tarot",
    "hey car",
    "hey bar",
    "hey tar",
    "tars stop",
    "stop",
]
# What counts as hearing the wake phrase. "darts" is how a soft t often comes out; nobody says "hey darts".
ACCEPT_WORDS = {"tars", "tarz", "tarse", "darts"}

SETS = {  # name: (files, should the check accept these?)
    "your hey TARS": (sorted((USER / "hey_tars").glob("*.wav")), True),
    "other voices hey TARS": (sorted((HELD / "hey_tars").glob("*.wav")), True),
    "your lookalikes": (sorted((USER / "hey_tars_lookalikes").glob("*.wav")), False),
    "other lookalikes": (sorted((HELD / "hey_tars_near_miss").glob("*.wav")), False),
    "your TARS stop": (sorted((USER / "tars_stop").glob("*.wav")), False),
    "your sentences": (sorted((USER / "speech").glob("*.wav")), False),
}
CONDITIONS = [
    ("clean", None, None, False),
    ("tv_5dB", "tv", 5, False),
    ("babble_5dB", "babble", 5, False),
    ("far_room+tv_10dB", "tv", 10, True),
]


def accepted(text: str) -> bool:
    words = re.sub(r"[^a-z' ]", " ", text.lower().replace("tar's", "tars")).split()
    return any(a == "hey" and b in ACCEPT_WORDS for a, b in pairwise(words))


# ---------- candidates: each turns 16 kHz int16 audio into text ----------


def vosk(threads):
    from vosk import KaldiRecognizer, Model, SetLogLevel

    SetLogLevel(-1)
    model = Model(str(REPO / "models" / "vosk-model-small-en-us-0.15"))
    grammar = json.dumps(["hey tars", "hey darts"] + LOOKALIKES + ["hey", "[unk]"])

    def hear(pcm):
        r = KaldiRecognizer(model, 16000, grammar)
        r.AcceptWaveform(pcm.tobytes())
        return json.loads(r.FinalResult())["text"]

    return hear


def kws(threads):
    """sherpa-onnx keyword spotter: listens only for the listed phrases (lookalikes included, so they can win)."""
    import sherpa_onnx

    d = MODELS / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
    # A streaming spotter fires as soon as a phrase completes, so a lookalike that is the start of another
    # ("hey tar" in "hey tars") would always win: leave those out.
    phrases = ["HEY TARS", "HEY DARTS"] + [
        p.upper()
        for p in LOOKALIKES
        if not any(o != p and o.startswith(p) for o in LOOKALIKES + ["hey tars", "hey darts"])
    ]
    tokens = sherpa_onnx.text2token(
        phrases, tokens=str(d / "tokens.txt"), tokens_type="bpe", bpe_model=str(d / "bpe.model")
    )
    keywords = Path(wb.BENCH / "kws_keywords.txt")
    keywords.write_text("".join(" ".join(t) + f" @{p.replace(' ', '_')}\n" for p, t in zip(phrases, tokens)))
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
        s.accept_waveform(16000, pcm.astype(np.float32) / 32768)
        s.accept_waveform(16000, np.zeros(8000, np.float32))  # flush the streaming model
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
        s.accept_waveform(16000, pcm.astype(np.float32) / 32768)
        recognizer.decode_stream(s)
        return s.result.text

    return hear


def moonshine(size):
    def make(threads):
        import sherpa_onnx

        d = MODELS / f"sherpa-onnx-moonshine-{size}-en-int8"
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


def parakeet(threads, hot=False):
    import sherpa_onnx

    d = MODELS / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
    extra = {}
    if hot:  # bias the decoder toward the word it has never seen
        hotwords = wb.BENCH / "parakeet_hotwords.txt"
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
    def make(threads):
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
    "parakeet-hot": lambda t: parakeet(t, hot=True),
    "whisper-tiny": whisper("tiny"),
    "whisper-base": whisper("base"),
    "whisper-small": whisper("small"),
    "whisper-base-hot": whisper("base", "TARS"),
    "whisper-small-hot": whisper("small", "TARS"),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", choices=CANDIDATES)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    hear = CANDIDATES[args.candidate](args.threads)
    rng = np.random.default_rng(0)
    banks = {n: wb.bank(n) for n in ["tv", "babble"]}
    rooms = wb.bank("rooms", limit=1000)
    hear(np.zeros(16000, np.int16))  # warm up
    results, examples, seconds, n = {}, {}, 0.0, 0
    for cond, noise, snr, room in CONDITIONS:
        for name, (files, should) in SETS.items():
            outs = []
            for f in files:
                clip = wb.read_wav(f)
                clip = wb.in_room(clip, rooms[rng.integers(len(rooms))]) if room else clip
                clip = wb.mix(clip, banks[noise][rng.integers(len(banks[noise]))], snr, rng) if noise else clip
                t0 = time.perf_counter()
                outs.append(hear(clip))
                seconds += time.perf_counter() - t0
                n += 1
            rate = float(np.mean([accepted(o) for o in outs]))
            results[f"{name}@{cond}"] = round(rate, 3)
            wrong = [o for o in outs if accepted(o) != should]
            examples[f"{name}@{cond}"] = sorted({o.strip() for o in wrong})[:8]
            print(
                f"{args.candidate:<15} {cond:<17} {name:<22} accepts {rate:5.0%}  {'(want 100%)' if should else '(want 0%)'}",
                flush=True,
            )
    ms = seconds / n * 1000
    print(f"{args.candidate}: {ms:.0f} ms per clip on {args.threads} threads")
    out = wb.BENCH / f"verifier_{args.candidate}.json"
    out.write_text(
        json.dumps(
            {"accept_rates": results, "ms_per_clip": ms, "threads": args.threads, "mistakes": examples}, indent=2
        )
    )


if __name__ == "__main__":
    main()
