"""What TARS's browser agent learns about a site from working it: a few short notes a site, written after each run
and read at the start of the next one there, so a site it has been to before goes quicker.

The notes are what a person would jot down: the addresses that go straight to something (a search with the query
in it), what things are called and where they are, what failed and what worked instead, what the site asks for
before the checkout. They are written by the model from the run's own trace (what it did and what each step did,
not the pages' text), about the site and never about the person: no names, addresses or orders. The agent reads them
as hints, and its goal still decides; the gate holds whatever they say.

They're plain text, one file a site (voice_data/browser/notes/wolt.com.md), so a person can read, fix or delete
them."""

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

FOLDER = Path(__file__).resolve().parents[3] / "voice_data/browser/notes"
MOST = 2_000  # characters of notes a site
TRACE = 12_000  # characters of a run's trace sent to write them
# Second-level domains under a country's: wolt.com, but ksp.co.il and ikea.co.il.
UNDER_COUNTRY = {"co", "com", "org", "net", "gov", "ac", "edu"}

LEARN = """You keep short notes about a website, so that a browser agent working it next time gets there in fewer \
steps. Below are the notes so far (maybe none) and the trace of a run on {site}: each step the agent took and what \
it did.

Write the notes again, at most 12 short bullets, keeping what still holds and adding what this run showed:
- addresses that go straight to something, with the pattern (https://site/search?q=<words>)
- what things are called on the site and where they are (the basket button, the filter for ratings)
- steps that failed or changed nothing, and what worked instead
- what the site asks for on the way to checkout or booking (options to pick, a delivery address, a sign-in)

Only what holds for other tasks on this site too: not this run's items, and nothing about the person (no names, \
addresses, orders, accounts). Write only the bullets.

Notes so far:
{notes}

The run (goal: {goal}; it ended {ended}):
{trace}"""


def site(url: str) -> str:
    """The site an address is on, as notes are kept: wolt.com for https://wolt.com/en/..., ksp.co.il for
    https://ksp.co.il/web/."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in UNDER_COUNTRY:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


class Notes:
    def __init__(self, folder: Path = FOLDER):
        self.folder = folder

    def _file(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z0-9.-]+", name):
            raise ValueError(f"not a site: {name!r}")
        return self.folder / f"{name}.md"

    def read(self, name: str) -> str:
        path = self._file(name)
        return path.read_text().strip() if path.exists() else ""

    def write(self, name: str, notes: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._file(name).write_text(notes.strip()[:MOST] + "\n")

    def known(self) -> list[str]:
        return sorted(p.stem for p in self.folder.glob("*.md")) if self.folder.exists() else []

    def named_in(self, goal: str) -> list[str]:
        """The sites with notes that the goal names ("on Wolt", "ksp.co.il"): they're read before the first step."""
        words = set(re.findall(r"[a-z0-9]+", goal.lower()))
        return [name for name in self.known() if name.split(".")[0] in words or name in goal.lower()]

    def learn(self, client, model: str, goal: str, ended: str, turns: list[dict[str, Any]]) -> tuple[str, int, int]:
        """Rewrites the notes of the site the run spent most of its turns on, from the run's turns: the new notes
        ("" for a run too short to learn from) and the tokens it took, in and out."""
        where: dict[str, int] = {}
        for turn in turns:
            if name := site(turn.get("url", "")):
                where[name] = where.get(name, 0) + 1
        if not where or len(turns) < 3:
            return "", 0, 0
        name = max(where, key=where.__getitem__)
        prompt = LEARN.format(site=name, notes=self.read(name) or "(none)", goal=goal, ended=ended, trace=_trace(turns))
        reply = client.responses.create(
            model=model,
            input=prompt,
            store=False,
            reasoning={"effort": "low"},
        )
        notes = (reply.output_text or "").strip()
        if notes:
            self.write(name, notes)
        usage = reply.usage
        return notes, (usage.input_tokens if usage else 0), (usage.output_tokens if usage else 0)


def _trace(turns: list[dict[str, Any]]) -> str:
    """The run as the learner reads it: a line a step, with what it did, newest last."""
    lines = []
    for t in turns:
        args = json.dumps(t.get("args", {}), ensure_ascii=False)[:300]
        said = " | ".join(str(t.get("said", "")).splitlines())[:300]
        lines.append(f"{t.get('turn')}. at {t.get('url', '')[:120]}: {t.get('call')} {args} -> {said}")
    text = "\n".join(lines)
    return text if len(text) <= TRACE else "…\n" + text[-TRACE:]
