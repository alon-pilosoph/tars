"""Wishes that come true (docs/wishes.md): something TARS was asked for and couldn't do, built by a coding agent once a
maintainer says yes, and told to whoever asked once it's installed.

1. A wish (myself.py) goes to the maintainers' chats with what would be posted to GitHub, and a Build it button.
2. A maintainer's tap opens an issue on [wishes] repo, labelled [wishes] label, which .github/workflows/wish.yml hands
   to Claude Code: it builds the wish on a branch of its own and opens a pull request. It has the repository, and of
   the house only the issue: no .env, no config.local.toml, no database.
3. follow(), every few minutes in the Telegram service, watches the pull request Claude Code opened (its app's, on
   this repository's own claude/issue-N- branch: nobody else's): CI green, the maintainers are told what it does; CI
   red, it's asked to fix it, [wishes] tries times at most; stuck for hours, or closed unmerged, the maintainers hear
   it's dropped.
4. Merged is installed by the updater (updater.py). Once the running checkout has it, the wish is granted and the one
   who asked is told, once: in their chat, or the next time they speak to TARS.

Held in code: only a maintainer's tap starts a build, at most [wishes] monthly in 30 days; what's posted is capped so
the offer always fits one Telegram message, so what a maintainer approves is all of what's posted; nothing here merges.
"""

import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import WishesConfig
from .store import Store

TAP = "wish:"  # what a Build it button sends back, before the wish's number
OFFERED, BUILDING, READY, MERGED, FAILED, GRANTED = "offered", "building", "ready", "merged", "failed", "granted"
FOLLOW_EVERY_S = 5 * 60.0
NO_PULL_AFTER_S = 3 * 3600.0  # a build with no pull request by then has failed
STALLED_AFTER_S = 3 * 3600.0  # nor CI finishing on its latest commit, nor a fix pushed after it was sent back
MONTH_S = 30 * 86400.0
PASSED = {"success", "neutral", "skipped"}
API = "https://api.github.com"
BOT = "claude[bot]"  # who Claude Code's GitHub App opens pull requests as
# The most of what was asked for, and of what stopped TARS, that's posted: with them, the issue's body is at most about
# 1,700 characters, so the offer, which shows it whole, always fits one Telegram message (telegram.MAX_TEXT).
ASKED_MAX, WHY_MAX = 300, 1200
TITLE_MAX = 120


class GitHubError(Exception):
    pass


