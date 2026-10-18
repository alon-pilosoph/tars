"""TARS on Telegram: the household chats with it in writing, and it sends them what it has for them.

`voice-assistant --telegram` is a process of its own, like the web UI, so a problem with Telegram never touches the
voice. It long-polls Telegram's Bot API for messages, so nothing at home is reachable from the internet: no webhook,
no open port. Only the people in [telegram] people are answered; anyone else is ignored, and logged once, so a new
person's chat id can be found.

Each person has their own conversation (the brain's history, kept until it's quiet for [telegram] memory_minutes,
or they send /new; what TARS posts by itself is part of it) with the same
brain, tools and reminders as at home, told it's writing rather than speaking; a voice message is written down by
the same speech to text as at home (stt.py), and answered as if written. What it sends goes into the chat as
well as onto the TARS page. A reminder set here for someone with a chat comes to that chat when it's due, and
whatever TARS sends at home for someone with a chat comes to it too. See docs/telegram.md.
"""

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

from .audio import SAMPLE_RATE, decode_recording
from .config import ConfigError
from .conversations import FILE, LINK, LIST, LLM, NOTE, OTHER, STT, ConversationLog, SentItem
from .events import person_key
from .llm import REMIND_TOOLS, SEARCHING, THINKING, Brain, split_ack
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
)
from .stt import Transcriber, transcribe_recording

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
# Telegram saying a chat can't get messages from the bot, for good: blocked (403), or no such chat (400). Trying
# again won't help.
UNREACHABLE = (400, 403)
DELIVERY_REST_S = 30.0
SEND_TRIES = 3
TYPING_EVERY_S = 4.0  # "typing..." shows for 5 s
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
    "Hello, {name}. It's TARS. Ask me anything, have me set a timer or a reminder, or ask me to send you something. "
    "Send /new to start a new conversation."
)
FRESH = "Starting fresh. What's up?"
# The bot's menu of commands in Telegram's app.
COMMANDS = [{"command": "new", "description": "Start a new conversation"}]
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


class Bot:
    """The few methods of Telegram's Bot API that TARS uses. The token is part of every address, so no error says
    which address failed: it would end up in the logs."""

    def __init__(self, token: str, http: httpx.Client | None = None):
        self._base = f"{API}/bot{token}"
        self._files = f"{API}/file/bot{token}"
        self._http = http or httpx.Client(timeout=httpx.Timeout(POLL_S + 10, connect=10))

    def call(self, method: str, files: dict | None = None, **params):
        try:
            if files:
                response = self._http.post(f"{self._base}/{method}", data=params, files=files)
            else:
                response = self._http.post(f"{self._base}/{method}", json=params)
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
        """New messages, waiting up to POLL_S for one. Asking with `offset` confirms everything before it."""
        params = {"timeout": POLL_S, "allowed_updates": ["message"]}
        return self.call("getUpdates", **params, **({"offset": offset} if offset is not None else {}))

    def send(self, chat_id: int, text: str) -> None:
        for part in split_text(text):
            self.call("sendMessage", chat_id=chat_id, text=part)

    def send_file(self, chat_id: int, name: str, data: bytes, mime: str, caption: str = "") -> None:
        params = {"chat_id": str(chat_id), "caption": caption[:MAX_CAPTION]}
        self.call("sendDocument", files={"document": (name, data, mime)}, **params)

    def typing(self, chat_id: int) -> None:
        self.call("sendChatAction", chat_id=chat_id, action="typing")

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
    return f"{datetime.fromtimestamp(at).astimezone():%I:%M %p}".lstrip("0")


def tidy(reply: str) -> str:
    """A reply written to be heard, for reading: without the lines said while a slow answer comes ("Looking it
    up."), which a chat shows as "typing..." instead."""
    text = reply.strip().removeprefix(SEARCHING).replace(THINKING, "", 1)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


