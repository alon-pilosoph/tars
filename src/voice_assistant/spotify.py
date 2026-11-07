"""Spotify, through Spotify Connect: TARS finds what to play with Spotify's own search and plays it on a Spotify device
in the house (the TV's Spotify app, a speaker), checking each time with Spotify that it is. It needs Premium, and a
one-time sign-in: an app of your own at developer.spotify.com, its client id in .env as SPOTIFY_CLIENT_ID, then

    uv run python -m voice_assistant.spotify login

which prints a link to sign in with, and asks for the address Spotify sends you on to. The sign-in is kept in
voice_data/spotify.json, readable by TARS's user alone: Spotify swaps it for a new one as it's used.

A device that only shows up in Spotify while its app is open (a TV's) can be woken first: `wake` names the device in
the house that opens it, the Fire TV, whose "open" turns the TV on too."""

import base64
import hashlib
import json
import os
import random
import re
import secrets
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .home import PLAY, PRESS, SET, STATUS, Device, HomeError, Request, button

CLIENT_KEY = "SPOTIFY_CLIENT_ID"
TOKEN_FILE = "voice_data/spotify.json"
ACCOUNTS, API = "https://accounts.spotify.com", "https://api.spotify.com/v1"
REDIRECT = "http://127.0.0.1:8888/callback"  # Spotify's own rule: plain http only on this machine's own address
SCOPES = (  # playing, and reading what's liked and played, for "something I like"; nothing that changes the library
    "user-read-playback-state user-modify-playback-state user-read-currently-playing user-top-read "
    "user-read-recently-played user-library-read playlist-read-private"
)
TIMEOUT_S = 8.0
WAKE_WAIT_S = 30.0  # a TV's Spotify app opening and showing up as a device
PLAYING_WAIT_S = 6.0  # Spotify saying it's playing what it was asked to
POLL_S = 1.0
KINDS = ("playlist", "album", "artist", "track")  # with no mode, an exact name is taken in this order
SONG, ARTIST, ALBUM, PLAYLIST, GENRE, MOOD, LIKED, MORE, CHOICE = (
    "song",
    "artist",
    "album",
    "playlist",
    "genre",
    "mood",
    "liked",
    "more",
    "choice",
)
MODES = ("", SONG, ARTIST, ALBUM, PLAYLIST, GENRE, MOOD, LIKED, MORE, CHOICE)
FAVOURITES_S = 3600.0
MOST_OFFERED = 4  # options to choose from, said aloud
OFFER_S = 600.0  # how long an answer to "which?" still counts
MIN_TRACKS = 15  # a mood's playlist this short is passed over when there's a longer one
MOST_URIS = 100
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}  # not "one": "the live one" isn't the first
FILLER = {"the", "one", "a", "version", "that", "please", "by"}
SAME_RECORDING = re.compile(r"\s*[-(\[].*(remaster|mono|stereo|radio edit).*$", re.IGNORECASE)  # not live or a mix
ALTERED = re.compile(r"\b(live|remix|mix|acoustic|karaoke|instrumental|demo|unplugged)\b", re.IGNORECASE)
VERSION = re.compile(
    r"\s*[-(\[].*(remaster|live|version|edit|mix|mono|stereo|deluxe|anniversary|acoustic|karaoke|instrumental|demo|unplugged).*$",
    re.IGNORECASE,
)
BUTTONS = {
    "pause": ("PUT", "/me/player/pause"),
    "play": ("PUT", "/me/player/play"),
    "next": ("POST", "/me/player/next"),
    "previous": ("POST", "/me/player/previous"),
}
LINK = re.compile(
    r"open\.spotify\.com/(?:intl-\w+/)?(track|album|playlist|artist)/(\w+)|spotify:(track|album|playlist|artist):(\w+)"
)


