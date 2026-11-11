"""Wishes that come true (wishes.py): a maintainer's tap opens an issue, its pull request is followed through CI, and
whoever asked is told once it's installed."""

import dataclasses
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from voice_assistant.config import HouseholdConfig, WishesConfig, _wishes_problems, load_config
from voice_assistant.events import EventLog
from voice_assistant.home import Request
from voice_assistant.myself import Myself
from voice_assistant.wishes import (
    BUILDING,
    FAILED,
    GRANTED,
    MERGED,
    NO_PULL_AFTER_S,
    OFFERED,
    READY,
    GitHub,
    GitHubError,
    Told,
    Wishes,
    issue,
    running_has,
    tap_listener,
)

NOW = 1_800_000_000.0
REPO = "alon-pilosoph/tars"


class FakeGitHub(httpx.MockTransport):
    """GitHub's REST API, as much as a wish uses, kept in memory."""

    def __init__(self):
        super().__init__(self.handle)
        self.issues: list[dict] = []
        self.pulls: list[dict] = []
        self.runs: dict[str, list[dict]] = {}
        self.comments: list[tuple[int, str]] = []
        self.auth: set[str] = set()
        self.down = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no route")
        self.auth.add(request.headers["Authorization"])
        path, method = request.url.path.removeprefix(f"/repos/{REPO}"), request.method
        if method == "POST" and path == "/issues":
            body = json.loads(request.content)
            number = 100 + len(self.issues)
            self.issues.append({"number": number, **body})
            return httpx.Response(
                201, json={"number": number, "html_url": f"https://github.com/{REPO}/issues/{number}"}
            )
        if method == "GET" and path == "/pulls":
            return httpx.Response(200, json=list(reversed(self.pulls)))
        if method == "GET" and path.startswith("/pulls/"):
            number = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=next(p for p in self.pulls if p["number"] == number))
        if method == "GET" and path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": self.runs.get(path.split("/")[2], [])})
        if method == "POST" and path.endswith("/comments"):
            self.comments.append((int(path.split("/")[2]), json.loads(request.content)["body"]))
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": "Not Found"})

    def open_pull(self, issue: int, sha: str = "aaa1", body: str | None = None) -> dict:
        pull = {
            "number": 200 + len(self.pulls),
            "title": "TARS reads the kindergarten's updates",
            "html_url": f"https://github.com/{REPO}/pull/{200 + len(self.pulls)}",
            "body": body if body is not None else f"Closes #{issue}.\n\nYou can ask TARS for the updates.\n\nDetails.",
            "state": "open",
            "merged_at": None,
            "merge_commit_sha": None,
            "head": {"sha": sha, "ref": f"claude/issue-{issue}-updates"},
        }
        self.pulls.append(pull)
        return pull

    def ci(self, sha: str, *conclusions: str | None) -> None:
        self.runs[sha] = [
            {"name": f"check {i}", "status": "completed" if c else "in_progress", "conclusion": c}
            for i, c in enumerate(conclusions)
        ]


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def log(tmp_path):
    return EventLog(tmp_path / "events")


def wishing(log, github, running=lambda sha: False, **cfg):
    said: list[tuple[str, list]] = []
    people: list[tuple[str, str, str]] = []
    clock = Clock()
    wishes = Wishes(
        log.store,
        WishesConfig(enabled=True, repo=REPO, **cfg),
        lambda: GitHub(REPO, "ghp_secret", transport=github),
        Told(lambda text, buttons: said.append((text, buttons)), lambda *p: people.append(p)),
        running,
        clock=clock,
    )
    return wishes, said, people, clock


def wish(log, person="Stacey", via="chat", asked="read the kindergarten's WhatsApp updates") -> int:
    return log.store.write(
        "INSERT INTO wishes (at, person, via, asked, why) VALUES (?, ?, ?, ?, ?)",
        (NOW, person, via, asked, "TARS can't reach WhatsApp; a WhatsApp bridge would let it."),
    )


def row(log, wish_id) -> dict:
    return log.store.row("SELECT * FROM wishes WHERE id=?", (wish_id,))


def test_whats_posted_names_nobody_and_says_what_stopped_tars(log):
    title, body = issue(row(log, wish(log, person="Stacey", asked="read the kindergarten's WhatsApp updates.")))
    assert title == "Read the kindergarten's WhatsApp updates"
    assert "**What stopped it:** TARS can't reach WhatsApp" in body and "Stacey" not in title + body


def test_a_wish_is_offered_to_the_maintainers_with_a_build_button_and_what_would_be_posted(log, github):
    wishes, said, _, _ = wishing(log, github)
    n = wish(log)
    wishes.offer(n, "Stacey asked me: read the updates. I couldn't: no WhatsApp.")
    (text, buttons), = said  # fmt: skip
    assert text.startswith("Stacey asked me") and "Want me to have it built?" in text and issue(row(log, n))[1] in text
    assert buttons == [("Build it", f"wish:{n}")] and row(log, n)["build"] == OFFERED


