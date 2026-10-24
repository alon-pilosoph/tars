"""A run's trace: one JSON line an event, as it happens (agent.py's turns, mcp_server.py's, claude.py's), for
reading back what a run did, learning a site from it (notes.py), and the evals. Every event has its time ("t") and
its kind ("event"): start, turn, approval, notes, follow_up, error, end."""

import json
import time
from pathlib import Path
from typing import Any


class Trace:
    """Writes events to `path`, or nowhere without one. Called with an event, as a log is."""

    def __init__(self, path: Path | None):
        self.path = path

    def __call__(self, entry: dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as out:
            out.write(json.dumps({"t": round(time.time(), 2), **entry}, ensure_ascii=False, default=str) + "\n")


def read(path: Path) -> list[dict[str, Any]]:
    """A trace's events, in order; a line that isn't one (cut short as the run was stopped) is left out."""
    events = []
    for line in path.read_text(errors="replace").split("\n"):  # not splitlines: a page's U+2028 isn't a line end
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events
