"""Group the requests TARS has heard by voice, and enroll the voices people have named. No recording sessions.

Each request's voice embedding (from speaker ID) joins the closest existing voice if it's similar enough, or
starts a new one. Anything a person decided stays put: requests moved or merged by hand, and voices someone
named or marked "not a person". A voice someone has named (with enough requests) becomes a voiceprint, so TARS
recognizes that person from then on.
"""

import time

import numpy as np

from .audio import read_wav
from .events import NOT_PERSON, PERSON, UNKNOWN, EventLog, person_key
from .speaker import SpeakerID

# Same-person sentences score ~0.8-0.9 with this speaker model and different people <= ~0.2 (measured on the
# user's recordings); requests are often shorter and noisier, so the bar sits well below the same-person range.
SAME_VOICE = 0.5
MIN_REQUESTS_TO_ENROLL = 5


def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-9)


def recluster(log: EventLog, same_voice: float = SAME_VOICE) -> dict:
    started = time.time()
    rows = sorted(log.embeddings())  # oldest first, so voices are founded by their earliest requests
    decided = {c["id"] for c in log.clusters() if c["name"] or c["kind"] == NOT_PERSON}

    def settled(cluster, pinned):  # a person put it there, or it's in a voice a person named or classified
        return cluster is not None and (pinned or cluster in decided)

    members: dict[int, list[np.ndarray]] = {}
    for _, emb, cluster, pinned in rows:
        if settled(cluster, pinned):
            members.setdefault(cluster, []).append(_unit(emb))
    moved = created = 0
    for event_id, emb, cluster, pinned in rows:
        if settled(cluster, pinned):
            continue
        e = _unit(emb)
        centroids = {cid: _unit(np.mean(vs, axis=0)) for cid, vs in members.items()}
        best = max(centroids, key=lambda cid: float(centroids[cid] @ e), default=None)
        if best is None or float(centroids[best] @ e) < same_voice:
            if cluster is not None and cluster not in members:
                best = cluster  # keep its old voice number, so "Voice 3" stays Voice 3 across runs
            else:
                best = log.new_cluster()
                created += 1
        members.setdefault(best, []).append(e)
        if best != cluster:
            log.assign(event_id, best, pinned=False)
            moved += 1
    # Undecided voices that ended up empty are leftovers from earlier runs. One made since this run started is
    # someone's "New voice" about to get its first request, so it stays.
    for c in log.clusters():
        if c["size"] == 0 and not c["name"] and c["kind"] == UNKNOWN and c["created"] < started:
            log.delete_cluster(c["id"])
    return {
        "summary": f"{len(rows)} requests grouped into {len(log.clusters())} voices "
        f"({created} new, {moved} moved; your manual choices kept)."
    }


def enroll_named(log: EventLog, speaker_id: SpeakerID) -> list[str]:
    """Rebuild the voiceprints of every named person from their voices' requests (voices sharing a name count as
    one person). Returns who has a voiceprint now."""
    people: dict[str, tuple[str, list]] = {}
    for c in log.clusters():
        if c["kind"] == PERSON and (key := person_key(c["name"])):
            people.setdefault(key, (c["name"].strip(), []))[1].extend(log.request_audio(c["id"]))
    enough = {key: (name, clips) for key, (name, clips) in people.items() if len(clips) >= MIN_REQUESTS_TO_ENROLL}
    speaker_id.set_cluster_voiceprints(
        {key: speaker_id.voiceprint([read_wav(p) for p in clips]) for key, (_, clips) in enough.items()}
    )
    return sorted(name for name, _ in enough.values())


def regroup(log: EventLog, speaker_id: SpeakerID | None) -> dict:
    result = recluster(log)
    if speaker_id:
        enrolled = enroll_named(log, speaker_id)
        if enrolled:
            result["summary"] += f" Voiceprints updated for: {', '.join(enrolled)}."
    return result