@dataclass
class Chat:
    """One person's chat: their own conversation with the brain, and the one kept for the web UI."""

    person: str
    chat_id: int
    brain: Brain
    conversation: int | None = None
    last_at: float = 0.0
    # Held while TARS answers in the chat, or posts to it by itself: one at a time, never one inside the other.
    busy: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


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
    ):
        """`people`: name → chat id, the only chats answered. `make_brain`: a new brain, one for each person, so
        each has their own conversation. `conversations`: where chats are kept for the web UI (None: nowhere).
        `send_items`: also post what TARS sends at home for someone with a chat. `transcriber`: speech to text for
        voice messages (None: they're turned down). `clock`, `typing` and `sleep` are for the tests."""
        self._bot, self._make_brain = bot, make_brain
        self._chats = {chat_id: Chat(name, chat_id, make_brain()) for name, chat_id in people.items()}
        self._by_person = {person_key(name): chat_id for name, chat_id in people.items()}
        self._conversations, self._tools = conversations, reminder_tools
        self._memory_s, self._send_items, self._clock, self._typing = memory_s, send_items, clock, typing
        self._sleep, self._transcriber = sleep, transcriber
        self._ignored: set[int] = set()
        # A chat's conversation is kept from two threads: the one answering, and the one posting what's due.
        self._lock = threading.Lock()

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
        offset, failures = None, 0
        while True:
            try:
                updates = self._bot.updates(offset)
                failures = 0
            except TelegramError as e:
                if e.status in (401, 404):
                    raise ConfigError("Telegram refused TELEGRAM_BOT_TOKEN: check it in .env.") from None
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
        self.answer(chat, text)

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
            recording, stage, start = None, OTHER, time.perf_counter()
            try:
                with _Typing(self._bot, chat.chat_id) if self._typing else nullcontext():
                    data = self._bot.download(voice["file_id"])
                    stage = STT
                    recording = decode_recording(data)
                    if len(recording) > MAX_VOICE_S * SAMPLE_RATE:  # Telegram's duration is a claim; this is the audio
                        self._send(chat.chat_id, TOO_LONG)
                        return
                    text = transcribe_recording(self._transcriber, recording) if len(recording) >= MIN_VOICE else ""
            except Exception as e:  # noqa: BLE001 - said in the chat, and kept as a failed turn with where it failed
                print(f"(Telegram: couldn't listen to {chat.person}'s voice message: {e!r})", flush=True)
                self._keep_unanswered(chat, recording, failed_at=stage, error=f"{type(e).__name__}: {e}")
                self._send(chat.chat_id, COULDNT_LISTEN)
                return
            print(f"(Telegram: {chat.person}'s voice message, {time.perf_counter() - start:.1f} s to write down)")
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
        conversation = self._current_conversation(chat, now)
        if conversation is not None:
            self._log(self._conversations.add_person_turn, conversation, text, recording, speaker=chat.person, ts=now)
        brain, acks = chat.brain, None
        asked = f"[Speaker: {chat.person}] {VOICE_TAG + ' ' if recording is not None else ''}{text}"
        try:
            with _Typing(self._bot, chat.chat_id) if self._typing else nullcontext():
                pieces = brain.stream_reply(asked)
                if self._tools:
                    acks, pieces = split_ack(pieces)
                reply = tidy("".join(pieces))
            if reply:
                self._send(chat.chat_id, reply, tries=SEND_TRIES)
        except Exception as e:  # noqa: BLE001 - said in the chat, kept in the log, and forgotten by the brain
            brain.forget_last()
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
        if acks is not None and self._tools:
            self._ack(acks, chat.person, changed={c.id for c in changes if c.id is not None})
        for change in changes:
            self._apply(change, chat, conversation)

    def deliver(self) -> None:
        """Posts the reminders due on Telegram, how those set here and said at home went, and what TARS sent at home
        for someone with a chat. A chat in the middle of an exchange is left for the next round, so nothing TARS
        posts by itself lands between a question and its answer. Raises TelegramError when Telegram can't be reached;
        what wasn't posted is tried again next time."""
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
        if self._conversations and self._send_items:
            for row in self._conversations.undelivered(since=now - ITEMS_WITHIN_S):
                chat = self._chat_for(row["for_name"])
                if chat is None:
                    continue
                with _free(chat) as free:
                    if free:
                        self._post_sent_at_home(chat, row, now)

    def _chat_for(self, person: str | None) -> Chat | None:
        chat_id = self._by_person.get(person_key(person))
        return self._chats[chat_id] if chat_id is not None else None

    def _post_reminder(self, chat: Chat, r: dict, now: float) -> None:
        reminders = self._tools.reminders
        text = " ".join(t for t in (line(r), late(r, now)) if t)
        try:
            self._bot.send(chat.chat_id, text)
        except TelegramError as e:
            if e.status not in UNREACHABLE:
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
            if e.status not in UNREACHABLE:
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
        except OSError as e:  # its file is gone: there's nothing to post, now or later
            print(f"(Telegram: couldn't read {row['title']!r} to post it: {e!r})")
            log.mark_delivered(row["id"], now)
            return
        try:
            self._post_item(chat.chat_id, item, raises=True)
        except TelegramError as e:
            if e.status not in UNREACHABLE:
                raise
            print(f"(Telegram: can't post {row['title']!r} ({e}); it's on the TARS page)")
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
            if item.kind == FILE:
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
        """Raises TelegramError after `tries` failures of one part of `text`; a single try's failure is only printed.
        Each part is tried on its own, so one that failed doesn't post those before it again."""
        for part in split_text(text):
            for attempt in range(tries):
                try:
                    self._bot.send(chat_id, part)
                    break
                except TelegramError as e:
                    if tries == 1:
                        print(f"(Telegram: couldn't send a message: {e})")
                        return
                    if attempt == tries - 1:
                        raise
                    self._sleep(e.retry_after or RETRY_S[attempt])

    def _conversation(self, chat: Chat, now: float) -> int | None:
        """The chat's conversation for the web UI: the same one until it goes quiet for as long as the brain
        remembers, or someone deletes it there. Call with the lock held."""
        log = self._conversations
        if log is None:
            return None
        current = chat.conversation
        if current is not None and now - chat.last_at <= self._memory_s and self._log(log.exists, current):
            return current
        if current is not None:
            self._log(log.end, current, ts=chat.last_at or now)
        chat.conversation = self._log(log.start, None, ts=now, via=TELEGRAM)
        return chat.conversation

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

    def _said(self, chat: Chat, text: str, now: float) -> None:
        """TARS wrote first (a reminder, or how one said at home went): part of the brain's conversation, so "snooze
        that" right after knows what "that" is, and kept in the chat's conversation, or one of its own."""
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
