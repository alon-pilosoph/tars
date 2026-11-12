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
    ASKED_MAX,
    BUILDING,
    FAILED,
    GRANTED,
    MERGED,
    NO_PULL_AFTER_S,
    OFFERED,
    READY,
    STALLED_AFTER_S,
    WHY_MAX,
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

    def open_pull(self, issue: int, sha: str = "aaa1", body: str | None = None, ref=None, repo=REPO, user=None) -> dict:
        pull = {
            "number": 200 + len(self.pulls),
            "title": "TARS reads the kindergarten's updates",
            "html_url": f"https://github.com/{REPO}/pull/{200 + len(self.pulls)}",
            "body": body if body is not None else f"Closes #{issue}.\n\nYou can ask TARS for the updates.\n\nDetails.",
            "state": "open",
            "merged_at": None,
            "merge_commit_sha": None,
            "head": {"sha": sha, "ref": ref or f"claude/issue-{issue}-updates", "repo": {"full_name": repo}},
            "user": user or {"login": "claude[bot]", "type": "Bot"},
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


@pytest.mark.parametrize("asked, why", [("a" * 10_000, "b" * 10_000), ("word " * 2000, "why " * 2000)])
def test_what_a_maintainer_is_shown_is_all_of_what_is_posted_however_long_the_wish(log, github, asked, why):
    from voice_assistant import telegram

    sent = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    bot = telegram.Bot("t", http=httpx.Client(transport=httpx.MockTransport(answer)))
    wishes, _, _, _ = wishing(log, github)
    wishes.told.maintainers = lambda text, buttons: [bot.send_buttons(1, text, buttons) and "Alon"]
    n = log.store.write(
        "INSERT INTO wishes (at, person, via, asked, why) VALUES (?, ?, ?, ?, ?)", (NOW, "S" * 5000, "chat", asked, why)
    )
    lead = Myself._wish_text("S" * 5000, asked, why)
    assert wishes.offer(n, lead) == ["Alon"]
    wishes.build(n)
    (shown,), (posted,) = sent, github.issues
    assert len(shown) <= telegram.MAX_TEXT and shown.endswith(f"\n\n{posted['title']}\n{posted['body']}")
    assert shown.startswith("SSS") and "Want me to have it built?" in shown
    assert (
        f"{asked[: ASKED_MAX - 1].rstrip()}…." in posted["body"]
        and f"{why[: WHY_MAX - 1].rstrip()}…." in posted["body"]
    )


def test_a_short_wish_is_posted_whole(log):
    title, body = issue({"id": 3, "asked": "read the updates.", "why": "no WhatsApp."})
    assert (title, "…" in body) == ("Read the updates", False) and "**What stopped it:** no WhatsApp." in body


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


def test_a_pull_request_closed_unmerged_drops_the_wish_and_says_so(log, github):
    wishes, said, _, _, n = building(log, github)
    github.open_pull(100)["state"] = "closed"
    wishes.follow()
    assert row(log, n)["build"] == FAILED and said[0][0].startswith(
        "“read the kindergarten's WhatsApp updates” was closed"
    )


@pytest.mark.parametrize(
    "kind",
    [
        {"repo": "someone/tars"},  # a fork's branch of the same name
        {"user": {"login": "someone", "type": "User"}, "ref": "claude/issue-100-mine"},
        {"user": {"login": "someone", "type": "User"}, "ref": "fix-it", "body": "Closes #100.\n\nMine."},
        {"ref": "claude/issue-1000-other"},
    ],
)
def test_only_claude_codes_own_pull_request_for_the_issue_is_followed(log, github, kind):
    wishes, said, _, _, n = building(log, github)
    pull = github.open_pull(100, **kind)
    github.ci("aaa1", "failure")
    wishes.follow()
    assert row(log, n)["pr"] is None and github.comments == [] and said == []
    pull["merged_at"], pull["merge_commit_sha"] = "2026-11-12T10:00:00Z", "m3rg3"
    wishes.follow()
    assert row(log, n)["build"] == BUILDING


def test_a_followed_pull_request_that_stops_being_claudes_gets_no_comment(log, github):
    wishes, said, _, _, n = building(log, github)
    pull = github.open_pull(100)
    github.ci("aaa1", "success", None)
    wishes.follow()
    assert row(log, n)["pr"] == 200
    pull["head"]["repo"] = {"full_name": "someone/tars"}
    github.ci("aaa1", "failure")
    wishes.follow()
    assert github.comments == [] and row(log, n)["build"] == FAILED and "isn't Claude Code's own" in said[0][0]


def test_ci_that_never_finishes_is_given_up_on(log, github):
    wishes, said, _, clock, n = building(log, github)
    github.open_pull(100)
    wishes.follow()
    clock.t += STALLED_AFTER_S - 60
    wishes.follow()
    assert said == []
    clock.t += 120
    wishes.follow()
    assert row(log, n)["build"] == FAILED and "CI never finished on it in 3 hours" in said[0][0]


def test_a_fix_never_pushed_is_given_up_on_and_a_push_restarts_the_wait(log, github):
    wishes, said, _, clock, n = building(log, github)
    pull = github.open_pull(100)
    github.ci("aaa1", "failure")
    wishes.follow()
    assert len(github.comments) == 1
    clock.t += STALLED_AFTER_S - 60
    pull["head"]["sha"] = "aaa2"  # pushed, and CI is running
    wishes.follow()
    clock.t += STALLED_AFTER_S - 60
    wishes.follow()
    assert said == [] and row(log, n)["build"] == BUILDING
    github.ci("aaa2", "failure")
    wishes.follow()
    clock.t += STALLED_AFTER_S + 60
    wishes.follow()
    assert row(log, n)["build"] == FAILED and "no fix was pushed after it was sent back" in said[0][0]


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


def test_a_grant_that_couldnt_be_told_is_told_on_the_next_pass(log, github):
    wishes, _, _, _ = wishing(log, github, running=lambda sha: True)
    n = wish(log)
    log.store.write("UPDATE wishes SET build=?, sha=? WHERE id=?", (MERGED, "m3rg3", n))
    told = []

    def person(*p):
        if not told:
            told.append(None)
            raise RuntimeError("the database was busy")
        told.append(p)

    wishes.told.person = person
    with pytest.raises(RuntimeError):
        wishes.follow()
    assert row(log, n)["build"] == MERGED
    wishes.follow()
    wishes.follow()
    assert len(told) == 2 and row(log, n)["build"] == GRANTED


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


def test_one_github_client_serves_every_tap_and_pass_and_is_closed_with_tars(log, github):
    made: list[GitHub] = []

    def make() -> GitHub:
        made.append(GitHub(REPO, "ghp_secret", transport=github))
        return made[-1]

    wishes, _, _, _ = wishing(log, github)
    wishes._github = make
    n = wish(log)
    wishes.offer(n, "x")
    wishes.build(n)
    github.open_pull(100)
    for _ in range(3):
        wishes.follow()
    assert len(made) == 1 and row(log, n)["pr"] == 200
    wishes.close()
    assert made[0]._http.is_closed
    with pytest.raises(GitHubError, match="TARS is stopping"):
        wishes._gh()


def test_a_client_that_couldnt_be_made_is_tried_again_next_time(log, github, capsys):
    wishes, _, _, _ = wishing(log, github)
    n = wish(log)
    wishes.offer(n, "x")
    tries = []

    def make() -> GitHub:
        tries.append(None)
        if len(tries) == 1:
            raise GitHubError("no GITHUB_TOKEN in .env")
        return GitHub(REPO, "ghp_secret", transport=github)

    wishes._github = make
    assert wishes.build(n) == "I couldn't ask for it on GitHub: no GITHUB_TOKEN in .env"
    assert wishes.build(n).startswith("Asked for it") and len(tries) == 2


def test_closing_stops_the_following(log, github, monkeypatch):
    import threading

    from voice_assistant import wishes as module

    monkeypatch.setattr(module, "FOLLOW_EVERY_S", 0.01)
    wishes, _, _, _, _ = building(log, github)
    passes = []
    wishes.follow = lambda: passes.append(None)
    wishes.start()
    (thread,) = [t for t in threading.enumerate() if t.name == "wishes"]
    wishes.close()
    thread.join(timeout=2)
    assert not thread.is_alive() and passes


@pytest.mark.parametrize(
    "answer, said",
    [
        (httpx.Response(502, text="<html>Bad gateway</html>"), "GitHub said 502: <html>Bad gateway</html>"),
        (httpx.Response(200, text="not json"), "GitHub's answer wasn't JSON"),
        (httpx.Response(200, json=["a list"]), "GitHub's answer wasn't the dict expected"),
        (httpx.Response(201, json={"id": 1}), "GitHub's answer had no issue in it"),
    ],
)
def test_an_answer_from_github_that_isnt_what_was_expected_is_a_github_error(answer, said):
    gh = GitHub(REPO, "t", transport=httpx.MockTransport(lambda r: answer))
    with pytest.raises(GitHubError, match=said.replace("(", r"\(")):
        gh.open_issue("x", "y", "wish")


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
    tars = Myself(cfg, tmp_path, log, offer=lambda n, text: offered.append((n, text)) or ["Alon"])
    said = tars.call(WISH, Request("Stacey", by_chat=True))
    assert said.startswith("passed on to Alon in their chat (1)")
    assert offered == [(1, "Stacey asked me: read the WhatsApp updates. I couldn't: no WhatsApp.")]


WISH = {"action": "wish", "query": "read the WhatsApp updates", "why": "no WhatsApp", "since": None, "until": None,
        "value": None}  # fmt: skip


@pytest.mark.parametrize(
    "reached, asker, said",
    [
        (["Alon", "Stacey"], "stacey", "passed on to Alon in their chat (1)"),  # not to the one who asked
        (["Alon"], "Alon", "noted for whoever looks after TARS (1)"),  # offered to Alon, who asked
    ],
)
def test_a_wish_offered_is_said_passed_on_only_to_the_maintainers_reached_besides_the_asker(
    log, tmp_path, reached, asker, said
):
    shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    cfg = dataclasses.replace(shipped, household=HouseholdConfig(["Alon", "Stacey", "Sam"]))
    tars = Myself(cfg, tmp_path, log, offer=lambda n, text: reached)
    assert tars.call(WISH, Request(asker, by_chat=True)).startswith(said)


def test_a_wish_offered_to_nobody_is_told_the_plain_way(log, tmp_path):
    from voice_assistant.config import TelegramConfig

    shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    cfg = dataclasses.replace(
        shipped,
        household=HouseholdConfig(["Alon", "Stacey"]),
        telegram=TelegramConfig(enabled=True, people={"Alon": 1, "Stacey": 2}),
    )
    tars = Myself(cfg, tmp_path, log, offer=lambda n, text: [])
    assert tars.call(WISH, Request("Stacey", by_chat=True)).startswith("passed on to Alon in their chat (1)")
    assert [r["for_name"] for r in log.store.rows("SELECT * FROM reminders")] == ["Alon"]


def test_the_maintainers_told_straight_through_telegram_are_those_reached(tmp_path, monkeypatch):
    from voice_assistant import __main__ as main
    from voice_assistant import telegram
    from voice_assistant.config import TelegramConfig

    shipped = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    cfg = dataclasses.replace(
        shipped,
        household=HouseholdConfig(["alon ", "Stacey", "Sam"]),
        telegram=TelegramConfig(enabled=True, people={"Alon": 1, "Stacey": 2}),
    )

    class Bot:
        def __init__(self, token):
            pass

        def send_buttons(self, chat, text, buttons):
            if chat == 2:
                raise telegram.TelegramError("blocked")

    monkeypatch.setattr(telegram, "Bot", Bot)
    monkeypatch.setattr(main, "api_key", lambda env, name: "t")
    assert main.tell_maintainers(cfg, tmp_path, "x", [("Build it", "wish:1")]) == ["Alon"]


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
    make_wishes(cfg, tmp_path, log).told.person("stacey", via, "I can do it now.")
    (r,) = log.store.rows("SELECT * FROM reminders")
    assert (r["for_name"], r["text"], r["channel"], r["due"] is not None) == (
        "stacey",
        "I can do it now.",
        channel,
        due_now,
    )


ROOT = Path(__file__).parents[1]
FENCED_IN_WISH_MD = [
    ".github/wish.md",
    ".github/workflows/ci.yml",
    ".github/workflows/wish.yml",
    "deploy/install-pi.sh",
    "deploy/tars-system",
    "deploy/voice-assistant-telegram.service",
    "src/voice_assistant/updater.py",
    "src/voice_assistant/wishes.py",
    "src/voice_assistant/draft.py",
    "src/voice_assistant/homenet.py",
    "src/voice_assistant/browser/gate.py",
    "src/voice_assistant/browser/money.py",
    "src/voice_assistant/browser/vault.py",
]


def test_ci_fences_off_what_wish_md_tells_the_agent_not_to_change():
    import re

    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    (fenced,) = re.findall(r"^\s+FENCED: '(.+)'$", ci, re.MULTILINE)
    fence = re.compile(fenced)
    for path in FENCED_IN_WISH_MD:
        assert (ROOT / path).is_file() and fence.search(path), path  # moved, it'd no longer be fenced
    for path in ["src/voice_assistant/home.py", "src/voice_assistant/browser/errands.py", "tests/test_wishes.py",
                 "docs/wishes.md", "src/voice_assistant/wishes_more.py", "examples/deploy/x.py"]:  # fmt: skip
        assert not fence.search(path), path
    told = (ROOT / ".github/wish.md").read_text()
    for name in ["`.github/`", "`deploy/`", "`updater.py`", "`wishes.py`", "`draft.py`", "`homenet.py`",
                 "`browser/gate.py`", "`browser/money.py`", "`browser/vault.py`", "`wish fence`"]:  # fmt: skip
        assert name in told, name
    assert "startsWith(github.head_ref, 'claude/issue-')" in ci and "--no-renames" in ci
