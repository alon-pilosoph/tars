"""The web UI's API and voice clustering."""

import re

import numpy as np
import pytest
from fastapi.testclient import TestClient

from voice_assistant import webui
from voice_assistant.clustering import MIN_REQUESTS_TO_ENROLL, enroll_named, recluster
from voice_assistant.events import NOT_PERSON, PERSON, REAL, UNKNOWN
from voice_assistant.verify import ANSWER
from voice_assistant.webui import allowed_host, create_app

from .conftest import AUDIO


def voice(direction, rng, n=1):
    """Embeddings of one 'person': a direction plus a little noise."""
    return [direction + rng.normal(0, 0.05, direction.shape) for _ in range(n)]


def add_requests(log, embeddings):
    ids = []
    for emb in embeddings:
        e = log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9)
        log.add_request(e, AUDIO, "hi", None, None, emb.astype(np.float32))
        ids.append(e)
    return ids


def test_api_lists_labels_retags_and_deletes(log, client):
    event = log.add_wake(AUDIO, 0.9, ANSWER, "hey tars", 0.9)
    (row,) = client.get("/api/events").json()
    assert row["id"] == event and row["has_wake_audio"] and not row["has_request_audio"]
    assert set(row) == {
        "id",
        "ts",
        "kind",
        "outcome",
        "wake_score",
        "heard",
        "confidence",
        "transcript",
        "follow",
        "cluster_id",
        "cluster_pinned",
        "label",
        "auto_label",
        "auto_reason",
        "has_wake_audio",
        "has_request_audio",
    }  # never where things are stored, or the voice embedding
    assert client.post(f"/api/events/{event}/label", json={"label": REAL}).status_code == 200
    assert log.get(event)["label"] == REAL
    cluster = client.post("/api/clusters", json={"name": "Stacey"}).json()["id"]
    client.post(f"/api/events/{event}/cluster", json={"cluster_id": cluster})
    assert log.get(event)["cluster_id"] == cluster and log.get(event)["cluster_pinned"] == 1
    assert client.get(f"/api/audio/{event}/wake").status_code == 200
    assert client.get(f"/api/audio/{event}/request").status_code == 404
    assert client.delete(f"/api/events/{event}").status_code == 200 and log.get(event) is None


def test_api_rejects_bad_labels_and_same_voice_merges(log, client):
    event = log.add_wake(AUDIO, 0.9, ANSWER, "", None)
    r = client.post(f"/api/events/{event}/label", json={"label": "maybe"})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "label"]
    c = log.new_cluster()
    assert client.post("/api/clusters/merge", json={"keep": c, "absorb": c}).status_code == 400
    assert client.post("/api/recluster").status_code == 501  # clustering not wired in this app
    assert client.get("/api/events?limit=-1").status_code == 422


def test_voices_that_dont_exist_are_refused(log, client):
    event = log.add_wake(AUDIO, 0.9, ANSWER, "", None)
    real = log.new_cluster("Alon", PERSON)
    assert client.post(f"/api/events/{event}/cluster", json={"cluster_id": 999}).status_code == 404
    assert log.get(event)["cluster_id"] is None
    assert client.post("/api/clusters/merge", json={"keep": 999, "absorb": real}).status_code == 404
    assert client.post("/api/clusters/999", json={"name": "Stacey"}).status_code == 404
    assert [c["name"] for c in log.clusters()] == ["Alon"]
    assert client.post(f"/api/clusters/{real}", json={"name": "Alon", "kind": "banana"}).status_code == 422


def test_voice_names_are_tidied_and_blank_means_no_name(log, client):
    c = client.post("/api/clusters", json={"name": "  Stacey   Smith "}).json()["id"]
    assert log.cluster(c)["name"] == "Stacey Smith" and log.cluster(c)["kind"] == PERSON
    client.post(f"/api/clusters/{c}", json={"name": "   "})
    assert log.cluster(c)["name"] is None and log.cluster(c)["kind"] == UNKNOWN
    assert client.post(f"/api/clusters/{c}", json={"name": "x" * 61}).status_code == 422


