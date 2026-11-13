"""TARS on Telegram: the household chats with it in writing, and it sends them what it has for them.

`voice-assistant --telegram` is a process of its own, like the web UI, so a problem with Telegram never touches the
voice. It long-polls Telegram's Bot API for messages, so nothing at home is reachable from the internet: no webhook,
no open port. Only the people in [telegram] people are answered; anyone else is ignored, and logged once, so a new
person's chat id can be found.

Each person has their own conversation (the brain's history, kept through [telegram] memory_minutes of quiet, then
for as long as a quick check says each message carries it on, up to keep_days (threads.py), or until they send /new;
read back from the log after a restart; what TARS posts by itself is part of it) with the same
brain, tools and reminders as at home, told it's writing rather than speaking; a voice message is written down by
the same speech to text as at home (stt.py), and answered as if written. What it sends goes into the chat as
well as onto the TARS page. A reminder set here for someone with a chat comes to that chat when it's due, and
whatever TARS sends at home for someone with a chat comes to it too. See docs/telegram.md.

With [browser] on, a chat can send TARS on an errand in its browser (browser/errands.py), and the errand reaches the
person here (telegram_errands.py): an order to approve, a link for a login TARS lacks, a code to send back, and its
answer when it's done.
"""

import dataclasses
import json
import re
import threading
import time
import traceback
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

from . import usage
from .audio import SAMPLE_RATE, decode_recording, warm_decoder
from .config import TELEGRAM_KEY, ConfigError
from .control import Refused, Unavailable
from .conversations import (
    FILE,
    HOUSEHOLD,
    LINK,
    LIST,
    LLM,
    NOTE,
    OTHER,
    ROLE_PERSON,
    STT,
    ConversationLog,
    SentItem,
)
from .events import person_key
from .home import OFF, ON, PRESS, SET, STATUS, HomeTools, Request
from .llm import FILLERS, REMIND_TOOLS, SKIP, THINKING, Brain, Reply
from .mic_mute import DEVICE as MIC
from .reminders import (
    ACKNOWLEDGED,
    ADD,
    MISSED,
    SAID,
    TELEGRAM,
    VOICE,
    WAITING,
    WEB,
    Change,
    ReminderTools,
    late,
    line,
    name,
    twelve_hour,
)
from .stt import Transcriber, transcribe_recording
from .threads import Continuity
from .volume import DEVICE as SPEAKER

API = "https://api.telegram.org"
POLL_S = 50  # how long a request for new messages waits for one to come; Telegram's longest
MAX_TEXT = 4096  # Telegram's longest message
MAX_CAPTION = 1024
# A message older than this when TARS reads it came while TARS was off: "in ten minutes" would be wrong by now.
STALE_S = 10 * 60.0
DELIVER_EVERY_S = 2.0
# What TARS sent at home for someone is posted to their chat if it's newer than this: after a long time off, a
# backlog of old notes would only be noise (they're on the TARS page).
ITEMS_WITHIN_S = 60 * 60.0
RETRY_S = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)  # waiting for Telegram again, after each failure in a row
REFUSED = (401, 404)  # Telegram turning the bot's token down: a setup problem, which trying again won't fix
DELIVERY_REST_S = 30.0
SEND_TRIES = 3
TYPING_EVERY_S = 4.0  # "typing..." shows for 5 s
# The longest a "typing..." may take, and so the longest a reply waits for one on its way, so it can't come after.
TYPING_TIMEOUT_S = 5.0
SORRY = "That didn't work. Try again in a moment."
CANT_READ = "I can only read text and listen to voice messages for now."
# The longest voice message TARS listens to: a few minutes is a message, and anything longer is better written. Five
# minutes of a voice message's Opus is about 2 MB; a file far bigger than that isn't fetched to find out.
MAX_VOICE_S, MAX_VOICE_BYTES = 5 * 60, 10_000_000
MIN_VOICE = SAMPLE_RATE // 10  # shorter than a tenth of a second: a tap on the button, with no words in it
NO_VOICE = "I can't listen to voice messages here. Write it instead."
TOO_LONG = "That's a long one. Keep a voice message under five minutes, or write it."
NO_WORDS = "I couldn't make out any words in that. Try again, or write it."
COULDNT_LISTEN = "I couldn't listen to that just now. Try again in a moment, or write it."
VOICE_TAG = "[Voice message]"  # what the brain is asked is spoken, and written down by speech to text
HELLO = (
    "Hello, {name}. It's TARS. Ask me anything, have me set a timer or a reminder, work the house, or ask me to send "
    "you something. /say says something aloud at home; the menu has the rest."
)
FRESH = "Starting fresh. What's up?"
# The bot's menu of commands in Telegram's app. Each is done by code at once, with no model in between, but /new's.
COMMANDS = [
    {"command": "say", "description": "Say something aloud at home: /say dinner's ready"},
    {"command": "home", "description": "How everything in the house is"},
    {"command": "volume", "description": "TARS's volume at home: /volume 40, up, down"},
    {"command": "mute", "description": "Mute TARS's microphone at home"},
    {"command": "unmute", "description": "Unmute TARS's microphone at home"},
    {"command": "spotify", "description": "Sign TARS in to Spotify"},
    {"command": "new", "description": "Start a new conversation"},
]
SAY_HOW = "Send /say and what to say, like: /say dinner's ready"
NO_ALOUD = "I can't say it: the voice assistant at home isn't running."
VOLUME_HOW = "Send /volume and a level, up or down, like: /volume 40"
NO_HOUSE = "There's no house to work from here."
NO_SPOTIFY = "Spotify isn't set up here: SPOTIFY_CLIENT_ID in .env (docs/home.md#spotify)."
WRITTEN = (
    "This conversation is on Telegram, in writing, not aloud: what's said above about replies being read aloud, "
    "and written for the ear, doesn't apply here. Reply as in a text message: short, plain text with no markdown. "
    "Links and web addresses are fine here. The [Speaker: name] tag is who's writing, from their Telegram account. "
    f"A message tagged {VOICE_TAG} was spoken, and written down by speech to text, so a word may be misheard: when "
    "it doesn't make sense, say what you heard and ask. "
    "Anything you send with the send tool comes to this chat as well as the TARS page: say it's here, and when "
    "saying what you can do, say you can send things here.\n"
    "A timer, reminder or message set here comes to this chat when it's due, if it's for someone on Telegram "
    "({people}) or for no one in particular; for anyone else it's said aloud at home. To have something said aloud "
    'at home instead ("tell the house dinner\'s ready", "say at home that I\'m running late", "tell Stacey at home '
    'to call me"), set it with aloud true, in_minutes 0 for now, as a message for the person named, or for '
    "\"everyone\" when it's for the whole house. TARS tells this chat when it's been said, and who said they got it."
)


