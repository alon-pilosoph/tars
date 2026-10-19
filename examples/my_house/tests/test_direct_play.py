"""Playing a title on Stremio straight from its link: found on IMDb, opened by link, the first stream chosen, and said
to be playing only once the box says so. Anything it can't do that way goes to the agent."""

import types

import httpx

from examples.my_house.direct_play import Catalog, FireTv, Found, Title
from voice_assistant.androidtv import AndroidTV
from voice_assistant.home import Request

STREMIO = "com.stremio.one"


class Box(AndroidTV):
    """A Fire TV whose shell answers from a script: Stremio plays once a stream is chosen on a title's page."""

    def __init__(self):
        super().__init__("10.0.0.8", key_path=None)
        self.front, self.session, self.commands = "com.amazon.tv.launcher", "", []

    def _run(self, command, decode=True, timeout_s=10.0):
        self.commands.append(command)
        if command.startswith("dumpsys power"):
            return "  mWakefulness=Awake"
        if command.startswith("dumpsys window"):
            return f"  mCurrentFocus=Window{{7e2 u0 {self.front}/{self.front}.MainActivity}}"
        if command.startswith("dumpsys media_session"):
            return self.session
        if command == "pm list packages":
            return f"package:com.amazon.tv.launcher\npackage:{STREMIO}\n"
        if command.startswith("am start "):
            self.front = command.split()[-1]
        elif command == "input keyevent KEYCODE_DPAD_CENTER" and self.front == STREMIO:
            self.session = f"  package={STREMIO}\n  active=true\n  state=PlaybackState {{state=3}}\n"
        return ""


class Titles:
    """IMDb, as far as the TV can tell: what it was asked, and Ratatouille or Wednesday found."""

    def __init__(self, found=True):
        self.asked: list[Title] = []
        self.found = found

    def imdb(self, title: Title) -> Found | None:
        self.asked.append(title)
        if not self.found:
            return None
        if title.season or title.name == "Wednesday":
            return Found("tt13443470", "Wednesday", 2022, True)
        return Found("tt0382932", "Ratatouille", 2007, False)


def player(found=True) -> tuple[FireTv, Box, Titles, list[str]]:
    box, titles, goals = Box(), Titles(found), []
    agent = types.SimpleNamespace(start=goals.append, stop=lambda: None, model="gpt")
    return FireTv("fire tv", box, agent, titles, sleep=lambda s: None), box, titles, goals


def play(tv: FireTv, title: str, app: str | None = "stremio") -> str:
    return tv.call("play", {"title": title, "app": app}, Request("alon"))


def test_titles_are_read_with_their_year_or_episode():
    assert Title.parse("Ratatouille (2007)") == Title("Ratatouille", 2007)
    assert Title.parse(" Wednesday s1e3 ") == Title("Wednesday", None, 1, 3)
    assert Title.parse("Toy Story") == Title("Toy Story")


def test_a_film_plays_on_stremio_from_its_link_and_the_first_stream():
    tv, box, titles, goals = player()
    assert play(tv, "Ratatouille (2007)") == "done: Ratatouille (2007) is playing on Stremio (the fire tv says so)"
    link = next(c for c in box.commands if c.startswith("am start"))
    assert "-d stremio:///detail/movie/tt0382932/tt0382932 " in link and link.endswith(f" {STREMIO}")
    assert titles.asked == [Title("Ratatouille", 2007)] and goals == []


def test_stremio_is_where_this_house_watches_unless_it_says_otherwise():
    tv, _, _, _ = player()
    assert play(tv, "Ratatouille", app=None).startswith("done: Ratatouille (2007) is playing on Stremio")


def test_an_episode_goes_to_its_own_streams():
    tv, box, _, _ = player()
    assert play(tv, "Wednesday S1E3").startswith("done: Wednesday (2022), season 1 episode 3 is playing on Stremio")
    assert any("stremio:///detail/series/tt13443470/tt13443470:1:3" in c for c in box.commands)


def test_a_series_without_an_episode_opens_its_page_and_asks_which():
    tv, box, _, _ = player()
    said = play(tv, "Wednesday")
    assert "ask which season and episode" in said and "KEYCODE_DPAD_CENTER" not in " ".join(box.commands)


def test_a_series_page_that_never_comes_up_is_not_said_to_be_open():
    tv, box, _, _ = player()
    box.open_link = lambda uri, package, **kw: None  # Stremio didn't come to the front
    assert play(tv, "Wednesday").startswith("not confirmed: asked Stremio to open Wednesday (2022)")


def test_the_agent_is_told_how_stremio_lists_streams():
    tv, _, _, _ = player()
    assert "first one that plays" in tv.agent.extra_rules


def test_what_it_cant_find_or_another_app_goes_to_the_agent():
    tv, box, _, goals = player(found=False)
    assert play(tv, "Ratatoullie").startswith("started") and goals == ["play Ratatoullie on stremio"]
    assert play(tv, "the news", app="YouTube").startswith("started") and goals[-1] == "play the news on youtube"
    assert not any(c.startswith("am start") for c in box.commands)


def test_imdb_not_answering_goes_to_the_agent_too():
    tv, _, titles, goals = player()

    def down(title):
        raise httpx.ConnectError("no route to host")

    titles.imdb = down
    assert play(tv, "Ratatouille").startswith("started") and goals == ["play Ratatouille on stremio"]


def test_an_id_that_isnt_imdbs_is_never_taken():
    def answer(request):
        return httpx.Response(200, json={"d": [{"id": "tt1'; reboot", "l": "Ratatouille", "y": 2007, "qid": "movie"}]})

    assert Catalog(httpx.Client(transport=httpx.MockTransport(answer))).imdb(Title("Ratatouille")) is None


def test_the_catalog_reads_imdbs_suggestions_and_takes_the_first_of_the_right_kind():
    def answer(request):
        assert request.url.host == "v3.sg.media-imdb.com" and request.url.path == "/suggestion/r/ratatouille.json"
        d = [
            {"id": "tt13721696", "l": "Ratatouille: The TikTok Musical", "y": 2021, "qid": "video"},
            {"id": "tt0382932", "l": "Ratatouille", "y": 2007, "qid": "movie"},
        ]
        return httpx.Response(200, json={"d": d})

    catalog = Catalog(httpx.Client(transport=httpx.MockTransport(answer)))
    assert catalog.imdb(Title("Ratatouille")) == Found("tt0382932", "Ratatouille", 2007, False)
    assert catalog.imdb(Title("Ratatouille", 1999)) is None
    assert catalog.imdb(Title("Ratatouille", None, 1, 1)) is None  # an episode needs a series
