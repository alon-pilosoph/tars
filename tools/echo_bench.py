"""Does TARS still hear itself with the mic open? Echo cancellation, measured in the room it runs in.

    uv run python tools/echo_bench.py            # the devices in config.toml
    uv run python tools/echo_bench.py --talk     # and once more, with you talking over it

Plays recorded speech (tools/demo_audio) through the speaker with the mic open and echo cancellation on, as TARS will
when it greets you with the mic open, and keeps what the mic heard before and after cancellation. Then, for each:
how loud TARS was, what speech recognition (the local Vosk model) makes of it, and how much of it the speech
detector took for someone talking. With --talk, a last clip plays while you say "what's the weather tomorrow", to
check you still come through. Run it with the volume as TARS will use it; the canceller learns the room over the
first second or two, so the first clip is the hardest.
"""

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from voice_assistant.audio import BLOCK_SAMPLES, SAMPLE_RATE, Microphone, Speaker, find_device
from voice_assistant.config import load_config
from voice_assistant.echo import EchoCanceller
from voice_assistant.recorder import make_recorder

REPO = Path(__file__).parents[1]
PLAY_RATE = 24_000  # TARS's voices
CLIPS = [REPO / "tools/demo_audio/stacey" / f"say_0{i}.flac" for i in range(6)]


class Tap(EchoCanceller):
    """The canceller, keeping what the mic heard before and after it."""

    def __init__(self, rate: int):
        super().__init__(rate)
        self.raw, self.clean = [], []

    def heard(self, block):
        out = super().heard(block)
        self.raw.append(block.copy())
        self.clean.append(out.copy())
        return out


def words(pcm: np.ndarray, model) -> str:
    from vosk import KaldiRecognizer

    r = KaldiRecognizer(model, SAMPLE_RATE)
    data = pcm.astype(np.int16).tobytes()
    for i in range(0, len(data), 8000):
        r.AcceptWaveform(data[i : i + 8000])
    return json.loads(r.FinalResult())["text"]


def db(pcm: np.ndarray) -> float:
    return 10 * np.log10(np.mean(pcm.astype(np.float64) ** 2) + 1.0) - 90.3  # dBFS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--talk", action="store_true", help="a last clip with you talking over it")
    args = parser.parse_args()
    cfg = load_config(REPO / "config.toml")
    from vosk import Model, SetLogLevel

    SetLogLevel(-1)
    vosk = Model(str(REPO / "models/vosk-model-small-en-us-0.15"))
    vad = make_recorder(cfg, REPO, turn_model=False)._vad  # the speech detector TARS listens with
    clips = [resample_poly(sf.read(c, dtype="float32")[0], 3, 2) for c in CLIPS if c.exists()]
    if not clips:
        sys.exit("No clips in tools/demo_audio.")
    if args.talk:
        clips.append(clips[0])

    tap = Tap(SAMPLE_RATE)
    speaker = Speaker(find_device(cfg.audio.output_device, "output"), PLAY_RATE, cfg.audio.playback_prebuffer_s, tap)
    rows = []
    with Microphone(find_device(cfg.audio.input_device, "input"), tap) as mic, speaker:
        tap.set_delay(mic.latency + speaker.latency)
        reader_stop = time.monotonic() + 3600

        def drain() -> None:  # keep the mic's queue moving; the blocks are kept by the tap
            while time.monotonic() < reader_stop:
                mic.read()

        threading.Thread(target=drain, daemon=True).start()
        for i, clip in enumerate(clips):
            talking = args.talk and i == len(clips) - 1
            if talking:
                print('\nNow say "what\'s the weather tomorrow" while it plays...', flush=True)
                time.sleep(1.0)
            start = len(tap.raw)
            speaker.play_pcm_stream([(np.clip(clip, -1, 1) * 32767).astype(np.int16).tobytes()], PLAY_RATE)
            time.sleep(0.4)  # the room's tail
            raw, clean = np.concatenate(tap.raw[start:]), np.concatenate(tap.clean[start:])
            vad.reset()
            speechy = [vad(b) for b in clean.reshape(-1, BLOCK_SAMPLES)]
            rows.append((i + 1, talking, db(raw), db(clean), words(raw, vosk), words(clean, vosk), speechy))
            time.sleep(0.6)
        reader_stop = 0

    print("\n clip | mic before -> after (dBFS) | heard before | heard after | speech detector, after")
    for n, talking, before, after, w_raw, w_clean, speechy in rows:
        share = np.mean(np.array(speechy) >= cfg.recorder.vad_threshold) if speechy else 0.0
        tag = " (you talking)" if talking else ""
        print(f" {n}{tag}: {before:6.1f} -> {after:6.1f} ({before - after:4.1f} dB removed)")
        print(f"      before: {w_raw!r}\n      after:  {w_clean!r}\n      taken for speech: {share:.0%} of it")
    quiet = [r for r in rows[1:] if not r[1]]  # after the first clip, without you talking
    if quiet:
        removed = np.median([r[2] - r[3] for r in quiet])
        heard = sum(1 for r in quiet if r[5])
        print(
            f"\nAfter the first clip: {removed:.0f} dB removed (median); words heard in {heard} of {len(quiet)} clips."
        )
        print("Good enough to keep the mic open: about 20 dB or more, and no words heard.")


if __name__ == "__main__":
    main()