def test_naming_a_voice_that_isnt_a_person_keeps_it_that_way(log, client):
    tv = log.new_cluster(None, NOT_PERSON)
    client.post(f"/api/clusters/{tv}", json={"name": "Living-room TV"})
    assert log.cluster(tv)["kind"] == NOT_PERSON
    client.post(f"/api/clusters/{tv}", json={"name": "Stacey", "kind": "person"})  # the page can still say otherwise
    assert log.cluster(tv)["kind"] == PERSON


def test_status_says_what_the_page_can_offer(client):
    assert client.get("/api/status").json() == {"clustering": False, "enroll_at": MIN_REQUESTS_TO_ENROLL}


def test_each_voice_comes_with_its_newest_requests(log, client):
    rng = np.random.default_rng(5)
    ids = add_requests(log, voice(np.eye(16)[0], rng, 6))
    recluster(log)
    (cluster,) = client.get("/api/clusters").json()
    assert cluster["size"] == 6 and [s["event_id"] for s in cluster["samples"]] == ids[::-1][:4]
    assert cluster["samples"][0]["transcript"] == "hi"


@pytest.mark.parametrize(
    "host, ok",
    [
        ("127.0.0.1:8080", True),
        ("192.168.1.20:8080", True),
        ("[::1]:8080", True),
        ("localhost:5173", True),
        ("tars.local:8080", True),
        ("tars:8080", True),
        ("tars.tail1234.ts.net", True),
        ("evil.example", False),
        ("tars.local.evil.example", False),
        ("", False),
    ],
)
def test_only_home_network_names_are_answered(host, ok):
    assert allowed_host(host) is ok


