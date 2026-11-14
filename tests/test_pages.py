"""Reading pages: a link's words, the quick way or in the browser, and what's learned of each site, so the second
time goes straight to the way that works and nobody is asked twice whether they subscribe."""

import json
import types

import httpx
import pytest

from voice_assistant.config import LLMConfig
from voice_assistant.feeds import Page
from voice_assistant.home import Request
from voice_assistant.llm import OpenAIChat
from voice_assistant.pages import RELEARN_S, Pages, Refused, article, fetch, site_of
from voice_assistant.store import Store

from .conftest import fake_openai_chat

NOW = 1_800_000_000.0
STORY = "Record rain fell in Kadima-Tzoran overnight, and the roads to the coast were closed until morning. " * 8


def ld(**fields):
    return f'<script type="application/ld+json">{json.dumps(fields)}</script>'


YNET = f"<html><head><title>ynet</title>{ld(headline='Storm floods the Sharon', articleBody=STORY)}</head></html>"
ARTICLE = (
    "<html><head><title>Davar: a strike</title></head><body><nav><p>Home | News | Sport</p></nav>"
    "<p>Subscribe to our newsletter</p><article><h1>The port strikes</h1><script>var x = 1;</script>"
    f"<p>{STORY}</p><aside><p>Read also: something else</p></aside><p>The talks go on tomorrow.</p></article>"
    "<footer><p>All rights reserved</p></footer></body></html>"
)
TEASER = "The coalition's plan for the budget, as told to Haaretz by two of its ministers."
HAARETZ = "<html>" + ld(**{"@graph": [{"@type": "NewsArticle", "headline": "The budget", "articleBody": TEASER,
                                       "isAccessibleForFree": "False"}]}) + "</html>"  # fmt: skip
SCRIPTED = "<html><head><title>An app</title></head><body><div id='root'></div></body></html>"
RELATED = "A different story entirely, about the elections, in a box of related ones. " * 20
WITH_RELATED = (
    f"<html>{ld(headline='A player speaks out', articleBody=STORY)}<article><p>{RELATED}</p></article></html>"
)
ASPX = (  # Globes: the whole page one form, a paywall flag on its free pages too, and no articleBody
    f"<html><body><form id='form1'>{ld(headline='Wall Street rises', isAccessibleForFree=False)}"
    f"<article><div><p><strong>22:01</strong><br/>{STORY}</p></div></article></form></body></html>"
)
NO_PARAGRAPHS = f"<html><body><article><div class='text'>{STORY}</div><div>{STORY}</div></article></body></html>"


class Sites:
    """The web, as the tests have it: each address's page, or the status it answers with; and what was asked for."""

    def __init__(self):
        self.pages = {
            "https://www.ynet.co.il/news/article/1": YNET,
            "https://www.ynet.co.il/news/article/2": YNET,
            "https://www.davar1.co.il/1/": ARTICLE,
            "https://www.haaretz.co.il/news/1": HAARETZ,
            "https://www.haaretz.co.il/news/2": HAARETZ,
            "https://app.example/story": SCRIPTED,
        }
        self.refusing = {"timesofisrael.com": 403}
        self.asked: list[str] = []

    def __call__(self, url):
        self.asked.append(url)
        site = site_of(url)
        if site in self.refusing:
            response = httpx.Response(self.refusing[site])
            raise httpx.HTTPStatusError("no", request=httpx.Request("GET", url), response=response)
        if url not in self.pages:
            raise httpx.HTTPStatusError("gone", request=httpx.Request("GET", url), response=httpx.Response(404))
        return Page(self.pages[url].encode(), "text/html", url)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def web():
    return Sites()


@pytest.fixture
def clock():
    return Clock()


class Browse:
    """The brain's browse tool, as the tests have it: the errands started, and what it answers."""

    def __init__(self):
        self.errands: list[str] = []

    def call(self, args, request):
        self.errands.append(args["errand"])
        return "started: say you're on it, in a few words; the answer comes to this chat when it's done"


@pytest.fixture
def browse():
    return Browse()


@pytest.fixture
def pages(tmp_path, web, clock, browse):
    return Pages(Store(tmp_path / "db"), browse=browse, get=web, clock=clock)


def ask(pages, speaker="Alon", **args):
    full = {"action": "read", "url": None, "subscribes": None} | args
    return pages.call(full, types.SimpleNamespace(speaker=speaker))