def test_a_tap_opens_a_labelled_issue_once(log, github):
    wishes, _, _, _ = wishing(log, github)
    n = wish(log)
    wishes.offer(n, "x")
    assert (
        wishes.build(n)
        == f"Asked for it: https://github.com/{REPO}/issues/100. I'll tell you when there's a pull request to look at."
    )
    assert github.issues[0]["labels"] == ["wish"] and github.auth == {"Bearer ghp_secret"}
    assert row(log, n)["build"] == BUILDING and row(log, n)["issue"] == 100
    assert wishes.build(n) == "That one's already on its way, or done." and len(github.issues) == 1


def test_no_more_builds_than_monthly_allows_in_30_days(log, github):
    wishes, _, _, clock = wishing(log, github, monthly=1)
    first, second = wish(log), wish(log)
    for n in (first, second):
        wishes.offer(n, "x")
    wishes.build(first)
    assert wishes.build(second).startswith("I've had 1 built in the last 30 days") and len(github.issues) == 1
    clock.t += 31 * 86400
    assert wishes.build(second).startswith("Asked for it")


def test_github_not_answering_is_said_and_the_wish_can_be_tapped_again(log, github):
    wishes, _, _, _ = wishing(log, github)
    n = wish(log)
    wishes.offer(n, "x")
    github.down = True
    assert wishes.build(n) == "I couldn't ask for it on GitHub: GitHub didn't answer: ConnectError"
    assert row(log, n)["build"] == OFFERED


def building(log, github, **cfg):
    wishes, said, people, clock = wishing(log, github, **cfg)
    n = wish(log)
    wishes.offer(n, "x")
    wishes.build(n)
    said.clear()
    return wishes, said, people, clock, n


def test_once_ci_passes_the_maintainers_are_told_what_it_does(log, github):
    wishes, said, _, _, n = building(log, github)
    wishes.follow()
    assert said == [] and row(log, n)["pr"] is None  # no pull request yet
    github.open_pull(100)
    github.ci("aaa1", "success", None)
    wishes.follow()
    assert said == [] and row(log, n)["pr"] == 200  # CI still running
    github.ci("aaa1", "success", "skipped")
    wishes.follow()
    wishes.follow()
    (text, _), = said  # fmt: skip
    assert "is built, and CI passes" in text and f"https://github.com/{REPO}/pull/200" in text
    assert text.endswith("\n\nYou can ask TARS for the updates.") and "Closes" not in text
    assert row(log, n)["build"] == READY


def test_failing_ci_is_sent_back_to_be_fixed_once_per_commit_and_given_up_on_after_tries(log, github):
    wishes, said, _, _, n = building(log, github, tries=2)
    pull = github.open_pull(100)
    for sha in ("aaa1", "aaa2"):
        pull["head"]["sha"] = sha
        github.ci(sha, "failure", "success")
        wishes.follow()
        wishes.follow()
    assert [c[0] for c in github.comments] == [200, 200] and github.comments[0][1].startswith("@claude CI fails")
    assert "(check 0)" in github.comments[0][1] and row(log, n)["tries"] == 2
    pull["head"]["sha"] = "aaa3"
    github.ci("aaa3", "failure", "success")
    wishes.follow()
    assert len(github.comments) == 2 and row(log, n)["build"] == FAILED
    assert said[0][0].startswith("I gave up on “read the kindergarten's WhatsApp updates”: CI still fails")


def test_a_fix_that_passes_after_a_failure_is_told_as_built(log, github):
    wishes, said, _, _, n = building(log, github)
    pull = github.open_pull(100)
    github.ci("aaa1", "failure")
    wishes.follow()
    pull["head"]["sha"] = "aaa2"
    github.ci("aaa2", "success")
    wishes.follow()
    assert row(log, n)["build"] == READY and "is built" in said[0][0]


def test_no_pull_request_after_hours_is_a_failed_build(log, github):
    wishes, said, _, clock, n = building(log, github)
    clock.t += NO_PULL_AFTER_S + 1
    wishes.follow()
    assert row(log, n)["build"] == FAILED and said[0][0].startswith("Nothing came of building")


def test_a_pull_request_closed_unmerged_drops_the_wish_quietly(log, github):
    wishes, said, _, _, n = building(log, github)
    github.open_pull(100)["state"] = "closed"
    wishes.follow()
    assert row(log, n)["build"] == FAILED and said == []


def test_merged_and_installed_grants_it_and_tells_whoever_asked_once(log, github):
    installed: set[str] = set()
    wishes, _, people, _, n = building(log, github, running=lambda sha: sha in installed)
    pull = github.open_pull(100)
    pull.update(merged_at="2026-11-12T10:00:00Z", merge_commit_sha="m3rg3", state="closed")
    wishes.follow()
    assert row(log, n)["build"] == MERGED and row(log, n)["sha"] == "m3rg3"
    wishes.follow()
    assert people == [] and row(log, n)["granted"] is None  # merged, not installed yet
    installed.add("m3rg3")
    wishes.follow()
    wishes.follow()
    assert people == [
        ("Stacey", "chat", "Remember you asked me for this: read the kindergarten's WhatsApp updates? I can do it now.")
    ]
    assert row(log, n)["build"] == GRANTED and row(log, n)["granted"] is not None


