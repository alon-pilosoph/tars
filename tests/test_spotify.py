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


SONGS = {
    "bohemian rhapsody": [
        item("track", "Bohemian Rhapsody - Live Aid", "q3", "Queen"),
        item("track", "Bohemian Rhapsody", "q1", "Queen"),
        item("track", "Bohemian Rhapsody - Remastered 2011", "q2", "Queen"),
        item("track", "Bohemian Rhapsody", "p1", "Panic! At The Disco"),
    ],
    "hello": [
        item("track", "Hello", "a1", "Adele"),
        item("track", "Hello", "l1", "Lionel Richie"),
    ],
    "yesterday": [
        item("track", "Yesterday - Remastered 2009", "y1", "The Beatles"),
        item("track", "Yesterday", "y2", "The Beatles"),
    ],
}
MINE = [{"type": "playlist", "name": "Running 2026", "uri": "spotify:playlist:run"}, None]
MOODS = [
    {"type": "playlist", "name": "Calm by Spotify", "uri": "spotify:playlist:sp", "owner": {"id": "spotify"}},
    {
        "type": "playlist",
        "name": "calm 5",
        "uri": "spotify:playlist:short",
        "owner": {"id": "a"},
        "tracks": {"total": 5},
    },
    {
        "type": "playlist",
        "name": "Calm Evening",
        "uri": "spotify:playlist:calm",
        "owner": {"id": "b"},
        "tracks": {"total": 80},
    },
]


class Service:
    """Spotify's API as far as TARS sees it: devices, a search, and a player that plays what it's told."""

    def __init__(self, devices=(TV, PHONE), plays=True, status=200):
        self.devices, self.plays, self.status = list(devices), plays, status
        self.played, self.calls, self.player = [], [], None
        self.favourites: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, query = request.url.path.removeprefix("/v1"), dict(request.url.params)
        self.calls.append((request.method, path))
        if self.status != 200:
            return httpx.Response(self.status)
        if path == "/me/player/devices":
            return httpx.Response(200, json={"devices": self.devices})
        if path == "/search":
            q, kind = query["q"].lower(), query["type"]
            if kind == "track":
                return httpx.Response(200, json={"tracks": {"items": SONGS.get(q, [])}})
            if kind == "playlist":
                return httpx.Response(200, json={"playlists": {"items": MOODS if "calm" in q else []}})
            return httpx.Response(200, json=SEARCH.get(q, {}))
        if path == "/me/playlists":
            return httpx.Response(200, json={"items": MINE})
        if path == "/me/top/artists":
            return httpx.Response(200, json={"items": [{"name": n} for n in self.favourites]})
        if path.startswith("/me/top/tracks") or path == "/me/tracks":
            liked = [{"track": item("track", "Liked", "l1")}] if path == "/me/tracks" else [item("track", "Top", "t9")]
            return httpx.Response(200, json={"items": liked})
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


def play(found, title, mode=""):
    return found.call("play", {"title": title, "mode": mode}, Request())


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


def test_a_song_with_one_clear_version_just_plays():
    service = Service()
    assert "Yesterday" in play(device(service), "Yesterday", "song")  # a remaster is the same recording
    assert service.played[0][1] == {"uris": ["spotify:track:y1"]}


def test_one_artists_song_plays_its_studio_version_not_the_live_one_ranked_first():
    service = Service()
    assert "Bohemian Rhapsody by Queen" in play(device(service), "Bohemian Rhapsody", "song")
    assert service.played[0][1] == {"uris": ["spotify:track:q1"]}


def test_a_live_one_plays_when_its_asked_for():
    service = Service()
    SONGS["bohemian rhapsody live"] = SONGS["bohemian rhapsody"]
    assert "Live Aid" in play(device(service), "Bohemian Rhapsody live", "song")
    assert service.played[0][1] == {"uris": ["spotify:track:q3"]}


def test_a_cover_ranked_second_doesnt_make_it_ask():
    service = Service()
    SONGS["shape of you"] = [
        item("track", "Shape of You", "e1", "Ed Sheeran"),
        item("track", "Shape of You", "c1", "Bossa Bros"),
        item("track", "Shape of You - Acoustic", "e2", "Ed Sheeran"),
    ]
    assert "Ed Sheeran" in play(device(service), "Shape of You", "song")
    assert service.played[0][1] == {"uris": ["spotify:track:e1"]}


def test_a_tie_goes_to_the_artist_they_play_themselves():
    service = Service()
    service.favourites = ["Adele"]
    assert "Hello by Adele" in play(device(service), "Hello", "song")
    assert service.played[0][1] == {"uris": ["spotify:track:a1"]}