def test_a_pages_words_are_its_structured_datas_article_or_its_articles_paragraphs():
    found = article(YNET)
    assert (found.title, found.words, found.paywalled) == ("Storm floods the Sharon", STORY.strip(), False)
    found = article(ARTICLE)
    assert found.words.splitlines() == ["The port strikes", STORY.strip(), "The talks go on tomorrow."]
    assert found.title == "Davar: a strike"
    found = article(HAARETZ)
    assert (found.words, found.paywalled) == (TEASER, True)  # in an @graph, its flag a string


@pytest.mark.parametrize(
    ("page", "starts"),
    [(WITH_RELATED, "Record rain"), (ASPX, "22:01 Record rain"), (NO_PARAGRAPHS, "Record rain")],
    ids=["its own story, not the related ones", "a page that's all one form", "an article without paragraphs"],
)
def test_a_pages_words_are_found_however_its_written(page, starts):
    found = article(page)
    assert found.words.startswith(starts) and "elections" not in found.words and len(found.words) > 500


def test_a_page_read_the_quick_way_is_told_and_its_site_remembered_so(pages, web):
    said = ask(pages, url="https://www.ynet.co.il/news/article/1")
    assert said.startswith("Storm floods the Sharon (https://www.ynet.co.il/news/article/1):\n") and STORY[:40] in said
    assert ask(pages, action="known", url="ynet.co.il") == (
        "What TARS knows about reading sites:\n- ynet.co.il: read with a plain request (learned today)"
    )


def test_a_site_that_turns_tars_away_is_read_in_the_browser_from_then_on(pages, web, browse):
    said = ask(pages, url="https://www.timesofisrael.com/a-story/")
    assert said.startswith("Reading it in TARS's browser, since the site turns away a plain request (the site answered")
    assert said.endswith("The browse tool says: started: say you're on it, in a few words; the answer comes to this "
                         "chat when it's done")  # fmt: skip
    again = ask(pages, url="https://www.timesofisrael.com/another/")
    assert again.startswith("Reading it in TARS's browser, since its pages are read in the browser")
    assert web.asked == ["https://www.timesofisrael.com/a-story/"]  # the second time, straight to the browser
    assert browse.errands == [
        "Read https://www.timesofisrael.com/a-story/, and tell me what it says.",
        "Read https://www.timesofisrael.com/another/, and tell me what it says.",
    ]


def test_without_the_browser_a_page_tars_cant_read_is_searched_for(tmp_path, web):
    pages = Pages(Store(tmp_path / "db"), get=web, clock=lambda: NOW)
    said = ask(pages, url="https://app.example/story")
    assert said.startswith("Can't read it here: its words aren't in the page as it comes")
    assert said.endswith("Search the web for the story.")


def test_a_page_for_subscribers_asks_once_whether_they_subscribe(pages, web, browse):
    said = ask(pages, url="https://www.haaretz.co.il/news/1")
    assert said.startswith("haaretz.co.il is for subscribers.") and TEASER in said
    assert "Ask them, briefly, whether they subscribe to it" in said
    assert ask(pages, action="subscribed", url="https://www.haaretz.co.il", subscribes=True) == (
        "done: they subscribe to haaretz.co.il"
    )
    said = ask(pages, url="https://www.haaretz.co.il/news/2")
    assert said.startswith("Reading it in TARS's browser, since it's for subscribers, and they subscribe")
    assert browse.errands == [
        (
            "Read https://www.haaretz.co.il/news/2, signed in to my haaretz.co.il subscription (sign in if it asks), "
            "and tell me what it says."
        )
    ]
    assert ask(pages, action="known", url=None) == (
        "What TARS knows about reading sites:\n"
        "- haaretz.co.il: read with a plain request, some of its pages for subscribers; they subscribe (learned today)"
    )


def test_a_site_with_free_pages_and_subscribers_has_its_free_ones_read_the_quick_way(pages, web):
    ask(pages, url="https://www.haaretz.co.il/news/1")
    web.pages["https://www.haaretz.co.il/free"] = YNET
    assert ask(pages, url="https://www.haaretz.co.il/free").startswith("Storm floods the Sharon")
    assert "some of its pages for subscribers" in ask(pages, action="known", url="haaretz.co.il")


