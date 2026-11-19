"""The household's people, as TARS knows them by voice: what the web UI's Voices page shows and asks.

People, not voices: a person is a voice someone named (events.PERSON), and the page never shows a voice nobody
named. What it asks is one request at a time: "Who said this?", for the recent requests TARS can't place, with its
guess (the person whose requests, on the same mic, it sounds most like); and, for each person, the few of their
requests that sound least like the rest ("Yours?"), so one put there by mistake is caught. Each answer is final: the
request is pinned where it was put, and asked about never again. Not sure, or the TV, are answers too.

Voiceprints are rebuilt from the people's requests after every answer that changes someone's (`rebuild`, on its own
thread): what's named is what's learned from, and nothing else (clustering.enroll_named).
"""

import threading
import time
from collections.abc import Callable

import numpy as np

from .clustering import MARGIN, MIN_REQUESTS_TO_ENROLL, SAME_VOICE
from .events import NOT_PERSON, PERSON, UNKNOWN, EventLog, person_key
from .rooms import MAIN

ASK_DAYS = 14  # how far back "Who said this?" goes: older requests aren't worth a person's time
MOST_ASKED = 10  # cards waiting at a time
CHECK_BELOW = 0.5  # a person's request that sounds less like the rest than this is asked about ("Yours?")
MOST_CHECKED = 3  # of each person's, at a time
MOST_SHOWN = 60  # of each person's requests, newest first
TV, UNSURE = "tv", "unsure"


def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-9)


