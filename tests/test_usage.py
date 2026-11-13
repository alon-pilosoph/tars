"""What TARS used of the paid services (usage.py): a row for each call, for whoever's turn it was part of, and what
it cost, worked out from prices.toml when it's read."""

import threading
import types
from datetime import datetime

import pytest

from voice_assistant import usage
from voice_assistant.config import LLMConfig
from voice_assistant.llm import OpenAIChat
from voice_assistant.usage import LLM, STT, TTS, Meter, Prices

from .conftest import fake_openai_chat

NOW = datetime(2026, 11, 13, 21, 0).astimezone().timestamp()


@pytest.fixture
def meter(log):
    """A meter on the test's event log, installed for the test only."""
    made = Meter(log.store, clock=lambda: NOW)
    usage.install(made)
    yield made
    usage.install(None)


def rows(meter) -> list[dict]:
    meter.flush()
    return meter.store.rows("SELECT * FROM usage ORDER BY id")


def test_nothing_is_counted_without_a_meter(log):
    usage.install(None)
    usage.llm("openai", "gpt-6-luna", input=10)  # no meter: nothing happens, nothing breaks
    assert log.store.rows("SELECT * FROM usage") == []


def test_a_call_is_counted_for_the_turn_it_was_part_of(meter):
    with usage.turn("Alon", usage.TELEGRAM):
        usage.llm("openai", "gpt-6-luna", input=100, output=20, reasoning=5, cached=60, searches=1)
    usage.stt("deepgram", "nova-3", 3.5)  # outside a turn: the background's, nobody's
    [chat, heard] = rows(meter)
    assert (chat["person"], chat["channel"], chat["kind"]) == ("alon", "telegram", LLM)
    assert (chat["input"], chat["output"], chat["reasoning"], chat["cached"], chat["searches"]) == (100, 20, 5, 60, 1)
    assert (heard["person"], heard["channel"], heard["seconds"], heard["kind"]) == ("", "background", 3.5, STT)


def test_the_voice_names_its_speaker_partway_and_a_draft_on_its_own_thread_counts_for_them(meter):
    started, named = threading.Event(), threading.Event()

    def draft():
        started.set()
        named.wait(5)
        usage.llm("cerebras", "qwen-3.8-27b", input=40, output=8)  # its stream ends after the speaker is known

    with usage.turn(None, usage.VOICE):
        thread = threading.Thread(target=usage.carried(draft))
        thread.start()
        started.wait(5)
        usage.identified("Stacey")
        named.set()
        thread.join(5)
    [row] = rows(meter)
    assert (row["person"], row["channel"]) == ("stacey", "voice")


def test_a_thread_started_without_the_turn_is_nobodys(meter):
    with usage.turn("Alon", usage.TELEGRAM):
        thread = threading.Thread(target=lambda: usage.tts("deepgram", "aura-2-thalia-en", 42))
        thread.start()
        thread.join(5)
    [row] = rows(meter)
    assert (row["person"], row["channel"], row["characters"]) == ("", "background", 42)


def test_a_conversation_through_openai_is_counted_round_by_round(meter):
    """A turn with a tool call is two requests: two rows, each with what that request used, and the searches made."""
    client = fake_openai_chat(
        lambda m: "On it." if len(m) == 2 else "Done.",
        calls_for=lambda m: [("set_timer", {"minutes": 5})] if len(m) == 2 else [],
        search_for=lambda m: (
            ["response.web_search_call.in_progress", "response.web_search_call.completed"] if len(m) == 2 else []
        ),
        used=lambda m: (1200, 30, 10, 1000) if len(m) == 2 else (1300, 12, 0, 1200),
    )
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False))
    with usage.turn("Alon", usage.VOICE):
        "".join(brain.stream_reply("set a 5 minute timer"))
    got = [(r["service"], r["model"], r["input"], r["output"], r["reasoning"], r["cached"], r["searches"], r["person"])
           for r in rows(meter)]  # fmt: skip
    model = LLMConfig().model
    assert got == [
        ("openai", model, 1200, 30, 10, 1000, 1, "alon"),
        ("openai", model, 1300, 12, 0, 1200, 0, "alon"),
    ]


def test_a_response_that_says_nothing_of_its_use_is_counted_as_unreported(meter):
    brain = OpenAIChat(fake_openai_chat(lambda m: "Hi."), LLMConfig(web_search=False, send=False))
    "".join(brain.stream_reply("hello"))
    [row] = rows(meter)
    assert row["reported"] == 0 and row["input"] == 0


