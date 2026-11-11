"""Wishes that come true (docs/wishes.md): something TARS was asked for and couldn't do, built by a coding agent once a
maintainer says yes, and told to whoever asked once it's installed.

1. A wish (myself.py) goes to the maintainers' chats with what would be posted to GitHub, and a Build it button.
2. A maintainer's tap opens an issue on [wishes] repo, labelled [wishes] label, which .github/workflows/wish.yml hands
   to Claude Code: it builds the wish on a branch of its own and opens a pull request. It has the repository and
   nothing else: no .env, no config.local.toml, no database; what it knows of the house is the issue.
3. follow(), every few minutes in the Telegram service, watches the pull request: CI green, the maintainers are told
   what it does; CI red, it's asked to fix it, [wishes] tries times at most; closed unmerged, it's dropped.
4. Merged is installed by the updater (updater.py). Once the running checkout has it, the wish is granted and the one
   who asked is told, once: in their chat, or the next time they speak to TARS.

Held in code: only a maintainer's tap starts a build, at most [wishes] monthly in 30 days; nothing here merges.
"""

import re
import subprocess
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
MONTH_S = 30 * 86400.0
PASSED = {"success", "neutral", "skipped"}
API = "https://api.github.com"


class GitHubError(Exception):
    pass


class GitHub:
    """The few calls of GitHub's REST API a wish needs, with a token that may open issues and comment on [wishes]
    repo. The token never leaves this class."""

    def __init__(self, repo: str, token: str, transport: httpx.BaseTransport | None = None):
        self.repo = repo
        self._http = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=20,
            transport=transport,
        )

    def _call(self, method: str, path: str, **kw) -> dict | list:
        try:
            r = self._http.request(method, path.format(repo=self.repo), **kw)
        except httpx.HTTPError as e:
            raise GitHubError(f"GitHub didn't answer: {type(e).__name__}") from None
        if r.status_code >= 400:
            raise GitHubError(f"GitHub said {r.status_code}: {r.json().get('message', '') if r.content else ''}")
        return r.json()

    def open_issue(self, title: str, body: str, label: str) -> tuple[int, str]:
        made = self._call("POST", "/repos/{repo}/issues", json={"title": title, "body": body, "labels": [label]})
        return made["number"], made["html_url"]

    def pull_for(self, issue: int) -> dict | None:
        """The newest pull request that says it closes `issue`, open or not."""
        pulls = self._call("GET", "/repos/{repo}/pulls", params={"state": "all", "per_page": 50})
        marks = (f"#{issue}\n", f"#{issue} ", f"#{issue}.")
        for p in pulls:
            body = f"{p.get('body') or ''}\n"
            if any(m in body for m in marks) or p["head"]["ref"].startswith(f"claude/issue-{issue}-"):
                return p
        return None

    def pull(self, number: int) -> dict:
        return self._call("GET", f"/repos/{{repo}}/pulls/{number}")

    def checks(self, sha: str) -> tuple[str, list[str]]:
        """CI on a commit: ("pending" | "passed" | "failed", the names of the checks that failed)."""
        runs = self._call("GET", f"/repos/{{repo}}/commits/{sha}/check-runs", params={"per_page": 100})["check_runs"]
        if not runs or any(r["status"] != "completed" for r in runs):
            return "pending", []
        failed = sorted(r["name"] for r in runs if r["conclusion"] not in PASSED)
        return ("failed" if failed else "passed"), failed

    def comment(self, number: int, body: str) -> None:
        self._call("POST", f"/repos/{{repo}}/issues/{number}/comments", json={"body": body})


@dataclass
class Told:
    """Where what happens to a wish is said: `maintainers(text, buttons)`, and `person(name, via, text)` for the one
    who asked."""

    maintainers: Callable[[str, list[tuple[str, str]]], None]
    person: Callable[[str, str, str], None]


