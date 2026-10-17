"""evals: the scenario files load, the checks say what's wrong, and a run plays turns the way the assistant does."""

import json
import types
from datetime import UTC, datetime

import httpx
import pytest
from openai import APIStatusError, RateLimitError

from evals.report import compare, gate, wilson
from evals.runner import play, prompt
from evals.scenarios import Scenario, Seed, Turn, _turn, check_reminder_text, check_spoken, check_text, load
from voice_assistant.config import LLMConfig
from voice_assistant.reminders import ADD, REMINDER, Change, NewReminder

from .conftest import FakeStream, ToolCallStream, fake_openai_chat

TIMER_10 = {"kind": "timer", "text": None, "for": None, "in_minutes": 10, "day": None, "time": None}
TIMER_10 |= {"when_back": False, "wait_for_ack": True}


def test_every_scenario_file_loads_with_unique_ids():
    scenarios = load()
    assert len(scenarios) >= 20
    assert len({s.id for s in scenarios}) == len(scenarios)
    assert all(s.tags and s.about for s in scenarios)


@pytest.mark.parametrize(
    "toml, error",
    [
        ('[[scenario]]\nid="a"\nabout="x"\n[[scenario.turn]]\nsay="hi"\nwhat="?"', "unknown setting"),
        ('[[scenario]]\nid="a"\nabout="x"\n[[scenario.turn]]\nsay="hi"\nroute="elsewhere"', "isn't one of"),
        (
            (
                '[[scenario]]\nid="a"\nabout="x"\n[[scenario.turn]]\nsay="hi"\n'
                'reminders=[{action="cancel", ref="pasta"}]'
            ),
            "isn't set before it",
        ),
        ('[[scenario]]\nid="a"\nabout="x"\n[[scenario.turn]]\nsay="hi"\nactive=[{day="friday"}]', "needs a time"),
        ('[[scenario]]\nid="a"\nabout="x"', "at least one turn"),
    ],
)
def test_a_scenario_file_with_a_mistake_is_refused(tmp_path, toml, error):
    (tmp_path / "bad.toml").write_text(toml)
    with pytest.raises(ValueError, match=error):
        load(tmp_path)


@pytest.mark.parametrize(
    "said, raw, wrong",
    [
        ("Pasta timer, eight minutes.", "", ""),
        ("Done. <reminder id=2>", "", "a tag"),
        ("It's on bbc.co.uk today.", "", "web address"),
        ("Here's the plan. Here's the plan.", "", ""),  # too short to count as said twice
        ("I set the timer for you. I set the timer for you.", "", "twice"),
        ("Eggs, milk.", "- eggs\n- milk", "a list"),
        ("", "", "said nothing"),
        ("Less than look-up.", "&lt;look-up&gt;", "a tag"),  # escaped: TARS wouldn't recognize it
    ],
)
def test_what_tars_shouldnt_say_aloud(said, raw, wrong):
    found = check_spoken(said, "quick", raw)
    assert (wrong in " ".join(found)) if wrong else found == []


def test_says_and_never_says_ignore_case():
    turn = _turn({"say": "x", "says": ["canberra"], "never_says": ["sydney"]}, "test")
    assert check_text(turn, "Canberra.") == []
    assert check_text(turn, "Sydney, not canberra.") == ["says /sydney/"]


def test_the_prompt_is_tagged_as_the_assistant_tags_it():
    assert prompt(Turn("hi")) == "[Speaker: alon] hi"
    assert prompt(Turn("and Canada?", tag="follow-up")).startswith("[Follow-up, no wake word] [Speaker: alon]")


def quick(script):
    """A Chat Completions client whose every request is answered by `script(messages)`: text, a list of tool calls
    [(name, arguments)], or an error to raise."""

    def create(**kw):
        reply = script(kw["messages"])
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, list) and kw.get("tool_choice") != "none":
            return ToolCallStream(reply)
        return FakeStream(reply if isinstance(reply, str) else "")

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


def after_tool(messages) -> bool:
    return messages[-1].get("role") == "tool" or messages[-1].get("type") == "function_call_output"


CFG = LLMConfig(quick_tools=True, send=True, web_search=True, think_effort="high")


def run(turns, quick_script, openai_reply="From OpenAI.", openai_calls=None, **scenario):
    s = Scenario("test", "a test", [_turn(t, "test") for t in turns], ["test"], **scenario)
    openai = fake_openai_chat(lambda m: openai_reply, openai_calls)
    return play(s, CFG, quick(quick_script), "qwen", openai), openai


