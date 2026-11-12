"""House commands on the Pi, measured on the household's own recorded takes (docs/home.md#house-commands-on-the-pi).

    uv run python tools/commands_bench.py                  # every house recording session's takes, Vosk only: free
    uv run python tools/commands_bench.py --cloud          # also the cloud's, for sessions made with --cloud
    uv run python tools/commands_bench.py --session TOKEN  # one session's

Each take from a house recording session (voice-assistant --record-session house --for NAME) is played to Vosk,
limited to the house's phrases as commands_on_pi builds them, and what it heard is matched as TARS would. Against
what the take means, it's right (the command it is, or nothing for words that mustn't count), missed (a command
heard as nothing: it goes to the model, which costs time, not a mistake) or wrong (the one failure that matters: a
command it isn't). With --cloud, each take of a session made with --cloud (--record-session house --cloud, whose
page told the person so) also goes to Deepgram's endpoint for whole recordings, timed: what local_first adds for a
request that isn't a command, and the transcript commands_on_pi matches when the cloud is there. A session made
without it said its takes stay on the Pi, so they're never sent: only Vosk hears them. About $0.004 a minute of takes
sent. A missed command is also matched as written, to tell words the phrases don't have from words Vosk didn't hear.

Every take's result is saved (voice_data/bench/commands/<time>.jsonl), with what it was run on; a take whose audio is
missing or damaged is skipped and counted. The exit code is 1 if any take was wrong, heard by Vosk or by the cloud.
"""

import argparse
import functools
import json
import statistics
import subprocess
import sys
import time
import wave
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).parents[1]
sys.path.insert(0, str(REPO / "src"))

from voice_assistant.commands import Command, Commands
from voice_assistant.errors import ServiceError
from voice_assistant.recording import HOUSE

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
    problem: str | None = None  # what the recording session's check found wrong with the take (recording.check)
    written: str = ""  # what the words it was meant to say match, as written: a missed command the phrases don't have
    cloud_text: str | None = None
    cloud_matched: str | None = None
    cloud_outcome: str | None = None
    cloud_ms: float | None = None
    cloud_error: str | None = None
    stays: bool = False  # in a run with the cloud, not sent to it: its session was made to stay on the Pi


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


def score(
    takes: list[dict],
    commands: Commands,
    names: dict[str, str],
    hear: Callable[[bytes], str],
    cloud: Callable[[bytes], str] | None = None,
) -> list[Result]:
    """Each take ({session, prompt, say, means, pcm, seconds, problem, cloud}) heard by `hear(pcm)` and, given
    `cloud(pcm)`, by the cloud too, if its session was made for that (its `cloud`); never otherwise. The cloud failing
    on a take is kept on its result, not the end of the run."""
    results = []
    for t in takes:
        want = expected(t["means"], names)
        start = time.perf_counter()
        heard = hear(t["pcm"])
        vosk_ms = (time.perf_counter() - start) * 1000
        got = commands.match(heard) if heard else None
        r = Result(t["session"], t["prompt"], t["say"], t["means"], text(want), heard, text(got), outcome(want, got),
                   round(vosk_ms, 1), t["seconds"], t.get("problem"), text(commands.match(t["say"])))  # fmt: skip
        if cloud and not t.get("cloud"):
            r.stays = True
        elif cloud:
            start = time.perf_counter()
            try:
                r.cloud_text = cloud(t["pcm"])
            except ServiceError as e:
                r.cloud_error = str(e)
            else:
                r.cloud_ms = round((time.perf_counter() - start) * 1000, 1)
                cloud_got = commands.match(r.cloud_text)
                r.cloud_matched, r.cloud_outcome = text(cloud_got), outcome(want, cloud_got)
        results.append(r)
    return results


def counted(results: list[Result], outcome_of: Callable[[Result], str | None]) -> str:
    right, missed, wrong = (sum(outcome_of(r) == o for r in results) for o in ("right", "missed", "wrong"))
    return f"{right} right, {missed} missed, {wrong} wrong"


def any_wrong(results: list[Result]) -> bool:
    return any("wrong" in (r.outcome, r.cloud_outcome) for r in results)


def summary(results: list[Result], skipped: int = 0) -> str:
    lines = []
    for kind, picked in (
        ("commands", [r for r in results if r.expected]),
        ("not commands", [r for r in results if not r.expected]),
    ):
        if not picked:
            continue
        line = f"{kind:13} {len(picked):3} takes: {counted(picked, lambda r: r.outcome)}"
        missed = [r for r in picked if r.outcome == "missed"]
        if missed:
            unknown = sum(r.written != r.expected for r in missed)
            line += f" ({unknown} of the missed not in the phrases, {len(missed) - unknown} not heard)"
        lines.append(line)
    if skipped:
        lines.append(f"{skipped} takes skipped: their audio is missing or damaged")
    if results:
        lines.append(f"Vosk: median {statistics.median(r.vosk_ms for r in results):.0f} ms a take")
    timed = [r for r in results if r.cloud_outcome is not None]
    if timed:
        asks = [r.cloud_ms for r in timed if not r.expected]
        if asks:
            lines.append(f"cloud, whole recordings, for what isn't a command: median {statistics.median(asks):.0f} ms")
        lines.append(f"the cloud's transcripts, matched: {counted(timed, lambda r: r.cloud_outcome)}")
    if failed := sum(r.cloud_error is not None for r in results):
        lines.append(f"the cloud failed on {failed} takes (each one's error is kept with its result)")
    if stays := sum(r.stays for r in results):
        lines.append(f"{stays} takes not sent to the cloud: their sessions were made to stay on the Pi")
    for r in results:
        if r.outcome == "wrong" or r.cloud_outcome == "wrong":
            problem = f" ({r.problem})" if r.problem else ""
            lines.append(
                f"  WRONG {r.prompt}{problem}: {r.said!r} heard {r.heard!r} as {r.matched or '-'}"
                f" (cloud: {r.cloud_matched})"
            )
    return "\n".join(lines)


