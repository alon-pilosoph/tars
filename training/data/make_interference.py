"""Synthesized interference: people talking over each other (babble) and a TV in a room, for training and for testing.

    DATA/mww/.venv/bin/python -m training.data.make_interference

Training and test versions come from disjoint sources, so the benchmark measures sounds no model heard:
  train: MUSAN speech/music/noise + RIRS_NOISES *simulated* rooms   -> DATA/aug/train/{babble,tv}, DATA/aug/train_rirs
  test:  LibriSpeech test-clean + FMA-xsmall + ESC-50 + *real* rooms
         -> DATA/bench/interference/test/{babble,tv,music,noise,rooms}
         plus one hour of continuous test TV (tv_hour) for false answers per hour.
Needs downloads.sh first. Resumable; writes DATA/aug/interference.done at the end.
"""

import random
import sys

import librosa
import numpy as np
import soundfile as sf
from scipy.signal import fftconvolve

from training.common import Layout, parser

SR = 16000


def load(path, seconds=None, offset=None):
    info = sf.info(str(path))
    dur = info.duration
    if seconds is None or dur <= seconds:
        start = 0.0
    else:
        start = offset if offset is not None else random.uniform(0, dur - seconds)
    y, sr = sf.read(
        str(path),
        start=int(start * info.samplerate),
        frames=-1 if seconds is None else int(seconds * info.samplerate),
        dtype="float32",
        always_2d=True,
    )
    y = y.mean(axis=1)
    return librosa.resample(y, orig_sr=sr, target_sr=SR) if sr != SR else y


def fit(y, n):
    return np.resize(y, n) if len(y) < n else y[:n]


def rms(y):
    return float(np.sqrt(np.mean(y**2)) + 1e-9)


def at_gain_db(y, db, ref):
    return y * (ref / rms(y)) * 10 ** (db / 20)


def reverb(y, rirs):
    rir = load(random.choice(rirs))
    rir = rir / (np.abs(rir).max() + 1e-9)
    return fftconvolve(y, rir)[: len(y)]


def babble(speech, seconds):
    n = int(SR * seconds)
    out = np.zeros(n, np.float32)
    for _ in range(random.randint(3, 6)):
        s = fit(load(random.choice(speech), seconds), n)
        out += at_gain_db(s, random.uniform(-6, 0), 0.05)
    return out


def tv(speech, music, effects, rirs, seconds):
    """A TV in the room: a talking head or two over a music bed, the odd sound effect, all through a room."""
    n = int(SR * seconds)
    mix = np.zeros(n, np.float32)
    for _ in range(random.randint(1, 2)):
        mix += at_gain_db(fit(load(random.choice(speech), seconds), n), random.uniform(-3, 0), 0.05)
    mix += at_gain_db(fit(load(random.choice(music), seconds), n), random.uniform(-15, -5), 0.05)
    if random.random() < 0.6:
        fx = fit(load(random.choice(effects), min(seconds, 5)), int(SR * min(seconds, 5)))
        at = random.randint(0, n - len(fx))
        mix[at : at + len(fx)] += at_gain_db(fx, random.uniform(-10, 0), 0.05)
    # TV speakers: no deep bass, no sparkle
    spec = np.fft.rfft(mix)
    f = np.fft.rfftfreq(n, 1 / SR)
    spec[(f < 120) | (f > 7000)] *= 0.1
    return reverb(np.fft.irfft(spec, n).astype(np.float32), rirs)


def write_set(folder, make, count: int, seconds: float | None):
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        path = folder / f"{i:05d}.wav"
        if path.exists():
            continue
        y = make(seconds)
        sf.write(path, (y / (np.abs(y).max() + 1e-9) * 0.7).astype(np.float32), SR, subtype="PCM_16")
    print(f"{folder}: {count}", flush=True)


def main():
    args = parser(__doc__).parse_args()
    layout = Layout(args.data)
    aug, bench = layout.aug, layout.bench
    train_out, test_out = aug / "train", bench / "interference" / "test"
    random.seed(0)
    musan = aug / "musan"
    train_speech = sorted((musan / "speech").glob("**/*.wav"))
    train_music = sorted((musan / "music").glob("**/*.wav"))
    train_noise = sorted((musan / "noise").glob("**/*.wav"))
    rirs_root = aug / "RIRS_NOISES"
    sim_rirs = sorted((rirs_root / "simulated_rirs").glob("**/*.wav"))
    real_rirs = sorted((rirs_root / "real_rirs_isotropic_noises").glob("*rir*.wav"))
    test_speech = sorted((bench / "LibriSpeech/test-clean").glob("**/*.flac"))
    test_music = sorted((bench / "interference/fma_small").glob("**/*.mp3"))
    test_noise = sorted((bench / "interference/ESC-50-master/audio").glob("*.wav"))
    sources = {
        "train_speech": train_speech,
        "train_music": train_music,
        "train_noise": train_noise,
        "sim_rirs": sim_rirs,
        "real_rirs": real_rirs,
        "test_speech": test_speech,
        "test_music": test_music,
        "test_noise": test_noise,
    }
    print({k: len(v) for k, v in sources.items()}, flush=True)
    missing = [k for k, v in sources.items() if not v]
    if missing:
        sys.exit(f"Missing sources: {', '.join(missing)}. Run training/data/downloads.sh first.")
    rir_subset = random.sample(sim_rirs, 5000)
    (aug / "train_rirs.txt").write_text("\n".join(map(str, rir_subset)))
    # microWakeWord's augmenter takes folders, so the 5,000 training rooms become a folder of links.
    links = aug / "train_rirs"
    links.mkdir(exist_ok=True)
    for i, path in enumerate(rir_subset):
        link = links / f"{i:05d}.wav"
        if not link.exists():
            link.symlink_to(path)

    write_set(train_out / "babble", lambda s: babble(train_speech, s), 2000, 10)
    write_set(train_out / "tv", lambda s: tv(train_speech, train_music, train_noise, rir_subset, s), 2000, 10)

    write_set(test_out / "babble", lambda s: babble(test_speech, s), 300, 10)
    write_set(test_out / "tv", lambda s: tv(test_speech, test_music, test_noise, real_rirs, s), 300, 10)
    write_set(test_out / "music", lambda s: fit(load(random.choice(test_music), s), int(SR * s)), 200, 10)
    write_set(test_out / "noise", lambda s: fit(load(random.choice(test_noise)), int(SR * s)), 400, 5)
    write_set(test_out / "rooms", lambda s: load(random.choice(real_rirs)), len(real_rirs), None)
    # One continuous hour of test TV (60 x 60 s) for false answers per hour with the TV on.
    write_set(test_out / "tv_hour", lambda s: tv(test_speech, test_music, test_noise, real_rirs, s), 60, 60)
    (aug / "interference.done").touch()
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