def test_a_change_a_hand_off_wasnt_meant_to_make_is_caught():
    # Qwen sets the timer, then hands the weather over; OpenAI sets another one nobody asked for.
    def qwen(messages):
        return "<look-up>" if after_tool(messages) else [("remind", TIMER_10)]

    asked = []

    def another(messages):  # on its first request only: its input already ends with Qwen's tool result
        asked.append(1)
        return [("remind", TIMER_10 | {"in_minutes": 15})] if len(asked) == 1 else []

    turn = {
        "say": "set a timer for ten minutes and what's the weather?",
        "route": "look-up",
        "reminders": [{"kind": "timer", "in_minutes": 10}],
    }
    result, _ = run([turn], qwen, "Set, and sunny tomorrow.", another)
    (t,) = result.turns
    assert t.route == "look-up"
    assert [w for w in t.wrong if w.startswith("also did: add timer")]


def test_a_later_turn_cancels_what_an_earlier_one_set():
    def qwen(messages):
        asked = messages[-1]["content"] if not after_tool(messages) else ""
        if after_tool(messages):
            return "Done."
        if "cancel" in asked:
            return [("cancel_reminder", {"id": 1})]
        return [("remind", TIMER_10)]

    turns = [
        {"say": "timer for ten minutes", "route": "quick", "reminders": [{"in_minutes": 10, "ref": "t"}]},
        {"say": "cancel that", "reminders": [{"action": "cancel", "ref": "t"}], "active": []},
    ]
    result, openai = run(turns, qwen)
    assert result.ok, [t.wrong for t in result.turns]
    assert openai.requests == []


def test_a_skipped_turn_is_forgotten_and_changes_nothing():
    def qwen(messages):
        if "feed the cat" in messages[-1]["content"]:
            return "<skip>"
        assert not any("feed the cat" in str(m.get("content")) for m in messages)  # forgotten by the next turn
        return "Canberra."

    turns = [
        {"say": "Stacey, did you feed the cat?", "tag": "follow-up", "route": "skip", "active": []},
        {"say": "capital of Australia?", "route": "quick", "says": ["Canberra"]},
    ]
    result, _ = run(turns, qwen)
    assert result.ok, [t.wrong for t in result.turns]


def test_an_ack_marks_the_reminder_said_acknowledged():
    turn = {"say": "got it", "tag": "reminder", "route": "ack", "active": []}
    result, _ = run([turn], lambda m: "<ack>", seeds=[Seed("pills", "reminder", "pills", 1, "alon", said=True)])
    assert result.ok, result.turns[0].wrong
    assert result.turns[0].said == "Got it."


def test_cited_sources_are_checked_as_heard_not_as_written():
    turn = {"say": "weather?", "route": "look-up"}
    result, _ = run([turn], lambda m: "<look-up>", "Sunny. ([BBC](https://bbc.co.uk/weather))")
    assert result.ok, result.turns[0].wrong
    assert result.turns[0].said == "Sunny."


def test_a_failure_sends_the_turn_to_openai_and_the_service_sits_out_its_cooldown():
    turns = [{"say": "hi", "quick": "down", "route": "fallback"}, {"say": "and now?", "route": "fallback"}]
    result, openai = run(turns, lambda m: "From Qwen.")
    assert result.ok, [t.wrong for t in result.turns]
    assert len(openai.requests) == 2


def test_a_real_rate_limit_is_not_a_result():
    request = httpx.Request("POST", "https://api.cerebras.ai")
    limited = RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)
    result, _ = run([{"say": "hi", "route": "quick"}], lambda m: limited)
    assert result.limited and not result.ok


def test_time_between_turns_comes_from_the_scenario_not_the_wall_clock():
    seen = []

    def qwen(messages):
        seen.append([m["content"] for m in messages[1:]])
        return "Noted."

    turns = [{"say": "my favourite colour is green"}, {"say": "what is it?", "after_minutes": 15}]
    result, _ = run(turns, qwen)
    assert result.ok, [t.wrong for t in result.turns]
    assert len(seen[-1]) == 1  # memory_minutes (10) had passed: the brain started fresh


def usage_chunk():
    """The last chunk of a streamed chat completion: no text, the request's usage."""
    usage = types.SimpleNamespace(prompt_tokens=100, completion_tokens=20, completion_tokens_details=None)
    return types.SimpleNamespace(choices=[], usage=usage)