def test_a_wish_from_someone_unknown_is_told_to_the_maintainers_when_installed(log, github):
    wishes, said, people, _ = wishing(log, github, running=lambda sha: True)
    n = wish(log, person=None, via="voice")
    log.store.write("UPDATE wishes SET build=?, sha=? WHERE id=?", (MERGED, "m3rg3", n))
    wishes.follow()
    assert people == [] and said == [("“read the kindergarten's WhatsApp updates” is installed: I can do it now.", [])]


def test_github_errors_while_following_are_a_line_not_a_crash(log, github, capsys):
    wishes, _, _, _, n = building(log, github)
    github.down = True
    wishes.follow()
    assert row(log, n)["build"] == BUILDING and "couldn't follow wish" in capsys.readouterr().out


def test_an_error_from_github_says_what_it_said():
    gh = GitHub(
        REPO, "t", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "Bad credentials"}))
    )
    with pytest.raises(GitHubError, match="GitHub said 401: Bad credentials"):
        gh.open_issue("x", "y", "wish")


class Bot:
    def __init__(self):
        self.sent, self.dropped, self.answered = [], [], []

    def answer_tap(self, tap_id):
        self.answered.append(tap_id)

    def drop_buttons(self, chat, message_id):
        self.dropped.append((chat, message_id))

    def send(self, chat, text):
        self.sent.append((chat, text))


def tap(chat: int, data: str) -> dict:
    return {"callback_query": {"id": "t1", "data": data, "from": {"id": chat}, "message": {"message_id": 7}}}


def test_only_a_maintainers_tap_builds_it(log, github):
    wishes, _, _, _ = wishing(log, github)
    n = wish(log)
    wishes.offer(n, "x")
    bot = Bot()
    take = tap_listener(wishes, [1], bot)
    assert take({"message": {"text": "hi"}}) is False
    assert take(tap(2, f"wish:{n}")) is True and github.issues == [] and bot.sent == []
    assert take(tap(1, f"wish:{n}")) is True and len(github.issues) == 1
    assert bot.dropped == [(1, 7)] and bot.sent[0][1].startswith("Asked for it") and bot.answered == ["t1", "t1"]


def test_the_running_checkout_is_asked_whether_it_has_the_merge(tmp_path):
    git = ["git", "-C", str(tmp_path), "-c", "user.name=T", "-c", "user.email=t@x"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "one"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    has = running_has(tmp_path)
    assert has(sha) and not has("0" * 40)


def test_the_wishes_settings_are_checked():
    shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    on = dataclasses.replace(shipped, wishes=WishesConfig(enabled=True, repo="tars", tries=11, monthly=-1, label="a/b"))
    problems = _wishes_problems(on)
    assert any("repo must be" in p for p in problems) and any("needs [household] maintainers" in p for p in problems)
    assert any("tries must be" in p for p in problems) and any("monthly must be" in p for p in problems)
    assert any("label must be" in p for p in problems)
    ok = dataclasses.replace(on, wishes=WishesConfig(enabled=True, repo=REPO), household=HouseholdConfig(["Alon"]))
    assert _wishes_problems(ok) == [] and _wishes_problems(shipped) == []


def test_a_wish_noted_with_wishes_on_is_offered_with_its_number(log, tmp_path):
    shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    cfg = dataclasses.replace(shipped, household=HouseholdConfig(["Alon"]))
    offered = []
    tars = Myself(cfg, tmp_path, log, offer=lambda n, text: offered.append((n, text)))
    said = tars.call(
        {"action": "wish", "query": "read the WhatsApp updates", "why": "no WhatsApp", "since": None,
         "until": None, "value": None},
        Request("Stacey", by_chat=True),
    )  # fmt: skip
    assert said.startswith("passed on to Alon in their chat (1)")
    assert offered == [(1, "Stacey asked me: read the WhatsApp updates. I couldn't: no WhatsApp.")]


@pytest.mark.parametrize("via, channel, due_now", [("chat", "telegram", True), ("voice", "voice", False)])
def test_whoever_asked_is_told_in_their_chat_or_the_next_time_theyre_heard(log, tmp_path, via, channel, due_now):
    from voice_assistant.__main__ import make_wishes
    from voice_assistant.config import TelegramConfig

    shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    cfg = dataclasses.replace(
        shipped,
        wishes=WishesConfig(enabled=True, repo=REPO),
        household=HouseholdConfig(["Alon"]),
        telegram=TelegramConfig(enabled=True, people={"Alon": 1, "Stacey": 2}),
    )
    make_wishes(cfg, tmp_path, log).told.person("Stacey", via, "I can do it now.")
    (r,) = log.store.rows("SELECT * FROM reminders")
    assert (r["for_name"], r["text"], r["channel"], r["due"] is not None) == (
        "Stacey",
        "I can do it now.",
        channel,
        due_now,
    )
