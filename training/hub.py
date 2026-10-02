"""The prepared training data on Hugging Face: what a new machine downloads instead of spending a day making it.

    uv run --group training python -m training.hub download              # into the data folder
    uv run --group training python -m training.hub download --from DIR   # from a local copy of the datasets
    uv run --group training python -m training.hub export OUT            # (maintainer) build both datasets
    uv run --group training python -m training.hub upload OUT            # (maintainer) needs a write token

Two datasets, split by what their sources allow (ATTRIBUTION.md):
  open: CC BY 4.0. Kokoro and OpenAI clips, the permissively licensed Piper voices, real LibriSpeech lookalike words,
        the held-out test voices (AI-generated), and the double-check's training rows (numbers, not audio).
  nc:   CC BY-NC-SA 4.0. The Piper voices built on research-only or non-commercial voices, the Piper LibriTTS clips,
        and the pitch and tempo variants (made from the hosted sources only, see export).
Only the clean clips are hosted: several of the background noise, music and room sources can't be redistributed, so
they're downloaded from their own sources and mixed in on the training machine. Voice conversions into real people's
voices are never hosted. Clips are FLAC in tar shards (a hub folder holds at most 10,000 files) and extract to the
16 kHz WAVs the scripts read. Resumable: a finished shard leaves a .done marker. Downloads are pinned to the dataset
commits in DATASETS; update them after an upload.
"""

import argparse
import csv
import hashlib
import io
import shutil
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path

import soundfile as sf

from training.common import REPO, Layout, log, parser

DATASETS = {
    "open": ("alon-p/hey-tars-training", "7b57b0a0458a79ec6c8d56af2fee58438462660b"),
    "nc": ("alon-p/hey-tars-training-nc", "5f1ffc657d8d2d7fa46f20346516894a2dd9492d"),
}
LICENSES = {"open": "cc-by-4.0", "nc": "cc-by-nc-sa-4.0"}
SHARD = 5000
# Piper voices by what their model cards allow (ATTRIBUTION.md). A voice in neither set is never hosted.
PIPER_OPEN = {"en_US-libritts-high", "en_GB-cori-medium", "en_US-kristin-medium", "en_US-john-medium"}
PIPER_NC = {
    "en_GB-alba-medium",
    "en_GB-aru-medium",
    "en_GB-jenny_dioco-medium",
    "en_GB-northern_english_male-medium",
    "en_GB-semaine-medium",
    "en_GB-southern_english_female-low",
    "en_GB-vctk-medium",
    "en_US-arctic-medium",
    "en_US-bryce-medium",
    "en_US-hfc_female-medium",
    "en_US-hfc_male-medium",
    "en_US-joe-medium",
    "en_US-kathleen-low",
    "en_US-l2arctic-medium",
    "en_US-lessac-medium",
}


def piper_voice(path: Path) -> str:
    return path.stem.split("_s")[0]  # en_US-lessac-medium_s000_012 -> en_US-lessac-medium


def sets(layout: Layout) -> list[tuple[str, str, Path, Callable[[Path], bool]]]:
    """Everything hosted, as (dataset, path in the data folder, source folder, clip filter)."""
    everything = lambda f: True
    rows = []
    for kind in ("positive", "near_miss"):
        for source in ("kokoro", "openai"):
            rows.append(
                ("open", f"clips/{source}/hey_tars/{kind}", layout.clip_dir(source, "hey_tars", kind), everything)
            )
        piper = layout.clip_dir("piper_voices", "hey_tars", kind)
        rows.append(("open", f"clips/piper_voices/hey_tars/{kind}", piper, lambda f: piper_voice(f) in PIPER_OPEN))
        rows.append(("nc", f"clips/piper_voices/hey_tars/{kind}", piper, lambda f: piper_voice(f) in PIPER_NC))
        rows.append(
            (
                "nc",
                f"clips/piper_libritts/hey_tars/{kind}",
                layout.clip_dir("piper_libritts", "hey_tars", kind),
                everything,
            )
        )
    rows.append(
        ("nc", "clips/prosody/hey_tars/positive", layout.clip_dir("prosody", "hey_tars", "positive"), everything)
    )
    rows.append(("open", "real_lookalikes", layout.real_lookalikes, everything))
    for kind in ("hey_tars", "hey_tars_near_miss"):
        rows.append(("open", f"bench/clips_heldout/{kind}", layout.bench / "clips_heldout" / kind, everything))
    return rows