def test_tokens_are_counted_by_model_without_changing_the_requests():
    asked = []

    def create(**kw):
        asked.append(kw)
        return [*FakeStream("Canberra."), usage_chunk()]

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    s = Scenario("t", "a test", [_turn({"say": "capital?"}, "t")], ["t"])
    result = play(s, CFG, client, "qwen", fake_openai_chat(lambda m: "x"))
    assert result.usage == {"qwen": {"requests": 1, "input": 100, "output": 20, "reasoning": 0, "searches": 0}}
    assert "stream_options" not in asked[0]


def test_wilson_intervals_are_honest_at_the_edges():
    low, high = wilson(3, 3)
    assert 0.4 < low < 0.5 and high == 1.0  # 3/3 is no proof of 100%
    assert wilson(0, 0) == (0.0, 1.0)


def summary_of(**scenarios):
    return {"scenarios": {k: {"right": r, "runs": n, "fingerprint": f} for k, (r, n, f) in scenarios.items()}}


def test_a_scenario_is_worse_only_when_it_drops_by_half_its_runs_and_its_definition_didnt_change():
    before = summary_of(a=(3, 3, "x"), b=(3, 3, "x"), c=(3, 3, "x"), d=(0, 3, "x"))
    now = summary_of(a=(2, 3, "x"), b=(1, 3, "x"), c=(0, 3, "y"), d=(3, 3, "x"), e=(3, 3, "x"))
    assert {c.scenario: c.verdict for c in compare(now, before)} == {
        "a": "same",  # one run either way is noise
        "b": "worse",
        "c": "changed",  # a different definition: not comparable
        "d": "better",
        "e": "new",
    }


def test_the_gate_fails_on_a_low_score_or_a_regression_only_when_asked():
    before, now = summary_of(a=(3, 3, "x")), summary_of(a=(1, 3, "x")) | {"right": [1, 3]}
    changes = compare(now, before)
    assert gate(now, changes, None, False) == []
    assert gate(now, changes, 0.5, False) == ["1/3 runs right is under 50%"]
    assert gate(now, changes, None, True) == ["a got worse: 3/3 -> 1/3"]


def test_every_run_is_kept_turn_by_turn(tmp_path, monkeypatch):
    import evals.__main__ as cli

    monkeypatch.setattr(cli, "RUNS", tmp_path)
    s = Scenario("t", "a test", [_turn({"say": "hi"}, "t")], ["t"], fingerprint="abc")
    result = play(s, CFG, quick(lambda m: "Hello."), "qwen", fake_openai_chat(lambda m: "x"))
    folder = cli.keep_transcripts(datetime(2026, 10, 17, 12, tzinfo=UTC), [s], [result], {"right": [1, 1]})
    (row,) = [json.loads(line) for line in (folder / "runs.jsonl").read_text().splitlines()]
    assert row["scenario"] == "t" and row["fingerprint"] == "abc" and row["ok"]
    assert row["turns"][0]["prompt"] == "[Speaker: alon] hi" and row["turns"][0]["said"] == "Hello."
    assert json.loads((folder / "summary.json").read_text()) == {"right": [1, 1]}


def test_a_service_refusing_the_account_isnt_a_result():
    # OpenAI out of credits: the hand-off fails, and that's the account's doing, not TARS's.
    from openai import APIError

    def broke(**kw):
        raise APIError("You have no credits remaining. Add credits to continue.", request=None, body=None)

    openai = types.SimpleNamespace(responses=types.SimpleNamespace(create=broke))
    s = Scenario("t", "a test", [_turn({"say": "weather?", "route": "look-up"}, "t")], ["t"])
    result = play(s, CFG, quick(lambda m: "<look-up>"), "qwen", openai)
    assert "no credits" in result.unavailable and not result.counted and not result.ok


def test_the_quick_service_refusing_the_account_isnt_a_result_either():
    request = httpx.Request("POST", "https://api.cerebras.ai")
    unpaid = APIStatusError("402 Payment required", response=httpx.Response(402, request=request), body=None)
    s = Scenario("t", "a test", [_turn({"say": "hi", "route": "quick"}, "t")], ["t"])
    result = play(s, CFG, quick(lambda m: unpaid), "qwen", fake_openai_chat(lambda m: "From OpenAI."))
    assert result.unavailable and not result.counted


def test_once_the_account_is_refused_the_runs_left_are_skipped_and_not_counted():
    import evals.__main__ as cli
    from evals.report import summarize
    from evals.runner import RunResult

    s = Scenario("t", "a test", [_turn({"say": "hi"}, "t")], ["t"])
    stop, calls = cli.Stop(), []

    def run(scenario):
        calls.append(1)
        return RunResult(scenario.id, [], unavailable="AuthenticationError: bad key")

    results = [cli.run_until_answered(s, run, stop) for _ in range(3)]
    assert len(calls) == 1 and stop.why.startswith("AuthenticationError")
    summary = summarize([s], results)
    assert summary["right"] == [0, 0] and summary["not_run"] == 3


