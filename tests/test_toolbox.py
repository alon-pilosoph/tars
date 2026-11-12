"""The brain's toolbox (toolbox.py): a provider's tools offered and called through it, each held back as it says."""

import pytest

from voice_assistant.home import Request
from voice_assistant.toolbox import CHANGES, COLLECTS, READS, Tool, Toolbox, Toolset, Turn


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


def test_a_tool_has_to_say_what_it_does_and_needs_a_name_of_its_own():
    with pytest.raises(ValueError, match="has to say what it does"):
        Tool(spec("water"), "", lambda args, turn: "")
    with pytest.raises(ValueError, match="two tools are called water"):
        Toolbox([Garden().toolset(), Toolset([Tool(spec("water"), READS, lambda args, turn: "")])])