class People:
    """`log`: the event log. `rebuild()`: the voiceprints made again from the people's requests (regroup's
    enroll_named), called on its own thread after an answer that changes someone's; None when speaker ID is off.
    `clock`: seconds."""

    def __init__(
        self, log: EventLog, rebuild: Callable[[], object] | None = None, clock: Callable[[], float] = time.time
    ):
        self.log, self._rebuild, self._clock = log, rebuild, clock
        self._rebuilding = threading.Lock()

    # What the page shows.

    def page(self) -> dict:
        rows = self._requests()
        people = [c for c in self.log.clusters() if c["kind"] == PERSON and c["name"]]
        ids = {c["id"] for c in people}
        theirs: dict[int, list[dict]] = {}
        for r in rows:
            if r["cluster_id"] in ids:
                theirs.setdefault(r["cluster_id"], []).append(r)
        centroids = {cid: _by_mic(rs) for cid, rs in theirs.items()}
        names = {c["id"]: c["name"].strip() for c in people}
        return {
            "people": [self._person(c, theirs.get(c["id"], [])) for c in sorted(people, key=lambda c: c["id"])],
            "ask": self._ask(rows, ids, centroids, names),
            "enroll_at": MIN_REQUESTS_TO_ENROLL,
        }

    def _person(self, c: dict, rows: list[dict]) -> dict:
        mics: dict[str, int] = {}
        for r in rows:
            mics[r["mic"]] = mics.get(r["mic"], 0) + 1
        newest = sorted(rows, key=lambda r: -r["ts"])
        return {
            "id": c["id"],
            "name": c["name"].strip(),
            "requests": len(rows),
            "mics": [{"room": m, "requests": n} for m, n in sorted(mics.items(), key=lambda x: -x[1])],
            "check": self._check(rows),
            "shown": [_public(r) for r in newest[:MOST_SHOWN]],
        }

    def _check(self, rows: list[dict]) -> list[dict]:
        """Their requests nobody confirmed that sound least like the rest of theirs on the same mic."""
        odd = []
        for r in rows:
            if r["pinned"]:
                continue
            rest = [o for o in rows if o is not r and o["mic"] == r["mic"]]
            if len(rest) < 2:
                continue
            score = float(_unit(np.mean([o["e"] for o in rest], axis=0)) @ r["e"])
            if score < CHECK_BELOW:
                odd.append((score, r))
        return [_public(r) for _, r in sorted(odd, key=lambda x: x[0])[:MOST_CHECKED]]

    def _ask(self, rows: list[dict], people: set[int], centroids: dict, names: dict[int, str]) -> list[dict]:
        """Recent requests in nobody's voice that nobody answered, newest first, each with TARS's guess."""
        decided = {c["id"] for c in self.log.clusters() if c["kind"] == NOT_PERSON} | people
        since = self._clock() - ASK_DAYS * 24 * 3600
        waiting = [r for r in rows if not r["pinned"] and r["cluster_id"] not in decided and r["ts"] >= since]
        cards = []
        for r in sorted(waiting, key=lambda r: -r["ts"])[:MOST_ASKED]:
            scores = sorted(((float(_near(cs, r["mic"]) @ r["e"]), cid) for cid, cs in centroids.items()), reverse=True)
            sure = scores and scores[0][0] >= SAME_VOICE and (len(scores) == 1 or scores[0][0] - scores[1][0] >= MARGIN)
            guess = {"id": scores[0][1], "name": names[scores[0][1]]} if sure else None
            cards.append({**_public(r), "guess": guess})
        return cards

    def _requests(self) -> list[dict]:
        """Every request with a voice embedding and its recording."""
        rows = self.log.store.rows(
            "SELECT id, ts, room, transcript, embedding, cluster_id, cluster_pinned FROM events "
            "WHERE embedding IS NOT NULL AND utterance_audio IS NOT NULL"
        )
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "mic": r["room"] or MAIN,
                "transcript": r["transcript"] or "",
                "e": _unit(np.frombuffer(r["embedding"], dtype=np.float32)),
                "cluster_id": r["cluster_id"],
                "pinned": bool(r["cluster_pinned"]),
            }
            for r in rows
        ]

    # Answers, each final.

    def answer(self, event_id: int, to: int | str) -> None:
        """Whose request `event_id` is: a person (their id), TV (not a person), or UNSURE (nobody's, never asked again).
        ValueError for a person who isn't one."""
        if self.log.get(event_id) is None:
            raise ValueError("no such request")
        if to == TV:
            self.log.assign(event_id, self._tv(), pinned=True)
        elif to == UNSURE:
            self.log.assign(event_id, None, pinned=True)
        else:
            if not any(c["id"] == to and c["kind"] == PERSON for c in self.log.clusters()):
                raise ValueError("no such person")
            self.log.assign(event_id, to, pinned=True)
        self.relearn()

    def add(self, name: str) -> int:
        """Someone new, by name; the one there already, if the name is taken."""
        key = person_key(name)
        if not key:
            raise ValueError("a person needs a name")
        for c in self.log.clusters():
            if c["kind"] == PERSON and person_key(c["name"]) == key:
                return c["id"]
        return self.log.new_cluster(name.strip(), PERSON)

    def clean_start(self) -> int:
        """Every request that isn't a person's or the TV's set aside for good (nobody's, never asked about), and the
        voices nobody named gone. How many were set aside."""
        kept = {c["id"] for c in self.log.clusters() if c["kind"] in (PERSON, NOT_PERSON)}
        rows = self.log.store.rows("SELECT id, cluster_id FROM events WHERE embedding IS NOT NULL")
        aside = [r["id"] for r in rows if r["cluster_id"] not in kept]
        for event_id in aside:
            self.log.assign(event_id, None, pinned=True)
        for c in self.log.clusters():
            if c["kind"] == UNKNOWN and c["size"] == 0:
                self.log.delete_cluster(c["id"])
        return len(aside)

    def _tv(self) -> int:
        """The voice of whatever isn't a person, made the first time it's needed."""
        found = [c for c in self.log.clusters() if c["kind"] == NOT_PERSON]
        return max(found, key=lambda c: c["size"])["id"] if found else self.log.new_cluster(None, NOT_PERSON)

    def relearn(self) -> None:
        """The voiceprints made again, on a thread of their own; one at a time, and one more if asked meanwhile."""
        if self._rebuild is None:
            return

        def run() -> None:
            with self._rebuilding:
                try:
                    self._rebuild()
                except Exception as e:  # noqa: BLE001 - tried again on the next answer
                    print(f"(couldn't relearn the voices: {e!r})")

        threading.Thread(target=run, daemon=True, name="relearn-voices").start()


def _by_mic(rows: list[dict]) -> dict[str, np.ndarray]:
    mics: dict[str, list[np.ndarray]] = {}
    for r in rows:
        mics.setdefault(r["mic"], []).append(r["e"])
    return {m: _unit(np.mean(es, axis=0)) for m, es in mics.items()}


def _near(centroids: dict[str, np.ndarray], mic: str) -> np.ndarray:
    """A person as heard on `mic`: their requests there, or, with none there, all of theirs together."""
    if mic in centroids:
        return centroids[mic]
    return _unit(np.mean(list(centroids.values()), axis=0))


def _public(r: dict) -> dict:
    return {"event_id": r["id"], "ts": r["ts"], "room": r["mic"], "transcript": r["transcript"]}