def read_takes(store, session_token: str | None) -> tuple[list[dict], list[str]]:
    """Every house session's takes that have audio (or one session's, by its token), with what each prompt said and
    meant and whether its session lets it go to the cloud; and why each take whose audio couldn't be read was left
    out. A token no house session has is a LookupError."""
    query = "SELECT * FROM sessions WHERE kind = ?" + (" AND token = ?" if session_token else "")
    sessions = store.rows(query, (HOUSE, session_token) if session_token else (HOUSE,))
    if session_token and not sessions:
        raise LookupError("No house recording session has that token.")
    takes, skipped = [], []
    for s in sessions:
        prompts = {p["id"]: p for p in json.loads(s["prompts"])}
        for t in store.rows("SELECT * FROM takes WHERE session_id = ? AND audio IS NOT NULL", (s["id"],)):
            p = prompts.get(t["prompt"])
            if p is None or p.get("means") is None:
                continue
            try:
                with wave.open(str(store.folder / t["audio"])) as w:
                    pcm = w.readframes(w.getnframes())
            except (OSError, EOFError, wave.Error) as e:
                skipped.append(f"session {s['id']}, {t['prompt']}: {e}")
                continue
            takes.append({"session": s["id"], "prompt": t["prompt"], "say": p["say"], "means": p["means"], "pcm": pcm,
                          "seconds": t["seconds"], "problem": t["problem"], "cloud": bool(s["cloud"])})  # fmt: skip
    return takes, skipped


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--config", type=Path, default=REPO / "config.toml")
    p.add_argument("--session", help="one session's takes, by its link's token")
    p.add_argument(
        "--cloud",
        action="store_true",
        help="also time the cloud's speech to text, for sessions made with --cloud (paid)",
    )
    p.add_argument("--save", type=Path, help="where to keep the results (default voice_data/bench/commands/)")
    args = p.parse_args()

    from voice_assistant.__main__ import DEEPGRAM_KEY, api_key, make_event_log, make_home
    from voice_assistant.commands import device_words, vosk_hearing
    from voice_assistant.config import load_config

    cfg = load_config(args.config)
    root = args.config.resolve().parent
    log = make_event_log(cfg, root)
    home = make_home(cfg, root, events=log)
    if log is None or home is None:
        print("The bench needs the event log and the house's devices.")
        return 2
    found = device_words(home.devices)
    commands = Commands(found)
    names = {d.label.lower(): d.name for d in found} | {d.name: d.name for d in found}
    try:
        takes, skipped = read_takes(log.store, args.session)
    except LookupError as e:
        print(e)
        return 2
    for why in skipped:
        print(f"(skipped {why})")
    if not takes:
        print("No house takes yet: voice-assistant --record-session house --for NAME")
        return 2
    cloud: Callable[[bytes], str] | None = None
    sent = [t for t in takes if t["cloud"]] if args.cloud else []
    if args.cloud and not sent:
        print("(no session here was made with --cloud: their takes stay on the Pi, so only Vosk hears them)")
    elif sent:
        from voice_assistant.stt import DeepgramFileTranscriber, transcribe_recording

        minutes = sum(t["seconds"] for t in sent) / 60
        print(f"{len(sent)} takes to Deepgram, {minutes:.1f} minutes: about ${minutes * PRICE_A_MINUTE:.2f}")
        deepgram = DeepgramFileTranscriber(api_key(root / ".env", DEEPGRAM_KEY), cfg.stt)
        cloud = functools.partial(transcribe_recording, deepgram)

    results = score(takes, commands, names, vosk_hearing(commands.grammar(), root / "models"), cloud)
    print(summary(results, len(skipped)))
    save = args.save or root / "voice_data" / "bench" / "commands" / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    save.parent.mkdir(parents=True, exist_ok=True)
    sha = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    )
    meta = {"run": time.strftime("%Y-%m-%dT%H:%M:%S"), "code": sha.stdout.strip(), "phrases": len(commands.grammar()),
            "takes": len(takes), "skipped": len(skipped), "cloud": bool(cloud)}  # fmt: skip
    save.write_text("\n".join([json.dumps({"meta": meta}), *(json.dumps(asdict(r)) for r in results)]) + "\n")
    print(f"(kept in {save})")
    return 1 if any_wrong(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
