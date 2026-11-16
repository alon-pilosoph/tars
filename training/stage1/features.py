"""Stage 1's training features: augmented spectrograms of every clip, the way microWakeWord trains on them.

    DATA/mww/.venv/bin/python -m training.stage1.features synthetic      # every voice source + real lookalikes
    DATA/mww/.venv/bin/python -m training.stage1.features synthetic --device   # the same through made-up microphones
    DATA/mww/.venv/bin/python -m training.stage1.features synthetic --cut      # some "hey TARS" with its ending cut
    DATA/mww/.venv/bin/python -m training.stage1.features tv-negatives         # TV, chatter, music as negatives
    DATA/mww/.venv/bin/python -m training.stage1.features speech-negatives     # real read speech as negatives
    DATA/mww/.venv/bin/python -m training.stage1.features negatives NAME --folder DIR   # any WAVs as negatives
    DATA/mww/.venv/bin/python -m training.stage1.features user           # the owner's training half (personal setup)
    DATA/mww/.venv/bin/python -m training.stage1.features household RUN  # a household's wakes (training.household)
    DATA/mww/.venv/bin/python -m training.stage1.features cloned         # cloned speakers, for the large pair

Augmentation: room echoes (MIT + 5,000 simulated rooms), MUSAN music/speech/noise, synthesized
babble and TV, AudioSet and FMA at -5 to 15 dB, plus EQ, distortion, pitch and gain changes. Each source is split
80/10/10 into training/validation/testing. Spectrograms are stored as uint16, which is exact: the microfrontend's
outputs are integers times 0.0390625. Output: DATA/features/<phrase>/{positive,near_miss}/<source>/ and
DATA/features/user/<phrase>/{positive,negative}/, DATA/features/household/<run>/{positive,negative}/. Resumable per
split; about 2 hours for everything synthetic on the development Mac at 4 threads, minutes for the rest.
"""

import shutil
from pathlib import Path

import numpy as np

from training.common import Layout, add_user_arg, cap_threads, log, parser, user_split

SCALE = 0.0390625

# 15 owner takes x 150 augmentations was enough, so a household gets about that total however many clips it has.
POSITIVE_SPECTROGRAMS, NEGATIVE_SPECTROGRAMS = 2250, 800

# source: max clips used
POSITIVES = {"piper_libritts": 50_000, "kokoro": 20_000, "openai": 1_312, "piper_voices": 22_000, "prosody": 25_000}
NEAR_MISSES = {"piper_libritts": 30_000, "kokoro": 16_000, "openai": 304, "piper_voices": 26_000}
REAL_LOOKALIKES = 5_000
CLONED = 50_000
# --device: the share of clips heard through a made-up microphone (training/data/device.py), and how many 3.2 s cuts of
# TV, chatter, music and noise go through one as things that aren't the phrase, so a device's sound can't mean a wake.
DEVICE_SHARE = 0.7
CUT_SHARE = 0.3  # --cut: the share of "hey TARS" clips whose ending is cut (training/data/device.py clip_ending)
DEVICE_NEGATIVES = 20_000
CUT_S = 3.2


def augmenter(layout: Layout, owner: bool = False):
    from microwakeword.audio.augmentation import Augmentation

    musan, bg, aug = layout.aug / "musan", layout.backgrounds, layout.aug
    if owner:
        backgrounds = [
            musan / "music",
            musan / "noise",
            bg / "audioset_16k",
            bg / "fma",
            musan / "speech",
            aug / "train/babble",
            aug / "train/tv",
        ]
    else:
        backgrounds = [
            musan / "music",
            musan / "speech",
            musan / "noise",
            aug / "train/babble",
            aug / "train/tv",
            bg / "audioset_16k",
            bg / "fma",
        ]
    return Augmentation(
        augmentation_duration_s=3.2,
        augmentation_probabilities={
            "SevenBandParametricEQ": 0.2,
            "TanhDistortion": 0.1,
            "PitchShift": 0.3,
            "BandStopFilter": 0.1,
            "AddColorNoise": 0.15,
            "AddBackgroundNoise": 0.8 if owner else 0.9,
            "Gain": 1.0,
            "GainTransition": 0.25,
            "RIR": 0.5 if owner else 0.6,
        },
        impulse_paths=[str(bg / "mit_rirs"), str(aug / "train_rirs")],
        background_paths=[str(p) for p in backgrounds],
        background_min_snr_db=-5,
        background_max_snr_db=15,
        min_jitter_s=0.195,
        max_jitter_s=0.205,
    )