def test_a_stream_closed_before_its_end_is_counted_once_as_unreported(meter):
    class Stream:
        def __iter__(self):
            yield types.SimpleNamespace(type="response.output_text.delta", delta="Hi")
            yield types.SimpleNamespace(type="response.output_text.delta", delta=" there")

        def close(self):
            pass

    counted = usage.CountedResponses(Stream(), "openai", "gpt-6-luna")
    reading = iter(counted)
    next(reading)  # the reply's first words, then the listener cuts in
    counted.close()
    reading.close()
    assert [(r["reported"], r["model"]) for r in rows(meter)] == [(0, "gpt-6-luna")]


USED = types.SimpleNamespace(prompt_tokens=50, completion_tokens=9)


@pytest.mark.parametrize(
    "chunk",
    [
        types.SimpleNamespace(usage=USED),  # Cerebras
        types.SimpleNamespace(usage=None, x_groq=types.SimpleNamespace(usage=USED)),  # Groq
    ],
)
def test_a_chat_completions_stream_is_counted_from_its_last_chunk(meter, chunk):
    first = types.SimpleNamespace(usage=None, choices=[])
    list(usage.CountedChat(iter([first, chunk]), "cerebras", "qwen-3.8-27b"))
    [row] = rows(meter)
    assert (row["input"], row["output"], row["reported"]) == (50, 9, 1)


# The cost


PRICES = Prices(
    models={"gpt-6-luna": {"input": 0.10, "output": 0.50, "cache_read": 0.01}},
    search=0.01,
    speech={"nova-3": 0.0048},
    voices={"aura-2": 30.0, "flux-": 45.0},
    checked="2026-11-13",
)


def row(kind, model, **used):
    return {"kind": kind, "model": model, **dict.fromkeys(usage.COLUMNS, 0), **used}


def test_each_kind_is_priced_its_own_way():
    tokens = row(LLM, "gpt-6-luna", input=1_000_000, cached=500_000, output=100_000, searches=2)
    assert PRICES.of(tokens) == pytest.approx(0.5 * 0.10 + 0.5 * 0.01 + 0.1 * 0.50 + 2 * 0.01)
    assert PRICES.of(row(STT, "nova-3", seconds=600)) == pytest.approx(10 * 0.0048)
    assert PRICES.of(row(TTS, "aura-2-thalia-en", characters=10_000)) == pytest.approx(0.3)
    assert PRICES.of(row(TTS, "flux-wes-en", characters=10_000)) == pytest.approx(0.45)


def test_what_has_no_price_is_not_priced_rather_than_free():
    assert PRICES.of(row(LLM, "some-new-model", input=1000)) is None
    assert PRICES.of(row(STT, "whisper-9", seconds=60)) is None
    assert PRICES.of(row(TTS, "alloy", characters=10)) is None


def test_the_month_adds_up_by_service_and_by_person(meter):
    with usage.turn("Alon", usage.TELEGRAM):
        usage.llm("openai", "gpt-6-luna", input=2_000_000, output=0)
    with usage.turn("Stacey", usage.VOICE):
        usage.stt("deepgram", "nova-3", 120)
        usage.llm("openai", "unknown-model", input=5)
    meter.flush()
    m = usage.month(meter.store, prices=PRICES, now=NOW)
    assert m.month == "2026-11" and m.cost == pytest.approx(0.2 + 2 * 0.0048) and m.unpriced == 1
    assert (m.by_service[0].name, m.by_service[0].calls) == ("openai gpt-6-luna", 1)
    assert {line.name: round(line.cost, 4) for line in m.by_person} == {"alon": 0.2, "stacey": 0.0096}
    said = usage.report(m)
    assert "2026-11" in said and "$0.21" in said and "no price" in said


def test_another_month_is_left_out(meter):
    usage.llm("openai", "gpt-6-luna", input=1_000_000)
    meter.flush()
    assert usage.month(meter.store, "2026-10", PRICES).by_service == []


def test_old_rows_are_pruned(meter):
    usage.llm("openai", "gpt-6-luna", input=1)
    meter.flush()
    assert meter.prune(now=NOW + (usage.KEEP_DAYS - 1) * 86400) == 0
    assert meter.prune(now=NOW + (usage.KEEP_DAYS + 1) * 86400) == 1


def test_the_shipped_price_file_reads_and_prices_the_defaults():
    prices = usage.load_prices()
    assert prices.models["gpt-6-luna"]["input"] > 0 and prices.speech["nova-3"] > 0
    assert prices.of(row(TTS, "flux-wes-en", characters=1000)) is not None


def test_a_row_that_cant_be_written_never_reaches_the_caller(log, capsys):
    broken = Meter(types.SimpleNamespace(write=lambda *a: (_ for _ in ()).throw(OSError("disk full"))))
    usage.install(broken)
    try:
        usage.llm("openai", "gpt-6-luna", input=1)
        broken.flush()
    finally:
        usage.install(None)
    assert "couldn't keep what a call used" in capsys.readouterr().out