def test_someone_who_doesnt_subscribe_has_the_web_searched_and_isnt_asked_again(pages):
    ask(pages, url="https://www.haaretz.co.il/news/1")
    ask(pages, action="subscribed", url="haaretz.co.il", subscribes=False)
    said = ask(pages, url="https://www.haaretz.co.il/news/2")
    assert said.startswith("haaretz.co.il is for subscribers, and they don't subscribe: search the web")
    assert "Ask them" in ask(pages, speaker="Stacey", url="https://www.haaretz.co.il/news/2")  # hers is her own


def test_whose_subscription_needs_whose_voice(pages):
    said = ask(pages, speaker=None, url="https://www.haaretz.co.il/news/1")
    assert "TARS doesn't know who's asking" in said and "search the web" in said
    assert ask(pages, speaker=None, action="subscribed", url="haaretz.co.il", subscribes=True).startswith("error")


def test_whats_learned_of_a_site_is_tried_the_quick_way_again_after_thirty_days(pages, web, clock):
    ask(pages, url="https://www.timesofisrael.com/a/")
    web.refusing.clear()
    web.pages["https://www.timesofisrael.com/b/"] = YNET
    clock.now += RELEARN_S + 1
    assert ask(pages, url="https://www.timesofisrael.com/b/").startswith("Storm floods the Sharon")
    assert "read with a plain request" in ask(pages, action="known", url="timesofisrael.com")


def test_a_site_that_starts_turning_tars_away_is_learned_again(pages, web):
    ask(pages, url="https://www.ynet.co.il/news/article/1")
    web.refusing["ynet.co.il"] = 403
    assert ask(pages, url="https://www.ynet.co.il/news/article/2").startswith("Reading it in TARS's browser")


def test_a_page_that_isnt_there_teaches_nothing(pages):
    assert ask(pages, url="https://www.ynet.co.il/gone").startswith("error: couldn't read it: the site answered 404")
    assert ask(pages, action="known", url=None) == "TARS hasn't read any site's pages yet."


def test_saving_a_subscription_is_held_back_as_any_change_is(pages):
    searched = types.SimpleNamespace(speaker="Alon", read_the_web=True)
    args = {"action": "subscribed", "url": "haaretz.co.il", "subscribes": True}
    assert pages.call(args, searched).startswith("refused: this reply read the web")
    assert "subscribe" not in ask(pages, action="known", url=None)


def test_reading_needs_a_link_and_subscribing_a_yes_or_no(pages):
    assert ask(pages, url=None) == "error: read needs the link"
    assert ask(pages, action="subscribed", url="haaretz.co.il").startswith("error: subscribed needs")
    assert ask(pages, action="nope").startswith("error: action is one of")


def test_the_home_network_is_never_read_not_even_by_a_redirect():
    def answer(request):
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"location": "http://192.168.1.10/admin"})
        return httpx.Response(200, text=YNET)

    transport = httpx.MockTransport(answer)
    assert fetch("https://93.184.215.14/story", transport=transport).body.decode() == YNET
    for url in ("http://192.168.1.10/admin", "http://homeassistant.local:8123/", "https://93.184.215.14/moved",
                "file:///etc/passwd"):  # fmt: skip
        with pytest.raises(Refused):
            fetch(url, transport=transport)


def test_a_reply_that_read_a_page_only_reads_the_house_after(pages):
    from voice_assistant import toolbox

    seen = []
    spy = toolbox.Tool({"type": "function", "name": "tars", "parameters": {}}, toolbox.READS,
                       lambda args, turn: seen.append(turn.request.read_the_web) or "ok")  # fmt: skip
    myself = types.SimpleNamespace(toolset=lambda: toolbox.Toolset([spy]))

    def calls(messages):
        if messages[-1].get("role") == "user":
            return [
                ("read_page", {"action": "read", "url": "https://www.ynet.co.il/news/article/1", "subscribes": None})
            ]
        if messages[-1].get("type") == "function_call_output" and "Storm" in messages[-1]["output"]:
            return [("tars", {})]
        return []

    client = fake_openai_chat(lambda m: "Done." if m[-1].get("output") == "ok" else "", calls)
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False), myself=myself, tools=[pages])
    assert "".join(brain.stream_reply("tell me more", Request(speaker="Alon"))) == "Done."
    assert seen == [True]
    assert "Pages: read_page reads the page a link goes to" in client.requests[0]["instructions"]
