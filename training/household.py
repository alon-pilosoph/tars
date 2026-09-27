"""Train a household's own pair of wake models, test it against the pair in use, and install it only if it's better.

    uv run python -m training.household                          # TARS runs on this machine
    uv run python -m training.household --pi pi@tars.local        # TARS runs on a Pi: its wakes come over SSH, and
                                                                 # the new pair goes back the same way

Needs a prepared data folder (training/setup/prepare.sh), and on the Pi, the repo at --remote with its usual setup.
1. Copy the labeled wakes and near-misses (events.learning_label) and the models in use from TARS. Nothing is
   changed there, and nothing leaves this machine.
2. Keep every fifth aside (by id) to test on; trim the rest of each real one after the last speech.
3. Train the wake model (the generic recipe plus the household's clips, 20 to 45 minutes), then the double-check on
   the generic layer's training rows plus the household's clips in every training condition.
4. Test both pairs end to end the way the assistant runs: the household's held-out wakes, held-out voices and
   lookalikes in 8 conditions, and false answers per hour on an hour of TV and of audiobooks.
5. Install it (voice-assistant --install-models) if it answers more of the household's real wakes, or lets fewer
   through that weren't for TARS, and does no worse on anything else. Otherwise it asks; --yes answers no.
The run is kept in DATA/household/<run>/.
"""

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from training.common import REPO, Layout, bench_tools, log, parser, write_wav
from training.eval.pipeline import CONDITIONS, stream
from voice_assistant.config import load_config
from voice_assistant.events import NOT_REAL, REAL, EventLog, learning_label
from voice_assistant.versions import ABOUT, CHECK, MODEL, ModelVersions, Pair

TEST_EVERY = 5  # events whose id is a multiple of this are held out: always the same ones, so the test only grows
END_PAD_S = 0.15  # kept after the last speech when trimming a real wake
QUIET = "clean"


