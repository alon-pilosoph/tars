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
import re
import secrets
import sys
import time
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
KINDS = ("playlist", "album", "artist", "track")  # an exact name is taken in this order
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

    def what(self) -> str:
        return f"Spotify, playing on {self.play_on}: status (what's playing), play, press ({', '.join(BUTTONS)}), set"

    def rules(self) -> str:
        return (
            "For music on Spotify, use spotify's play with what they asked for as the title, in their words (\"some "
            'jazz", "the Beatles", "Bohemian Rhapsody", a Spotify link); press pause, play, next or previous; set its '
            "volume with level. Say what play found and that it's playing only when it says so."
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
            return self.play(str(args.get("title") or "").strip())
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

    def play(self, wanted: str) -> str:
        if not wanted:
            return 'error: play needs the title: what to play, as "some jazz" or "the Beatles"'
        uri, what = self._find(wanted)
        if uri is None:
            return f"error: Spotify found nothing for {wanted!r}"
        device = self._device()
        body = {"uris": [uri]} if uri.startswith("spotify:track:") else {"context_uri": uri}
        self._api("PUT", f"/me/player/play?device_id={device['id']}", body)
        deadline = self._clock() + PLAYING_WAIT_S
        while True:
            now = self._api("GET", "/me/player") or {}
            if now.get("is_playing") and (now.get("device") or {}).get("id") == device["id"]:
                return f"done: playing {what} on {device['name']} (Spotify says so)"
            if self._clock() >= deadline:
                return f"not confirmed: asked Spotify to play {what} on {device['name']}, and it doesn't say it is"
            self._sleep(POLL_S)

    def _find(self, wanted: str) -> tuple[str | None, str]:
        if found := LINK.search(wanted):
            kind, key = (found[1], found[2]) if found[1] else (found[3], found[4])
            return f"spotify:{kind}:{key}", f"that {kind}"
        answer = self._api("GET", "/search?" + urlencode({"q": wanted, "type": ",".join(KINDS), "limit": 5}))
        lists = {kind: [i for i in (answer.get(f"{kind}s") or {}).get("items") or [] if i] for kind in KINDS}
        plain = normal(wanted)
        for kind in KINDS:
            for item in lists[kind]:
                if normal(item.get("name", "")) == plain:
                    return item["uri"], describe(item)
        for kind in ("playlist", "track"):  # "some jazz": a playlist of it; then whatever song came first
            if lists[kind]:
                return lists[kind][0]["uri"], describe(lists[kind][0])
        return None, ""

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
        return answer.json() if answer.content else {}


def normal(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower().removeprefix("the ")).strip()


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


def login(client_id: str, path: Path, ask: Callable[[str], str] = input, http: httpx.Client | None = None) -> None:
    """Sign in once, with PKCE (no secret to keep): a link to open anywhere, and the address it goes on to, pasted."""
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
    print(f"Open this, sign in to Spotify and agree:\n\n{ACCOUNTS}/authorize?{urlencode(query)}\n")
    print("Spotify then sends the browser on to an address that won't load. Copy that address from the address bar.")
    pasted = parse_qs(urlsplit(ask("The address: ").strip()).query)
    if pasted.get("state", [""])[0] != state or not pasted.get("code"):
        raise SystemExit("That isn't the address Spotify sent you to: try again")
    http = http or httpx.Client(timeout=TIMEOUT_S)
    answer = http.post(
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
        raise SystemExit(f"Spotify turned the sign-in down ({answer.status_code}): try again")
    keep(path, answer.json()["refresh_token"])
    print(f"Signed in. Kept in {path}.")


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
