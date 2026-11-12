"""Feeds: each person's RSS and Atom feeds, by category, found, previewed, followed and read by talking to TARS."""

import types
from email.utils import formatdate

import httpx
import pytest

from voice_assistant.config import LLMConfig
from voice_assistant.feeds import Feeds, NotAFeed, Page, feed_links, parse, quality, told
from voice_assistant.home import Request
from voice_assistant.llm import OpenAIChat
from voice_assistant.store import Store

from .test_llm import fake_openai_chat

NOW = 1_800_000_000.0
HOUR = 3600.0
FULL = "The whole article, at length. " * 50


def rss(title, *items):
    return f"<rss><channel><title>{title}</title>{''.join(items)}</channel></rss>".encode()


def item(title, link, summary="", hours_ago=1.0, content=""):
    body = f"<content:encoded><![CDATA[{content}]]></content:encoded>" if content else ""
    return (
        f"<item><title><![CDATA[{title}]]></title><link>{link}</link><description><![CDATA[{summary}]]></description>"
        f"{body}<pubDate>{formatdate(NOW - hours_ago * HOUR)}</pubDate></item>"
    )


def rss_with_content(title, *items):
    ns = 'xmlns:content="http://purl.org/rss/1.0/modules/content/"'
    return f"<rss {ns}><channel><title>{title}</title>{''.join(items)}</channel></rss>".encode()