def test_different_artists_songs_of_one_name_play_spotifys_first_and_say_the_others():
    service = Service()
    found = device(service)
    said = play(found, "Hello", "song")
    assert said.startswith("done: playing Hello by Adele") and "Also found: 2. Hello by Lionel Richie" in said
    assert service.played[0][1] == {"uris": ["spotify:track:a1"]}
    assert "Lionel Richie" in play(found, "the Lionel Richie one", "choice")
    assert service.played[1][1] == {"uris": ["spotify:track:l1"]}


@pytest.mark.parametrize("answer, uri", [("2", "l1"), ("the second", "l1"), ("the one by lionel", "l1")])
def test_an_answer_can_be_its_number_or_its_words(answer, uri):
    service = Service()
    found = device(service)
    play(found, "Hello", "song")
    play(found, answer, "choice")
    assert service.played[-1][1] == {"uris": [f"spotify:track:{uri}"]}


def test_an_answer_that_fits_none_or_comes_too_late_plays_nothing_more():
    service = Service()
    found = device(service)
    play(found, "Hello", "song")
    assert play(found, "the jazz one", "choice").startswith("error")
    found._offered_at -= spotify.OFFER_S + 1
    assert play(found, "2", "choice").startswith("error") and len(service.played) == 1


def test_their_own_playlist_comes_first():
    service = Service()
    assert "your playlist Running 2026" in play(device(service), "my running playlist", "playlist")


def test_a_mood_plays_a_long_enough_playlist_not_spotifys_own():
    service = Service()
    assert "Calm Evening" in play(device(service), "something calm", "mood")


def test_something_i_like_is_a_shuffled_mix_of_their_own():
    service = Service()
    assert "a mix of what you like" in play(device(service), "", "liked")
    assert set(service.played[0][1]["uris"]) == {"spotify:track:l1", "spotify:track:t9"}
    assert ("PUT", "/me/player/shuffle") in service.calls


def test_more_like_this_is_more_of_the_artist_playing():
    service = Service()
    service.player = {"is_playing": True, "item": item("track", "Yesterday", "y", "The Beatles"), "device": TV}
    service.player["item"]["artists"][0]["uri"] = "spotify:artist:b1"
    assert "more The Beatles" in play(device(service), "", "more")
    assert service.played[0][1] == {"context_uri": "spotify:artist:b1"}


def test_an_unknown_mode_is_refused():
    assert play(device(Service()), "x", "vibes").startswith("error: mode must be")


def test_an_answer_that_isnt_json_is_nothing_to_read():
    service = Service()
    found = device(service)
    original = service.handle
    service.handle = lambda request: (
        httpx.Response(200, text="ok") if request.url.path.endswith("/pause") else original(request)
    )
    found._http = httpx.Client(transport=httpx.MockTransport(service.handle))
    assert found.call("press", {"button": "pause"}, Request()) == "sent: pause"


def test_a_sign_in_from_a_chat_sends_the_link_then_the_page_and_keeps_it(tmp_path, monkeypatch):
    from voice_assistant.browser import login_page

    path, sent, asked = tmp_path / "spotify.json", [], []

    def page(asking, send_link, host, port, wait_s=600):
        asked.append((asking.fields[0].label, host, port))
        send_link("http://10.0.0.5:8091/login?token=t")
        link = sent[0].split("\n")[1]
        state = dict(httpx.URL(link).params)["state"]
        return {"address": f"http://127.0.0.1:8888/callback?code=c&state={state}"}

    monkeypatch.setattr(login_page, "ask", page)
    accounts = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"refresh_token": "r9"})))
    said = spotify.page_login("client", path, "10.0.0.5", 8091, sent.append, accounts)
    assert said == "Spotify is signed in." and json.loads(path.read_text()) == {"refresh_token": "r9"}
    assert sent[0].startswith("To sign TARS in to Spotify") and "accounts.spotify.com/authorize" in sent[0]
    assert "works once" in sent[1] and asked == [("The address Spotify sent you to", "10.0.0.5", 8091)]


def test_a_sign_in_from_a_chat_with_no_address_in_time_says_how_to_try_again(tmp_path, monkeypatch):
    from voice_assistant.browser import login_page

    monkeypatch.setattr(login_page, "ask", lambda *a, **kw: None)
    said = spotify.page_login("client", tmp_path / "s.json", "h", 1, lambda text: None)
    assert "Send /spotify" in said and not (tmp_path / "s.json").exists()


def test_a_wrong_address_on_the_page_is_said(tmp_path, monkeypatch):
    from voice_assistant.browser import login_page

    monkeypatch.setattr(
        login_page, "ask", lambda *a, **kw: {"address": "http://127.0.0.1:8888/callback?code=x&state=no"}
    )
    said = spotify.page_login("client", tmp_path / "s.json", "h", 1, lambda text: None)
    assert said.startswith("That isn't the address") and "Send /spotify" in said