def test_a_reply_that_acks_and_puts_off_a_reminder_leaves_it_set_as_the_assistant_does():
    def qwen(messages):
        return "<ack> Pushed." if after_tool(messages) else [("snooze_reminder", {"id": 1, "minutes": 10})]

    turn = {"say": "remind me again in ten minutes", "tag": "reminder", "active_has": [{"in_minutes": 10}]}
    result, _ = run([turn], qwen, seeds=[Seed("pills", "reminder", "pills", 1, "alon", said=True)])
    assert result.ok, result.turns[0].wrong


@pytest.mark.parametrize(
    "text, wrong",
    [
        ("take your pills", False),
        ("Alon, a reminder: take your pills.", True),
        ("A message from Stacey: dinner's at eight", True),
        ("a reminder about the dentist", False),
    ],
)
def test_a_reminder_text_that_copies_what_tars_says_around_it_is_caught(text, wrong):
    assert bool(check_reminder_text([Change(ADD, NewReminder(REMINDER, text))])) is wrong


def test_a_runs_cost_is_its_tokens_at_each_models_price_and_its_searches():
    from evals.costs import Prices, cost

    prices = Prices({"luna": {"input": 0.10, "output": 0.50, "fast": 2.0}}, search=0.01, checked="today")
    use = {"luna": {"input": 1_000_000, "output": 100_000, "searches": 10}, "qwen": {"input": 5_000_000}}
    assert cost(use, prices) == pytest.approx(0.10 + 0.05 + 0.10)
    assert cost(use, prices, fast=True) == pytest.approx(0.20 + 0.10 + 0.10)  # searches cost the same on any tier
    assert prices.unpriced(use) == ["qwen"]


def test_the_estimate_comes_from_earlier_runs_of_each_scenario(tmp_path):
    from evals.costs import estimate

    (tmp_path / "2026-10-17-100000").mkdir()
    old = {"scenario": "a", "usage": {"luna": {"input": 100, "searches": 2}}, "turns": []}
    older = {"scenario": "b", "usage": {"luna": {"input": 300}}, "turns": [{"route": "look-up"}, {"route": "quick"}]}
    lines = [json.dumps(r) for r in (old, older)]
    (tmp_path / "2026-10-17-100000" / "runs.jsonl").write_text("\n".join(lines) + "\n")
    a, b, c = (Scenario(i, "x", [_turn({"say": "hi"}, "t")], ["t"]) for i in "abc")
    use, unknown = estimate([a, b, c], runs=2, folder=tmp_path)
    assert unknown == ["c"]  # no earlier runs: estimated as the average of the others
    assert use["luna"]["input"] == pytest.approx(2 * (100 + 300 + 200))
    assert use["luna"]["searches"] == pytest.approx(2 * (2 + 1 + 1.5))  # b: one search per turn OpenAI answered


def test_the_budget_stops_the_runs_once_its_spent():
    import evals.__main__ as cli
    from evals.costs import Prices
    from evals.runner import RunResult

    spent = cli.Spent(Prices({}, search=0.01, checked="today"), fast=False, budget=0.05)
    run = RunResult("a", [], usage={"luna": {"searches": 3}})
    assert spent.add(run) == ""
    assert spent.add(run).startswith("over the budget of $0.05")


def test_web_searches_are_counted():
    from evals.runner import Usage, _Counted

    usage = Usage()
    events = [types.SimpleNamespace(type="response.web_search_call.completed")] * 2
    list(_Counted(events, usage, "luna"))
    assert usage.by_model["luna"]["searches"] == 2


def test_a_reply_too_long_for_the_first_service_is_written_on_the_roomier_one():
    asked = []

    def create(**kw):
        asked.append(kw["max_completion_tokens"])
        if kw["max_completion_tokens"] < 1000:
            return FakeStream("", finish="length")
        return FakeStream("Sent. It's on the TARS page.")

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    s = Scenario("t", "a test", [_turn({"say": "send me the plan", "route": "quick"}, "t")], ["t"])
    services = [("Cerebras, as Groq", 500), ("Cerebras", 2000)]
    result = play(s, CFG, client, "qwen", fake_openai_chat(lambda m: "x"), services)
    assert result.ok, result.turns[0].wrong
    assert asked == [500, 2000] and result.turns[0].service == "Cerebras"
