"""Home Assistant's setup, worked by TARS: adding what Home Assistant found on the network (or an integration by name),
removing an integration, renaming a device and putting it in a room, and undoing any of it. The home_assistant tool's
setup actions (ha_tool.py) come here.

Kept in code:
- Only TARS's maintainers ([household] maintainers) may change the setup; with none, nobody may.
- A setup step that asks for a password, a token or a code, or a sign-in in a browser, isn't filled in by TARS: the
  person is sent to that step on Home Assistant's own page, so a secret never goes through a chat or a model.
- Removing an integration, or undoing a change, takes two turns: the first says what would go (and which of TARS's
  devices would stop working), and only a later message from the same person, within a few minutes, does it.
- With [home] config_dir, every change is kept in Home Assistant's setup history (haconfig.py), committed before and
  after, so it can be undone: the reply says so.
"""

import json
import re
import time
from collections.abc import Callable

from .hass import HomeAssistant
from .home import HomeError, Request

DISCOVERED, INTEGRATIONS, SETUP, REMOVE, RENAME, AREA, UNDO, SETUP_HISTORY = (
    "discovered",
    "integrations",
    "setup",
    "remove",
    "rename",
    "area",
    "undo",
    "setup_history",
)
ACTIONS = (DISCOVERED, INTEGRATIONS, SETUP, REMOVE, RENAME, AREA, UNDO, SETUP_HISTORY)
CHANGING = (SETUP, REMOVE, RENAME, AREA, UNDO)
CONFIRM_WITHIN_S = 600.0  # the second turn that confirms a removal or an undo comes within this
SECRET = re.compile(r"pass|token|secret|key|pin\b|code|auth|otp", re.IGNORECASE)
ADD_PAGE = "/config/integrations/dashboard/add?domain={domain}"  # Home Assistant's own page to add one
DISCOVERED_PAGE = "/config/integrations/dashboard"  # where it lists what it found

RULES = (
    "Home Assistant's setup, with the same tool: discovered lists what it found on the network and waits to add; "
    "integrations lists what's set up, with each one's devices and rooms; setup adds one (target: a discovered one's "
    "flow id, or query: an integration's domain), and answers each step it asks (target: the flow id, data: the "
    "answers as JSON); rename and area change a device's name or room (target: the device id; query: the new name, "
    "or the room); remove takes an integration out (target: its entry id); undo puts back the setup as it was "
    "before one change (target: the change, from setup_history). Remove and undo say first what would happen: tell "
    "them, and only do it when they say yes, in a new message. When a step needs a password or a sign-in, give them "
    "the link the tool answers: they finish it in Home Assistant, never by telling you a password."
)