def export(layout: Layout, out: Path) -> None:
    """A shard left by an interrupted export is reused if it holds exactly the clips it should."""
    # The pitch and tempo variants don't record which clip they came from, so they're hosted only from a data folder
    # whose Piper voices can all be hosted: they must be made after any other voice is removed.
    unhosted = (
        {
            piper_voice(f)
            for kind in ("positive", "near_miss")
            for f in layout.clip_dir("piper_voices", "hey_tars", kind).glob("*.wav")
        }
        - PIPER_OPEN
        - PIPER_NC
    )
    if unhosted:
        raise SystemExit(
            f"Piper voices that can't be hosted are in the data folder ({', '.join(sorted(unhosted))}), "
            "and the pitch and tempo variants may be made from them: remove them and remake those."
        )
    manifests = {name: [] for name in DATASETS}
    for dataset, target, source, keep in sets(layout):
        clips = sorted(f for f in source.glob("*.wav") if keep(f))
        if not clips:
            raise SystemExit(f"No clips in {source}: is the data folder complete?")
        for n, start in enumerate(range(0, len(clips), SHARD)):
            shard, chunk = out / dataset / target / f"{n:03d}.tar", clips[start : start + SHARD]
            if not (shard.exists() and _names(shard) == [f"{c.stem}.flac" for c in chunk]):
                _write_shard(shard, chunk)
            manifests[dataset].append(
                {"shard": str(shard.relative_to(out / dataset)), "clips": len(chunk), "sha256": _sha256(shard)}
            )
        log(f"{dataset}: {target} ({len(clips)} clips)")
    base = layout.check / "generic_base.npz"
    if not base.exists():
        raise SystemExit(f"{base} is missing: run training.stage2.train_check generic first.")
    (out / "open" / "check").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(base, out / "open" / "check" / "generic_base.npz")
    manifests["open"].append({"shard": "check/generic_base.npz", "clips": 0, "sha256": _sha256(base)})
    for name, rows in manifests.items():
        folder = out / name
        with open(folder / "manifest.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["shard", "clips", "sha256"])
            writer.writeheader()
            writer.writerows(rows)
        shutil.copyfile(REPO / "ATTRIBUTION.md", folder / "ATTRIBUTION.md")
        (folder / "README.md").write_text(_card(name, sum(r["clips"] for r in rows)))
    log(f"exported to {out}: {', '.join(f'{n} ({_size(out / n)})' for n in DATASETS)}")


def download(layout: Layout, source: Path | None) -> None:
    """`source`: a local copy of the datasets (export's output) to use instead of the hub."""
    for name, (repo_id, revision) in DATASETS.items():
        folder = source / name if source else _snapshot(repo_id, layout.hub / name, revision)
        with open(folder / "manifest.csv") as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            path = folder / row["shard"]
            target = layout.root / Path(row["shard"]).parent
            # Keyed by dataset (both fill some folders) and by hash (so a changed shard is extracted again).
            done = target / f".{name}-{Path(row['shard']).name}-{row['sha256'][:16]}.done"
            if done.exists():
                continue
            if _sha256(path) != row["sha256"]:
                raise SystemExit(f"{path} doesn't match its manifest: download it again.")
            if path.suffix == ".tar":
                _extract_shard(path, target)
            else:
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target / path.name)
            done.touch()
        log(f"{name}: {len(rows)} files in place")


