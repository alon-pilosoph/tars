"""The brain's toolbox (toolbox.py): a provider's tools offered and called through it, each held back as it says."""

import pytest

from evals.house import make_house
from voice_assistant.home import Request
from voice_assistant.llm import LOOK_UP, LLMConfig, OpenAIChat, QuickChat, QuickService
from voice_assistant.toolbox import CHANGES, COLLECTS, READS, Tool, Toolbox, Toolset, Turn

from .conftest import fake_openai_chat
from .test_llm import fake_cerebras, reminder_tools

WEB = ["response.web_search_call.in_progress"]


def spec(name: str) -> dict:
    return {"type": "function", "name": name, "description": f"The {name} tool.", "parameters": {}}


class Garden:
    """A provider of two tools: one that reads the soil, and one that waters it."""

    def __init__(self):
        self.watered: list[tuple[dict, Request]] = []

    def water(self, args, turn: Turn) -> str:
        self.watered.append((args, turn.request))
        return "done: watered"

    def toolset(self) -> Toolset:
        soil = Tool(spec("moisture"), READS, lambda args, turn: "soil: 31%")
        tools = [soil, Tool(spec("water"), CHANGES, self.water)]
        return Toolset(tools, ["GARDEN RULES"], ["water the garden"], ["anything about the garden"])


def calling(*calls: tuple[str, dict], then: str = "Done.", search: list[str] = ()):
    """OpenAI's model, making `calls` on its first round, after any web `search`, and saying `then` once they're
    answered."""
    return fake_openai_chat(
        lambda m: then if m[-1].get("type") == "function_call_output" else "",
        lambda m: list(calls) if m[-1].get("role") == "user" else [],
        lambda m: list(search) if m[-1].get("role") == "user" else [],
    )


def answers(client) -> list[str]:
    return [i["output"] for i in client.requests[-1]["input"] if i.get("type") == "function_call_output"]


def test_a_provider_given_to_the_brain_is_offered_described_and_called(capsys):
    garden, client = Garden(), calling(("moisture", {}), ("water", {"zone": 1}))
    brain = OpenAIChat(client, LLMConfig(web_search=False, send=False), tools=[garden])
    asked = Request("alon", 0.9, by_voice=True)
    assert "".join(brain.stream_reply("how's the garden? water it", asked)) == "Done."
    first = client.requests[0]
    assert [t["name"] for t in first["tools"]] == ["moisture", "water"]
    assert "GARDEN RULES" in first["instructions"] and "water the garden" in first["instructions"]
    assert answers(client) == ["soil: 31%", "done: watered"]
    assert [(args, request.speaker) for args, request in garden.watered] == [({"zone": 1}, "alon")]
    assert '(tool water {"zone": 1}: done: watered)' in capsys.readouterr().out


def test_a_provider_is_offered_only_where_its_given():
    garden, openai = Garden(), fake_openai_chat(lambda m: "Watered.")
    quick = fake_cerebras(lambda m: LOOK_UP)
    cfg = LLMConfig(web_search=False, send=False, quick_tools=True, think_effort="")
    brain = QuickChat(openai, cfg, [QuickService("Cerebras", quick, "qwen")], tools=[garden])
    "".join(brain.stream_reply("water the garden"))
    own = quick.requests[0]
    assert "tools" not in own and "GARDEN RULES" not in own["messages"][0]["content"]
    assert "- anything about the garden" in own["messages"][0]["content"]  # handed over, to the model that has it
    assert [t["name"] for t in openai.requests[0]["tools"]] == ["moisture", "water"]
    chat = calling(("water", {}))  # a chat's brain, made without the garden
    "".join(OpenAIChat(chat, LLMConfig(web_search=False, send=False)).stream_reply("water the garden"))
    assert "tools" not in chat.requests[0] and answers(chat) == ["error: no tool called water"]
    assert garden.watered == []


def test_a_tool_that_changes_something_is_refused_after_a_web_search_and_reads_still_go():
    garden, client = Garden(), calling(("moisture", {}), ("water", {}), search=WEB)
    brain = OpenAIChat(client, LLMConfig(send=False), tools=[garden])
    "".join(brain.stream_reply("water the garden if it won't rain", Request("alon", 0.9, by_voice=True)))
    soil, watered = answers(client)
    assert soil == "soil: 31%" and watered.startswith("refused: this reply read the web")
    assert garden.watered == []


def test_a_change_waits_for_the_turn_to_go_ahead():
    garden = Garden()
    tools = Toolbox([garden.toolset()])
    withdrawn = Turn(Request(may_act=lambda timeout: False))
    assert tools.call(tools.get("water"), {}, withdrawn).startswith("error: the request was withdrawn")
    assert tools.call(tools.get("moisture"), {}, withdrawn) == "soil: 31%"
    assert garden.watered == []
    assert tools.call(tools.get("water"), {}, Turn(Request(may_act=lambda timeout: True))) == "done: watered"


def test_what_a_tool_collects_goes_on_the_turn_and_a_failure_is_said():
    def note(args, turn: Turn) -> str:
        turn.sent.append(args["title"])
        return "sent"

    def broken(args, turn: Turn) -> str:
        raise OSError("disk full")

    broke = Tool(spec("broken"), READS, broken, "error: {e!r}")
    tools = Toolbox([Toolset([Tool(spec("note"), COLLECTS, note), broke])])
    turn = Turn(Request(read_the_web=True))
    assert tools.call(tools.get("note"), {"title": "Eggs"}, turn) == "sent" and turn.sent == ["Eggs"]
    assert tools.call(tools.get("broken"), {}, turn) == "error: OSError('disk full')"


def test_each_model_is_offered_its_tools_in_the_order_it_always_was(tmp_path):
    house = make_house({"lock": "locked"}).tools
    cfg = LLMConfig(quick_tools=True)
    quick = fake_cerebras(lambda m: "Hi.")
    brain = QuickChat(None, cfg, [QuickService("Cerebras", quick, "qwen")], reminder_tools(tmp_path), home=house)
    reminding = ["remind", "cancel_reminder", "snooze_reminder"]
    assert [t.get("name", t["type"]) for t in brain._tools] == ["web_search", "send", *reminding, "home"]
    "".join(brain.stream_reply("hi"))
    assert [t["function"]["name"] for t in quick.requests[0]["tools"]] == [*reminding, "send", "home"]


def test_a_tool_has_to_say_what_it_does_and_needs_a_name_of_its_own():
    with pytest.raises(ValueError, match="has to say what it does"):
        Tool(spec("water"), "", lambda args, turn: "")
    with pytest.raises(ValueError, match="two tools are called water"):
        Toolbox([Garden().toolset(), Toolset([Tool(spec("water"), READS, lambda args, turn: "")])])
