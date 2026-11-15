"""Wake models measured on real microphones: the wake tests' takes and the wakes TARS kept, never used for training
(docs/wake-word.md#measuring-on-real-microphones).

    uv run python tools/wake_scorecard.py fetch --pi alonpi@homepi.local    # copy them from TARS (read only)
    uv run python tools/wake_scorecard.py score MODEL.tflite [MODEL ...]     # the scorecard, first model the reference

`fetch` copies, into DIR (default ~/tars-training/real), every wake test's takes (--record-session wake) with what
each prompt asked for and where it was recorded, and the wakes and near-misses TARS kept that have an answer: a
person's in Review, or a request that followed. Nothing on TARS changes. `score` plays each to each model the way the
assistant listens (stage 1 only, the model reset before each clip) and compares them at the same strictness: each
model's threshold is the lowest at which it wakes to no more of the things that weren't "hey TARS" (lookalikes said
in the wake tests, and the kept wakes answered "not it") than the first model does at its own (--threshold). Then
it reports, per model, the "hey TARS" it caught by device, by how they were said and by who said them, and how often
it woke to a minute of the room with the TV on.
"""

import argparse
import json
import shlex
import subprocess
import sys
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).parents[1]
sys.path.insert(0, str(REPO / "src"))

from voice_assistant.audio import SAMPLE_RATE

BLOCK = 1280  # the assistant's 80 ms blocks
LEAD_S, TAIL_S = 0.5, 1.0  # quiet around each clip, as a wake comes out of a quiet room and is scored after it
REFRACTORY_S = 2.0  # a wake on the room counts once, as TARS stops listening for its name when it wakes
GRID = [round(0.05 * i, 2) for i in range(1, 20)]
HOW = {
    "": "close",
    "From across the room": "far",
    "Quietly, as if someone's asleep": "softly",
    "With the TV or music on, as loud as you'd usually have it": "TV",
    "While other people are talking nearby": "people talking",
    "In one breath, as you'd ask": "into a request",
}
DEFAULT_DIR = Path.home() / "tars-training" / "real"

# Runs on TARS: what fetch needs from its event database, as JSON on stdout.
REMOTE = r"""
import json, sqlite3
db = sqlite3.connect("file:voice_data/events/events.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row
out = {"takes": [], "kept": []}
for s in db.execute("SELECT * FROM sessions WHERE kind = 'wake'"):
    prompts = {p["id"]: p for p in json.loads(s["prompts"])}
    for t in db.execute("SELECT * FROM takes WHERE session_id = ? AND audio IS NOT NULL", (s["id"],)):
        p = prompts.get(t["prompt"], {})
        out["takes"].append({
            "audio": t["audio"], "person": s["person"], "device": s["device"] or "TARS's speakerphone",
            "prompt": t["prompt"], "say": p.get("say", ""), "how": p.get("how", ""), "means": p.get("means"),
            "seconds": p.get("seconds", 0), "problem": t["problem"],
        })
for e in db.execute("SELECT * FROM events WHERE audio IS NOT NULL"):
    label = e["label"] or ("real" if e["kind"] == "wake" and e["follow"] == "asked" else None)
    if label:
        out["kept"].append({"audio": e["audio"], "label": label, "kind": e["kind"], "outcome": e["outcome"]})
print(json.dumps(out))
"""


def fetch(pi: str, remote: str, out: Path) -> None:
    folder = f"{remote}/voice_data/events"
    found = json.loads(
        subprocess.run(
            ["ssh", pi, f"cd {shlex.quote(remote)} && python3 -"], input=REMOTE, text=True, capture_output=True,
            check=True,
        ).stdout
    )  # fmt: skip
    paths = sorted({x["audio"] for x in found["takes"] + found["kept"]})
    out.mkdir(parents=True, exist_ok=True)
    tar = subprocess.Popen(["ssh", pi, f"cd {shlex.quote(folder)} && tar cf - -T -"], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE)  # fmt: skip
    unpack = subprocess.Popen(["tar", "xf", "-", "-C", str(out)], stdin=tar.stdout)
    tar.stdin.write("\n".join(paths).encode())
    tar.stdin.close()
    if unpack.wait() or tar.wait():
        raise SystemExit("copying the recordings from TARS failed")
    (out / "manifest.json").write_text(json.dumps(found, indent=1))
    real = sum(k["label"] == "real" for k in found["kept"])
    print(f"{len(found['takes'])} wake test takes and {len(found['kept'])} kept wakes ({real} real) in {out}")


def read(path: Path) -> np.ndarray | None:
    try:
        with wave.open(str(path)) as w:
            if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1:
                return None
            return np.frombuffer(w.readframes(w.getnframes()), np.int16)
    except OSError, EOFError, wave.Error:
        return None


def scores(trigger, pcm: np.ndarray) -> np.ndarray:
    """The model's score for each 80 ms block, freshly reset, with quiet around the clip."""
    trigger.reset()
    pad = lambda s: np.zeros(int(s * SAMPLE_RATE), np.int16)
    audio = np.concatenate([pad(LEAD_S), pcm, pad(TAIL_S)])
    return np.array([trigger.score(audio[i : i + BLOCK]) for i in range(0, len(audio), BLOCK)])


