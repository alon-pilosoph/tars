"""Does TARS cut you off? The end of the turn, today's way (Silero VAD + Smart Turn) against Deepgram's Flux.

    uv run python tools/turn_bench.py                     # both, on the owner's recorded sentences
    uv run python tools/turn_bench.py --flux-eot 0.8      # a more patient Flux

The owner's own sentences (voice_data/*/*/speech/), each played twice: whole, to see how soon each way knows the
turn is over; and with a mid-sentence pause (after "and", "the", "with"...; 0.5, 0.8 or 1.2 s of real room tone)
where the sentence obviously isn't finished, to count the times each way ends the turn inside it: a cut-off. Word
times come from Deepgram once, cached in voice_data/bench/turns/. Flux hears every clip streamed at real-time pace,
several at once; a run costs about 10 cents.
"""

import argparse
import dataclasses
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).parent))
from latency_bench import RoomTone

from voice_assistant.audio import BLOCK_SAMPLES, SAMPLE_RATE
from voice_assistant.config import load_config
from voice_assistant.recorder import make_recorder

REPO = Path(__file__).parents[1]
UNFINISHED = {
    "and",
    "but",
    "or",
    "so",
    "because",
    "the",
    "a",
    "an",
    "to",
    "for",
    "with",
    "of",
    "my",
    "your",
    "in",
    "on",
    "at",
    "about",
    "that",
    "is",
    "some",
    "if",
}
PAUSES_S = (0.5, 0.8, 1.2)
LEAD_S, TAIL_S = 0.5, 4.0
EDGE_S = 0.03  # kept around each word when splicing, so no word loses its edge


@dataclasses.dataclass
class Clip:
    name: str
    audio: np.ndarray
    speech_end_s: float  # where the last word ends, from the clip's start
    resume_s: float | None  # a paused clip: where the rest of the sentence starts; ending before it is a cut-off


def words(path: Path, key: str, cache: Path) -> list[dict]:
    if cache.exists():
        return json.loads(cache.read_text())
    r = httpx.post(
        "https://api.deepgram.com/v1/listen?model=nova-3",
        headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
        content=path.read_bytes(),
        timeout=60,
    )
    r.raise_for_status()
    found = r.json()["results"]["channels"][0]["alternatives"][0]["words"]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(found))
    return found


def clips(root: Path, key: str, room: RoomTone) -> list[Clip]:
    out = []
    for path in sorted((root / "voice_data").glob("*/*/speech/*.wav")):
        pcm = sf.read(path, dtype="int16")[0]
        ws = words(path, key, root / "voice_data/bench/turns" / f"{path.parent.parent.parent.name}-{path.stem}.json")
        if len(ws) < 5:
            continue
        lead = room(int(LEAD_S * SAMPLE_RATE))
        first, last = ws[0]["start"], ws[-1]["end"]
        body = pcm[max(0, int((first - EDGE_S) * SAMPLE_RATE)) : int((last + EDGE_S) * SAMPLE_RATE)]
        name = f"{path.parent.parent.parent.name}/{path.stem}"
        out.append(
            Clip(
                f"{name} whole",
                np.concatenate([lead, body, room(int(TAIL_S * SAMPLE_RATE))]),
                LEAD_S + len(body) / SAMPLE_RATE,
                None,
            )
        )
        # The unfinished word nearest the middle, never the first two or last two words.
        middle = [i for i in range(2, len(ws) - 2) if ws[i]["word"].lower().strip(".,?!") in UNFINISHED]
        if not middle:
            continue
        i = min(middle, key=lambda j: abs(j - len(ws) / 2))
        cut, resume = ws[i]["end"] + EDGE_S, ws[i + 1]["start"] - EDGE_S
        before = pcm[max(0, int((first - EDGE_S) * SAMPLE_RATE)) : int(cut * SAMPLE_RATE)]
        after = pcm[int(resume * SAMPLE_RATE) : int((last + EDGE_S) * SAMPLE_RATE)]
        for pause in PAUSES_S:
            gap = room(int(pause * SAMPLE_RATE))
            audio = np.concatenate([lead, before, gap, after, room(int(TAIL_S * SAMPLE_RATE))])
            resume_at = LEAD_S + (len(before) + len(gap)) / SAMPLE_RATE
            out.append(
                Clip(
                    f"{name} pause {pause}s after {ws[i]['word']!r}",
                    audio,
                    resume_at + len(after) / SAMPLE_RATE,
                    resume_at,
                )
            )
    return out