class Account:
    """The sign-in: an access token, renewed from the refresh token in `path` when it runs out."""

    def __init__(self, client_id: str, path: Path, http: httpx.Client, clock: Callable[[], float] = time.time):
        self.client_id, self.path, self._http, self._clock = client_id, path, http, clock
        self._token, self._until = "", 0.0

    def token(self, renew: bool = False) -> str:
        if renew or not self._token or self._clock() >= self._until:
            self._renew()
        return self._token

    def _renew(self) -> None:
        try:
            refresh = json.loads(self.path.read_text())["refresh_token"]
        except OSError, ValueError, KeyError:
            raise HomeError("Spotify isn't signed in: uv run python -m voice_assistant.spotify login") from None
        answer = self._http.post(
            f"{ACCOUNTS}/api/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": self.client_id},
        )
        if answer.status_code != 200:
            raise HomeError("Spotify turned down the sign-in: sign in again (python -m voice_assistant.spotify login)")
        got = answer.json()
        self._token, self._until = got["access_token"], self._clock() + got.get("expires_in", 3600) - 60
        if got.get("refresh_token"):  # Spotify may swap it: the old one stops working
            keep(self.path, got["refresh_token"])


def keep(path: Path, refresh: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"refresh_token": refresh}, f)


class Spotify(Device):
    actions = (STATUS, PLAY, PRESS, SET)

    def __init__(
        self,
        name: str,
        account: Account,
        play_on: str,
        http: httpx.Client,
        wake: Callable[[], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        """`play_on`: the Spotify device to play on, by its name in Spotify, or part of it ("Fire TV"). `wake()`:
        opens it, if it shows up only while its app is open, and says how that went."""
        super().__init__(name)
        self.account, self.play_on, self._http, self._wake = account, play_on, http, wake
        self._sleep, self._clock = sleep, clock
        self._offered: list[dict] = []  # the options last asked about
        self._offered_at = 0.0
        self._favourite_names: set[str] = set()
        self._favourites_at = -FAVOURITES_S - 1

    def what(self) -> str:
        return (
            f"Spotify, playing on {self.play_on}: status (what's playing), play (title: what they asked for; mode: "
            f"{', '.join(MODES)}), press ({', '.join(BUTTONS)}), set (volume)"
        )

    def rules(self) -> str:
        return (
            "For music, use spotify's play: the title in their words, and the mode for what it is: song, artist, "
            'album, playlist (theirs first), genre or mood ("some jazz", "something calm": a playlist of it), liked '
            '("something I like", "my music": from what they like and play; no title needed), more ("more like this": '
            "more of what's playing). A Spotify link needs no mode. When play asks which, say the options briefly and "
            'ask; then play with their answer as the title ("2", "the live one") and the mode choice. Say what '
            "play found and that it's playing only when it says so."
        )

    def slow(self, action: str, args: dict) -> bool:
        return action == PLAY

    def status(self) -> str:
        now = self._api("GET", "/me/player")
        if not now:
            return f"{self.name}: nothing playing"
        item = now.get("item") or {}
        what = describe(item) if item else "something"
        state = "playing" if now.get("is_playing") else "paused"
        return f"{self.name}: {state} {what}, on {(now.get('device') or {}).get('name', 'a device')}"

    def call(self, action: str, args: dict, request: Request) -> str:
        if action == PLAY:
            return self.play(str(args.get("title") or "").strip(), str(args.get("mode") or "").strip().lower())
        if action == SET:
            level = args.get("level")
            if not isinstance(level, (int, float)) or not 0 <= level <= 100:
                return "error: set needs level, 0 to 100"
            self._api("PUT", f"/me/player/volume?volume_percent={int(level)}")
            return f"sent: volume {int(level)}%"
        pressed = button(args)
        if pressed not in BUTTONS:
            return f"error: its buttons are {', '.join(BUTTONS)}"
        method, path = BUTTONS[pressed]
        self._api(method, path)
        return f"sent: {pressed}"

    def play(self, wanted: str, mode: str = "") -> str:
        """What to play, found by `mode`; several equally good matches are offered back to choose from instead."""
        if mode not in MODES:
            return f"error: mode must be one of {', '.join(MODES)}"
        if mode == CHOICE:
            picked = self._pick(wanted)
            if picked is None:
                return "error: that isn't one of the options offered; play it by name instead"
            return self._start(*picked)
        if found := LINK.search(wanted):
            kind, key = (found[1], found[2]) if found[1] else (found[3], found[4])
            return self._start(
                [f"spotify:{kind}:{key}"] if kind == "track" else f"spotify:{kind}:{key}", f"that {kind}"
            )
        if not wanted and mode not in (LIKED, MORE):
            return "error: play needs the title: what to play, in their words"
        finding = {
            SONG: self._song,
            ARTIST: self._artist,
            ALBUM: self._album,
            PLAYLIST: self._playlist,
            GENRE: self._mood,
            MOOD: self._mood,
            LIKED: self._liked,
            MORE: self._more,
            "": self._anything,
        }
        found = finding[mode](wanted)
        if isinstance(found, str):  # a question, or why nothing was found
            return found
        return self._start(*found)

    def _start(self, what: str | list[str], said: str, shuffle: bool = False, others: list | None = None) -> str:
        self._last = {
            "uri": what[0] if isinstance(what, list) else what,
            "type": "track" if isinstance(what, list) else "",
            "name": said,
        }
        device = self._device()
        body = {"uris": what} if isinstance(what, list) else {"context_uri": what}
        if shuffle:
            self._api("PUT", f"/me/player/shuffle?state=true&device_id={device['id']}")
        self._api("PUT", f"/me/player/play?device_id={device['id']}", body)
        self._offered, deadline = [], self._clock() + PLAYING_WAIT_S
        while True:
            now = self._api("GET", "/me/player") or {}
            if now.get("is_playing") and (now.get("device") or {}).get("id") == device["id"]:
                done = f"done: playing {said} on {device['name']} (Spotify says so)"
                if others:  # kept to switch to, as an answer to "which?" is
                    self._offered, self._offered_at = [self._last, *others], self._clock()
                    listed = "; ".join(f"{n}. {describe(i)}" for n, i in enumerate(others, 2))
                    done += f". Also found: {listed}; say so in a few words, and play their pick with mode choice"
                return done
            if self._clock() >= deadline:
                return f"not confirmed: asked Spotify to play {said} on {device['name']}, and it doesn't say it is"
            self._sleep(POLL_S)

    def _search(self, query: str, kind: str, limit: int = 10) -> list[dict]:
        answer = self._api("GET", "/search?" + urlencode({"q": query, "type": kind, "limit": limit}))
        return [i for i in (answer.get(f"{kind}s") or {}).get("items") or [] if i]

    def _offer(self, items: list[dict]) -> str:
        """Asks which, keeping what was offered for the answer."""
        self._offered, self._offered_at = items, self._clock()
        options = "; ".join(f"{n}. {describe(i)}" for n, i in enumerate(items, 1))
        return f"ask which: {options}. Then play with their answer as the title, mode choice"

    def _pick(self, answer: str) -> tuple | None:
        if not self._offered or self._clock() - self._offered_at > OFFER_S:
            return None
        items, words = self._offered, normal(answer)
        number = re.search(r"\d+", answer) or next((ORDINALS[w] for w in words.split() if w in ORDINALS), None)
        if isinstance(number, re.Match):
            number = int(number[0])
        if isinstance(number, int) and 1 <= number <= len(items):
            item = items[number - 1]
        else:  # "the live one", "the one by Panic": the option that has their words, if only one does
            fits = [
                i for i in items if words and all(w in normal(describe(i)) for w in words.split() if w not in FILLER)
            ]
            if len(fits) != 1:
                return None
            item = fits[0]
        return ([item["uri"]] if item["type"] == "track" else item["uri"]), describe(item)

    def _song(self, wanted: str):
        """The song, as Spotify ranks it: its first studio version, unless they asked for a live one or a remix. Asked
        which only when the first two are different artists' songs of that name ("Hello": Adele's, Lionel Richie's)."""
        tracks = self._search(wanted, "track")
        if not tracks:
            return f"error: Spotify found no song for {wanted!r}"
        title = normal(re.split(r"\s+by\s+", wanted)[0])
        same = [t for t in tracks if normal(base_name(t.get("name", ""))) == title] or tracks
        return self._first_or_ask(same, wanted)

    def _first_or_ask(self, items: list[dict], wanted: str):
        """The artist it's most likely theirs: the one with the most of these versions (an original has its live ones
        and remixes, a cover one), and the first of theirs as Spotify ranks them. When two come level ("Hello":
        Lionel Richie's, Adele's), the one they play themselves; otherwise Spotify's first, since it ranks by how much
        each is played, with the others said to switch to. A live one, a remix, only when asked for."""
        versions = distinct(items)
        wants = {w.lower() for w in ALTERED.findall(wanted)}
        fitting = [v for v in versions if wants & {w.lower() for w in ALTERED.findall(v.get("name", ""))}]
        plain = [v for v in versions if not ALTERED.search(v.get("name", ""))]
        chosen = (fitting if wants else plain) or versions
        counts = Counter(artists(v) for v in versions)
        ranked = sorted(dict.fromkeys(artists(v) for v in versions), key=lambda a: -counts[a])  # ties keep Spotify's
        if len(ranked) > 1 and counts[ranked[0]] == counts[ranked[1]] and not wants:
            level = [a for a in ranked if counts[a] == counts[ranked[0]]]
            theirs = [a for a in level if set(a) & self._favourites()]
            if len(theirs) == 1:  # one they play themselves
                ranked = theirs
            else:  # Spotify ranks by how much each is played, so its first; the others are said, to switch to
                others = [v for v in chosen if artists(v) in level and artists(v) != ranked[0]][: MOST_OFFERED - 1]
                pick = next((v for v in chosen if artists(v) == ranked[0]), chosen[0])
                return ([pick["uri"]] if pick.get("type") == "track" else pick["uri"]), describe(pick), False, others
        pick = next((v for v in chosen if artists(v) == ranked[0]), chosen[0])
        return ([pick["uri"]] if pick.get("type") == "track" else pick["uri"]), describe(pick)

    def _favourites(self) -> set[str]:
        """The artists they play most, by name, looked up once an hour."""
        if self._clock() - self._favourites_at > FAVOURITES_S:
            top = self._api("GET", "/me/top/artists?limit=50&time_range=medium_term").get("items") or []
            self._favourite_names = {a.get("name") for a in top if a}
            self._favourites_at = self._clock()
        return self._favourite_names

    def _artist(self, wanted: str):
        artists = self._search(wanted, "artist", 5)
        if not artists:
            return f"error: Spotify found no artist {wanted!r}"
        same = [a for a in artists if normal(a.get("name", "")) == normal(wanted)] or artists[:1]
        return same[0]["uri"], describe(same[0])

    def _album(self, wanted: str):
        albums = self._search(wanted, "album", 8)
        if not albums:
            return f"error: Spotify found no album {wanted!r}"
        title = normal(re.split(r"\s+by\s+", wanted)[0])
        return self._first_or_ask(
            [a for a in albums if normal(base_name(a.get("name", ""))) == title] or albums, wanted
        )

    def _playlist(self, wanted: str):
        mine = self._api("GET", "/me/playlists?limit=50").get("items") or []
        words = normal(wanted).replace(" playlist", "").removeprefix("my ").strip()
        own = [p for p in mine if p and words and words in normal(p.get("name", ""))]
        if len(own) == 1:
            return own[0]["uri"], f"your playlist {own[0]['name']}"
        if len(own) > 1:
            return self._offer(own[:MOST_OFFERED])
        return self._mood(wanted)

    def _mood(self, wanted: str):
        """A genre or a mood: a playlist of it. Spotify's own playlists are closed to an app like this one."""
        query = re.sub(
            r"\b(some|something|music|songs|playlist|please|play)\b", " ", wanted, flags=re.IGNORECASE
        ).strip()
        lists = [p for p in self._search(query or wanted, "playlist") if (p.get("owner") or {}).get("id") != "spotify"]
        lists = [p for p in lists if ((p.get("tracks") or {}).get("total") or MIN_TRACKS) >= MIN_TRACKS] or lists
        if not lists:
            return f"error: Spotify found no playlist for {wanted!r}"
        return lists[0]["uri"], describe(lists[0])

    def _liked(self, wanted: str):
        """What they like: their most played and their liked songs, shuffled. Spotify's own picks for them are closed
        to an app like this one."""
        uris: list[str] = []
        for path in (
            "/me/top/tracks?limit=50&time_range=medium_term",
            "/me/tracks?limit=50",
            "/me/top/tracks?limit=50&time_range=short_term",
        ):
            for entry in self._api("GET", path).get("items") or []:
                track = entry.get("track", entry) if entry else None
                if track and track.get("uri") and track["uri"] not in uris:
                    uris.append(track["uri"])
        if not uris:
            return "error: Spotify has nothing they've liked or played much to go by yet"
        random.shuffle(uris)
        return uris[:MOST_URIS], "a mix of what you like", True

    def _more(self, wanted: str):
        """More like what's playing: its artist. Spotify's recommendations are closed to an app like this one."""
        item = (self._api("GET", "/me/player") or {}).get("item") or {}
        artists = item.get("artists") or []
        if not artists and not wanted:
            return "error: nothing is playing to play more like"
        if artists and artists[0].get("uri"):
            return artists[0]["uri"], f"more {artists[0].get('name', 'of that')}"
        return self._artist(wanted)

    def _anything(self, wanted: str):
        """No mode: an exact name, a playlist before an album, an artist, a song; otherwise a playlist of it."""
        answer = self._api(
            "GET", "/search?" + urlencode({"q": wanted, "type": "playlist,album,artist,track", "limit": 5})
        )
        lists = {k: [i for i in (answer.get(f"{k}s") or {}).get("items") or [] if i] for k in KINDS}
        plain = normal(wanted)
        for kind in KINDS:
            for item in lists[kind]:
                if normal(item.get("name", "")) == plain:
                    return ([item["uri"]] if kind == "track" else item["uri"]), describe(item)
        for kind in ("playlist", "track"):
            if lists[kind]:
                item = lists[kind][0]
                return ([item["uri"]] if kind == "track" else item["uri"]), describe(item)
        return f"error: Spotify found nothing for {wanted!r}"

    def _device(self) -> dict:
        found = self._match(self._api("GET", "/me/player/devices").get("devices") or [])
        if found:
            return found
        if self._wake is None:
            raise HomeError(f"{self.play_on} isn't on Spotify now: is its app open?")
        said = self._wake()
        if said.startswith(("error", "not confirmed")):
            raise HomeError(f"couldn't open Spotify on it: {said}")
        deadline = self._clock() + WAKE_WAIT_S
        while self._clock() < deadline:
            self._sleep(POLL_S)
            if found := self._match(self._api("GET", "/me/player/devices").get("devices") or []):
                return found
        raise HomeError(f"{self.play_on} didn't show up on Spotify after its app was opened")

    def _match(self, devices: list[dict]) -> dict | None:
        return next(
            (d for d in devices if self.play_on.lower() in str(d.get("name", "")).lower() and d.get("id")), None
        )

    def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        for renew in (False, True):
            headers = {"Authorization": f"Bearer {self.account.token(renew)}"}
            try:
                answer = self._http.request(method, API + path, headers=headers, json=body)
            except httpx.HTTPError as e:
                raise HomeError(f"Spotify didn't answer ({type(e).__name__})") from None
            if answer.status_code != 401:
                break
        if answer.status_code == 403:
            raise HomeError("Spotify refused: it needs Premium for this")
        if answer.status_code == 404:
            raise HomeError("Spotify has no device playing to do that on")
        if answer.status_code >= 400:
            raise HomeError(f"Spotify said {answer.status_code}")
        try:  # some answers carry a body that isn't JSON (a pause's): only what's read matters
            return answer.json() if answer.content else {}
        except ValueError:
            return {}


def normal(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower().removeprefix("the ")).strip()


def base_name(name: str) -> str:
    """A song or album without its version: "Bohemian Rhapsody - Remastered 2011" is "Bohemian Rhapsody"."""
    return VERSION.sub("", name)


def distinct(items: list[dict]) -> list[dict]:
    """The same thing listed once, a remaster as the same recording: by name and artists, keeping the first, as
    Spotify ranks them."""
    seen, kept = set(), []
    for item in items:
        key = (
            normal(SAME_RECORDING.sub("", item.get("name", ""))),
            tuple(a.get("name") for a in item.get("artists") or []),
        )
        if key not in seen:
            seen.add(key)
            kept.append(item)
    return kept


def artists(item: dict) -> tuple:
    return tuple(a.get("name") for a in item.get("artists") or [])


def describe(item: dict) -> str:
    kind, name = item.get("type", ""), item.get("name", "")
    artists = ", ".join(a.get("name", "") for a in item.get("artists") or [])
    if kind in ("track", "album") and artists:
        return f"{name} by {artists}"
    return f"{name} ({kind})" if kind in ("playlist", "artist") else name


def make(name: str, spec: dict, context) -> Spotify:
    client = context.secret(CLIENT_KEY)
    if not client:
        raise ValueError(f"no {CLIENT_KEY} in .env: an app of your own at developer.spotify.com (docs/home.md#spotify)")
    http = httpx.Client(timeout=TIMEOUT_S)
    account = Account(client, context.root / TOKEN_FILE, http)
    wake = None
    if spec.get("wake"):
        target = str(spec["wake"]).strip().lower()

        def wake() -> str:
            device = context.devices.get(target)
            if device is None:
                return f"error: there's no device called {target!r} to open Spotify on"
            return device.call("open", {"app": "spotify"}, Request())

    return Spotify(name, account, spec["play_on"], http, wake)


def begin(client_id: str, path: Path, http: httpx.Client | None = None) -> tuple[str, Callable[[str], None]]:
    """A sign-in, with PKCE (no secret to keep): the link to sign in with, and `finish(address)`, which takes the
    address Spotify then goes on to and keeps the sign-in in `path`; ValueError if it can't."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    query = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "scope": SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    }

    def finish(address: str) -> None:
        pasted = parse_qs(urlsplit(address.strip()).query)
        if pasted.get("state", [""])[0] != state or not pasted.get("code"):
            raise ValueError("That isn't the address Spotify sent you to, for this sign-in")
        answer = (http or httpx.Client(timeout=TIMEOUT_S)).post(
            f"{ACCOUNTS}/api/token",
            data={
                "grant_type": "authorization_code",
                "code": pasted["code"][0],
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        if answer.status_code != 200:
            raise ValueError(f"Spotify turned the sign-in down ({answer.status_code}): it may have taken too long")
        keep(path, answer.json()["refresh_token"])

    return f"{ACCOUNTS}/authorize?{urlencode(query)}", finish


def login(client_id: str, path: Path, ask: Callable[[str], str] = input, http: httpx.Client | None = None) -> None:
    """Signs in from the command line: the link printed, the address pasted back."""
    link, finish = begin(client_id, path, http)
    print(f"Open this, sign in to Spotify and agree:\n\n{link}\n")
    print("Spotify then sends the browser on to an address that won't load. Copy that address from the address bar.")
    try:
        finish(ask("The address: "))
    except ValueError as e:
        raise SystemExit(f"{e}: try again") from None
    print(f"Signed in. Kept in {path}.")


def page_login(
    client_id: str, path: Path, host: str, port: int, send: Callable[[str], None], http: httpx.Client | None = None
) -> str:
    """Signs in from a chat, as the browser's logins are added: the link to sign in with, then a one-time page
    (browser/login_page.py) to paste the address Spotify goes on to. What to tell them at the end."""
    from .browser.login_page import LOGIN_WAIT_S, Asking, Field, ask

    link, finish = begin(client_id, path, http)
    send(f"To sign TARS in to Spotify, open this, sign in and agree:\n{link}")
    minutes = f"{LOGIN_WAIT_S // 60:.0f} minutes"

    def send_link(url: str) -> None:
        send(
            f"Your browser then goes on to an address that won't load. Copy it from the address bar and paste it "
            f"here (the link works once, for {minutes}):\n{url}"
        )

    field = Field("address", "The address Spotify sent you to")
    try:
        given = ask(Asking("https://accounts.spotify.com", "to play Spotify for you", (field,)), send_link, host, port)
    except OSError as e:  # the port taken, or the address gone
        print(f"(spotify: couldn't serve the sign-in page on {host}:{port}: {e})")
        return "I couldn't open the page to paste the address on, so I stopped there."
    if not given:
        return "No address came in time. Send /spotify to try again."
    try:
        finish(given["address"])
    except ValueError as e:
        return f"{e}. Send /spotify to try again."
    return "Spotify is signed in."


def main() -> None:
    from dotenv import dotenv_values

    if sys.argv[1:] != ["login"]:
        raise SystemExit("usage: python -m voice_assistant.spotify login")
    client = dotenv_values(".env").get(CLIENT_KEY) if Path(".env").exists() else None
    if not client:
        raise SystemExit(f"Put your app's client id in .env as {CLIENT_KEY} first (docs/home.md#spotify)")
    login(client, Path(TOKEN_FILE))


if __name__ == "__main__":
    main()
