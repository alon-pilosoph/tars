"""Reading pages: the words of the page a link goes to, for "tell me more about that one" or a link sent in a chat.

The quick way first: one plain request, as TARS, and the article's words from the page itself: its structured data
(articleBody), else the paragraphs in its <article>. A site that turns that away, or whose words a script fills in,
is read in TARS's browser instead: an errand to read it is started by this code itself, through the brain's browse
tool and its guards (browser/errands.py), before anything of the page reaches the model, and its answer comes to
their chat a few minutes later.
A page that says it's for subscribers (isAccessibleForFree false, with only a teaser to show) is asked about once:
do they subscribe? Yes, and the browser reads it signed in, with the vault's login, or the one-time page for one.
No, and the web is searched for the story instead.

What worked is remembered, so the second time goes straight to it: for each site, the household's, how its pages
are read and whether it's for subscribers (page_sites); for each person, whether they subscribe, yes or no
(page_subscriptions), so nobody is asked twice. What's learned of a site is tried the quick way again after thirty
days. Never the home network: an address there, or one a redirect goes to, isn't read (homenet.py). What it reads
is anyone's words: the reply that's read a page only reads the house after (toolbox.Tool.outside)."""

import contextlib
import html
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from .events import person_key
from .feeds import FETCH_TIMEOUT_S, USER_AGENT, Page, clean, why
from .homenet import at_home, host_of
from .store import Store
from .toolbox import ITS_OWN, Toolset, held_back, one_tool

QUICK, BROWSER = "quick", "browser"  # how a site's pages are read
RELEARN_S = 30 * 24 * 3600.0  # what's learned of a site is tried the quick way again after this long
MOST_CHARS = 6000  # of a page's words, handed to the model
TOO_LITTLE = 300  # characters: fewer, and the page's words are put there by a script, or aren't there
TEASER = 1500  # characters: a page for subscribers with fewer is only showing its teaser
REDIRECTS = 5
TURNED_AWAY = (401, 403, 429, 451)  # a site refusing TARS's request, not a page that isn't there
# Never a page's words. Not <form>: an ASP.NET page (Globes) is one from top to bottom.
SKIPPED = {"script", "style", "noscript", "nav", "header", "footer", "aside", "figure", "svg", "button"}
READ, SUBSCRIBED, KNOWN = "read", "subscribed", "known"
ACTIONS = (READ, SUBSCRIBED, KNOWN)

PAGES_TOOL = {
    "type": "function",
    "name": "read_page",
    "description": "Read the page a link goes to (a story from a feed, a link someone sent), and what TARS has "
    "learned about reading a site: whether it's for subscribers, and whether the person asking subscribes.",
    "strict": True,
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "url", "subscribes"],
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": "read: the page's words. subscribed: what they said about subscribing to a site. "
                "known: what TARS knows about reading a site (null url: every site it knows).",
            },
            "url": {"type": ["string", "null"], "description": "read: the link; subscribed, known: the site's."},
            "subscribes": {"type": ["boolean", "null"], "description": "subscribed: whether they do."},
        },
    },
}
CAN_PAGES = "read the page a link goes to, with the read_page tool, signed in to a site they subscribe to"
NEEDS_PAGES = "more about a story than its summary says, or what the page at a link says"
RULES = (
    "Pages: read_page reads the page a link goes to: a story from the feeds they want more of, or a link they sent. "
    "Tell what it says in your own words, as much as they asked for. When it can't be read here, it says what was "
    "done instead (TARS's browser sent to read it, its answer in their chat later) or what to do: say or do that, "
    "and never guess at what a page says.\n"
    "- A site for subscribers: when it says to, ask once whether they subscribe to it, and save the answer with "
    "subscribed, yes or no, before anything else. Saying they've cancelled, or that they've subscribed, is saved "
    'the same way. "How do you read Haaretz?": known.'
)


@dataclass
class Article:
    title: str
    words: str
    paywalled: bool  # the page says it's for subscribers


