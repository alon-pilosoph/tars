"""House commands on the Pi, measured on the household's own recorded takes (docs/home.md#house-commands-on-the-pi).

    uv run python tools/commands_bench.py                  # every house recording session's takes, Vosk only: free
    uv run python tools/commands_bench.py --cloud          # also the cloud's speech to text for whole recordings
    uv run python tools/commands_bench.py --session TOKEN  # one session's

Each take from a house recording session (voice-assistant --record-session house --for NAME) is played to Vosk,
limited to the house's phrases as commands_on_pi builds them, and what it heard is matched as TARS would. Against
what the take means, it's right (the command it is, or nothing for words that mustn't count), missed (a command
heard as nothing: it goes to the model, which costs time, not a mistake) or wrong (the one failure that matters: a
command it isn't). With --cloud, each take also goes to Deepgram's endpoint for whole recordings, timed: what
local_first adds for a request that isn't a command, and the transcript commands_on_pi matches when the cloud is
there. About $0.004 a minute of takes.

Every take's result is saved (voice_data/bench/commands/<time>.jsonl), with what it was run on. The exit code is 1 if
any take was wrong.
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).parents[1]
sys.path.insert(0, str(REPO / "src"))

from voice_assistant.commands import Command, Commands

COMMAND_ACTIONS = ("on", "off", "lock", "set")  # what the Pi does; anything else a take asks for goes to the model
PRICE_A_MINUTE = 0.0043  # Deepgram's endpoint for whole recordings (Nova-3), USD


@dataclass
class Result:
    session: int
    prompt: str
    said: str  # the words the take was meant to say
    means: str
    expected: str  # the command it should be, or "" for none
    heard: str  # what Vosk heard
    matched: str
    outcome: str  # right | missed | wrong
    vosk_ms: float
    seconds: float
    cloud_text: str | None = None
    cloud_matched: str | None = None
    cloud_ms: float | None = None


def expected(means: str, names: dict[str, str]) -> Command | None:
    """The command a take's meaning is, as the Pi would do it: "AC|set:23" is set on ac, at 23; "" or anything the
    Pi doesn't do (unlocking, a question) is None."""
    if not means:
        return None
    device, _, action = means.partition("|")
    action, _, t = action.partition(":")
    if action not in COMMAND_ACTIONS:
        return None
    return Command(names.get(device.lower(), device.lower()), action, int(t) if t else None)


def outcome(want: Command | None, got: Command | None) -> str:
    if got == want:
        return "right"
    return "missed" if got is None else "wrong"


def text(c: Command | None) -> str:
    return "" if c is None else f"{c.device}|{c.action}" + (f":{c.temperature}" if c.temperature else "")


def score(takes: list[dict], commands: Commands, names: dict[str, str], hear, cloud=None) -> list[Result]:
    """Each take ({session, prompt, say, means, pcm, seconds}) heard by `hear(pcm)` and, given `cloud(pcm)`, by the
    cloud too."""
    results = []
    for t in takes:
        want = expected(t["means"], names)
        start = time.perf_counter()
        heard = hear(t["pcm"])
        vosk_ms = (time.perf_counter() - start) * 1000
        got = commands.match(heard) if heard else None
        r = Result(t["session"], t["prompt"], t["say"], t["means"], text(want), heard, text(got), outcome(want, got),
                   round(vosk_ms, 1), t["seconds"])  # fmt: skip
        if cloud:
            start = time.perf_counter()
            r.cloud_text = cloud(t["pcm"])
            r.cloud_ms = round((time.perf_counter() - start) * 1000, 1)
            r.cloud_matched = text(commands.match(r.cloud_text))
        results.append(r)
    return results


