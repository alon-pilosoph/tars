"""The hub's boundary (hub.py, docs/house-hub.md): the house tools reach every device through it, and answer as
they did when they held the devices themselves."""

import ast
from pathlib import Path

import pytest

from voice_assistant.home import HomeError, HomeTools, Request
from voice_assistant.hub import HubDevice, LocalHub, behind_hub

from .test_home import ALON, args, house

SRC = Path(__file__).parents[1] / "src" / "voice_assistant"


def both():
    """The same house twice: one holding its devices, one with them behind the hub."""
    held, hubbed = house(lock="locked", boiler="off"), house(lock="locked", boiler="off")
    hub = behind_hub(hubbed.tools)
    return held, hubbed, hub


def test_every_device_is_reached_through_the_hub_and_the_tool_is_the_same():
    held, hubbed, hub = both()
    assert all(isinstance(d, HubDevice) for d in hubbed.tools.devices.values())
    assert list(hubbed.tools.devices) == list(held.tools.devices) and hubbed.tools.tool == held.tools.tool
    assert hubbed.tools.rules() == held.tools.rules()
    assert set(hub.describe()) == set(held.tools.devices)


@pytest.mark.parametrize(
    "call",
    [args("boiler", "status"), args("boiler", "on"), args("front door", "lock"), args("everything", "status")],
)
def test_a_call_through_the_hub_answers_as_the_device_itself_does(call):
    held, hubbed, _ = both()
    assert hubbed.tools.call(call, ALON) == held.tools.call(call, ALON)
    assert hubbed.ha.calls == held.ha.calls


def test_the_rules_stay_with_the_house_tools_on_the_brains_side():
    """A turn that read the web changes nothing, and an unlock is held to the voice's rules, before the hub's asked."""
    _, hubbed, hub = both()
    asked = []
    real = hub.call
    hub.call = lambda *a: asked.append(a) or real(*a)
    webbed = Request("alon", 0.9, by_voice=True, heard="turn on the boiler", read_the_web=True)
    assert hubbed.tools.call(args("boiler", "on"), webbed).startswith("refused")
    stranger = Request("stacey", 0.9, by_voice=True, heard="unlock the front door")
    assert hubbed.tools.call(args("front door", "unlock"), stranger).startswith("refused")
    assert asked == [] and hubbed.ha.calls == []


def test_a_devices_own_error_comes_back_through_the_hub():
    class Broken(HubDevice.__mro__[1]):  # a Device
        actions = ("status",)

        def status(self):
            raise HomeError("it isn't answering")

    hub = LocalHub({"fan": Broken("fan")})
    (described,) = hub.describe().values()
    with pytest.raises(HomeError, match="isn't answering"):
        HubDevice(hub, described).status()
    with pytest.raises(ValueError):
        hub.ask("fan", "call")  # only reads go through ask


def test_what_the_brain_is_told_of_a_device_is_what_doesnt_change():
    _, _, hub = both()
    door = hub.describe()["front door"]
    assert (door.name, door.entity, "unlock" in door.actions) == ("front door", "lock.front_door", True)


# The boundary itself: what the brain's side of TARS may import. A device client is the code that talks to something
# on the home network; only the hub's side builds and holds them.
CLIENTS = {"hass", "androidtv", "routers", "mosquitto", "spotify", "speakerphone", "haconfig"}
HUB_SIDE = {"hub", "check", "wiring", "entrypoints", "presence", "doors", "autolock", "plugins", "mic_mute", "volume"}
HUB_SIDE |= CLIENTS
NOT_YET: set[tuple[str, str]] = set()  # what's still to move behind the hub, by (module, client)


def imported(path: Path) -> set[str]:
    """What a module imports from TARS's own, leaving out what's only for type checking (if TYPE_CHECKING:)."""
    tree = ast.parse(path.read_text())
    typing_only = {
        id(n) for block in ast.walk(tree)
        if isinstance(block, ast.If) and getattr(block.test, "id", "") == "TYPE_CHECKING"
        for n in ast.walk(block)
    }  # fmt: skip
    found = set()
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                found.add(node.module.split(".")[0])
            else:
                found |= {alias.name for alias in node.names}
    return found


def test_no_module_on_the_brains_side_imports_a_device_client():
    crossing = sorted(
        (path.stem, client)
        for path in SRC.glob("*.py")
        if path.stem not in HUB_SIDE
        for client in imported(path) & CLIENTS
        if (path.stem, client) not in NOT_YET
    )
    assert crossing == [], f"reach these through the hub (hub.py): {crossing}"
    assert HomeTools  # the brain's side of the house


def test_the_home_assistant_tool_reaches_home_assistant_through_the_hub():
    from voice_assistant.hub import HA_REQUESTS, HubHomeAssistant

    both()  # the house tools behind the hub build as before
    asked = []

    class Hass:
        def __getattr__(self, request):
            return lambda *a, **k: asked.append(request) or []

    hub = LocalHub({}, Hass())
    ha = HubHomeAssistant(hub)
    assert ha.states() == [] and asked == ["states"]
    with pytest.raises(AttributeError):
        ha.token  # noqa: B018 - only the requests the tool makes cross, never the connection's own parts
    with pytest.raises(ValueError):
        hub.home_assistant("delete_everything")
    assert "call_service" in HA_REQUESTS and "token" not in HA_REQUESTS


def test_what_happens_at_home_reaches_every_handler_and_one_that_fails_stops_none(capsys):
    from voice_assistant.hub import EVENTS, HubEvents

    events, told = HubEvents(), []
    events.on("came_in", lambda names, door: 1 / 0)
    events.on("came_in", lambda names, door: told.append((names, door)))
    events.on("home", None)  # nothing to do with it: left out
    events.emit("came_in", ["alon"], "front door")
    assert told == [(["alon"], "front door")] and "wasn't handled" in capsys.readouterr().out
    assert not events.wanted("home") and events.wanted("came_in")
    assert set(EVENTS) == {"home", "came_in", "said", "posted", "maintainers"}