def fires(series: np.ndarray, threshold: float) -> int:
    """Wakes over a long take: each time the score reaches the threshold, then not again for REFRACTORY_S."""
    count, until, per_s = 0, -1, SAMPLE_RATE / BLOCK
    for i, s in enumerate(series):
        if s >= threshold and i > until:
            count, until = count + 1, i + int(REFRACTORY_S * per_s)
    return count


def condition(take: dict) -> str:
    """How a wake test's take was said; a kept wake, as it happened."""
    if "how" not in take:
        return "as it happened"
    return HOW.get(take["how"], take["how"] or "close")


def score(models: list[Path], data: Path, threshold: float) -> dict:
    from voice_assistant.wake import MicroWakeWordTrigger

    manifest = json.loads((data / "manifest.json").read_text())
    clips = []  # (kind, take): kind is "wake", "not", or "room"
    for t in manifest["takes"]:
        if t["seconds"]:
            clips.append(("room", t))
        elif t["means"] == "hey tars":
            clips.append(("wake", {**t, "set": "wake tests"}))
        elif t["means"] == "":
            clips.append(("not", {**t, "set": "wake tests"}))
    for k in manifest["kept"]:
        clips.append(("wake" if k["label"] == "real" else "not", {**k, "set": "kept by TARS", "device": "TARS"}))
    audio = {c["audio"]: read(data / c["audio"]) for _, c in clips}
    clips = [(kind, c) for kind, c in clips if audio[c["audio"]] is not None]
    report = {}
    for model in models:
        trigger = MicroWakeWordTrigger(str(model), threshold)
        peak, room = [], []
        for kind, c in clips:
            series = scores(trigger, audio[c["audio"]])
            if kind == "room":
                room.append(series)
            else:
                peak.append((kind, c, float(series.max())))
        report[model.stem] = {"peak": peak, "room": room}
    reference = next(iter(report.values()))
    negatives = lambda r, t: sum(s >= t for k, _, s in r["peak"] if k == "not")
    allowed = negatives(reference, threshold)
    hours = sum(len(s) for s in reference["room"]) * BLOCK / SAMPLE_RATE / 3600
    out = {"threshold": threshold, "negatives": sum(k == "not" for k, _, _ in reference["peak"]), "room_hours": hours}
    for name, r in report.items():
        t = threshold if r is reference else next((g for g in GRID if negatives(r, g) <= allowed), GRID[-1])
        caught = defaultdict(lambda: [0, 0])
        for kind, c, s in r["peak"]:
            if kind != "wake":
                continue
            for key in ("all", f"set: {c['set']}", f"device: {c['device']}", f"said: {condition(c)}",
                        f"who: {c.get('person', 'household')}"):  # fmt: skip
                caught[key][0] += s >= t
                caught[key][1] += 1
        out[name] = {
            "threshold": t,
            "woke to not hey TARS": negatives(r, t),
            "room wakes per hour": round(sum(fires(s, t) for s in r["room"]) / hours, 1) if hours else None,
            "caught": {k: v for k, v in sorted(caught.items())},
        }
    return out


def show(result: dict) -> None:
    names = [n for n in result if isinstance(result[n], dict)]
    keys = sorted({k for n in names for k in result[n]["caught"]}, key=lambda k: (k != "all", k))
    width = max(len(k) for k in keys + ["at threshold"]) + 2
    print(f"Same strictness: each wakes to no more of the {result['negatives']} not-hey-TARS clips than {names[0]}.")
    print("".ljust(width) + "".join(n[:18].rjust(20) for n in names))
    print("at threshold".ljust(width) + "".join(f"{result[n]['threshold']:.2f}".rjust(20) for n in names))
    woke = "".join(str(result[n]["woke to not hey TARS"]).rjust(20) for n in names)
    print("woke to not hey TARS".ljust(width) + woke)
    if result["room_hours"]:
        line = "".join(f"{result[n]['room wakes per hour']}/h".rjust(20) for n in names)
        print(f"room, {result['room_hours'] * 60:.0f} min".ljust(width) + line)
    for k in keys:
        cells = []
        for n in names:
            got, of = result[n]["caught"].get(k, [0, 0])
            cells.append(f"{got}/{of} {100 * got / of:3.0f}%".rjust(20) if of else "".rjust(20))
        print(k.ljust(width) + "".join(cells))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help="copy the wake tests' takes and the kept wakes from TARS")
    f.add_argument("--pi", required=True, help="where TARS runs, as ssh reaches it (user@host)")
    f.add_argument("--remote", default="voice-assistant", help="TARS's folder there (default ~/voice-assistant)")
    f.add_argument("--data", type=Path, default=DEFAULT_DIR)
    s = sub.add_parser("score", help="the scorecard")
    s.add_argument("models", nargs="+", type=Path, help="stage 1 models (.tflite); the first is the reference")
    s.add_argument("--threshold", type=float, default=0.3, help="the reference model's threshold (TARS's: 0.3)")
    s.add_argument("--data", type=Path, default=DEFAULT_DIR)
    s.add_argument("--save", type=Path, help="also write the numbers as JSON")
    args = p.parse_args()
    if args.command == "fetch":
        fetch(args.pi, args.remote, args.data)
        return 0
    result = score(args.models, args.data, args.threshold)
    show(result)
    if args.save:
        args.save.write_text(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
