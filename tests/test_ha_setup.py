"""Home Assistant's setup, worked by TARS: what it found added, a step that wants a password handed to Home
Assistant's own page, a removal or an undo only on a second message, every change kept so it can be undone, and only
for the people allowed."""

import pytest

from voice_assistant.ha_setup import HASetup
from voice_assistant.home import HomeError, Request

ALON = Request(speaker="Alon", by_chat=True, heard="what did Home Assistant find?")


class FakeHA:
    def __init__(self):
        self.flows = {
            "f1": [{"type": "form", "flow_id": "f1", "handler": "dlna_dmr", "step_id": "confirm", "data_schema": []},
                   {"type": "create_entry", "flow_id": "f1", "handler": "dlna_dmr", "title": "Living room TV"}],
            "f2": [{"type": "external", "flow_id": "f2", "handler": "smartthings", "step_id": "auth"}],
            "f3": [{"type": "form", "flow_id": "f3", "handler": "shelly", "step_id": "credentials",
                    "data_schema": [{"name": "username", "type": "string"}, {"name": "password", "type": "string"}]}],
        }  # fmt: skip
        self.answered, self.removed, self.renamed = [], [], []
        self.entries_ = [{"entry_id": "e1", "domain": "nuki", "title": "Home door", "state": "loaded"}]

    def discovered(self):
        return [
            {
                "flow_id": "f1",
                "handler": "dlna_dmr",
                "context": {"source": "ssdp", "title_placeholders": {"name": "TV"}},
            }
        ]

    def answer_setup(self, flow, answers):
        self.answered.append((flow, answers))
        return self.flows[flow].pop(0) if len(self.flows[flow]) > 1 else self.flows[flow][0]

    def start_setup(self, handler):
        flow = {"shelly": "f3", "smartthings": "f2"}.get(handler)
        if flow is None:
            raise HomeError(f"Home Assistant has no integration {handler}")
        return self.flows[flow][0]

    def entries(self):
        return self.entries_

    def entity_entries(self):
        return [
            {"entity_id": "lock.home_door", "config_entry_id": "e1"},
            {"entity_id": "sensor.door_battery", "config_entry_id": "e1"},
        ]

    def remove_entry(self, entry):
        self.removed.append(entry)
        self.entries_ = []

    def devices(self):
        return [{"id": "d1", "name": "Nuki", "config_entries": ["e1"], "area_id": "hall"}]

    def area_list(self):
        return [{"area_id": "hall", "name": "Hall"}, {"area_id": "bedroom", "name": "Bedroom"}]

    def update_device(self, device, **changes):
        self.renamed.append((device, changes))

    def link(self, path):
        return f"http://homepi.local:8123{path}"


class FakeHistory:
    def __init__(self):
        self.commits, self.restored = [], []

    def ready(self):
        return True

    def commit(self, message):
        self.commits.append(message)
        return f"{len(self.commits):08x}"

    def history(self, count):
        from voice_assistant.haconfig import Change

        return [Change(f"{i + 1:08x}deadbeef", 0.0, m, ()) for i, m in reversed(list(enumerate(self.commits)))][:count]

    def restore(self, commit):
        self.restored.append(commit)
        return "abcdef12"


@pytest.fixture
def setup():
    ha, history = FakeHA(), FakeHistory()
    s = HASetup(ha, ["Alon"], history, listed={"lock.home_door"}, clock=lambda: 1000.0)
    s.ha, s.history = ha, history
    return s


def do(setup, action, request=ALON, target="", query="", data=None):
    return setup.call(action, target, query, data, request)


def test_what_it_found_is_listed_and_added_once_they_say_yes(setup):
    assert "flow f1: dlna_dmr (TV), found by ssdp" in do(setup, "discovered")
    asks = do(setup, "setup", target="f1")
    assert "it only asks to confirm" in asks and "target f1, data {}" in asks
    added = do(setup, "setup", target="f1", data="{}")
    assert (
        added == "done: added Living room TV (dlna_dmr) to Home Assistant; kept as change 00000001, so it can be undone"
    )
    assert setup.history.commits == ["Added Living room TV (dlna_dmr), by TARS for Alon"]


def test_a_password_or_a_sign_in_is_finished_on_home_assistants_own_page(setup):
    form = do(setup, "setup", query="shelly")
    assert (
        "asks for a password or code" in form
        and "http://homepi.local:8123/config/integrations/dashboard/add?domain=shelly" in form
    )
    assert "sign-in in a browser" in do(setup, "setup", query="smartthings")
    refused = do(setup, "setup", target="f3", data='{"username": "admin", "password": "hunter2"}')
    assert refused.startswith("refused: a password or code is never given through TARS")
    assert setup.ha.answered == []  # the password never reached Home Assistant through TARS


def test_removing_takes_a_second_message_says_what_breaks_and_is_kept(setup):
    first = do(setup, "remove", target="e1")
    assert first.startswith("not yet: removing nuki (Home door) takes out its 2 entities")
    assert "These, which TARS works, would stop: lock.home_door." in first and "can be undone" in first
    assert do(setup, "remove", target="e1").startswith("not yet")  # the same message again isn't a yes
    assert setup.ha.removed == []
    yes = Request(speaker="Alon", by_chat=True, heard="yes, remove it")
    assert do(setup, "remove", yes, target="e1").startswith("done: removed nuki (Home door)")
    assert setup.ha.removed == ["e1"]
    assert setup.history.commits == ["Changes made in Home Assistant", "Removed nuki (Home door), by TARS for Alon"]


def test_someone_else_cant_confirm_a_removal_and_only_maintainers_may_change_it(setup):
    do(setup, "remove", target="e1")
    stacey = Request(speaker="Stacey", by_chat=True, heard="yes")
    assert do(setup, "remove", stacey, target="e1").startswith("refused: changing Home Assistant's setup is only")
    assert do(setup, "discovered", stacey).startswith("found")  # looking is fine
    nobody = HASetup(FakeHA(), [], None)
    assert "maintainers is empty" in nobody.call("rename", "d1", "Front door", None, ALON)
    searched = Request(speaker="Alon", by_chat=True, heard="x", read_the_web=True)
    assert do(setup, "rename", searched, target="d1", query="x").startswith("refused: this reply searched the web")
    by_door = Request(speaker="Alon", by_chat=True, heard="x", by_door="front door")
    assert do(setup, "rename", by_door, target="d1", query="x").startswith("refused: this ran because the front door")


def test_a_device_is_renamed_and_put_in_a_room_by_name(setup):
    assert "device d1: Nuki (Hall)" in do(setup, "integrations")
    assert do(setup, "rename", target="d1", query="Front door").startswith("done: the device is now called Front door")
    assert do(setup, "area", target="d1", query="bedroom").startswith("done: the device is in Bedroom")
    assert "no room 'garage'; it has: Hall, Bedroom" in do(setup, "area", target="d1", query="garage")
    assert setup.ha.renamed == [("d1", {"name_by_user": "Front door"}), ("d1", {"area_id": "bedroom"})]


def test_undo_puts_back_the_setup_before_a_change_on_a_second_message(setup):
    do(setup, "rename", target="d1", query="Front door")
    change = setup.history.history(1)[0].commit[:8]
    assert f"change {change}" in do(setup, "setup_history")
    assert do(setup, "undo", target=change).startswith('not yet: undoing "Renamed a device to Front door')
    yes = Request(speaker="Alon", by_chat=True, heard="yes, undo it")
    assert do(setup, "undo", yes, target=change).startswith('done: undid "Renamed a device to Front door')
    assert setup.history.restored == [f"{setup.history.history(1)[0].commit}^"]