class ListMic:
    """The clip in 80 ms blocks, as fast as asked (the local detectors don't care about the clock)."""

    def __init__(self, audio: np.ndarray):
        self.blocks = audio[: len(audio) // BLOCK_SAMPLES * BLOCK_SAMPLES].reshape(-1, BLOCK_SAMPLES)
        self.read_count = 0

    def read(self) -> np.ndarray:
        if self.read_count >= len(self.blocks):
            raise EOFError
        self.read_count += 1
        return self.blocks[self.read_count - 1]

    def clear(self) -> None:
        pass


def local_end(recorder, clip: Clip) -> float | None:
    """When today's recorder stops listening, in seconds from the clip's start (None: it never did)."""
    mic = ListMic(clip.audio)
    try:
        pcm = recorder.record(mic, start_timeout_s=3.0)
    except EOFError:
        return None
    return mic.read_count * BLOCK_SAMPLES / SAMPLE_RATE if pcm else None


def flux_end(key: str, clip: Clip, eot: float, eager: float) -> dict:
    """Stream the clip to Flux at real-time pace; when (wall clock, from the clip's start) it says each thing."""
    from websockets.sync.client import connect

    url = (
        f"wss://api.deepgram.com/v2/listen?model=flux-general-en&encoding=linear16&sample_rate={SAMPLE_RATE}"
        f"&eot_threshold={eot}&eager_eot_threshold={eager}"
    )
    seen: dict = {"eager": [], "resumed": [], "end": None}
    with connect(url, additional_headers={"Authorization": f"Token {key}"}, open_timeout=10) as ws:
        t0 = time.perf_counter()

        def feed():
            for n, i in enumerate(range(0, len(clip.audio), BLOCK_SAMPLES)):
                wait = t0 + n * BLOCK_SAMPLES / SAMPLE_RATE - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
                try:
                    ws.send(clip.audio[i : i + BLOCK_SAMPLES].tobytes())
                except Exception:  # noqa: BLE001 - the socket closed once the turn ended
                    return

        threading.Thread(target=feed, daemon=True).start()
        deadline = t0 + len(clip.audio) / SAMPLE_RATE + 1.0
        while time.perf_counter() < deadline:
            try:
                event = json.loads(ws.recv(timeout=0.5))
            except TimeoutError:
                continue
            t = time.perf_counter() - t0
            kind = event.get("event")
            if kind == "EagerEndOfTurn":
                seen["eager"].append(t)
            elif kind == "TurnResumed":
                seen["resumed"].append(t)
            elif kind == "EndOfTurn" and seen["end"] is None:
                seen["end"] = t
                break
    return seen


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--flux-eot", type=float, default=0.7, help="Flux's eot_threshold (0.5-1.0)")
    p.add_argument("--flux-eager", type=float, default=0.5, help="Flux's eager_eot_threshold")
    p.add_argument("--skip-local", action="store_true")
    args = p.parse_args()
    key = dotenv_values(REPO / ".env")["DEEPGRAM_API_KEY"]
    cfg = load_config(REPO / "config.toml")
    room = RoomTone(REPO, cfg.recorder, np.random.default_rng(0))
    todo = clips(REPO, key, room)
    whole = [c for c in todo if c.resume_s is None]
    paused = [c for c in todo if c.resume_s is not None]
    print(f"{len(whole)} whole sentences, {len(paused)} with a pause")

    def report(name, ends):
        waits = [ends[c.name] - c.speech_end_s for c in whole if ends.get(c.name) is not None]
        cut = [c for c in paused if ends.get(c.name) is not None and ends[c.name] < c.resume_s]
        by_pause = {s: sum(1 for c in cut if f"pause {s}s" in c.name) for s in PAUSES_S}
        n_each = len(paused) // len(PAUSES_S)
        print(f"\n{name}")
        if waits:
            print(
                f"  turn over after you stop: median {statistics.median(waits):.2f}s, "
                f"90% within {sorted(waits)[int(0.9 * (len(waits) - 1))]:.2f}s ({len(waits)}/{len(whole)} ended)"
            )
        print(
            f"  cut-offs: {len(cut)}/{len(paused)} ("
            + ", ".join(f"{s}s pause: {k}/{n_each}" for s, k in by_pause.items())
            + ")"
        )
        for c in cut:
            print(f"    - {c.name}: ended {c.resume_s - ends[c.name]:.2f}s before the rest")

    if not args.skip_local:
        recorder = make_recorder(cfg, REPO)
        report(
            f"Today: Silero + Smart Turn (end_silence_s {cfg.recorder.end_silence_s}, max_pause_s "
            f"{cfg.recorder.max_pause_s})",
            {c.name: local_end(recorder, c) for c in todo},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(
            zip(
                [c.name for c in todo],
                pool.map(lambda c: flux_end(key, c, args.flux_eot, args.flux_eager), todo),
                strict=True,
            )
        )
    report(f"Flux (eot_threshold {args.flux_eot})", {n: r["end"] for n, r in results.items()})
    eager = [results[c.name]["eager"][0] - c.speech_end_s for c in whole if results[c.name]["eager"]]
    wasted = sum(1 for c in paused if results[c.name]["eager"] and results[c.name]["resumed"])
    if eager:
        print(
            f"  EagerEndOfTurn (where a draft would start): median {statistics.median(eager):.2f}s after you stop; "
            f"{wasted}/{len(paused)} paused sentences had one taken back (a draft thrown away)"
        )


if __name__ == "__main__":
    main()