ATOM_FEED = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Indie Games</title>
<entry><title>A tiny game about frogs</title><link rel="alternate" href="https://indie/frogs"/>
<summary>It's about frogs, and it's lovely, and you can finish it in an afternoon with time to spare.</summary>
<updated>2027-01-15T07:00:00Z</updated></entry></feed>"""

YNET = rss(
    "Ynet",
    item("Storm floods the Sharon", "https://y/1", "<div><img src='p.jpg'></div>Record rain fell in Kadima-Tzoran."),
    item("Old news", "https://y/old", "From days ago.", hours_ago=100),
)
TOI = rss("The Times of Israel", item("Knesset convenes", "https://t/1", "The new Knesset meets on Tuesday.", 2))
GAMES = rss("Eurogamer", item("A new Zelda", "https://g/1", "Nintendo announced a new Zelda for next year.", 3))
HOME_PAGE = (
    b'<!doctype html><html><head><link rel="alternate" type="application/rss+xml" href="/rss.xml"></head></html>'
)

SITES = {
    "https://ynet/rss": (YNET, "application/xml"),
    "https://toi/feed": (TOI, "application/rss+xml"),
    "https://games/feed": (GAMES, "text/xml"),
    "https://blog.example": (HOME_PAGE, "text/html"),
    "https://blog.example/rss.xml": (GAMES, "application/xml"),
    "https://quiet.example": (b"<!doctype html><html></html>", "text/html"),
    "https://quiet.example/feed": (TOI, "application/xml"),
}


def get(url):
    if url in SITES:
        return Page(*SITES[url], url)
    if url == "https://moved.example/feed/":  # as Times of Israel: kept where it ends up
        return Page(TOI, "application/xml", "https://www.moved.example/feed/")
    if url in ("https://refused.example", "https://moved.example"):
        raise httpx.HTTPStatusError("no", request=httpx.Request("GET", url), response=httpx.Response(403))
    raise httpx.HTTPStatusError("gone", request=httpx.Request("GET", url), response=httpx.Response(404))


@pytest.fixture
def feeds(tmp_path):
    return Feeds(Store(tmp_path / "db"), default_person="Alon", get=get, clock=lambda: NOW)


def ask(feeds, speaker="Alon", **args):
    full = {"action": None, "category": None, "url": None, "name": None, "everything": None} | args
    return feeds.call(full, types.SimpleNamespace(speaker=speaker))


def test_rss_gives_each_storys_headline_words_link_and_time_newest_first():
    parsed = parse("Ynet", YNET)
    assert parsed.title == "Ynet"
    first = parsed.stories[0]
    assert (first.title, first.summary, first.link) == (
        "Storm floods the Sharon",
        "Record rain fell in Kadima-Tzoran.",
        "https://y/1",
    )
    assert first.published == NOW - HOUR


def test_atom_too():
    parsed = parse("", ATOM_FEED)
    assert parsed.title == "Indie Games" and parsed.stories[0].link == "https://indie/frogs"
    assert parsed.stories[0].summary.startswith("It's about frogs")


def test_a_summary_too_short_to_say_anything_gives_way_to_the_articles_start():
    assert told("Read more", FULL).startswith("The whole article")
    assert told("A summary long enough to say what happened, who did it, and why anyone should care.", FULL).startswith(
        "A summary"
    )
    feed = rss_with_content("PC", item("A game", "https://p/1", "Short.", content=FULL))
    (story,) = parse("PC", feed).stories
    assert story.summary.startswith("The whole article") and len(story.summary) == 300


def test_quality_says_how_fresh_how_often_and_whether_items_say_anything():
    feed = rss(
        "Thin",
        item("One", "https://x/1", "", hours_ago=2),
        item("Two", "https://x/2", "", hours_ago=26),
    )
    assert quality(parse("", feed), NOW) == (
        "2 items, newest 2 hours ago, about 2.0 a day, summaries of about 0 characters, 2 with no summary"
    )


def test_a_page_points_to_its_feeds():
    page = '<link rel="alternate" type="application/atom+xml" href="/atom.xml"><link rel="stylesheet" href="x.css">'
    assert feed_links(page, "https://site.example/blog/") == ["https://site.example/atom.xml"]


def test_preview_reads_a_feed_or_finds_it_from_its_site(feeds):
    said = ask(feeds, action="preview", url="games/feed")
    assert said.startswith("Eurogamer (https://games/feed): 1 items, newest 3 hours ago")
    assert "- [3 hours ago] A new Zelda: Nintendo announced" in said
    assert ask(feeds, action="preview", url="https://blog.example").startswith(
        "Eurogamer (https://blog.example/rss.xml)"
    )
    assert ask(feeds, action="preview", url="https://quiet.example").startswith("The Times of Israel (https://quiet")
    assert ask(feeds, action="preview", url="https://nothing.example") == (
        "error: no feed there: that page points to no RSS or Atom feed, and none is where feeds usually are"
    )


def test_following_keeps_the_feed_by_its_own_title_in_their_category(feeds):
    said = ask(feeds, action="follow", url="https://toi/feed", category="Israeli News")
    assert said.startswith("done: following The Times of Israel in israeli news (1 items")
    ask(feeds, action="follow", url="https://ynet/rss", category="israeli news", name="Ynet")
    ask(feeds, action="follow", url="https://games/feed", category="gaming")
    assert ask(feeds, action="list") == (
        "Their feeds, by category:\n- gaming: Eurogamer (https://games/feed)\n"
        "- israeli news: The Times of Israel (https://toi/feed); Ynet (https://ynet/rss)"
    )


def test_a_site_that_turns_tars_away_still_has_its_feed_found_and_kept_where_it_ends_up(feeds):
    assert ask(feeds, action="follow", url="moved.example", category="news").startswith(
        "done: following The Times of Israel in news"
    )
    assert feeds.followed("alon")[0]["url"] == "https://www.moved.example/feed/"


def test_following_one_again_moves_it(feeds):
    ask(feeds, action="follow", url="https://games/feed", category="news")
    assert ask(feeds, action="follow", url="https://games/feed", category="gaming").startswith(
        "done: following Eurogamer in gaming, moved from news"
    )
    assert len(feeds.followed("alon")) == 1


def test_whats_new_is_whats_recent_and_not_told_before(feeds):
    ask(feeds, action="follow", url="https://ynet/rss", category="israeli news")
    ask(feeds, action="follow", url="https://toi/feed", category="israeli news")
    said = ask(feeds, action="read", category="israeli news").splitlines()
    assert said[0] == "From their israeli news feeds (Ynet, The Times of Israel): 2 recent, 2 new to them."
    assert said[1] == "- [Ynet, 1 hours ago] Storm floods the Sharon: Record rain fell in Kadima-Tzoran. (https://y/1)"
    assert said[2].startswith("- [The Times of Israel, 2 hours ago] Knesset convenes")
    again = ask(feeds, action="read", category="israeli news")
    assert "0 new to them" in again and "Nothing new since they last asked" in again
    assert "Storm floods" in ask(feeds, action="read", category="israeli news", everything=True)


def test_each_person_has_their_own_feeds_and_their_own_news(feeds):
    ask(feeds, action="follow", url="https://ynet/rss", category="news")
    ask(feeds, speaker="Stacey", action="follow", url="https://ynet/rss", category="news")
    ask(feeds, action="read")
    assert "1 new to them" in ask(feeds, speaker="Stacey", action="read")  # Alon's reading isn't hers
    ask(feeds, speaker="Stacey", action="unfollow", name="ynet")
    assert ask(feeds, speaker="Stacey", action="list") == "They follow no feeds yet."
    assert "Ynet" in ask(feeds, action="list")


def test_an_unknown_voice_is_the_default_persons_and_without_one_nobodys(feeds, tmp_path):
    ask(feeds, action="follow", url="https://ynet/rss", category="news")
    assert "Ynet" in ask(feeds, speaker=None, action="list")
    nobody = Feeds(Store(tmp_path / "db2"), get=get, clock=lambda: NOW)
    assert ask(nobody, speaker=None, action="list").startswith("error: TARS doesn't know whose feeds")
    assert ask(nobody, speaker=None, action="preview", url="https://games/feed").startswith("Eurogamer")


def test_unfollowing_by_address_or_a_whole_category(feeds):
    for url, category in [("https://ynet/rss", "news"), ("https://toi/feed", "news"), ("https://games/feed", "gaming")]:
        ask(feeds, action="follow", url=url, category=category)
    assert ask(feeds, action="unfollow", url="toi/feed") == "done: no longer following The Times of Israel"
    assert ask(feeds, action="unfollow", category="news") == "done: no longer following Ynet"
    assert ask(feeds, action="unfollow", name="nothing").startswith("error: they follow no such feed")
    assert ask(feeds, action="unfollow").startswith("error: they follow no such feed")  # never all at once
    assert [f["name"] for f in feeds.followed("alon")] == ["Eurogamer"]


def test_a_feed_that_doesnt_answer_is_said_and_the_rest_go_on(feeds):
    ask(feeds, action="follow", url="https://ynet/rss", category="news")
    feeds.store.write("INSERT INTO feeds (person, category, name, url, added) VALUES ('alon', 'news', 'Gone', "
                      "'https://gone.example/feed', 0)")  # fmt: skip
    said = ask(feeds, action="read", category="news")
    assert "Didn't answer: Gone (the site answered 404)." in said and "Storm floods" in said


def test_reading_a_category_they_dont_have_names_theirs(feeds):
    assert ask(feeds, action="read").startswith("They follow no feeds yet")
    ask(feeds, action="follow", url="https://games/feed", category="gaming")
    assert ask(feeds, action="read", category="cooking") == "error: they have no category 'cooking'; theirs are: gaming"


def test_follow_needs_a_feed_and_a_category(feeds):
    assert ask(feeds, action="follow", url="https://games/feed").startswith("error: follow needs")
    assert ask(feeds, action="follow", url="https://refused.example/x", category="x").startswith(
        "error: couldn't follow it: that page points to no RSS"
    )


def test_the_brain_has_the_feeds_tool_and_its_rules(feeds):
    ask(feeds, action="follow", url="https://ynet/rss", category="news")
    client = fake_openai_chat(
        lambda m: "A storm flooded the Sharon." if m[-1].get("type") == "function_call_output" else "",
        lambda m: [("feeds", {"action": "read", "category": "news"})] if m[-1].get("role") == "user" else [],
    )
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False), feeds=feeds)
    assert "".join(brain.stream_reply("what's new?", Request(speaker="Alon"))) == "A storm flooded the Sharon."
    assert any(t.get("name") == "feeds" for t in client.requests[0]["tools"])
    assert "Feeds: each person follows RSS feeds" in client.requests[0]["instructions"]
    assert "Storm floods the Sharon" in client.requests[1]["input"][-1]["output"]


BOMB = (
    b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
    b"<rss><channel><title>&b;</title></channel></rss>"
)
EXTERNAL = (
    b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><title>&x;'
    b"</title></channel></rss>"
)


@pytest.mark.parametrize("body", [BOMB, EXTERNAL])
def test_a_feed_with_entities_is_refused_not_expanded(body):
    with pytest.raises(NotAFeed, match="not a feed"):
        parse("https://example.com/feed", body)
