"""Whether a chat message after a quiet spell carries its conversation on (threads.py)."""

import pytest

from voice_assistant.threads import PROMPT, RECENT_TURNS, Continuity, messages, verdict_of

TURNS = [("Alon", "my last Wolt order?"), ("TARS", "I couldn't sign in to Wolt: it wanted a robot check.")]


@pytest.mark.parametrize(
    "said, verdict",
    [("continued", "continued"), ("New", "new"), ("new.", "new"), ("<think>it's about Wolt</think>\ncontinued",
     "continued"), ("It's new", "new"), ("maybe", "")],
)  # fmt: skip
def test_the_answer_is_its_last_word(said, verdict):
    assert verdict_of(said) == verdict


@pytest.mark.parametrize("said, carries", [("continued", True), ("new", False), ("I can't tell", True)])
def test_only_new_starts_a_new_conversation(said, carries):
    assert Continuity(lambda m: said).continues(TURNS, "try again", 7200, "Alon") is carries


def test_the_model_is_told_to_say_one_word_and_reads_the_last_turns_and_how_long_it_was_quiet():
    many = [("Alon", f"question {i}") for i in range(20)]
    system, asked = messages(many, "and the next?", 3 * 86400, "Alon")
    assert system == {"role": "system", "content": PROMPT} and "one word: continued or new" in PROMPT
    shown = asked["content"]
    assert shown.count("Alon: question") == RECENT_TURNS and "question 19" in shown and "question 13" not in shown
    assert shown.endswith("3 days later, the new message:\nAlon: and the next?")


def test_a_long_turn_is_cut():
    (_, asked) = messages([("TARS", "word " * 500)], "ok", 7200, "Alon")
    assert len(asked["content"]) < 700 and "…" in asked["content"]


def test_nothing_before_is_nothing_to_carry_on():
    asked = []
    assert not Continuity(lambda m: asked.append(m) or "continued").continues([], "hi", 7200, "Alon")
    assert asked == []


def test_the_quick_model_is_asked_in_order_and_openai_without_one(monkeypatch, tmp_path):
    import dataclasses
    import types
    from pathlib import Path

    from voice_assistant import wiring
    from voice_assistant.config import load_config

    cfg = load_config(Path(__file__).parents[1] / "config.toml", local=False)
    asked = []

    def completions(fails=False):
        def create(**kw):
            asked.append(kw)
            if fails:
                raise TimeoutError("Groq")
            message = types.SimpleNamespace(content="new")
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

    services = [types.SimpleNamespace(name="Groq", model="q1", client=completions(fails=True)),
                types.SimpleNamespace(name="Cerebras", model="q2", client=completions())]  # fmt: skip
    monkeypatch.setattr(wiring, "quick_services", lambda llm, env: services)
    check = wiring.make_continuity(cfg, tmp_path / ".env", None)
    assert not check.continues(TURNS, "what's the weather?", 7200, "Alon")
    assert [kw["model"] for kw in asked] == ["q1", "q2"]
    assert asked[1]["max_completion_tokens"] == wiring.CONTINUITY_MAX_TOKENS and "stream" not in asked[1]

    monkeypatch.setattr(wiring, "quick_services", lambda llm, env: [])
    sent = []
    client = types.SimpleNamespace(responses=types.SimpleNamespace(
        create=lambda **kw: sent.append(kw) or types.SimpleNamespace(output_text="continued")))  # fmt: skip
    assert wiring.make_continuity(cfg, tmp_path / ".env", client).continues(TURNS, "again", 7200, "Alon")
    assert sent[0]["model"] == cfg.llm.model and sent[0]["instructions"] == PROMPT
    assert wiring.make_continuity(cfg, tmp_path / ".env", None) is None
    off = dataclasses.replace(cfg, telegram=dataclasses.replace(cfg.telegram, keep_days=0))
    assert wiring.make_continuity(off, tmp_path / ".env", client) is None
