"""Groups heard requests by voice and enrolls the voices people have named.

Each request's speaker embedding joins the closest voice if similar enough, or starts a new one. Decisions made by
hand stay put: moved or merged requests, and voices named or marked "not a person". A named voice with enough
requests becomes a voiceprint.
"""

import time
from pathlib import Path

import numpy as np

from .audio import read_wav
from .events import PERSON, UNKNOWN, EventLog, person_key
from .rooms import MAIN
from .speaker import ANY_MIC, SpeakerID

# Same-person sentences score ~0.8-0.9 with this speaker model and different people <= ~0.2 (measured on the
# user's recordings); requests are often shorter and noisier, so the bar sits well below the same-person range. A
# request is compared with each voice as heard on its own mic (a voice through another mic scores lower: one
# grouping across mics mixed the TV, two parents and a child), and joins one only when it's at least MARGIN closer
# than the next: what's about as close to two voices starts its own. A stricter bar only shattered the speakerphone's
# old requests into single ones (tried on the household's, 2026-11-19).
SAME_VOICE = 0.5
MARGIN = 0.05
MIN_REQUESTS_TO_ENROLL = 5


def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-9)


def _centroid(mics: dict[str, list[np.ndarray]], mic: str) -> np.ndarray:
    """A voice as heard on `mic`: its requests there, or, with none there, all of them (another mic's beats none)."""
    return _unit(np.mean(mics.get(mic) or [v for vs in mics.values() for v in vs], axis=0))


def recluster(log: EventLog, same_voice: float = SAME_VOICE, margin: float = MARGIN) -> dict:
    """Puts each request that isn't in a voice yet into the one it sounds like, or a new one. A request already in a
    voice stays there: what the page shows only changes when someone changes it (2026-11-19: regrouping reshuffled
    the voices someone was going through)."""
    started = time.time()
    rows = sorted(log.embeddings())  # oldest first, so voices are founded by their earliest requests
    clusters = {c["id"] for c in log.clusters()}
    members: dict[int, dict[str, list[np.ndarray]]] = {}  # voice: its requests, by mic
    for _, emb, cluster, _, room in rows:
        if cluster in clusters:
            members.setdefault(cluster, {}).setdefault(room or MAIN, []).append(_unit(emb))
    placed = created = 0
    for event_id, emb, cluster, _, room in rows:
        if cluster in clusters:
            continue
        e, mic = _unit(emb), room or MAIN
        scores = sorted(((float(_centroid(vs, mic) @ e), cid) for cid, vs in members.items()), reverse=True)
        best = scores[0][1] if scores and scores[0][0] >= same_voice else None
        if best is not None and len(scores) > 1 and scores[0][0] - scores[1][0] < margin:
            best = None  # two voices about as close: neither, rather than a guess
        if best is None:
            best = log.new_cluster()
            created += 1
        members.setdefault(best, {}).setdefault(mic, []).append(e)
        log.assign(event_id, best, pinned=False)
        placed += 1
    # Empty unnamed voices are leftovers (everything in them was moved), except one created during this run: that's
    # a "New voice" someone just made in the UI, about to get its first request.
    for c in log.clusters():
        if c["size"] == 0 and not c["name"] and c["kind"] == UNKNOWN and c["created"] < started:
            log.delete_cluster(c["id"])
    if not placed:
        return {"summary": "Nothing new to sort: every request is in a voice. Nothing already in one was moved."}
    return {
        "summary": f"Sorted {placed} new requests into voices, {created} of them new. Nothing already in a voice "
        "was moved."
    }


def enroll_named(log: EventLog, speaker_id: SpeakerID) -> list[str]:
    """Rebuilds every named person's voiceprints: one for each mic (room) they have MIN_REQUESTS_TO_ENROLL requests
    on, and one of all of them together (ANY_MIC), for a mic they have too few on. Voices sharing a name count as
    one person. Returns who is enrolled."""
    people: dict[str, tuple[str, dict[str, list]]] = {}
    for c in log.clusters():
        if c["kind"] == PERSON and (key := person_key(c["name"])):
            mics = people.setdefault(key, (c["name"].strip(), {}))[1]
            for path, room in log.request_audio_by_room(c["id"]):
                mics.setdefault(room or MAIN, []).extend(_readable([path]))
    prints, enrolled = {}, []
    for key, (name, mics) in people.items():
        every = [clip for clips in mics.values() for clip in clips]
        if len(every) < MIN_REQUESTS_TO_ENROLL:
            continue
        enrolled.append(name)
        prints[(key, ANY_MIC)] = speaker_id.voiceprint(every)
        for mic, clips in mics.items():
            if len(clips) >= MIN_REQUESTS_TO_ENROLL:
                prints[(key, mic)] = speaker_id.voiceprint(clips)
    speaker_id.set_cluster_voiceprints(prints)
    return sorted(enrolled)


def _readable(paths: list[Path]) -> list[np.ndarray]:
    """The recordings, but one removed since it was listed (the backup making room on the machine) is left out."""
    out = []
    for path in paths:
        try:
            out.append(read_wav(path))
        except FileNotFoundError:
            continue
    return out


def regroup(log: EventLog, speaker_id: SpeakerID | None) -> dict:
    result = recluster(log)
    if speaker_id:
        enrolled = enroll_named(log, speaker_id)
        if enrolled:
            result["summary"] += f" Updated the voiceprints of {', '.join(enrolled)}."
    return result