def channel_rules(people: Collection[str]) -> str:
    """The brain's instructions for a Telegram chat, last so they win over those for a conversation aloud."""
    return WRITTEN.format(people=", ".join(people))


def _with_aloud(tool: dict) -> dict:
    """The remind tool, able to say it aloud at home instead of in the chat."""
    if tool["name"] != "remind":
        return tool
    parameters = tool["parameters"]
    aloud = {"type": "boolean", "description": "Say it aloud at home, rather than posting it to a chat."}
    return {
        **tool,
        "description": "Set a timer, a reminder or a message for someone: posted to their chat, or said aloud at home.",
        "parameters": {
            **parameters,
            "required": [*parameters["required"], "aloud"],
            "properties": {**parameters["properties"], "aloud": aloud},
        },
    }


# A chat's reminder tools: the same, with the choice of having one said aloud at home.
CHAT_REMIND_TOOLS = [_with_aloud(t) for t in REMIND_TOOLS]


class TelegramError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after

    @property
    def retryable(self) -> bool:
        """Whether trying again later may work: Telegram not reached, asking to wait (429), or its own trouble (5xx)."""
        return self.status is None or self.status == 429 or self.status >= 500

    @property
    def refused(self) -> bool:
        """Telegram turning the bot's token down: nothing works until it's fixed."""
        return self.status in REFUSED

    @property
    def for_good(self) -> bool:
        """Telegram turning this one message down for good: a chat that blocked the bot, a file too big."""
        return not (self.retryable or self.refused)


def refused_token_error(env: Path | str) -> ConfigError:
    return ConfigError(f"Telegram refused {TELEGRAM_KEY}: check it in {env}.")


class Bot:
    """The few methods of Telegram's Bot API that TARS uses. The token is part of every address, so no error says
    which address failed: it would end up in the logs."""

    def __init__(self, token: str, http: httpx.Client | None = None):
        self._base = f"{API}/bot{token}"
        self._files = f"{API}/file/bot{token}"
        self._http = http or httpx.Client(timeout=httpx.Timeout(POLL_S + 10, connect=10))

    def call(self, method: str, files: dict | None = None, wait_s: float | None = None, **params):
        """`wait_s`: how long to wait for Telegram's answer, if not the client's own."""
        timeout = httpx.USE_CLIENT_DEFAULT if wait_s is None else wait_s
        try:
            if files:
                response = self._http.post(f"{self._base}/{method}", data=params, files=files, timeout=timeout)
            else:
                response = self._http.post(f"{self._base}/{method}", json=params, timeout=timeout)
        except httpx.HTTPError as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {response.status_code}", response.status_code) from None
        if not body.get("ok"):
            retry = (body.get("parameters") or {}).get("retry_after")
            why = body.get("description") or f"HTTP {response.status_code}"
            raise TelegramError(f"{method}: {why}", body.get("error_code") or response.status_code, retry)
        return body["result"]

    def me(self) -> dict:
        return self.call("getMe")

    def set_commands(self, commands: list[dict]) -> None:
        self.call("setMyCommands", commands=commands)

    def updates(self, offset: int | None) -> list[dict]:
        """New messages and taps on TARS's buttons, waiting up to POLL_S for one. Asking with `offset` confirms
        everything before it."""
        params = {"timeout": POLL_S, "allowed_updates": ["message", "callback_query"]}
        return self.call("getUpdates", **params, **({"offset": offset} if offset is not None else {}))

    def send(self, chat_id: int, text: str) -> None:
        for part in split_text(text):
            self.call("sendMessage", chat_id=chat_id, text=part)

    def send_buttons(self, chat_id: int, text: str, buttons: list[tuple[str, str]], preview: bool = True) -> int:
        """`text` with a row of buttons under it, each (label, what a tap sends back); the message's id. `preview`:
        False sends a link in it without a preview, which Telegram makes by fetching the link itself."""
        markup = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]}
        extra = {} if preview else {"link_preview_options": {"is_disabled": True}}
        return self.call("sendMessage", chat_id=chat_id, text=text[:MAX_TEXT], reply_markup=markup, **extra)[
            "message_id"
        ]

    def drop_buttons(self, chat_id: int, message_id: int) -> None:
        self.call(
            "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []}
        )

    def answer_tap(self, tap_id: str) -> None:
        """Tells Telegram a tap was seen, so the button stops spinning."""
        self.call("answerCallbackQuery", callback_query_id=tap_id)

    def send_photo(self, chat_id: int, data: bytes, caption: str = "") -> None:
        params = {"chat_id": str(chat_id), "caption": caption[:MAX_CAPTION]}
        self.call("sendPhoto", files={"photo": ("page.jpg", data, "image/jpeg")}, **params)

    def send_file(self, chat_id: int, name: str, data: bytes, mime: str, caption: str = "") -> None:
        params = {"chat_id": str(chat_id), "caption": caption[:MAX_CAPTION]}
        self.call("sendDocument", files={"document": (name, data, mime)}, **params)

    def typing(self, chat_id: int) -> None:
        self.call("sendChatAction", wait_s=TYPING_TIMEOUT_S, chat_id=chat_id, action="typing")

    def download(self, file_id: str) -> bytes:
        """A file someone sent (a voice message), by its id. Telegram lets a bot fetch files of up to 20 MB."""
        path = self.call("getFile", file_id=file_id).get("file_path")
        if not path:
            raise TelegramError("getFile: no file to download")
        try:
            response = self._http.get(f"{self._files}/{path}")
        except httpx.HTTPError as e:
            raise TelegramError(f"download: {type(e).__name__}") from None
        if response.status_code != 200:
            raise TelegramError(f"download: HTTP {response.status_code}", response.status_code)
        return response.content


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """`text` in messages Telegram takes, split between paragraphs, else lines, else words, where it can."""
    parts, rest = [], text.strip()
    while len(rest) > limit:
        cut = next((at for sep in ("\n\n", "\n", " ") if (at := rest.rfind(sep, 0, limit)) > 0), limit)
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return [*parts, rest] if rest else parts


