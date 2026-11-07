"""Feeds: the RSS and Atom feeds each person follows, by category, and what's new in them when they ask.

Everything about them is done by talking. "I want to follow gaming": the model looks for the topic's feeds on the web
and previews each before suggesting it, since a feed is only worth following if it's live and its items carry a
summary of their own (a headline and a link to a paywalled article tells nothing). They pick, and it follows those,
in a category in their words. "What's new in gaming?" reads that category's feeds, all at once, and hands the model
what it hasn't handed them before, for it to tell the few that matter. Each person's feeds are their own (the
event database's feeds table), and so is what they've been told (feed_seen): who's asking is the voice TARS knows,
or the chat, else [feeds] default_person."""

import html
import re
import statistics
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import NamedTuple
from urllib.parse import urljoin

import httpx

from .events import person_key
from .store import Store

FETCH_TIMEOUT_S = 8.0
CACHE_S = 180.0  # a feed read within this long is read again from memory
RECENT_S = 3 * 24 * 3600.0  # older stories are left out of what's new
SEEN_KEEP_S = 30 * 24 * 3600.0
PER_FEED = 10  # the newest of each feed, so one busy feed doesn't drown the rest
MOST_STORIES = 30  # handed to the model at once, newest first
PREVIEWED = 5
SUMMARY_CHARS = 300
SHORT_SUMMARY = 80  # characters: shorter, and the article's start says more, when the feed has it
USER_AGENT = "Mozilla/5.0 (TARS home assistant; feeds)"
ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
USUAL_PATHS = ("/feed", "/feed/", "/rss", "/rss/", "/feed.xml", "/rss.xml", "/rss/index.xml", "/atom.xml", "/index.xml")
LIST, READ, PREVIEW, FOLLOW, UNFOLLOW = "list", "read", "preview", "follow", "unfollow"
ACTIONS = (LIST, READ, PREVIEW, FOLLOW, UNFOLLOW)

FEEDS_TOOL = {
    "type": "function",
    "name": "feeds",
    "description": "The RSS feeds the person asking follows, by category: what's new in them, and which they follow.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "category", "url", "name", "everything"],
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": "list: their categories and feeds. read: what's new in a category's feeds (null: in "
                "all of them). preview: a feed's latest items and how good it is, by its address or its site's, "
                "before following it. follow: add a feed to a category. unfollow: drop a feed, by name or address, "
                "or a whole category, by category alone.",
            },
            "category": {"type": ["string", "null"], "description": "read, follow, unfollow: in their words."},
            "url": {"type": ["string", "null"], "description": "preview, follow, unfollow: the feed's or its site's."},
            "name": {"type": ["string", "null"], "description": "follow: what to call it; unfollow: which, by name."},
            "everything": {
                "type": ["boolean", "null"],
                "description": "read: true for every recent story, told before or not; false or null: only new ones.",
            },
        },
    },
}
CAN_FEEDS = (
    "follow RSS feeds for each person, by category, find good ones on a topic, and tell what's new in them, with the "
    "feeds tool"
)
NEEDS_FEEDS = "the news or what's new in a topic they follow, finding feeds on a topic, or changing which they follow"
RULES = (
    "Feeds: each person follows RSS feeds, in categories of their own, with the feeds tool.\n"
    '- What\'s new ("what\'s new?", "the news", "anything new in gaming?"): read that category (null: all of them). '
    "Pick the few stories that matter most to them; the same story in several feeds is a bigger one, told once. Tell "
    "each in a sentence or two, in your own words, from its summary, never reading out headlines. Aloud, three or "
    "four; in a chat, up to six, each with its link. Then offer more, or more about one: for more than its summary "
    "says, search the web. Nothing new since they last asked: say so, and offer the latest again (everything true).\n"
    '- Finding feeds ("I want to follow gaming", "any good feeds about cooking?"): search the web for the topic\'s '
    "best RSS feeds, and add those you know, then preview each before suggesting it. Suggest only feeds that answer, "
    "were updated lately and whose items carry real summaries, three or four, each with what it's good for in a few "
    "words; say why you left out any they named. Follow only the ones they pick, into the category they say, or the "
    "topic's name. A site they name: preview it, which finds its feed.\n"
    '- "What\'s the latest from <a feed>?": preview it, and tell a few of its items.\n'
    "- Unfollowing a whole category: ask first."
)


@dataclass
class Story:
    source: str
    title: str
    summary: str
    link: str
    published: float | None  # Unix time, when the feed says


@dataclass
class Parsed:
    title: str  # the feed's own
    stories: list[Story]
    full_text: int  # items whose whole article is in the feed