class GitHub:
    """The few calls of GitHub's REST API a wish needs, on [wishes] repo, with a fine-grained token for it alone:
    Issues read and write (the issue, a comment), Pull requests read-only, Metadata read-only; nothing else. Check runs
    have no fine-grained permission: they're read as a public repository's. The token never leaves this class. One
    client, kept for as long as TARS runs, and closed by close()."""

    def __init__(self, repo: str, token: str, transport: httpx.BaseTransport | None = None):
        self.repo = repo
        self._http = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=20,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kw) -> dict | list:
        try:
            r = self._http.request(method, path.format(repo=self.repo), **kw)
        except httpx.HTTPError as e:
            raise GitHubError(f"GitHub didn't answer: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                said = r.json().get("message", "")
            except ValueError, AttributeError:
                said = r.text[:200]
            raise GitHubError(f"GitHub said {r.status_code}: {said}")
        try:
            return r.json()
        except ValueError:
            raise GitHubError(f"GitHub's answer wasn't JSON ({r.status_code}): {r.text[:200]}") from None

    def open_issue(self, title: str, body: str, label: str) -> tuple[int, str]:
        made = self._expect(
            self._call("POST", "/repos/{repo}/issues", json={"title": title, "body": body, "labels": [label]}), dict
        )
        try:
            return int(made["number"]), str(made["html_url"])
        except KeyError, TypeError, ValueError:
            raise GitHubError("GitHub's answer had no issue in it") from None

    def ours(self, pull: dict, issue: int) -> bool:
        """Whether `pull` is Claude Code's build of `issue`: opened by its app, from this repository's own
        claude/issue-<issue>- branch. Anyone can open a pull request on a public repository, so nothing else counts."""
        head = pull.get("head") or {}
        return (
            (head.get("repo") or {}).get("full_name") == self.repo
            and str(head.get("ref", "")).startswith(f"claude/issue-{issue}-")
            and (pull.get("user") or {}).get("type") == "Bot"
            and (pull.get("user") or {}).get("login") == BOT
        )

    def pull_for(self, issue: int) -> dict | None:
        """The newest pull request Claude Code opened for `issue`, open or not."""
        pulls = self._expect(self._call("GET", "/repos/{repo}/pulls", params={"state": "all", "per_page": 50}), list)
        return next((p for p in pulls if isinstance(p, dict) and self.ours(p, issue)), None)

    def pull(self, number: int) -> dict:
        return self._expect(self._call("GET", f"/repos/{{repo}}/pulls/{number}"), dict)

    def checks(self, sha: str) -> tuple[str, list[str]]:
        """CI on a commit: ("pending" | "passed" | "failed", the names of the checks that failed)."""
        got = self._call("GET", f"/repos/{{repo}}/commits/{sha}/check-runs", params={"per_page": 100})
        runs = self._expect(self._expect(got, dict).get("check_runs"), list)
        if not runs or any(r["status"] != "completed" for r in runs):
            return "pending", []
        failed = sorted(r["name"] for r in runs if r["conclusion"] not in PASSED)
        return ("failed" if failed else "passed"), failed

    def comment(self, number: int, body: str) -> None:
        self._call("POST", f"/repos/{{repo}}/issues/{number}/comments", json={"body": body})

    @staticmethod
    def _expect(got, kind: type):
        if not isinstance(got, kind):
            raise GitHubError(f"GitHub's answer wasn't the {kind.__name__} expected")
        return got


@dataclass
class Told:
    """Where what happens to a wish is said: `maintainers(text, buttons)`, which returns the maintainers it reached,
    and `person(name, via, text)` for the one who asked."""

    maintainers: Callable[[str, list[tuple[str, str]]], list[str] | None]
    person: Callable[[str, str, str], None]


def issue(wish: dict) -> tuple[str, str]:
    """What's posted to GitHub for a wish: its title and body. Not who asked, nor the chat it came from; the model is
    told to write the wish without names too. What was asked for and what stopped TARS are cut at ASKED_MAX and
    WHY_MAX, so the offer can show all of it."""
    asked = _clip(wish["asked"].strip().rstrip("."), ASKED_MAX)
    why = _clip(wish["why"].strip().rstrip("."), WHY_MAX)
    title = asked[:1].upper() + asked[1:]
    body = (
        f"TARS was asked for this and couldn't do it.\n\n**Asked for:** {asked}.\n\n**What stopped it:** {why}.\n\n"
        f"Build it as [.github/wish.md](../blob/main/.github/wish.md) says. (TARS's wish {int(wish['id'])}.)"
    )
    return _clip(title, TITLE_MAX), body


def _clip(text: str, most: int) -> str:
    """`text`, or as much of it as fits in `most` characters with an ellipsis to show it was cut."""
    return text if len(text) <= most else text[: most - 1].rstrip() + "…"


class Wishes:
    def __init__(
        self,
        store: Store,
        cfg: WishesConfig,
        github: Callable[[], GitHub],
        told: Told,
        running: Callable[[str], bool],
        clock: Callable[[], float] = time.time,
    ):
        """`github()` makes the GitHub client, once, the first time it's needed (GitHubError if it can't: no token,
        say), and again only after that failed. `running(sha)`: whether the checkout TARS runs from has that commit."""
        self.store, self.cfg, self._github, self.told, self._running, self._clock = (
            store,
            cfg,
            github,
            told,
            running,
            clock,
        )
        self._client: GitHub | None = None
        self._lock = threading.Lock()  # a tap (the Telegram service's thread) and follow() (its own) share the client
        self._closing = threading.Event()

    def offer(self, wish_id: int, text: str) -> list[str]:
        """Tells the maintainers about a wish just noted (`text`), asking whether to have it built. The maintainers
        reached. The issue is shown whole, as it would be posted: should the message be too long for Telegram, it's
        `text` that's cut, never the issue."""
        from .telegram import MAX_TEXT

        wish = self._wish(wish_id)
        title, body = issue(wish)
        self.store.write("UPDATE wishes SET build=? WHERE id=?", (OFFERED, wish_id))
        posted = (
            "\n\nWant me to have it built? Claude Code would build it from this, posted on GitHub as an issue, and "
            f"open a pull request for you to review:\n\n{title}\n{body}"
        )
        return (
            self.told.maintainers(_clip(text, MAX_TEXT - len(posted)) + posted, [("Build it", f"{TAP}{wish_id}")]) or []
        )

    def build(self, wish_id: int) -> str:
        """A maintainer tapped Build it: the issue is opened. What to tell them."""
        wish = self._wish(wish_id)
        if wish is None or wish["build"] != OFFERED or wish["granted"] is not None:
            return "That one's already on its way, or done."
        since = self._clock() - MONTH_S
        built = self.store.row("SELECT COUNT(*) AS n FROM wishes WHERE built_at > ?", (since,))["n"]
        if built >= self.cfg.monthly:
            return f"I've had {built} built in the last 30 days, the most I'm set to ([wishes] monthly). Ask me later."
        title, body = issue(wish)
        try:
            number, url = self._gh().open_issue(title, body, self.cfg.label)
        except GitHubError as e:
            return f"I couldn't ask for it on GitHub: {e}"
        self.store.write(
            "UPDATE wishes SET build=?, issue=?, built_at=? WHERE id=?", (BUILDING, number, self._clock(), wish_id)
        )
        return f"Asked for it: {url}. I'll tell you when there's a pull request to look at."

    def follow(self) -> None:
        """One pass over the wishes being built, merged or waiting to be told."""
        for wish in self.store.rows(
            "SELECT * FROM wishes WHERE build IN (?, ?, ?) AND granted IS NULL", (BUILDING, READY, MERGED)
        ):
            try:
                if wish["build"] == MERGED:
                    self._grant(wish)
                else:
                    self._watch(wish)
            except GitHubError as e:
                print(f"(wishes: couldn't follow wish {wish['id']}: {e})")

    def _watch(self, wish: dict) -> None:
        github, now = self._gh(), self._clock()
        pull = github.pull_for(wish["issue"]) if wish["pr"] is None else github.pull(wish["pr"])
        if pull is None:
            if now - wish["built_at"] > NO_PULL_AFTER_S:
                self._set(wish, FAILED)
                self.told.maintainers(f"Nothing came of building “{wish['asked']}”: no pull request after 3 hours.", [])
            return
        if not github.ours(pull, wish["issue"]):
            self._set(wish, FAILED)
            self.told.maintainers(
                f"I stopped following “{wish['asked']}”: pull request {pull.get('number')} isn't Claude Code's own.", []
            )
            return
        if wish["pr"] is None:
            self.store.write("UPDATE wishes SET pr=? WHERE id=?", (pull["number"], wish["id"]))
        if pull.get("merged_at"):
            self.store.write(
                "UPDATE wishes SET build=?, sha=? WHERE id=?", (MERGED, pull["merge_commit_sha"], wish["id"])
            )
            return
        if pull["state"] == "closed":
            self._set(wish, FAILED)
            self.told.maintainers(
                f"“{wish['asked']}” was closed without merging, so I've dropped it. {pull['html_url']}", []
            )
            return
        head = pull["head"]["sha"]
        moved = wish["moved"] or wish["built_at"]
        if head != wish["head"]:
            moved = now
            self.store.write("UPDATE wishes SET head=?, moved=? WHERE id=?", (head, moved, wish["id"]))
        state, failed = github.checks(head)
        if state == "passed" and wish["build"] == BUILDING:
            self._set(wish, READY)
            self.told.maintainers(
                f"“{wish['asked']}” is built, and CI passes: {pull['title']}. {pull['html_url']}\n\n"
                f"{_first_paragraph(pull.get('body') or '')}",
                [],
            )
        elif state == "failed" and wish["checked"] != head:
            if wish["tries"] >= self.cfg.tries:
                self._set(wish, FAILED)
                self.told.maintainers(
                    f"I gave up on “{wish['asked']}”: CI still fails ({', '.join(failed)}) after {wish['tries']} "
                    f"tries. {pull['html_url']}",
                    [],
                )
            else:
                github.comment(
                    pull["number"],
                    f"@claude CI fails on this pull request ({', '.join(failed)}). Read the failing runs, fix the "
                    "cause, and push to this branch, keeping to .github/wish.md.",
                )
                self.store.write(
                    "UPDATE wishes SET tries=tries+1, checked=?, build=?, moved=? WHERE id=?",
                    (head, BUILDING, now, wish["id"]),
                )
        elif state != "passed" and wish["build"] == BUILDING and now - moved > STALLED_AFTER_S:
            self._set(wish, FAILED)
            stuck = "no fix was pushed after it was sent back" if state == "failed" else "CI never finished on it"
            self.told.maintainers(f"I gave up on “{wish['asked']}”: {stuck} in 3 hours. {pull['html_url']}", [])

    def _grant(self, wish: dict) -> None:
        if not (wish["sha"] and self._running(wish["sha"])):
            return
        # Told first: should marking it fail, they may hear it twice, never not at all.
        if wish["person"]:
            self.told.person(
                wish["person"],
                wish["via"],
                f"Remember you asked me for this: {wish['asked'].rstrip('.')}? I can do it now.",
            )
        else:
            self.told.maintainers(f"“{wish['asked']}” is installed: I can do it now.", [])
        self.store.write(
            "UPDATE wishes SET build=?, granted=? WHERE id=? AND build=?", (GRANTED, self._clock(), wish["id"], MERGED)
        )

    def _set(self, wish: dict, build: str) -> None:
        self.store.write("UPDATE wishes SET build=? WHERE id=?", (build, wish["id"]))

    def _wish(self, wish_id: int) -> dict | None:
        return self.store.row("SELECT * FROM wishes WHERE id=?", (wish_id,))

    def _gh(self) -> GitHub:
        """The one GitHub client, made the first time it's needed."""
        with self._lock:
            if self._closing.is_set():
                raise GitHubError("TARS is stopping")
            if self._client is None:
                self._client = self._github()
            return self._client

    def start(self) -> None:
        def loop():
            while not self._closing.is_set():
                try:
                    self.follow()
                except Exception as e:  # noqa: BLE001 - the next pass tries again
                    print(f"(wishes: a pass failed: {e!r})")
                self._closing.wait(FOLLOW_EVERY_S)

        threading.Thread(target=loop, daemon=True, name="wishes").start()

    def close(self) -> None:
        """Stops following, and closes the GitHub client's connections. Nothing reaches GitHub after it."""
        self._closing.set()
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None


def _first_paragraph(text: str) -> str:
    """What a pull request says it does: its first paragraph, past the line that says which issue it closes."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    paragraphs = [p for p in paragraphs if not re.fullmatch(r"(Closes|Fixes|Resolves) #\d+\.?", p)]
    return paragraphs[0][:600] if paragraphs else ""


def running_has(root: Path) -> Callable[[str], bool]:
    """Whether the checkout at `root` (the one TARS runs from) has a commit."""

    def has(sha: str) -> bool:
        done = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", sha, "HEAD"],
            capture_output=True,
            timeout=20,
            check=False,
        )
        return done.returncode == 0

    return has


def tap_listener(wishes: Wishes, maintainers: list[int], bot):
    """For the Telegram service (TelegramTars.listen_first): a maintainer's tap on a wish's Build it button opens its
    issue, and the button goes. Anyone else's tap is only acknowledged."""

    def take(update: dict) -> bool:
        tap = update.get("callback_query") or {}
        data = str(tap.get("data", ""))
        if not data.startswith(TAP):
            return False
        try:
            bot.answer_tap(tap["id"])
        except Exception as e:  # noqa: BLE001 - only the button's spinner
            print(f"(wishes: couldn't answer the tap: {e})")
        chat = (tap.get("from") or {}).get("id")
        number = data.removeprefix(TAP)
        if chat not in maintainers or not number.isdigit():
            return True
        said = wishes.build(int(number))
        try:
            bot.drop_buttons(chat, (tap.get("message") or {}).get("message_id"))
            bot.send(chat, said)
        except Exception as e:  # noqa: BLE001 - the build goes ahead whether or not this is said
            print(f"(wishes: couldn't answer the maintainer: {e})")
        return True

    return take