def upload(out: Path) -> None:
    from huggingface_hub import HfApi

    api = HfApi()
    for name, (repo_id, _) in DATASETS.items():
        api.create_repo(repo_id, repo_type="dataset", exist_ok=True)
        api.upload_folder(
            repo_id=repo_id,
            repo_type="dataset",
            folder_path=out / name,
            commit_message="The prepared training clips (training/hub.py export)",
        )
        log(f"uploaded {out / name} to https://huggingface.co/datasets/{repo_id}")


def _write_shard(shard: Path, clips: list[Path]) -> None:
    shard.parent.mkdir(parents=True, exist_ok=True)
    partial = shard.with_suffix(".part")
    with tarfile.open(partial, "w") as tar:
        for clip in clips:
            audio, sr = sf.read(clip, dtype="int16")
            buf = io.BytesIO()
            sf.write(buf, audio, sr, format="FLAC")
            data = buf.getvalue()  # not buf.tell(): closing rewinds the stream to finish the header
            info = tarfile.TarInfo(f"{clip.stem}.flac")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    partial.replace(shard)


def _extract_shard(shard: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(shard) as tar:
        for member in _flac_members(tar):
            audio, sr = sf.read(io.BytesIO(tar.extractfile(member).read()), dtype="int16")
            sf.write(target / f"{Path(member.name).stem}.wav", audio, sr, subtype="PCM_16")


def _names(shard: Path) -> list[str]:
    with tarfile.open(shard) as tar:
        return tar.getnames()


def _flac_members(tar: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    for member in tar:
        # Only plain top-level .flac files, so a shard can't write outside its folder.
        if member.isfile() and member.name.endswith(".flac") and "/" not in member.name and ".." not in member.name:
            yield member


def _snapshot(repo_id: str, local: Path, revision: str) -> Path:
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(repo_id, repo_type="dataset", local_dir=local, revision=revision))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _size(folder: Path) -> str:
    return f"{sum(f.stat().st_size for f in folder.rglob('*') if f.is_file()) / 1e9:.1f} GB"


def _card(name: str, clips: int) -> str:
    what = {
        "open": "Kokoro and OpenAI text-to-speech clips, the permissively licensed Piper voices, real LibriSpeech "
        "lookalike words, held-out test voices, and the double-check's training rows",
        "nc": "Piper voices built on research-only or non-commercial voices (including the Piper LibriTTS clips), "
        "and pitch and tempo variants of the hosted voices",
    }[name]
    return f"""---
license: {LICENSES[name]}
language: [en]
tags: [audio, wake-word, keyword-spotting, synthetic-speech]
pretty_name: hey TARS wake word training clips ({name})
---

# "hey TARS" training clips ({name})

{clips} synthetic "hey TARS" clips and lookalike phrases ("hey cars", "hey stars"...) for training the wake models
of TARS, a hackable voice assistant: {what}. 16 kHz mono FLAC in tar shards, listed with
their SHA-256 in `manifest.csv`. The clips are clean: background noise, music and rooms are mixed in at training time
from their own sources. All speech here is synthetic (AI-generated) except the LibriSpeech lookalike words.

Download it with the repo's `training.hub download`. Sources and their terms: `ATTRIBUTION.md`.
"""


def main():
    p = parser(__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    d = commands.add_parser("download", help="both datasets into the data folder")
    d.add_argument("--from", dest="source", type=Path, help="a local copy of the datasets instead of the hub")
    commands.add_parser("export", help="build both datasets from the data folder").add_argument("out", type=Path)
    commands.add_parser("upload", help="upload an export (needs a Hugging Face write token)").add_argument(
        "out", type=Path
    )
    for command in commands.choices.values():  # also accept --data after the subcommand
        command.add_argument("--data", type=Path, default=argparse.SUPPRESS, help="the training data folder")
    args = p.parse_args()
    layout = Layout(args.data)
    if args.command == "download":
        download(layout, args.source)
    elif args.command == "export":
        export(layout, args.out)
    else:
        upload(args.out)


if __name__ == "__main__":
    main()