def issue(wish: dict) -> tuple[str, str]:
    """What's posted to GitHub for a wish: its title and body. Nobody's name, nor the chat it came from."""
    asked, why = wish["asked"].strip().rstrip("."), wish["why"].strip().rstrip(".")
    title = asked[:1].upper() + asked[1:]
    body = (
        f"TARS was asked for this and couldn't do it.\n\n**Asked for:** {asked}.\n\n**What stopped it:** {why}.\n\n"
        f"Build it as [.github/wish.md](../blob/main/.github/wish.md) says. (TARS's wish {wish['id']}.)"
    )
    return title[:120], body


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
        """`running(sha)`: whether the checkout TARS runs from has that commit."""
        self.store, self.cfg, self._github, self.told, self._running, self._clock = (
            store,
            cfg,
            github,
            told,
            running,
            clock,
        )

    def offer(self, wish_id: int, text: str) -> None:
        """Tells the maintainers about a wish just noted (`text`), asking whether to have it built."""
        wish = self._wish(wish_id)
        title, body = issue(wish)
        self.store.write("UPDATE wishes SET build=? WHERE id=?", (OFFERED, wish_id))
        self.told.maintainers(
            f"{text}\n\nWant me to have it built? Claude Code would build it from this, posted on GitHub as "
            f"an issue, and open a pull request for you to review:\n\n{title}\n{body}",
            [("Build it", f"{TAP}{wish_id}")],
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
            number, url = self._github().open_issue(title, body, self.cfg.label)
        except GitHubError as e:
            return f"I couldn't ask for it on GitHub: {e}"
        self.store.write(
            "UPDATE wishes SET build=?, issue=?, built_at=? WHERE id=?", (BUILDING, number, self._clock(), wish_id)
        )
        return f"Asked for it: {url}. I'll tell you when there's a pull request to look at."

    def follow(self) -> None:
        """One pass over the wishes being built, merged or waiting to be told."""
        for wish in self.store.rows("SELECT * FROM wishes WHERE build IN (?, ?, ?)", (BUILDING, READY, MERGED)):
            try:
                if wish["build"] == MERGED:
                    self._grant(wish)
                else:
                    self._watch(wish)
            except GitHubError as e:
                print(f"(wishes: couldn't follow wish {wish['id']}: {e})")

    def _watch(self, wish: dict) -> None:
        github = self._github()
        pull = github.pull_for(wish["issue"]) if wish["pr"] is None else github.pull(wish["pr"])
        if pull is None:
            if self._clock() - wish["built_at"] > NO_PULL_AFTER_S:
                self._set(wish, FAILED)
                self.told.maintainers(f"Nothing came of building “{wish['asked']}”: no pull request after 3 hours.", [])
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
            return
        head = pull["head"]["sha"]
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
                    "UPDATE wishes SET tries=tries+1, checked=?, build=? WHERE id=?", (head, BUILDING, wish["id"])
                )

    def _grant(self, wish: dict) -> None:
        if not (wish["sha"] and self._running(wish["sha"])):
            return
        with self.store.transaction() as db:
            if not db.execute(
                "UPDATE wishes SET build=?, granted=? WHERE id=? AND build=?",
                (GRANTED, self._clock(), wish["id"], MERGED),
            ).rowcount:
                return
        text = f"Remember you asked me for this: {wish['asked'].rstrip('.')}? I can do it now."
        if wish["person"]:
            self.told.person(wish["person"], wish["via"], text)
        else:
            self.told.maintainers(f"“{wish['asked']}” is installed: I can do it now.", [])

    def _set(self, wish: dict, build: str) -> None:
        self.store.write("UPDATE wishes SET build=? WHERE id=?", (build, wish["id"]))

    def _wish(self, wish_id: int) -> dict | None:
        return self.store.row("SELECT * FROM wishes WHERE id=?", (wish_id,))

    def start(self) -> None:
        import threading

        def loop():
            while True:
                try:
                    self.follow()
                except Exception as e:  # noqa: BLE001 - the next pass tries again
                    print(f"(wishes: a pass failed: {e!r})")
                time.sleep(FOLLOW_EVERY_S)

        threading.Thread(target=loop, daemon=True, name="wishes").start()


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
