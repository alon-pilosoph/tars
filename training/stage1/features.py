"""Stage 1's training features: augmented spectrograms of every clip, the way microWakeWord trains on them.

    DATA/mww/.venv/bin/python -m training.stage1.features synthetic      # every voice source + real lookalikes
    DATA/mww/.venv/bin/python -m training.stage1.features user           # the owner's training half (personal setup)
    DATA/mww/.venv/bin/python -m training.stage1.features household RUN  # a household's wakes (training.household)

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


def build(layout: Layout, clip_dir: Path, out: Path, max_clips: int, slide: int) -> None:
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
    aug = augmenter(layout)
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


def synthetic(layout: Layout, phrase: str) -> None:
    if not (layout.aug / "interference.done").exists():
        raise SystemExit("Run training.data.make_interference first.")
    root = layout.features / phrase
    for kind, table, slide in [("positive", POSITIVES, 5), ("near_miss", NEAR_MISSES, 1)]:
        for source, cap in table.items():
            build(layout, layout.clip_dir(source, phrase, kind), root / kind / source, cap, slide)
    if phrase == "hey_tars":
        build(layout, layout.real_lookalikes, root / "near_miss" / "real", REAL_LOOKALIKES, slide=1)


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
    p.add_argument("what", choices=["synthetic", "user", "household"])
    p.add_argument("run", nargs="?", help="household: the training run's name")
    p.add_argument("--phrase", default="hey_tars", choices=["hey_tars", "tars_stop"])
    add_user_arg(p)
    args = p.parse_args()
    cap_threads()
    layout = Layout(args.data)
    if args.what == "synthetic":
        synthetic(layout, args.phrase)
    elif args.what == "user":
        if not (args.user and args.user.is_dir()):
            p.error("user: pass --user, the owner's recordings")
        user(layout, args.phrase, args.user)
    else:
        household(layout, args.run)


if __name__ == "__main__":
    main()
