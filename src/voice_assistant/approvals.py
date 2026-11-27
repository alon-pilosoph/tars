"""What's asked for on a phone call and not done on one's word alone (docs/phone.md): it's posted to the caller's
Telegram chat with Approve and Cancel, and done when they tap Approve, on the call or after it.

What may be done straight away is listed (SAFE), and nothing else is: a tool, or an action of one, that's new here
waits for a tap until it's added. What's asked is shown whole, or refused: nothing approved is out of sight.

The call is in the voice process and taps reach Telegram's, so a tap comes back as a file in the event log's folder:
the asker writes who may answer, the Telegram service writes the answer (tap_listener), and only the asker, which holds
what was asked in memory, acts on it. A restart forgets what was waiting.
"""

import dataclasses
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .files import atomic_write
from .toolbox import COLLECTS, READS, Tool, Turn, held_back

FOLDER = "approvals"  # in the event log's folder
TAP = "phone:"  # what a button sends back, before yes/no and the request
WAIT_S = 600.0
POLL_S = 0.5
MAX_SHOWN = 3000  # Telegram takes 4096 characters a message
MAX_WAITING = 5
NONCE = re.compile(r"[0-9a-f]{32}")
# The tools a call may use as at home, and, for those that do more than that, which of their actions. The house's own
# tool never unlocks from a call (home.HomeTools), and the browser asks before it commits anyone to anything.
ANY = None
SAFE: Mapping[str, frozenset[str] | None] = {
    "home": ANY,
    "feeds": ANY,
    "read_page": ANY,
    "browse": ANY,
    "send": ANY,
    "cancel_reminder": ANY,
    "snooze_reminder": ANY,
    "memory": frozenset({"remember", "recall", "list"}),
    "tars": frozenset({"about", "changes", "docs", "records", "log", "wishes", "settings"}),
    "skills": frozenset({"list", "runs"}),
    "home_assistant": frozenset({"find", "state", "history", "statistics", "logbook", "services"}),
}
ASKED = (
    "not done yet: they've been asked to approve it in their Telegram chat, and it's done when they tap Approve, now "
    "or after the call. Say that in one short line, and never that it's done; they needn't look while driving"
)
NO_CHAT = "refused: on a call this needs a tap in their Telegram chat, and they don't have one here"
TOO_LONG = "refused: too long to show whole for approval from a call; they can ask for it at home or in a chat"
TOO_MANY = "refused: too many things already wait for their approval"


def safe(tool: Tool, args) -> bool:
    if tool.name in SAFE:
        actions = SAFE[tool.name]
        return actions is ANY or (isinstance(args, dict) and args.get("action") in actions)
    return tool.does in (READS, COLLECTS)


def shown(name: str, args) -> str:
    """What a call to a tool asks for, whole: its name, and everything it was given."""
    given = args if isinstance(args, dict) else {"": args}
    lines = [f"{k}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}" for k, v in given.items()
             if v is not None and v != ""]  # fmt: skip
    return "\n".join([name.replace("_", " "), *lines])