def as_uint16(spectrograms):
    for s in spectrograms:
        yield np.rint(s / SCALE).astype(np.uint16)


def write(out: Path, generator) -> None:
    """Written to a temp folder and renamed, so an interrupted run redoes only that split."""
    from mmap_ninja.ragged import RaggedMmap

    final = out / "wakeword_mmap"
    if final.exists():
        return
    tmp = out / "wakeword_mmap.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    RaggedMmap.from_generator(out_dir=str(tmp), batch_size=100, verbose=False, sample_generator=as_uint16(generator))
    tmp.rename(final)


def build(layout: Layout, clip_dir: Path, out: Path, max_clips: int, slide: int, aug=None, plain=False) -> None:
    """`aug`: an augmenter in place of the usual one; `plain`: none at all (the clips are ready as they are)."""
    from microwakeword.audio.clips import Clips
    from microwakeword.audio.spectrograms import SpectrogramGeneration

    clips = Clips(
        input_directory=str(clip_dir),
        file_pattern="*.wav",
        max_clip_duration_s=None,
        remove_silence=False,
        random_split_seed=10,
        split_count=0.1,
    )
    aug = None if plain else aug or augmenter(layout)
    for split, split_name, split_slide in [
        ("training", "train", slide),
        ("validation", "validation", slide),
        ("testing", "test", 1),
    ]:
        if (out / split / "wakeword_mmap").exists():
            continue
        cap = int(max_clips * (0.8 if split == "training" else 0.1))
        log(f"features: {out.parent.name}/{out.name}/{split} (up to {cap} clips x {split_slide} slides)")
        gen = SpectrogramGeneration(clips=clips, augmenter=aug, slide_frames=split_slide, step_ms=10)
        write(out / split, gen.spectrogram_generator(split=split_name, repeat=1, max_clips=cap))


def synthetic(layout: Layout, phrase: str, device: bool = False, cut: bool = False) -> None:
    """`device`: into DATA/features/<phrase>-device, DEVICE_SHARE of every clip through a made-up microphone, plus
    DEVICE_NEGATIVES cuts of the backgrounds through one (negative/device)."""
    if not (layout.aug / "interference.done").exists():
        raise SystemExit("Run training.data.make_interference first.")
    if cut:
        return cut_endings(layout, phrase)
    root = layout.features / (f"{phrase}-device" if device else phrase)
    aug = None
    if device:
        from training.data.device import DeviceAugmentation

        aug = DeviceAugmentation(augmenter(layout), DEVICE_SHARE)
    for kind, table, slide in [("positive", POSITIVES, 5), ("near_miss", NEAR_MISSES, 1)]:
        for source, cap in table.items():
            build(layout, layout.clip_dir(source, phrase, kind), root / kind / source, cap, slide, aug)
    if phrase == "hey_tars":
        build(layout, layout.real_lookalikes, root / "near_miss" / "real", REAL_LOOKALIKES, slide=1, aug=aug)
    if device:
        build(layout, device_negatives(layout), root / "negative" / "device", DEVICE_NEGATIVES, slide=1, plain=True)


def cut_endings(layout: Layout, phrase: str) -> None:
    """Into DATA/features/<phrase>-cut: the positives again with CUT_SHARE of them cut short; the lookalikes are the
    plain set's, linked, since nothing about them changes."""
    from training.data.device import CutEndings

    base, root = layout.features / phrase, layout.features / f"{phrase}-cut"
    if not (base / "near_miss").is_dir():
        raise SystemExit("Run features synthetic first: --cut reuses its lookalikes.")
    (root / "near_miss").mkdir(parents=True, exist_ok=True)
    for source in sorted((base / "near_miss").iterdir()):
        link = root / "near_miss" / source.name
        if not link.exists():
            link.symlink_to(source, target_is_directory=True)
    aug = CutEndings(augmenter(layout), CUT_SHARE)
    for source, cap in POSITIVES.items():
        build(layout, layout.clip_dir(source, phrase, "positive"), root / "positive" / source, cap, 5, aug)


