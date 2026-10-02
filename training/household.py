"""Train a household's own pair of wake models, test it against the pair in use, and install it only if it's better.

    uv run --group training python -m training.household                       # TARS runs on this machine
    uv run --group training python -m training.household --pi pi@tars.local    # TARS runs on a Pi: its wakes come
                                                                               # over SSH, the new pair goes back

Needs a prepared data folder (training/setup/prepare.sh), and on the Pi, the repo at --remote with its usual setup.
1. Copy the labeled wakes and near-misses (events.learning_label) and the models in use from TARS. Nothing is
   changed there, and nothing leaves this machine.
2. Hold every fifth out (by id) for testing; trim each real training wake after its last speech.
3. Train the wake model (the generic recipe plus the household's clips, 20 to 45 minutes), then the double-check on
   the generic layer's training rows plus the household's clips in every training condition.
4. Test both pairs end to end the way the assistant runs: the household's held-out wakes, held-out voices and
   lookalikes in 8 conditions, and false answers per hour on an hour of TV and of audiobooks.
5. Install it (voice-assistant --install-models) if it answers more of the household's real wakes, or lets fewer
   through that weren't for TARS, and does no worse on anything else. Otherwise ask; with --only-if-better or no
   terminal, skip it.
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

from training.audio import QUIET, Interference, conditioned, long_speech, quiet_room, read_wav
from training.common import REPO, SR, Layout, log, parser, write_wav
from training.eval.pipeline import stream
from training.stage1.train import NEGATIVES, VALIDATION_NEGATIVES
from voice_assistant.config import load_config
from voice_assistant.events import MISSED_WINDOW_S, NOT_REAL, REAL, EventLog, learning_label
from voice_assistant.versions import ABOUT, CHECK, MODEL, ModelVersions, Pair

TEST_EVERY = 5  # held out by id, so the same events stay in the test set and it only grows
END_PAD_S = 0.15
RSYNC_SOME_FILES_MISSING = (23, 24)  # a wake deleted in Review after the database was copied


@dataclass
class Source:
    """Where TARS runs: a folder on this machine (host None) or a Pi over SSH."""

    host: str | None
    remote: str

    def run(self, command: str) -> str:
        if self.host is None:
            return subprocess.run(
                command, shell=True, check=True, stdout=subprocess.PIPE, text=True, cwd=self.remote
            ).stdout
        return subprocess.run(
            ["ssh", self.host, f"cd {shlex.quote(self.remote)} && {command}"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout

    def exists(self, rel: str) -> bool:
        return self.run(f"test -e {shlex.quote(rel)} && echo yes || true").strip() == "yes"

    def pull(self, rel: str, to: Path, files: list[str] | None = None) -> None:
        """`files`: copy only these, tolerating ones deleted since they were listed."""
        to.mkdir(parents=True, exist_ok=True)
        src = f"{self.host}:{self.remote}/{rel}/" if self.host else f"{Path(self.remote) / rel}/"
        cmd = ["rsync", "-a"] + (["--files-from=-"] if files is not None else [])
        done = subprocess.run(cmd + [src, f"{to}/"], input="\n".join(files or []), text=True, check=False)
        if done.returncode and not (files is not None and done.returncode in RSYNC_SOME_FILES_MISSING):
            raise subprocess.CalledProcessError(done.returncode, cmd)


# Opened read-only so a missing database isn't created on TARS's machine.
_BACKUP = (
    "import pathlib, sqlite3; s = sqlite3.connect(pathlib.Path({src!r}).resolve().as_uri() + '?mode=ro', "
    "uri=True); d = sqlite3.connect({dst!r}); s.backup(d); d.close(); s.close()"
)


def snapshot(source: Source, run_dir: Path) -> tuple[EventLog, Path, Path]:
    """Copies config.toml, the event database, labeled audio, trained pairs and the installed pair's files (under
    project/). The database goes through sqlite's backup API, so the copy is consistent while TARS writes to it."""
    events = run_dir / "events"
    events.mkdir(parents=True, exist_ok=True)
    config = run_dir / "config.toml"
    config.write_text(source.run("cat config.toml"))
    cfg = load_config(config)
    db = f"{cfg.learning.folder}/events.db"

    def backup(dst: str) -> None:
        source.run(f"python3 -c {shlex.quote(_BACKUP.format(src=db, dst=dst))}")

    if source.host is None:
        backup(str(events / "events.db"))
    else:
        remote_tmp = source.run("umask 077 && mktemp").strip()  # transcripts and voiceprints: private
        try:
            backup(remote_tmp)
            subprocess.run(["scp", "-q", f"{source.host}:{remote_tmp}", str(events / "events.db")], check=True)
        finally:
            source.run(f"rm -f {shlex.quote(remote_tmp)}")
    log_ = EventLog(events)
    audio = [e["audio"] for e in log_.events() if e["audio"] and learning_label(e)]
    source.pull(cfg.learning.folder, events, files=audio)
    if source.exists(f"{cfg.learning.folder}/wake_models"):  # exists once a trained pair has been installed
        source.pull(f"{cfg.learning.folder}/wake_models", events / "wake_models")
    project = run_dir / "project"
    source.pull(".", project, files=[cfg.wake.model, cfg.wake.check_model])
    return log_, config, project


