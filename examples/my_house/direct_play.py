"""Playing a film or an episode on Stremio straight from its link, in seconds and with no model working the screen:
the title is looked up on IMDb by name (IMDb's ids are what Stremio's add-ons go by), its page opened by link, the
first stream chosen, and it's playing only once the box's media session says so. Any other app, or a title it can't
find, goes to the agent as usual.

The title asked for goes to IMDb's public search suggestions, with no key; nothing else is sent."""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from voice_assistant.androidtv import AndroidTV, TvAgent, TvDevice, wait_for

STREMIO = "stremio"
IMDB_SUGGEST = "https://v3.sg.media-imdb.com/suggestion/{first}/{query}.json"
LOOKUP_TIMEOUT_S = 5.0
MOVIES, SERIES = ("movie", "tvMovie"), ("tvSeries", "tvMiniSeries")
STREAMS_WAIT_S = 2.5  # Stremio's stream list filling in once its page is up
PLAY_WAIT_S = 20.0  # the first stream starting to play
EPISODE = re.compile(r"\s+s(\d+)\s*e(\d+)\s*$", re.IGNORECASE)
YEAR = re.compile(r"\s*\((\d{4})\)\s*$")


@dataclass
class Title:
    """What someone asked to play: "Ratatouille (2007)", "Wednesday S1E3"."""

    name: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None

    @classmethod
    def parse(cls, text: str) -> Title:
        text, season, episode = text.strip(), None, None
        if found := EPISODE.search(text):
            season, episode, text = int(found[1]), int(found[2]), text[: found.start()]
        year = int(found[1]) if (found := YEAR.search(text)) else None
        return cls(YEAR.sub("", text).strip(), year, season, episode)


@dataclass
class Found:
    id: str  # IMDb's: tt0382932
    name: str
    year: int | None
    series: bool

    def said(self) -> str:
        return f"{self.name} ({self.year})" if self.year else self.name


class Catalog:
    """Titles by name, from IMDb's search suggestions."""

    def __init__(self, client: httpx.Client | None = None):
        self._http = client or httpx.Client(timeout=LOOKUP_TIMEOUT_S, follow_redirects=True)

    def imdb(self, title: Title) -> Found | None:
        """The best match on IMDb: its first suggestion of the right kind (a series, for an episode) and year."""
        query = title.name.lower()
        first = query[0] if query[:1].isalnum() else "x"
        response = self._http.get(IMDB_SUGGEST.format(first=first, query=quote(query)))
        response.raise_for_status()
        kinds = SERIES if title.season else MOVIES + SERIES
        for s in response.json().get("d", []):
            if str(s.get("id", "")).startswith("tt") and s.get("qid") in kinds and title.year in (None, s.get("y")):
                return Found(s["id"], s.get("l") or title.name, s.get("y"), s["qid"] in SERIES)
        return None


class FireTv(TvDevice):
    """The household's Fire TV: the core Android TV device, with play going straight to Stremio."""

    def __init__(
        self,
        name: str,
        box: AndroidTV,
        agent: TvAgent | None = None,
        catalog: Catalog | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        super().__init__(name, box, agent, sleep=sleep)
        self.catalog = catalog or Catalog()

    def play(self, title: str, app: str) -> str:
        app = (app or STREMIO).strip().lower()  # this house watches on Stremio unless it says otherwise
        wanted = Title.parse(title)
        if app != STREMIO or not wanted.name:
            return super().play(title, app)
        package = self.box.find_app(app)
        if not package:
            return f"error: {app} isn't installed on the {self.name}"
        try:
            found = self.catalog.imdb(wanted)
        except (httpx.HTTPError, ValueError) as e:
            print(f"(tv: couldn't look up {wanted.name!r} on IMDb: {e!r}; the agent finds it on screen)")
            found = None
        if found is None:
            return super().play(title, app)
        if not self.box.awake():
            self.box.wake()
            self._sleep(2.0)
        if found.series and not wanted.season:
            self.box.open_link(f"stremio:///detail/series/{found.id}", package)
            return f"done: {found.said()} is open in Stremio; it's a series, so ask which season and episode"
        kind = "series" if found.series else "movie"
        video = f"{found.id}:{wanted.season}:{wanted.episode}" if found.series else found.id
        self.box.open_link(f"stremio:///detail/{kind}/{found.id}/{video}", package)
        self._sleep(STREAMS_WAIT_S)
        self.box.press("select")  # the first stream
        episode = f", season {wanted.season} episode {wanted.episode}" if wanted.season else ""
        if wait_for(lambda: self._playing(package), PLAY_WAIT_S, self._sleep):
            return f"done: {found.said()}{episode} is playing on Stremio (the {self.name} says so)"
        return f"not confirmed: opened {found.said()}{episode} on Stremio and chose the first stream; nothing plays yet"

    def _playing(self, package: str) -> bool:
        return any(p == package and state == "playing" for p, state, _ in self.box.media())

    def slow(self, action: str, args: dict) -> bool:
        return super().slow(action, args) or action == "play"
