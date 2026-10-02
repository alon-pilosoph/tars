"""Shared by every training script: the data folder layout, the owner's train/test split, small helpers.

Everything goes under one data folder (`--data`, default $TARS_TRAINING_DATA or ~/tars-training), laid out as
described in training/README.md. Only the standard library and numpy are imported here: the scripts run in four
different environments.
"""

import argparse
import os
import time
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
SR = 16_000
PHRASES = ("hey_tars", "tars_stop")


class Layout:
    def __init__(self, root: Path):
        self.root = root
        self.clips = root / "clips"  # <source>/<phrase>/{positive,near_miss}/*.wav
        self.piper_models = root / "piper_models"
        self.backgrounds = root / "backgrounds"  # mit_rirs, audioset_16k, fma
        self.aug = root / "aug"  # training interference: MUSAN, RIRS_NOISES, train/{babble,tv}
        self.bench = root / "bench"  # test only: clips_heldout, interference/test, LibriSpeech
        self.heldout = self.bench / "clips_heldout"  # the held-out OpenAI voices: hey_tars/, hey_tars_near_miss/
        self.interference = self.bench / "interference" / "test"  # tv, babble, music, noise, rooms, tv_hour
        self.librispeech_test = self.bench / "LibriSpeech" / "test-clean"  # audiobooks, for false answers per hour
        self.librispeech = root / "LibriSpeech" / "train-clean-100"
        self.vctk = root / "vctk"
        self.clean = root / "clean"  # lists of synthetic clips Vosk heard as labeled
        self.vc = root / "vc"  # kNN-VC into LibriSpeech speakers
        self.vc_vctk = root / "vc_vctk"  # kNN-VC into VCTK speakers
        self.real_lookalikes = root / "real_lookalikes"
        self.mww = root / "mww"  # microWakeWord: venv, clone, negative_datasets
        self.trained_models = root / "trained_models"  # microWakeWord's checkpoints, one folder per model
        self.oww = root / "oww"  # openWakeWord, only to generate the Piper LibriTTS clips
        self.features = root / "features"  # stage 1 spectrograms (uint16)
        self.check = root / "check"  # stage 2: layers (.pkl), training and test rows
        self.models = root / "models"  # what to copy into the repo's models/
        self.results = root / "results"
        self.household = root / "household"
        self.hub = root / "hub"  # Hugging Face dataset snapshots

    def clip_dir(self, source: str, phrase: str, kind: str) -> Path:
        return self.clips / source / phrase / kind


def parser(doc: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    default = Path(os.environ.get("TARS_TRAINING_DATA", Path.home() / "tars-training"))
    p.add_argument("--data", type=Path, default=default, help=f"the training data folder (default: {default})")
    return p


def add_user_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--user",
        type=Path,
        help="the owner's recordings, e.g. voice_data/<name>/laptop: hey_tars/, hey_tars_lookalikes/, "
        "tars_stop/, speech/ (never committed)",
    )


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def write_wav(path: Path, audio: np.ndarray) -> None:
    """Written to a temp file and renamed, so a killed run never leaves a truncated clip a resumed run takes as done."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    with wave.open(str(partial), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(np.asarray(audio, np.int16).tobytes())
    partial.replace(path)


def cap_threads(threads: int = 4) -> None:
    for k, v in {
        "OMP_NUM_THREADS": str(threads),
        "TF_NUM_INTRAOP_THREADS": str(threads),
        "TF_NUM_INTEROP_THREADS": "2",
        "TF_CPP_MIN_LOG_LEVEL": "2",
    }.items():
        os.environ.setdefault(k, v)


def user_split(user: Path, set_name: str) -> tuple[list[Path], list[Path]]:
    """The owner's recordings as (train, test); nothing ever trains on the test half.

    Wake phrases: 5 variations (normal, far, quick, quiet, TV on) x 6 takes; takes 0-2 of each train, 3-5 test.
    Lookalikes: 3 takes per phrase, and the test take rotates so the test covers every variation.
    Sentences alternate.
    """
    files = sorted((user / set_name).glob("*.wav"))
    if set_name == "speech":
        return files[::2], files[1::2]
    if set_name.endswith("_lookalikes"):
        test = [f for f in files if int(f.stem) % 3 == (int(f.stem) // 3) % 3]
        return [f for f in files if f not in test], test
    train = [f for f in files if int(f.stem) % 6 < 3]
    return train, [f for f in files if f not in train]


def cap_onnxruntime_threads(threads: int = 4) -> None:
    """onnxruntime starts one thread per core and ignores OMP_NUM_THREADS.

    Call before importing anything that creates sessions (Piper does it on load).
    """
    import onnxruntime

    session = onnxruntime.InferenceSession

    def capped(*args, **kwargs):
        options = kwargs.get("sess_options") or onnxruntime.SessionOptions()
        options.intra_op_num_threads, options.inter_op_num_threads = threads, 1
        kwargs["sess_options"] = options
        return session(*args, **kwargs)

    onnxruntime.InferenceSession = capped


def test_sets(layout: Layout, user: Path | None, all_user: bool = False) -> dict:
    """{name: (files, should the check accept them?)}: the owner's test half (or every take, for a setup that never
    trained on them) and the held-out OpenAI voices. The order fixes which noise each clip gets."""
    held = layout.heldout
    other = {
        "other voices hey TARS": (sorted((held / "hey_tars").glob("*.wav")), True),
        "other lookalikes": (sorted((held / "hey_tars_near_miss").glob("*.wav")), False),
    }
    if user is None:
        return other
    if not user.is_dir():
        raise SystemExit(f"--user {user} doesn't exist.")
    label = "all" if all_user else "held-out"

    def pick(s):
        return sorted((user / s).glob("*.wav")) if all_user else user_split(user, s)[1]

    return {
        f"your hey TARS ({label})": (pick("hey_tars"), True),
        "other voices hey TARS": other["other voices hey TARS"],
        f"your lookalikes ({label})": (pick("hey_tars_lookalikes"), False),
        "other lookalikes": other["other lookalikes"],
        f"your TARS stop ({label})": (pick("tars_stop"), False),
        f"your sentences ({label})": (pick("speech"), False),
    }
