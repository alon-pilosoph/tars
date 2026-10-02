"""Does TARS cut you off? The end of the turn, Deepgram's Flux against the local fallback (Silero VAD + Smart Turn).

    uv run python tools/turn_bench.py                     # both, on the recorded sentences

The household's own recorded sentences (voice_data/<person>/<mic>/speech/), each played twice: whole, to see how
soon each way knows the turn is over; and with a mid-sentence pause (after "and", "the", "with"...; 0.5, 0.8 or
1.2 s of real room tone) where the sentence obviously isn't finished, to count the times each way ends the turn
inside it: a cut-off. Word times come from Deepgram once, cached in voice_data/bench/turns/. Flux hears every clip
streamed at real-time pace, several at once; a run costs about 10 cents.
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

sys.path.insert(0, str(Path(__file__).parent))
from latency_bench import RoomTone

from voice_assistant.__main__ import api_key
from voice_assistant.audio import BLOCK_SAMPLES, SAMPLE_RATE
from voice_assistant.config import load_config
from voice_assistant.recorder import BACKSTOP_S, make_recorder
from voice_assistant.stt import FLUX_EAGER_EOT, FLUX_EOT, FLUX_TIMEOUT_MS, FluxTranscriber

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
        person, mic = path.parents[2].name, path.parents[1].name  # the same person can have a session per mic
        ws = words(path, key, root / "voice_data/bench/turns" / f"{person}-{mic}-{path.stem}.json")
        if len(ws) < 5:
            continue
        lead = room(int(LEAD_S * SAMPLE_RATE))
        first, last = ws[0]["start"], ws[-1]["end"]
        body = pcm[max(0, int((first - EDGE_S) * SAMPLE_RATE)) : int((last + EDGE_S) * SAMPLE_RATE)]
        name = f"{person}/{mic}/{path.stem}"
        out.append(
            Clip(
                f"{name} whole",
                np.concatenate([lead, body, room(int(TAIL_S * SAMPLE_RATE))]),
                LEAD_S + len(body) / SAMPLE_RATE,
                None,
            )
        )
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
    """When the local recorder stops listening, in seconds from the clip's start (None: it never did)."""
    mic = ListMic(clip.audio)
    try:
        pcm = recorder.record(mic, start_timeout_s=3.0)
    except EOFError:
        return None
    return mic.read_count * BLOCK_SAMPLES / SAMPLE_RATE if pcm else None


def flux_end(key: str, clip: Clip) -> dict:
    """Streams the clip to Flux at real-time pace with the assistant's settings. Records each event's arrival time
    and, for EndOfTurn, where in the audio Flux was (audio_window_end), both from the clip's start."""
    from websockets.sync.client import connect

    url = FluxTranscriber(key)._url
    seen: dict = {"eager": [], "resumed": [], "end": None, "end_audio": None, "error": None}
    try:
        with connect(url, additional_headers={"Authorization": f"Token {key}"}, open_timeout=10) as ws:
            t0 = time.perf_counter()

            def feed():
                for n, i in enumerate(range(0, len(clip.audio), BLOCK_SAMPLES)):
                    # A block goes out once all of it has been "heard", as from a mic.
                    wait = t0 + (n + 1) * BLOCK_SAMPLES / SAMPLE_RATE - time.perf_counter()
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
                t, kind = time.perf_counter() - t0, event.get("event")
                if kind == "EagerEndOfTurn":
                    seen["eager"].append(t)
                elif kind == "TurnResumed":
                    seen["resumed"].append(t)
                elif kind == "EndOfTurn":
                    seen["end"], seen["end_audio"] = t, event.get("audio_window_end")
                    break
    except Exception as e:  # noqa: BLE001 - one failed connection shouldn't cost the whole run
        seen["error"] = repr(e)
    return seen


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--skip-local", action="store_true")
    args = p.parse_args()
    key = api_key(REPO / ".env", "DEEPGRAM_API_KEY")
    cfg = load_config(REPO / "config.toml")
    room = RoomTone(REPO, cfg.recorder, np.random.default_rng(0))
    todo = clips(REPO, key, room)
    whole = [c for c in todo if c.resume_s is None]
    paused = [c for c in todo if c.resume_s is not None]
    print(f"{len(whole)} whole sentences, {len(paused)} with a pause")

    def report(name, ends, cut_at=None):
        """`ends`: when the turn was over; `cut_at`: where in the audio it was decided, when that's known."""
        cut_at = cut_at or ends
        waits = [ends[c.name] - c.speech_end_s for c in whole if ends.get(c.name) is not None]
        cut = [c for c in paused if cut_at.get(c.name) is not None and cut_at[c.name] < c.resume_s]
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
            print(f"    - {c.name}: ended {c.resume_s - cut_at[c.name]:.2f}s before the rest")

    if not args.skip_local:
        recorder = make_recorder(cfg, REPO)
        report(
            f"Local fallback: Silero + Smart Turn (end_silence_s {cfg.recorder.end_silence_s}, max_pause_s "
            f"{cfg.recorder.max_pause_s})",
            {c.name: local_end(recorder, c) for c in todo},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip([c.name for c in todo], pool.map(lambda c: flux_end(key, c), todo), strict=True))
    failed = {n: r["error"] for n, r in results.items() if r["error"]}
    if failed:
        print(f"\n{len(failed)} clips couldn't reach Flux, left out: {next(iter(failed.values()))}")
    # As the assistant runs it: silence past BACKSTOP_S ends the turn whatever Flux thinks.
    by_clip = {c.name: c for c in todo}
    ends, cut_at = {}, {}
    for n, r in results.items():
        if r["error"]:
            continue
        spoken_to = by_clip[n].speech_end_s
        backstop = spoken_to + BACKSTOP_S
        ends[n] = min(r["end"], backstop) if r["end"] is not None else backstop
        cut_at[n] = r["end_audio"] if r["end_audio"] is not None else ends[n]
    backstopped = sum(1 for c in whole if c.name in ends and ends[c.name] >= c.speech_end_s + BACKSTOP_S)
    report(
        f"Flux (the assistant's settings: eot_threshold {FLUX_EOT}, eager {FLUX_EAGER_EOT}, "
        f"timeout {FLUX_TIMEOUT_MS / 1000:g} s)",
        ends,
        cut_at,
    )
    print(f"  ended by the {BACKSTOP_S} s backstop instead of Flux: {backstopped}/{len(whole)} whole sentences")
    eager = []
    for c in whole:
        r = results[c.name]
        before_end = [t for t in r["eager"] if r["end"] is None or t <= r["end"]]
        if before_end:
            eager.append(before_end[-1] - c.speech_end_s)
    wasted = sum(1 for c in paused if results[c.name]["eager"] and results[c.name]["resumed"])
    if eager:
        print(
            f"  EagerEndOfTurn (where a draft would start): median {statistics.median(eager):.2f}s after you stop; "
            f"{wasted}/{len(paused)} paused sentences had one taken back (a draft thrown away)"
        )


if __name__ == "__main__":
    main()