@dataclass
class Source:
    """Where TARS runs: a folder on this machine (host None) or a Pi over SSH."""

    host: str | None
    remote: str  # TARS's folder there (on this machine: its path)

    def run(self, command: str) -> str:
        if self.host is None:
            return subprocess.run(
                command, shell=True, check=True, capture_output=True, text=True, cwd=self.remote
            ).stdout
        return subprocess.run(
            ["ssh", self.host, f"cd {shlex.quote(self.remote)} && {command}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def exists(self, rel: str) -> bool:
        return self.run(f"test -e {shlex.quote(rel)} && echo yes || true").strip() == "yes"

    def pull(self, rel: str, to: Path, files: list[str] | None = None) -> None:
        """Copy a folder (or only `files` in it) from the repo there to `to`."""
        to.mkdir(parents=True, exist_ok=True)
        src = f"{self.host}:{self.remote}/{rel}/" if self.host else f"{Path(self.remote) / rel}/"
        cmd = ["rsync", "-a"]
        if files is not None:
            cmd += ["--files-from=-"]
        subprocess.run(cmd + [src, f"{to}/"], input="\n".join(files or []), text=True, check=True)


def snapshot(source: Source, run_dir: Path) -> tuple[EventLog, Path]:
    """The event database (a consistent copy, even while TARS writes to it), the audio of labeled events, the models
    in use and config.toml, from wherever TARS runs."""
    events = run_dir / "events"
    events.mkdir(parents=True, exist_ok=True)
    config = run_dir / "config.toml"
    cfg_text = source.run("cat config.toml")
    config.write_text(cfg_text)
    folder = load_config(config).learning.folder
    backup = f"/tmp/tars-events-{run_dir.name}.db"
    source.run(f"python3 -c {shlex.quote(_BACKUP.format(src=f'{folder}/events.db', dst=backup))}")
    if source.host:
        subprocess.run(["scp", "-q", f"{source.host}:{backup}", str(events / "events.db")], check=True)
        source.run(f"rm -f {backup}")
    else:
        Path(backup).replace(events / "events.db")
    log_ = EventLog(events)
    audio = [e["audio"] for e in log_.events(limit=1_000_000_000) if e["audio"] and learning_label(e)]
    source.pull(folder, events, files=audio)
    if source.exists(f"{folder}/wake_models"):  # only once a trained pair has been installed
        source.pull(f"{folder}/wake_models", events / "wake_models")
    return log_, config


_BACKUP = "import sqlite3; s = sqlite3.connect({src!r}); d = sqlite3.connect({dst!r}); s.backup(d); d.close()"


def household_clips(log_: EventLog, run_dir: Path) -> dict[str, int]:
    """The labeled clips, split and written out: clips/{train,test}/{positive,negative}/<id>.wav. Returns counts."""
    from voice_assistant.audio import read_wav
    from voice_assistant.vad import SileroVAD

    vad = SileroVAD(REPO / "models" / "silero_vad.onnx")
    counts: dict[str, int] = {}
    for e in log_.events(limit=1_000_000_000):
        label = learning_label(e)
        path = log_.folder / (e["audio"] or "")
        if label not in (REAL, NOT_REAL) or not e["audio"] or not path.exists():
            continue
        try:
            pcm = read_wav(path)
        except (OSError, EOFError) as err:
            print(f"(leaving out event {e['id']}: {err!r})")
            continue
        split = "test" if e["id"] % TEST_EVERY == 0 else "train"
        kind = "positive" if label == REAL else "negative"
        if kind == "positive" and split == "train":
            pcm = trim_after_speech(pcm, vad)
        write_wav(run_dir / "clips" / split / kind / f"{e['id']}.wav", pcm)
        counts[f"{split}_{kind}"] = counts.get(f"{split}_{kind}", 0) + 1
    return counts


def trim_after_speech(pcm: np.ndarray, vad) -> np.ndarray:
    """Cut a wake's window shortly after its last speech, so "hey TARS" sits at the end of the clip, where the wake
    model learns to fire. A window with no speech found is kept whole."""
    from voice_assistant.audio import BLOCK_SAMPLES

    vad.reset()
    last = None
    for i in range(0, len(pcm) - BLOCK_SAMPLES + 1, BLOCK_SAMPLES):
        if vad(pcm[i : i + BLOCK_SAMPLES]) >= 0.5:
            last = i + BLOCK_SAMPLES
    return pcm if last is None else pcm[: min(len(pcm), last + int(END_PAD_S * 16000))]


@dataclass
class Score:
    name: str
    before: int
    after: int
    total: int  # clips (0 for per-hour rows)
    lower_is_better: bool = False
    household: bool = False
    hours: float = 0.0

    def shown(self, k: int) -> str:
        if self.hours:
            return f"{k / self.hours:.1f}"
        return f"{k} of {self.total}" if self.household else f"{100 * k / self.total:.1f}%"

    def better(self) -> bool:
        return self.after < self.before if self.lower_is_better else self.after > self.before

    def worse(self) -> bool:
        return self.after > self.before if self.lower_is_better else self.after < self.before

    def row(self) -> dict:
        return {
            "name": self.name,
            "current": self.shown(self.before),
            "candidate": self.shown(self.after),
            "lower_is_better": self.lower_is_better,
        }


class Listener:
    """A pair the way the assistant runs it: the wake model on 80 ms blocks, the check on the window before a wake."""

    def __init__(self, model: Path, threshold: float, check: dict, window_s: float):
        from voice_assistant.verify import PhraseVerifier
        from voice_assistant.wake import MicroWakeWordTrigger

        self.wake = MicroWakeWordTrigger(str(model), threshold)
        verifier = PhraseVerifier("hey tars", REPO / "models")
        verifier.use_check(check)
        self.checks = {"check": lambda pcm: verifier.check(pcm)[0]}
        self.window_s = window_s

    def answered(self, audio: np.ndarray, every: bool = False) -> int:
        return stream(self.wake, audio, self.checks, self.window_s, stop_at_first=not every)[1]["check"]


def compare(layout: Layout, run_dir: Path, before: Listener, after: Listener) -> list[Score]:
    wb, _ = bench_tools(layout, layout.root / "no-owner-recordings")
    rng = np.random.default_rng(0)

    def pad() -> np.ndarray:  # 2 s of a quiet room before and after each clip, as in pipeline.py
        return rng.normal(0, 40, 32000).astype(np.int16)

    scores = []
    for kind, name, lower in (
        ("positive", "Your held-out hey TARS", False),
        ("negative", "Your held-out wakes that weren't for TARS, let through", True),
    ):
        clips = [wb.read_wav(f) for f in sorted((run_dir / "clips" / "test" / kind).glob("*.wav"))]
        audios = [np.concatenate([pad(), c, pad()]) for c in clips]
        if audios:
            scores.append(
                Score(
                    name,
                    *(sum(lst.answered(a) > 0 for a in audios) for lst in (before, after)),
                    len(audios),
                    lower,
                    household=True,
                )
            )
    held = layout.bench / "clips_heldout"
    sets = {
        "voices": sorted((held / "hey_tars").glob("*.wav")),
        "lookalikes": sorted((held / "hey_tars_near_miss").glob("*.wav")),
    }
    banks = {n: wb.bank(n) for n in ["tv", "babble"]}
    rooms = wb.bank("rooms", limit=1000)
    counts = {key: [0, 0, 0] for key in ("quiet", "noise", "lookalikes")}  # before, after, total
    for cond, noise, snr, room in CONDITIONS:
        for set_name, files in sets.items():
            for f in files:
                c = wb.read_wav(f)
                c = wb.in_room(c, rooms[rng.integers(len(rooms))]) if room else c
                c = wb.mix(c, banks[noise][rng.integers(len(banks[noise]))], snr, rng) if noise else c
                audio = np.concatenate([pad(), c, pad()])
                key = "lookalikes" if set_name == "lookalikes" else "quiet" if cond == QUIET else "noise"
                counts[key][0] += before.answered(audio) > 0
                counts[key][1] += after.answered(audio) > 0
                counts[key][2] += 1
    scores += [
        Score("Other voices, quiet", *counts["quiet"]),
        Score("Other voices, TV and chatter", *counts["noise"]),
        Score("Lookalikes let through", *counts["lookalikes"], lower_is_better=True),
    ]
    tv = np.concatenate([wb.read_wav(f) for f in sorted((wb.INTERFERENCE / "tv_hour").glob("*.wav"))])
    for label, audio in (("TV", tv), ("audiobooks", wb.long_speech(1.0))):
        hours = len(audio) / 16000 / 3600
        scores.append(
            Score(
                f"False answers per hour, {label}",
                before.answered(audio, every=True),
                after.answered(audio, every=True),
                0,
                lower_is_better=True,
                hours=hours,
            )
        )
    return scores


def good_enough(scores: list[Score]) -> bool:
    """Better on the household's own wakes, and not one clip or false answer worse anywhere."""
    return any(s.better() for s in scores if s.household) and not any(s.worse() for s in scores)


def print_table(scores: list[Score]) -> None:
    print(f"\n{'':<58}{'in use':>12}{'new':>12}")
    for s in scores:
        mark = "  better" if s.better() else "  WORSE" if s.worse() else ""
        print(f"{s.name:<58}{s.shown(s.before):>12}{s.shown(s.after):>12}{mark}")
    print()


def install(source: Source, pair_dir: Path, based_on: str) -> None:
    if source.host is None:
        cmd = [
            "uv",
            "run",
            "voice-assistant",
            "--config",
            str(Path(source.remote) / "config.toml"),
            "--install-models",
            str(pair_dir),
            "--based-on",
            based_on,
        ]
        subprocess.run(cmd, check=True, cwd=REPO)
        return
    incoming = f"voice_data/incoming/{pair_dir.parent.name}"
    subprocess.run(["rsync", "-a", f"{pair_dir}/", f"{source.host}:{source.remote}/{incoming}/"], check=True)
    # A login shell, so uv is on the PATH the way install-pi.sh left it.
    command = f"uv run voice-assistant --install-models {incoming} --based-on {shlex.quote(based_on)}"
    print(source.run(f"bash -lc {shlex.quote(command)}"), end="")


def main():
    p = parser(__doc__)
    p.add_argument("--pi", metavar="USER@HOST", help="TARS runs there (default: on this machine)")
    p.add_argument(
        "--remote", default="voice-assistant", help="the repo's folder on the Pi (default: ~/voice-assistant)"
    )
    p.add_argument(
        "--project", type=Path, default=REPO, help="without --pi: the TARS folder on this machine (default: this repo)"
    )
    p.add_argument("--yes", action="store_true", help="don't ask: install only if it's better")
    p.add_argument("--force", action="store_true", help="install even if it isn't better (after showing why)")
    args = p.parse_args()
    layout = Layout(args.data)
    mww = layout.mww / ".venv" / "bin" / "python"
    missing = [
        str(x)
        for x in (
            mww,
            layout.check / "generic_base.npz",
            layout.features / "hey_tars",
            layout.aug / "interference.done",
        )
        if not x.exists()
    ]
    if missing:
        sys.exit("The data folder isn't ready (run training/setup/prepare.sh). Missing:\n  " + "\n  ".join(missing))
    source = Source(args.pi, args.remote) if args.pi else Source(None, str(args.project.resolve()))
    run = time.strftime("%Y%m%d-%H%M%S")
    run_dir = layout.root / "household" / run
    log(f"run {run}: copying the labeled wakes from {args.pi or 'this machine'}")
    log_, config = snapshot(source, run_dir)
    counts = household_clips(log_, run_dir)
    log(f"clips: {counts}")
    if not counts.get("train_positive") or not counts.get("test_positive"):
        sys.exit("Not enough to learn from yet: it needs real wakes both to train on and, every fifth, to test on.")
    cfg = load_config(config)
    versions = ModelVersions(
        log_.folder, REPO, cfg.wake.model, cfg.wake.check_model, cfg.wake.threshold, cfg.wake.check_window_s
    )
    current: Pair = versions.in_use()
    log(f"the pair in use: {current.version}")

    for step in (["training.stage1.features", "household", run], ["training.stage1.train", "household", "--run", run]):
        log(f"wake model: {' '.join(step)}")
        subprocess.run(
            ["nice", "-n", "10", str(mww), "-m", *step, "--data", str(layout.root)],
            check=True,
            cwd=REPO,
            env={**os.environ, "OMP_NUM_THREADS": "4"},
        )
    from training.stage2.train_check import household_layer

    log("double-check")
    pair_dir = run_dir / "pair"
    check = household_layer(layout, run_dir / "clips" / "train")
    (pair_dir / CHECK).write_text(json.dumps(check, indent=1))

    log("testing both pairs end to end")
    scores = compare(
        layout,
        run_dir,
        Listener(current.model_path, current.threshold, current.check, current.check_window_s),
        Listener(pair_dir / MODEL, current.threshold, check, current.check_window_s),
    )
    print_table(scores)
    better = good_enough(scores)
    real = counts.get("train_positive", 0)
    note = (
        f"Trained on {real + counts.get('train_negative', 0)} of your wakes ({real} real, "
        f"{counts.get('train_negative', 0)} not) on {time.strftime('%Y-%m-%d')}."
    )
    (pair_dir / ABOUT).write_text(
        json.dumps(
            {
                "threshold": current.threshold,
                "check_window_s": current.check_window_s,
                "note": note,
                "results": [s.row() for s in scores],
                "run": run,
                "counts": counts,
            },
            indent=1,
        )
    )
    if better:
        log("better for you, and no worse for anyone else: installing")
    elif args.force:
        log("not better, installing anyway (--force)")
    elif args.yes or input("Not better on everything (see WORSE above). Install anyway? [y/N] ").strip().lower() != "y":
        log(f"not installed; the run is in {run_dir}")
        return
    install(source, pair_dir, current.version)
    log(f"installed: TARS switches to it within seconds. The run is in {run_dir}")


if __name__ == "__main__":
    main()