class NotAFeed(Exception):
    """The address answered, with something other than a feed, and no feed it points to."""


def text(raw: str | None) -> str:
    """Plain words from a feed's field, which may be HTML or escaped HTML."""
    raw = html.unescape(raw or "")
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", raw)).split())


def when(raw: str | None) -> float | None:
    """An RSS date (RFC 822) or an Atom one (ISO 8601), as Unix time; a date with no zone is UTC."""
    raw = text(raw)
    if not raw:
        return None
    try:
        at = parsedate_to_datetime(raw)
    except TypeError, ValueError:
        try:
            at = datetime.fromisoformat(raw)
        except ValueError:
            return None
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).timestamp()


def parse(source: str, body: bytes) -> Parsed:
    """An RSS or Atom feed's stories, newest first. NotAFeed for anything else."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise NotAFeed(f"not a feed ({e})") from None
    stories, full = [], 0
    if root.tag.startswith(ATOM):
        title = text(root.findtext(f"{ATOM}title"))
        for entry in root.iter(f"{ATOM}entry"):
            links = entry.findall(f"{ATOM}link")
            link = next((x.get("href") for x in links if x.get("rel", "alternate") == "alternate"), None)
            summary = told(entry.findtext(f"{ATOM}summary"), entry.findtext(f"{ATOM}content"))
            full += len(text(entry.findtext(f"{ATOM}content"))) > 1000
            published = when(entry.findtext(f"{ATOM}published") or entry.findtext(f"{ATOM}updated"))
            stories.append(Story(source, text(entry.findtext(f"{ATOM}title")), summary, link or "", published))
    elif root.find("channel") is not None:
        title = text(root.findtext("channel/title"))
        for item in root.iter("item"):
            summary = told(item.findtext("description"), item.findtext(CONTENT))
            full += len(text(item.findtext(CONTENT))) > 1000
            link = text(item.findtext("link")) or text(item.findtext("guid"))
            headline, published = text(item.findtext("title")), when(item.findtext("pubDate"))
            stories.append(Story(source, headline, summary, link, published))
    else:
        raise NotAFeed("not a feed: neither RSS nor Atom")
    for s in stories:
        s.summary = s.summary[:SUMMARY_CHARS]
    stories = [s for s in stories if s.title and s.link]
    stories.sort(key=lambda s: -(s.published or 0))
    return Parsed(title, stories, full)


def told(summary: str | None, article: str | None) -> str:
    """What a story says: its summary, or, when that's too short to tell anything, the start of its article."""
    summary = text(summary)
    return summary if len(summary) >= SHORT_SUMMARY else text(article) or summary


def feed_links(page: str, base: str) -> list[str]:
    """The feeds a web page points to: its <link rel="alternate"> to RSS or Atom."""
    found = []
    for tag in re.findall(r"<link\b[^>]*>", page, re.IGNORECASE):
        attrs = dict(re.findall(r'([\w-]+)\s*=\s*["\']([^"\']*)["\']', tag))
        attrs = {k.lower(): v for k, v in attrs.items()}
        if "alternate" in attrs.get("rel", "").lower() and re.search(r"(rss|atom)\+xml", attrs.get("type", "")):
            found.append(urljoin(base, html.unescape(attrs.get("href", ""))))
    return [f for f in dict.fromkeys(found) if f]


def quality(parsed: Parsed, now: float) -> str:
    """What makes a feed worth following, in a line: how fresh, how often, and whether its items say anything."""
    stories = parsed.stories
    if not stories:
        return "it has no items"
    lengths = [len(s.summary) for s in stories]
    empty = sum(n < 40 for n in lengths)
    dated = [s.published for s in stories if s.published]
    parts = [f"{len(stories)} items"]
    if dated:
        parts.append(f"newest {age(now - max(dated))}")
        span_days = max((max(dated) - min(dated)) / 86400, 1 / 24)
        if len(dated) > 1:
            parts.append(f"about {len(dated) / span_days:.1f} a day")
    parts.append(f"summaries of about {statistics.median(lengths):.0f} characters")
    if empty:
        parts.append(f"{empty} with no summary")
    if parsed.full_text:
        parts.append(f"{parsed.full_text} with the whole article")
    return ", ".join(parts)


def age(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(seconds, 0) / 60:.0f} minutes ago"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.0f} hours ago"
    return f"{seconds / 86400:.0f} days ago"


class Page(NamedTuple):
    body: bytes
    kind: str  # its content type
    url: str  # where it was in the end, after any redirects: the address to keep