def test_requests_to_other_names_and_changes_from_other_sites_are_refused(log):
    event = log.add_wake(AUDIO, 0.9, ANSWER, "", None)
    home = TestClient(create_app(log), base_url="http://tars.local:8080")
    elsewhere = TestClient(create_app(log), base_url="http://evil.example").get("/api/events")
    assert elsewhere.status_code == 421 and "allowed_hosts" in elsewhere.json()["detail"]
    assert (
        TestClient(create_app(log, allowed_hosts=frozenset({"tars.example.com"})), base_url="http://tars.example.com")
        .get("/api/events")
        .status_code
        == 200
    )
    url = f"/api/events/{event}/label"
    refused = home.post(url, json={"label": REAL}, headers={"Origin": "https://evil.example"})
    assert refused.status_code == 403
    assert refused.json() == {"detail": "changes can only come from the TARS page itself"}
    assert home.post(url, json={"label": REAL}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert home.post(url, json={"label": REAL}, headers={"Origin": "http://tars.local:8080"}).status_code == 200
    assert home.post(url, json={"label": None}, headers={"Origin": "http://localhost:5173"}).status_code == 200  # dev
    assert home.get("/api/events", headers={"Origin": "https://evil.example"}).status_code == 200  # reads: CORS


def test_two_voices_become_two_clusters_and_stay_put_on_rerun(log):
    rng = np.random.default_rng(0)
    a, b = np.eye(16)[0], np.eye(16)[1]
    ids_a, ids_b = add_requests(log, voice(a, rng, 4)), add_requests(log, voice(b, rng, 3))
    recluster(log)
    groups = {log.get(i)["cluster_id"] for i in ids_a}, {log.get(i)["cluster_id"] for i in ids_b}
    assert len(groups[0]) == 1 and len(groups[1]) == 1 and groups[0] != groups[1]
    before = {i: log.get(i)["cluster_id"] for i in ids_a + ids_b}
    recluster(log)
    assert {i: log.get(i)["cluster_id"] for i in ids_a + ids_b} == before  # voice numbers are stable


def test_manual_moves_and_named_voices_survive_reclustering(log):
    rng = np.random.default_rng(1)
    a, b = np.eye(16)[0], np.eye(16)[1]
    ids = add_requests(log, voice(a, rng, 3) + voice(b, rng, 2))
    recluster(log)
    mine = log.get(ids[0])["cluster_id"]
    log.rename_cluster(mine, "Alon")
    odd_one = ids[3]  # a "b" voice, but a person says it's Alon's
    log.assign(odd_one, mine, pinned=True)
    recluster(log)
    assert log.get(odd_one)["cluster_id"] == mine
    assert all(log.get(i)["cluster_id"] == mine for i in ids[:3])
    assert [c["name"] for c in log.clusters() if c["id"] == mine] == ["Alon"]


def test_a_voice_marked_not_a_person_survives_reclustering(log):
    rng = np.random.default_rng(3)
    ids = add_requests(log, voice(np.eye(16)[0], rng, 3))
    recluster(log)
    tv = log.get(ids[0])["cluster_id"]
    log.rename_cluster(tv, None, NOT_PERSON)
    log.new_cluster(None, NOT_PERSON)  # an empty one someone made and classified
    recluster(log)
    assert all(log.get(i)["cluster_id"] == tv for i in ids)
    assert sum(c["kind"] == NOT_PERSON for c in log.clusters()) == 2


def test_reclustering_clears_old_empty_voices_but_not_one_just_made(log):
    old = log.new_cluster()
    log.store.write("UPDATE clusters SET created=0 WHERE id=?", (old,))
    fresh = log.new_cluster()  # "New voice", its request about to be moved in
    log.store.write("UPDATE clusters SET created=? WHERE id=?", (1e12, fresh))
    recluster(log)
    assert [c["id"] for c in log.clusters()] == [fresh]


class RecordingSpeakerID:
    def __init__(self):
        self.enrolled = {}

    def voiceprint(self, clips):
        return len(clips)

    def set_cluster_voiceprints(self, voiceprints):
        self.enrolled = voiceprints


def test_named_voices_with_enough_requests_are_enrolled(log):
    rng = np.random.default_rng(2)
    ids = add_requests(log, voice(np.eye(16)[0], rng, MIN_REQUESTS_TO_ENROLL) + voice(np.eye(16)[1], rng, 2))
    recluster(log)
    big, small = log.get(ids[0])["cluster_id"], log.get(ids[-1])["cluster_id"]
    log.rename_cluster(big, "Stacey")
    log.rename_cluster(small, "Guest")
    sid = RecordingSpeakerID()
    assert enroll_named(log, sid) == ["Stacey"] and sid.enrolled == {"stacey": MIN_REQUESTS_TO_ENROLL}


def test_voices_sharing_a_name_are_one_person(log):
    rng = np.random.default_rng(4)
    ids = add_requests(log, voice(np.eye(16)[0], rng, 3) + voice(np.eye(16)[1], rng, 3))
    recluster(log)
    log.rename_cluster(log.get(ids[0])["cluster_id"], "Alon")
    log.rename_cluster(log.get(ids[-1])["cluster_id"], " alon")  # far from the mic: a second voice, same person
    sid = RecordingSpeakerID()
    assert enroll_named(log, sid) == ["Alon"] and sid.enrolled == {"alon": 6}


def test_serves_the_built_page_and_its_assets(log, client):
    if not (webui.STATIC / "index.html").exists():
        pytest.skip("the web UI isn't built (npm run build in webui/)")
    page = client.get("/")
    assert page.status_code == 200 and '<div id="root">' in page.text
    (script,) = re.findall(r'src="(/assets/[^"]+\.js)"', page.text)
    assert client.get(script).status_code == 200


def test_the_api_works_without_the_built_page(log, monkeypatch, tmp_path):
    monkeypatch.setattr(webui, "STATIC", tmp_path / "not_built")
    client = TestClient(create_app(log))
    assert client.get("/").status_code == 503 and "npm" in client.get("/").text
    assert client.get("/api/events").status_code == 200