def splits(events: list[dict]) -> dict[int, str]:
    """{id: "train" | "test"}. An event within MISSED_WINDOW_S of the previous one shares its split: a quick second
    "hey TARS" often has the first in its window, and the same audio mustn't be both trained and tested on."""
    split: dict[int, str] = {}
    last = None
    for e in sorted(events, key=lambda e: e["ts"]):
        if last is not None and e["ts"] - last["ts"] <= MISSED_WINDOW_S:
            split[e["id"]] = split[last["id"]]
        else:
            split[e["id"]] = "test" if e["id"] % TEST_EVERY == 0 else "train"
        last = e
    return split


def household_clips(log_: EventLog, run_dir: Path, vad) -> dict[str, int]:
    """Writes clips/{train,test}/{positive,negative}/<id>.wav and returns the counts."""
    from voice_assistant.audio import read_wav as read_event_audio

    events = [e for e in log_.events() if e["audio"]]
    split = splits(events)
    counts: dict[str, int] = {}
    for e in events:
        label = learning_label(e)
        path = log_.folder / e["audio"]
        if label not in (REAL, NOT_REAL) or not path.exists():
            continue
        try:
            pcm = read_event_audio(path)
        except (OSError, EOFError) as err:
            log(f"leaving out event {e['id']}: {err!r}")
            continue
        kind = "positive" if label == REAL else "negative"
        if kind == "positive" and split[e["id"]] == "train":
            pcm = trim_after_speech(pcm, vad)
        write_wav(run_dir / "clips" / split[e["id"]] / kind / f"{e['id']}.wav", pcm)
        counts[f"{split[e['id']]}_{kind}"] = counts.get(f"{split[e['id']]}_{kind}", 0) + 1
    return counts


def trim_after_speech(pcm: np.ndarray, vad) -> np.ndarray:
    """Puts "hey TARS" at the end of the clip, where the wake model learns to fire. A window without speech is kept
    whole."""
    from voice_assistant.audio import BLOCK_SAMPLES

    vad.reset()
    last = None
    for i in range(0, len(pcm) - BLOCK_SAMPLES + 1, BLOCK_SAMPLES):
        if vad(pcm[i : i + BLOCK_SAMPLES]) >= 0.5:
            last = i + BLOCK_SAMPLES
    return pcm if last is None else pcm[: min(len(pcm), last + int(END_PAD_S * SR))]


@dataclass
class Score:
    name: str
    before: int
    after: int
    total: int  # 0 for per-hour rows
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
    rng = np.random.default_rng(0)
    scores = []
    for kind, name, lower in (
        ("positive", "Your held-out hey TARS", False),
        ("negative", "Your held-out wakes that weren't for TARS, let through", True),
    ):
        clips = [read_wav(f) for f in sorted((run_dir / "clips" / "test" / kind).glob("*.wav"))]
        audios = [np.concatenate([quiet_room(rng), c, quiet_room(rng)]) for c in clips]
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
    sets = {
        "voices": (sorted((layout.heldout / "hey_tars").glob("*.wav")), True),
        "lookalikes": (sorted((layout.heldout / "hey_tars_near_miss").glob("*.wav")), False),
    }
    counts = {key: [0, 0, 0] for key in ("quiet", "noise", "lookalikes")}  # before, after, total
    for cond, set_name, _should, clip in conditioned(sets, Interference(layout.interference), rng):
        audio = np.concatenate([quiet_room(rng), clip, quiet_room(rng)])
        key = "lookalikes" if set_name == "lookalikes" else "quiet" if cond == QUIET else "noise"
        counts[key][0] += before.answered(audio) > 0
        counts[key][1] += after.answered(audio) > 0
        counts[key][2] += 1
    scores += [
        Score("Other voices, quiet", *counts["quiet"]),
        Score("Other voices, TV and chatter", *counts["noise"]),
        Score("Lookalikes let through", *counts["lookalikes"], lower_is_better=True),
    ]
    tv = np.concatenate([read_wav(f) for f in sorted((layout.interference / "tv_hour").glob("*.wav"))])
    for label, audio in (("TV", tv), ("audiobooks", long_speech(layout.librispeech_test, 1.0))):
        hours = len(audio) / SR / 3600
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
    source.run(f"mkdir -p {shlex.quote(incoming)}")  # GNU rsync creates only the last folder of the destination
    subprocess.run(["rsync", "-a", f"{pair_dir}/", f"{source.host}:{source.remote}/{incoming}/"], check=True)
    # A login shell, so uv is on the PATH the way install-pi.sh left it.
    command = f"uv run voice-assistant --install-models {incoming} --based-on {shlex.quote(based_on)}"
    print(source.run(f"bash -lc {shlex.quote(command)}"), end="")


