"""Spotify: found with its own search, played on the device in the house it's told to, woken first if it isn't on
Spotify yet, and said to play only once Spotify says so. The sign-in renews itself and keeps what Spotify swaps in."""

import itertools
import json
import stat

import httpx
import pytest

from voice_assistant import spotify
from voice_assistant.home import HomeError, Request
from voice_assistant.spotify import Account, Spotify

TV = {"id": "tv1", "name": "Fire TV Stick 4K Max", "is_active": False}
PHONE = {"id": "ph1", "name": "Alon's phone", "is_active": True}


def item(kind, name, key, artist=None):
    found = {"type": kind, "name": name, "uri": f"spotify:{kind}:{key}"}
    if artist:
        found["artists"] = [{"name": artist}]
    return found


SEARCH = {
    "the beatles": {"artists": {"items": [item("artist", "The Beatles", "b1")]}, "tracks": {"items": []}},
    "some jazz": {"playlists": {"items": [None, item("playlist", "Jazz Classics", "j1")]}, "tracks": {"items": []}},
    "bohemian rhapsody": {
        "playlists": {"items": [item("playlist", "Queen Hits", "q1")]},
        "tracks": {"items": [item("track", "Bohemian Rhapsody", "t1", "Queen")]},
    },
    "nothing": {},
}


class Service:
    """Spotify's API as far as TARS sees it: devices, a search, and a player that plays what it's told."""

    def __init__(self, devices=(TV, PHONE), plays=True, status=200):
        self.devices, self.plays, self.status = list(devices), plays, status
        self.played, self.calls, self.player = [], [], None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, query = request.url.path.removeprefix("/v1"), dict(request.url.params)
        self.calls.append((request.method, path))
        if self.status != 200:
            return httpx.Response(self.status)
        if path == "/me/player/devices":
            return httpx.Response(200, json={"devices": self.devices})
        if path == "/search":
            return httpx.Response(200, json=SEARCH.get(query["q"].lower(), {}))
        if path == "/me/player/play" and request.method == "PUT":
            body = json.loads(request.content or b"{}")
            self.played.append((query.get("device_id"), body))
            if self.plays:
                self.player = {"is_playing": True, "device": {"id": query.get("device_id"), "name": "x"}}
            return httpx.Response(204)
        if path == "/me/player":
            return httpx.Response(200, json=self.player) if self.player else httpx.Response(204)
        return httpx.Response(204)


class Signed:
    def token(self, renew=False):
        return "t"


def device(service, play_on="Fire TV", wake=None):
    http = httpx.Client(transport=httpx.MockTransport(service.handle))
    ticks = itertools.count(step=1.0)
    return Spotify("spotify", Signed(), play_on, http, wake, sleep=lambda s: None, clock=lambda: next(ticks))


def play(found, title):
    return found.call("play", {"title": title}, Request())


def test_an_artist_by_name_plays_on_the_device_named_not_the_one_playing():
    service = Service()
    assert (
        play(device(service), "the Beatles")
        == "done: playing The Beatles (artist) on Fire TV Stick 4K Max (Spotify says so)"
    )
    assert service.played == [("tv1", {"context_uri": "spotify:artist:b1"})]


def test_a_song_by_its_name_is_the_song_not_a_playlist_named_after_it():
    service = Service()
    assert "Bohemian Rhapsody by Queen" in play(device(service), "Bohemian Rhapsody")
    assert service.played[0][1] == {"uris": ["spotify:track:t1"]}


def test_a_kind_of_music_plays_a_playlist_of_it():
    service = Service()
    assert "Jazz Classics (playlist)" in play(device(service), "some jazz")


def test_a_spotify_link_plays_as_it_is():
    service = Service()
    play(device(service), "https://open.spotify.com/intl-he/album/abc123?si=x")
    assert service.played[0][1] == {"context_uri": "spotify:album:abc123"}
    assert ("GET", "/search") not in service.calls


def test_nothing_found_says_so_and_plays_nothing():
    service = Service()
    assert play(device(service), "nothing") == "error: Spotify found nothing for 'nothing'"
    assert service.played == []


def test_it_isnt_said_to_play_until_spotify_says_it_does():
    assert play(device(Service(plays=False)), "the Beatles").startswith("not confirmed")


def test_a_device_not_on_spotify_yet_is_woken_and_waited_for():
    service = Service(devices=[PHONE])
    woken = []

    def wake():
        woken.append(1)
        service.devices.append(TV)
        return "done: spotify is open"

    assert play(device(service, wake=wake), "the Beatles").startswith("done")
    assert woken == [1] and service.played[0][0] == "tv1"