class _Words(HTMLParser):
    """A page's paragraphs and headings, without its menus, scripts and the rest; those in its <article> if it has
    one; and all of its <article>'s words, for a page that writes them without paragraphs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.everywhere: list[str] = []
        self.in_article: list[str] = []
        self.article_text: list[str] = []
        self.title = ""
        self._skip = self._article = 0
        self._block: list[str] | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIPPED:
            self._skip += 1
        elif tag == "article":
            self._article += 1
        elif tag in ("p", "h1", "h2", "h3", "li", "blockquote") and not self._skip:
            self._end_block()
            self._block = []
        elif tag == "title":
            self._in_title = True
        elif tag == "br":
            self.handle_data(" ")

    def handle_endtag(self, tag):
        if tag in SKIPPED:
            self._skip = max(self._skip - 1, 0)
        elif tag == "article":
            self._end_block()
            self._article = max(self._article - 1, 0)
        elif tag in ("p", "h1", "h2", "h3", "li", "blockquote"):
            self._end_block()
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._article and not self._skip and data.strip():
            self.article_text.append(data.strip())
        if self._block is not None and not self._skip:
            self._block.append(data)

    def _end_block(self):
        if self._block is not None and (line := " ".join("".join(self._block).split())):
            self.everywhere.append(line)
            if self._article:
                self.in_article.append(line)
        self._block = None


def article(page: str) -> Article:
    """What a page says: its structured data's articleBody if it has one, else its paragraphs (in its <article> if it
    has one), else all its <article>'s words; and whether it says it's for subscribers. The structured data comes
    first: a page's paragraphs often have other stories' in them, in a box of related ones."""
    body, paywalled, headline = "", False, ""
    for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(html.unescape(block.strip()), strict=False)
        except ValueError:
            continue
        for item in _items(data):
            if isinstance(text := item.get("articleBody"), str) and len(text) > len(body):
                body = text
            if str(item.get("isAccessibleForFree", "")).lower() == "false":
                paywalled = True
            if not headline and isinstance(item.get("headline"), str):
                headline = item["headline"]
    words = _Words()
    with contextlib.suppress(Exception):  # a page too broken to read for paragraphs still has its structured data
        words.feed(page)
        words.close()
    body = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())
    paragraphs = "\n".join(words.in_article or words.everywhere)
    if len(paragraphs) < TOO_LITTLE and len(text := " ".join(" ".join(words.article_text).split())) > len(paragraphs):
        paragraphs = text
    title = " ".join((headline or words.title).split())
    return Article(title, body if len(body) >= TOO_LITTLE or not paragraphs else paragraphs, paywalled)


def _items(data) -> list[dict]:
    """Every object in a page's structured data, however it's nested (@graph, lists, parts)."""
    found, stack = [], [data]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            found.append(item)
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
    return found


def site_of(url: str) -> str:
    """The site a link is on, as it's remembered: its host, without www."""
    host = host_of(url if re.match(r"https?://", url) else f"https://{url}")
    return host.removeprefix("www.")


class Refused(Exception):
    """An address TARS doesn't read: on the home network, or not a web page's."""


def fetch(url: str, home: frozenset[str] = frozenset(), transport: httpx.BaseTransport | None = None) -> Page:
    """The page at `url`, following redirects one at a time, so that none goes to the home network. `transport`:
    the tests' own."""
    with httpx.Client(timeout=FETCH_TIMEOUT_S, headers={"User-Agent": USER_AGENT}, transport=transport) as client:
        for _ in range(REDIRECTS + 1):
            if not re.match(r"https?://", url) or at_home(url, home):
                raise Refused("that address is on the home network, or isn't a web page's")
            response = client.get(url)
            if response.is_redirect and (to := response.headers.get("location")):
                url = urljoin(url, to)
                continue
            response.raise_for_status()
            return Page(response.content, response.headers.get("content-type", ""), str(response.url))
    raise Refused("too many redirects")