def item_text(item: SentItem) -> str:
    """A link, note or list as a message."""
    if item.kind == LINK:
        return "\n".join(t for t in (item.title, item.description, item.url) if t)
    if item.kind == NOTE:
        return f"{item.title}\n\n{item.body}"
    if item.kind == LIST:
        return "\n".join([item.title, *(f"• {e}" for e in item.entries or [])])
    return item.title


def stored_item(row: dict, folder: Path) -> SentItem:
    """An item as the conversation log keeps it, ready to send again: its file read back, and only the list
    entries not ticked off yet."""
    item = SentItem(row["kind"], row["title"], row["scope"], url=row["url"], site=row["site"])
    item.description, item.body = row["description"], row["body"]
    if row["kind"] == LIST:
        item.entries = [e["text"] for e in json.loads(row["entries"] or "[]") if not e.get("done")]
    if row["kind"] == FILE:
        item.file_name, item.mime = row["file_name"], row["mime"]
        item.file_bytes = (folder / row["file"]).read_bytes()
    return item


def report(r: dict) -> str:
    """What the chat that set a reminder said aloud at home is told, as it goes: said, then got it or missed."""
    said = f"“{line(r)}”"
    if r["status"] == SAID:
        return f"Said at home at {_clock(r['last_said'])}: {said}"
    if r["status"] == WAITING:
        return f"Said at home at {_clock(r['last_said'])}: {said} Waiting for someone to say they got it."
    if r["status"] == ACKNOWLEDGED:
        if r["acked_via"] == WEB:
            who = "Someone on the TARS page"
        else:
            who = name(r["acked_by"]) if r["acked_by"] else "Someone"
        where = " on Telegram" if r["acked_via"] == TELEGRAM else ""
        return f"{who} said they got it{where} at {_clock(r['acked_at'])}: {said}"
    if r["status"] == MISSED:
        times = "once" if r["tries"] == 1 else f"{r['tries']} times"
        return f"Nobody said they got it (said {times} at home): {said}"
    return said


def _clock(at: float) -> str:
    return twelve_hour(datetime.fromtimestamp(at).astimezone())


def tidy(reply: str) -> str:
    """A reply written to be heard, for reading: without the lines said while a slow answer comes ("Looking it
    up."), which a chat shows as "typing..." instead."""
    text = reply.strip()
    # Only with more after them: "On it." can be the whole reply.
    filler = next((f for f in FILLERS if text.startswith(f)), "")
    if text[len(filler) :].strip():
        text = text[len(filler) :]
    if text.replace(THINKING, "", 1).strip():
        text = text.replace(THINKING, "", 1)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


@dataclass
class Chat:
    """One person's chat: their own conversation with the brain, and the one kept for the web UI."""

    person: str
    chat_id: int
    brain: Brain
    conversation: int | None = None
    last_at: float = 0.0
    restored: bool = False  # whether its conversation has been read back from the log since TARS started
    # Held while TARS answers in the chat, or posts to it by itself: one at a time, never one inside the other.
    busy: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


@contextmanager
def _timed(took: dict[str, float], what: str) -> Iterator[None]:
    """How long the block took, in seconds, kept in `took` under `what`."""
    start = time.perf_counter()
    try:
        yield
    finally:
        took[what] = time.perf_counter() - start


@contextmanager
def _free(chat: Chat) -> Iterator[bool]:
    """Holds `chat` if nothing else is: True then, False if TARS is answering in it (try again later)."""
    got = chat.busy.acquire(blocking=False)
    try:
        yield got
    finally:
        if got:
            chat.busy.release()


class _Typing:
    """ "typing..." in the chat until stopped: Telegram shows it for 5 s at a time."""

    def __init__(self, bot: Bot, chat_id: int):
        self._bot, self._chat_id, self._stop = bot, chat_id, threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="telegram-typing")

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(TYPING_TIMEOUT_S)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._bot.typing(self._chat_id)
            except TelegramError:
                pass  # only a courtesy
            self._stop.wait(TYPING_EVERY_S)