def folder_negatives(layout: Layout, phrase: str, name: str, folder: Path) -> None:
    """Into DATA/features/<phrase>/negative/<name>: every WAV in `folder` as it is, as things that aren't the phrase
    (train --extra NAME:WEIGHT). For clips made elsewhere: mined false wakes, real speech cut to length."""
    n = len(list(folder.glob("*.wav")))
    log(f"negatives {name}: {n} clips from {folder}")
    build(layout, folder, layout.features / phrase / "negative" / name, n, slide=1, plain=True)


def speech_cuts(layout: Layout, count: int = DEVICE_NEGATIVES) -> Path:
    """`count` cuts of CUT_S from LibriSpeech train-clean-100 (real people reading, 100 hours), as 16 kHz WAVs in
    DATA/aug/speech_negatives (made once)."""
    import soundfile as sf

    out = layout.aug / "speech_negatives"
    if (out / ".done").exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    files = sorted((layout.root / "LibriSpeech" / "train-clean-100").rglob("*.flac"))
    rng = np.random.default_rng(16)
    n, want = 0, int(CUT_S * 16000)
    log(f"speech negatives: {count} cuts from {len(files)} files")
    while n < count:
        path = files[rng.integers(len(files))]
        info = sf.info(str(path))
        if info.frames < want:
            continue
        start = int(rng.integers(0, info.frames - want + 1))
        x, _ = sf.read(str(path), start=start, frames=want, dtype="float32")
        y = x / max(np.abs(x).max(), 1e-6) * rng.uniform(0.1, 0.9)
        sf.write(str(out / f"{n:05d}.wav"), (y * 32767).astype(np.int16), 16000, subtype="PCM_16")
        n += 1
    (out / ".done").touch()
    return out


def tv_negatives(layout: Layout, phrase: str) -> None:
    """Into DATA/features/<phrase>/negative/tv: DEVICE_NEGATIVES cuts of the TV, chatter, music and noise as they
    are, for a model that wakes to them less (train --tv-negatives)."""
    out = device_negatives(layout, through_device=False)
    build(layout, out, layout.features / phrase / "negative" / "tv", DEVICE_NEGATIVES, slide=1, plain=True)


def device_negatives(layout: Layout, through_device: bool = True) -> Path:
    """DEVICE_NEGATIVES cuts of CUT_S from the TV, chatter, music and noise, each through a made-up microphone (or,
    `through_device` False, as they are), as 16 kHz WAVs (made once, in DATA/aug/device_negatives or tv_negatives)."""
    import soundfile as sf

    from training.data.device import device

    out = layout.aug / ("device_negatives" if through_device else "tv_negatives")
    if (out / ".done").exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    musan, bg = layout.aug / "musan", layout.backgrounds
    folders = [layout.aug / "train/tv", layout.aug / "train/babble", musan / "speech", musan / "music",
               musan / "noise", bg / "audioset_16k", bg / "fma"]  # fmt: skip
    files = [f for d in folders for f in sorted(d.rglob("*.wav"))]
    rng = np.random.default_rng(15)
    n, want = 0, int(CUT_S * 16000)
    log(f"device negatives: {DEVICE_NEGATIVES} cuts from {len(files)} files")
    while n < DEVICE_NEGATIVES:
        path = files[rng.integers(len(files))]
        info = sf.info(str(path))
        if info.samplerate != 16000 or info.frames < want:
            continue
        start = int(rng.integers(0, info.frames - want + 1))
        x, _ = sf.read(str(path), start=start, frames=want, dtype="float32", always_2d=True)
        x = x.mean(axis=1)
        if np.sqrt((x**2).mean()) < 1e-3:  # silence teaches nothing here
            continue
        y = x / max(np.abs(x).max(), 1e-6) * rng.uniform(0.1, 0.9)
        y = device(y, rng) if through_device else y
        sf.write(str(out / f"{n:05d}.wav"), (y * 32767).astype(np.int16), 16000, subtype="PCM_16")
        n += 1
    (out / ".done").touch()
    return out