def summary(results: list[Result]) -> str:
    lines = []
    for kind, picked in (
        ("commands", [r for r in results if r.expected]),
        ("not commands", [r for r in results if not r.expected]),
    ):
        if not picked:
            continue
        counts = {o: sum(r.outcome == o for r in picked) for o in ("right", "missed", "wrong")}
        right, missed, wrong = (counts[o] for o in ("right", "missed", "wrong"))
        lines.append(f"{kind:13} {len(picked):3} takes: {right} right, {missed} missed, {wrong} wrong")
    if results:
        lines.append(f"Vosk: median {statistics.median(r.vosk_ms for r in results):.0f} ms a take")
    timed = [r for r in results if r.cloud_ms is not None]
    if timed:
        asks = [r.cloud_ms for r in timed if not r.expected]
        if asks:
            lines.append(f"cloud, whole recordings, for what isn't a command: median {statistics.median(asks):.0f} ms")
        cloud_wrong = sum(1 for r in timed if r.cloud_matched and r.cloud_matched != r.expected)
        cloud_right = sum(1 for r in timed if r.cloud_matched == r.expected)
        lines.append(f"the cloud's transcripts, matched: {cloud_right} right, {cloud_wrong} wrong")
    for r in results:
        if r.outcome == "wrong" or (r.cloud_matched and r.cloud_matched != r.expected):
            lines.append(
                f"  WRONG {r.prompt}: {r.said!r} heard {r.heard!r} as {r.matched or '-'} (cloud: {r.cloud_matched})"
            )
    return "\n".join(lines)


def read_takes(store, session_token: str | None) -> list[dict]:
    """Every house session's takes that have audio, with what each prompt said and meant."""
    query = "SELECT * FROM sessions WHERE kind = 'house'" + (" AND token = ?" if session_token else "")
    takes = []
    for s in store.rows(query, (session_token,) if session_token else ()):
        prompts = {p["id"]: p for p in json.loads(s["prompts"])}
        for t in store.rows("SELECT * FROM takes WHERE session_id = ? AND audio IS NOT NULL", (s["id"],)):
            p = prompts.get(t["prompt"])
            if p is None or p.get("means") is None:
                continue
            with wave.open(str(store.folder / t["audio"])) as w:
                pcm = w.readframes(w.getnframes())
            takes.append({"session": s["id"], "prompt": t["prompt"], "say": p["say"], "means": p["means"], "pcm": pcm,
                          "seconds": t["seconds"]})  # fmt: skip
    return takes


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--config", type=Path, default=REPO / "config.toml")
    p.add_argument("--session", help="one session's takes, by its link's token")
    p.add_argument("--cloud", action="store_true", help="also time the cloud's speech to text (paid)")
    p.add_argument("--save", type=Path, help="where to keep the results (default voice_data/bench/commands/)")
    args = p.parse_args()

    from voice_assistant.__main__ import DEEPGRAM_KEY, api_key, make_event_log, make_home
    from voice_assistant.commands import vosk_hearing
    from voice_assistant.config import load_config
    from voice_assistant.home import SET

    cfg = load_config(args.config)
    root = args.config.resolve().parent
    log = make_event_log(cfg, root)
    home = make_home(cfg, root, events=log)
    if log is None or home is None:
        print("The bench needs the event log and the house's devices.")
        return 2
    found = [
        (n, d.display, tuple(d.actions), SET in d.actions and d.takes_temperature) for n, d in home.devices.items()
    ]
    commands = Commands(found)
    names = {d.display.lower(): n for n, d in home.devices.items()} | {n: n for n in home.devices}
    takes = read_takes(log.store, args.session)
    if not takes:
        print("No house takes yet: voice-assistant --record-session house --for NAME")
        return 2
    minutes = sum(t["seconds"] for t in takes) / 60
    cloud = None
    if args.cloud:
        from voice_assistant.stt import DeepgramFileTranscriber

        print(f"{len(takes)} takes, {minutes:.1f} minutes: about ${minutes * PRICE_A_MINUTE:.2f} at Deepgram")
        deepgram = DeepgramFileTranscriber(api_key(root / ".env", DEEPGRAM_KEY), cfg.stt)

        def cloud(pcm: bytes) -> str:
            session = deepgram.session()
            session.feed(pcm)
            return session.transcript()

    results = score(takes, commands, names, vosk_hearing(commands.grammar(), root / "models"), cloud)
    print(summary(results))
    save = args.save or root / "voice_data" / "bench" / "commands" / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    save.parent.mkdir(parents=True, exist_ok=True)
    sha = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    )
    meta = {"run": time.strftime("%Y-%m-%dT%H:%M:%S"), "code": sha.stdout.strip(), "phrases": len(commands.grammar()),
            "takes": len(takes), "cloud": bool(cloud)}  # fmt: skip
    save.write_text("\n".join([json.dumps({"meta": meta}), *(json.dumps(asdict(r)) for r in results)]) + "\n")
    print(f"(kept in {save})")
    return 1 if any(r.outcome == "wrong" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