class Pages:
    """The brain's read_page tool. `browse`: the brain's browse tool (browser/errands.py's BrowseTool), for the pages
    the quick way can't read; None where there's no browser."""

    tool, rules, can, needs = PAGES_TOOL, RULES, CAN_PAGES, NEEDS_PAGES

    def __init__(
        self,
        store: Store,
        browse=None,
        get: Callable[[str], Page] | None = None,
        clock: Callable[[], float] = time.time,
        home: frozenset[str] = frozenset(),
    ):
        self.store, self.browse, self._clock = store, browse, clock
        self._get = get or (lambda url: fetch(url, home))

    def toolset(self) -> Toolset:
        """The read_page tool for the brain (toolbox.py). Reading goes through at once, and what it reads is from
        outside: the reply only reads the house after. An errand to read it in the browser is started through the
        browse tool, held back as it holds back any (toolbox.held_back); a subscription saved is held back too."""
        return one_tool(self, ITS_OWN, "error: couldn't read the page ({e!r})", outside=True)

    def call(self, args: dict, request=None) -> str:
        args = args if isinstance(args, dict) else {}
        action, url = args.get("action"), clean(args.get("url"))
        person = person_key(getattr(request, "speaker", None) or "")
        if action == READ:
            return self.read(person, url, request) if url else "error: read needs the link"
        if action == SUBSCRIBED:
            if refused := held_back(request):
                return refused
            return self.subscribed(person, url, args.get("subscribes"))
        if action == KNOWN:
            return self.known(person, url)
        return f"error: action is one of {', '.join(ACTIONS)}"

    # Reading.

    def read(self, person: str | None, url: str, request=None) -> str:
        url = url if re.match(r"https?://", url) else f"https://{url}"
        site = site_of(url)
        known = self._site(site)
        if known and known["way"] == BROWSER:
            return self._in_browser(url, site, "its pages are read in the browser", request)
        try:
            page = self._get(url)
        except Refused as e:
            return f"error: TARS doesn't read that address: {e}"
        except httpx.HTTPStatusError as e:
            if e.response.status_code in TURNED_AWAY:
                self._learn(site, BROWSER, False)
                return self._in_browser(url, site, f"the site turns away a plain request ({why(e)})", request)
            return f"error: couldn't read it: {why(e)}"
        except Exception as e:  # noqa: BLE001 - said, for the model to tell them; nothing learned from a hiccup
            return f"error: couldn't read it: {why(e)}"
        found = article(page.body.decode("utf-8", "replace"))
        if found.paywalled and len(found.words) < TEASER:
            self._learn(site, QUICK, True)
            return self._for_subscribers(person, site, url, request, found)  # its free pages are still read quick
        if len(found.words) < TOO_LITTLE:
            self._learn(site, BROWSER, False)
            why_ = "its words aren't in the page as it comes (a script puts them there)"
            return self._in_browser(url, site, why_, request)
        self._learn(site, QUICK, False)
        words = found.words[:MOST_CHARS] + (" […]" if len(found.words) > MOST_CHARS else "")
        return f"{found.title or url} ({url}):\n{words}"

    def _for_subscribers(self, person: str | None, site: str, url: str, request, found: Article | None = None) -> str:
        teaser = f' All it shows is: "{found.words[:400]}"' if found and found.words else ""
        if not person:
            return (f"{site} is for subscribers, and TARS doesn't know who's asking, so not whose subscription: say "
                    f"so, and search the web for the story instead.{teaser}")  # fmt: skip
        subscribes = self._subscribes(person, site)
        if subscribes is None:
            return (f"{site} is for subscribers.{teaser} Ask them, briefly, whether they subscribe to it, and save "
                    "the answer with subscribed; then, if they do, read it again.")  # fmt: skip
        if not subscribes:
            return f"{site} is for subscribers, and they don't subscribe: search the web for the story.{teaser}"
        return self._in_browser(url, site, "it's for subscribers, and they subscribe", request, signed_in=True)

    def _in_browser(self, url: str, site: str, why_: str, request, signed_in: bool = False) -> str:
        """An errand to read it, started here through the browse tool (and its guards), not by the model: nothing of
        the page has reached the model yet."""
        if self.browse is None:
            return f"Can't read it here: {why_}, and TARS's browser isn't set up here. Search the web for the story."
        as_them = f", signed in to my {site} subscription (sign in if it asks)" if signed_in else ""
        started = self.browse.call({"errand": f"Read {url}{as_them}, and tell me what it says."}, request)
        return f"Reading it in TARS's browser, since {why_}. The browse tool says: {started}"

    # What's learned.

    def subscribed(self, person: str | None, url: str, subscribes) -> str:
        if not person:
            return "error: TARS doesn't know who's asking, so not whose subscription this is: say so"
        if not url or not isinstance(subscribes, bool):
            return "error: subscribed needs the site and whether they subscribe"
        site = site_of(url)
        self.store.write(
            "INSERT INTO page_subscriptions (person, site, subscribes, at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(person, site) DO UPDATE SET subscribes=excluded.subscribes, at=excluded.at",
            (person, site, int(subscribes), self._clock()),
        )
        return f"done: they {'subscribe' if subscribes else 'do not subscribe'} to {site}"

    def known(self, person: str | None, url: str) -> str:
        where, args = ("WHERE site=?", (site_of(url),)) if url else ("", ())
        sites = self.store.rows(f"SELECT * FROM page_sites {where} ORDER BY site", args)
        mine = {r["site"]: bool(r["subscribes"]) for r in self.store.rows(
            "SELECT site, subscribes FROM page_subscriptions WHERE person=?", (person or "",))}  # fmt: skip
        lines = []
        for s in sites:
            way = "in the browser" if s["way"] == BROWSER else "with a plain request"
            line = f"- {s['site']}: read {way}"
            if s["paywalled"]:
                line += ", some of its pages for subscribers"
                if s["site"] in mine:
                    line += f"; they {'subscribe' if mine[s['site']] else 'do not subscribe'}"
            lines.append(f"{line} (learned {_days(self._clock() - s['learned'])})")
        if not lines:
            return "TARS hasn't read anything there yet." if url else "TARS hasn't read any site's pages yet."
        return "What TARS knows about reading sites:\n" + "\n".join(lines)

    def _site(self, site: str) -> dict | None:
        """What's learned of `site`, unless it's old enough to be tried the quick way again."""
        rows = self.store.rows("SELECT * FROM page_sites WHERE site=?", (site,))
        if not rows or self._clock() - rows[0]["learned"] > RELEARN_S:
            return None
        return rows[0]

    def _subscribes(self, person: str, site: str) -> bool | None:
        rows = self.store.rows("SELECT subscribes FROM page_subscriptions WHERE person=? AND site=?", (person, site))
        return bool(rows[0]["subscribes"]) if rows else None

    def _learn(self, site: str, way: str, paywalled: bool) -> None:
        """What a read showed of `site`. Some of its pages for subscribers stays known until it's learned afresh: a
        site with free pages and subscribers' (Globes) is still one to ask about."""
        paywalled = paywalled or bool((known := self._site(site)) and known["paywalled"])
        self.store.write(
            "INSERT INTO page_sites (site, way, paywalled, learned) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(site) DO UPDATE SET way=excluded.way, paywalled=excluded.paywalled, learned=excluded.learned",
            (site, way, int(paywalled), self._clock()),
        )


def _days(seconds: float) -> str:
    days = int(seconds // 86400)
    return "today" if days < 1 else f"{days} day{'s' * (days != 1)} ago"


def make(events, browse=None, home: frozenset[str] = frozenset()) -> Pages | None:
    """The read_page tool, in the event database, with the brain's `browse` tool if it has one; None without the
    database ([learning] log_events off)."""
    if events is None:
        print(
            "(pages: off, since what's learned of sites is kept in the event database and [learning] log_events is off)"
        )
        return None
    return Pages(events.store, browse=browse, home=home)
