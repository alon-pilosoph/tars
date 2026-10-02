"""Groups heard requests by voice and enrolls the voices people have named.

Each request's speaker embedding joins the closest voice if similar enough, or starts a new one. Decisions made by
hand stay put: moved or merged requests, and voices named or marked "not a person". A named voice with enough
requests becomes a voiceprint.
"""

import time

import numpy as np

from .audio import read_wav
from .events import PERSON, UNKNOWN, EventLog, person_key, voice_decided
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
    clusters = {c["id"]: c for c in log.clusters()}

    def settled(cluster: int | None, pinned: bool) -> bool:
        return voice_decided(clusters.get(cluster), pinned)

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
    # Empty undecided voices are leftovers, except one created during this run: that's a "New voice" someone just
    # made in the UI, about to get its first request.
    for c in log.clusters():
        if c["size"] == 0 and not c["name"] and c["kind"] == UNKNOWN and c["created"] < started:
            log.delete_cluster(c["id"])
    return {
        "summary": f"Grouped {len(rows)} requests into {len(log.clusters())} voices, {created} of them new. "
        f"{moved} requests moved. Anything you set by hand stayed put."
    }


def enroll_named(log: EventLog, speaker_id: SpeakerID) -> list[str]:
    """Rebuilds every named person's voiceprint; voices sharing a name count as one person. Returns who is enrolled."""
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
            result["summary"] += f" Updated the voiceprints of {', '.join(enrolled)}."
    return result