class Approvals:
    """`folder`: the event log's. `chat_of(person)`: their Telegram chat's id, or None. `post(chat, text, buttons)`:
    posts with buttons [(label, what a tap sends)], maybe none."""

    def __init__(
        self,
        folder: Path,
        chat_of: Callable[[str | None], int | None],
        post: Callable[[int, str, list[tuple[str, str]]], None],
        wait_s: float = WAIT_S,
    ):
        self._folder, self._chat_of, self._post, self._wait_s = Path(folder) / FOLDER, chat_of, post, wait_s
        self._lock, self._waiting = threading.Lock(), 0
        for old in self._folder.glob("*"):  # what waited when TARS last stopped
            old.unlink(missing_ok=True)

    def check(self, person: str | None, heard: str) -> Callable[[Tool, object, Turn], str | None]:
        """Request.approve for one request on a call: what to answer a tool call instead of making it, or None to make
        it."""

        def approve(tool: Tool, args, turn: Turn) -> str | None:
            if tool.name == "remind":  # collected, and held for approval once the reply is kept (calls.py)
                other = for_other(args.get("for") if isinstance(args, dict) else None, person)
                return NO_CHAT if other and self._chat_of(person) is None else None
            if safe(tool, args):
                return None
            if refused := held_back(turn.request):
                return refused
            # Made later, so nothing of this reply's is left in it: the turn is over, and they've said so themselves.
            request = dataclasses.replace(turn.request, approve=None, may_act=None, emit=None, held_for_web=None)
            return self.ask(person, heard, shown(tool.name, args), lambda: tool.call(args, Turn(request)))

        return approve

    def ask(self, person: str | None, heard: str, what: str, do: Callable[[], str | None]) -> str:
        """Asks `person` to approve `what`, and has it done if they do. Answers at once, in words for the model."""
        chat = self._chat_of(person)
        if chat is None:
            return NO_CHAT
        text = f'From your phone call ("{heard}"):\n\n{what}'
        if len(text) > MAX_SHOWN:
            return TOO_LONG
        with self._lock:
            if self._waiting >= MAX_WAITING:
                return TOO_MANY
            self._waiting += 1
        nonce = secrets.token_hex(16)
        asked = self._folder / f"{nonce}.json"
        try:
            atomic_write(asked, json.dumps({"chat": chat, "until": time.time() + self._wait_s}))
            self._post(chat, text, [("Approve", f"{TAP}yes:{nonce}"), ("Cancel", f"{TAP}no:{nonce}")])
        except Exception as e:  # noqa: BLE001 - Telegram, or the disk: not asked, so not done
            self._done(asked)
            print(f"(approvals: couldn't ask: {e!r})", flush=True)
            return "error: couldn't reach their Telegram chat to ask; nothing was done"
        threading.Thread(target=self._wait, args=(chat, asked, do), daemon=True, name="approval").start()
        return ASKED

    def _wait(self, chat: int, asked: Path, do: Callable[[], str | None]) -> None:
        answer = asked.with_suffix(".answer")
        deadline = time.monotonic() + self._wait_s
        said = "That waited too long, so I didn't do it."
        try:
            while time.monotonic() < deadline:
                if answer.exists():
                    if answer.read_text() != "yes":
                        said = "Okay, I won't."
                        break
                    try:
                        said = do() or "Done."
                    except Exception as e:  # noqa: BLE001 - said to them
                        said = f"That failed ({type(e).__name__}); it may not have been done."
                    break
                time.sleep(POLL_S)
        finally:
            self._done(asked)
        try:
            self._post(chat, said, [])
        except Exception as e:  # noqa: BLE001 - only the news of it
            print(f"(approvals: couldn't say how it went: {e!r})", flush=True)

    def _done(self, asked: Path) -> None:
        with self._lock:
            self._waiting -= 1
        asked.unlink(missing_ok=True)
        asked.with_suffix(".answer").unlink(missing_ok=True)


def for_other(name: str | None, person: str | None) -> bool:
    """Whether something for `name` is for someone besides the caller."""
    return bool(name and name.strip()) and name.strip().lower() != (person or "").strip().lower()


def tap_listener(folder: Path, bot):
    """For the Telegram service (TelegramTars.listen_first): a tap on Approve or Cancel, taken only from the chat that
    was asked, and once."""
    folder = Path(folder) / FOLDER

    def take(update: dict) -> bool:
        tap = update.get("callback_query") or {}
        data = str(tap.get("data", ""))
        if not data.startswith(TAP):
            return False
        try:
            bot.answer_tap(tap["id"])
        except Exception as e:  # noqa: BLE001 - only the button's spinner
            print(f"(approvals: couldn't answer the tap: {e})")
        verdict, _, nonce = data.removeprefix(TAP).partition(":")
        chat = (tap.get("from") or {}).get("id")
        if verdict not in ("yes", "no") or not NONCE.fullmatch(nonce):
            return True
        stale = True
        try:
            asked = json.loads((folder / f"{nonce}.json").read_text())
            if asked["chat"] != chat:
                return True
            answer = folder / f"{nonce}.answer"
            stale = time.time() > asked["until"]
            if not stale and not answer.exists():
                atomic_write(answer, verdict)
        except OSError, ValueError, KeyError, TypeError:
            pass
        try:
            bot.drop_buttons(chat, (tap.get("message") or {}).get("message_id"))
            if stale:
                bot.send(chat, "That one isn't waiting any more, so nothing was done. Ask me again.")
        except Exception as e:  # noqa: BLE001 - only tidying
            print(f"(approvals: couldn't tidy up after a tap: {e})")
        return True

    return take