def not_ready(layout: Layout) -> list[Path]:
    negatives = layout.mww / "negative_datasets"
    needed = [
        layout.mww / ".venv" / "bin" / "python",
        layout.check / "generic_base.npz",
        layout.features / "hey_tars",
        layout.aug / "interference.done",
        *(negatives / n for n in [*NEGATIVES, VALIDATION_NEGATIVES]),
        layout.heldout / "hey_tars",
        layout.heldout / "hey_tars_near_miss",
        layout.librispeech_test,
        *(layout.interference / n for n in ("tv", "babble", "rooms", "tv_hour")),
    ]
    return [p for p in needed if not p.exists()]


def main():
    p = parser(__doc__)
    p.add_argument("--pi", metavar="USER@HOST", help="TARS runs there (default: on this machine)")
    p.add_argument(
        "--remote", default="voice-assistant", help="the repo's folder on the Pi (default: ~/voice-assistant)"
    )
    p.add_argument(
        "--project", type=Path, default=REPO, help="without --pi: the TARS folder on this machine (default: this repo)"
    )
    p.add_argument("--only-if-better", action="store_true", help="don't ask: install it only if it's better")
    p.add_argument("--force", action="store_true", help="install even if it isn't better (after showing why)")
    args = p.parse_args()
    layout = Layout(args.data)
    missing = not_ready(layout)
    if missing:
        raise SystemExit(
            "The data folder isn't ready (run training/setup/prepare.sh). Missing:\n  " + "\n  ".join(map(str, missing))
        )
    from voice_assistant.vad import SileroVAD
    from voice_assistant.verify import ensure_model

    ensure_model(REPO / "models")  # the double-check's recognizer, before anything slow
    vad = SileroVAD(REPO / "models" / "silero_vad.onnx")
    source = Source(args.pi, args.remote) if args.pi else Source(None, str(args.project.resolve()))
    run = time.strftime("%Y%m%d-%H%M%S")
    run_dir = layout.household / run
    log(f"run {run}: copying the labeled wakes from {args.pi or 'this machine'}")
    log_, config, project = snapshot(source, run_dir)
    counts = household_clips(log_, run_dir, vad)
    log(f"clips: {counts}")
    if not counts.get("train_positive") or not counts.get("test_positive"):
        raise SystemExit(
            "Not enough to learn from yet: it needs real wakes both to train on and, every fifth, to test on."
        )
    cfg = load_config(config)
    versions = ModelVersions(
        log_.folder, project, cfg.wake.model, cfg.wake.check_model, cfg.wake.threshold, cfg.wake.check_window_s
    )
    current: Pair = versions.in_use()
    log(f"the pair in use: {current.version}")

    mww = layout.mww / ".venv" / "bin" / "python"
    for step in (["training.stage1.features", "household", run], ["training.stage1.train", "household", "--run", run]):
        log(f"wake model: {' '.join(step)}")
        subprocess.run(
            ["nice", "-n", "19", str(mww), "-m", *step, "--data", str(layout.root)],
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
    elif (
        args.only_if_better
        or not sys.stdin.isatty()
        or input("Not better on everything (see WORSE above). Install anyway? [y/N] ").strip().lower() != "y"
    ):
        log(f"not installed; the run is in {run_dir}")
        return
    install(source, pair_dir, current.version)
    log(f"installed: TARS switches to it within seconds. The run is in {run_dir}")


if __name__ == "__main__":
    main()