def cloned(layout: Layout) -> None:
    root = layout.features / "hey_tars"
    for kind, slide in [("positive", 5), ("near_miss", 1)]:
        build(layout, layout.clip_dir("cloned", "hey_tars", kind), root / kind / "cloned", CLONED, slide)


def user(layout: Layout, phrase: str, recordings: Path) -> None:
    """Only 15 takes, so each is augmented many times over."""
    for set_name, kind, slide, repeat in [(phrase, "positive", 5, 150), ("speech", "negative", None, 60)]:
        train, _ = user_split(recordings, set_name)
        folder = layout.features / "user_split" / set_name / "train"  # Clips reads a whole folder
        folder.mkdir(parents=True, exist_ok=True)
        for f in train:
            link = folder / f.name
            if not link.exists():
                link.symlink_to(f)
        log(f"user features: {phrase} {kind}: {len(train)} clips x {repeat} augmentations")
        augmented(layout, folder, layout.features / "user" / phrase / kind / "training", slide, repeat)


def household(layout: Layout, run: str) -> None:
    for kind, slide, total in [("positive", 5, POSITIVE_SPECTROGRAMS), ("negative", None, NEGATIVE_SPECTROGRAMS)]:
        folder = layout.household / run / "clips" / "train" / kind
        n = len(list(folder.glob("*.wav")))
        if not n:
            continue
        repeat = max(5, min(150, -(-total // n)))
        log(f"household features: {kind}: {n} clips x {repeat} augmentations")
        augmented(layout, folder, layout.features / "household" / run / kind / "training", slide, repeat)


def augmented(layout: Layout, folder: Path, out: Path, slide: int | None, repeat: int) -> None:
    from microwakeword.audio.clips import Clips
    from microwakeword.audio.spectrograms import SpectrogramGeneration

    clips = Clips(
        input_directory=str(folder),
        file_pattern="*.wav",
        max_clip_duration_s=None,
        remove_silence=False,
        random_split_seed=None,
    )
    gen = SpectrogramGeneration(clips=clips, augmenter=augmenter(layout, owner=True), slide_frames=slide, step_ms=10)
    write(out, gen.spectrogram_generator(split=None, repeat=repeat))


def main():
    p = parser(__doc__)
    p.add_argument(
        "what", choices=["synthetic", "user", "household", "cloned", "tv-negatives", "speech-negatives", "negatives"]
    )
    p.add_argument("run", nargs="?", help="household: the training run's name; negatives: the set's name")
    p.add_argument("--folder", type=Path, help="negatives: the folder of WAVs")
    p.add_argument("--phrase", default="hey_tars", choices=["hey_tars", "tars_stop"])
    p.add_argument("--device", action="store_true", help="synthetic: through made-up microphones (data/device.py)")
    p.add_argument("--cut", action="store_true", help="synthetic: some positives with their ending cut")
    add_user_arg(p)
    args = p.parse_args()
    cap_threads()
    layout = Layout(args.data)
    if args.what == "synthetic":
        synthetic(layout, args.phrase, args.device, args.cut)
    elif args.what == "speech-negatives":
        folder_negatives(layout, args.phrase, "speech", speech_cuts(layout))
    elif args.what == "negatives":
        if not (args.run and args.folder):
            p.error("negatives: NAME and --folder")
        folder_negatives(layout, args.phrase, args.run, args.folder)
    elif args.what == "tv-negatives":
        tv_negatives(layout, args.phrase)
    elif args.what == "cloned":
        cloned(layout)
    elif args.what == "user":
        if not (args.user and args.user.is_dir()):
            p.error("user: pass --user, the owner's recordings")
        user(layout, args.phrase, args.user)
    else:
        household(layout, args.run)


if __name__ == "__main__":
    main()