def fetch(url: str) -> Page:
    """The body at `url`, and its content type."""
    response = httpx.get(url, timeout=FETCH_TIMEOUT_S, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return Page(response.content, response.headers.get("content-type", ""), str(response.url))


class Feeds:
    """The brain's feeds tool: tool, rules, can and needs, as the browser's errands give them (llm.py)."""

    tool, rules, can, needs = FEEDS_TOOL, RULES, CAN_FEEDS, NEEDS_FEEDS

    def __init__(
        self,
        store: Store,
        default_person: str = "",
        get: Callable[[str], Page] = fetch,
        clock: Callable[[], float] = time.time,
    ):
        self.store, self.default_person = store, default_person.strip()
        self._get, self._clock = get, clock
        self._cache: dict[str, tuple[float, Page]] = {}

    def call(self, args: dict, request=None) -> str:
        args = args if isinstance(args, dict) else {}
        action = args.get("action")
        person = person_key(getattr(request, "speaker", None) or self.default_person)
        if action == PREVIEW:
            return self.preview(clean(args.get("url")))
        if person is None:
            return (
                "error: TARS doesn't know whose feeds these are (the voice wasn't recognized, and [feeds] has no "
                "default_person): say so"
            )
        category, url, name = clean(args.get("category")).lower(), clean(args.get("url")), clean(args.get("name"))
        if action == LIST:
            return self.listing(person)
        if action == READ:
            return self.read(person, category, bool(args.get("everything")))
        if action == FOLLOW:
            return self.follow(person, category, url, name)
        if action == UNFOLLOW:
            return self.unfollow(person, category, url, name)
        return f"error: action is one of {', '.join(ACTIONS)}"

    # Following.

    def followed(self, person: str, category: str = "") -> list[dict]:
        where, args = ("AND category=?", (person, category)) if category else ("", (person,))
        return self.store.rows(f"SELECT * FROM feeds WHERE person=? {where} ORDER BY category, id", args)

    def listing(self, person: str) -> str:
        feeds = self.followed(person)
        if not feeds:
            return "They follow no feeds yet."
        by: dict[str, list[str]] = {}
        for f in feeds:
            by.setdefault(f["category"], []).append(f"{f['name']} ({f['url']})")
        return "Their feeds, by category:\n" + "\n".join(f"- {c}: {'; '.join(names)}" for c, names in by.items())

    def follow(self, person: str, category: str, url: str, name: str) -> str:
        if not url or not category:
            return "error: follow needs the feed's address (or its site's) and a category"
        try:
            feed_url, parsed = self.find(url)
        except Exception as e:  # noqa: BLE001 - said, for the model to tell them
            return f"error: couldn't follow it: {why(e)}"
        name = name or parsed.title or feed_url
        with self.store.transaction() as db:
            had = db.execute("SELECT category FROM feeds WHERE person=? AND url=?", (person, feed_url)).fetchone()
            if had:
                db.execute(
                    "UPDATE feeds SET category=?, name=? WHERE person=? AND url=?", (category, name, person, feed_url)
                )
            else:
                db.execute(
                    "INSERT INTO feeds (person, category, name, url, added) VALUES (?, ?, ?, ?, ?)",
                    (person, category, name, feed_url, self._clock()),
                )
        moved = f", moved from {had['category']}" if had and had["category"] != category else ""
        return f"done: following {name} in {category}{moved} ({quality(parsed, self._clock())})"

    def unfollow(self, person: str, category: str, url: str, name: str) -> str:
        feeds = self.followed(person, category)
        if url or name:
            gone = [f for f in feeds if same_feed(f, url, name)]
        else:
            gone = feeds if category else []
        if not gone:
            return "error: they follow no such feed; list shows the ones they do"
        with self.store.transaction() as db:
            db.executemany("DELETE FROM feeds WHERE id=?", [(f["id"],) for f in gone])
        return f"done: no longer following {', '.join(f['name'] for f in gone)}"

    # Reading.

    def read(self, person: str, category: str, everything: bool) -> str:
        feeds = self.followed(person, category)
        if not feeds:
            known = sorted({f["category"] for f in self.followed(person)})
            if category and known:
                return f"error: they have no category {category!r}; theirs are: {', '.join(known)}"
            return "They follow no feeds yet: offer to find some on a topic they like."
        urls = list(dict.fromkeys(f["url"] for f in feeds))
        with ThreadPoolExecutor(max_workers=min(len(urls), 16)) as pool:
            got = dict(zip(urls, pool.map(self._try, urls), strict=True))
        now, found, failed = self._clock(), [], []
        for f in feeds:
            body = got[f["url"]]
            try:
                if isinstance(body, Exception):
                    raise body
                recent = [s for s in parse(f["name"], body).stories if not s.published or now - s.published < RECENT_S]
                found += recent[:PER_FEED]
            except Exception as e:  # noqa: BLE001 - one feed down costs its stories, not the rest
                failed.append(f"{f['name']} ({why(e)})")
        seen = {r["link"] for r in self.store.rows("SELECT link FROM feed_seen WHERE person=?", (person,))}
        new = [s for s in found if s.link not in seen]
        shown = sorted(found if everything else new, key=lambda s: -(s.published or 0))[:MOST_STORIES]
        self._saw(person, [s.link for s in shown])
        where = f"their {category} feeds" if category else "all their feeds"
        lines = [f"From {where} ({', '.join(f['name'] for f in feeds)}): {len(found)} recent, {len(new)} new to them."]
        if failed:
            lines.append(f"Didn't answer: {'; '.join(failed)}.")
        if not shown:
            lines.append("Nothing new since they last asked: read again with everything true for the latest.")
        lines += [f"- [{s.source}, {dated(s, now)}] {s.title}: {s.summary} ({s.link})" for s in shown]
        return "\n".join(lines)

    def preview(self, url: str) -> str:
        if not url:
            return "error: preview needs the feed's address, or its site's"
        try:
            feed_url, parsed = self.find(url)
        except Exception as e:  # noqa: BLE001 - said, for the model to tell them
            return f"error: no feed there: {why(e)}"
        now = self._clock()
        lines = [f"{parsed.title or feed_url} ({feed_url}): {quality(parsed, now)}. Its latest:"]
        lines += [f"- [{dated(s, now)}] {s.title}: {s.summary} ({s.link})" for s in parsed.stories[:PREVIEWED]]
        return "\n".join(lines)

    def find(self, url: str) -> tuple[str, Parsed]:
        """The feed at `url`, or the one the page there points to: its address, read."""
        url = url if re.match(r"https?://", url) else f"https://{url}"
        try:
            page = self._read(url)
        except httpx.HTTPStatusError:  # a site that turns away TARS's requests may still serve its feed
            page = Page(b"", "text/html", url)
        if page.body:
            try:
                return page.url, parse("", page.body)
            except NotAFeed:
                start = page.body.lstrip()[:200].lower()
                if "html" not in page.kind and not start.startswith((b"<!doctype html", b"<html")):
                    raise
        for link in feed_links(page.body.decode("utf-8", "replace"), page.url):
            if found := self._try_feed(link):
                return found
        # Many sites have one where feeds usually are, without saying so on the page.
        guesses = [urljoin(page.url, path) for path in USUAL_PATHS]
        with ThreadPoolExecutor(max_workers=len(guesses)) as pool:
            for found in pool.map(self._try_feed, guesses):
                if found:
                    return found
        raise NotAFeed("that page points to no RSS or Atom feed, and none is where feeds usually are")

    def _try_feed(self, url: str) -> tuple[str, Parsed] | None:
        """The feed at `url`, by where it was in the end, and read; None if there's none there."""
        try:
            page = self._read(url)
            return page.url, parse("", page.body)
        except Exception:  # noqa: BLE001 - not there
            return None

    def _saw(self, person: str, links: list[str]) -> None:
        now = self._clock()
        with self.store.transaction() as db:
            db.executemany(
                "INSERT OR IGNORE INTO feed_seen (person, link, at) VALUES (?, ?, ?)", [(person, k, now) for k in links]
            )
            db.execute("DELETE FROM feed_seen WHERE at < ?", (now - SEEN_KEEP_S,))

    def _try(self, url: str) -> bytes | Exception:
        try:
            return self._read(url).body
        except Exception as e:  # noqa: BLE001 - said as that feed not answering
            return e

    def _read(self, url: str) -> Page:
        cached = self._cache.get(url)
        if cached and self._clock() - cached[0] < CACHE_S:
            return cached[1]
        page = self._get(url)
        self._cache[url] = (self._clock(), page)
        return page


def dated(story: Story, now: float) -> str:
    return age(now - story.published) if story.published else "undated"


def same_feed(feed: dict, url: str, name: str) -> bool:
    """Whether a followed feed is the one meant: by its address (or part of it), or its name."""
    return bool((url and url.rstrip("/") in feed["url"]) or (name and name.lower() == feed["name"].lower()))


def clean(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def why(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"the site answered {e.response.status_code}"
    if isinstance(e, httpx.HTTPError):
        return "the site didn't answer"
    return str(e) or type(e).__name__


def make(cfg, events) -> Feeds | None:
    """The feeds tool, in the event database; None without one ([learning] log_events off)."""
    if events is None:
        print("(feeds: off, since they're kept in the event database and [learning] log_events is off)")
        return None
    return Feeds(events.store, cfg.default_person)
