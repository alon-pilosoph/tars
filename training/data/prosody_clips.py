"""Pitch and tempo variations of the wake phrase, drawn from every English voice source.

    DATA/tts/.venv/bin/python -m training.data.prosody_clips hey_tars 25000     (PHRASE N)

Each output clip is a random source clip, pitch-shifted by up to +-4 semitones and time-stretched 0.8-1.25x
(both random, independently), so the model hears deep, high, rushed and drawn-out versions of every voice.
Most come out mushy: only 28% still sounded like "hey TARS" to Vosk (see training/README.md).
Output: DATA/clips/prosody/PHRASE/positive/<n>.wav. Resumable.
"""

import random
import wave

import numpy as np

from training.common import PHRASES, Layout, parser, write_wav


def read(path):
    with wave.open(str(path)) as f:
        return np.frombuffer(f.readframes(f.getnframes()), np.int16).astype(np.float32) / 32768


def main():
    p = parser(__doc__)
    p.add_argument("phrase", choices=PHRASES)
    p.add_argument("n", type=int)
    args = p.parse_args()
    import librosa

    layout = Layout(args.data)
    names = ["piper_voices", "kokoro", "openai", "piper_libritts"]
    sources = [layout.clip_dir(s, args.phrase, "positive") for s in names]
    out = layout.clip_dir("prosody", args.phrase, "positive")
    out.mkdir(parents=True, exist_ok=True)
    pool = [f for s in sources for f in sorted(s.glob("*.wav"))]
    rng = random.Random(f"prosody-{args.phrase}")
    print(f"{args.phrase}: {len(pool)} source clips", flush=True)
    for i in range(args.n):
        src, semitones, rate = rng.choice(pool), rng.uniform(-4, 4), rng.uniform(0.8, 1.25)
        path = out / f"{i:06d}.wav"
        if path.exists():
            continue
        y = librosa.effects.time_stretch(librosa.effects.pitch_shift(read(src), sr=16000, n_steps=semitones), rate=rate)
        write_wav(path, (np.clip(y, -1, 1) * 32767).astype(np.int16))
        if i % 2000 == 0:
            print(f"{args.phrase}: {i}/{args.n}", flush=True)
    print(f"{args.phrase}: ALL DONE", flush=True)


if __name__ == "__main__":
    main()