class TelegramTars:
    def __init__(
        self,
        bot: Bot,
        people: dict[str, int],
        make_brain: Callable[[], Brain],
        conversations: ConversationLog | None = None,
        reminder_tools: ReminderTools | None = None,
        memory_s: float = 600.0,
        send_items: bool = True,
        clock: Callable[[], float] = time.time,
        typing: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        transcriber: Transcriber | None = None,
        env: Path | str = ".env",
        home: HomeTools | None = None,
        say: Callable[[str], None] | None = None,
        spotify_login: Callable[[Callable[[str], None]], str] | None = None,
        skill_clock=None,
        keep_s: float = 0.0,
        continuity: Continuity | None = None,
    ):
        """`people`: name → chat id, the only chats answered. `make_brain`: a new brain, one for each person, so
        each has their own conversation. `conversations`: where chats are kept for the web UI (None: nowhere).
        `send_items`: also post what TARS sends at home for someone with a chat. `transcriber`: speech to text for
        voice messages (None: they're turned down). `env`: where the token is, for saying so when Telegram refuses it.
        `home`: the chat's house, for /home, /volume, /mute and /unmute. `say(text)`: has the assistant say it aloud
        at home, word for word, for /say; raises control.py's Unavailable or Refused when it can't.
        `spotify_login(send)`: signs in to Spotify for /spotify, telling them with `send` (spotify.page_login).
        `skill_clock`: the skills due to run by themselves in their owner's chat (skills.Clock).
        `memory_s`: how long a chat is one conversation however quiet it's been. `keep_s`: after that, and up to this,
        a message the `continuity` check says carries it on keeps it going; the conversation is read back from the
        log after a restart too (0: never). `clock`, `typing` and `sleep` are for the tests."""
        self._bot, self._make_brain = bot, make_brain
        self._chats = {chat_id: Chat(name, chat_id, make_brain()) for name, chat_id in people.items()}
        self._by_person = {person_key(name): chat_id for name, chat_id in people.items()}
        self._conversations, self._tools = conversations, reminder_tools
        self._memory_s, self._send_items, self._clock, self._typing = memory_s, send_items, clock, typing
        self._sleep, self._transcriber, self._env, self._home, self._say = sleep, transcriber, env, home, say
        self._spotify_login = spotify_login
        self._skill_clock = skill_clock
        self._keep_s, self._continuity = keep_s, continuity
        self._ignored: set[int] = set()
        # A chat's conversation is kept from two threads: the one answering, and the one posting what's due.
        self._lock = threading.Lock()
        self._listeners: list[Callable[[dict], bool]] = []  # what sees each update first (listen_first)
        self._last_asked: Chat | None = None  # whose turn it was last: who hears how a task they started went

    def run_forever(self, me: dict | None = None) -> None:
        """`me`: the bot, as Telegram's getMe said, if it's been asked already."""
        me = me or self._bot.me()
        names = ", ".join(c.person for c in self._chats.values())
        print(f"TARS is on Telegram as @{me.get('username')}, for {names}.", flush=True)
        try:
            self._bot.set_commands(COMMANDS)
        except TelegramError as e:  # only the menu: /new works without it
            print(f"(Telegram: couldn't set the commands menu: {e})")
        threading.Thread(target=self._deliver_forever, daemon=True, name="telegram-deliver").start()
        if self._transcriber is not None:  # a voice message's decoder loads in seconds on a Pi: not on the first one
            threading.Thread(target=warm_decoder, daemon=True, name="telegram-warm").start()
        offset, failures = None, 0
        while True:
            try:
                updates = self._bot.updates(offset)
                failures = 0
            except TelegramError as e:
                if e.refused:
                    raise refused_token_error(self._env) from None
                wait = e.retry_after or RETRY_S[min(failures, len(RETRY_S) - 1)]
                failures += 1
                if e.status == 409:
                    print("(Telegram: something else is reading this bot's messages, another TARS?)")
                print(f"(Telegram: {e}; trying again in {wait:.0f} s)", flush=True)
                self._sleep(wait)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle(update)
                except Exception:  # noqa: BLE001 - one message gone wrong never stops the chat
                    traceback.print_exc()

    def handle(self, update: dict) -> None:
        if any(listener(update) for listener in self._listeners):
            return
        message = update.get("message") or {}
        if (message.get("chat") or {}).get("type") != "private":
            return
        chat_id = message["chat"]["id"]
        chat = self._chats.get(chat_id)
        if chat is None:
            if chat_id not in self._ignored:
                self._ignored.add(chat_id)
                who = (message.get("from") or {}).get("first_name") or "someone"
                print(f"(Telegram: {who} wrote from chat {chat_id}, which isn't in [telegram] people; not answered)")
            return
        with usage.turn(chat.person, usage.TELEGRAM):  # what answering costs is counted for this chat's person
            self._from(chat, chat_id, message)

    def _from(self, chat: Chat, chat_id: int, message: dict) -> None:
        """A message from one of the people TARS answers here."""
        text, voice = (message.get("text") or "").strip(), message.get("voice")
        if not text and not voice:
            self._send(chat_id, CANT_READ)
            return
        sent_at = message.get("date") or self._clock()
        if self._clock() - sent_at > STALE_S:
            off = f"I was off when this came in, at {_clock(sent_at)}. Send it again if it still matters."
            self._send(chat_id, off)
            return
        if voice:
            self.listen(chat, voice)
            return
        command = text.split()[0].split("@")[0]
        if command == "/start":
            self._send(chat_id, HELLO.format(name=chat.person))
            return
        if command == "/new":
            self.start_over(chat)
            self._send(chat_id, FRESH)
            return
        rest = text[len(text.split()[0]) :].strip()
        if command == "/say":
            self._send(chat_id, self.say_aloud(chat, rest))
            return
        if command in ("/home", "/volume", "/mute", "/unmute"):
            self._send(chat_id, self.house_command(chat, command, rest))
            return
        if command == "/spotify":
            self.spotify(chat_id)
            return
        self.answer(chat, text)

    def spotify(self, chat_id: int) -> None:
        """/spotify: a sign-in from this chat, in a thread of its own, since it waits minutes for the page."""
        if self._spotify_login is None:
            self._send(chat_id, NO_SPOTIFY)
            return
        login = self._spotify_login

        def run() -> None:
            self._send(chat_id, login(lambda text: self._send(chat_id, text)))

        threading.Thread(target=run, daemon=True, name="spotify-login").start()

    def say_aloud(self, chat: Chat, text: str) -> str:
        """/say: what they wrote, said aloud at home word for word, after a chime, between conversations (an
        announcement, assistant.py), even while TARS is muted."""
        if not text:
            return SAY_HOW
        if self._say is None:
            return NO_ALOUD
        try:
            self._say(text)
        except Unavailable, Refused:
            return NO_ALOUD
        print(f"(Telegram: {chat.person} /say: {text!r})", flush=True)
        return "Saying it at home now."

    def house_command(self, chat: Chat, command: str, rest: str = "") -> str:
        """/home: the whole house, a line a device, for reading (HomeTools.glance). /volume: TARS's speaker's, read,
        set or turned. /mute and /unmute: TARS's microphone at home."""
        if self._home is None:
            return NO_HOUSE
        if command == "/home":
            return self._home.glance()
        if command == "/volume":
            return self.volume(chat, rest)
        if MIC.lower() not in self._home.devices:
            return "I can't reach my microphone from here: the voice assistant has no event log to meet me at."
        action = OFF if command == "/mute" else ON
        said = self._home.call({"device": MIC, "action": action}, Request(speaker=chat.person, by_chat=True))
        if said.startswith("done"):
            return "Muted: I'm not listening at home." if action == OFF else "Unmuted: I'm listening at home again."
        return "Couldn't: " + said.partition(": ")[2]

    def volume(self, chat: Chat, rest: str) -> str:
        """/volume: the level; /volume 40: set to 40%; /volume up or down: a step (10%)."""
        if SPEAKER.lower() not in self._home.devices:
            return "I can't turn my speaker from here: [home] own_speaker is off, or it can't be read."
        word = rest.lower().removesuffix("%").strip()
        if not word:
            args = {"action": STATUS}
        elif word in ("up", "down"):
            args = {"action": PRESS, "button": f"volume_{word}"}
        elif word.isdigit():
            args = {"action": SET, "level": int(word)}
        else:
            return VOLUME_HOW
        said = self._home.call({"device": SPEAKER, **args}, Request(speaker=chat.person, by_chat=True))
        kind, _, what = said.partition(": ")
        if kind == "done":
            return f"Done: {what}."
        if kind == "error":
            return f"Couldn't: {what}"
        return said  # its status, or "not confirmed: ..."

    def listen(self, chat: Chat, voice: dict) -> None:
        """A voice message: written down by speech to text (Deepgram, then OpenAI, as at home), then answered as if
        written. Its recording is kept with the turn, so the web UI plays it like one said at home. The chat is held
        from the start, as for a written message, so nothing TARS posts by itself lands before the answer."""
        if self._transcriber is None:
            self._send(chat.chat_id, NO_VOICE)
            return
        if (voice.get("duration") or 0) > MAX_VOICE_S or (voice.get("file_size") or 0) > MAX_VOICE_BYTES:
            self._send(chat.chat_id, TOO_LONG)
            return
        with chat.busy:
            recording, stage, took = None, OTHER, {}
            try:
                with _Typing(self._bot, chat.chat_id) if self._typing else nullcontext():
                    with _timed(took, "download"):
                        data = self._bot.download(voice["file_id"])
                    stage = STT
                    with _timed(took, "decode"):
                        recording = decode_recording(data)
                    if len(recording) > MAX_VOICE_S * SAMPLE_RATE:  # Telegram's duration is a claim; this is the audio
                        self._send(chat.chat_id, TOO_LONG)
                        return
                    with _timed(took, "speech to text"):
                        text = transcribe_recording(self._transcriber, recording) if len(recording) >= MIN_VOICE else ""
            except Exception as e:  # noqa: BLE001 - said in the chat, and kept as a failed turn with where it failed
                print(f"(Telegram: couldn't listen to {chat.person}'s voice message: {e!r})", flush=True)
                self._keep_unanswered(chat, recording, failed_at=stage, error=f"{type(e).__name__}: {e}")
                self._send(chat.chat_id, COULDNT_LISTEN)
                return
            stages = ", ".join(f"{what} {s:.1f} s" for what, s in took.items())
            print(f"(Telegram: {chat.person}'s voice message, {sum(took.values()):.1f} s to write down: {stages})")
            if not text:
                self._keep_unanswered(chat, recording, said=NO_WORDS)
                self._send(chat.chat_id, NO_WORDS)
                return
            self._answer(chat, text, recording)

    def answer(self, chat: Chat, text: str) -> None:
        """Answers in the chat, and keeps the exchange for the web UI. As by voice, what the reply did (reminders
        set, things sent) only happens once the reply has reached them. The chat is held meanwhile, so what TARS
        posts by itself (deliver) waits until the exchange is over."""
        with chat.busy:
            self._answer(chat, text)

    def _answer(self, chat: Chat, text: str, recording: np.ndarray | None = None) -> None:
        """`recording`: the voice message `text` was written down from; None for a written message."""
        now = self._clock()
        print(f"{chat.person} (Telegram{', by voice' if recording is not None else ''}):  {text!r}", flush=True)
        self._carry_on_or_not(chat, text, now)
        conversation = self._current_conversation(chat, now)
        if conversation is not None:
            self._log(self._conversations.add_person_turn, conversation, text, recording, speaker=chat.person, ts=now)
        brain, written = chat.brain, None
        self._last_asked = chat
        asked = f"[Speaker: {chat.person}] {VOICE_TAG + ' ' if recording is not None else ''}{text}"
        request = Request(speaker=chat.person, by_chat=True, heard=text)
        try:
            with _Typing(self._bot, chat.chat_id) if self._typing else nullcontext():
                # A chat is always meant for TARS: a skip is answered, and the tag never posted.
                written = Reply(brain, asked, request, may_skip=False, acks=bool(self._tools))
                reply = tidy(written.read())
            if reply:
                self._send(chat.chat_id, reply, tries=SEND_TRIES)
            if written.pardoned:  # as aloud: what they write next answers it, and isn't skipped
                brain.remember_said(reply)
        except Exception as e:  # noqa: BLE001 - said in the chat, kept in the log, and forgotten by the brain
            if written:  # posting it failed: one that failed before is forgotten already
                written.drop()
            stage = OTHER if isinstance(e, TelegramError) else LLM
            print(f"(Telegram: couldn't answer {chat.person}: {e!r})", flush=True)
            self._log_turn(conversation, "", brain, failed_at=stage, error=f"{type(e).__name__}: {e}")
            if stage == LLM:
                self._send(chat.chat_id, SORRY)
            return
        sent, changes = list(brain.sent), list(brain.changes)
        print(f"TARS (Telegram):  {reply}", flush=True)
        turn = self._log_turn(conversation, reply, brain)
        for item in sent:
            delivered = self._post_item(chat.chat_id, item)
            if self._conversations:  # kept even without a conversation, like those sent by voice
                self._log(self._conversations.add_item, conversation, turn, item, chat.person, delivered=delivered)
        if written.acks is not None and self._tools:
            self._ack(written.acks, chat.person, changed={c.id for c in changes if c.id is not None})
        for change in changes:
            self._apply(change, chat, conversation)

    def deliver(self) -> None:
        """Posts the reminders due on Telegram, how those set here and said at home went, and what TARS sent at home
        for someone with a chat. A chat in the middle of an exchange is left for the next round, so nothing TARS
        posts by itself lands between a question and its answer. Raises TelegramError when Telegram can't be reached
        or refuses the token, and the table's own errors: what wasn't posted is tried again next time. One Telegram
        turns down for good, or that can't be read, is given up on alone, so it can't hold up the rest."""
        now = self._clock()
        if self._tools:
            reminders = self._tools.reminders
            for r in reminders.due(now, channel=TELEGRAM):
                chat = self._chat_for(r["for_name"] or r["from_name"])
                if chat is None:  # their chat was taken out of [telegram] people since: said at home instead
                    reminders.say_aloud(r["id"])
                    continue
                with _free(chat) as free:
                    if free:
                        self._post_reminder(chat, r, now)
            for r in reminders.to_report(now):
                chat = self._chat_for(r["from_name"])
                if chat is None:
                    continue
                with _free(chat) as free:
                    if free:
                        self._report(chat, r, now)
        if self._skill_clock:
            from .skills import CHAT

            for due in self._skill_clock.take():
                skill, chat = due.skill, self._chat_for(due.skill.asker)
                if skill.once is not None and (skill.say != CHAT or chat is None):
                    self._run_skill(None, due)  # not answered in a chat: handed over to be said aloud
                    continue
                if chat is None:
                    print(f"(skill {skill.name}: {skill.owner} has no chat to post it in)")
                    continue
                with chat.busy:  # due now: after an exchange in progress, not the next round
                    self._run_skill(chat, due)
        if self._conversations and self._send_items:
            for row in self._conversations.undelivered(since=now - ITEMS_WITHIN_S):
                chat = self._chat_for(row["for_name"])
                if chat is None:
                    continue
                with _free(chat) as free:
                    if free:
                        self._post_sent_at_home(chat, row, now)

    def _run_skill(self, chat: Chat | None, due) -> None:
        """A skill whose time has come, in its owner's chat: a turn of its own, with its instructions as the request,
        whose reply is posted, and part of the chat's conversation, so their answer follows on from it. `chat`: None
        for a one-off that isn't answered in a chat here (asked aloud, and taken here because the assistant isn't
        running; or for someone without a chat): run with a brain of its own, and its answer handed over to be said
        aloud (skills.Clock.hand_over). A one-off's try that didn't happen isn't posted: it's tried again, or given
        up and whoever asked is told (skills.Clock.settle). What the turn sent, and asked of the reminders, is made
        once its reply is kept, posted or handed over, as an ordinary chat turn's is once it's posted (_skill_did);
        never for one skipped, one that didn't happen, or one whose post failed."""
        with usage.turn(due.skill.owner, usage.SKILL):
            from .skills import Answers, happened, on_its_own

            skill, clock = due.skill, self._skill_clock
            # What the house answers, for whether a one-off happened; one a door's lock set off changes nothing in it
            # (home.Request.by_door).
            house = Answers()
            if chat:
                request = Request(speaker=chat.person, by_chat=True, changed=house, by_door=due.door)
                brain, where = chat.brain, f"in {chat.person}'s chat"
            else:  # not part of any chat's conversation
                request = Request(speaker=None if skill.household else skill.owner, changed=house, by_door=due.door)
                brain, where = self._make_brain(), "to be said aloud"
            print(f"(skill {skill.name}: {due.why or f'at {due.when:%H:%M}'}, {where})", flush=True)
            if chat:
                self._new_if_quiet(chat, self._clock())
            try:
                written = Reply(brain, on_its_own(skill, due.when, SKIP, due.why), request)
                reply = tidy(written.read())
            except Exception as e:  # noqa: BLE001 - a skill that can't run waits for its next time
                print(f"(skill {skill.name} didn't run: {e!r})", flush=True)
                if skill.once is not None:
                    clock.settle(due, house, e)
                return
            sent, changes = list(brain.sent), list(brain.changes)  # the brain forgets them with the turn
            if skill.once is not None and happened(house)[0]:
                written.drop()
                print(f"(skill {skill.name}: not posted, since it didn't happen: {reply!r})", flush=True)
                clock.settle(due, house)
                return
            if not reply:  # skipped, or nothing at all
                written.drop()
                print(f"(skill {skill.name}: nothing to do now)", flush=True)
                clock.record(due, "")
                return
            if chat is None:
                clock.hand_over(due, reply)
                self._skill_did(None, request.speaker, None, None, sent, changes)
                return
            try:
                self._send(chat.chat_id, reply, tries=SEND_TRIES)
            except TelegramError as e:
                written.drop()
                print(f"(skill {skill.name}: couldn't post it: {e})", flush=True)
                if skill.once is not None:  # done all the same: posted with the reminders, once Telegram takes it
                    clock.hand_over(due, reply)
                    self._skill_did(chat, chat.person, None, None, sent, changes, post=False)
                return
            clock.record(due, reply)
            now = self._clock()
            turn = None
            if (conversation := self._current_conversation(chat, now)) is not None:
                turn = self._log(self._conversations.add_tars_turn, conversation, reply, ts=now)
            self._skill_did(chat, chat.person, conversation, turn, sent, changes)

    def _skill_did(
        self,
        chat: Chat | None,
        person: str | None,
        conversation: int | None,
        turn: int | None,
        sent: list[SentItem],
        changes: list[Change],
        post: bool = True,
    ) -> None:
        """What a skill's own turn sent, and asked of the reminders, once its reply is kept, as _answer makes a chat
        turn's: in `chat`, posted there now (`post`) or, its reply handed over, by deliver() once Telegram takes it;
        with no chat (a one-off asked aloud, run here), kept for the TARS page and `person`'s chat, and made from
        `person` (None: the household's), as by voice."""
        for item in sent:
            delivered = self._post_item(chat.chat_id, item) if chat and post else None
            if self._conversations:  # kept even without a conversation, like those sent by voice
                item = item if person else dataclasses.replace(item, scope=HOUSEHOLD)
                self._log(self._conversations.add_item, conversation, turn, item, person, delivered=delivered)
        for change in changes:
            if chat:
                self._apply(change, chat, conversation)
            elif self._tools:
                try:
                    self._tools.apply(change, person)
                    print(f"(reminders: {change.action} done)")
                except Exception as e:  # noqa: BLE001 - checked when the model asked; only a broken table gets here
                    print(f"(couldn't {change.action} the reminder: {e!r})")

    # For what posts to a chat besides its conversation (telegram_errands.py)

    def listen_first(self, listener: Callable[[dict], bool]) -> None:
        """Has `listener` see each update before the chats do: one it says it took (True) goes no further."""
        self._listeners.append(listener)

    @property
    def bot(self) -> Bot:
        return self._bot

    def chat_for(self, person: str | None) -> Chat | None:
        return self._chat_for(person)

    def chat_for_id(self, chat_id: int | None) -> Chat | None:
        return self._chats.get(chat_id)

    def post(self, chat: Chat, text: str, tries: int = SEND_TRIES) -> None:
        """Posts `text` to `chat`, outside its conversation (a question an errand asks, a notice)."""
        self._send(chat.chat_id, text, tries=tries)

    def remember(self, chat: Chat, text: str) -> None:
        """`text`, posted by TARS by itself, as part of the chat's conversation from now on."""
        self._said(chat, text, self._clock())

    def _chat_for(self, person: str | None) -> Chat | None:
        chat_id = self._by_person.get(person_key(person))
        return self._chats[chat_id] if chat_id is not None else None

    def _post_reminder(self, chat: Chat, r: dict, now: float) -> None:
        reminders = self._tools.reminders
        text = " ".join(t for t in (line(r), late(r, now)) if t)
        try:
            self._bot.send(chat.chat_id, text)
        except TelegramError as e:
            if not e.for_good:
                raise
            print(f"(Telegram: can't post reminder {r['id']} ({e}); it's said aloud at home instead)")
            reminders.say_aloud(r["id"])
            return
        reminders.delivered(r["id"], now)
        print(f"TARS (Telegram, to {chat.person}):  {text}  (reminder {r['id']})", flush=True)
        self._said(chat, text, now)

    def _report(self, chat: Chat, r: dict, now: float) -> None:
        """Tells the chat that set a reminder said at home how it went; one that can't be reached isn't told again."""
        text = report(r)
        try:
            self._bot.send(chat.chat_id, text)
        except TelegramError as e:
            if not e.for_good:
                raise
            print(f"(Telegram: can't tell {chat.person} how reminder {r['id']} went ({e}))")
            self._tools.reminders.reported(r["id"], r["status"])
            return
        self._tools.reminders.reported(r["id"], r["status"])
        self._said(chat, text, now)

    def _post_sent_at_home(self, chat: Chat, row: dict, now: float) -> None:
        log = self._conversations
        try:
            item = stored_item(row, log.store.folder)
            self._post_item(chat.chat_id, item, raises=True)
        except (TelegramError, OSError, ValueError) as e:  # Telegram's no, its file gone, or its list unreadable
            if isinstance(e, TelegramError) and not e.for_good:
                raise
            print(f"(Telegram: can't post {row['title']!r} ({e!r}); it's on the TARS page)")
            log.mark_delivered(row["id"], now)
            return
        log.mark_delivered(row["id"], now)
        chat.brain.remember_said(item_text(item))

    def _deliver_forever(self) -> None:
        wait = DELIVER_EVERY_S
        while True:
            self._sleep(wait)
            try:
                self.deliver()
                wait = DELIVER_EVERY_S
            except Exception as e:  # noqa: BLE001 - Telegram or the table: tried again after a rest
                print(f"(Telegram: couldn't post what's due: {e!r}; trying again in {DELIVERY_REST_S:.0f} s)")
                wait = DELIVERY_REST_S

    def channel(self, change: Change, asker: str) -> str:
        """Where a reminder set here is said: aloud at home when asked to be, or when it waits until someone's back;
        else in the chat of the person it's for (or of whoever set it, for "a timer"), if they have one."""
        r = change.reminder
        if change.action != ADD or r is None or r.due is None or change.aloud:
            return VOICE
        return TELEGRAM if person_key(r.for_name or asker) in self._by_person else VOICE

    def _apply(self, change: Change, chat: Chat, conversation: int | None) -> None:
        try:
            channel = self.channel(change, chat.person)
            self._tools.apply(change, chat.person, conversation, via=TELEGRAM, channel=channel)
            print(f"(reminders: {change.action} done{' on Telegram' * (channel == TELEGRAM)})")
        except Exception as e:  # noqa: BLE001 - checked when the model asked; only a broken table gets here
            print(f"(couldn't {change.action} the reminder: {e!r})")

    def _ack(self, ids: list[int], who: str, changed: Collection[int]) -> None:
        """Only a numbered <ack N>: a bare one would be for the reminder said aloud at home last, whoever it's for,
        and a chat has none of its own waiting, so it's a slip."""
        reminders = self._tools.reminders
        try:
            for i in [i for i in ids if i not in changed]:
                if reminders.ack(i, who, TELEGRAM):
                    print(f"(reminder {i} acknowledged by {who}, on Telegram)")
        except Exception as e:  # noqa: BLE001 - the reply was sent; only the record of it failed
            print(f"(couldn't mark the reminder acknowledged: {e!r})")

    def _post_item(self, chat_id: int, item: SentItem, raises: bool = False) -> float | None:
        """When it was posted, or None if it couldn't be (it's on the TARS page all the same)."""
        try:
            if item.kind == FILE and (item.mime or "").startswith("image/"):
                self._bot.send_photo(chat_id, item.file_bytes, item.title)  # a camera snapshot: shown inline
            elif item.kind == FILE:
                self._bot.send_file(chat_id, item.file_name, item.file_bytes, item.mime, item.title)
            else:
                self._bot.send(chat_id, item_text(item))
        except TelegramError as e:
            if raises:
                raise
            print(f"(Telegram: couldn't post {item.title!r}: {e})")
            return None
        return self._clock()

    def _send(self, chat_id: int, text: str, tries: int = 1) -> None:
        """Each part of `text` is tried `tries` times on its own, so one that failed doesn't post those before it
        again. With one try a failure is only printed. With more, TelegramError when none of it arrived; once a part
        has, the rest failing is printed, and it counts as sent: they've read some of it, which may say what it did
        ("Set for 5pm."), so that must be done."""
        parts = split_text(text)
        for sent, part in enumerate(parts):
            for attempt in range(tries):
                try:
                    self._bot.send(chat_id, part)
                    break
                except TelegramError as e:
                    if tries == 1:
                        print(f"(Telegram: couldn't send a message: {e})")
                        return
                    if attempt == tries - 1:
                        if sent == 0:
                            raise
                        print(f"(Telegram: only {sent} of the {len(parts)} parts of a message arrived: {e})")
                        return
                    self._sleep(e.retry_after or RETRY_S[attempt])

    def _conversation(self, chat: Chat, now: float) -> int | None:
        """The chat's conversation for the web UI: the same one until it goes quiet for as long as the brain
        remembers, or someone deletes it there. Call with the lock held."""
        log = self._conversations
        if log is None:
            return None
        self._restore(chat)
        current = chat.conversation
        if current is not None and now - chat.last_at <= self._memory_s and self._log(log.exists, current):
            return current
        if current is not None:
            self._log(log.end, current, ts=chat.last_at or now)
        chat.conversation = self._log(log.start, None, ts=now, via=TELEGRAM)
        return chat.conversation

    def _carry_on_or_not(self, chat: Chat, text: str, now: float) -> None:
        """Before answering `text`: after [telegram] memory_minutes of quiet, the chat's conversation goes on only if
        the continuity check says the message carries it on, within [telegram] keep_days; otherwise the brain starts
        over and the next turn starts a new conversation."""
        with self._lock:
            self._restore(chat)
            current, quiet = chat.conversation, now - chat.last_at
            if not chat.last_at or quiet <= self._memory_s:
                return
        checks = current is not None and quiet <= self._keep_s and self._continuity is not None
        turns = self._turns(current, chat.person) if checks else []
        carries = bool(turns) and self._continuity.continues(turns, text, quiet, chat.person)
        with self._lock:
            if chat.conversation != current:  # something else moved it on meanwhile
                return
            if carries:
                chat.last_at = now
            else:
                self._start_afresh(chat)

    def _new_if_quiet(self, chat: Chat, now: float) -> None:
        """What TARS posts by itself after [telegram] memory_minutes of quiet starts a new conversation."""
        with self._lock:
            self._restore(chat)
            if chat.last_at and now - chat.last_at > self._memory_s:
                self._start_afresh(chat)

    def _start_afresh(self, chat: Chat) -> None:
        """The brain forgets the chat, and the next turn starts a new conversation. Call with the lock held."""
        if chat.conversation is not None and self._conversations is not None:
            self._log(self._conversations.end, chat.conversation, ts=chat.last_at or self._clock())
        chat.conversation, chat.last_at = None, 0.0
        chat.brain.start_over()

    def _restore(self, chat: Chat) -> None:
        """Once after a start: the chat's last conversation read back from the log into its brain, if it was within
        [telegram] keep_days, so a restart (an update) doesn't lose the thread. Call with the lock held."""
        if chat.restored:
            return
        chat.restored = True
        log = self._conversations
        if log is None or not self._keep_s:
            return
        conversation = self._log(log.latest_with, chat.person, TELEGRAM)
        turns = self._log(log.turns_of, conversation) if conversation is not None else None
        if not turns or self._clock() - turns[-1]["ts"] > self._keep_s:
            return
        asked = None
        for turn in turns:
            if turn["role"] == ROLE_PERSON:
                asked = f"[Speaker: {chat.person}] {VOICE_TAG + ' ' if turn['spoken'] else ''}{turn['text'] or ''}"
            elif turn["failed_at"] or not turn["text"]:
                asked = None  # unanswered: as the brain forgets a failed turn
            elif asked is not None:
                chat.brain.remember_exchange(asked, turn["text"])
                asked = None
            else:
                chat.brain.remember_said(turn["text"])
        chat.conversation, chat.last_at = conversation, turns[-1]["ts"]
        print(f"(Telegram: {chat.person}'s conversation from {_clock(chat.last_at)} read back)", flush=True)

    def _turns(self, conversation: int, person: str) -> list[tuple[str, str]]:
        """A conversation's turns as the continuity check reads them: (who, text), oldest first."""
        turns = self._log(self._conversations.turns_of, conversation) if self._conversations else None
        return [(person if t["role"] == ROLE_PERSON else "TARS", t["text"]) for t in turns or [] if t["text"]]

    def _keep_unanswered(self, chat: Chat, recording: np.ndarray | None, said: str = "", **failure) -> None:
        """A voice message that got no answer, kept with its recording if there is one, and what TARS said back or
        where it failed, so the web UI shows it came."""
        now = self._clock()
        conversation = self._current_conversation(chat, now)
        if conversation is None:
            return
        self._log(self._conversations.add_person_turn, conversation, "", recording, speaker=chat.person, ts=now)
        self._log(self._conversations.add_tars_turn, conversation, said, ts=now, **failure)

    def _current_conversation(self, chat: Chat, now: float) -> int | None:
        with self._lock:
            conversation = self._conversation(chat, now)
            chat.last_at = now
        return conversation

    def start_over(self, chat: Chat) -> None:
        """/new: the brain forgets the chat so far, and the next message starts a new conversation on the web UI."""
        with chat.busy, self._lock:
            if chat.conversation is not None and self._conversations is not None:
                self._log(self._conversations.end, chat.conversation, ts=chat.last_at or self._clock())
            chat.conversation, chat.last_at = None, 0.0
            chat.brain.start_over()

    def announce(self, line: str) -> None:
        """What TARS says on its own, later, about something a chat asked for (how a task on the TV went): posted to
        the chat that asked last, once its exchange is over, and part of its conversation."""
        chat = self._last_asked
        if chat is None:
            print(f"(Telegram: nobody to tell: {line})")
            return
        with chat.busy:
            try:
                self._send(chat.chat_id, line, tries=SEND_TRIES)
            except TelegramError as e:
                print(f"(Telegram: couldn't tell {chat.person}: {line} ({e}))")
                return
            self._said(chat, line, self._clock())

    def _said(self, chat: Chat, text: str, now: float) -> None:
        """TARS wrote first (a reminder, or how one said at home went): part of the brain's conversation, so "snooze
        that" right after knows what "that" is, and kept in the chat's conversation, or one of its own."""
        self._new_if_quiet(chat, now)
        chat.brain.remember_said(text)
        if (conversation := self._current_conversation(chat, now)) is not None:
            self._log(self._conversations.add_tars_turn, conversation, text, ts=now)

    def _log_turn(self, conversation: int | None, text: str, brain: Brain, **failure) -> int | None:
        if conversation is None:
            return None
        return self._log(
            self._conversations.add_tars_turn,
            conversation,
            text,
            ts=self._clock(),
            answered_by=brain.answered_by,
            quick_service=brain.quick_service,
            **failure,
        )

    @staticmethod
    def _log(write, *args, **kwargs):
        """Keeping the chat must never cost a reply: a failure is printed, and the chat carries on."""
        try:
            return write(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - any failure to write, by design
            print(f"(couldn't save this to the log: {e!r})")
            return None