class HASetup:
    """`people`: who may change the setup ([household] maintainers). `history`: Home Assistant's setup history
    (haconfig.HAConfig), None if it isn't kept. `listed`: the entity ids of the devices [home] lists, so a removal
    can say which of them would stop working."""

    def __init__(
        self,
        hass: HomeAssistant,
        people: list[str],
        history=None,
        listed: set[str] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._hass, self._history, self._listed, self._clock = hass, history, listed or set(), clock
        self._people = {p.strip().lower() for p in people}
        self._pending: dict[tuple[str, str], tuple[str, str, float]] = {}  # (action, target): (who, heard, when)

    def call(self, action: str, target: str, query: str, data: str | None, request: Request) -> str:
        if action in CHANGING:
            if refused := self._may(request):
                return refused
            if request.read_the_web:
                return "refused: this reply searched the web, so it changes nothing in the house; ask on its own"
        try:
            if action == DISCOVERED:
                return self.discovered()
            if action == INTEGRATIONS:
                return self.integrations()
            if action == SETUP_HISTORY:
                return self.setup_history()
            if action == SETUP:
                return self.setup(target, query, data, request)
            if action == RENAME:
                return self.rename(target, query, request)
            if action == AREA:
                return self.area(target, query, request)
            if action == REMOVE:
                return self.remove(target, request)
            if action == UNDO:
                return self.undo(target, request)
        except HomeError as e:
            return f"error: {e}"
        return f"error: action is one of {', '.join(ACTIONS)}"

    def _may(self, request: Request) -> str | None:
        if not self._people:
            return "refused: nobody may change Home Assistant's setup through TARS ([household] maintainers is empty)"
        if not request.speaker or request.speaker.strip().lower() not in self._people:
            return "refused: changing Home Assistant's setup is only for TARS's maintainers"
        return None

    # Reading.

    def discovered(self) -> str:
        found = []
        for f in self._hass.discovered():
            names = f.get("context", {}).get("title_placeholders") or {}
            name = names.get("name") or ", ".join(str(v) for v in names.values()) or f.get("handler", "")
            source = f.get("context", {}).get("source", "")
            found.append(f"flow {f.get('flow_id')}: {f.get('handler')} ({name}), found by {source}")
        if not found:
            return "Home Assistant hasn't found anything new waiting to be added"
        return "found and waiting to be added (setup with target = the flow id):\n" + "\n".join(found)

    def integrations(self) -> str:
        devices = self._hass.devices()
        areas = {a["area_id"]: a["name"] for a in self._hass.area_list()}
        lines = []
        for e in self._hass.entries():
            lines.append(f"entry {e['entry_id']}: {e['domain']}, {e.get('title', '')} ({e.get('state', '')})")
            for d in devices:
                if e["entry_id"] in (d.get("config_entries") or []):
                    name = d.get("name_by_user") or d.get("name") or "unnamed"
                    room = areas.get(d.get("area_id"), "no room")
                    lines.append(f"  device {d['id']}: {name} ({room})")
        return "\n".join(lines) or "nothing is set up in Home Assistant"

    def setup_history(self) -> str:
        if self._history is None or not self._history.ready():
            return "Home Assistant's setup history isn't kept here ([home] config_dir)"
        return "\n".join(
            f"change {c.commit[:8]}: {time.strftime('%Y-%m-%d %H:%M', time.localtime(c.when))} {c.message}"
            for c in self._history.history(15)
        )

    # Changing.

    def setup(self, target: str, query: str, data: str | None, request: Request) -> str:
        try:
            answers = json.loads(data) if data else {}
        except json.JSONDecodeError:
            return "error: data must be the step's answers as a JSON object"
        if not isinstance(answers, dict):
            return "error: data must be the step's answers as a JSON object"
        if target:
            if any(SECRET.search(str(k)) for k in answers):
                return "refused: a password or code is never given through TARS; " + self._hand_off(target)
            step = self._hass.answer_setup(target, answers)
        elif query:
            step = self._hass.start_setup(query.strip().lower())
        else:
            return "error: setup needs target (a discovered one's flow id) or query (an integration's domain)"
        return self._step(step, request)

    def _step(self, step: dict, request: Request) -> str:
        kind, flow, handler = step.get("type"), step.get("flow_id", ""), step.get("handler", "")
        if kind == "create_entry":
            title = step.get("title") or handler
            return f"done: added {title} ({handler}) to Home Assistant" + self._keep(
                f"Added {title} ({handler})", request
            )
        if kind == "abort":
            return f"not done: Home Assistant stopped it: {step.get('reason', 'no reason given')}"
        if kind in ("external", "external_step"):
            return f"it needs a sign-in in a browser: {self._hand_off(flow, handler)}"
        if kind in ("progress", "show_progress"):
            return f"Home Assistant is working on it: call setup again with target {flow} in a moment"
        if kind == "menu":
            options = step.get("menu_options") or []
            return f"step {step.get('step_id')} asks them to choose: {', '.join(map(str, options))}; answer with " + (
                f'setup, target {flow}, data {{"next_step_id": "<choice>"}}'
            )
        if kind == "form":
            fields = step.get("data_schema") or []
            if any(_secret(f) for f in fields):
                return f"this step asks for a password or code: {self._hand_off(flow, handler)}"
            errors = step.get("errors") or {}
            said = [f"step {step.get('step_id')} of adding {handler}"]
            if errors:
                said.append(f"Home Assistant said: {json.dumps(errors)}")
            if fields:
                said.append("it asks: " + "; ".join(_field(f) for f in fields))
                said.append(f"ask them, then answer with setup, target {flow}, data the answers as JSON")
            else:
                said.append(f"it only asks to confirm: once they say yes, answer with setup, target {flow}, data {{}}")
            return "\n".join(said)
        return f"Home Assistant answered a step TARS doesn't know ({kind}): {self._hand_off(flow, handler)}"

    def rename(self, device: str, name: str, request: Request) -> str:
        if not device or not name.strip():
            return "error: rename needs target (the device id, from integrations) and query (the new name)"
        self._hass.update_device(device, name_by_user=name.strip())
        return f"done: the device is now called {name.strip()}" + self._keep(
            f"Renamed a device to {name.strip()}", request
        )

    def area(self, device: str, room: str, request: Request) -> str:
        if not device or not room.strip():
            return "error: area needs target (the device id, from integrations) and query (the room)"
        areas = {a["name"].lower(): a for a in self._hass.area_list()}
        found = areas.get(room.strip().lower())
        if found is None:
            return f"error: Home Assistant has no room {room!r}; it has: {', '.join(a['name'] for a in areas.values())}"
        self._hass.update_device(device, area_id=found["area_id"])
        return f"done: the device is in {found['name']}" + self._keep(f"Put a device in {found['name']}", request)

    def remove(self, entry: str, request: Request) -> str:
        found = next((e for e in self._hass.entries() if e["entry_id"] == entry), None)
        if not entry or found is None:
            return "error: remove needs target, an integration's entry id, as integrations lists them"
        what = f"{found['domain']} ({found.get('title', '')})"
        if not self._confirmed(REMOVE, entry, request):
            ids = {e["entity_id"] for e in self._hass.entity_entries() if e.get("config_entry_id") == entry}
            mine = sorted(ids & self._listed)
            undo = " It can be undone from the setup history." if self._kept() else " It can't be undone from here."
            breaks = f" These, which TARS works, would stop: {', '.join(mine)}." if mine else ""
            return (
                f"not yet: removing {what} takes out its {len(ids)} entities.{breaks}{undo} Tell them, and remove it "
                "only when they say yes, in a new message"
            )
        self._keep_now()
        self._hass.remove_entry(entry)
        return f"done: removed {what} from Home Assistant" + self._keep(f"Removed {what}", request)

    def undo(self, change: str, request: Request) -> str:
        if self._history is None or not self._history.ready():
            return "error: Home Assistant's setup history isn't kept here, so nothing can be undone ([home] config_dir)"
        found = (
            next((c for c in self._history.history(50) if c.commit.startswith(change.strip())), None)
            if change
            else None
        )
        if found is None:
            return "error: undo needs target, a change as setup_history lists it"
        if not self._confirmed(UNDO, found.commit, request):
            return (
                f'not yet: undoing "{found.message}" puts Home Assistant\'s setup back as it was before it, and '
                "restarts Home Assistant (a minute without the house). Tell them, and undo it only when they say yes, "
                "in a new message"
            )
        made = self._history.restore(f"{found.commit}^")
        return f'done: undid "{found.message}", kept as change {made[:8]}; Home Assistant is restarting'

    def _confirmed(self, action: str, target: str, request: Request) -> bool:
        """Whether this is the second turn for `target`: the same person, a later message, within CONFIRM_WITHIN_S.
        The first only notes it."""
        key, now = (action, target), self._clock()
        who, heard = (request.speaker or "").strip().lower(), request.heard.strip()
        asked = self._pending.get(key)
        if asked and asked[0] == who and asked[1] != heard and now - asked[2] < CONFIRM_WITHIN_S:
            del self._pending[key]
            return True
        self._pending[key] = (who, heard, now)
        return False

    def _hand_off(self, flow: str, handler: str = "") -> str:
        """Where they finish it themselves: the setup's page in Home Assistant."""
        page = ADD_PAGE.format(domain=handler) if handler else DISCOVERED_PAGE
        return f"send them this link to finish it in Home Assistant: {self._hass.link(page)}"

    def _kept(self) -> bool:
        return self._history is not None and self._history.ready()

    def _keep_now(self) -> None:
        """Whatever changed by hand since the last commit, kept first, so an undo goes back to exactly before."""
        if self._kept():
            try:
                self._history.commit("Changes made in Home Assistant")
            except Exception as e:  # noqa: BLE001 - a history that can't be written never stops the change
                print(f"(home_assistant: couldn't keep the setup before the change: {e})")

    def _keep(self, what: str, request: Request) -> str:
        if not self._kept():
            return ""
        try:
            made = self._history.commit(f"{what}, by TARS for {request.speaker or 'someone'}")
        except Exception as e:  # noqa: BLE001 - said, not raised: the change itself is done
            return f" (not kept in the setup history: {e})"
        return f"; kept as change {made[:8]}, so it can be undone" if made else ""


def _secret(field: dict) -> bool:
    selector = field.get("selector") or {}
    text = selector.get("text") or {}
    return bool(SECRET.search(str(field.get("name", "")))) or (
        isinstance(text, dict) and text.get("type") == "password"
    )


def _field(field: dict) -> str:
    said = str(field.get("name", ""))
    if options := field.get("options"):
        said += f" (one of: {', '.join(str(o[0] if isinstance(o, list) else o) for o in options)})"
    elif field.get("type"):
        said += f" ({field['type']})"
    if field.get("required"):
        said += ", required"
    if "default" in field:
        said += f", default {field['default']}"
    return said