def test_without_a_way_to_wake_it_says_to_open_its_app():
    with pytest.raises(HomeError, match="is its app open"):
        play(device(Service(devices=[PHONE])), "the Beatles")


def test_a_wake_that_fails_is_said():
    with pytest.raises(HomeError, match="couldn't open Spotify on it: error: no tv"):
        play(device(Service(devices=[PHONE]), wake=lambda: "error: no tv"), "the Beatles")


def test_buttons_and_volume_go_to_spotify():
    service = Service()
    found = device(service)
    assert found.call("press", {"button": "next"}, Request()) == "sent: next"
    assert found.call("set", {"level": 30}, Request()) == "sent: volume 30%"
    assert ("POST", "/me/player/next") in service.calls and ("PUT", "/me/player/volume") in service.calls
    assert found.call("press", {"button": "eject"}, Request()).startswith("error")
    assert found.call("set", {"level": 300}, Request()).startswith("error")


def test_status_says_whats_playing_and_where():
    service = Service()
    service.player = {"is_playing": True, "item": item("track", "Yesterday", "y", "The Beatles"), "device": TV}
    assert device(service).status() == "spotify: playing Yesterday by The Beatles, on Fire TV Stick 4K Max"
    service.player = None
    assert device(service).status() == "spotify: nothing playing"


def test_without_premium_it_says_so():
    with pytest.raises(HomeError, match="Premium"):
        play(device(Service(status=403)), "the Beatles")


def test_the_sign_in_renews_and_keeps_a_swapped_refresh_token_private(tmp_path):
    path = tmp_path / "spotify.json"
    spotify.keep(path, "old")
    seen = []

    def accounts(request):
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "a1", "expires_in": 3600, "refresh_token": "new"})

    account = Account("client", path, httpx.Client(transport=httpx.MockTransport(accounts)), clock=lambda: 0.0)
    assert account.token() == "a1" and account.token() == "a1"  # kept until it runs out
    assert len(seen) == 1 and seen[0]["refresh_token"] == "old" and seen[0]["client_id"] == "client"
    assert json.loads(path.read_text()) == {"refresh_token": "new"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_not_signed_in_says_how_to(tmp_path):
    account = Account("client", tmp_path / "none.json", httpx.Client())
    with pytest.raises(HomeError, match="spotify login"):
        account.token()


def test_login_takes_the_pasted_address_and_keeps_the_refresh_token(tmp_path, capsys):
    path, sent = tmp_path / "spotify.json", []

    def accounts(request):
        sent.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r1"})

    def ask(prompt):
        link = capsys.readouterr().out.split("\n\n")[1].strip()
        state = dict(httpx.URL(link).params)["state"]
        return f"http://127.0.0.1:8888/callback?code=c0de&state={state}"

    spotify.login("client", path, ask, httpx.Client(transport=httpx.MockTransport(accounts)))
    assert sent[0]["code"] == "c0de" and sent[0]["code_verifier"]
    assert json.loads(path.read_text()) == {"refresh_token": "r1"}


def test_login_refuses_an_address_from_some_other_sign_in(tmp_path):
    with pytest.raises(SystemExit, match="isn't the address"):
        spotify.login("client", tmp_path / "s.json", lambda p: "http://127.0.0.1:8888/callback?code=x&state=forged")


def problems(**device):
    from voice_assistant.config import HomeConfig, _home_problems

    return _home_problems(HomeConfig(devices=[{"name": "spotify", "kind": "spotify", **device}]))


def test_config_needs_where_to_play_and_knows_its_settings():
    assert problems(play_on="Fire TV", wake="fire tv") == []
    assert any("needs play_on" in p for p in problems())
    assert any("wake is the name" in p for p in problems(play_on="TV", wake=""))
    assert any("has no setting volume" in p for p in problems(play_on="TV", volume=30))


def test_build_needs_its_client_id(tmp_path):
    from voice_assistant.config import HomeConfig
    from voice_assistant.home import Context, build

    cfg = HomeConfig(devices=[{"name": "spotify", "kind": "spotify", "play_on": "TV"}])
    built = build(cfg, Context(tmp_path, lambda name: None))
    assert "SPOTIFY_CLIENT_ID" in built.unavailable["spotify"]
    built = build(cfg, Context(tmp_path, lambda name: "abc"))
    assert built.unavailable == {} and isinstance(built.tools.devices["spotify"], Spotify)
